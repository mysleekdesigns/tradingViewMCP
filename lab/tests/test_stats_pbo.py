"""PBO via CSCV: Bailey, Borwein, López de Prado & Zhu (2017), "The Probability of Backtest
Overfitting", Journal of Computational Finance 20(4).

Published properties reproduced here: C(S, S/2) combinations (12,870 for S = 16, the paper's
recommended split count); trials with no skill give PBO ≈ 0.5 in expectation (the in-sample
best is a random draw out of sample); a trial whose true edge dominates gives PBO ≈ 0. Plus a
hand-computed S = 4, N = 2 case where every logit is checked by hand.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from lab.stats.metrics import sharpe_per_bar
from lab.stats.pbo import PBOResult, pbo_cscv

LN2 = math.log(2.0)


def _noise(seed: int, t: int = 1600, n: int = 16) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 0.01, (t, n))


def _mean(x: np.ndarray) -> float:
    return float(np.mean(x))


def test_hand_case_s4_n2_logits() -> None:
    # One row per block. Metric = mean. A trends up; B is constant at 0.0251.
    # Combos (in-sample blocks) in itertools order and the hand result:
    #  {0,1}: IS A .015 < B -> pick B; OOS A .035 > B -> B rank 1/3 -> λ = ln(1/2) = -ln2
    #  {0,2}: IS A .020 -> B; OOS A .030 > B -> -ln2
    #  {0,3}: IS A .025 -> B; OOS A .025 < B -> B rank 2/3 -> +ln2
    #  {1,2}: IS A .025 -> B; OOS A .025 < B -> +ln2
    #  {1,3}: IS A .030 -> A; OOS A .020 < B -> A rank 1/3 -> -ln2
    #  {2,3}: IS A .035 -> A; OOS A .015 < B -> -ln2
    mat = np.column_stack([[0.01, 0.02, 0.03, 0.04], np.full(4, 0.0251)])
    out = pbo_cscv(mat, n_splits=4, metric=_mean)
    expected = np.array([-LN2, -LN2, LN2, LN2, -LN2, -LN2])
    np.testing.assert_allclose(out.logits, expected, rtol=1e-12)
    assert out.pbo == pytest.approx(4 / 6)
    assert (out.n_splits, out.n_combinations, out.n_trials) == (4, 6, 2)


def test_combination_count_for_16_splits_is_12870() -> None:
    out = pbo_cscv(_noise(0, t=320, n=3), n_splits=16)
    assert isinstance(out, PBOResult)
    assert out.n_combinations == math.comb(16, 8) == 12870
    assert out.logits.shape == (12870,)


def test_vectorised_sharpe_matches_generic_metric_path() -> None:
    mat = _noise(1, t=203, n=5)  # 203 rows: the 3 oldest are dropped for S = 8
    fast = pbo_cscv(mat, n_splits=8)
    slow = pbo_cscv(mat, n_splits=8, metric=sharpe_per_bar)
    np.testing.assert_allclose(fast.logits, slow.logits, rtol=1e-12)
    assert fast.pbo == slow.pbo


def test_remainder_rows_dropped_from_the_start() -> None:
    mat = _noise(2, t=10, n=3)
    np.testing.assert_array_equal(
        pbo_cscv(mat, n_splits=4).logits, pbo_cscv(mat[2:], n_splits=4).logits
    )


def test_noise_trials_give_pbo_near_half() -> None:
    # One noise matrix gives a very wide PBO (sd ≈ 0.2 across seeds, measured), so average 40
    # seeds: SE ≈ 0.2 / sqrt(40) ≈ 0.03; ±0.15 is ~4.5 SE.
    pbos = [pbo_cscv(_noise(100 + s), n_splits=16).pbo for s in range(40)]
    assert abs(np.mean(pbos) - 0.5) < 0.15


@pytest.mark.slow
def test_noise_trials_give_pbo_near_half_many_seeds() -> None:
    # 400 seeds: SE ≈ 0.01; ±0.05 is ~5 SE.
    pbos = [pbo_cscv(_noise(1000 + s), n_splits=16).pbo for s in range(400)]
    assert abs(np.mean(pbos) - 0.5) < 0.05


def test_dominant_true_edge_gives_pbo_near_zero() -> None:
    # Trial 3 has a true per-bar Sharpe of 0.3. Each half has 1000 rows, so the Sharpe estimate's
    # SE is ~0.03 and trial 3 beats 15 noise trials (Sharpe ~0 ± 0.03) in both halves.
    mat = _noise(7, t=2000)
    mat[:, 3] += 0.003
    out = pbo_cscv(mat, n_splits=16)
    assert out.pbo < 0.01
    assert np.all(out.logits > 0)


def test_odd_n_median_rank_counts_as_overfit() -> None:
    # N = 3, IS winner lands exactly at the OOS median -> ω = 2/4, λ = 0, counted in PBO.
    mat = np.array([[3.0, 2.0, 1.0], [1.0, 2.0, 3.0]])  # metric = mean, S = 2
    out = pbo_cscv(mat, n_splits=2, metric=_mean)
    # Combos {0} and {1}. {0}: IS best col 0 (3), OOS row 1 gives it 1 -> rank 1, ω = 1/4.
    # {1}: IS best col 2 (3), OOS row 0 gives it 1 -> rank 1. λ = ln(1/3) both times.
    np.testing.assert_allclose(out.logits, [math.log(1 / 3)] * 2, rtol=1e-12)
    assert out.pbo == 1.0
    mat2 = np.array([[3.0, 2.0, 1.0], [2.0, 3.0, 1.0]])
    out2 = pbo_cscv(mat2, n_splits=2, metric=_mean)
    # {0}: col 0 scores 2 OOS -> rank 2 of 3, ω = 0.5, λ = 0. {1}: col 1 scores 2 -> same.
    np.testing.assert_array_equal(out2.logits, [0.0, 0.0])
    assert out2.pbo == 1.0


def test_nan_metric_ranks_last() -> None:
    # Column 1 is constant -> zero-variance Sharpe is NaN, never chosen in-sample.
    mat = _noise(3, t=80, n=3)
    mat[:, 1] = 0.0
    out = pbo_cscv(mat, n_splits=4)
    assert np.all(np.isfinite(out.logits))


def test_speed_s16_n16_t15000() -> None:
    mat = _noise(4, t=15_000, n=16)
    start = time.perf_counter()
    pbo_cscv(mat, n_splits=16)
    assert time.perf_counter() - start < 2.0


@pytest.mark.parametrize(
    ("mat", "splits", "match"),
    [
        (np.zeros(20), 4, "2-D"),
        (np.zeros((20, 1)), 4, "N >= 2"),
        (np.zeros((20, 3)), 5, "even"),
        (np.zeros((20, 3)), 0, "even"),
        (np.zeros((6, 3)), 8, "T >= n_splits"),
        (np.full((20, 3), np.nan), 4, "NaN"),
    ],
)
def test_bad_inputs_raise(mat: np.ndarray, splits: int, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        pbo_cscv(mat, n_splits=splits)
