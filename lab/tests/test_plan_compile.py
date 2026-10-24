"""Compiler: hand-checked signals, warm-up forcing, and the no-look-ahead perturbation test."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.plan import PlanValidationError, compile_plan, parse_plan
from lab.types import FeatureRef, FeatureSpec, ParamSpec, Plan
from tests.synthetic import candles_from_rows, make_candles, perturb_after
from tests.test_plan_validator import FAKE_REGISTRY, example_plan

# Hand-made closes; open = previous close so bars are realistic. sma3 from row 2:
# 11, 11.333, 11, 10, 9.667, 10.333, 11.667, 12.333
# close - sma3: 1, -0.333, -1, -1, 0.333, 1.667, 1.333, -0.333
CLOSES = [10.0, 11.0, 12.0, 11.0, 10.0, 9.0, 10.0, 12.0, 13.0, 12.0]


def tiny_candles() -> pd.DataFrame:
    rows = []
    for i, c in enumerate(CLOSES):
        o = CLOSES[i - 1] if i else c
        rows.append((o, max(o, c) + 0.5, min(o, c) - 0.5, c))
    return candles_from_rows(rows)


def sma_plan(entry: str, exit_: str) -> Plan:
    return parse_plan(
        {
            "name": "sma_cross",
            "symbol": "BTCUSDT",
            "interval": "4h",
            "features": {"sma": {"sma": {"period": 3}}},
            "long_entry": entry,
            "long_exit": exit_,
        },
        FAKE_REGISTRY,
    )


def test_hand_checked_cross_signals() -> None:
    plan = sma_plan("crosses_above(close, sma)", "crosses_below(close, sma)")
    out = compile_plan(plan, tiny_candles(), FAKE_REGISTRY)
    assert out.warmup_bars == 3  # sma warm-up 2 + 1 for crosses_*
    assert np.flatnonzero(out.entries.to_numpy()).tolist() == [6]
    assert np.flatnonzero(out.exits.to_numpy()).tolist() == [3, 9]
    assert out.entries.dtype == bool and out.exits.dtype == bool
    assert out.entries.index.equals(tiny_candles().index)
    assert list(out.features.columns) == ["sma"]
    assert out.features["sma"].iloc[3] == pytest.approx(34 / 3, abs=1e-12)


def test_warmup_forces_signals_false() -> None:
    # Row 2: sma valid (11) and close 12 > prev close 11, so the raw condition is True there;
    # warm-up = 2 (sma) + 1 (prev n) = 3 forces rows 0..2 False.
    plan = sma_plan("close > prev(close, 1) and sma > 0", "close < sma")
    out = compile_plan(plan, tiny_candles(), FAKE_REGISTRY)
    assert out.warmup_bars == 3
    assert out.entries.tolist() == [
        False, False, False, False, False, False, True, True, True, False,
    ]  # fmt: skip
    # close < sma3 at rows 3, 4, 5, 9 (row 2 is not below: 12 > 11)
    assert np.flatnonzero(out.exits.to_numpy()).tolist() == [3, 4, 5, 9]


def test_multi_output_feature_columns_are_alias_dot_output() -> None:
    plan = parse_plan(example_plan(), FAKE_REGISTRY)
    out = compile_plan(plan, make_candles(300, seed=3), FAKE_REGISTRY)
    assert list(out.features.columns) == [
        "ema_fast", "ema_slow", "rsi", "macd.macd", "macd.signal", "macd.hist",
    ]  # fmt: skip
    # ema_slow warm-up 99 dominates macd's 33; +1 for crosses_*.
    assert out.warmup_bars == 100
    assert not out.entries.iloc[:100].any() and not out.exits.iloc[:100].any()


def perturbation_plan() -> Plan:
    raw = example_plan()
    raw["features"]["ema_slow"]["ema"]["period"] = 50
    raw["long_entry"] = "ema_fast > ema_slow and crosses_below(rsi, 45) or prev(close, 3) < low"
    raw["long_exit"] = "crosses_above(rsi, 60) or macd.hist < 0 and close < prev(open, 2)"
    return parse_plan(raw, FAKE_REGISTRY)


@pytest.mark.parametrize("t", [60, 150, 151, 299, 398])
def test_no_lookahead_perturbation(t: int) -> None:
    plan = perturbation_plan()
    candles = make_candles(400, seed=7)
    base = compile_plan(plan, candles, FAKE_REGISTRY)
    moved = compile_plan(plan, perturb_after(candles, t), FAKE_REGISTRY)
    assert base.entries.any() and base.exits.any()  # the test has something to compare
    pd.testing.assert_series_equal(base.entries.iloc[: t + 1], moved.entries.iloc[: t + 1])
    pd.testing.assert_series_equal(base.exits.iloc[: t + 1], moved.exits.iloc[: t + 1])
    pd.testing.assert_frame_equal(
        base.features.iloc[: t + 1], moved.features.iloc[: t + 1], check_exact=True
    )
    assert not base.features.iloc[t + 1 :].equals(moved.features.iloc[t + 1 :])


def test_perturbation_harness_catches_a_leaky_feature() -> None:
    def leaky(c: pd.DataFrame, p: Mapping[str, float]) -> pd.DataFrame:
        return pd.DataFrame({"value": c["close"].shift(-1)}, index=c.index)

    reg = {
        **FAKE_REGISTRY,
        "peek": FeatureSpec("peek", "leaks", (), ("value",), leaky, lambda p: 0),
    }
    plan = parse_plan(
        {
            "name": "leak",
            "symbol": "BTCUSDT",
            "interval": "4h",
            "features": {"nxt": {"peek": {}}},
            "long_entry": "nxt > close",
            "long_exit": "nxt < close",
        },
        reg,
    )
    candles = make_candles(100, seed=1)
    t = 50
    base = compile_plan(plan, candles, reg)
    moved = compile_plan(plan, perturb_after(candles, t), reg)
    assert not base.features.iloc[: t + 1].equals(moved.features.iloc[: t + 1])


def test_compile_revalidates_hand_built_plan() -> None:
    bad = Plan(
        name="x",
        symbol="BTCUSDT",
        interval="4h",
        features=(FeatureRef("sma", "sma", {"period": 3}),),
        long_entry="__import__('os') > sma",
        long_exit="close < sma",
    )
    with pytest.raises(PlanValidationError, match="not an allowed function"):
        compile_plan(bad, tiny_candles(), FAKE_REGISTRY)


def test_compile_rejects_feature_breaking_contract() -> None:
    def wrong(c: pd.DataFrame, p: Mapping[str, float]) -> pd.DataFrame:
        return pd.DataFrame({"oops": c["close"]}, index=c.index)

    spec = FeatureSpec("bad", "wrong columns", (), ("value",), wrong, lambda p: 0)
    reg: dict[str, Any] = {**FAKE_REGISTRY, "bad": spec}
    plan = parse_plan(
        {
            "name": "x",
            "symbol": "BTCUSDT",
            "interval": "4h",
            "features": {"b": {"bad": {}}},
            "long_entry": "b > close",
            "long_exit": "b < close",
        },
        reg,
    )
    with pytest.raises(ValueError, match="returned columns"):
        compile_plan(plan, tiny_candles(), reg)


def test_params_are_passed_filled_to_compute() -> None:
    seen: list[dict[str, float]] = []

    def spy(c: pd.DataFrame, p: Mapping[str, float]) -> pd.DataFrame:
        seen.append(dict(p))
        return pd.DataFrame({"value": c["close"]}, index=c.index)

    spec = FeatureSpec(
        "spy", "records params", (ParamSpec("k", "int", 7, 1, 9, "k"),), ("value",), spy,
        lambda p: 0,
    )  # fmt: skip
    reg = {**FAKE_REGISTRY, "spy": spec}
    plan = parse_plan(
        {
            "name": "x",
            "symbol": "BTCUSDT",
            "interval": "4h",
            "features": {"s": {"spy": {}}},
            "long_entry": "s > open",
            "long_exit": "s < open",
        },
        reg,
    )
    compile_plan(plan, tiny_candles(), reg)
    assert seen == [{"k": 7}]
