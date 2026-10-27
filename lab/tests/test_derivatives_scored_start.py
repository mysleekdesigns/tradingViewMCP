"""Phase 12 items 3-4, quant verify round 3 (PLAN.md Phase 12):

* W1: every derivatives plan on a (symbol, interval) is scored from ONE start, the latest first
  timestamp of the five derivatives archive series (fundingRate, metrics, premium/mark/index
  klines at the interval) + ``[derivatives] warmup_days``, snapped to the first bar at or after
  it and never before ``design_start``. Inside a research run it comes from the run's frozen
  snapshot, so it cannot move mid-run. Which derivatives features a plan uses, or their
  periods, never move it: a derivatives look-back longer than ``warmup_days`` is refused. A
  missing series refuses every derivatives plan on that symbol/interval, before any ledger row.
* W2: the holdout check also refuses (nothing spent) when a needed ``drv_*`` column starts late
  inside the holdout, or the frozen manifest records a gap inside the holdout longer than the
  column's staleness limit.

Offline: seeded synthetic candles, tmp ledgers and tmp archive directories.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.config import DEFAULT_CONFIG_PATH, LabConfig, load_config
from lab.data.derivatives import build_manifest
from lab.data.derivatives_columns import (
    DataUnavailableError,
    derivatives_start_floor,
    max_derivatives_period,
    require_holdout_coverage,
    required_series_keys,
)
from lab.ledger import Ledger
from lab.plan import parse_plan
from lab.registry.derivatives import DERIVATIVES_COLUMNS, DRV_FUNDING_RATE
from lab.report import seconds
from lab.research.contract import load_research_config
from lab.research.holdout import HoldoutRefusedError, run_holdout_check
from lab.research.runs import check_derivatives_snapshot, derivatives_snapshot, start_research_run
from lab.runner import (
    DerivativesLookbackError,
    derivatives_lookback_errors,
    plan_design_window,
    run_backtest_report,
)
from tests import test_mcp_server as mcp
from tests.synthetic import make_candles, with_derivatives, write_derivatives_archive
from tests.test_derivatives_scoring import (
    CRASH_DRV,
    PRICE_GATED,
    PRICE_ONLY,
    SCORED_START,
    loader_of,
    write_archive,
)
from tests.test_research_derivatives_snapshot import (
    GATED_PLAN,
    WITH_DRV,
    drv_lab,
    seed_gated_pre_holdout,
    start_lax,
    write_manifest,
)
from tests.test_runner import small_cfg

H4 = pd.Timedelta(hours=4)


def gated(name: str, gate: str, features: dict[str, Any]) -> dict[str, Any]:
    """``close > ema_slow`` (the price signal) and a derivatives ``gate``."""
    return {
        **PRICE_ONLY,
        "name": name,
        "features": {"ema_slow": {"ema": {"period": 50}}, **features},
        "long_entry": f"close > ema_slow and {gate}",
    }


FZ_180 = gated("fz180", "fz < 2", {"fz": {"funding_zscore": {"period": 180}}})
FUNDING = gated("funding", "fr < 0.0004", {"fr": {"funding_rate": {}}})
BASIS = gated("basis", "b < 0.1", {"b": {"basis_pct": {}}})
LS = gated("ls", "ls < 1.9", {"ls": {"long_short_ratio": {}}})
# PRICE_GATED plus a gate that is true wherever it is defined: oi_change(180) needs 180 bars of
# open interest, i.e. exactly the 30-day warm-up, so it is defined from the shared start on
NOOP = {
    **PRICE_GATED,
    "name": "ema_trend_funding_gate_noop_oi",
    "features": {**PRICE_GATED["features"], "oc": {"oi_change": {"period": 180}}},
    "long_entry": f"{PRICE_GATED['long_entry']} and oc > -1000",
}


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    out = small_cfg(tmp_path)
    write_archive(out)  # every series starts at DRV_FROM (2019-06-01)
    return out


def bare_cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path / "bare")


def history(cfg: LabConfig) -> pd.DataFrame:
    return CRASH_DRV.loc[CRASH_DRV.index < cfg.splits.holdout_start]


# --------------------------------------------------------------------------- W1: one start


def test_every_derivatives_plan_on_a_symbol_interval_shares_one_scored_start(
    cfg: LabConfig,
) -> None:
    """Before the fix the start was each plan's own first-ready bar: fz(30) 2019-06-05,
    fz(180) / oi_change(180) 2019-07-01, funding/basis/ls 2019-06-01."""
    starts = set()
    for raw in (PRICE_GATED, FZ_180, FUNDING, BASIS, LS, NOOP):
        window, scoring = plan_design_window(parse_plan(raw), history(cfg), cfg)
        assert scoring is not None and scoring["scored_from"] == seconds(window.start)
        starts.add(window.start)
    assert starts == {SCORED_START}


def test_a_no_op_gate_does_not_move_the_window_or_the_result(
    cfg: LabConfig, tmp_path: Path
) -> None:
    """The verify-round regression: `... and oc > -1000` moved BTC 4h's start 13.5 months and
    changed Sharpe 1.11 -> 0.49. Now the same logic scores the same bars, folds and returns."""
    with Ledger(tmp_path / "l.db") as ledger:
        base = run_backtest_report(PRICE_GATED, cfg=cfg, ledger=ledger, loader=loader_of(CRASH_DRV))
        noop = run_backtest_report(NOOP, cfg=cfg, ledger=ledger, loader=loader_of(CRASH_DRV))
    assert base["window"] == noop["window"]
    assert base["window"]["start"] == seconds(SCORED_START)
    assert base["scoring"]["scored_from"] == noop["scoring"]["scored_from"]
    assert base["metrics"]["net"] == noop["metrics"]["net"]
    assert base["benchmarks"]["buy_and_hold"] == noop["benchmarks"]["buy_and_hold"]
    folds = [(r["test_start"], r["test_end"]) for r in base["walk_forward"]]
    assert folds and folds == [(r["test_start"], r["test_end"]) for r in noop["walk_forward"]]


def test_gate_base_check_runs_on_the_shared_window(cfg: LabConfig) -> None:
    hist = history(cfg)
    window, scoring = plan_design_window(parse_plan(PRICE_GATED), hist, cfg)
    assert scoring is not None
    in_window = (hist.index >= SCORED_START) & (hist.index < window.end)
    assert scoring["bars"] == int(in_window.sum())  # close > ema(50) is defined on every bar
    ema = hist["close"].ewm(span=50, adjust=False).mean()  # registry ema: same recursion
    share = float((hist["close"] > ema)[in_window].mean())
    assert scoring["price_clause_true_share"] == pytest.approx(share, abs=0.01)


def test_scored_start_is_the_latest_series_first_plus_warmup(tmp_path: Path) -> None:
    """A funding-only plan starts after the METRICS series it does not read: the start belongs
    to the symbol/interval, not to the plan's features."""
    cfg = bare_cfg(tmp_path)
    late = pd.Timestamp("2019-08-01 02:00", tz="UTC")
    write_archive(cfg, firsts={"metrics": late})
    window, _ = plan_design_window(parse_plan(PRICE_GATED), history(cfg), cfg)
    # 2019-08-31 02:00 snapped to the next 4h bar open
    assert window.start == pd.Timestamp("2019-08-31 04:00", tz="UTC")
    window_fr, _ = plan_design_window(parse_plan(FUNDING), history(cfg), cfg)
    assert window_fr.start == window.start


def test_scored_start_is_never_before_design_start(tmp_path: Path) -> None:
    cfg = bare_cfg(tmp_path)
    write_archive(cfg, first=pd.Timestamp("2018-07-01", tz="UTC"))  # + 30 days < 2019-01-01
    window, scoring = plan_design_window(parse_plan(PRICE_GATED), history(cfg), cfg)
    assert window.start == cfg.splits.design_start
    assert scoring is not None and scoring["scored_from_utc"] == "2019-01-01T00:00:00Z"


def test_start_floor_reads_the_series_firsts() -> None:
    series = {
        key: {"first": "2020-01-01T00:00:00+00:00"} for key in required_series_keys("X", "4h")
    }
    series["metrics/X"] = {"first": "2020-09-01T00:00:00+00:00"}
    got = derivatives_start_floor("X", "4h", series, warmup_days=30)
    assert got == pd.Timestamp("2020-10-01", tz="UTC")
    assert required_series_keys("X", "1h") == (
        "fundingRate/X",
        "premiumIndexKlines/X/1h",
        "markPriceKlines/X/1h",
        "indexPriceKlines/X/1h",
        "metrics/X",
    )


# --------------------------------------------------------------------------- W1: frozen


def test_the_scored_start_is_frozen_with_the_run(cfg: LabConfig, tmp_path: Path) -> None:
    """A metrics re-download mid-run (a series the funding plan does not read, so the snapshot
    check lets the plan run) changes the manifest's first; the run keeps its frozen start."""
    research = load_research_config(mcp.write_criteria(tmp_path / "c.toml"))
    with Ledger(tmp_path / "l.db") as ledger:
        run_id = start_research_run(ledger, research, label="t", cfg=cfg)
        shutil.rmtree(cfg.derivatives_dir / "metrics")
        later = pd.Timestamp("2019-07-01", tz="UTC")
        write_archive(cfg, firsts={"metrics": later})
        check_derivatives_snapshot(ledger, run_id, cfg, PRICE_GATED)  # funding unchanged
        in_run = run_backtest_report(
            PRICE_GATED, cfg=cfg, ledger=ledger, run_id=run_id, loader=loader_of(CRASH_DRV)
        )
        outside = run_backtest_report(
            PRICE_GATED, cfg=cfg, ledger=ledger, run_id="ui", loader=loader_of(CRASH_DRV)
        )
    assert in_run["window"]["start"] == seconds(SCORED_START)
    assert outside["window"]["start"] == seconds(later + pd.Timedelta(days=30))


# --------------------------------------------------------------------------- W1: missing


def test_a_missing_series_refuses_every_derivatives_plan_before_any_row(
    tmp_path: Path,
) -> None:
    cfg = bare_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        with pytest.raises(DataUnavailableError, match="missing from a derivatives manifest"):
            run_backtest_report(PRICE_GATED, cfg=cfg, ledger=ledger, loader=loader_of(CRASH_DRV))
        write_archive(cfg)
        shutil.rmtree(cfg.derivatives_dir / "metrics")
        build_manifest(cfg.derivatives_dir, gap_factor=cfg.derivatives.gap_factor)
        for plan in (PRICE_GATED, FUNDING):  # neither reads open interest
            with pytest.raises(DataUnavailableError) as info:
                run_backtest_report(plan, cfg=cfg, ledger=ledger, loader=loader_of(CRASH_DRV))
            msg = str(info.value)
            assert "metrics/BTCUSDT is missing from the derivatives manifest" in msg
            assert (
                "uv run python -m lab.data.derivatives download BTCUSDT --datasets "
                "fundingRate,premiumIndexKlines,markPriceKlines,indexPriceKlines,metrics "
                "--interval 4h" in msg
            )
            assert "nothing was recorded in the ledger" in msg
        assert ledger.count() == 0
        # price-only plans never need the archive
        run_backtest_report(PRICE_ONLY, cfg=cfg, ledger=ledger, loader=loader_of(CRASH_DRV))


def test_mcp_refuses_a_plan_whose_run_froze_an_incomplete_archive(tmp_path: Path) -> None:
    cfg = mcp.small_cfg(tmp_path)
    write_derivatives_archive(cfg.derivatives_dir, first="2018-06-01", end="2020-06-01")
    shutil.rmtree(cfg.derivatives_dir / "markPriceKlines")
    build_manifest(cfg.derivatives_dir, gap_factor=cfg.derivatives.gap_factor)
    lab = drv_lab(tmp_path, manifest=False)
    run_id = lab.start()
    msg = mcp.error_text(lab.server, "run_backtest", {"run_id": run_id, "plan": GATED_PLAN})
    assert "markPriceKlines/BTCUSDT/4h is missing" in msg and "lab.data.derivatives" in msg
    assert mcp.counts(lab, run_id) == (0, 0, 0)


# --------------------------------------------------------------------------- W1: look-back cap


def test_a_look_back_longer_than_warmup_days_is_refused_before_any_row(
    cfg: LabConfig, tmp_path: Path
) -> None:
    long_oi = {**NOOP, "features": {**NOOP["features"], "oc": {"oi_change": {"period": 1000}}}}
    long_fz = gated("fz1000", "fz < 2", {"fz": {"funding_zscore": {"period": 1000}}})
    with Ledger(tmp_path / "l.db") as ledger:
        for raw, feature in ((long_oi, "oi_change"), (long_fz, "funding_zscore")):
            with pytest.raises(DerivativesLookbackError) as info:
                run_backtest_report(raw, cfg=cfg, ledger=ledger, loader=loader_of(CRASH_DRV))
            assert (
                f"{feature} period 1000 on 4h = 166.7 days > [derivatives] warmup_days 30; "
                "use <= 180" in str(info.value)
            )
        assert ledger.count() == 0
        ok_plan = gated("fz180", "fz < 2", {"fz": {"funding_zscore": {"period": 180}}})
        assert derivatives_lookback_errors(parse_plan(ok_plan), cfg) == []
        too_long = gated("fz181", "fz < 2", {"fz": {"funding_zscore": {"period": 181}}})
        (err,) = derivatives_lookback_errors(parse_plan(too_long), cfg)
        assert err.path == "features.fz.funding_zscore.period" and "use <= 180" in err.message


def test_look_back_cap_scales_with_the_interval(cfg: LabConfig) -> None:
    assert max_derivatives_period("4h", 30) == 180
    assert max_derivatives_period("1h", 30) == 720
    assert max_derivatives_period("1d", 30) == 30
    hourly = {**FZ_180, "interval": "1h", "features": {"fz": {"funding_zscore": {"period": 721}}}}
    hourly["features"]["ema_slow"] = {"ema": {"period": 50}}
    (err,) = derivatives_lookback_errors(parse_plan(hourly), cfg)
    assert "funding_zscore period 721 on 1h = 30.0 days" in err.message
    assert "use <= 720" in err.message


def test_neighbours_never_look_back_past_the_cap(cfg: LabConfig, tmp_path: Path) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        run_backtest_report(FZ_180, cfg=cfg, ledger=ledger, run_id="t", loader=loader_of(CRASH_DRV))
        rows = [r for r in ledger.list(run_id="t", limit=ledger.count("t"))]
    periods = [
        parse_plan(r.plan_json).features[1].params["period"]
        for r in rows
        if r.source == "neighbourhood"
    ]
    assert periods and max(periods) <= 180 and min(periods) < 180


def test_mcp_validate_and_sweep_apply_the_look_back_cap(tmp_path: Path) -> None:
    lab = drv_lab(tmp_path)
    run_id = lab.start()
    long_fz = {**GATED_PLAN, "features": {**GATED_PLAN["features"]}}
    long_fz["features"]["fz"] = {"funding_zscore": {"period": 1000}}
    out = mcp.ok(lab.server, "validate_plan", {"plan": long_fz})
    assert out["valid"] is False
    assert any("use <= 180" in e["message"] for e in out["errors"])
    good = mcp.ok(lab.server, "validate_plan", {"plan": GATED_PLAN})
    assert good["valid"] and good["tunable_params"]["fz.period"]["high"] == 180
    assert good["tunable_params"]["fz.period"]["low"] == 12  # Note 2
    msg = mcp.error_text(
        lab.server,
        "run_sweep",
        {
            "run_id": run_id,
            "plan": GATED_PLAN,
            "space": {"fz.period": {"low": 20, "high": 300}},
            "n_trials": 2,
        },
    )
    assert "use high <= 180" in msg
    assert mcp.counts(lab, run_id) == (0, 0, 0)


# --------------------------------------------------------------------------- W1: price-only


def test_price_only_reports_are_bit_identical_with_or_without_the_archive(
    cfg: LabConfig, tmp_path: Path
) -> None:
    bare = bare_cfg(tmp_path)
    with Ledger(tmp_path / "a.db") as a, Ledger(tmp_path / "b.db") as b:
        with_archive = run_backtest_report(
            PRICE_ONLY, cfg=cfg, ledger=a, loader=loader_of(CRASH_DRV)
        )
        without = run_backtest_report(PRICE_ONLY, cfg=bare, ledger=b, loader=loader_of(CRASH_DRV))
    with_archive.pop("elapsed_ms")
    without.pop("elapsed_ms")
    assert with_archive == without
    assert with_archive["window"]["start"] == seconds(cfg.splits.design_start)


# --------------------------------------------------------------------------- config + snapshot


def test_warmup_days_is_configured_and_validated(tmp_path: Path) -> None:
    assert load_config().derivatives.warmup_days == 30.0
    raw = DEFAULT_CONFIG_PATH.read_text()
    assert "warmup_days = 30 " in raw
    path = tmp_path / "lab.toml"
    path.write_text(raw.replace("warmup_days = 30 ", "warmup_days = 0 "))
    with pytest.raises(ValueError, match=r"derivatives\.warmup_days"):
        load_config(path)


def test_snapshot_freezes_warmup_days_and_refuses_a_change(cfg: LabConfig, tmp_path: Path) -> None:
    snap = derivatives_snapshot(cfg)
    assert snap is not None and snap["warmup_days"] == 30.0
    assert all("gaps" in entry for entry in snap["series"].values())
    research = load_research_config(mcp.write_criteria(tmp_path / "c.toml"))
    with Ledger(tmp_path / "l.db") as ledger:
        run_id = start_research_run(ledger, research, label="t", cfg=cfg)
        longer = replace(cfg, derivatives=replace(cfg.derivatives, warmup_days=60.0))
        with pytest.raises(ValueError, match=r"warmup_days changed.*start a new run"):
            check_derivatives_snapshot(ledger, run_id, longer, PRICE_GATED)


def test_snapshot_without_warmup_days_is_refused(
    cfg: LabConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lab.research import runs as runs_mod

    old = derivatives_snapshot(cfg)
    assert old is not None
    monkeypatch.setattr(
        runs_mod,
        "derivatives_snapshot",
        lambda _c: {k: v for k, v in old.items() if k != "warmup_days"},
    )
    research = load_research_config(mcp.write_criteria(tmp_path / "c.toml"))
    with Ledger(tmp_path / "l.db") as ledger:
        run_id = start_research_run(ledger, research, label="t", cfg=cfg)
        with pytest.raises(ValueError, match=r"predates the shared derivatives scored start"):
            check_derivatives_snapshot(ledger, run_id, cfg, PRICE_GATED)


# --------------------------------------------------------------------------- W2: unit


YEAR = with_derivatives(
    make_candles(6 * 366, seed=31, start="2024-01-01", price=40_000.0), seed=31, gaps=False
)
H_START = pd.Timestamp("2024-01-01", tz="UTC")


def coverage(candles: pd.DataFrame, series: dict[str, Any] | None = None) -> None:
    require_holdout_coverage(
        GATED_PLAN,
        candles,
        symbol="BTCUSDT",
        interval="4h",
        stale_after_intervals=2.0,
        series=series,
        start=H_START,
    )


def test_a_column_that_starts_late_in_the_holdout_is_refused() -> None:
    """NaN for the first 11 of 12 holdout months, finite at the end: the old end-only check
    passed it."""
    late = YEAR.copy()
    late.loc[late.index < pd.Timestamp("2024-12-01", tz="UTC"), DRV_FUNDING_RATE] = np.nan
    with pytest.raises(DataUnavailableError, match=r"drv_funding_rate starts at the bar opening"):
        coverage(late)
    coverage(YEAR)  # full coverage passes
    # a start within the staleness limit (2 x 8h funding) is tolerated
    edge = YEAR.copy()
    edge.loc[edge.index < H_START + pd.Timedelta(hours=16), DRV_FUNDING_RATE] = np.nan
    coverage(edge)
    edge.loc[edge.index < H_START + pd.Timedelta(hours=20), DRV_FUNDING_RATE] = np.nan
    with pytest.raises(DataUnavailableError, match="starts at"):
        coverage(edge)


def test_a_manifest_gap_inside_the_holdout_is_refused() -> None:
    """The aligned column hides nothing here (finite throughout); the manifest's gap does."""

    def gaps(*spans: tuple[str, str]) -> dict[str, Any]:
        return {"fundingRate/BTCUSDT": {"gaps": [{"from": a, "to": b} for a, b in spans]}}

    with pytest.raises(DataUnavailableError, match=r"fundingRate/BTCUSDT has a gap from"):
        coverage(YEAR, gaps(("2024-06-01T00:00:00+00:00", "2024-06-03T00:00:00+00:00")))
    # within the 16h staleness limit, or outside the holdout: no refusal
    coverage(YEAR, gaps(("2024-06-01T00:00:00+00:00", "2024-06-01T16:00:00+00:00")))
    coverage(YEAR, gaps(("2023-06-01T00:00:00+00:00", "2023-12-31T08:00:00+00:00")))
    # a gap reaching into the holdout counts only its overlap: 17h inside -> refused
    with pytest.raises(DataUnavailableError, match="has a gap"):
        coverage(YEAR, gaps(("2023-12-01T00:00:00+00:00", "2024-01-01T17:00:00+00:00")))
    # another series' gap (metrics: not read by a funding plan) is not this plan's problem
    coverage(YEAR, {"metrics/BTCUSDT": {"gaps": [{"from": "2024-03-01", "to": "2024-04-01"}]}})


# --------------------------------------------------------------------------- W2: holdout


def test_holdout_refuses_a_column_that_starts_late_and_spends_nothing(tmp_path: Path) -> None:
    cfg = mcp.small_cfg(tmp_path)
    write_manifest(cfg)
    late = WITH_DRV.copy()
    cut = late.index[-1] - pd.Timedelta(days=10)  # holdout 2019-10-01 .. 2020-01: NaN until then
    holdout = late.index >= cfg.splits.holdout_start
    late.loc[holdout & (late.index < cut), list(DERIVATIVES_COLUMNS)] = np.nan
    with Ledger(tmp_path / "ledger.db") as led:
        run_id = start_lax(led, tmp_path, cfg)
        seed_gated_pre_holdout(led, run_id, cfg)
        n = led.count(run_id)
        with pytest.raises(HoldoutRefusedError, match=r"starts at.*no holdout check was spent"):
            run_holdout_check(
                GATED_PLAN, run_id=run_id, cfg=cfg, ledger=led, loader=mcp.loader_for(late)
            )
        assert led.holdout_checks_used(run_id) == 0 and led.count(run_id) == n


def _funding_hole(cfg: LabConfig, lo: str, hi: str) -> None:
    """Drop the archive's funding rows in [lo, hi) and rebuild the manifest."""
    folder = cfg.derivatives_dir / "fundingRate" / "BTCUSDT"
    a, b = pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC")
    for path in sorted(folder.glob("*.parquet")):
        frame = pd.read_parquet(path)
        keep = frame.loc[(frame.index < a) | (frame.index >= b)]
        if len(keep) != len(frame):
            keep.to_parquet(path, engine="pyarrow")
    build_manifest(cfg.derivatives_dir, gap_factor=cfg.derivatives.gap_factor)


@pytest.mark.parametrize(
    ("hole", "spends"),
    [
        (("2019-11-10", "2019-11-15"), False),  # mid-holdout, 5 days: refused
        (("2019-03-10", "2019-03-15"), True),  # in the design window: not a holdout problem
    ],
)
def test_holdout_refuses_a_frozen_manifest_gap_inside_the_holdout(
    tmp_path: Path, hole: tuple[str, str], spends: bool
) -> None:
    cfg = mcp.small_cfg(tmp_path)
    write_manifest(cfg)
    _funding_hole(cfg, *hole)
    with Ledger(tmp_path / "ledger.db") as led:
        run_id = start_lax(led, tmp_path, cfg)
        run = led.research_run(run_id)
        assert run is not None
        frozen = run["config"]["derivatives"]["series"]["fundingRate/BTCUSDT"]["gaps"]
        assert frozen and frozen[0]["from"].startswith(hole[0][:8])
        seed_gated_pre_holdout(led, run_id, cfg)
        n = led.count(run_id)
        if not spends:
            with pytest.raises(HoldoutRefusedError, match=r"has a gap.*no holdout check"):
                run_holdout_check(
                    GATED_PLAN, run_id=run_id, cfg=cfg, ledger=led, loader=mcp.loader_for(WITH_DRV)
                )
            assert led.holdout_checks_used(run_id) == 0 and led.count(run_id) == n
        else:
            out = run_holdout_check(
                GATED_PLAN, run_id=run_id, cfg=cfg, ledger=led, loader=mcp.loader_for(WITH_DRV)
            )
            assert out["checks_used"] == 1 and led.holdout_checks_used(run_id) == 1


def test_holdout_with_full_coverage_passes_the_guard_and_spends(tmp_path: Path) -> None:
    cfg = mcp.small_cfg(tmp_path)
    write_manifest(cfg)
    with Ledger(tmp_path / "ledger.db") as led:
        run_id = start_lax(led, tmp_path, cfg)
        seed_gated_pre_holdout(led, run_id, cfg)
        out = run_holdout_check(
            GATED_PLAN, run_id=run_id, cfg=cfg, ledger=led, loader=mcp.loader_for(WITH_DRV)
        )
        assert out["checks_used"] == 1 and set(out) == {"passed", "checks_used", "checks_left"}
        assert led.holdout_checks_used(run_id) == 1
