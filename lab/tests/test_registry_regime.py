"""Regime features against hand calculations and against hedgeFund's own functions.

``regime_signal`` is computed by an O(n) recursion; hedgeFund's ``ew_transition_matrix`` +
``signal_from_matrix`` re-run on ``labels[: t + 1]`` at every bar is the O(n^2) oracle.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest
from hedgefund.regime.labels import label_regimes
from hedgefund.regime.markov import ew_transition_matrix
from hedgefund.regime.signal import signal_from_matrix

from lab.registry import REGISTRY
from lab.registry.regime import ew_markov_signal, regime_labels
from tests.synthetic import candles_from_rows, make_candles


def closes(values: list[float]) -> pd.DataFrame:
    return candles_from_rows([(v, v, v, v) for v in values])


def test_regime_state_matches_hand_calculated_labels() -> None:
    # window 2, threshold 5%: 104/100-1 = +4% -> 0; 110/100-1 = +10% -> +1;
    # 100/104-1 = -3.8% -> 0; 95/110-1 = -13.6% -> -1. First 2 bars have no 2-bar return.
    out = REGISTRY["regime_state"].compute(
        closes([100, 100, 104, 110, 100, 95]), {"window_bars": 2, "threshold": 0.05}
    )["value"]
    assert out.iloc[:2].isna().all()
    assert out.iloc[2:].tolist() == [0.0, 1.0, 0.0, -1.0]


def test_regime_state_is_hedgefund_label_minus_one() -> None:
    candles = make_candles(500, seed=4, vol=0.02)
    for window, threshold in ((20, 0.05), (7, 0.02)):
        out = REGISTRY["regime_state"].compute(
            candles, {"window_bars": window, "threshold": threshold}
        )["value"]
        ref = label_regimes(candles["close"], window=window, threshold=threshold) - 1
        assert out.iloc[:window].isna().all()
        assert np.array_equal(out.iloc[window:].to_numpy(), ref.to_numpy(dtype=float))


def test_ew_markov_signal_matches_hand_calculation() -> None:
    # Labels (after one warm-up row): Bull, Bull, Bear, Bull; half-life 1 -> decay 0.5.
    # t1: no pairs -> 0. t2: {B->B: 1}, row Bull [0,0,1] -> +1.
    # t3: {B->B: .5, B->Bear: 1}, current Bear has no outgoing pair -> 0.
    # t4: {B->B: .25, B->Bear: .5, Bear->B: 1}, row Bull [.5, 0, .25] -> (.25-.5)/.75.
    out = ew_markov_signal(np.array([-1, 2, 2, 0, 2]), half_life_bars=1.0)
    assert np.isnan(out[0])
    np.testing.assert_allclose(out[1:], [0.0, 1.0, 0.0, -1 / 3], rtol=0, atol=1e-12)


@pytest.mark.parametrize("half_life", [1.0, 5.0, 250.0, 100_000.0])
def test_regime_signal_matches_hedgefund_refit_at_every_bar(half_life: float) -> None:
    # 1e-12 absolute: the recursion and hedgeFund's direct 0.5**(k/h) weights sum the same
    # terms in a different order; the result is a ratio in [-1, 1].
    candles = make_candles(400, seed=7, vol=0.02)
    window, threshold = 10, 0.03
    out = (
        REGISTRY["regime_signal"]
        .compute(
            candles, {"window_bars": window, "threshold": threshold, "half_life_bars": half_life}
        )["value"]
        .to_numpy()
    )
    labels = label_regimes(candles["close"], window=window, threshold=threshold).to_numpy()
    assert np.isnan(out[:window]).all()
    expected = np.array(
        [
            signal_from_matrix(ew_transition_matrix(labels[: k + 1], half_life), int(labels[k]))
            for k in range(len(labels))
        ]
    )
    np.testing.assert_allclose(out[window:], expected, rtol=0, atol=1e-12)


def test_regime_signal_stays_within_minus_one_and_one() -> None:
    out = REGISTRY["regime_signal"].compute(make_candles(2_000, seed=2, vol=0.03), {})["value"]
    assert out.dropna().between(-1.0, 1.0).all()


def test_regime_labels_are_minus_one_during_warmup_and_on_short_history() -> None:
    close = make_candles(5)["close"]
    assert regime_labels(close, 10, 0.05).tolist() == [-1] * 5
    assert np.isnan(ew_markov_signal(np.full(5, -1), 10.0)).all()


def test_regime_signal_is_fast_on_15k_bars() -> None:
    # PLAN budget: a 15k-bar 4h series must compute well under a second or two.
    candles = make_candles(15_000, seed=1)
    start = time.perf_counter()
    REGISTRY["regime_signal"].compute(candles, {})
    REGISTRY["regime_state"].compute(candles, {})
    assert time.perf_counter() - start < 2.0
