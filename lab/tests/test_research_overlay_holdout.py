"""PLAN.md Phase 15 holdout check for overlay plans (offline, seeded synthetic candles, tmp
ledger): the question gate comes first, the check is refused without spending until every
pre-holdout overlay row passes (a matched cross-check included; not_testable is not a pass),
the owner's pass rule on the holdout (point estimates at base costs; a calm holdout cannot
confirm), pass/fail-only output, and holdout death keyed on logic AND behaviour family across
both research questions, in both directions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from lab.config import LabConfig
from lab.ledger import Ledger
from lab.plan import parse_plan, plan_hash
from lab.research.holdout import HoldoutRefusedError, overlay_holdout_passed, run_holdout_check
from lab.research.logic import logic_hash
from lab.research.overlay import overlay_holdout_view
from lab.research.overlay_criteria import evaluate_overlay_plan
from lab.research.pipeline import backtest_evidence, crosscheck_evidence
from lab.research.runs import frozen_overlay_criteria
from lab.types import Window
from tests.test_mcp_server import EMA_PLAN, loader_for, trend_candles
from tests.test_research_overlay import (
    CRASH_CANDLES,
    MA_EDGE_ALIKE,
    MA_EDGE_SAME_LOGIC,
    MA_OVERLAY,
    crash_candles,
    overlay_cfg,
    start_edge_run,
    start_overlay_run,
)

ONLY_PASS_FAIL = {"passed", "checks_used", "checks_left"}


@pytest.fixture
def led(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as ledger:
        yield ledger


def ready_overlay(
    led: Ledger,
    tmp_path: Path,
    cfg: LabConfig,
    candles: pd.DataFrame = CRASH_CANDLES,
    plan: dict[str, Any] = MA_OVERLAY,
    *,
    crosscheck: bool = True,
    calls: list[pd.Timestamp] | None = None,
) -> tuple[str, str]:
    """An overlay run with the plan's backtest (and matched cross-check) evidence."""
    run_id = start_overlay_run(led, tmp_path / "overlay", cfg)
    load = loader_for(candles, calls)
    bt = backtest_evidence(plan, run_id=run_id, cfg=cfg, ledger=led, loader=load)
    if crosscheck:
        cross = crosscheck_evidence(plan, run_id=run_id, cfg=cfg, ledger=led, loader=load)
        assert cross["matched"] is True, cross
    return run_id, str(bt["plan_hash"])


def spend_edge_holdout(led: Ledger, tmp_path: Path, cfg: LabConfig, plan: dict[str, Any]) -> str:
    """Spend an edge run's holdout check on ``plan`` through the ledger's race-free guard (the
    same call the edge holdout check makes once its criteria pass; the edge pre-holdout bar is
    not what is under test here)."""
    edge = start_edge_run(led, tmp_path, cfg)
    h, lh = plan_hash(parse_plan(plan)), logic_hash(plan)
    led.record_holdout_check(
        edge, h, budget=1, logic_hash=lh, family_logic_hashes={lh}, family_plan_hashes={h}
    )
    return edge


def check(led: Ledger, run_id: str, plan: dict[str, Any], cfg: LabConfig, **kw: Any) -> Any:
    candles = kw.pop("candles", CRASH_CANDLES)
    return run_holdout_check(
        plan, run_id=run_id, cfg=cfg, ledger=led, loader=loader_for(candles, kw.pop("calls", None))
    )


# --------------------------------------------------------------------------- pass / fail


def test_a_working_overlay_passes_a_holdout_with_a_bust_and_only_pass_fail_leaves(
    led: Ledger, tmp_path: Path
) -> None:
    cfg = overlay_cfg(tmp_path)
    calls: list[pd.Timestamp] = []
    run_id, h = ready_overlay(led, tmp_path, cfg, calls=calls)
    assert all(end <= cfg.splits.holdout_start for end in calls)  # research never saw it
    rep = evaluate_overlay_plan(led, run_id, h, cfg, loader=loader_for(CRASH_CANDLES))
    assert rep["pre_holdout_passed"] is True, rep["rows"]
    out = check(led, run_id, MA_OVERLAY, cfg, calls=calls)
    assert out == {"passed": True, "checks_used": 1, "checks_left": 0}
    assert calls[-1] > cfg.splits.holdout_start
    hold = [r for r in led.list(run_id=run_id, limit=10_000) if r.window_kind == "holdout"]
    assert len(hold) == 1 and hold[0].plan_hash == h and hold[0].status == "ok"
    assert hold[0].window_start >= cfg.splits.holdout_start
    ev = led.evidence(run_id, plan_hash=h, kind="holdout")
    assert ev[-1]["payload"] == {"passed": True, "logic_hash": logic_hash(MA_OVERLAY)}
    after = evaluate_overlay_plan(led, run_id, h, cfg, loader=loader_for(CRASH_CANDLES))
    row = next(r for r in after["rows"] if r["criterion"] == "require_holdout_pass")
    assert row["status"] == "pass" and after["passed"] is True
    # one check per hypothesis: a second call is refused and spends nothing
    with pytest.raises(HoldoutRefusedError, match="already holdout-checked"):
        check(led, run_id, MA_OVERLAY, cfg)
    assert led.holdout_checks_used(run_id) == 1


def test_a_calm_holdout_cannot_confirm_a_drawdown_benefit(led: Ledger, tmp_path: Path) -> None:
    """Same design window; the holdout is a steady climb, so buy-and-hold's holdout drawdown is
    below episode_depth_min: FAIL (cannot confirm), still pass/fail only."""
    candles = crash_candles(holdout_drift=0.002)
    cfg = overlay_cfg(tmp_path)
    run_id, h = ready_overlay(led, tmp_path, cfg, candles)
    assert evaluate_overlay_plan(led, run_id, h, cfg, loader=loader_for(candles))[
        "pre_holdout_passed"
    ]
    crit = frozen_overlay_criteria(led, run_id)
    window = Window(
        start=cfg.splits.holdout_start,
        end=candles.index[-1] + pd.Timedelta("4h"),
        kind="holdout",
    )
    _, view = overlay_holdout_view(parse_plan(MA_OVERLAY), candles, window, cfg, crit)
    assert view["max_drawdown"]["buy_hold"] < crit.episode_depth_min
    out = check(led, run_id, MA_OVERLAY, cfg, candles=candles)
    assert out == {"passed": False, "checks_used": 1, "checks_left": 0}
    assert set(out) == ONLY_PASS_FAIL


VIEW = {
    "avg_exposure": 0.7,
    "cdar_ratio_vs_buy_hold": 0.5,
    "cdar": {"overlay": 0.1, "buy_hold": 0.2, "exposure_matched": 0.15},
    "sharpe": {"overlay": 1.0, "buy_hold": 1.1},
    "max_drawdown": {"overlay": 0.15, "buy_hold": 0.35, "exposure_matched": 0.25},
}


@pytest.mark.parametrize(
    ("change", "passed"),
    [
        ({}, True),
        ({"max_drawdown": {"buy_hold": 0.29}}, False),  # cannot confirm
        ({"max_drawdown": {"buy_hold": 0.30}}, True),  # the depth bar is inclusive here
        ({"avg_exposure": 0.49}, False),
        ({"cdar_ratio_vs_buy_hold": 0.76}, False),
        ({"cdar": {"overlay": 0.15, "buy_hold": 0.2, "exposure_matched": 0.15}}, False),
        ({"sharpe": {"overlay": 0.89, "buy_hold": 1.1}}, False),  # gap -0.21
        ({"sharpe": {"overlay": 0.91, "buy_hold": 1.1}}, True),  # gap -0.19
        ({"sharpe": {"overlay": None, "buy_hold": 1.1}}, False),
    ],
)
def test_overlay_holdout_pass_rule(
    tmp_path: Path, led: Ledger, change: dict[str, Any], passed: bool
) -> None:
    cfg = overlay_cfg(tmp_path)
    crit = frozen_overlay_criteria(led, start_overlay_run(led, tmp_path, cfg))
    view = {**VIEW, **change}
    assert overlay_holdout_passed(view, crit) is passed


# --------------------------------------------------------------------------- refusals


def test_refused_without_spending_while_rows_are_not_testable(led: Ledger, tmp_path: Path) -> None:
    candles = trend_candles()  # no 30% drawdowns: not_testable
    cfg = overlay_cfg(tmp_path)
    run_id, _ = ready_overlay(led, tmp_path, cfg, candles)
    before = led.count(run_id)
    with pytest.raises(HoldoutRefusedError) as exc:
        check(led, run_id, MA_OVERLAY, cfg, candles=candles)
    msg = str(exc.value)
    assert "pre-holdout overlay rows" in msg and "no holdout check was spent" in msg
    assert "min_drawdown_episodes (not_testable)" in msg
    assert led.count(run_id) == before and led.holdout_checks_used(run_id) == 0


def test_refused_without_a_matched_crosscheck(led: Ledger, tmp_path: Path) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id, _ = ready_overlay(led, tmp_path, cfg, crosscheck=False)
    with pytest.raises(HoldoutRefusedError, match="matched event-driven cross-check first"):
        check(led, run_id, MA_OVERLAY, cfg)
    assert led.holdout_checks_used(run_id) == 0


def test_the_question_is_checked_before_anything_else(led: Ledger, tmp_path: Path) -> None:
    cfg = overlay_cfg(tmp_path)
    edge = start_edge_run(led, tmp_path, cfg)
    overlay = start_overlay_run(led, tmp_path, cfg)
    calls: list[pd.Timestamp] = []
    with pytest.raises(HoldoutRefusedError, match="asks the edge question"):
        check(led, edge, MA_OVERLAY, cfg, calls=calls)
    with pytest.raises(HoldoutRefusedError, match="asks the risk-overlay question"):
        check(led, overlay, EMA_PLAN, cfg, calls=calls)
    assert calls == [] and led.holdout_checks_used(edge) == led.holdout_checks_used(overlay) == 0


# --------------------------------------------------------------------------- holdout death


def test_logic_spent_under_the_edge_question_is_dead_for_an_overlay(
    led: Ledger, tmp_path: Path
) -> None:
    """flat_when X and an edge plan entering on not (X) / exiting on X share one logic hash: an
    edge holdout check on it means the overlay can never be checked."""
    assert logic_hash(MA_OVERLAY) == logic_hash(MA_EDGE_SAME_LOGIC)
    cfg = overlay_cfg(tmp_path)
    spend_edge_holdout(led, tmp_path, cfg, MA_EDGE_SAME_LOGIC)
    run_id, _ = ready_overlay(led, tmp_path, cfg)
    with pytest.raises(HoldoutRefusedError, match="already holdout-checked") as exc:
        check(led, run_id, MA_OVERLAY, cfg)
    assert "either research question" in str(exc.value)
    assert led.holdout_checks_used(run_id) == 0
    rep = evaluate_overlay_plan(led, run_id, plan_hash(parse_plan(MA_OVERLAY)), cfg)
    row = next(r for r in rep["rows"] if r["criterion"] == "require_holdout_pass")
    assert row["status"] == "fail"  # the hypothesis's single check is used


def test_logic_spent_under_the_overlay_question_is_dead_for_an_edge_plan(
    led: Ledger, tmp_path: Path
) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id, _ = ready_overlay(led, tmp_path, cfg)
    assert check(led, run_id, MA_OVERLAY, cfg)["checks_used"] == 1
    edge = start_edge_run(led, tmp_path, cfg)
    with pytest.raises(HoldoutRefusedError, match="already holdout-checked"):
        check(led, edge, MA_EDGE_SAME_LOGIC, cfg)
    assert led.holdout_checks_used(edge) == 0


def test_an_overlay_holdout_kills_an_edge_plan_of_its_behaviour_family(
    led: Ledger, tmp_path: Path
) -> None:
    """A different logic that trades alike (returns correlated >= family_return_corr) is the
    same hypothesis: refused by the family guard, nothing spent."""
    assert logic_hash(MA_EDGE_ALIKE) != logic_hash(MA_OVERLAY)
    cfg = overlay_cfg(tmp_path)
    run_id, _ = ready_overlay(led, tmp_path, cfg)
    assert check(led, run_id, MA_OVERLAY, cfg)["checks_used"] == 1
    edge = start_edge_run(led, tmp_path, cfg)
    backtest_evidence(
        MA_EDGE_ALIKE, run_id=edge, cfg=cfg, ledger=led, loader=loader_for(CRASH_CANDLES)
    )
    with pytest.raises(HoldoutRefusedError, match="same behaviour family"):
        check(led, edge, MA_EDGE_ALIKE, cfg)
    assert led.holdout_checks_used(edge) == 0


def test_an_edge_holdout_kills_an_overlay_of_its_behaviour_family(
    led: Ledger, tmp_path: Path
) -> None:
    cfg = overlay_cfg(tmp_path)
    edge = spend_edge_holdout(led, tmp_path, cfg, MA_EDGE_ALIKE)
    # the edge plan's design-window trial is what puts the overlay in its family
    backtest_evidence(
        MA_EDGE_ALIKE, run_id=edge, cfg=cfg, ledger=led, loader=loader_for(CRASH_CANDLES)
    )
    run_id, _ = ready_overlay(led, tmp_path, cfg)
    with pytest.raises(HoldoutRefusedError, match="same behaviour family") as exc:
        check(led, run_id, MA_OVERLAY, cfg)
    assert "under either research question" in str(exc.value)
    assert led.holdout_checks_used(run_id) == 0
