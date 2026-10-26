"""Portfolio research plumbing: config, fingerprint, criteria file, universe helpers, scoring
span, and the independent accounting cross-check (offline, synthetic data)."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lab.config import DEFAULT_CONFIG_PATH, load_config
from lab.crosscheck.portfolio import EQUITY_TOLERANCE, compare_equity, replay_portfolio
from lab.data.universe_build import manifest_sha256, write_universe
from lab.engine.portfolio import run_portfolio
from lab.ledger import Ledger
from lab.plan import portfolio as pf
from lab.portfolio_runner import (
    StaleUniverseError,
    design_window,
    impact_model,
    load_universe_data,
    null_rank,
    portfolio_holdout_evaluation,
    run_on_window,
    score_from,
    stale_universe_note,
)
from lab.research.contract import ResearchConfigError, load_research_config
from lab.research.logic import any_logic_hash, logic_hash, portfolio_logic_hash
from lab.research.portfolio_criteria import _random_selection_row
from lab.research.runs import (
    check_lab_fingerprint,
    check_universe_manifest,
    lab_fingerprint,
    start_research_run,
)
from lab.stats import metrics_from_returns
from lab.types import CostModel
from tests.synthetic import make_universe_candles
from tests.test_mcp_server import EMA_PLAN, write_criteria
from tests.test_portfolio_pipeline import H1, portfolio_cfg

# --------------------------------------------------------------------------- config


def test_lab_toml_has_the_portfolio_settings() -> None:
    cfg = load_config()
    assert cfg.universe.liquidity_cap_mult == 10.0
    p = cfg.portfolio
    assert (p.impact_coef, p.impact_aum_usd, p.impact_cap) == (0.7, 1_000_000.0, 0.05)
    assert (p.impact_adv_days, p.impact_vol_days, p.alpha_rolling_days) == (30, 30, 60)
    assert p.null_paths == 500 and p.min_rebalance == 0.0
    assert p.neighbourhood_max_trials == 1 + 9 * 4


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("impact_cap = 0.05", "impact_cap = 0.3", "must stay below 1"),  # (0.01+0.3) x4 >= 1
        ("impact_cap = 0.05", "impact_cap = 1.0", r"portfolio\.impact_cap"),
        ("impact_coef = 0.7", "impact_coef = -1", r"portfolio\.impact_coef"),
        ("null_paths = 500", "null_paths = 0", r"portfolio\.null_paths"),
        ("liquidity_cap_mult = 10.0", "liquidity_cap_mult = 0", r"universe\.liquidity_cap_mult"),
    ],
)
def test_bad_portfolio_settings_are_rejected(
    tmp_path: Path, old: str, new: str, match: str
) -> None:
    raw = DEFAULT_CONFIG_PATH.read_text()
    assert old in raw
    path = tmp_path / "lab.toml"
    path.write_text(raw.replace(old, new))
    with pytest.raises(ValueError, match=match):
        load_config(path)


def test_the_fingerprint_freezes_the_liquidity_cap_and_portfolio_settings(tmp_path: Path) -> None:
    cfg = portfolio_cfg(tmp_path)
    values = lab_fingerprint(cfg)["values"]
    assert values["universe"]["liquidity_cap_mult"] == 10.0
    assert values["universe"]["portfolio"]["impact_coef"] == 0.7
    with Ledger(tmp_path / "l.db") as led:
        rc = load_research_config(write_criteria(tmp_path / "c.toml"))
        run_id = start_research_run(led, rc, label="x", cfg=cfg)
        for changed in (
            replace(cfg, universe=replace(cfg.universe, liquidity_cap_mult=5.0)),
            replace(cfg, portfolio=replace(cfg.portfolio, impact_coef=0.5)),
            replace(cfg, portfolio=replace(cfg.portfolio, null_paths=7)),
        ):
            with pytest.raises(ValueError, match="changed: universe"):
                check_lab_fingerprint(led, run_id, changed)


# --------------------------------------------------------------------------- criteria file


def test_portfolio_criteria_table_is_optional_and_frozen_when_present(tmp_path: Path) -> None:
    plain = load_research_config(write_criteria(tmp_path / "a.toml"))
    assert plain.portfolio is None and "portfolio" not in json.loads(plain.to_json())
    path = write_criteria(tmp_path / "b.toml")
    path.write_text(
        path.read_text()
        + "\n[portfolio]\nbeat_equal_weight = true\nalpha_p_max = 0.05\nalpha_min_annual = 0.02\n"
    )
    rc = load_research_config(path)
    assert rc.portfolio is not None and rc.portfolio.alpha_p_max == 0.05
    assert json.loads(rc.to_json())["portfolio"]["alpha_min_annual"] == 0.02
    with Ledger(tmp_path / "l.db") as led:
        run_id = start_research_run(led, rc, label="x")
        run = led.research_run(run_id)
        assert run is not None and run["config"]["portfolio"]["beat_equal_weight"] is True


@pytest.mark.parametrize(
    ("table", "match"),
    [
        ("beat_equal_weight = true\nalpha_p_max = 0.05\n", r"missing keys \['alpha_min_annual'\]"),
        ("beat_equal_weight = true\nalpha_p_max = 2.0\nalpha_min_annual = 0.0\n", "p-value"),
        ("beat_equal_weight = 1\nalpha_p_max = 0.05\nalpha_min_annual = 0.0\n", "beat_equal"),
    ],
)
def test_malformed_portfolio_criteria_are_rejected(tmp_path: Path, table: str, match: str) -> None:
    path = write_criteria(tmp_path / "c.toml")
    path.write_text(path.read_text() + "\n[portfolio]\n" + table)
    with pytest.raises(ResearchConfigError, match=match):
        load_research_config(path)


def test_the_proposed_criteria_file_documents_the_portfolio_table(tmp_path: Path) -> None:
    from tests.test_mcp_server import PROPOSED_CRITERIA

    text = PROPOSED_CRITERIA.read_text()
    assert "# [portfolio]" in text and "REBALANCES" in text
    assert load_research_config(PROPOSED_CRITERIA).portfolio is None  # commented out: opt-in
    head, _, block = text.partition("# [portfolio]")
    uncommented = head + "[portfolio]" + block.replace("\n# ", "\n")
    path = tmp_path / "c.toml"
    path.write_text(uncommented)
    pc = load_research_config(path).portfolio
    assert pc is not None
    assert (pc.beat_equal_weight, pc.alpha_p_max, pc.alpha_min_annual) == (True, 0.05, 0.0)


# --------------------------------------------------------------------------- logic


def test_portfolio_logic_ignores_numbers_and_names_but_not_structure() -> None:
    base = portfolio_logic_hash(H1)
    retuned = {**H1, "name": "other", "score": {"trailing_return": {"lookback": 90}}}
    assert portfolio_logic_hash(retuned) == base == any_logic_hash(H1)
    banded = {**H1, "selection": {"quantile": 0.2, "exit_quantile": 0.3, "min_names": 1}}
    assert portfolio_logic_hash(banded) != base
    assert any_logic_hash(EMA_PLAN) == logic_hash(EMA_PLAN)


# --------------------------------------------------------------------------- universe helpers


@pytest.fixture
def universe(tmp_path: Path):  # type: ignore[no-untyped-def]
    cfg = portfolio_cfg(tmp_path)
    candles = make_universe_candles()
    write_universe(candles, cfg.universe, universe_dir=cfg.universe_dir, missing=[])

    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        c = candles[symbol]
        return c.loc[c.index < end]

    return cfg, candles, load


def test_manifest_sha256_hashes_the_manifest_file(universe) -> None:  # type: ignore[no-untyped-def]
    cfg, _, _ = universe
    path = cfg.universe_dir / "USDT-1d.json"
    assert (
        manifest_sha256(cfg.universe_dir, "USDT", "1d")
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert manifest_sha256(cfg.universe_dir, "USDT", "4h") is None


def test_check_universe_manifest_needs_a_frozen_and_unchanged_manifest(
    universe, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    cfg, _, _ = universe
    rc = load_research_config(write_criteria(tmp_path / "c.toml"))
    with Ledger(tmp_path / "l.db") as led:
        bare = start_research_run(led, rc, label="no cfg")
        with pytest.raises(ValueError, match="froze no universe manifest"):
            check_universe_manifest(led, bare, cfg)
        run_id = start_research_run(led, rc, label="x", cfg=cfg)
        assert check_universe_manifest(led, run_id, cfg) == manifest_sha256(
            cfg.universe_dir, "USDT", "1d"
        )
        (cfg.universe_dir / "USDT-1d.json").write_text("{}")
        with pytest.raises(ValueError, match="universe manifest changed"):
            check_universe_manifest(led, run_id, cfg)


def test_universe_data_refuses_a_build_that_does_not_match_lab_toml(universe) -> None:  # type: ignore[no-untyped-def]
    cfg, _, load = universe
    other = replace(cfg, universe=replace(cfg.universe, min_history_days=60))
    with pytest.raises(ValueError, match="min_history_days"):
        load_universe_data(other, load, end=cfg.splits.holdout_start)


def test_universe_data_members_are_the_point_in_time_top_n(universe) -> None:  # type: ignore[no-untyped-def]
    cfg, _, load = universe
    data = load_universe_data(cfg, load, end=cfg.splits.holdout_start)
    assert data.index.max() < cfg.splits.holdout_start
    assert int(data.members.sum(axis=1).max()) <= cfg.universe.top_n
    assert set(data.segment_starts) == {"GAPUSDT"}
    assert data.btc is not None and data.btc.index.max() < cfg.splits.holdout_start
    # a member is only ever a coin with a bar (eligibility needs a tradable bar)
    assert not (data.members & data.closes.isna()).to_numpy().any()


# --------------------------------------------------------------------------- scoring span


def test_score_from_ignores_every_row_before_the_first_decision() -> None:
    idx = pd.date_range("2020-01-01", periods=50, freq="D", tz="UTC")
    rng = np.random.default_rng(3)
    r = pd.Series(rng.normal(0.001, 0.02, 50), index=idx)
    first = idx[20]
    other = r.copy()
    other.iloc[:20] = rng.normal(0.05, 0.3, 20)  # anything before the first decision
    a = metrics_from_returns(score_from(r, first), "1d")
    b = metrics_from_returns(score_from(other, first), "1d")
    assert a == b and a.bars == 30
    assert score_from(r, None).empty


def test_first_decision_and_returns_cover_the_design_window(universe) -> None:  # type: ignore[no-untyped-def]
    cfg, _, load = universe
    data = load_universe_data(cfg, load, end=cfg.splits.holdout_start)
    window = design_window(data, cfg)
    plan = pf.validate_portfolio_plan(H1, interval="1d").plan
    assert plan is not None
    run = run_on_window(plan, data, window, cfg)
    assert run.first_decision is not None and run.first_decision >= window.start
    assert (run.returns.loc[run.returns.index < run.first_decision] == 0).all()
    assert run.scored.index[0] == run.first_decision
    assert run.decisions[0] == run.first_decision and run.n_filled == len(run.decisions)


# --------------------------------------------------------------------------- cross-check


def _engine_inputs(universe):  # type: ignore[no-untyped-def]
    cfg, _, load = universe
    data = load_universe_data(cfg, load, end=cfg.splits.holdout_start)
    window = design_window(data, cfg)
    plan = pf.validate_portfolio_plan(H1, interval="1d").plan
    assert plan is not None
    run = run_on_window(plan, data, window, cfg)
    return cfg, run


def test_the_replay_matches_the_engine_with_impact_delisting_and_segments(universe) -> None:  # type: ignore[no-untyped-def]
    cfg, run = _engine_inputs(universe)
    assert not run.result.segment_exits.empty  # the GAPUSDT break is exercised
    replay = replay_portfolio(
        run.view.candles,
        run.weights,
        costs=cfg.costs,
        init_cash=cfg.init_cash,
        segment_starts=run.view.segment_starts,
        impact=impact_model(cfg),
    )
    cmp = compare_equity(run.result.equity, replay.equity)
    assert cmp.matched, cmp
    assert cmp.max_rel_diff <= EQUITY_TOLERANCE
    assert replay.n_orders == len(run.result.orders)
    assert replay.fees_paid == pytest.approx(run.result.fees_paid, rel=1e-9)
    assert replay.impact_cost == pytest.approx(run.result.impact_cost, rel=1e-9)


def test_the_replay_matches_the_engine_with_a_rebalance_band_and_no_impact(universe) -> None:  # type: ignore[no-untyped-def]
    cfg, run = _engine_inputs(universe)
    kwargs = {"costs": cfg.costs, "init_cash": cfg.init_cash, "min_rebalance": 0.05}
    engine = run_portfolio(
        run.view.candles, run.weights, segment_starts=run.view.segment_starts, **kwargs
    )
    replay = replay_portfolio(
        run.view.candles, run.weights, segment_starts=run.view.segment_starts, **kwargs
    )
    assert compare_equity(engine.equity, replay.equity).matched


def test_the_crosscheck_catches_an_accounting_difference(universe) -> None:  # type: ignore[no-untyped-def]
    cfg, run = _engine_inputs(universe)
    pricier = CostModel(
        cfg.costs.fee_rate * 1.01,
        cfg.costs.slippage_rate,
        cfg.costs.slippage_vol_mult,
        cfg.costs.slippage_cap,
    )
    replay = replay_portfolio(
        run.view.candles,
        run.weights,
        costs=pricier,
        init_cash=cfg.init_cash,
        segment_starts=run.view.segment_starts,
        impact=impact_model(cfg),
    )
    cmp = compare_equity(run.result.equity, replay.equity)
    assert not cmp.matched and cmp.max_rel_diff > 100 * EQUITY_TOLERANCE


# --------------------------------------------------------------------------- quant-review fixes


def test_null_rank_drops_undefined_paths_from_percentile_and_p_value() -> None:
    """NOTE 3: one rule for NaN paths. 6 of 8 valid: [0.1, 0.2, 0.5, 0.9, 1.1, 1.4]; real 1.0
    beats 4 of 6 (66.67th percentile) and 2 valid paths are >= it: p = 3 / 7."""
    null = np.array([0.1, np.nan, 0.2, 0.5, np.nan, 0.9, 1.1, 1.4])
    pct, p, enough = null_rank(1.0, null)
    assert enough is True
    assert pct == pytest.approx(100.0 * 4 / 6, rel=1e-12)
    assert p == pytest.approx(3 / 7, rel=1e-12)


def test_null_rank_is_undefined_with_fewer_than_half_the_paths_valid() -> None:
    null = np.array([0.1, np.nan, np.nan, np.nan, 0.4])  # 2 of 5 valid
    pct, p, enough = null_rank(1.0, null)
    assert enough is False and math.isnan(pct) and math.isnan(p)
    pct, p, enough = null_rank(math.nan, np.array([0.1, 0.2]))
    assert enough is True and math.isnan(pct) and math.isnan(p)


def test_random_selection_criterion_is_missing_with_too_few_valid_paths(tmp_path: Path) -> None:
    crit = load_research_config(write_criteria(tmp_path / "c.toml")).criteria
    null = {"n_paths": 10, "n_valid_paths": 4, "percentile": 99.0}
    bt = {"benchmarks": {"random_selection": null}}
    row = _random_selection_row(crit, bt)
    assert row["status"] == "missing" and "fewer than half" in row["note"]
    null["n_valid_paths"] = 5
    row = _random_selection_row(crit, bt)
    assert row["status"] == "pass" and "5 of 10" in row["note"]


def test_a_stale_rank_table_cuts_the_design_window_with_a_warning(tmp_path: Path) -> None:
    """W2: ranks built on candles through mid-2020, candles refreshed through the design
    window: the window ends after the last rank row instead of scoring a cash tail."""
    cfg = portfolio_cfg(tmp_path)
    candles = make_universe_candles()
    cut = pd.Timestamp("2020-06-01", tz="UTC")
    assert max(c.index.max() for c in candles.values()) > cut
    write_universe(
        {s: c.loc[c.index < cut] for s, c in candles.items()},
        cfg.universe,
        universe_dir=cfg.universe_dir,
        missing=[],
    )

    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        c = candles[symbol]
        return c.loc[c.index < end]

    data = load_universe_data(cfg, load, end=cfg.splits.holdout_start)
    assert data.ranks_end is not None and data.ranks_end < data.index[-1]
    window = design_window(data, cfg)
    assert window.end == data.ranks_end + pd.Timedelta(days=1) <= cut
    note = stale_universe_note(data, cfg)
    assert note is not None and "universe_build build" in note
    plan = pf.validate_portfolio_plan(H1, interval="1d").plan
    assert plan is not None
    run = run_on_window(plan, data, window, cfg)
    held = run.result.weights.loc[run.scored.index].sum(axis=1)
    assert (held.iloc[-10:] > 0).all()  # invested to the end: no cash tail


def test_a_fresh_rank_table_leaves_the_design_window_alone(universe) -> None:  # type: ignore[no-untyped-def]
    cfg, _, load = universe
    data = load_universe_data(cfg, load, end=cfg.splits.holdout_start)
    assert data.ranks_end == data.index[-1]
    assert stale_universe_note(data, cfg) is None


def test_the_holdout_evaluation_raises_on_a_stale_rank_table(tmp_path: Path) -> None:
    cfg = portfolio_cfg(tmp_path)
    candles = make_universe_candles(start="2019-01-01", n=900)
    cut = cfg.splits.holdout_start + pd.Timedelta(days=20)
    write_universe(
        {s: c.loc[c.index < cut] for s, c in candles.items()},
        cfg.universe,
        universe_dir=cfg.universe_dir,
        missing=[],
    )

    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        c = candles[symbol]
        return c.loc[c.index < end]

    plan = pf.validate_portfolio_plan(H1, interval="1d").plan
    assert plan is not None
    with pytest.raises(StaleUniverseError, match="rank table ends"):
        portfolio_holdout_evaluation(plan, cfg, load)


def test_signal_delay_keeps_the_decision_rows_and_starts_later(universe) -> None:  # type: ignore[no-untyped-def]
    """W1: stale inputs, same rebalance rows (shifting the weights would move the weekday)."""
    cfg, _, load = universe
    data = load_universe_data(cfg, load, end=cfg.splits.holdout_start)
    window = design_window(data, cfg)
    plan = pf.validate_portfolio_plan(H1, interval="1d").plan
    assert plan is not None
    base = run_on_window(plan, data, window, cfg)
    stale = run_on_window(plan, data, window, cfg, signal_delay=3 * plan.rebalance_every)
    assert stale.signals.schedule.equals(base.signals.schedule)
    weekdays = {t.day_of_week for t in base.decisions}
    assert len(weekdays) == 1 and {t.day_of_week for t in stale.decisions} == weekdays
    assert stale.first_decision is not None and base.first_decision is not None
    assert stale.first_decision > base.first_decision
    assert not stale.weights.loc[stale.decisions].equals(base.weights.loc[stale.decisions])
