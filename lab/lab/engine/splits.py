"""Design / walk-forward / locked-holdout splits (PLAN.md Phase 7 How §4). Pure: no I/O.

* The **design window** is ``[design_start, holdout_start)``. All search and tuning happens here.
* **Walk-forward folds** live inside the design window: train on N calendar months, test on the
  next M, roll forward by ``step`` months. A fold whose test window would end after the design
  window is dropped, so validation never spills into the holdout.
* The **locked holdout** is ``[holdout_start, data_end)``. ``holdout_start`` is a fixed date from
  config so it never silently moves as data refreshes. Search/Optuna/tuning code calls
  :func:`assert_no_holdout` on its inputs; only ``holdout_check`` (budgeted in the ledger) reads
  the holdout.

All windows are half-open ``[start, end)`` on bar open times (tz-aware UTC).

**Combinatorial Purged Cross-Validation** (López de Prado 2018, *Advances in Financial Machine
Learning*, ch. 7 and ch. 12.4) is an *additional* validator beside walk-forward, index-based
(bar positions inside the design window, never dates): :func:`cpcv_groups` cuts the bars into
contiguous groups, :func:`cpcv_splits` builds the ``C(n_groups, n_test_groups)`` purged and
embargoed train/test masks, and :func:`cpcv_path_assignment` says which split supplies each
group of each of the ``C(n_groups - 1, n_test_groups - 1)`` out-of-sample paths. The path
returns themselves are assembled in ``lab.stats.cpcv``.

Results are historical and not forward-looking.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import comb
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from lab.types import WalkForwardFold, Window

if TYPE_CHECKING:  # type only: keep the engine free of the config module's file I/O
    from lab.config import SplitConfig


def _check_ts(value: object, name: str) -> pd.Timestamp:
    if not isinstance(value, pd.Timestamp):
        raise ValueError(f"{name} must be a pd.Timestamp, got {type(value).__name__}")
    if value.tzinfo is None:
        raise ValueError(f"{name} must be tz-aware UTC, got naive {value}")
    return value.tz_convert("UTC")


def _check_window(window: Window, name: str = "window") -> tuple[pd.Timestamp, pd.Timestamp]:
    start = _check_ts(window.start, f"{name}.start")
    end = _check_ts(window.end, f"{name}.end")
    if end <= start:
        raise ValueError(f"{name} is empty: end {end} <= start {start}")
    return start, end


def _check_index(candles: pd.DataFrame, name: str = "candles") -> pd.DatetimeIndex:
    idx = candles.index
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
        raise ValueError(f"{name} index must be a tz-aware UTC DatetimeIndex")
    return idx


def _check_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


def design_window(cfg: SplitConfig) -> Window:
    """``[design_start, holdout_start)`` with kind ``"design"``."""
    start = _check_ts(cfg.design_start, "design_start")
    end = _check_ts(cfg.holdout_start, "holdout_start")
    if end <= start:
        raise ValueError(f"design_start {start} must be before holdout_start {end}")
    return Window(start=start, end=end, kind="design")


def holdout_window(cfg: SplitConfig, data_end: pd.Timestamp) -> Window:
    """``[holdout_start, data_end)`` with kind ``"holdout"``.

    ``data_end`` is exclusive (e.g. the open time just after the last closed bar). Raises
    ``ValueError`` if it is not after ``holdout_start``.
    """
    start = _check_ts(cfg.holdout_start, "holdout_start")
    end = _check_ts(data_end, "data_end")
    if end <= start:
        raise ValueError(f"data_end {end} must be after holdout_start {start}; no holdout data")
    return Window(start=start, end=end, kind="holdout")


def walk_forward_folds(
    design: Window,
    *,
    train_months: int,
    test_months: int,
    step_months: int,
    anchored: bool,
) -> list[WalkForwardFold]:
    """Calendar-month walk-forward folds inside ``design``.

    Fold ``i`` tests on ``[start + train + i*step, start + train + i*step + test)`` months.
    Rolling folds train on the ``train_months`` before the test; anchored folds always train
    from ``design.start``. ``train.end == test.start``. Folds whose test window ends after
    ``design.end`` are dropped. Offsets are computed from ``design.start`` each time (not
    chained) so month-end dates don't drift.
    """
    start, end = _check_window(design, "design")
    train_months = _check_positive_int(train_months, "train_months")
    test_months = _check_positive_int(test_months, "test_months")
    step_months = _check_positive_int(step_months, "step_months")
    if not isinstance(anchored, bool):
        raise ValueError(f"anchored must be a bool, got {anchored!r}")

    def at(months: int) -> pd.Timestamp:
        return start + pd.DateOffset(months=months)

    folds: list[WalkForwardFold] = []
    i = 0
    while True:
        test_start_m = train_months + i * step_months
        test_end = at(test_start_m + test_months)
        if test_end > end:
            break
        test_start = at(test_start_m)
        train_start = start if anchored else at(test_start_m - train_months)
        folds.append(
            WalkForwardFold(
                index=i,
                train=Window(start=train_start, end=test_start, kind="walk_forward_train"),
                test=Window(start=test_start, end=test_end, kind="walk_forward_test"),
            )
        )
        i += 1
    if not folds:
        raise ValueError(
            f"no walk-forward fold fits in design window [{start}, {end}): "
            f"train_months={train_months} + test_months={test_months} months exceed it"
        )
    return folds


def slice_window(candles: pd.DataFrame, window: Window) -> pd.DataFrame:
    """Rows with ``window.start <= time < window.end`` (half-open)."""
    start, end = _check_window(window)
    idx = _check_index(candles)
    return candles.loc[(idx >= start) & (idx < end)]


def history_through(candles: pd.DataFrame, window: Window) -> pd.DataFrame:
    """Every row with ``time < window.end``, including history before ``window.start``.

    Compute features on this (so indicators are warmed up at the window start), then trim
    with :func:`slice_window`. Nothing at or after ``window.end`` is visible.
    """
    _, end = _check_window(window)
    idx = _check_index(candles)
    return candles.loc[idx < end]


def assert_no_holdout(candles: pd.DataFrame, cfg: SplitConfig) -> None:
    """Raise ``ValueError`` if any row is at or after ``holdout_start``.

    Search, Optuna, and tuning code call this on every frame they receive.
    """
    holdout_start = _check_ts(cfg.holdout_start, "holdout_start")
    idx = _check_index(candles)
    leaked = idx >= holdout_start
    if leaked.any():
        first = idx[leaked][0]
        raise ValueError(
            f"candles reach into the locked holdout: {int(leaked.sum())} rows at or after "
            f"holdout_start {holdout_start.isoformat()} (first {first.isoformat()}); "
            "search and tuning must use design_view()"
        )


def design_view(candles: pd.DataFrame, cfg: SplitConfig) -> pd.DataFrame:
    """Rows with ``time < holdout_start``. Earlier history (before ``design_start``) is kept
    for indicator warm-up."""
    holdout_start = _check_ts(cfg.holdout_start, "holdout_start")
    idx = _check_index(candles)
    return candles.loc[idx < holdout_start]


# ------------------------------------------------------------------ CPCV (index-based)


@dataclass(frozen=True)
class CpcvSplit:
    """One CPCV split: boolean masks over the ``n_bars`` design-window bars."""

    index: int  # position in itertools.combinations order
    test_groups: tuple[int, ...]
    train: np.ndarray  # bool, length n_bars: bars a selection may look at
    test: np.ndarray  # bool, length n_bars: the out-of-sample bars of this split


def _check_count(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | np.integer) or value < minimum:
        raise ValueError(f"{name} must be an int >= {minimum}, got {value!r}")
    return int(value)


def cpcv_groups(n_bars: int, n_groups: int) -> list[tuple[int, int]]:
    """Contiguous ``[start, end)`` bar blocks covering ``range(n_bars)``.

    Each group has ``n_bars // n_groups`` bars and the first ``n_bars % n_groups`` groups get
    one extra (the ``purgedcv`` / scikit-learn ``KFold`` convention). Raises ``ValueError``
    unless ``n_groups >= 2`` and ``n_bars >= n_groups``.
    """
    n_groups = _check_count(n_groups, "n_groups", 2)
    n_bars = _check_count(n_bars, "n_bars", 1)
    if n_bars < n_groups:
        raise ValueError(f"n_bars={n_bars} must be >= n_groups={n_groups} (no empty groups)")
    size, remainder = divmod(n_bars, n_groups)
    out: list[tuple[int, int]] = []
    cursor = 0
    for g in range(n_groups):
        length = size + (1 if g < remainder else 0)
        out.append((cursor, cursor + length))
        cursor += length
    return out


def _check_test_groups(n_groups: int, n_test_groups: int) -> tuple[int, int]:
    n_groups = _check_count(n_groups, "n_groups", 2)
    n_test_groups = _check_count(n_test_groups, "n_test_groups", 1)
    if n_test_groups >= n_groups:
        raise ValueError(
            f"n_test_groups must be in [1, n_groups - 1] = [1, {n_groups - 1}], got {n_test_groups}"
        )
    return n_groups, n_test_groups


def cpcv_splits(
    n_bars: int,
    n_groups: int,
    n_test_groups: int,
    *,
    purge_bars: int,
    embargo_bars: int,
) -> list[CpcvSplit]:
    """The ``C(n_groups, n_test_groups)`` CPCV splits, in ``itertools.combinations`` order.

    ``test`` marks the bars of the chosen groups. ``train`` is every other bar except, for each
    contiguous test block (adjacent test groups merge into one block):

    * **purge**: the ``purge_bars`` bars immediately *before* the block, whose outcomes overlap
      the block's first bars (AFML 7.4.1);
    * **embargo**: the ``embargo_bars`` bars immediately *after* the block, which can still
      carry information from it through serial correlation (AFML 7.4.2).

    Blocks are clipped at the ends of the series. With ``embargo_bars >= purge_bars`` this is
    exactly ``purgedcv.CombinatorialPurgedCV`` with a ``purge_bars + 1``-bar label horizon and
    ``embargo_observations=embargo_bars`` (its label-overlap purge also removes ``purge_bars``
    bars after a block, which the embargo already covers); tested in
    ``tests/test_stats_crosscheck.py``.
    """
    n_groups, n_test_groups = _check_test_groups(n_groups, n_test_groups)
    purge_bars = _check_count(purge_bars, "purge_bars", 0)
    embargo_bars = _check_count(embargo_bars, "embargo_bars", 0)
    groups = cpcv_groups(n_bars, n_groups)
    splits: list[CpcvSplit] = []
    for index, combo in enumerate(combinations(range(n_groups), n_test_groups)):
        test = np.zeros(n_bars, dtype=bool)
        for g in combo:
            test[groups[g][0] : groups[g][1]] = True
        train = ~test
        # Contiguous test blocks: runs of adjacent group indices.
        runs: list[list[int]] = []
        for g in combo:
            if runs and runs[-1][-1] == g - 1:
                runs[-1].append(g)
            else:
                runs.append([g])
        for run in runs:
            start, end = groups[run[0]][0], groups[run[-1]][1]
            train[max(0, start - purge_bars) : start] = False
            train[end : min(n_bars, end + embargo_bars)] = False
        splits.append(CpcvSplit(index=index, test_groups=combo, train=train, test=test))
    return splits


def cpcv_path_assignment(n_groups: int, n_test_groups: int) -> list[list[int]]:
    """For each of the ``C(n_groups - 1, n_test_groups - 1)`` paths, the split index that
    supplies each group: ``out[p][g]`` is the split whose test set fills group ``g`` of path
    ``p``.

    Every group is a test group in exactly ``C(n_groups - 1, n_test_groups - 1)`` splits; its
    ``j``-th such split (in :func:`cpcv_splits` order) fills path ``j`` (AFML 12.4, the
    ``purgedcv.reconstruct_paths`` rule). So every path covers every group exactly once.
    """
    n_groups, n_test_groups = _check_test_groups(n_groups, n_test_groups)
    n_paths = comb(n_groups - 1, n_test_groups - 1)
    per_group: list[list[int]] = [[] for _ in range(n_groups)]
    for index, combo in enumerate(combinations(range(n_groups), n_test_groups)):
        for g in combo:
            per_group[g].append(index)
    return [[per_group[g][p] for g in range(n_groups)] for p in range(n_paths)]
