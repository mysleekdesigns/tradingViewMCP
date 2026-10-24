"""Event-driven cross-check of the vectorbt engine with nautilus_trader (PLAN.md Phase 7 How §8).

The point is to catch vectorbt simplifications (fill timing, intrabar stops) that could make a
result look better than an event-driven simulation would. Both engines receive the *same*
precomputed plan signals (``compile_plan`` on the same candles), so every difference comes from
fill, stop, sizing, or cost mechanics, never from signal logic. Neither engine is tuned to
force agreement: the differences found are listed in :attr:`CrossCheckResult.notes`.

What the cross-check verifies **independently** (nautilus' own matching engine, order
management and cash account decide it, not lab code):

* stop-loss / take-profit triggering: on which bar, in which intrabar order (open -> nearer
  extreme -> farther extreme -> close), and the fill price (the level, or the open on a gap);
* gap-through fills, untradable-bar handling as seen by the matching engine, and that a stop
  already beyond the market triggers at once;
* cash and position accounting (free balance, no overdraft, final equity), fee charging per
  fill, and order handling (reduce-only exits, cancelling the other protective order, an exit
  signal at the open executed before an intrabar stop, no pyramiding, denials/rejections).

What is **shared by construction** (the same lab code or rule feeds both, so the cross-check
cannot catch a bug in it): the signals themselves, the signal timing rule (decide at bar t's
close, trade at the next tradable open; nautilus only enforces that bar t is not visible
before its close), the per-bar slippage series (``slippage_series``, applied to nautilus
MARKET orders by a fill model written here), the sizing formula (computed in the replay
strategy), and the candles' tick rounding. The look-ahead and slippage-model tests cover those.

Two passes, because nautilus fills stop-loss / take-profit orders WITHOUT slippage while
vectorbt slips them, so a configured-cost run differs by design whenever a stop fires:

1. **Configured pass**: ``costs`` as given.
2. **Mechanics pass**: the same fees, ``slippage_rate = slippage_vol_mult = 0`` (skipped when
   the costs have no slippage). Here both engines fill every order at the same reference
   price, so stop triggers/fills, gaps, cash and fees must agree within tolerance.

For the configured pass the log-equity gap is attributed to vectorbt's stop/TP slippage; the
residual must be within tolerance. ``agree`` = configured pass agrees OR (mechanics pass agrees
AND slippage explains the gap); see :class:`CrossCheckResult`.

How the nautilus side is modelled (read from the nautilus_trader 1.231 source):

* **Venue/instrument.** A ``CASH`` account, ``NETTING`` OMS venue ``BINANCE`` and a
  ``CurrencyPair`` built from the plan symbol (``BTCUSDT`` -> BTC/USDT). The price tick is
  fine enough for ~9 significant digits of the lowest price and never coarser than the data's
  own decimals (BTCUSDT at $3,300 -> 1e-5), so rounding slipped fills and stop levels to the
  tick moves them by < 1e-8 relative; a real 0.01 tick would distort percent slippage on cheap
  coins. Data without an exact decimal representation (synthetic floats) is rounded to that tick
  and *both* engines run on the rounded candles. Sizes have 8 decimals (rounded down).
  ``maker_fee = taker_fee = costs.fee_rate``.
* **Data.** Each tradable bar becomes an external ``LAST`` bar with ``ts_event = ts_init`` = the
  bar's CLOSE time (open + interval), so nothing is visible before the bar has closed, plus a
  trade tick at the bar's open price 1 ns after its open time (= 1 ns after the previous bar's
  close). The strategy, which only learns bar t's signal at t's close, trades on that open
  print, i.e. at the next tradable bar's open like vectorbt. The matching engine then walks the
  bar open -> nearer extreme -> farther extreme -> close (``bar_adaptive_high_low_ordering``),
  each price point carrying a quarter of the bar volume. Untradable (zero-volume) bars are not
  sent: no prints, no fills, no stop checks, as in vectorbt.
* **Signals.** On each open print the strategy ORs every signal decided since the previous
  print. Flat with entry and exit due -> no entry; in a position an exit due -> market sell (the
  entry is ignored); no pyramiding. Same rules as ``lab.engine.backtest``.
* **Sizing.** ``position_size_pct`` of the free quote balance (= equity when flat) including the
  entry fee: ``qty = pct x cash / (fill_price x (1 + fee))``, rounded down to 1e-8.
* **Slippage.** nautilus models slippage in ticks (``prob_slippage``), not percent. A small
  ``FillModel`` subclass gives MARKET orders a one-level book at the open print x (1 +/- slip)
  with the same per-bar rate vectorbt uses (``slippage_series``), rounded to the tick on the
  worse side. Stop-loss / take-profit orders are left to nautilus' own trigger logic, which fills
  at the trigger price (or the gapped open) WITHOUT slippage: a known, reported difference
  (vectorbt slips stop fills). A percent book for stops would bypass that trigger logic.
* **Stops.** After the entry fill, a reduce-only ``STOP_MARKET`` sell at ``fill x (1 - sl)``
  and/or a reduce-only ``MARKET_IF_TOUCHED`` sell at ``fill x (1 + tp)`` (taker fee, like
  vectorbt's TP fills), both GTC, cancelled when the position closes (one-cancels-other done by
  the strategy). nautilus decides when they trigger and at what price. A stop already beyond
  the price when placed (entry slippage larger than the stop distance) triggers at once at the
  market (``reject_stop_orders=False``) rather than being rejected, like vectorbt's same-bar
  stop; such cases are counted in the notes.
* **Sanity.** Orders denied/rejected by nautilus, or a run that ends before the last bar (e.g.
  nautilus stops its backtest on a negative cash balance), are reported and make ``agree``
  False.
* **End.** A position open at the last bar is not closed; equity is marked at the last close.

Pure apart from running the in-process simulator: no files, no network, no orders anywhere.
Results are historical and not forward-looking.
"""

from __future__ import annotations

import importlib
import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from lab.engine import run_signals, slippage_series
from lab.engine.splits import history_through, slice_window
from lab.plan import compile_plan
from lab.types import (
    BacktestResult,
    CostModel,
    ExitReason,
    Plan,
    Trade,
    Window,
    interval_seconds,
)

ENGINE_NAME = "nautilus_trader"
NOT_INSTALLED = "nautilus_trader not installed (uv sync --group crosscheck)"
SIZE_PRECISION = 8  # quantity decimals; BTC trades in 1e-5 on Binance, 1e-8 keeps sizing exact
_MAX_EXACT_DECIMALS = 8
_SIGNIFICANT_DIGITS = 9  # tick <= 1e-8 relative to the lowest price
_MAX_DECIMALS = 12
_MAX_LISTED = 8  # per-trade mismatches listed in notes before "... and N more"
_KNOWN_QUOTES = ("FDUSD", "USDT", "USDC", "BUSD", "TUSD", "USD", "EUR", "TRY", "BTC", "ETH", "BNB")
_NS = 1_000_000_000


# --------------------------------------------------------------------------- result


@dataclass(frozen=True)
class TradeComparison:
    """One trade matched by entry bar (``None`` on the side that has no such trade)."""

    entry_time: pd.Timestamp
    entry_vectorbt: float | None
    entry_event: float | None
    exit_time_vectorbt: pd.Timestamp | None
    exit_time_event: pd.Timestamp | None
    exit_vectorbt: float | None
    exit_event: float | None
    reason_vectorbt: ExitReason | None
    reason_event: ExitReason | None


@dataclass(frozen=True)
class CrossCheckResult:
    """vectorbt vs event-driven engine on the same plan signals.

    A **pass agrees** when both engines have the same trades (every trade matched by entry bar,
    the same exit bar and exit reason, the same ones still open at the end), nautilus reported
    no order problems, the largest entry and exit price differences are within
    ``tolerance_pct``, and the final equities differ by at most ``tolerance_pct`` percent in
    log terms: ``100 x |ln((1 + R_event) / (1 + R_vectorbt))|``.

    Two passes run (see the module docstring): the **configured** pass with ``costs`` as given
    (``agree_configured``) and the **mechanics** pass with the same fees but zero slippage
    (``mechanics_agree``). nautilus fills stop-loss / take-profit exits without slippage and
    vectorbt slips them, so with slippage on, the configured pass can differ by design. The
    **attribution** of that gap: ``slippage_explained_pct`` = ``100 x sum_k ln(1 + c_k / E_k)``
    over vectorbt's SL/TP exits, where ``c_k`` is the quote amount vectorbt's stop slippage cost
    on exit ``k`` (``size x ref x s x (1 - fee)`` with ``ref = exit / (1 - s)`` for a long) and
    ``E_k`` vectorbt's equity right after it (flat at that close); every other cash flow scales
    with equity, so this is the whole expected log-equity gap if the mechanics match.
    ``attribution_residual_pct`` = ``100 x ln(E_event / E_vectorbt)`` minus that.
    ``max_stop_attribution_diff_pct`` = per SL/TP exit ``100 x |ln(vb_exit / (nt_exit x
    (1 - s)))|``, i.e. whether vectorbt's fill is nautilus' fill slipped by the bar's rate.
    ``slippage_explains_gap`` = the configured pass has the same trade structure (as above,
    with entry and signal-exit prices within tolerance), ``|residual|`` and every per-exit
    difference are within ``tolerance_pct``.

    ``agree`` = ``agree_configured`` OR (``mechanics_agree`` AND ``slippage_explains_gap``);
    ``notes`` say which. The headline number a strategy is judged on must be reproduced or
    fully explained; otherwise the backtest is not trustworthy to that precision.
    ``trades`` and the price/return fields describe the configured pass.
    """

    available: bool
    reason: str | None
    engine: str
    n_trades_vectorbt: int
    n_trades_event: int
    matched_trades: int
    max_entry_price_diff_pct: float | None
    max_exit_price_diff_pct: float | None
    total_return_vectorbt: float | None
    total_return_event: float | None
    agree: bool
    notes: tuple[str, ...]
    return_diff_pct: float | None = None
    tolerance_pct: float = 0.5
    price_precision: int | None = None
    trades: tuple[TradeComparison, ...] = ()
    vectorbt_result: BacktestResult | None = field(default=None, repr=False, compare=False)
    agree_configured: bool = False
    mechanics_agree: bool | None = None
    mechanics_ran_separately: bool = False  # False when costs have no slippage (same pass)
    mechanics_return_diff_pct: float | None = None
    mechanics_max_exit_price_diff_pct: float | None = None
    mechanics_total_return_vectorbt: float | None = None
    mechanics_total_return_event: float | None = None
    mechanics_trades: tuple[TradeComparison, ...] = ()
    slippage_explained_pct: float | None = None
    attribution_residual_pct: float | None = None
    max_stop_attribution_diff_pct: float | None = None
    slippage_explains_gap: bool | None = None


def _unavailable(reason: str, tolerance_pct: float) -> CrossCheckResult:
    return CrossCheckResult(
        available=False,
        reason=reason,
        engine=ENGINE_NAME,
        n_trades_vectorbt=0,
        n_trades_event=0,
        matched_trades=0,
        max_entry_price_diff_pct=None,
        max_exit_price_diff_pct=None,
        total_return_vectorbt=None,
        total_return_event=None,
        agree=False,
        notes=(reason,),
        tolerance_pct=tolerance_pct,
    )


# --------------------------------------------------------------------------- helpers


def _import_nautilus() -> dict[str, Any]:
    """Import nautilus_trader lazily; raises ``ImportError`` if it is not installed."""
    importlib.import_module("nautilus_trader")  # first, so a missing package fails here
    names: dict[str, tuple[str, ...]] = {
        "nautilus_trader.backtest.config": ("BacktestEngineConfig",),
        "nautilus_trader.backtest.engine": ("BacktestEngine",),
        "nautilus_trader.backtest.models": ("FillModel",),
        "nautilus_trader.config": (
            "LoggingConfig",
            "StrategyConfig",
        ),
        "nautilus_trader.model.book": ("OrderBook",),
        "nautilus_trader.model.data": (
            "Bar",
            "BarSpecification",
            "BarType",
            "BookOrder",
            "TradeTick",
        ),
        "nautilus_trader.model.enums": (
            "AccountType",
            "AggregationSource",
            "AggressorSide",
            "BarAggregation",
            "BookType",
            "OmsType",
            "OrderSide",
            "OrderType",
            "PriceType",
            "TimeInForce",
        ),
        "nautilus_trader.model.identifiers": (
            "InstrumentId",
            "Symbol",
            "TradeId",
            "Venue",
        ),
        "nautilus_trader.model.instruments": ("CurrencyPair",),
        "nautilus_trader.model.objects": (
            "Currency",
            "Money",
            "Price",
            "Quantity",
        ),
        "nautilus_trader.trading.strategy": ("Strategy",),
    }
    out: dict[str, Any] = {}
    for module_name, attrs in names.items():
        module = importlib.import_module(module_name)
        for attr in attrs:
            out[attr] = getattr(module, attr)
    return out


def split_symbol(symbol: str) -> tuple[str, str]:
    """``"BTCUSDT"`` -> ``("BTC", "USDT")`` using common quote suffixes."""
    for quote in _KNOWN_QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    raise ValueError(
        f"cannot split symbol {symbol!r} into base/quote; expected a suffix in "
        f"{', '.join(_KNOWN_QUOTES)}"
    )


def price_precision(prices: np.ndarray) -> tuple[int, bool]:
    """``(decimals, exact)`` for the instrument tick ``10**-decimals``.

    ``decimals`` gives ~9 significant digits of the smallest price, raised to the data's own
    decimals when those are finer (capped at 12). ``exact`` is True when every price is
    representable with at most 8 decimals, i.e. rounding to the tick changes nothing.
    """
    p = np.asarray(prices, dtype=float)
    if p.size == 0 or not np.isfinite(p).all() or (p <= 0).any():
        raise ValueError("prices must be a non-empty array of finite numbers > 0")
    significant = math.ceil(_SIGNIFICANT_DIGITS - math.log10(float(p.min())))
    for d in range(_MAX_EXACT_DECIMALS + 1):
        if np.all(np.abs(np.round(p, d) - p) <= 1e-12 * np.abs(p)):  # float noise only
            return int(min(max(d, significant, 0), _MAX_DECIMALS)), True
    return int(min(max(significant, 0), _MAX_DECIMALS)), False


def _round_prices(candles: pd.DataFrame, decimals: int) -> pd.DataFrame:
    out = candles.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = np.round(out[col].to_numpy(dtype=float), decimals)
    return out


def _bar_spec(interval: str) -> tuple[int, str]:
    seconds = interval_seconds(interval)
    for unit, size in (("DAY", 86_400), ("HOUR", 3_600), ("MINUTE", 60)):
        if seconds % size == 0:
            return seconds // size, unit
    raise ValueError(f"interval {interval!r} cannot be expressed as a nautilus bar spec")


def slipped_ticks(price: float, slip: float, buy: bool, decimals: int) -> int:
    """Fill price in ticks of ``10**-decimals``: ``price x (1 +/- slip)`` rounded to the worse
    side (up for buys, down for sells). Values within float noise of a whole tick snap to it,
    so an unslipped price that is already on the tick never moves."""
    x = price * ((1.0 + slip) if buy else (1.0 - slip)) * 10.0**decimals
    nearest = round(x)
    if abs(x - nearest) <= 1e-12 * abs(x) + 1e-9:
        return int(nearest)
    return math.ceil(x) if buy else math.floor(x)


def _pct_diff(a: float, b: float) -> float:
    return 100.0 * abs(a - b) / abs(b)


# --------------------------------------------------------------------------- nautilus run


@dataclass
class _EventTrade:
    entry_bar: int
    entry_price: float
    qty: float
    exit_bar: int | None = None
    exit_price: float | None = None
    reason: ExitReason = "end"


@dataclass
class _EventRun:
    trades: list[_EventTrade]
    final_equity: float
    multi_fill_orders: int
    denied_or_rejected: list[str]
    stops_in_market: int


def _run_nautilus(
    nt: dict[str, Any],
    plan: Plan,
    bars: pd.DataFrame,
    entries: np.ndarray,
    exits: np.ndarray,
    slip: np.ndarray,
    costs: CostModel,
    init_cash: float,
    decimals: int,
) -> _EventRun:
    base_code, quote_code = split_symbol(plan.symbol)
    currency = nt["Currency"]
    base, quote = currency.from_str(base_code), currency.from_str(quote_code)
    price_cls, qty_cls = nt["Price"], nt["Quantity"]
    order_side, order_type = nt["OrderSide"], nt["OrderType"]
    venue = nt["Venue"]("BINANCE")
    instrument_id = nt["InstrumentId"](nt["Symbol"](plan.symbol), venue)
    tick = 10.0**-decimals
    fee = Decimal(repr(costs.fee_rate))
    instrument = nt["CurrencyPair"](
        instrument_id=instrument_id,
        raw_symbol=nt["Symbol"](plan.symbol),
        base_currency=base,
        quote_currency=quote,
        price_precision=decimals,
        size_precision=SIZE_PRECISION,
        price_increment=price_cls(tick, decimals),
        size_increment=qty_cls(10.0**-SIZE_PRECISION, SIZE_PRECISION),
        lot_size=None,
        max_quantity=None,
        min_quantity=None,
        max_notional=None,
        min_notional=None,
        max_price=None,
        min_price=None,
        margin_init=Decimal(0),
        margin_maint=Decimal(0),
        maker_fee=fee,
        taker_fee=fee,
        ts_event=0,
        ts_init=0,
    )
    step, unit = _bar_spec(plan.interval)
    bar_type = nt["BarType"](
        instrument_id,
        nt["BarSpecification"](step, getattr(nt["BarAggregation"], unit), nt["PriceType"].LAST),
        nt["AggregationSource"].EXTERNAL,
    )

    tradable = bars["tradable"].to_numpy(dtype=bool)
    o = bars["open"].to_numpy(dtype=float)
    h = bars["high"].to_numpy(dtype=float)
    lo = bars["low"].to_numpy(dtype=float)
    c = bars["close"].to_numpy(dtype=float)
    vol = bars["volume"].to_numpy(dtype=float)
    open_ns = bars.index.as_unit("ns").asi8
    interval_ns = interval_seconds(plan.interval) * _NS
    size_tick = 10.0**-SIZE_PRECISION

    data: list[Any] = []
    tick_row: dict[int, int] = {}
    for i in np.flatnonzero(tradable):
        ts_open, ts_close = int(open_ns[i]) + 1, int(open_ns[i]) + interval_ns
        quarter = max(math.floor(vol[i] / 4.0 / size_tick) * size_tick, size_tick)
        data.append(
            nt["TradeTick"](
                instrument_id,
                price_cls(o[i], decimals),
                qty_cls(quarter, SIZE_PRECISION),
                nt["AggressorSide"].NO_AGGRESSOR,
                nt["TradeId"](f"O{i}"),
                ts_open,
                ts_open,
            )
        )
        data.append(
            nt["Bar"](
                bar_type,
                price_cls(o[i], decimals),
                price_cls(h[i], decimals),
                price_cls(lo[i], decimals),
                price_cls(c[i], decimals),
                qty_cls(max(vol[i], size_tick), SIZE_PRECISION),
                ts_close,
                ts_close,
            )
        )
        tick_row[ts_open] = int(i)

    def bar_of(ts_ns: int) -> int:
        return int(np.searchsorted(open_ns, ts_ns - 1, side="right") - 1)

    unlimited = qty_cls(1e12, SIZE_PRECISION)

    fill_model_base: type = nt["FillModel"]
    strategy_base: type = nt["Strategy"]

    class PercentSlippageFill(fill_model_base):
        """MARKET orders fill at the current print x (1 +/- slip); stops use nautilus logic."""

        def __init__(self) -> None:
            super().__init__()
            self.slip = 0.0

        def get_orderbook_for_fill_simulation(
            self, instrument: Any, order: Any, best_bid: Any, best_ask: Any
        ) -> Any:
            if order.order_type != order_type.MARKET:
                return None
            book = nt["OrderBook"](instrument_id=instrument.id, book_type=nt["BookType"].L2_MBP)
            bid = slipped_ticks(best_bid.as_double(), self.slip, False, decimals)
            ask = slipped_ticks(best_ask.as_double(), self.slip, True, decimals)
            book.add(
                nt["BookOrder"](order_side.BUY, price_cls(bid * tick, decimals), unlimited, 1), 0, 0
            )
            book.add(
                nt["BookOrder"](order_side.SELL, price_cls(ask * tick, decimals), unlimited, 2),
                0,
                0,
            )
            return book

    fill_model = PercentSlippageFill()
    risk = plan.risk
    fee_rate = costs.fee_rate

    class SignalReplay(strategy_base):
        """Replays precomputed plan signals; trades only on the next open print."""

        def __init__(self) -> None:
            super().__init__(nt["StrategyConfig"](strategy_id="CROSSCHECK-001"))
            self.next_row = 0  # first signal row not yet consumed
            self.last_row = -1  # last open print processed (detects an early stop)
            self.long_qty = 0.0
            self.trades: list[_EventTrade] = []
            self.fills: dict[Any, list[tuple[float, float]]] = {}
            self.multi_fill_orders = 0
            self.stops_in_market = 0  # protective orders already beyond the price when placed
            self.problems: list[str] = []
            self.protective: list[Any] = []
            self.exit_reason: dict[Any, ExitReason] = {}

        def on_start(self) -> None:
            self.subscribe_trade_ticks(instrument_id)

        def on_trade_tick(self, tick_: Any) -> None:
            row = tick_row.get(int(tick_.ts_event))
            if row is None:
                return
            self.last_row = row
            want_entry = bool(entries[self.next_row : row].any())
            want_exit = bool(exits[self.next_row : row].any())
            self.next_row = row
            if self.long_qty > 0:
                if want_exit:
                    self._cancel_protective()
                    self._market(order_side.SELL, self.long_qty, row, "signal")
            elif want_entry and not want_exit:
                cash = float(self.portfolio.account(venue).balance_free(quote).as_double())
                px_ticks = slipped_ticks(o[row], float(slip[row]), True, decimals)
                px = px_ticks * tick
                units = math.floor(
                    risk.position_size_pct / 100.0 * cash / (px * (1.0 + fee_rate)) / size_tick
                )
                # Keep a micro-cent margin for nautilus' own fixed-point rounding; a negative
                # cash balance would stop its backtest.
                while units > 0 and units * size_tick * px * (1.0 + fee_rate) > cash - 1e-6:
                    units -= 1
                qty = units * size_tick
                if qty > 0:
                    self._market(order_side.BUY, qty, row, "entry")

        def _market(self, side: Any, qty: float, row: int, tag: str) -> None:
            fill_model.slip = float(slip[row])
            order = self.order_factory.market(
                instrument_id,
                side,
                qty_cls(qty, SIZE_PRECISION),
                reduce_only=side == order_side.SELL,
                tags=[tag],
            )
            if tag == "signal":
                self.exit_reason[order.client_order_id] = "signal"
            self.submit_order(order)

        def _cancel_protective(self) -> None:
            for order in self.protective:
                if order.is_open:
                    self.cancel_order(order)
            self.protective = []

        def on_order_filled(self, event: Any) -> None:
            oid = event.client_order_id
            parts = self.fills.setdefault(oid, [])
            parts.append((event.last_px.as_double(), event.last_qty.as_double()))
            order = self.cache.order(oid)
            if order is None or not order.is_closed:
                return
            if len(parts) > 1:
                self.multi_fill_orders += 1
            qty = sum(q for _, q in parts)
            avg = sum(p * q for p, q in parts) / qty
            bar = bar_of(int(event.ts_event))
            if event.order_side == order_side.BUY:
                self.long_qty = qty
                self.trades.append(_EventTrade(entry_bar=bar, entry_price=avg, qty=qty))
                self._place_protective(avg, qty, float(o[bar]))
            else:
                self.long_qty = 0.0
                self._cancel_protective()
                trade = self.trades[-1]
                trade.exit_bar, trade.exit_price = bar, avg
                trade.reason = self.exit_reason.get(oid, "signal")

        def _place_protective(self, fill_px: float, qty: float, market: float) -> None:
            q = qty_cls(qty, SIZE_PRECISION)
            if risk.stop_loss_pct is not None:
                level = round(fill_px * (1.0 - risk.stop_loss_pct / 100.0) / tick) * tick
                if level >= market:  # entry slippage exceeded the stop distance
                    self.stops_in_market += 1
                sl = self.order_factory.stop_market(
                    instrument_id,
                    order_side.SELL,
                    q,
                    price_cls(level, decimals),
                    time_in_force=nt["TimeInForce"].GTC,
                    reduce_only=True,
                    tags=["stop_loss"],
                )
                self.exit_reason[sl.client_order_id] = "stop_loss"
                self.protective.append(sl)
                self.submit_order(sl)
            if risk.take_profit_pct is not None:
                level = round(fill_px * (1.0 + risk.take_profit_pct / 100.0) / tick) * tick
                if level <= market:
                    self.stops_in_market += 1
                tp = self.order_factory.market_if_touched(
                    instrument_id,
                    order_side.SELL,
                    q,
                    price_cls(level, decimals),
                    time_in_force=nt["TimeInForce"].GTC,
                    reduce_only=True,
                    tags=["take_profit"],
                )
                self.exit_reason[tp.client_order_id] = "take_profit"
                self.protective.append(tp)
                self.submit_order(tp)

        def on_order_denied(self, event: Any) -> None:
            self.problems.append(f"order denied: {event.reason}")

        def on_order_rejected(self, event: Any) -> None:
            self.problems.append(f"order rejected: {event.reason}")

    engine = None
    try:
        with warnings.catch_warnings():
            # nautilus 1.231 calls pd.Timestamp.utcnow(), deprecated in pandas 3.
            warnings.filterwarnings("ignore", message=".*utcnow.*")
            engine = nt["BacktestEngine"](
                nt["BacktestEngineConfig"](
                    logging=nt["LoggingConfig"](bypass_logging=True), run_analysis=False
                )
            )
            engine.add_venue(
                venue=venue,
                oms_type=nt["OmsType"].NETTING,
                account_type=nt["AccountType"].CASH,
                starting_balances=[nt["Money"](init_cash, quote)],
                fill_model=fill_model,
                bar_execution=True,
                bar_adaptive_high_low_ordering=True,
                # A stop already beyond the price when placed triggers at once (at market)
                # instead of being rejected, matching vectorbt's immediate same-bar stop.
                reject_stop_orders=False,
            )
            engine.add_instrument(instrument)
            engine.add_data(data)
            strategy = SignalReplay()
            engine.add_strategy(strategy)
            engine.run()
            account = engine.cache.account_for_venue(venue)
            cash = float(account.balance_total(quote).as_double())
            held_money = account.balance_total(base)
            held = 0.0 if held_money is None else float(held_money.as_double())
            last_close = float(c[np.flatnonzero(tradable)[-1]]) if tradable.any() else 0.0
            final_equity = cash + held * last_close
            problems = list(strategy.problems)
            if tradable.any() and strategy.last_row != int(np.flatnonzero(tradable)[-1]):
                stopped = (
                    "never started"
                    if strategy.last_row < 0
                    else (f"after {bars.index[strategy.last_row].isoformat()}")
                )
                problems.append(
                    f"nautilus stopped early ({stopped}), e.g. on an account error; its result "
                    "covers only part of the window"
                )
            return _EventRun(
                trades=strategy.trades,
                final_equity=final_equity,
                multi_fill_orders=strategy.multi_fill_orders,
                stops_in_market=strategy.stops_in_market,
                denied_or_rejected=problems,
            )
    finally:
        if engine is not None:
            engine.dispose()


# --------------------------------------------------------------------------- comparison


def _compare(
    vb_trades: Sequence[Trade], ev_trades: Sequence[_EventTrade], index: pd.DatetimeIndex
) -> tuple[list[TradeComparison], list[str]]:
    vb_by = {t.entry_time: t for t in vb_trades}
    ev_by = {index[t.entry_bar]: t for t in ev_trades}
    rows: list[TradeComparison] = []
    for ts in sorted(set(vb_by) | set(ev_by)):
        v, e = vb_by.get(ts), ev_by.get(ts)
        rows.append(
            TradeComparison(
                entry_time=ts,
                entry_vectorbt=None if v is None else v.entry_price,
                entry_event=None if e is None else e.entry_price,
                exit_time_vectorbt=None if v is None else v.exit_time,
                exit_time_event=None if e is None or e.exit_bar is None else index[e.exit_bar],
                exit_vectorbt=None if v is None else v.exit_price,
                exit_event=None if e is None else e.exit_price,
                reason_vectorbt=None if v is None else v.exit_reason,
                reason_event=None if e is None else e.reason,
            )
        )
    problems: list[str] = []
    for r in rows:
        when = r.entry_time.isoformat()
        if r.entry_vectorbt is None:
            problems.append(f"trade entered {when} only in nautilus")
        elif r.entry_event is None:
            problems.append(f"trade entered {when} only in vectorbt")
        elif r.reason_vectorbt != r.reason_event or r.exit_time_vectorbt != r.exit_time_event:
            problems.append(
                f"trade entered {when}: vectorbt exits {r.reason_vectorbt} at "
                f"{_ts(r.exit_time_vectorbt)}, nautilus exits {r.reason_event} at "
                f"{_ts(r.exit_time_event)}"
            )
    return rows, problems


def _ts(ts: pd.Timestamp | None) -> str:
    return "end (open)" if ts is None else ts.isoformat()


def _listed(header: str, items: list[str]) -> list[str]:
    if not items:
        return []
    shown = items[:_MAX_LISTED]
    more = len(items) - len(shown)
    tail = f" ... and {more} more" if more else ""
    return [f"{header}: " + "; ".join(shown) + tail]


@dataclass(frozen=True)
class _Pass:
    """One cross-check pass (both engines on one cost model)."""

    vb: BacktestResult
    ev: _EventRun
    slip: np.ndarray
    rows: list[TradeComparison]
    mismatches: list[str]
    matched: list[TradeComparison]
    max_entry: float | None
    max_exit: float | None
    open_mismatch: bool
    total_return_vb: float
    total_return_ev: float
    return_diff: float  # 100 x |ln(E_event / E_vectorbt)|, inf if either side is <= 0
    log_gap: float  # signed ln(E_event / E_vectorbt), inf/nan if either side is <= 0
    agree: bool


def _run_pass(
    nt: dict[str, Any],
    plan: Plan,
    history: pd.DataFrame,
    bars: pd.DataFrame,
    entries: pd.Series,
    exits: pd.Series,
    costs: CostModel,
    init_cash: float,
    decimals: int,
    tolerance_pct: float,
) -> _Pass:
    # vectorbt warms its vol-scaled slippage up on the history; nautilus gets the same series.
    vb = run_signals(
        bars,
        entries,
        exits,
        costs=costs,
        risk=plan.risk,
        init_cash=init_cash,
        cost_history=history,
    )
    slip = slippage_series(history, costs)[len(history) - len(bars) :]
    ev = _run_nautilus(
        nt,
        plan,
        bars,
        entries.to_numpy(dtype=bool),
        exits.to_numpy(dtype=bool),
        slip,
        costs,
        init_cash,
        decimals,
    )
    rows, mismatches = _compare(vb.trades, ev.trades, bars.index)
    matched = [r for r in rows if r.entry_vectorbt is not None and r.entry_event is not None]
    entry_diffs = [
        _pct_diff(r.entry_event, r.entry_vectorbt)
        for r in matched
        if r.entry_event is not None and r.entry_vectorbt is not None
    ]
    exit_diffs = [
        _pct_diff(r.exit_event, r.exit_vectorbt)
        for r in matched
        if r.exit_event is not None and r.exit_vectorbt is not None
    ]
    open_mismatch = any((r.exit_event is None) != (r.exit_vectorbt is None) for r in matched)
    max_entry = max(entry_diffs) if entry_diffs else None
    max_exit = max(exit_diffs) if exit_diffs else None

    tr_vb = float(vb.equity.iloc[-1] / init_cash - 1.0)
    tr_ev = float(ev.final_equity / init_cash - 1.0)
    if 1.0 + tr_vb > 0 and 1.0 + tr_ev > 0:
        log_gap = math.log((1.0 + tr_ev) / (1.0 + tr_vb))
        return_diff = 100.0 * abs(log_gap)
    else:
        log_gap, return_diff = math.nan, math.inf
    agree = (
        _same_structure(vb, ev, matched, mismatches, open_mismatch)
        and (max_entry is None or max_entry <= tolerance_pct)
        and (max_exit is None or max_exit <= tolerance_pct)
        and return_diff <= tolerance_pct
    )
    return _Pass(
        vb=vb,
        ev=ev,
        slip=slip,
        rows=rows,
        mismatches=mismatches,
        matched=matched,
        max_entry=max_entry,
        max_exit=max_exit,
        open_mismatch=open_mismatch,
        total_return_vb=tr_vb,
        total_return_ev=tr_ev,
        return_diff=return_diff,
        log_gap=log_gap,
        agree=bool(agree),
    )


def _same_structure(
    vb: BacktestResult,
    ev: _EventRun,
    matched: list[TradeComparison],
    mismatches: list[str],
    open_mismatch: bool,
) -> bool:
    """Same trades (entry bar, exit bar, exit reason, still-open set) and no order problems."""
    return (
        len(vb.trades) == len(ev.trades) == len(matched)
        and not mismatches
        and not open_mismatch
        and not ev.denied_or_rejected
    )


@dataclass(frozen=True)
class _Attribution:
    explained_pct: float
    residual_pct: float
    max_trade_diff_pct: float | None
    explains: bool


_STOP_REASONS = ("stop_loss", "take_profit")


def _attribute(
    p: _Pass, bars: pd.DataFrame, costs: CostModel, tolerance_pct: float
) -> _Attribution:
    """How much of the configured pass' log-equity gap is vectorbt's stop/TP slippage (see
    :class:`CrossCheckResult`)."""
    vb_by = {t.entry_time: t for t in p.vb.trades}
    fee = float(costs.fee_rate)
    explained = 0.0
    trade_diffs: list[float] = []
    signal_exit_ok = True
    for r in p.matched:
        t = vb_by[r.entry_time]
        if t.exit_time is None or t.exit_price is None or r.exit_event is None:
            continue
        if t.exit_reason not in _STOP_REASONS:
            signal_exit_ok &= _pct_diff(r.exit_event, t.exit_price) <= tolerance_pct
            continue
        j = int(bars.index.get_loc(t.exit_time))
        s = float(p.slip[j])
        long = t.direction == "long"
        slipped_event = r.exit_event * ((1.0 - s) if long else (1.0 + s))
        trade_diffs.append(100.0 * abs(math.log(t.exit_price / slipped_event)))
        ref = t.exit_price / ((1.0 - s) if long else (1.0 + s))
        cost = t.size * ref * s * ((1.0 - fee) if long else (1.0 + fee))
        explained += math.log1p(cost / float(p.vb.equity.iloc[j]))
    explained_pct = 100.0 * explained
    residual_pct = 100.0 * p.log_gap - explained_pct if math.isfinite(p.log_gap) else math.inf
    max_trade = max(trade_diffs) if trade_diffs else None
    explains = (
        _same_structure(p.vb, p.ev, p.matched, p.mismatches, p.open_mismatch)
        and (p.max_entry is None or p.max_entry <= tolerance_pct)
        and signal_exit_ok
        and abs(residual_pct) <= tolerance_pct
        and (max_trade is None or max_trade <= tolerance_pct)
    )
    return _Attribution(
        explained_pct=explained_pct,
        residual_pct=residual_pct,
        max_trade_diff_pct=max_trade,
        explains=bool(explains),
    )


def crosscheck(
    plan: Plan,
    candles: pd.DataFrame,
    *,
    costs: CostModel,
    init_cash: float = 10_000.0,
    tolerance_pct: float = 0.5,
    window: Window | None = None,
) -> CrossCheckResult:
    """Re-run ``plan`` in nautilus_trader and compare with the vectorbt engine.

    Signals are compiled once on ``candles`` (through ``window.end`` if a window is given, so
    earlier rows serve as indicator and slippage warm-up) and both engines trade the window
    slice (all rows without a window). Two passes run: the configured costs, and the same fees
    with zero slippage (the mechanics pass; skipped, and taken equal to the configured pass,
    when ``costs`` has no slippage). Returns ``available=False`` with a reason if
    nautilus_trader is not installed. See the module docstring for the modelling choices and
    :class:`CrossCheckResult` for the definition of ``agree``.
    """
    if not isinstance(tolerance_pct, int | float) or not math.isfinite(tolerance_pct):
        raise ValueError(f"tolerance_pct must be a finite number > 0, got {tolerance_pct!r}")
    if tolerance_pct <= 0:
        raise ValueError(f"tolerance_pct must be > 0, got {tolerance_pct!r}")
    if not math.isfinite(init_cash) or init_cash <= 0:
        raise ValueError(f"init_cash must be a finite number > 0, got {init_cash!r}")
    try:
        nt = _import_nautilus()
    except ImportError:
        return _unavailable(NOT_INSTALLED, float(tolerance_pct))

    tol = float(tolerance_pct)
    history = candles if window is None else history_through(candles, window)
    if len(history) == 0:
        raise ValueError("candles contain no rows before the window end")
    decimals, exact = price_precision(history[["open", "high", "low", "close"]].to_numpy())
    history = _round_prices(history, decimals)
    signals = compile_plan(plan, history)
    bars = history if window is None else slice_window(history, window)
    if len(bars) == 0:
        raise ValueError("window contains no bars")
    entries = signals.entries.loc[bars.index]
    exits = signals.exits.loc[bars.index]

    args = (nt, plan, history, bars, entries, exits)
    configured = _run_pass(*args, costs, float(init_cash), decimals, tol)
    separate = bool(np.any(configured.slip > 0))
    mechanics_costs = replace(costs, slippage_rate=0.0, slippage_vol_mult=0.0)
    mechanics = (
        _run_pass(*args, mechanics_costs, float(init_cash), decimals, tol)
        if separate
        else configured
    )
    attribution = _attribute(configured, bars, costs, tol)
    agree = configured.agree or (mechanics.agree and attribution.explains)

    vb, ev = configured.vb, configured.ev
    notes = [_agreement_note(configured, mechanics, attribution, separate, tol)]
    notes += _notes(plan, bars, costs, configured.slip, decimals, exact, ev, vb)
    notes += _listed("mismatched trades", configured.mismatches)
    notes += _listed("nautilus order problems", ev.denied_or_rejected)
    if separate:
        notes += _listed("mechanics pass mismatched trades", mechanics.mismatches)
        notes += _listed("mechanics pass nautilus order problems", mechanics.ev.denied_or_rejected)
    reason_pairs = [
        f"{r.reason_vectorbt}->{r.reason_event}"
        for r in configured.matched
        if r.reason_vectorbt != r.reason_event
    ]
    if reason_pairs:
        counts = pd.Series(reason_pairs).value_counts()
        notes.append(
            "exit reason differences (vectorbt->nautilus): "
            + ", ".join(f"{k} x{v}" for k, v in counts.items())
        )
    return CrossCheckResult(
        available=True,
        reason=None,
        engine=ENGINE_NAME,
        n_trades_vectorbt=len(vb.trades),
        n_trades_event=len(ev.trades),
        matched_trades=len(configured.matched),
        max_entry_price_diff_pct=configured.max_entry,
        max_exit_price_diff_pct=configured.max_exit,
        total_return_vectorbt=configured.total_return_vb,
        total_return_event=configured.total_return_ev,
        agree=bool(agree),
        notes=tuple(notes),
        return_diff_pct=configured.return_diff,
        tolerance_pct=tol,
        price_precision=decimals,
        trades=tuple(configured.rows),
        vectorbt_result=vb,
        agree_configured=configured.agree,
        mechanics_agree=mechanics.agree,
        mechanics_ran_separately=separate,
        mechanics_return_diff_pct=mechanics.return_diff,
        mechanics_max_exit_price_diff_pct=mechanics.max_exit,
        mechanics_total_return_vectorbt=mechanics.total_return_vb,
        mechanics_total_return_event=mechanics.total_return_ev,
        mechanics_trades=tuple(mechanics.rows),
        slippage_explained_pct=attribution.explained_pct,
        attribution_residual_pct=attribution.residual_pct,
        max_stop_attribution_diff_pct=attribution.max_trade_diff_pct,
        slippage_explains_gap=attribution.explains,
    )


def _agreement_note(
    configured: _Pass, mechanics: _Pass, attribution: _Attribution, separate: bool, tol: float
) -> str:
    gap = f"{configured.return_diff:.4f}%"
    if configured.agree:
        return f"agreement: the configured-cost pass agrees (log-equity gap {gap} <= {tol}%)"
    mech = (
        f"mechanics pass (fees kept, slippage 0) {'agrees' if mechanics.agree else 'DISAGREES'}"
        f" (log-equity gap {mechanics.return_diff:.4f}%)"
        if separate
        else "costs have no slippage, so the mechanics pass is the configured pass"
    )
    attr = (
        f"stop/TP slippage explains {attribution.explained_pct:+.4f}% of the "
        f"{100.0 * configured.log_gap:+.4f}% log-equity gap (residual "
        f"{attribution.residual_pct:+.4f}%, tolerance {tol}%)"
        if math.isfinite(configured.log_gap)
        else "the log-equity gap is undefined (an account went to <= 0)"
    )
    if mechanics.agree and attribution.explains:
        return (
            f"agreement via attribution: the configured-cost pass differs ({gap}), but the {mech}"
            f" and {attr}"
        )
    why = "slippage does not explain the gap" if mechanics.agree else "the mechanics differ"
    return f"DISAGREEMENT ({why}): configured-cost pass gap {gap}; {mech}; {attr}"


def _notes(
    plan: Plan,
    bars: pd.DataFrame,
    costs: CostModel,
    slip: np.ndarray,
    decimals: int,
    exact: bool,
    ev: _EventRun,
    vb: BacktestResult,
) -> list[str]:
    risk = plan.risk
    notes = [
        "fills: both engines fill a signal decided at bar t's close at the next tradable bar's "
        "open; nautilus gets each open as a trade print 1 ns after the bar opens and the "
        "strategy trades on it",
        f"fees: {costs.fee_rate:.4%} per side in both engines (nautilus maker = taker fee)",
    ]
    if np.any(slip > 0):
        notes.append(
            f"slippage: vectorbt's per-bar rate (mean {float(np.mean(slip)):.4%}, max "
            f"{float(np.max(slip)):.4%} at {bars.index[int(np.argmax(slip))].isoformat()}) is "
            "applied to nautilus MARKET orders by a percent fill model, rounded to the tick on "
            "the worse side"
        )
        if risk.stop_loss_pct is not None or risk.take_profit_pct is not None:
            notes.append(
                "known difference: nautilus fills stop-loss/take-profit at the trigger price (or "
                "the gapped open) WITHOUT slippage; vectorbt slips stop fills, so nautilus stop "
                "exits are better by about the slippage rate"
            )
    if risk.stop_loss_pct is not None or risk.take_profit_pct is not None:
        notes.append(
            "stops: STOP_MARKET (stop-loss) / MARKET_IF_TOUCHED (take-profit) sells placed after "
            "the entry fill at the fill price -/+ pct, rounded to the nearest tick; nautilus "
            "checks them from the entry bar on its bar walk open -> nearer extreme -> farther "
            "extreme -> close, filling at the trigger, or at the open on a gap"
        )
    if risk.stop_loss_pct is not None and risk.take_profit_pct is not None:
        notes.append(
            "known difference: when one bar touches both stop-loss and take-profit, vectorbt "
            "assumes the stop-loss first (pessimistic); nautilus takes whichever extreme is "
            "nearer the open"
        )
    if ev.stops_in_market:
        notes.append(
            f"{ev.stops_in_market} protective order(s) were already beyond the price when placed "
            "(entry slippage larger than the stop distance); nautilus triggers them at once at "
            "the market price without slippage (reject_stop_orders=False), vectorbt stops out on "
            "the same bar and slips the fill"
        )
    if ev.multi_fill_orders:
        notes.append(
            f"liquidity: {ev.multi_fill_orders} nautilus order(s) filled in several parts "
            "(a triggered stop takes a quarter of the bar volume per price point and the rest one "
            "tick worse); prices compared are size-weighted averages"
        )
    if not exact:
        notes.append(
            f"precision: prices rounded to {decimals} decimals for nautilus; both engines ran on "
            "the rounded candles"
        )
    untradable = int((~bars["tradable"].to_numpy(dtype=bool)).sum())
    if untradable:
        notes.append(
            f"{untradable} untradable bar(s) were not sent to nautilus (no prints, fills or stop "
            "checks), matching vectorbt"
        )
    if vb.trades and vb.trades[-1].exit_reason == "end":
        notes.append(
            "a trade still open at the window end is left open in both engines and marked at the "
            "last close (no exit fee)"
        )
    notes.append(
        f"sizing: nautilus quantities are rounded down to 1e-{SIZE_PRECISION} units; vectorbt "
        "sizes are unrounded"
    )
    return notes
