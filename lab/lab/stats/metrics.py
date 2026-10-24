"""Headline metrics for one backtest (PLAN.md Phase 7 How §6).

Annualisation uses the crypto calendar: ``bars_per_year(interval)`` = 365 days x bars/day,
never 252. Sharpe, Sortino, CAGR, profit-factor-style helpers from ``hedgefund.backtest.metrics``
are reused with ``periods_per_year`` passed explicitly (their default is 252 trading days).

Departures from hedgeFund, on purpose:

* **Max drawdown** starts the equity curve at 1.0 *before* the first return, so a loss on the
  first bar counts. hedgeFund's ``max_drawdown`` starts at the first bar's equity and would
  miss it.
* **Win rate / profit factor** come from closed trades' ``pnl`` (``exit_reason != "end"``),
  not from per-bar returns.
* **Empty / flat inputs** return defined values (0 or NaN) instead of hedgeFund's NaN for an
  empty total return, and a wiped-out account has CAGR -1 instead of NaN.

Pure: arrays in, a frozen ``Metrics`` out; no I/O. Results are historical, not forward-looking.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
from hedgefund.backtest import metrics as hf_metrics

from lab.types import BacktestResult, Metrics, Trade, bars_per_year

NAN = float("nan")


def _as_1d(values: pd.Series | np.ndarray | Sequence[float], name: str) -> np.ndarray:
    arr = np.asarray(values.to_numpy() if isinstance(values, pd.Series) else values, dtype=float)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be 1-D, got shape {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} contains NaN or inf; per-bar values must be finite")
    return arr


def sharpe_per_bar(returns: pd.Series | np.ndarray | Sequence[float]) -> float:
    """Non-annualised Sharpe ``mean / std(ddof=1)`` (rf = 0), the statistic DSR/PSR need.

    NaN with fewer than 2 observations or zero variance.
    """
    arr = _as_1d(returns, "returns")
    if arr.size < 2:
        return NAN
    return hf_metrics.sharpe_ratio(arr, periods_per_year=1)


def _max_drawdown(arr: np.ndarray) -> float:
    """Deepest fall of ``cumprod(1 + r)`` from its running peak, peak starting at 1.0."""
    if arr.size == 0:
        return 0.0
    equity = np.concatenate([[1.0], np.cumprod(1.0 + arr)])
    peak = np.maximum.accumulate(equity)
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = np.where(peak > 0, equity / peak - 1.0, -1.0)
    return float(dd.min())


def _trade_stats(trades: Sequence[Trade]) -> tuple[float, float]:
    """``(win_rate, profit_factor)`` over closed trades (``exit_reason != "end"``).

    Win rate: share of closed trades with pnl > 0; NaN with no closed trades.
    Profit factor: gross wins / |gross losses|; inf with wins and no losses; NaN with neither.
    """
    pnl = np.array([t.pnl for t in trades if t.exit_reason != "end"], dtype=float)
    if pnl.size == 0:
        return NAN, NAN
    win_rate = float(np.mean(pnl > 0.0))
    wins = float(pnl[pnl > 0.0].sum())
    losses = float(-pnl[pnl < 0.0].sum())
    if losses > 0.0:
        return win_rate, wins / losses
    return win_rate, (float("inf") if wins > 0.0 else NAN)


def metrics_from_returns(
    returns: pd.Series | np.ndarray,
    interval: str,
    *,
    position: pd.Series | np.ndarray | None = None,
    trades: Sequence[Trade] = (),
) -> Metrics:
    """Metrics for per-bar simple ``returns`` on ``interval`` bars.

    * ``total_return`` = prod(1 + r) - 1; ``cagr`` over ``bars / bars_per_year`` years
      (NaN with no bars, -1 if equity hits zero).
    * ``volatility``, ``sharpe``, ``sortino`` annualised with ``sqrt(bars_per_year)``, rf = 0.
      Sharpe/Sortino are NaN when undefined (fewer than 2 bars, zero variance, no losing bar).
    * ``max_drawdown`` <= 0, from a curve that starts at 1.0.
    * ``win_rate`` / ``profit_factor`` from closed trades' pnl; ``n_trades`` counts every trade
      passed, including one still open at the window end (``exit_reason == "end"``).
    * ``exposure`` = share of bars with ``position != 0``; ``turnover`` =
      sum |Δposition| x bars_per_year / bars, the first position entered from flat.
      Both are 0 when ``position`` is None or empty.
    """
    ppy = bars_per_year(interval)
    arr = _as_1d(returns, "returns")
    n = int(arr.size)

    if n == 0:
        total, cagr = 0.0, NAN
    else:
        total = float(np.prod(1.0 + arr) - 1.0)
        growth = 1.0 + total
        cagr = float(growth ** (ppy / n) - 1.0) if growth > 0.0 else -1.0

    if n < 2:
        vol, sharpe, sortino = NAN, NAN, NAN
    else:
        vol = float(arr.std(ddof=1) * np.sqrt(ppy))
        sharpe = hf_metrics.sharpe_ratio(arr, periods_per_year=ppy)  # type: ignore[arg-type]
        sortino = hf_metrics.sortino_ratio(arr, periods_per_year=ppy)  # type: ignore[arg-type]

    if position is None:
        exposure, turnover = 0.0, 0.0
    else:
        pos = _as_1d(position, "position")
        if pos.size != n:
            raise ValueError(f"position has {pos.size} bars but returns has {n}; they must align")
        if n == 0:
            exposure, turnover = 0.0, 0.0
        else:
            exposure = hf_metrics.time_in_market(pos)
            turnover = hf_metrics.annual_turnover(pos, periods_per_year=ppy)  # type: ignore[arg-type]

    win_rate, profit_factor = _trade_stats(trades)
    return Metrics(
        total_return=total,
        cagr=cagr,
        volatility=vol,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=_max_drawdown(arr),
        win_rate=win_rate,
        profit_factor=profit_factor,
        n_trades=len(trades),
        exposure=exposure,
        turnover=turnover,
        bars=n,
    )


def compute_metrics(result: BacktestResult, interval: str, *, gross: bool = False) -> Metrics:
    """Metrics of an engine result: net ``returns`` by default, ``gross_returns`` if ``gross``.

    Trade-based fields (win rate, profit factor, trade count) always use ``result.trades``,
    whose pnl is net of costs; report net beside gross, never gross alone.
    """
    returns = result.gross_returns if gross else result.returns
    return metrics_from_returns(returns, interval, position=result.position, trades=result.trades)
