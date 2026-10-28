"""Forward-vs-backtest comparison: read-only dry-run DB reading, hand-checkable stats, entry
matching and slippage, every verdict branch, lab replay without look-ahead, atomic writes, and
the public kline loader (MockTransport, offline)."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest

import research.paper.compare as cmp_mod
from lab.config import LabConfig, load_config
from lab.engine import run_signals
from lab.plan import compile_plan, parse_plan
from lab.types import CostModel, Trade
from research.paper.compare import (
    EXIT_KIND_BY_REASON,
    LAB_EXIT_KIND,
    MIN_FORWARD_TRADES,
    ForwardTrade,
    backtest_same_period,
    backtest_stats,
    compare,
    comparison_path,
    forward_stats,
    match_entries,
    public_kline_loader,
    read_dryrun_trades,
    render_verdict_markdown,
    replay_trades,
    write_comparison,
    write_missing_comparison,
)
from research.paper.protocol import Check, Verdict, WeekShortfall

T0 = pd.Timestamp("2026-09-01", tz="UTC")
HOUR = pd.Timedelta(hours=1)
FULL_COLUMNS = (
    "id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN, open_date DATETIME, "
    "close_date DATETIME, open_rate FLOAT, close_rate FLOAT, amount FLOAT, fee_open FLOAT, "
    "fee_close FLOAT, close_profit FLOAT, exit_reason TEXT"
)


def make_db(path: Path, rows: list[dict[str, Any]], columns: str = FULL_COLUMNS) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE trades ({columns})")
    names = [c.split()[0] for c in columns.split(", ") if not c.startswith("id ")]
    for r in rows:
        keys = [k for k in names if k in r]
        conn.execute(
            f"INSERT INTO trades ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
            [r[k] for k in keys],
        )
    conn.commit()
    conn.close()
    return path


def ft_date(ts: pd.Timestamp) -> str:
    """freqtrade stores naive UTC datetimes like '2026-09-01 05:00:03.000000'."""
    return ts.tz_convert("UTC").tz_localize(None).strftime("%Y-%m-%d %H:%M:%S.%f")


def flat_candles(days: int = 30, *, open_price: float = 100.0) -> pd.DataFrame:
    n = days * 24
    index = pd.date_range(T0, periods=n, freq="1h", name="time")
    return pd.DataFrame(
        {
            "open": np.full(n, open_price),
            "high": np.full(n, open_price * 1.01),
            "low": np.full(n, open_price * 0.99),
            "close": np.full(n, open_price),
            "volume": np.full(n, 10.0),
            "tradable": np.ones(n, dtype=bool),
        },
        index=index,
    )


def bt_trade(entry: pd.Timestamp, ret: float, *, closed: bool = True) -> Trade:
    return Trade(
        direction="long",
        entry_time=entry,
        entry_price=100.0,
        exit_time=entry + 2 * HOUR if closed else None,
        exit_price=100.0 * (1 + ret) if closed else None,
        size=1.0,
        pnl=100.0 * ret,
        return_pct=ret,
        fees=0.0,
        exit_reason="signal" if closed else "end",
        bars_held=2,
    )


def fw_trade(open_time: pd.Timestamp, rate: float, profit: float | None) -> ForwardTrade:
    return ForwardTrade(
        pair="BTC/USDT",
        is_open=profit is None,
        open_time=open_time,
        open_rate=rate,
        close_time=None if profit is None else open_time + 2 * HOUR,
        close_rate=None if profit is None else rate * (1 + profit),
        close_profit=profit,
    )


# --------------------------------------------------------------------------- reading


def test_read_trades_and_hand_checked_stats(tmp_path: Path) -> None:
    rows = [
        {"pair": "BTC/USDT", "is_open": 0, "open_date": ft_date(T0 + 5 * HOUR),
         "close_date": ft_date(T0 + 9 * HOUR), "open_rate": 100.0, "close_rate": 110.0,
         "amount": 1.0, "fee_open": 0.001, "fee_close": 0.001, "close_profit": 0.10,
         "exit_reason": "exit_signal"},
        {"pair": "BTC/USDT", "is_open": 0, "open_date": ft_date(T0 + 20 * HOUR),
         "close_date": ft_date(T0 + 22 * HOUR), "open_rate": 110.0, "close_rate": 104.5,
         "close_profit": -0.05, "exit_reason": "stop_loss"},
        {"pair": "BTC/USDT", "is_open": 0, "open_date": ft_date(T0 + 30 * HOUR),
         "close_date": ft_date(T0 + 31 * HOUR), "open_rate": 104.5, "close_rate": 106.59,
         "close_profit": 0.02, "exit_reason": "roi"},
        {"pair": "BTC/USDT", "is_open": 1, "open_date": ft_date(T0 + 40 * HOUR),
         "open_rate": 106.0},
        {"pair": "ETH/USDT", "is_open": 0, "open_date": ft_date(T0 + 6 * HOUR),
         "close_date": ft_date(T0 + 7 * HOUR), "open_rate": 3000.0, "close_rate": 3300.0,
         "close_profit": 0.5},
    ]  # fmt: skip
    db = make_db(tmp_path / "dry.sqlite", rows)
    trades = read_dryrun_trades(db, pair="BTC/USDT")
    assert len(trades) == 4
    first = trades[0]
    assert first.open_time == T0 + 5 * HOUR and first.open_time.tzinfo is not None
    assert first.exit_reason == "exit_signal" and first.fee_open == 0.001
    assert trades[-1].is_open and not trades[-1].closed

    stats = forward_stats(trades)  # closed trades only: +10%, -5%, +2%
    assert stats["n_trades"] == 3
    assert stats["total_return"] == pytest.approx(1.10 * 0.95 * 1.02 - 1)  # 0.0659
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["mean_trade_return"] == pytest.approx((0.10 - 0.05 + 0.02) / 3)
    assert len(read_dryrun_trades(db)) == 5  # no pair filter


def test_missing_optional_columns_are_tolerated(tmp_path: Path) -> None:
    columns = (
        "id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN, open_date DATETIME, "
        "close_date DATETIME, open_rate FLOAT, close_rate FLOAT, fee_open FLOAT, fee_close FLOAT"
    )
    rows = [
        {"pair": "BTC/USDT", "is_open": 0, "open_date": ft_date(T0), "close_date":
         ft_date(T0 + HOUR), "open_rate": 100.0, "close_rate": 110.0, "fee_open": 0.001,
         "fee_close": 0.001},
    ]  # fmt: skip
    (trade,) = read_dryrun_trades(make_db(tmp_path / "db.sqlite", rows, columns))
    assert trade.close_profit is None and trade.exit_reason is None and trade.amount is None
    # 110 x (1 - 0.001) / (100 x (1 + 0.001)) - 1
    assert trade.net_return() == pytest.approx(110 * 0.999 / (100 * 1.001) - 1)


def test_database_is_opened_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = make_db(tmp_path / "db.sqlite", [])
    before = db.read_bytes()
    seen: list[tuple[str, dict[str, Any]]] = []
    real_connect = sqlite3.connect

    def spy(target: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        seen.append((target, kwargs))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(cmp_mod.sqlite3, "connect", spy)
    assert read_dryrun_trades(db) == []
    ((target, kwargs),) = seen
    assert target.startswith("file:") and target.endswith("?mode=ro") and kwargs["uri"] is True
    assert db.read_bytes() == before


def test_read_only_connection_cannot_write(tmp_path: Path) -> None:
    db = make_db(tmp_path / "db.sqlite", [])
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM trades")
    conn.close()


def test_missing_file_table_or_column_is_named(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        read_dryrun_trades(tmp_path / "nope.sqlite")
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    with pytest.raises(ValueError, match="no `trades` table"):
        read_dryrun_trades(empty)
    thin = make_db(tmp_path / "thin.sqlite", [], "id INTEGER PRIMARY KEY, pair TEXT")
    with pytest.raises(ValueError, match="missing columns"):
        read_dryrun_trades(thin)


# --------------------------------------------------------------------------- matching


def test_entry_match_rate_and_slippage_by_hand() -> None:
    candles = flat_candles()  # every bar opens at 100.0
    backtest = [
        bt_trade(T0 + 5 * HOUR, 0.01),
        bt_trade(T0 + 20 * HOUR, 0.01),
        bt_trade(T0 + 40 * HOUR, 0.01),
        bt_trade(T0 + 60 * HOUR, 0.01),
    ]
    forward = [
        fw_trade(T0 + 5 * HOUR + pd.Timedelta(seconds=3), 100.5, 0.01),  # +50 bps, matched
        fw_trade(T0 + 20 * HOUR + pd.Timedelta(minutes=30), 99.9, 0.01),  # -10 bps, matched
        fw_trade(T0 + 61 * HOUR + pd.Timedelta(seconds=1), 100.0, 0.01),  # > 1 bar late
    ]
    pairs = match_entries(forward, backtest, interval="1h")
    assert [(b.entry_time, f.open_rate) for b, f in pairs] == [
        (T0 + 5 * HOUR, 100.5),
        (T0 + 20 * HOUR, 99.9),
    ]
    result = compare(forward, backtest, interval="1h", candles=candles)
    assert result["entry_match_rate"] == pytest.approx(2 / 4)
    assert result["mean_entry_slippage_bps"] == pytest.approx((50.0 - 10.0) / 2)


def test_each_forward_entry_matches_at_most_once() -> None:
    backtest = [bt_trade(T0 + 5 * HOUR, 0.0), bt_trade(T0 + 6 * HOUR, 0.0)]
    forward = [fw_trade(T0 + 5 * HOUR + pd.Timedelta(minutes=40), 100.0, 0.0)]
    assert len(match_entries(forward, backtest, interval="1h")) == 1


def test_backtest_stats_skip_trades_open_at_period_end() -> None:
    stats = backtest_stats([bt_trade(T0, 0.02), bt_trade(T0 + HOUR, -0.01, closed=False)])
    assert stats["n_trades"] == 1 and stats["total_return"] == pytest.approx(0.02)


# --------------------------------------------------------------------------- verdicts


FILL_DELAY = pd.Timedelta(seconds=60)  # freqtrade fills shortly after the signal bar closes


def _daily(
    n: int, ret: float, *, shift: pd.Timedelta = FILL_DELAY
) -> tuple[list[ForwardTrade], list[Trade]]:
    bt = [bt_trade(T0 + pd.Timedelta(days=d + 1), 0.01) for d in range(n)]
    fw = [fw_trade(T0 + pd.Timedelta(days=d + 1) + shift, 100.0, ret) for d in range(n)]
    return fw, bt


def test_verdict_insufficient_with_few_trades() -> None:
    fw, bt = _daily(MIN_FORWARD_TRADES - 1, 0.01)
    result = compare(fw, bt, interval="1h", candles=flat_candles(30))
    assert result["verdict"] == "insufficient_data"


def test_verdict_insufficient_with_a_short_period() -> None:
    fw, bt = _daily(6, 0.01)
    result = compare(fw, bt, interval="1h", candles=flat_candles(10))
    assert result["verdict"] == "insufficient_data"
    assert any("10.0 days" in n for n in result["notes"])


def test_verdict_consistent() -> None:
    fw, bt = _daily(6, 0.009)
    result = compare(fw, bt, interval="1h", candles=flat_candles(30))
    assert result["entry_match_rate"] == 1.0
    assert result["verdict"] == "consistent"


def test_verdict_diverging_on_match_rate() -> None:
    fw, bt = _daily(6, 0.01, shift=pd.Timedelta(hours=3))
    result = compare(fw, bt, interval="1h", candles=flat_candles(30))
    assert result["entry_match_rate"] == 0.0
    assert result["verdict"] == "diverging"
    assert any("match rate" in n for n in result["notes"])


def test_verdict_diverging_on_return_gap() -> None:
    fw, bt = _daily(6, -0.02)  # forward 0.98^6 - 1 = -11.4% vs backtest 1.01^6 - 1 = +6.2%
    result = compare(fw, bt, interval="1h", candles=flat_candles(30))
    assert result["forward"]["total_return"] == pytest.approx(0.98**6 - 1)
    assert result["backtest"]["total_return"] == pytest.approx(1.01**6 - 1)
    assert result["entry_match_rate"] == 1.0
    assert result["verdict"] == "diverging"
    assert any("total return" in n for n in result["notes"])


def test_verdict_diverging_when_backtest_has_no_entries() -> None:
    fw, _ = _daily(6, 0.01)
    result = compare(fw, [], interval="1h", candles=flat_candles(30))
    assert result["entry_match_rate"] is None
    assert result["verdict"] == "diverging"


def test_forward_trades_outside_the_period_are_ignored() -> None:
    fw, bt = _daily(6, 0.009)
    early = fw_trade(T0 - pd.Timedelta(days=3), 100.0, 0.5)
    result = compare([early, *fw], bt, interval="1h", candles=flat_candles(30))
    assert result["forward"]["n_trades"] == 6
    assert result["period_start"] == int(T0.timestamp())
    assert result["period_end"] == int((T0 + pd.Timedelta(days=30)).timestamp())


# --------------------------------------------------------------------------- lab replay


def synthetic_candles(n: int, *, seed: int, start: pd.Timestamp) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, periods=n, freq="1h", name="time")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    open_ = np.r_[100.0, close[:-1]]
    spread = np.abs(rng.normal(0.0, 0.004, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.uniform(5.0, 50.0, n),
            "tradable": np.ones(n, dtype=bool),
        },
        index=index,
    )


PLAN: dict[str, Any] = {
    "name": "replay",
    "symbol": "BTCUSDT",
    "interval": "1h",
    "features": {"fast": {"ema": {"period": 8}}, "slow": {"ema": {"period": 30}}},
    "long_entry": "crosses_above(fast, slow)",
    "long_exit": "crosses_below(fast, slow)",
    "risk": {"position_size_pct": 100, "stop_loss_pct": 4},
}


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return replace(load_config(), data_dir=tmp_path)


def test_backtest_same_period_uses_warmup_and_keeps_trades_in_window(cfg: LabConfig) -> None:
    candles = synthetic_candles(2000, seed=5, start=T0 - pd.Timedelta(days=40))
    start, end = T0, T0 + pd.Timedelta(days=30)

    def loader(symbol: str, interval: str, until: pd.Timestamp) -> pd.DataFrame:
        return candles  # deliberately returns bars after `until` too

    trades = backtest_same_period(PLAN, start=start, end=end, cfg=cfg, loader=loader)
    assert trades and all(start <= t.entry_time < end for t in trades)

    history = candles.loc[candles.index < end]
    sig = compile_plan(parse_plan(PLAN), history)
    mask = history.index >= start
    expected = run_signals(
        history.loc[mask],
        sig.entries.loc[mask],
        sig.exits.loc[mask],
        costs=cfg.costs,
        risk=parse_plan(PLAN).risk,
        init_cash=cfg.init_cash,
        cost_history=history,
    ).trades
    assert trades == list(expected)

    # no look-ahead: bars at/after `end` cannot change the replay
    later = candles.copy()
    after = later.index >= end
    later.loc[after, ["open", "high", "low", "close"]] *= 1.5
    again = backtest_same_period(PLAN, start=start, end=end, cfg=cfg, loader=lambda *a: later)
    assert again == trades


def test_backtest_same_period_edge_cases(cfg: LabConfig) -> None:
    candles = synthetic_candles(100, seed=1, start=T0)
    with pytest.raises(ValueError, match="after start"):
        backtest_same_period(PLAN, start=T0, end=T0, cfg=cfg, loader=lambda *a: candles)
    late = T0 + pd.Timedelta(days=30)
    out = backtest_same_period(
        PLAN, start=late, end=late + HOUR, cfg=cfg, loader=lambda *a: candles
    )
    assert out == []


def test_replay_trades_matches_backtest_same_period_and_is_causal(cfg: LabConfig) -> None:
    """The protocol's timing match replays the plan flat at ``start`` on the same candles; the
    replay is what backtest_same_period returns, and bars at/after ``end`` change nothing."""
    candles = synthetic_candles(2000, seed=5, start=T0 - pd.Timedelta(days=40))
    start, end = T0, T0 + pd.Timedelta(days=30)
    direct = replay_trades(PLAN, candles, start=start, end=end, costs=cfg.costs)
    via_loader = backtest_same_period(
        PLAN, start=start, end=end, cfg=cfg, loader=lambda *a: candles
    )
    assert direct == via_loader and direct
    later = candles.copy()
    later.loc[later.index >= end, ["open", "high", "low", "close"]] *= 0.5
    assert replay_trades(PLAN, later, start=start, end=end, costs=cfg.costs) == direct
    # the replay starts flat: nothing is entered before start
    assert all(t.entry_time >= start for t in direct)


def test_backtest_same_period_uses_the_frozen_cost_model(cfg: LabConfig) -> None:
    candles = synthetic_candles(2000, seed=5, start=T0 - pd.Timedelta(days=40))
    start, end = T0, T0 + pd.Timedelta(days=30)
    frozen = CostModel(fee_rate=0.0, slippage_rate=0.002, slippage_vol_mult=0.0, slippage_cap=0.01)
    trades = backtest_same_period(
        PLAN, start=start, end=end, cfg=cfg, loader=lambda *a: candles, costs=frozen
    )
    first = trades[0]
    assert first.entry_price == pytest.approx(float(candles.loc[first.entry_time, "open"]) * 1.002)


def test_render_verdict_markdown_shows_coverage_alerts_and_week_flags() -> None:
    verdict = Verdict(
        verdict="continue",
        mode="implementation_only",
        headline="continue: incomplete data: 10 of 20 bars, 1 fills unjudged",
        reasons=("incomplete data: 10 of 20 bars, 1 fills unjudged",),
        fail_kinds=(),
        alerts=("week 0 median shortfall +12.0 bps > 5.0 bps over 3 fills (alert only)",),
        checks=(Check("entries", "pending", 2, 1, 5, "2 entries so far"),),
        weeks=(WeekShortfall(0, 0, 3, 12.0, True, True, False),),
        elapsed_days=3.3,
        at_decision=True,
        data_complete=False,
        bars_expected=20,
        bars_present=10,
        fills_unjudged=1,
        protocol_sha256="ab" * 32,
    )
    md = "\n".join(render_verdict_markdown(verdict))
    failed = "\n".join(render_verdict_markdown(replace(verdict, fail_kinds=("regime",))))
    assert "Fail kind: regime." in failed
    assert "## Protocol verdict: continue" in md
    assert "data 10 of 20 bars, 1 fills unjudged (incomplete)" in md
    assert "- Alert: week 0 median shortfall +12.0 bps" in md
    assert "| 0 | 3 | +12.0 | yes | yes | no |" in md


def test_exit_kinds_pair_freqtrade_reasons_with_lab_exits() -> None:
    """C-A: a paper exit only matches a lab exit of the same kind; unknown reasons never do."""
    assert {EXIT_KIND_BY_REASON[r] for r in ("exit_signal", "sell_signal")} == {"signal"}
    assert EXIT_KIND_BY_REASON["stop_loss"] == LAB_EXIT_KIND["stop_loss"] == "stop"
    assert EXIT_KIND_BY_REASON["roi"] == LAB_EXIT_KIND["take_profit"] == "target"
    assert "force_exit" not in EXIT_KIND_BY_REASON


def test_comparison_path_is_known_before_writing(tmp_path: Path) -> None:
    fw, bt = _daily(6, 0.009)
    result = compare(
        fw,
        bt,
        interval="1h",
        candles=flat_candles(30),
        candidate="c1",
        now=datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
    )
    path = comparison_path(result, tmp_path)
    assert path == tmp_path / "c1" / "comparison-2026-10-24.json" and not path.exists()
    assert write_comparison(result, tmp_path) == path


# --------------------------------------------------------------------------- writing


def test_write_comparison_json_and_markdown(tmp_path: Path) -> None:
    fw, bt = _daily(6, 0.009)
    result = compare(
        fw,
        bt,
        interval="1h",
        candles=flat_candles(30),
        candidate="ema-rsi_1",
        plan_hash="ab" * 32,
        symbol="BTCUSDT",
        now=datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
    )
    path = write_comparison(result, tmp_path)
    assert path == tmp_path / "ema-rsi_1" / "comparison-2026-10-24.json"
    assert json.loads(path.read_text()) == json.loads(json.dumps(result))
    md = path.with_suffix(".md").read_text()
    assert "**Verdict: consistent**" in md
    assert "not forward-looking" in md
    assert not list(path.parent.glob("*.tmp"))
    with pytest.raises(ValueError, match="candidate"):
        write_comparison({**result, "candidate": "../x"}, tmp_path)  # type: ignore[typeddict-item]


# --------------------------------------------------------------------------- public candles


def test_public_kline_loader_pages_parses_and_drops_forming_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cmp_mod, "KLINE_PAGE_LIMIT", 2)
    ms = int(T0.timestamp() * 1000)
    hour_ms = 3_600_000

    def kline(i: int) -> list[Any]:
        o = ms + i * hour_ms
        return [o, f"{100 + i}.5", f"{101 + i}", f"{99 + i}", f"{100 + i}", "12.5", o + hour_ms - 1]

    all_rows = [kline(i) for i in range(5)]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "data-api.binance.vision"
        assert request.url.path == "/api/v3/klines"
        start = int(request.url.params["startTime"])
        limit = int(request.url.params["limit"])
        page = [r for r in all_rows if r[0] >= start][:limit]
        return httpx.Response(200, json=page)

    now = (T0 + 4 * HOUR + pd.Timedelta(minutes=10)).to_pydatetime()  # bar 4 still forming
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        loader = public_kline_loader(since=T0, client=client, now=now)
        frame = loader("BTCUSDT", "1h", T0 + 10 * HOUR)
    assert len(requests) == 3  # pages of 2, 2, 1
    assert list(frame.index) == [T0 + i * HOUR for i in range(4)]
    assert str(frame.index.tz) == "UTC"
    assert frame["open"].iloc[1] == 101.5 and frame["close"].iloc[3] == 103.0
    assert frame["tradable"].all() and frame["volume"].dtype == np.float64
    assert not any(math.isnan(v) for v in frame["high"])


def _kline_rows(n: int) -> list[list[Any]]:
    ms, hour_ms = int(T0.timestamp() * 1000), 3_600_000
    return [
        [ms + i * hour_ms, "100", "101", "99", "100", "1", ms + (i + 1) * hour_ms - 1]
        for i in range(n)
    ]


def _scripted(responses: list[httpx.Response]) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(queue)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_public_kline_loader_retries_429_honouring_retry_after() -> None:
    client, seen = _scripted(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429),  # no Retry-After: the doubling default (2 s x 2)
            httpx.Response(200, json=_kline_rows(3)),
        ]
    )
    waits: list[float] = []
    now = (T0 + 10 * HOUR).to_pydatetime()
    loader = public_kline_loader(since=T0, client=client, now=now, sleep=waits.append)
    frame = loader("BTCUSDT", "1h", T0 + 3 * HOUR)
    assert waits == [7.0, 4.0] and len(seen) == 3
    assert list(frame.index) == [T0 + i * HOUR for i in range(3)]


def test_public_kline_loader_never_retries_418() -> None:
    client, seen = _scripted([httpx.Response(418, headers={"Retry-After": "1"})])
    waits: list[float] = []
    loader = public_kline_loader(since=T0, client=client, sleep=waits.append)
    with pytest.raises(httpx.HTTPStatusError, match="HTTP 418") as info:
        loader("BTCUSDT", "1h", T0 + 3 * HOUR)
    assert info.value.response.status_code == 418
    assert len(seen) == 1 and waits == []


def test_public_kline_loader_gives_up_after_bounded_retries() -> None:
    client, seen = _scripted([httpx.Response(429, headers={"Retry-After": "1"})] * 10)
    waits: list[float] = []
    loader = public_kline_loader(since=T0, client=client, sleep=waits.append)
    with pytest.raises(httpx.HTTPStatusError, match=r"HTTP 429.*4 attempt"):
        loader("BTCUSDT", "1h", T0 + 3 * HOUR)
    assert len(seen) == 1 + cmp_mod.KLINE_MAX_RETRIES and waits == [1.0, 1.0, 1.0]


def test_public_kline_loader_caps_the_total_wait() -> None:
    """A Retry-After that would push the load's total wait past the cap is not waited for."""
    client, seen = _scripted([httpx.Response(429, headers={"Retry-After": "45"})] * 3)
    waits: list[float] = []
    loader = public_kline_loader(since=T0, client=client, sleep=waits.append)
    with pytest.raises(httpx.HTTPStatusError, match="HTTP 429"):
        loader("BTCUSDT", "1h", T0 + 3 * HOUR)
    assert waits == [45.0] and sum(waits) <= cmp_mod.KLINE_MAX_WAIT_S and len(seen) == 2
    client2, _ = _scripted([httpx.Response(429, headers={"Retry-After": "3600"})])
    with pytest.raises(httpx.HTTPStatusError, match="HTTP 429"):
        public_kline_loader(since=T0, client=client2, sleep=waits.append)(
            "BTCUSDT", "1h", T0 + 3 * HOUR
        )
    assert waits == [45.0]  # an hour-long Retry-After is never slept


def _result(candidate: str = "c1") -> Any:
    fw, bt = _daily(6, 0.009)
    return compare(
        fw,
        bt,
        interval="1h",
        candles=flat_candles(30),
        candidate=candidate,
        now=datetime(2026, 10, 24, 9, 0, tzinfo=UTC),
    )


def test_atomic_write_uses_a_unique_fsynced_tmp_and_keeps_the_old_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _result()
    path = comparison_path(result, tmp_path)
    path.parent.mkdir(parents=True)
    # the old fixed "<name>.tmp" name is taken (another writer, or a stale leftover): no clash
    (path.parent / (path.name + ".tmp")).mkdir()
    synced: list[int] = []
    real_fsync = cmp_mod.os.fsync
    monkeypatch.setattr(cmp_mod.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    assert write_comparison(result, tmp_path) == path
    assert json.loads(path.read_text())["candidate"] == "c1"
    assert len(synced) >= 2  # both files fsynced before the rename
    old = path.read_text()

    def broken_replace(src: Any, dst: Any) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(cmp_mod.os, "replace", broken_replace)
    with pytest.raises(OSError, match="rename failed"):
        write_comparison({**result, "notes": ["changed"]}, tmp_path)
    assert path.read_text() == old  # never truncated or lost
    assert not list(path.parent.glob(".comparison-*"))  # the temporary file is cleaned up


def test_write_missing_comparison_creates_only_missing_files(tmp_path: Path) -> None:
    result = _result()
    folder = tmp_path / "c1"
    name = "comparison-2026-01-20.json"  # the journal's name, not today's
    written = write_missing_comparison(result, tmp_path, name=name)
    assert [p.name for p in written] == [name, "comparison-2026-01-20.md"]
    assert json.loads((folder / name).read_text())["candidate"] == "c1"
    (folder / name).write_text("kept\n")
    (folder / name).with_suffix(".md").unlink()
    written = write_missing_comparison(result, tmp_path, name=name)
    assert [p.name for p in written] == ["comparison-2026-01-20.md"]
    assert (folder / name).read_text() == "kept\n"  # never overwritten
    assert write_missing_comparison(result, tmp_path, name=name) == []
    for bad in ("../x.json", "comparison-2026-01-20.json/../../y", "notes.json"):
        with pytest.raises(ValueError, match="not a comparison file name"):
            write_missing_comparison(result, tmp_path, name=bad)


# --------------------------------------------------------------------------- CLI


def test_cli_compare_needs_a_registered_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bot prepared without the pre-registered protocol (Phase 13) is never judged: compare
    refuses instead of writing a comparison. The full prepare -> compare flow is tested in
    test_paper_protocol.py."""
    import paper_trade

    from research.paper.freqtrade import prepare_candidate

    prepared = prepare_candidate(PLAN, candidate="cli", paper_dir=tmp_path)
    assert prepared.db_path is not None
    make_db(
        prepared.db_path,
        [{"pair": "BTC/USDT", "is_open": 0, "open_date": ft_date(T0 + 5 * HOUR),
          "close_date": ft_date(T0 + 9 * HOUR), "open_rate": 100.0, "close_rate": 101.0,
          "close_profit": 0.008}],
    )  # fmt: skip
    candles = synthetic_candles(2000, seed=5, start=T0 - pd.Timedelta(days=40))
    monkeypatch.setattr(paper_trade, "default_loader", lambda cfg: lambda *a: candles)
    argv = ["--paper-dir", str(tmp_path), "compare", "--candidate", "cli", "--source", "archive"]
    assert paper_trade.main([*argv, "--end", "2026-10-01"]) == 2
    err = capsys.readouterr().err
    assert "protocol.json not found" in err and "no registered protocol" in err
    assert not list((tmp_path / "cli").glob("comparison-*"))


def test_cli_compare_without_prepare_fails_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import paper_trade

    assert paper_trade.main(["--paper-dir", str(tmp_path), "compare", "--candidate", "x"]) == 2
    assert "run `prepare` first" in capsys.readouterr().err
