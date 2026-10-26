"""Portfolio engine (lab.engine.portfolio) against hand-calculated 3-6 bar examples.

Costs in the hand examples: fee 1% and slippage 1% per side with ``slippage_vol_mult=0`` (a
flat rate), so every fill, fee, and equity number is easy to check by hand; the arithmetic is
written out next to each expectation. Bars are daily (open, close) pairs; high/low are set to
max/min of the two. Tolerance: relative 1e-12, because the engine reaches the same numbers
through a handful of float operations in a different order.

The perturbation tests compare outputs through bar t with ``np.array_equal`` (bit-identical).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest
from _pytest.python_api import ApproxBase

from lab.engine.costs import ImpactModel
from lab.engine.portfolio import ORDER_COLUMNS, PortfolioResult, net_returns_for, run_portfolio
from lab.types import ZERO_COSTS, CostModel
from tests.synthetic import make_candles, perturb_after

FEE, SLIP = 0.01, 0.01
COSTS = CostModel(fee_rate=FEE, slippage_rate=SLIP, slippage_vol_mult=0.0)
CASH = 10_000.0
REL = 1e-12
START = pd.Timestamp("2024-01-01", tz="UTC")


def approx(x: float) -> ApproxBase:
    return pytest.approx(x, rel=REL, abs=1e-9)


def bars(
    rows: Sequence[tuple[float, float]],
    *,
    offset: int = 0,
    drop: Sequence[int] = (),
    untradable: Sequence[int] = (),
) -> pd.DataFrame:
    """Daily candles from (open, close) pairs starting ``offset`` days after START.

    ``drop`` removes rows (missing bars), ``untradable`` marks rows ``tradable=False``.
    """
    arr = np.asarray(rows, dtype=float)
    index = pd.date_range(START + pd.Timedelta(days=offset), periods=len(rows), freq="1D")
    frame = pd.DataFrame(
        {
            "open": arr[:, 0],
            "high": arr.max(axis=1),
            "low": arr.min(axis=1),
            "close": arr[:, 1],
            "volume": np.ones(len(rows)),
            "tradable": np.ones(len(rows), dtype=bool),
        },
        index=pd.DatetimeIndex(index, name="time"),
    )
    frame.iloc[list(untradable), frame.columns.get_loc("tradable")] = False
    return frame.drop(index=frame.index[list(drop)])


def union(cands: Mapping[str, pd.DataFrame]) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex([], tz="UTC")
    for c in cands.values():
        idx = idx.union(c.index)
    return pd.DatetimeIndex(idx, name="time")


def targets(
    cands: Mapping[str, pd.DataFrame], decisions: Mapping[int, Mapping[str, float]]
) -> pd.DataFrame:
    """Weights frame on the union index; ``decisions[row] = {symbol: weight}`` (rest NaN)."""
    idx = union(cands)
    w = pd.DataFrame(np.nan, index=idx, columns=sorted(cands))
    for row, vec in decisions.items():
        for sym, val in vec.items():
            w.iloc[row, w.columns.get_loc(sym)] = val
    return w


def orders_at(res: PortfolioResult, row: int) -> pd.DataFrame:
    return res.orders[res.orders["time"] == res.equity.index[row]].reset_index(drop=True)


# --------------------------------------------------------------------------- hand examples


def test_two_symbol_rebalance_with_fees_and_slippage() -> None:
    cands = {
        "A": bars([(100, 100), (100, 110), (110, 120), (120, 120)]),
        "B": bars([(50, 50), (50, 40), (40, 50), (50, 50)]),
    }
    w = targets(cands, {0: {"A": 0.5, "B": 0.5}, 2: {"A": 1.0}})  # row 2: B NaN -> 0
    res = run_portfolio(cands, w, costs=COSTS, init_cash=CASH)

    # Row 1 open: equity 10000, budgets 5000 each (fee included), cash -> 0.
    ua = 5000 / (101.0 * 1.01)  # fill 100 * 1.01 = 101
    ub = 5000 / (50.5 * 1.01)  # fill 50 * 1.01 = 50.5
    fee_a1, fee_b1 = ua * 101.0 * FEE, ub * 50.5 * FEE  # 5000 * 0.01 / 1.01 each
    eq1 = ua * 110 + ub * 40
    eq2 = ua * 120 + ub * 50
    # Row 3 open: equity_open = eq2 (opens equal the row-2 closes). Sell all B at 50 * 0.99.
    fee_b3 = ub * 49.5 * FEE
    cash = ub * 49.5 - fee_b3
    # A target 1.0: delta = eq2 - ua * 120 = ub * 50 > cash -> budget scaled to the cash.
    add = cash / (120 * 1.01 * 1.01)  # fill 120 * 1.01 = 121.2
    fee_a3 = add * 121.2 * FEE
    eq3 = (ua + add) * 120

    assert res.equity.tolist() == [approx(CASH), approx(eq1), approx(eq2), approx(eq3)]
    assert res.returns.iloc[0] == 0.0
    assert res.returns.iloc[3] == approx(eq3 / eq2 - 1)
    o = res.orders
    assert list(o.columns) == list(ORDER_COLUMNS)
    assert o[["symbol", "side", "reason"]].values.tolist() == [
        ["A", "buy", "rebalance"],
        ["B", "buy", "rebalance"],
        ["B", "sell", "rebalance"],  # sells before buys at the same open
        ["A", "buy", "rebalance"],
    ]
    assert list(o["time"]) == [w.index[1], w.index[1], w.index[3], w.index[3]]
    assert o["units"].tolist() == [approx(ua), approx(ub), approx(ub), approx(add)]
    assert o["price"].tolist() == [approx(101.0), approx(50.5), approx(49.5), approx(121.2)]
    assert o["fee"].tolist() == [approx(fee_a1), approx(fee_b1), approx(fee_b3), approx(fee_a3)]
    assert res.fees_paid == approx(fee_a1 + fee_b1 + fee_b3 + fee_a3)
    assert res.turnover.tolist() == [
        0.0,
        approx((ua * 101 + ub * 50.5) / CASH),  # = 1 / 1.01
        0.0,
        approx((ub * 49.5 + add * 121.2) / eq2),
    ]
    assert res.weights.loc[w.index[1]].tolist() == [approx(ua * 110 / eq1), approx(ub * 40 / eq1)]
    assert res.weights.loc[w.index[3]].tolist() == [approx(1.0), 0.0]
    assert res.equity.name == "equity" and res.returns.name == "returns"
    assert res.init_cash == CASH


def test_pending_target_waits_for_a_tradable_bar_and_a_missing_bar() -> None:
    cands = {
        "A": bars([(10, 10), (11, 11), (12, 12), (13, 13)], untradable=[1]),
        "B": bars([(20, 20), (21, 21), (22, 22), (23, 23)], drop=[1]),
    }
    w = targets(cands, {0: {"A": 0.5, "B": 0.5}})
    res = run_portfolio(cands, w, costs=ZERO_COSTS, init_cash=CASH)

    # Row 1: A untradable, B has no bar -> nothing fills, all cash.
    assert res.equity.iloc[1] == CASH
    assert res.orders["time"].tolist() == [w.index[2], w.index[2]]
    assert res.orders["price"].tolist() == [12.0, 22.0]
    assert res.orders["units"].tolist() == [approx(5000 / 12), approx(5000 / 22)]
    assert res.equity.iloc[3] == approx(5000 / 12 * 13 + 5000 / 22 * 23)


def test_later_decision_replaces_a_pending_target() -> None:
    cands = {
        "A": bars([(10, 10), (10, 10), (10, 10), (20, 20)], untradable=[1, 2]),
        "B": bars([(10, 10), (10, 10), (10, 10), (10, 10)]),
    }
    # Row 0 wants A 0.8 / B 0.2; row 1 replaces it with A 0.3 and (NaN ->) B 0.
    w = targets(cands, {0: {"A": 0.8, "B": 0.2}, 1: {"A": 0.3}})
    res = run_portfolio(cands, w, costs=ZERO_COSTS, init_cash=CASH)

    o = res.orders
    assert o[["symbol", "side"]].values.tolist() == [["B", "buy"], ["B", "sell"], ["A", "buy"]]
    assert list(o["time"]) == [w.index[1], w.index[2], w.index[3]]
    assert o["units"].tolist() == [approx(200.0), approx(200.0), approx(0.3 * CASH / 20)]
    assert res.weights.iloc[3].tolist() == [approx(0.3), 0.0]


def test_decision_on_last_row_is_dropped() -> None:
    cands = {"A": bars([(10, 10), (10, 10), (10, 10)]), "B": bars([(5, 5), (5, 5), (5, 5)])}
    res = run_portfolio(cands, targets(cands, {2: {"A": 1.0}}), costs=COSTS, init_cash=CASH)
    assert res.orders.empty
    assert list(res.orders.columns) == list(ORDER_COLUMNS)
    assert res.equity.tolist() == [CASH] * 3


def test_buys_are_scaled_pro_rata_when_cash_is_short() -> None:
    cands = {
        "A": bars([(100, 100), (100, 100), (100, 100)]),
        "B": bars([(20, 20), (20, 20), (20, 20)]),
        "C": bars([(40, 40), (40, 40), (40, 40)]),
    }
    w = targets(cands, {0: {"A": 1.0}, 1: {"B": 0.5, "C": 0.5}})
    res = run_portfolio(cands, w, costs=COSTS, init_cash=CASH)

    ua = CASH / (101.0 * 1.01)
    eq_open = ua * 100
    cash = ua * 99 * 0.99  # sell all A at 100 * 0.99, minus the 1% fee
    # Budgets 0.5 * eq_open each sum to eq_open > cash -> each scaled to cash / 2.
    ub = (cash / 2) / (20.2 * 1.01)
    uc = (cash / 2) / (40.4 * 1.01)
    row2 = orders_at(res, 2)
    assert row2[["symbol", "side"]].values.tolist() == [["A", "sell"], ["B", "buy"], ["C", "buy"]]
    assert row2["units"].tolist() == [approx(ua), approx(ub), approx(uc)]
    # Cash ends at exactly 0: every scaled budget (fee included) is spent.
    assert res.equity.iloc[2] == approx(ub * 20 + uc * 40)
    assert res.weights.iloc[2].sum() == approx(1.0)
    assert res.turnover.iloc[2] == approx((ua * 99 + ub * 20.2 + uc * 40.4) / eq_open)


def test_delisting_exits_at_last_close_and_books_the_crash() -> None:
    cands = {
        "A": bars([(100, 100), (100, 90), (60, 30)]),  # stops printing after row 2
        "B": bars([(10, 10)] * 5),
    }
    w = targets(cands, {0: {"A": 0.5, "B": 0.5}, 3: {"A": 0.5, "B": 0.5}})
    res = run_portfolio(cands, w, costs=COSTS, init_cash=CASH)

    ua = 5000 / (101.0 * 1.01)
    ub = 5000 / (10.1 * 1.01)
    eq_open2 = ua * 60 + ub * 10
    fill = 30 * 0.99  # the crashed last close, slipped
    fee = ua * fill * FEE
    cash = ua * fill - fee
    assert res.equity.iloc[1] == approx(ua * 90 + ub * 10)
    assert res.equity.iloc[2] == approx(cash + ub * 10)
    exit_row = orders_at(res, 2)
    assert exit_row.values.tolist() == [
        [w.index[2], "A", "sell", approx(ua), approx(fill), approx(fee), "delisting"]
    ]
    assert res.turnover.iloc[2] == approx(ua * fill / eq_open2)
    # The row-3 decision's A target never fills (no more bars); B rebalances to 0.5.
    later = res.orders[res.orders["time"] > w.index[2]]
    assert set(later["symbol"]) == {"B"}
    assert res.weights["A"].iloc[2:].tolist() == [0.0, 0.0, 0.0]


def test_segment_end_exit_then_rebuy_never_sees_the_redenomination_jump() -> None:
    # A 1:100 redenomination at row 3: the old market ends at row 2's close (110).
    rows_a = [(100, 100), (100, 100), (100, 110), (11000, 11000), (11000, 12100), (12100, 12100)]
    cands = {"A": bars(rows_a), "B": bars([(10, 10)] * 6)}
    seg = {"A": pd.DatetimeIndex([cands["A"].index[3]])}
    # Row 2's decision is made on the old market at the segment-end close -> cancelled.
    w = targets(cands, {0: {"A": 1.0}, 2: {"A": 1.0}, 3: {"A": 1.0}})
    res = run_portfolio(cands, w, costs=COSTS, init_cash=CASH, segment_starts=seg)

    ua = CASH / (101.0 * 1.01)
    cash = ua * 108.9 * 0.99  # sell at 110 * 0.99, 1% fee
    u_new = cash / (11110.0 * 1.01)  # bought at row 4's open 11000 * 1.01
    assert res.equity.tolist() == [
        approx(CASH),
        approx(ua * 100),
        approx(cash),
        approx(cash),
        approx(u_new * 12100),
        approx(u_new * 12100),
    ]
    o = res.orders
    assert o[["side", "reason"]].values.tolist() == [
        ["buy", "rebalance"],
        ["sell", "segment_end"],
        ["buy", "rebalance"],
    ]
    assert list(o["time"]) == [w.index[1], w.index[2], w.index[4]]
    assert (res.equity / res.equity.shift(1)).dropna().max() < 1.2

    # Without the segment break the position is held through the 100x jump.
    naive = run_portfolio(cands, w, costs=COSTS, init_cash=CASH)
    assert naive.equity.iloc[3] == approx(ua * 11000)


def test_segment_end_cancels_pending_buy_from_the_old_market() -> None:
    rows_a = [(100, 100), (100, 100), (1, 1), (1, 1)]
    cands = {"A": bars(rows_a, untradable=[1]), "B": bars([(10, 10)] * 4)}
    seg = {"A": pd.DatetimeIndex([cands["A"].index[2]])}
    # Decided at row 0; A cannot fill at row 1 (untradable), then its market ends.
    w = targets(cands, {0: {"A": 0.5}})
    res = run_portfolio(cands, w, costs=ZERO_COSTS, init_cash=CASH, segment_starts=seg)
    assert res.orders.empty
    assert res.equity.tolist() == [CASH] * 4


def test_min_rebalance_band_skips_small_changes_but_always_sells_to_zero() -> None:
    cands = {
        "A": bars([(100, 100), (100, 100), (100, 110), (110, 110)]),
        "B": bars([(10, 10), (10, 10), (10, 5), (5, 5)]),
        "C": bars([(10, 10)] * 4),
    }
    w = targets(cands, {0: {"A": 0.8, "B": 0.15}, 2: {"A": 0.85, "C": 0.05}})
    res = run_portfolio(cands, w, costs=ZERO_COSTS, init_cash=CASH, min_rebalance=0.1)

    # Row 1: A 80 units, B 150 units, cash 500 (both moves >= 0.1 from 0).
    assert orders_at(res, 1)["units"].tolist() == [approx(80.0), approx(150.0)]
    # Row 3 open: equity 80*110 + 150*5 + 500 = 10050. A 0.8756 -> 0.85 (skip), C 0 -> 0.05
    # (skip), B 0.0746 -> 0: inside the band, but a zero target always sells everything.
    row3 = orders_at(res, 3)
    assert row3[["symbol", "side", "units", "price"]].values.tolist() == [
        ["B", "sell", approx(150.0), approx(5.0)]
    ]
    assert res.weights.iloc[3].tolist() == [approx(8800 / 10050), 0.0, 0.0]

    no_band = run_portfolio(cands, w, costs=ZERO_COSTS, init_cash=CASH)
    assert set(orders_at(no_band, 3)["symbol"]) == {"A", "B", "C"}


def test_gross_returns_rerun_the_decisions_without_costs() -> None:
    cands = {
        "A": bars([(100, 100), (100, 110), (110, 120), (120, 120)]),
        "B": bars([(50, 50), (50, 40), (40, 50), (50, 50)]),
    }
    w = targets(cands, {0: {"A": 0.5, "B": 0.5}, 2: {"A": 1.0}})
    net = run_portfolio(cands, w, costs=COSTS, init_cash=CASH)
    free = run_portfolio(cands, w, costs=ZERO_COSTS, init_cash=CASH)

    assert np.array_equal(net.gross_returns.to_numpy(), free.returns.to_numpy())
    assert np.array_equal(free.gross_returns.to_numpy(), free.returns.to_numpy())
    assert net.gross_returns.name == "gross_returns"
    assert net.equity.iloc[-1] < free.equity.iloc[-1]
    assert free.fees_paid == 0.0
    assert net.fees_paid == approx(float(net.orders["fee"].sum()))


def test_hole_in_a_held_symbol_is_marked_at_its_last_close() -> None:
    cands = {
        "A": bars([(10, 10), (10, 12), (14, 14), (15, 15)], drop=[2]),
        "B": bars([(5, 5)] * 4),
    }
    w = targets(cands, {0: {"A": 1.0}})
    res = run_portfolio(cands, w, costs=ZERO_COSTS, init_cash=CASH)
    assert res.equity.tolist() == [CASH, approx(12_000.0), approx(12_000.0), approx(15_000.0)]


# --------------------------------------------------------------------------- validation


def _ok() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    cands = {"A": bars([(10, 10)] * 3), "B": bars([(5, 5)] * 3)}
    return cands, targets(cands, {0: {"A": 0.5}})


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda c, w, kw: (c, w.iloc[:-1], kw), "weights index"),
        (lambda c, w, kw: (c, w[["B", "A"]], kw), "weights columns"),
        (lambda c, w, kw: (c, w.assign(A=[0.6, np.nan, np.nan], B=0.6), kw), "sums to"),
        (lambda c, w, kw: (c, w.assign(A=[-0.1, np.nan, np.nan]), kw), "within \\[0, 1\\]"),
        (lambda c, w, kw: (c, w.assign(A=[np.inf, np.nan, np.nan]), kw), "within \\[0, 1\\]"),
        (lambda c, w, kw: ({}, w, kw), "non-empty"),
        (lambda c, w, kw: (c, w, {**kw, "init_cash": 0.0}), "init_cash"),
        (lambda c, w, kw: (c, w, {**kw, "min_rebalance": float("nan")}), "min_rebalance"),
        (lambda c, w, kw: (c, w, {**kw, "costs": CostModel(fee_rate=-0.1)}), "fee_rate"),
        (
            lambda c, w, kw: (c, w, {**kw, "segment_starts": {"Z": c["A"].index[1:2]}}),
            "not in candles_by_symbol",
        ),
        (
            lambda c, w, kw: (c, w, {**kw, "segment_starts": {"A": c["A"].index[:1]}}),
            "first bar",
        ),
        (
            lambda c, w, kw: (
                c,
                w,
                {**kw, "segment_starts": {"A": pd.DatetimeIndex([START + pd.Timedelta("5D")])}},
            ),
            "not a bar",
        ),
        (
            lambda c, w, kw: ({**c, "A": c["A"].assign(close=[10.0, np.nan, 10.0])}, w, kw),
            "candles_by_symbol\\['A'\\]",
        ),
    ],
)
def test_invalid_inputs_raise_a_named_value_error(mutate: object, match: str) -> None:
    cands, w = _ok()
    c, w2, kw = mutate(cands, w, {"costs": ZERO_COSTS})  # type: ignore[operator]
    with pytest.raises(ValueError, match=match):
        run_portfolio(c, w2, **kw)


# --------------------------------------------------------------------------- no look-ahead


N_BARS = 200
SYMBOLS = ("AAA", "BBB", "CCC", "DDD")
VOL_COSTS = CostModel(
    fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.5, slippage_cap=0.01
)
SEG_ROW = 100  # DDD's new segment starts at its own row 100


def _universe() -> tuple[dict[str, pd.DataFrame], dict[str, pd.DatetimeIndex]]:
    cands: dict[str, pd.DataFrame] = {}
    for k, sym in enumerate(SYMBOLS):
        c = make_candles(N_BARS, seed=10 + k, interval="1d", start=f"2023-01-{1 + 3 * k:02d}")
        c.iloc[40 + 7 * k, c.columns.get_loc("tradable")] = False
        cands[sym] = c.drop(index=c.index[[60 + 5 * k, 61 + 5 * k]])  # a two-day hole
    cands["CCC"] = cands["CCC"].iloc[:150]  # delisted early
    return cands, {"DDD": cands["DDD"].index[SEG_ROW : SEG_ROW + 1]}


def _decisions(index: pd.DatetimeIndex, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    w = pd.DataFrame(np.nan, index=index, columns=list(SYMBOLS))
    for r in range(0, len(index), 5):
        picks = rng.random(len(SYMBOLS)) < 0.7
        w.iloc[r] = np.where(picks, rng.dirichlet(np.ones(len(SYMBOLS))) * 0.95, np.nan)
    return w


def _run(
    cands: Mapping[str, pd.DataFrame],
    master: pd.DataFrame,
    seg: Mapping[str, pd.DatetimeIndex],
) -> PortfolioResult:
    idx = union(cands)
    kept = {s: t[t.isin(cands[s].index)] for s, t in seg.items()}
    return run_portfolio(
        cands,
        master.reindex(idx),
        costs=VOL_COSTS,
        init_cash=CASH,
        min_rebalance=0.02,
        segment_starts=kept,
    )


def _assert_same_through(a: PortfolioResult, b: PortfolioResult, t_time: pd.Timestamp) -> None:
    for name in ("equity", "returns", "gross_returns", "turnover"):
        x, y = getattr(a, name).loc[:t_time], getattr(b, name).loc[:t_time]
        assert x.index.equals(y.index), name
        assert np.array_equal(x.to_numpy(), y.to_numpy()), name
    wa, wb = a.weights.loc[:t_time], b.weights.loc[:t_time]
    assert np.array_equal(wa.to_numpy(), wb.to_numpy())
    oa = a.orders[a.orders["time"] <= t_time].reset_index(drop=True)
    ob = b.orders[b.orders["time"] <= t_time].reset_index(drop=True)
    pd.testing.assert_frame_equal(oa, ob, check_exact=True)


@pytest.mark.parametrize("t", [30, 63, 110, 155, 180])
def test_no_lookahead_perturbing_prices_volumes_and_decisions_after_t(t: int) -> None:
    cands, seg = _universe()
    idx = union(cands)
    master = _decisions(idx, seed=1)
    t_time = idx[t]
    base = _run(cands, master, seg)

    perturbed = {
        s: perturb_after(c, int(c.index.searchsorted(t_time, side="right")) - 1, seed=7)
        for s, c in cands.items()
    }
    later = _decisions(idx, seed=2)
    mixed = master.copy()
    mixed.loc[mixed.index > t_time] = later.loc[later.index > t_time]
    _assert_same_through(base, _run(perturbed, mixed, seg), t_time)
    assert len(base.orders) > 10  # the strategy actually trades


@pytest.mark.parametrize("t", [30, 105, 155, 180])
def test_no_lookahead_truncating_the_data_after_t(t: int) -> None:
    cands, seg = _universe()
    idx = union(cands)
    master = _decisions(idx, seed=1)
    t_time = idx[t]
    # Preconditions (portfolio module docstring point 5, the one use of end-of-data knowledge):
    # no symbol is in a hole at t and no segment starts at t+1, else the full data would
    # force-exit at t's close where the truncated data cannot know it must.
    for c in cands.values():
        assert c.index[0] > t_time or c.index[-1] < t_time or t_time in c.index
    assert all(not (s > t_time).any() or s[s > t_time][0] > idx[t + 1] for s in seg.values())
    base = _run(cands, master, seg)

    cut = {s: c.loc[:t_time] for s, c in cands.items()}
    _assert_same_through(base, _run(cut, master, seg), t_time)


@pytest.mark.parametrize("t", [30, 110, 155])
def test_no_lookahead_truncating_or_holing_one_symbol_after_t(t: int) -> None:
    cands, seg = _universe()
    idx = union(cands)
    master = _decisions(idx, seed=1)
    t_time, t1 = idx[t], idx[t + 1]
    base = _run(cands, master, seg)

    # A symbol that stops printing after t+1 is force-exited at t+1's close, never earlier.
    for sym in ("AAA", "DDD"):
        cut = {**cands, sym: cands[sym].loc[:t1]}
        _assert_same_through(base, _run(cut, master, seg), t_time)
    # Bars deleted after t+1 (holes) change nothing through t.
    a = cands["BBB"]
    later = a.index[(a.index > t1)][::3][:-1]
    holed = {**cands, "BBB": a.drop(index=later)}
    _assert_same_through(base, _run(holed, master, seg), t_time)


# --------------------------------------------------------------------------- performance


def test_performance_200_symbols_by_1500_daily_bars() -> None:
    n, m = 1500, 200
    cands: dict[str, pd.DataFrame] = {}
    for k in range(m):
        start = (pd.Timestamp("2020-01-01") + pd.Timedelta(days=int(k * 3))).strftime("%Y-%m-%d")
        length = n - 3 * k if k % 4 else n // 2  # every 4th coin delists half-way
        c = make_candles(max(length, 60), seed=1000 + k, interval="1d", start=start, vol=0.04)
        cands[f"S{k:03d}"] = c
    idx = union(cands)
    closes = pd.DataFrame({s: c["close"] for s, c in cands.items()}).reindex(idx)
    mom = closes / closes.shift(28) - 1  # NaN when either end is missing
    w = pd.DataFrame(np.nan, index=idx, columns=sorted(cands))
    for r in range(28, len(idx), 7):
        row = mom.iloc[r].dropna()
        top = row.nlargest(20).index
        w.iloc[r] = 0.0
        w.loc[idx[r], top] = 1.0 / 20

    t0 = time.perf_counter()
    res = run_portfolio(cands, w, costs=VOL_COSTS, init_cash=CASH, min_rebalance=0.005)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"run_portfolio took {elapsed:.2f}s"
    assert np.isfinite(res.equity.to_numpy()).all() and (res.equity > 0).all()
    assert (res.orders["reason"] == "delisting").any()
    assert res.weights.sum(axis=1).max() <= 1 + 1e-9


def test_segment_exits_record_the_reopen_ratio() -> None:
    """A held coin force-closed before a segment break is listed with the next segment's first
    open over the exit close, so a report can flag a crash dodged during a suspension."""
    from lab.engine.portfolio import SEGMENT_EXIT_COLUMNS

    idx = pd.DatetimeIndex(
        [pd.Timestamp(f"2024-01-{d:02d}", tz="UTC") for d in (1, 2, 3, 7, 8)], name="time"
    )
    coin = pd.DataFrame(
        {
            "open": [10.0, 10.0, 10.0, 0.1, 0.1],
            "high": [10.0, 10.0, 10.0, 0.1, 0.1],
            "low": [10.0, 10.0, 10.0, 0.1, 0.1],
            "close": [10.0, 10.0, 10.0, 0.1, 0.1],
            "volume": 1.0,
            "tradable": True,
        },
        index=idx,
    )
    weights = pd.DataFrame({"XUSDT": [1.0, np.nan, np.nan, np.nan, np.nan]}, index=idx)
    zero = CostModel(fee_rate=0.0, slippage_rate=0.0, slippage_vol_mult=0.0, slippage_cap=0.0)
    res = run_portfolio({"XUSDT": coin}, weights, costs=zero, segment_starts={"XUSDT": idx[[3]]})
    assert list(res.segment_exits.columns) == list(SEGMENT_EXIT_COLUMNS)
    row = res.segment_exits.iloc[0]
    assert row["time"] == idx[2] and row["reopen_time"] == idx[3]
    assert row["exit_close"] == 10.0 and row["reopen_ratio"] == pytest.approx(0.01)
    plain = run_portfolio({"XUSDT": coin}, weights, costs=zero)
    assert plain.segment_exits.empty and list(plain.segment_exits.columns) == list(
        SEGMENT_EXIT_COLUMNS
    )


# --------------------------------------------------------------------------- participation impact

# coef 0.1, a 100k book, 1-day ADV (the previous bar's dollar volume) and 2-return sigma: the
# first fill with finite inputs is at row 3 (sigma needs returns at rows 1 and 2).
IMPACT = ImpactModel(coef=0.1, aum_usd=1e5, adv_days=1, vol_days=2, cap=0.03)
# Rows (open, close); volume 1000 on every bar, so dollar volume = 1000 * close.
IMPACT_ROWS = [(100, 100), (100, 110), (110, 99), (99, 100), (100, 100), (100, 100)]


def _impact_bars(n: int = 6) -> pd.DataFrame:
    frame = bars(IMPACT_ROWS[:n])
    frame["volume"] = 1000.0
    return frame


def test_impact_hand_example_one_buy_one_sell() -> None:
    cands = {"A": _impact_bars()}
    w = targets(cands, {2: {"A": 1.0}, 3: {"A": 0.0}})
    res = run_portfolio(cands, w, costs=COSTS, init_cash=CASH, impact=IMPACT)

    # Buy at row 3's open (99). Q = |delta| / equity_open * aum = 10000 / 10000 * 1e5 = 1e5.
    # ADV = dollar volume of row 2 = 99 * 1000 = 99000.
    # sigma = std(ddof=1) of r1 = log 1.1, r2 = log 0.9 -> log(1.1 / 0.9) / sqrt(2).
    sig3 = np.log(1.1 / 0.9) / np.sqrt(2)
    frac_b = 0.1 * sig3 * np.sqrt(1e5 / 99_000)  # ~0.01426, under the 0.03 cap
    fill_b = 99 * (1 + SLIP + frac_b)
    units = CASH / (fill_b * (1 + FEE))
    fee_b = units * fill_b * FEE
    eq3 = units * 100
    # Sell everything at row 4's open (100): Q = units * 100 / eq3 * 1e5 = 1e5,
    # ADV = row 3's 100 * 1000 = 1e5, sigma = std of r2 = log 0.9, r3 = log(100 / 99).
    sig4 = abs(np.log(0.9) - np.log(100 / 99)) / np.sqrt(2)
    frac_s = 0.1 * sig4 * np.sqrt(1e5 / 1e5)
    fill_s = 100 * (1 - SLIP - frac_s)
    fee_s = units * fill_s * FEE
    eq4 = units * fill_s - fee_s

    o = res.orders
    assert o[["side", "reason"]].values.tolist() == [["buy", "rebalance"], ["sell", "rebalance"]]
    assert list(o["time"]) == [w.index[3], w.index[4]]
    assert o["price"].tolist() == [approx(fill_b), approx(fill_s)]
    assert o["units"].tolist() == [approx(units), approx(units)]
    assert o["fee"].tolist() == [approx(fee_b), approx(fee_s)]
    assert res.equity.tolist() == [
        approx(CASH),
        approx(CASH),
        approx(CASH),
        approx(eq3),
        approx(eq4),
        approx(eq4),
    ]
    assert res.impact_cost == approx(units * 99 * frac_b + units * 100 * frac_s)
    # The gross run pays no impact (nor fees/slippage): the buy fills at 99, the sell at 100.
    assert res.gross_returns.iloc[4] == approx(0.0)
    assert res.gross_returns.iloc[3] == approx(100 / 99 - 1)


def test_impact_in_warm_up_charges_the_cap() -> None:
    cands = {"A": _impact_bars()}
    w = targets(cands, {0: {"A": 1.0}})
    res = run_portfolio(cands, w, costs=COSTS, init_cash=CASH, impact=IMPACT)
    # Row 1's open: no ADV (needs a full 1-day window before row 1) and no sigma -> cap 0.03.
    assert res.orders["price"].tolist() == [approx(100 * (1 + SLIP + 0.03))]
    units = CASH / (100 * (1 + SLIP + 0.03) * (1 + FEE))
    assert res.impact_cost == approx(units * 100 * 0.03)


def test_forced_exit_pays_impact_from_rows_before_the_exit_bar() -> None:
    # A stops printing after row 4 (delisted); B keeps the union going and is never held.
    cands = {"A": _impact_bars(5), "B": bars([(50, 50)] * 6)}
    w = targets(cands, {2: {"A": 1.0}})
    res = run_portfolio(cands, w, costs=COSTS, init_cash=CASH, impact=IMPACT)

    sig3 = np.log(1.1 / 0.9) / np.sqrt(2)
    frac_b = 0.1 * sig3 * np.sqrt(1e5 / 99_000)
    units = CASH / (99 * (1 + SLIP + frac_b) * (1 + FEE))
    # Exit at row 4's close (100). Q = units * 100 / equity_close * aum = 1e5 (the whole book),
    # with the same inputs as a fill at row 4's open: ADV = 1e5 (row 3), sigma from r2, r3.
    sig4 = abs(np.log(0.9) - np.log(100 / 99)) / np.sqrt(2)
    frac_x = 0.1 * sig4
    fill_x = 100 * (1 - SLIP - frac_x)
    exit_row = orders_at(res, 4)
    assert exit_row["reason"].tolist() == ["delisting"]
    assert exit_row["price"].tolist() == [approx(fill_x)]
    assert res.equity.iloc[4] == approx(units * fill_x * (1 - FEE))
    assert res.impact_cost == approx(units * 99 * frac_b + units * 100 * frac_x)


def test_zero_impact_model_is_bit_identical_to_no_impact() -> None:
    cands, seg = _universe()
    idx = union(cands)
    w = _decisions(idx, seed=1)
    kw = {"costs": VOL_COSTS, "init_cash": CASH, "min_rebalance": 0.02, "segment_starts": seg}
    plain = run_portfolio(cands, w, **kw)  # type: ignore[arg-type]
    zero = ImpactModel(coef=0.0, aum_usd=1e6, adv_days=5, vol_days=5, cap=0.0)
    free = run_portfolio(cands, w, impact=zero, **kw)  # type: ignore[arg-type]
    for name in ("equity", "returns", "gross_returns", "turnover"):
        assert np.array_equal(getattr(plain, name).to_numpy(), getattr(free, name).to_numpy())
    pd.testing.assert_frame_equal(plain.orders, free.orders, check_exact=True)
    assert plain.impact_cost == 0.0 and free.impact_cost == 0.0
    assert len(plain.orders) > 10


def _impact_universe_run(aum: float) -> PortfolioResult:
    cands, seg = _universe()
    w = _decisions(union(cands), seed=1)
    model = ImpactModel(coef=0.5, aum_usd=aum, adv_days=10, vol_days=10, cap=0.2)
    return run_portfolio(
        cands,
        w,
        costs=VOL_COSTS,
        init_cash=CASH,
        min_rebalance=0.02,
        segment_starts=seg,
        impact=model,
    )


def test_impact_is_monotone_in_book_size_and_never_touches_gross() -> None:
    cands, seg = _universe()
    w = _decisions(union(cands), seed=1)
    plain = run_portfolio(
        cands, w, costs=VOL_COSTS, init_cash=CASH, min_rebalance=0.02, segment_starts=seg
    )
    runs = [_impact_universe_run(aum) for aum in (1e2, 1e4, 1e6)]
    finals = [plain.equity.iloc[-1]] + [r.equity.iloc[-1] for r in runs]
    assert all(a > b for a, b in pairwise(finals)), finals
    assert 0 < runs[0].impact_cost < runs[1].impact_cost
    for r in runs:
        assert np.array_equal(r.gross_returns.to_numpy(), plain.gross_returns.to_numpy())


def test_net_returns_for_matches_run_portfolio_with_impact() -> None:
    cands, seg = _universe()
    w = _decisions(union(cands), seed=3)
    model = ImpactModel(coef=0.5, aum_usd=1e4, adv_days=10, vol_days=10, cap=0.2)
    kw = {"costs": VOL_COSTS, "init_cash": CASH, "min_rebalance": 0.02, "segment_starts": seg}
    full = run_portfolio(cands, w, impact=model, **kw)  # type: ignore[arg-type]
    (fast,) = net_returns_for(cands, [w], impact=model, **kw)  # type: ignore[arg-type]
    assert np.array_equal(fast.returns.to_numpy(), full.returns.to_numpy())
    assert np.array_equal(fast.turnover.to_numpy(), full.turnover.to_numpy())
    assert fast.impact_cost == full.impact_cost > 0


@pytest.mark.parametrize("t", [30, 63, 110, 155, 180])
def test_no_lookahead_with_impact_perturbing_after_t(t: int) -> None:
    cands, seg = _universe()
    idx = union(cands)
    master = _decisions(idx, seed=1)
    t_time = idx[t]
    model = ImpactModel(coef=0.5, aum_usd=1e4, adv_days=10, vol_days=10, cap=0.2)

    def run(c: Mapping[str, pd.DataFrame], wts: pd.DataFrame) -> PortfolioResult:
        kept = {s: v[v.isin(c[s].index)] for s, v in seg.items()}
        return run_portfolio(
            c,
            wts.reindex(union(c)),
            costs=VOL_COSTS,
            init_cash=CASH,
            min_rebalance=0.02,
            segment_starts=kept,
            impact=model,
        )

    base = run(cands, master)
    perturbed = {
        s: perturb_after(c, int(c.index.searchsorted(t_time, side="right")) - 1, seed=7)
        for s, c in cands.items()
    }
    later = _decisions(idx, seed=2)
    mixed = master.copy()
    mixed.loc[mixed.index > t_time] = later.loc[later.index > t_time]
    _assert_same_through(base, run(perturbed, mixed), t_time)
    assert base.impact_cost > 0


def test_rejects_a_bad_impact_model() -> None:
    cands, w = _ok()
    with pytest.raises(ValueError, match="impact"):
        run_portfolio(cands, w, costs=COSTS, impact="0.1")  # type: ignore[arg-type]
    bad = ImpactModel(coef=0.1, aum_usd=-1.0, adv_days=1, vol_days=2, cap=0.03)
    with pytest.raises(ValueError, match="aum_usd"):
        run_portfolio(cands, w, costs=COSTS, impact=bad)
    with pytest.raises(ValueError, match="cap"):
        net_returns_for(
            cands,
            [w],
            costs=COSTS,
            impact=ImpactModel(coef=0.1, aum_usd=1.0, adv_days=1, vol_days=2, cap=1.0),
        )


@pytest.mark.parametrize("entry", ["run_portfolio", "net_returns_for"])
def test_rejects_slippage_plus_impact_cap_of_one_or_more(entry: str) -> None:
    """Sells fill at open * (1 - slip - frac): at slip + frac >= 1 the price is <= 0."""
    cands, w = _ok()
    costs = CostModel(fee_rate=FEE, slippage_rate=SLIP, slippage_vol_mult=0.1, slippage_cap=0.5)
    impact = ImpactModel(coef=0.1, aum_usd=1.0, adv_days=1, vol_days=2, cap=0.5)

    def call(c: CostModel, im: ImpactModel) -> object:
        if entry == "run_portfolio":
            return run_portfolio(cands, w, costs=c, impact=im)
        return net_returns_for(cands, [w], costs=c, impact=im)

    with pytest.raises(ValueError, match=r"costs\.slippage_cap.*impact\.cap"):
        call(costs, impact)
    flat = CostModel(fee_rate=FEE, slippage_rate=0.6, slippage_vol_mult=0.0)
    with pytest.raises(ValueError, match=r"costs\.slippage_rate.*impact\.cap"):
        call(flat, ImpactModel(coef=0.1, aum_usd=1.0, adv_days=1, vol_days=2, cap=0.4))
    call(costs, ImpactModel(coef=0.1, aum_usd=1.0, adv_days=1, vol_days=2, cap=0.49))
    call(COSTS, impact)  # flat 1% slippage: 0.01 + 0.5 < 1 although slippage_cap is 1.0
