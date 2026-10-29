"""Drawdown statistics for the risk-overlay question (PLAN.md Phase 15).

* ``drawdown_series``: bar-level drawdown ``DD_k = 1 - E_k / peak_k`` in ``[0, 1]`` of the
  equity curve ``E_k = prod_{i<=k} (1 + r_i)``, the running peak starting at the initial
  equity 1.0 (so a loss on the first bar counts, as in ``metrics._max_drawdown``). Its maximum
  is the magnitude of ``Metrics.max_drawdown``.
* ``cdar``: conditional drawdown at risk of Chekhlov, Uryasev & Zabarankin (2005),
  ``CDaR_a = min_z  z + sum_k max(DD_k - z, 0) / ((1 - a) n)``: the mean of the worst
  ``(1 - a)`` fraction of the ``n`` bar drawdowns, with the boundary bar weighted by its exact
  fractional share. ``a = 0`` gives the mean drawdown; ``a -> 1`` gives the maximum drawdown.
* ``drawdown_episodes``: maximal runs of bars with ``DD_k > 0``; an episode runs from the last
  bar at the peak (``-1`` = the initial equity, before bar 0) to the first bar back at (or
  above) that peak (``None`` while unrecovered). Equality with the peak counts as recovery;
  no tolerance is applied, consistent with ``drawdown_series``.
* ``exposure_matched_returns``: the benchmark held at a constant fraction ``e`` of equity,
  rebalanced every bar at zero cost (``e * r_b``). Zero cost and perfect rebalancing favour the
  benchmark, so beating it is the conservative test.

Pure numpy; no I/O. Results are historical, not forward-looking.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "DrawdownEpisode",
    "cdar",
    "cdar_from_drawdowns",
    "count_drawdown_episodes",
    "drawdown_episodes",
    "drawdown_series",
    "exposure_matched_returns",
]

_ArrayLike = pd.Series | np.ndarray | Sequence[float]


def _returns_1d(values: _ArrayLike, name: str = "returns") -> np.ndarray:
    arr = np.asarray(values.to_numpy() if isinstance(values, pd.Series) else values, dtype=float)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be 1-D, got shape {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} contains NaN or inf; per-bar returns must be finite")
    return arr


def drawdown_series(returns: _ArrayLike) -> np.ndarray:
    """Bar drawdowns ``DD_k = 1 - E_k / peak_k`` in ``[0, 1]``, one per return (length ``n``).

    The running peak includes the initial equity 1.0. A return below -1 (equity below zero) is
    rejected; a return of exactly -1 wipes the account out (``DD = 1`` from then on).
    """
    arr = _returns_1d(returns)
    if np.any(arr < -1.0):
        raise ValueError("returns below -1 imply negative equity; per-bar returns must be >= -1")
    if arr.size == 0:
        return np.zeros(0)
    equity = np.cumprod(1.0 + arr)
    peak = np.maximum.accumulate(np.maximum(equity, 1.0))  # peak_k >= 1.0 > 0
    return np.clip(1.0 - equity / peak, 0.0, 1.0)


def _check_confidence(confidence: float) -> float:
    a = float(confidence)
    if not (math.isfinite(a) and 0.0 <= a < 1.0):
        raise ValueError(f"confidence must be in [0, 1), got {confidence!r}")
    return a


def cdar_from_drawdowns(dd: _ArrayLike, confidence: float) -> float:
    """CDaR of a drawdown series ``dd`` (values in ``[0, 1]``) at ``confidence`` in ``[0, 1)``.

    Closed form of the CUZ minimisation: with ``m = (1 - a) n`` and ``d_(1) >= d_(2) >= ...``,
    ``CDaR = (sum_{i <= floor(m)} d_(i) + (m - floor(m)) d_(floor(m)+1)) / m``. NaN for an
    empty series.
    """
    a = _check_confidence(confidence)
    d = _returns_1d(dd, "dd")
    n = d.size
    if n == 0:
        return float("nan")
    m = (1.0 - a) * n
    if abs(m - round(m)) < 1e-9:  # 0.05 * 20 = 1.0000000000000009 is a whole bar
        m = float(round(m))
    m = min(m, float(n))
    desc = np.sort(d)[::-1]
    whole = math.floor(m)
    total = float(desc[:whole].sum())
    frac = m - whole
    if frac > 0.0:
        total += frac * float(desc[whole])
    return total / m


def cdar(returns: _ArrayLike, confidence: float = 0.95) -> float:
    """CDaR of the equity curve built from per-bar ``returns`` (see ``cdar_from_drawdowns``)."""
    return cdar_from_drawdowns(drawdown_series(returns), confidence)


@dataclass(frozen=True)
class DrawdownEpisode:
    """One drawdown: bar positions into the returns series.

    ``peak`` is the last bar at the running peak before the fall (``-1`` = initial equity),
    ``trough`` the first bar at the episode's deepest drawdown, ``recovery`` the first bar back
    at or above the peak (``None`` if the series ends first), ``depth`` that deepest drawdown.
    """

    peak: int
    trough: int
    recovery: int | None
    depth: float


def drawdown_episodes(returns: _ArrayLike) -> tuple[DrawdownEpisode, ...]:
    """Every peak-to-recovery drawdown episode, in time order (maximal runs of ``DD_k > 0``)."""
    dd = drawdown_series(returns)
    n = dd.size
    episodes: list[DrawdownEpisode] = []
    k = 0
    while k < n:
        if dd[k] <= 0.0:
            k += 1
            continue
        start = k
        while k < n and dd[k] > 0.0:
            k += 1
        run = dd[start:k]
        trough = start + int(np.argmax(run))
        episodes.append(
            DrawdownEpisode(
                peak=start - 1,
                trough=trough,
                recovery=k if k < n else None,
                depth=float(run.max()),
            )
        )
    return tuple(episodes)


def count_drawdown_episodes(returns: _ArrayLike, min_depth: float) -> int:
    """Number of episodes strictly deeper than ``min_depth`` (recovered or not)."""
    if not math.isfinite(float(min_depth)):
        raise ValueError(f"min_depth must be finite, got {min_depth!r}")
    return sum(1 for e in drawdown_episodes(returns) if e.depth > float(min_depth))


def exposure_matched_returns(benchmark_returns: _ArrayLike, exposure: float) -> np.ndarray:
    """``exposure * r_b``: the benchmark at a constant weight, rebalanced each bar, zero cost."""
    e = float(exposure)
    if not (math.isfinite(e) and 0.0 <= e <= 1.0):
        raise ValueError(f"exposure must be in [0, 1], got {exposure!r}")
    return e * _returns_1d(benchmark_returns, "benchmark_returns")
