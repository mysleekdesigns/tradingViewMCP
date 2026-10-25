"""Event-driven cross-check (nautilus_trader) against the vectorbt engine.

Hand-made bars use 2-decimal prices so both engines see exactly the same numbers; stop levels
are chosen to land on a tick. Expected fills are worked out by hand in the comments.

Tolerances: prices on the tick compare exactly (abs 1e-12); final equities to 1e-6 relative
(nautilus rounds quantities down to 1e-8 units). The attribution residual on exact-tick data is
~1e-9 % (size rounding only), far inside any tolerance used here.
"""

from __future__ import annotations

import importlib
import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lab.crosscheck import (
    NOT_INSTALLED,
    crosscheck,
    price_precision,
    slipped_ticks,
    split_symbol,
)
from lab.crosscheck import nautilus as nautilus_mod
from lab.engine import run_signals
from lab.engine.splits import history_through, slice_window
from lab.ledger import Ledger, costs_to_json
from lab.plan import compile_plan, parse_plan
from lab.types import ZERO_COSTS, BacktestResult, CostModel, Plan, Window
from tests.synthetic import candles_from_rows, make_candles


def ema_plan(risk: dict | None = None, fast: int = 10, slow: int = 30) -> Plan:
    return parse_plan(
        {
            "name": "ema_cross",
            "symbol": "BTCUSDT",
            "interval": "4h",
            "features": {"fast": {"ema": {"period": fast}}, "slow": {"ema": {"period": slow}}},
            "long_entry": "crosses_above(fast, slow)",
            "long_exit": "crosses_below(fast, slow)",
            "risk": risk or {"position_size_pct": 100},
        }
    )


def level_plan(risk: dict) -> Plan:
    """Enter when the bar's close/open ratio crosses above 1.01 (on ROWS: bars 2 and 6, the
    same bars where close crosses above 101); never exit by signal (close never doubles its open
    here), so stops do the exits. Scale-free: plans may not compare prices with fixed numbers."""
    return parse_plan(
        {
            "name": "level_break",
            "symbol": "BTCUSDT",
            "interval": "4h",
            "features": {"m": {"sma": {"period": 1}}},
            "long_entry": "crosses_above(m / open, 1.01)",
            "long_exit": "m > 2 * open",
            "risk": risk,
        }
    )


# (open, high, low, close). Signals at bar 2 and bar 6 closes (close/open crosses above 1.01:
# 101.2/100 = 1.012 and 101.5/98.5 = 1.030; every other bar is below 1.01).
ROWS = [
    (100.0, 100.5, 99.5, 100.0),  # 0
    (100.0, 100.5, 99.5, 100.0),  # 1
    (100.0, 101.5, 99.8, 101.2),  # 2  entry signal
    (100.0, 100.8, 99.5, 100.5),  # 3  entry fills at the open 100.00; SL 3% -> 97.00
    (100.5, 101.0, 96.5, 98.0),  # 4  low 96.5 pierces 97.00 intrabar -> stop at 97.00
    (98.0, 99.0, 97.5, 98.5),  # 5
    (98.5, 101.6, 98.0, 101.5),  # 6  entry signal
    (101.8, 102.5, 101.5, 102.0),  # 7  entry fills at 101.80; SL 3% -> 98.746 (tick 98.75)
    (95.0, 96.0, 94.0, 95.5),  # 8  opens below the stop -> gap fill at the open 95.00
    (95.5, 96.0, 95.0, 95.8),  # 9
]


def stop_candles() -> pd.DataFrame:
    # Deep volume so nautilus' per-price-point liquidity never splits a fill.
    return candles_from_rows(ROWS, volume=1e9)


def test_ema_cross_agrees_with_entries_at_next_bar_open() -> None:
    candles = make_candles(220, seed=4, vol=0.012)
    res = crosscheck(ema_plan(), candles, costs=ZERO_COSTS)
    assert res.available and res.reason is None
    assert res.n_trades_vectorbt == res.n_trades_event == res.matched_trades >= 2
    assert res.agree
    # Prices are rounded to the instrument tick for both engines (synthetic floats).
    assert res.price_precision is not None
    rounded = np.round(candles["open"], res.price_precision)
    idx = candles.index
    for t in res.trades:
        next_open = rounded.iloc[idx.get_loc(t.entry_time)]
        assert t.entry_vectorbt == pytest.approx(next_open, rel=1e-12)
        assert t.entry_event == pytest.approx(next_open, rel=1e-12)
    assert res.max_entry_price_diff_pct is not None and res.max_entry_price_diff_pct < 1e-9
    assert res.return_diff_pct is not None and res.return_diff_pct < 1e-4
    assert any("precision" in n for n in res.notes)


def test_entry_is_never_on_the_signal_bar() -> None:
    res = crosscheck(
        level_plan({"position_size_pct": 100, "stop_loss_pct": 3}), stop_candles(), costs=ZERO_COSTS
    )
    idx = stop_candles().index
    assert [t.entry_time for t in res.trades] == [idx[3], idx[7]]  # signals at bars 2 and 6


def test_stop_loss_fills_match_intrabar_and_gap() -> None:
    res = crosscheck(
        level_plan({"position_size_pct": 100, "stop_loss_pct": 3}), stop_candles(), costs=ZERO_COSTS
    )
    idx = stop_candles().index
    assert res.agree, res.notes
    first, second = res.trades
    # Trade 1: entry 100.00 at bar 3, stop 97.00 touched by bar 4's low -> fill at the level.
    assert first.entry_vectorbt == first.entry_event == 100.0
    assert first.exit_vectorbt == pytest.approx(97.0, abs=1e-12)
    assert first.exit_event == pytest.approx(97.0, abs=1e-12)
    assert first.exit_time_vectorbt == first.exit_time_event == idx[4]
    assert first.reason_vectorbt == first.reason_event == "stop_loss"
    # Trade 2: entry 101.80, stop 98.746; bar 8 opens at 95.00 below it -> both fill at the open.
    assert second.entry_vectorbt == second.entry_event == 101.8
    assert second.exit_vectorbt == pytest.approx(95.0, abs=1e-12)
    assert second.exit_event == pytest.approx(95.0, abs=1e-12)
    assert second.exit_time_vectorbt == second.exit_time_event == idx[8]
    assert second.reason_vectorbt == second.reason_event == "stop_loss"
    # Equity: 10000 * 0.97 * (95 / 101.8) = 9052.0628...
    assert res.total_return_vectorbt == pytest.approx(0.97 * 95.0 / 101.8 - 1.0, abs=1e-9)
    assert res.total_return_event == pytest.approx(0.97 * 95.0 / 101.8 - 1.0, abs=1e-6)


def test_take_profit_fills_at_the_level() -> None:
    # Entry 100.00 at bar 3, TP 2% -> 102.00; first touched by bar 7's high 102.5 (bar 6's
    # entry signal is ignored while long). Exit at 102.00 on bar 7 in both engines.
    res = crosscheck(
        level_plan({"position_size_pct": 100, "take_profit_pct": 2}),
        stop_candles(),
        costs=ZERO_COSTS,
    )
    idx = stop_candles().index
    assert res.agree, res.notes
    (trade,) = res.trades
    assert trade.exit_vectorbt == pytest.approx(102.0, abs=1e-12)
    assert trade.exit_event == pytest.approx(102.0, abs=1e-12)
    assert trade.exit_time_vectorbt == trade.exit_time_event == idx[7]
    assert trade.reason_vectorbt == trade.reason_event == "take_profit"


def test_stop_slippage_difference_is_reported_not_hidden() -> None:
    # vectorbt slips the stop fill by 0.05%; nautilus fills at the trigger. Entry slippage and
    # fees are matched, so the only exit difference is the stop slippage.
    costs = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.0)
    res = crosscheck(
        level_plan({"position_size_pct": 100, "stop_loss_pct": 3}), stop_candles(), costs=costs
    )
    assert res.n_trades_vectorbt == res.n_trades_event == res.matched_trades == 2
    assert res.max_entry_price_diff_pct is not None and res.max_entry_price_diff_pct < 0.006
    assert res.max_exit_price_diff_pct == pytest.approx(0.05, abs=0.006)
    assert res.total_return_event is not None and res.total_return_vectorbt is not None
    assert res.total_return_event > res.total_return_vectorbt  # nautilus stops are kinder
    assert any("WITHOUT slippage" in n for n in res.notes)


def test_stop_slippage_gap_fails_the_configured_pass_but_is_explained() -> None:
    # Two stop exits each slipped 0.05% by vectorbt only: the configured pass is ~0.1% apart
    # (> 0.01% tolerance), the no-slippage mechanics pass matches exactly, and the gap is
    # exactly the stop slippage, so the engines agree via attribution.
    costs = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.0)
    res = crosscheck(
        level_plan({"position_size_pct": 100, "stop_loss_pct": 3}),
        stop_candles(),
        costs=costs,
        tolerance_pct=0.01,
    )
    assert not res.agree_configured
    assert res.return_diff_pct is not None and res.return_diff_pct > 0.09
    assert res.mechanics_ran_separately and res.mechanics_agree
    assert res.mechanics_return_diff_pct is not None and res.mechanics_return_diff_pct < 1e-6
    # Hand check: 100% sizing, so each stop's slippage costs ln(1 / (1 - 0.0005)) of equity.
    assert res.slippage_explained_pct == pytest.approx(-200 * math.log(1 - 0.0005), rel=1e-9)
    assert res.attribution_residual_pct is not None and abs(res.attribution_residual_pct) < 1e-6
    assert res.max_stop_attribution_diff_pct is not None
    assert res.max_stop_attribution_diff_pct < 1e-9
    assert res.slippage_explains_gap and res.agree
    assert res.notes[0].startswith("agreement via attribution")


def test_mechanics_pass_keeps_fees_and_drops_slippage() -> None:
    costs = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.0)
    plan = level_plan({"position_size_pct": 100, "stop_loss_pct": 3})
    res = crosscheck(plan, stop_candles(), costs=costs)
    sig = compile_plan(plan, stop_candles())
    fee_only = run_signals(
        stop_candles(), sig.entries, sig.exits, costs=CostModel(0.001, 0.0, 0.0), risk=plan.risk
    )
    # 10000 x (97/100 x 0.999 / 1.001) x (95/101.8 x 0.999 / 1.001): fees on both sides.
    assert res.mechanics_total_return_vectorbt == pytest.approx(
        float(fee_only.equity.iloc[-1] / 1e4 - 1), rel=1e-12
    )
    assert res.mechanics_total_return_event == pytest.approx(
        res.mechanics_total_return_vectorbt, rel=1e-6
    )
    m1, m2 = res.mechanics_trades
    assert (m1.exit_vectorbt, m1.exit_event) == (pytest.approx(97.0), pytest.approx(97.0))
    assert (m2.exit_vectorbt, m2.exit_event) == (pytest.approx(95.0), pytest.approx(95.0))


def _bump_stop_exits(factor: float, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a vectorbt stop bug: every stop-loss exit is reported ``factor`` too high."""
    real = nautilus_mod.run_signals

    def buggy(*args, **kwargs) -> BacktestResult:
        res = real(*args, **kwargs)
        trades = tuple(
            replace(t, exit_price=t.exit_price * factor)
            if t.exit_reason == "stop_loss" and t.exit_price is not None
            else t
            for t in res.trades
        )
        return replace(res, trades=trades)

    monkeypatch.setattr(nautilus_mod, "run_signals", buggy)


def test_injected_stop_bug_is_caught_by_the_mechanics_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    costs = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.0)
    plan = level_plan({"position_size_pct": 100, "stop_loss_pct": 3})
    assert crosscheck(plan, stop_candles(), costs=costs).agree  # control: no bug
    _bump_stop_exits(1.01, monkeypatch)
    res = crosscheck(plan, stop_candles(), costs=costs)
    assert res.mechanics_agree is False
    # |nautilus - vectorbt| / vectorbt = 1 - 1/1.01
    assert res.mechanics_max_exit_price_diff_pct == pytest.approx(100 * (1 - 1 / 1.01), rel=1e-9)
    assert res.max_stop_attribution_diff_pct is not None
    assert res.max_stop_attribution_diff_pct > 0.9
    assert not res.slippage_explains_gap and not res.agree
    assert res.notes[0].startswith("DISAGREEMENT (the mechanics differ)")


def test_unexplained_gap_disagrees_even_when_mechanics_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A vectorbt bug that only shows with slippage on: 1% of equity vanishes at the end.
    real = nautilus_mod.run_signals

    def leaky(*args, **kwargs) -> BacktestResult:
        res = real(*args, **kwargs)
        if kwargs["costs"].slippage_rate == 0:
            return res
        equity = res.equity.copy()
        equity.iloc[-1] *= 0.99
        return replace(res, equity=equity)

    monkeypatch.setattr(nautilus_mod, "run_signals", leaky)
    costs = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.0)
    res = crosscheck(
        level_plan({"position_size_pct": 100, "stop_loss_pct": 3}), stop_candles(), costs=costs
    )
    assert res.mechanics_agree
    assert res.attribution_residual_pct == pytest.approx(-100 * math.log(0.99), rel=1e-6)
    assert not res.slippage_explains_gap and not res.agree
    assert res.notes[0].startswith("DISAGREEMENT (slippage does not explain the gap)")


def test_exit_reason_mismatch_alone_breaks_agreement(monkeypatch: pytest.MonkeyPatch) -> None:
    # Relabel vectorbt's stop exits as signal exits: same bars, prices and equity, but the
    # trades no longer match, so the engines must not "agree".
    real = nautilus_mod.run_signals

    def relabel(*args, **kwargs) -> BacktestResult:
        res = real(*args, **kwargs)
        return replace(res, trades=tuple(replace(t, exit_reason="signal") for t in res.trades))

    monkeypatch.setattr(nautilus_mod, "run_signals", relabel)
    res = crosscheck(
        level_plan({"position_size_pct": 100, "stop_loss_pct": 3}), stop_candles(), costs=ZERO_COSTS
    )
    assert res.n_trades_vectorbt == res.n_trades_event == res.matched_trades == 2
    assert res.max_exit_price_diff_pct is not None and res.max_exit_price_diff_pct < 1e-9
    assert res.return_diff_pct is not None and res.return_diff_pct < 1e-6
    assert not res.agree_configured and not res.agree
    assert any("mismatched trades" in n for n in res.notes)


def test_no_slippage_costs_reuse_the_configured_pass() -> None:
    res = crosscheck(
        level_plan({"position_size_pct": 100, "stop_loss_pct": 3}),
        stop_candles(),
        costs=CostModel(0.001, 0.0, 0.0),
    )
    assert not res.mechanics_ran_separately
    assert res.mechanics_agree == res.agree_configured == res.agree is True
    assert res.slippage_explained_pct == 0.0


def test_vectorbt_side_warms_vol_slippage_up_on_the_history() -> None:
    candles = make_candles(400, seed=8, vol=0.012)
    window = Window(candles.index[150], candles.index[-1] + pd.Timedelta(hours=4), "custom")
    costs = CostModel(0.001, 0.0005, 0.5, 0.01)
    plan = ema_plan(risk={"position_size_pct": 100, "stop_loss_pct": 2})
    res = crosscheck(plan, candles, costs=costs, window=window)
    assert res.price_precision is not None
    history = nautilus_mod._round_prices(history_through(candles, window), res.price_precision)
    bars = slice_window(history, window)
    sig = compile_plan(plan, history)
    warmed = run_signals(
        bars,
        sig.entries.loc[bars.index],
        sig.exits.loc[bars.index],
        costs=costs,
        risk=plan.risk,
        cost_history=history,
    )
    assert res.vectorbt_result is not None
    pd.testing.assert_series_equal(res.vectorbt_result.equity, warmed.equity)
    assert res.agree, res.notes


def test_window_uses_earlier_history_for_warmup_only() -> None:
    candles = make_candles(400, seed=8, vol=0.012)
    window = Window(candles.index[150], candles.index[-1] + pd.Timedelta(hours=4), "custom")
    res = crosscheck(ema_plan(), candles, costs=ZERO_COSTS, window=window)
    assert res.available and res.agree
    assert res.vectorbt_result is not None and len(res.vectorbt_result.equity) == 250
    assert all(t.entry_time >= window.start for t in res.trades)


def test_not_installed_returns_a_clear_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "nautilus_trader", None)  # import raises ImportError
    res = crosscheck(ema_plan(), make_candles(120, seed=1), costs=ZERO_COSTS)
    assert not res.available and not res.agree
    assert (
        res.reason == NOT_INSTALLED == "nautilus_trader not installed (uv sync --group crosscheck)"
    )
    assert res.n_trades_event == 0 and res.total_return_event is None


def test_split_symbol_and_bad_symbol() -> None:
    assert split_symbol("BTCUSDT") == ("BTC", "USDT")
    assert split_symbol("ETHBTC") == ("ETH", "BTC")
    with pytest.raises(ValueError, match="XYZ"):
        split_symbol("XYZ")


def test_price_precision_finds_the_exact_tick() -> None:
    # 9 significant digits of the lowest price: 28999.5 -> 5 decimals; exact data.
    assert price_precision(np.array([29000.01, 28999.5, 30000.0])) == (5, True)
    assert price_precision(np.array([1.0, 2.0])) == (9, True)
    # The data's own decimals win when finer than 9 significant digits.
    assert price_precision(np.array([50000.123456, 50001.0])) == (6, True)
    decimals, exact = price_precision(np.array([100.0 / 3.0, 101.0]))
    assert not exact and decimals == 8  # 9 significant digits of 33.33...


def test_slipped_price_rounds_to_the_worse_side() -> None:
    assert slipped_ticks(100.55, 0.0, True, 2) == 10055  # no slippage, no float drift
    assert slipped_ticks(100.5, 0.0005, True, 2) == 10056  # 100.55025 -> up
    assert slipped_ticks(100.5, 0.0005, False, 2) == 10044  # 100.44975 -> down


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_bad_tolerance_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="tolerance_pct"):
        crosscheck(ema_plan(), make_candles(60, seed=1), costs=ZERO_COSTS, tolerance_pct=bad)


def test_exit_signal_at_the_open_beats_an_intrabar_stop() -> None:
    plan = parse_plan(
        {
            "name": "level_break_exit",
            "symbol": "BTCUSDT",
            "interval": "4h",
            "features": {"m": {"sma": {"period": 1}}},
            "long_entry": "crosses_above(m / open, 1.01)",
            "long_exit": "crosses_below(m / open, 0.99)",
            "risk": {"position_size_pct": 100, "stop_loss_pct": 5},
        }
    )
    rows = [
        (100.0, 100.5, 99.5, 100.0),  # 0
        (100.0, 100.5, 99.5, 100.0),  # 1
        (100.0, 101.5, 99.8, 101.2),  # 2  entry signal
        (100.0, 100.8, 99.5, 100.5),  # 3  entry at 100.00; SL 5% -> 95.00
        (100.5, 101.0, 96.5, 98.0),  # 4  exit signal (close/open 0.975 < 0.99); low > 95
        (98.0, 98.5, 94.0, 94.5),  # 5  exit at the open 98.00, before the low 94 hits the stop
        (94.5, 95.0, 94.0, 94.8),  # 6
    ]
    candles = candles_from_rows(rows, volume=1e9)
    res = crosscheck(plan, candles, costs=ZERO_COSTS)
    assert res.agree, res.notes
    (trade,) = res.trades
    assert trade.exit_vectorbt == pytest.approx(98.0, abs=1e-12)
    assert trade.exit_event == pytest.approx(98.0, abs=1e-12)  # size-weighted average of fills
    assert trade.exit_time_vectorbt == trade.exit_time_event == candles.index[5]
    assert trade.reason_vectorbt == trade.reason_event == "signal"


def test_stop_already_in_the_market_is_reported() -> None:
    # 4% entry slippage > 3% stop: fill 104.00 at bar 3's open 100, stop 100.88 is above the
    # price. vectorbt stops out at bar 3's open slipped (100 x 0.96 = 96); nautilus triggers at
    # once at the market (100.00, no stop slippage). Same bar, same reason, reported.
    costs = CostModel(fee_rate=0.0, slippage_rate=0.04, slippage_vol_mult=0.0)
    res = crosscheck(
        level_plan({"position_size_pct": 100, "stop_loss_pct": 3}), stop_candles(), costs=costs
    )
    first = res.trades[0]
    assert first.entry_vectorbt == pytest.approx(104.0) and first.entry_event == pytest.approx(
        104.0
    )
    assert first.exit_time_vectorbt == first.exit_time_event == stop_candles().index[3]
    assert first.reason_vectorbt == first.reason_event == "stop_loss"
    assert first.exit_vectorbt == pytest.approx(96.0) and first.exit_event == pytest.approx(100.0)
    assert any("already beyond the price when placed" in n for n in res.notes)
    # The 4% exit difference fails the configured pass; it is exactly vectorbt's slippage on
    # the stop fill (100 x 0.96 = 96), and the no-slippage mechanics pass matches.
    assert not res.agree_configured
    assert res.mechanics_agree
    assert res.max_stop_attribution_diff_pct is not None
    assert res.max_stop_attribution_diff_pct < 1e-9
    assert res.slippage_explains_gap and res.agree


def test_full_size_entries_never_overdraw_the_cash_account() -> None:
    # Regression: 100% sizing with fees once left nautilus' cash balance at -9e-7 USDT after a
    # one-tick float rounding, which silently ends its backtest. Many compounding trades here.
    candles = make_candles(3000, seed=21, vol=0.01, drift=0.0003)
    res = crosscheck(ema_plan(fast=3, slow=8), candles, costs=CostModel(0.001, 0.0, 0.0))
    assert not any("stopped early" in n for n in res.notes)
    assert res.n_trades_vectorbt == res.n_trades_event > 100
    assert res.agree, res.notes


def test_unslipped_price_on_the_tick_never_moves() -> None:
    # Price(20230.35, 6).as_double() can come back as 20230.350000000002.
    assert slipped_ticks(20230.350000000002, 0.0, True, 6) == 20_230_350_000
    assert slipped_ticks(20230.349999999998, 0.0, False, 6) == 20_230_350_000


def test_cli_summary_shows_both_passes_and_records_costs(tmp_path: Path) -> None:
    cli = importlib.import_module("lab.crosscheck.__main__")
    costs = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.0)
    plan = level_plan({"position_size_pct": 100, "stop_loss_pct": 3})
    res = crosscheck(plan, stop_candles(), costs=costs, tolerance_pct=0.01)
    text = cli.format_result(res)
    assert "configured costs: agree False" in text
    assert "mechanics (fees kept, slippage 0): agree True" in text
    assert "explains gap: True" in text
    idx = stop_candles().index
    window = Window(idx[0], idx[-1] + pd.Timedelta(hours=4), "design")
    with Ledger(tmp_path / "l.db") as ledger:
        row = ledger.get(cli._record(ledger, "cc", plan, window, res, costs))
    assert row.source == "manual" and row.costs_json == costs_to_json(costs)
