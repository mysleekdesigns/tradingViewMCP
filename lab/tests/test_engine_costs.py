"""Slippage series: hand values, causality, validation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab.engine.costs import slippage_series, validate_costs
from lab.types import CostModel
from tests.synthetic import candles_from_rows, make_candles, perturb_after

# Tolerance 1e-12: values come from a few float subtractions/divisions of exact decimals.
TOL = 1e-12

# Bar ranges r = (high - low) / close with close = 100:
#   r = [0.02, 0.04, 0.03, 0.10, 0.01, 0.02]
RANGE_ROWS = [
    (100.0, 101.0, 99.0, 100.0),
    (100.0, 102.0, 98.0, 100.0),
    (100.0, 101.5, 98.5, 100.0),
    (100.0, 105.0, 95.0, 100.0),
    (100.0, 100.5, 99.5, 100.0),
    (100.0, 101.0, 99.0, 100.0),
]


def test_flat_slippage_when_vol_mult_is_zero() -> None:
    candles = make_candles(50, seed=1)
    slip = slippage_series(candles, CostModel(fee_rate=0.001, slippage_rate=0.0005))
    assert slip.shape == (50,)
    assert np.all(slip == 0.0005)


def test_vol_scaled_slippage_matches_hand_values() -> None:
    candles = candles_from_rows(RANGE_ROWS)
    # Cap far above every value here, so this test pins the uncapped formula.
    costs = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.5, slippage_cap=1.0)
    slip = slippage_series(candles, costs, median_window=3)
    # Trailing 3-bar medians of r: med[0], med[1] = NaN (warm-up),
    #   med[2] = median(.02,.04,.03) = .03, med[3] = median(.04,.03,.10) = .04,
    #   med[4] = median(.03,.10,.01) = .03
    # slip[j] = 0.0005 + 0.5 * max(0, r[j-1] - med[j-1]):
    #   slip[0] = base, slip[1] = base (med[0] NaN), slip[2] = base (med[1] NaN)
    #   slip[3] = base + 0.5 * max(0, .03 - .03) = 0.0005
    #   slip[4] = base + 0.5 * (.10 - .04)       = 0.0305
    #   slip[5] = base + 0.5 * max(0, .01 - .03) = 0.0005
    expected = np.array([0.0005, 0.0005, 0.0005, 0.0005, 0.0305, 0.0005])
    np.testing.assert_allclose(slip, expected, rtol=0, atol=TOL)


def test_slippage_at_bar_ignores_that_bars_own_range() -> None:
    candles = make_candles(300, seed=3)
    costs = CostModel(slippage_rate=0.0005, slippage_vol_mult=0.3)
    base = slippage_series(candles, costs, median_window=20)
    j = 150
    bumped = candles.copy()
    bumped.iloc[j, bumped.columns.get_loc("high")] *= 1.5
    bumped.iloc[j, bumped.columns.get_loc("low")] *= 0.6
    slip = slippage_series(bumped, costs, median_window=20)
    assert np.array_equal(slip[: j + 1], base[: j + 1])
    assert slip[j + 1] != base[j + 1]  # bar j's range is first used for the fill at j + 1


@pytest.mark.parametrize("t", [0, 25, 120, 298])
def test_slippage_is_causal_under_perturbation(t: int) -> None:
    candles = make_candles(300, seed=4)
    costs = CostModel(slippage_rate=0.0005, slippage_vol_mult=0.3)
    a = slippage_series(candles, costs, median_window=20)
    b = slippage_series(perturb_after(candles, t), costs, median_window=20)
    # slip[t + 1] only uses rows <= t, so it must also be unchanged.
    assert np.array_equal(a[: t + 2], b[: t + 2])


def test_slippage_is_never_below_base_rate() -> None:
    candles = make_candles(400, seed=5, vol=0.03)
    costs = CostModel(slippage_rate=0.0005, slippage_vol_mult=1.0)
    slip = slippage_series(candles, costs, median_window=10)
    assert np.all(slip >= 0.0005)
    assert np.any(slip > 0.0005)


def test_rejects_bad_median_window() -> None:
    with pytest.raises(ValueError, match="median_window"):
        slippage_series(make_candles(10), CostModel(slippage_vol_mult=1.0), median_window=0)


@pytest.mark.parametrize(
    "field", ["fee_rate", "slippage_rate", "slippage_vol_mult", "slippage_cap"]
)
def test_rejects_negative_cost_fields(field: str) -> None:
    kwargs = {"fee_rate": 0.001, "slippage_rate": 0.0005, "slippage_vol_mult": 0.0, field: -0.1}
    with pytest.raises(ValueError, match=field):
        validate_costs(CostModel(**kwargs))


def test_rejects_candles_breaking_the_convention() -> None:
    candles = make_candles(10)
    bad = candles.copy()
    bad.index = pd.RangeIndex(10)
    with pytest.raises(ValueError, match="index"):
        slippage_series(bad, CostModel())


def test_slippage_is_clipped_to_the_cap() -> None:
    candles = candles_from_rows(RANGE_ROWS)
    costs = CostModel(
        fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.5, slippage_cap=0.01
    )
    slip = slippage_series(candles, costs, median_window=3)
    # Same bars as the hand-value test: uncapped slip[4] = 0.0005 + 0.5 * (.10 - .04) = 0.0305,
    # so min(0.0305, 0.01) = 0.01; every other bar stays at the 0.0005 base rate.
    expected = np.array([0.0005, 0.0005, 0.0005, 0.0005, 0.01, 0.0005])
    np.testing.assert_allclose(slip, expected, rtol=0, atol=TOL)


def test_rejects_a_cap_below_the_base_rate() -> None:
    with pytest.raises(ValueError, match="slippage_cap"):
        validate_costs(CostModel(slippage_rate=0.02, slippage_cap=0.01))
    validate_costs(CostModel(slippage_rate=0.01, slippage_cap=0.01))  # equal is fine


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.001])
def test_rejects_non_finite_or_negative_cap(bad: float) -> None:
    with pytest.raises(ValueError, match="slippage_cap"):
        validate_costs(CostModel(slippage_cap=bad))


def test_scaled_costs_scale_the_cap() -> None:
    assert CostModel(slippage_cap=0.01).scaled(2.0).slippage_cap == pytest.approx(0.02)
