"""Known-value tests for the price/volume registry features.

Hand-calculated vectors are the same ones the web chart's indicator tests use
(``web/src/indicators/*.test.ts``) wherever the web has that indicator, so the chart and the
lab agree. Tolerances are stated per test: exact where every intermediate is a short sum of
small binary fractions, 1e-12 where pandas' smoothing divides by (1 - alpha) + alpha or a
square root / log enters, and 1e-9 relative against brute-force loops (the same bound the
web tests use) because the loops round in a different order.
"""

from __future__ import annotations

import math
from types import MappingProxyType

import numpy as np
import pandas as pd
import pytest

from lab.registry import REGISTRY, fill_params, get_feature
from lab.types import CANDLE_COLUMNS
from tests.synthetic import candles_from_rows, make_candles

TIGHT = 1e-12


def closes(values: list[float], volume: list[float] | None = None) -> pd.DataFrame:
    """Flat bars (open = high = low = close) like the web tests' ``closes`` helper."""
    frame = candles_from_rows([(v, v, v, v) for v in values])
    if volume is not None:
        frame["volume"] = np.asarray(volume, dtype=float)
    return frame


def run(name: str, candles: pd.DataFrame, **params: float) -> pd.DataFrame:
    return REGISTRY[name].compute(candles, params)


def valid(series: pd.Series) -> list[float]:
    return series.dropna().tolist()


# --------------------------------------------------------------------------- references


def ref_ema(xs: np.ndarray, n: int) -> np.ndarray:
    """Plain loop: SMA seed of the first n values, then alpha*x + (1-alpha)*prev."""
    out = np.full(len(xs), np.nan)
    alpha = 2 / (n + 1)
    prev = 0.0
    for i in range(len(xs)):
        if i == n - 1:
            prev = sum(xs[:n]) / n
            out[i] = prev
        elif i >= n:
            prev = alpha * xs[i] + (1 - alpha) * prev
            out[i] = prev
    return out


def ref_rsi(c: np.ndarray, n: int) -> np.ndarray:
    """Brute-force Wilder RSI (the web test's naiveRsi)."""
    out = np.full(len(c), np.nan)
    avg_gain = avg_loss = 0.0
    for i in range(1, len(c)):
        ch = c[i] - c[i - 1]
        g, loss = max(ch, 0.0), max(-ch, 0.0)
        if i < n:
            continue
        if i == n:
            chs = np.diff(c[: n + 1])
            avg_gain = float(np.clip(chs, 0, None).sum()) / n
            avg_loss = float(np.clip(-chs, 0, None).sum()) / n
        else:
            avg_gain = (avg_gain * (n - 1) + g) / n
            avg_loss = (avg_loss * (n - 1) + loss) / n
        if avg_loss == 0:
            out[i] = 100.0
        elif avg_gain == 0:
            out[i] = 0.0
        else:
            out[i] = 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def assert_close_rel(actual: np.ndarray, expected: np.ndarray, rel: float) -> None:
    assert np.array_equal(np.isnan(actual), np.isnan(expected))
    mask = ~np.isnan(expected)
    assert np.all(np.abs(actual[mask] - expected[mask]) <= rel * np.abs(expected[mask]) + rel)


# --------------------------------------------------------------------------- sma / ema


def test_sma_matches_hand_calculated_values() -> None:
    # web sma.test.ts: period 3 over 1..6 -> 2, 3, 4, 5. Exact: sums of small integers.
    out = run("sma", closes([1, 2, 3, 4, 5, 6]), period=3)["value"]
    assert out.iloc[:2].isna().all()
    assert valid(out) == [2.0, 3.0, 4.0, 5.0]


def test_ema_matches_web_hand_calculated_values() -> None:
    # web ema.test.ts: period 3 (alpha 0.5), seed (1+2+3)/3 = 2, then 3, 4, 5, 7.5.
    out = run("ema", closes([1, 2, 3, 4, 5, 6, 10]), period=3)["value"]
    assert out.iloc[:2].isna().all()
    np.testing.assert_allclose(valid(out), [2, 3, 4, 5, 7.5], rtol=0, atol=TIGHT)


def test_ema_matches_brute_force_reference() -> None:
    # 1e-9 relative, as the web test: alpha*x + (1-alpha)*prev rounds differently.
    candles = make_candles(300, seed=11)
    c = candles["close"].to_numpy()
    for period in (1, 5, 20, 50):
        out = run("ema", candles, period=period)["value"].to_numpy()
        assert_close_rel(out, ref_ema(c, period), 1e-9)


def test_ema_first_value_is_at_bar_period_minus_one() -> None:
    out = run("ema", make_candles(100, seed=12))["value"]
    assert out.first_valid_index() == out.index[19]


# --------------------------------------------------------------------------- rsi


def test_rsi_matches_web_period_two_example() -> None:
    # web rsi.test.ts: closes 10, 11, 13, 12, 15 -> 100, 60, 100 - 100/8.5 from bar 2.
    out = run("rsi", closes([10, 11, 13, 12, 15]), period=2)["value"].to_numpy()
    assert np.isnan(out[:2]).all()
    assert out[2] == 100.0
    assert out[3] == pytest.approx(60.0, abs=TIGHT)
    assert out[4] == pytest.approx(100 - 100 / 8.5, abs=TIGHT)


def test_rsi_matches_brute_force_reference() -> None:
    # 1e-9 absolute on a 0-100 scale, as the web test: (prev*(n-1)+x)/n vs prev + (x-prev)/n.
    candles = make_candles(300, seed=42)
    c = candles["close"].to_numpy()
    for period in (2, 5, 14, 30):
        out = run("rsi", candles, period=period)["value"].to_numpy()
        expected = ref_rsi(c, period)
        assert np.array_equal(np.isnan(out), np.isnan(expected))
        assert np.nanmax(np.abs(out - expected)) < 1e-9


def test_rsi_is_100_on_rising_and_0_on_falling_closes() -> None:
    up = run("rsi", closes([100 + 1.5 * i for i in range(40)]), period=14)["value"]
    down = run("rsi", closes([200 - 1.5 * i for i in range(40)]), period=14)["value"]
    assert valid(up) == [100.0] * 26
    assert valid(down) == [0.0] * 26


def test_rsi_stays_within_0_and_100() -> None:
    out = run("rsi", make_candles(500, seed=3, vol=0.03), period=3)["value"].dropna()
    assert out.between(0.0, 100.0).all()


# --------------------------------------------------------------------------- atr


def test_atr_matches_hand_calculated_wilder_values() -> None:
    # TR: bar0 h-l = 2; bar1 max(2, |12-10|, |10-10|) = 2; bar2 max(3.5, 0.5, |8-11|=3) = 3.5;
    # bar3 max(4, |13-9|, 0) = 4; bar4 gap up max(1, |15-12|, |14-12|) = 3.
    # Period 3: seed (2+2+3.5)/3 = 2.5; 2.5 + (4-2.5)/3 = 3.0; 3.0 + (3-3)/3 = 3.0.
    # 1e-12: pandas' ewm divides by (1 - alpha) + alpha, which need not be exactly 1.
    candles = candles_from_rows(
        [(10, 11, 9, 10), (10, 12, 10, 11), (11, 11.5, 8, 9), (9, 13, 9, 12), (14, 15, 14, 14.5)]
    )
    out = run("atr", candles, period=3)["value"].to_numpy()
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [2.5, 3.0, 3.0], rtol=0, atol=TIGHT)


def test_atr_period_one_is_true_range() -> None:
    candles = candles_from_rows([(10, 11, 9, 10), (10, 12, 10, 11), (11, 11.5, 8, 9)])
    out = run("atr", candles, period=1)["value"].to_numpy()
    np.testing.assert_allclose(out, [2.0, 2.0, 3.5], rtol=0, atol=TIGHT)


# --------------------------------------------------------------------------- bollinger


def test_bollinger_matches_web_hand_calculated_values() -> None:
    # web bollinger.test.ts: period 3, mult 2 over 1, 2, 3, 4, 8 with POPULATION stdev:
    # sd sqrt(2/3), sqrt(2/3), sqrt(14/3). 1e-12: rolling variance and sqrt round.
    out = run("bollinger", closes([1, 2, 3, 4, 8]), period=3, mult=2)
    assert list(out.columns) == ["mid", "upper", "lower"]
    sd = np.sqrt([2 / 3, 2 / 3, 14 / 3])
    mid = np.array([2.0, 3.0, 5.0])
    np.testing.assert_allclose(valid(out["mid"]), mid, rtol=0, atol=TIGHT)
    np.testing.assert_allclose(valid(out["upper"]), mid + 2 * sd, rtol=0, atol=TIGHT)
    np.testing.assert_allclose(valid(out["lower"]), mid - 2 * sd, rtol=0, atol=TIGHT)
    # The web test's six-decimal published numbers.
    np.testing.assert_allclose(valid(out["upper"]), [3.6329932, 4.6329932, 9.3204938], atol=1e-6)


# --------------------------------------------------------------------------- macd


def test_macd_matches_web_hand_calculated_fractions() -> None:
    # web macd.test.ts: fast 2 / slow 3 / signal 2 over 1, 2, 3, 5, 4, 6, 8, 7.
    # 1e-12 (the web test's 12 digits): fractions with denominators up to 11664.
    out = run("macd", closes([1, 2, 3, 5, 4, 6, 8, 7]), fast=2, slow=3, signal=2)
    macd = [1 / 2, 2 / 3, 11 / 36, 103 / 216, 881 / 1296, 2491 / 7776]
    signal = [7 / 12, 43 / 108, 73 / 162, 391 / 648, 4837 / 11664]
    assert out["macd"].first_valid_index() == out.index[2]
    assert out["signal"].first_valid_index() == out.index[3]
    np.testing.assert_allclose(valid(out["macd"]), macd, rtol=0, atol=TIGHT)
    np.testing.assert_allclose(valid(out["signal"]), signal, rtol=0, atol=TIGHT)
    np.testing.assert_allclose(
        valid(out["hist"]), np.array(macd[1:]) - np.array(signal), rtol=0, atol=TIGHT
    )


def test_macd_line_is_ema_difference() -> None:
    candles = make_candles(200, seed=5)
    out = run("macd", candles)
    diff = run("ema", candles, period=12)["value"] - run("ema", candles, period=26)["value"]
    assert np.array_equal(out["macd"].to_numpy(), diff.to_numpy(), equal_nan=True)


# --------------------------------------------------------------------------- donchian / roc


def test_donchian_matches_hand_calculated_values_and_includes_bar_t() -> None:
    candles = candles_from_rows([(5, 6, 4, 5), (5, 9, 5, 8), (8, 8, 2, 3), (3, 4, 3, 4)])
    out = run("donchian", candles, period=2)
    assert valid(out["upper"]) == [9.0, 9.0, 8.0]
    assert valid(out["lower"]) == [4.0, 2.0, 2.0]
    # Window includes bar t, so close never exceeds the same bar's upper.
    big = make_candles(300, seed=8)
    assert (big["close"] <= run("donchian", big)["upper"].fillna(np.inf)).all()


def test_roc_is_percent_change_over_period_bars() -> None:
    c = closes([100, 110, 121, 99])
    one = run("roc", c, period=1)["value"].to_numpy()
    two = run("roc", c, period=2)["value"].to_numpy()
    # 1e-12: one division and a multiply by 100 on short decimals.
    np.testing.assert_allclose(one[1:], [10.0, 10.0, -100 * 22 / 121], rtol=0, atol=TIGHT)
    np.testing.assert_allclose(two[2:], [21.0, -10.0], rtol=0, atol=TIGHT)
    assert np.isnan(one[0]) and np.isnan(two[:2]).all()


def test_roc_with_period_longer_than_history_is_all_nan() -> None:
    assert run("roc", closes([1, 2, 3]), period=5)["value"].isna().all()


# --------------------------------------------------------------------------- vol / zscore


def test_realized_vol_is_sample_stdev_of_log_returns_not_annualised() -> None:
    # log returns 0.1, 0.2, -0.1; period 2 sample stdev: |0.1-0.2|/sqrt2, |0.2+0.1|/sqrt2.
    # 1e-12: exp/log round trip.
    c = closes(list(100 * np.exp(np.cumsum([0.0, 0.1, 0.2, -0.1]))))
    out = run("realized_vol", c, period=2)["value"].to_numpy()
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [0.1 / math.sqrt(2), 0.3 / math.sqrt(2)], atol=TIGHT)


def test_zscore_matches_hand_values_and_bollinger() -> None:
    # 1, 2, 3, 4, 8 period 3: (3-2)/sqrt(2/3), (4-3)/sqrt(2/3), (8-5)/sqrt(14/3). 1e-12: sqrt.
    c = closes([1, 2, 3, 4, 8])
    out = run("zscore", c, period=3)["value"]
    expected = [1 / math.sqrt(2 / 3), 1 / math.sqrt(2 / 3), 3 / math.sqrt(14 / 3)]
    np.testing.assert_allclose(valid(out), expected, rtol=0, atol=TIGHT)
    big = make_candles(300, seed=9)
    bb = run("bollinger", big, period=20, mult=2)
    via_bb = (big["close"] - bb["mid"]) / ((bb["upper"] - bb["mid"]) / 2)
    np.testing.assert_allclose(
        run("zscore", big, period=20)["value"].dropna(), via_bb.dropna(), atol=1e-9
    )


def test_zscore_is_zero_on_a_flat_window() -> None:
    out = run("zscore", closes([5.0] * 10), period=4)["value"]
    assert valid(out) == [0.0] * 7


def test_volume_sma_matches_web_hand_calculated_values() -> None:
    # web volumeMa.test.ts: period 2 over volumes 10, 20, 30, 0 -> 15, 25, 15.
    out = run("volume_sma", closes([1, 1, 1, 1], volume=[10, 20, 30, 0]), period=2)["value"]
    assert valid(out) == [15.0, 25.0, 15.0]


# --------------------------------------------------------------------------- registry API


def test_registry_is_read_only_and_keyed_by_spec_name() -> None:
    assert isinstance(REGISTRY, MappingProxyType)
    with pytest.raises(TypeError):
        REGISTRY["sma"] = REGISTRY["ema"]  # type: ignore[index]
    for name, spec in REGISTRY.items():
        assert spec.name == name


def test_registry_has_the_planned_features() -> None:
    expected = {
        "sma",
        "ema",
        "rsi",
        "atr",
        "bollinger",
        "macd",
        "donchian",
        "roc",
        "realized_vol",
        "zscore",
        "volume_sma",
        "regime_state",
        "regime_signal",
    }
    assert expected <= set(REGISTRY)


def test_every_spec_has_a_short_description_and_valid_defaults() -> None:
    for spec in REGISTRY.values():
        assert 20 <= len(spec.description) <= 320, spec.name
        assert spec.description.count(". ") <= 1, spec.name  # one or two sentences
        assert spec.outputs and len(set(spec.outputs)) == len(spec.outputs)
        for p in spec.params:
            assert p.low <= p.default <= p.high, (spec.name, p.name)
            assert p.description, (spec.name, p.name)
            assert p.name not in CANDLE_COLUMNS


def test_get_feature_returns_the_spec() -> None:
    assert get_feature("rsi") is REGISTRY["rsi"]


def test_get_feature_names_unknown_feature_and_close_matches() -> None:
    with pytest.raises(ValueError, match=r"unknown feature 'emaa'.*'ema'") as info:
        get_feature("emaa")
    assert "available features" in str(info.value)


def test_fill_params_fills_defaults_and_casts_ints() -> None:
    filled = fill_params(REGISTRY["bollinger"], {"period": 30.0})
    assert filled == {"period": 30, "mult": 2.0}
    assert type(filled["period"]) is int
    assert type(filled["mult"]) is float


@pytest.mark.parametrize(
    ("params", "pattern"),
    [
        ({"length": 14}, r"'rsi' has no parameter\(s\) \['length'\]"),
        ({"period": 14.5}, r"'period' must be a whole number"),
        ({"period": 0}, r"'period'=0 is out of range \[1, 1000\]"),
        ({"period": 5000}, r"out of range"),
        ({"period": True}, r"must be a number"),
        ({"period": "14"}, r"must be a number"),
        ({"period": float("nan")}, r"must be finite"),
    ],
)
def test_fill_params_rejects_bad_params_with_specific_messages(
    params: dict[str, float], pattern: str
) -> None:
    with pytest.raises(ValueError, match=pattern):
        fill_params(REGISTRY["rsi"], params)


def test_compute_validates_params_when_called_directly() -> None:
    with pytest.raises(ValueError, match="out of range"):
        REGISTRY["sma"].compute(make_candles(50), {"period": -3})


def test_warmup_uses_defaults_for_missing_params() -> None:
    assert REGISTRY["macd"].warmup({}) == 26 + 9 - 2
    assert REGISTRY["sma"].warmup({"period": 50}) == 49


# --------------------------------------------------------------------------- gaps / untradable


def gapped_candles() -> pd.DataFrame:
    """Absent bars (dropped rows) plus zero-volume tradable=False rows."""
    candles = make_candles(400, seed=21)
    drop = np.r_[50:58, 200:230, 301]
    candles = candles.drop(candles.index[drop])
    flag = candles.index[[10, 11, 120, 250]]
    candles.loc[flag, "volume"] = 0.0
    candles.loc[flag, "tradable"] = False
    return candles


def test_features_handle_gaps_and_untradable_rows() -> None:
    candles = gapped_candles()
    for name, spec in REGISTRY.items():
        out = spec.compute(candles, {})
        assert out.index.equals(candles.index), name
        assert out.iloc[spec.warmup({}) :].notna().all().all(), name


def test_windows_count_rows_not_wall_clock_time() -> None:
    # A gap is never forward-filled: the feature on the gapped frame equals the feature on
    # the same rows laid out on a regular grid.
    candles = gapped_candles()
    regular = candles.copy()
    regular.index = make_candles(len(candles)).index
    for name, spec in REGISTRY.items():
        a = spec.compute(candles, {}).to_numpy()
        b = spec.compute(regular, {}).to_numpy()
        assert np.array_equal(a, b, equal_nan=True), name


def test_features_on_empty_and_short_frames() -> None:
    empty = make_candles(5).iloc[:0]
    short = make_candles(5)
    for name, spec in REGISTRY.items():
        out = spec.compute(empty, {})
        assert list(out.columns) == list(spec.outputs) and len(out) == 0, name
        assert spec.compute(short, {}).index.equals(short.index), name
