"""Point-in-time universe: which symbols were eligible to trade at each bar.

A symbol is eligible at bar t only if it printed a bar at t, that bar is tradable (non-zero
volume), and its first bar in the data is at least ``min_history_days`` before t. With
``relist_gap_days`` set, a hole longer than that restarts the clock: Binance delists and relists
pairs and reuse tickers, so history before the hole is a different market. With
``relist_jump`` set, a shorter hole also restarts it when the first open after the hole is more
than that factor away from the last close before it: a redenomination or token swap under the
same ticker (BNX 1:100, COCOS 1000:1, QUICK 1:1000) or a quick reuse (LUNAUSDT was Terra Classic
until 13 May 2022 and Terra 2.0 from 31 May). Both are known at the first bar after the hole.
Everything is decided from rows at or before t, never from today's listing, so altering or
removing data after t cannot change eligibility at t.

Liquidity ranking (:func:`trailing_dollar_volume`, :func:`liquidity_rank`) uses the same rule:
the rank at t comes from bars in ``(t - window, t]`` only.

Note: "first bar" is the first bar in the frames you pass. If the download starts after a coin's
real listing, eligibility starts later than strictly necessary; that errs on the safe side.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from lab.types import interval_seconds, validate_candles


def _validated(candles_by_symbol: Mapping[str, pd.DataFrame]) -> None:
    for symbol, frame in candles_by_symbol.items():
        try:
            validate_candles(frame)
        except ValueError as exc:
            raise ValueError(f"candles for {symbol!r}: {exc}") from exc


def _union_index(candles_by_symbol: Mapping[str, pd.DataFrame]) -> pd.DatetimeIndex:
    indexes = [f.index for f in candles_by_symbol.values() if len(f)]
    if not indexes:
        return pd.DatetimeIndex([], tz="UTC", name="time")
    union = indexes[0]
    for other in indexes[1:]:
        union = union.union(other)
    return pd.DatetimeIndex(union, name="time")


def _check_relist(
    relist_gap_days: int | None, relist_jump: float | None, interval: str | None
) -> None:
    if relist_gap_days is not None and relist_gap_days < 1:
        raise ValueError(f"relist_gap_days must be >= 1 or None, got {relist_gap_days}")
    if relist_jump is not None and not relist_jump > 1.0:
        raise ValueError(f"relist_jump must be > 1 or None, got {relist_jump}")
    if relist_jump is not None and interval is None:
        raise ValueError("relist_jump needs the bar interval (e.g. '1d') to know what a hole is")
    if interval is not None:
        interval_seconds(interval)


def segment_starts(
    candles: pd.DataFrame,
    *,
    relist_gap_days: int | None,
    relist_jump: float | None = None,
    interval: str | None = None,
) -> pd.DatetimeIndex:
    """For each bar, the first bar of its listing segment: the first bar overall, or the first
    bar after a hole longer than ``relist_gap_days``, or after any hole (a step longer than one
    ``interval``) across which open-after / close-before moved more than ``relist_jump``x either
    way. ``None`` disables a rule. Uses the bar itself and the bar before it only.
    """
    _check_relist(relist_gap_days, relist_jump, interval)
    index = candles.index
    if not len(index):
        return pd.DatetimeIndex([], tz="UTC", name="time")
    starts = np.zeros(len(index), dtype=bool)
    starts[0] = True
    if len(index) > 1:
        step = index[1:] - index[:-1]  # Timedelta arithmetic: safe for any datetime unit
        if relist_gap_days is not None:
            starts[1:] |= np.asarray(step > pd.Timedelta(days=relist_gap_days), dtype=bool)
        if relist_jump is not None:
            bar = pd.Timedelta(seconds=interval_seconds(str(interval)))
            hole = np.asarray(step > bar, dtype=bool)  # nominal bar, never a whole-frame statistic
            opens = candles["open"].to_numpy(dtype=np.float64)[1:]
            closes = candles["close"].to_numpy(dtype=np.float64)[:-1]
            with np.errstate(divide="ignore", invalid="ignore"):
                moved = np.abs(np.log(opens / closes)) > np.log(relist_jump)
            starts[1:] |= hole & moved
    marked = pd.Series(index.where(starts), index=index).ffill()
    return pd.DatetimeIndex(marked, name="time")


def eligibility_mask(
    candles_by_symbol: Mapping[str, pd.DataFrame],
    *,
    min_history_days: int = 365,
    relist_gap_days: int | None = None,
    relist_jump: float | None = None,
    interval: str | None = None,
) -> pd.DataFrame:
    """Bool frame (index = union of bar times, columns = symbols) of point-in-time eligibility."""
    if min_history_days < 0:
        raise ValueError(f"min_history_days must be >= 0, got {min_history_days}")
    _check_relist(relist_gap_days, relist_jump, interval)
    _validated(candles_by_symbol)
    union = _union_index(candles_by_symbol)

    history = pd.Timedelta(days=min_history_days)
    columns: dict[str, np.ndarray] = {}
    for symbol, frame in candles_by_symbol.items():
        if not len(frame):
            columns[symbol] = np.zeros(len(union), dtype=bool)
            continue
        starts = segment_starts(
            frame, relist_gap_days=relist_gap_days, relist_jump=relist_jump, interval=interval
        )
        seasoned = frame.index >= starts + history
        ok = pd.Series(frame["tradable"].to_numpy(dtype=bool) & seasoned, index=frame.index)
        columns[symbol] = ok.reindex(union, fill_value=False).to_numpy(dtype=bool)
    return pd.DataFrame(columns, index=union, dtype=bool)


def trailing_dollar_volume(
    candles_by_symbol: Mapping[str, pd.DataFrame], *, window_days: int
) -> pd.DataFrame:
    """Median of ``close * volume`` over each symbol's own bars in ``(t - window_days, t]``, on
    the union index; NaN where the symbol has no bar at t. Zero-volume bars count as zero.

    For a ``*USDT`` pair this is daily quote volume in USDT, up to the bar's intraday VWAP drift.
    """
    if window_days < 1:
        raise ValueError(f"window_days must be >= 1, got {window_days}")
    _validated(candles_by_symbol)
    union = _union_index(candles_by_symbol)
    window = f"{int(window_days)}D"
    columns: dict[str, pd.Series] = {}
    for symbol, frame in candles_by_symbol.items():
        dollar = (frame["close"] * frame["volume"]).astype(np.float64)
        columns[symbol] = dollar.rolling(window).median().reindex(union)
    return pd.DataFrame(columns, index=union, dtype=np.float64)


def liquidity_rank(dollar_volume: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """Rank by trailing dollar volume among symbols eligible at each bar (1 = most liquid);
    NaN where a symbol is not eligible. Ties go to the earlier column."""
    if list(dollar_volume.columns) != list(mask.columns):
        raise ValueError("dollar_volume and mask must have the same columns in the same order")
    dv = dollar_volume.reindex(mask.index).where(mask.astype(bool))
    return dv.rank(axis=1, ascending=False, method="first")


def top_n_members(rank: pd.DataFrame, n: int) -> pd.DataFrame:
    """Bool frame: True where a symbol ranks in the top ``n`` at that bar."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    return rank.le(n)


def listing_table(
    candles_by_symbol: Mapping[str, pd.DataFrame],
    *,
    relist_gap_days: int | None,
    delisted_after_days: int,
    relist_jump: float | None = None,
    interval: str | None = None,
) -> pd.DataFrame:
    """One row per symbol: ``first_bar``, ``last_bar``, ``bars``, ``segments`` (listings split
    by holes over ``relist_gap_days``) and ``delisted`` (no bar in the last
    ``delisted_after_days`` before the newest bar of any symbol). A data summary, not a signal:
    ``delisted`` looks at the end of the data, so never use it to pick members at an earlier t.
    """
    if delisted_after_days < 0:
        raise ValueError(f"delisted_after_days must be >= 0, got {delisted_after_days}")
    union = _union_index(candles_by_symbol)
    end = union[-1] if len(union) else None
    rows = []
    for symbol, frame in candles_by_symbol.items():
        if not len(frame):
            rows.append((symbol, pd.NaT, pd.NaT, 0, 0, False))
            continue
        starts = segment_starts(
            frame, relist_gap_days=relist_gap_days, relist_jump=relist_jump, interval=interval
        )
        segments = int(starts.nunique())
        last = frame.index[-1]
        gone = end is not None and last < end - pd.Timedelta(days=delisted_after_days)
        rows.append((symbol, frame.index[0], last, len(frame), segments, bool(gone)))
    return pd.DataFrame(
        rows, columns=["symbol", "first_bar", "last_bar", "bars", "segments", "delisted"]
    ).set_index("symbol")


def eligible_at(mask: pd.DataFrame, at: pd.Timestamp) -> list[str]:
    """Symbols eligible at bar time ``at`` (column order). ``[]`` if no symbol has a bar there.

    A naive ``at`` is read as UTC.
    """
    try:
        ts = pd.Timestamp(at)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid at {at!r}") from exc
    ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    if ts not in mask.index:
        return []
    row = mask.loc[ts]
    return [str(c) for c in mask.columns if bool(row[c])]
