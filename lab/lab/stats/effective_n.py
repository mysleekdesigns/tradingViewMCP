"""Effective number of independent trials: a DIAGNOSTIC (PLAN.md Phase 11).

Plans that trade alike are not independent bets. This module measures how alike: the
**eigenvalue participation ratio** of the correlation matrix ``C`` of the plans'
design-window returns::

    N_eff = (sum_i l_i)^2 / sum_i l_i^2,    l_i = eigenvalues of C

For ``M`` identical plans (``C`` all ones) it is 1; for ``M`` uncorrelated plans (``C = I``)
it is ``M``; for ``k`` equal blocks of identical plans it is ``k``. By Cauchy-Schwarz it lies
in ``[1, rank(C)]`` for any non-zero positive semi-definite ``C``. References: the
participation ratio of a correlation spectrum (e.g. Bouchaud & Potters, *Theory of Financial
Risk and Derivative Pricing*, random-matrix chapter); López de Prado & Lewis (2019),
"Detection of false investment strategies using unsupervised learning methods", Quantitative
Finance 19(9), discuss effective trial counts from clustered correlations.

**It is NOT the Deflated Sharpe Ratio's N** (the lab's DSR deflates with the nominal count of
distinct plans; ``lab.research.contract`` refuses ``dsr_trial_count = "effective"``):

* it counts the shared market factor twice: long-only crypto plans share one dominant factor,
  ``V[SR]`` (the spread of the trials' Sharpes) already reflects it, and the ratio is capped
  near ``1 / share^2`` where ``share = l1 / sum(l)`` is that factor's share of the spectrum
  (equal correlation 0.4: share ~0.4, cap ~6.25). On a one-factor
  zero-skill null (equal correlation 0.4) using it as N let about 24% of runs reach
  DSR >= 0.95, against about 0.5% with the nominal N (Phase 11 quant review);
* it is not monotone in the trials tried: adding near-copies of one plan LOWERS it (100
  independent plans score about 95; adding 300 near-copies of one of them, about 1.8), so a
  search that tries more variants would be rewarded with a lower bar;
* with ``M`` plans over ``T`` rows the noise eigenvalues spread (Marchenko-Pastur), so even
  ``M`` independent plans score about ``M / (1 + (M - 1) / T)``.

:func:`null_participation_ratio` gives the reference point: the same ratio after each column
is shuffled in time within its own observed rows (cross-plan correlation destroyed, each
plan's values and missing-data pattern kept). Read ``N_eff`` against that null, not against
``M``.

Inputs: a ``T x M`` matrix of per-period returns (rows = timestamps on a common grid, e.g.
daily; columns = plans), ``NaN`` where a plan has no observation (not yet started, a gap, or a
different symbol's calendar). Correlations are pairwise-complete Pearson.

Choices that raise ``N_eff`` when data is missing:

* A pair with fewer than ``min_overlap`` common rows, or a zero-variance column on the overlap
  (e.g. a plan that never traded there), has an **unknown** correlation and is treated as
  **0** (independent).
* Plans that have no returns at all (failed or empty trials) cannot be correlated with
  anything and each count as **one full independent trial** (``n_without_returns``).
* Pairwise-complete correlation matrices need not be positive semi-definite; negative
  eigenvalues are clipped to 0 before the ratio rather than "repairing" the matrix, which
  would require choices that can move ``N_eff`` either way.
* The result is clamped to ``[min(1, n_plans), n_plans]`` so floating-point noise never lets
  ``N_eff`` exceed the nominal count.

Sampling noise: for ``M`` truly independent plans over ``T`` rows the sample correlations are
about ``N(0, 1/T)``, so the expected ratio is about ``M / (1 + (M - 1) / T)``, below ``M``,
and the gap grows with ``M / T``: 3,000 independent plans over 3,000 daily rows give about
1,500, not 3,000 (the Marchenko-Pastur spread of noise eigenvalues). Callers report ``M``,
``T`` and :func:`null_participation_ratio` beside ``N_eff``.

Pure numpy; no I/O. Results are historical, not forward-looking.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "EffectiveN",
    "effective_n_trials",
    "null_participation_ratio",
    "pairwise_correlation",
    "participation_ratio",
]

# Column blocks for the masked matrix products: memory per product is _CHUNK x M floats.
_CHUNK = 512
# A column whose centred sum of squares on an overlap is below this share of its raw centred
# sum of squares is constant there (the one-pass variance carries ~n * eps relative error).
_ZERO_VAR_REL = 1e-10


@dataclass(frozen=True)
class EffectiveN:
    n_effective: float  # participation_ratio(...) + n_without_returns
    n_plans: int  # n_with_returns + n_without_returns
    n_with_returns: int
    n_without_returns: int
    n_unknown_pairs: int  # off-diagonal pairs (i < j) whose correlation was NaN


def _check_matrix(matrix: np.ndarray) -> np.ndarray:
    mat = np.asarray(matrix, dtype=float)
    if mat.ndim != 2:
        raise ValueError(f"matrix must be 2-D (T rows x M plans), got shape {mat.shape}")
    return mat


def _check_min_overlap(min_overlap: int) -> int:
    if isinstance(min_overlap, bool) or not isinstance(min_overlap, int | np.integer):
        raise ValueError(f"min_overlap must be an int >= 2, got {min_overlap!r}")
    if min_overlap < 2:
        raise ValueError(
            f"min_overlap must be >= 2 (a correlation needs 2 rows), got {min_overlap}"
        )
    return int(min_overlap)


def pairwise_correlation(matrix: np.ndarray, *, min_overlap: int) -> np.ndarray:
    """Pairwise-complete Pearson correlation of the columns of a ``T x M`` matrix.

    Each pair uses only the rows where **both** columns are finite (non-finite = no
    observation). A pair with fewer than ``min_overlap`` such rows, or where either column is
    constant on them, is ``NaN``. The diagonal is ``1.0`` for every column (even a constant or
    empty one); the result is symmetric and clipped to ``[-1, 1]``.

    Vectorised with masked matrix products (column blocks of 512), so ``M`` in the thousands
    costs a few BLAS calls rather than an ``O(M^2)`` Python loop. Columns are centred by their
    own finite mean first, which leaves every pairwise correlation unchanged and limits
    cancellation in the one-pass sums.
    """
    mat = _check_matrix(matrix)
    min_overlap = _check_min_overlap(min_overlap)
    m = mat.shape[1]
    corr = np.full((m, m), np.nan)
    if m == 0:
        return corr
    finite = np.isfinite(mat)
    f = finite.astype(float)
    counts = f.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        col_mean = np.where(counts > 0, np.where(finite, mat, 0.0).sum(axis=0) / counts, 0.0)
    x = np.where(finite, mat - col_mean, 0.0)  # centred, 0 where missing
    x2 = x * x
    for lo in range(0, m, _CHUNK):
        hi = min(m, lo + _CHUNK)
        fi, xi, x2i = f[:, lo:hi], x[:, lo:hi], x2[:, lo:hi]
        n = fi.T @ f  # common rows (exact small integers)
        s_i = xi.T @ f  # sum of x_i over the overlap with j
        s_j = fi.T @ x  # sum of x_j over the overlap with i
        q_i = x2i.T @ f
        q_j = fi.T @ x2
        c_ij = xi.T @ x
        with np.errstate(invalid="ignore", divide="ignore"):
            n_safe = np.where(n > 0, n, 1.0)
            var_i = q_i - s_i * s_i / n_safe
            var_j = q_j - s_j * s_j / n_safe
            cov = c_ij - s_i * s_j / n_safe
            ok = (
                (n >= min_overlap)
                & (var_i > _ZERO_VAR_REL * q_i)
                & (var_j > _ZERO_VAR_REL * q_j)
                & (var_i > 0)
                & (var_j > 0)
            )
            block = np.where(ok, cov / np.sqrt(np.where(ok, var_i * var_j, 1.0)), np.nan)
        corr[lo:hi] = np.clip(block, -1.0, 1.0)
    # Exact symmetry: keep the upper triangle and mirror it.
    upper = np.triu(np.ones((m, m), dtype=bool), k=1)
    corr = np.where(upper, corr, corr.T)
    np.fill_diagonal(corr, 1.0)
    return corr


def participation_ratio(corr: np.ndarray) -> float:
    """Eigenvalue participation ratio ``(sum l)^2 / sum(l^2)`` of a correlation matrix.

    ``NaN`` off-diagonal entries are treated as 0 (unknown = independent, the conservative
    choice: it raises the ratio) and a ``NaN`` diagonal entry as 1. The matrix is symmetrised,
    and negative eigenvalues (possible for pairwise-complete matrices) are clipped to 0.
    ``0.0`` for a 0 x 0 matrix, ``1.0`` for 1 x 1; otherwise in ``[1, M]``.
    """
    c = np.asarray(corr, dtype=float)
    if c.ndim != 2 or c.shape[0] != c.shape[1]:
        raise ValueError(f"corr must be a square 2-D matrix, got shape {c.shape}")
    m = c.shape[0]
    if m == 0:
        return 0.0
    if m == 1:
        return 1.0
    c = np.where(np.isnan(c), 0.0, c)
    diag = np.diag(np.asarray(corr, dtype=float))
    np.fill_diagonal(c, np.where(np.isnan(diag), 1.0, diag))
    if not np.all(np.isfinite(c)):
        raise ValueError("corr contains inf; correlations must be finite or NaN")
    c = 0.5 * (c + c.T)
    eig = np.clip(np.linalg.eigvalsh(c), 0.0, None)
    total_sq = float(np.sum(eig * eig))
    if total_sq == 0.0:
        return 0.0
    pr = float(np.sum(eig)) ** 2 / total_sq
    return float(min(max(pr, 1.0), float(m)))


def effective_n_trials(
    matrix: np.ndarray, *, min_overlap: int, n_without_returns: int = 0
) -> EffectiveN:
    """Effective number of independent trials for ``M`` plans' returns plus
    ``n_without_returns`` plans that have none: a diagnostic, never the DSR's N (module
    docstring).

    ``n_effective = participation_ratio(pairwise_correlation(matrix)) + n_without_returns``,
    always within ``[min(1, n_plans), n_plans]``. Callers pass the design-window returns of
    the ledgered plans of one run or of every run. See the module docstring for the choices
    made on missing data.
    """
    mat = _check_matrix(matrix)
    if (
        isinstance(n_without_returns, bool)
        or not isinstance(n_without_returns, int | np.integer)
        or n_without_returns < 0
    ):
        raise ValueError(f"n_without_returns must be an int >= 0, got {n_without_returns!r}")
    n_without = int(n_without_returns)
    corr = pairwise_correlation(mat, min_overlap=min_overlap)
    m = corr.shape[0]
    n_unknown = int(np.isnan(corr[np.triu_indices(m, k=1)]).sum()) if m > 1 else 0
    pr = participation_ratio(corr)
    n_plans = m + n_without
    n_eff = min(max(pr + n_without, float(min(1, n_plans))), float(n_plans))
    return EffectiveN(
        n_effective=float(n_eff),
        n_plans=n_plans,
        n_with_returns=m,
        n_without_returns=n_without,
        n_unknown_pairs=n_unknown,
    )


def null_participation_ratio(matrix: np.ndarray, *, min_overlap: int, seed: int) -> float:
    """Participation ratio of ``matrix`` after a time shuffle: each column's finite values are
    permuted among that column's own finite rows (``numpy.random.default_rng(seed)``, columns
    in order), so every plan keeps its values and its missing-data pattern while any
    correlation BETWEEN plans is destroyed. The result is what ``M`` independent plans with
    these row patterns score (about ``M / (1 + (M - 1) / T)`` on full columns: the
    Marchenko-Pastur bias), the reference for reading :func:`participation_ratio` of the
    unshuffled matrix. Pairs are scored exactly as :func:`pairwise_correlation` scores them.

    ``0.0`` for no columns, ``1.0`` for one. Raises ``ValueError`` for a non-2-D matrix, a bad
    ``min_overlap`` or a ``seed`` that is not a non-negative int."""
    mat = _check_matrix(matrix)
    min_overlap = _check_min_overlap(min_overlap)
    if isinstance(seed, bool) or not isinstance(seed, int | np.integer) or seed < 0:
        raise ValueError(f"seed must be an int >= 0, got {seed!r}")
    rng = np.random.default_rng(int(seed))
    shuffled = np.array(mat, dtype=float, copy=True)
    for j in range(shuffled.shape[1]):
        rows = np.flatnonzero(np.isfinite(shuffled[:, j]))
        if rows.size > 1:
            shuffled[rows, j] = rng.permutation(shuffled[rows, j])
    return participation_ratio(pairwise_correlation(shuffled, min_overlap=min_overlap))
