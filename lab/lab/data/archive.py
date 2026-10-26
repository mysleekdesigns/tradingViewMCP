"""Binance public archive (``data.binance.vision``) -> monthly Parquet files.

Monthly kline zips live at
``{base}/monthly/klines/{SYMBOL}/{interval}/{SYMBOL}-{interval}-{YYYY-MM}.zip``
with a sibling ``.CHECKSUM`` file (``<sha256>  <filename>``). Each zip holds one CSV with the
REST kline columns and normally no header. Newer spot files store times in microseconds instead of
milliseconds, so units are detected per value by magnitude.

Output: ``{out_dir}/{SYMBOL}/{interval}/{YYYY-MM}.parquet`` in the ``lab.types`` candle convention.
Missing bars stay absent (never forward-filled); zero-volume bars are kept with ``tradable=False``.

CLI: ``uv run python -m lab.data.archive BTCUSDT 4h 2019-01 2026-09``. Market data needs no API key.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from lab.types import CANDLE_COLUMNS, interval_seconds, validate_candles

KLINE_CSV_COLUMNS: tuple[str, ...] = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_base",
    "taker_quote",
    "ignore",
)
_US_THRESHOLD = 1e14  # ms timestamps are ~1.7e12 today; µs are ~1.7e15
# Letters and digits only (no "/", ".", "_" or spaces, so a symbol is a safe path segment); any
# script, because Binance lists coins such as 币安人生USDT; no lowercase.
_SYMBOL_RE = re.compile(r"^[^\W_]{2,30}$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _check_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not _SYMBOL_RE.match(symbol) or symbol != symbol.upper():
        raise ValueError(
            f"invalid symbol {symbol!r}; expected an uppercase Binance symbol like BTCUSDT"
        )
    return symbol


def _check_month(year: int, month: int) -> None:
    if not (1 <= int(month) <= 12) or not (2000 <= int(year) <= 2100):
        raise ValueError(f"invalid year/month {year!r}-{month!r}")


def _parse_month(value: str, name: str) -> tuple[int, int]:
    m = _MONTH_RE.match(value) if isinstance(value, str) else None
    if m is None:
        raise ValueError(f"invalid {name} {value!r}; expected 'YYYY-MM'")
    year, month = int(m.group(1)), int(m.group(2))
    _check_month(year, month)
    return year, month


def _utc_now() -> pd.Timestamp:
    """Current UTC time (a seam so tests can pin the clock)."""
    return pd.Timestamp.now(tz="UTC")


def archive_url(base_url: str, symbol: str, interval: str, year: int, month: int) -> str:
    """URL of one monthly kline zip.

    e.g. ``.../monthly/klines/BTCUSDT/4h/BTCUSDT-4h-2024-01.zip``.
    """
    _check_symbol(symbol)
    interval_seconds(interval)
    _check_month(year, month)
    name = f"{symbol}-{interval}-{int(year):04d}-{int(month):02d}.zip"
    return f"{base_url.rstrip('/')}/monthly/klines/{symbol}/{interval}/{name}"


def _empty_candles() -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name="time")
    frame = pd.DataFrame({c: pd.Series([], dtype="float64") for c in CANDLE_COLUMNS}, index=index)
    frame["tradable"] = pd.Series([], dtype="bool", index=index)
    return frame


def parse_kline_csv(data: bytes, *, drop_identical_rows: bool = False) -> pd.DataFrame:
    """Parse a Binance kline CSV into the candle convention.

    Tolerates a header row. Open times in ms or µs (detected per value, > 1e14 -> µs) are
    normalised to a UTC index. ``tradable = volume > 0``. Raises ``ValueError`` on malformed
    input or duplicate open times. With ``drop_identical_rows``, a line repeated byte for byte
    (seen in Binance's own AXSUSDT 2026-02 and UMAUSDT 2026-07 files) is kept once; rows that
    share an open time but differ still raise.
    """
    text = data.decode("utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if drop_identical_rows:
        lines = list(dict.fromkeys(ln.strip() for ln in lines))
    if not lines:
        return _empty_candles()
    first_field = lines[0].split(",", 1)[0].strip()
    if not first_field.lstrip("-").isdigit():
        lines = lines[1:]  # header row
        if not lines:
            return _empty_candles()
    try:
        raw = pd.read_csv(
            io.StringIO("\n".join(lines)),
            header=None,
            usecols=range(6),
            names=list(KLINE_CSV_COLUMNS[:6]),
            dtype={"open_time": "int64", **{c: "float64" for c in CANDLE_COLUMNS}},
        )
    except (ValueError, pd.errors.ParserError) as exc:
        raise ValueError(f"malformed Binance kline CSV: {exc}") from exc

    open_time = raw["open_time"].to_numpy(dtype=np.int64)
    ms = np.where(open_time > _US_THRESHOLD, open_time // 1000, open_time)
    index = pd.DatetimeIndex(pd.to_datetime(ms, unit="ms", utc=True), name="time")
    frame = pd.DataFrame(
        {c: raw[c].to_numpy(dtype=np.float64) for c in CANDLE_COLUMNS}, index=index
    )
    frame["tradable"] = frame["volume"].to_numpy() > 0
    frame = frame.sort_index(kind="stable")
    if not frame.index.is_unique:
        dupes = frame.index[frame.index.duplicated()].unique()
        raise ValueError(f"kline CSV has duplicate open times: {list(dupes[:3])}")
    validate_candles(frame)
    return frame


def _expected_sha256(checksum_text: str, filename: str) -> str:
    for line in checksum_text.splitlines():
        parts = line.split()
        if (
            parts
            and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0])
            and (len(parts) == 1 or parts[-1].lstrip("*") == filename)
        ):
            return parts[0].lower()
    raise ValueError(f"no SHA-256 for {filename} in its .CHECKSUM file")


def _csv_from_zip(payload: bytes, filename: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if len(members) != 1:
                raise ValueError(f"{filename} should contain exactly one CSV, found {members}")
            return zf.read(members[0])
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{filename} is not a valid zip: {exc}") from exc


def download_month(
    symbol: str, interval: str, year: int, month: int, *, base_url: str, client: httpx.Client
) -> pd.DataFrame | None:
    """Fetch, checksum-verify, and parse one monthly kline zip. Returns ``None`` on 404.

    Raises ``ValueError`` naming the file on a SHA-256 mismatch or a missing checksum, and
    ``httpx.HTTPStatusError`` on other HTTP errors.
    """
    url = archive_url(base_url, symbol, interval, year, month)
    filename = url.rsplit("/", 1)[-1]
    resp = client.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    payload = resp.content

    check = client.get(url + ".CHECKSUM")
    if check.status_code == 404:
        raise ValueError(f"{filename}: .CHECKSUM not found, refusing unverified data")
    check.raise_for_status()
    expected = _expected_sha256(check.text, filename)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f"{filename}: SHA-256 mismatch (expected {expected}, got {actual})")

    return parse_kline_csv(_csv_from_zip(payload, filename), drop_identical_rows=True)


def _month_range(start: str, end: str) -> list[tuple[int, int]]:
    sy, sm = _parse_month(start, "start")
    ey, em = _parse_month(end, "end")
    if (ey, em) < (sy, sm):
        raise ValueError(f"end {end!r} is before start {start!r}")
    months: list[tuple[int, int]] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _month_is_complete(year: int, month: int, now: pd.Timestamp) -> bool:
    next_start = pd.Timestamp(year=year, month=month, day=1, tz="UTC") + pd.offsets.MonthBegin(1)
    return next_start <= now


def month_path(out_dir: Path, symbol: str, interval: str, year: int, month: int) -> Path:
    """Where one month's Parquet file lives."""
    return Path(out_dir) / symbol / interval / f"{int(year):04d}-{int(month):02d}.parquet"


@dataclass
class RangeResult:
    """What ``download_range_detailed`` did, month by month (``YYYY-MM`` keys)."""

    written: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)  # already on disk
    not_found: list[str] = field(default_factory=list)  # archive returned 404
    incomplete: list[str] = field(default_factory=list)  # current/future UTC month, not written
    bars_written: int = 0

    @property
    def paths(self) -> list[Path]:
        return sorted(self.written + self.skipped)


def _write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    frame.to_parquet(tmp, engine="pyarrow", index=True)
    tmp.replace(path)


def download_range_detailed(
    symbol: str,
    interval: str,
    start: str,
    end: str,
    *,
    out_dir: Path,
    base_url: str,
    client: httpx.Client | None = None,
    refresh: bool = False,
) -> RangeResult:
    """Like ``download_range`` but returns a per-month breakdown (used by the CLI)."""
    _check_symbol(symbol)
    interval_seconds(interval)
    months = _month_range(start, end)
    now = _utc_now()
    result = RangeResult()
    own_client = client is None
    http = client or httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True)
    try:
        for year, month in months:
            label = f"{year:04d}-{month:02d}"
            if not _month_is_complete(year, month, now):
                result.incomplete.append(label)
                continue
            path = month_path(out_dir, symbol, interval, year, month)
            if path.exists() and not refresh:
                result.skipped.append(path)
                continue
            frame = download_month(symbol, interval, year, month, base_url=base_url, client=http)
            if frame is None:
                result.not_found.append(label)
                continue
            _write_parquet(frame, path)
            result.written.append(path)
            result.bars_written += len(frame)
    finally:
        if own_client:
            http.close()
    return result


def download_range(
    symbol: str,
    interval: str,
    start: str,
    end: str,
    *,
    out_dir: Path,
    base_url: str,
    client: httpx.Client | None = None,
    refresh: bool = False,
) -> list[Path]:
    """Download months ``start``..``end`` (``YYYY-MM``, inclusive) into Parquet.

    Writes ``out_dir/{SYMBOL}/{interval}/{YYYY-MM}.parquet``; skips months already on disk unless
    ``refresh``; skips archive 404s; never writes the current (incomplete) or a future UTC month.
    Returns the sorted Parquet paths for the range that are on disk afterwards (written + skipped).
    """
    return download_range_detailed(
        symbol,
        interval,
        start,
        end,
        out_dir=out_dir,
        base_url=base_url,
        client=client,
        refresh=refresh,
    ).paths


def main(argv: list[str] | None = None) -> int:
    from lab.config import load_config

    cfg = load_config()
    parser = argparse.ArgumentParser(
        prog="python -m lab.data.archive",
        description="Download Binance public archive klines into monthly Parquet files.",
    )
    parser.add_argument("symbol", help="e.g. BTCUSDT")
    parser.add_argument("interval", help="e.g. 4h")
    parser.add_argument("start", help="first month, YYYY-MM")
    parser.add_argument("end", help="last month, YYYY-MM (inclusive)")
    parser.add_argument("--out-dir", type=Path, default=cfg.parquet_dir)
    parser.add_argument("--base-url", default=cfg.archive_base_url)
    parser.add_argument("--refresh", action="store_true", help="re-download months on disk")
    args = parser.parse_args(argv)

    try:
        res = download_range_detailed(
            args.symbol.upper(),
            args.interval,
            args.start,
            args.end,
            out_dir=args.out_dir,
            base_url=args.base_url,
            refresh=args.refresh,
        )
    except (ValueError, httpx.HTTPError) as exc:
        print(f"error: {exc}")
        return 1
    print(
        f"{args.symbol.upper()} {args.interval} {args.start}..{args.end}: "
        f"{len(res.written)} written ({res.bars_written} bars), "
        f"{len(res.skipped)} already on disk, "
        f"{len(res.not_found)} not in archive, {len(res.incomplete)} incomplete/future skipped"
    )
    if res.not_found:
        print(f"  not in archive: {', '.join(res.not_found)}")
    if res.incomplete:
        print(f"  not written (month not finished): {', '.join(res.incomplete)}")
    print(f"  dir: {Path(args.out_dir) / args.symbol.upper() / args.interval}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
