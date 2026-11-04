"""The causal HMM (PLAN.md Phase 20 Part B): no look-ahead, hedgeFund agreement, gaps, labels.

Synthetic prices only: nothing here may look at real data before the pre-registered Part D run.
Small settings keep each fit in the milliseconds; the real ones are timed outside the test suite.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hedgefund.backtest.hmm_walk_forward import run_hmm_walk_forward
from hedgefund.regime.hmm import fit_hmm_features
from hedgefund.regime.hmm_features import build_features, fit_scaler
from hedgefund.regime.hmm_filter import HMMParams, filter_step, forward_filter

import lab.data.hmm_columns as hmm_columns_module
import lab.registry.hmm as hmm_module
from lab.plan.schema import HMM_FEATURE_NAMES
from lab.plan.units import DIMENSIONLESS, output_scale
from lab.registry import REGISTRY
from lab.registry.hmm import (
    HMM_BEAR_HIGH_VOL_COLUMN,
    HMM_BEAR_PROB_COLUMN,
    HMM_COLUMNS,
    HMM_STATE_COLUMN,
    HMM_STATE_PROB_COLUMN,
    FittedBlock,
    HmmSeries,
    causal_hmm,
    feature_columns,
    fit_block,
    refit_blocks,
)
from lab.types import HmmSettings, interval_seconds
from tests.synthetic import equal_mean_close, make_candles, two_regime_close, with_hmm

BAR = interval_seconds("4h")
SETTINGS = HmmSettings(
    n_states=2,
    vol_window_bars=10,
    train_bars=200,
    refit_every_bars=42,
    seed=7,
    restarts=1,
    n_iter=50,
)
N_BARS = 420
FIRST_DECISION = SETTINGS.vol_window_bars + SETTINGS.train_bars  # no gaps: first usable row is w
REFIT_ROWS = tuple(range(FIRST_DECISION, N_BARS, SETTINGS.refit_every_bars))


def close_series(n: int = N_BARS, *, seed: int = 3) -> pd.Series:
    return make_candles(n, seed=seed, vol=0.02)["close"]


def without_rows(close: pd.Series, *rows: int) -> pd.Series:
    """``close`` with the rows at ``rows`` removed: missing bars are absent, never filled."""
    keep = np.ones(len(close), dtype=bool)
    keep[list(rows)] = False
    return close.loc[keep]


def assert_same_prefix(a: HmmSeries, b: HmmSeries, stop: int) -> None:
    for name in ("state", "bear_prob", "state_prob", "bear_high_vol"):
        left, right = getattr(a, name)[:stop], getattr(b, name)[:stop]
        assert np.array_equal(left, right, equal_nan=True), name


def shocked(close: pd.Series, t: int, *, seed: int = 99) -> pd.Series:
    """``close`` with every row after position ``t`` replaced by a different path."""
    rng = np.random.default_rng(seed)
    values = close.to_numpy(dtype=np.float64, copy=True)
    values[t + 1 :] = values[t + 1 :] * np.exp(rng.normal(0.0, 0.2, len(close) - t - 1))
    return pd.Series(values, index=close.index, name=close.name)


# --------------------------------------------------------------------------- no look-ahead


@pytest.mark.parametrize(
    "t",
    [
        0,
        FIRST_DECISION - 1,
        FIRST_DECISION,
        FIRST_DECISION + 1,
        REFIT_ROWS[1] - 1,
        REFIT_ROWS[1],
        REFIT_ROWS[1] + 1,
        N_BARS - 2,
    ],
)
def test_outputs_through_t_are_bit_identical_after_perturbing_the_future(t: int) -> None:
    close = close_series()
    base = causal_hmm(close, SETTINGS, bar_seconds=BAR)
    after = causal_hmm(shocked(close, t), SETTINGS, bar_seconds=BAR)
    assert_same_prefix(base, after, t + 1)


def test_the_perturbation_has_teeth() -> None:
    close = close_series()
    t = REFIT_ROWS[1]
    base = causal_hmm(close, SETTINGS, bar_seconds=BAR)
    after = causal_hmm(shocked(close, t), SETTINGS, bar_seconds=BAR)
    assert not np.array_equal(base.bear_prob[t + 1 :], after.bear_prob[t + 1 :], equal_nan=True)


def test_perturbing_the_future_of_a_gapped_series_changes_nothing_before_it() -> None:
    close = without_rows(close_series(N_BARS + 3), 260, 261, 300)
    t = 330
    base = causal_hmm(close, SETTINGS, bar_seconds=BAR)
    after = causal_hmm(shocked(close, t), SETTINGS, bar_seconds=BAR)
    assert_same_prefix(base, after, t + 1)


@pytest.mark.parametrize("gapped", [False, True])
def test_a_longer_series_gives_bit_identical_output_on_the_shared_prefix(gapped: bool) -> None:
    """Refit positions are anchored on row 0, so the design and holdout views agree."""
    close = close_series()
    if gapped:
        close = without_rows(close, 250, 251)
    for stop in (REFIT_ROWS[2], REFIT_ROWS[2] + 5, len(close) - 1):
        cut = causal_hmm(close.iloc[:stop], SETTINGS, bar_seconds=BAR)
        full = causal_hmm(close, SETTINGS, bar_seconds=BAR)
        assert_same_prefix(cut, full, stop)


def test_every_fit_sees_only_the_train_bars_rows_strictly_before_its_refit_row() -> None:
    close = close_series()
    features = build_features(close, vol_short=2, vol_long=SETTINGS.vol_window_bars)
    frame = features[list(feature_columns(SETTINGS))].to_numpy(dtype=np.float64)
    blocks: list[np.ndarray] = []

    def spy(block: np.ndarray, columns: tuple[str, ...], settings: HmmSettings) -> FittedBlock:
        blocks.append(block.copy())
        return fit_block(block, columns, settings)

    causal_hmm(close, SETTINGS, bar_seconds=BAR, fit=spy)
    assert len(blocks) == len(REFIT_ROWS)
    columns = feature_columns(SETTINGS)
    for block, row in zip(blocks, REFIT_ROWS, strict=True):
        position = row - SETTINGS.vol_window_bars  # feature rows start at close row w
        raw = frame[position - SETTINGS.train_bars : position]
        assert np.array_equal(block, fit_scaler(raw, columns).transform(raw))


@pytest.mark.parametrize("gapped", [False, True])
def test_refit_blocks_lists_what_causal_hmm_asks_for_in_order_without_fitting(gapped: bool) -> None:
    close = without_rows(close_series(), 250, 251) if gapped else close_series()
    asked: list[tuple[np.ndarray, tuple[str, ...]]] = []

    def spy(block: np.ndarray, columns: tuple[str, ...], settings: HmmSettings) -> FittedBlock:
        asked.append((block.copy(order="K"), columns))
        return fit_block(block, columns, settings)

    causal_hmm(close, SETTINGS, bar_seconds=BAR, fit=spy)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hmm_module, "fit_hmm_features", None)  # calling it would raise
        listed = refit_blocks(close, SETTINGS, bar_seconds=BAR)
    assert len(listed) == len(asked) > 2
    for item, (block, columns) in zip(listed, asked, strict=True):
        assert item.columns == columns
        assert item.block.tobytes() == block.tobytes()
        assert item.block.flags.f_contiguous == block.flags.f_contiguous


def test_refit_blocks_of_a_short_or_empty_series_is_empty() -> None:
    assert refit_blocks(close_series(FIRST_DECISION), SETTINGS, bar_seconds=BAR) == ()
    assert refit_blocks(close_series().iloc[:0], SETTINGS, bar_seconds=BAR) == ()


def test_the_carried_posterior_comes_from_the_fit_and_the_block_is_not_replayed() -> None:
    close = close_series()
    fitted: list[FittedBlock] = []

    def spy(block: np.ndarray, columns: tuple[str, ...], settings: HmmSettings) -> FittedBlock:
        fitted.append(fit_block(block, columns, settings))
        return fitted[-1]

    first = causal_hmm(close, SETTINGS, bar_seconds=BAR, fit=spy)
    stored = iter(fitted)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hmm_module, "forward_filter", None)  # a replay would raise
        second = causal_hmm(close, SETTINGS, bar_seconds=BAR, fit=lambda *_: next(stored))
    assert_same_prefix(first, second, len(close))


def test_fit_block_posterior_is_the_last_forward_filter_row() -> None:
    close = close_series()
    item = refit_blocks(close, SETTINGS, bar_seconds=BAR)[0]
    got = fit_block(item.block, item.columns, SETTINGS)
    assert np.array_equal(got.posterior, forward_filter(got.params, item.block)[0][-1])


# --------------------------------------------------------------------------- static check


@pytest.mark.parametrize("module", [hmm_module, hmm_columns_module])
def test_no_smoothed_probability_or_decoding_path_anywhere_in_the_module(module: object) -> None:
    banned = {"smooth", "viterbi", "predict", "predict_proba", "decode", "hysteresis_step"}
    banned |= {"hysteresis_decision", "hmm_summary", "fit_hmm"}
    source = Path(str(module.__file__)).read_text()  # type: ignore[attr-defined]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.alias):
            found.update(node.name.split("."))
            found.add(node.asname or node.name)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    assert not found & banned


# --------------------------------------------------------------------------- hedgeFund


def test_matches_hedgefund_at_every_decision_bar_including_each_refit_bar() -> None:
    """Three states, the same two-column feature frame, ``run_hmm_walk_forward`` directly.

    It is not the ``hmm_walk_forward_from_close`` wrapper because that wrapper always passes two
    volatility columns and this model uses one; the wrapper only builds the frame and calls this
    function. ``run_hmm_walk_forward`` hard-wires three states, hence ``n_states=3`` here.
    """
    settings = replace(SETTINGS, n_states=3, restarts=2)
    close = close_series(520, seed=11)
    columns = list(feature_columns(settings))
    features = build_features(close, vol_short=2, vol_long=settings.vol_window_bars)[columns]
    hedge = run_hmm_walk_forward(
        features,
        min_train=settings.train_bars,
        refit_every=settings.refit_every_bars,
        train_bars=settings.train_bars,
        seeds=settings.seeds,
        n_iter=settings.n_iter,
    )
    assert hedge.available
    got = causal_hmm(close, settings, bar_seconds=BAR)
    rows = close.index.get_indexer(hedge.filtered.index)
    assert rows[0] == got.warmup_bars
    assert len(hedge.refit_dates) > 5
    assert np.array_equal(got.bear_prob[rows], hedge.filtered["bear"].to_numpy())
    assert np.array_equal(got.state[rows], hedge.raw_regimes.to_numpy(dtype=np.float64))
    assert np.array_equal(got.state_prob[rows], hedge.filtered.to_numpy().max(axis=1))
    assert np.isnan(got.bear_prob[: rows[0]]).all()


def test_two_state_first_block_matches_fit_hmm_features_plus_forward_filter() -> None:
    close = close_series()
    columns = feature_columns(SETTINGS)
    features = build_features(close, vol_short=2, vol_long=SETTINGS.vol_window_bars)
    values = features[list(columns)].to_numpy(dtype=np.float64)
    block = values[: SETTINGS.train_bars]
    scaler = fit_scaler(block, columns)
    scaled = scaler.transform(block)
    fitted = fit_hmm_features(
        scaled,
        feature_names=columns,
        seeds=SETTINGS.seeds,
        n_components=2,
        n_iter=SETTINGS.n_iter,
    )
    assert fitted is not None
    carried = forward_filter(fitted.params, scaled)[0][-1]
    post, _ = filter_step(fitted.params, carried, scaler.transform(values[SETTINGS.train_bars]))
    got = causal_hmm(close, SETTINGS, bar_seconds=BAR)
    at = FIRST_DECISION
    assert got.bear_prob[at] == post[0]
    assert got.state[at] == float(np.argmax(post))
    assert got.state_prob[at] == post.max()


# --------------------------------------------------------------------------- labels


def test_labels_stay_ordered_and_stable_across_refits_on_two_regimes() -> None:
    settings = replace(SETTINGS, train_bars=600, restarts=2)
    close, regime = two_regime_close(1500, seed=5)
    means: list[np.ndarray] = []

    def spy(block: np.ndarray, columns: tuple[str, ...], s: HmmSettings) -> FittedBlock:
        fitted = fit_block(block, columns, s)
        means.append(fitted.params.means[:, 0])
        return fitted

    out = causal_hmm(close, settings, bar_seconds=BAR, fit=spy)
    assert len(means) > 15
    assert all(m[0] < m[1] for m in means)  # state 0 has the lowest mean return in every fit
    decided = np.isfinite(out.state)
    assert decided.sum() > 800
    assert (out.state[decided] == regime[decided]).mean() > 0.85
    # no refit span has its labels swapped: each one agrees with the truth, not just on average
    refit_spans = np.array_split(np.flatnonzero(decided), len(means))
    for span in refit_spans:
        assert (out.state[span] == regime[span]).mean() > 0.5
    # a falling, volatile regime reads as bear with confidence, and a rising one does not
    assert np.nanmean(out.bear_prob[regime == 0]) > 0.8
    assert np.nanmean(out.bear_prob[regime == 1]) < 0.2


def test_the_diagnostic_is_true_when_the_bear_state_is_the_volatile_one() -> None:
    settings = replace(SETTINGS, train_bars=600, restarts=2)
    close, _ = two_regime_close(1100, seed=5)
    fits: list[FittedBlock] = []

    def spy(block: np.ndarray, columns: tuple[str, ...], s: HmmSettings) -> FittedBlock:
        fits.append(fit_block(block, columns, s))
        return fits[-1]

    out = causal_hmm(close, settings, bar_seconds=BAR, fit=spy)
    decided = np.isfinite(out.state)
    assert np.array_equal(np.isfinite(out.bear_high_vol), decided)
    assert (out.bear_high_vol[decided] == 1.0).all()  # the falling regime is the volatile one
    assert all(f.params.covars[0, 0, 0] > f.params.covars[-1, 0, 0] for f in fits)


def test_the_diagnostic_reports_label_flips_when_states_differ_only_in_volatility() -> None:
    """Equal means: ordering by mean return is a coin toss per fit, so state 0 is the volatile
    state in some fits and the calm one in others. Read alone, ``bear_prob`` means opposite things
    across those fits, and the diagnostic is what says which one a bar got."""
    settings = replace(SETTINGS, train_bars=500, restarts=2)
    close, volatile = equal_mean_close(1500, seed=8)
    out = causal_hmm(close, settings, bar_seconds=BAR)
    decided = np.isfinite(out.state)
    flag = out.bear_high_vol
    high, low = decided & (flag == 1.0), decided & (flag == 0.0)
    assert high.sum() > 100 and low.sum() > 100  # the labels flipped between fits
    # settled bars: the first week after a regime change is the filter's lag, not a label error
    settled = ((np.arange(len(close)) % 300) > 42) & decided
    assert (out.state[high & settled] == 0.0)[volatile[high & settled] == 1].mean() > 0.8
    assert (out.state[low & settled] == 1.0)[volatile[low & settled] == 1].mean() > 0.8


def test_probabilities_and_state_agree() -> None:
    out = causal_hmm(close_series(), replace(SETTINGS, n_states=3), bar_seconds=BAR)
    decided = np.isfinite(out.state)
    assert decided.any()
    assert set(np.unique(out.state[decided])) <= {0.0, 1.0, 2.0}
    assert ((out.bear_prob[decided] >= 0.0) & (out.bear_prob[decided] <= 1.0)).all()
    assert (out.state_prob[decided] >= out.bear_prob[decided]).all()
    assert (out.state_prob[decided] >= 1.0 / 3.0).all()
    bear = out.state[decided] == 0.0
    assert np.array_equal(out.state_prob[decided][bear], out.bear_prob[decided][bear])


# --------------------------------------------------------------------------- warm-up and gaps


def test_nan_during_warm_up_and_warmup_bars_is_the_first_finite_row() -> None:
    out = causal_hmm(close_series(), SETTINGS, bar_seconds=BAR)
    assert out.warmup_bars == FIRST_DECISION
    for values in (out.state, out.bear_prob, out.state_prob):
        assert values.dtype == np.float64
        assert np.isnan(values[: out.warmup_bars]).all()
        assert np.isfinite(values[out.warmup_bars :]).all()


def test_a_series_too_short_for_the_first_decision_is_all_nan() -> None:
    close = close_series(FIRST_DECISION)
    out = causal_hmm(close, SETTINGS, bar_seconds=BAR)
    assert out.warmup_bars == FIRST_DECISION
    assert np.isnan(out.bear_prob).all()
    empty = causal_hmm(close.iloc[:0], SETTINGS, bar_seconds=BAR)
    assert empty.warmup_bars == 0
    assert empty.state.shape == (0,)


def test_nothing_is_forward_filled_across_a_gap() -> None:
    w = SETTINGS.vol_window_bars
    removed = 300
    close = without_rows(close_series(), removed)  # the row at `removed` now spans two bars
    out = causal_hmm(close, SETTINGS, bar_seconds=BAR)
    for values in (out.state, out.bear_prob, out.state_prob):
        assert np.isfinite(values[out.warmup_bars : removed]).all()
        assert np.isnan(values[removed : removed + w]).all()  # the return and its vol window
        assert np.isfinite(values[removed + w :]).all()


def test_a_gap_is_measured_in_time_not_rows() -> None:
    close = close_series()
    assert_same_prefix(
        causal_hmm(close, SETTINGS, bar_seconds=BAR),
        causal_hmm(close, SETTINGS, bar_seconds=BAR * 2),  # 4h rows are not gaps on 8h bars
        len(close),
    )
    coarse = causal_hmm(close, SETTINGS, bar_seconds=BAR // 2)  # every row is a gap on 2h bars
    assert coarse.warmup_bars == len(close)


def test_gap_in_warm_up_delays_the_first_decision_by_its_lost_rows() -> None:
    """Warm-up counts usable rows, so a gap costs the rows it blanks and nothing else."""
    w = SETTINGS.vol_window_bars
    close = without_rows(close_series(), 100)
    out = causal_hmm(close, SETTINGS, bar_seconds=BAR)
    assert out.warmup_bars == w + SETTINGS.train_bars + w


def test_the_filter_predicts_only_across_rows_without_an_observation() -> None:
    """A fixed model, hand-stepped: across NaN rows the posterior moves by the transition matrix
    alone, with no emission update, and the bars around the gap match that path exactly."""
    w = SETTINGS.vol_window_bars
    close = without_rows(close_series(), 300)
    columns = feature_columns(SETTINGS)
    params = HMMParams(
        startprob=np.array([0.5, 0.5]),
        transmat=np.array([[0.9, 0.1], [0.3, 0.7]]),
        means=np.array([[-0.5, 0.5], [0.5, -0.5]]),
        covars=np.array([np.eye(2), np.eye(2)]),
    )
    long_refit = replace(SETTINGS, refit_every_bars=10_000)

    def fixed(block: np.ndarray, _columns: tuple[str, ...], _s: HmmSettings) -> FittedBlock:
        return FittedBlock(params, forward_filter(params, block)[0][-1])

    out = causal_hmm(close, long_refit, bar_seconds=BAR, fit=fixed)

    pieces = [
        build_features(close.iloc[:300], vol_short=2, vol_long=w),
        build_features(close.iloc[300:], vol_short=2, vol_long=w),
    ]
    values = pd.concat(pieces)[list(columns)]
    raw = values.to_numpy(dtype=np.float64)
    train = SETTINGS.train_bars
    block = raw[:train]
    scaler = fit_scaler(block, columns)
    carried = forward_filter(params, scaler.transform(block))[0][-1]
    rows = close.index.get_indexer(values.index)
    for i in range(train, len(raw)):
        for _ in range(int(rows[i] - rows[i - 1]) - 1):
            carried = carried @ params.transmat
        carried, _ = filter_step(params, carried, scaler.transform(raw[i]))
        assert out.bear_prob[rows[i]] == carried[0]


def test_training_blocks_skip_rows_without_an_observation() -> None:
    close = without_rows(close_series(), 120, 180)
    seen: list[bool] = []

    def spy(block: np.ndarray, columns: tuple[str, ...], settings: HmmSettings) -> FittedBlock:
        seen.append(bool(np.isfinite(block).all()) and block.shape[0] == settings.train_bars)
        return fit_block(block, columns, settings)

    causal_hmm(close, SETTINGS, bar_seconds=BAR, fit=spy)
    assert seen and all(seen)


def zero_volume_mask(n: int, *rows: int) -> np.ndarray:
    mask = np.ones(n, dtype=bool)
    mask[list(rows)] = False
    return mask


def test_all_rows_observed_is_bit_identical_to_no_mask() -> None:
    close = close_series()
    plain = causal_hmm(close, SETTINGS, bar_seconds=BAR)
    masked = causal_hmm(close, SETTINGS, bar_seconds=BAR, observed=np.ones(len(close), dtype=bool))
    assert_same_prefix(plain, masked, len(close))
    assert plain.warmup_bars == masked.warmup_bars


def test_an_unobserved_row_blanks_its_return_and_the_next_one_with_nothing_filled() -> None:
    w = SETTINGS.vol_window_bars
    close = close_series()
    row = 300
    out = causal_hmm(close, SETTINGS, bar_seconds=BAR, observed=zero_volume_mask(len(close), row))
    for values in (out.state, out.bear_prob, out.state_prob, out.bear_high_vol):
        assert np.isfinite(values[out.warmup_bars : row]).all()
        assert np.isnan(values[row : row + 1 + w]).all()  # the row, then a fresh volatility window
        assert np.isfinite(values[row + 1 + w :]).all()


def test_rows_before_an_unobserved_row_do_not_move() -> None:
    close = close_series()
    row = 300
    plain = causal_hmm(close, SETTINGS, bar_seconds=BAR)
    masked = causal_hmm(
        close, SETTINGS, bar_seconds=BAR, observed=zero_volume_mask(len(close), row)
    )
    assert_same_prefix(plain, masked, row)


def test_the_mask_is_causal() -> None:
    close = close_series()
    early = zero_volume_mask(len(close), 280)
    late = zero_volume_mask(len(close), 280, 400)
    a = causal_hmm(close, SETTINGS, bar_seconds=BAR, observed=early)
    b = causal_hmm(close, SETTINGS, bar_seconds=BAR, observed=late)
    assert_same_prefix(a, b, 400)


def test_refit_blocks_follow_the_mask() -> None:
    close = close_series()
    observed = zero_volume_mask(len(close), 120)
    asked: list[bytes] = []

    def spy(block: np.ndarray, columns: tuple[str, ...], settings: HmmSettings) -> FittedBlock:
        asked.append(block.tobytes())
        return fit_block(block, columns, settings)

    causal_hmm(close, SETTINGS, bar_seconds=BAR, fit=spy, observed=observed)
    listed = refit_blocks(close, SETTINGS, bar_seconds=BAR, observed=observed)
    assert [item.block.tobytes() for item in listed] == asked


# --------------------------------------------------------------------------- input checks


def test_bad_inputs_are_named() -> None:
    close = close_series()
    with pytest.raises(ValueError, match="DatetimeIndex"):
        causal_hmm(close.reset_index(drop=True), SETTINGS, bar_seconds=BAR)
    with pytest.raises(ValueError, match="bar_seconds"):
        causal_hmm(close, SETTINGS, bar_seconds=0)
    with pytest.raises(ValueError, match="vol_window_bars"):
        causal_hmm(close, replace(SETTINGS, vol_window_bars=2), bar_seconds=BAR)
    with pytest.raises(ValueError, match="observed"):
        causal_hmm(close, SETTINGS, bar_seconds=BAR, observed=np.ones(3, dtype=bool))


def test_fit_block_says_when_hmmlearn_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hmm_module, "fit_hmm_features", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="hmmlearn"):
        fit_block(np.zeros((4, 2)), ("log_return", "vol_3"), SETTINGS)


# --------------------------------------------------------------------------- registry


def test_the_features_are_registered_without_params() -> None:
    assert set(HMM_FEATURE_NAMES) <= set(REGISTRY)
    assert HMM_COLUMNS == (
        HMM_STATE_COLUMN,
        HMM_BEAR_PROB_COLUMN,
        HMM_STATE_PROB_COLUMN,
        HMM_BEAR_HIGH_VOL_COLUMN,
    )
    for name in HMM_FEATURE_NAMES:
        spec = REGISTRY[name]
        assert spec.params == ()
        assert spec.outputs == ("value",)
        assert spec.warmup({}) == 0
        assert output_scale(name, "value") == DIMENSIONLESS


@pytest.mark.parametrize(
    ("name", "column"),
    [("hmm_state", HMM_STATE_COLUMN), ("hmm_bear_prob", HMM_BEAR_PROB_COLUMN)],
)
def test_each_feature_reads_its_column(name: str, column: str) -> None:
    candles = with_hmm(make_candles(100, seed=2), seed=2)
    got = REGISTRY[name].compute(candles, {})
    assert got.index.equals(candles.index)
    assert np.array_equal(got["value"].to_numpy(), candles[column].to_numpy(), equal_nan=True)


@pytest.mark.parametrize(
    ("name", "column"),
    [("hmm_state", HMM_STATE_COLUMN), ("hmm_bear_prob", HMM_BEAR_PROB_COLUMN)],
)
def test_a_missing_column_is_named_with_the_config_section(name: str, column: str) -> None:
    with pytest.raises(ValueError, match=rf"{column}.*\[regime\.hmm\]"):
        REGISTRY[name].compute(make_candles(20, seed=2), {})
