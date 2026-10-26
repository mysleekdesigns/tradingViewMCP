"""Archive downloader: URL, CSV parsing (ms/µs, header), checksum, 404, range writing. Offline."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from lab.data import archive
from lab.data.archive import archive_url, download_month, download_range, parse_kline_csv
from lab.types import validate_candles

BASE = "https://data.binance.vision/data/spot"
H4_MS = 4 * 3600 * 1000


def kline_rows(
    start: str, n: int, *, step_ms: int = H4_MS, micro: bool = False, skip: tuple[int, ...] = ()
) -> list[list[str]]:
    t0 = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    rows = []
    for i in range(n):
        if i in skip:
            continue
        ot = t0 + i * step_ms
        ct = ot + step_ms - 1
        mult = 1000 if micro else 1
        p = 100.0 + i
        vol = 0.0 if i == 2 else 10.0 + i
        rows.append(
            [
                str(ot * mult),
                f"{p:.8f}",
                f"{p + 2:.8f}",
                f"{p - 1:.8f}",
                f"{p + 1:.8f}",
                f"{vol:.8f}",
                str(ct * mult + (999 if micro else 0)),
                "1000.0",
                "42",
                "5.0",
                "500.0",
                "0",
            ]
        )
    return rows


def to_csv(rows: list[list[str]], header: bool = False) -> bytes:
    lines = [",".join(r) for r in rows]
    if header:
        lines.insert(0, ",".join(archive.KLINE_CSV_COLUMNS))
    return ("\n".join(lines) + "\n").encode()


def to_zip(csv: bytes, name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, csv)
    return buf.getvalue()


def checksum(payload: bytes, filename: str) -> bytes:
    return f"{hashlib.sha256(payload).hexdigest()}  {filename}\n".encode()


class FakeArchive:
    """In-memory data.binance.vision: {url: bytes}; anything else is 404."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []

    def add_month(
        self,
        symbol: str,
        interval: str,
        year: int,
        month: int,
        csv: bytes,
        *,
        bad_sum: bool = False,
    ) -> None:
        url = archive_url(BASE, symbol, interval, year, month)
        name = url.rsplit("/", 1)[-1]
        payload = to_zip(csv, name.replace(".zip", ".csv"))
        self.files[url] = payload
        self.files[url + ".CHECKSUM"] = checksum(b"tampered" if bad_sum else payload, name)

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


def test_archive_url_matches_binance_layout() -> None:
    assert archive_url(BASE + "/", "BTCUSDT", "4h", 2024, 1) == (
        "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/4h/BTCUSDT-4h-2024-01.zip"
    )


@pytest.mark.parametrize(
    ("symbol", "interval", "year", "month", "match"),
    [
        ("btcusdt", "4h", 2024, 1, "'btcusdt'"),
        ("BTCUSDT", "7h", 2024, 1, "'7h'"),
        ("BTCUSDT", "4h", 2024, 13, "2024-13"),
    ],
)
def test_archive_url_rejects_bad_input(
    symbol: str, interval: str, year: int, month: int, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        archive_url(BASE, symbol, interval, year, month)


def test_parse_ms_file_follows_candle_convention() -> None:
    df = parse_kline_csv(to_csv(kline_rows("2024-01-01", 6)))
    validate_candles(df)
    assert df.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert df.index[1] - df.index[0] == pd.Timedelta(hours=4)
    assert df.index.name == "time"
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "tradable"]
    assert all(df[c].dtype == np.float64 for c in ("open", "high", "low", "close", "volume"))
    assert df["tradable"].dtype == bool
    assert df.loc[df.index[0], "open"] == 100.0
    assert df.loc[df.index[3], "close"] == 104.0


def test_parse_microsecond_file_matches_millisecond_file() -> None:
    ms = parse_kline_csv(to_csv(kline_rows("2025-02-01", 6)))
    us = parse_kline_csv(to_csv(kline_rows("2025-02-01", 6, micro=True)))
    pd.testing.assert_frame_equal(ms, us)
    assert us.index[0] == pd.Timestamp("2025-02-01", tz="UTC")


def test_parse_tolerates_header_row() -> None:
    rows = kline_rows("2024-01-01", 4)
    pd.testing.assert_frame_equal(
        parse_kline_csv(to_csv(rows, header=True)), parse_kline_csv(to_csv(rows))
    )


def test_zero_volume_bar_kept_but_untradable() -> None:
    df = parse_kline_csv(to_csv(kline_rows("2024-01-01", 5)))
    assert len(df) == 5
    assert df["volume"].iloc[2] == 0.0
    assert df["tradable"].tolist() == [True, True, False, True, True]


def test_missing_bars_stay_absent() -> None:
    df = parse_kline_csv(to_csv(kline_rows("2024-01-01", 8, skip=(3, 4))))
    assert len(df) == 6
    assert not df.isna().to_numpy().any()
    assert pd.Timestamp("2024-01-01 12:00", tz="UTC") not in df.index
    assert pd.Timestamp("2024-01-01 16:00", tz="UTC") not in df.index


def test_parse_rejects_duplicates_and_garbage() -> None:
    rows = kline_rows("2024-01-01", 3)
    with pytest.raises(ValueError, match="duplicate"):
        parse_kline_csv(to_csv([*rows, rows[0]]))
    with pytest.raises(ValueError, match="malformed"):
        parse_kline_csv(b"1704067200000,abc,1,1,1,1,0,0,0,0,0,0\n")


def test_parse_empty_file_returns_empty_candles() -> None:
    df = parse_kline_csv(b"")
    assert len(df) == 0
    validate_candles(df)


def test_download_month_verifies_and_parses() -> None:
    fake = FakeArchive()
    fake.add_month("BTCUSDT", "4h", 2024, 1, to_csv(kline_rows("2024-01-01", 6)))
    with fake.client() as client:
        df = download_month("BTCUSDT", "4h", 2024, 1, base_url=BASE, client=client)
    assert df is not None and len(df) == 6
    assert any(u.endswith(".CHECKSUM") for u in fake.requests)


def test_download_month_checksum_mismatch_names_file() -> None:
    fake = FakeArchive()
    fake.add_month("BTCUSDT", "4h", 2024, 1, to_csv(kline_rows("2024-01-01", 6)), bad_sum=True)
    with fake.client() as client, pytest.raises(ValueError, match=r"BTCUSDT-4h-2024-01\.zip"):
        download_month("BTCUSDT", "4h", 2024, 1, base_url=BASE, client=client)


def test_download_month_missing_checksum_refuses() -> None:
    fake = FakeArchive()
    fake.add_month("BTCUSDT", "4h", 2024, 1, to_csv(kline_rows("2024-01-01", 6)))
    del fake.files[archive_url(BASE, "BTCUSDT", "4h", 2024, 1) + ".CHECKSUM"]
    with fake.client() as client, pytest.raises(ValueError, match="CHECKSUM not found"):
        download_month("BTCUSDT", "4h", 2024, 1, base_url=BASE, client=client)


def test_download_month_404_returns_none() -> None:
    with FakeArchive().client() as client:
        assert download_month("BTCUSDT", "4h", 2017, 1, base_url=BASE, client=client) is None


def test_download_month_server_error_raises() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with client, pytest.raises(httpx.HTTPStatusError):
        download_month("BTCUSDT", "4h", 2024, 1, base_url=BASE, client=client)


def test_download_range_writes_skips_and_never_writes_current_month(
    tmp_path: Path, fixed_now: pd.Timestamp
) -> None:
    fake = FakeArchive()
    fake.add_month("BTCUSDT", "4h", 2023, 12, to_csv(kline_rows("2023-12-01", 4)))
    fake.add_month("BTCUSDT", "4h", 2024, 2, to_csv(kline_rows("2024-02-01", 5, micro=True)))
    # 2024-01 is missing from the archive (404); 2024-03 is the current month at fixed_now.
    fake.add_month("BTCUSDT", "4h", 2024, 3, to_csv(kline_rows("2024-03-01", 3)))
    with fake.client() as client:
        paths = download_range(
            "BTCUSDT", "4h", "2023-12", "2024-04", out_dir=tmp_path, base_url=BASE, client=client
        )
    folder = tmp_path / "BTCUSDT" / "4h"
    assert paths == [folder / "2023-12.parquet", folder / "2024-02.parquet"]
    assert not (folder / "2024-01.parquet").exists()
    assert not (folder / "2024-03.parquet").exists()
    assert not any("2024-03" in u or "2024-04" in u for u in fake.requests)

    stored = pd.read_parquet(folder / "2024-02.parquet")
    validate_candles(stored)
    assert stored.index[0] == pd.Timestamp("2024-02-01", tz="UTC")
    assert stored["tradable"].tolist() == [True, True, False, True, True]

    # Second run: months on disk are skipped (no new zip requests) unless refresh=True.
    fake.requests.clear()
    with fake.client() as client:
        again = download_range(
            "BTCUSDT", "4h", "2023-12", "2024-02", out_dir=tmp_path, base_url=BASE, client=client
        )
    assert again == paths
    assert fake.requests == [archive_url(BASE, "BTCUSDT", "4h", 2024, 1)]

    fake.requests.clear()
    with fake.client() as client:
        download_range(
            "BTCUSDT",
            "4h",
            "2023-12",
            "2023-12",
            out_dir=tmp_path,
            base_url=BASE,
            client=client,
            refresh=True,
        )
    assert archive_url(BASE, "BTCUSDT", "4h", 2023, 12) in fake.requests


def test_download_range_rejects_bad_months(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="'2024-1'"):
        download_range("BTCUSDT", "4h", "2024-1", "2024-02", out_dir=tmp_path, base_url=BASE)
    with pytest.raises(ValueError, match="before start"):
        download_range("BTCUSDT", "4h", "2024-03", "2024-02", out_dir=tmp_path, base_url=BASE)


def test_cli_prints_summary(
    tmp_path: Path, fixed_now: pd.Timestamp, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    fake = FakeArchive()
    fake.add_month("ETHUSDT", "1h", 2024, 1, to_csv(kline_rows("2024-01-01", 3, step_ms=3600000)))
    real_client = httpx.Client
    monkeypatch.setattr(
        archive.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(fake.handler)),
    )
    code = archive.main(
        ["ETHUSDT", "1h", "2024-01", "2024-01", "--out-dir", str(tmp_path), "--base-url", BASE]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "1 written (3 bars)" in out
    assert (tmp_path / "ETHUSDT" / "1h" / "2024-01.parquet").exists()


def test_parse_can_drop_byte_identical_rows_but_not_conflicting_ones() -> None:
    rows = kline_rows("2024-01-01", 3)
    df = parse_kline_csv(to_csv([*rows, rows[1]]), drop_identical_rows=True)
    assert len(df) == 3
    conflicting = [*rows, [rows[1][0], *rows[2][1:]]]
    with pytest.raises(ValueError, match="duplicate"):
        parse_kline_csv(to_csv(conflicting), drop_identical_rows=True)


def test_symbols_in_other_scripts_are_valid_but_paths_and_lowercase_are_not() -> None:
    assert archive_url(BASE, "币安人生USDT", "1d", 2025, 10).endswith(
        "/monthly/klines/币安人生USDT/1d/币安人生USDT-1d-2025-10.zip"
    )
    for bad in ("../USDT", "BTC_USDT", "BTC USDT", "btcUSDT", "ÉTHUSDT".lower()):
        with pytest.raises(ValueError, match="invalid symbol"):
            archive_url(BASE, bad, "1d", 2025, 10)
