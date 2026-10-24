"""Stationary-bootstrap confidence intervals (PLAN.md Phase 7 How §6).

Politis & Romano (1994), "The Stationary Bootstrap", JASA 89(428): resample blocks whose
start is uniform on the series and whose length is geometric with mean ``mean_block``,
wrapping around the end. Unlike an iid bootstrap it keeps short-range dependence
(volatility clustering) inside each block, so the CI is not too narrow on bar returns.

Pure numpy; the generator is passed in or seeded explicitly. Results are historical,
not forward-looking.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = ["BootstrapCI", "stationary_bootstrap_ci", "stationary_bootstrap_indices"]

_CHUNK_ELEMENTS = 2_000_000  # index matrix cells built at once in the CI (~16 MB of int64)


@dataclass(frozen=True)
class BootstrapCI:
    point: float  # statistic on the original series
    low: float
    high: float
    level: float  # e.g. 0.95
    n_samples: int  # bootstrap resamples whose statistic was finite


def stationary_bootstrap_indices(
    n: int, *, mean_block: float, n_samples: int, rng: np.random.Generator
) -> np.ndarray:
    """``(n_samples, n)`` int array of resampled positions in ``range(n)``.

    Position 0 of each sample starts a block; every later position starts a new block with
    probability ``1 / mean_block`` (at a uniform random index), else continues the current
    block at the next index modulo ``n``.
    """
    if int(n) != n or n < 1:
        raise ValueError(f"n must be an integer >= 1, got {n!r}")
    if int(n_samples) != n_samples or n_samples < 1:
        raise ValueError(f"n_samples must be an integer >= 1, got {n_samples!r}")
    if not np.isfinite(mean_block) or mean_block < 1:
        raise ValueError(f"mean_block must be >= 1, got {mean_block!r}")
    n, n_samples = int(n), int(n_samples)
    starts = rng.integers(0, n, size=(n_samples, n))
    new_block = rng.random((n_samples, n)) < 1.0 / mean_block
    new_block[:, 0] = True
    pos = np.arange(n)
    # Column of the most recent block start at or before each position.
    last_start = np.maximum.accumulate(np.where(new_block, pos, 0), axis=1)
    rows = np.arange(n_samples)[:, None]
    return (starts[rows, last_start] + (pos - last_start)) % n


def stationary_bootstrap_ci(
    returns: pd.Series | np.ndarray | Sequence[float],
    statistic: Callable[[np.ndarray], float],
    *,
    n_samples: int,
    mean_block: float,
    confidence: float,
    seed: int,
) -> BootstrapCI:
    """Percentile CI of ``statistic`` under the stationary bootstrap.

    ``low``/``high`` are the ``(1 - confidence) / 2`` and ``(1 + confidence) / 2`` quantiles of
    the finite resampled statistics (NaN if none is finite). Deterministic for a given ``seed``.
    """
    arr = np.asarray(returns.to_numpy() if isinstance(returns, pd.Series) else returns, float)
    if arr.ndim != 1 or arr.size < 1:
        raise ValueError(f"returns must be a non-empty 1-D series, got shape {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError("returns contains NaN or inf; per-bar returns must be finite")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be strictly between 0 and 1, got {confidence!r}")
    if int(n_samples) != n_samples or n_samples < 1:
        raise ValueError(f"n_samples must be an integer >= 1, got {n_samples!r}")
    rng = np.random.default_rng(seed)
    n = arr.size
    chunk = max(1, _CHUNK_ELEMENTS // n)
    stats = np.empty(int(n_samples))
    done = 0
    while done < n_samples:
        k = min(chunk, int(n_samples) - done)
        idx = stationary_bootstrap_indices(n, mean_block=mean_block, n_samples=k, rng=rng)
        for row in idx:
            stats[done] = float(statistic(arr[row]))
            done += 1
    finite = stats[np.isfinite(stats)]
    alpha = (1.0 - confidence) / 2.0
    if finite.size == 0:
        low = high = float("nan")
    else:
        low, high = (float(q) for q in np.quantile(finite, [alpha, 1.0 - alpha]))
    return BootstrapCI(
        point=float(statistic(arr)),
        low=low,
        high=high,
        level=float(confidence),
        n_samples=int(finite.size),
    )
