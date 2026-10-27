"""USD-M futures archive downloader: URLs, parsing, checksums, 404s, duplicates, manifest."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from lab.config import DEFAULT_CONFIG_PATH, DerivativesConfig, load_config
from lab.data import archive, derivatives
from lab.data.derivatives import (
    FUNDING_COLUMNS,
    KLINE_CLOSE_TIME,
    KLINE_COLUMNS,
    KLINE_FRAME_COLUMNS,
    METRICS_COLUMNS,
    SCHEMA_VERSION,
    build_manifest,
    content_sha256,
    derivatives_url,
    download,
    load_series,
    manifest_sha256,
    parse_csv,
)
from lab.data.derivatives_align import align_dataset

BASE = "https://data.binance.vision/data/futures/um"
FUNDING_HEADER = "calc_time,funding_interval_hours,last_funding_rate"
KLINE_HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,"
    "taker_buy_quote_volume,ignore"
)
METRICS_HEADER = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,"
    "sum_taker_long_short_vol_ratio"
)
H = 3600 * 1000


def ms(ts: str) -> int:
    return int(pd.Timestamp(ts, tz="UTC").timestamp() * 1000)


def funding_csv(start: str, n: int, *, hours: int = 8, skip: tuple[int, ...] = ()) -> bytes:
    t0 = ms(start)
    rows = [FUNDING_HEADER]
    for i in range(n):
        if i not in skip:
            rows.append(f"{t0 + i * hours * H},{hours},{0.0001 * (i + 1):.8f}")
    return ("\n".join(rows) + "\n").encode()


def kline_csv(start: str, n: int, *, step_h: int = 4, skip: tuple[int, ...] = ()) -> bytes:
    t0 = ms(start)
    rows = [KLINE_HEADER]
    for i in range(n):
        if i in skip:
            continue
        ot = t0 + i * step_h * H
        o = -0.0003 + i * 0.0001
        rows.append(
            f"{ot},{o:.8f},{o + 0.0002:.8f},{o - 0.0002:.8f},{o + 0.0001:.8f},0,"
            f"{ot + step_h * H - 1},0,{100 + i},0,0,0"
        )
    return ("\n".join(rows) + "\n").encode()


def metrics_csv(
    start: str, n: int, *, symbol: str = "BTCUSDT", epoch: bool = False, skip: tuple[int, ...] = ()
) -> bytes:
    t0 = pd.Timestamp(start)
    rows = [METRICS_HEADER]
    for i in range(n):
        if i in skip:
            continue
        t = t0 + pd.Timedelta(minutes=5 * i)
        stamp = str(ms(str(t))) if epoch else t.strftime("%Y-%m-%d %H:%M:%S")
        rows.append(f"{stamp},{symbol},{1000.0 + i},{5e7 + i},1.5,1.6,1.7,{0.9 + i / 100:.4f}")
    return ("\n".join(rows) + "\n").encode()


def to_zip(csv: bytes, name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, csv)
    return buf.getvalue()


class FakeFuturesArchive:
    """In-memory futures/um archive: {url: bytes}; anything else is 404."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []

    def add(
        self,
        dataset: str,
        label: str,
        csv: bytes,
        *,
        symbol: str = "BTCUSDT",
        interval: str | None = None,
        bad_sum: bool = False,
        no_sum: bool = False,
    ) -> bytes:
        url = derivatives_url(dataset, symbol, label, base_url=BASE, interval=interval)
        name = url.rsplit("/", 1)[-1]
        payload = to_zip(csv, name.replace(".zip", ".csv"))
        self.files[url] = payload
        if not no_sum:
            digest = hashlib.sha256(b"tampered" if bad_sum else payload).hexdigest()
            self.files[url + ".CHECKSUM"] = f"{digest}  {name}\n".encode()
        return payload

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.files:
            return httpx.Response(200, content=self.files[url])
        return httpx.Response(404, content=b"<Error>NoSuchKey</Error>")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> pd.Timestamp:
    now = pd.Timestamp("2024-03-15 12:00", tz="UTC")
    monkeypatch.setattr(archive, "_utc_now", lambda: now)
    return now


@pytest.fixture
def fake() -> FakeFuturesArchive:
    return FakeFuturesArchive()


def run(fake: FakeFuturesArchive, dataset: str, start: str, end: str, data_dir: Path, **kw):
    with fake.client() as client:
        return download(
            dataset, "BTCUSDT", start, end, data_dir=data_dir, base_url=BASE, client=client, **kw
        )


# --------------------------------------------------------------------------- URLs


def test_urls_match_the_futures_archive_layout() -> None:
    assert derivatives_url("fundingRate", "BTCUSDT", "2024-01", base_url=BASE + "/") == (
        f"{BASE}/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    )
    for ds in ("premiumIndexKlines", "markPriceKlines", "indexPriceKlines"):
        assert derivatives_url(ds, "BTCUSDT", "2024-01", base_url=BASE, interval="4h") == (
            f"{BASE}/monthly/{ds}/BTCUSDT/4h/BTCUSDT-4h-2024-01.zip"
        )
    assert derivatives_url("metrics", "BTCUSDT", "2024-01-01", base_url=BASE) == (
        f"{BASE}/daily/metrics/BTCUSDT/BTCUSDT-metrics-2024-01-01.zip"
    )


@pytest.mark.parametrize(
    ("dataset", "symbol", "label", "interval", "match"),
    [
        ("bookTicker", "BTCUSDT", "2024-01", None, "'bookTicker'"),
        ("fundingRate", "btcusdt", "2024-01", None, "'btcusdt'"),
        ("fundingRate", "BTCUSDT", "2024-01", "4h", "interval='4h'"),
        ("markPriceKlines", "BTCUSDT", "2024-01", None, "needs an interval"),
        ("markPriceKlines", "BTCUSDT", "2024-01", "7h", "'7h'"),
        ("fundingRate", "BTCUSDT", "2024-13", None, "2024-13"),
        ("metrics", "BTCUSDT", "2024-02-30", None, "'2024-02-30'"),
        ("metrics", "BTCUSDT", "2024-02", None, "'2024-02'"),
    ],
)
def test_bad_url_inputs_raise_naming_the_input(
    dataset: str, symbol: str, label: str, interval: str | None, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        derivatives_url(dataset, symbol, label, base_url=BASE, interval=interval)


# --------------------------------------------------------------------------- parsing


def test_funding_parses_to_documented_columns() -> None:
    df = parse_csv("fundingRate", funding_csv("2024-01-01", 3))
    assert tuple(df.columns) == FUNDING_COLUMNS
    assert all(df[c].dtype == np.float64 for c in df.columns)
    assert df.index.name == "time" and str(df.index.tz) == "UTC"
    assert df.index[0] == pd.Timestamp("2024-01-01", tz="UTC")  # calc_time, the archive's own
    assert df.index[1] - df.index[0] == pd.Timedelta(hours=8)
    assert df["funding_rate"].tolist() == pytest.approx([0.0001, 0.0002, 0.0003], abs=1e-12)
    assert df["funding_interval_hours"].tolist() == [8.0, 8.0, 8.0]


def test_verified_binance_funding_row_parses() -> None:
    df = parse_csv("fundingRate", f"{FUNDING_HEADER}\n1704067200000,8,0.00037409\n".encode())
    assert df.index[0] == pd.Timestamp("2024-01-01 00:00", tz="UTC")
    assert df["funding_rate"].iloc[0] == pytest.approx(0.00037409, abs=1e-15)


def test_kline_datasets_parse_ohlc_by_open_time_keeping_signed_values() -> None:
    df = parse_csv("premiumIndexKlines", kline_csv("2024-01-01", 4))
    assert tuple(df.columns) == KLINE_FRAME_COLUMNS == (*KLINE_COLUMNS, KLINE_CLOSE_TIME)
    assert all(df[c].dtype == np.float64 for c in KLINE_COLUMNS)
    assert df.index[0] == pd.Timestamp("2024-01-01", tz="UTC")  # open_time
    assert df["open"].iloc[0] == pytest.approx(-0.0003, abs=1e-15)  # negative premium kept
    assert len(df) == 4


def test_kline_close_time_is_kept_from_the_csv() -> None:
    df = parse_csv("premiumIndexKlines", kline_csv("2024-01-01", 2))
    assert str(df[KLINE_CLOSE_TIME].dtype) == "datetime64[ns, UTC]"
    assert df[KLINE_CLOSE_TIME].iloc[0] == pd.Timestamp("2024-01-01 03:59:59.999", tz="UTC")
    daily = parse_csv("markPriceKlines", kline_csv("2024-01-01", 2, step_h=24))
    assert daily[KLINE_CLOSE_TIME].iloc[1] == pd.Timestamp("2024-01-02 23:59:59.999", tz="UTC")


def test_kline_close_time_before_open_rejects_the_file() -> None:
    row = f"{ms('2024-01-01 04:00')},1,1,1,1,0,{ms('2024-01-01 03:00')},0,1,0,0,0"
    with pytest.raises(derivatives.RejectedFileError, match="close_time before open_time"):
        parse_csv("premiumIndexKlines", f"{KLINE_HEADER}\n{row}\n".encode(), filename="x.zip")


def test_csv_without_header_is_read_positionally() -> None:
    body = b"1704067200000,8,0.0001\n1704096000000,8,0.0002\n"
    df = parse_csv("fundingRate", body)
    assert df["funding_rate"].tolist() == pytest.approx([0.0001, 0.0002], abs=1e-15)


def test_metrics_string_timestamp_is_utc() -> None:
    df = parse_csv("metrics", metrics_csv("2024-01-01 00:05:00", 3), symbol="BTCUSDT")
    assert tuple(df.columns) == METRICS_COLUMNS
    assert all(df[c].dtype == np.float64 for c in df.columns)
    assert df.index[0] == pd.Timestamp("2024-01-01 00:05:00", tz="UTC")
    assert df.index[0].value == ms("2024-01-01 00:05:00") * 1_000_000
    assert df["sum_open_interest"].tolist() == [1000.0, 1001.0, 1002.0]


def test_metrics_integer_epoch_timestamp_is_tolerated() -> None:
    a = parse_csv("metrics", metrics_csv("2024-01-01 00:05:00", 3))
    b = parse_csv("metrics", metrics_csv("2024-01-01 00:05:00", 3, epoch=True))
    pd.testing.assert_frame_equal(a, b)


def test_identical_duplicate_lines_are_dropped() -> None:
    csv = metrics_csv("2020-09-01 00:00:00", 3)
    lines = csv.decode().strip().splitlines()
    doubled = "\n".join([lines[0], *lines[1:], *lines[1:]]) + "\n"
    df = parse_csv("metrics", doubled.encode())
    pd.testing.assert_frame_equal(df, parse_csv("metrics", csv))
    assert len(df) == 3


def test_conflicting_duplicate_rows_reject_the_file() -> None:
    csv = funding_csv("2024-01-01", 2) + b"1704067200000,8,0.0099\n"
    with pytest.raises(derivatives.RejectedFileError, match="conflicting rows"):
        parse_csv("fundingRate", csv, filename="BTCUSDT-fundingRate-2024-01.zip")


def test_metrics_rows_for_another_symbol_reject_the_file() -> None:
    with pytest.raises(derivatives.RejectedFileError, match="ETHUSDT"):
        parse_csv("metrics", metrics_csv("2024-01-01", 2, symbol="ETHUSDT"), symbol="BTCUSDT")


# --------------------------------------------------------------------------- download


def test_download_writes_parquet_and_records_source_sha(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    payload = fake.add("fundingRate", "2024-01", funding_csv("2024-01-01", 93))
    res = run(fake, "fundingRate", "2024-01", "2024-01", tmp_path)
    path = tmp_path / "fundingRate" / "BTCUSDT" / "2024-01.parquet"
    assert res.written == [path] and res.rows_written == 93
    side = json.loads((path.parent / derivatives.SIDECAR_NAME).read_text())
    assert side["sources"]["2024-01"]["sha256"] == hashlib.sha256(payload).hexdigest()
    pd.testing.assert_frame_equal(
        pd.read_parquet(path), parse_csv("fundingRate", funding_csv("2024-01-01", 93))
    )


def test_kline_download_uses_interval_directory(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    fake.add("markPriceKlines", "2024-01", kline_csv("2024-01-01", 186), interval="4h")
    res = run(fake, "markPriceKlines", "2024-01", "2024-01", tmp_path, interval="4h")
    assert res.written == [tmp_path / "markPriceKlines" / "BTCUSDT" / "4h" / "2024-01.parquet"]


def test_downloaded_daily_klines_align_from_their_own_close_time(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    # W1 end to end: the Parquet keeps close_time, so daily klines on 4h bars are visible only
    # at the daily close, and declaring them 4h is an error rather than 20h of look-ahead.
    fake.add("premiumIndexKlines", "2024-01", kline_csv("2024-01-01", 3, step_h=24), interval="1d")
    run(fake, "premiumIndexKlines", "2024-01", "2024-01", tmp_path, interval="1d")
    df = load_series("premiumIndexKlines", "BTCUSDT", data_dir=tmp_path, interval="1d")
    assert tuple(df.columns) == KLINE_FRAME_COLUMNS
    assert df[KLINE_CLOSE_TIME].iloc[0] == pd.Timestamp("2024-01-01 23:59:59.999", tz="UTC")
    bars = pd.date_range(pd.Timestamp("2024-01-01", tz="UTC"), periods=6, freq="4h")
    out = align_dataset("premiumIndexKlines", df, bars, 4 * 3600, stale_after_intervals=2.0)
    assert out["close"].isna().tolist() == [True] * 5 + [False]
    assert out["close"].iloc[5] == pytest.approx(-0.0002, abs=1e-15)
    with pytest.raises(ValueError, match="disagrees with the data"):
        align_dataset(
            "premiumIndexKlines", df, bars, 4 * 3600, interval_seconds=4 * 3600,
            stale_after_intervals=2.0,
        )  # fmt: skip


def test_kline_parquet_without_close_time_loads_as_nat_and_alignment_refuses_it(
    tmp_path: Path,
) -> None:
    # A schema-1 Parquet (OHLC only) still loads, but alignment asks for a --refresh download.
    old = parse_csv("premiumIndexKlines", kline_csv("2024-01-01", 3)).drop(columns=KLINE_CLOSE_TIME)
    path = tmp_path / "premiumIndexKlines" / "BTCUSDT" / "4h" / "2024-01.parquet"
    archive.write_parquet(old, path)
    df = load_series("premiumIndexKlines", "BTCUSDT", data_dir=tmp_path, interval="4h")
    assert len(df) == 3 and df[KLINE_CLOSE_TIME].isna().all()
    assert str(df[KLINE_CLOSE_TIME].dtype) == "datetime64[ns, UTC]"
    bars = pd.date_range(pd.Timestamp("2024-01-01", tz="UTC"), periods=3, freq="4h")
    with pytest.raises(ValueError, match="--refresh"):
        align_dataset("premiumIndexKlines", df, bars, 4 * 3600, stale_after_intervals=2.0)


def test_checksum_mismatch_raises_naming_the_file(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    fake.add("fundingRate", "2024-01", funding_csv("2024-01-01", 3), bad_sum=True)
    with pytest.raises(ValueError, match=r"BTCUSDT-fundingRate-2024-01\.zip: SHA-256 mismatch"):
        run(fake, "fundingRate", "2024-01", "2024-01", tmp_path)
    assert not (tmp_path / "fundingRate" / "BTCUSDT" / "2024-01.parquet").exists()


def test_missing_checksum_raises_naming_the_file(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    fake.add("metrics", "2024-01-01", metrics_csv("2024-01-01", 3), no_sum=True)
    with pytest.raises(ValueError, match=r"BTCUSDT-metrics-2024-01-01.zip: .CHECKSUM not found"):
        run(fake, "metrics", "2024-01", "2024-01", tmp_path)


def test_404_is_recorded_as_not_found_and_download_continues(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    fake.add("fundingRate", "2024-02", funding_csv("2024-02-01", 87))
    res = run(fake, "fundingRate", "2024-01", "2024-02", tmp_path)
    assert res.not_found == ["2024-01"]
    assert [p.name for p in res.written] == ["2024-02.parquet"]
    manifest = build_manifest(tmp_path, gap_factor=1.5)
    assert manifest["series"]["fundingRate/BTCUSDT"]["not_found"] == ["2024-01"]


def test_conflicting_file_is_rejected_listed_and_not_written(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    good = metrics_csv("2024-01-02 00:00:00", 288)
    bad = metrics_csv("2024-01-01 00:00:00", 3) + b"2024-01-01 00:05:00,BTCUSDT,9,9,9,9,9,9\n"
    fake.add("metrics", "2024-01-01", bad)
    fake.add("metrics", "2024-01-02", good)
    with fake.client() as client:
        res = download(
            "metrics", "BTCUSDT", "2024-01", "2024-01", data_dir=tmp_path, base_url=BASE,
            client=client,
        )  # fmt: skip
    assert "2024-01-01" in res.rejected and "conflicting rows" in res.rejected["2024-01-01"]
    assert [p.name for p in res.written] == ["2024-01-02.parquet"]  # continued past the reject
    assert not (tmp_path / "metrics" / "BTCUSDT" / "2024-01-01.parquet").exists()
    entry = build_manifest(tmp_path, gap_factor=1.5)["series"]["metrics/BTCUSDT"]
    assert "conflicting rows" in entry["rejected"]["2024-01-01"]
    assert entry["rows"] == 288


def test_sidecar_is_checkpointed_during_a_long_download(
    tmp_path: Path,
    fake: FakeFuturesArchive,
    fixed_now: pd.Timestamp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a run killed mid-way (no finally) must still have recorded the source hashes it wrote
    monkeypatch.setattr(derivatives, "SIDECAR_CHECKPOINT_EVERY", 2)
    for day in ("2024-01-01", "2024-01-02", "2024-01-03"):
        fake.add("metrics", day, metrics_csv(f"{day} 00:00:00", 3))
    sidecar = tmp_path / "metrics" / "BTCUSDT" / derivatives.SIDECAR_NAME
    seen: list[set[str]] = []
    inner = fake.handler

    def spy(request: httpx.Request) -> httpx.Response:
        if "2024-01-03.zip" in str(request.url) and not str(request.url).endswith("CHECKSUM"):
            seen.append(
                set(json.loads(sidecar.read_text())["sources"]) if sidecar.exists() else set()
            )
        return inner(request)

    monkeypatch.setattr(fake, "handler", spy)
    run(fake, "metrics", "2024-01", "2024-01", tmp_path)
    assert seen == [{"2024-01-01", "2024-01-02"}]


def test_identical_duplicate_lines_in_a_download_are_written_once(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    csv = metrics_csv("2024-01-01 00:00:00", 4)
    lines = csv.decode().strip().splitlines()
    fake.add("metrics", "2024-01-01", ("\n".join(lines + lines[1:]) + "\n").encode())
    res = run(fake, "metrics", "2024-01", "2024-01", tmp_path)
    assert res.rows_written == 4 and not res.rejected


def test_current_and_future_periods_are_never_written(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    for label in ("2024-02", "2024-03", "2024-04"):
        fake.add("fundingRate", label, funding_csv(f"{label}-01", 3))
    res = run(fake, "fundingRate", "2024-02", "2024-04", tmp_path)
    assert res.incomplete == ["2024-03", "2024-04"]
    assert [p.name for p in res.written] == ["2024-02.parquet"]
    assert not any("2024-03" in u or "2024-04" in u for u in fake.requests)

    # daily: now is 2024-03-15 12:00, so 03-14 is the last finished day
    for day in ("2024-03-14", "2024-03-15", "2024-03-16"):
        fake.add("metrics", day, metrics_csv(f"{day} 00:00:00", 2))
    res = run(fake, "metrics", "2024-03", "2024-03", tmp_path)
    assert "2024-03-14.parquet" in [p.name for p in res.written]
    assert res.incomplete[0] == "2024-03-15" and res.incomplete[-1] == "2024-03-31"
    assert not (tmp_path / "metrics" / "BTCUSDT" / "2024-03-15.parquet").exists()


def test_daily_metrics_expand_months_to_days(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    res = run(fake, "metrics", "2024-02", "2024-02", tmp_path)
    assert len(res.not_found) == 29  # leap February, every day requested
    assert res.not_found[0] == "2024-02-01" and res.not_found[-1] == "2024-02-29"


def test_files_on_disk_are_skipped_unless_refresh(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    fake.add("fundingRate", "2024-01", funding_csv("2024-01-01", 3))
    run(fake, "fundingRate", "2024-01", "2024-01", tmp_path)
    n = len(fake.requests)
    res = run(fake, "fundingRate", "2024-01", "2024-01", tmp_path)
    assert res.skipped and not res.written and len(fake.requests) == n
    res = run(fake, "fundingRate", "2024-01", "2024-01", tmp_path, refresh=True)
    assert res.written


def test_download_validates_dataset_and_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs an interval"):
        download("indexPriceKlines", "BTCUSDT", "2024-01", "2024-01", data_dir=tmp_path,
                 base_url=BASE)  # fmt: skip
    with pytest.raises(ValueError, match="'nope'"):
        download("nope", "BTCUSDT", "2024-01", "2024-01", data_dir=tmp_path, base_url=BASE)


# --------------------------------------------------------------------------- load_series


def test_load_series_concatenates_months_and_filters_inclusively(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    fake.add("fundingRate", "2024-01", funding_csv("2024-01-01", 93))
    fake.add("fundingRate", "2024-02", funding_csv("2024-02-01", 87))
    run(fake, "fundingRate", "2024-01", "2024-02", tmp_path)
    df = load_series("fundingRate", "BTCUSDT", data_dir=tmp_path)
    assert len(df) == 180 and df.index.is_monotonic_increasing and df.index.is_unique
    assert tuple(df.columns) == FUNDING_COLUMNS
    assert df.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert df.index[-1] == pd.Timestamp("2024-02-29 16:00", tz="UTC")
    cut = load_series(
        "fundingRate",
        "BTCUSDT",
        data_dir=tmp_path,
        start=pd.Timestamp("2024-01-31 16:00", tz="UTC"),
        end=pd.Timestamp("2024-02-01 08:00", tz="UTC"),
    )
    assert list(cut.index) == [
        pd.Timestamp("2024-01-31 16:00", tz="UTC"),
        pd.Timestamp("2024-02-01 00:00", tz="UTC"),
        pd.Timestamp("2024-02-01 08:00", tz="UTC"),
    ]


def test_load_series_with_nothing_on_disk_is_an_empty_frame(tmp_path: Path) -> None:
    for ds, iv, cols in (
        ("fundingRate", None, FUNDING_COLUMNS),
        ("premiumIndexKlines", "4h", KLINE_COLUMNS),
        ("metrics", None, METRICS_COLUMNS),
    ):
        df = load_series(ds, "BTCUSDT", data_dir=tmp_path, interval=iv)
        frame_cols = (*cols, KLINE_CLOSE_TIME) if ds == "premiumIndexKlines" else cols
        assert df.empty and tuple(df.columns) == frame_cols
        assert isinstance(df.index, pd.DatetimeIndex) and str(df.index.tz) == "UTC"
        assert df.index.name == "time"
        assert all(df[c].dtype == np.float64 for c in cols)
    empty_k = load_series("markPriceKlines", "BTCUSDT", data_dir=tmp_path, interval="4h")
    assert str(empty_k[KLINE_CLOSE_TIME].dtype) == "datetime64[ns, UTC]"


@pytest.mark.parametrize(
    ("dataset", "interval", "match"),
    [
        ("markPriceKlines", None, "needs an interval"),
        ("fundingRate", "4h", "has no interval"),
        ("metrics", "5m", "has no interval"),
        ("premiumIndexKlines", "3x", "'3x'"),
        ("aggTrades", None, "'aggTrades'"),
    ],
)
def test_load_series_validates_interval(dataset: str, interval: str | None, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_series(dataset, "BTCUSDT", data_dir=Path("/nonexistent"), interval=interval)


def test_load_series_metrics_across_days_keeps_boundary_rows_once(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    # each daily file runs 00:05 .. next day 00:00 (288 rows), so day boundaries touch
    fake.add("metrics", "2024-01-01", metrics_csv("2024-01-01 00:05:00", 288))
    fake.add("metrics", "2024-01-02", metrics_csv("2024-01-02 00:05:00", 288))
    run(fake, "metrics", "2024-01", "2024-01", tmp_path)
    df = load_series("metrics", "BTCUSDT", data_dir=tmp_path)
    assert len(df) == 576 and df.index.is_unique
    one_day = load_series(
        "metrics",
        "BTCUSDT",
        data_dir=tmp_path,
        start=pd.Timestamp("2024-01-02 00:00"),  # naive -> UTC
        end=pd.Timestamp("2024-01-02 00:10", tz="UTC"),
    )
    assert len(one_day) == 3


# --------------------------------------------------------------------------- manifest


def test_manifest_records_first_last_and_gaps_without_synthesising_rows(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    # 4h premium index with bars 3 and 4 missing: one hole from bar 2 to bar 5
    fake.add(
        "premiumIndexKlines", "2024-01", kline_csv("2024-01-01", 10, skip=(3, 4)), interval="4h"
    )
    run(fake, "premiumIndexKlines", "2024-01", "2024-01", tmp_path, interval="4h")
    manifest = build_manifest(tmp_path, gap_factor=1.5)
    entry = manifest["series"]["premiumIndexKlines/BTCUSDT/4h"]
    assert entry["first"] == "2024-01-01T00:00:00+00:00"
    assert entry["last"] == "2024-01-02T12:00:00+00:00"
    assert entry["rows"] == 8 and entry["files"] == 1  # 10 - 2 missing, nothing filled in
    assert entry["gaps"] == [
        {"from": "2024-01-01T08:00:00+00:00", "to": "2024-01-01T20:00:00+00:00"}
    ]
    assert len(load_series("premiumIndexKlines", "BTCUSDT", data_dir=tmp_path, interval="4h")) == 8
    on_disk = json.loads((tmp_path / "manifest.json").read_text())
    assert on_disk == manifest


def test_funding_gaps_use_the_rows_own_interval(tmp_path: Path) -> None:
    times = pd.to_datetime(
        ["2024-01-01 00:00", "2024-01-01 08:00", "2024-01-01 12:00", "2024-01-01 16:00",
         "2024-01-02 00:00", "2024-01-02 16:00"],
        utc=True,
    )  # fmt: skip
    frame = pd.DataFrame(
        {"funding_rate": [0.0001] * 6, "funding_interval_hours": [8.0, 8.0, 4.0, 4.0, 8.0, 8.0]},
        index=pd.DatetimeIndex(times, name="time"),
    )
    gaps = derivatives.find_series_gaps("fundingRate", frame, gap_factor=1.5)
    # 8h->4h and 4h->8h switches are not gaps; 16h after an 8h settlement is
    assert gaps == [{"from": "2024-01-02T00:00:00+00:00", "to": "2024-01-02T16:00:00+00:00"}]


def test_metrics_gap_uses_five_minute_step() -> None:
    frame = parse_csv("metrics", metrics_csv("2024-01-01 00:00:00", 6, skip=(2,)))
    gaps = derivatives.find_series_gaps("metrics", frame, gap_factor=1.5)
    assert gaps == [{"from": "2024-01-01T00:05:00+00:00", "to": "2024-01-01T00:15:00+00:00"}]


def test_gap_factor_below_one_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="gap_factor"):
        build_manifest(tmp_path, gap_factor=0.5)


def test_manifest_sha256_is_none_then_stable(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    assert manifest_sha256(tmp_path) is None
    fake.add("fundingRate", "2024-01", funding_csv("2024-01-01", 3))
    run(fake, "fundingRate", "2024-01", "2024-01", tmp_path)
    build_manifest(tmp_path, gap_factor=1.5)
    first = manifest_sha256(tmp_path)
    assert first == hashlib.sha256((tmp_path / "manifest.json").read_bytes()).hexdigest()
    build_manifest(tmp_path, gap_factor=1.5)
    assert manifest_sha256(tmp_path) == first  # deterministic: no wall-clock field
    fake.add("fundingRate", "2024-02", funding_csv("2024-02-01", 3))
    run(fake, "fundingRate", "2024-02", "2024-02", tmp_path)
    build_manifest(tmp_path, gap_factor=1.5)
    assert manifest_sha256(tmp_path) != first


def test_manifest_sha256_covers_parquet_content(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    # W2: editing a value on disk (source zip sha unchanged) must change the manifest sha256.
    fake.add("fundingRate", "2024-01", funding_csv("2024-01-01", 3))
    run(fake, "fundingRate", "2024-01", "2024-01", tmp_path)
    manifest = build_manifest(tmp_path, gap_factor=1.5)
    assert manifest["schema_version"] == SCHEMA_VERSION == 2
    entry = manifest["series"]["fundingRate/BTCUSDT"]
    loaded = load_series("fundingRate", "BTCUSDT", data_dir=tmp_path)
    assert entry["content_sha256"] == content_sha256(loaded)
    clean = manifest_sha256(tmp_path)

    path = tmp_path / "fundingRate" / "BTCUSDT" / "2024-01.parquet"
    original = pd.read_parquet(path)
    tampered = original.copy()
    tampered["funding_rate"] *= 100
    archive.write_parquet(tampered, path)
    after = build_manifest(tmp_path, gap_factor=1.5)
    assert after["series"]["fundingRate/BTCUSDT"]["sources"] == entry["sources"]  # zip unchanged
    assert after["series"]["fundingRate/BTCUSDT"]["content_sha256"] != entry["content_sha256"]
    assert manifest_sha256(tmp_path) != clean

    archive.write_parquet(original, path)  # same data rewritten -> identical sha
    build_manifest(tmp_path, gap_factor=1.5)
    assert manifest_sha256(tmp_path) == clean


def test_content_sha256_is_content_not_encoding() -> None:
    a = parse_csv("premiumIndexKlines", kline_csv("2024-01-01", 4))
    assert content_sha256(a) == content_sha256(a.copy())
    # same values through a different float path and a different datetime unit: same hash
    b = a.copy()
    b[KLINE_CLOSE_TIME] = b[KLINE_CLOSE_TIME].dt.as_unit("ms")
    b.index = b.index.as_unit("us")
    assert content_sha256(b) == content_sha256(a)
    # any changed value, timestamp, close_time, NaN, or column order changes it
    variants = [a.copy() for _ in range(5)]
    variants[0].iloc[2, 0] += 1e-12
    variants[1].index = variants[1].index + pd.Timedelta(milliseconds=1)
    variants[2][KLINE_CLOSE_TIME] = variants[2][KLINE_CLOSE_TIME] + pd.Timedelta(milliseconds=1)
    variants[3].iloc[0, 1] = np.nan
    variants[4] = variants[4][list(reversed(variants[4].columns))]
    assert len({content_sha256(v) for v in variants} | {content_sha256(a)}) == 6
    # NaN payloads are canonicalised
    c, d = variants[3].copy(), variants[3].copy()
    raw = d["high"].to_numpy().copy()
    raw.view(np.int64)[0] = np.int64(0x7FF8000000000001)  # another quiet NaN bit pattern
    d["high"] = raw
    assert content_sha256(c) == content_sha256(d)


def test_manifest_content_sha_includes_kline_close_time(
    tmp_path: Path, fake: FakeFuturesArchive, fixed_now: pd.Timestamp
) -> None:
    fake.add("premiumIndexKlines", "2024-01", kline_csv("2024-01-01", 6), interval="4h")
    run(fake, "premiumIndexKlines", "2024-01", "2024-01", tmp_path, interval="4h")
    first = build_manifest(tmp_path, gap_factor=1.5)
    path = tmp_path / "premiumIndexKlines" / "BTCUSDT" / "4h" / "2024-01.parquet"
    frame = pd.read_parquet(path)
    frame[KLINE_CLOSE_TIME] = frame[KLINE_CLOSE_TIME] + pd.Timedelta(hours=20)
    archive.write_parquet(frame, path)
    second = build_manifest(tmp_path, gap_factor=1.5)
    key = "premiumIndexKlines/BTCUSDT/4h"
    assert second["series"][key]["content_sha256"] != first["series"][key]["content_sha256"]


def test_manifest_cli_summarises_disk(
    tmp_path: Path,
    fake: FakeFuturesArchive,
    fixed_now: pd.Timestamp,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake.add("fundingRate", "2024-01", funding_csv("2024-01-01", 3))
    run(fake, "fundingRate", "2024-01", "2024-01", tmp_path)
    assert derivatives.main(["--data-dir", str(tmp_path), "manifest"]) == 0
    out = capsys.readouterr().out
    assert "fundingRate/BTCUSDT: 3 rows in 1 files" in out
    assert (tmp_path / "manifest.json").exists()


# --------------------------------------------------------------------------- config


def test_config_has_derivatives_section() -> None:
    cfg = load_config()
    assert cfg.derivatives == DerivativesConfig(
        archive_base_url="https://data.binance.vision/data/futures/um",
        stale_after_intervals=2.0,
        gap_factor=1.5,
    )
    assert cfg.derivatives_dir == cfg.data_dir / "derivatives"


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("gap_factor = 1.5", "gap_factor = 0.9", r"derivatives\.gap_factor"),
        ("stale_after_intervals = 2.0", "stale_after_intervals = 0", r"derivatives\.stale_after"),
        ("stale_after_intervals = 2.0", 'stale_after_intervals = "2"', r"derivatives\.stale_after"),
    ],
)
def test_bad_derivatives_settings_are_rejected(
    tmp_path: Path, old: str, new: str, match: str
) -> None:
    raw = DEFAULT_CONFIG_PATH.read_text()
    assert old in raw
    path = tmp_path / "lab.toml"
    path.write_text(raw.replace(old, new))
    with pytest.raises(ValueError, match=match):
        load_config(path)
