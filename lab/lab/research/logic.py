"""The *logic* of a plan, independent of cosmetics and tuned numbers (PLAN.md Phase 8).

``plan_hash`` changes with anything: a renamed plan, an edited description, a re-tuned period.
That is right for the trial ledger (each configuration is a trial), but wrong for the
questions "is this the same hypothesis?" (one locked-holdout check per hypothesis, ever) and
"which walk-forward / sweep results belong to this idea?". :func:`logic_hash` answers those.

It is the sha256 of canonical JSON of:

* ``symbol`` (NOT the interval: the same rule on another bar size is the same hypothesis, so it
  shares the one holdout check and its walk-forward / sweep history);
* the features as registry feature names, with aliases renamed canonically (``f0``, ``f1``,
  ...) in order of first appearance in ``long_entry`` then ``long_exit`` (a source-order walk
  of the parsed expression), then the unreferenced features by feature name;
* ``long_entry`` / ``long_exit`` as parsed ASTs (``lab.plan``'s whitelisted parser) with the
  aliases renamed and every numeric literal replaced by a placeholder, so whitespace, alias
  names, thresholds (``rsi < 30`` vs ``rsi < 25``) and ``prev`` lags are irrelevant: a literal
  in a rule is a tuned number just like a feature param, and re-tuning it is not a new idea;
* which risk keys are set (``stop_loss_pct`` / ``take_profit_pct``), not their values.

Excluded: name, description, interval, feature param values, risk values. Changing an operator,
a feature, a price input, or the expression structure gives a different hash.

Because literals are stripped, the hash cannot tell ``rsi < 20`` from ``rsi < 45``.
:func:`literal_vector` returns them (signed, in canonical order) so the walk-forward lineage can
refuse a hypothesis whose thresholds were hand-tuned across plans.

The hash is *syntactic*: a no-op clause (``or ema_fast < 0``) or an extra risk key gives a new
hash for the same behaviour. :mod:`lab.research.family` closes that gap with a behavioural key
(design-window return correlation); use it for every "same hypothesis?" decision.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from collections.abc import Iterator, Mapping
from typing import Any

from lab.plan import checked_expressions, parse_plan
from lab.types import Plan

__all__ = [
    "LOGIC_VERSION",
    "alias_map",
    "canonical_param_key",
    "canonical_space",
    "feature_multiset",
    "literal_vector",
    "logic_hash",
    "logic_payload",
    "param_match_tokens",
]

# Bump if the canonical form changes (old and new hashes never compare). v2 dropped the interval.
LOGIC_VERSION = 2
_NUMBER = "<num>"
_RISK_KEYS: tuple[str, ...] = ("stop_loss_pct", "take_profit_pct")


def _as_plan(plan: Plan | Mapping[str, Any] | str) -> Plan:
    return plan if isinstance(plan, Plan) else parse_plan(plan)


def _source_order(node: ast.AST) -> Iterator[ast.AST]:
    """Pre-order walk following each node's field order (left before right, func before
    args), i.e. the order names appear in the source text."""
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _source_order(child)


def _trees(plan: Plan) -> tuple[ast.Expression, ast.Expression]:
    entry, exit_ = checked_expressions(plan)
    return entry.tree, exit_.tree


def alias_map(plan: Plan | Mapping[str, Any] | str) -> dict[str, str]:
    """``alias -> canonical id`` (``f0``, ``f1``, ...) for every feature of ``plan``."""
    p = _as_plan(plan)
    aliases = {f.alias: f.feature for f in p.features}
    order: list[str] = []
    for tree in _trees(p):
        for node in _source_order(tree):
            if isinstance(node, ast.Name) and node.id in aliases and node.id not in order:
                order.append(node.id)
    rest = sorted((a for a in aliases if a not in order), key=lambda a: (aliases[a], a))
    return {alias: f"f{i}" for i, alias in enumerate([*order, *rest])}


class _Canonicalise(ast.NodeTransformer):
    def __init__(self, names: Mapping[str, str]) -> None:
        self.names = names

    def visit_Name(self, node: ast.Name) -> ast.AST:
        return ast.Name(id=self.names.get(node.id, node.id), ctx=ast.Load())

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, int | float) and not isinstance(node.value, bool):
            return ast.Constant(value=_NUMBER)
        return node


def _dump(tree: ast.Expression, names: Mapping[str, str]) -> str:
    fresh = copy.deepcopy(tree)  # the checked tree stays intact
    return ast.dump(_Canonicalise(names).visit(fresh), annotate_fields=True)


def logic_payload(plan: Plan | Mapping[str, Any] | str) -> dict[str, Any]:
    """The canonical structure :func:`logic_hash` hashes (see the module docstring)."""
    p = _as_plan(plan)
    names = alias_map(p)
    entry, exit_ = _trees(p)
    features = sorted(
        ({"id": names[f.alias], "feature": f.feature} for f in p.features),
        key=lambda f: int(f["id"][1:]),
    )
    return {
        "v": LOGIC_VERSION,
        "symbol": p.symbol,
        "features": features,
        "long_entry": _dump(entry, names),
        "long_exit": _dump(exit_, names),
        "risk_keys": [k for k in _RISK_KEYS if getattr(p.risk, k) is not None],
    }


def logic_hash(plan: Plan | Mapping[str, Any] | str) -> str:
    """sha256 hex of :func:`logic_payload` as sorted-key compact JSON. Raises
    :class:`~lab.plan.validator.PlanValidationError` for an invalid plan."""
    text = json.dumps(logic_payload(plan), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_param_key(plan: Plan | Mapping[str, Any] | str, key: str) -> str:
    """A tunable key (``"alias.param"`` or ``"risk.x"``) with the alias replaced by its
    canonical id, so search spaces of alias-renamed plans compare. ``ValueError`` for a key
    whose alias is not a feature of ``plan``."""
    head, sep, tail = key.partition(".")
    if head == "risk" and sep:
        return key
    names = alias_map(plan)
    if not sep or head not in names:
        raise ValueError(f"{key!r} is not an 'alias.param' key of this plan")
    return f"{names[head]}.{tail}"


def canonical_space(
    plan: Plan | Mapping[str, Any] | str, space: Mapping[str, Any]
) -> dict[str, list[float]]:
    """``{canonical key: [low, high]}`` for a search space given as ``{key: (low, high)}`` or
    ``{key: [low, high]}``."""
    p = _as_plan(plan)
    out: dict[str, list[float]] = {}
    for key, rng in space.items():
        lo, hi = rng
        out[canonical_param_key(p, key)] = [float(lo), float(hi)]
    return out


def param_match_tokens(plan: Plan | Mapping[str, Any] | str, key: str) -> frozenset[str]:
    """Tokens under which the tunable ``key`` of ``plan`` is compared with another plan's
    tunables (walk-forward lineage across a behaviour family).

    Two tokens per feature param, so a value tried by one plan constrains the other plan's
    param when EITHER matches: ``c:<canonical key>:<feature>`` (:func:`canonical_param_key`,
    same feature at the same canonical position) and ``o:<feature>#<k>.<param>`` (the k-th
    feature of that registry name in canonical order), which still lines up when a no-op
    clause shifts the canonical ids. Risk keys match by name (``r:risk.x``). Matching two
    tokens can only add constraints (a wider required range), never drop one.
    ``ValueError`` for a key that is not an ``alias.param`` / ``risk.x`` key of ``plan``.
    """
    p = _as_plan(plan)
    head, sep, tail = key.partition(".")
    if head == "risk" and sep:
        return frozenset({f"r:{key}"})
    names = alias_map(p)
    if not sep or head not in names:
        raise ValueError(f"{key!r} is not an 'alias.param' key of this plan")
    feature = {f.alias: f.feature for f in p.features}
    cid = names[head]
    ordinal = sum(
        1
        for alias, other in names.items()
        if feature[alias] == feature[head] and int(other[1:]) < int(cid[1:])
    )
    return frozenset({f"c:{cid}.{tail}:{feature[head]}", f"o:{feature[head]}#{ordinal}.{tail}"})


def _literals(tree: ast.AST) -> list[float]:
    """Numeric literals of ``tree`` in source order; a unary minus on a literal is folded into
    it (``-1`` is ``-1.0``, not ``1.0``)."""
    out: list[float] = []

    def visit(node: ast.AST) -> None:
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub | ast.UAdd)
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, int | float)
            and not isinstance(node.operand.value, bool)
        ):
            value = float(node.operand.value)
            out.append(-value if isinstance(node.op, ast.USub) else value)
            return
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
        ):
            out.append(float(node.value))
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return out


def literal_vector(plan: Plan | Mapping[str, Any] | str) -> tuple[float, ...]:
    """Every numeric literal of ``long_entry`` then ``long_exit`` in source order (the
    positions :func:`logic_hash` replaces with a placeholder, so two plans of one logic have
    vectors of the same length): thresholds and ``prev`` lags, i.e. the rule numbers a
    walk-forward cannot search."""
    p = _as_plan(plan)
    entry, exit_ = _trees(p)
    return (*_literals(entry), *_literals(exit_))


def feature_multiset(plan: Plan | Mapping[str, Any] | str) -> tuple[str, ...]:
    """The plan's registry feature names, sorted, with repeats (``("ema", "ema")``). Two plans
    with the same multiset can trade alike after a no-op clause; used to fail closed when their
    returns cannot be compared."""
    return tuple(sorted(f.feature for f in _as_plan(plan).features))
