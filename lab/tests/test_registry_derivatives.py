"""Derivatives risk-filter features (PLAN.md Phase 12): known values, gaps, errors, contract.

Known values are hand-calculated on tiny frames. Pure pass-through features must be
bit-identical (tolerance 0). Computed features use ``rel=1e-12``: the hand values are exact
fractions and the only error is floating-point rounding in a handful of operations (pandas'
rolling moments for the z-score, one division for the percent changes).

The generic perturbation test in ``tests/test_registry_lookahead.py`` covers these features
on a frame with NaN gaps; the test here perturbs only the derivatives columns, so it isolates
the drv_* inputs. Results are historical and not forward-looking.
"""

from __future__ import annotations

import math
from types import MappingProxyType

import numpy as np
import pandas as pd
import pytest

from lab.plan import compile_plan, parse_plan, validate_plan
from lab.registry import REGISTRY, fill_params
from lab.registry.derivatives import (
    DERIVATIVES_COLUMNS,
    DERIVATIVES_FEATURE_SPECS,
    DERIVATIVES_FEATURES,
    DERIVATIVES_INPUTS,
    DRV_FUNDING_RATE,
    DRV_INDEX_CLOSE,
    DRV_LS_RATIO,
    DRV_MARK_CLOSE,
    DRV_OI,
    DRV_PREMIUM_CLOSE,
)
from tests.synthetic import make_candles, with_derivatives

NAN = math.nan
PHRASE = "exposure or crash-risk filter, not a direction signal"
REL = 1e-12


def frame(**columns: list[float]) -> pd.DataFrame:
    """Flat candles at 100 carrying the given drv_* columns (all lists the same length)."""
    n = len(next(iter(columns.values())))
    candles = make_candles(n, seed=1)
    for col, values in columns.items():
        candles[col] = np.asarray(values, dtype=float)
    return candles


def run(name: str, candles: pd.DataFrame, **params: float) -> np.ndarray:
    return REGISTRY[name].compute(candles, params)["value"].to_numpy()


def assert_values(got: np.ndarray, want: list[float]) -> None:
    assert len(got) == len(want)
    for i, (g, w) in enumerate(zip(got, want, strict=True)):
        if math.isnan(w):
            assert math.isnan(g), (i, g)
        else:
            assert g == pytest.approx(w, rel=REL, abs=1e-15), (i, g, w)


# --------------------------------------------------------------------------- interface


def test_columns_and_inputs_match_the_published_interface() -> None:
    assert DERIVATIVES_COLUMNS == (
        "drv_funding_rate",
        "drv_premium_close",
        "drv_mark_close",
        "drv_index_close",
        "drv_oi",
        "drv_ls_ratio",
    )
    assert dict(DERIVATIVES_INPUTS) == {
        "funding_rate": (DRV_FUNDING_RATE,),
        "funding_zscore": (DRV_FUNDING_RATE,),
        "premium_index": (DRV_PREMIUM_CLOSE,),
        "basis_pct": (DRV_MARK_CLOSE, DRV_INDEX_CLOSE),
        "oi_change": (DRV_OI,),
        "long_short_ratio": (DRV_LS_RATIO,),
    }
    assert isinstance(DERIVATIVES_INPUTS, MappingProxyType)
    with pytest.raises(TypeError):
        DERIVATIVES_INPUTS["funding_rate"] = ()  # type: ignore[index]
    assert frozenset(DERIVATIVES_INPUTS) == DERIVATIVES_FEATURES
    assert {s.name for s in DERIVATIVES_FEATURE_SPECS} == DERIVATIVES_FEATURES


def test_all_six_are_registered_with_single_value_outputs() -> None:
    for spec in DERIVATIVES_FEATURE_SPECS:
        assert REGISTRY[spec.name] is spec
        assert spec.outputs == ("value",)


def test_every_description_says_filter_not_direction_signal() -> None:
    for spec in DERIVATIVES_FEATURE_SPECS:
        assert PHRASE in spec.description, spec.name


def test_params_defaults_bounds_and_warmups() -> None:
    zs = REGISTRY["funding_zscore"]
    (period,) = zs.params
    assert (period.name, period.kind, period.default, period.low, period.high) == (
        "period",
        "int",
        180,
        12,
        1000,
    )
    assert zs.warmup({}) == 179 and zs.warmup({"period": 12}) == 11
    oi = REGISTRY["oi_change"]
    (period,) = oi.params
    assert (period.name, period.kind, period.default, period.low, period.high) == (
        "period",
        "int",
        6,
        1,
        1000,
    )
    assert oi.warmup({}) == 6 and oi.warmup({"period": 2}) == 2
    for name in ("funding_rate", "premium_index", "basis_pct", "long_short_ratio"):
        assert REGISTRY[name].params == ()
        assert REGISTRY[name].warmup({}) == 0
    with pytest.raises(ValueError, match=r"funding_zscore.*period"):
        fill_params(zs, {"period": 1})
    # Note 2: a population z over p <= 5 values never reaches 2, so the low bound is 12
    with pytest.raises(ValueError, match=r"funding_zscore.*period"):
        fill_params(zs, {"period": 11})
    assert fill_params(zs, {"period": 12})["period"] == 12


# --------------------------------------------------------------------------- known values


def test_funding_rate_passes_the_aligned_rate_through_unchanged() -> None:
    values = [1e-4, -2e-4, NAN, 3e-4, 3e-4]
    got = run("funding_rate", frame(drv_funding_rate=values))
    assert np.array_equal(got, np.asarray(values), equal_nan=True)  # tolerance 0


def test_funding_zscore_matches_hand_calculated_population_zscores() -> None:
    # In units of 1e-4 (the z-score is scale-free), period 12 (the registry's low bound):
    # t11 [1 x11, 13]: mean 2, pop var (11*1 + 11^2)/12 = 11 -> z = 11/sqrt(11) = sqrt(11), the
    #     sqrt(p - 1) ceiling of a population z (one outlier among p - 1 equal values)
    # t12 [1 x10, 13, 1]: mean 2, pop var 11 -> z = (1 - 2)/sqrt(11) = -1/sqrt(11)
    # t13 NaN in; t13..t24 hold the gap -> NaN
    # t25 [2 x11, 14]: mean 3, pop var (11*1 + 11^2)/12 = 11 -> z = sqrt(11)
    units = [1.0] * 11 + [13.0, 1.0, NAN] + [2.0] * 11 + [14.0]
    got = run("funding_zscore", frame(drv_funding_rate=[u * 1e-4 for u in units]), period=12)
    assert_values(
        got,
        [NAN] * 11 + [math.sqrt(11), -1 / math.sqrt(11)] + [NAN] * 12 + [math.sqrt(11)],
    )
    assert np.nanmax(np.abs(got)) <= math.sqrt(12 - 1) * (1 + 1e-12)


def test_funding_zscore_is_exactly_zero_on_a_flat_window_not_nan_or_inf() -> None:
    got = run("funding_zscore", frame(drv_funding_rate=[1e-4] * 16), period=12)
    assert np.isnan(got[:11]).all()
    assert (got[11:] == 0.0).all()


def test_funding_zscore_default_period_is_180_rows() -> None:
    candles = with_derivatives(make_candles(400, seed=4), seed=4, gaps=False)
    got = run("funding_zscore", candles)
    assert np.isnan(got[:179]).all() and np.isfinite(got[179:]).all()
    window = candles[DRV_FUNDING_RATE].to_numpy()[220:400]
    want = (window[-1] - window.mean()) / window.std(ddof=0)
    assert got[399] == pytest.approx(want, rel=1e-9)  # brute-force numpy, 180-term sums


def test_premium_index_passes_the_premium_close_through_unchanged() -> None:
    values = [5e-4, -1e-4, NAN, 0.0]
    got = run("premium_index", frame(drv_premium_close=values))
    assert np.array_equal(got, np.asarray(values), equal_nan=True)


def test_basis_pct_is_percent_mark_over_index() -> None:
    mark = [101.0, 99.0, NAN, 100.0, 50.0, 10.0, 100.5]
    index = [100.0, 100.0, 100.0, NAN, 0.0, -1.0, 100.0]
    got = run("basis_pct", frame(drv_mark_close=mark, drv_index_close=index))
    # 100*(101-100)/100 = 1; 100*(99-100)/100 = -1; NaN mark/index -> NaN; index 0 or < 0 -> NaN;
    # 100*0.5/100 = 0.5
    assert_values(got, [1.0, -1.0, NAN, NAN, NAN, NAN, 0.5])


def test_oi_change_is_trailing_percent_change_over_period_rows() -> None:
    oi = [100.0, 200.0, 110.0, 0.0, 220.0, 50.0, NAN, 300.0, 330.0]
    got = run("oi_change", frame(drv_oi=oi), period=2)
    # t2 110/100 -> +10; t3 0/200 -> -100; t4 220/110 -> +100; t5 past 0 -> NaN;
    # t6 current NaN -> NaN; t7 300/50 -> +500; t8 past NaN -> NaN (no forward fill)
    assert_values(got, [NAN, NAN, 10.0, -100.0, 100.0, NAN, NAN, 500.0, NAN])


def test_long_short_ratio_passes_the_ratio_through_unchanged() -> None:
    values = [1.5, 2.25, NAN, 0.8]
    got = run("long_short_ratio", frame(drv_ls_ratio=values))
    assert np.array_equal(got, np.asarray(values), equal_nan=True)


# --------------------------------------------------------------------------- gaps


@pytest.mark.parametrize("name", sorted(DERIVATIVES_FEATURES))
def test_a_gap_stays_nan_and_is_never_forward_filled(name: str) -> None:
    candles = with_derivatives(make_candles(80, seed=7), seed=7, gaps=False)
    for col in DERIVATIVES_INPUTS[name]:
        candles.iloc[40, candles.columns.get_loc(col)] = NAN
    params = {"period": 12} if REGISTRY[name].params else {}
    got = run(name, candles, **params)
    assert math.isnan(got[40]), name
    assert np.isfinite(got[53:]).all(), name  # windows clear of the gap recover


def test_a_derivatives_gap_does_not_touch_price_features() -> None:
    clean = with_derivatives(make_candles(200, seed=8), seed=8, gaps=False)
    gapped = with_derivatives(make_candles(200, seed=8), seed=8)
    for name in ("ema", "rsi", "atr"):
        a = REGISTRY[name].compute(clean, {}).to_numpy()
        b = REGISTRY[name].compute(gapped, {}).to_numpy()
        assert np.array_equal(a, b, equal_nan=True), name


# --------------------------------------------------------------------------- errors


@pytest.mark.parametrize("name", sorted(DERIVATIVES_FEATURES))
def test_missing_column_names_it_and_the_download_command(name: str) -> None:
    candles = make_candles(20, seed=2)
    first = DERIVATIVES_INPUTS[name][0]
    with pytest.raises(ValueError) as info:
        REGISTRY[name].compute(candles, {})
    message = str(info.value)
    assert repr(first) in message
    assert "uv run python -m lab.data.derivatives download" in message


def test_basis_names_the_index_column_when_only_mark_is_present() -> None:
    candles = frame(drv_mark_close=[100.0, 101.0])
    with pytest.raises(ValueError, match=DRV_INDEX_CLOSE):
        REGISTRY["basis_pct"].compute(candles, {})


# --------------------------------------------------------------------------- no look-ahead


@pytest.mark.parametrize("name", sorted(DERIVATIVES_FEATURES))
def test_perturbing_future_derivatives_leaves_the_past_bit_identical(name: str) -> None:
    candles = with_derivatives(make_candles(400, seed=9), seed=9)
    params = fill_params(REGISTRY[name], {"period": 20} if REGISTRY[name].params else {})
    base = run(name, candles, **params)
    rng = np.random.default_rng(5)
    for t in (0, 25, 199, 398):
        shocked = candles.copy()
        k = len(candles) - t - 1
        for col in DERIVATIVES_INPUTS[name]:
            future = shocked[col].to_numpy()[t + 1 :] * rng.uniform(0.2, 5.0, k)
            future[rng.random(k) < 0.1] = NAN
            shocked.iloc[t + 1 :, shocked.columns.get_loc(col)] = future
        got = run(name, shocked, **params)
        assert np.array_equal(base[: t + 1], got[: t + 1], equal_nan=True), (name, t)
        if t == 199:
            assert not np.array_equal(base[t + 1 :], got[t + 1 :], equal_nan=True), name


# --------------------------------------------------------------------------- compiled plan


def gated_plan() -> dict[str, object]:
    return {
        "name": "ema_trend_funding_gate",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {
            "ema_x": {"ema": {"period": 10}},
            "fz": {"funding_zscore": {"period": 20}},
        },
        "long_entry": "close > ema_x and fz < 2",
        "long_exit": "close < ema_x",
    }


def test_nan_funding_gives_no_entry_and_no_crash() -> None:
    candles = with_derivatives(make_candles(300, seed=12, vol=0.02), seed=12, gaps=False)
    candles.iloc[150:160, candles.columns.get_loc(DRV_FUNDING_RATE)] = NAN
    plan = parse_plan(gated_plan())
    out = compile_plan(plan, candles)
    entries = out.entries.to_numpy()
    fz = out.features["fz"].to_numpy()
    price_signal = (candles["close"] > out.features["ema_x"]).to_numpy()
    gap = np.isnan(fz) & (np.arange(len(candles)) >= out.warmup_bars)
    assert gap[150:179].all()  # the gap plus the 20-row windows that hold it
    assert price_signal[gap].any()  # the price rule alone would have entered in the gap
    assert not entries[gap].any()
    # elsewhere the gate only removes entries: entry = price signal and fz < 2
    ok = ~np.isnan(fz) & (np.arange(len(candles)) >= out.warmup_bars)
    assert np.array_equal(entries[ok], (price_signal & (fz < 2))[ok])
    # the exit never reads funding, so it still fires inside the gap
    assert out.exits.to_numpy()[gap].any()


def test_compiling_without_derivatives_data_raises_with_the_download_hint() -> None:
    plan = parse_plan(gated_plan())
    with pytest.raises(ValueError, match=r"lab\.data\.derivatives download"):
        compile_plan(plan, make_candles(100, seed=1))


def test_gated_plan_validates_against_the_real_registry() -> None:
    plan, errors = validate_plan(gated_plan())
    assert errors == [] and plan is not None
