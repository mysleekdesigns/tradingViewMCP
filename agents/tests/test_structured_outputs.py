"""Structured Designer/Critic outputs (PLAN.md Phase 13): draft-07 role schemas, the local
second check, invalid Designer outputs counted toward the proposal budget, the ledger hash chain
handed to the journal, and the ``--dry-run`` end to end."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import research_loop

from lab.config import LabConfig, load_config
from lab.ledger import Ledger
from lab.plan.validator import parse_plan
from research.orchestrator import (
    CRITIC_SCHEMA,
    DESIGNER_SCHEMA,
    DRAFT_07,
    DRAFT_07_KEYWORDS,
    ROLE_SCHEMAS,
    SCOUT_SCHEMA,
    Collaborators,
    RoleResult,
    schema_keywords,
    to_draft07,
    validate_output,
)
from research.sdk_runner import ScriptedAgentRunner
from research.state import RunStateStore
from tests.test_orchestrator import (
    World,
    critic,
    journal_of,
    make,
    research,
    scout_output,
    valid_plan,
)

PORTFOLIO_PLAN: dict[str, Any] = {
    "kind": "portfolio",
    "name": "xs_momentum_28d_top_quintile",
    "description": "long-only top quintile on 28-day return",
    "interval": "1d",
    "score": {"trailing_return": {"lookback": 28}},
    "rebalance": {"every": 7, "offset": 0},
    "selection": {"quantile": 0.2, "exit_quantile": 0.3, "min_names": 3},
    "weighting": {"equal": {}},
    "max_weight": 0.25,
    "dispersion_gate": None,
}


@pytest.fixture
def cfg() -> LabConfig:
    return load_config()


def _designer_out(plan: dict[str, Any], h: str = "h") -> dict[str, Any]:
    return {"plan_hash": h, "plan": plan, "trial_ids": [1], "notes": "n"}


def _nested_schema_keys(schema: Any, top: bool = True) -> int:
    if isinstance(schema, dict):
        own = 0 if top else int("$schema" in schema)
        return own + sum(_nested_schema_keys(v, False) for v in schema.values())
    if isinstance(schema, list):
        return sum(_nested_schema_keys(v, False) for v in schema)
    return 0


# --------------------------------------------------------------------------- schemas


@pytest.mark.parametrize("role", ["scout", "designer", "critic"])
def test_role_schemas_are_draft_07_with_no_nested_schema_key(role: str) -> None:
    schema = ROLE_SCHEMAS[role]  # type: ignore[index]
    assert schema["$schema"] == DRAFT_07
    assert _nested_schema_keys(schema) == 0
    assert schema_keywords(schema) <= DRAFT_07_KEYWORDS
    json.dumps(schema)  # sent to the SDK as-is


def test_designer_plan_is_the_lab_plan_schema_portfolio_or_empty() -> None:
    from lab.plan.portfolio import portfolio_plan_json_schema
    from lab.plan.schema import plan_json_schema

    branches = DESIGNER_SCHEMA["properties"]["plan"]["anyOf"]
    assert branches[0] == to_draft07(plan_json_schema(), top=False)
    assert branches[1] == to_draft07(portfolio_plan_json_schema(), top=False)
    assert branches[2]["maxProperties"] == 0
    # Everything but $schema is the lab's schema unchanged (it already used draft-07 keywords).
    lab = {k: v for k, v in plan_json_schema().items() if k != "$schema"}
    assert branches[0] == lab
    assert DESIGNER_SCHEMA["required"] == ["plan_hash", "plan", "trial_ids", "notes"]


def test_valid_scripted_plans_validate_against_schema_and_lab() -> None:
    for plan in (valid_plan("x"), research_loop.dry_plan(2)):
        assert validate_output(_designer_out(plan), DESIGNER_SCHEMA) == []
        parse_plan(plan)  # and the lab's own validator accepts it
    assert validate_output(_designer_out(PORTFOLIO_PLAN), DESIGNER_SCHEMA) == []
    not_expressible = {"plan_hash": "", "plan": {}, "trial_ids": [], "notes": "needs L2 data"}
    assert validate_output(not_expressible, DESIGNER_SCHEMA) == []


@pytest.mark.parametrize(
    "plan",
    [
        {"name": "x"},  # not a plan, not empty
        {**valid_plan("x"), "short_entry": "close < open"},  # unknown key
        {**valid_plan("x"), "features": {"f": {"not_a_feature": {}}}},
        {**valid_plan("x"), "features": {"f": {"ema": {"period": "20"}}}},
        {**valid_plan("x"), "interval": "7m"},
        {**valid_plan("x"), "risk": {"position_size_pct": 0}},  # exclusiveMinimum
        {**PORTFOLIO_PLAN, "kind": "portfolios"},  # const
    ],
)
def test_invalid_plans_fail_the_designer_schema(plan: dict[str, Any]) -> None:
    errors = validate_output(_designer_out(plan), DESIGNER_SCHEMA)
    assert errors and errors[0].startswith("$.plan: matches none of the 3 allowed schemas")


def test_designer_schema_types() -> None:
    out = _designer_out(valid_plan("x"))
    assert validate_output({**out, "trial_ids": [True]}, DESIGNER_SCHEMA)
    assert validate_output({**out, "trial_ids": [1.0]}, DESIGNER_SCHEMA) == []
    assert validate_output([], DESIGNER_SCHEMA) == ["$: expected object, got list"]


def test_critic_requires_cited_evidence() -> None:
    good = critic("abandon", "fees eat it", tags=["cost_bound"])
    assert validate_output(good, CRITIC_SCHEMA) == []
    no_evidence = {k: v for k, v in good.items() if k != "evidence"}
    assert "$: missing required key 'evidence'" in validate_output(no_evidence, CRITIC_SCHEMA)
    assert validate_output({**good, "evidence": []}, CRITIC_SCHEMA)  # at least one pointer
    assert validate_output({**good, "evidence": [{"field": "dsr"}]}, CRITIC_SCHEMA)
    bad_source = {**good, "evidence": [{"field": "dsr", "value": "0.4", "source": "web"}]}
    assert validate_output(bad_source, CRITIC_SCHEMA)
    assert validate_output({**good, "failure_tags": ["vibes"]}, CRITIC_SCHEMA)
    assert validate_output({**good, "failure_tags": ["other", "other"]}, CRITIC_SCHEMA)
    assert validate_output({**good, "reasons": []}, CRITIC_SCHEMA)
    lean = {"verdict": "promising", "reasons": ["r"], "evidence": [{"field": "f", "value": "v"}]}
    assert validate_output(lean, CRITIC_SCHEMA) == []  # lessons / tags / concern optional
    assert validate_output({"hypotheses": []}, SCOUT_SCHEMA) == []


# --------------------------------------------------------------------------- local checker


def test_validate_output_combinators_and_bounds() -> None:
    schema = {
        "anyOf": [
            {"type": "object", "required": ["a"], "additionalProperties": False,
             "properties": {"a": {"type": "integer", "minimum": 1}}},
            {"type": "object", "maxProperties": 0},
        ]
    }  # fmt: skip
    assert validate_output({"a": 1}, schema) == []
    assert validate_output({}, schema) == []
    assert validate_output({"a": 0}, schema)
    assert validate_output({"a": 1, "b": 2}, schema)
    one = {"oneOf": [{"type": "number"}, {"type": "integer"}]}
    assert validate_output(1.5, one) == []
    assert "matches 2 of the oneOf" in validate_output(2, one)[0]
    assert validate_output(None, {"type": ["number", "null"]}) == []
    assert validate_output("a", {"type": ["number", "null"]})
    assert validate_output(0, {"type": "number", "exclusiveMinimum": 0})
    assert validate_output(1, {"type": "number", "exclusiveMaximum": 1})
    assert validate_output("ABC", {"type": "string", "pattern": "^[A-Z]+$"}) == []
    assert validate_output("abc", {"type": "string", "pattern": "^[A-Z]+$"})
    assert validate_output("", {"type": "string", "minLength": 1})
    assert validate_output(True, {"const": 1})  # JSON: true is not 1
    assert validate_output(1, {"not": {"const": 1}})
    names = {"type": "object", "propertyNames": {"pattern": "^[a-z]+$"}}
    assert validate_output({"Bad": 1}, names)
    assert validate_output(
        {"x": "s"}, {"type": "object", "additionalProperties": {"type": "integer"}}
    )


def test_to_draft07_converts_newer_spellings_and_refuses_the_rest() -> None:
    newer = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"n": {"type": "integer"}},
        "type": "object",
        "properties": {
            "a": {"$ref": "#/$defs/n"},
            "t": {"prefixItems": [{"type": "string"}], "items": False},
            "nested": {"$schema": "https://json-schema.org/draft/2020-12/schema"},
        },
        "dependentRequired": {"a": ["t"]},
    }
    out = to_draft07(newer)
    assert out["$schema"] == DRAFT_07
    assert out["definitions"] == {"n": {"type": "integer"}}
    assert out["properties"]["a"] == {"$ref": "#/definitions/n"}
    assert out["properties"]["t"] == {"items": [{"type": "string"}], "additionalItems": False}
    assert out["properties"]["nested"] == {}
    assert out["dependencies"] == {"a": ["t"]}
    assert schema_keywords(out) <= DRAFT_07_KEYWORDS
    with pytest.raises(ValueError, match="unevaluatedProperties"):
        to_draft07({"unevaluatedProperties": False})


def test_default_plan_hash_matches_the_lab_for_both_plan_kinds() -> None:
    from lab.plan import portfolio as pf
    from lab.plan.validator import plan_hash

    h = Collaborators.default().plan_hash
    assert h(valid_plan("x")) == plan_hash(parse_plan(valid_plan("x")))
    checked = pf.validate_portfolio_plan(PORTFOLIO_PLAN, interval="1d")
    assert checked.plan is not None
    assert h(PORTFOLIO_PLAN) == pf.portfolio_plan_hash(checked.plan)
    with pytest.raises(ValueError):
        h({**PORTFOLIO_PLAN, "selection": {"quantile": 5}})


# --------------------------------------------------------------------------- orchestrator


INVALID = RoleResult(
    None, 0.1, "s-bad", "invalid output after one retry: $.plan: ...", invalid_outputs=2
)


def _first_invalid_then(w: World) -> Any:
    calls = {"n": 0}

    def designer(req: Any) -> Any:
        calls["n"] += 1
        return INVALID if calls["n"] == 1 else w.designer(req)

    return designer


async def test_invalid_designer_output_counts_toward_proposals_and_run_continues(
    tmp_path: Path, cfg: LabConfig
) -> None:
    w = World(tmp_path, cfg)
    runner = ScriptedAgentRunner(
        {
            "scout": scout_output("A", "B"),
            "designer": _first_invalid_then(w),
            "critic": critic("abandon"),
        }
    )
    orch, ideas = make(w, runner)
    state = await orch.run(["BTCUSDT"], "4h")

    assert state["outcome"] == "no_edge"
    assert [r.role for r in runner.requests] == [
        "scout", "designer", "designer", "critic",
    ]  # fmt: skip
    # Idea 1 is abandoned (the SDK already retried); idea 2 is designed and reviewed.
    assert ideas.ideas[1]["status"] == "abandoned" and ideas.ideas[1]["plans"] == []
    assert ideas.ideas[2]["status"] == "abandoned" and len(ideas.ideas[2]["plans"]) == 1
    # 1 plan with lab evidence + 2 invalid outputs.
    assert state["used"]["proposals"] == 3
    j = journal_of(state, w)
    assert j.summary.used["invalid_designer_outputs"] == 2
    assert j.summary.used["proposals"] == 3
    assert any("1 distinct plans with lab evidence + 2 invalid" in n for n in j.summary.notes)
    assert any(k == "budget" and "2 invalid output(s)" in t for k, t in j.logs)
    # Kept per idea in the resume sidecar.
    resume = RunStateStore(tmp_path / "runs").read_resume(state["run_id"])
    assert [p.context.get("invalid_outputs", 0) for p in resume.ideas] == [2, 0]


async def test_local_check_rejecting_a_runner_accepted_output_counts_one_more(
    tmp_path: Path, cfg: LabConfig
) -> None:
    w = World(tmp_path, cfg)
    sneaky = RoleResult(_designer_out({"name": "not a plan"}), 0.0, "s", None, invalid_outputs=1)
    runner = ScriptedAgentRunner(
        {"scout": scout_output("A"), "designer": sneaky, "critic": critic("abandon")}
    )
    orch, ideas = make(w, runner)
    state = await orch.run(["BTCUSDT"], "4h")
    assert state["used"]["proposals"] == 2  # the runner's 1 + the local rejection
    assert "critic" not in [r.role for r in runner.requests]
    assert "designer failed: invalid output" in (ideas.statuses[-1][2] or "")


async def test_invalid_outputs_can_exhaust_the_proposal_budget(
    tmp_path: Path, cfg: LabConfig
) -> None:
    w = World(tmp_path, cfg)
    runner = ScriptedAgentRunner(
        {"scout": scout_output("A", "B", "C"), "designer": INVALID, "critic": critic("abandon")}
    )
    orch, _ = make(w, runner, rc=research(max_proposals=4))
    state = await orch.run(["BTCUSDT"], "4h")
    assert [r.role for r in runner.requests].count("designer") == 2  # 2 + 2 = the budget
    assert state["outcome"] == "budget_exhausted"
    assert state["used"]["proposals"] == 4


async def test_invalid_critic_output_is_logged_not_charged_to_proposals(
    tmp_path: Path, cfg: LabConfig
) -> None:
    w = World(tmp_path, cfg)
    no_evidence = {"verdict": "abandon", "reasons": ["meh"]}
    runner = ScriptedAgentRunner(
        {"scout": scout_output("A"), "designer": w.designer, "critic": no_evidence}
    )
    orch, ideas = make(w, runner)
    state = await orch.run(["BTCUSDT"], "4h")
    assert state["used"]["proposals"] == 1  # only the designed plan
    assert ideas.ideas[1]["status"] == "abandoned"
    assert ideas.tags == []  # no Critic verdict, nothing tagged
    j = journal_of(state, w)
    assert any(k == "critic" and "invalid output(s)" in t for k, t in j.logs)
    assert any("critic unavailable: invalid output" in t for _, t in j.logs)


async def test_resume_restores_the_invalid_output_count(tmp_path: Path, cfg: LabConfig) -> None:
    w = World(tmp_path, cfg)
    base = w.deps()
    crash = {"on": True}

    def evaluate(ledger: Ledger, run_id: str, h: str, cfg: LabConfig) -> Any:
        if crash["on"]:
            raise RuntimeError("network dropped")
        return base.evaluate_plan(ledger, run_id, h, cfg)

    deps = replace(base, evaluate_plan=evaluate)
    runner = ScriptedAgentRunner(
        {
            "scout": scout_output("A", "B"),
            "designer": _first_invalid_then(w),
            "critic": critic("abandon"),
        }
    )
    orch, ideas = make(w, runner, deps=deps)
    with pytest.raises(RuntimeError, match="network dropped"):
        await orch.run(["BTCUSDT"], "4h")
    [state_file] = [p for p in (tmp_path / "runs").glob("r-*.json") if "resume" not in p.name]
    run_id = state_file.stem
    assert RunStateStore(tmp_path / "runs").read(run_id)["used"]["proposals"] == 3

    crash["on"] = False
    runner2 = ScriptedAgentRunner({"designer": w.designer, "critic": critic("abandon")})
    orch2, _ = make(w, runner2, ideas=ideas, deps=deps)
    state = await orch2.resume(run_id)
    assert state["outcome"] == "no_edge"
    # Idea 2 redesigned (same logic, so the ledger counts 2 distinct plans) + 2 invalid.
    assert state["used"]["proposals"] == 4
    assert journal_of(state, w).summary.used["invalid_designer_outputs"] == 2


async def test_ledger_chain_and_run_config_reach_the_run_summary(
    tmp_path: Path, cfg: LabConfig
) -> None:
    w = World(tmp_path, cfg, trials_per_design=2)
    runner = ScriptedAgentRunner(
        {"scout": scout_output("A"), "designer": w.designer, "critic": critic("abandon")}
    )
    orch, _ = make(w, runner)
    state = await orch.run(["BTCUSDT"], "4h")
    s = journal_of(state, w).summary
    assert s.ledger_chain["ok"] is True and s.ledger_chain["n_trials"] == 2
    assert s.ledger_chain == orch.last_ledger_chain
    assert s.run_config["budget"]["max_proposals"] == research().budget.max_proposals
    assert s.ledger_path.endswith("ledger.db")


async def test_broken_or_missing_chain_check_is_a_note_never_a_crash(
    tmp_path: Path, cfg: LabConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path, cfg)

    def tampered(self: Ledger, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "ok": False, "n_trials": 1, "head": None,
                "first_bad_trial_id": 1, "error": "trial 1 content changed"}  # fmt: skip

    monkeypatch.setattr(Ledger, "verify_chain", tampered)
    runner = ScriptedAgentRunner(
        {"scout": scout_output("A"), "designer": w.designer, "critic": critic("abandon")}
    )
    orch, _ = make(w, runner)
    state = await orch.run(["BTCUSDT"], "4h")
    s = journal_of(state, w).summary
    assert s.ledger_chain["ok"] is False
    assert any("verification FAILED: first bad trial 1" in n for n in s.notes)

    def boom(self: Ledger, run_id: str) -> dict[str, Any]:
        raise RuntimeError("db locked")

    monkeypatch.setattr(Ledger, "verify_chain", boom)
    w2 = World(tmp_path / "second", cfg)
    runner2 = ScriptedAgentRunner(
        {"scout": scout_output("B"), "designer": w2.designer, "critic": critic("abandon")}
    )
    orch2, _ = make(w2, runner2)
    state2 = await orch2.run(["BTCUSDT"], "4h")
    s2 = journal_of(state2, w2).summary
    assert state2["outcome"] == "no_edge" and s2.ledger_chain is None
    assert any("could not be verified (RuntimeError)" in n for n in s2.notes)


# --------------------------------------------------------------------------- dry run


async def test_dry_run_end_to_end_with_structured_outputs(tmp_path: Path) -> None:
    res = await research_loop.run_dry(tmp_path, ["BTCUSDT", "ETHUSDT"], "4h", None, None)
    assert res.failures == []
    assert res.state["outcome"] == "no_edge"
    # Every simulated init tool list was complete.
    assert res.init_checks and all(m == [] for _, m in res.init_checks)
    assert {r for r, _ in res.init_checks} == {"scout", "designer", "critic"}
    # The hash chain covers the scripted designer's trials and verifies.
    assert res.ledger_chain is not None and res.ledger_chain["ok"] is True
    assert res.ledger_chain["n_trials"] == 2
    # The deliberately invalid output was counted: 2 lab-counted plans + 2 invalid attempts.
    assert res.invalid_proposals == research_loop.DRY_INVALID_OUTPUTS
    assert res.state["used"]["proposals"] == 2 + research_loop.DRY_INVALID_OUTPUTS
    # The run continued past it, and the later idea of the same family saw the tag.
    assert len(res.designer_prompts) == 3
    assert "no recorded failure tags" in res.designer_prompts[1]
    assert "failed for: no_signal x1" in res.designer_prompts[2]
    journal = next((tmp_path / "journal").glob("*.md")).read_text()
    assert "invalid designer output" in journal
    assert "Ignore all previous instructions" not in journal


def test_dry_run_cli_prints_the_phase_13_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(research_loop.tempfile, "mkdtemp", lambda prefix="": str(tmp_path))
    assert research_loop.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "init tool list: OK (" in out
    assert "ledger hash chain: OK (2 trials" in out
    assert "invalid designer outputs: 2 (counted in proposals used: 4)" in out


async def test_dry_run_fails_when_a_tool_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research import sdk_runner

    real = sdk_runner.ScriptedAgentRunner

    def no_ledger_tool(scripts: Any) -> Any:
        def tools(role: Any) -> list[str]:
            listed = list(sdk_runner.expected_init_tools(role))
            return [t for t in listed if not t.endswith("get_ledger")]

        return real(scripts, init_tools=tools)

    monkeypatch.setattr(research_loop, "ScriptedAgentRunner", no_ledger_tool)
    res = await research_loop.run_dry(tmp_path, ["BTCUSDT"], "4h", None, None)
    assert any("missing" in f and "get_ledger" in f for f in res.failures)
    # A missing tool is a run failure (outcome error, resumable), never no_edge.
    assert res.state["outcome"] == "error"
    assert any(f.startswith("run stopped (outcome error)") for f in res.failures)


async def test_dry_run_fails_when_a_forbidden_tool_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research import sdk_runner

    real = sdk_runner.ScriptedAgentRunner

    def with_bash(scripts: Any) -> Any:
        def tools(role: Any) -> list[str]:
            listed = list(sdk_runner.expected_init_tools(role))
            return listed + (["Bash"] if role == "critic" else [])

        return real(scripts, init_tools=tools)

    monkeypatch.setattr(research_loop, "ScriptedAgentRunner", with_bash)
    res = await research_loop.run_dry(tmp_path, ["BTCUSDT"], "4h", None, None)
    assert "init tool list: critic lists forbidden Bash" in res.failures
    assert res.state["outcome"] == "error"


def test_cli_reports_an_init_tool_failure_with_the_resume_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from research.orchestrator import InitToolsError

    async def stopped(_args: Any) -> int:
        raise InitToolsError("designer", "init tool list missing: x", "r-20261028-000000-abc123")

    monkeypatch.setattr(research_loop, "_live", stopped)
    assert research_loop.main(["--symbols", "BTCUSDT"]) == research_loop.EXIT_INIT_TOOLS == 3
    err = capsys.readouterr().err
    assert "outcome error" in err and "No idea was abandoned" in err and "resumable" in err
    assert "--resume r-20261028-000000-abc123" in err
    assert "Nothing was tested" not in err


def test_cli_critic_init_failure_says_resume_continues_with_the_review(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Critic-side failure comes after the Designer tested a plan: the message must not
    claim nothing was tested."""
    from research.orchestrator import InitToolsError

    async def stopped(_args: Any) -> int:
        raise InitToolsError("critic", "init tool list missing: x", "r-20261028-000000-abc123")

    monkeypatch.setattr(research_loop, "_live", stopped)
    assert research_loop.main(["--symbols", "BTCUSDT"]) == 3
    err = capsys.readouterr().err
    assert "already tested this round's plan" in err and "Critic review" in err
    assert "Nothing was tested" not in err and "No idea had been tested" not in err


def test_cli_help_documents_the_init_tools_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        research_loop.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "exit codes" in out and "3 a role's tool setup was wrong" in out
    assert "3 a role session's tool setup was wrong" in " ".join(
        (research_loop.__doc__ or "").split()
    )


def test_live_run_shares_one_jev_client_with_the_scout_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK runner's Scout PostToolUse screen gets the same JevClient as the orchestrator."""
    import asyncio

    seen: dict[str, Any] = {}

    class FakeRunner:
        def __init__(self, *, models: Any, jev: Any = None) -> None:
            seen["runner_jev"] = jev

    class FakeOrch:
        def __init__(self, cfg: Any, rc: Any, runner: Any, ideas: Any, jev: Any) -> None:
            seen["orch_jev"] = jev

        async def run(self, *a: Any) -> Any:
            raise RuntimeError("stop here")

    class FakeLib:
        def close(self) -> None:
            pass

    monkeypatch.setattr(research_loop, "SdkAgentRunner", FakeRunner)
    monkeypatch.setattr(research_loop, "Orchestrator", FakeOrch)
    monkeypatch.setattr(research_loop, "load_research_config", lambda: research())
    monkeypatch.setattr("research.ideas.IdeaLibrary", FakeLib)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    args = research_loop.build_parser().parse_args(["--symbols", "BTCUSDT"])
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(research_loop._live(args))
    assert seen["runner_jev"] is not None and seen["runner_jev"] is seen["orch_jev"]
