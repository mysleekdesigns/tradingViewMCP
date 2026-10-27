"""Derivatives plans after the Phase 12 quant review (PLAN.md Phase 12 items 3-4):

* W1: a gated plan whose price clause is (trivially) always true is refused before any ledger
  row, statically in the validator where cheap and on the design data in the runner / MCP /
  holdout / cross-check;
* W2: a derivatives plan is scored from the ONE start of its symbol/interval (latest archive
  series start + [derivatives] warmup_days; tests/test_derivatives_scored_start.py pins that it
  is shared by every plan); benchmarks, nulls, neighbours and walk-forward folds start there
  too, and plans without derivatives features keep their window;
* W3: the holdout check refuses (nothing spent) when the derivatives archive does not cover the
  holdout;
* W4: the run snapshot is per series: only the series a plan reads are compared, and their
  Parquet content on disk is re-hashed;
* Note 2: a design-window load never uses a derivatives row stamped at or after its end
  (``holdout_start``);
* Note 3: a derivatives gate fails CLOSED on a missing value, through ``not`` and ``or``.

Offline: seeded synthetic candles, tmp ledgers and tmp archive directories.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.config import LabConfig
from lab.data.derivatives import FUNDING_COLUMNS, build_manifest
from lab.data.derivatives_columns import (
    DataUnavailableError,
    attach_derivatives,
    derivative_columns,
    require_holdout_coverage,
)
from lab.engine.splits import walk_forward_folds
from lab.evaluate.benchmarks import buy_and_hold
from lab.ledger import Ledger
from lab.plan import compile_plan, parse_plan, validate_plan
from lab.registry.derivatives import DERIVATIVES_COLUMNS, DRV_FUNDING_RATE
from lab.report import seconds
from lab.research import runs as runs_mod
from lab.research.contract import load_research_config
from lab.research.holdout import (
    HoldoutRefusedError,
    _load_full,
    _scored_design_bars,
    run_holdout_check,
)
from lab.research.pipeline import backtest_evidence, crosscheck_evidence
from lab.research.runs import check_derivatives_snapshot, start_research_run
from lab.runner import (
    GateBaseRefusedError,
    _design_window,
    default_loader,
    plan_design_window,
    run_adversarial_report,
    run_backtest_report,
)
from lab.stats import compute_metrics
from lab.types import Window
from tests import test_mcp_server as mcp
from tests.synthetic import make_candles, with_derivatives, write_derivatives_archive
from tests.test_data_derivatives_columns import _write, write_funding
from tests.test_research_derivatives_snapshot import (
    GATED_PLAN,
    GATED_SPACE,
    WITH_DRV,
    drv_lab,
    seed_gated_pre_holdout,
    start_lax,
    write_manifest,
)
from tests.test_runner import small_cfg
from tests.test_runner_derivatives import write_candles

# --------------------------------------------------------------------------- shared data

# 4h bars 2018-07-01 .. ~2020-04 (design_start 2019-01-01 under tests.test_runner.small_cfg),
# low volatility so the only deep drawdown is the crash below
N_BARS = 4000
CRASH_AT = pd.Timestamp("2019-03-01", tz="UTC")  # every price from here on is 60% lower
DRV_FROM = pd.Timestamp("2019-06-01", tz="UTC")  # derivatives archive "starts" here
FZ_PERIOD = 30
WARMUP = pd.Timedelta(days=30)  # lab.toml [derivatives] warmup_days


def crash_candles() -> pd.DataFrame:
    c = make_candles(N_BARS, seed=21, start="2018-07-01", price=8000.0, vol=0.004)
    after = c.index >= CRASH_AT
    for col in ("open", "high", "low", "close"):
        c.loc[after, col] = c.loc[after, col] * 0.4
    return c


CRASH = crash_candles()
CRASH_DRV = with_derivatives(CRASH, seed=8, gaps=False)
CRASH_DRV.loc[CRASH_DRV.index < DRV_FROM, list(DERIVATIVES_COLUMNS)] = np.nan
# every derivatives plan on BTCUSDT 4h: the archive's first timestamp + 30 days (a 4h bar open)
SCORED_START = DRV_FROM + WARMUP
ALL_DRV = with_derivatives(CRASH, seed=8, gaps=False)  # derivatives on every bar

PRICE_GATED: dict[str, Any] = {
    "name": "ema_trend_funding_gate",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {
        "ema_slow": {"ema": {"period": 50}},
        "fz": {"funding_zscore": {"period": FZ_PERIOD}},
    },
    "long_entry": "close > ema_slow and fz < 2",
    "long_exit": "close < ema_slow",
    "risk": {"position_size_pct": 100},
}
PRICE_ONLY: dict[str, Any] = {
    "name": "ema_trend",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"ema_slow": {"ema": {"period": 50}}},
    "long_entry": "close > ema_slow",
    "long_exit": "close < ema_slow",
    "risk": {"position_size_pct": 100},
}


def loader_of(candles: pd.DataFrame) -> Any:
    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        return candles.loc[candles.index < end]

    return load


def trivial_plan(entry: str, features: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": "trivial_price_clause",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {**(features or {}), "fz": {"funding_zscore": {"period": FZ_PERIOD}}},
        "long_entry": entry,
        "long_exit": "close < open",
        "risk": {"position_size_pct": 100},
    }


def write_archive(cfg: LabConfig, first: pd.Timestamp = DRV_FROM, **kw: Any) -> None:
    """The on-disk derivatives archive (manifest) behind the shared scored start."""
    write_derivatives_archive(
        cfg.derivatives_dir,
        first=first,
        end=pd.Timestamp("2020-06-01", tz="UTC"),
        gap_factor=cfg.derivatives.gap_factor,
        **kw,
    )


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    out = small_cfg(tmp_path)
    write_archive(out)
    return out


# --------------------------------------------------------------------------- W1


# validate_plan accepted these before the fix; each makes the gate the de-facto signal
DATA_TRIVIAL = [
    trivial_plan("close > 0 and fz < -1"),
    trivial_plan("volume >= 0 and fz < -1"),
    trivial_plan("rsi >= 0 and fz < -1", {"rsi": {"rsi": {"period": 14}}}),
    trivial_plan("rsi < 101 and fz < -1", {"rsi": {"rsi": {"period": 14}}}),
    trivial_plan("close > ema_slow - ema_slow and fz < -1", {"ema_slow": {"ema": {"period": 50}}}),
    trivial_plan("not (close < 0) and fz < -1"),
]
STATIC_TRIVIAL = [
    trivial_plan("high >= low and fz < -1"),
    trivial_plan("close >= close and fz < -1"),
    trivial_plan("low <= close and fz < -1"),
]


@pytest.mark.parametrize("plan", DATA_TRIVIAL, ids=lambda p: p["long_entry"])
def test_always_true_price_clause_is_refused_before_any_ledger_row(
    cfg: LabConfig, tmp_path: Path, plan: dict[str, Any]
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        for run in (run_backtest_report, run_adversarial_report):
            with pytest.raises(GateBaseRefusedError) as info:
                run(plan, cfg=cfg, ledger=ledger, loader=loader_of(ALL_DRV))
            msg = str(info.value)
            assert "long_entry" in msg and "gate_base_max_true_frac" in msg
            assert "de-facto entry signal" in msg and "close > ema_slow" in msg
            assert "Nothing was recorded" in msg
        assert ledger.count() == 0


@pytest.mark.parametrize("plan", STATIC_TRIVIAL, ids=lambda p: p["long_entry"])
def test_statically_constant_price_clause_is_a_validation_error(plan: dict[str, Any]) -> None:
    parsed, errors = validate_plan(plan)
    assert parsed is None
    assert any(e.path == "long_entry" and "true on every bar" in e.message for e in errors)


def test_static_check_leaves_real_signals_and_price_only_plans_alone() -> None:
    # one real price clause beside a redundant identity: the price clause still selects
    mixed = trivial_plan("close > ema_slow and high >= low and fz < 2", {"ema_slow": {"ema": {}}})
    assert validate_plan(mixed)[0] is not None
    # no derivatives feature: the gating rules do not apply
    plain = {**PRICE_ONLY, "long_entry": "close > ema_slow and high >= low"}
    assert validate_plan(plain)[0] is not None


def test_a_real_price_clause_runs_and_reports_its_true_share(
    cfg: LabConfig, tmp_path: Path
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_backtest_report(PRICE_GATED, cfg=cfg, ledger=ledger, loader=loader_of(ALL_DRV))
    scoring = report["scoring"]
    assert scoring["price_clause"] == "close > ema_slow"
    assert 0.2 < scoring["price_clause_true_share"] < cfg.derivatives.gate_base_max_true_frac


def test_gate_threshold_comes_from_config(cfg: LabConfig, tmp_path: Path) -> None:
    """`close > ema_slow` is refused once the bar is set below its own true share."""
    strict = replace(cfg, derivatives=replace(cfg.derivatives, gate_base_max_true_frac=0.05))
    with Ledger(tmp_path / "l.db") as ledger:
        with pytest.raises(GateBaseRefusedError, match=r"gate_base_max_true_frac 0\.05"):
            run_backtest_report(PRICE_GATED, cfg=strict, ledger=ledger, loader=loader_of(ALL_DRV))
        assert ledger.count() == 0


MCP_TRIVIAL = {
    **GATED_PLAN,
    "name": "always_in_funding_gate",
    "long_entry": "ema_fast > 0 and ema_slow > 0 and fz < 2",
}


@pytest.mark.parametrize(
    ("tool", "extra"),
    [
        ("run_backtest", {}),
        ("run_adversarial", {}),
        ("run_sweep", {"space": GATED_SPACE, "n_trials": 2}),
        ("walk_forward", {"space": GATED_SPACE, "n_trials_per_fold": 1}),
    ],
)
def test_mcp_refuses_an_always_true_price_clause_without_a_trial(
    tmp_path: Path, tool: str, extra: dict[str, Any]
) -> None:
    lab = drv_lab(tmp_path)
    run_id = lab.start()
    msg = mcp.error_text(lab.server, tool, {"run_id": run_id, "plan": MCP_TRIVIAL, **extra})
    assert "gate_base_max_true_frac" in msg and "de-facto entry signal" in msg
    assert mcp.counts(lab, run_id) == (0, 0, 0)


def test_crosscheck_and_holdout_refuse_an_always_true_price_clause(tmp_path: Path) -> None:
    cfg = mcp.small_cfg(tmp_path)
    write_manifest(cfg)
    with Ledger(tmp_path / "l.db") as ledger:
        run_id = start_lax(ledger, tmp_path, cfg)
        backtest_evidence(
            GATED_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=mcp.loader_for(WITH_DRV)
        )
        # a backtest of the real plan exists; its trivially gated copy is refused by the
        # cross-check's own guard (no evidence of it is needed to get that far in the check)
        n = ledger.count(run_id)
        trivial = parse_plan(MCP_TRIVIAL)
        with pytest.raises(GateBaseRefusedError):
            plan_design_window(
                trivial,
                WITH_DRV.loc[WITH_DRV.index < cfg.splits.holdout_start],
                cfg,
            )
        with pytest.raises(HoldoutRefusedError, match="no holdout check was spent"):
            _scored_design_bars(trivial, WITH_DRV, cfg)
        assert ledger.count(run_id) == n and ledger.holdout_checks_used(run_id) == 0


def test_crosscheck_evidence_runs_the_gate_check_before_recording(tmp_path: Path) -> None:
    cfg = mcp.small_cfg(tmp_path)
    write_manifest(cfg)
    with Ledger(tmp_path / "l.db") as ledger:
        run_id = start_lax(ledger, tmp_path, cfg)
        # hand-recorded backtest evidence for the trivial plan (as an older run could hold)
        trivial = parse_plan(MCP_TRIVIAL)
        from lab.plan import plan_hash, plan_to_dict
        from lab.research.evidence import record_report

        h = plan_hash(trivial)
        window = {
            "kind": "design",
            "start": seconds(cfg.splits.design_start),
            "end": seconds(cfg.splits.holdout_start),
        }
        record_report(
            ledger, run_id, h, "backtest", {"plan": plan_to_dict(trivial), "window": window}
        )
        n_rows, n_ev = ledger.count(run_id), len(ledger.evidence(run_id))
        with pytest.raises(GateBaseRefusedError):
            crosscheck_evidence(
                MCP_TRIVIAL, run_id=run_id, cfg=cfg, ledger=ledger, loader=mcp.loader_for(WITH_DRV)
            )
        assert ledger.count(run_id) == n_rows and len(ledger.evidence(run_id)) == n_ev


# --------------------------------------------------------------------------- W2


def test_gated_plan_is_scored_from_the_shared_start(cfg: LabConfig, tmp_path: Path) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_backtest_report(
            PRICE_GATED, cfg=cfg, ledger=ledger, run_id="t", loader=loader_of(CRASH_DRV)
        )
        rows = ledger.list(run_id="t", limit=ledger.count("t"))
    assert report["window"]["start"] == seconds(SCORED_START)
    scoring = report["scoring"]
    assert scoring["scored_from"] == seconds(SCORED_START)
    assert scoring["scored_from_utc"] == "2019-07-01T00:00:00Z"
    assert scoring["design_start_utc"] == "2019-01-01T00:00:00Z"
    # every row (plan, benchmarks, neighbours) starts there; walk-forward folds inside it
    for rec in rows:
        if rec.window_kind == "walk_forward_test":
            assert rec.window_start >= SCORED_START + pd.DateOffset(months=6)
        else:
            assert rec.window_start == SCORED_START, (rec.source, rec.window_start)
    folds = walk_forward_folds(
        Window(SCORED_START, pd.Timestamp(report["window"]["end"], unit="s", tz="UTC"), "design"),
        train_months=6,
        test_months=3,
        step_months=3,
        anchored=cfg.splits.wf_anchored,
    )
    assert folds and [r["test_start"] for r in report["walk_forward"]] == [
        seconds(f.test.start) for f in folds
    ]


def test_crash_before_the_scored_start_is_not_in_any_drawdown(
    cfg: LabConfig, tmp_path: Path
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        gated = run_backtest_report(
            PRICE_GATED, cfg=cfg, ledger=ledger, loader=loader_of(CRASH_DRV)
        )
        plain = run_backtest_report(PRICE_ONLY, cfg=cfg, ledger=ledger, loader=loader_of(CRASH_DRV))
    # the price-only plan is scored from design_start: its buy-and-hold carries the -60% crash
    assert plain["window"]["start"] == seconds(cfg.splits.design_start)
    assert plain["benchmarks"]["buy_and_hold"]["max_drawdown"] <= -0.6
    assert "scoring" not in plain
    # the gated plan and its buy-and-hold start after it
    bh_dd = gated["benchmarks"]["buy_and_hold"]["max_drawdown"]
    assert bh_dd > -0.6 and gated["metrics"]["net"]["max_drawdown"] > -0.6
    history = CRASH_DRV.loc[
        CRASH_DRV.index < pd.Timestamp(gated["window"]["end"], unit="s", tz="UTC")
    ]
    want = buy_and_hold(
        history.loc[history.index >= SCORED_START],
        costs=cfg.costs,
        init_cash=cfg.init_cash,
        cost_history=history,
    )
    assert bh_dd == pytest.approx(compute_metrics(want, "4h").max_drawdown)
    assert gated["benchmarks"]["random_entry"]["n_paths"] == cfg.benchmarks.random_entry_paths


def test_adversarial_report_uses_the_scored_start(cfg: LabConfig, tmp_path: Path) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        out = run_adversarial_report(
            PRICE_GATED, cfg=cfg, ledger=ledger, loader=loader_of(CRASH_DRV)
        )
        assert {r.window_start for r in ledger.list(limit=ledger.count())} == {SCORED_START}
    assert out["window"]["start"] == seconds(SCORED_START)
    assert out["scoring"]["scored_from_utc"] == "2019-07-01T00:00:00Z"


def test_plans_without_derivatives_keep_their_window(cfg: LabConfig) -> None:
    history = CRASH_DRV.loc[CRASH_DRV.index < cfg.splits.holdout_start]
    window, scoring = plan_design_window(parse_plan(PRICE_ONLY), history, cfg)
    assert window == _design_window(history, cfg, "4h") and scoring is None


# the trend fixture's ema_fast > ema_slow is true on ~89% of bars; a crossover selects
CROSS_GATED = {**GATED_PLAN, "long_entry": "crosses_above(ema_fast, ema_slow) and fz < 2"}


MCP_DRV_FROM = pd.Timestamp("2018-10-01", tz="UTC")  # after mcp small_cfg's 2018-07 design_start
MCP_SCORED_START = MCP_DRV_FROM + WARMUP


def mcp_archive_candles(cfg: LabConfig) -> pd.DataFrame:
    """WITH_DRV with no derivatives before MCP_DRV_FROM, and an on-disk archive starting there."""
    candles = WITH_DRV.copy()
    candles.loc[candles.index < MCP_DRV_FROM, list(DERIVATIVES_COLUMNS)] = np.nan
    write_derivatives_archive(
        cfg.derivatives_dir,
        first=MCP_DRV_FROM,
        end="2020-06-01",
        gap_factor=cfg.derivatives.gap_factor,
    )
    return candles


def test_mcp_sweep_and_walk_forward_start_at_the_scored_start(tmp_path: Path) -> None:
    candles = mcp_archive_candles(mcp.small_cfg(tmp_path))  # the Lab's cfg (same tmp_path)
    lab = drv_lab(tmp_path, candles=candles, manifest=False)
    run_id = lab.start()
    start = MCP_SCORED_START
    bt = mcp.ok(lab.server, "run_backtest", {"run_id": run_id, "plan": CROSS_GATED})
    assert bt["window"]["start"] == seconds(start)
    sweep = mcp.ok(
        lab.server,
        "run_sweep",
        {"run_id": run_id, "plan": CROSS_GATED, "space": GATED_SPACE, "n_trials": 2},
    )
    assert sweep["window"]["start"] == seconds(start)
    assert sweep["scoring"]["scored_from"] == seconds(start)
    with lab.ledger() as led:
        optuna_rows = [
            r for r in led.list(run_id=run_id, limit=led.count(run_id)) if r.source == "optuna"
        ]
    assert optuna_rows and {r.window_start for r in optuna_rows} == {start}
    wf_space = {
        "ema_fast.period": {"low": 10, "high": 30},
        "ema_slow.period": {"low": 40, "high": 60},
        "fz.period": {"low": 20, "high": 40},
    }
    wf = mcp.ok(
        lab.server,
        "walk_forward",
        {"run_id": run_id, "plan": CROSS_GATED, "space": wf_space, "n_trials_per_fold": 1},
    )
    assert wf["window"]["start"] == seconds(start)
    assert wf["folds"] and wf["folds"][0]["train"]["start"] == seconds(start)
    assert wf["scoring"]["scored_from"] == seconds(start)


def test_holdout_scales_min_trades_by_scored_design_bars(tmp_path: Path) -> None:
    cfg = mcp.small_cfg(tmp_path)
    candles = mcp_archive_candles(cfg)
    bars, scoring = _scored_design_bars(parse_plan(CROSS_GATED), candles, cfg)
    design = candles.loc[
        (candles.index >= MCP_SCORED_START) & (candles.index < cfg.splits.holdout_start)
    ]
    assert bars == len(design) and scoring is not None
    assert scoring["scored_from"] == seconds(MCP_SCORED_START)


# --------------------------------------------------------------------------- W3


def test_holdout_refuses_when_derivatives_stop_before_the_holdout_ends(tmp_path: Path) -> None:
    """Spot runs to the end of the data; the derivatives archive stops a month earlier."""
    cfg = mcp.small_cfg(tmp_path)
    write_manifest(cfg)
    short = WITH_DRV.copy()
    cut = short.index[-1] - pd.Timedelta(days=30)
    short.loc[short.index > cut, list(DERIVATIVES_COLUMNS)] = np.nan
    with pytest.raises(HoldoutRefusedError, match=r"does not cover the holdout.*no holdout check"):
        _load_full(parse_plan(GATED_PLAN), cfg, mcp.loader_for(short))
    with Ledger(tmp_path / "ledger.db") as led:
        run_id = start_lax(led, tmp_path, cfg)
        seed_gated_pre_holdout(led, run_id, cfg)
        n = led.count(run_id)
        with pytest.raises(HoldoutRefusedError, match="does not cover the holdout"):
            run_holdout_check(
                GATED_PLAN, run_id=run_id, cfg=cfg, ledger=led, loader=mcp.loader_for(short)
            )
        assert led.holdout_checks_used(run_id) == 0 and led.count(run_id) == n


def test_holdout_inputs_are_checked_on_the_holdout_slice(tmp_path: Path) -> None:
    """Derivatives that exist only before holdout_start passed the old full-frame check."""
    cfg = mcp.small_cfg(tmp_path)
    design_only = WITH_DRV.copy()
    design_only.loc[design_only.index >= cfg.splits.holdout_start, list(DERIVATIVES_COLUMNS)] = (
        np.nan
    )
    with pytest.raises(HoldoutRefusedError, match=r"drv_funding_rate has no value"):
        _load_full(parse_plan(GATED_PLAN), cfg, mcp.loader_for(design_only))


def test_holdout_coverage_tolerates_a_gap_within_the_staleness_limit(tmp_path: Path) -> None:
    cfg = mcp.small_cfg(tmp_path)
    tail = WITH_DRV.copy()
    tail.loc[tail.index[-2:], DRV_FUNDING_RATE] = np.nan  # 8h <= 2 x 8h funding staleness
    candles, window = _load_full(parse_plan(GATED_PLAN), cfg, mcp.loader_for(tail))
    assert window.kind == "holdout" and len(candles) == len(tail)
    tail.loc[tail.index[-6:], DRV_FUNDING_RATE] = np.nan  # 24h > 16h
    with pytest.raises(DataUnavailableError, match="does not cover the holdout"):
        require_holdout_coverage(
            GATED_PLAN,
            tail.loc[tail.index >= cfg.splits.holdout_start],
            symbol="BTCUSDT",
            interval="4h",
            stale_after_intervals=cfg.derivatives.stale_after_intervals,
        )


# --------------------------------------------------------------------------- W4


ETH_GATED = {**GATED_PLAN, "symbol": "ETHUSDT"}


def build(cfg: LabConfig) -> None:
    build_manifest(cfg.derivatives_dir, gap_factor=cfg.derivatives.gap_factor)


@pytest.fixture
def snap(tmp_path: Path) -> tuple[LabConfig, Ledger, str]:
    cfg = mcp.small_cfg(tmp_path)
    write_funding(cfg.derivatives_dir)
    build(cfg)
    led = Ledger(tmp_path / "ledger.db")
    research = load_research_config(mcp.write_criteria(tmp_path / "c.toml"))
    run_id = start_research_run(led, research, label="t", cfg=cfg)
    yield cfg, led, run_id
    led.close()


def test_another_symbol_download_does_not_refuse_frozen_plans(
    snap: tuple[LabConfig, Ledger, str],
) -> None:
    cfg, led, run_id = snap
    write_funding(cfg.derivatives_dir, "ETHUSDT", seed=9)
    build(cfg)  # the manifest's bytes change; the BTC series entry does not
    check_derivatives_snapshot(led, run_id, cfg, GATED_PLAN)
    check_derivatives_snapshot(led, run_id, cfg, json.dumps(GATED_PLAN))  # plan JSON too
    with pytest.raises(ValueError, match=r"fundingRate/ETHUSDT was not in the archive.*new run"):
        check_derivatives_snapshot(led, run_id, cfg, ETH_GATED)


def test_parquet_edit_without_manifest_rebuild_is_refused(
    snap: tuple[LabConfig, Ledger, str],
) -> None:
    cfg, led, run_id = snap
    check_derivatives_snapshot(led, run_id, cfg, GATED_PLAN)
    folder = cfg.derivatives_dir / "fundingRate" / "BTCUSDT"
    path = sorted(folder.glob("*.parquet"))[0]
    frame = pd.read_parquet(path)
    frame.iloc[0, 0] = frame.iloc[0, 0] + 1e-3  # one funding value
    frame.to_parquet(path, engine="pyarrow")
    with pytest.raises(ValueError, match=r"data on disk for fundingRate/BTCUSDT changed"):
        check_derivatives_snapshot(led, run_id, cfg, GATED_PLAN)


def test_rebuilt_series_is_refused(snap: tuple[LabConfig, Ledger, str]) -> None:
    cfg, led, run_id = snap
    write_funding(cfg.derivatives_dir, seed=42)
    build(cfg)
    with pytest.raises(ValueError, match=r"manifest changed for fundingRate/BTCUSDT.*new run"):
        check_derivatives_snapshot(led, run_id, cfg, GATED_PLAN)


def test_content_hash_is_computed_once_per_unchanged_series(
    snap: tuple[LabConfig, Ledger, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, led, run_id = snap
    runs_mod._CONTENT_CACHE.clear()
    calls: list[str] = []
    real = runs_mod.drv_data.load_series

    def spy(dataset: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
        calls.append(dataset)
        return real(dataset, *args, **kwargs)

    monkeypatch.setattr(runs_mod.drv_data, "load_series", spy)
    for _ in range(3):
        check_derivatives_snapshot(led, run_id, cfg, GATED_PLAN)
    assert calls == ["fundingRate"]


def test_snapshot_without_series_falls_back_to_the_whole_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = mcp.small_cfg(tmp_path)
    write_funding(cfg.derivatives_dir)
    build(cfg)
    old = runs_mod.derivatives_snapshot(cfg)
    assert old is not None
    # no per-series entries (the first per-run layout); warmup_days is required since the
    # shared scored start (tests/test_derivatives_scored_start.py refuses a snapshot without it)
    legacy = {k: old[k] for k in ("manifest_sha256", "stale_after_intervals", "warmup_days")}
    monkeypatch.setattr(runs_mod, "derivatives_snapshot", lambda _cfg: legacy)
    research = load_research_config(mcp.write_criteria(tmp_path / "c.toml"))
    with Ledger(tmp_path / "ledger.db") as led:
        run_id = start_research_run(led, research, label="legacy", cfg=cfg)
        check_derivatives_snapshot(led, run_id, cfg, GATED_PLAN)
        write_funding(cfg.derivatives_dir, "ETHUSDT", seed=9)
        build(cfg)
        with pytest.raises(ValueError, match="derivatives manifest changed"):
            check_derivatives_snapshot(led, run_id, cfg, GATED_PLAN)


def test_mcp_btc_plan_keeps_running_after_an_eth_download(tmp_path: Path) -> None:
    lab = drv_lab(tmp_path)
    run_id = lab.start()
    mcp.ok(lab.server, "run_backtest", {"run_id": run_id, "plan": GATED_PLAN})
    write_funding(lab.cfg.derivatives_dir, "ETHUSDT", seed=9)
    build(lab.cfg)
    out = mcp.ok(lab.server, "run_adversarial", {"run_id": run_id, "plan": GATED_PLAN})
    assert out["plan_hash"]


def test_changed_gate_threshold_is_refused(snap: tuple[LabConfig, Ledger, str]) -> None:
    cfg, led, run_id = snap
    loose = replace(cfg, derivatives=replace(cfg.derivatives, gate_base_max_true_frac=0.99))
    with pytest.raises(ValueError, match=r"gate_base_max_true_frac changed.*new run"):
        check_derivatives_snapshot(led, run_id, loose, GATED_PLAN)


# --------------------------------------------------------------------------- Note 2

END = pd.Timestamp("2024-02-15", tz="UTC")  # the load's end, like holdout_start


@pytest.fixture
def exact_funding_cfg(tmp_path: Path) -> tuple[LabConfig, pd.DataFrame]:
    """Settlements stamped exactly on the 8h boundaries (one at END itself)."""
    base = small_cfg(tmp_path)
    cfg = replace(base, derivatives=replace(base.derivatives, stale_after_intervals=2.0))
    write_candles(cfg, make_candles(120, seed=4, start="2024-02-01", price=40_000.0))
    times = pd.date_range("2024-01-25", "2024-03-01", freq="8h", tz="UTC", inclusive="left")
    funding = pd.DataFrame(
        {
            FUNDING_COLUMNS[0]: np.arange(len(times), dtype=np.float64) * 1e-5,
            FUNDING_COLUMNS[1]: np.full(len(times), 8.0),
        },
        index=pd.DatetimeIndex(times, name="time"),
    )
    _write(funding, cfg.derivatives_dir / "fundingRate" / "BTCUSDT", "%Y-%m")
    return cfg, funding


def test_design_load_never_uses_a_row_stamped_at_its_end(
    exact_funding_cfg: tuple[LabConfig, pd.DataFrame],
) -> None:
    cfg, funding = exact_funding_cfg
    got = default_loader(cfg)("BTCUSDT", "4h", END)
    last = got.index[-1]
    assert last + pd.Timedelta(hours=4) == END  # the last bar closes exactly at END
    # without the cutoff the row stamped AT END is visible at that close ...
    loose = attach_derivatives(
        got[["open", "high", "low", "close", "volume", "tradable"]],
        "BTCUSDT",
        "4h",
        data_dir=cfg.derivatives_dir,
        stale_after_intervals=2.0,
    )
    assert loose.loc[last, DRV_FUNDING_RATE] == funding.loc[END, FUNDING_COLUMNS[0]]
    # ... the design loader keeps the 16:00 settlement instead
    want = funding.loc[END - pd.Timedelta(hours=8), FUNDING_COLUMNS[0]]
    assert got.loc[last, DRV_FUNDING_RATE] == want
    pd.testing.assert_series_equal(
        got[DRV_FUNDING_RATE].iloc[:-1], loose[DRV_FUNDING_RATE].iloc[:-1]
    )


def test_stamped_before_drops_rows_at_or_after_it(
    exact_funding_cfg: tuple[LabConfig, pd.DataFrame],
) -> None:
    cfg, funding = exact_funding_cfg
    bars = pd.date_range(
        pd.Timestamp("2024-02-10", tz="UTC"), END, freq="4h", inclusive="left", name="time"
    )
    cols = derivative_columns(
        bars,
        "BTCUSDT",
        "4h",
        data_dir=cfg.derivatives_dir,
        stale_after_intervals=2.0,
        stamped_before=END - pd.Timedelta(hours=8),
    )
    # the 16:00 and 00:00 settlements are cut: the last bars fall back to 08:00, then go stale
    v = cols.values[DRV_FUNDING_RATE]
    assert v[-1] == funding.loc[END - pd.Timedelta(hours=16), FUNDING_COLUMNS[0]]


# --------------------------------------------------------------------------- Note 3


def note3_candles() -> pd.DataFrame:
    c = with_derivatives(
        make_candles(600, seed=12, start="2024-01-01", price=30_000.0), seed=2, gaps=False
    )
    c.iloc[200:260, c.columns.get_loc(DRV_FUNDING_RATE)] = np.nan
    return c


def gated(entry: str) -> Any:
    return parse_plan(
        {
            **PRICE_ONLY,
            "name": "note3",
            "features": {"ema_slow": {"ema": {"period": 20}}, "fr": {"funding_rate": {}}},
            "long_entry": entry,
        }
    )


@pytest.mark.parametrize(
    "entry",
    [
        "close > ema_slow and not (fr > 0.0002)",
        "close > ema_slow and fr <= 0.0002",
        "close > ema_slow and (fr < 0 or not (fr > 0.0002))",
    ],
)
def test_derivatives_gate_fails_closed_on_a_missing_value(entry: str) -> None:
    c = note3_candles()
    sig = compile_plan(gated(entry), c)
    missing = np.isnan(c[DRV_FUNDING_RATE].to_numpy())
    price = (c["close"] > sig.features["ema_slow"]).to_numpy()
    assert (price & missing).any()  # the price clause alone would enter on those bars
    assert not sig.entries.to_numpy()[missing].any()


def test_not_gate_equals_its_positive_form_where_defined() -> None:
    c = note3_candles()
    a = compile_plan(gated("close > ema_slow and not (fr > 0.0002)"), c).entries
    b = compile_plan(gated("close > ema_slow and fr <= 0.0002"), c).entries
    pd.testing.assert_series_equal(a, b)
    assert a.any()
