"""Candle bar permutation (Masters) for the full-pipeline permutation null."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab.stats.permutation import percentile_rank, permutation_p_value, permute_candles
from lab.types import validate_candles
from tests.synthetic import make_candles


def _shapes(c: pd.DataFrame) -> np.ndarray:
    """Per-bar (gap, high, low, close) log-ratios for bars 1.., rows sorted for multiset compare."""
    o, h, lo, cl = (c[k].to_numpy() for k in ("open", "high", "low", "close"))
    s = np.column_stack(
        [
            np.log(o[1:] / cl[:-1]),
            np.log(h[1:] / o[1:]),
            np.log(lo[1:] / o[1:]),
            np.log(cl[1:] / o[1:]),
        ]
    )
    return s[np.lexsort(s.T[::-1])]


CANDLES = make_candles(300, seed=5, vol=0.02)


def test_permuted_candles_follow_the_convention() -> None:
    p = permute_candles(CANDLES, seed=1)
    validate_candles(p)
    assert p.index.equals(CANDLES.index)
    assert (p["high"] >= p[["open", "close"]].max(axis=1)).all()
    assert (p["low"] <= p[["open", "close"]].min(axis=1)).all()
    assert (p[["open", "high", "low", "close"]] > 0).all().all()


def test_first_bar_kept_and_last_close_preserved() -> None:
    p = permute_candles(CANDLES, seed=2)
    pd.testing.assert_series_equal(p.iloc[0], CANDLES.iloc[0])
    # Sum of log returns is order-invariant; only cumsum rounding differs (rel 1e-12).
    assert p["close"].iloc[-1] == pytest.approx(CANDLES["close"].iloc[-1], rel=1e-12)


def test_volume_and_tradable_stay_in_place() -> None:
    c = CANDLES.copy()
    c.iloc[10, c.columns.get_loc("tradable")] = False
    p = permute_candles(c, seed=3)
    pd.testing.assert_series_equal(p["volume"], c["volume"])
    pd.testing.assert_series_equal(p["tradable"], c["tradable"])


def test_multiset_of_bar_shapes_preserved() -> None:
    p = permute_candles(CANDLES, seed=4)
    # Rebuilding through exp/log round-trips at ~1e-15 relative per price; atol 1e-9 on logs.
    np.testing.assert_allclose(_shapes(p), _shapes(CANDLES), atol=1e-9)


def test_order_actually_changes() -> None:
    p = permute_candles(CANDLES, seed=4)
    assert not np.allclose(p["close"].to_numpy(), CANDLES["close"].to_numpy())


def test_seeded_determinism_and_different_seeds_differ() -> None:
    a = permute_candles(CANDLES, seed=7)
    b = permute_candles(CANDLES, seed=7)
    c = permute_candles(CANDLES, seed=8)
    pd.testing.assert_frame_equal(a, b)
    assert not a["close"].equals(c["close"])


def test_input_not_modified() -> None:
    before = CANDLES.copy()
    permute_candles(CANDLES, seed=9)
    pd.testing.assert_frame_equal(CANDLES, before)


def test_destroys_serial_structure() -> None:
    # Strongly trending-then-reverting closes: lag-1 autocorrelation of bar returns is high
    # by construction (AR(1) phi = 0.8); a permuted series has ~0 (SE ≈ 1/sqrt(n) ≈ 0.03).
    rng = np.random.default_rng(0)
    n = 1000
    eps = rng.normal(0.0, 0.01, n)
    r = np.empty(n)
    r[0] = eps[0]
    for t in range(1, n):
        r[t] = 0.8 * r[t - 1] + eps[t]
    close = 100.0 * np.exp(np.cumsum(r))
    c = make_candles(n, seed=1)
    c["open"] = np.concatenate([[100.0], close[:-1]])
    c["close"] = close
    c["high"] = np.maximum(c["open"], c["close"]) * 1.001
    c["low"] = np.minimum(c["open"], c["close"]) * 0.999

    def ac1(df: pd.DataFrame) -> float:
        x = np.diff(np.log(df["close"].to_numpy()))
        return float(np.corrcoef(x[:-1], x[1:])[0, 1])

    assert ac1(c) > 0.7
    assert abs(ac1(permute_candles(c, seed=0))) < 0.15


def test_short_frames_return_a_copy() -> None:
    two = CANDLES.iloc[:2]
    pd.testing.assert_frame_equal(permute_candles(two, seed=0), two)


def test_bad_candles_raise() -> None:
    bad = CANDLES.copy()
    bad.iloc[5, bad.columns.get_loc("low")] = 0.0
    with pytest.raises(ValueError, match="positive"):
        permute_candles(bad, seed=0)
    with pytest.raises(ValueError, match="tradable"):
        permute_candles(CANDLES.drop(columns="tradable"), seed=0)


def test_rank_helpers_wrap_hedgefund() -> None:
    null = np.array([0.1, 0.2, 0.3, np.nan])
    assert permutation_p_value(0.25, null) == pytest.approx((1 + 1) / (3 + 1))
    assert percentile_rank(0.25, null) == pytest.approx(100 * 2 / 3)
