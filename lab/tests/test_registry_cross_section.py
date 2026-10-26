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

from lab.data.universe import eligibility_mask
from lab.registry import REGISTRY
from lab.registry.cross_section import (
    close_panel,
    cross_sectional_percentile,
    cross_sectional_rank,
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
