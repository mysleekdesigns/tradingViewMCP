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


def plan_json_schema(registry: Mapping[str, FeatureSpec] | None = None) -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for a plan, with one ``oneOf`` branch per registry feature.

    Expressions are only described here (``maxLength``); their grammar is checked by
    :func:`lab.plan.validator.validate_plan`.
    """
    if registry is None:
        from lab.registry import REGISTRY

        registry = REGISTRY
    feature_branches = [
        {
            "type": "object",
            "properties": {name: _param_schema(spec)},
            "required": [name],
            "additionalProperties": False,
            "description": f"{spec.description} Outputs: {', '.join(spec.outputs)}.",
        }
        for name, spec in sorted(registry.items())
    ]
    expr = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_EXPR_CHARS,
        "description": (
            "Condition over feature aliases (`alias` or `alias.output`), price inputs "
            f"({', '.join(PRICE_INPUTS)}), numbers, + - * /, > < >= <=, and/or/not, "
            f"crosses_above(a, b), crosses_below(a, b), prev(x, n) with 1 <= n <= {MAX_PREV_BARS}. "
            "Units must match: compare prices with prices (close > ema_slow), volume with volume "
            "(volume > 2 * vol_avg), or use a ratio (close / ema_slow > 1.02); a price or volume "
            "compared with a fixed number (close > 50000) is a hidden level and is rejected. "
            "0 fits any scale (macd.hist > 0)."
        ),
    }

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
            "features": {
                "type": "object",
                "minProperties": MIN_FEATURES,
                "maxProperties": MAX_FEATURES,
                "propertyNames": {
                    "pattern": ALIAS_PATTERN,
                    "not": {"enum": sorted(RESERVED_ALIASES)},
                },
                "additionalProperties": {"oneOf": feature_branches},
                "description": (
                    f"alias -> {{registry_feature: {{params}}}}; at most {MAX_FEATURE_PARAMS} "
                    "params in total across features, defaults included."
                ),
            },
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
