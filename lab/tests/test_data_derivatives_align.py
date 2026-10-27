"""Derivatives series aligned to spot bar closes: hand-checked cases, the shifted-timestamp case,
staleness gaps, and the perturbation (no-look-ahead) test. Offline, synthetic data."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab.data import derivatives
from lab.data.derivatives_align import (
    KLINE_CLOSE_TIME,
    METRICS_PUBLICATION_LAG,
    VISIBLE_AT,
    align_dataset,
    align_to_bar_closes,
    funding_events,
    kline_events,
    metrics_events,
)

H4 = 4 * 3600
H1 = 3600
MS = pd.Timedelta(milliseconds=1)


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def bars(start: str, n: int, seconds: int = H4) -> pd.DatetimeIndex:
    return pd.date_range(ts(start), periods=n, freq=pd.Timedelta(seconds=seconds), name="time")


def frame(times: list[pd.Timestamp], **cols: list[float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex(times, name="time")
    return pd.DataFrame({k: np.asarray(v, dtype=np.float64) for k, v in cols.items()}, index=idx)


def funding(times: list[str], rates: list[float], hours: float = 8.0) -> pd.DataFrame:
    return frame(
        [ts(t) for t in times], funding_rate=rates, funding_interval_hours=[hours] * len(rates)
    )


def klines(opens: list[pd.Timestamp], interval: pd.Timedelta, **cols: list[float]) -> pd.DataFrame:
    """Kline frame as load_series returns it: index open_time, close_time = open + iv - 1 ms."""
    out = frame(opens, **cols)
    out[KLINE_CLOSE_TIME] = pd.DatetimeIndex(opens) + interval - MS
    return out


def col(out: pd.DataFrame, name: str) -> list[float | None]:
    return [None if np.isnan(v) else float(v) for v in out[name].to_numpy()]


# --------------------------------------------------------------------------- funding


def test_funding_on_4h_bars_hand_checked() -> None:
    # Settlements at 00:00, 08:00, 16:00. 4h bars open 20:00 (prev day) .. 20:00.
    # Bar 20:00 closes 00:00 -> sees the 00:00 settlement (at the close counts).
    # Bar 00:00 closes 04:00 -> still 00:00; bar 04:00 closes 08:00 -> 08:00 settlement; etc.
    ev = funding(
        ["2024-01-02 00:00", "2024-01-02 08:00", "2024-01-02 16:00"], [0.0001, 0.0002, -0.0003]
    )
    b = bars("2024-01-01 16:00", 8)  # opens 16:00, 20:00, 00:00, ..., 20:00
    out = align_dataset("fundingRate", ev, b, H4, stale_after_intervals=2.0)
    assert col(out, "funding_rate") == [
        None,  # 16:00-20:00: before the first settlement
        0.0001,  # 20:00-00:00
        0.0001,  # 00:00-04:00
        0.0002,  # 04:00-08:00
        0.0002,  # 08:00-12:00
        -0.0003,  # 12:00-16:00
        -0.0003,  # 16:00-20:00
        -0.0003,  # 20:00-00:00 (no 00:00 Jan 3 row; 8h old, within 2 x 8h)
    ]
    assert out.index.equals(b)
    assert out[VISIBLE_AT].iloc[1] == ts("2024-01-02 00:00")
    assert pd.isna(out[VISIBLE_AT].iloc[0])
    assert list(out.columns) == ["funding_rate", "funding_interval_hours", VISIBLE_AT]
    assert out["funding_rate"].dtype == np.float64


def test_shifted_timestamp_one_millisecond_later_moves_to_next_bar() -> None:
    b = bars("2024-01-01 00:00", 4)  # closes 04:00, 08:00, 12:00, 16:00
    exact = funding(["2024-01-01 04:00"], [0.001])
    out = align_dataset("fundingRate", exact, b, H4, stale_after_intervals=2.0)
    assert col(out, "funding_rate") == [0.001, 0.001, 0.001, 0.001]

    late = frame([ts("2024-01-01 04:00") + MS], funding_rate=[0.001], funding_interval_hours=[8])
    out = align_dataset("fundingRate", late, b, H4, stale_after_intervals=2.0)
    assert col(out, "funding_rate") == [None, 0.001, 0.001, 0.001]
    assert out[VISIBLE_AT].iloc[1] == ts("2024-01-01 04:00") + MS

    early = frame([ts("2024-01-01 04:00") - MS], funding_rate=[0.001], funding_interval_hours=[8])
    out = align_dataset("fundingRate", early, b, H4, stale_after_intervals=2.0)
    assert col(out, "funding_rate")[0] == 0.001


def test_funding_staleness_gap_is_nan_not_carried() -> None:
    # 8h settlements with a missing day: the 2 x 8h = 16h limit runs out, the gap shows as NaN.
    ev = funding(["2024-01-01 00:00", "2024-01-02 08:00"], [0.0001, 0.0005])
    b = bars("2024-01-01 00:00", 10)  # closes 04:00 .. 16:00 next day (+4h steps)
    out = align_dataset("fundingRate", ev, b, H4, stale_after_intervals=2.0)
    # closes: 04,08,12,16 (age 16h ok), 20 (20h stale), 00, 04, 08 (new), 12, 16
    assert col(out, "funding_rate") == [
        0.0001,
        0.0001,
        0.0001,
        0.0001,
        None,
        None,
        None,
        0.0005,
        0.0005,
        0.0005,
    ]
    assert out[VISIBLE_AT].isna().tolist() == [False] * 4 + [True] * 3 + [False] * 3


def test_funding_interval_per_row_sets_its_own_limit() -> None:
    # A 4h-interval row goes stale after 8h at stale_after=2; an 8h row after 16h.
    ev = funding(["2024-01-01 00:00"], [0.002], hours=4.0)
    b = bars("2024-01-01 00:00", 4)  # closes 04, 08, 12, 16
    out = align_dataset("fundingRate", ev, b, H4, stale_after_intervals=2.0)
    assert col(out, "funding_rate") == [0.002, 0.002, None, None]


def test_funding_missing_interval_falls_back_to_8h() -> None:
    ev = frame([ts("2024-01-01 00:00")], funding_rate=[0.002], funding_interval_hours=[np.nan])
    events, max_age = funding_events(ev, 2.0)
    assert max_age.iloc[0] == pd.Timedelta(hours=16)
    no_col = frame([ts("2024-01-01 00:00")], funding_rate=[0.002])
    assert funding_events(no_col, 1.5)[1].iloc[0] == pd.Timedelta(hours=12)
    assert events is ev


# --------------------------------------------------------------------------- klines


def test_kline_visible_only_at_its_close() -> None:
    # 1h premium klines opening 03:00 (closes 04:00) and 04:00 (closes 05:00).
    ev = klines(
        [ts("2024-01-01 03:00"), ts("2024-01-01 04:00")],
        pd.Timedelta(hours=1),
        open=[1.0, 2.0],
        high=[1.0, 2.0],
        low=[1.0, 2.0],
        close=[0.0003, 0.0004],
    )
    b = bars("2024-01-01 00:00", 2)  # closes 04:00 and 08:00
    out = align_dataset(
        "premiumIndexKlines", ev, b, H4, interval_seconds=H1, stale_after_intervals=10.0
    )
    assert col(out, "close") == [0.0003, 0.0004]
    assert out[VISIBLE_AT].iloc[0] == ts("2024-01-01 04:00")
    assert out[VISIBLE_AT].iloc[1] == ts("2024-01-01 05:00")
    assert list(out.columns) == ["open", "high", "low", "close", VISIBLE_AT]  # close_time dropped
    # the interval is optional: visibility comes from close_time alone
    alone = align_dataset("premiumIndexKlines", ev, b, H4, stale_after_intervals=10.0)
    pd.testing.assert_frame_equal(alone, out)


def test_kline_events_shift_and_limit() -> None:
    ev = klines([ts("2024-01-01 03:00")], pd.Timedelta(hours=1), close=[1.0])
    shifted, max_age = kline_events(ev, 2.0, interval_seconds=H1)
    assert shifted.index[0] == ts("2024-01-01 04:00")  # close_time 03:59:59.999 + 1 ms
    assert list(shifted.columns) == ["close"]
    assert ev.index[0] == ts("2024-01-01 03:00")  # input untouched
    assert KLINE_CLOSE_TIME in ev.columns
    assert max_age.iloc[0] == pd.Timedelta(hours=2)
    b = bars("2024-01-01 00:00", 3, H1)  # closes 01, 02, 03: kline not closed yet
    out = align_dataset("markPriceKlines", ev, b, H1, interval_seconds=H1, stale_after_intervals=2)
    assert col(out, "close") == [None, None, None]


def test_kline_staleness_gap() -> None:
    # visible 01:00, stale after 03:00
    ev = klines([ts("2024-01-01 00:00")], pd.Timedelta(hours=1), close=[5.0])
    b = bars("2024-01-01 00:00", 4, H1)  # closes 01, 02, 03, 04
    out = align_dataset(
        "indexPriceKlines", ev, b, H1, interval_seconds=H1, stale_after_intervals=2.0
    )
    assert col(out, "close") == [5.0, 5.0, 5.0, None]


def test_daily_klines_on_4h_bars_are_visible_only_at_the_daily_close() -> None:
    # W1 probe: a daily kline opening 2024-01-01 00:00 closes 2024-01-02 00:00. On 4h bars it is
    # first visible at the sixth bar (closes 00:00 Jan 2), never at the bar closing 04:00 Jan 1.
    ev = klines([ts("2024-01-01 00:00")], pd.Timedelta(days=1), close=[0.0007])
    b = bars("2024-01-01 00:00", 6)  # closes 04, 08, 12, 16, 20, 00 (Jan 2)
    out = align_dataset("premiumIndexKlines", ev, b, H4, stale_after_intervals=2.0)
    assert col(out, "close") == [None] * 5 + [0.0007]
    assert out[VISIBLE_AT].iloc[5] == ts("2024-01-02 00:00")
    same = align_dataset(
        "premiumIndexKlines", ev, b, H4, interval_seconds=86_400, stale_after_intervals=2.0
    )
    pd.testing.assert_frame_equal(same, out)


def test_kline_interval_that_disagrees_with_close_time_raises() -> None:
    # W1 probe: daily klines declared as 4h used to become visible 20h before their close.
    ev = klines([ts("2024-01-01 00:00")], pd.Timedelta(days=1), close=[0.0007])
    b = bars("2024-01-01 00:00", 6)
    with pytest.raises(ValueError, match=r"interval_seconds=14400 disagrees with the data"):
        align_dataset(
            "premiumIndexKlines", ev, b, H4, interval_seconds=H4, stale_after_intervals=2.0
        )


def test_kline_frame_without_close_time_asks_for_a_refresh() -> None:
    old = frame([ts("2024-01-01 00:00")], close=[5.0])  # Parquet written before schema 2
    b = bars("2024-01-01", 2)
    for kw in ({}, {"interval_seconds": H4}):
        with pytest.raises(ValueError, match=r"close_time.*--refresh"):
            align_dataset("premiumIndexKlines", old, b, H4, stale_after_intervals=2, **kw)
    nat = old.copy()
    nat[KLINE_CLOSE_TIME] = pd.DatetimeIndex([pd.NaT], tz="UTC")  # load_series' old-file marker
    with pytest.raises(ValueError, match=r"1 row\(s\) without 'close_time'.*--refresh"):
        align_dataset("markPriceKlines", nat, b, H4, stale_after_intervals=2)


def test_kline_close_time_must_be_utc_and_after_open() -> None:
    b = bars("2024-01-01", 2)
    naive = frame([ts("2024-01-01 00:00")], close=[5.0])
    naive[KLINE_CLOSE_TIME] = pd.DatetimeIndex([pd.Timestamp("2024-01-01 03:59:59.999")])
    with pytest.raises(ValueError, match="tz-aware UTC"):
        align_dataset("premiumIndexKlines", naive, b, H4, stale_after_intervals=2)
    backwards = frame([ts("2024-01-01 04:00")], close=[5.0])
    backwards[KLINE_CLOSE_TIME] = pd.DatetimeIndex([ts("2024-01-01 03:00")])
    with pytest.raises(ValueError, match="close_time before its open_time"):
        align_dataset("premiumIndexKlines", backwards, b, H4, stale_after_intervals=2)


def test_kline_close_time_name_matches_the_downloader() -> None:
    assert derivatives.KLINE_CLOSE_TIME == KLINE_CLOSE_TIME


# --------------------------------------------------------------------------- metrics


def test_metrics_on_4h_bars_take_the_row_one_period_before_the_close() -> None:
    # Rows every 5 min, value = 10 x row number. A row is visible 5 min after its create_time,
    # so the 04:00 close sees the 03:55 row (#47), not the 04:00 row (#48).
    times = list(pd.date_range(ts("2024-01-01 00:00"), periods=97, freq="5min"))  # to 08:00
    oi = np.arange(97, dtype=np.float64) * 10.0
    ev = frame(times, sum_open_interest_value=list(oi))
    b = bars("2024-01-01 00:00", 3)  # closes 04:00, 08:00, 12:00
    out = align_dataset("metrics", ev, b, H4, stale_after_intervals=2.0)
    assert col(out, "sum_open_interest_value") == [470.0, 950.0, None]  # 12:00: 3h55m stale
    assert out[VISIBLE_AT].iloc[0] == ts("2024-01-01 04:00")  # the 03:55 row's visibility


def test_metrics_row_stamped_at_the_close_is_not_visible_at_that_bar() -> None:
    # W3: a 5-minute aggregate stamped at the START of its window covers close .. close+5min.
    b = bars("2024-01-01 00:00", 2)  # closes 04:00 and 08:00
    at_close = frame([ts("2024-01-01 04:00")], sum_taker_long_short_vol_ratio=[1.3])
    out = align_dataset("metrics", at_close, b, H4, stale_after_intervals=2.0)
    assert col(out, "sum_taker_long_short_vol_ratio") == [None, None]  # 08:00: 3h55m stale
    five_before = frame([ts("2024-01-01 03:55")], sum_taker_long_short_vol_ratio=[1.3])
    out = align_dataset("metrics", five_before, b, H4, stale_after_intervals=2.0)
    assert col(out, "sum_taker_long_short_vol_ratio") == [1.3, None]
    one_ms_late = frame([ts("2024-01-01 03:55") + MS], sum_taker_long_short_vol_ratio=[1.3])
    out = align_dataset("metrics", one_ms_late, b, H4, stale_after_intervals=2.0)
    assert col(out, "sum_taker_long_short_vol_ratio") == [None, None]
    assert pd.Timedelta(minutes=5) == METRICS_PUBLICATION_LAG


def test_metrics_missing_row_tolerated_within_limit() -> None:
    # The 03:55 row is missing: the 03:50 row (visible 03:55) is 5 min old at the 04:00 close,
    # within the 2 x 5 min limit measured from visibility.
    times = [ts("2024-01-01 03:45"), ts("2024-01-01 03:50"), ts("2024-01-01 04:00")]
    ev = frame(times, sum_open_interest_value=[1.0, 2.0, 3.0])
    out = align_dataset("metrics", ev, bars("2024-01-01 00:00", 1), H4, stale_after_intervals=2)
    assert col(out, "sum_open_interest_value") == [2.0]
    events, max_age = metrics_events(ev, 2.0)
    assert (max_age == pd.Timedelta(minutes=10)).all()
    assert list(events.index) == [t + pd.Timedelta(minutes=5) for t in times]
    assert ev.index[0] == ts("2024-01-01 03:45")  # input untouched


def test_metrics_staleness_measured_from_visibility() -> None:
    # Row 03:40 is visible 03:45: 15 min old at the 04:00 close > 10 min -> NaN; row 03:45
    # (visible 03:50) is exactly 10 min old -> kept.
    b = bars("2024-01-01 00:00", 1)
    old = frame([ts("2024-01-01 03:40")], sum_open_interest_value=[1.0])
    assert col(
        align_dataset("metrics", old, b, H4, stale_after_intervals=2), "sum_open_interest_value"
    ) == [None]
    edge = frame([ts("2024-01-01 03:45")], sum_open_interest_value=[1.0])
    assert col(
        align_dataset("metrics", edge, b, H4, stale_after_intervals=2), "sum_open_interest_value"
    ) == [1.0]


# --------------------------------------------------------------------------- generic


def test_empty_events_give_all_nan() -> None:
    empty = pd.DataFrame(
        {"funding_rate": pd.Series([], dtype=np.float64)},
        index=pd.DatetimeIndex([], tz="UTC", name="time"),
    )
    b = bars("2024-01-01", 5)
    out = align_to_bar_closes(empty, b, H4, max_age=pd.Timedelta(hours=8))
    assert out["funding_rate"].isna().all()
    assert out[VISIBLE_AT].isna().all()
    assert len(out) == 5
    out = align_dataset("fundingRate", empty, b, H4, stale_after_intervals=2.0)
    assert out["funding_rate"].isna().all()


def test_empty_bars() -> None:
    ev = funding(["2024-01-01 00:00"], [0.1])
    out = align_dataset(
        "fundingRate",
        ev,
        pd.DatetimeIndex([], tz="UTC"),
        H4,
        stale_after_intervals=2.0,
    )
    assert out.empty and VISIBLE_AT in out.columns


def test_scalar_max_age_and_mixed_time_units() -> None:
    ev = frame([ts("2024-01-01 00:00")], x=[1.0])
    ev.index = ev.index.as_unit("ms")
    b = bars("2024-01-01 00:00", 3).as_unit("s")
    out = align_to_bar_closes(ev, b, H4, max_age=pd.Timedelta(hours=8))
    assert col(out, "x") == [1.0, 1.0, None]  # closes 04 (4h), 08 (8h = limit), 12 (12h)


@pytest.mark.parametrize(
    ("events", "bar_times", "bar_seconds", "match"),
    [
        (
            frame([ts("2024-01-01")], x=[1.0]).tz_convert("Europe/London"),
            bars("2024-01-01", 2),
            H4,
            "events.index",
        ),
        (
            frame([ts("2024-01-01")], x=[1.0]).tz_localize(None),
            bars("2024-01-01", 2),
            H4,
            "events.index",
        ),
        (
            frame([ts("2024-01-02"), ts("2024-01-01")], x=[1.0, 2.0]),
            bars("2024-01-01", 2),
            H4,
            "events.index",
        ),
        (
            frame([ts("2024-01-01"), ts("2024-01-01")], x=[1.0, 2.0]),
            bars("2024-01-01", 2),
            H4,
            "events.index",
        ),
        (frame([ts("2024-01-01")], x=[1.0]), bars("2024-01-01", 2)[::-1], H4, "bar_times"),
        (frame([ts("2024-01-01")], x=[1.0]), bars("2024-01-01", 2).tz_localize(None), H4, "bar"),
        (frame([ts("2024-01-01")], x=[1.0]), bars("2024-01-01", 2), 0, "bar_seconds"),
        (frame([ts("2024-01-01")], x=[1.0]), bars("2024-01-01", 2), -60, "bar_seconds"),
    ],
)
def test_input_validation(
    events: pd.DataFrame, bar_times: pd.DatetimeIndex, bar_seconds: int, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        align_to_bar_closes(events, bar_times, bar_seconds, max_age=pd.Timedelta(hours=8))


def test_max_age_validation() -> None:
    ev = frame([ts("2024-01-01"), ts("2024-01-02")], x=[1.0, 2.0])
    b = bars("2024-01-01", 2)
    with pytest.raises(ValueError, match="max_age"):
        align_to_bar_closes(ev, b, H4, max_age=pd.Series([pd.Timedelta(hours=1)]))
    with pytest.raises(ValueError, match="max_age"):
        align_to_bar_closes(ev, b, H4, max_age=pd.Timedelta(hours=-1))
    with pytest.raises(ValueError, match="max_age"):
        align_to_bar_closes(ev, b, H4, max_age=pd.Series([pd.NaT, pd.NaT], index=ev.index))
    nat = pd.Series(pd.to_timedelta([np.nan, 3600.0], unit="s"), index=ev.index)
    with pytest.raises(ValueError, match="max_age contains NaT"):
        align_to_bar_closes(ev, b, H4, max_age=nat)
    with pytest.raises(ValueError, match="stale_after_intervals"):
        align_dataset("metrics", ev, b, H4, stale_after_intervals=0.0)
    with pytest.raises(ValueError, match="stale_after_intervals"):
        align_dataset("metrics", ev, b, H4, stale_after_intervals=float("nan"))
    with pytest.raises(ValueError, match="unknown derivatives dataset"):
        align_dataset("bookTicker", ev, b, H4, stale_after_intervals=2.0)


# --------------------------------------------------------------------------- look-ahead


def _random_events(rng: np.random.Generator, start: pd.Timestamp, n: int) -> pd.DataFrame:
    # Irregular visibility times (some gaps longer than the staleness limit), two columns.
    steps = rng.choice([60, 300, 300, 300, 3600, 6 * 3600], size=n)
    offs = np.cumsum(steps) * 1_000 + rng.integers(0, 1_000, size=n)  # ms, unique ascending
    idx = start + pd.to_timedelta(offs, unit="ms")
    return pd.DataFrame(
        {"a": rng.normal(size=n), "b": rng.uniform(1e6, 1e9, size=n)},
        index=pd.DatetimeIndex(idx, name="time"),
    )


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("bar_seconds", [300, H1, H4])
def test_perturbing_events_after_close_t_leaves_bars_up_to_t_bit_identical(
    seed: int, bar_seconds: int
) -> None:
    rng = np.random.default_rng(seed)
    start = ts("2024-03-01 00:00")
    ev = _random_events(rng, start, 600)
    b = pd.date_range(start, ev.index[-1], freq=pd.Timedelta(seconds=bar_seconds))
    max_age = pd.Series(pd.to_timedelta(rng.integers(600, 20_000, len(ev)), unit="s"), ev.index)
    full = align_to_bar_closes(ev, b, bar_seconds, max_age=max_age)
    assert full["a"].notna().sum() > 10 and full["a"].isna().sum() > 0  # both cases exercised

    for t in rng.choice(len(b), size=15, replace=False):
        close_t = b[t] + pd.Timedelta(seconds=bar_seconds)
        later = ev.index > close_t
        # 1) new values (and new staleness limits) for every row visible after bar t's close
        pert = ev.copy()
        pert.loc[later, "a"] = rng.normal(size=int(later.sum())) * 1e3
        pert.loc[later, "b"] = np.nan
        p_age = max_age.copy()
        p_age[later] = pd.to_timedelta(rng.integers(1, 10**6, int(later.sum())), unit="s")
        out = align_to_bar_closes(pert, b, bar_seconds, max_age=p_age)
        pd.testing.assert_frame_equal(out.iloc[: t + 1], full.iloc[: t + 1], check_exact=True)
        a_out = out["a"].to_numpy()[: t + 1].view(np.int64)
        a_full = full["a"].to_numpy()[: t + 1].view(np.int64)
        assert np.array_equal(a_out, a_full)  # bit-identical, NaN payloads included

        # 2) drop every later row and insert rows just after the close
        keep = ev.loc[~later]
        offsets = np.unique(rng.integers(1, 10**7, 5))  # ms after the close, ascending
        extra_idx = pd.DatetimeIndex(close_t + pd.to_timedelta(offsets, unit="ms"), name="time")
        k = len(extra_idx)
        extra = pd.DataFrame({"a": rng.normal(size=k), "b": rng.normal(size=k)}, index=extra_idx)
        moved = pd.concat([keep, extra]).sort_index()
        m_age = pd.Series(pd.Timedelta(hours=1), index=moved.index)
        m_age.loc[keep.index] = max_age.loc[keep.index]
        out2 = align_to_bar_closes(moved, b, bar_seconds, max_age=m_age)
        pd.testing.assert_frame_equal(out2.iloc[: t + 1], full.iloc[: t + 1], check_exact=True)


@pytest.mark.parametrize("dataset", ["fundingRate", "metrics", "premiumIndexKlines"])
def test_dataset_perturbation_uses_visibility_not_archive_time(dataset: str) -> None:
    # For klines the archive time is the open; perturbing a kline that opened before close t
    # but closes after it must not change bar t (it was never visible there).
    rng = np.random.default_rng(7)
    interval = H1
    start = ts("2024-03-01 00:00")
    n = {"fundingRate": 400, "metrics": 3000, "premiumIndexKlines": 400}[dataset]
    step = {"fundingRate": 8 * 3600, "metrics": 300, "premiumIndexKlines": interval}[dataset]
    idx = pd.date_range(start, periods=n, freq=pd.Timedelta(seconds=step), name="time")
    cols = {
        "fundingRate": ["funding_rate", "funding_interval_hours"],
        "metrics": ["sum_open_interest_value"],
        "premiumIndexKlines": ["open", "high", "low", "close"],
    }[dataset]
    data = {c: rng.normal(size=n) for c in cols}
    if dataset == "fundingRate":
        data["funding_interval_hours"] = np.full(n, 8.0)
    raw = pd.DataFrame(data, index=idx)
    if dataset == "premiumIndexKlines":
        raw[KLINE_CLOSE_TIME] = idx + pd.Timedelta(seconds=interval) - MS
    b = pd.date_range(start, idx[-1], freq=pd.Timedelta(seconds=H4))
    kw = {"interval_seconds": interval} if dataset == "premiumIndexKlines" else {}
    full = align_dataset(dataset, raw, b, H4, stale_after_intervals=2.0, **kw)
    # When each row really becomes visible: settlement; create_time + lag; close_time + 1 ms.
    visible = {
        "fundingRate": raw.index,
        "metrics": raw.index + METRICS_PUBLICATION_LAG,
        "premiumIndexKlines": pd.DatetimeIndex(raw.get(KLINE_CLOSE_TIME, raw.index)) + MS,
    }[dataset]
    for t in rng.choice(len(b), size=20, replace=False):
        close_t = b[t] + pd.Timedelta(seconds=H4)
        later = visible > close_t
        pert = raw.copy()
        pert.loc[later, cols[0]] = rng.normal(size=int(later.sum())) * 1e6
        out = align_dataset(dataset, pert, b, H4, stale_after_intervals=2.0, **kw)
        pd.testing.assert_frame_equal(out.iloc[: t + 1], full.iloc[: t + 1], check_exact=True)
        if t + 1 < len(b) - 2:
            assert not out[cols[0]].iloc[t + 1 :].equals(full[cols[0]].iloc[t + 1 :])
