"""Effective-N DIAGNOSTIC from the ledger (PLAN.md Phase 11, ``effective_n_trials``).

The Deflated Sharpe Ratio deflates with the NOMINAL ``N``: distinct plans
(:meth:`lab.ledger.Ledger.count_distinct_plans_research` across every run, or
:meth:`lab.ledger.Ledger.count_distinct_plans` for one run). This module reports, as
information only, how alike those plans' returns are: the eigenvalue participation ratio
(:func:`lab.stats.effective_n.effective_n_trials`) of the return correlation matrix of the SAME
plan set, beside its time-shuffled null (:func:`lab.stats.effective_n.null_participation_ratio`,
seed :data:`TRIAL_COUNT_NULL_SEED`): what as many independent plans on the same days would
score. It is never the DSR's N (see :mod:`lab.stats.effective_n` for why: the shared market
factor is counted twice, near-copies lower it, and ``M / T`` biases it down).

How each plan becomes one column:

* its trial is :meth:`lab.ledger.Ledger.plan_return_trials`: the latest ok trial with stored
  returns from a selection source, design window preferred, then walk-forward train / test, then
  custom; ``holdout`` rows and any window ending after ``cfg.splits.holdout_start`` are never
  read;
* its bar open times are rebuilt (the ledger stores return values only). A series that fills
  every interval-aligned slot of its ``[window_start, window_end)`` exactly has those slots as
  its bar times (no candles needed; this covers the portfolio ``universe:<QUOTE>`` plans, whose
  rows are one per calendar day). Any other series is rebuilt from the candles the way the
  runner built its bars (:func:`lab.research.family.design_bar_index`); a rebuild whose length
  differs from the stored series (the candles changed since) is unusable;
* its simple returns become log returns summed onto a daily UTC grid
  (:data:`TRIAL_COUNT_GRID_SECONDS`) with :func:`lab.research.family._to_grid`'s rules
  (computed once per bar-time vector and shared by the plans on it): a bar counts
  only when its predecessor is present and a day counts only when all its bars do, so a gap is
  a gap, never a zero. A daily grid aligns every bar size from 1m to 1d (crypto trades 24/7);
* the columns form one ``T x M`` matrix over the union of their days (NaN = no data).

Missing data (each choice can only RAISE the effective ``N``):

* a plan with no usable non-holdout returns (only error trials, non-finite or <= -100% returns,
  a bar size over one day, bar times that cannot be rebuilt or no longer match) is one
  independent trial;
* a pair with fewer than :data:`TRIAL_COUNT_MIN_OVERLAP_DAYS` common complete days is unknown
  and counted as independent (correlation 0).

So ``effective <= nominal`` always (the participation ratio of ``M`` columns is at most ``M``).
The null is computed on the same matrix and also counts the plans without returns as one each.

Results are cached per (ledger file, newest trial id, run scope, design split, loader): the
ledger is append-only, so a count is valid while no trial was added.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from lab.config import LabConfig
from lab.ledger import SELECTION_SOURCES, Ledger
from lab.research.family import design_bar_index
from lab.stats.effective_n import effective_n_trials, null_participation_ratio
from lab.types import INTERVAL_SECONDS

__all__ = [
    "TRIAL_COUNT_GRID_SECONDS",
    "TRIAL_COUNT_MIN_OVERLAP_DAYS",
    "TRIAL_COUNT_NULL_SEED",
    "TrialCount",
    "clear_trial_count_cache",
    "trial_counts",
]

_log = logging.getLogger(__name__)

# Daily UTC grid: crypto trades 24/7, and a day is a whole number of bars for every interval
# from 1m to 1d, so plans on different bar sizes line up by timestamp.
TRIAL_COUNT_GRID_SECONDS = 86_400
# Two months of daily returns: a correlation's standard error is ~1/sqrt(60) ~ 0.13, enough to
# tell a near-copy from an unrelated plan. Pairs with less overlap are unknown and counted as
# independent (which raises effective N, never lowers the bar).
TRIAL_COUNT_MIN_OVERLAP_DAYS = 60
# Seed of the time-shuffled null (one fixed permutation per column): the null is a reference
# point, not a test statistic, so one fixed draw keeps reports reproducible and cacheable.
TRIAL_COUNT_NULL_SEED = 0

_CACHE_MAX = 32
_ALL_RUNS = "all runs"

# A loader as the runner uses it: (symbol, interval, end) -> candles with bars < end.
Loader = Callable[[str, str, pd.Timestamp], pd.DataFrame]


@dataclass(frozen=True)
class TrialCount:
    """Nominal and effective trial counts for one scope (module docstring)."""

    scope: str  # "all runs" or the run_id
    nominal: int
    effective: float  # participation ratio + plans without returns: a diagnostic, never DSR N
    # the same ratio after shuffling each plan's daily returns in time within its own observed
    # days (seed TRIAL_COUNT_NULL_SEED), + plans without returns: what independent plans score
    effective_null: float
    rows: int  # T: days of the matrix (union of the plans' complete days)
    n_with_returns: int  # M: columns of the matrix
    # no usable non-holdout returns, or bar times not rebuildable / not matching -> independent
    n_without_returns: int
    n_unknown_pairs: int  # pairs with too little overlap -> independent
    grid_seconds: int
    min_overlap_days: int
    basis: str  # one human sentence stating the method and the conservative choices


_CACHE: OrderedDict[tuple[Any, ...], TrialCount] = OrderedDict()


def clear_trial_count_cache() -> None:
    """Forget every cached count (tests; the cache is also bounded)."""
    _CACHE.clear()


def _candle_loader(cfg: LabConfig) -> Loader:
    """The default loader: the lab's Parquet candles only. Bar times are all this module needs;
    :func:`lab.runner.default_loader` returns the same bar index (it only adds ``drv_*``
    columns) but reads the derivatives archive on every first load (~2 s per series)."""
    from lab.data.store import load_candles  # lazy: keeps this module light to import

    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        return load_candles(symbol, interval, parquet_dir=cfg.parquet_dir, end=end)

    return load


def _contiguous_times(start: int, end: int, step: int, n: int) -> np.ndarray | None:
    """Bar times when ``n`` bars fill every ``step``-aligned slot of ``[start, end)``: then
    those slots ARE the bar times, whatever the candles look like today."""
    if start % step != 0:
        return None
    slots = -(-(end - start) // step)
    if n != slots:
        return None
    return start + step * np.arange(n, dtype=np.int64)


@dataclass(frozen=True)
class _DailyGrid:
    """:func:`lab.research.family._to_grid`'s bucketing of one bar-time vector onto the daily
    grid, computed once and shared by every plan on the same bars."""

    valid: np.ndarray  # bar counts (its predecessor is exactly one step earlier)
    inverse: np.ndarray  # bar -> day bucket
    starts: np.ndarray  # every day bucket start
    keep: np.ndarray  # day bucket is complete

    @classmethod
    def build(cls, ts: np.ndarray, step: int) -> _DailyGrid:
        valid = np.zeros(ts.size, dtype=bool)
        if ts.size > 1:
            valid[1:] = np.diff(ts) == step
        bucket = (ts // TRIAL_COUNT_GRID_SECONDS) * TRIAL_COUNT_GRID_SECONDS
        starts, inverse = np.unique(bucket, return_inverse=True)
        counts = np.bincount(inverse, weights=valid.astype(np.float64), minlength=starts.size)
        keep = counts == float(TRIAL_COUNT_GRID_SECONDS // step)
        return cls(valid=valid, inverse=inverse, starts=starts, keep=keep)

    def apply(self, logret: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``(complete day starts, summed log returns)``, as ``_to_grid`` returns them."""
        weights = np.where(self.valid, logret, 0.0)
        sums = np.bincount(self.inverse, weights=weights, minlength=self.starts.size)
        return self.starts[self.keep], sums[self.keep]


def _bar_times(
    meta: dict[str, Any],
    step: int,
    cfg: LabConfig,
    loader: Loader,
    history: dict[tuple[str, str], np.ndarray | str],
) -> np.ndarray | None:
    """The trial's bar open times (module docstring), or None when they cannot be rebuilt or
    no longer match the stored series' length."""
    start, end, n = meta["window_start"], meta["window_end"], meta["n_returns"]
    ts = _contiguous_times(start, end, step, n)
    if ts is not None:
        return ts
    try:
        ts = design_bar_index(
            loader, cfg, meta["symbol"], meta["interval"], start, end, _history=history
        )
    except Exception as exc:  # no candles / unreadable: the plan counts as independent
        _log.debug("trial count: no bar times for %s %s: %s", meta["symbol"], meta["interval"], exc)
        return None
    return ts if ts.size == n else None


def _plan_column(
    ledger: Ledger,
    meta: dict[str, Any],
    cfg: LabConfig,
    loader: Loader,
    history: dict[tuple[str, str], np.ndarray | str],
    grids: dict[tuple[str, str, int, int, int], _DailyGrid | None],
) -> tuple[np.ndarray, np.ndarray] | None:
    """``(day starts, daily log-return sums)`` of one plan, or None when unusable."""
    step = INTERVAL_SECONDS.get(meta["interval"])
    if step is None or TRIAL_COUNT_GRID_SECONDS % step != 0:
        return None
    n = meta["n_returns"]
    key = (meta["symbol"], meta["interval"], meta["window_start"], meta["window_end"], n)
    if key not in grids:
        ts = _bar_times(meta, step, cfg, loader, history)
        grids[key] = None if ts is None else _DailyGrid.build(ts, step)
    grid = grids[key]
    if grid is None:
        return None
    returns = ledger.trial_returns(meta["trial_id"])
    if returns is None or returns.size != n:
        return None
    if not np.isfinite(returns).all() or (returns <= -1.0).any():
        return None
    days, sums = grid.apply(np.log1p(returns))
    if days.size == 0:
        return None
    return days, sums


def _basis(n_without: int, nominal: int) -> str:
    return (
        "Effective N = eigenvalue participation ratio of the pairwise Pearson correlation of "
        "each plan's latest pre-holdout returns (design window preferred), summed to daily UTC "
        "log returns on complete days; pairs with fewer than "
        f"{TRIAL_COUNT_MIN_OVERLAP_DAYS} common days and the {n_without} of {nominal} plans "
        "without usable returns count as independent. The null shuffles each plan's days in "
        f"time (seed {TRIAL_COUNT_NULL_SEED}) and is scored the same way."
    )


def trial_counts(
    ledger: Ledger,
    cfg: LabConfig,
    *,
    run_id: str | None = None,
    loader: Loader | None = None,
) -> TrialCount:
    """Nominal trial count and the effective-N diagnostic with its time-shuffled null, across
    every run (``run_id`` None) or for one run (module docstring). ``loader`` rebuilds bar
    times of series with gaps (default: the lab's Parquet candles for ``cfg``). Cached per
    ledger state, so only the first call after a new trial pays for the matrix work."""
    if run_id is not None and (not isinstance(run_id, str) or not run_id):
        raise ValueError(f"run_id must be a non-empty string or None, got {run_id!r}")
    scope = _ALL_RUNS if run_id is None else run_id
    key: tuple[Any, ...] | None = None
    if ledger.path is not None:
        key = (
            str(ledger.path),
            ledger.max_trial_id(),
            run_id,
            scope,
            cfg.splits.holdout_start.isoformat(),
            id(loader) if loader is not None else str(cfg.parquet_dir),
        )
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit

    if run_id is None:
        nominal = ledger.count_distinct_plans_research(sources=SELECTION_SOURCES)
    else:
        nominal = ledger.count_distinct_plans(run_id, sources=SELECTION_SOURCES)
    meta, missing = ledger.plan_return_trials(
        sources=SELECTION_SOURCES, run_id=run_id, holdout_start=cfg.splits.holdout_start
    )
    if len(meta) + len(missing) != nominal:  # pragma: no cover - the reader's contract
        raise RuntimeError(
            f"plan set mismatch: {len(meta)} with returns + {len(missing)} without != {nominal}"
        )

    load = loader if loader is not None else _candle_loader(cfg)
    history: dict[tuple[str, str], np.ndarray | str] = {}
    grids: dict[tuple[str, str, int, int, int], _DailyGrid | None] = {}
    columns: list[tuple[np.ndarray, np.ndarray]] = []
    for m in meta:
        col = _plan_column(ledger, m, cfg, load, history, grids)
        if col is not None:
            columns.append(col)
    n_without = nominal - len(columns)

    if columns:
        days = np.unique(np.concatenate([d for d, _ in columns]))
        matrix = np.full((days.size, len(columns)), np.nan)
        for j, (d, v) in enumerate(columns):
            matrix[np.searchsorted(days, d), j] = v
        result = effective_n_trials(
            matrix, min_overlap=TRIAL_COUNT_MIN_OVERLAP_DAYS, n_without_returns=n_without
        )
        effective = float(result.n_effective)
        n_with, n_unknown = int(result.n_with_returns), int(result.n_unknown_pairs)
        n_without = int(result.n_without_returns)
        null = null_participation_ratio(
            matrix, min_overlap=TRIAL_COUNT_MIN_OVERLAP_DAYS, seed=TRIAL_COUNT_NULL_SEED
        )
        effective_null = min(null + n_without, float(nominal))
        rows = int(days.size)
    else:  # nothing to correlate: every plan is its own independent trial
        effective, n_with, n_unknown = float(n_without), 0, 0
        effective_null, rows = float(n_without), 0
    count = TrialCount(
        scope=scope,
        nominal=nominal,
        effective=effective,
        effective_null=effective_null,
        rows=rows,
        n_with_returns=n_with,
        n_without_returns=n_without,
        n_unknown_pairs=n_unknown,
        grid_seconds=TRIAL_COUNT_GRID_SECONDS,
        min_overlap_days=TRIAL_COUNT_MIN_OVERLAP_DAYS,
        basis=_basis(n_without, nominal),
    )
    if key is not None:
        _CACHE[key] = count
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return count
