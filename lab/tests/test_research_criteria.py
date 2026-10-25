"""evaluate_plan rows (pass / fail / missing) on hand-built evidence, the DSR re-deflation with
the run's current trial count, the neighbourhood row from ledger trials, and the regime
breakdown on a hand-checkable series."""

from __future__ import annotations

import json
import math
from dataclasses import fields
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.config import LabConfig
from lab.ledger import Ledger, costs_to_json
from lab.plan import parse_plan, plan_hash, plan_to_dict, with_params
from lab.research.contract import SuccessCriteria, load_research_config
from lab.research.criteria import (
    CRITERION_PARAMS,
    PRE_HOLDOUT_EXCLUDED,
    evaluate_plan,
    frozen_criteria,
    regime_breakdown,
)
from lab.research.evidence import record_report
from lab.research.logic import logic_hash
from lab.research.runs import start_research_run
from lab.stats import deflated_sharpe, pbo_cscv, sharpe_per_bar
from lab.types import TrialRecord
from tests.test_mcp_server import EMA_PLAN, LAX, small_cfg, write_criteria

PLAN = parse_plan(EMA_PLAN)
H = plan_hash(PLAN)
W_START = pd.Timestamp("2018-07-01", tz="UTC")
W_END = pd.Timestamp("2019-10-01", tz="UTC")
WINDOW = {"kind": "design", "start": int(W_START.timestamp()), "end": int(W_END.timestamp())}


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def start(ledger: Ledger, tmp_path: Path, **criteria: object) -> str:
    path = write_criteria(tmp_path / f"c{len(ledger.research_runs())}.toml", **criteria)
    # small_cfg(tmp_path) has the same lab values as the ``cfg`` fixture (only data_dir,
    # which is not fingerprinted, differs), so the run's lab.toml fingerprint matches it.
    return start_research_run(
        ledger, load_research_config(path), label="t", cfg=small_cfg(tmp_path)
    )


def trial(
    ledger: Ledger,
    run_id: str,
    returns: np.ndarray,
    *,
    plan_dict: dict[str, Any] | None = None,
    source: str = "api",
    cfg: LabConfig,
    metrics: dict[str, float] | None = None,
    status: str = "ok",
    params_json: str = "{}",
    costs_json: str | None = None,
) -> int:
    p = parse_plan(plan_dict or EMA_PLAN)
    return ledger.record(
        TrialRecord(
            run_id=run_id,
            source=source,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            plan_hash=plan_hash(p),
            plan_json=json.dumps(plan_to_dict(p)),
            params_json=params_json,
            symbol=p.symbol,
            interval=p.interval,
            window_kind="design",
            window_start=W_START,
            window_end=W_END,
            metrics_json=json.dumps(metrics or {}),
            sharpe_per_bar=sharpe_per_bar(returns) if status == "ok" else None,
            n_trades=10,
            error=None if status == "ok" else "boom",
            costs_json=costs_json if costs_json is not None else costs_to_json(cfg.costs),
            returns=returns if status == "ok" else None,
        )
    )


def backtest_payload(trial_id: int | None = None, **over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trial_id": trial_id,
        "plan": plan_to_dict(PLAN),
        "plan_hash": H,
        "interval": "4h",
        "window": WINDOW,
        "metrics": {"net": {"sharpe": 1.5, "max_drawdown": -0.2, "n_trades": 120}},
        "benchmarks": {
            "buy_and_hold": {"sharpe": 1.0},
            "vol_target": {"sharpe": 1.2},
            "random_entry": {"n_paths": 200, "percentile": 97.0},
        },
        "stats": {
            "dsr": {"dsr": 0.99, "n_trials": 1},
            "pbo": {"pbo": 0.1, "n_trials": 5, "basis": "neighbourhood"},
        },
        "regimes": {
            "basis": "test",
            "regimes": {
                "bull": {"bars": 10, "active_bars": 8, "total_return": 0.2},
                "bear": {"bars": 10, "active_bars": 5, "total_return": 0.01},
                "sideways": {"bars": 10, "active_bars": 5, "total_return": -0.02},
            },
        },
    }
    payload.update(over)
    return payload


def net(sharpe: float, max_drawdown: float, n_trades: int) -> dict[str, Any]:
    return {
        "metrics": {"net": {"sharpe": sharpe, "max_drawdown": max_drawdown, "n_trades": n_trades}}
    }


def adversarial_payload(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "base": {"sharpe": 2.0, "status": "ok", "total_return": 0.5},
        "delay": {
            "rows": [
                {"delay": 1, "sharpe": 1.6},
                {"delay": 3, "sharpe": 1.2},
                {"delay": 5, "sharpe": 0.8},
            ]
        },
        "cost_stress": {
            "rows": [
                {"factor": 2.0, "status": "ok", "total_return": 0.2},
                {"factor": 4.0, "status": "ok", "total_return": -0.1},
            ]
        },
    }
    payload.update(over)
    return payload


def rows(report: Any) -> dict[str, Any]:
    return {r["criterion"]: r for r in report["rows"]}


# --------------------------------------------------------------------------- shape / missing


def test_no_evidence_means_every_required_row_is_missing(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    rep = evaluate_plan(ledger, run_id, H, cfg)
    criteria = [f.name for f in fields(SuccessCriteria) if f.name not in CRITERION_PARAMS]
    assert len(criteria) == 14  # PLAN.md's criteria; the regime definition is not a row
    assert [r["criterion"] for r in rep["rows"]] == criteria
    assert all(r["status"] == "missing" for r in rep["rows"])
    assert rep["missing"] == criteria
    assert rep["pre_holdout_passed"] is False and rep["passed"] is False
    assert rep["ledger_n"] == 0 and set(rep["budget"]) == {"budget", "used", "remaining"}


def test_unknown_run_raises(ledger: Ledger, cfg: LabConfig) -> None:
    with pytest.raises(ValueError, match="never started"):
        evaluate_plan(ledger, "nope", H, cfg)


def test_not_required_rows_pass(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> None:
    run_id = start(
        ledger,
        tmp_path,
        beat_buy_hold=False,
        beat_vol_target=False,
        require_gradual_delay_decay=False,
        min_profitable_regimes=0,
        neighbourhood_pct=0.0,
        require_holdout_pass=False,
        require_crosscheck_match=False,
    )
    r = rows(evaluate_plan(ledger, run_id, H, cfg))
    for name in (
        "beat_buy_hold",
        "beat_vol_target",
        "require_gradual_delay_decay",
        "min_profitable_regimes",
        "neighbourhood_pct",
        "require_holdout_pass",
        "require_crosscheck_match",
    ):
        assert r[name]["status"] == "pass" and "not required" in r[name]["note"], name
    assert r["dsr_min"]["status"] == "missing"


# --------------------------------------------------------------------------- backtest rows


def test_backtest_rows_pass_on_good_evidence(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "backtest", backtest_payload())
    r = rows(evaluate_plan(ledger, run_id, H, cfg))
    assert r["pbo_max"]["status"] == "missing" and "run_sweep first" in r["pbo_max"]["note"]
    for name in (
        "beat_buy_hold",
        "beat_vol_target",
        "random_entry_percentile_min",
        "max_drawdown_max",
        "min_trades",
        "min_profitable_regimes",
    ):
        assert r[name]["status"] == "pass", (name, r[name])
    assert r["max_drawdown_max"]["value"] == 0.2  # positive fraction
    assert r["min_profitable_regimes"]["value"] == 2.0


@pytest.mark.parametrize(
    ("over", "name"),
    [
        (
            net(0.9, -0.2, 120),
            "beat_buy_hold",
        ),
        (
            net(1.1, -0.2, 120),
            "beat_vol_target",
        ),
        (
            {"benchmarks": {"random_entry": {"n_paths": 200, "percentile": 80.0}}},
            "random_entry_percentile_min",
        ),
        (
            net(1.5, -0.3, 120),
            "max_drawdown_max",
        ),
        (net(1.5, -0.2, 99), "min_trades"),
        (
            {
                "regimes": {
                    "regimes": {
                        "bull": {"active_bars": 3, "total_return": 0.2},
                        "bear": {"active_bars": 3, "total_return": -0.1},
                        "sideways": {"active_bars": 3, "total_return": 0.0},
                    }
                }
            },
            "min_profitable_regimes",
        ),
    ],
)
def test_backtest_rows_fail(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, over: dict[str, Any], name: str
) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "backtest", backtest_payload(**over))
    assert rows(evaluate_plan(ledger, run_id, H, cfg))[name]["status"] == "fail"


def test_pbo_not_computed_and_no_regimes_are_missing(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    payload = backtest_payload(stats={"pbo": {"pbo": None, "n_trials": 1}})
    del payload["regimes"]
    record_report(ledger, run_id, H, "backtest", payload)
    r = rows(evaluate_plan(ledger, run_id, H, cfg))
    assert r["pbo_max"]["status"] == "missing"
    assert r["min_profitable_regimes"]["status"] == "missing"
    assert "no regime breakdown" in r["min_profitable_regimes"]["note"]


def test_latest_evidence_of_a_kind_wins(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "backtest", backtest_payload())
    bad = {"net": {"sharpe": 1.5, "max_drawdown": -0.2, "n_trades": 5}}
    record_report(ledger, run_id, H, "backtest", backtest_payload(metrics=bad))
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["min_trades"]["status"] == "fail"


def test_regime_gated_plan_passes_when_every_traded_regime_is_profitable(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    gated = {
        **EMA_PLAN,
        "features": {**EMA_PLAN["features"], "reg": {"regime_state": {"window_bars": 120}}},
        "long_entry": "crosses_above(ema_fast, ema_slow) and reg > 0",
    }
    gp = parse_plan(gated)
    gh = plan_hash(gp)
    run_id = start(ledger, tmp_path)
    regimes = {
        "basis": "t",
        "regimes": {
            "bull": {"active_bars": 9, "total_return": 0.3},
            "bear": {"active_bars": 0, "total_return": 0.0},
            "sideways": {"active_bars": 0, "total_return": 0.0},
        },
    }
    record_report(
        ledger, run_id, gh, "backtest", backtest_payload(plan=plan_to_dict(gp), regimes=regimes)
    )
    row = rows(evaluate_plan(ledger, run_id, gh, cfg))["min_profitable_regimes"]
    assert row["status"] == "pass" and "regime-gated" in row["note"]


# --------------------------------------------------------------------------- adversarial rows


def test_adversarial_rows_pass(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "adversarial", adversarial_payload())
    r = rows(evaluate_plan(ledger, run_id, H, cfg))
    assert r["cost_stress_factor"]["status"] == "pass"
    assert r["cost_stress_factor"]["value"] == 0.2
    assert r["require_gradual_delay_decay"]["status"] == "pass"


@pytest.mark.parametrize(
    "delays",
    [
        [{"delay": 1, "sharpe": 0.3}, {"delay": 3, "sharpe": 0.2}, {"delay": 5, "sharpe": 0.1}],
        [{"delay": 1, "sharpe": 2.0}, {"delay": 3, "sharpe": 1.9}, {"delay": 5, "sharpe": 1.9}],
    ],
    ids=["vanishes", "unchanged"],
)
def test_delay_row_fails_on_a_cliff_or_no_decay(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, delays: list[dict[str, float]]
) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "adversarial", adversarial_payload(delay={"rows": delays}))
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["require_gradual_delay_decay"]["status"] == (
        "fail"
    )


def test_cost_stress_fails_when_unprofitable_and_is_missing_when_not_run(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path, cost_stress_factor=4.0)
    record_report(ledger, run_id, H, "adversarial", adversarial_payload())
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["cost_stress_factor"]["status"] == "fail"
    run2 = start(ledger, tmp_path, cost_stress_factor=3.0)
    record_report(ledger, run2, H, "adversarial", adversarial_payload())
    row = rows(evaluate_plan(ledger, run2, H, cfg))["cost_stress_factor"]
    assert row["status"] == "missing" and "x3" in row["note"]


# --------------------------------------------------------------------------- wf / flags


def test_walk_forward_row(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "walk_forward", {"oos_metrics": {"sharpe": 1.2}, "folds": []})
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["wf_oos_sharpe_min"]["status"] == "pass"
    record_report(ledger, run_id, H, "walk_forward", {"oos_metrics": {"sharpe": 0.4}, "folds": []})
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["wf_oos_sharpe_min"]["status"] == "fail"
    record_report(ledger, run_id, H, "walk_forward", {"oos_metrics": {"sharpe": None}})
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["wf_oos_sharpe_min"]["status"] == "fail"


def test_holdout_and_crosscheck_flags(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "holdout", {"passed": False, "failed": ["net_sharpe"]})
    record_report(ledger, run_id, H, "crosscheck", {"matched": True})
    r = rows(evaluate_plan(ledger, run_id, H, cfg))
    assert r["require_holdout_pass"]["status"] == "fail"
    assert r["require_crosscheck_match"]["status"] == "pass"


def test_evidence_for_another_plan_is_ignored(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, "f" * 64, "walk_forward", {"oos_metrics": {"sharpe": 3.0}})
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["wf_oos_sharpe_min"]["status"] == "missing"


# --------------------------------------------------------------------------- DSR


def _three_trials(ledger: Ledger, run_id: str, cfg: LabConfig) -> tuple[int, list[np.ndarray]]:
    rng = np.random.default_rng(3)
    rets = [rng.normal(mu, 0.01, 600) for mu in (0.002, 0.0005, -0.0003)]
    base = trial(ledger, run_id, rets[0], cfg=cfg)
    for k, r in enumerate(rets[1:]):
        variant = plan_to_dict(with_params(PLAN, {"ema_fast.period": 30 + k}))
        trial(ledger, run_id, r, plan_dict=variant, source="optuna", cfg=cfg)
    return base, rets


def _trials(
    ledger: Ledger, run_id: str, cfg: LabConfig, n: int = 10
) -> tuple[int, list[np.ndarray]]:
    """The base plan plus ``n - 1`` re-tuned Optuna variants (``n`` commensurable plans, at
    least lab.toml ``min_variance_plans`` by default)."""
    rng = np.random.default_rng(3)
    mus = [0.002, *np.linspace(-0.0005, 0.001, n - 1)]
    rets = [rng.normal(mu, 0.01, 600) for mu in mus]
    base = trial(ledger, run_id, rets[0], cfg=cfg)
    for k, r in enumerate(rets[1:]):
        variant = plan_to_dict(with_params(PLAN, {"ema_fast.period": 30 + k}))
        trial(ledger, run_id, r, plan_dict=variant, source="optuna", cfg=cfg)
    return base, rets


def test_dsr_is_redeflated_with_the_runs_current_trial_count(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    base, rets = _trials(ledger, run_id, cfg)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    variance = float(np.var([sharpe_per_bar(r) for r in rets], ddof=1))
    expected = deflated_sharpe(rets[0], n_trials=10, sharpe_variance=variance).dsr
    rep = evaluate_plan(ledger, run_id, H, cfg)
    row = rows(rep)["dsr_min"]
    assert row["value"] == pytest.approx(expected, rel=1e-12)  # same computation, float noise
    assert "N = 10" in row["note"] and "report used N = 1" in row["note"]
    assert rep["ledger_n"] == 10
    assert row["status"] == ("pass" if expected >= 0.95 else "fail")


def test_dsr_is_missing_below_the_minimum_number_of_commensurable_plans(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """W1: a variance from a handful of Sharpes is noise; below lab.toml min_variance_plans the
    DSR row is missing, never a pass."""
    assert cfg.research.min_variance_plans == 10
    run_id = start(ledger, tmp_path)
    base, _ = _trials(ledger, run_id, cfg, n=9)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["dsr_min"]
    assert row["status"] == "missing" and row["value"] is None
    assert "only 9 commensurable plans" in row["note"] and "min_variance_plans = 10" in row["note"]
    # a tenth commensurable plan anywhere in research (here: another run) lifts the floor
    other = start(ledger, tmp_path)
    variant = plan_to_dict(with_params(PLAN, {"ema_fast.period": 45}))
    trial(ledger, other, np.random.default_rng(9).normal(0, 0.01, 600), plan_dict=variant, cfg=cfg)
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["dsr_min"]
    assert row["status"] in {"pass", "fail"} and row["value"] is not None


def test_dsr_with_one_plan_or_no_variance_is_missing(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    rng = np.random.default_rng(4)
    same = rng.normal(0.001, 0.01, 600)
    base = trial(ledger, run_id, same, cfg=cfg)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["dsr_min"]
    assert row["status"] == "missing" and "only 1 commensurable plans" in row["note"]
    # ten plans with one identical Sharpe: enough plans, but V[SR] = 0 is not estimable
    for k in range(9):
        other = plan_to_dict(with_params(PLAN, {"ema_fast.period": 33 + k}))
        trial(ledger, run_id, same, plan_dict=other, cfg=cfg)
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["dsr_min"]
    assert row["status"] == "missing" and "not estimable" in row["note"]


def test_dsr_missing_when_the_trial_belongs_to_another_run(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    other = start(ledger, tmp_path)
    base, _ = _three_trials(ledger, other, cfg)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["dsr_min"]["status"] == "missing"


# --------------------------------------------------------------------------- neighbourhood


def _neighbours(cfg: LabConfig) -> list[dict[str, float]]:
    from lab.plan import tunable_params
    from lab.runner import neighbourhood_overrides

    # the full ±20% grid (the criterion never accepts a capped grid)
    return neighbourhood_overrides(tunable_params(PLAN), cfg.stats.neighbourhood_steps, 1000)


@pytest.mark.parametrize("losing", [False, True])
def test_neighbourhood_row_from_ledger_trials(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, losing: bool
) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "backtest", backtest_payload())
    rng = np.random.default_rng(5)
    overrides = _neighbours(cfg)
    assert len(overrides) == 8  # 2 tunables x (0.8, 0.9, 1.1, 1.2)
    for k, ov in enumerate(overrides):
        ret = -0.05 if losing and k == 2 else 0.1
        trial(
            ledger,
            run_id,
            rng.normal(0, 0.01, 50),
            plan_dict=plan_to_dict(with_params(PLAN, ov)),
            source="neighbourhood",
            cfg=cfg,
            metrics={"total_return": ret},
        )
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["neighbourhood_pct"]
    assert row["status"] == ("fail" if losing else "pass")
    assert row["value"] == (0.875 if losing else 1.0)
    assert ("7 of 8" if losing else "8 of 8") in row["note"]


def test_neighbourhood_missing_without_trials(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "backtest", backtest_payload())
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["neighbourhood_pct"]["status"] == "missing"


# --------------------------------------------------------------------------- pre-holdout


def test_pre_holdout_needs_the_crosscheck_but_not_the_holdout(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """W4(b): the event-driven cross-check gates the holdout (only the holdout row itself is
    excluded from ``pre_holdout_passed``)."""
    assert PRE_HOLDOUT_EXCLUDED == ("require_holdout_pass",)
    lax = {**LAX, "neighbourhood_pct": 0.0, "dsr_min": 0.0}
    run_id = start(ledger, tmp_path, **lax)
    base, _ = _trials(ledger, run_id, cfg)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    record_report(ledger, run_id, H, "adversarial", adversarial_payload())
    record_report(ledger, run_id, H, "walk_forward", {"oos_metrics": {"sharpe": 1.0}})
    rep = evaluate_plan(ledger, run_id, H, cfg)
    assert rep["missing"] == ["pbo_max", "require_holdout_pass", "require_crosscheck_match"]
    _study_trials(ledger, run_id, cfg, "S1", cfg.research.min_pbo_trials, seed=9)
    record_report(ledger, run_id, H, "sweep", _sweep("S1", LH))
    rep = evaluate_plan(ledger, run_id, H, cfg)
    assert rep["missing"] == ["require_holdout_pass", "require_crosscheck_match"]
    assert rep["pre_holdout_passed"] is False  # no cross-check yet
    record_report(ledger, run_id, H, "crosscheck", {"matched": False, "available": False})
    rep = evaluate_plan(ledger, run_id, H, cfg)
    assert rep["pre_holdout_passed"] is False  # engine unavailable: missing, still refused
    assert "nautilus_trader" in rows(rep)["require_crosscheck_match"]["note"]
    record_report(ledger, run_id, H, "crosscheck", {"matched": True})
    rep = evaluate_plan(ledger, run_id, H, cfg)
    assert rep["pre_holdout_passed"] is True
    assert rep["passed"] is False
    assert rep["missing"] == ["require_holdout_pass"]
    record_report(ledger, run_id, H, "holdout", {"passed": True, "failed": []})
    assert evaluate_plan(ledger, run_id, H, cfg)["passed"] is True


# --------------------------------------------------------------------------- regime breakdown


def test_regime_breakdown_hand_checked() -> None:
    # Daily bars: flat at 100 for 100 bars, then +1% a day. Labels use close[t]/close[t-90]-1
    # with a strict 10% threshold: rows 0-89 undefined, 90-109 sideways (1.01**9 - 1 < 10%),
    # 110-199 bull. Bar t's return takes the label at t-1: 91 unlabelled, 20 sideways, 89 bull.
    n = 200
    idx = pd.date_range("2020-01-01", periods=n, freq="1D", tz="UTC", name="time")
    close = np.where(np.arange(n) < 100, 100.0, 100.0 * 1.01 ** (np.arange(n) - 100))
    candles = pd.DataFrame({"close": close}, index=idx)
    r = np.full(n, 0.001)
    out = regime_breakdown(
        candles, idx[0], idx[-1] + pd.Timedelta(days=1), r, "1d", window_days=90, threshold=0.10
    )
    assert out["unlabelled_bars"] == 91
    assert out["basis"].startswith("trailing 90-day simple return (90 bars): above +10% bull")
    reg = out["regimes"]
    assert (reg["sideways"]["bars"], reg["bull"]["bars"], reg["bear"]["bars"]) == (20, 89, 0)
    assert reg["bull"]["total_return"] == pytest.approx(1.001**89 - 1, rel=1e-12)
    assert reg["bear"]["total_return"] is None
    assert reg["sideways"]["active_bars"] == 20


def test_regime_breakdown_uses_history_before_the_window_for_warm_up() -> None:
    n = 200
    idx = pd.date_range("2020-01-01", periods=n, freq="1D", tz="UTC", name="time")
    close = np.where(np.arange(n) < 100, 100.0, 100.0 * 1.01 ** (np.arange(n) - 100))
    candles = pd.DataFrame({"close": close}, index=idx)
    end = idx[-1] + pd.Timedelta(days=1)
    out = regime_breakdown(
        candles, idx[150], end, np.zeros(50), "1d", window_days=90, threshold=0.1
    )
    assert out["unlabelled_bars"] == 0 and out["regimes"]["bull"]["bars"] == 50
    assert out["regimes"]["bull"]["active_bars"] == 0
    with pytest.raises(ValueError, match="returns has 49 values"):
        regime_breakdown(candles, idx[150], end, np.zeros(49), "1d", window_days=90, threshold=0.1)
    assert not math.isnan(out["regimes"]["bull"]["total_return"])


def test_regime_breakdown_follows_the_window_and_threshold_it_is_given() -> None:
    # Same series as the hand-checked test. window_days=20: labels defined from row 20; rows
    # 20-99 flat (0% <= 10%, sideways); 1.01**k / 1.01**(k-20) - 1 = 22% from row 120, so rows
    # 100-109 (k <= 9: 1.01**9 - 1 = 9.4%) are sideways and rows 110+ (k >= 10, 10.5%) bull.
    # Returns take the label at t-1: 21 unlabelled, 90 sideways, 89 bull. A 25% threshold over
    # 20 days is never crossed (max 1.01**20 - 1 = 22%), so every labelled bar is sideways.
    n = 200
    idx = pd.date_range("2020-01-01", periods=n, freq="1D", tz="UTC", name="time")
    close = np.where(np.arange(n) < 100, 100.0, 100.0 * 1.01 ** (np.arange(n) - 100))
    candles = pd.DataFrame({"close": close}, index=idx)
    end = idx[-1] + pd.Timedelta(days=1)
    r = np.full(n, 0.001)
    out = regime_breakdown(candles, idx[0], end, r, "1d", window_days=20, threshold=0.10)
    reg = out["regimes"]
    assert out["unlabelled_bars"] == 21
    assert (reg["sideways"]["bars"], reg["bull"]["bars"], reg["bear"]["bars"]) == (90, 89, 0)
    assert "trailing 20-day" in out["basis"]
    wide = regime_breakdown(candles, idx[0], end, r, "1d", window_days=20, threshold=0.25)
    assert wide["regimes"]["bull"]["bars"] == 0 and wide["regimes"]["sideways"]["bars"] == 179
    assert "+25% bull" in wide["basis"]


@pytest.mark.parametrize(
    ("window_days", "threshold", "match"),
    [(0, 0.1, "window_days"), (True, 0.1, "window_days"), (90, -0.1, "threshold")],
)
def test_regime_breakdown_rejects_bad_definition(
    window_days: Any, threshold: float, match: str
) -> None:
    idx = pd.date_range("2020-01-01", periods=5, freq="1D", tz="UTC", name="time")
    candles = pd.DataFrame({"close": np.full(5, 100.0)}, index=idx)
    with pytest.raises(ValueError, match=match):
        regime_breakdown(
            candles,
            idx[0],
            idx[-1] + pd.Timedelta(days=1),
            np.zeros(5),
            "1d",
            window_days=window_days,
            threshold=threshold,
        )


def test_frozen_criteria_carry_the_regime_definition(ledger: Ledger, tmp_path: Path) -> None:
    run_id = start(ledger, tmp_path, regime_window_days=30, regime_threshold=0.05)
    c = frozen_criteria(ledger, run_id)
    assert (c.regime_window_days, c.regime_threshold) == (30, 0.05)


def test_frozen_criteria_with_stale_keys_fail_closed(ledger: Ledger, tmp_path: Path) -> None:
    run_id = start(ledger, tmp_path)
    run = ledger.research_run(run_id)
    assert run is not None
    stale = {k: v for k, v in run["config"]["criteria"].items() if k != "regime_threshold"}

    class StaleLedger:  # a run frozen before the regime fields existed (rows are immutable)
        def research_run(self, rid: str) -> dict[str, Any]:
            return {**run, "config": {**run["config"], "criteria": stale}}

    with pytest.raises(ValueError, match=r"missing \['regime_threshold'\]"):
        frozen_criteria(StaleLedger(), run_id)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Phase 8 review fixes

LH = logic_hash(EMA_PLAN)


def test_dsr_n_counts_distinct_plans_across_every_research_run_and_interval(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """A new run, another interval, or the web UI must not reset the multiple-testing bar: N
    is global over EVERY run (research and not); V[SR] is the largest of this run's, every
    research run's commensurable variance (same interval, window, costs), and the
    cross-interval pool (each Sharpe annualised with 365-day bars, rescaled to 4h bars)."""
    earlier = start(ledger, tmp_path)
    rng = np.random.default_rng(8)
    hourly = rng.normal(0, 0.01, 600)
    trial(ledger, earlier, hourly, plan_dict={**EMA_PLAN, "interval": "1h"}, cfg=cfg)
    other_rets = rng.normal(0, 0.01, 600)
    trial(ledger, earlier, other_rets, plan_dict=OTHER_EMA, source="optuna", cfg=cfg)
    trial(
        ledger, earlier, rng.normal(0, 0.01, 600), plan_dict=OTHER_EMA, source="benchmark", cfg=cfg
    )
    # a non-research run (the web UI's Strategy Tester) counts toward N, not toward V
    trial(ledger, "ui-run", rng.normal(0.01, 0.01, 600), plan_dict=THIRD_EMA, cfg=cfg)
    run_id = start(ledger, tmp_path)
    base, rets = _trials(ledger, run_id, cfg)
    record_report(ledger, run_id, H, "backtest", backtest_payload(base))
    rep = evaluate_plan(ledger, run_id, H, cfg)
    row = rows(rep)["dsr_min"]
    srs = [sharpe_per_bar(r) for r in rets]
    run_v = float(np.var(srs, ddof=1))
    all_v = float(np.var([*srs, sharpe_per_bar(other_rets)], ddof=1))
    # 1h -> 4h per-bar scale: x sqrt(bars_per_year(1h) / bars_per_year(4h)) = x 2
    pooled = [*srs, sharpe_per_bar(other_rets), sharpe_per_bar(hourly) * 2.0]
    pooled_v = float(np.var(pooled, ddof=1))
    variance = max(run_v, all_v, pooled_v)
    expected = deflated_sharpe(rets[0], n_trials=13, sharpe_variance=variance).dsr
    assert rep["ledger_n"] == 13  # 10 here + 2 distinct in the earlier run + 1 in the UI run
    assert row["value"] == pytest.approx(expected, rel=1e-12)
    assert "N = 13 distinct plans tried in every run of the lab" in row["note"]
    assert "10 commensurable trial Sharpes in this run" in row["note"]
    assert "11 in every research run" in row["note"]
    assert "12 plans pooled across 2 interval(s)" in row["note"]


def test_a_winner_moved_to_another_interval_is_deflated_with_the_pooled_variance(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """W1 (probe_r3 a): a search on 4h, then only the winner re-submitted on 1h in a fresh run.
    The 1h run has one commensurable plan; the pooled cross-interval variance (4h Sharpes
    rescaled to 1h bars, x 1/2) is what deflates it."""
    search = start(ledger, tmp_path)
    rng = np.random.default_rng(21)
    four = [rng.normal(mu, 0.01, 600) for mu in np.linspace(-0.002, 0.004, 12)]
    for k, r in enumerate(four):
        variant = plan_to_dict(with_params(PLAN, {"ema_fast.period": 10 + k}))
        trial(ledger, search, r, plan_dict=variant, source="optuna", cfg=cfg)
    fresh = start(ledger, tmp_path)
    hourly_plan = {**EMA_PLAN, "interval": "1h"}
    winner = rng.normal(0.001, 0.01, 2400)
    base = trial(ledger, fresh, winner, plan_dict=hourly_plan, cfg=cfg)
    h1 = plan_hash(parse_plan(hourly_plan))
    record_report(ledger, fresh, h1, "backtest", backtest_payload(base))
    row = rows(evaluate_plan(ledger, fresh, h1, cfg))["dsr_min"]
    pooled = [sharpe_per_bar(r) * 0.5 for r in four] + [sharpe_per_bar(winner)]
    expected = deflated_sharpe(winner, n_trials=13, sharpe_variance=np.var(pooled, ddof=1)).dsr
    assert row["value"] == pytest.approx(expected, rel=1e-12)
    assert "1 commensurable trial Sharpes in this run" in row["note"]
    assert "13 plans pooled across 2 interval(s)" in row["note"]


OTHER_EMA = {
    **EMA_PLAN,
    "features": {"ema_fast": {"ema": {"period": 12}}, "ema_slow": {"ema": {"period": 40}}},
}
THIRD_EMA = {
    **EMA_PLAN,
    "features": {"ema_fast": {"ema": {"period": 8}}, "ema_slow": {"ema": {"period": 30}}},
}


def test_walk_forward_row_takes_the_minimum_over_every_result_of_the_logic(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)  # wf_oos_sharpe_min 1.0
    variant = plan_hash(parse_plan(OTHER_EMA))  # same logic, re-tuned periods
    other_logic = {**EMA_PLAN, "long_exit": "ema_fast < ema_slow"}
    record_report(
        ledger, run_id, H, "walk_forward", {"oos_metrics": {"sharpe": 1.5}, "logic_hash": LH}
    )
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["wf_oos_sharpe_min"]["status"] == "pass"
    record_report(
        ledger, run_id, variant, "walk_forward", {"oos_metrics": {"sharpe": 0.6}, "logic_hash": LH}
    )
    record_report(
        ledger,
        run_id,
        plan_hash(parse_plan(other_logic)),
        "walk_forward",
        {"oos_metrics": {"sharpe": -3.0}, "logic_hash": logic_hash(other_logic)},
    )
    # a later, better result of the same logic does not replace the worse one
    record_report(
        ledger, run_id, H, "walk_forward", {"oos_metrics": {"sharpe": 2.5}, "logic_hash": LH}
    )
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["wf_oos_sharpe_min"]
    assert row["status"] == "fail" and row["value"] == 0.6
    assert "minimum" in row["note"] and "over 3 walk_forward result(s)" in row["note"]


def _study_trials(
    ledger: Ledger, run_id: str, cfg: LabConfig, study: str, n: int, seed: int
) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n):
        r = rng.normal(0.0, 0.01, 640)
        variant = plan_to_dict(with_params(PLAN, {"ema_fast.period": 21 + k + seed}))
        params = json.dumps({"ema_fast.period": 21 + k + seed, "_study": {"study": study}})
        trial(ledger, run_id, r, plan_dict=variant, source="optuna", cfg=cfg, params_json=params)
        out.append(r)
    return out


def _sweep(study: str, lh: str) -> dict[str, Any]:
    return {"study": study, "window": WINDOW, "logic_hash": lh, "space": {}}


def test_pbo_row_is_the_max_of_the_neighbourhood_and_the_sweep_trials(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)  # pbo_max 0.2
    record_report(ledger, run_id, H, "backtest", backtest_payload())  # neighbourhood PBO 0.1
    # the neighbourhood basis alone is not enough: no sweep, no PBO
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["pbo_max"]["status"] == "missing"
    cols = _study_trials(ledger, run_id, cfg, "S1", 16, seed=1)
    _study_trials(ledger, run_id, cfg, "S-other", 16, seed=50)  # another logic's study
    record_report(ledger, run_id, H, "sweep", _sweep("S1", LH))
    other = {**EMA_PLAN, "long_exit": "ema_fast < ema_slow"}
    record_report(ledger, run_id, "e" * 64, "sweep", _sweep("S-other", logic_hash(other)))
    expected = pbo_cscv(np.column_stack(cols), n_splits=cfg.stats.pbo_splits).pbo
    assert expected > 0.1  # pure-noise trials: the sweep basis is the binding one
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["pbo_max"]
    assert row["value"] == pytest.approx(max(0.1, expected))
    assert row["status"] == ("pass" if max(0.1, expected) <= 0.2 else "fail")
    assert "neighbourhood CSCV" in row["note"] and "sweep CSCV" in row["note"]
    assert "over 16 design-window Optuna trials of 1 study" in row["note"]


def test_pbo_row_uses_the_sweep_basis_alone_and_is_missing_without_either(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["pbo_max"]
    assert row["status"] == "missing" and "run_sweep first" in row["note"]
    record_report(ledger, run_id, H, "sweep", _sweep("S1", LH))
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["pbo_max"]
    assert row["status"] == "missing" and "sweep too small for PBO" in row["note"]
    _study_trials(ledger, run_id, cfg, "S1", 1, seed=2)  # one trial: CSCV needs >= 2
    assert rows(evaluate_plan(ledger, run_id, H, cfg))["pbo_max"]["status"] == "missing"
    cols = _study_trials(ledger, run_id, cfg, "S1", 15, seed=3)
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["pbo_max"]
    assert row["status"] in {"pass", "fail"} and "sweep CSCV" in row["note"]
    assert "neighbourhood basis not computable" in row["note"]
    assert len(cols) == 15 and "over 16 design-window Optuna trials" in row["note"]


def test_a_sweep_below_the_trial_floor_gives_no_pbo(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """W2 (probe item 25): a 2-trial sweep with one dominated column has PBO 0.0; below lab.toml
    min_pbo_trials the sweep basis is "sweep too small for PBO", never a pass."""
    assert cfg.research.min_pbo_trials == 16
    run_id = start(ledger, tmp_path, pbo_max=1.0)
    record_report(ledger, run_id, H, "backtest", backtest_payload())  # neighbourhood PBO 0.1
    rng = np.random.default_rng(4)
    good = rng.normal(0.002, 0.01, 640)
    for k, r in enumerate([good, good - 0.004]):  # the second column is dominated everywhere
        variant = plan_to_dict(with_params(PLAN, {"ema_fast.period": 60 + k}))
        params = json.dumps({"ema_fast.period": 60 + k, "_study": {"study": "TINY"}})
        trial(ledger, run_id, r, plan_dict=variant, source="optuna", cfg=cfg, params_json=params)
    assert pbo_cscv(np.column_stack([good, good - 0.004]), n_splits=16).pbo == 0.0
    record_report(ledger, run_id, H, "sweep", _sweep("TINY", LH))
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["pbo_max"]
    assert row["status"] == "missing" and row["value"] is None
    assert "sweep too small for PBO" in row["note"] and "min_pbo_trials = 16" in row["note"]


def test_neighbourhood_row_is_missing_when_fewer_neighbours_than_expected(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "backtest", backtest_payload())
    rng = np.random.default_rng(6)
    for ov in _neighbours(cfg)[:3]:  # 3 of the 8 expected, all profitable
        trial(
            ledger,
            run_id,
            rng.normal(0, 0.01, 50),
            plan_dict=plan_to_dict(with_params(PLAN, ov)),
            source="neighbourhood",
            cfg=cfg,
            metrics={"total_return": 0.1},
        )
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["neighbourhood_pct"]
    assert row["status"] == "missing" and "only 3 of 8 expected" in row["note"]


def test_neighbourhood_row_ignores_trials_run_with_other_costs(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "backtest", backtest_payload())
    rng = np.random.default_rng(7)
    cheap = json.dumps({"fee_rate": 0.0, "slippage_rate": 0.0})
    overrides = _neighbours(cfg)
    for k, ov in enumerate(overrides):
        trial(
            ledger,
            run_id,
            rng.normal(0, 0.01, 50),
            plan_dict=plan_to_dict(with_params(PLAN, ov)),
            source="neighbourhood",
            cfg=cfg,
            metrics={"total_return": 0.1},
            costs_json=cheap if k == 0 else None,
        )
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["neighbourhood_pct"]
    assert row["status"] == "missing" and "only 7 of 8" in row["note"]
    assert "other costs" in row["note"]


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        (
            {"matched": False, "available": False, "reason": "nautilus_trader is not installed"},
            "missing",
        ),
        ({"matched": False, "available": True, "reason": None, "matched_trades": 3}, "fail"),
        ({"matched": True, "available": True, "reason": None, "matched_trades": 9}, "pass"),
    ],
)
def test_crosscheck_row_from_crosscheck_evidence(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, payload: dict[str, Any], status: str
) -> None:
    run_id = start(ledger, tmp_path)
    record_report(ledger, run_id, H, "crosscheck", {**payload, "logic_hash": LH})
    row = rows(evaluate_plan(ledger, run_id, H, cfg))["require_crosscheck_match"]
    assert row["status"] == status
    if status == "missing":
        assert "unavailable: nautilus_trader is not installed" in row["note"]


def test_evaluate_plan_refuses_a_run_whose_lab_toml_changed(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    from dataclasses import replace

    run_id = start(ledger, tmp_path)
    evaluate_plan(ledger, run_id, H, cfg)  # same values: fine
    moved = replace(
        cfg, splits=replace(cfg.splits, holdout_start=pd.Timestamp("2019-11-01", tz="UTC"))
    )
    with pytest.raises(ValueError, match=r"lab\.toml changed since this run started.*splits"):
        evaluate_plan(ledger, run_id, H, moved)


def test_evaluate_plan_refuses_a_run_started_without_a_lab_fingerprint(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    path = write_criteria(tmp_path / "bare.toml")
    run_id = start_research_run(ledger, load_research_config(path), label="bare")
    with pytest.raises(ValueError, match=r"no lab\.toml fingerprint"):
        evaluate_plan(ledger, run_id, H, cfg)
