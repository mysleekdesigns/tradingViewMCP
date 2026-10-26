"""The Phase 8 shared contract: success-criteria loading (fail closed), run ids, the Ledger's
research tables (append-only, duplicate ids, evidence kinds), budget accounting, and the
evidence compaction."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from lab.ledger import EVIDENCE_KINDS, Ledger
from lab.research.contract import (
    RESEARCH_RUN_ID_PATTERN,
    ResearchConfigError,
    load_research_config,
)
from lab.research.evidence import MAX_LIST_ITEMS, compact_report, record_report
from lab.research.runs import (
    budget_status,
    check_lab_fingerprint,
    frozen_budget,
    holdout_budget,
    lab_fingerprint,
    new_run_id,
    run_usage,
    start_research_run,
)
from tests.test_mcp_server import PROPOSED_CRITERIA, small_cfg, write_criteria


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def start(ledger: Ledger, tmp_path: Path, **criteria: object) -> str:
    path = write_criteria(tmp_path / f"c{len(ledger.research_runs())}.toml", **criteria)
    # small_cfg(tmp_path) has the same lab values as the ``cfg`` fixture (only data_dir,
    # which is not fingerprinted, differs), so the run's lab.toml fingerprint matches it.
    return start_research_run(
        ledger, load_research_config(path), label="lbl", cfg=small_cfg(tmp_path)
    )


# --------------------------------------------------------------------------- criteria file


def test_proposed_file_loads_with_the_plan_md_thresholds(tmp_path: Path) -> None:
    path = write_criteria(tmp_path / "c.toml")
    rc = load_research_config(path)
    assert rc.criteria.wf_oos_sharpe_min == 1.0 and rc.criteria.dsr_min == 0.95
    assert rc.criteria.pbo_max == 0.2 and rc.criteria.max_drawdown_max == 0.25
    assert rc.criteria.min_trades == 100 and rc.criteria.require_holdout_pass is True
    assert rc.budget.max_holdout_checks == 1 and rc.budget.max_usd == 20.0
    assert rc.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert json.loads(rc.to_json()) == {
        "criteria": asdict(rc.criteria),
        "budget": asdict(rc.budget),
    }


def test_missing_file_fails_closed_with_the_install_command(tmp_path: Path) -> None:
    with pytest.raises(ResearchConfigError, match=r"success_criteria\.proposed\.toml"):
        load_research_config(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda t: t.replace("dsr_min = 0.95", ""), "missing keys \\['dsr_min'\\]"),
        (lambda t: t.replace("[budget]", "[budget]\nextra_key = 1"), "unknown keys"),
        (lambda t: t.replace("min_trades = 100", 'min_trades = "lots"'), "min_trades"),
        (lambda t: t.replace("beat_buy_hold = true", "beat_buy_hold = 1"), "beat_buy_hold"),
        (lambda t: t.replace("pbo_max = 0.2", "pbo_max = -0.2"), "pbo_max"),
        (lambda t: t.replace("[criteria]", "[criteria"), "not valid TOML"),
        (lambda t: t.replace("[budget]", "[budgets]"), "no \\[budget\\] table"),
    ],
)
def test_malformed_files_are_rejected(tmp_path: Path, edit: Any, match: str) -> None:
    path = tmp_path / "c.toml"
    path.write_text(edit(PROPOSED_CRITERIA.read_text()))
    with pytest.raises(ResearchConfigError, match=match):
        load_research_config(path)


# --------------------------------------------------------------------------- run ids


def test_new_run_id_matches_the_pattern_and_encodes_utc_time() -> None:
    rid = new_run_id(datetime(2026, 10, 24, 13, 5, 9, tzinfo=UTC))
    assert re.match(RESEARCH_RUN_ID_PATTERN, rid)
    assert rid.startswith("r-20261024-130509-")
    assert new_run_id() != new_run_id()


# --------------------------------------------------------------------------- ledger tables


def test_start_research_run_freezes_the_config(ledger: Ledger, tmp_path: Path) -> None:
    run_id = start(ledger, tmp_path, dsr_min=0.9)
    run = ledger.research_run(run_id)
    assert run is not None
    assert run["label"] == "lbl" and run["config"]["criteria"]["dsr_min"] == 0.9
    assert run["config"]["budget"]["max_proposals"] == 15
    assert len(run["config_sha256"]) == 64
    assert [r["run_id"] for r in ledger.research_runs()] == [run_id]
    assert ledger.research_run("never") is None


def test_duplicate_run_id_is_rejected(ledger: Ledger) -> None:
    ledger.start_research_run("r-1", label="a", config_json="{}", config_sha256="x")
    with pytest.raises(ValueError, match="already exists"):
        ledger.start_research_run("r-1", label="b", config_json="{}", config_sha256="y")


def test_bad_config_json_is_rejected(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="config_json"):
        ledger.start_research_run("r-1", label="a", config_json="{nope", config_sha256="x")


def test_evidence_kind_run_and_json_are_validated(ledger: Ledger) -> None:
    ledger.start_research_run("r-1", label="a", config_json="{}", config_sha256="x")
    with pytest.raises(ValueError, match="kind must be one of"):
        ledger.record_evidence("r-1", "h", "vibes", "{}")
    with pytest.raises(ValueError, match="never started"):
        ledger.record_evidence("r-2", "h", "backtest", "{}")
    with pytest.raises(ValueError, match="payload_json"):
        ledger.record_evidence("r-1", "h", "backtest", "{bad")
    assert set(EVIDENCE_KINDS) == {
        "backtest",
        "adversarial",
        "sweep",
        "walk_forward",
        "crosscheck",
        "holdout",
    }


def test_evidence_filters_and_order(ledger: Ledger) -> None:
    ledger.start_research_run("r-1", label="a", config_json="{}", config_sha256="x")
    a = ledger.record_evidence("r-1", "h1", "backtest", '{"n": 1}')
    b = ledger.record_evidence("r-1", "h2", "backtest", '{"n": 2}')
    c = ledger.record_evidence("r-1", "h1", "sweep", '{"n": 3}')
    assert [e["id"] for e in ledger.evidence("r-1")] == [a, b, c]
    assert [e["id"] for e in ledger.evidence("r-1", plan_hash="h1")] == [a, c]
    assert [e["payload"] for e in ledger.evidence("r-1", kind="backtest")] == [{"n": 1}, {"n": 2}]
    assert ledger.evidence("r-1", plan_hash="h1", kind="backtest")[0]["id"] == a


def test_count_evidence_plans_counts_distinct_plans_per_run(ledger: Ledger) -> None:
    ledger.start_research_run("r-1", label="a", config_json="{}", config_sha256="x")
    ledger.start_research_run("r-2", label="b", config_json="{}", config_sha256="x")
    assert ledger.count_evidence_plans("r-1") == 0
    ledger.record_evidence("r-1", "h1", "backtest", "{}")
    ledger.record_evidence("r-1", "h1", "sweep", "{}")
    ledger.record_evidence("r-1", "h2", "backtest", "{}")
    ledger.record_evidence("r-2", "h3", "backtest", "{}")
    assert ledger.count_evidence_plans("r-1") == 2
    assert ledger.count_evidence_plans("r-2") == 1


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE research_runs SET label = 'x'",
        "DELETE FROM research_runs",
        "UPDATE research_evidence SET payload = '{}'",
        "DELETE FROM research_evidence",
    ],
)
def test_research_tables_are_append_only(tmp_path: Path, sql: str) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path) as led:
        led.start_research_run("r-1", label="a", config_json="{}", config_sha256="x")
        led.record_evidence("r-1", "h", "backtest", "{}")
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)
    finally:
        conn.close()
    with Ledger(path) as led:
        assert len(led.research_runs()) == 1 and len(led.evidence("r-1")) == 1


# --------------------------------------------------------------------------- budgets


def test_budget_accounting(ledger: Ledger, tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    run_id = start(ledger, tmp_path, max_holdout_checks=5)
    assert frozen_budget(ledger, run_id).max_holdout_checks == 5
    assert holdout_budget(ledger, run_id, cfg) == 3  # capped by lab.toml holdout_budget
    capped = replace(cfg, splits=replace(cfg.splits, holdout_budget=1))
    assert holdout_budget(ledger, run_id, capped) == 1

    ledger.record_evidence(run_id, "h1", "backtest", "{}")
    ledger.record_evidence(run_id, "h1", "sweep", "{}")
    ledger.record_evidence(run_id, "h2", "backtest", "{}")
    ledger.record_holdout_check(run_id, "h1", budget=3)
    assert run_usage(ledger, run_id) == {"proposals": 2, "backtests": 0, "holdout_checks": 1}
    status = budget_status(ledger, run_id, cfg)
    assert status["budget"] == {"proposals": 15, "backtests": 3000, "holdout_checks": 3}
    assert status["remaining"] == {"proposals": 13, "backtests": 3000, "holdout_checks": 2}


def test_frozen_budget_of_unknown_run_raises(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="never started"):
        frozen_budget(ledger, "nope")


# --------------------------------------------------------------------------- evidence


def test_compact_report_drops_long_lists_and_nonfinite_values() -> None:
    long = list(range(MAX_LIST_ITEMS + 1))
    out = compact_report(
        {
            "equity": long,
            "short": [1, 2],
            "nested": {"trades": [{"x": 1}] * (MAX_LIST_ITEMS + 5)},
            "nan": math.nan,
            "np": np.float64(1.5),
            "arr": np.arange(3),
        }
    )
    assert out == {
        "equity": {"n": MAX_LIST_ITEMS + 1},
        "short": [1, 2],
        "nested": {"trades": {"n": MAX_LIST_ITEMS + 5}},
        "nan": None,
        "np": 1.5,
        "arr": [0, 1, 2],
    }


def test_record_report_stores_the_compact_payload(ledger: Ledger, tmp_path: Path) -> None:
    run_id = start(ledger, tmp_path)
    eid = record_report(ledger, run_id, "h", "backtest", {"equity": list(range(100)), "x": 1})
    (ev,) = ledger.evidence(run_id)
    assert ev["id"] == eid and ev["payload"] == {"equity": {"n": 100}, "x": 1}


# --------------------------------------------------------------------------- lab fingerprint


def test_start_research_run_snapshots_the_lab_config(ledger: Ledger, tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    run_id = start(ledger, tmp_path)
    run = ledger.research_run(run_id)
    assert run is not None
    lab = run["config"]["lab"]
    assert lab == lab_fingerprint(cfg)
    values = lab["values"]
    assert set(values) == {
        "costs",
        "stress_factors",
        "splits",
        "benchmarks",
        "stats",
        "optuna_min_trades",
        "optuna_seed",
        "init_cash",
        "research",
        "universe",
    }
    assert values["universe"]["top_n"] == cfg.universe.top_n
    assert values["optuna_seed"] == cfg.optuna_seed
    assert values["research"] == {
        "family_return_corr": cfg.research.family_return_corr,
        "family_min_overlap_bars": cfg.research.family_min_overlap_bars,
        "min_variance_plans": cfg.research.min_variance_plans,
        "min_pbo_trials": cfg.research.min_pbo_trials,
        "literal_lineage_corr": cfg.research.literal_lineage_corr,
    }
    assert values["splits"]["holdout_start"] == "2019-10-01T00:00:00+00:00"  # ISO date
    assert values["costs"]["fee_rate"] == cfg.costs.fee_rate
    text = json.dumps(values, sort_keys=True, separators=(",", ":"))
    assert lab["fingerprint"] == hashlib.sha256(text.encode()).hexdigest()
    # criteria + budget are still frozen alongside, and the file hash is unchanged
    assert set(run["config"]) == {"criteria", "budget", "lab"}


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda c: replace(c, optuna_seed=c.optuna_seed + 1), id="optuna-seed"),
        pytest.param(
            lambda c: replace(c, research=replace(c.research, family_return_corr=0.7)),
            id="family-threshold",
        ),
        pytest.param(
            lambda c: replace(c, research=replace(c.research, family_min_overlap_bars=100)),
            id="family-overlap",
        ),
        pytest.param(
            lambda c: replace(c, research=replace(c.research, min_variance_plans=5)),
            id="variance-plans",
        ),
        pytest.param(
            lambda c: replace(c, research=replace(c.research, min_pbo_trials=8)),
            id="pbo-trials",
        ),
        pytest.param(
            lambda c: replace(c, research=replace(c.research, literal_lineage_corr=0.9)),
            id="literal-lineage-corr",
        ),
    ],
)
def test_lab_fingerprint_covers_the_optuna_seed_and_the_family_threshold(
    tmp_path: Path, change: Any
) -> None:
    cfg = small_cfg(tmp_path)
    assert lab_fingerprint(change(cfg))["fingerprint"] != lab_fingerprint(cfg)["fingerprint"]


def test_lab_fingerprint_ignores_the_data_directory(tmp_path: Path) -> None:
    a = lab_fingerprint(small_cfg(tmp_path / "a"))
    b = lab_fingerprint(small_cfg(tmp_path / "b"))
    assert a == b


@pytest.mark.parametrize(
    "change",
    [
        lambda c: replace(c, costs=replace(c.costs, slippage_rate=0.001)),
        lambda c: replace(c, stress_factors=(3.0,)),
        lambda c: replace(c, splits=replace(c.splits, wf_test_months=4)),
        lambda c: replace(c, benchmarks=replace(c.benchmarks, random_entry_paths=11)),
        lambda c: replace(c, stats=replace(c.stats, neighbourhood_steps=(0.9, 1.0, 1.1))),
        lambda c: replace(c, optuna_min_trades=31),
        lambda c: replace(c, init_cash=5000.0),
    ],
)
def test_check_lab_fingerprint_refuses_any_result_affecting_change(
    ledger: Ledger, tmp_path: Path, change: Any
) -> None:
    cfg = small_cfg(tmp_path)
    run_id = start(ledger, tmp_path)
    check_lab_fingerprint(ledger, run_id, cfg)  # unchanged: fine
    with pytest.raises(
        ValueError, match=r"lab\.toml changed since this run started; start a new run"
    ):
        check_lab_fingerprint(ledger, run_id, change(cfg))


def test_check_lab_fingerprint_refuses_runs_without_one_and_unknown_runs(
    ledger: Ledger, tmp_path: Path
) -> None:
    path = write_criteria(tmp_path / "c.toml")
    bare = start_research_run(ledger, load_research_config(path), label="bare")  # no cfg
    with pytest.raises(ValueError, match=r"no lab\.toml fingerprint"):
        check_lab_fingerprint(ledger, bare, small_cfg(tmp_path))
    with pytest.raises(ValueError, match="never started"):
        check_lab_fingerprint(ledger, "nope", small_cfg(tmp_path))


@pytest.mark.parametrize(
    "change",
    [
        lambda c: replace(c, universe=replace(c.universe, top_n=20)),
        lambda c: replace(c, universe=replace(c.universe, exclude_bases=("USDC",))),
        lambda c: replace(c, universe=replace(c.universe, relist_jump=10.0)),
    ],
)
def test_lab_fingerprint_covers_the_universe(tmp_path: Path, change: Any) -> None:
    cfg = small_cfg(tmp_path)
    assert lab_fingerprint(change(cfg))["fingerprint"] != lab_fingerprint(cfg)["fingerprint"]
