"""Offline tests for smoke_live.py (the live SDK smoke check): no session is opened here."""

from __future__ import annotations

from typing import Any

import pytest
import smoke_live as sl

from research.orchestrator import CRITIC_SCHEMA, DESIGNER_SCHEMA, validate_output
from research.sdk_runner import CRITIC_LAB_TOOLS, DESIGNER_LAB_TOOLS, SdkAgentRunner


async def test_deny_hook_blocks_every_lab_tool_and_websearch_only() -> None:
    for name in ("mcp__strategy-lab__get_ledger", "mcp__strategy-lab__run_backtest", "WebSearch"):
        out = await sl.deny_lab_and_search({"tool_name": name}, None, None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await sl.deny_lab_and_search({"tool_name": "WebFetch"}, None, None) == {}


def test_fixed_replies_satisfy_the_real_role_schemas() -> None:
    assert validate_output(sl.DESIGNER_REPLY, DESIGNER_SCHEMA) == []
    assert validate_output(sl.CRITIC_REPLY, CRITIC_SCHEMA) == []


@pytest.mark.parametrize("role", ["scout", "designer", "critic"])
def test_smoke_options_are_the_loops_own_plus_the_deny_hook(role: Any) -> None:
    runner = SdkAgentRunner(jev=None)
    schema = {"scout": sl.SCOUT_SMOKE_SCHEMA, "designer": DESIGNER_SCHEMA}.get(role, CRITIC_SCHEMA)
    req = sl.RoleRequest(
        role=role, prompt="", run_id=sl.SMOKE_RUN_ID, output_schema=schema, max_budget_usd=0.5
    )
    base = runner.build_options(req)
    opts = sl.smoke_options(runner, role, schema, 0.5, sl.HookLog())
    for name in (
        "tools",
        "allowed_tools",
        "disallowed_tools",
        "mcp_servers",
        "strict_mcp_config",
        "permission_mode",
        "system_prompt",
        "model",
        "env",
        "output_format",
        "max_budget_usd",
        "setting_sources",
    ):
        assert getattr(opts, name) == getattr(base, name), name
    assert opts.max_turns == sl.MAX_TURNS[role]
    pre = opts.hooks["PreToolUse"]
    assert pre[0].hooks == [sl.deny_lab_and_search]
    assert len(pre) == len(base.hooks["PreToolUse"]) + 1
    assert ("PostToolUse" in opts.hooks) == (role == "scout")


async def test_recorder_counts_rewrites_and_keeps_the_hook_output() -> None:
    log = sl.HookLog()
    rewritten = {"hookSpecificOutput": {"updatedToolOutput": {"result": "x"}}}

    async def hook(input_data: Any, tool_use_id: Any, context: Any) -> dict[str, Any]:
        return rewritten

    out = await sl.recording(hook, log)({"tool_name": "WebFetch"}, None, None)
    assert out is rewritten
    assert log.calls == ["WebFetch"] and log.rewrites == 1


def _ok_session(role: str, tools: list[str], structured: Any) -> sl.Session:
    return sl.Session(
        role,
        tools=tools,
        mcp={"strategy-lab": "connected"},
        subtype="success",
        structured=structured,
    )


def test_check_session_passes_the_expected_designer_and_critic_sessions() -> None:
    d = _ok_session("designer", list(DESIGNER_LAB_TOOLS), sl.DESIGNER_REPLY)
    c = _ok_session("critic", list(CRITIC_LAB_TOOLS), sl.CRITIC_REPLY)
    assert sl.check_session(d, "designer", DESIGNER_SCHEMA) == []
    assert sl.check_session(c, "critic", CRITIC_SCHEMA) == []


def test_check_session_flags_missing_forbidden_disconnected_and_bad_output() -> None:
    tools = [t for t in DESIGNER_LAB_TOOLS if not t.endswith("run_sweep")] + ["Bash"]
    s = sl.Session(
        "designer", tools=tools, mcp={"strategy-lab": "failed"}, subtype="success", structured={}
    )
    failed = " | ".join(sl.check_session(s, "designer", DESIGNER_SCHEMA))
    assert "missing mcp__strategy-lab__run_sweep" in failed
    assert "forbidden tools Bash" in failed
    assert "status is 'failed'" in failed
    assert "fails its schema" in failed
    assert sl.check_session(sl.Session("critic"), "critic", CRITIC_SCHEMA)[0].startswith(
        "no init message"
    )
