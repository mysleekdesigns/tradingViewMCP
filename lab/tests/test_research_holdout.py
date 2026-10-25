"""run_holdout_check: guards refuse before spending, the server-side budget, pass/fail on a
known synthetic market, the returned shape (no metrics), the ledger row, and no look-ahead
inside the holdout evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.config import LabConfig
from lab.ledger import Ledger
from lab.plan import parse_plan, plan_hash, plan_to_dict, with_params
from lab.plan.validator import PlanValidationError
from lab.research.contract import load_research_config
from lab.research.evidence import record_report
from lab.research.holdout import (
    HOLDOUT_SOURCE,
    HoldoutRefusedError,
    holdout_min_trades,
    run_holdout_check,
)
from lab.research.logic import logic_hash
from lab.research.runs import start_research_run
from lab.runner import DataUnavailableError
from tests.synthetic import perturb_after
from tests.test_mcp_server import (
    HOLDOUT_PASS_CANDLES,
    LAX,
    TREND_PLAN,
    loader_for,
    small_cfg,
    trend_candles,
    write_criteria,
)
from tests.test_research_criteria import WINDOW, adversarial_payload, backtest_payload, trial

UP = trend_candles()  # a clean trend: TREND_PLAN makes 1 holdout trade (too few to pass)
DOWN = trend_candles(holdout_drift=-0.003)
PASS = HOLDOUT_PASS_CANDLES  # TREND_PLAN: 7 holdout trades, net Sharpe ~2, positive return


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def start(
    ledger: Ledger, tmp_path: Path, *, lab_cfg: LabConfig | None = None, **criteria: object
) -> str:
    path = write_criteria(tmp_path / f"c{len(ledger.research_runs())}.toml", **criteria)
    # small_cfg(tmp_path) has the same lab values as the ``cfg`` fixture (only data_dir,
    # which is not fingerprinted, differs), so the run's lab.toml fingerprint matches it.
    return start_research_run(
        ledger, load_research_config(path), label="t", cfg=lab_cfg or small_cfg(tmp_path)
    )


def crosscheck_payload(matched: bool = True, available: bool = True) -> dict[str, Any]:
    return {
        "matched": matched,
        "available": available,
        "reason": None if available else "nautilus_trader is not installed",
        "n_trades_vectorbt": 10,
        "n_trades_event": 10,
        "matched_trades": 10 if matched else 4,
        "return_diff_pct": 0.1,
        "notes": [],
    }


def seed_pre_holdout(
    ledger: Ledger, run_id: str, cfg: LabConfig, *, crosscheck: bool = True
) -> str:
    """Hand-built evidence that passes every pre-holdout row of ``LAX`` for TREND_PLAN: the base
    trial, a ``min_pbo_trials``-trial sweep (so the PBO and the DSR's V[SR] have their floors),
    adversarial and walk-forward evidence, and (``crosscheck``) a matched cross-check."""
    p = parse_plan(TREND_PLAN)
    rng = np.random.default_rng(11)
    base = trial(ledger, run_id, rng.normal(0.002, 0.01, 600), plan_dict=TREND_PLAN, cfg=cfg)
    study = f"S-{run_id}"
    for k in range(cfg.research.min_pbo_trials):
        variant = plan_to_dict(with_params(p, {"ema_fast.period": 30 + k}))
        params = json.dumps({"ema_fast.period": 30 + k, "_study": {"study": study}})
        trial(
            ledger,
            run_id,
            rng.normal(0.0, 0.01, 600),
            plan_dict=variant,
            source="optuna",
            cfg=cfg,
            params_json=params,
        )
    h = plan_hash(p)
    record_report(
        ledger, run_id, h, "backtest", backtest_payload(base, plan=plan_to_dict(p), plan_hash=h)
    )
    # the sweep-CSCV PBO basis (required): the study of the Optuna trials above
    record_report(
        ledger,
        run_id,
        h,
        "sweep",
        {"study": study, "window": WINDOW, "logic_hash": logic_hash(p), "plan": plan_to_dict(p)},
    )
    record_report(ledger, run_id, h, "adversarial", adversarial_payload())
    record_report(ledger, run_id, h, "walk_forward", {"oos_metrics": {"sharpe": 1.0}})
    if crosscheck:
        record_report(ledger, run_id, h, "crosscheck", crosscheck_payload())
    return h


def holdout_rows(ledger: Ledger, run_id: str) -> list[Any]:
    return [r for r in ledger.list(run_id=run_id, limit=10_000) if r.window_kind == "holdout"]


# --------------------------------------------------------------------------- refusals


def test_invalid_plan_is_rejected_before_anything(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path, **LAX)
    with pytest.raises(PlanValidationError):
        run_holdout_check({**TREND_PLAN, "interval": "7m"}, run_id=run_id, cfg=cfg, ledger=ledger)
    assert ledger.holdout_checks_used(run_id) == 0


def test_unknown_run_is_refused(ledger: Ledger, cfg: LabConfig) -> None:
    with pytest.raises(HoldoutRefusedError, match="never started"):
        run_holdout_check(TREND_PLAN, run_id="nope", cfg=cfg, ledger=ledger, loader=loader_for(UP))


def test_refused_without_spending_when_pre_holdout_criteria_fail(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path, **LAX)
    calls: list[pd.Timestamp] = []
    with pytest.raises(HoldoutRefusedError, match="pre-holdout") as info:
        run_holdout_check(
            TREND_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(UP, calls)
        )
    assert "dsr_min (missing)" in str(info.value)
    assert ledger.holdout_checks_used(run_id) == 0 and ledger.count(run_id) == 0
    assert calls == []  # no holdout bar was even loaded


def test_refused_when_the_budget_is_exhausted(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path, **LAX)  # max_holdout_checks = 1
    seed_pre_holdout(ledger, run_id, cfg)
    ledger.record_holdout_check(run_id, "e" * 64, budget=1)  # another plan spent it
    with pytest.raises(HoldoutRefusedError, match="used 1 of 1"):
        run_holdout_check(TREND_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(UP))
    assert ledger.holdout_checks_used(run_id) == 1
    assert holdout_rows(ledger, run_id) == []
    assert ledger.evidence(run_id, kind="holdout") == []


def test_lab_toml_cap_applies_on_top_of_the_run_budget(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    from dataclasses import replace

    capped = replace(cfg, splits=replace(cfg.splits, holdout_budget=0))
    run_id = start(ledger, tmp_path, lab_cfg=capped, **LAX)
    seed_pre_holdout(ledger, run_id, cfg)
    with pytest.raises(HoldoutRefusedError, match="used 0 of 0"):
        run_holdout_check(
            TREND_PLAN, run_id=run_id, cfg=capped, ledger=ledger, loader=loader_for(UP)
        )
    assert ledger.holdout_checks_used(run_id) == 0


def test_no_holdout_data_refuses_without_spending(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path, **LAX)
    seed_pre_holdout(ledger, run_id, cfg)
    early = UP.loc[UP.index < cfg.splits.holdout_start]
    with pytest.raises(DataUnavailableError, match="no BTCUSDT 4h bars after holdout_start"):
        run_holdout_check(
            TREND_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(early)
        )
    assert ledger.holdout_checks_used(run_id) == 0


# --------------------------------------------------------------------------- outcomes


def test_pass_returns_only_pass_fail_and_budget(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path, **LAX)
    h = seed_pre_holdout(ledger, run_id, cfg)
    n_before = ledger.count_distinct_plans(run_id, interval="4h")
    out = run_holdout_check(
        TREND_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(PASS)
    )
    assert out == {"passed": True, "checks_used": 1, "checks_left": 0}
    (ev,) = ledger.evidence(run_id, kind="holdout")
    assert ev["plan_hash"] == h
    assert ev["payload"] == {"passed": True, "logic_hash": logic_hash(TREND_PLAN)}
    (row,) = holdout_rows(ledger, run_id)
    assert row.source == HOLDOUT_SOURCE and row.plan_hash == h and row.status == "ok"
    assert row.window_start == cfg.splits.holdout_start
    assert row.window_end == PASS.index[-1] + pd.Timedelta(hours=4)
    assert json.loads(row.metrics_json)["n_trades"] >= 5
    assert ledger.count_distinct_plans(run_id, interval="4h") == n_before  # DSR N unchanged


def test_fail_is_final_for_the_hypothesis(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> None:
    run_id = start(ledger, tmp_path, **LAX, max_holdout_checks=3)
    seed_pre_holdout(ledger, run_id, cfg)
    out = run_holdout_check(
        TREND_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(DOWN)
    )
    assert out == {"passed": False, "checks_used": 1, "checks_left": 2}  # no rule names
    (ev,) = ledger.evidence(run_id, kind="holdout")
    assert ev["payload"] == {"passed": False, "logic_hash": logic_hash(TREND_PLAN)}
    with pytest.raises(HoldoutRefusedError, match="already holdout-checked"):
        run_holdout_check(TREND_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(UP))
    assert ledger.holdout_checks_used(run_id) == 1


def test_any_failing_pre_holdout_row_blocks_the_check(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path, **{**LAX, "max_drawdown_max": 0.001})
    h = seed_pre_holdout(ledger, run_id, cfg)
    # the hand-built evidence says max drawdown 0.2, so pre-holdout must fail first
    with pytest.raises(HoldoutRefusedError, match="max_drawdown_max"):
        run_holdout_check(TREND_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(UP))
    assert ledger.holdout_checks_used(run_id) == 0
    assert ledger.evidence(run_id, plan_hash=h, kind="holdout") == []


def test_holdout_evaluation_has_no_look_ahead(tmp_path: Path, cfg: LabConfig) -> None:
    """Perturb every bar after holdout bar t: the holdout trial's returns through t are
    bit-identical (signals at t use closes <= t, fills at t+1's open)."""
    t = int(np.searchsorted(UP.index, cfg.splits.holdout_start)) + 200
    returns = []
    for k, candles in enumerate((UP, perturb_after(UP, t))):
        with Ledger(tmp_path / f"l{k}.db") as led:
            (tmp_path / f"d{k}").mkdir()
            run_id = start(led, tmp_path / f"d{k}", **LAX)
            seed_pre_holdout(led, run_id, cfg)
            run_holdout_check(
                TREND_PLAN, run_id=run_id, cfg=cfg, ledger=led, loader=loader_for(candles)
            )  # pass or fail does not matter here, only the recorded returns
            (row,) = [
                r
                for r in led.list(run_id=run_id, limit=10_000, include_returns=True)
                if r.window_kind == "holdout"
            ]
            returns.append(row.returns)
    upto = t - int(np.searchsorted(UP.index, cfg.splits.holdout_start)) + 1
    assert returns[0] is not None and returns[1] is not None
    np.testing.assert_array_equal(returns[0][:upto], returns[1][:upto])
    assert not np.array_equal(returns[0], returns[1])  # the perturbation did change the tail


# --------------------------------------------------------------------------- Phase 8 review fixes


def test_holdout_min_trades_scales_the_design_bar_with_a_floor() -> None:
    assert holdout_min_trades(100, 2190, 8760) == 25  # a quarter of the bars, a quarter of 100
    assert holdout_min_trades(100, 2191, 8760) == 26  # ceil
    assert holdout_min_trades(10, 500, 5000) == 5  # floor of 5 trades
    assert holdout_min_trades(0, 500, 5000) == 5
    assert holdout_min_trades(100, 500, 0) == 5


def test_too_few_holdout_trades_fail_even_when_profitable(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """UP is a clean trend: TREND_PLAN holds through it with 1 trade (net Sharpe ~8, return
    > 0). One trade is not evidence, so the check fails (min_trades 0 -> floor of 5)."""
    run_id = start(ledger, tmp_path, **LAX)
    seed_pre_holdout(ledger, run_id, cfg)
    out = run_holdout_check(
        TREND_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(UP)
    )
    assert out == {"passed": False, "checks_used": 1, "checks_left": 0}
    (row,) = holdout_rows(ledger, run_id)
    metrics = json.loads(row.metrics_json)
    assert metrics["n_trades"] < 5 and metrics["total_return"] > 0 and metrics["sharpe"] > 0.5


def _cosmetic_copy() -> dict[str, Any]:
    """TREND_PLAN renamed, re-described, aliases renamed, periods re-tuned, rules re-spaced."""
    return {
        **TREND_PLAN,
        "name": "totally_new_idea",
        "description": "a fresh hypothesis, honest",
        "features": {"quick": {"ema": {"period": 25}}, "slow_line": {"ema": {"period": 60}}},
        "long_entry": "quick   >  slow_line",
        "long_exit": "quick<slow_line",
    }


def test_a_renamed_or_retuned_copy_cannot_get_a_second_check_in_the_same_run(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path, **{**LAX, "max_holdout_checks": 3})
    seed_pre_holdout(ledger, run_id, cfg)
    run_holdout_check(TREND_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(DOWN))
    copy = _cosmetic_copy()
    assert plan_hash(parse_plan(copy)) != plan_hash(parse_plan(TREND_PLAN))
    calls: list[pd.Timestamp] = []
    with pytest.raises(HoldoutRefusedError, match="already holdout-checked"):
        run_holdout_check(copy, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(UP, calls))
    assert ledger.holdout_checks_used(run_id) == 1 and calls == []


def test_a_new_run_cannot_buy_a_second_check_for_the_same_logic(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    first = start(ledger, tmp_path, **LAX)
    seed_pre_holdout(ledger, first, cfg)
    run_holdout_check(TREND_PLAN, run_id=first, cfg=cfg, ledger=ledger, loader=loader_for(DOWN))
    second = start(ledger, tmp_path, **LAX)
    seed_pre_holdout(ledger, second, cfg)  # passes pre-holdout in the new run too
    for plan in (TREND_PLAN, _cosmetic_copy()):
        with pytest.raises(HoldoutRefusedError, match="ever, in any run"):
            run_holdout_check(plan, run_id=second, cfg=cfg, ledger=ledger, loader=loader_for(UP))
    assert ledger.holdout_checks_used(second) == 0
    assert ledger.evidence(second, kind="holdout") == []
    # the criteria row of the new run says the hypothesis can never pass its holdout
    from lab.research.criteria import evaluate_plan

    rows = {
        r["criterion"]: r
        for r in evaluate_plan(ledger, second, plan_hash(parse_plan(TREND_PLAN)), cfg)["rows"]
    }
    assert rows["require_holdout_pass"]["status"] == "fail"


def test_a_different_logic_still_gets_its_own_check(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path, **{**LAX, "max_holdout_checks": 3})
    seed_pre_holdout(ledger, run_id, cfg)
    run_holdout_check(TREND_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(DOWN))
    other = {**TREND_PLAN, "long_exit": "ema_fast < ema_slow or close < ema_slow"}
    assert logic_hash(other) != logic_hash(TREND_PLAN)
    with pytest.raises(HoldoutRefusedError, match="pre-holdout"):  # its own guards apply
        run_holdout_check(other, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(UP))


def test_changed_lab_toml_refuses_the_check_without_spending(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    from dataclasses import replace

    run_id = start(ledger, tmp_path, **LAX)
    seed_pre_holdout(ledger, run_id, cfg)
    pricier = replace(cfg, costs=replace(cfg.costs, fee_rate=0.002))
    with pytest.raises(HoldoutRefusedError, match=r"lab\.toml changed since this run started"):
        run_holdout_check(
            TREND_PLAN, run_id=run_id, cfg=pricier, ledger=ledger, loader=loader_for(PASS)
        )
    assert ledger.holdout_checks_used(run_id) == 0 and holdout_rows(ledger, run_id) == []
