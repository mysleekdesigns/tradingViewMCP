"""Portfolio plan type: validator fix-it errors, canonical form, tuning hooks, logic identity,
hand-checked weights on a tiny panel, and the perturbation test on ``build_signals``.

The tiny panel's numbers are written out so every weight can be checked on paper.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.plan.portfolio import (
    LAB_REBALANCE_OFFSET,
    MAX_PORTFOLIO_PARAMS,
    PORTFOLIO_KIND,
    DispersionGate,
    PortfolioPlan,
    build_signals,
    is_portfolio_plan,
    pinned_offset_errors,
    plan_logic,
    plan_to_dict,
    portfolio_plan_hash,
    portfolio_plan_json_schema,
    tunable_params,
    tunable_values,
    validate_portfolio_plan,
    with_params,
)
from lab.plan.validator import PlanValidationError, validate_plan
from lab.registry.cross_section import trailing_volatility

NAN = np.nan

H1: dict[str, Any] = {
    "kind": "portfolio",
    "name": "xs_momentum_28d_top_quintile",
    "description": "H1: long-only top quintile of the liquid universe on 28-day return",
    "interval": "1d",
    "score": {"trailing_return": {"lookback": 28}},
    "rebalance": {"every": 7, "offset": 0},
    "selection": {"quantile": 0.2, "exit_quantile": 0.3, "min_names": 3},
    "weighting": {"equal": {}},
    "max_weight": 0.25,
    "dispersion_gate": None,
}

FULL: dict[str, Any] = {
    **H1,
    "weighting": {"inverse_vol": {"lookback": 30}},
    "dispersion_gate": {"lookback": 28, "min": 0.05},
}


def ok(raw: Any, interval: str = "1d") -> PortfolioPlan:
    res = validate_portfolio_plan(raw, interval=interval)
    assert res.ok, res.errors
    assert res.plan is not None and res.errors == ()
    return res.plan


def errors_of(raw: Any, interval: str = "1d") -> tuple[str, ...]:
    res = validate_portfolio_plan(raw, interval=interval)
    assert not res.ok and res.plan is None
    assert res.errors
    return res.errors


def mutate(path: str, value: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deep copy of ``base`` (H1) with ``path`` set; ``value=...`` (Ellipsis) deletes it."""
    out = copy.deepcopy(H1 if base is None else base)
    *parents, leaf = path.split(".")
    node = out
    for part in parents:
        node = node[part]
    if value is Ellipsis:
        del node[leaf]
    else:
        node[leaf] = value
    return out


# --------------------------------------------------------------------------- validator


def test_accepts_h1_plan() -> None:
    plan = ok(H1)
    assert plan == PortfolioPlan(
        name="xs_momentum_28d_top_quintile",
        description=H1["description"],
        interval="1d",
        score_feature="trailing_return",
        score_lookback=28,
        rebalance_every=7,
        rebalance_offset=0,
        quantile=0.2,
        exit_quantile=0.3,
        min_names=3,
        weighting="equal",
        vol_lookback=None,
        max_weight=0.25,
        dispersion_gate=None,
    )


def test_accepts_full_feature_plan_and_json_text() -> None:
    plan = ok(json.dumps(FULL))
    assert plan.weighting == "inverse_vol" and plan.vol_lookback == 30
    assert plan.dispersion_gate == DispersionGate(lookback=28, min=0.05)


def test_defaults_filled() -> None:
    minimal = {
        "kind": "portfolio",
        "name": "m",
        "interval": "1d",
        "score": {"trailing_return": {"lookback": 28}},
        "rebalance": {"every": 7},
        "selection": {"quantile": 0.2},
        "weighting": {"inverse_vol": {}},
    }
    plan = ok(minimal)
    assert plan.rebalance_offset == 0
    assert plan.exit_quantile is None
    assert plan.min_names == 1
    assert plan.vol_lookback == 30
    assert plan.max_weight == 1.0
    assert plan.dispersion_gate is None
    assert plan.description == ""


def test_is_portfolio_plan_routes() -> None:
    assert is_portfolio_plan(H1)
    assert is_portfolio_plan(json.dumps(H1))
    assert not is_portfolio_plan({"name": "x", "symbol": "BTCUSDT"})
    assert not is_portfolio_plan({"kind": "single"})
    assert not is_portfolio_plan("not json")
    assert not is_portfolio_plan(None)
    assert not is_portfolio_plan(["kind", "portfolio"])


def test_single_symbol_validator_rejects_portfolio_plan() -> None:
    # The router must send kind=portfolio here, never to validate_plan.
    plan, errors = validate_plan(H1)
    assert plan is None and any(e.path == "kind" for e in errors)


def _one(errors: tuple[str, ...], path: str) -> str:
    hits = [e for e in errors if e.startswith(f"{path}: ")]
    assert hits, f"no error at {path!r}: {errors}"
    return hits[0]


@pytest.mark.parametrize(
    ("raw", "path", "needle"),
    [
        # wrong kind
        (mutate("kind", "single"), "kind", 'must be "portfolio"'),
        (mutate("kind", ...), "kind", '"kind": "portfolio"'),
        # unknown / misplaced keys
        (mutate("rebalanse", {"every": 7}), "rebalanse", "Did you mean `rebalance`"),
        (mutate("symbols", ["BTCUSDT"]), "symbols", "lab.toml [universe]"),
        (mutate("top_n", 20), "top_n", "lab.toml [universe]"),
        (mutate("long_entry", "close > 1"), "long_entry", "single-symbol plans"),
        (mutate("short_entry", "x"), "short_entry", "long-only"),
        (mutate("liquidity_cap_mult", 2.0), "liquidity_cap_mult", "fingerprint"),
        (mutate("selection.quantil", 0.2), "selection.quantil", "Did you mean `quantile`"),
        (mutate("rebalance.weekday", 1), "rebalance.weekday", "Allowed: every, offset"),
        # missing required
        (mutate("score", ...), "score", "missing required key `score`"),
        (mutate("rebalance.every", ...), "rebalance.every", "missing required `every`"),
        (mutate("selection.quantile", ...), "selection.quantile", "top quintile"),
        # interval
        (mutate("interval", "4h"), "interval", 'Use "interval": "1d"'),
        (mutate("interval", "1D"), "interval", "not a known interval"),
        # score
        (
            mutate("score", {"momentum": {"lookback": 28}}),
            "score.momentum",
            "Score features: trailing_return",
        ),
        (mutate("score", {"trailing_retrun": {"lookback": 28}}), "score.trailing_retrun", "Did"),
        (mutate("score.trailing_return.lookback", 1), "score.trailing_return.lookback", "[2, 365]"),
        (
            mutate("score.trailing_return.lookback", 400),
            "score.trailing_return.lookback",
            "Try 365",
        ),
        (
            mutate("score.trailing_return.lookback", 28.5),
            "score.trailing_return.lookback",
            "whole number",
        ),
        (mutate("score.trailing_return.lookback", True), "score.trailing_return.lookback", "bool"),
        (
            mutate("score", {"trailing_return": {"lookback": 28}, "x": {}}),
            "score",
            "exactly one key",
        ),
        # rebalance
        (mutate("rebalance.every", 0), "rebalance.every", "[1, 60]"),
        (mutate("rebalance.every", 61), "rebalance.every", "Try 60"),
        (mutate("rebalance.offset", 7), "rebalance.offset", "[0, 6]"),
        (mutate("rebalance.offset", -1), "rebalance.offset", "Try 0"),
        # selection
        (mutate("selection.quantile", 0), "selection.quantile", "(0, 0.5]"),
        (mutate("selection.quantile", 0.6), "selection.quantile", "Try 0.5"),
        (mutate("selection.exit_quantile", 0.1), "selection.exit_quantile", "below quantile 0.2"),
        (mutate("selection.exit_quantile", 1.5), "selection.exit_quantile", "(0, 1]"),
        (mutate("selection.min_names", 0), "selection.min_names", "[1, 10]"),
        (mutate("selection.min_names", 11), "selection.min_names", "Try 10"),
        # weighting
        (mutate("weighting", {"cap_weight": {}}), "weighting.cap_weight", "Allowed: equal"),
        (
            mutate("weighting", {"inverse_vol": {"lookback": 1}}),
            "weighting.inverse_vol.lookback",
            "[2, 365]",
        ),
        (mutate("weighting", {"equal": {"lookback": 5}}), "weighting.equal.lookback", "unknown"),
        (mutate("weighting", {"equal": {}, "inverse_vol": {}}), "weighting", "exactly one key"),
        (mutate("weighting", "equal"), "weighting", "must be an object"),
        # max weight
        (mutate("max_weight", 0), "max_weight", "(0, 1]"),
        (mutate("max_weight", 1.5), "max_weight", "Try 1"),
        (mutate("max_weight", "0.25"), "max_weight", "got string"),
        # gate
        (mutate("dispersion_gate", {"lookback": 28}), "dispersion_gate.min", "missing required"),
        (mutate("dispersion_gate", {"lookback": 1, "min": 0.05}), "dispersion_gate.lookback", "[2"),
        (mutate("dispersion_gate", {"lookback": 28, "min": 0}), "dispersion_gate.min", "(0, 1)"),
        (mutate("dispersion_gate", 0.05), "dispersion_gate", "must be null or an object"),
        (
            mutate("dispersion_gate", {"lookback": 28, "min": 0.05, "max": 0.5}),
            "dispersion_gate.max",
            "unknown key",
        ),
        # name / description
        (mutate("name", ""), "name", "non-empty string"),
        (mutate("name", "x" * 81), "name", "limit is 80"),
        (mutate("description", 5), "description", "must be a string"),
    ],
)
def test_rejects_bad_field_with_fix_it(raw: dict[str, Any], path: str, needle: str) -> None:
    msg = _one(errors_of(raw), path)
    assert needle in msg, msg


@pytest.mark.parametrize(
    ("raw", "path", "needle"),
    [
        (mutate("min_price", 1.0), "min_price", "no prices or levels"),
        (mutate("selection.min_volume", 1e6), "selection.min_volume", "no prices or levels"),
        (mutate("rebalance.price_level", 50000), "rebalance.price_level", "no prices or levels"),
        (mutate("score", {"close": {"lookback": 28}}), "score.close", "levels that differ by coin"),
        (
            mutate("score", {"dollar_volume": {"lookback": 28}}),
            "score.dollar_volume",
            "return-scale score",
        ),
        (
            mutate("dispersion_gate", {"lookback": 28, "min": 50000}),
            "dispersion_gate.min",
            "never a price or level",
        ),
        (
            mutate("dispersion_gate", {"lookback": 28, "min": 1}),
            "dispersion_gate.min",
            "return-scale number in (0, 1)",
        ),
    ],
)
def test_rejects_price_scale_values(raw: dict[str, Any], path: str, needle: str) -> None:
    msg = _one(errors_of(raw), path)
    assert needle in msg, msg


def test_collects_every_error() -> None:
    raw = mutate("rebalance.every", 0, mutate("selection.quantile", 0.9, mutate("kind", "x")))
    errs = errors_of(raw)
    paths = {e.split(":", 1)[0] for e in errs}
    assert {"kind", "rebalance.every", "selection.quantile"} <= paths


def test_rejects_non_object_and_bad_json() -> None:
    assert errors_of("[1, 2]")[0].startswith("$: plan must be a JSON object")
    assert errors_of("{bad json")[0].startswith("$: invalid JSON")
    assert "NaN is not valid JSON" in errors_of('{"max_weight": NaN}')[0]


def test_interval_is_a_parameter() -> None:
    ok(mutate("interval", "4h"), interval="4h")
    assert "lab.toml [universe] interval" in _one(errors_of(H1, interval="4h"), "interval")


# --------------------------------------------------------------------------- canonical form


@pytest.mark.parametrize("raw", [H1, FULL, mutate("selection.exit_quantile", None)])
def test_plan_to_dict_round_trips(raw: dict[str, Any]) -> None:
    plan = ok(raw)
    data = plan_to_dict(plan)
    assert data["kind"] == PORTFOLIO_KIND
    assert ok(data) == plan
    assert ok(json.dumps(data)) == plan
    assert plan_to_dict(ok(data)) == data
    assert portfolio_plan_hash(ok(data)) == portfolio_plan_hash(plan)


def test_hash_changes_with_numbers() -> None:
    assert portfolio_plan_hash(ok(H1)) != portfolio_plan_hash(
        ok(mutate("score.trailing_return.lookback", 21))
    )


def test_json_schema_shape() -> None:
    schema = portfolio_plan_json_schema()
    json.dumps(schema)  # serialisable
    assert schema["properties"]["kind"] == {"const": "portfolio"}
    assert set(schema["required"]) == {
        "kind",
        "name",
        "interval",
        "score",
        "rebalance",
        "selection",
        "weighting",
    }
    assert schema["additionalProperties"] is False
    assert "trailing_return" in json.dumps(schema["properties"]["score"])
    assert (
        schema["properties"]["dispersion_gate"]["oneOf"][1]["properties"]["min"]["exclusiveMaximum"]
        == 1
    )


# --------------------------------------------------------------------------- tuning hooks


def test_tunable_params_h1_and_full() -> None:
    h1 = tunable_params(ok(H1))
    assert list(h1) == [
        "score.trailing_return.lookback",
        "rebalance.every",
        "selection.quantile",
        "selection.exit_quantile",
        "selection.min_names",
        "max_weight",
    ]
    assert h1["score.trailing_return.lookback"] == (2.0, 365.0, "int")
    assert h1["selection.quantile"] == (0.01, 0.5, "float")
    assert "rebalance.offset" not in h1  # fixed: tuning it fits the weekday

    full = tunable_params(ok(FULL))
    assert len(full) == MAX_PORTFOLIO_PARAMS == 9
    assert full["weighting.inverse_vol.lookback"] == (2.0, 365.0, "int")
    assert full["dispersion_gate.min"] == (0.001, 0.999, "float")

    no_band = tunable_params(ok(mutate("selection.exit_quantile", None)))
    assert "selection.exit_quantile" not in no_band


def test_tunable_values_reads_current_values() -> None:
    values = tunable_values(ok(FULL))
    assert values["score.trailing_return.lookback"] == 28.0
    assert values["dispersion_gate.min"] == 0.05
    assert values["weighting.inverse_vol.lookback"] == 30.0
    assert set(values) == set(tunable_params(ok(FULL)))


def test_with_params_applies_and_validates() -> None:
    plan = ok(FULL)
    new = with_params(
        plan,
        {
            "score.trailing_return.lookback": 20.6,  # int leaf rounds
            "selection.quantile": 0.25,
            "dispersion_gate.min": 0.08,
            "weighting.inverse_vol.lookback": 14,
        },
    )
    assert new.score_lookback == 21
    assert new.quantile == 0.25
    assert new.dispersion_gate == DispersionGate(28, 0.08)
    assert new.vol_lookback == 14
    assert new.name == plan.name and new.rebalance_offset == plan.rebalance_offset
    assert plan.score_lookback == 28  # original untouched


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"score.trailing_return.lookback": 1}, "out of range"),
        ({"selection.quantile": 0.4}, "below quantile 0.4"),  # exit 0.3 < quantile 0.4
        ({"max_weight": 0.0}, "out of range"),
        ({"rebalance.offset": 3}, "`rebalance.offset` is fixed"),
        ({"selection.quantlie": 0.2}, "Did you mean `selection.quantile`"),
        ({"weighting.inverse_vol.lookback": 30}, "unknown tunable"),  # H1 is equal-weighted
    ],
)
def test_with_params_rejects(overrides: dict[str, float], needle: str) -> None:
    with pytest.raises(ValueError) as exc:
        with_params(ok(H1), overrides)
    assert isinstance(exc.value, PlanValidationError)
    assert needle in str(exc.value)


# --------------------------------------------------------------------------- logic identity


def test_plan_logic_ignores_numbers() -> None:
    base = plan_logic(ok(H1))
    assert base == "portfolio|score=trailing_return|weighting=equal|banding=on|gate=off"
    retuned = ok(
        mutate(
            "rebalance",
            {"every": 14, "offset": 3},
            mutate(
                "selection",
                {"quantile": 0.1, "exit_quantile": 0.5, "min_names": 5},
                mutate("score.trailing_return.lookback", 60, mutate("max_weight", 0.5)),
            ),
        )
    )
    assert plan_logic(retuned) == base
    renamed = ok(mutate("name", "other", mutate("description", "different words")))
    assert plan_logic(renamed) == base


def test_plan_logic_changes_with_structure() -> None:
    base = plan_logic(ok(H1))
    variants = {
        plan_logic(ok(mutate("weighting", {"inverse_vol": {"lookback": 30}}))),
        plan_logic(ok(mutate("selection.exit_quantile", None))),
        plan_logic(ok(mutate("dispersion_gate", {"lookback": 28, "min": 0.05}))),
    }
    assert base not in variants and len(variants) == 3
    gate_a = ok(mutate("dispersion_gate", {"lookback": 28, "min": 0.05}))
    gate_b = ok(mutate("dispersion_gate", {"lookback": 60, "min": 0.2}))
    assert plan_logic(gate_a) == plan_logic(gate_b)


# --------------------------------------------------------------------------- build_signals

SYMS = ["A", "B", "C", "D", "E"]
IDX = pd.date_range("2024-01-01", periods=8, freq="D", tz="UTC", name="time")
# 2-row returns: row 2 vs row 0 and row 5 vs row 3 (rows 0 and 3 are all 100).
TINY = pd.DataFrame(
    [
        [100.0, 100.0, 100.0, 100.0, 100.0],
        [100.0, 100.0, 100.0, 100.0, 100.0],
        [110.0, 120.0, 90.0, 105.0, 130.0],  # A .10  B .20  C -.10  D .05  E .30
        [100.0, 100.0, 100.0, 100.0, 100.0],
        [100.0, 100.0, 100.0, 100.0, 100.0],
        [150.0, 100.0, 140.0, 90.0, 101.0],  # A .50  B 0    C .40   D -.10 E .01
        [100.0, 100.0, 100.0, 100.0, 100.0],
        [100.0, 100.0, 100.0, 100.0, 100.0],
    ],
    index=IDX,
    columns=SYMS,
)
ALL = pd.DataFrame(True, index=IDX, columns=SYMS)


def tiny_plan(**kw: Any) -> PortfolioPlan:
    base = dict(
        name="tiny",
        description="",
        interval="1d",
        score_feature="trailing_return",
        score_lookback=2,
        rebalance_every=3,
        rebalance_offset=2,
        quantile=0.4,
        exit_quantile=None,
        min_names=1,
        weighting="equal",
        vol_lookback=None,
        max_weight=1.0,
        dispersion_gate=None,
    )
    base.update(kw)
    plan = PortfolioPlan(**base)
    assert ok(plan_to_dict(plan)) == plan  # every tiny plan is a valid plan
    return plan


def weights_row(sig: Any, i: int) -> dict[str, float]:
    row = sig.weights.iloc[i]
    return {s: float(row[s]) for s in SYMS}


def test_build_signals_equal_hand_checked() -> None:
    members = ALL.copy()
    members.loc[IDX[5], "A"] = False  # A leaves the universe before the second rebalance
    sig = build_signals(tiny_plan(), TINY, members, dollar_volume=None, liquidity_cap_mult=None)
    assert sig.schedule.tolist() == [False, False, True, False, False, True, False, False]
    # Row 2: k=5, n=ceil(0.4*5)=2 -> E (.30), B (.20).
    assert weights_row(sig, 2) == {"A": 0.0, "B": 0.5, "C": 0.0, "D": 0.0, "E": 0.5}
    # Row 5: A not a member, k=4, n=ceil(1.6)=2 -> C (.40), E (.01).
    assert weights_row(sig, 5) == {"A": 0.0, "B": 0.0, "C": 0.5, "D": 0.0, "E": 0.5}
    for i in (0, 1, 3, 4, 6, 7):
        assert sig.weights.iloc[i].isna().all()  # hold
    assert sig.first_decision == IDX[2]
    assert sig.inverse_vol is None and sig.gate is None
    pd.testing.assert_frame_equal(sig.vol_panel, trailing_volatility(TINY, 30))
    # Dispersion of the 2-row returns among members (ddof=1).
    r2 = np.array([0.10, 0.20, -0.10, 0.05, 0.30])
    r5 = np.array([0.0, 0.40, -0.10, 0.01])  # A excluded at row 5
    assert sig.dispersion.iloc[2] == pytest.approx(np.std(r2, ddof=1), rel=1e-12)
    assert sig.dispersion.iloc[5] == pytest.approx(np.std(r5, ddof=1), rel=1e-12)
    assert sig.dispersion.iloc[:2].isna().all()


def test_build_signals_max_weight_leaves_cash() -> None:
    sig = build_signals(
        tiny_plan(max_weight=0.3), TINY, ALL, dollar_volume=None, liquidity_cap_mult=None
    )
    assert weights_row(sig, 2) == {"A": 0.0, "B": 0.3, "C": 0.0, "D": 0.0, "E": 0.3}


def test_build_signals_banding_hand_checked() -> None:
    # quantile .2 -> n=1, exit .6 -> e=3. Row 2 picks E. Row 5 ranks A .5, C .4, E .01 (3rd):
    # E is held and still within the exit band, so it stays beside the new top name A.
    banded = build_signals(
        tiny_plan(quantile=0.2, exit_quantile=0.6),
        TINY,
        ALL,
        dollar_volume=None,
        liquidity_cap_mult=None,
    )
    assert weights_row(banded, 2) == {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0, "E": 1.0}
    assert weights_row(banded, 5) == {"A": 0.5, "B": 0.0, "C": 0.0, "D": 0.0, "E": 0.5}
    plain = build_signals(
        tiny_plan(quantile=0.2), TINY, ALL, dollar_volume=None, liquidity_cap_mult=None
    )
    assert weights_row(plain, 5) == {"A": 1.0, "B": 0.0, "C": 0.0, "D": 0.0, "E": 0.0}


def test_build_signals_gate_hand_checked() -> None:
    # Dispersion of 2-row returns: row 2 ~ 0.152, row 5 ~ 0.269. min 0.2 closes row 2 only.
    sig = build_signals(
        tiny_plan(quantile=0.2, exit_quantile=0.6, dispersion_gate=DispersionGate(2, 0.2)),
        TINY,
        ALL,
        dollar_volume=None,
        liquidity_cap_mult=None,
    )
    assert sig.gate is not None
    # Rows 3, 4, 6, 7 have dispersion 0, ~0.13, 0, ~0.195: all below 0.2.
    assert sig.gate.tolist() == [False, False, False, False, False, True, False, False]
    assert weights_row(sig, 2) == dict.fromkeys(SYMS, 0.0)  # gated: all cash
    # Gate reset the banding state, so row 5 is the plain top 1 (A), not A + E.
    assert weights_row(sig, 5) == {"A": 1.0, "B": 0.0, "C": 0.0, "D": 0.0, "E": 0.0}
    # Scoring starts on the first row the plan COULD have invested (row 2: 5 eligible names
    # >= min_names), not on the first open gate (row 5): gate-closed cash rows count.
    assert sig.first_decision == IDX[2]
    assert sig.investable.tolist() == [False, False, True, False, False, True, False, False]


def test_build_signals_gate_uses_its_own_lookback() -> None:
    plan = tiny_plan(dispersion_gate=DispersionGate(3, 0.01))
    sig = build_signals(plan, TINY, ALL, dollar_volume=None, liquidity_cap_mult=None)
    assert sig.gate is not None
    # 3-row returns need row t-3: rows 0..2 have none -> NaN -> gate closed.
    assert sig.gate.iloc[:3].tolist() == [False, False, False]
    assert weights_row(sig, 2) == dict.fromkeys(SYMS, 0.0)


def test_build_signals_inverse_vol() -> None:
    plan = tiny_plan(weighting="inverse_vol", vol_lookback=2)
    sig = build_signals(plan, TINY, ALL, dollar_volume=None, liquidity_cap_mult=None)
    assert sig.inverse_vol is not None
    pd.testing.assert_frame_equal(sig.vol_panel, trailing_volatility(TINY, 2))
    assert sig.inverse_vol is sig.vol_panel
    vol = sig.vol_panel.iloc[2]
    inv = 1.0 / vol[["B", "E"]]
    expected = inv / inv.sum()
    row = sig.weights.iloc[2]
    assert row["B"] == pytest.approx(expected["B"], rel=1e-12)
    assert row["E"] == pytest.approx(expected["E"], rel=1e-12)
    assert row[["A", "C", "D"]].tolist() == [0.0, 0.0, 0.0]
    assert math.fsum(row.tolist()) <= 1.0


def test_build_signals_liquidity_cap() -> None:
    dv = pd.DataFrame(
        np.tile([100.0, 100.0, 100.0, 100.0, 25.0], (len(IDX), 1)), index=IDX, columns=SYMS
    )
    sig = build_signals(tiny_plan(), TINY, ALL, dollar_volume=dv, liquidity_cap_mult=2.0)
    row = sig.weights.iloc[2]  # picks B, E at 0.5 before the cap; shares 100/425, 25/425
    assert row["B"] == pytest.approx(2.0 * 100.0 / 425.0, rel=1e-12)
    assert row["E"] == pytest.approx(2.0 * 25.0 / 425.0, rel=1e-12)
    # dollar_volume without a multiplier is ignored.
    same = build_signals(tiny_plan(), TINY, ALL, dollar_volume=dv, liquidity_cap_mult=None)
    plain = build_signals(tiny_plan(), TINY, ALL, dollar_volume=None, liquidity_cap_mult=None)
    pd.testing.assert_frame_equal(same.weights, plain.weights)


def test_build_signals_input_errors() -> None:
    with pytest.raises(ValueError, match="dollar_volume is None"):
        build_signals(tiny_plan(), TINY, ALL, dollar_volume=None, liquidity_cap_mult=2.0)
    with pytest.raises(ValueError, match="PortfolioPlan"):
        build_signals(H1, TINY, ALL, dollar_volume=None, liquidity_cap_mult=None)  # type: ignore[arg-type]


def test_build_signals_no_decision_is_none() -> None:
    sig = build_signals(
        tiny_plan(min_names=6), TINY, ALL, dollar_volume=None, liquidity_cap_mult=None
    )
    assert sig.first_decision is None
    assert (sig.weights[sig.schedule] == 0.0).all().all()


# --------------------------------------------------------------------------- perturbation


def _random_panel(seed: int = 7, n: int = 160, m: int = 9) -> tuple[pd.DataFrame, ...]:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC", name="time")
    cols = [f"S{j}" for j in range(m)]
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.04, (n, m)), axis=0))
    closes[rng.random((n, m)) < 0.03] = NAN  # holes, never filled
    closes[:40, m - 1] = NAN  # listed late
    members = rng.random((n, m)) > 0.15
    dv = rng.uniform(1e5, 1e7, (n, m))
    return (
        pd.DataFrame(closes, index=idx, columns=cols),
        pd.DataFrame(members, index=idx, columns=cols),
        pd.DataFrame(dv, index=idx, columns=cols),
    )


PERTURB_PLAN = PortfolioPlan(
    name="perturb",
    description="",
    interval="1d",
    score_feature="trailing_return",
    score_lookback=10,
    rebalance_every=5,
    rebalance_offset=2,
    quantile=0.3,
    exit_quantile=0.6,
    min_names=2,
    weighting="inverse_vol",
    vol_lookback=8,
    max_weight=0.4,
    dispersion_gate=DispersionGate(lookback=12, min=0.05),
)


def _assert_prefix_equal(a: Any, b: Any, t: int) -> None:
    np.testing.assert_array_equal(
        a.weights.iloc[: t + 1].to_numpy(), b.weights.iloc[: t + 1].to_numpy()
    )
    np.testing.assert_array_equal(
        a.schedule.iloc[: t + 1].to_numpy(), b.schedule.iloc[: t + 1].to_numpy()
    )
    np.testing.assert_array_equal(
        a.dispersion.iloc[: t + 1].to_numpy(), b.dispersion.iloc[: t + 1].to_numpy()
    )
    np.testing.assert_array_equal(
        a.vol_panel.iloc[: t + 1].to_numpy(), b.vol_panel.iloc[: t + 1].to_numpy()
    )
    np.testing.assert_array_equal(
        a.inverse_vol.iloc[: t + 1].to_numpy(), b.inverse_vol.iloc[: t + 1].to_numpy()
    )
    np.testing.assert_array_equal(a.gate.iloc[: t + 1].to_numpy(), b.gate.iloc[: t + 1].to_numpy())
    if a.first_decision is not None and a.first_decision <= a.weights.index[t]:
        assert b.first_decision == a.first_decision


@pytest.mark.parametrize("t", [30, 57, 99, 140])
def test_build_signals_perturbation(t: int) -> None:
    assert ok(plan_to_dict(PERTURB_PLAN)) == PERTURB_PLAN
    closes, members, dv = _random_panel()
    base = build_signals(PERTURB_PLAN, closes, members, dollar_volume=dv, liquidity_cap_mult=1.5)
    assert base.first_decision is not None
    assert (base.weights.fillna(0.0) > 0).any().any()  # the test exercises real picks

    rng = np.random.default_rng(t)
    k = len(closes) - (t + 1)
    shocked = closes.copy()
    shocked.iloc[t + 1 :] = closes.iloc[t + 1 :].to_numpy() * np.exp(rng.normal(0, 0.5, (k, 9)))
    shocked_members = members.copy()
    shocked_members.iloc[t + 1 :] = rng.random((k, 9)) > 0.5
    shocked_dv = dv.copy()
    shocked_dv.iloc[t + 1 :] = rng.uniform(1.0, 1e9, (k, 9))
    changed = build_signals(
        PERTURB_PLAN, shocked, shocked_members, dollar_volume=shocked_dv, liquidity_cap_mult=1.5
    )
    _assert_prefix_equal(base, changed, t)

    cut = slice(None, t + 1)
    truncated = build_signals(
        PERTURB_PLAN,
        closes.iloc[cut],
        members.iloc[cut],
        dollar_volume=dv.iloc[cut],
        liquidity_cap_mult=1.5,
    )
    _assert_prefix_equal(base, truncated, t)


# --------------------------------------------------------------------------- pinned offset


def test_the_plan_type_accepts_an_offset_but_the_pipeline_pins_it_at_zero() -> None:
    plan = ok(mutate("rebalance.offset", 3))  # the plan type (adversarial variants) allows it
    (err,) = pinned_offset_errors(plan)
    assert err.path == "rebalance.offset"
    assert "fixed by the lab at offset 0" in err.message and 'Use "offset": 0' in err.message
    assert LAB_REBALANCE_OFFSET == 0
    assert pinned_offset_errors(ok(H1)) == []
    offset = portfolio_plan_json_schema()["properties"]["rebalance"]["properties"]["offset"]
    assert offset["const"] == 0


# --------------------------------------------------------------------------- signal delay


def test_build_signals_signal_delay_hand_checked() -> None:
    """Delay 3 = one rebalance period on the tiny panel: the schedule stays on rows 2 and 5,
    row 2 has no row -1 to read (no members: all cash), and row 5 decides on row 2's scores
    and members."""
    members = ALL.copy()
    members.loc[IDX[2], "E"] = False  # E is not a member at row 2 (it is again at row 5)
    plan = tiny_plan()
    now = build_signals(plan, TINY, members, dollar_volume=None, liquidity_cap_mult=None)
    stale = build_signals(
        plan, TINY, members, dollar_volume=None, liquidity_cap_mult=None, signal_delay=3
    )
    assert stale.signal_delay == 3
    assert stale.schedule.tolist() == now.schedule.tolist()  # same rebalance rows
    assert weights_row(stale, 2) == dict.fromkeys(SYMS, 0.0)  # row -1 does not exist
    # Row 5 on row 2's scores (A .10 B .20 C -.10 D .05 E .30) and members (no E): B, A.
    assert weights_row(stale, 5) == {"A": 0.5, "B": 0.5, "C": 0.0, "D": 0.0, "E": 0.0}
    # Undelayed row 5 picks C, A (row 5's scores, E a member again, A .50 C .40).
    assert weights_row(now, 5) == {"A": 0.5, "B": 0.0, "C": 0.5, "D": 0.0, "E": 0.0}
    assert stale.first_decision == IDX[5] and now.first_decision == IDX[2]
    assert stale.dispersion.iloc[5] == now.dispersion.iloc[2]
    for i in (0, 1, 3, 4, 6, 7):
        assert stale.weights.iloc[i].isna().all()


def test_build_signals_signal_delay_reads_every_input_from_d_rows_earlier() -> None:
    from lab.registry import cross_section as xs

    closes, members, dv = _random_panel()
    d = 10
    sig = build_signals(
        PERTURB_PLAN, closes, members, dollar_volume=dv, liquidity_cap_mult=1.5, signal_delay=d
    )
    p = PERTURB_PLAN
    assert p.dispersion_gate is not None and p.vol_lookback is not None
    gate = xs.cross_sectional_dispersion(
        xs.trailing_return(closes, p.dispersion_gate.lookback), members
    )
    expected = xs.top_quantile_weights(
        xs.trailing_return(closes, p.score_lookback).shift(d),
        members.shift(d, fill_value=False).astype(bool),
        xs.rebalance_rows(closes.index, every=p.rebalance_every, offset=p.rebalance_offset),
        quantile=p.quantile,
        min_names=p.min_names,
        inverse_vol=trailing_volatility(closes, p.vol_lookback).shift(d),
        max_weight=p.max_weight,
        exit_quantile=p.exit_quantile,
        liquidity_share=xs.liquidity_share(dv, members).shift(d),
        liquidity_cap_mult=1.5,
        gate=(gate >= p.dispersion_gate.min).shift(d, fill_value=False).astype(bool),
    )
    pd.testing.assert_frame_equal(sig.weights, expected)
    assert (sig.weights.fillna(0.0) > 0).any().any()


def test_build_signals_signal_delay_must_be_a_whole_number() -> None:
    for bad in (-1, 1.5, True):
        with pytest.raises(ValueError, match="signal_delay"):
            build_signals(
                tiny_plan(),
                TINY,
                ALL,
                dollar_volume=None,
                liquidity_cap_mult=None,
                signal_delay=bad,  # type: ignore[arg-type]
            )


@pytest.mark.parametrize("t", [30, 99, 140])
def test_build_signals_with_signal_delay_perturbation(t: int) -> None:
    closes, members, dv = _random_panel()
    kw = {"liquidity_cap_mult": 1.5, "signal_delay": 15}
    base = build_signals(PERTURB_PLAN, closes, members, dollar_volume=dv, **kw)
    assert base.first_decision is not None
    rng = np.random.default_rng(t)
    k = len(closes) - (t + 1)
    shocked = closes.copy()
    shocked.iloc[t + 1 :] = closes.iloc[t + 1 :].to_numpy() * np.exp(rng.normal(0, 0.5, (k, 9)))
    shocked_members = members.copy()
    shocked_members.iloc[t + 1 :] = rng.random((k, 9)) > 0.5
    shocked_dv = dv.copy()
    shocked_dv.iloc[t + 1 :] = rng.uniform(1.0, 1e9, (k, 9))
    changed = build_signals(PERTURB_PLAN, shocked, shocked_members, dollar_volume=shocked_dv, **kw)
    _assert_prefix_equal(base, changed, t)
    np.testing.assert_array_equal(
        base.investable.iloc[: t + 1].to_numpy(), changed.investable.iloc[: t + 1].to_numpy()
    )


def test_first_decision_ignores_the_gate_and_needs_min_names() -> None:
    """NOTE 2: investable = a schedule row with >= min_names sizable names, gate or not."""
    members = ALL.copy()
    members.loc[IDX[2], ["A", "B", "C"]] = False  # 2 members at row 2 < min_names 3
    shut = tiny_plan(min_names=3, dispersion_gate=DispersionGate(2, 0.99))  # never opens
    sig = build_signals(shut, TINY, members, dollar_volume=None, liquidity_cap_mult=None)
    assert sig.investable.tolist() == [False, False, False, False, False, True, False, False]
    assert sig.first_decision == IDX[5]
    assert (sig.weights[sig.schedule] == 0.0).all().all()  # gate shut: all cash throughout
