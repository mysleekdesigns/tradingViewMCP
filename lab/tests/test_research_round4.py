"""Phase 8 round-4 fixes: the cross-interval family (C1), rule literals in the walk-forward
lineage (C2), the pooled DSR variance and the PBO sweep floor live in test_research_criteria
(W1, W2), family membership through submitted trials only (W3), non-research runs in the family
and lineage (W4a), the cross-check gating the holdout (W4b, see test_research_family), decode
counting and the family cache (note 1), unknown comparisons failing closed (note 2), and tried
values below a search floor (note 3). Offline: seeded synthetic candles (an hourly market with
39 missing bars and its 4h aggregate) and hand-built ledger trials with known answers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.config import DEFAULT_CONFIG_PATH, LabConfig, load_config
from lab.ledger import Ledger
from lab.plan import parse_plan, plan_hash, plan_to_dict, with_params
from lab.research.criteria import evaluate_plan
from lab.research.evidence import record_report
from lab.research.family import (
    aligned_correlation,
    behaviour_family,
    clear_family_cache,
    design_bar_index,
    family_holdout_conflict,
    tried_literals,
    tried_ranges,
    wf_lineage_check,
    wf_lineage_problems,
)
from lab.research.holdout import HoldoutRefusedError, run_holdout_check
from lab.research.logic import feature_multiset, literal_vector, logic_hash
from lab.research.pipeline import backtest_evidence
from lab.types import TrialRecord
from tests.test_mcp_server import (
    EMA_PLAN,
    LAX,
    RSI_PLAN,
    TREND_PLAN,
    Lab,
    call,
    loader_for,
    ok,
    small_cfg,
)
from tests.test_research_criteria import W_END, W_START, WINDOW, backtest_payload, rows, trial
from tests.test_research_family import NOOP_EXIT, start
from tests.test_research_holdout import UP, seed_pre_holdout


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


# =========================================================================== synthetic market

MISSING_HOURS = 39


def hourly_candles(n: int = 11_500, *, seed: int = 3, missing: int = MISSING_HOURS) -> Any:
    """Seeded hourly random walk with regime-switching drift (trends long enough for slow MA
    crosses), from 2018-07-01, with ``missing`` scattered bars removed (exchange gaps)."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(pd.Timestamp("2018-07-01", tz="UTC"), periods=n, freq="1h", name="time")
    blocks = rng.choice([0.0007, -0.0006], size=n // 500 + 1)
    mu = np.repeat(blocks, 500)[:n]
    close = 6000.0 * np.exp(np.cumsum(rng.normal(mu, 0.004)))
    open_ = np.empty(n)
    open_[0] = 6000.0
    open_[1:] = close[:-1] * np.exp(rng.normal(0.0, 0.0004, n - 1))
    spread = np.abs(rng.normal(0.0, 0.003, n)) * close
    frame = pd.DataFrame(
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
    drop = rng.choice(np.arange(500, n - 500), size=missing, replace=False)
    return frame.drop(frame.index[np.sort(drop)])


def to_4h(hourly: pd.DataFrame) -> pd.DataFrame:
    """The 4h candles of ``hourly`` (buckets aligned to UNIX time, built from the bars present)."""
    agg = hourly.resample("4h", origin="epoch", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    agg = agg.dropna()
    agg["tradable"] = True
    agg.index.name = "time"
    return agg


HOURLY = hourly_candles()
FOUR_H = to_4h(HOURLY)


def interval_loader(
    frames: dict[str, pd.DataFrame] | None = None, calls: list[str] | None = None
) -> Any:
    """A loader that, unlike ``loader_for``, returns the candles of the requested interval."""
    data = frames if frames is not None else {"1h": HOURLY, "4h": FOUR_H}

    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        if calls is not None:
            calls.append(interval)
        if interval not in data:
            raise ValueError(f"no {interval} candles in this test")
        frame = data[interval]
        return frame.loc[frame.index < end]

    return load


def ema_cross(fast: int, slow: int, interval: str = "4h", exit_: str | None = None) -> Any:
    return {
        "name": "ema_cross",
        "symbol": "BTCUSDT",
        "interval": interval,
        "features": {"a": {"ema": {"period": fast}}, "b": {"ema": {"period": slow}}},
        "long_entry": "crosses_above(a, b)",
        "long_exit": exit_ or "crosses_below(a, b)",
        "risk": {"position_size_pct": 100},
    }


FOUR_H_PLAN = ema_cross(29, 150)
HOURLY_NOOP = ema_cross(116, 600, "1h", exit_="crosses_below(a, b) or a < 0")


def _h(plan: Any) -> str:
    return plan_hash(parse_plan(plan))


# =========================================================================== C1: alignment


def test_aligned_correlation_sums_fine_bars_by_timestamp_and_treats_gaps_as_gaps() -> None:
    """Known answer: a 4h series that IS the hourly log returns summed per bucket correlates
    1.0 once aligned by timestamp, even with hourly bars missing; position alignment (4 bars
    per coarse bar) drifts after the first gap and loses it. A missing hour drops its bucket
    and the bucket of the bar after it (whose return spans the gap), never shifts the rest."""
    rng = np.random.default_rng(0)
    hours = np.arange(4_000, dtype=np.int64) * 3600
    logret = rng.normal(0.0, 0.01, hours.size)
    four_h = hours[::4]
    coarse = np.expm1(logret.reshape(-1, 4).sum(axis=1))
    keep = np.ones(hours.size, dtype=bool)
    keep[[401, 1203, 2002]] = False  # gaps mid-bucket and at a bucket end
    fine_ts, fine = hours[keep], np.expm1(logret[keep])
    corr, why = aligned_correlation(fine_ts, fine, 3600, four_h, coarse, 14_400, min_overlap=100)
    assert why is None and corr == pytest.approx(1.0, abs=1e-12)
    # position alignment drifts after the first gap
    m = fine.size // 4
    by_position = np.corrcoef(
        np.log1p(fine[: 4 * m]).reshape(-1, 4).sum(axis=1), np.log1p(coarse[:m])
    )
    assert by_position[0, 1] < 0.5
    # exactly which coarse bars survive: bucket 0 (the first bar has no predecessor), each gap's
    # own bucket (100, 300, 500), and bucket 301, whose first bar (hour 1204) follows the gap at
    # hour 1203 (its return spans two hours); the bars after hours 401 and 2002 are in the same
    # bucket as the gap
    from lab.research.family import _to_grid

    grid, _ = _to_grid(fine_ts, np.log1p(fine), 3600, 14_400)
    lost = {0, 100, 300, 301, 500}
    assert set(grid // 14_400) == set(range(1000)) - lost


def test_aligned_correlation_reports_unknown_instead_of_dropping() -> None:
    ts = np.arange(50, dtype=np.int64) * 3600
    r = np.random.default_rng(1).normal(0, 0.01, 50)
    corr, why = aligned_correlation(ts, r, 3600, ts, r, 3600, min_overlap=100)
    assert corr is None and why is not None and "common" in why
    corr, why = aligned_correlation(ts, r, 3600, ts[:-1], r, 3600, min_overlap=10)
    assert corr is None and why is not None and "length" in why
    flat = np.zeros(50)
    assert aligned_correlation(ts, r, 3600, ts, flat, 3600, min_overlap=10) == (None, None)


def test_design_bar_index_rebuilds_the_runners_bars_for_a_trial(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    run = start(ledger, tmp_path, **LAX)
    load = interval_loader()
    out = backtest_evidence(HOURLY_NOOP, run_id=run, cfg=cfg, ledger=ledger, loader=load)
    rec = ledger.get(int(out["trial_id"]), include_returns=True)
    assert rec.returns is not None
    ts = design_bar_index(
        load,
        cfg,
        "BTCUSDT",
        "1h",
        int(rec.window_start.timestamp()),
        int(rec.window_end.timestamp()),
    )
    assert ts.size == rec.returns.size
    full = np.arange(ts[0], ts[-1] + 1, 3600)
    hourly_s = HOURLY.index.as_unit("s").asi8
    dropped_inside = np.setdiff1d(full, hourly_s).size
    assert 0 < dropped_inside <= MISSING_HOURS
    assert full.size - ts.size == dropped_inside  # every missing hour is a gap, none filled


def _composite_dodge(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> tuple[str, str, Any]:
    """probe_r3b: the 4h EMA 29/150 cross is holdout-checked in run A; run B submits the same
    rule on 1h with x4 periods and a no-op exit clause (another logic hash, another interval,
    no same-logic trial on 1h)."""
    load = interval_loader()
    a = start(ledger, tmp_path, **LAX)
    backtest_evidence(FOUR_H_PLAN, run_id=a, cfg=cfg, ledger=ledger, loader=load)
    ledger.record_holdout_check(a, _h(FOUR_H_PLAN), budget=3, logic_hash=logic_hash(FOUR_H_PLAN))
    b = start(ledger, tmp_path, **LAX)
    backtest_evidence(HOURLY_NOOP, run_id=b, cfg=cfg, ledger=ledger, loader=load)
    return a, b, load


def test_the_same_rule_on_a_finer_interval_with_a_no_op_joins_the_family(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """C1: the 1h no-op variant's returns, summed onto 4h bars by timestamp (39 hourly bars are
    missing), correlate with the checked 4h plan, so the 4h logic is in its family and the
    holdout is refused without spending a check."""
    assert logic_hash(HOURLY_NOOP) != logic_hash(FOUR_H_PLAN)
    _, b, load = _composite_dodge(ledger, tmp_path, cfg)
    fam = behaviour_family(
        ledger, symbol="BTCUSDT", plan_hash=_h(HOURLY_NOOP), cfg=cfg, plan=HOURLY_NOOP, loader=load
    )
    assert logic_hash(FOUR_H_PLAN) in fam.logic_hashes
    assert fam.correlated[logic_hash(FOUR_H_PLAN)] >= cfg.research.family_return_corr
    assert fam.n_unknown == 0
    conflict = family_holdout_conflict(ledger, fam)
    assert conflict is not None and "aligning bars by timestamp" in conflict
    with pytest.raises(HoldoutRefusedError, match="same behaviour family") as info:
        run_holdout_check(HOURLY_NOOP, run_id=b, cfg=cfg, ledger=ledger, loader=load)
    assert "another interval" in str(info.value)
    assert ledger.holdout_checks_used(b) == 0
    # symmetric: from the 4h plan's side the 1h variant is family too
    fam4 = behaviour_family(
        ledger, symbol="BTCUSDT", plan_hash=_h(FOUR_H_PLAN), cfg=cfg, plan=FOUR_H_PLAN, loader=load
    )
    assert logic_hash(HOURLY_NOOP) in fam4.logic_hashes


def test_the_cross_interval_family_pulls_the_walk_forward_minimum(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """C1: the 4h plan's (bad) walk-forward in run A is the 1h variant's WF row in run B."""
    a, b, load = _composite_dodge(ledger, tmp_path, cfg)
    record_report(
        ledger,
        a,
        _h(FOUR_H_PLAN),
        "walk_forward",
        {
            "oos_metrics": {"sharpe": -1.25},
            "plan": FOUR_H_PLAN,
            "logic_hash": logic_hash(FOUR_H_PLAN),
        },
    )
    record_report(
        ledger,
        b,
        _h(HOURLY_NOOP),
        "walk_forward",
        {
            "oos_metrics": {"sharpe": 2.0},
            "plan": HOURLY_NOOP,
            "logic_hash": logic_hash(HOURLY_NOOP),
        },
    )
    row = rows(evaluate_plan(ledger, b, _h(HOURLY_NOOP), cfg, loader=load))["wf_oos_sharpe_min"]
    assert row["value"] == -1.25 and row["status"] == "fail"
    assert "1 correlated logic" in row["note"]


# =========================================================================== note 2: unknown


def _checked_hourly_trial(ledger: Ledger, run: str, plan: Any, cfg: LabConfig) -> None:
    """A hand-built 1h design trial (600 returns, which no candles can line up with) whose
    logic was holdout-checked."""
    trial(ledger, run, np.random.default_rng(5).normal(0, 0.01, 600), plan_dict=plan, cfg=cfg)
    ledger.record_holdout_check(run, _h(plan), budget=3, logic_hash=logic_hash(plan))


def test_an_unalignable_same_feature_candidate_fails_the_holdout_closed(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """Note 2: a checked 1h plan with the same features (two EMAs) whose stored returns do not
    match its candles cannot be compared: it is not silently dropped; the holdout refuses."""
    a = start(ledger, tmp_path, **LAX)
    hourly_noop = {**NOOP_EXIT, "interval": "1h"}
    assert feature_multiset(hourly_noop) == feature_multiset(TREND_PLAN)
    _checked_hourly_trial(ledger, a, hourly_noop, cfg)
    b = start(ledger, tmp_path, **LAX)
    seed_pre_holdout(ledger, b, cfg)
    fam = behaviour_family(
        ledger,
        symbol="BTCUSDT",
        plan_hash=_h(TREND_PLAN),
        cfg=cfg,
        plan=TREND_PLAN,
        loader=loader_for(UP),
    )
    assert logic_hash(hourly_noop) in fam.uncertain
    assert "stored returns" in fam.uncertain[logic_hash(hourly_noop)]
    assert fam.n_unknown >= 1 and "could not be aligned" in fam.note()
    with pytest.raises(HoldoutRefusedError, match="fail closed"):
        run_holdout_check(TREND_PLAN, run_id=b, cfg=cfg, ledger=ledger, loader=loader_for(UP))
    assert ledger.holdout_checks_used(b) == 0


def test_an_unalignable_candidate_with_other_features_does_not_block(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    a = start(ledger, tmp_path, **LAX)
    hourly_rsi = {**RSI_PLAN, "interval": "1h"}
    _checked_hourly_trial(ledger, a, hourly_rsi, cfg)
    b = start(ledger, tmp_path, **LAX)
    seed_pre_holdout(ledger, b, cfg)
    fam = behaviour_family(
        ledger,
        symbol="BTCUSDT",
        plan_hash=_h(TREND_PLAN),
        cfg=cfg,
        plan=TREND_PLAN,
        loader=loader_for(UP),
    )
    assert fam.n_unknown == 1 and fam.uncertain == {}
    assert family_holdout_conflict(ledger, fam) is None
    out = run_holdout_check(TREND_PLAN, run_id=b, cfg=cfg, ledger=ledger, loader=loader_for(UP))
    assert out["checks_used"] == 1


def test_a_missing_interval_makes_the_comparison_unknown(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """No candles for the other interval at all (the loader raises): unknown, not dropped."""
    _, b, _ = _composite_dodge(ledger, tmp_path, cfg)
    only_hourly = interval_loader({"1h": HOURLY})
    clear_family_cache()
    fam = behaviour_family(
        ledger,
        symbol="BTCUSDT",
        plan_hash=_h(HOURLY_NOOP),
        cfg=cfg,
        plan=HOURLY_NOOP,
        loader=only_hourly,
    )
    assert logic_hash(FOUR_H_PLAN) in fam.uncertain
    assert "no BTCUSDT 4h candles" in fam.uncertain[logic_hash(FOUR_H_PLAN)]
    with pytest.raises(HoldoutRefusedError, match="fail closed"):
        run_holdout_check(HOURLY_NOOP, run_id=b, cfg=cfg, ledger=ledger, loader=only_hourly)


# =========================================================================== W3 / W4a: members


def test_only_submitted_trials_admit_a_logic(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """W3: an Optuna trial of another logic with returns identical to the base never pulls
    that logic into the family (a near-always-long sweep corner would merge unrelated ideas);
    a submitted ("api") or "manual" design trial does."""
    run = start(ledger, tmp_path)
    r = np.random.default_rng(2).normal(0.001, 0.01, 600)
    trial(ledger, run, r, plan_dict=TREND_PLAN, cfg=cfg)
    sweep_only = {**TREND_PLAN, "long_exit": "ema_fast < ema_slow or ema_fast / ema_slow < -1"}
    trial(ledger, run, r, plan_dict=sweep_only, source="optuna", cfg=cfg)
    fam = behaviour_family(
        ledger, symbol="BTCUSDT", plan_hash=_h(TREND_PLAN), cfg=cfg, plan=TREND_PLAN
    )
    assert logic_hash(sweep_only) not in fam.logic_hashes
    manual = {**TREND_PLAN, "long_exit": "ema_fast < ema_slow or ema_fast / ema_slow < -2"}
    trial(ledger, run, r, plan_dict=manual, source="manual", cfg=cfg)
    fam = behaviour_family(
        ledger, symbol="BTCUSDT", plan_hash=_h(TREND_PLAN), cfg=cfg, plan=TREND_PLAN
    )
    assert logic_hash(manual) in fam.logic_hashes


def test_the_lab_toml_family_threshold_is_point_eight_with_the_new_guards() -> None:
    research = load_config().research
    assert research.family_return_corr == 0.8
    assert research.family_min_overlap_bars == 250
    assert research.min_variance_plans == 10
    assert research.min_pbo_trials == 16


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("family_min_overlap_bars", "2"),
        ("min_variance_plans", "1"),
        ("min_pbo_trials", "1.5"),
        ("min_pbo_trials", "true"),
    ],
)
def test_the_new_research_guards_are_validated(tmp_path: Path, key: str, bad: str) -> None:
    raw = DEFAULT_CONFIG_PATH.read_text()
    lines = [
        f"{key} = {bad}" if line.split("=")[0].strip() == key else line for line in raw.splitlines()
    ]
    path = tmp_path / "lab.toml"
    path.write_text("\n".join(lines))
    with pytest.raises(ValueError, match=rf"research\.{key}"):
        load_config(path)


def test_web_ui_trials_join_the_family_and_the_lineage(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """W4(a): trials of non-research runs (the Strategy Tester's "ui" run) count: a no-op variant
    tried only in the UI is family, and values tried in the UI bind the walk-forward."""
    run = start(ledger, tmp_path)
    r = np.random.default_rng(3).normal(0.001, 0.01, 600)
    trial(ledger, run, r, plan_dict=TREND_PLAN, cfg=cfg)
    trial(ledger, "ui", r, plan_dict=NOOP_EXIT, cfg=cfg)
    retuned_in_ui = plan_to_dict(with_params(parse_plan(TREND_PLAN), {"ema_fast.period": 7}))
    trial(ledger, "ui", r * 0.5, plan_dict=retuned_in_ui, cfg=cfg)
    fam = behaviour_family(
        ledger, symbol="BTCUSDT", plan_hash=_h(TREND_PLAN), cfg=cfg, plan=TREND_PLAN
    )
    assert logic_hash(NOOP_EXIT) in fam.logic_hashes
    ranges = tried_ranges(ledger, fam, parse_plan(TREND_PLAN))
    assert ranges["ema_fast.period"][0] == 7.0
    assert ledger.count_distinct_plans_research() == 3


# =========================================================================== C2: literals


def rsi(threshold: int, period: int = 14) -> dict[str, Any]:
    return {
        **RSI_PLAN,
        "features": {"r": {"rsi": {"period": period}}},
        "long_entry": f"r < {threshold}",
        "long_exit": "r > 60",
    }


def test_literal_vector_is_signed_and_in_canonical_order() -> None:
    plan = {
        **TREND_PLAN,
        "long_entry": "ema_fast > ema_slow and prev(close, 2) / close > -1.5",
        "long_exit": "ema_fast < ema_slow * 0.98",
    }
    assert literal_vector(plan) == (2.0, -1.5, 0.98)
    assert literal_vector(TREND_PLAN) == ()
    assert logic_hash(rsi(20)) == logic_hash(rsi(45)) and literal_vector(rsi(20)) != literal_vector(
        rsi(45)
    )


def test_hand_tuned_thresholds_refuse_the_walk_forward(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    """C2 (probe_r3 c): rsi < 20..45 hand-tuned with run_backtest leaves the tunable ranges
    untouched; the walk-forward over r.period must still be refused."""
    run = start(ledger, tmp_path)
    r = np.random.default_rng(4).normal(0.0, 0.01, 600)
    trial(ledger, run, r, plan_dict=rsi(30), cfg=cfg)
    plan = parse_plan(rsi(30))
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=_h(rsi(30)), cfg=cfg, plan=plan)
    lits = tried_literals(ledger, fam, plan)
    assert lits == {logic_hash(plan): {(30.0, 60.0)}}
    assert (
        wf_lineage_problems(plan, {"r.period": (8, 20)}, tried_ranges(ledger, fam, plan), lits)
        == []
    )
    for th in (20, 25, 35, 40, 45):
        trial(ledger, run, r + th * 1e-6, plan_dict=rsi(th), cfg=cfg)
    fam = behaviour_family(ledger, symbol="BTCUSDT", plan_hash=_h(rsi(30)), cfg=cfg, plan=plan)
    lits = tried_literals(ledger, fam, plan)
    problems = wf_lineage_problems(
        plan, {"r.period": (8, 20)}, tried_ranges(ledger, fam, plan), lits
    )
    assert len(problems) == 1
    assert problems[0].startswith("thresholds were hand-tuned across plans (6 literal vectors")
    assert "(20, 60)" in problems[0] and "(45, 60)" in problems[0]
    assert "a walk-forward cannot search rule literals" in problems[0]
    assert "make the threshold a fixed hypothesis and test a new logic" in problems[0]


def test_a_single_literal_vector_per_family_logic_is_allowed() -> None:
    """A correlated no-op variant has its own (longer) literal vector; one vector per logic is
    not hand-tuning. Two vectors on a correlated logic is."""
    plan = parse_plan(rsi(30))
    noop = {**rsi(30), "long_exit": "r > 60 or r < -1"}
    own, other = logic_hash(plan), logic_hash(noop)
    lits = {own: {(30.0, 60.0)}, other: {(30.0, 60.0, -1.0)}}
    assert wf_lineage_problems(plan, {"r.period": (8, 20)}, {}, lits) == []
    lits[other].add((25.0, 60.0, -1.0))
    assert "hand-tuned" in wf_lineage_problems(plan, {"r.period": (8, 20)}, {}, lits)[0]


def test_the_server_refuses_a_walk_forward_after_hand_tuned_thresholds(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    run_id = lab.start()
    for th in (30, 25):
        ok(lab.server, "run_backtest", {"run_id": run_id, "plan": rsi(th)})
    res = call(
        lab.server,
        "walk_forward",
        {
            "run_id": run_id,
            "plan": rsi(30),
            "space": {"r.period": {"low": 8, "high": 20}},
            "n_trials_per_fold": 1,
        },
    )
    assert res.is_error and "thresholds were hand-tuned across plans" in res.content[0].text


def test_the_neighbourhood_note_says_literals_are_not_perturbed(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig
) -> None:
    from lab.plan import tunable_params
    from lab.runner import neighbourhood_overrides

    run = start(ledger, tmp_path)
    plan = parse_plan(rsi(30))
    record_report(
        ledger,
        run,
        _h(rsi(30)),
        "backtest",
        backtest_payload(plan=plan_to_dict(plan), plan_hash=_h(rsi(30))),
    )
    rng = np.random.default_rng(5)
    for ov in neighbourhood_overrides(tunable_params(plan), cfg.stats.neighbourhood_steps, 1000):
        trial(
            ledger,
            run,
            rng.normal(0, 0.01, 50),
            plan_dict=plan_to_dict(with_params(plan, ov)),
            source="neighbourhood",
            cfg=cfg,
            metrics={"total_return": 0.1},
        )
    row = rows(evaluate_plan(ledger, run, _h(rsi(30)), cfg))["neighbourhood_pct"]
    assert row["status"] == "pass"
    assert "rule literals (thresholds, lags) are NOT perturbed: 30, 60" in row["note"]


# =========================================================================== note 3


def test_a_tried_stop_below_the_search_floor_clamps_the_required_coverage() -> None:
    plan = parse_plan({**TREND_PLAN, "risk": {"position_size_pct": 100, "stop_loss_pct": 2.0}})
    ranges = {"risk.stop_loss_pct": (0.005, 3.0, 4)}
    problems, notes = wf_lineage_check(plan, {"risk.stop_loss_pct": (0.01, 3.0)}, ranges)
    assert problems == []
    assert len(notes) == 1 and "clamped to [0.01, 3]" in notes[0]
    problems, _ = wf_lineage_check(plan, {"risk.stop_loss_pct": (0.5, 3.0)}, ranges)
    assert problems and "does not cover [0.01, 3]" in problems[0]


# =========================================================================== note 1: speed


def test_rows_of_family_logics_are_never_decoded_and_families_are_cached(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3000 submitted trials of the plan's own logic (re-tunes, here and in the UI) are skipped
    before decoding; only the distinct plans of other logics are decoded, once each; a second
    call with no new trial is served from the cache."""
    run = start(ledger, tmp_path)
    rng = np.random.default_rng(6)
    base = trial(ledger, run, rng.normal(0.001, 0.01, 60), plan_dict=TREND_PLAN, cfg=cfg)
    rec = ledger.get(base)
    own_json = json.dumps(plan_to_dict(parse_plan(TREND_PLAN)), sort_keys=True)
    for i in range(3000):
        ledger.record(
            TrialRecord(
                run_id=run if i % 2 else "ui",
                source="api",
                status="ok",
                plan_hash=f"{i:064x}",
                plan_json=own_json,
                params_json="{}",
                symbol="BTCUSDT",
                interval="4h",
                window_kind="design",
                window_start=W_START,
                window_end=W_END,
                metrics_json="{}",
                sharpe_per_bar=0.0,
                n_trades=1,
                costs_json=rec.costs_json,
                returns=rng.normal(0, 0.01, 60),
            )
        )
    others = [
        {**TREND_PLAN, "long_exit": f"ema_fast < ema_slow or ema_fast / ema_slow < {-k}"}
        for k in (1, 2)
    ]
    for p in others:
        trial(ledger, run, rng.normal(0, 0.01, 60), plan_dict=p, cfg=cfg)
        trial(ledger, run, rng.normal(0, 0.01, 60), plan_dict=p, cfg=cfg)  # re-run: one decode
    decoded: list[int] = []
    real = Ledger.trial_returns

    def counting(self: Ledger, trial_id: int) -> Any:
        decoded.append(trial_id)
        return real(self, trial_id)

    monkeypatch.setattr(Ledger, "trial_returns", counting)
    first = behaviour_family(
        ledger, symbol="BTCUSDT", plan_hash=_h(TREND_PLAN), cfg=cfg, plan=TREND_PLAN
    )
    assert len(decoded) == 2  # the newest trial of each other-logic plan
    again = behaviour_family(
        ledger, symbol="BTCUSDT", plan_hash=_h(TREND_PLAN), cfg=cfg, plan=TREND_PLAN
    )
    assert again is first and len(decoded) == 2
    trial(ledger, run, rng.normal(0, 0.01, 60), plan_dict=others[0], cfg=cfg)  # new trial
    behaviour_family(ledger, symbol="BTCUSDT", plan_hash=_h(TREND_PLAN), cfg=cfg, plan=TREND_PLAN)
    assert len(decoded) == 4  # cache invalidated by the new trial id


def test_window_constant_is_the_criteria_tests_window() -> None:
    assert WINDOW["start"] == int(W_START.timestamp()) and WINDOW["end"] == int(W_END.timestamp())
    assert EMA_PLAN["interval"] == "4h"
