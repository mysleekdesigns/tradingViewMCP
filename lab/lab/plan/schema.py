"""Plan JSON shape: keys, limits, and a JSON Schema document for agents and the MCP server.

The authoritative checks live in :mod:`lab.plan.validator` (they give fix-it messages); this
module holds the limits they share and renders them as JSON Schema for tools that want one.
Limits follow ``.claude/rules/research-agents.md`` (max ~4 features, ~6 tunable parameters).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lab.plan.expr import FUNCTIONS, MAX_EXPR_CHARS, MAX_PREV_BARS
from lab.types import INTERVAL_SECONDS, PRICE_INPUTS, FeatureSpec

TOP_LEVEL_KEYS: tuple[str, ...] = (
    "name",
    "description",
    "symbol",
    "interval",
    "features",
    "long_entry",
    "long_exit",
    "risk",
)
REQUIRED_KEYS: tuple[str, ...] = (
    "name",
    "symbol",
    "interval",
    "features",
    "long_entry",
    "long_exit",
)
RISK_KEYS: tuple[str, ...] = ("position_size_pct", "stop_loss_pct", "take_profit_pct")
# Keys an agent may reach for that the spot long/flat engine cannot honour.
SHORT_KEYS: tuple[str, ...] = ("short_entry", "short_exit")

SYMBOL_PATTERN = r"^[A-Z0-9]{2,20}$"
ALIAS_PATTERN = r"^[a-z][a-z0-9_]{0,30}$"
MAX_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 2000
MIN_FEATURES = 1
MAX_FEATURES = 4
MAX_FEATURE_PARAMS = 6  # every param of every used feature, defaults included

# Risk bounds as (exclusive low, inclusive high), percent of price / equity.
RISK_BOUNDS: Mapping[str, tuple[float, float]] = {
    "position_size_pct": (0.0, 100.0),
    "stop_loss_pct": (0.0, 50.0),
    "take_profit_pct": (0.0, 500.0),
}
# Smallest value offered to a tuner for a risk percentage (the validation low is exclusive).
RISK_SEARCH_FLOOR = 0.01

RESERVED_ALIASES: frozenset[str] = frozenset({*PRICE_INPUTS, *FUNCTIONS})

# --------------------------------------------------------------------------- overlay plans
# A risk overlay (PLAN.md Phase 15): buy-and-hold of one symbol, flat at bar t+1's open iff
# ``flat_when`` holds at bar t's close. Exposure is 0 or 1 only (owner decision), so there is no
# ``risk`` key. Tighter limits than edge plans: overlays use published parameters, untuned.
OVERLAY_KIND = "overlay"
OVERLAY_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "kind",
    "name",
    "description",
    "symbol",
    "interval",
    "features",
    "flat_when",
)
OVERLAY_REQUIRED_KEYS: tuple[str, ...] = (
    "kind",
    "name",
    "symbol",
    "interval",
    "features",
    "flat_when",
)
MAX_OVERLAY_FEATURES = 3
MAX_OVERLAY_PARAMS = 3  # every param of every used feature, defaults included


def _param_schema(spec: FeatureSpec) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for p in spec.params:
        props[p.name] = {
            "type": "integer" if p.kind == "int" else "number",
            "minimum": p.low,
            "maximum": p.high,
            "default": int(p.default) if p.kind == "int" else float(p.default),
            "description": p.description,
        }
    return {"type": "object", "properties": props, "additionalProperties": False}


def _resolve(registry: Mapping[str, FeatureSpec] | None) -> Mapping[str, FeatureSpec]:
    if registry is None:
        from lab.registry import REGISTRY

        return REGISTRY
    return registry


def _feature_branches(registry: Mapping[str, FeatureSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "object",
            "properties": {name: _param_schema(spec)},
            "required": [name],
            "additionalProperties": False,
            "description": f"{spec.description} Outputs: {', '.join(spec.outputs)}.",
        }
        for name, spec in sorted(registry.items())
    ]


_EXPR_GRAMMAR = (
    "Condition over feature aliases (`alias` or `alias.output`), price inputs "
    f"({', '.join(PRICE_INPUTS)}), numbers, + - * /, > < >= <=, and/or/not, "
    f"crosses_above(a, b), crosses_below(a, b), prev(x, n) with 1 <= n <= {MAX_PREV_BARS}. "
    "Units must match: compare prices with prices (close > ema_slow), volume with volume "
    "(volume > 2 * vol_avg), or use a ratio (close / ema_slow > 1.02); a price or volume "
    "compared with a fixed number (close > 50000) is a hidden level and is rejected. "
    "0 fits any scale (macd.hist > 0)."
)


def _expr_schema(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_EXPR_CHARS,
        "description": description,
    }


def _features_schema(
    registry: Mapping[str, FeatureSpec], max_features: int, max_params: int
) -> dict[str, Any]:
    return {
        "type": "object",
        "minProperties": MIN_FEATURES,
        "maxProperties": max_features,
        "propertyNames": {
            "pattern": ALIAS_PATTERN,
            "not": {"enum": sorted(RESERVED_ALIASES)},
        },
        "additionalProperties": {"oneOf": _feature_branches(registry)},
        "description": (
            f"alias -> {{registry_feature: {{params}}}}; at most {max_params} "
            "params in total across features, defaults included."
        ),
    }


def plan_json_schema(registry: Mapping[str, FeatureSpec] | None = None) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for a plan, with one ``oneOf`` branch per registry feature.

    Expressions are only described here (``maxLength``); their grammar is checked by
    :func:`lab.plan.validator.validate_plan`.
    """
    registry = _resolve(registry)
    expr = _expr_schema(_EXPR_GRAMMAR)

    def pct(key: str, nullable: bool) -> dict[str, Any]:
        low, high = RISK_BOUNDS[key]
        schema: dict[str, Any] = {
            "type": ["number", "null"] if nullable else "number",
            "exclusiveMinimum": low,
            "maximum": high,
        }
        return schema

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Strategy plan (long/flat spot)",
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_CHARS},
            "description": {"type": "string", "maxLength": MAX_DESCRIPTION_CHARS},
            "symbol": {"type": "string", "pattern": SYMBOL_PATTERN},
            "interval": {"type": "string", "enum": list(INTERVAL_SECONDS)},
            "features": _features_schema(registry, MAX_FEATURES, MAX_FEATURE_PARAMS),
            "long_entry": expr,
            "long_exit": expr,
            "risk": {
                "type": "object",
                "properties": {
                    "position_size_pct": pct("position_size_pct", nullable=False),
                    "stop_loss_pct": pct("stop_loss_pct", nullable=True),
                    "take_profit_pct": pct("take_profit_pct", nullable=True),
                },
                "additionalProperties": False,
            },
        },
        "required": list(REQUIRED_KEYS),
        "additionalProperties": False,
    }


OVERLAY_FLAT_WHEN_NOTE = (
    "The risk rule: when it is true at bar t's close the overlay is flat (0% exposure) from bar "
    "t+1's open; otherwise it holds 100% of the symbol (buy-and-hold). A NaN in any operand "
    "(e.g. a derivatives archive gap or the start of an expanding history) makes the whole rule "
    "unknown, and an unknown rule keeps the overlay invested: it counts as NOT flat (even under "
    "'or' with a true clause), so the overlay falls back to buy-and-hold there. Derivatives "
    "features (funding, premium, basis, open interest, long/short ratio) may appear anywhere "
    "in it: they are exposure or crash-risk filters, not direction signals, which is exactly "
    "this rule's role. "
)


def overlay_plan_json_schema(registry: Mapping[str, FeatureSpec] | None = None) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for a risk-overlay plan (``{"kind": "overlay", ...}``,
    PLAN.md Phase 15): one symbol held 100% or flat, ``flat_when`` decided at bar t's close and
    filled at bar t+1's open; 1 to :data:`MAX_OVERLAY_FEATURES` features with at most
    :data:`MAX_OVERLAY_PARAMS` params in total (defaults included); no ``risk`` key."""
    registry = _resolve(registry)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Risk-overlay plan (buy-and-hold of one symbol, exposure 0 or 1)",
        "type": "object",
        "properties": {
            "kind": {"const": OVERLAY_KIND},
            "name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_CHARS},
            "description": {"type": "string", "maxLength": MAX_DESCRIPTION_CHARS},
            "symbol": {"type": "string", "pattern": SYMBOL_PATTERN},
            "interval": {"type": "string", "enum": list(INTERVAL_SECONDS)},
            "features": _features_schema(registry, MAX_OVERLAY_FEATURES, MAX_OVERLAY_PARAMS),
            "flat_when": _expr_schema(OVERLAY_FLAT_WHEN_NOTE + _EXPR_GRAMMAR),
        },
        "required": list(OVERLAY_REQUIRED_KEYS),
        "additionalProperties": False,
    }
