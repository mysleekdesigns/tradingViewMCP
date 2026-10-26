"""Portfolio benchmarks: equal-weight universe, vol-targeted BTC on the portfolio calendar,
random-selection null. Offline, synthetic candles with explicit seeds."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab.data.universe import trailing_dollar_volume
from lab.engine.costs import ImpactModel
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


def capped_p_values(seeds: range, n_coins: int, bars: int, n_paths: int) -> np.ndarray:
    pvals = []
    for seed in seeds:
        data, members, w, kw = capped_no_skill(seed, n_coins, bars)
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
