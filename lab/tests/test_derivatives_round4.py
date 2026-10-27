"""Phase 12 items 3-4, quant verify round 4 (PLAN.md Phase 12):

1. The ``neighbourhood_pct`` criterion builds its EXPECTED neighbours with the same
   ``[derivatives] warmup_days`` look-back caps the runner clips at, so a derivatives period at
   the cap (funding_zscore 180 on 4h) gets a complete row; the clipped side is labelled.
2. Sweeps and walk-forward folds run the gate-base check on every trial's params (scored design
   window): a trial whose price clause is ~always true is a ledgered failed trial (counts in
   DSR's N), never selected.
3. Derivatives plans' backtest reports carry an informational ``benchmarks.ungated`` baseline
   (the plan without its derivatives entry clauses, same costs and scored window): no ledger
   row, no new criterion; price-only reports are unchanged.

Offline: seeded synthetic candles, tmp ledgers and tmp archive directories.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import lab.runner as runner
from lab.config import LabConfig
from lab.engine.splits import walk_forward_folds
from lab.ledger import SELECTION_SOURCES, Ledger
from lab.plan import compile_plan, compute_features, parse_plan, plan_hash, with_params
from lab.report import metrics_dict
from lab.research.contract import SuccessCriteria, load_research_config
from lab.research.criteria import CRITERION_PARAMS, _neighbourhood_row, ungated_baseline
from lab.runner import (
    GateBaseTrialError,
    gate_base_trial_check,
    plan_design_window,
    run_backtest_report,
    ungated_plan,
)
from lab.search.optuna_search import (
    STUDY_KEY,
    evaluate_on_window,
    optuna_search,
    walk_forward_optimize,
)
from lab.stats import compute_metrics
from lab.types import Window
from tests import test_mcp_server as mcp
from tests.test_derivatives_scoring import (
    CRASH_DRV,
    PRICE_GATED,
    PRICE_ONLY,
    loader_of,
    write_archive,
)
from tests.test_research_derivatives_snapshot import GATED_PLAN, drv_lab
from tests.test_runner import small_cfg


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    out = small_cfg(tmp_path)
    write_archive(out)  # every derivatives series starts 2019-06-01
    return out


def history(cfg: LabConfig) -> pd.DataFrame:
    return CRASH_DRV.loc[CRASH_DRV.index < cfg.splits.holdout_start]


def criteria(tmp_path: Path, **over: object) -> SuccessCriteria:
    path = mcp.write_criteria(tmp_path / "criteria.toml", **over)
    return load_research_config(path).criteria


def gated_fz(period: int) -> dict[str, Any]:
    return {
        **PRICE_GATED,
        "name": f"ema_trend_fz{period}",
        "features": {
            "ema_slow": {"ema": {"period": 50}},
            "fz": {"funding_zscore": {"period": period}},
        },
    }


def neighbour_periods(ledger: Ledger, run_id: str) -> list[int]:
    """The fz periods of the neighbourhood rows that perturb ``fz.period``."""
    out = []
    for r in ledger.list(run_id=run_id, limit=ledger.count(run_id)):
        params = json.loads(r.params_json)
        if r.source == "neighbourhood" and "fz.period" in params:
            out.append(int(params["fz.period"]))
    return sorted(out)


# =========================================================================== 1. neighbourhood


def test_a_derivatives_period_at_the_cap_gets_a_complete_neighbourhood_row(
    cfg: LabConfig, tmp_path: Path
) -> None:
    """Before the fix the criterion expected fz 198 / 216 (uncapped) while the runner clipped
    them to 180 = base and skipped them: the row was permanently missing."""
    plan = gated_fz(180)
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_backtest_report(
            plan, cfg=cfg, ledger=ledger, run_id="t", loader=loader_of(CRASH_DRV)
        )
        assert neighbour_periods(ledger, "t") == [144, 162]  # nothing above the cap
        row = _neighbourhood_row(
            criteria(tmp_path, neighbourhood_pct=0.2),
            ledger,
            "t",
            report["plan_hash"],
            report,
            cfg,
        )
    assert row["status"] in ("pass", "fail"), row
    note = row["note"]
    assert "clipped at the [derivatives] warmup_days look-back cap" in note
    assert note.split("look-back cap")[1].startswith(" (no neighbour beyond it): fz.period +20%")
    assert "validator bound" not in note  # the cap is not a registry bound
    assert "every tunable has both sides" in note


def test_a_period_below_the_cap_expects_the_cap_as_its_plus_neighbour(
    cfg: LabConfig, tmp_path: Path
) -> None:
    plan = gated_fz(160)  # +10% = 176, +20% = 192 -> clipped to the cap 180
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_backtest_report(
            plan, cfg=cfg, ledger=ledger, run_id="t", loader=loader_of(CRASH_DRV)
        )
        assert neighbour_periods(ledger, "t") == [128, 144, 176, 180]
        row = _neighbourhood_row(
            criteria(tmp_path, neighbourhood_pct=0.2),
            ledger,
            "t",
            report["plan_hash"],
            report,
            cfg,
        )
    assert row["status"] in ("pass", "fail"), row
    assert "clipped" not in row["note"]  # 180 != 160: the + side exists, nothing clipped


def test_a_capped_neighbour_that_was_never_run_is_still_missing(
    cfg: LabConfig, tmp_path: Path
) -> None:
    """The cap only aligns expected with run; it never turns an absent neighbour into a pass."""
    plan = gated_fz(160)
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_backtest_report(
            plan, cfg=cfg, ledger=ledger, run_id="t", loader=loader_of(CRASH_DRV)
        )
        # the same evidence judged under a run whose neighbour rows were never written
        row = _neighbourhood_row(
            criteria(tmp_path, neighbourhood_pct=0.2),
            ledger,
            "other",
            report["plan_hash"],
            report,
            cfg,
        )
    assert row["status"] == "missing"


# =========================================================================== 2. per-trial gate


# `r < 70` is true on 68-87% of the scored bars for rsi periods 2..6 and on >= 91% from 8 up
# (the smoother RSI rarely leaves the middle band), so the sweep's space straddles the 0.9 bar.
RSI_GATED: dict[str, Any] = {
    "name": "rsi_funding_gate",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"r": {"rsi": {"period": 3}}, "fz": {"funding_zscore": {"period": 30}}},
    "long_entry": "r < 70 and fz < 2",
    "long_exit": "r > 80",
    "risk": {"position_size_pct": 100},
}
RSI_SPACE = {"r.period": (2.0, 20.0)}


def price_share(cfg: LabConfig, period: int, window: Window) -> float:
    """Independent oracle: share of scored bars (where RSI is defined) with `r < 70`."""
    hist = history(cfg)
    plan = with_params(parse_plan(RSI_GATED), {"r.period": float(period)})
    r = compute_features(plan, hist)["r"]
    inside = (hist.index >= window.start) & (hist.index < window.end) & r.notna().to_numpy()
    return float((r[inside] < 70).mean())


def trial_period(params_json: str) -> int:
    return int(json.loads(params_json)["r.period"])


def test_sweep_trials_whose_price_clause_is_always_true_are_ledgered_failed(
    cfg: LabConfig, tmp_path: Path
) -> None:
    hist = history(cfg)
    plan = parse_plan(RSI_GATED)
    window, scoring = plan_design_window(plan, hist, cfg)
    assert scoring is not None and scoring["price_clause_true_share"] < 0.9  # base passes
    limit = cfg.derivatives.gate_base_max_true_frac
    with Ledger(tmp_path / "l.db") as ledger:
        res = optuna_search(
            plan,
            hist,
            window,
            ledger=ledger,
            run_id="t",
            cfg=cfg,
            n_trials=24,
            seed=3,
            space=RSI_SPACE,
            min_trades=5,
            require_space=True,
            trial_check=gate_base_trial_check(hist, window, cfg),
        )
        rows = [ledger.get(i) for i in res.trial_ids]
        n_distinct = ledger.count_distinct_plans_research(sources=SELECTION_SOURCES)
    refused = [r for r in rows if price_share(cfg, trial_period(r.params_json), window) >= limit]
    allowed = [r for r in rows if r not in refused]
    assert refused and allowed  # the space straddles the bar
    for r in refused:
        assert r.status == "error" and r.source == "optuna"
        assert r.error is not None and "GateBaseTrialError" in r.error
        assert "gate-base check failed for these params" in r.error
        assert r.metrics_json == "{}" and r.returns is None  # no backtest was run
        assert STUDY_KEY in json.loads(r.params_json)
    assert all(r.status == "ok" for r in allowed)
    assert res.n_failed >= len(refused)
    assert res.best_params is not None
    assert price_share(cfg, int(res.best_params["r.period"]), window) < limit
    # every failed configuration still counts in DSR's N
    assert n_distinct == len({r.plan_hash for r in rows})


def test_walk_forward_never_selects_a_trial_that_fails_the_gate_base_check(
    cfg: LabConfig, tmp_path: Path
) -> None:
    hist = history(cfg)
    plan = parse_plan(RSI_GATED)
    window, _ = plan_design_window(plan, hist, cfg)
    folds = walk_forward_folds(window, train_months=3, test_months=2, step_months=2, anchored=False)
    assert len(folds) >= 2
    limit = cfg.derivatives.gate_base_max_true_frac
    with Ledger(tmp_path / "l.db") as ledger:
        wf = walk_forward_optimize(
            plan,
            hist,
            folds,
            ledger=ledger,
            run_id="t",
            cfg=cfg,
            n_trials_per_fold=12,
            seed=5,
            space=RSI_SPACE,
            min_trades=3,
            require_space=True,
            trial_check=gate_base_trial_check(hist, window, cfg),
        )
        train = [
            r
            for r in ledger.list(run_id="t", limit=ledger.count("t"))
            if r.window_kind == "walk_forward_train"
        ]
    assert any(r.status == "error" and "GateBaseTrialError" in (r.error or "") for r in train)
    for fold in wf.folds:
        assert price_share(cfg, int(fold.best_params["r.period"]), window) < limit


def test_the_trial_check_is_a_no_op_for_plans_without_derivatives_gates(cfg: LabConfig) -> None:
    hist = history(cfg)
    plan = parse_plan(PRICE_ONLY)
    check = gate_base_trial_check(hist, plan_design_window(plan, hist, cfg)[0], cfg)
    check(plan)  # no raise


def test_the_trial_check_raises_the_trial_error_with_the_share(cfg: LabConfig) -> None:
    hist = history(cfg)
    plan = parse_plan(RSI_GATED)
    window, _ = plan_design_window(plan, hist, cfg)
    check = gate_base_trial_check(hist, window, cfg)
    check(plan)  # period 3: 76% passes
    with pytest.raises(GateBaseTrialError, match=r"`r < 70` is true on 9\d\.\d% of the"):
        check(with_params(plan, {"r.period": 20.0}))


def test_mcp_sweep_ledgers_gate_failing_trials_as_failed(tmp_path: Path) -> None:
    lab = drv_lab(tmp_path)
    run_id = lab.start()
    plan = {
        **GATED_PLAN,
        "features": {**GATED_PLAN["features"], "r": {"rsi": {"period": 3}}},
        # on these trending candles `r > 30` is true on 84% of the bars at period 3 (the base
        # passes) and on >= 93% from period 6 up
        "long_entry": "r > 30 and fz < 2",
        "long_exit": "r > 80",
    }
    plan["features"].pop("ema_fast")
    plan["features"].pop("ema_slow")
    out = mcp.ok(
        lab.server,
        "run_sweep",
        {
            "run_id": run_id,
            "plan": plan,
            "space": {"r.period": {"low": 2, "high": 30}},
            "n_trials": 16,
        },
    )
    with lab.ledger() as led:
        rows = [r for r in led.list(run_id=run_id, limit=led.count(run_id)) if r.source == "optuna"]
    gate_failed = [r for r in rows if "GateBaseTrialError" in (r.error or "")]
    assert gate_failed and all(r.status == "error" for r in gate_failed)
    failed_periods = {trial_period(r.params_json) for r in gate_failed}
    assert out["best_params"] is None or int(out["best_params"]["r.period"]) not in failed_periods
    assert out["n_failed"] >= len(gate_failed)


# =========================================================================== 3. ungated baseline


def run_report(cfg: LabConfig, ledger: Ledger, plan: dict[str, Any]) -> dict[str, Any]:
    return run_backtest_report(
        plan, cfg=cfg, ledger=ledger, run_id="t", loader=loader_of(CRASH_DRV)
    )


def test_ungated_metrics_equal_an_independent_run_of_the_stripped_plan(
    cfg: LabConfig, tmp_path: Path
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_report(cfg, ledger, PRICE_GATED)
    block = report["benchmarks"]["ungated"]
    # the price clause alone; the gate feature, unused now, is dropped
    stripped = parse_plan({**PRICE_ONLY, "name": PRICE_GATED["name"]})
    assert parse_plan(block["plan"]) == stripped
    assert block["price_clause"] == "close > ema_slow" and block["gate_features"] == ["fz"]
    window = Window(
        start=pd.Timestamp(report["window"]["start"], unit="s", tz="UTC"),
        end=pd.Timestamp(report["window"]["end"], unit="s", tz="UTC"),
        kind="design",
    )
    independent = evaluate_on_window(stripped, history(cfg), window, cfg)
    assert block["metrics"] == metrics_dict(compute_metrics(independent, "4h"))
    net = report["metrics"]["net"]
    diff = block["plan_minus_ungated"]
    assert diff["sharpe"] == pytest.approx(net["sharpe"] - block["metrics"]["sharpe"])
    assert diff["max_drawdown"] == pytest.approx(
        net["max_drawdown"] - block["metrics"]["max_drawdown"]
    )
    # binding share from the compiled signals, counted independently
    hist = history(cfg).loc[lambda f: f.index < window.end]
    idx = hist.index[(hist.index >= window.start)]
    gated = compile_plan(parse_plan(PRICE_GATED), hist).entries.loc[idx].to_numpy()
    price = compile_plan(stripped, hist).entries.loc[idx].to_numpy()
    blocked = int((price & ~gated).sum())
    assert block["price_clause_entry_bars"] == int(price.sum())
    assert block["gate_binding_bars"] == blocked > 0
    assert block["gate_binding_share"] == pytest.approx(blocked / int(price.sum()))
    assert 0 < block["gate_binding_share"] < 1


def test_the_ungated_baseline_adds_no_ledger_row_and_no_dsr_trial(
    cfg: LabConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Ledger(tmp_path / "a.db") as a:
        with_block = run_report(cfg, a, PRICE_GATED)
        rows_a = a.list(run_id="t", limit=a.count("t"))
        n_a = a.count_distinct_plans_research(sources=SELECTION_SOURCES)
    monkeypatch.setattr(runner, "_ungated_benchmark", lambda *_a, **_k: None)
    with Ledger(tmp_path / "b.db") as b:
        without = run_report(cfg, b, PRICE_GATED)
        rows_b = b.list(run_id="t", limit=b.count("t"))
        n_b = b.count_distinct_plans_research(sources=SELECTION_SOURCES)
    assert "ungated" in with_block["benchmarks"] and "ungated" not in without["benchmarks"]
    assert len(rows_a) == len(rows_b) and n_a == n_b
    assert [(r.source, r.plan_hash) for r in rows_a] == [(r.source, r.plan_hash) for r in rows_b]
    ungated_hash = plan_hash(parse_plan(with_block["benchmarks"]["ungated"]["plan"]))
    assert all(r.plan_hash != ungated_hash for r in rows_a)
    for key in ("metrics", "stats", "walk_forward", "trades"):
        assert with_block[key] == without[key]


def test_a_gate_that_never_binds_has_share_zero_and_identical_metrics(
    cfg: LabConfig, tmp_path: Path
) -> None:
    never = {
        **PRICE_ONLY,
        "name": "ema_trend_funding_noop",
        "features": {"ema_slow": {"ema": {"period": 50}}, "fr": {"funding_rate": {}}},
        "long_entry": "close > ema_slow and fr > -1",
    }
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_report(cfg, ledger, never)
    block = report["benchmarks"]["ungated"]
    assert block["gate_binding_bars"] == 0 and block["gate_binding_share"] == 0.0
    assert block["price_clause_entry_bars"] > 0
    assert block["metrics"] == report["metrics"]["net"]
    assert block["plan_minus_ungated"] == {"sharpe": 0.0, "max_drawdown": 0.0, "total_return": 0.0}


def test_price_only_reports_have_no_ungated_block(cfg: LabConfig, tmp_path: Path) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_report(cfg, ledger, PRICE_ONLY)
    assert set(report["benchmarks"]) == {"buy_and_hold", "vol_target", "random_entry"}
    assert ungated_baseline(report) is None
    assert ungated_plan(parse_plan(PRICE_ONLY)) is None


def test_get_report_shows_the_baseline_as_information_never_as_a_criterion(
    tmp_path: Path,
) -> None:
    lab = drv_lab(tmp_path)
    run_id = lab.start()
    bt = mcp.ok(lab.server, "run_backtest", {"run_id": run_id, "plan": GATED_PLAN})
    assert "ungated" in bt["benchmarks"]
    report = mcp.ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": bt["plan_hash"]})
    names = [r["criterion"] for r in report["rows"]]
    assert names == [f.name for f in fields(SuccessCriteria) if f.name not in CRITERION_PARAMS]
    info = report["informational"]["ungated_baseline"]
    assert info["price_clause"] == "ema_fast > ema_slow"
    for side in ("plan", "ungated"):
        assert set(info[side]) == {"sharpe", "total_return", "max_drawdown", "n_trades", "exposure"}
    assert info["plan"]["sharpe"] == bt["metrics"]["net"]["sharpe"]
    assert info["ungated"]["sharpe"] == bt["benchmarks"]["ungated"]["metrics"]["sharpe"]
    assert "NOT a success criterion" in info["note"]
    assert set(info["plan_minus_ungated"]) == {"sharpe", "max_drawdown", "total_return"}


def test_evaluate_plan_of_a_price_only_plan_has_no_informational_block(tmp_path: Path) -> None:
    lab = mcp.Lab(tmp_path)
    run_id = lab.start()
    bt = mcp.ok(lab.server, "run_backtest", {"run_id": run_id, "plan": mcp.TREND_PLAN})
    report = mcp.ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": bt["plan_hash"]})
    assert "informational" not in report
    assert "ungated" not in bt["benchmarks"]
