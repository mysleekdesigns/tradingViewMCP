"""Behaviour families: which plans are the SAME hypothesis (PLAN.md Phase 8 guardrails).

:func:`lab.research.logic.logic_hash` is syntactic. A no-op clause (``or ema_fast < 0``,
``and rsi > -1``), a stop-loss that never triggers, a re-ordered expression, or the same rule
moved to another interval with scaled periods gives a new hash for a plan that trades exactly
like the original, which would buy a second holdout look and a fresh walk-forward / sweep
history. A behaviour family adds a behavioural key:

* the plan's own logic hash (no interval: the same rule on another bar size is the same
  hypothesis), plus
* every logic hash with a SUBMITTED design-window trial (source ``"api"`` or ``"manual"``:
  the base trial of a backtest, from a research run OR any other run such as the web UI's
  Strategy Tester) on the same symbol whose per-bar net returns have Pearson correlation
  >= ``cfg.research.family_return_corr`` with this plan's base design-window trial.

How two return series are compared:

* same interval and the identical window: position by position (the bars are the same);
* anything else (another interval, another window): each trial's bar open times are rebuilt
  from the candles the same way the runner built them (:func:`design_bar_index`: the loader's
  bars before ``holdout_start`` (:func:`lab.engine.splits.design_view`), sliced to the trial's
  ``[window_start, window_end)`` with :func:`lab.engine.splits.slice_window`). Both series are
  turned into log returns and summed onto the coarser grid (the least common multiple of the
  two bar sizes, buckets aligned to UNIX time) BY TIMESTAMP. A bar whose predecessor is
  missing (a gap) is not used, and a coarse bucket counts only when every one of its finer bars
  is present, so a missing bar is a gap, never a zero that shifts the rest. The correlation is
  over the common coarse bars and needs at least ``cfg.research.family_min_overlap_bars`` of
  them.
* a comparison that cannot be made (no candles for that interval, a stored series whose length
  no longer matches the candles, too little overlap, non-finite values) is UNKNOWN, never a
  silent "not correlated". Unknown candidates with the plan's feature multiset
  (:func:`lab.research.logic.feature_multiset`, e.g. two EMAs) are kept in
  :attr:`FamilyInfo.uncertain`, and the holdout guard treats them as family (fails closed).

A constant (zero-variance) return series correlates with nothing. Optuna trials never admit a
logic into the family (a near-always-long corner of an unrelated sweep would merge unrelated
ideas); they only feed the walk-forward lineage (:func:`tried_ranges`).

The family is used for: the holdout refusal (any family member, or uncertain same-feature
candidate, already checked in any run), the walk-forward lineage (:func:`wf_lineage_problems`:
the walk-forward space must cover every value any family plan tried in any run, and the plan's
own logic, plus every family logic with a plan correlated >= ``cfg.research.
literal_lineage_corr`` (:attr:`FamilyInfo.literal_kin`), must not have hand-tuned rule
literals), the walk-forward row (minimum OOS Sharpe over the family's walk-forward evidence in
every run), and the sweep-PBO basis.

Why a second, higher bar for literals: at the family threshold genuinely different trend
followers merge (EMA cross vs ``roc > 0`` correlate ~0.88 on BTC 4h), and one hand-tuned
``roc > 0`` / ``roc > 1`` would then block the walk-forward of every trend follower for the
ledger's life. A renamed, re-tuned, no-op or otherwise cosmetic copy of a tuned logic trades
almost identically (correlation ~1.0), so it still meets the higher bar. For this, plans of an
already-admitted logic keep being compared until one reaches the bar; an unknown comparison of
an admitted logic counts as reaching it (fail closed).

Performance: candidate rows are read without their returns, filtered by logic hash (rows of the
plan's own logic, or of a logic already admitted at the literal bar, are never decoded),
de-duplicated per plan hash, and only then decoded one at a time. A family is cached per
(ledger file, plan, base trial, newest trial id, config, loader) within a process: the ledger
is append-only, so a family is still valid while no trial was added.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import json
import logging
import math
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from lab.config import LabConfig
from lab.engine.splits import design_view, slice_window
from lab.ledger import Ledger
from lab.plan import parse_plan, tunable_params
from lab.plan.validator import PlanValidationError
from lab.research.logic import feature_multiset, literal_vector, logic_hash, param_match_tokens
from lab.types import Plan, Window, interval_seconds

__all__ = [
    "CORRELATION_SOURCES",
    "FamilyInfo",
    "aligned_correlation",
    "behaviour_family",
    "clear_family_cache",
    "design_bar_index",
    "evidence_in_family",
    "family_evidence",
    "family_holdout_conflict",
    "plan_logic",
    "tried_literals",
    "tried_ranges",
    "wf_lineage_check",
    "wf_lineage_problems",
]

_log = logging.getLogger(__name__)

# Trial sources whose design-window returns can put another logic into this plan's family:
# submitted base trials only (a backtest's own plan). Optuna trials feed the lineage only.
CORRELATION_SOURCES: tuple[str, ...] = ("api", "manual")
_EPS = 1e-9  # float slack when comparing a searched range with tried values
_CACHE_MAX = 128

# A loader as the runner uses it: (symbol, interval, end) -> candles with bars < end.
Loader = Callable[[str, str, pd.Timestamp], pd.DataFrame]


@dataclass(frozen=True)
class FamilyInfo:
    """A plan's behaviour family (module docstring)."""

    logic_hashes: set[str]
    plan_hashes: set[str]
    base_trial_id: int | None = None  # the plan's base design-window trial; None = not found
    # other logic -> the first correlation at/above the threshold that admitted it
    correlated: dict[str, float] = field(default_factory=dict)
    # other logic with the plan's feature multiset whose comparison was UNKNOWN (reason);
    # the holdout guard treats these as family (fail closed)
    uncertain: dict[str, str] = field(default_factory=dict)
    n_unknown: int = 0  # comparisons that could not be made (any feature multiset)
    # family logics whose hand-tuned rule literals block this plan's walk-forward: its own logic
    # plus logics with a plan correlated >= cfg.research.literal_lineage_corr (or an unknown
    # comparison, fail closed). None = every family logic (the conservative default).
    literal_kin: set[str] | None = None

    def note(self) -> str:
        """One line for criteria notes."""
        extra = len(self.logic_hashes) - 1 if self.logic_hashes else 0
        if self.base_trial_id is None:
            return "behaviour family = this logic only (no base design-window trial to correlate)"
        text = f"behaviour family = this logic + {extra} correlated logic(s)"
        if self.n_unknown:
            text += (
                f"; {self.n_unknown} comparison(s) could not be aligned "
                f"({len(self.uncertain)} same-feature logic(s) treated as family for the holdout)"
            )
        return text


# --------------------------------------------------------------------------- cached parsing


@lru_cache(maxsize=65_536)
def _logic_of_text(plan_json: str) -> str | None:
    try:
        return logic_hash(plan_json)
    except (PlanValidationError, ValueError, TypeError):
        return None


def plan_logic(plan: str | Mapping[str, Any] | None) -> str | None:
    """Logic hash of a plan given as JSON text or a dict (cached by text); None if invalid."""
    if plan is None:
        return None
    text = plan if isinstance(plan, str) else json.dumps(plan, sort_keys=True)
    return _logic_of_text(text)


@lru_cache(maxsize=65_536)
def _multiset_of_text(plan_json: str) -> tuple[str, ...] | None:
    try:
        return feature_multiset(plan_json)
    except (PlanValidationError, ValueError, TypeError):
        return None


@lru_cache(maxsize=65_536)
def _literals_of_text(plan_json: str) -> tuple[float, ...] | None:
    try:
        return literal_vector(plan_json)
    except (PlanValidationError, ValueError, TypeError):
        return None


@lru_cache(maxsize=65_536)
def _plan_values(plan_json: str) -> tuple[tuple[frozenset[str], float], ...]:
    """``(match tokens, value)`` for every tunable of the plan in ``plan_json``."""
    try:
        p = parse_plan(plan_json)
        return tuple(
            (param_match_tokens(p, key), float(spec.default))
            for key, spec in tunable_params(p).items()
        )
    except (PlanValidationError, ValueError, TypeError):
        return ()


@lru_cache(maxsize=65_536)
def _key_tokens(plan_json: str, key: str) -> frozenset[str]:
    try:
        return param_match_tokens(parse_plan(plan_json), key)
    except (PlanValidationError, ValueError, TypeError):
        return frozenset()


def _text(plan: Plan | Mapping[str, Any] | str) -> str:
    if isinstance(plan, str):
        return plan
    if isinstance(plan, Plan):
        from lab.plan import plan_to_dict

        return json.dumps(plan_to_dict(plan), sort_keys=True)
    return json.dumps(plan, sort_keys=True)


# --------------------------------------------------------------------------- correlation


def _correlation(a: np.ndarray, b: np.ndarray) -> float | None:
    """Pearson correlation, or None for different lengths, non-finite values, or a constant
    series (zero variance correlates with nothing)."""
    if a.shape != b.shape or a.size < 3:
        return None
    if not (np.isfinite(a).all() and np.isfinite(b).all()):
        return None
    sa, sb = float(np.std(a)), float(np.std(b))
    if sa == 0.0 or sb == 0.0:
        return None
    value = float(np.corrcoef(a, b)[0, 1])
    return value if math.isfinite(value) else None


def _to_grid(
    ts: np.ndarray, logret: np.ndarray, step: int, target: int
) -> tuple[np.ndarray, np.ndarray]:
    """Sum the log returns of bars (open times ``ts`` in UNIX seconds, ``step`` seconds apart)
    onto ``target``-second buckets aligned to UNIX time. A bar counts only when the bar before
    it is exactly ``step`` earlier (its return then spans exactly its own bar); a bucket is kept
    only when all ``target // step`` of its bars count. Returns ``(bucket starts, sums)``."""
    n = ts.size
    valid = np.zeros(n, dtype=bool)
    if n > 1:
        valid[1:] = np.diff(ts) == step
    bucket = (ts // target) * target
    starts, inverse = np.unique(bucket, return_inverse=True)
    sums = np.bincount(inverse, weights=np.where(valid, logret, 0.0), minlength=starts.size)
    counts = np.bincount(inverse, weights=valid.astype(np.float64), minlength=starts.size)
    keep = counts == float(target // step)
    return starts[keep], sums[keep]


def aligned_correlation(
    a_ts: np.ndarray,
    a_returns: np.ndarray,
    a_step: int,
    b_ts: np.ndarray,
    b_returns: np.ndarray,
    b_step: int,
    *,
    min_overlap: int,
) -> tuple[float | None, str | None]:
    """Correlation of two per-bar simple-return series given their bar open times (UNIX
    seconds) and bar sizes, aligned BY TIMESTAMP on the coarser common grid (module
    docstring). Returns ``(corr, None)``; ``(None, None)`` when a series is constant on the
    overlap (correlates with nothing); ``(None, reason)`` when the comparison is unknown."""
    if a_ts.size != a_returns.size or b_ts.size != b_returns.size:
        return None, "bar times and returns differ in length"
    if not (np.isfinite(a_returns).all() and np.isfinite(b_returns).all()):
        return None, "non-finite returns"
    if (a_returns <= -1.0).any() or (b_returns <= -1.0).any():
        return None, "a return of -100% or worse has no log return"
    target = math.lcm(int(a_step), int(b_step))
    ga, va = _to_grid(a_ts, np.log1p(a_returns), int(a_step), target)
    gb, vb = _to_grid(b_ts, np.log1p(b_returns), int(b_step), target)
    _, ia, ib = np.intersect1d(ga, gb, assume_unique=True, return_indices=True)
    if ia.size < min_overlap:
        return None, f"only {ia.size} common {target // 60}-minute bars (< {min_overlap})"
    return _correlation(va[ia], vb[ib]), None


def design_bar_index(
    loader: Loader,
    cfg: LabConfig,
    symbol: str,
    interval: str,
    window_start: int,
    window_end: int,
    *,
    _history: dict[tuple[str, str], np.ndarray | str] | None = None,
) -> np.ndarray:
    """Bar open times (UNIX seconds) of the design-window trial ``[window_start, window_end)``
    on ``symbol`` / ``interval``, rebuilt from the candles the way the runner builds a trial's
    bars: ``loader(symbol, interval, holdout_start)``, :func:`design_view`, then
    :func:`slice_window`. Raises ``ValueError`` (or the loader's error) when the candles are not
    available."""
    key = (symbol, interval)
    cached = _history.get(key) if _history is not None else None
    if isinstance(cached, str):
        raise ValueError(cached)
    if cached is None:
        try:
            raw = loader(symbol, interval, cfg.splits.holdout_start)
            history = design_view(raw, cfg.splits)
            cached = np.asarray(pd.DatetimeIndex(history.index).as_unit("s").asi8, dtype=np.int64)
        except Exception as exc:  # any loader failure makes the comparison unknown
            message = f"no {symbol} {interval} candles to rebuild bar times ({exc})"
            if _history is not None:
                _history[key] = message
            raise ValueError(message) from exc
        if _history is not None:
            _history[key] = cached
    window = Window(
        start=pd.Timestamp(window_start, unit="s", tz="UTC"),
        end=pd.Timestamp(window_end, unit="s", tz="UTC"),
        kind="design",
    )
    frame = pd.DataFrame(
        index=pd.DatetimeIndex(pd.to_datetime(cached, unit="s", utc=True), name="time")
    )
    return np.asarray(slice_window(frame, window).index.as_unit("s").asi8, dtype=np.int64)


# --------------------------------------------------------------------------- the family

_FAMILY_CACHE: OrderedDict[tuple[Any, ...], tuple[FamilyInfo, Any]] = OrderedDict()


def clear_family_cache() -> None:
    """Forget every cached family (tests; the cache is also bounded)."""
    _FAMILY_CACHE.clear()


def _default_loader(cfg: LabConfig) -> Loader:
    from lab.runner import default_loader  # lazy: runner is heavy and imports the engine

    return default_loader(cfg)


def behaviour_family(
    ledger: Ledger,
    *,
    symbol: str,
    plan_hash: str,
    cfg: LabConfig,
    plan: Plan | Mapping[str, Any] | str | None = None,
    loader: Loader | None = None,
) -> FamilyInfo:
    """The behaviour family of the plan ``plan_hash`` on ``symbol`` (module docstring).

    ``plan`` (the plan itself) gives its logic hash directly; without it the logic comes from
    the plan's trials in the ledger. The plan's base trial is its newest submitted (source
    ``"api"``) ok design-window trial with returns in any research run; without one, the family
    is the plan's own logic only (``base_trial_id`` None). ``loader`` rebuilds bar times for
    comparisons across intervals or windows (default: the lab's parquet loader for ``cfg``).
    ``plan_hashes`` holds every plan hash tried (selection sources, any window but the holdout,
    ANY run) whose logic is in the family, plus ``plan_hash`` itself."""
    plans = ledger.research_plans(symbol=symbol, research_only=False)
    base = ledger.latest_research_base_trial(plan_hash)
    if plan is not None:
        own = logic_hash(plan)
    else:
        own = plan_logic(plans.get(plan_hash)) or (
            plan_logic(base.plan_json) if base is not None else None
        )
    if base is None or base.symbol != symbol or base.returns is None:
        logic_hashes = {own} if own else set()
        return FamilyInfo(
            logic_hashes=logic_hashes,
            plan_hashes={plan_hash}
            | {h for h, pj in plans.items() if plan_logic(pj) in logic_hashes},
            literal_kin=set(logic_hashes),
        )
    load = loader if loader is not None else _default_loader(cfg)
    key: tuple[Any, ...] | None = None
    if ledger.path is not None:
        key = (
            str(ledger.path),
            plan_hash,
            symbol,
            own,
            base.id,
            ledger.max_trial_id(),
            len(ledger.research_runs()),
            json.dumps(asdict(cfg.research), sort_keys=True),
            cfg.splits.holdout_start.isoformat(),
            id(load) if loader is not None else str(cfg.parquet_dir),
        )
        hit = _FAMILY_CACHE.get(key)
        if hit is not None:
            _FAMILY_CACHE.move_to_end(key)
            return hit[0]
    info = _compute_family(ledger, symbol, plan_hash, cfg, plan, own, base, plans, load)
    if key is not None:
        # The loader is held with the entry so its id() cannot be reused while cached.
        _FAMILY_CACHE[key] = (info, load if loader is not None else None)
        while len(_FAMILY_CACHE) > _CACHE_MAX:
            _FAMILY_CACHE.popitem(last=False)
    return info


def _compute_family(
    ledger: Ledger,
    symbol: str,
    plan_hash: str,
    cfg: LabConfig,
    plan: Plan | Mapping[str, Any] | str | None,
    own: str | None,
    base: Any,
    plans: Mapping[str, str],
    load: Loader,
) -> FamilyInfo:
    threshold = cfg.research.family_return_corr
    literal_bar = max(threshold, cfg.research.literal_lineage_corr)
    min_overlap = cfg.research.family_min_overlap_bars
    logic_hashes: set[str] = {own} if own else set()
    literal_kin: set[str] = {own} if own else set()
    correlated: dict[str, float] = {}
    uncertain: dict[str, str] = {}
    n_unknown = 0
    own_multiset = _multiset_of_text(_text(plan)) if plan is not None else None
    if own_multiset is None:
        own_multiset = _multiset_of_text(base.plan_json)
    mine = np.asarray(base.returns, dtype=np.float64)
    base_step = interval_seconds(base.interval)
    base_start = int(base.window_start.timestamp())
    base_end = int(base.window_end.timestamp())
    history: dict[tuple[str, str], np.ndarray | str] = {}
    times: dict[tuple[str, int, int], np.ndarray | str] = {}

    def bar_times(interval: str, start: int, end: int, n: int | None) -> np.ndarray | str:
        k = (interval, start, end)
        if k not in times:
            try:
                ts = design_bar_index(load, cfg, symbol, interval, start, end, _history=history)
            except ValueError as exc:
                times[k] = str(exc)
            else:
                times[k] = (
                    ts
                    if n is None or ts.size == n
                    else (
                        f"{ts.size} {interval} bars in the window but {n} stored returns (the "
                        "candles changed since the trial ran)"
                    )
                )
        return times[k]

    seen: set[str] = set()
    for meta in ledger.design_trial_meta(symbol=symbol, sources=CORRELATION_SOURCES):
        if meta["id"] == base.id or meta["plan_hash"] in seen:
            continue
        seen.add(meta["plan_hash"])  # newest trial per plan
        lh = plan_logic(meta["plan_json"])
        if lh is None or lh in literal_kin:
            continue  # excluded before any returns are decoded
        admitted = lh in logic_hashes  # in the family, but no plan at the literal bar yet
        same_grid = (
            meta["interval"] == base.interval
            and meta["window_start"] == base_start
            and meta["window_end"] == base_end
            and meta["n_returns"] == mine.size
        )
        reason: str | None = None
        corr: float | None = None
        if same_grid:
            other = ledger.trial_returns(meta["id"])
            if other is None:
                continue
            corr = _correlation(mine, np.asarray(other, dtype=np.float64))
        else:
            mine_ts = bar_times(base.interval, base_start, base_end, mine.size)
            other_ts = bar_times(
                meta["interval"], meta["window_start"], meta["window_end"], meta["n_returns"]
            )
            if isinstance(mine_ts, str):
                reason = f"this plan's bars: {mine_ts}"
            elif isinstance(other_ts, str):
                reason = other_ts
            else:
                other = ledger.trial_returns(meta["id"])
                if other is None:
                    continue
                corr, reason = aligned_correlation(
                    mine_ts,
                    mine,
                    base_step,
                    other_ts,
                    np.asarray(other, dtype=np.float64),
                    interval_seconds(meta["interval"]),
                    min_overlap=min_overlap,
                )
        if reason is not None and admitted:
            literal_kin.add(lh)  # cannot rule out a near-identical copy: fail closed
            continue
        if reason is not None:
            n_unknown += 1
            _log.info(
                "family of %s: trial %s not comparable: %s", plan_hash[:12], meta["id"], reason
            )
            if _multiset_of_text(meta["plan_json"]) == own_multiset and lh not in uncertain:
                uncertain[lh] = f"trial {meta['id']} ({meta['interval']}): {reason}"
            continue
        if corr is not None and corr >= literal_bar:
            literal_kin.add(lh)
        if corr is not None and corr >= threshold and not admitted:
            logic_hashes.add(lh)
            correlated[lh] = corr
            uncertain.pop(lh, None)
    plan_hashes = {plan_hash} | {h for h, pj in plans.items() if plan_logic(pj) in logic_hashes}
    return FamilyInfo(
        logic_hashes=logic_hashes,
        plan_hashes=plan_hashes,
        base_trial_id=base.id,
        correlated=correlated,
        uncertain={k: v for k, v in uncertain.items() if k not in logic_hashes},
        n_unknown=n_unknown,
        literal_kin=literal_kin & logic_hashes,
    )


def evidence_in_family(family: FamilyInfo, row_plan_hash: str, payload: Any) -> bool:
    """True when an evidence row belongs to the family: its plan hash is a family plan, or its
    payload's plan (recomputed with the current logic form) or stored logic hash is a family
    logic."""
    if row_plan_hash in family.plan_hashes:
        return True
    if not isinstance(payload, Mapping):
        return False
    plan = payload.get("plan")
    if isinstance(plan, Mapping) and plan_logic(plan) in family.logic_hashes:
        return True
    stored = payload.get("logic_hash")
    return isinstance(stored, str) and stored in family.logic_hashes


def family_evidence(ledger: Ledger, family: FamilyInfo, kind: str) -> list[dict[str, Any]]:
    """Evidence rows (``{id, created_at, run_id, plan_hash, kind, payload}``) of ``kind`` in
    EVERY research run that belong to ``family``, oldest first."""
    return [
        row
        for row in ledger.evidence(None, kind=kind)
        if isinstance(row["payload"], dict)
        and evidence_in_family(family, row["plan_hash"], row["payload"])
    ]


def family_holdout_conflict(ledger: Ledger, family: FamilyInfo) -> str | None:
    """A message naming the earlier holdout check of a family member (logic hash as stored,
    logic recomputed from the checked plan with the current canonical form, or plan hash), or
    of an UNCERTAIN same-feature candidate (fail closed), in any run; None when no family member
    or uncertain candidate was ever checked."""
    for check in ledger.holdout_checked():
        stored = check["logic_hash"]
        recomputed = plan_logic(check["plan_json"])
        logics = {x for x in (stored, recomputed) if isinstance(x, str)}
        if logics & family.logic_hashes or check["plan_hash"] in family.plan_hashes:
            return (
                f"plan {str(check['plan_hash'])[:12]} of the same behaviour family (the same "
                "logic on any interval, or submitted design-window returns correlated >= the "
                "lab's family threshold after aligning bars by timestamp, finer intervals summed "
                f"onto the coarser grid) was already holdout-checked in run {check['run_id']}"
            )
        hit = sorted(logics & set(family.uncertain))
        if hit:
            return (
                f"plan {str(check['plan_hash'])[:12]} (same features as this plan) was already "
                f"holdout-checked in run {check['run_id']}, and its returns could not be "
                f"compared with this plan's ({family.uncertain[hit[0]]}); an unknown comparison "
                "is treated as the same hypothesis (fail closed)"
            )
    return None


# --------------------------------------------------------------------------- WF lineage


def _family_plans(ledger: Ledger, family: FamilyInfo, symbol: str) -> dict[str, str]:
    """``plan_hash -> plan_json`` of every family plan tried in ANY run (selection sources,
    any window but the holdout)."""
    return {
        h: pj
        for h, pj in ledger.research_plans(symbol=symbol, research_only=False).items()
        if h in family.plan_hashes or plan_logic(pj) in family.logic_hashes
    }


def tried_ranges(
    ledger: Ledger, family: FamilyInfo, plan: Plan
) -> dict[str, tuple[float, float, int]]:
    """``{tunable key of plan: (min, max, n values)}`` over every value tried for that param
    by any trial (selection sources, any window but the holdout) of any family plan in ANY run
    (research runs and the web UI's), plus the declared bounds of every family Optuna study.
    Values are matched to ``plan``'s keys with :func:`lab.research.logic.param_match_tokens`
    (raw values: a period tried on another interval is not rescaled, which can only widen the
    required range). Keys never tried are absent."""
    by_token: dict[str, list[str]] = {}
    for key in tunable_params(plan):
        for token in param_match_tokens(plan, key):
            by_token.setdefault(token, []).append(key)
    values: dict[str, list[float]] = {}

    def add(tokens: Iterable[str], value: float) -> None:
        if not math.isfinite(value):
            return
        for key in {k for t in tokens for k in by_token.get(t, ())}:
            values.setdefault(key, []).append(value)

    for pj in _family_plans(ledger, family, plan.symbol).values():
        for tokens, value in _plan_values(pj):
            add(tokens, value)
    for pj, space_json in ledger.research_study_spaces(symbol=plan.symbol, research_only=False):
        if plan_logic(pj) not in family.logic_hashes:
            continue
        try:
            space = json.loads(space_json)
        except ValueError:
            continue
        if not isinstance(space, dict):
            continue
        for key, bounds in space.items():
            if isinstance(bounds, list) and len(bounds) == 2:
                tokens = _key_tokens(pj, str(key))
                for b in bounds:
                    if isinstance(b, int | float) and not isinstance(b, bool):
                        add(tokens, float(b))
    return {k: (min(v), max(v), len(v)) for k, v in values.items()}


def tried_literals(
    ledger: Ledger, family: FamilyInfo, plan: Plan
) -> dict[str, set[tuple[float, ...]]]:
    """``{logic hash: {literal vector, ...}}`` (:func:`lab.research.logic.literal_vector`) over
    every plan tried in ANY run of the plan's own logic or of a family logic in
    :attr:`FamilyInfo.literal_kin` (correlated >= ``cfg.research.literal_lineage_corr``; every
    family logic when it is None), plus ``plan``'s own vector under its logic. A family logic
    below that bar (a genuinely different idea that merely trends alike) is left out, so its
    hand-tuned literals do not block this plan's walk-forward."""
    own = logic_hash(plan)
    kin = family.logic_hashes if family.literal_kin is None else family.literal_kin
    allowed = (set(kin) & family.logic_hashes) | {own}
    out: dict[str, set[tuple[float, ...]]] = {}
    for pj in _family_plans(ledger, family, plan.symbol).values():
        lh, vec = plan_logic(pj), _literals_of_text(pj)
        if lh is not None and vec is not None and lh in allowed:
            out.setdefault(lh, set()).add(vec)
    out.setdefault(own, set()).add(literal_vector(plan))
    return out


def _fmt_vec(vec: tuple[float, ...]) -> str:
    return "(" + ", ".join(f"{v:g}" for v in vec) + ")"


def wf_lineage_check(
    plan: Plan,
    space: Mapping[str, tuple[float, float]],
    ranges: Mapping[str, tuple[float, float, int]],
    literals: Mapping[str, set[tuple[float, ...]]] | None = None,
) -> tuple[list[str], list[str]]:
    """``(problems, notes)`` for a walk-forward ``space`` (``{key: (low, high)}``) of ``plan``
    against the family's tried values ``ranges`` (:func:`tried_ranges`) and tried rule literals
    ``literals`` (:func:`tried_literals`; None skips that check). Problems refuse the
    walk-forward:

    * a searched key must span ``[min, max]`` of the tried values, clamped to the key's
      searchable bounds (a hand-tried ``stop_loss_pct`` below the 0.01 search floor cannot be
      searched, so the floor is required instead, and a note says so);
    * an unsearched key must never have been tried at another value than the plan's own;
    * the plan's logic must have been tried with ONE literal vector (its own), and no other
      logic in ``literals`` with more than one: a walk-forward cannot search rule literals, so
      a threshold hand-tuned across plans makes its result in-sample. :func:`tried_literals`
      applies the rule to the plan's own logic unconditionally and to another family logic only
      when one of its plans correlates >= ``cfg.research.literal_lineage_corr`` with this plan
      (a renamed, re-tuned or cosmetic copy), not to every logic that merely trends alike."""
    tunables = tunable_params(plan)
    own = {k: float(s.default) for k, s in tunables.items()}
    problems: list[str] = []
    notes: list[str] = []
    for key in sorted(ranges):
        lo_t, hi_t, n = ranges[key]
        spec = tunables.get(key)
        lo_req, hi_req = lo_t, hi_t
        if spec is not None:
            lo_req, hi_req = max(lo_t, float(spec.low)), min(hi_t, float(spec.high))
            if lo_req > hi_req:  # every tried value is outside the searchable bounds
                lo_req = hi_req = min(max(own[key], float(spec.low)), float(spec.high))
            if lo_req != lo_t or hi_req != hi_t:
                notes.append(
                    f"{key}: tried values [{lo_t:g}, {hi_t:g}] reach outside the searchable "
                    f"bounds [{float(spec.low):g}, {float(spec.high):g}]; the required coverage "
                    f"is clamped to [{lo_req:g}, {hi_req:g}]"
                )
        if key in space:
            lo, hi = float(space[key][0]), float(space[key][1])
            if lo > lo_req + _EPS or hi < hi_req - _EPS:
                problems.append(
                    f"{key} [{lo:g}, {hi:g}] does not cover [{lo_req:g}, {hi_req:g}], the values "
                    f"this behaviour family already tried ({n} values)"
                )
        elif key in own and (lo_req < own[key] - _EPS or hi_req > own[key] + _EPS):
            problems.append(
                f"{key} is not searched (fixed at {own[key]:g}) but this behaviour family "
                f"already tried it over [{lo_req:g}, {hi_req:g}]; search at least that range"
            )
    if literals is not None:
        own_lh = logic_hash(plan)
        own_vec = literal_vector(plan)
        tuned: list[tuple[float, ...]] = []
        for lh in sorted(literals, key=lambda h: (h != own_lh, h)):
            vecs = set(literals[lh]) | ({own_vec} if lh == own_lh else set())
            if len(vecs) > 1:
                tuned.extend(sorted(vecs))
        if tuned:
            shown = ", ".join(_fmt_vec(v) for v in tuned[:8]) + (", ..." if len(tuned) > 8 else "")
            problems.append(
                f"thresholds were hand-tuned across plans ({len(tuned)} literal vectors tried: "
                f"{shown}; this plan uses {_fmt_vec(own_vec)}); a walk-forward cannot search "
                "rule literals, so its result would not be out-of-sample — make the threshold a "
                "fixed hypothesis and test a new logic"
            )
    return problems, notes


def wf_lineage_problems(
    plan: Plan,
    space: Mapping[str, tuple[float, float]],
    ranges: Mapping[str, tuple[float, float, int]],
    literals: Mapping[str, set[tuple[float, ...]]] | None = None,
) -> list[str]:
    """The problems of :func:`wf_lineage_check` (empty when the walk-forward is allowed)."""
    return wf_lineage_check(plan, space, ranges, literals)[0]
