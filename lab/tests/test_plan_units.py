"""Plan units (quant-review round 4, C1): no prices or levels in plans, enforced server-side.

``close > 50000`` is a price level: on a trending market it is a date switch (True only in the
periods when BTC traded above 50k), which let a plan pick history by calendar and reuse the
holdout (probe_q4c: EMA 24/120 ``and close > 50000`` had design correlation 0.38 with a
holdout-checked EMA 29/150 but holdout correlation 0.895). The validator tags every expression
with a dimension (price-scale, volume-scale, dimensionless) and rejects comparisons of unlike
units, so a price or volume compared with a fixed number never validates. Offline: seeded
synthetic candles; the scale table is checked against the registry by homogeneity (prices x8
scale price-scale outputs x8, volume x4 scales volume-scale outputs x4, dimensionless outputs
do not move)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from lab.plan import validate_plan
from lab.plan.units import (
    DIMENSIONLESS,
    FEATURE_SCALES,
    PRICE,
    VOLUME,
    describe,
    output_scale,
)
from lab.registry import REGISTRY, fill_params
from tests.synthetic import make_candles

FEATURES: dict[str, Any] = {
    "ema_fast": {"ema": {"period": 20}},
    "ema_slow": {"ema": {"period": 50}},
    "volume_sma": {"volume_sma": {"period": 20}},
    "rsi": {"rsi": {"period": 14}},
}
# Uses every feature so the "defined but unused" check stays quiet; every clause is valid.
EXIT = "crosses_below(ema_fast, ema_slow) and volume_sma > 0 and rsi > 0"
MACD: dict[str, Any] = {"m": {"macd": {}}, "ema_fast": {"ema": {"period": 20}}}
MACD_EXIT = "m.hist < 0 and ema_fast > 0"


def plan(entry: str, exit_: str = EXIT, features: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": "units",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": features or FEATURES,
        "long_entry": entry,
        "long_exit": exit_,
        "risk": {"position_size_pct": 100},
    }


def _plan_for(entry: str) -> dict[str, Any]:
    return plan(entry, MACD_EXIT, MACD) if "m." in entry else plan(entry)


def entry_errors(entry: str) -> list[str]:
    parsed, errors = validate_plan(_plan_for(entry))
    assert parsed is None, f"{entry!r} should be rejected"
    return [e.message for e in errors if e.path == "long_entry"]


def accepted(entry: str) -> bool:
    parsed, errors = validate_plan(_plan_for(entry))
    assert not errors, errors
    return parsed is not None


# --------------------------------------------------------------------------- the required cases


@pytest.mark.parametrize(
    ("entry", "scaled", "kind"),
    [
        ("close > 50000", "close", "price"),
        ("volume > 1000", "volume", "volume"),
        ("ema_fast > 30000", "ema_fast", "price"),
    ],
)
def test_a_price_or_volume_compared_with_a_fixed_number_is_rejected(
    entry: str, scaled: str, kind: str
) -> None:
    (msg,) = entry_errors(entry)
    assert msg.startswith(f"`{entry}` compares a {kind}-scale value (`{scaled}`) with a fixed")
    assert f"that is a {kind} level" in msg
    assert "hidden date switch" in msg and "must not contain prices or levels" in msg
    assert "ratio" in msg


def test_the_rejection_suggests_a_ratio_built_from_the_plans_own_names() -> None:
    (msg,) = entry_errors("ema_fast > 30000")
    assert "`ema_fast / ema_slow > 1.02` or `ema_fast > ema_slow`" in msg
    (msg,) = entry_errors("close > 50000")
    assert "`close / ema_fast > 1.02` or `close > ema_fast`" in msg
    (msg,) = entry_errors("volume > 1000")
    assert "`volume / volume_sma > 2` or `volume > 2 * volume_sma`" in msg
    # following the suggestion gives a valid plan
    assert accepted("close / ema_fast > 1.02") and accepted("volume > 2 * volume_sma")


@pytest.mark.parametrize(
    "entry",
    [
        "close / ema_slow > 1.02",
        "close > ema_slow",
        "volume > 4 * volume_sma",
        "rsi < 30",
        "crosses_above(ema_fast, ema_slow)",
    ],
)
def test_scale_free_rules_stay_valid(entry: str) -> None:
    assert accepted(entry)


def test_probe_q4c_price_level_filter_no_longer_validates() -> None:
    """The probe's plan: an EMA cross gated by ``close > K`` (BTC's holdout never traded below
    58k, so the gate selected design history by date)."""
    for k in (30000, 50000, 60000):
        q4c = {
            "name": "x",
            "symbol": "BTCUSDT",
            "interval": "4h",
            "features": {"a": {"ema": {"period": 24}}, "b": {"ema": {"period": 120}}},
            "long_entry": f"crosses_above(a, b) and close > {k}",
            "long_exit": "crosses_below(a, b)",
            "risk": {"position_size_pct": 100},
        }
        parsed, errors = validate_plan(q4c)
        assert parsed is None
        (err,) = errors
        assert err.path == "long_entry"
        assert err.message.startswith(f"`close > {k}` compares a price-scale value (`close`)")
        assert "`close / a > 1.02` or `close > a`" in err.message


# --------------------------------------------------------------------------- more arithmetic


@pytest.mark.parametrize(
    "entry",
    [
        "crosses_above(close, 50000)",  # a cross of a level is still a level
        "crosses_below(volume, 500)",
        "prev(close, 3) < 40000",  # prev keeps the scale; the lag is not a level
        "close / 50000 > 1",  # price / number is still price-scale
        "50000 / close < 1",  # 1/price is not dimensionless
        "close - ema_slow > 500",  # a price difference is price-scale
        "-close < -50000",
        "m.macd > 100",  # MACD lines are price differences
        "close * volume > 1e9",  # a notional level
        "volume / close > 3",
    ],
)
def test_hidden_levels_in_arithmetic_are_rejected(entry: str) -> None:
    (msg,) = entry_errors(entry)
    assert "with a fixed number" in msg and "level" in msg


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ("close + 100 > ema_slow", "adds a price-scale value (`close`) and a dimensionless one"),
        ("close * 0 + 50000 < close", "adds a price-scale value (`close * 0`)"),
        ("ema_slow - 250 < close", "subtracts a price-scale value (`ema_slow`)"),
        ("close + volume > ema_fast", "adds a price-scale value (`close`) and a volume-scale"),
    ],
)
def test_adding_unlike_scales_is_rejected(entry: str, fragment: str) -> None:
    (msg,) = entry_errors(entry)
    assert fragment in msg
    assert "+ and - need the same scale" in msg


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ("close > rsi", "compares a price-scale value (`close`) with a dimensionless one (`rsi`)"),
        ("volume > ema_fast", "compares a volume-scale value (`volume`) with a price-scale one"),
        ("crosses_above(rsi, ema_fast)", "crosses a price-scale value (`ema_fast`)"),
    ],
)
def test_comparing_unlike_scales_is_rejected(entry: str, fragment: str) -> None:
    (msg,) = entry_errors(entry)
    assert fragment in msg and "units differ" in msg


@pytest.mark.parametrize(
    "entry",
    [
        "m.hist > 0",  # 0 fits any scale: a sign test
        "ema_fast < 0 or rsi > 30",  # a zero no-op clause is still legal (family guard's job)
        "30 < rsi < 70",
        "prev(close, 1) < close",
        "crosses_below(rsi, 30)",
        "crosses_above(m.hist, 0)",
        "(close - ema_slow) / ema_slow > 0.02",
        "close > ema_slow * 1.02",
        "close - ema_slow > 2 * (ema_fast - ema_slow)",
        "volume / volume_sma > 1.5 and rsi < 40",
        "ema_fast / prev(ema_fast, 3) > 1.01",
        "rsi / 100 < 0.3",
    ],
)
def test_like_with_like_and_ratios_are_accepted(entry: str) -> None:
    assert accepted(entry)


def test_the_exit_rule_is_checked_too() -> None:
    parsed, errors = validate_plan(plan("rsi < 30", exit_="close > 65000 and " + EXIT))
    assert parsed is None
    assert [e.path for e in errors] == ["long_exit"]
    assert "`close > 65000` compares a price-scale value" in errors[0].message


def test_bounded_features_compared_with_numbers_are_fine() -> None:
    feats = {
        "z": {"zscore": {"period": 20}},
        "roc": {"roc": {"period": 10}},
        "vol": {"realized_vol": {"period": 20}},
        "rs": {"regime_state": {}},
    }
    p, errors = validate_plan(
        plan("z < -2 and roc > 1 and vol < 0.05", exit_="rs < 0", features=feats)
    )
    assert not errors and p is not None
    feats = {"rg": {"regime_signal": {}}, "atr": {"atr": {"period": 14}}}
    p, errors = validate_plan(plan("rg > 0.2", exit_="atr / close > 0.03", features=feats))
    assert not errors and p is not None
    p, errors = validate_plan(plan("atr > 500", exit_="rg < 0", features=feats))
    assert p is None and "`atr > 500` compares a price-scale value (`atr`)" in errors[0].message


# --------------------------------------------------------------------------- the scale table


def test_every_registry_output_has_a_declared_scale_and_nothing_else() -> None:
    assert set(FEATURE_SCALES) == set(REGISTRY)
    for name, spec in REGISTRY.items():
        assert set(FEATURE_SCALES[name]) == set(spec.outputs), name
        for out in spec.outputs:
            assert output_scale(name, out) in (PRICE, VOLUME, DIMENSIONLESS)
    assert output_scale("not_a_feature", "value") is None


def test_declared_scales_match_the_registry_by_homogeneity() -> None:
    """Multiply prices by 8 and volume by 4 (powers of two, exact in floating point): each
    output must scale by 8 (price), 4 (volume) or 1 (dimensionless) exactly as declared."""
    candles = make_candles(1200, seed=11, vol=0.02, drift=0.0004)
    scaled = candles.copy()
    for col in ("open", "high", "low", "close"):
        scaled[col] = candles[col] * 8.0
    scaled["volume"] = candles["volume"] * 4.0
    factor = {PRICE: 8.0, VOLUME: 4.0, DIMENSIONLESS: 1.0}
    for name, spec in REGISTRY.items():
        params = fill_params(spec, {})
        base = spec.compute(candles, params)
        moved = spec.compute(scaled, params)
        for out in spec.outputs:
            a, b = base[out].to_numpy(), moved[out].to_numpy()
            ok = np.isfinite(a) & np.isfinite(b)
            assert ok.sum() > 100, (name, out)
            want = factor[FEATURE_SCALES[name][out]]
            np.testing.assert_allclose(b[ok], want * a[ok], rtol=1e-9, atol=1e-9, err_msg=name)
            if want != 1.0:  # and it is not secretly dimensionless (e.g. all zeros)
                assert np.abs(a[ok]).max() > 0, (name, out)


def test_describe_names_compound_units() -> None:
    assert describe(PRICE) == "price-scale"
    assert describe(VOLUME) == "volume-scale"
    assert describe(DIMENSIONLESS) == "dimensionless"
    assert describe((1, 1)) == "price x volume"
    assert describe((-1, 0)) == "1/price"
    assert describe((-1, 1)) == "volume/price"
    assert describe((2, 0)) == "price^2"
