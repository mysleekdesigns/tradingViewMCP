"""Portfolio benchmarks: equal-weight universe, vol-targeted BTC on the portfolio calendar,
random-selection null. Offline, synthetic candles with explicit seeds."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab.engine.portfolio import net_returns_for, run_portfolio
from lab.evaluate.benchmarks import vol_target_buy_and_hold
from lab.evaluate.portfolio_benchmarks import (
    RandomSelectionNull,
    annual_turnover,
    btc_vol_target_returns,
    equal_weight_universe,
    equal_weight_weights,
    random_selection_null,
    random_selection_weights,
)
from lab.registry.cross_section import (
    close_panel,
    rebalance_rows,
    top_quantile_weights,
    trailing_return,
    trailing_volatility,
)
from lab.stats.metrics import metrics_from_returns
from lab.types import CostModel
from tests.synthetic import make_candles

COSTS = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.1, slippage_cap=0.01)


def coins(n: int = 4, bars: int = 120, drifts: tuple[float, ...] | None = None) -> dict:
    drifts = drifts or (0.0,) * n
    return {
        f"C{i}USDT": make_candles(bars, seed=i, interval="1d", start="2024-01-01", drift=drifts[i])
        for i in range(n)
    }


def panel_index(data: dict) -> pd.DatetimeIndex:
    idx = None
    for f in data.values():
        idx = f.index if idx is None else idx.union(f.index)
    return pd.DatetimeIndex(idx, name="time")


def all_members(data: dict) -> pd.DataFrame:
    idx = panel_index(data)
    return pd.DataFrame(
        {
            s: f["tradable"].reindex(idx, fill_value=False).astype(bool)
            for s, f in sorted(data.items())
        },
        index=idx,
    )


def weekly(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(np.arange(len(index)) % 7 == 0, index=index)


def test_equal_weight_weights_split_members_and_hold_off_schedule() -> None:
    idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC", name="time")
    members = pd.DataFrame(
        {"A": [True, True, False], "B": [True, False, False], "C": [False, True, False]}, index=idx
    )
    w = equal_weight_weights(members, pd.Series([True, False, True], index=idx))
    assert w.iloc[0].tolist() == [0.5, 0.5, 0.0]
    assert w.iloc[1].isna().all()
    assert w.iloc[2].tolist() == [0.0, 0.0, 0.0]  # nobody eligible: cash
    with pytest.raises(ValueError, match="bool frame"):
        equal_weight_weights(members.astype(float), pd.Series([True] * 3, index=idx))


def test_equal_weight_universe_is_run_portfolio_on_equal_weights() -> None:
    data = coins()
    members = all_members(data)
    sched = weekly(members.index)
    res = equal_weight_universe(data, members, sched, costs=COSTS)
    direct = run_portfolio(data, equal_weight_weights(members, sched), costs=COSTS)
    pd.testing.assert_series_equal(res.equity, direct.equity)
    assert res.fees_paid > 0


def test_net_returns_for_matches_run_portfolio_exactly() -> None:
    data = coins()
    members = all_members(data)
    w = equal_weight_weights(members, weekly(members.index))
    (fast,) = net_returns_for(data, [w], costs=COSTS)
    full = run_portfolio(data, w, costs=COSTS)
    pd.testing.assert_series_equal(fast.returns, full.returns)
    pd.testing.assert_series_equal(fast.turnover, full.turnover)


def test_btc_vol_target_returns_reindexed_to_calendar() -> None:
    btc = make_candles(80, seed=1, interval="1d", start="2024-01-01")
    late = make_candles(20, seed=2, interval="1d", start="2024-03-21")  # extends the calendar
    index = btc.index.union(late.index)
    kwargs = {"target_annual": 0.4, "lookback_days": 10, "max_leverage": 1.0}
    r = btc_vol_target_returns(btc, index, "1d", costs=COSTS, **kwargs)
    direct = vol_target_buy_and_hold(btc, "1d", costs=COSTS, **kwargs).returns
    assert r.index.equals(index)
    pd.testing.assert_series_equal(r.loc[btc.index], direct.rename("returns"))
    assert (r.loc[index.difference(btc.index)] == 0.0).all()
    with pytest.raises(ValueError, match="outside the portfolio index"):
        btc_vol_target_returns(btc, index[:-30], "1d", costs=COSTS, **kwargs)


def test_random_selection_matches_breadth_and_exposure() -> None:
    data = coins(6)
    members = all_members(data)
    members.iloc[:10, 5] = False  # C5 not yet eligible early on
    strat = pd.DataFrame(np.nan, index=members.index, columns=members.columns)
    strat.iloc[0] = [0.3, 0.3, 0.0, 0.0, 0.0, 0.0]  # 2 names, 60% invested
    strat.iloc[7] = [0.0] * 6  # all cash
    path = random_selection_weights(strat, members, np.random.default_rng(3))
    row0 = path.iloc[0]
    assert (row0 > 0).sum() == 2 and row0.sum() == pytest.approx(0.6)
    assert row0["C5USDT"] == 0.0  # only members are drawn
    assert (path.iloc[7] == 0.0).all()
    assert path.drop(path.index[[0, 7]]).isna().all().all()
    again = random_selection_weights(strat, members, np.random.default_rng(3))
    pd.testing.assert_frame_equal(path, again)


def test_random_selection_null_is_seeded_and_ranks_an_oracle_pick() -> None:
    data = coins(6, bars=200, drifts=(0.004, 0.0, 0.0, 0.0, 0.0, 0.0))  # C0 trends up
    members = all_members(data)
    sched = weekly(members.index)
    strat = pd.DataFrame(np.nan, index=members.index, columns=members.columns)
    strat.loc[sched.to_numpy()] = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    null = random_selection_null(data, strat, members, "1d", costs=COSTS, n_paths=40, seed=11)
    again = random_selection_null(data, strat, members, "1d", costs=COSTS, n_paths=40, seed=11)
    np.testing.assert_array_equal(null.sharpe, again.sharpe)
    assert null.n_paths == 40 and null.sharpe.shape == (40,)
    strat_sharpe = metrics_from_returns(
        run_portfolio(data, strat, costs=COSTS).returns, "1d"
    ).sharpe
    # The strategy keeps its one name every week, so a turnover-matched null buys one random
    # coin and holds it: about 1 path in 6 holds C0 and ties the oracle exactly. No path may
    # beat it, and p is about the share of ties.
    assert np.nanmax(null.sharpe) <= strat_sharpe + 1e-9
    ties = float(np.mean(np.isclose(null.sharpe, strat_sharpe)))
    assert null.p_value(strat_sharpe) == pytest.approx((ties * 40 + 1) / 41)
    assert ties < 0.35


def test_p_value_counts_the_strategy_and_ignores_nan_paths() -> None:
    null = RandomSelectionNull(
        sharpe=np.array([0.5, 1.5, np.nan, 2.0]),
        total_return=np.zeros(4),
        turnover=np.zeros(4),
        n_paths=4,
        seed=0,
    )
    assert null.p_value(1.0) == (2 + 1) / 5
    assert null.p_value(3.0) == 1 / 5
    assert np.isnan(null.p_value(float("nan")))


def test_null_rejects_bad_inputs() -> None:
    data = coins()
    members = all_members(data)
    strat = equal_weight_weights(members, weekly(members.index))
    with pytest.raises(ValueError, match="n_paths"):
        random_selection_null(data, strat, members, "1d", costs=COSTS, n_paths=0, seed=1)
    with pytest.raises(ValueError, match="same index and columns"):
        random_selection_null(
            data, strat, members.iloc[:, ::-1], "1d", costs=COSTS, n_paths=2, seed=1
        )


def test_null_keeps_as_many_of_its_own_names_as_the_strategy_kept() -> None:
    data = coins(8)
    members = all_members(data)
    strat = pd.DataFrame(np.nan, index=members.index, columns=members.columns)
    strat.iloc[0] = [0.25, 0.25, 0.25, 0.25, 0, 0, 0, 0]
    strat.iloc[7] = [0.25, 0.25, 0.25, 0, 0.25, 0, 0, 0]  # kept 3 of 4
    strat.iloc[14] = [0, 0, 0, 0, 0, 0.25, 0.25, 0.25]  # kept 0, now 3 names
    for seed in range(20):
        path = random_selection_weights(strat, members, np.random.default_rng(seed))
        a, b, c = (set(np.flatnonzero(path.iloc[i] > 0)) for i in (0, 7, 14))
        assert len(a) == len(b) == 4 and len(c) == 3
        assert len(a & b) == 3  # same retention as the strategy
        assert len(b & c) == 0  # strategy replaced everything, so does the null
        assert path.iloc[14].sum() == pytest.approx(0.75)


def test_null_uses_inverse_vol_weights_and_skips_unsizeable_names() -> None:
    data = coins(4)
    members = all_members(data)
    vol = pd.DataFrame(0.02, index=members.index, columns=members.columns)
    vol.iloc[:, 1] = 0.04
    vol.iloc[:, 3] = np.nan  # cannot be sized: never drawn
    strat = pd.DataFrame(np.nan, index=members.index, columns=members.columns)
    strat.iloc[0] = [0.5, 0.5, 0.0, 0.0]
    for seed in range(10):
        path = random_selection_weights(
            strat, members, np.random.default_rng(seed), inverse_vol=vol
        )
        row = path.iloc[0]
        assert row.iloc[3] == 0.0 and row.sum() == pytest.approx(1.0)
        if row.iloc[1] > 0 and row.iloc[0] > 0:
            assert row.iloc[0] == pytest.approx(2 * row.iloc[1])  # 1/0.02 vs 1/0.04


def no_skill_momentum(seed: int) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    data = {
        f"C{i:02d}USDT": make_candles(
            400, seed=seed * 100 + i, interval="1d", start="2023-01-01", vol=0.03
        )
        for i in range(20)
    }
    closes = close_panel(data)
    members = closes.notna()
    schedule = rebalance_rows(closes.index, every=7, offset=28)
    weights = top_quantile_weights(trailing_return(closes, 28), members, schedule, quantile=0.2)
    return data, members, weights


def test_null_is_calibrated_and_turnover_matched_for_a_no_skill_strategy() -> None:
    """iid driftless coins: weekly 28-day momentum has no skill, so its p-values against the
    null should be roughly uniform. A null that redraws every name weekly trades ~2x more,
    pays ~2x the costs and pushes p-values toward 0 (reviewer finding C1)."""
    pvals, strat_turn, null_turn = [], [], []
    for seed in range(24):
        data, members, weights = no_skill_momentum(seed)
        res = run_portfolio(data, weights, costs=COSTS)
        null = random_selection_null(
            data, weights, members, "1d", costs=COSTS, n_paths=20, seed=seed
        )
        pvals.append(null.p_value(metrics_from_returns(res.returns, "1d").sharpe))
        strat_turn.append(annual_turnover(res.turnover, "1d"))
        null_turn.append(float(np.mean(null.turnover)))
    ratio = np.mean(null_turn) / np.mean(strat_turn)
    assert 0.85 < ratio < 1.15, ratio
    p = np.array(pvals)
    assert 0.3 < p.mean() < 0.7, p.mean()
    assert (p <= 0.1).mean() <= 0.25, p


def test_fresh_null_names_come_from_the_strategy_fresh_names_vol_terciles() -> None:
    data = coins(9)
    members = all_members(data)
    vol = pd.DataFrame(
        np.tile(np.arange(1, 10, dtype=float) / 100, (len(members), 1)),
        index=members.index,
        columns=members.columns,
    )  # C0..C2 low, C3..C5 mid, C6..C8 high tercile
    strat = pd.DataFrame(np.nan, index=members.index, columns=members.columns)
    strat.iloc[0] = [0, 0, 0, 0, 0, 0, 0.5, 0.5, 0]  # two fresh high-vol names
    for seed in range(20):
        path = random_selection_weights(strat, members, np.random.default_rng(seed), vol_panel=vol)
        picked = set(np.flatnonzero(path.iloc[0] > 0))
        assert len(picked) == 2 and picked <= {6, 7, 8}
        assert path.iloc[0].sum() == pytest.approx(1.0)  # equal weights: vol_panel only buckets


def test_vol_matched_null_is_calibrated_when_momentum_tilts_to_volatile_coins() -> None:
    """Driftless log returns with volatilities from 1% to 7% a day: momentum tilts toward the
    volatile coins, whose arithmetic mean is higher, so a vol-blind null credits that tilt as
    skill (reviewer finding). Drawing fresh names by volatility tercile keeps the tails near
    nominal (probe: 60 seeds x 30 paths, p<=0.05 in 1.7%, p<=0.10 in 11.7%, mean p 0.44)."""
    vols = np.linspace(0.01, 0.07, 20)
    pvals = []
    for seed in range(24):
        data = {
            f"C{i:02d}USDT": make_candles(
                500, seed=seed * 100 + i, interval="1d", start="2023-01-01", vol=float(vols[i])
            )
            for i in range(20)
        }
        closes = close_panel(data)
        members = closes.notna()
        schedule = rebalance_rows(closes.index, every=7, offset=28)
        w = top_quantile_weights(trailing_return(closes, 28), members, schedule, quantile=0.2)
        sharpe = metrics_from_returns(run_portfolio(data, w, costs=COSTS).returns, "1d").sharpe
        null = random_selection_null(
            data,
            w,
            members,
            "1d",
            costs=COSTS,
            n_paths=20,
            seed=seed,
            vol_panel=trailing_volatility(closes, 28),
        )
        pvals.append(null.p_value(sharpe))
    p = np.array(pvals)
    assert p.mean() > 0.3, p.mean()
    assert (p <= 0.1).mean() <= 0.25, p
