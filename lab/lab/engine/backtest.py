"""vectorbt backtest engine with next-bar fills, costs, and intrabar stops (PLAN.md Phase 7).

Semantics (every point is pinned by a hand-calculated test in ``tests/test_engine_*.py``):

1. **Next-bar fills.** A signal True at bar t is decided at t's close. It fills at the open of
   the first *tradable* bar after row ``t + delay`` (``delay`` = adversarial signal delay in
   rows; ``delay=0`` -> the next tradable bar). Missing bars are absent rows, so a signal on
   the bar before a time gap fills on the next existing bar; ``tradable=False`` bars never
   receive fills. Signals whose fill would fall after the last bar are dropped. Several signal
   bars mapping to the same fill bar are OR-ed.
2. **Prices and costs.** Fill price = reference x (1 + slip) for buys and x (1 - slip) for
   sells (always the worse side), where the reference is the open (or the stop level for stop
   fills) and ``slip = slippage_series(...)[bar]``. Fee = ``fee_rate`` x filled value, per side.
3. **Signals.** Flat with entry and exit due on the same fill bar -> no entry. In a position,
   an exit due -> exit at that open (an entry due on the same bar is ignored). Entries while
   already in a position are ignored (no pyramiding).
4. **Sizing.** ``position_size_pct`` of current equity (= cash, since entries happen only when
   flat) is committed per entry *including the entry fee*: notional + fee = pct x equity, so
   100 never makes cash negative.
5. **Stops** (``RiskSpec.stop_loss_pct`` / ``take_profit_pct``, relative to the entry *fill*
   price). Checked intrabar from the fill bar onwards with low/high (long; mirrored for short).
   If a bar opens beyond the level the stop fills at the open (gap-through), otherwise at the
   level; slippage and fee apply to stop fills too. If stop-loss and take-profit are both
   touched in one bar, the stop-loss is assumed first (pessimistic). Stops are never evaluated
   on ``tradable=False`` bars; they are checked at the next tradable bar, at its open first.
   An exit signal due at a bar's open is executed before any intrabar stop on that bar.
6. **Short** (``direction="short"``, used only by the adversarial sign flip) mirrors all of
   the above: entries sell short, exits buy back, stop-loss sits above the entry fill.
7. **Open trade at the end** is reported with ``exit_time=None``, ``exit_price=None``,
   ``exit_reason="end"`` and pnl marked to the last close net of the entry costs. It is not
   force-closed, so no exit fee is charged.
8. **Outputs** on ``candles.index``: equity marked at each close, ``returns`` = per-bar simple
   returns of that equity (bar 0 = 0), ``gross_returns`` = the same signals re-run with
   ``ZERO_COSTS`` (sizes and stop levels then differ slightly; that is intended),
   ``position`` = signed exposure (units x close / equity) at each close.
9. **Equity exhausted.** If the account value is <= 0 on any vectorbt row (open or close
   mark), the run raises ``ValueError("... equity exhausted ...")``. Long-only with sizing
   <= 100% cannot get there; a short that the market runs over can (it would otherwise keep
   "trading" with negative equity and could even recover on paper). The adversarial runner
   maps this message to a "blown" variant.
10. **Cost history.** ``cost_history=`` (optional) is a longer candle frame ending at the same
   bar whose tail is exactly ``candles``. The vol-scaled slippage series is computed on it and
   then sliced to ``candles.index``, so the first ``median_window`` bars of a window slice are
   charged the real vol-scaled slippage rather than the warm-up base rate. Slippage at bar j
   still depends only on rows before j, so the result stays causal.

How vectorbt is used, and where its defaults had to be adapted:

* vectorbt checks stops only from the bar *after* the entry and gives a stop priority over a
  user exit signal on the same row. To get (5), every bar j is split into two vectorbt rows:
  row ``2j`` is the open (O=H=L=C=open[j]; all signal orders go here) and row ``2j+1`` is the
  rest of the bar (open[j], high[j], low[j], close[j]). An entry filled on row 2j therefore has
  its stop checked on row 2j+1 of the same bar, a gap beyond the stop is caught on the next
  bar's open row, and an exit signal on row 2j can only coincide with a stop when the open
  itself gapped beyond it (same price, labelled ``"signal"``). Equity, exposure, and returns
  are read from the odd rows only (marked at the close).
* ``stop_entry_price=FillPrice`` (stop levels relative to the slipped fill, not the close),
  ``stop_exit_price=StopMarket`` (fill at the level or the gapped open, slippage applied),
  ``upon_stop_exit=Close``. vectorbt's ``get_stop_price_nb`` checks SL before TP (point 5).
* ``upon_long_conflict``/``upon_short_conflict = Opposite`` gives point 3 (vectorbt's default
  ``Ignore`` would also drop an exit that coincides with an ignored entry while in a position).
* Stops on ``tradable=False`` bars are disabled with ``adjust_sl_func_nb``/``adjust_tp_func_nb``
  returning NaN on those rows (vectorbt has no per-row stop mask).
* ``run_weights`` uses ``Portfolio.from_orders`` with ``size_type="targetpercent"`` and
  ``val_price=open`` so the target weight is measured against equity valued at the fill open.

Pure: no I/O, no prints, no module state. Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd
import vectorbt as vbt
from numba import njit
from vectorbt.portfolio import enums as vbt_enums

from lab.engine.costs import slippage_series, validate_costs
from lab.types import (
    ZERO_COSTS,
    BacktestResult,
    CostModel,
    Direction,
    ExitReason,
    RiskSpec,
    Trade,
    validate_candles,
)

_ROWS_PER_BAR = 2  # vectorbt rows per candle: (open, rest of bar)
_NO_RISK = RiskSpec()  # immutable default: 100% sizing, no stops


# --------------------------------------------------------------------------- numba callbacks


@njit
def _adjust_sl_nb(c: object, tradable_rows: np.ndarray, sl: float) -> tuple[float, bool]:
    """Stop-loss fraction for vectorbt row ``c.i``: disabled when flat or on untradable bars."""
    if c.position_now == 0 or not tradable_rows[c.i]:
        return np.nan, False
    return sl, False


@njit
def _adjust_tp_nb(c: object, tradable_rows: np.ndarray, tp: float) -> float:
    """Take-profit fraction for vectorbt row ``c.i``: disabled when flat or on untradable bars."""
    if c.position_now == 0 or not tradable_rows[c.i]:
        return np.nan
    return tp


# --------------------------------------------------------------------------- validation


def _check_candles(candles: pd.DataFrame) -> None:
    if not isinstance(candles, pd.DataFrame):
        raise ValueError(f"candles must be a pandas DataFrame, got {type(candles).__name__}")
    validate_candles(candles)
    if len(candles) == 0:
        raise ValueError("candles must contain at least one bar")
    if not pd.api.types.is_bool_dtype(candles["tradable"]) or candles["tradable"].hasnans:
        raise ValueError("candles['tradable'] must be a bool column without NA")
    prices = candles[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError("candles open/high/low/close must be finite and > 0")


def _check_bool_signal(name: str, signal: pd.Series, index: pd.DatetimeIndex) -> np.ndarray:
    if not isinstance(signal, pd.Series):
        raise ValueError(f"{name} must be a pandas Series, got {type(signal).__name__}")
    if not signal.index.equals(index):
        raise ValueError(f"{name} index must equal candles.index")
    if not pd.api.types.is_bool_dtype(signal) or signal.hasnans:
        raise ValueError(f"{name} must be a bool Series without NA, got dtype {signal.dtype}")
    return signal.to_numpy(dtype=bool)


def _check_init_cash(init_cash: float) -> None:
    if not np.isfinite(init_cash) or init_cash <= 0:
        raise ValueError(f"init_cash must be a finite number > 0, got {init_cash!r}")


def _check_risk(risk: RiskSpec) -> None:
    if not isinstance(risk, RiskSpec):
        raise ValueError(f"risk must be a RiskSpec, got {type(risk).__name__}")
    pct = risk.position_size_pct
    if not np.isfinite(pct) or not 0 < pct <= 100:
        raise ValueError(f"risk.position_size_pct must be in (0, 100], got {pct!r}")
    for name in ("stop_loss_pct", "take_profit_pct"):
        value = getattr(risk, name)
        if value is not None and (not np.isfinite(value) or value <= 0):
            raise ValueError(f"risk.{name} must be None or a finite number > 0, got {value!r}")


# --------------------------------------------------------------------------- fill timing


def _next_tradable(tradable: np.ndarray) -> np.ndarray:
    """``nxt[i]`` = first row ``k >= i`` with ``tradable[k]``; ``n`` if none. Length ``n + 1``."""
    n = len(tradable)
    rows = np.where(tradable, np.arange(n), n)
    nxt = np.empty(n + 1, dtype=np.int64)
    nxt[n] = n
    nxt[:n] = np.minimum.accumulate(rows[::-1])[::-1]
    return nxt


def _fill_rows(signal_rows: np.ndarray, nxt: np.ndarray, delay: int) -> np.ndarray:
    """Fill row for each decision row (``-1`` when the fill would fall after the last bar)."""
    n = len(nxt) - 1
    start = signal_rows + delay + 1
    fills = np.full(len(signal_rows), -1, dtype=np.int64)
    ok = start < n
    fills[ok] = nxt[start[ok]]
    fills[fills >= n] = -1
    return fills


def _fill_mask(signal: np.ndarray, nxt: np.ndarray, delay: int) -> np.ndarray:
    mask = np.zeros(len(signal), dtype=bool)
    fills = _fill_rows(np.flatnonzero(signal), nxt, delay)
    mask[fills[fills >= 0]] = True
    return mask


def _interleave(at_open: np.ndarray, rest: np.ndarray) -> np.ndarray:
    return np.column_stack([at_open, rest]).ravel()


# --------------------------------------------------------------------------- costs


def _check_cost_history(cost_history: pd.DataFrame, candles: pd.DataFrame) -> None:
    if not isinstance(cost_history, pd.DataFrame):
        raise ValueError(
            f"cost_history must be a pandas DataFrame or None, got {type(cost_history).__name__}"
        )
    validate_candles(cost_history)
    n = len(candles)
    if len(cost_history) < n or cost_history.index[-1] != candles.index[-1]:
        raise ValueError(
            "cost_history must end exactly at candles.index[-1] and contain candles.index as "
            "its tail"
        )
    tail = cost_history.iloc[len(cost_history) - n :]
    if not tail.index.equals(candles.index):
        raise ValueError("cost_history's last len(candles) rows must have index candles.index")
    cols = ["open", "high", "low", "close"]
    if not np.array_equal(tail[cols].to_numpy(dtype=float), candles[cols].to_numpy(dtype=float)):
        raise ValueError("cost_history's tail prices must equal the candles' prices")


def _window_slippage(
    candles: pd.DataFrame, costs: CostModel, cost_history: pd.DataFrame | None
) -> np.ndarray:
    """Per-bar slippage on ``candles.index``, warmed up on ``cost_history`` when given."""
    if cost_history is None:
        return slippage_series(candles, costs)
    return slippage_series(cost_history, costs)[len(cost_history) - len(candles) :]


def _check_not_exhausted(value: np.ndarray) -> None:
    """Raise once the account value is <= 0 on any row (point 9 of the module docstring)."""
    bad = np.flatnonzero(value <= 0)
    if bad.size:
        row = int(bad[0])
        raise ValueError(
            f"account value {float(value[row]):.6g} <= 0 at bar {row // _ROWS_PER_BAR} "
            "(equity exhausted); a short the market ran over cannot keep trading"
        )


# --------------------------------------------------------------------------- simulation


def _equity_outputs(
    candles: pd.DataFrame, value: np.ndarray, assets: np.ndarray
) -> tuple[pd.Series, pd.Series, pd.Series]:
    close = candles["close"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        position = np.where(value != 0, assets * close / value, 0.0)
        returns = np.zeros(len(value))
        returns[1:] = value[1:] / value[:-1] - 1.0
    idx = candles.index
    return (
        pd.Series(value, index=idx, name="equity"),
        pd.Series(returns, index=idx, name="returns"),
        pd.Series(position, index=idx, name="position"),
    )


def _weight_positions(records: np.ndarray, candles: pd.DataFrame) -> tuple[Trade, ...]:
    """vectorbt positions (flat -> flat, possibly several rebalance orders) as long trades.

    Prices are vectorbt's size-weighted average fill prices; an open position is marked to the
    last close net of its entry fees.
    """
    idx = candles.index
    last_bar = len(candles) - 1
    trades = []
    for rec in records:
        entry_bar = int(rec["entry_idx"])
        size = float(rec["size"])
        entry_price = float(rec["entry_price"])
        pnl = float(rec["pnl"])
        # Partial rebalance sells inside a still-open position are already in exit_fees.
        fees = float(rec["entry_fees"]) + float(rec["exit_fees"])
        if int(rec["status"]) == vbt_enums.TradeStatus.Open:
            exit_time, exit_price, reason, exit_bar = None, None, "end", last_bar
        else:
            exit_bar = int(rec["exit_idx"])
            exit_time, exit_price, reason = idx[exit_bar], float(rec["exit_price"]), "signal"
        notional = size * entry_price
        trades.append(
            Trade(
                direction="long",
                entry_time=idx[entry_bar],
                entry_price=entry_price,
                exit_time=exit_time,
                exit_price=exit_price,
                size=size,
                pnl=pnl,
                return_pct=pnl / notional if notional > 0 else float("nan"),
                fees=fees,
                exit_reason=reason,
                bars_held=exit_bar - entry_bar,
            )
        )
    return tuple(trades)


def _round_trips(
    orders: np.ndarray,
    candles: pd.DataFrame,
    direction: Direction,
    reason_of: Callable[[int, float], ExitReason],
) -> tuple[Trade, ...]:
    """Pair vectorbt order records (entry, exit, entry, ...) into trades with exact fill prices.

    Without pyramiding every position is one entry order and one full exit order. The pnl is
    ``size * (exit - entry)`` (sign-flipped for shorts) minus both fees; an unpaired last entry
    is the open trade, marked to the last close net of its entry fee only.
    """
    idx = candles.index
    last_bar = len(candles) - 1
    last_close = float(candles["close"].iloc[-1])
    sign = 1.0 if direction == "long" else -1.0
    trades = []
    for k in range(0, len(orders), 2):
        entry = orders[k]
        size = float(entry["size"])
        entry_price = float(entry["price"])
        entry_bar = int(entry["idx"]) // _ROWS_PER_BAR
        fees = float(entry["fees"])
        if k + 1 < len(orders):
            exit_ = orders[k + 1]
            if not np.isclose(float(exit_["size"]), size, rtol=1e-9, atol=0.0):
                raise ValueError(
                    "position could not be closed in full (equity exhausted); "
                    f"entry size {size}, exit size {float(exit_['size'])}"
                )
            exit_row = int(exit_["idx"])
            exit_bar = exit_row // _ROWS_PER_BAR
            exit_price: float | None = float(exit_["price"])
            exit_time: pd.Timestamp | None = idx[exit_bar]
            fees += float(exit_["fees"])
            pnl = sign * size * (float(exit_["price"]) - entry_price) - fees
            reason = reason_of(exit_row, entry_price)
        else:
            exit_bar, exit_price, exit_time = last_bar, None, None
            pnl = sign * size * (last_close - entry_price) - fees
            reason = "end"
        notional = size * entry_price
        trades.append(
            Trade(
                direction=direction,
                entry_time=idx[entry_bar],
                entry_price=entry_price,
                exit_time=exit_time,
                exit_price=exit_price,
                size=size,
                pnl=pnl,
                return_pct=pnl / notional if notional > 0 else float("nan"),
                fees=fees,
                exit_reason=reason,
                bars_held=exit_bar - entry_bar,
            )
        )
    return tuple(trades)


def _simulate_signals(
    candles: pd.DataFrame,
    entry_fill: np.ndarray,
    exit_fill: np.ndarray,
    costs: CostModel,
    risk: RiskSpec,
    direction: Direction,
    init_cash: float,
    slip: np.ndarray,
) -> tuple[pd.Series, pd.Series, pd.Series, tuple[Trade, ...], float]:
    o = candles["open"].to_numpy(dtype=float)
    h = candles["high"].to_numpy(dtype=float)
    lo = candles["low"].to_numpy(dtype=float)
    c = candles["close"].to_numpy(dtype=float)
    tradable = candles["tradable"].to_numpy(dtype=bool)
    no_signal = np.zeros(len(candles), dtype=bool)

    sl = np.nan if risk.stop_loss_pct is None else risk.stop_loss_pct / 100.0
    tp = np.nan if risk.take_profit_pct is None else risk.take_profit_pct / 100.0
    tradable_rows = _interleave(tradable, tradable)
    pf = vbt.Portfolio.from_signals(
        close=_interleave(o, c),
        entries=_interleave(entry_fill, no_signal),
        exits=_interleave(exit_fill, no_signal),
        direction="longonly" if direction == "long" else "shortonly",
        price=_interleave(o, o),
        open=_interleave(o, o),
        high=_interleave(o, h),
        low=_interleave(o, lo),
        size=risk.position_size_pct / 100.0,
        size_type="percent",
        fees=costs.fee_rate,
        slippage=_interleave(slip, slip),
        init_cash=float(init_cash),
        accumulate=False,
        upon_long_conflict=vbt_enums.ConflictMode.Opposite,
        upon_short_conflict=vbt_enums.ConflictMode.Opposite,
        sl_stop=sl,
        tp_stop=tp,
        stop_entry_price=vbt_enums.StopEntryPrice.FillPrice,
        stop_exit_price=vbt_enums.StopExitPrice.StopMarket,
        upon_stop_exit=vbt_enums.StopExitMode.Close,
        adjust_sl_func_nb=_adjust_sl_nb,
        adjust_sl_args=(tradable_rows, sl),
        adjust_tp_func_nb=_adjust_tp_nb,
        adjust_tp_args=(tradable_rows, tp),
        use_stops=True,
    )
    raw_value = pf.value().to_numpy(dtype=float)
    _check_not_exhausted(raw_value)
    value = raw_value[1::_ROWS_PER_BAR]
    assets = pf.assets().to_numpy(dtype=float)[1::_ROWS_PER_BAR]
    equity, returns, position = _equity_outputs(candles, value, assets)

    def reason_of(exit_row: int, entry_price: float) -> ExitReason:
        bar = exit_row // _ROWS_PER_BAR
        at_open = exit_row % _ROWS_PER_BAR == 0
        if at_open and exit_fill[bar]:
            return "signal"
        # A stop fired: replicate vectorbt's get_stop_price_nb, which tests the SL first
        # against the same fill price it stored as the stop's entry price.
        if not np.isnan(sl):
            if direction == "long":
                touched = (o[bar] if at_open else lo[bar]) <= entry_price * (1 - sl)
            else:
                touched = (o[bar] if at_open else h[bar]) >= entry_price * (1 + sl)
            if touched:
                return "stop_loss"
        return "take_profit"

    trades = _round_trips(pf.orders.records_arr, candles, direction, reason_of)
    fees_paid = float(pf.orders.records_arr["fees"].sum())
    return equity, returns, position, trades, fees_paid


def run_signals(
    candles: pd.DataFrame,
    entries: pd.Series,
    exits: pd.Series,
    *,
    costs: CostModel,
    risk: RiskSpec = _NO_RISK,
    direction: Direction = "long",
    init_cash: float = 10_000.0,
    delay: int = 0,
    cost_history: pd.DataFrame | None = None,
) -> BacktestResult:
    """Backtest bool entry/exit signals decided at each bar's close (see module docstring).

    ``delay`` adds that many rows between the decision and the fill (adversarial signal-delay
    check); ``delay=0`` fills at the next tradable bar's open. ``cost_history`` (point 10)
    warms the vol-scaled slippage up on earlier bars; ``None`` computes it on ``candles``.
    Raises ``ValueError`` mentioning "equity exhausted" when the account value hits <= 0.
    """
    _check_candles(candles)
    entry_sig = _check_bool_signal("entries", entries, candles.index)
    exit_sig = _check_bool_signal("exits", exits, candles.index)
    if not isinstance(costs, CostModel):
        raise ValueError(f"costs must be a CostModel, got {type(costs).__name__}")
    validate_costs(costs)
    _check_risk(risk)
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    _check_init_cash(init_cash)
    if isinstance(delay, bool) or not isinstance(delay, int | np.integer) or delay < 0:
        raise ValueError(f"delay must be an int >= 0, got {delay!r}")
    if cost_history is not None:
        _check_cost_history(cost_history, candles)

    nxt = _next_tradable(candles["tradable"].to_numpy(dtype=bool))
    entry_fill = _fill_mask(entry_sig, nxt, int(delay))
    exit_fill = _fill_mask(exit_sig, nxt, int(delay))

    slip = _window_slippage(candles, costs, cost_history)
    equity, returns, position, trades, fees_paid = _simulate_signals(
        candles, entry_fill, exit_fill, costs, risk, direction, init_cash, slip
    )
    if costs == ZERO_COSTS:
        gross = returns.copy()
    else:
        gross = _simulate_signals(
            candles,
            entry_fill,
            exit_fill,
            ZERO_COSTS,
            risk,
            direction,
            init_cash,
            np.zeros(len(candles)),
        )[1]
    return BacktestResult(
        equity=equity,
        returns=returns,
        gross_returns=gross.rename("gross_returns"),
        position=position,
        trades=trades,
        fees_paid=fees_paid,
        init_cash=float(init_cash),
    )


# --------------------------------------------------------------------------- target weights


def _target_orders(weights: np.ndarray, nxt: np.ndarray, min_rebalance: float) -> np.ndarray:
    """Target weight per fill row (NaN = no order).

    The decision at row t goes to the next tradable row after t; when several decisions land
    on the same fill row the latest one wins. A target is skipped when it differs from the
    last *executed* target (0 at the start) by less than ``min_rebalance``.
    """
    n = len(weights)
    targets = np.full(n, np.nan)
    rows = np.flatnonzero(~np.isnan(weights))
    fills = _fill_rows(rows, nxt, 0)
    for t, f in zip(rows, fills, strict=True):
        if f >= 0:
            targets[f] = weights[t]  # rows ascend, so a later decision overwrites
    last = 0.0
    for f in np.flatnonzero(~np.isnan(targets)):
        if abs(targets[f] - last) < min_rebalance:
            targets[f] = np.nan
        else:
            last = float(targets[f])
    return targets


def _simulate_weights(
    candles: pd.DataFrame,
    targets: np.ndarray,
    costs: CostModel,
    init_cash: float,
    slip: np.ndarray,
) -> tuple[pd.Series, pd.Series, pd.Series, tuple[Trade, ...], float]:
    o = candles["open"].to_numpy(dtype=float)
    pf = vbt.Portfolio.from_orders(
        close=candles["close"].to_numpy(dtype=float),
        size=targets,
        size_type="targetpercent",
        direction="longonly",
        price=o,
        val_price=o,
        fees=costs.fee_rate,
        slippage=slip,
        init_cash=float(init_cash),
    )
    value = pf.value().to_numpy(dtype=float)
    _check_not_exhausted(value)
    assets = pf.assets().to_numpy(dtype=float)
    equity, returns, position = _equity_outputs(candles, value, assets)
    trades = _weight_positions(pf.positions.records_arr, candles)
    fees_paid = float(pf.orders.records_arr["fees"].sum())
    return equity, returns, position, trades, fees_paid


def run_weights(
    candles: pd.DataFrame,
    weights: pd.Series,
    *,
    costs: CostModel,
    init_cash: float = 10_000.0,
    min_rebalance: float = 0.0,
    cost_history: pd.DataFrame | None = None,
) -> BacktestResult:
    """Backtest target long weights (0..1 of equity) decided at each bar's close.

    The target decided at bar t is rebalanced to at the next tradable bar's open, measured
    against equity valued at that open; NaN means "hold" (no order). Fees and slippage are
    charged on the traded delta only. Used for the buy-and-hold and volatility-targeted
    buy-and-hold benchmarks. A target within ``min_rebalance`` (absolute weight) of the last
    executed target is skipped; with ``min_rebalance=0`` every non-NaN target trades the drift
    back to the target. Trades are vectorbt positions (flat -> flat), exit reason ``"signal"``.
    ``cost_history`` warms the vol-scaled slippage up exactly as in :func:`run_signals`.
    """
    _check_candles(candles)
    if not isinstance(weights, pd.Series):
        raise ValueError(f"weights must be a pandas Series, got {type(weights).__name__}")
    if not weights.index.equals(candles.index):
        raise ValueError("weights index must equal candles.index")
    if not pd.api.types.is_numeric_dtype(weights) or pd.api.types.is_bool_dtype(weights):
        raise ValueError(f"weights must be a float Series, got dtype {weights.dtype}")
    w = weights.to_numpy(dtype=float, na_value=np.nan)
    finite = w[~np.isnan(w)]
    if not np.isfinite(finite).all() or (finite < 0).any() or (finite > 1).any():
        raise ValueError("weights must be NaN or within [0, 1]")
    if not isinstance(costs, CostModel):
        raise ValueError(f"costs must be a CostModel, got {type(costs).__name__}")
    validate_costs(costs)
    _check_init_cash(init_cash)
    if not np.isfinite(min_rebalance) or min_rebalance < 0:
        raise ValueError(f"min_rebalance must be a finite number >= 0, got {min_rebalance!r}")
    if cost_history is not None:
        _check_cost_history(cost_history, candles)

    nxt = _next_tradable(candles["tradable"].to_numpy(dtype=bool))
    targets = _target_orders(w, nxt, float(min_rebalance))
    equity, returns, position, trades, fees_paid = _simulate_weights(
        candles, targets, costs, init_cash, _window_slippage(candles, costs, cost_history)
    )
    if costs == ZERO_COSTS:
        gross = returns.copy()
    else:
        gross = _simulate_weights(candles, targets, ZERO_COSTS, init_cash, np.zeros(len(candles)))[
            1
        ]
    return BacktestResult(
        equity=equity,
        returns=returns,
        gross_returns=gross.rename("gross_returns"),
        position=position,
        trades=trades,
        fees_paid=fees_paid,
        init_cash=float(init_cash),
    )
