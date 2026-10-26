"""Portfolio (cross-sectional) plan type: JSON shape, validator, tuning hooks, weight builder.

A portfolio plan is discriminated by ``"kind": "portfolio"``; single-symbol plans have no
``kind`` and go through :func:`lab.plan.validator.validate_plan` unchanged. Phase 10 / H1:
long-only top quantile of the point-in-time universe ranked on a trailing return, rebalanced
every ``every`` rows, equal or inverse-volatility weights, optional rank banding (hysteresis)
and an optional cross-sectional dispersion gate.

The **universe** (``top_n``, quote, eligibility, liquidity cap) comes from ``lab.toml
[universe]`` and is frozen into the run fingerprint; it is never part of a plan. The plan's
interval must equal the universe interval, which the caller passes in (this module reads no
config).

Plans hold no prices or levels: the score is a return-scale statistic, quantiles are
fractions of the eligible set, and the dispersion gate's ``min`` is a cross-sectional
standard deviation of returns (0.05 = 5 percentage points).

Errors carry a path (``selection.exit_quantile``) and a fix-it message, like the
single-symbol validator, so research agents can repair their own plans.

Tunable parameters
------------------
Every numeric leaf is tunable **except** ``rebalance.offset``: the offset only picks which
weekday the weekly rebalance lands on, and tuning it fits the calendar, not the hypothesis.
The plan *type* still accepts ``0 <= offset < every`` (the lab itself builds the other-offset
variants for run_adversarial's descriptive timing-luck table), but the research pipeline pins
it: :func:`pinned_offset_errors` rejects any submitted plan whose offset is not
:data:`LAB_REBALANCE_OFFSET` (0), in validate_plan and in every portfolio tool. Otherwise an
agent could read the timing-luck table and resubmit the best weekday, a selection the
deflated Sharpe's trial count and the walk-forward lineage never see.

Optional blocks contribute their leaves only when present (``exit_quantile`` when banding is
on, ``weighting.inverse_vol.lookback`` with inverse-vol weights, both gate leaves with a
gate), mirroring how the single-symbol validator lists ``risk.*`` only when set.

The single-symbol cap is 6 (``lab.plan.schema.MAX_FEATURE_PARAMS``). A portfolio plan with
every PLAN.md Phase 10 safeguard switched on (banding + inverse-vol + dispersion gate) has 9
tunable leaves, and the safeguards are what make H1 honest (turnover and regime are its
first-order risks), so the cap is raised to :data:`MAX_PORTFOLIO_PARAMS` = 9 for portfolio
plans only. The validator still enforces it, so a future multi-parameter score feature cannot
silently widen the search.

No look-ahead: :func:`build_signals` only composes causal panel functions from
:mod:`lab.registry.cross_section` (row t reads rows <= t). Weights are the decision at row
t's close; the portfolio engine fills them at t+1's open.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from lab.plan.schema import MAX_DESCRIPTION_CHARS, MAX_NAME_CHARS
from lab.plan.validator import PlanValidationError
from lab.registry import cross_section as xs
from lab.types import INTERVAL_SECONDS, PlanError

PORTFOLIO_KIND = "portfolio"

TOP_LEVEL_KEYS: tuple[str, ...] = (
    "kind",
    "name",
    "description",
    "interval",
    "score",
    "rebalance",
    "selection",
    "weighting",
    "max_weight",
    "dispersion_gate",
)
REQUIRED_KEYS: tuple[str, ...] = (
    "kind",
    "name",
    "interval",
    "score",
    "rebalance",
    "selection",
    "weighting",
)
REBALANCE_KEYS: tuple[str, ...] = ("every", "offset")
SELECTION_KEYS: tuple[str, ...] = ("quantile", "exit_quantile", "min_names")
WEIGHTING_SCHEMES: tuple[str, ...] = ("equal", "inverse_vol")
GATE_KEYS: tuple[str, ...] = ("lookback", "min")

# Bounds. Look-backs count rows of the universe's union index (days for "1d").
LOOKBACK_BOUNDS = (2, 365)  # score / inverse-vol / gate look-back, rows
EVERY_BOUNDS = (1, 60)  # rebalance every N rows; 60 days is the slowest sensible schedule
MAX_QUANTILE = 0.5  # top half at most: a wider "top" is the universe, not a selection
MIN_NAMES_BOUNDS = (1, 10)  # all cash when fewer eligible names; above 10 rarely fits top 30
DEFAULT_VOL_LOOKBACK = 30  # inverse-vol default and the null's vol-tercile panel: one month
DEFAULT_MAX_WEIGHT = 1.0
DEFAULT_MIN_NAMES = 1
DEFAULT_OFFSET = 0
# The research pipeline pins the rebalance weekday (module docstring, "Tunable parameters").
LAB_REBALANCE_OFFSET = 0
PINNED_OFFSET_MESSAGE = (
    "the rebalance weekday is fixed by the lab at offset 0; it is calendar, not hypothesis; "
    "timing luck across the other offsets is reported by run_adversarial (descriptive only, "
    'never resubmittable). Use "offset": 0 or omit it'
)
MAX_PORTFOLIO_PARAMS = 9  # see module docstring: every safeguard on = 9 tunable leaves

# Smallest values offered to a tuner where the validation low is exclusive.
QUANTILE_SEARCH_FLOOR = 0.01
MAX_WEIGHT_SEARCH_FLOOR = 0.01
GATE_MIN_SEARCH_BOUNDS = (0.001, 0.999)

# Keys an agent may reach for that a portfolio plan must not carry.
_UNIVERSE_KEYS = frozenset(
    {"symbol", "symbols", "universe", "top_n", "quote", "coins", "pairs", "eligibility"}
)
_SINGLE_SYMBOL_KEYS = frozenset({"features", "long_entry", "long_exit", "risk"})
_SHORT_KEYS = frozenset({"short", "short_entry", "short_exit", "bottom_quantile", "long_short"})
_LIQUIDITY_KEYS = frozenset({"liquidity_cap", "liquidity_cap_mult", "liquidity"})
_LEVEL_WORDS = ("price", "volume", "close", "open", "high", "low", "market_cap", "level")


# --------------------------------------------------------------------------- score features


@dataclass(frozen=True)
class ScoreFeature:
    """A cross-sectional score: ``compute(closes, lookback)`` -> panel, causal, return-scale."""

    name: str
    description: str
    lookback_low: int
    lookback_high: int
    compute: Callable[[pd.DataFrame, int], pd.DataFrame]


SCORE_FEATURES: Mapping[str, ScoreFeature] = {
    "trailing_return": ScoreFeature(
        name="trailing_return",
        description=(
            "Simple return over the last `lookback` rows (close_t / close_{t-lookback} - 1); "
            "higher ranks first"
        ),
        lookback_low=LOOKBACK_BOUNDS[0],
        lookback_high=LOOKBACK_BOUNDS[1],
        compute=xs.trailing_return,
    ),
}


# --------------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class DispersionGate:
    """Trade only on rebalances where the cross-sectional std of ``lookback``-row returns
    among universe members is at least ``min`` (a return-scale number, never a price)."""

    lookback: int
    min: float


@dataclass(frozen=True)
class PortfolioPlan:
    name: str
    description: str
    interval: str
    score_feature: str
    score_lookback: int
    rebalance_every: int
    rebalance_offset: int
    quantile: float
    exit_quantile: float | None
    min_names: int
    weighting: str  # "equal" | "inverse_vol"
    vol_lookback: int | None
    max_weight: float
    dispersion_gate: DispersionGate | None


@dataclass(frozen=True)
class PortfolioValidation:
    ok: bool
    plan: PortfolioPlan | None
    errors: tuple[str, ...]  # "path: message", one per problem


@dataclass(frozen=True)
class PortfolioSignals:
    weights: pd.DataFrame  # target weights (NaN rows = hold), union index x symbols
    schedule: pd.Series  # bool rebalance rows
    dispersion: pd.Series  # cross-sectional std of the score look-back's returns, every row
    inverse_vol: pd.DataFrame | None  # vol panel used for inverse-vol weights (None if equal)
    vol_panel: pd.DataFrame  # trailing vol (vol_lookback or 30) for the null's vol terciles
    # First schedule row on which the strategy COULD have invested: at least ``min_names``
    # sizable eligible names, whatever the dispersion gate says. Scoring (net Sharpe, MaxDD,
    # cost stress, every benchmark) starts here, so a gate that keeps the book in cash early on
    # is charged for those cash rows instead of being scored only from its first open gate.
    first_decision: pd.Timestamp | None
    investable: pd.Series  # bool per row: a schedule row where the ungated book holds a name
    gate: pd.Series | None = None  # bool per row: dispersion gate open (None without a gate)
    signal_delay: int = 0  # rows of staleness of every panel input (see build_signals)


# --------------------------------------------------------------------------- helpers


def _did_you_mean(word: str, options: tuple[str, ...] | list[str]) -> str:
    match = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return f" Did you mean `{match[0]}`?" if match else ""


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _type_name(v: object) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, str):
        return "string"
    if isinstance(v, Mapping):
        return "object"
    if isinstance(v, list | tuple):
        return "array"
    return type(v).__name__


def _fmt(v: float) -> str:
    return f"{int(v)}" if float(v).is_integer() else f"{v:g}"


def _looks_like_level(key: str) -> bool:
    k = key.lower()
    return any(word in k for word in _LEVEL_WORDS)


def _level_message(key: str) -> str:
    return (
        f"`{key}` looks like a price, volume or level; portfolio plans contain no prices or "
        "levels (they differ by coin and would hide a fitted threshold). Liquidity filtering "
        "is lab.toml [universe] (top_n by trailing dollar volume); rank on a return-scale "
        "score instead. Remove it"
    )


def _unknown_keys(
    obj: Mapping[str, Any], allowed: tuple[str, ...], base: str, errors: list[PlanError]
) -> None:
    for key in obj:
        if key in allowed:
            continue
        path = f"{base}.{key}" if base else str(key)
        if not isinstance(key, str):
            errors.append(PlanError(base or "$", f"keys must be strings, got {key!r}"))
        elif _looks_like_level(key):
            errors.append(PlanError(path, _level_message(key)))
        else:
            where = f"`{base}`" if base else "a portfolio plan"
            errors.append(
                PlanError(
                    path,
                    f"unknown key `{key}` in {where}.{_did_you_mean(key, allowed)} Allowed: "
                    f"{', '.join(allowed)}",
                )
            )


def _check_number(
    obj: Mapping[str, Any],
    key: str,
    path: str,
    errors: list[PlanError],
    *,
    kind: str,
    low: float,
    high: float,
    low_open: bool = False,
    high_open: bool = False,
    what: str,
    example: float,
    required: bool = True,
    default: float | None = None,
) -> float | None:
    """Validate one numeric leaf; returns the value (int for ``kind="int"``) or ``None``."""
    rng = f"{'(' if low_open else '['}{_fmt(low)}, {_fmt(high)}{')' if high_open else ']'}"
    noun = "whole number" if kind == "int" else "number"
    if key not in obj:
        if required:
            errors.append(
                PlanError(
                    path,
                    f"missing required `{key}`: {what}, a {noun} in {rng}; e.g. "
                    f'"{key}": {_fmt(example)}',
                )
            )
            return None
        return default
    v = obj[key]
    if not _is_number(v) or not math.isfinite(float(v)):
        errors.append(
            PlanError(
                path,
                f"must be a {noun} in {rng} ({what}), got {_type_name(v)} {v!r}; "
                f"e.g. {_fmt(example)}",
            )
        )
        return None
    fv = float(v)
    if kind == "int" and not fv.is_integer():
        errors.append(
            PlanError(path, f"must be a whole number in {rng}, got {v!r}; use {round(fv)}")
        )
        return None
    too_low = fv <= low if low_open else fv < low
    too_high = fv >= high if high_open else fv > high
    if too_low or too_high:
        clamp = example if (low_open and too_low) or (high_open and too_high) else None
        if clamp is None:
            clamp = min(max(fv, low), high)
        errors.append(
            PlanError(
                path,
                f"{_fmt(fv)} is out of range; `{key}` must be in {rng} ({what}). Try {_fmt(clamp)}",
            )
        )
        return None
    return int(fv) if kind == "int" else fv


def _object(
    raw: Mapping[str, Any], key: str, errors: list[PlanError], example: str
) -> Mapping[str, Any] | None:
    if key not in raw:
        return None
    v = raw[key]
    if not isinstance(v, Mapping):
        errors.append(
            PlanError(key, f'must be an object, e.g. "{key}": {example}; got {_type_name(v)}')
        )
        return None
    return v


# --------------------------------------------------------------------------- sections

_REQUIRED_HINTS: Mapping[str, str] = {
    "kind": f'Add "kind": "{PORTFOLIO_KIND}"',
    "name": 'Add a short name, e.g. "name": "xs_momentum_28d_top_quintile"',
    "interval": 'Add the universe interval, e.g. "interval": "1d"',
    "score": 'Add the ranking score, e.g. "score": {"trailing_return": {"lookback": 28}}',
    "rebalance": 'Add the schedule, e.g. "rebalance": {"every": 7, "offset": 0}',
    "selection": 'Add the selection, e.g. "selection": {"quantile": 0.2, "min_names": 3}',
    "weighting": 'Add the weighting, e.g. "weighting": {"equal": {}}',
}


def _check_top_level(raw: Mapping[str, Any], errors: list[PlanError]) -> None:
    for key in raw:
        if key in TOP_LEVEL_KEYS:
            continue
        if not isinstance(key, str):
            errors.append(PlanError("$", f"keys must be strings, got {key!r}"))
        elif key in _UNIVERSE_KEYS:
            errors.append(
                PlanError(
                    key,
                    f"`{key}` is not part of a portfolio plan: the universe (top_n coins by "
                    "trailing dollar volume, quote asset, eligibility) comes from lab.toml "
                    "[universe] and is frozen into the run. Remove it",
                )
            )
        elif key in _SINGLE_SYMBOL_KEYS:
            errors.append(
                PlanError(
                    key,
                    f"`{key}` belongs to single-symbol plans (no `kind`); a portfolio plan ranks "
                    "coins with `score` and picks them with `selection`. Remove it, or drop "
                    '"kind" to write a single-symbol plan',
                )
            )
        elif key in _SHORT_KEYS:
            errors.append(
                PlanError(
                    key,
                    f"`{key}` is not supported: portfolio plans are long-only (Binance spot has "
                    "no shorts); unpicked coins are held as cash. Remove it",
                )
            )
        elif key in _LIQUIDITY_KEYS:
            errors.append(
                PlanError(
                    key,
                    f"`{key}` is set in lab.toml [universe] and frozen into the run fingerprint, "
                    "not chosen per plan. Remove it",
                )
            )
        elif _looks_like_level(key):
            errors.append(PlanError(key, _level_message(key)))
        else:
            errors.append(
                PlanError(
                    key,
                    f"unknown key `{key}`.{_did_you_mean(key, TOP_LEVEL_KEYS)} Allowed keys: "
                    f"{', '.join(TOP_LEVEL_KEYS)}",
                )
            )
    for key in REQUIRED_KEYS:
        if key not in raw:
            errors.append(PlanError(key, f"missing required key `{key}`. {_REQUIRED_HINTS[key]}"))


def _check_kind(raw: Mapping[str, Any], errors: list[PlanError]) -> None:
    if "kind" not in raw:
        return
    kind = raw["kind"]
    if kind != PORTFOLIO_KIND:
        errors.append(
            PlanError(
                "kind",
                f'must be "{PORTFOLIO_KIND}" for a portfolio plan, got {_type_name(kind)} '
                f'{kind!r}. Single-symbol plans omit "kind" and use validate_plan',
            )
        )


def _check_name(raw: Mapping[str, Any], errors: list[PlanError]) -> str:
    if "name" not in raw:
        return ""
    name = raw["name"]
    if not isinstance(name, str) or not name.strip():
        errors.append(
            PlanError("name", f"must be a non-empty string, got {_type_name(name)} {name!r}")
        )
        return ""
    if len(name) > MAX_NAME_CHARS:
        errors.append(
            PlanError("name", f"is {len(name)} characters; the limit is {MAX_NAME_CHARS}")
        )
    if any(ord(c) < 32 for c in name):
        errors.append(PlanError("name", "must not contain control characters or newlines"))
    return name.strip()


def _check_description(raw: Mapping[str, Any], errors: list[PlanError]) -> str:
    desc = raw.get("description")
    if desc is None:
        return ""
    if not isinstance(desc, str):
        errors.append(PlanError("description", f"must be a string, got {_type_name(desc)}"))
        return ""
    if len(desc) > MAX_DESCRIPTION_CHARS:
        errors.append(
            PlanError(
                "description", f"is {len(desc)} characters; the limit is {MAX_DESCRIPTION_CHARS}"
            )
        )
    return desc


def _check_interval(raw: Mapping[str, Any], interval: str, errors: list[PlanError]) -> str:
    if "interval" not in raw:
        return ""
    iv = raw["interval"]
    if iv != interval:
        known = (
            ""
            if isinstance(iv, str) and iv in INTERVAL_SECONDS
            else f" ({iv!r} is not a known interval)"
        )
        errors.append(
            PlanError(
                "interval",
                f"portfolio plans run on the universe interval (lab.toml [universe] interval "
                f'= "{interval}"), got {_type_name(iv)} {iv!r}{known}. Use "interval": '
                f'"{interval}"',
            )
        )
        return ""
    return interval


def _check_score(raw: Mapping[str, Any], errors: list[PlanError]) -> tuple[str, int] | None:
    example = '{"trailing_return": {"lookback": 28}}'
    score = _object(raw, "score", errors, example)
    if score is None:
        return None
    names = tuple(SCORE_FEATURES)
    if len(score) != 1:
        errors.append(
            PlanError(
                "score",
                "must have exactly one key, the score feature, whose value is its params, e.g. "
                f'"score": {example}; got keys {sorted(map(str, score))}. Score features: '
                f"{', '.join(names)}",
            )
        )
        return None
    ((fname, params),) = score.items()
    spec = SCORE_FEATURES.get(fname) if isinstance(fname, str) else None
    if spec is None:
        fname_s = str(fname)
        level = (
            " Raw prices and volumes are levels that differ by coin and cannot be ranked across "
            "coins; use a return-scale score."
            if _looks_like_level(fname_s)
            else ""
        )
        errors.append(
            PlanError(
                f"score.{fname_s}",
                f"unknown score feature `{fname_s}`.{_did_you_mean(fname_s, names)}{level} "
                f"Score features: {', '.join(names)}",
            )
        )
        return None
    base = f"score.{fname}"
    if not isinstance(params, Mapping):
        errors.append(
            PlanError(
                base, f'params must be an object like {{"lookback": 28}}, got {_type_name(params)}'
            )
        )
        return None
    _unknown_keys(params, ("lookback",), base, errors)
    lookback = _check_number(
        params,
        "lookback",
        f"{base}.lookback",
        errors,
        kind="int",
        low=spec.lookback_low,
        high=spec.lookback_high,
        what="rows of the universe index the return is measured over (days for 1d)",
        example=28,
    )
    if lookback is None:
        return None
    return fname, int(lookback)


def _check_rebalance(raw: Mapping[str, Any], errors: list[PlanError]) -> tuple[int, int] | None:
    reb = _object(raw, "rebalance", errors, '{"every": 7, "offset": 0}')
    if reb is None:
        return None
    _unknown_keys(reb, REBALANCE_KEYS, "rebalance", errors)
    every = _check_number(
        reb,
        "every",
        "rebalance.every",
        errors,
        kind="int",
        low=EVERY_BOUNDS[0],
        high=EVERY_BOUNDS[1],
        what="rows between rebalances; 7 is weekly on 1d bars",
        example=7,
    )
    if every is None:
        # Still type-check offset, but its upper bound needs `every`.
        _check_number(
            reb,
            "offset",
            "rebalance.offset",
            errors,
            kind="int",
            low=0,
            high=EVERY_BOUNDS[1] - 1,
            what="row of the first rebalance",
            example=0,
            required=False,
            default=DEFAULT_OFFSET,
        )
        return None
    offset = _check_number(
        reb,
        "offset",
        "rebalance.offset",
        errors,
        kind="int",
        low=0,
        high=int(every) - 1,
        what=f"row of the first rebalance, 0 to every - 1 = {int(every) - 1}",
        example=0,
        required=False,
        default=DEFAULT_OFFSET,
    )
    if offset is None:
        return None
    return int(every), int(offset)


def _check_selection(
    raw: Mapping[str, Any], errors: list[PlanError]
) -> tuple[float, float | None, int] | None:
    sel = _object(raw, "selection", errors, '{"quantile": 0.2, "exit_quantile": 0.3}')
    if sel is None:
        return None
    _unknown_keys(sel, SELECTION_KEYS, "selection", errors)
    quantile = _check_number(
        sel,
        "quantile",
        "selection.quantile",
        errors,
        kind="float",
        low=0.0,
        high=MAX_QUANTILE,
        low_open=True,
        what="fraction of the eligible coins to hold; 0.2 is the top quintile",
        example=0.2,
    )
    exit_quantile: float | None = None
    exit_ok = True
    if sel.get("exit_quantile") is not None:
        low = quantile if quantile is not None else 0.0
        exit_quantile = _check_number(
            sel,
            "exit_quantile",
            "selection.exit_quantile",
            errors,
            kind="float",
            low=0.0,
            high=1.0,
            low_open=True,
            what=(
                "rank banding: a held coin stays while its rank is within this wider fraction; "
                "must be >= quantile, or null for no banding"
            ),
            example=min(1.0, max(0.3, low)),
        )
        exit_ok = exit_quantile is not None
        if exit_quantile is not None and quantile is not None and exit_quantile < quantile:
            errors.append(
                PlanError(
                    "selection.exit_quantile",
                    f"{_fmt(exit_quantile)} is below quantile {_fmt(quantile)}; the exit band "
                    "must be at least as wide as the entry band (held coins leave only when "
                    f"they fall below it). Use a value in [{_fmt(quantile)}, 1], e.g. "
                    f"{_fmt(min(1.0, round(quantile * 1.5, 4)))}, or null for no banding",
                )
            )
            exit_ok = False
    min_names = _check_number(
        sel,
        "min_names",
        "selection.min_names",
        errors,
        kind="int",
        low=MIN_NAMES_BOUNDS[0],
        high=MIN_NAMES_BOUNDS[1],
        what="fewest coins to hold; the portfolio is all cash when fewer are eligible",
        example=3,
        required=False,
        default=DEFAULT_MIN_NAMES,
    )
    if quantile is None or min_names is None or not exit_ok:
        return None
    return float(quantile), exit_quantile, int(min_names)


def _check_weighting(
    raw: Mapping[str, Any], errors: list[PlanError]
) -> tuple[str, int | None] | None:
    example = '{"equal": {}} or {"inverse_vol": {"lookback": 30}}'
    w = _object(raw, "weighting", errors, example)
    if w is None:
        return None
    if len(w) != 1:
        errors.append(
            PlanError(
                "weighting",
                f"must have exactly one key, the scheme: {example}; got keys {sorted(map(str, w))}",
            )
        )
        return None
    ((scheme, params),) = w.items()
    if scheme not in WEIGHTING_SCHEMES:
        s = str(scheme)
        errors.append(
            PlanError(
                f"weighting.{s}",
                f"unknown weighting `{s}`.{_did_you_mean(s, WEIGHTING_SCHEMES)} Allowed: "
                f"{', '.join(WEIGHTING_SCHEMES)}",
            )
        )
        return None
    base = f"weighting.{scheme}"
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        errors.append(PlanError(base, f"params must be an object, got {_type_name(params)}"))
        return None
    if scheme == "equal":
        _unknown_keys(params, (), base, errors)
        if params:
            return None
        return "equal", None
    _unknown_keys(params, ("lookback",), base, errors)
    lookback = _check_number(
        params,
        "lookback",
        f"{base}.lookback",
        errors,
        kind="int",
        low=LOOKBACK_BOUNDS[0],
        high=LOOKBACK_BOUNDS[1],
        what="rows of daily log returns in the trailing volatility used for 1/vol weights",
        example=DEFAULT_VOL_LOOKBACK,
        required=False,
        default=DEFAULT_VOL_LOOKBACK,
    )
    if lookback is None or any(k != "lookback" for k in params):
        return None
    return "inverse_vol", int(lookback)


def _check_max_weight(raw: Mapping[str, Any], errors: list[PlanError]) -> float | None:
    v = _check_number(
        raw,
        "max_weight",
        "max_weight",
        errors,
        kind="float",
        low=0.0,
        high=1.0,
        low_open=True,
        what="cap on any one coin's weight; the excess stays in cash",
        example=0.25,
        required=False,
        default=DEFAULT_MAX_WEIGHT,
    )
    return None if v is None else float(v)


def _check_gate(
    raw: Mapping[str, Any], errors: list[PlanError]
) -> tuple[bool, DispersionGate | None]:
    """Returns ``(ok, gate)``; ``gate`` is None when absent or null."""
    gate = raw.get("dispersion_gate")
    if gate is None:
        return True, None
    if not isinstance(gate, Mapping):
        errors.append(
            PlanError(
                "dispersion_gate",
                'must be null or an object like {"lookback": 28, "min": 0.05}; got '
                f"{_type_name(gate)}",
            )
        )
        return False, None
    n_errors = len(errors)
    _unknown_keys(gate, GATE_KEYS, "dispersion_gate", errors)
    lookback = _check_number(
        gate,
        "lookback",
        "dispersion_gate.lookback",
        errors,
        kind="int",
        low=LOOKBACK_BOUNDS[0],
        high=LOOKBACK_BOUNDS[1],
        what="rows of the trailing return whose cross-sectional std is the dispersion",
        example=28,
    )
    gmin: float | None = None
    raw_min = gate.get("min")
    if _is_number(raw_min) and math.isfinite(float(raw_min)) and float(raw_min) >= 1.0:
        errors.append(
            PlanError(
                "dispersion_gate.min",
                f"{_fmt(float(raw_min))} is not a dispersion: `min` is the cross-sectional "
                "standard deviation of trailing returns among universe coins (0.05 = 5 "
                "percentage points), a return-scale number in (0, 1), never a price or level. "
                "Typical values are 0.02 to 0.3; e.g. 0.05",
            )
        )
    else:
        gmin = _check_number(
            gate,
            "min",
            "dispersion_gate.min",
            errors,
            kind="float",
            low=0.0,
            high=1.0,
            low_open=True,
            high_open=True,
            what="minimum cross-sectional std of trailing returns to trade (0.05 = 5 points)",
            example=0.05,
        )
    if lookback is None or gmin is None or len(errors) != n_errors:
        return False, None
    return True, DispersionGate(lookback=int(lookback), min=float(gmin))


# --------------------------------------------------------------------------- entry points


def _parse_raw(raw: Mapping[str, Any] | str) -> tuple[Mapping[str, Any] | None, list[PlanError]]:
    if isinstance(raw, str):

        def reject(token: str) -> float:
            raise ValueError(f"{token} is not valid JSON; use a finite number or null")

        try:
            obj = json.loads(raw, parse_constant=reject)
        except json.JSONDecodeError as e:
            return None, [
                PlanError("$", f"invalid JSON: {e.msg} at line {e.lineno} column {e.colno}")
            ]
        except (ValueError, RecursionError) as e:
            return None, [PlanError("$", f"invalid JSON: {e}")]
    else:
        obj = raw
    if not isinstance(obj, Mapping):
        return None, [
            PlanError(
                "$",
                f"plan must be a JSON object with keys {', '.join(REQUIRED_KEYS)}; got "
                f"{_type_name(obj)}",
            )
        ]
    return obj, []


def is_portfolio_plan(raw: object) -> bool:
    """True when ``raw`` is a mapping (or JSON object text) with ``"kind": "portfolio"``.

    Use it to route a plan: True -> :func:`validate_portfolio_plan`, False -> the
    single-symbol :func:`lab.plan.validator.validate_plan` (which rejects a stray ``kind``).
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return False
    return isinstance(raw, Mapping) and raw.get("kind") == PORTFOLIO_KIND


def pinned_offset_errors(plan: PortfolioPlan) -> list[PlanError]:
    """Research-pipeline rule on top of the plan type: ``rebalance.offset`` must be
    :data:`LAB_REBALANCE_OFFSET`. Empty when the plan complies. validate_plan and every
    portfolio tool apply it (module docstring, "Tunable parameters")."""
    if plan.rebalance_offset == LAB_REBALANCE_OFFSET:
        return []
    return [
        PlanError(
            "rebalance.offset",
            f"{plan.rebalance_offset} is not allowed: {PINNED_OFFSET_MESSAGE}",
        )
    ]


def _validate(
    raw: Mapping[str, Any] | str, interval: str
) -> tuple[PortfolioPlan | None, list[PlanError]]:
    obj, errors = _parse_raw(raw)
    if obj is None:
        return None, errors
    _check_top_level(obj, errors)
    _check_kind(obj, errors)
    name = _check_name(obj, errors)
    description = _check_description(obj, errors)
    iv = _check_interval(obj, interval, errors)
    score = _check_score(obj, errors)
    rebalance = _check_rebalance(obj, errors)
    selection = _check_selection(obj, errors)
    weighting = _check_weighting(obj, errors)
    max_weight = _check_max_weight(obj, errors)
    _, gate = _check_gate(obj, errors)
    if errors:
        return None, errors
    assert score is not None and rebalance is not None and selection is not None
    assert weighting is not None and max_weight is not None
    plan = PortfolioPlan(
        name=name,
        description=description,
        interval=iv,
        score_feature=score[0],
        score_lookback=score[1],
        rebalance_every=rebalance[0],
        rebalance_offset=rebalance[1],
        quantile=selection[0],
        exit_quantile=selection[1],
        min_names=selection[2],
        weighting=weighting[0],
        vol_lookback=weighting[1],
        max_weight=max_weight,
        dispersion_gate=gate,
    )
    n_tunable = len(tunable_params(plan))
    if n_tunable > MAX_PORTFOLIO_PARAMS:  # pragma: no cover - reachable once scores grow params
        errors.append(
            PlanError(
                "$",
                f"this plan has {n_tunable} tunable params; the portfolio limit is "
                f"{MAX_PORTFOLIO_PARAMS}. Drop banding, the gate, or inverse-vol weighting",
            )
        )
        return None, errors
    return plan, []


def validate_portfolio_plan(raw: Mapping[str, Any] | str, *, interval: str) -> PortfolioValidation:
    """Validate a portfolio plan dict or JSON text against the universe ``interval``.

    Collects every problem; each error string is ``"path: message"`` with a fix-it hint.
    """
    plan, errors = _validate(raw, interval)
    if plan is None:
        return PortfolioValidation(False, None, tuple(f"{e.path}: {e.message}" for e in errors))
    return PortfolioValidation(True, plan, ())


def plan_to_dict(plan: PortfolioPlan) -> dict[str, Any]:
    """Canonical JSON-able dict, defaults filled; validating it gives back ``plan``."""
    weighting: dict[str, Any] = (
        {"equal": {}}
        if plan.weighting == "equal"
        else {"inverse_vol": {"lookback": plan.vol_lookback}}
    )
    gate = (
        None
        if plan.dispersion_gate is None
        else {"lookback": plan.dispersion_gate.lookback, "min": plan.dispersion_gate.min}
    )
    return {
        "kind": PORTFOLIO_KIND,
        "name": plan.name,
        "description": plan.description,
        "interval": plan.interval,
        "score": {plan.score_feature: {"lookback": plan.score_lookback}},
        "rebalance": {"every": plan.rebalance_every, "offset": plan.rebalance_offset},
        "selection": {
            "quantile": plan.quantile,
            "exit_quantile": plan.exit_quantile,
            "min_names": plan.min_names,
        },
        "weighting": weighting,
        "max_weight": plan.max_weight,
        "dispersion_gate": gate,
    }


def portfolio_plan_hash(plan: PortfolioPlan) -> str:
    """sha256 hex of the canonical plan JSON (sorted keys, no whitespace)."""
    text = json.dumps(plan_to_dict(plan), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tunable_params(plan: PortfolioPlan) -> dict[str, tuple[float, float, str]]:
    """Dotted path -> ``(low, high, "int" | "float")`` search bounds for every tunable leaf.

    ``rebalance.offset`` is never tunable (see the module docstring). Optional leaves appear
    only when the plan uses them. Bounds are the validation bounds, with small floors where
    the validation low is exclusive. Cross-field rule a sampler must respect:
    ``selection.exit_quantile >= selection.quantile`` (:func:`with_params` raises otherwise).
    """
    score = SCORE_FEATURES[plan.score_feature]
    out: dict[str, tuple[float, float, str]] = {
        f"score.{plan.score_feature}.lookback": (
            float(score.lookback_low),
            float(score.lookback_high),
            "int",
        ),
        "rebalance.every": (float(EVERY_BOUNDS[0]), float(EVERY_BOUNDS[1]), "int"),
        "selection.quantile": (QUANTILE_SEARCH_FLOOR, MAX_QUANTILE, "float"),
    }
    if plan.exit_quantile is not None:
        out["selection.exit_quantile"] = (QUANTILE_SEARCH_FLOOR, 1.0, "float")
    out["selection.min_names"] = (float(MIN_NAMES_BOUNDS[0]), float(MIN_NAMES_BOUNDS[1]), "int")
    if plan.weighting == "inverse_vol":
        out["weighting.inverse_vol.lookback"] = (
            float(LOOKBACK_BOUNDS[0]),
            float(LOOKBACK_BOUNDS[1]),
            "int",
        )
    out["max_weight"] = (MAX_WEIGHT_SEARCH_FLOOR, 1.0, "float")
    if plan.dispersion_gate is not None:
        out["dispersion_gate.lookback"] = (
            float(LOOKBACK_BOUNDS[0]),
            float(LOOKBACK_BOUNDS[1]),
            "int",
        )
        out["dispersion_gate.min"] = (*GATE_MIN_SEARCH_BOUNDS, "float")
    return out


def tunable_values(plan: PortfolioPlan) -> dict[str, float]:
    """Current value of every :func:`tunable_params` path (handy for neighbourhoods)."""
    data = plan_to_dict(plan)
    out: dict[str, float] = {}
    for path in tunable_params(plan):
        node: Any = data
        for part in path.split("."):
            node = node[part]
        out[path] = float(node)
    return out


def with_params(plan: PortfolioPlan, overrides: Mapping[str, float]) -> PortfolioPlan:
    """New validated plan with ``overrides`` (keys from :func:`tunable_params`) applied.

    Int leaves are rounded to the nearest whole number. Unknown keys (including
    ``rebalance.offset``) and out-of-range or inconsistent values raise
    :class:`lab.plan.validator.PlanValidationError` (a ``ValueError``).
    """
    tunables = tunable_params(plan)
    data = plan_to_dict(plan)
    errors: list[PlanError] = []
    for key, value in overrides.items():
        if key not in tunables:
            note = ""
            if key == "rebalance.offset":
                note = (
                    " `rebalance.offset` is fixed: tuning the rebalance weekday fits the "
                    "calendar, not the hypothesis."
                )
            errors.append(
                PlanError(
                    str(key),
                    f"unknown tunable param `{key}`.{_did_you_mean(str(key), tuple(tunables))}"
                    f"{note} Tunable: {', '.join(tunables)}",
                )
            )
            continue
        v: object = value
        if _is_number(value) and math.isfinite(float(value)) and tunables[key][2] == "int":
            v = round(float(value))
        *parents, leaf = key.split(".")
        node: Any = data
        for part in parents:
            node = node[part]
        node[leaf] = v
    if errors:
        raise PlanValidationError(errors)
    new, errs = _validate(data, plan.interval)
    if new is None:
        raise PlanValidationError(errs)
    return new


def plan_logic(plan: PortfolioPlan) -> str:
    """Logic identity for hypothesis families: what the plan does, with no numbers.

    Re-tuning any number (look-backs, quantiles, schedule, caps, gate threshold) keeps the
    logic; switching the score feature, the weighting scheme, banding on/off, or the gate
    on/off changes it.
    """
    banding = "on" if plan.exit_quantile is not None else "off"
    gate = "on" if plan.dispersion_gate is not None else "off"
    return (
        f"{PORTFOLIO_KIND}|score={plan.score_feature}|weighting={plan.weighting}"
        f"|banding={banding}|gate={gate}"
    )


# --------------------------------------------------------------------------- JSON schema


def portfolio_plan_json_schema() -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for a portfolio plan. The cross-field rules
    (``exit_quantile >= quantile``, interval = universe interval) are checked by
    :func:`validate_portfolio_plan`; the schema shows ``rebalance.offset`` as the constant the
    research pipeline accepts (:func:`pinned_offset_errors`)."""

    def integer(low: int, high: int, desc: str, default: int | None = None) -> dict[str, Any]:
        s: dict[str, Any] = {"type": "integer", "minimum": low, "maximum": high}
        if default is not None:
            s["default"] = default
        s["description"] = desc
        return s

    lookback = integer(LOOKBACK_BOUNDS[0], LOOKBACK_BOUNDS[1], "rows (days for 1d)")
    score_branches = [
        {
            "type": "object",
            "properties": {
                name: {
                    "type": "object",
                    "properties": {
                        "lookback": integer(spec.lookback_low, spec.lookback_high, "rows")
                    },
                    "required": ["lookback"],
                    "additionalProperties": False,
                }
            },
            "required": [name],
            "additionalProperties": False,
            "description": spec.description,
        }
        for name, spec in sorted(SCORE_FEATURES.items())
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Portfolio plan (long-only cross-sectional, point-in-time universe)",
        "description": (
            "The universe (top_n by trailing dollar volume, quote, eligibility, liquidity cap) "
            "comes from lab.toml [universe], never from the plan. No prices or levels. "
            f"At most {MAX_PORTFOLIO_PARAMS} tunable numeric leaves (rebalance.offset is fixed at "
            f"{LAB_REBALANCE_OFFSET})."
        ),
        "type": "object",
        "properties": {
            "kind": {"const": PORTFOLIO_KIND},
            "name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_CHARS},
            "description": {"type": "string", "maxLength": MAX_DESCRIPTION_CHARS},
            "interval": {
                "type": "string",
                "enum": list(INTERVAL_SECONDS),
                "description": "must equal the universe interval (lab.toml [universe] interval)",
            },
            "score": {"oneOf": score_branches},
            "rebalance": {
                "type": "object",
                "properties": {
                    "every": integer(
                        EVERY_BOUNDS[0], EVERY_BOUNDS[1], "rows between rebalances (7 = weekly)"
                    ),
                    "offset": {
                        "type": "integer",
                        "const": LAB_REBALANCE_OFFSET,
                        "default": LAB_REBALANCE_OFFSET,
                        "description": "fixed by the lab at 0 (the rebalance weekday is "
                        "calendar, not hypothesis); run_adversarial reports the other offsets "
                        "as descriptive timing luck",
                    },
                },
                "required": ["every"],
                "additionalProperties": False,
            },
            "selection": {
                "type": "object",
                "properties": {
                    "quantile": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": MAX_QUANTILE,
                        "description": "fraction of eligible coins held (0.2 = top quintile)",
                    },
                    "exit_quantile": {
                        "type": ["number", "null"],
                        "exclusiveMinimum": 0,
                        "maximum": 1,
                        "description": "rank banding: held coins stay within this wider "
                        "fraction; >= quantile; null = no banding",
                    },
                    "min_names": integer(
                        MIN_NAMES_BOUNDS[0],
                        MIN_NAMES_BOUNDS[1],
                        "all cash when fewer coins are eligible",
                        DEFAULT_MIN_NAMES,
                    ),
                },
                "required": ["quantile"],
                "additionalProperties": False,
            },
            "weighting": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"equal": {"type": "object", "additionalProperties": False}},
                        "required": ["equal"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "inverse_vol": {
                                "type": "object",
                                "properties": {
                                    "lookback": {**lookback, "default": DEFAULT_VOL_LOOKBACK}
                                },
                                "additionalProperties": False,
                            }
                        },
                        "required": ["inverse_vol"],
                        "additionalProperties": False,
                    },
                ]
            },
            "max_weight": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 1,
                "default": DEFAULT_MAX_WEIGHT,
                "description": "cap per coin; the excess stays in cash",
            },
            "dispersion_gate": {
                "oneOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "properties": {
                            "lookback": lookback,
                            "min": {
                                "type": "number",
                                "exclusiveMinimum": 0,
                                "exclusiveMaximum": 1,
                                "description": "cross-sectional std of trailing returns "
                                "(0.05 = 5 points); a return-scale number, never a price",
                            },
                        },
                        "required": ["lookback", "min"],
                        "additionalProperties": False,
                    },
                ]
            },
        },
        "required": list(REQUIRED_KEYS),
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------- signals


def _stale(panel: pd.DataFrame, rows: int, fill: object = np.nan) -> pd.DataFrame:
    """``panel`` read ``rows`` rows late (row t holds row t - rows; ``fill`` before that)."""
    if rows == 0:
        return panel
    return panel.shift(rows, fill_value=fill)


def build_signals(
    plan: PortfolioPlan,
    closes: pd.DataFrame,
    members: pd.DataFrame,
    *,
    dollar_volume: pd.DataFrame | None,
    liquidity_cap_mult: float | None,
    signal_delay: int = 0,
) -> PortfolioSignals:
    """Target weights and diagnostics for ``plan`` on a close panel.

    ``closes``: union index x symbols, NaN = no bar (see :func:`lab.registry.cross_section.
    close_panel`). ``members``: bool frame aligned with ``closes`` (the point-in-time universe,
    e.g. ``top_n_members(liquidity_rank(...), top_n)``). With ``liquidity_cap_mult`` set,
    ``dollar_volume`` (aligned with ``closes``) gives each member's liquidity share and each
    weight is capped at ``liquidity_cap_mult * share``; without it ``dollar_volume`` is unused.

    Steps (all causal, row t reads rows <= t): score = the plan's score feature; schedule =
    ``rebalance_rows(every, offset)``; dispersion = cross-sectional std among members of the
    score look-back's trailing return; gate (when set) = dispersion of the gate look-back's
    trailing return >= ``min`` (NaN -> closed, all cash); then ``top_quantile_weights``.

    ``signal_delay`` (rows, >= 0) is the stale-information adversarial check: the decision on
    schedule row t is made on the SAME schedule, but every panel input to it (scores,
    members, dispersion and the gate, the inverse-vol / vol panel, liquidity shares) is the
    one of row ``t - signal_delay`` (NaN / not a member / gate closed where that row does not
    exist). Shifting the finished weights instead would move the rebalance to another
    weekday and measure timing luck, not staleness. ``investable`` and ``first_decision``
    are computed from the same stale inputs.

    ``first_decision`` is the first schedule row with at least ``min_names`` sizable eligible
    names, gate or no gate (:class:`PortfolioSignals`); ``investable`` flags every such row.
    """
    if not isinstance(plan, PortfolioPlan):
        raise ValueError(f"plan must be a PortfolioPlan, got {type(plan).__name__}")
    if not isinstance(closes, pd.DataFrame) or not isinstance(members, pd.DataFrame):
        raise ValueError("closes and members must be pandas DataFrames (union index x symbols)")
    if liquidity_cap_mult is not None and dollar_volume is None:
        raise ValueError(
            "liquidity_cap_mult is set but dollar_volume is None; pass the trailing dollar-volume "
            "panel aligned with closes, or liquidity_cap_mult=None"
        )
    if (
        isinstance(signal_delay, bool)
        or not isinstance(signal_delay, int | np.integer)
        or signal_delay < 0
    ):
        raise ValueError(f"signal_delay must be a whole number of rows >= 0, got {signal_delay!r}")
    d = int(signal_delay)

    spec = SCORE_FEATURES[plan.score_feature]
    scores = _stale(spec.compute(closes, plan.score_lookback), d)
    schedule = xs.rebalance_rows(
        closes.index, every=plan.rebalance_every, offset=plan.rebalance_offset
    )
    score_returns = xs.trailing_return(closes, plan.score_lookback)
    dispersion = xs.cross_sectional_dispersion(score_returns, members)

    gate_series: pd.Series | None = None
    if plan.dispersion_gate is not None:
        g = plan.dispersion_gate
        gate_disp = (
            dispersion
            if g.lookback == plan.score_lookback
            else xs.cross_sectional_dispersion(xs.trailing_return(closes, g.lookback), members)
        )
        gd = gate_disp.to_numpy(dtype=np.float64)
        with np.errstate(invalid="ignore"):
            open_ = np.isfinite(gd) & (gd >= g.min)
        gate_series = pd.Series(open_, index=closes.index, name="gate", dtype=bool)
        if d:
            gate_series = gate_series.shift(d, fill_value=False).astype(bool)
    if d:
        dispersion = dispersion.shift(d)

    vol_panel = _stale(xs.trailing_volatility(closes, plan.vol_lookback or DEFAULT_VOL_LOOKBACK), d)
    inverse_vol = vol_panel if plan.weighting == "inverse_vol" else None

    liquidity: pd.DataFrame | None = None
    if liquidity_cap_mult is not None:
        assert dollar_volume is not None
        liquidity = _stale(xs.liquidity_share(dollar_volume, members), d)
    stale_members = _stale(members, d, False).astype(bool) if d else members

    def weights_for(gate: pd.Series | None) -> pd.DataFrame:
        return xs.top_quantile_weights(
            scores,
            stale_members,
            schedule,
            quantile=plan.quantile,
            min_names=plan.min_names,
            inverse_vol=inverse_vol,
            max_weight=plan.max_weight,
            exit_quantile=plan.exit_quantile,
            liquidity_share=liquidity,
            liquidity_cap_mult=liquidity_cap_mult,
            gate=gate,
        )

    weights = weights_for(gate_series)
    ungated = weights if gate_series is None else weights_for(None)

    w = ungated.to_numpy(dtype=np.float64)
    with np.errstate(invalid="ignore"):
        can_invest = schedule.to_numpy(dtype=bool) & (np.nan_to_num(w, nan=0.0) > 0).any(axis=1)
    investable = pd.Series(can_invest, index=closes.index, name="investable", dtype=bool)
    hits = np.flatnonzero(can_invest)
    first = pd.Timestamp(closes.index[hits[0]]) if hits.size else None

    return PortfolioSignals(
        weights=weights,
        schedule=schedule,
        dispersion=dispersion,
        inverse_vol=inverse_vol,
        vol_panel=vol_panel,
        first_decision=first,
        investable=investable,
        gate=gate_series,
        signal_delay=d,
    )


__all__ = [
    "DEFAULT_VOL_LOOKBACK",
    "LAB_REBALANCE_OFFSET",
    "MAX_PORTFOLIO_PARAMS",
    "PINNED_OFFSET_MESSAGE",
    "PORTFOLIO_KIND",
    "SCORE_FEATURES",
    "DispersionGate",
    "PortfolioPlan",
    "PortfolioSignals",
    "PortfolioValidation",
    "ScoreFeature",
    "build_signals",
    "is_portfolio_plan",
    "pinned_offset_errors",
    "plan_logic",
    "plan_to_dict",
    "portfolio_plan_hash",
    "portfolio_plan_json_schema",
    "tunable_params",
    "tunable_values",
    "validate_portfolio_plan",
    "with_params",
]
