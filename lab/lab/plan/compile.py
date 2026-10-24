"""Compile a validated plan into bar-close entry/exit signals.

Each feature is computed once on the full candle frame (registry features are causal, which
the perturbation tests enforce), then both rule expressions are evaluated vectorised. A signal
at bar t uses data through bar t's close only; the engine fills it at the next bar's open.

Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from lab.plan.expr import evaluate_expression
from lab.plan.validator import checked_expressions, resolve_registry
from lab.types import PRICE_INPUTS, CompiledSignals, FeatureSpec, Plan, validate_candles


def compute_features(
    plan: Plan, candles: pd.DataFrame, registry: Mapping[str, FeatureSpec] | None = None
) -> pd.DataFrame:
    """Feature frame on ``candles.index``: column ``alias`` for single-output features,
    ``alias.output`` for multi-output ones. Raises ``ValueError`` if a feature breaks the
    registry contract (wrong index or columns)."""
    reg = resolve_registry(registry)
    columns: dict[str, pd.Series] = {}
    for ref in plan.features:
        spec = reg.get(ref.feature)
        if spec is None:
            raise ValueError(
                f"feature {ref.feature!r} (alias {ref.alias!r}) is not in the registry"
            )
        params = {p.name: ref.params.get(p.name, p.default) for p in spec.params}
        out = spec.compute(candles, params)
        if not isinstance(out, pd.DataFrame) or not out.index.equals(candles.index):
            raise ValueError(f"feature {ref.feature!r} must return a DataFrame on the candle index")
        if list(out.columns) != list(spec.outputs):
            raise ValueError(
                f"feature {ref.feature!r} returned columns {list(out.columns)}, "
                f"expected {list(spec.outputs)}"
            )
        single = len(spec.outputs) == 1
        for col in spec.outputs:
            key = ref.alias if single else f"{ref.alias}.{col}"
            columns[key] = out[col].astype("float64")
    return pd.DataFrame(columns, index=candles.index)


def compile_plan(
    plan: Plan, candles: pd.DataFrame, registry: Mapping[str, FeatureSpec] | None = None
) -> CompiledSignals:
    """Entries/exits (bool Series on ``candles.index``) and the feature frame for ``plan``.

    ``warmup_bars`` = max feature warm-up + the expressions' look-back (nested ``prev`` n,
    +1 per ``crosses_*``). Signals are False for the first ``warmup_bars`` rows and wherever
    an operand is NaN.
    """
    reg = resolve_registry(registry)
    validate_candles(candles)
    entry_expr, exit_expr = checked_expressions(plan, reg)
    features = compute_features(plan, candles, reg)
    n = len(candles)
    cols: dict[str, np.ndarray] = {c: candles[c].to_numpy(dtype=np.float64) for c in PRICE_INPUTS}
    cols.update({c: features[c].to_numpy(dtype=np.float64) for c in features.columns})

    feature_warmup = 0
    for ref in plan.features:
        spec = reg[ref.feature]
        params = {p.name: ref.params.get(p.name, p.default) for p in spec.params}
        feature_warmup = max(feature_warmup, int(spec.warmup(params)))
    warmup = feature_warmup + max(entry_expr.lag, exit_expr.lag)

    entries = evaluate_expression(entry_expr, cols, n)
    exits = evaluate_expression(exit_expr, cols, n)
    cut = min(warmup, n)
    entries[:cut] = False
    exits[:cut] = False
    return CompiledSignals(
        entries=pd.Series(entries, index=candles.index, name="entries", dtype=bool),
        exits=pd.Series(exits, index=candles.index, name="exits", dtype=bool),
        features=features,
        warmup_bars=warmup,
    )
