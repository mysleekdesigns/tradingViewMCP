"""Effective trial count (eigenvalue participation ratio) on constructed cases with known answers.

Exact cases (identical plans -> 1, block structure -> k, identity -> M) hold to 1e-9: the only
error is eigvalsh round-off on matrices of size <= 60. Random-noise cases state their own
tolerance from the sampling distribution of the correlations.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from lab.stats.effective_n import (
    EffectiveN,
    effective_n_trials,
    null_participation_ratio,
    pairwise_correlation,
    participation_ratio,
)


def _noise(t: int, m: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal((t, m)) * 0.01


# ------------------------------------------------------------------ pairwise_correlation


def test_full_overlap_matches_numpy_corrcoef() -> None:
    x = _noise(400, 7, seed=1)
    got = pairwise_correlation(x, min_overlap=2)
    assert np.allclose(got, np.corrcoef(x, rowvar=False), atol=1e-12, rtol=0)


def test_pairwise_complete_uses_only_common_rows() -> None:
    x = _noise(300, 3, seed=2)
    x[:100, 0] = np.nan  # column 0 starts later
    x[250:, 2] = np.nan  # column 2 stops early
    got = pairwise_correlation(x, min_overlap=2)
    for i in range(3):
        for j in range(3):
            if i == j:
                continue
            both = np.isfinite(x[:, i]) & np.isfinite(x[:, j])
            want = np.corrcoef(x[both, i], x[both, j])[0, 1]
            assert got[i, j] == pytest.approx(want, abs=1e-12)


def test_result_is_exactly_symmetric_with_unit_diagonal() -> None:
    x = _noise(200, 600, seed=3)  # > one 512-column chunk
    x[np.random.default_rng(4).random(x.shape) < 0.2] = np.nan
    c = pairwise_correlation(x, min_overlap=10)
    assert np.array_equal(c, c.T, equal_nan=True)
    assert np.all(np.diag(c) == 1.0)
    assert np.nanmax(np.abs(c)) <= 1.0


def test_short_overlap_is_nan() -> None:
    x = _noise(100, 2, seed=5)
    x[:95, 0] = np.nan  # 5 common rows
    assert math.isnan(pairwise_correlation(x, min_overlap=6)[0, 1])
    assert math.isfinite(pairwise_correlation(x, min_overlap=5)[0, 1])


def test_zero_variance_on_the_overlap_is_nan_but_diagonal_is_one() -> None:
    x = _noise(100, 3, seed=6)
    x[:, 1] = 0.0  # a plan that never traded
    x[:60, 2] = 0.0012  # constant non-zero on rows where column 0 also exists...
    x[60:, 2] = np.nan  # ...and missing elsewhere
    c = pairwise_correlation(x, min_overlap=2)
    assert math.isnan(c[0, 1]) and math.isnan(c[1, 0])
    assert math.isnan(c[0, 2]) and math.isnan(c[1, 2])
    assert np.all(np.diag(c) == 1.0)


def test_identical_columns_correlate_one() -> None:
    col = _noise(500, 1, seed=7)
    c = pairwise_correlation(np.hstack([col, col, 3.0 * col + 0.002]), min_overlap=2)
    assert np.allclose(c, 1.0, atol=1e-12, rtol=0)


def test_min_overlap_below_two_raises() -> None:
    with pytest.raises(ValueError, match="min_overlap"):
        pairwise_correlation(_noise(10, 2, seed=8), min_overlap=1)


def test_non_2d_matrix_raises() -> None:
    with pytest.raises(ValueError, match="matrix"):
        pairwise_correlation(np.zeros(5), min_overlap=2)


# ------------------------------------------------------------------ participation_ratio


def test_identity_gives_m() -> None:
    assert participation_ratio(np.eye(12)) == pytest.approx(12.0, abs=1e-9)


def test_all_ones_gives_one() -> None:
    assert participation_ratio(np.ones((9, 9))) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize(("k", "size"), [(2, 5), (3, 4), (6, 10)])
def test_k_equal_blocks_of_identical_plans_give_k(k: int, size: int) -> None:
    corr = np.kron(np.eye(k), np.ones((size, size)))
    assert participation_ratio(corr) == pytest.approx(float(k), abs=1e-9)


def test_unequal_blocks_give_the_closed_form() -> None:
    # Blocks of sizes 1, 2, 5: eigenvalues 1, 2, 5 -> (8)^2 / (1 + 4 + 25) = 64 / 30.
    sizes = (1, 2, 5)
    corr = np.zeros((8, 8))
    start = 0
    for s in sizes:
        corr[start : start + s, start : start + s] = 1.0
        start += s
    assert participation_ratio(corr) == pytest.approx(64 / 30, abs=1e-9)


def test_nan_off_diagonal_is_treated_as_independent() -> None:
    corr = np.full((4, 4), np.nan)
    np.fill_diagonal(corr, 1.0)
    assert participation_ratio(corr) == pytest.approx(4.0, abs=1e-9)


def test_non_psd_input_is_clipped_and_stays_in_range() -> None:
    # Pairwise-complete matrices can be indefinite: rho(a,b) = rho(a,c) = 0.9, rho(b,c) = -0.9
    # has eigenvalues 1.9, 1.9 and -0.8. Clipping gives (3.8)^2 / (2 * 1.9^2) = 2.
    corr = np.array([[1.0, 0.9, 0.9], [0.9, 1.0, -0.9], [0.9, -0.9, 1.0]])
    assert np.linalg.eigvalsh(corr).min() < 0
    pr = participation_ratio(corr)
    assert pr == pytest.approx(2.0, abs=1e-9)
    assert 1.0 <= pr <= 3.0


def test_degenerate_sizes() -> None:
    assert participation_ratio(np.zeros((0, 0))) == 0.0
    assert participation_ratio(np.array([[1.0]])) == 1.0


def test_non_square_raises() -> None:
    with pytest.raises(ValueError, match="square"):
        participation_ratio(np.ones((2, 3)))


# ------------------------------------------------------------------ effective_n_trials


def test_identical_plans_give_one() -> None:
    col = _noise(800, 1, seed=10)
    out = effective_n_trials(np.repeat(col, 25, axis=1), min_overlap=30)
    assert isinstance(out, EffectiveN)
    assert out.n_effective == pytest.approx(1.0, abs=1e-9)
    assert (out.n_plans, out.n_with_returns, out.n_without_returns) == (25, 25, 0)
    assert out.n_unknown_pairs == 0


def test_independent_plans_give_about_n() -> None:
    # M = 50 independent columns over T = 5000 rows. Sample correlations are ~N(0, 1/T), so
    # E[PR] ~ M / (1 + (M - 1) / T) = 49.51 (sampling noise pulls PR slightly below M). Over 200
    # seeds PR had mean 49.515 and sd 0.020, so abs 0.1 is a 5-sd band around the expectation.
    m, t = 50, 5000
    out = effective_n_trials(_noise(t, m, seed=11), min_overlap=30)
    assert out.n_effective <= m
    assert out.n_effective == pytest.approx(m / (1 + (m - 1) / t), abs=0.1)


def test_k_groups_of_identical_plans_give_k() -> None:
    # Three independent base series, each copied into a group of 7 plans (scaled copies are
    # still identical in correlation). The 294 off-block entries have E[r^2] = 1/T, so
    # PR ~ 21^2 / (3 * 7^2 + 294 / T) = 2.9997 for T = 20000; abs 0.02 is ~60x that deficit.
    base = _noise(20000, 3, seed=12)
    plans = np.hstack([base[:, [g]] * (1 + 0.1 * j) for g in range(3) for j in range(7)])
    out = effective_n_trials(plans, min_overlap=30)
    assert out.n_effective == pytest.approx(3.0, abs=0.02)


def test_unknown_pairs_counted_and_treated_independent() -> None:
    # Two identical plans on rows 0..99 and two identical plans on rows 200..299: within each
    # pair rho = 1, across pairs no overlap -> unknown -> 0. PR of two blocks of 2 = 2.
    col = _noise(300, 1, seed=13)[:, 0]
    x = np.full((300, 4), np.nan)
    x[:100, 0] = x[:100, 1] = col[:100]
    x[200:, 2] = x[200:, 3] = col[200:]
    out = effective_n_trials(x, min_overlap=30)
    assert out.n_unknown_pairs == 4  # (0,2) (0,3) (1,2) (1,3)
    assert out.n_effective == pytest.approx(2.0, abs=1e-9)


def test_plans_without_returns_each_add_one() -> None:
    col = _noise(500, 1, seed=14)
    out = effective_n_trials(np.repeat(col, 4, axis=1), min_overlap=30, n_without_returns=3)
    assert out.n_effective == pytest.approx(4.0, abs=1e-9)  # 1 + 3
    assert (out.n_plans, out.n_with_returns, out.n_without_returns) == (7, 4, 3)


def test_no_returns_at_all() -> None:
    out = effective_n_trials(np.zeros((10, 0)), min_overlap=2, n_without_returns=5)
    assert out.n_effective == 5.0 and out.n_plans == 5
    empty = effective_n_trials(np.zeros((10, 0)), min_overlap=2)
    assert empty.n_effective == 0.0 and empty.n_plans == 0


def test_bounds_hold_on_messy_input() -> None:
    rng = np.random.default_rng(15)
    base = rng.standard_normal((400, 4))
    x = np.hstack([base + 0.3 * rng.standard_normal((400, 4)) for _ in range(10)])
    x[rng.random(x.shape) < 0.4] = np.nan
    x[:, 5] = 0.0  # never traded
    x[:, 6] = np.nan  # no observation at all
    out = effective_n_trials(x, min_overlap=40, n_without_returns=2)
    assert min(1, out.n_plans) <= out.n_effective <= out.n_plans
    assert out.n_plans == 42


def test_bad_n_without_returns_raises() -> None:
    with pytest.raises(ValueError, match="n_without_returns"):
        effective_n_trials(_noise(10, 2, seed=16), min_overlap=2, n_without_returns=-1)


# ------------------------------------------------------------------ null_participation_ratio


def _mp_expected(m: int, t: int) -> float:
    """Expected ratio of ``m`` independent full columns over ``t`` rows (module docstring)."""
    return m / (1 + (m - 1) / t)


def test_null_of_independent_noise_is_the_marchenko_pastur_level() -> None:
    # 40 plans x 400 rows: about 40 / (1 + 39/400) = 36.4. Tolerance 1.5: the ratio's spread
    # over seeds at this size is ~0.3 (5 sigma).
    t, m = 400, 40
    got = null_participation_ratio(_noise(t, m, seed=31), min_overlap=30, seed=0)
    assert got == pytest.approx(_mp_expected(m, t), abs=1.5)


def test_null_removes_a_shared_factor_that_the_ratio_itself_sees() -> None:
    # 50 plans on one factor (correlation ~0.5): the ratio is ~4, the shuffled null ~ what 50
    # independent plans score on 500 rows (45.5).
    rng = np.random.default_rng(32)
    t, m = 500, 50
    x = rng.standard_normal((t, 1)) + rng.standard_normal((t, m))
    pr = participation_ratio(pairwise_correlation(x, min_overlap=30))
    null = null_participation_ratio(x, min_overlap=30, seed=0)
    assert pr < 5.0
    assert null == pytest.approx(_mp_expected(m, t), abs=2.0)


def test_null_of_identical_plans_is_about_m() -> None:
    col = _noise(600, 1, seed=33)
    x = np.repeat(col, 20, axis=1)
    assert participation_ratio(pairwise_correlation(x, min_overlap=30)) == pytest.approx(1.0)
    null = null_participation_ratio(x, min_overlap=30, seed=0)
    assert null == pytest.approx(_mp_expected(20, 600), abs=1.0)


def test_null_keeps_each_columns_missing_rows() -> None:
    # Two columns that never overlap stay unknown (independent) after the shuffle: 2.0; a
    # column observed on 50 rows only is shuffled within those rows.
    x = _noise(200, 3, seed=34)
    x[100:, 0] = np.nan
    x[:100, 1] = np.nan
    x[:150, 2] = np.nan
    assert null_participation_ratio(x[:, :2], min_overlap=30, seed=1) == pytest.approx(2.0)
    rng = np.random.default_rng(5)
    shuffled = x.copy()
    for j in range(3):
        rows = np.flatnonzero(np.isfinite(shuffled[:, j]))
        shuffled[rows, j] = rng.permutation(shuffled[rows, j])
    want = participation_ratio(pairwise_correlation(shuffled, min_overlap=30))
    assert null_participation_ratio(x, min_overlap=30, seed=5) == want  # same draws, same value


def test_null_is_reproducible_for_a_seed_and_leaves_the_input_untouched() -> None:
    x = np.random.default_rng(35).standard_normal((300, 1)) + _noise(300, 15, seed=36) * 100
    before = x.copy()
    a = null_participation_ratio(x, min_overlap=30, seed=7)
    assert null_participation_ratio(x, min_overlap=30, seed=7) == a
    np.testing.assert_array_equal(x, before)


def test_null_edge_cases_and_bad_inputs() -> None:
    assert null_participation_ratio(np.zeros((10, 0)), min_overlap=2, seed=0) == 0.0
    assert null_participation_ratio(_noise(10, 1, seed=37), min_overlap=2, seed=0) == 1.0
    for bad in (-1, 1.5, True, None):
        with pytest.raises(ValueError, match="seed"):
            null_participation_ratio(_noise(10, 2, seed=38), min_overlap=2, seed=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="min_overlap"):
        null_participation_ratio(_noise(10, 2, seed=39), min_overlap=1, seed=0)
    with pytest.raises(ValueError, match="2-D"):
        null_participation_ratio(np.zeros(5), min_overlap=2, seed=0)


# ------------------------------------------- why the ratio must not be the DSR's N (review C1)


def test_one_factor_null_puts_the_ratio_far_below_m() -> None:
    """M zero-skill plans on one shared factor (equal correlation 0.4) are M distinct trials
    for the DSR, but the ratio is capped near 1 / 0.4^2 = 6.25: exactly
    M^2 / ((1 + (M-1) rho)^2 + (M-1)(1-rho)^2) = 6.09 for M = 200. V[SR] already reflects the
    shared factor, so using this as N would count it twice."""
    m, rho = 200, 0.4
    corr = np.full((m, m), rho)
    np.fill_diagonal(corr, 1.0)
    exact = m**2 / ((1 + (m - 1) * rho) ** 2 + (m - 1) * (1 - rho) ** 2)
    assert participation_ratio(corr) == pytest.approx(exact, rel=1e-9)
    assert exact == pytest.approx(6.09, abs=0.01) and exact < 1 / rho**2
    # the same on sampled returns (seeded): far below M, near the population value
    rng = np.random.default_rng(40)
    t = 1000
    x = math.sqrt(rho) * rng.standard_normal((t, 1)) + math.sqrt(1 - rho) * rng.standard_normal(
        (t, m)
    )
    sampled = participation_ratio(pairwise_correlation(x, min_overlap=30))
    assert sampled < 0.05 * m
    assert sampled == pytest.approx(exact, abs=1.0)


def test_adding_near_copies_lowers_the_ratio() -> None:
    """Not monotone in trials tried: 100 independent plans, then 300 near-copies of one of
    them. Nominal N rises 100 -> 400; the ratio FALLS (exactly 100 -> 400^2 / (301^2 + 99) =
    1.76 for exact copies), so as N it would reward trying more variants."""
    block = np.eye(400)
    block[:301, :301] = 1.0
    assert participation_ratio(np.eye(100)) == pytest.approx(100.0)
    assert participation_ratio(block) == pytest.approx(400**2 / (301**2 + 99), rel=1e-9)
    rng = np.random.default_rng(41)
    t = 2000
    base = rng.standard_normal((t, 100))
    before = participation_ratio(pairwise_correlation(base, min_overlap=30))
    copies = base[:, :1] + 0.05 * rng.standard_normal((t, 300))
    after = participation_ratio(pairwise_correlation(np.hstack([base, copies]), min_overlap=30))
    assert before == pytest.approx(_mp_expected(100, t), abs=2.0)  # ~95
    assert after < 2.5 < before  # ~1.8
