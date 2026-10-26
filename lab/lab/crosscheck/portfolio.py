"""Independent re-implementation of the portfolio accounting (PLAN.md Phase 10 cross-check).

:func:`replay_portfolio` recomputes net equity from the same target weights, candles, costs,
segment starts and participation impact as :func:`lab.engine.portfolio.run_portfolio`, written
from that module's documented semantics only, deliberately differently: a plain Python loop
over rows and, inside it, over symbols one at a time with scalar arithmetic and dictionaries
(no dense arrays, no shared code with ``lab.engine.portfolio``). The cost *model* functions
(:func:`lab.engine.costs.slippage_series`, :func:`~lab.engine.costs.impact_inputs`,
:func:`~lab.engine.costs.impact_fraction`) are reused on purpose: they are the definition of
the cost assumption, not the accounting under test.

Semantics replayed (``lab.engine.portfolio`` module docstring): a decision row (any non-NaN
weight) decided at a close fills from the next row, per symbol at the first later open where
it has a tradable bar; a later decision replaces every pending target; sells before buys;
buys are scaled pro rata to the cash left; a target of 0 always sells everything; a symbol is
skipped inside the rebalance band; held symbols are force-sold at the close of their last bar
(delisting) or the last bar before a new listing segment; marks use the last close through
holes.

:func:`compare_equity` judges agreement: the largest relative difference of the two equity
curves at any row must be at most :data:`EQUITY_TOLERANCE` (``1e-8``). Both implementations do
the same arithmetic in a different order (vector sums vs a scalar loop), so they differ only by
float rounding, ~1e-15 relative per operation; 1e-8 leaves room for that to accumulate over
thousands of rows and orders while any accounting difference (one fee, one slippage side, a
wrongly scaled buy) is orders of magnitude larger.

What it can and cannot catch: the replay checks ACCOUNTING only. It takes the strategy's own
target weights as given and reuses the lab's cost functions, so a match says fills, fees,
slippage and impact charging, cash, scaling and marks agree with the engine. It cannot catch
signal look-ahead (the weights could already peek at the future and both implementations would
agree), nor a bug in the cost model itself (both sides call the same functions). Look-ahead is
the job of the perturbation tests and run_adversarial's stale-input check; the cost model is
tested against hand examples in the engine tests.

Pure: no I/O. Results are historical and not forward-looking.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from lab.engine.costs import ImpactModel, impact_fraction, impact_inputs, slippage_series
from lab.types import CostModel

__all__ = [
    "EQUITY_TOLERANCE",
    "CrosscheckComparison",
    "ReplayResult",
    "compare_equity",
    "replay_portfolio",
]

EQUITY_TOLERANCE = 1e-8  # max relative equity difference at any row (module docstring)


@dataclass(frozen=True)
class ReplayResult:
    equity: pd.Series  # net equity at each close of the union index
    fees_paid: float
    impact_cost: float
    n_orders: int  # one per symbol fill (rebalance buys/sells and forced exits)


@dataclass(frozen=True)
class CrosscheckComparison:
    matched: bool
    max_rel_diff: float
    worst_time: pd.Timestamp | None
    final_engine: float
    final_replay: float
    tolerance: float


def _impact(trade_value_share: float, adv: float, sigma: float, impact: ImpactModel) -> float:
    frac = impact_fraction(
        np.array([trade_value_share * impact.aum_usd]),
        np.array([adv]),
        np.array([sigma]),
        impact,
    )
    return float(frac[0])


def replay_portfolio(
    candles_by_symbol: Mapping[str, pd.DataFrame],
    weights: pd.DataFrame,
    *,
    costs: CostModel,
    init_cash: float,
    min_rebalance: float = 0.0,
    segment_starts: Mapping[str, pd.DatetimeIndex] | None = None,
    impact: ImpactModel | None = None,
) -> ReplayResult:
    """Net equity of target ``weights`` (union index x sorted symbols; all-NaN row = hold) on
    ``candles_by_symbol`` (module docstring). Raises ``ValueError`` for misaligned weights."""
    symbols = sorted(candles_by_symbol)
    union = candles_by_symbol[symbols[0]].index
    for s in symbols[1:]:
        union = union.union(candles_by_symbol[s].index)
    if not weights.index.equals(union) or list(weights.columns) != symbols:
        raise ValueError("weights must be on the union index with the sorted symbols as columns")

    # per-symbol lookups keyed by bar time
    bars: dict[str, dict[pd.Timestamp, tuple[float, float, bool, float, float, float]]] = {}
    forced_at: dict[str, dict[pd.Timestamp, str]] = {}
    for s in symbols:
        c = candles_by_symbol[s]
        slip = slippage_series(c, costs)
        starts = None
        if segment_starts is not None and s in segment_starts and len(segment_starts[s]):
            starts = segment_starts[s]
        if impact is not None:
            adv, sigma = impact_inputs(c, impact, segment_starts=starts)
        else:
            adv = sigma = np.full(len(c), np.nan)
        table = {}
        for i, t in enumerate(c.index):
            table[t] = (
                float(c["open"].iloc[i]),
                float(c["close"].iloc[i]),
                bool(c["tradable"].iloc[i]),
                float(slip[i]),
                float(adv[i]),
                float(sigma[i]),
            )
        bars[s] = table
        marks: dict[pd.Timestamp, str] = {}
        if starts is not None:
            for start in starts:
                pos = c.index.get_loc(start)
                marks[c.index[pos - 1]] = "segment_end"
        if c.index[-1] < union[-1]:
            marks[c.index[-1]] = "delisting"
        forced_at[s] = marks

    fee_rate = float(costs.fee_rate)
    cash = float(init_cash)
    units = dict.fromkeys(symbols, 0.0)
    last_close = dict.fromkeys(symbols, 0.0)
    pending: dict[str, float] = {}
    fees = 0.0
    impact_cost = 0.0
    n_orders = 0
    equity_out: list[float] = []
    w_values = weights.to_numpy(dtype=float)

    for row, t in enumerate(union):
        here = {s: bars[s].get(t) for s in symbols}
        fill_now = [s for s in symbols if s in pending and here[s] is not None and here[s][2]]
        forced_now = {s: forced_at[s][t] for s in symbols if t in forced_at[s]}

        equity_open = math.nan
        if fill_now or any(units[s] > 0 for s in forced_now):
            equity_open = cash
            for s in symbols:
                bar = here[s]
                equity_open += units[s] * (bar[0] if bar is not None else last_close[s])

        if fill_now:
            plan_rows = []
            for s in fill_now:
                target = pending.pop(s)
                open_, _, _, slip, adv, sigma = here[s]  # type: ignore[misc]
                current_value = units[s] * open_
                delta = target * equity_open - current_value
                sell_all = target == 0.0 and units[s] > 0
                band_ok = abs(target - current_value / equity_open) >= min_rebalance
                if band_ok or sell_all:
                    plan_rows.append((s, delta, sell_all, open_, slip, adv, sigma))
            for s, delta, sell_all, open_, slip, adv, sigma in plan_rows:
                if not (sell_all or delta < 0):
                    continue
                qty = units[s] if sell_all else min(units[s], -delta / open_)
                frac = 0.0
                if impact is not None:
                    frac = _impact(abs(delta) / equity_open, adv, sigma, impact)
                    impact_cost += qty * open_ * frac
                price = open_ * (1.0 - slip - frac)
                proceeds = qty * price
                fee = fee_rate * proceeds
                cash += proceeds - fee
                fees += fee
                units[s] = 0.0 if sell_all else units[s] - qty
                n_orders += 1
            buys = [r for r in plan_rows if not r[2] and r[1] > 0]
            if buys:
                wanted = sum(r[1] for r in buys)
                scale = cash / wanted if wanted > cash else 1.0
                spent = 0.0
                for s, delta, _, open_, slip, adv, sigma in buys:
                    frac = 0.0
                    if impact is not None:  # sized on the intended trade, before scaling
                        frac = _impact(delta / equity_open, adv, sigma, impact)
                    budget = delta * scale
                    price = open_ * (1.0 + slip + frac)
                    qty = budget / (price * (1.0 + fee_rate))
                    if impact is not None:
                        impact_cost += qty * open_ * frac
                    fees += fee_rate * qty * price
                    units[s] += qty
                    spent += budget
                    n_orders += 1
                cash = max(cash - spent, 0.0)

        if forced_now:
            for s in forced_now:
                pending.pop(s, None)
            held_forced = [s for s in symbols if s in forced_now and units[s] > 0]
            if held_forced:
                equity_close = cash
                for s in symbols:
                    bar = here[s]
                    equity_close += units[s] * (bar[1] if bar is not None else last_close[s])
                for s in held_forced:
                    _, close, _, slip, adv, sigma = here[s]  # type: ignore[misc]
                    qty = units[s]
                    frac = 0.0
                    if impact is not None:
                        frac = _impact(qty * close / equity_close, adv, sigma, impact)
                        impact_cost += qty * close * frac
                    price = close * (1.0 - slip - frac)
                    proceeds = qty * price
                    fee = fee_rate * proceeds
                    cash += proceeds - fee
                    fees += fee
                    units[s] = 0.0
                    n_orders += 1

        for s in symbols:
            if here[s] is not None:
                last_close[s] = here[s][1]  # type: ignore[index]
        equity_out.append(cash + sum(units[s] * last_close[s] for s in symbols))

        row_w = w_values[row]
        if row < len(union) - 1 and not np.isnan(row_w).all():
            pending = {
                s: (0.0 if np.isnan(row_w[k]) else float(row_w[k]))
                for k, s in enumerate(symbols)
                if s not in forced_now
            }

    return ReplayResult(
        equity=pd.Series(equity_out, index=union, name="equity"),
        fees_paid=fees,
        impact_cost=impact_cost,
        n_orders=n_orders,
    )


def compare_equity(
    engine: pd.Series, replay: pd.Series, *, tolerance: float = EQUITY_TOLERANCE
) -> CrosscheckComparison:
    """Largest relative difference ``|a - b| / max(|a|, tiny)`` over every row; matched when it
    is at most ``tolerance`` and both curves have the same index."""
    if not engine.index.equals(replay.index):
        return CrosscheckComparison(False, math.inf, None, math.nan, math.nan, tolerance)
    a = engine.to_numpy(dtype=float)
    b = replay.to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.abs(a - b) / np.maximum(np.abs(a), 1e-12)
    if not np.isfinite(rel).all():
        return CrosscheckComparison(False, math.inf, None, float(a[-1]), float(b[-1]), tolerance)
    k = int(np.argmax(rel)) if rel.size else 0
    worst = float(rel[k]) if rel.size else 0.0
    return CrosscheckComparison(
        matched=worst <= tolerance,
        max_rel_diff=worst,
        worst_time=pd.Timestamp(engine.index[k]) if rel.size else None,
        final_engine=float(a[-1]) if a.size else math.nan,
        final_replay=float(b[-1]) if b.size else math.nan,
        tolerance=tolerance,
    )
