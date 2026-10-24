"""Engine fill timing: next tradable open, gaps, delay, signal conflicts, causality, validation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab.engine import run_signals, slippage_series
from lab.types import ZERO_COSTS, BacktestResult, CostModel, RiskSpec
from tests.synthetic import candles_from_rows, make_candles, perturb_after

# Opens are distinct per bar so the fill bar can be read straight off the entry price.
ROWS = [
    (100.0, 101.0, 99.0, 100.0),
    (101.0, 102.0, 100.0, 101.0),
    (102.0, 103.0, 101.0, 102.0),
    (103.0, 104.0, 102.0, 103.0),
    (104.0, 105.0, 103.0, 104.0),
    (105.0, 106.0, 104.0, 105.0),
]


def sig(index: pd.DatetimeIndex, *rows: int) -> pd.Series:
    out = np.zeros(len(index), dtype=bool)
    out[list(rows)] = True
    return pd.Series(out, index=index)


def run(
    candles: pd.DataFrame, entries: list[int], exits: list[int], **kw: object
) -> BacktestResult:
    return run_signals(
        candles, sig(candles.index, *entries), sig(candles.index, *exits), costs=ZERO_COSTS, **kw
    )


def test_signal_fills_at_next_bar_open() -> None:
    candles = candles_from_rows(ROWS)
    res = run(candles, [1], [3])
    (trade,) = res.trades
    assert trade.entry_time == candles.index[2] and trade.entry_price == 102.0
    assert trade.exit_time == candles.index[4] and trade.exit_price == 104.0
    assert trade.exit_reason == "signal" and trade.bars_held == 2
    # Flat until bar 2's open, so exposure is 0 at the close of bars 0 and 1.
    assert list(res.position.to_numpy() != 0) == [False, False, True, True, False, False]


def test_signal_before_untradable_bar_fills_after_it() -> None:
    candles = candles_from_rows(ROWS)
    candles.loc[candles.index[2], "tradable"] = False
    candles.loc[candles.index[3], "tradable"] = False
    (trade,) = run(candles, [1], [4]).trades
    assert trade.entry_time == candles.index[4] and trade.entry_price == 104.0
    assert trade.exit_time == candles.index[5] and trade.exit_price == 105.0


def test_exit_signal_before_untradable_bar_fills_after_it() -> None:
    candles = candles_from_rows(ROWS)
    candles.loc[candles.index[3], "tradable"] = False
    (trade,) = run(candles, [0], [2]).trades
    assert trade.exit_time == candles.index[4] and trade.exit_price == 104.0


def test_signal_before_absent_bar_fills_on_next_existing_bar() -> None:
    full = candles_from_rows(ROWS)
    candles = full.drop(full.index[2])  # a time gap: the 3rd bar never printed
    (trade,) = run(candles, [1], [3]).trades
    assert trade.entry_time == full.index[3] and trade.entry_price == 103.0
    assert trade.exit_time == full.index[5] and trade.exit_price == 105.0


@pytest.mark.parametrize(("delay", "fill_bar"), [(0, 1), (1, 2), (2, 3)])
def test_delay_shifts_the_fill(delay: int, fill_bar: int) -> None:
    candles = candles_from_rows(ROWS)
    (trade,) = run(candles, [0], [], delay=delay).trades
    assert trade.entry_time == candles.index[fill_bar]
    assert trade.entry_price == ROWS[fill_bar][0]


def test_delay_then_skips_untradable_bars() -> None:
    candles = candles_from_rows(ROWS)
    candles.loc[candles.index[2], "tradable"] = False
    (trade,) = run(candles, [0], [], delay=1).trades  # row 0 + 1 -> next tradable after row 1
    assert trade.entry_time == candles.index[3]


def test_same_bar_entry_and_exit_while_flat_does_not_enter() -> None:
    candles = candles_from_rows(ROWS)
    res = run(candles, [1], [1])
    assert res.trades == ()
    assert np.all(res.equity == 10_000.0)


def test_exit_wins_over_entry_while_in_position() -> None:
    candles = candles_from_rows(ROWS)
    (trade,) = run(candles, [0, 2], [2]).trades
    assert trade.entry_time == candles.index[1]
    assert trade.exit_time == candles.index[3] and trade.exit_reason == "signal"


def test_entries_while_in_position_are_ignored() -> None:
    candles = candles_from_rows(ROWS)
    res = run(candles, [0, 1, 2], [3])
    (trade,) = res.trades
    # All cash was spent at bar 1's open (101): size = 10000 / 101, never topped up.
    assert trade.size == pytest.approx(10_000.0 / 101.0, rel=1e-12)
    assert trade.entry_price == 101.0


def test_reentry_after_exit() -> None:
    candles = candles_from_rows(ROWS)
    res = run(candles, [0, 3], [1])
    assert [t.entry_time for t in res.trades] == [candles.index[1], candles.index[4]]
    assert res.trades[0].exit_time == candles.index[2]


def test_signal_on_last_bar_is_dropped() -> None:
    candles = candles_from_rows(ROWS)
    res = run(candles, [5], [])
    assert res.trades == ()


def test_fill_on_last_bar_is_an_open_trade() -> None:
    candles = candles_from_rows(ROWS)
    (trade,) = run(candles, [4], []).trades
    assert trade.entry_time == candles.index[5]
    assert trade.exit_time is None and trade.exit_reason == "end" and trade.bars_held == 0


@pytest.mark.parametrize("t", [30, 100, 170])
def test_outputs_up_to_t_ignore_prices_and_signals_after_t(t: int) -> None:
    candles = make_candles(200, seed=11, vol=0.02)
    rng = np.random.default_rng(5)
    entries = pd.Series(rng.random(200) < 0.15, index=candles.index)
    exits = pd.Series(rng.random(200) < 0.15, index=candles.index)
    costs = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.5)
    risk = RiskSpec(position_size_pct=80, stop_loss_pct=2.0, take_profit_pct=3.0)
    base = run_signals(candles, entries, exits, costs=costs, risk=risk)

    # Different future prices and different future signals.
    entries2, exits2 = entries.copy(), exits.copy()
    entries2.iloc[t + 1 :] = ~entries.iloc[t + 1 :]
    exits2.iloc[t + 1 :] = ~exits.iloc[t + 1 :]
    other = run_signals(perturb_after(candles, t), entries2, exits2, costs=costs, risk=risk)

    for name in ("equity", "returns", "gross_returns", "position"):
        a = getattr(base, name).to_numpy()[: t + 1]
        b = getattr(other, name).to_numpy()[: t + 1]
        assert np.array_equal(a, b), name
    cutoff = candles.index[t]
    closed_a = [x for x in base.trades if x.exit_time is not None and x.exit_time <= cutoff]
    closed_b = [x for x in other.trades if x.exit_time is not None and x.exit_time <= cutoff]
    assert closed_a == closed_b and len(closed_a) > 0


def test_short_direction_fills_next_bar_open() -> None:
    candles = candles_from_rows(ROWS)
    (trade,) = run(candles, [0], [2], direction="short").trades
    assert trade.direction == "short"
    assert trade.entry_price == 101.0 and trade.exit_price == 103.0
    assert trade.pnl < 0  # price rose against the short


# --------------------------------------------------------------------------- validation


def test_rejects_misaligned_signal_index() -> None:
    candles = candles_from_rows(ROWS)
    entries = sig(candles.index, 0).iloc[1:]
    with pytest.raises(ValueError, match="entries"):
        run_signals(candles, entries, sig(candles.index), costs=ZERO_COSTS)


def test_rejects_non_bool_signal() -> None:
    candles = candles_from_rows(ROWS)
    exits = pd.Series(np.zeros(len(candles)), index=candles.index)
    with pytest.raises(ValueError, match="exits"):
        run_signals(candles, sig(candles.index), exits, costs=ZERO_COSTS)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"delay": -1}, "delay"),
        ({"delay": 1.5}, "delay"),
        ({"direction": "both"}, "direction"),
        ({"init_cash": 0.0}, "init_cash"),
        ({"risk": RiskSpec(position_size_pct=0)}, "position_size_pct"),
        ({"risk": RiskSpec(position_size_pct=120)}, "position_size_pct"),
        ({"risk": RiskSpec(stop_loss_pct=-1)}, "stop_loss_pct"),
        ({"risk": RiskSpec(take_profit_pct=0)}, "take_profit_pct"),
    ],
)
def test_rejects_bad_parameters(kwargs: dict[str, object], match: str) -> None:
    candles = candles_from_rows(ROWS)
    with pytest.raises(ValueError, match=match):
        run_signals(candles, sig(candles.index), sig(candles.index), costs=ZERO_COSTS, **kwargs)


def test_rejects_non_bool_tradable_column() -> None:
    candles = candles_from_rows(ROWS)
    candles["tradable"] = 1.0
    with pytest.raises(ValueError, match="tradable"):
        run(candles, [0], [])


# --------------------------------------------------------------------------- cost history

VOL_COSTS = CostModel(fee_rate=0.0, slippage_rate=0.0005, slippage_vol_mult=1.0, slippage_cap=1.0)


def _history_and_window(n: int = 400, window: int = 150) -> tuple[pd.DataFrame, pd.DataFrame]:
    history = make_candles(n, seed=23, vol=0.02)
    k = n - window
    # Give the window's first bar an unusually wide range so its excess over the trailing
    # median is > 0 once the median is known (it is only known with the earlier history).
    history.iloc[k, history.columns.get_loc("high")] *= 1.2
    return history, history.iloc[k:]


def test_cost_history_gives_the_window_start_the_real_vol_scaled_slippage() -> None:
    history, candles = _history_and_window()
    entries, exits = sig(candles.index, 0), sig(candles.index, 5)
    cold = run_signals(candles, entries, exits, costs=VOL_COSTS)
    warm = run_signals(candles, entries, exits, costs=VOL_COSTS, cost_history=history)
    # Without history the 100-bar median is still NaN at the window start: base rate.
    assert cold.trades[0].entry_price == candles["open"].iloc[1] * (1 + 0.0005)
    full = slippage_series(history, VOL_COSTS)
    k = len(history) - len(candles)
    assert full[k + 1] > 0.0005  # bar k's wide range widens the fill at k + 1
    assert warm.trades[0].entry_price == candles["open"].iloc[1] * (1 + full[k + 1])
    assert warm.trades[0].entry_price > cold.trades[0].entry_price


def test_cost_history_equal_to_the_candles_changes_nothing() -> None:
    candles = make_candles(300, seed=9, vol=0.02)
    entries, exits = sig(candles.index, 5, 120), sig(candles.index, 60, 200)
    a = run_signals(candles, entries, exits, costs=VOL_COSTS)
    b = run_signals(candles, entries, exits, costs=VOL_COSTS, cost_history=candles)
    assert np.array_equal(a.equity.to_numpy(), b.equity.to_numpy())
    assert a.trades == b.trades


@pytest.mark.parametrize("t", [270, 300, 380])
def test_cost_history_run_is_causal_under_perturbation(t: int) -> None:
    history, candles = _history_and_window()
    k = len(history) - len(candles)
    rng = np.random.default_rng(3)
    entries = pd.Series(rng.random(len(candles)) < 0.1, index=candles.index)
    exits = pd.Series(rng.random(len(candles)) < 0.1, index=candles.index)
    base = run_signals(candles, entries, exits, costs=VOL_COSTS, cost_history=history)
    moved_hist = perturb_after(history, t)
    moved = moved_hist.iloc[k:]
    entries2 = entries.copy()
    entries2.iloc[t - k + 1 :] = ~entries.iloc[t - k + 1 :]
    other = run_signals(moved, entries2, exits, costs=VOL_COSTS, cost_history=moved_hist)
    upto = t - k + 1
    for name in ("equity", "returns", "position"):
        a = getattr(base, name).to_numpy()[:upto]
        b = getattr(other, name).to_numpy()[:upto]
        assert np.array_equal(a, b), name


def test_cost_history_must_end_at_the_window_and_contain_it() -> None:
    history, candles = _history_and_window()
    entries, exits = sig(candles.index, 0), sig(candles.index, 5)
    with pytest.raises(ValueError, match="cost_history"):
        run_signals(candles, entries, exits, costs=VOL_COSTS, cost_history=history.iloc[:-1])
    shifted = history.copy()
    shifted.iloc[-3, shifted.columns.get_loc("close")] *= 1.01
    with pytest.raises(ValueError, match="cost_history"):
        run_signals(candles, entries, exits, costs=VOL_COSTS, cost_history=shifted)
