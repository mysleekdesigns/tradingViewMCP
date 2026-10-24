"""Probability of Backtest Overfitting via CSCV (PLAN.md Phase 7 How §6).

Bailey, Borwein, López de Prado & Zhu (2017), "The Probability of Backtest Overfitting",
Journal of Computational Finance 20(4), Section 2 (combinatorially symmetric cross-validation):

1. Split the T x N matrix of per-bar trial returns (same window for every trial) row-wise into
   S equal, contiguous blocks.
2. For every one of the C(S, S/2) ways to pick S/2 blocks as the in-sample set J (the other
   S/2 are the out-of-sample set J̄), pick the in-sample best trial n* by the metric.
3. Take n*'s relative rank among the N out-of-sample metrics, ω̄ = rank / (N + 1) with ranks
   1..N ascending (ties get the average rank), and its logit λ = ln(ω̄ / (1 - ω̄)).
4. PBO = share of combinations with λ <= 0, i.e. the in-sample winner landed at or below
   the out-of-sample median.

Conventions: when T is not a multiple of S the oldest ``T mod S`` rows are dropped so every
block has the same length. A NaN metric (e.g. zero-variance Sharpe) ranks below every number;
in-sample ties go to the lowest column index.

Pure numpy; no I/O. Results are historical, not forward-looking.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations
from math import comb

import numpy as np

__all__ = ["PBOResult", "pbo_cscv"]


@dataclass(frozen=True)
class PBOResult:
    pbo: float  # share of combinations whose in-sample best is at/below the OOS median
    logits: np.ndarray  # λ per combination, shape (n_combinations,)
    n_splits: int
    n_combinations: int
    n_trials: int


def _combination_masks(n_splits: int) -> np.ndarray:
    """Boolean (C, S) matrix; row c marks the in-sample blocks of combination c."""
    half = n_splits // 2
    combos = np.array(list(combinations(range(n_splits), half)), dtype=np.intp)
    masks = np.zeros((combos.shape[0], n_splits), dtype=bool)
    masks[np.arange(combos.shape[0])[:, None], combos] = True
    return masks


def _sharpe_per_combination(blocks: np.ndarray, masks: np.ndarray) -> tuple[np.ndarray, ...]:
    """Per-bar Sharpe (ddof=1) of every trial on the in- and out-of-sample halves.

    ``blocks`` is (S, m, N). Sums per block are combined with one matrix product, so all
    C(S, S/2) combinations cost two (C x S) @ (S x N) products. Columns are shifted by their
    full-sample mean first; the shift leaves variances unchanged and avoids cancellation.
    """
    _, m, _ = blocks.shape
    shift = blocks.mean(axis=(0, 1))
    centred = blocks - shift
    s1 = centred.sum(axis=1)  # (S, N)
    s2 = (centred**2).sum(axis=1)
    w = masks.astype(float)
    out = []
    for weights in (w, 1.0 - w):
        count = weights.sum(axis=1, keepdims=True) * m  # (C, 1)
        a = weights @ s1
        b = weights @ s2
        mean_c = a / count
        var = (b - count * mean_c**2) / (count - 1.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            sr = np.where(var > 0, (mean_c + shift) / np.sqrt(np.where(var > 0, var, 1.0)), np.nan)
        out.append(sr)
    return out[0], out[1]


def _metric_per_combination(
    blocks: np.ndarray, masks: np.ndarray, metric: Callable[[np.ndarray], float]
) -> tuple[np.ndarray, np.ndarray]:
    """Generic path: ``metric`` on every trial column of every half (slow: 2 x C x N calls)."""
    n_comb = masks.shape[0]
    n_trials = blocks.shape[2]
    is_vals = np.empty((n_comb, n_trials))
    oos_vals = np.empty((n_comb, n_trials))
    for c in range(n_comb):
        train = blocks[masks[c]].reshape(-1, n_trials)
        test = blocks[~masks[c]].reshape(-1, n_trials)
        for j in range(n_trials):
            is_vals[c, j] = float(metric(train[:, j]))
            oos_vals[c, j] = float(metric(test[:, j]))
    return is_vals, oos_vals


def pbo_cscv(
    returns_matrix: np.ndarray,
    *,
    n_splits: int = 16,
    metric: Callable[[np.ndarray], float] | None = None,
) -> PBOResult:
    """PBO of a T x N matrix of per-bar returns (rows = bars, columns = trials).

    ``metric(returns_1d) -> float`` scores one trial on one half; higher is better. The default
    is the per-bar Sharpe (ddof=1), computed in closed form for all combinations at once
    (S=16, N=16, T=15,000 takes well under a second). A custom metric runs a Python loop.

    Raises ``ValueError`` unless ``returns_matrix`` is finite and 2-D with N >= 2 columns,
    ``n_splits`` is even and >= 2, and T >= ``n_splits``.
    """
    mat = np.asarray(returns_matrix, dtype=float)
    if mat.ndim != 2:
        raise ValueError(f"returns_matrix must be 2-D (T bars x N trials), got shape {mat.shape}")
    n_rows, n_trials = mat.shape
    if n_trials < 2:
        raise ValueError(f"returns_matrix needs N >= 2 trial columns, got {n_trials}")
    if int(n_splits) != n_splits or n_splits < 2 or n_splits % 2:
        raise ValueError(f"n_splits must be an even integer >= 2, got {n_splits!r}")
    n_splits = int(n_splits)
    if n_rows < n_splits:
        raise ValueError(f"returns_matrix has T={n_rows} rows; need T >= n_splits={n_splits}")
    if not np.all(np.isfinite(mat)):
        raise ValueError("returns_matrix contains NaN or inf; per-bar returns must be finite")

    m = n_rows // n_splits
    blocks = mat[n_rows - m * n_splits :].reshape(n_splits, m, n_trials)
    masks = _combination_masks(n_splits)
    if metric is None:
        is_vals, oos_vals = _sharpe_per_combination(blocks, masks)
    else:
        is_vals, oos_vals = _metric_per_combination(blocks, masks, metric)

    is_vals = np.where(np.isnan(is_vals), -np.inf, is_vals)
    oos_vals = np.where(np.isnan(oos_vals), -np.inf, oos_vals)
    rows = np.arange(masks.shape[0])
    best = np.argmax(is_vals, axis=1)  # first index on ties
    chosen = oos_vals[rows, best][:, None]
    below = (oos_vals < chosen).sum(axis=1)
    equal = (oos_vals == chosen).sum(axis=1) - 1  # other trials tied with n*
    rank = 1.0 + below + 0.5 * equal
    omega = rank / (n_trials + 1.0)
    logits = np.log(omega / (1.0 - omega))
    return PBOResult(
        pbo=float(np.mean(logits <= 0.0)),
        logits=logits,
        n_splits=n_splits,
        n_combinations=comb(n_splits, n_splits // 2),
        n_trials=int(n_trials),
    )
