"""Benchmarks: buy-and-hold, vol-targeted buy-and-hold, random-entry null (hand-checked)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from lab.engine import run_signals, slippage_series
from lab.evaluate.benchmarks import (
    buy_and_hold,
    random_entry_null,
    random_entry_signals,
    vol_target_buy_and_hold,
    vol_target_weights,
)
from lab.stats import compute_metrics
from lab.types import ZERO_COSTS, CostModel, RiskSpec, bars_per_year
from tests.synthetic import candles_from_rows, make_candles, perturb_after

FLAT_ROWS = [(100.0, 100.0, 100.0, 100.0)] * 2


# --------------------------------------------------------------------------- buy and hold


def test_buy_and_hold_buys_once_at_the_second_open_and_holds() -> None:
    c = candles_from_rows([(100, 100, 100, 100), (100, 100, 100, 100), (200, 200, 200, 200),
                           (400, 400, 400, 400)])  # fmt: skip
    res = buy_and_hold(c, costs=ZERO_COSTS, init_cash=10_000.0)
    # 10_000 / 100 = 100 units bought at bar 1's open; marked at 100, 200, 400.
    assert res.equity.tolist() == pytest.approx([10_000.0, 10_000.0, 20_000.0, 40_000.0])
    assert len(res.trades) == 1 and res.trades[0].exit_reason == "end"
    assert res.trades[0].entry_time == c.index[1]


def test_buy_and_hold_pays_the_entry_fee_on_the_committed_cash() -> None:
    c = candles_from_rows([(100, 100, 100, 100), (100, 100, 100, 100), (400, 400, 400, 400)])
    res = buy_and_hold(c, costs=CostModel(0.001, 0.0, 0.0), init_cash=10_000.0)
    units = 10_000.0 / (100.0 * 1.001)  # notional + fee = all the cash
    assert res.equity.iloc[-1] == pytest.approx(units * 400.0, rel=1e-12)
    assert res.fees_paid == pytest.approx(units * 100.0 * 0.001, rel=1e-12)


def test_buy_and_hold_needs_two_bars() -> None:
    with pytest.raises(ValueError, match="at least 2 bars"):
        buy_and_hold(candles_from_rows(FLAT_ROWS[:1]), costs=ZERO_COSTS)


# --------------------------------------------------------------------------- vol target


def test_vol_target_weights_match_a_hand_calculation() -> None:
    closes = [100.0, 102.0, 99.0, 103.0, 101.0, 104.0, 100.0]
    c = candles_from_rows([(x, x, x, x) for x in closes], interval="1d")
    w = vol_target_weights(c, "1d", target_annual=0.5, lookback_days=3, max_leverage=1.0)
    logr = np.diff(np.log(closes))
    expected = [0.0, 0.0, 0.0]  # warm-up: 3 returns are needed, the first exists at bar 1
    for t in range(3, len(closes)):
        vol = np.std(logr[t - 3 : t], ddof=1) * math.sqrt(365.0)
        expected.append(min(1.0, 0.5 / vol))
    assert w.tolist() == pytest.approx(expected, rel=1e-12)
    assert 0 < w.iloc[3] < 1  # this series is volatile enough for the target to bind


def test_vol_target_weights_are_capped_at_max_leverage() -> None:
    c = make_candles(400, seed=1, vol=0.001)  # ~4.7% annual vol, far below a 40% target
    w = vol_target_weights(c, "4h", target_annual=0.4, lookback_days=30, max_leverage=0.8)
    lookback = 30 * 6
    assert (w.iloc[:lookback] == 0.0).all()
    assert (w.iloc[lookback:] == 0.8).all()


def test_vol_target_weights_at_t_ignore_prices_after_t() -> None:
    c = make_candles(800, seed=3, vol=0.02)
    base = vol_target_weights(c, "4h", target_annual=0.4, lookback_days=30, max_leverage=1.0)
    for t in (200, 450, 700):
        moved = vol_target_weights(
            perturb_after(c, t), "4h", target_annual=0.4, lookback_days=30, max_leverage=1.0
        )
        np.testing.assert_array_equal(base.to_numpy()[: t + 1], moved.to_numpy()[: t + 1])
        assert not np.array_equal(base.to_numpy()[t + 1 :], moved.to_numpy()[t + 1 :])


def test_vol_target_weights_reject_leverage_above_spot() -> None:
    c = make_candles(50)
    with pytest.raises(ValueError, match="max_leverage"):
        vol_target_weights(c, "4h", target_annual=0.4, lookback_days=1, max_leverage=2.0)


def test_vol_target_rebalance_band_trades_less_than_rebalancing_every_bar() -> None:
    c = make_candles(1500, seed=5, vol=0.03)
    kw = dict(costs=CostModel(0.001, 0.0005, 0.0), target_annual=0.4, lookback_days=10)
    every = vol_target_buy_and_hold(c, "4h", max_leverage=1.0, min_rebalance=0.0, **kw)  # type: ignore[arg-type]
    banded = vol_target_buy_and_hold(c, "4h", max_leverage=1.0, min_rebalance=0.1, **kw)  # type: ignore[arg-type]
    assert banded.fees_paid < every.fees_paid
    assert (banded.position <= 1.0 + 1e-9).all() and (banded.position >= 0).all()


def test_vol_target_buy_and_hold_uses_supplied_weights() -> None:
    c = make_candles(100, seed=2)
    w = pd.Series(np.nan, index=c.index)
    w.iloc[0] = 0.5
    res = vol_target_buy_and_hold(
        c, "4h", costs=ZERO_COSTS, target_annual=0.4, lookback_days=1, max_leverage=1.0, weights=w
    )
    assert res.position.iloc[5] == pytest.approx(0.5, rel=0.05)


# --------------------------------------------------------------------------- random entry


@pytest.mark.parametrize("seed", range(20))
def test_random_entry_signals_place_non_overlapping_trades(seed: int) -> None:
    rng = np.random.default_rng(seed)
    n = 200
    holding = rng.integers(1, 12, size=10)
    ent, ex = random_entry_signals(n, holding, rng)
    e_rows, x_rows = np.flatnonzero(ent), np.flatnonzero(ex)
    assert len(e_rows) == len(x_rows) == 10
    np.testing.assert_array_equal(x_rows - e_rows, holding)
    assert (e_rows[1:] > x_rows[:-1]).all()  # next entry signal after the previous exit
    assert x_rows[-1] <= n - 2  # the last exit can still fill


def test_random_entry_trades_hold_for_the_drawn_lengths_in_the_engine() -> None:
    c = make_candles(300, seed=4)
    holding = np.array([3, 7, 1, 12, 5])
    ent, ex = random_entry_signals(len(c), holding, np.random.default_rng(0))
    res = run_signals(c, pd.Series(ent, c.index), pd.Series(ex, c.index), costs=ZERO_COSTS)
    assert [t.bars_held for t in res.trades] == holding.tolist()


def test_random_entry_signals_raise_when_trades_do_not_fit() -> None:
    with pytest.raises(ValueError, match="do not fit"):
        random_entry_signals(10, np.array([5, 5]), np.random.default_rng(0))


def test_random_entry_null_is_seeded_and_keeps_the_trade_count() -> None:
    c = make_candles(600, seed=6)
    kw = dict(
        n_trades=8, holding_bars=[4, 10, 20, 4, 6, 10, 2, 20], costs=ZERO_COSTS, risk=RiskSpec(),
        n_paths=12,
    )  # fmt: skip
    a = random_entry_null(c, "4h", seed=1, **kw)  # type: ignore[arg-type]
    b = random_entry_null(c, "4h", seed=1, **kw)  # type: ignore[arg-type]
    other = random_entry_null(c, "4h", seed=2, **kw)  # type: ignore[arg-type]
    np.testing.assert_array_equal(a.sharpes, b.sharpes)
    assert not np.array_equal(a.sharpes, other.sharpes)
    assert a.n_trades == 8 and a.n_paths == 12 and a.sharpes.shape == (12,)
    assert a.sharpe_median == pytest.approx(float(np.median(a.sharpes)))
    assert a.sharpe_p95 == pytest.approx(float(np.percentile(a.sharpes, 95)))


def test_random_entry_path_sharpe_is_the_engine_sharpe_of_its_trades() -> None:
    c = make_candles(400, seed=8)
    lengths = [6, 2, 9, 4, 6]
    res = random_entry_null(
        c, "4h", n_trades=5, holding_bars=lengths, costs=ZERO_COSTS, risk=RiskSpec(), n_paths=1,
        seed=3,
    )  # fmt: skip
    # Re-draw the same path by hand: one permutation of the 5 lengths, then the placement.
    rng = np.random.default_rng(3)
    holding = rng.permutation(np.array(lengths))
    ent, ex = random_entry_signals(len(c), holding, rng)
    direct = run_signals(c, pd.Series(ent, c.index), pd.Series(ex, c.index), costs=ZERO_COSTS)
    assert res.sharpes[0] == compute_metrics(direct, "4h").sharpe
    assert len(direct.trades) == 5


def test_random_entry_percentile_ranks_the_real_sharpe() -> None:
    c = make_candles(500, seed=9)
    res = random_entry_null(
        c, "4h", n_trades=6, holding_bars=[5, 9, 5, 9, 5, 9], costs=ZERO_COSTS, risk=RiskSpec(),
        n_paths=40, seed=0,
    )  # fmt: skip
    assert res.percentile(1e9) == 100.0
    assert res.percentile(-1e9) == 0.0
    assert math.isnan(res.percentile(math.nan))


def test_random_entry_null_without_trades_is_empty() -> None:
    res = random_entry_null(
        make_candles(100), "4h", n_trades=0, holding_bars=[], costs=ZERO_COSTS, risk=RiskSpec(),
        n_paths=5, seed=0,
    )  # fmt: skip
    assert res.n_paths == 0 and res.sharpes.size == 0 and math.isnan(res.sharpe_median)


def test_random_entry_holding_shrinks_to_fit_long_strategy_trades() -> None:
    c = make_candles(100, seed=1)
    res = random_entry_null(
        c, "4h", n_trades=4, holding_bars=[60] * 4, costs=ZERO_COSTS, risk=RiskSpec(), n_paths=3,
        seed=0,
    )  # fmt: skip
    assert np.isfinite(res.sharpes).all()
    # 100 bars: budget = 99 - 4 = 95 holding bars, trimmed evenly from the longest holds.
    assert (res.exposure_bars == 95).all()


def test_annualisation_uses_365_days() -> None:
    assert bars_per_year("4h") == 2190.0


def _strategy_with_stops(c: pd.DataFrame) -> tuple[list[int], RiskSpec]:
    rng = np.random.default_rng(13)
    entries = pd.Series(rng.random(len(c)) < 0.05, index=c.index)
    exits = pd.Series(rng.random(len(c)) < 0.02, index=c.index)
    risk = RiskSpec(position_size_pct=100, stop_loss_pct=1.5, take_profit_pct=3.0)
    res = run_signals(c, entries, exits, costs=CostModel(0.001, 0.0005, 0.0), risk=risk)
    assert any(t.exit_reason in ("stop_loss", "take_profit") for t in res.trades)
    return [t.bars_held for t in res.trades], risk


def test_random_entry_paths_hold_exactly_the_strategys_total_bars() -> None:
    c = make_candles(1500, seed=14, vol=0.015)
    held, risk = _strategy_with_stops(c)
    held = [max(1, h) for h in held]  # a zero-bar stop-out cannot be placed; it counts as 1
    res = random_entry_null(
        c, "4h", n_trades=len(held), holding_bars=held, costs=CostModel(0.001, 0.0005, 0.0),
        risk=risk, n_paths=25, seed=4,
    )  # fmt: skip
    assert res.exposure_bars.shape == (25,)
    assert (res.exposure_bars == sum(held)).all()  # same exposure on every path


def test_random_entry_null_ignores_the_strategys_stops() -> None:
    c = make_candles(1500, seed=14, vol=0.015)
    held, risk = _strategy_with_stops(c)
    kw = dict(n_trades=len(held), holding_bars=held, costs=CostModel(0.001, 0.0005, 0.0),
              n_paths=10, seed=2)  # fmt: skip
    with_stops = random_entry_null(c, "4h", risk=risk, **kw)  # type: ignore[arg-type]
    without = random_entry_null(c, "4h", risk=RiskSpec(position_size_pct=100), **kw)  # type: ignore[arg-type]
    np.testing.assert_array_equal(with_stops.sharpes, without.sharpes)


def test_random_entry_null_needs_one_holding_length_per_trade() -> None:
    with pytest.raises(ValueError, match="per strategy trade"):
        random_entry_null(
            make_candles(200), "4h", n_trades=3, holding_bars=[5], costs=ZERO_COSTS,
            risk=RiskSpec(), n_paths=2, seed=0,
        )  # fmt: skip


def test_benchmarks_warm_slippage_up_on_cost_history() -> None:
    history = make_candles(400, seed=15, vol=0.02)
    window = history.iloc[250:]
    costs = CostModel(0.001, 0.0005, 1.0, 1.0)
    cold = buy_and_hold(window, costs=costs)
    warm = buy_and_hold(window, costs=costs, cost_history=history)
    # Same single buy at the window's bar 1 open; only its slippage differs (warm-up vs real).
    assert cold.trades[0].entry_time == warm.trades[0].entry_time
    assert cold.trades[0].entry_price == window["open"].iloc[1] * 1.0005
    slip = slippage_series(history, costs)[250 + 1]
    assert warm.trades[0].entry_price == window["open"].iloc[1] * (1 + slip)
