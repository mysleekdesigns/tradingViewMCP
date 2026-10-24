"""Benchmarks every strategy is compared against (PLAN.md Phase 7 How §6).

* :func:`buy_and_hold` - all-in at the first fill and never trade again.
* :func:`vol_target_weights` / :func:`vol_target_buy_and_hold` - hold the asset with a weight
  that targets a fixed annualised volatility (the strongest simple benchmark in crypto).
* :func:`random_entry_null` - random, non-overlapping entries with the **same trade count**
  and the strategy's own holding lengths (permuted, without replacement, so every path has
  exactly the strategy's bars in position), through the same engine but **without stops**:
  the holding lengths already contain the strategy's realised stop exits, so applying the
  stops again would truncate the null's holds twice.

Pure: candles in, results out; no I/O, no prints, no module state. Every benchmark runs through
the same engine as the strategy, so fills (next tradable bar's open), fees, and slippage are
identical. Each accepts ``cost_history=`` (the history through the window end) so the
vol-scaled slippage at the window start is warmed up exactly like the strategy's. Annualisation
uses 365 days. Results are historical and not forward-looking.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from lab.engine import run_signals, run_weights
from lab.stats import compute_metrics, percentile_rank
from lab.types import (
    SECONDS_PER_DAY,
    BacktestResult,
    CostModel,
    RiskSpec,
    bars_per_year,
    interval_seconds,
    validate_candles,
)

__all__ = [
    "VOL_TARGET_MIN_REBALANCE",
    "RandomEntryResult",
    "buy_and_hold",
    "random_entry_null",
    "random_entry_signals",
    "vol_target_buy_and_hold",
    "vol_target_weights",
]

# Fallback default for direct callers of the pure function only. The runner always passes
# ``cfg.benchmarks.vol_min_rebalance`` from ``config/lab.toml`` (the value that changes results
# lives in config, with its justification there).
VOL_TARGET_MIN_REBALANCE = 0.10


def buy_and_hold(
    candles: pd.DataFrame,
    *,
    costs: CostModel,
    init_cash: float = 10_000.0,
    cost_history: pd.DataFrame | None = None,
) -> BacktestResult:
    """Target weight 1.0 decided at bar 0 (filled at the next tradable open), then hold.

    One buy, never sold: the position is marked to each close and stays open at the end.
    """
    validate_candles(candles)
    if len(candles) < 2:
        raise ValueError(f"buy_and_hold needs at least 2 bars, got {len(candles)}")
    weights = pd.Series(np.nan, index=candles.index, dtype=float)
    weights.iloc[0] = 1.0
    return run_weights(
        candles, weights, costs=costs, init_cash=init_cash, cost_history=cost_history
    )


def vol_target_weights(
    candles: pd.DataFrame,
    interval: str,
    *,
    target_annual: float,
    lookback_days: float,
    max_leverage: float,
) -> pd.Series:
    """Target weight per bar: ``min(max_leverage, target_annual / realised_vol_t)``.

    ``realised_vol_t`` is the sample stdev (ddof=1) of the last ``lookback`` close-to-close log
    returns ending at bar t, annualised with ``sqrt(bars_per_year(interval))``; ``lookback`` in
    bars = ``round(lookback_days * bars per day)``. The weight at t uses closes up to t only.
    Bars before a full look-back (warm-up) get weight 0; zero volatility gets ``max_leverage``.
    ``max_leverage`` must lie in (0, 1]: Binance spot cannot borrow.
    """
    validate_candles(candles)
    if not math.isfinite(target_annual) or target_annual <= 0:
        raise ValueError(f"target_annual must be > 0, got {target_annual!r}")
    if not math.isfinite(lookback_days) or lookback_days <= 0:
        raise ValueError(f"lookback_days must be > 0, got {lookback_days!r}")
    if not math.isfinite(max_leverage) or not 0 < max_leverage <= 1:
        raise ValueError(f"max_leverage must be in (0, 1] for spot, got {max_leverage!r}")
    bars_per_day = SECONDS_PER_DAY / interval_seconds(interval)
    lookback = max(2, round(lookback_days * bars_per_day))
    close = candles["close"].to_numpy(dtype=float)
    log_ret = np.full(len(close), np.nan)
    if len(close) > 1:
        log_ret[1:] = np.log(close[1:] / close[:-1])
    vol = pd.Series(log_ret, index=candles.index).rolling(lookback, min_periods=lookback).std(
        ddof=1
    ).to_numpy() * math.sqrt(bars_per_year(interval))
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = np.where(vol > 0, target_annual / vol, max_leverage)
    weights = np.where(np.isnan(vol), 0.0, np.minimum(max_leverage, raw))
    return pd.Series(weights, index=candles.index, name="weight", dtype=float)


def vol_target_buy_and_hold(
    candles: pd.DataFrame,
    interval: str,
    *,
    costs: CostModel,
    target_annual: float,
    lookback_days: float,
    max_leverage: float,
    min_rebalance: float = VOL_TARGET_MIN_REBALANCE,
    weights: pd.Series | None = None,
    init_cash: float = 10_000.0,
    cost_history: pd.DataFrame | None = None,
) -> BacktestResult:
    """Vol-targeted buy-and-hold through ``run_weights``.

    ``weights`` (on ``candles.index``) overrides the computed ones; callers pass weights
    computed on the full history before a window so the window starts warmed up.
    """
    if weights is None:
        weights = vol_target_weights(
            candles,
            interval,
            target_annual=target_annual,
            lookback_days=lookback_days,
            max_leverage=max_leverage,
        )
    elif not weights.index.equals(candles.index):
        raise ValueError("weights index must equal candles.index")
    return run_weights(
        candles,
        weights,
        costs=costs,
        init_cash=init_cash,
        min_rebalance=min_rebalance,
        cost_history=cost_history,
    )


# --------------------------------------------------------------------------- random entry


@dataclass(frozen=True)
class RandomEntryResult:
    """Annualised net Sharpe of each random-entry path (NaN where undefined).

    ``exposure_bars[p]`` is the total ``bars_held`` of path p's trades (identical on every
    path: the same holding lengths, only placed differently).
    """

    n_paths: int
    n_trades: int
    sharpes: np.ndarray
    sharpe_median: float
    sharpe_p95: float
    exposure_bars: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int64))

    def percentile(self, real_sharpe: float) -> float:
        """Percent (0-100) of finite null Sharpes strictly below ``real_sharpe``."""
        if self.sharpes.size == 0 or not math.isfinite(real_sharpe):
            return math.nan
        return percentile_rank(real_sharpe, self.sharpes)


def random_entry_signals(
    n_bars: int, holding: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """Bool entry/exit signal arrays for ``len(holding)`` non-overlapping trades in ``n_bars``.

    Trade i signals entry at row ``e_i`` and exit at row ``e_i + holding[i]``, so the engine
    holds it for ``holding[i]`` bars (fills at ``e_i + 1`` and ``e_i + holding[i] + 1``). The
    next entry signal comes at least one row after the previous exit signal. Gaps are a
    uniform random composition of the free rows; the last exit signal is at most row
    ``n_bars - 2`` so it can still fill.
    """
    k = len(holding)
    entries = np.zeros(n_bars, dtype=bool)
    exits = np.zeros(n_bars, dtype=bool)
    if k == 0:
        return entries, exits
    free = (n_bars - 1) - int(np.sum(holding + 1))
    if free < 0:
        raise ValueError(f"{k} trades holding {int(holding.sum())} bars do not fit in {n_bars}")
    cuts = np.sort(rng.integers(0, free + 1, size=k))
    gaps = np.diff(np.concatenate([[0], cuts]))  # k gaps before each trade, sum <= free
    pos = 0
    for g, h in zip(gaps, holding, strict=True):
        pos += int(g)
        entries[pos] = True
        exits[pos + int(h)] = True
        pos += int(h) + 1
    return entries, exits


def _fit_holding(holding: np.ndarray, n_bars: int) -> np.ndarray:
    """Trim the fewest bars needed (one at a time from the longest hold, min 1) to fit.

    The strategy's trades nearly always fit already; they can overflow by a few bars because
    the random layout needs one free signal row between trades while the strategy may re-enter
    on the bar right after a stop. The trim is deterministic, so every path keeps the same
    multiset of lengths (and total exposure).
    """
    k = len(holding)
    budget = (n_bars - 1) - k  # rows left for holding once each trade's entry row is counted
    out = holding.astype(np.int64).copy()
    if k == 0 or out.sum() <= budget:
        return out
    if k > budget:
        raise ValueError(f"{k} random trades cannot fit in {n_bars} bars")
    for _ in range(int(out.sum()) - budget):
        out[int(np.argmax(out))] -= 1  # k <= budget, so the longest hold is always > 1 here
    return out


def random_entry_null(
    candles: pd.DataFrame,
    interval: str,
    *,
    n_trades: int,
    holding_bars: Sequence[int] | np.ndarray,
    costs: CostModel,
    risk: RiskSpec,
    n_paths: int,
    seed: int,
    init_cash: float = 10_000.0,
    cost_history: pd.DataFrame | None = None,
) -> RandomEntryResult:
    """Random-entry null with the strategy's trade count and exact holding lengths.

    ``holding_bars`` are the strategy's ``Trade.bars_held`` (one per trade, so its length must
    equal ``n_trades``; lengths < 1 become 1 because a zero-bar hold cannot be placed). Each
    path permutes them WITHOUT replacement, places the trades at random non-overlapping
    positions (:func:`random_entry_signals`), and runs them through ``run_signals`` with the
    strategy's ``costs`` and position size but **no stop-loss / take-profit** (the lengths
    already reflect the strategy's realised stops). Every path therefore has the same total
    bars in position. Deterministic for ``seed``. With ``n_trades == 0`` there is nothing to
    compare: an empty result with NaN summaries.
    """
    validate_candles(candles)
    if isinstance(n_trades, bool) or not isinstance(n_trades, int | np.integer) or n_trades < 0:
        raise ValueError(f"n_trades must be an int >= 0, got {n_trades!r}")
    if isinstance(n_paths, bool) or not isinstance(n_paths, int | np.integer) or n_paths < 1:
        raise ValueError(f"n_paths must be an int >= 1, got {n_paths!r}")
    pool = np.maximum(1, np.asarray(holding_bars, dtype=np.int64).ravel())
    if n_trades == 0:
        return RandomEntryResult(0, 0, np.empty(0), math.nan, math.nan)
    if pool.size != int(n_trades):
        raise ValueError(
            f"holding_bars has {pool.size} lengths but n_trades is {n_trades}; pass one "
            "Trade.bars_held per strategy trade"
        )
    n = len(candles)
    pool = _fit_holding(pool, n)
    no_stops = replace(risk, stop_loss_pct=None, take_profit_pct=None)
    rng = np.random.default_rng(seed)
    sharpes = np.empty(int(n_paths))
    exposure = np.empty(int(n_paths), dtype=np.int64)
    for p in range(int(n_paths)):
        holding = rng.permutation(pool)
        ent, ex = random_entry_signals(n, holding, rng)
        res = run_signals(
            candles,
            pd.Series(ent, index=candles.index),
            pd.Series(ex, index=candles.index),
            costs=costs,
            risk=no_stops,
            init_cash=init_cash,
            cost_history=cost_history,
        )
        sharpes[p] = compute_metrics(res, interval).sharpe
        exposure[p] = sum(t.bars_held for t in res.trades)
    finite = sharpes[np.isfinite(sharpes)]
    median = float(np.median(finite)) if finite.size else math.nan
    p95 = float(np.percentile(finite, 95)) if finite.size else math.nan
    return RandomEntryResult(int(n_paths), int(n_trades), sharpes, median, p95, exposure)
