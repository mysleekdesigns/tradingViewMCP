"""Point-in-time universe: which symbols were eligible to trade at each bar.

A symbol is eligible at bar t only if it printed a bar at t, that bar is tradable (non-zero
volume), and its first bar in the data is at least ``min_history_days`` before t. Everything is
decided from rows at or before t, never from today's listing, so altering or removing data after
t cannot change eligibility at t.

Note: "first bar" is the first bar in the frames you pass. If the download starts after a coin's
real listing, eligibility starts later than strictly necessary; that errs on the safe side.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from lab.types import validate_candles


def eligibility_mask(
    candles_by_symbol: Mapping[str, pd.DataFrame], *, min_history_days: int = 365
) -> pd.DataFrame:
    """Bool frame (index = union of bar times, columns = symbols) of point-in-time eligibility."""
    if min_history_days < 0:
        raise ValueError(f"min_history_days must be >= 0, got {min_history_days}")
    for symbol, frame in candles_by_symbol.items():
        try:
            validate_candles(frame)
        except ValueError as exc:
            raise ValueError(f"candles for {symbol!r}: {exc}") from exc

    indexes = [f.index for f in candles_by_symbol.values() if len(f)]
    if indexes:
        union = indexes[0]
        for other in indexes[1:]:
            union = union.union(other)
        union = pd.DatetimeIndex(union, name="time")
    else:
        union = pd.DatetimeIndex([], tz="UTC", name="time")

    history = pd.Timedelta(days=min_history_days)
    columns: dict[str, np.ndarray] = {}
    for symbol, frame in candles_by_symbol.items():
        if not len(frame):
            columns[symbol] = np.zeros(len(union), dtype=bool)
            continue
        first = frame.index[0]
        tradable = frame["tradable"].astype(bool).reindex(union, fill_value=False)
        seasoned = union >= first + history
        columns[symbol] = tradable.to_numpy(dtype=bool) & np.asarray(seasoned, dtype=bool)
    return pd.DataFrame(columns, index=union, dtype=bool)


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
