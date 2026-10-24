"""Markov regime features built on hedgeFund's regime engine (PLAN.md Phase 7 How 10).

* ``regime_state`` labels bar t Bear / Sideways / Bull from its trailing ``window_bars``
  return via ``hedgefund.regime.labels.label_regimes``, mapped to -1 / 0 / +1.
* ``regime_signal`` is hedgeFund's ``signal_from_matrix``, P(next = Bull) - P(next = Bear),
  read from the row of bar t's state in the exponentially weighted transition matrix of
  ``hedgefund.regime.markov.ew_transition_matrix`` fitted on the labels at or before t only.
  Calling hedgeFund once per bar would be O(n^2); the same counts follow the O(n) recursion
  ``C_t = d * C_{t-1} + [pair ending at t]`` with ``d = 0.5 ** (1 / half_life_bars)``, which
  ``tests/test_registry_regime.py`` checks against hedgeFund's own functions at every bar.

hedgeFund was written for daily equity bars. Its defaults (20 trading days, a 250-bar
half-life, 252-day years) are NOT used here: every window is a parameter counted in bars of
whatever interval the plan runs on. ``label_regimes`` itself only does ``pct_change(window)``
over rows, and ``ew_transition_counts`` only decays per label step, so neither assumes a
calendar. Gaps are not filled: a window of N bars spans N rows.

``hmm_bear_prob`` is deliberately absent: hedgeFund's causal HMM
(``hmm_walk_forward_from_close``: refits on past rows plus a forward filter) takes about 75 s
on a 15k-bar 4h series with a single restart, far too slow for a registry feature that
agents recompute hundreds of times. A smoothed or full-sample fit would be look-ahead.

Pure module: DataFrames in, DataFrames out, no I/O, no globals mutated.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd
from hedgefund.regime.labels import BEAR, BULL, STATES, label_regimes

from lab.registry.features import make_spec
from lab.types import FeatureSpec, ParamSpec

N_STATES = len(STATES)  # Bear, Sideways, Bull (hedgeFund's canonical order)
# hedgeFund state index -> lab output: Bear 0 -> -1, Sideways 1 -> 0, Bull 2 -> +1.
_STATE_VALUE = np.array([-1.0, 0.0, 1.0])

_VALUE = ("value",)

_WINDOW = ParamSpec(
    "window_bars",
    "int",
    20,  # the video's 20-bar trailing return; counted in bars of the plan's interval
    1,
    5_000,
    "Bars in the trailing return that labels each bar.",
)
_THRESHOLD = ParamSpec(
    "threshold",
    "float",
    0.05,  # the video's +/-5% trailing-return threshold
    0.001,
    1.0,
    "Trailing simple return (fraction) above which a bar is Bull and below minus which Bear.",
)
_HALF_LIFE = ParamSpec(
    "half_life_bars",
    "float",
    250.0,  # hedgeFund's DEFAULT_HALF_LIFE_BARS, here in bars of the plan's interval
    1.0,
    100_000.0,
    "Half-life in bars of the weight on past transitions in the matrix.",
)


def regime_labels(close: pd.Series, window_bars: int, threshold: float) -> np.ndarray:
    """hedgeFund state index (0 Bear, 1 Sideways, 2 Bull) per row; -1 during warm-up.

    Wraps ``label_regimes`` (simple trailing return over ``window_bars`` rows, strict
    thresholds). Row t uses ``close[t]`` and ``close[t - window_bars]`` only.
    """
    out = np.full(close.shape[0], -1, dtype=np.int64)
    if close.shape[0] <= window_bars:
        return out
    labels = label_regimes(close, window=window_bars, threshold=threshold, return_type="simple")
    out[close.index.get_indexer(labels.index)] = labels.to_numpy(dtype=np.int64)
    return out


def ew_markov_signal(labels: np.ndarray, half_life_bars: float) -> np.ndarray:
    """P(Bull) - P(Bear) next state from EW transition counts on labels ``<= t``.

    ``labels`` holds state indices with a leading block of ``-1`` (undefined). At each
    defined row t the counts are hedgeFund's ``ew_transition_counts(labels[first:t+1])``:
    the pair ending at t has weight 1, the pair k labels earlier ``0.5 ** (k / half_life)``.
    The signal is read from the current state's row; a row with no outgoing transitions
    yet is all zeros in hedgeFund's ``normalise_rows`` and gives 0. Output is NaN where the
    label is undefined.
    """
    n = labels.shape[0]
    out = np.full(n, np.nan)
    defined = np.flatnonzero(labels >= 0)
    if defined.size == 0:
        return out
    first = int(defined[0])
    seq = labels[first:]
    m = seq.shape[0]
    # One indicator column per (from, to) pair; row 0 has no pair yet.
    pairs = np.zeros((m, N_STATES * N_STATES))
    if m >= 2:
        pairs[np.arange(1, m), seq[:-1] * N_STATES + seq[1:]] = 1.0
    decay = 0.5 ** (1.0 / half_life_bars)
    # ewm(adjust=False) gives y_k = decay * y_{k-1} + (1 - decay) * x_k with y_0 = x_0 = 0,
    # i.e. (1 - decay) times the EW counts; the constant cancels in the row normalisation.
    counts = (
        pd.DataFrame(pairs).ewm(alpha=1.0 - decay, adjust=False).mean().to_numpy()
        if decay < 1.0
        else np.cumsum(pairs, axis=0)
    )
    rows = counts.reshape(m, N_STATES, N_STATES)[np.arange(m), seq]  # current state's row
    total = rows.sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        signal = (rows[:, BULL] - rows[:, BEAR]) / total
    out[first:] = np.where(total > 0.0, signal, 0.0)
    return out


def _close(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].astype(np.float64)


def _regime_state(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    labels = regime_labels(_close(candles), int(params["window_bars"]), float(params["threshold"]))
    value = np.where(labels >= 0, _STATE_VALUE[np.clip(labels, 0, None)], np.nan)
    return pd.DataFrame({"value": value}, index=candles.index, columns=list(_VALUE))


def _regime_signal(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    labels = regime_labels(_close(candles), int(params["window_bars"]), float(params["threshold"]))
    value = ew_markov_signal(labels, float(params["half_life_bars"]))
    return pd.DataFrame({"value": value}, index=candles.index, columns=list(_VALUE))


REGIME_STATE = make_spec(
    "regime_state",
    "Markov regime of bar t from its trailing `window_bars` return: +1 Bull (return > "
    "threshold), -1 Bear (return < -threshold), 0 Sideways; useful as a trend filter.",
    (_WINDOW, _THRESHOLD),
    _VALUE,
    _regime_state,
    lambda p: p["window_bars"],
)

REGIME_SIGNAL = make_spec(
    "regime_signal",
    "Markov-chain regime signal in [-1, 1]: P(next bar Bull) - P(next bar Bear) from an "
    "exponentially weighted transition matrix fitted only on regime labels up to bar t; "
    "noisy until a few half-lives of history, best used as a filter.",
    (_WINDOW, _THRESHOLD, _HALF_LIFE),
    _VALUE,
    _regime_signal,
    lambda p: p["window_bars"],
)

REGIME_FEATURES: tuple[FeatureSpec, ...] = (REGIME_STATE, REGIME_SIGNAL)
