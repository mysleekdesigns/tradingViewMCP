"""``persist(condition, k)``: PLAN.md Phase 20 Part C.

True at bar t only when the condition held on each of rows t-k+1..t. Known values on a hand-built
series, the perturbation test, ``persist(c, 1)`` equals ``c``, the validator's limits, the tunable
count, and literal lineage for ``k``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import pytest

from lab.plan import (
    MAX_PERSIST_BARS,
    CheckedExpr,
    ExprScope,
    PlanValidationError,
    check_expression,
    checked_expressions,
    compile_plan,
    evaluate_expression,
    parse_plan,
    persist,
    plan_hash,
    plan_to_dict,
    tunable_params,
    validate_plan,
    with_params,
)
from lab.plan.expr import MAX_PREV_BARS, _Evaluator
from lab.registry.features import make_spec
from lab.research.family import wf_lineage_problems
from lab.research.logic import alias_map, literal_vector, logic_hash, logic_payload
from tests.synthetic import make_candles, perturb_after

SCOPE = ExprScope(frozenset({"close", "open", "x", "y"}))
BoolArray = npt.NDArray[np.bool_]


def checked(source: str) -> CheckedExpr:
    expr, errors = check_expression(source, SCOPE, "rule")
    assert expr is not None, errors
    return expr


def run(source: str, x: npt.ArrayLike) -> BoolArray:
    arr = np.asarray(x, dtype=np.float64)
    return evaluate_expression(checked(source), {"x": arr}, len(arr))


def messages(source: str) -> str:
    expr, errors = check_expression(source, SCOPE, "rule")
    assert expr is None, "expected the rule to be rejected"
    return "\n".join(e.message for e in errors)


def edge(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": "ema_persist",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {"ema_slow": {"ema": {"period": 50}}, "m": {"rsi": {"period": 14}}},
        "long_entry": "persist(close > ema_slow, 3) and m < 70",
        "long_exit": "close < ema_slow",
    }
    raw.update(changes)
    return raw


def overlay(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "kind": "overlay",
        "name": "sma_persist_overlay",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {"sma_slow": {"sma": {"period": 20}}},
        "flat_when": "persist(close < sma_slow, 3)",
    }
    raw.update(changes)
    return raw


def errors_of(raw: Mapping[str, Any]) -> list[str]:
    plan, errors = validate_plan(raw)
    assert plan is None, "expected the plan to be rejected"
    return [f"{e.path}: {e.message}" for e in errors]


# --------------------------------------------------------------------------- known values


def test_known_values_on_a_hand_built_series() -> None:
    #      index  0  1  2  3  4  5  6  7  8  9
    x = np.array([1, 1, 1, 0, 1, 1, 1, 1, 0, 1], dtype=np.float64)
    got = run("persist(x > 0, 3)", x)
    assert np.flatnonzero(got).tolist() == [2, 6, 7]
    assert np.flatnonzero(run("persist(x > 0, 2)", x)).tolist() == [1, 2, 5, 6, 7]
    assert not run("persist(x > 0, 5)", x).any()
    assert np.flatnonzero(run("persist(x > 0, 4)", x)).tolist() == [7]


def test_the_public_helper_matches_the_expression_and_refuses_non_boolean_input() -> None:
    flags = np.array([True, True, False, True, True, True])
    assert persist(flags, 3).tolist() == [False, False, False, False, False, True]
    assert persist(flags, 1).tolist() == flags.tolist()
    with pytest.raises(ValueError, match="boolean array"):
        persist(np.array([1.0, np.nan, 1.0]), 2)  # a NaN would cast to True
    for bad in (0, MAX_PERSIST_BARS + 1, True, 2.0):
        with pytest.raises(ValueError, match="persist k must be an int"):
            persist(flags, bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("k", [1, 2, 3, 7, MAX_PERSIST_BARS])
def test_matches_a_plain_loop_on_random_data_with_nans(k: int) -> None:
    rng = np.random.default_rng(k)
    x = rng.normal(0.3, 1.0, 400)
    x[rng.random(400) < 0.03] = np.nan
    got = run(f"persist(x > 0, {k})", x)
    want = [i >= k - 1 and bool(np.all(x[i - k + 1 : i + 1] > 0)) for i in range(400)]
    assert got.tolist() == want


def test_shorter_than_k_is_never_true() -> None:
    assert run("persist(x > 0, 5)", [1.0, 1.0, 1.0]).tolist() == [False, False, False]
    assert run("persist(x > 0, 3)", np.empty(0)).tolist() == []


# --------------------------------------------------------------------------- validity


def test_validity_is_false_unless_all_k_rows_are_valid() -> None:
    #      index  0  1  2  3       4  5  6  7  8
    x = np.array([1, 1, 1, np.nan, 1, 1, 1, 1, 1], dtype=np.float64)
    # `not` must be False wherever the window is invalid: first k-1 rows, the NaN, and k-1 after.
    assert np.flatnonzero(run("not persist(x > 0, 3)", x)).tolist() == []
    assert np.flatnonzero(run("persist(x > 0, 3)", x)).tolist() == [2, 6, 7, 8]
    y = np.array([-1, -1, -1, np.nan, -1, -1, -1, -1, -1], dtype=np.float64)
    assert np.flatnonzero(run("not persist(x > 0, 3)", y)).tolist() == [2, 6, 7, 8]
    assert np.flatnonzero(run("persist(x > 0, 3) or x > 0", x)).tolist() == [2, 6, 7, 8]


def _risk_with_a_gap(candles: pd.DataFrame, _params: Mapping[str, float]) -> pd.DataFrame:
    """1.0 on every row (the rule below is always True) except a NaN at row 20."""
    values = np.ones(len(candles))
    values[20] = np.nan
    return pd.DataFrame({"value": values}, index=candles.index)


def test_overlay_flat_when_with_a_nan_stays_invested() -> None:
    gappy = make_spec("gappy", "Test feature.", (), ("value",), _risk_with_a_gap, lambda _p: 0)
    registry = {"gappy": gappy}
    raw = overlay(features={"g": {"gappy": {}}}, flat_when="persist(g > 0, 3)")
    out = compile_plan(parse_plan(raw, registry), make_candles(40, seed=4), registry)
    assert out.warmup_bars == 2
    invested = [20, 21, 22]  # the three windows that hold the NaN: unknown -> invested
    flat = [i for i in range(2, 40) if i not in invested]
    assert np.flatnonzero(out.exits.to_numpy()).tolist() == flat
    assert np.flatnonzero(out.entries.to_numpy()).tolist() == invested


# --------------------------------------------------------------------------- persist(c, 1) = c


@pytest.mark.parametrize(
    ("wrapped", "plain"),
    [
        ("persist(x > 0, 1)", "x > 0"),
        ("persist(prev(x, 2) > y, 1)", "prev(x, 2) > y"),
        ("persist(crosses_above(x, y), 1)", "crosses_above(x, y)"),
        ("persist(x > 0 and y < 1, 1)", "x > 0 and y < 1"),
    ],
)
def test_persist_one_equals_the_condition_bit_for_bit_including_validity(
    wrapped: str, plain: str
) -> None:
    rng = np.random.default_rng(11)
    x, y = rng.normal(0.0, 1.0, 300), rng.normal(0.0, 1.0, 300)
    x[rng.random(300) < 0.05] = np.nan
    y[rng.random(300) < 0.05] = np.nan
    columns = {"x": x, "y": y}
    a, b = checked(wrapped), checked(plain)
    assert a.lag == b.lag
    evaluator = _Evaluator(columns, 300)
    got, want = evaluator.ev(a.tree.body), evaluator.ev(b.tree.body)
    assert np.array_equal(got.values, want.values)
    assert np.array_equal(got.valid, want.valid)
    assert got.values.dtype == want.values.dtype and got.is_bool == want.is_bool
    assert (
        evaluate_expression(checked(f"not {wrapped}"), columns, 300).tobytes()
        == evaluate_expression(checked(f"not ({plain})"), columns, 300).tobytes()
    )


# --------------------------------------------------------------------------- no look-ahead


@pytest.mark.parametrize("k", [1, 3, 10])
@pytest.mark.parametrize("t", [40, 150, 260])
def test_perturbation_rows_up_to_t_are_bit_identical(t: int, k: int) -> None:
    rng = np.random.default_rng(100 * t + k)
    x = rng.normal(1.5, 1.0, 300)  # mostly positive, so even k=10 has true windows after t
    x[rng.random(300) < 0.02] = np.nan
    moved = x.copy()
    moved[t + 1 :] = rng.normal(-1.5, 1.0, 300 - t - 1)
    for source in (f"persist(x > 0, {k})", f"not persist(x > 0, {k})"):
        base, after = run(source, x), run(source, moved)
        assert base[: t + 1].tobytes() == after[: t + 1].tobytes(), source
    base, after = run(f"persist(x > 0, {k})", x), run(f"persist(x > 0, {k})", moved)
    assert base[t + 1 :].tobytes() != after[t + 1 :].tobytes()  # the test can see a change


@pytest.mark.parametrize("t", [120, 300])
def test_perturbation_through_the_compiled_plan(t: int) -> None:
    plan = parse_plan(edge())
    candles = make_candles(400, seed=7)
    base = compile_plan(plan, candles)
    moved = compile_plan(plan, perturb_after(candles, t))
    assert base.entries.any()
    assert base.entries.iloc[: t + 1].equals(moved.entries.iloc[: t + 1])
    assert base.exits.iloc[: t + 1].equals(moved.exits.iloc[: t + 1])
    assert not base.entries.iloc[t + 1 :].equals(moved.entries.iloc[t + 1 :])


# --------------------------------------------------------------------------- lag and warm-up


def test_lag_grows_by_k_minus_one_and_records_each_call() -> None:
    assert checked("persist(x > 0, 1)").lag == 0
    assert checked("persist(x > 0, 3)").lag == 2
    assert checked("persist(prev(x, 4) > 0, 3)").lag == 6
    assert checked("prev(persist(x > 0, 3), 4)").lag == 6
    assert checked("persist(crosses_above(x, y), 3)").lag == 3
    assert checked("not persist(x > 0, 3)").lag == 2
    assert checked("persist(x > 0, 3) and persist(y > 0, 5)").lag == 4
    assert checked("persist(x > 0, 3) and persist(y > 0, 5)").persist_ks == (3, 5)
    assert checked("x > y").persist_ks == ()


def test_compile_warm_up_includes_the_persist_lag() -> None:
    candles = make_candles(300, seed=2)
    plain = compile_plan(parse_plan(edge(long_entry="close > ema_slow and m < 70")), candles)
    held = compile_plan(parse_plan(edge()), candles)
    assert held.warmup_bars == plain.warmup_bars + 2
    assert not held.entries.iloc[: held.warmup_bars].any()


def test_overlay_entry_keeps_the_persist_record() -> None:
    entry, flat = checked_expressions(parse_plan(overlay()))
    assert entry.persist_ks == flat.persist_ks == (3,)
    assert entry.lag == flat.lag == 2


# --------------------------------------------------------------------------- validator limits


def test_max_persist_bars_is_the_configured_limit() -> None:
    assert MAX_PERSIST_BARS == 50
    assert checked(f"persist(x > 0, {MAX_PERSIST_BARS})").persist_ks == (MAX_PERSIST_BARS,)


@pytest.mark.parametrize(
    "k", ["0", "-1", str(MAX_PERSIST_BARS + 1), "2.5", "3.0", "True", "x", "1 + 2", "'3'"]
)
def test_validator_rejects_a_bad_k_and_says_what_is_allowed(k: str) -> None:
    msg = messages(f"persist(x > 0, {k})")
    assert f"whole-number literal bar count from 1 to {MAX_PERSIST_BARS}" in msg
    assert f"`{k}`" in msg
    assert "persist(close > ema_slow, 3)" in msg


def test_the_whole_plan_is_rejected_for_a_bad_k_at_the_rule_path() -> None:
    for k in (0, MAX_PERSIST_BARS + 1):
        assert any(
            e.startswith("long_entry: ")
            for e in errors_of(edge(long_entry=f"persist(close > ema_slow, {k}) and m < 70"))
        )
    assert parse_plan(edge(long_entry="persist(close > ema_slow, 50) and m < 70"))


@pytest.mark.parametrize("arg", ["x", "x + y", "1", "prev(x, 1)"])
def test_first_argument_must_be_a_condition(arg: str) -> None:
    assert "`persist` needs a condition" in messages(f"persist({arg}, 3)")


def test_bad_condition_and_bad_k_are_both_reported() -> None:
    msg = messages("persist(x, 0)")
    assert "`persist` needs a condition" in msg and "whole-number literal" in msg


@pytest.mark.parametrize(
    ("source", "text"),
    [
        ("persist(x > 0, k=3)", "positional arguments only"),
        ("persist(x > 0)", "takes exactly 2 arguments"),
        ("persist(x > 0, 3, 4)", "takes exactly 2 arguments"),
        ("persist", "is a function"),
        ("persist(*x, 3)", "unpacking"),
    ],
)
def test_call_shape_errors_show_a_working_example(source: str, text: str) -> None:
    msg = messages(source)
    assert text in msg and "persist(close > ema_slow, 3)" in msg


def test_error_texts_list_persist() -> None:
    assert "persist(condition, k)" in messages("foo(x, 1)")
    assert "persist(condition, k)" in messages("x[1] > 0")
    assert MAX_PREV_BARS == 500  # `prev` keeps its own limit


def test_persist_is_a_reserved_alias() -> None:
    feats = {"persist": {"ema": {"period": 20}}}
    errors = errors_of(edge(features=feats, long_entry="close > 1", long_exit="close < 1"))
    assert any(e.startswith("features.persist: ") and "function name" in e for e in errors)


def test_a_persist_result_cannot_be_used_as_a_number() -> None:
    assert "needs a number" in messages("persist(x > 0, 3) + 1 > 0")
    assert "needs a number" in messages("crosses_above(persist(x > 0, 3), y)")


# --------------------------------------------------------------------------- tunable count


FIVE_PARAMS: dict[str, Any] = {
    "macd": {"macd": {}},  # fast, slow, signal
    "ema_slow": {"ema": {"period": 50}},
    "m": {"rsi": {"period": 14}},
}
SIX_PARAMS: dict[str, Any] = {**FIVE_PARAMS, "z": {"zscore": {"period": 20}}}


def rules_for(entry: str, exit_: str = "macd.hist < 0 and close < ema_slow") -> dict[str, Any]:
    return {"long_entry": entry, "long_exit": exit_}


def test_edge_limit_accepts_params_plus_persist_up_to_six() -> None:
    raw = edge(
        features=FIVE_PARAMS,
        **rules_for("persist(macd.hist > 0, 3) and close > ema_slow and m < 70"),
    )
    assert parse_plan(raw)


def test_edge_limit_refuses_six_params_plus_one_persist_naming_both() -> None:
    raw = edge(
        features=SIX_PARAMS,
        **rules_for("persist(macd.hist > 0, 3) and close > ema_slow and m < 70 and z < 2"),
    )
    errors = errors_of(raw)
    assert len(errors) == 1
    msg = errors[0]
    assert msg.startswith("features: ")
    assert "7 tunable parameters" in msg and "6 feature params" in msg
    assert "macd: 3" in msg and "ema_slow: 1" in msg
    assert "1 `persist` call(s) (long_entry: 1)" in msg
    assert "the limit is 6" in msg


def test_persist_calls_count_across_entry_and_exit() -> None:
    raw = edge(
        features=FIVE_PARAMS,
        long_entry="persist(macd.hist > 0, 3) and close > ema_slow and m < 70",
        long_exit="persist(macd.hist < 0, 2) and persist(close < ema_slow, 2)",
    )
    errors = errors_of(raw)
    assert len(errors) == 1
    assert "8 tunable parameters" in errors[0]
    assert "3 `persist` call(s) (long_entry: 1, long_exit: 2)" in errors[0]


def test_persist_calls_count_even_when_the_rule_has_another_error() -> None:
    raw = edge(
        features=SIX_PARAMS,
        **rules_for("persist(macd.hist > 0, 3) and close > 50000 and m < 70 and z < 2"),
    )
    errors = errors_of(raw)
    assert any(e.startswith("long_entry: ") for e in errors)
    assert any(e.startswith("features: ") and "`persist` call(s)" in e for e in errors)


def test_overlay_limit_counts_persist_once_and_names_its_reason() -> None:
    two = {"sma_slow": {"sma": {"period": 20}}, "m": {"rsi": {"period": 14}}}
    assert parse_plan(overlay(features=two, flat_when="persist(close < sma_slow, 3) and m < 30"))
    errors = errors_of(
        overlay(
            features=two,
            flat_when="persist(close < sma_slow, 3) and persist(m < 30, 2)",
        )
    )
    assert len(errors) == 1
    assert "4 tunable parameters" in errors[0] and "the limit is 3" in errors[0]
    assert "2 `persist` call(s) (flat_when: 2)" in errors[0]
    assert "overlays use published parameters" in errors[0]


def test_persist_is_not_an_optuna_tunable() -> None:
    plan = parse_plan(edge())
    assert set(tunable_params(plan)) == {"ema_slow.period", "m.period"}
    assert "persist" in with_params(plan, {"m.period": 21}).long_entry
    for key in ("persist", "persist.k", "long_entry.persist", "ema_slow.k"):
        with pytest.raises(PlanValidationError, match="unknown tunable param"):
            with_params(plan, {key: 5})


def test_plan_round_trip_and_hash_see_k() -> None:
    plan = parse_plan(edge())
    assert parse_plan(plan_to_dict(plan)) == plan
    other = parse_plan(edge(long_entry="persist(close > ema_slow, 4) and m < 70"))
    assert plan_hash(plan) != plan_hash(other)


# --------------------------------------------------------------------------- literal lineage


def k_plan(k: int, threshold: int = 70) -> dict[str, Any]:
    return edge(long_entry=f"persist(close > ema_slow, {k}) and m < {threshold}")


def test_plans_differing_only_in_k_share_a_logic_hash_not_a_literal_vector() -> None:
    assert logic_hash(k_plan(2)) == logic_hash(k_plan(3))
    assert literal_vector(k_plan(2)) == (2.0, 70.0)
    assert literal_vector(k_plan(3)) == (3.0, 70.0)
    assert literal_vector(k_plan(3, 60)) == (3.0, 60.0)


def test_persist_is_a_different_logic_from_the_bare_condition() -> None:
    bare = edge(long_entry="close > ema_slow and m < 70")
    assert logic_hash(bare) != logic_hash(k_plan(3))


@pytest.mark.parametrize(
    ("wrapped", "plain"),
    [
        ("persist(close > ema_slow, 1) and m < 70", "close > ema_slow and m < 70"),
        ("not persist(close > ema_slow, 1) and m < 70", "not close > ema_slow and m < 70"),
        ("persist(close > ema_slow and m < 70, 1)", "close > ema_slow and m < 70"),
        ("persist(m < 70, 1) and close > ema_slow", "m < 70 and close > ema_slow"),
        (
            "prev(persist(m < 70, 1), 2) and close > ema_slow",
            "prev(m < 70, 2) and close > ema_slow",
        ),
    ],
)
def test_persist_one_is_the_same_logic_as_its_condition(wrapped: str, plain: str) -> None:
    a, b = edge(long_entry=wrapped), edge(long_entry=plain)
    assert logic_hash(a) == logic_hash(b)
    assert logic_payload(a) == logic_payload(b)
    assert literal_vector(a) == literal_vector(b)
    assert alias_map(a) == alias_map(b)
    assert plan_hash(parse_plan(a)) != plan_hash(parse_plan(b))  # still its own ledger trial


def test_persist_one_is_normalised_in_long_exit_and_in_overlays() -> None:
    plain = edge(long_exit="close < ema_slow")
    wrapped = edge(long_exit="persist(close < ema_slow, 1)")
    assert logic_hash(wrapped) == logic_hash(plain)
    assert literal_vector(wrapped) == literal_vector(plain)
    flat, one = (
        overlay(flat_when="close < sma_slow"),
        overlay(flat_when="persist(close < sma_slow, 1)"),
    )
    assert logic_hash(one) == logic_hash(flat)
    assert literal_vector(one) == literal_vector(flat) == ()


def test_only_the_unit_call_is_normalised() -> None:
    mixed = edge(long_entry="persist(close > ema_slow, 3) and persist(m < 70, 1)")
    plain = edge(long_entry="persist(close > ema_slow, 3) and m < 70")
    assert logic_hash(mixed) == logic_hash(plain)
    assert literal_vector(mixed) == literal_vector(plain) == (3.0, 70.0)


def test_persist_two_and_up_stay_one_logic_apart_from_the_bare_condition() -> None:
    one = logic_hash(k_plan(1))
    assert one == logic_hash(edge(long_entry="close > ema_slow and m < 70"))
    assert one != logic_hash(k_plan(2))
    assert logic_hash(k_plan(2)) == logic_hash(k_plan(50))
    assert literal_vector(k_plan(2)) != literal_vector(k_plan(50))


def test_normalising_leaves_the_checked_rule_alone() -> None:
    plan = parse_plan(edge(long_entry="persist(close > ema_slow, 1) and m < 70"))
    logic_hash(plan)
    entry, _ = checked_expressions(plan)
    assert plan.long_entry == entry.source == "persist(close > ema_slow, 1) and m < 70"
    assert entry.persist_ks == (1,)


def test_overlay_k_is_lineage_tracked_too() -> None:
    a, b = overlay(), overlay(flat_when="persist(close < sma_slow, 5)")
    assert logic_hash(a) == logic_hash(b)
    assert literal_vector(a) != literal_vector(b)


def test_hand_tuning_k_trips_the_literal_lineage_rule() -> None:
    plan = parse_plan(k_plan(3))
    tried = {logic_hash(plan): {literal_vector(k_plan(2)), literal_vector(k_plan(3))}}
    problems = wf_lineage_problems(plan, {}, {}, tried)
    assert any("hand-tuned across plans" in p for p in problems)
    single = {logic_hash(plan): {literal_vector(k_plan(3))}}
    assert not any("hand-tuned" in p for p in wf_lineage_problems(plan, {}, {}, single))


def test_nested_persist_is_refused_so_the_limit_is_a_hard_limit() -> None:
    scope = ExprScope(single=frozenset({"close", "open"}))
    checked, errors = check_expression("persist(persist(close > open, 50), 50)", scope, "rule")
    assert checked is None
    assert any("`persist` inside `persist`" in e.message for e in errors)
