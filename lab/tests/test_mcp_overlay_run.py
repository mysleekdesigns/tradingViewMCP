"""strategy-lab MCP server, PLAN.md Phase 15 question plumbing (offline, in-process client, tmp
ledger, tmp copies of the proposed criteria files): ``start_run(label, question=...)``, the
missing-file refusal, the question gate on every run-scoped tool (before the plan is parsed),
run_sweep / walk_forward refused in overlay runs before any criteria are read, and get_budget /
start_run / validate_plan output for edge runs unchanged."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from lab.mcp_server import INSTRUCTIONS, _overlay_schema
from tests.test_mcp_server import (
    EMA_PLAN,
    SPACE,
    WF_SPACE,
    Lab,
    call,
    counts,
    error_text,
    ok,
)
from tests.test_research_overlay_contract import (
    OVERLAY_PLAN,
    PORTFOLIO_RAW,
    SPEC_BUDGET,
    SPEC_OVERLAY,
    write_overlay,
)

INSTALL = "cp agents/config/overlay_criteria.proposed.toml lab/config/overlay_criteria.toml"
EDGE_START_KEYS = {
    "run_id",
    "criteria",
    "budget",
    "holdout_budget",
    "config_sha256",
    "derivatives_manifest_sha256",
}


def overlay_lab(tmp_path: Path, **overrides: object) -> Lab:
    path = write_overlay(tmp_path / "overlay.toml", **overrides)
    return Lab(tmp_path, overlay_config_path=path)


def start_overlay(lab: Lab, label: str = "overlay") -> str:
    out = ok(lab.server, "start_run", {"label": label, "question": "risk_overlay"})
    return str(out["run_id"])


# --------------------------------------------------------------------------- start_run


def test_start_run_overlay_freezes_question_rows_and_file_sha(tmp_path: Path) -> None:
    lab = overlay_lab(tmp_path)
    out = ok(lab.server, "start_run", {"label": "ov", "question": "risk_overlay"})
    sha = hashlib.sha256((tmp_path / "overlay.toml").read_bytes()).hexdigest()
    assert out["question"] == "risk_overlay"
    assert out["overlay_criteria"] == SPEC_OVERLAY
    assert out["budget"] == SPEC_BUDGET
    assert out["holdout_budget"] == 1  # min(frozen 1, lab.toml 3)
    assert out["config_sha256"] == sha
    assert "criteria" not in out
    with lab.ledger() as led:
        run = led.research_run(out["run_id"])
    assert run is not None
    assert run["config_sha256"] == sha
    assert run["config"]["question"] == "risk_overlay"
    assert run["config"]["overlay_criteria"] == SPEC_OVERLAY
    assert "criteria" not in run["config"]


def test_start_run_overlay_without_the_file_names_the_owner_install(tmp_path: Path) -> None:
    lab = Lab(tmp_path, overlay_config_path=tmp_path / "missing.toml")
    msg = error_text(lab.server, "start_run", {"label": "x", "question": "risk_overlay"})
    assert "cannot start a risk-overlay run" in msg and "not found" in msg
    assert INSTALL in msg
    with lab.ledger() as led:
        assert led.research_runs() == []


def test_start_run_overlay_with_a_malformed_file_is_refused(tmp_path: Path) -> None:
    lab = overlay_lab(tmp_path, alpha=1.5)
    msg = error_text(lab.server, "start_run", {"label": "x", "question": "risk_overlay"})
    assert "alpha" in msg and "out of range" in msg
    with lab.ledger() as led:
        assert led.research_runs() == []


def test_start_run_rejects_an_unknown_question(tmp_path: Path) -> None:
    lab = overlay_lab(tmp_path)
    msg = error_text(lab.server, "start_run", {"label": "x", "question": "alpha"})
    assert "question must be one of" in msg and "'risk_overlay'" in msg
    with lab.ledger() as led:
        assert led.research_runs() == []


def test_start_run_edge_default_is_unchanged(tmp_path: Path) -> None:
    """No question (or question="edge") gives the pre-Phase 15 output and frozen config, even
    when no overlay file exists."""
    lab = Lab(tmp_path, overlay_config_path=tmp_path / "missing.toml")
    default = ok(lab.server, "start_run", {"label": "a"})
    explicit = ok(lab.server, "start_run", {"label": "b", "question": "edge"})
    for out in (default, explicit):
        assert set(out) == EDGE_START_KEYS
        with lab.ledger() as led:
            run = led.research_run(out["run_id"])
        assert run is not None
        assert "question" not in run["config"] and "overlay_criteria" not in run["config"]
        assert set(run["config"]) - {"derivatives", "universe_manifest"} == {
            "criteria",
            "budget",
            "lab",
            "software",
        }


def test_start_run_schema_has_question_defaulting_to_edge(tmp_path: Path) -> None:
    lab = Lab(tmp_path)

    async def go() -> Any:
        async with Client(lab.server, raise_exceptions=True) as c:
            return {t.name: t for t in (await c.list_tools()).tools}

    tool = anyio.run(go)["start_run"]
    props = tool.input_schema["properties"]
    assert props["question"]["default"] == "edge"
    assert tool.input_schema.get("required") == ["label"]


def test_pinned_server_refuses_an_overlay_start_too(tmp_path: Path) -> None:
    lab = Lab(
        tmp_path,
        overlay_config_path=write_overlay(tmp_path / "o.toml"),
        pinned_run_id="r-20261029-000000-abcdef",
    )
    msg = error_text(lab.server, "start_run", {"label": "x", "question": "risk_overlay"})
    assert "pinned" in msg


# --------------------------------------------------------------------------- question gate

RUN_TOOLS = ("run_backtest", "run_adversarial", "run_crosscheck")


@pytest.mark.parametrize("tool", RUN_TOOLS)
def test_overlay_plan_in_an_edge_run_is_refused_before_parsing(tmp_path: Path, tool: str) -> None:
    lab = overlay_lab(tmp_path)
    run_id = lab.start()
    msg = error_text(lab.server, tool, {"run_id": run_id, "plan": OVERLAY_PLAN})
    assert "asks the edge question" in msg and 'question="risk_overlay"' in msg
    assert "plan is invalid" not in msg
    assert counts(lab, run_id) == (0, 0, 0)
    assert lab.calls == []  # nothing loaded


@pytest.mark.parametrize("tool", RUN_TOOLS)
@pytest.mark.parametrize("plan", [EMA_PLAN, PORTFOLIO_RAW])
def test_other_plans_in_an_overlay_run_are_refused(
    tmp_path: Path, tool: str, plan: dict[str, Any]
) -> None:
    lab = overlay_lab(tmp_path)
    run_id = start_overlay(lab)
    msg = error_text(lab.server, tool, {"run_id": run_id, "plan": plan})
    assert "asks the risk-overlay question" in msg
    assert counts(lab, run_id) == (0, 0, 0)
    assert lab.calls == []


def test_holdout_check_gates_the_question_without_spending(tmp_path: Path) -> None:
    lab = overlay_lab(tmp_path)
    edge = lab.start()
    overlay = start_overlay(lab)
    msg = error_text(lab.server, "holdout_check", {"run_id": edge, "plan": OVERLAY_PLAN})
    assert "asks the edge question" in msg
    msg = error_text(lab.server, "holdout_check", {"run_id": overlay, "plan": EMA_PLAN})
    assert "asks the risk-overlay question" in msg
    assert counts(lab, edge) == (0, 0, 0) and counts(lab, overlay) == (0, 0, 0)


@pytest.mark.parametrize("plan", [OVERLAY_PLAN, EMA_PLAN])
def test_run_sweep_is_refused_in_an_overlay_run(tmp_path: Path, plan: dict[str, Any]) -> None:
    """Refused for any plan, before frozen_criteria (an overlay run has no edge criteria)."""
    lab = overlay_lab(tmp_path)
    run_id = start_overlay(lab)
    msg = error_text(
        lab.server, "run_sweep", {"run_id": run_id, "plan": plan, "space": SPACE, "n_trials": 3}
    )
    assert "run_sweep is refused in risk-overlay run" in msg
    assert "published parameters, untuned" in msg
    assert counts(lab, run_id) == (0, 0, 0)


@pytest.mark.parametrize("plan", [OVERLAY_PLAN, EMA_PLAN])
def test_walk_forward_is_refused_in_an_overlay_run(tmp_path: Path, plan: dict[str, Any]) -> None:
    lab = overlay_lab(tmp_path)
    run_id = start_overlay(lab)
    args = {"run_id": run_id, "plan": plan, "space": WF_SPACE, "n_trials_per_fold": 2}
    msg = error_text(lab.server, "walk_forward", args)
    assert "walk_forward is refused in risk-overlay run" in msg
    assert "published parameters, untuned" in msg
    assert counts(lab, run_id) == (0, 0, 0)


def test_run_sweep_still_runs_in_an_edge_run(tmp_path: Path) -> None:
    lab = overlay_lab(tmp_path)
    run_id = lab.start()
    out = ok(
        lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 2}
    )
    assert out["n_trials"] == 2


def test_edge_plan_in_an_edge_run_still_backtests(tmp_path: Path) -> None:
    lab = overlay_lab(tmp_path)
    run_id = lab.start()
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    assert "question" not in out["budget"]
    assert counts(lab, run_id)[0] > 0


def test_unknown_run_errors_are_unchanged(tmp_path: Path) -> None:
    lab = overlay_lab(tmp_path)
    msg = error_text(
        lab.server, "run_backtest", {"run_id": "r-20261029-000000-000000", "plan": OVERLAY_PLAN}
    )
    assert "was never started" in msg


# --------------------------------------------------------------------------- reads


def test_get_budget_overlay_adds_question_and_uses_the_overlay_budget(tmp_path: Path) -> None:
    lab = overlay_lab(tmp_path)
    run_id = start_overlay(lab)
    out = ok(lab.server, "get_budget", {"run_id": run_id})
    assert out["question"] == "risk_overlay"
    assert out["budget"] == {"proposals": 6, "backtests": 600, "holdout_checks": 1}
    assert out["used"] == {"proposals": 0, "backtests": 0, "holdout_checks": 0}


def test_get_budget_edge_output_is_unchanged(tmp_path: Path) -> None:
    lab = overlay_lab(tmp_path)
    run_id = lab.start()
    out = ok(lab.server, "get_budget", {"run_id": run_id})
    assert set(out) == {"budget", "used", "remaining"}
    assert out["budget"] == {"proposals": 15, "backtests": 3000, "holdout_checks": 1}


def test_get_report_in_an_overlay_run_never_reads_edge_criteria(tmp_path: Path) -> None:
    """Routed to the overlay rows (lab.research.overlay_criteria), never a KeyError on
    "criteria": a plan with no evidence yet gets only overlay rows, all missing."""
    lab = overlay_lab(tmp_path)
    run_id = start_overlay(lab)
    r = call(lab.server, "get_report", {"run_id": run_id, "plan_hash": "0" * 64})
    assert not r.is_error, r.content[0].text
    assert "'criteria'" not in str(r.content[0].text)
    out = r.structured_content
    assert importlib.util.find_spec("lab.research.overlay_criteria") is not None
    assert out["question"] == "risk_overlay"
    assert out["rows"][0]["criterion"] == "cdar_vs_buy_hold_max"
    assert out["pre_holdout_passed"] is False and "criteria" not in out
    assert "cdar_vs_buy_hold_max" in out["missing"]


def test_list_features_shows_the_overlay_schema_when_available(tmp_path: Path) -> None:
    out = ok(Lab(tmp_path).server, "list_features")
    assert "portfolio" in out
    schema = _overlay_schema()
    if schema is None:
        assert "overlay" not in out
    else:
        assert out["overlay"]["schema"] == schema
        assert "untuned" in out["overlay"]["note"]


def test_validate_plan_edge_output_is_unchanged(tmp_path: Path) -> None:
    out = ok(Lab(tmp_path).server, "validate_plan", {"plan": EMA_PLAN})
    assert set(out) == {"valid", "errors", "plan_hash", "tunable_params"}


def test_validate_plan_marks_an_overlay_plan(tmp_path: Path) -> None:
    out = ok(Lab(tmp_path).server, "validate_plan", {"plan": OVERLAY_PLAN})
    assert out["valid"] is True, out
    assert out["kind"] == "overlay"
    assert "run_sweep and walk_forward are refused" in out["note"]


def test_instructions_describe_the_overlay_question() -> None:
    text = " ".join(INSTRUCTIONS.split())
    assert 'question="risk_overlay"' in text
    assert "run_sweep and walk_forward are refused" in text
    assert "The edge question is unchanged" in text
    assert "a risk overlay" in text
