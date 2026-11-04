"""The freqtrade bridge behind ``compare`` for a protocol with protections (PLAN.md Phase 20).

Offline: every run goes through a fake runner that writes the shape real freqtrade 2026.9 wrote on
2026-11-04. Another version may change it, so the parser's job is to refuse anything else, and
these tests pin that.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pandas as pd
import paper_trade
import pytest

from lab.config import load_config
from lab.types import Trade
from research.paper.compare import (
    ForwardTrade,
    bar_is_locked,
    frozen_costs,
    mask_locked_entries,
    modelled_slippage,
    observe_paper,
    replay_trades,
)
from research.paper.freqtrade import class_name_for, prepare_candidate
from research.paper.protected_backtest import (
    BACKTEST_TIMEOUT_S,
    ProtectedBacktestError,
    ProtectedRun,
    backtest_command,
    blocked_note,
    check_backtest_command,
    download_command,
    has_protections,
    masked_replay,
    read_export,
    run_protected_backtest,
    timerange,
)
from research.paper.protocol import (
    Fill,
    evaluate,
    load_registered_protocol,
    recorded_final_comparison,
    recorded_final_verdict,
)
from research.paper.risk import Protections, RiskControls
from tests.test_paper_protocol import (
    CANDLES,
    COSTS,
    DESIGN_START,
    PLAN,
    SPLIT,
    STEP4H,
    _Clock,
    build,
    impl_only,
    observed,
    r2_candles,
    r2_design,
    seed_ledger,
    sharpe_protocol,
)

PROTECTIONS = RiskControls(
    protections=Protections(2, 24, 3, 12, 42, 5, 24, 0.06, 1 / 3), circuit_breaker=None
)
CLASS = "TvPaperCand"
PAIR = "BTC/USDT"
T0 = pd.Timestamp("2026-01-05 08:00", tz="UTC")
FAR_MS = 4_102_444_800_000  # 2100: data that reach past any end in these tests


def protected(**kwargs: Any) -> Any:
    protocol = build(r2_design(), candidate="cand", claimed=0.5)
    return replace(protocol, risk_controls=PROTECTIONS, version=8, **kwargs)


def write_export(directory: Path, members: dict[str, Any], *, name: str = "result.zip") -> None:
    with zipfile.ZipFile(directory / name, "w") as archive:
        for member, content in members.items():
            archive.writestr(member, content if isinstance(content, str) else json.dumps(content))


def lock(
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    pair: str = PAIR,
    side: str = "*",
    active: bool = True,
) -> dict[str, Any]:
    """One entry of the real export's ``locks`` list."""
    return {
        "id": None,
        "pair": pair,
        "lock_time": start.strftime("%Y-%m-%d %H:%M:%S"),
        "lock_timestamp": int(start.timestamp() * 1000),
        "lock_end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
        "lock_end_timestamp": int(end.timestamp() * 1000),
        "reason": "Cooldown period for for 1 candle.",
        "side": side,
        "active": active,
    }


def block_of(trades: list[Any], locks: list[Any] | None = None, end_ms: int = FAR_MS) -> Any:
    return {"trades": trades, "locks": locks or [], "backtest_end_ts": end_ms}


def export_of(
    times: list[str], locks: list[Any] | None = None, end_ms: int = FAR_MS
) -> dict[str, Any]:
    trades = [{"open_date": t, "pair": "BTC/USDT"} for t in times]
    return {
        "backtest-result.json": {"strategy": {CLASS: block_of(trades, locks, end_ms)}},
        "backtest-result_config.json": {"max_open_trades": 1},
        f"backtest-result_{CLASS}.py": "class X: ...",
    }


class FakeRunner:
    """Stands in for ``subprocess.run``: records commands and writes the export."""

    def __init__(
        self,
        times: list[str],
        *,
        fail_on: str = "",
        locks: list[Any] | None = None,
        end_ms: int = FAR_MS,
    ) -> None:
        self.times = times
        self.fail_on = fail_on
        self.locks = locks
        self.end_ms = end_ms
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.calls.append((command, kwargs))
        code = 3 if command[1] == self.fail_on else 0
        if command[1] == "backtesting" and not code:
            out = Path(command[command.index("--backtest-directory") + 1])
            write_export(out, export_of(self.times, self.locks, self.end_ms))
        return type("Done", (), {"returncode": code, "stderr": "exchange said no"})()


def prepared_folder(tmp_path: Path) -> Path:
    prepare_candidate(PLAN, candidate="cand", paper_dir=tmp_path)
    return tmp_path / "cand"


# --------------------------------------------------------------------------- commands


def test_timerange_covers_whole_days() -> None:
    start = pd.Timestamp("2026-01-05 12:00", tz="UTC")
    assert timerange(start, pd.Timestamp("2026-02-03 04:00", tz="UTC")) == "20260105-20260204"
    assert timerange(start, pd.Timestamp("2026-02-03 00:00", tz="UTC")) == "20260105-20260203"


def test_commands_are_the_documented_ones(tmp_path: Path) -> None:
    config, user = tmp_path / "config.json", tmp_path / "user_data"
    end = T0 + pd.Timedelta(days=2)
    assert download_command(config=config, userdir=user, interval="4h", start=T0, end=end) == [
        "freqtrade",
        "download-data",
        "--config",
        str(config),
        "--userdir",
        str(user),
        "--timeframes",
        "4h",
        "--timerange",
        "20260105-20260108",
    ]
    command = backtest_command(
        config=config,
        class_name=CLASS,
        folder=tmp_path,
        userdir=user,
        export_dir=tmp_path / "out",
        start=T0,
        end=end,
    )
    assert command[:2] == ["freqtrade", "backtesting"]
    assert "--enable-protections" in command
    assert command[command.index("--export") + 1] == "trades"
    assert command[command.index("--strategy") + 1] == CLASS
    check_backtest_command(command, config=config)


def test_a_backtest_without_protections_is_refused(tmp_path: Path) -> None:
    """PLAN.md: a protocol with protections and a backtest without them is refused."""
    config = tmp_path / "config.json"
    command = ["freqtrade", "backtesting", "--config", str(config)]
    with pytest.raises(ProtectedBacktestError, match="without --enable-protections"):
        check_backtest_command(command, config=config)


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (["freqtrade", "trade", "--enable-protections"], "only `freqtrade backtesting`"),
        (["freqtrade", "backtesting", "--enable-protections"], "exactly the prepared config"),
        (
            ["freqtrade", "backtesting", "--enable-protections", "--config", "/other.json"],
            "exactly the prepared config",
        ),
    ],
)
def test_only_the_prepared_backtest_runs(command: list[str], message: str, tmp_path: Path) -> None:
    with pytest.raises(ProtectedBacktestError, match=message):
        check_backtest_command(command, config=tmp_path / "config.json")


# --------------------------------------------------------------------------- export


def test_export_gives_utc_open_times(tmp_path: Path) -> None:
    write_export(tmp_path, export_of(["2026-01-05 08:00:00+00:00", "2026-01-06 12:00:00"]))
    assert read_export(tmp_path, CLASS, PAIR).opened == (
        T0,
        pd.Timestamp("2026-01-06 12:00", tz="UTC"),
    )


def test_export_with_no_trades_is_empty(tmp_path: Path) -> None:
    write_export(tmp_path, export_of([]))
    assert read_export(tmp_path, CLASS, PAIR).opened == ()


def test_export_reads_locks_and_the_data_end(tmp_path: Path) -> None:
    end = T0 + STEP4H
    locks = [
        lock(T0, end),
        lock(T0, end, pair="*"),  # a global lock (StoplossGuard, MaxDrawdown) blocks this pair
        lock(T0, end, side="long"),
        lock(T0, end, pair="ETH/USDT"),
        lock(T0, end, side="short"),
        lock(T0, end, active=False),
    ]
    write_export(tmp_path, export_of([], locks, end_ms=1_767_600_000_000))
    run = read_export(tmp_path, CLASS, PAIR)
    assert run.locks == ((T0, end),) * 3
    assert run.backtest_end == pd.Timestamp(1_767_600_000_000, unit="ms", tz="UTC")


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"trades": [], "backtest_end_ts": FAR_MS}, "no locks list"),
        ({"trades": [], "locks": None, "backtest_end_ts": FAR_MS}, "no locks list"),
        ({"trades": [], "locks": {}, "backtest_end_ts": FAR_MS}, "no locks list"),
        ({"trades": [], "locks": ["x"], "backtest_end_ts": FAR_MS}, "unreadable lock"),
        (block_of([], [{**lock(T0, T0 + STEP4H), "lock_timestamp": "x"}]), "lock_timestamp"),
        (block_of([], [{**lock(T0, T0 + STEP4H), "lock_end_timestamp": None}]), "lock_end"),
        (block_of([], [{**lock(T0, T0 + STEP4H), "lock_timestamp": True}]), "lock_timestamp"),
        (block_of([], [{**lock(T0, T0 + STEP4H), "active": "yes"}]), "unreadable lock"),
        (block_of([], [{**lock(T0, T0 + STEP4H), "pair": None}]), "unreadable lock"),
        (block_of([], [{**lock(T0, T0 + STEP4H), "side": "up"}]), "unreadable lock"),
        (block_of([], [lock(T0 + STEP4H, T0)]), "ends before it starts"),
        ({"trades": [], "locks": []}, "backtest_end_ts"),
        ({"trades": [], "locks": [], "backtest_end_ts": "2026"}, "backtest_end_ts"),
    ],
)
def test_missing_or_malformed_locks_fail_closed(
    tmp_path: Path, block: dict[str, Any], message: str
) -> None:
    """A misread lock would excuse a skipped entry."""
    write_export(tmp_path, {"a.json": {"strategy": {CLASS: block}}})
    with pytest.raises(ProtectedBacktestError, match=message):
        read_export(tmp_path, CLASS, PAIR)


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ({"a.json": {"strategy": {"Other": {"trades": []}}}}, "0 JSON files"),
        ({"a.json": {"strategy": {CLASS: {"results": []}}}}, "0 JSON files"),
        ({"a.json": {"trades": []}}, "0 JSON files"),
        ({"a.json": [1, 2]}, "0 JSON files"),
        ({"a.json": "not json"}, "0 JSON files"),
        (
            {
                "a.json": {"strategy": {CLASS: {"trades": []}}},
                "b.json": {"strategy": {CLASS: {"trades": []}}},
            },
            "2 JSON files",
        ),
        ({"a.json": {"strategy": {CLASS: block_of([{"pair": "x"}])}}}, "unreadable open_date"),
        (
            {"a.json": {"strategy": {CLASS: block_of([{"open_date": "soon"}])}}},
            "unreadable open_date",
        ),
        (
            {"a.json": {"strategy": {CLASS: block_of([{"open_date": 1767600000000}])}}},
            "unreadable open_date",
        ),
        ({"a.json": {"strategy": {CLASS: block_of(["x"])}}}, "unreadable open_date"),
    ],
)
def test_any_other_export_shape_raises_instead_of_guessing(
    tmp_path: Path, members: dict[str, Any], message: str
) -> None:
    write_export(tmp_path, members)
    with pytest.raises(ProtectedBacktestError, match=message):
        read_export(tmp_path, CLASS, PAIR)


def test_export_needs_exactly_one_zip(tmp_path: Path) -> None:
    with pytest.raises(ProtectedBacktestError, match="found 0"):
        read_export(tmp_path, CLASS, PAIR)
    write_export(tmp_path, export_of([]))
    write_export(tmp_path, export_of([]), name="older.zip")
    with pytest.raises(ProtectedBacktestError, match="found 2"):
        read_export(tmp_path, CLASS, PAIR)


def test_a_corrupt_zip_raises(tmp_path: Path) -> None:
    (tmp_path / "result.zip").write_bytes(b"not a zip")
    with pytest.raises(ProtectedBacktestError, match="cannot read the export"):
        read_export(tmp_path, CLASS, PAIR)


# --------------------------------------------------------------------------- running


def test_run_downloads_then_backtests_and_stays_dry(tmp_path: Path) -> None:
    folder = prepared_folder(tmp_path)
    runner = FakeRunner(["2026-01-05 08:00:00+00:00"])
    protocol = protected()
    end = pd.Timestamp(protocol.start, unit="s", tz="UTC") + pd.Timedelta(days=4)
    run = run_protected_backtest(
        protocol,
        folder,
        class_name=CLASS,
        interval="4h",
        end=end,
        runner=runner,
        which=lambda _: "/bin/freqtrade",
        env={"FREQTRADE__EXCHANGE__KEY": "leaked", "PATH": "/bin"},
    )
    assert run.opened == (T0,)
    assert [c[1] for c, _ in runner.calls] == ["download-data", "backtesting"]
    download, backtest = runner.calls[0][0], runner.calls[1][0]
    warm_day = pd.Timestamp(protocol.data.warm_from, unit="s", tz="UTC").strftime("%Y%m%d")
    assert download[download.index("--timerange") + 1].startswith(warm_day)
    assert "--enable-protections" in backtest
    assert backtest[backtest.index("--userdir") + 1] == str(folder / "user_data")
    for _, kwargs in runner.calls:
        assert kwargs["env"]["FREQTRADE__DRY_RUN"] == "true"
        assert "FREQTRADE__EXCHANGE__KEY" not in kwargs["env"]
    assert runner.calls[1][1]["timeout"] == BACKTEST_TIMEOUT_S


def _covering(tmp_path: Path, end_ms: int) -> ProtectedRun:
    return run_protected_backtest(
        protected(),
        prepared_folder(tmp_path),
        class_name=CLASS,
        interval="4h",
        end=T0 + 10 * STEP4H,
        runner=FakeRunner([T0.isoformat()], end_ms=end_ms),
        which=lambda _: "/bin/freqtrade",
    )


def test_data_that_stop_before_the_end_fail_closed(tmp_path: Path) -> None:
    """Entries past the data's end used to count as blocked; a download can exit 0 and fetch
    nothing. Real run: data to 2026-11-04 asked for 2026-12-20."""
    stops = T0 + 8 * STEP4H  # the last bar is T0 + 9 bars
    with pytest.raises(ProtectedBacktestError, match="did not cover the period"):
        _covering(tmp_path, int(stops.timestamp() * 1000))


def test_data_to_the_last_bar_are_enough(tmp_path: Path) -> None:
    last_bar = T0 + 9 * STEP4H
    assert _covering(tmp_path, int(last_bar.timestamp() * 1000)).backtest_end == last_bar


def test_no_subprocess_for_a_protocol_without_protections(tmp_path: Path) -> None:
    runner = FakeRunner([])
    plain = build(r2_design(), candidate="cand", claimed=0.5)
    assert not has_protections(plain)
    with pytest.raises(ProtectedBacktestError, match="has no protections"):
        run_protected_backtest(
            plain,
            tmp_path,
            class_name=CLASS,
            interval="4h",
            end=T0,
            runner=runner,
            which=lambda _: "/bin/freqtrade",
        )
    assert runner.calls == []


def test_breaker_only_controls_run_no_subprocess() -> None:
    from research.paper.risk import CircuitBreaker

    breaker = RiskControls(protections=None, circuit_breaker=CircuitBreaker(3.0, 10.0, 1000.0))
    plain = build(r2_design(), candidate="cand", claimed=0.5)
    assert not has_protections(replace(plain, risk_controls=breaker, version=8))


def test_missing_freqtrade_fails_closed(tmp_path: Path) -> None:
    runner = FakeRunner([])
    with pytest.raises(ProtectedBacktestError, match="freqtrade not installed"):
        run_protected_backtest(
            protected(),
            tmp_path,
            class_name=CLASS,
            interval="4h",
            end=T0,
            runner=runner,
            which=lambda _: None,
        )
    assert runner.calls == []


@pytest.mark.parametrize(
    ("fail_on", "calls"), [("download-data", ["download-data"]), ("backtesting", None)]
)
def test_a_failed_run_fails_closed(tmp_path: Path, fail_on: str, calls: list[str] | None) -> None:
    folder = prepared_folder(tmp_path)
    runner = FakeRunner([], fail_on=fail_on)
    with pytest.raises(ProtectedBacktestError, match=f"freqtrade {fail_on} exited 3"):
        run_protected_backtest(
            protected(),
            folder,
            class_name=CLASS,
            interval="4h",
            end=T0,
            runner=runner,
            which=lambda _: "/bin/freqtrade",
        )
    assert [c[1] for c, _ in runner.calls] == (calls or ["download-data", "backtesting"])


def test_a_live_config_is_refused_before_any_subprocess(tmp_path: Path) -> None:
    folder = prepared_folder(tmp_path)
    config = json.loads((folder / "config.json").read_text())
    (folder / "config.json").write_text(json.dumps({**config, "dry_run": False}))
    runner = FakeRunner([])
    with pytest.raises(ValueError, match="dry_run"):
        run_protected_backtest(
            protected(),
            folder,
            class_name=CLASS,
            interval="4h",
            end=T0,
            runner=runner,
            which=lambda _: "/bin/freqtrade",
        )
    assert runner.calls == []


# --------------------------------------------------------------------------- the masked replay


LEVEL_PLAN = {
    **PLAN,
    "long_entry": "fast > slow",
    "long_exit": "fast < slow",
    "risk": {"position_size_pct": 100, "stop_loss_pct": 1.0},
}


def period(protocol: Any) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    return start, pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")


def world(plan: Any = PLAN, seed: int = 7) -> tuple[Any, pd.DataFrame, Any]:
    """A protocol, its candles, and the lab replay of ``plan`` on them under given locks."""
    protocol = build(r2_design(), candidate="r2", claimed=0.5)
    candles = r2_candles(protocol, seed)
    start, end = period(protocol)

    def replay(locks: Any = ()) -> list[Trade]:
        return replay_trades(plan, candles, start=start, end=end, costs=COSTS, locks=locks)

    return protocol, candles, replay


def ran(opened: list[pd.Timestamp], locks: tuple[Any, ...] = ()) -> ProtectedRun:
    return ProtectedRun(opened=tuple(opened), locks=locks, backtest_end=T0 + 1000 * STEP4H)


def mask(lab: list[Trade], run: ProtectedRun, replay: Any, tolerance: int = 1) -> Any:
    return masked_replay(lab, run, replay, interval="4h", tolerance_bars=tolerance)


def mirror(trades: list[Trade]) -> list[ForwardTrade]:
    return [
        ForwardTrade(
            pair="BTC/USDT",
            is_open=False,
            open_time=t.entry_time + pd.Timedelta(seconds=5),
            open_rate=t.entry_price,
            close_time=t.exit_time + pd.Timedelta(seconds=5),
            close_rate=t.exit_price,
            fee_open=COSTS.fee_rate,
            fee_close=COSTS.fee_rate,
            exit_reason="exit_signal" if t.exit_reason == "signal" else "stop_loss",
        )
        for t in trades
        if t.exit_time is not None
    ]


def test_an_entry_on_a_locked_bar_is_masked_and_no_other() -> None:
    _, _, replay = world()
    lab = replay()
    assert len(lab) >= 4
    entry = lab[2].entry_time
    around = (entry - STEP4H, entry + STEP4H)
    done = mask(lab, ran([t.entry_time for t in lab if t is not lab[2]], (around,)), replay)
    assert [t.entry_time for t in done.trades] == [t.entry_time for t in lab if t is not lab[2]]
    assert (done.masked, done.total) == (1, len(lab))


def test_no_lock_gives_the_unmasked_replay() -> None:
    _, _, replay = world()
    lab = replay()
    done = mask(lab, ran([t.entry_time for t in lab]), replay)
    assert done.trades == lab and done.masked == 0


def test_a_skipped_entry_outside_every_lock_stays_and_counts() -> None:
    """The C1 case: 26 of 29 entries used to vanish because the bot skipped them, while the export
    held three 1-candle cooldowns. Skipping is what the timing rules are for, so a bot that made
    only the first few entries is irregular on each one it left out."""
    protocol, candles, replay = world()
    lab = replay()
    assert len(lab) >= 5
    closed = [t for t in lab if t.exit_time is not None]
    cooldowns = tuple((t.exit_time, t.exit_time + STEP4H) for t in closed[:3])
    done = mask(lab, ran([t.entry_time for t in lab[:3]], cooldowns), replay)
    assert done.trades == lab and done.masked == 0
    end = period(protocol)[1]
    skipped = observe_paper(
        protocol,
        mirror(lab[:3]),
        candles,
        lab_trades=done.trades,
        slippage=modelled_slippage(candles, COSTS),
        asof=end,
    )
    assert skipped.unmatched_lab_entries == len(lab) - 3
    assert skipped.irregular >= len(lab) - 3


@pytest.mark.parametrize(
    ("offset", "blocked"),
    [(-1, False), (0, False), (1, True), (2, False)],
)
def test_a_lock_blocks_the_bars_after_its_start_up_to_its_end(offset: int, blocked: bool) -> None:
    """freqtrade asks at the fill bar whether lock_end_time > the candle date. The lock is recorded
    on its exit's bar, and backtest_loop runs entries before exits, so that bar's entry was free."""
    locks = ((T0 + 10 * STEP4H, T0 + 12 * STEP4H),)
    assert bar_is_locked(T0 + (10 + offset) * STEP4H, locks) is blocked


def test_the_trade_that_set_a_lock_on_its_own_fill_bar_stays() -> None:
    """Real freqtrade: a trade stopped out on its fill bar records a lock from that bar, so that
    entry sits on the lock's start. Masking it would delete the trade that caused the lock."""
    _, _, replay = world()
    lab = replay()
    entry = lab[1].entry_time
    done = mask(lab, ran([t.entry_time for t in lab], ((entry, entry + STEP4H),)), replay)
    assert done.trades == lab and done.masked == 0


def test_a_signal_is_masked_by_a_lock_on_its_fill_bar_not_on_its_own_bar() -> None:
    index = pd.date_range(T0, periods=4, freq="4h", name="time")
    candles = pd.DataFrame({"tradable": [True, False, True, True]}, index=index)
    entries = pd.Series([True, False, False, True], index=index)
    # the signal on row 0 fills on row 2, the next tradable bar
    on_fill = ((index[1], index[3]),)
    assert mask_locked_entries(entries, candles, on_fill).tolist() == [False, False, False, True]
    on_signal = ((index[0] - STEP4H, index[1]),)
    assert mask_locked_entries(entries, candles, on_signal).tolist() == entries.tolist()


def test_a_masked_replay_matches_a_bot_held_out_by_a_lock_on_a_level_signal() -> None:
    """``fast > slow`` stays true after a stop-out, so the bot re-enters when the lock ends, at a
    bar the unprotected replay may not trade. Deleting replay entries afterwards left those
    entries unmatched: 13 irregular fills on a real bot identical to its backtest."""
    protocol, candles, replay = world(LEVEL_PLAN)
    lab = replay()
    stops = [t for t in lab if t.exit_reason == "stop_loss" and t.exit_time is not None]
    assert stops
    locks = tuple((t.exit_time, t.exit_time + 4 * STEP4H) for t in stops)
    masked = replay(locks)
    assert {t.entry_time for t in masked} - {t.entry_time for t in lab}  # a re-entry of its own
    bot = mirror(masked)
    assert len(bot) >= 3
    end = period(protocol)[1]
    run = partial(
        observe_paper,
        protocol,
        bot,
        candles,
        slippage=modelled_slippage(candles, COSTS),
        asof=end,
    )
    deleted = [t for t in lab if not bar_is_locked(t.entry_time, locks)]
    still_open = sum(t.exit_time is None for t in masked)  # ``mirror`` copies closed trades
    assert run(lab_trades=masked).irregular == still_open
    assert run(lab_trades=deleted).irregular > still_open


def test_the_judged_trades_come_from_the_re_run_not_from_a_deletion() -> None:
    _, _, replay = world(LEVEL_PLAN)
    lab = replay()
    stops = [t for t in lab if t.exit_reason == "stop_loss" and t.exit_time is not None]
    locks = tuple((t.exit_time, t.exit_time + 4 * STEP4H) for t in stops)
    done = mask(lab, ran([lab[0].entry_time], locks), replay)
    assert done.trades == replay(locks)
    assert done.trades != [t for t in lab if not bar_is_locked(t.entry_time, locks)]


def test_a_shifted_entry_counts_as_masked() -> None:
    """Masked means this bar's entry did not happen, so entering a bar later is no excuse: a share
    from the entry counts alone would read 0 here."""
    _, _, replay = world()
    lab = replay()
    later = [replace(t, entry_time=t.entry_time + STEP4H) for t in lab]
    done = mask(lab, ran([t.entry_time for t in lab]), lambda locks: later)
    assert done.masked == done.total == len(lab)


def test_an_empty_backtest_is_not_read_as_every_entry_blocked() -> None:
    _, _, replay = world()
    with pytest.raises(ProtectedBacktestError, match="cannot block every entry"):
        mask(replay(), ran([]), replay)
    assert mask([], ran([]), replay).total == 0


def test_the_first_replay_entry_must_be_matched() -> None:
    """A protection reacts to earlier trades, so a run that misses the first entry did not run
    the plan, whatever its later entries match, and a lock over that bar proves nothing."""
    _, _, replay = world()
    lab = replay()
    first = lab[0].entry_time
    run = ran([t.entry_time for t in lab[1:]], ((first - STEP4H, first + STEP4H),))
    with pytest.raises(ProtectedBacktestError, match="no entry for the first lab entry"):
        mask(lab, run, replay, tolerance=0)


def test_a_freqtrade_entry_on_a_blocked_bar_fails_closed() -> None:
    """Then the locks do not mean what the masking assumes, and any mask would be a guess."""
    _, _, replay = world()
    lab = replay()
    entry = lab[1].entry_time
    run = ran([t.entry_time for t in lab], ((entry - STEP4H, entry + STEP4H),))
    with pytest.raises(ProtectedBacktestError, match="its own locks block"):
        mask(lab, run, replay)


def test_the_note_reports_masked_over_total() -> None:
    assert blocked_note(1, 29).startswith("Protections: masked 1 of 29 lab replay entries (3%)")
    assert blocked_note(0, 0).startswith("Protections: masked 0 of 0 lab replay entries:")


def test_a_blocked_entry_does_not_read_as_an_irregular_fill() -> None:
    """The point of the bridge: the bot skipped a trade because of a protection, the replay knows,
    and the timing rules see no irregular fill. Without the mask they count it."""
    protocol, candles, replay = world()
    lab = replay()
    closed = [t for t in lab if t.exit_time is not None]
    assert len(closed) >= 3
    blocked = closed[1]
    bot = mirror([t for t in closed if t is not blocked])
    opened = [t.entry_time for t in lab if t is not blocked]
    locks = ((blocked.entry_time - STEP4H, blocked.entry_time + STEP4H),)
    done = mask(lab, ran(opened, locks), replay, tolerance=protocol.matching.tolerance_bars)
    assert done.masked == 1
    slip = modelled_slippage(candles, COSTS)
    end = period(protocol)[1]
    run = partial(observe_paper, protocol, bot, candles, slippage=slip, asof=end)
    assert run(lab_trades=done.trades).irregular < run(lab_trades=lab).irregular


# --------------------------------------------------------------------------- compare, end to end


@pytest.fixture
def rc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    base = load_config()
    splits = replace(base.splits, design_start=DESIGN_START, holdout_start=SPLIT)
    cfg = replace(base, data_dir=tmp_path / "lab", splits=splits)
    monkeypatch.setattr(paper_trade, "load_config", lambda: cfg)
    monkeypatch.setattr(paper_trade, "default_loader", lambda c: lambda *a: CANDLES)
    monkeypatch.setattr(paper_trade, "datetime", _Clock)
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 1, 9, 15, tzinfo=UTC))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN))
    seed_ledger(cfg.ledger_path, PLAN, wf_sharpes=(0.4,))
    toml = tmp_path / "risk.toml"
    toml.write_text(
        "[risk_controls.protections]\n"
        "cooldown_candles = 2\n"
        "stoploss_guard_lookback_candles = 24\n"
        "stoploss_guard_trade_limit = 3\n"
        "stoploss_guard_stop_candles = 12\n"
        "max_drawdown_lookback_candles = 42\n"
        "max_drawdown_trade_limit = 5\n"
        "max_drawdown_stop_candles = 24\n"
        "max_drawdown_allowed = 0.06\n"
        "max_blocked_share = 0.333\n"
    )
    paper, journal = tmp_path / "paper", tmp_path / "journal"
    base_args = ["--paper-dir", str(paper), "--journal-dir", str(journal)]
    argv = [*base_args, "prepare", "--plan-file", str(plan_file), "--candidate", "cand"]
    assert paper_trade.main([*argv, "--candle-source", "archive", "--risk-config", str(toml)]) == 0
    monkeypatch.setattr(_Clock, "at", datetime(2025, 11, 20, 0, 0, tzinfo=UTC))
    import sqlite3

    conn = sqlite3.connect(paper / "cand" / "dryrun.sqlite")
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN, "
        "open_date DATETIME, close_date DATETIME, open_rate FLOAT, close_rate FLOAT)"
    )
    conn.commit()
    conn.close()
    return {"paper": paper, "journal": journal, "base": base_args, "cfg": cfg}


def replay_entries(rc: dict[str, Any]) -> list[pd.Timestamp]:
    protocol, _ = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    warm = pd.Timestamp(protocol.data.warm_from, unit="s", tz="UTC")
    end = pd.Timestamp(_Clock.at)
    history = CANDLES.loc[(CANDLES.index >= warm) & (CANDLES.index < end)]
    trades = replay_trades(PLAN, history, start=start, end=end, costs=frozen_costs(protocol))
    assert len(trades) >= 2
    return [t.entry_time for t in trades]


def use(monkeypatch: pytest.MonkeyPatch, runner: Any, found: str | None = "/bin/ft") -> None:
    monkeypatch.setattr(
        paper_trade,
        "run_protected_backtest",
        partial(run_protected_backtest, runner=runner, which=lambda _: found),
    )


def verdict_line(rc: dict[str, Any]) -> str:
    (entry,) = rc["journal"].glob("*.md")
    return next(ln for ln in entry.read_text().splitlines() if ln.startswith("- 20"))


def test_compare_masks_blocked_entries_and_says_how_many(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = replay_entries(rc)
    cooldown = [lock(entries[-1] - STEP4H, entries[-1] + 2 * STEP4H)]
    runner = FakeRunner([t.isoformat() for t in entries[:-1]], locks=cooldown)
    monkeypatch.setattr(
        paper_trade,
        "run_protected_backtest",
        partial(run_protected_backtest, runner=runner, which=lambda _: "/bin/freqtrade"),
    )
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    assert [c[1] for c, _ in runner.calls] == ["download-data", "backtesting"]
    report = next((rc["paper"] / "cand").glob("comparison-*.md")).read_text()
    assert f"Protections: masked 1 of {len(entries)} lab replay entries" in report
    assert f"Protections: masked 1 of {len(entries)} lab replay entries" in verdict_line(rc)


def test_compare_keeps_an_entry_the_backtest_skipped_without_a_lock(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = replay_entries(rc)
    runner = FakeRunner([t.isoformat() for t in entries[:1]])
    monkeypatch.setattr(
        paper_trade,
        "run_protected_backtest",
        partial(run_protected_backtest, runner=runner, which=lambda _: "/bin/freqtrade"),
    )
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    report = next((rc["paper"] / "cand").glob("comparison-*.md")).read_text()
    assert f"Protections: masked 0 of {len(entries)} lab replay entries" in report


@pytest.mark.parametrize(
    "kind", ["missing", "failed", "bad-export", "no-locks", "short-data", "first-unmatched"]
)
def test_compare_exits_2_and_writes_nothing_when_the_bridge_fails(
    rc: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    (entry,) = rc["journal"].glob("*.md")
    before = entry.read_text()
    entries = replay_entries(rc)
    times = [t.isoformat() for t in entries]
    runner: Any = FakeRunner(times, fail_on="backtesting" if kind == "failed" else "")
    if kind == "short-data":
        runner = FakeRunner(times, end_ms=int(entries[0].timestamp() * 1000))
    if kind == "first-unmatched":
        runner = FakeRunner(times[1:])
    if kind in ("bad-export", "no-locks"):
        shapes: dict[str, Any] = {
            "bad-export": {"unexpected": True},
            "no-locks": {"strategy": {CLASS: {"trades": [], "backtest_end_ts": FAR_MS}}},
        }

        def runner(command: list[str], **kwargs: Any) -> Any:
            if command[1] == "backtesting":
                out = Path(command[command.index("--backtest-directory") + 1])
                write_export(out, {"a.json": shapes[kind]})
            return type("Done", (), {"returncode": 0, "stderr": ""})()

    found = None if kind == "missing" else "/bin/freqtrade"
    monkeypatch.setattr(
        paper_trade,
        "run_protected_backtest",
        partial(run_protected_backtest, runner=runner, which=lambda _: found),
    )
    capsys.readouterr()
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 2
    assert "cannot compare" in capsys.readouterr().err
    assert entry.read_text() == before
    assert not list((rc["paper"] / "cand").glob("comparison-*"))


def test_compare_refuses_a_strategy_with_other_protections(
    rc: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    path = rc["paper"] / "cand" / f"{class_name_for('cand')}.py"
    path.write_text(path.read_text().replace("'cooldown_candles': 2", "'cooldown_candles': 9"))
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 2
    assert "is not the strategy this protocol renders" in capsys.readouterr().err


# --------------------------------------------------------------------------- the cap on masking


def with_cap(share: float = 1 / 3) -> Any:
    guards = Protections(2, 24, 3, 12, 42, 5, 24, 0.06, share)
    return replace(impl_only(), risk_controls=RiskControls(guards, None))


def test_up_to_the_cap_a_pass_stands() -> None:
    p = with_cap()
    verdict = evaluate(p, replace(observed(p), blocked_entries=9, replay_entries=27))
    assert verdict.verdict == "pass", verdict.headline
    assert next(c for c in verdict.checks if c.name == "blocked_share").status == "ok"


def test_above_the_cap_the_best_verdict_is_a_final_inconclusive() -> None:
    """A bot mirroring its backtest passed 4/27 at 68% masked: that measured the protections."""
    p = with_cap()
    verdict = evaluate(p, replace(observed(p), blocked_entries=10, replay_entries=27))
    assert verdict.verdict == "inconclusive" and verdict.final
    assert "masked 10 of 27 replay entries" in verdict.headline
    assert "max_blocked_share" in verdict.headline


def test_above_the_cap_a_real_fail_still_fails() -> None:
    p = with_cap()
    bad = observed(p, entries=p.trades.hi + 1)
    verdict = evaluate(p, replace(bad, blocked_entries=20, replay_entries=27))
    assert verdict.verdict == "fail"


def test_the_cap_does_not_decide_before_the_decision_bar() -> None:
    p = with_cap()
    early = observed(p, asof=p.start + 10 * 86400, entries=1)
    verdict = evaluate(p, replace(early, blocked_entries=20, replay_entries=27))
    assert verdict.verdict == "continue"


def test_a_protocol_without_protections_has_no_such_check() -> None:
    p = impl_only()
    verdict = evaluate(p, replace(observed(p), blocked_entries=20, replay_entries=27))
    assert verdict.verdict == "pass" and all(c.name != "blocked_share" for c in verdict.checks)


def after_grace(p: Any) -> int:
    return p.decision_end + round(p.data.grace_days * 86400)


def test_a_stake_problem_never_demotes_a_fail() -> None:
    p = with_cap()
    bad = observed(p, entries=p.trades.hi + 1)
    verdict = evaluate(p, replace(bad, stake_problem="paper stake: x1.3"))
    assert verdict.verdict == "fail" and verdict.final
    assert "implementation" in verdict.fail_kinds
    assert "implementation problem: paper stake: x1.3" in verdict.reasons
    assert "implementation problem: paper stake: x1.3" in verdict.headline


def test_a_stake_problem_is_never_a_pass() -> None:
    p = with_cap()
    assert evaluate(p, observed(p)).verdict == "pass"
    verdict = evaluate(p, replace(observed(p), stake_problem="paper stake: x1.3"))
    assert verdict.verdict == "inconclusive" and verdict.final and verdict.fail_kinds == ()
    assert verdict.reasons == ("implementation problem: paper stake: x1.3",)


def test_a_stake_problem_before_the_decision_bar_continues_and_says_so() -> None:
    p = with_cap()
    early = observed(p, asof=p.start + 10 * 86400, entries=1)
    verdict = evaluate(p, replace(early, stake_problem="paper stake: x1.3"))
    assert verdict.verdict == "continue"
    assert "implementation problem: paper stake: x1.3" in verdict.headline


def test_a_stake_problem_keeps_the_data_rule() -> None:
    """Missing bars still wait for the grace, and the verdict then names both."""
    p = with_cap()
    gap = observed(p, bars_present=p.decision_bars - 1)
    verdict = evaluate(p, replace(gap, stake_problem="paper stake: x1.3"))
    assert verdict.verdict == "continue" and "paper stake: x1.3" in verdict.headline
    late = replace(gap, observed_until=after_grace(p), stake_problem="paper stake: x1.3")
    verdict = evaluate(p, late)
    assert verdict.verdict == "inconclusive"
    assert verdict.reasons[0].startswith("data:") and "paper stake: x1.3" in verdict.reasons[1]


@pytest.mark.parametrize("rule", ["irregular_fills", "irregular_fills_cap", "shortfall"])
def test_a_replay_check_says_nothing_without_its_replay(rule: str) -> None:
    p = with_cap()
    gross = tuple(Fill(p.start + i * 7_200, "entry", 999.0) for i in range(6))
    change = {
        "irregular_fills": {"late": 20},
        "irregular_fills_cap": {"settled": 9_999},
        "shortfall": {"fills": gross},
    }
    bad = observed(p, **change[rule])
    assert any(c.name.startswith(rule) for c in evaluate(p, bad).checks if c.status == "fail")
    inside = evaluate(p, replace(bad, replay_unknown="protected backtest: down"))
    assert inside.verdict == "continue" and inside.weeks == () and inside.alerts == ()
    assert not [c for c in inside.checks if c.status == "fail"]
    late = replace(bad, replay_unknown="protected backtest: down", observed_until=after_grace(p))
    verdict = evaluate(p, late)
    assert verdict.verdict == "inconclusive" and verdict.final
    assert verdict.reasons[0] == "cannot be judged: protected backtest: down"


@pytest.mark.parametrize(
    ("change", "word"),
    [
        ({"entries": 10_000}, "entries"),  # entries_cap, at any time
        ({"entries": 0}, "entries"),
        ({"exposure": 0.0}, "exposure"),
        ({"exposure": 1.0}, "bars in market"),  # exposure_cap
    ],
    ids=["entries_cap", "entries", "exposure", "exposure_cap"],
)
def test_a_replay_independent_fail_stands_without_the_replay(
    change: dict[str, Any], word: str
) -> None:
    p = with_cap()
    verdict = evaluate(p, replace(observed(p, **change), replay_unknown="protected backtest: down"))
    assert verdict.verdict == "fail" and word in verdict.headline
    assert "cannot be judged: protected backtest: down" in verdict.reasons


def test_a_failed_sharpe_stands_without_the_replay() -> None:
    p = sharpe_protocol()
    losing = observed(p, returns=(-0.01, -0.03, -0.02) * 200)
    assert evaluate(p, losing).verdict == "fail"
    verdict = evaluate(p, replace(losing, replay_unknown="protected backtest: down"))
    assert verdict.verdict == "fail" and verdict.fail_kinds == ("performance",)


def test_a_regime_fail_without_the_replay_never_claims_every_fill_matched() -> None:
    p = with_cap()
    verdict = evaluate(p, replace(observed(p, entries=0), replay_unknown="down"))
    assert "timing unknown" in verdict.headline and "lag 0" not in verdict.headline
    assert verdict.fail_kinds == ("implementation", "regime")


def test_a_trip_beats_a_stake_problem_and_an_unknown_replay() -> None:
    p = with_cap()
    tripped = replace(
        observed(p),
        breaker_trip="circuit breaker tripped: x",
        stake_problem="paper stake: y",
        replay_unknown="protected backtest: z",
    )
    verdict = evaluate(p, tripped)
    assert verdict.verdict == "fail" and verdict.fail_kinds == ("circuit_breaker",)
    assert verdict.reasons == ("circuit breaker tripped: x",)


# --------------------------------------------------------------------------- no verdict forever


def past_grace(rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    protocol, _ = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    grace = pd.Timedelta(days=protocol.data.grace_days)
    at = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC") + grace
    monkeypatch.setattr(_Clock, "at", at.to_pydatetime())
    return protocol


def in_band_bot(rc: dict[str, Any]) -> None:
    """The dry-run DB of a bot that traded the replay: nothing replay-independent fails."""
    protocol, _ = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    warm = pd.Timestamp(protocol.data.warm_from, unit="s", tz="UTC")
    end = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    history = CANDLES.loc[(CANDLES.index >= warm) & (CANDLES.index < end)]
    trades = replay_trades(PLAN, history, start=start, end=end, costs=frozen_costs(protocol))

    def text(at: pd.Timestamp) -> str:
        return at.tz_localize(None).strftime("%Y-%m-%d %H:%M:%S.%f")

    conn = sqlite3.connect(rc["paper"] / "cand" / "dryrun.sqlite")
    for t in trades:
        conn.execute(
            "INSERT INTO trades (pair, is_open, open_date, close_date, open_rate, close_rate) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                PAIR,
                0 if t.exit_time is not None else 1,
                text(t.entry_time + pd.Timedelta(seconds=5)),
                None if t.exit_time is None else text(t.exit_time + pd.Timedelta(seconds=5)),
                t.entry_price,
                t.exit_price,
            ),
        )
    conn.commit()
    conn.close()


def too_many_entries(rc: dict[str, Any]) -> None:
    """More one-bar entries than ``entries_cap`` allows, all inside the elapsed period."""
    protocol, _ = load_registered_protocol(rc["paper"], "cand", rc["journal"])
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    conn = sqlite3.connect(rc["paper"] / "cand" / "dryrun.sqlite")
    for k in range(protocol.trades.hi + 1):
        at = (start + k * STEP4H).tz_localize(None).strftime("%Y-%m-%d %H:%M:%S.%f")
        conn.execute(
            "INSERT INTO trades (pair, is_open, open_date, close_date, open_rate, close_rate) "
            "VALUES (?, 0, ?, ?, 100.0, 100.0)",
            (PAIR, at, at),
        )
    conn.commit()
    conn.close()


def test_a_bridge_that_still_fails_past_the_grace_is_a_final_inconclusive(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    past_grace(rc, monkeypatch)
    in_band_bot(rc)
    use(monkeypatch, FakeRunner([], fail_on="backtesting"))
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    (entry,) = rc["journal"].glob("*.md")
    recorded = recorded_final_verdict(entry)
    assert recorded is not None and recorded["outcome"] == "inconclusive"
    line = verdict_line(rc)
    assert "cannot be judged: protected backtest: freqtrade backtesting exited 3" in line
    assert "Protections: masked entries not computed:" in line


def test_a_second_compare_keeps_the_final_inconclusive(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    past_grace(rc, monkeypatch)
    in_band_bot(rc)
    use(monkeypatch, FakeRunner([], fail_on="backtesting"))
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    (entry,) = rc["journal"].glob("*.md")
    before = entry.read_text()
    assert 'outcome: "inconclusive"' in before
    use(monkeypatch, FakeRunner(["2025-11-05 00:00:00"]))
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    assert entry.read_text() == before


def test_one_bar_before_the_grace_ends_it_stays_exit_2(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    protocol = past_grace(rc, monkeypatch)
    in_band_bot(rc)
    at = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    monkeypatch.setattr(_Clock, "at", (at + pd.Timedelta(days=6)).to_pydatetime())
    use(monkeypatch, FakeRunner([], fail_on="backtesting"))
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 2
    (entry,) = rc["journal"].glob("*.md")
    assert recorded_final_verdict(entry) is None
    assert "cannot compare: freqtrade backtesting exited 3" in capsys.readouterr().err


def test_freqtrade_off_path_cannot_hide_an_early_fail(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before the decision bar, with the bridge down: this used to exit 2."""
    too_many_entries(rc)
    use(monkeypatch, FakeRunner([]), found=None)
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    (entry,) = rc["journal"].glob("*.md")
    recorded = recorded_final_verdict(entry)
    assert recorded is not None and recorded["outcome"] == "fail"
    assert "cannot be judged: protected backtest:" in verdict_line(rc)
    assert "freqtrade not installed" in verdict_line(rc)


def test_freqtrade_off_path_past_the_grace_cannot_turn_a_fail_into_inconclusive(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    past_grace(rc, monkeypatch)  # the empty DB: 0 entries and 0 exposure fail on their own
    use(monkeypatch, FakeRunner([]), found=None)
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    (entry,) = rc["journal"].glob("*.md")
    assert recorded_final_verdict(entry)["outcome"] == "fail"  # type: ignore[index]


def test_a_fail_at_the_decision_bar_needs_no_grace_with_the_bridge_down(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    protocol = past_grace(rc, monkeypatch)
    at = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    monkeypatch.setattr(_Clock, "at", (at + pd.Timedelta(days=1)).to_pydatetime())
    use(monkeypatch, FakeRunner([]), found=None)
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    (entry,) = rc["journal"].glob("*.md")
    assert recorded_final_verdict(entry)["outcome"] == "fail"  # type: ignore[index]


def test_a_bridge_failure_before_the_decision_bar_with_nothing_failing_stays_exit_2(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (entry,) = rc["journal"].glob("*.md")
    before = entry.read_text()
    use(monkeypatch, FakeRunner([]), found=None)
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 2
    assert entry.read_text() == before


def test_a_multi_line_error_keeps_the_verdict_on_one_line(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """freqtrade's stderr has newlines, and the final line must stay readable: the comparison
    file it names is how a failed write is repaired."""
    past_grace(rc, monkeypatch)

    def runner(command: list[str], **kwargs: Any) -> Any:
        stderr = "x" * 3000 + "\nTraceback\n  line one\n  line two"
        return type("Done", (), {"returncode": 1, "stderr": stderr})()

    use(monkeypatch, runner)
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    (entry,) = rc["journal"].glob("*.md")
    assert recorded_final_comparison(entry) is not None
    assert len(verdict_line(rc)) < 2_000 and "line one line two" in verdict_line(rc)


def test_a_known_trip_skips_the_backtest_and_records_the_fail(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A trip followed by a failed bridge used to exit 2 for ever, hiding the fail."""
    monkeypatch.setattr(
        paper_trade.guard_module, "trip_reason", lambda *a, **k: "circuit breaker tripped: test"
    )
    runner = FakeRunner([], fail_on="backtesting")
    use(monkeypatch, runner)
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    (entry,) = rc["journal"].glob("*.md")
    recorded = recorded_final_verdict(entry)
    assert recorded is not None and recorded["outcome"] == "fail"
    assert runner.calls == []
    assert "Protections: masked entries not computed: the circuit breaker" in verdict_line(rc)


def test_a_trip_wins_over_a_bridge_failure_past_the_grace(
    rc: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    past_grace(rc, monkeypatch)
    monkeypatch.setattr(
        paper_trade.guard_module, "trip_reason", lambda *a, **k: "circuit breaker tripped: test"
    )
    use(monkeypatch, FakeRunner([], fail_on="backtesting"))
    assert paper_trade.main([*rc["base"], "compare", "--candidate", "cand"]) == 0
    (entry,) = rc["journal"].glob("*.md")
    assert recorded_final_verdict(entry)["outcome"] == "fail"  # type: ignore[index]
