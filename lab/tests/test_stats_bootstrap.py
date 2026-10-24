"""Stationary bootstrap (Politis & Romano 1994): determinism, block-length law, coverage."""

from __future__ import annotations

import numpy as np
import pytest

from lab.stats.bootstrap import BootstrapCI, stationary_bootstrap_ci, stationary_bootstrap_indices


def _mean(x: np.ndarray) -> float:
    return float(np.mean(x))


def _block_starts(idx: np.ndarray, n: int) -> np.ndarray:
    """True where a position does not continue the previous one (idx + 1 mod n)."""
    starts = np.ones_like(idx, dtype=bool)
    starts[:, 1:] = idx[:, 1:] != (idx[:, :-1] + 1) % n
    return starts


def test_indices_shape_range_and_seeded_determinism() -> None:
    a = stationary_bootstrap_indices(50, mean_block=5, n_samples=7, rng=np.random.default_rng(1))
    b = stationary_bootstrap_indices(50, mean_block=5, n_samples=7, rng=np.random.default_rng(1))
    c = stationary_bootstrap_indices(50, mean_block=5, n_samples=7, rng=np.random.default_rng(2))
    assert a.shape == (7, 50)
    assert a.min() >= 0 and a.max() < 50
    np.testing.assert_array_equal(a, b)
    assert not np.array_equal(a, c)


def test_mean_block_length_matches_request() -> None:
    # Block lengths are geometric with mean 1 / p = mean_block. A new block that happens to
    # start at the next index is invisible (prob 1/n = 0.0005), so expected observed ≈ 10.0.
    # 200 x 2000 positions give ~40,000 blocks: SE of the mean length ≈ 9.5 / 200 ≈ 0.05.
    n = 2000
    idx = stationary_bootstrap_indices(
        n, mean_block=10, n_samples=200, rng=np.random.default_rng(0)
    )
    observed = idx.size / _block_starts(idx, n).sum()
    assert observed == pytest.approx(10.0, abs=0.3)


def test_blocks_wrap_around_the_end() -> None:
    n = 20
    idx = stationary_bootstrap_indices(
        n, mean_block=50, n_samples=200, rng=np.random.default_rng(3)
    )
    wraps = (idx[:, :-1] == n - 1) & (idx[:, 1:] == 0)
    assert wraps.any()


def test_mean_block_one_is_iid_resampling() -> None:
    # p = 1: every position starts a new block, so positions are iid uniform.
    idx = stationary_bootstrap_indices(
        10, mean_block=1, n_samples=5000, rng=np.random.default_rng(4)
    )
    counts = np.bincount(idx.ravel(), minlength=10) / idx.size
    np.testing.assert_allclose(counts, 0.1, atol=0.005)  # SE ≈ 0.0013


def test_ci_is_seeded_and_brackets_the_point() -> None:
    r = np.random.default_rng(5).normal(0.001, 0.01, 300)
    kw = {"n_samples": 300, "mean_block": 5.0, "confidence": 0.9}
    a = stationary_bootstrap_ci(r, _mean, seed=11, **kw)
    b = stationary_bootstrap_ci(r, _mean, seed=11, **kw)
    c = stationary_bootstrap_ci(r, _mean, seed=12, **kw)
    assert isinstance(a, BootstrapCI)
    assert a == b
    assert a != c
    assert a.point == pytest.approx(r.mean())
    assert a.low < a.point < a.high
    assert a.level == 0.9
    assert a.n_samples == 300


def _coverage(n_seeds: int) -> float:
    hits = 0
    for s in range(n_seeds):
        r = np.random.default_rng(10_000 + s).normal(0.0005, 0.01, 300)
        ci = stationary_bootstrap_ci(r, _mean, n_samples=400, mean_block=3, confidence=0.9, seed=s)
        hits += ci.low <= 0.0005 <= ci.high
    return hits / n_seeds


def test_ci_covers_true_mean_near_nominal_rate() -> None:
    # 100 seeds at a nominal 90%: binomial SE = 0.03. Percentile intervals on n = 300 bars
    # undercover a little (block resampling biases the variance low), measured 0.86 here and
    # 0.882 over 1000 seeds; the band 0.80-0.97 is ~3 SE around 0.90.
    assert 0.80 <= _coverage(100) <= 0.97


@pytest.mark.slow
def test_ci_covers_true_mean_near_nominal_rate_many_seeds() -> None:
    # 1000 seeds: SE ≈ 0.0095; 0.86-0.94 is ~4 SE around 0.90 (measured 0.882).
    assert 0.86 <= _coverage(1000) <= 0.94


def test_ci_nan_statistic_everywhere_gives_nan_bounds() -> None:
    ci = stationary_bootstrap_ci(
        np.ones(20), lambda x: float("nan"), n_samples=10, mean_block=2, confidence=0.9, seed=0
    )
    assert np.isnan(ci.low) and np.isnan(ci.high)
    assert ci.n_samples == 0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"mean_block": 0.5}, "mean_block"),
        ({"n_samples": 0}, "n_samples"),
        ({"confidence": 1.0}, "confidence"),
    ],
)
def test_bad_ci_inputs_raise(kwargs: dict[str, float], match: str) -> None:
    base = {"n_samples": 10, "mean_block": 2.0, "confidence": 0.9, "seed": 0}
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        stationary_bootstrap_ci(np.ones(20), _mean, **base)  # type: ignore[arg-type]


def test_empty_or_nan_returns_raise() -> None:
    with pytest.raises(ValueError, match="returns"):
        stationary_bootstrap_ci(
            np.array([]), _mean, n_samples=5, mean_block=2, confidence=0.9, seed=0
        )
    with pytest.raises(ValueError, match="returns"):
        stationary_bootstrap_ci(
            np.array([0.1, np.nan]), _mean, n_samples=5, mean_block=2, confidence=0.9, seed=0
        )
