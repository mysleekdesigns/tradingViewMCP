"""Multi-coin universe: relisting reset, dollar-volume rank, perturbation test, delistings, and
the build/download pipeline. Offline (mocked archive, synthetic candles)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from lab.config import UniverseConfig, load_config
from lab.data import universe_build
from lab.data.archive import archive_url
from lab.data.universe import (
    eligibility_mask,
    liquidity_rank,
    listing_table,
    segment_starts,
    top_n_members,
    trailing_dollar_volume,
)
from lab.data.universe_build import (
    DownloadReport,
    build_universe,
    download_symbols,
    incomplete_symbols,
    load_universe,
    write_download_record,
    write_universe,
)
from tests.synthetic import make_candles, perturb_after
from tests.test_data_archive import checksum, kline_rows, to_csv, to_zip

T = pd.Timestamp
DAY_MS = 86_400_000


def day(s: str) -> pd.Timestamp:
    return T(s, tz="UTC")


def with_volume(frame: pd.DataFrame, volume: float) -> pd.DataFrame:
    out = frame.copy()
    out["volume"] = volume
    out["close"] = 1.0
    out["open"] = 1.0
    out["high"] = 1.0
    out["low"] = 1.0
    return out


def test_segment_starts_restart_after_a_long_hole() -> None:
    idx = [day("2024-01-01"), day("2024-01-02"), day("2024-03-01"), day("2024-03-02")]
    frame = make_candles(4, interval="1d", start="2024-01-01").set_axis(
        pd.DatetimeIndex(idx, name="time")
    )
    first, march = [day("2024-01-01")] * 4, [day("2024-01-01")] * 2 + [day("2024-03-01")] * 2
    assert list(segment_starts(frame, relist_gap_days=None)) == first
    assert list(segment_starts(frame, relist_gap_days=30)) == march
    assert list(segment_starts(frame, relist_gap_days=60)) == first  # 59-day hole is not > 60


def redenominated(factor: float, *, hole_days: int) -> pd.DataFrame:
    """20 daily bars; after bar 9, ``hole_days`` missing bars and prices times ``factor``."""
    frame = make_candles(20, seed=3, interval="1d", start="2024-01-01")
    frame.index = frame.index.where(
        np.arange(20) < 10, frame.index + pd.Timedelta(days=hole_days)
    ).rename("time")
    for col in ("open", "high", "low", "close"):
        frame.iloc[10:, frame.columns.get_loc(col)] *= factor
    return frame


@pytest.mark.parametrize(
    ("factor", "hole_days", "new_segment"),
    [
        (100.0, 3, True),  # BNX/DREP-style swap across a suspension
        (0.001, 3, True),  # QUICK/SUN-style
        (100.0, 0, False),  # same move with no hole: a real market move, kept
        (1.0, 3, False),  # a suspension with no price change: same market
        (4.0, 3, False),  # inside the 5x threshold
    ],
)
def test_hole_with_a_price_jump_starts_a_new_segment(
    factor: float, hole_days: int, new_segment: bool
) -> None:
    frame = redenominated(factor, hole_days=hole_days)
    starts = segment_starts(frame, relist_gap_days=30, relist_jump=5.0, interval="1d")
    assert (starts[10] == frame.index[10]) is new_segment
    assert starts[9] == frame.index[0]
    assert segment_starts(frame, relist_gap_days=30)[10] == frame.index[0]  # rule off


def test_redenominated_coin_must_season_again() -> None:
    frame = redenominated(100.0, hole_days=3)
    mask = eligibility_mask({"BNXUSDT": frame}, min_history_days=5, relist_jump=5.0, interval="1d")
    assert mask["BNXUSDT"].loc[frame.index[9]]
    assert not mask["BNXUSDT"].loc[frame.index[10]]
    assert mask["BNXUSDT"].loc[frame.index[15]]


def test_relisted_symbol_must_season_again() -> None:
    first = make_candles(20, seed=1, interval="1d", start="2024-01-01")
    second = make_candles(20, seed=2, interval="1d", start="2024-03-01")  # ticker reused
    luna = pd.concat([first, second])
    mask = eligibility_mask({"LUNAUSDT": luna}, min_history_days=10, relist_gap_days=30)
    assert mask.loc[day("2024-01-15"), "LUNAUSDT"]
    assert not mask.loc[day("2024-03-05"), "LUNAUSDT"]  # history before the hole does not count
    assert mask.loc[day("2024-03-11"), "LUNAUSDT"]
    without = eligibility_mask({"LUNAUSDT": luna}, min_history_days=10)
    assert without.loc[day("2024-03-05"), "LUNAUSDT"]


def test_trailing_dollar_volume_is_a_median_of_own_bars() -> None:
    a = make_candles(5, interval="1d", start="2024-01-01")
    a["close"] = [1.0, 2.0, 3.0, 4.0, 5.0]
    a["volume"] = [10.0, 10.0, 10.0, 0.0, 10.0]
    a["tradable"] = a["volume"] > 0
    for col in ("open", "high", "low"):
        a[col] = a["close"]
    b = make_candles(2, interval="1d", start="2024-01-04")
    dv = trailing_dollar_volume({"A": a, "B": b}, window_days=3)
    # window (t-3d, t]: day 3 -> {10,20,30} median 20; day 4 -> {20,30,0} median 20;
    # day 5 -> {30,0,50} median 30
    assert dv["A"].tolist() == [10.0, 15.0, 20.0, 20.0, 30.0]
    assert dv["B"].isna().tolist() == [True, True, True, False, False]


def test_liquidity_rank_only_ranks_eligible_symbols() -> None:
    idx = pd.DatetimeIndex([day("2024-01-01"), day("2024-01-02")], name="time")
    dv = pd.DataFrame({"A": [5.0, 5.0], "B": [9.0, 9.0], "C": [7.0, 5.0]}, index=idx)
    mask = pd.DataFrame({"A": [True, True], "B": [False, True], "C": [True, True]}, index=idx)
    rank = liquidity_rank(dv, mask)
    assert rank.loc[idx[0]].tolist()[0] == 2.0 and np.isnan(rank.loc[idx[0], "B"])
    assert rank.loc[idx[0], "C"] == 1.0
    assert rank.loc[idx[1]].tolist() == [2.0, 1.0, 3.0]  # A ties C at 5: earlier column wins
    assert top_n_members(rank, 1).loc[idx[1]].tolist() == [False, True, False]
    with pytest.raises(ValueError, match="same columns"):
        liquidity_rank(dv[["B", "A", "C"]], mask)


def three_coins() -> dict[str, pd.DataFrame]:
    return {
        "AAAUSDT": make_candles(80, seed=1, interval="1d", start="2024-01-01"),
        "BBBUSDT": make_candles(70, seed=2, interval="1d", start="2024-01-05"),
        "CCCUSDT": make_candles(50, seed=3, interval="1d", start="2024-01-01"),  # delisted
    }


def test_rank_at_t_unchanged_when_later_data_changes() -> None:
    data = three_coins()
    cut = day("2024-02-10")
    kwargs = {"min_history_days": 7, "relist_gap_days": 30, "relist_jump": 5.0, "interval": "1d"}
    base = liquidity_rank(
        trailing_dollar_volume(data, window_days=10), eligibility_mask(data, **kwargs)
    )

    altered = {}
    for s, f in data.items():
        pos = int((f.index <= cut).sum()) - 1
        g = perturb_after(f, pos)
        later = g.index > cut
        g.loc[later, "volume"] = g.loc[later, "volume"] * 50.0  # reorder later liquidity
        altered[s] = g
    bbb = altered["BBBUSDT"]
    hole = (bbb.index > cut + pd.Timedelta(days=1)) & (bbb.index < bbb.index[-1])
    altered["BBBUSDT"] = bbb[~hole]  # a later hole longer than relist_gap_days
    altered["NEWUSDT"] = make_candles(30, seed=9, interval="1d", start="2024-02-11")  # listed later
    altered["AAAUSDT"] = altered["AAAUSDT"].loc[: cut + pd.Timedelta(days=3)]  # later delisting
    pert = liquidity_rank(
        trailing_dollar_volume(altered, window_days=10), eligibility_mask(altered, **kwargs)
    )
    pd.testing.assert_frame_equal(
        base.loc[:cut], pert.loc[:cut, list(base.columns)], check_freq=False
    )
    assert pert.loc[:cut, "NEWUSDT"].isna().all()
    assert not base.loc[cut:].iloc[1:].equals(pert.loc[cut:, list(base.columns)].iloc[1:])


def test_listing_table_flags_delisted_and_relisted() -> None:
    data = three_coins()
    data["DDDUSDT"] = pd.concat(
        [
            make_candles(10, seed=4, interval="1d", start="2024-01-01"),
            make_candles(10, seed=5, interval="1d", start="2024-03-01"),
        ]
    )
    table = listing_table(data, relist_gap_days=30, delisted_after_days=7)
    assert table.loc["CCCUSDT", "delisted"]
    assert table.loc["CCCUSDT", "last_bar"] == day("2024-02-19")
    assert not table.loc["AAAUSDT", "delisted"]
    assert table.loc["DDDUSDT", "segments"] == 2 and table.loc["AAAUSDT", "segments"] == 1


def ucfg(**over: object) -> UniverseConfig:
    base = load_config().universe
    return dataclasses.replace(base, min_history_days=7, volume_window_days=10, top_n=2, **over)


def test_build_keeps_delisted_coins_until_their_last_bar() -> None:
    data = three_coins()
    data["CCCUSDT"] = with_volume(data["CCCUSDT"], 1e9)  # by far the most liquid while listed
    table = build_universe(data, ucfg())
    ccc = table[table["symbol"] == "CCCUSDT"]
    assert ccc["time"].min() == day("2024-01-08")  # 7 days of history
    assert ccc["time"].max() == day("2024-02-19")  # its last bar, then gone
    assert (ccc["rank"] == 1).all()
    after = table[table["time"] > day("2024-02-19")]
    assert set(after["symbol"]) == {"AAAUSDT", "BBBUSDT"}
    dense = table.groupby("time")["rank"].apply(lambda r: sorted(r) == list(range(1, len(r) + 1)))
    assert dense.all()  # ranks are 1..k at every bar


def test_write_and_load_universe_round_trip(tmp_path: Path) -> None:
    data = three_coins()
    cfg = ucfg()
    path, info = write_universe(data, cfg, universe_dir=tmp_path, missing=["ZZZUSDT"])
    assert path == tmp_path / "USDT-1d.parquet"
    assert info["parquet_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = json.loads(path.with_suffix(".json").read_text())
    assert manifest["delisted"] == ["CCCUSDT"]
    assert manifest["symbols_missing_data"] == ["ZZZUSDT"]
    assert manifest["settings"]["top_n"] == 2
    wide = load_universe(tmp_path, "USDT", "1d")
    expected = build_universe(data, cfg).pivot(index="time", columns="symbol", values="rank")
    assert wide.equals(expected.rename_axis(columns=None).astype(wide.dtypes.iloc[0]))
    with pytest.raises(ValueError, match="no universe"):
        load_universe(tmp_path, "USDT", "4h")


class FakeBucket:
    """S3 listing + archive files for a few 1d symbols."""

    def __init__(self, base: str, list_url: str) -> None:
        self.base, self.list_url = base, list_url
        self.keys: dict[str, list[str]] = {}
        self.files: dict[str, bytes] = {}

    def add(self, symbol: str, year: int, month: int, *, broken: bool = False) -> None:
        rows = kline_rows(f"{year}-{month:02d}-01", 28, step_ms=DAY_MS)
        url = archive_url(self.base, symbol, "1d", year, month)
        name = url.rsplit("/", 1)[-1]
        payload = to_zip(to_csv(rows), name.replace(".zip", ".csv"))
        self.files[url] = payload
        self.files[url + ".CHECKSUM"] = checksum(b"x" if broken else payload, name)
        folder = f"data/spot/monthly/klines/{symbol}/1d/"
        self.keys.setdefault(folder, []).append(folder + name)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(self.list_url):
            keys = self.keys.get(request.url.params["prefix"], [])
            body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
            xml = f"<ListBucketResult><IsTruncated>false</IsTruncated>{body}</ListBucketResult>"
            return httpx.Response(200, content=xml.encode())
        if url in self.files:
            return httpx.Response(200, content=self.files[url])
        return httpx.Response(404)


def test_download_symbols_fetches_listed_months_and_reports(tmp_path: Path) -> None:
    base, list_url = "https://data.example/data/spot", "https://s3.example/bucket"
    bucket = FakeBucket(base, list_url)
    bucket.add("AAAUSDT", 2023, 1)
    bucket.add("AAAUSDT", 2023, 3)  # February missing in the archive: never requested
    bucket.add("BADUSDT", 2023, 1, broken=True)
    client = httpx.Client(transport=httpx.MockTransport(bucket.handler))
    seen: list[tuple[int, int, str]] = []
    rep = download_symbols(
        ["AAAUSDT", "BADUSDT", "NONEUSDT"],
        interval="1d",
        list_url=list_url,
        base_url=base,
        out_dir=tmp_path,
        client=client,
        workers=2,
        progress=lambda d, n, s: seen.append((d, n, s)),
    )
    assert sorted(p.name for p in (tmp_path / "AAAUSDT" / "1d").iterdir()) == [
        "2023-01.parquet",
        "2023-03.parquet",
    ]
    assert rep.months_written == 2 and rep.bars_written == 56
    assert rep.no_data == ["NONEUSDT"]
    assert list(rep.failed) == ["BADUSDT"] and "SHA-256 mismatch" in rep.failed["BADUSDT"]
    assert len(seen) == 3 and seen[-1][:2] == (3, 3)
    again = download_symbols(
        ["AAAUSDT"], interval="1d", list_url=list_url, base_url=base, out_dir=tmp_path,
        client=client,
    )  # fmt: skip
    assert again.months_written == 0 and again.months_skipped == 2


def test_cli_build_needs_a_symbol_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = dataclasses.replace(load_config(), data_dir=tmp_path)
    assert universe_build.main(["build"], cfg=cfg) == 1
    assert "run `uv run python -m lab.data.universe_build symbols` first" in capsys.readouterr().out


def test_cli_symbols_then_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = dataclasses.replace(load_config(), data_dir=tmp_path)
    cfg = dataclasses.replace(cfg, universe=dataclasses.replace(cfg.universe, min_history_days=7))
    pre = "data/spot/monthly/klines/"
    listing = "".join(
        f"<CommonPrefixes><Prefix>{pre}{s}/</Prefix></CommonPrefixes>"
        for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "BTCUPUSDT", "BTCUSDT", "USDCUSDT", "AAABTC")
    )
    xml = f"<ListBucketResult><IsTruncated>false</IsTruncated>{listing}</ListBucketResult>"
    reply = httpx.Response(200, content=xml.encode())
    mock = httpx.Client(transport=httpx.MockTransport(lambda r: reply))
    monkeypatch.setattr(universe_build, "_client", lambda: mock)
    assert universe_build.main(["symbols"], cfg=cfg) == 0
    out = capsys.readouterr().out
    assert "4 USDT coins kept, 2 dropped" in out
    assert "dropped BTCUPUSDT: leveraged token on BTC" in out

    assert universe_build.main(["build"], cfg=cfg) == 1  # nothing downloaded yet
    assert "4 symbol(s) not fully downloaded" in capsys.readouterr().out

    report = DownloadReport(listed={"BTCUSDT": []})
    for symbol, frame in three_coins().items():
        folder = tmp_path / "klines" / symbol / "1d"
        folder.mkdir(parents=True)
        for (y, m), part in frame.groupby([frame.index.year, frame.index.month]):
            part.to_parquet(folder / f"{y:04d}-{m:02d}.parquet")
            report.listed.setdefault(symbol, []).append(f"{y:04d}-{m:02d}")
    record = tmp_path / "universe" / "download-USDT-1d.json"
    write_download_record(report, path=record, interval="1d")
    assert universe_build.main(["build"], cfg=cfg) == 0
    out = capsys.readouterr().out
    assert "3 symbols with data" in out
    manifest = json.loads((tmp_path / "universe" / "USDT-1d.json").read_text())
    assert manifest["symbols_missing_data"] == ["BTCUSDT"]
    assert manifest["symbols_incomplete_download"] == {}
    assert manifest["delisted"] == ["CCCUSDT"]

    (tmp_path / "klines" / "AAAUSDT" / "1d" / "2024-02.parquet").unlink()  # lost month
    assert universe_build.main(["build"], cfg=cfg) == 1
    assert "AAAUSDT: 1 archive month(s) not on disk, first 2024-02" in capsys.readouterr().out
    assert universe_build.main(["build", "--allow-incomplete"], cfg=cfg) == 0
    assert "built WITHOUT 1 incomplete symbol(s)" in capsys.readouterr().out
    manifest = json.loads((tmp_path / "universe" / "USDT-1d.json").read_text())
    assert list(manifest["symbols_incomplete_download"]) == ["AAAUSDT"]
    assert "AAAUSDT" not in manifest["ever_in_top_n"]


def test_download_record_merges_runs_and_clears_fixed_failures(tmp_path: Path) -> None:
    path = tmp_path / "rec.json"
    write_download_record(
        DownloadReport(listed={"AAAUSDT": ["2024-01"]}, failed={"BBBUSDT": "boom"}),
        path=path,
        interval="1d",
    )
    rec = json.loads(path.read_text())
    assert rec["symbols"]["BBBUSDT"] == {"listed": None, "error": "boom"}
    write_download_record(DownloadReport(listed={"BBBUSDT": ["2024-02"]}), path=path, interval="1d")
    rec = json.loads(path.read_text())
    assert rec["symbols"] == {
        "AAAUSDT": {"listed": ["2024-01"], "error": None},
        "BBBUSDT": {"listed": ["2024-02"], "error": None},
    }
    reasons = incomplete_symbols(
        ["AAAUSDT", "BBBUSDT", "CCCUSDT"], rec, interval="1d", parquet_dir=tmp_path
    )
    assert reasons["CCCUSDT"] == "not downloaded"
    assert reasons["AAAUSDT"] == "1 archive month(s) not on disk, first 2024-01"


def test_manifest_flags_price_jumps_between_bars(tmp_path: Path) -> None:
    data = three_coins()
    jumpy = data["BBBUSDT"].copy()
    later = jumpy.index >= day("2024-02-01")
    for col in ("open", "high", "low", "close"):
        jumpy.loc[later, col] = jumpy.loc[later, col] * 1000.0  # redenomination under one ticker
    data["BBBUSDT"] = jumpy
    _, info = write_universe(data, ucfg(), universe_dir=tmp_path, missing=[])
    assert info["price_jumps"] == {"BBBUSDT": ["2024-02-01"]}


def test_config_rejects_seasoning_shorter_than_volume_window(tmp_path: Path) -> None:
    text = (Path(__file__).parents[1] / "config" / "lab.toml").read_text()
    bad = text.replace("min_history_days = 365", "min_history_days = 10")
    assert bad != text
    path = tmp_path / "lab.toml"
    path.write_text(bad)
    with pytest.raises(ValueError, match=r"min_history_days \(10\) must be >="):
        load_config(path)


def test_hole_is_judged_by_the_bar_interval_not_by_later_bars() -> None:
    """Reviewer case: on 1d bars every 2-day step is a hole, so the 10x move on Jan 5 starts a
    new segment, and a later bar must not change that. The old whole-frame minimum step called
    2-day steps normal until the 1-day step on Jan 10 arrived, rewriting eligibility back to
    Jan 5 ([F, F, T, T, T] -> [F, F, F, F, T])."""
    times = [day(f"2024-01-{d:02d}") for d in (1, 3, 5, 7, 9)]
    frame = make_candles(5, interval="1d", start="2024-01-01").set_axis(
        pd.DatetimeIndex(times, name="time")
    )
    for col in ("open", "high", "low", "close"):
        frame[col] = [1.0, 1.0, 10.0, 10.0, 10.0]
    kwargs = {"min_history_days": 3, "relist_gap_days": 30, "relist_jump": 5.0, "interval": "1d"}
    base = eligibility_mask({"X": frame}, **kwargs)["X"]
    extra = make_candles(1, interval="1d", start="2024-01-10").iloc[:1]
    for col in ("open", "high", "low", "close"):
        extra[col] = 10.0
    later = eligibility_mask({"X": pd.concat([frame, extra])}, **kwargs)["X"]
    assert base.tolist() == [False, False, False, False, True]
    pd.testing.assert_series_equal(base, later.loc[: times[-1]], check_freq=False)


def test_relist_jump_needs_an_interval() -> None:
    with pytest.raises(ValueError, match="needs the bar interval"):
        eligibility_mask(three_coins(), relist_jump=5.0)


def test_load_universe_refuses_incomplete_or_mismatched_builds(tmp_path: Path) -> None:
    data = three_coins()
    path, _ = write_universe(
        data, ucfg(), universe_dir=tmp_path, missing=[], incomplete={"DDDUSDT": "not downloaded"}
    )
    with pytest.raises(ValueError, match="without 1 incomplete symbol"):
        load_universe(tmp_path, "USDT", "1d")
    assert len(load_universe(tmp_path, "USDT", "1d", allow_incomplete=True).columns) == 3
    write_universe(data, ucfg(), universe_dir=tmp_path, missing=[])
    path.write_bytes(path.read_bytes() + b"x")  # table no longer matches its manifest
    with pytest.raises(ValueError, match="no matching manifest"):
        load_universe(tmp_path, "USDT", "1d")
