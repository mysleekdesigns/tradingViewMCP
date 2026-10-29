"""Paired stationary-bootstrap tests, break-even cost multiple and block length (Phase 15)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from lab.stats import (
    PairedBootstrap,
    bootstrap_block_bars,
    break_even_cost_multiple,
    cdar,
    cdar_difference_test,
    paired_stationary_bootstrap,
    sharpe_difference_test,
    sharpe_per_bar,
    stationary_bootstrap_indices,
)


def _mean(x: np.ndarray) -> float:
    return float(np.mean(x))


def _path(seed: int, n: int = 200) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0005, 0.03, n)


# --------------------------------------------------------------------------- core bootstrap


def test_identical_series_point_zero_and_p_one() -> None:
    r = _path(0)
    res = paired_stationary_bootstrap(r, r, _mean, n_samples=300, mean_block=5, seed=1, level=0.95)
    assert res.point == 0.0
    assert res.p_value == 1.0
    assert res.lower == 0.0 and res.upper == 0.0
    assert res.n_samples == res.n_valid == 300


def test_clearly_better_series_hits_the_p_value_floor() -> None:
    # The overlay never loses (CDaR 0 in every resample); the benchmark has many losing bars,
    # so its resampled CDaR is positive every time and d* < 0 in every resample.
    bench = _path(3)
    overlay = np.maximum(bench, 0.0)
    res = cdar_difference_test(
        overlay, bench, confidence=0.95, n_samples=400, mean_block=10, seed=7, level=0.95
    )
    assert res.n_valid == 400
    assert res.p_value == pytest.approx(1.0 / (1.0 + res.n_valid))
    assert res.point == pytest.approx(-cdar(bench, 0.95))
    assert res.upper < 0.0


def test_p_value_floor_is_one_over_one_plus_n_valid() -> None:
    a, b = np.zeros(10), np.ones(10)
    res = paired_stationary_bootstrap(a, b, _mean, n_samples=99, mean_block=3, seed=0, level=0.9)
    assert res.p_value == 1.0 / 100.0
    assert res.point == -1.0 and res.lower == -1.0 and res.upper == -1.0


def test_deterministic_per_seed() -> None:
    a, b = _path(1), _path(2)
    kw = {"n_samples": 200, "mean_block": 8.0, "level": 0.95}
    r1 = paired_stationary_bootstrap(a, b, _mean, seed=42, **kw)
    r2 = paired_stationary_bootstrap(a, b, _mean, seed=42, **kw)
    r3 = paired_stationary_bootstrap(a, b, _mean, seed=43, **kw)
    assert r1 == r2
    assert (r1.lower, r1.upper) != (r3.lower, r3.upper)
    assert isinstance(r1, PairedBootstrap)
    assert (r1.seed, r1.mean_block, r1.level, r1.n_samples) == (42, 8.0, 0.95, 200)


def test_a_and_b_are_resampled_with_the_same_indices() -> None:
    n, seed, n_samples, mean_block = 12, 5, 6, 3.0
    a = np.arange(n, dtype=float)
    b = a + 1000.0
    seen: list[np.ndarray] = []

    def record(x: np.ndarray) -> float:
        seen.append(x.copy())
        return 0.0

    paired_stationary_bootstrap(
        a, b, record, n_samples=n_samples, mean_block=mean_block, seed=seed, level=0.95
    )
    expected = stationary_bootstrap_indices(
        n, mean_block=mean_block, n_samples=n_samples, rng=np.random.default_rng(seed)
    )
    assert len(seen) == 2 * n_samples + 2  # each resample on a then b, then the originals
    for k in range(n_samples):
        np.testing.assert_array_equal(seen[2 * k], expected[k])  # a[i] is i
        np.testing.assert_array_equal(seen[2 * k + 1], expected[k] + 1000.0)


def test_quantiles_and_p_value_match_a_manual_replay() -> None:
    a, b = _path(10, 60), _path(11, 60)
    seed, n_samples, mean_block, level = 9, 250, 4.0, 0.9
    res = paired_stationary_bootstrap(
        a, b, _mean, n_samples=n_samples, mean_block=mean_block, seed=seed, level=level
    )
    idx = stationary_bootstrap_indices(
        60, mean_block=mean_block, n_samples=n_samples, rng=np.random.default_rng(seed)
    )
    d = a[idx].mean(axis=1) - b[idx].mean(axis=1)
    assert res.point == pytest.approx(a.mean() - b.mean(), abs=1e-15)
    assert res.lower == pytest.approx(float(np.quantile(d, 1 - level)), abs=1e-15)
    assert res.upper == pytest.approx(float(np.quantile(d, level)), abs=1e-15)
    assert res.p_value == (1 + int(np.sum(d >= 0))) / (1 + n_samples)


def test_non_finite_resamples_are_dropped() -> None:
    # A single non-zero bar: resamples that miss it have zero variance -> NaN Sharpe.
    overlay = np.zeros(20)
    overlay[7] = 0.01
    bench = _path(4, 20)
    res = sharpe_difference_test(
        overlay, bench, interval="1d", n_samples=300, mean_block=2, seed=3, level=0.95
    )
    assert 0 < res.n_valid < res.n_samples == 300
    assert math.isfinite(res.lower) and math.isfinite(res.upper)
    assert res.p_value >= 1.0 / (1.0 + res.n_valid)


def test_no_valid_resample_gives_nan_bounds_and_p_one() -> None:
    flat = np.zeros(10)
    res = sharpe_difference_test(
        flat, flat, interval="1d", n_samples=20, mean_block=2, seed=0, level=0.95
    )
    assert res.n_valid == 0
    assert res.p_value == 1.0
    assert math.isnan(res.lower) and math.isnan(res.upper) and math.isnan(res.point)


def test_chunked_draws_keep_every_row() -> None:
    # n large enough that the index matrix is drawn in several chunks.
    a, b = _path(20, 50_000), _path(21, 50_000)
    res = paired_stationary_bootstrap(a, b, _mean, n_samples=45, mean_block=50, seed=2, level=0.95)
    assert res.n_samples == res.n_valid == 45


def test_input_validation() -> None:
    a = _path(0, 10)
    kw = {"n_samples": 10, "mean_block": 2.0, "seed": 0, "level": 0.95}
    with pytest.raises(ValueError, match="align"):
        paired_stationary_bootstrap(a, a[:9], _mean, **kw)
    with pytest.raises(ValueError, match="NaN or inf"):
        paired_stationary_bootstrap(a, np.full(10, np.nan), _mean, **kw)
    with pytest.raises(ValueError, match="non-empty"):
        paired_stationary_bootstrap([], [], _mean, **kw)
    for bad in (0.5, 1.0, 0.2, float("nan")):
        with pytest.raises(ValueError, match="level"):
            paired_stationary_bootstrap(a, a, _mean, **{**kw, "level": bad})
    with pytest.raises(ValueError, match="n_samples"):
        paired_stationary_bootstrap(a, a, _mean, **{**kw, "n_samples": 0})
    with pytest.raises(ValueError, match="mean_block"):
        paired_stationary_bootstrap(a, a, _mean, **{**kw, "mean_block": 0.5})


# --------------------------------------------------------------------------- CDaR / Sharpe


def test_cdar_difference_point_is_overlay_minus_matched() -> None:
    a, b = _path(5, 100), _path(6, 100)
    res = cdar_difference_test(a, b, confidence=0.9, n_samples=50, mean_block=5, seed=1, level=0.95)
    assert res.point == pytest.approx(cdar(a, 0.9) - cdar(b, 0.9), abs=1e-15)


@pytest.mark.parametrize(("interval", "bars"), [("1d", 365.0), ("4h", 2190.0)])
def test_sharpe_difference_annualised_with_365_days(interval: str, bars: float) -> None:
    # a: mean .02, std(ddof=1) = sqrt(4e-4 / 3) -> per-bar Sharpe sqrt(3); b: mean 0 -> 0.
    a = [0.01, 0.03, 0.01, 0.03]
    b = [0.01, -0.01, 0.01, -0.01]
    assert sharpe_per_bar(a) == pytest.approx(math.sqrt(3.0), rel=1e-12)
    res = sharpe_difference_test(
        a, b, interval=interval, n_samples=20, mean_block=1, seed=0, level=0.95
    )
    assert res.point == pytest.approx(math.sqrt(3.0 * bars), rel=1e-12)
    assert res.point != pytest.approx(math.sqrt(3.0 * 252.0), rel=1e-3)


def test_sharpe_difference_lower_bound_below_point_and_unknown_interval() -> None:
    a, b = _path(7, 300), _path(8, 300)
    res = sharpe_difference_test(
        a, b, interval="1d", n_samples=300, mean_block=10, seed=4, level=0.95
    )
    assert res.lower < res.point < res.upper
    with pytest.raises(ValueError, match="interval"):
        sharpe_difference_test(a, b, interval="2d", n_samples=5, mean_block=2, seed=0, level=0.95)


# --------------------------------------------------------------------------- break-even


def test_break_even_cost_multiple_cases() -> None:
    assert break_even_cost_multiple({1.0: True, 1.5: True, 2.0: True, 3.0: True, 4.0: True}) == 4.0
    assert break_even_cost_multiple({1.0: True, 1.5: True, 2.0: True, 3.0: False}) == 2.0
    # The walk stops at the first failure; a later multiple that holds again does not count.
    assert break_even_cost_multiple({1.0: True, 1.5: False, 2.0: True, 3.0: True}) == 1.0
    # Order of insertion does not matter.
    assert break_even_cost_multiple({3.0: False, 1.0: True, 2.0: True}) == 2.0
    # The smallest multiple fails (or nothing tested): None.
    assert break_even_cost_multiple({1.0: False, 2.0: True}) is None
    assert break_even_cost_multiple({}) is None
    assert break_even_cost_multiple({1.0: np.bool_(True)}) == 1.0


def test_break_even_cost_multiple_with_grid_gaps() -> None:
    # Untested multiples between grid points are not failures: 4 is the largest tested m.
    assert break_even_cost_multiple({1.0: True, 2.0: True, 4.0: True}) == 4.0
    assert break_even_cost_multiple({1.0: True, 2.0: True, 4.0: False}) == 2.0


def test_break_even_cost_multiple_validation() -> None:
    with pytest.raises(ValueError, match="bool"):
        break_even_cost_multiple({1.0: 1})  # type: ignore[dict-item]
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="cost multiples"):
            break_even_cost_multiple({bad: True})


# --------------------------------------------------------------------------- block length


@pytest.mark.parametrize(
    ("days", "interval", "bars"),
    [
        (60, "1d", 60),
        (60, "4h", 360),
        (60, "1h", 1440),
        (60, "12h", 120),
        (1.5, "1d", 2),
        (0.1, "1d", 1),
        (0, "1d", 1),
        (0.5, "12h", 1),
        (0.7, "1h", 17),  # 16.8 bars -> 17
        ((0.1 + 0.2) * 10, "1d", 3),  # 3.0000000000000004 is 3 whole days, not 4
    ],
)
def test_bootstrap_block_bars(days: float, interval: str, bars: int) -> None:
    assert bootstrap_block_bars(days, interval) == bars


def test_bootstrap_block_bars_validation() -> None:
    with pytest.raises(ValueError, match="interval"):
        bootstrap_block_bars(60, "1w")
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="days"):
            bootstrap_block_bars(bad, "1d")
