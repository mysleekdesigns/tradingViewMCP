"""The backtest side of ``compare`` under a protocol's protections (PLAN.md Phase 20 Part E).

``compare``'s backtest is the lab engine replay, which has no freqtrade protections. For a
protocol that froze some, this bridge runs the same strategy and config through ``freqtrade
backtesting --enable-protections`` and reads the entries it made, the locks it recorded and how
far its data reached. The lab replay then runs again with entries off on the bars those locks
block (:func:`research.paper.compare.mask_locked_entries`), so it walks the bot's path: positions
and later entries follow from the masked signals, and nothing is deleted afterwards. Every entry
outside a lock stays and counts: one the bot did not make is irregular, whatever the reason.

Fails closed: freqtrade missing from PATH, a failed or timed-out run, an export of any unexpected
shape (locks included), data that stop more than a bar before ``end``, a first replay entry
without a freqtrade entry, or a freqtrade entry on a bar its own locks block raises
:class:`ProtectedBacktestError`, which ``compare`` reports as exit 2 "cannot compare" until the
frozen grace period after the decision bar, and then records as a final ``inconclusive``. There
is no fallback to an unprotected comparison, and nothing here ever runs for a protocol without
protections or builds a ``freqtrade trade`` command.

Run against real freqtrade 2026.9 (``~/.venvs/freqtrade``; the HC bots use it, and it only needs
to be on PATH) on 2026-11-04: the export (one zip whose main JSON has
``strategy.<Class>.trades``, ``.locks[*].lock_timestamp`` / ``lock_end_timestamp`` and
``.backtest_end_ts``), the flags as written here, and ``download-data`` through the config's
``ccxt_config`` host. Tests use a fake runner. Still unverified: another freqtrade version (the
parser refuses a changed shape), and the host override from a network without the sandbox proxy.

What this does and does not guarantee:

* Only entries on locked bars are masked, and masked/total goes in the report and on every journal
  line. Masked means an entry of the unprotected replay that the masked replay does not make on
  that bar, so a shifted entry counts too. Above the frozen ``max_blocked_share`` a pass is out of
  reach.
* The locks are the backtest's, computed from the backtest's own trades. A bot whose fills drift
  can be locked at other bars, and a skip inside a backtest lock is not caught for that entry.

Paper trading only: backtesting places nothing and the config is re-checked as dry-run. Results
are historical, not forward-looking.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from lab.types import Trade, interval_seconds
from research.paper.compare import Lock, bar_is_locked, match_fill_bars
from research.paper.freqtrade import (
    FREQTRADE_INSTALL_HINT,
    _child_env,
    check_paper_config,
    symbol_to_pair,
)
from research.paper.protocol import PaperProtocol

DOWNLOAD_TIMEOUT_S = 600.0  # one pair and interval: minutes at most, or the host is failing
BACKTEST_TIMEOUT_S = 600.0  # a 28-90 day test on one pair: far less than this
ERROR_TAIL_CHARS = 2_000  # enough of freqtrade's stderr to name the failure
LOCK_SIDES = ("*", "long")  # this project is long-only; "*" is every side
GLOBAL_LOCK_PAIR = "*"  # freqtrade's pair for a lock on every pair
BLOCKED_LINE_PREFIX = "Protections:"

Runner = Callable[..., Any]


class ProtectedBacktestError(ValueError):
    """The protected backtest cannot be trusted or did not run: ``compare`` must not judge."""


@dataclass(frozen=True)
class ProtectedRun:
    """What one protected backtest recorded, in UTC."""

    opened: tuple[pd.Timestamp, ...]  # each entry's open time
    locks: tuple[Lock, ...]  # (start, end) of this pair's locks: see ``bar_is_locked``
    backtest_end: pd.Timestamp


@dataclass(frozen=True)
class MaskedReplay:
    """The lab replay under the backtest's locks."""

    trades: list[Trade]  # what ``compare`` judges the bot against
    masked: int  # entries of the unprotected replay that this replay does not make on their bar
    total: int  # entries of the unprotected replay


def has_protections(protocol: PaperProtocol) -> bool:
    """Whether ``protocol`` froze freqtrade protections (only then may this module run)."""
    controls = protocol.risk_controls
    return controls is not None and controls.protections is not None


def _day(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y%m%d")


def timerange(start: pd.Timestamp, end: pd.Timestamp) -> str:
    """``YYYYMMDD-YYYYMMDD`` covering ``[start, end)``: the first day floored, the last rounded
    up, because freqtrade reads whole days here. Entries outside are dropped by the matching."""
    return f"{_day(start.floor('1D'))}-{_day(end.ceil('1D'))}"


def download_command(
    *, config: Path, userdir: Path, interval: str, start: pd.Timestamp, end: pd.Timestamp
) -> list[str]:
    return [
        "freqtrade",
        "download-data",
        "--config",
        str(config),
        "--userdir",
        str(userdir),
        "--timeframes",
        interval,
        "--timerange",
        timerange(start, end),
    ]


def backtest_command(
    *,
    config: Path,
    class_name: str,
    folder: Path,
    userdir: Path,
    export_dir: Path,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[str]:
    return [
        "freqtrade",
        "backtesting",
        "--config",
        str(config),
        "--strategy",
        class_name,
        "--strategy-path",
        str(folder),
        "--userdir",
        str(userdir),
        "--timerange",
        timerange(start, end),
        "--enable-protections",
        "--export",
        "trades",
        "--backtest-directory",
        str(export_dir),
    ]


def check_backtest_command(command: Sequence[str], *, config: Path) -> None:
    """Refuse any command but ``freqtrade backtesting --enable-protections`` on ``config``.

    PLAN.md Part E: a protocol with protections and a backtest without them is refused."""
    cmd = list(command)
    if cmd[:2] != ["freqtrade", "backtesting"]:
        raise ProtectedBacktestError(f"refusing {cmd[:2]!r}: only `freqtrade backtesting` runs")
    if "--enable-protections" not in cmd:
        raise ProtectedBacktestError(
            "refusing a backtest without --enable-protections: paper and backtest must trade "
            "under the same protections"
        )
    configs = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a in ("--config", "-c")]
    if configs != [str(config)]:
        raise ProtectedBacktestError(
            f"the backtest must load exactly the prepared config {config}, got {configs!r}"
        )


def _strategy_block(document: Any, class_name: str) -> Mapping[str, Any] | None:
    if not isinstance(document, Mapping):
        return None
    strategies = document.get("strategy")
    if not isinstance(strategies, Mapping):
        return None
    block = strategies.get(class_name)
    if not isinstance(block, Mapping) or not isinstance(block.get("trades"), list):
        return None
    return block


def _open_time(trade: Any) -> pd.Timestamp:
    raw = trade.get("open_date") if isinstance(trade, Mapping) else None
    try:
        stamp = pd.Timestamp(raw) if isinstance(raw, str) else pd.NaT
    except ValueError as exc:
        raise ProtectedBacktestError(f"unreadable open_date {raw!r} in the export") from exc
    if stamp is pd.NaT:
        raise ProtectedBacktestError(f"unreadable open_date {raw!r} in the export")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _millis(value: Any, what: str) -> pd.Timestamp:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtectedBacktestError(f"unreadable {what} {value!r} in the export")
    return pd.Timestamp(value, unit="ms", tz="UTC")


def _locks_of(block: Mapping[str, Any], pair: str) -> list[Lock]:
    """Active locks on ``pair`` (or every pair) for a long entry. A lock has to be proved: anything
    malformed raises, because a misread lock would excuse a skipped entry."""
    raw = block.get("locks")
    if not isinstance(raw, list):
        raise ProtectedBacktestError(f"the export has no locks list, got {raw!r}")
    locks: list[Lock] = []
    for lock in raw:
        if not isinstance(lock, Mapping) or not isinstance(lock.get("active"), bool):
            raise ProtectedBacktestError(f"unreadable lock {lock!r} in the export")
        begin = _millis(lock.get("lock_timestamp"), "lock_timestamp")
        end = _millis(lock.get("lock_end_timestamp"), "lock_end_timestamp")
        if end <= begin:
            raise ProtectedBacktestError(f"lock {lock!r} ends before it starts")
        if not isinstance(lock.get("pair"), str) or lock.get("side") not in (*LOCK_SIDES, "short"):
            raise ProtectedBacktestError(f"unreadable lock {lock!r} in the export")
        if (
            lock["active"]
            and lock["pair"] in (pair, GLOBAL_LOCK_PAIR)
            and lock["side"] in LOCK_SIDES
        ):
            locks.append((begin, end))
    return locks


def read_export(export_dir: Path, class_name: str, pair: str) -> ProtectedRun:
    """The entries, locks and data end of the one zip in ``export_dir``.

    The parser takes nothing on trust: exactly one zip, exactly one JSON in it with
    ``strategy.<class_name>.trades``, every trade carrying a parseable ``open_date``, a readable
    ``locks`` list and ``backtest_end_ts``; anything else raises rather than guessing."""
    zips = sorted(Path(export_dir).glob("*.zip"))
    if len(zips) != 1:
        raise ProtectedBacktestError(
            f"expected one exported zip in {export_dir}, found {len(zips)}"
        )
    found: list[Mapping[str, Any]] = []
    try:
        with zipfile.ZipFile(zips[0]) as archive:
            for name in archive.namelist():
                if not name.endswith(".json"):
                    continue
                try:
                    document = json.loads(archive.read(name))
                except ValueError:
                    continue
                block = _strategy_block(document, class_name)
                if block is not None:
                    found.append(block)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProtectedBacktestError(f"cannot read the export {zips[0]}: {exc}") from exc
    if len(found) != 1:
        raise ProtectedBacktestError(
            f"the export {zips[0].name} has {len(found)} JSON files with "
            f"strategy.{class_name}.trades, expected 1: the export format is not the expected one"
        )
    block = found[0]
    return ProtectedRun(
        opened=tuple(_open_time(trade) for trade in block["trades"]),
        locks=tuple(_locks_of(block, pair)),
        backtest_end=_millis(block.get("backtest_end_ts"), "backtest_end_ts"),
    )


def _run(
    command: list[str], *, cwd: Path, timeout: float, runner: Runner, env: Mapping[str, str] | None
) -> None:
    try:
        done = runner(
            command,
            cwd=str(cwd),
            env=_child_env(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProtectedBacktestError(f"freqtrade {command[1]} did not run: {exc}") from exc
    if done.returncode != 0:
        tail = str(done.stderr or "")[-ERROR_TAIL_CHARS:]
        raise ProtectedBacktestError(
            f"freqtrade {command[1]} exited {done.returncode}: {tail.strip()}"
        )


def run_protected_backtest(
    protocol: PaperProtocol,
    folder: Path,
    *,
    class_name: str,
    interval: str,
    end: pd.Timestamp,
    runner: Runner | None = None,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> ProtectedRun:
    """What freqtrade does over ``[protocol.start, end)`` with the frozen protections on, from
    the prepared strategy and config. Candles reach back to ``data.warm_from``. The export goes
    to a temporary folder, so an old result is never read. A download can exit 0 and fetch
    nothing, so a run whose data stop more than one bar before ``end`` raises: an entry past the
    data's end was not blocked by anything."""
    if not has_protections(protocol):
        raise ProtectedBacktestError(
            f"{protocol.candidate} has no protections: this backtest never runs for it"
        )
    if which("freqtrade") is None:
        raise ProtectedBacktestError(
            f"{protocol.candidate} froze protections and {FREQTRADE_INSTALL_HINT}; refusing to "
            "compare without them"
        )
    root = Path(folder)
    config = root / "config.json"
    check_paper_config(json.loads(config.read_text(encoding="utf-8")))
    userdir = root / "user_data"
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    warm = pd.Timestamp(protocol.data.warm_from, unit="s", tz="UTC")
    run = runner if runner is not None else subprocess.run
    with tempfile.TemporaryDirectory(prefix="tv-protected-") as tmp:
        export_dir = Path(tmp)
        command = backtest_command(
            config=config,
            class_name=class_name,
            folder=root,
            userdir=userdir,
            export_dir=export_dir,
            start=start,
            end=end,
        )
        check_backtest_command(command, config=config)
        download = download_command(
            config=config, userdir=userdir, interval=interval, start=warm, end=end
        )
        _run(download, cwd=root, timeout=DOWNLOAD_TIMEOUT_S, runner=run, env=env)
        _run(command, cwd=root, timeout=BACKTEST_TIMEOUT_S, runner=run, env=env)
        result = read_export(export_dir, class_name, symbol_to_pair(protocol.symbol))
    last_bar = end - pd.Timedelta(seconds=interval_seconds(interval))
    if result.backtest_end < last_bar:
        raise ProtectedBacktestError(
            f"the protected backtest stops at {result.backtest_end}, before the last bar "
            f"{last_bar}: it did not cover the period, so its missing entries prove nothing"
        )
    return result


def masked_replay(
    lab_trades: Sequence[Trade],
    run: ProtectedRun,
    replay: Callable[[Sequence[Lock]], list[Trade]],
    *,
    interval: str,
    tolerance_bars: int,
) -> MaskedReplay:
    """``replay`` (the lab replay of the plan) again under ``run``'s locks, and what that masked.

    ``lab_trades`` is the unprotected replay. A protection reacts to earlier trades, so it cannot
    block the first entry: if that one has no protected entry, the backtest did not run the plan,
    and that raises. So does a freqtrade entry on a bar ``bar_is_locked`` calls blocked: the locks
    do not mean what this reading says, and every mask below would be wrong."""
    step = interval_seconds(interval)
    protected = [int(t.timestamp()) // step * step for t in run.opened]
    for bar in protected:
        stamp = pd.Timestamp(bar, unit="s", tz="UTC")
        if bar_is_locked(stamp, run.locks):
            raise ProtectedBacktestError(
                f"the protected backtest entered on {stamp}, a bar its own locks block: the locks "
                "cannot be read the way the masking needs"
            )
    if not lab_trades:
        return MaskedReplay([], 0, 0)
    if not run.opened:
        raise ProtectedBacktestError(
            f"the protected backtest made no entries where the lab replay made "
            f"{len(lab_trades)}: protections cannot block every entry, so it did not run the plan"
        )
    lab_bars = [int(t.entry_time.timestamp()) for t in lab_trades]
    pairs = match_fill_bars(protected, lab_bars, tolerance_seconds=tolerance_bars * step)
    first = lab_bars.index(min(lab_bars))
    if first not in {j for _, j in pairs}:
        raise ProtectedBacktestError(
            f"the protected backtest has no entry for the first lab entry "
            f"{lab_trades[first].entry_time}: a protection cannot block it, so it did not run "
            "the plan"
        )
    trades = replay(run.locks)
    kept = {t.entry_time for t in trades}
    return MaskedReplay(trades, sum(t.entry_time not in kept for t in lab_trades), len(lab_trades))


def blocked_note(masked: int, total: int) -> str:
    """The masked/total sentence of the report and of every journal line of a protected test."""
    share = f" ({masked / total:.0%})" if total else ""
    return (
        f"{BLOCKED_LINE_PREFIX} masked {masked} of {total} lab replay entries{share}: the replay "
        "ran with entries off on bars inside a recorded protection lock."
    )


def blocked_unknown_note(why: str) -> str:
    """The same place when the masked share could not be computed."""
    return f"{BLOCKED_LINE_PREFIX} masked entries not computed: {why}"


__all__ = [
    "BACKTEST_TIMEOUT_S",
    "BLOCKED_LINE_PREFIX",
    "DOWNLOAD_TIMEOUT_S",
    "MaskedReplay",
    "ProtectedBacktestError",
    "ProtectedRun",
    "backtest_command",
    "blocked_note",
    "blocked_unknown_note",
    "check_backtest_command",
    "download_command",
    "has_protections",
    "masked_replay",
    "read_export",
    "run_protected_backtest",
    "timerange",
]
