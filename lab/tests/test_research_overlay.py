"""PLAN.md Phase 15 overlay judging (offline, seeded synthetic candles, tmp ledger): the measured
overlay block (same bars for overlay and buy-and-hold, features on the full history, the 1x
rerun equals the ledgered trial, the exposure-matched constant mix, the seeded paired bootstrap
with an independent re-implementation of the non-inferiority p and the bootstrap bias), the
per-episode comparison (hand-built known values), the overlay rows (not_testable, Bonferroni
with N_overlay, the underpowered note, episodes improved, break-even walk, the fold check's
episode gate, the PBO note, no "edge" wording), N_overlay across runs, the one-parameter-set
rule per overlay logic, and overlay trials counting in the edge N but never in its V[SR].

Helpers at the top are shared with ``test_research_overlay_holdout`` and
``test_mcp_overlay_pipeline``."""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.config import LabConfig
from lab.engine.splits import design_view, slice_window, walk_forward_folds
from lab.evaluate.benchmarks import buy_and_hold
from lab.ledger import SELECTION_SOURCES, Ledger
from lab.plan import compile_plan, parse_plan, plan_hash
from lab.research.contract import OverlayCriteria, load_overlay_config, load_research_config
from lab.research.criteria import CriterionStatus
from lab.research.logic import logic_hash
from lab.research.overlay import (
    OVERLAY_COST_MULTIPLES,
    cost_multiples,
    episode_comparison,
    overlay_block,
)
from lab.research.overlay_criteria import (
    OVERLAY_ROWS,
    check_overlay_untuned,
    evaluate_overlay_plan,
    overlay_submissions,
    overlay_trial_count,
    param_key,
)
from lab.research.pipeline import adversarial_evidence, backtest_evidence, crosscheck_evidence
from lab.research.runs import start_research_run
from lab.stats.bootstrap import stationary_bootstrap_indices
from lab.stats.drawdown import DrawdownEpisode, cdar, drawdown_series, exposure_matched_returns
from lab.stats.metrics import sharpe_per_bar
from lab.stats.paired import bootstrap_block_bars, sharpe_difference_test
from lab.types import Window, bars_per_year, interval_seconds
from tests.test_mcp_server import loader_for, small_cfg, trend_candles, write_criteria
from tests.test_research_overlay_contract import write_overlay

# --------------------------------------------------------------------------- shared helpers

HOLDOUT_START = "2019-10-01"  # small_cfg's holdout
OVERLAY_SAMPLES = 100  # bootstrap resamples: p floor 1/101, below alpha / N for N <= 4
BLOCK_DAYS = 10  # stationary-bootstrap mean block for the tests (60 bars of 4h)


def crash_candles(
    n: int = 3300,
    *,
    seed: int = 3,
    start: str = "2018-07-01",
    up: float = 0.004,
    down: float = -0.006,
    up_len: int = 260,
    down_len: int = 90,
    vol: float = 0.008,
    holdout_drift: float | None = None,
) -> pd.DataFrame:
    """Seeded 4h boom-bust market: ``up_len`` bars of drift ``up`` then ``down_len`` bars of
    drift ``down``, repeated (each bust is a ~40% buy-and-hold drawdown that recovers to a new
    high, so the design window holds many separate deep episodes). ``holdout_drift`` replaces
    the drift from ``HOLDOUT_START`` on (a calm holdout)."""
    rng = np.random.default_rng(seed)
    step = pd.Timedelta(seconds=interval_seconds("4h"))
    index = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=n, freq=step, name="time")
    mu = np.empty(n)
    k, phase = 0, 0
    while k < n:
        length = up_len if phase == 0 else down_len
        mu[k : k + length] = up if phase == 0 else down
        k += length
        phase = 1 - phase
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


CRASH_CANDLES = crash_candles()
# flat while the close is below its 60-bar SMA: on CRASH_CANDLES it sidesteps every bust
MA_OVERLAY: dict[str, Any] = {
    "kind": "overlay",
    "name": "below_sma60",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"ma": {"sma": {"period": 60}}},
    "flat_when": "close < ma",
}
# the same rule as an edge plan: identical logic hash (logic_hash ignores "kind")
MA_EDGE_SAME_LOGIC: dict[str, Any] = {
    "name": "sma60_state",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"m": {"sma": {"period": 60}}},
    "long_entry": "not (close < m)",
    "long_exit": "close < m",
}
# a different logic that trades alike (returns correlated far above family_return_corr)
MA_EDGE_ALIKE: dict[str, Any] = {
    **MA_EDGE_SAME_LOGIC,
    "name": "sma60_cross_state",
    "long_entry": "close > m",
}
RSI_OVERLAY: dict[str, Any] = {
    "kind": "overlay",
    "name": "rsi_hot",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"r": {"rsi": {"period": 14}}},
    "flat_when": "r > 70",
}


def overlay_cfg(tmp_path: Path, **stats: Any) -> LabConfig:
    base = small_cfg(tmp_path)
    return replace(
        base, stats=replace(base.stats, **{"bootstrap_samples": OVERLAY_SAMPLES, **stats})
    )


def overlay_toml(tmp_path: Path, **overrides: object) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return write_overlay(
        tmp_path / "overlay.toml", **{"bootstrap_mean_block_days": BLOCK_DAYS, **overrides}
    )


def overlay_criteria(tmp_path: Path, **overrides: object) -> OverlayCriteria:
    return load_overlay_config(overlay_toml(tmp_path, **overrides)).criteria


def start_overlay_run(led: Ledger, tmp_path: Path, cfg: LabConfig, **overrides: object) -> str:
    research = load_overlay_config(overlay_toml(tmp_path, **overrides))
    return start_research_run(led, research, label="overlay", cfg=cfg)


def start_edge_run(led: Ledger, tmp_path: Path, cfg: LabConfig) -> str:
    research = load_research_config(write_criteria(tmp_path / "criteria.toml"))
    return start_research_run(led, research, label="edge", cfg=cfg)


def edge_word(text: str) -> list[str]:
    """Every word starting with "edge" (case-insensitive). "ledger" / "hedge" do not count."""
    return re.findall(r"\bedge\w*", text, flags=re.IGNORECASE)


@pytest.fixture
def led(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as ledger:
        yield ledger


def _overlay_evidence(
    led: Ledger, tmp_path: Path, plan: dict[str, Any], candles: pd.DataFrame = CRASH_CANDLES
) -> tuple[LabConfig, str, dict[str, Any]]:
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    out = backtest_evidence(plan, run_id=run_id, cfg=cfg, ledger=led, loader=loader_for(candles))
    return cfg, run_id, out


# --------------------------------------------------------------------------- the block


def test_cost_multiples_include_the_frozen_minimum(tmp_path: Path) -> None:
    assert cost_multiples(overlay_criteria(tmp_path)) == OVERLAY_COST_MULTIPLES
    odd = overlay_criteria(tmp_path, break_even_cost_multiple_min=2.5)
    assert cost_multiples(odd) == (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)


def test_block_scores_both_from_the_first_decision_bar_after_warmup(
    led: Ledger, tmp_path: Path
) -> None:
    cfg, _, out = _overlay_evidence(led, tmp_path, MA_OVERLAY)
    block = out["overlay"]
    history = design_view(CRASH_CANDLES, cfg.splits)
    warmup = compile_plan(parse_plan(MA_OVERLAY), history).warmup_bars
    assert block["warmup_bars"] == warmup == 59  # sma 60
    # small_cfg's design window starts at the first bar, so the warm-up moves the start
    assert block["window"]["start"] == int(history.index[warmup].timestamp())
    assert block["window"]["end"] == int(pd.Timestamp(HOLDOUT_START, tz="UTC").timestamp())
    assert block["window"]["bars"] == len(history) - warmup
    assert block["interval"] == "4h"
    assert "overlay" not in out["benchmarks"] and "ungated" not in out["benchmarks"]
    assert "dsr" not in out["stats"]


def test_one_x_rerun_equals_the_ledgered_trial(led: Ledger, tmp_path: Path) -> None:
    cfg, run_id, out = _overlay_evidence(led, tmp_path, MA_OVERLAY)
    rec = led.get(int(out["trial_id"]), include_returns=True)
    assert rec.returns is not None and rec.source == "api"
    n = out["overlay"]["window"]["bars"]
    assert not np.any(rec.returns[:-n])  # flat during the warm-up
    # a different trial (buy-and-hold's benchmark row) is refused, nothing recorded
    bh_trial = next(
        r
        for r in led.list(run_id=run_id, limit=100, include_returns=True)
        if r.source == "benchmark" and r.returns is not None
    )
    with pytest.raises(ValueError, match="does not reproduce ledgered trial"):
        overlay_block(
            MA_OVERLAY,
            cfg=cfg,
            ledger=led,
            run_id=run_id,
            loader=loader_for(CRASH_CANDLES),
            criteria=overlay_criteria(tmp_path),
            trial_id=int(bh_trial.id or 0),
        )


def _scored(cfg: LabConfig, block: dict[str, Any]) -> pd.DataFrame:
    history = design_view(CRASH_CANDLES, cfg.splits)
    start = pd.Timestamp(block["window"]["start"], unit="s", tz="UTC")
    end = pd.Timestamp(block["window"]["end"], unit="s", tz="UTC")
    return slice_window(history, Window(start=start, end=end, kind="design"))


def test_exposure_matched_benchmark_is_the_constant_mix_of_buy_and_hold(
    led: Ledger, tmp_path: Path
) -> None:
    """Independent of the module: buy-and-hold rerun on the block's bars, scaled by the block's
    average exposure, has the block's exposure-matched CDaR; the overlay's CDaR (from the
    ledgered trial) and buy-and-hold's CDaR match too."""
    cfg, _, out = _overlay_evidence(led, tmp_path, MA_OVERLAY)
    block = out["overlay"]
    candles = _scored(cfg, block)
    history = design_view(CRASH_CANDLES, cfg.splits)
    bh = buy_and_hold(
        candles, costs=cfg.costs, init_cash=cfg.init_cash, cost_history=history
    ).returns.to_numpy()
    rec = led.get(int(out["trial_id"]), include_returns=True)
    assert rec.returns is not None
    r_ov = rec.returns[-len(candles) :]
    e = block["avg_exposure"]
    assert 0.5 < e < 1.0
    assert block["cdar"]["buy_hold"] == pytest.approx(cdar(bh, 0.95), abs=1e-12)
    assert block["cdar"]["exposure_matched"] == pytest.approx(cdar(e * bh, 0.95), abs=1e-12)
    assert block["cdar"]["overlay"] == pytest.approx(cdar(r_ov, 0.95), abs=1e-12)
    ratio = block["cdar"]["overlay"] / block["cdar"]["buy_hold"]
    assert block["cdar_ratio_vs_buy_hold"] == pytest.approx(ratio, rel=1e-12)
    scale = math.sqrt(bars_per_year("4h"))
    assert block["sharpe"]["buy_hold"] == pytest.approx(sharpe_per_bar(bh) * scale, rel=1e-12)
    # the exposure-matched benchmark is shallower than buy-and-hold, deeper than e x D
    d = block["max_drawdown"]["buy_hold"]
    assert e * d < block["max_drawdown"]["exposure_matched"] < d
    assert any("rebalanced every bar at zero cost" in n for n in block["notes"])


def test_bootstrap_is_seeded_and_uses_the_frozen_block(led: Ledger, tmp_path: Path) -> None:
    cfg, run_id, out = _overlay_evidence(led, tmp_path, MA_OVERLAY)
    block = out["overlay"]
    assert block["bootstrap"] == {
        "n_samples": OVERLAY_SAMPLES,
        "mean_block": bootstrap_block_bars(BLOCK_DAYS, "4h"),
        "mean_block_days": float(BLOCK_DAYS),
        "seed": 7,
        "level": 0.95,
    }
    crit = overlay_criteria(tmp_path)
    kwargs = {"cfg": cfg, "ledger": led, "run_id": run_id, "loader": loader_for(CRASH_CANDLES)}
    again = overlay_block(MA_OVERLAY, criteria=crit, **kwargs)
    assert json.dumps(again, sort_keys=True) == json.dumps(block, sort_keys=True)
    other = overlay_block(MA_OVERLAY, criteria=crit, seed=8, **kwargs)
    assert other["bootstrap"]["seed"] == 8
    assert other["by_cost"][0]["sharpe_lower"] != block["by_cost"][0]["sharpe_lower"]


def test_sharpe_bootstrap_matches_the_stats_module_and_a_second_implementation(
    led: Ledger, tmp_path: Path
) -> None:
    """The 1x Sharpe gap equals lab.stats.paired.sharpe_difference_test on the same series, and
    the non-inferiority p equals a direct re-implementation of the resampling (one index
    matrix from stationary_bootstrap_indices with the same seeded generator)."""
    cfg, _, out = _overlay_evidence(led, tmp_path, MA_OVERLAY)
    block = out["overlay"]
    candles = _scored(cfg, block)
    history = design_view(CRASH_CANDLES, cfg.splits)
    bh = buy_and_hold(
        candles, costs=cfg.costs, init_cash=cfg.init_cash, cost_history=history
    ).returns.to_numpy()
    rec = led.get(int(out["trial_id"]), include_returns=True)
    assert rec.returns is not None
    ov = rec.returns[-len(candles) :]
    boot = block["bootstrap"]
    ref = sharpe_difference_test(
        ov,
        bh,
        interval="4h",
        n_samples=boot["n_samples"],
        mean_block=boot["mean_block"],
        seed=boot["seed"],
        level=boot["level"],
    )
    row = block["by_cost"][0]
    assert row["multiple"] == 1.0
    assert row["sharpe_point"] == pytest.approx(ref.point, rel=1e-12)
    assert row["sharpe_lower"] == pytest.approx(ref.lower, rel=1e-12)
    idx = stationary_bootstrap_indices(
        len(ov),
        mean_block=boot["mean_block"],
        n_samples=boot["n_samples"],
        rng=np.random.default_rng(boot["seed"]),
    )
    scale = math.sqrt(bars_per_year("4h"))
    d = np.array([(sharpe_per_bar(ov[i]) - sharpe_per_bar(bh[i])) * scale for i in idx])
    d = d[np.isfinite(d)]
    expected = (1 + np.count_nonzero(d <= -0.2)) / (1 + d.size)
    assert row["sharpe_ni_p_value"] == pytest.approx(expected, rel=1e-9)
    # bias = mean(d*) - point, for the Sharpe gap and the CDaR gap (same index matrix)
    assert row["sharpe_bias"] == pytest.approx(float(np.mean(d)) - ref.point, abs=1e-9)
    e = block["avg_exposure"]
    dc = np.array([cdar(ov[i], 0.95) - cdar(e * bh[i], 0.95) for i in idx])
    dc = dc[np.isfinite(dc)]
    point = cdar(ov, 0.95) - cdar(e * bh, 0.95)
    assert row["cdar_point"] == pytest.approx(point, abs=1e-12)
    assert row["cdar_bias"] == pytest.approx(float(np.mean(dc)) - point, abs=1e-9)
    assert row["cdar_p_value"] == pytest.approx(
        (1 + np.count_nonzero(dc >= 0.0)) / (1 + dc.size), rel=1e-12
    )
    assert all("cdar_bias" in r and "sharpe_bias" in r for r in block["by_cost"])


def test_features_are_computed_on_the_full_history_then_sliced(tmp_path: Path) -> None:
    """vol_rank is an expanding rank: recomputed from the window start it would differ. The
    block's exposure follows the full-history signals (exposure at bar k = entry signal at
    k - 1), and is not what a window-start recomputation gives."""
    base = overlay_cfg(tmp_path)
    cfg = replace(
        base, splits=replace(base.splits, design_start=pd.Timestamp("2018-11-01", tz="UTC"))
    )
    plan = {
        "kind": "overlay",
        "name": "vol_hot",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {"vr": {"vol_rank": {"period": 30}}},
        "flat_when": "vr > 0.8",
    }
    with Ledger(tmp_path / "l.db") as led:
        run_id = start_overlay_run(led, tmp_path, cfg)
        out = backtest_evidence(
            plan, run_id=run_id, cfg=cfg, ledger=led, loader=loader_for(CRASH_CANDLES)
        )
    block = out["overlay"]
    history = design_view(CRASH_CANDLES, cfg.splits)
    start = pd.Timestamp(block["window"]["start"], unit="s", tz="UTC")
    assert start == cfg.splits.design_start  # the warm-up fits before the design window
    window = history.index >= start
    full = compile_plan(parse_plan(plan), history).entries[window].to_numpy()
    expected = float(np.mean(full[:-1]))
    assert block["avg_exposure"] == pytest.approx(expected, abs=1e-12)
    recomputed = compile_plan(parse_plan(plan), history.loc[window]).entries.to_numpy()
    assert not np.array_equal(full, recomputed)
    assert float(np.mean(recomputed[:-1])) != pytest.approx(expected, abs=1e-6)


def test_wf_fixed_stitches_the_lab_fold_splitter_test_folds(led: Ledger, tmp_path: Path) -> None:
    cfg, _, out = _overlay_evidence(led, tmp_path, MA_OVERLAY)
    block = out["overlay"]
    wf = block["wf_fixed"]
    start = pd.Timestamp(block["window"]["start"], unit="s", tz="UTC")
    end = pd.Timestamp(block["window"]["end"], unit="s", tz="UTC")
    s = cfg.splits
    folds = walk_forward_folds(
        Window(start=start, end=end, kind="design"),
        train_months=s.wf_train_months,
        test_months=s.wf_test_months,
        step_months=s.wf_step_months,
        anchored=s.wf_anchored,
    )
    assert wf["folds"] == [
        {"start": int(f.test.start.timestamp()), "end": int(f.test.end.timestamp())} for f in folds
    ]
    history = design_view(CRASH_CANDLES, cfg.splits)
    assert wf["bars"] == sum(len(slice_window(history, f.test)) for f in folds)
    assert [r["multiple"] for r in wf["by_cost"]] == list(OVERLAY_COST_MULTIPLES)
    assert {"avg_exposure", "cdar", "cdar_ratio_vs_buy_hold", "sharpe", "max_drawdown"} <= set(wf)


def test_block_episodes_list_buy_and_hold_drawdowns_deeper_than_the_depth(
    led: Ledger, tmp_path: Path
) -> None:
    _, _, out = _overlay_evidence(led, tmp_path, MA_OVERLAY)
    ep = out["overlay"]["episodes"]
    assert ep["depth_min"] == 0.30
    assert ep["buy_hold_count"] == len(ep["buy_hold"]) >= 3
    assert all(e["depth"] > 0.30 and e["peak"] < e["trough"] for e in ep["buy_hold"])
    assert ep["overlay_count"] < ep["buy_hold_count"]
    for e in ep["buy_hold"]:
        assert set(e) == {
            "peak",
            "trough",
            "recovery",
            "depth",
            "overlay_max_dd",
            "matched_max_dd",
            "improved",
        }
        assert e["improved"] is (e["overlay_max_dd"] < e["matched_max_dd"])
    assert ep["improved_count"] == sum(e["improved"] for e in ep["buy_hold"])
    base = out["overlay"]["by_cost"][0]
    assert base["multiple"] == 1.0
    assert base["episodes_qualifying"] == ep["buy_hold_count"]
    assert base["episodes_improved"] == ep["improved_count"]


def test_block_episode_windows_match_an_independent_recomputation(
    led: Ledger, tmp_path: Path
) -> None:
    """Each episode's overlay / matched max drawdown, recomputed here from the ledgered trial
    and a buy-and-hold rerun: equity rebased to 1 at the peak bar, bars peak+1..recovery."""
    cfg, _, out = _overlay_evidence(led, tmp_path, MA_OVERLAY)
    block = out["overlay"]
    candles = _scored(cfg, block)
    history = design_view(CRASH_CANDLES, cfg.splits)
    bh = buy_and_hold(
        candles, costs=cfg.costs, init_cash=cfg.init_cash, cost_history=history
    ).returns.to_numpy()
    rec = led.get(int(out["trial_id"]), include_returns=True)
    assert rec.returns is not None
    ov = rec.returns[-len(candles) :]
    matched = block["avg_exposure"] * bh
    secs = [int(t.timestamp()) for t in candles.index]
    for e in block["episodes"]["buy_hold"]:
        peak = secs.index(e["peak"])
        end = secs.index(e["recovery"]) + 1 if e["recovery"] is not None else len(secs)

        def mdd(r: np.ndarray, lo: int = peak, hi: int = end) -> float:
            equity = np.cumprod(1.0 + r[lo + 1 : hi])
            return float(np.max(1.0 - equity / np.maximum.accumulate(np.maximum(equity, 1.0))))

        assert e["overlay_max_dd"] == pytest.approx(mdd(ov), abs=1e-12)
        assert e["matched_max_dd"] == pytest.approx(mdd(matched), abs=1e-12)


# --------------------------------------------------------------------------- the rows


def test_report_on_a_working_overlay_passes_every_pre_holdout_row_but_crosscheck(
    led: Ledger, tmp_path: Path
) -> None:
    cfg, run_id, out = _overlay_evidence(led, tmp_path, MA_OVERLAY)
    rep = evaluate_overlay_plan(
        led, run_id, out["plan_hash"], cfg, loader=loader_for(CRASH_CANDLES)
    )
    assert [r["criterion"] for r in rep["rows"]] == list(OVERLAY_ROWS)
    status = {r["criterion"]: r["status"] for r in rep["rows"]}
    assert status.pop("require_holdout_pass") == "missing"
    assert status.pop("require_crosscheck_match") == "missing"
    assert set(status.values()) == {"pass"}, rep["rows"]
    assert rep["pre_holdout_passed"] is False and rep["passed"] is False
    assert rep["question"] == "risk_overlay" and rep["n_overlay"] == 1
    assert rep["alpha_adjusted"] == 0.05 and rep["not_testable"] == []
    pbo = next(r for r in rep["rows"] if r["criterion"] == "pbo_max")
    assert pbo["status"] == "pass" and "nothing swept: overlays are untuned by rule" in pbo["note"]
    ep = next(r for r in rep["rows"] if r["criterion"] == "min_drawdown_episodes")
    assert isinstance(ep["value"], int) and ep["value"] >= 3
    assert rep["overlay"] == out["overlay"]


def test_an_overlay_report_never_says_edge(led: Ledger, tmp_path: Path) -> None:
    cfg, run_id, out = _overlay_evidence(led, tmp_path, MA_OVERLAY)
    rep = evaluate_overlay_plan(
        led, run_id, out["plan_hash"], cfg, loader=loader_for(CRASH_CANDLES)
    )
    text = json.dumps(rep)
    assert edge_word(text) == []
    empty = evaluate_overlay_plan(led, run_id, "0" * 64, cfg)
    assert edge_word(json.dumps(empty)) == []


def test_edge_word_check_catches_the_word_but_not_ledger() -> None:
    assert edge_word("the ledger holds a hedge") == []
    assert edge_word("no Edge found; edges") == ["Edge", "edges"]


def test_too_few_drawdown_episodes_make_the_drawdown_rows_not_testable(
    led: Ledger, tmp_path: Path
) -> None:
    """trend_candles() has no 30% buy-and-hold drawdown: not testable, never a pass."""
    candles = trend_candles()
    cfg, run_id, out = _overlay_evidence(led, tmp_path, MA_OVERLAY, candles)
    assert out["overlay"]["episodes"]["buy_hold_count"] < 3
    rep = evaluate_overlay_plan(led, run_id, out["plan_hash"], cfg, loader=loader_for(candles))
    status = {r["criterion"]: r["status"] for r in rep["rows"]}
    for name in (
        "min_drawdown_episodes",
        "cdar_vs_buy_hold_max",
        "beat_exposure_matched",
        "break_even_cost_multiple_min",
    ):
        assert status[name] == "not_testable", (name, rep["rows"])
    # on the folds the drawdown rows are not_testable too; a fold row that fails outright
    # (here the Sharpe gap) makes the row a fail, which is never a pass either
    wf = next(r for r in rep["rows"] if r["criterion"] == "wf_rows_hold")
    assert wf["status"] in {"not_testable", "fail"}
    assert "cdar_vs_buy_hold_max not_testable" in wf["note"]
    assert "beat_exposure_matched not_testable" in wf["note"]
    assert set(rep["not_testable"]) >= {"min_drawdown_episodes", "cdar_vs_buy_hold_max"}
    assert status["sharpe_non_inferiority"] in {"pass", "fail"}  # always testable
    assert rep["pre_holdout_passed"] is False


def test_no_backtest_means_every_measured_row_is_missing(led: Ledger, tmp_path: Path) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    rep = evaluate_overlay_plan(led, run_id, "0" * 64, cfg)
    status = {r["criterion"]: r["status"] for r in rep["rows"]}
    assert status["pbo_max"] == "pass"
    assert {k for k, v in status.items() if v == "missing"} == set(OVERLAY_ROWS) - {"pbo_max"}
    assert rep["overlay"] is None and rep["pre_holdout_passed"] is False


def test_evaluate_overlay_plan_refuses_an_edge_run(led: Ledger, tmp_path: Path) -> None:
    cfg = overlay_cfg(tmp_path)
    edge = start_edge_run(led, tmp_path, cfg)
    with pytest.raises(ValueError, match="not a risk-overlay run"):
        evaluate_overlay_plan(led, edge, "0" * 64, cfg)


def test_not_testable_is_a_criterion_status() -> None:
    assert "not_testable" in CriterionStatus.__args__  # type: ignore[attr-defined]


# ---- forged evidence: hand-set numbers, so each rule is checked exactly


def _by_cost(**over: Any) -> list[dict[str, Any]]:
    rows = []
    for m in OVERLAY_COST_MULTIPLES:
        row = {
            "multiple": m,
            "cdar_ratio": 0.5,
            "avg_exposure": 0.8,
            "cdar_point": -0.1,
            "cdar_p_value": 0.01,
            "sharpe_point": 0.1,
            "sharpe_lower": -0.1,
            "sharpe_ni_p_value": 0.01,
            "cdar_bias": 0.01,
            "sharpe_bias": 0.02,
            "episodes_qualifying": 4,
            "episodes_improved": 3,
        }
        row.update(over.get(str(m), {}))
        rows.append(row)
    return rows


def _block(
    episodes: int = 4,
    by_cost: list[dict[str, Any]] | None = None,
    wf_episodes: int | None = None,
    **top: Any,
) -> dict[str, Any]:
    core = {
        "avg_exposure": 0.8,
        "cdar": {"overlay": 0.1, "buy_hold": 0.2, "exposure_matched": 0.16},
        "cdar_ratio_vs_buy_hold": 0.5,
        "sharpe": {"overlay": 1.0, "buy_hold": 0.9},
        "max_drawdown": {"overlay": 0.2, "buy_hold": 0.5, "exposure_matched": 0.4},
        "by_cost": by_cost if by_cost is not None else _by_cost(),
    }
    block = {
        **core,
        "episodes": {
            "depth_min": 0.3,
            "buy_hold_count": episodes,
            "overlay_count": 0,
            "buy_hold": [],
        },
        "wf_fixed": {
            **core,
            "bars": 500,
            "folds": [{"start": 0, "end": 1}],
            "episodes": {
                "depth_min": 0.3,
                "buy_hold_count": episodes if wf_episodes is None else wf_episodes,
                "overlay_count": 0,
                "improved_count": 3,
            },
        },
        "bootstrap": {"n_samples": OVERLAY_SAMPLES},
    }
    block.update(top)
    return block


def _forge(
    led: Ledger, run_id: str, block: dict[str, Any], plan: dict[str, Any] = MA_OVERLAY
) -> str:
    h = plan_hash(parse_plan(plan))
    payload = {"plan": plan, "overlay": block, "logic_hash": logic_hash(plan)}
    led.record_evidence(run_id, h, "backtest", json.dumps(payload))
    return h


def _record_trial(
    led: Ledger, run_id: str, plan: dict[str, Any], *, status: str = "ok", source: str = "api"
) -> None:
    from lab.types import TrialRecord

    parsed = parse_plan(plan)
    led.record(
        TrialRecord(
            run_id=run_id,
            source=source,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            plan_hash=plan_hash(parsed),
            plan_json=json.dumps(plan, sort_keys=True),
            params_json="{}",
            symbol=parsed.symbol,
            interval=parsed.interval,
            window_kind="design",
            window_start=pd.Timestamp("2018-07-01", tz="UTC"),
            window_end=pd.Timestamp(HOLDOUT_START, tz="UTC"),
            metrics_json="{}",
            sharpe_per_bar=0.01 if status == "ok" else None,
            n_trades=1,
            error=None if status == "ok" else "boom",
            costs_json='{"fee_rate": 0.001}',
        )
    )


def _status(rep: Any) -> dict[str, CriterionStatus]:
    return {r["criterion"]: r["status"] for r in rep["rows"]}


def test_forged_block_with_every_number_inside_the_bar_passes(led: Ledger, tmp_path: Path) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg, require_crosscheck_match=False)
    h = _forge(led, run_id, _block())
    rep = evaluate_overlay_plan(led, run_id, h, cfg)
    status = _status(rep)
    assert status.pop("require_holdout_pass") == "missing"
    assert set(status.values()) == {"pass"}, rep["rows"]
    assert rep["pre_holdout_passed"] is True
    be = next(r for r in rep["rows"] if r["criterion"] == "break_even_cost_multiple_min")
    assert be["value"] == 4.0


def test_bonferroni_divides_alpha_by_n_overlay_across_runs(led: Ledger, tmp_path: Path) -> None:
    """p = 0.03 beats alpha / 1 = 0.05 but not alpha / 2 = 0.025 once a second overlay logic
    was tried in ANOTHER overlay run; the Sharpe non-inferiority p is divided too."""
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    block = _block(by_cost=_by_cost(**{"1.0": {"cdar_p_value": 0.03, "sharpe_ni_p_value": 0.03}}))
    h = _forge(led, run_id, block)
    _record_trial(led, run_id, MA_OVERLAY)
    first = evaluate_overlay_plan(led, run_id, h, cfg)
    assert first["n_overlay"] == 1 and first["alpha_adjusted"] == 0.05
    assert _status(first)["beat_exposure_matched"] == "pass"
    assert _status(first)["sharpe_non_inferiority"] == "pass"
    other = start_overlay_run(led, tmp_path / "x", cfg)
    _record_trial(led, other, RSI_OVERLAY)
    second = evaluate_overlay_plan(led, run_id, h, cfg)
    assert second["n_overlay"] == 2 and second["alpha_adjusted"] == 0.025
    assert _status(second)["beat_exposure_matched"] == "fail"
    assert _status(second)["sharpe_non_inferiority"] == "fail"
    assert "alpha / N_overlay = 0.025" in next(
        r["note"] for r in second["rows"] if r["criterion"] == "beat_exposure_matched"
    )


def test_sharpe_row_needs_the_lower_bound_and_the_non_inferiority_p(
    led: Ledger, tmp_path: Path
) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    low = _forge(led, run_id, _block(by_cost=_by_cost(**{"1.0": {"sharpe_lower": -0.21}})))
    row = next(
        r
        for r in evaluate_overlay_plan(led, run_id, low, cfg)["rows"]
        if r["criterion"] == "sharpe_non_inferiority"
    )
    assert row["status"] == "fail" and row["value"] == -0.21 and row["threshold"] == -0.2
    edge_case = {**MA_OVERLAY, "name": "renamed"}
    h = _forge(led, run_id, _block(by_cost=_by_cost(**{"1.0": {"sharpe_lower": -0.2}})), edge_case)
    row = next(
        r
        for r in evaluate_overlay_plan(led, run_id, h, cfg)["rows"]
        if r["criterion"] == "sharpe_non_inferiority"
    )
    assert row["status"] == "pass"  # the bound is inclusive


def test_beat_exposure_matched_needs_a_negative_point(led: Ledger, tmp_path: Path) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    h = _forge(led, run_id, _block(by_cost=_by_cost(**{"1.0": {"cdar_point": 0.0}})))
    assert _status(evaluate_overlay_plan(led, run_id, h, cfg))["beat_exposure_matched"] == "fail"


def test_break_even_walk_stops_at_the_first_failing_multiple(led: Ledger, tmp_path: Path) -> None:
    """Holds at 1x and 1.5x, the CDaR ratio fails at 2x, holds again at 3x: break-even 1.5."""
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    h = _forge(led, run_id, _block(by_cost=_by_cost(**{"2.0": {"cdar_ratio": 0.76}})))
    rep = evaluate_overlay_plan(led, run_id, h, cfg)
    row = next(r for r in rep["rows"] if r["criterion"] == "break_even_cost_multiple_min")
    assert row["status"] == "fail" and row["value"] == 1.5
    assert "2x fails" in row["note"] and "3x holds" in row["note"]
    base_fail = _forge(
        led, run_id, _block(by_cost=_by_cost(**{"1.0": {"sharpe_ni_p_value": 0.2}})), RSI_OVERLAY
    )
    row = next(
        r
        for r in evaluate_overlay_plan(led, run_id, base_fail, cfg)["rows"]
        if r["criterion"] == "break_even_cost_multiple_min"
    )
    assert row["status"] == "fail" and row["value"] is None


def test_min_avg_exposure_and_ratio_rows(led: Ledger, tmp_path: Path) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    h = _forge(led, run_id, _block(avg_exposure=0.49, cdar_ratio_vs_buy_hold=0.751))
    status = _status(evaluate_overlay_plan(led, run_id, h, cfg))
    assert status["min_avg_exposure"] == "fail"
    assert status["cdar_vs_buy_hold_max"] == "fail"


def test_wf_rows_hold_fails_when_a_fold_row_fails(led: Ledger, tmp_path: Path) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    block = _block()
    block["wf_fixed"] = {**block["wf_fixed"], "avg_exposure": 0.3}
    h = _forge(led, run_id, block)
    rep = evaluate_overlay_plan(led, run_id, h, cfg)
    row = next(r for r in rep["rows"] if r["criterion"] == "wf_rows_hold")
    assert row["status"] == "fail" and "min_avg_exposure fail" in row["note"]
    assert _status(rep)["min_avg_exposure"] == "pass"  # the design window still passes


def test_wf_rows_hold_is_missing_without_folds_and_not_testable_propagates(
    led: Ledger, tmp_path: Path
) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    h = _forge(led, run_id, _block(wf_fixed={"error": "no walk-forward test fold: too short"}))
    row = next(
        r
        for r in evaluate_overlay_plan(led, run_id, h, cfg)["rows"]
        if r["criterion"] == "wf_rows_hold"
    )
    assert row["status"] == "missing" and "too short" in row["note"]
    few = _forge(led, run_id, _block(episodes=2), RSI_OVERLAY)
    status = _status(evaluate_overlay_plan(led, run_id, few, cfg))
    assert status["wf_rows_hold"] == "not_testable"
    assert status["min_drawdown_episodes"] == "not_testable"


def test_rows_not_required_by_the_frozen_file_pass_with_a_note(led: Ledger, tmp_path: Path) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg, beat_exposure_matched=False, wf_rows_hold=False)
    block = _block(by_cost=_by_cost(**{"1.0": {"cdar_point": 0.5, "cdar_p_value": 1.0}}))
    block["wf_fixed"] = {"error": "none"}
    h = _forge(led, run_id, block)
    rep = evaluate_overlay_plan(led, run_id, h, cfg)
    for name in ("beat_exposure_matched", "wf_rows_hold"):
        row = next(r for r in rep["rows"] if r["criterion"] == name)
        assert row["status"] == "pass" and "not required" in row["note"]
    assert _status(rep)["break_even_cost_multiple_min"] == "pass"


# --------------------------------------------------------------------------- N_overlay


def test_n_overlay_counts_logics_across_overlay_runs_and_never_resets(
    led: Ledger, tmp_path: Path
) -> None:
    cfg = overlay_cfg(tmp_path)
    assert overlay_trial_count(led) == 1  # floor
    first = start_overlay_run(led, tmp_path, cfg)
    _record_trial(led, first, MA_OVERLAY)
    renamed = {
        **MA_OVERLAY,
        "name": "again",
        "features": {"x": {"sma": {"period": 200}}},
        "flat_when": "close < x",
    }
    _record_trial(led, first, renamed, source="neighbourhood")  # same logic, other params
    assert overlay_trial_count(led) == 1
    second = start_overlay_run(led, tmp_path / "b", cfg)
    assert overlay_trial_count(led) == 1  # a new run does not reset or add anything
    _record_trial(led, second, RSI_OVERLAY, status="error")  # an error trial was still tried
    assert overlay_trial_count(led) == 2
    _record_trial(led, second, {**RSI_OVERLAY, "flat_when": "r < 30"}, source="benchmark")
    assert overlay_trial_count(led) == 2  # benchmark rows are not selection trials
    edge = start_edge_run(led, tmp_path, cfg)
    _record_trial(led, edge, {k: v for k, v in MA_EDGE_ALIKE.items()})
    assert overlay_trial_count(led) == 2  # edge runs never count


# --------------------------------------------------------------------------- edge N and V[SR]


def test_overlay_trials_count_in_the_edge_n_but_never_in_its_variance_pools(
    led: Ledger, tmp_path: Path
) -> None:
    """Owner decision 2026-10-29, "count in N, not V[SR]": the research-wide N (and the
    effective-N plan set that must match it) gains exactly the overlay run's distinct selection
    plans, while every research-wide Sharpe pool is unchanged."""
    cfg = overlay_cfg(tmp_path)
    edge = start_edge_run(led, tmp_path, cfg)
    for plan in (MA_EDGE_SAME_LOGIC, MA_EDGE_ALIKE):
        _record_trial(led, edge, plan)
    scope = {
        "interval": "4h",
        "window_start": pd.Timestamp("2018-07-01", tz="UTC"),
        "window_end": pd.Timestamp(HOLDOUT_START, tz="UTC"),
        "costs_json": '{"fee_rate": 0.001}',
    }

    def pools() -> tuple[Any, ...]:
        return (
            led.latest_sharpes_per_plan_research(**scope),
            led.selection_sharpe_variance_research(**scope),
            led.research_design_sharpes(costs_json=scope["costs_json"]),
        )

    n_before = led.count_distinct_plans_research(sources=SELECTION_SOURCES)
    v_before = pools()
    assert n_before == 2
    overlay = start_overlay_run(led, tmp_path, cfg)
    _record_trial(led, overlay, MA_OVERLAY)
    _record_trial(led, overlay, MA_OVERLAY)  # a re-run of one plan does not inflate N
    _record_trial(led, overlay, RSI_OVERLAY, status="error")  # an error trial was still tried
    _record_trial(led, overlay, {**RSI_OVERLAY, "name": "nb"}, source="neighbourhood")
    _record_trial(led, overlay, {**RSI_OVERLAY, "name": "adv"}, source="adversarial")
    counted = led.count_distinct_plans(overlay, sources=SELECTION_SOURCES)
    assert counted == 3  # MA, RSI, the neighbour; the adversarial variant is not a selection
    n_after = led.count_distinct_plans_research(sources=SELECTION_SOURCES)
    assert n_after - n_before == counted
    assert pools() == v_before  # V[SR] values are unchanged
    meta, missing = led.plan_return_trials(holdout_start=cfg.splits.holdout_start)
    assert len(meta) + len(missing) == n_after  # the effective-N plan set follows N
    raw = led._conn.execute("SELECT COUNT(DISTINCT plan_hash) FROM trials").fetchone()[0]
    assert raw == 6


# --------------------------------------------------------------------------- one parameter set


SMA50_OVERLAY: dict[str, Any] = {
    **MA_OVERLAY,
    "features": {"ma": {"sma": {"period": 50}}},
}  # the same logic as MA_OVERLAY with another parameter value


def _n_trials(led: Ledger) -> int:
    return int(led._conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0])


def test_a_second_parameter_set_of_an_overlay_logic_is_refused_in_any_overlay_run(
    led: Ledger, tmp_path: Path
) -> None:
    cfg = overlay_cfg(tmp_path)
    load = loader_for(CRASH_CANDLES)
    first = start_overlay_run(led, tmp_path, cfg)
    out = backtest_evidence(MA_OVERLAY, run_id=first, cfg=cfg, ledger=led, loader=load)
    h = out["plan_hash"]
    n_overlay, n_trials = overlay_trial_count(led), _n_trials(led)
    assert overlay_submissions(led) == {logic_hash(MA_OVERLAY): (h, first)}
    msg = (
        "overlays use published parameters, untuned: logic already tried as plan "
        f"{h[:12]} in run {first}"
    )
    renamed = {**MA_OVERLAY, "name": "same_rule_other_name"}
    later = start_overlay_run(led, tmp_path / "later", cfg)
    for run_id in (first, later):
        for plan in (SMA50_OVERLAY, renamed):
            for fn in (backtest_evidence, adversarial_evidence, crosscheck_evidence):
                with pytest.raises(ValueError, match=re.escape(msg)):
                    fn(plan, run_id=run_id, cfg=cfg, ledger=led, loader=load)
    assert _n_trials(led) == n_trials  # nothing was recorded
    assert overlay_trial_count(led) == n_overlay == 1
    # re-submitting the SAME plan hash is allowed, in this run and in a later one
    again = backtest_evidence(MA_OVERLAY, run_id=later, cfg=cfg, ledger=led, loader=load)
    assert again["plan_hash"] == h
    assert overlay_trial_count(led) == 1
    # a different logic is a new submission
    backtest_evidence(RSI_OVERLAY, run_id=later, cfg=cfg, ledger=led, loader=load)
    assert overlay_trial_count(led) == 2


def test_lab_generated_neighbours_are_not_submissions(led: Ledger, tmp_path: Path) -> None:
    """run_backtest's +-20% neighbourhood trials share the logic under other plan hashes;
    they never block the submitted plan, and an edge run's single trial of the twin with the
    SAME parameters does not block it either."""
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    _record_trial(led, run_id, SMA50_OVERLAY, source="neighbourhood")
    _record_trial(led, run_id, SMA50_OVERLAY, source="adversarial")
    edge = start_edge_run(led, tmp_path, cfg)
    _record_trial(led, edge, MA_EDGE_SAME_LOGIC)  # same logic and params, an edge submission
    ma = parse_plan(MA_OVERLAY)
    check_overlay_untuned(led, ma)  # no overlay submission yet: allowed
    _record_trial(led, run_id, MA_OVERLAY)
    check_overlay_untuned(led, ma)  # the same plan: allowed
    with pytest.raises(ValueError, match="one published parameter set only"):
        check_overlay_untuned(led, parse_plan(SMA50_OVERLAY))


# rewrites of MA_OVERLAY's rule that the logic hash does not see (reviewer probe, round 2)
SMA_REWRITES: dict[str, dict[str, Any]] = {
    "swap_operands_p50": {
        **MA_OVERLAY,
        "name": "v1",
        "features": {"ma": {"sma": {"period": 50}}},
        "flat_when": "ma > close",
    },
    "not_ge_p40": {
        **MA_OVERLAY,
        "name": "v2",
        "features": {"ma": {"sma": {"period": 40}}},
        "flat_when": "not (close >= ma)",
    },
    "ratio_p80": {
        **MA_OVERLAY,
        "name": "v4",
        "features": {"ma": {"sma": {"period": 80}}},
        "flat_when": "close / ma < 1",
    },
    "same_rule_on_1d": {**MA_OVERLAY, "name": "v5", "interval": "1d"},
}
EMA_COPY: dict[str, Any] = {
    **MA_OVERLAY,
    "name": "v6",
    "features": {"ma": {"ema": {"period": 60}}},
}


def test_rewrites_of_a_submitted_rule_with_other_parameters_are_refused(
    led: Ledger, tmp_path: Path
) -> None:
    """Same registry features with other parameters (or another interval) as an earlier
    overlay submission, in this or a later overlay run: refused before any trial, naming the
    earlier plan and run. The same features with identical parameters and a different rule
    are allowed."""
    cfg = overlay_cfg(tmp_path)
    first = start_overlay_run(led, tmp_path, cfg)
    _record_trial(led, first, MA_OVERLAY)
    h = plan_hash(parse_plan(MA_OVERLAY))
    later = start_overlay_run(led, tmp_path / "later", cfg)
    _record_trial(led, later, RSI_OVERLAY)
    n_overlay, n_trials = overlay_trial_count(led), _n_trials(led)
    for name, plan in SMA_REWRITES.items():
        with pytest.raises(ValueError) as exc:
            check_overlay_untuned(led, parse_plan(plan))
        msg = str(exc.value)
        assert f"plan {h[:12]} in run {first}" in msg, (name, msg)
        assert "Nothing was run or recorded" in msg
        if name != "same_rule_on_1d":  # the 1d copy has the same logic hash
            assert "the same features (sma) were already submitted with other parameters" in msg
    rsi_other = {**RSI_OVERLAY, "name": "r2", "features": {"r": {"rsi": {"period": 21}}}}
    with pytest.raises(ValueError, match=r"in run " + re.escape(later)):
        check_overlay_untuned(led, parse_plan({**rsi_other, "flat_when": "r >= 80"}))
    # identical parameters, a genuinely different rule on the same published feature
    check_overlay_untuned(
        led, parse_plan({**MA_OVERLAY, "name": "deep", "flat_when": "close < 0.9 * ma"})
    )
    check_overlay_untuned(led, parse_plan(EMA_COPY))  # other features: left to pbo_max
    assert overlay_trial_count(led) == n_overlay and _n_trials(led) == n_trials


def test_param_key_fills_registry_defaults_and_ignores_aliases() -> None:
    explicit = parse_plan(MA_OVERLAY)
    renamed = parse_plan(
        {**MA_OVERLAY, "features": {"x": {"sma": {"period": 60}}}, "flat_when": "close < x"}
    )
    assert param_key(explicit) == param_key(renamed) == ("4h", (("sma", (("period", 60.0),)),))
    rsi_default = parse_plan({**RSI_OVERLAY, "features": {"r": {"rsi": {}}}})
    assert param_key(rsi_default) == param_key(parse_plan(RSI_OVERLAY))  # rsi default is 14


def test_a_logic_tuned_in_an_edge_run_cannot_be_asked_as_an_overlay(
    led: Ledger, tmp_path: Path
) -> None:
    """The signal twin (same logic hash) tried with more than one parameter set outside the
    overlay runs, or with one set that differs from the overlay's: refused, naming the edge run.
    The same-feature rule is NOT applied to edge runs (other logics on sma are fine)."""
    cfg = overlay_cfg(tmp_path)
    edge = start_edge_run(led, tmp_path, cfg)
    twin_35 = {**MA_EDGE_SAME_LOGIC, "features": {"m": {"sma": {"period": 35}}}}
    _record_trial(led, edge, twin_35)
    other_logic = {**MA_EDGE_ALIKE, "features": {"m": {"sma": {"period": 90}}}}
    _record_trial(led, edge, other_logic)  # another logic on sma: never blocks
    sma35 = parse_plan({**MA_OVERLAY, "features": {"ma": {"sma": {"period": 35}}}})
    check_overlay_untuned(led, sma35)  # exactly the one edge parameter set: allowed
    with pytest.raises(ValueError, match="logic already tuned in edge run " + re.escape(edge)):
        check_overlay_untuned(led, parse_plan(MA_OVERLAY))  # one edge set, a different one
    _record_trial(led, edge, {**twin_35, "name": "nb"}, source="neighbourhood")  # same params
    check_overlay_untuned(led, sma35)
    _record_trial(led, edge, MA_EDGE_SAME_LOGIC, source="optuna")  # a second edge set
    with pytest.raises(ValueError, match=r"2 parameter set\(s\) of logic"):
        check_overlay_untuned(led, sma35)


def test_pbo_max_fails_when_alike_overlays_were_submitted(led: Ledger, tmp_path: Path) -> None:
    """An ema copy of the sma rule passes the submission rules (other features), but its
    design-window returns correlate with the sma overlay's, so both plans' pbo_max rows fail
    naming the two hashes; a lone overlay keeps the "nothing swept" pass."""
    cfg = overlay_cfg(tmp_path)
    load = loader_for(CRASH_CANDLES)
    run_id = start_overlay_run(led, tmp_path, cfg)
    ma = backtest_evidence(MA_OVERLAY, run_id=run_id, cfg=cfg, ledger=led, loader=load)
    alone = next(
        r
        for r in evaluate_overlay_plan(led, run_id, ma["plan_hash"], cfg, loader=load)["rows"]
        if r["criterion"] == "pbo_max"
    )
    assert alone["status"] == "pass" and "nothing swept" in alone["note"]
    later = start_overlay_run(led, tmp_path / "later", cfg)
    ema = backtest_evidence(EMA_COPY, run_id=later, cfg=cfg, ledger=led, loader=load)
    for rid, h in ((run_id, ma["plan_hash"]), (later, ema["plan_hash"])):
        row = next(
            r
            for r in evaluate_overlay_plan(led, rid, h, cfg, loader=load)["rows"]
            if r["criterion"] == "pbo_max"
        )
        assert row["status"] == "fail", row
        assert row["note"].startswith("2 alike overlays submitted: ")
        assert ma["plan_hash"][:12] in row["note"] and ema["plan_hash"][:12] in row["note"]
        assert "nothing swept" not in row["note"]


# --------------------------------------------------------------------------- episodes


# Hand-built buy-and-hold path (exact binary fractions): equity 1, .5, 1 | 2, 1.5, .75, 1.5,
# 2.25 | 1.6875, 2.53125 | .6328125, .94921875. Episodes: peak 0 -> trough 1 (50%) -> back at
# bar 2; peak 3 -> trough 5 (62.5%) -> back at bar 7; a 25% dip 7 -> 8 -> 9 (too shallow); peak
# 9 -> trough 10 (75%), unrecovered.
EPISODE_BH = np.array([0.0, -0.5, 1.0, 1.0, -0.25, -0.5, 1.0, 0.5, -0.25, 0.5, -0.75, 0.5])
EPISODE_OV = np.array([0.0, -0.1, 0.2, 0.5, -0.5, 0.0, 0.0, 0.0, 0.0, 0.1, 0.0, 0.0])


def test_episode_comparison_known_values_on_a_hand_built_path() -> None:
    """Matched = 0.5 x buy-and-hold. Episode 1 (bars 1-2): overlay -10%, +20% -> 0.1; matched
    -25%, +50% -> 0.25 (improved). Episode 2 (bars 4-7): overlay -50% -> 0.5; matched -12.5%,
    -25% -> 1 - .875 x .75 = 0.34375 (not improved). Episode 3 (bars 10-11, to the end): overlay
    flat -> 0 although its whole-path drawdown there is 1 - .891 / 1.62 = 0.45 (measured from
    its own equity at the episode peak, not its earlier high); matched -37.5% -> 0.375
    (improved). 2 of 3 improved."""
    matched = exposure_matched_returns(EPISODE_BH, 0.5)
    got = episode_comparison(EPISODE_OV, EPISODE_BH, matched, 0.30)
    assert [e for e, _, _ in got] == [
        DrawdownEpisode(peak=0, trough=1, recovery=2, depth=0.5),
        DrawdownEpisode(peak=3, trough=5, recovery=7, depth=0.625),
        DrawdownEpisode(peak=9, trough=10, recovery=None, depth=0.75),
    ]
    assert [ov for _, ov, _ in got] == pytest.approx([0.1, 0.5, 0.0], abs=1e-15)
    assert [m for _, _, m in got] == pytest.approx([0.25, 0.34375, 0.375], abs=1e-15)
    assert [ov < m for _, ov, m in got] == [True, False, True]
    assert float(drawdown_series(EPISODE_OV)[10]) == pytest.approx(1 - 0.891 / 1.62, abs=1e-12)
    # the 25% dip is not strictly deeper than 25%; at 20% it counts
    assert len(episode_comparison(EPISODE_OV, EPISODE_BH, matched, 0.25)) == 3
    assert len(episode_comparison(EPISODE_OV, EPISODE_BH, matched, 0.20)) == 4
    with pytest.raises(ValueError, match="aligned"):
        episode_comparison(EPISODE_OV[:-1], EPISODE_BH, matched, 0.30)


def _episodes_row(rep: Any) -> Any:
    return next(r for r in rep["rows"] if r["criterion"] == "min_episodes_improved")


def test_min_episodes_improved_on_the_hand_built_path_and_its_strict_share(
    led: Ledger, tmp_path: Path
) -> None:
    """The hand-built path's 2 of 3 passes (needs 2); 2 of 4 fails, 3 of 4 passes (strictly
    above 0.5); too few episodes is not_testable."""
    matched = exposure_matched_returns(EPISODE_BH, 0.5)
    got = episode_comparison(EPISODE_OV, EPISODE_BH, matched, 0.30)
    q, imp = len(got), sum(1 for _, ov, m in got if ov < m)
    assert (q, imp) == (3, 2)
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    cases = [
        (3, 2, "pass", "2 of 3"),
        (4, 2, "fail", "3 of 4"),
        (4, 3, "pass", "3 of 4"),
        (3, 1, "fail", "2 of 3"),
    ]
    for k, (qual, improved, want, need) in enumerate(cases):
        plan = {**MA_OVERLAY, "name": f"case{k}"}
        counts = {"episodes_qualifying": qual, "episodes_improved": improved}
        block = _block(episodes=qual, by_cost=_by_cost(**{"1.0": counts}))
        row = _episodes_row(
            evaluate_overlay_plan(led, run_id, _forge(led, run_id, block, plan), cfg)
        )
        assert row["status"] == want, (qual, improved, row)
        assert row["value"] == pytest.approx(improved / qual)
        assert row["threshold"] == 0.5 and f"({need})" in row["note"]
    few = _forge(led, run_id, _block(episodes=2), {**MA_OVERLAY, "name": "few"})
    assert _episodes_row(evaluate_overlay_plan(led, run_id, few, cfg))["status"] == "not_testable"
    assert (
        OVERLAY_ROWS.index("min_episodes_improved")
        == OVERLAY_ROWS.index("beat_exposure_matched") + 1
    )


def test_break_even_needs_the_episodes_row_at_each_multiple(led: Ledger, tmp_path: Path) -> None:
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    by_cost = _by_cost(**{"2.0": {"episodes_improved": 2}})  # 2 of 4 is not above half
    rep = evaluate_overlay_plan(led, run_id, _forge(led, run_id, _block(by_cost=by_cost)), cfg)
    row = next(r for r in rep["rows"] if r["criterion"] == "break_even_cost_multiple_min")
    assert row["status"] == "fail" and row["value"] == 1.5 and "2x fails" in row["note"]
    assert "episodes-improved" in row["note"]
    assert _status(rep)["min_episodes_improved"] == "pass"  # 1x still holds


def test_underpowered_bootstrap_says_so_and_fails(led: Ledger, tmp_path: Path) -> None:
    """alpha / N_overlay = 0.05 < 1 / (1 + 10): no bootstrap p can reach it, whatever the
    forged p says; the rows fail with the underpowered note (never a pass)."""
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    h = _forge(led, run_id, _block(bootstrap={"n_samples": 10}))
    rep = evaluate_overlay_plan(led, run_id, h, cfg)
    for name in ("beat_exposure_matched", "sharpe_non_inferiority", "break_even_cost_multiple_min"):
        row = next(r for r in rep["rows"] if r["criterion"] == name)
        assert row["status"] == "fail", row
        assert "underpowered: smallest attainable p is 1/(1+B)" in row["note"]
        assert "> alpha/N_overlay = 0.05" in row["note"]
    ok_rep = evaluate_overlay_plan(
        led, run_id, _forge(led, run_id, _block(), {**MA_OVERLAY, "name": "b100"}), cfg
    )
    assert all("underpowered" not in r["note"] for r in ok_rep["rows"])  # 1/101 < 0.05


# --------------------------------------------------------------------------- the fold check


def _wf(rep: Any) -> Any:
    return next(r for r in rep["rows"] if r["criterion"] == "wf_rows_hold")


def test_fold_episode_rows_are_not_testable_with_too_few_fold_episodes(
    led: Ledger, tmp_path: Path
) -> None:
    """Design window testable (4 episodes) but the stitched folds hold 2: the fold's
    episode-based rows are not_testable, so the row is not_testable (never a pass), and the
    note names what was judged; a failing judged fold row still makes it a fail."""
    cfg = overlay_cfg(tmp_path)
    run_id = start_overlay_run(led, tmp_path, cfg)
    rep = evaluate_overlay_plan(led, run_id, _forge(led, run_id, _block(wf_episodes=2)), cfg)
    row = _wf(rep)
    assert row["status"] == "not_testable" and rep["pre_holdout_passed"] is False
    note = row["note"]
    assert note.startswith(
        "sub-window robustness check on stitched fixed-parameter test folds (same design bars, "
        "not out-of-sample)"
    )
    assert "Judged: sharpe_non_inferiority, min_avg_exposure;" in note
    assert (
        "not_testable: cdar_vs_buy_hold_max, beat_exposure_matched, min_episodes_improved, "
        "break_even_cost_multiple_min (not_testable is never a pass)"
    ) in note
    assert "2 buy-and-hold episode(s)" in note
    assert _status(rep)["cdar_vs_buy_hold_max"] == "pass"  # the design window is judged
    block = _block(wf_episodes=2)
    block["wf_fixed"] = {**block["wf_fixed"], "avg_exposure": 0.3}
    plan = {**MA_OVERLAY, "name": "fold_fail"}
    assert (
        _wf(evaluate_overlay_plan(led, run_id, _forge(led, run_id, block, plan), cfg))["status"]
        == "fail"
    )
    plan = {**MA_OVERLAY, "name": "fold_ok"}
    good = evaluate_overlay_plan(led, run_id, _forge(led, run_id, _block(wf_episodes=3), plan), cfg)
    assert _wf(good)["status"] == "pass" and "not_testable: none" in _wf(good)["note"]
