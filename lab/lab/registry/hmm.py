"""Causal HMM regime features (PLAN.md Phase 20 Part B).

``causal_hmm`` walks a Gaussian HMM forward over a close series with the mechanics of
``hedgefund.backtest.hmm_walk_forward.run_hmm_walk_forward``. The inputs are the bar log return
and one trailing sample standard deviation of it over ``vol_window_bars``. The first decision
row is ``train_bars`` usable feature rows in. Every ``refit_every_bars`` decisions, starting with
the first, a scaler and the model are fitted on the ``train_bars`` usable rows strictly before
the decision row, and the carried posterior is the last row of ``forward_filter`` over that
standardised block. :func:`fit_block` returns that posterior with the parameters, so a cached fit
needs no replay of the block. Between refits ``filter_step`` advances one bar at a time, so bar t
reads rows at or before t only. Only filtered probabilities exist here: the smoothed and Viterbi
paths read the whole sample and are never imported (``tests/test_registry_hmm.py`` checks this
statically). hedgeFund's hysteresis is not used, because persistence is a separate plan construct.

hedgeFund was written for daily equity bars. ``fit_hmm_features``, ``build_features``,
``fit_scaler``, ``forward_filter`` and ``filter_step`` contain no 252-day or daily-bar logic: they
count rows. Only hedgeFund's DEFAULT windows assume a trading year or month, so every window here
is passed explicitly from ``[regime.hmm]`` and none of its defaults is read.

States are ordered by ascending mean return inside each fit's own training window (hedgeFund
permutes them that way), so state 0 is always that window's lowest-mean-return state and labels
do not switch at a refit. ``bear_prob`` is the filtered probability of state 0.

Refit positions are anchored on the first row of ``close``: they count usable rows from there and
nothing else. A longer series with the same prefix therefore gives bit-identical output on that
prefix, which is why a design view and a holdout view agree on every design bar.

Gaps are not filled. The row after a gap has a return spanning several bars, so it is not an
observation: its log return is NaN, which also makes the rolling volatility NaN for its window. A
row with a non-finite feature is NaN in every output, and the filter steps across it with the
transition matrix alone (``prior @ transmat``, no emission update). Training blocks use the
finite rows only. Absent rows are not invented: a hole of several bars is one predict-only row
per row present in ``close``, not one per missing bar. A row the caller marks unobserved
(``observed`` False, a zero-volume bar) is treated the same way: its log return and the next
row's are NaN. A series with neither matches hedgeFund exactly.

``bear_high_vol`` says what state 0 meant in the fit that scored each bar: 1.0 when state 0's
return variance exceeds that of the highest-mean state, else 0.0. States are ordered by mean
return, and when the means are close that order can flip between fits while the volatility
regimes stay put, so ``hmm_bear_prob`` would read the calm state in those fits. The diagnostic
reports this; it does not change the ordering.

``hmm_state`` and ``hmm_bear_prob`` read columns that ``lab.data.hmm_columns`` attaches to the
candles, because a fit costs a large fraction of a second and a sweep computes features
thousands of times. :func:`refit_blocks` lists a series' training blocks up front without fitting
anything, so the data layer can fit the missing ones in parallel before ``causal_hmm`` runs. Neither
feature has a param, so a plan cannot change a setting. Their warm-up is data-dependent and not
a param, so ``warmup`` returns 0 and ``lab.plan.compile`` uses the first finite row instead.

Pure module: arrays in, arrays out, no I/O, no globals mutated.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd
from hedgefund.regime.hmm import fit_hmm_features
from hedgefund.regime.hmm_features import FeatureScaler, build_features, fit_scaler
from hedgefund.regime.hmm_filter import HMMParams, filter_step, forward_filter

from lab.registry.features import make_spec
from lab.types import FeatureSpec, HmmSettings

HMM_STATE_COLUMN = "hmm_series_state"
HMM_BEAR_PROB_COLUMN = "hmm_series_bear_prob"
HMM_STATE_PROB_COLUMN = "hmm_series_state_prob"  # filtered probability of the argmax state
HMM_BEAR_HIGH_VOL_COLUMN = "hmm_series_bear_high_vol"  # label-meaning diagnostic
HMM_COLUMNS: tuple[str, ...] = (
    HMM_STATE_COLUMN,
    HMM_BEAR_PROB_COLUMN,
    HMM_STATE_PROB_COLUMN,
    HMM_BEAR_HIGH_VOL_COLUMN,
)
HMM_ALGO_VERSION = 2  # bump when the output of causal_hmm changes; part of every cache key

# hedgeFund's build_features needs vol_short >= 2 and vol_long > vol_short, so the one volatility
# kept is passed as vol_long beside a throwaway vol_short of 2 rows, which needs a window of 3.
_THROWAWAY_VOL_WINDOW = 2
_RETURN_COLUMN = "log_return"  # hedgeFund's column 0, the column states are ordered by
_NS_PER_SECOND = 1_000_000_000
_VALUE = ("value",)


@dataclass(frozen=True)
class HmmSeries:
    """One causal HMM output per row of the input close series."""

    state: np.ndarray  # float64, ordered state id 0..n_states-1, 0 = lowest mean return; NaN = none
    bear_prob: np.ndarray  # float64, filtered P(state 0)
    state_prob: np.ndarray  # float64, filtered probability of `state`
    warmup_bars: int  # rows before the first non-NaN output (len(close) if none)
    bear_high_vol: np.ndarray  # float64, 1.0 = state 0 is the higher-variance state in this fit


@dataclass(frozen=True)
class FittedBlock:
    """One refit's result: the ordered parameters and the filter's belief at the block's end."""

    params: HMMParams
    posterior: np.ndarray  # last row of forward_filter over the standardised block


@dataclass(frozen=True)
class RefitBlock:
    """One refit's input: a standardised training block and its column names."""

    block: np.ndarray
    columns: tuple[str, ...]


FitFn = Callable[[np.ndarray, tuple[str, ...], HmmSettings], FittedBlock]
"""Standardised training block, its column names, the settings -> the fitted block."""


def fit_block(block: np.ndarray, columns: tuple[str, ...], settings: HmmSettings) -> FittedBlock:
    """Multi-restart Baum-Welch on one standardised training block, states ordered by mean return,
    plus the carried posterior.

    Raises ``RuntimeError`` naming the cause when hmmlearn is missing or every restart failed."""
    fitted = fit_hmm_features(
        block,
        feature_names=columns,
        seeds=settings.seeds,
        n_components=settings.n_states,
        n_iter=settings.n_iter,
    )
    if fitted is None:
        raise RuntimeError("hmmlearn is not installed, so the causal HMM cannot be fitted")
    return FittedBlock(fitted.params, forward_filter(fitted.params, block)[0][-1])


def feature_columns(settings: HmmSettings) -> tuple[str, ...]:
    """The two model inputs, in hedgeFund's names: the return first, then the one volatility."""
    return (_RETURN_COLUMN, f"vol_{settings.vol_window_bars}")


def _contiguous_runs(
    close: pd.Series, bar_seconds: int, observed: np.ndarray | None
) -> list[tuple[int, int]]:
    """``[start, stop)`` row positions of the runs of ``close`` whose rows are one bar apart and
    observed. An unobserved row belongs to no run, so the row after it starts a new one."""
    stamps = pd.DatetimeIndex(close.index).as_unit("ns").asi8
    breaks = np.flatnonzero(np.diff(stamps) > bar_seconds * _NS_PER_SECOND) + 1
    edges = [0, *breaks.tolist(), len(close)]
    if observed is None or observed.all():
        return list(pairwise(edges))
    runs: list[tuple[int, int]] = []
    for start, stop in pairwise(edges):
        first = start
        for row in np.flatnonzero(~observed[start:stop]) + start:
            runs.append((first, int(row)))
            first = int(row) + 1
        runs.append((first, stop))
    return [(start, stop) for start, stop in runs if start < stop]


def _feature_matrix(
    close: pd.Series, settings: HmmSettings, bar_seconds: int, observed: np.ndarray | None
) -> np.ndarray:
    """``(len(close), 2)`` float64 inputs, NaN on every row that is not an observation.

    Each contiguous run goes through hedgeFund's ``build_features`` on its own, so the first row
    of a run has no return and the next ``vol_window_bars - 1`` rows have no full volatility
    window, the same rows a NaN return on the row after the gap would blank. Nothing crosses a
    gap or an unobserved row, and nothing is filled."""
    columns = feature_columns(settings)
    out = np.full((len(close), len(columns)), np.nan, dtype=np.float64)
    for start, stop in _contiguous_runs(close, bar_seconds, observed):
        run = close.iloc[start:stop]
        built = build_features(
            run, vol_short=_THROWAWAY_VOL_WINDOW, vol_long=settings.vol_window_bars
        )
        rows = run.index.get_indexer(built.index) + start
        out[rows] = built[list(columns)].to_numpy(dtype=np.float64)
    return out


def _observed_mask(close: pd.Series, observed: np.ndarray | None) -> np.ndarray | None:
    """``observed`` as a bool array; ``ValueError`` names a length that does not match."""
    if observed is None:
        return None
    mask = np.asarray(observed, dtype=bool)
    if mask.shape != (len(close),):
        raise ValueError(f"observed must have one row per close, got shape {mask.shape}")
    return mask


def _check_inputs(close: pd.Series, settings: HmmSettings, bar_seconds: int) -> None:
    if not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError(f"close must have a DatetimeIndex, got {type(close.index).__name__}")
    if bar_seconds <= 0:
        raise ValueError(f"bar_seconds must be positive, got {bar_seconds}")
    if settings.vol_window_bars <= _THROWAWAY_VOL_WINDOW:
        raise ValueError(
            f"[regime.hmm] vol_window_bars must be > {_THROWAWAY_VOL_WINDOW}, got "
            f"{settings.vol_window_bars}: hedgeFund's build_features needs a shorter window too"
        )
    if settings.train_bars < 2:
        raise ValueError(f"[regime.hmm] train_bars must be >= 2, got {settings.train_bars}")
    if settings.refit_every_bars < 1:
        raise ValueError(
            f"[regime.hmm] refit_every_bars must be >= 1, got {settings.refit_every_bars}"
        )


def _usable_rows(
    close: pd.Series, settings: HmmSettings, bar_seconds: int, observed: np.ndarray | None
) -> tuple[np.ndarray, np.ndarray]:
    """``(row positions, feature rows)`` of every row of ``close`` that is an observation."""
    if not len(close):
        return np.empty(0, dtype=np.int64), np.empty((0, 2), dtype=np.float64)
    features = _feature_matrix(close, settings, bar_seconds, _observed_mask(close, observed))
    usable = np.flatnonzero(np.isfinite(features).all(axis=1))
    # Same memory order as hedgeFund's frame.to_numpy(): numpy reductions sum in memory order, so
    # a C-ordered copy of the same numbers gives a scaler that differs in the last bits.
    return usable, np.asfortranarray(features[usable])


def _standardised_block(
    rows: np.ndarray, at: int, settings: HmmSettings
) -> tuple[FeatureScaler, np.ndarray]:
    """The scaler and standardised training block of the decision at usable row ``at``."""
    block = rows[at - settings.train_bars : at]
    scaler = fit_scaler(block, feature_columns(settings))
    return scaler, scaler.transform(block)


def refit_blocks(
    close: pd.Series,
    settings: HmmSettings,
    *,
    bar_seconds: int,
    observed: np.ndarray | None = None,
) -> tuple[RefitBlock, ...]:
    """Every refit's input, in the order :func:`causal_hmm` asks for them, with nothing fitted.

    Each block is the one ``causal_hmm`` hands to its ``fit``, so results computed from these
    can be served to it by content. ``ValueError`` names a bad input."""
    _check_inputs(close, settings, bar_seconds)
    _, rows = _usable_rows(close, settings, bar_seconds, observed)
    columns = feature_columns(settings)
    positions = range(settings.train_bars, rows.shape[0], settings.refit_every_bars)
    return tuple(RefitBlock(_standardised_block(rows, i, settings)[1], columns) for i in positions)


def causal_hmm(
    close: pd.Series,
    settings: HmmSettings,
    *,
    bar_seconds: int,
    fit: FitFn = fit_block,
    observed: np.ndarray | None = None,
) -> HmmSeries:
    """The filtered state, bear probability and state probability of every row (module docstring).

    Row t depends on rows at or before t only, and ``fit`` sees only rows strictly before its
    refit row. Rows with no decision are NaN: every row before the first decision, and any later
    row with a non-finite feature. ``observed`` (bool per row, None = all) marks rows that are not
    observations. ``ValueError`` names a bad input."""
    _check_inputs(close, settings, bar_seconds)
    n = len(close)
    state = np.full(n, np.nan, dtype=np.float64)
    bear_prob = np.full(n, np.nan, dtype=np.float64)
    state_prob = np.full(n, np.nan, dtype=np.float64)
    bear_high_vol = np.full(n, np.nan, dtype=np.float64)
    usable, rows = _usable_rows(close, settings, bar_seconds, observed)
    train = settings.train_bars
    if usable.size <= train:
        return HmmSeries(state, bear_prob, state_prob, n, bear_high_vol)

    columns = feature_columns(settings)
    for k, i in enumerate(range(train, usable.size)):
        if k % settings.refit_every_bars == 0:
            scaler, scaled = _standardised_block(rows, i, settings)
            fitted = fit(scaled, columns, settings)
            params, carried = fitted.params, fitted.posterior
            variance = params.covars[:, 0, 0]  # state n_states - 1 has the highest mean return
            bear_is_high_vol = float(variance[0] > variance[-1])
        at = int(usable[i])
        for _ in range(at - int(usable[i - 1]) - 1):  # rows with no observation: predict only
            carried = carried @ params.transmat
        carried, _ = filter_step(params, carried, scaler.transform(rows[i]))
        best = int(np.argmax(carried))
        state[at] = float(best)
        bear_prob[at] = float(carried[0])
        state_prob[at] = float(carried[best])
        bear_high_vol[at] = bear_is_high_vol
    return HmmSeries(state, bear_prob, state_prob, int(usable[train]), bear_high_vol)


def _series_column(candles: pd.DataFrame, column: str) -> pd.DataFrame:
    """``column`` of ``candles`` as the feature frame; ``ValueError`` naming it when absent."""
    if column not in candles.columns:
        raise ValueError(
            f"candles have no {column!r} column: the causal HMM is not loaded for this "
            "symbol/interval; [regime.hmm] symbols and intervals in lab.toml list the series the "
            "loader fits"
        )
    value = candles[column].to_numpy(dtype=np.float64, copy=True)
    return pd.DataFrame({"value": value}, index=candles.index, columns=list(_VALUE))


def _hmm_state(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    return _series_column(candles, HMM_STATE_COLUMN)


def _hmm_bear_prob(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    return _series_column(candles, HMM_BEAR_PROB_COLUMN)


HMM_STATE = make_spec(
    "hmm_state",
    "Causal HMM regime of bar t: the forward filter's most probable state, 0 = the lowest "
    "mean return of the latest fit's training window; fixed in lab.toml [regime.hmm]. A filter, "
    "not a direction signal: only overlay `flat_when` or an `and` gate beside a price signal; "
    "NaN until the first fit and across gaps.",
    (),
    _VALUE,
    _hmm_state,
    lambda _p: 0,
)

HMM_BEAR_PROB = make_spec(
    "hmm_bear_prob",
    "Causal HMM filtered probability in [0, 1] that bar t is in the lowest-mean-return state "
    "of the latest fit's training window; fixed in lab.toml [regime.hmm]. A filter, not a "
    "direction signal: only overlay `flat_when` or an `and` gate beside a price signal; NaN "
    "until the first fit and across gaps.",
    (),
    _VALUE,
    _hmm_bear_prob,
    lambda _p: 0,
)

HMM_FEATURES: tuple[FeatureSpec, ...] = (HMM_STATE, HMM_BEAR_PROB)
