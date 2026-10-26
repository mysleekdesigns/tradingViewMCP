"""Portfolio benchmarks: equal-weight universe, vol-targeted BTC on the portfolio calendar,
random-selection null. Offline, synthetic candles with explicit seeds."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

import numpy as np
import pandas as pd
import pytest

import lab.evaluate.portfolio_benchmarks as pb
from lab.data.universe import trailing_dollar_volume
from lab.engine.costs import ImpactModel
from lab.engine.portfolio import net_returns_for, run_portfolio
from lab.evaluate.benchmarks import vol_target_buy_and_hold
from lab.evaluate.portfolio_benchmarks import (
    MATCH_LEVELS,
    RANK_MATCH_KEYS,
    RandomSelectionNull,
    _draw_fresh,
    _GapAcc,
    _liquidity_ranks,
    _match_nearest,
    _RankMatch,
    _retain,
    _tercile_buckets,
    annual_turnover,
    btc_vol_target_returns,
    equal_weight_universe,
    equal_weight_weights,
    mean_book_mix,
    mean_path_stat,
    random_selection_null,
    random_selection_path,
    random_selection_weights,
    sum_kept_match_fallbacks,
    sum_match_fallbacks,
)
from lab.registry.cross_section import (
    close_panel,
    liquidity_share,
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


IMPACT = ImpactModel(coef=0.5, aum_usd=1e5, adv_days=10, vol_days=10, cap=0.05)


def test_equal_weight_universe_passes_impact_through() -> None:
    data = coins()
    members = all_members(data)
    sched = weekly(members.index)
    plain = equal_weight_universe(data, members, sched, costs=COSTS)
    hit = equal_weight_universe(data, members, sched, costs=COSTS, impact=IMPACT)
    direct = run_portfolio(data, equal_weight_weights(members, sched), costs=COSTS, impact=IMPACT)
    pd.testing.assert_series_equal(hit.equity, direct.equity)
    assert hit.impact_cost == direct.impact_cost > 0
    assert plain.impact_cost == 0.0
    assert hit.equity.iloc[-1] / hit.init_cash < plain.equity.iloc[-1] / plain.init_cash
    pd.testing.assert_series_equal(hit.gross_returns, plain.gross_returns)


def test_random_selection_null_pays_the_same_impact_as_the_strategy() -> None:
    data = coins(n=6)
    members = all_members(data)
    w = pd.DataFrame(np.nan, index=members.index, columns=members.columns)
    w.loc[weekly(members.index).to_numpy()] = [1 / 3, 1 / 3, 1 / 3, 0.0, 0.0, 0.0]
    kw = {"costs": COSTS, "n_paths": 5, "seed": 11}
    plain = random_selection_null(data, w, members, "1d", **kw)  # type: ignore[arg-type]
    hit = random_selection_null(data, w, members, "1d", impact=IMPACT, **kw)  # type: ignore[arg-type]
    # Same seed -> same paths; impact only lowers every path's return.
    assert np.all(hit.total_return < plain.total_return)
    paths = [random_selection_weights(w, members, np.random.default_rng(11)) for _ in range(1)]
    (first,) = net_returns_for(data, paths, costs=COSTS, impact=IMPACT)
    assert metrics_from_returns(first.returns, "1d").total_return == pytest.approx(
        hit.total_return[0], rel=1e-12
    )


# --------------------------------------------------------------------------- liquidity cap


def frame(index: pd.DatetimeIndex, columns: pd.Index, rows: dict[int, list[float]]) -> pd.DataFrame:
    out = pd.DataFrame(np.nan, index=index, columns=columns)
    for i, vals in rows.items():
        out.iloc[i] = vals
    return out


def test_capped_equal_weight_splits_sizable_members_and_keeps_the_excess_in_cash() -> None:
    idx = pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC", name="time")
    members = pd.DataFrame({c: [True, True] for c in "ABCD"}, index=idx)
    share = pd.DataFrame(
        {"A": [0.5, 0.5], "B": [0.3, 0.3], "C": [0.2, 0.2], "D": [np.nan, np.nan]}, index=idx
    )
    sched = pd.Series([True, False], index=idx)
    w = equal_weight_weights(members, sched, liquidity_share=share, liquidity_cap_mult=1.0)
    # k = 3 (D has no share), 1/3 each, capped at 1.0 * share; 1/6 stays in cash.
    np.testing.assert_allclose(w.iloc[0].to_numpy(), [1 / 3, 0.3, 0.2, 0.0], rtol=1e-15)
    assert w.iloc[0].sum() == pytest.approx(5 / 6)
    assert w.iloc[1].isna().all()
    loose = equal_weight_weights(members, sched, liquidity_share=share, liquidity_cap_mult=1e6)
    np.testing.assert_allclose(loose.iloc[0].to_numpy(), [1 / 3, 1 / 3, 1 / 3, 0.0])


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"liquidity_cap_mult": 2.0}, "together"),
        ({"share": True}, "together"),
        ({"share": True, "liquidity_cap_mult": 0.0}, "liquidity_cap_mult"),
        ({"share": True, "liquidity_cap_mult": float("inf")}, "liquidity_cap_mult"),
        ({"share": True, "liquidity_cap_mult": True}, "liquidity_cap_mult"),
        ({"share": "short", "liquidity_cap_mult": 2.0}, "same index and columns"),
    ],
)
def test_liquidity_cap_inputs_are_validated_everywhere(kwargs: dict, match: str) -> None:
    data = coins()
    members = all_members(data)
    sched = weekly(members.index)
    share = pd.DataFrame(0.25, index=members.index, columns=members.columns)
    kw = dict(kwargs)
    marker = kw.pop("share", None)
    if marker is True:
        kw["liquidity_share"] = share
    elif marker == "short":
        kw["liquidity_share"] = share.iloc[:-1]
    strat = equal_weight_weights(members, sched)
    with pytest.raises(ValueError, match=match):
        equal_weight_weights(members, sched, **kw)
    with pytest.raises(ValueError, match=match):
        equal_weight_universe(data, members, sched, costs=COSTS, **kw)
    with pytest.raises(ValueError, match=match):
        random_selection_weights(strat, members, np.random.default_rng(0), **kw)
    with pytest.raises(ValueError, match=match):
        random_selection_null(data, strat, members, "1d", costs=COSTS, n_paths=2, seed=0, **kw)


def test_capped_equal_weight_universe_runs_the_capped_weights() -> None:
    data = coins()
    members = all_members(data)
    sched = weekly(members.index)
    share = pd.DataFrame(
        np.tile([0.6, 0.25, 0.1, 0.05], (len(members), 1)),
        index=members.index,
        columns=members.columns,
    )
    kw = {"liquidity_share": share, "liquidity_cap_mult": 2.0}
    res = equal_weight_universe(data, members, sched, costs=COSTS, impact=IMPACT, **kw)
    w = equal_weight_weights(members, sched, **kw)
    direct = run_portfolio(data, w, costs=COSTS, impact=IMPACT)
    pd.testing.assert_series_equal(res.equity, direct.equity)
    # caps 1.2, 0.5, 0.2, 0.1 against 1/4: the two small coins bind, 20% stays in cash
    assert w.loc[sched].sum(axis=1).to_numpy() == pytest.approx(0.8)
    plain = equal_weight_universe(data, members, sched, costs=COSTS, impact=IMPACT)
    assert res.impact_cost < plain.impact_cost


LIQ = np.array([0.4, 0.3, 0.15, 0.1, 0.05, np.nan])  # C5 has no share: never drawn


def six_coin_case() -> tuple[pd.DataFrame, pd.DataFrame]:
    data = coins(6)
    members = all_members(data)
    share = pd.DataFrame(
        np.tile(LIQ, (len(members), 1)), index=members.index, columns=members.columns
    )
    return members, share


def test_capped_null_draws_sizable_names_and_caps_each_pick_like_the_strategy() -> None:
    members, share = six_coin_case()
    strat = frame(members.index, members.columns, {0: [0, 0, 0, 0.1, 0.05, 0], 7: [0.0] * 6})
    # The strategy's two picks (C3, C4) are both capped (1/2 > 1.0 * share): pre-cap T = 1.
    for seed in range(30):
        path = random_selection_weights(
            strat,
            members,
            np.random.default_rng(seed),
            liquidity_share=share,
            liquidity_cap_mult=1.0,
        )
        row = path.iloc[0].to_numpy()
        picks = np.flatnonzero(row > 0)
        assert len(picks) == 2 and 5 not in picks
        np.testing.assert_array_equal(row[picks], np.minimum(0.5, LIQ[picks]))
        assert row.sum() < 1.0  # the excess over the caps stays in cash
        assert (path.iloc[7] == 0.0).all()


def test_capped_null_reads_the_pre_cap_total_from_the_uncapped_names() -> None:
    members, share = six_coin_case()
    # 60% invested before caps: C0 uncapped at 0.3 (< 0.4), C4 capped at 0.05.
    strat = frame(members.index, members.columns, {0: [0.3, 0, 0, 0, 0.05, 0]})
    seen = set()
    for seed in range(30):
        path = random_selection_weights(
            strat,
            members,
            np.random.default_rng(seed),
            liquidity_share=share,
            liquidity_cap_mult=1.0,
        )
        row = path.iloc[0].to_numpy()
        picks = np.flatnonzero(row > 0)
        seen.update(picks.tolist())
        np.testing.assert_allclose(row[picks], np.minimum(0.3, LIQ[picks]), rtol=1e-12)
    assert seen == {0, 1, 2, 3, 4}


def test_capped_null_with_inverse_vol_reads_the_pre_cap_total() -> None:
    members, share = six_coin_case()
    vol = pd.DataFrame(0.02, index=members.index, columns=members.columns)
    vol.iloc[:, 1] = 0.04
    inv = np.array([50.0, 25.0])
    pre = 0.9 * inv / inv.sum()  # [0.6, 0.3]: under the caps 0.8 and 0.6 (mult 2)
    strat = frame(members.index, members.columns, {0: [pre[0], pre[1], 0, 0, 0, 0]})
    for seed in range(30):
        path = random_selection_weights(
            strat,
            members,
            np.random.default_rng(seed),
            inverse_vol=vol,
            liquidity_share=share,
            liquidity_cap_mult=2.0,
        )
        row = path.iloc[0].to_numpy()
        picks = np.flatnonzero(row > 0)
        raw = 1.0 / vol.iloc[0].to_numpy()[picks]
        expect = np.minimum(0.9 * raw / raw.sum(), 2.0 * LIQ[picks])
        np.testing.assert_allclose(row[picks], expect, rtol=1e-12)


def test_null_with_a_cap_that_never_binds_matches_the_uncapped_null() -> None:
    data = coins(6)
    members = all_members(data)
    share = pd.DataFrame(1 / 6, index=members.index, columns=members.columns)
    strat = equal_weight_weights(members, weekly(members.index))
    strat.loc[strat.notna().all(axis=1)] = [0.5, 0.5, 0.0, 0.0, 0.0, 0.0]
    a = random_selection_weights(strat, members, np.random.default_rng(4))
    b = random_selection_weights(
        strat, members, np.random.default_rng(4), liquidity_share=share, liquidity_cap_mult=1e6
    )
    pd.testing.assert_frame_equal(a, b, rtol=1e-12)


def test_null_reports_strategy_and_path_exposure() -> None:
    members, share = six_coin_case()
    data = coins(6)
    strat = frame(
        members.index,
        members.columns,
        {0: [0, 0, 0, 0.1, 0.05, 0], 7: [0.25, 0.25, 0, 0, 0, 0], 14: [0.0] * 6},
    )
    kw = {"costs": COSTS, "n_paths": 6, "seed": 3}
    plain = random_selection_null(data, strat, members, "1d", **kw)  # type: ignore[arg-type]
    assert plain.strategy_exposure == pytest.approx((0.15 + 0.5 + 0.0) / 3)
    # Without a cap the null asks for the strategy's row sums exactly.
    np.testing.assert_allclose(plain.exposure, plain.strategy_exposure)
    capped = random_selection_null(
        data, strat, members, "1d", liquidity_share=share, liquidity_cap_mult=1.0, **kw
    )  # type: ignore[arg-type]
    assert capped.exposure.shape == (6,)
    assert capped.strategy_exposure == plain.strategy_exposure
    # Row 0: T = 1 (both capped) -> two picks at min(0.5, share). Row 7: T = 0.5 (uncapped
    # 0.25 each) -> two picks at min(0.25, share). Exposure follows the names drawn.
    paths = [
        random_selection_weights(
            strat,
            members,
            np.random.default_rng(3),
            liquidity_share=share,
            liquidity_cap_mult=1.0,
        )
    ]
    first = paths[0].iloc[[0, 7, 14]].sum(axis=1).mean()
    assert capped.exposure[0] == pytest.approx(first, rel=1e-12)
    default = RandomSelectionNull(np.zeros(1), np.zeros(1), np.zeros(1), n_paths=1, seed=0)
    assert default.exposure.size == 0 and np.isnan(default.strategy_exposure)


def capped_no_skill(
    seed: int, n_coins: int, bars: int, mult: float = 2.0
) -> tuple[dict, pd.DataFrame, pd.DataFrame, dict]:
    """Driftless iid coins whose dollar volume is log-spaced from $5M to $2B a day (so the
    cap binds on the small ones), 28-day momentum top quintile, weekly, liquidity-capped."""
    rng = np.random.default_rng(seed)
    adv = np.exp(np.linspace(np.log(5e6), np.log(2e9), n_coins))
    data = {}
    for k in range(n_coins):
        c = make_candles(bars, seed=seed * 100 + k, interval="1d", start="2023-01-01", vol=0.04)
        c["volume"] = adv[k] / c["close"] * rng.uniform(0.7, 1.3, len(c))
        data[f"C{k:02d}USDT"] = c
    closes = close_panel(data)
    members = closes.notna() & (np.arange(len(closes))[:, None] >= 40)
    dv = trailing_dollar_volume(data, window_days=30)[closes.columns]
    kw = {"liquidity_share": liquidity_share(dv, members), "liquidity_cap_mult": mult}
    sched = rebalance_rows(closes.index, every=7, offset=40)
    w = top_quantile_weights(trailing_return(closes, 28), members, sched, quantile=0.2, **kw)
    return data, members, w, kw


# A $50M book: with the old null (cap ignored, the strategy's post-cap total spread equally)
# the fast test below gave mean p 0.21 and 6 of 24 seeds at p <= 0.05; with the fix 0.42 and 0.
CAL_IMPACT = ImpactModel(coef=1.0, aum_usd=5e7, adv_days=30, vol_days=30, cap=0.05)


def capped_p_values(
    seeds: range, n_coins: int, bars: int, n_paths: int, *, runner_call: bool = False
) -> np.ndarray:
    pvals = []
    for seed in seeds:
        data, members, w, kw = capped_no_skill(seed, n_coins, bars)
        if runner_call:  # the portfolio runner's null: vol and dollar-volume terciles too
            closes = close_panel(data)
            kw = {
                **kw,
                "vol_panel": trailing_volatility(closes, 28),
                "liquidity_panel": trailing_dollar_volume(data, window_days=30)[closes.columns],
            }
        res = run_portfolio(data, w, costs=COSTS, impact=CAL_IMPACT)
        null = random_selection_null(
            data,
            w,
            members,
            "1d",
            costs=COSTS,
            n_paths=n_paths,
            seed=seed,
            impact=CAL_IMPACT,
            **kw,
        )
        pvals.append(null.p_value(metrics_from_returns(res.returns, "1d").sharpe))
    return np.array(pvals)


def test_cap_binds_in_the_null_and_in_equal_weight() -> None:
    _, members, w, kw = capped_no_skill(0, n_coins=20, bars=200)
    share = kw["liquidity_share"].to_numpy()
    sched = w.notna().any(axis=1).to_numpy()
    assert w.loc[sched].sum(axis=1).max() < 1.0  # the strategy itself is capped
    path = random_selection_weights(w, members, np.random.default_rng(1), **kw)
    p = path.to_numpy()[sched]
    k = (w.to_numpy()[sched] > 0).sum(axis=1)
    held = p > 0
    assert (held.sum(axis=1) == k).all()  # breadth matched
    limit = np.minimum(1.0 / k[:, None], 2.0 * share[sched])
    np.testing.assert_allclose(p[held], limit[held], rtol=1e-12)  # T = 1, every pick capped
    assert (p[held] < 1.0 / np.broadcast_to(k[:, None], p.shape)[held]).mean() > 0.5
    ew = equal_weight_weights(members, pd.Series(sched, index=members.index), **kw)
    assert ew.loc[sched].sum(axis=1).max() < 1.0


def test_capped_null_is_calibrated_for_a_no_skill_strategy_with_impact() -> None:
    """Reviewer finding: a null that spread the capped strategy's total equally over random
    coins held illiquid coins at full weight and paid far more impact, so no-skill capped
    momentum came out significant (probe: mean p 0.22, 5 of 12 seeds p <= 0.05). Replicating the
    strategy's cap on the random names restores calibration. Fast version; the slow one runs
    more seeds and paths."""
    p = capped_p_values(range(24), n_coins=20, bars=300, n_paths=20)
    assert 0.35 < p.mean() < 0.65, p
    # 20 paths: p <= 0.05 only when no path beats the strategy, chance 1/21 per seed; 24
    # seeds expect ~1.1 and P(>= 5) is under 1%.
    assert (p <= 0.05).sum() <= 4, p
    assert (p <= 0.10).sum() <= 6, p  # chance 2/21 per seed, ~2.3 expected


@pytest.mark.slow
def test_capped_null_is_calibrated_for_a_no_skill_strategy_with_impact_slow() -> None:
    p = capped_p_values(range(80), n_coins=30, bars=600, n_paths=40)
    assert 0.4 < p.mean() < 0.6, p
    # 40 paths: chance of p <= 0.05 is 2/41 per seed; 80 seeds expect ~3.9, P(>= 10) < 1%.
    assert (p <= 0.05).sum() <= 9, p
    assert (p <= 0.10).mean() <= 0.17, p


# --------------------------------------------------------------------------- liquidity terciles


def _digest(df: pd.DataFrame) -> str:
    a = np.round(np.nan_to_num(df.to_numpy(dtype=float), nan=-1.0), 12) + 0.0
    return hashlib.sha256(a.tobytes()).hexdigest()[:16]


def test_default_off_null_is_bit_identical_to_before_liquidity_matching() -> None:
    """Digests recorded from the null as committed at a5fa079 (before ``liquidity_panel``):
    without a liquidity panel every path, RNG draw included, is unchanged."""
    data, members, w, kw = capped_no_skill(0, n_coins=20, bars=200)
    vol = trailing_volatility(close_panel(data), 28)
    cases = {
        "plain": ({}, "e62f7085c7c5956b"),
        "vol": ({"vol_panel": vol}, "356da93f2e2801e2"),
        "capped_inv_vol": ({"inverse_vol": vol, "max_weight": 0.3, **kw}, "e610b9c16941903d"),
    }
    for name, (extra, expected) in cases.items():
        path = random_selection_weights(w, members, np.random.default_rng(5), **extra)
        assert _digest(path) == expected, name
        off = random_selection_weights(
            w, members, np.random.default_rng(5), liquidity_panel=None, **extra
        )
        pd.testing.assert_frame_equal(path, off)


def nine_coins() -> tuple[pd.DataFrame, pd.DataFrame]:
    data = coins(9)
    members = all_members(data)
    dollar = pd.DataFrame(
        np.tile(np.arange(1, 10, dtype=float) * 1e6, (len(members), 1)),
        index=members.index,
        columns=members.columns,
    )  # C0..C2 least liquid tercile, C3..C5 middle, C6..C8 most liquid
    return members, dollar


def test_fresh_null_names_come_from_the_rank_band_around_each_strategy_name() -> None:
    """Nine coins ranked 0..8 by liquidity; the strategy holds C0 and C4. With the default
    band of 5 ranks (+/-2, clipped at the ends) the null's names are one from {0, 1, 2} (C0's
    band) and one from {2, ..., 6} (C4's), and over seeds every name of each band is drawn."""
    members, dollar = nine_coins()
    strat = frame(members.index, members.columns, {0: [0.5, 0, 0, 0, 0.5, 0, 0, 0, 0]})
    seen: set[int] = set()
    for seed in range(60):
        res = random_selection_path(
            strat, members, np.random.default_rng(seed), liquidity_panel=dollar
        )
        picked = set(np.flatnonzero(res.weights.iloc[0] > 0).tolist())
        assert len(picked) == 2 and picked <= set(range(7))
        assert picked & {0, 1, 2}, picked  # C0's band (C4's band can take C2 as well)
        assert len(picked & {2, 3, 4, 5, 6}) >= 1, picked
        assert res.match_fallbacks["rank"] == 2 and res.match_fallbacks["pool"] == 0
        assert res.match_fallbacks["widened"] == 0
        seen |= picked
    assert seen == set(range(7))
    # A share panel ranks the same as the dollar volume behind it: same draws.
    share = dollar.div(dollar.sum(axis=1), axis=0)
    a = random_selection_weights(strat, members, np.random.default_rng(1), liquidity_panel=dollar)
    b = random_selection_weights(strat, members, np.random.default_rng(1), liquidity_panel=share)
    pd.testing.assert_frame_equal(a, b)


def eighteen_coin_panels() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Vol tercile = c // 6, liquidity tercile = c % 3: every joint bucket holds two coins
    ({0, 3} is (0, 0), {7, 10} is (1, 1), {14, 17} is (2, 2), ...)."""
    data = coins(18, bars=30)
    members = all_members(data)
    c = np.arange(18, dtype=float)

    def tile(row: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame(
            np.tile(row, (len(members), 1)), index=members.index, columns=members.columns
        )

    return members, tile(c / 100), tile((c % 3) * 100 + c)


def test_within_the_band_the_null_prefers_the_vol_tercile_only_with_two_other_names() -> None:
    """Eighteen coins, vol tercile c // 6; liquidity ranks 0..5 are C0, C3, ..., C15, ranks
    6..11 are C1, C4, ..., C16 and ranks 12..17 are C2, C5, ..., C17. The strategy holds C0
    (rank 0, vol 0), C7 (rank 8, vol 1) and C14 (rank 16, vol 2).

    Default band of 5 ranks: C0's band {C0, C3, C6} holds one vol-0 name besides C0, C7's
    {C1, C4, C7, C10, C13} one vol-1 name besides C7, C14's {C8, C11, C14, C17} one vol-2 name
    besides C14. Restricting to the tercile would leave the target and one other name, so the
    null would copy the strategy half the time (package H4): the preference is skipped
    (``vol_thin``) and every band name is drawn, other terciles included.

    Band of 13 ranks: C0's band (ranks 0..6) holds C1 and C3 in vol 0 besides C0, C7's (ranks
    2..14) C6, C8, C9 and C10 in vol 1, C14's (ranks 10..17) C13, C16 and C17 in vol 2, so the
    draw keeps to the tercile (``vol_met``)."""
    members, vol, dollar = eighteen_coin_panels()
    row = [0.0] * 18
    for c in (0, 7, 14):
        row[c] = 1 / 3
    strat = frame(members.index, members.columns, {0: row})
    for window, groups, level in (
        (5, ({0, 3, 6}, {1, 4, 7, 10, 13}, {8, 11, 14, 17}), "vol_thin"),
        (13, ({0, 1, 3}, {6, 7, 8, 9, 10}, {13, 14, 16, 17}), "vol_met"),
    ):
        seen: set[int] = set()
        for seed in range(60):
            res = random_selection_path(
                strat,
                members,
                np.random.default_rng(seed),
                vol_panel=vol,
                liquidity_panel=dollar,
                liquidity_window=window,
            )
            picked = set(np.flatnonzero(res.weights.iloc[0] > 0).tolist())
            assert all(len(picked & g) == 1 for g in groups), (window, picked)
            fb = res.match_fallbacks
            assert (fb["rank"], fb["pool"], fb[level]) == (3, 0, 3), (window, fb)
            seen |= picked
        assert seen == set().union(*groups), (window, seen)


def test_match_nearest_band_widen_narrow_vol_and_pool() -> None:
    """``_match_nearest`` on hand-made ranks (C0..C7 ranked 0..7, C8 unranked): the band is
    the free names within +/-(window // 2) ranks; an empty band widens to the nearest free
    rank(s); the volatility tercile is preferred inside the band only, and only when the band
    holds at least two names of that tercile besides the target itself (``vol_met``; else
    ``vol_thin`` and the whole band); an unranked target draws from the whole pool. No name is
    drawn twice."""
    ranks = np.array([0, 1, 2, 3, 4, 5, 6, 7, -1])
    vol_b = np.array([0, 1, 1, 0, 2, 2, 0, 1, 0])
    every = list(range(8))
    cases = [
        # (avail, targets, window, vol, allowed draws, expected counts)
        ([0, 1, 2, 3, 4, 5, 6, 7], [3], 5, False, {1, 2, 3, 4, 5}, {"rank": 1}),
        ([0, 1, 2, 3, 4, 5, 6, 7], [0], 5, False, {0, 1, 2}, {"rank": 1, "narrow": 1}),
        ([0, 1, 2, 3, 4, 5, 6, 7], [3], 1, False, {3}, {"rank": 1}),
        ([0, 6, 7], [3], 5, False, {0, 6}, {"rank": 1, "widened": 1}),  # gap 3 both sides
        ([0, 7], [3], 5, False, {0}, {"rank": 1, "widened": 1}),
        # band {1..5} around C3 (vol 0): no other vol-0 name, so the whole band (the H3 rule
        # restricted the draw to C3 itself, a copy of the strategy's name)
        ([0, 1, 2, 3, 4, 5, 6, 7], [3], 5, True, {1, 2, 3, 4, 5}, {"rank": 1, "vol_thin": 1}),
        # band {0..6} (window 7): C0 and C6 are vol 0 besides C3 -> restricted to {0, 3, 6}
        ([0, 1, 2, 3, 4, 5, 6, 7], [3], 7, True, {0, 3, 6}, {"rank": 1, "vol_met": 1}),
        # C3 taken: band {0..6} minus C3 still holds C0 and C6 in vol 0
        ([0, 1, 2, 4, 5, 6, 7], [3], 7, True, {0, 6}, {"rank": 1, "narrow": 1, "vol_met": 1}),
        # band {1, 2, 4, 5} holds no vol-0 name: any band name, never C0/C6 (vol 0, outside)
        ([0, 1, 2, 4, 5, 6], [3], 5, True, {1, 2, 4, 5}, {"rank": 1, "narrow": 1, "vol_thin": 1}),
        # C1 (vol 1): band {0..3} has C2 only besides C1 in vol 1 -> whole band
        (every, [1], 5, True, {0, 1, 2, 3}, {"rank": 1, "narrow": 1, "vol_thin": 1}),
        # C2 (vol 1), window 9: band {0..6} besides C2 holds C1 only in vol 1 (C7 is out)
        (every, [2], 9, True, set(range(7)), {"rank": 1, "narrow": 1, "vol_thin": 1}),
        # C2 (vol 1), window 11: band {0..7} holds C1 and C7 besides C2 -> {1, 2, 7}
        (every, [2], 11, True, {1, 2, 7}, {"rank": 1, "narrow": 1, "vol_met": 1}),
        # C8 has no liquidity rank: any free name, counted pool
        ([0, 1, 2, 3, 4, 5, 6, 7, 8], [8], 5, True, set(range(9)), {"pool": 1}),
    ]
    for avail, targets, window, use_vol, allowed, expected in cases:
        seen: set[int] = set()
        for seed in range(40):
            counts = dict.fromkeys(RANK_MATCH_KEYS, 0)
            gaps = _GapAcc()
            got, rest = _match_nearest(
                np.random.default_rng(seed),
                np.array(avail),
                len(targets),
                np.array(targets),
                vol_b if use_vol else None,
                _RankMatch(ranks, window, gaps),
                counts,
            )
            assert len(rest) == 0
            assert len(got) == 1 and int(got[0]) in allowed, (avail, targets, got)
            assert counts == {**dict.fromkeys(RANK_MATCH_KEYS, 0), **expected}, (avail, counts)
            if counts["rank"]:
                assert gaps.n == 1 and gaps.total == abs(int(ranks[got[0]]) - ranks[targets[0]])
            seen.add(int(got[0]))
        assert seen == allowed, (avail, targets, seen)
    # A ranked target without a volatility tercile: the whole band, counted vol_missed.
    counts = dict.fromkeys(RANK_MATCH_KEYS, 0)
    no_vol = np.where(np.arange(9) == 3, -1, vol_b)
    got, _ = _match_nearest(
        np.random.default_rng(0),
        np.arange(8),
        1,
        np.array([3]),
        no_vol,
        _RankMatch(ranks, 5, _GapAcc()),
        counts,
    )
    assert int(got[0]) in {1, 2, 3, 4, 5}
    assert counts == {**dict.fromkeys(RANK_MATCH_KEYS, 0), "rank": 1, "vol_missed": 1}
    # Several targets: distinct draws, extra draws beyond the targets are pool.
    counts = dict.fromkeys(RANK_MATCH_KEYS, 0)
    got, rest = _match_nearest(
        np.random.default_rng(0),
        np.arange(8),
        5,
        np.array([0, 1, 2]),
        None,
        _RankMatch(ranks, 3, _GapAcc()),
        counts,
    )
    assert len(set(got.tolist())) == 5 and len(rest) == 0
    assert counts["rank"] == 3 and counts["pool"] == 2
    # Fewer draws than targets: the unmatched targets come back, in the processing order.
    rng = np.random.default_rng(3)
    order = np.array([0, 1, 2, 5])[np.random.default_rng(3).permutation(4)]
    got, rest = _match_nearest(
        rng,
        np.arange(8),
        2,
        np.array([0, 1, 2, 5]),
        None,
        _RankMatch(ranks, 3, _GapAcc()),
        dict.fromkeys(RANK_MATCH_KEYS, 0),
    )
    assert len(got) == 2 and rest.tolist() == order[2:].tolist()


def test_targets_are_matched_in_a_random_order() -> None:
    """Two targets competing for one band: C0 and C1 (ranks 0, 1) with a band of 3 and only
    C0..C2 free plus C7. Whichever target goes first takes a name near it; over seeds both
    orders occur (neither end is always served first), and the draws stay distinct."""
    ranks = np.arange(8)
    firsts = set()
    for seed in range(40):
        rng = np.random.default_rng(seed)
        first = int(np.array([0, 1])[np.random.default_rng(seed).permutation(2)][0])
        got, _ = _match_nearest(
            rng,
            np.array([0, 1, 2, 7]),
            2,
            np.array([0, 1]),
            None,
            _RankMatch(ranks, 3, _GapAcc()),
            dict.fromkeys(RANK_MATCH_KEYS, 0),
        )
        assert len(set(got.tolist())) == 2 and set(got.tolist()) <= {0, 1, 2}
        firsts.add(first)
    assert firsts == {0, 1}


def test_liquidity_ranks_are_unique_and_tercile_consistent() -> None:
    values = np.array([3.0, 1.0, np.nan, 1.0, 9.0, 2.0, 5.0])
    ranked = np.array([True, True, True, True, True, False, True])
    r = _liquidity_ranks(values, ranked)
    np.testing.assert_array_equal(r, [2, 0, -1, 1, 4, -1, 3])  # tie C1/C3 keeps column order
    n = int((r >= 0).sum())
    np.testing.assert_array_equal(
        np.where(r >= 0, r * 3 // n, -1), _tercile_buckets(values, ranked)
    )


def test_matching_falls_back_joint_then_liquidity_then_vol_then_pool() -> None:
    """``_draw_fresh`` on hand-made terciles: each level is used only when the one before it
    has no free name, every level is counted, and no name is drawn twice."""
    vol_b = np.array([0, 0, 1, 1, 2, 2, 0, 1])
    liq_b = np.array([0, 1, 0, 1, 2, 2, -1, -1])
    cases = [
        # (fresh pool, strategy fresh names, allowed draws, expected counts)
        ([0, 1, 2, 3], [0], {0}, {"joint": 1}),  # (0, 0) has C0
        ([1, 2, 3, 5], [0], {2}, {"liquidity": 1}),  # no free (0, 0): liquidity 0 is C2 only
        ([1, 3, 5, 6], [0], {1, 6}, {"vol": 1}),  # no liquidity 0 either: vol 0 is C1 or C6
        ([3, 4, 5, 7], [0], {3, 4, 5, 7}, {"pool": 1}),  # nothing in vol 0: whole pool
        ([0, 1, 2, 3, 4, 5], [6], {0, 1}, {"vol": 1}),  # a pick with no liquidity tercile
        # two strategy names in (2, 2) with one free: one joint, the other falls to the pool
        # (liquidity 2 and vol 2 are both used up after C4)
        ([0, 1, 4], [4, 5], {0, 1, 4}, {"joint": 1, "pool": 1}),
    ]
    for pool, targets, allowed, expected in cases:
        for seed in range(20):
            counts = dict.fromkeys(MATCH_LEVELS, 0)
            got = _draw_fresh(
                np.random.default_rng(seed),
                np.array(pool),
                len(targets),
                np.array(targets),
                vol_b,
                liq_b,
                counts,
            )
            assert len(got) == len(targets) == len(set(got.tolist()))
            assert set(got.tolist()) <= allowed and set(got.tolist()) <= set(pool)
            assert counts == {**dict.fromkeys(MATCH_LEVELS, 0), **expected}, (pool, targets)
            if expected == {"joint": 1, "pool": 1}:
                assert 4 in got  # the joint name is always taken


def test_more_draws_than_strategy_names_count_as_pool() -> None:
    counts = dict.fromkeys(MATCH_LEVELS, 0)
    got = _draw_fresh(
        np.random.default_rng(0),
        np.arange(6),
        3,
        np.array([0]),
        None,
        np.array([0, 0, 1, 1, 2, 2]),
        counts,
    )
    assert len(set(got.tolist())) == 3
    assert counts == {"joint": 0, "liquidity": 1, "vol": 0, "pool": 2}


def test_liquidity_matched_null_keeps_breadth_retention_and_counts_every_fresh_draw() -> None:
    data, members, w, kw = capped_no_skill(2, n_coins=20, bars=200)
    closes = close_panel(data)
    dollar = trailing_dollar_volume(data, window_days=30)[closes.columns]
    vol = trailing_volatility(closes, 28)
    sw = w.to_numpy()
    rows = np.flatnonzero(~np.isnan(sw).all(axis=1))
    held = np.nan_to_num(sw[rows], nan=0.0) > 0
    k = held.sum(axis=1)
    kept = np.r_[0, (held[1:] & held[:-1]).sum(axis=1)]
    overlaps, chance = [], []
    for seed in range(5):
        res = random_selection_path(
            w, members, np.random.default_rng(seed), vol_panel=vol, liquidity_panel=dollar, **kw
        )
        p = np.nan_to_num(res.weights.to_numpy()[rows], nan=0.0) > 0
        assert (p.sum(axis=1) == k).all()  # breadth (so no name drawn twice)
        np.testing.assert_array_equal(np.r_[0, (p[1:] & p[:-1]).sum(axis=1)], kept)
        fb, kf = res.match_fallbacks, res.kept_match_fallbacks
        assert fb["rank"] + fb["pool"] == int((k - kept).sum())  # every fresh draw counted
        assert kf["rank"] + kf["pool"] == int(kept.sum())  # every kept name counted
        assert fb["pool"] == 0 and fb["rank"] >= fb["widened"] + fb["narrow"]
        assert fb["vol_met"] + fb["vol_thin"] + fb["vol_missed"] == fb["rank"]
        assert kf["vol_met"] + kf["vol_thin"] + kf["vol_missed"] == kf["rank"]
        assert 0.0 <= res.rank_gap < 2.0 and 0.0 <= res.overlap <= 1.0
        overlaps.append(res.overlap)
        chance.append(res.overlap_chance)
    # The band can hold the strategy's own name, so on average the null overlaps the strategy
    # more than a uniform draw would (one 200-bar path can fall below: few rows).
    assert np.mean(overlaps) > np.mean(chance), (overlaps, chance)


def test_liquidity_matched_null_is_causal() -> None:
    """Perturbation: changing every input after row ``cut`` (strategy weights, members,
    liquidity and vol panels) leaves the null's weights up to ``cut`` bit-identical for the
    same seed."""
    data, members, w, kw = capped_no_skill(1, n_coins=20, bars=200)
    closes = close_panel(data)
    dollar = trailing_dollar_volume(data, window_days=30)[closes.columns]
    vol = trailing_volatility(closes, 28)
    cut = 120
    rng = np.random.default_rng(99)
    late = np.arange(len(w)) > cut
    dollar2, vol2, w2 = dollar.copy(), vol.copy(), w.copy()
    dollar2.loc[late] = rng.uniform(1e6, 1e10, dollar2.loc[late].shape)
    vol2.loc[late] = rng.uniform(0.01, 0.2, vol2.loc[late].shape)
    w2.loc[late & w.notna().any(axis=1).to_numpy()] = 0.0
    share2 = kw["liquidity_share"].copy()
    share2.loc[late] = share2.loc[late].iloc[:, ::-1].to_numpy()
    members2 = members.copy()
    members2.loc[late, members.columns[:5]] = False
    kw2 = {**kw, "liquidity_share": share2}
    a = random_selection_weights(
        w, members, np.random.default_rng(7), vol_panel=vol, liquidity_panel=dollar, **kw
    )
    b = random_selection_weights(
        w2, members2, np.random.default_rng(7), vol_panel=vol2, liquidity_panel=dollar2, **kw2
    )
    pd.testing.assert_frame_equal(a.iloc[: cut + 1], b.iloc[: cut + 1], check_exact=True)
    assert not a.iloc[cut + 1 :].equals(b.iloc[cut + 1 :])


def test_liquidity_panel_is_validated() -> None:
    members, dollar = nine_coins()
    strat = frame(members.index, members.columns, {0: [0.5, 0, 0, 0, 0.5, 0, 0, 0, 0]})
    with pytest.raises(ValueError, match="liquidity_panel must have the same index"):
        random_selection_weights(
            strat, members, np.random.default_rng(0), liquidity_panel=dollar.iloc[:-1]
        )
    with pytest.raises(ValueError, match="liquidity_panel must have the same index"):
        random_selection_null(
            coins(9),
            strat,
            members,
            "1d",
            costs=COSTS,
            n_paths=1,
            seed=0,
            liquidity_panel=dollar.iloc[:, ::-1],
        )
    with pytest.raises(ValueError, match="liquidity_panel must hold numbers"):
        random_selection_weights(
            strat, members, np.random.default_rng(0), liquidity_panel=dollar.astype(str) + "x"
        )


def test_null_sums_the_match_counts_over_paths() -> None:
    members, dollar = nine_coins()
    data = coins(9)
    strat = frame(
        members.index,
        members.columns,
        {0: [0.5, 0, 0, 0, 0.5, 0, 0, 0, 0], 7: [0.5, 0, 0, 0, 0, 0, 0, 0.5, 0]},
    )
    null = random_selection_null(
        data, strat, members, "1d", costs=COSTS, n_paths=4, seed=3, liquidity_panel=dollar
    )
    rng = np.random.default_rng(3)
    per_path = [
        random_selection_path(strat, members, rng, liquidity_panel=dollar).match_fallbacks
        for _ in range(4)
    ]
    assert null.match_fallbacks == sum_match_fallbacks(per_path)
    assert set(null.match_fallbacks) == set(RANK_MATCH_KEYS)
    fb = null.match_fallbacks
    assert fb["rank"] + fb["pool"] == 4 * 3  # fresh draws: 2, then 1 (plus 1 kept name)
    plain = random_selection_null(data, strat, members, "1d", costs=COSTS, n_paths=4, seed=3)
    assert plain.match_fallbacks == {"joint": 0, "liquidity": 0, "vol": 0, "pool": 12}
    default = RandomSelectionNull(np.zeros(1), np.zeros(1), np.zeros(1), n_paths=1, seed=0)
    assert default.match_fallbacks == {}
    # Sums: tercile-level keys stay MATCH_LEVELS (also for no paths); rank keys first, then
    # any other key seen.
    assert sum_match_fallbacks([]) == dict.fromkeys(MATCH_LEVELS, 0)
    mixed = sum_match_fallbacks([{"rank": 2, "pool": 1}, {"rank": 1, "joint": 4}])
    assert list(mixed) == [*RANK_MATCH_KEYS, "joint"]
    assert (mixed["rank"], mixed["pool"], mixed["joint"]) == (3, 1, 4)


def illiquid_tilt(
    seed: int, n_coins: int, bars: int, drift: float = 0.002
) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """No-skill strategy with a liquidity tilt. Dollar volume is log-spaced from $5M to $2B a
    day; the least liquid third of the coins has a positive log drift ``drift`` a day (a
    small-cap premium; the liquid two thirds are driftless), all at 4% daily vol. The strategy
    ranks on ``-log(trailing dollar volume) + N(0, 1)`` noise and holds the top quintile weekly:
    it leans hard into illiquid coins but picks at random among them, so it has no skill
    beyond the tilt. Returns (candles, members, weights, dollar volume, vol panel)."""
    rng = np.random.default_rng(10_000 + seed)
    adv = np.exp(np.linspace(np.log(5e6), np.log(2e9), n_coins))
    data = {}
    for k in range(n_coins):
        c = make_candles(
            bars,
            seed=seed * 100 + k,
            interval="1d",
            start="2023-01-01",
            vol=0.04,
            drift=drift if k < n_coins // 3 else 0.0,
        )
        c["volume"] = adv[k] / c["close"] * rng.uniform(0.7, 1.3, len(c))
        data[f"C{k:02d}USDT"] = c
    closes = close_panel(data)
    members = closes.notna() & (np.arange(len(closes))[:, None] >= 40)
    dollar = trailing_dollar_volume(data, window_days=30)[closes.columns]
    score = -np.log(dollar) + rng.normal(0.0, 1.0, dollar.shape)
    sched = rebalance_rows(closes.index, every=7, offset=40)
    w = top_quantile_weights(score, members, sched, quantile=0.2)
    return data, members, w, dollar, trailing_volatility(closes, 28)


def illiquid_tilt_p_values(
    seeds: range, n_coins: int, bars: int, n_paths: int
) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """p-values of the tilted no-skill strategy against today's vol-matched null
    (unmatched on liquidity) and against the joint vol x liquidity null (the runner's call)."""
    unmatched, matched, counts = [], [], []
    for seed in seeds:
        data, members, w, dollar, vol = illiquid_tilt(seed, n_coins, bars)
        sharpe = metrics_from_returns(run_portfolio(data, w, costs=COSTS).returns, "1d").sharpe
        kw = {"costs": COSTS, "n_paths": n_paths, "seed": seed, "vol_panel": vol}
        u = random_selection_null(data, w, members, "1d", **kw)  # type: ignore[arg-type]
        m = random_selection_null(data, w, members, "1d", liquidity_panel=dollar, **kw)  # type: ignore[arg-type]
        unmatched.append(u.p_value(sharpe))
        matched.append(m.p_value(sharpe))
        counts.append(m.match_fallbacks)
    return np.array(unmatched), np.array(matched), sum_match_fallbacks(counts)


def test_liquidity_matched_null_is_calibrated_when_the_strategy_leans_illiquid() -> None:
    """Probe (24 seeds, 30 coins, 300 bars, 20 paths): without liquidity matching mean p 0.23
    and 7 of 24 seeds at p <= 0.05 (the illiquid drift is credited as skill); with nearest-rank
    matching mean p 0.46, 3 of 24 at p <= 0.05, 3 at p <= 0.10 (H3's volatility rule: 0.53, 1,
    3; H2's terciles: 0.53, 2, 3; package H: 0.46, 1, 3). Every fresh draw was rank matched;
    2.8% had to widen past the band; the band held two other names of the target's volatility
    tercile for 12% of draws (``vol_met``; H3's rule restricted 89%)."""
    unmatched, matched, counts = illiquid_tilt_p_values(range(24), 30, 300, 20)
    assert unmatched.mean() < 0.3, unmatched
    assert (unmatched <= 0.05).sum() >= 4, unmatched  # chance expects ~1.1
    assert 0.35 < matched.mean() < 0.65, matched
    assert (matched <= 0.05).sum() <= 4, matched  # chance 1/21 per seed, P(>= 5) < 1%
    assert (matched <= 0.10).sum() <= 6, matched
    assert counts["pool"] == 0 and counts["widened"] / counts["rank"] < 0.05, counts


@pytest.mark.slow
def test_liquidity_matched_null_is_calibrated_when_the_strategy_leans_illiquid_slow() -> None:
    """Probe (80 seeds, 600 bars, 40 paths): unmatched mean p 0.14, 45% of seeds at
    p <= 0.05; nearest-rank matched mean p 0.51, 1 of 80 at p <= 0.05 (chance 3.9), 9% at
    p <= 0.10 (chance 9.8%; H3's volatility rule: 0.51, 2, 10%; H2's terciles: 0.54, 2, 6%;
    package H: 0.43, 5, 16%)."""
    unmatched, matched, _ = illiquid_tilt_p_values(range(80), 30, 600, 40)
    assert unmatched.mean() < 0.25, unmatched
    assert (unmatched <= 0.05).mean() > 0.25, unmatched
    assert 0.35 < matched.mean() < 0.65, matched
    assert (matched <= 0.05).sum() <= 9, matched  # 2/41 per seed: ~3.9 expected, P(>= 10) < 1%
    assert (matched <= 0.10).mean() <= 0.2, matched


def test_capped_no_skill_null_stays_calibrated_with_the_runners_liquidity_matching() -> None:
    """The existing capped no-skill case, nulled the way the runner now calls it (vol panel,
    trailing dollar-volume liquidity panel, caps, impact). Probe (nearest-rank matching): mean
    p 0.48, 1 of 24 seeds at p <= 0.05, 2 at p <= 0.10 (H3's volatility rule: 0.44, 1, 1; H2's
    terciles: 0.42, 1, 2; package H: 0.46, 0, 1)."""
    p = capped_p_values(range(24), n_coins=20, bars=300, n_paths=20, runner_call=True)
    assert 0.35 < p.mean() < 0.65, p
    assert (p <= 0.05).sum() <= 4, p
    assert (p <= 0.10).sum() <= 6, p


# --------------------------------------------------------------------------- kept names (H2)


def nine_coin_two_rows() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Row 0 holds C0 (liquidity tercile 0) at 0.6 and C4 (tercile 1) at 0.2; row 7 keeps C0
    at 0.3, drops C4 and adds C7 (tercile 2) at 0.5."""
    members, dollar = nine_coins()
    strat = frame(
        members.index,
        members.columns,
        {0: [0.6, 0, 0, 0, 0.2, 0, 0, 0, 0], 7: [0.3, 0, 0, 0, 0, 0, 0, 0.5, 0]},
    )
    return members, dollar, strat


def test_kept_null_names_come_from_the_band_around_the_strategy_kept_names() -> None:
    """Nine coins ranked 0..8. Row 0 holds C0 and C4; row 7 keeps C0 (rank 0), drops C4 and
    adds C7. The null keeps the one of its own two names inside C0's band (ranks 0..2), never a
    name outside it (a uniform keep drops the right one whenever its C4 stand-in sits outside
    the band), and its fresh name comes from C7's band (ranks 5..8)."""
    members, dollar, strat = nine_coin_two_rows()
    outside = 0
    for seed in range(40):
        res = random_selection_path(
            strat, members, np.random.default_rng(seed), liquidity_panel=dollar
        )
        first = set(np.flatnonzero(res.weights.iloc[0] > 0).tolist())
        second = set(np.flatnonzero(res.weights.iloc[7] > 0).tolist())
        kept = first & second
        assert len(kept) == 1 and kept <= {0, 1, 2}, (first, second)
        (fresh,) = second - first
        assert fresh in {5, 6, 7, 8}
        outside += len(first - {0, 1, 2}) == 1
        assert res.kept_match_fallbacks["rank"] == 1 and res.kept_match_fallbacks["pool"] == 0
        assert res.match_fallbacks["rank"] == 3 and res.match_fallbacks["pool"] == 0
    assert outside > 10  # the rule had a wrong name to drop on many seeds


def test_kept_names_the_survivors_cannot_match_are_matched_by_fresh_draws() -> None:
    """Membership churn: the strategy holds C0 and C1 (the two least liquid of nine) on rows 0
    and 7, and C2 and C3 leave the pool on row 7. When the null held C2 or C3 it has fewer
    survivors than the strategy kept names; the strategy names left unmatched join the fresh
    targets, so every slot is drawn from a band (row 7 ranks: C0 0, C1 1, C4 2, C5 3, ...; a
    band of 3 ranks around rank 0 or 1 lies in {C0, C1, C4}, the bottom liquidity tercile of
    the seven ranked names, like the strategy's book) and none is a uniform pool draw.
    Handing them back as pool draws (the H3 code, simulated by dropping the leftovers) puts
    names from the whole pool in the book."""
    members, dollar = nine_coins()
    members.iloc[7, [2, 3]] = False
    strat = frame(
        members.index,
        members.columns,
        {0: [0.5, 0.5, 0, 0, 0, 0, 0, 0, 0], 7: [0.5, 0.5] + [0] * 7},
    )
    row7_terciles = _tercile_buckets(dollar.iloc[7].to_numpy(), members.iloc[7].to_numpy())
    assert (row7_terciles[[0, 1]] == 0).all()
    short = 0
    for seed in range(60):
        res = random_selection_path(
            strat, members, np.random.default_rng(seed), liquidity_panel=dollar, liquidity_window=3
        )
        first = set(np.flatnonzero(res.weights.iloc[0] > 0).tolist())
        second = set(np.flatnonzero(res.weights.iloc[7] > 0).tolist())
        survivors = first - {2, 3}
        short += len(survivors) < 2
        assert len(second) == 2 and second <= {0, 1, 4}, (first, second)
        assert survivors <= second  # the null still keeps every survivor it can
        assert res.match_fallbacks["pool"] == 0 and res.kept_match_fallbacks["pool"] == 0
        n_kept = len(survivors)
        assert res.kept_match_fallbacks["rank"] == n_kept
        assert res.match_fallbacks["rank"] == 2 + (2 - n_kept)  # row 0, then the leftovers
        # row 7's liquidity mix is the strategy's: every name in the bottom tercile
        assert (row7_terciles[sorted(second)] == 0).all(), second
    assert short > 20  # the churn case happened on many seeds
    outside = pool = 0
    real_retain = pb._retain

    def drop_leftovers(*args: object) -> tuple[np.ndarray, np.ndarray]:
        keep, _ = real_retain(*args)  # type: ignore[arg-type]
        return keep, np.zeros(0, dtype=int)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pb, "_retain", drop_leftovers)
        for seed in range(60):
            res = random_selection_path(
                strat,
                members,
                np.random.default_rng(seed),
                liquidity_panel=dollar,
                liquidity_window=3,
            )
            outside += not set(np.flatnonzero(res.weights.iloc[7] > 0).tolist()) <= {0, 1, 4}
            pool += res.match_fallbacks["pool"]
    assert pool > 20 and outside > 10, (pool, outside)


def test_retention_uses_the_band_and_is_uniform_without_liquidity() -> None:
    ranks = np.arange(6)
    counts = dict.fromkeys(RANK_MATCH_KEYS, 0)
    # The strategy kept a rank-5 name; the null's survivors are ranks 1 and 2, outside the
    # band 4..6: the nearest one (rank 2) is kept and the band counts as widened.
    match = _RankMatch(ranks, 3, _GapAcc())
    for seed in range(10):
        got, rest = _retain(
            np.random.default_rng(seed), np.array([1, 2]), 1, np.array([5]), None, match, counts
        )
        assert got.tolist() == [2] and len(rest) == 0
    assert counts["rank"] == 10 and counts["widened"] == 10 and match.gaps.total == 30
    # Without a liquidity panel: the legacy draw, same RNG calls, nothing counted.
    counts = dict.fromkeys(MATCH_LEVELS, 0)
    survivors = np.array([0, 2, 4, 5])
    for seed in range(10):
        got, rest = _retain(
            np.random.default_rng(seed), survivors, 2, np.array([4]), None, None, counts
        )
        assert len(rest) == 0  # legacy retention never hands kept names to the fresh draw
        want = np.random.default_rng(seed).choice(survivors, size=2, replace=False)
        np.testing.assert_array_equal(got, want)
    assert counts == dict.fromkeys(MATCH_LEVELS, 0)


def uniform_retain(
    rng: np.random.Generator, survivors: np.ndarray, n_keep: int, *_: object
) -> tuple[np.ndarray, np.ndarray]:
    """Package H's retention: keep ``n_keep`` survivors uniformly at random (no kept name is
    handed on to the fresh draw)."""
    return rng.choice(survivors, size=n_keep, replace=False), np.zeros(0, dtype=int)


def tercile_match(
    rng: np.random.Generator,
    avail: np.ndarray,
    need: int,
    targets: np.ndarray,
    vol_b: np.ndarray | None,
    match: _RankMatch,
    counts: dict[str, int],
) -> tuple[np.ndarray, np.ndarray]:
    """Package H2's liquidity-tercile rule in place of ``_match_nearest``: the row's liquidity
    terciles are ``rank * 3 // n`` (exactly ``_tercile_buckets``), then ``_draw_fresh``'s
    joint / liquidity / vol / pool levels; unmatched kept names are not handed on (H2 left
    those slots to pool draws). Pinned bit-identical to H2 below."""
    n = int((match.ranks >= 0).sum())
    liq_b = np.where(match.ranks >= 0, match.ranks * 3 // max(n, 1), -1)
    levels = dict.fromkeys(MATCH_LEVELS, 0)
    out = _draw_fresh(rng, avail, need, targets, vol_b, liq_b, levels)
    for key, value in levels.items():
        counts[key] = counts.get(key, 0) + value
    return out, targets[:0]


RULES = ("nearest", "tercile", "uniform")  # production; H2; package H (terciles, uniform keep)


@contextmanager
def matching_rule(rule: str) -> Iterator[None]:
    """``nearest`` = the null as shipped; ``tercile`` = H2 (fresh and kept names tercile
    matched); ``uniform`` = package H (fresh names tercile matched, kept names uniform)."""
    assert rule in RULES, rule
    with pytest.MonkeyPatch.context() as mp:
        if rule != "nearest":
            mp.setattr(pb, "_match_nearest", tercile_match)
        if rule == "uniform":
            mp.setattr(pb, "_retain", uniform_retain)
        yield


def test_old_rules_reproduce_and_old_paths_are_unchanged() -> None:
    """Default and vol-only: no kept counts (legacy uniform retention; the a5fa079 digests are
    pinned in ``test_default_off_null_is_bit_identical_to_before_liquidity_matching``). With a
    liquidity panel the shipped null differs from both earlier liquidity rules, and swapping
    the matcher back reproduces each of them bit for bit (digests recorded from the code as it
    was: package H, then H2), so the ``tercile`` and ``uniform`` rules the calibration tests
    compare against are exactly the old code."""
    data, members, w, kw = capped_no_skill(0, n_coins=20, bars=200)
    closes = close_panel(data)
    vol = trailing_volatility(closes, 28)
    dollar = trailing_dollar_volume(data, window_days=30)[closes.columns]
    for extra in ({}, {"vol_panel": vol}, {"inverse_vol": vol, "max_weight": 0.3, **kw}):
        res = random_selection_path(w, members, np.random.default_rng(5), **extra)
        assert res.kept_match_fallbacks == {}
        assert set(res.match_fallbacks) == set(MATCH_LEVELS) and np.isnan(res.rank_gap)
    cases = {
        "liq": ({"liquidity_panel": dollar}, "411dd06754562a48", "ddbe2f175739b6bf"),
        "joint_capped": (
            {"vol_panel": vol, "liquidity_panel": dollar, "max_weight": 0.3, **kw},
            "2bfc2ea7f8fb9fed",
            "f232ad2c522ce447",
        ),
    }
    for name, (extra, package_h, package_h2) in cases.items():
        now = random_selection_path(w, members, np.random.default_rng(5), **extra)
        assert set(now.match_fallbacks) == set(RANK_MATCH_KEYS), name
        assert now.kept_match_fallbacks["rank"] > 0, name
        assert _digest(now.weights) not in (package_h, package_h2), name
        for rule, want in (("uniform", package_h), ("tercile", package_h2)):
            with matching_rule(rule):
                old = random_selection_weights(w, members, np.random.default_rng(5), **extra)
            assert _digest(old) == want, (name, rule)


def test_book_mix_overlap_and_rank_gap_of_a_one_rank_band() -> None:
    """Hand-computed: strategy rows {C0 0.6, C4 0.2} and {C0 0.3, C7 0.5} are liquidity
    terciles (0, 1) then (0, 2): count shares [0.5, 0.25, 0.25], weight shares
    mean([0.75, 0.25, 0], [0.375, 0, 0.625]). With a band of one rank every null draw is the
    strategy's own name (always free here): the null (equal weights) holds the same names, so
    its count and weight shares are [0.5, 0.25, 0.25], its overlap is 1 against a chance level
    of 2/9 (two names of nine), and every rank gap is 0."""
    members, dollar, strat = nine_coin_two_rows()
    null = random_selection_null(
        coins(9),
        strat,
        members,
        "1d",
        costs=COSTS,
        n_paths=6,
        seed=2,
        liquidity_panel=dollar,
        liquidity_window=1,
    )
    mix = null.book_mix
    assert set(mix) == {"strategy", "null"} and set(mix["strategy"]) == {
        "liquidity_count",
        "liquidity_weight",
    }
    np.testing.assert_allclose(mix["strategy"]["liquidity_count"], [0.5, 0.25, 0.25])
    np.testing.assert_allclose(mix["strategy"]["liquidity_weight"], [0.5625, 0.125, 0.3125])
    np.testing.assert_allclose(mix["null"]["liquidity_count"], [0.5, 0.25, 0.25])
    np.testing.assert_allclose(mix["null"]["liquidity_weight"], [0.5, 0.25, 0.25])
    assert null.kept_match_fallbacks == {**dict.fromkeys(RANK_MATCH_KEYS, 0), "rank": 6}
    assert null.match_fallbacks == {**dict.fromkeys(RANK_MATCH_KEYS, 0), "rank": 18}
    assert null.overlap == 1.0 and null.rank_gap == 0.0
    assert null.overlap_chance == pytest.approx(2 / 9)
    rng = np.random.default_rng(2)
    paths = [
        random_selection_path(strat, members, rng, liquidity_panel=dollar, liquidity_window=1)
        for _ in range(6)
    ]
    assert mean_book_mix(paths) == mix
    assert sum_kept_match_fallbacks(paths) == null.kept_match_fallbacks
    # Vol terciles: by vol_panel, or by 1 / inverse_vol (tercile 0 = least volatile either way).
    vol = dollar / 1e8  # C0 least volatile
    by_panel = random_selection_path(strat, members, np.random.default_rng(0), vol_panel=vol)
    by_inv = random_selection_path(strat, members, np.random.default_rng(0), inverse_vol=1 / vol)
    for res in (by_panel, by_inv):
        assert set(res.book_mix["strategy"]) == {"vol_count", "vol_weight"}
        np.testing.assert_allclose(res.book_mix["strategy"]["vol_count"], [0.5, 0.25, 0.25])
        assert res.kept_match_fallbacks == {}
        assert 0.0 <= res.overlap <= 1.0 and res.overlap_chance == pytest.approx(2 / 9)
    plain = random_selection_path(strat, members, np.random.default_rng(0))
    assert plain.book_mix == {"strategy": {}, "null": {}}
    assert mean_book_mix([]) == {} and sum_kept_match_fallbacks([plain]) == {}
    default = RandomSelectionNull(np.zeros(1), np.zeros(1), np.zeros(1), n_paths=1, seed=0)
    assert default.kept_match_fallbacks == {} and default.book_mix == {}
    assert np.isnan(default.overlap) and np.isnan(default.overlap_chance)
    assert np.isnan(default.rank_gap)


def test_overlap_is_the_share_of_null_names_the_strategy_holds() -> None:
    """Hand-checked against the weights: per decision row, |null names & strategy names| /
    |null names|, averaged over the rows the null draws on; chance = |held & pool| / |pool|."""
    data, members, w, kw = capped_no_skill(3, n_coins=20, bars=200)
    closes = close_panel(data)
    dollar = trailing_dollar_volume(data, window_days=30)[closes.columns]
    res = random_selection_path(w, members, np.random.default_rng(1), liquidity_panel=dollar, **kw)
    sw, nw = w.to_numpy(), res.weights.to_numpy()
    pool = members.to_numpy() & (kw["liquidity_share"].to_numpy() > 0)
    share, chance = [], []
    for i in np.flatnonzero(~np.isnan(sw).all(axis=1)):
        held, null = np.nan_to_num(sw[i]) > 0, np.nan_to_num(nw[i]) > 0
        if null.any():
            share.append((held & null).sum() / null.sum())
            chance.append((held & pool[i]).sum() / pool[i].sum())
    assert res.overlap == pytest.approx(np.mean(share))
    assert res.overlap_chance == pytest.approx(np.mean(chance))
    rng = np.random.default_rng(1)
    paths = [random_selection_path(w, members, rng, liquidity_panel=dollar, **kw) for _ in range(3)]
    null = random_selection_null(
        data, w, members, "1d", costs=COSTS, n_paths=3, seed=1, liquidity_panel=dollar, **kw
    )
    assert null.overlap == pytest.approx(mean_path_stat(p.overlap for p in paths))
    assert null.rank_gap == pytest.approx(mean_path_stat(p.rank_gap for p in paths))
    assert mean_path_stat([float("nan")]) != mean_path_stat([float("nan")])  # NaN when none


def test_liquidity_window_is_validated_and_changes_the_draws() -> None:
    members, dollar = nine_coins()
    strat = frame(members.index, members.columns, {0: [0.5, 0, 0, 0, 0.5, 0, 0, 0, 0]})
    for bad in (0, 2, -1, 1.0, True, "5"):
        with pytest.raises(ValueError, match="liquidity_window must be an odd int"):
            random_selection_weights(
                strat,
                members,
                np.random.default_rng(0),
                liquidity_panel=dollar,
                liquidity_window=bad,  # type: ignore[arg-type]
            )
    with pytest.raises(ValueError, match="liquidity_window"):
        random_selection_null(
            coins(9), strat, members, "1d", costs=COSTS, n_paths=1, seed=0, liquidity_window=4
        )
    one = {
        tuple(
            np.flatnonzero(
                random_selection_weights(
                    strat,
                    members,
                    np.random.default_rng(s),
                    liquidity_panel=dollar,
                    liquidity_window=1,
                ).iloc[0]
                > 0
            )
        )
        for s in range(10)
    }
    wide = {
        tuple(
            np.flatnonzero(
                random_selection_weights(
                    strat,
                    members,
                    np.random.default_rng(s),
                    liquidity_panel=dollar,
                    liquidity_window=9,
                ).iloc[0]
                > 0
            )
        )
        for s in range(30)
    }
    assert one == {(0, 4)} and len(wide) > 5


def persistent_tilt(
    seed: int,
    bars: int,
    premium: float = 0.002,
    phi: float = 0.95,
    n_coins: int = 30,
    shape: str = "linear",
) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, np.ndarray]:
    """No-skill strategy with a PERSISTENT liquidity-tilted score and a premium that is not
    aligned to tercile boundaries. Dollar volume is log-spaced from $5M to $2B a day. Coin k's
    log drift a day is ``premium * (1 - k / (n - 1))`` (``shape="linear"``: linear in log dollar
    volume, so it also varies inside each tercile) or ``premium`` for the 5 least liquid coins
    and 0 for the rest (``shape="concentrated"``: half the bottom tercile of 30 coins, the case
    terciles cannot see). The score is ``-log(trailing dollar volume) + e``, ``e`` an AR(1) with
    coefficient ``phi`` and unit variance (a weekly-rebalanced score that barely moves, like a
    28-day momentum), top quintile weekly, 4% daily vol. Returns (candles, members, weights,
    dollar volume, vol panel, drift per column)."""
    rng = np.random.default_rng(20_000 + seed)
    adv = np.exp(np.linspace(np.log(5e6), np.log(2e9), n_coins))
    k = np.arange(n_coins)
    if shape == "linear":
        drift = premium * (1 - k / (n_coins - 1))
    elif shape == "concentrated":
        drift = np.where(k < 5, premium, 0.0)
    else:
        raise ValueError(shape)
    data = {}
    for j in range(n_coins):
        c = make_candles(
            bars, seed=seed * 100 + j, interval="1d", start="2023-01-01", vol=0.04, drift=drift[j]
        )
        c["volume"] = adv[j] / c["close"] * rng.uniform(0.7, 1.3, len(c))
        data[f"C{j:02d}USDT"] = c
    closes = close_panel(data)
    members = closes.notna() & (np.arange(len(closes))[:, None] >= 40)
    dollar = trailing_dollar_volume(data, window_days=30)[closes.columns]
    shocks = rng.normal(0.0, 1.0, dollar.shape)
    noise = np.empty_like(shocks)
    noise[0] = shocks[0]
    for t in range(1, len(noise)):
        noise[t] = phi * noise[t - 1] + np.sqrt(1 - phi**2) * shocks[t]
    sched = rebalance_rows(closes.index, every=7, offset=40)
    w = top_quantile_weights(-np.log(dollar) + noise, members, sched, quantile=0.2)
    return data, members, w, dollar, trailing_volatility(closes, 28), drift


def kept_least_liquid_share(
    weights: pd.DataFrame, dollar: pd.DataFrame, members: pd.DataFrame
) -> float:
    """Share of kept names (held on this and the previous decision row) in liquidity tercile 0,
    terciles ranked as the null ranks them (members | held)."""
    w = weights.to_numpy(dtype=float)
    d = dollar.to_numpy(dtype=float)
    m = members.to_numpy(dtype=bool)
    low = total = 0
    prev = None
    for i in np.flatnonzero(~np.isnan(w).all(axis=1)):
        held = np.nan_to_num(w[i]) > 0
        if prev is not None:
            b = _tercile_buckets(d[i], m[i] | held)[held & prev]
            low += int((b == 0).sum())
            total += int((b >= 0).sum())
        prev = held
    return low / total


def premium_captured(weights: pd.DataFrame, drift: np.ndarray) -> float:
    """Mean over decision rows of the target book's expected log drift a day (sum w * drift):
    the expected return a no-skill strategy and its null must share."""
    w = weights.to_numpy(dtype=float)
    rows = np.flatnonzero(~np.isnan(w).all(axis=1))
    return float((np.nan_to_num(w[rows]) * drift).sum(axis=1).mean())


def tilt_books(
    seeds: range, bars: int, n_paths: int, rule: str, shape: str = "linear"
) -> dict[str, np.ndarray]:
    """Per seed: the strategy's and the null's (mean over paths) kept least-liquid share, book
    least-liquid share, expected premium captured, the max book vol-tercile gap and overlap."""
    out: dict[str, list[float]] = {k: [] for k in ("kept", "book", "premium", "s_kept")}
    out |= {"s_book": [], "s_premium": [], "vol_gap": [], "overlap": []}
    for seed in seeds:
        _, members, w, dollar, vol, drift = persistent_tilt(seed, bars, shape=shape)
        rng = np.random.default_rng(seed)
        with matching_rule(rule):
            paths = [
                random_selection_path(w, members, rng, vol_panel=vol, liquidity_panel=dollar)
                for _ in range(n_paths)
            ]
        mix = mean_book_mix(paths)
        out["kept"].append(
            np.mean([kept_least_liquid_share(p.weights, dollar, members) for p in paths])
        )
        out["book"].append(mix["null"]["liquidity_count"][0])
        out["premium"].append(np.mean([premium_captured(p.weights, drift) for p in paths]))
        out["s_kept"].append(kept_least_liquid_share(w, dollar, members))
        out["s_book"].append(mix["strategy"]["liquidity_count"][0])
        out["s_premium"].append(premium_captured(w, drift))
        gap = np.subtract(mix["null"]["vol_count"], mix["strategy"]["vol_count"])
        out["vol_gap"].append(float(np.abs(gap).max()))
        out["overlap"].append(mean_path_stat(p.overlap for p in paths))
    return {k: np.array(v) for k, v in out.items()}


def premium_gap(books: dict[str, np.ndarray]) -> np.ndarray:
    """Per seed: the share of the strategy's expected premium the null's book misses."""
    return 1 - books["premium"] / books["s_premium"]


def test_nearest_rank_null_closes_the_book_gap_of_a_persistent_score() -> None:
    """The reviewer's case (PLAN.md Phase 10, H2/H3): a persistent score keeps its names
    because of the tilt, so its kept names sit deeper in it than its fresh ones. Probe (seeds
    0..29 in blocks of 10, 600 bars, 5 paths): strategy kept names 94-96% least-liquid
    tercile, book 89-92%. Nearest rank (H4 volatility rule): null kept/book within 0.032 /
    0.037 of the strategy's on every seed (seeds 0..9: 0.021 / 0.026); book volatility-tercile
    gap at most 0.045; expected premium a day missed 1.2-1.5% (max 2.1%); overlap 0.61-0.63.
    (H3's rule, which copied the strategy's names: 0.018 / 0.024, missed 0.5-0.6%, overlap
    0.77; copying hid the band's end-clipping gap, module docstring step 2.) H2's terciles:
    kept/book exact, premium missed 3.9-4.5% (the within-tercile part of a linear premium);
    overlap 0.60. Package H's uniform retention: kept 77-83%, premium missed 8.5-10.1%. The
    tercile and uniform assertions are the old code's behaviour (pinned bit-identical above)."""
    books = {rule: tilt_books(range(10), 600, 5, rule) for rule in RULES}
    near, terc, unif = books["nearest"], books["tercile"], books["uniform"]
    np.testing.assert_array_equal(near["s_kept"], unif["s_kept"])  # same strategy
    assert near["s_kept"].mean() > 0.9 and near["s_book"].mean() > 0.85
    # nearest rank: the null's kept names and whole book sit where the strategy's do
    assert np.abs(near["kept"] - near["s_kept"]).max() < 0.035, near
    assert np.abs(near["book"] - near["s_book"]).max() < 0.04, near  # rank, not tercile, match
    assert near["vol_gap"].max() < 0.06, near["vol_gap"]
    gap_n, gap_t, gap_u = premium_gap(near), premium_gap(terc), premium_gap(unif)
    assert gap_n.mean() < 0.02 and gap_n.max() < 0.03, gap_n
    # terciles leave the within-tercile part of a linear premium; uniform retention ~2x that
    assert gap_t.mean() > 2 * gap_n.mean() and gap_t.mean() > 0.03, (gap_t, gap_n)
    assert (unif["s_kept"] - unif["kept"]).mean() > 0.1, unif
    assert gap_u.mean() > 0.07 and gap_u.mean() > 1.6 * gap_t.mean(), (gap_u, gap_t)


def test_nearest_rank_null_closes_the_within_tercile_gap_on_long_histories() -> None:
    """Noise-free version of the long-history calibration (slow test below): 2,400 bars (about
    H1's design window), the premium linear in log dollar volume or concentrated in the 5 least
    liquid coins (inside the bottom tercile). Expected premium a day the null misses, nearest
    rank vs H2's terciles. Probe (seeds 0..7, 5 paths): linear 1.30% (1.20-1.40%) vs 4.0%
    (3.5-4.7%); concentrated 5.0% (3.3-6.7%) vs 20.5% (17.9-22.9%). This test (seeds 0..2, 3
    paths): linear max 1.39% vs min 3.7%; concentrated max 6.9% vs min 17.1%. (H3's rule
    missed 0.61% and 2.2% by copying the strategy's names, at half the power; the rest of
    the gap is the band clipped at the least liquid end, module docstring step 2.)"""
    for shape, near_max, terc_min in (("linear", 0.02, 0.03), ("concentrated", 0.09, 0.15)):
        near = premium_gap(tilt_books(range(3), 2400, 3, "nearest", shape))
        terc = premium_gap(tilt_books(range(3), 2400, 3, "tercile", shape))
        assert near.max() < near_max, (shape, near)
        assert terc.min() > terc_min, (shape, terc)
        assert terc.mean() > 2.5 * near.mean(), (shape, near, terc)


def persistent_tilt_p_values(
    seeds: range,
    bars: int,
    n_paths: int,
    *,
    rule: str = "nearest",
    premium: float = 0.002,
    shape: str = "linear",
) -> np.ndarray:
    pvals = []
    for seed in seeds:
        data, members, w, dollar, vol, _ = persistent_tilt(seed, bars, premium=premium, shape=shape)
        sharpe = metrics_from_returns(run_portfolio(data, w, costs=COSTS).returns, "1d").sharpe
        with matching_rule(rule):
            null = random_selection_null(
                data,
                w,
                members,
                "1d",
                costs=COSTS,
                n_paths=n_paths,
                seed=seed,
                vol_panel=vol,
                liquidity_panel=dollar,
            )
        pvals.append(null.p_value(sharpe))
    return np.array(pvals)


def test_nearest_rank_null_is_calibrated_for_a_persistent_tilted_score() -> None:
    """No-skill p-values, persistent score, linear premium. Probe (seed blocks 0..23, 24..47,
    48..71; 600 bars, 20 paths): mean p 0.484 / 0.464 / 0.554, at p <= 0.05 2 / 0 / 2 of 24,
    at p <= 0.10 3 / 2 / 3 (H3's volatility rule: 0.536 / 0.508 / 0.546, 0 / 1 / 0, 1 / 2 / 1;
    H2's tercile rule: 0.514 / 0.518 / 0.534, 1 / 1 / 0, 3 / 1 / 2)."""
    p = persistent_tilt_p_values(range(24), 600, 20)
    assert 0.35 < p.mean() < 0.65, p
    assert (p <= 0.05).sum() <= 4, p  # chance 1/21 per seed, P(>= 5) < 1%
    assert (p <= 0.10).sum() <= 6, p


@pytest.mark.slow
def test_nearest_rank_null_is_calibrated_and_uniform_retention_over_rejects_slow() -> None:
    """80 seeds, 600 bars, 40 paths, premium 0.003 a day at the least liquid coin. Probe:
    nearest rank mean p 0.506, 2 / 5 / 20 seeds at p <= 0.05 / 0.10 / 0.25 (H3's volatility
    rule: 0.528, 2 / 4 / 15); package H's uniform
    retention 0.441, 6 / 12 / 27 (H2's tercile rule was 0.503, 2 / 5 / 19). Chance per seed:
    p <= 0.10 is 4/41 (~7.8 of 80, sd 2.7), p <= 0.25 is 10/41 (~19.5, sd 3.8)."""
    near = persistent_tilt_p_values(range(80), 600, 40, premium=0.003)
    unif = persistent_tilt_p_values(range(80), 600, 40, rule="uniform", premium=0.003)
    assert 0.42 < near.mean() < 0.6, near
    assert (near <= 0.05).sum() <= 9, near  # 2/41 per seed: ~3.9 expected, P(>= 10) < 1%
    assert (near <= 0.10).sum() <= 13, near
    assert (near <= 0.25).sum() <= 25, near
    assert (unif <= 0.25).sum() >= 25, unif  # over-rejects (chance ~19.5)
    assert (unif <= 0.10).sum() >= 11, unif  # chance ~7.8
    assert unif.mean() < near.mean() - 0.04, (unif.mean(), near.mean())


@pytest.mark.slow
def test_nearest_rank_null_is_calibrated_on_long_histories_and_terciles_over_reject_slow() -> None:
    """The case H2 left open: 2,400 daily bars (about H1's design window), premium 0.002 a day
    on the 5 least liquid coins only (inside the bottom tercile), AR(0.95) score, 30 paths.
    Probe (H4 volatility rule): nearest rank seeds 0..59 mean p 0.467, 7 / 9 at p <= 0.05 /
    0.10 (seeds 60..119: 0.429, 3 / 7); zero-premium control seeds 0..29 0.542 (1 / 4), 30..59
    0.544 (2 / 3); H2's terciles seeds 0..29 mean p 0.297, 7 / 12 (30..59: 0.247, 7 / 11).
    With 40 paths and two fresh 80-seed blocks (3000-3079 / 3100-3179): nearest rank 0.461 /
    0.445 (7 / 9 of 80 at p <= 0.05), control 0.540 / 0.530 (4 / 4); linear premium 0.509 /
    0.498 (6 / 7). (H3's rule, which copied the strategy's names at half the power: seeds
    0..59 0.516, 1 / 6; terciles 0.258 / 0.198, 21 / 26 of 80.) The tercile residual H2
    recorded is gone; what is left in this extreme case (a 73%/yr premium on the 5 least
    liquid coins) is a mild over-rejection from the band clipped at the least liquid end,
    module docstring **Window**."""
    near = persistent_tilt_p_values(range(60), 2400, 30, shape="concentrated")
    terc = persistent_tilt_p_values(range(30), 2400, 30, rule="tercile", shape="concentrated")
    zero = persistent_tilt_p_values(range(30), 2400, 30, premium=0.0, shape="concentrated")
    assert 0.38 < near.mean() < 0.62, near
    assert (near <= 0.05).sum() <= 8, near  # 2/31 per seed: ~3.9 expected of 60, P(>= 9) < 1%
    assert (near <= 0.10).sum() <= 13, near  # 4/31: ~7.7 expected
    assert 0.35 < zero.mean() < 0.65, zero
    assert terc.mean() < 0.35, terc  # terciles credit the within-tercile premium as skill
    assert (terc <= 0.05).sum() >= 5, terc  # chance ~1.9 of 30
    assert terc.mean() < near.mean() - 0.12, (terc.mean(), near.mean())


def momentum_skill_world(
    seed: int, bars: int, edge: float, comove: float = 0.0, n_coins: int = 30
) -> tuple[dict, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """An H1-like strategy WITH skill (the quant reviewer's power probe, package H4): 30 coins,
    a common market factor (2.5% a day), idiosyncratic daily vol 6% (least liquid) to 3% (most
    liquid) times U(0.8, 1.2), and a planted causal cross-sectional momentum edge: from bar 29
    the log return on bar t gets ``edge * z``, z the cross-sectional z-score of the 28-day log
    return known at the close of t - 1 (so the engine's t open fill can earn it), minus
    sigma^2 / 2. Dollar volume log-spaced $5M-$2B times a slow AR(0.99) factor, times
    ``exp(comove * 28-day log return)`` (``comove`` = 1: liquidity rises with momentum, so the
    null's band follows the strategy's names), times U(0.7, 1.3). The strategy is the top
    quintile of the 28-day log return, weekly. ``edge`` 0.0004 is about +4%/yr over the
    equal-weight universe net of costs at 2,450 bars (probe). Returns (candles, members,
    weights, dollar volume, vol panel)."""
    rng = np.random.default_rng(777_000 + seed)
    index = pd.date_range("2019-01-01", periods=bars, freq="1D", tz="UTC", name="time")
    adv = np.exp(np.linspace(np.log(5e6), np.log(2e9), n_coins))
    sig = np.linspace(0.06, 0.03, n_coins) * rng.uniform(0.8, 1.2, n_coins)
    market = rng.normal(0.0, 0.025, bars)
    eps = rng.normal(0.0, 1.0, (bars, n_coins)) * sig
    logp = np.zeros((bars, n_coins))
    for t in range(bars):
        drift = 0.0
        if t >= 29 and edge:
            mom = logp[t - 1] - logp[t - 29]
            drift = edge * (mom - mom.mean()) / (mom.std() + 1e-12)
        log_ret = market[t] + eps[t] + drift - 0.5 * sig**2
        logp[t] = (logp[t - 1] if t else 0.0) + log_ret
    close = 100.0 * np.exp(logp)
    ar = np.zeros((bars, n_coins))
    shocks = rng.normal(0.0, 0.05, (bars, n_coins))
    for t in range(1, bars):
        ar[t] = 0.99 * ar[t - 1] + shocks[t]
    mom28 = np.vstack([np.zeros((28, n_coins)), logp[28:] - logp[:-28]])
    dv = adv * np.exp(ar + comove * mom28) * rng.uniform(0.7, 1.3, (bars, n_coins))
    data = {}
    for k in range(n_coins):
        o = np.r_[100.0, close[:-1, k]]
        c = close[:, k]
        spread = np.abs(rng.normal(0.0, sig[k], bars)) * c
        data[f"C{k:02d}USDT"] = pd.DataFrame(
            {
                "open": o,
                "high": np.maximum(o, c) + spread,
                "low": np.minimum(o, c) - spread,
                "close": c,
                "volume": dv[:, k] / c,
                "tradable": np.ones(bars, dtype=bool),
            },
            index=index,
        )
    closes = close_panel(data)
    members = closes.notna() & (np.arange(len(closes))[:, None] >= 40)
    dollar = trailing_dollar_volume(data, window_days=30)[closes.columns]
    score = np.log(closes / closes.shift(28))
    w = top_quantile_weights(
        score, members, rebalance_rows(closes.index, every=7, offset=40), quantile=0.2
    )
    return data, members, w, dollar, trailing_volatility(closes, 28)


def h3_prefer_vol(
    band: np.ndarray, target: int, vol_b: np.ndarray, counts: dict[str, int]
) -> np.ndarray:
    """Package H3's volatility preference in place of ``_prefer_vol``: restrict the band to the
    target's tercile whenever any such name is free, however few (usually the target and one
    other name, so the null copied the strategy). The negative control of the power tests."""
    same = band[vol_b[band] == vol_b[target]] if vol_b[target] >= 0 else band[:0]
    if len(same):
        counts["vol_met"] += 1
        return same
    counts["vol_missed"] += 1
    return band


POWER_NULLS = ("vol_only", "nearest", "h3")


def skill_power(
    seeds: range, bars: int, edge: float, n_paths: int, nulls: tuple[str, ...]
) -> dict[str, dict[str, np.ndarray]]:
    """Per null and seed in :func:`momentum_skill_world`: the strategy's p-value, its Sharpe
    gap over the null's mean Sharpe (copying the strategy shrinks it; less noisy than p with
    few paths) and the null's overlap. ``vol_only`` = no liquidity panel; ``nearest`` = the
    shipped nearest-rank null; ``h3`` = nearest rank with H3's volatility preference."""
    out = {n: {"p": [], "gap": [], "overlap": []} for n in nulls}
    for seed in seeds:
        data, members, w, dollar, vol = momentum_skill_world(seed, bars, edge)
        sharpe = metrics_from_returns(run_portfolio(data, w, costs=COSTS).returns, "1d").sharpe
        for name in nulls:
            assert name in POWER_NULLS, name
            liq = {} if name == "vol_only" else {"liquidity_panel": dollar}
            with pytest.MonkeyPatch.context() as mp:
                if name == "h3":
                    mp.setattr(pb, "_prefer_vol", h3_prefer_vol)
                null = random_selection_null(
                    data,
                    w,
                    members,
                    "1d",
                    costs=COSTS,
                    n_paths=n_paths,
                    seed=seed,
                    vol_panel=vol,
                    **liq,
                )
            out[name]["p"].append(null.p_value(sharpe))
            out[name]["gap"].append(sharpe - float(np.nanmean(null.sharpe)))
            out[name]["overlap"].append(null.overlap)
    return {n: {k: np.array(v) for k, v in d.items()} for n, d in out.items()}


def gap_ratio(res: dict[str, dict[str, np.ndarray]], name: str) -> float:
    """The null's mean Sharpe gap to the strategy as a share of the volatility-only null's."""
    return float(res[name]["gap"].mean() / res["vol_only"]["gap"].mean())


def test_nearest_rank_null_still_sees_a_real_selection_skill() -> None:
    """Power sanity (package H4): the band must not let the null copy the strategy and go
    blind. H1-like world with a planted causal cross-sectional momentum edge of 0.08% a day per
    unit z-score (``momentum_skill_world``), 1,200 bars, 20 paths, seeds 0..15. Measure: the
    strategy's Sharpe gap over the null's mean Sharpe, relative to the volatility-only null's
    gap (copying shrinks it). Probe: nearest rank 0.82 (blocks of 8 seeds 0.92 / 0.74), H3's
    rule 0.58 (0.64 / 0.53); overlap 0.37 vs H3 0.56 and vol-only 0.24. With so few paths the
    p-value counts barely separate the nulls (p <= 0.10: vol-only 9, nearest 9, H3 8 of 16);
    the slow test below measures power at H1's length."""
    res = skill_power(range(16), 1200, 0.0008, 20, ("vol_only", "nearest"))
    near, vol_only = res["nearest"], res["vol_only"]
    assert gap_ratio(res, "nearest") > 0.65, (near["gap"], vol_only["gap"])
    assert vol_only["overlap"].mean() < near["overlap"].mean() < 0.45, near["overlap"]
    assert (near["p"] <= 0.10).sum() >= (vol_only["p"] <= 0.10).sum() - 2, (near, vol_only)


@pytest.mark.slow
def test_nearest_rank_null_power_is_close_to_the_vol_only_null_at_h1_length_slow() -> None:
    """The quant reviewer's power case at H1's length: 2,450 bars, planted edge 0.04% a day per
    unit z-score (about +4%/yr net over the equal-weight universe), 40 paths, seeds 0..23,
    against the volatility-only null on the same seeds and H3's rule as a negative control (a
    null that copies the strategy must fail this test). Probe (two blocks of 12 seeds): gap
    ratio nearest 0.77 / 0.76, H3 0.49 / 0.60; mean p vol-only 0.139, nearest 0.165, H3 0.208;
    p <= 0.10 in 16, 12 and 9 of 24 seeds. The larger probe (60 seeds x 100 paths, module
    docstring **Overlap**): p <= 0.05 in 30/60 vol-only, 24/60 nearest, 16/60 H3."""
    res = skill_power(range(24), 2450, 0.0004, 40, POWER_NULLS)
    near, vol_only, h3 = res["nearest"], res["vol_only"], res["h3"]
    assert gap_ratio(res, "nearest") > 0.68, (near["gap"], vol_only["gap"])
    assert gap_ratio(res, "h3") < 0.65, h3["gap"]  # the test can see a copying null
    dp_near = float((near["p"] - vol_only["p"]).mean())
    dp_h3 = float((h3["p"] - vol_only["p"]).mean())
    assert dp_near < 0.05 and dp_near < dp_h3, (dp_near, dp_h3)
    assert (near["p"] <= 0.10).sum() >= (vol_only["p"] <= 0.10).sum() - 5, (near, vol_only)
