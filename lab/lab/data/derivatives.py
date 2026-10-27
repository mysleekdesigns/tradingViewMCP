"""Binance USD-M futures public archive (``data.binance.vision/data/futures/um``) -> Parquet.

Historical risk-filter inputs only: no futures account, no key, nothing here trades.

Archive layout (every zip has a sibling ``.CHECKSUM`` ``<sha256>  <filename>``; every CSV has a
header row, tolerated if absent):

* ``monthly/fundingRate/{S}/{S}-fundingRate-{YYYY-MM}.zip``
  (``calc_time,funding_interval_hours,last_funding_rate``; ``calc_time`` in ms)
* ``monthly/{premiumIndexKlines|markPriceKlines|indexPriceKlines}/{S}/{iv}/{S}-{iv}-{YYYY-MM}.zip``
  (REST kline columns; premium index values are small signed fractions; ``close_time`` is the
  kline's last millisecond, ``open_time + interval - 1 ms``)
* ``daily/metrics/{S}/{S}-metrics-{YYYY-MM-DD}.zip`` (5-minute open interest / long-short rows;
  ``create_time`` is a UTC string such as ``2024-01-01 00:05:00``, or an integer epoch)

On disk under ``data_dir`` (``LabConfig.derivatives_dir``)::

    fundingRate/{S}/{YYYY-MM}.parquet
    {kline dataset}/{S}/{interval}/{YYYY-MM}.parquet
    metrics/{S}/{YYYY-MM-DD}.parquet
    <series dir>/_downloads.json      # download outcomes: source sha256, not_found, rejected
    manifest.json                      # build_manifest(): per series first/last, rows, gaps, ...

Each frame's index is the archive's OWN timestamp (UTC, named ``time``); alignment to spot bars is
``lab.data.derivatives_align``'s job. Kline frames also keep the CSV's ``close_time`` (column
``close_time``, ``datetime64[ns, UTC]``) so alignment derives each kline's visibility
(``close_time + 1 ms``) from the data, never from an interval passed separately. Parquet written
before ``SCHEMA_VERSION`` 2 lacks that column; ``load_series`` returns it as NaT and alignment
refuses it (re-download with ``--refresh``).

Byte-identical duplicate lines are dropped; rows sharing a timestamp but differing reject the
whole file (recorded, not written). Missing samples stay absent: nothing is forward-filled or
synthesised. The current or a future UTC month (monthly) or
day (daily) is never written.

CLI::

    uv run python -m lab.data.derivatives download BTCUSDT --datasets fundingRate,metrics \\
        --interval 4h --start 2020-01 --end 2026-09
    uv run python -m lab.data.derivatives manifest
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from lab.data import archive
from lab.types import interval_seconds

DATASETS: tuple[str, ...] = (
    "fundingRate",
    "premiumIndexKlines",
    "markPriceKlines",
    "indexPriceKlines",
    "metrics",
)
KLINE_DATASETS: frozenset[str] = frozenset(
    {"premiumIndexKlines", "markPriceKlines", "indexPriceKlines"}
)
DAILY_DATASETS: frozenset[str] = frozenset({"metrics"})
FUNDING_COLUMNS: tuple[str, ...] = ("funding_rate", "funding_interval_hours")
KLINE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")  # float64 value columns
# The kline's own close time (``datetime64[ns, UTC]``; the CSV's last millisecond of the kline).
# Kept beside KLINE_COLUMNS so alignment never relies on an interval passed separately (W1).
KLINE_CLOSE_TIME = "close_time"
KLINE_FRAME_COLUMNS: tuple[str, ...] = (*KLINE_COLUMNS, KLINE_CLOSE_TIME)
METRICS_COLUMNS: tuple[str, ...] = (
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)
METRICS_STEP = pd.Timedelta(minutes=5)  # metrics rows are 5-minute samples

MANIFEST_NAME = "manifest.json"
# 1: first layout. 2: kline Parquet carries close_time; each series has a content_sha256.
SCHEMA_VERSION = 2
SIDECAR_NAME = "_downloads.json"
SIDECAR_CHECKPOINT_EVERY = 50  # files between sidecar saves during a long (daily metrics) download

# Columns as named in the archive's CSV header (used positionally when a file has no header).
_FUNDING_CSV = ("calc_time", "funding_interval_hours", "last_funding_rate")
_KLINE_CSV = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)
_METRICS_CSV = ("create_time", "symbol", *METRICS_COLUMNS)
_US_THRESHOLD = 1e14  # ms epochs are ~1.7e12 today; µs are ~1.7e15
_S_THRESHOLD = 1e11  # s epochs are ~1.7e9


class RejectedFileError(ValueError):
    """A checksum-verified archive file whose content cannot be trusted (e.g. two different rows
    for one timestamp). The download records it under ``rejected`` and moves on."""


def _utc_now() -> pd.Timestamp:
    """Current UTC time; defers to ``archive._utc_now`` so either seam pins the clock in tests."""
    return archive._utc_now()


# --------------------------------------------------------------------------- validation / paths


def _check_dataset(dataset: str, interval: str | None) -> None:
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; expected one of {list(DATASETS)}")
    if dataset in KLINE_DATASETS:
        if interval is None:
            raise ValueError(f"dataset {dataset!r} needs an interval, e.g. '4h'")
        interval_seconds(interval)
    elif interval is not None:
        raise ValueError(f"dataset {dataset!r} has no interval; got interval={interval!r}")


def _columns(dataset: str) -> tuple[str, ...]:
    """Float64 value columns of ``dataset`` (klines: OHLC; ``close_time`` is separate)."""
    if dataset == "fundingRate":
        return FUNDING_COLUMNS
    if dataset == "metrics":
        return METRICS_COLUMNS
    return KLINE_COLUMNS


def frame_columns(dataset: str) -> tuple[str, ...]:
    """Every column of a ``dataset`` frame, in order (klines add ``close_time``)."""
    if dataset in KLINE_DATASETS:
        return KLINE_FRAME_COLUMNS
    return _columns(dataset)


def series_dir(data_dir: Path, dataset: str, symbol: str, interval: str | None = None) -> Path:
    """Directory holding one series' Parquet files and its ``_downloads.json`` sidecar."""
    _check_dataset(dataset, interval)
    archive.check_symbol(symbol)
    base = Path(data_dir) / dataset / symbol
    return base / interval if interval is not None else base


def series_key(dataset: str, symbol: str, interval: str | None = None) -> str:
    """Manifest key: ``dataset/SYMBOL`` or ``dataset/SYMBOL/interval``."""
    return "/".join(p for p in (dataset, symbol, interval) if p is not None)


def derivatives_url(
    dataset: str, symbol: str, label: str, *, base_url: str, interval: str | None = None
) -> str:
    """Archive URL of one file; ``label`` is ``YYYY-MM`` (monthly) or ``YYYY-MM-DD`` (metrics)."""
    _check_dataset(dataset, interval)
    archive.check_symbol(symbol)
    _label_start(dataset, label)
    base = base_url.rstrip("/")
    if dataset == "fundingRate":
        return f"{base}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{label}.zip"
    if dataset == "metrics":
        return f"{base}/daily/metrics/{symbol}/{symbol}-metrics-{label}.zip"
    return f"{base}/monthly/{dataset}/{symbol}/{interval}/{symbol}-{interval}-{label}.zip"


def _label_start(dataset: str, label: str) -> pd.Timestamp:
    """UTC start of the month/day a file label names; ``ValueError`` naming the label."""
    if dataset in DAILY_DATASETS:
        try:
            day = pd.Timestamp(label)
        except ValueError:
            day = None
        if day is None or len(label) != 10 or day.strftime("%Y-%m-%d") != label:
            raise ValueError(f"invalid day {label!r} for {dataset}; expected 'YYYY-MM-DD'")
        return day.tz_localize("UTC")
    year, month = archive.parse_month(label, "month")
    return pd.Timestamp(year=year, month=month, day=1, tz="UTC")


def _label_end(dataset: str, label: str) -> pd.Timestamp:
    """UTC start of the period after ``label`` (exclusive end)."""
    start = _label_start(dataset, label)
    if dataset in DAILY_DATASETS:
        return start + pd.Timedelta(days=1)
    return start + pd.offsets.MonthBegin(1)


def _labels(dataset: str, start: str, end: str) -> list[str]:
    """File labels for months ``start``..``end`` (``YYYY-MM``); daily datasets expand to days."""
    months = archive.month_range(start, end)
    if dataset not in DAILY_DATASETS:
        return [f"{y:04d}-{m:02d}" for y, m in months]
    first = pd.Timestamp(year=months[0][0], month=months[0][1], day=1)
    last = pd.Timestamp(year=months[-1][0], month=months[-1][1], day=1) + pd.offsets.MonthEnd(0)
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(first, last, freq="D")]


# --------------------------------------------------------------------------- CSV parsing


def _empty_frame(dataset: str) -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name="time")
    cols: dict[str, pd.Series] = {
        c: pd.Series([], dtype="float64", index=index) for c in _columns(dataset)
    }
    if dataset in KLINE_DATASETS:
        cols[KLINE_CLOSE_TIME] = pd.Series([], dtype="datetime64[ns, UTC]", index=index)
    return pd.DataFrame(cols, index=index)


def _parse_times(values: pd.Series, filename: str) -> pd.DatetimeIndex:
    """Integer epochs (s/ms/µs by magnitude) or UTC date-time strings -> UTC index."""
    text = values.astype(str).str.strip()
    if text.str.fullmatch(r"\d+").all():
        raw = text.astype("int64").to_numpy()
        ms = np.where(
            raw > _US_THRESHOLD, raw // 1000, np.where(raw < _S_THRESHOLD, raw * 1000, raw)
        )
        return pd.DatetimeIndex(pd.to_datetime(ms, unit="ms", utc=True), name="time")
    try:
        parsed = pd.to_datetime(text, utc=True, format="ISO8601")
    except (ValueError, TypeError) as exc:
        raise RejectedFileError(f"{filename}: unparseable timestamps ({exc})") from exc
    return pd.DatetimeIndex(parsed, name="time")


def parse_csv(
    dataset: str, data: bytes, *, filename: str = "<csv>", symbol: str | None = None
) -> pd.DataFrame:
    """Parse one archive CSV into the documented frame for ``dataset``.

    Byte-identical duplicate lines are kept once. Raises ``RejectedFileError`` (a ``ValueError``)
    naming ``filename`` when rows share a timestamp but differ, a needed column is missing, a
    value is not numeric, or (metrics) a row names another symbol.
    """
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; expected one of {list(DATASETS)}")
    if dataset == "fundingRate":
        csv_cols, time_col = _FUNDING_CSV, "calc_time"
        mapping = {
            "last_funding_rate": "funding_rate",
            "funding_interval_hours": "funding_interval_hours",
        }
    elif dataset == "metrics":
        csv_cols, time_col = _METRICS_CSV, "create_time"
        mapping = {c: c for c in METRICS_COLUMNS}
    else:
        csv_cols, time_col = _KLINE_CSV, "open_time"
        mapping = {c: c for c in KLINE_COLUMNS}

    text = data.decode("utf-8-sig")
    lines = list(dict.fromkeys(ln.strip() for ln in text.splitlines() if ln.strip()))
    if not lines:
        return _empty_frame(dataset)
    header = [h.strip().lower() for h in lines[0].split(",")]
    if header and header[0] == time_col:
        body = lines[1:]
    else:
        header, body = list(csv_cols), lines
    if not body:
        return _empty_frame(dataset)
    missing = [c for c in (time_col, *mapping) if c not in header]
    if missing:
        raise RejectedFileError(f"{filename}: missing column(s) {missing}; header is {header}")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", pd.errors.ParserWarning)
            raw = pd.read_csv(
                io.StringIO("\n".join(body)),
                header=None,
                names=header,
                dtype=str,
                index_col=False,
            )
    except (ValueError, pd.errors.ParserError) as exc:
        raise RejectedFileError(f"{filename}: malformed CSV ({exc})") from exc

    if dataset == "metrics" and symbol is not None and "symbol" in raw.columns:
        others = sorted(set(raw["symbol"].dropna().str.strip()) - {symbol})
        if others:
            raise RejectedFileError(f"{filename}: rows for other symbols {others[:3]}")

    index = _parse_times(raw[time_col], filename)
    values: dict[str, np.ndarray] = {}
    for src, dst in mapping.items():
        try:
            values[dst] = pd.to_numeric(raw[src].str.strip(), errors="raise").to_numpy(
                dtype=np.float64
            )
        except (ValueError, TypeError) as exc:
            raise RejectedFileError(f"{filename}: non-numeric {src!r} ({exc})") from exc
    frame = pd.DataFrame(values, index=index)[list(_columns(dataset))]
    if dataset in KLINE_DATASETS:
        if KLINE_CLOSE_TIME not in raw.columns:
            raise RejectedFileError(f"{filename}: missing column {KLINE_CLOSE_TIME!r}")
        close = pd.DatetimeIndex(_parse_times(raw[KLINE_CLOSE_TIME], filename)).as_unit("ns")
        if (close < frame.index).any():
            raise RejectedFileError(f"{filename}: close_time before open_time")
        frame[KLINE_CLOSE_TIME] = close  # positional: one close per row, tz-aware UTC
    if not frame.index.is_unique:
        dupes = frame.index[frame.index.duplicated()].unique()
        raise RejectedFileError(
            f"{filename}: {len(dupes)} timestamp(s) with conflicting rows, "
            f"e.g. {[t.isoformat() for t in dupes[:3]]}"
        )
    return frame.sort_index(kind="stable")


# --------------------------------------------------------------------------- download


@dataclass
class DownloadResult:
    """What ``download`` did, file by file (labels ``YYYY-MM`` or ``YYYY-MM-DD``)."""

    written: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)  # already on disk
    not_found: list[str] = field(default_factory=list)  # archive returned 404
    rejected: dict[str, str] = field(default_factory=dict)  # label -> reason; not written
    incomplete: list[str] = field(default_factory=list)  # current/future UTC period, not written
    rows_written: int = 0

    @property
    def paths(self) -> list[Path]:
        return sorted(self.written + self.skipped)


def _read_sidecar(path: Path) -> dict:
    if not path.exists():
        return {"sources": {}, "not_found": [], "rejected": {}}
    raw = json.loads(path.read_text())
    return {
        "sources": dict(raw.get("sources", {})),
        "not_found": list(raw.get("not_found", [])),
        "rejected": dict(raw.get("rejected", {})),
    }


def _write_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def download(
    dataset: str,
    symbol: str,
    start: str,
    end: str,
    *,
    data_dir: Path,
    base_url: str,
    interval: str | None = None,
    client: httpx.Client | None = None,
    refresh: bool = False,
) -> DownloadResult:
    """Download ``dataset`` for months ``start``..``end`` (``YYYY-MM``, inclusive) into Parquet.

    Daily datasets (metrics) expand to every day of those months. Files already on disk are
    skipped unless ``refresh``. A 404 is recorded as not_found; a file with conflicting rows is
    recorded as rejected (reason kept) and not written; the current or a future UTC month/day is
    never written. A SHA-256 mismatch or a missing ``.CHECKSUM`` raises ``ValueError`` naming the
    file. Outcomes (source sha256 per file, not_found, rejected) persist in the series'
    ``_downloads.json`` for ``build_manifest``; call ``build_manifest`` afterwards (the CLI does).
    """
    _check_dataset(dataset, interval)
    archive.check_symbol(symbol)
    labels = _labels(dataset, start, end)
    now = _utc_now()
    out = series_dir(data_dir, dataset, symbol, interval)
    sidecar_path = out / SIDECAR_NAME
    sidecar = _read_sidecar(sidecar_path)
    not_found = set(sidecar["not_found"])
    result = DownloadResult()
    own_client = client is None
    http = client or httpx.Client(timeout=archive.DEFAULT_TIMEOUT, follow_redirects=True)
    try:
        for label in labels:
            if _label_end(dataset, label) > now:
                result.incomplete.append(label)
                continue
            path = out / f"{label}.parquet"
            if path.exists() and not refresh:
                result.skipped.append(path)
                continue
            url = derivatives_url(dataset, symbol, label, base_url=base_url, interval=interval)
            filename = url.rsplit("/", 1)[-1]
            payload = archive.fetch_verified_zip(url, http)
            if payload is None:
                result.not_found.append(label)
                not_found.add(label)
                continue
            not_found.discard(label)
            sidecar["sources"][label] = {
                "file": filename,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            try:
                frame = parse_csv(
                    dataset,
                    archive.csv_from_zip(payload, filename),
                    filename=filename,
                    symbol=symbol,
                )
            except RejectedFileError as exc:
                result.rejected[label] = str(exc)
                sidecar["rejected"][label] = str(exc)
                if path.exists():
                    path.unlink()
                continue
            sidecar["rejected"].pop(label, None)
            archive.write_parquet(frame, path)
            result.written.append(path)
            result.rows_written += len(frame)
            if len(result.written) % SIDECAR_CHECKPOINT_EVERY == 0:
                # a killed run (no finally) keeps the source hashes of what it already wrote
                sidecar["not_found"] = sorted(not_found)
                _write_json(sidecar, sidecar_path)
    finally:
        if own_client:
            http.close()
        sidecar["not_found"] = sorted(not_found)
        if result.written or result.not_found or result.rejected or sidecar_path.exists():
            _write_json(sidecar, sidecar_path)
    return result


# --------------------------------------------------------------------------- loading


def _parquet_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.parquet") if p.is_file())


def load_series(
    dataset: str,
    symbol: str,
    *,
    data_dir: Path,
    interval: str | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Concatenate the Parquet files on disk. Index: UTC DatetimeIndex named "time" holding the
    ARCHIVE'S OWN timestamp (fundingRate: calc_time; *Klines: open_time; metrics: create_time),
    sorted, unique. Columns float64 per dataset (FUNDING_COLUMNS / KLINE_COLUMNS / METRICS_COLUMNS);
    *Klines add ``close_time`` (datetime64[ns, UTC]; NaT for rows from Parquet written before
    schema 2, which alignment refuses). interval is required for *Klines and must be None
    otherwise (ValueError). start/end inclusive filters on the index. Nothing on disk -> an EMPTY
    frame with the right columns (no raise). Gaps stay absent; never forward-filled."""
    directory = series_dir(data_dir, dataset, symbol, interval)
    lo = _as_utc(start, "start")
    hi = _as_utc(end, "end")
    if lo is not None and hi is not None and hi < lo:
        raise ValueError(f"end {hi} is before start {lo}")
    slack = pd.Timedelta(days=1)  # a file may hold a row at its period's boundary
    frames: list[pd.DataFrame] = []
    for path in _parquet_files(directory):
        try:
            p_start, p_end = _label_start(dataset, path.stem), _label_end(dataset, path.stem)
        except ValueError:
            continue  # not one of ours
        if hi is not None and p_start - slack > hi:
            continue
        if lo is not None and p_end + slack <= lo:
            continue
        frames.append(pd.read_parquet(path, engine="pyarrow"))
    value_cols = list(_columns(dataset))
    frames = [f for f in frames if len(f)]
    if not frames:
        return _empty_frame(dataset)
    frame = pd.concat([f[value_cols] for f in frames]).astype("float64")
    frame.index = pd.DatetimeIndex(frame.index, name="time").tz_convert("UTC")
    if dataset in KLINE_DATASETS:
        parts = [
            _close_times(f)
            if KLINE_CLOSE_TIME in f.columns
            else np.full(len(f), np.datetime64("NaT", "ns"))
            for f in frames
        ]
        frame[KLINE_CLOSE_TIME] = pd.DatetimeIndex(np.concatenate(parts), tz="UTC")
    frame = frame.sort_index(kind="stable")
    if not frame.index.is_unique:
        # the same row in two files (period boundary) is one row; two different rows are an error
        frame = frame.reset_index().drop_duplicates().set_index("time")
        if not frame.index.is_unique:
            dupes = frame.index[frame.index.duplicated()].unique()
            raise ValueError(
                f"{series_key(dataset, symbol, interval)}: conflicting rows across files at "
                f"{[t.isoformat() for t in dupes[:3]]}"
            )
    if lo is not None:
        frame = frame[frame.index >= lo]
    if hi is not None:
        frame = frame[frame.index <= hi]
    return frame


def _close_times(frame: pd.DataFrame) -> np.ndarray:
    """A Parquet frame's ``close_time`` as naive-UTC ``M8[ns]`` values."""
    col = pd.DatetimeIndex(frame[KLINE_CLOSE_TIME])
    col = col.tz_localize("UTC") if col.tz is None else col.tz_convert("UTC")
    return col.as_unit("ns").tz_localize(None).to_numpy()


def _as_utc(ts: pd.Timestamp | None, name: str) -> pd.Timestamp | None:
    if ts is None:
        return None
    try:
        out = pd.Timestamp(ts)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid {name} {ts!r}: {exc}") from exc
    return out.tz_localize("UTC") if out.tzinfo is None else out.tz_convert("UTC")


# --------------------------------------------------------------------------- manifest


def expected_steps(dataset: str, frame: pd.DataFrame, interval: str | None = None) -> pd.Series:
    """Expected spacing before each row after the first (index = the later row's time).

    fundingRate: the larger of the two rows' ``funding_interval_hours`` (robust to an interval
    change in either direction); *Klines: the interval; metrics: 5 minutes.
    """
    times = frame.index[1:]
    if dataset == "fundingRate":
        hours = frame["funding_interval_hours"].to_numpy(dtype=np.float64)
        step_h = np.fmax(hours[:-1], hours[1:])  # NaN only if both are NaN: no expectation
        return pd.Series(pd.to_timedelta(step_h, unit="h"), index=times)
    if dataset in KLINE_DATASETS:
        if interval is None:
            raise ValueError(f"dataset {dataset!r} needs an interval")
        return pd.Series(pd.Timedelta(seconds=interval_seconds(interval)), index=times)
    return pd.Series(METRICS_STEP, index=times)


def find_series_gaps(
    dataset: str, frame: pd.DataFrame, *, gap_factor: float, interval: str | None = None
) -> list[dict[str, str]]:
    """Holes where consecutive rows are more than ``gap_factor`` x the expected step apart.

    Each gap is ``{"from": last row before, "to": first row after}`` (ISO UTC). Nothing is filled.
    """
    if not gap_factor >= 1.0:
        raise ValueError(f"gap_factor must be >= 1, got {gap_factor!r}")
    if len(frame) < 2:
        return []
    spacing = pd.Series(frame.index[1:] - frame.index[:-1], index=frame.index[1:])
    expected = expected_steps(dataset, frame, interval)
    is_gap = (spacing > expected * gap_factor).to_numpy()
    prev = frame.index[:-1]
    nxt = frame.index[1:]
    return [{"from": prev[i].isoformat(), "to": nxt[i].isoformat()} for i in np.flatnonzero(is_gap)]


def content_sha256(frame: pd.DataFrame) -> str:
    """SHA-256 of a loaded frame's CONTENT, independent of how Parquet encodes it on disk.

    Hashes, in order: the row count and column names, the index as little-endian int64 ns, then
    each column in frame order as little-endian float64 (NaN canonicalised) or, for datetime
    columns, int64 ns (NaT = int64 min). Changing any value, timestamp, row, or column changes it;
    rewriting the same data with another pyarrow version does not.
    """
    digest = hashlib.sha256()
    names = [str(c) for c in frame.columns]
    digest.update(f"rows={len(frame)};columns={','.join(names)}\n".encode())
    index = pd.DatetimeIndex(frame.index)
    digest.update(index.as_unit("ns").asi8.astype("<i8").tobytes())
    for name, col in zip(names, (frame[c] for c in frame.columns), strict=True):
        digest.update(name.encode() + b"\x00")
        if isinstance(col.dtype, pd.DatetimeTZDtype) or np.issubdtype(col.dtype, np.datetime64):
            arr = pd.DatetimeIndex(col).as_unit("ns").asi8.astype("<i8")
        else:
            vals = col.to_numpy(dtype=np.float64, na_value=np.nan)
            arr = np.where(np.isnan(vals), np.nan, vals).astype("<f8")
        digest.update(arr.tobytes())
    return digest.hexdigest()


def _iter_series(data_dir: Path) -> Iterator[tuple[str, str, str | None, Path]]:
    for dataset in DATASETS:
        root = Path(data_dir) / dataset
        if not root.is_dir():
            continue
        for sym_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if dataset in KLINE_DATASETS:
                for iv_dir in sorted(p for p in sym_dir.iterdir() if p.is_dir()):
                    yield dataset, sym_dir.name, iv_dir.name, iv_dir
            else:
                yield dataset, sym_dir.name, None, sym_dir


def build_manifest(data_dir: Path, *, gap_factor: float) -> dict:
    """Summarise every series on disk and write ``{data_dir}/manifest.json``.

    Per series (key ``dataset/SYMBOL[/interval]``): first/last timestamp (ISO UTC, ``None`` when
    empty), rows, files, gaps (``{from, to}`` where spacing exceeds ``gap_factor`` x the expected
    step), ``content_sha256`` (``content_sha256`` of the loaded frame, so editing a Parquet value
    changes the manifest even though the source zips did not), and from the download sidecar:
    not_found and rejected labels and each source zip's sha256. Top level: ``schema_version``
    (``SCHEMA_VERSION``) and ``gap_factor``. Deterministic (no wall-clock field), so its sha256
    changes only when the data does.
    """
    if not gap_factor >= 1.0:
        raise ValueError(f"gap_factor must be >= 1, got {gap_factor!r}")
    series: dict[str, dict] = {}
    for dataset, symbol, interval, directory in _iter_series(data_dir):
        try:
            frame = load_series(dataset, symbol, data_dir=data_dir, interval=interval)
        except ValueError as exc:
            if "conflicting rows" in str(exc):
                raise
            continue  # a directory that is not a valid series (bad symbol/interval name)
        sidecar = _read_sidecar(directory / SIDECAR_NAME)
        files = _parquet_files(directory)
        if not files and not (sidecar["sources"] or sidecar["not_found"] or sidecar["rejected"]):
            continue
        series[series_key(dataset, symbol, interval)] = {
            "dataset": dataset,
            "symbol": symbol,
            "interval": interval,
            "first": frame.index[0].isoformat() if len(frame) else None,
            "last": frame.index[-1].isoformat() if len(frame) else None,
            "rows": len(frame),
            "files": len(files),
            "content_sha256": content_sha256(frame),
            "gaps": find_series_gaps(dataset, frame, gap_factor=gap_factor, interval=interval),
            "not_found": sorted(sidecar["not_found"]),
            "rejected": dict(sorted(sidecar["rejected"].items())),
            "sources": {k: v["sha256"] for k, v in sorted(sidecar["sources"].items())},
        }
    manifest = {"schema_version": SCHEMA_VERSION, "gap_factor": gap_factor, "series": series}
    _write_json(manifest, Path(data_dir) / MANIFEST_NAME)
    return manifest


def manifest_sha256(data_dir: Path) -> str | None:
    """SHA-256 of ``{data_dir}/manifest.json``'s bytes, or ``None`` if there is no manifest."""
    path = Path(data_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    from lab.config import load_config

    cfg = load_config()
    parser = argparse.ArgumentParser(
        prog="python -m lab.data.derivatives",
        description="Download Binance USD-M futures archive data (historical risk filters).",
    )
    parser.add_argument("--data-dir", type=Path, default=cfg.derivatives_dir)
    sub = parser.add_subparsers(dest="command", required=True)
    dl = sub.add_parser("download", help="download series, then rebuild the manifest")
    dl.add_argument("symbol", help="e.g. BTCUSDT")
    dl.add_argument("--datasets", default=",".join(DATASETS), help="comma-separated")
    dl.add_argument("--interval", default=None, help="kline interval, e.g. 4h (*Klines only)")
    dl.add_argument("--start", required=True, help="first month, YYYY-MM")
    dl.add_argument("--end", required=True, help="last month, YYYY-MM (inclusive)")
    dl.add_argument("--base-url", default=cfg.derivatives.archive_base_url)
    dl.add_argument("--refresh", action="store_true", help="re-download files on disk")
    sub.add_parser("manifest", help="rebuild manifest.json from the files on disk")
    args = parser.parse_args(argv)
    gap_factor = cfg.derivatives.gap_factor

    if args.command == "manifest":
        manifest = build_manifest(args.data_dir, gap_factor=gap_factor)
        _print_manifest(manifest, args.data_dir)
        return 0

    symbol = args.symbol.upper()
    datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]
    try:
        for dataset in datasets:
            _check_dataset(dataset, args.interval if dataset in KLINE_DATASETS else None)
        for dataset in datasets:
            interval = args.interval if dataset in KLINE_DATASETS else None
            res = download(
                dataset,
                symbol,
                args.start,
                args.end,
                data_dir=args.data_dir,
                base_url=args.base_url,
                interval=interval,
                refresh=args.refresh,
            )
            key = series_key(dataset, symbol, interval)
            print(
                f"{key} {args.start}..{args.end}: {len(res.written)} written "
                f"({res.rows_written} rows), {len(res.skipped)} already on disk, "
                f"{len(res.not_found)} not in archive, {len(res.rejected)} rejected, "
                f"{len(res.incomplete)} incomplete/future skipped"
            )
            if res.not_found:
                print(f"  not in archive: {_compact(res.not_found)}")
            for label, reason in res.rejected.items():
                print(f"  rejected {label}: {reason}")
            if res.incomplete:
                print(f"  not written (period not finished): {_compact(res.incomplete)}")
    except (ValueError, httpx.HTTPError) as exc:
        print(f"error: {exc}")
        return 1
    manifest = build_manifest(args.data_dir, gap_factor=gap_factor)
    _print_manifest(manifest, args.data_dir)
    return 0


def _compact(labels: list[str]) -> str:
    return ", ".join(labels) if len(labels) <= 6 else f"{labels[0]} .. {labels[-1]} ({len(labels)})"


def _print_manifest(manifest: dict, data_dir: Path) -> None:
    print(f"manifest: {Path(data_dir) / MANIFEST_NAME} (sha256 {manifest_sha256(data_dir)})")
    for key, s in manifest["series"].items():
        print(
            f"  {key}: {s['rows']} rows in {s['files']} files, {s['first']} .. {s['last']}, "
            f"{len(s['gaps'])} gaps, {len(s['not_found'])} not found, "
            f"{len(s['rejected'])} rejected"
        )


if __name__ == "__main__":
    raise SystemExit(main())
