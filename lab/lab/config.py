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
class ResearchGuardConfig:
    """Phase 8 research guards that judge whether two plans are the same hypothesis."""

    family_return_corr: float  # design-window return correlation at/above which plans are kin
    family_min_overlap_bars: int = 250  # common (coarse) bars needed to correlate two series
    min_variance_plans: int = 10  # commensurable plans needed for DSR's V[SR]
    min_pbo_trials: int = 16  # usable Optuna trials a sweep needs for its CSCV PBO
    # correlation at/above which ANOTHER family logic's hand-tuned rule literals also block this
    # plan's walk-forward (the plan's own logic always counts)
    literal_lineage_corr: float = 0.95


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
    research: ResearchGuardConfig

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
        research=ResearchGuardConfig(
            family_return_corr=_unit_interval(
                raw["research"]["family_return_corr"], "research.family_return_corr"
            ),
            family_min_overlap_bars=_min_int(
                raw["research"]["family_min_overlap_bars"], "research.family_min_overlap_bars", 3
            ),
            min_variance_plans=_min_int(
                raw["research"]["min_variance_plans"], "research.min_variance_plans", 2
            ),
            min_pbo_trials=_min_int(
                raw["research"]["min_pbo_trials"], "research.min_pbo_trials", 2
            ),
            literal_lineage_corr=_unit_interval(
                raw["research"]["literal_lineage_corr"], "research.literal_lineage_corr"
            ),
        ),
    )


def _unit_interval(value: object, name: str) -> float:
    """``value`` as a float in (0, 1]; ``ValueError`` naming ``name`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number in (0, 1], got {value!r}")
    v = float(value)
    if not 0.0 < v <= 1.0:
        raise ValueError(f"{name} must be in (0, 1], got {v}")
    return v


def _min_int(value: object, name: str, low: int) -> int:
    """``value`` as an int >= ``low``; ``ValueError`` naming ``name`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer >= {low}, got {value!r}")
    if value < low:
        raise ValueError(f"{name} must be >= {low}, got {value}")
    return int(value)
