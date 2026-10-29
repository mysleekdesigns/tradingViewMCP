"""strategy-lab MCP server, PLAN.md Phase 15 risk-overlay pipeline end to end (offline,
in-process client, tmp ledger, seeded synthetic candles, tmp copies of the proposed criteria
files): run_backtest -> run_adversarial -> run_crosscheck -> get_report (only overlay rows, CDaR
vs buy-and-hold and vs exposure-matched buy-and-hold, Sharpe non-inferiority, break-even cost,
episode count and N_overlay; never the word "edge") -> holdout_check (pass/fail only);
get_budget's n_overlay across runs; one published parameter set per overlay logic (a second
set refused before anything is recorded); and an edge run after an overlay run is added to the
same ledger: its N grows by exactly the overlay run's distinct selection plans while its V[SR]
values are unchanged."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from lab.ledger import SELECTION_SOURCES, Ledger
from lab.mcp_server import build_server
from tests.test_mcp_server import (
    TREND_PLAN,
    error_text,
    loader_for,
    ok,
    trend_candles,
    write_criteria,
)
from tests.test_research_overlay import (
    CRASH_CANDLES,
    EMA_COPY,
    MA_EDGE_SAME_LOGIC,
    MA_OVERLAY,
    RSI_OVERLAY,
    SMA_REWRITES,
    edge_word,
    overlay_cfg,
    overlay_toml,
)

OVERLAY_ROW_NAMES = [
    "cdar_vs_buy_hold_max",
    "beat_exposure_matched",
    "min_episodes_improved",
    "sharpe_non_inferiority",
    "break_even_cost_multiple_min",
    "min_drawdown_episodes",
    "min_avg_exposure",
    "wf_rows_hold",
    "pbo_max",
    "require_holdout_pass",
    "require_crosscheck_match",
]
EDGE_LEDGER_KEYS = {
    "run_id",
    "total",
    "by_source",
    "by_status",
    "dsr_n",
    "dsr_n_basis",
    "run_distinct_plans",
    "items",
}


class OverlayLab:
    """A server with both criteria files, the overlay test bootstrap size and a loader spy."""

    def __init__(self, tmp_path: Path, candles: pd.DataFrame = CRASH_CANDLES) -> None:
        self.cfg = overlay_cfg(tmp_path)
        self.ledger_path = tmp_path / "ledger.db"
        self.calls: list[pd.Timestamp] = []
        self.server = build_server(
            cfg=self.cfg,
            ledger_path=self.ledger_path,
            loader=loader_for(candles, self.calls),
            research_config_path=write_criteria(tmp_path / "criteria.toml"),
            overlay_config_path=overlay_toml(tmp_path),
        )

    def ledger(self) -> Ledger:
        return Ledger(self.ledger_path)

    def edge(self) -> str:
        return str(ok(self.server, "start_run", {"label": "edge"})["run_id"])

    def overlay(self) -> str:
        out = ok(self.server, "start_run", {"label": "ov", "question": "risk_overlay"})
        return str(out["run_id"])

    def pipeline(self, run_id: str, plan: dict[str, Any]) -> str:
        bt = ok(self.server, "run_backtest", {"run_id": run_id, "plan": plan})
        ok(self.server, "run_adversarial", {"run_id": run_id, "plan": plan})
        cross = ok(self.server, "run_crosscheck", {"run_id": run_id, "plan": plan})
        assert cross["available"] is True and cross["matched"] is True, cross
        return str(bt["plan_hash"])

    def report(self, run_id: str, h: str) -> dict[str, Any]:
        return ok(self.server, "get_report", {"run_id": run_id, "plan_hash": h})


def test_overlay_pipeline_reports_only_overlay_rows_and_spends_one_holdout_check(
    tmp_path: Path,
) -> None:
    lab = OverlayLab(tmp_path)
    run_id = lab.overlay()
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": MA_OVERLAY})
    assert "overlay" in bt and "dsr_run_local" not in bt and "dsr" not in bt["stats"]
    assert "ungated" not in bt["benchmarks"]
    assert bt["overlay"]["episodes"]["buy_hold_count"] >= 3
    ok(lab.server, "run_adversarial", {"run_id": run_id, "plan": MA_OVERLAY})
    cross = ok(lab.server, "run_crosscheck", {"run_id": run_id, "plan": MA_OVERLAY})
    assert cross["matched"] is True, cross
    h = str(bt["plan_hash"])

    rep = lab.report(run_id, h)
    assert [r["criterion"] for r in rep["rows"]] == OVERLAY_ROW_NAMES
    status = {r["criterion"]: r["status"] for r in rep["rows"]}
    assert status.pop("require_holdout_pass") == "missing"
    assert set(status.values()) == {"pass"}, rep["rows"]
    assert rep["pre_holdout_passed"] is True and rep["passed"] is False
    assert rep["question"] == "risk_overlay"
    assert rep["n_overlay"] == 1 and rep["alpha_adjusted"] == 0.05
    assert "Bonferroni" in rep["n_overlay_basis"]
    assert {"cdar", "cdar_ratio_vs_buy_hold", "episodes", "by_cost", "wf_fixed"} <= set(
        rep["overlay"]
    )
    assert "criteria" not in rep and "ledger_n" not in rep and "statistics" not in rep
    assert edge_word(json.dumps(rep)) == []

    budget = ok(lab.server, "get_budget", {"run_id": run_id})
    assert budget["question"] == "risk_overlay" and budget["n_overlay"] == 1
    led_out = ok(lab.server, "get_ledger", {"run_id": run_id, "limit": 5})
    assert led_out["n_overlay"] == 1

    n_calls = len(lab.calls)
    assert all(end <= lab.cfg.splits.holdout_start for end in lab.calls)
    out = ok(lab.server, "holdout_check", {"run_id": run_id, "plan": MA_OVERLAY})
    assert out == {"passed": True, "checks_used": 1, "checks_left": 0}
    assert len(lab.calls) == n_calls + 1 and lab.calls[-1] > lab.cfg.splits.holdout_start
    after = lab.report(run_id, h)
    assert after["passed"] is True
    assert edge_word(json.dumps(after)) == []
    msg = error_text(lab.server, "holdout_check", {"run_id": run_id, "plan": MA_OVERLAY})
    assert "already holdout-checked" in msg


def test_not_testable_market_blocks_the_holdout_through_the_server(tmp_path: Path) -> None:
    lab = OverlayLab(tmp_path, candles=trend_candles())
    run_id = lab.overlay()
    h = lab.pipeline(run_id, MA_OVERLAY)
    rep = lab.report(run_id, h)
    assert "min_drawdown_episodes" in rep["not_testable"]
    assert rep["pre_holdout_passed"] is False
    msg = error_text(lab.server, "holdout_check", {"run_id": run_id, "plan": MA_OVERLAY})
    assert "min_drawdown_episodes (not_testable)" in msg and "no holdout check was spent" in msg
    with lab.ledger() as led:
        assert led.holdout_checks_used(run_id) == 0


def test_n_overlay_grows_across_overlay_runs_and_a_new_run_never_resets_it(
    tmp_path: Path,
) -> None:
    lab = OverlayLab(tmp_path)
    first = lab.overlay()
    ok(lab.server, "run_backtest", {"run_id": first, "plan": MA_OVERLAY})
    assert ok(lab.server, "get_budget", {"run_id": first})["n_overlay"] == 1
    second = lab.overlay()
    ok(lab.server, "run_backtest", {"run_id": second, "plan": RSI_OVERLAY})
    for run_id in (first, second):
        assert ok(lab.server, "get_budget", {"run_id": run_id})["n_overlay"] == 2
    third = lab.overlay()
    assert ok(lab.server, "get_budget", {"run_id": third})["n_overlay"] == 2
    rep = lab.report(first, ok(lab.server, "validate_plan", {"plan": MA_OVERLAY})["plan_hash"])
    assert rep["n_overlay"] == 2 and rep["alpha_adjusted"] == 0.025


def _v_sr(report: dict[str, Any]) -> str:
    """The V[SR] part of the edge report's dsr_min note (the values deflated with)."""
    note = next(r["note"] for r in report["rows"] if r["criterion"] == "dsr_min")
    m = re.search(r"V\[SR\] .*?; latest Sharpe per distinct plan", note)
    assert m is not None, note
    return m.group(0)


def test_an_edge_run_counts_overlay_trials_in_n_but_not_in_v_sr(tmp_path: Path) -> None:
    """Owner decision 2026-10-29, "count in N, not V[SR]". The overlay trades ETHUSDT on the
    same interval, window and costs as the edge plan, so without the V[SR] exclusion its
    trials would land in every Sharpe pool (a different symbol keeps the behaviour family,
    which spans both questions, out of the comparison)."""
    lab = OverlayLab(tmp_path)
    edge = lab.edge()
    h = lab.pipeline(edge, TREND_PLAN)
    space = {"ema_fast.period": {"low": 15, "high": 25}}
    ok(lab.server, "run_sweep", {"run_id": edge, "plan": TREND_PLAN, "space": space, "n_trials": 4})
    before = lab.report(edge, h)
    ledger_before = ok(lab.server, "get_ledger", {"run_id": edge, "limit": 500})
    budget_before = ok(lab.server, "get_budget", {"run_id": edge})

    eth = {**MA_OVERLAY, "symbol": "ETHUSDT"}
    overlay = lab.overlay()
    lab.pipeline(overlay, eth)
    with lab.ledger() as led:
        counted = led.count_distinct_plans(overlay, sources=SELECTION_SOURCES)
    assert counted >= 1
    after = lab.report(edge, h)
    assert after["ledger_n"] == before["ledger_n"] + counted
    assert _v_sr(after) == _v_sr(before)  # every V[SR] pool is unchanged
    ledger_after = ok(lab.server, "get_ledger", {"run_id": edge, "limit": 500})
    assert ledger_after["dsr_n"] == ledger_before["dsr_n"] + counted
    assert {k: v for k, v in ledger_after.items() if k != "dsr_n"} == {
        k: v for k, v in ledger_before.items() if k != "dsr_n"
    }
    assert ok(lab.server, "get_budget", {"run_id": edge}) == budget_before
    assert set(ledger_after) == EDGE_LEDGER_KEYS
    assert set(budget_before) == {"budget", "used", "remaining"}
    with lab.ledger() as led:
        everything = led._conn.execute(
            "SELECT COUNT(DISTINCT plan_hash) FROM trials WHERE source IN (?, ?, ?, ?)",
            SELECTION_SOURCES,
        ).fetchone()[0]
    assert everything == ledger_after["dsr_n"]  # every selection plan of every run is in N
    sweep = ok(
        lab.server, "run_sweep", {"run_id": edge, "plan": TREND_PLAN, "space": space, "n_trials": 2}
    )
    with lab.ledger() as led:
        assert sweep["dsr_n"] == led.count_distinct_plans_research(sources=SELECTION_SOURCES)


def test_a_second_parameter_set_is_refused_by_every_overlay_tool(tmp_path: Path) -> None:
    lab = OverlayLab(tmp_path)
    first = lab.overlay()
    h = str(ok(lab.server, "run_backtest", {"run_id": first, "plan": MA_OVERLAY})["plan_hash"])
    other = {**MA_OVERLAY, "features": {"ma": {"sma": {"period": 50}}}}
    second = lab.overlay()
    with lab.ledger() as led:
        n_trials = led._conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
    for run_id in (first, second):
        for tool in ("run_backtest", "run_adversarial", "run_crosscheck", "holdout_check"):
            msg = error_text(lab.server, tool, {"run_id": run_id, "plan": other})
            assert (
                "overlays use published parameters, untuned: logic already tried as plan "
                f"{h[:12]} in run {first}"
            ) in msg, (tool, msg)
        assert ok(lab.server, "get_budget", {"run_id": run_id})["n_overlay"] == 1
    with lab.ledger() as led:
        assert led._conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == n_trials
        assert led.holdout_checks_used(first) == 0 and led.holdout_checks_used(second) == 0
    # the same plan is still welcome in the later run
    again = ok(lab.server, "run_backtest", {"run_id": second, "plan": MA_OVERLAY})
    assert again["plan_hash"] == h


def _pbo(lab: OverlayLab, run_id: str, h: str) -> dict[str, Any]:
    return next(r for r in lab.report(run_id, h)["rows"] if r["criterion"] == "pbo_max")


def test_reviewer_rewrites_are_refused_and_an_alike_copy_fails_pbo(tmp_path: Path) -> None:
    """Round-2 probe: after sma(60) `close < ma`, the operand swap, the negation, the ratio
    form (each with another period) and the 1d copy are refused by run_backtest before any
    trial; the ema(60) copy is accepted (other features) but then pbo_max fails for both."""
    lab = OverlayLab(tmp_path)
    run_id = lab.overlay()
    h = str(ok(lab.server, "run_backtest", {"run_id": run_id, "plan": MA_OVERLAY})["plan_hash"])
    assert _pbo(lab, run_id, h)["status"] == "pass"
    with lab.ledger() as led:
        n_trials = led._conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
    for name, plan in SMA_REWRITES.items():
        msg = error_text(lab.server, "run_backtest", {"run_id": run_id, "plan": plan})
        assert "overlays use published parameters, untuned" in msg, (name, msg)
        assert f"plan {h[:12]} in run {run_id}" in msg, (name, msg)
    with lab.ledger() as led:
        assert led._conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == n_trials
    assert ok(lab.server, "get_budget", {"run_id": run_id})["n_overlay"] == 1
    ema = str(ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_COPY})["plan_hash"])
    for plan_hash in (h, ema):
        row = _pbo(lab, run_id, plan_hash)
        assert row["status"] == "fail" and row["note"].startswith("2 alike overlays submitted")
    assert (tmp_path / "locks" / "overlay-submissions.lock").exists()


def test_an_edge_sweep_of_the_signal_twin_blocks_the_overlay(tmp_path: Path) -> None:
    """Round-2 probe: an edge-run sweep of the same logic picks the period; the overlay with
    that period, or any other, is then refused ("logic already tuned in edge run")."""
    lab = OverlayLab(tmp_path)
    edge = lab.edge()
    sweep = ok(
        lab.server,
        "run_sweep",
        {
            "run_id": edge,
            "plan": MA_EDGE_SAME_LOGIC,
            "space": {"m.period": {"low": 20, "high": 120}},
            "n_trials": 6,
        },
    )
    best = int(sweep["best_params"]["m.period"])
    run_id = lab.overlay()
    for period in (best, 90):
        plan = {**MA_OVERLAY, "features": {"ma": {"sma": {"period": period}}}}
        msg = error_text(lab.server, "run_backtest", {"run_id": run_id, "plan": plan})
        assert f"logic already tuned in edge run {edge}" in msg, (period, msg)
    assert ok(lab.server, "get_budget", {"run_id": run_id})["n_overlay"] == 1
    with lab.ledger() as led:
        assert led.count(run_id) == 0
