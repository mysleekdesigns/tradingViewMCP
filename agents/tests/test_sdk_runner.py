"""SdkAgentRunner: per-role option building, hooks, and JSON-output handling. No network: the
SDK's ``query`` is replaced by a fake async generator yielding real SDK message objects."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from research.orchestrator import (
    CRITIC_SCHEMA,
    DESIGNER_SCHEMA,
    INIT_FORBIDDEN_ERROR,
    INIT_MISSING_ERROR,
    SCOUT_SCHEMA,
    RoleRequest,
    is_init_tools_error,
)
from research.sdk_runner import (
    CRITIC_LAB_TOOLS,
    DESIGNER_LAB_TOOLS,
    HOLDOUT_TOOL,
    LAB_TOOLS,
    MCP_PREFIX,
    START_RUN_TOOL,
    SUBAGENT_ENV,
    ScriptedAgentRunner,
    SdkAgentRunner,
    deny_unsafe_tools,
    expected_init_tools,
    extract_output,
    forbidden_init_tools,
    lab_server_tool_names,
    missing_init_tools,
    parse_last_json_object,
)

RUN_ID = "r-20261024-120000-abc123"
REPO = Path("/repo")
EVIDENCE = [{"field": "rows.wf_oos_sharpe.value", "value": "0.1", "source": "get_report"}]


def _critic(verdict: str = "abandon") -> dict[str, Any]:
    """A Critic output that satisfies CRITIC_SCHEMA."""
    return {"verdict": verdict, "reasons": ["x"], "evidence": EVIDENCE, "lessons": []}


def _critic_json(verdict: str = "abandon") -> str:
    return json.dumps(_critic(verdict))


# A small schema for runner mechanics that do not depend on the Designer's plan schema.
SIMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["plan_hash", "notes"],
    "properties": {"plan_hash": {"type": "string"}, "notes": {"type": "string"}},
}


def _req(role: str, schema: dict[str, Any], cap: float | None = 2.0) -> RoleRequest:
    return RoleRequest(
        role=role,
        prompt="go",
        run_id=RUN_ID,
        output_schema=schema,  # type: ignore[arg-type]
        max_budget_usd=cap,
    )


def _runner(**kw: Any) -> SdkAgentRunner:
    return SdkAgentRunner(repo_root=REPO, **kw)


def _result(
    text: str | None,
    *,
    cost: float = 0.1,
    sid: str = "s1",
    subtype: str = "success",
    structured: Any = None,
    is_error: bool = False,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id=sid,
        total_cost_usd=cost,
        result=text,
        structured_output=structured,
    )


class FakeQuery:
    """Yields a scripted list of messages per call; records (prompt, options)."""

    def __init__(self, *scripts: list[Any]) -> None:
        self.scripts = list(scripts)
        self.calls: list[tuple[str, ClaudeAgentOptions]] = []
        self.closed: list[bool] = []  # one entry per stream closed before its end

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.calls.append((prompt, options))
        script = self.scripts.pop(0)
        closed = self.closed

        async def gen() -> AsyncIterator[Any]:
            finished = False
            try:
                for m in script:
                    if isinstance(m, Exception):
                        raise m
                    yield m
                finished = True
            finally:
                if not finished:
                    closed.append(True)

        return gen()


# --------------------------------------------------------------------------- options per role


def test_scout_options_web_only_no_mcp() -> None:
    o = _runner().build_options(_req("scout", SCOUT_SCHEMA))
    assert o.tools == ["WebSearch", "WebFetch"]
    assert o.allowed_tools == ["WebSearch", "WebFetch", "StructuredOutput"]
    assert o.mcp_servers == {}
    assert o.strict_mcp_config is True
    for t in ("Bash", "Edit", "Write", "NotebookEdit", HOLDOUT_TOOL, START_RUN_TOOL):
        assert t in o.disallowed_tools
    assert o.model == "sonnet"


def test_designer_options_pin_run_and_disable_holdout() -> None:
    o = _runner().build_options(_req("designer", DESIGNER_SCHEMA))
    assert o.tools == []  # no built-in tools at all
    assert set(o.allowed_tools) == {*DESIGNER_LAB_TOOLS, "StructuredOutput"}
    assert HOLDOUT_TOOL not in o.allowed_tools and START_RUN_TOOL not in o.allowed_tools
    assert "mcp__strategy-lab__run_sweep" in o.allowed_tools
    for t in (
        HOLDOUT_TOOL,
        START_RUN_TOOL,
        "Bash",
        "Edit",
        "Write",
        "NotebookEdit",
        "WebSearch",
        "WebFetch",
    ):
        assert t in o.disallowed_tools
    server = o.mcp_servers["strategy-lab"]  # type: ignore[index]
    assert server["type"] == "stdio" and server["command"] == "uv"
    assert server["args"] == ["--directory", "/repo/lab", "run", "python", "-m", "lab.mcp_server"]
    assert server["env"] == {"STRATEGY_LAB_RUN_ID": RUN_ID, "STRATEGY_LAB_DISABLE_HOLDOUT": "1"}
    assert o.permission_mode == "dontAsk"
    assert o.max_budget_usd == 2.0
    assert o.cwd == "/repo"
    assert o.setting_sources == ["project"]  # project hooks also apply
    assert o.output_format == {"type": "json_schema", "schema": DESIGNER_SCHEMA}
    assert o.model == "opus"


def test_critic_options_read_only_tools() -> None:
    o = _runner().build_options(_req("critic", CRITIC_SCHEMA))
    assert set(o.allowed_tools) == {*CRITIC_LAB_TOOLS, "StructuredOutput"}
    assert set(CRITIC_LAB_TOOLS) == {
        "mcp__strategy-lab__get_report",
        "mcp__strategy-lab__get_ledger",
        "mcp__strategy-lab__get_budget",
    }
    for t in ("mcp__strategy-lab__run_backtest", "mcp__strategy-lab__run_sweep", HOLDOUT_TOOL):
        assert t in o.disallowed_tools
    assert o.mcp_servers["strategy-lab"]["env"]["STRATEGY_LAB_DISABLE_HOLDOUT"] == "1"  # type: ignore[index]
    assert o.resume is None  # always a fresh session
    assert "sceptical" in str(o.system_prompt)


@pytest.mark.parametrize(
    "role,schema",
    [("scout", SCOUT_SCHEMA), ("designer", DESIGNER_SCHEMA), ("critic", CRITIC_SCHEMA)],
)
async def test_every_role_has_the_unsafe_tool_hook_and_an_allowlist(role: str, schema: Any) -> None:
    o = _runner().build_options(_req(role, schema))
    [matcher] = o.hooks["PreToolUse"]  # type: ignore[index]
    assert matcher.matcher is None  # applies to every tool
    assert deny_unsafe_tools in matcher.hooks
    allowlist = matcher.hooks[1]
    allowed = o.allowed_tools[0]
    assert await allowlist({"hook_event_name": "PreToolUse", "tool_name": allowed}, "t", None) == {}
    denied = await allowlist({"hook_event_name": "PreToolUse", "tool_name": "Read"}, "t", None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_project_settings_can_be_turned_off_and_models_overridden() -> None:
    from research.sdk_runner import RoleModels

    o = _runner(load_project_settings=False, models=RoleModels(designer="sonnet")).build_options(
        _req("designer", DESIGNER_SCHEMA)
    )
    assert o.setting_sources == []
    assert o.model == "sonnet"


def test_agent_definition_uses_camel_case_fields() -> None:
    d = _runner().agent_definition("critic")
    assert isinstance(d, AgentDefinition)
    assert d.disallowedTools is not None and HOLDOUT_TOOL in d.disallowedTools
    assert d.maxTurns == 15
    assert d.tools == list(CRITIC_LAB_TOOLS)


def test_role_prompts_exist_and_state_untrusted_rule() -> None:
    from research.sdk_runner import load_role_prompt

    assert "untrusted" in load_role_prompt("scout").lower()
    assert "holdout_check" in load_role_prompt("designer")
    assert "no edge after" in load_role_prompt("critic").lower()


# --------------------------------------------------------------------------- JSON parsing


def test_parse_last_json_prefers_last_fenced_block() -> None:
    text = 'first ```json\n{"a": 1}\n``` then ```json\n{"a": 2}\n``` done'
    assert parse_last_json_object(text) == {"a": 2}


def test_parse_last_bare_object_skips_nested_and_garbage() -> None:
    text = 'Thoughts {not json}. {"x": {"y": 1}} and finally {"verdict": "abandon", "n": [1]} bye'
    assert parse_last_json_object(text) == {"verdict": "abandon", "n": [1]}
    assert parse_last_json_object("no json here") is None
    assert parse_last_json_object(None) is None


def test_extract_output_prefers_structured_and_validates() -> None:
    good = _critic("refine")
    assert extract_output(good, "junk", CRITIC_SCHEMA) == (good, [])
    out, errs = extract_output(None, '{"verdict": "maybe"}', CRITIC_SCHEMA)
    assert out is None and any("verdict" in e or "reasons" in e for e in errs)


# --------------------------------------------------------------------------- run + retry


async def test_run_returns_parsed_output_and_cost() -> None:
    q = FakeQuery(
        [
            AssistantMessage(content=[TextBlock(text="working")], model="m"),
            _result(
                "Done.\n```json\n" + _critic_json() + "\n```",
                cost=0.25,
                sid="abc",
            ),
        ]
    )
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA))
    assert res.output == _critic()
    assert res.cost_usd == 0.25 and res.session_id == "abc" and res.error is None
    assert len(q.calls) == 1


async def test_bad_output_gets_one_retry_in_same_session_and_costs_add_up() -> None:
    q = FakeQuery(
        [_result("I think it is fine.", cost=0.3, sid="s1")],
        [_result(_critic_json("promising"), cost=0.1, sid="s1")],
    )
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA, cap=2.0))
    assert res.output is not None and res.output["verdict"] == "promising"
    assert res.cost_usd == pytest.approx(0.4)
    retry_prompt, retry_opts = q.calls[1]
    assert retry_opts.resume == "s1"
    assert retry_opts.max_budget_usd == pytest.approx(1.7)  # cap covers both attempts
    assert "ONLY that JSON object" in retry_prompt


async def test_bad_output_twice_is_an_error() -> None:
    q = FakeQuery([_result("nope", cost=0.2)], [_result("still nope", cost=0.2)])
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA))
    assert res.output is None
    assert res.error is not None and "after one retry" in res.error
    assert res.cost_usd == pytest.approx(0.4)
    assert len(q.calls) == 2


async def test_budget_stop_is_not_retried() -> None:
    q = FakeQuery([_result("partial", cost=1.9, subtype="error_max_budget_usd", is_error=True)])
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA))
    assert res.output is None and "error_max_budget_usd" in (res.error or "")
    assert res.cost_usd == pytest.approx(1.9)
    assert len(q.calls) == 1


async def test_no_retry_when_cap_is_spent() -> None:
    q = FakeQuery([_result("nope", cost=1.99)])
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA, cap=2.0))
    assert res.output is None and "no budget to retry" in (res.error or "")
    assert len(q.calls) == 1


async def test_sdk_exception_becomes_role_error() -> None:
    q = FakeQuery([RuntimeError("CLI not found")])
    res = await _runner(query_fn=q).run(_req("scout", SCOUT_SCHEMA))
    assert res.output is None and "CLI not found" in (res.error or "")
    assert res.cost_usd == 0.0


async def test_structured_output_is_used_when_present() -> None:
    structured = {"plan_hash": "h", "notes": "n"}
    q = FakeQuery([_result("prose only", structured=structured)])
    res = await _runner(query_fn=q).run(_req("designer", SIMPLE_SCHEMA))
    assert res.output == structured


# --------------------------------------------------------------------------- unknown cost (W5)


def _req_left(role: str, schema: dict[str, Any], cap: float | None, left: float | None) -> Any:
    from dataclasses import replace

    return replace(_req(role, schema, cap=cap), remaining_usd=left)


async def test_missing_cost_is_charged_at_the_cap_and_not_retried() -> None:
    q = FakeQuery([_result("prose, no JSON", cost=None, sid="s1")])  # type: ignore[arg-type]
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA, cap=1.5))
    assert res.cost_usd == pytest.approx(1.5)  # never 0 for an unknown cost
    assert res.cost_estimated is True
    assert res.output is None and "no budget to retry" in (res.error or "")
    assert len(q.calls) == 1


async def test_missing_cost_with_valid_output_still_charges_the_cap() -> None:
    good = _critic_json()
    q = FakeQuery([_result(good, cost=None)])  # type: ignore[arg-type]
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA, cap=0.7))
    assert res.output is not None
    assert res.cost_usd == pytest.approx(0.7) and res.cost_estimated is True


async def test_exception_after_the_call_started_is_charged_at_the_cap() -> None:
    q = FakeQuery(
        [
            AssistantMessage(content=[TextBlock(text="calling tools")], model="m"),
            RuntimeError("connection reset"),
        ]
    )
    res = await _runner(query_fn=q).run(_req("designer", DESIGNER_SCHEMA, cap=2.0))
    assert res.output is None and "connection reset" in (res.error or "")
    assert res.cost_usd == pytest.approx(2.0) and res.cost_estimated is True
    assert len(q.calls) == 1


async def test_stream_ending_without_a_result_is_charged_at_the_cap() -> None:
    q = FakeQuery([AssistantMessage(content=[TextBlock(text="half done")], model="m")])
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA, cap=0.9))
    assert res.cost_usd == pytest.approx(0.9) and res.cost_estimated is True
    assert res.output is None


async def test_exception_before_any_message_spent_nothing() -> None:
    q = FakeQuery([RuntimeError("CLI not found")])
    res = await _runner(query_fn=q).run(_req("scout", SCOUT_SCHEMA, cap=2.0))
    assert res.cost_usd == 0.0 and res.cost_estimated is False


async def test_retry_with_missing_cost_is_charged_its_remaining_cap() -> None:
    q = FakeQuery(
        [_result("I think it is fine.", cost=0.3, sid="s1")],
        [_result("still prose", cost=None, sid="s1")],  # type: ignore[arg-type]
    )
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA, cap=2.0))
    assert res.cost_usd == pytest.approx(2.0)  # 0.3 actual + 1.7 retry cap
    assert res.cost_estimated is True
    assert q.calls[1][1].max_budget_usd == pytest.approx(1.7)


async def test_without_a_cap_an_unknown_cost_charges_the_remaining_budget() -> None:
    q = FakeQuery([AssistantMessage(content=[TextBlock(text="x")], model="m"), OSError("pipe")])
    res = await _runner(query_fn=q).run(_req_left("critic", CRITIC_SCHEMA, cap=None, left=4.25))
    assert res.cost_usd == pytest.approx(4.25) and res.cost_estimated is True


def test_designer_may_run_the_crosscheck_but_the_critic_may_not() -> None:
    cross = "mcp__strategy-lab__run_crosscheck"
    assert cross in DESIGNER_LAB_TOOLS
    o = _runner().build_options(_req("designer", DESIGNER_SCHEMA))
    assert cross in o.allowed_tools and cross not in o.disallowed_tools
    c = _runner().build_options(_req("critic", CRITIC_SCHEMA))
    assert cross not in c.allowed_tools and cross in c.disallowed_tools


# --------------------------------------------------------------------------- Phase 13: env caps


@pytest.mark.parametrize(
    "role,schema",
    [("scout", SCOUT_SCHEMA), ("designer", DESIGNER_SCHEMA), ("critic", CRITIC_SCHEMA)],
)
def test_every_role_sets_the_subagent_caps_explicitly(role: str, schema: Any) -> None:
    o = _runner().build_options(_req(role, schema))
    assert o.env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
    assert o.env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "2"
    assert dict(SUBAGENT_ENV).items() <= o.env.items()
    assert o.permission_mode == "dontAsk"
    assert "Task" in o.disallowed_tools and "Agent" in o.disallowed_tools


@pytest.mark.parametrize("role,schema", [("designer", DESIGNER_SCHEMA), ("critic", CRITIC_SCHEMA)])
def test_lab_roles_turn_tool_search_off(role: str, schema: Any) -> None:
    o = _runner().build_options(_req(role, schema))
    assert o.env["ENABLE_TOOL_SEARCH"] == "false"


def test_env_caps_cannot_be_loosened_through_the_constant() -> None:
    with pytest.raises(TypeError):
        SUBAGENT_ENV["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] = "3"  # type: ignore[index]
    o = _runner().build_options(_req("critic", CRITIC_SCHEMA))
    o.env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] = "9"  # a copy per call, not the constant
    assert SUBAGENT_ENV["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"


# --------------------------------------------------------------------------- tool list check


def test_lab_server_registers_exactly_the_lab_tools() -> None:
    names = lab_server_tool_names()
    assert sorted(names) == sorted(f"{MCP_PREFIX}{t}" for t in LAB_TOOLS)
    assert len(names) == 12


def test_missing_init_tools_names_what_the_role_lacks() -> None:
    every = list(DESIGNER_LAB_TOOLS)
    assert missing_init_tools("designer", every) == []
    assert missing_init_tools("designer", every[1:]) == [DESIGNER_LAB_TOOLS[0]]
    assert missing_init_tools("scout", ["WebFetch"]) == ["WebSearch"]
    assert missing_init_tools("critic", []) == list(CRITIC_LAB_TOOLS)


def test_expected_init_tools_cover_every_role_and_hide_disallowed_ones() -> None:
    for role in ("scout", "designer", "critic"):
        assert missing_init_tools(role, expected_init_tools(role)) == []  # type: ignore[arg-type]
        assert forbidden_init_tools(role, expected_init_tools(role)) == []  # type: ignore[arg-type]
    assert HOLDOUT_TOOL not in expected_init_tools("designer")
    assert expected_init_tools("scout") == ["WebSearch", "WebFetch"]


def _init(tools: list[str]) -> SystemMessage:
    return SystemMessage(subtype="init", data={"type": "system", "subtype": "init", "tools": tools})


async def test_init_missing_a_tool_is_an_error_and_recorded() -> None:
    listed = [t for t in DESIGNER_LAB_TOOLS if not t.endswith("run_sweep")]
    q = FakeQuery([_init(listed), _result('{"plan_hash": "h", "notes": "n"}', cost=0.3)])
    runner = _runner(query_fn=q)
    res = await runner.run(_req("designer", SIMPLE_SCHEMA))
    assert res.output is None  # no output accepted from a session missing tools
    assert res.error == "init tool list missing: mcp__strategy-lab__run_sweep"
    assert is_init_tools_error(res.error)
    assert runner.init_checks == [("designer", ["mcp__strategy-lab__run_sweep"])]
    # Fail closed: the CLI may already have sent a billed request when init is read.
    assert res.cost_usd == pytest.approx(2.0) and res.cost_estimated is True
    assert res.invalid_outputs == 0
    assert len(q.calls) == 1  # never retried
    assert q.closed == [True]  # the session was closed at once


async def test_init_missing_after_the_model_ran_is_charged_the_cap() -> None:
    q = FakeQuery(
        [
            AssistantMessage(content=[TextBlock(text="thinking")], model="m"),
            _init([]),
            _result(_critic_json(), cost=0.1),
        ]
    )
    runner = _runner(query_fn=q)
    res = await runner.run(_req("critic", CRITIC_SCHEMA, cap=1.25))
    assert res.output is None and (res.error or "").startswith("init tool list missing")
    assert res.cost_usd == pytest.approx(1.25) and res.cost_estimated is True
    assert runner.init_checks == [("critic", list(CRITIC_LAB_TOOLS))]


async def test_init_abort_without_a_cap_charges_the_run_remaining_budget() -> None:
    q = FakeQuery([_init([])])
    res = await _runner(query_fn=q).run(_req_left("critic", CRITIC_SCHEMA, cap=None, left=3.5))
    assert res.error is not None and res.error.startswith(INIT_MISSING_ERROR)
    assert res.cost_usd == pytest.approx(3.5) and res.cost_estimated is True


async def test_init_abort_on_the_retry_is_charged_its_remaining_cap() -> None:
    q = FakeQuery(
        [_init(list(CRITIC_LAB_TOOLS)), _result("prose", cost=0.4, sid="s1")],
        [_init([*CRITIC_LAB_TOOLS, "Bash"])],
    )
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA, cap=2.0))
    assert res.output is None and (res.error or "").startswith(INIT_FORBIDDEN_ERROR)
    assert res.cost_usd == pytest.approx(2.0)  # 0.4 known + 1.6 unknown retry cap
    assert res.cost_estimated is True


def test_forbidden_init_tools_names_what_the_role_must_not_have() -> None:
    for role in ("scout", "designer", "critic"):
        for name in ("Bash", "Edit", "Write", "Task", "Agent", HOLDOUT_TOOL, START_RUN_TOOL):
            assert forbidden_init_tools(role, [name]) == [name], (role, name)  # type: ignore[arg-type]
        assert forbidden_init_tools(role, ["mcp__other__holdout_check"]) != []  # type: ignore[arg-type]
        assert forbidden_init_tools(role, ["mcp__other__start_run"]) != []  # type: ignore[arg-type]
        assert forbidden_init_tools(role, ["mcp__x__place_order"]) != []  # type: ignore[arg-type]
    # The role's disallowed list: web tools for the lab roles, Designer tools for the Critic.
    assert forbidden_init_tools("designer", ["WebFetch"]) == ["WebFetch"]
    sweep = f"{MCP_PREFIX}run_sweep"
    assert forbidden_init_tools("critic", [sweep]) == [sweep]
    assert forbidden_init_tools("designer", [sweep]) == []  # its own tool
    assert forbidden_init_tools("scout", ["WebSearch", "WebFetch", "Read"]) == []


@pytest.mark.parametrize(
    "role,schema,extra",
    [
        ("designer", DESIGNER_SCHEMA, HOLDOUT_TOOL),
        ("designer", DESIGNER_SCHEMA, START_RUN_TOOL),
        ("critic", CRITIC_SCHEMA, f"{MCP_PREFIX}run_backtest"),
        ("scout", SCOUT_SCHEMA, "Bash"),
        ("scout", SCOUT_SCHEMA, "Agent"),
    ],
)
async def test_init_listing_a_forbidden_tool_is_an_init_failure(
    role: str, schema: Any, extra: str
) -> None:
    listed = [*expected_init_tools(role), extra]  # type: ignore[arg-type]
    good = _critic_json() if role == "critic" else "{}"
    q = FakeQuery([_init(listed), _result(good, cost=0.1)])
    runner = _runner(query_fn=q)
    res = await runner.run(_req(role, schema, cap=1.0))
    assert res.output is None
    assert res.error == f"{INIT_FORBIDDEN_ERROR}: {extra}"
    assert is_init_tools_error(res.error)
    assert runner.init_checks == [(role, [f"forbidden {extra}"])]
    assert res.cost_usd == pytest.approx(1.0) and res.cost_estimated is True
    assert res.invalid_outputs == 0 and len(q.calls) == 1 and q.closed == [True]


async def test_init_missing_and_forbidden_are_both_reported() -> None:
    q = FakeQuery([_init([CRITIC_LAB_TOOLS[0], CRITIC_LAB_TOOLS[1], "Write"])])
    runner = _runner(query_fn=q)
    res = await runner.run(_req("critic", CRITIC_SCHEMA))
    assert res.error == (
        f"{INIT_MISSING_ERROR}: {CRITIC_LAB_TOOLS[2]}; {INIT_FORBIDDEN_ERROR}: Write"
    )
    assert runner.init_checks == [("critic", [CRITIC_LAB_TOOLS[2], "forbidden Write"])]


async def test_full_init_list_succeeds_and_records_an_empty_check() -> None:
    q = FakeQuery([_init([*CRITIC_LAB_TOOLS, "Read"]), _result(_critic_json(), cost=0.2)])
    runner = _runner(query_fn=q)
    res = await runner.run(_req("critic", CRITIC_SCHEMA))
    assert res.output == _critic() and res.error is None
    assert runner.init_checks == [("critic", [])]
    assert all(not missing for _, missing in runner.init_checks)


async def test_retry_session_is_checked_too() -> None:
    q = FakeQuery(
        [_init(list(CRITIC_LAB_TOOLS)), _result("prose", cost=0.1, sid="s1")],
        [_init(list(CRITIC_LAB_TOOLS)), _result(_critic_json(), cost=0.1, sid="s1")],
    )
    runner = _runner(query_fn=q)
    res = await runner.run(_req("critic", CRITIC_SCHEMA))
    assert res.output is not None
    assert runner.init_checks == [("critic", []), ("critic", [])]


# --------------------------------------------------------------------------- invalid outputs


async def test_valid_first_output_counts_no_invalid_outputs() -> None:
    q = FakeQuery([_result(_critic_json(), cost=0.1)])
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA))
    assert res.output is not None and res.invalid_outputs == 0


async def test_invalid_then_valid_counts_one_invalid_output() -> None:
    bad = {"verdict": "promising", "reasons": [], "evidence": []}  # fails minItems
    q = FakeQuery(
        [_result("see structured", cost=0.1, structured=bad)],
        [_result("ok", cost=0.1, structured=_critic("promising"))],
    )
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA))
    assert res.output == _critic("promising")
    assert res.invalid_outputs == 1


async def test_invalid_twice_counts_two_invalid_outputs() -> None:
    q = FakeQuery([_result("nope", cost=0.1)], [_result('{"verdict": "maybe"}', cost=0.1)])
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA))
    assert res.output is None and res.invalid_outputs == 2


async def test_invalid_with_no_budget_to_retry_counts_one() -> None:
    q = FakeQuery([_result("nope", cost=1.99)])
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA, cap=2.0))
    assert res.invalid_outputs == 1 and "no budget to retry" in (res.error or "")


async def test_sdk_structured_output_retries_exhausted_counts_as_invalid() -> None:
    q = FakeQuery(
        [_result(None, cost=0.4, subtype="error_max_structured_output_retries", is_error=True)]
    )
    res = await _runner(query_fn=q).run(_req("designer", SIMPLE_SCHEMA))
    assert res.output is None and res.invalid_outputs == 1
    assert "error_max_structured_output_retries" in (res.error or "")
    assert len(q.calls) == 1  # the SDK already retried; no extra paid retry


async def test_errors_that_are_not_outputs_count_zero() -> None:
    budget = FakeQuery(
        [_result("partial", cost=1.9, subtype="error_max_budget_usd", is_error=True)]
    )
    res = await _runner(query_fn=budget).run(_req("critic", CRITIC_SCHEMA))
    assert res.output is None and res.invalid_outputs == 0
    crash = FakeQuery([RuntimeError("CLI not found")])
    res2 = await _runner(query_fn=crash).run(_req("critic", CRITIC_SCHEMA))
    assert res2.invalid_outputs == 0


# --------------------------------------------------------------------------- scout PostToolUse


def test_scout_options_screen_web_tool_output_after_use() -> None:
    o = _runner().build_options(_req("scout", SCOUT_SCHEMA))
    [matcher] = o.hooks["PostToolUse"]  # type: ignore[index]
    assert matcher.matcher == "WebFetch|WebSearch"
    assert matcher.timeout == 60.0
    assert len(matcher.hooks) == 1 and matcher.hooks[0].__name__ == "scout_screen_hook"


@pytest.mark.parametrize("role,schema", [("designer", DESIGNER_SCHEMA), ("critic", CRITIC_SCHEMA)])
def test_lab_roles_have_no_post_tool_use_hook(role: str, schema: Any) -> None:
    o = _runner().build_options(_req(role, schema))
    assert "PostToolUse" not in o.hooks  # type: ignore[operator]


async def test_scout_hook_uses_the_runner_jev_client() -> None:
    class RaisingJev:
        enabled = True
        calls = 0

        def ask(self, state: Any, questions: Any) -> Any:
            RaisingJev.calls += 1
            raise RuntimeError("jev down")

    o = _runner(jev=RaisingJev()).build_options(_req("scout", SCOUT_SCHEMA))  # type: ignore[arg-type]
    hook = o.hooks["PostToolUse"][0].hooks[0]  # type: ignore[index]
    out = await hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://x.example/a", "prompt": "p"},
            "tool_response": {
                "bytes": 3,
                "code": 200,
                "codeText": "OK",
                "result": "RSI rule",
                "durationMs": 5,
                "url": "https://x.example/a",
            },
        },
        "t1",
        None,
    )
    assert RaisingJev.calls == 1  # the runner's client was asked, then heuristics took over
    assert "RSI rule" in out["hookSpecificOutput"]["updatedToolOutput"]["result"]


# --------------------------------------------------------------------------- scripted runner


async def test_scripted_runner_checks_the_real_lab_tool_list_by_default() -> None:
    runner = ScriptedAgentRunner(scripts={"critic": _critic(), "designer": {}, "scout": {}})
    for role in ("scout", "designer", "critic"):
        res = await runner.run(_req(role, CRITIC_SCHEMA))
        assert res.error is None
    assert [r for r, _ in runner.init_checks] == ["scout", "designer", "critic"]
    assert all(not missing for _, missing in runner.init_checks)


async def test_scripted_runner_returns_an_error_when_a_tool_is_missing() -> None:
    runner = ScriptedAgentRunner(
        scripts={"designer": {"plan_hash": "h"}},
        init_tools=lambda role: [t for t in expected_init_tools(role) if "walk_forward" not in t],
    )
    res = await runner.run(_req("designer", DESIGNER_SCHEMA))
    assert res.output is None
    assert res.error == "init tool list missing: mcp__strategy-lab__walk_forward"
    assert runner.init_checks == [("designer", ["mcp__strategy-lab__walk_forward"])]


async def test_scripted_runner_flags_a_forbidden_tool_and_charges_the_call() -> None:
    runner = ScriptedAgentRunner(
        scripts={"critic": _critic()},
        cost_per_call=0.3,
        init_tools=lambda role: [*expected_init_tools(role), HOLDOUT_TOOL],
    )
    res = await runner.run(_req("critic", CRITIC_SCHEMA))
    assert res.output is None and res.error == f"{INIT_FORBIDDEN_ERROR}: {HOLDOUT_TOOL}"
    assert res.cost_usd == pytest.approx(0.3) and res.cost_estimated is True
    assert runner.init_checks == [("critic", [f"forbidden {HOLDOUT_TOOL}"])]


@pytest.mark.parametrize("role", ["scout", "designer", "critic"])
async def test_every_role_may_call_the_structured_output_tool(role: Any) -> None:
    """Live smoke (CLI 2.1.310): output_format answers arrive through the CLI's synthetic
    StructuredOutput tool; denying it left every role retrying until error_max_turns."""
    from research.sdk_runner import STRUCTURED_OUTPUT_TOOL, forbidden_init_tools

    runner = _runner()
    opts = runner.build_options(_req(role, SIMPLE_SCHEMA))
    assert STRUCTURED_OUTPUT_TOOL in opts.allowed_tools
    assert STRUCTURED_OUTPUT_TOOL not in opts.tools  # not a built-in the role can opt into
    for matcher in opts.hooks["PreToolUse"]:
        for hook in matcher.hooks:
            assert await hook({"tool_name": STRUCTURED_OUTPUT_TOOL}, None, None) == {}
    assert forbidden_init_tools(role, [STRUCTURED_OUTPUT_TOOL]) == []
