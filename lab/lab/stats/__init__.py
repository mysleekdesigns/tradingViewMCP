"""Pure statistics for the lab: metrics, PSR/DSR, PBO (CSCV), stationary bootstrap,
Monte Carlo trade reshuffle and candle permutation. Results are historical, not
forward-looking."""

from lab.stats.bootstrap import BootstrapCI, stationary_bootstrap_ci, stationary_bootstrap_indices
from lab.stats.metrics import compute_metrics, metrics_from_returns, sharpe_per_bar
from lab.stats.montecarlo import MonteCarloResult, trade_reshuffle
from lab.stats.pbo import PBOResult, pbo_cscv
from lab.stats.permutation import percentile_rank, permutation_p_value, permute_candles
from lab.stats.sharpe import (
    DSRResult,
    deflated_sharpe,
    expected_max_sharpe,
    probabilistic_sharpe,
    trial_sharpe_variance,
)

__all__ = [
    "BootstrapCI",
    "DSRResult",
    "MonteCarloResult",
    "PBOResult",
    "compute_metrics",
    "deflated_sharpe",
    "expected_max_sharpe",
    "metrics_from_returns",
    "pbo_cscv",
    "percentile_rank",
    "permutation_p_value",
    "permute_candles",
    "probabilistic_sharpe",
    "sharpe_per_bar",
    "stationary_bootstrap_ci",
    "stationary_bootstrap_indices",
    "trade_reshuffle",
    "trial_sharpe_variance",
]
