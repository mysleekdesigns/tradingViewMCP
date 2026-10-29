"""Registry features added for risk overlays (PLAN.md Phase 15): ``vol_rank`` and
``premium_mean_rank``. Known values on hand-built series, a brute-force second implementation
of the expanding percentile rank, warm-up, gaps, and the perturbation (no-look-ahead) test.

Tolerance: ranks are ratios of integer counts, so the brute force must match exactly (the same
two integers divided); realized_vol inputs are compared at 1e-12 relative.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from lab.plan import validate_plan
from lab.registry import REGISTRY, fill_params
from lab.registry.derivatives import (
    DERIVATIVES_FEATURES,
    DERIVATIVES_INPUTS,
    DRV_PREMIUM_CLOSE,
    FILTER_NOTE,
)
from lab.registry.features import EXPANDING_RANK_MIN_HISTORY, expanding_pct_rank
from tests.synthetic import candles_from_rows, make_candles, perturb_after, with_derivatives

NAN = math.nan
MIN = EXPANDING_RANK_MIN_HISTORY


def brute_rank(x: np.ndarray, min_history: int) -> np.ndarray:
    """Second implementation: count, for each t, the valid past values at or below x[t]."""
    out = np.full(len(x), np.nan)
    for t in range(len(x)):
        if np.isnan(x[t]):
            continue
        past = x[: t + 1]
        past = past[~np.isnan(past)]
        if past.size >= min_history:
            out[t] = np.count_nonzero(past <= x[t]) / past.size
    return out


# --------------------------------------------------------------------------- the rank helper


def test_min_history_constant_is_documented_value() -> None:
    assert EXPANDING_RANK_MIN_HISTORY == 20


def test_expanding_rank_hand_values() -> None:
    x = np.array([NAN, 3.0, 1.0, NAN, 2.0, 2.0, 5.0, 1.0])
    got = expanding_pct_rank(x, 2)
    # valid history at each defined row: [3,1] -> 1/2; [3,1,2] -> 2/3; [3,1,2,2] -> 3/4 (ties
    # count as at or below); [..,5] -> 5/5; [3,1,2,2,5,1] -> 2/6
    want = [NAN, NAN, 0.5, NAN, 2 / 3, 0.75, 1.0, 2 / 6]
    assert np.array_equal(got, np.array(want), equal_nan=True)


def test_expanding_rank_is_in_zero_one_and_matches_brute_force() -> None:
    rng = np.random.default_rng(3)
    x = rng.normal(size=400)
    x[rng.random(400) < 0.05] = np.nan
    x[100:110] = 0.25  # ties
    got = expanding_pct_rank(x, MIN)
    assert np.array_equal(got, brute_rank(x, MIN), equal_nan=True)
    ok = ~np.isnan(got)
    assert (got[ok] > 0).all() and (got[ok] <= 1).all()
    # NaN until MIN valid values exist
    first_ok = int(np.flatnonzero(ok)[0])
    assert np.count_nonzero(~np.isnan(x[: first_ok + 1])) == MIN


def test_a_new_running_maximum_ranks_one() -> None:
    x = np.arange(1.0, 41.0)
    got = expanding_pct_rank(x, MIN)
    assert np.isnan(got[: MIN - 1]).all()
    assert (got[MIN - 1 :] == 1.0).all()
    y = -x  # a new running minimum ranks 1/count
    got = expanding_pct_rank(y, MIN)
    assert np.array_equal(got[MIN - 1 :], 1.0 / np.arange(MIN, 41))


# --------------------------------------------------------------------------- vol_rank


def test_vol_rank_spec() -> None:
    spec = REGISTRY["vol_rank"]
    (period,) = spec.params
    assert (period.name, period.kind, period.default, period.low) == ("period", "int", 30, 2)
    assert spec.outputs == ("value",)
    assert spec.warmup({}) == 30 + MIN - 1
    assert spec.warmup({"period": 5}) == 5 + MIN - 1
    assert "vol_rank" not in DERIVATIVES_FEATURES


def test_vol_rank_equals_the_rank_of_realized_vol() -> None:
    candles = make_candles(300, seed=12, interval="1d", vol=0.03)
    for period in (2, 10, 30):
        vol = REGISTRY["realized_vol"].compute(candles, {"period": period})["value"].to_numpy()
        got = REGISTRY["vol_rank"].compute(candles, {"period": period})["value"].to_numpy()
        assert np.array_equal(got, brute_rank(vol, MIN), equal_nan=True), period
        warm = REGISTRY["vol_rank"].warmup({"period": period})
        assert np.isnan(got[:warm]).all() and np.isfinite(got[warm:]).all()


def test_vol_rank_hand_built_regime() -> None:
    # 30 calm bars (+-0.1% alternating), then a stretch whose alternating moves grow by 2% per
    # bar (+2%, -4%, +6%, ...). With period 2, the volatility is |r_t - r_(t-1)| / sqrt(2), so
    # every bar from 30 on sets a new running maximum and ranks exactly 1 (it is at or above
    # every earlier value, itself included).
    closes = [100.0]
    for i in range(1, 40):
        step = 0.001 if i < 30 else 0.02 * (i - 29)
        closes.append(closes[-1] * (1 + step if i % 2 else 1 - step))
    rows = [(c, c * 1.5, c * 0.5, c) for c in closes]
    candles = candles_from_rows(rows, interval="1d")
    got = REGISTRY["vol_rank"].compute(candles, {"period": 2})["value"].to_numpy()
    assert np.isnan(got[: 2 + MIN - 1]).all() and np.isfinite(got[2 + MIN - 1 :]).all()
    assert (got[30:] == 1.0).all()


# --------------------------------------------------------------------------- premium_mean_rank


def test_premium_mean_rank_spec_and_derivatives_role() -> None:
    spec = REGISTRY["premium_mean_rank"]
    (period,) = spec.params
    assert (period.name, period.kind, period.default, period.low) == ("period", "int", 42, 1)
    assert spec.warmup({}) == 42 + MIN - 2
    assert spec.warmup({"period": 7}) == 7 + MIN - 2
    assert "premium_mean_rank" in DERIVATIVES_FEATURES
    assert DERIVATIVES_INPUTS["premium_mean_rank"] == (DRV_PREMIUM_CLOSE,)
    assert FILTER_NOTE in spec.description
    assert "exposure or crash-risk filter, not a direction signal" in spec.description


def test_a_7_day_window_plus_min_history_fits_30_derivatives_warmup_days_on_1d() -> None:
    spec = REGISTRY["premium_mean_rank"]
    assert spec.warmup({"period": 7}) + 1 <= 30  # first defined value inside 30 daily bars
    assert spec.warmup({}) + 1 <= 180  # default (7 days of 4h bars) inside 30 days of 4h bars


def premium_frame(values: list[float]) -> pd.DataFrame:
    rows = [(100.0, 101.0, 99.0, 100.0)] * len(values)
    candles = candles_from_rows(rows, interval="1d")
    candles[DRV_PREMIUM_CLOSE] = np.asarray(values, dtype=np.float64)
    return candles


def test_premium_mean_rank_hand_values() -> None:
    # premium = 0, 1, 2, ... ; the 2-bar mean is increasing, so once MIN means exist every rank
    # is 1 (a new running maximum); the next bar drops to -5 -> its mean is the lowest -> 1/n.
    values = [float(i) for i in range(30)] + [-100.0]
    candles = premium_frame(values)
    got = REGISTRY["premium_mean_rank"].compute(candles, {"period": 2})["value"].to_numpy()
    warm = 2 + MIN - 2
    assert np.isnan(got[:warm]).all()
    assert (got[warm:30] == 1.0).all()
    assert got[30] == 1.0 / 30  # 30 valid means (rows 1..30), the last is the lowest


def test_premium_mean_rank_matches_brute_force_with_gaps() -> None:
    candles = with_derivatives(make_candles(300, seed=4, interval="1d"), seed=4)
    prem = candles[DRV_PREMIUM_CLOSE].to_numpy()
    assert np.isnan(prem).any()  # the fixture has an archive gap
    for period in (1, 7, 42):
        mean = pd.Series(prem).rolling(period, min_periods=period).mean().to_numpy()
        got = REGISTRY["premium_mean_rank"].compute(candles, {"period": period})
        assert np.array_equal(got["value"].to_numpy(), brute_rank(mean, MIN), equal_nan=True)


def test_premium_gap_stays_nan_and_is_left_out_of_the_history() -> None:
    values = list(np.linspace(-1.0, 1.0, 60))
    values[40] = NAN
    got = (
        REGISTRY["premium_mean_rank"]
        .compute(premium_frame(values), {"period": 3})["value"]
        .to_numpy()
    )
    assert np.isnan(got[40:43]).all()  # every 3-bar window holding the gap
    assert np.isfinite(got[43:]).all()


def test_missing_premium_column_names_it() -> None:
    with pytest.raises(ValueError, match=DRV_PREMIUM_CLOSE):
        REGISTRY["premium_mean_rank"].compute(make_candles(30, seed=1), {})


def test_edge_plans_still_treat_premium_mean_rank_as_a_gate_only() -> None:
    base = {
        "name": "x",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {"ema_slow": {"ema": {}}, "pr": {"premium_mean_rank": {}}},
        "long_entry": "close > ema_slow and pr < 0.9",
        "long_exit": "close < ema_slow",
    }
    plan, errors = validate_plan(base)
    assert errors == [] and plan is not None
    _, errors = validate_plan({**base, "long_exit": "close < ema_slow or pr > 0.9"})
    assert any(e.path == "long_exit" and "derivatives feature" in e.message for e in errors)


# --------------------------------------------------------------------------- no look-ahead


@pytest.mark.parametrize("name", ["vol_rank", "premium_mean_rank"])
@pytest.mark.parametrize("params", [{}, {"period": 5}])
def test_perturbing_the_future_leaves_the_past_bit_identical(
    name: str, params: dict[str, float]
) -> None:
    candles = with_derivatives(make_candles(500, seed=21, interval="1d", vol=0.03), seed=21)
    spec = REGISTRY[name]
    p = fill_params(spec, params)
    base = spec.compute(candles, p)["value"].to_numpy()
    warm = spec.warmup(p)
    for t in sorted({0, 7, warm - 1, warm, warm + 1, 250, 498}):
        shocked = spec.compute(perturb_after(candles, t), p)["value"].to_numpy()
        assert np.array_equal(base[: t + 1], shocked[: t + 1], equal_nan=True), (name, t)
        if t == 250:
            assert not np.array_equal(base[t + 1 :], shocked[t + 1 :], equal_nan=True), name


@pytest.mark.parametrize("name", ["vol_rank", "premium_mean_rank"])
def test_truncating_the_end_gives_the_same_prefix(name: str) -> None:
    candles = with_derivatives(make_candles(400, seed=22, interval="1d"), seed=22)
    spec = REGISTRY[name]
    full = spec.compute(candles, {})["value"].to_numpy()
    for t in (80, 200, 399):
        cut = spec.compute(candles.iloc[: t + 1], {})["value"].to_numpy()
        assert np.array_equal(full[: t + 1], cut, equal_nan=True), (name, t)
