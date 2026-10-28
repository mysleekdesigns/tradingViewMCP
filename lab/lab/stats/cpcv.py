"""Combinatorial Purged Cross-Validation paths for a parameter search (PLAN.md Phase 11).

An *additional* validator beside walk-forward (López de Prado 2018, *Advances in Financial
Machine Learning*, ch. 12.4). Input: the ``T x M`` per-bar net returns of the ``M``
configurations a sweep tried, all on the same design-window bars. For each of the
``C(n_groups, n_test_groups)`` splits from ``lab.engine.splits.cpcv_splits``:

1. choose the configuration with the best per-bar Sharpe (mean / std, ddof=1) on the split's
   **train bars only** (test, purged and embargoed bars are never looked at; ties go to the
   lowest column; a zero-variance column ranks below every finite Sharpe);
2. take that configuration's returns on the split's test bars.

Each of the ``C(n_groups - 1, n_test_groups - 1)`` paths then concatenates, group by group in
time order, the test returns of the split ``cpcv_path_assignment`` names for that group, so
every path covers every bar exactly once and is wholly out-of-sample. The result is a
*distribution* of out-of-sample Sharpes for the search procedure instead of the single path
walk-forward gives.

Paths are views of the same ``M`` configurations, not new trials: DSR still deflates by the
configurations searched, never by the number of paths.

Defaults (AFML ch. 7 / ch. 12 conventions):

* ``CPCV_GROUPS = 6``, ``CPCV_TEST_GROUPS = 2``: 15 splits and 5 paths, each train set
  two-thirds of the window (the book's worked example).
* ``CPCV_PURGE_BARS = 1``: a bar return has a one-bar label horizon (signal at bar t's close,
  fill at bar t+1's open), so only the bar just before a test block shares an outcome with it.
* ``CPCV_EMBARGO_FRACTION = 0.01``: AFML's suggested embargo of about 1% of the sample after
  each test block, rounded **up** to whole bars (``embargo_bars = ceil(0.01 * T)``) so it is
  never zero; ``purgedcv`` rounds down.

Annualised with ``sqrt(bars_per_year)`` (365-day crypto year). Pure numpy; no I/O. Results are
historical, not forward-looking.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from lab.engine.splits import cpcv_groups, cpcv_path_assignment, cpcv_splits

__all__ = [
    "CPCV_EMBARGO_FRACTION",
    "CPCV_GROUPS",
    "CPCV_PURGE_BARS",
    "CPCV_TEST_GROUPS",
    "CpcvResult",
    "cpcv_paths",
]

CPCV_GROUPS = 6  # AFML 12.4 worked example: N = 6 groups
CPCV_TEST_GROUPS = 2  # k = 2 -> C(6, 2) = 15 splits, C(5, 1) = 5 paths
CPCV_PURGE_BARS = 1  # bar returns have a one-bar label horizon
CPCV_EMBARGO_FRACTION = 0.01  # AFML 7.4.2: ~1% of the sample after each test block

_QUANTILES = (
    ("min", 0.0),
    ("p05", 0.05),
    ("p25", 0.25),
    ("median", 0.5),
    ("p75", 0.75),
    ("p95", 0.95),
    ("max", 1.0),
)


@dataclass(frozen=True)
class CpcvResult:
    n_groups: int
    n_test_groups: int
    n_splits: int
    n_paths: int
    n_configs: int
    n_bars: int
    purge_bars: int
    embargo_bars: int
    chosen: tuple[int, ...]  # per split: column with the best TRAIN Sharpe (ties -> lowest)
    path_sharpes: tuple[float, ...]  # annualised OOS Sharpe per path; NaN if a path's std is 0
    quantiles: dict[str, float]  # min/p05/p25/median/p75/p95/max over finite path Sharpes
    share_positive: float  # share of finite path Sharpes > 0


def _sharpe_columns(block: np.ndarray) -> np.ndarray:
    """Per-bar Sharpe (ddof=1) of every column; NaN where a column is constant."""
    mean = block.mean(axis=0)
    std = block.std(axis=0, ddof=1)
    constant = np.ptp(block, axis=0) == 0
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(constant | (std == 0), np.nan, mean / np.where(std > 0, std, 1.0))


def _annual_sharpe(path: np.ndarray, bars_per_year: float) -> float:
    if np.ptp(path) == 0:
        return float("nan")
    std = float(path.std(ddof=1))
    if std == 0 or not math.isfinite(std):
        return float("nan")
    return float(path.mean() / std * math.sqrt(bars_per_year))


def cpcv_paths(
    returns: np.ndarray,
    *,
    bars_per_year: float,
    n_groups: int = CPCV_GROUPS,
    n_test_groups: int = CPCV_TEST_GROUPS,
    purge_bars: int = CPCV_PURGE_BARS,
    embargo_bars: int | None = None,
) -> CpcvResult:
    """CPCV out-of-sample path Sharpes of the best-in-train selection over ``M`` configs.

    ``returns`` is ``T x M`` (rows = bars in time order, columns = configurations), finite.
    ``embargo_bars`` defaults to ``ceil(CPCV_EMBARGO_FRACTION * T)``. Selection uses train bars
    only (see the module docstring). Raises ``ValueError`` for a non-2-D or non-finite matrix,
    ``M < 1``, invalid group counts, a non-positive ``bars_per_year``, or ``T`` too short: the
    smallest group must keep at least 2 bars after a purge before it and an embargo after it
    (``group >= purge_bars + embargo_bars + 2``), so every train set has data from every
    training group.
    """
    mat = np.asarray(returns, dtype=float)
    if mat.ndim != 2:
        raise ValueError(f"returns must be 2-D (T bars x M configs), got shape {mat.shape}")
    n_bars, n_configs = mat.shape
    if n_configs < 1:
        raise ValueError("returns needs M >= 1 configuration column, got 0")
    if not np.all(np.isfinite(mat)):
        raise ValueError("returns contains NaN or inf; per-bar returns must be finite")
    bpy = float(bars_per_year)
    if not math.isfinite(bpy) or bpy <= 0:
        raise ValueError(f"bars_per_year must be a positive finite number, got {bars_per_year!r}")
    if embargo_bars is None:
        embargo_bars = math.ceil(CPCV_EMBARGO_FRACTION * n_bars)
    splits = cpcv_splits(
        n_bars, n_groups, n_test_groups, purge_bars=purge_bars, embargo_bars=embargo_bars
    )
    groups = cpcv_groups(n_bars, n_groups)
    smallest = min(end - start for start, end in groups)
    need = purge_bars + embargo_bars + 2
    if smallest < need:
        raise ValueError(
            f"returns has T={n_bars} bars: the smallest of n_groups={n_groups} groups has "
            f"{smallest} bars, fewer than purge_bars + embargo_bars + 2 = {need}; "
            "use more bars or fewer groups"
        )

    chosen: list[int] = []
    for split in splits:
        train_sr = _sharpe_columns(mat[split.train])
        ranked = np.where(np.isnan(train_sr), -np.inf, train_sr)
        chosen.append(int(np.argmax(ranked)))  # first index on ties

    assignment = cpcv_path_assignment(n_groups, n_test_groups)
    path_sharpes: list[float] = []
    for path in assignment:
        pieces = [
            mat[start:end, chosen[split_index]]
            for (start, end), split_index in zip(groups, path, strict=True)
        ]
        path_sharpes.append(_annual_sharpe(np.concatenate(pieces), bpy))

    finite = np.array([s for s in path_sharpes if math.isfinite(s)], dtype=float)
    if finite.size:
        quantiles = {name: float(np.quantile(finite, q)) for name, q in _QUANTILES}
        share_positive = float(np.mean(finite > 0))
    else:
        quantiles = {name: float("nan") for name, _ in _QUANTILES}
        share_positive = float("nan")
    return CpcvResult(
        n_groups=int(n_groups),
        n_test_groups=int(n_test_groups),
        n_splits=len(splits),
        n_paths=len(assignment),
        n_configs=int(n_configs),
        n_bars=int(n_bars),
        purge_bars=int(purge_bars),
        embargo_bars=int(embargo_bars),
        chosen=tuple(chosen),
        path_sharpes=tuple(path_sharpes),
        quantiles=quantiles,
        share_positive=share_positive,
    )
