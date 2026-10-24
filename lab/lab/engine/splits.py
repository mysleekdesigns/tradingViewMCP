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

Results are historical and not forward-looking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
