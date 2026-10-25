"""logic_hash: one hypothesis = one hash, whatever its name, aliases, whitespace, tuned params,
rule thresholds, or risk values; a different operator, feature, input, or expression structure
is a different hypothesis."""

from __future__ import annotations

import json
from typing import Any

import pytest

from lab.plan import parse_plan, plan_hash
from lab.plan.validator import PlanValidationError
from lab.research.logic import (
    alias_map,
    canonical_param_key,
    canonical_space,
    logic_hash,
    logic_payload,
)

BASE: dict[str, Any] = {
    "name": "rsi_trend",
    "description": "buy dips in an uptrend",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {
        "trend": {"ema": {"period": 100}},
        "osc": {"rsi": {"period": 14}},
    },
    "long_entry": "close > trend and crosses_below(osc, 30)",
    "long_exit": "osc > 70 or close < trend",
    "risk": {"position_size_pct": 100, "stop_loss_pct": 5},
}
H = logic_hash(BASE)


def variant(**over: Any) -> dict[str, Any]:
    return {**BASE, **over}


# --------------------------------------------------------------------------- same logic


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param(variant(name="other", description="new words"), id="name+description"),
        pytest.param(
            variant(
                long_entry="close>trend   and crosses_below( osc ,30 )",
                long_exit="  osc > 70 or close < trend",
            ),
            id="whitespace",
        ),
        pytest.param(
            variant(
                features={"slow": {"ema": {"period": 100}}, "r": {"rsi": {"period": 14}}},
                long_entry="close > slow and crosses_below(r, 30)",
                long_exit="r > 70 or close < slow",
            ),
            id="alias-rename",
        ),
        pytest.param(
            variant(features={"osc": {"rsi": {"period": 14}}, "trend": {"ema": {"period": 100}}}),
            id="feature-order",
        ),
        pytest.param(
            variant(features={"trend": {"ema": {"period": 150}}, "osc": {"rsi": {"period": 9}}}),
            id="param-values",
        ),
        pytest.param(
            variant(
                long_entry="close > trend and crosses_below(osc, 25)",
                long_exit="osc > 75 or close < trend",
            ),
            id="rule-thresholds",
        ),
        pytest.param(variant(risk={"position_size_pct": 50, "stop_loss_pct": 8}), id="risk-values"),
        # the same rule on another bar size is the same hypothesis (one holdout check, one
        # walk-forward / sweep history)
        pytest.param(variant(interval="1h"), id="interval"),
    ],
)
def test_cosmetic_and_tuning_changes_keep_the_logic_hash(plan: dict[str, Any]) -> None:
    assert logic_hash(plan) == H
    if plan != BASE:
        assert plan_hash(parse_plan(plan)) != plan_hash(parse_plan(BASE))


def test_hash_accepts_a_plan_dict_json_or_parsed_plan() -> None:
    assert logic_hash(json.dumps(BASE)) == H == logic_hash(parse_plan(BASE))
    assert len(H) == 64 and int(H, 16) >= 0


# --------------------------------------------------------------------------- different logic


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param(
            variant(long_entry="close > trend and crosses_above(osc, 30)"), id="operator-cross"
        ),
        pytest.param(variant(long_exit="osc < 70 or close < trend"), id="comparison-direction"),
        pytest.param(variant(long_exit="osc > 70 and close < trend"), id="and-vs-or"),
        pytest.param(
            variant(features={"trend": {"sma": {"period": 100}}, "osc": {"rsi": {"period": 14}}}),
            id="feature",
        ),
        pytest.param(variant(long_entry="open > trend and crosses_below(osc, 30)"), id="input"),
        pytest.param(
            variant(long_entry="crosses_below(osc, 30) and close > trend"), id="structure-order"
        ),
        pytest.param(variant(long_exit="osc > 70"), id="structure-dropped-clause"),
        pytest.param(variant(risk={"position_size_pct": 100}), id="risk-key-removed"),
        pytest.param(
            variant(risk={"position_size_pct": 100, "stop_loss_pct": 5, "take_profit_pct": 9}),
            id="risk-key-added",
        ),
        pytest.param(variant(symbol="ETHUSDT"), id="symbol"),
    ],
)
def test_logic_changes_change_the_hash(plan: dict[str, Any]) -> None:
    assert logic_hash(plan) != H


def test_swapping_which_alias_plays_which_role_is_different_logic() -> None:
    """Two EMAs: the canonical ids follow first appearance, so ``a > b`` with a fast and b slow
    and the same text with the roles of two *different* features swapped differ. (Each side is
    a ratio of a feature to its own past, so the text is unit-consistent either way round.)"""
    entry = "x / prev(x, 1) > 1.01 and y / prev(y, 1) > 1.01"
    ema_rsi = variant(
        features={"x": {"ema": {"period": 20}}, "y": {"rsi": {"period": 14}}},
        long_entry=entry,
        long_exit="x / prev(x, 1) < 0.99",
    )
    rsi_ema = variant(
        features={"x": {"rsi": {"period": 14}}, "y": {"ema": {"period": 20}}},
        long_entry=entry,
        long_exit="x / prev(x, 1) < 0.99",
    )
    assert logic_hash(ema_rsi) != logic_hash(rsi_ema)


def test_invalid_plan_raises() -> None:
    with pytest.raises(PlanValidationError):
        logic_hash(variant(long_entry="crosses_below(osk, 30)"))


# --------------------------------------------------------------------------- canonical keys


def test_alias_map_follows_first_appearance_in_entry_then_exit() -> None:
    plan = variant(
        features={
            "vol": {"atr": {"period": 14}},
            "osc": {"rsi": {"period": 14}},
            "trend": {"ema": {"period": 100}},
        },
        long_exit="osc > 70 or close < trend - vol",  # vol only appears in the exit rule
    )
    # entry mentions trend then osc; vol first appears in the exit
    assert alias_map(plan) == {"trend": "f0", "osc": "f1", "vol": "f2"}
    payload = logic_payload(plan)
    assert [f["feature"] for f in payload["features"]] == ["ema", "rsi", "atr"]
    assert payload["risk_keys"] == ["stop_loss_pct"]
    assert "trend" not in payload["long_entry"] and "f0" in payload["long_entry"]


def test_canonical_space_matches_across_alias_renames() -> None:
    renamed = variant(
        features={"slow": {"ema": {"period": 100}}, "r": {"rsi": {"period": 14}}},
        long_entry="close > slow and crosses_below(r, 30)",
        long_exit="r > 70 or close < slow",
    )
    a = canonical_space(BASE, {"trend.period": (50, 150), "risk.stop_loss_pct": [2, 8]})
    b = canonical_space(renamed, {"slow.period": [50, 150], "risk.stop_loss_pct": (2, 8)})
    assert a == b == {"f0.period": [50.0, 150.0], "risk.stop_loss_pct": [2.0, 8.0]}
    assert canonical_param_key(BASE, "osc.period") == "f1.period"
    with pytest.raises(ValueError, match=r"not an 'alias\.param' key"):
        canonical_param_key(BASE, "nope.period")


def test_param_match_tokens_line_up_across_a_no_op_reordering() -> None:
    """A no-op clause put first shifts the canonical ids; the feature-ordinal token still
    matches the k-th EMA of one plan with the k-th EMA of the other."""
    from lab.research.logic import param_match_tokens

    ema = {
        "name": "e",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {"fast": {"ema": {"period": 20}}, "slow": {"ema": {"period": 50}}},
        "long_entry": "crosses_above(fast, slow)",
        "long_exit": "crosses_below(fast, slow)",
        "risk": {"position_size_pct": 100, "stop_loss_pct": 5},
    }
    padded = {
        **ema,
        "features": {**ema["features"], "junk": {"rsi": {"period": 14}}},
        "long_entry": "junk > -1 and crosses_above(fast, slow)",
    }
    a = param_match_tokens(ema, "fast.period")
    b = param_match_tokens(padded, "fast.period")
    assert "c:f0.period:ema" in a and "c:f1.period:ema" in b  # canonical ids shifted
    assert a & b == {"o:ema#0.period"}  # ... but the 1st EMA still matches the 1st EMA
    assert param_match_tokens(ema, "slow.period") & param_match_tokens(padded, "slow.period")
    assert not param_match_tokens(padded, "junk.period") & a
    assert param_match_tokens(ema, "risk.stop_loss_pct") == {"r:risk.stop_loss_pct"}
    with pytest.raises(ValueError, match=r"not an 'alias\.param' key"):
        param_match_tokens(ema, "nope.period")
