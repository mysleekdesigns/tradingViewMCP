"""Plan JSON validation with fix-it error messages, canonical form, hashing, and tuning hooks.

Every problem is collected (not just the first) as a :class:`lab.types.PlanError` whose path
points at the offending key (``features.ema_fast.ema.period``, ``long_entry``,
``risk.stop_loss_pct``) and whose message says what is wrong, what is allowed, and how to fix
it, so research agents can repair their own plans. Pure: the only parsing is ``json.loads`` of
the string the caller passes in.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import keyword
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from lab.plan.expr import FUNCTIONS, CheckedExpr, ExprScope, check_expression
from lab.plan.schema import (
    ALIAS_PATTERN,
    MAX_DESCRIPTION_CHARS,
    MAX_FEATURE_PARAMS,
    MAX_FEATURES,
    MAX_NAME_CHARS,
    MIN_FEATURES,
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
) -> tuple[list[FeatureRef], ExprScope]:
    refs: list[FeatureRef] = []
    single: set[str] = set(PRICE_INPUTS)
    multi: dict[str, tuple[str, ...]] = {}
    lenient: set[str] = set()
    scales: dict[str, tuple[int, int]] = {}
    names = sorted(registry)
    scope_reg = frozenset(names)
    if "features" not in raw:
        return refs, ExprScope(frozenset(single), multi, frozenset(), scope_reg)
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
        return refs, ExprScope(frozenset(single), multi, frozenset(), scope_reg)
    if not MIN_FEATURES <= len(feats) <= MAX_FEATURES:
        errors.append(
            PlanError(
                "features",
                f"has {len(feats)} features; use {MIN_FEATURES} to {MAX_FEATURES} "
                "(fewer features means fewer chances to overfit)"
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
    if total > MAX_FEATURE_PARAMS:
        detail = ", ".join(f"{a}: {n}" for a, n in param_counts.items())
        errors.append(
            PlanError(
                "features",
                f"these features have {total} tunable params in total ({detail}), defaults "
                f"included; the limit is {MAX_FEATURE_PARAMS}. Drop a feature or pick ones "
                "with fewer params",
            )
        )
    return refs, ExprScope(frozenset(single), multi, frozenset(lenient), scope_reg, scales)


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
    _check_top_level(obj, errors)
    name = _check_name(obj, errors)
    description = _check_description(obj, errors)
    symbol = _check_symbol(obj, errors)
    interval = _check_interval(obj, errors)
    refs, scope = _check_features(obj, registry, errors)
    entry: CheckedExpr | None = None
    exit_: CheckedExpr | None = None
    if "long_entry" in obj:
        entry, errs = check_expression(obj["long_entry"], scope, "long_entry")
        errors.extend(errs)
    if "long_exit" in obj:
        exit_, errs = check_expression(obj["long_exit"], scope, "long_exit")
        errors.extend(errs)
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
    """Canonical JSON-able dict (params filled); ``parse_plan(plan_to_dict(p)) == p``."""
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
