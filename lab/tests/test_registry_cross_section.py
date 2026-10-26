"""Cross-sectional panel features: hand-computed values, validation, and the perturbation test.

Tiny panels are written out by hand so every number can be checked on paper. Where the
implementation performs the same IEEE operations as the expected value (``c / p - 1``,
``rank / k``, ``1 / n``) the comparison is exact; standard deviations are compared with
``rtol=1e-12`` because NumPy's reference ``std`` sums in a different order.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from lab.data.universe import eligibility_mask, trailing_dollar_volume
from lab.registry import REGISTRY
from lab.registry.cross_section import (
    close_panel,
    cross_sectional_dispersion,
    cross_sectional_percentile,
    cross_sectional_rank,
    liquidity_share,
    log_returns,
    rebalance_rows,
    top_quantile_weights,
    trailing_return,
    trailing_volatility,
)
from tests.synthetic import make_candles, perturb_after

NAN = np.nan
DAYS = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC", name="time")


def bars(closes: dict[str, float]) -> pd.DataFrame:
    """Daily candles with open = high = low = close at the given dates."""
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in closes], name="time")
    c = np.asarray(list(closes.values()), dtype=np.float64)
    return pd.DataFrame(
        {
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": np.ones(len(c)),
            "tradable": np.ones(len(c), dtype=bool),
        },
        index=index,
    )


def tiny_candles() -> dict[str, pd.DataFrame]:
    """A: full history. B: hole on 01-02. C: listed on 01-03. Given out of name order."""
    return {
        "C": bars({"2024-01-03": 10.0, "2024-01-04": 20.0, "2024-01-05": 15.0}),
        "A": bars(
            {
                "2024-01-01": 100.0,
                "2024-01-02": 110.0,
                "2024-01-03": 121.0,
                "2024-01-04": 110.0,
                "2024-01-05": 99.0,
            }
        ),
        "B": bars({"2024-01-01": 50.0, "2024-01-03": 60.0, "2024-01-04": 66.0, "2024-01-05": 33.0}),
    }


def panel(data: dict[str, list[float]], index: pd.DatetimeIndex | None = None) -> pd.DataFrame:
    n = len(next(iter(data.values())))
    idx = DAYS[:n] if index is None else index
    return pd.DataFrame(data, index=idx, dtype=np.float64)


def full_mask(values: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(True, index=values.index, columns=values.columns)


def assert_same(actual: pd.DataFrame, expected: np.ndarray) -> None:
    assert actual.dtypes.eq(np.float64).all()
    assert np.array_equal(actual.to_numpy(), np.asarray(expected, dtype=np.float64), equal_nan=True)


# --------------------------------------------------------------------------- close_panel


def test_close_panel_uses_union_index_sorted_columns_and_never_fills() -> None:
    closes = close_panel(tiny_candles())
    assert list(closes.columns) == ["A", "B", "C"]
    assert closes.index.equals(DAYS)
    assert closes.index.name == "time"
    assert_same(
        closes,
        [
            [100.0, 50.0, NAN],
            [110.0, NAN, NAN],
            [121.0, 60.0, 10.0],
            [110.0, 66.0, 20.0],
            [99.0, 33.0, 15.0],
        ],
    )


def test_close_panel_empty_frame_gives_all_nan_column() -> None:
    data = tiny_candles()
    data["D"] = data["A"].iloc[:0]
    closes = close_panel(data)
    assert list(closes.columns) == ["A", "B", "C", "D"]
    assert closes["D"].isna().all()


def test_close_panel_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="non-empty mapping"):
        close_panel({})
    bad = tiny_candles()
    bad["B"] = bad["B"].drop(columns=["tradable"])
    with pytest.raises(ValueError, match="'B'"):
        close_panel(bad)
    with pytest.raises(ValueError, match="symbol keys"):
        close_panel({"": tiny_candles()["A"]})


# --------------------------------------------------------------------------- trailing_return


def test_trailing_return_one_row() -> None:
    out = trailing_return(close_panel(tiny_candles()), 1)
    assert_same(
        out,
        [
            [NAN, NAN, NAN],
            [110 / 100 - 1, NAN, NAN],
            [121 / 110 - 1, NAN, NAN],  # B's previous row is a hole
            [110 / 121 - 1, 66 / 60 - 1, 20 / 10 - 1],
            [99 / 110 - 1, 33 / 66 - 1, 15 / 20 - 1],
        ],
    )


def test_trailing_return_counts_union_rows_and_a_hole_at_either_end_is_nan() -> None:
    out = trailing_return(close_panel(tiny_candles()), 2)
    assert_same(
        out,
        [
            [NAN, NAN, NAN],
            [NAN, NAN, NAN],
            [121 / 100 - 1, 60 / 50 - 1, NAN],
            [110 / 110 - 1, NAN, NAN],  # B: start (01-02) is a hole; C: not listed yet
            [99 / 121 - 1, 33 / 60 - 1, 15 / 10 - 1],
        ],
    )


def test_trailing_return_longer_than_history_is_all_nan() -> None:
    assert trailing_return(close_panel(tiny_candles()), 5).isna().all().all()


def test_trailing_return_non_positive_or_non_finite_close_is_nan() -> None:
    closes = panel({"X": [0.0, 2.0, -1.0, 4.0, np.inf], "Y": [1.0, 2.0, 3.0, 4.0, 5.0]})
    out = trailing_return(closes, 1)
    assert_same(
        out,
        [[NAN, NAN], [NAN, 2 / 1 - 1], [NAN, 3 / 2 - 1], [NAN, 4 / 3 - 1], [NAN, 5 / 4 - 1]],
    )


@pytest.mark.parametrize("bad", [0, -1, 2.0, True, "3"])
def test_trailing_return_rejects_bad_lookback(bad: object) -> None:
    with pytest.raises(ValueError, match="lookback"):
        trailing_return(close_panel(tiny_candles()), bad)  # type: ignore[arg-type]


def test_panel_functions_reject_unsorted_or_non_datetime_index() -> None:
    closes = close_panel(tiny_candles())
    with pytest.raises(ValueError, match="closes index must be strictly increasing"):
        trailing_return(closes.iloc[::-1], 1)
    with pytest.raises(ValueError, match="closes index must be a DatetimeIndex"):
        trailing_return(closes.reset_index(drop=True), 1)
    with pytest.raises(ValueError, match="closes must hold numbers"):
        trailing_return(closes.assign(A="x"), 1)


# --------------------------------------------------------------------------- volatility


def test_log_returns_break_on_holes() -> None:
    out = log_returns(close_panel(tiny_candles()))
    b = out["B"].to_numpy()
    assert np.isnan(b[:3]).all()  # 01-01 first row; 01-02 hole; 01-03 follows the hole
    assert b[3] == np.log(66 / 60) and b[4] == np.log(33 / 66)


def test_trailing_volatility_hand_computed() -> None:
    closes = close_panel(tiny_candles())
    out = trailing_volatility(closes, 3, min_periods=2)
    a = [np.log(110 / 100), np.log(121 / 110), np.log(110 / 121), np.log(99 / 110)]
    expected = np.array(
        [
            [NAN, NAN, NAN],
            [NAN, NAN, NAN],  # A has one return so far
            [np.std(a[:2], ddof=1), NAN, NAN],
            [np.std(a[:3], ddof=1), NAN, NAN],  # B: one return (66/60) in the window
            [
                np.std(a[1:4], ddof=1),
                np.std([np.log(66 / 60), np.log(33 / 66)], ddof=1),
                np.std([np.log(20 / 10), np.log(15 / 20)], ddof=1),
            ],
        ]
    )
    got = out.to_numpy()
    assert np.array_equal(np.isnan(got), np.isnan(expected))
    np.testing.assert_allclose(got, expected, rtol=1e-12, atol=0.0)


def test_trailing_volatility_default_min_periods_is_half_lookback_rounded_up() -> None:
    closes = close_panel(tiny_candles())[["A"]]
    out = trailing_volatility(closes, 5)["A"].to_numpy()  # min_periods = 3
    assert np.isnan(out[2]) and np.isfinite(out[3])
    out4 = trailing_volatility(closes, 4)["A"].to_numpy()  # min_periods = 2
    assert np.isnan(out4[1]) and np.isfinite(out4[2])


def test_trailing_volatility_is_not_annualized() -> None:
    closes = panel({"X": [100.0, 110.0, 99.0, 108.9, 98.01]})
    out = trailing_volatility(closes, 4, min_periods=4)["X"].iloc[-1]
    r = np.diff(np.log(closes["X"].to_numpy()))
    assert out == pytest.approx(np.std(r, ddof=1), rel=1e-12)


@pytest.mark.parametrize(
    ("lookback", "min_periods", "match"),
    [(1, None, "lookback"), (3, 4, "min_periods"), (3, 1, "min_periods"), (2.5, None, "lookback")],
)
def test_trailing_volatility_rejects_bad_windows(
    lookback: object, min_periods: object, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        trailing_volatility(close_panel(tiny_candles()), lookback, min_periods=min_periods)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- rank / percentile


def test_rank_largest_is_one_among_eligible_finite_values() -> None:
    values = panel({"A": [3.0, 1.0], "B": [1.0, np.inf], "C": [2.0, 5.0], "D": [9.0, NAN]})
    mask = full_mask(values)
    mask.loc[DAYS[0], "D"] = False
    out = cross_sectional_rank(values, mask)
    assert_same(out, [[1.0, 3.0, 2.0, NAN], [2.0, NAN, 1.0, NAN]])


def test_rank_ascending_makes_smallest_best() -> None:
    values = panel({"A": [3.0], "B": [1.0], "C": [2.0]})
    out = cross_sectional_rank(values, full_mask(values), ascending=True)
    assert_same(out, [[3.0, 1.0, 2.0]])


def test_rank_ties_go_to_the_earlier_column() -> None:
    values = panel({"A": [1.0], "B": [2.0], "C": [2.0], "D": [1.0]})
    assert_same(cross_sectional_rank(values, full_mask(values)), [[3.0, 1.0, 2.0, 4.0]])


def test_rank_row_with_nothing_eligible_is_all_nan() -> None:
    values = panel({"A": [1.0], "B": [2.0]})
    mask = full_mask(values) & False
    assert cross_sectional_rank(values, mask).isna().all().all()


def test_percentile_is_rank_over_count_best_is_one_over_k() -> None:
    values = panel({"A": [3.0, 1.0], "B": [1.0, NAN], "C": [2.0, 5.0], "D": [4.0, 0.0]})
    out = cross_sectional_percentile(values, full_mask(values))
    assert_same(out, [[2 / 4, 4 / 4, 3 / 4, 1 / 4], [2 / 3, NAN, 1 / 3, 3 / 3]])


def test_rank_rejects_misaligned_or_non_bool_mask() -> None:
    values = panel({"A": [1.0], "B": [2.0]})
    mask = full_mask(values)
    with pytest.raises(ValueError, match="mask columns must equal values columns"):
        cross_sectional_rank(values, mask[["B", "A"]])
    with pytest.raises(ValueError, match="mask index must equal values index"):
        cross_sectional_rank(values, mask.set_axis(DAYS[1:2]))
    with pytest.raises(ValueError, match="mask must be a bool frame"):
        cross_sectional_rank(values, mask.astype(float))
    with pytest.raises(ValueError, match="mask must be a bool frame"):
        cross_sectional_percentile(values, mask.astype(float))


# --------------------------------------------------------------------------- schedule


def test_rebalance_rows_every_and_offset() -> None:
    idx = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
    sched = rebalance_rows(idx, every=3, offset=1)
    assert sched.dtype == bool and sched.index.equals(idx)
    assert list(np.flatnonzero(sched.to_numpy())) == [1, 4, 7]
    assert list(np.flatnonzero(rebalance_rows(idx, every=1).to_numpy())) == list(range(10))
    assert not rebalance_rows(idx, every=7, offset=10).any()


@pytest.mark.parametrize(("every", "offset"), [(0, 0), (1, -1), (2.0, 0), (True, 0)])
def test_rebalance_rows_rejects_bad_arguments(every: object, offset: object) -> None:
    idx = pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC")
    with pytest.raises(ValueError, match=r"every|offset"):
        rebalance_rows(idx, every=every, offset=offset)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="index must be a DatetimeIndex"):
        rebalance_rows(pd.RangeIndex(3), every=1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- weights


def scores5() -> pd.DataFrame:
    return panel(
        {"A": [5.0, 1.0], "B": [4.0, 2.0], "C": [3.0, 3.0], "D": [2.0, 4.0], "E": [1.0, 5.0]}
    )


def always(frame: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=frame.index, dtype=bool)


def test_weights_equal_top_quantile() -> None:
    s = scores5()
    w = top_quantile_weights(s, full_mask(s), always(s), quantile=0.4)
    assert_same(w, [[0.5, 0.5, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.5, 0.5]])


def test_weights_non_schedule_rows_are_nan_hold() -> None:
    s = scores5()
    sched = pd.Series([False, True], index=s.index, dtype=bool)
    w = top_quantile_weights(s, full_mask(s), sched, quantile=0.2)
    assert w.iloc[0].isna().all()
    assert_same(w.iloc[[1]], [[0.0, 0.0, 0.0, 0.0, 1.0]])


def test_weights_ceiling_tolerates_float_noise() -> None:
    assert 0.14 * 50 > 7  # 7.000000000000001: a naive ceil would pick 8
    cols = [f"S{i:02d}" for i in range(50)]
    s = pd.DataFrame([np.arange(50, 0, -1, dtype=float)], index=DAYS[:1], columns=cols)
    w = top_quantile_weights(s, full_mask(s), always(s), quantile=0.14)
    assert list(w.columns[w.iloc[0] > 0]) == [f"S{i:02d}" for i in range(7)]


def test_weights_min_names_floor_and_cash_when_too_few_eligible() -> None:
    s = scores5()
    mask = full_mask(s)
    w = top_quantile_weights(s, mask, always(s), quantile=0.1, min_names=2)  # ceil(0.5) = 1 -> 2
    assert_same(w, [[0.5, 0.5, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.5, 0.5]])
    mask.loc[:, ["C", "D", "E"]] = False
    mask.loc[DAYS[1], "B"] = False  # row 1: only A eligible
    w = top_quantile_weights(s, mask, always(s), quantile=0.5, min_names=2)
    assert_same(w, [[0.5, 0.5, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0, 0.0]])


def test_weights_skip_ineligible_and_non_finite_scores() -> None:
    s = scores5()
    s.loc[DAYS[1], "E"] = NAN
    mask = full_mask(s)
    mask.loc[DAYS[0], "A"] = False
    w = top_quantile_weights(s, mask, always(s), quantile=0.25)  # k = 4 -> 1 name
    assert_same(w, [[0.0, 1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0, 0.0]])


def test_weights_ties_go_to_the_earlier_column() -> None:
    s = panel({"A": [1.0], "B": [2.0], "C": [2.0], "D": [2.0]})
    w = top_quantile_weights(s, full_mask(s), always(s), quantile=0.5)
    assert_same(w, [[0.0, 0.5, 0.5, 0.0]])


def test_weights_inverse_vol_proportional_to_one_over_vol() -> None:
    s = scores5()
    vol = panel(
        {"A": [0.1, 0.1], "B": [0.2, 0.1], "C": [0.3, 0.1], "D": [0.4, 0.3], "E": [0.5, 0.1]}
    )
    w = top_quantile_weights(s, full_mask(s), always(s), quantile=0.4, inverse_vol=vol)
    # row 0: A, B with 1/0.1 = 10 and 1/0.2 = 5 -> 2/3, 1/3; row 1: E (10) and D (10/3) -> 3/4, 1/4
    expected = [[2 / 3, 1 / 3, 0, 0, 0], [0, 0, 0, 1 / 4, 3 / 4]]
    np.testing.assert_allclose(w.to_numpy(), expected, rtol=1e-15, atol=0.0)


def test_weights_symbol_without_usable_vol_is_dropped_before_ranking() -> None:
    s = scores5()
    vol = panel(
        {"A": [NAN, 0.1], "B": [0.2, 0.1], "C": [0.2, 0.1], "D": [0.2, 0.0], "E": [0.2, -1.0]}
    )
    w = top_quantile_weights(s, full_mask(s), always(s), quantile=0.25, inverse_vol=vol)
    # row 0: A unsizable -> k = 4 -> 1 name: B. row 1: D, E unsizable -> k = 3 -> 1 name: C.
    assert_same(w, [[0.0, 1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0, 0.0]])


def test_weights_cap_leaves_excess_in_cash() -> None:
    s = scores5()
    w = top_quantile_weights(s, full_mask(s), always(s), quantile=0.4, max_weight=0.3)
    assert_same(w, [[0.3, 0.3, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.3, 0.3]])
    vol = panel({c: [0.1, 0.1] for c in "ABCDE"}) * pd.Series(
        [1.0, 2.0, 1, 1, 1], index=list("ABCDE")
    )
    w = top_quantile_weights(
        s, full_mask(s), always(s), quantile=0.4, inverse_vol=vol, max_weight=0.6
    )
    np.testing.assert_allclose(w.iloc[0].to_numpy(), [0.6, 1 / 3, 0, 0, 0], rtol=1e-15, atol=0.0)
    assert w.iloc[0].sum() < 1.0


def test_weights_rows_never_sum_above_one() -> None:
    rng = np.random.default_rng(20261026)
    cols = [f"S{i}" for i in range(23)]
    idx = pd.date_range("2024-01-01", periods=300, freq="D", tz="UTC")
    s = pd.DataFrame(rng.normal(size=(300, 23)), index=idx, columns=cols)
    vol = pd.DataFrame(rng.uniform(0.001, 0.2, size=(300, 23)), index=idx, columns=cols)
    mask = pd.DataFrame(rng.random((300, 23)) > 0.3, index=idx, columns=cols)
    for q in (0.2, 1 / 3, 0.7, 1.0):
        for iv in (None, vol):
            w = top_quantile_weights(s, mask, always(s), quantile=q, inverse_vol=iv)
            assert all(math.fsum(row) <= 1.0 for row in w.to_numpy().tolist())
            assert (w.sum(axis=1) <= 1.0 + 1e-12).all()  # float sum: a few ulps over at most
            assert (w >= 0).all().all()


def test_weights_pick_matches_cross_sectional_rank() -> None:
    rng = np.random.default_rng(7)
    cols = list("ABCDEFGH")
    idx = pd.date_range("2024-01-01", periods=40, freq="D", tz="UTC")
    s = pd.DataFrame(rng.integers(0, 4, size=(40, 8)).astype(float), index=idx, columns=cols)
    mask = pd.DataFrame(rng.random((40, 8)) > 0.25, index=idx, columns=cols)
    w = top_quantile_weights(s, mask, always(s), quantile=0.3)
    rank = cross_sectional_rank(s, mask).to_numpy()
    k = np.isfinite(rank).sum(axis=1, keepdims=True)
    n = np.ceil(0.3 * k - 1e-9)
    expected_pick = np.isfinite(rank) & (rank <= n)
    assert np.array_equal(w.to_numpy() > 0, expected_pick)


def test_weights_reject_bad_arguments() -> None:
    s = scores5()
    mask, sched = full_mask(s), always(s)
    cases: list[tuple[dict[str, object], str]] = [
        ({"quantile": 0.0}, "quantile"),
        ({"quantile": 1.5}, "quantile"),
        ({"quantile": NAN}, "quantile"),
        ({"quantile": True}, "quantile"),
        ({"quantile": 0.2, "min_names": 0}, "min_names"),
        ({"quantile": 0.2, "max_weight": 0.0}, "max_weight"),
        ({"quantile": 0.2, "max_weight": 1.1}, "max_weight"),
        ({"quantile": 0.2, "inverse_vol": s[["B", "A", "C", "D", "E"]]}, "inverse_vol columns"),
    ]
    for kwargs, match in cases:
        with pytest.raises(ValueError, match=match):
            top_quantile_weights(s, mask, sched, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="schedule must be a bool Series"):
        top_quantile_weights(s, mask, sched.astype(int), quantile=0.2)
    with pytest.raises(ValueError, match="schedule index"):
        top_quantile_weights(s, mask, sched.iloc[:1], quantile=0.2)
    with pytest.raises(ValueError, match="mask columns"):
        top_quantile_weights(s, mask[list("EDCBA")], sched, quantile=0.2)


# --------------------------------------------------------------------------- no look-ahead

N_DAYS = 120
SEED = 20261026
CUTS = [20, 45, 59, 60, 61, 90, N_DAYS - 2]


def universe() -> dict[str, pd.DataFrame]:
    """Four daily coins: a hole, a suspension, a late listing; given out of name order."""
    aaa = make_candles(N_DAYS, seed=SEED, interval="1d", start="2024-01-01", vol=0.04)
    bbb = make_candles(N_DAYS - 10, seed=SEED + 1, interval="1d", start="2024-01-11", vol=0.05)
    ccc = make_candles(N_DAYS, seed=SEED + 2, interval="1d", start="2024-01-01", vol=0.03)
    ccc = ccc.drop(ccc.index[[30, 31, 32, 70]])  # holes
    ddd = make_candles(N_DAYS - 40, seed=SEED + 3, interval="1d", start="2024-02-10", vol=0.06)
    return {"DDD": ddd, "BBB": bbb, "AAA": aaa, "CCC": ccc}


def pipeline(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    closes = close_panel(data)
    mask = eligibility_mask(data, min_history_days=10)[closes.columns]
    ret = trailing_return(closes, 28)
    vol = trailing_volatility(closes, 14)
    sched = rebalance_rows(closes.index, every=7, offset=3)
    return {
        "closes": closes,
        "log_returns": log_returns(closes),
        "trailing_return": ret,
        "trailing_volatility": vol,
        "rank": cross_sectional_rank(ret, mask),
        "rank_asc": cross_sectional_rank(ret, mask, ascending=True),
        "percentile": cross_sectional_percentile(ret, mask),
        "schedule": sched.to_frame(),
        "weights_equal": top_quantile_weights(ret, mask, sched, quantile=0.5),
        "weights_invvol": top_quantile_weights(
            ret, mask, sched, quantile=0.5, inverse_vol=vol, max_weight=0.7
        ),
    }


def shock_after(data: dict[str, pd.DataFrame], cut: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """Every price after ``cut`` changed, later rows of BBB deleted, CCC truncated (delisted),
    and a new symbol EEE listed after ``cut``."""
    out = {s: perturb_after(f, int((f.index <= cut).sum()) - 1, seed=5) for s, f in data.items()}
    bbb = out["BBB"]
    later = np.flatnonzero(bbb.index > cut)
    out["BBB"] = bbb.drop(bbb.index[later[::3]])
    ccc = out["CCC"]
    out["CCC"] = ccc[ccc.index <= cut + pd.Timedelta(days=4)]
    out["EEE"] = make_candles(
        30, seed=SEED + 9, interval="1d", start=str((cut + pd.Timedelta(days=1)).date())
    )
    return out


def test_pipeline_runs_and_is_not_trivially_empty() -> None:
    out = pipeline(universe())
    assert list(out["closes"].columns) == ["AAA", "BBB", "CCC", "DDD"]
    w = out["weights_equal"]
    assert (w.fillna(0) > 0).any(axis=None)
    assert (out["weights_invvol"].fillna(0) > 0).sum(axis=1).max() >= 2
    assert out["rank"].notna().sum(axis=1).max() == 4


@pytest.mark.parametrize("t", CUTS)
def test_outputs_through_t_are_bit_identical_after_perturbing_the_future(t: int) -> None:
    data = universe()
    base = pipeline(data)
    cut = base["closes"].index[t]
    shocked = pipeline(shock_after(data, cut))
    for name, frame in base.items():
        after = shocked[name]
        assert after.index[: t + 1].equals(frame.index[: t + 1]), name
        cols = list(frame.columns)
        before = frame.iloc[: t + 1].to_numpy()
        got = after.loc[after.index[: t + 1], cols].to_numpy()
        assert np.array_equal(before, got, equal_nan=True), (
            f"{name} changed at or before t={t} after perturbing the future"
        )
        if "EEE" in after.columns:  # the late listing is invisible through t
            new = after["EEE"].to_numpy()[: t + 1]
            if name.startswith("weights"):
                sched = base["schedule"]["rebalance"].to_numpy()[: t + 1]
                assert np.array_equal(new[sched], np.zeros(sched.sum()))
                assert np.isnan(new[~sched]).all()
            else:
                assert np.isnan(new).all(), name


@pytest.mark.parametrize(
    "name", ["trailing_return", "trailing_volatility", "rank", "weights_equal"]
)
def test_perturbation_actually_changes_later_outputs(name: str) -> None:
    """Sanity: the shock moves something after t, so the test has teeth."""
    data = universe()
    base = pipeline(data)
    t = 60
    cut = base["closes"].index[t]
    shocked = pipeline(shock_after(data, cut))
    cols = list(base[name].columns)
    later = base[name].index[t + 1 :]
    a = base[name].loc[later, cols].to_numpy()
    b = shocked[name].reindex(index=later, columns=cols).to_numpy()
    assert not np.array_equal(a, b, equal_nan=True), name


def test_functions_are_deterministic_and_do_not_mutate_inputs() -> None:
    data = universe()
    snapshot = {s: f.copy() for s, f in data.items()}
    a = pipeline(data)
    b = pipeline({s: f.copy() for s, f in data.items()})
    for s, f in data.items():
        pd.testing.assert_frame_equal(f, snapshot[s])
    for name in a:
        assert np.array_equal(a[name].to_numpy(), b[name].to_numpy(), equal_nan=True), name


def test_panel_functions_are_not_in_the_single_symbol_registry() -> None:
    names = {"close_panel", "trailing_return", "cross_sectional_rank", "top_quantile_weights"}
    assert names.isdisjoint(REGISTRY)


# --------------------------------------------------------------------------- dispersion


def test_dispersion_is_sample_std_over_eligible_finite_cells() -> None:
    v = panel(
        {
            "A": [1.0, 2.0, 7.0, 1.0, NAN],
            "B": [3.0, 9.0, NAN, 2.0, 5.0],
            "C": [5.0, 4.0, 8.0, NAN, NAN],
        }
    )
    mask = full_mask(v)
    mask.loc[DAYS[1], "B"] = False  # row 1: {2, 4}
    d = cross_sectional_dispersion(v, mask)
    assert d.name == "dispersion"
    assert d.dtype == np.float64
    assert d.index.equals(v.index)
    # row 0: mean 3, squared deviations 4 + 0 + 4, / (3 - 1) = 4 -> 2.
    # row 1: mean 3, (1 + 1) / 1 = 2 -> sqrt(2). row 2: {7, 8} -> sqrt(0.5).
    # row 3: {1, 2} -> sqrt(0.5). row 4: one finite cell -> NaN.
    expected = [2.0, math.sqrt(2.0), math.sqrt(0.5), math.sqrt(0.5), NAN]
    assert np.array_equal(d.to_numpy(), np.asarray(expected), equal_nan=True)


def test_dispersion_min_names_and_empty_rows() -> None:
    v = panel({"A": [1.0, 2.0], "B": [3.0, 4.0], "C": [5.0, NAN]})
    d = cross_sectional_dispersion(v, full_mask(v), min_names=3)
    assert np.array_equal(d.to_numpy(), [2.0, NAN], equal_nan=True)
    none = pd.DataFrame(False, index=v.index, columns=v.columns)
    assert cross_sectional_dispersion(v, none).isna().all()


def test_dispersion_matches_numpy_std_and_ignores_column_order() -> None:
    rng = np.random.default_rng(7)
    idx = pd.date_range("2024-01-01", periods=40, freq="D", tz="UTC", name="time")
    v = pd.DataFrame(
        rng.normal(0.0, 0.2, (40, 12)), index=idx, columns=[f"S{j}" for j in range(12)]
    )
    mask = pd.DataFrame(rng.random((40, 12)) > 0.3, index=idx, columns=v.columns)
    d = cross_sectional_dispersion(v, mask).to_numpy()
    for i in range(40):
        x = v.to_numpy()[i][mask.to_numpy()[i]]
        ref = np.std(x, ddof=1) if x.size >= 2 else NAN
        assert np.allclose(d[i], ref, rtol=1e-12, equal_nan=True)
    cols = list(reversed(v.columns))
    flipped = cross_sectional_dispersion(v[cols], mask[cols]).to_numpy()
    assert np.array_equal(d, flipped, equal_nan=True)


def test_dispersion_rejects_bad_input() -> None:
    v = panel({"A": [1.0, 2.0], "B": [3.0, 4.0]})
    mask = full_mask(v)
    for bad in (1, 0, 2.0, True):
        with pytest.raises(ValueError, match="min_names"):
            cross_sectional_dispersion(v, mask, min_names=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="mask columns"):
        cross_sectional_dispersion(v, mask[["B", "A"]])
    with pytest.raises(ValueError, match="mask index"):
        cross_sectional_dispersion(v, mask.iloc[:1])
    with pytest.raises(ValueError, match="bool frame"):
        cross_sectional_dispersion(v, mask.astype(float))


# --------------------------------------------------------------------------- liquidity share


def test_liquidity_share_is_share_of_eligible_positive_dollar_volume() -> None:
    dv = panel(
        {
            "A": [10.0, 10.0, 0.0, NAN],
            "B": [30.0, 30.0, 25.0, -5.0],
            "C": [60.0, 60.0, 75.0, NAN],
            "D": [NAN, 5.0, NAN, NAN],
        }
    )
    mask = full_mask(dv)
    mask.loc[DAYS[1], "B"] = False
    share = liquidity_share(dv, mask)
    assert_same(
        share,
        [
            [10.0 / 100.0, 30.0 / 100.0, 60.0 / 100.0, NAN],
            [10.0 / 75.0, NAN, 60.0 / 75.0, 5.0 / 75.0],  # B masked out of the total
            [NAN, 25.0 / 100.0, 75.0 / 100.0, NAN],  # zero volume is not a share
            [NAN, NAN, NAN, NAN],  # negative / missing: nothing eligible
        ],
    )
    sums = share.sum(axis=1, min_count=1).to_numpy()
    assert np.allclose(sums[:3], 1.0, rtol=0, atol=1e-15)


def test_liquidity_share_rejects_bad_input() -> None:
    dv = panel({"A": [1.0, 2.0], "B": [3.0, 4.0]})
    mask = full_mask(dv)
    with pytest.raises(ValueError, match="mask columns"):
        liquidity_share(dv, mask[["B", "A"]])
    with pytest.raises(ValueError, match="mask index"):
        liquidity_share(dv, mask.iloc[1:])
    with pytest.raises(ValueError, match="bool frame"):
        liquidity_share(dv, mask.astype(int))
    with pytest.raises(ValueError, match="numbers only"):
        liquidity_share(dv.astype(object).assign(A=["x", "y"]), mask)


# --------------------------------------------------------------------------- banding


def banding_scores() -> pd.DataFrame:
    """Rows 0-2 and 4 are rebalances; row 3 is not (state must carry across it)."""
    return panel(
        {
            "A": [5.0, 4.0, 2.0, 9.0, 3.0],
            "B": [4.0, 3.0, 1.0, 9.0, 4.0],
            "C": [3.0, 5.0, 4.0, 9.0, 2.0],
            "D": [2.0, 2.0, 5.0, 9.0, 1.0],
            "E": [1.0, 1.0, 3.0, 9.0, 5.0],
        }
    )


def banding_schedule(s: pd.DataFrame) -> pd.Series:
    return pd.Series([True, True, True, False, True], index=s.index, dtype=bool)


def test_banding_hand_computed() -> None:
    """quantile 0.4 of 5 -> enter at rank <= 2; exit_quantile 0.8 -> leave below rank 4.

    row 0: A5 B4 C3 D2 E1 -> top 2 {A, B}.
    row 1: C5 A4 B3 D2 E1 -> top 2 {C, A}; B (rank 3) held and inside the exit band stays;
           D (rank 4) is inside the exit band but was not held, so it does not enter. 3 > n.
    row 2: D5 C4 E3 A2 B1 -> top 2 {D, C}; A (rank 4) stays; B (rank 5) leaves; E (rank 3)
           not held, does not enter.
    row 3: not a rebalance (NaN), state {D, C, A} carries over.
    row 4: E5 B4 A3 C2 D1 -> top 2 {E, B}; A (rank 3) and C (rank 4) stay; D (rank 5) leaves.
    """
    s = banding_scores()
    w = top_quantile_weights(s, full_mask(s), banding_schedule(s), quantile=0.4, exit_quantile=0.8)
    third, quarter = 1.0 / 3.0, 1.0 / 4.0
    assert_same(
        w,
        [
            [0.5, 0.5, 0.0, 0.0, 0.0],
            [third, third, third, 0.0, 0.0],
            [third, 0.0, third, third, 0.0],
            [NAN] * 5,
            [quarter, quarter, quarter, 0.0, quarter],
        ],
    )
    plain = top_quantile_weights(s, full_mask(s), banding_schedule(s), quantile=0.4)
    assert_same(
        plain,
        [
            [0.5, 0.5, 0.0, 0.0, 0.0],
            [0.5, 0.0, 0.5, 0.0, 0.0],
            [0.0, 0.0, 0.5, 0.5, 0.0],
            [NAN] * 5,
            [0.0, 0.5, 0.0, 0.0, 0.5],
        ],
    )


def test_banding_held_name_that_becomes_ineligible_leaves() -> None:
    s = banding_scores()
    mask = full_mask(s)
    mask.loc[DAYS[1], "B"] = False  # B would have stayed at rank 3
    w = top_quantile_weights(s, mask, banding_schedule(s), quantile=0.4, exit_quantile=0.8)
    # row 1 eligible: C5 A4 D2 E1 (k = 4) -> n = ceil(1.6) = 2, e = ceil(3.2) = 4: {C, A}.
    assert_same(w.iloc[[1]], [[0.5, 0.0, 0.5, 0.0, 0.0]])


def test_banding_state_resets_when_too_few_are_eligible() -> None:
    s = banding_scores()
    mask = full_mask(s)
    mask.loc[DAYS[1]] = [True, False, False, False, False]  # k = 1 < min_names = 2: cash
    w = top_quantile_weights(
        s, mask, banding_schedule(s), quantile=0.4, exit_quantile=0.8, min_names=2
    )
    assert_same(w.iloc[[1]], [[0.0] * 5])
    # row 2 starts from nothing held: just the top 2 {D, C}, A (rank 4) is not kept.
    assert_same(w.iloc[[2]], [[0.0, 0.0, 0.5, 0.5, 0.0]])


def test_banding_equal_to_quantile_is_exactly_the_plain_rule() -> None:
    rng = np.random.default_rng(11)
    idx = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC", name="time")
    cols = [f"S{j}" for j in range(9)]
    s = pd.DataFrame(rng.normal(size=(60, 9)), index=idx, columns=cols)
    mask = pd.DataFrame(rng.random((60, 9)) > 0.2, index=idx, columns=cols)
    vol = pd.DataFrame(rng.uniform(0.01, 0.05, (60, 9)), index=idx, columns=cols)
    sched = rebalance_rows(idx, every=3, offset=1)
    for q in (0.2, 1.0 / 3.0, 0.5, 1.0):
        plain = top_quantile_weights(s, mask, sched, quantile=q, inverse_vol=vol, min_names=2)
        same = top_quantile_weights(
            s, mask, sched, quantile=q, inverse_vol=vol, min_names=2, exit_quantile=q
        )
        assert np.array_equal(plain.to_numpy(), same.to_numpy(), equal_nan=True), q
        wide = top_quantile_weights(s, mask, sched, quantile=q, exit_quantile=1.0, min_names=2)
        assert ((wide.fillna(0) > 0).sum(axis=1) >= (plain.fillna(0) > 0).sum(axis=1)).all()


def test_banding_with_inverse_vol_weights_over_all_picks() -> None:
    s = banding_scores()
    vol = panel({c: [0.1, 0.2, 0.4, 0.1, 0.1] for c in "ABCDE"})
    vol["B"] = [0.1, 0.2, 0.4, 0.1, 0.1]
    vol["A"] = [0.1, 0.4, 0.4, 0.1, 0.1]
    w = top_quantile_weights(
        s, full_mask(s), banding_schedule(s), quantile=0.4, exit_quantile=0.8, inverse_vol=vol
    )
    # row 1 picks {C, A, B} in rank order with vols 0.2, 0.4, 0.2.
    inv = np.array([1 / 0.2, 1 / 0.4, 1 / 0.2])
    ref = inv / inv.sum()
    assert_same(w.iloc[[1]], [[ref[1], ref[2], ref[0], 0.0, 0.0]])


# --------------------------------------------------------------------------- liquidity cap


def test_liquidity_cap_limits_each_weight_and_leaves_excess_in_cash() -> None:
    s = panel({"A": [4.0], "B": [3.0], "C": [2.0], "D": [1.0]})
    share = panel({"A": [0.5], "B": [0.3], "C": [0.15], "D": [0.05]})
    mask, sched = full_mask(s), always(s)
    one = top_quantile_weights(
        s, mask, sched, quantile=1.0, liquidity_share=share, liquidity_cap_mult=1.0
    )
    assert_same(one, [[0.25, 0.25, 0.15, 0.05]])
    two = top_quantile_weights(
        s, mask, sched, quantile=1.0, liquidity_share=share, liquidity_cap_mult=2.0
    )
    assert_same(two, [[0.25, 0.25, 0.25, 2.0 * 0.05]])
    capped = top_quantile_weights(
        s, mask, sched, quantile=1.0, max_weight=0.2, liquidity_share=share, liquidity_cap_mult=2.0
    )
    assert_same(capped, [[0.2, 0.2, 0.2, 2.0 * 0.05]])


def test_liquidity_cap_drops_unsizable_symbol_before_ranking() -> None:
    s = panel({"A": [4.0], "B": [3.0], "C": [2.0], "D": [1.0]})
    share = panel({"A": [NAN], "B": [0.5], "C": [0.0], "D": [0.5]})
    w = top_quantile_weights(
        s, full_mask(s), always(s), quantile=0.5, liquidity_share=share, liquidity_cap_mult=5.0
    )
    # A (NaN share) and C (zero share) are not eligible: k = 2 -> n = 1 -> B.
    assert_same(w, [[0.0, 1.0, 0.0, 0.0]])


def test_liquidity_cap_off_by_default_matches_huge_multiple() -> None:
    s = scores5()
    share = liquidity_share(panel({c: [1.0, 2.0] for c in s.columns}), full_mask(s))
    plain = top_quantile_weights(s, full_mask(s), always(s), quantile=0.4)
    loose = top_quantile_weights(
        s, full_mask(s), always(s), quantile=0.4, liquidity_share=share, liquidity_cap_mult=1e6
    )
    assert np.array_equal(plain.to_numpy(), loose.to_numpy(), equal_nan=True)


# --------------------------------------------------------------------------- gate


def test_gate_off_rows_go_to_cash_and_reset_banding() -> None:
    s = banding_scores()
    gate = pd.Series([True, False, True, False, True], index=s.index, dtype=bool)
    third = 1.0 / 3.0
    w = top_quantile_weights(
        s, full_mask(s), banding_schedule(s), quantile=0.4, exit_quantile=0.8, gate=gate
    )
    assert_same(
        w,
        [
            [0.5, 0.5, 0.0, 0.0, 0.0],
            [0.0] * 5,  # gated off: all cash
            [0.0, 0.0, 0.5, 0.5, 0.0],  # nothing held, so A (rank 4) is not kept
            [NAN] * 5,  # gate ignored on a non-schedule row
            [0.0, third, third, 0.0, third],  # held {D, C}: C (rank 4) stays, D (5) leaves
        ],
    )
    all_on = pd.Series(True, index=s.index, dtype=bool)
    ungated = top_quantile_weights(s, full_mask(s), banding_schedule(s), quantile=0.4)
    on = top_quantile_weights(s, full_mask(s), banding_schedule(s), quantile=0.4, gate=all_on)
    assert np.array_equal(ungated.to_numpy(), on.to_numpy(), equal_nan=True)


def test_dispersion_gate_from_feature() -> None:
    s = panel({"A": [0.1, 0.5, 0.2], "B": [0.0, -0.5, 0.2], "C": [NAN, NAN, NAN]})
    mask = full_mask(s)
    disp = cross_sectional_dispersion(s, mask)
    gate = disp >= 0.05  # 0.0707, 0.707, 0.0 (row 2); NaN would compare False too
    w = top_quantile_weights(s, mask, always(s), quantile=0.5, gate=gate)
    assert_same(w, [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 0.0]])


# --------------------------------------------------------------------------- validation


def test_safeguard_kwargs_reject_bad_arguments() -> None:
    s = scores5()
    mask, sched = full_mask(s), always(s)
    share = liquidity_share(panel({c: [1.0, 2.0] for c in s.columns}), mask)
    good_gate = pd.Series(True, index=s.index, dtype=bool)
    cases: list[tuple[dict[str, object], str]] = [
        ({"exit_quantile": 0.1}, "exit_quantile must be >= quantile"),
        ({"exit_quantile": 1.5}, "exit_quantile"),
        ({"exit_quantile": 0.0}, "exit_quantile"),
        ({"exit_quantile": NAN}, "exit_quantile"),
        ({"exit_quantile": True}, "exit_quantile"),
        ({"liquidity_cap_mult": 2.0}, "given together"),
        ({"liquidity_share": share}, "given together"),
        ({"liquidity_share": share, "liquidity_cap_mult": 0.0}, "liquidity_cap_mult"),
        ({"liquidity_share": share, "liquidity_cap_mult": -1.0}, "liquidity_cap_mult"),
        ({"liquidity_share": share, "liquidity_cap_mult": math.inf}, "liquidity_cap_mult"),
        ({"liquidity_share": share, "liquidity_cap_mult": True}, "liquidity_cap_mult"),
        (
            {"liquidity_share": share[list("EDCBA")], "liquidity_cap_mult": 1.0},
            "liquidity_share columns",
        ),
        ({"liquidity_share": share.iloc[:1], "liquidity_cap_mult": 1.0}, "liquidity_share index"),
        ({"gate": good_gate.astype(float)}, "gate must be a bool Series"),
        (
            {"gate": pd.Series([True, np.nan], index=s.index, dtype=object)},
            "gate must be a bool Series",
        ),
        ({"gate": good_gate.to_numpy()}, "gate must be a bool Series"),
        ({"gate": good_gate.iloc[:1]}, "gate index"),
    ]
    for kwargs, match in cases:
        with pytest.raises(ValueError, match=match):
            top_quantile_weights(s, mask, sched, quantile=0.4, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- safeguard look-ahead

SAFE_DAYS = 220
SAFE_CUTS = [40, 75, 101, 102, 103, 150, SAFE_DAYS - 2]


def safeguard_universe() -> dict[str, pd.DataFrame]:
    """Eight daily coins (no ``EEE``: :func:`shock_after` lists that one) with holes, late
    listings and an early delisting, so banding sees names come and go over ~30 rebalances."""
    data: dict[str, pd.DataFrame] = {}
    names = ["AAA", "BBB", "CCC", "DDD", "FFF", "GGG", "HHH", "III"]
    for j, sym in enumerate(names):
        start = pd.Timestamp("2024-01-01") + pd.Timedelta(days=[0, 5, 0, 30, 0, 12, 60, 0][j])
        n = SAFE_DAYS - (start - pd.Timestamp("2024-01-01")).days
        frame = make_candles(
            n, seed=SEED + 20 + j, interval="1d", start=str(start.date()), vol=0.03 + 0.01 * (j % 4)
        )
        data[sym] = frame
    data["CCC"] = data["CCC"].drop(data["CCC"].index[[30, 31, 32, 70, 140]])
    data["III"] = data["III"].iloc[:130]  # delisted
    return data


def safeguard_pipeline(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    closes = close_panel(data)
    mask = eligibility_mask(data, min_history_days=10)[closes.columns]
    ret = trailing_return(closes, 28)
    vol = trailing_volatility(closes, 14)
    dv = trailing_dollar_volume(data, window_days=14)[closes.columns]
    share = liquidity_share(dv, mask)
    disp = cross_sectional_dispersion(ret, mask)
    gate = disp >= 0.12
    sched = rebalance_rows(closes.index, every=7, offset=3)
    band = {"quantile": 0.25, "exit_quantile": 0.6, "schedule": sched}
    return {
        "dispersion": disp.to_frame(),
        "gate": gate.to_frame(),
        "liquidity_share": share,
        "weights_band": top_quantile_weights(ret, mask, **band),  # type: ignore[arg-type]
        "weights_band_invvol": top_quantile_weights(
            ret,
            mask,
            inverse_vol=vol,
            max_weight=0.6,
            **band,  # type: ignore[arg-type]
        ),
        "weights_band_liq_gate": top_quantile_weights(
            ret,
            mask,
            liquidity_share=share,
            liquidity_cap_mult=2.0,
            gate=gate,
            **band,  # type: ignore[arg-type]
        ),
        "schedule": sched.to_frame(),
    }


def test_safeguard_pipeline_exercises_every_safeguard() -> None:
    out = safeguard_pipeline(safeguard_universe())
    sched = out["schedule"]["rebalance"].to_numpy()
    gate = out["gate"]["dispersion"].to_numpy()
    assert sched.sum() >= 30
    assert gate[sched].any() and not gate[sched].all(), "gate must switch on and off"
    plain = top_quantile_weights(
        *_ret_mask(safeguard_universe()), out["schedule"]["rebalance"], quantile=0.25
    )
    banded = out["weights_band"]
    counts_band = (banded.fillna(0) > 0).sum(axis=1).to_numpy()[sched]
    counts_plain = (plain.fillna(0) > 0).sum(axis=1).to_numpy()[sched]
    assert (counts_band > counts_plain).any(), "banding must hold a name outside the entry band"
    liq = out["weights_band_liq_gate"].to_numpy()[sched]
    assert np.isclose(liq.sum(axis=1), 0.0).sum() >= 1  # gated-off rows are cash
    share = out["liquidity_share"].to_numpy()[sched]
    held = liq > 0
    assert np.all(liq[held] <= 2.0 * share[held])
    assert (liq.sum(axis=1) < 1.0 - 1e-9).any(), "the liquidity cap must bind somewhere"


def _ret_mask(data: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    closes = close_panel(data)
    mask = eligibility_mask(data, min_history_days=10)[closes.columns]
    return trailing_return(closes, 28), mask


@pytest.mark.parametrize("t", SAFE_CUTS)
def test_safeguard_outputs_through_t_are_bit_identical_after_perturbing_the_future(t: int) -> None:
    data = safeguard_universe()
    base = safeguard_pipeline(data)
    cut = base["dispersion"].index[t]
    shocked = safeguard_pipeline(shock_after(data, cut))
    for name, frame in base.items():
        after = shocked[name]
        assert after.index[: t + 1].equals(frame.index[: t + 1]), name
        cols = list(frame.columns)
        before = frame.iloc[: t + 1].to_numpy()
        got = after.loc[after.index[: t + 1], cols].to_numpy()
        assert np.array_equal(before, got, equal_nan=True), (
            f"{name} changed at or before t={t} after perturbing the future"
        )
        if "EEE" in after.columns:  # the late listing is invisible through t
            new = after["EEE"].to_numpy()[: t + 1]
            if name.startswith("weights"):
                sched = base["schedule"]["rebalance"].to_numpy()[: t + 1]
                assert np.array_equal(new[sched], np.zeros(sched.sum()))
                assert np.isnan(new[~sched]).all()
            else:
                assert np.isnan(new).all(), name


@pytest.mark.parametrize(
    "name", ["dispersion", "liquidity_share", "weights_band", "weights_band_liq_gate"]
)
def test_safeguard_perturbation_actually_changes_later_outputs(name: str) -> None:
    data = safeguard_universe()
    base = safeguard_pipeline(data)
    t = 102
    cut = base["dispersion"].index[t]
    shocked = safeguard_pipeline(shock_after(data, cut))
    cols = list(base[name].columns)
    later = base[name].index[t + 1 :]
    a = base[name].loc[later, cols].to_numpy()
    b = shocked[name].reindex(index=later, columns=cols).to_numpy()
    assert not np.array_equal(a, b, equal_nan=True), name


def test_safeguards_are_deterministic_and_do_not_mutate_inputs() -> None:
    data = safeguard_universe()
    snapshot = {s: f.copy() for s, f in data.items()}
    a = safeguard_pipeline(data)
    b = safeguard_pipeline({s: f.copy() for s, f in data.items()})
    for s, f in data.items():
        pd.testing.assert_frame_equal(f, snapshot[s])
    for name in a:
        assert np.array_equal(a[name].to_numpy(), b[name].to_numpy(), equal_nan=True), name
