"""Second-implementation tests: our PSR, DSR, expected max Sharpe, MinTRL, MinBTL, CSCV PBO and
CPCV splits/paths against ``purgedcv`` 0.1.11 (MIT, a dev dependency) on fixed seeded inputs.

Convention differences, and how each comparison handles them:

* **Moments.** ``purgedcv`` computes PSR/DSR from SR with ``ddof=0`` and bias-corrected skew
  and kurtosis (``bias=False``); ours uses SR with ``ddof=1`` and biased (scipy default)
  moments. The formulas are therefore compared at moment level: purgedcv's own moments (from
  ``deflated_sharpe_ratio_full``, public) go into our ``psr_from_moments``, which must
  reproduce its PSR/DSR to ``rel=1e-12`` (same closed form, double precision). Return-level
  values are also compared with a stated tolerance: the two moment conventions differ by
  O(1/n) (``sqrt((n - 1)/n)`` on SR, ``1 + O(1/n)`` on skew and kurtosis), which moves PSR by
  at most ``phi(z) * |dz|``; at the n = 2000 used here the observed max over 50 seeds is
  1.3e-4 for PSR (benchmarks 0 and 0.02) and 3.7e-4 for DSR (every ``TRIALS`` count,
  ``sharpe_variance=0.0009``), so the band is ``abs=1e-3``. That band cannot tell raw from
  excess kurtosis, so the moments themselves are also compared exactly: our biased skew and
  kurtosis, put through the standard bias corrections (checked against scipy's
  ``bias=False`` in the test), must equal purgedcv's to ``rel=1e-12``.
* **Expected max Z.** Compared with purgedcv's public
  ``deflated_sharpe_ratio_full(...).expected_max_z`` (not the private ``_expected_max_z``).
* **MinTRL.** purgedcv returns ``1 + ceil(n - 1)``; ours returns the unrounded ``1 + (n - 1)``.
  The test asserts ``ours <= theirs < ours + 1``.
* **PBO.** purgedcv takes ``(n_configs, n_obs)``, ours ``(T, N)``: we pass the transpose. It
  counts ``logit < 0``, ours ``logit <= 0``; with an even number of configs and continuous
  returns no logit is exactly 0, so the two agree exactly. purgedcv spreads ``T mod S`` over
  the first blocks while ours drops the oldest ``T mod S`` rows, so ``T`` is a multiple of S.
* **CPCV.** purgedcv purges by label overlap: a ``purge_bars + 1``-bar label horizon removes
  ``purge_bars`` bars before *and after* each test block; ours removes ``purge_bars`` before
  and ``embargo_bars`` after. They coincide when ``embargo_bars >= purge_bars``, which every
  case here uses. Our embargo fraction rounds up, purgedcv's down, so bar counts are passed
  explicitly (``embargo_observations``).
* **Effective N** is not compared: purgedcv's ``effective_n_trials`` is a different estimator
  (integrated autocorrelation time of the trial-Sharpe sequence), not the correlation-matrix
  participation ratio used here.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import purgedcv
import pytest
from scipy import stats as sp_stats

from lab.engine.splits import cpcv_groups, cpcv_path_assignment, cpcv_splits
from lab.stats.cpcv import cpcv_paths
from lab.stats.pbo import pbo_cscv
from lab.stats.sharpe import (
    _moments,
    deflated_sharpe,
    expected_max_z,
    min_backtest_length,
    min_track_record_length_from_moments,
    probabilistic_sharpe,
    psr_from_moments,
)
from lab.types import bars_per_year

SEEDS = (0, 1, 2)
TRIALS = (2, 10, 46, 100, 2479)


def _returns(seed: int, n: int = 2000) -> np.ndarray:
    """Fat-tailed (Student t, 4 dof), slightly positive per-bar returns."""
    return np.random.default_rng(seed).standard_t(4, n) * 0.01 + 0.0006


def test_purgedcv_version_is_pinned() -> None:
    assert purgedcv.__version__ == "0.1.11"


# ------------------------------------------------------------------ PSR


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("bench", [0.0, 0.02])
def test_psr_formula_matches_purgedcv_on_its_moments(seed: int, bench: float) -> None:
    r = _returns(seed)
    m = purgedcv.deflated_sharpe_ratio_full(r, 1, 0.0)  # moments only; N = 1 -> no deflation
    theirs = purgedcv.probabilistic_sharpe_ratio(r, bench)
    ours = psr_from_moments(m.observed_sr, m.n_obs, m.skew, m.kurt, bench)
    assert ours == pytest.approx(theirs, rel=1e-12)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("bench", [0.0, 0.02])
def test_psr_on_returns_within_the_moment_convention_band(seed: int, bench: float) -> None:
    r = _returns(seed)
    theirs = purgedcv.probabilistic_sharpe_ratio(r, bench)
    assert probabilistic_sharpe(r, bench) == pytest.approx(theirs, abs=1e-3)


# ------------------------------------------------------------------ moments (raw vs excess)


def _unbiased_skew(g1: float, n: int) -> float:
    """Adjusted Fisher-Pearson skew: ``G1 = g1 * sqrt(n (n - 1)) / (n - 2)``."""
    return g1 * math.sqrt(n * (n - 1)) / (n - 2)


def _unbiased_raw_kurtosis(g2_raw: float, n: int) -> float:
    """``G2 = ((n + 1) g2 + 6)(n - 1) / ((n - 2)(n - 3))`` on excess kurtosis, returned raw."""
    g2_excess = g2_raw - 3.0
    return ((n + 1) * g2_excess + 6.0) * (n - 1) / ((n - 2) * (n - 3)) + 3.0


@pytest.mark.parametrize("seed", SEEDS)
def test_bias_correction_formulas_match_scipy(seed: int) -> None:
    r = _returns(seed)
    n = r.size
    g1 = sp_stats.skew(r)
    g2_raw = sp_stats.kurtosis(r, fisher=False)
    assert _unbiased_skew(g1, n) == pytest.approx(sp_stats.skew(r, bias=False), rel=1e-12)
    want = sp_stats.kurtosis(r, bias=False, fisher=False)
    assert _unbiased_raw_kurtosis(g2_raw, n) == pytest.approx(want, rel=1e-12)


@pytest.mark.parametrize("seed", SEEDS)
def test_our_moments_are_purgedcvs_before_bias_correction(seed: int) -> None:
    # Exact, so a raw/excess kurtosis swap (or ddof slip) in our moments fails here even
    # though the return-level PSR band above is too wide to see it.
    r = _returns(seed)
    sr, g1, g2_raw, n = _moments(r)
    m = purgedcv.deflated_sharpe_ratio_full(r, 1, 0.0)
    assert n == m.n_obs == r.size
    assert sr * math.sqrt(n / (n - 1)) == pytest.approx(m.observed_sr, rel=1e-12)  # ddof 1 -> 0
    assert _unbiased_skew(g1, n) == pytest.approx(m.skew, rel=1e-12)
    assert _unbiased_raw_kurtosis(g2_raw, n) == pytest.approx(m.kurt, rel=1e-12)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("bench", [0.0, 0.02])
def test_psr_on_returns_is_exact_on_purgedcv_moments_converted_to_ours(
    seed: int, bench: float
) -> None:
    # Public-API version of the moment check: undo purgedcv's conventions (ddof=0 SR,
    # bias-corrected raw kurtosis) and our probabilistic_sharpe must match to rel=1e-12.
    r = _returns(seed)
    n = r.size
    m = purgedcv.deflated_sharpe_ratio_full(r, 1, 0.0)
    sr = m.observed_sr * math.sqrt((n - 1) / n)
    g1 = m.skew * (n - 2) / math.sqrt(n * (n - 1))
    g2_raw = (((m.kurt - 3.0) * (n - 2) * (n - 3) / (n - 1)) - 6.0) / (n + 1) + 3.0
    want = psr_from_moments(sr, n, g1, g2_raw, bench)
    assert probabilistic_sharpe(r, bench) == pytest.approx(want, rel=1e-12)


# ------------------------------------------------------------------ expected max Z / DSR


@pytest.mark.parametrize("n_trials", TRIALS)
def test_expected_max_z_matches_purgedcv(n_trials: int) -> None:
    diag = purgedcv.deflated_sharpe_ratio_full(_returns(0), n_trials, 0.001)
    assert expected_max_z(n_trials) == pytest.approx(diag.expected_max_z, rel=1e-12)


@pytest.mark.parametrize("n_trials", TRIALS)
def test_dsr_benchmark_and_formula_match_purgedcv(n_trials: int) -> None:
    r, var = _returns(1), 0.0009
    diag = purgedcv.deflated_sharpe_ratio_full(r, n_trials, var)
    ours = deflated_sharpe(r, n_trials=n_trials, sharpe_variance=var)
    assert ours.sr0_per_bar == pytest.approx(diag.sr_star, rel=1e-12)
    # Same DSR from the same moments:
    dsr = psr_from_moments(diag.observed_sr, diag.n_obs, diag.skew, diag.kurt, ours.sr0_per_bar)
    assert dsr == pytest.approx(diag.dsr, rel=1e-12)
    # And on the returns, within the moment-convention band (module docstring).
    assert ours.dsr == pytest.approx(diag.dsr, abs=1e-3)


def test_dsr_with_one_trial_is_psr_at_zero_in_both() -> None:
    r = _returns(2)
    theirs = purgedcv.deflated_sharpe_ratio(r, 1, 0.004)
    assert theirs == purgedcv.probabilistic_sharpe_ratio(r, 0.0)
    ours = deflated_sharpe(r, n_trials=1, sharpe_variance=0.004)
    assert ours.sr0_per_bar == 0.0
    assert ours.dsr == ours.psr == probabilistic_sharpe(r, 0.0)
    # Ours against theirs: same closed form, moment conventions differ by O(1/n), so the
    # return-level band from the module docstring applies.
    assert ours.dsr == pytest.approx(theirs, abs=1e-3)
    # And exactly, on purgedcv's own moments:
    m = purgedcv.deflated_sharpe_ratio_full(r, 1, 0.004)
    assert psr_from_moments(m.observed_sr, m.n_obs, m.skew, m.kurt, 0.0) == pytest.approx(
        theirs, rel=1e-12
    )


def test_dsr_annualised_variance_equivalence_with_bars_per_year() -> None:
    # purgedcv divides an annualised trial-Sharpe variance by bars_per_year; ours takes the
    # per-bar variance. 4h bars on the 365-day year: 2190 bars.
    r, n_trials, var_annual = _returns(0), 300, 1.7
    bpy = bars_per_year("4h")
    assert bpy == 2190.0
    diag = purgedcv.deflated_sharpe_ratio_full(r, n_trials, var_annual, bars_per_year=int(bpy))
    ours = deflated_sharpe(r, n_trials=n_trials, sharpe_variance=var_annual / bpy)
    assert ours.sr0_per_bar == pytest.approx(diag.sr_star, rel=1e-12)
    dsr = psr_from_moments(diag.observed_sr, diag.n_obs, diag.skew, diag.kurt, ours.sr0_per_bar)
    assert dsr == pytest.approx(diag.dsr, rel=1e-12)


# ------------------------------------------------------------------ MinTRL / MinBTL


@pytest.mark.parametrize(
    ("sr", "target", "skew", "kurt", "conf"),
    [
        (2 / math.sqrt(12), 1 / math.sqrt(12), -0.72, 5.78, 0.95),  # 2012 paper appendix A.3
        (0.05, 0.0, -0.4, 6.0, 0.95),
        (0.08, 0.02, 0.3, 4.0, 0.90),
        (0.03, 0.0, 0.0, 3.0, 0.99),
    ],
)
def test_min_track_record_length_matches_purgedcv_up_to_its_ceiling(
    sr: float, target: float, skew: float, kurt: float, conf: float
) -> None:
    ours = min_track_record_length_from_moments(sr, skew, kurt, target, conf)
    theirs = purgedcv.min_track_record_length(sr, target, 1 - conf, skew, kurt)
    assert ours <= theirs < ours + 1


def test_min_track_record_length_unreachable_target_is_inf_in_both() -> None:
    assert min_track_record_length_from_moments(0.01, 0.0, 3.0, 0.02, 0.95) == math.inf
    assert purgedcv.min_track_record_length(0.01, 0.02, 0.05, 0.0, 3.0) == math.inf


@pytest.mark.parametrize("n_trials", [1, *TRIALS])
@pytest.mark.parametrize("target", [0.5, 1.0, 2.0])
def test_min_backtest_length_matches_purgedcv(n_trials: int, target: float) -> None:
    theirs = purgedcv.minimum_backtest_length(n_trials, target_sharpe=target)
    assert min_backtest_length(n_trials, target) == pytest.approx(theirs, rel=1e-12, abs=0.0)


# ------------------------------------------------------------------ CSCV PBO


def _sweep(seed: int, t: int, n_configs: int, edge: float) -> np.ndarray:
    """T x N per-bar returns: noise plus a small config-specific drift (some real ranking)."""
    rng = np.random.default_rng(seed)
    drift = edge * np.linspace(-1.0, 1.0, n_configs)
    return rng.standard_normal((t, n_configs)) * 0.01 + drift


@pytest.mark.parametrize(
    ("seed", "n_splits", "n_configs", "edge"),
    [(0, 16, 10, 0.0), (1, 16, 10, 0.0004), (2, 8, 6, 0.0002), (3, 8, 20, 0.0)],
)
def test_pbo_cscv_matches_purgedcv(seed: int, n_splits: int, n_configs: int, edge: float) -> None:
    mat = _sweep(seed, t=100 * n_splits, n_configs=n_configs, edge=edge)
    ours = pbo_cscv(mat, n_splits=n_splits)
    theirs = purgedcv.probability_of_backtest_overfitting(mat.T, n_splits=n_splits)
    assert ours.n_combinations == theirs.n_combos
    assert not np.any(theirs.logits == 0.0)  # so "<= 0" and "< 0" count the same cases
    assert ours.pbo == theirs.pbo
    # Combination order differs (ours enumerates in-sample sets, theirs out-of-sample sets),
    # so compare the logits as multisets.
    assert np.allclose(np.sort(ours.logits), np.sort(theirs.logits), rtol=1e-12, atol=0)


# ------------------------------------------------------------------ CPCV


def _purgedcv_cv(
    n_bars: int, n_groups: int, n_test_groups: int, purge_bars: int, embargo_bars: int
) -> purgedcv.CombinatorialPurgedCV:
    pred = pd.Series(pd.date_range("2024-01-01", periods=n_bars, freq="4h", tz="UTC"))
    evalu = pred + pd.Timedelta(hours=4 * (purge_bars + 1))  # (purge_bars + 1)-bar label
    return purgedcv.CombinatorialPurgedCV(
        n_splits=n_groups,
        n_test_groups=n_test_groups,
        prediction_times=pred,
        evaluation_times=evalu,
        embargo_observations=embargo_bars,
    )


@pytest.mark.parametrize(
    ("n_bars", "n_groups", "n_test_groups", "purge_bars", "embargo_bars"),
    [(120, 6, 2, 0, 0), (103, 6, 2, 1, 1), (103, 6, 2, 1, 3), (257, 8, 3, 2, 5), (61, 5, 1, 0, 2)],
)
def test_cpcv_splits_match_purgedcv(
    n_bars: int, n_groups: int, n_test_groups: int, purge_bars: int, embargo_bars: int
) -> None:
    ours = cpcv_splits(
        n_bars, n_groups, n_test_groups, purge_bars=purge_bars, embargo_bars=embargo_bars
    )
    cv = _purgedcv_cv(n_bars, n_groups, n_test_groups, purge_bars, embargo_bars)
    theirs = list(cv.split(np.zeros((n_bars, 1))))
    assert len(ours) == cv.get_n_splits() == math.comb(n_groups, n_test_groups)
    for split, (train_idx, test_idx) in zip(ours, theirs, strict=True):
        assert np.flatnonzero(split.test).tolist() == sorted(test_idx.tolist())
        assert np.flatnonzero(split.train).tolist() == sorted(train_idx.tolist())


@pytest.mark.parametrize(("n_groups", "n_test_groups"), [(6, 2), (4, 2), (8, 3), (5, 1)])
def test_cpcv_path_assignment_matches_purgedcv_reconstruct_paths(
    n_groups: int, n_test_groups: int
) -> None:
    n_bars = 10 * n_groups + 3  # uneven groups, to exercise the remainder layout too
    cv = _purgedcv_cv(n_bars, n_groups, n_test_groups, 0, 0)
    tests = [test for _, test in cv.split(np.zeros((n_bars, 1)))]
    # Each fold "predicts" its own index; reconstructed paths then show which fold fed what.
    paths = cv.reconstruct_paths([np.full(len(t), float(i)) for i, t in enumerate(tests)])
    ours = cpcv_path_assignment(n_groups, n_test_groups)
    n_paths = math.comb(n_groups - 1, n_test_groups - 1)
    assert paths.shape == (len(ours), n_bars) == (n_paths, n_bars)
    groups = cpcv_groups(n_bars, n_groups)
    for p, path in enumerate(ours):
        for (start, end), split_index in zip(groups, path, strict=True):
            assert np.all(paths[p, start:end] == split_index)


def _select_on_train(mat: np.ndarray, train_idx: np.ndarray) -> int:
    """Best per-bar Sharpe (mean / std, ddof=1) over ``mat[train_idx]``; lowest index on ties."""
    block = mat[np.sort(train_idx)]
    return int(np.argmax(block.mean(axis=0) / block.std(axis=0, ddof=1)))


def test_cpcv_path_sharpes_match_purgedcv_reconstructed_paths() -> None:
    # The selection is recomputed here, independently, from purgedcv's own train indices (so
    # a selection that peeks at test, purged or embargoed bars disagrees with it); purgedcv's
    # reconstructed paths of that selection must then give the same annualised Sharpes as ours.
    n_bars, bpy = 1203, bars_per_year("4h")
    mat = _sweep(7, t=n_bars, n_configs=12, edge=0.0003)
    out = cpcv_paths(mat, bars_per_year=bpy, purge_bars=1, embargo_bars=13)
    cv = _purgedcv_cv(n_bars, 6, 2, 1, 13)
    folds = list(cv.split(np.zeros((n_bars, 1))))
    chosen = [_select_on_train(mat, train) for train, _ in folds]
    # The fixture must make selection matter: train-only and all-bars choices differ somewhere.
    assert any(c != _select_on_train(mat, np.arange(n_bars)) for c in chosen)
    assert out.chosen == tuple(chosen)
    preds = [mat[test, c] for (_, test), c in zip(folds, chosen, strict=True)]
    paths = cv.reconstruct_paths(preds)
    want = [row.mean() / row.std(ddof=1) * math.sqrt(bpy) for row in paths]
    assert out.path_sharpes == pytest.approx(want, rel=1e-12)
