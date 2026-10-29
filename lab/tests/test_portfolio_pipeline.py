"""Portfolio plans through the ledgered research pipeline (PLAN.md Phase 10), end to end over the
``strategy-lab`` MCP tools, on a seeded synthetic multi-coin universe in a temp data dir with a
temp ledger (offline; never the real universe or ledger)."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from lab.config import LabConfig, load_config
from lab.data.universe_build import load_universe_ranks, manifest_path, write_universe
from lab.ledger import SELECTION_SOURCES, Ledger
from lab.mcp_server import build_server
from lab.plan import portfolio as pf
from lab.plan.validator import PlanValidationError
from lab.research.family import family_holdout_conflict
from lab.research.holdout import HoldoutRefusedError
from lab.research.logic import portfolio_logic_hash
from lab.research.portfolio import portfolio_family, run_portfolio_holdout_check
from lab.runner import DataUnavailableError
from tests.synthetic import make_universe_candles
from tests.test_mcp_server import EMA_PLAN, error_text, ok, trend_candles, write_criteria

DESIGN_START = "2019-01-01"
HOLDOUT_START = "2021-01-01"

H1: dict[str, Any] = {
    "kind": "portfolio",
    "name": "xs_mom_28d",
    "description": "test: top 40% of a 5-coin universe on 28-day return",
    "interval": "1d",
    "score": {"trailing_return": {"lookback": 28}},
    "rebalance": {"every": 7, "offset": 0},
    "selection": {"quantile": 0.4, "min_names": 1},
    "weighting": {"equal": {}},
}

# covers the backtest's ±10/20% neighbours of H1 (lookback 22-34, every 6-8, quantile
# 0.32-0.48, max_weight 0.8-1; min_names 1 has no distinct neighbour)
WF_SPACE = {
    "score.trailing_return.lookback": {"low": 20, "high": 36},
    "rebalance.every": {"low": 5, "high": 9},
    "selection.quantile": {"low": 0.3, "high": 0.5},
    "max_weight": {"low": 0.5, "high": 1.0},
}

LAX_PORTFOLIO = {
    "wf_oos_sharpe_min": 0.0,
    "dsr_min": 0.0,
    "pbo_max": 1.0,
    "beat_buy_hold": False,
    "beat_vol_target": False,
    "random_entry_percentile_min": 0.0,
    "cost_stress_factor": 1.0,
    "require_gradual_delay_decay": False,
    "max_drawdown_max": 1.0,
    "min_trades": 0,
    "min_profitable_regimes": 0,
    "neighbourhood_pct": 0.0,
    "require_holdout_pass": True,
    "require_crosscheck_match": True,
}

PORTFOLIO_TABLE_LAX = (
    "\n[portfolio]\nbeat_equal_weight = false\nalpha_p_max = 1.0\nalpha_min_annual = 0.0\n"
)


def portfolio_cfg(tmp_path: Path) -> LabConfig:
    base = load_config()
    return replace(
        base,
        data_dir=tmp_path / "data",
        splits=replace(
            base.splits,
            design_start=pd.Timestamp(DESIGN_START, tz="UTC"),
            holdout_start=pd.Timestamp(HOLDOUT_START, tz="UTC"),
            wf_train_months=6,
            wf_test_months=3,
            wf_step_months=3,
        ),
        universe=replace(base.universe, top_n=5, min_history_days=30, volume_window_days=10),
        portfolio=replace(base.portfolio, null_paths=5),
        benchmarks=replace(base.benchmarks, random_entry_paths=10),
        stats=replace(
            base.stats,
            bootstrap_samples=50,
            monte_carlo_paths=20,
            permutation_paths=3,
            neighbourhood_max_trials=5,
        ),
        optuna_min_trades=0,
    )


def build_universe(cfg: LabConfig, candles: dict[str, pd.DataFrame]) -> Path:
    write_universe(candles, cfg.universe, universe_dir=cfg.universe_dir, missing=[])
    return manifest_path(cfg.universe_dir, cfg.universe.quote, cfg.universe.interval)


class PLab:
    """A server on a tmp ledger and a tmp universe with a loader spy."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        criteria: dict[str, object] | None = None,
        portfolio_table: str | None = None,
        build: bool = True,
        single: pd.DataFrame | None = None,
    ) -> None:
        self.cfg = portfolio_cfg(tmp_path)
        self.ledger_path = tmp_path / "ledger.db"
        self.criteria_path = write_criteria(tmp_path / "criteria.toml", **(criteria or {}))
        if portfolio_table is not None:
            self.criteria_path.write_text(self.criteria_path.read_text() + portfolio_table)
        self.candles = make_universe_candles(start="2019-01-01", n=900)
        self.single = single if single is not None else trend_candles()
        self.calls: list[tuple[str, pd.Timestamp]] = []
        self.manifest = build_universe(self.cfg, self.candles) if build else None

        def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
            self.calls.append((symbol, end))
            if interval == "4h" and symbol == "BTCUSDT":
                frame = self.single
            elif symbol in self.candles and interval == "1d":
                frame = self.candles[symbol]
            else:
                raise DataUnavailableError(f"no {symbol} {interval} in the test universe")
            return frame.loc[frame.index < end]

        self.loader = load
        self.server = build_server(
            cfg=self.cfg,
            ledger_path=self.ledger_path,
            loader=load,
            research_config_path=self.criteria_path,
        )

    def ledger(self) -> Ledger:
        return Ledger(self.ledger_path)

    def start(self) -> str:
        return str(ok(self.server, "start_run", {"label": "portfolio"})["run_id"])


def variant(**changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(H1)
    for path, value in changes.items():
        *parents, leaf = path.split("__")
        node = out
        for part in parents:
            node = node[part]
        node[leaf] = value
    return out


PLAN_H1 = pf.validate_portfolio_plan(H1, interval="1d").plan
assert PLAN_H1 is not None


@pytest.fixture
def lab(tmp_path: Path) -> PLab:
    return PLab(tmp_path)


# --------------------------------------------------------------------------- validate / list


def test_validate_plan_routes_a_portfolio_plan_and_lists_its_tunable_paths(lab: PLab) -> None:
    out = ok(lab.server, "validate_plan", {"plan": H1})
    assert out["valid"] is True and out["kind"] == "portfolio"
    assert out["plan_hash"] == pf.portfolio_plan_hash(
        pf.validate_portfolio_plan(H1, interval="1d").plan  # type: ignore[arg-type]
    )
    assert "selection.quantile" in out["tunable_params"]
    assert "rebalance.offset" not in out["tunable_params"]
    assert out["plan_logic"].startswith("portfolio|score=trailing_return")
    with lab.ledger() as led:
        assert led.count() == 0  # free: no trial


def test_validate_plan_reports_portfolio_errors_with_paths(lab: PLab) -> None:
    bad = variant(selection__quantile=0.9, interval="4h")
    out = ok(lab.server, "validate_plan", {"plan": bad})
    assert out["valid"] is False and out["kind"] == "portfolio"
    paths = {e["path"] for e in out["errors"]}
    assert "selection.quantile" in paths and "interval" in paths


def test_validate_plan_single_symbol_response_is_unchanged(lab: PLab) -> None:
    out = ok(lab.server, "validate_plan", {"plan": EMA_PLAN})
    assert set(out) == {"valid", "errors", "plan_hash", "tunable_params"}
    assert out["valid"] is True and "ema_fast.period" in out["tunable_params"]


def test_list_features_documents_the_portfolio_schema(lab: PLab) -> None:
    out = ok(lab.server, "list_features")
    assert out["features"]  # single-symbol registry unchanged
    port = out["portfolio"]
    assert port["schema"]["properties"]["kind"]
    assert [s["name"] for s in port["score_features"]] == ["trailing_return"]


# --------------------------------------------------------------------------- run_backtest


def test_run_backtest_portfolio_report_fields_ledger_and_budget(lab: PLab) -> None:
    run_id = lab.start()
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    assert out["kind"] == "portfolio" and out["symbol"] == "universe:USDT"
    net = out["metrics"]["net"]
    assert net["sharpe"] is not None and out["metrics"]["gross"]["sharpe"] is not None
    assert net["n_trades"] == out["portfolio"]["n_rebalances_filled"] > 0
    for key in ("annual_turnover", "fees_paid", "impact_cost", "n_rebalances", "n_orders"):
        assert out["portfolio"][key] is not None
    bench = out["benchmarks"]
    for key in ("equal_weight", "btc_buy_and_hold", "btc_vol_target"):
        assert bench[key]["sharpe"] is not None
    rnd = bench["random_selection"]
    assert rnd["n_paths"] == 5 and rnd["percentile"] is not None
    assert rnd["target_exposure_strategy"] is not None
    assert rnd["target_exposure_null_mean"] is not None
    assert "liquidity rank" in rnd["matched_on"] and "volatility-tercile" in rnd["matched_on"]
    assert rnd["liquidity_window"] == lab.cfg.portfolio.null_liquidity_window == 5
    assert "the 5 ranks centred" in rnd["matched_on"]
    keys = {"rank", "pool", "widened", "narrow", "vol_met", "vol_thin", "vol_missed"}
    fb = rnd["match_fallbacks"]
    assert set(fb) == keys and fb["rank"] > 0
    assert "widened" in rnd["match_fallbacks_note"]
    kept = rnd["kept_match_fallbacks"]
    assert set(kept) == keys and kept["rank"] > 0
    assert "kept" in rnd["kept_match_fallbacks_note"]
    assert rnd["liquidity_rank_gap_mean"] is not None and rnd["liquidity_rank_gap_mean"] >= 0
    assert "rank gap" in rnd["liquidity_rank_gap_note"]
    assert 0 < rnd["overlap_chance"] <= 1 and 0 <= rnd["overlap_null"] <= 1
    assert "power" in rnd["overlap_note"]
    side = rnd["vol_only_sidebar"]  # descriptive only: same paths and seed, no liquidity match
    assert "descriptive, not a criterion" in side["label"]
    assert side["n_paths"] == rnd["n_paths"] and side["seed"] == rnd["seed"]
    assert side["percentile"] is not None and 0 < side["p_value"] <= 1
    mix = rnd["book_mix"]
    assert set(mix) == {"strategy", "null"}
    for side in mix.values():
        assert set(side) == {"liquidity_count", "liquidity_weight", "vol_count", "vol_weight"}
        for shares in side.values():
            assert len(shares) == 3 and abs(sum(shares) - 1.0) < 1e-9
    assert "tercile" in rnd["book_mix_note"]
    ab = out["alpha_beta"]
    assert {"alpha_annual", "beta", "alpha_p", "rolling_summary"} <= set(ab)
    assert ab["rolling_days"] == 60
    assert out["costs"]["impact"]["coef"] == lab.cfg.portfolio.impact_coef
    assert "fixed book" in out["costs"]["impact_note"]
    assert "historical" in out["disclaimer"].lower() and "not forward-looking" in out["disclaimer"]
    assert "dsr_run_local" in out and "dsr" not in out["stats"]
    assert out["regimes"]["market"] == "BTCUSDT"
    with lab.ledger() as led:
        n = led.count(run_id)
        recs = led.list(run_id=run_id, limit=n)
        assert {r.symbol for r in recs} == {"universe:USDT"}
        assert sum(r.source == "api" for r in recs) == 1
        assert sum(r.source == "benchmark" for r in recs) == 4
        assert any(r.source == "neighbourhood" for r in recs)
        (ev,) = led.evidence(run_id, kind="backtest")
        assert ev["payload"]["logic_hash"] == portfolio_logic_hash(H1)
    assert out["budget"]["used"]["backtests"] == n
    assert out["budget"]["used"]["proposals"] == 1


def test_every_benchmark_is_scored_from_the_first_decision_row(lab: PLab) -> None:
    run_id = lab.start()
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    first = out["scored_from"]
    assert first is not None and first > out["window"]["start"]
    bars = out["metrics"]["net"]["bars"]
    rows = sum(
        1
        for t in lab.candles["BTCUSDT"].index.union(lab.candles["AAAUSDT"].index)
        if first <= t.value // 10**9 < out["window"]["end"]
    )
    assert bars == rows
    for key in ("equal_weight", "btc_buy_and_hold", "btc_vol_target"):
        assert out["benchmarks"][key]["bars"] == bars, key
    assert out["metrics"]["gross"]["bars"] == bars
    assert out["alpha_beta"]["n"] <= bars


def test_segment_exits_and_the_dispersion_log_are_listed_per_row(lab: PLab) -> None:
    run_id = lab.start()
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    seg = out["segment_exits"]
    assert seg["n"] == len(seg["rows"]) >= 1
    key = next(iter(seg["rows"]))
    assert key.endswith(":GAPUSDT") and seg["rows"][key]["reopen_ratio"] > 1
    log = out["rebalance_log"]
    assert log["columns"] == ["dispersion", "gate_open", "n_names", "target_exposure", "names"]
    assert len(log["rows"]) == out["portfolio"]["n_rebalances"] > 50  # never compacted
    first = next(iter(log["rows"].values()))
    assert isinstance(first[0], float) and first[1] is None and first[2] >= 1
    terciles = out["dispersion_terciles"]["terciles"]
    assert [t["tercile"] for t in terciles] == ["low", "mid", "high"]
    assert sum(t["decisions"] for t in terciles) == out["portfolio"]["n_rebalances"]


def test_a_gated_plan_logs_the_gate_state_per_rebalance(lab: PLab) -> None:
    run_id = lab.start()
    gated = variant(dispersion_gate={"lookback": 28, "min": 0.18})
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": gated})
    states = {row[1] for row in out["rebalance_log"]["rows"].values()}
    assert states == {True, False}  # a 0.18 floor is crossed both ways on this universe


# --------------------------------------------------------------------------- universe manifest


def test_start_run_freezes_the_universe_manifest_sha256(lab: PLab) -> None:
    run_id = lab.start()
    with lab.ledger() as led:
        run = led.research_run(run_id)
    assert run is not None and lab.manifest is not None
    frozen = run["config"]["universe_manifest"]
    assert frozen["sha256"] == hashlib.sha256(lab.manifest.read_bytes()).hexdigest()


def test_a_changed_universe_manifest_refuses_every_portfolio_call(lab: PLab) -> None:
    run_id = lab.start()
    assert lab.manifest is not None
    data = json.loads(lab.manifest.read_text())
    data["built_at"] = "2030-01-01T00:00:00+00:00"  # a rebuild
    lab.manifest.write_text(json.dumps(data, indent=2, sort_keys=True))
    for tool, extra in (
        ("run_backtest", {}),
        ("run_adversarial", {}),
        ("run_crosscheck", {}),
        ("run_sweep", {"space": WF_SPACE, "n_trials": 2}),
        ("walk_forward", {"space": WF_SPACE, "n_trials_per_fold": 1}),
    ):
        msg = error_text(lab.server, tool, {"run_id": run_id, "plan": H1, **extra})
        assert "universe manifest changed" in msg, tool
    with lab.ledger() as led:
        assert led.count(run_id) == 0
    # single-symbol plans are not tied to the universe
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})


def test_a_run_started_before_the_universe_existed_refuses_portfolio_plans(
    tmp_path: Path,
) -> None:
    lab = PLab(tmp_path, build=False)
    run_id = lab.start()
    build_universe(lab.cfg, lab.candles)
    msg = error_text(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    assert "froze no universe manifest" in msg


def test_design_tools_never_load_a_holdout_row(lab: PLab) -> None:
    run_id = lab.start()
    holdout = lab.cfg.splits.holdout_start
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    ok(lab.server, "run_adversarial", {"run_id": run_id, "plan": H1})
    ok(lab.server, "run_crosscheck", {"run_id": run_id, "plan": H1})
    ok(lab.server, "run_sweep", {"run_id": run_id, "plan": H1, "space": WF_SPACE, "n_trials": 2})
    wf = {"run_id": run_id, "plan": H1, "space": WF_SPACE, "n_trials_per_fold": 1}
    ok(lab.server, "walk_forward", wf)
    ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": pf.portfolio_plan_hash(PLAN_H1)})
    assert lab.calls and all(end <= holdout for _, end in lab.calls)
    ranks, _ = load_universe_ranks(lab.cfg.universe_dir, "USDT", "1d", end=holdout)
    assert ranks.index.max() < holdout


# --------------------------------------------------------------------------- sweeps / walk-forward


def test_run_sweep_ledgers_every_trial_and_prunes_infeasible_banding(lab: PLab) -> None:
    run_id = lab.start()
    banded = variant(selection__exit_quantile=0.45)
    space = {
        "selection.quantile": {"low": 0.2, "high": 0.5},
        "selection.exit_quantile": {"low": 0.2, "high": 0.5},
    }
    out = ok(
        lab.server, "run_sweep", {"run_id": run_id, "plan": banded, "space": space, "n_trials": 12}
    )
    assert out["n_trials"] == 12 and out["n_pruned_infeasible"] >= 1
    with lab.ledger() as led:
        recs = led.list(run_id=run_id, limit=100)
        assert len(recs) == 12 and {r.source for r in recs} == {"optuna"}
        errors = [r for r in recs if r.status == "error"]
        assert len(errors) == out["n_pruned_infeasible"]
        assert all("infeasible" in (r.error or "") for r in errors)
        assert all(json.loads(r.params_json)["_study"]["study"] == out["study"] for r in recs)
        # every row, the pruned infeasible ones too, records its own Optuna trial number
        numbers = sorted(json.loads(r.params_json)["_study"]["trial"] for r in recs)
        assert numbers == list(range(12))
        (ev,) = led.evidence(run_id, kind="sweep")
        assert ev["payload"]["study"] == out["study"]
    assert out["budget"]["used"]["backtests"] == 12


def test_sweep_space_must_name_tunable_paths_and_be_wide_enough(lab: PLab) -> None:
    run_id = lab.start()
    msg = error_text(
        lab.server,
        "run_sweep",
        {
            "run_id": run_id,
            "plan": H1,
            "space": {"rebalance.offset": {"low": 0, "high": 6}},
            "n_trials": 2,
        },
    )
    assert "rebalance.offset is fixed" in msg
    msg = error_text(
        lab.server,
        "run_sweep",
        {
            "run_id": run_id,
            "plan": H1,
            "space": {"selection.quantile": {"low": 0.39, "high": 0.41}},
            "n_trials": 2,
        },
    )
    assert "neighbourhood_pct" in msg


def test_walk_forward_needs_a_backtest_and_must_cover_what_was_tried(lab: PLab) -> None:
    run_id = lab.start()
    msg = error_text(
        lab.server,
        "walk_forward",
        {"run_id": run_id, "plan": H1, "space": WF_SPACE, "n_trials_per_fold": 1},
    )
    assert "needs a run_backtest" in msg
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    narrow = {"score.trailing_return.lookback": {"low": 26, "high": 40}}
    msg = error_text(
        lab.server,
        "walk_forward",
        {"run_id": run_id, "plan": H1, "space": narrow, "n_trials_per_fold": 1},
    )
    assert "does not cover" in msg and "rebalance.every is not searched" in msg
    with lab.ledger() as led:
        before = led.count(run_id)
    out = ok(
        lab.server,
        "walk_forward",
        {"run_id": run_id, "plan": H1, "space": WF_SPACE, "n_trials_per_fold": 2},
    )
    folds = out["folds"]
    assert len(folds) >= 3
    assert all(f["test_sharpe"] is not None or f["test_total_return"] is not None for f in folds)
    assert out["oos_metrics"]["sharpe"] is not None
    with lab.ledger() as led:
        assert led.count(run_id) - before == len(folds) * 3  # 2 train + 1 test per fold
        recs = led.list(run_id=run_id, limit=1000)
        kinds = {r.window_kind for r in recs if r.source == "optuna"}
        assert kinds == {"walk_forward_train", "walk_forward_test"}
        (ev,) = led.evidence(run_id, kind="walk_forward")
        assert ev["payload"]["oos_metrics"]["sharpe"] == out["oos_metrics"]["sharpe"]


# ----------------------------------------------------------------------- adversarial / cross-check


def test_run_adversarial_scales_impact_with_costs_and_shifts_decisions(lab: PLab) -> None:
    run_id = lab.start()
    out = ok(lab.server, "run_adversarial", {"run_id": run_id, "plan": H1})
    # delays are whole rebalance periods of stale inputs: 1/3/5 weeks = 7/21/35 rows
    assert [r["delay_periods"] for r in out["delay"]["rows"]] == [1, 3, 5]
    assert [r["delay"] for r in out["delay"]["rows"]] == [7, 21, 35]
    assert [r["factor"] for r in out["cost_stress"]["rows"]] == [2.0, 4.0]
    assert len(out["rebalance_offset"]["rows"]) == 6
    assert set(out["not_applicable"]) == {"sign_flip", "monte_carlo", "permutation"}
    with lab.ledger() as led:
        recs = [r for r in led.list(run_id=run_id, limit=100) if r.source == "adversarial"]
        stressed = [json.loads(r.costs_json) for r in recs if "factor" in json.loads(r.params_json)]
    coef = lab.cfg.portfolio.impact_coef
    assert sorted(c["impact"]["coef"] for c in stressed) == pytest.approx([2 * coef, 4 * coef])
    assert sorted(c["fee_rate"] for c in stressed) == pytest.approx(
        [2 * lab.cfg.costs.fee_rate, 4 * lab.cfg.costs.fee_rate]
    )
    base = out["base"]["sharpe"]
    worst = out["cost_stress"]["rows"][-1]["sharpe"]
    assert worst < base  # x4 costs cost something


def test_run_crosscheck_matches_the_engine_and_needs_a_backtest(lab: PLab) -> None:
    run_id = lab.start()
    msg = error_text(lab.server, "run_crosscheck", {"run_id": run_id, "plan": H1})
    assert "run run_backtest first" in msg
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    out = ok(lab.server, "run_crosscheck", {"run_id": run_id, "plan": H1})
    assert out["matched"] is True and out["available"] is True
    assert out["max_rel_equity_diff"] <= out["tolerance"] == 1e-8
    assert out["n_orders_engine"] == out["n_orders_replay"] > 0
    with lab.ledger() as led:
        (ev,) = led.evidence(run_id, kind="crosscheck")
        assert ev["payload"]["matched"] is True


# --------------------------------------------------------------------------- criteria


def test_get_report_fails_closed_without_portfolio_criteria(lab: PLab) -> None:
    run_id = lab.start()
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": bt["plan_hash"]})
    rows = {r["criterion"]: r for r in rep["rows"]}
    for name in ("beat_equal_weight", "alpha_p_max", "alpha_min_annual"):
        assert rows[name]["status"] == "missing"
        assert "[portfolio]" in rows[name]["note"]
    assert rep["pre_holdout_passed"] is False
    assert rows["min_trades"]["value"] == bt["portfolio"]["n_rebalances_filled"]
    assert "rebalances" in rows["min_trades"]["note"]
    assert "random-selection" in rows["random_entry_percentile_min"]["note"]
    assert "BTC buy-and-hold" in rows["beat_buy_hold"]["note"]


def test_get_report_maps_the_portfolio_criteria_rows(tmp_path: Path) -> None:
    table = "\n[portfolio]\nbeat_equal_weight = true\nalpha_p_max = 0.05\nalpha_min_annual = 0.0\n"
    lab = PLab(tmp_path, portfolio_table=table)
    run_id = lab.start()
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": bt["plan_hash"]})
    rows = {r["criterion"]: r for r in rep["rows"]}
    ew = rows["beat_equal_weight"]
    mine, other = bt["metrics"]["net"]["sharpe"], bt["benchmarks"]["equal_weight"]["sharpe"]
    assert ew["status"] == ("pass" if mine > other else "fail")
    p = rows["alpha_p_max"]
    assert p["value"] == pytest.approx(bt["alpha_beta"]["alpha_p"])
    assert p["status"] == ("pass" if bt["alpha_beta"]["alpha_p"] <= 0.05 else "fail")
    a = rows["alpha_min_annual"]
    assert a["value"] == pytest.approx(bt["alpha_beta"]["alpha_annual"])
    # every single-symbol criterion still has its row, in order, then the portfolio rows
    assert [r["criterion"] for r in rep["rows"]][-3:] == [
        "beat_equal_weight",
        "alpha_p_max",
        "alpha_min_annual",
    ]
    assert rows["wf_oos_sharpe_min"]["status"] == "missing"  # no walk_forward yet
    assert rows["require_crosscheck_match"]["status"] == "missing"


def _pipeline(lab: PLab, run_id: str, plan: dict[str, Any]) -> str:
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": plan})
    ok(lab.server, "run_adversarial", {"run_id": run_id, "plan": plan})
    ok(lab.server, "run_sweep", {"run_id": run_id, "plan": plan, "space": WF_SPACE, "n_trials": 16})
    ok(
        lab.server,
        "walk_forward",
        {"run_id": run_id, "plan": plan, "space": WF_SPACE, "n_trials_per_fold": 2},
    )
    cross = ok(lab.server, "run_crosscheck", {"run_id": run_id, "plan": plan})
    assert cross["matched"] is True
    return str(bt["plan_hash"])


def test_holdout_check_for_a_portfolio_plan_spends_one_check_per_logic(tmp_path: Path) -> None:
    lab = PLab(tmp_path, criteria=LAX_PORTFOLIO, portfolio_table=PORTFOLIO_TABLE_LAX)
    run_id = lab.start()
    h = _pipeline(lab, run_id, H1)
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": h})
    assert rep["pre_holdout_passed"] is True, [r for r in rep["rows"] if r["status"] != "pass"]
    n_calls = len(lab.calls)
    out = ok(lab.server, "holdout_check", {"run_id": run_id, "plan": H1})
    assert set(out) == {"passed", "checks_used", "checks_left"}
    assert out["checks_used"] == 1
    holdout = lab.cfg.splits.holdout_start
    assert any(end > holdout for _, end in lab.calls[n_calls:])  # only the holdout check reads it
    with lab.ledger() as led:
        recs = led.list(run_id=run_id, limit=10_000)
        assert sum(r.window_kind == "holdout" for r in recs) == 1
    # one check per hypothesis: the same plan, a renamed copy, and a re-tuned copy are refused
    retuned = variant(name="renamed", score__trailing_return__lookback=21)
    for plan in (H1, retuned):
        msg = error_text(lab.server, "holdout_check", {"run_id": run_id, "plan": plan})
        assert "already holdout-checked" in msg
    with lab.ledger() as led:
        assert led.holdout_checks_used(run_id) == 1


def test_a_noop_band_and_an_always_open_gate_share_the_plain_plans_holdout(
    tmp_path: Path,
) -> None:
    """plan_logic gives a no-op band (exit_quantile == quantile) and a near-no-op gate
    (min = 0.001) their own logic strings; the return-correlation family joins them to the
    plain plan, so they cannot buy a second holdout check."""
    lab = PLab(tmp_path)
    run_id = lab.start()
    band = variant(name="band", selection__exit_quantile=0.4)
    gate = variant(name="gate", dispersion_gate={"lookback": 28, "min": 0.001})
    for plan in (H1, band, gate):
        ok(lab.server, "run_backtest", {"run_id": run_id, "plan": plan})
    plain_logic = portfolio_logic_hash(H1)
    assert portfolio_logic_hash(band) != plain_logic != portfolio_logic_hash(gate)
    with lab.ledger() as led:
        plans = {}
        for raw in (H1, band, gate):
            p = pf.validate_portfolio_plan(raw, interval="1d").plan
            assert p is not None
            plans[raw["name"]] = (p, pf.portfolio_plan_hash(p))
        for name in ("band", "gate"):
            p, h = plans[name]
            fam = portfolio_family(led, p, h, lab.cfg, lab.loader)
            assert plain_logic in fam.logic_hashes, name
            assert fam.correlated[plain_logic] >= lab.cfg.research.family_return_corr
        # spend the plain plan's single check directly (as if it had passed the bar)
        _, h0 = plans["xs_mom_28d"]
        led.record_holdout_check(run_id, h0, budget=3, logic_hash=plain_logic)
        for name in ("band", "gate"):
            p, h = plans[name]
            fam = portfolio_family(led, p, h, lab.cfg, lab.loader)
            assert family_holdout_conflict(led, fam) is not None
            with pytest.raises(HoldoutRefusedError, match="already holdout-checked"):
                run_portfolio_holdout_check(
                    pf.plan_to_dict(p), run_id=run_id, cfg=lab.cfg, ledger=led, loader=lab.loader
                )
        assert led.holdout_checks_used(run_id) == 1


def test_the_single_symbol_holdout_path_is_untouched_by_portfolio_routing(lab: PLab) -> None:
    run_id = lab.start()
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    msg = error_text(lab.server, "holdout_check", {"run_id": run_id, "plan": EMA_PLAN})
    assert "pre-holdout success criteria" in msg and "no holdout check was spent" in msg


def test_neighbourhood_row_reads_every_portfolio_neighbour(lab: PLab) -> None:
    run_id = lab.start()  # proposed criteria: neighbourhood_pct = 0.2
    three = variant(name="three", selection__min_names=3)
    reports = {}
    for plan in (H1, three):
        bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": plan})
        rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": bt["plan_hash"]})
        reports[plan["name"]] = {r["criterion"]: r for r in rep["rows"]}["neighbourhood_pct"]
    # min_names = 1: 0.8 and 1.2 both round back to 1, so that side is not established
    assert reports["xs_mom_28d"]["status"] == "missing"
    assert "selection.min_names" in reports["xs_mom_28d"]["note"]
    row = reports["three"]
    assert row["status"] in {"pass", "fail"} and 0.0 <= row["value"] <= 1.0
    assert "portfolio tunables" in row["note"] and "rebalance.offset is fixed" in row["note"]


# --------------------------------------------------------------------------- quant-review fixes


def test_a_nonzero_rebalance_offset_is_refused_by_every_portfolio_tool(lab: PLab) -> None:
    """C1: the lab pins rebalance.offset at 0, so the timing-luck table can never be mined for
    the best weekday (a selection DSR's N and the walk-forward lineage would not see)."""
    shifted = variant(rebalance__offset=3)
    out = ok(lab.server, "validate_plan", {"plan": shifted})
    assert out["valid"] is False
    (err,) = out["errors"]
    assert err["path"] == "rebalance.offset"
    assert "fixed by the lab at offset 0" in err["message"]
    assert "calendar, not hypothesis" in err["message"]
    assert "run_adversarial" in err["message"]
    run_id = lab.start()
    for tool, extra in (
        ("run_backtest", {}),
        ("run_adversarial", {}),
        ("run_crosscheck", {}),
        ("run_sweep", {"space": WF_SPACE, "n_trials": 2}),
        ("walk_forward", {"space": WF_SPACE, "n_trials_per_fold": 1}),
        ("holdout_check", {}),
    ):
        msg = error_text(lab.server, tool, {"run_id": run_id, "plan": shifted, **extra})
        assert "fixed by the lab at offset 0" in msg, tool
    with lab.ledger() as led:
        assert led.count(run_id) == 0 and led.holdout_checks_used(run_id) == 0
    with lab.ledger() as led, pytest.raises(PlanValidationError, match=r"rebalance\.offset"):
        run_portfolio_holdout_check(
            shifted, run_id=run_id, cfg=lab.cfg, ledger=led, loader=lab.loader
        )
    # the schema an agent reads shows the pinned value
    schema = ok(lab.server, "list_features")["portfolio"]["schema"]
    assert schema["properties"]["rebalance"]["properties"]["offset"]["const"] == 0


def test_the_timing_luck_table_is_descriptive_and_never_a_selection(lab: PLab) -> None:
    run_id = lab.start()
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    with lab.ledger() as led:
        n0 = led.count_distinct_plans_research(sources=SELECTION_SOURCES)
    adv = ok(lab.server, "run_adversarial", {"run_id": run_id, "plan": H1})
    block = adv["rebalance_offset"]
    assert [r["offset"] for r in block["rows"]] == [1, 2, 3, 4, 5, 6]
    assert block["resubmittable"] is False and block["lab_offset"] == 0
    assert "descriptive only" in block["note"] and "timing luck" in block["verdict"]
    sharpes = [adv["base"]["sharpe"], *(r["sharpe"] for r in block["rows"])]
    assert block["sharpe_spread"] == pytest.approx(max(sharpes) - min(sharpes))
    h = pf.portfolio_plan_hash(PLAN_H1)
    with lab.ledger() as led:
        assert led.count_distinct_plans_research(sources=SELECTION_SOURCES) == n0
        recs = led.list(run_id=run_id, limit=led.count(run_id))
        luck = [r for r in recs if "timing_luck_offset" in json.loads(r.params_json)]
        assert len(luck) == 6
        assert all(r.source == "adversarial" and r.plan_hash == h for r in luck)
        assert "adversarial" not in SELECTION_SOURCES
    # the best weekday cannot be resubmitted
    best = max(block["rows"], key=lambda r: r["sharpe"])
    msg = error_text(
        lab.server,
        "run_backtest",
        {"run_id": run_id, "plan": variant(rebalance__offset=best["offset"])},
    )
    assert "fixed by the lab at offset 0" in msg


def test_the_delay_check_measures_stale_inputs_in_rebalance_periods(lab: PLab) -> None:
    """W1: the delayed variants keep the plan's rebalance weekday and read inputs 1/3/5
    rebalance periods old; the shared delay criterion reads the rows."""
    run_id = lab.start()
    weekly = variant(name="biweekly", rebalance__every=14)
    adv = ok(lab.server, "run_adversarial", {"run_id": run_id, "plan": weekly})
    rows = adv["delay"]["rows"]
    assert [(r["delay_periods"], r["delay"]) for r in rows] == [(1, 14), (3, 42), (5, 70)]
    assert "same rebalance rows" in adv["delay"]["note"].lower()
    assert "14" in adv["delay"]["unit"]
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": adv["plan_hash"]})
    row = {r["criterion"]: r for r in rep["rows"]}["require_gradual_delay_decay"]
    assert "rebalance periods of stale inputs" in row["note"]


def test_a_gated_plan_is_scored_from_the_first_row_it_could_have_invested(lab: PLab) -> None:
    """NOTE 2: a 120-row gate is closed (undefined dispersion) for months; the cash rows
    before it opens are scored, and every benchmark starts on the same row."""
    run_id = lab.start()
    plain = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    gated = variant(name="late_gate", dispersion_gate={"lookback": 120, "min": 0.05})
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": gated})
    assert out["scored_from"] == plain["scored_from"]
    log = out["rebalance_log"]["rows"]
    states = list(log.values())
    assert states[0][1] is False and states[0][3] == 0  # first scored rebalance: gate shut, cash
    opened = next(i for i, row in enumerate(states) if row[1] is True)
    assert opened > 4  # the gate opens weeks later
    bars = out["metrics"]["net"]["bars"]
    assert bars == plain["metrics"]["net"]["bars"]
    for key in ("equal_weight", "btc_buy_and_hold", "btc_vol_target"):
        assert out["benchmarks"][key]["bars"] == bars, key


def test_run_crosscheck_states_it_checks_accounting_only(lab: PLab) -> None:
    run_id = lab.start()
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": H1})
    out = ok(lab.server, "run_crosscheck", {"run_id": run_id, "plan": H1})
    assert "accounting only" in out["scope"]
    assert "look-ahead" in out["scope"] and "cost model" in out["scope"]


def test_a_stale_universe_refuses_the_holdout_before_spending_a_check(tmp_path: Path) -> None:
    """W2 (probe_stale): the rank table was built 20 days into the holdout but candles run
    ~150 days further; the tail would be scored as cash, so the check is refused unspent."""
    full = make_universe_candles(start="2019-01-01", n=900)
    cut = pd.Timestamp(HOLDOUT_START, tz="UTC") + pd.Timedelta(days=20)
    lab = PLab(tmp_path, criteria=LAX_PORTFOLIO, portfolio_table=PORTFOLIO_TABLE_LAX, build=False)
    assert all(lab.candles[s].equals(full[s]) for s in full)  # the loader serves full candles
    build_universe(lab.cfg, {s: c.loc[c.index < cut] for s, c in full.items()})
    run_id = lab.start()
    h = _pipeline(lab, run_id, H1)
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": h})
    assert rep["pre_holdout_passed"] is True, [r for r in rep["rows"] if r["status"] != "pass"]
    msg = error_text(lab.server, "holdout_check", {"run_id": run_id, "plan": H1})
    assert "universe rank table ends" in msg and "universe_build build" in msg
    assert "No holdout check was spent" in msg
    with lab.ledger() as led:
        assert led.holdout_checks_used(run_id) == 0
        recs = led.list(run_id=run_id, limit=led.count(run_id))
        assert not any(r.window_kind == "holdout" for r in recs)
        assert not led.evidence(run_id, kind="holdout")
