"""Probabilistic and Deflated Sharpe ratios, MinTRL and MinBTL (PLAN.md Phase 7 How §6, Phase 11).

* PSR / MinTRL - Bailey & López de Prado (2012), "The Sharpe Ratio Efficient Frontier",
  Journal of Risk 15(2), eqs. 8, 11, 13.
* DSR - Bailey & López de Prado (2014), "The Deflated Sharpe Ratio: Correcting for Selection
  Bias, Backtest Overfitting and Non-Normality", Journal of Portfolio Management 40(5),
  eq. 5 (expected max Sharpe ``SR0``) and eq. 6 (``DSR = PSR(SR0)``).
* MinBTL - Bailey, Borwein, López de Prado & Zhu (2014), "Pseudo-Mathematics and Financial
  Charlatanism: The Effects of Backtest Overfitting on Out-of-Sample Performance", Notices of
  the AMS 61(5): the expected-maximum bound inverted for the backtest length.

Every Sharpe here is **per bar** (not annualised): the formulas are written for the per-period
statistic and its sample length. The one exception is ``min_backtest_length``, which, like the
paper, takes an annualised target Sharpe and returns years.

Reuse of ``hedgefund.backtest.significance``: ``expected_max_sharpe`` (eq. 5) is re-exported as
is (per period, no 252-day assumption). Its bracket term is also written out here as
``expected_max_z`` (the standardised expected maximum, accepting a fractional trial count)
because MinBTL needs it on its own; ``deflated_sharpe`` uses ``expected_max_z`` with the same
operation order (the clamp at 0 only acts for 1 < N < ~1.3), so its numbers are
bit-identical to the hedgeFund route for every integer N (tested).
hedgeFund's PSR and MinTRL only accept a return series, but the published worked examples are
stated as summary moments (SR, n, skew, kurtosis), so the formulas live here at moment level
(``psr_from_moments``, ``min_track_record_length_from_moments``) and the return-level functions
call them. Tests assert they equal hedgeFund's ``probabilistic_sharpe_ratio`` /
``min_track_record_length`` on the same returns, so both implementations are pinned to the
papers' numbers. ``tests/test_stats_crosscheck.py`` also checks them against ``purgedcv``.

``n_trials`` must be the run's full trial count from the ledger (every backtest, every Optuna
trial), and ``sharpe_variance`` the variance of those trials' per-bar Sharpes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from hedgefund.backtest.significance import expected_max_sharpe
from scipy import stats as sp_stats

from lab.stats.metrics import sharpe_per_bar

__all__ = [
    "DSRResult",
    "deflated_sharpe",
    "expected_max_sharpe",
    "expected_max_z",
    "min_backtest_length",
    "min_track_record_length",
    "min_track_record_length_from_moments",
    "probabilistic_sharpe",
    "psr_from_moments",
    "trial_sharpe_variance",
]


@dataclass(frozen=True)
class DSRResult:
    dsr: float  # PSR against sr0_per_bar: P(true Sharpe > the luck-only maximum)
    psr: float  # PSR against 0
    sharpe_per_bar: float
    sr0_per_bar: float  # expected max per-bar Sharpe of n_trials zero-skill trials
    n_trials: int
    sharpe_variance: float  # variance of the trials' per-bar Sharpes, as given


def _check_trial_count(n_trials: float) -> float:
    if isinstance(n_trials, bool):
        raise ValueError(f"n_trials must be a number, got {n_trials!r}")
    n = float(n_trials)
    if not np.isfinite(n):
        raise ValueError(f"n_trials must be finite, got {n_trials!r}")
    return n


def expected_max_z(n_trials: float) -> float:
    """Standardised expected maximum of ``n_trials`` independent N(0, 1) draws.

    The bracket of Bailey & López de Prado (2014) eq. 5 (Euler-Mascheroni approximation)::

        E[max Z] ~ (1 - g) * Phi^-1(1 - 1/N) + g * Phi^-1(1 - 1/(N e)),  g = 0.5772...

    ``SR0 = sqrt(V) * expected_max_z(N)``. ``n_trials`` may be fractional. ``0.0`` for
    ``N <= 1``: with at most one independent trial nothing was selected (the formula diverges
    at N = 1). Clamped at ``0.0`` from below: just above ``N = 1`` (``1 < N < ~1.3``) the
    approximation goes negative, while the expected maximum of ``N >= 1`` draws is never below
    the mean of one draw (0); the clamp keeps it non-decreasing in ``N``. Raises
    ``ValueError`` for a non-finite ``n_trials``.
    """
    n = _check_trial_count(n_trials)
    if n <= 1.0:
        return 0.0
    gamma = np.euler_gamma
    z_max = (1.0 - gamma) * sp_stats.norm.ppf(1.0 - 1.0 / n)
    z_max += gamma * sp_stats.norm.ppf(1.0 - 1.0 / (n * np.e))
    return max(0.0, float(z_max))


def min_backtest_length(n_trials: float, target_sharpe_annual: float) -> float:
    """Minimum backtest length (MinBTL) in **years**; Bailey, Borwein, López de Prado & Zhu
    (2014), Notices of the AMS 61(5).

    With ``N`` zero-skill configurations whose annualised Sharpes over ``y`` years have standard
    deviation ``1 / sqrt(y)``, the expected best annualised Sharpe is about
    ``expected_max_z(N) / sqrt(y)``. MinBTL is the length at which that luck-only maximum equals
    ``target_sharpe_annual``::

        MinBTL = (expected_max_z(N) / SR_target)^2        [years]

    Below it, an annualised Sharpe of ``SR_target`` from the best of ``N`` trials is within what
    selection alone produces. The paper also gives the simpler upper bound
    ``MinBTL < 2 ln N / SR_target^2``; this is the tighter Z form (the same one ``purgedcv``
    uses; cross-checked in ``tests/test_stats_crosscheck.py``).

    Convert to bars with ``years * bars_per_year(interval)`` (``lab.types.bars_per_year``,
    365-day crypto year), never 252.

    ``0.0`` for ``N <= 1`` (no selection); ``math.inf`` for a non-positive or non-finite
    target (no length makes a zero or negative Sharpe evidence of skill). ``n_trials`` may be
    fractional; a non-finite one raises ``ValueError``.
    """
    n = _check_trial_count(n_trials)
    target = float(target_sharpe_annual)
    if not np.isfinite(target) or target <= 0.0:
        return math.inf
    if n <= 1.0:
        return 0.0
    return float((expected_max_z(n) / target) ** 2)


def _returns_array(returns: pd.Series | np.ndarray | Sequence[float]) -> np.ndarray:
    arr = np.asarray(returns.to_numpy() if isinstance(returns, pd.Series) else returns, float)
    if arr.ndim != 1:
        raise ValueError(f"returns must be 1-D, got shape {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError("returns contains NaN or inf; per-bar returns must be finite")
    return arr


def _sharpe_variance_scale(sr: float, skew: float, kurtosis: float) -> float:
    """``1 - g3*SR + (g4 - 1)/4 * SR^2`` (Bailey & López de Prado 2012 eq. 8, raw kurtosis)."""
    return 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr**2


def psr_from_moments(
    sharpe_per_bar: float,
    n_obs: float,
    skew: float,
    kurtosis: float,
    benchmark_sharpe_per_bar: float = 0.0,
) -> float:
    """PSR from summary moments, Bailey & López de Prado (2012) eq. 11::

        PSR(SR*) = Phi( (SR - SR*) * sqrt(n - 1) / sqrt(1 - g3*SR + (g4 - 1)/4 * SR^2) )

    ``sharpe_per_bar`` and ``benchmark_sharpe_per_bar`` are per period (not annualised),
    ``kurtosis`` is raw (3 for a normal), ``n_obs`` the number of periods (may be fractional,
    e.g. a MinTRL). NaN when any input is non-finite, ``n_obs <= 1``, or the variance term is
    not positive (the asymptotic formula breaks down).
    """
    vals = (sharpe_per_bar, n_obs, skew, kurtosis, benchmark_sharpe_per_bar)
    if not all(np.isfinite(v) for v in vals) or n_obs <= 1:
        return float("nan")
    scale = _sharpe_variance_scale(sharpe_per_bar, skew, kurtosis)
    if scale <= 0:
        return float("nan")
    z = (sharpe_per_bar - benchmark_sharpe_per_bar) * np.sqrt(n_obs - 1.0) / np.sqrt(scale)
    return float(sp_stats.norm.cdf(z))


def min_track_record_length_from_moments(
    sharpe_per_bar: float,
    skew: float,
    kurtosis: float,
    target_sharpe_per_bar: float = 0.0,
    confidence: float = 0.95,
) -> float:
    """Periods needed before PSR(target) reaches ``confidence``; Bailey & López de Prado (2012)
    eq. 13::

        MinTRL = 1 + (1 - g3*SR + (g4 - 1)/4 * SR^2) * (Z_conf / (SR - SR*))^2

    ``Z_conf = Phi^-1(confidence)`` (one-sided). inf when SR <= SR*; NaN when an input is
    non-finite or the variance term is not positive. ``confidence`` must lie in (0, 1).
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be strictly between 0 and 1, got {confidence!r}")
    vals = (sharpe_per_bar, skew, kurtosis, target_sharpe_per_bar)
    if not all(np.isfinite(v) for v in vals):
        return float("nan")
    scale = _sharpe_variance_scale(sharpe_per_bar, skew, kurtosis)
    if scale <= 0:
        return float("nan")
    if sharpe_per_bar <= target_sharpe_per_bar:
        return float("inf")
    z = sp_stats.norm.ppf(confidence)
    return float(1.0 + scale * (z / (sharpe_per_bar - target_sharpe_per_bar)) ** 2)


def _moments(arr: np.ndarray) -> tuple[float, float, float, int]:
    """Per-bar ``(SR, skew, raw kurtosis, n)``: SR with ddof=1, biased sample skew/kurtosis
    (scipy defaults, as in eq. 8 and in hedgeFund's ``_sharpe_moments``). NaNs if undefined."""
    n = int(arr.size)
    nan = float("nan")
    if n < 2:
        return nan, nan, nan, n
    std = float(arr.std(ddof=1))
    if std == 0 or not np.isfinite(std):
        return nan, nan, nan, n
    return (
        float(arr.mean() / std),
        float(sp_stats.skew(arr)),
        float(sp_stats.kurtosis(arr, fisher=False)),
        n,
    )


def probabilistic_sharpe(
    returns: pd.Series | np.ndarray | Sequence[float], benchmark_sharpe_per_bar: float = 0.0
) -> float:
    """P(true per-bar Sharpe > ``benchmark_sharpe_per_bar``) given skew, kurtosis and length.

    ``psr_from_moments`` on the sample moments; equals hedgeFund's
    ``probabilistic_sharpe_ratio`` (tested). NaN with fewer than 2 bars, zero variance, or a
    non-positive variance term.
    """
    sr, g3, g4, n = _moments(_returns_array(returns))
    return psr_from_moments(sr, n, g3, g4, benchmark_sharpe_per_bar)


def min_track_record_length(
    returns: pd.Series | np.ndarray | Sequence[float],
    target_sharpe_per_bar: float = 0.0,
    confidence: float = 0.95,
) -> float:
    """MinTRL in bars for ``returns`` (see ``min_track_record_length_from_moments``)."""
    sr, g3, g4, _ = _moments(_returns_array(returns))
    return min_track_record_length_from_moments(sr, g3, g4, target_sharpe_per_bar, confidence)


def trial_sharpe_variance(sharpes_per_bar: Sequence[float] | np.ndarray) -> float:
    """Sample variance (ddof=1) of the finite per-bar trial Sharpes; NaN with < 2 of them."""
    arr = np.asarray(sharpes_per_bar, dtype=float).ravel()
    finite = arr[np.isfinite(arr)]
    if finite.size < 2:
        return float("nan")
    return float(finite.var(ddof=1))


def deflated_sharpe(
    returns: pd.Series | np.ndarray | Sequence[float], *, n_trials: int, sharpe_variance: float
) -> DSRResult:
    """Deflated Sharpe ratio of the selected strategy's per-bar ``returns``.

    Conventions:

    * ``n_trials < 1`` raises ``ValueError``; a negative finite ``sharpe_variance`` too.
    * ``n_trials == 1``: nothing was selected, so ``SR0 = 0`` and ``DSR == PSR``.
    * ``sharpe_variance`` NaN/inf/0 (e.g. fewer than two finite trial Sharpes): the luck-only
      maximum cannot be estimated, so ``SR0 = 0`` and ``DSR == PSR``. This is the lenient
      reading; callers judging a multi-trial run must check ``sharpe_variance`` themselves.
    """
    if int(n_trials) != n_trials or n_trials < 1:
        raise ValueError(f"n_trials must be an integer >= 1, got {n_trials!r}")
    var = float(sharpe_variance)
    if np.isfinite(var) and var < 0:
        raise ValueError(f"sharpe_variance must be >= 0, got {sharpe_variance}")
    arr = _returns_array(returns)
    n_trials = int(n_trials)
    sr0 = (
        0.0
        if n_trials == 1 or not np.isfinite(var) or var == 0
        else float(np.sqrt(var) * expected_max_z(n_trials))
    )
    sr, g3, g4, n = _moments(arr)
    psr = psr_from_moments(sr, n, g3, g4, 0.0)
    dsr = psr if sr0 == 0.0 else psr_from_moments(sr, n, g3, g4, sr0)
    return DSRResult(
        dsr=float(dsr),
        psr=float(psr),
        sharpe_per_bar=sharpe_per_bar(arr),
        sr0_per_bar=float(sr0),
        n_trials=n_trials,
        sharpe_variance=var,
    )
