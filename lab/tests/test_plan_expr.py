"""Expression language semantics on tiny hand-made series (rule functions, NaN edges, logic)."""

from __future__ import annotations

import numpy as np
import pytest

from lab.plan import (
    CheckedExpr,
    ExprScope,
    check_expression,
    crosses_above,
    crosses_below,
    evaluate_expression,
    prev,
)
from lab.types import PRICE_INPUTS

NAN = np.nan
SCOPE = ExprScope(
    single=frozenset({*PRICE_INPUTS, "a", "b"}),
    multi={"m": ("x", "y")},
)


def checked(src: str) -> CheckedExpr:
    expr, errors = check_expression(src, SCOPE, "long_entry")
    assert errors == [], errors
    assert expr is not None
    return expr


def run(src: str, **cols: list[float]) -> list[bool]:
    arrays = {k.replace("__", "."): np.asarray(v, dtype=float) for k, v in cols.items()}
    n = len(next(iter(arrays.values())))
    return evaluate_expression(checked(src), arrays, n).tolist()


def messages(src: str) -> list[str]:
    expr, errors = check_expression(src, SCOPE, "long_exit")
    assert expr is None
    assert errors and all(e.path == "long_exit" for e in errors)
    return [e.message for e in errors]


# --------------------------------------------------------------------------- rule functions


def test_crosses_above_hand_checked() -> None:
    a = [1.0, 2.0, 3.0, 2.0, 4.0]
    b = [2.0, 2.0, 2.0, 2.0, 2.0]
    # t=2: 3 > 2 and 2 <= 2 -> True; t=4: 4 > 2 and 2 <= 2 -> True; t=1: 2 > 2 False.
    assert crosses_above(a, b).tolist() == [False, False, True, False, True]


def test_crosses_below_hand_checked() -> None:
    a = [3.0, 2.0, 1.0, 2.0, 1.0]
    # t=2: 1 < 2 and 2 >= 2; t=4: 1 < 2 and 2 >= 2.
    assert crosses_below(a, 2.0).tolist() == [False, False, True, False, True]


def test_crosses_false_when_any_operand_nan() -> None:
    a = [1.0, NAN, 3.0, 1.0, 3.0]
    b = [2.0, 2.0, 2.0, 2.0, NAN]
    # t=2 would cross but a[t-1] is NaN; t=4 would cross but b[t] is NaN.
    assert crosses_above(a, b).tolist() == [False] * 5


def test_first_bar_never_crosses() -> None:
    assert crosses_above([5.0, 6.0], [1.0, 1.0]).tolist() == [False, False]


def test_prev_shifts_forward_strictly_past() -> None:
    out = prev([1.0, 2.0, 3.0, 4.0], 2)
    assert np.isnan(out[:2]).all()
    assert out[2:].tolist() == [1.0, 2.0]


def test_prev_longer_than_series_is_all_nan() -> None:
    assert np.isnan(prev([1.0, 2.0], 5)).all()


@pytest.mark.parametrize("n", [0, -1, 501, 1.5, True])
def test_prev_rejects_bad_n(n: object) -> None:
    with pytest.raises(ValueError, match="prev n"):
        prev([1.0], n)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- evaluation


def test_comparison_and_or_not() -> None:
    a = [1.0, 2.0, 3.0, 4.0]
    b = [4.0, 3.0, 2.0, 1.0]
    assert run("a > b", a=a, b=b) == [False, False, True, True]
    assert run("a > 1 and b > 1", a=a, b=b) == [False, True, True, False]
    assert run("a < 2 or b < 2", a=a, b=b) == [True, False, False, True]
    assert run("not a > b", a=a, b=b) == [True, True, False, False]


def test_arithmetic_precedence_and_unary_minus() -> None:
    a = [1.0, 2.0, 3.0]
    b = [1.0, 1.0, 1.0]
    # a * 2 - b: 1, 3, 5 ; -a + 4: 3, 2, 1
    assert run("a * 2 - b > 2", a=a, b=b) == [False, True, True]
    assert run("-a + 4 >= 2", a=a, b=b) == [True, True, False]
    assert run("(a + b) / 2 <= 1.5", a=a, b=b) == [True, True, False]


def test_division_by_zero_is_false_not_inf() -> None:
    assert run("a / b > 0", a=[1.0, 1.0], b=[0.0, 2.0]) == [False, True]


def test_chained_comparison_means_and() -> None:
    assert run("30 < a < 70", a=[20.0, 50.0, 80.0]) == [False, True, False]


def test_nan_operand_makes_whole_expression_false_even_through_or_and_not() -> None:
    a = [NAN, 1.0]
    b = [5.0, 5.0]
    assert run("a > 0 or b > 0", a=a, b=b) == [False, True]
    assert run("not a > 10", a=a, b=b) == [False, True]


def test_prev_inside_expression() -> None:
    a = [1.0, 2.0, 1.0, 3.0]
    assert run("a > prev(a, 1)", a=a) == [False, True, False, True]


def test_prev_of_condition() -> None:
    a = [1.0, 3.0, 1.0, 3.0]
    assert run("prev(a > 2, 1) and a < 2", a=a) == [False, False, True, False]


def test_multi_output_attribute_reads_column() -> None:
    assert run("m.x > m.y", m__x=[1.0, 3.0], m__y=[2.0, 2.0]) == [False, True]


def test_crosses_inside_expression_matches_rule_function() -> None:
    a = [1.0, 2.0, 3.0, 2.0, 4.0]
    assert run("crosses_above(a, 2)", a=a) == crosses_above(a, 2.0).tolist()


def test_lag_and_refs_reported() -> None:
    e = checked("crosses_above(prev(a, 3), b) or prev(prev(close, 2), 4) > m.x")
    assert e.lag == 6  # prev(prev(.,2),4) = 6 beats prev 3 + cross 1 = 4
    assert e.max_prev == 4
    assert e.uses_cross
    assert e.refs == {"a", "b", "close", "m.x"}


def test_evaluate_requires_all_columns() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        evaluate_expression(checked("a > b"), {"a": np.ones(3)}, 3)


# --------------------------------------------------------------------------- rejections


@pytest.mark.parametrize(
    ("src", "fragment"),
    [
        ("a == b", "`==` is not allowed"),
        ("a != b", "`!=` is not allowed"),
        ("a + b", "expression must be a condition"),
        ("a > b and 3", "needs a condition"),
        ("(a > b) + 1 > 0", "needs a number"),
        ("prev(a, b) > 0", "whole-number literal"),
        ("prev(a, 0) > 0", "from 1 to 500"),
        ("prev(a, 501) > 0", "from 1 to 500"),
        ("prev(a, 2.0) > 0", "whole-number literal"),
        ("crosses_above(a) ", "exactly 2 arguments"),
        ("crosses_above(1, 2)", "two constants"),
        ("2 > 1", "compares constants"),
        ("prev(3, 1) > a", "constant"),
        ("m > 0", "has several outputs"),
        ("a.value > 0", "write `a`"),
        ("close.x > 0", "price inputs have no outputs"),
        ("m.x.y > 0", "may only follow a feature alias"),
        ("prev > 0", "is a function"),
        ("crosses_abve(a, b)", "Did you mean `crosses_above`?"),
        ("a > b or ~(a > b)", "use `not`"),
        ("a % 2 > 0", "modulo"),
        ("a > 1e999", "too large"),
        ("", "empty"),
    ],
)
def test_rejected_expressions_explain_fix(src: str, fragment: str) -> None:
    assert any(fragment in m for m in messages(src)), messages(src)


def test_errors_include_column() -> None:
    (msg,) = messages("a > b and zz > 1")
    assert "unknown name `zz`" in msg and "(column 11)" in msg


def test_all_errors_in_one_expression_collected() -> None:
    msgs = messages("foo > 1 and bar < 2 and a == b")
    assert len(msgs) == 3


def test_deep_nesting_rejected_cleanly() -> None:
    src = "-" * 200 + "a > 0"
    assert any("nested too deeply" in m for m in messages(src))


def test_lenient_alias_suppresses_cascading_errors() -> None:
    scope = ExprScope(single=frozenset(PRICE_INPUTS), lenient=frozenset({"broken"}))
    expr, errors = check_expression("broken > close and broken.any < 1", scope, "long_entry")
    assert errors == [] and expr is not None
