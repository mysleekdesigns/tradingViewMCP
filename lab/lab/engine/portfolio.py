"""Long-only multi-coin portfolio backtest (PLAN.md Phase 10, cross-sectional momentum).

A plain numpy time loop (one iteration per row of the union index, vector ops across symbols),
so a universe of ~500 coins over ~3000 daily bars runs in well under a second. vectorbt is not
used here: its group/cash-sharing model has no notion of per-symbol listing segments, forced
delisting exits at the last close, or "pending until this coin's next tradable bar".

Semantics (every point is pinned by a hand-calculated test in ``tests/test_engine_portfolio.py``):

1. **Decisions.** ``weights`` is indexed by the sorted union of every symbol's candle index,
   with one column per symbol (sorted). A row that is entirely NaN means "hold" (no decision).
   Any row with at least one non-NaN value is a *full* target vector decided at that row's
   close; its NaN cells mean 0 (sell). Values must be NaN or finite in [0, 1] and a decision
   row must sum to <= 1 + 1e-9. Whatever is not allocated stays in cash (fewer qualifying
   coins -> more cash).
2. **Fills.** A decision at union row t applies from row t+1. Per symbol, the pending target
   executes at the OPEN of the first row > t where that symbol has a bar and ``tradable`` is
   True; until then it stays pending. A later decision replaces every still-pending target.
   A decision on the last union row is dropped (nothing can fill it).
3. **Execution at an open.** ``equity_open = cash + sum(units * mark)`` with mark = this
   row's open where the symbol has a bar, else its last close. For each executing symbol,
   ``target_value = w * equity_open`` and ``delta = target_value - units * open``. A symbol is
   skipped when ``|w - units * open / equity_open| < min_rebalance``, except that a target of
   0 with units > 0 always sells everything. Its pending target is consumed either way.
   All sells go first: ``fill = open * (1 - slip)``, ``fee = fee_rate * units_sold * fill``,
   ``cash += units_sold * fill - fee``. Then buys: budgets ``b_i = delta_i`` (open terms); if
   ``sum(b) > cash`` every ``b_i`` is scaled by ``cash / sum(b)``; ``fill = open * (1 + slip)``
   and ``units = b / (fill * (1 + fee_rate))``, so the budget includes the fee, ``cash -= b``
   exactly, and cash never goes negative (no leverage, no shorting).
4. **Slippage** per symbol is :func:`lab.engine.costs.slippage_series` on that symbol's own
   candles (causal: the fraction at its row j depends only on its rows < j).
5. **Forced exits** (survivorship-safe delisting returns). If a symbol is held (units > 0) at
   its LAST bar in the data and that bar is earlier than the union's last row (it stopped
   printing: delisted), everything is sold at that bar's ``close * (1 - slip)`` with the fee,
   reason ``"delisting"``. Likewise at the last bar before each time in
   ``segment_starts[symbol]`` (a new listing segment, e.g. a 1:100 redenomination or a reused
   ticker: prices across the break are a different market and are never held through),
   reason ``"segment_end"``. Forced exits are booked at the close even if that bar is
   ``tradable=False`` (there is no later price for that market). At a forced-exit bar any
   pending target for the symbol is cancelled, including one decided at that same close (it
   was decided on the old market); a decision at a later close may buy the new segment.
   "This is the symbol's last bar / last bar of its segment" is the ONLY end-of-data knowledge
   the engine uses, and only to exit. A consequence: a symbol whose final rows in the data are
   a hole (it stops printing before the union ends) is treated as delisted at its last bar.
6. **Holes.** A held symbol with no bar at row j is marked at its last close and cannot
   trade there. This is valuation of a held position, not forward-filled data.
7. **Equity** at each close = ``cash + sum(units * mark)`` with mark = close where the symbol
   has a bar, else its last close (after any forced exit at that close). ``returns`` are the
   per-row simple returns of that equity, ``returns[0] = 0``. ``weights`` (the output) is the
   held weight ``units * mark / equity`` at each close. ``turnover`` at row j = traded notional
   at fill prices (sells + buys at the open, plus forced exits at the close) / ``equity_open``
   of row j.
8. **Gross.** ``gross_returns`` re-runs the same decisions with ``ZERO_COSTS`` and zero
   slippage (sizes then differ slightly from the net run; that is intended).

Pure: no I/O, no prints, no module state. Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from lab.engine.costs import slippage_series, validate_costs
from lab.types import CostModel, validate_candles

_SUM_TOL = 1e-9  # float slack on a decision row's weight sum
_REASON_DELISTING = 1
_REASON_SEGMENT_END = 2
_REASON_NAMES = {_REASON_DELISTING: "delisting", _REASON_SEGMENT_END: "segment_end"}
ORDER_COLUMNS: tuple[str, ...] = ("time", "symbol", "side", "units", "price", "fee", "reason")


@dataclass(frozen=True)
class PortfolioResult:
    """Portfolio engine output on the union index (see the module docstring)."""

    equity: pd.Series  # marked at each close, name "equity"
    returns: pd.Series  # simple per-row returns of equity (row 0 = 0), name "returns"
    gross_returns: pd.Series  # same decisions with ZERO_COSTS and zero slippage
    weights: pd.DataFrame  # held weight per symbol at each close (units * mark / equity)
    turnover: pd.Series  # traded notional at fill prices / equity at that row's open
    orders: pd.DataFrame  # one row per fill, columns ORDER_COLUMNS
    fees_paid: float
    init_cash: float
    # One row per forced segment-end exit of a held position: the pre-hole close it was booked
    # at and the next segment's first open. ``reopen_ratio`` far from a power of ten flags a
    # crash during a suspension that the exit dodged (the break itself is only known after the
    # hole), so reports list these and never treat them as skill.
    segment_exits: pd.DataFrame


SEGMENT_EXIT_COLUMNS: tuple[str, ...] = (
    "time",
    "symbol",
    "exit_close",
    "reopen_time",
    "reopen_open",
    "reopen_ratio",
)


@dataclass(frozen=True)
class NetRun:
    """Net-only result of :func:`net_returns_for`."""

    returns: pd.Series
    turnover: pd.Series


@dataclass(frozen=True)
class _Market:
    """Dense (rows x symbols) arrays on the union index."""

    index: pd.DatetimeIndex
    symbols: tuple[str, ...]
    open: np.ndarray  # NaN where the symbol has no bar
    close: np.ndarray
    has_bar: np.ndarray  # bool
    can_fill: np.ndarray  # bool: has a bar and tradable
    slip: np.ndarray  # slippage fraction at the symbol's own row; 0 where no bar
    force: np.ndarray  # int8: 0, _REASON_DELISTING or _REASON_SEGMENT_END at a close


@dataclass(frozen=True)
class _Sim:
    equity: np.ndarray
    held: np.ndarray
    turnover: np.ndarray
    orders: list[tuple[int, np.ndarray, str, np.ndarray, np.ndarray, np.ndarray, str]]
    fees_paid: float


# --------------------------------------------------------------------------- validation


def _check_symbol_candles(symbol: str, candles: object) -> pd.DataFrame:
    if not isinstance(candles, pd.DataFrame):
        raise ValueError(
            f"candles_by_symbol[{symbol!r}] must be a pandas DataFrame, "
            f"got {type(candles).__name__}"
        )
    try:
        validate_candles(candles)
    except ValueError as exc:
        raise ValueError(f"candles_by_symbol[{symbol!r}]: {exc}") from None
    if len(candles) == 0:
        raise ValueError(f"candles_by_symbol[{symbol!r}] must contain at least one bar")
    if not pd.api.types.is_bool_dtype(candles["tradable"]) or candles["tradable"].hasnans:
        raise ValueError(f"candles_by_symbol[{symbol!r}]['tradable'] must be bool without NA")
    prices = candles[["open", "high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError(
            f"candles_by_symbol[{symbol!r}] open/high/low/close must be finite and > 0"
        )
    return candles


def _check_number(name: str, value: object, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | np.floating | np.integer):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    v = float(value)
    if not np.isfinite(v) or (v <= 0 if positive else v < 0):
        bound = "> 0" if positive else ">= 0"
        raise ValueError(f"{name} must be a finite number {bound}, got {value!r}")
    return v


def _check_weights(
    weights: object, index: pd.DatetimeIndex, symbols: tuple[str, ...]
) -> np.ndarray:
    if not isinstance(weights, pd.DataFrame):
        raise ValueError(f"weights must be a pandas DataFrame, got {type(weights).__name__}")
    if not weights.index.equals(index):
        raise ValueError(
            "weights index must equal the sorted union of every symbol's candle index "
            f"({len(index)} rows), got {len(weights.index)} rows that differ"
        )
    if tuple(weights.columns) != symbols:
        raise ValueError(
            f"weights columns must be the sorted symbols {list(symbols)}, "
            f"got {list(weights.columns)}"
        )
    for col in symbols:
        dtype = weights[col].dtype
        if not pd.api.types.is_numeric_dtype(dtype) or pd.api.types.is_bool_dtype(dtype):
            raise ValueError(f"weights[{col!r}] must be a float column, got dtype {dtype}")
    w = weights.to_numpy(dtype=float, na_value=np.nan)
    present = ~np.isnan(w)
    bad = present & (~np.isfinite(w) | (w < 0) | (w > 1))
    if bad.any():
        r, c = np.argwhere(bad)[0]
        raise ValueError(
            f"weights must be NaN or finite within [0, 1]; weights[{symbols[c]!r}] at "
            f"{index[r]} is {w[r, c]!r}"
        )
    sums = np.where(present, w, 0.0).sum(axis=1)
    over = present.any(axis=1) & (sums > 1 + _SUM_TOL)
    if over.any():
        r = int(np.flatnonzero(over)[0])
        raise ValueError(
            f"weights row at {index[r]} sums to {sums[r]!r}; a decision row must sum to <= 1 "
            "(long-only, no leverage)"
        )
    return w


def _segment_end_rows(
    segment_starts: object,
    frames: Mapping[str, pd.DataFrame],
) -> dict[str, np.ndarray]:
    """Per symbol, own-row positions of the last bar before each new segment."""
    if segment_starts is None:
        return {}
    if not isinstance(segment_starts, Mapping):
        raise ValueError(
            f"segment_starts must be a Mapping[str, DatetimeIndex] or None, "
            f"got {type(segment_starts).__name__}"
        )
    out: dict[str, np.ndarray] = {}
    for symbol, starts in segment_starts.items():
        if symbol not in frames:
            raise ValueError(
                f"segment_starts has symbol {symbol!r} that is not in candles_by_symbol"
            )
        if not isinstance(starts, pd.DatetimeIndex) or starts.tz is None:
            raise ValueError(f"segment_starts[{symbol!r}] must be a tz-aware UTC DatetimeIndex")
        if str(starts.tz) != "UTC":
            raise ValueError(f"segment_starts[{symbol!r}] must be UTC, got {starts.tz}")
        own = frames[symbol].index
        pos = own.get_indexer(starts)
        if (pos < 0).any():
            missing = starts[pos < 0][0]
            raise ValueError(
                f"segment_starts[{symbol!r}] has {missing}, which is not a bar of {symbol!r}"
            )
        if (pos == 0).any():
            raise ValueError(
                f"segment_starts[{symbol!r}] includes the symbol's first bar {own[0]}; "
                "list only bars where a NEW segment begins after earlier bars"
            )
        out[symbol] = np.unique(pos) - 1
    return out


# --------------------------------------------------------------------------- market arrays


def _build_market(
    frames: Mapping[str, pd.DataFrame],
    symbols: tuple[str, ...],
    index: pd.DatetimeIndex,
    costs: CostModel,
    segment_ends: Mapping[str, np.ndarray],
) -> _Market:
    n, m = len(index), len(symbols)
    open_ = np.full((n, m), np.nan)
    close = np.full((n, m), np.nan)
    has_bar = np.zeros((n, m), dtype=bool)
    can_fill = np.zeros((n, m), dtype=bool)
    slip = np.zeros((n, m))
    force = np.zeros((n, m), dtype=np.int8)
    for k, symbol in enumerate(symbols):
        candles = frames[symbol]
        rows = index.get_indexer(candles.index)
        open_[rows, k] = candles["open"].to_numpy(dtype=float)
        close[rows, k] = candles["close"].to_numpy(dtype=float)
        has_bar[rows, k] = True
        can_fill[rows, k] = candles["tradable"].to_numpy(dtype=bool)
        slip[rows, k] = slippage_series(candles, costs)
        if rows[-1] < n - 1:
            force[rows[-1], k] = _REASON_DELISTING
        if symbol in segment_ends:
            force[rows[segment_ends[symbol]], k] = _REASON_SEGMENT_END
    return _Market(index, symbols, open_, close, has_bar, can_fill, slip, force)


# --------------------------------------------------------------------------- simulation


def _simulate(
    mkt: _Market,
    w: np.ndarray,
    decision: np.ndarray,
    fee_rate: float,
    slip: np.ndarray,
    init_cash: float,
    min_rebalance: float,
) -> _Sim:
    n, m = w.shape
    units = np.zeros(m)
    last_close = np.zeros(m)  # 0 until the symbol prints; units are 0 then, so it is unused
    pending = np.full(m, np.nan)  # NaN = no pending target
    cash = float(init_cash)
    fees_paid = 0.0
    equity = np.empty(n)
    held = np.zeros((n, m))
    turnover = np.zeros(n)
    orders: list[tuple[int, np.ndarray, str, np.ndarray, np.ndarray, np.ndarray, str]] = []

    for j in range(n):
        has_bar = mkt.has_bar[j]
        traded = 0.0
        equity_open = np.nan

        # ---- open: execute pending targets on symbols that can fill here
        executing = ~np.isnan(pending) & mkt.can_fill[j]
        force_row = mkt.force[j]
        forced = force_row != 0
        if executing.any() or (forced & (units > 0)).any():
            mark_open = np.where(has_bar, mkt.open[j], last_close)
            equity_open = cash + float(units @ mark_open)
        if executing.any():
            cols = np.flatnonzero(executing)
            target = pending[cols]
            pending[cols] = np.nan
            px = mkt.open[j, cols]
            u = units[cols]
            current = u * px / equity_open
            delta = target * equity_open - u * px
            sell_all = (target == 0.0) & (u > 0)
            act = (np.abs(target - current) >= min_rebalance) | sell_all
            # Sells first.
            sells = act & ((delta < 0) | sell_all)
            if sells.any():
                c = cols[sells]
                sold = np.where(
                    sell_all[sells], u[sells], np.minimum(u[sells], -delta[sells] / px[sells])
                )
                fill = px[sells] * (1.0 - slip[j, c])
                notional = sold * fill
                fee = fee_rate * notional
                cash += float(notional.sum() - fee.sum())
                units[c] -= sold
                units[c[sell_all[sells]]] = 0.0
                fees_paid += float(fee.sum())
                traded += float(notional.sum())
                orders.append((j, c, "sell", sold, fill, fee, "rebalance"))
            # Then buys, scaled to the cash available.
            buys = act & ~sell_all & (delta > 0)
            if buys.any():
                c = cols[buys]
                budget = delta[buys]
                total = float(budget.sum())
                if total > cash:
                    budget = budget * (cash / total)
                    total = float(budget.sum())
                fill = px[buys] * (1.0 + slip[j, c])
                bought = budget / (fill * (1.0 + fee_rate))
                notional = bought * fill
                fee = fee_rate * notional
                cash = max(cash - total, 0.0)
                units[c] += bought
                fees_paid += float(fee.sum())
                traded += float(notional.sum())
                orders.append((j, c, "buy", bought, fill, fee, "rebalance"))

        # ---- close: forced exits, then mark
        if forced.any():
            pending[forced] = np.nan
            exit_mask = forced & (units > 0)
            if exit_mask.any():
                c = np.flatnonzero(exit_mask)
                sold = units[c].copy()
                fill = mkt.close[j, c] * (1.0 - slip[j, c])
                notional = sold * fill
                fee = fee_rate * notional
                cash += float(notional.sum() - fee.sum())
                units[c] = 0.0
                fees_paid += float(fee.sum())
                traded += float(notional.sum())
                for code, name in _REASON_NAMES.items():
                    pick = force_row[c] == code
                    if pick.any():
                        orders.append((j, c[pick], "sell", sold[pick], fill[pick], fee[pick], name))
        last_close = np.where(has_bar, mkt.close[j], last_close)
        eq = cash + float(units @ last_close)
        equity[j] = eq
        held[j] = units * last_close / eq
        if traded:
            turnover[j] = traded / equity_open

        # ---- decision at this close (fills from row j + 1)
        if decision[j] and j < n - 1:
            pending = np.nan_to_num(w[j], nan=0.0)
            pending[forced] = np.nan  # decided on a market that ends at this close

    return _Sim(equity, held, turnover, orders, fees_paid)


def _orders_frame(sim: _Sim, mkt: _Market) -> pd.DataFrame:
    if not sim.orders:
        return pd.DataFrame(
            {
                "time": pd.DatetimeIndex([], tz="UTC"),
                "symbol": pd.Series([], dtype=object),
                "side": pd.Series([], dtype=object),
                "units": pd.Series([], dtype=float),
                "price": pd.Series([], dtype=float),
                "fee": pd.Series([], dtype=float),
                "reason": pd.Series([], dtype=object),
            }
        )
    rows = np.concatenate([np.full(len(c), j) for j, c, *_ in sim.orders])
    cols = np.concatenate([c for _, c, *_ in sim.orders])
    sym = np.asarray(mkt.symbols, dtype=object)
    return pd.DataFrame(
        {
            "time": mkt.index[rows],
            "symbol": sym[cols],
            "side": np.concatenate(
                [np.full(len(c), s, dtype=object) for _, c, s, *_ in sim.orders]
            ),
            "units": np.concatenate([o[3] for o in sim.orders]),
            "price": np.concatenate([o[4] for o in sim.orders]),
            "fee": np.concatenate([o[5] for o in sim.orders]),
            "reason": np.concatenate([np.full(len(o[1]), o[6], dtype=object) for o in sim.orders]),
        }
    )


def _returns(equity: np.ndarray) -> np.ndarray:
    out = np.zeros(len(equity))
    out[1:] = equity[1:] / equity[:-1] - 1.0
    return out


# --------------------------------------------------------------------------- public


def run_portfolio(
    candles_by_symbol: Mapping[str, pd.DataFrame],
    weights: pd.DataFrame,
    *,
    costs: CostModel,
    init_cash: float = 10_000.0,
    min_rebalance: float = 0.0,
    segment_starts: Mapping[str, pd.DatetimeIndex] | None = None,
) -> PortfolioResult:
    """Backtest long-only target weights across many symbols (see the module docstring).

    ``candles_by_symbol`` follows the :mod:`lab.types` candle convention per symbol (missing
    bars absent, ``tradable`` bool). ``weights`` is indexed by the sorted union of every
    candle index with the sorted symbols as columns; a row is a decision at that row's close
    (all-NaN row = hold). ``segment_starts[symbol]`` lists the symbol's bar times where a new
    listing segment begins (never its first bar); the position is force-closed at the close
    before each. Raises ``ValueError`` naming the bad input.
    """
    if not isinstance(candles_by_symbol, Mapping) or len(candles_by_symbol) == 0:
        raise ValueError("candles_by_symbol must be a non-empty Mapping[str, DataFrame]")
    for symbol in candles_by_symbol:
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"candles_by_symbol keys must be non-empty strings, got {symbol!r}")
    frames = {s: _check_symbol_candles(s, c) for s, c in candles_by_symbol.items()}
    symbols = tuple(sorted(frames))
    if not isinstance(costs, CostModel):
        raise ValueError(f"costs must be a CostModel, got {type(costs).__name__}")
    validate_costs(costs)
    cash0 = _check_number("init_cash", init_cash, positive=True)
    band = _check_number("min_rebalance", min_rebalance, positive=False)

    index = frames[symbols[0]].index
    for s in symbols[1:]:
        index = index.union(frames[s].index)
    index = pd.DatetimeIndex(index, name="time")
    w = _check_weights(weights, index, symbols)
    segment_ends = _segment_end_rows(segment_starts, frames)

    mkt = _build_market(frames, symbols, index, costs, segment_ends)
    decision = (~np.isnan(w)).any(axis=1)
    net = _simulate(mkt, w, decision, costs.fee_rate, mkt.slip, cash0, band)
    gross = _simulate(mkt, w, decision, 0.0, np.zeros_like(mkt.slip), cash0, band)

    held = pd.DataFrame(net.held, index=index, columns=pd.Index(symbols, name=weights.columns.name))
    orders = _orders_frame(net, mkt)
    return PortfolioResult(
        equity=pd.Series(net.equity, index=index, name="equity"),
        returns=pd.Series(_returns(net.equity), index=index, name="returns"),
        gross_returns=pd.Series(_returns(gross.equity), index=index, name="gross_returns"),
        weights=held,
        turnover=pd.Series(net.turnover, index=index, name="turnover"),
        orders=orders,
        fees_paid=float(net.fees_paid),
        init_cash=cash0,
        segment_exits=_segment_exits(orders, frames),
    )


def _segment_exits(orders: pd.DataFrame, frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for order in orders[orders["reason"] == "segment_end"].itertuples(index=False):
        candles = frames[str(order.symbol)]
        exit_close = float(candles.loc[order.time, "close"])
        after = candles.index[candles.index > order.time]
        if len(after):
            reopen = float(candles.loc[after[0], "open"])
            rows.append(
                (order.time, order.symbol, exit_close, after[0], reopen, reopen / exit_close)
            )
    return pd.DataFrame(rows, columns=list(SEGMENT_EXIT_COLUMNS))


def net_returns_for(
    candles_by_symbol: Mapping[str, pd.DataFrame],
    weights_list: Sequence[pd.DataFrame],
    *,
    costs: CostModel,
    init_cash: float = 10_000.0,
    min_rebalance: float = 0.0,
    segment_starts: Mapping[str, pd.DatetimeIndex] | None = None,
) -> list[NetRun]:
    """Net per-bar returns and turnover of several weight frames on the same market, with
    exactly the semantics of :func:`run_portfolio` but building the market once and skipping
    the gross re-run, orders and held weights. For nulls that need hundreds of runs."""
    if not isinstance(candles_by_symbol, Mapping) or len(candles_by_symbol) == 0:
        raise ValueError("candles_by_symbol must be a non-empty Mapping[str, DataFrame]")
    frames = {s: _check_symbol_candles(s, c) for s, c in candles_by_symbol.items()}
    symbols = tuple(sorted(frames))
    if not isinstance(costs, CostModel):
        raise ValueError(f"costs must be a CostModel, got {type(costs).__name__}")
    validate_costs(costs)
    cash0 = _check_number("init_cash", init_cash, positive=True)
    band = _check_number("min_rebalance", min_rebalance, positive=False)
    index = frames[symbols[0]].index
    for s in symbols[1:]:
        index = index.union(frames[s].index)
    index = pd.DatetimeIndex(index, name="time")
    mkt = _build_market(frames, symbols, index, costs, _segment_end_rows(segment_starts, frames))
    out = []
    for weights in weights_list:
        w = _check_weights(weights, index, symbols)
        decision = (~np.isnan(w)).any(axis=1)
        sim = _simulate(mkt, w, decision, costs.fee_rate, mkt.slip, cash0, band)
        out.append(
            NetRun(
                returns=pd.Series(_returns(sim.equity), index=index, name="returns"),
                turnover=pd.Series(sim.turnover, index=index, name="turnover"),
            )
        )
    return out
