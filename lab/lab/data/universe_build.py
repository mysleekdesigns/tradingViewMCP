"""Build the point-in-time multi-coin universe from the Binance public archive.

Three steps, each resumable (CLI: ``uv run python -m lab.data.universe_build <step>``):

1. ``symbols``  list every archive symbol (delisted included), keep ordinary ``*{quote}`` coins,
   write ``{universe_dir}/symbols-{quote}.json`` with the kept and dropped symbols and reasons.
2. ``download`` fetch each kept symbol's monthly klines for its listed months into the usual
   ``klines/{SYMBOL}/{interval}/{YYYY-MM}.parquet`` tree (months on disk are skipped).
3. ``build``    eligibility (1 year of history, restarted after a relisting gap, tradable bar)
   and trailing dollar-volume rank at every bar, written as a long table
   ``{universe_dir}/{quote}-{interval}.parquet`` (time, symbol, rank, dollar_volume; eligible rows
   only) plus a ``.json`` manifest with the settings, coverage, delistings and a sha256.

Membership at bar t uses data at or before t only. A strategy that ranks at t's close trades at
t+1's open, as everywhere in the lab. Market data needs no API key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from lab.config import LabConfig, UniverseConfig
from lab.data.archive import DEFAULT_TIMEOUT, download_range_detailed
from lab.data.listing import list_months, list_symbols, select_symbols
from lab.data.store import load_candles
from lab.data.universe import (
    eligibility_mask,
    liquidity_rank,
    listing_table,
    trailing_dollar_volume,
)

PEG_MEDIAN_ABS_RETURN = (
    0.002  # median |daily log return| under 0.2%: looks pegged, check exclusions
)


def symbols_path(universe_dir: Path, quote: str) -> Path:
    return Path(universe_dir) / f"symbols-{quote}.json"


def universe_path(universe_dir: Path, quote: str, interval: str) -> Path:
    return Path(universe_dir) / f"{quote}-{interval}.parquet"


def write_symbols(
    ucfg: UniverseConfig, *, universe_dir: Path, client: httpx.Client
) -> dict[str, Any]:
    """Step 1: list and select symbols; write and return the selection record."""
    listed = list_symbols(ucfg.archive_list_url, client=client)
    sel = select_symbols(listed, quote=ucfg.quote, exclude_bases=ucfg.exclude_bases)
    record: dict[str, Any] = {
        "listed_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "quote": ucfg.quote,
        "archive_symbols": len(listed),
        "selected": sel.selected,
        "excluded": sel.excluded,
    }
    path = symbols_path(universe_dir, ucfg.quote)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True))
    return record


def read_symbols(universe_dir: Path, quote: str) -> list[str]:
    path = symbols_path(universe_dir, quote)
    if not path.is_file():
        raise ValueError(
            f"no symbol list at {str(path)!r}; run `uv run python -m lab.data.universe_build "
            "symbols` first"
        )
    return list(json.loads(path.read_text())["selected"])


@dataclass
class DownloadReport:
    bars_written: int = 0
    months_written: int = 0
    months_skipped: int = 0
    no_data: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    listed: dict[str, list[str]] = field(default_factory=dict)  # symbol -> archive months


def download_symbols(
    symbols: list[str],
    *,
    interval: str,
    list_url: str,
    base_url: str,
    out_dir: Path,
    client: httpx.Client,
    workers: int = 8,
    progress: Callable[[int, int, str], None] | None = None,
) -> DownloadReport:
    """Step 2: download each symbol's listed months (``httpx.Client`` is thread-safe). A symbol
    that errors is recorded in ``failed`` and the rest continue; rerunning resumes."""
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    report = DownloadReport()

    def one(symbol: str) -> tuple[str, int, int, int]:
        months = list_months(list_url, symbol, interval, client=client)
        report.listed[symbol] = months
        if not months:
            return symbol, -1, 0, 0
        res = download_range_detailed(
            symbol,
            interval,
            months[0],
            months[-1],
            out_dir=out_dir,
            base_url=base_url,
            client=client,
        )
        return symbol, res.bars_written, len(res.written), len(res.skipped)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, s): s for s in symbols}
        for done, fut in enumerate(as_completed(futures), start=1):
            symbol = futures[fut]
            try:
                _, bars, written, skipped = fut.result()
            except (ValueError, httpx.HTTPError) as exc:
                report.failed[symbol] = str(exc)
            else:
                if bars < 0:
                    report.no_data.append(symbol)
                else:
                    report.bars_written += bars
                    report.months_written += written
                    report.months_skipped += skipped
            if progress is not None:
                progress(done, len(symbols), symbol)
    report.no_data.sort()
    return report


def download_record_path(universe_dir: Path, quote: str, interval: str) -> Path:
    return Path(universe_dir) / f"download-{quote}-{interval}.json"


def write_download_record(
    report: DownloadReport, *, path: Path, interval: str
) -> dict[str, Any]:
    """Persist what the archive listed and what failed, so ``build`` can refuse partial data."""
    previous: dict[str, Any] = json.loads(path.read_text()) if path.is_file() else {}
    symbols: dict[str, Any] = dict(previous.get("symbols", {}))
    for symbol, months in report.listed.items():
        symbols[symbol] = {"listed": months, "error": None}
    for symbol, err in report.failed.items():
        symbols.setdefault(symbol, {"listed": None, "error": None})["error"] = err
    record = {
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "interval": interval,
        "symbols": dict(sorted(symbols.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2))
    return record


def incomplete_symbols(
    symbols: list[str], record: Mapping[str, Any], *, interval: str, parquet_dir: Path
) -> dict[str, str]:
    """Selected symbols whose download is unfinished: never attempted, failed, or an archive
    month missing on disk. A partial history would look like a delisting, so ``build`` refuses
    these unless told otherwise."""
    entries = record.get("symbols", {})
    out: dict[str, str] = {}
    for symbol in symbols:
        entry = entries.get(symbol)
        if entry is None or entry.get("listed") is None:
            out[symbol] = "not downloaded"
        elif entry.get("error"):
            out[symbol] = f"download failed: {entry['error']}"
        else:
            folder = Path(parquet_dir) / symbol / interval
            gone = [m for m in entry["listed"] if not (folder / f"{m}.parquet").is_file()]
            if gone:
                out[symbol] = f"{len(gone)} archive month(s) not on disk, first {gone[0]}"
    return out


def load_symbol_candles(
    symbols: list[str], *, interval: str, parquet_dir: Path
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Candles per symbol from Parquet, and the symbols with no data on disk."""
    out: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for symbol in symbols:
        folder = Path(parquet_dir) / symbol / interval
        if not folder.is_dir():
            missing.append(symbol)
            continue
        try:
            out[symbol] = load_candles(symbol, interval, parquet_dir=parquet_dir)
        except ValueError:
            missing.append(symbol)
    return out, missing


def build_universe(
    candles_by_symbol: Mapping[str, pd.DataFrame], ucfg: UniverseConfig
) -> pd.DataFrame:
    """Long table (time, symbol, rank, dollar_volume) of eligible symbols at every bar."""
    symbols = sorted(candles_by_symbol)
    data = {s: candles_by_symbol[s] for s in symbols}
    mask = eligibility_mask(
        data,
        min_history_days=ucfg.min_history_days,
        relist_gap_days=ucfg.relist_gap_days,
        relist_jump=ucfg.relist_jump,
        interval=ucfg.interval,
    )
    dollar = trailing_dollar_volume(data, window_days=ucfg.volume_window_days)
    rank = liquidity_rank(dollar, mask)
    long = rank.stack().dropna().rename("rank").to_frame()
    long.index.names = ["time", "symbol"]
    long["dollar_volume"] = dollar.stack().reindex(long.index)
    long = long.reset_index()
    long["rank"] = long["rank"].astype(np.int32)
    return long.sort_values(["time", "rank"], kind="stable").reset_index(drop=True)


def load_universe(
    universe_dir: Path, quote: str, interval: str, *, allow_incomplete: bool = False
) -> pd.DataFrame:
    """Wide rank frame (index = bar time, columns = symbols, NaN = not eligible).

    Refuses a universe built with ``--allow-incomplete`` that left symbols out (failures can
    cluster in old or delisted coins, which brings survivorship back) unless ``allow_incomplete``.
    """
    path = universe_path(universe_dir, quote, interval)
    if not path.is_file():
        raise ValueError(
            f"no universe at {str(path)!r}; run `uv run python -m lab.data.universe_build build`"
        )
    meta_path = path.with_suffix(".json")
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else None
    if meta is None or meta.get("parquet_sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError(f"universe {path.name} has no matching manifest; rebuild it")
    left_out = meta.get("symbols_incomplete_download") or {}
    if left_out and not allow_incomplete:
        raise ValueError(
            f"universe {path.name} was built without {len(left_out)} incomplete symbol(s) "
            f"({', '.join(sorted(left_out)[:5])}); finish `download` and rebuild"
        )
    long = pd.read_parquet(path)
    wide = long.pivot(index="time", columns="symbol", values="rank").sort_index()
    wide.index = pd.DatetimeIndex(wide.index, name="time")
    wide.columns.name = None
    return wide


def _peg_suspects(candles_by_symbol: Mapping[str, pd.DataFrame]) -> list[str]:
    out = []
    for symbol, frame in candles_by_symbol.items():
        close = frame["close"].to_numpy(dtype=np.float64)
        if len(close) > 30:
            med = float(np.median(np.abs(np.diff(np.log(close)))))
            if med < PEG_MEDIAN_ABS_RETURN:
                out.append(symbol)
    return sorted(out)


PRICE_JUMP_FACTOR = 5.0  # a >5x close-to-close move between bars: likely a ticker reuse or redenomination


def _price_jumps(candles_by_symbol: Mapping[str, pd.DataFrame]) -> dict[str, list[str]]:
    """Bars whose close moved more than ``PRICE_JUMP_FACTOR``x from the previous bar (holes
    included): two markets joined under one ticker, which would fake a momentum return."""
    limit = float(np.log(PRICE_JUMP_FACTOR))
    out: dict[str, list[str]] = {}
    for symbol, frame in candles_by_symbol.items():
        step = np.abs(np.diff(np.log(frame["close"].to_numpy(dtype=np.float64))))
        hits = frame.index[1:][step > limit]
        if len(hits):
            out[symbol] = [str(t.date()) for t in hits]
    return dict(sorted(out.items()))


def manifest(
    table: pd.DataFrame,
    candles_by_symbol: Mapping[str, pd.DataFrame],
    ucfg: UniverseConfig,
    *,
    missing: list[str],
    parquet_sha256: str,
    incomplete: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Summary of a built universe: settings, coverage, members per year, delistings."""
    listing = listing_table(
        candles_by_symbol,
        relist_gap_days=ucfg.relist_gap_days,
        delisted_after_days=ucfg.delisted_after_days,
        relist_jump=ucfg.relist_jump,
        interval=ucfg.interval,
    )
    top = table[table["rank"] <= ucfg.top_n]
    per_bar = top.groupby("time")["symbol"].count()
    by_year = per_bar.groupby(pd.DatetimeIndex(per_bar.index).year).agg(["min", "median", "max"])
    ever_top = sorted(top["symbol"].unique())
    delisted = listing[listing["delisted"]]
    return {
        "built_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "settings": {
            "quote": ucfg.quote,
            "interval": ucfg.interval,
            "top_n": ucfg.top_n,
            "volume_window_days": ucfg.volume_window_days,
            "min_history_days": ucfg.min_history_days,
            "relist_gap_days": ucfg.relist_gap_days,
            "relist_jump": ucfg.relist_jump,
            "delisted_after_days": ucfg.delisted_after_days,
            "exclude_bases": list(ucfg.exclude_bases),
        },
        "parquet_sha256": parquet_sha256,
        "symbols_with_data": len(candles_by_symbol),
        "symbols_missing_data": missing,
        "symbols_incomplete_download": dict(sorted((incomplete or {}).items())),
        "first_bar": str(table["time"].min()) if len(table) else None,
        "last_bar": str(table["time"].max()) if len(table) else None,
        "top_n_members_per_bar_by_year": {
            str(y): {k: float(v) for k, v in row.items()} for y, row in by_year.iterrows()
        },
        "ever_in_top_n": ever_top,
        "ever_in_top_n_and_delisted": sorted(set(ever_top) & set(delisted.index)),
        "delisted": sorted(delisted.index),
        "relisted_or_reused": sorted(listing.index[listing["segments"] > 1]),
        "peg_suspects": _peg_suspects(candles_by_symbol),
        "price_jumps": _price_jumps(candles_by_symbol),
    }


def write_universe(
    candles_by_symbol: Mapping[str, pd.DataFrame],
    ucfg: UniverseConfig,
    *,
    universe_dir: Path,
    missing: list[str],
    incomplete: Mapping[str, str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Step 3: build, write the Parquet table and its manifest; return (path, manifest)."""
    table = build_universe(candles_by_symbol, ucfg)
    path = universe_path(universe_dir, ucfg.quote, ucfg.interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(path, index=False)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    info = manifest(
        table,
        candles_by_symbol,
        ucfg,
        missing=missing,
        parquet_sha256=digest,
        incomplete=incomplete,
    )
    path.with_suffix(".json").write_text(json.dumps(info, indent=2, sort_keys=True))
    return path, info


def _client() -> httpx.Client:
    return httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True)


def main(argv: list[str] | None = None, cfg: LabConfig | None = None) -> int:
    from lab.config import load_config

    cfg = cfg or load_config()
    ucfg = cfg.universe
    parser = argparse.ArgumentParser(
        prog="python -m lab.data.universe_build",
        description="Build the point-in-time multi-coin universe from the Binance archive.",
    )
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("symbols", help="list archive symbols and select ordinary coins")
    dl = sub.add_parser("download", help="download klines for the selected symbols")
    dl.add_argument("--workers", type=int, default=8)
    dl.add_argument("--limit", type=int, default=None, help="first N symbols only (testing)")
    bd = sub.add_parser("build", help="compute eligibility and liquidity rank; write the table")
    bd.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="build even if some downloads are unfinished (listed in the manifest)",
    )
    args = parser.parse_args(argv)

    try:
        if args.step == "symbols":
            with _client() as client:
                rec = write_symbols(ucfg, universe_dir=cfg.universe_dir, client=client)
            excluded = rec["excluded"]
            print(
                f"{rec['archive_symbols']} archive symbols; {len(rec['selected'])} "
                f"{ucfg.quote} coins kept, {len(excluded)} dropped"
            )
            for sym, why in sorted(excluded.items()):
                print(f"  dropped {sym}: {why}")
            print(f"  wrote {symbols_path(cfg.universe_dir, ucfg.quote)}")
        elif args.step == "download":
            symbols = read_symbols(cfg.universe_dir, ucfg.quote)[: args.limit]

            def progress(done: int, total: int, symbol: str) -> None:
                if done % 50 == 0 or done == total:
                    print(f"  {done}/{total} ({symbol})", flush=True)

            with _client() as client:
                rep = download_symbols(
                    symbols,
                    interval=ucfg.interval,
                    list_url=ucfg.archive_list_url,
                    base_url=cfg.archive_base_url,
                    out_dir=cfg.parquet_dir,
                    client=client,
                    workers=args.workers,
                    progress=progress,
                )
            print(
                f"{len(symbols)} symbols: {rep.months_written} months written "
                f"({rep.bars_written} bars), {rep.months_skipped} already on disk, "
                f"{len(rep.no_data)} with no {ucfg.interval} data, {len(rep.failed)} failed"
            )
            record_path = download_record_path(cfg.universe_dir, ucfg.quote, ucfg.interval)
            write_download_record(rep, path=record_path, interval=ucfg.interval)
            print(f"  record: {record_path}")
            for sym, err in sorted(rep.failed.items()):
                print(f"  failed {sym}: {err}")
            if rep.failed:
                print("  rerun `download` to retry; months on disk are skipped")
                return 1
        else:
            symbols = read_symbols(cfg.universe_dir, ucfg.quote)
            record_path = download_record_path(cfg.universe_dir, ucfg.quote, ucfg.interval)
            record = json.loads(record_path.read_text()) if record_path.is_file() else {}
            incomplete = incomplete_symbols(
                symbols, record, interval=ucfg.interval, parquet_dir=cfg.parquet_dir
            )
            if incomplete and not args.allow_incomplete:
                print(f"error: {len(incomplete)} symbol(s) not fully downloaded:")
                for sym, why in sorted(incomplete.items())[:20]:
                    print(f"  {sym}: {why}")
                print("  rerun `download`, or pass --allow-incomplete to build without them")
                return 1
            symbols = [s for s in symbols if s not in incomplete]
            candles, missing = load_symbol_candles(
                symbols, interval=ucfg.interval, parquet_dir=cfg.parquet_dir
            )
            path, info = write_universe(
                candles,
                ucfg,
                universe_dir=cfg.universe_dir,
                missing=missing,
                incomplete=incomplete,
            )
            print(
                f"universe {path.name}: {info['symbols_with_data']} symbols with data, "
                f"{info['first_bar']} .. {info['last_bar']}"
            )
            print(f"  ever in top {ucfg.top_n}: {len(info['ever_in_top_n'])}")
            print(f"  of those delisted: {info['ever_in_top_n_and_delisted']}")
            print(f"  peg suspects (check exclude_bases): {info['peg_suspects']}")
            print(f"  >{PRICE_JUMP_FACTOR:g}x bar-to-bar jumps (check tickers): {info['price_jumps']}")
            if incomplete:
                print(f"  built WITHOUT {len(incomplete)} incomplete symbol(s); see manifest")
            print(f"  manifest: {path.with_suffix('.json')}")
    except (ValueError, httpx.HTTPError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
