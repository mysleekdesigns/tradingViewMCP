"""Slippage series and participation impact: hand values, causality, validation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab.data.universe import trailing_dollar_volume
from lab.engine.costs import (
    ImpactModel,
    impact_fraction,
    impact_inputs,
    slippage_series,
    validate_costs,
    validate_impact,
)
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


# --------------------------------------------------------------------------- market impact

IMPACT = ImpactModel(coef=0.5, aum_usd=1e6, adv_days=2, vol_days=2, cap=0.05)


def test_impact_fraction_matches_hand_values_and_charges_unknown_liquidity_the_cap() -> None:
    q = np.array([10_000.0, 1e6, 0.0, 1e8, 5_000.0, 5_000.0, 5_000.0, 5_000.0, 5_000.0])
    adv = np.array([1e6, 1e6, 1e6, 1e6, np.nan, 0.0, -1.0, 1e6, 1e6])
    sigma = np.array([0.04, 0.04, 0.04, 0.04, 0.04, 0.04, 0.04, np.nan, 0.0])
    frac = impact_fraction(q, adv, sigma, IMPACT)
    expected = [
        0.5 * 0.04 * 0.1,  # sqrt(1e4 / 1e6) = 0.1 -> 0.002
        0.5 * 0.04 * 1.0,  # Q = ADV -> 0.02
        0.0,  # no trade, no impact
        0.05,  # 0.5 * 0.04 * sqrt(100) = 0.2, capped at 0.05
        0.05,  # ADV NaN (warm-up): cap
        0.05,  # ADV 0 (no volume): cap
        0.05,  # ADV < 0: cap
        0.05,  # sigma NaN: cap
        0.05,  # sigma 0: cap
    ]
    np.testing.assert_allclose(frac, expected, rtol=0, atol=TOL)


def test_impact_fraction_broadcasts_and_rejects_negative_trade_values() -> None:
    frac = impact_fraction(np.array([1e4, 4e4]), np.array(1e6), np.array(0.04), IMPACT)
    np.testing.assert_allclose(frac, [0.002, 0.004], rtol=0, atol=TOL)
    with pytest.raises(ValueError, match="trade_value_usd"):
        impact_fraction(np.array([-1.0]), np.array([1e6]), np.array([0.04]), IMPACT)
    with pytest.raises(ValueError, match="trade_value_usd"):
        impact_fraction(np.array([np.nan]), np.array([1e6]), np.array([0.04]), IMPACT)


def _daily(closes: list[float], volumes: list[float]) -> pd.DataFrame:
    close = np.asarray(closes, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range(pd.Timestamp("2024-01-01", tz="UTC"), periods=len(close), freq="1D")
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close),
            "low": np.minimum(open_, close),
            "close": close,
            "volume": np.asarray(volumes, dtype=float),
            "tradable": np.ones(len(close), dtype=bool),
        },
        index=pd.DatetimeIndex(index, name="time"),
    )


HAND_CLOSES = [100.0, 110.0, 99.0, 108.9, 108.9, 119.79]
HAND_VOLUMES = [10.0, 20.0, 30.0, 10.0, 40.0, 5.0]


def test_impact_inputs_match_hand_values() -> None:
    candles = _daily(HAND_CLOSES, HAND_VOLUMES)
    adv, sigma = impact_inputs(candles, IMPACT)
    # dollar volume close * volume = [1000, 2200, 2970, 1089, 4356, 598.95]
    # 2-day median over (t_i - 2d, t_i] (rows i-1, i), full once t_i - 2d >= t_0 (i >= 2):
    #   med[2] = (2200 + 2970) / 2 = 2585, med[3] = (2970 + 1089) / 2 = 2029.5,
    #   med[4] = (1089 + 4356) / 2 = 2722.5
    # A fill at row j's open sees med[j - 1]: adv = [nan, nan, nan, 2585, 2029.5, 2722.5]
    np.testing.assert_allclose(
        adv, [np.nan, np.nan, np.nan, 2585.0, 2029.5, 2722.5], rtol=0, atol=1e-9
    )
    # log returns r1 = log 1.1, r2 = log 0.9, r3 = log 1.1, r4 = 0, r5 = log 1.1.
    # 2-sample std (ddof=1) = |a - b| / sqrt(2): sd[2] = sd[3] = log(1.1 / 0.9) / sqrt(2),
    #   sd[4] = log(1.1) / sqrt(2). sigma[j] = sd[j - 1].
    a = np.log(1.1 / 0.9) / np.sqrt(2)
    b = np.log(1.1) / np.sqrt(2)
    np.testing.assert_allclose(sigma, [np.nan, np.nan, np.nan, a, a, b], rtol=0, atol=TOL)


def test_impact_fraction_on_warm_up_rows_is_the_cap() -> None:
    candles = _daily(HAND_CLOSES, HAND_VOLUMES)
    adv, sigma = impact_inputs(candles, IMPACT)
    frac = impact_fraction(np.full(6, 100.0), adv, sigma, IMPACT)
    assert frac[:3].tolist() == [0.05, 0.05, 0.05]
    # Row 3: 0.5 * log(1.1 / 0.9) / sqrt(2) * sqrt(100 / 2585), well under the cap.
    expected = 0.5 * np.log(1.1 / 0.9) / np.sqrt(2) * np.sqrt(100 / 2585)
    assert frac[3] == pytest.approx(expected, rel=1e-12)


def test_zero_volume_window_is_charged_the_cap() -> None:
    candles = _daily(HAND_CLOSES, [10.0, 20.0, 0.0, 0.0, 40.0, 5.0])
    adv, sigma = impact_inputs(candles, IMPACT)
    assert adv[4] == 0.0  # median(0, 0) over rows 2..3, seen by the fill at row 4
    frac = impact_fraction(np.full(6, 100.0), adv, sigma, IMPACT)
    assert frac[4] == 0.05


def test_adv_is_the_universe_trailing_dollar_volume_one_bar_earlier() -> None:
    candles = make_candles(120, seed=6, interval="1d")
    model = ImpactModel(coef=1.0, aum_usd=1e5, adv_days=10, vol_days=5, cap=0.1)
    adv, _ = impact_inputs(candles, model)
    tdv = trailing_dollar_volume({"X": candles}, window_days=10)["X"].to_numpy()
    # Full windows from own row 10 (t_10 - 10d = t_0) on, seen one bar later.
    assert np.isnan(adv[:11]).all()
    np.testing.assert_allclose(adv[11:], tdv[10:-1], rtol=1e-12, atol=0)


@pytest.mark.parametrize("j", [1, 5, 30, 150, 299])
def test_impact_inputs_are_causal_under_perturbation(j: int) -> None:
    """Changing bar j's own high/low/close/volume, or anything later, never changes the impact
    charged at j's open."""
    candles = make_candles(300, seed=8, interval="1d", vol=0.03)
    model = ImpactModel(coef=0.7, aum_usd=1e4, adv_days=7, vol_days=10, cap=0.2)
    a_adv, a_sig = impact_inputs(candles, model)
    b_adv, b_sig = impact_inputs(perturb_after(candles, j - 1), model)  # rows >= j change
    assert np.array_equal(a_adv[: j + 1], b_adv[: j + 1], equal_nan=True)
    assert np.array_equal(a_sig[: j + 1], b_sig[: j + 1], equal_nan=True)
    q = np.full(300, 5_000.0)
    fa = impact_fraction(q, a_adv, a_sig, model)
    fb = impact_fraction(q, b_adv, b_sig, model)
    assert np.array_equal(fa[: j + 1], fb[: j + 1])
    if j + 1 < 300 and j > model.vol_days:
        assert a_sig[j + 1] != b_sig[j + 1]  # bar j is first used for the fill at j + 1


def test_bar_j_own_volume_and_range_do_not_move_impact_at_j() -> None:
    candles = make_candles(80, seed=9, interval="1d")
    model = ImpactModel(coef=1.0, aum_usd=1e4, adv_days=5, vol_days=5, cap=0.5)
    base_adv, base_sig = impact_inputs(candles, model)
    j = 40
    bumped = candles.copy()
    for col, factor in (("volume", 50.0), ("close", 1.3), ("high", 1.5), ("low", 0.5)):
        bumped.iloc[j, bumped.columns.get_loc(col)] *= factor
    adv, sig = impact_inputs(bumped, model)
    assert np.array_equal(adv[: j + 1], base_adv[: j + 1], equal_nan=True)
    assert np.array_equal(sig[: j + 1], base_sig[: j + 1], equal_nan=True)
    assert adv[j + 1] != base_adv[j + 1] and sig[j + 1] != base_sig[j + 1]


def test_segments_restart_the_impact_warm_up() -> None:
    candles = make_candles(60, seed=10, interval="1d")
    model = ImpactModel(coef=1.0, aum_usd=1e4, adv_days=3, vol_days=4, cap=0.5)
    s = 25
    adv, sig = impact_inputs(candles, model, segment_starts=candles.index[[s]])
    whole_adv, whole_sig = impact_inputs(candles, model)
    tail_adv, tail_sig = impact_inputs(candles.iloc[s:], model)
    assert np.array_equal(adv[:s], whole_adv[:s], equal_nan=True)
    assert np.array_equal(sig[:s], whole_sig[:s], equal_nan=True)
    assert np.array_equal(adv[s:], tail_adv, equal_nan=True)
    assert np.array_equal(sig[s:], tail_sig, equal_nan=True)
    assert np.isnan(adv[s]) and np.isnan(sig[s])  # nothing from the old market leaks in
    with pytest.raises(ValueError, match="segment_starts"):
        impact_inputs(candles, model, segment_starts=pd.DatetimeIndex(["2000-01-01"], tz="UTC"))


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("coef", -0.1),
        ("coef", float("nan")),
        ("aum_usd", 0.0),
        ("aum_usd", float("inf")),
        ("adv_days", 0),
        ("adv_days", 2.5),
        ("adv_days", True),
        ("vol_days", 1),
        ("cap", 1.0),
        ("cap", -0.01),
        ("cap", float("nan")),
    ],
)
def test_validate_impact_names_the_bad_field(field: str, bad: object) -> None:
    kwargs: dict[str, object] = {
        "coef": 0.5,
        "aum_usd": 1e6,
        "adv_days": 30,
        "vol_days": 30,
        "cap": 0.05,
    }
    kwargs[field] = bad
    with pytest.raises(ValueError, match=field):
        validate_impact(ImpactModel(**kwargs))  # type: ignore[arg-type]
    validate_impact(ImpactModel(coef=0.0, aum_usd=1.0, adv_days=1, vol_days=2, cap=0.0))
    with pytest.raises(ValueError, match="ImpactModel"):
        validate_impact(CostModel())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- impact stress


IMPACT_OK = ImpactModel(coef=0.5, aum_usd=1e6, adv_days=30, vol_days=20, cap=0.05)


def test_impact_scaled_scales_coef_and_cap_only() -> None:
    for factor in (2.0, 4, 0.5):
        out = IMPACT_OK.scaled(factor)
        assert out.coef == 0.5 * factor and out.cap == 0.05 * factor
        assert (out.aum_usd, out.adv_days, out.vol_days) == (1e6, 30, 20)
    assert IMPACT_OK.scaled(1.0) == IMPACT_OK
    # Stressed impact charges proportionally more where the cap does not bind.
    q, adv, sigma = np.array([1e4]), np.array([1e8]), np.array([0.02])
    base = impact_fraction(q, adv, sigma, IMPACT_OK)
    np.testing.assert_allclose(impact_fraction(q, adv, sigma, IMPACT_OK.scaled(2.0)), 2 * base)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), True, "2"])
def test_impact_scaled_rejects_bad_factors(bad: object) -> None:
    with pytest.raises(ValueError, match="factor"):
        IMPACT_OK.scaled(bad)  # type: ignore[arg-type]


def test_impact_scaled_rejects_a_stressed_cap_of_one_or_more() -> None:
    with pytest.raises(ValueError, match=r"impact\.cap"):
        IMPACT_OK.scaled(20.0)  # cap 0.05 * 20 = 1.0


def test_validate_impact_with_costs_rejects_slippage_plus_impact_of_one_or_more() -> None:
    vol_scaled = CostModel(slippage_rate=0.001, slippage_vol_mult=0.1, slippage_cap=0.6)
    with pytest.raises(ValueError, match=r"costs\.slippage_cap.*impact\.cap"):
        validate_impact(ImpactModel(0.5, 1e6, 30, 20, cap=0.4), vol_scaled)  # exactly 1
    with pytest.raises(ValueError, match=r"costs\.slippage_cap.*impact\.cap"):
        validate_impact(ImpactModel(0.5, 1e6, 30, 20, cap=0.5), vol_scaled)
    validate_impact(ImpactModel(0.5, 1e6, 30, 20, cap=0.39), vol_scaled)
    validate_impact(ImpactModel(0.5, 1e6, 30, 20, cap=0.5), None)  # costs not given: no check
    # Flat slippage (vol_mult 0) never reaches the cap, so the default "uncapped"
    # slippage_cap = 1.0 is fine and slippage_rate is the bound that matters.
    flat = CostModel(slippage_rate=0.3, slippage_vol_mult=0.0)
    validate_impact(ImpactModel(0.5, 1e6, 30, 20, cap=0.6), flat)
    with pytest.raises(ValueError, match=r"costs\.slippage_rate.*impact\.cap"):
        validate_impact(ImpactModel(0.5, 1e6, 30, 20, cap=0.7), flat)
    with pytest.raises(ValueError, match="CostModel"):
        validate_impact(IMPACT_OK, "costs")  # type: ignore[arg-type]
