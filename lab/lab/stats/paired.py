"""Paired stationary-bootstrap tests for the risk-overlay question (PLAN.md Phase 15).

Two return series on the same bars (an overlay and its benchmark) are resampled *together*:
one stationary-bootstrap index row (``bootstrap.stationary_bootstrap_indices``, Politis &
Romano 1994) is applied to both, so their cross-dependence (same crash, same bar) is kept.
For each row ``d* = stat(a[i]) - stat(b[i])``; non-finite ``d*`` are dropped.

* ``point = stat(a) - stat(b)`` on the original series.
* ``p_value = (1 + #{d* >= 0}) / (1 + n_valid)``, the one-sided bootstrap p for
  H1: ``stat(a) < stat(b)`` (floor ``1 / (1 + n_valid)``, never 0).
* ``lower`` / ``upper`` = the ``1 - level`` / ``level`` quantiles of ``d*``: one-sided bounds.

Index rows are drawn in chunks from one ``np.random.default_rng(seed)`` (memory bound); each
row is shared by ``a`` and ``b``, and results are deterministic per seed. Sharpe is
annualised with ``bars_per_year(interval)`` (365 days, never 252).

Pure numpy; no I/O. Results are historical, not forward-looking.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from lab.stats.bootstrap import stationary_bootstrap_indices
from lab.stats.drawdown import cdar
from lab.stats.metrics import sharpe_per_bar
from lab.types import DAYS_PER_YEAR, bars_per_year

__all__ = [
    "PairedBootstrap",
    "bootstrap_block_bars",
    "break_even_cost_multiple",
    "cdar_difference_test",
    "paired_stationary_bootstrap",
    "sharpe_difference_test",
]

_CHUNK_ELEMENTS = 2_000_000  # index-matrix cells drawn at once (~16 MB of int64)

_ArrayLike = pd.Series | np.ndarray | Sequence[float]


@dataclass(frozen=True)
class PairedBootstrap:
    point: float  # stat(a) - stat(b) on the original series
    p_value: float  # (1 + #{d* >= 0}) / (1 + n_valid); H1: stat(a) < stat(b)
    lower: float  # quantile(d*, 1 - level)
    upper: float  # quantile(d*, level)
    level: float
    n_samples: int  # resamples drawn
    n_valid: int  # resamples whose d* was finite
    mean_block: float
    seed: int


def _as_1d(values: _ArrayLike, name: str) -> np.ndarray:
    arr = np.asarray(values.to_numpy() if isinstance(values, pd.Series) else values, dtype=float)
    if arr.ndim != 1 or arr.size < 1:
        raise ValueError(f"{name} must be a non-empty 1-D series, got shape {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} contains NaN or inf; per-bar returns must be finite")
    return arr


def paired_stationary_bootstrap(
    a: _ArrayLike,
    b: _ArrayLike,
    statistic: Callable[[np.ndarray], float],
    *,
    n_samples: int,
    mean_block: float,
    seed: int,
    level: float,
) -> PairedBootstrap:
    """Paired stationary bootstrap of ``statistic(a) - statistic(b)`` (see module docstring)."""
    xa, xb = _as_1d(a, "a"), _as_1d(b, "b")
    if xa.size != xb.size:
        raise ValueError(f"a has {xa.size} bars but b has {xb.size}; they must align")
    if int(n_samples) != n_samples or n_samples < 1:
        raise ValueError(f"n_samples must be an integer >= 1, got {n_samples!r}")
    if not (math.isfinite(float(level)) and 0.5 < float(level) < 1.0):
        raise ValueError(f"level must be strictly between 0.5 and 1, got {level!r}")
    if int(seed) != seed:
        raise ValueError(f"seed must be an integer, got {seed!r}")
    n, total = xa.size, int(n_samples)
    rng = np.random.default_rng(int(seed))
    chunk = max(1, _CHUNK_ELEMENTS // n)
    diffs = np.empty(total)
    done = 0
    while done < total:
        k = min(chunk, total - done)
        idx = stationary_bootstrap_indices(n, mean_block=mean_block, n_samples=k, rng=rng)
        for row in idx:
            diffs[done] = float(statistic(xa[row])) - float(statistic(xb[row]))
            done += 1
    valid = diffs[np.isfinite(diffs)]
    n_valid = int(valid.size)
    p_value = (1.0 + float(np.count_nonzero(valid >= 0.0))) / (1.0 + n_valid)
    if n_valid == 0:
        lower = upper = float("nan")
    else:
        lower, upper = (float(q) for q in np.quantile(valid, [1.0 - level, level]))
    return PairedBootstrap(
        point=float(statistic(xa)) - float(statistic(xb)),
        p_value=p_value,
        lower=lower,
        upper=upper,
        level=float(level),
        n_samples=total,
        n_valid=n_valid,
        mean_block=float(mean_block),
        seed=int(seed),
    )


def cdar_difference_test(
    overlay: _ArrayLike,
    matched: _ArrayLike,
    *,
    confidence: float,
    n_samples: int,
    mean_block: float,
    seed: int,
    level: float,
) -> PairedBootstrap:
    """CDaR(overlay) - CDaR(matched); small ``p_value`` = overlay's CDaR is lower."""

    def stat(x: np.ndarray) -> float:
        return cdar(x, confidence)

    return paired_stationary_bootstrap(
        overlay, matched, stat, n_samples=n_samples, mean_block=mean_block, seed=seed, level=level
    )


def sharpe_difference_test(
    overlay: _ArrayLike,
    benchmark: _ArrayLike,
    *,
    interval: str,
    n_samples: int,
    mean_block: float,
    seed: int,
    level: float,
) -> PairedBootstrap:
    """Annualised Sharpe(overlay) - Sharpe(benchmark), ``sqrt(bars_per_year(interval))``.

    ``lower`` is the one-sided ``level`` lower bound used for non-inferiority. A resample with
    zero variance in either series has an undefined Sharpe and is dropped (``n_valid``).
    """
    scale = math.sqrt(bars_per_year(interval))

    def stat(x: np.ndarray) -> float:
        return sharpe_per_bar(x) * scale

    return paired_stationary_bootstrap(
        overlay, benchmark, stat, n_samples=n_samples, mean_block=mean_block, seed=seed, level=level
    )


def break_even_cost_multiple(holds: Mapping[float, bool]) -> float | None:
    """Largest cost multiple ``m`` with ``holds[k]`` true for every tested ``k <= m``.

    ``None`` if nothing was tested or the smallest multiple already fails. A failure stops the
    walk: a later multiple that holds again (a gap) does not count.
    """
    best: float | None = None
    for m in sorted(holds):
        mf = float(m)
        if not math.isfinite(mf) or mf <= 0.0:
            raise ValueError(f"cost multiples must be finite and > 0, got {m!r}")
        ok = holds[m]
        if not isinstance(ok, bool | np.bool_):
            raise ValueError(f"holds[{m!r}] must be a bool, got {ok!r}")
        if not ok:
            break
        best = mf
    return best


def bootstrap_block_bars(days: float, interval: str) -> int:
    """Mean bootstrap block in bars: ``max(1, ceil(days * bars_per_day(interval)))``."""
    d = float(days)
    if not (math.isfinite(d) and d >= 0.0):
        raise ValueError(f"days must be finite and >= 0, got {days!r}")
    bars_per_day = bars_per_year(interval) / DAYS_PER_YEAR
    return max(1, math.ceil(round(d * bars_per_day, 9)))
