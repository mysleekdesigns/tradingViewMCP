"""Load ``config/lab.toml`` into typed settings. I/O lives here, never in engine/registry/stats."""

from __future__ import annotations

import math
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
class UniverseConfig:
    quote: str
    interval: str
    archive_list_url: str
    top_n: int
    volume_window_days: int
    min_history_days: int
    relist_gap_days: int
    relist_jump: float
    delisted_after_days: int
    exclude_bases: tuple[str, ...]
    # a coin's target weight is capped at this multiple of its share of eligible trailing
    # dollar volume (portfolio runs; not a universe-build setting)
    liquidity_cap_mult: float


@dataclass(frozen=True)
class PortfolioConfig:
    """``[portfolio]``: settings of multi-coin portfolio research runs (lab.portfolio_runner)."""

    impact_coef: float  # square-root impact Y
    impact_aum_usd: float  # fixed book size the participation is sized on (not current equity)
    impact_adv_days: int
    impact_vol_days: int
    impact_cap: float  # max impact fraction per fill, in [0, 1)
    min_rebalance: float  # engine rebalance band (absolute weight)
    alpha_rolling_days: int  # rolling alpha/beta window to BTC
    null_paths: int  # random-selection null paths
    # odd band of liquidity ranks the null draws each replaced name from (centred on the
    # strategy name's rank; lab.evaluate.portfolio_benchmarks, nearest-rank matching)
    null_liquidity_window: int
    neighbourhood_max_trials: int  # base + every +/-pct neighbour of the portfolio tunables


@dataclass(frozen=True)
class DerivativesConfig:
    """``[derivatives]``: the USD-M futures public archive (lab.data.derivatives) and alignment."""

    archive_base_url: str = "https://data.binance.vision/data/futures/um"
    # at a bar close, a value older than this many of its own sampling intervals is a gap
    stale_after_intervals: float = 2.0
    # manifest: spacing above this multiple of the expected step is recorded as a gap
    gap_factor: float = 1.5
    # a gated plan is refused when its derivatives-free entry clauses alone are true on at least
    # this share of the scored design bars (the price clause is then not doing the selecting)
    gate_base_max_true_frac: float = 0.9
    # derivatives plans on a (symbol, interval) are all scored from ONE start: the latest first
    # timestamp of its derivatives series plus this many days (room for look-backs; a
    # derivatives feature's period x bar length must fit inside it)
    warmup_days: float = 30.0


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
    universe: UniverseConfig
    portfolio: PortfolioConfig
    derivatives: DerivativesConfig = DerivativesConfig()

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "klines"

    @property
    def universe_dir(self) -> Path:
        return self.data_dir / "universe"

    @property
    def derivatives_dir(self) -> Path:
        return self.data_dir / "derivatives"


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> LabConfig:
    """Parse the TOML at ``path``; relative ``data.dir`` resolves against the lab root."""
    raw = tomllib.loads(Path(path).read_text())
    data_dir = Path(raw["data"]["dir"])
    if not data_dir.is_absolute():
        data_dir = LAB_ROOT / data_dir
    c, s, b, st = raw["costs"], raw["splits"], raw["benchmarks"], raw["stats"]
    cfg = LabConfig(
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
        universe=_universe(raw["universe"]),
        portfolio=_portfolio(raw["portfolio"]),
        derivatives=_derivatives(raw.get("derivatives", {})),
    )
    _check_sell_price_positive(cfg)
    return cfg


def _check_sell_price_positive(cfg: LabConfig) -> None:
    """A portfolio sell fills at ``open * (1 - slippage - impact)``: both caps, at the largest
    cost-stress factor, must stay below 1 or a stressed sell price could go to zero or below."""
    worst = max((1.0, *cfg.stress_factors))
    total = (cfg.costs.slippage_cap + cfg.portfolio.impact_cap) * worst
    if not total < 1.0:
        raise ValueError(
            f"costs.slippage_cap ({cfg.costs.slippage_cap:g}) + portfolio.impact_cap "
            f"({cfg.portfolio.impact_cap:g}) at the largest cost stress x{worst:g} is {total:g}; "
            "it must stay below 1 so a sell fill price stays positive"
        )


def _portfolio(p: dict) -> PortfolioConfig:
    return PortfolioConfig(
        impact_coef=_non_negative(p["impact_coef"], "portfolio.impact_coef"),
        impact_aum_usd=_positive(p["impact_aum_usd"], "portfolio.impact_aum_usd"),
        impact_adv_days=_min_int(p["impact_adv_days"], "portfolio.impact_adv_days", 1),
        impact_vol_days=_min_int(p["impact_vol_days"], "portfolio.impact_vol_days", 2),
        impact_cap=_fraction_below_one(p["impact_cap"], "portfolio.impact_cap"),
        min_rebalance=_fraction_below_one(p["min_rebalance"], "portfolio.min_rebalance"),
        alpha_rolling_days=_min_int(p["alpha_rolling_days"], "portfolio.alpha_rolling_days", 2),
        null_paths=_min_int(p["null_paths"], "portfolio.null_paths", 1),
        null_liquidity_window=_odd_int(
            p["null_liquidity_window"], "portfolio.null_liquidity_window"
        ),
        neighbourhood_max_trials=_min_int(
            p["neighbourhood_max_trials"], "portfolio.neighbourhood_max_trials", 1
        ),
    )


def _derivatives(d: dict) -> DerivativesConfig:
    """``[derivatives]``; a missing key falls back to the ``DerivativesConfig`` default."""
    default = DerivativesConfig()
    url = d.get("archive_base_url", default.archive_base_url)
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValueError(f"derivatives.archive_base_url must be an http(s) URL, got {url!r}")
    return DerivativesConfig(
        archive_base_url=url,
        stale_after_intervals=_at_least_one(
            d.get("stale_after_intervals", default.stale_after_intervals),
            "derivatives.stale_after_intervals",
        ),
        gap_factor=_at_least_one(d.get("gap_factor", default.gap_factor), "derivatives.gap_factor"),
        gate_base_max_true_frac=_unit_interval(
            d.get("gate_base_max_true_frac", default.gate_base_max_true_frac),
            "derivatives.gate_base_max_true_frac",
        ),
        warmup_days=_at_least_one(
            d.get("warmup_days", default.warmup_days), "derivatives.warmup_days"
        ),
    )


def _at_least_one(value: object, name: str) -> float:
    v = _number(value, name)
    if v < 1.0:
        raise ValueError(f"{name} must be >= 1, got {v}")
    return v


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number, got {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return v


def _non_negative(value: object, name: str) -> float:
    v = _number(value, name)
    if v < 0:
        raise ValueError(f"{name} must be >= 0, got {v}")
    return v


def _positive(value: object, name: str) -> float:
    v = _number(value, name)
    if v <= 0:
        raise ValueError(f"{name} must be > 0, got {v}")
    return v


def _fraction_below_one(value: object, name: str) -> float:
    v = _number(value, name)
    if not 0.0 <= v < 1.0:
        raise ValueError(f"{name} must be in [0, 1), got {v}")
    return v


def _universe(u: dict) -> UniverseConfig:
    cfg = UniverseConfig(
        quote=str(u["quote"]).upper(),
        interval=str(u["interval"]),
        archive_list_url=str(u["archive_list_url"]),
        top_n=_min_int(u["top_n"], "universe.top_n", 1),
        volume_window_days=_min_int(u["volume_window_days"], "universe.volume_window_days", 1),
        min_history_days=_min_int(u["min_history_days"], "universe.min_history_days", 0),
        relist_gap_days=_min_int(u["relist_gap_days"], "universe.relist_gap_days", 1),
        relist_jump=_above_one(u["relist_jump"], "universe.relist_jump"),
        delisted_after_days=_min_int(u["delisted_after_days"], "universe.delisted_after_days", 0),
        exclude_bases=tuple(str(b).upper() for b in u["exclude_bases"]),
        liquidity_cap_mult=_positive(u["liquidity_cap_mult"], "universe.liquidity_cap_mult"),
    )
    if cfg.min_history_days < cfg.volume_window_days:
        raise ValueError(
            f"universe.min_history_days ({cfg.min_history_days}) must be >= "
            f"universe.volume_window_days ({cfg.volume_window_days}), or launch-week volume "
            "decides a new coin's rank"
        )
    return cfg


def _unit_interval(value: object, name: str) -> float:
    """``value`` as a float in (0, 1]; ``ValueError`` naming ``name`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number in (0, 1], got {value!r}")
    v = float(value)
    if not 0.0 < v <= 1.0:
        raise ValueError(f"{name} must be in (0, 1], got {v}")
    return v


def _above_one(value: object, name: str) -> float:
    """``value`` as a float > 1; ``ValueError`` naming ``name`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not float(value) > 1.0:
        raise ValueError(f"{name} must be a number > 1, got {value!r}")
    return float(value)


def _odd_int(value: object, name: str) -> int:
    """``value`` as an odd int >= 1; ``ValueError`` naming ``name`` otherwise."""
    out = _min_int(value, name, 1)
    if out % 2 == 0:
        raise ValueError(f"{name} must be odd (a band centred on one rank), got {out}")
    return out


def _min_int(value: object, name: str, low: int) -> int:
    """``value`` as an int >= ``low``; ``ValueError`` naming ``name`` otherwise."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer >= {low}, got {value!r}")
    if value < low:
        raise ValueError(f"{name} must be >= {low}, got {value}")
    return int(value)
