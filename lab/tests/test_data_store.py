"""load_candles (Parquet + read-only SQLite top-up) and find_gaps. Offline, temp files only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lab.data import archive
from lab.data.store import find_gaps, load_candles
from lab.types import validate_candles
from tests.synthetic import make_candles

SERVER_SCHEMA = """
CREATE TABLE candles (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, time INTEGER NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  volume REAL NOT NULL, PRIMARY KEY (symbol, interval, time)
) WITHOUT ROWID;
"""


def write_months(root: Path, symbol: str, interval: str, candles: pd.DataFrame) -> None:
    folder = root / symbol / interval
    folder.mkdir(parents=True, exist_ok=True)
    for period, chunk in candles.groupby(candles.index.strftime("%Y-%m")):
        chunk.to_parquet(folder / f"{period}.parquet")


def make_db(path: Path, symbol: str, interval: str, candles: pd.DataFrame) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SERVER_SCHEMA)
    rows = [
        (symbol, interval, int(t.timestamp()), r.open, r.high, r.low, r.close, r.volume)
        for t, r in candles.iterrows()
    ]
    conn.executemany("INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def jan_feb(tmp_path: Path) -> pd.DataFrame:
    c = make_candles(60 * 6, seed=3, interval="4h", start="2024-01-01")  # Jan 1 .. Feb 29 20:00
    write_months(tmp_path, "BTCUSDT", "4h", c)
    return c


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> pd.Timestamp:
    now = pd.Timestamp("2024-03-05 10:00", tz="UTC")
    monkeypatch.setattr("lab.data.store._utc_now", lambda: now)
    monkeypatch.setattr(archive, "_utc_now", lambda: now)
    return now


def test_load_concatenates_months(tmp_path: Path, jan_feb: pd.DataFrame) -> None:
    out = load_candles("BTCUSDT", "4h", parquet_dir=tmp_path)
    validate_candles(out)
    pd.testing.assert_frame_equal(out, jan_feb, check_freq=False, check_index_type=False)
    assert len(list((tmp_path / "BTCUSDT" / "4h").glob("*.parquet"))) == 2


def test_load_filters_half_open_window(tmp_path: Path, jan_feb: pd.DataFrame) -> None:
    start = pd.Timestamp("2024-01-10", tz="UTC")
    end = pd.Timestamp("2024-02-01", tz="UTC")
    out = load_candles("BTCUSDT", "4h", parquet_dir=tmp_path, start=start, end=end)
    assert out.index[0] == start
    assert out.index[-1] == pd.Timestamp("2024-01-31 20:00", tz="UTC")
    assert end not in out.index
    naive = load_candles(
        "BTCUSDT", "4h", parquet_dir=tmp_path, start=pd.Timestamp("2024-01-10"), end=end
    )
    pd.testing.assert_frame_equal(out, naive)


def test_load_missing_data_names_symbol_and_cli(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"ETHUSDT 1h.*python -m lab\.data\.archive ETHUSDT 1h"):
        load_candles("ETHUSDT", "1h", parquet_dir=tmp_path)
    (tmp_path / "ETHUSDT" / "1h").mkdir(parents=True)
    with pytest.raises(ValueError, match="ETHUSDT 1h"):
        load_candles("ETHUSDT", "1h", parquet_dir=tmp_path)


def test_load_rejects_inverted_window(tmp_path: Path, jan_feb: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="must be after start"):
        load_candles(
            "BTCUSDT",
            "4h",
            parquet_dir=tmp_path,
            start=pd.Timestamp("2024-02-01", tz="UTC"),
            end=pd.Timestamp("2024-01-01", tz="UTC"),
        )


def test_sqlite_appends_only_newer_closed_bars_and_parquet_wins(
    tmp_path: Path, jan_feb: pd.DataFrame, fixed_now: pd.Timestamp
) -> None:
    db = tmp_path / "app.db"
    # Server rows: an overlapping Feb bar with different prices, then March bars up to "now".
    overlap = jan_feb.iloc[[-1]].copy()
    overlap[["open", "high", "low", "close"]] = 1.0
    march = make_candles(30, seed=8, interval="4h", start="2024-03-01")
    march.iloc[1, march.columns.get_loc("volume")] = 0.0
    make_db(db, "BTCUSDT", "4h", pd.concat([overlap, march]))
    make_db_other = sqlite3.connect(db)
    make_db_other.execute(
        "INSERT INTO candles VALUES ('ETHUSDT', '4h', ?, 1, 1, 1, 1, 1)",
        (int(pd.Timestamp("2024-03-02", tz="UTC").timestamp()),),
    )
    make_db_other.commit()
    make_db_other.close()

    out = load_candles("BTCUSDT", "4h", parquet_dir=tmp_path, sqlite_path=db)
    validate_candles(out)
    # Parquet wins the overlap.
    assert out.loc[jan_feb.index[-1], "close"] == jan_feb["close"].iloc[-1]
    # Closed March bars appended: last closed 4h bar at 10:00 is the 04:00 bar (closes 08:00).
    new = out[out.index >= pd.Timestamp("2024-03-01", tz="UTC")]
    assert new.index[-1] == pd.Timestamp("2024-03-05 04:00", tz="UTC")
    assert len(new) == 4 * 6 + 2
    assert not new["tradable"].iloc[1]
    assert new["tradable"].iloc[0]
    np.testing.assert_array_equal(new["close"].to_numpy(), march["close"].to_numpy()[: len(new)])


def test_sqlite_is_opened_read_only(
    tmp_path: Path, jan_feb: pd.DataFrame, fixed_now: pd.Timestamp
) -> None:
    db = tmp_path / "app.db"
    make_db(db, "BTCUSDT", "4h", make_candles(3, seed=1, start="2024-03-01"))
    db.chmod(0o444)
    try:
        before = db.read_bytes()
        out = load_candles("BTCUSDT", "4h", parquet_dir=tmp_path, sqlite_path=db)
        assert len(out) == len(jan_feb) + 3
        assert db.read_bytes() == before
    finally:
        db.chmod(0o644)


def test_sqlite_missing_file_raises(tmp_path: Path, jan_feb: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match=r"nope\.db"):
        load_candles("BTCUSDT", "4h", parquet_dir=tmp_path, sqlite_path=tmp_path / "nope.db")


def test_load_keeps_gaps_absent(tmp_path: Path) -> None:
    c = make_candles(40, seed=5, start="2024-01-01")
    holed = c.drop(c.index[10:13])
    write_months(tmp_path, "BTCUSDT", "4h", holed)
    out = load_candles("BTCUSDT", "4h", parquet_dir=tmp_path)
    assert len(out) == 37
    assert not out.isna().to_numpy().any()
    assert c.index[11] not in out.index


def test_find_gaps_reports_runs_of_missing_bars() -> None:
    c = make_candles(50, seed=2, start="2024-01-01")
    holed = c.drop(c.index[[5, 6, 7, 20]])
    gaps = find_gaps(holed, "4h")
    assert list(gaps.columns) == ["gap_start", "gap_end", "missing_bars"]
    assert gaps["gap_start"].tolist() == [c.index[4], c.index[19]]
    assert gaps["gap_end"].tolist() == [c.index[8], c.index[21]]
    assert gaps["missing_bars"].tolist() == [3, 1]


def test_find_gaps_ignores_zero_volume_bars_and_handles_no_gaps() -> None:
    c = make_candles(20, seed=2)
    c.iloc[4, c.columns.get_loc("volume")] = 0.0
    c.iloc[4, c.columns.get_loc("tradable")] = False
    gaps = find_gaps(c, "4h")
    assert gaps.empty
    assert list(gaps.columns) == ["gap_start", "gap_end", "missing_bars"]
    assert find_gaps(c.iloc[:1], "4h").empty


def test_find_gaps_rejects_unknown_interval() -> None:
    with pytest.raises(ValueError, match="'5h'"):
        find_gaps(make_candles(5), "5h")
