"""PLAN.md Phase 8: "Lab code and success criteria protected from agent edits (permissions/hooks)".

Two layers are proven here:

1. The SDK-side hook every research role gets (:func:`research.sdk_runner.deny_unsafe_tools`)
   denies file edits, the shell, the holdout tool, start_run, and order/withdraw tools, while
   read-only lab tools pass.
2. The project's own Claude Code hooks (``.claude/hooks/protect-files.sh`` and ``guard-bash.sh``,
   loaded by SDK roles through ``setting_sources=["project"]``) block edits to the engine,
   registry, stats, and success criteria while ``.claude/lab.lock`` exists. The hook scripts are
   copied unmodified into a temp project and run as subprocesses with fake PreToolUse JSON.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from lab.research.contract import REPO_ROOT
from research.orchestrator import DESIGNER_SCHEMA, RoleRequest
from research.sdk_runner import SdkAgentRunner, deny_unsafe_tools, unsafe_tool_reason

HOOKS = REPO_ROOT / ".claude" / "hooks"
LOCKED_PATHS = (
    "lab/lab/engine/backtest.py",
    "lab/lab/engine/splits.py",  # Phase 11: walk-forward folds and CPCV splits
    "lab/lab/registry/features.py",
    "lab/lab/stats/sharpe.py",
    "lab/lab/stats/cpcv.py",  # Phase 11: CPCV path Sharpes
    "lab/lab/stats/effective_n.py",  # Phase 11: effective number of trials
    "lab/config/success_criteria.toml",
)


def _needs_tools() -> None:
    # Environment precondition only: the hooks themselves are bash + jq scripts.
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required to run the project hook scripts")


# --------------------------------------------------------------------------- SDK-side hook


async def _decide(tool: str) -> dict[str, Any]:
    return await deny_unsafe_tools(
        {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}, "id", None
    )


@pytest.mark.parametrize(
    "tool",
    [
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
        "Bash",
        "mcp__strategy-lab__holdout_check",
        "mcp__strategy-lab__start_run",
        "mcp__binance__place_order",
        "mcp__exchange__cancelOrder",
        "mcp__x__create_trade",
        "mcp__x__close_position",
        "mcp__wallet__withdraw",
        "mcp__x__submit_order",
    ],
)
async def test_sdk_hook_denies_unsafe_tools(tool: str) -> None:
    out = await _decide(tool)
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecisionReason"]
    assert unsafe_tool_reason(tool) is not None


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__strategy-lab__get_report",
        "mcp__strategy-lab__get_ledger",
        "mcp__strategy-lab__get_budget",
        "mcp__strategy-lab__list_features",
        "mcp__strategy-lab__validate_plan",
        "mcp__strategy-lab__run_backtest",
        "mcp__strategy-lab__run_sweep",
        "mcp__strategy-lab__walk_forward",
        "mcp__strategy-lab__run_adversarial",
        "WebSearch",
        "WebFetch",
    ],
)
async def test_sdk_hook_allows_lab_and_web_read_tools(tool: str) -> None:
    assert await _decide(tool) == {}


def test_sdk_roles_load_project_hooks_from_the_repo_root() -> None:
    o = SdkAgentRunner().build_options(
        RoleRequest(
            role="designer",
            prompt="p",
            run_id="r-20261024-120000-abc123",
            output_schema=DESIGNER_SCHEMA,
        )
    )
    assert o.setting_sources == ["project"]
    assert Path(str(o.cwd)) == REPO_ROOT
    for t in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"):
        assert t in o.disallowed_tools


def test_project_settings_register_the_protection_hooks() -> None:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    pre = settings["hooks"]["PreToolUse"]
    by_script = {
        Path(h["args"][0]).name: m["matcher"] for m in pre for h in m["hooks"] if h.get("args")
    }
    assert by_script["protect-files.sh"] == "Edit|Write|MultiEdit|NotebookEdit"
    assert by_script["guard-bash.sh"] == "Bash"
    assert "withdraw" in by_script["block-order-tools.sh"]
    assert by_script["confirm-holdout.sh"] == "mcp__strategy-lab__holdout_check"


# --------------------------------------------------------------------------- project hooks


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A temp project with unmodified copies of the hook scripts."""
    _needs_tools()
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("protect-files.sh", "guard-bash.sh", "block-order-tools.sh"):
        shutil.copy2(HOOKS / name, hooks / name)
        assert (hooks / name).read_bytes() == (HOOKS / name).read_bytes()
    return tmp_path


def _lock(root: Path) -> None:
    (root / ".claude" / "lab.lock").touch()


def _run_hook(root: Path, script: str, payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CLAUDE_PROJECT_DIR": str(root)}
    return subprocess.run(
        ["bash", str(root / ".claude" / "hooks" / script)],
        input=json.dumps({"hook_event_name": "PreToolUse", **payload}),
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        timeout=30,
        check=False,
    )


def _edit(root: Path, rel: str, tool: str = "Edit") -> subprocess.CompletedProcess[str]:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return _run_hook(
        root, "protect-files.sh", {"tool_name": tool, "tool_input": {key: str(root / rel)}}
    )


@pytest.mark.parametrize("rel", LOCKED_PATHS)
@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
def test_protect_files_blocks_lab_edits_under_the_lock(project: Path, rel: str, tool: str) -> None:
    _lock(project)
    res = _edit(project, rel, tool)
    assert res.returncode == 2, res.stderr
    assert "read-only while the lab is locked" in res.stderr


def test_protect_files_blocks_relative_paths_under_the_lock(project: Path) -> None:
    _lock(project)
    res = _run_hook(
        project,
        "protect-files.sh",
        {"tool_name": "Write", "tool_input": {"file_path": "lab/lab/stats/dsr.py"}},
    )
    assert res.returncode == 2


@pytest.mark.parametrize("rel", LOCKED_PATHS)
def test_protect_files_allows_lab_edits_without_the_lock(project: Path, rel: str) -> None:
    res = _edit(project, rel)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == ""


def test_protect_files_allows_agents_code_under_the_lock(project: Path) -> None:
    _lock(project)
    assert _edit(project, "agents/research/orchestrator.py").returncode == 0


def test_protect_files_blocks_the_lock_itself(project: Path) -> None:
    res = _edit(project, ".claude/lab.lock", "Write")
    assert res.returncode == 2
    assert "owner only" in res.stderr


def _bash(root: Path, command: str) -> dict[str, Any] | None:
    res = _run_hook(
        root, "guard-bash.sh", {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout) if res.stdout.strip() else None


@pytest.mark.parametrize(
    "command",
    [
        "sed -i '' 's/0.95/0.5/' lab/lab/engine/x.py",
        "echo 'dsr_min = 0' > lab/config/success_criteria.toml",
        "cp /tmp/evil.py lab/lab/registry/features.py",
        "git checkout HEAD~5 -- lab/lab/stats/sharpe.py",
    ],
)
def test_guard_bash_denies_lab_writes_under_the_lock(project: Path, command: str) -> None:
    _lock(project)
    decision = _bash(project, command)
    assert decision is not None
    spec = decision["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "locked" in spec["permissionDecisionReason"]


def test_guard_bash_allows_the_same_command_without_the_lock(project: Path) -> None:
    assert _bash(project, "sed -i '' 's/a/b/' lab/lab/engine/x.py") is None


def test_guard_bash_allows_reading_lab_files_under_the_lock(project: Path) -> None:
    _lock(project)
    assert _bash(project, "cat lab/lab/engine/x.py") is None


def test_guard_bash_denies_removing_the_lock(project: Path) -> None:
    decision = _bash(project, "rm .claude/lab.lock")
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_block_order_tools_always_denies(project: Path) -> None:
    res = _run_hook(
        project,
        "block-order-tools.sh",
        {"tool_name": "mcp__binance__place_order", "tool_input": {}},
    )
    assert res.returncode == 0
    spec = json.loads(res.stdout)["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "mcp__binance__place_order" in spec["permissionDecisionReason"]
