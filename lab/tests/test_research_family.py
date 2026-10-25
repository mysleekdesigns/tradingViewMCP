"""Phase 8 round-3 fixes: research-wide DSR variance (K1), behaviour families for the holdout
(K2), the walk-forward lineage across families and runs (K3), Phase 7 API writes into research
runs (W-a), and neighbourhood coverage (W-c). Offline: hand-built ledger trials with known
answers, plus real backtests on seeded synthetic candles through the in-process MCP server."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lab.api import create_app
from lab.config import DEFAULT_CONFIG_PATH, LabConfig, load_config
from lab.ledger import HoldoutAlreadyCheckedError, Ledger, costs_to_json
from lab.plan import parse_plan, plan_hash, plan_to_dict, tunable_params, with_params
from lab.research.contract import load_research_config
from lab.research.criteria import evaluate_plan
from lab.research.evidence import record_report
from lab.research.family import (
    behaviour_family,
    family_holdout_conflict,
    tried_ranges,
    wf_lineage_problems,
)
from lab.research.holdout import HoldoutRefusedError, run_holdout_check
from lab.research.logic import logic_hash
from lab.research.runs import start_research_run
from lab.runner import neighbourhood_overrides
from lab.stats import deflated_sharpe, sharpe_per_bar
from lab.types import TrialRecord
from tests.test_mcp_server import (
    EMA_PLAN,
    LAX,
    RSI_PLAN,
    TREND_PLAN,
    Lab,
    call,
    counts,
    error_text,
    loader_for,
    ok,
    small_cfg,
    trend_candles,
    write_criteria,
)
from tests.test_research_criteria import (
    W_END,
    W_START,
    WINDOW,
    backtest_payload,
    rows,
    trial,
)
from tests.test_research_holdout import DOWN, UP, crosscheck_payload, seed_pre_holdout

H = plan_hash(parse_plan(EMA_PLAN))


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def start(ledger: Ledger, tmp_path: Path, **criteria: object) -> str:
    path = write_criteria(tmp_path / f"c{len(ledger.research_runs())}.toml", **criteria)
    return start_research_run(
        ledger, load_research_config(path), label="t", cfg=small_cfg(tmp_path)
    )


def ema(fast: int, slow: int = 50, **over: Any) -> dict[str, Any]:
    return {
        **EMA_PLAN,
        "features": {"ema_fast": {"ema": {"period": fast}}, "ema_slow": {"ema": {"period": slow}}},
        **over,
    }


# No-op variants of TREND_PLAN: they trade exactly like it but have other logic hashes.
NOOP_EXIT = {**TREND_PLAN, "long_exit": "ema_fast < ema_slow or ema_fast < 0"}
ALWAYS_TRUE_ENTRY = {
    **TREND_PLAN,
    "features": {**TREND_PLAN["features"], "junk": {"rsi": {"period": 14}}},
    "long_entry": "ema_fast > ema_slow and junk > -1",
}
LOOSE_STOP = {**TREND_PLAN, "risk": {"position_size_pct": 100, "stop_loss_pct": 50}}
OTHER_INTERVAL = {**TREND_PLAN, "interval": "1h"}


# =========================================================================== K1: V[SR]


def test_research_wide_variance_spans_every_research_run_but_only_commensurable_rows(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    a, b = start(ledger, tmp_path), start(ledger, tmp_path)
    rng = np.random.default_rng(1)
    ra, rb, rc = (rng.normal(mu, 0.01, 300) for mu in (0.002, -0.001, 0.0005))
    trial(ledger, a, ra, plan_dict=ema(10), source="optuna", cfg=cfg)
    trial(ledger, b, rb, plan_dict=ema(11), source="optuna", cfg=cfg)
    trial(ledger, b, rc, plan_dict=ema(12), cfg=cfg)
    # not commensurable / not research: other costs, a benchmark row, a non-research run
    trial(ledger, b, rng.normal(0.01, 0.01, 300), plan_dict=ema(13), cfg=cfg, costs_json="{}")
    trial(ledger, b, rng.normal(0.01, 0.01, 300), plan_dict=ema(14), source="benchmark", cfg=cfg)
    trial(ledger, "ui", rng.normal(0.01, 0.01, 300), plan_dict=ema(15), cfg=cfg)
    scope = {
        "interval": "4h",
        "window_start": W_START,
        "window_end": W_END,
        "costs_json": costs_to_json(cfg.costs),
    }
    latest = ledger.latest_sharpes_per_plan_research(**scope)
    assert len(latest) == 3
    want = float(np.var([sharpe_per_bar(r) for r in (ra, rb, rc)], ddof=1))
    assert ledger.selection_sharpe_variance_research(**scope) == pytest.approx(want, rel=1e-12)
    # Phase 7's per-run variance is unchanged: only run b's two plans
    want_b = float(np.var([sharpe_per_bar(r) for r in (rb, rc)], ddof=1))
    assert ledger.selection_sharpe_variance(b, **scope) == pytest.approx(want_b, rel=1e-12)


def test_research_wide_variance_never_reads_holdout_or_walk_forward_test_rows(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run = start(ledger, tmp_path)
    rng = np.random.default_rng(2)
    for kind in ("holdout", "walk_forward_test"):
        for k in range(2):
            ret = rng.normal(0.003 * k, 0.01, 50)
            p = parse_plan(ema(30 + k))
            ledger.record(
                TrialRecord(
                    run_id=run,
                    source="manual",
                    status="ok",
                    plan_hash=plan_hash(p) + kind,
                    plan_json=json.dumps(plan_to_dict(p)),
                    params_json="{}",
                    symbol="BTCUSDT",
                    interval="4h",
                    window_kind=kind,  # type: ignore[arg-type]
                    window_start=W_START,
                    window_end=W_END,
                    metrics_json="{}",
                    sharpe_per_bar=sharpe_per_bar(ret),
                    n_trades=1,
                    costs_json=costs_to_json(cfg.costs),
                    returns=ret,
                )
            )
    assert ledger.latest_sharpes_per_plan_research() == {}


def test_a_new_run_holding_only_the_winner_is_deflated_with_the_searchs_variance(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """probe_v scenario: run A sweeps 60 trials; run B submits only the winner (plus a few
    near-identical neighbours). B's DSR uses the larger, research-wide V[SR]."""
    a = start(ledger, tmp_path)
    rng = np.random.default_rng(42)
    sweep = [rng.normal(mu, 0.01, 800) for mu in np.linspace(-0.001, 0.0012, 60)]
    winner = rng.normal(0.0016, 0.01, 800)
    for k, r in enumerate(sweep):
        trial(ledger, a, r, plan_dict=ema(5 + k), source="optuna", cfg=cfg)
    trial(ledger, a, winner, plan_dict=ema(70), source="optuna", cfg=cfg)
    b = start(ledger, tmp_path)
    base = trial(ledger, b, winner, plan_dict=ema(70), cfg=cfg)
    neighbours = [winner + rng.normal(0.0, 0.0005, 800) for _ in range(4)]
    for k, r in enumerate(neighbours):
        trial(ledger, b, r, plan_dict=ema(70, 51 + k), source="neighbourhood", cfg=cfg)
    h = plan_hash(parse_plan(ema(70)))
    record_report(ledger, b, h, "backtest", backtest_payload(base, plan=ema(70), plan_hash=h))

    scope = {
        "interval": "4h",
        "window_start": W_START,
        "window_end": W_END,
        "costs_json": costs_to_json(cfg.costs),
    }
    run_v = ledger.selection_sharpe_variance(b, **scope)
    all_v = ledger.selection_sharpe_variance_research(**scope)
    assert all_v > 10 * run_v  # the fresh run's own variance is tiny
    n = ledger.count_distinct_plans_research()
    assert n == 61 + 4  # the winner's plan hash is shared between the runs
    row = rows(evaluate_plan(ledger, b, h, cfg))["dsr_min"]
    expected = deflated_sharpe(winner, n_trials=n, sharpe_variance=all_v).dsr
    run_local = deflated_sharpe(winner, n_trials=n, sharpe_variance=run_v).dsr
    assert row["value"] == pytest.approx(expected, rel=1e-12)
    assert row["value"] < run_local  # the old run-scoped V would have been more lenient
    assert "5 commensurable trial Sharpes in this run" in row["note"]
    assert "65 in every research run" in row["note"]


# =========================================================================== K2: families


def _base(ledger: Ledger, run: str, plan: dict[str, Any], r: np.ndarray, cfg: LabConfig) -> int:
    return trial(ledger, run, r, plan_dict=plan, cfg=cfg)


def test_family_is_own_logic_plus_correlated_design_window_returns(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    a, b = start(ledger, tmp_path), start(ledger, tmp_path)
    rng = np.random.default_rng(3)
    r = rng.normal(0.001, 0.01, 500)
    _base(ledger, a, TREND_PLAN, r, cfg)
    _base(ledger, b, NOOP_EXIT, r.copy(), cfg)  # identical behaviour, other run: corr 1
    _base(ledger, b, LOOSE_STOP, r + rng.normal(0, 0.003, 500), cfg)  # corr ~0.96
    _base(ledger, b, RSI_PLAN, rng.normal(0.001, 0.01, 500), cfg)  # independent: corr ~0
    flat = {**TREND_PLAN, "long_exit": "ema_fast < ema_slow or close < 0"}
    _base(ledger, b, flat, np.zeros(500), cfg)  # zero variance correlates with nothing
    # a correlated trial on ANOTHER window does not count
    p = parse_plan(ALWAYS_TRUE_ENTRY)
    ledger.record(
        TrialRecord(
            run_id=b,
            source="api",
            status="ok",
            plan_hash=plan_hash(p),
            plan_json=json.dumps(plan_to_dict(p)),
            params_json="{}",
            symbol="BTCUSDT",
            interval="4h",
            window_kind="design",
            window_start=W_START,
            window_end=W_END + pd.Timedelta(hours=4),
            metrics_json="{}",
            sharpe_per_bar=sharpe_per_bar(r),
            n_trades=1,
            costs_json=costs_to_json(cfg.costs),
            returns=r,
        )
    )
    h = plan_hash(parse_plan(TREND_PLAN))
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=h, cfg=cfg, plan=TREND_PLAN)
    assert fam.logic_hashes == {
        logic_hash(TREND_PLAN),
        logic_hash(NOOP_EXIT),
        logic_hash(LOOSE_STOP),
    }
    assert fam.correlated[logic_hash(NOOP_EXIT)] == pytest.approx(1.0)
    assert 0.9 <= fam.correlated[logic_hash(LOOSE_STOP)] < 1.0
    assert logic_hash(RSI_PLAN) not in fam.logic_hashes
    assert logic_hash(flat) not in fam.logic_hashes
    assert logic_hash(ALWAYS_TRUE_ENTRY) not in fam.logic_hashes
    assert plan_hash(parse_plan(NOOP_EXIT)) in fam.plan_hashes
    assert plan_hash(parse_plan(RSI_PLAN)) not in fam.plan_hashes
    # the threshold comes from lab.toml [research]: at 0.99 the noisy copy drops out
    strict = replace(cfg, research=replace(cfg.research, family_return_corr=0.99))
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=h, cfg=strict, plan=TREND_PLAN)
    assert logic_hash(LOOSE_STOP) not in fam.logic_hashes
    assert logic_hash(NOOP_EXIT) in fam.logic_hashes


def test_family_without_a_base_trial_is_the_plans_own_logic(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run = start(ledger, tmp_path)
    _base(ledger, run, NOOP_EXIT, np.random.default_rng(4).normal(0, 0.01, 300), cfg)
    h = plan_hash(parse_plan(TREND_PLAN))
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=h, cfg=cfg, plan=TREND_PLAN)
    assert fam.base_trial_id is None and fam.logic_hashes == {logic_hash(TREND_PLAN)}
    assert "no base design-window trial" in fam.note()


def test_the_lab_toml_family_threshold_is_loaded_and_validated(tmp_path: Path) -> None:
    raw = DEFAULT_CONFIG_PATH.read_text()
    assert tomllib.loads(raw)["research"]["family_return_corr"] == 0.8
    assert load_config().research.family_return_corr == 0.8
    for bad in ("0.0", "1.5", '"high"'):
        path = tmp_path / "lab.toml"
        path.write_text(raw.replace("family_return_corr = 0.8", f"family_return_corr = {bad}"))
        with pytest.raises(ValueError, match=r"research\.family_return_corr"):
            load_config(path)


@pytest.mark.parametrize(
    "variant",
    [NOOP_EXIT, ALWAYS_TRUE_ENTRY, LOOSE_STOP],
    ids=["no-op-exit", "always-true-entry", "stop-loss-50"],
)
def test_a_behaviourally_identical_variant_is_refused_after_the_original_failed(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, variant: dict[str, Any]
) -> None:
    first = start(ledger, tmp_path, **LAX)
    seed_pre_holdout(ledger, first, cfg)
    out = run_holdout_check(
        TREND_PLAN, run_id=first, cfg=cfg, ledger=ledger, loader=loader_for(DOWN)
    )
    assert out["passed"] is False
    second = start(ledger, tmp_path, **{**LAX, "max_holdout_checks": 3})
    assert logic_hash(variant) != logic_hash(TREND_PLAN)
    # the variant's base trial trades exactly like the original's (same design-window returns)
    original = ledger.latest_research_base_trial(plan_hash(parse_plan(TREND_PLAN)))
    assert original is not None and original.returns is not None
    _base(ledger, second, variant, original.returns.copy(), cfg)
    calls: list[pd.Timestamp] = []
    with pytest.raises(HoldoutRefusedError, match="same behaviour family"):
        run_holdout_check(
            variant, run_id=second, cfg=cfg, ledger=ledger, loader=loader_for(UP, calls)
        )
    assert ledger.holdout_checks_used(second) == 0 and calls == []
    rep = evaluate_plan(ledger, second, plan_hash(parse_plan(variant)), cfg)
    assert rows(rep)["require_holdout_pass"]["status"] == "fail"


def test_the_same_rule_on_another_interval_is_refused_without_any_trial(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    first = start(ledger, tmp_path, **LAX)
    seed_pre_holdout(ledger, first, cfg)
    run_holdout_check(TREND_PLAN, run_id=first, cfg=cfg, ledger=ledger, loader=loader_for(DOWN))
    second = start(ledger, tmp_path, **LAX)
    assert logic_hash(OTHER_INTERVAL) == logic_hash(TREND_PLAN)
    with pytest.raises(HoldoutRefusedError, match="already holdout-checked"):
        run_holdout_check(
            OTHER_INTERVAL, run_id=second, cfg=cfg, ledger=ledger, loader=loader_for(UP)
        )
    assert ledger.holdout_checks_used(second) == 0


def test_a_genuinely_different_strategy_is_not_refused_as_family(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    first = start(ledger, tmp_path, **{**LAX, "max_holdout_checks": 3})
    seed_pre_holdout(ledger, first, cfg)
    run_holdout_check(TREND_PLAN, run_id=first, cfg=cfg, ledger=ledger, loader=loader_for(DOWN))
    _base(ledger, first, RSI_PLAN, np.random.default_rng(5).normal(0.001, 0.01, 600), cfg)
    h = plan_hash(parse_plan(RSI_PLAN))
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=h, cfg=cfg, plan=RSI_PLAN)
    assert family_holdout_conflict(ledger, fam) is None
    # its own guards still apply (no evidence yet), but not the family refusal
    with pytest.raises(HoldoutRefusedError, match="pre-holdout") as exc:
        run_holdout_check(RSI_PLAN, run_id=first, cfg=cfg, ledger=ledger, loader=loader_for(UP))
    assert "behaviour family" not in str(exc.value)


def test_a_check_stored_under_an_older_logic_version_is_still_found(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """Holdout checks recorded before the interval left the logic hash (v1) are matched by
    recomputing the logic from the checked plan's holdout trial."""
    run = start(ledger, tmp_path)
    p = parse_plan(TREND_PLAN)
    ledger.record_holdout_check(run, plan_hash(p), budget=3, logic_hash="v1-" + "0" * 61)
    ledger.record(
        TrialRecord(
            run_id=run,
            source="manual",
            status="error",
            plan_hash=plan_hash(p),
            plan_json=json.dumps(plan_to_dict(p)),
            params_json="{}",
            symbol="BTCUSDT",
            interval="4h",
            window_kind="holdout",
            window_start=pd.Timestamp("2019-10-01", tz="UTC"),
            window_end=pd.Timestamp("2020-01-01", tz="UTC"),
            metrics_json="{}",
            sharpe_per_bar=None,
            n_trades=0,
            error="x",
        )
    )
    other = plan_hash(parse_plan(OTHER_INTERVAL))
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=other, cfg=cfg, plan=OTHER_INTERVAL)
    assert family_holdout_conflict(ledger, fam) is not None


def test_the_ledger_refuses_a_family_member_atomically(ledger: Ledger, tmp_path: Path) -> None:
    run = start(ledger, tmp_path)
    ledger.record_holdout_check(run, "p1", budget=5, logic_hash="L1")
    with pytest.raises(HoldoutAlreadyCheckedError, match="same behaviour family"):
        ledger.record_holdout_check(
            run, "p2", budget=5, logic_hash="L2", family_logic_hashes={"L2", "L1"}
        )
    with pytest.raises(HoldoutAlreadyCheckedError, match="same behaviour family"):
        ledger.record_holdout_check(run, "p3", budget=5, logic_hash="L3", family_plan_hashes={"p1"})
    assert ledger.holdout_checks_used(run) == 1
    assert ledger.record_holdout_check(run, "p4", budget=5, logic_hash="L4") == 2


def test_a_plan_whose_crosscheck_failed_never_spends_a_holdout_check(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run = start(ledger, tmp_path, **LAX)
    h = seed_pre_holdout(ledger, run, cfg)
    failed = {"matched": False, "available": True, "reason": None, "logic_hash": "x"}
    record_report(ledger, run, h, "crosscheck", failed)
    calls: list[pd.Timestamp] = []
    with pytest.raises(HoldoutRefusedError, match="failed the event-driven cross-check"):
        run_holdout_check(
            TREND_PLAN, run_id=run, cfg=cfg, ledger=ledger, loader=loader_for(UP, calls)
        )
    assert ledger.holdout_checks_used(run) == 0 and calls == []


def test_an_unavailable_crosscheck_keeps_the_holdout_refused(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """W4(b): the cross-check gates the holdout. With the engine unavailable the row is
    missing, and the refusal says the holdout stays refused until it is installed."""
    run = start(ledger, tmp_path, **LAX)
    h = seed_pre_holdout(ledger, run, cfg, crosscheck=False)
    record_report(
        ledger,
        run,
        h,
        "crosscheck",
        crosscheck_payload(matched=False, available=False),
    )
    calls: list[pd.Timestamp] = []
    with pytest.raises(HoldoutRefusedError) as info:
        run_holdout_check(
            TREND_PLAN, run_id=run, cfg=cfg, ledger=ledger, loader=loader_for(UP, calls)
        )
    msg = str(info.value)
    assert "require_crosscheck_match (missing)" in msg
    assert "stays refused until the engine (nautilus_trader) is installed" in msg
    assert ledger.holdout_checks_used(run) == 0 and calls == []
    report = evaluate_plan(ledger, run, h, cfg)
    assert report["pre_holdout_passed"] is False
    assert rows(report)["require_crosscheck_match"]["status"] == "missing"


def test_a_holdout_check_needs_a_crosscheck_first(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run = start(ledger, tmp_path, **LAX)
    seed_pre_holdout(ledger, run, cfg, crosscheck=False)
    with pytest.raises(HoldoutRefusedError, match="needs a matched event-driven cross-check"):
        run_holdout_check(TREND_PLAN, run_id=run, cfg=cfg, ledger=ledger, loader=loader_for(UP))
    assert ledger.holdout_checks_used(run) == 0


def test_real_no_op_variants_are_refused_through_the_server(tmp_path: Path) -> None:
    """End to end on real backtests: the original fails its holdout; a no-op exit clause, an
    always-true entry clause, a 50% stop, and the same rule on another interval are all
    refused (in a new run), spending nothing. RSI reversion is not refused as family."""
    lab = Lab(tmp_path, candles=trend_candles(holdout_drift=-0.003), criteria=LAX)
    first = lab.start()
    ok(lab.server, "run_backtest", {"run_id": first, "plan": TREND_PLAN})
    ok(lab.server, "run_adversarial", {"run_id": first, "plan": TREND_PLAN})
    space = {"ema_fast.period": {"low": 15, "high": 25}, "ema_slow.period": {"low": 40, "high": 60}}
    ok(
        lab.server,
        "run_sweep",
        {"run_id": first, "plan": TREND_PLAN, "space": space, "n_trials": 16},
    )
    ok(
        lab.server,
        "walk_forward",
        {"run_id": first, "plan": TREND_PLAN, "space": space, "n_trials_per_fold": 2},
    )
    cross = ok(lab.server, "run_crosscheck", {"run_id": first, "plan": TREND_PLAN})
    assert cross["available"] is True and cross["matched"] is True
    out = ok(lab.server, "holdout_check", {"run_id": first, "plan": TREND_PLAN})
    assert out["passed"] is False
    second = lab.start("second")
    for variant in (NOOP_EXIT, ALWAYS_TRUE_ENTRY, LOOSE_STOP):
        ok(lab.server, "run_backtest", {"run_id": second, "plan": variant})
        msg = error_text(lab.server, "holdout_check", {"run_id": second, "plan": variant})
        assert "same behaviour family" in msg, variant
    msg = error_text(lab.server, "holdout_check", {"run_id": second, "plan": OTHER_INTERVAL})
    assert "already holdout-checked" in msg
    ok(lab.server, "run_backtest", {"run_id": second, "plan": RSI_PLAN})
    msg = error_text(lab.server, "holdout_check", {"run_id": second, "plan": RSI_PLAN})
    assert "pre-holdout" in msg and "behaviour family" not in msg
    assert counts(lab, second)[2] == 0


# =========================================================================== K3: WF lineage


def _wf(lab: Lab, run_id: str, plan: dict[str, Any], space: dict[str, Any]) -> Any:
    return call(
        lab.server,
        "walk_forward",
        {"run_id": run_id, "plan": plan, "space": space, "n_trials_per_fold": 1},
    )


SLOW = {"ema_slow.period": {"low": 40, "high": 60}}


def test_a_narrow_walk_forward_around_hand_picked_backtests_is_refused(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    for fast in (12, 28, 20):  # hand hill-climbing with run_backtest, no sweep
        ok(lab.server, "run_backtest", {"run_id": run_id, "plan": ema(fast)})
    before = counts(lab, run_id)
    narrow = _wf(lab, run_id, ema(20), {"ema_fast.period": {"low": 16, "high": 24}, **SLOW})
    # tried: 12, 20, 28 and their ±10% neighbours (11, 13, 18, 22, 25, 31)
    assert narrow.is_error
    assert "ema_fast.period [16, 24] does not cover [11, 31]" in narrow.content[0].text
    assert counts(lab, run_id) == before
    honest = {"ema_fast.period": {"low": 10, "high": 32}, **SLOW}
    assert not _wf(lab, run_id, ema(20), honest).is_error


def test_a_sweep_in_another_run_binds_a_walk_forward_in_a_new_run(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    a = lab.start()
    ok(lab.server, "run_backtest", {"run_id": a, "plan": EMA_PLAN})
    wide = {"ema_fast.period": {"low": 5, "high": 60}, "ema_slow.period": {"low": 40, "high": 60}}
    ok(lab.server, "run_sweep", {"run_id": a, "plan": EMA_PLAN, "space": wide, "n_trials": 3})
    b = lab.start("fresh")
    ok(lab.server, "run_backtest", {"run_id": b, "plan": EMA_PLAN})
    narrow = _wf(lab, b, EMA_PLAN, {"ema_fast.period": {"low": 16, "high": 24}, **SLOW})
    assert narrow.is_error and "does not cover [5, 60]" in narrow.content[0].text
    assert not _wf(lab, b, EMA_PLAN, wide).is_error


def test_a_sweep_on_a_no_op_variant_binds_the_original(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    a = lab.start()
    variant = {**EMA_PLAN, "long_exit": "crosses_below(ema_fast, ema_slow) or ema_fast < 0"}
    assert logic_hash(variant) != logic_hash(EMA_PLAN)
    # submitted at deliberately different parameters, then swept: its design-window sweep
    # trials still correlate with the original, so the sweep is the original's history
    ok(lab.server, "run_backtest", {"run_id": a, "plan": ema(8, 120, **variant_rules(variant))})
    wide = {"ema_fast.period": {"low": 5, "high": 60}, "ema_slow.period": {"low": 40, "high": 60}}
    ok(
        lab.server,
        "run_sweep",
        {"run_id": a, "plan": ema(8, 120, **variant_rules(variant)), "space": wide, "n_trials": 6},
    )
    b = lab.start("fresh")
    ok(lab.server, "run_backtest", {"run_id": b, "plan": EMA_PLAN})
    narrow = _wf(lab, b, EMA_PLAN, {"ema_fast.period": {"low": 16, "high": 24}, **SLOW})
    assert narrow.is_error and "behaviour family" in narrow.content[0].text
    assert "ema_fast.period [16, 24] does not cover [5, 60]" in narrow.content[0].text
    # the honest space also covers the variant's own submitted slow=120 and its ±10% neighbours
    assert (
        "ema_slow.period [40, 60] does not cover [40, 132]"
        in _wf(lab, b, EMA_PLAN, wide).content[0].text
    )
    honest = {
        "ema_fast.period": {"low": 5, "high": 60},
        "ema_slow.period": {"low": 40, "high": 132},
    }
    assert not _wf(lab, b, EMA_PLAN, honest).is_error


def variant_rules(plan: dict[str, Any]) -> dict[str, Any]:
    return {"long_entry": plan["long_entry"], "long_exit": plan["long_exit"]}


def test_lineage_problems_for_fixed_and_searched_keys() -> None:
    p = parse_plan(EMA_PLAN)
    ranges = {"ema_fast.period": (10.0, 30.0, 5), "ema_slow.period": (50.0, 50.0, 3)}
    assert wf_lineage_problems(p, {"ema_fast.period": (10, 30)}, ranges) == []
    probs = wf_lineage_problems(p, {"ema_fast.period": (11, 30)}, ranges)
    assert len(probs) == 1 and "does not cover [10, 30]" in probs[0]
    moved = {**ranges, "ema_slow.period": (45.0, 55.0, 3)}
    probs = wf_lineage_problems(p, {"ema_fast.period": (10, 30)}, moved)
    assert len(probs) == 1 and "ema_slow.period is not searched (fixed at 50)" in probs[0]


def test_tried_ranges_include_declared_study_spaces_and_other_runs(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    a, b = start(ledger, tmp_path), start(ledger, tmp_path)
    p = parse_plan(EMA_PLAN)
    rng = np.random.default_rng(6)
    study = {"study": "S", "space": {"ema_fast.period": [3.0, 90.0]}}
    trial(
        ledger,
        a,
        rng.normal(0, 0.01, 100),
        plan_dict=ema(25),
        source="optuna",
        cfg=cfg,
        params_json=json.dumps({"ema_fast.period": 25, "_study": study}),
    )
    trial(ledger, b, rng.normal(0, 0.01, 100), plan_dict=EMA_PLAN, cfg=cfg)
    trial(ledger, b, rng.normal(0, 0.01, 100), plan_dict=RSI_PLAN, cfg=cfg)  # other family
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=H, cfg=cfg, plan=EMA_PLAN)
    ranges = tried_ranges(ledger, fam, p)
    assert ranges["ema_fast.period"][:2] == (3.0, 90.0)  # the declared range, not just 20/25
    assert ranges["ema_slow.period"][:2] == (50.0, 50.0)


def test_wf_row_is_the_minimum_over_the_family_in_every_run(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    a, b = start(ledger, tmp_path), start(ledger, tmp_path)
    rng = np.random.default_rng(7)
    r = rng.normal(0.001, 0.01, 400)
    base = trial(ledger, b, r, plan_dict=EMA_PLAN, cfg=cfg)
    record_report(ledger, b, H, "backtest", backtest_payload(base))
    record_report(ledger, b, H, "walk_forward", {"oos_metrics": {"sharpe": 1.5}, "plan": EMA_PLAN})
    assert rows(evaluate_plan(ledger, b, H, cfg))["wf_oos_sharpe_min"]["status"] == "pass"
    # a worse walk-forward of the same logic in ANOTHER run counts
    retuned = ema(25)
    record_report(
        ledger,
        a,
        plan_hash(parse_plan(retuned)),
        "walk_forward",
        {"oos_metrics": {"sharpe": 0.8}, "plan": retuned},
    )
    row = rows(evaluate_plan(ledger, b, H, cfg))["wf_oos_sharpe_min"]
    assert row["status"] == "fail" and row["value"] == 0.8
    # and so does one of a no-op variant whose base trial trades identically
    noop = {**EMA_PLAN, "long_exit": "crosses_below(ema_fast, ema_slow) or ema_fast < 0"}
    trial(ledger, a, r.copy(), plan_dict=noop, cfg=cfg)
    record_report(
        ledger,
        a,
        plan_hash(parse_plan(noop)),
        "walk_forward",
        {"oos_metrics": {"sharpe": 0.3}, "logic_hash": logic_hash(noop)},
    )
    row = rows(evaluate_plan(ledger, b, H, cfg))["wf_oos_sharpe_min"]
    assert row["value"] == 0.3 and "over 3 walk_forward result(s)" in row["note"]
    assert "every research run" in row["note"]


def test_pbo_is_missing_without_a_sweep_and_uses_a_family_sweep_from_another_run(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    a, b = start(ledger, tmp_path), start(ledger, tmp_path)
    record_report(ledger, b, H, "backtest", backtest_payload())  # neighbourhood PBO 0.1
    row = rows(evaluate_plan(ledger, b, H, cfg))["pbo_max"]
    assert row["status"] == "missing" and "run_sweep first" in row["note"]
    rng = np.random.default_rng(8)
    for k in range(cfg.research.min_pbo_trials):
        params = json.dumps({"ema_fast.period": 30 + k, "_study": {"study": "SA"}})
        trial(
            ledger,
            a,
            rng.normal(0, 0.01, 640),
            plan_dict=ema(30 + k),
            source="optuna",
            cfg=cfg,
            params_json=params,
        )
    record_report(
        ledger,
        a,
        plan_hash(parse_plan(ema(30))),
        "sweep",
        {"study": "SA", "window": WINDOW, "plan": ema(30), "logic_hash": logic_hash(EMA_PLAN)},
    )
    row = rows(evaluate_plan(ledger, b, H, cfg))["pbo_max"]
    assert row["status"] in {"pass", "fail"} and "sweep CSCV" in row["note"]
    assert "over 16 design-window Optuna trials" in row["note"]


# =========================================================================== W-a: Phase 7 API


def test_phase7_api_refuses_to_write_into_research_runs(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    app = create_app(small_cfg(tmp_path), ledger_path=db, loader=loader_for(trend_candles()))
    path = write_criteria(tmp_path / "c.toml")
    with Ledger(db) as led:
        registered = start_research_run(led, load_research_config(path), label="r")
        led.start_research_run("custom-id", label="c", config_json="{}", config_sha256="x")
    unregistered = "r-20260101-000000-abcdef"  # matches the server-assigned pattern
    with TestClient(app, base_url="http://127.0.0.1:8788") as client:
        for route in ("/lab/backtest", "/lab/adversarial"):
            for run_id in (registered, unregistered, "custom-id"):
                res = client.post(route, json={"plan": EMA_PLAN, "run_id": run_id})
                assert res.status_code == 422, (route, run_id)
                (err,) = res.json()["errors"]
                assert err["path"] == "run_id" and "strategy-lab MCP server" in err["message"]
        with Ledger(db) as led:
            assert led.count() == 0  # nothing ran
        assert client.post("/lab/backtest", json={"plan": EMA_PLAN}).status_code == 200  # "ui"


# =========================================================================== W-c: neighbourhood


def _neighbourhood_setup(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, plan: dict[str, Any], cap: int
) -> tuple[str, str, list[dict[str, float]]]:
    run = start(ledger, tmp_path)
    h = plan_hash(parse_plan(plan))
    record_report(ledger, run, h, "backtest", backtest_payload(plan=plan, plan_hash=h))
    ovs = neighbourhood_overrides(
        tunable_params(parse_plan(plan)), cfg.stats.neighbourhood_steps, cap
    )
    rng = np.random.default_rng(9)
    for ov in ovs:
        trial(
            ledger,
            run,
            rng.normal(0, 0.01, 50),
            plan_dict=plan_to_dict(with_params(parse_plan(plan), ov)),
            source="neighbourhood",
            cfg=cfg,
            metrics={"total_return": 0.1},
        )
    return run, h, ovs


def test_a_capped_neighbourhood_grid_is_missing_and_names_the_cap(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    assert cfg.stats.neighbourhood_max_trials == 5  # small_cfg: base + 4 of the 8 neighbours
    run, h, ovs = _neighbourhood_setup(
        ledger, tmp_path, cfg, EMA_PLAN, cfg.stats.neighbourhood_max_trials
    )
    assert len(ovs) == 4
    row = rows(evaluate_plan(ledger, run, h, cfg))["neighbourhood_pct"]
    assert row["status"] == "missing" and "only 4 of 8" in row["note"]
    assert "neighbourhood_max_trials = 5 caps the grid at 4" in row["note"]


def test_a_neighbour_lost_to_integer_rounding_is_missing(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    tiny = ema(2, 50)  # 2 x 0.8 = 1.6 and 2 x 1.2 = 2.4 both round back to 2
    run, h, _ = _neighbourhood_setup(ledger, tmp_path, cfg, tiny, 1000)
    row = rows(evaluate_plan(ledger, run, h, cfg))["neighbourhood_pct"]
    assert row["status"] == "missing" and "rounds back to the integer base value" in row["note"]
    assert "ema_fast.period -20%" in row["note"] and "ema_fast.period +20%" in row["note"]


def test_a_side_clipped_at_a_validator_bound_is_covered_and_noted(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    capped = ema(20, 1000)  # ema period's upper bound is 1000: no +side neighbour exists
    run, h, ovs = _neighbourhood_setup(ledger, tmp_path, cfg, capped, 1000)
    assert len(ovs) == 6  # fast both sides (4) + slow 0.8 / 0.9 only
    row = rows(evaluate_plan(ledger, run, h, cfg))["neighbourhood_pct"]
    assert row["status"] == "pass", row
    assert "clipped at a validator bound" in row["note"] and "ema_slow.period +20%" in row["note"]


def test_the_lab_toml_cap_fits_six_tunables_times_the_steps() -> None:
    """Phase 8's complexity cap is ~6 tunables; lab.toml's neighbourhood_max_trials must let
    the runner run every ±20% neighbour of such a plan."""
    real = load_config()
    six = {
        **EMA_PLAN,
        "features": {
            "a": {"ema": {"period": 20}},
            "b": {"ema": {"period": 50}},
            "c": {"sma": {"period": 100}},
            "r": {"rsi": {"period": 14}},
        },
        "long_entry": "crosses_above(a, b) and close > c and r < 70",
        "long_exit": "crosses_below(a, b)",
        "risk": {"position_size_pct": 100, "stop_loss_pct": 5, "take_profit_pct": 10},
    }
    tunables = tunable_params(parse_plan(six))
    assert len(tunables) == 6
    full = neighbourhood_overrides(tunables, real.stats.neighbourhood_steps, 1000)
    capped = neighbourhood_overrides(
        tunables, real.stats.neighbourhood_steps, real.stats.neighbourhood_max_trials
    )
    assert len(full) == 24 and capped == full


# =========================================================================== nits


def test_run_backtest_labels_its_dsr_run_local(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    assert "dsr" not in out["stats"]
    assert set(out["dsr_run_local"]) == {"value", "note"}
    assert "authoritative" in out["dsr_run_local"]["note"]


def test_new_lab_toml_values_changed_since_a_run_started_refuse_it(
    tmp_path: Path, cfg: LabConfig
) -> None:
    from lab.research.runs import check_lab_fingerprint

    with Ledger(tmp_path / "l.db") as led:
        run = start(led, tmp_path)
        moved = replace(cfg, optuna_seed=cfg.optuna_seed + 1)
        with pytest.raises(ValueError, match="optuna_seed"):
            check_lab_fingerprint(led, run, moved)
        loose = replace(cfg, research=replace(cfg.research, family_return_corr=0.5))
        with pytest.raises(ValueError, match="research"):
            check_lab_fingerprint(led, run, loose)
