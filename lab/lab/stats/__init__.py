"""Pure statistics for the lab: metrics, PSR/DSR, MinTRL/MinBTL, effective trial count, PBO
(CSCV), CPCV paths, stationary bootstrap, Monte Carlo trade reshuffle and candle permutation.
Results are historical, not forward-looking."""

from lab.stats.bootstrap import BootstrapCI, stationary_bootstrap_ci, stationary_bootstrap_indices
from lab.stats.cpcv import CpcvResult, cpcv_paths
from lab.stats.effective_n import (
    EffectiveN,
    effective_n_trials,
    pairwise_correlation,
    participation_ratio,
)
from lab.stats.metrics import compute_metrics, metrics_from_returns, sharpe_per_bar
from lab.stats.montecarlo import MonteCarloResult, trade_reshuffle
from lab.stats.pbo import PBOResult, pbo_cscv
from lab.stats.permutation import percentile_rank, permutation_p_value, permute_candles
from lab.stats.sharpe import (
    DSRResult,
    deflated_sharpe,
    expected_max_sharpe,
    expected_max_z,
    min_backtest_length,
    min_track_record_length,
    min_track_record_length_from_moments,
    probabilistic_sharpe,
    psr_from_moments,
    trial_sharpe_variance,
)

__all__ = [
    "BootstrapCI",
    "CpcvResult",
    "DSRResult",
    "EffectiveN",
    "MonteCarloResult",
    "PBOResult",
    "compute_metrics",
    "cpcv_paths",
    "deflated_sharpe",
    "effective_n_trials",
    "expected_max_sharpe",
    "expected_max_z",
    "metrics_from_returns",
    "min_backtest_length",
    "min_track_record_length",
    "min_track_record_length_from_moments",
    "pairwise_correlation",
    "participation_ratio",
    "pbo_cscv",
    "percentile_rank",
    "permutation_p_value",
    "permute_candles",
    "probabilistic_sharpe",
    "psr_from_moments",
    "sharpe_per_bar",
    "stationary_bootstrap_ci",
    "stationary_bootstrap_indices",
    "trade_reshuffle",
    "trial_sharpe_variance",
]
