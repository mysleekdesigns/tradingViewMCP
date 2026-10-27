"""Align USDⓈ-M derivatives series to spot bar closes, point in time (PLAN.md Phase 12).

A spot bar opening at ``t`` with length ``bar_seconds`` closes at ``c = t + bar_seconds``. A
derivatives row is *visible* from a moment fixed by the archive's own timestamp:

- ``fundingRate``: the settlement time (``calc_time``) - the rate is known once it settles;
- ``premiumIndexKlines`` / ``markPriceKlines`` / ``indexPriceKlines``: the kline's *close*, taken
  from the DATA: its own ``close_time`` column + 1 ms (== ``open_time + interval`` for Binance).
  Its close/high/low are not known before then. Visibility never depends on an interval passed
  separately from the data (a daily frame aligned "as 4h" would otherwise see a kline up to 20h
  before it closes); a passed ``interval_seconds`` is only cross-checked, and a frame without
  ``close_time`` (Parquet written before derivatives schema 2) is refused;
- ``metrics``: ``create_time + METRICS_PUBLICATION_LAG`` (5 minutes). Binance does not document
  whether a 5-minute aggregate row (e.g. ``sum_taker_long_short_vol_ratio``) is stamped at the
  start or the end of its window. Stamped at the start, the row describes trading up to
  ``create_time + 5 min``, so treating it as visible at ``create_time`` would leak up to five
  minutes of the future into the bar closing at ``create_time``. We assume the worst case; it
  costs at most 5 minutes of staleness (nothing on 4h bars).

Each bar takes the last row whose visibility time is at or before its close (an event exactly
at the close is visible at that bar; one a millisecond later is not). A row older than its
staleness limit (``stale_after_intervals`` x the series' own interval) is not carried across an
archive hole: the bar gets NaN, so a gap stays a gap. Bars before the first visible row are NaN.

Known, intentional consequences (decisions for the owner before funding features exist):

(a) Real BTCUSDT ``calc_time`` is often 1-47 ms after the hour (about 45% of settlements). Under
    the strict ``close >= visibility`` rule such a settlement is visible one bar later than the
    hour suggests (e.g. the 08:00:00.002 rate first shows at the bar closing 12:00 on 4h bars).
    That is honest - the rate did not exist at 08:00:00.000 - and is left as is; rounding
    ``calc_time`` down would be a (tiny) look-ahead.
(b) After a funding-interval switch from 8h to 4h, the last 8h row keeps its own 16h staleness
    limit (at ``stale_after_intervals = 2``), so it can bridge one or more missed 4h settlements.
    The next interval is not knowable point in time from the last row, so the limit cannot be
    tightened without look-ahead.

Pure: frames in, frames out. No I/O, no globals, no look-ahead (appending or changing rows that
become visible after a bar's close never changes that bar's output; see the perturbation test).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

__all__ = [
    "FUNDING_DATASET",
    "FUNDING_FALLBACK_INTERVAL",
    "KLINE_CLOSE_TIME",
    "KLINE_DATASETS",
    "KLINE_VISIBLE_AFTER_CLOSE_TIME",
    "METRICS_DATASET",
    "METRICS_INTERVAL",
    "METRICS_PUBLICATION_LAG",
    "VISIBLE_AT",
    "align_dataset",
    "align_to_bar_closes",
    "funding_events",
    "kline_events",
    "metrics_events",
]

FUNDING_DATASET = "fundingRate"
METRICS_DATASET = "metrics"
KLINE_DATASETS: tuple[str, ...] = ("premiumIndexKlines", "markPriceKlines", "indexPriceKlines")
METRICS_INTERVAL = pd.Timedelta(minutes=5)
# metrics rows become visible one period after create_time: whether a 5-minute aggregate is
# stamped at the start or end of its window is unverified, so assume the start (no leak either
# way); costs at most 5 minutes of staleness, nothing on 4h bars.
METRICS_PUBLICATION_LAG = pd.Timedelta(minutes=5)
# A kline's close_time is its last millisecond; it is complete (visible) one millisecond later.
KLINE_CLOSE_TIME = "close_time"  # same name as lab.data.derivatives.KLINE_CLOSE_TIME
KLINE_VISIBLE_AFTER_CLOSE_TIME = pd.Timedelta(milliseconds=1)
FUNDING_FALLBACK_INTERVAL = pd.Timedelta(hours=8)
VISIBLE_AT = "visible_at"

_UTC_NAMES = frozenset({"UTC", "Etc/UTC", "UTC+00:00", "Etc/GMT", "GMT"})


def _check_utc_index(idx: object, name: str) -> pd.DatetimeIndex:
    if not isinstance(idx, pd.DatetimeIndex):
        raise ValueError(f"{name} must be a pandas DatetimeIndex, got {type(idx).__name__}")
    if idx.tz is None or str(idx.tz) not in _UTC_NAMES:
        raise ValueError(f"{name} must be tz-aware UTC, got tz={idx.tz!r}")
    if idx.hasnans:
        raise ValueError(f"{name} contains NaT")
    if not idx.is_monotonic_increasing:
        raise ValueError(f"{name} must be sorted ascending")
    if not idx.is_unique:
        raise ValueError(f"{name} must not contain duplicate timestamps")
    return idx.as_unit("ns")


def _check_stale_after(stale_after_intervals: float) -> float:
    value = float(stale_after_intervals)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"stale_after_intervals must be a positive finite number, got {value!r}")
    return value


def _ns(td: pd.Timedelta) -> np.int64:
    return np.int64(pd.Timedelta(td).as_unit("ns").value)


def align_to_bar_closes(
    events: pd.DataFrame,
    bar_times: pd.DatetimeIndex,
    bar_seconds: int,
    *,
    max_age: pd.Timedelta | pd.Series,
) -> pd.DataFrame:
    """For each bar, the last event visible at or before the bar's close, or NaN.

    ``events``: index = the UTC time each row becomes visible (sorted, unique); numeric columns.
    ``bar_times``: spot bar *open* times (UTC, sorted, unique); bar close = open + ``bar_seconds``.
    ``max_age``: the staleness limit, either one Timedelta or a Series indexed like ``events``
    giving each row's own limit. A bar whose latest visible row is older than that row's limit
    (``close - visible_at > max_age``) gets NaN.

    Returns a frame indexed by ``bar_times`` with the events' columns as float64 plus
    ``visible_at`` (the chosen row's visibility time; NaT where the bar is NaN).
    """
    if not isinstance(events, pd.DataFrame):
        raise ValueError(f"events must be a pandas DataFrame, got {type(events).__name__}")
    ev_idx = _check_utc_index(events.index, "events.index")
    bars = _check_utc_index(bar_times, "bar_times")
    if isinstance(bar_seconds, bool) or not isinstance(bar_seconds, int | np.integer):
        raise ValueError(f"bar_seconds must be an integer, got {bar_seconds!r}")
    if bar_seconds <= 0:
        raise ValueError(f"bar_seconds must be > 0, got {bar_seconds}")
    if VISIBLE_AT in events.columns:
        raise ValueError(f"events must not already have a {VISIBLE_AT!r} column")
    try:
        values = events.to_numpy(dtype=np.float64, copy=True, na_value=np.nan)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"events columns must be numeric: {exc}") from exc
    values = values.reshape(len(events), len(events.columns))

    n_ev = len(ev_idx)
    if isinstance(max_age, pd.Series):
        if len(max_age) != n_ev or not max_age.index.equals(events.index):
            raise ValueError("max_age Series must be indexed exactly like events")
        try:
            ages = pd.to_timedelta(max_age)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"max_age Series must hold Timedeltas: {exc}") from exc
        if ages.isna().any():
            raise ValueError("max_age contains NaT")
        limits = ages.dt.as_unit("ns").to_numpy().astype(np.int64)
    elif isinstance(max_age, pd.Timedelta):
        if pd.isna(max_age):
            raise ValueError("max_age is NaT")
        limits = np.full(n_ev, _ns(max_age), dtype=np.int64)
    else:
        raise ValueError(f"max_age must be a Timedelta or a Series, got {type(max_age).__name__}")
    if (limits < 0).any():
        raise ValueError("max_age must be non-negative")

    ev_ns = ev_idx.asi8
    close_ns = bars.asi8 + np.int64(bar_seconds) * np.int64(1_000_000_000)
    # side="right": an event exactly at the close counts as visible at that bar.
    pos = np.searchsorted(ev_ns, close_ns, side="right") - 1
    has = pos >= 0
    safe = np.where(has, pos, 0)
    if n_ev:
        age = close_ns - ev_ns[safe]
        ok = has & (age <= limits[safe])
    else:
        ok = np.zeros(len(bars), dtype=bool)

    out_vals = np.full((len(bars), values.shape[1]), np.nan, dtype=np.float64)
    if n_ev and ok.any():
        out_vals[ok] = values[pos[ok]]
    out = pd.DataFrame(out_vals, index=bar_times, columns=events.columns)
    vis = np.full(len(bars), np.iinfo(np.int64).min, dtype=np.int64)  # NaT
    if n_ev:
        vis[ok] = ev_ns[pos[ok]]
    out[VISIBLE_AT] = pd.DatetimeIndex(vis.view("M8[ns]"), tz="UTC")
    return out


def funding_events(
    frame: pd.DataFrame, stale_after_intervals: float
) -> tuple[pd.DataFrame, pd.Series]:
    """``fundingRate`` rows are visible at settlement (their own ``calc_time`` index).

    Staleness limit per row: ``stale_after_intervals x funding_interval_hours`` (8h when the
    interval is missing), so a skipped settlement shows as a gap instead of an old rate.
    """
    stale = _check_stale_after(stale_after_intervals)
    _check_utc_index(frame.index, "funding frame index")
    if "funding_interval_hours" in frame.columns:
        hours = pd.to_numeric(frame["funding_interval_hours"], errors="coerce").astype(np.float64)
        hours = hours.where(hours > 0)
    else:
        hours = pd.Series(np.nan, index=frame.index, dtype=np.float64)
    fallback_h = FUNDING_FALLBACK_INTERVAL / pd.Timedelta(hours=1)
    hours = hours.fillna(fallback_h)
    max_age = pd.to_timedelta(hours.to_numpy() * stale, unit="h")
    return frame, pd.Series(max_age, index=frame.index, name="max_age")


def _refresh_hint(what: str) -> str:
    return (
        f"kline frame {what}: it was probably written before derivatives schema 2. Re-download "
        "the series with `uv run python -m lab.data.derivatives download SYMBOL --datasets "
        "<kline dataset> --interval <iv> --start YYYY-MM --end YYYY-MM --refresh`"
    )


def kline_events(
    frame: pd.DataFrame, stale_after_intervals: float, *, interval_seconds: int | None = None
) -> tuple[pd.DataFrame, pd.Series]:
    """A derivatives kline (index = ``open_time``) is visible only once it has closed, at its own
    ``close_time + 1 ms`` (``close_time`` column, ``datetime64[ns, UTC]``), read from the data.

    ``interval_seconds``, if given, is only a cross-check: ``ValueError`` when any row's
    ``close_time + 1 ms - open_time`` differs from it. A frame without ``close_time`` (or with
    NaT there) raises ``ValueError`` asking for a ``--refresh`` re-download; there is no fallback
    to a passed interval. Staleness limit per row: ``stale_after_intervals x`` its own duration.
    The returned events drop ``close_time`` (index = visibility time).
    """
    stale = _check_stale_after(stale_after_intervals)
    if interval_seconds is not None:
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int | np.integer):
            raise ValueError(f"interval_seconds must be an integer, got {interval_seconds!r}")
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be > 0, got {interval_seconds}")
    idx = _check_utc_index(frame.index, "kline frame index")
    if KLINE_CLOSE_TIME not in frame.columns:
        raise ValueError(_refresh_hint(f"has no {KLINE_CLOSE_TIME!r} column"))
    col = frame[KLINE_CLOSE_TIME]
    if not isinstance(col.dtype, pd.DatetimeTZDtype) or str(col.dtype.tz) not in _UTC_NAMES:
        raise ValueError(
            f"kline {KLINE_CLOSE_TIME!r} must be datetime64 tz-aware UTC, got {col.dtype}"
        )
    close = pd.DatetimeIndex(col).as_unit("ns")
    if close.hasnans:
        n_missing = int(close.isna().sum())
        raise ValueError(_refresh_hint(f"has {n_missing} row(s) without {KLINE_CLOSE_TIME!r}"))
    visible = close + KLINE_VISIBLE_AFTER_CLOSE_TIME
    duration = pd.TimedeltaIndex(visible - idx)
    if (duration <= pd.Timedelta(0)).any():
        first = idx[np.flatnonzero(duration <= pd.Timedelta(0))[0]]
        raise ValueError(f"kline opening {first.isoformat()} has close_time before its open_time")
    if interval_seconds is not None:
        expected = pd.Timedelta(seconds=int(interval_seconds))
        bad = np.flatnonzero(duration != expected)
        if len(bad):
            i = bad[0]
            raise ValueError(
                f"interval_seconds={int(interval_seconds)} disagrees with the data: the kline "
                f"opening {idx[i].isoformat()} closes at {visible[i].isoformat()} "
                f"({duration[i]} long; {len(bad)} row(s) disagree)"
            )
    events = frame.drop(columns=[KLINE_CLOSE_TIME])
    events.index = pd.DatetimeIndex(visible, name=frame.index.name)
    max_age = pd.Series(duration * stale, index=events.index, name="max_age")
    return events, max_age


def metrics_events(
    frame: pd.DataFrame, stale_after_intervals: float
) -> tuple[pd.DataFrame, pd.Series]:
    """``metrics`` rows (open interest, long/short ratios) are visible at
    ``create_time + METRICS_PUBLICATION_LAG`` (see the module docstring). Staleness limit,
    measured from that visibility time: ``stale_after_intervals x 5 minutes``."""
    stale = _check_stale_after(stale_after_intervals)
    idx = _check_utc_index(frame.index, "metrics frame index")
    events = frame.copy()
    events.index = (idx + METRICS_PUBLICATION_LAG).rename(frame.index.name)
    limit = METRICS_INTERVAL * stale
    return events, pd.Series(limit, index=events.index, name="max_age")


def align_dataset(
    dataset: str,
    frame: pd.DataFrame,
    bar_times: pd.DatetimeIndex,
    bar_seconds: int,
    *,
    interval_seconds: int | None = None,
    stale_after_intervals: float,
) -> pd.DataFrame:
    """Align one archive frame (as ``lab.data.derivatives.load_series`` returns it) to spot bar
    closes. ``*Klines`` visibility comes from the frame's own ``close_time``; ``interval_seconds``
    (optional) is cross-checked against it (``ValueError`` on disagreement)."""
    if dataset == FUNDING_DATASET:
        events, max_age = funding_events(frame, stale_after_intervals)
    elif dataset == METRICS_DATASET:
        events, max_age = metrics_events(frame, stale_after_intervals)
    elif dataset in KLINE_DATASETS:
        events, max_age = kline_events(
            frame, stale_after_intervals, interval_seconds=interval_seconds
        )
    else:
        known = (FUNDING_DATASET, *KLINE_DATASETS, METRICS_DATASET)
        raise ValueError(f"unknown derivatives dataset {dataset!r}; expected one of {known}")
    return align_to_bar_closes(events, bar_times, bar_seconds, max_age=max_age)
