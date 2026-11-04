"""Regime-model features in plans: PLAN.md Phase 20 Part B.

``hmm_state`` and ``hmm_bear_prob`` are filters, never direction signals: usable anywhere in an
overlay's ``flat_when`` and as an ``and`` gate of ``long_entry`` only. The tests use fake specs so
they do not depend on the real features being registered.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab import registry as registry_module
from lab.plan import (
    OVERLAY_KIND,
    ExprScope,
    check_expression,
    evaluate_expression,
    parse_plan,
    plan_hash,
    validate_plan,
)
from lab.plan.schema import HMM_FEATURE_NAMES, MIN_HMM_INTERVAL_SECONDS
from lab.plan.validator import REGIME_RULE, gate_base_expressions
from lab.registry import REGISTRY
from lab.registry.features import make_spec
from lab.research.logic import literal_vector, logic_hash
from lab.types import FeatureSpec, Plan, PlanError


def _unused(candles: pd.DataFrame, _params: Mapping[str, float]) -> pd.DataFrame:
    return pd.DataFrame({"value": np.nan}, index=candles.index)


FAKES: dict[str, FeatureSpec] = {
    name: make_spec(name, "Fake regime feature.", (), ("value",), _unused, lambda _p: 0)
    for name in HMM_FEATURE_NAMES
}
REG: dict[str, FeatureSpec] = {**REGISTRY, **FAKES}


def edge(entry: str, exit_: str = "close < ema_slow", **changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": "ema_trend_hmm_gate",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {
            "ema_slow": {"ema": {"period": 100}},
            "bear": {"hmm_bear_prob": {}},
            "state": {"hmm_state": {}},
            "fz": {"funding_zscore": {"period": 180}},
        },
        "long_entry": entry,
        "long_exit": exit_,
    }
    raw.update(changes)
    used = f"{raw['long_entry']} {raw['long_exit']}"
    raw["features"] = {a: v for a, v in raw["features"].items() if a in used}
    return raw


def overlay(rule: str, **changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "kind": OVERLAY_KIND,
        "name": "hmm_overlay",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {
            "bear": {"hmm_bear_prob": {}},
            "state": {"hmm_state": {}},
            "sma_slow": {"sma": {"period": 20}},
        },
        "flat_when": rule,
    }
    raw.update(changes)
    raw["features"] = {a: v for a, v in raw["features"].items() if a in raw["flat_when"]}
    return raw


def valid(raw: Mapping[str, Any]) -> Plan:
    plan, errors = validate_plan(raw, REG)
    assert plan is not None, errors
    return plan


def rejected(raw: Mapping[str, Any]) -> list[PlanError]:
    plan, errors = validate_plan(raw, REG)
    assert plan is None, "expected the plan to be rejected"
    return errors


def assert_names_the_regime_rule(errors: list[PlanError], path: str) -> None:
    mine = [e for e in errors if e.path == path]
    assert mine, errors
    for error in mine:
        assert REGIME_RULE in error.message
        assert "regime-model" in error.message
        assert "derivatives" not in error.message


def test_the_rule_text_is_the_plan_rule() -> None:
    assert REGIME_RULE == "a regime model is a filter, never a direction signal"


# --------------------------------------------------------------------------- edge plans


@pytest.mark.parametrize(
    "entry",
    [
        "close > ema_slow and bear < 0.5",
        "bear < 0.5 and close > ema_slow",
        "close > ema_slow and state > 0",
        "close > ema_slow and bear < 0.5 and state > 0",
        "close > ema_slow and persist(bear < 0.5, 3)",
        "persist(close > ema_slow, 3) and bear < 0.5",
        "close > ema_slow and (bear < 0.5 or state > 0)",
        "close > ema_slow and not bear > 0.5",
    ],
)
def test_hmm_features_are_accepted_as_gating_and_clauses(entry: str) -> None:
    assert valid(edge(entry))


def test_a_clause_mixing_hmm_and_derivatives_features_is_a_gate_clause() -> None:
    assert valid(edge("close > ema_slow and (bear < 0.5 or fz < 2)"))
    assert valid(edge("close > ema_slow and bear < 0.5 and fz < 2"))


@pytest.mark.parametrize(
    "entry",
    [
        "bear < 0.5",
        "state > 0",
        "bear < 0.5 and state > 0",
        "persist(bear < 0.5, 3)",
        "not bear > 0.5",
        "crosses_below(bear, 0.5)",
    ],
)
def test_an_hmm_feature_is_never_the_only_entry_signal(entry: str) -> None:
    errors = rejected(edge(entry))
    assert_names_the_regime_rule(errors, "long_entry")
    assert "uses only regime-model feature(s)" in errors[0].message
    assert "`close > ema_slow and" in errors[0].message


@pytest.mark.parametrize(
    "entry",
    [
        "close > ema_slow or bear < 0.5",
        "ema_slow * bear > close",
        "persist(close > ema_slow and bear < 0.5, 3)",
        "not (close > ema_slow and bear < 0.5)",
    ],
)
def test_an_hmm_feature_cannot_share_a_clause_with_price(entry: str) -> None:
    errors = rejected(edge(entry))
    assert_names_the_regime_rule(errors, "long_entry")
    assert any("in one clause" in e.message for e in errors)


@pytest.mark.parametrize(
    "exit_",
    [
        "bear > 0.5",
        "close < ema_slow or bear > 0.5",
        "persist(state < 1, 3)",
    ],
)
def test_hmm_features_are_refused_in_long_exit(exit_: str) -> None:
    errors = rejected(edge("close > ema_slow and bear < 0.5", exit_))
    assert_names_the_regime_rule(errors, "long_exit")


def test_a_rule_with_a_unit_error_still_gets_the_gate_error() -> None:
    errors = rejected(edge("close > ema_slow and bear < 0.5", "close > 50000 or bear > 0.5"))
    assert_names_the_regime_rule([e for e in errors if "regime-model" in e.message], "long_exit")


@pytest.mark.parametrize(
    "clause",
    [
        "high >= low",
        "close >= close",
        "close <= high",
        "persist(high >= low, 3)",
        "persist(high >= low and close >= close, 3)",
    ],
)
def test_static_always_true_rule_applies_to_hmm_gates(clause: str) -> None:
    errors = rejected(edge(f"{clause} and bear < 0.5"))
    assert_names_the_regime_rule(errors, "long_entry")
    assert any("true on every bar" in e.message for e in errors)


def test_static_always_true_rule_sees_through_persist_for_derivatives_gates_too() -> None:
    errors = rejected(edge("persist(high >= low, 3) and fz < 2"))
    assert any("true on every bar" in e.message and "derivatives" in e.message for e in errors)


def test_a_real_persist_clause_is_not_always_true() -> None:
    assert valid(edge("persist(close >= ema_slow, 3) and bear < 0.5"))


def test_each_family_gets_its_own_rule_when_both_break_it() -> None:
    errors = rejected(edge("close > ema_slow and bear < 0.5", "fz > 2 or bear > 0.5"))
    assert len(errors) == 2
    assert "uses derivatives feature(s) `fz` (funding_zscore)" in errors[0].message
    assert REGIME_RULE not in errors[0].message
    assert "uses regime-model feature(s) `bear` (hmm_bear_prob)" in errors[1].message
    assert "derivatives" not in errors[1].message


def test_derivatives_rule_text_is_unchanged_when_no_hmm_feature_is_involved() -> None:
    errors = rejected(edge("fz < 2"))
    assert len(errors) == 1
    assert errors[0].message.startswith("uses only derivatives feature(s) `fz` (funding_zscore)")
    assert REGIME_RULE not in errors[0].message


# --------------------------------------------------------------------------- level, not trigger


def trigger_plan(entry: str, gate: str = "b") -> dict[str, Any]:
    """Features ``b``, ``s``, ``fz`` (whichever ``entry`` reads) and ``r``, which the exit reads."""
    features: dict[str, Any] = {
        "b": {"hmm_bear_prob": {}},
        "s": {"hmm_state": {}},
        "fz": {"funding_zscore": {"period": 180}},
        "r": {"rsi": {"period": 14}},
    }
    used = {*re.findall(r"[a-z_]+", entry), "r"}
    return {
        "name": "regime_trigger",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {a: v for a, v in features.items() if a in used},
        "long_entry": entry,
        "long_exit": "r > 90",
    }


LEVEL_RULE = "a gate must be a level"


@pytest.mark.parametrize(
    ("entry", "quoted"),
    [
        ("crosses_below(b, 0.5) and r < 80", "`crosses_below(b, 0.5)`"),
        ("b < 0.5 and prev(b, 1) >= 0.5 and r < 80", "`prev(b, 1)`"),
        ("s > 0 and prev(s, 1) < 1 and r < 80", "`prev(s, 1)`"),
    ],
)
def test_the_three_reported_regime_triggers_are_refused(entry: str, quoted: str) -> None:
    errors = rejected(trigger_plan(entry))
    assert len(errors) == 1
    assert_names_the_regime_rule(errors, "long_entry")
    assert LEVEL_RULE in errors[0].message and quoted in errors[0].message


@pytest.mark.parametrize(
    "entry",
    [
        "crosses_above(b, 0.5) and r < 80",
        "crosses_above(s, 0) and r < 80",
        "not crosses_above(b, 0.5) and r < 80",
        "prev(b + 1, 1) < 1.5 and r < 80",
        "persist(prev(b, 1) < 0.5, 3) and r < 80",
        "prev(persist(b < 0.5, 3), 1) and r < 80",
        "r < 80 and (r < 30 or prev(s, 1) < 1)",  # mixes prices too, but the read is still named
        "crosses_above(fz, b) and r < 80",  # a clause mixing the gate families
    ],
)
def test_change_calls_are_refused_anywhere_in_long_entry(entry: str) -> None:
    errors = rejected(trigger_plan(entry))
    reads = [e for e in errors if LEVEL_RULE in e.message]
    assert len(reads) == 1
    assert reads[0].path == "long_entry" and REGIME_RULE in reads[0].message


@pytest.mark.parametrize(
    "entry",
    [
        "b < 0.5 and not persist(b < 0.5, 2) and r < 80",
        "persist(b < 0.5, 3) and not persist(b < 0.5, 4) and r < 80",
        "s > 0 and not (persist(s > 0, 2) or b > 0.9) and r < 80",
    ],
)
def test_negated_persist_cannot_rebuild_the_trigger(entry: str) -> None:
    errors = rejected(trigger_plan(entry))
    assert len(errors) == 1
    assert_names_the_regime_rule(errors, "long_entry")
    assert LEVEL_RULE in errors[0].message and "under `not`" in errors[0].message


def test_why_negated_persist_is_refused_it_is_the_same_series_as_prev() -> None:
    scope = ExprScope(frozenset({"b"}))
    b = np.random.default_rng(5).random(2000)

    def series(source: str) -> np.ndarray:
        expr, errors = check_expression(source, scope, "rule")
        assert expr is not None, errors
        return evaluate_expression(expr, {"b": b}, 2000)

    first_bar = series("b < 0.5 and prev(b, 1) >= 0.5")
    assert first_bar.any()
    assert np.array_equal(first_bar, series("b < 0.5 and not persist(b < 0.5, 2)"))


@pytest.mark.parametrize(
    "entry",
    [
        "persist(b < 0.5, 3) and r < 80",
        "persist(s > 0, 50) and r < 80",
        "not b > 0.5 and r < 80",
        "not not persist(b < 0.5, 3) and r < 80",
        "not persist(b > 0.5, 1) and r < 80",  # persist(c, 1) is c
        "crosses_above(r, 30) and b < 0.5",  # the signal may cross; the gate stays a level
        "prev(r, 1) < 30 and persist(b < 0.5, 3)",
    ],
)
def test_levels_stay_allowed(entry: str) -> None:
    assert valid(trigger_plan(entry))


@pytest.mark.parametrize(
    "entry",
    [
        "crosses_below(fz, 0.5) and r < 80",
        "fz < 0.5 and prev(fz, 1) >= 0.5 and r < 80",
        "fz > 0 and prev(fz, 1) < 1 and r < 80",
        "fz < 2 and not persist(fz < 2, 2) and r < 80",
    ],
)
def test_derivatives_equivalents_still_validate(entry: str) -> None:
    assert valid(trigger_plan(entry))


@pytest.mark.parametrize(
    "rule",
    [
        "crosses_below(bear, 0.5)",
        "prev(bear, 1) > 0.5",
        "bear > 0.5 and prev(state, 1) < 1",
        "not persist(bear < 0.5, 3)",
    ],
)
def test_overlays_keep_their_freedom_in_flat_when(rule: str) -> None:
    assert valid(overlay(rule)).kind == OVERLAY_KIND


# --------------------------------------------------------------------------- overlays


@pytest.mark.parametrize(
    "rule",
    [
        "bear > 0.92",
        "persist(bear > 0.5, 3)",
        "state < 1",
        "close < sma_slow or bear > 0.92",
        "close / sma_slow < bear",
        "bear > 0.5 and state < 1",
        "not bear < 0.5",
    ],
)
def test_overlays_may_use_hmm_features_anywhere_in_flat_when(rule: str) -> None:
    assert valid(overlay(rule)).kind == OVERLAY_KIND


def test_the_two_slate_overlays_have_different_logic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "REGISTRY", REG)
    confidence = overlay("bear > 0.92")
    stability = overlay("persist(bear > 0.5, 3)")
    assert logic_hash(confidence) != logic_hash(stability)
    assert literal_vector(stability) != literal_vector(overlay("persist(bear > 0.5, 2)"))
    assert logic_hash(stability) == logic_hash(overlay("persist(bear > 0.5, 2)"))


# --------------------------------------------------------------------------- minimum interval


def test_the_minimum_is_one_hour() -> None:
    assert MIN_HMM_INTERVAL_SECONDS == 3_600


@pytest.mark.parametrize("interval", ["1m", "5m", "15m", "30m"])
@pytest.mark.parametrize("build", [edge, overlay])
def test_hmm_features_are_refused_below_one_hour(build: Any, interval: str) -> None:
    rule = "close > ema_slow and bear < 0.5" if build is edge else "bear > 0.92"
    errors = rejected(build(rule, interval=interval))
    assert [e.path for e in errors] == ["features.bear"]
    assert "1h or longer" in errors[0].message and repr(interval) in errors[0].message


@pytest.mark.parametrize("interval", ["1h", "2h", "4h", "1d"])
@pytest.mark.parametrize("build", [edge, overlay])
def test_hmm_features_are_accepted_from_one_hour_up(build: Any, interval: str) -> None:
    rule = "close > ema_slow and bear < 0.5" if build is edge else "bear > 0.92"
    assert valid(build(rule, interval=interval))


def test_every_hmm_alias_is_reported_on_a_short_interval() -> None:
    errors = rejected(edge("close > ema_slow and bear < 0.5 and state > 0", interval="5m"))
    assert [e.path for e in errors] == ["features.bear", "features.state"]


def test_an_unknown_interval_gets_only_the_interval_error() -> None:
    errors = rejected(edge("close > ema_slow and bear < 0.5", interval="7m"))
    assert [e.path for e in errors] == ["interval"]


def test_short_intervals_are_untouched_without_hmm_features() -> None:
    assert valid(edge("close > ema_slow", interval="5m"))


# --------------------------------------------------------------------------- gate base


def base_of(raw: Mapping[str, Any]) -> Any:
    return gate_base_expressions(parse_plan(raw, REG), REG)


def test_gate_base_strips_hmm_clauses() -> None:
    exprs = base_of(edge("close > ema_slow and bear < 0.5"))
    assert exprs is not None
    base, negated = exprs
    assert base.source == "close > ema_slow"
    assert negated.source == "not (close > ema_slow)"


def test_gate_base_strips_hmm_and_derivatives_together() -> None:
    base, _ = base_of(edge("close > ema_slow and bear < 0.5 and fz < 2 and (bear < 1 or fz < 3)"))
    assert base.source == "close > ema_slow"


def test_gate_base_stays_defined_where_the_hmm_column_is_nan() -> None:
    base, negated = base_of(edge("close > ema_slow and bear < 0.5"))
    n = 6
    close = np.array([1.0, 3.0, 1.0, 3.0, 1.0, 3.0])
    cols = {"close": close, "ema_slow": np.full(n, 2.0), "bear": np.full(n, np.nan)}
    assert evaluate_expression(base, cols, n).tolist() == [False, True] * 3
    assert evaluate_expression(negated, cols, n).tolist() == [True, False] * 3


def test_gate_base_records_only_its_own_persist_calls() -> None:
    base, negated = base_of(edge("persist(close > ema_slow, 3) and persist(bear < 0.5, 4)"))
    assert base.source == "persist(close > ema_slow, 3)"
    assert base.persist_ks == negated.persist_ks == (3,)


def test_gate_base_is_none_for_a_plan_without_gates_and_for_overlays() -> None:
    assert base_of(edge("close > ema_slow")) is None
    assert base_of(overlay("bear > 0.92")) is None


# --------------------------------------------------------------------------- unchanged plans


# Computed from the sources before this feature existed, like test_plan_overlay's edge hashes.
PINNED_PLAN_HASH = "648db19cdc54b1592af2e3bb7cdb4fca5334a2070a50336a7d135685b92f79c2"
PINNED_LOGIC_HASH = "f60ff42b1cef274d0ec58495a2dd0e4b918a0b1d499be90ed10fde2aca159f60"


def test_plans_without_these_features_hash_as_before() -> None:
    raw: dict[str, Any] = {
        "name": "ema_trend_funding_gate",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {
            "ema_slow": {"ema": {"period": 100}},
            "fz": {"funding_zscore": {"period": 180}},
        },
        "long_entry": "close > ema_slow and fz < 2",
        "long_exit": "close < ema_slow",
    }
    assert plan_hash(parse_plan(raw)) == PINNED_PLAN_HASH
    assert logic_hash(raw) == PINNED_LOGIC_HASH
