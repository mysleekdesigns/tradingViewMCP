"""Candle permutation for the full-pipeline permutation null (PLAN.md Phase 7 adversarial checks).

Bar permutation after Masters, "Permutation and Randomization Tests for Trading System
Development" (2020): describe each bar after the first by four log-ratios,

* gap   = ln(open_t / close_{t-1})
* high  = ln(high_t / open_t), low = ln(low_t / open_t), close = ln(close_t / open_t),

shuffle the bars' 4-tuples jointly (one permutation, each bar's shape stays intact) and
rebuild prices forward from bar 0, which is kept as is. The multiset of bar shapes and the
sum of log returns are unchanged, so the first and last close are preserved (the last up to
float rounding) while every serial pattern a strategy could exploit is destroyed. Running the
whole pipeline (features, signals, engine, costs) on many such paths gives the null.

Volume, ``tradable`` and any other columns stay on their original rows: the index, gaps and
untradeable bars keep their positions. ``permutation_p_value`` / ``percentile_rank`` turn the
real statistic and the null draws into the reported numbers.

Pure: DataFrame in, new DataFrame out, explicit seed. Results are historical, not
forward-looking.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from hedgefund.backtest.benchmarks import percentile_rank as _hf_percentile_rank
from hedgefund.backtest.benchmarks import permutation_p_value as _hf_permutation_p_value

from lab.types import validate_candles

__all__ = ["percentile_rank", "permutation_p_value", "permute_candles"]


def permute_candles(candles: pd.DataFrame, *, seed: int) -> pd.DataFrame:
    """A bar-permuted copy of ``candles`` (same index and non-price columns).

    Raises ``ValueError`` if ``candles`` breaks the candle convention or has a non-positive
    price. Fewer than 3 bars leave nothing to shuffle and return an unchanged copy.
    """
    validate_candles(candles)
    prices = candles[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if np.any(prices <= 0):
        raise ValueError("candles must have strictly positive open/high/low/close to permute")
    out = candles.copy()
    n = len(candles)
    if n < 3:
        return out

    o, h, lo, c = prices.T
    gap = np.log(o[1:] / c[:-1])
    rel_h = np.log(h[1:] / o[1:])
    rel_l = np.log(lo[1:] / o[1:])
    rel_c = np.log(c[1:] / o[1:])

    perm = np.random.default_rng(seed).permutation(n - 1)
    gap, rel_h, rel_l, rel_c = gap[perm], rel_h[perm], rel_l[perm], rel_c[perm]

    # log close_t = log close_0 + cumsum(gap + rel_c); open_t = close_{t-1} * exp(gap_t).
    log_close = np.log(c[0]) + np.cumsum(gap + rel_c)
    log_prev_close = np.concatenate([[np.log(c[0])], log_close[:-1]])
    log_open = log_prev_close + gap
    new_o = np.exp(log_open)
    new_c = np.exp(log_close)
    new_h = np.exp(log_open + rel_h)
    new_l = np.exp(log_open + rel_l)
    # Clamp only removes float rounding: each shape already satisfied the convention.
    new_h = np.maximum(new_h, np.maximum(new_o, new_c))
    new_l = np.minimum(new_l, np.minimum(new_o, new_c))

    for col, values in (("open", new_o), ("high", new_h), ("low", new_l), ("close", new_c)):
        out.iloc[1:, out.columns.get_loc(col)] = values
    return out


def percentile_rank(real: float, null: np.ndarray) -> float:
    """Percent (0-100) of finite null values strictly below ``real``; NaN if undefined.

    Wraps ``hedgefund.backtest.benchmarks.percentile_rank`` (no calendar assumption).
    """
    return _hf_percentile_rank(float(real), np.asarray(null, dtype=float))


def permutation_p_value(real: float, null: np.ndarray) -> float:
    """One-sided ``(count(null >= real) + 1) / (N + 1)`` over finite null draws; NaN if undefined.

    Wraps ``hedgefund.backtest.benchmarks.permutation_p_value`` (no calendar assumption).
    """
    return _hf_permutation_p_value(float(real), np.asarray(null, dtype=float))
