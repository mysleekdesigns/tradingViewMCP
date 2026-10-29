"""Pure statistics for the lab: metrics, PSR/DSR, MinTRL/MinBTL, effective trial count, PBO
(CSCV), CPCV paths, stationary bootstrap, Monte Carlo trade reshuffle, candle permutation,
drawdown statistics (CDaR, episodes, exposure-matched benchmark) and paired bootstrap tests.
Results are historical, not forward-looking."""

from lab.stats.bootstrap import BootstrapCI, stationary_bootstrap_ci, stationary_bootstrap_indices
from lab.stats.cpcv import CpcvResult, cpcv_paths
from lab.stats.drawdown import (
    DrawdownEpisode,
    cdar,
    cdar_from_drawdowns,
    count_drawdown_episodes,
    drawdown_episodes,
    drawdown_series,
    exposure_matched_returns,
)
from lab.stats.effective_n import (
    EffectiveN,
    effective_n_trials,
    pairwise_correlation,
    participation_ratio,
)
from lab.stats.metrics import compute_metrics, metrics_from_returns, sharpe_per_bar
from lab.stats.montecarlo import MonteCarloResult, trade_reshuffle
from lab.stats.paired import (
    PairedBootstrap,
    bootstrap_block_bars,
    break_even_cost_multiple,
    cdar_difference_test,
    paired_stationary_bootstrap,
    sharpe_difference_test,
)
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
    "DrawdownEpisode",
    "EffectiveN",
    "MonteCarloResult",
    "PBOResult",
    "PairedBootstrap",
    "bootstrap_block_bars",
    "break_even_cost_multiple",
    "cdar",
    "cdar_difference_test",
    "cdar_from_drawdowns",
    "compute_metrics",
    "count_drawdown_episodes",
    "cpcv_paths",
    "deflated_sharpe",
    "drawdown_episodes",
    "drawdown_series",
    "effective_n_trials",
    "expected_max_sharpe",
    "expected_max_z",
    "exposure_matched_returns",
    "metrics_from_returns",
    "min_backtest_length",
    "min_track_record_length",
    "min_track_record_length_from_moments",
    "paired_stationary_bootstrap",
    "pairwise_correlation",
    "participation_ratio",
    "pbo_cscv",
    "percentile_rank",
    "permutation_p_value",
    "permute_candles",
    "probabilistic_sharpe",
    "psr_from_moments",
    "sharpe_difference_test",
    "sharpe_per_bar",
    "stationary_bootstrap_ci",
    "stationary_bootstrap_indices",
    "trade_reshuffle",
    "trial_sharpe_variance",
]
