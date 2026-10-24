"""Safe expression language for plan rules (``long_entry`` / ``long_exit``).

Expressions are parsed with ``ast.parse(mode="eval")`` and checked against a strict whitelist.
Nothing is ever passed to ``eval``/``exec``/``compile``: evaluation walks the checked tree and
applies vectorised numpy operations to columns the caller supplies.

Grammar (everything else is rejected with an error naming the token):

* numbers: ``35``, ``0.5``, ``1e-3`` (finite; ``True``/``False``/strings are not numbers)
* names: price inputs ``open high low close volume``; single-output feature aliases
  (``ema_fast``); ``alias.output`` for multi-output features (``macd.hist``)
* arithmetic: ``+ - * /`` and unary ``-``/``+`` on numbers (division by zero gives NaN)
* comparisons: ``> < >= <=`` on numbers, chaining allowed (``30 < rsi < 70`` means
  ``30 < rsi and rsi < 70``); ``==``/``!=`` are rejected (float equality is fragile)
* logic: ``and``, ``or``, ``not`` on conditions
* rule functions:
  ``crosses_above(a, b)``: ``a[t] > b[t] and a[t-1] <= b[t-1]``;
  ``crosses_below(a, b)``: ``a[t] < b[t] and a[t-1] >= b[t-1]``;
  ``prev(x, n)``: ``x`` as it was ``n`` bars ago (literal int, 1..500, strictly past data).

NaN semantics (conservative, no look-ahead): every value carries a validity mask. A NaN or
non-finite operand anywhere in an expression makes the whole expression False at that bar,
including through ``or`` and ``not``. The top-level result must be a condition.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import ast
import difflib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt

from lab.types import PRICE_INPUTS, PlanError

FUNCTIONS: tuple[str, ...] = ("crosses_above", "crosses_below", "prev")
MAX_EXPR_CHARS = 500
MAX_PREV_BARS = 500
MAX_DEPTH = 60  # nesting limit; keeps the checker far from Python's recursion limit

ALLOWED_SUMMARY = (
    "Allowed: numbers; price inputs (open, high, low, close, volume); feature aliases "
    "defined under `features` (multi-output ones as `alias.output`); arithmetic + - * /; "
    "comparisons > < >= <=; and, or, not; crosses_above(a, b), crosses_below(a, b), prev(x, n)."
)

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]


# --------------------------------------------------------------------------- rule functions


def _shift_float(x: FloatArray, n: int) -> FloatArray:
    out = np.full(x.shape, np.nan, dtype=np.float64)
    if n < len(x):
        out[n:] = x[: len(x) - n]
    return out


def _shift_bool(x: BoolArray, n: int) -> BoolArray:
    out = np.zeros(x.shape, dtype=np.bool_)
    if n < len(x):
        out[n:] = x[: len(x) - n]
    return out


def _as_float(x: npt.ArrayLike) -> FloatArray:
    return np.asarray(x, dtype=np.float64)


def prev(x: npt.ArrayLike, n: int) -> FloatArray:
    """``x`` shifted forward by ``n`` bars (value from ``n`` bars ago); the first ``n`` are NaN."""
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_PREV_BARS:
        raise ValueError(f"prev n must be an int in [1, {MAX_PREV_BARS}], got {n!r}")
    return _shift_float(_as_float(x), n)


def _cross(a: FloatArray, b: FloatArray, va: BoolArray, vb: BoolArray, up: bool) -> BoolArray:
    ok = va & vb
    ok_prev = _shift_bool(ok, 1)
    a_prev = _shift_float(a, 1)
    b_prev = _shift_float(b, 1)
    with np.errstate(invalid="ignore"):
        hit = (a > b) & (a_prev <= b_prev) if up else (a < b) & (a_prev >= b_prev)
    return hit & ok & ok_prev


def crosses_above(a: npt.ArrayLike, b: npt.ArrayLike) -> BoolArray:
    """True at t when ``a[t] > b[t]`` and ``a[t-1] <= b[t-1]``; False if any of them is NaN."""
    fa, fb = np.broadcast_arrays(_as_float(a), _as_float(b))
    return _cross(fa, fb, np.isfinite(fa), np.isfinite(fb), up=True)


def crosses_below(a: npt.ArrayLike, b: npt.ArrayLike) -> BoolArray:
    """True at t when ``a[t] < b[t]`` and ``a[t-1] >= b[t-1]``; False if any of them is NaN."""
    fa, fb = np.broadcast_arrays(_as_float(a), _as_float(b))
    return _cross(fa, fb, np.isfinite(fa), np.isfinite(fb), up=False)


# --------------------------------------------------------------------------- static checking


@dataclass(frozen=True)
class ExprScope:
    """Names an expression may reference.

    ``single``: bare names (price inputs + single-output aliases). ``multi``: alias -> outputs.
    ``lenient``: aliases whose feature definition already has an error; references to them are
    accepted silently so one mistake does not cascade. ``registry_names``: registry features,
    used only to suggest a fix when an expression names a feature the plan did not define.
    """

    single: frozenset[str]
    multi: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    lenient: frozenset[str] = frozenset()
    registry_names: frozenset[str] = frozenset()

    def known_names(self) -> list[str]:
        return sorted({*self.single, *self.multi, *self.lenient})


@dataclass(frozen=True)
class CheckedExpr:
    """A whitelisted, type-checked expression ready for :func:`evaluate_expression`.

    ``lag``: bars of history the expression looks back beyond its inputs (sum of nested
    ``prev`` n, +1 per ``crosses_*``). ``refs``: column names it reads ("close", "ema_fast",
    "macd.hist").
    """

    source: str
    tree: ast.Expression
    lag: int
    max_prev: int
    uses_cross: bool
    refs: frozenset[str]


Kind = Literal["num", "bool", "bad"]


@dataclass(frozen=True)
class _T:
    kind: Kind
    const: bool = False
    lag: int = 0


_BAD = _T("bad")

_DISALLOWED_NAMES: dict[type[ast.AST], str] = {
    ast.Lambda: "lambda",
    ast.Subscript: "indexing/subscript `[...]`",
    ast.Slice: "slice",
    ast.ListComp: "list comprehension",
    ast.SetComp: "set comprehension",
    ast.DictComp: "dict comprehension",
    ast.GeneratorExp: "generator expression",
    ast.IfExp: "`x if c else y` expression",
    ast.NamedExpr: "`:=` assignment",
    ast.List: "list literal",
    ast.Tuple: "tuple",
    ast.Dict: "dict literal",
    ast.Set: "set literal",
    ast.JoinedStr: "f-string",
    ast.FormattedValue: "f-string",
    ast.Starred: "`*` unpacking",
    ast.Await: "await",
    ast.Yield: "yield",
    ast.YieldFrom: "yield",
}

_BINOP_HINTS: dict[type[ast.operator], str] = {
    ast.BitAnd: "use `and` to combine conditions",
    ast.BitOr: "use `or` to combine conditions",
    ast.BitXor: "rewrite with `and`/`or`/`not`",
    ast.Pow: "write the multiplication out, e.g. `x * x`",
    ast.Mod: "modulo is not supported",
    ast.FloorDiv: "use `/`",
    ast.MatMult: "matrix multiply is not supported",
    ast.LShift: "bit shifts are not supported",
    ast.RShift: "bit shifts are not supported",
}

_CMP_TEXT: dict[type[ast.cmpop], str] = {
    ast.Gt: ">",
    ast.Lt: "<",
    ast.GtE: ">=",
    ast.LtE: "<=",
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Is: "is",
    ast.IsNot: "is not",
}


def _did_you_mean(word: str, options: list[str] | tuple[str, ...]) -> str:
    match = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return f" Did you mean `{match[0]}`?" if match else ""


def _depth(node: ast.AST) -> int:
    best = 0
    stack: list[tuple[ast.AST, int]] = [(node, 1)]
    while stack:
        cur, d = stack.pop()
        best = max(best, d)
        stack.extend((child, d + 1) for child in ast.iter_child_nodes(cur))
    return best


class _Checker:
    def __init__(self, source: str, scope: ExprScope, path: str) -> None:
        self.source = source
        self.scope = scope
        self.path = path
        self.errors: list[PlanError] = []
        self.refs: set[str] = set()
        self.max_prev = 0
        self.uses_cross = False

    def err(self, node: ast.AST, message: str) -> _T:
        col = getattr(node, "col_offset", None)
        where = f" (column {col + 1})" if isinstance(col, int) else ""
        self.errors.append(PlanError(self.path, f"{message}{where}"))
        return _BAD

    def text(self, node: ast.AST) -> str:
        seg = ast.get_source_segment(self.source, node) or type(node).__name__
        return seg if len(seg) <= 40 else seg[:37] + "..."

    def check(self, node: ast.AST) -> _T:
        if isinstance(node, ast.Constant):
            return self.constant(node)
        if isinstance(node, ast.Name):
            return self.name(node)
        if isinstance(node, ast.Attribute):
            return self.attribute(node)
        if isinstance(node, ast.UnaryOp):
            return self.unary(node)
        if isinstance(node, ast.BinOp):
            return self.binop(node)
        if isinstance(node, ast.BoolOp):
            return self.boolop(node)
        if isinstance(node, ast.Compare):
            return self.compare(node)
        if isinstance(node, ast.Call):
            return self.call(node)
        kind = _DISALLOWED_NAMES.get(type(node), type(node).__name__)
        return self.err(node, f"`{self.text(node)}` is not allowed: {kind}. {ALLOWED_SUMMARY}")

    # -- leaves

    def constant(self, node: ast.Constant) -> _T:
        v = node.value
        if isinstance(v, bool) or v is None:
            return self.err(
                node,
                f"`{v!r}` is not allowed: only numbers are literals; write a condition such "
                "as `close > open` instead of True/False/None",
            )
        if isinstance(v, str | bytes):
            return self.err(
                node,
                f"`{self.text(node)}` is not allowed: strings are not part of the language. "
                f"{ALLOWED_SUMMARY}",
            )
        if not isinstance(v, int | float):
            return self.err(node, f"`{self.text(node)}` is not allowed: only real numbers")
        try:
            fv = float(v)
        except OverflowError:
            fv = math.inf
        if not math.isfinite(fv):
            return self.err(node, f"number `{self.text(node)}` is too large; use a finite number")
        return _T("num", const=True)

    def name(self, node: ast.Name) -> _T:
        n = node.id
        sc = self.scope
        if n in sc.lenient:
            return _T("num")
        if n in sc.single:
            self.refs.add(n)
            return _T("num")
        if n in sc.multi:
            outs = ", ".join(f"{n}.{o}" for o in sc.multi[n])
            return self.err(node, f"`{n}` has several outputs; use one of: {outs}")
        if n in FUNCTIONS:
            return self.err(node, f"`{n}` is a function; call it, e.g. `{_example_call(n)}`")
        hint = _did_you_mean(n, sc.known_names())
        if n in sc.registry_names:
            hint += (
                f" `{n}` is a registry feature but this plan does not define it; add it under "
                f'features, e.g. "{n}": {{"{n}": {{}}}}'
            )
        known = ", ".join(sc.known_names())
        return self.err(node, f"unknown name `{n}`. Known names: {known}.{hint}")

    def attribute(self, node: ast.Attribute) -> _T:
        base = node.value
        if not isinstance(base, ast.Name):
            return self.err(
                node,
                f"`{self.text(node)}` is not allowed: `.` may only follow a feature alias, "
                "as in `macd.hist`",
            )
        b, attr = base.id, node.attr
        sc = self.scope
        if b in sc.lenient:
            return _T("num")
        if b in sc.multi:
            outs = sc.multi[b]
            if attr in outs:
                self.refs.add(f"{b}.{attr}")
                return _T("num")
            listed = ", ".join(f"{b}.{o}" for o in outs)
            return self.err(
                node,
                f"`{b}` has no output `{attr}`; its outputs are: {listed}."
                f"{_did_you_mean(attr, outs)}",
            )
        if b in sc.single:
            what = "price inputs have no outputs" if b in PRICE_INPUTS else "it has one output"
            return self.err(node, f"`{b}.{attr}` is not allowed: {what}; write `{b}`")
        return self.err(
            node,
            f"`{self.text(node)}` is not allowed: `{b}` is not a feature alias. "
            f"Known names: {', '.join(sc.known_names())}",
        )

    # -- operators

    def need_num(self, node: ast.AST, t: _T, context: str) -> bool:
        if t.kind == "bool":
            self.err(
                node,
                f"{context} needs a number but `{self.text(node)}` is a condition; "
                "combine conditions with and/or/not instead",
            )
            return False
        return t.kind == "num"

    def need_bool(self, node: ast.AST, t: _T, context: str) -> bool:
        if t.kind == "num":
            self.err(
                node,
                f"{context} needs a condition but `{self.text(node)}` is a number; compare it, "
                "e.g. `ema_fast > ema_slow`",
            )
            return False
        return t.kind == "bool"

    def unary(self, node: ast.UnaryOp) -> _T:
        t = self.check(node.operand)
        if isinstance(node.op, ast.Not):
            return _T("bool", t.const, t.lag) if self.need_bool(node.operand, t, "`not`") else _BAD
        if isinstance(node.op, ast.USub | ast.UAdd):
            return _T("num", t.const, t.lag) if self.need_num(node.operand, t, "`-`") else _BAD
        return self.err(node, f"`{self.text(node)}` is not allowed: use `not` to negate conditions")

    def binop(self, node: ast.BinOp) -> _T:
        if not isinstance(node.op, ast.Add | ast.Sub | ast.Mult | ast.Div):
            hint = _BINOP_HINTS.get(type(node.op), "")
            self.check(node.left)
            self.check(node.right)
            return self.err(
                node,
                f"operator in `{self.text(node)}` is not allowed ({hint}). "
                "Arithmetic allows + - * / only",
            )
        lt, rt = self.check(node.left), self.check(node.right)
        ok = self.need_num(node.left, lt, "arithmetic")
        ok = self.need_num(node.right, rt, "arithmetic") and ok
        if not ok:
            return _BAD
        return _T("num", lt.const and rt.const, max(lt.lag, rt.lag))

    def boolop(self, node: ast.BoolOp) -> _T:
        word = "`and`" if isinstance(node.op, ast.And) else "`or`"
        types = [self.check(v) for v in node.values]
        ok = all([self.need_bool(v, t, word) for v, t in zip(node.values, types, strict=True)])
        if not ok:
            return _BAD
        return _T("bool", all(t.const for t in types), max(t.lag for t in types))

    def compare(self, node: ast.Compare) -> _T:
        operands = [node.left, *node.comparators]
        types = [self.check(o) for o in operands]
        ok = True
        for op in node.ops:
            if not isinstance(op, ast.Gt | ast.Lt | ast.GtE | ast.LtE):
                sym = _CMP_TEXT.get(type(op), type(op).__name__)
                why = (
                    "exact float equality is fragile; use > < >= <="
                    if isinstance(op, ast.Eq | ast.NotEq)
                    else "use > < >= <="
                )
                self.err(node, f"comparison `{sym}` is not allowed: {why}")
                ok = False
        for o, t in zip(operands, types, strict=True):
            ok = self.need_num(o, t, "comparison") and ok
        if not ok:
            return _BAD
        if all(t.const for t in types):
            return self.err(
                node,
                f"`{self.text(node)}` compares constants only, so it never changes; compare a "
                "feature or price input, e.g. `rsi < 30`",
            )
        return _T("bool", False, max(t.lag for t in types))

    def call(self, node: ast.Call) -> _T:
        func = node.func
        if not isinstance(func, ast.Name) or func.id not in FUNCTIONS:
            label = func.id if isinstance(func, ast.Name) else self.text(func)
            for a in node.args:
                if not isinstance(a, ast.Starred):
                    self.check(a)
            hint = _did_you_mean(label, FUNCTIONS) if isinstance(func, ast.Name) else ""
            return self.err(
                node,
                f"`{label}` is not an allowed function. Allowed functions: "
                f"crosses_above(a, b), crosses_below(a, b), prev(x, n).{hint}",
            )
        fname = func.id
        bad = False
        if node.keywords:
            kw = ", ".join(f"{k.arg}=" if k.arg else "**" for k in node.keywords)
            self.err(
                node,
                f"`{fname}` takes positional arguments only (got {kw}); "
                f"write `{_example_call(fname)}`",
            )
            bad = True
        if any(isinstance(a, ast.Starred) for a in node.args):
            self.err(node, f"`*` unpacking is not allowed; write `{_example_call(fname)}`")
            return _BAD
        if len(node.args) != 2:
            for a in node.args:
                self.check(a)
            return self.err(
                node,
                f"`{fname}` takes exactly 2 arguments, got {len(node.args)}; "
                f"write `{_example_call(fname)}`",
            )
        if fname == "prev":
            return self.prev_call(node, bad)
        self.uses_cross = True
        ta, tb = self.check(node.args[0]), self.check(node.args[1])
        ok = self.need_num(node.args[0], ta, f"`{fname}`")
        ok = self.need_num(node.args[1], tb, f"`{fname}`") and ok
        if not ok or bad:
            return _BAD
        if ta.const and tb.const:
            return self.err(
                node, f"`{fname}` of two constants never fires; pass a feature or price input"
            )
        return _T("bool", False, max(ta.lag, tb.lag) + 1)

    def prev_call(self, node: ast.Call, bad: bool) -> _T:
        x, n = node.args
        tx = self.check(x)
        if (
            not isinstance(n, ast.Constant)
            or isinstance(n.value, bool)
            or not isinstance(n.value, int)
            or not 1 <= n.value <= MAX_PREV_BARS
        ):
            return self.err(
                n,
                f"`prev` needs a whole-number literal bar count from 1 to {MAX_PREV_BARS} as "
                f"its second argument, got `{self.text(n)}`; e.g. `prev(close, 1)`",
            )
        if bad or tx.kind == "bad":
            return _BAD
        if tx.const:
            return self.err(node, "`prev` of a constant is the same constant; drop the `prev`")
        self.max_prev = max(self.max_prev, n.value)
        return _T(tx.kind, False, tx.lag + n.value)


def _example_call(fname: str) -> str:
    return {
        "crosses_above": "crosses_above(ema_fast, ema_slow)",
        "crosses_below": "crosses_below(rsi, 30)",
        "prev": "prev(close, 1)",
    }[fname]


def check_expression(
    source: object, scope: ExprScope, path: str
) -> tuple[CheckedExpr | None, list[PlanError]]:
    """Parse and whitelist-check ``source``; collect every error found (paths = ``path``)."""
    if not isinstance(source, str):
        got = type(source).__name__
        return None, [
            PlanError(path, f"must be a string expression, got {got}; e.g. `ema_fast > ema_slow`")
        ]
    text = source.strip()
    if not text:
        return None, [
            PlanError(path, "expression is empty; write a condition, e.g. `ema_fast > ema_slow`")
        ]
    if len(source) > MAX_EXPR_CHARS:
        return None, [
            PlanError(
                path,
                f"expression is {len(source)} characters; the limit is {MAX_EXPR_CHARS}. "
                "Simplify the rule or use fewer conditions",
            )
        ]
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        col = f" at column {e.offset}" if e.offset else ""
        return None, [
            PlanError(path, f"syntax error{col}: {e.msg}. {ALLOWED_SUMMARY}"),
        ]
    except (ValueError, RecursionError, MemoryError) as e:
        return None, [PlanError(path, f"cannot parse expression: {e}. {ALLOWED_SUMMARY}")]
    if _depth(tree) > MAX_DEPTH:
        return None, [
            PlanError(path, f"expression is nested too deeply (limit {MAX_DEPTH}); simplify it")
        ]
    checker = _Checker(text, scope, path)
    t = checker.check(tree.body)
    if t.kind == "num" and not checker.errors:
        checker.errors.append(
            PlanError(
                path,
                f"expression must be a condition, e.g. `ema_fast > ema_slow`; `{text[:40]}` is a "
                "number. Compare it with something using > < >= <=",
            )
        )
    if not checker.errors and not checker.refs:
        checker.errors.append(
            PlanError(
                path,
                "expression uses no feature or price input, so it never changes; reference a "
                "feature alias or one of open, high, low, close, volume",
            )
        )
    if checker.errors or t.kind != "bool":
        return None, checker.errors
    return (
        CheckedExpr(
            source=text,
            tree=tree,
            lag=t.lag,
            max_prev=checker.max_prev,
            uses_cross=checker.uses_cross,
            refs=frozenset(checker.refs),
        ),
        [],
    )


# --------------------------------------------------------------------------- evaluation


@dataclass(frozen=True)
class _V:
    values: npt.NDArray[np.float64] | npt.NDArray[np.bool_]
    valid: BoolArray
    is_bool: bool


def _num(v: _V) -> FloatArray:
    return np.asarray(v.values, dtype=np.float64)


def _flag(v: _V) -> BoolArray:
    return np.asarray(v.values, dtype=np.bool_)


class _Evaluator:
    def __init__(self, columns: Mapping[str, npt.ArrayLike], n: int) -> None:
        self.columns = columns
        self.n = n

    def load(self, key: str) -> _V:
        arr = np.asarray(self.columns[key], dtype=np.float64)
        if arr.shape != (self.n,):
            raise ValueError(f"column {key!r} has shape {arr.shape}, expected ({self.n},)")
        return _V(arr, np.isfinite(arr), False)

    def ev(self, node: ast.AST) -> _V:
        n = self.n
        if isinstance(node, ast.Constant):
            return _V(np.full(n, float(node.value)), np.ones(n, dtype=np.bool_), False)
        if isinstance(node, ast.Name):
            return self.load(node.id)
        if isinstance(node, ast.Attribute):
            assert isinstance(node.value, ast.Name)
            return self.load(f"{node.value.id}.{node.attr}")
        if isinstance(node, ast.UnaryOp):
            v = self.ev(node.operand)
            if isinstance(node.op, ast.Not):
                return _V(~_flag(v) & v.valid, v.valid, True)
            sign = -1.0 if isinstance(node.op, ast.USub) else 1.0
            return _V(sign * _num(v), v.valid, False)
        if isinstance(node, ast.BinOp):
            lv, rv = self.ev(node.left), self.ev(node.right)
            a, b, va, vb = _num(lv), _num(rv), lv.valid, rv.valid
            with np.errstate(all="ignore"):
                if isinstance(node.op, ast.Add):
                    r = a + b
                elif isinstance(node.op, ast.Sub):
                    r = a - b
                elif isinstance(node.op, ast.Mult):
                    r = a * b
                else:
                    r = np.where(b == 0.0, np.nan, a / np.where(b == 0.0, 1.0, b))
            return _V(r, va & vb & np.isfinite(r), False)
        if isinstance(node, ast.BoolOp):
            parts = [self.ev(x) for x in node.values]
            valid = np.logical_and.reduce([p.valid for p in parts])
            flags = [_flag(p) for p in parts]
            if isinstance(node.op, ast.And):
                out = np.logical_and.reduce(flags)
            else:
                out = np.logical_or.reduce(flags)
            return _V(out & valid, valid, True)
        if isinstance(node, ast.Compare):
            operands = [self.ev(x) for x in (node.left, *node.comparators)]
            valid = np.logical_and.reduce([o.valid for o in operands])
            out = np.ones(n, dtype=np.bool_)
            with np.errstate(invalid="ignore"):
                for op, lhs, rhs in zip(node.ops, operands, operands[1:], strict=False):
                    a, b = _num(lhs), _num(rhs)
                    if isinstance(op, ast.Gt):
                        out &= a > b
                    elif isinstance(op, ast.Lt):
                        out &= a < b
                    elif isinstance(op, ast.GtE):
                        out &= a >= b
                    else:
                        out &= a <= b
            return _V(out & valid, valid, True)
        if isinstance(node, ast.Call):
            assert isinstance(node.func, ast.Name)
            if node.func.id == "prev":
                v = self.ev(node.args[0])
                k = node.args[1]
                assert isinstance(k, ast.Constant) and isinstance(k.value, int)
                shift = k.value
                valid = _shift_bool(v.valid, shift)
                if v.is_bool:
                    return _V(_shift_bool(_flag(v), shift) & valid, valid, True)
                return _V(_shift_float(_num(v), shift), valid, False)
            a, b = self.ev(node.args[0]), self.ev(node.args[1])
            up = node.func.id == "crosses_above"
            hit = _cross(_num(a), _num(b), a.valid, b.valid, up=up)
            valid = a.valid & b.valid & _shift_bool(a.valid & b.valid, 1)
            return _V(hit, valid, True)
        raise ValueError(f"unchecked node {type(node).__name__}; run check_expression first")


def evaluate_expression(
    expr: CheckedExpr, columns: Mapping[str, npt.ArrayLike], n: int
) -> BoolArray:
    """Evaluate a checked condition over ``n`` bars. ``columns`` maps every name in
    ``expr.refs`` to a length-``n`` numeric array. Invalid (NaN-touched) bars are False."""
    missing = sorted(expr.refs - set(columns))
    if missing:
        raise ValueError(f"missing columns for expression {expr.source!r}: {missing}")
    v = _Evaluator(columns, n).ev(expr.tree.body)
    if not v.is_bool:
        raise ValueError(f"expression {expr.source!r} is not a condition")
    return _flag(v) & v.valid
