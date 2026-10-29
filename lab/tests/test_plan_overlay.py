"""Risk-overlay plan kind (PLAN.md Phase 15): validation, canonical form, compilation, engine
fills, and the no-look-ahead perturbation test.

An overlay is buy-and-hold of one symbol with one risk rule, ``flat_when``: true at bar t's
close -> flat from bar t+1's open, otherwise 100% invested. Exposure is 0 or 1 only. A NaN in
``flat_when`` keeps the overlay invested (it falls back to the base).

Results are historical and not forward-looking.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.engine.backtest import run_signals
from lab.plan import (
    MAX_OVERLAY_FEATURES,
    MAX_OVERLAY_PARAMS,
    OVERLAY_KIND,
    PlanValidationError,
    checked_expressions,
    compile_plan,
    is_overlay_plan,
    overlay_plan_json_schema,
    parse_plan,
    plan_hash,
    plan_json_schema,
    plan_to_dict,
    tunable_params,
    validate_plan,
    with_params,
)
from lab.plan.validator import gate_base_expressions
from lab.registry.derivatives import DRV_PREMIUM_CLOSE
from lab.research.logic import logic_hash
from lab.runner import (
    check_gate_base,
    derivatives_lookback_errors,
    gate_base_share,
    gate_base_trial_check,
    ungated_plan,
)
from lab.types import ZERO_COSTS, CostModel, Window
from tests.synthetic import candles_from_rows, make_candles, perturb_after, with_derivatives
from tests.test_runner import small_cfg

# --------------------------------------------------------------------------- fixtures

# Edge-plan hashes computed at HEAD (7680942) before the overlay kind existed. plan_to_dict of a
# signal plan must stay byte-identical, so these never move.
EDGE_EMA_CROSS = {
    "name": "ema_cross",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"ema_fast": {"ema": {"period": 20}}, "ema_slow": {"ema": {"period": 50}}},
    "long_entry": "ema_fast > ema_slow",
    "long_exit": "ema_fast < ema_slow",
}
EDGE_EMA_CROSS_HASH = "cd9895ddc404f7e3db5e16eb9c15d3c6fa99c52e46eea3e425621c39b8a9f192"
EDGE_GATED = {
    "name": "gated",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"ema_slow": {"ema": {"period": 50}}, "fz": {"funding_zscore": {}}},
    "long_entry": "close > ema_slow and fz < 2",
    "long_exit": "close < ema_slow",
    "risk": {"stop_loss_pct": 3},
}
EDGE_GATED_HASH = "420bcab78ebc1264ade9a65f4e08b65e31aa90bfe7a49b8e2729d477d022a6f8"


def overlay(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "kind": "overlay",
        "name": "sma_trend_overlay",
        "description": "flat while close is below its slow SMA",
        "symbol": "BTCUSDT",
        "interval": "1d",
        "features": {"sma_slow": {"sma": {"period": 20}}},
        "flat_when": "close < sma_slow",
    }
    raw.update(changes)
    return {k: v for k, v in raw.items() if v is not None}


THREE = {
    "sma_slow": {"sma": {"period": 20}},
    "pr": {"premium_mean_rank": {"period": 7}},
    "vr": {"vol_rank": {"period": 10}},
}
THREE_RULE = "close < sma_slow or pr > 0.9 or vr > 0.9"


def errors_of(raw: dict[str, Any] | str) -> list[str]:
    plan, errors = validate_plan(raw)
    assert plan is None
    return [f"{e.path}: {e.message}" for e in errors]


# --------------------------------------------------------------------------- edge unchanged


def test_edge_plan_hashes_are_unchanged_from_head() -> None:
    a = parse_plan(EDGE_EMA_CROSS)
    b = parse_plan(EDGE_GATED)
    assert a.kind == "signal" and b.kind == "signal"
    assert "kind" not in plan_to_dict(a)
    assert plan_hash(a) == EDGE_EMA_CROSS_HASH
    assert plan_hash(b) == EDGE_GATED_HASH


def test_edge_plans_keep_every_derivatives_gating_rule() -> None:
    exit_gate = {**EDGE_GATED, "long_exit": "close < ema_slow or fz > 2"}
    assert any("long_exit: uses derivatives feature" in e for e in errors_of(exit_gate))
    only_gate = {**EDGE_GATED, "long_entry": "fz < 2"}
    assert any("uses only derivatives feature" in e for e in errors_of(only_gate))


def test_a_stray_kind_on_an_edge_plan_names_the_kinds() -> None:
    (err,) = errors_of({**EDGE_EMA_CROSS, "kind": "signal"})
    assert err.startswith("kind: ") and '"kind": "overlay"' in err and "Omit `kind`" in err


# --------------------------------------------------------------------------- parse + canonical


def test_overlay_parses_to_a_plan_with_the_documented_fields() -> None:
    plan = parse_plan(overlay())
    assert plan.kind == "overlay"
    assert plan.long_exit == "close < sma_slow"
    assert plan.long_entry == "not (close < sma_slow)"
    assert plan.risk.position_size_pct == 100.0
    assert plan.risk.stop_loss_pct is None and plan.risk.take_profit_pct is None
    assert plan.symbol == "BTCUSDT" and plan.interval == "1d"


def test_plan_to_dict_round_trips_and_has_exactly_the_overlay_keys() -> None:
    plan = parse_plan(overlay(features=THREE, flat_when=THREE_RULE))
    data = plan_to_dict(plan)
    assert list(data) == [
        "kind",
        "name",
        "description",
        "symbol",
        "interval",
        "features",
        "flat_when",
    ]
    assert data["kind"] == OVERLAY_KIND and data["flat_when"] == THREE_RULE
    assert parse_plan(data) == plan
    assert parse_plan(json.dumps(data)) == plan
    assert plan_hash(parse_plan(data)) == plan_hash(plan)


def test_overlay_hash_differs_from_the_same_rule_as_an_edge_plan() -> None:
    ov = parse_plan(overlay())
    edge = parse_plan(
        {
            "name": ov.name,
            "description": ov.description,
            "symbol": ov.symbol,
            "interval": ov.interval,
            "features": {"sma_slow": {"sma": {"period": 20}}},
            "long_entry": ov.long_entry,
            "long_exit": ov.long_exit,
        }
    )
    assert edge.kind == "signal"
    assert plan_hash(edge) != plan_hash(ov)


def test_is_overlay_plan() -> None:
    raw = overlay()
    assert is_overlay_plan(raw)
    assert is_overlay_plan(json.dumps(raw))
    assert is_overlay_plan(parse_plan(raw))
    assert not is_overlay_plan(EDGE_EMA_CROSS)
    assert not is_overlay_plan(parse_plan(EDGE_EMA_CROSS))
    assert not is_overlay_plan(json.dumps({"kind": "portfolio"}))
    assert not is_overlay_plan("{not json")
    assert not is_overlay_plan(None)
    assert not is_overlay_plan(["kind", "overlay"])


def test_checked_expressions_returns_not_flat_when_and_flat_when() -> None:
    plan = parse_plan(overlay(flat_when="crosses_below(close, sma_slow) or prev(close, 2) < 0"))
    entry, exit_ = checked_expressions(plan)
    assert exit_.source == plan.long_exit
    assert entry.source == f"not ({plan.long_exit})"
    assert entry.lag == exit_.lag == 2


def test_tunable_params_and_with_params_round_trip() -> None:
    plan = parse_plan(overlay(features=THREE, flat_when=THREE_RULE))
    assert set(tunable_params(plan)) == {"sma_slow.period", "pr.period", "vr.period"}
    moved = with_params(plan, {"sma_slow.period": 30, "vr.period": 12.4})
    assert moved.kind == "overlay" and moved.long_exit == THREE_RULE
    assert {f.alias: f.params["period"] for f in moved.features} == {
        "sma_slow": 30,
        "pr": 7,
        "vr": 12,
    }
    assert with_params(moved, {"sma_slow.period": 20, "vr.period": 10}) == plan
    unknown = re.escape("unknown tunable param `risk.stop_loss_pct`")
    with pytest.raises(PlanValidationError, match=unknown):
        with_params(plan, {"risk.stop_loss_pct": 3})


def test_logic_hash_ignores_name_alias_params_and_literals() -> None:
    a = parse_plan(overlay())
    b = parse_plan(
        overlay(
            name="other",
            features={"slow": {"sma": {"period": 200}}},
            flat_when="close < slow",
        )
    )
    c = parse_plan(overlay(flat_when="close > sma_slow"))
    assert logic_hash(a) == logic_hash(b) == logic_hash(plan_to_dict(a))
    assert logic_hash(a) != logic_hash(c)


def test_overlay_json_schema_has_the_overlay_shape_and_caps() -> None:
    schema = overlay_plan_json_schema()
    props = schema["properties"]
    assert set(props) == {
        "kind",
        "name",
        "description",
        "symbol",
        "interval",
        "features",
        "flat_when",
    }
    assert props["kind"] == {"const": "overlay"}
    assert props["features"]["maxProperties"] == MAX_OVERLAY_FEATURES == 3
    assert f"at most {MAX_OVERLAY_PARAMS} params" in props["features"]["description"]
    assert MAX_OVERLAY_PARAMS == 3
    assert "risk" not in props and "long_entry" not in props
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"kind", "name", "symbol", "interval", "features"} | {
        "flat_when"
    }
    assert "NOT flat" in props["flat_when"]["description"]
    # the edge schema keeps its own shape
    edge = plan_json_schema()
    assert "kind" not in edge["properties"] and edge["properties"]["features"]["maxProperties"] == 4


# --------------------------------------------------------------------------- caps + rules


def test_three_features_and_three_params_are_accepted() -> None:
    plan, errors = validate_plan(overlay(features=THREE, flat_when=THREE_RULE))
    assert errors == [] and plan is not None
    plan, errors = validate_plan(
        overlay(features={"macd": {"macd": {}}}, flat_when="macd.hist < 0")
    )
    assert errors == [] and plan is not None  # macd: 3 params (defaults count)


def test_a_fourth_feature_is_rejected() -> None:
    feats = {**THREE, "rsi": {"rsi": {}}}
    errs = errors_of(overlay(features=feats, flat_when=THREE_RULE + " or rsi > 80"))
    assert any(e.startswith("features: has 4 features; use 1 to 3") for e in errs), errs
    assert any("overlays use published parameters, untuned" in e for e in errs)


def test_more_than_three_params_is_rejected_defaults_included() -> None:
    feats = {"macd": {"macd": {}}, "sma_slow": {"sma": {}}}
    errs = errors_of(overlay(features=feats, flat_when="macd.hist < 0 or close < sma_slow"))
    (err,) = errs
    assert "4 tunable params in total (macd: 3, sma_slow: 1)" in err
    assert "the limit is 3" in err


def test_registry_features_only() -> None:
    errs = errors_of(overlay(features={"m": {"magic_crash_detector": {}}}, flat_when="m > 0"))
    assert any("unknown feature `magic_crash_detector`" in e for e in errs)


def test_price_and_volume_levels_are_rejected() -> None:
    errs = errors_of(overlay(flat_when="close < 50000 or sma_slow < 0"))
    assert any("flat_when" in e and "price-scale" in e for e in errs), errs
    errs = errors_of(
        overlay(features={"v": {"volume_sma": {}}}, flat_when="v > 1000"),
    )
    assert any("volume-scale" in e for e in errs), errs


def test_no_risk_key_and_no_entry_exit_keys() -> None:
    errs = errors_of(overlay(risk={"position_size_pct": 50}))
    assert errs == [
        "risk: an overlay has no `risk` key: it holds 100% of equity in the symbol or is flat "
        "(exposure 0 or 1 only; half exposure or stops would need an engine change and are out "
        "of scope). Remove it"
    ]
    errs = errors_of(overlay(long_entry="close > sma_slow", long_exit="close < sma_slow"))
    assert len(errs) == 2 and all("write its one risk rule as `flat_when`" in e for e in errs)
    errs = errors_of(overlay(short_entry="close < sma_slow"))
    assert errs[0].startswith("short_entry: `short_entry` is not supported")


def test_exactly_one_symbol() -> None:
    errs = errors_of(overlay(symbol=["BTCUSDT", "ETHUSDT"]))
    assert errs == [
        'symbol: an overlay covers exactly one symbol: use one string, e.g. "symbol": '
        '"BTCUSDT", and submit one overlay per symbol'
    ]
    errs = errors_of(overlay(symbols=["BTCUSDT"]))
    assert any(e.startswith("symbols: an overlay covers exactly one symbol") for e in errs)
    errs = errors_of(overlay(symbol="BTC/USDT"))
    assert any('Use "BTCUSDT"' in e for e in errs)


def test_required_keys_and_unknown_keys() -> None:
    raw = overlay()
    del raw["flat_when"]
    (err,) = errors_of(raw)
    assert err.startswith("flat_when: missing required key `flat_when`")
    errs = errors_of(overlay(flat_whn="close < sma_slow"))
    assert any("unknown key `flat_whn`. Did you mean `flat_when`?" in e for e in errs)


def test_unused_feature_is_rejected() -> None:
    feats = {"sma_slow": {"sma": {}}, "rsi": {"rsi": {}}}
    (err,) = errors_of(overlay(features=feats))
    assert err.startswith("features.rsi: feature `rsi` is defined but not used in flat_when")


@pytest.mark.parametrize(
    "rule",
    [
        "pr > 0.9",
        "fz > 2 or pr > 0.9",
        "not (fz < 2)",
        "close < sma_slow or pr > 0.9",
        "close / sma_slow < 1 and fz > 1",
    ],
)
def test_derivatives_features_may_drive_flat_when(rule: str) -> None:
    feats = {
        "pr": {"premium_mean_rank": {"period": 7}},
        "fz": {"funding_zscore": {"period": 12}},
        "sma_slow": {"sma": {"period": 20}},
    }
    used = {k: v for k, v in feats.items() if k in rule}
    plan, errors = validate_plan(overlay(features=used, flat_when=rule))
    assert errors == [] and plan is not None, errors
    assert gate_base_expressions(plan) is None
    assert ungated_plan(plan) is None


# --------------------------------------------------------------------------- runner no-ops


def test_runner_gate_checks_are_no_ops_for_overlays(tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    # A price clause that is always true would be refused for an edge plan; an overlay has no
    # gate, so nothing is checked.
    plan = parse_plan(
        overlay(
            features={"fz": {"funding_zscore": {"period": 12}}, "sma_slow": {"sma": {}}},
            flat_when="close > 0 and fz > 2 or close < sma_slow",
        )
    )
    candles = with_derivatives(make_candles(400, seed=3, interval="1d"), seed=3, gaps=False)
    window = Window(candles.index[50], candles.index[-1], "design")
    assert gate_base_share(plan, candles, window) is None
    assert check_gate_base(plan, candles, window, cfg) is None
    assert gate_base_trial_check(candles, window, cfg)(plan) is None


def test_derivatives_lookback_cap_still_applies_to_overlays(tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    ok = parse_plan(
        overlay(features={"pr": {"premium_mean_rank": {"period": 7}}}, flat_when="pr > 0.9")
    )
    assert derivatives_lookback_errors(ok, cfg) == []
    long = parse_plan(
        overlay(features={"pr": {"premium_mean_rank": {"period": 31}}}, flat_when="pr > 0.9")
    )
    (err,) = derivatives_lookback_errors(long, cfg)
    assert err.path == "features.pr.premium_mean_rank.period" and "use <= 30" in err.message


# --------------------------------------------------------------------------- compile


CLOSES = [10.0, 11.0, 12.0, 11.0, 10.0, 9.0, 10.0, 12.0, 13.0, 12.0]


def tiny_candles() -> pd.DataFrame:
    rows = []
    for i, c in enumerate(CLOSES):
        o = CLOSES[i - 1] if i else c
        rows.append((o, max(o, c) + 0.5, min(o, c) - 0.5, c))
    return candles_from_rows(rows, interval="1d")


def test_hand_checked_overlay_signals_and_warmup() -> None:
    # sma3 from row 2: 11, 11.333, 11, 10, 9.667, 10.333, 11.667, 12.333
    # close < sma3 at rows 3 (11 < 11.33), 4 (10 < 11), 5 (9 < 10); row 9: 12 < 12.33
    plan = parse_plan(overlay(features={"s": {"sma": {"period": 3}}}, flat_when="close < s"))
    out = compile_plan(plan, tiny_candles())
    assert out.warmup_bars == 2
    exits = out.exits.to_numpy()
    entries = out.entries.to_numpy()
    assert np.flatnonzero(exits).tolist() == [3, 4, 5, 9]
    assert np.flatnonzero(entries).tolist() == [2, 6, 7, 8]
    assert not entries[:2].any() and not exits[:2].any()  # warm-up: neither (flat)
    assert (entries[2:] ^ exits[2:]).all()  # after warm-up exactly one each bar


def test_nan_in_flat_when_means_invested() -> None:
    candles = with_derivatives(make_candles(120, seed=5, interval="1d"), seed=5, gaps=False)
    candles.iloc[60:63, candles.columns.get_loc(DRV_PREMIUM_CLOSE)] = np.nan
    plan = parse_plan(
        overlay(features={"pr": {"premium_mean_rank": {"period": 2}}}, flat_when="pr > 0.0")
    )
    out = compile_plan(plan, candles)
    pr = out.features["pr"].to_numpy()
    nan_rows = np.flatnonzero(np.isnan(pr))
    w = out.warmup_bars
    assert w == 2 + 20 - 2
    gap_rows = [r for r in nan_rows if r >= w]
    assert gap_rows == [60, 61, 62, 63]  # 2-bar mean: the gap plus one bar after it
    # pr > 0 is true wherever pr is defined, so the overlay is flat except at the gap rows
    entries, exits = out.entries.to_numpy(), out.exits.to_numpy()
    assert entries[gap_rows].all() and not exits[gap_rows].any()
    defined = np.setdiff1d(np.arange(w, len(candles)), gap_rows)
    assert exits[defined].all() and not entries[defined].any()


# --------------------------------------------------------------------------- engine


def three_feature_candles(n: int = 600, seed: int = 41) -> pd.DataFrame:
    return with_derivatives(make_candles(n, seed=seed, interval="1d", vol=0.03), seed=seed)


def three_feature_plan() -> Any:
    return parse_plan(overlay(features=THREE, flat_when=THREE_RULE))


def exposure(candles: pd.DataFrame, costs: CostModel = ZERO_COSTS) -> tuple[Any, np.ndarray]:
    plan = three_feature_plan()
    sig = compile_plan(plan, candles)
    res = run_signals(candles, sig.entries, sig.exits, costs=costs, risk=plan.risk)
    return sig, res.position.to_numpy()


@pytest.mark.parametrize("costs", [ZERO_COSTS, CostModel()])
def test_exposure_is_zero_or_one_and_follows_the_rule_one_bar_later(costs: CostModel) -> None:
    candles = three_feature_candles()
    sig, pos = exposure(candles, costs)
    invested = pos > 0.5
    assert np.all((pos == 0.0) | (np.abs(pos - 1.0) < 1e-12)), np.unique(pos)
    flat = sig.exits.to_numpy()
    w = sig.warmup_bars
    assert not invested[: w + 1].any()  # nothing decided before the warm-up ends
    # exposure over bar k (held from bar k's open) = not flat_when at bar k-1's close
    assert np.array_equal(invested[w + 1 :], ~flat[w:-1])
    assert 0 < invested.mean() < 1  # the rule binds on this path and is not always flat


def test_fills_happen_at_the_next_bar_open() -> None:
    candles = three_feature_candles()
    plan = three_feature_plan()
    sig = compile_plan(plan, candles)
    res = run_signals(candles, sig.entries, sig.exits, costs=ZERO_COSTS, risk=plan.risk)
    idx = candles.index
    entries, exits = sig.entries.to_numpy(), sig.exits.to_numpy()
    assert res.trades
    for trade in res.trades:
        k = idx.get_loc(trade.entry_time)
        assert entries[k - 1] and trade.entry_price == candles["open"].iloc[k]
        if trade.exit_time is not None and trade.exit_reason == "signal":
            j = idx.get_loc(trade.exit_time)
            assert exits[j - 1] and trade.exit_price == candles["open"].iloc[j]


@pytest.mark.parametrize("t", [25, 60, 199, 300, 450, 597])
def test_perturbing_after_t_never_changes_exposure_at_or_before_t_plus_1(t: int) -> None:
    candles = three_feature_candles()
    shocked = perturb_after(candles, t)
    # Tradability of bar t+1 is known at its open, not a look-ahead; keep every bar tradable so
    # the comparison isolates information leaking from prices and derivatives after t.
    shocked["tradable"] = True
    sig_a, pos_a = exposure(candles)
    sig_b, pos_b = exposure(shocked)
    for name in ("entries", "exits"):
        a = getattr(sig_a, name).to_numpy()[: t + 1]
        b = getattr(sig_b, name).to_numpy()[: t + 1]
        assert np.array_equal(a, b), (name, t)
    assert np.array_equal(pos_a[: t + 2] > 0.5, pos_b[: t + 2] > 0.5), t
    assert sig_a.features.iloc[: t + 1].equals(sig_b.features.iloc[: t + 1])
    if t < 450:  # the shock has teeth: something later moves
        assert not np.array_equal(
            sig_a.features.to_numpy()[t + 1 :], sig_b.features.to_numpy()[t + 1 :], equal_nan=True
        )
