"""Monte Carlo trade-order reshuffle (PLAN.md Phase 7 adversarial checks).

Compounds the per-trade returns in ``n_paths`` random orders. Multiplication commutes, so the
final return is the same on every path (up to floating-point rounding, checked to 1e-9
relative): reshuffling says nothing about whether the edge is real. What it shows is the
**drawdown** the same trades could have produced in a different order, i.e. how much of the
backtest's shallow drawdown was luck of sequencing.

``trade_returns`` are per-trade returns on account equity (fractions, > -1 except a total loss
of exactly -1). With position sizes below 100%, scale each trade's ``return_pct`` by its size
before calling. Pure numpy, explicit seed. Results are historical, not forward-looking.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

__all__ = ["MonteCarloResult", "trade_reshuffle"]

_PERCENTILES = {"p5": 5.0, "p50": 50.0, "p95": 95.0}


@dataclass(frozen=True)
class MonteCarloResult:
    n_paths: int
    final_return: dict[str, float]  # "p5", "p50", "p95" of compounded total return
    max_drawdown: dict[str, float]  # "p5" is the deepest (most negative) of the three


def _percentiles(values: np.ndarray) -> dict[str, float]:
    qs = np.percentile(values, list(_PERCENTILES.values()))
    return {k: float(q) for k, q in zip(_PERCENTILES, qs, strict=True)}


def trade_reshuffle(
    trade_returns: np.ndarray | Sequence[float], *, n_paths: int, seed: int
) -> MonteCarloResult:
    """Percentiles of final return and max drawdown over ``n_paths`` random trade orders.

    Each path's equity starts at 1.0 before the first trade; max drawdown is the deepest fall
    from the running peak (<= 0). With no trades every statistic is 0.
    """
    if int(n_paths) != n_paths or n_paths < 1:
        raise ValueError(f"n_paths must be an integer >= 1, got {n_paths!r}")
    r = np.asarray(trade_returns, dtype=float)
    if r.ndim != 1:
        raise ValueError(f"trade_returns must be 1-D, got shape {r.shape}")
    if not np.all(np.isfinite(r)):
        raise ValueError("trade_returns contains NaN or inf")
    if np.any(r < -1.0):
        raise ValueError("trade_returns must be >= -1 (a trade cannot lose more than equity)")
    n_paths = int(n_paths)
    if r.size == 0:
        zeros = dict.fromkeys(_PERCENTILES, 0.0)
        return MonteCarloResult(n_paths=n_paths, final_return=zeros, max_drawdown=dict(zeros))

    rng = np.random.default_rng(seed)
    paths = rng.permuted(np.tile(r, (n_paths, 1)), axis=1)
    equity = np.concatenate([np.ones((n_paths, 1)), np.cumprod(1.0 + paths, axis=1)], axis=1)
    peak = np.maximum.accumulate(equity, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = np.where(peak > 0, equity / peak - 1.0, -1.0)
    max_dd = dd.min(axis=1)
    final = equity[:, -1] - 1.0
    expected = float(np.prod(1.0 + r) - 1.0)
    if not np.allclose(final, expected, rtol=1e-9, atol=1e-12):  # order invariance
        raise AssertionError("compounded final return changed with trade order")
    return MonteCarloResult(
        n_paths=n_paths, final_return=_percentiles(final), max_drawdown=_percentiles(max_dd)
    )
