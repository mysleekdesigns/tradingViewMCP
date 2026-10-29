"""Drawdown statistics (PLAN.md Phase 15): CDaR, episodes, exposure-matched benchmark.

Known values come from a hand-built 9-bar path with dyadic returns (exact in binary):

    bar      0     1     2     3     4      5      6     7      8
    r      +.25  -.20  +.25  +.20  -.50   +.50  +1.00  -.50   -.20
    E      1.25  1.00  1.25  1.50  0.75  1.125  2.25  1.125  0.90
    DD     0     .20   0     0     .50    .25    0     .50    .60

Episodes: peak 0 -> trough 1 -> back at the equal peak on bar 2 (depth .2); peak 3 -> trough
4 -> new high on bar 6 (depth .5); peak 6 -> trough 8, unrecovered (depth .6).

Second implementations: the CUZ linear programme solved by ``scipy.optimize.linprog``, a brute
force over ``z in {DD_k}``, and vectorbt's drawdown records.
"""

from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
import pytest
from scipy.optimize import linprog

from lab.stats import (
    DrawdownEpisode,
    cdar,
    cdar_from_drawdowns,
    count_drawdown_episodes,
    drawdown_episodes,
    drawdown_series,
    exposure_matched_returns,
    metrics_from_returns,
)

PATH = [0.25, -0.2, 0.25, 0.2, -0.5, 0.5, 1.0, -0.5, -0.2]
PATH_DD = [0.0, 0.2, 0.0, 0.0, 0.5, 0.25, 0.0, 0.5, 0.6]


def _random_path(seed: int, n: int = 300) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0005, 0.03, n)


# --------------------------------------------------------------------------- drawdown_series


def test_drawdown_series_known_values() -> None:
    dd = drawdown_series(PATH)
    assert dd.shape == (9,)
    np.testing.assert_allclose(dd, PATH_DD, rtol=0, atol=1e-15)
    assert dd[2] == 0.0  # recovery exactly at the equal peak is exactly zero


def test_drawdown_series_first_bar_loss_and_wipeout() -> None:
    np.testing.assert_allclose(drawdown_series([-0.5, 1.0]), [0.5, 0.0])
    np.testing.assert_allclose(drawdown_series([-1.0, 0.5]), [1.0, 1.0])
    assert drawdown_series([]).shape == (0,)
    assert drawdown_series(pd.Series([0.1, 0.2])).tolist() == [0.0, 0.0]


def test_drawdown_series_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="NaN or inf"):
        drawdown_series([0.1, float("nan")])
    with pytest.raises(ValueError, match="below -1"):
        drawdown_series([0.1, -1.5])
    with pytest.raises(ValueError, match="1-D"):
        drawdown_series(np.zeros((2, 2)))


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_max_drawdown_series_equals_metrics_max_drawdown(seed: int) -> None:
    r = _random_path(seed)
    assert drawdown_series(r).max() == pytest.approx(
        -metrics_from_returns(r, "1d").max_drawdown, abs=1e-12
    )
    assert drawdown_series(PATH).max() == pytest.approx(
        -metrics_from_returns(np.array(PATH), "1d").max_drawdown, abs=1e-15
    )


# --------------------------------------------------------------------------- CDaR


def test_cdar_integer_tail() -> None:
    # a = 2/3 on 9 bars: (1 - a) n = 3 whole bars, worst three = .6, .5, .5.
    assert cdar(PATH, 2.0 / 3.0) == pytest.approx(1.6 / 3.0, abs=1e-12)


def test_cdar_fractional_tail() -> None:
    # a = .75 on 9 bars: m = 2.25 -> .6 + .5 + .25 x .5 over 2.25.
    assert cdar(PATH, 0.75) == pytest.approx((0.6 + 0.5 + 0.25 * 0.5) / 2.25, abs=1e-12)
    # a = .95 on 9 bars: m = .45 < 1 bar -> the maximum drawdown.
    assert cdar(PATH, 0.95) == pytest.approx(0.6, abs=1e-12)
    # a = .9 on 9 bars: m = .9 -> still within the worst bar.
    assert cdar(PATH, 0.9) == pytest.approx(0.6, abs=1e-12)
    # a = .8 on 9 bars: m = 1.8 -> (.6 + .8 x .5) / 1.8.
    assert cdar(PATH, 0.8) == pytest.approx((0.6 + 0.8 * 0.5) / 1.8, abs=1e-12)


def test_cdar_whole_bar_tail_despite_float_noise() -> None:
    # 20 bars at a = .95: (1 - .95) x 20 = 1.0000000000000009 is one whole bar, the max.
    dd = np.array([0.0] * 17 + [0.1, 0.3, 0.2])
    assert cdar_from_drawdowns(dd, 0.95) == 0.3
    # a = .9: two whole bars.
    assert cdar_from_drawdowns(dd, 0.9) == pytest.approx(0.25, abs=1e-15)


def test_cdar_confidence_zero_is_mean_and_near_one_is_max() -> None:
    assert cdar(PATH, 0.0) == pytest.approx(sum(PATH_DD) / 9, abs=1e-12)
    assert cdar(PATH, 0.999999) == pytest.approx(0.6, abs=1e-12)
    r = _random_path(7)
    dd = drawdown_series(r)
    assert cdar(r, 0.0) == pytest.approx(dd.mean(), abs=1e-12)
    assert cdar(r, 1.0 - 1e-9) == pytest.approx(dd.max(), abs=1e-12)


def test_cdar_default_confidence_and_monotone() -> None:
    r = _random_path(11)
    assert cdar(r) == cdar(r, 0.95)
    values = [cdar(r, a) for a in (0.0, 0.5, 0.8, 0.9, 0.95, 0.99)]
    assert all(x <= y + 1e-15 for x, y in itertools.pairwise(values))


def test_cdar_edge_cases_and_validation() -> None:
    assert np.isnan(cdar([], 0.95))
    assert cdar([0.01, 0.02, 0.03], 0.95) == 0.0
    for bad in (-0.1, 1.0, 1.5, float("nan")):
        with pytest.raises(ValueError, match="confidence"):
            cdar(PATH, bad)
    with pytest.raises(ValueError, match="NaN or inf"):
        cdar_from_drawdowns([0.1, float("inf")], 0.5)


def _cdar_lp(dd: np.ndarray, a: float) -> float:
    """CUZ (2005) LP: min z + sum u_k / ((1 - a) n), u_k >= DD_k - z, u_k >= 0, z free."""
    n = dd.size
    m = (1.0 - a) * n
    c = np.concatenate([[1.0], np.full(n, 1.0 / m)])
    a_ub = np.hstack([-np.ones((n, 1)), -np.eye(n)])
    bounds = [(None, None)] + [(0.0, None)] * n
    res = linprog(c, A_ub=a_ub, b_ub=-dd, bounds=bounds, method="highs")
    assert res.status == 0
    return float(res.fun)


def _cdar_brute(dd: np.ndarray, a: float) -> float:
    """The CUZ objective is convex piecewise linear with kinks at the DD_k: try each one."""
    m = (1.0 - a) * dd.size
    return min(z + np.maximum(dd - z, 0.0).sum() / m for z in dd)


@pytest.mark.parametrize("a", [0.0, 0.5, 0.75, 0.9, 0.95, 0.97, 0.99])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_cdar_matches_linprog_and_brute_force(a: float, seed: int) -> None:
    r = _random_path(seed, n=150)
    dd = drawdown_series(r)
    ours = cdar(r, a)
    assert ours == pytest.approx(_cdar_lp(dd, a), abs=1e-9)
    assert ours == pytest.approx(_cdar_brute(dd, a), abs=1e-12)


@pytest.mark.parametrize("a", [0.0, 2.0 / 3.0, 0.75, 0.8, 0.95])
def test_cdar_hand_path_matches_linprog(a: float) -> None:
    dd = np.array(PATH_DD)
    assert cdar(PATH, a) == pytest.approx(_cdar_lp(dd, a), abs=1e-9)
    assert cdar(PATH, a) == pytest.approx(_cdar_brute(dd, a), abs=1e-12)


# --------------------------------------------------------------------------- episodes


def test_episodes_known_path() -> None:
    eps = drawdown_episodes(PATH)
    assert len(eps) == 3
    assert eps[0] == DrawdownEpisode(peak=0, trough=1, recovery=2, depth=pytest.approx(0.2))
    assert (eps[1].peak, eps[1].trough, eps[1].recovery) == (3, 4, 6)
    assert eps[1].depth == pytest.approx(0.5)
    assert (eps[2].peak, eps[2].trough, eps[2].recovery) == (6, 8, None)
    assert eps[2].depth == pytest.approx(0.6)


def test_episode_recovery_exactly_at_equal_peak() -> None:
    # 1.25 -> 1.0 -> 1.25: equal to the peak counts as recovered, on that bar.
    eps = drawdown_episodes([0.25, -0.2, 0.25])
    assert eps == (DrawdownEpisode(peak=0, trough=1, recovery=2, depth=pytest.approx(0.2)),)
    # One bar short of the peak: unrecovered.
    eps = drawdown_episodes([0.25, -0.2, 0.2])
    assert eps[0].recovery is None


def test_episode_from_initial_equity_and_unrecovered() -> None:
    eps = drawdown_episodes([-0.5, 0.5, -0.5, 0.25])
    # E: .5, .75, .375, .46875: never back at 1.0.
    assert eps == (DrawdownEpisode(peak=-1, trough=2, recovery=None, depth=pytest.approx(0.625)),)
    assert drawdown_episodes([-0.5, 1.0]) == (
        DrawdownEpisode(peak=-1, trough=0, recovery=1, depth=0.5),
    )


def test_episodes_empty_and_monotone() -> None:
    assert drawdown_episodes([]) == ()
    assert drawdown_episodes([0.0, 0.1, 0.0, 0.2]) == ()


def test_two_episodes_and_trough_is_first_deepest_bar() -> None:
    # E: .5, 1.0 (rec), .5, .5 (tie, first is trough), 1.0 (rec)
    eps = drawdown_episodes([-0.5, 1.0, -0.5, 0.0, 1.0])
    assert [(e.peak, e.trough, e.recovery) for e in eps] == [(-1, 0, 1), (1, 2, 4)]
    assert [e.depth for e in eps] == [0.5, 0.5]


def test_count_episodes_strict_depth_threshold() -> None:
    assert count_drawdown_episodes(PATH, 0.0) == 3
    assert count_drawdown_episodes(PATH, 0.2) == 2  # the .2 episode is not deeper than .2
    assert count_drawdown_episodes(PATH, 0.19) == 3
    assert count_drawdown_episodes(PATH, 0.5) == 1  # .5 is not deeper than .5
    assert count_drawdown_episodes(PATH, 0.6) == 0
    assert count_drawdown_episodes([0.25, -0.2, 0.25], 0.3) == 0
    with pytest.raises(ValueError, match="min_depth"):
        count_drawdown_episodes(PATH, float("nan"))


def _vbt_episodes(r: np.ndarray) -> list[tuple[int, int, int | None, float]]:
    """vectorbt's drawdowns on the equity curve with the initial 1.0 prepended.

    vectorbt indexes the curve itself (position 0 = initial equity), so every index shifts by
    one; its ``end_idx`` of an *active* drawdown is the last bar, which maps to ``None``.
    Its depth is ``(peak_val - valley_val) / peak_val``.
    """
    import vectorbt as vbt

    equity = pd.Series(np.concatenate([[1.0], np.cumprod(1.0 + r)]))
    rec = vbt.Drawdowns.from_ts(equity).records
    out: list[tuple[int, int, int | None, float]] = []
    for row in rec.itertuples():
        recovery = int(row.end_idx) - 1 if int(row.status) == 1 else None
        depth = (row.peak_val - row.valley_val) / row.peak_val
        out.append((int(row.peak_idx) - 1, int(row.valley_idx) - 1, recovery, float(depth)))
    return out


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_episodes_match_vectorbt(seed: int) -> None:
    r = _random_path(seed, n=400)
    ours = drawdown_episodes(r)
    theirs = _vbt_episodes(r)
    assert len(ours) == len(theirs) > 3
    for e, (peak, trough, recovery, depth) in zip(ours, theirs, strict=True):
        assert (e.peak, e.trough, e.recovery) == (peak, trough, recovery)
        assert e.depth == pytest.approx(depth, abs=1e-12)


def test_episodes_hand_path_matches_vectorbt() -> None:
    ours = [(e.peak, e.trough, e.recovery, e.depth) for e in drawdown_episodes(PATH)]
    theirs = _vbt_episodes(np.array(PATH))
    assert len(ours) == len(theirs) == 3
    for o, t in zip(ours, theirs, strict=True):
        assert o[:3] == t[:3]
        assert o[3] == pytest.approx(t[3], abs=1e-12)


# --------------------------------------------------------------------------- exposure-matched


def test_exposure_matched_scaling() -> None:
    r = [0.1, -0.2, 0.05, -0.5]
    np.testing.assert_allclose(exposure_matched_returns(r, 0.5), [0.05, -0.1, 0.025, -0.25])
    np.testing.assert_array_equal(exposure_matched_returns(r, 1.0), r)
    np.testing.assert_array_equal(exposure_matched_returns(r, 0.0), np.zeros(4))
    # Constant mix rebalanced every bar: a -50% bar at half weight is a 25% drawdown.
    assert drawdown_series(exposure_matched_returns([-0.5], 0.5)).tolist() == [0.25]
    # Rebalanced every bar, half exposure is NOT half the drawdown over several bars: two
    # -50% bars are a 75% fall in full, but 1 - .75^2 = 43.75% (> 37.5%) at half weight.
    assert drawdown_series(exposure_matched_returns([-0.5, -0.5], 0.5))[-1] == 0.4375
    assert drawdown_series([-0.5, -0.5])[-1] == 0.75


def test_exposure_matched_validation() -> None:
    for bad in (-0.01, 1.01, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="exposure"):
            exposure_matched_returns([0.1], bad)
    with pytest.raises(ValueError, match="NaN or inf"):
        exposure_matched_returns([float("nan")], 0.5)
