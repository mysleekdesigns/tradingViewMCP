"""The circuit breaker of a paper test (PLAN.md Phase 20 Part E): ``paper_trade.py guard``.

For a protocol with a frozen ``circuit_breaker`` only (the HC tests have none and are refused
without a write). Each check builds marked equity at every closed bar (:func:`equity_curve`) and
asks :func:`research.paper.risk.evaluate_breaker` for the first bar at or beyond a threshold. On a
trip, in this order:

1. ``write_halt_lock``: ``prepare`` and ``launch`` refuse from then on, and the strategy's
   ``confirm_trade_entry`` blocks entries even for a bot restarted by hand;
2. stop the process (:func:`stop_bot`): SIGTERM only after the pid's command line proves it is
   ``freqtrade trade --dry-run`` on this folder's config. A pid that cannot be verified is never
   signalled: the lock stays and the owner's manual step is printed. A pid whose process no longer
   exists is named as gone (a bot started by hand has another pid), and ``ps`` failing (the
   sandbox, say) is not read as that;
3. append the halt line to the test's journal, under its own heading (``append_halt``).

A later run on a locked folder repeats steps 2 and 3 from the lock (a crash between steps loses
nothing) and leaves the lock byte-identical; a lock it cannot read is still a lock. No code path
deletes ``halt.lock``: only the owner removes it, outside Claude Code. This module reads a dry-run
DB and places nothing.

The breaker acts only while ``guard --watch`` runs (``--launch`` does not start it). ``compare``
applies the same thresholds afterwards through :func:`trip_reason`, so a test nobody guarded still
fails, but a bot nobody stopped went on trading until then. The wallet is the breaker's frozen one.
"""

from __future__ import annotations

import os
import shlex
import signal
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from lab.types import interval_seconds
from research.paper.compare import ForwardTrade
from research.paper.protocol import PaperProtocol, append_halt
from research.paper.risk import (
    HALT_LOCK,
    UNREADABLE_LOCK_REASON,
    evaluate_breaker,
    read_halt_lock,
    write_halt_lock,
)

BOT_PID = "bot.pid"
EXIT_CLEAR = 0  # checked, nothing tripped
EXIT_HALTED = 1  # the test is halted (this run or an earlier one)
EXIT_ERROR = 2  # refused or could not check
SETTLE_SECONDS = 20.0  # after a bar boundary, so the closed candle has reached the public host
PS_TIMEOUT_S = 10.0
# A closed bar normally reaches the public host within SETTLE_SECONDS. Three more tries, a minute
# in all, save the rest of the bar under --watch; a longer outage waits for the next bar as before.
STALE_RETRIES = 3
STALE_RETRY_DELAY_S = 20.0
# A 403 may lift, but it is also what a host that refuses this address answers for ever. A dozen
# checks in a row (two days on 4h bars) is no blip: a watcher that goes on guards nothing.
MAX_CONSECUTIVE_403 = 12

# The process's words, [] when no such process exists, None when that could not be found out.
CmdlineReader = Callable[[int], list[str] | None]
SignalSender = Callable[[int, int], None]
Out = Callable[[str], None]


class StaleCandlesError(ValueError):
    """The candles stop before the last closed bar: nothing can be said about the bars since.
    ``newest_only`` says that bar is the only one missing, the usual moment after a bar closes."""

    def __init__(self, message: str, *, newest_only: bool = False) -> None:
        super().__init__(message)
        self.newest_only = newest_only


def refuse_reason(protocol: PaperProtocol) -> str | None:
    """Why ``guard`` must not touch this test, or None."""
    controls = protocol.risk_controls
    if controls is None or controls.circuit_breaker is None:
        return (
            f"{protocol.candidate} has no pre-registered circuit breaker: nothing to guard, and "
            "a registered protocol is never changed. Nothing was written"
        )
    return None


def _trade_pnl(trade: ForwardTrade, price: float, *, closing: bool) -> float:
    """Net P&L of ``trade`` at ``price``: both fees when it closes, the open fee when marked."""
    if trade.amount is None:
        raise ValueError(f"the trade opened {trade.open_time} has no amount: equity is unknown")
    fee_open = trade.fee_open or 0.0
    fee_close = (trade.fee_close or 0.0) if closing else 0.0
    return trade.amount * (price * (1.0 - fee_close) - trade.open_rate * (1.0 + fee_open))


def equity_curve(
    trades: Sequence[ForwardTrade], candles: pd.DataFrame, *, wallet: float, interval: str
) -> pd.Series:
    """Marked equity after each bar in ``candles``: wallet + closed P&L + open positions at that
    bar's close. Pure; each value reads trades and prices up to that bar's close only.

    The index is the bar's open time, the project's candle time, so the bar closing at midnight
    counts in the day it traded. The first row is ``wallet`` one bar before the first candle, so
    a loss in the first bar is measured from the wallet. A trade counts as closed from the bar
    its close time falls in; one closing exactly at a bar's close is still marked at that bar
    (the rule ``observe_paper`` uses). Bars without a candle have no value: never filled."""
    if not wallet > 0:
        raise ValueError(f"wallet must be > 0, got {wallet!r}")
    if candles.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    step = pd.Timedelta(seconds=interval_seconds(interval))
    index = pd.DatetimeIndex(candles.index)
    closes = candles["close"].to_numpy(dtype=float)
    equity = np.full(len(index), float(wallet))
    for i, bar in enumerate(index):
        bar_close = bar + step
        for trade in trades:
            if trade.open_time >= bar_close:
                continue
            if trade.closed and trade.close_time is not None and trade.close_time < bar_close:
                if trade.close_rate is None:
                    raise ValueError(f"the trade closed {trade.close_time} has no close_rate")
                equity[i] += _trade_pnl(trade, trade.close_rate, closing=True)
            else:
                equity[i] += _trade_pnl(trade, float(closes[i]), closing=False)
    first = pd.DatetimeIndex([index[0] - step])
    return pd.Series(
        np.r_[float(wallet), equity], index=first.append(index), dtype=float, name="equity"
    )


# --------------------------------------------------------------------------- stopping the bot


def read_process_cmdline(pid: int) -> list[str] | None:
    """The command line of ``pid`` from ``ps`` (read only); ``[]`` when ``ps`` ran and found no
    such process (exit 1, nothing on stdout or stderr, the macOS and procps behaviour); None when
    ``ps`` itself failed or could not be read, which says nothing about the process."""
    try:
        done = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
            check=False,
        )
        if done.returncode == 0 and done.stdout.strip():
            return shlex.split(done.stdout)
        gone = done.returncode == 1 and not done.stdout.strip() and not done.stderr.strip()
        return [] if gone else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def is_this_dry_run(cmdline: Sequence[str] | None, config: Path) -> bool:
    """Whether ``cmdline`` is ``freqtrade trade --dry-run`` loading exactly ``config``. A wrapper
    (``python .../bin/freqtrade trade ...``) is fine; the words themselves must all be there."""
    if not cmdline:
        return False
    args = list(cmdline)
    subcommand = any(
        Path(word).name == "freqtrade" and args[i + 1] == "trade"
        for i, word in enumerate(args[:-1])
    )
    configs = [args[i + 1] for i, word in enumerate(args[:-1]) if word in ("--config", "-c")]
    return (
        subcommand
        and "--dry-run" in args
        and len(configs) == 1
        and Path(configs[0]).resolve() == Path(config).resolve()
    )


@dataclass(frozen=True)
class StopResult:
    signalled: bool
    message: str


def stop_bot(
    folder: Path,
    *,
    read_cmdline: CmdlineReader = read_process_cmdline,
    send_signal: SignalSender = os.kill,
) -> StopResult:
    """SIGTERM the folder's dry-run bot, only after verifying the pid (it may have been reused).
    Anything unverifiable sends nothing and names the owner's manual step."""
    root = Path(folder)
    manual = (
        f"stop the freqtrade dry-run for {root.name} by hand. Entries stay blocked: the strategy "
        f"reads {root / HALT_LOCK}"
    )
    try:
        pid = int((root / BOT_PID).read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        return StopResult(False, f"no usable {BOT_PID} ({exc}): {manual}")
    if pid <= 1:
        return StopResult(False, f"{BOT_PID} holds {pid}: {manual}")
    cmdline = read_cmdline(pid)
    if cmdline == []:
        return StopResult(
            False,
            f"pid {pid} from {BOT_PID} is gone; a bot started by hand would not show here. "
            f"Entries stay blocked by {HALT_LOCK} for it too, and it needs stopping by hand",
        )
    if cmdline is None:
        return StopResult(False, f"cannot read pid {pid} (not verified, no signal): {manual}")
    if not is_this_dry_run(cmdline, root / "config.json"):
        return StopResult(
            False,
            f"pid {pid} is not this folder's `freqtrade trade --dry-run` (not verified, no "
            f"signal): {manual}",
        )
    try:
        send_signal(pid, signal.SIGTERM)
    except ProcessLookupError:
        return StopResult(False, f"pid {pid} has already exited")
    except OSError as exc:
        return StopResult(False, f"could not signal pid {pid} ({exc}): {manual}")
    return StopResult(True, f"sent SIGTERM to pid {pid}")


def halt_line(lock: Mapping[str, Any]) -> str:
    """The journal line for a lock. Built only from the lock, so it is the same on every run."""
    if lock.get("reason") == UNREADABLE_LOCK_REASON:
        return (
            f"- Halted: `{HALT_LOCK}` exists but cannot be read, and a lock is a lock. Only the "
            "owner removes it, outside Claude Code."
        )
    return (
        f"- Halted at bar {lock.get('time')}: {lock.get('reason')} {lock.get('value')}% at or "
        f"beyond {lock.get('threshold')}%. `{HALT_LOCK}` written; only the owner removes it, "
        "outside Claude Code."
    )


def _read_lock(root: Path, out: Out) -> Mapping[str, Any] | None:
    """The lock, None without one. An unreadable lock still stops the test."""
    try:
        return read_halt_lock(root)
    except ValueError as exc:
        out(f"{exc}; it still counts as a lock")
        return {"reason": UNREADABLE_LOCK_REASON}


def _check_fresh(protocol: PaperProtocol, candles: pd.DataFrame, end: pd.Timestamp) -> None:
    """:class:`StaleCandlesError` unless ``candles`` reach the last bar closed by ``end``: a loss
    on a bar that has not arrived must not read as clear."""
    step = pd.Timedelta(seconds=interval_seconds(protocol.interval))
    last_bar = end - step
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    if end <= start:
        return  # the test has not started: no bar can be missing
    newest = None if candles.empty else candles.index[-1]
    if newest is None or newest < last_bar:
        raise StaleCandlesError(
            f"{protocol.candidate}: the candles stop at {newest}, the last closed bar is "
            f"{last_bar}; cannot say the test is clear",
            newest_only=last_bar == start if newest is None else newest == last_bar - step,
        )


def guard_once(
    protocol: PaperProtocol,
    folder: Path,
    journal: Path,
    *,
    end: pd.Timestamp,
    load_trades: Callable[[], Sequence[ForwardTrade]],
    load_candles: Callable[[], pd.DataFrame],
    read_cmdline: CmdlineReader = read_process_cmdline,
    send_signal: SignalSender = os.kill,
    out: Out = print,
) -> int:
    """One check of the bars closed by ``end``. Returns :data:`EXIT_CLEAR` or
    :data:`EXIT_HALTED`. An existing lock halts, readable or not, and a trip on the candles that
    are there halts even if later ones are late. Otherwise candles that stop before the last
    closed bar raise :class:`StaleCandlesError`; a failed load raises its own error."""
    root = Path(folder)
    breaker = protocol.risk_controls.circuit_breaker if protocol.risk_controls else None
    if breaker is None:
        raise ValueError(refuse_reason(protocol) or "no circuit breaker")
    lock = _read_lock(root, out)
    if lock is None:
        candles = load_candles()
        if candles.empty:
            _check_fresh(protocol, candles, end)
            out(f"{protocol.candidate}: no closed bar yet; nothing to check")
            return EXIT_CLEAR
        curve = equity_curve(
            load_trades(), candles, wallet=breaker.wallet, interval=protocol.interval
        )
        trip = evaluate_breaker(curve, breaker)
        if trip is None:
            _check_fresh(protocol, candles, end)  # late bars cannot undo a trip, only hide one
            out(
                f"{protocol.candidate}: clear through bar {curve.index[-1].isoformat()}, marked "
                f"equity {curve.iloc[-1]:.2f}"
            )
            return EXIT_CLEAR
        write_halt_lock(root, trip)
        lock = _read_lock(root, out)
        if lock is None:
            raise ValueError(f"{root / HALT_LOCK} vanished right after it was written")
        out(f"HALTED {protocol.candidate}: {trip.reason} {trip.value:.2f}% at {trip.time}")
    else:
        out(f"{protocol.candidate} is already halted ({lock.get('reason')!r}); the lock stands")
    stopped = stop_bot(root, read_cmdline=read_cmdline, send_signal=send_signal)
    out(stopped.message)
    if append_halt(journal, halt_line(lock)):
        out(f"journal: {journal}")
    return EXIT_HALTED


def trip_reason(
    protocol: PaperProtocol,
    folder: Path,
    trades: Sequence[ForwardTrade],
    candles: pd.DataFrame,
    *,
    end: pd.Timestamp,
) -> str | None:
    """Why ``compare`` must fail this test, or None: the frozen thresholds on the bars before
    ``end``, whether or not ``guard`` ran, then a lock for a bar before ``end`` (or one that
    cannot be read). A lock is a recorded halt even if revised candles no longer reproduce it."""
    breaker = protocol.risk_controls.circuit_breaker if protocol.risk_controls else None
    if breaker is None:
        return None
    curve = equity_curve(trades, candles, wallet=breaker.wallet, interval=protocol.interval)
    trip = evaluate_breaker(curve, breaker)
    if trip is not None:
        return (
            f"circuit breaker tripped: {trip.reason} {trip.value:.2f}% at or beyond "
            f"{trip.threshold:g}% at bar {trip.time}"
        )
    try:
        lock = read_halt_lock(Path(folder))
    except ValueError:
        return f"circuit breaker tripped: {HALT_LOCK} exists but cannot be read"
    if lock is None:
        return None
    try:
        at = pd.Timestamp(lock.get("time"))
        if at >= end:
            return None
    except (ValueError, TypeError):
        pass  # no readable time: it still halted the test
    return (
        f"circuit breaker tripped: {HALT_LOCK} records {lock.get('reason')} at {lock.get('time')}"
    )


def is_transient(exc: BaseException) -> bool:
    """Whether a failed check may pass at the next bar: a network failure, a rate limit, a ban or
    a refusal that lifts (Binance's 418 and 403), a server error, a dry-run DB locked by the bot,
    or a candle that has not reached the host yet. Retrying a 418 at the next bar is one request
    a bar, not the hammering that earned it."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (403, 418, 429) or exc.response.status_code >= 500
    if isinstance(exc, sqlite3.OperationalError):
        return "locked" in str(exc) or "busy" in str(exc)
    return isinstance(
        exc, httpx.TransportError | ConnectionError | TimeoutError | StaleCandlesError
    )


def _is_403(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403


def watch(
    check: Callable[[], int],
    *,
    interval: str,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    retryable: Callable[[BaseException], bool] = is_transient,
    out: Out = print,
    err: Out = print,
    max_checks: int | None = None,
) -> int:
    """Run ``check`` after each bar close until the test halts. A ``retryable`` failure is printed
    and retried at the next bar: a watcher must outlive a blip. Only the newest bar being late
    gets :data:`STALE_RETRIES` tries first, :data:`STALE_RETRY_DELAY_S` apart, so a slow candle
    does not cost a whole bar. Anything else propagates and ends the watch, because a watcher that
    retries a permanent error looks alive and guards nothing, and so does a 403 after
    :data:`MAX_CONSECUTIVE_403` checks in a row."""
    step = interval_seconds(interval)
    done = 0
    tries = 0
    forbidden = 0
    while True:
        try:
            code = check()
        except Exception as exc:
            if not retryable(exc):
                raise
            forbidden = forbidden + 1 if _is_403(exc) else 0
            if forbidden >= MAX_CONSECUTIVE_403:
                err(f"HTTP 403 on {forbidden} checks in a row: this is not a blip, stopping")
                raise
            if isinstance(exc, StaleCandlesError) and exc.newest_only and tries < STALE_RETRIES:
                tries += 1
                err(f"newest bar not here yet, try {tries} of {STALE_RETRIES}: {exc}")
                sleep(STALE_RETRY_DELAY_S)
                continue
            err(f"cannot check now, retrying after the next bar: {exc}")
            code = EXIT_CLEAR
        else:
            forbidden = 0
        tries = 0
        if code != EXIT_CLEAR:
            return code
        done += 1
        if max_checks is not None and done >= max_checks:
            return EXIT_CLEAR
        moment = now()
        sleep((moment // step + 1) * step + SETTLE_SECONDS - moment)
        out("next bar closed; checking again")


__all__ = [
    "BOT_PID",
    "EXIT_CLEAR",
    "EXIT_ERROR",
    "EXIT_HALTED",
    "MAX_CONSECUTIVE_403",
    "SETTLE_SECONDS",
    "STALE_RETRIES",
    "STALE_RETRY_DELAY_S",
    "StaleCandlesError",
    "StopResult",
    "equity_curve",
    "guard_once",
    "halt_line",
    "is_this_dry_run",
    "is_transient",
    "read_process_cmdline",
    "refuse_reason",
    "stop_bot",
    "trip_reason",
    "watch",
]
