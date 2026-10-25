"""SdkAgentRunner: per-role option building, hooks, and JSON-output handling. No network: the
SDK's ``query`` is replaced by a fake async generator yielding real SDK message objects."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
)

from research.orchestrator import CRITIC_SCHEMA, DESIGNER_SCHEMA, SCOUT_SCHEMA, RoleRequest
from research.sdk_runner import (
    CRITIC_LAB_TOOLS,
    DESIGNER_LAB_TOOLS,
    HOLDOUT_TOOL,
    START_RUN_TOOL,
    SdkAgentRunner,
    deny_unsafe_tools,
    extract_output,
    parse_last_json_object,
)

RUN_ID = "r-20261024-120000-abc123"
REPO = Path("/repo")


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

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        self.calls.append((prompt, options))
        script = self.scripts.pop(0)

        async def gen() -> AsyncIterator[Any]:
            for m in script:
                if isinstance(m, Exception):
                    raise m
                yield m

        return gen()


# --------------------------------------------------------------------------- options per role


def test_scout_options_web_only_no_mcp() -> None:
    o = _runner().build_options(_req("scout", SCOUT_SCHEMA))
    assert o.tools == ["WebSearch", "WebFetch"]
    assert o.allowed_tools == ["WebSearch", "WebFetch"]
    assert o.mcp_servers == {}
    assert o.strict_mcp_config is True
    for t in ("Bash", "Edit", "Write", "NotebookEdit", HOLDOUT_TOOL, START_RUN_TOOL):
        assert t in o.disallowed_tools
    assert o.model == "sonnet"


def test_designer_options_pin_run_and_disable_holdout() -> None:
    o = _runner().build_options(_req("designer", DESIGNER_SCHEMA))
    assert o.tools == []  # no built-in tools at all
    assert set(o.allowed_tools) == set(DESIGNER_LAB_TOOLS)
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
    assert set(o.allowed_tools) == set(CRITIC_LAB_TOOLS)
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
    good = {"verdict": "refine", "reasons": [], "lessons": []}
    assert extract_output(good, "junk", CRITIC_SCHEMA) == (good, [])
    out, errs = extract_output(None, '{"verdict": "maybe"}', CRITIC_SCHEMA)
    assert out is None and any("verdict" in e or "reasons" in e for e in errs)


# --------------------------------------------------------------------------- run + retry


async def test_run_returns_parsed_output_and_cost() -> None:
    q = FakeQuery(
        [
            AssistantMessage(content=[TextBlock(text="working")], model="m"),
            _result(
                'Done.\n```json\n{"verdict": "abandon", "reasons": ["x"], "lessons": []}\n```',
                cost=0.25,
                sid="abc",
            ),
        ]
    )
    res = await _runner(query_fn=q).run(_req("critic", CRITIC_SCHEMA))
    assert res.output == {"verdict": "abandon", "reasons": ["x"], "lessons": []}
    assert res.cost_usd == 0.25 and res.session_id == "abc" and res.error is None
    assert len(q.calls) == 1


async def test_bad_output_gets_one_retry_in_same_session_and_costs_add_up() -> None:
    q = FakeQuery(
        [_result("I think it is fine.", cost=0.3, sid="s1")],
        [_result('{"verdict": "promising", "reasons": [], "lessons": []}', cost=0.1, sid="s1")],
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
    structured = {"plan_hash": "h", "plan": {"a": 1}, "trial_ids": [3], "notes": "n"}
    q = FakeQuery([_result("prose only", structured=structured)])
    res = await _runner(query_fn=q).run(_req("designer", DESIGNER_SCHEMA))
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
    good = '{"verdict": "abandon", "reasons": [], "lessons": []}'
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
