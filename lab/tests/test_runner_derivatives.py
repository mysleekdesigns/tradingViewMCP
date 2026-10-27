"""Runner + derivatives: default_loader attaches drv_* columns (cached), plans without
derivatives are unchanged, and a missing download fails before any ledger row (PLAN.md
Phase 12)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import lab.runner as runner
from lab.config import LabConfig
from lab.data.derivatives import build_manifest
from lab.data.derivatives_columns import attach_derivatives
from lab.data.store import load_candles
from lab.ledger import Ledger
from lab.registry.derivatives import DERIVATIVES_COLUMNS, DRV_FUNDING_RATE, DRV_OI
from lab.research.contract import load_research_config
from lab.research.pipeline import backtest_evidence, crosscheck_evidence
from lab.research.runs import start_research_run
from lab.runner import (
    DataUnavailableError,
    default_loader,
    run_adversarial_report,
    run_backtest_report,
)
from tests import test_mcp_server as mcp_helpers
from tests.synthetic import make_candles, write_derivatives_archive
from tests.test_data_derivatives_columns import write_all, write_funding
from tests.test_runner import EMA_PLAN, small_cfg

# 2018-07 .. ~2020-01 (as tests/test_runner.py): warm-up history before design_start.
CANDLES = make_candles(3300, seed=17, start="2018-07-01", price=6000.0, vol=0.012)

GATED_PLAN: dict[str, Any] = {
    **EMA_PLAN,
    "name": "ema_funding_gate",
    "features": {**EMA_PLAN["features"], "fz": {"funding_zscore": {"period": 30}}},
    "long_entry": "crosses_above(ema_fast, ema_slow) and fz < 2",
}


def with_drv(candles: pd.DataFrame, *, seed: int = 0, nan_share: float = 0.2) -> pd.DataFrame:
    """Candles plus every drv_* column (random values, some NaN gaps)."""
    rng = np.random.default_rng(seed)
    out = candles.copy()
    for column in DERIVATIVES_COLUMNS:
        values = rng.normal(0.0, 1.0, len(out))
        values[rng.uniform(size=len(out)) < nan_share] = np.nan
        out[column] = values
    return out


def loader_of(candles: pd.DataFrame) -> Any:
    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        return candles.loc[candles.index < end]

    return load


def without_timing(report: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in report.items() if k != "elapsed_ms"}


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


# --------------------------------------------------------------------------- unchanged results


def test_plan_without_derivatives_is_bit_identical_with_columns_attached(
    cfg: LabConfig, tmp_path: Path
) -> None:
    with Ledger(tmp_path / "a.db") as a, Ledger(tmp_path / "b.db") as b:
        plain = run_backtest_report(
            EMA_PLAN, cfg=cfg, ledger=a, run_id="t", loader=loader_of(CANDLES)
        )
        attached = run_backtest_report(
            EMA_PLAN, cfg=cfg, ledger=b, run_id="t", loader=loader_of(with_drv(CANDLES))
        )
        assert without_timing(attached) == without_timing(plain)
        assert a.count("t") == b.count("t")
        rows_a = [a.get(i, include_returns=True) for i in range(1, a.count("t") + 1)]
        rows_b = [b.get(i, include_returns=True) for i in range(1, b.count("t") + 1)]
        for ra, rb in zip(rows_a, rows_b, strict=True):
            assert ra.metrics_json == rb.metrics_json
            if ra.returns is not None:
                np.testing.assert_array_equal(ra.returns, rb.returns)


def test_adversarial_report_is_bit_identical_with_columns_attached(
    cfg: LabConfig, tmp_path: Path
) -> None:
    with Ledger(tmp_path / "a.db") as a, Ledger(tmp_path / "b.db") as b:
        plain = run_adversarial_report(EMA_PLAN, cfg=cfg, ledger=a, loader=loader_of(CANDLES))
        attached = run_adversarial_report(
            EMA_PLAN, cfg=cfg, ledger=b, loader=loader_of(with_drv(CANDLES))
        )
    assert without_timing(attached) == without_timing(plain)


# --------------------------------------------------------------------------- preflight


def test_missing_derivatives_column_raises_before_any_ledger_row(
    cfg: LabConfig, tmp_path: Path
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        for run in (run_backtest_report, run_adversarial_report):
            with pytest.raises(DataUnavailableError) as info:
                run(GATED_PLAN, cfg=cfg, ledger=ledger, loader=loader_of(CANDLES))
            msg = str(info.value)
            assert DRV_FUNDING_RATE in msg
            assert (
                "uv run python -m lab.data.derivatives download BTCUSDT --datasets fundingRate"
                in msg
            )
            assert ledger.count() == 0


def test_all_nan_derivatives_column_in_the_design_window_raises(
    cfg: LabConfig, tmp_path: Path
) -> None:
    candles = with_drv(CANDLES)
    candles[DRV_FUNDING_RATE] = np.nan
    with Ledger(tmp_path / "l.db") as ledger:
        with pytest.raises(DataUnavailableError, match="drv_funding_rate has no value"):
            run_backtest_report(GATED_PLAN, cfg=cfg, ledger=ledger, loader=loader_of(candles))
        assert ledger.count() == 0


def test_gated_plan_runs_when_its_columns_are_present(cfg: LabConfig, tmp_path: Path) -> None:
    # no gaps here: funding_zscore is NaN for any window that contains a gap
    full = with_drv(CANDLES, nan_share=0.0)
    # the archive manifest the shared derivatives scored start reads (every series, from 2018-06)
    write_derivatives_archive(cfg.derivatives_dir, first="2018-06-01", end="2020-06-01")
    with Ledger(tmp_path / "l.db") as ledger:
        gated = run_backtest_report(GATED_PLAN, cfg=cfg, ledger=ledger, loader=loader_of(full))
        ungated = run_backtest_report(EMA_PLAN, cfg=cfg, ledger=ledger, loader=loader_of(full))
    n_gated = gated["metrics"]["net"]["n_trades"]
    assert 0 < n_gated <= ungated["metrics"]["net"]["n_trades"]


def test_crosscheck_evidence_refuses_missing_inputs_and_records_nothing(
    tmp_path: Path,
) -> None:
    cfg = mcp_helpers.small_cfg(tmp_path)
    full = with_drv(mcp_helpers.trend_candles(), nan_share=0.0)
    full[DRV_FUNDING_RATE] = np.abs(full[DRV_FUNDING_RATE]) * 1e-4
    write_derivatives_archive(cfg.derivatives_dir, first="2018-06-01", end="2020-06-01")
    with Ledger(tmp_path / "l.db") as ledger:
        criteria = mcp_helpers.write_criteria(tmp_path / "c.toml")
        run_id = start_research_run(ledger, load_research_config(criteria), label="drv", cfg=cfg)
        backtest_evidence(GATED_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_of(full))
        n_rows, n_ev = ledger.count(run_id), len(ledger.evidence(run_id))
        bare = full.drop(columns=list(DERIVATIVES_COLUMNS))
        with pytest.raises(DataUnavailableError, match="drv_funding_rate"):
            crosscheck_evidence(
                GATED_PLAN, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader_of(bare)
            )
        assert ledger.count(run_id) == n_rows
        assert len(ledger.evidence(run_id)) == n_ev


# --------------------------------------------------------------------------- default_loader


def write_candles(cfg: LabConfig, candles: pd.DataFrame) -> None:
    folder = cfg.parquet_dir / "BTCUSDT" / "4h"
    folder.mkdir(parents=True, exist_ok=True)
    for month, part in candles.groupby(candles.index.strftime("%Y-%m")):
        part.to_parquet(folder / f"{month}.parquet")


@pytest.fixture
def disk_cfg(tmp_path: Path) -> LabConfig:
    base = small_cfg(tmp_path)
    cfg = replace(base, derivatives=replace(base.derivatives, stale_after_intervals=2.0))
    write_candles(cfg, make_candles(120, seed=4, start="2024-02-01", price=40_000.0))
    write_all(cfg.derivatives_dir)
    return cfg


END = pd.Timestamp("2024-02-15", tz="UTC")


def test_default_loader_attaches_derivatives(disk_cfg: LabConfig) -> None:
    got = default_loader(disk_cfg)("BTCUSDT", "4h", END)
    bare = load_candles("BTCUSDT", "4h", parquet_dir=disk_cfg.parquet_dir, end=END)
    want = attach_derivatives(
        bare,
        "BTCUSDT",
        "4h",
        data_dir=disk_cfg.derivatives_dir,
        stale_after_intervals=disk_cfg.derivatives.stale_after_intervals,
    )
    pd.testing.assert_frame_equal(got, want)
    assert set(DERIVATIVES_COLUMNS) <= set(got.columns)
    assert got.index[-1] < END


def counting(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = runner.derivative_columns

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "derivative_columns", spy)
    return calls


def test_default_loader_caches_aligned_columns_per_manifest(
    disk_cfg: LabConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_manifest(disk_cfg.derivatives_dir, gap_factor=1.5)
    calls = counting(monkeypatch)
    load = default_loader(disk_cfg)
    first = load("BTCUSDT", "4h", END)
    second = load("BTCUSDT", "4h", END)
    assert len(calls) == 1
    pd.testing.assert_frame_equal(first, second)
    first.loc[first.index[0], DRV_OI] = -1.0  # callers own their copy; the cache is unaffected
    assert load("BTCUSDT", "4h", END).loc[first.index[0], DRV_OI] != -1.0
    load("BTCUSDT", "4h", END - pd.Timedelta(days=1))  # another end: its own entry
    assert len(calls) == 2
    # new data + rebuilt manifest -> a new key, values recomputed
    write_funding(disk_cfg.derivatives_dir, seed=42)
    build_manifest(disk_cfg.derivatives_dir, gap_factor=1.5)
    third = load("BTCUSDT", "4h", END)
    assert len(calls) == 3
    assert not np.array_equal(
        third[DRV_FUNDING_RATE].to_numpy(), first[DRV_FUNDING_RATE].to_numpy(), equal_nan=True
    )


def test_default_loader_without_manifest_does_not_cache(
    disk_cfg: LabConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = counting(monkeypatch)
    load = default_loader(disk_cfg)
    load("BTCUSDT", "4h", END)
    load("BTCUSDT", "4h", END)
    assert len(calls) == 2
