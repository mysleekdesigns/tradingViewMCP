"""PLAN.md Phase 15 question + criteria file plumbing (offline): the overlay-criteria loader
fails closed, ``start_research_run`` freezes the question with the overlay file's sha256 and no
edge ``criteria`` key, an edge run's frozen config is exactly what it was before Phase 15 (no
``question`` key), and ``check_question`` refuses a plan that answers the other question.

Helpers at the top are shared with ``test_mcp_overlay_run``."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lab.ledger import Ledger
from lab.research.contract import (
    OVERLAY_CRITERIA_PATH,
    QUESTIONS,
    REPO_ROOT,
    OverlayCriteria,
    OverlayResearchConfig,
    ResearchConfigError,
    load_overlay_config,
    load_research_config,
)
from lab.research.runs import (
    check_question,
    frozen_budget,
    frozen_overlay_criteria,
    frozen_question,
    lab_fingerprint,
    overlay_run_ids,
    software_values,
    start_research_run,
)
from tests.test_mcp_server import EMA_PLAN, small_cfg, write_criteria

# --------------------------------------------------------------------------- shared helpers

PROPOSED_OVERLAY = REPO_ROOT / "agents" / "config" / "overlay_criteria.proposed.toml"

# PLAN.md Phase 15 + the binding spec (owner decisions 2026-10-29)
SPEC_OVERLAY = {
    "cdar_vs_buy_hold_max": 0.75,
    "beat_exposure_matched": True,
    "sharpe_non_inferiority": 0.2,
    "break_even_cost_multiple_min": 2.0,
    "min_drawdown_episodes": 3,
    "min_avg_exposure": 0.5,
    "alpha": 0.05,
    "wf_rows_hold": True,
    "pbo_max": 0.2,
    "require_holdout_pass": True,
    "require_crosscheck_match": True,
    "cdar_confidence": 0.95,
    "episode_depth_min": 0.30,
    "bootstrap_mean_block_days": 60,
    "min_episodes_improved_share": 0.5,
}
SPEC_BUDGET = {
    "max_hypotheses": 3,
    "max_proposals": 6,
    "max_backtests": 600,
    "max_holdout_checks": 1,
    "max_usd": 10.0,
}

OVERLAY_PLAN: dict[str, Any] = {
    "kind": "overlay",
    "name": "below_sma200",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"ma": {"sma": {"period": 200}}},
    "flat_when": "close < ma",
}


def write_overlay(path: Path, **overrides: object) -> Path:
    """Copy of the proposed overlay-criteria file with ``key = value`` lines replaced."""
    text = PROPOSED_OVERLAY.read_text()
    for key, value in overrides.items():
        if isinstance(value, bool):
            literal = "true" if value else "false"
        elif isinstance(value, str) and value.startswith("RAW:"):
            literal = value[4:]
        else:
            literal = repr(value)
        text, n = re.subn(rf"(?m)^{key}\s*=\s*[^#\n]*", f"{key} = {literal} ", text)
        assert n == 1, key
    path.write_text(text)
    return path


def drop_key(path: Path, key: str) -> Path:
    text, n = re.subn(rf"(?m)^{key}\s*=.*\n", "", path.read_text())
    assert n == 1, key
    path.write_text(text)
    return path


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def start_overlay(ledger: Ledger, tmp_path: Path, **overrides: object) -> str:
    path = write_overlay(tmp_path / f"o{len(ledger.research_runs())}.toml", **overrides)
    return start_research_run(
        ledger, load_overlay_config(path), label="overlay", cfg=small_cfg(tmp_path)
    )


def start_edge(ledger: Ledger, tmp_path: Path) -> str:
    path = write_criteria(tmp_path / f"c{len(ledger.research_runs())}.toml")
    return start_research_run(
        ledger, load_research_config(path), label="edge", cfg=small_cfg(tmp_path)
    )


def forge(ledger: Ledger, run_id: str, config: dict[str, Any]) -> str:
    ledger.start_research_run(
        run_id, label="forged", config_json=json.dumps(config), config_sha256="0" * 64
    )
    return run_id


# --------------------------------------------------------------------------- the file


def test_questions_and_path() -> None:
    assert QUESTIONS == ("edge", "risk_overlay")
    assert OVERLAY_CRITERIA_PATH.name == "overlay_criteria.toml"
    assert OVERLAY_CRITERIA_PATH.parent.name == "config"


def test_proposed_file_loads_with_the_spec_values() -> None:
    research = load_overlay_config(PROPOSED_OVERLAY)
    assert asdict(research.criteria) == SPEC_OVERLAY
    assert asdict(research.budget) == SPEC_BUDGET
    assert research.question == "risk_overlay"
    assert research.sha256 == hashlib.sha256(PROPOSED_OVERLAY.read_bytes()).hexdigest()
    assert len(fields(OverlayCriteria)) == 15
    assert isinstance(research.criteria.min_drawdown_episodes, int)
    assert isinstance(research.criteria.bootstrap_mean_block_days, int)
    assert isinstance(research.criteria.cdar_vs_buy_hold_max, float)


def test_proposed_file_never_calls_a_pass_an_edge() -> None:
    """The edge word appears only in statements that the edge question is unchanged."""
    for line in PROPOSED_OVERLAY.read_text().splitlines():
        if re.search(r"\bedge\b", line, flags=re.IGNORECASE):  # not "ledger"
            assert "edge question" in line.lower(), line
    assert "risk overlay" in PROPOSED_OVERLAY.read_text().lower()


def test_to_json_has_question_overlay_criteria_budget_only() -> None:
    research = load_overlay_config(PROPOSED_OVERLAY)
    text = research.to_json()
    data = json.loads(text)
    assert set(data) == {"question", "overlay_criteria", "budget"}
    assert data["question"] == "risk_overlay"
    assert text == json.dumps(data, sort_keys=True)


def test_missing_file_names_the_owner_install(tmp_path: Path) -> None:
    with pytest.raises(ResearchConfigError) as exc:
        load_overlay_config(tmp_path / "nope.toml")
    msg = str(exc.value)
    assert "not found" in msg and "no risk-overlay research" in msg
    assert "cp agents/config/overlay_criteria.proposed.toml lab/config/overlay_criteria.toml" in msg


def test_missing_key_is_refused(tmp_path: Path) -> None:
    path = drop_key(write_overlay(tmp_path / "o.toml"), "cdar_confidence")
    with pytest.raises(ResearchConfigError, match=r"missing keys \['cdar_confidence'\]"):
        load_overlay_config(path)


def test_missing_budget_key_is_refused(tmp_path: Path) -> None:
    path = drop_key(write_overlay(tmp_path / "o.toml"), "max_backtests")
    with pytest.raises(ResearchConfigError, match="max_backtests"):
        load_overlay_config(path)


def test_unknown_key_is_refused(tmp_path: Path) -> None:
    path = write_overlay(tmp_path / "o.toml")
    path.write_text(path.read_text().replace("[budget]", "extra_row = 1\n\n[budget]"))
    with pytest.raises(ResearchConfigError, match=r"unknown keys \['extra_row'\]"):
        load_overlay_config(path)


def test_unknown_table_is_refused(tmp_path: Path) -> None:
    path = write_overlay(tmp_path / "o.toml")
    path.write_text(path.read_text() + "\n[criteria]\ndsr_min = 0.0\n")
    with pytest.raises(ResearchConfigError, match="unknown tables"):
        load_overlay_config(path)


def test_missing_tables_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "o.toml"
    path.write_text("[budget]\nmax_hypotheses = 1\n")
    with pytest.raises(ResearchConfigError, match=r"no \[overlay\] table"):
        load_overlay_config(path)
    text = PROPOSED_OVERLAY.read_text()
    path.write_text(text[: text.index("[budget]")])
    with pytest.raises(ResearchConfigError, match=r"no \[budget\] table"):
        load_overlay_config(path)


def test_invalid_toml_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "o.toml"
    path.write_text("[overlay\nalpha = ")
    with pytest.raises(ResearchConfigError, match="not valid TOML"):
        load_overlay_config(path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("beat_exposure_matched", 1),  # int for a bool
        ("wf_rows_hold", 'RAW:"yes"'),  # string for a bool
        ("alpha", True),  # bool for a float
        ("alpha", 'RAW:"0.05"'),  # string for a float
        ("min_drawdown_episodes", 3.0),  # float for an int
        ("bootstrap_mean_block_days", 60.5),  # float for an int
        ("bootstrap_mean_block_days", False),  # bool for an int
        ("min_episodes_improved_share", True),  # bool for a float
        ("min_episodes_improved_share", 'RAW:"0.5"'),  # string for a float
        ("min_episodes_improved_share", -0.1),  # negative
        ("sharpe_non_inferiority", -0.2),  # negative
    ],
)
def test_mistyped_value_is_refused(tmp_path: Path, key: str, value: object) -> None:
    path = write_overlay(tmp_path / "o.toml", **{key: value})
    with pytest.raises(ResearchConfigError, match=key):
        load_overlay_config(path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("alpha", 0.0),
        ("alpha", 1.0),
        ("alpha", 1.5),
        ("cdar_confidence", 0.0),
        ("cdar_confidence", 1.0),
        ("cdar_vs_buy_hold_max", 0.0),
        ("cdar_vs_buy_hold_max", 1.25),
        ("min_avg_exposure", 0.0),
        ("min_avg_exposure", 1.01),
        ("pbo_max", 1.5),
        ("episode_depth_min", 0.0),
        ("episode_depth_min", 1.0),
        ("break_even_cost_multiple_min", 0.5),
        ("min_drawdown_episodes", 0),
        ("bootstrap_mean_block_days", 0),
        ("min_episodes_improved_share", 1.0),
        ("min_episodes_improved_share", 1.5),
    ],
)
def test_out_of_range_value_is_refused(tmp_path: Path, key: str, value: object) -> None:
    path = write_overlay(tmp_path / "o.toml", **{key: value})
    with pytest.raises(ResearchConfigError, match=rf"{key} = .* out of range"):
        load_overlay_config(path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("alpha", 0.01),
        ("cdar_vs_buy_hold_max", 1.0),
        ("min_avg_exposure", 1.0),
        ("pbo_max", 0.0),
        ("break_even_cost_multiple_min", 1.0),
        ("min_drawdown_episodes", 1),
        ("bootstrap_mean_block_days", 1),
        ("sharpe_non_inferiority", 0.0),
        ("min_episodes_improved_share", 0.0),
        ("min_episodes_improved_share", 0.99),
    ],
)
def test_boundary_values_inside_the_range_load(tmp_path: Path, key: str, value: object) -> None:
    research = load_overlay_config(write_overlay(tmp_path / "o.toml", **{key: value}))
    assert getattr(research.criteria, key) == value


def test_edge_loader_still_ignores_the_overlay_file(tmp_path: Path) -> None:
    """The edge criteria file is read by its own loader only; an overlay file is not one."""
    with pytest.raises(ResearchConfigError, match=r"no \[criteria\] table"):
        load_research_config(PROPOSED_OVERLAY)


# --------------------------------------------------------------------------- freezing a run


def test_overlay_run_freezes_question_rows_budget_and_file_sha(
    ledger: Ledger, tmp_path: Path
) -> None:
    path = write_overlay(tmp_path / "o.toml")
    research = load_overlay_config(path)
    run_id = start_research_run(ledger, research, label="ov", cfg=small_cfg(tmp_path))
    run = ledger.research_run(run_id)
    assert run is not None
    config = run["config"]
    assert config["question"] == "risk_overlay"
    assert "criteria" not in config
    assert config["overlay_criteria"] == SPEC_OVERLAY
    assert config["budget"] == SPEC_BUDGET
    assert run["config_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert config["lab"] == lab_fingerprint(small_cfg(tmp_path))
    assert config["software"] == software_values()
    assert set(config) - {"derivatives", "universe_manifest"} == {
        "question",
        "overlay_criteria",
        "budget",
        "lab",
        "software",
    }
    assert frozen_question(ledger, run_id) == "risk_overlay"
    assert frozen_overlay_criteria(ledger, run_id) == research.criteria
    assert asdict(frozen_budget(ledger, run_id)) == SPEC_BUDGET


def test_edge_run_config_is_exactly_the_pre_phase_15_shape(ledger: Ledger, tmp_path: Path) -> None:
    """HEAD behaviour: ``config`` = the criteria file's JSON + lab fingerprint + software (+
    snapshots), sorted keys, and NO ``question`` key (absence means edge)."""
    path = write_criteria(tmp_path / "c.toml")
    research = load_research_config(path)
    cfg = small_cfg(tmp_path)
    run_id = start_research_run(ledger, research, label="edge", cfg=cfg)
    run = ledger.research_run(run_id)
    assert run is not None
    expected = json.loads(research.to_json())
    expected["lab"] = lab_fingerprint(cfg)
    expected["software"] = software_values()
    got = {k: v for k, v in run["config"].items() if k not in ("derivatives", "universe_manifest")}
    assert got == expected
    assert set(json.loads(research.to_json())) == {"criteria", "budget"}
    assert "question" not in run["config"] and "overlay_criteria" not in run["config"]
    assert run["config_sha256"] == research.sha256
    assert frozen_question(ledger, run_id) == "edge"
    with pytest.raises(ValueError, match="is an edge run, not a risk-overlay run"):
        frozen_overlay_criteria(ledger, run_id)


def test_overlay_run_ids_lists_only_overlay_runs(ledger: Ledger, tmp_path: Path) -> None:
    edge = start_edge(ledger, tmp_path)
    one = start_overlay(ledger, tmp_path)
    two = start_overlay(ledger, tmp_path, alpha=0.01)
    assert overlay_run_ids(ledger) == frozenset({one, two})
    assert edge not in overlay_run_ids(ledger)
    assert frozen_overlay_criteria(ledger, two).alpha == 0.01


def test_overlay_run_ids_is_empty_without_overlay_runs(ledger: Ledger, tmp_path: Path) -> None:
    assert overlay_run_ids(ledger) == frozenset()
    start_edge(ledger, tmp_path)
    assert overlay_run_ids(ledger) == frozenset()


def test_frozen_question_fails_closed(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="never started"):
        frozen_question(ledger, "r-20261029-000000-000000")
    forge(ledger, "r-q", {"question": "alpha", "budget": SPEC_BUDGET})
    with pytest.raises(ValueError, match="froze question 'alpha'"):
        frozen_question(ledger, "r-q")
    forge(
        ledger,
        "r-both",
        {"question": "risk_overlay", "criteria": {}, "overlay_criteria": SPEC_OVERLAY},
    )
    with pytest.raises(ValueError, match="one question"):
        frozen_question(ledger, "r-both")
    forge(ledger, "r-edge-explicit", {"question": "edge", "criteria": {}})
    assert frozen_question(ledger, "r-edge-explicit") == "edge"


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {k: v for k, v in SPEC_OVERLAY.items() if k != "alpha"},
        {**SPEC_OVERLAY, "extra": 1},
        {**SPEC_OVERLAY, "alpha": 2.0},
        {**SPEC_OVERLAY, "min_drawdown_episodes": "3"},
    ],
)
def test_frozen_overlay_criteria_fails_closed_on_a_forged_snapshot(
    ledger: Ledger, snapshot: object
) -> None:
    forge(ledger, "r-forged", {"question": "risk_overlay", "overlay_criteria": snapshot})
    with pytest.raises(ValueError, match="start a new run"):
        frozen_overlay_criteria(ledger, "r-forged")


def test_overlay_research_config_default_question() -> None:
    research = load_overlay_config(PROPOSED_OVERLAY)
    assert isinstance(research, OverlayResearchConfig)
    assert OverlayResearchConfig(research.criteria, research.budget, "x").question == (
        "risk_overlay"
    )


# --------------------------------------------------------------------------- check_question

PORTFOLIO_RAW: dict[str, Any] = {"kind": "portfolio", "name": "top_momentum"}


@pytest.mark.parametrize(
    "plan",
    [
        OVERLAY_PLAN,
        json.dumps(OVERLAY_PLAN),
        SimpleNamespace(kind="overlay", symbol="BTCUSDT"),
    ],
)
def test_overlay_plan_is_refused_in_an_edge_run(
    ledger: Ledger, tmp_path: Path, plan: object
) -> None:
    run_id = start_edge(ledger, tmp_path)
    with pytest.raises(ValueError, match="asks the edge question") as exc:
        check_question(ledger, run_id, plan)
    assert 'question="risk_overlay"' in str(exc.value)


@pytest.mark.parametrize(
    "plan",
    [EMA_PLAN, json.dumps(EMA_PLAN), PORTFOLIO_RAW, SimpleNamespace(kind="signal")],
)
def test_edge_and_portfolio_plans_pass_in_an_edge_run(
    ledger: Ledger, tmp_path: Path, plan: object
) -> None:
    check_question(ledger, start_edge(ledger, tmp_path), plan)


@pytest.mark.parametrize(
    ("plan", "shown"),
    [
        (EMA_PLAN, "single-symbol signal plan"),
        (json.dumps(EMA_PLAN), "single-symbol signal plan"),
        (SimpleNamespace(kind="signal"), "single-symbol signal plan"),
        (PORTFOLIO_RAW, "kind 'portfolio'"),
        ("not json", "single-symbol signal plan"),
    ],
)
def test_other_plans_are_refused_in_an_overlay_run(
    ledger: Ledger, tmp_path: Path, plan: object, shown: str
) -> None:
    run_id = start_overlay(ledger, tmp_path)
    with pytest.raises(ValueError, match="asks the risk-overlay question") as exc:
        check_question(ledger, run_id, plan)
    assert shown in str(exc.value)


@pytest.mark.parametrize(
    "plan", [OVERLAY_PLAN, json.dumps(OVERLAY_PLAN), SimpleNamespace(kind="overlay")]
)
def test_overlay_plan_passes_in_an_overlay_run(
    ledger: Ledger, tmp_path: Path, plan: object
) -> None:
    check_question(ledger, start_overlay(ledger, tmp_path), plan)


def test_check_question_on_an_unknown_run(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="never started"):
        check_question(ledger, "r-20261029-000000-000000", OVERLAY_PLAN)
