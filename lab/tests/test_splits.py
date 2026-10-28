"""Design / walk-forward / locked-holdout splits. Boundaries are hand-checked calendar dates."""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from lab.config import SplitConfig
from lab.engine.splits import (
    assert_no_holdout,
    cpcv_groups,
    cpcv_path_assignment,
    cpcv_splits,
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


# ------------------------------------------------------------------ CPCV (index-based)


def test_cpcv_groups_spread_the_remainder_over_the_first_groups() -> None:
    # 20 bars / 6 groups = 3 rem 2: the first two groups get 4 bars.
    assert cpcv_groups(20, 6) == [(0, 4), (4, 8), (8, 11), (11, 14), (14, 17), (17, 20)]
    assert cpcv_groups(12, 6) == [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 12)]


@pytest.mark.parametrize(("n_bars", "n_groups"), [(5, 6), (10, 1), (10, 0)])
def test_cpcv_groups_rejects_bad_sizes(n_bars: int, n_groups: int) -> None:
    with pytest.raises(ValueError, match=r"n_bars|n_groups"):
        cpcv_groups(n_bars, n_groups)


def test_cpcv_6_choose_2_has_15_splits_in_combinations_order() -> None:
    splits = cpcv_splits(60, 6, 2, purge_bars=0, embargo_bars=0)
    assert len(splits) == 15
    assert [s.index for s in splits] == list(range(15))
    assert splits[0].test_groups == (0, 1)
    assert splits[4].test_groups == (0, 5)
    assert splits[-1].test_groups == (4, 5)


def test_cpcv_without_purge_train_is_the_complement_of_test() -> None:
    for s in cpcv_splits(60, 6, 2, purge_bars=0, embargo_bars=0):
        assert s.train.dtype == bool and s.test.dtype == bool
        assert np.array_equal(s.train, ~s.test)
        assert int(s.test.sum()) == 20


def test_cpcv_train_and_test_never_overlap() -> None:
    for s in cpcv_splits(103, 6, 2, purge_bars=2, embargo_bars=3):
        assert not np.any(s.train & s.test)


def test_cpcv_purge_before_and_embargo_after_each_test_block_hand_checked() -> None:
    # 60 bars, groups of 10. Split (1, 4): test bars 10-19 and 40-49. Purge 2 removes 8-9 and
    # 38-39; embargo 3 removes 20-22 and 50-52.
    splits = cpcv_splits(60, 6, 2, purge_bars=2, embargo_bars=3)
    s = next(x for x in splits if x.test_groups == (1, 4))
    assert np.flatnonzero(s.test).tolist() == [*range(10, 20), *range(40, 50)]
    removed = sorted({*range(8, 10), *range(38, 40), *range(20, 23), *range(50, 53)})
    want_train = [i for i in range(60) if not s.test[i] and i not in removed]
    assert np.flatnonzero(s.train).tolist() == want_train


def test_cpcv_adjacent_test_groups_form_one_block() -> None:
    # Split (2, 3): one block 20-39; purge 18-19 and embargo 40-41 only (nothing at 30).
    s = next(
        x for x in cpcv_splits(60, 6, 2, purge_bars=2, embargo_bars=2) if x.test_groups == (2, 3)
    )
    assert np.flatnonzero(~s.train & ~s.test).tolist() == [18, 19, 40, 41]


def test_cpcv_purge_and_embargo_clip_at_the_series_ends() -> None:
    splits = cpcv_splits(60, 6, 2, purge_bars=5, embargo_bars=5)
    first = next(x for x in splits if x.test_groups == (0, 1))
    last = next(x for x in splits if x.test_groups == (4, 5))
    assert np.flatnonzero(~first.train & ~first.test).tolist() == list(range(20, 25))
    assert np.flatnonzero(~last.train & ~last.test).tolist() == list(range(35, 40))


@pytest.mark.parametrize(
    ("kwargs", "name"),
    [
        ({"n_test_groups": 6}, "n_test_groups"),
        ({"n_test_groups": 0}, "n_test_groups"),
        ({"purge_bars": -1}, "purge_bars"),
        ({"embargo_bars": -1}, "embargo_bars"),
    ],
)
def test_cpcv_splits_reject_bad_arguments(kwargs: dict, name: str) -> None:
    args = {"n_test_groups": 2, "purge_bars": 1, "embargo_bars": 1, **kwargs}
    with pytest.raises(ValueError, match=name):
        cpcv_splits(60, 6, args.pop("n_test_groups"), **args)


def test_cpcv_6_choose_2_has_5_paths_each_covering_every_group_once() -> None:
    paths = cpcv_path_assignment(6, 2)
    assert len(paths) == 5
    combos = [s.test_groups for s in cpcv_splits(60, 6, 2, purge_bars=0, embargo_bars=0)]
    for path in paths:
        assert len(path) == 6
        for g, split_index in enumerate(path):
            assert g in combos[split_index]  # that split really tests group g
    # Each (split, group) test slot is used by exactly one path: 15 splits x 2 groups = 30.
    slots = {(split_index, g) for path in paths for g, split_index in enumerate(path)}
    assert len(slots) == 30


def test_cpcv_path_assignment_hand_checked_4_choose_2() -> None:
    # Splits: 0=(0,1) 1=(0,2) 2=(0,3) 3=(1,2) 4=(1,3) 5=(2,3). Group g's j-th split -> path j.
    # g0: 0,1,2; g1: 0,3,4; g2: 1,3,5; g3: 2,4,5.
    assert cpcv_path_assignment(4, 2) == [[0, 0, 1, 2], [1, 3, 3, 4], [2, 4, 5, 5]]


def test_cpcv_every_bar_of_every_path_is_covered_exactly_once() -> None:
    n_bars = 101
    splits = cpcv_splits(n_bars, 6, 2, purge_bars=1, embargo_bars=2)
    groups = cpcv_groups(n_bars, 6)
    for path in cpcv_path_assignment(6, 2):
        covered = np.zeros(n_bars, dtype=int)
        for (start, end), split_index in zip(groups, path, strict=True):
            test = splits[split_index].test
            assert test[start:end].all()
            covered[start:end] += 1
        assert np.all(covered == 1)
