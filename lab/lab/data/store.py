"""Load lab candles from monthly Parquet, optionally topped up from the server's SQLite cache.

The Phase 5 server keeps CLOSED bars in ``server/data/app.db`` table
``candles(symbol, interval, time, open, high, low, close, volume)`` with ``time`` = bar open time
in UNIX seconds. It is opened read-only; only bars newer than the last Parquet bar are appended,
so the archive always wins on overlap. Missing bars stay absent (never forward-filled).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from lab.data.archive import _utc_now
from lab.types import CANDLE_COLUMNS, interval_seconds, validate_candles

_MONTH_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.parquet$")


def _as_utc(ts: pd.Timestamp | None, name: str) -> pd.Timestamp | None:
    if ts is None:
        return None
    try:
        out = pd.Timestamp(ts)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name} {ts!r}") from exc
    return out.tz_localize("UTC") if out.tz is None else out.tz_convert("UTC")


def _cli_hint(symbol: str, interval: str) -> str:
    return f"run `uv run python -m lab.data.archive {symbol} {interval} YYYY-MM YYYY-MM` first"


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(frame.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    out = pd.DataFrame(
        {c: frame[c].to_numpy(dtype=np.float64) for c in CANDLE_COLUMNS},
        index=pd.DatetimeIndex(idx, name="time"),
    )
    out["tradable"] = frame["tradable"].to_numpy(dtype=bool)
    return out


def _read_sqlite(
    sqlite_path: Path, symbol: str, interval: str, after_s: int | None, now: pd.Timestamp
) -> pd.DataFrame:
    path = Path(sqlite_path)
    if not path.is_file():
        raise ValueError(f"sqlite_path {str(path)!r} does not exist")
    step = interval_seconds(interval)
    # A bar is closed once its open time + interval has passed; the server stores closed bars
    # only, but guard anyway so a forming bar can never reach a backtest.
    latest_open = int(now.timestamp()) - step
    sql = (
        "SELECT time, open, high, low, close, volume FROM candles "
        "WHERE symbol = ? AND interval = ? AND time <= ?"
    )
    params: list[object] = [symbol, interval, latest_open]
    if after_s is not None:
        sql += " AND time > ?"
        params.append(after_s)
    sql += " ORDER BY time"
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read candles from sqlite {str(path)!r}: {exc}") from exc
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame()
    arr = np.asarray(rows, dtype=np.float64)
    times = arr[:, 0].astype(np.int64)
    index = pd.DatetimeIndex(pd.to_datetime(times, unit="s", utc=True), name="time")
    frame = pd.DataFrame({c: arr[:, i + 1] for i, c in enumerate(CANDLE_COLUMNS)}, index=index)
    frame["tradable"] = frame["volume"].to_numpy() > 0
    return frame


def load_candles(
    symbol: str,
    interval: str,
    *,
    parquet_dir: Path,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    sqlite_path: Path | None = None,
) -> pd.DataFrame:
    """Candles for ``symbol``/``interval`` on the half-open window ``[start, end)``.

    Concatenates ``parquet_dir/{SYMBOL}/{interval}/*.parquet``; if ``sqlite_path`` is given,
    appends closed bars from the server cache that are newer than the last Parquet bar.
    Naive ``start``/``end`` are read as UTC. Raises ``ValueError`` naming symbol/interval (and the
    download command) if no Parquet data exists.
    """
    interval_seconds(interval)
    start_ts, end_ts = _as_utc(start, "start"), _as_utc(end, "end")
    if start_ts is not None and end_ts is not None and end_ts <= start_ts:
        raise ValueError(f"end {end_ts} must be after start {start_ts}")

    folder = Path(parquet_dir) / symbol / interval
    if not folder.is_dir():
        raise ValueError(
            f"no Parquet data for {symbol} {interval} in {str(folder)!r}; "
            f"{_cli_hint(symbol, interval)}"
        )
    files = sorted(p for p in folder.iterdir() if _MONTH_FILE_RE.match(p.name))
    if not files:
        raise ValueError(
            f"no monthly Parquet files for {symbol} {interval} in {str(folder)!r}; "
            f"{_cli_hint(symbol, interval)}"
        )

    frames = [_normalise(pd.read_parquet(p)) for p in files]
    frames = [f for f in frames if len(f)]
    candles = pd.concat(frames) if frames else _normalise(pd.read_parquet(files[0]))
    candles = candles.sort_index(kind="stable")
    candles = candles[~candles.index.duplicated(keep="first")]

    if sqlite_path is not None:
        after_s = int(candles.index[-1].timestamp()) if len(candles) else None
        extra = _read_sqlite(Path(sqlite_path), symbol, interval, after_s, _utc_now())
        if len(extra):
            candles = pd.concat([candles, extra])
            candles = candles[~candles.index.duplicated(keep="first")].sort_index(kind="stable")

    if start_ts is not None:
        candles = candles[candles.index >= start_ts]
    if end_ts is not None:
        candles = candles[candles.index < end_ts]
    candles = candles.copy()
    candles.index.name = "time"
    validate_candles(candles)
    return candles


def find_gaps(candles: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Runs of missing bars. One row per gap: ``gap_start`` (last bar before), ``gap_end``
    (first bar after), ``missing_bars``. Zero-volume bars are present (untradable), not gaps.
    """
    step = interval_seconds(interval)
    validate_candles(candles)
    idx = candles.index
    if len(idx) < 2:
        return pd.DataFrame(
            {
                "gap_start": idx[:0],
                "gap_end": idx[:0],
                "missing_bars": np.array([], dtype=np.int64),
            }
        )
    deltas = (idx[1:] - idx[:-1]).total_seconds().to_numpy()
    where = np.flatnonzero(deltas > step)
    return pd.DataFrame(
        {
            "gap_start": idx[where],
            "gap_end": idx[where + 1],
            "missing_bars": (np.ceil(deltas[where] / step) - 1).astype(np.int64),
        }
    ).reset_index(drop=True)
