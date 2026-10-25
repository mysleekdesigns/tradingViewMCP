"""lab.research.pipeline: the one shared path from a design-window report to research evidence
(stripped, regimes from the run's frozen definition, holdout never loaded)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from lab.config import LabConfig
from lab.engine.splits import design_view
from lab.ledger import Ledger
from lab.plan import parse_plan, plan_hash
from lab.research.contract import load_research_config
from lab.research.criteria import evaluate_plan, regime_breakdown
from lab.research.logic import logic_hash
from lab.research.pipeline import (
    HIDDEN_REPORT_KEYS,
    adversarial_evidence,
    backtest_evidence,
    crosscheck_evidence,
    strip_report,
)
from lab.research.runs import start_research_run
from tests.test_mcp_server import EMA_PLAN, loader_for, small_cfg, trend_candles, write_criteria

H = plan_hash(parse_plan(EMA_PLAN))


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path: Path) -> Any:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


@pytest.fixture
def candles() -> pd.DataFrame:
    return trend_candles()  # runs past the holdout start, so a leak would be visible


def start(ledger: Ledger, tmp_path: Path, **criteria: object) -> str:
    path = write_criteria(tmp_path / f"c{len(ledger.research_runs())}.toml", **criteria)
    # small_cfg(tmp_path) has the same lab values as the ``cfg`` fixture (only data_dir,
    # which is not fingerprinted, differs), so the run's lab.toml fingerprint matches it.
    return start_research_run(
        ledger, load_research_config(path), label="pipeline", cfg=small_cfg(tmp_path)
    )


def test_strip_report_drops_hidden_keys_and_counts_trades() -> None:
    report = {
        "trades": [{"i": i} for i in range(3)],
        "equity": [1.0] * 80,
        "benchmark_equity": {"buy_and_hold": [1.0]},
        "metrics": {"net": {"sharpe": 1.2}},
        "warnings": list(range(60)),
    }
    out = strip_report(report)
    assert not HIDDEN_REPORT_KEYS & set(out)
    assert out["n_trades_listed"] == 3
    assert out["metrics"] == {"net": {"sharpe": 1.2}}
    assert out["warnings"] == {"n": 60}  # long lists compacted


def test_backtest_evidence_records_regimes_from_the_frozen_definition(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, candles: pd.DataFrame
) -> None:
    run_id = start(ledger, tmp_path, regime_window_days=30, regime_threshold=0.05)
    out = backtest_evidence(
        EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(candles)
    )
    (ev,) = ledger.evidence(run_id)
    assert ev["kind"] == "backtest" and ev["plan_hash"] == H
    assert ev["payload"] == out
    assert isinstance(out["trial_id"], int)
    # Known answer: the same breakdown computed directly with the frozen 30-day / 5% definition.
    rec = ledger.get(out["trial_id"], include_returns=True)
    assert rec.returns is not None
    history = design_view(candles.loc[candles.index < cfg.splits.holdout_start], cfg.splits)
    expected = regime_breakdown(
        history,
        rec.window_start,
        rec.window_end,
        rec.returns,
        "4h",
        window_days=30,
        threshold=0.05,
    )
    assert out["regimes"] == expected
    assert out["regimes"]["basis"].startswith("trailing 30-day simple return (180 bars)")
    regime_bars = sum(v["bars"] for v in out["regimes"]["regimes"].values())
    assert regime_bars + out["regimes"]["unlabelled_bars"] == rec.returns.shape[0]


def test_backtest_evidence_feeds_the_regime_criterion(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, candles: pd.DataFrame
) -> None:
    run_id = start(ledger, tmp_path, min_profitable_regimes=1)
    backtest_evidence(EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(candles))
    rows = {r["criterion"]: r for r in evaluate_plan(ledger, run_id, H, cfg)["rows"]}
    assert rows["min_profitable_regimes"]["status"] in {"pass", "fail"}  # never "missing"
    assert "trailing 90-day" in rows["min_profitable_regimes"]["note"]


def test_backtest_evidence_strips_hidden_keys(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, candles: pd.DataFrame
) -> None:
    run_id = start(ledger, tmp_path)
    out = backtest_evidence(
        EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(candles)
    )
    (ev,) = ledger.evidence(run_id)
    for payload in (out, ev["payload"]):
        assert not HIDDEN_REPORT_KEYS & set(payload)
        assert isinstance(payload["n_trades_listed"], int)


def test_evidence_never_loads_holdout_bars(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, candles: pd.DataFrame
) -> None:
    assert candles.index[-1] >= cfg.splits.holdout_start  # the fixture has holdout bars
    run_id = start(ledger, tmp_path)
    calls: list[pd.Timestamp] = []
    load = loader_for(candles, calls)
    backtest_evidence(EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=load)
    adversarial_evidence(EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=load)
    assert calls and all(end == cfg.splits.holdout_start for end in calls)
    assert all(r.window_kind != "holdout" for r in ledger.list(run_id=run_id, limit=10_000))


def test_adversarial_evidence_is_stripped_and_recorded(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, candles: pd.DataFrame
) -> None:
    run_id = start(ledger, tmp_path)
    out = adversarial_evidence(
        EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(candles)
    )
    (ev,) = ledger.evidence(run_id)
    assert ev["kind"] == "adversarial" and ev["plan_hash"] == H
    assert ev["payload"] == out
    assert not HIDDEN_REPORT_KEYS & set(out) and "regimes" not in out


def test_backtest_evidence_for_an_unknown_run_records_nothing(
    ledger: Ledger, cfg: LabConfig, candles: pd.DataFrame
) -> None:
    with pytest.raises(ValueError, match="never started"):
        backtest_evidence(
            EMA_PLAN, run_id="nope", cfg=cfg, ledger=ledger, loader=loader_for(candles)
        )
    assert ledger.count() == 0


# --------------------------------------------------------------------------- cross-check


def test_crosscheck_evidence_requires_backtest_evidence_first(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, candles: pd.DataFrame
) -> None:
    run_id = start(ledger, tmp_path)
    with pytest.raises(ValueError, match="no backtest evidence"):
        crosscheck_evidence(
            EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(candles)
        )
    assert ledger.evidence(run_id) == []


def test_crosscheck_evidence_unavailable_engine_reads_as_missing(
    ledger: Ledger,
    tmp_path: Path,
    cfg: LabConfig,
    candles: pd.DataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lab.crosscheck import nautilus

    def not_installed() -> Any:
        raise ImportError("nautilus_trader")

    run_id = start(ledger, tmp_path)
    backtest_evidence(EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(candles))
    monkeypatch.setattr(nautilus, "_import_nautilus", not_installed)
    out = crosscheck_evidence(
        EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(candles)
    )
    assert out["available"] is False and out["matched"] is False
    assert out["reason"] == nautilus.NOT_INSTALLED and out["logic_hash"] == logic_hash(EMA_PLAN)
    (ev,) = ledger.evidence(run_id, kind="crosscheck")
    assert ev["payload"] == out and ev["plan_hash"] == H
    row = next(
        r
        for r in evaluate_plan(ledger, run_id, H, cfg)["rows"]
        if r["criterion"] == "require_crosscheck_match"
    )
    assert row["status"] == "missing" and nautilus.NOT_INSTALLED in row["note"]


def test_crosscheck_evidence_runs_the_event_engine_on_the_design_window_only(
    ledger: Ledger, tmp_path: Path, cfg: LabConfig, candles: pd.DataFrame
) -> None:
    run_id = start(ledger, tmp_path)
    bt = backtest_evidence(
        EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(candles)
    )
    calls: list[pd.Timestamp] = []
    out = crosscheck_evidence(
        EMA_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_for(candles, calls)
    )
    assert calls == [cfg.splits.holdout_start]
    assert out["available"] is True  # nautilus_trader is a lab dependency
    assert out["n_trades_vectorbt"] == bt["metrics"]["net"]["n_trades"]
    if out["matched"]:  # agreement means every trade matched
        assert out["matched_trades"] == out["n_trades_vectorbt"] == out["n_trades_event"]
    assert isinstance(out["notes"], list) and out["notes"]
