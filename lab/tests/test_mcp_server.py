"""strategy-lab MCP server: every tool through the in-process MCP client, budgets enforced
server-side, holdout gating, pinned runs, and the holdout data boundary (offline, synthetic
seeded candles, tmp ledger, tmp copy of the proposed success-criteria file).

Helpers at the top are shared with the other ``test_research_*`` modules."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import numpy as np
import pandas as pd
from mcp import Client
from mcp.server.mcpserver import MCPServer

from lab.config import LabConfig, load_config
from lab.ledger import Ledger
from lab.mcp_server import TOOL_NAMES, build_server
from lab.registry import REGISTRY
from lab.research.contract import REPO_ROOT, RESEARCH_RUN_ID_PATTERN
from lab.types import interval_seconds

# --------------------------------------------------------------------------- shared helpers

PROPOSED_CRITERIA = REPO_ROOT / "agents" / "config" / "success_criteria.proposed.toml"
HOLDOUT_START = "2019-10-01"

# Every criterion relaxed except the trend/holdout ones, so a strongly trending synthetic
# market can pass pre-holdout. Holdout pass uses wf_oos_sharpe_min and max_drawdown_max.
LAX = {
    "wf_oos_sharpe_min": 0.5,
    "dsr_min": 0.0,
    "pbo_max": 1.0,
    "beat_buy_hold": False,
    "beat_vol_target": False,
    "random_entry_percentile_min": 0.0,
    "cost_stress_factor": 2.0,
    "require_gradual_delay_decay": False,
    "max_drawdown_max": 1.0,
    "min_trades": 0,
    "min_profitable_regimes": 0,
    "regime_window_days": 90,
    "regime_threshold": 0.10,
    "neighbourhood_pct": 0.0,
    "require_holdout_pass": True,
    "require_crosscheck_match": True,
}

EMA_PLAN: dict[str, Any] = {
    "name": "ema_cross",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"ema_fast": {"ema": {"period": 20}}, "ema_slow": {"ema": {"period": 50}}},
    "long_entry": "crosses_above(ema_fast, ema_slow)",
    "long_exit": "crosses_below(ema_fast, ema_slow)",
    "risk": {"position_size_pct": 100},
}
# State-based (not crossing) rules, so the plan is in the market from the first holdout bar of
# a trend instead of waiting for a fresh cross.
TREND_PLAN: dict[str, Any] = {
    **EMA_PLAN,
    "name": "ema_trend",
    "long_entry": "ema_fast > ema_slow",
    "long_exit": "ema_fast < ema_slow",
}
OTHER_PLAN: dict[str, Any] = {
    **EMA_PLAN,
    "name": "sma_cross",
    "features": {"fast": {"sma": {"period": 10}}, "slow": {"sma": {"period": 40}}},
    "long_entry": "crosses_above(fast, slow)",
    "long_exit": "crosses_below(fast, slow)",
}

# RSI reversion: on trend_candles() its design-window returns correlate ~0 with EMA_PLAN's, so
# it is a different behaviour family (the MA-trend plans above correlate > 0.9 with each other).
RSI_PLAN: dict[str, Any] = {
    **EMA_PLAN,
    "name": "rsi_reversion",
    "features": {"r": {"rsi": {"period": 14}}},
    "long_entry": "crosses_below(r, 30)",
    "long_exit": "r > 50",
}


def write_criteria(path: Path, **overrides: object) -> Path:
    """Copy of the proposed criteria file with ``key = value`` lines replaced."""
    text = PROPOSED_CRITERIA.read_text()
    for key, value in overrides.items():
        literal = ("true" if value else "false") if isinstance(value, bool) else repr(value)
        text, n = re.subn(rf"(?m)^{key}\s*=\s*[^#\n]*", f"{key} = {literal} ", text)
        assert n == 1, key
    path.write_text(text)
    return path


def trend_candles(
    n: int = 3300,
    *,
    seed: int = 5,
    start: str = "2018-07-01",
    drift: float = 0.0015,
    holdout_drift: float | None = None,
    vol: float = 0.01,
    interval: str = "4h",
) -> pd.DataFrame:
    """Seeded geometric random walk with per-bar log drift ``drift``, switching to
    ``holdout_drift`` from ``HOLDOUT_START`` on (all bars tradable)."""
    rng = np.random.default_rng(seed)
    step = pd.Timedelta(seconds=interval_seconds(interval))
    index = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=n, freq=step, name="time")
    mu = np.full(n, drift)
    if holdout_drift is not None:
        mu[index >= pd.Timestamp(HOLDOUT_START, tz="UTC")] = holdout_drift
    close = 6000.0 * np.exp(np.cumsum(rng.normal(mu, vol)))
    open_ = np.empty(n)
    open_[0] = 6000.0
    open_[1:] = close[:-1] * np.exp(rng.normal(0.0, vol / 10, n - 1))
    spread = np.abs(rng.normal(0.0, vol, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(10.0, 100.0, n),
            "tradable": np.ones(n, dtype=bool),
        },
        index=index,
    )


# A noisier seed of the same trending market: TREND_PLAN makes 7 trades in the holdout with a
# net Sharpe of ~2 and a positive return, so it passes a holdout check under LAX (the default
# trend_candles() gives a single holdout trade, below the 5-trade floor).
HOLDOUT_PASS_CANDLES = trend_candles(seed=9)


def loader_for(candles: pd.DataFrame, calls: list[pd.Timestamp] | None = None) -> Any:
    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        if calls is not None:
            calls.append(end)
        return candles.loc[candles.index < end]

    return load


def small_cfg(tmp_path: Path) -> LabConfig:
    base = load_config()
    return replace(
        base,
        data_dir=tmp_path / "data",
        splits=replace(
            base.splits,
            design_start=pd.Timestamp("2018-07-01", tz="UTC"),
            holdout_start=pd.Timestamp(HOLDOUT_START, tz="UTC"),
            wf_train_months=6,
            wf_test_months=3,
            wf_step_months=3,
        ),
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


def call(server: MCPServer, name: str, args: dict[str, Any] | None = None) -> Any:
    """Call one tool through the in-process MCP client; returns the CallToolResult."""

    async def go() -> Any:
        async with Client(server, raise_exceptions=True) as c:
            return await c.call_tool(name, args or {})

    return anyio.run(go)


def ok(server: MCPServer, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    r = call(server, name, args)
    assert not r.is_error, r.content[0].text
    assert isinstance(r.structured_content, dict)
    return r.structured_content


def error_text(server: MCPServer, name: str, args: dict[str, Any] | None = None) -> str:
    r = call(server, name, args)
    assert r.is_error, r.structured_content
    return str(r.content[0].text)


class Lab:
    """A server on a tmp ledger with a loader spy."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        candles: pd.DataFrame | None = None,
        criteria: dict[str, object] | None = None,
        **kwargs: Any,
    ) -> None:
        self.cfg = small_cfg(tmp_path)
        self.ledger_path = tmp_path / "ledger.db"
        self.criteria_path = write_criteria(tmp_path / "criteria.toml", **(criteria or {}))
        self.calls: list[pd.Timestamp] = []
        self.candles = candles if candles is not None else trend_candles()
        self.server = build_server(
            cfg=self.cfg,
            ledger_path=self.ledger_path,
            loader=loader_for(self.candles, self.calls),
            research_config_path=self.criteria_path,
            **kwargs,
        )

    def ledger(self) -> Ledger:
        return Ledger(self.ledger_path)

    def start(self, label: str = "test") -> str:
        return str(ok(self.server, "start_run", {"label": label})["run_id"])


def counts(lab: Lab, run_id: str) -> tuple[int, int, int]:
    with lab.ledger() as led:
        return led.count(run_id), len(led.evidence(run_id)), led.holdout_checks_used(run_id)


SPACE = {"ema_fast.period": {"low": 10, "high": 30}}
# Covers SPACE and the ±10% neighbours run_backtest tries under small_cfg (fast 18/22, slow
# 45/55), so a walk-forward after a backtest + SPACE sweep passes the lineage check.
WF_SPACE = {"ema_fast.period": {"low": 10, "high": 30}, "ema_slow.period": {"low": 40, "high": 60}}


# --------------------------------------------------------------------------- tool list / free


def test_tool_names_are_exactly_the_contract(tmp_path: Path) -> None:
    lab = Lab(tmp_path)

    async def go() -> list[str]:
        async with Client(lab.server, raise_exceptions=True) as c:
            return [t.name for t in (await c.list_tools()).tools]

    assert sorted(anyio.run(go)) == sorted(TOOL_NAMES)
    assert len(TOOL_NAMES) == 12 and "run_crosscheck" in TOOL_NAMES


def test_start_run_freezes_criteria_and_budget(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    out = ok(lab.server, "start_run", {"label": "first"})
    assert re.match(RESEARCH_RUN_ID_PATTERN, out["run_id"])
    assert out["criteria"]["dsr_min"] == 0.95
    assert out["budget"]["max_proposals"] == 15
    assert out["holdout_budget"] == 1  # min(frozen 1, lab.toml 3)
    with lab.ledger() as led:
        run = led.research_run(out["run_id"])
    assert run is not None and run["label"] == "first"
    assert run["config_sha256"] == out["config_sha256"]


def test_start_run_without_criteria_file_explains_the_owner_installs_it(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    lab.criteria_path.unlink()
    msg = error_text(lab.server, "start_run", {"label": "x"})
    assert "success_criteria.proposed.toml" in msg and "not found" in msg
    with lab.ledger() as led:
        assert led.research_runs() == []


def test_list_features_matches_the_registry(tmp_path: Path) -> None:
    out = ok(Lab(tmp_path).server, "list_features")
    assert [f["name"] for f in out["features"]] == list(REGISTRY)
    ema = next(f for f in out["features"] if f["name"] == "ema")
    assert ema["params"][0]["name"] == "period" and ema["params"][0]["kind"] == "int"
    assert {"low", "high", "default"} <= set(ema["params"][0])


def test_validate_plan_valid_returns_hash_and_tunables_without_writing(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    out = ok(lab.server, "validate_plan", {"plan": EMA_PLAN})
    assert out["valid"] is True and out["errors"] == []
    assert len(out["plan_hash"]) == 64
    assert set(out["tunable_params"]) == {"ema_fast.period", "ema_slow.period"}
    with lab.ledger() as led:
        assert led.count() == 0 and led.research_runs() == []


def test_validate_plan_invalid_lists_paths(tmp_path: Path) -> None:
    bad = {**EMA_PLAN, "long_entry": "crosses_above(ema_fats, ema_slow)"}
    out = ok(Lab(tmp_path).server, "validate_plan", {"plan": bad})
    assert out["valid"] is False
    assert out["errors"] and all({"path", "message"} == set(e) for e in out["errors"])
    assert "plan_hash" not in out


# --------------------------------------------------------------------------- lab runs


def test_run_backtest_returns_compact_report_and_records_evidence(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    assert not {"trades", "equity", "benchmark_equity"} & set(out)
    # the report's DSR is run-local (this run's N and V); get_report's row is authoritative
    assert "dsr" not in out["stats"] and out["dsr_run_local"]["value"]["n_trials"] >= 1
    assert "get_report" in out["dsr_run_local"]["note"]
    assert set(out["regimes"]["regimes"]) == {"bear", "sideways", "bull"}
    assert out["budget"]["used"]["proposals"] == 1
    with lab.ledger() as led:
        (ev,) = led.evidence(run_id)
        assert ev["kind"] == "backtest" and ev["plan_hash"] == out["plan_hash"]
        assert "equity" not in ev["payload"] and "regimes" in ev["payload"]
        assert ev["payload"]["stats"]["dsr"]["n_trials"] >= 1  # evidence keeps the raw report
        assert out["budget"]["used"]["backtests"] == led.count(run_id) > 1
    assert "not forward-looking" in out["disclaimer"]


def test_run_backtest_regimes_use_the_runs_frozen_regime_definition(tmp_path: Path) -> None:
    lab = Lab(tmp_path, criteria={"regime_window_days": 30, "regime_threshold": 0.05})
    run_id = lab.start()
    out = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    # 30 days of 4h bars = 180; the basis states the frozen definition, not a code constant.
    assert out["regimes"]["basis"].startswith("trailing 30-day simple return (180 bars)")
    assert "+5% bull, below -5% bear" in out["regimes"]["basis"]


def test_invalid_plan_is_a_readable_tool_error_and_records_nothing(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    bad = {**EMA_PLAN, "features": {"ema_fast": {"emma": {"period": 20}}}}
    msg = error_text(lab.server, "run_backtest", {"run_id": run_id, "plan": bad})
    assert "plan is invalid" in msg and "features.ema_fast" in msg
    assert counts(lab, run_id) == (0, 0, 0)


def test_unknown_run_id_is_refused(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    for tool, args in [
        ("run_backtest", {"plan": EMA_PLAN}),
        ("get_budget", {}),
        ("get_ledger", {}),
        ("holdout_check", {"plan": EMA_PLAN}),
    ]:
        msg = error_text(lab.server, tool, {"run_id": "r-20260101-000000-abcdef", **args})
        assert "never started" in msg
    with lab.ledger() as led:
        assert led.count() == 0


def test_proposal_budget_refuses_a_new_plan_without_recording(tmp_path: Path) -> None:
    lab = Lab(tmp_path, criteria={"max_proposals": 1})
    run_id = lab.start()
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    before = counts(lab, run_id)
    msg = error_text(lab.server, "run_backtest", {"run_id": run_id, "plan": OTHER_PLAN})
    assert "proposal budget exhausted: 1 of 1" in msg
    assert counts(lab, run_id) == before
    # the already-submitted plan may still be worked on
    ok(
        lab.server,
        "run_sweep",
        {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 2},
    )


def test_sweep_larger_than_remaining_backtests_is_refused(tmp_path: Path) -> None:
    lab = Lab(tmp_path, criteria={"max_backtests": 5})
    run_id = lab.start()
    msg = error_text(
        lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 6}
    )
    assert "up to 6 ledger trials but only 5 of 5 remain" in msg
    assert counts(lab, run_id) == (0, 0, 0)


def test_exhausted_backtest_budget_refuses_everything(tmp_path: Path) -> None:
    lab = Lab(tmp_path, criteria={"max_backtests": 3})
    run_id = lab.start()
    ok(lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 3})
    before = counts(lab, run_id)
    assert before[0] == 3
    msg = error_text(
        lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 1}
    )
    assert "backtest budget exhausted: 3 of 3" in msg
    msg = error_text(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    assert "backtest budget exhausted" in msg
    assert counts(lab, run_id) == before


def test_backtest_whose_worst_case_exceeds_the_budget_is_refused(tmp_path: Path) -> None:
    lab = Lab(tmp_path, criteria={"max_backtests": 6})
    run_id = lab.start()
    msg = error_text(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    assert "run_backtest would record up to" in msg
    assert counts(lab, run_id) == (0, 0, 0)


def test_run_sweep_records_every_trial_and_sweep_evidence(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    out = ok(
        lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 3}
    )
    assert out["n_trials"] == 3 and out["space"] == {"ema_fast.period": [10.0, 30.0]}
    assert out["window"]["end"] <= int(pd.Timestamp(HOLDOUT_START, tz="UTC").timestamp())
    assert out["dsr_n"] >= 1 and "biased upward" in out["note"]
    with lab.ledger() as led:
        assert led.count(run_id) == 3
        assert {r.source for r in led.list(run_id=run_id)} == {"optuna"}
        (ev,) = led.evidence(run_id, kind="sweep")
        assert ev["plan_hash"] == out["plan_hash"]


def test_sweep_space_key_must_be_tunable(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    msg = error_text(  # n_trials is required by the tool's input schema
        lab.server,
        "run_sweep",
        {"run_id": run_id, "plan": EMA_PLAN, "space": {"ema_fast.period": {"low": 5, "high": 9}}},
    )
    assert "n_trials" in msg
    msg = error_text(
        lab.server,
        "run_sweep",
        {
            "run_id": run_id,
            "plan": EMA_PLAN,
            "space": {"ema_fats.period": {"low": 5, "high": 9}},
            "n_trials": 2,
        },
    )
    assert "'ema_fats.period' is not a tunable param" in msg and "ema_fast.period" in msg
    msg = error_text(
        lab.server,
        "run_sweep",
        {
            "run_id": run_id,
            "plan": EMA_PLAN,
            "space": {"ema_fast.period": {"low": 5, "high": 9, "step": 2}},
            "n_trials": 2,
        },
    )
    assert "step" in msg and "not supported" in msg
    msg = error_text(
        lab.server,
        "run_sweep",
        {
            "run_id": run_id,
            "plan": EMA_PLAN,
            "space": {"ema_fast.period": {"low": 1, "high": 99999}},
            "n_trials": 2,
        },
    )
    assert "outside the param's bounds" in msg
    assert counts(lab, run_id) == (0, 0, 0)


def test_walk_forward_records_oos_evidence(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    msg = error_text(
        lab.server,
        "walk_forward",
        {"run_id": run_id, "plan": EMA_PLAN, "space": WF_SPACE, "n_trials_per_fold": 2},
    )
    assert "needs a run_backtest of this exact plan" in msg
    assert counts(lab, run_id) == (0, 0, 0)
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    before = counts(lab, run_id)[0]
    out = ok(
        lab.server,
        "walk_forward",
        {"run_id": run_id, "plan": EMA_PLAN, "space": WF_SPACE, "n_trials_per_fold": 2},
    )
    assert len(out["folds"]) == 3  # 15-month design window, 6m train / 3m test / 3m step
    assert out["n_train_trials"] == 6
    assert set(out["oos_metrics"]) >= {"sharpe", "total_return", "max_drawdown", "n_trades"}
    with lab.ledger() as led:
        assert led.count(run_id) == before + 9  # 3 x (2 train + 1 test)
        (ev,) = led.evidence(run_id, kind="walk_forward")
        assert ev["payload"]["oos_metrics"]["sharpe"] == out["oos_metrics"]["sharpe"]


def test_run_adversarial_records_evidence(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    out = ok(lab.server, "run_adversarial", {"run_id": run_id, "plan": EMA_PLAN})
    assert [r["delay"] for r in out["delay"]["rows"]] == [1, 3, 5]
    assert [r["factor"] for r in out["cost_stress"]["rows"]] == [2.0, 4.0]
    with lab.ledger() as led:
        (ev,) = led.evidence(run_id, kind="adversarial")
        assert ev["plan_hash"] == out["plan_hash"]
        assert out["budget"]["used"]["backtests"] == led.count(run_id)


def test_get_report_and_get_budget(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": bt["plan_hash"]})
    assert len(rep["rows"]) == 14 and rep["passed"] is False
    assert "wf_oos_sharpe_min" in rep["missing"]  # no walk_forward evidence yet
    budget = ok(lab.server, "get_budget", {"run_id": run_id})
    assert set(budget) == {"budget", "used", "remaining"}
    assert budget["used"]["proposals"] == 1 and budget["remaining"]["holdout_checks"] == 1


def test_get_report_statistics_block_has_every_part_and_records_nothing(tmp_path: Path) -> None:
    """PLAN.md Phase 11 Done-when: nominal N, effective N, MinBTL, MinTRL and a CPCV path
    distribution (from the plan's design-window sweep) on get_report, as information only."""
    lab = Lab(tmp_path)
    run_id = lab.start()
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    sweep = ok(
        lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 8}
    )
    before = counts(lab, run_id)
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": bt["plan_hash"]})
    assert counts(lab, run_id) == before  # no trials, no evidence: N is unchanged
    st = rep["statistics"]
    tc = st["trial_count"]
    assert tc["nominal"] == tc["dsr_n"] == rep["ledger_n"] and tc["used_for_dsr"] == "nominal"
    # the effective N is a diagnostic beside its time-shuffled null, never the DSR's N
    assert 1.0 <= tc["effective_diagnostic"] <= tc["nominal"]
    assert 1.0 <= tc["effective_null"] <= tc["nominal"]
    assert tc["plans"] >= 1 and tc["rows"] >= 1 and "NOT used for the DSR" in tc["basis"]
    assert st["min_backtest_length"]["n_trials"] == rep["ledger_n"]
    assert st["min_backtest_length"]["years"] >= 0.0
    assert st["min_track_record_length"]["confidence"] == 0.95
    assert st["min_track_record_length"]["design_bars"] == st["min_backtest_length"]["design_bars"]
    cp = st["cpcv"]
    assert "missing" not in cp and cp["n_configs"] == sweep["n_trials"]
    assert len(cp["path_sharpes"]) == cp["n_paths"] >= 2
    assert set(cp["quantiles"]) >= {"min", "median", "max"}
    assert "Optuna trials (4h, 2018-07-01 to 2019-10-01)" in cp["configs_source"]
    assert run_id in cp["configs_source"] and "No new backtests" in cp["configs_source"]
    assert cp["basis"].startswith("upper bound: candidates were chosen by a search")
    # 8 trials < n_startup_trials = 10: every trial of this sweep was a random start-up draw
    cs = st["cpcv_startup"]
    assert "missing" not in cs and cs["n_configs"] == 8
    assert cs["path_sharpes"] == cp["path_sharpes"]
    assert len(rep["rows"]) == 14  # statistics never adds a criterion row


def test_start_run_refuses_an_effective_dsr_trial_count(tmp_path: Path) -> None:
    """Phase 11 review C1: the participation-ratio effective N is a diagnostic only, so a
    criteria file choosing it never starts a run (and records nothing)."""
    lab = Lab(tmp_path, criteria={"dsr_trial_count": "effective"})
    text = error_text(lab.server, "start_run", {"label": "effective-n"})
    assert 'dsr_trial_count = "effective" is not accepted' in text
    assert "diagnostic only" in text and 'use "nominal"' in text
    with lab.ledger() as led:
        assert led.research_runs() == []


def test_get_ledger_limits_and_counts(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    ok(lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 3})
    out = ok(lab.server, "get_ledger", {"run_id": run_id, "limit": 2})
    assert out["total"] == 3 and len(out["items"]) == 2
    assert out["by_source"] == {"optuna": 3} and out["dsr_n"] >= 1
    assert all("returns" not in item for item in out["items"])
    assert "limit must be" in error_text(
        lab.server, "get_ledger", {"run_id": run_id, "limit": 10_000}
    )


def test_get_ledger_omits_the_chain_unless_asked(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    assert "chain" not in ok(lab.server, "get_ledger", {"run_id": run_id})


def test_get_ledger_verify_chain_passes_on_an_untouched_run(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    ok(lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 3})
    out = ok(lab.server, "get_ledger", {"run_id": run_id, "verify_chain": True})
    chain = out["chain"]
    assert set(chain) == {"run_id", "ok", "n_trials", "head", "first_bad_trial_id", "error"}
    assert chain["run_id"] == run_id and chain["ok"] is True
    assert chain["n_trials"] == out["total"] == 3
    assert isinstance(chain["head"], str) and chain["head"]
    assert chain["first_bad_trial_id"] is None and chain["error"] is None


def test_get_ledger_verify_chain_names_the_first_tampered_trial(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    ok(lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 3})
    with lab.ledger() as led:
        ids = sorted(int(r.id or 0) for r in led.list(run_id=run_id, limit=led.count(run_id)))
    # Simulate someone editing the ledger file behind the server's back: lift the append-only
    # trigger, then rewrite the middle trial's trade count.
    conn = sqlite3.connect(lab.ledger_path)
    try:
        conn.execute("DROP TRIGGER trials_no_update")
        conn.execute("UPDATE trials SET n_trades = n_trades + 7 WHERE id = ?", (ids[1],))
        conn.commit()
    finally:
        conn.close()
    chain = ok(lab.server, "get_ledger", {"run_id": run_id, "verify_chain": True})["chain"]
    assert chain["ok"] is False and chain["first_bad_trial_id"] == ids[1]
    assert isinstance(chain["error"], str) and str(ids[1]) in chain["error"]


def test_criteria_snapshot_is_frozen_even_if_the_file_changes(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    write_criteria(lab.criteria_path, dsr_min=0.10)
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": "0" * 64})
    dsr = next(r for r in rep["rows"] if r["criterion"] == "dsr_min")
    assert dsr["threshold"] == 0.95
    # a new run picks the new file up
    new = ok(lab.server, "start_run", {"label": "second"})
    assert new["criteria"]["dsr_min"] == 0.10


# --------------------------------------------------------------------------- pinned / disabled


def test_pinned_server_refuses_start_run_and_other_run_ids(tmp_path: Path) -> None:
    free = Lab(tmp_path)
    run_id = free.start()
    other = free.start("other")
    pinned = build_server(
        cfg=free.cfg,
        ledger_path=free.ledger_path,
        loader=loader_for(free.candles),
        research_config_path=free.criteria_path,
        pinned_run_id=run_id,
    )
    msg = error_text(pinned, "start_run", {"label": "x"})
    assert run_id in msg and "pinned" in msg
    assert run_id in error_text(pinned, "get_budget", {"run_id": other})
    assert ok(pinned, "get_budget", {"run_id": run_id})["used"]["backtests"] == 0


def test_disabled_holdout_is_refused(tmp_path: Path) -> None:
    lab = Lab(tmp_path, holdout_enabled=False)
    run_id = lab.start()
    msg = error_text(lab.server, "holdout_check", {"run_id": run_id, "plan": EMA_PLAN})
    assert "STRATEGY_LAB_DISABLE_HOLDOUT" in msg
    assert counts(lab, run_id) == (0, 0, 0)


# --------------------------------------------------------------------------- holdout via MCP


def _full_pipeline(lab: Lab, run_id: str, plan: dict[str, Any]) -> str:
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": plan})
    ok(lab.server, "run_adversarial", {"run_id": run_id, "plan": plan})
    # covers the backtest's neighbours (fast 18/22, slow 45/55); the sweep gives the PBO basis
    # (lab.toml min_pbo_trials) and, with them, enough commensurable plans for the DSR's V[SR]
    space = {"ema_fast.period": {"low": 15, "high": 25}, "ema_slow.period": {"low": 40, "high": 60}}
    ok(
        lab.server,
        "run_sweep",
        {"run_id": run_id, "plan": plan, "space": space, "n_trials": 16},
    )
    ok(
        lab.server,
        "walk_forward",
        {"run_id": run_id, "plan": plan, "space": space, "n_trials_per_fold": 2},
    )
    # the holdout needs a matched event-driven cross-check (nautilus_trader is installed here)
    cross = ok(lab.server, "run_crosscheck", {"run_id": run_id, "plan": plan})
    assert cross["available"] is True and cross["matched"] is True, cross
    return str(bt["plan_hash"])


def test_holdout_refused_without_spending_when_pre_holdout_criteria_fail(tmp_path: Path) -> None:
    lab = Lab(tmp_path)  # the strict proposed criteria
    run_id = lab.start()
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": TREND_PLAN})
    before = counts(lab, run_id)
    msg = error_text(lab.server, "holdout_check", {"run_id": run_id, "plan": TREND_PLAN})
    assert "pre-holdout success criteria" in msg and "no holdout check was spent" in msg
    assert counts(lab, run_id) == before and before[2] == 0


def test_holdout_pass_returns_only_pass_fail_and_hides_the_trial(tmp_path: Path) -> None:
    lab = Lab(tmp_path, candles=HOLDOUT_PASS_CANDLES, criteria=LAX)
    run_id = lab.start()
    h = _full_pipeline(lab, run_id, TREND_PLAN)
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": h})
    assert rep["pre_holdout_passed"] is True, rep["rows"]
    visible_before = ok(lab.server, "get_ledger", {"run_id": run_id, "limit": 500})

    out = ok(lab.server, "holdout_check", {"run_id": run_id, "plan": TREND_PLAN})
    assert out == {"passed": True, "checks_used": 1, "checks_left": 0}

    visible_after = ok(lab.server, "get_ledger", {"run_id": run_id, "limit": 500})
    assert visible_after["total"] == visible_before["total"]
    assert all(i["window_kind"] != "holdout" for i in visible_after["items"])
    assert visible_after["dsr_n"] == visible_before["dsr_n"]
    with lab.ledger() as led:
        assert any(r.window_kind == "holdout" for r in led.list(run_id=run_id, limit=10_000))
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": h})
    hold = next(r for r in rep["rows"] if r["criterion"] == "require_holdout_pass")
    assert hold["status"] == "pass" and hold["value"] is True

    # one check per hypothesis: a second call is refused and spends nothing
    msg = error_text(lab.server, "holdout_check", {"run_id": run_id, "plan": TREND_PLAN})
    assert "already holdout-checked" in msg
    assert counts(lab, run_id)[2] == 1


def test_only_holdout_check_loads_bars_from_the_holdout(tmp_path: Path) -> None:
    lab = Lab(tmp_path, criteria=LAX)
    run_id = lab.start()
    holdout_start = lab.cfg.splits.holdout_start
    ok(lab.server, "validate_plan", {"plan": TREND_PLAN})
    _full_pipeline(lab, run_id, TREND_PLAN)
    assert lab.calls and all(end <= holdout_start for end in lab.calls)
    n = len(lab.calls)
    ok(lab.server, "holdout_check", {"run_id": run_id, "plan": TREND_PLAN})
    assert len(lab.calls) == n + 1 and lab.calls[-1] > holdout_start


def _no_numbers(obj: object, allowed: Callable[[str], bool]) -> bool:
    if isinstance(obj, dict):
        return all(
            (allowed(k) or not isinstance(v, int | float) or isinstance(v, bool))
            and _no_numbers(v, allowed)
            for k, v in obj.items()
        )
    if isinstance(obj, list):
        return all(not isinstance(v, float) and _no_numbers(v, allowed) for v in obj)
    return True


def test_holdout_failure_reports_neither_rule_names_nor_metrics(tmp_path: Path) -> None:
    candles = trend_candles(holdout_drift=-0.003)
    lab = Lab(tmp_path, candles=candles, criteria=LAX)
    run_id = lab.start()
    _full_pipeline(lab, run_id, TREND_PLAN)
    out = ok(lab.server, "holdout_check", {"run_id": run_id, "plan": TREND_PLAN})
    assert out == {"passed": False, "checks_used": 1, "checks_left": 0}
    assert _no_numbers(out, lambda k: k in {"checks_used", "checks_left"})


def test_holdout_with_too_few_trades_fails_through_the_server(tmp_path: Path) -> None:
    """Default trend_candles(): TREND_PLAN holds the whole holdout trend in 1 trade (profitable,
    high Sharpe). Below the 5-trade floor that is a fail, not a pass."""
    lab = Lab(tmp_path, criteria=LAX)
    run_id = lab.start()
    h = _full_pipeline(lab, run_id, TREND_PLAN)
    assert ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": h})["pre_holdout_passed"]
    out = ok(lab.server, "holdout_check", {"run_id": run_id, "plan": TREND_PLAN})
    assert out == {"passed": False, "checks_used": 1, "checks_left": 0}


def test_a_renamed_copy_of_a_checked_plan_is_refused_in_a_new_run(tmp_path: Path) -> None:
    lab = Lab(tmp_path, candles=HOLDOUT_PASS_CANDLES, criteria=LAX)
    first = lab.start()
    _full_pipeline(lab, first, TREND_PLAN)
    ok(lab.server, "holdout_check", {"run_id": first, "plan": TREND_PLAN})
    second = lab.start("second")
    copy = {
        **TREND_PLAN,
        "name": "fresh",
        "features": {"a": {"ema": {"period": 22}}, "b": {"ema": {"period": 55}}},
        "long_entry": "a > b",
        "long_exit": "a < b",
    }
    msg = error_text(lab.server, "holdout_check", {"run_id": second, "plan": copy})
    assert "already holdout-checked" in msg and "in any run" in msg
    assert counts(lab, second)[2] == 0


# --------------------------------------------------------------------------- Phase 8 review fixes


def test_sweep_space_needs_low_below_high_and_the_frozen_neighbourhood_width(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path)  # proposed criteria: neighbourhood_pct 0.2
    run_id = lab.start()
    args = {"run_id": run_id, "plan": EMA_PLAN, "n_trials": 2}
    msg = error_text(
        lab.server, "run_sweep", {**args, "space": {"ema_fast.period": {"low": 20, "high": 20}}}
    )
    assert "needs low < high" in msg
    msg = error_text(
        lab.server, "run_sweep", {**args, "space": {"ema_fast.period": {"low": 25, "high": 15}}}
    )
    assert "needs low < high" in msg
    # [19, 21]: 2 wide; 2 x 0.2 x |20| = 8 needed
    msg = error_text(
        lab.server, "run_sweep", {**args, "space": {"ema_fast.period": {"low": 19, "high": 21}}}
    )
    assert "neighbourhood_pct 0.2 needs at least 8" in msg and "wider range" in msg
    msg = error_text(
        lab.server,
        "walk_forward",
        {
            "run_id": run_id,
            "plan": EMA_PLAN,
            "space": {"ema_fast.period": {"low": 19, "high": 21}},
            "n_trials_per_fold": 1,
        },
    )
    assert "wider range" in msg
    assert counts(lab, run_id) == (0, 0, 0)
    ok(lab.server, "run_sweep", {**args, "space": {"ema_fast.period": {"low": 16, "high": 24}}})


def test_walk_forward_must_cover_every_range_an_earlier_sweep_of_the_logic_searched(
    tmp_path: Path,
) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    ok(lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 2})
    renamed = {  # same logic, aliases renamed: the lineage still applies
        **EMA_PLAN,
        "name": "renamed",
        "features": {"fast": {"ema": {"period": 20}}, "slow": {"ema": {"period": 50}}},
        "long_entry": "crosses_above(fast, slow)",
        "long_exit": "crosses_below(fast, slow)",
    }
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": renamed})
    before = counts(lab, run_id)
    slow = {"ema_slow.period": {"low": 40, "high": 60}}

    def wf(plan: dict[str, Any], space: dict[str, Any]) -> Any:
        return call(
            lab.server,
            "walk_forward",
            {"run_id": run_id, "plan": plan, "space": space, "n_trials_per_fold": 1},
        )

    narrow = wf(EMA_PLAN, {"ema_fast.period": {"low": 15, "high": 25}, **slow})
    assert narrow.is_error and "ema_fast.period [15, 25] does not cover [10, 30]" in (
        narrow.content[0].text
    )
    dropped = wf(EMA_PLAN, slow)
    assert dropped.is_error and "ema_fast.period is not searched (fixed at 20)" in (
        dropped.content[0].text
    )
    renamed_narrow = wf(renamed, {"fast.period": {"low": 12, "high": 30}, "slow.period": [40, 60]})
    assert renamed_narrow.is_error
    renamed_narrow = wf(
        renamed, {"fast.period": {"low": 12, "high": 30}, "slow.period": {"low": 40, "high": 60}}
    )
    assert renamed_narrow.is_error and "fast.period [12, 30] does not cover" in (
        renamed_narrow.content[0].text
    )
    assert counts(lab, run_id) == before  # refusals run and record nothing
    wide = {"fast.period": {"low": 8, "high": 32}, "slow.period": {"low": 40, "high": 60}}
    assert not wf(renamed, wide).is_error
    # a strategy that does not trade like this one (RSI reversion, corr ~0) is not bound by it
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": RSI_PLAN})
    assert not wf(RSI_PLAN, {"r.period": {"low": 10, "high": 18}}).is_error
    # ... but a differently written exit that trades identically (fast < slow fires exactly at
    # the cross below) is the same behaviour family: corr 1.0, so the sweep binds it
    same = {**EMA_PLAN, "long_exit": "ema_fast < ema_slow"}
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": same})
    bound = wf(same, slow | {"ema_fast.period": {"low": 16, "high": 24}})
    # (the earlier wide walk-forward's [8, 32] is part of the family's history too)
    assert bound.is_error and "ema_fast.period [16, 24] does not cover [8, 32]" in (
        bound.content[0].text
    )


def test_every_evidence_payload_carries_the_logic_hash_and_sweeps_their_study(
    tmp_path: Path,
) -> None:
    from lab.research.logic import logic_hash

    lab = Lab(tmp_path)
    run_id = lab.start()
    lh = logic_hash(EMA_PLAN)
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    sweep = ok(
        lab.server, "run_sweep", {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 2}
    )
    ok(
        lab.server,
        "walk_forward",
        {"run_id": run_id, "plan": EMA_PLAN, "space": WF_SPACE, "n_trials_per_fold": 1},
    )
    ok(lab.server, "run_adversarial", {"run_id": run_id, "plan": EMA_PLAN})
    with lab.ledger() as led:
        evidence = led.evidence(run_id)
    assert {e["kind"] for e in evidence} == {"sweep", "walk_forward", "backtest", "adversarial"}
    assert all(e["payload"]["logic_hash"] == lh for e in evidence)
    by_kind = {e["kind"]: e["payload"] for e in evidence}
    assert by_kind["sweep"]["study"] == sweep["study_name"]
    assert by_kind["sweep"]["space_canonical"] == {"f0.period": [10.0, 30.0]}
    # sweeps and walk-forwards carry the plan, so their logic can be recomputed later
    assert by_kind["sweep"]["plan"]["features"] == EMA_PLAN["features"]
    assert by_kind["walk_forward"]["plan"]["features"] == EMA_PLAN["features"]
    assert len(by_kind["walk_forward"]["study"]) == 3  # one Optuna study per fold
    assert all(isinstance(n, str) for n in by_kind["walk_forward"]["study"])


def test_get_ledger_reports_the_global_research_dsr_n(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    first = lab.start()
    ok(lab.server, "run_sweep", {"run_id": first, "plan": EMA_PLAN, "space": SPACE, "n_trials": 3})
    second = lab.start("second")
    out = ok(lab.server, "get_ledger", {"run_id": second})
    assert out["total"] == 0 and out["run_distinct_plans"] == 0
    with lab.ledger() as led:
        n = led.count_distinct_plans_research()
    assert n >= 1 and out["dsr_n"] == n  # a new run starts at the global N
    assert "every research run" in out["dsr_n_basis"]


def test_a_server_with_a_different_lab_toml_refuses_the_run(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    changed = replace(lab.cfg, costs=replace(lab.cfg.costs, fee_rate=0.002))
    other = build_server(
        cfg=changed,
        ledger_path=lab.ledger_path,
        loader=loader_for(lab.candles),
        research_config_path=lab.criteria_path,
    )
    before = counts(lab, run_id)
    h = ok(lab.server, "validate_plan", {"plan": EMA_PLAN})["plan_hash"]
    for tool, args in [
        ("run_backtest", {"plan": EMA_PLAN}),
        ("run_sweep", {"plan": EMA_PLAN, "space": SPACE, "n_trials": 1}),
        ("run_crosscheck", {"plan": EMA_PLAN}),
        ("get_report", {"plan_hash": h}),
        ("get_budget", {}),
        ("holdout_check", {"plan": EMA_PLAN}),
    ]:
        msg = error_text(other, tool, {"run_id": run_id, **args})
        assert "lab.toml changed since this run started; start a new run" in msg, tool
        assert "costs" in msg
    assert counts(lab, run_id) == before
    # a run started by the changed server works with it
    fresh = str(ok(other, "start_run", {"label": "new costs"})["run_id"])
    ok(other, "get_budget", {"run_id": fresh})


def test_two_server_processes_cannot_overspend_the_backtest_budget(tmp_path: Path) -> None:
    """Two servers (separate thread locks, like two processes) on one ledger and run: the
    per-run flock serializes guard + run, so the worst-case budget is never exceeded."""
    import threading

    lab = Lab(tmp_path, criteria={"max_backtests": 11})  # exactly one backtest's worst case
    run_id = lab.start()
    twin = build_server(
        cfg=lab.cfg,
        ledger_path=lab.ledger_path,
        loader=loader_for(lab.candles),
        research_config_path=lab.criteria_path,
    )
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def go(server: MCPServer) -> None:
        barrier.wait()
        results.append(
            bool(call(server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN}).is_error)
        )

    threads = [threading.Thread(target=go, args=(s,)) for s in (lab.server, twin)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]  # one ran, one was refused
    with lab.ledger() as led:
        assert 0 < led.count(run_id) <= 11
    assert (lab.ledger_path.parent / "locks" / f"{run_id}.lock").exists()


def test_run_crosscheck_needs_a_backtest_and_records_crosscheck_evidence(tmp_path: Path) -> None:
    from lab.research.logic import logic_hash

    lab = Lab(tmp_path)
    run_id = lab.start()
    msg = error_text(lab.server, "run_crosscheck", {"run_id": run_id, "plan": EMA_PLAN})
    assert "no backtest evidence" in msg
    assert counts(lab, run_id) == (0, 0, 0)
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    n_trials = counts(lab, run_id)[0]
    calls_before = len(lab.calls)
    out = ok(lab.server, "run_crosscheck", {"run_id": run_id, "plan": EMA_PLAN})
    assert set(out) >= {
        "matched",
        "available",
        "reason",
        "logic_hash",
        "n_trades_vectorbt",
        "n_trades_event",
        "matched_trades",
        "return_diff_pct",
        "notes",
        "budget",
    }
    assert out["logic_hash"] == logic_hash(EMA_PLAN)
    assert out["available"] is True or out["matched"] is False  # matched implies available
    assert out["n_trades_vectorbt"] == bt["metrics"]["net"]["n_trades"]
    # design window only, and no ledger trial (it confirms the existing backtest)
    assert all(end <= lab.cfg.splits.holdout_start for end in lab.calls[calls_before:])
    assert counts(lab, run_id)[0] == n_trials
    with lab.ledger() as led:
        (ev,) = led.evidence(run_id, kind="crosscheck")
    assert ev["plan_hash"] == bt["plan_hash"] and ev["payload"]["matched"] == out["matched"]
    rep = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": bt["plan_hash"]})
    row = next(r for r in rep["rows"] if r["criterion"] == "require_crosscheck_match")
    assert row["status"] == ("pass" if out["matched"] else "fail")


def test_run_crosscheck_counts_against_an_exhausted_backtest_budget(tmp_path: Path) -> None:
    lab = Lab(tmp_path, criteria={"max_backtests": 11})
    run_id = lab.start()
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": EMA_PLAN})
    with lab.ledger() as led:
        used = led.count(run_id)
    if used < 11:  # top the budget up to exactly exhausted with a cheap sweep
        ok(
            lab.server,
            "run_sweep",
            {"run_id": run_id, "plan": EMA_PLAN, "space": SPACE, "n_trials": 11 - used},
        )
    msg = error_text(lab.server, "run_crosscheck", {"run_id": run_id, "plan": EMA_PLAN})
    assert "backtest budget exhausted" in msg
    with lab.ledger() as led:
        assert led.evidence(run_id, kind="crosscheck") == []
