"""strategy-lab MCP tool descriptions follow the audited rubric (PLAN.md Phase 13): a purpose
sentence, then ``Use when:``, ``Not for:``, ``Params:`` (every input-schema parameter named),
``Example:`` (a call of the tool itself), and ``Limits:``, in that order, kept compact.

Read through the in-process MCP client, so this checks exactly what an agent sees."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from lab.mcp_server import TOOL_NAMES
from lab.plan import validate_plan
from tests.test_mcp_server import Lab

EXPECTED_TOOLS = {
    "start_run",
    "list_features",
    "validate_plan",
    "run_backtest",
    "run_sweep",
    "walk_forward",
    "run_adversarial",
    "run_crosscheck",
    "get_ledger",
    "get_report",
    "get_budget",
    "holdout_check",
}
LABELS = ("Use when:", "Not for:", "Params:", "Example:", "Limits:")
# Cap per description. The longest audited description (run_backtest, which must also state
# the derivatives baseline and portfolio benchmark rules) is ~1,000 characters; 1,200 leaves
# a little room for one more fact while keeping every tool far below the "full" variants the
# Phase 13 study found added length without adding task success. Twelve tools x 1,200 chars is
# ~3.6k tokens of tool context at most.
MAX_DESCRIPTION_CHARS = 1200


def _tools(tmp_path: Path) -> list[Any]:
    lab = Lab(tmp_path)

    async def go() -> list[Any]:
        async with Client(lab.server, raise_exceptions=True) as c:
            return list((await c.list_tools()).tools)

    return anyio.run(go)


@pytest.fixture(scope="module")
def tools(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return {t.name: t for t in _tools(tmp_path_factory.mktemp("descriptions"))}


def _sections(text: str) -> dict[str, str]:
    """``{"purpose": ..., "Use when:": ..., ...}``; each label must start a line, once."""
    starts: list[tuple[int, str]] = []
    for label in LABELS:
        hits = [m.start() for m in re.finditer(rf"(?m)^{re.escape(label)}", text)]
        assert len(hits) == 1, f"{label!r} must start exactly one line, found {len(hits)}"
        starts.append((hits[0], label))
    assert [label for _, label in sorted(starts)] == list(LABELS), "labels out of order"
    out = {"purpose": text[: starts[0][0]].strip()}
    bounds = [*(s for s, _ in starts), len(text)]
    for i, (start, label) in enumerate(starts):
        out[label] = text[start + len(label) : bounds[i + 1]].strip()
    return out


def test_tool_set_is_exactly_the_twelve_contract_tools(tools: dict[str, Any]) -> None:
    assert set(tools) == EXPECTED_TOOLS == set(TOOL_NAMES)


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_description_has_every_rubric_component(tools: dict[str, Any], name: str) -> None:
    text = tools[name].description or ""
    assert text == text.strip() and "\n " not in text, "description must be dedented"
    assert len(text) < MAX_DESCRIPTION_CHARS, f"{name}: {len(text)} chars"
    sec = _sections(text)

    purpose = sec["purpose"]
    assert 20 <= len(purpose) <= 400 and purpose.endswith("."), purpose
    assert purpose[0].isupper(), "purpose starts a sentence"

    for label in LABELS:
        assert len(sec[label]) >= 4, f"{name}: empty {label}"

    params = set(tools[name].input_schema.get("properties", {}))
    if not params:
        assert sec["Params:"].lower().startswith("none"), sec["Params:"]
    for param in params:
        # each item reads "name — meaning", at the start of the section or after "; "
        pattern = rf"(?:^|;\s+){re.escape(param)} —\s+\S"
        assert re.search(pattern, sec["Params:"]), f"{name}: Params does not explain {param!r}"

    assert re.match(rf"{re.escape(name)}\(", sec["Example:"]), sec["Example:"]
    for param in re.findall(r"(\w+)=", sec["Example:"].split("(", 1)[1]):
        assert param in params, f"{name}: example passes unknown param {param!r}"


def test_tool_descriptions_name_no_tool_outside_the_contract(tools: dict[str, Any]) -> None:
    """A tool a description points to must exist (``run_id`` and ``run_*`` are not tools)."""
    for tool in tools.values():
        for ref in re.findall(
            r"\b((?:run|get)_[a-z_]+|holdout_check|walk_forward)\b", tool.description
        ):
            if ref == "run_id" or ref.endswith("_"):
                continue
            assert ref in EXPECTED_TOOLS, f"{tool.name} mentions unknown tool {ref!r}"


def test_validate_plan_example_is_a_valid_plan(tools: dict[str, Any]) -> None:
    """The one full plan an agent sees in a description must itself validate."""
    example = _sections(tools["validate_plan"].description)["Example:"]
    plan_text = example[len("validate_plan(plan=") : -1]
    plan = json.loads(plan_text.replace("\n", " "))
    parsed, errors = validate_plan(plan)
    assert errors == [] and parsed is not None


def test_safety_facts_survive_the_audit(tools: dict[str, Any]) -> None:
    """Facts the honesty rules rely on stay in the tool descriptions themselves."""
    d = {name: " ".join(t.description.split()) for name, t in tools.items()}
    assert "pass/fail and checks left only" in d["holdout_check"]
    assert "One check per hypothesis ever" in d["holdout_check"]
    assert "matched run_crosscheck" in d["holdout_check"]
    assert "never re-tune" in d["holdout_check"]
    for name in ("run_backtest", "run_adversarial", "run_sweep", "walk_forward"):
        assert "design window" in d[name], name
        assert "ledger" in d[name] or "trials" in d[name], name
    assert "biased upward" in d["run_sweep"]
    assert "rule literals" in d["walk_forward"] and "already tried" in d["walk_forward"]
    assert "never in long_exit" in d["list_features"]
    assert "information only" in d["run_backtest"] and "information only" in d["get_report"]
    assert "Holdout trials are never listed" in d["get_ledger"]
    assert "free" in d["validate_plan"] and "close > 50000" in d["validate_plan"]
    assert "frozen" in d["get_budget"] and "start_run" in d["get_budget"]
