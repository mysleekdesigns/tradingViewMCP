"""Design / walk-forward / locked-holdout splits. Boundaries are hand-checked calendar dates."""

from __future__ import annotations

from itertools import pairwise

import pandas as pd
import pytest

from lab.config import SplitConfig
from lab.engine.splits import (
    assert_no_holdout,
    design_view,
    design_window,
    history_through,
    holdout_window,
    slice_window,
    walk_forward_folds,
)
from lab.types import Window
from tests.synthetic import make_candles


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


# The values in config/lab.toml, written out so the test does not read the file.
CFG = SplitConfig(
    design_start=ts("2019-01-01"),
    holdout_start=ts("2025-10-01"),
    holdout_budget=3,
    wf_train_months=24,
    wf_test_months=6,
    wf_step_months=6,
    wf_anchored=False,
)


def folds_for(anchored: bool) -> list:
    return walk_forward_folds(
        design_window(CFG),
        train_months=CFG.wf_train_months,
        test_months=CFG.wf_test_months,
        step_months=CFG.wf_step_months,
        anchored=anchored,
    )


# Hand-checked: train 24 months, test the next 6, roll by 6. The 10th fold would test
# 2025-07-01..2026-01-01, past the design end 2025-10-01, so it is dropped.
ROLLING = [
    # (train_start, train_end == test_start, test_end)
    ("2019-01-01", "2021-01-01", "2021-07-01"),
    ("2019-07-01", "2021-07-01", "2022-01-01"),
    ("2020-01-01", "2022-01-01", "2022-07-01"),
    ("2020-07-01", "2022-07-01", "2023-01-01"),
    ("2021-01-01", "2023-01-01", "2023-07-01"),
    ("2021-07-01", "2023-07-01", "2024-01-01"),
    ("2022-01-01", "2024-01-01", "2024-07-01"),
    ("2022-07-01", "2024-07-01", "2025-01-01"),
    ("2023-01-01", "2025-01-01", "2025-07-01"),
]


def test_design_window_is_design_start_to_holdout_start() -> None:
    w = design_window(CFG)
    assert (w.start, w.end, w.kind) == (ts("2019-01-01"), ts("2025-10-01"), "design")


def test_design_window_rejects_holdout_before_design_start() -> None:
    bad = SplitConfig(ts("2026-01-01"), ts("2025-10-01"), 3, 24, 6, 6, False)
    with pytest.raises(ValueError, match="design_start"):
        design_window(bad)


def test_holdout_window_runs_from_holdout_start_to_data_end() -> None:
    w = holdout_window(CFG, ts("2026-10-01"))
    assert (w.start, w.end, w.kind) == (ts("2025-10-01"), ts("2026-10-01"), "holdout")


@pytest.mark.parametrize("end", ["2025-10-01", "2025-06-01"])
def test_holdout_window_rejects_data_end_not_after_holdout_start(end: str) -> None:
    with pytest.raises(ValueError, match="data_end"):
        holdout_window(CFG, ts(end))


def test_holdout_window_rejects_naive_data_end() -> None:
    with pytest.raises(ValueError, match="data_end"):
        holdout_window(CFG, pd.Timestamp("2026-10-01"))


def test_rolling_folds_match_hand_checked_boundaries() -> None:
    folds = folds_for(anchored=False)
    got = [(f.train.start, f.train.end, f.test.start, f.test.end, f.index) for f in folds]
    want = [(ts(a), ts(b), ts(b), ts(c), i) for i, (a, b, c) in enumerate(ROLLING)]
    assert got == want
    assert all(f.train.kind == "walk_forward_train" for f in folds)
    assert all(f.test.kind == "walk_forward_test" for f in folds)


def test_anchored_folds_always_train_from_design_start() -> None:
    folds = folds_for(anchored=True)
    assert len(folds) == len(ROLLING)
    for f, (_, b, c) in zip(folds, ROLLING, strict=True):
        assert f.train.start == ts("2019-01-01")
        assert (f.train.end, f.test.start, f.test.end) == (ts(b), ts(b), ts(c))


@pytest.mark.parametrize("anchored", [False, True])
def test_no_fold_test_window_passes_design_end(anchored: bool) -> None:
    design = design_window(CFG)
    for f in folds_for(anchored):
        assert f.test.end <= design.end
        assert f.train.start >= design.start
        assert f.train.end == f.test.start


def test_test_windows_do_not_overlap_when_step_equals_test() -> None:
    folds = folds_for(anchored=False)
    for a, b in pairwise(folds):
        assert a.test.end == b.test.start


def test_month_offsets_do_not_drift_from_month_end_start() -> None:
    design = Window(ts("2020-01-31"), ts("2021-01-31"), "design")
    folds = walk_forward_folds(design, train_months=1, test_months=1, step_months=1, anchored=False)
    # Every boundary is start + k months from 2020-01-31 (clamped to month end), not chained.
    assert [f.test.start for f in folds[:3]] == [
        ts("2020-02-29"),
        ts("2020-03-31"),
        ts("2020-04-30"),
    ]


def test_fold_that_exactly_fills_design_window_is_kept() -> None:
    design = Window(ts("2020-01-01"), ts("2021-01-01"), "design")
    folds = walk_forward_folds(design, train_months=6, test_months=6, step_months=6, anchored=False)
    assert len(folds) == 1
    assert folds[0].test.end == ts("2021-01-01")


def test_no_fold_fits_raises_naming_train_months() -> None:
    design = Window(ts("2024-01-01"), ts("2025-01-01"), "design")
    with pytest.raises(ValueError, match="train_months"):
        walk_forward_folds(design, train_months=24, test_months=6, step_months=6, anchored=False)


@pytest.mark.parametrize("arg", ["train_months", "test_months", "step_months"])
def test_non_positive_month_args_raise_naming_the_arg(arg: str) -> None:
    kwargs = {"train_months": 24, "test_months": 6, "step_months": 6, arg: 0}
    with pytest.raises(ValueError, match=arg):
        walk_forward_folds(design_window(CFG), anchored=False, **kwargs)


def test_slice_window_is_half_open() -> None:
    c = make_candles(12, interval="1d", start="2024-01-01")
    w = Window(ts("2024-01-03"), ts("2024-01-06"), "custom")
    out = slice_window(c, w)
    assert list(out.index) == [ts("2024-01-03"), ts("2024-01-04"), ts("2024-01-05")]
    assert out.equals(c.iloc[2:5])


def test_history_through_keeps_earlier_rows_and_drops_window_end() -> None:
    c = make_candles(12, interval="1d", start="2024-01-01")
    w = Window(ts("2024-01-05"), ts("2024-01-08"), "custom")
    out = history_through(c, w)
    assert out.index[0] == ts("2024-01-01")
    assert out.index[-1] == ts("2024-01-07")
    assert out.equals(c.iloc[:7])


def test_assert_no_holdout_raises_naming_holdout_start() -> None:
    c = make_candles(10, interval="1d", start="2025-09-25")
    with pytest.raises(ValueError, match=r"holdout_start 2025-10-01T00:00:00\+00:00"):
        assert_no_holdout(c, CFG)


def test_assert_no_holdout_accepts_bars_before_holdout_start() -> None:
    c = make_candles(6, interval="1d", start="2025-09-25")  # last bar opens 2025-09-30
    assert_no_holdout(c, CFG)


def test_design_view_drops_holdout_rows_and_keeps_pre_design_history() -> None:
    c = make_candles(40, interval="1d", start="2018-12-20")
    early_cfg = SplitConfig(ts("2019-01-01"), ts("2019-01-10"), 3, 24, 6, 6, False)
    out = design_view(c, early_cfg)
    assert out.index[0] == ts("2018-12-20")  # warm-up history before design_start kept
    assert out.index[-1] == ts("2019-01-09")
    assert_no_holdout(out, early_cfg)


def test_slice_rejects_naive_index() -> None:
    c = make_candles(5, interval="1d", start="2024-01-01")
    c.index = c.index.tz_localize(None)
    with pytest.raises(ValueError, match="tz-aware"):
        slice_window(c, Window(ts("2024-01-01"), ts("2024-01-03"), "custom"))
