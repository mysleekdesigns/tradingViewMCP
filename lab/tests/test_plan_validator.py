"""Plan validator: fix-it errors at the right path, all errors collected, injection rejected.

Uses a small causal FAKE registry (defined here, imported by the other plan tests) so these
tests do not depend on the real ``lab.registry``.
"""

from __future__ import annotations

import ast
import builtins
import copy
import json
from collections.abc import Mapping
from typing import Any

import pandas as pd
import pytest

from lab.plan import (
    PlanValidationError,
    parse_plan,
    plan_hash,
    plan_json_schema,
    plan_to_dict,
    tunable_params,
    validate_plan,
    with_params,
)
from lab.types import FeatureSpec, ParamSpec, PlanError

# --------------------------------------------------------------------------- fake registry


def _sma(c: pd.DataFrame, p: Mapping[str, float]) -> pd.DataFrame:
    n = int(p["period"])
    return pd.DataFrame({"value": c["close"].rolling(n, min_periods=n).mean()}, index=c.index)


def _ema(c: pd.DataFrame, p: Mapping[str, float]) -> pd.DataFrame:
    n = int(p["period"])
    v = c["close"].ewm(span=n, adjust=False, min_periods=n).mean()
    return pd.DataFrame({"value": v}, index=c.index)


def _rsi(c: pd.DataFrame, p: Mapping[str, float]) -> pd.DataFrame:
    n = int(p["period"])
    d = c["close"].diff()
    up = d.clip(lower=0).rolling(n, min_periods=n).mean()
    dn = (-d).clip(lower=0).rolling(n, min_periods=n).mean()
    return pd.DataFrame({"value": 100 - 100 / (1 + up / dn)}, index=c.index)


def _macd(c: pd.DataFrame, p: Mapping[str, float]) -> pd.DataFrame:
    f, s, g = int(p["fast"]), int(p["slow"]), int(p["signal"])
    fast = c["close"].ewm(span=f, adjust=False, min_periods=f).mean()
    slow = c["close"].ewm(span=s, adjust=False, min_periods=s).mean()
    line = fast - slow
    sig = line.ewm(span=g, adjust=False, min_periods=g).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig}, index=c.index)


def _bbands(c: pd.DataFrame, p: Mapping[str, float]) -> pd.DataFrame:
    n, k = int(p["period"]), float(p["mult"])
    mid = c["close"].rolling(n, min_periods=n).mean()
    sd = c["close"].rolling(n, min_periods=n).std(ddof=0)
    return pd.DataFrame({"upper": mid + k * sd, "middle": mid, "lower": mid - k * sd})


def _period(default: float, high: float = 500) -> ParamSpec:
    return ParamSpec("period", "int", default, 2, high, "lookback in bars")


FAKE_REGISTRY: dict[str, FeatureSpec] = {
    "sma": FeatureSpec(
        "sma",
        "Simple moving average of close.",
        (_period(20),),
        ("value",),
        _sma,
        lambda p: int(p["period"]) - 1,
    ),
    "ema": FeatureSpec(
        "ema",
        "Exponential moving average of close.",
        (_period(20),),
        ("value",),
        _ema,
        lambda p: int(p["period"]) - 1,
    ),
    "rsi": FeatureSpec(
        "rsi",
        "Cutler RSI of close.",
        (_period(14, 100),),
        ("value",),
        _rsi,
        lambda p: int(p["period"]),
    ),
    "macd": FeatureSpec(
        "macd",
        "MACD line, signal and histogram.",
        (
            ParamSpec("fast", "int", 12, 2, 100, "fast EMA span"),
            ParamSpec("slow", "int", 26, 2, 200, "slow EMA span"),
            ParamSpec("signal", "int", 9, 2, 50, "signal EMA span"),
        ),
        ("macd", "signal", "hist"),
        _macd,
        lambda p: int(p["slow"]) + int(p["signal"]) - 2,
    ),
    "bbands": FeatureSpec(
        "bbands",
        "Bollinger bands.",
        (_period(20), ParamSpec("mult", "float", 2.0, 0.5, 5.0, "band width in stdevs")),
        ("upper", "middle", "lower"),
        _bbands,
        lambda p: int(p["period"]) - 1,
    ),
}


def example_plan() -> dict[str, Any]:
    """PLAN.md's example plan plus a multi-output feature (6 params: the limit)."""
    return {
        "name": "ema_trend_rsi_pullback",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {
            "ema_fast": {"ema": {"period": 20}},
            "ema_slow": {"ema": {"period": 100}},
            "rsi": {"rsi": {"period": 14}},
            "macd": {"macd": {}},
        },
        "long_entry": "ema_fast > ema_slow and crosses_below(rsi, 35)",
        "long_exit": "crosses_above(rsi, 65) or ema_fast < ema_slow or macd.hist < 0",
        "risk": {"stop_loss_pct": 3, "take_profit_pct": None, "position_size_pct": 100},
    }


def errors_of(raw: Mapping[str, Any] | str) -> list[PlanError]:
    plan, errors = validate_plan(raw, FAKE_REGISTRY)
    assert plan is None, "expected the plan to be rejected"
    assert errors
    return errors


def only_error(raw: Mapping[str, Any] | str, path: str) -> str:
    errors = errors_of(raw)
    assert [e.path for e in errors] == [path], errors
    return errors[0].message


def mutate(**changes: Any) -> dict[str, Any]:
    raw = example_plan()
    raw.update(changes)
    return raw


# --------------------------------------------------------------------------- valid plans


def test_example_plan_is_valid_and_fills_defaults() -> None:
    plan, errors = validate_plan(example_plan(), FAKE_REGISTRY)
    assert errors == []
    assert plan is not None
    assert [f.alias for f in plan.features] == ["ema_fast", "ema_slow", "rsi", "macd"]
    assert plan.features[3].params == {"fast": 12, "slow": 26, "signal": 9}
    assert plan.risk.stop_loss_pct == 3.0
    assert plan.risk.take_profit_pct is None
    assert plan.risk.position_size_pct == 100.0


def test_plan_md_example_text_is_valid() -> None:
    text = """{
      "name": "ema_trend_rsi_pullback",
      "symbol": "BTCUSDT", "interval": "4h",
      "features": { "ema_fast": {"ema": {"period": 20}}, "ema_slow": {"ema": {"period": 100}},
                    "rsi": {"rsi": {"period": 14}} },
      "long_entry": "ema_fast > ema_slow and crosses_below(rsi, 35)",
      "long_exit":  "crosses_above(rsi, 65) or ema_fast < ema_slow",
      "risk": { "stop_loss_pct": 3, "position_size_pct": 100 }
    }"""
    plan = parse_plan(text, FAKE_REGISTRY)
    assert plan.name == "ema_trend_rsi_pullback"


def test_risk_is_optional_and_defaults() -> None:
    raw = example_plan()
    del raw["risk"]
    plan = parse_plan(raw, FAKE_REGISTRY)
    assert plan.risk.position_size_pct == 100.0
    assert plan.risk.stop_loss_pct is None


def test_integral_float_accepted_for_int_param() -> None:
    raw = example_plan()
    raw["features"]["ema_fast"]["ema"]["period"] = 20.0
    plan = parse_plan(raw, FAKE_REGISTRY)
    assert plan.features[0].params["period"] == 20
    assert isinstance(plan.features[0].params["period"], int)


# --------------------------------------------------------------------------- top level


def test_bad_json_reports_one_error_at_root() -> None:
    msg = only_error('{"name": "x",', "$")
    assert "invalid JSON" in msg and "line 1" in msg


def test_nan_literal_in_json_rejected() -> None:
    assert only_error('{"name": NaN}', "$").startswith("invalid JSON")


def test_non_object_plan_rejected() -> None:
    assert "must be a JSON object" in only_error("[1, 2]", "$")


def test_unknown_top_level_key_suggests_fix() -> None:
    msg = only_error(mutate(long_entri="close > open"), "long_entri")
    assert "Did you mean `long_entry`?" in msg


def test_short_keys_explain_long_only() -> None:
    msg = only_error(mutate(short_entry="close < open"), "short_entry")
    assert "long/flat only" in msg


def test_missing_required_key_names_it_with_example() -> None:
    raw = example_plan()
    del raw["long_exit"]
    msg = only_error(raw, "long_exit")
    assert "missing required key" in msg and "ema_fast < ema_slow" in msg


def test_symbol_pattern_with_suggested_fix() -> None:
    msg = only_error(mutate(symbol="btc/usdt"), "symbol")
    assert 'Use "BTCUSDT"' in msg


def test_interval_must_be_known_with_suggestion() -> None:
    msg = only_error(mutate(interval="4H"), "interval")
    assert "Did you mean `4h`?" in msg and "1d" in msg


def test_name_must_be_non_empty_string() -> None:
    assert "non-empty string" in only_error(mutate(name=""), "name")


def test_multiple_errors_are_all_collected() -> None:
    raw = mutate(symbol="btc", interval="7h", long_entry="ema_fast >")
    raw["risk"]["stop_loss_pct"] = 80
    raw["features"]["ema_fast"]["ema"]["period"] = 1
    paths = {e.path for e in errors_of(raw)}
    assert paths == {
        "symbol",
        "interval",
        "long_entry",
        "risk.stop_loss_pct",
        "features.ema_fast.ema.period",
    }


def test_validation_error_str_lists_every_error() -> None:
    with pytest.raises(PlanValidationError) as info:
        parse_plan(mutate(symbol="x", interval="9h"), FAKE_REGISTRY)
    text = str(info.value)
    assert "2 plan errors" in text
    assert "- symbol:" in text and "- interval:" in text
    assert len(info.value.errors) == 2


# --------------------------------------------------------------------------- features


def test_feature_count_limit() -> None:
    raw = example_plan()
    raw["features"]["sma_x"] = {"sma": {}}
    errors = errors_of(raw)
    msgs = [e.message for e in errors if e.path == "features"]
    assert any("has 5 features; use 1 to 4" in m for m in msgs)
    assert any("7 tunable params in total" in m for m in msgs)


def test_empty_features_rejected() -> None:
    raw = mutate(features={}, long_entry="close > open", long_exit="close < open")
    assert "has 0 features" in only_error(raw, "features")


def test_param_limit_counts_defaults() -> None:
    raw = example_plan()
    raw["features"]["rsi"] = {"bbands": {}}  # 1 + 1 + 2 + 3 = 7 params
    raw["long_entry"] = "ema_fast > ema_slow and close < rsi.lower"
    raw["long_exit"] = "close > rsi.upper or macd.hist < 0"
    msg = only_error(raw, "features")
    assert "7 tunable params" in msg and "macd: 3" in msg and "limit is 6" in msg


def test_unknown_feature_suggests_registry_name() -> None:
    raw = example_plan()
    raw["features"]["rsi"] = {"rsii": {"period": 14}}
    msg = only_error(raw, "features.rsi.rsii")
    assert "Did you mean `rsi`?" in msg and "Registry features:" in msg


def test_unknown_param_suggests_name() -> None:
    raw = example_plan()
    raw["features"]["ema_fast"]["ema"] = {"perod": 20}
    msg = only_error(raw, "features.ema_fast.ema.perod")
    assert "Did you mean `period`?" in msg


def test_param_out_of_range_gives_bounds_and_fix() -> None:
    raw = example_plan()
    raw["features"]["ema_slow"]["ema"]["period"] = 1000
    msg = only_error(raw, "features.ema_slow.ema.period")
    assert "[2, 500]" in msg and "Try 500" in msg


def test_int_param_rejects_fraction() -> None:
    raw = example_plan()
    raw["features"]["ema_fast"]["ema"]["period"] = 20.5
    msg = only_error(raw, "features.ema_fast.ema.period")
    assert "whole number" in msg


@pytest.mark.parametrize("bad", ["20", True, None, [20]])
def test_param_must_be_numeric(bad: object) -> None:
    raw = example_plan()
    raw["features"]["ema_fast"]["ema"]["period"] = bad
    assert "must be a whole number" in only_error(raw, "features.ema_fast.ema.period")


def test_float_param_accepts_fraction() -> None:
    raw = example_plan()
    raw["features"]["macd"] = {"bbands": {"mult": 2.5}}
    raw["long_exit"] = "close > macd.upper"
    plan = parse_plan(raw, FAKE_REGISTRY)
    assert plan.features[3].params == {"period": 20, "mult": 2.5}


def test_feature_value_must_have_one_key() -> None:
    raw = example_plan()
    raw["features"]["ema_fast"] = {"ema": {"period": 20}, "sma": {"period": 20}}
    msg = only_error(raw, "features.ema_fast")
    assert "exactly one key" in msg and "['ema', 'sma']" in msg


def test_feature_shorthand_gets_concrete_fix() -> None:
    raw = example_plan()
    raw["features"]["rsi"] = {"period": 14}
    msg = only_error(raw, "features.rsi.period")
    assert "looks like a param" in msg
    assert '"rsi": {"rsi": {"period": 14}}' in msg


@pytest.mark.parametrize(
    ("alias", "fragment"),
    [("EmaFast", "lowercase"), ("close", "price input"), ("prev", "function"), ("and", "reserved")],
)
def test_bad_alias_rejected(alias: str, fragment: str) -> None:
    raw = example_plan()
    raw["features"][alias] = raw["features"].pop("ema_fast")
    raw["long_entry"] = "ema_slow > 0 and crosses_below(rsi, 35)"
    raw["long_exit"] = "crosses_above(rsi, 65) or macd.hist < 0"
    msg = only_error(raw, f"features.{alias}")
    assert fragment in msg


def test_unused_feature_rejected() -> None:
    raw = example_plan()
    raw["long_exit"] = "crosses_above(rsi, 65) or ema_fast < ema_slow"
    msg = only_error(raw, "features.macd")
    assert "not used" in msg


# --------------------------------------------------------------------------- expressions


def test_undefined_feature_name_points_to_features() -> None:
    raw = example_plan()
    del raw["features"]["rsi"]
    raw["long_exit"] = "ema_fast < ema_slow or macd.hist < 0"
    msg = only_error(raw, "long_entry")
    assert "unknown name `rsi`" in msg
    assert '"rsi": {"rsi": {}}' in msg


def test_misspelled_alias_in_expression_suggests_fix() -> None:
    msg = only_error(
        mutate(long_entry="ema_fsat > ema_slow and crosses_below(rsi, 35)"), "long_entry"
    )
    assert "Did you mean `ema_fast`?" in msg


def test_multi_output_alias_needs_output() -> None:
    msg = only_error(mutate(long_exit="macd < 0"), "long_exit")
    assert "macd.macd, macd.signal, macd.hist" in msg


def test_unknown_output_suggests_fix() -> None:
    msg = only_error(mutate(long_exit="macd.hst < 0"), "long_exit")
    assert "Did you mean `hist`?" in msg


def test_bare_number_expression_must_be_condition() -> None:
    raw = mutate(long_exit="ema_fast - ema_slow + macd.hist")
    msg = only_error(raw, "long_exit")
    assert "expression must be a condition, e.g. `ema_fast > ema_slow`" in msg


def test_expression_length_limit() -> None:
    long = " and ".join(["ema_fast > ema_slow"] * 30) + " and crosses_below(rsi, 35)"
    assert len(long) > 500
    assert "limit is 500" in only_error(mutate(long_entry=long), "long_entry")


def test_syntax_error_reported_with_column() -> None:
    msg = only_error(mutate(long_entry="ema_fast > > ema_slow"), "long_entry")
    assert "syntax error" in msg and "column" in msg


def test_expression_must_be_string() -> None:
    assert "must be a string" in only_error(mutate(long_entry=5), "long_entry")


def test_both_expressions_errors_collected() -> None:
    raw = mutate(long_entry="ema_fast == ema_slow", long_exit="macd.foo < 0")
    paths = [e.path for e in errors_of(raw)]
    assert "long_entry" in paths and "long_exit" in paths


INJECTIONS = [
    "__import__('os').system('echo pwned') > 0",
    "ema_fast.__class__ > 0",
    "ema_fast.__class__.__mro__ > 0",
    "(lambda: 1)() > 0",
    "close[0] > 1",
    "[c for c in (1, 2)] > 0",
    "crosses_above(a=close, b=open)",
    "'abc' > close",
    "open(__file__) > 0",
    "eval('1') > 0",
    "close if close else open",
    "(x := close) > 0",
    "f'{close}' > 0",
    "close.__getattribute__('x') > 0",
    "getattr(close, 'x') > 0",
    "close > open == open",
    "close ** 2 > 1",
    "close & open",
    "True or close > open",
]


@pytest.mark.parametrize("expr", INJECTIONS)
def test_injection_attempts_rejected_without_executing(expr: str) -> None:
    calls: list[str] = []
    names = ("__import__", "eval", "exec", "open", "compile")
    saved = {name: getattr(builtins, name) for name in names}
    real_compile = saved["compile"]

    def trap(name: str) -> Any:
        def boom(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"{name} was called")

        return boom

    def ast_only_compile(source: Any, filename: Any, mode: Any, flags: int = 0, **kw: Any) -> Any:
        # ast.parse goes through compile(); anything that would build a code object is a bug.
        if not flags & ast.PyCF_ONLY_AST:
            calls.append("compile")
            raise AssertionError("compile was called to build code")
        return real_compile(source, filename, mode, flags, **kw)

    raw = mutate(long_entry=expr + " and crosses_below(rsi, 35) and ema_fast > ema_slow")
    try:
        builtins.compile = ast_only_compile
        for name in ("eval", "exec", "open", "__import__"):
            setattr(builtins, name, trap(name))
        plan, errors = validate_plan(raw, FAKE_REGISTRY)
    finally:
        for name, fn in saved.items():
            setattr(builtins, name, fn)
    assert calls == []
    assert plan is None
    assert errors and all(e.path == "long_entry" for e in errors)
    assert all(e.message for e in errors)


def test_disallowed_function_lists_allowed_ones() -> None:
    errors = errors_of(mutate(long_entry="__import__('os') > 0 and rsi < 30"))
    assert {e.path for e in errors} == {"long_entry"}
    msg = next(e.message for e in errors if "__import__" in e.message)
    assert "`__import__` is not an allowed function" in msg
    assert "crosses_above(a, b), crosses_below(a, b), prev(x, n)" in msg
    assert any("strings are not part of the language" in e.message for e in errors)


# --------------------------------------------------------------------------- risk


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("stop_loss_pct", 0, "(0, 50]"),
        ("stop_loss_pct", 60, "(0, 50]"),
        ("take_profit_pct", 501, "(0, 500]"),
        ("position_size_pct", 150, "(0, 100]"),
        ("position_size_pct", None, "got null"),
        ("stop_loss_pct", "3%", "must be a number"),
    ],
)
def test_risk_bounds(key: str, value: object, fragment: str) -> None:
    raw = example_plan()
    raw["risk"][key] = value
    assert fragment in only_error(raw, f"risk.{key}")


def test_unknown_risk_key_suggests_fix() -> None:
    raw = example_plan()
    raw["risk"]["stoploss_pct"] = 3
    assert "Did you mean `stop_loss_pct`?" in only_error(raw, "risk.stoploss_pct")


# --------------------------------------------------------------------------- canonical form


def test_plan_to_dict_round_trips() -> None:
    plan = parse_plan(example_plan(), FAKE_REGISTRY)
    again = parse_plan(plan_to_dict(plan), FAKE_REGISTRY)
    assert again == plan
    assert plan_to_dict(again) == plan_to_dict(plan)
    json.dumps(plan_to_dict(plan))  # JSON-able


def test_plan_hash_ignores_key_order_and_whitespace() -> None:
    raw = example_plan()
    reordered = dict(reversed(list(copy.deepcopy(raw).items())))
    text = json.dumps(raw, indent=4)
    h = plan_hash(parse_plan(raw, FAKE_REGISTRY))
    assert plan_hash(parse_plan(reordered, FAKE_REGISTRY)) == h
    assert plan_hash(parse_plan(text, FAKE_REGISTRY)) == h
    assert len(h) == 64


def test_plan_hash_treats_explicit_default_like_omitted() -> None:
    raw = example_plan()
    explicit = copy.deepcopy(raw)
    explicit["features"]["macd"]["macd"] = {"fast": 12, "slow": 26, "signal": 9}
    assert plan_hash(parse_plan(raw, FAKE_REGISTRY)) == plan_hash(
        parse_plan(explicit, FAKE_REGISTRY)
    )


def test_plan_hash_changes_with_param() -> None:
    raw = example_plan()
    other = copy.deepcopy(raw)
    other["features"]["ema_fast"]["ema"]["period"] = 21
    assert plan_hash(parse_plan(raw, FAKE_REGISTRY)) != plan_hash(parse_plan(other, FAKE_REGISTRY))


# --------------------------------------------------------------------------- tuning hooks


def test_tunable_params_lists_feature_and_set_risk_params() -> None:
    plan = parse_plan(example_plan(), FAKE_REGISTRY)
    tp = tunable_params(plan, FAKE_REGISTRY)
    assert list(tp) == [
        "ema_fast.period",
        "ema_slow.period",
        "rsi.period",
        "macd.fast",
        "macd.slow",
        "macd.signal",
        "risk.stop_loss_pct",
    ]
    assert tp["ema_slow.period"].default == 100
    assert (tp["ema_slow.period"].low, tp["ema_slow.period"].high) == (2, 500)
    sl = tp["risk.stop_loss_pct"]
    assert sl.kind == "float" and sl.default == 3.0 and sl.high == 50.0 and sl.low > 0


def test_with_params_returns_new_validated_plan_and_rounds_ints() -> None:
    plan = parse_plan(example_plan(), FAKE_REGISTRY)
    new = with_params(plan, {"ema_fast.period": 12.6, "risk.stop_loss_pct": 5.5}, FAKE_REGISTRY)
    assert new.features[0].params["period"] == 13
    assert new.risk.stop_loss_pct == 5.5
    assert plan.features[0].params["period"] == 20  # original untouched
    assert plan_hash(new) != plan_hash(plan)


def test_with_params_unknown_key_suggests_fix() -> None:
    plan = parse_plan(example_plan(), FAKE_REGISTRY)
    with pytest.raises(PlanValidationError) as info:
        with_params(plan, {"ema_fast.perod": 10}, FAKE_REGISTRY)
    assert "Did you mean `ema_fast.period`?" in str(info.value)


def test_with_params_unset_risk_key_rejected() -> None:
    plan = parse_plan(example_plan(), FAKE_REGISTRY)
    with pytest.raises(PlanValidationError, match="not set in this plan"):
        with_params(plan, {"risk.take_profit_pct": 10}, FAKE_REGISTRY)


def test_with_params_out_of_range_rejected_at_feature_path() -> None:
    plan = parse_plan(example_plan(), FAKE_REGISTRY)
    with pytest.raises(PlanValidationError) as info:
        with_params(plan, {"macd.slow": 999}, FAKE_REGISTRY)
    assert [e.path for e in info.value.errors] == ["features.macd.macd.slow"]


# --------------------------------------------------------------------------- JSON schema


def test_json_schema_lists_registry_features_and_limits() -> None:
    schema = plan_json_schema(FAKE_REGISTRY)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "name",
        "symbol",
        "interval",
        "features",
        "long_entry",
        "long_exit",
    }
    feats = schema["properties"]["features"]
    assert feats["maxProperties"] == 4
    names = {next(iter(b["properties"])) for b in feats["additionalProperties"]["oneOf"]}
    assert names == set(FAKE_REGISTRY)
    json.dumps(schema)
