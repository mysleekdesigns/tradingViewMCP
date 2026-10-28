"""Live smoke check of the research roles' Agent SDK setup (PLAN.md Phase 13 owner checks).

Usage (from ``agents/``)::

    uv run python smoke_live.py [--url https://example.com/] [--max-usd 0.5] [--model sonnet]

Opens one short, real Agent SDK session per role (scout, designer, critic) with the exact
options the research loop builds (:meth:`research.sdk_runner.SdkAgentRunner.build_options`)
and reports:

* each role's real ``init`` tool list and MCP server status, and the missing / forbidden tools
  the loop's init check would flag (a flag there makes every live research run exit 3);
* whether the CLI accepts the Designer's and Critic's ``output_format`` schemas (the session
  ends with a schema-valid structured output);
* for the Scout: one ``WebFetch`` of ``--url``, whether the PostToolUse screening hook ran and
  returned a rewrite, and whether the model received the rewritten text (``<untrusted_source>``
  markers), i.e. whether the CLI accepted the rewrite's output shape.

Two additions to the loop's options, for safety and cost only: an extra PreToolUse hook denies
every ``mcp__`` tool and ``WebSearch`` (no lab tool ever runs, so nothing reaches the ledger or
the DSR trial count), and ``max_turns`` is small. Each session is capped at ``--max-usd``.
Exit code 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, SystemMessage, query

from research.orchestrator import (
    CRITIC_SCHEMA,
    DESIGNER_SCHEMA,
    DRAFT_07,
    Role,
    RoleRequest,
    validate_output,
)
from research.screening import OPEN_MARKER
from research.sdk_runner import (
    MCP_SERVER_NAME,
    SCOUT_HOOK_MATCHER,
    SCOUT_HOOK_TIMEOUT_S,
    HookCallback,
    RoleModels,
    SdkAgentRunner,
    init_tool_problems,
    make_scout_screen_hook,
)

# The lab server is pinned to this id; no lab tool can run in a smoke session anyway.
SMOKE_RUN_ID = "smoke-live-no-run"
DEFAULT_URL = "https://example.com/"
MAX_TURNS: dict[str, int] = {"scout": 4, "designer": 3, "critic": 3}

DESIGNER_REPLY: dict[str, Any] = {
    "plan_hash": "",
    "plan": {},
    "trial_ids": [],
    "notes": "smoke test: no plan",
}
CRITIC_REPLY: dict[str, Any] = {
    "verdict": "abandon",
    "reasons": ["smoke test"],
    "evidence": [{"field": "smoke_test", "value": "none", "source": "prompt"}],
    "lessons": [],
    "failure_tags": ["other"],
}
SCOUT_SMOKE_SCHEMA: dict[str, Any] = {
    "$schema": DRAFT_07,
    "type": "object",
    "required": ["fetched", "saw_untrusted_marker", "excerpt"],
    "properties": {
        "fetched": {"type": "boolean"},
        "saw_untrusted_marker": {"type": "boolean"},
        "excerpt": {"type": "string", "maxLength": 400},
    },
    "additionalProperties": False,
}


def fixed_reply_prompt(reply: Mapping[str, Any]) -> str:
    return (
        "This is a connectivity check of this session's setup, not research. Do not call any "
        "tool. Reply now with exactly this JSON object as your structured output, nothing "
        f"else: {json.dumps(reply, sort_keys=True)}"
    )


def scout_prompt(url: str) -> str:
    return (
        "This is a connectivity check of this session's setup, not research. Call WebFetch "
        f"exactly once on {url} with the prompt 'Return the page text verbatim.' Call no other "
        "tool. Then report: fetched (did the tool return content), saw_untrusted_marker (does "
        f"the tool result you received contain the literal text {OPEN_MARKER}), and excerpt "
        "(the first 200 characters of the tool result exactly as you received it)."
    )


# --------------------------------------------------------------------------- hooks


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


async def deny_lab_and_search(
    input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
) -> dict[str, Any]:
    """Smoke-only PreToolUse hook: no lab tool and no WebSearch ever runs."""
    name = str(input_data.get("tool_name", ""))
    if name.startswith("mcp__"):
        return _deny("smoke test: lab tools are never called")
    if name == "WebSearch":
        return _deny("smoke test: only one WebFetch is allowed")
    return {}


@dataclass
class HookLog:
    """What the real scout screening hook did during the smoke session."""

    calls: list[str] = field(default_factory=list)
    rewrites: int = 0
    notes: list[str] = field(default_factory=list)


def recording(hook: HookCallback, log: HookLog) -> HookCallback:
    """Wrap the real scout hook and record its calls and rewrites (output unchanged)."""

    async def wrapper(
        input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        out = await hook(input_data, tool_use_id, context)
        log.calls.append(str(input_data.get("tool_name", "")))
        spec = out.get("hookSpecificOutput") if isinstance(out, Mapping) else None
        if isinstance(spec, Mapping) and "updatedToolOutput" in spec:
            log.rewrites += 1
            if spec.get("additionalContext"):
                log.notes.append(str(spec["additionalContext"])[:200])
        return out

    return wrapper


def smoke_options(
    runner: SdkAgentRunner, role: Role, schema: dict[str, Any], max_usd: float, log: HookLog
) -> ClaudeAgentOptions:
    """The loop's own options for ``role`` plus the smoke-only deny hook and a small turn cap;
    the scout's real screening hook is wrapped by a recorder."""
    req = RoleRequest(
        role=role, prompt="", run_id=SMOKE_RUN_ID, output_schema=schema, max_budget_usd=max_usd
    )
    opts = runner.build_options(req)
    hooks: dict[str, list[HookMatcher]] = dict(opts.hooks or {})  # type: ignore[arg-type]
    hooks["PreToolUse"] = [
        HookMatcher(matcher=None, hooks=[deny_lab_and_search]),
        *hooks.get("PreToolUse", []),
    ]
    if role == "scout":
        hooks["PostToolUse"] = [
            HookMatcher(
                matcher=SCOUT_HOOK_MATCHER,
                hooks=[recording(make_scout_screen_hook(runner.jev), log)],
                timeout=SCOUT_HOOK_TIMEOUT_S,
            )
        ]
    return replace(opts, hooks=hooks, max_turns=MAX_TURNS[role])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- sessions


@dataclass
class Session:
    role: str
    model: str = ""
    tools: list[str] | None = None
    mcp: dict[str, str] = field(default_factory=dict)
    subtype: str | None = None
    structured: Any = None
    cost: float | None = None
    error: str | None = None


async def run_session(prompt: str, options: ClaudeAgentOptions, role: str) -> Session:
    s = Session(role)
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, SystemMessage) and msg.subtype == "init" and s.tools is None:
                data = msg.data if isinstance(msg.data, Mapping) else {}
                s.tools = [str(t) for t in data.get("tools") or []]
                s.model = str(data.get("model") or "")
                for server in data.get("mcp_servers") or []:
                    if isinstance(server, Mapping):
                        s.mcp[str(server.get("name"))] = str(server.get("status"))
            elif isinstance(msg, ResultMessage):
                s.subtype = msg.subtype
                s.structured = msg.structured_output
                s.cost = msg.total_cost_usd
                if msg.is_error or msg.subtype != "success":
                    s.error = "; ".join(msg.errors or []) or str(msg.subtype)
    except Exception as exc:  # report, never crash: this is a diagnostic
        s.error = f"{type(exc).__name__}: {str(exc)[:500]}"
    return s


def check_session(s: Session, role: Role, schema: Mapping[str, Any]) -> list[str]:
    """Failed checks for a session (empty = passed)."""
    if s.tools is None:
        return [f"no init message received ({s.error or 'session did not start'})"]
    failed: list[str] = []
    missing, forbidden = init_tool_problems(role, s.tools)
    if missing:
        failed.append(f"init tool list is missing {', '.join(missing)}")
    if forbidden:
        failed.append(f"init tool list has forbidden tools {', '.join(forbidden)}")
    if role != "scout" and s.mcp.get(MCP_SERVER_NAME) != "connected":
        failed.append(f"{MCP_SERVER_NAME} server status is {s.mcp.get(MCP_SERVER_NAME)!r}")
    if s.subtype != "success":
        failed.append(f"session ended {s.subtype!r}: {s.error}")
    elif not isinstance(s.structured, dict):
        failed.append("no structured output returned")
    else:
        errors = validate_output(s.structured, schema)
        if errors:
            failed.append(f"structured output fails its schema: {'; '.join(errors[:3])}")
    return failed


def print_session(s: Session, failed: Sequence[str]) -> None:
    status = "PASS" if not failed else "FAIL"
    cost = "unknown" if s.cost is None else f"${s.cost:.4f}"
    print(f"\n== {s.role}: {status} (model {s.model or '?'}, cost {cost})")
    if s.mcp:
        print("   MCP servers: " + ", ".join(f"{k}={v}" for k, v in s.mcp.items()))
    if s.tools is not None:
        print(f"   init tools ({len(s.tools)}): {', '.join(s.tools) or '(none)'}")
    if s.structured is not None:
        print(f"   structured output: {json.dumps(s.structured, sort_keys=True)[:400]}")
    for f in failed:
        print(f"   - {f}")


async def smoke(url: str, max_usd: float, models: RoleModels) -> int:
    from research.jev import JevClient  # same client the live loop passes to the runner

    runner = SdkAgentRunner(models=models, jev=JevClient())
    log = HookLog()
    plans: list[tuple[Role, str, dict[str, Any]]] = [
        ("designer", fixed_reply_prompt(DESIGNER_REPLY), DESIGNER_SCHEMA),
        ("critic", fixed_reply_prompt(CRITIC_REPLY), CRITIC_SCHEMA),
        ("scout", scout_prompt(url), SCOUT_SMOKE_SCHEMA),
    ]
    all_ok = True
    total = 0.0
    for role, prompt, schema in plans:
        opts = smoke_options(runner, role, schema, max_usd, log)
        s = await run_session(prompt, opts, role)
        failed = check_session(s, role, schema)
        if role == "scout":
            if "WebFetch" not in log.calls:
                failed.append("the screening hook never ran on a WebFetch result")
            elif not log.rewrites:
                failed.append("the screening hook ran but returned no rewrite")
            out = s.structured if isinstance(s.structured, dict) else {}
            if log.rewrites and out.get("saw_untrusted_marker") is not True:
                failed.append(
                    "the model did not see the rewritten text: the CLI may have rejected the "
                    "rewrite's output shape and passed the original page through"
                )
        print_session(s, failed)
        if role == "scout":
            print(f"   hook calls: {log.calls or '(none)'}; rewrites: {log.rewrites}")
            for note in log.notes:
                print(f"   hook note: {note}")
        total += s.cost or 0.0
        all_ok = all_ok and not failed
    print(f"\nsmoke result: {'PASS' if all_ok else 'FAIL'} (total reported cost ${total:.4f})")
    return 0 if all_ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=DEFAULT_URL, help="page the Scout fetches once")
    ap.add_argument("--max-usd", type=float, default=0.5, help="dollar cap per session")
    ap.add_argument("--model", default=None, help="one model alias for every role (cheaper)")
    args = ap.parse_args(argv)
    if not args.max_usd > 0:
        print("error: --max-usd must be > 0", file=sys.stderr)
        return 2
    models = RoleModels() if args.model is None else RoleModels(args.model, args.model, args.model)
    return asyncio.run(smoke(args.url, args.max_usd, models))


if __name__ == "__main__":
    sys.exit(main())
