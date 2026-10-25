"""Claude Agent SDK runner for the Scout, Designer, and Critic roles.

Each role call is a **fresh SDK session** (``claude_agent_sdk.query``) with its own options:

* **scout**: ``WebSearch`` + ``WebFetch`` only, no MCP servers.
* **designer**: the ``strategy-lab`` MCP tools except ``holdout_check`` and ``start_run``. The MCP
  server is pinned to the run (``STRATEGY_LAB_RUN_ID``) and its holdout tool is disabled
  (``STRATEGY_LAB_DISABLE_HOLDOUT=1``). No built-in tools at all.
* **critic**: fresh context, read-only ``get_report`` / ``get_ledger`` / ``get_budget``.

Layers of protection (any one is enough to stop a forbidden call):

1. ``tools=[...]`` restricts the built-in tool set; ``strict_mcp_config`` ignores the project's
   ``.mcp.json`` (an unpinned server with the holdout enabled).
2. ``disallowed_tools`` removes Bash/Edit/Write/NotebookEdit, ``holdout_check``, ``start_run``.
3. ``permission_mode="dontAsk"``: anything not pre-approved in ``allowed_tools`` is denied.
4. SDK ``PreToolUse`` hooks: :func:`deny_unsafe_tools` (edits, Bash, holdout, order/withdraw
   names) and a per-role allowlist (:func:`make_allowlist_hook`).
5. ``setting_sources=["project"]`` so the project's own hooks (``.claude/hooks/protect-files.sh``,
   ``guard-bash.sh``, ``block-order-tools.sh``, ``confirm-holdout.sh``) also run.

Output: each role must end with one JSON object matching its schema. The SDK's structured output
(``output_format``) is requested; otherwise the last JSON object in the result text is parsed.
Invalid output gets one retry in the same session; the per-call dollar cap covers both attempts.

Dollars fail closed: when an attempt's cost is unknown (``ResultMessage.total_cost_usd`` is
None, or the stream raised or ended after it started without a result) the attempt is charged
its full ``max_budget_usd`` cap (or, with no cap, the run's remaining budget,
``RoleRequest.remaining_usd``), never zero. A retry attempt is charged the same way. An attempt
that fails before the CLI yields any message (e.g. the CLI is not installed) spent nothing.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    query,
)

from lab.research.contract import AGENTS_DIR, REPO_ROOT
from research.orchestrator import (
    ROLE_SCHEMAS,
    Role,
    RoleRequest,
    RoleResult,
    validate_output,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = AGENTS_DIR / "prompts"
MCP_SERVER_NAME = "strategy-lab"
MCP_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
HOLDOUT_TOOL = f"{MCP_PREFIX}holdout_check"
START_RUN_TOOL = f"{MCP_PREFIX}start_run"
LAB_TOOLS: tuple[str, ...] = (
    "start_run",
    "list_features",
    "validate_plan",
    "run_backtest",
    "run_sweep",
    "walk_forward",
    "run_adversarial",
    "get_ledger",
    "get_report",
    "get_budget",
    "run_crosscheck",
    "holdout_check",
)
DESIGNER_LAB_TOOLS: tuple[str, ...] = tuple(
    f"{MCP_PREFIX}{t}" for t in LAB_TOOLS if t not in ("holdout_check", "start_run")
)
CRITIC_LAB_TOOLS: tuple[str, ...] = tuple(
    f"{MCP_PREFIX}{t}" for t in ("get_report", "get_ledger", "get_budget")
)
SCOUT_TOOLS: tuple[str, ...] = ("WebSearch", "WebFetch")
EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
SHELL_TOOLS: frozenset[str] = frozenset({"Bash", "BashOutput", "KillShell", "KillBash"})
ORDER_PATTERN = re.compile(
    r"(place|submit|create|cancel|replace|modify|amend|close)_?(order|position|trade)|withdraw",
    re.IGNORECASE,
)
# Always removed for every role (belt and braces next to the hooks).
BASE_DISALLOWED: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "Task",
    "Agent",
    HOLDOUT_TOOL,
    START_RUN_TOOL,
)
RETRY_PROMPT = (
    "Your final message did not contain one valid JSON object matching the required schema "
    "({errors}). Reply now with ONLY that JSON object, no prose and no tool calls."
)
MIN_RETRY_USD = 0.02


# --------------------------------------------------------------------------- hooks


def unsafe_tool_reason(tool_name: str) -> str | None:
    """Why ``tool_name`` is forbidden for every research role, or None if not forbidden."""
    name = str(tool_name or "")
    if name in EDIT_TOOLS:
        return f"{name} is blocked: research agents never edit files (the lab is read-only)."
    if name in SHELL_TOOLS:
        return f"{name} is blocked: research agents have no shell."
    if name == HOLDOUT_TOOL or name.endswith("__holdout_check"):
        return "holdout_check is blocked: only the orchestrator spends holdout checks."
    if name == START_RUN_TOOL:
        return "start_run is blocked: the orchestrator owns the research run and its budget."
    if ORDER_PATTERN.search(name):
        return f"{name} is blocked: this project never places, cancels, or modifies orders."
    return None


def _deny(input_data: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": input_data.get("hook_event_name", "PreToolUse"),
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


async def deny_unsafe_tools(
    input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
) -> dict[str, Any]:
    """SDK ``PreToolUse`` hook: deny edits, shell, holdout, start_run, and order/withdraw tools."""
    reason = unsafe_tool_reason(str(input_data.get("tool_name", "")))
    return _deny(input_data, reason) if reason else {}


HookCallback = Callable[[Mapping[str, Any], str | None, Any], Any]


def make_allowlist_hook(allowed: frozenset[str] | set[str]) -> HookCallback:
    """SDK ``PreToolUse`` hook denying any tool not in ``allowed`` (the role's tool list)."""
    allowed_set = frozenset(allowed)

    async def allowlist_hook(
        input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        name = str(input_data.get("tool_name", ""))
        if name in allowed_set:
            return {}
        return _deny(input_data, f"{name} is not in this research role's tool list.")

    return allowlist_hook


# --------------------------------------------------------------------------- output parsing

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
MAX_PARSE_CHARS = 200_000


def parse_last_json_object(text: str | None) -> dict[str, Any] | None:
    """The last JSON object in ``text``: the last parseable fenced block, else the last
    top-level bare ``{...}`` object. None when there is none."""
    if not text:
        return None
    s = text[-MAX_PARSE_CHARS:]
    for block in reversed(_FENCE.findall(s)):
        try:
            obj = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    decoder = json.JSONDecoder()
    last: dict[str, Any] | None = None
    i = 0
    while True:
        i = s.find("{", i)
        if i < 0:
            return last
        try:
            obj, end = decoder.raw_decode(s, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            last = obj
        i = end


def extract_output(
    structured: Any, text: str | None, schema: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    """``(output, errors)``: structured output if present, else the last JSON object in text."""
    candidate = structured if isinstance(structured, dict) else parse_last_json_object(text)
    if candidate is None:
        return None, ["no JSON object found"]
    errors = validate_output(candidate, schema)
    return (None, errors) if errors else (candidate, [])


# --------------------------------------------------------------------------- runner


@dataclass(frozen=True)
class RoleModels:
    """Model alias per role. PLAN cost note: strong model for Designer/Critic."""

    scout: str = "sonnet"
    designer: str = "opus"
    critic: str = "opus"

    def for_role(self, role: Role) -> str:
        return str(getattr(self, role))


DEFAULT_MAX_TURNS: dict[str, int] = {"scout": 30, "designer": 40, "critic": 15}


def load_role_prompt(role: Role, prompts_dir: Path = PROMPTS_DIR) -> str:
    p = Path(prompts_dir) / f"{role}.md"
    if not p.is_file():
        raise FileNotFoundError(f"role prompt {p} is missing")
    return p.read_text(encoding="utf-8")


def role_tools(role: Role) -> tuple[str, ...]:
    """Every tool the role may call (built-in + MCP)."""
    if role == "scout":
        return SCOUT_TOOLS
    if role == "designer":
        return DESIGNER_LAB_TOOLS
    return CRITIC_LAB_TOOLS


def role_disallowed(role: Role) -> list[str]:
    extra: list[str] = []
    if role != "scout":
        extra += list(SCOUT_TOOLS)
    if role == "critic":
        extra += [t for t in DESIGNER_LAB_TOOLS if t not in CRITIC_LAB_TOOLS]
    return list(dict.fromkeys([*BASE_DISALLOWED, *extra]))


@dataclass
class _Attempt:
    text: str | None = None
    structured: Any = None
    cost: float = 0.0
    session_id: str | None = None
    error: str | None = None
    started: bool = False  # the stream yielded at least one message
    cost_estimated: bool = False  # cost unknown; charged at the attempt's cap


def _known_cost(value: Any) -> float | None:
    """``value`` as a finite cost >= 0, or None when the SDK did not report a usable cost."""
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= 0 else None


def _unknown_charge(attempt_cap: float | None, remaining_usd: float | None) -> float:
    """What an attempt with an unknown cost is charged: its cap, else the run's remaining
    budget. Both missing means the caller gave no limit at all; that is logged and charged 0
    (the orchestrator always passes both)."""
    for x in (attempt_cap, remaining_usd):
        known = _known_cost(x)
        if known is not None:
            return known
    logger.warning("model call cost unknown and no cap or remaining budget given; charged 0")
    return 0.0


QueryFn = Callable[..., AsyncIterator[Any]]


@dataclass
class SdkAgentRunner:
    """:class:`research.orchestrator.AgentRunner` backed by ``claude_agent_sdk.query``."""

    repo_root: Path = REPO_ROOT
    prompts_dir: Path = PROMPTS_DIR
    models: RoleModels = field(default_factory=RoleModels)
    max_turns: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_MAX_TURNS))
    load_project_settings: bool = True
    query_fn: QueryFn | None = None  # injectable for tests; defaults to claude_agent_sdk.query

    def mcp_servers(self, role: Role, run_id: str) -> dict[str, Any]:
        if role == "scout":
            return {}
        return {
            MCP_SERVER_NAME: {
                "type": "stdio",
                "command": "uv",
                "args": [
                    "--directory",
                    str(Path(self.repo_root) / "lab"),
                    "run",
                    "python",
                    "-m",
                    "lab.mcp_server",
                ],
                "env": {"STRATEGY_LAB_RUN_ID": run_id, "STRATEGY_LAB_DISABLE_HOLDOUT": "1"},
            }
        }

    def agent_definition(self, role: Role) -> AgentDefinition:
        """The role as an SDK subagent definition (for callers that prefer ``agents=``)."""
        return AgentDefinition(
            description=f"tradingView research {role}",
            prompt=load_role_prompt(role, self.prompts_dir),
            tools=list(role_tools(role)),
            disallowedTools=role_disallowed(role),
            model=self.models.for_role(role),
            maxTurns=self.max_turns.get(role),
        )

    def build_options(
        self,
        req: RoleRequest,
        *,
        resume: str | None = None,
        max_budget_usd: float | None = None,
    ) -> ClaudeAgentOptions:
        role = req.role
        tools = list(role_tools(role))
        builtin = [t for t in tools if not t.startswith("mcp__")]
        schema = req.output_schema or ROLE_SCHEMAS[role]
        system = (
            load_role_prompt(role, self.prompts_dir)
            + "\n\n## Output contract\nEnd with ONE JSON object matching this JSON Schema:\n"
            + json.dumps(schema, sort_keys=True)
        )
        return ClaudeAgentOptions(
            tools=builtin,
            allowed_tools=tools,
            disallowed_tools=role_disallowed(role),
            mcp_servers=self.mcp_servers(role, req.run_id),
            strict_mcp_config=True,
            permission_mode="dontAsk",
            system_prompt=system,
            model=self.models.for_role(role),
            max_turns=self.max_turns.get(role),
            max_budget_usd=max_budget_usd if max_budget_usd is not None else req.max_budget_usd,
            cwd=str(self.repo_root),
            setting_sources=["project"] if self.load_project_settings else [],
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher=None,
                        hooks=[deny_unsafe_tools, make_allowlist_hook(frozenset(tools))],
                    )
                ]
            },
            output_format={"type": "json_schema", "schema": schema},
            resume=resume,
        )

    async def _attempt(
        self, prompt: str, options: ClaudeAgentOptions, *, unknown_charge: float
    ) -> _Attempt:
        """One SDK session. ``unknown_charge`` is what the attempt costs when the SDK does not
        report a cost after starting (module docstring: dollars fail closed)."""
        q = self.query_fn or query
        at = _Attempt()
        last_text: list[str] = []
        got_result = False
        try:
            async for msg in q(prompt=prompt, options=options):
                at.started = True
                if isinstance(msg, AssistantMessage):
                    texts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                    if texts:
                        last_text = texts
                elif isinstance(msg, ResultMessage):
                    got_result = True
                    cost = _known_cost(msg.total_cost_usd)
                    at.cost = unknown_charge if cost is None else cost
                    at.cost_estimated = cost is None
                    at.session_id = msg.session_id or None
                    at.text = msg.result if msg.result is not None else "\n".join(last_text)
                    at.structured = msg.structured_output
                    if msg.is_error or msg.subtype != "success":
                        detail = "; ".join(msg.errors or [])[:300]
                        at.error = f"sdk result {msg.subtype}" + (f": {detail}" if detail else "")
        except Exception as exc:  # SDK/CLI failures become a role error, not a crash
            at.error = f"sdk error: {type(exc).__name__}: {str(exc)[:300]}"
        if at.started and not got_result:
            # The session ran (and may have spent money) but never reported its cost.
            at.cost = unknown_charge
            at.cost_estimated = True
            at.error = at.error or "sdk stream ended without a result message"
        if at.cost_estimated:
            logger.warning("model call cost unknown; charged its cap $%.4f", at.cost)
        if at.text is None and last_text:
            at.text = "\n".join(last_text)
        return at

    async def run(self, req: RoleRequest) -> RoleResult:
        schema = req.output_schema or ROLE_SCHEMAS[req.role]
        cap = req.max_budget_usd
        first = await self._attempt(
            req.prompt,
            self.build_options(req),
            unknown_charge=_unknown_charge(cap, req.remaining_usd),
        )
        est = first.cost_estimated

        def result(
            out: dict[str, Any] | None, cost: float, sid: str | None, err: str | None
        ) -> RoleResult:
            return RoleResult(out, cost, sid, err, cost_estimated=est)

        output, errors = extract_output(first.structured, first.text, schema)
        if output is not None:
            return result(output, first.cost, first.session_id, None)
        if first.error and first.text is None:
            return result(None, first.cost, first.session_id, first.error)
        if "max_budget" in (first.error or "") or first.session_id is None:
            return result(None, first.cost, first.session_id, first.error or "; ".join(errors))
        remaining = None if cap is None else cap - first.cost
        if remaining is not None and remaining < MIN_RETRY_USD:
            return result(None, first.cost, first.session_id, "invalid output; no budget to retry")
        run_left = None if req.remaining_usd is None else max(0.0, req.remaining_usd - first.cost)
        retry_opts = self.build_options(req, resume=first.session_id, max_budget_usd=remaining)
        second = await self._attempt(
            RETRY_PROMPT.format(errors="; ".join(errors[:3])),
            retry_opts,
            unknown_charge=_unknown_charge(remaining, run_left),
        )
        est = est or second.cost_estimated
        total = first.cost + second.cost
        sid = second.session_id or first.session_id
        output2, errors2 = extract_output(second.structured, second.text, schema)
        if output2 is not None:
            return result(output2, total, sid, None)
        msg = second.error or "invalid output after one retry: " + "; ".join(errors2[:3])
        return result(None, total, sid, msg)


# --------------------------------------------------------------------------- dry-run runner


@dataclass
class ScriptedAgentRunner:
    """Offline runner for ``research_loop.py --dry-run`` and tests: returns scripted outputs
    per role (a callable gets the request), records every request, costs ``cost_per_call``."""

    scripts: Mapping[str, Any]
    cost_per_call: float = 0.0
    requests: list[RoleRequest] = field(default_factory=list)

    async def run(self, req: RoleRequest) -> RoleResult:
        self.requests.append(req)
        script = self.scripts.get(req.role)
        out = script(req) if callable(script) else script
        if isinstance(out, RoleResult):
            return out
        n = len(self.requests)
        return RoleResult(out, self.cost_per_call, f"scripted-{req.role}-{n}", None)
