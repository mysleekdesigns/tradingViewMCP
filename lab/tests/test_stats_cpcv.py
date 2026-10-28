"""CPCV path Sharpes over a sweep's configurations: counts, known answers, and train-only
selection. Synthetic returns with explicit seeds; exact cases hold to 1e-12 (the only error is
the order of floating-point sums)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from lab.engine.splits import cpcv_groups, cpcv_path_assignment, cpcv_splits
from lab.stats.cpcv import (
    CPCV_EMBARGO_FRACTION,
    CPCV_GROUPS,
    CPCV_PURGE_BARS,
    CPCV_TEST_GROUPS,
    CpcvResult,
    cpcv_paths,
)

BPY = 2190.0  # 4h bars in a 365-day year


def _noise(t: int, m: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal((t, m)) * 0.01


def test_defaults_are_6_groups_2_test_purge_1_embargo_1pct() -> None:
    assert (CPCV_GROUPS, CPCV_TEST_GROUPS, CPCV_PURGE_BARS) == (6, 2, 1)
    assert CPCV_EMBARGO_FRACTION == 0.01
    out = cpcv_paths(_noise(1234, 3, seed=1), bars_per_year=BPY)
    assert isinstance(out, CpcvResult)
    assert (out.n_groups, out.n_test_groups, out.n_splits, out.n_paths) == (6, 2, 15, 5)
    assert (out.n_configs, out.n_bars, out.purge_bars) == (3, 1234, 1)
    assert out.embargo_bars == math.ceil(0.01 * 1234) == 13  # rounded up, never zero
    assert len(out.chosen) == 15 and len(out.path_sharpes) == 5


def test_dominant_config_is_always_chosen_and_every_path_is_its_full_sharpe() -> None:
    # Column 2 = column 0 + 0.01 on every bar: same std, higher mean, so it has the best
    # Sharpe on any subset. Every split picks it, every path is its full series reordered by
    # group (= the series itself), so every path Sharpe equals its full-sample Sharpe.
    base = _noise(600, 2, seed=2)
    r = np.column_stack([base[:, 0], base[:, 1], base[:, 0] + 0.01])
    out = cpcv_paths(r, bars_per_year=BPY)
    assert out.chosen == (2,) * 15
    full = r[:, 2].mean() / r[:, 2].std(ddof=1) * math.sqrt(BPY)
    assert out.path_sharpes == pytest.approx([full] * 5, rel=1e-12)
    assert out.share_positive == 1.0
    assert out.quantiles["min"] == pytest.approx(full, rel=1e-12)
    assert out.quantiles["max"] == pytest.approx(full, rel=1e-12)


def test_regime_switch_hand_checked_selection_and_paths() -> None:
    # 6 groups of 100 bars. Config 0 earns +0.01 drift in groups 0-2 and -0.01 in groups 3-5;
    # config 1 the reverse. With identical noise, the train winner is the config whose drift
    # sum over the train groups is larger. Test groups (a, b): train = the other four groups.
    t = 600
    noise = _noise(t, 1, seed=3)[:, 0]
    drift0 = np.where(np.arange(t) < 300, 0.01, -0.01)
    r = np.column_stack([noise + drift0, noise - drift0])
    out = cpcv_paths(r, bars_per_year=BPY, purge_bars=0, embargo_bars=0)
    splits = cpcv_splits(t, 6, 2, purge_bars=0, embargo_bars=0)
    for split, chosen in zip(splits, out.chosen, strict=True):
        train_groups = [g for g in range(6) if g not in split.test_groups]
        early = sum(g < 3 for g in train_groups)
        late = len(train_groups) - early
        if early != late:
            assert chosen == (0 if early > late else 1)
    # Rebuild every path by hand from the chosen columns.
    groups = cpcv_groups(t, 6)
    for p, path in enumerate(cpcv_path_assignment(6, 2)):
        series = np.concatenate(
            [r[s:e, out.chosen[k]] for (s, e), k in zip(groups, path, strict=True)]
        )
        want = series.mean() / series.std(ddof=1) * math.sqrt(BPY)
        assert out.path_sharpes[p] == pytest.approx(want, rel=1e-12)


def test_selection_never_sees_test_purge_or_embargo_bars() -> None:
    # No look-ahead: rewriting a config's returns on a split's test bars (and on its purged and
    # embargoed bars) must never change that split's choice.
    r = _noise(900, 6, seed=4)
    r[:, 3] += 0.002  # a mild winner so choices are not all ties
    base = cpcv_paths(r, bars_per_year=BPY, purge_bars=2, embargo_bars=5)
    splits = cpcv_splits(900, 6, 2, purge_bars=2, embargo_bars=5)
    rng = np.random.default_rng(5)
    for split in splits:
        hidden = ~split.train
        for col in range(6):
            bumped = r.copy()
            bumped[hidden, col] = rng.standard_normal(int(hidden.sum())) * 0.05 + 0.05
            out = cpcv_paths(bumped, bars_per_year=BPY, purge_bars=2, embargo_bars=5)
            assert out.chosen[split.index] == base.chosen[split.index]


def test_ties_go_to_the_lowest_column_and_constant_columns_rank_last() -> None:
    col = _noise(300, 1, seed=6)[:, 0]
    r = np.column_stack([np.zeros(300), col, col, np.full(300, 0.001)])
    out = cpcv_paths(r, bars_per_year=BPY, purge_bars=0, embargo_bars=0)
    # Columns 1 and 2 tie; constant columns 0 and 3 have no Sharpe and rank below them.
    assert out.chosen == (1,) * 15


def test_all_constant_paths_give_nan_sharpes_and_nan_summary() -> None:
    out = cpcv_paths(np.zeros((120, 2)), bars_per_year=BPY, purge_bars=0, embargo_bars=0)
    assert all(math.isnan(s) for s in out.path_sharpes)
    assert all(math.isnan(v) for v in out.quantiles.values())
    assert math.isnan(out.share_positive)


def test_quantiles_and_share_positive_summarise_the_finite_path_sharpes() -> None:
    out = cpcv_paths(_noise(1200, 8, seed=7), bars_per_year=BPY)
    vals = np.array(out.path_sharpes)
    assert list(out.quantiles) == ["min", "p05", "p25", "median", "p75", "p95", "max"]
    assert out.quantiles["min"] == vals.min() and out.quantiles["max"] == vals.max()
    assert out.quantiles["median"] == pytest.approx(float(np.median(vals)), rel=1e-12)
    assert out.share_positive == float(np.mean(vals > 0))


def test_single_config_is_allowed() -> None:
    r = _noise(300, 1, seed=8)
    out = cpcv_paths(r, bars_per_year=BPY)
    full = r[:, 0].mean() / r[:, 0].std(ddof=1) * math.sqrt(BPY)
    assert out.path_sharpes == pytest.approx([full] * 5, rel=1e-12)


@pytest.mark.parametrize(
    ("returns", "kwargs", "match"),
    [
        (np.zeros((100, 0)), {}, "M >= 1"),
        (np.zeros(100), {}, "2-D"),
        (np.full((100, 2), np.nan), {}, "NaN"),
        (np.zeros((100, 2)), {"bars_per_year": 0.0}, "bars_per_year"),
        (np.zeros((100, 2)), {"n_test_groups": 6}, "n_test_groups"),
        (np.zeros((100, 2)), {"n_groups": 1}, "n_groups"),
        # 6 groups of 4 bars: 4 < purge 1 + embargo 2 + 2.
        (np.zeros((24, 2)), {"embargo_bars": 2}, "T=24"),
    ],
)
def test_bad_inputs_raise(returns: np.ndarray, kwargs: dict, match: str) -> None:
    args = {"bars_per_year": BPY, **kwargs}
    with pytest.raises(ValueError, match=match):
        cpcv_paths(returns, **args)
