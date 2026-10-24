"""PSR / DSR / MinTRL tests reproducing the published worked examples.

DSR source (confirmed against the authors' PDF, SSRN version pp. 9-10,
http://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf): Bailey, D. H. & López de Prado, M.
(2014), "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and
Non-Normality", Journal of Portfolio Management 40(5), numerical example: N = 100 independent
trials, best annualised SR = 2.5, variance of the trials' annualised SRs V = 1/2, de-annualised
"with 250 observations per year", T = 1250, skewness -3, raw kurtosis 10. Printed results:
SR0 ≈ 0.1132 per period and DSR = 0.9004; with N = 46 (other inputs unchanged) DSR = 0.9505.

PSR / MinTRL source (authors' PDF, https://www.davidhbailey.com/dhbpapers/sharpe-frontier.pdf):
Bailey, D. H. & López de Prado, M. (2012), "The Sharpe Ratio Efficient Frontier", Journal of
Risk 15(2), eq. 11 / eq. 13, per-period units, raw kurtosis. Worked example p. 10 / Figure 6
(monthly, SR = 0.458 as printed, skew -2.448, kurtosis 10.164): PSR(0) = 0.982 for n = 24 under
Normal moments, 0.913 for n = 24 and 0.953 for n = 36 with the non-Normal moments. Appendix
A.3 (p. 20): SR = 2/sqrt(12), SR* = 1/sqrt(12), skew -0.72, kurtosis 5.78, 95% one-sided ->
MinTRL = 59.895 months, and PSR(SR*) at n = MinTRL is 0.95.

The paper annualises with 250 days; per-period values are SR = 2.5 / sqrt(250) and
V = 0.5 / 250. Nothing in lab.stats.sharpe annualises, so the 250 is only the paper's input
convention, not a calendar assumption in the code.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from hedgefund.backtest import significance as hf_sig
from scipy import stats as sp_stats

from lab.stats.sharpe import (
    DSRResult,
    deflated_sharpe,
    expected_max_sharpe,
    min_track_record_length,
    min_track_record_length_from_moments,
    probabilistic_sharpe,
    psr_from_moments,
    trial_sharpe_variance,
)

PAPER_T = 1250
PAPER_SR = 2.5 / math.sqrt(250)  # per-period Sharpe of the selected strategy
PAPER_VAR = 0.5 / 250  # per-period variance of trial Sharpes
PAPER_N = 100


def _two_point_returns(n: int, n_low: int, sharpe: float, scale: float = 0.01) -> np.ndarray:
    """``n`` returns taking two values, ``n_low`` of them at the low value, with Sharpe exactly
    ``sharpe`` (mean / std ddof=1) after an affine map, which leaves skew and kurtosis alone.

    Skew -3 with kurtosis 10 sits on the bound kurtosis >= skew^2 + 1, which only two-point
    distributions reach. A share p = (13 - sqrt(117)) / 26 = 0.08397 at the low value gives
    exactly skew -3; with T = 1250 the nearest count is 105 (p = 0.084), giving sample skew
    -2.99941 and kurtosis 9.99646.
    """
    x = np.concatenate([np.full(n_low, -1.0), np.zeros(n - n_low)])
    z = (x - x.mean()) / x.std(ddof=1)
    return scale * (z + sharpe)


PAPER_RETURNS = _two_point_returns(PAPER_T, 105, PAPER_SR)


def test_paper_sample_matches_the_paper_moments() -> None:
    r = PAPER_RETURNS
    assert r.mean() / r.std(ddof=1) == pytest.approx(PAPER_SR, rel=1e-12)
    assert sp_stats.skew(r) == pytest.approx(-3.0, abs=1e-3)
    assert sp_stats.kurtosis(r, fisher=False) == pytest.approx(10.0, abs=4e-3)


def test_expected_max_sharpe_reproduces_paper_sr0() -> None:
    # Paper: SR0 ≈ 0.1132 (4 decimals, so +/-5e-5 from rounding).
    assert expected_max_sharpe(PAPER_N, PAPER_VAR) == pytest.approx(0.1132, abs=5e-5)


def test_deflated_sharpe_reproduces_paper_example() -> None:
    out = deflated_sharpe(PAPER_RETURNS, n_trials=PAPER_N, sharpe_variance=PAPER_VAR)
    assert isinstance(out, DSRResult)
    assert out.n_trials == PAPER_N
    assert out.sharpe_per_bar == pytest.approx(PAPER_SR, rel=1e-12)
    assert out.sr0_per_bar == pytest.approx(0.1132, abs=5e-5)
    # Paper: DSR ≈ 0.9004. Tolerance 1e-4 = 4-decimal rounding (5e-5) plus the integer-count
    # moments above (skew off by 6e-4 moves DSR by ~8e-6).
    assert out.dsr == pytest.approx(0.9004, abs=1e-4)
    assert out.dsr < out.psr


def test_deflated_sharpe_equals_eq6_with_the_samples_moments() -> None:
    r = PAPER_RETURNS
    sr = r.mean() / r.std(ddof=1)
    g3 = sp_stats.skew(r)
    g4 = sp_stats.kurtosis(r, fisher=False)
    gamma = np.euler_gamma
    sr0 = math.sqrt(PAPER_VAR) * (
        (1 - gamma) * sp_stats.norm.ppf(1 - 1 / PAPER_N)
        + gamma * sp_stats.norm.ppf(1 - 1 / (PAPER_N * math.e))
    )
    z = (sr - sr0) * math.sqrt(PAPER_T - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2)
    out = deflated_sharpe(r, n_trials=PAPER_N, sharpe_variance=PAPER_VAR)
    assert out.dsr == pytest.approx(sp_stats.norm.cdf(z), rel=1e-12)


def _three_point_gaussian_moments(mean: float, spread: float, k: int = 50) -> np.ndarray:
    """k at mean - spread, 4k at mean, k at mean + spread: skew 0, kurtosis exactly 3."""
    return np.concatenate(
        [np.full(k, mean - spread), np.full(4 * k, mean), np.full(k, mean + spread)]
    )


def test_psr_hand_value_with_gaussian_moments() -> None:
    # Bailey & López de Prado (2012) eq. 11 with g3 = 0, g4 = 3:
    # PSR = Phi((SR - SR*) sqrt(n - 1) / sqrt(1 + SR^2 / 2)). Exact moments, so rel=1e-12.
    r = _three_point_gaussian_moments(0.002, 0.01)
    sr = r.mean() / r.std(ddof=1)
    n = r.size
    for bench in (0.0, 0.05):
        expected = sp_stats.norm.cdf((sr - bench) * math.sqrt(n - 1) / math.sqrt(1 + sr**2 / 2))
        assert probabilistic_sharpe(pd.Series(r), bench) == pytest.approx(expected, rel=1e-12)


def test_psr_is_half_at_its_own_sharpe() -> None:
    r = _three_point_gaussian_moments(0.002, 0.01)
    assert probabilistic_sharpe(r, r.mean() / r.std(ddof=1)) == 0.5


def test_single_trial_dsr_equals_psr() -> None:
    out = deflated_sharpe(PAPER_RETURNS, n_trials=1, sharpe_variance=PAPER_VAR)
    assert out.sr0_per_bar == 0.0
    assert out.dsr == out.psr == probabilistic_sharpe(PAPER_RETURNS)


@pytest.mark.parametrize("variance", [float("nan"), float("inf"), 0.0])
def test_degenerate_variance_gives_sr0_zero(variance: float) -> None:
    out = deflated_sharpe(PAPER_RETURNS, n_trials=50, sharpe_variance=variance)
    assert out.sr0_per_bar == 0.0
    assert out.dsr == out.psr


def test_bad_trial_count_or_variance_raises() -> None:
    with pytest.raises(ValueError, match="n_trials"):
        deflated_sharpe(PAPER_RETURNS, n_trials=0, sharpe_variance=0.01)
    with pytest.raises(ValueError, match="n_trials"):
        deflated_sharpe(PAPER_RETURNS, n_trials=2.5, sharpe_variance=0.01)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sharpe_variance"):
        deflated_sharpe(PAPER_RETURNS, n_trials=5, sharpe_variance=-0.1)


def test_dsr_falls_as_trials_grow() -> None:
    few = deflated_sharpe(PAPER_RETURNS, n_trials=10, sharpe_variance=PAPER_VAR).dsr
    many = deflated_sharpe(PAPER_RETURNS, n_trials=1000, sharpe_variance=PAPER_VAR).dsr
    assert many < few


def test_trial_sharpe_variance_ignores_non_finite() -> None:
    assert trial_sharpe_variance([0.1, 0.2, float("nan"), 0.3]) == pytest.approx(0.01)
    assert math.isnan(trial_sharpe_variance([0.1, float("nan")]))


def test_nan_returns_rejected() -> None:
    with pytest.raises(ValueError, match="returns"):
        probabilistic_sharpe([0.01, float("nan"), 0.02])


# ------------------------------------------------- published examples at moment level


@pytest.mark.parametrize(("n_trials", "printed_dsr"), [(100, 0.9004), (46, 0.9505)])
def test_dsr_paper_examples_from_moments(n_trials: int, printed_dsr: float) -> None:
    # Exact paper moments (skew -3, kurtosis 10); abs 5e-5 = the paper's 4-decimal rounding.
    sr0 = expected_max_sharpe(n_trials, PAPER_VAR)
    dsr = psr_from_moments(PAPER_SR, PAPER_T, -3.0, 10.0, sr0)
    assert dsr == pytest.approx(printed_dsr, abs=5e-5)


def test_dsr_paper_n46_example_through_returns() -> None:
    # Same sample as the N = 100 test; abs 1e-4 for rounding plus the sample's moments.
    out = deflated_sharpe(PAPER_RETURNS, n_trials=46, sharpe_variance=PAPER_VAR)
    assert out.dsr == pytest.approx(0.9505, abs=1e-4)


@pytest.mark.parametrize(
    ("n", "skew", "kurtosis", "printed"),
    [
        (24, 0.0, 3.0, 0.982),  # Normal moments
        (24, -2.448, 10.164, 0.913),
        (36, -2.448, 10.164, 0.953),
    ],
)
def test_psr_sharpe_frontier_example(n: int, skew: float, kurtosis: float, printed: float) -> None:
    # The output is printed to 3 decimals (±5e-4) and so is the input SR = 0.458 (true value in
    # [0.4575, 0.4585]). For n = 36 the printed 0.953 sits 5e-6 outside the output-rounding band
    # at SR = 0.458 exactly (we get 0.95351), so the band also carries the propagated SR
    # rounding (~±1.1e-4 in PSR): the printed value must lie within the PSR range over the SR
    # interval, widened by the output rounding. n = 24 cases also hold at SR = 0.458 alone.
    lo = psr_from_moments(0.4575, n, skew, kurtosis, 0.0)
    hi = psr_from_moments(0.4585, n, skew, kurtosis, 0.0)
    assert lo - 5e-4 <= printed <= hi + 5e-4
    if n == 24:
        assert psr_from_moments(0.458, n, skew, kurtosis, 0.0) == pytest.approx(printed, abs=5e-4)


def test_min_track_record_length_appendix_example() -> None:
    sr, target = 2 / math.sqrt(12), 1 / math.sqrt(12)
    trl = min_track_record_length_from_moments(sr, -0.72, 5.78, target, 0.95)
    assert trl == pytest.approx(59.895, abs=5e-4)  # printed to 3 decimals
    # By construction PSR(SR*) at n = MinTRL is the confidence level.
    assert psr_from_moments(sr, trl, -0.72, 5.78, target) == pytest.approx(0.95, abs=1e-12)
    assert psr_from_moments(sr, 59.895, -0.72, 5.78, target) == pytest.approx(0.95, abs=5e-4)


def test_min_track_record_length_edge_cases() -> None:
    assert min_track_record_length_from_moments(0.1, 0.0, 3.0, 0.2) == math.inf
    assert math.isnan(min_track_record_length_from_moments(float("nan"), 0.0, 3.0))
    with pytest.raises(ValueError, match="confidence"):
        min_track_record_length_from_moments(0.1, 0.0, 3.0, 0.0, 1.0)


def test_psr_from_moments_undefined_cases_are_nan() -> None:
    assert math.isnan(psr_from_moments(0.1, 1, 0.0, 3.0))
    assert math.isnan(psr_from_moments(float("nan"), 100, 0.0, 3.0))
    assert math.isnan(psr_from_moments(1.0, 100, 5.0, 1.0))  # variance term 1 - 5 + 0 < 0


def test_return_level_functions_match_hedgefund() -> None:
    r = np.random.default_rng(21).standard_t(4, 800) * 0.01 + 0.001
    for bench in (0.0, 0.03):
        assert probabilistic_sharpe(r, bench) == pytest.approx(
            hf_sig.probabilistic_sharpe_ratio(r, bench), rel=1e-12
        )
    assert min_track_record_length(r, 0.02, 0.9) == pytest.approx(
        hf_sig.min_track_record_length(r, 0.02, 0.9), rel=1e-12
    )
    out = deflated_sharpe(r, n_trials=30, sharpe_variance=0.002)
    assert out.dsr == pytest.approx(hf_sig.deflated_sharpe_ratio(r, 30, 0.002)["dsr"], rel=1e-12)
