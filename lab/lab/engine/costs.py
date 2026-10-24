"""Per-bar trading costs for the engine (PLAN.md Phase 7 How §3).

Costs are charged per side on traded value:

* **fee**: ``CostModel.fee_rate`` x traded value (Binance spot taker fee, 0.1% by default).
* **slippage**: the fill price is moved against the trader by a fraction of the reference
  price (the bar's open, or the stop price for stop fills). ``slippage_series`` gives that
  fraction for every bar.

Volatility-scaled slippage (``CostModel.slippage_vol_mult > 0``) widens the base rate when the
market has just been unusually wild. It is causal: the fraction applied to a fill at bar j's
open uses only bars up to and including j-1, because the fill bar's own high/low are not known
when its open trades::

    r[i]     = (high[i] - low[i]) / close[i]                      # relative bar range
    med[i]   = median(r[i - median_window + 1 .. i])              # NaN until the window is full
    slip[j]  = min(slippage_rate + slippage_vol_mult * max(0, r[j-1] - med[j-1]), slippage_cap)
    slip[0]  = slippage_rate; slip[j] = slippage_rate while med[j-1] is still NaN (warm-up)

``slippage_cap`` (>= ``slippage_rate``, checked by :func:`validate_costs`) bounds every bar's
fraction, so one freak bar range cannot charge an absurd fill. Callers that backtest a window
slice should compute the series on the longer history and slice it (the engine's
``cost_history=``), so the first ``median_window`` bars of the window already get the
vol-scaled rate instead of the warm-up base rate.

Pure: arrays in, arrays out. Results are historical and not forward-looking.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from lab.types import CostModel, validate_candles


def validate_costs(costs: CostModel) -> None:
    """Raise ``ValueError`` naming the field if any cost rate is negative or non-finite, the
    fee is >= 1, or ``slippage_cap`` is below ``slippage_rate``."""
    for name in ("fee_rate", "slippage_rate", "slippage_vol_mult", "slippage_cap"):
        value = getattr(costs, name)
        if not np.isfinite(value) or value < 0:
            raise ValueError(f"costs.{name} must be a finite number >= 0, got {value!r}")
    if costs.fee_rate >= 1:
        raise ValueError(f"costs.fee_rate must be < 1 (a fraction), got {costs.fee_rate!r}")
    if costs.slippage_cap < costs.slippage_rate:
        raise ValueError(
            f"costs.slippage_cap ({costs.slippage_cap!r}) must be >= costs.slippage_rate "
            f"({costs.slippage_rate!r}); a cap below the base rate would silently lower it"
        )


def slippage_series(
    candles: pd.DataFrame, costs: CostModel, *, median_window: int = 100
) -> np.ndarray:
    """Slippage fraction applied to a fill at each bar's open (see the module docstring).

    ``slip[j]`` depends only on candle rows ``< j``, so changing bar j's own high/low/close
    never changes the slippage charged on bar j's open.
    """
    validate_candles(candles)
    validate_costs(costs)
    if isinstance(median_window, bool) or not isinstance(median_window, int) or median_window < 1:
        raise ValueError(f"median_window must be an int >= 1, got {median_window!r}")

    n = len(candles)
    cap = float(costs.slippage_cap)
    slip = np.full(n, float(costs.slippage_rate))
    if costs.slippage_vol_mult == 0.0 or n < 2:
        return slip

    high = candles["high"].to_numpy(dtype=float)
    low = candles["low"].to_numpy(dtype=float)
    close = candles["close"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rng = (high - low) / close
    rng = np.where(np.isfinite(rng), rng, np.nan)
    med = (
        pd.Series(rng)
        .rolling(median_window, min_periods=median_window)
        .median()
        .to_numpy(dtype=float)
    )
    excess = rng - med  # NaN during warm-up or where the range is undefined
    excess = np.where(np.isfinite(excess), np.maximum(excess, 0.0), 0.0)
    slip[1:] = np.minimum(costs.slippage_rate + costs.slippage_vol_mult * excess[:-1], cap)
    return slip
