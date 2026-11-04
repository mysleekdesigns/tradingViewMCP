"""The circuit breaker of a paper test (PLAN.md Phase 20 Part E), on fixture dry-run DBs.

Offline, in ``tmp_path``. No process is ever signalled: the signal sender is a recorder, and the
one CLI path that could reach ``os.kill`` runs without a ``bot.pid``.
"""

from __future__ import annotations

import ast
import json
import signal
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd
import paper_trade
import pytest

from research.paper import guard, risk
from research.paper.compare import ForwardTrade, read_dryrun_trades
from research.paper.freqtrade import launch, prepare_candidate
from research.paper.guard import (
    BOT_PID,
    EXIT_CLEAR,
    EXIT_HALTED,
    StaleCandlesError,
    equity_curve,
    guard_once,
    halt_line,
    is_this_dry_run,
    stop_bot,
)
from research.paper.protocol import (
    HALT_HEADING,
    VERDICTS_HEADING,
    append_halt,
    append_verdict,
    build_protocol,
    evaluate,
    load_protocol_config,
    load_registered_protocol,
    protocol_sha256,
    recorded_final_comparison,
    recorded_final_verdict,
    register_protocol,
)
from research.paper.risk import (
    CircuitBreaker,
    PaperConfig,
    RiskControls,
    evaluate_breaker,
    write_halt_lock,
)
from tests.test_paper_protocol import COSTS, LINK, PLAN, _Clock, observed, synthetic_design

H4 = pd.Timedelta(hours=4)
BREAKER = CircuitBreaker(max_daily_loss_pct=3.0, max_drawdown_pct=10.0, wallet=1000.0)
CONTROLS = RiskControls(protections=None, circuit_breaker=BREAKER)
CREATED = datetime(2026, 11, 2, 10, 30, tzinfo=UTC)
START = pd.Timestamp("2026-11-02 12:00", tz="UTC")  # the first bar after CREATED


def candles_of(closes: list[float], start: pd.Timestamp = START) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq="4h", name="time")
    prices = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": 1.0,
            "tradable": True,
        },
        index=index,
    )


def trade(**kw: Any) -> ForwardTrade:
    base: dict[str, Any] = {
        "pair": "BTC/USDT",
        "is_open": True,
        "open_time": START + pd.Timedelta(seconds=5),
        "open_rate": 100.0,
        "amount": 10.0,
    }
    return ForwardTrade(**{**base, **kw})


def protocol_with(controls: RiskControls | None, candidate: str = "guarded") -> Any:
    return build_protocol(
        candidate=candidate,
        plan=PLAN,
        design=synthetic_design(mean=0.001),
        costs=COSTS,
        config=load_protocol_config(),
        created_at=CREATED,
        link=LINK,
        claimed_sharpe_annual=0.5,
        claim_source="owner_supplied",
        claim_note="test claim",
        risk_controls=controls,
        paper_config=None if controls is None else PaperConfig("binance", None, 100.0, 1000.0, 1),
    )


class Test:
    """A registered test on disk with its dry-run DB."""

    __test__ = False

    def __init__(self, root: Path, controls: RiskControls | None = CONTROLS) -> None:
        self.paper, self.journal = root / "paper", root / "journal"
        self.protocol = protocol_with(controls)
        _, self.jpath = register_protocol(
            self.protocol, paper_dir=self.paper, journal_dir=self.journal
        )
        self.prepared = prepare_candidate(
            PLAN,
            candidate="guarded",
            paper_dir=self.paper,
            warm_from=self.protocol.data.warm_from,
            start=self.protocol.start,
            risk_controls=controls,
        )
        self.folder = self.prepared.dir
        self.signals: list[tuple[int, int]] = []

    def run(self, closes: list[float], trades: list[ForwardTrade], **kw: Any) -> int:
        kw.setdefault("read_cmdline", lambda pid: None)
        return guard_once(
            self.protocol,
            self.folder,
            self.jpath,
            end=kw.pop("end", START + len(closes) * H4),
            load_trades=lambda: trades,
            load_candles=lambda: candles_of(closes),
            send_signal=lambda pid, sig: self.signals.append((pid, sig)),
            out=lambda text: None,
            **kw,
        )


@pytest.fixture
def test(tmp_path: Path) -> Test:
    return Test(tmp_path)


def make_db(path: Path, rows: list[dict[str, Any]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN, "
        "open_date DATETIME, close_date DATETIME, open_rate FLOAT, close_rate FLOAT, "
        "amount FLOAT, fee_open FLOAT, fee_close FLOAT, close_profit FLOAT, exit_reason TEXT)"
    )
    for row in rows:
        names, marks = ", ".join(row), ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO trades ({names}) VALUES ({marks})", tuple(row.values()))
    conn.commit()
    conn.close()


def _date(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").tz_localize(None).strftime("%Y-%m-%d %H:%M:%S.%f")


# --------------------------------------------------------------------------- equity_curve


def test_equity_marks_a_fixture_db_by_hand(tmp_path: Path) -> None:
    """Wallet 1000. A closed trade (10 units, 100 to 110, fees 0.1%) then an open one (5 units at
    120). Every number below is worked out on paper."""
    path = tmp_path / "dryrun.sqlite"
    make_db(
        path,
        [
            {
                "pair": "BTC/USDT",
                "is_open": 0,
                "open_date": _date(START + pd.Timedelta(seconds=5)),
                "close_date": _date(START + 2 * H4 + pd.Timedelta(seconds=5)),
                "open_rate": 100.0,
                "close_rate": 110.0,
                "amount": 10.0,
                "fee_open": 0.001,
                "fee_close": 0.001,
            },
            {
                "pair": "BTC/USDT",
                "is_open": 1,
                "open_date": _date(START + 3 * H4 + pd.Timedelta(seconds=5)),
                "open_rate": 120.0,
                "amount": 5.0,
                "fee_open": 0.0,
                "fee_close": 0.0,
            },
        ],
    )
    trades = read_dryrun_trades(path, pair="BTC/USDT")
    curve = equity_curve(trades, candles_of([105, 108, 112, 125]), wallet=1000.0, interval="4h")
    assert list(curve.index) == [START - H4, *pd.date_range(START, periods=4, freq="4h")]
    realised = 10 * (110 * 0.999 - 100 * 1.001)
    assert curve.tolist() == pytest.approx(
        [
            1000.0,
            1000 + 10 * (105 - 100.1),  # open, the entry fee already paid
            1000 + 10 * (108 - 100.1),
            1000 + realised,  # closed inside this bar: both fees, no mark
            1000 + realised + 5 * (125 - 120),
        ]
    )


def test_a_loss_in_the_first_bar_is_measured_from_the_wallet() -> None:
    curve = equity_curve([trade()], candles_of([95]), wallet=1000.0, interval="4h")
    assert curve.tolist() == [1000.0, 950.0]
    assert evaluate_breaker(curve, BREAKER).reason == "daily_loss"  # type: ignore[union-attr]


def test_a_trade_closing_at_the_bar_close_is_still_marked_at_that_bar() -> None:
    """The position-at-close rule ``observe_paper`` uses: a fill at the next bar's open."""
    closed = trade(is_open=False, close_time=START + H4, close_rate=90.0)
    curve = equity_curve([closed], candles_of([95, 90]), wallet=1000.0, interval="4h")
    assert curve.tolist() == [1000.0, 950.0, 900.0]


def test_equity_ignores_a_trade_that_opens_after_the_bar() -> None:
    late = trade(open_time=START + 2 * H4)
    curve = equity_curve([late], candles_of([100, 100, 90]), wallet=1000.0, interval="4h")
    assert curve.tolist() == [1000.0, 1000.0, 1000.0, 900.0]


def test_equity_perturbation_after_bar_t_changes_nothing_up_to_t() -> None:
    closes = [100.0, 101.0, 99.0, 102.0, 98.0, 103.0]
    base = equity_curve([trade()], candles_of(closes), wallet=1000.0, interval="4h")
    changed = [*closes[:3], 1.0, 500.0, 2.0]
    after = equity_curve([trade()], candles_of(changed), wallet=1000.0, interval="4h")
    np.testing.assert_array_equal(base.iloc[:4].to_numpy(), after.iloc[:4].to_numpy())


def test_bars_without_a_candle_have_no_value() -> None:
    candles = candles_of([100, 100, 100]).drop(START + H4)
    curve = equity_curve([trade()], candles, wallet=1000.0, interval="4h")
    assert START + H4 not in curve.index and len(curve) == 3


def test_equity_refuses_what_it_cannot_value() -> None:
    with pytest.raises(ValueError, match="no amount"):
        equity_curve([trade(amount=None)], candles_of([100]), wallet=1000.0, interval="4h")
    closed = trade(is_open=False, close_time=START + pd.Timedelta(hours=1), close_rate=None)
    with pytest.raises(ValueError, match="no close_rate"):
        equity_curve([closed], candles_of([100]), wallet=1000.0, interval="4h")
    with pytest.raises(ValueError, match="wallet"):
        equity_curve([], candles_of([100]), wallet=0.0, interval="4h")


def test_equity_of_no_candles_is_empty() -> None:
    empty = pd.DataFrame(columns=["close"])
    assert equity_curve([trade()], empty, wallet=1000.0, interval="4h").empty


# --------------------------------------------------------------------------- thresholds


def test_daily_loss_trips_at_the_right_bar_and_not_before(test: Test) -> None:
    """Three flat bars on day one, then 99, 98, 97: 3% of the 1000 the day opened on."""
    closes = [100.0, 100.0, 100.0, 99.0, 98.0, 97.0, 96.0]
    assert test.run(closes[:5], [trade()]) == EXIT_CLEAR
    assert risk.read_halt_lock(test.folder) is None
    assert test.run(closes, [trade()]) == EXIT_HALTED
    lock = risk.read_halt_lock(test.folder)
    assert lock is not None
    assert lock["reason"] == "daily_loss" and lock["threshold"] == 3.0
    assert lock["value"] == pytest.approx(3.0)
    assert lock["time"] == (START + 5 * H4).isoformat()


def test_daily_loss_resets_at_the_utc_day(test: Test) -> None:
    """2% a day never reaches 3% in one UTC day, whatever the total (short of the drawdown)."""
    day = [100.0, 100.0, 100.0, 98.0, 98.0, 98.0, 98.0, 98.0, 98.0, 96.0]
    assert test.run(day, [trade()]) == EXIT_CLEAR


def test_drawdown_trips_at_the_right_bar_and_not_before(tmp_path: Path) -> None:
    only_drawdown = CircuitBreaker(max_daily_loss_pct=50.0, max_drawdown_pct=10.0, wallet=1000.0)
    test = Test(tmp_path, RiskControls(protections=None, circuit_breaker=only_drawdown))
    closes = [100.0, 102.0, 104.0, 100.0, 96.0, 93.6]  # peak 1040; 1040 x 0.9 = 936
    assert test.run(closes[:5], [trade()]) == EXIT_CLEAR
    assert test.run(closes, [trade()]) == EXIT_HALTED
    lock = risk.read_halt_lock(test.folder)
    assert lock is not None and lock["reason"] == "drawdown"
    assert lock["value"] == pytest.approx(10.0) and lock["time"] == (START + 5 * H4).isoformat()


def test_no_closed_bar_yet_is_clear(test: Test) -> None:
    assert test.run([], [trade()]) == EXIT_CLEAR
    assert risk.read_halt_lock(test.folder) is None


# --------------------------------------------------------------------------- on a trip


def test_the_trip_order_is_lock_then_stop_then_journal(
    test: Test, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, bool]] = []
    real_stop, real_append = guard.stop_bot, guard.append_halt

    def stop(*a: Any, **k: Any) -> Any:
        seen.append(("stop", (test.folder / "halt.lock").exists()))
        return real_stop(*a, **k)

    def journal(path: Path, line: str) -> bool:
        seen.append(("journal", HALT_HEADING not in test.jpath.read_text()))
        return real_append(path, line)

    monkeypatch.setattr(guard, "stop_bot", stop)
    monkeypatch.setattr(guard, "append_halt", journal)
    assert test.run([100.0, 95.0], [trade()]) == EXIT_HALTED
    assert seen == [("stop", True), ("journal", True)]


def test_a_second_run_leaves_the_lock_byte_identical(test: Test) -> None:
    assert test.run([100.0, 95.0], [trade()]) == EXIT_HALTED
    lock = (test.folder / "halt.lock").read_bytes()
    journal = test.jpath.read_bytes()
    assert test.run([100.0, 90.0], [trade()]) == EXIT_HALTED  # other prices: the first trip stands
    assert (test.folder / "halt.lock").read_bytes() == lock
    assert test.jpath.read_bytes() == journal  # the same halt is logged once


def test_a_crash_after_the_lock_is_finished_by_the_next_run(test: Test) -> None:
    """The lock alone is enough: the next run stops the bot and writes the journal line."""
    write_halt_lock(
        test.folder, risk.BreakerTrip("drawdown", 10.0, 11.0, "2026-11-03T00:00:00+00:00")
    )
    assert test.run([100.0], []) == EXIT_HALTED
    assert "drawdown 11.0%" in test.jpath.read_text()


@pytest.mark.parametrize("content", [b"not json", b"", b"\xff\xfe", b"[1, 2]"])
def test_an_unreadable_lock_is_still_a_lock(test: Test, content: bytes) -> None:
    """It stops the bot and journals the halt, once, and is never replaced."""
    (test.folder / "halt.lock").write_bytes(content)
    (test.folder / BOT_PID).write_text("4242\n")
    words = dry_run_words(test.folder)
    assert test.run([100.0], [], read_cmdline=lambda pid: words) == EXIT_HALTED
    assert test.signals == [(4242, signal.SIGTERM)]
    assert "`halt.lock` exists but cannot be read" in test.jpath.read_text()
    journal = test.jpath.read_bytes()
    assert test.run([100.0], [], read_cmdline=lambda pid: words) == EXIT_HALTED
    assert test.jpath.read_bytes() == journal
    assert (test.folder / "halt.lock").read_bytes() == content


def test_the_lock_blocks_every_prepare_path_and_launch(tmp_path: Path) -> None:
    test = Test(tmp_path)
    assert test.run([100.0, 95.0], [trade()]) == EXIT_HALTED
    with pytest.raises(ValueError, match="only the owner removes"):
        risk.check_not_halted(test.folder)
    started: list[Any] = []
    with pytest.raises(ValueError, match="only the owner removes"):
        launch(test.prepared, runner=lambda *a, **k: started.append(a), which=lambda _: "/x")
    assert started == []


def test_the_cli_refuses_prepare_on_a_halted_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    test = Test(tmp_path)
    assert test.run([100.0, 95.0], [trade()]) == EXIT_HALTED
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN))
    argv = ["--paper-dir", str(test.paper), "--journal-dir", str(test.journal), "prepare"]
    argv += ["--plan-file", str(plan_file), "--candidate", "guarded"]
    for extra in ([], ["--launch"]):
        assert paper_trade.main([*argv, *extra]) == 2
        assert "paper test is halted" in capsys.readouterr().err


def test_no_code_path_deletes_the_lock() -> None:
    """Static: guard and risk never unlink, rename or rmtree. Only the owner deletes the file."""
    for module in (guard, risk):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        calls = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert not calls & {"unlink", "remove", "rename", "replace", "rmtree", "rmdir"}


# --------------------------------------------------------------------------- stopping the bot


CONFIG = "/paper/guarded/config.json"


@pytest.mark.parametrize(
    "words",
    [
        ["freqtrade", "trade", "--dry-run", "--config", CONFIG],
        ["/venv/bin/python", "/venv/bin/freqtrade", "trade", "--dry-run", "-c", CONFIG],
    ],
)
def test_the_bot_is_recognised(words: list[str]) -> None:
    assert is_this_dry_run(words, Path(CONFIG))


@pytest.mark.parametrize(
    "words",
    [
        None,
        [],
        ["python", "server.py"],
        ["freqtrade", "trade", "--config", CONFIG],  # live: never ours
        ["freqtrade", "backtesting", "--dry-run", "--config", CONFIG],
        ["freqtrade", "trade", "--dry-run", "--config", "/paper/other/config.json"],
        ["freqtrade", "trade", "--dry-run"],
        ["freqtrade", "trade", "--dry-run", "--config", CONFIG, "--config", "/x.json"],
        ["vi", "freqtrade", "--dry-run", "--config", CONFIG],
    ],
)
def test_anything_else_is_not_the_bot(words: list[str] | None) -> None:
    assert not is_this_dry_run(words, Path(CONFIG))


def dry_run_words(folder: Path) -> list[str]:
    return ["freqtrade", "trade", "--dry-run", "--config", str(folder / "config.json")]


def test_a_verified_bot_gets_sigterm(test: Test) -> None:
    (test.folder / BOT_PID).write_text("4242\n")
    words = dry_run_words(test.folder)
    assert test.run([100.0, 95.0], [trade()], read_cmdline=lambda pid: words) == EXIT_HALTED
    assert test.signals == [(4242, signal.SIGTERM)]


def test_a_wrong_process_is_never_signalled(test: Test) -> None:
    """Pid reuse: the file names a pid that now belongs to something else."""
    (test.folder / BOT_PID).write_text("4242\n")
    other = ["python", "something-else.py"]
    assert test.run([100.0, 95.0], [trade()], read_cmdline=lambda pid: other) == EXIT_HALTED
    assert test.signals == []
    assert (test.folder / "halt.lock").is_file()


@pytest.mark.parametrize("content", [None, "", "not a pid", "1", "0", "-5"])
def test_an_unusable_pid_file_is_never_signalled(test: Test, content: str | None) -> None:
    if content is not None:
        (test.folder / BOT_PID).write_text(content)
    result = stop_bot(
        test.folder,
        read_cmdline=lambda pid: dry_run_words(test.folder),
        send_signal=lambda pid, sig: test.signals.append((pid, sig)),
    )
    assert not result.signalled and "by hand" in result.message
    assert test.signals == []


def test_a_pid_with_no_process_is_named_as_gone_not_as_no_bot(test: Test) -> None:
    """A stale ``bot.pid`` beside a bot started by hand must not read as "not running"."""
    (test.folder / BOT_PID).write_text("4242\n")
    result = stop_bot(
        test.folder,
        read_cmdline=lambda pid: [],
        send_signal=lambda pid, sig: test.signals.append((pid, sig)),
    )
    assert not result.signalled and test.signals == []
    assert "pid 4242 from bot.pid is gone" in result.message
    assert "a bot started by hand would not show here" in result.message
    assert "not running" not in result.message and "nothing needs" not in result.message


def test_ps_failing_is_not_read_as_the_bot_being_gone(test: Test) -> None:
    (test.folder / BOT_PID).write_text("4242\n")
    result = stop_bot(test.folder, read_cmdline=lambda pid: None, send_signal=lambda *a: None)
    assert "not verified, no signal" in result.message
    assert "not running" not in result.message and "nothing needs" not in result.message


def test_a_live_other_process_stays_unverified(test: Test) -> None:
    (test.folder / BOT_PID).write_text("4242\n")
    result = stop_bot(
        test.folder,
        read_cmdline=lambda pid: ["/usr/bin/vim", "notes.md"],
        send_signal=lambda pid, sig: test.signals.append((pid, sig)),
    )
    assert "not verified, no signal" in result.message and "by hand" in result.message
    assert "not running" not in result.message and test.signals == []


@pytest.mark.parametrize(
    ("code", "out", "err", "words"),
    [
        (0, "freqtrade trade --dry-run\n", "", ["freqtrade", "trade", "--dry-run"]),
        (1, "", "", []),  # ps ran and found no such process
        (1, "", "ps: process id too large: 999999\n", None),  # ps complaining is not an answer
        (2, "", "", None),
        (0, "", "", None),  # nothing to show for a clean exit: not an answer either
    ],
)
def test_ps_output_tells_a_gone_process_from_a_failed_ps(
    monkeypatch: pytest.MonkeyPatch, code: int, out: str, err: str, words: list[str] | None
) -> None:
    done = type("Done", (), {"returncode": code, "stdout": out, "stderr": err})()
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: done)
    assert guard.read_process_cmdline(4242) == words


def test_a_ps_that_cannot_run_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sandbox refuses to exec ps at all."""

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(guard.subprocess, "run", denied)
    assert guard.read_process_cmdline(4242) is None


def test_unverifiable_prints_the_owners_manual_step(test: Test) -> None:
    (test.folder / BOT_PID).write_text("4242\n")
    result = stop_bot(test.folder, read_cmdline=lambda pid: None, send_signal=lambda *a: None)
    assert not result.signalled
    assert "stop the freqtrade dry-run for guarded by hand" in result.message
    assert "Entries stay blocked" in result.message


def test_a_bot_that_already_exited_is_fine(test: Test) -> None:
    (test.folder / BOT_PID).write_text("4242\n")

    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    result = stop_bot(
        test.folder, read_cmdline=lambda pid: dry_run_words(test.folder), send_signal=gone
    )
    assert not result.signalled and "already exited" in result.message


def test_a_permission_error_keeps_the_manual_step(test: Test) -> None:
    (test.folder / BOT_PID).write_text("4242\n")

    def denied(pid: int, sig: int) -> None:
        raise PermissionError("no")

    result = stop_bot(
        test.folder, read_cmdline=lambda pid: dry_run_words(test.folder), send_signal=denied
    )
    assert not result.signalled and "by hand" in result.message


# --------------------------------------------------------------------------- journal


def _sections(text: str) -> tuple[str, str]:
    head, _, tail = text.partition(VERDICTS_HEADING)
    return head, tail


def test_the_halt_line_sits_under_its_own_heading_before_the_verdicts(test: Test) -> None:
    before = test.jpath.read_text()
    assert test.run([100.0, 95.0], [trade()]) == EXIT_HALTED
    text = test.jpath.read_text()
    head, tail = _sections(text)
    assert HALT_HEADING in head and tail == _sections(before)[1]
    assert head.startswith(_sections(before)[0])  # only added, nothing registered was edited


def test_a_halt_leaves_the_sha_and_later_verdicts_intact(test: Test) -> None:
    p = test.protocol
    verdict = evaluate(p, observed(p, asof=p.start + 10 * 86400, entries=1))
    append_verdict(test.jpath, verdict, asof=p.start + 10 * 86400)
    assert test.run([100.0, 95.0], [trade()]) == EXIT_HALTED
    protocol, _ = load_registered_protocol(test.paper, "guarded", test.journal)
    assert protocol_sha256(protocol) == protocol_sha256(p)

    final = evaluate(p, observed(p))
    assert final.final
    name = "comparison-2026-12-25.json"
    append_verdict(test.jpath, final, asof=p.decision_end, comparison=name)
    assert recorded_final_verdict(test.jpath)["outcome"] == final.verdict  # type: ignore[index]
    assert recorded_final_comparison(test.jpath) == name
    assert _sections(test.jpath.read_text())[1].count("\n- ") == 2


def test_a_halt_after_a_final_verdict_changes_no_parsing(test: Test) -> None:
    p = test.protocol
    final = evaluate(p, observed(p))
    name = "comparison-2026-12-25.json"
    append_verdict(test.jpath, final, asof=p.decision_end, comparison=name)
    recorded = recorded_final_verdict(test.jpath)
    tail = _sections(test.jpath.read_text())[1]
    assert test.run([100.0, 95.0], [trade()]) == EXIT_HALTED
    assert recorded_final_verdict(test.jpath) == recorded
    assert recorded_final_comparison(test.jpath) == name
    assert _sections(test.jpath.read_text())[1] == tail


def test_two_different_halts_share_one_heading(test: Test) -> None:
    append_halt(test.jpath, "- first")
    assert append_halt(test.jpath, "- second") is True
    assert append_halt(test.jpath, "- second") is False
    head = _sections(test.jpath.read_text())[0]
    assert head.count(HALT_HEADING) == 1
    assert head.index("- first") < head.index("- second")


def test_the_halt_line_comes_only_from_the_lock() -> None:
    lock = {"reason": "drawdown", "value": 11.0, "threshold": 10.0, "time": "T"}
    assert halt_line(lock) == halt_line(dict(lock))
    assert "only the owner removes it" in halt_line(lock)


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def cli(test: Test, monkeypatch: pytest.MonkeyPatch) -> Test:
    frame = candles_of([100.0, 100.0, 95.0])
    monkeypatch.setattr(paper_trade, "public_kline_loader", lambda since, host: lambda *a: frame)
    monkeypatch.setattr(paper_trade, "datetime", _Clock)
    monkeypatch.setattr(_Clock, "at", datetime(2026, 11, 3, 1, 0, tzinfo=UTC))
    return test


def guard_argv(test: Test) -> list[str]:
    return ["--paper-dir", str(test.paper), "--journal-dir", str(test.journal), "guard"]


def test_cli_guard_halts_on_a_fixture_db(cli: Test, capsys: pytest.CaptureFixture[str]) -> None:
    make_db(
        cli.folder / "dryrun.sqlite",
        [
            {
                "pair": "BTC/USDT",
                "is_open": 1,
                "open_date": _date(START + pd.Timedelta(seconds=5)),
                "open_rate": 100.0,
                "amount": 10.0,
                "fee_open": 0.0,
            }
        ],
    )
    assert paper_trade.main([*guard_argv(cli), "--candidate", "guarded"]) == EXIT_HALTED
    out = capsys.readouterr().out
    assert "HALTED guarded" in out and "no usable bot.pid" in out
    assert (cli.folder / "halt.lock").is_file() and HALT_HEADING in cli.jpath.read_text()


def test_cli_guard_is_clear_without_a_trip(cli: Test, capsys: pytest.CaptureFixture[str]) -> None:
    make_db(cli.folder / "dryrun.sqlite", [])
    assert paper_trade.main([*guard_argv(cli), "--candidate", "guarded"]) == EXIT_CLEAR
    assert "clear through bar" in capsys.readouterr().out
    assert not (cli.folder / "halt.lock").exists()


def test_cli_guard_exit_2_when_it_cannot_check(
    cli: Test, capsys: pytest.CaptureFixture[str]
) -> None:
    assert paper_trade.main([*guard_argv(cli), "--candidate", "guarded"]) == 2  # no dry-run DB
    assert "cannot guard" in capsys.readouterr().err
    assert not (cli.folder / "halt.lock").exists()


def test_cli_guard_refuses_a_protocol_without_a_breaker_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    test = Test(tmp_path, None)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert paper_trade.main([*guard_argv(test), "--candidate", "guarded"]) == 2
    assert "no pre-registered circuit breaker" in capsys.readouterr().err
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_guard_once_refuses_a_protocol_without_a_breaker(tmp_path: Path) -> None:
    test = Test(tmp_path, None)
    with pytest.raises(ValueError, match="no pre-registered circuit breaker"):
        test.run([100.0], [])


def test_cli_guard_refuses_a_config_that_is_not_dry_run(
    cli: Test, capsys: pytest.CaptureFixture[str]
) -> None:
    """No live mode is reachable: the guard reads nothing from a config that is not dry-run."""
    make_db(cli.folder / "dryrun.sqlite", [])
    path = cli.folder / "config.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "dry_run": False}))
    assert paper_trade.main([*guard_argv(cli), "--candidate", "guarded"]) == 2
    assert "dry_run" in capsys.readouterr().err


def test_bot_pid_is_written_only_for_a_protocol_with_a_breaker(tmp_path: Path) -> None:
    class Process:
        pid = 777

    guarded = Test(tmp_path / "a")
    launch(
        guarded.prepared,
        runner=lambda *a, **k: Process(),
        which=lambda _: "/x",
        pid_file=guarded.folder / BOT_PID,
    )
    assert (guarded.folder / BOT_PID).read_text() == "777\n"
    plain = Test(tmp_path / "b", None)
    launch(plain.prepared, runner=lambda *a, **k: Process(), which=lambda _: "/x")
    assert not (plain.folder / BOT_PID).exists()


@pytest.mark.parametrize(("controls", "with_pid"), [(CONTROLS, True), (None, False)])
def test_cli_launch_asks_for_a_pid_file_only_with_a_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, controls: Any, with_pid: bool
) -> None:
    test = Test(tmp_path, controls)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN))
    seen: list[dict[str, Any]] = []

    def fake(prepared: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return type("R", (), {"started": True, "message": "ok"})()

    monkeypatch.setattr(paper_trade, "launch", fake)
    argv = ["--paper-dir", str(test.paper), "--journal-dir", str(test.journal), "prepare"]
    argv += ["--plan-file", str(plan_file), "--candidate", "guarded", "--launch"]
    assert paper_trade.main(argv) == 0
    assert ("pid_file" in seen[0]) is with_pid


# --------------------------------------------------------------------------- watch


def test_watch_checks_after_each_bar_until_it_halts() -> None:
    codes = iter([EXIT_CLEAR, EXIT_CLEAR, EXIT_HALTED])
    sleeps: list[float] = []
    step = 4 * 3600
    code = guard.watch(
        lambda: next(codes),
        interval="4h",
        now=lambda: 100 * step + 60.0,
        sleep=sleeps.append,
        out=lambda text: None,
    )
    assert code == EXIT_HALTED
    assert sleeps == [step - 60.0 + guard.SETTLE_SECONDS] * 2


def _response(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://data-api.binance.vision/api/v3/klines")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


@pytest.mark.parametrize(
    "failure",
    [
        sqlite3.OperationalError("database is locked"),
        httpx.ConnectError("no route"),
        httpx.ReadTimeout("slow"),
        _response(403),
        _response(418),
        _response(429),
        _response(503),
        ConnectionResetError("reset"),
        StaleCandlesError("the last bar has not reached the host"),
    ],
)
def test_watch_outlives_a_blip(failure: Exception) -> None:
    errors: list[str] = []

    def check() -> int:
        raise failure

    code = guard.watch(
        check,
        interval="4h",
        now=lambda: 0.0,
        sleep=lambda s: None,
        out=lambda text: None,
        err=errors.append,
        max_checks=2,
    )
    assert code == EXIT_CLEAR and len(errors) == 2


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("protocol changed after registration"),
        KeyError("close"),
        PermissionError("config.json"),
        FileNotFoundError("dryrun.sqlite"),
        sqlite3.OperationalError("no such table: trades"),
        sqlite3.DatabaseError("file is not a database"),
        _response(401),
        _response(404),
    ],
)
def test_watch_stops_on_a_permanent_error(failure: Exception) -> None:
    """A watcher that retries forever looks alive and guards nothing."""
    calls: list[int] = []

    def check() -> int:
        calls.append(1)
        raise failure

    with pytest.raises(type(failure)):
        guard.watch(
            check,
            interval="4h",
            now=lambda: 0.0,
            sleep=lambda s: None,
            out=lambda text: None,
            err=lambda text: None,
            max_checks=5,
        )
    assert calls == [1]


def _late(*, newest_only: bool) -> StaleCandlesError:
    return StaleCandlesError("late", newest_only=newest_only)


def _watch_late(failures: list[Exception], sleeps: list[float], checks: int = 2) -> None:
    def check() -> int:
        if failures:
            raise failures.pop(0)
        return EXIT_CLEAR

    guard.watch(
        check,
        interval="4h",
        now=lambda: 0.0,
        sleep=sleeps.append,
        out=lambda text: None,
        err=lambda text: None,
        max_checks=checks,
    )


def test_a_late_newest_bar_is_retried_within_the_bar() -> None:
    sleeps: list[float] = []
    _watch_late([_late(newest_only=True), _late(newest_only=True)], sleeps, checks=1)
    assert sleeps == [guard.STALE_RETRY_DELAY_S] * 2  # then clear: no whole bar lost


def test_the_quick_retries_are_bounded_then_the_next_bar_takes_over() -> None:
    sleeps: list[float] = []
    failures: list[Exception] = [_late(newest_only=True)] * (guard.STALE_RETRIES + 1)
    _watch_late(failures, sleeps)
    wait = guard.STALE_RETRY_DELAY_S
    assert sleeps == [wait] * guard.STALE_RETRIES + [4 * 3600 + guard.SETTLE_SECONDS]


def test_the_quick_retries_count_again_after_a_good_check() -> None:
    codes: list[Any] = [_late(newest_only=True)] * guard.STALE_RETRIES + [EXIT_CLEAR]
    codes = codes + codes + [EXIT_HALTED]
    sleeps: list[float] = []

    def check() -> int:
        code = codes.pop(0)
        if isinstance(code, Exception):
            raise code
        return int(code)

    guard.watch(check, interval="4h", now=lambda: 0.0, sleep=sleeps.append, out=str, err=str)
    assert sleeps.count(guard.STALE_RETRY_DELAY_S) == 2 * guard.STALE_RETRIES


def test_an_older_gap_gets_no_quick_retry() -> None:
    sleeps: list[float] = []
    _watch_late([_late(newest_only=False)], sleeps)
    assert sleeps == [4 * 3600 + guard.SETTLE_SECONDS]


@pytest.mark.parametrize(("bars_there", "newest_only"), [(2, True), (1, False)])
def test_stale_candles_say_whether_only_the_newest_bar_is_late(
    test: Test, bars_there: int, newest_only: bool
) -> None:
    closes = [100.0] * bars_there
    with pytest.raises(StaleCandlesError) as caught:
        test.run(closes, [], end=START + 3 * H4)
    assert caught.value.newest_only is newest_only


@pytest.mark.parametrize(("bars_closed", "newest_only"), [(1, True), (2, False)])
def test_with_no_candles_only_the_tests_first_bar_counts_as_the_newest(
    test: Test, bars_closed: int, newest_only: bool
) -> None:
    empty = candles_of([100.0]).iloc[:0]
    with pytest.raises(StaleCandlesError) as caught:
        guard_once(
            test.protocol,
            test.folder,
            test.jpath,
            end=START + bars_closed * H4,
            load_trades=lambda: [],
            load_candles=lambda: empty,
        )
    assert caught.value.newest_only is newest_only


def test_watch_stops_on_a_403_that_never_lifts() -> None:
    errors: list[str] = []
    calls: list[int] = []

    def check() -> int:
        calls.append(1)
        raise _response(403)

    with pytest.raises(httpx.HTTPStatusError):
        guard.watch(
            check,
            interval="4h",
            now=lambda: 0.0,
            sleep=lambda s: None,
            out=lambda text: None,
            err=errors.append,
            max_checks=guard.MAX_CONSECUTIVE_403 * 3,
        )
    assert len(calls) == guard.MAX_CONSECUTIVE_403
    assert errors[-1].startswith(f"HTTP 403 on {guard.MAX_CONSECUTIVE_403} checks in a row")


@pytest.mark.parametrize("between", [None, _response(429), httpx.ConnectError("no route")])
def test_only_consecutive_403s_count(between: Exception | None) -> None:
    """One short of the cap, then anything else: the count starts again."""
    script: list[Exception | None] = [_response(403)] * (guard.MAX_CONSECUTIVE_403 - 1)
    script = [*script, between, *script]

    def check() -> int:
        step = script.pop(0)
        if step is not None:
            raise step
        return EXIT_CLEAR

    code = guard.watch(
        check,
        interval="4h",
        now=lambda: 0.0,
        sleep=lambda s: None,
        out=lambda text: None,
        err=lambda text: None,
        max_checks=2 * guard.MAX_CONSECUTIVE_403 - 1,
    )
    assert code == EXIT_CLEAR and script == []


def test_cli_watch_exits_2_on_a_403_that_never_lifts(
    cli: Test, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refused(*a: Any, **k: Any) -> Any:
        raise _response(403)

    monkeypatch.setattr(paper_trade, "_closed_candles", refused)
    monkeypatch.setattr(paper_trade.time, "sleep", lambda s: None)
    code = paper_trade.main([*guard_argv(cli), "--candidate", "guarded", "--watch"])
    assert code == guard.EXIT_ERROR
    assert "cannot guard" in capsys.readouterr().err


def test_cli_watch_exits_2_on_a_permanent_error(
    cli: Test, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps: list[float] = []
    time_sleep = paper_trade.time.sleep
    paper_trade.time.sleep = sleeps.append  # type: ignore[assignment]
    try:
        code = paper_trade.main([*guard_argv(cli), "--candidate", "guarded", "--watch"])
    finally:
        paper_trade.time.sleep = time_sleep  # type: ignore[assignment]
    assert code == 2 and sleeps == []  # no dry-run DB table: it never slept and retried
    assert "cannot guard" in capsys.readouterr().err


# --------------------------------------------------------------------------- stale candles


def test_candles_that_stop_early_are_not_clear(test: Test) -> None:
    """A bar that has not arrived may hold the loss: never read it as clear."""
    stale = START + 5 * H4  # the last closed bar is START + 4h x 4, the candles stop at + 4h x 1
    with pytest.raises(StaleCandlesError, match="cannot say the test is clear"):
        test.run([100.0, 100.0], [trade()], end=stale)
    assert not (test.folder / "halt.lock").exists()


def test_the_last_closed_bar_is_fresh_enough(test: Test) -> None:
    assert test.run([100.0, 100.0], [], end=START + 2 * H4) == EXIT_CLEAR


def test_no_candle_after_the_start_is_not_clear(test: Test) -> None:
    with pytest.raises(StaleCandlesError, match="stop at None"):
        test.run([], [], end=START + H4)


def test_no_candle_before_the_start_is_nothing_to_check(test: Test) -> None:
    assert test.run([], [], end=START) == EXIT_CLEAR


def test_a_trip_in_late_candles_still_halts(test: Test) -> None:
    """Late bars cannot undo a loss already seen: staleness only stops a clear."""
    assert test.run([100.0, 95.0], [trade()], end=START + 9 * H4) == EXIT_HALTED
    assert (test.folder / "halt.lock").is_file()


def test_a_lock_outranks_late_candles(test: Test) -> None:
    write_halt_lock(test.folder, risk.BreakerTrip("drawdown", 10.0, 11.0, "T"))
    assert test.run([100.0], [], end=START + 9 * H4) == EXIT_HALTED


def test_cli_guard_exit_2_on_stale_candles(
    cli: Test, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_db(cli.folder / "dryrun.sqlite", [])
    monkeypatch.setattr(_Clock, "at", datetime(2026, 11, 4, 1, 0, tzinfo=UTC))
    assert paper_trade.main([*guard_argv(cli), "--candidate", "guarded"]) == 2
    assert "cannot say the test is clear" in capsys.readouterr().err
    assert not (cli.folder / "halt.lock").exists()


# --------------------------------------------------------------------------- the frozen wallet


def _losing_db(test: Test) -> None:
    make_db(
        test.folder / "dryrun.sqlite",
        [
            {
                "pair": "BTC/USDT",
                "is_open": 1,
                "open_date": _date(START + pd.Timedelta(seconds=5)),
                "open_rate": 100.0,
                "amount": 10.0,
                "fee_open": 0.0,
            }
        ],
    )


@pytest.mark.parametrize(
    "edit", [{"dry_run_wallet": 1_000_000.0}, {"fee": 0.01}, {"stake_amount": 500.0}]
)
def test_guard_refuses_a_config_that_is_not_the_frozen_one(
    cli: Test, capsys: pytest.CaptureFixture[str], edit: dict[str, Any]
) -> None:
    path = cli.folder / "config.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **edit}))
    _losing_db(cli)
    capsys.readouterr()
    assert paper_trade.main([*guard_argv(cli), "--candidate", "guarded"]) == guard.EXIT_ERROR
    assert "differs from the frozen config" in capsys.readouterr().err
    assert not (cli.folder / "halt.lock").exists()


def test_an_edit_during_a_watch_ends_it(cli: Test, capsys: pytest.CaptureFixture[str]) -> None:
    """The check runs each bar, not once: a config edited mid-test must not read as guarded."""
    path = cli.folder / "config.json"
    original = path.read_text()
    sleeps: list[float] = []

    def edit_after_first_bar(seconds: float) -> None:
        sleeps.append(seconds)
        assert len(sleeps) == 1, "the watch carried on past the edit"
        path.write_text(json.dumps({**json.loads(original), "fee": 0.01}))

    time_sleep = paper_trade.time.sleep
    paper_trade.time.sleep = edit_after_first_bar  # type: ignore[assignment]
    try:
        make_db(cli.folder / "dryrun.sqlite", [])
        code = paper_trade.main([*guard_argv(cli), "--candidate", "guarded", "--watch"])
    finally:
        paper_trade.time.sleep = time_sleep
    assert code == guard.EXIT_ERROR and len(sleeps) == 1
    assert "differs from the frozen config" in capsys.readouterr().err


def test_a_halted_folder_gets_its_stop_whatever_the_config_says(
    cli: Test, capsys: pytest.CaptureFixture[str]
) -> None:
    path = cli.folder / "config.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "fee": 0.01}))
    write_halt_lock(
        cli.folder, risk.BreakerTrip("drawdown", 10.0, 11.0, "2026-11-02T12:00:00+00:00")
    )
    assert paper_trade.main([*guard_argv(cli), "--candidate", "guarded"]) == EXIT_HALTED
    assert "already halted" in capsys.readouterr().out


# --------------------------------------------------------------------------- compare's own check


def test_trip_reason_sees_a_loss_nobody_guarded(test: Test) -> None:
    reason = guard.trip_reason(
        test.protocol, test.folder, [trade()], candles_of([100.0, 95.0]), end=START + 2 * H4
    )
    assert reason is not None and reason.startswith("circuit breaker tripped: daily_loss 5.00%")
    assert not (test.folder / "halt.lock").exists()  # compare only reads


def test_trip_reason_is_none_without_a_trip_or_a_breaker(tmp_path: Path) -> None:
    guarded = Test(tmp_path / "a")
    args = ([trade()], candles_of([100.0, 99.9]))
    assert guard.trip_reason(guarded.protocol, guarded.folder, *args, end=START + 2 * H4) is None
    plain = Test(tmp_path / "b", None)
    assert guard.trip_reason(plain.protocol, plain.folder, *args, end=START + 2 * H4) is None


def test_trip_reason_counts_a_recorded_lock(test: Test) -> None:
    """Revised candles may not reproduce a trip, but the test did stop."""
    at = (START + H4).isoformat()
    write_halt_lock(test.folder, risk.BreakerTrip("drawdown", 10.0, 11.0, at))
    candles = candles_of([100.0, 100.0])
    reason = guard.trip_reason(test.protocol, test.folder, [], candles, end=START + 2 * H4)
    assert reason is not None and f"records drawdown at {at}" in reason


def test_a_lock_after_the_judged_period_does_not_count(test: Test) -> None:
    """A watcher left running past the decision bar cannot rewrite a finished test."""
    at = (START + 9 * H4).isoformat()
    write_halt_lock(test.folder, risk.BreakerTrip("drawdown", 10.0, 11.0, at))
    candles = candles_of([100.0, 100.0])
    assert guard.trip_reason(test.protocol, test.folder, [], candles, end=START + 2 * H4) is None


@pytest.mark.parametrize("content", ["not json", '{"reason": "drawdown", "time": "soon"}'])
def test_an_unreadable_lock_counts_at_compare(test: Test, content: str) -> None:
    (test.folder / "halt.lock").write_text(content)
    candles = candles_of([100.0, 100.0])
    reason = guard.trip_reason(test.protocol, test.folder, [], candles, end=START + 2 * H4)
    assert reason is not None and reason.startswith("circuit breaker tripped:")


def test_a_tripped_test_is_a_final_fail_with_its_own_reason(test: Test) -> None:
    p = test.protocol
    why = "circuit breaker tripped: drawdown 12.00% at or beyond 10% at bar T"
    verdict = evaluate(p, replace(observed(p, asof=p.start + 86400, entries=1), breaker_trip=why))
    assert verdict.verdict == "fail" and verdict.final
    assert verdict.fail_kinds == ("circuit_breaker",)
    assert verdict.reasons == (why,)
    assert verdict.headline.startswith(f"fail: {why}")


def test_a_trip_outranks_every_timing_label_at_the_decision_bar(test: Test) -> None:
    """A halted bot misses the replay's later trades: that is the halt, not an irregular fill."""
    p = test.protocol
    why = "circuit breaker tripped: drawdown 12.00% at or beyond 10% at bar T"
    late = replace(observed(p), breaker_trip=why, lab_entries=40, matched_entries=0)
    verdict = evaluate(p, late)
    assert verdict.verdict == "fail" and verdict.fail_kinds == ("circuit_breaker",)
    assert "irregular" not in verdict.headline and "timing" not in verdict.headline
    assert any(c.name == "irregular_fills" and c.status == "fail" for c in verdict.checks)


def test_a_passing_test_with_a_trip_can_never_pass(test: Test) -> None:
    p = test.protocol
    clean = evaluate(p, observed(p))
    assert clean.verdict != "fail"
    tripped = evaluate(p, replace(observed(p), breaker_trip="circuit breaker tripped: x"))
    assert tripped.verdict == "fail"
