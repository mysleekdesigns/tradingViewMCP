"""Load ``config/lab.toml`` into typed settings. I/O lives here, never in engine/registry/stats."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from lab.types import CostModel

LAB_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = LAB_ROOT / "config" / "lab.toml"


@dataclass(frozen=True)
class SplitConfig:
    design_start: pd.Timestamp
    holdout_start: pd.Timestamp  # bars at or after this are the locked holdout
    holdout_budget: int
    wf_train_months: int
    wf_test_months: int
    wf_step_months: int
    wf_anchored: bool


@dataclass(frozen=True)
class BenchmarkConfig:
    vol_target_annual: float
    vol_lookback_days: int
    vol_max_leverage: float
    random_entry_paths: int
    vol_min_rebalance: float


@dataclass(frozen=True)
class StatsConfig:
    bootstrap_samples: int
    bootstrap_mean_block: int
    confidence: float
    pbo_splits: int
    neighbourhood_steps: tuple[float, ...]
    neighbourhood_max_trials: int
    adversarial_delays: tuple[int, ...]
    monte_carlo_paths: int
    permutation_paths: int


@dataclass(frozen=True)
class LabConfig:
    data_dir: Path
    archive_base_url: str
    costs: CostModel
    stress_factors: tuple[float, ...]
    init_cash: float
    splits: SplitConfig
    benchmarks: BenchmarkConfig
    stats: StatsConfig
    optuna_trials: int
    optuna_seed: int
    optuna_min_trades: int
    api_host: str
    api_port: int

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "klines"


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> LabConfig:
    """Parse the TOML at ``path``; relative ``data.dir`` resolves against the lab root."""
    raw = tomllib.loads(Path(path).read_text())
    data_dir = Path(raw["data"]["dir"])
    if not data_dir.is_absolute():
        data_dir = LAB_ROOT / data_dir
    c, s, b, st = raw["costs"], raw["splits"], raw["benchmarks"], raw["stats"]
    return LabConfig(
        data_dir=data_dir,
        archive_base_url=raw["data"]["archive_base_url"],
        costs=CostModel(
            c["fee_rate"], c["slippage_rate"], c["slippage_vol_mult"], c["slippage_cap"]
        ),
        stress_factors=tuple(float(x) for x in c["stress_factors"]),
        init_cash=float(raw["engine"]["init_cash"]),
        splits=SplitConfig(
            design_start=pd.Timestamp(s["design_start"], tz="UTC"),
            holdout_start=pd.Timestamp(s["holdout_start"], tz="UTC"),
            holdout_budget=int(s["holdout_budget"]),
            wf_train_months=int(s["wf_train_months"]),
            wf_test_months=int(s["wf_test_months"]),
            wf_step_months=int(s["wf_step_months"]),
            wf_anchored=bool(s["wf_anchored"]),
        ),
        benchmarks=BenchmarkConfig(
            vol_target_annual=float(b["vol_target_annual"]),
            vol_lookback_days=int(b["vol_lookback_days"]),
            vol_max_leverage=float(b["vol_max_leverage"]),
            random_entry_paths=int(b["random_entry_paths"]),
            vol_min_rebalance=float(b["vol_min_rebalance"]),
        ),
        stats=StatsConfig(
            bootstrap_samples=int(st["bootstrap_samples"]),
            bootstrap_mean_block=int(st["bootstrap_mean_block"]),
            confidence=float(st["confidence"]),
            pbo_splits=int(st["pbo_splits"]),
            neighbourhood_steps=tuple(float(x) for x in st["neighbourhood_steps"]),
            neighbourhood_max_trials=int(st["neighbourhood_max_trials"]),
            adversarial_delays=tuple(int(x) for x in st["adversarial_delays"]),
            monte_carlo_paths=int(st["monte_carlo_paths"]),
            permutation_paths=int(st["permutation_paths"]),
        ),
        optuna_trials=int(raw["optuna"]["n_trials"]),
        optuna_seed=int(raw["optuna"]["seed"]),
        optuna_min_trades=int(raw["optuna"]["min_trades"]),
        api_host=raw["api"]["host"],
        api_port=int(raw["api"]["port"]),
    )
