"""POST /lab/derivatives: funding and open interest aligned to spot bar closes for the chart's
panes, from Parquet written here in the lab.data.derivatives layout (offline)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lab.api import MAX_DERIVATIVES_BARS, create_app
from lab.config import LabConfig, load_config
from lab.data.derivatives import FUNDING_COLUMNS, METRICS_COLUMNS
from lab.ledger import Ledger

H4 = 4 * 3600
SOURCE = "data.binance.vision futures/um archive (historical; no live stream)"


def unix(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").timestamp())


def write_funding(root: Path, symbol: str, rows: dict[str, float], hours: float = 8.0) -> None:
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz="UTC") for t in rows], name="time")
    frame = pd.DataFrame(
        {
            FUNDING_COLUMNS[0]: np.asarray(list(rows.values()), dtype=np.float64),
            FUNDING_COLUMNS[1]: np.full(len(rows), hours),
        },
        index=idx,
    )
    for month, part in frame.groupby(frame.index.strftime("%Y-%m")):
        out = root / "derivatives" / "fundingRate" / symbol / f"{month}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(out, engine="pyarrow")


def write_metrics(root: Path, symbol: str, idx: pd.DatetimeIndex, oi_value: np.ndarray) -> None:
    frame = pd.DataFrame(
        {c: np.full(len(idx), 1.0) for c in METRICS_COLUMNS}, index=idx.rename("time")
    )
    frame["sum_open_interest_value"] = np.asarray(oi_value, dtype=np.float64)
    for day, part in frame.groupby(frame.index.strftime("%Y-%m-%d")):
        out = root / "derivatives" / "metrics" / symbol / f"{day}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(out, engine="pyarrow")


def make_cfg(tmp_path: Path) -> LabConfig:
    base = load_config()
    return replace(
        base,
        data_dir=tmp_path / "data",
        derivatives=replace(base.derivatives, stale_after_intervals=2.0),
    )


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return make_cfg(tmp_path)


@pytest.fixture
def client(cfg: LabConfig, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(cfg, ledger_path=tmp_path / "ledger.db", warm_up=False)
    with TestClient(app, base_url="http://127.0.0.1:8788") as c:
        yield c


def post(client: TestClient, **body: Any) -> Any:
    return client.post("/lab/derivatives", json=body)


def error_paths(r: Any) -> list[str]:
    assert r.status_code == 422, r.text
    return [e["path"] for e in r.json()["errors"]]


@pytest.fixture
def btc_data(cfg: LabConfig) -> None:
    # Funding settles 00/08/16; Jan 2 08:00 and 16:00 and Jan 3 00:00 are missing (a gap).
    write_funding(
        cfg.data_dir,
        "BTCUSDT",
        {
            "2024-01-01 00:00": 0.0001,
            "2024-01-01 08:00": 0.0002,
            "2024-01-01 16:00": -0.0001,
            "2024-01-02 00:00": 0.0003,
            "2024-01-03 08:00": 0.0004,
        },
    )
    # OI every 5 minutes on Jan 1 (value = minutes since midnight), then nothing.
    idx = pd.date_range(pd.Timestamp("2024-01-01", tz="UTC"), periods=288, freq="5min")
    write_metrics(cfg.data_dir, "BTCUSDT", idx, np.arange(288) * 5.0)


# --------------------------------------------------------------------------- contract


def test_values_aligned_to_bar_closes(client: TestClient, btc_data: None) -> None:
    times = [unix("2023-12-31 20:00") + H4 * i for i in range(14)]  # 20:00 Dec 31 .. 00:00 Jan 3
    r = post(client, symbol="BTCUSDT", times=times, bar_seconds=H4)
    assert r.status_code == 200, r.text
    body = r.json()
    assert list(body) == [
        "symbol",
        "bar_seconds",
        "funding",
        "open_interest",
        "available",
        "source",
    ]
    assert body["symbol"] == "BTCUSDT" and body["bar_seconds"] == H4
    assert body["source"] == SOURCE
    assert [p["time"] for p in body["funding"]] == times
    assert [p["time"] for p in body["open_interest"]] == times
    # bar closes: 00,04,08,12,16,20 (Jan 1), 00,04,08,12,16,20 (Jan 2), 00,04 (Jan 3)
    assert [p["value"] for p in body["funding"]] == [
        0.0001,  # close 00:00 Jan 1: the settlement at the close is visible
        0.0001,
        0.0002,
        0.0002,
        -0.0001,
        -0.0001,
        0.0003,  # close 00:00 Jan 2
        0.0003,
        0.0003,
        0.0003,
        0.0003,  # close 16:00 Jan 2: 16h old = the 2 x 8h limit
        None,  # close 20:00 Jan 2: stale, a gap is not carried
        None,
        None,  # close 04:00 Jan 3 (08:00 settlement not yet visible)
    ]
    # OI value = minutes since midnight Jan 1 of the row's create_time. A row is visible 5 min
    # after create_time (window convention unverified), so each close sees the row stamped 5 min
    # before it: none at the 00:00 Jan 1 close, 235 (03:55) at 04:00, ..., 1435 (23:55, visible
    # 00:00 Jan 2) at the 00:00 Jan 2 close; later closes are stale (> 2 x 5 min).
    expected_oi = [None, 235.0, 475.0, 715.0, 955.0, 1195.0, 1435.0] + [None] * 7
    assert [p["value"] for p in body["open_interest"]] == expected_oi
    assert body["available"] == {
        "funding": {"first": unix("2024-01-01 00:00"), "last": unix("2024-01-03 08:00")},
        "open_interest": {"first": unix("2024-01-01 00:00"), "last": unix("2024-01-01 23:55")},
    }


def test_bars_before_the_archive_are_null(client: TestClient, btc_data: None) -> None:
    times = [unix("2023-12-01 00:00") + H4 * i for i in range(3)]
    body = post(client, symbol="BTCUSDT", times=times, bar_seconds=H4).json()
    assert [p["value"] for p in body["funding"]] == [None, None, None]
    assert [p["value"] for p in body["open_interest"]] == [None, None, None]
    assert body["available"]["funding"]["first"] == unix("2024-01-01 00:00")


def test_shifted_settlement_one_second_late_moves_to_next_bar(
    client: TestClient, cfg: LabConfig
) -> None:
    write_funding(cfg.data_dir, "ETHUSDT", {"2024-01-01 04:00:01": 0.0007})
    times = [unix("2024-01-01 00:00"), unix("2024-01-01 04:00")]
    body = post(client, symbol="ETHUSDT", times=times, bar_seconds=H4).json()
    assert [p["value"] for p in body["funding"]] == [None, 0.0007]
    assert [p["value"] for p in body["open_interest"]] == [None, None]
    assert body["available"]["open_interest"] == {"first": None, "last": None}


def test_one_minute_bars_see_each_oi_row_from_its_own_timestamp(
    client: TestClient, btc_data: None
) -> None:
    start = unix("2024-01-01 00:00")
    times = [start + 60 * i for i in range(11)]  # closes 00:01 .. 00:11
    body = post(client, symbol="BTCUSDT", times=times, bar_seconds=60).json()
    # rows at 00:00 (0) and 00:05 (5) are visible at 00:05 and 00:10: the 00:05 close (bar 4) is
    # the first to see the 00:00 row, the 00:10 close (bar 9) the first to see the 00:05 row
    assert [p["value"] for p in body["open_interest"]] == [None] * 4 + [0.0] * 5 + [5.0] * 2


def test_load_margin_covers_the_metrics_publication_lag(client: TestClient, btc_data: None) -> None:
    # The last OI row (23:55 Jan 1) is visible 00:00 Jan 2 and stale after 00:10. A 1-minute bar
    # opening 00:09 closes 00:10: it still sees that row, which sits 14 min before the bar's open,
    # so the load window must reach back 2 x 5 min + the 5-min lag (not just 2 x 5 min).
    start = unix("2024-01-02 00:09")
    body = post(client, symbol="BTCUSDT", times=[start], bar_seconds=60).json()
    assert body["open_interest"] == [{"time": start, "value": 1435.0}]
    times = [unix("2024-01-01 23:50") + 60 * i for i in range(22)]  # closes 23:51 .. 00:12
    full = post(client, symbol="BTCUSDT", times=times, bar_seconds=60).json()
    for k in range(len(times)):
        part = post(client, symbol="BTCUSDT", times=times[k:], bar_seconds=60).json()
        assert part["open_interest"] == full["open_interest"][k:]


def test_loading_a_window_matches_loading_everything(client: TestClient, btc_data: None) -> None:
    # The load window (first bar - margin .. last close) must not change any value.
    times = [unix("2023-12-31 20:00") + H4 * i for i in range(14)]
    full = post(client, symbol="BTCUSDT", times=times, bar_seconds=H4).json()
    for k in range(len(times)):
        part = post(client, symbol="BTCUSDT", times=times[k:], bar_seconds=H4).json()
        assert part["funding"] == full["funding"][k:]
        assert part["open_interest"] == full["open_interest"][k:]


def test_perturbing_rows_after_a_close_leaves_earlier_bars_unchanged(
    cfg: LabConfig, tmp_path: Path, btc_data: None
) -> None:
    times = [unix("2023-12-31 20:00") + H4 * i for i in range(14)]
    app = create_app(cfg, ledger_path=tmp_path / "ledger.db", warm_up=False)
    with TestClient(app, base_url="http://127.0.0.1:8788") as c:
        full = post(c, symbol="BTCUSDT", times=times, bar_seconds=H4).json()
    t = 3  # bar 3 closes 12:00 Jan 1
    rng = np.random.default_rng(3)
    write_funding(
        cfg.data_dir,
        "BTCUSDT",
        {
            "2024-01-01 00:00": 0.0001,
            "2024-01-01 08:00": 0.0002,
            "2024-01-01 12:00:00.001": float(rng.normal()),  # just after the close: invisible
            "2024-01-01 16:00": float(rng.normal()),
            "2024-01-02 00:00": float(rng.normal()),
        },
    )
    idx = pd.date_range(pd.Timestamp("2024-01-01", tz="UTC"), periods=288, freq="5min")
    oi = np.arange(288) * 5.0
    # rows stamped at or after the 12:00 close become visible from 12:05: all invisible at bar 3
    oi[idx >= pd.Timestamp("2024-01-01 12:00", tz="UTC")] = rng.uniform(1e6, 1e9, 144)
    write_metrics(cfg.data_dir, "BTCUSDT", idx, oi)
    with TestClient(app, base_url="http://127.0.0.1:8788") as c:
        pert = post(c, symbol="BTCUSDT", times=times, bar_seconds=H4).json()
    assert pert["funding"][: t + 1] == full["funding"][: t + 1]
    assert pert["open_interest"][: t + 1] == full["open_interest"][: t + 1]
    assert pert["funding"][t + 1 :] != full["funding"][t + 1 :]
    assert pert["open_interest"][t + 1 :] != full["open_interest"][t + 1 :]


def test_empty_times_with_data_on_disk(client: TestClient, btc_data: None) -> None:
    r = post(client, symbol="BTCUSDT", times=[], bar_seconds=H4)
    assert r.status_code == 200
    body = r.json()
    assert body["funding"] == [] and body["open_interest"] == []
    assert body["available"]["funding"]["first"] == unix("2024-01-01 00:00")


def test_only_one_series_on_disk_is_not_an_error(client: TestClient, cfg: LabConfig) -> None:
    idx = pd.date_range(pd.Timestamp("2024-01-01", tz="UTC"), periods=12, freq="5min")
    write_metrics(cfg.data_dir, "SOLUSDT", idx, np.arange(12) * 1.0)
    start = unix("2024-01-01 00:00")  # closes 01:00: sees the 00:55 row (visible 01:00)
    r = post(client, symbol="SOLUSDT", times=[start], bar_seconds=3600)
    assert r.status_code == 200
    assert r.json()["funding"] == [{"time": start, "value": None}]
    assert r.json()["open_interest"] == [{"time": start, "value": 11.0}]


def test_no_data_on_disk_is_503_naming_the_cli(client: TestClient, btc_data: None) -> None:
    r = post(client, symbol="XRPUSDT", times=[unix("2024-01-01")], bar_seconds=H4)
    assert r.status_code == 503
    assert r.json() == {
        "detail": (
            "no derivatives data for XRPUSDT; run `uv run python -m lab.data.derivatives "
            "download XRPUSDT --datasets fundingRate,metrics --start YYYY-MM --end YYYY-MM` first"
        )
    }


def test_all_null_value_files_count_as_no_data(client: TestClient, cfg: LabConfig) -> None:
    write_funding(cfg.data_dir, "ADAUSDT", {"2024-01-01 00:00": float("nan")})
    r = post(client, symbol="ADAUSDT", times=[unix("2024-01-01")], bar_seconds=H4)
    assert r.status_code == 503


def test_writes_nothing_to_the_ledger(client: TestClient, tmp_path: Path, btc_data: None) -> None:
    post(client, symbol="BTCUSDT", times=[unix("2024-01-01")], bar_seconds=H4)
    with Ledger(tmp_path / "ledger.db") as led:
        assert led.count(None) == 0


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize("times", [[3, 2, 4], [1, 1, 2]], ids=["descending", "duplicate"])
def test_times_must_be_strictly_ascending(client: TestClient, times: list[int]) -> None:
    assert error_paths(post(client, symbol="BTCUSDT", times=times, bar_seconds=H4)) == ["times"]


@pytest.mark.parametrize("bad", [2.5, "2", -1, 5_000_000_000])
def test_times_must_be_in_range_integers(client: TestClient, bad: Any) -> None:
    r = post(client, symbol="BTCUSDT", times=[1, bad], bar_seconds=H4)
    assert error_paths(r) == ["times.1"]


@pytest.mark.parametrize("symbol", ["btcusdt", "B", "BTC-USDT", "BTC/USDT", "A" * 31, 123])
def test_symbol_must_be_an_uppercase_binance_symbol(client: TestClient, symbol: Any) -> None:
    assert error_paths(post(client, symbol=symbol, times=[1], bar_seconds=H4)) == ["symbol"]


@pytest.mark.parametrize("bar_seconds", [0, 59, 604_801, 14400.5, "14400"])
def test_bar_seconds_bounds(client: TestClient, bar_seconds: Any) -> None:
    r = post(client, symbol="BTCUSDT", times=[1], bar_seconds=bar_seconds)
    assert error_paths(r) == ["bar_seconds"]


def test_bar_limit(client: TestClient, btc_data: None) -> None:
    n = MAX_DERIVATIVES_BARS + 1
    r = post(client, symbol="BTCUSDT", times=list(range(n)), bar_seconds=60)
    assert error_paths(r) == ["times"]
    times = [unix("2023-12-20") + 60 * i for i in range(MAX_DERIVATIVES_BARS)]
    ok = post(client, symbol="BTCUSDT", times=times, bar_seconds=60)
    assert ok.status_code == 200 and len(ok.json()["funding"]) == MAX_DERIVATIVES_BARS


def test_missing_fields(client: TestClient) -> None:
    assert set(error_paths(post(client))) == {"symbol", "times", "bar_seconds"}


def test_host_guard_applies(cfg: LabConfig, tmp_path: Path, btc_data: None) -> None:
    app = create_app(cfg, ledger_path=tmp_path / "ledger.db", warm_up=False)
    with TestClient(app, base_url="http://evil.example") as c:
        r = post(c, symbol="BTCUSDT", times=[unix("2024-01-01")], bar_seconds=H4)
    assert r.status_code == 403
