"""Plan JSON validation with fix-it error messages, canonical form, hashing, and tuning hooks.

Every problem is collected (not just the first) as a :class:`lab.types.PlanError` whose path
points at the offending key (``features.ema_fast.ema.period``, ``long_entry``,
``risk.stop_loss_pct``) and whose message says what is wrong, what is allowed, and how to fix
it, so research agents can repair their own plans. Pure: the only parsing is ``json.loads`` of
the string the caller passes in.

Derivatives features (``lab.registry.derivatives``: funding, premium, basis, open interest,
long/short ratio) are exposure or crash-risk filters, not direction signals. They are accepted
only as gating clauses of a spot long-only entry:

1. a derivatives alias may appear only in ``long_entry``, inside top-level ``and`` clauses that
   reference no price input or non-derivatives feature (``close > ema_slow and
   funding_zscore < 2`` is fine; ``close > ema_slow or funding_zscore < 2`` and
   ``close > funding_rate`` are not);
2. ``long_entry`` keeps at least one top-level ``and`` clause free of derivatives features, so a
   filter is never the only entry signal;
3. ``long_exit`` may not reference them: a missing derivatives value (an archive gap) makes the
   whole expression False at that bar, which would silently suppress the price exit;
4. the derivatives-free clauses must not all be statically always true (``high >= low``,
   ``close >= close``): the gate would then be the de-facto signal. This is the cheap static
   form; ``lab.runner.check_gate_base`` refuses data-dependent cases (``close > 0``,
   ``rsi < 101``) on the design window before any ledger row.

A gate fails CLOSED on a missing value: :mod:`lab.plan.expr` makes the whole ``long_entry``
False at any bar where an operand is NaN, through ``not`` and ``or`` too, so
``close > ema_slow and not (fz > 2)`` cannot enter on a bar without funding data.

Regime-model features (:data:`lab.plan.schema.HMM_FEATURE_NAMES`, PLAN.md Phase 20) follow the
same rules 1-4 for a different reason: a regime model is a filter, never a direction signal
(idea 13 killed the regime-as-signal family). A clause mixing them with derivatives features is
still a gate clause, and :func:`gate_base_expressions` strips both kinds. They are refused at
``features.<alias>`` on an interval below :data:`lab.plan.schema.MIN_HMM_INTERVAL_SECONDS`, in
edge and overlay plans alike.

An edge plan's regime gate must also be a level, so the entry cannot fire on a regime change:
``long_entry`` may not read a regime-model alias across bars through ``crosses_above``,
``crosses_below`` or ``prev``, nor through ``persist`` with k >= 2 under ``not``
(``b < 0.5 and not persist(b < 0.5, 2)`` is the same series as ``b < 0.5 and prev(b, 1) >= 0.5``).
A plain ``persist(gate, k)`` is a level: its windows nest, so it cannot mark a change without a
negation. Overlays keep their freedom in ``flat_when``, and this rule is not applied to
derivatives features.

Each ``persist(condition, k)`` call counts as one tunable parameter toward the plan's limit
(``MAX_FEATURE_PARAMS``, or ``MAX_OVERLAY_PARAMS`` for an overlay), beside the feature params.
``k`` is a rule literal, not an Optuna tunable: :func:`tunable_params` does not list it and
:func:`with_params` cannot set it. Its lineage goes through
:func:`lab.research.logic.literal_vector`, like a threshold.

Risk-overlay plans (``{"kind": "overlay", ...}``, PLAN.md Phase 15) are validated by the same
entry points (:func:`validate_plan`, :func:`parse_plan`, :func:`checked_expressions`,
:func:`with_params`), dispatched on ``kind``. An overlay is buy-and-hold of ONE symbol with one
risk rule, ``flat_when``: true at bar t's close -> flat from bar t+1's open, otherwise 100%
invested (exposure 0 or 1 only, so there is no ``risk`` key and no ``long_entry`` /
``long_exit``). Registry features only, 1 to ``MAX_OVERLAY_FEATURES`` (3) features with at most
``MAX_OVERLAY_PARAMS`` (3) params in total, defaults included; the unit (no price or volume
level) rules apply as for edge plans. The derivatives gating rules 1-4 above do NOT apply:
derivatives and regime-model features may appear anywhere in ``flat_when``, since gating
exposure is their stated role. Here a missing value fails OPEN to the base: a bar where
``flat_when`` is NaN is not flat (:func:`lab.plan.compile.compile_plan`), so a derivatives gap
means buy-and-hold, never a hidden exit. The parsed :class:`lab.types.Plan` has ``kind="overlay"``,
``long_exit = flat_when``, ``long_entry = "not (flat_when)"`` and the default risk; edge plans
and their canonical form (hence ``plan_hash``) are unchanged.
"""

from __future__ import annotations

import ast
import copy
import difflib
import hashlib
import json
import keyword
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from typing import Any

from lab.plan.expr import FUNCTIONS, MAX_EXPR_CHARS, CheckedExpr, ExprScope, check_expression
from lab.plan.schema import (
    ALIAS_PATTERN,
    HMM_FEATURE_NAMES,
    MAX_DESCRIPTION_CHARS,
    MAX_FEATURE_PARAMS,
    MAX_FEATURES,
    MAX_NAME_CHARS,
    MAX_OVERLAY_FEATURES,
    MAX_OVERLAY_PARAMS,
    MIN_FEATURES,
    MIN_HMM_INTERVAL_SECONDS,
    OVERLAY_KIND,
    OVERLAY_REQUIRED_KEYS,
    OVERLAY_TOP_LEVEL_KEYS,
    REQUIRED_KEYS,
    RESERVED_ALIASES,
    RISK_BOUNDS,
    RISK_KEYS,
    RISK_SEARCH_FLOOR,
    SHORT_KEYS,
    SYMBOL_PATTERN,
    TOP_LEVEL_KEYS,
)
from lab.plan.units import output_scale
from lab.registry.derivatives import DERIVATIVES_FEATURES, FILTER_NOTE
from lab.types import (
    INTERVAL_SECONDS,
    PRICE_INPUTS,
    FeatureRef,
    FeatureSpec,
    ParamSpec,
    Plan,
    PlanError,
    RiskSpec,
)

_SYMBOL_RE = re.compile(SYMBOL_PATTERN)
_ALIAS_RE = re.compile(ALIAS_PATTERN)


class PlanValidationError(ValueError):
    """Raised by :func:`parse_plan` / :func:`with_params`; ``str()`` lists every error."""

    def __init__(self, errors: list[PlanError]) -> None:
        self.errors: list[PlanError] = list(errors)
        super().__init__(format_errors(self.errors))


def format_errors(errors: list[PlanError]) -> str:
    """One line per error: ``- path: message``."""
    head = f"{len(errors)} plan error{'s' if len(errors) != 1 else ''}:"
    return "\n".join([head, *(f"- {e.path}: {e.message}" for e in errors)])


def resolve_registry(registry: Mapping[str, FeatureSpec] | None) -> Mapping[str, FeatureSpec]:
    if registry is not None:
        return registry
    from lab.registry import REGISTRY

    return REGISTRY


def _did_you_mean(word: str, options: list[str] | tuple[str, ...]) -> str:
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


@dataclass(frozen=True)
class _Validated:
    plan: Plan | None
    errors: list[PlanError]
    entry: CheckedExpr | None = None
    exit: CheckedExpr | None = None


# --------------------------------------------------------------------------- pieces


def _check_top_level(raw: Mapping[str, Any], errors: list[PlanError]) -> None:
    for key in raw:
        if key in TOP_LEVEL_KEYS:
            continue
        if not isinstance(key, str):
            errors.append(PlanError("$", f"keys must be strings, got {key!r}"))
        elif key == "kind":
            errors.append(
                PlanError(
                    "kind",
                    f"`kind` {raw[key]!r} is not a single-symbol plan kind. Omit `kind` for an "
                    'edge plan (long_entry / long_exit / risk), use "kind": "overlay" for a risk '
                    'overlay (flat_when on buy-and-hold of one symbol) or "kind": "portfolio" '
                    "for a portfolio plan",
                )
            )
        elif key in SHORT_KEYS:
            errors.append(
                PlanError(
                    key,
                    f"`{key}` is not supported: plans are long/flat only (Binance spot has no "
                    "shorts). Remove it and put the exit condition in `long_exit`",
                )
            )
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


_REQUIRED_HINTS: Mapping[str, str] = {
    "name": 'Add a short name, e.g. "name": "ema_trend_rsi_pullback"',
    "symbol": 'Add the trading pair, e.g. "symbol": "BTCUSDT"',
    "interval": f'Add the bar interval, one of {", ".join(INTERVAL_SECONDS)}; e.g. "4h"',
    "features": 'Add features, e.g. "features": {"ema_fast": {"ema": {"period": 20}}}',
    "long_entry": 'Add the entry condition, e.g. "long_entry": "ema_fast > ema_slow"',
    "long_exit": 'Add the exit condition, e.g. "long_exit": "ema_fast < ema_slow"',
}


def _check_name(raw: Mapping[str, Any], errors: list[PlanError]) -> str:
    name = raw.get("name")
    if "name" not in raw:
        return ""
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
                "description",
                f"is {len(desc)} characters; the limit is {MAX_DESCRIPTION_CHARS}",
            )
        )
    return desc


def _check_symbol(raw: Mapping[str, Any], errors: list[PlanError]) -> str:
    if "symbol" not in raw:
        return ""
    sym = raw["symbol"]
    if not isinstance(sym, str) or not _SYMBOL_RE.match(sym):
        fix = ""
        if isinstance(sym, str):
            cleaned = re.sub(r"[^A-Za-z0-9]", "", sym).upper()
            if _SYMBOL_RE.match(cleaned):
                fix = f' Use "{cleaned}".'
        errors.append(
            PlanError(
                "symbol",
                f'must be 2-20 uppercase letters/digits with no separator, like "BTCUSDT"; '
                f"got {sym!r}.{fix}",
            )
        )
        return ""
    return sym


def _check_interval(raw: Mapping[str, Any], errors: list[PlanError]) -> str:
    if "interval" not in raw:
        return ""
    iv = raw["interval"]
    if not isinstance(iv, str) or iv not in INTERVAL_SECONDS:
        hint = ""
        if isinstance(iv, str):
            hint = _did_you_mean(iv.lower(), tuple(INTERVAL_SECONDS)) if iv.lower() != iv else ""
            hint = hint or _did_you_mean(iv, tuple(INTERVAL_SECONDS))
        errors.append(
            PlanError(
                "interval",
                f"unknown interval {iv!r}; allowed: {', '.join(INTERVAL_SECONDS)}.{hint}",
            )
        )
        return ""
    return iv


def _check_params(
    alias: str,
    fname: str,
    spec: FeatureSpec,
    raw_params: object,
    errors: list[PlanError],
) -> dict[str, float] | None:
    base = f"features.{alias}.{fname}"
    if raw_params is None:
        raw_params = {}
    if not isinstance(raw_params, Mapping):
        errors.append(
            PlanError(
                base,
                f'params must be an object like {{"{spec.params[0].name}": '
                f"{_fmt(spec.params[0].default)}}} (or {{}} for defaults), got "
                f"{_type_name(raw_params)}"
                if spec.params
                else f"params must be an object ({{}} - `{fname}` has no params), got "
                f"{_type_name(raw_params)}",
            )
        )
        return None
    known = {p.name: p for p in spec.params}
    ok = True
    for pname in raw_params:
        if pname not in known:
            listing = ", ".join(known) if known else "none"
            errors.append(
                PlanError(
                    f"{base}.{pname}",
                    f"unknown param `{pname}` for feature `{fname}`."
                    f"{_did_you_mean(str(pname), tuple(known))} Its params: {listing}",
                )
            )
            ok = False
    filled: dict[str, float] = {}
    for p in spec.params:
        path = f"{base}.{p.name}"
        if p.name not in raw_params:
            filled[p.name] = int(p.default) if p.kind == "int" else float(p.default)
            continue
        v = raw_params[p.name]
        rng = f"[{_fmt(p.low)}, {_fmt(p.high)}]"
        kind = "whole number" if p.kind == "int" else "number"
        if not _is_number(v) or not math.isfinite(float(v)):
            errors.append(
                PlanError(
                    path,
                    f"must be a {kind} in {rng} ({p.description}), got {_type_name(v)} {v!r}; "
                    f"e.g. {_fmt(p.default)}",
                )
            )
            ok = False
            continue
        fv = float(v)
        if p.kind == "int" and not fv.is_integer():
            errors.append(
                PlanError(
                    path,
                    f"must be a whole number in {rng}, got {v!r}; use {round(fv)}",
                )
            )
            ok = False
            continue
        if not p.low <= fv <= p.high:
            clamp = min(max(fv, p.low), p.high)
            errors.append(
                PlanError(
                    path,
                    f"{_fmt(fv)} is out of range; `{p.name}` must be in {rng} "
                    f"({p.description}). Try {_fmt(clamp)}",
                )
            )
            ok = False
            continue
        filled[p.name] = int(fv) if p.kind == "int" else fv
    return filled if ok else None


def _check_features(
    raw: Mapping[str, Any],
    registry: Mapping[str, FeatureSpec],
    errors: list[PlanError],
    *,
    max_features: int = MAX_FEATURES,
    max_params: int = MAX_FEATURE_PARAMS,
    limit_note: str = "",
) -> tuple[list[FeatureRef], ExprScope, dict[str, int]]:
    """Feature refs, the expression scope, and alias -> param count. ``max_features`` /
    ``max_params`` default to the edge-plan limits; overlays pass their own and a ``limit_note``
    saying why."""
    refs: list[FeatureRef] = []
    single: set[str] = set(PRICE_INPUTS)
    multi: dict[str, tuple[str, ...]] = {}
    lenient: set[str] = set()
    scales: dict[str, tuple[int, int]] = {}
    names = sorted(registry)
    scope_reg = frozenset(names)
    if "features" not in raw:
        return refs, ExprScope(frozenset(single), multi, frozenset(), scope_reg), {}
    feats = raw["features"]
    example = '{"ema_fast": {"ema": {"period": 20}}}'
    if not isinstance(feats, Mapping):
        errors.append(
            PlanError(
                "features",
                f"must be an object mapping alias -> {{feature: {{params}}}}, e.g. {example}; "
                f"got {_type_name(feats)}",
            )
        )
        return refs, ExprScope(frozenset(single), multi, frozenset(), scope_reg), {}
    if not MIN_FEATURES <= len(feats) <= max_features:
        errors.append(
            PlanError(
                "features",
                f"has {len(feats)} features; use {MIN_FEATURES} to {max_features} "
                f"(fewer features means fewer chances to overfit{limit_note})"
                + ("; e.g. " + example if not feats else ". Drop the least important ones"),
            )
        )
    param_counts: dict[str, int] = {}
    for alias, value in feats.items():
        path = f"features.{alias}"
        alias_ok = True
        if not isinstance(alias, str) or not _ALIAS_RE.match(alias):
            suggestion = re.sub(r"[^a-z0-9_]", "_", str(alias).lower()).lstrip("_0123456789")[:31]
            fix = f' e.g. "{suggestion}"' if suggestion and _ALIAS_RE.match(suggestion) else ""
            errors.append(
                PlanError(
                    path,
                    f"alias {alias!r} must start with a lowercase letter and use only a-z, "
                    f"0-9 and _ (max 31 chars);{fix}",
                )
            )
            alias_ok = False
        elif alias in RESERVED_ALIASES or keyword.iskeyword(alias):
            what = (
                "a price input"
                if alias in PRICE_INPUTS
                else "a function name"
                if alias in FUNCTIONS
                else "a reserved word"
            )
            errors.append(
                PlanError(
                    path,
                    f"alias `{alias}` is {what} and cannot name a feature; rename it, "
                    f'e.g. "{alias}_f"',
                )
            )
            alias_ok = False
        alias_s = str(alias)
        spec: FeatureSpec | None = None
        if not isinstance(value, Mapping) or len(value) != 1:
            got = (
                f"keys {sorted(map(str, value))}"
                if isinstance(value, Mapping)
                else _type_name(value)
            )
            hint = ""
            if isinstance(value, Mapping):
                hint = (
                    f" To use `{alias_s}` with these params write "
                    f'"{alias_s}": {{"{alias_s}": {json.dumps(dict(value), default=str)}}}'
                    if alias_s in registry
                    else ""
                )
            errors.append(
                PlanError(
                    path,
                    "must be an object with exactly one key, the registry feature name, whose "
                    f'value is its params, e.g. "{alias_s}": {{"ema": {{"period": 20}}}}; got '
                    f"{got}.{hint}",
                )
            )
        else:
            ((fname, raw_params),) = value.items()
            spec = registry.get(fname) if isinstance(fname, str) else None
            if spec is None:
                param_names = {p.name for s in registry.values() for p in s.params}
                shape = ""
                if fname in param_names:
                    target = alias_s if alias_s in registry else "ema"
                    shape = (
                        f" `{fname}` looks like a param; write the registry feature name "
                        f'first, e.g. "{alias_s}": {{"{target}": '
                        f"{json.dumps({fname: raw_params}, default=str)}}}."
                    )
                errors.append(
                    PlanError(
                        f"{path}.{fname}",
                        f"unknown feature `{fname}`.{_did_you_mean(str(fname), names)}{shape} "
                        f"Registry features: {', '.join(names)}",
                    )
                )
            else:
                param_counts[alias_s] = len(spec.params)
                params = _check_params(alias_s, fname, spec, raw_params, errors)
                if params is not None and alias_ok:
                    refs.append(FeatureRef(alias_s, fname, params))
        if not alias_ok:
            lenient.add(alias_s)  # already reported; don't cascade "unknown name" errors
            continue
        if spec is None:
            lenient.add(alias_s)
        elif len(spec.outputs) == 1:
            single.add(alias_s)
            dim = output_scale(spec.name, spec.outputs[0])
            if dim is not None:
                scales[alias_s] = dim
        else:
            multi[alias_s] = tuple(spec.outputs)
            for out in spec.outputs:
                dim = output_scale(spec.name, out)
                if dim is not None:
                    scales[f"{alias_s}.{out}"] = dim
    total = sum(param_counts.values())
    if total > max_params:
        detail = ", ".join(f"{a}: {n}" for a, n in param_counts.items())
        errors.append(
            PlanError(
                "features",
                f"these features have {total} tunable params in total ({detail}), defaults "
                f"included; the limit is {max_params}{limit_note}. Drop a feature or pick ones "
                "with fewer params",
            )
        )
    scope = ExprScope(frozenset(single), multi, frozenset(lenient), scope_reg, scales)
    return refs, scope, param_counts


def _check_risk(raw: Mapping[str, Any], errors: list[PlanError]) -> RiskSpec:
    risk = raw.get("risk")
    if risk is None:
        return RiskSpec()
    if not isinstance(risk, Mapping):
        errors.append(
            PlanError(
                "risk",
                'must be an object, e.g. {"stop_loss_pct": 3, "position_size_pct": 100}; got '
                f"{_type_name(risk)}",
            )
        )
        return RiskSpec()
    for key in risk:
        if key not in RISK_KEYS:
            errors.append(
                PlanError(
                    f"risk.{key}",
                    f"unknown risk key `{key}`.{_did_you_mean(str(key), RISK_KEYS)} Allowed: "
                    f"{', '.join(RISK_KEYS)} (percent values, e.g. 3 means 3%)",
                )
            )
    values: dict[str, float | None] = {}
    for key in RISK_KEYS:
        low, high = RISK_BOUNDS[key]
        v = risk.get(key)
        nullable = key != "position_size_pct"
        if v is None:
            if key in risk and not nullable:
                errors.append(
                    PlanError(
                        f"risk.{key}",
                        f"must be a number in (0, {_fmt(high)}], got null; omit it for 100",
                    )
                )
            values[key] = None
            continue
        null_hint = " or null for none" if nullable else ""
        if not _is_number(v) or not math.isfinite(float(v)):
            errors.append(
                PlanError(
                    f"risk.{key}",
                    f"must be a number in (0, {_fmt(high)}] (percent){null_hint}, got "
                    f"{_type_name(v)} {v!r}",
                )
            )
            values[key] = None
            continue
        fv = float(v)
        if not low < fv <= high:
            fix = (
                f"use a value above 0 and at most {_fmt(high)}"
                + (", or null to disable it" if nullable else "")
                + ". Values are percent: 3 means 3%"
            )
            errors.append(
                PlanError(
                    f"risk.{key}",
                    f"{_fmt(fv)} is out of range (0, {_fmt(high)}]; {fix}",
                )
            )
            values[key] = None
            continue
        values[key] = fv
    size = values["position_size_pct"]
    return RiskSpec(
        position_size_pct=100.0 if size is None else size,
        stop_loss_pct=values["stop_loss_pct"],
        take_profit_pct=values["take_profit_pct"],
    )


def _is_persist(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "persist"
    )


def is_unit_persist(node: ast.AST) -> bool:
    """``persist(c, 1)``: the same rule as ``c``."""
    return (
        isinstance(node, ast.Call)
        and _is_persist(node)
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == 1
    )


def _persist_calls(node: ast.AST) -> list[ast.Call]:
    """Every ``persist(...)`` call inside ``node``, nested ones included. Walks by hand: this runs
    on every plan and ``ast.walk`` imports at call time, which validation must never do."""
    found: list[ast.Call] = []
    stack: list[ast.AST] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.Call) and _is_persist(cur):
            found.append(cur)
        stack.extend(ast.iter_child_nodes(cur))
    return found


def _check_persist_budget(
    rules: Mapping[str, tuple[str, ast.expr] | None],
    param_counts: Mapping[str, int],
    max_params: int,
    limit_note: str,
    errors: list[PlanError],
) -> None:
    """Feature params plus ``persist`` calls must fit the plan's tunable limit. ``rules`` maps a
    rule key to its :func:`_parsed` result, so an invalid rule's calls still count. Plans with no
    ``persist`` add nothing here: :func:`_check_features` has already judged their params."""
    calls = {key: len(_persist_calls(rule[1])) for key, rule in rules.items() if rule is not None}
    n_calls = sum(calls.values())
    params = sum(param_counts.values())
    if n_calls == 0 or params + n_calls <= max_params:
        return
    detail = ", ".join(f"{a}: {n}" for a, n in param_counts.items()) or "none"
    where = ", ".join(f"{key}: {n}" for key, n in calls.items() if n)
    errors.append(
        PlanError(
            "features",
            f"this plan has {params + n_calls} tunable parameters: {params} feature params "
            f"({detail}), defaults included, plus {n_calls} `persist` call(s) ({where}), each "
            f"of which counts as one; the limit is {max_params}{limit_note}. Remove a `persist` "
            "or drop a feature",
        )
    )


# --------------------------------------------------------------------------- gate features

REGIME_RULE = "a regime model is a filter, never a direction signal"

# Example thresholds for error messages only (each feature's natural scale), not suggestions.
_GATE_EXAMPLES: Mapping[str, str] = {
    "funding_rate": "< 0.0003",
    "funding_zscore": "< 2",
    "premium_index": "< 0.001",
    "basis_pct": "< 0.1",
    "oi_change": "< 10",
    "long_short_ratio": "< 2",
    "hmm_state": "> 0",
    "hmm_bear_prob": "< 0.5",
}


def _aliases_of(
    raw: Mapping[str, Any], registry: Mapping[str, FeatureSpec], features: Collection[str]
) -> dict[str, str]:
    """alias -> feature name for every plan feature named in ``features``."""
    feats = raw.get("features")
    if not isinstance(feats, Mapping):
        return {}
    out: dict[str, str] = {}
    for alias, value in feats.items():
        if not isinstance(alias, str) or not isinstance(value, Mapping) or len(value) != 1:
            continue
        ((fname, _params),) = value.items()
        if isinstance(fname, str) and fname in features and fname in registry:
            out[alias] = fname
    return out


def _check_hmm_interval(interval: str, hmm: Mapping[str, str], errors: list[PlanError]) -> None:
    """Refuse regime-model features on bars shorter than the model's minimum."""
    seconds = INTERVAL_SECONDS.get(interval)
    if seconds is None or seconds >= MIN_HMM_INTERVAL_SECONDS:
        return
    minimum = f"{MIN_HMM_INTERVAL_SECONDS // 3_600}h"
    for alias, fname in hmm.items():
        errors.append(
            PlanError(
                f"features.{alias}",
                f"`{fname}` needs bars of {minimum} or longer, got interval {interval!r}: the "
                f"regime model is not run below {minimum}. Use an interval of at least "
                f"{minimum}, e.g. 4h, or remove this feature",
            )
        )


def _conjuncts(node: ast.expr) -> list[ast.expr]:
    """Top-level ``and`` clauses of ``node`` (nested ``and`` groups flattened)."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        out: list[ast.expr] = []
        for value in node.values:
            out.extend(_conjuncts(value))
        return out
    return [node]


# Each of these compares a value with its own past, which turns a level into a change detector.
_CHANGE_CALLS: tuple[str, ...] = ("crosses_above", "crosses_below", "prev")


def _regime_change_reads(
    node: ast.AST, hmm: Mapping[str, str], negated: bool = False
) -> list[ast.Call]:
    """Outermost calls under ``node`` that read a regime-model alias across bars, source order:
    a :data:`_CHANGE_CALLS` call, or a ``persist`` with k >= 2 under an odd number of ``not``."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        negated = not negated
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and _names(node) & hmm.keys():
        held_negated = negated and _is_persist(node) and not is_unit_persist(node)
        if node.func.id in _CHANGE_CALLS or held_negated:
            return [node]
    return [
        call
        for child in ast.iter_child_nodes(node)
        for call in _regime_change_reads(child, hmm, negated)
    ]


def _names(node: ast.AST) -> set[str]:
    """Every variable name ``node`` reads (``macd.hist`` counts as ``macd``), not function
    names."""
    called = {id(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and id(n) not in called}


def _listing(aliases: set[str], drv: Mapping[str, str]) -> str:
    return ", ".join(
        f"`{a}`" if drv.get(a, a) == a else f"`{a}` ({drv[a]})" for a in sorted(aliases)
    )


def _gate_example(alias: str, drv: Mapping[str, str]) -> str:
    return f"`close > ema_slow and {alias} {_GATE_EXAMPLES.get(drv[alias], '< 2')}`"


def _parsed(checked: CheckedExpr | None, source: object) -> tuple[str, ast.expr] | None:
    """``(text, body)`` of a rule: the checked tree, or (when the rule has other errors, such
    as a unit mismatch) a plain parse of its source so the gating rules are still reported.
    None when it does not parse."""
    if checked is not None:
        return checked.source, checked.tree.body
    if not isinstance(source, str) or len(source) > MAX_EXPR_CHARS:
        return None
    text = source.strip()
    try:
        return text, ast.parse(text, mode="eval").body
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _check_gates(
    entry: tuple[str, ast.expr] | None,
    exit_: tuple[str, ast.expr] | None,
    drv: Mapping[str, str],
    hmm: Mapping[str, str],
    errors: list[PlanError],
) -> None:
    """Rules 1-3 of the module docstring: gate features (derivatives, regime model) only gate a
    long entry. Each family's errors state that family's own rule.

    ``entry`` / ``exit_`` are ``(source text, parsed body)`` from :func:`_parsed`."""
    gates = {**drv, **hmm}
    if not gates:
        return
    if exit_ is not None:
        used = _names(exit_[1]) & gates.keys()
        used_drv, used_hmm = used & drv.keys(), used & hmm.keys()
        if used_drv:
            errors.append(
                PlanError(
                    "long_exit",
                    f"uses derivatives feature(s) {_listing(used_drv, gates)}; each is an "
                    f"{FILTER_NOTE}, allowed only as a gating `and` clause of long_entry. In "
                    "long_exit a missing derivatives value (an archive gap or a bar before its "
                    "first record) makes the whole expression False at that bar, which would "
                    "suppress the price exit and keep the position open. Write long_exit with "
                    "price inputs and non-derivatives features only, and gate the entry instead, "
                    f"e.g. {_gate_example(min(used_drv), gates)}",
                )
            )
        if used_hmm:
            errors.append(
                PlanError(
                    "long_exit",
                    f"uses regime-model feature(s) {_listing(used_hmm, gates)}; {REGIME_RULE}, "
                    "so it may gate exposure only: an overlay's `flat_when`, or a gating `and` "
                    "clause of long_entry, never long_exit. Write long_exit with price inputs and "
                    f"non-gate features only, and gate the entry instead, e.g. "
                    f"{_gate_example(min(used_hmm), gates)}",
                )
            )
    if entry is None:
        return
    signal_clause = False
    mixed = False
    source, body = entry
    for clause in _conjuncts(body):
        names = _names(clause)
        used = names & gates.keys()
        if not used:
            signal_clause = True
            continue
        others = names - gates.keys()
        if not others:
            continue  # a pure gate clause: allowed, whichever gate families it mixes
        mixed = True
        text = ast.get_source_segment(source, clause) or source
        used_drv, used_hmm = used & drv.keys(), used & hmm.keys()
        if used_drv:
            errors.append(
                PlanError(
                    "long_entry",
                    f"`{text}` combines derivatives feature(s) {_listing(used_drv, gates)} with "
                    f"{_listing(others, gates)} in one clause; a derivatives feature is an "
                    f"{FILTER_NOTE}, so it may appear only in its own top-level `and` clause, "
                    "compared with numbers or other derivatives features (not inside an `or`, "
                    "and not compared with or computed from prices or other features). Split "
                    f"it, e.g. {_gate_example(min(used_drv), gates)}",
                )
            )
        if used_hmm:
            errors.append(
                PlanError(
                    "long_entry",
                    f"`{text}` combines regime-model feature(s) {_listing(used_hmm, gates)} with "
                    f"{_listing(others, gates)} in one clause; {REGIME_RULE}, so it may appear "
                    "only in its own top-level `and` clause, compared with numbers or other gate "
                    "features (not inside an `or`, and not compared with or computed from prices "
                    f"or other features). Split it, e.g. {_gate_example(min(used_hmm), gates)}",
                )
            )
    if signal_clause and not mixed:
        trivial = _constant_true_clauses(source, body, gates)
        if trivial is not None:
            used = _names(body) & gates.keys()
            clauses = ", ".join(f"`{t}`" for t in trivial)
            used_drv, used_hmm = used & drv.keys(), used & hmm.keys()
            if used_drv:
                errors.append(
                    PlanError(
                        "long_entry",
                        f"its price clause(s) {clauses} are true on "
                        "every bar, so the derivatives gate(s) "
                        f"{_listing(used_drv, gates)} would be the de-facto entry signal; each "
                        f"is an {FILTER_NOTE}. Replace the always-true clause with a real price "
                        f"signal that selects bars on its own, e.g. "
                        f"{_gate_example(min(used_drv), gates)}",
                    )
                )
            if used_hmm:
                errors.append(
                    PlanError(
                        "long_entry",
                        f"its price clause(s) {clauses} are true on every bar, so the "
                        f"regime-model gate(s) {_listing(used_hmm, gates)} would be the de-facto "
                        f"entry signal; {REGIME_RULE}. Replace the always-true clause with a "
                        "real price signal that selects bars on its own, e.g. "
                        f"{_gate_example(min(used_hmm), gates)}",
                    )
                )
    if not signal_clause and not mixed:
        used = _names(body) & gates.keys()
        used_drv, used_hmm = used & drv.keys(), used & hmm.keys()
        if used_drv:
            errors.append(
                PlanError(
                    "long_entry",
                    f"uses only derivatives feature(s) {_listing(used_drv, gates)}; each is an "
                    f"{FILTER_NOTE}, so it cannot be the entry signal on its own. Add a "
                    "top-level `and` clause built from price inputs or non-derivatives features, "
                    f"e.g. {_gate_example(min(used_drv), gates)}",
                )
            )
        if used_hmm:
            errors.append(
                PlanError(
                    "long_entry",
                    f"uses only regime-model feature(s) {_listing(used_hmm, gates)}; "
                    f"{REGIME_RULE}, so it cannot be the entry signal on its own. Add a "
                    "top-level `and` clause built from price inputs or non-gate features, e.g. "
                    f"{_gate_example(min(used_hmm), gates)}",
                )
            )
    for call in _regime_change_reads(body, hmm):
        text = ast.get_source_segment(source, call) or ast.unparse(call)
        where = " under `not`" if _is_persist(call) else ""
        read = _names(call) & hmm.keys()
        errors.append(
            PlanError(
                "long_entry",
                f"`{text}`{where} reads regime-model feature(s) {_listing(read, gates)} across "
                f"bars, so the entry would fire on a regime change; {REGIME_RULE}, so a gate must "
                "be a level. Compare the current value, or require that it held with a plain "
                f"`persist(condition, k)`, e.g. {_gate_example(min(read), gates)}",
            )
        )


# Candle identities: ``high`` is at least every other price input and ``low`` at most, on every
# bar (lab.types.validate_candles), so e.g. ``high >= low`` or ``close <= high`` is always true.
_CANDLE_PRICES = frozenset({"open", "high", "low", "close"})


def _always_true_compare(node: ast.expr) -> bool:
    """A single ``a >= b`` / ``a <= b`` that is true on every bar where it is defined: the same
    operand on both sides (``close >= close``), or a candle identity (``high >= low``,
    ``close <= high``, ``low <= open``), also when wrapped in ``persist``, which only asks that
    the condition keep holding. Static and cheap; the runner's data check
    (``lab.runner.check_gate_base``) is the real guard."""
    if isinstance(node, ast.Call) and _is_persist(node) and node.args:
        return all(_always_true_compare(c) for c in _conjuncts(node.args[0]))
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    op, left, right = node.ops[0], node.left, node.comparators[0]
    if isinstance(op, ast.LtE):
        left, right = right, left
    elif not isinstance(op, ast.GtE):
        return False
    if ast.dump(left) == ast.dump(right):
        return True
    if isinstance(left, ast.Name) and isinstance(right, ast.Name):
        big, small = left.id, right.id
        return {big, small} <= _CANDLE_PRICES and (big == "high" or small == "low")
    return False


def _constant_true_clauses(
    source: str, body: ast.expr, gates: Mapping[str, str]
) -> list[str] | None:
    """The text of every gate-free top-level ``and`` clause of ``long_entry`` when ALL of them
    are statically always true (so the gate is the only selector); None otherwise."""
    free = [c for c in _conjuncts(body) if not (_names(c) & gates.keys())]
    if not free or not all(_always_true_compare(c) for c in free):
        return None
    return [ast.get_source_segment(source, c) or ast.unparse(c) for c in free]


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


def _validate(raw: Mapping[str, Any] | str, registry: Mapping[str, FeatureSpec]) -> _Validated:
    obj, errors = _parse_raw(raw)
    if obj is None:
        return _Validated(None, errors)
    if obj.get("kind") == OVERLAY_KIND:
        return _validate_overlay(obj, registry)
    _check_top_level(obj, errors)
    name = _check_name(obj, errors)
    description = _check_description(obj, errors)
    symbol = _check_symbol(obj, errors)
    interval = _check_interval(obj, errors)
    refs, scope, param_counts = _check_features(obj, registry, errors)
    hmm = _aliases_of(obj, registry, HMM_FEATURE_NAMES)
    _check_hmm_interval(interval, hmm, errors)
    entry: CheckedExpr | None = None
    exit_: CheckedExpr | None = None
    if "long_entry" in obj:
        entry, errs = check_expression(obj["long_entry"], scope, "long_entry")
        errors.extend(errs)
    if "long_exit" in obj:
        exit_, errs = check_expression(obj["long_exit"], scope, "long_exit")
        errors.extend(errs)
    parsed_entry = _parsed(entry, obj.get("long_entry"))
    parsed_exit = _parsed(exit_, obj.get("long_exit"))
    _check_persist_budget(
        {"long_entry": parsed_entry, "long_exit": parsed_exit},
        param_counts,
        MAX_FEATURE_PARAMS,
        "",
        errors,
    )
    _check_gates(
        parsed_entry,
        parsed_exit,
        _aliases_of(obj, registry, DERIVATIVES_FEATURES),
        hmm,
        errors,
    )
    risk = _check_risk(obj, errors)
    if entry is not None and exit_ is not None:
        used = {r.split(".", 1)[0] for r in entry.refs | exit_.refs}
        for ref in refs:
            if ref.alias not in used:
                errors.append(
                    PlanError(
                        f"features.{ref.alias}",
                        f"feature `{ref.alias}` is defined but not used in long_entry or "
                        "long_exit; use it in a rule or remove it (unused features still count "
                        "toward the param limit)",
                    )
                )
    if errors:
        return _Validated(None, errors)
    assert entry is not None and exit_ is not None
    plan = Plan(
        name=name,
        symbol=symbol,
        interval=interval,
        features=tuple(refs),
        long_entry=entry.source,
        long_exit=exit_.source,
        risk=risk,
        description=description,
    )
    return _Validated(plan, [], entry, exit_)


# --------------------------------------------------------------------------- overlay plans

_OVERLAY_LIMIT_NOTE = "; overlays use published parameters, untuned"
_OVERLAY_EXAMPLE = '"flat_when": "close < sma_slow"'
_OVERLAY_REQUIRED_HINTS: Mapping[str, str] = {
    "kind": 'Add "kind": "overlay"',
    "name": 'Add a short name, e.g. "name": "sma200_trend_overlay"',
    "symbol": 'Add the one symbol held, e.g. "symbol": "BTCUSDT"',
    "interval": _REQUIRED_HINTS["interval"],
    "features": 'Add features, e.g. "features": {"sma_slow": {"sma": {"period": 200}}}',
    "flat_when": f"Add the risk rule (true -> flat from the next bar's open), e.g. "
    f"{_OVERLAY_EXAMPLE}",
}


def _check_overlay_top_level(raw: Mapping[str, Any], errors: list[PlanError]) -> None:
    for key in raw:
        if key in OVERLAY_TOP_LEVEL_KEYS:
            continue
        if not isinstance(key, str):
            errors.append(PlanError("$", f"keys must be strings, got {key!r}"))
        elif key == "risk":
            errors.append(
                PlanError(
                    "risk",
                    "an overlay has no `risk` key: it holds 100% of equity in the symbol or is "
                    "flat (exposure 0 or 1 only; half exposure or stops would need an engine "
                    "change and are out of scope). Remove it",
                )
            )
        elif key in ("long_entry", "long_exit"):
            errors.append(
                PlanError(
                    key,
                    f"an overlay has no `{key}`: write its one risk rule as `flat_when` (true at "
                    "bar t's close -> flat from bar t+1's open, otherwise invested in "
                    f"buy-and-hold), e.g. {_OVERLAY_EXAMPLE}. Remove `{key}`",
                )
            )
        elif key in SHORT_KEYS:
            errors.append(
                PlanError(
                    key,
                    f"`{key}` is not supported: an overlay is long buy-and-hold or flat "
                    "(Binance spot has no shorts). Remove it",
                )
            )
        elif key in ("symbols", "universe"):
            errors.append(
                PlanError(
                    key,
                    "an overlay covers exactly one symbol: use one string, e.g. "
                    '"symbol": "BTCUSDT", and submit one overlay per symbol',
                )
            )
        else:
            errors.append(
                PlanError(
                    key,
                    f"unknown key `{key}`.{_did_you_mean(key, OVERLAY_TOP_LEVEL_KEYS)} Allowed "
                    f"overlay keys: {', '.join(OVERLAY_TOP_LEVEL_KEYS)}",
                )
            )
    for key in OVERLAY_REQUIRED_KEYS:
        if key not in raw:
            errors.append(
                PlanError(key, f"missing required key `{key}`. {_OVERLAY_REQUIRED_HINTS[key]}")
            )


def _negated(expr: CheckedExpr) -> CheckedExpr:
    """``not (expr)`` built from the already-checked tree (no re-parse, no length limit)."""
    body = ast.UnaryOp(op=ast.Not(), operand=copy.deepcopy(expr.tree.body))
    return CheckedExpr(
        source=f"not ({expr.source})",
        tree=ast.fix_missing_locations(ast.Expression(body=body)),
        lag=expr.lag,
        max_prev=expr.max_prev,
        uses_cross=expr.uses_cross,
        refs=expr.refs,
        persist_ks=expr.persist_ks,
    )


def _validate_overlay(raw: Mapping[str, Any], registry: Mapping[str, FeatureSpec]) -> _Validated:
    errors: list[PlanError] = []
    _check_overlay_top_level(raw, errors)
    name = _check_name(raw, errors)
    description = _check_description(raw, errors)
    symbol = ""
    if isinstance(raw.get("symbol"), list | tuple):
        errors.append(
            PlanError(
                "symbol",
                "an overlay covers exactly one symbol: use one string, e.g. "
                '"symbol": "BTCUSDT", and submit one overlay per symbol',
            )
        )
    else:
        symbol = _check_symbol(raw, errors)
    interval = _check_interval(raw, errors)
    refs, scope, param_counts = _check_features(
        raw,
        registry,
        errors,
        max_features=MAX_OVERLAY_FEATURES,
        max_params=MAX_OVERLAY_PARAMS,
        limit_note=_OVERLAY_LIMIT_NOTE,
    )
    _check_hmm_interval(interval, _aliases_of(raw, registry, HMM_FEATURE_NAMES), errors)
    flat: CheckedExpr | None = None
    if "flat_when" in raw:
        flat, errs = check_expression(raw["flat_when"], scope, "flat_when")
        errors.extend(errs)
    _check_persist_budget(
        {"flat_when": _parsed(flat, raw.get("flat_when"))},
        param_counts,
        MAX_OVERLAY_PARAMS,
        _OVERLAY_LIMIT_NOTE,
        errors,
    )
    if flat is not None:
        used = {r.split(".", 1)[0] for r in flat.refs}
        for ref in refs:
            if ref.alias not in used:
                errors.append(
                    PlanError(
                        f"features.{ref.alias}",
                        f"feature `{ref.alias}` is defined but not used in flat_when; use it "
                        "in the rule or remove it (unused features still count toward the "
                        "param limit)",
                    )
                )
    if errors:
        return _Validated(None, errors)
    assert flat is not None
    entry = _negated(flat)
    plan = Plan(
        name=name,
        symbol=symbol,
        interval=interval,
        features=tuple(refs),
        long_entry=entry.source,
        long_exit=flat.source,
        risk=RiskSpec(),
        description=description,
        kind="overlay",
    )
    return _Validated(plan, [], entry, flat)


def is_overlay_plan(raw: object) -> bool:
    """True for a risk-overlay plan: a :class:`lab.types.Plan` with ``kind == "overlay"``, or a
    mapping (or JSON object text) with ``"kind": "overlay"``. Never raises."""
    if isinstance(raw, Plan):
        return raw.kind == OVERLAY_KIND
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return False
    return isinstance(raw, Mapping) and raw.get("kind") == OVERLAY_KIND


def validate_plan(
    raw: Mapping[str, Any] | str, registry: Mapping[str, FeatureSpec] | None = None
) -> tuple[Plan | None, list[PlanError]]:
    """Validate a plan dict or JSON text. Returns ``(plan, [])`` or ``(None, errors)``."""
    result = _validate(raw, resolve_registry(registry))
    return result.plan, result.errors


def parse_plan(
    raw: Mapping[str, Any] | str, registry: Mapping[str, FeatureSpec] | None = None
) -> Plan:
    """Like :func:`validate_plan` but raises :class:`PlanValidationError` listing every error."""
    plan, errors = validate_plan(raw, registry)
    if plan is None:
        raise PlanValidationError(errors)
    return plan


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    """Canonical JSON-able dict (params filled); ``parse_plan(plan_to_dict(p)) == p``.

    Edge (``kind="signal"``) plans keep their original form with no ``kind`` key, so their
    ``plan_hash`` is unchanged; an overlay is ``{"kind", "name", "description", "symbol",
    "interval", "features", "flat_when"}``."""
    if plan.kind == OVERLAY_KIND:
        return {
            "kind": OVERLAY_KIND,
            "name": plan.name,
            "description": plan.description,
            "symbol": plan.symbol,
            "interval": plan.interval,
            "features": {f.alias: {f.feature: dict(f.params)} for f in plan.features},
            "flat_when": plan.long_exit,
        }
    return {
        "name": plan.name,
        "description": plan.description,
        "symbol": plan.symbol,
        "interval": plan.interval,
        "features": {f.alias: {f.feature: dict(f.params)} for f in plan.features},
        "long_entry": plan.long_entry,
        "long_exit": plan.long_exit,
        "risk": {
            "position_size_pct": plan.risk.position_size_pct,
            "stop_loss_pct": plan.risk.stop_loss_pct,
            "take_profit_pct": plan.risk.take_profit_pct,
        },
    }


def plan_hash(plan: Plan) -> str:
    """sha256 hex of the canonical plan JSON (sorted keys, no whitespace)."""
    text = json.dumps(plan_to_dict(plan), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def checked_expressions(
    plan: Plan, registry: Mapping[str, FeatureSpec] | None = None
) -> tuple[CheckedExpr, CheckedExpr]:
    """Re-validate ``plan`` and return its checked (entry, exit) expressions."""
    result = _validate(plan_to_dict(plan), resolve_registry(registry))
    if result.entry is None or result.exit is None:
        raise PlanValidationError(result.errors)
    return result.entry, result.exit


def gate_base_expressions(
    plan: Plan, registry: Mapping[str, FeatureSpec] | None = None
) -> tuple[CheckedExpr, CheckedExpr] | None:
    """``(base, not base)`` for a plan with gate features: ``base`` is the conjunction of the
    top-level ``and`` clauses of ``long_entry`` that read NO derivatives or regime-model feature
    (the price signal on its own), built from the already-checked entry tree (no new parse, no
    length limit). Evaluate both with :func:`lab.plan.expr.evaluate_expression`: a bar where
    ``base`` is defined is True in exactly one of them (NaN makes both False). None when the plan
    uses no gate feature or has no gate-free clause, and always None for a risk overlay (its
    ``flat_when`` may use gate features freely; there is no gate to strip)."""
    if plan.kind == OVERLAY_KIND:
        return None
    reg = resolve_registry(registry)
    gates = {
        ref.alias: ref.feature
        for ref in plan.features
        if (ref.feature in DERIVATIVES_FEATURES or ref.feature in HMM_FEATURE_NAMES)
        and ref.feature in reg
    }
    if not gates:
        return None
    entry, _ = checked_expressions(plan, reg)
    clauses = [c for c in _conjuncts(entry.tree.body) if not (_names(c) & gates.keys())]
    if not clauses:
        return None
    texts = [ast.get_source_segment(entry.source, c) or ast.unparse(c) for c in clauses]
    if len(clauses) == 1:
        body: ast.expr = clauses[0]
        source = texts[0]
    else:
        body = ast.BoolOp(op=ast.And(), values=clauses)
        source = " and ".join(f"({t})" for t in texts)
    common = {
        "lag": entry.lag,
        "max_prev": entry.max_prev,
        "uses_cross": entry.uses_cross,
        "refs": entry.refs,
        "persist_ks": tuple(
            ast.literal_eval(call.args[1]) for clause in clauses for call in _persist_calls(clause)
        ),
    }
    base = CheckedExpr(source=source, tree=ast.Expression(body=body), **common)
    negated = CheckedExpr(
        source=f"not ({source})",
        tree=ast.Expression(body=ast.UnaryOp(op=ast.Not(), operand=body)),
        **common,
    )
    return base, negated


def tunable_params(
    plan: Plan, registry: Mapping[str, FeatureSpec] | None = None
) -> dict[str, ParamSpec]:
    """Every feature param as ``"alias.param"`` plus ``risk.stop_loss_pct`` /
    ``risk.take_profit_pct`` when set. ``default`` holds the plan's current value; ``low``/
    ``high`` are the validation bounds (risk lows use a 0.01 floor since 0 is excluded)."""
    reg = resolve_registry(registry)
    out: dict[str, ParamSpec] = {}
    for ref in plan.features:
        spec = reg.get(ref.feature)
        if spec is None:
            raise PlanValidationError(
                [PlanError(f"features.{ref.alias}.{ref.feature}", "unknown feature")]
            )
        for p in spec.params:
            out[f"{ref.alias}.{p.name}"] = replace(
                p, default=float(ref.params.get(p.name, p.default))
            )
    for key, desc in (
        ("stop_loss_pct", "stop-loss distance below entry, percent"),
        ("take_profit_pct", "take-profit distance above entry, percent"),
    ):
        value = getattr(plan.risk, key)
        if value is not None:
            out[f"risk.{key}"] = ParamSpec(
                name=key,
                kind="float",
                default=float(value),
                low=RISK_SEARCH_FLOOR,
                high=RISK_BOUNDS[key][1],
                description=desc,
            )
    return out


def with_params(
    plan: Plan,
    overrides: Mapping[str, float],
    registry: Mapping[str, FeatureSpec] | None = None,
) -> Plan:
    """New validated plan with ``overrides`` (keys from :func:`tunable_params`) applied.

    Int params are rounded to the nearest whole number. Unknown keys or out-of-range values
    raise :class:`PlanValidationError`.
    """
    reg = resolve_registry(registry)
    tunables = tunable_params(plan, reg)
    data = plan_to_dict(plan)
    errors: list[PlanError] = []
    by_alias = {f.alias: f for f in plan.features}
    for key, value in overrides.items():
        if key not in tunables:
            hint = _did_you_mean(key, tuple(tunables))
            unset = (
                f" `{key}` is not set in this plan; set it under `risk` first."
                if key in {"risk.stop_loss_pct", "risk.take_profit_pct"}
                else ""
            )
            errors.append(
                PlanError(
                    key,
                    f"unknown tunable param `{key}`.{hint}{unset} Tunable: {', '.join(tunables)}",
                )
            )
            continue
        v: object = value
        if _is_number(value) and math.isfinite(float(value)) and tunables[key].kind == "int":
            v = round(float(value))
        prefix, pname = key.split(".", 1)
        if prefix == "risk":
            data["risk"][pname] = v
        else:
            ref = by_alias[prefix]
            data["features"][prefix][ref.feature][pname] = v
    if errors:
        raise PlanValidationError(errors)
    result = _validate(data, reg)
    if result.plan is None:
        raise PlanValidationError(result.errors)
    return result.plan
