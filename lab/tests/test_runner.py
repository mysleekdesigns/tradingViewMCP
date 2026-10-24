"""Report runner: BacktestReport JSON contract, design-window discipline, ledger policy."""

from __future__ import annotations

import itertools
import json
import math
from collections import Counter
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import lab.runner as runner
from lab.config import LabConfig, load_config
from lab.ledger import Ledger, costs_to_json
from lab.plan import PlanValidationError, parse_plan, tunable_params
from lab.report import ledger_item, seconds
from lab.runner import (
    DataUnavailableError,
    archive_cli_hint,
    default_loader,
    neighbourhood_overrides,
    pbo_split_count,
    run_adversarial_report,
    run_backtest_report,
)
from lab.stats import deflated_sharpe
from lab.types import Metrics, ParamSpec, TrialRecord
from tests.synthetic import make_candles

EMA_PLAN = {
    "name": "ema_cross",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"ema_fast": {"ema": {"period": 20}}, "ema_slow": {"ema": {"period": 50}}},
    "long_entry": "crosses_above(ema_fast, ema_slow)",
    "long_exit": "crosses_below(ema_fast, ema_slow)",
    "risk": {"position_size_pct": 100},
}

METRIC_KEYS = {
    "total_return", "cagr", "volatility", "sharpe", "sortino", "max_drawdown", "win_rate",
    "profit_factor", "n_trades", "exposure", "turnover", "bars",
}  # fmt: skip
REPORT_KEYS = {
    "trial_id", "run_id", "plan_hash", "plan", "symbol", "interval", "window", "costs",
    "metrics", "benchmarks", "stats", "walk_forward", "walk_forward_kind", "trades", "equity",
    "benchmark_equity", "warnings", "disclaimer", "elapsed_ms",
}  # fmt: skip


def small_cfg(tmp_path: Path) -> LabConfig:
    base = load_config()
    return replace(
        base,
        data_dir=tmp_path,
        splits=replace(base.splits, wf_train_months=6, wf_test_months=3, wf_step_months=3),
        benchmarks=replace(base.benchmarks, random_entry_paths=20),
        stats=replace(
            base.stats, bootstrap_samples=200, monte_carlo_paths=100, permutation_paths=10
        ),
    )


# 2018-07 .. ~2020-01: starts before design_start (2019-01-01) so warm-up history exists.
CANDLES = make_candles(3300, seed=17, start="2018-07-01", price=6000.0, vol=0.012)


def fake_loader(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
    return CANDLES.loc[CANDLES.index < end]


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path: Path):  # type: ignore[no-untyped-def]
    led = Ledger(tmp_path / "ledger.db")
    yield led
    led.close()


@pytest.fixture
def report(cfg: LabConfig, ledger: Ledger) -> dict:
    return run_backtest_report(EMA_PLAN, cfg=cfg, ledger=ledger, run_id="t", loader=fake_loader)


# --------------------------------------------------------------------------- contract


def test_report_has_exactly_the_contract_fields(report: dict) -> None:
    assert set(report) == REPORT_KEYS
    assert set(report["metrics"]) == {"net", "gross"}
    assert set(report["metrics"]["net"]) == METRIC_KEYS
    assert set(report["benchmarks"]) == {"buy_and_hold", "vol_target", "random_entry"}
    assert set(report["benchmarks"]["vol_target"]) == METRIC_KEYS
    assert set(report["benchmarks"]["random_entry"]) == {
        "n_paths", "sharpe_median", "sharpe_p95", "percentile",
    }  # fmt: skip
    assert set(report["stats"]) == {"dsr", "pbo", "sharpe_ci"}
    assert set(report["stats"]["dsr"]) == {"dsr", "psr", "n_trials", "sr0_annual"}
    assert set(report["stats"]["pbo"]) == {"pbo", "n_trials", "n_splits", "basis"}
    assert report["stats"]["pbo"]["basis"] == "neighbourhood"
    assert report["walk_forward_kind"] == "fixed_params"
    assert set(report["stats"]["sharpe_ci"]) == {"low", "high", "level"}
    assert set(report["window"]) == {"kind", "start", "end"}
    assert set(report["costs"]) == {
        "fee_rate", "slippage_rate", "slippage_vol_mult", "slippage_cap",
    }  # fmt: skip
    assert set(report["benchmark_equity"]) == {"buy_and_hold", "vol_target"}
    for row in report["walk_forward"]:
        assert set(row) == {"fold", "test_start", "test_end", "sharpe", "total_return", "n_trades"}
    for t in report["trades"]:
        assert set(t) == {
            "entry_time", "entry_price", "exit_time", "exit_price", "pnl", "return_pct",
            "exit_reason",
        }  # fmt: skip


def test_report_is_strict_json(report: dict) -> None:
    text = json.dumps(report, allow_nan=False)  # NaN/inf must already be null
    assert json.loads(text) == report


def test_times_are_unix_seconds_and_equity_starts_at_one(report: dict, cfg: LabConfig) -> None:
    w = report["window"]
    assert w["kind"] == "design"
    assert w["start"] == seconds(cfg.splits.design_start)  # max(design_start, first bar)
    assert isinstance(w["end"], int) and w["end"] <= seconds(cfg.splits.holdout_start)
    eq = report["equity"]
    assert eq[0] == {"time": w["start"], "value": 1.0}
    assert all(isinstance(p["time"], int) for p in eq)
    assert all(a["time"] < b["time"] for a, b in itertools.pairwise(eq))
    assert report["benchmark_equity"]["buy_and_hold"][0]["value"] == 1.0
    assert len(report["benchmark_equity"]["vol_target"]) == len(eq)
    t0 = report["trades"][0]
    assert isinstance(t0["entry_time"], int) and w["start"] <= t0["entry_time"] < w["end"]


def test_report_numbers_match_the_metrics(report: dict) -> None:
    net, gross = report["metrics"]["net"], report["metrics"]["gross"]
    assert net["n_trades"] == len(report["trades"]) > 0
    assert gross["sharpe"] >= net["sharpe"]  # costs only ever subtract
    final = report["equity"][-1]["value"]
    assert final - 1.0 == pytest.approx(net["total_return"], rel=1e-9)
    assert report["benchmarks"]["random_entry"]["n_paths"] == 20
    assert 0.0 <= report["benchmarks"]["random_entry"]["percentile"] <= 100.0
    ci = report["stats"]["sharpe_ci"]
    assert ci["low"] < net["sharpe"] < ci["high"] and ci["level"] == 0.95
    assert report["stats"]["pbo"]["n_splits"] == 16 and 0 <= report["stats"]["pbo"]["pbo"] <= 1
    assert "not forward-looking" in report["disclaimer"] and "0.100%" in report["disclaimer"]
    assert report["elapsed_ms"] > 0


def test_walk_forward_rows_are_test_windows_inside_the_design_window(report: dict) -> None:
    rows = report["walk_forward"]
    assert len(rows) >= 2
    w = report["window"]
    for prev, row in itertools.pairwise(rows):
        assert row["test_start"] == prev["test_end"]  # step == test length: contiguous tests
    assert rows[0]["test_start"] > w["start"] and rows[-1]["test_end"] <= w["end"]


def test_few_trades_raise_a_warning(report: dict) -> None:
    assert any("trades (< 100)" in w for w in report["warnings"])


# --------------------------------------------------------------------------- holdout discipline


def test_holdout_rows_never_reach_the_report(cfg: LabConfig, ledger: Ledger) -> None:
    late = make_candles(900, seed=5, start="2025-06-01", price=60_000.0)
    seen_end: list[pd.Timestamp] = []

    def leaky_loader(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        seen_end.append(end)
        return late  # ignores `end` and returns holdout rows too

    rep = run_backtest_report(EMA_PLAN, cfg=cfg, ledger=ledger, loader=leaky_loader)
    holdout = seconds(cfg.splits.holdout_start)
    assert seen_end == [cfg.splits.holdout_start]
    assert rep["window"]["end"] == holdout
    assert max(p["time"] for p in rep["equity"]) < holdout
    assert all(t["entry_time"] < holdout for t in rep["trades"])


def test_signals_use_warm_up_history_before_the_window(cfg: LabConfig, ledger: Ledger) -> None:
    rep = run_backtest_report(EMA_PLAN, cfg=cfg, ledger=ledger, loader=fake_loader)
    trimmed = run_backtest_report(
        EMA_PLAN,
        cfg=cfg,
        ledger=ledger,
        loader=lambda s, i, e: CANDLES.loc[(CANDLES.index >= cfg.splits.design_start)],
    )
    # Same window, but without warm-up the EMAs start cold, so the first trades differ.
    assert rep["window"] == trimmed["window"]
    assert rep["trades"][0] != trimmed["trades"][0] or len(rep["trades"]) != len(trimmed["trades"])


# --------------------------------------------------------------------------- ledger policy


def test_every_backtest_is_recorded_with_its_source(report: dict, ledger: Ledger) -> None:
    rows = ledger.list(run_id="t", limit=1000)
    by_source = Counter(r.source for r in rows)
    n_neigh = report["stats"]["pbo"]["n_trials"] - 1
    n_folds = len(report["walk_forward"])
    assert n_folds >= 2
    assert by_source == {"api": 1 + n_folds, "benchmark": 3, "neighbourhood": n_neigh}
    api = ledger.get(report["trial_id"], include_returns=True)
    assert api.source == "api" and api.plan_hash == report["plan_hash"]
    assert api.window_kind == "design" and api.returns is not None
    names = sorted(json.loads(r.plan_json)["name"] for r in rows if r.source == "benchmark")
    assert names == ["benchmark:buy_and_hold", "benchmark:random_entry", "benchmark:vol_target"]


def test_dsr_n_counts_distinct_plans_and_rerunning_does_not_inflate_it(
    report: dict, cfg: LabConfig, ledger: Ledger
) -> None:
    n = report["stats"]["dsr"]["n_trials"]
    assert n == report["stats"]["pbo"]["n_trials"]  # base + its distinct neighbours
    again = run_backtest_report(EMA_PLAN, cfg=cfg, ledger=ledger, run_id="t", loader=fake_loader)
    assert again["stats"]["dsr"]["n_trials"] == n
    assert again["stats"]["dsr"]["dsr"] == pytest.approx(report["stats"]["dsr"]["dsr"])
    other = dict(EMA_PLAN, features={"ema_fast": {"ema": {"period": 30}},
                                     "ema_slow": {"ema": {"period": 90}}})  # fmt: skip
    third = run_backtest_report(other, cfg=cfg, ledger=ledger, run_id="t", loader=fake_loader)
    assert third["stats"]["dsr"]["n_trials"] > n
    assert ledger.count("t") > 2 * n  # raw rows keep growing; N does not


def test_dsr_is_at_most_psr_with_many_trials(report: dict) -> None:
    d = report["stats"]["dsr"]
    assert d["n_trials"] > 1 and d["sr0_annual"] > 0
    assert d["dsr"] <= d["psr"]


def test_engine_failure_is_recorded_as_an_error_trial(
    cfg: LabConfig, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise ValueError("synthetic engine failure")

    monkeypatch.setattr(runner, "run_signals", boom)
    with pytest.raises(ValueError, match="synthetic engine failure"):
        run_backtest_report(EMA_PLAN, cfg=cfg, ledger=ledger, run_id="err", loader=fake_loader)
    (row,) = ledger.list(run_id="err")
    assert row.status == "error" and row.source == "api"
    assert row.error == "synthetic engine failure"
    assert ledger_item(row)["status"] == "error"


def test_adversarial_report_records_variants_and_one_null_summary(
    cfg: LabConfig, ledger: Ledger
) -> None:
    rep = run_adversarial_report(EMA_PLAN, cfg=cfg, ledger=ledger, run_id="adv", loader=fake_loader)
    json.dumps(rep, allow_nan=False)
    assert [r["delay"] for r in rep["delay"]["rows"]] == [1, 3, 5]
    assert [r["factor"] for r in rep["cost_stress"]["rows"]] == [2.0, 4.0]
    assert rep["permutation"]["n_paths"] == 10
    assert len(rep["verdicts"]) == 5 and rep["delay"]["verdict"].startswith("delay:")
    by_source = Counter(r.source for r in ledger.list(run_id="adv", limit=100))
    assert by_source == {"api": 1, "adversarial": 6, "benchmark": 1}
    assert ledger.count_distinct_plans("adv") == 1  # variants are not selection trials


# --------------------------------------------------------------------------- errors / data


def test_invalid_plan_raises_with_every_error(cfg: LabConfig, ledger: Ledger) -> None:
    bad = dict(EMA_PLAN, interval="7m", long_entry="ema_fats > 1")
    with pytest.raises(PlanValidationError) as info:
        run_backtest_report(bad, cfg=cfg, ledger=ledger, loader=fake_loader)
    assert {e.path for e in info.value.errors} >= {"interval"}
    assert ledger.count() == 0


def test_missing_archive_names_the_download_command(cfg: LabConfig, ledger: Ledger) -> None:
    with pytest.raises(DataUnavailableError) as info:
        run_backtest_report(EMA_PLAN, cfg=cfg, ledger=ledger)  # tmp data dir: nothing there
    assert "cd lab && uv run python -m lab.data.archive BTCUSDT 4h 2018-01 " in str(info.value)


def test_default_loader_reads_parquet_only_before_the_holdout(tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    folder = cfg.parquet_dir / "BTCUSDT" / "4h"
    folder.mkdir(parents=True)
    c = make_candles(400, seed=1, start="2025-09-01")
    for month, part in c.groupby(c.index.strftime("%Y-%m")):
        part.to_parquet(folder / f"{month}.parquet")
    got = default_loader(cfg)("BTCUSDT", "4h", cfg.splits.holdout_start)
    assert len(got) and got.index[-1] < cfg.splits.holdout_start


def test_archive_hint_uses_the_last_full_month() -> None:
    hint = archive_cli_hint("ETHUSDT", "1h", pd.Timestamp("2026-10-24 12:00", tz="UTC"))
    assert hint == "cd lab && uv run python -m lab.data.archive ETHUSDT 1h 2018-01 2026-09"
    jan = archive_cli_hint("BTCUSDT", "4h", pd.Timestamp("2026-01-03", tz="UTC"))
    assert jan.endswith("2018-01 2025-12")


def test_data_ending_before_the_design_window_is_unavailable(
    cfg: LabConfig, ledger: Ledger
) -> None:
    old = make_candles(300, seed=2, start="2018-01-01")
    with pytest.raises(DataUnavailableError, match="design window"):
        run_backtest_report(EMA_PLAN, cfg=cfg, ledger=ledger, loader=lambda s, i, e: old)


# --------------------------------------------------------------------------- neighbourhood


def test_neighbourhood_varies_one_param_at_a_time_nearest_first() -> None:
    plan = parse_plan(EMA_PLAN)
    ovs = neighbourhood_overrides(tunable_params(plan), (0.8, 0.9, 1.0, 1.1, 1.2), 16)
    assert ovs == [
        {"ema_fast.period": 18.0},
        {"ema_slow.period": 45.0},
        {"ema_fast.period": 22.0},
        {"ema_slow.period": 55.0},
        {"ema_fast.period": 16.0},
        {"ema_slow.period": 40.0},
        {"ema_fast.period": 24.0},
        {"ema_slow.period": 60.0},
    ]


def test_neighbourhood_respects_the_cap_bounds_and_rounding() -> None:
    spec = {"a.period": ParamSpec("period", "int", 2.0, 1.0, 1000.0, "")}
    # 2 * 0.9 = 1.8 -> 2 (= base, skipped); 2 * 1.1 = 2.2 -> 2 (skipped); 0.8 -> 2; 1.2 -> 2
    assert neighbourhood_overrides(spec, (0.8, 0.9, 1.0, 1.1, 1.2), 16) == []
    spec = {"a.x": ParamSpec("x", "float", 10.0, 9.5, 10.5, "")}
    assert neighbourhood_overrides(spec, (0.8, 0.9, 1.1), 16) == [{"a.x": 9.5}, {"a.x": 10.5}]
    many = {f"f{i}.p": ParamSpec("p", "float", 10.0, 1.0, 100.0, "") for i in range(10)}
    assert len(neighbourhood_overrides(many, (0.8, 0.9, 1.1, 1.2), 16)) == 15


def test_pbo_split_count_is_the_largest_even_value_that_fits() -> None:
    assert pbo_split_count(10_000, 16) == 16
    assert pbo_split_count(9, 16) == 8
    assert pbo_split_count(1, 16) == 0
    assert pbo_split_count(100, 15) == 14


def test_plan_without_neighbours_reports_null_pbo_with_a_warning(
    cfg: LabConfig, ledger: Ledger
) -> None:
    plan = dict(
        EMA_PLAN,
        features={"e": {"ema": {"period": 1}}},
        long_entry="close > prev(e, 1)",
        long_exit="close < prev(e, 1)",
    )
    rep = run_backtest_report(plan, cfg=cfg, ledger=ledger, loader=fake_loader)
    assert rep["stats"]["pbo"] == {
        "pbo": None, "n_trials": 1, "n_splits": 0, "basis": "neighbourhood",
    }  # fmt: skip
    assert any("PBO not computed" in w for w in rep["warnings"])
    assert rep["stats"]["dsr"]["n_trials"] == 1


def test_bootstrap_and_random_entry_are_seeded(cfg: LabConfig, tmp_path: Path) -> None:
    with Ledger(tmp_path / "a.db") as a, Ledger(tmp_path / "b.db") as b:
        ra = run_backtest_report(EMA_PLAN, cfg=cfg, ledger=a, loader=fake_loader)
        rb = run_backtest_report(EMA_PLAN, cfg=cfg, ledger=b, loader=fake_loader)
    assert ra["stats"] == rb["stats"]
    assert ra["benchmarks"] == rb["benchmarks"]
    assert not math.isnan(ra["stats"]["sharpe_ci"]["low"])
    assert np.isfinite(ra["benchmarks"]["random_entry"]["sharpe_median"])


# --------------------------------------------------------------------------- review fixes


def test_walk_forward_folds_are_fixed_param_ledger_rows_that_do_not_change_n(
    report: dict, ledger: Ledger
) -> None:
    assert report["walk_forward_kind"] == "fixed_params"
    assert runner.WALK_FORWARD_WARNING in report["warnings"]
    assert "python -m lab.search --walk-forward" in runner.WALK_FORWARD_WARNING
    folds = [r for r in ledger.list(run_id="t", limit=1000) if r.window_kind == "walk_forward_test"]
    folds.sort(key=lambda r: r.window_start)
    rows = report["walk_forward"]
    assert len(folds) == len(rows) >= 2
    for rec, row in zip(folds, rows, strict=True):
        assert rec.source == "api" and rec.plan_hash == report["plan_hash"]
        assert seconds(rec.window_start) == row["test_start"]
        assert seconds(rec.window_end) == row["test_end"]
        assert rec.n_trades == row["n_trades"]
        full = ledger.get(int(rec.id), include_returns=True)  # type: ignore[arg-type]
        assert full.returns is not None and len(full.returns) > 0
    n = report["stats"]["dsr"]["n_trials"]
    assert (
        ledger.count_distinct_plans("t", interval="4h") == n == report["stats"]["pbo"]["n_trials"]
    )


def _foreign_row(plan_hash: str, *, interval: str, kind: str, start: pd.Timestamp,
                 end: pd.Timestamp, sharpe: float) -> TrialRecord:  # fmt: skip
    return TrialRecord(
        run_id="t", source="manual", status="ok", plan_hash=plan_hash, plan_json="{}",
        params_json="{}", symbol="BTCUSDT", interval=interval, window_kind=kind,  # type: ignore[arg-type]
        window_start=start, window_end=end, metrics_json="{}", sharpe_per_bar=sharpe,
        n_trades=5, costs_json=costs_to_json(load_config().costs),
    )  # fmt: skip


def test_dsr_n_and_variance_ignore_other_intervals_windows_and_fold_rows(
    cfg: LabConfig, tmp_path: Path
) -> None:
    with Ledger(tmp_path / "clean.db") as clean:
        ref = run_backtest_report(EMA_PLAN, cfg=cfg, ledger=clean, run_id="t", loader=fake_loader)
        w = ref["window"]
        start = pd.Timestamp(w["start"], unit="s", tz="UTC")
        end = pd.Timestamp(w["end"], unit="s", tz="UTC")
        clean_var = clean.selection_sharpe_variance(
            "t", interval="4h", window_start=start, window_end=end,
            costs_json=costs_to_json(cfg.costs),
        )  # fmt: skip
        base_returns = clean.get(ref["trial_id"], include_returns=True).returns
    with Ledger(tmp_path / "noisy.db") as noisy:
        # A 1d plan on the same window, and a wild walk-forward fold Sharpe on the same window.
        noisy.record(_foreign_row("d" * 64, interval="1d", kind="design", start=start, end=end,
                                  sharpe=3.0))  # fmt: skip
        noisy.record(_foreign_row("f" * 64, interval="4h", kind="walk_forward_test",
                                  start=start, end=end, sharpe=-2.0))  # fmt: skip
        # A 4h plan on another window: counted in N (it was tried) but not in the variance.
        other = (start - pd.Timedelta(days=200), start - pd.Timedelta(days=10))
        noisy.record(_foreign_row("o" * 64, interval="4h", kind="design", start=other[0],
                                  end=other[1], sharpe=2.5))  # fmt: skip
        rep = run_backtest_report(EMA_PLAN, cfg=cfg, ledger=noisy, run_id="t", loader=fake_loader)
    n_ref = ref["stats"]["dsr"]["n_trials"]
    # The 1d row is excluded by interval; the fold row and the other-window row are 4h tries.
    assert rep["stats"]["dsr"]["n_trials"] == n_ref + 2
    # The variance is the clean one (only same-window, same-cost, non-fold 4h Sharpes): the
    # report's DSR is exactly DSR(base returns, N = n_ref + 2, V = clean V).
    assert np.isfinite(clean_var) and base_returns is not None
    expected = deflated_sharpe(base_returns, n_trials=n_ref + 2, sharpe_variance=clean_var)
    assert rep["stats"]["dsr"]["dsr"] == pytest.approx(expected.dsr, rel=1e-12)


def test_client_chosen_run_id_warns_that_n_is_run_scoped(cfg: LabConfig, ledger: Ledger) -> None:
    ui = run_backtest_report(EMA_PLAN, cfg=cfg, ledger=ledger, loader=fake_loader)
    assert any("client-chosen run_id 'ui'" in w for w in ui["warnings"])
    srv = run_backtest_report(
        EMA_PLAN, cfg=cfg, ledger=ledger, run_id="srv-1", loader=fake_loader, run_id_assigned=True
    )
    assert not any("client-chosen" in w for w in srv["warnings"])


def _metrics(sharpe: float = 1.0, n_trades: int = 200) -> Metrics:
    return Metrics(0.1, 0.1, 0.4, sharpe, 1.0, -0.2, 0.5, 1.2, n_trades, 0.5, 5.0, 1000)


def _warn(pbo: float | None) -> list[str]:
    return runner._warnings(
        net=_metrics(), gross=_metrics(), vol_target=_metrics(0.5), dsr=0.99, n_trials=9,
        pbo=pbo, pbo_trials=9, random_pct=99.0, random_paths=200, wf_rows=[{"fold": 0}],
        run_id="srv", run_id_assigned=True,
    )  # fmt: skip


def test_pbo_warning_uses_the_phase_8_bar_and_names_its_basis() -> None:
    assert runner.PBO_THRESHOLD == 0.2 and runner.DSR_THRESHOLD == 0.95
    assert runner.RANDOM_ENTRY_PERCENTILE == 95.0 and runner.MIN_TRADES == 100
    (text,) = [w for w in _warn(0.25) if w.startswith("PBO")]
    assert text == (
        "PBO 0.25 over the ±20% parameter neighbourhood exceeds the Phase 8 bar of 0.2 "
        "(neighbourhood PBO measures parameter stability, not search overfitting)"
    )
    assert not [w for w in _warn(0.15) if w.startswith("PBO")]
    assert _warn(0.15) == [runner.WALK_FORWARD_WARNING]


def test_vol_target_rebalance_band_comes_from_config(cfg: LabConfig, tmp_path: Path) -> None:
    def vt(band: float) -> tuple[dict, dict]:
        c = replace(cfg, benchmarks=replace(cfg.benchmarks, vol_min_rebalance=band))
        with Ledger(tmp_path / f"vt{band}.db") as led:
            rep = run_backtest_report(EMA_PLAN, cfg=c, ledger=led, loader=fake_loader)
            (row,) = [
                r for r in led.list(limit=100)
                if json.loads(r.plan_json).get("benchmark") == "vol_target"
            ]  # fmt: skip
        return rep["benchmarks"]["vol_target"], json.loads(row.params_json)

    every, every_params = vt(0.0)
    banded, banded_params = vt(0.25)
    assert every_params["min_rebalance"] == 0.0 and banded_params["min_rebalance"] == 0.25
    assert every["turnover"] > banded["turnover"]


def test_every_engine_row_records_its_cost_model(cfg: LabConfig, ledger: Ledger) -> None:
    run_adversarial_report(EMA_PLAN, cfg=cfg, ledger=ledger, run_id="adv", loader=fake_loader)
    rows = ledger.list(run_id="adv", limit=100)
    base_json = costs_to_json(cfg.costs)
    for r in rows:
        params = json.loads(r.params_json)
        if params.get("variant") == "cost_stress":
            assert r.costs_json == costs_to_json(cfg.costs.scaled(params["factor"]))
        else:
            assert r.costs_json == base_json


def test_runner_warms_the_engine_slippage_on_the_history(
    cfg: LabConfig, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[pd.DatetimeIndex, pd.DataFrame | None]] = []
    real = runner.run_signals

    def spy(candles: pd.DataFrame, *args: object, **kwargs: object) -> object:
        seen.append((candles.index, kwargs.get("cost_history")))  # type: ignore[arg-type]
        return real(candles, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "run_signals", spy)
    run_backtest_report(EMA_PLAN, cfg=cfg, ledger=ledger, loader=fake_loader)
    assert seen
    for index, hist in seen:
        assert hist is not None
        assert hist.index[-1] == index[-1] and hist.index[0] < index[0]
