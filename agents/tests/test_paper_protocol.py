"""Pre-registered paper-trade protocol: arithmetic on constructed inputs (Poisson and design-block
bands, flat-start exposure, 365-day annualisation, MinTRL straight from lab.stats, false-fail
odds), every verdict branch (pass / fail / continue / inconclusive), data completeness, the
timing match against the lab replay, the ledger link and the claim, the observation side
(shortfall vs the matched lab fill, exposure, per-bar returns), the design backtest never
reading the locked split, and the prepare / compare CLI (exclusive registration before launch,
duplicate refusal, sha256 check, append-only and write-once verdicts, holdout guard not
tripped). Regression scenarios from the adversarial review (probe_gap / probe_gap_rand /
probe_flat) are reproduced offline on seeded synthetic candles."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
import threading
import time
import types
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from scipy import stats as sp_stats

import research.paper.protocol as proto_mod
from lab.config import LabConfig, load_config
from lab.engine import run_signals
from lab.ledger import Ledger
from lab.plan import compile_plan, parse_plan, plan_hash
from lab.research.logic import logic_hash
from lab.stats.metrics import sharpe_per_bar
from lab.stats.sharpe import min_track_record_length, min_track_record_length_from_moments
from lab.types import CostModel, Trade, TrialRecord, bars_per_year
from research.journal import HOLDOUT_WORD, guard_holdout, parse_frontmatter
from research.paper.compare import (
    ForwardTrade,
    fill_shortfalls,
    match_fill_bars,
    modelled_slippage,
    observe_paper,
    paper_bar_returns,
    replay_trades,
)
from research.paper.freqtrade import LaunchResult
from research.paper.protocol import (
    VERDICTS_HEADING,
    DesignStats,
    Fill,
    FinalVerdictRecordedError,
    LedgerLink,
    ObservedPaper,
    PaperProtocol,
    ProtocolChangedError,
    ProtocolConfig,
    append_verdict,
    build_protocol,
    check_not_registered_elsewhere,
    complete_registration,
    design_blocks,
    design_stats,
    evaluate,
    gross_fill_windows,
    journal_path,
    ledger_link,
    load_protocol_config,
    load_registered_protocol,
    median_bounds,
    min_testable_sharpe,
    poisson_band,
    predictive_band,
    protocol_from_dict,
    protocol_sha256,
    protocol_to_dict,
    protocol_to_json,
    read_ledger_link,
    recorded_final_verdict,
    register_protocol,
    render_journal_entry,
    resolve_claim,
)

DAY = pd.Timedelta(days=1)
HOUR = pd.Timedelta(hours=1)
STEP4H = pd.Timedelta(hours=4)
COSTS = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.0, slippage_cap=0.01)
CREATED = datetime(2026, 11, 2, 10, 30, 7, tzinfo=UTC)
LINK = LedgerLink(
    run_id="r-20261101-000000-abcdef",
    trial_id=17,
    window_start=int(pd.Timestamp("2024-01-01", tz="UTC").timestamp()),
    window_end=int(pd.Timestamp("2025-01-01", tz="UTC").timestamp()),
    wf_oos_sharpes=(0.4,),
    wf_evidence_ids=(3,),
    wf_run_ids=("r-20261101-000000-abcdef",),
)

PLAN: dict[str, Any] = {
    "name": "proto",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"fast": {"ema": {"period": 8}}, "slow": {"ema": {"period": 30}}},
    "long_entry": "crosses_above(fast, slow)",
    "long_exit": "crosses_below(fast, slow)",
    "risk": {"position_size_pct": 100},
}


LAB_CFG = (
    load_config()
)  # family thresholds for ledger_link (same-window comparisons need no candles)


def config(**overrides: Any) -> ProtocolConfig:
    base = load_protocol_config()
    return replace(base, **overrides)


def synthetic_design(
    *, mean: float, sd: float = 0.01, days: int = 365, seed: int = 3, interval: str = "4h"
) -> DesignStats:
    """Constructed design stats: known returns, position on in runs of 10 bars out of 30."""
    per_day = {"4h": 6, "1h": 24, "1d": 1}[interval]
    n = days * per_day
    index = pd.date_range("2024-01-01", periods=n, freq=f"{24 // per_day}h", tz="UTC")
    rng = np.random.default_rng(seed)
    returns = rng.normal(mean, sd, n)
    position = np.asarray([1.0 if (i // 10) % 3 == 0 else 0.0 for i in range(n)])
    starts = [i for i in range(n) if position[i] and (i == 0 or not position[i - 1])]
    step = index[1] - index[0]
    return DesignStats(
        interval=interval,
        index=index,
        returns=returns,
        position=position,
        entry_times=pd.DatetimeIndex(index[starts]),
        window_start=index[0],
        window_end=index[-1] + step,
    )


def design_sharpe_annual(design: DesignStats) -> float:
    return sharpe_per_bar(design.returns) * math.sqrt(bars_per_year(design.interval))


def build(
    design: DesignStats,
    *,
    candidate: str = "ev",
    claimed: float | None = None,
    source: str = "owner_supplied",
    cfg: ProtocolConfig | None = None,
    created_at: datetime = CREATED,
    plan: dict[str, Any] | None = None,
) -> PaperProtocol:
    """A protocol on ``design``; the claim defaults to the design Sharpe (owner-supplied) so the
    arithmetic is checkable against the same returns."""
    value = design_sharpe_annual(design) if claimed is None else claimed
    return build_protocol(
        candidate=candidate,
        plan=plan or PLAN,
        design=design,
        costs=COSTS,
        config=cfg or config(),
        created_at=created_at,
        link=LINK,
        claimed_sharpe_annual=value,
        claim_source=source,  # type: ignore[arg-type]
        claim_note="test claim",
    )


def hand_poisson_ppf(q: float, lam: float) -> int:
    k, pmf = 0, math.exp(-lam)
    cdf = pmf
    while cdf < q:
        k += 1
        pmf *= lam / k
        cdf += pmf
    return k


def seed_ledger(
    path: Path,
    plan: dict[str, Any],
    *,
    wf_sharpes: tuple[float | None, ...] = (0.4,),
    run_id: str = "r-test-run",
) -> int:
    """A tmp ledger holding ``plan``'s design-window trial and its walk-forward evidence."""
    p = parse_plan(plan)
    with Ledger(path) as led:
        led.start_research_run(run_id, label="t", config_json="{}", config_sha256="0" * 64)
        trial_id = led.record(
            TrialRecord(
                run_id=run_id,
                source="api",
                status="ok",
                plan_hash=plan_hash(p),
                plan_json=json.dumps(plan),
                params_json="{}",
                symbol=p.symbol,
                interval=p.interval,
                window_kind="design",
                window_start=pd.Timestamp("2025-01-01", tz="UTC"),
                window_end=pd.Timestamp("2025-10-01", tz="UTC"),
                metrics_json="{}",
                sharpe_per_bar=0.01,
                n_trades=4,
                returns=np.zeros(16),
            )
        )
        for value in wf_sharpes:
            payload = {"oos_metrics": {"sharpe": value}}
            led.record_evidence(run_id, plan_hash(p), "walk_forward", json.dumps(payload))
    return trial_id


# --------------------------------------------------------------------------- config


def test_config_file_loads_with_documented_values() -> None:
    cfg = load_protocol_config()
    assert (cfg.min_days, cfg.max_days) == (28.0, 56.0)
    assert cfg.confidence == 0.95 and cfg.benchmark_annual == 0.0
    assert cfg.shortfall_tolerance_mult == 1.0 and cfg.shortfall_min_fills == 3
    assert cfg.fill_noise_bps == 10.0 and cfg.gross_breakage_multiple == 4.0
    assert cfg.match_tolerance_bars == 1 and cfg.irregular_share == 0.05
    assert cfg.max_band_width == 0.8
    assert len(cfg.config_sha256) == 64


def test_config_validation_names_the_bad_key(tmp_path: Path) -> None:
    text = proto_mod.DEFAULT_PROTOCOL_CONFIG.read_text()
    bad = tmp_path / "p.toml"
    cases = [
        ("max_days = 56", "max_days = 7", "min_days <= max_days"),
        ("power = 0.8", "power = 0.3", "power"),
        ("[shortfall]", "[shortfallx]", r"\[shortfall\]"),
        ("[matching]", "[matchingx]", r"\[matching\]"),
        ("irregular_share = 0.05", "irregular_share = 0.5", "irregular_share"),
        ("gross_breakage_multiple = 4.0", "gross_breakage_multiple = 1.0", "gross_breakage"),
        ("fill_noise_bps = 10.0", "fill_noise_bps = 0.0", "fill_noise_bps"),
        ("max_band_width = 0.8", "max_band_width = 0.0", "max_band_width"),
        ("match_tolerance_bars = 1", "match_tolerance_bars = -1", "match_tolerance_bars"),
    ]
    for old, new, match in cases:
        assert old in text
        bad.write_text(text.replace(old, new))
        with pytest.raises(ValueError, match=match):
            load_protocol_config(bad)


# --------------------------------------------------------------------------- arithmetic


@pytest.mark.parametrize("lam", [0.5, 3.0, 10.0, 42.7])
def test_poisson_band_matches_hand_cdf(lam: float) -> None:
    lo, hi = poisson_band(lam, 0.95)
    assert lo == hand_poisson_ppf(0.025, lam)
    assert hi == hand_poisson_ppf(0.975, lam)
    assert poisson_band(0.0, 0.95) == (0, 0)
    assert poisson_band(10.0, 0.95) == (4, 17)  # textbook 95% band for a mean of 10


def test_design_blocks_by_hand_are_scored_flat_at_the_block_start() -> None:
    index = pd.date_range("2024-01-01", periods=70, freq="1D", tz="UTC")
    position = np.zeros(70)
    position[:10] = 1.0
    design = DesignStats(
        interval="1d",
        index=index,
        returns=np.zeros(70),
        position=position,
        entry_times=pd.DatetimeIndex(index[[0, 3, 10, 20, 50]]),
        window_start=index[0],
        window_end=index[-1] + DAY,
    )
    counts, exposure = design_blocks(design, block_seconds=14 * 86400, step_seconds=7 * 86400)
    assert counts.tolist() == [3, 2, 1, 0, 0, 0, 1, 1, 0]
    assert exposure[0] == pytest.approx(10 / 14)
    # block 1 starts on day 7 inside the position entered on day 3: a bot starting flat on
    # day 7 cannot hold it, so those 3 bars do not count (it used to read 3/14)
    assert exposure[1] == 0.0
    assert exposure[2:].tolist() == [0.0] * 7


def test_design_block_exposure_ignores_a_carried_position_until_it_exits() -> None:
    """Note 2 (probe_flat): one long trade from day 0 to day 19, a second from day 30 to 34.
    A block starting on day 14 is flat until day 30, then holds 5 bars."""
    index = pd.date_range("2024-01-01", periods=60, freq="1D", tz="UTC")
    position = np.zeros(60)
    position[0:20] = 1.0
    position[30:35] = 1.0
    design = DesignStats(
        interval="1d",
        index=index,
        returns=np.zeros(60),
        position=position,
        entry_times=pd.DatetimeIndex(index[[0, 30]]),
        window_start=index[0],
        window_end=index[-1] + DAY,
    )
    counts, exposure = design_blocks(design, block_seconds=28 * 86400, step_seconds=14 * 86400)
    # blocks [0, 28), [14, 42), [28, 56)
    assert counts.tolist() == [1, 1, 1]
    assert exposure[0] == pytest.approx(20 / 28)
    assert exposure[1] == pytest.approx(5 / 28)  # carried bars 14..19 ignored; 30..34 count
    assert exposure[2] == pytest.approx(5 / 28)


def test_module_docstring_says_bars_at_or_after_the_split_are_never_loaded() -> None:
    doc = proto_mod.__doc__ or ""
    assert "bars at or after the lab's locked split are" in doc
    assert "never loaded" in doc and "bars before the lab's locked split" not in doc


def test_build_protocol_arithmetic_implementation_only() -> None:
    design = synthetic_design(mean=0.0001)
    p = build(design, candidate="ema-x")
    bpy = bars_per_year("4h")
    assert bpy == 365 * 6  # crypto calendar, never 252
    sr = sharpe_per_bar(design.returns)
    sh = p.sharpe
    assert sh.claim_source == "owner_supplied" and sh.claim_note == "test claim"
    assert sh.claimed_per_bar == pytest.approx(sr, rel=1e-12)
    assert sh.claimed_annual == pytest.approx(sr * math.sqrt(2190), rel=1e-12)
    # MinTRL straight from lab.stats on the same returns
    assert sh.min_trl_bars == pytest.approx(min_track_record_length(design.returns, 0.0, 0.95))
    assert sh.min_trl_days == pytest.approx(sh.min_trl_bars / 6)
    # power 0.8: the same lab formula at confidence Phi(z_0.95 + z_0.8), checked by hand
    g3 = float(sp_stats.skew(design.returns))
    g4 = float(sp_stats.kurtosis(design.returns, fisher=False))
    z = sp_stats.norm.ppf(0.95) + sp_stats.norm.ppf(0.8)
    by_hand = 1 + (1 - g3 * sr + (g4 - 1) / 4 * sr**2) * (z / sr) ** 2
    assert sh.decision_trl_bars == pytest.approx(by_hand, rel=1e-9)
    assert not sh.testable and p.mode == "implementation_only"
    assert sh.decision_trl_days > 56
    # decision at the maximum: 56 days of 4h bars
    assert p.decision_bars == 56 * 6 and p.decision_days == 56.0
    # clock: next 4h boundary after 10:30:07 is 12:00
    assert p.start == int(datetime(2026, 11, 2, 12, tzinfo=UTC).timestamp())
    assert p.decision_end == p.start + 336 * 4 * 3600
    # entries: rate x horizon, band = Poisson united with design blocks
    n = len(design.entry_times)
    t = p.trades
    assert t.design_trades == n and t.expected == pytest.approx(n / 365 * 56)
    assert (t.poisson_lo, t.poisson_hi) == (
        hand_poisson_ppf(0.025, t.expected),
        hand_poisson_ppf(0.975, t.expected),
    )
    counts, exposures = design_blocks(design, block_seconds=56 * 86400, step_seconds=7 * 86400)
    assert p.design_blocks == counts.size == (365 - 56) // 7 + 1
    assert t.empirical_lo == int(np.quantile(counts, 0.025, method="lower"))
    assert t.empirical_hi == int(np.quantile(counts, 0.975, method="higher"))
    # predictive interval for one new block: m = floor(365 / 56) = 6 non-overlapping blocks,
    # mean and sd from every rolling block, t with m - 1 = 5 degrees of freedom
    assert t.independent_blocks == 6
    half = sp_stats.t.ppf(0.975, 5) * counts.std(ddof=1) * math.sqrt(1 + 1 / 6)
    assert t.predictive_lo == max(0, math.floor(counts.mean() - half))
    assert t.predictive_hi == math.ceil(counts.mean() + half)
    assert t.lo == min(t.poisson_lo, t.empirical_lo, t.predictive_lo)
    assert t.hi == max(t.poisson_hi, t.empirical_hi, t.predictive_hi)
    # exposure: share of bars with a position, empirical (flat-start) quantiles united with the
    # same predictive interval, +/- slack
    e = p.exposure
    assert e.expected == pytest.approx(np.mean(design.position != 0))
    x_half = sp_stats.t.ppf(0.975, 5) * exposures.std(ddof=1) * math.sqrt(1 + 1 / 6)
    assert e.predictive_lo == pytest.approx(exposures.mean() - x_half)
    assert e.predictive_hi == pytest.approx(exposures.mean() + x_half)
    x_lo = float(np.quantile(exposures, 0.025, method="lower"))
    x_hi = float(np.quantile(exposures, 0.975, method="higher"))
    assert e.lo == pytest.approx(max(0.0, min(x_lo, e.predictive_lo) - 0.02))
    assert e.hi == pytest.approx(min(1.0, max(x_hi, e.predictive_hi) + 0.02))
    # frozen data rule
    assert p.data.candle_source == "api" and p.data.candle_host == "data-api.binance.vision"
    assert p.data.warmup_bars == 200 and p.data.grace_days == 7.0
    # shortfall: 1 x the 5 bps modelled slippage; fee + slippage per side
    s = p.shortfall
    assert s.tolerance_bps == pytest.approx(5.0)
    assert s.modelled_fill_cost_bps == pytest.approx(15.0)
    assert s.round_trip_cost_bps == pytest.approx(30.0)
    assert s.gross_bps == pytest.approx(20.0)
    # timing rule frozen from the config
    assert (p.matching.tolerance_bars, p.matching.irregular_share) == (1, 0.05)
    assert p.logic_hash == logic_hash(PLAN) and p.plan_hash == plan_hash(parse_plan(PLAN))
    # ledger link recorded
    assert (p.source.run_id, p.source.trial_id) == (LINK.run_id, LINK.trial_id)
    assert p.source.wf_evidence_ids == (3,)
    # implementation-only wording, written upfront; nothing vacuous here
    assert any("Sharpe NOT tested" in r and "never 'Sharpe confirmed'" in r for r in p.rules)
    assert p.inconclusive_reasons == ()


def test_false_fail_probability_arithmetic_by_hand() -> None:
    """W1: median ~ N(mu, (pi/2) sigma^2 / n) with mu = -slippage, stated in the rules."""
    p = build(synthetic_design(mean=0.0001))
    s = p.shortfall
    n_fill = p.trades.expected * 2.0  # every synthetic design trade leaves on a signal
    assert s.expected_fills == pytest.approx(n_fill)
    assert s.overall_n == math.floor(n_fill)
    sd = math.sqrt(math.pi / 2) * 10.0 / math.sqrt(s.overall_n)
    assert s.honest_mean_bps == pytest.approx(-5.0)
    assert s.overall_sd_bps == pytest.approx(sd)
    assert s.false_fail_overall == pytest.approx(1 - sp_stats.norm.cdf((5.0 + 5.0) / sd))
    sd3 = math.sqrt(math.pi / 2) * 10.0 / math.sqrt(3)
    p_week = 1 - sp_stats.norm.cdf((20.0 + 5.0) / sd3)
    assert s.weekly_sd_bps == pytest.approx(sd3)
    assert s.false_fail_gross_week == pytest.approx(p_week)
    assert s.weeks == 8
    assert s.false_fail_gross_any_week == pytest.approx(1 - (1 - p_week) ** 8)
    windows = s.overall_n - 3 + 1
    assert s.windows_expected == windows
    assert s.false_fail_gross_any_window == pytest.approx(min(1.0, windows * p_week))
    assert s.false_fail_gross_early == pytest.approx(min(1.0, (8 + windows) * p_week))
    assert s.false_fail_gross_early < 0.01  # ~22 fills: under 1% with the default config
    text = " ".join(p.rules)
    assert "sqrt(pi/2)" in text and f"{s.false_fail_overall:.2%}" in text
    assert f"{s.false_fail_gross_any_week:.3%}" in text
    assert f"min(1, {windows} x P) = {s.false_fail_gross_any_window:.3%}" in text
    assert f"min(1, (8 + {windows}) x P) = {s.false_fail_gross_early:.3%}" in text


def test_false_fail_probability_matches_a_seeded_simulation() -> None:
    """The normal approximation of the median is close to the truth at the expected fill count:
    20 000 seeded honest bots (fills N(-5, 10) bps). Tolerance 0.01 absolute: the normal
    approximation of a median over ~22 draws is within a fraction of a percent here."""
    p = build(synthetic_design(mean=0.0001))
    s = p.shortfall
    rng = np.random.default_rng(42)
    draws = rng.normal(s.honest_mean_bps, s.fill_noise_bps, (20_000, s.overall_n))
    empirical = float(np.mean(np.median(draws, axis=1) > s.tolerance_bps))
    assert empirical == pytest.approx(s.false_fail_overall, abs=0.01)


def test_build_protocol_testable_sharpe_and_power_half_is_plain_mintrl() -> None:
    design = synthetic_design(mean=0.002)  # per-bar SR ~ 0.2: MinTRL of days, not years
    p = build(design, candidate="hi")
    assert p.sharpe.testable and p.mode == "sharpe_and_implementation"
    assert p.sharpe.decision_trl_days <= 56
    expected_days = max(p.sharpe.decision_trl_days, 28.0)
    assert p.decision_bars == math.ceil(expected_days * 6 - 1e-9)
    (rule,) = [r for r in p.rules if r.startswith("Sharpe: claimed")]
    assert "the claimed Sharpe itself is not confirmed" in rule

    half = build(design, candidate="hi", cfg=config(power=0.5))
    assert half.sharpe.decision_trl_bars == pytest.approx(half.sharpe.min_trl_bars, rel=1e-12)


def test_min_testable_sharpe_is_printed_and_fits_the_maximum_exactly() -> None:
    """W2: the rules print the smallest annual Sharpe the 56-day maximum can test; the decision
    track record at that Sharpe is exactly the maximum's bars."""
    design = synthetic_design(mean=0.0001)
    p = build(design)
    sh = p.sharpe
    assert sh.min_testable_annual is not None
    z = sp_stats.norm.ppf(0.95) + sp_stats.norm.ppf(0.8)
    per_bar = sh.min_testable_annual / math.sqrt(2190)
    trl = min_track_record_length_from_moments(
        per_bar, sh.skew, sh.kurtosis, 0.0, float(sp_stats.norm.cdf(z))
    )
    assert trl == pytest.approx(56 * 6, rel=1e-6)
    assert sh.claimed_annual < sh.min_testable_annual  # why this protocol is impl-only
    assert any(f"is {sh.min_testable_annual:.2f}." in r for r in p.rules)
    assert min_testable_sharpe(float("nan"), 3.0, 0.0, 0.95, 336) is None


def test_claim_owner_supplied_uses_365_day_scaling_and_design_moments() -> None:
    design = synthetic_design(mean=0.0001)
    p = build(design, candidate="ov", claimed=2.0)
    per_bar = 2.0 / math.sqrt(2190)
    g3 = float(sp_stats.skew(design.returns))
    g4 = float(sp_stats.kurtosis(design.returns, fisher=False))
    assert p.sharpe.claim_source == "owner_supplied"
    assert p.sharpe.claimed_per_bar == pytest.approx(per_bar)
    assert p.sharpe.min_trl_bars == pytest.approx(
        min_track_record_length_from_moments(per_bar, g3, g4, 0.0, 0.95)
    )
    assert any("owner-supplied" in r for r in p.rules)


def test_resolve_claim_defaults_to_the_minimum_walk_forward_oos_sharpe() -> None:
    link = replace(LINK, wf_oos_sharpes=(0.9, 0.3, 0.6), wf_evidence_ids=(1, 2, 3))
    value, source, note = resolve_claim(link, None)
    assert (value, source) == (0.3, "walk_forward_oos")
    assert "minimum" in note and "1, 2, 3" in note
    value, source, note = resolve_claim(link, 1.7)
    assert (value, source) == (1.7, "owner_supplied") and "owner-supplied" in note
    with pytest.raises(ValueError, match="never used as the claim"):
        resolve_claim(replace(LINK, wf_oos_sharpes=(), wf_evidence_ids=()), None)
    with pytest.raises(ValueError, match="undefined"):
        resolve_claim(replace(LINK, wf_oos_sharpes=(0.5, None), wf_evidence_ids=(1, 2)), None)
    with pytest.raises(ValueError, match="finite"):
        resolve_claim(LINK, float("nan"))


def test_build_protocol_refuses_a_short_design_window() -> None:
    with pytest.raises(ValueError, match="min_blocks"):
        build(synthetic_design(mean=0.0001, days=80), candidate="short")


def test_protocol_json_round_trip_and_sha256() -> None:
    p = build(synthetic_design(mean=0.0001), candidate="rt")
    again = protocol_from_dict(json.loads(protocol_to_json(p)))
    assert again == p
    assert protocol_sha256(again) == protocol_sha256(p)
    changed = replace(p, trades=replace(p.trades, hi=p.trades.hi + 1))
    assert protocol_sha256(changed) != protocol_sha256(p)
    with pytest.raises(ValueError, match="malformed"):
        protocol_from_dict({**protocol_to_dict(p), "trades": {"lo": 1}})
    with pytest.raises(ValueError, match="version"):
        protocol_from_dict({**protocol_to_dict(p), "version": 1})


# --------------------------------------------------------------------------- vacuous bands (W3)


def test_vacuous_bands_are_inconclusive_and_said_upfront() -> None:
    design = synthetic_design(mean=0.0001)
    base = build(design)
    assert base.inconclusive_reasons == ()
    # weak entry count
    weak = build(design, cfg=config(trades_min_expected=50))
    assert weak.trades.weak and any("cannot discriminate" in r for r in weak.inconclusive_reasons)
    # an exposure band as wide as [0, 1]: slack 0.9 widens the band past max_band_width
    wide = build(design, cfg=config(exposure_slack=0.5))
    assert wide.exposure.hi - wide.exposure.lo >= 0.8
    assert any("exposure band" in r for r in wide.inconclusive_reasons)
    for p in (weak, wide):
        assert any("never pass" in r for r in p.rules)
        v = evaluate(p, observed(p))
        assert v.verdict == "inconclusive", v.headline
        assert v.final


def test_entry_band_including_zero_is_inconclusive() -> None:
    design = synthetic_design(mean=0.0001)
    sparse = DesignStats(
        interval=design.interval,
        index=design.index,
        returns=design.returns,
        position=np.zeros(len(design.index)),
        entry_times=design.entry_times[::40],  # ~2 entries per 56 days
        window_start=design.window_start,
        window_end=design.window_end,
    )
    p = build(sparse, cfg=config(trades_min_expected=0))
    assert p.trades.lo == 0
    assert any("includes 0" in r for r in p.inconclusive_reasons)
    v = evaluate(p, observed(p, entries=2, exposure=0.0))
    assert v.verdict == "inconclusive" and any("includes 0" in r for r in v.reasons)


# --------------------------------------------------------------------------- evaluation


def impl_only() -> PaperProtocol:
    return build(synthetic_design(mean=0.0001), candidate="ev")


def sharpe_protocol() -> PaperProtocol:
    return build(synthetic_design(mean=0.002), candidate="ev2")


def observed(
    p: PaperProtocol,
    *,
    asof: int | None = None,
    until: int | None = None,
    entries: int | None = None,
    exposure: float | None = None,
    fills: tuple[Fill, ...] | None = None,
    returns: tuple[float, ...] = (),
    bars_present: int | None = None,
    missing: int = 0,
    missing_entries: int = 0,
    lab_entries: int | None = None,
    matched: int | None = None,
    settled: int = 0,
    exits: int | None = None,
    lab_exits: int | None = None,
    matched_exits: int | None = None,
    late: int = 0,
    early: int = 0,
    exit_late: int = 0,
    exit_early: int = 0,
) -> ObservedPaper:
    """A complete, fully matched, on-time observation unless told otherwise."""
    asof = p.decision_end if asof is None else asof
    bars = (min(asof, p.decision_end) - p.start) // (4 * 3600)
    n = round(p.trades.expected) if entries is None else entries
    x = (p.exposure.lo + p.exposure.hi) / 2 if exposure is None else exposure
    if fills is None:
        fills = tuple(Fill(p.start + i * 86400 * 3, "entry", 0.0) for i in range(6))
    present = bars if bars_present is None else bars_present
    n_exits = n if exits is None else exits
    m_entries = n if matched is None else matched
    m_exits = n_exits if matched_exits is None else matched_exits
    obs = ObservedPaper(
        asof=asof,
        observed_until=asof if until is None else until,
        bars_expected=bars,
        bars_elapsed=present,
        n_entries=n,
        bars_in_market=int(x * bars),
        fills=fills,
        fills_skipped=0,
        fills_missing_candle=missing,
        entries_missing_candle=missing_entries,
        lab_entries=n if lab_entries is None else lab_entries,
        matched_entries=m_entries,
        unmatched_paper_settled=settled,
        paper_exits=n_exits,
        lab_exits=n_exits if lab_exits is None else lab_exits,
        matched_exits=m_exits,
        paper_signal_exits=n_exits,
        lab_signal_exits=n_exits,
        entry_early=early,
        entry_late=late,
        exit_early=exit_early,
        exit_late=exit_late,
        irregular_before_gap=0,
        bar_returns=returns,
    )
    # complete data: every irregular fill is settled; incomplete: best case, none before a gap
    return replace(obs, irregular_before_gap=obs.irregular if obs.complete else 0)


def test_continue_before_the_decision_bar() -> None:
    p = impl_only()
    v = evaluate(p, observed(p, asof=p.start + 10 * 86400, entries=1))
    assert v.verdict == "continue" and not v.at_decision and not v.final
    assert v.elapsed_days == pytest.approx(10.0)
    assert {c.status for c in v.checks} == {"pending"}


def test_pass_at_decision_is_implementation_only_wording() -> None:
    p = impl_only()
    assert p.trades.lo <= round(p.trades.expected) <= p.trades.hi
    v = evaluate(p, observed(p))
    assert v.verdict == "pass", v.headline
    assert "implementation consistent" in v.headline and "NOT tested" in v.headline
    assert v.mode == "implementation_only" and v.data_complete and v.final
    assert v.protocol_sha256 == protocol_sha256(p)
    assert not any(c.name == "sharpe" for c in v.checks)


def test_incomplete_candles_at_decision_continue_never_pass() -> None:
    """C1: the decision bar has passed but half the bars are missing: continue, not pass."""
    p = impl_only()
    half = p.decision_bars // 2
    v = evaluate(p, observed(p, bars_present=half))
    assert v.verdict == "continue" and v.at_decision and not v.data_complete
    assert v.reasons == (f"incomplete data: {half} of {p.decision_bars} bars, 0 fills unjudged",)
    assert "incomplete data" in v.headline and not v.final


def test_unjudged_fills_at_decision_continue_unless_the_rule_fails_anyway() -> None:
    """C1 / W-A: two fills fell on missing candles. Nothing else wrong: continue. An entry
    count below the band does not depend on candles, so it fails whatever they hold."""
    p = impl_only()
    v = evaluate(p, observed(p, missing=2, missing_entries=1))
    assert v.verdict == "continue"
    assert "2 fills unjudged" in v.headline
    low = evaluate(p, observed(p, missing=2, entries=0, matched=0, exits=0))
    assert low.verdict == "fail" and low.final
    assert any("0 entries <" in r for r in low.reasons)
    assert "whatever the missing data hold" in low.headline


def test_early_fail_still_fires_with_incomplete_data() -> None:
    p = impl_only()
    v = evaluate(p, observed(p, bars_present=10, entries=p.trades.hi + 1))
    assert v.verdict == "fail" and v.final


def test_weekly_shortfall_above_tolerance_is_an_alert_not_a_fail() -> None:
    """W1: a week at 12 bps (> 5 bps tolerance, < 20 bps gross) is reported, never a verdict."""
    p = impl_only()
    drift = tuple(Fill(p.start + 3600 * (10 + i), "entry", 12.0) for i in range(3))
    v = evaluate(p, observed(p, asof=p.start + 8 * 86400, entries=2, fills=drift))
    assert v.verdict == "continue"
    assert v.weeks[0].judged and v.weeks[0].alert and not v.weeks[0].gross
    assert v.alerts and "alert only" in v.alerts[0]


def test_gross_weekly_breakage_fails_before_the_decision_bar() -> None:
    p = impl_only()
    drift = tuple(Fill(p.start + 3600 * (10 + i), "entry", 25.0) for i in range(3))
    v = evaluate(p, observed(p, asof=p.start + 8 * 86400, entries=2, fills=drift))
    assert v.verdict == "fail" and not v.at_decision
    assert "gross breakage" in v.headline
    assert v.weeks[0].gross and v.weeks[0].median_bps == pytest.approx(25.0)
    # the same fills while week 0 is still running: the week is not judged yet, but the three
    # consecutive fills already are (rolling fill window)
    early = evaluate(p, observed(p, asof=p.start + 5 * 86400, entries=2, fills=drift))
    assert not early.weeks[0].judged
    assert early.verdict == "fail"
    assert [c.name for c in early.checks if c.status == "fail"] == ["shortfall_fills_0"]
    # two bad fills in a week are below min_fills: not judged on their own
    two = evaluate(p, observed(p, asof=p.start + 8 * 86400, entries=2, fills=drift[:2]))
    assert two.verdict == "continue"


def test_gross_fill_windows_by_hand() -> None:
    def f(i: int, bps: float) -> Fill:
        return Fill(i, "entry", bps)

    fills = [f(0, -5.0), f(1, 30.0), f(2, 25.0), f(3, -5.0), f(4, 40.0), f(5, 50.0)]
    # windows of 3: [-5,30,25]->25, [30,25,-5]->25, [25,-5,40]->25, [-5,40,50]->40
    assert gross_fill_windows(fills, size=3, gross_bps=20.0) == [
        (0, 25.0),
        (1, 25.0),
        (2, 25.0),
        (3, 40.0),
    ]
    assert gross_fill_windows(fills, size=3, gross_bps=30.0) == [(3, 40.0)]
    assert gross_fill_windows(fills[:2], size=3, gross_bps=0.0) == []
    with pytest.raises(ValueError, match="size"):
        gross_fill_windows(fills, size=0, gross_bps=0.0)


def test_sparse_run_of_bad_fills_fails_through_the_fill_window() -> None:
    """Fills spread one per week never make a judged week; three consecutive bad ones still
    fail at once, and one bad fill among honest ones does not."""
    p = impl_only()
    wk = 7 * 86400
    honest = [Fill(p.start + k * wk + 3600, "entry", -5.0) for k in range(4)]
    bad = [Fill(p.start + (4 + k) * wk + 3600, "entry", 55.0) for k in range(3)]
    v = evaluate(p, observed(p, asof=p.start + 7 * wk + 7200, entries=4, fills=(*honest, *bad)))
    assert not any(w.judged for w in v.weeks)
    assert v.verdict == "fail" and "3 consecutive fills" in v.headline
    one = (*honest, bad[0], *[replace(h, time=h.time + 5 * wk) for h in honest[:2]])
    ok = evaluate(p, observed(p, asof=p.start + 7 * wk + 7200, entries=4, fills=one))
    assert ok.verdict == "continue"


def test_overall_median_shortfall_decides_at_the_decision_bar() -> None:
    p = impl_only()
    bad = tuple(Fill(p.start + i * 86400 * 3, "entry", 8.0) for i in range(6))
    v = evaluate(p, observed(p, fills=bad))
    assert v.verdict == "fail" and any("median +8.0 bps" in r for r in v.reasons)
    assert v.fail_kinds == ("implementation",)
    assert any(r.startswith("[implementation difference]") for r in v.reasons)


def test_favourable_shortfall_within_gross_is_not_a_breach_beyond_it_is_look_ahead() -> None:
    """W-B two-sided: -15 bps (better than the modelled fill, inside -gross = -20) passes; a
    median of -30 bps is implausibly favourable and fails naming possible look-ahead."""
    p = impl_only()
    good = tuple(Fill(p.start + i * 86400, "entry", -15.0) for i in range(7))
    assert evaluate(p, observed(p, fills=good)).verdict == "pass"
    too_good = tuple(Fill(p.start + i * 86400, "entry", -30.0) for i in range(7))
    v = evaluate(p, observed(p, fills=too_good))
    assert v.verdict == "fail"
    assert any("implausibly favourable" in r and "look-ahead" in r for r in v.reasons)


def test_entry_count_above_band_fails_at_once_and_below_fails_at_decision() -> None:
    p = impl_only()
    v = evaluate(p, observed(p, asof=p.start + 86400, entries=p.trades.hi + 1))
    assert v.verdict == "fail" and not v.at_decision
    assert p.trades.lo > 0
    low = evaluate(p, observed(p, entries=p.trades.lo - 1))
    assert low.verdict == "fail" and any("entries <" in r for r in low.reasons)


def test_exposure_outside_band_fails_at_decision() -> None:
    p = impl_only()
    v = evaluate(p, observed(p, exposure=min(1.0, p.exposure.hi + 0.1)))
    assert v.verdict == "fail" and any("exposure" in r or "bars in market" in r for r in v.reasons)


def test_too_few_fills_at_decision_are_inconclusive_not_verifiable() -> None:
    p = impl_only()
    v = evaluate(p, observed(p, fills=(Fill(p.start, "entry", 0.0),)))
    assert v.verdict == "inconclusive" and "not verifiable" in v.headline


def test_unmatched_entries_count_on_both_sides_at_decision() -> None:
    """C-new: 11 paper and 11 lab entries, 7 on the same bars: 4 + 4 irregular fills (the
    0.8 match-rate floor this replaces let up to 20% through)."""
    p = impl_only()
    v = evaluate(p, observed(p, entries=11, lab_entries=11, matched=7, settled=0))
    assert v.verdict == "fail"
    assert any("unmatched entries 4 paper / 4 lab" in r for r in v.reasons)
    one = evaluate(p, observed(p, entries=11, lab_entries=10, matched=10))
    assert one.verdict == "pass", one.headline  # 1 irregular of 42 fills, 2 allowed


def test_settled_unmatched_paper_entries_fail_early() -> None:
    """C-new: settled unmatched paper entries above the most the decision bar could allow
    (max(1, floor(0.05 x 4 x hi))) fail at once."""
    p = impl_only()
    cap = proto_mod.irregular_allowance(p.matching, 4 * p.trades.hi)
    need = cap + 1
    v = evaluate(p, observed(p, asof=p.start + 10 * 86400, entries=need, matched=0, settled=need))
    assert v.verdict == "fail" and not v.at_decision
    assert any("never can" in r for r in v.reasons)
    fewer = evaluate(
        p, observed(p, asof=p.start + 10 * 86400, entries=need, matched=0, settled=cap)
    )
    assert fewer.verdict == "continue"


def test_sharpe_tested_pass_and_fail() -> None:
    p = sharpe_protocol()
    rng = np.random.default_rng(11)
    good = tuple(float(x) for x in rng.normal(0.003, 0.01, p.decision_bars))
    v = evaluate(p, observed(p, returns=good))
    assert v.verdict == "pass", v.headline
    assert "SR > 0 at 95% (PSR); the claimed Sharpe itself is not confirmed" in v.headline
    assert "Sharpe confirmed" not in v.headline
    flat = tuple(float(x) for x in rng.normal(0.0, 0.01, p.decision_bars))
    bad = evaluate(p, observed(p, returns=flat))
    assert bad.verdict == "fail" and any("PSR" in r for r in bad.reasons)


# --------------------------------------------------------------------------- observation


def flat_candles(start: pd.Timestamp, bars: int, step: pd.Timedelta) -> pd.DataFrame:
    index = pd.date_range(start, periods=bars, freq=step, name="time")
    return pd.DataFrame(
        {
            "open": np.full(bars, 100.0),
            "high": np.full(bars, 101.0),
            "low": np.full(bars, 99.0),
            "close": np.full(bars, 100.0),
            "volume": np.full(bars, 10.0),
            "tradable": np.ones(bars, dtype=bool),
        },
        index=index,
    )


def ftrade(
    open_time: pd.Timestamp,
    rate: float,
    *,
    close_time: pd.Timestamp | None = None,
    close_rate: float | None = None,
    reason: str | None = "exit_signal",
    fee: float | None = 0.001,
) -> ForwardTrade:
    return ForwardTrade(
        pair="BTC/USDT",
        is_open=close_time is None,
        open_time=open_time,
        open_rate=rate,
        close_time=close_time,
        close_rate=close_rate,
        fee_open=fee,
        fee_close=fee if close_time is not None else None,
        exit_reason=reason if close_time is not None else None,
    )


def lab_trade(entry: pd.Timestamp, exit_: pd.Timestamp | None, reason: str = "signal") -> Trade:
    return Trade(
        direction="long",
        entry_time=entry,
        entry_price=100.0,
        exit_time=exit_,
        exit_price=None if exit_ is None else 100.0,
        size=1.0,
        pnl=0.0,
        return_pct=0.0,
        fees=0.0,
        exit_reason=reason if exit_ is not None else "end",  # type: ignore[arg-type]
        bars_held=0,
    )


def test_fill_shortfall_by_hand() -> None:
    t0 = pd.Timestamp("2026-11-02", tz="UTC")
    candles = flat_candles(t0, 48, HOUR)
    slip = modelled_slippage(candles, COSTS)  # flat 5 bps (no vol scaling)
    trades = [
        ftrade(t0 + 2 * HOUR + pd.Timedelta(seconds=3), 100.0,
               close_time=t0 + 6 * HOUR + pd.Timedelta(seconds=2), close_rate=100.0),
        ftrade(t0 + 9 * HOUR, 100.2, close_time=t0 + 12 * HOUR, close_rate=97.0,
               reason="stop_loss", fee=0.002),
    ]  # fmt: skip
    fills, skipped = fill_shortfalls(
        trades, candles, slip, interval="1h", fee_rate=0.001, start=t0, end=t0 + 48 * HOUR
    )
    assert skipped == 1  # the stop-loss exit fills at a level in the lab: not judged
    assert [f.side for f in fills] == ["entry", "exit", "entry"]
    assert fills[0].shortfall_bps == pytest.approx((100 / 100.05 - 1) * 1e4)  # -5.0 bps
    assert fills[1].shortfall_bps == pytest.approx((1 - 100 / 99.95) * 1e4)
    # worse price and a 10 bps higher fee than the lab: both are shortfall
    assert fills[2].shortfall_bps == pytest.approx((100.2 / 100.05 - 1) * 1e4 + 10.0)


def test_match_fill_bars_is_one_to_one_and_maximal() -> None:
    h = 3600
    # lab at 10h and 11h; paper at 10h and 12h: greedy-nearest would pair 11h-10h? no: each
    # lab fill takes the earliest feasible paper fill, so 10->10 and 11->12 (both matched)
    assert match_fill_bars([10 * h, 12 * h], [10 * h, 11 * h], tolerance_seconds=h) == [
        (0, 0),
        (1, 1),
    ]
    # a paper fill matches at most once
    assert match_fill_bars([10 * h], [10 * h, 11 * h], tolerance_seconds=h) == [(0, 0)]
    # outside the tolerance: no match
    assert match_fill_bars([13 * h], [10 * h], tolerance_seconds=h) == []
    # zero tolerance: exact bars only
    assert match_fill_bars([10 * h, 11 * h], [11 * h], tolerance_seconds=0) == [(1, 0)]
    with pytest.raises(ValueError, match="tolerance"):
        match_fill_bars([], [], tolerance_seconds=-1)


def test_lagged_fill_is_timed_not_priced_and_lag0_fills_use_the_lab_bar() -> None:
    """W-B: a paper entry one bar late is matched and counted as late, but its shortfall is not
    judged (the lag-0 rule judges it); the lag-0 signal exit is judged against the lab bar's
    open x (1 - s)."""
    p = impl_only()
    t0 = pd.Timestamp(p.start, unit="s", tz="UTC")
    candles = flat_candles(t0 - 10 * STEP4H, 60, STEP4H)
    candles.loc[t0 + 3 * STEP4H, "open"] = 101.0  # the bar after the lab's fill bar
    slip = modelled_slippage(candles, COSTS)
    lab = [lab_trade(t0 + 2 * STEP4H, t0 + 6 * STEP4H)]
    paper = [
        ftrade(t0 + 3 * STEP4H + pd.Timedelta(seconds=4), 101.0,
               close_time=t0 + 6 * STEP4H + pd.Timedelta(seconds=4), close_rate=100.0),
    ]  # fmt: skip
    obs = observe_paper(p, paper, candles, lab_trades=lab, slippage=slip, asof=t0 + 20 * STEP4H)
    assert (obs.matched_entries, obs.lab_entries, obs.n_entries) == (1, 1, 1)
    assert obs.matched_exits == 1 and (obs.paper_exits, obs.lab_exits) == (1, 1)
    assert (obs.matched_lag0, obs.matched_late, obs.matched_early) == (1, 1, 0)
    (exit_,) = obs.fills
    assert exit_.side == "exit" and exit_.lag_bars == 0
    assert exit_.shortfall_bps == pytest.approx((1 - 100 / 99.95) * 1e4)


def test_exits_of_every_reason_are_matched_by_kind() -> None:
    """C-A: a stop-loss paper exit matches a lab stop-loss exit; a ROI exit where the lab exited
    on a signal is unmatched; neither stop nor ROI prices are judged."""
    p = impl_only()
    t0 = pd.Timestamp(p.start, unit="s", tz="UTC")
    candles = flat_candles(t0 - 10 * STEP4H, 60, STEP4H)
    slip = modelled_slippage(candles, COSTS)
    lab = [
        lab_trade(t0 + 2 * STEP4H, t0 + 6 * STEP4H, reason="stop_loss"),
        lab_trade(t0 + 10 * STEP4H, t0 + 14 * STEP4H, reason="signal"),
    ]
    paper = [
        ftrade(t0 + 2 * STEP4H + pd.Timedelta(seconds=4), 100.0,
               close_time=t0 + 6 * STEP4H + pd.Timedelta(minutes=50), close_rate=95.0,
               reason="stop_loss"),
        ftrade(t0 + 10 * STEP4H + pd.Timedelta(seconds=4), 100.0,
               close_time=t0 + 14 * STEP4H + pd.Timedelta(seconds=4), close_rate=100.0,
               reason="roi"),
    ]  # fmt: skip
    obs = observe_paper(p, paper, candles, lab_trades=lab, slippage=slip, asof=t0 + 20 * STEP4H)
    assert (obs.paper_exits, obs.lab_exits, obs.matched_exits) == (2, 2, 1)
    assert obs.fills_skipped == 2  # stop + ROI prices are not judged
    assert [f.side for f in obs.fills] == ["entry", "entry"]


def test_paper_bar_returns_by_hand() -> None:
    t0 = pd.Timestamp("2026-11-02", tz="UTC")
    candles = flat_candles(t0, 6, HOUR)
    candles["close"] = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    trade = ftrade(t0 + HOUR + pd.Timedelta(seconds=5), 100.5,
                   close_time=t0 + 3 * HOUR + pd.Timedelta(seconds=5), close_rate=102.5,
                   fee=0.0)  # fmt: skip
    r = paper_bar_returns([trade], candles, interval="1h")
    assert r[0] == 0.0 and r[4] == 0.0 and r[5] == 0.0
    assert r[1] == pytest.approx(101.0 / 100.5 - 1)
    assert r[2] == pytest.approx(102.0 / 101.0 - 1)
    assert r[3] == pytest.approx(102.5 / 102.0 - 1)
    total = float(np.prod(1 + r) - 1)
    assert total == pytest.approx(102.5 / 100.5 - 1)


def test_observe_paper_counts_entries_exposure_coverage_and_ignores_bars_past_asof() -> None:
    p = impl_only()
    t0 = pd.Timestamp(p.start, unit="s", tz="UTC")
    candles = flat_candles(t0 - 10 * STEP4H, 200, STEP4H)
    slip = modelled_slippage(candles, COSTS)
    trades = [
        ftrade(t0 - STEP4H, 100.0, close_time=t0 + STEP4H, close_rate=100.0),  # before the start
        ftrade(t0 + 2 * STEP4H + pd.Timedelta(seconds=4), 100.0,
               close_time=t0 + 5 * STEP4H + pd.Timedelta(seconds=4), close_rate=100.0),
        ftrade(t0 + 20 * STEP4H + pd.Timedelta(seconds=4), 100.0),  # still open at asof
    ]  # fmt: skip
    lab = [lab_trade(t0 + 2 * STEP4H, t0 + 5 * STEP4H), lab_trade(t0 + 20 * STEP4H, None)]
    asof = t0 + 22 * STEP4H
    obs = observe_paper(p, trades, candles, lab_trades=lab, slippage=slip, asof=asof)
    assert obs.n_entries == 2 and obs.lab_entries == 2 and obs.matched_entries == 2
    assert obs.bars_expected == obs.bars_elapsed == 22 and obs.complete
    assert obs.bars_in_market == 3 + 2  # bars 2, 3, 4 and 20, 21 (held at bar close)
    assert len(obs.fills) == 3 and obs.fills_skipped == 0 and obs.fills_missing_candle == 0
    assert len(obs.bar_returns) == 22
    # later bars cannot change what was observed up to asof (no look-ahead)
    later = candles.copy()
    later.loc[later.index >= asof, ["open", "high", "low", "close"]] *= 1.7
    again = observe_paper(p, trades, later, lab_trades=lab, slippage=slip, asof=asof)
    assert again == obs
    # a missing candle under a paper fill: coverage drops and the fill is unjudged
    holed = candles.drop(index=[t0 + 2 * STEP4H])
    gap = observe_paper(p, trades, holed, lab_trades=lab, slippage=slip, asof=asof)
    assert gap.bars_elapsed == 21 and gap.bars_expected == 22
    assert gap.fills_missing_candle == 1 and not gap.complete


def test_unmatched_paper_entry_is_settled_only_when_its_window_is_observed() -> None:
    p = impl_only()
    t0 = pd.Timestamp(p.start, unit="s", tz="UTC")
    candles = flat_candles(t0 - 10 * STEP4H, 200, STEP4H)
    slip = modelled_slippage(candles, COSTS)
    paper = [ftrade(t0 + 5 * STEP4H + pd.Timedelta(seconds=4), 100.0)]
    # asof right after the paper bar: the lab could still fill on bar 6 -> not settled
    at6 = observe_paper(p, paper, candles, lab_trades=[], slippage=slip, asof=t0 + 6 * STEP4H)
    assert at6.unmatched_paper_settled == 0
    at7 = observe_paper(p, paper, candles, lab_trades=[], slippage=slip, asof=t0 + 7 * STEP4H)
    assert at7.unmatched_paper_settled == 1
    # a missing neighbour bar keeps it unsettled (the lab may have filled there)
    holed = candles.drop(index=[t0 + 6 * STEP4H])
    gap = observe_paper(p, paper, holed, lab_trades=[], slippage=slip, asof=t0 + 9 * STEP4H)
    assert gap.unmatched_paper_settled == 0


# --------------------------------------------------------------------------- review probes


def probe_candles(p: PaperProtocol, *, warm: int = 300, seed: int = 1) -> pd.DataFrame:
    """The reviewer's probe candles: a seeded 4h random walk, ``warm`` bars before the start."""
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    idx = pd.date_range(
        start - warm * STEP4H, periods=warm + p.decision_bars, freq="4h", tz="UTC", name="time"
    )
    rng = np.random.default_rng(seed)
    close = 60000 * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx))))
    opens = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, close) * 1.002,
            "low": np.minimum(opens, close) * 0.998,
            "close": close,
            "volume": 1.0,
            "tradable": True,
        },
        index=idx,
    )


def fixed_clock_bot(
    p: PaperProtocol, c: pd.DataFrame, *, warm: int = 300, adverse_second_half: float = 0.0
) -> list[ForwardTrade]:
    """The reviewer's bot: an entry every 30 bars held 10, on a clock unrelated to the plan."""
    out = []
    n = p.decision_bars
    for j in range(warm, warm + n - 10, 30):
        bad = j > warm + n // 2
        adv = adverse_second_half if bad else 0.0
        out.append(
            ForwardTrade(
                pair="BTC/USDT",
                is_open=False,
                open_time=c.index[j] + pd.Timedelta(seconds=5),
                open_rate=float(c.open.iloc[j]) * (1 + adv),
                close_time=c.index[j + 10] + pd.Timedelta(seconds=5),
                close_rate=float(c.open.iloc[j + 10]) * (1 - adv),
                amount=1.0,
                fee_open=0.001,
                fee_close=0.001,
                exit_reason="exit_signal",
            )
        )
    return out


def lab_following_bot(
    lab: list[Trade], c: pd.DataFrame, *, adverse_from: pd.Timestamp | None = None
) -> list[ForwardTrade]:
    """A bot that runs the plan: each lab fill, 5 s after the bar opens, at the open; fills at
    or after ``adverse_from`` are 60 bps worse (entries higher, exits lower)."""

    def adv(ts: pd.Timestamp) -> float:
        return 0.006 if adverse_from is not None and ts >= adverse_from else 0.0

    out = []
    for t in lab:
        closed = t.exit_time is not None
        out.append(
            ForwardTrade(
                pair="BTC/USDT",
                is_open=not closed,
                open_time=t.entry_time + pd.Timedelta(seconds=5),
                open_rate=float(c.loc[t.entry_time, "open"]) * (1 + adv(t.entry_time)),
                close_time=t.exit_time + pd.Timedelta(seconds=5) if closed else None,
                close_rate=(
                    float(c.loc[t.exit_time, "open"]) * (1 - adv(t.exit_time)) if closed else None
                ),
                fee_open=0.001,
                fee_close=0.001 if closed else None,
                exit_reason="exit_signal" if closed else None,
            )
        )
    return out


def run_probe(
    p: PaperProtocol, c: pd.DataFrame, bot: list[ForwardTrade], *, candles: pd.DataFrame
) -> Any:
    """Replay the plan on ``candles`` (what compare would see), observe, evaluate at the
    decision bar."""
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    end = pd.Timestamp(p.decision_end, unit="s", tz="UTC")
    lab = replay_trades(PLAN, candles, start=start, end=end, costs=COSTS)
    slip = modelled_slippage(candles, COSTS)
    obs = observe_paper(p, bot, candles, lab_trades=lab, slippage=slip, asof=end)
    return obs, evaluate(p, obs)


def probe_protocol() -> PaperProtocol:
    return build(synthetic_design(mean=0.0002, days=730), candidate="gap")


def test_probe_gap_fixed_clock_bot_never_passes_on_truncated_candles() -> None:
    """C1 probe_gap: the fixed-clock bot with +60 bps fills in the second half failed on full
    data but PASSED when candles stopped at day 28 (fills on missing candles were skipped)."""
    p = probe_protocol()
    c = probe_candles(p)
    bot = fixed_clock_bot(p, c, adverse_second_half=0.006)
    _, full = run_probe(p, c, bot, candles=c)
    assert full.verdict == "fail"
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    half = c.loc[c.index < start + (pd.Timestamp(p.decision_end, unit="s", tz="UTC") - start) / 2]
    obs, cut = run_probe(p, c, bot, candles=half)
    assert obs.bars_elapsed == p.decision_bars // 2 and obs.fills_missing_candle > 0
    assert cut.verdict != "pass"


def test_probe_gap_rand_fixed_clock_bot_fails_on_timing() -> None:
    """C2 probe_gap_rand: an honest-priced bot trading every 30 bars on a clock unrelated to
    the plan used to PASS ("implementation consistent"); the lab replay match now fails it."""
    p = probe_protocol()
    c = probe_candles(p)
    obs, v = run_probe(p, c, fixed_clock_bot(p, c), candles=c)
    assert obs.n_entries == 11 and obs.lab_entries > 0
    assert obs.matched_entries / max(obs.n_entries, obs.lab_entries) < 0.8
    assert v.verdict == "fail"
    assert any("[implementation difference] timing" in r for r in v.reasons)


# --------------------------------------------------------------------------- design backtest


def synthetic_candles(start: pd.Timestamp, end: pd.Timestamp, *, seed: int) -> pd.DataFrame:
    index = pd.date_range(start, end, freq="4h", inclusive="left", name="time")
    n = len(index)
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    open_ = np.r_[100.0, close[:-1]]
    spread = np.abs(rng.normal(0.0, 0.004, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(5.0, 50.0, n),
            "tradable": np.ones(n, dtype=bool),
        },
        index=index,
    )


# a ~32-month design window: 17 non-overlapping 56-day blocks for the predictive band
DESIGN_START = pd.Timestamp("2023-02-01", tz="UTC")
SPLIT = pd.Timestamp("2025-10-01", tz="UTC")
CANDLES = synthetic_candles(
    pd.Timestamp("2023-01-01", tz="UTC"), pd.Timestamp("2026-03-01", tz="UTC"), seed=9
)


@pytest.fixture
def lab_cfg(tmp_path: Path) -> LabConfig:
    base = load_config()
    splits = replace(base.splits, design_start=DESIGN_START, holdout_start=SPLIT)
    return replace(base, data_dir=tmp_path / "lab", splits=splits)


def test_design_stats_match_the_lab_run_and_never_read_past_the_split(lab_cfg: LabConfig) -> None:
    seen: list[pd.Timestamp] = []

    def loader(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        seen.append(end)
        return CANDLES  # deliberately returns bars after `end` too

    d = design_stats(PLAN, lab_cfg, loader)
    assert seen == [SPLIT]
    assert d.window_start == DESIGN_START and d.window_end == SPLIT
    assert d.index[-1] < SPLIT
    history = CANDLES.loc[CANDLES.index < SPLIT]
    plan = parse_plan(PLAN)
    sig = compile_plan(plan, history)
    mask = history.index >= DESIGN_START
    res = run_signals(
        history.loc[mask],
        sig.entries.loc[mask],
        sig.exits.loc[mask],
        costs=lab_cfg.costs,
        risk=plan.risk,
        init_cash=lab_cfg.init_cash,
        cost_history=history,
    )
    assert np.array_equal(d.returns, res.returns.to_numpy())
    assert list(d.entry_times) == [t.entry_time for t in res.trades]
    closed = [t for t in res.trades if t.exit_time is not None]
    assert d.signal_exit_share == pytest.approx(
        sum(t.exit_reason == "signal" for t in closed) / len(closed)
    )
    # perturbing every bar at/after the split changes nothing
    later = CANDLES.copy()
    later.loc[later.index >= SPLIT, ["open", "high", "low", "close"]] *= 3.0
    again = design_stats(PLAN, lab_cfg, lambda *a: later)
    assert np.array_equal(again.returns, d.returns)
    assert np.array_equal(again.position, d.position)


def test_plan_following_bot_passes_full_data_and_continues_on_truncated_candles(
    lab_cfg: LabConfig,
) -> None:
    """C1 on a protocol from the plan's own design run: a bot that runs the plan passes on full
    candles; the same bot with candles stopping at day 28 is continue ("incomplete data"), never
    pass; with +60 bps fills from day 14 it fails on full data."""
    design = design_stats(PLAN, lab_cfg, lambda *a: CANDLES)
    p = build(
        design,
        claimed=0.4,
        source="walk_forward_oos",
        created_at=datetime(2025, 11, 1, 9, 15, tzinfo=UTC),
    )
    assert p.mode == "implementation_only" and p.inconclusive_reasons == ()
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    end = pd.Timestamp(p.decision_end, unit="s", tz="UTC")
    full = CANDLES.loc[CANDLES.index < end]
    cut = CANDLES.loc[CANDLES.index < start + 28 * DAY]

    def judge(bot: list[ForwardTrade], candles: pd.DataFrame) -> tuple[ObservedPaper, Any]:
        lab = replay_trades(PLAN, candles, start=start, end=end, costs=lab_cfg.costs)
        slip = modelled_slippage(candles, lab_cfg.costs)
        obs = observe_paper(p, bot, candles, lab_trades=lab, slippage=slip, asof=end)
        return obs, evaluate(p, obs)

    lab = replay_trades(PLAN, full, start=start, end=end, costs=lab_cfg.costs)
    honest = lab_following_bot(lab, CANDLES)
    obs, v = judge(honest, full)
    assert v.verdict == "pass", v.headline
    assert obs.matched_entries == obs.n_entries == obs.lab_entries > 0
    obs, v = judge(honest, cut)
    assert v.verdict == "continue" and v.at_decision and not v.final
    k = obs.fills_missing_candle
    assert k > 0
    assert v.reasons == (f"incomplete data: 168 of 336 bars, {k} fills unjudged",)

    broken = lab_following_bot(lab, CANDLES, adverse_from=start + 14 * DAY)
    _, v = judge(broken, full)
    assert v.verdict == "fail" and "gross breakage" in v.headline
    assert any("shortfall: median" in r and "> 5.0 bps" in r for r in v.reasons)


def test_plan_following_bot_broken_late_fails_through_the_fill_window(
    lab_cfg: LabConfig,
) -> None:
    """Round-2 follow-up on the plan's own design protocol: +60 bps on every fill from day 35
    hits 6 of 20 judged fills, so the overall median stays at -5 bps and misses it; the rolling
    window of 3 consecutive judged fills fails it. The honest bot still passes, and its median
    against the bar open is ~0 even though the vol-scaled model makes it look favourable."""
    design = design_stats(PLAN, lab_cfg, lambda *a: CANDLES)
    p = build(
        design,
        claimed=0.4,
        source="walk_forward_oos",
        created_at=datetime(2025, 11, 1, 9, 15, tzinfo=UTC),
    )
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    end = pd.Timestamp(p.decision_end, unit="s", tz="UTC")
    full = CANDLES.loc[CANDLES.index < end]
    lab = replay_trades(PLAN, full, start=start, end=end, costs=lab_cfg.costs)
    slip = modelled_slippage(full, lab_cfg.costs)

    def judge(bot: list[ForwardTrade]) -> tuple[ObservedPaper, Any]:
        obs = observe_paper(p, bot, full, lab_trades=lab, slippage=slip, asof=end)
        return obs, evaluate(p, obs)

    obs, honest = judge(lab_following_bot(lab, CANDLES))
    assert honest.verdict == "pass", honest.headline
    assert min(f.shortfall_bps for f in obs.fills) < -20.0  # vol-scaled model fill
    # shortfall + s_j equals paper-vs-open up to the second-order term s_j^2 x 1e4 (< 0.01 bps)
    assert float(np.median([f.vs_open_bps for f in obs.fills])) == pytest.approx(0.0, abs=0.01)
    obs, broken = judge(lab_following_bot(lab, CANDLES, adverse_from=start + 35 * DAY))
    vals = [f.shortfall_bps for f in obs.fills]
    assert len(vals) == 20 and sum(v > 20.0 for v in vals) == 6
    assert float(np.median(vals)) < p.shortfall.tolerance_bps  # the overall median misses it
    assert broken.verdict == "fail", broken.headline
    fails = [c.name for c in broken.checks if c.status == "fail"]
    assert "shortfall" not in fails and any(n.startswith("shortfall_fills_") for n in fails)
    assert "3 consecutive fills" in broken.headline
    # the union-bound false-fail figure for this protocol, stated upfront
    s = p.shortfall
    assert s.false_fail_gross_early < 0.01
    assert any(f"{s.false_fail_gross_early:.3%}" in r for r in p.rules)


# --------------------------------------------------------------------------- ledger link (W4)


def test_ledger_link_requires_a_design_trial_and_records_it(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with pytest.raises(ValueError, match="not found"):
        read_ledger_link(path, PLAN, cfg=LAB_CFG)
    assert not path.exists()  # a missing ledger is never created
    trial_id = seed_ledger(path, PLAN, wf_sharpes=(0.7, 0.2))
    link = read_ledger_link(path, PLAN, cfg=LAB_CFG)
    assert (link.run_id, link.trial_id) == ("r-test-run", trial_id)
    assert link.wf_oos_sharpes == (0.7, 0.2) and len(link.wf_evidence_ids) == 2
    assert link.window_start == int(pd.Timestamp("2025-01-01", tz="UTC").timestamp())
    other = {**PLAN, "features": {"fast": {"ema": {"period": 9}}, "slow": {"ema": {"period": 30}}}}
    with Ledger(path) as led, pytest.raises(ValueError, match="no design-window trial"):
        ledger_link(led, other, cfg=LAB_CFG)


def test_duplicate_plan_or_logic_under_another_name_is_refused(tmp_path: Path) -> None:
    paper = tmp_path / "paper"
    p = build(synthetic_design(mean=0.0001), candidate="first")
    register_protocol(p, paper_dir=paper, journal_dir=tmp_path / "j")
    with pytest.raises(ValueError, match="'first'") as same_plan:
        check_not_registered_elsewhere(paper, "second", PLAN)
    assert "already registered" in str(same_plan.value)
    retuned = {
        **PLAN,
        "features": {"fast": {"ema": {"period": 9}}, "slow": {"ema": {"period": 40}}},
    }
    assert plan_hash(parse_plan(retuned)) != p.plan_hash
    with pytest.raises(ValueError, match=r"logic .* already registered as paper candidate 'first'"):
        check_not_registered_elsewhere(paper, "second", retuned)
    other_interval = {**PLAN, "interval": "1h"}
    with pytest.raises(ValueError, match="'first'"):
        check_not_registered_elsewhere(paper, "third", other_interval)
    # the same candidate (re-prepare) is allowed; a different logic is allowed
    check_not_registered_elsewhere(paper, "first", PLAN)
    different = {**PLAN, "long_exit": "fast < slow"}
    check_not_registered_elsewhere(paper, "fourth", different)


# --------------------------------------------------------------------------- registration


def test_register_is_exclusive_and_journal_has_no_holdout_numbers(tmp_path: Path) -> None:
    p = impl_only()
    ppath, jpath = register_protocol(p, paper_dir=tmp_path / "paper", journal_dir=tmp_path / "j")
    assert jpath == journal_path(p, tmp_path / "j")
    assert jpath.name == "2026-11-02-btcusdt-paper-ev.md"
    text = jpath.read_text()
    front = parse_frontmatter(text)
    assert front["protocol_sha256"] == protocol_sha256(p)
    assert front["outcome"] == "pending" and front["candidate"] == "ev"
    assert front["plan_hash"] == p.plan_hash and front["date"] == "2026-11-02"
    assert front["logic_hash"] == p.logic_hash
    assert front["run_id"] == LINK.run_id and front["trial_id"] == LINK.trial_id
    assert VERDICTS_HEADING in text and "Sharpe NOT tested" in text
    assert "pass / fail / continue / inconclusive" in text
    guard_holdout(text)  # does not raise
    guard_holdout(text, per_line=True)
    assert HOLDOUT_WORD.search(text) is None
    assert text == render_journal_entry(p)
    with pytest.raises(FileExistsError, match="never overwritten"):
        register_protocol(p, paper_dir=tmp_path / "paper", journal_dir=tmp_path / "j")
    assert json.loads(ppath.read_text()) == protocol_to_dict(p)


def test_load_registered_protocol_refuses_a_changed_protocol(tmp_path: Path) -> None:
    p = impl_only()
    ppath, _ = register_protocol(p, paper_dir=tmp_path / "paper", journal_dir=tmp_path / "j")
    got, _ = load_registered_protocol(tmp_path / "paper", "ev", tmp_path / "j")
    assert got == p
    data = json.loads(ppath.read_text())
    data["shortfall"]["tolerance_bps"] = 50.0
    ppath.write_text(json.dumps(data))
    with pytest.raises(ProtocolChangedError, match="changed after registration"):
        load_registered_protocol(tmp_path / "paper", "ev", tmp_path / "j")


def test_final_verdict_is_write_once(tmp_path: Path) -> None:
    """C1: the first final verdict stands; a later run cannot flip fail to pass."""
    p = impl_only()
    _, jpath = register_protocol(p, paper_dir=tmp_path / "paper", journal_dir=tmp_path / "j")
    cont = evaluate(p, observed(p, asof=p.start + 10 * 86400, entries=1))
    append_verdict(jpath, cont, asof=p.start + 10 * 86400)
    assert recorded_final_verdict(jpath) is None
    fail = evaluate(p, observed(p, asof=p.start + 86400, entries=p.trades.hi + 1))
    append_verdict(jpath, fail, asof=p.start + 86400)
    rec = recorded_final_verdict(jpath)
    assert rec == {
        "outcome": "fail",
        "verdict_asof": datetime.fromtimestamp(p.start + 86400, UTC).isoformat(),
        "verdict_protocol_sha256": protocol_sha256(p),
    }
    before = jpath.read_text()
    passing = evaluate(p, observed(p))
    assert passing.verdict == "pass"
    with pytest.raises(FinalVerdictRecordedError, match="never changed"):
        append_verdict(jpath, passing, asof=p.decision_end)
    assert jpath.read_text() == before
    # the registered sha is still intact, so the protocol still loads
    load_registered_protocol(tmp_path / "paper", "ev", tmp_path / "j")


# --------------------------------------------------------------------------- CLI


class _Clock:
    """Stand-in for ``paper_trade.datetime``: ``now()`` returns a settable instant."""

    at = datetime(2025, 11, 1, 9, 15, tzinfo=UTC)

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return cls.at


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lab_cfg: LabConfig) -> dict[str, Any]:
    import paper_trade

    monkeypatch.setattr(paper_trade, "load_config", lambda: lab_cfg)
    monkeypatch.setattr(paper_trade, "default_loader", lambda cfg: lambda *a: CANDLES)
    monkeypatch.setattr(paper_trade, "datetime", _Clock)
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 1, 9, 15, tzinfo=UTC))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN))
    seed_ledger(lab_cfg.ledger_path, PLAN, wf_sharpes=(0.4,))
    paper, journal = tmp_path / "paper", tmp_path / "journal"
    base = ["--paper-dir", str(paper), "--journal-dir", str(journal)]
    return {
        "mod": paper_trade,
        "plan": plan_file,
        "paper": paper,
        "journal": journal,
        "base": base,
        "ledger": lab_cfg.ledger_path,
    }


def _prepare(
    cli: dict[str, Any], *extra: str, candidate: str = "cand", source: str = "archive"
) -> int:
    """``prepare`` with the archive frozen as the candle source (the monkeypatched loader)."""
    argv = [*cli["base"], "prepare", "--plan-file", str(cli["plan"]), "--candidate", candidate]
    return int(cli["mod"].main([*argv, "--candle-source", source, *extra]))


def test_cli_prepare_registers_before_launch_and_refuses_overwrite(
    cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, bool] = {}

    def fake_launch(prepared: Any) -> LaunchResult:
        seen["protocol"] = (cli["paper"] / "cand" / "protocol.json").is_file()
        seen["journal"] = any(cli["journal"].glob("*-paper-cand.md"))
        return LaunchResult(True, "fake dry-run started")

    monkeypatch.setattr(cli["mod"], "launch", fake_launch)
    assert _prepare(cli, "--launch") == 0
    assert seen == {"protocol": True, "journal": True}
    out = capsys.readouterr().out
    assert "protocol registered" in out and "--dry-run" in out
    (entry,) = cli["journal"].glob("*.md")
    assert entry.name == "2025-11-01-btcusdt-paper-cand.md"
    first = (cli["paper"] / "cand" / "protocol.json").read_text()
    data = json.loads(first)
    assert data["source"]["run_id"] == "r-test-run" and data["source"]["trial_id"] >= 1
    assert data["sharpe"]["claim_source"] == "walk_forward_oos"
    assert data["sharpe"]["claimed_annual"] == pytest.approx(0.4)

    registered = entry.read_text()

    # re-preparing (e.g. to relaunch) keeps the frozen protocol and journal byte for byte,
    # even at a later time when a rebuild would give a different clock
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 9, 9, 15, tzinfo=UTC))
    assert _prepare(cli) == 0
    assert "kept unchanged" in capsys.readouterr().out
    assert (cli["paper"] / "cand" / "protocol.json").read_text() == first
    assert entry.read_text() == registered

    # anything that would change the protocol is refused: never overwritten
    assert _prepare(cli, "--claimed-sharpe", "3.0") == 2
    assert "never overwritten" in capsys.readouterr().err
    other = cli["plan"].with_name("other.json")
    other.write_text(json.dumps({**PLAN, "long_exit": "crosses_below(fast, slow) or fast < 0"}))
    argv = [*cli["base"], "prepare", "--plan-file", str(other), "--candidate", "cand"]
    argv += ["--candle-source", "archive"]
    assert cli["mod"].main(argv) == 2
    assert "plan would change it" in capsys.readouterr().err
    assert (cli["paper"] / "cand" / "protocol.json").read_text() == first
    assert entry.read_text() == registered
    assert len(list(cli["journal"].glob("*.md"))) == 1


def test_cli_prepare_refuses_the_same_plan_under_a_new_name(
    cli: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _prepare(cli) == 0
    capsys.readouterr()
    assert _prepare(cli, candidate="cand2") == 2
    err = capsys.readouterr().err
    assert "refusing to prepare" in err and "'cand'" in err
    assert not (cli["paper"] / "cand2" / "protocol.json").exists()


def test_cli_prepare_refuses_an_unledgered_plan_or_a_missing_claim(
    cli: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "other-ledger.db"
    assert _prepare(cli, "--ledger", str(empty)) == 2
    assert "not found" in capsys.readouterr().err and not empty.exists()
    no_wf = tmp_path / "no-wf.db"
    seed_ledger(no_wf, PLAN, wf_sharpes=())
    assert _prepare(cli, "--ledger", str(no_wf)) == 2
    assert "--claimed-sharpe" in capsys.readouterr().err
    assert not (cli["paper"] / "cand" / "protocol.json").exists()
    # an owner-supplied claim is accepted and labelled
    assert _prepare(cli, "--ledger", str(no_wf), "--claimed-sharpe", "1.1") == 0
    data = json.loads((cli["paper"] / "cand" / "protocol.json").read_text())
    assert data["sharpe"]["claim_source"] == "owner_supplied"
    assert data["sharpe"]["claimed_annual"] == pytest.approx(1.1)


def test_cli_prepare_refuses_when_the_dryrun_db_has_trades(
    cli: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = cli["paper"] / "cand"
    folder.mkdir(parents=True)
    conn = sqlite3.connect(folder / "dryrun.sqlite")
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT)")
    conn.execute("INSERT INTO trades (pair) VALUES ('BTC/USDT')")
    conn.commit()
    conn.close()
    assert _prepare(cli) == 2
    assert "before the first dry-run trade" in capsys.readouterr().err
    assert not (folder / "protocol.json").exists()
    assert not cli["journal"].exists() or not any(cli["journal"].iterdir())


def _ft_date(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").tz_localize(None).strftime("%Y-%m-%d %H:%M:%S.%f")


def test_cli_compare_evaluates_appends_and_keeps_the_first_final_verdict(
    cli: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _prepare(cli) == 0
    protocol, jpath = load_registered_protocol(cli["paper"], "cand", cli["journal"])
    registered = jpath.read_text()
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    conn = sqlite3.connect(cli["paper"] / "cand" / "dryrun.sqlite")
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN, "
        "open_date DATETIME, close_date DATETIME, open_rate FLOAT, close_rate FLOAT, "
        "fee_open FLOAT, fee_close FLOAT, close_profit FLOAT, exit_reason TEXT)"
    )
    bar = start + pd.Timedelta(hours=8)
    o = float(CANDLES.loc[bar, "open"])
    c = float(CANDLES.loc[bar + pd.Timedelta(hours=8), "open"])
    conn.execute(
        "INSERT INTO trades (pair, is_open, open_date, close_date, open_rate, close_rate, "
        "fee_open, fee_close, close_profit, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("BTC/USDT", 0, _ft_date(bar + pd.Timedelta(seconds=3)),
         _ft_date(bar + pd.Timedelta(hours=8, seconds=3)), o, c, 0.001, 0.001, c / o - 1,
         "exit_signal"),
    )  # fmt: skip
    conn.commit()
    conn.close()
    capsys.readouterr()

    # mid-test: continue; the registered section is untouched, one line appended
    _Clock.at = datetime(2025, 11, 20, 0, 0, tzinfo=UTC)
    argv = [*cli["base"], "compare", "--candidate", "cand", "--source", "archive"]
    assert cli["mod"].main(argv) == 0
    out = capsys.readouterr().out
    assert "protocol verdict: continue" in out
    text = jpath.read_text()
    assert text.startswith(registered.rstrip("\n"))
    assert text.count("**continue**") == 1
    assert parse_frontmatter(text)["outcome"] == "pending"
    (cmp_json,) = (cli["paper"] / "cand").glob("comparison-*.json")
    data = json.loads(cmp_json.read_text())
    assert data["protocol_verdict"]["verdict"] == "continue"
    assert data["protocol_verdict"]["protocol_sha256"] == protocol_sha256(protocol)
    assert data["protocol_verdict"]["data_complete"] is True
    assert "Protocol verdict: continue" in cmp_json.with_suffix(".md").read_text()

    # past the decision bar: one arbitrary paper trade against the lab replay's entries ->
    # the timing match fails (a final verdict, written once)
    _Clock.at = datetime(2026, 1, 20, 0, 0, tzinfo=UTC)
    assert cli["mod"].main(argv) == 0
    out = capsys.readouterr().out
    assert "protocol verdict: fail" in out and "timing" in out
    text = jpath.read_text()
    front = parse_frontmatter(text)
    assert front["outcome"] == "fail"
    assert front["protocol_sha256"] == protocol_sha256(protocol)
    assert front["verdict_protocol_sha256"] == protocol_sha256(protocol)
    assert front["verdict_asof"] == datetime.fromtimestamp(protocol.decision_end, UTC).isoformat()
    body = text.partition("\n---\n")[2]
    assert body.startswith(registered.partition("\n---\n")[2].rstrip("\n"))
    guard_holdout(text)

    # a later run prints the recorded verdict and changes nothing
    _Clock.at = datetime(2026, 2, 20, 0, 0, tzinfo=UTC)
    assert cli["mod"].main(argv) == 0
    out = capsys.readouterr().out
    assert "final verdict already recorded: fail" in out
    assert jpath.read_text() == text


def test_cli_compare_refuses_a_changed_protocol(
    cli: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _prepare(cli) == 0
    path = cli["paper"] / "cand" / "protocol.json"
    data = json.loads(path.read_text())
    data["trades"]["hi"] += 5
    path.write_text(json.dumps(data, indent=2))
    capsys.readouterr()
    argv = [*cli["base"], "compare", "--candidate", "cand", "--source", "archive"]
    assert cli["mod"].main(argv) == 2
    assert "protocol changed after registration" in capsys.readouterr().err
    assert not list((cli["paper"] / "cand").glob("comparison-*"))


def test_cli_compare_without_a_protocol_refuses(
    cli: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    from research.paper.freqtrade import prepare_candidate

    prepare_candidate(PLAN, candidate="cand", paper_dir=cli["paper"])
    argv = [*cli["base"], "compare", "--candidate", "cand", "--source", "archive"]
    assert cli["mod"].main(argv) == 2
    assert "run `prepare` first" in capsys.readouterr().err


# --------------------------------------------------------------------------- final review fixes


def _one_trade_db(cli: dict[str, Any], protocol: PaperProtocol) -> None:
    """The dry-run DB with one arbitrary closed trade (the lab replay's entries differ, so the
    verdict at the decision bar is a timing fail)."""
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    conn = sqlite3.connect(cli["paper"] / "cand" / "dryrun.sqlite")
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN, "
        "open_date DATETIME, close_date DATETIME, open_rate FLOAT, close_rate FLOAT, "
        "fee_open FLOAT, fee_close FLOAT, close_profit FLOAT, exit_reason TEXT)"
    )
    bar = start + pd.Timedelta(hours=8)
    o = float(CANDLES.loc[bar, "open"])
    c = float(CANDLES.loc[bar + pd.Timedelta(hours=8), "open"])
    conn.execute(
        "INSERT INTO trades (pair, is_open, open_date, close_date, open_rate, close_rate, "
        "fee_open, fee_close, close_profit, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("BTC/USDT", 0, _ft_date(bar + pd.Timedelta(seconds=3)),
         _ft_date(bar + pd.Timedelta(hours=8, seconds=3)), o, c, 0.001, 0.001, c / o - 1,
         "exit_signal"),
    )  # fmt: skip
    conn.commit()
    conn.close()


def _compare_argv(cli: dict[str, Any]) -> list[str]:
    return [*cli["base"], "compare", "--candidate", "cand", "--source", "archive"]


def test_cli_compare_network_error_or_rate_limit_exits_2_cleanly(
    cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dropped connection, or a 418/429/451/5xx from the public klines host, is a clean
    "cannot compare" (exit 2), never a traceback; nothing is appended or written."""
    import httpx

    import research.paper.compare as cmp_mod

    assert _prepare(cli, source="api") == 0
    protocol, jpath = load_registered_protocol(cli["paper"], "cand", cli["journal"])
    _one_trade_db(cli, protocol)
    registered = jpath.read_text()
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    argv = [*cli["base"], "compare", "--candidate", "cand"]
    real = cmp_mod.public_kline_loader

    def via(handler: Any) -> None:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(
            cli["mod"],
            "public_kline_loader",
            lambda **kw: real(**kw, client=client, sleep=lambda s: None),
        )

    def drop(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    cases: list[tuple[Any, str]] = [
        (drop, "connection reset"),
        (lambda r: httpx.Response(451), "451"),
        (lambda r: httpx.Response(503), "503"),
        (lambda r: httpx.Response(418), "HTTP 418"),
        (lambda r: httpx.Response(429, headers={"Retry-After": "1"}), "HTTP 429"),
    ]
    capsys.readouterr()
    for handler, needle in cases:
        via(handler)
        assert cli["mod"].main(argv) == 2
        err = capsys.readouterr().err
        assert err.startswith("cannot compare: ") and needle in err
        assert "Traceback" not in err
    assert jpath.read_text() == registered
    assert not list((cli["paper"] / "cand").glob("comparison-*"))


def test_cli_compare_locked_dryrun_db_exits_2_cleanly(
    cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """freqtrade writing its DB ("database is locked") is a clean exit 2, never a traceback,
    whether the trades or the trade count hit the lock."""
    assert _prepare(cli) == 0
    protocol, jpath = load_registered_protocol(cli["paper"], "cand", cli["journal"])
    _one_trade_db(cli, protocol)
    registered = jpath.read_text()
    _Clock.at = datetime(2025, 11, 20, 0, 0, tzinfo=UTC)

    def locked(*a: Any, **k: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cli["mod"], "read_dryrun_trades", locked)
    capsys.readouterr()
    assert cli["mod"].main(_compare_argv(cli)) == 2
    err = capsys.readouterr().err
    assert err == "cannot compare: database is locked\n"
    assert jpath.read_text() == registered
    # prepare (re-run) counts the dry-run trades first: the same lock is a clean refusal
    monkeypatch.setattr(cli["mod"], "dryrun_trade_count", locked)
    assert _prepare(cli) == 2
    err = capsys.readouterr().err
    assert err.startswith("refusing to prepare: database is locked")


def test_cli_reprepare_with_corrupt_meta_or_journal_refuses_cleanly(
    cli: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _prepare(cli) == 0
    meta = cli["paper"] / "cand" / "meta.json"
    good = meta.read_text()
    meta.write_text("{not json")
    capsys.readouterr()
    assert _prepare(cli) == 2  # JSONDecodeError from meta.json
    err = capsys.readouterr().err
    assert err.startswith("refusing to prepare: ") and "Traceback" not in err
    meta.write_text(good)
    (entry,) = cli["journal"].glob("*.md")
    entry.write_text("no frontmatter here\n")
    assert _prepare(cli) == 2  # ValueError from parse_frontmatter
    err = capsys.readouterr().err
    assert "refusing to prepare: " in err and "frontmatter" in err


def test_cli_compare_write_failure_after_the_verdict_then_recreates_only_the_missing_file(
    cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The final verdict line is in the journal but the comparison write failed: compare says
    so (exit 2). A later compare recreates ONLY the file the journal line names, with the same
    verdict, and never touches the journal; once it exists nothing more is written."""
    assert _prepare(cli) == 0
    protocol, jpath = load_registered_protocol(cli["paper"], "cand", cli["journal"])
    _one_trade_db(cli, protocol)
    folder = cli["paper"] / "cand"
    real_write = cli["mod"].write_comparison

    def disk_full(*a: Any, **kw: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli["mod"], "write_comparison", disk_full)
    _Clock.at = datetime(2026, 1, 20, 0, 0, tzinfo=UTC)
    capsys.readouterr()
    assert cli["mod"].main(_compare_argv(cli)) == 2
    cap = capsys.readouterr()
    assert "verdict recorded; comparison not written: " in cap.err
    assert "No space left on device" in cap.err
    assert "protocol verdict: fail" in cap.out
    assert recorded_final_verdict(jpath) is not None
    name = proto_mod.recorded_final_comparison(jpath)
    assert name == "comparison-2026-01-20.json"
    assert not list(folder.glob("comparison-*"))
    journal = jpath.read_text()

    # a later compare (another day): the verdict is kept, only the named file is recreated
    monkeypatch.setattr(cli["mod"], "write_comparison", real_write)
    _Clock.at = datetime(2026, 2, 20, 0, 0, tzinfo=UTC)
    assert cli["mod"].main(_compare_argv(cli)) == 0
    out = capsys.readouterr().out
    assert "final verdict already recorded: fail" in out and "recreating only that" in out
    assert jpath.read_text() == journal
    assert sorted(p.name for p in folder.glob("comparison-*")) == [
        "comparison-2026-01-20.json",
        "comparison-2026-01-20.md",
    ]
    data = json.loads((folder / name).read_text())
    assert data["protocol_verdict"]["verdict"] == "fail"
    assert data["protocol_verdict"]["protocol_sha256"] == protocol_sha256(protocol)
    assert any(n.startswith("Recreated ") for n in data["notes"])
    assert not list(folder.glob(".comparison-*"))  # no temporary file left behind

    # only the .md missing: only the .md is written, the JSON is never overwritten
    json_before = (folder / name).read_text()
    (folder / name).with_suffix(".md").unlink()
    assert cli["mod"].main(_compare_argv(cli)) == 0
    assert (folder / name).read_text() == json_before
    assert (folder / name).with_suffix(".md").is_file()

    # nothing missing: prints the verdict, writes nothing, changes nothing
    capsys.readouterr()
    assert cli["mod"].main(_compare_argv(cli)) == 0
    out = capsys.readouterr().out
    assert "final verdict already recorded: fail" in out and "written:" not in out
    assert jpath.read_text() == journal


def test_cli_compare_never_recreates_a_comparison_that_contradicts_the_journal(
    cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _prepare(cli) == 0
    protocol, jpath = load_registered_protocol(cli["paper"], "cand", cli["journal"])
    _one_trade_db(cli, protocol)
    monkeypatch.setattr(
        cli["mod"], "write_comparison", lambda *a, **k: (_ for _ in ()).throw(OSError("eio"))
    )
    _Clock.at = datetime(2026, 1, 20, 0, 0, tzinfo=UTC)
    assert cli["mod"].main(_compare_argv(cli)) == 2
    journal = jpath.read_text()
    real_eval = cli["mod"].evaluate
    monkeypatch.setattr(
        cli["mod"], "evaluate", lambda p, o: replace(real_eval(p, o), verdict="pass")
    )
    capsys.readouterr()
    assert cli["mod"].main(_compare_argv(cli)) == 2
    err = capsys.readouterr().err
    assert "cannot recreate the comparison" in err and "nothing written" in err
    assert not list((cli["paper"] / "cand").glob("comparison-*"))
    assert jpath.read_text() == journal


def test_recorded_final_comparison_reads_only_the_final_line(tmp_path: Path) -> None:
    p = impl_only()
    _, jpath = register_protocol(p, paper_dir=tmp_path / "paper", journal_dir=tmp_path / "j")
    cont = evaluate(p, observed(p, asof=p.start + 10 * 86400, entries=1))
    append_verdict(jpath, cont, asof=p.start + 10 * 86400, comparison="comparison-2026-01-01.json")
    assert proto_mod.recorded_final_comparison(jpath) is None  # not final yet
    fail = evaluate(p, observed(p, asof=p.start + 86400, entries=p.trades.hi + 1))
    append_verdict(jpath, fail, asof=p.start + 86400, comparison="comparison-2026-01-08.json")
    assert proto_mod.recorded_final_comparison(jpath) == "comparison-2026-01-08.json"
    _, j2 = register_protocol(
        replace(p, candidate="ev2"), paper_dir=tmp_path / "paper", journal_dir=tmp_path / "j2"
    )
    append_verdict(j2, fail, asof=p.start + 86400)  # names no file
    assert proto_mod.recorded_final_comparison(j2) is None


# --------------------------------------------------------------------------- round 3 (review r2)
# The reviewer's p13r2/common.py scenarios: a plan-consistent design (the EMA plan run by the lab
# on a seeded 730-day 4h random walk) and bots derived from the lab replay on fresh candles.


def rw(index: pd.DatetimeIndex, seed: int, vol: float = 0.01) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 60000 * np.exp(np.cumsum(rng.normal(0, vol, len(index))))
    opens = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, close) * 1.002,
            "low": np.minimum(opens, close) * 0.998,
            "close": close,
            "volume": 1.0,
            "tradable": True,
        },
        index=index,
    )


def r2_design(
    seed: int = 100, days: int = 730, plan_raw: dict[str, Any] | None = None
) -> DesignStats:
    idx = pd.date_range("2024-01-01", periods=days * 6, freq="4h", tz="UTC", name="time")
    c = rw(idx, seed)
    plan = parse_plan(plan_raw or PLAN)
    sig = compile_plan(plan, c)
    r = run_signals(
        c, sig.entries, sig.exits, costs=COSTS, risk=plan.risk, init_cash=10000.0, cost_history=c
    )
    closed = [t for t in r.trades if t.exit_time is not None]
    return DesignStats(
        interval="4h",
        index=idx,
        returns=r.returns.to_numpy(float),
        position=r.position.to_numpy(float),
        entry_times=pd.DatetimeIndex([t.entry_time for t in r.trades]),
        window_start=idx[0],
        window_end=idx[-1] + STEP4H,
        signal_exit_share=sum(t.exit_reason == "signal" for t in closed) / len(closed),
    )


R2 = build(r2_design(), candidate="r2", claimed=0.5)


def r2_candles(p: PaperProtocol, seed: int) -> pd.DataFrame:
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    idx = pd.date_range(
        start - 300 * STEP4H, periods=300 + p.decision_bars, freq="4h", tz="UTC", name="time"
    )
    return rw(idx, seed)


def r2_bot(
    lab: list[Trade],
    c: pd.DataFrame,
    end: pd.Timestamp,
    *,
    entry_shift: int = 0,
    exit_shift: int = 0,
    exit_reason: str = "exit_signal",
    only_even: bool = False,
    every: int | None = None,
    entry_adv: float = 0.0,
    exit_adv: float = 0.0,
) -> list[ForwardTrade]:
    """common.mk: each lab trade, optionally shifted by whole bars / re-tagged / mispriced.
    ``every=k`` shifts only trades 0, k, 2k, ... (p13r3/frac.py's ``k % n == 0``)."""
    out = []
    for k, t in enumerate(lab):
        on = (not only_even or k % 2 == 0) and (every is None or k % every == 0)
        eb = t.entry_time + (entry_shift if on else 0) * STEP4H
        closed = t.exit_time is not None
        xb = t.exit_time + (exit_shift if on else 0) * STEP4H if closed else None
        if eb not in c.index:
            continue
        if closed and (xb not in c.index or xb >= end):
            closed, xb = False, None
        out.append(
            ForwardTrade(
                pair="BTC/USDT",
                is_open=not closed,
                open_time=eb + pd.Timedelta(seconds=5),
                open_rate=float(c.loc[eb, "open"]) * (1 + entry_adv),
                close_time=xb + pd.Timedelta(seconds=5) if xb is not None else None,
                close_rate=float(c.loc[xb, "open"]) * (1 - exit_adv) if xb is not None else None,
                fee_open=0.001,
                fee_close=0.001 if closed else None,
                exit_reason=exit_reason if closed else None,
            )
        )
    return out


def r2_run(
    p: PaperProtocol,
    c: pd.DataFrame,
    bot: list[ForwardTrade],
    *,
    candles: pd.DataFrame | None = None,
    until: pd.Timestamp | None = None,
) -> tuple[ObservedPaper, Any]:
    candles = c if candles is None else candles
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    end = pd.Timestamp(p.decision_end, unit="s", tz="UTC")
    lab = replay_trades(PLAN, candles, start=start, end=end, costs=COSTS)
    slip = modelled_slippage(candles, COSTS)
    obs = observe_paper(p, bot, candles, lab_trades=lab, slippage=slip, asof=until or end)
    return obs, evaluate(p, obs)


def r2_lab(p: PaperProtocol, c: pd.DataFrame) -> tuple[list[Trade], pd.Timestamp]:
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    end = pd.Timestamp(p.decision_end, unit="s", tz="UTC")
    return replay_trades(PLAN, c, start=start, end=end, costs=COSTS), end


@pytest.mark.parametrize(
    ("case", "kw", "check"),
    [
        ("late+1", {"entry_shift": 1, "exit_shift": 1}, "irregular_fills"),
        ("early-1", {"entry_shift": -1, "exit_shift": -1}, "irregular_fills"),
        ("alt_late", {"entry_shift": 1, "exit_shift": 1, "only_even": True}, "irregular_fills"),
        ("exit+3", {"exit_shift": 3}, "irregular_fills"),
        ("exit-3", {"exit_shift": -3}, "irregular_fills"),
        ("exit+6", {"exit_shift": 6}, "irregular_fills"),
        ("exits_roi", {"exit_reason": "roi"}, "irregular_fills"),
        ("exit+3_roi", {"exit_shift": 3, "exit_reason": "roi"}, "irregular_fills"),
        ("exit-2_roi", {"exit_shift": -2, "exit_reason": "roi"}, "irregular_fills"),
    ],
)
def test_r2_timing_manipulations_fail_and_the_honest_bot_passes(
    case: str, kw: dict[str, Any], check: str
) -> None:
    """C-A / W-B (probe_lag2): exits 3-6 bars off, every exit tagged ROI, or every fill one bar
    late or early passed about as often as an honest bot (e.g. exit+3 52/60, exits_roi 54/60,
    early-1 48/60 pass). Now each fails on exit matching or the lag-0 share; the honest bot on
    the same candles passes."""
    c = r2_candles(R2, seed=1)
    lab, end = r2_lab(R2, c)
    _, honest = r2_run(R2, c, r2_bot(lab, c, end))
    assert honest.verdict == "pass", honest.headline
    _, v = r2_run(R2, c, r2_bot(lab, c, end, **kw))
    assert v.verdict == "fail", (case, v.headline)
    assert check in {ch.name for ch in v.checks if ch.status == "fail"}, (case, v.headline)
    assert "implementation" in v.fail_kinds
    if case == "early-1":
        assert any(proto_mod.EARLY_LABEL in r for r in v.reasons)
    if case in ("late+1", "alt_late"):
        assert any(proto_mod.LATE_LABEL in r for r in v.reasons)


def test_r2_fills_better_than_the_open_fail_as_possible_look_ahead() -> None:
    """W-B two-sided: on-time fills 40 bps better than the bar open (as if filled at a better,
    earlier price) are implausibly favourable."""
    c = r2_candles(R2, seed=1)
    lab, end = r2_lab(R2, c)
    _, v = r2_run(R2, c, r2_bot(lab, c, end, entry_adv=-0.004, exit_adv=-0.004))
    assert v.verdict == "fail"
    (reason,) = [r for r in v.reasons if "shortfall" in r]
    assert "implausibly favourable" in reason and "look-ahead" in reason


def test_r2_one_missing_bar_no_longer_hides_a_fail_forever() -> None:
    """W-A (probe_gap_stuck): +12 bps fills fail on full data; one permanently missing bar
    that holds no fill used to turn that into continue forever. The shortfall rule does not
    depend on bars without fills, so it fails anyway."""
    c = r2_candles(R2, seed=1)
    lab, end = r2_lab(R2, c)
    bot = r2_bot(lab, c, end, entry_adv=0.0012, exit_adv=0.0012)
    _, full = r2_run(R2, c, bot)
    assert full.verdict == "fail"
    start = pd.Timestamp(R2.start, unit="s", tz="UTC")
    fill_bars = {t.open_time.floor("4h") for t in bot} | {
        t.close_time.floor("4h") for t in bot if t.close_time is not None
    }
    gap = next(b for b in c.index[c.index >= start + (end - start) / 2] if b not in fill_bars)
    obs, v = r2_run(R2, c, bot, candles=c.drop(index=gap))
    assert not obs.complete and obs.bars_missing == 1
    assert v.verdict == "fail" and "whatever the missing data hold" in v.headline
    assert any("shortfall: median" in r for r in v.reasons)


def test_r2_missing_bar_on_an_honest_run_continues_then_is_inconclusive_after_grace() -> None:
    """W-A: an honest bot with one permanently missing bar is continue at the decision bar and
    a final inconclusive ("data") data_grace_days later."""
    c = r2_candles(R2, seed=1)
    lab, end = r2_lab(R2, c)
    bot = r2_bot(lab, c, end)
    start = pd.Timestamp(R2.start, unit="s", tz="UTC")
    fill_bars = {t.open_time.floor("4h") for t in bot} | {
        t.close_time.floor("4h") for t in bot if t.close_time is not None
    }
    gap = next(b for b in c.index[c.index >= start + (end - start) / 2] if b not in fill_bars)
    holed = c.drop(index=gap)
    _, v = r2_run(R2, c, bot, candles=holed)
    assert v.verdict == "continue" and v.at_decision and not v.final
    _, late = r2_run(R2, c, bot, candles=holed, until=end + 7 * DAY - STEP4H)
    assert late.verdict == "continue"
    _, done = r2_run(R2, c, bot, candles=holed, until=end + 7 * DAY)
    assert done.verdict == "inconclusive" and done.final
    assert done.reasons[0].startswith("data: incomplete data: 335 of 336 bars")


def test_exposure_is_bounded_over_missing_bars() -> None:
    """W-A: with 40 bars missing, exposure lies in [held/N, (held + 40)/N]; it fails only when
    that whole range is outside the band."""
    p = impl_only()
    n = p.decision_bars
    hi_held = math.ceil((p.exposure.hi + 0.05) * n)

    def obs(held: int) -> ObservedPaper:
        base = observed(p, bars_present=n - 40)
        return replace(base, bars_in_market=held)

    assert evaluate(p, obs(0)).verdict == "fail"  # (0 + 40)/N is below the band
    assert evaluate(p, obs(hi_held)).verdict == "fail"  # held/N alone is above it
    inside = round(p.exposure.lo * n)  # the range straddles the band: undecided
    assert evaluate(p, obs(inside)).verdict == "continue"


def test_complete_checks_every_expected_bar_time_not_just_counts() -> None:
    """Quant nit: a candle at an off-grid time cannot stand in for a missing bar."""
    p = impl_only()
    t0 = pd.Timestamp(p.start, unit="s", tz="UTC")
    candles = flat_candles(t0, 30, STEP4H)
    shifted = candles.rename(index={t0 + 5 * STEP4H: t0 + 5 * STEP4H + pd.Timedelta(hours=1)})
    slip = modelled_slippage(shifted, COSTS)
    obs = observe_paper(p, [], shifted, lab_trades=[], slippage=slip, asof=t0 + 30 * STEP4H)
    assert len(shifted.loc[shifted.index < t0 + 30 * STEP4H]) == 30
    assert obs.bars_expected == 30 and obs.bars_elapsed == 29 and not obs.complete


def test_median_bounds_by_hand() -> None:
    assert median_bounds([1.0, 2.0, 3.0], 0) == (2.0, 2.0)
    lo, hi = median_bounds([1.0, 2.0, 3.0], 2)  # 5 values: -inf -inf 1 2 3 / 1 2 3 inf inf
    assert (lo, hi) == (1.0, 3.0)
    assert median_bounds([], 1) == (-math.inf, math.inf)


def test_predictive_band_by_hand() -> None:
    vals = np.array([2.0, 4.0, 6.0])
    lo, hi = predictive_band(vals, 3, 0.95)
    half = sp_stats.t.ppf(0.975, 2) * 2.0 * math.sqrt(1 + 1 / 3)
    assert (lo, hi) == (pytest.approx(4.0 - half), pytest.approx(4.0 + half))
    with pytest.raises(ValueError, match="independent"):
        predictive_band(vals, 1, 0.95)


def test_bootstrap_estimate_is_frozen_seeded_and_printed() -> None:
    """W-C: the leave-overlap-out bootstrap is reproducible from the seed, its components are
    consistent with the total, and the rules print it."""
    again = build(r2_design(), candidate="r2", claimed=0.5)
    b = R2.bootstrap
    assert again.bootstrap == b and (b.draws, b.seed) == (2000, 20261028)
    parts = (b.entries_rate, b.exposure_rate, b.shortfall_rate, b.sharpe_rate)
    assert max(parts) <= b.fail_rate <= min(1.0, sum(parts))
    assert any(f"single-path estimate {b.fail_rate:.1%} (can be off" in r for r in R2.rules)
    other = build(r2_design(), candidate="r2", claimed=0.5, cfg=config(bootstrap_seed=7))
    assert other.bootstrap.seed == 7


def test_fail_reasons_say_regime_or_implementation() -> None:
    """W-C: entries below the band with every matched fill on time is a regime difference; the
    same with late fills is not attributed to the market alone."""
    p = impl_only()
    regime = evaluate(p, observed(p, entries=p.trades.lo - 1))
    assert regime.fail_kinds == ("regime",)
    assert regime.reasons[0].startswith("[regime difference (entries/exposure outside")
    mixed = evaluate(p, observed(p, entries=p.trades.lo - 1, late=1))
    assert mixed.reasons[0].startswith("[regime or implementation difference")
    assert "implementation" in mixed.fail_kinds


def test_claim_note_names_the_family_minimum_difference() -> None:
    _, _, note = resolve_claim(LINK, None)
    assert "behaviour family" in note and "this plan's own" in note


def test_ledger_link_refuses_a_checked_logic_and_a_non_research_trial(tmp_path: Path) -> None:
    """W-D: a dead hypothesis (its locked-split check did not pass, or its outcome is not
    recorded) is never paper-registered; one whose check passed is the normal candidate path
    (CLAUDE.md: holdout, then paper trading). A design trial outside a research run does not
    count."""
    h, lh = plan_hash(parse_plan(PLAN)), logic_hash(PLAN)

    def checked(name: str, passed: bool | None) -> Path:
        path = tmp_path / f"{name}.db"
        seed_ledger(path, PLAN)
        with Ledger(path) as led:
            led.record_holdout_check("r-test-run", h, budget=1, logic_hash=lh)
            if passed is not None:
                payload = json.dumps({"passed": passed, "logic_hash": lh}, sort_keys=True)
                led.record_evidence("r-test-run", h, "holdout", payload)
        return path

    for name, passed in (("failed", False), ("unrecorded", None)):
        with pytest.raises(ValueError, match="dead hypothesis"):
            read_ledger_link(checked(name, passed), PLAN, cfg=LAB_CFG)
    link = read_ledger_link(checked("passed", True), PLAN, cfg=LAB_CFG)
    assert link.run_id == "r-test-run"
    manual = tmp_path / "manual.db"
    p = parse_plan(PLAN)
    with Ledger(manual) as led:
        led.record(
            TrialRecord(
                run_id="web-ui",
                source="manual",
                status="ok",
                plan_hash=plan_hash(p),
                plan_json=json.dumps(PLAN),
                params_json="{}",
                symbol=p.symbol,
                interval=p.interval,
                window_kind="design",
                window_start=pd.Timestamp("2025-01-01", tz="UTC"),
                window_end=pd.Timestamp("2025-10-01", tz="UTC"),
                metrics_json="{}",
                sharpe_per_bar=0.01,
                n_trades=4,
                returns=np.zeros(16),
            )
        )
    with pytest.raises(ValueError, match="research run"):
        read_ledger_link(manual, PLAN, cfg=LAB_CFG)


def test_read_ledger_link_is_strictly_read_only(tmp_path: Path) -> None:
    """Code review W2: Ledger.open_readonly: the file is untouched and an older schema is
    refused (no migration)."""
    path = tmp_path / "ledger.db"
    seed_ledger(path, PLAN)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    read_ledger_link(path, PLAN, cfg=LAB_CFG)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT, at TEXT)")
    conn.execute("INSERT INTO schema_version VALUES (3, 'research_runs', 'x')")
    conn.commit()
    conn.close()
    old_bytes = old.read_bytes()
    with pytest.raises(ValueError, match="needs >= 4"):
        read_ledger_link(old, PLAN, cfg=LAB_CFG)
    assert old.read_bytes() == old_bytes


def test_journal_entries_and_slugs_block_re_registration(tmp_path: Path) -> None:
    """W-D: deleting paper/<old> cannot free a plan (the journal frontmatter still names it);
    a candidate whose journal slug collides with another is refused before any write."""
    paper, journal = tmp_path / "paper", tmp_path / "j"
    p = build(synthetic_design(mean=0.0001), candidate="foo_bar")
    register_protocol(p, paper_dir=paper, journal_dir=journal)
    (paper / "foo_bar" / "protocol.json").unlink()
    (paper / "foo_bar").rmdir()
    with pytest.raises(ValueError, match=r"'foo_bar'.*\.md"):
        check_not_registered_elsewhere(paper, "again", PLAN, journal_dir=journal)
    different = {**PLAN, "long_exit": "fast < slow"}
    with pytest.raises(ValueError, match="same journal name"):
        check_not_registered_elsewhere(paper, "foo-bar", different, journal_dir=journal)
    check_not_registered_elsewhere(paper, "other", different, journal_dir=journal)


def test_complete_registration_after_a_crash_between_the_two_writes(tmp_path: Path) -> None:
    p = impl_only()
    paper, journal = tmp_path / "paper", tmp_path / "j"
    _, jpath = register_protocol(p, paper_dir=paper, journal_dir=journal)
    text = jpath.read_text()
    jpath.unlink()  # as if the process died after protocol.json
    assert complete_registration(paper, "ev", journal) == jpath
    assert jpath.read_text() == text
    assert complete_registration(paper, "ev", journal) is None
    load_registered_protocol(paper, "ev", journal)


def test_append_verdict_holds_an_exclusive_lock(tmp_path: Path) -> None:
    """Code review W3: read-check-write is under flock; a second writer waits."""
    import fcntl

    p = impl_only()
    _, jpath = register_protocol(p, paper_dir=tmp_path / "paper", journal_dir=tmp_path / "j")
    v = evaluate(p, observed(p, asof=p.start + 86400, entries=1))
    lock = jpath.with_name(f".{jpath.name}.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    done = threading.Event()
    worker = threading.Thread(
        target=lambda: (append_verdict(jpath, v, asof=p.start + 86400), done.set())
    )
    worker.start()
    time.sleep(0.3)
    assert not done.is_set()  # blocked on the lock
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    worker.join(5)
    assert done.is_set() and "**continue**" in jpath.read_text()
    assert not [x for x in jpath.parent.iterdir() if x.name.endswith(".tmp")]


def test_cli_prepare_writes_protocol_before_freqtrade_files_and_recovers(
    cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Code-review nits: protocol + journal first; a failure writing the freqtrade files leaves
    a registration that `prepare` of the same name completes."""
    real = cli["mod"].prepare_candidate
    seen: list[bool] = []

    def failing(*a: Any, **k: Any) -> Any:
        seen.append((cli["paper"] / "cand" / "protocol.json").is_file())
        raise ValueError("disk full")

    monkeypatch.setattr(cli["mod"], "prepare_candidate", failing)
    assert _prepare(cli) == 2
    assert seen == [True]
    err = capsys.readouterr().err
    assert "re-run `prepare --candidate cand`" in err
    monkeypatch.setattr(cli["mod"], "prepare_candidate", real)
    assert _prepare(cli) == 0
    assert "kept unchanged" in capsys.readouterr().out
    assert (cli["paper"] / "cand" / "config.json").is_file()


def test_cli_prepare_completes_a_registration_missing_its_journal(
    cli: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _prepare(cli) == 0
    (entry,) = cli["journal"].glob("*.md")
    entry.unlink()
    capsys.readouterr()
    assert _prepare(cli) == 0
    assert "completed an interrupted registration" in capsys.readouterr().out
    assert entry.is_file()
    # once the bot has traded, the registration cannot be completed after the fact
    entry.unlink()
    conn = sqlite3.connect(cli["paper"] / "cand" / "dryrun.sqlite")
    conn.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, pair TEXT)")
    conn.execute("INSERT INTO trades (pair) VALUES ('BTC/USDT')")
    conn.commit()
    conn.close()
    assert _prepare(cli) == 2
    assert "register under a new candidate name" in capsys.readouterr().err
    assert not entry.exists()


def _empty_trades_db(cli: dict[str, Any]) -> None:
    conn = sqlite3.connect(cli["paper"] / "cand" / "dryrun.sqlite")
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN, "
        "open_date DATETIME, close_date DATETIME, open_rate FLOAT, close_rate FLOAT)"
    )
    conn.commit()
    conn.close()


def test_cli_compare_uses_the_frozen_source_and_warm_up(
    cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """W-E: a different --source or host is refused; the replay sees exactly the frozen
    warm-up bars before the start."""
    assert _prepare(cli) == 0
    _empty_trades_db(cli)
    protocol, _ = load_registered_protocol(cli["paper"], "cand", cli["journal"])
    capsys.readouterr()
    argv = [*cli["base"], "compare", "--candidate", "cand"]
    _Clock.at = datetime(2025, 11, 20, 0, 0, tzinfo=UTC)
    assert cli["mod"].main([*argv, "--source", "api"]) == 2
    assert "froze the candle source 'archive'" in capsys.readouterr().err
    assert cli["mod"].main([*argv, "--public-api-host", "api.example.com"]) == 2
    assert "froze the candle host" in capsys.readouterr().err
    seen: list[pd.Timestamp] = []
    real = cli["mod"].backtest_same_period

    def spy(plan: Any, *, start: Any, end: Any, cfg: Any, loader: Any, costs: Any) -> Any:
        seen.append(loader(None, None, end).index[0])
        return real(plan, start=start, end=end, cfg=cfg, loader=loader, costs=costs)

    monkeypatch.setattr(cli["mod"], "backtest_same_period", spy)
    assert cli["mod"].main(argv) == 0
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    assert seen == [start - protocol.data.warmup_bars * pd.Timedelta(hours=4)]


def test_cli_compare_writes_the_comparison_only_after_the_verdict_line(
    cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Code review W3 + nit: a failing journal append leaves no comparison file, and errors
    are reported cleanly (ValueError -> exit 2, a recorded final verdict -> exit 0)."""
    assert _prepare(cli) == 0
    _empty_trades_db(cli)
    _Clock.at = datetime(2025, 11, 20, 0, 0, tzinfo=UTC)
    argv = [*cli["base"], "compare", "--candidate", "cand"]

    def boom(*a: Any, **k: Any) -> str:
        raise ValueError("journal is read-only")

    monkeypatch.setattr(cli["mod"], "append_verdict", boom)
    capsys.readouterr()
    assert cli["mod"].main(argv) == 2
    assert "cannot compare: journal is read-only" in capsys.readouterr().err
    assert not list((cli["paper"] / "cand").glob("comparison-*"))

    def recorded(*a: Any, **k: Any) -> str:
        raise FinalVerdictRecordedError("x.md already records the final verdict 'fail'")

    monkeypatch.setattr(cli["mod"], "append_verdict", recorded)
    assert cli["mod"].main(argv) == 0
    assert "final verdict already recorded" in capsys.readouterr().out
    assert not list((cli["paper"] / "cand").glob("comparison-*"))


# --------------------------------------------------------------------------- round 4 (review r3)
# The reviewer's p13r3 probes (frac.py, stops.py, band.py) as regression tests.


def _timing_fails(v: Any) -> set[str]:
    return {c.name for c in v.checks if c.status == "fail" and c.name.startswith("irregular")}


def _over(obs: ObservedPaper) -> bool:
    """More irregular fills than the protocol allows for this observation."""
    return obs.irregular > proto_mod.irregular_allowance(R2.matching, obs.total_fills)


def test_r5_irregular_allowance_rounds_down() -> None:
    """C-new: max(1, floor(0.05 x all fills)); rounding up would excuse a skipped trade (2
    irregular fills) at 21-40 fills (far.py: drop1of6 62/100 pass with ceil, 7/100 with
    floor)."""
    m = R2.matching
    assert (m.tolerance_bars, m.irregular_share) == (1, 0.05)
    got = [proto_mod.irregular_allowance(m, n) for n in (0, 1, 20, 24, 39, 40, 59, 60, 80)]
    assert got == [1, 1, 1, 1, 1, 2, 2, 3, 4]


def test_r5_one_irregular_fill_among_twenty_honest_fills_passes() -> None:
    """C-new: the one allowance covers a rare warm-up near-tie; a second irregular fill at 20
    fills fails, and the pass headline prints the count and its breakdown."""
    honest = evaluate(R2, observed(R2, entries=5))
    assert honest.verdict == "pass", honest.headline
    assert "every fill matched at lag 0 (0 irregular of 20 fills)" in honest.headline
    one = evaluate(R2, observed(R2, entries=5, late=1))  # 20 fills, 1 late entry
    assert one.verdict == "pass", one.headline
    assert "1 irregular of 20 fills, within the 1 allowed" in one.headline
    assert "unmatched entries 0 paper / 0 lab, unmatched exits 0 paper / 0 lab" in one.headline
    assert "off the lab's fill bar 1 entries / 0 exits (1 late, 0 early)" in one.headline
    assert "every fill matched" not in one.headline
    skip = {"lab_entries": 6, "lab_exits": 6}  # a trade the bot skipped: its entry and exit
    for kw in ({"late": 2}, {"early": 1, "exit_early": 1}, skip):
        v = evaluate(R2, observed(R2, entries=5, **kw))
        assert v.verdict == "fail" and _timing_fails(v) == {"irregular_fills"}, kw
    late2 = evaluate(R2, observed(R2, entries=5, late=2))
    assert any(proto_mod.LATE_LABEL in r for r in late2.reasons)
    early = evaluate(R2, observed(R2, entries=5, early=1, exit_early=1))
    assert any(proto_mod.EARLY_LABEL in r for r in early.reasons)
    skipped = evaluate(R2, observed(R2, entries=5, **skip))
    assert any(proto_mod.UNMATCHED_LAB_LABEL in r for r in skipped.reasons)


def test_r5_shifting_a_trade_further_never_helps() -> None:
    """C-new (one.py): on seed 1 trade 0 shifted 1 bar failed but 2 bars passed with a false
    "(lag 0)" headline (entry 6/7, exit 5/6). Now a 2-bar shift costs 4 irregular fills
    (unmatched on both sides) against 2 for a 1-bar shift, so it fails whenever 1 bar does."""
    for seed in range(1, 21):
        c = r2_candles(R2, seed=seed)
        lab, end = r2_lab(R2, c)
        o1, v1 = r2_run(R2, c, r2_bot(lab, c, end, entry_shift=1, exit_shift=1, every=10**6))
        o2, v2 = r2_run(R2, c, r2_bot(lab, c, end, entry_shift=2, exit_shift=2, every=10**6))
        assert o2.irregular >= o1.irregular
        if v1.verdict == "fail":
            assert v2.verdict == "fail", (seed, v2.headline)
        if seed == 1:
            assert v1.verdict == v2.verdict == "fail"
            assert o2.unmatched_paper_entries == o2.unmatched_lab_entries == 1
            assert "lag 0" not in v2.headline and "irregular_fills" in _timing_fails(v2)


@pytest.mark.parametrize("shift", [2, 3])
def test_r5_one_trade_in_six_shifted_two_or_three_bars_fails(shift: int) -> None:
    """C-new (far.py late2_1of6 / late3_1of6: 27/100 passed before; 0/100 now)."""
    for seed in range(1, 21):
        c = r2_candles(R2, seed=seed)
        lab, end = r2_lab(R2, c)
        _, v = r2_run(R2, c, r2_bot(lab, c, end, entry_shift=shift, exit_shift=shift, every=6))
        assert v.verdict == "fail", (seed, v.headline)
        assert "irregular_fills" in _timing_fails(v)
        assert any(proto_mod.UNMATCHED_PAPER_LABEL in r for r in v.reasons)


def test_r5_one_in_five_entries_two_bars_late_fails() -> None:
    """C-new (far.py entrylate2_1of5: 19/100 passed before; 0/100 now)."""
    for seed in range(1, 21):
        c = r2_candles(R2, seed=seed)
        lab, end = r2_lab(R2, c)
        _, v = r2_run(R2, c, r2_bot(lab, c, end, entry_shift=2, every=5))
        assert v.verdict == "fail" and "irregular_fills" in _timing_fails(v), seed


@pytest.mark.parametrize("every", [6, 5])
def test_r5_skipping_one_trade_in_six_or_five_fails(every: int) -> None:
    """C-new (far.py drop1of6 77/100, drop1of5 65/100 passed before; 7/100 each now). Every
    run where a trade was skipped and the count exceeds the allowance fails; the few passes
    left skipped nothing (three or fewer trades) or have 40+ fills (2 allowed)."""
    failed = 0
    for seed in range(1, 21):
        c = r2_candles(R2, seed=seed)
        lab, end = r2_lab(R2, c)
        kept = [t for k, t in enumerate(lab) if k % every != 3]
        obs, v = r2_run(R2, c, r2_bot(kept, c, end))
        if len(kept) < len(lab):
            assert obs.unmatched_lab_entries >= 1
        if _over(obs):
            assert v.verdict == "fail" and "irregular_fills" in _timing_fails(v), seed
            assert any(proto_mod.UNMATCHED_LAB_LABEL in r for r in v.reasons)
            failed += 1
    assert failed >= 17


def test_r5_an_extra_paper_trade_fails() -> None:
    """C-new: one trade the lab never made (2 unmatched paper fills) fails."""
    c = r2_candles(R2, seed=1)
    lab, end = r2_lab(R2, c)
    honest = r2_bot(lab, c, end)
    _, ok = r2_run(R2, c, honest)
    assert ok.verdict == "pass", ok.headline
    busy = {t.entry_time for t in lab} | {t.exit_time for t in lab if t.exit_time is not None}
    start = pd.Timestamp(R2.start, unit="s", tz="UTC")
    bar = next(
        b
        for b in c.index[c.index >= start + 10 * STEP4H]
        if all(abs((b - x) / STEP4H) > 3 for x in busy)
        and all(not (t.entry_time <= b <= (t.exit_time or end)) for t in lab)
    )
    extra = ForwardTrade(
        pair="BTC/USDT",
        is_open=False,
        open_time=bar + pd.Timedelta(seconds=5),
        open_rate=float(c.loc[bar, "open"]),
        close_time=bar + 2 * STEP4H + pd.Timedelta(seconds=5),
        close_rate=float(c.loc[bar + 2 * STEP4H, "open"]),
        fee_open=0.001,
        fee_close=0.001,
        exit_reason="exit_signal",
    )
    obs, v = r2_run(R2, c, sorted([*honest, extra], key=lambda t: t.open_time))
    assert (obs.unmatched_paper_entries, obs.unmatched_paper_exits) == (1, 1)
    assert v.verdict == "fail" and "irregular_fills" in _timing_fails(v), v.headline
    assert any(proto_mod.UNMATCHED_PAPER_LABEL in r for r in v.reasons)


def test_r3_entries_late_on_one_trade_in_three_fail() -> None:
    """C1 (frac.py entrylate1of3: 56/100 passed before; 3/100 now)."""
    failed = 0
    for seed in range(1, 21):
        c = r2_candles(R2, seed=seed)
        lab, end = r2_lab(R2, c)
        obs, v = r2_run(R2, c, r2_bot(lab, c, end, entry_shift=1, every=3))
        assert obs.entry_early == 0
        if _over(obs):
            assert v.verdict == "fail" and "irregular_fills" in _timing_fails(v), seed
            assert any(proto_mod.LATE_LABEL in r for r in v.reasons)
            failed += 1
    assert failed >= 17


@pytest.mark.parametrize("shift", [1, -1])
def test_r3_one_trade_in_six_late_or_early_fails(shift: int) -> None:
    """C1 (frac.py late1of6 / early1of6: 27/100 passed before; 0/100 now)."""
    for seed in range(1, 21):
        c = r2_candles(R2, seed=seed)
        lab, end = r2_lab(R2, c)
        _, v = r2_run(R2, c, r2_bot(lab, c, end, entry_shift=shift, exit_shift=shift, every=6))
        assert v.verdict == "fail" and "irregular_fills" in _timing_fails(v), (seed, v.headline)
        label = proto_mod.LATE_LABEL if shift > 0 else proto_mod.EARLY_LABEL
        assert any(label in r for r in v.reasons)


STOP_PLAN: dict[str, Any] = {**PLAN, "risk": {**PLAN["risk"], "stop_loss_pct": 2.0}}
R3_STOPS = build(r2_design(plan_raw=STOP_PLAN), candidate="r3s", claimed=0.5, plan=STOP_PLAN)


def _stop_bot(lab: list[Trade], c: pd.DataFrame, end: pd.Timestamp, late: bool) -> Any:
    """stops.py mk2: freqtrade reasons; ``late`` fills every stop exit one bar late."""
    reasons = {"signal": "exit_signal", "stop_loss": "stop_loss", "take_profit": "roi"}
    out = []
    for t in lab:
        closed = t.exit_time is not None and t.exit_time < end
        xb = t.exit_time if closed else None
        reason = reasons.get(t.exit_reason, "other")
        if closed and late and reason == "stop_loss":
            assert xb is not None
            xb = xb + STEP4H
            if xb >= end:
                closed, xb = False, None
        out.append(
            ForwardTrade(
                pair="BTC/USDT",
                is_open=not closed,
                open_time=t.entry_time + pd.Timedelta(seconds=5),
                open_rate=float(c.loc[t.entry_time, "open"]),
                close_time=xb + pd.Timedelta(seconds=5) if xb is not None else None,
                close_rate=float(c.loc[xb, "open"]) if xb is not None else None,
                fee_open=0.001,
                fee_close=0.001 if closed else None,
                exit_reason=reason if closed else None,
            )
        )
    return out


def test_r3_stop_exits_one_bar_late_fail() -> None:
    """C1 (stops.py stop_at_open_next: 20/60 passed before; 7/60 now, the runs with at most
    one stop). Stops filled a bar late beyond the allowance fail; the honest bot on the same
    candles has no irregular fill."""
    start = pd.Timestamp(R3_STOPS.start, unit="s", tz="UTC")
    end = pd.Timestamp(R3_STOPS.decision_end, unit="s", tz="UTC")
    failed = 0
    for seed in range(1, 21):
        c = r2_candles(R3_STOPS, seed=seed)
        lab = replay_trades(STOP_PLAN, c, start=start, end=end, costs=COSTS)
        slip = modelled_slippage(c, COSTS)
        for late in (False, True):
            bot = _stop_bot(lab, c, end, late)
            obs = observe_paper(R3_STOPS, bot, c, lab_trades=lab, slippage=slip, asof=end)
            v = evaluate(R3_STOPS, obs)
            if not late:
                assert obs.irregular == 0 and not _timing_fails(v)
            elif obs.exit_late >= 2:
                assert v.verdict == "fail" and "irregular_fills" in _timing_fails(v), seed
                assert any(proto_mod.LATE_LABEL in r for r in v.reasons)
                failed += 1
    assert failed >= 10


def test_r3_honest_bot_never_has_an_irregular_fill() -> None:
    """C-new: the honest bot (frac.py / far.py honest: 94/100 pass before and after) is exact,
    so the timing rule never fails it; its other checks decide as before."""
    passes = 0
    for seed in range(1, 21):
        c = r2_candles(R2, seed=seed)
        lab, end = r2_lab(R2, c)
        obs, v = r2_run(R2, c, r2_bot(lab, c, end))
        assert obs.irregular == 0 and not _timing_fails(v)
        passes += v.verdict == "pass"
        if v.verdict == "pass":
            assert "every fill matched at lag 0" in v.headline
    assert passes >= 18


def test_r3_false_fail_estimate_is_never_printed_as_a_bare_rate() -> None:
    """W1: the bootstrap figure is a single-path estimate, off in either direction (band.py:
    1.8% vs a true 10%; 5.9% vs 0.8%), printed with the simulated range as a caveat."""
    text = render_journal_entry(R2)
    figure = f"{R2.bootstrap.fail_rate:.1%}"
    assert "false-fail rate" not in text.lower() and "floor, not" not in text
    head = text.partition("### Decision rules")[0]
    lines = [ln for ln in head.splitlines() if "false-fail" in ln.lower()]
    assert len(lines) == 1
    line = lines[0]
    assert f"single-path estimate {figure} (can be off in either direction" in line
    assert "excludes regime change" in line and "~0%-10%" in line
    assert "neither a floor nor a bound" in line
    assert any(f"single-path estimate {figure}" in r and "~0%-10%" in r for r in R2.rules)
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "so it is a floor" not in readme and "single-path estimate" in readme


# ----- W2: the lab's behaviour family decides what is the same hypothesis

TWIN: dict[str, Any] = {**PLAN, "long_exit": "crosses_below(fast, slow) or fast < 0"}  # no-op
OTHER: dict[str, Any] = {**PLAN, "long_entry": "fast > slow", "long_exit": "fast < slow"}


def family_ledger(path: Path, plans: list[tuple[dict[str, Any], np.ndarray]]) -> None:
    """One research run holding a design-window trial (same window) per plan, with returns."""
    with Ledger(path) as led:
        led.start_research_run("r-fam", label="t", config_json="{}", config_sha256="0" * 64)
        for raw, rets in plans:
            p = parse_plan(raw)
            led.record(
                TrialRecord(
                    run_id="r-fam",
                    source="api",
                    status="ok",
                    plan_hash=plan_hash(p),
                    plan_json=json.dumps(raw),
                    params_json="{}",
                    symbol=p.symbol,
                    interval=p.interval,
                    window_kind="design",
                    window_start=pd.Timestamp("2025-01-01", tz="UTC"),
                    window_end=pd.Timestamp("2025-10-01", tz="UTC"),
                    metrics_json="{}",
                    sharpe_per_bar=0.01,
                    n_trades=4,
                    returns=rets,
                )
            )
            led.record_evidence(
                "r-fam", plan_hash(p), "walk_forward", json.dumps({"oos_metrics": {"sharpe": 0.4}})
            )


def _check(path: Path, raw: dict[str, Any], passed: bool) -> None:
    h, lh = plan_hash(parse_plan(raw)), logic_hash(raw)
    with Ledger(path) as led:
        led.record_holdout_check("r-fam", h, budget=5, logic_hash=lh)
        payload = json.dumps({"passed": passed, "logic_hash": lh}, sort_keys=True)
        led.record_evidence("r-fam", h, "holdout", payload)


RETS = np.random.default_rng(11).normal(0.0, 0.01, 400)
UNRELATED = np.random.default_rng(12).normal(0.0, 0.01, 400)


def test_r3_no_op_twin_of_a_failed_plan_is_a_dead_hypothesis(tmp_path: Path) -> None:
    """W2: the twin has its own logic hash (the old exact-logic check let it through) but
    trades identically, so the lab's family makes it the failed hypothesis."""
    assert logic_hash(TWIN) != logic_hash(PLAN)
    path = tmp_path / "ledger.db"
    family_ledger(path, [(PLAN, RETS), (TWIN, RETS.copy()), (OTHER, UNRELATED)])
    _check(path, PLAN, passed=False)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="dead hypothesis") as err:
        read_ledger_link(path, TWIN, cfg=LAB_CFG)
    assert "same behaviour family" in str(err.value)
    with pytest.raises(ValueError, match="dead hypothesis"):
        read_ledger_link(path, PLAN, cfg=LAB_CFG)
    # an unrelated logic (returns not correlated) is not that hypothesis
    other = read_ledger_link(path, OTHER, cfg=LAB_CFG)
    assert other.locked_split_check == "unchecked"
    assert logic_hash(PLAN) not in other.family_logic_hashes
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before  # read-only


def _own_cfg(tmp_path: Path) -> LabConfig:
    """A lab config whose own ledger is ``tmp_path / "ledger.db"``."""
    return replace(LAB_CFG, data_dir=tmp_path)


def test_r5_locked_split_states_are_read_per_plan(tmp_path: Path) -> None:
    """W-new-1: "passed" only for the plan whose OWN check passed (with no other check in its
    family); a family member checked instead (the lab marks this variant's row FAIL: it can
    never be checked) is family_checked_not_this_plan; none checked is unchecked."""
    cfg = _own_cfg(tmp_path)
    path = cfg.ledger_path
    family_ledger(path, [(PLAN, RETS), (TWIN, RETS.copy())])
    unchecked = read_ledger_link(path, TWIN, cfg=cfg)
    assert unchecked.locked_split_check == "unchecked"
    assert set(unchecked.family_logic_hashes) == {logic_hash(PLAN), logic_hash(TWIN)}
    _check(path, PLAN, passed=True)
    assert read_ledger_link(path, PLAN, cfg=cfg).locked_split_check == "passed"
    twin = read_ledger_link(path, TWIN, cfg=cfg)
    assert twin.locked_split_check == "family_checked_not_this_plan"


UNC: dict[str, Any] = {  # same features (two EMAs), other logic, on 1h: not comparable here
    **PLAN,
    "interval": "1h",
    "long_entry": "crosses_above(fast, slow) and fast > slow",
}


def _with_uncertain(path: Path, outcome: bool) -> None:
    p = parse_plan(UNC)
    with Ledger(path) as led:
        led.record(
            TrialRecord(
                run_id="r-fam",
                source="api",
                status="ok",
                plan_hash=plan_hash(p),
                plan_json=json.dumps(UNC),
                params_json="{}",
                symbol=p.symbol,
                interval=p.interval,
                window_kind="design",
                window_start=pd.Timestamp("2025-02-01", tz="UTC"),
                window_end=pd.Timestamp("2025-03-01", tz="UTC"),
                metrics_json="{}",
                sharpe_per_bar=0.01,
                n_trades=4,
                returns=np.random.default_rng(13).normal(0.0, 0.01, 672),
            )
        )
    _check(path, UNC, passed=outcome)


def _no_candles(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
    raise ValueError(f"no {interval} candles in this test")


def test_r5_an_uncertain_member_that_failed_is_not_ignored(tmp_path: Path) -> None:
    """W-new-1: family_evidence never sees an uncomparable same-feature plan; its failed check
    now refuses the plan even though the plan's own check passed (fail closed), and a passed
    one makes it family_checked_not_this_plan, never "passed"."""
    cfg = _own_cfg(tmp_path)
    path = cfg.ledger_path
    family_ledger(path, [(PLAN, RETS)])
    _check(path, PLAN, passed=True)
    _with_uncertain(path, outcome=False)
    with pytest.raises(ValueError, match="dead hypothesis"):
        read_ledger_link(path, PLAN, cfg=cfg, loader=_no_candles)
    ok = tmp_path / "ok"
    ok.mkdir()
    cfg2 = _own_cfg(ok)
    family_ledger(cfg2.ledger_path, [(PLAN, RETS)])
    _check(cfg2.ledger_path, PLAN, passed=True)
    _with_uncertain(cfg2.ledger_path, outcome=True)
    link = read_ledger_link(cfg2.ledger_path, PLAN, cfg=cfg2, loader=_no_candles)
    assert logic_hash(UNC) in link.family_uncertain
    assert link.locked_split_check == "family_checked_not_this_plan"


def test_r5_a_non_default_ledger_never_gives_passed(tmp_path: Path) -> None:
    """W-new-2: --ledger may point anywhere; only the lab's own ledger can say "passed". The
    resolved path, whether it is the lab's, and the run's chain head are frozen."""
    cfg = _own_cfg(tmp_path)
    family_ledger(cfg.ledger_path, [(PLAN, RETS)])
    _check(cfg.ledger_path, PLAN, passed=True)
    own = read_ledger_link(cfg.ledger_path, PLAN, cfg=cfg)
    assert own.locked_split_check == "passed" and own.ledger_is_default
    assert own.ledger_path == str(cfg.ledger_path.resolve())
    with Ledger(cfg.ledger_path) as led:
        head = led.chain_head("r-fam")
    assert head is not None and own.chain_head == head
    copy = tmp_path / "elsewhere.db"
    copy.write_bytes(cfg.ledger_path.read_bytes())
    other = read_ledger_link(copy, PLAN, cfg=cfg)
    assert other.locked_split_check == "passed_other_ledger" and not other.ledger_is_default
    p = build_protocol(
        candidate="ev",
        plan=PLAN,
        design=r2_design(),
        costs=COSTS,
        config=config(),
        created_at=CREATED,
        link=other,
        claimed_sharpe_annual=0.5,
        claim_source="owner_supplied",
        claim_note="test claim",
    )
    assert (p.source.ledger_path, p.source.ledger_is_default) == (str(copy.resolve()), False)
    assert p.source.chain_head == head
    text = render_journal_entry(p)
    assert "NOT the lab ledger" in text and "PASSED OTHER LEDGER" in text
    v = evaluate(p, observed(p, entries=5))
    assert v.verdict == "pass" and "NOT a success candidate" in v.headline
    assert "not the lab's own ledger" in v.headline


def test_r3_twin_of_an_already_registered_plan_is_refused(tmp_path: Path) -> None:
    """W2: PLAN is registered for paper trading; its no-op twin (another plan and logic hash)
    is the same hypothesis by the lab's family and is refused under a new name."""
    path, paper, journal = tmp_path / "ledger.db", tmp_path / "paper", tmp_path / "j"
    family_ledger(path, [(PLAN, RETS), (TWIN, RETS.copy()), (OTHER, UNRELATED)])
    register_protocol(build(synthetic_design(mean=0.0001), candidate="first"),
                      paper_dir=paper, journal_dir=journal)  # fmt: skip
    check_not_registered_elsewhere(paper, "second", TWIN, journal_dir=journal)  # hashes differ
    link = read_ledger_link(path, TWIN, cfg=LAB_CFG)
    with pytest.raises(ValueError, match=r"'first'.*behaviour family"):
        check_not_registered_elsewhere(paper, "second", TWIN, journal_dir=journal, family=link)
    other = read_ledger_link(path, OTHER, cfg=LAB_CFG)
    check_not_registered_elsewhere(paper, "third", OTHER, journal_dir=journal, family=other)


@pytest.mark.parametrize("status", proto_mod.LOCKED_SPLIT_STATES)
def test_r3_locked_split_status_is_frozen_and_worded(tmp_path: Path, status: str) -> None:
    """W2: frozen in protocol.json and the journal frontmatter; the pass headline says NOT a
    success candidate unless the check passed. The journal never names the split's other
    word (the leak guard)."""
    p = build_protocol(
        candidate="ev",
        plan=PLAN,
        design=r2_design(),
        costs=COSTS,
        config=config(),
        created_at=CREATED,
        link=replace(LINK, locked_split_check=status),  # type: ignore[arg-type]
        claimed_sharpe_annual=0.5,
        claim_source="owner_supplied",
        claim_note="test claim",
    )
    assert p.locked_split_check == status and p.version == proto_mod.PROTOCOL_VERSION == 7
    ppath, jpath = register_protocol(p, paper_dir=tmp_path / "paper", journal_dir=tmp_path / "j")
    assert json.loads(ppath.read_text())["locked_split_check"] == status
    text = jpath.read_text()
    assert parse_frontmatter(text)["locked_split_check"] == status
    assert HOLDOUT_WORD.search(text) is None
    guard_holdout(text)
    v = evaluate(p, observed(p, entries=10))
    assert v.verdict == "pass", v.headline
    guard_holdout(v.headline)
    if status == "passed":
        assert "NOT a success candidate" not in v.headline
        assert "locked-split check of this plan passed" in v.headline
        assert any("PASSED for this plan itself" in r for r in p.rules)
    else:
        assert v.headline.startswith(
            "pass (implementation consistent, but NOT a success candidate ("
            + proto_mod.LOCKED_SPLIT_WHY[status]
        )
        assert any(status.replace("_", " ").upper() in r for r in p.rules)
        assert "NOT a success candidate" in text
    with pytest.raises(ValueError, match="must be one of"):
        build_protocol(
            candidate="ev",
            plan=PLAN,
            design=r2_design(),
            costs=COSTS,
            config=config(),
            created_at=CREATED,
            link=replace(LINK, locked_split_check="maybe"),  # type: ignore[arg-type]
            claimed_sharpe_annual=0.5,
            claim_source="owner_supplied",
            claim_note="test claim",
        )


def test_r3_cli_prepare_records_unchecked_in_protocol_and_journal(
    cli: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _prepare(cli) == 0
    data = json.loads((cli["paper"] / "cand" / "protocol.json").read_text())
    assert data["locked_split_check"] == "unchecked" and data["version"] == 7
    assert data["source"]["ledger_is_default"] is True
    assert data["source"]["ledger_path"] == str(cli["ledger"].resolve())
    (entry,) = cli["journal"].glob("*.md")
    assert parse_frontmatter(entry.read_text())["locked_split_check"] == "unchecked"


def test_r5_cli_prepare_with_another_ledger_freezes_it(cli: dict[str, Any], tmp_path: Path) -> None:
    """W-new-2 end to end: --ledger elsewhere is recorded as NOT the lab ledger."""
    copy = tmp_path / "copy.db"
    copy.write_bytes(Path(cli["ledger"]).read_bytes())
    assert _prepare(cli, "--ledger", str(copy)) == 0
    data = json.loads((cli["paper"] / "cand" / "protocol.json").read_text())
    assert data["source"]["ledger_path"] == str(copy.resolve())
    assert data["source"]["ledger_is_default"] is False
    (entry,) = cli["journal"].glob("*.md")
    assert "NOT the lab ledger" in entry.read_text()


def test_r5_missing_data_or_a_locked_ledger_exit_2_cleanly(
    cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Code review: DataUnavailableError (no candles on disk) and sqlite3.Error in prepare's
    step 2, and DataUnavailableError in compare, are a clean exit 2, never a traceback."""
    from lab.data.derivatives_columns import DataUnavailableError

    def no_data(*a: Any, **k: Any) -> Any:
        raise DataUnavailableError("no BTCUSDT 4h candles: run the archive downloader")

    def locked(*a: Any, **k: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    capsys.readouterr()
    for name, fn, needle in (
        ("design_stats", no_data, "archive downloader"),
        ("read_ledger_link", locked, "database is locked"),
    ):
        with monkeypatch.context() as m:
            m.setattr(cli["mod"], name, fn)
            assert _prepare(cli) == 2
        err = capsys.readouterr().err
        assert err.startswith("cannot build the paper-trade protocol: ") and needle in err
        assert not (cli["paper"] / "cand" / "protocol.json").exists()
    assert _prepare(cli) == 0
    protocol, _ = load_registered_protocol(cli["paper"], "cand", cli["journal"])
    _one_trade_db(cli, protocol)
    _Clock.at = datetime(2025, 11, 20, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(cli["mod"], "default_loader", lambda cfg: no_data)
    capsys.readouterr()
    assert cli["mod"].main(_compare_argv(cli)) == 2
    err = capsys.readouterr().err
    assert err.startswith("cannot compare: ") and "archive downloader" in err


# --------------------------------------------------------------------------- round 6 (r5 review)
# The reviewer's r5q/warm2.py: freqtrade holds ~1000 candles, so a strategy computing on all of
# them started its EMAs ~1000 bars back while the lab replay starts warmup_bars back; honest bots
# then disagreed near crossings (7/200 timing fails per plan). WARM_FROM fixes the history.

WARM_PLANS: list[tuple[int, int]] = [(3, 100), (5, 150), (50, 200)]


def warm_plan(fast: int, slow: int) -> dict[str, Any]:
    return {
        **PLAN,
        "features": {"fast": {"ema": {"period": fast}}, "slow": {"ema": {"period": slow}}},
        "long_entry": "crosses_above(fast, slow)",
        "long_exit": "crosses_below(close, fast)",
    }


@pytest.fixture
def stub_freqtrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A minimal ``freqtrade.strategy.IStrategy`` in sys.modules (freqtrade is not installed)."""

    class IStrategy:
        def __init__(self, config: dict[str, Any] | None = None) -> None:
            self.config = config or {}

    pkg = types.ModuleType("freqtrade")
    strategy = types.ModuleType("freqtrade.strategy")
    strategy.IStrategy = IStrategy  # type: ignore[attr-defined]
    pkg.strategy = strategy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "freqtrade", pkg)
    monkeypatch.setitem(sys.modules, "freqtrade.strategy", strategy)


def _rendered_signals(source: str, c: pd.DataFrame) -> pd.DataFrame:
    """Run a rendered strategy's populate_indicators on freqtrade's frame of ``c``."""
    ns: dict[str, Any] = {"__name__": "rendered"}
    exec(compile(source, "<rendered>", "exec"), ns)
    cls = next(v for k, v in ns.items() if k.startswith("TvPaper"))
    frame = c.reset_index().rename(columns={"time": "date"})
    out = cls().populate_indicators(frame[["date", "open", "high", "low", "close", "volume"]], {})
    return pd.DataFrame(
        {"entry": out["lab_entry"].to_numpy(), "exit": out["lab_exit"].to_numpy()}, index=c.index
    )


@pytest.mark.parametrize(("fast", "slow"), WARM_PLANS)
def test_r6_strategy_sliced_at_warm_from_matches_the_lab_replay(
    stub_freqtrade: None, fast: int, slow: int
) -> None:
    """On 1000 bars of freqtrade-like history, the rendered strategy (sliced at WARM_FROM)
    gives exactly the lab's signals on the frozen history, and the bot those signals make has
    0 irregular fills (warm2.py: 7/200 honest timing fails per plan before, 0/200 after)."""
    from research.paper.freqtrade import render_strategy

    plan = warm_plan(fast, slow)
    p = build(r2_design(plan_raw=plan), candidate=f"w{fast}", claimed=0.5, plan=plan)
    step = pd.Timedelta(seconds=4 * 3600)
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    end = pd.Timestamp(p.decision_end, unit="s", tz="UTC")
    warm = pd.Timestamp(p.data.warm_from, unit="s", tz="UTC")
    assert p.data.warm_from == p.start - p.data.warmup_bars * 4 * 3600
    source = render_strategy(plan, class_name="TvPaperWarm", warm_from=p.data.warm_from)
    assert f"\nWARM_FROM = {p.data.warm_from}\n" in source
    for seed in (*range(1, 7), 33, 42):
        idx = pd.date_range(
            start - 1000 * step, periods=1000 + p.decision_bars, freq="4h", tz="UTC", name="time"
        )
        c = rw(idx, seed)
        frozen = c.loc[c.index >= warm]
        bot_sig = _rendered_signals(source, c)
        assert not bot_sig.loc[c.index < warm].to_numpy().any()  # no signal before WARM_FROM
        lab_sig = compile_plan(parse_plan(plan), frozen)
        assert (bot_sig.loc[frozen.index, "entry"].to_numpy() == lab_sig.entries.to_numpy()).all()
        assert (bot_sig.loc[frozen.index, "exit"].to_numpy() == lab_sig.exits.to_numpy()).all()
        bot = r2_bot(replay_trades(plan, frozen, start=start, end=end, costs=COSTS), c, end)
        lab = replay_trades(plan, frozen, start=start, end=end, costs=COSTS)
        obs = observe_paper(
            p, bot, frozen, lab_trades=lab, slippage=modelled_slippage(frozen, COSTS), asof=end
        )
        assert obs.irregular == 0, (fast, slow, seed, obs.irregular_detail())


def test_r6_an_unsliced_bot_shows_the_warm_up_mismatch() -> None:
    """The test bites: computing on all 1000 bars (the old strategy) disagrees with the lab
    replay near a crossing (warm2.py seeds 33 and 42 of ema 3/100: "2 irregular ... the bot
    traded where the lab did not")."""
    plan = warm_plan(3, 100)
    p = build(r2_design(plan_raw=plan), candidate="w3", claimed=0.5, plan=plan)
    step = pd.Timedelta(seconds=4 * 3600)
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    end = pd.Timestamp(p.decision_end, unit="s", tz="UTC")
    warm = pd.Timestamp(p.data.warm_from, unit="s", tz="UTC")
    bites = 0
    for seed in (33, 42):
        idx = pd.date_range(
            start - 1000 * step, periods=1000 + p.decision_bars, freq="4h", tz="UTC", name="time"
        )
        c = rw(idx, seed)
        frozen = c.loc[c.index >= warm]
        unsliced = r2_bot(replay_trades(plan, c, start=start, end=end, costs=COSTS), c, end)
        lab = replay_trades(plan, frozen, start=start, end=end, costs=COSTS)
        obs = observe_paper(
            p, unsliced, frozen, lab_trades=lab, slippage=modelled_slippage(frozen, COSTS), asof=end
        )
        v = evaluate(p, obs)
        if obs.irregular > 0:
            bites += 1
            assert v.verdict == "fail" and "irregular_fills" in _timing_fails(v)
    assert bites >= 1


def test_r6_compare_refuses_a_strategy_with_another_warm_from(
    cli: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """WARM_FROM is frozen in protocol.json (data.warm_from) and rendered into the strategy by
    prepare after registration; compare refuses a strategy file computing from another bar."""
    from research.paper.freqtrade import class_name_for, strategy_warm_from

    assert _prepare(cli) == 0
    protocol, _ = load_registered_protocol(cli["paper"], "cand", cli["journal"])
    data = json.loads((cli["paper"] / "cand" / "protocol.json").read_text())
    assert data["data"]["warm_from"] == protocol.start - protocol.data.warmup_bars * 4 * 3600
    strat = cli["paper"] / "cand" / f"{class_name_for('cand')}.py"
    source = strat.read_text()
    assert strategy_warm_from(source) == data["data"]["warm_from"]
    _one_trade_db(cli, protocol)
    _Clock.at = datetime(2025, 11, 20, 0, 0, tzinfo=UTC)
    strat.write_text(source.replace(f"WARM_FROM = {data['data']['warm_from']}", "WARM_FROM = None"))
    capsys.readouterr()
    assert cli["mod"].main(_compare_argv(cli)) == 2
    assert "WARM_FROM None" in capsys.readouterr().err
    strat.write_text(source.replace("\nWARM_FROM = ", "\n# WARM_FROM = "))
    assert cli["mod"].main(_compare_argv(cli)) == 2
    assert "no single `WARM_FROM" in capsys.readouterr().err
    strat.write_text(source)
    assert cli["mod"].main(_compare_argv(cli)) == 0
    assert "protocol verdict: continue" in capsys.readouterr().out


# --------------------------------------------------------------------------- round 7 (START)
# The reviewer's r5q/warm3.py: `prepare --launch` starts the bot before `start`, and the lab
# replay (replay_trades keeps rows >= start) ignores signals before it. An ungated strategy can
# enter at `start` on a pre-start signal or hold a pre-start entry into it: 2 irregular fills,
# a false fail (3/200 ema 3/100, 4/200 ema 5/150). START gates enter_long on date >= start.

FT_CANDLE_LIMIT = 1000  # Binance ohlcv_candle_limit: freqtrade's first fetch


def _freqtrade_bot(
    source: str, c: pd.DataFrame, *, start: pd.Timestamp, end: pd.Timestamp, launch_bars: int,
    gated: bool,
) -> list[ForwardTrade]:  # fmt: skip
    """Run a rendered strategy bar by bar as freqtrade's dry-run does: launched
    ``launch_bars`` before ``start``; at each new closed candle t the dataframe is the candles
    up to t (at most candle limit + startup), the signal is the last row's, the market fill is
    at t+1's open. ``gated=False`` reads the raw lab_entry (the strategy before START)."""
    ns: dict[str, Any] = {"__name__": "rendered"}
    exec(compile(source, "<rendered>", "exec"), ns)
    strat = next(v for k, v in ns.items() if k.startswith("TvPaper"))()
    hold = FT_CANDLE_LIMIT + strat.startup_candle_count
    frame = c.reset_index().rename(columns={"time": "date"})
    frame = frame[["date", "open", "high", "low", "close", "volume"]]
    out: list[ForwardTrade] = []
    pos: pd.Timestamp | None = None
    first = c.index.get_loc(start - launch_bars * STEP4H)
    last = c.index.get_loc(end - STEP4H)
    for i in range(first, last):
        df = frame.iloc[max(0, i + 1 - hold) : i + 1].reset_index(drop=True)
        df = strat.populate_exit_trend(
            strat.populate_entry_trend(strat.populate_indicators(df, {}), {}), {}
        )
        row = df.iloc[-1]
        enter = bool(row["enter_long"]) if gated else bool(row["lab_entry"])
        nxt = c.index[i + 1]
        if pos is None and enter:
            pos = nxt
        elif pos is not None and bool(row["exit_long"]):
            out.append(
                ForwardTrade(
                    pair="BTC/USDT",
                    is_open=False,
                    open_time=pos + pd.Timedelta(seconds=5),
                    open_rate=float(c.loc[pos, "open"]),
                    close_time=nxt + pd.Timedelta(seconds=5),
                    close_rate=float(c.loc[nxt, "open"]),
                    fee_open=0.001,
                    fee_close=0.001,
                    exit_reason="exit_signal",
                )
            )
            pos = None
    if pos is not None:
        out.append(
            ForwardTrade(
                pair="BTC/USDT",
                is_open=True,
                open_time=pos + pd.Timedelta(seconds=5),
                open_rate=float(c.loc[pos, "open"]),
                close_time=None,
                close_rate=None,
                fee_open=0.001,
                fee_close=None,
                exit_reason=None,
            )
        )
    return out


@pytest.mark.parametrize(("fast", "slow", "seeds"), [(3, 100, (64, 65)), (5, 150, (19, 158))])
def test_r7_bot_launched_before_start_matches_the_replay(
    stub_freqtrade: None, fast: int, slow: int, seeds: tuple[int, ...]
) -> None:
    """The rendered strategy with START, run bar by bar from 6 bars before the start on the
    warm3 seeds that failed, has 0 irregular fills; the same strategy read without the gate
    (the old enter_long) mismatches on at least one of them, so the test bites."""
    from research.paper.freqtrade import render_strategy

    plan = warm_plan(fast, slow)
    p = build(r2_design(plan_raw=plan), candidate=f"s{fast}", claimed=0.5, plan=plan)
    start = pd.Timestamp(p.start, unit="s", tz="UTC")
    end = pd.Timestamp(p.decision_end, unit="s", tz="UTC")
    warm = pd.Timestamp(p.data.warm_from, unit="s", tz="UTC")
    source = render_strategy(
        plan, class_name="TvPaperStart", warm_from=p.data.warm_from, start=p.start
    )
    assert f"\nSTART = {p.start}\n" in source
    bites = 0
    for seed in seeds:
        idx = pd.date_range(
            start - 1000 * STEP4H, periods=1000 + p.decision_bars, freq="4h", tz="UTC", name="time"
        )
        c = rw(idx, seed)
        frozen = c.loc[c.index >= warm]
        lab = replay_trades(plan, frozen, start=start, end=end, costs=COSTS)
        slip = modelled_slippage(frozen, COSTS)
        for gated in (True, False):
            bot = _freqtrade_bot(source, c, start=start, end=end, launch_bars=6, gated=gated)
            obs = observe_paper(p, bot, frozen, lab_trades=lab, slippage=slip, asof=end)
            if gated:
                assert all(t.open_time >= start for t in bot)
                assert obs.irregular == 0, (fast, slow, seed, obs.irregular_detail())
            elif obs.irregular > 0:
                bites += 1
                assert evaluate(p, obs).verdict == "fail"
    assert bites >= 1


def test_r7_compare_refuses_a_strategy_with_another_start(
    cli: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """START is the protocol's frozen start, rendered by prepare (and recorded in meta.json);
    compare refuses a strategy whose START line is different, None, missing or duplicated."""
    from research.paper.freqtrade import class_name_for, strategy_frozen_value

    assert _prepare(cli) == 0
    protocol, _ = load_registered_protocol(cli["paper"], "cand", cli["journal"])
    folder = cli["paper"] / "cand"
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["start"] == protocol.start and meta["warm_from"] == protocol.data.warm_from
    strat = folder / f"{class_name_for('cand')}.py"
    source = strat.read_text()
    assert strategy_frozen_value(source, "START") == protocol.start
    _one_trade_db(cli, protocol)
    _Clock.at = datetime(2025, 11, 20, 0, 0, tzinfo=UTC)
    line = f"\nSTART = {protocol.start}\n"
    for bad, needle in (
        (f"\nSTART = {protocol.start + 4 * 3600}\n", f"START {protocol.start + 4 * 3600}"),
        ("\nSTART = None\n", "START None"),
        ("\n# START removed\n", "no single `START"),
        (line + "START = 0\n", "no single `START"),
    ):
        strat.write_text(source.replace(line, bad))
        capsys.readouterr()
        assert cli["mod"].main(_compare_argv(cli)) == 2, bad
        assert needle in capsys.readouterr().err
    strat.write_text(source)
    assert cli["mod"].main(_compare_argv(cli)) == 0
