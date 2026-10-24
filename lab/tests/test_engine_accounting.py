"""Engine accounting against hand-calculated 3-6 bar examples.

Costs in most examples: fee 1% and slippage 1% per side, so every number is easy to check by
hand. Bars are (open, high, low, close). Tolerance: relative 1e-12, because the engine and the
expected values reach the same numbers through a handful of float multiplications/divisions in a
different order (the exact decimal results are written in the comments).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from _pytest.python_api import ApproxBase

from lab.engine import run_signals, run_weights
from lab.types import ZERO_COSTS, BacktestResult, CostModel, RiskSpec
from tests.synthetic import candles_from_rows

FEE, SLIP = 0.01, 0.01
COSTS = CostModel(fee_rate=FEE, slippage_rate=SLIP)
CASH = 10_000.0
REL = 1e-12


def approx(x: float) -> ApproxBase:
    return pytest.approx(x, rel=REL, abs=1e-9)


def sig(index: pd.DatetimeIndex, *rows: int) -> pd.Series:
    out = np.zeros(len(index), dtype=bool)
    out[list(rows)] = True
    return pd.Series(out, index=index)


# --------------------------------------------------------------------------- long, 100%

ROWS_LONG = [
    (100.0, 100.0, 100.0, 100.0),  # 0: entry signal at close
    (100.0, 110.0, 100.0, 110.0),  # 1: buy at open 100 * 1.01 = 101
    (110.0, 120.0, 110.0, 120.0),  # 2: exit signal at close
    (121.0, 121.0, 121.0, 121.0),  # 3: sell at open 121 * 0.99 = 119.79
]

# Entry: notional + 1% fee = all cash -> notional = 10000 / 1.01 = 9900.990099...
ENTRY_NOTIONAL = CASH / (1 + FEE)
ENTRY_FEE = ENTRY_NOTIONAL * FEE  # 99.00990099...
ENTRY_PRICE = 100.0 * (1 + SLIP)  # 101
SIZE = ENTRY_NOTIONAL / ENTRY_PRICE  # 98.02960494...


def test_long_round_trip_accounting() -> None:
    candles = candles_from_rows(ROWS_LONG)
    res = run_signals(candles, sig(candles.index, 0), sig(candles.index, 2), costs=COSTS)

    exit_price = 121.0 * (1 - SLIP)  # 119.79
    proceeds = SIZE * exit_price  # 11742.96637...
    exit_fee = proceeds * FEE  # 117.4296637...
    final_cash = proceeds - exit_fee  # 11625.53671...
    # Equity at each close: 10000, 98.0296*110 = 10783.2565, 98.0296*120 = 11763.5526, cash.
    eq = [CASH, SIZE * 110.0, SIZE * 120.0, final_cash]

    (t,) = res.trades
    assert t.entry_time == candles.index[1] and t.exit_time == candles.index[3]
    assert t.entry_price == approx(ENTRY_PRICE)
    assert t.exit_price == approx(exit_price)
    assert t.size == approx(SIZE)
    assert t.fees == approx(ENTRY_FEE + exit_fee)  # 216.4395647...
    assert t.pnl == approx(SIZE * (exit_price - ENTRY_PRICE) - ENTRY_FEE - exit_fee)  # 1625.537
    assert t.pnl == approx(final_cash - CASH)
    assert t.return_pct == approx(t.pnl / (SIZE * ENTRY_PRICE))
    assert t.exit_reason == "signal" and t.bars_held == 2
    np.testing.assert_allclose(res.equity.to_numpy(), eq, rtol=REL)
    expected_returns = [0.0, eq[1] / eq[0] - 1, eq[2] / eq[1] - 1, eq[3] / eq[2] - 1]
    np.testing.assert_allclose(res.returns.to_numpy(), expected_returns, rtol=REL, atol=1e-15)
    np.testing.assert_allclose(res.position.to_numpy(), [0.0, 1.0, 1.0, 0.0], atol=1e-12)
    assert res.fees_paid == approx(ENTRY_FEE + exit_fee)
    assert res.init_cash == CASH
    assert res.equity.index.equals(candles.index)


def test_half_position_size_commits_half_of_equity_including_fee() -> None:
    candles = candles_from_rows(ROWS_LONG)
    res = run_signals(
        candles,
        sig(candles.index, 0),
        sig(candles.index, 2),
        costs=COSTS,
        risk=RiskSpec(position_size_pct=50),
    )
    # Budget 5000 incl. fee: notional = 5000 / 1.01 = 4950.495, fee 49.50495, size 49.01480.
    notional = 5_000.0 / (1 + FEE)
    size = notional / ENTRY_PRICE
    cash = CASH - 5_000.0  # 5000 left in cash
    exit_price = 121.0 * (1 - SLIP)
    final = cash + size * exit_price * (1 - FEE)  # 5000 + 5812.768 = 10812.768
    eq1 = cash + size * 110.0  # 10391.628
    (t,) = res.trades
    assert t.size == approx(size)
    assert t.fees == approx(notional * FEE + size * exit_price * FEE)
    np.testing.assert_allclose(
        res.equity.to_numpy(), [CASH, eq1, cash + size * 120.0, final], rtol=REL
    )
    assert res.position.iloc[1] == approx(size * 110.0 / eq1)  # 0.51884
    assert t.pnl == approx(final - CASH)


def test_short_round_trip_accounting() -> None:
    rows = [
        (100.0, 100.0, 100.0, 100.0),  # 0: short entry signal
        (100.0, 100.0, 90.0, 90.0),  # 1: sell short at 100 * 0.99 = 99; exit signal
        (80.0, 85.0, 80.0, 85.0),  # 2: buy back at 80 * 1.01 = 80.8
    ]
    candles = candles_from_rows(rows)
    res = run_signals(
        candles, sig(candles.index, 0), sig(candles.index, 1), costs=COSTS, direction="short"
    )
    entry = 99.0
    size = CASH / (entry * (1 + FEE))  # 100.0100010; proceeds + fee = all equity
    proceeds = size * entry  # 9900.990099
    fee_in = proceeds * FEE  # 99.00990099
    cash = CASH + proceeds - fee_in  # 19801.980198
    eq1 = cash - size * 90.0  # 10801.080108
    exit_price = 80.8
    fee_out = size * exit_price * FEE  # 80.80808
    final = cash - size * exit_price - fee_out  # 11640.364036
    (t,) = res.trades
    assert t.direction == "short"
    assert t.entry_price == approx(entry) and t.exit_price == approx(exit_price)
    assert t.size == approx(size)
    assert t.pnl == approx(size * (entry - exit_price) - fee_in - fee_out)  # 1640.364
    assert t.pnl == approx(final - CASH)
    np.testing.assert_allclose(res.equity.to_numpy(), [CASH, eq1, final], rtol=REL)
    assert res.position.iloc[1] == approx(-size * 90.0 / eq1)
    assert res.fees_paid == approx(fee_in + fee_out)


def test_open_trade_at_end_is_marked_to_last_close() -> None:
    candles = candles_from_rows(ROWS_LONG)
    res = run_signals(candles, sig(candles.index, 0), sig(candles.index), costs=COSTS)
    (t,) = res.trades
    assert t.exit_time is None and t.exit_price is None and t.exit_reason == "end"
    assert t.bars_held == 2
    assert t.fees == approx(ENTRY_FEE)  # no exit fee: not force-closed
    assert t.pnl == approx(SIZE * (121.0 - ENTRY_PRICE) - ENTRY_FEE)  # 1861.62
    assert res.equity.iloc[-1] == approx(SIZE * 121.0)
    assert res.fees_paid == approx(ENTRY_FEE)
    assert res.position.iloc[-1] == approx(1.0)


# --------------------------------------------------------------------------- stops

ENTRY_BAR = (100.0, 100.0, 100.0, 100.0)  # 0: entry signal; fill at bar 1 open -> 101
QUIET = (100.0, 102.0, 99.0, 101.0)  # stays inside a 5% stop (95.95) and 10% target (111.1)
SL_LEVEL = ENTRY_PRICE * (1 - 0.05)  # 95.95
TP_LEVEL = ENTRY_PRICE * (1 + 0.10)  # 111.1


def stop_run(
    rows: list[tuple[float, float, float, float]], **risk: float
) -> tuple[pd.DataFrame, BacktestResult]:
    candles = candles_from_rows(rows)
    return candles, run_signals(
        candles, sig(candles.index, 0), sig(candles.index), costs=COSTS, risk=RiskSpec(**risk)
    )


def test_stop_loss_fills_at_stop_level_intrabar() -> None:
    candles, res = stop_run(
        [ENTRY_BAR, QUIET, (100.0, 101.0, 94.0, 95.0), (95.0,) * 4], stop_loss_pct=5
    )
    (t,) = res.trades
    exit_price = SL_LEVEL * (1 - SLIP)  # 95.95 * 0.99 = 94.9905
    exit_fee = SIZE * exit_price * FEE
    assert t.exit_reason == "stop_loss" and t.exit_time == candles.index[2]
    assert t.exit_price == approx(exit_price) and t.bars_held == 1
    assert t.pnl == approx(SIZE * (exit_price - ENTRY_PRICE) - ENTRY_FEE - exit_fee)  # -706.6
    final = SIZE * exit_price * (1 - FEE)
    np.testing.assert_allclose(res.equity.to_numpy(), [CASH, SIZE * 101.0, final, final], rtol=REL)
    assert res.position.iloc[2] == 0.0


def test_stop_loss_is_checked_on_the_entry_bar() -> None:
    candles, res = stop_run([ENTRY_BAR, (100.0, 102.0, 95.0, 98.0), (98.0,) * 4], stop_loss_pct=5)
    (t,) = res.trades
    assert t.entry_time == candles.index[1] and t.exit_time == candles.index[1]
    assert t.bars_held == 0 and t.exit_reason == "stop_loss"
    assert t.exit_price == approx(SL_LEVEL * (1 - SLIP))
    assert res.equity.iloc[1] == approx(SIZE * SL_LEVEL * (1 - SLIP) * (1 - FEE))


def test_stop_loss_gap_through_fills_at_open() -> None:
    candles, res = stop_run([ENTRY_BAR, QUIET, (90.0, 92.0, 88.0, 91.0)], stop_loss_pct=5)
    (t,) = res.trades
    assert t.exit_reason == "stop_loss" and t.exit_time == candles.index[2]
    assert t.exit_price == approx(90.0 * (1 - SLIP))  # 89.1, worse than the 95.95 level
    assert res.equity.iloc[2] == approx(SIZE * 89.1 * (1 - FEE))


def test_take_profit_fills_at_target_level() -> None:
    _, res = stop_run(
        [ENTRY_BAR, QUIET, (105.0, 112.0, 104.0, 110.0)], stop_loss_pct=5, take_profit_pct=10
    )
    (t,) = res.trades
    assert t.exit_reason == "take_profit"
    assert t.exit_price == approx(TP_LEVEL * (1 - SLIP))  # 111.1 * 0.99 = 109.989
    exit_fee = SIZE * t.exit_price * FEE
    assert t.pnl == approx(SIZE * (TP_LEVEL * (1 - SLIP) - ENTRY_PRICE) - ENTRY_FEE - exit_fee)


def test_take_profit_gap_through_fills_at_open() -> None:
    _, res = stop_run([ENTRY_BAR, QUIET, (115.0, 116.0, 114.0, 115.0)], take_profit_pct=10)
    (t,) = res.trades
    assert t.exit_reason == "take_profit"
    assert t.exit_price == approx(115.0 * (1 - SLIP))


def test_stop_loss_wins_when_both_stops_touch_the_same_bar() -> None:
    _, res = stop_run(
        [ENTRY_BAR, QUIET, (100.0, 112.0, 94.0, 105.0)], stop_loss_pct=5, take_profit_pct=10
    )
    (t,) = res.trades
    assert t.exit_reason == "stop_loss"
    assert t.exit_price == approx(SL_LEVEL * (1 - SLIP))


def test_stops_skip_untradable_bars() -> None:
    rows = [ENTRY_BAR, QUIET, (100.0, 100.0, 90.0, 92.0), (97.0, 98.0, 96.0, 97.0)]
    candles = candles_from_rows(rows)
    candles.loc[candles.index[2], "tradable"] = False  # its low of 90 must not fire the stop
    res = run_signals(
        candles,
        sig(candles.index, 0),
        sig(candles.index),
        costs=COSTS,
        risk=RiskSpec(stop_loss_pct=5),
    )
    (t,) = res.trades
    assert t.exit_reason == "end"  # bar 3 opens at 97 and dips to 96: above 95.95
    assert res.equity.iloc[2] == approx(SIZE * 92.0)  # still marked at the untradable close


def test_stop_after_untradable_bar_fills_at_next_tradable_open() -> None:
    rows = [ENTRY_BAR, QUIET, (100.0, 100.0, 90.0, 92.0), (93.0, 94.0, 92.0, 93.0)]
    candles = candles_from_rows(rows)
    candles.loc[candles.index[2], "tradable"] = False
    res = run_signals(
        candles,
        sig(candles.index, 0),
        sig(candles.index),
        costs=COSTS,
        risk=RiskSpec(stop_loss_pct=5),
    )
    (t,) = res.trades
    assert t.exit_reason == "stop_loss" and t.exit_time == candles.index[3]
    assert t.exit_price == approx(93.0 * (1 - SLIP))  # gap-through at bar 3's open


def test_exit_signal_at_open_beats_intrabar_stop() -> None:
    rows = [ENTRY_BAR, QUIET, (100.0, 101.0, 94.0, 95.0)]
    candles = candles_from_rows(rows)
    res = run_signals(
        candles,
        sig(candles.index, 0),
        sig(candles.index, 1),
        costs=COSTS,
        risk=RiskSpec(stop_loss_pct=5),
    )
    (t,) = res.trades
    assert t.exit_reason == "signal"
    assert t.exit_price == approx(100.0 * (1 - SLIP))  # 99 at the open, not the 95.95 stop


def test_short_stop_loss_sits_above_entry() -> None:
    rows = [ENTRY_BAR, QUIET, (100.0, 105.0, 99.0, 104.0)]
    candles = candles_from_rows(rows)
    res = run_signals(
        candles,
        sig(candles.index, 0),
        sig(candles.index),
        costs=COSTS,
        risk=RiskSpec(stop_loss_pct=5),
        direction="short",
    )
    (t,) = res.trades
    entry = 100.0 * (1 - SLIP)  # 99
    level = entry * 1.05  # 103.95
    size = CASH / (entry * (1 + FEE))
    assert t.exit_reason == "stop_loss"
    assert t.exit_price == approx(level * (1 + SLIP))  # buy back at 104.9895
    fees = size * entry * FEE + size * level * (1 + SLIP) * FEE
    assert t.pnl == approx(size * (entry - level * (1 + SLIP)) - fees)


# --------------------------------------------------------------------------- gross vs net


def test_gross_returns_are_the_zero_cost_run() -> None:
    candles = candles_from_rows(ROWS_LONG)
    entries, exits = sig(candles.index, 0), sig(candles.index, 2)
    net = run_signals(candles, entries, exits, costs=COSTS)
    gross = run_signals(candles, entries, exits, costs=ZERO_COSTS)
    assert np.array_equal(net.gross_returns.to_numpy(), gross.returns.to_numpy())
    assert np.array_equal(gross.gross_returns.to_numpy(), gross.returns.to_numpy())
    # Zero costs: 100 units at 100; equity 10000, 11000, 12000, 12100.
    np.testing.assert_allclose(gross.equity.to_numpy(), [CASH, 11_000, 12_000, 12_100], rtol=REL)
    assert gross.fees_paid == 0.0
    assert net.equity.iloc[-1] < gross.equity.iloc[-1]


# --------------------------------------------------------------------------- run_weights

ROWS_W = [
    (100.0, 100.0, 100.0, 100.0),  # 0: target 1.0 decided
    (100.0, 110.0, 100.0, 110.0),  # 1: buy to 100% at open 100
    (110.0, 120.0, 110.0, 120.0),  # 2: target 0.5 decided
    (120.0, 120.0, 100.0, 100.0),  # 3: sell down to 50% at open 120
]
FEE_ONLY = CostModel(fee_rate=FEE, slippage_rate=0.0)


def weights(index: pd.DatetimeIndex, **at: float) -> pd.Series:
    w = pd.Series(np.nan, index=index)
    for k, v in at.items():
        w.iloc[int(k[1:])] = v
    return w


def test_run_weights_rebalances_with_costs_on_the_traded_delta() -> None:
    candles = candles_from_rows(ROWS_W)
    res = run_weights(candles, weights(candles.index, b0=1.0, b2=0.5), costs=FEE_ONLY)
    # Bar 1: all cash buys incl. fee: units = 10000 / (100 * 1.01) = 99.00990099, fee 99.0099.
    units = CASH / (100.0 * (1 + FEE))
    fee1 = CASH - units * 100.0
    eq2 = units * 120.0  # 11881.188
    # Bar 3: equity at the open 120 = 11881.188; target units = 0.5 * 11881.188 / 120 = 49.50495
    keep = 0.5 * eq2 / 120.0
    sold = units - keep  # 49.50495 units, notional 5940.594
    fee3 = sold * 120.0 * FEE  # 59.40594 - only on the traded delta
    cash3 = sold * 120.0 - fee3  # 5881.188
    eq3 = cash3 + keep * 100.0  # 10831.683
    np.testing.assert_allclose(res.equity.to_numpy(), [CASH, units * 110.0, eq2, eq3], rtol=REL)
    assert res.fees_paid == approx(fee1 + fee3)  # 158.4158
    np.testing.assert_allclose(
        res.position.to_numpy(), [0.0, 1.0, 1.0, keep * 100.0 / eq3], rtol=REL, atol=1e-12
    )
    (t,) = res.trades  # one flat -> flat position, still open
    assert t.exit_reason == "end" and t.entry_time == candles.index[1]


def test_run_weights_buy_and_hold_tracks_price() -> None:
    candles = candles_from_rows(ROWS_W)
    res = run_weights(candles, weights(candles.index, b0=1.0), costs=ZERO_COSTS)
    np.testing.assert_allclose(res.equity.to_numpy(), [CASH, 11_000, 12_000, 10_000], rtol=REL)
    np.testing.assert_allclose(res.returns.to_numpy(), [0.0, 0.1, 12 / 11 - 1, 10 / 12 - 1])


def test_run_weights_skips_rebalances_below_min_rebalance() -> None:
    candles = candles_from_rows(ROWS_W)
    w = weights(candles.index, b0=1.0, b2=0.95)
    skipped = run_weights(candles, w, costs=FEE_ONLY, min_rebalance=0.1)
    hold = run_weights(candles, weights(candles.index, b0=1.0), costs=FEE_ONLY)
    assert np.array_equal(skipped.equity.to_numpy(), hold.equity.to_numpy())
    traded = run_weights(candles, w, costs=FEE_ONLY, min_rebalance=0.01)
    assert traded.fees_paid > hold.fees_paid


def test_run_weights_slippage_moves_fill_against_trader() -> None:
    candles = candles_from_rows(ROWS_W)
    res = run_weights(candles, weights(candles.index, b0=1.0), costs=CostModel(0.0, SLIP))
    units = CASH / (100.0 * (1 + SLIP))  # bought at 101
    assert res.equity.iloc[1] == approx(units * 110.0)
    assert res.trades[0].entry_price == approx(101.0)


def test_run_weights_target_before_untradable_bar_fills_after_it() -> None:
    candles = candles_from_rows(ROWS_W)
    candles.loc[candles.index[1], "tradable"] = False
    res = run_weights(candles, weights(candles.index, b0=1.0), costs=ZERO_COSTS)
    assert res.trades[0].entry_time == candles.index[2]
    # Bought at bar 2's open 110: 10000 / 110 units, marked at 120 then 100.
    np.testing.assert_allclose(
        res.equity.to_numpy(), [CASH, CASH, CASH * 120 / 110, CASH * 100 / 110], rtol=REL
    )


@pytest.mark.parametrize("bad", [1.5, -0.1])
def test_run_weights_rejects_out_of_range_weights(bad: float) -> None:
    candles = candles_from_rows(ROWS_W)
    with pytest.raises(ValueError, match="weights"):
        run_weights(candles, weights(candles.index, b0=bad), costs=ZERO_COSTS)


def test_run_weights_rejects_negative_min_rebalance() -> None:
    candles = candles_from_rows(ROWS_W)
    with pytest.raises(ValueError, match="min_rebalance"):
        run_weights(candles, weights(candles.index, b0=1.0), costs=ZERO_COSTS, min_rebalance=-1)


def test_short_that_exhausts_equity_raises_instead_of_leaving_a_residual() -> None:
    # Short 100.01 units at 99 (cash 19801.98); covering at 300 * 1.01 needs ~30604 > cash.
    rows = [(100.0,) * 4, (100.0,) * 4, (300.0,) * 4]
    candles = candles_from_rows(rows)
    with pytest.raises(ValueError, match="equity exhausted"):
        run_signals(
            candles, sig(candles.index, 0), sig(candles.index, 1), costs=COSTS, direction="short"
        )


def test_run_weights_trade_fees_include_partial_rebalance_sells() -> None:
    candles = candles_from_rows(ROWS_W)
    res = run_weights(candles, weights(candles.index, b0=1.0, b2=0.5), costs=FEE_ONLY)
    (t,) = res.trades
    assert t.fees == approx(res.fees_paid)  # 99.0099 at entry + 59.40594 on the bar-3 sell


def test_short_whose_account_value_goes_negative_mid_trade_raises() -> None:
    # Quant-review repro (C2). Fee 0.1%, slippage 0.05% per side, every bar O=H=L=C.
    #   bar 1: sell short at 100 * (1 - 0.0005) = 99.95; size = 10000 / (99.95 * 1.001)
    #          = 99.95 units; cash = 10000 + 99.95 * 99.95 * (1 - 0.001) = 19980.03
    #   bar 3: close 250 -> value = 19980.03 - 99.95 * 250 = -5007.5 < 0  (equity exhausted)
    #   bar 5: the exit (signal at bar 4) would buy back at 120 and "recover" to ~7986 on
    #          paper, which is why the engine must stop at bar 3 instead.
    prices = [100.0, 100.0, 150.0, 250.0, 180.0, 120.0, 120.0]
    candles = candles_from_rows([(p, p, p, p) for p in prices])
    costs = CostModel(fee_rate=0.001, slippage_rate=0.0005)
    with pytest.raises(ValueError, match="equity exhausted"):
        run_signals(
            candles, sig(candles.index, 0), sig(candles.index, 4), costs=costs, direction="short"
        )


def test_long_at_full_size_never_exhausts_equity_even_in_a_crash() -> None:
    # Long 100% can lose at most what it committed: value = units * price > 0 for price > 0.
    prices = [100.0, 100.0, 10.0, 0.5, 0.01, 0.01]
    candles = candles_from_rows([(p, p, p, p) for p in prices])
    costs = CostModel(fee_rate=0.001, slippage_rate=0.0005)
    res = run_signals(candles, sig(candles.index, 0), sig(candles.index, 4), costs=costs)
    assert (res.equity > 0).all()
    units = CASH / (100.0 * 1.0005 * 1.001)
    assert res.equity.iloc[3] == approx(units * 0.5)
