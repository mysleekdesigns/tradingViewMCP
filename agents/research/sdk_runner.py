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

Session environment (``ClaudeAgentOptions.env``, merged over the parent environment for the CLI
subprocess, ``types.py`` 2479): :data:`SUBAGENT_ENV` caps subagents explicitly,
``CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1`` and ``CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=2`` (bundled
CLI 2.1.310 defaults: depth 3, 20 concurrent), on top of ``Task``/``Agent`` being disallowed.
``ENABLE_TOOL_SEARCH=false`` keeps every role's tools loaded up front (tool search is on by
default and withholds deferred tool definitions; a withheld lab tool silently degrades a run).
``max_budget_usd`` is the whole session's cap and **counts subagent spend** too (the CLI's
``--max-budget-usd`` halt also stops subagents), so the caps above are about blast radius, not
dollars.

Tool-list check: every attempt reads the SDK ``init`` ``SystemMessage`` (``data["tools"]``,
``types.py`` 1393-1491). It must be a superset of the role's tools (:func:`role_tools`) and must
not list a forbidden tool (:func:`forbidden_init_tools`: the role's disallowed list, edit/shell
tools, ``Task``/``Agent``, and any ``holdout_check``/``start_run``). Otherwise the attempt stops
at once with ``"init tool list missing: ..."`` / ``"init tool list has forbidden tools: ..."``
and no output is accepted; the orchestrator treats either as a run failure
(``research.orchestrator.InitToolsError``), never as an idea verdict. Every check is recorded on
the runner (``init_checks``: role and problems, empty when OK) so the dry run can assert and
print them. The ``PreToolUse`` hooks still deny those tools if they are ever called.

Scout screening: the Scout's ``WebFetch``/``WebSearch`` outputs pass through a ``PostToolUse``
hook (:func:`research.scout_hook.make_scout_screen_hook`) that rewrites them with
``updatedToolOutput`` before the model sees them; the orchestrator's source screen stays as the
second layer.

Output: each role must end with one JSON object matching its schema. The SDK's structured output
(``output_format``) is requested; otherwise the last JSON object in the result text is parsed.
Invalid output gets one retry in the same session; the per-call dollar cap covers both attempts.
``RoleResult.invalid_outputs`` counts the attempts whose final output failed the schema (0-2),
so the orchestrator can charge them to the proposal budget.

Dollars fail closed: when an attempt's cost is unknown (``ResultMessage.total_cost_usd`` is
None, or the stream raised or ended after it started without a result) the attempt is charged
its full ``max_budget_usd`` cap (or, with no cap, the run's remaining budget,
``RoleRequest.remaining_usd``), never zero. That covers **every** attempt that started (the
stream yielded a message) and got no ``ResultMessage``, init tool-list aborts included: the CLI
may already have sent its first billed request when Python reads ``init`` and closes the stream.
A retry attempt is charged the same way. Only an attempt that fails before the CLI yields any
message (e.g. the CLI is not installed) spent nothing.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import re
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    query,
)

from lab.research.contract import AGENTS_DIR, REPO_ROOT
from research.jev import JevClient
from research.orchestrator import (
    INIT_FORBIDDEN_ERROR,
    INIT_MISSING_ERROR,
    ROLE_SCHEMAS,
    Role,
    RoleRequest,
    RoleResult,
    validate_output,
)
from research.scout_hook import (
    SCOUT_HOOK_MATCHER,
    SCOUT_HOOK_TIMEOUT_S,
    HookCallback,
    make_scout_screen_hook,
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
# Explicit subagent caps for every role (module docstring). Read-only so a run cannot loosen them.
SUBAGENT_ENV: Mapping[str, str] = MappingProxyType(
    {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1", "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "2"}
)
# Tool search off: the Designer has 10 lab tools and the Critic 3; none may be withheld. The
# Scout gets it too: WebSearch is a deferred tool under tool search, and its init check must see it.
TOOL_SEARCH_ENV: Mapping[str, str] = MappingProxyType({"ENABLE_TOOL_SEARCH": "false"})
# ResultMessage subtype when the CLI's own structured-output retries (MAX_STRUCTURED_OUTPUT_RETRIES)
# all failed the schema.
SDK_RETRIES_EXHAUSTED = "error_max_structured_output_retries"


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


def role_env(role: Role) -> dict[str, str]:
    """The CLI subprocess environment overrides for ``role`` (module docstring)."""
    return {**SUBAGENT_ENV, **TOOL_SEARCH_ENV}


def missing_init_tools(role: Role, init_tools: Sequence[str]) -> list[str]:
    """The role's tools (:func:`role_tools`) absent from the SDK ``init`` tool list."""
    listed = set(init_tools)
    return [t for t in role_tools(role) if t not in listed]


# Forbidden in every role's init tool list whatever the role's disallowed list says.
ALWAYS_FORBIDDEN_INIT: frozenset[str] = frozenset(
    {"Bash", "Edit", "Write", "Task", "Agent", HOLDOUT_TOOL, START_RUN_TOOL}
)


def forbidden_init_tools(role: Role, init_tools: Sequence[str]) -> list[str]:
    """Tools in the SDK ``init`` list that the role must never have: anything in
    :func:`role_disallowed`, Bash/Edit/Write/Task/Agent, any server's ``holdout_check`` or
    ``start_run``, and anything :func:`unsafe_tool_reason` forbids (edits, shell, orders).
    A role's own tools are never forbidden. Order of the init list, no duplicates."""
    own = set(role_tools(role))
    blocked = set(role_disallowed(role)) | ALWAYS_FORBIDDEN_INIT
    out: list[str] = []
    for name in dict.fromkeys(str(t) for t in init_tools):
        if name in own:
            continue
        if (
            name in blocked
            or name.endswith(("__holdout_check", "__start_run"))
            or unsafe_tool_reason(name) is not None
        ):
            out.append(name)
    return out


def init_tool_problems(role: Role, init_tools: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(missing, forbidden)`` for the role's SDK ``init`` tool list (both empty = OK)."""
    return missing_init_tools(role, init_tools), forbidden_init_tools(role, init_tools)


def init_check_error(missing: Sequence[str], forbidden: Sequence[str]) -> str | None:
    """The attempt error for a failed init check (None when it passed). Starts with
    :data:`INIT_MISSING_ERROR` or :data:`INIT_FORBIDDEN_ERROR`, which the orchestrator turns
    into a run failure."""
    parts = []
    if missing:
        parts.append(f"{INIT_MISSING_ERROR}: {', '.join(missing)}")
    if forbidden:
        parts.append(f"{INIT_FORBIDDEN_ERROR}: {', '.join(forbidden)}")
    return "; ".join(parts) or None


def init_check_record(missing: Sequence[str], forbidden: Sequence[str]) -> list[str]:
    """What ``init_checks`` records per check: missing tool names, then ``"forbidden <name>"``
    for each forbidden one (empty = the init list passed)."""
    return [*missing, *(f"forbidden {t}" for t in forbidden)]


def _no_candles(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise RuntimeError("tool-name introspection never loads candles")


def _run_coro_in_thread(make: Callable[[], Any]) -> Any:
    """Run ``asyncio.run(make())`` on a fresh thread: works inside a running event loop too."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(make())
        except BaseException as exc:  # re-raised on the caller's thread
            box["error"] = exc

    t = threading.Thread(target=target, name="lab-tool-names")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


@functools.cache
def lab_server_tool_names() -> tuple[str, ...]:
    """The tools the real ``lab.mcp_server`` registers, prefixed ``mcp__strategy-lab__``.

    Builds the server with :func:`lab.mcp_server.build_server` (the repo's ``lab.toml``, a
    ledger path in a temporary directory that is never opened, and a loader that is never
    called), lists its tools, and never starts stdio or touches the real ledger. Cached."""
    from lab.mcp_server import build_server

    with tempfile.TemporaryDirectory(prefix="lab-tool-names-") as tmp:
        server = build_server(ledger_path=Path(tmp) / "ledger.db", loader=_no_candles)
        tools = _run_coro_in_thread(server.list_tools)
    return tuple(f"{MCP_PREFIX}{t.name}" for t in tools)


def expected_init_tools(role: Role) -> list[str]:
    """What a real ``init`` message lists for ``role``: the role's built-in tools plus, for the
    MCP roles, every tool the lab server registers minus the role's disallowed tools (the CLI
    removes those from context). Used by :class:`ScriptedAgentRunner` for the dry run."""
    builtin = [t for t in role_tools(role) if not t.startswith("mcp__")]
    if role == "scout":
        return builtin
    blocked = set(role_disallowed(role))
    return builtin + [t for t in lab_server_tool_names() if t not in blocked]


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
    init_failed: bool = False  # the init tool list lacked a role tool or listed a forbidden one


def _invalid_output(at: _Attempt) -> bool:
    """The attempt reached a final output that failed the schema (counts toward the proposal
    budget): a clean result, or the SDK's own structured-output retries exhausted. Budget stops,
    SDK errors, and init failures are not invalid outputs."""
    if at.init_failed:
        return False
    if at.error is None:
        return True
    return SDK_RETRIES_EXHAUSTED in at.error


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


async def _close_stream(stream: Any) -> None:
    """Close an SDK stream left early (the SDK's generator cleanup ends the CLI process)."""
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as exc:  # cleanup only; the attempt already has its error
        logger.warning("closing the SDK stream failed: %s", type(exc).__name__)


@dataclass
class SdkAgentRunner:
    """:class:`research.orchestrator.AgentRunner` backed by ``claude_agent_sdk.query``."""

    repo_root: Path = REPO_ROOT
    prompts_dir: Path = PROMPTS_DIR
    models: RoleModels = field(default_factory=RoleModels)
    max_turns: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_MAX_TURNS))
    load_project_settings: bool = True
    query_fn: QueryFn | None = None  # injectable for tests; defaults to claude_agent_sdk.query
    jev: JevClient | None = None  # Scout PostToolUse screen; None = heuristics only (fail-open)
    # (role, problems): see :func:`init_check_record`; empty problems = the init list passed.
    init_checks: list[tuple[str, list[str]]] = field(default_factory=list)

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
        hooks: dict[str, list[HookMatcher]] = {
            "PreToolUse": [
                HookMatcher(
                    matcher=None,
                    hooks=[deny_unsafe_tools, make_allowlist_hook(frozenset(tools))],
                )
            ]
        }
        if role == "scout":
            hooks["PostToolUse"] = [
                HookMatcher(
                    matcher=SCOUT_HOOK_MATCHER,
                    hooks=[make_scout_screen_hook(self.jev)],
                    timeout=SCOUT_HOOK_TIMEOUT_S,
                )
            ]
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
            hooks=hooks,  # type: ignore[arg-type]
            env=role_env(role),
            output_format={"type": "json_schema", "schema": schema},
            resume=resume,
        )

    async def _attempt(
        self,
        prompt: str,
        options: ClaudeAgentOptions,
        *,
        role: Role,
        unknown_charge: float,
    ) -> _Attempt:
        """One SDK session. ``unknown_charge`` is what the attempt costs when the SDK does not
        report a cost after starting (module docstring: dollars fail closed).

        Every ``init`` message is checked (missing role tools, forbidden tools) and recorded in
        ``init_checks``; a failed check stops the session at once with an error. Any attempt
        that started and got no ``ResultMessage`` (an init abort included: the CLI may already
        have sent a billed request) is charged ``unknown_charge`` with ``cost_estimated``."""
        q = self.query_fn or query
        at = _Attempt()
        last_text: list[str] = []
        got_result = False
        stream = q(prompt=prompt, options=options)
        try:
            async for msg in stream:
                at.started = True
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    listed = msg.data.get("tools") if isinstance(msg.data, Mapping) else None
                    names = [str(t) for t in listed] if isinstance(listed, list) else []
                    missing, forbidden = init_tool_problems(role, names)
                    self.init_checks.append((role, init_check_record(missing, forbidden)))
                    err = init_check_error(missing, forbidden)
                    if err is not None:
                        at.init_failed = True
                        at.error = err
                        logger.error("%s session %s", role, at.error)
                        break
                elif isinstance(msg, AssistantMessage):
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
        if at.init_failed:
            await _close_stream(stream)
        if at.started and not got_result:
            # The session ran (and may have spent money) but never reported its cost.
            at.cost = unknown_charge
            at.cost_estimated = True
            at.error = at.error or "sdk stream ended without a result message"
        if at.cost_estimated:
            logger.warning("model call cost unknown; charged its cap $%.4f", at.cost)
        if at.init_failed:
            at.text, at.structured = None, None  # no output is accepted after a failed check
        elif at.text is None and last_text:
            at.text = "\n".join(last_text)
        return at

    async def run(self, req: RoleRequest) -> RoleResult:
        schema = req.output_schema or ROLE_SCHEMAS[req.role]
        cap = req.max_budget_usd
        first = await self._attempt(
            req.prompt,
            self.build_options(req),
            role=req.role,
            unknown_charge=_unknown_charge(cap, req.remaining_usd),
        )
        est = first.cost_estimated
        invalid = 0

        def result(
            out: dict[str, Any] | None, cost: float, sid: str | None, err: str | None
        ) -> RoleResult:
            return RoleResult(out, cost, sid, err, cost_estimated=est, invalid_outputs=invalid)

        if first.init_failed:
            return result(None, first.cost, first.session_id, first.error)
        output, errors = extract_output(first.structured, first.text, schema)
        if output is not None:
            return result(output, first.cost, first.session_id, None)
        invalid = 1 if _invalid_output(first) else 0
        if first.error and first.text is None:
            return result(None, first.cost, first.session_id, first.error)
        # No retry after a budget stop, without a session to resume, or once the SDK has
        # already spent its own structured-output retries (another one would only add cost).
        stop = first.error or ""
        if "max_budget" in stop or SDK_RETRIES_EXHAUSTED in stop or first.session_id is None:
            return result(None, first.cost, first.session_id, first.error or "; ".join(errors))
        remaining = None if cap is None else cap - first.cost
        if remaining is not None and remaining < MIN_RETRY_USD:
            return result(None, first.cost, first.session_id, "invalid output; no budget to retry")
        run_left = None if req.remaining_usd is None else max(0.0, req.remaining_usd - first.cost)
        retry_opts = self.build_options(req, resume=first.session_id, max_budget_usd=remaining)
        second = await self._attempt(
            RETRY_PROMPT.format(errors="; ".join(errors[:3])),
            retry_opts,
            role=req.role,
            unknown_charge=_unknown_charge(remaining, run_left),
        )
        est = est or second.cost_estimated
        total = first.cost + second.cost
        sid = second.session_id or first.session_id
        if second.init_failed:
            return result(None, total, sid, second.error)
        output2, errors2 = extract_output(second.structured, second.text, schema)
        if output2 is not None:
            return result(output2, total, sid, None)
        invalid += 1 if _invalid_output(second) else 0
        msg = second.error or "invalid output after one retry: " + "; ".join(errors2[:3])
        return result(None, total, sid, msg)


# --------------------------------------------------------------------------- dry-run runner


@dataclass
class ScriptedAgentRunner:
    """Offline runner for ``research_loop.py --dry-run`` and tests: returns scripted outputs
    per role (a callable gets the request), records every request, costs ``cost_per_call``.

    Like :class:`SdkAgentRunner` it checks a simulated ``init`` tool list on every call
    (``init_tools(role)``; default :func:`expected_init_tools`, i.e. what the real lab server
    registers), records ``(role, problems)`` in ``init_checks``, and returns an error result
    when a role tool is missing or a forbidden tool is listed (charged ``cost_per_call`` as an
    estimated cost, as the SDK runner charges an init abort)."""

    scripts: Mapping[str, Any]
    cost_per_call: float = 0.0
    requests: list[RoleRequest] = field(default_factory=list)
    init_tools: Callable[[Role], Sequence[str]] | None = None
    init_checks: list[tuple[str, list[str]]] = field(default_factory=list)

    async def run(self, req: RoleRequest) -> RoleResult:
        self.requests.append(req)
        listed = (self.init_tools or expected_init_tools)(req.role)
        missing, forbidden = init_tool_problems(req.role, listed)
        self.init_checks.append((req.role, init_check_record(missing, forbidden)))
        err = init_check_error(missing, forbidden)
        if err is not None:
            return RoleResult(None, self.cost_per_call, None, err, cost_estimated=True)
        script = self.scripts.get(req.role)
        out = script(req) if callable(script) else script
        if isinstance(out, RoleResult):
            return out
        n = len(self.requests)
        return RoleResult(out, self.cost_per_call, f"scripted-{req.role}-{n}", None)
