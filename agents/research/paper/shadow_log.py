"""Append-only, hash-chained decision log of the Jev shadow judge (PLAN.md Phase 17).

The shadow judge watches the HC paper bots and never steers them. Every judgement it makes is
one line of ``agents/data/paper/jev-shadow/decisions.jsonl``, written before the fill bar's
outcome exists and never edited afterwards. This module owns only the file format and its
checks; it knows nothing about Jev, candles or the bots.

* **Format.** One JSON object per line, :func:`canonical_json` (sorted keys, no spaces, no NaN
  or infinity, UTF-8), each line ending in ``"\\n"``. Times are UNIX seconds (``written_at`` a
  float, every bar time an int).
* **Chain.** Every row carries ``seq`` (0-based, contiguous), ``prev_sha256`` (the previous
  row's ``sha256``; :data:`GENESIS_PREV` for the first row) and ``sha256`` =
  :func:`row_sha256` (sha256 of the canonical row without its ``sha256`` key). Row 0 is the
  ``genesis`` row that freezes the pre-registration (start, interval, grace, candidates, model,
  analysis); no later row may be a genesis.
* **Append only.** :class:`DecisionLog` writes under an exclusive ``fcntl.flock`` on the sibling
  ``decisions.jsonl.lock``, re-verifies the whole chain first (never appends to a broken chain),
  opens the file in append mode only, writes every new line in one write and fsyncs. Nothing
  here rewrites or truncates the file.
* **Verification.** :func:`verify_chain` fails (:class:`ChainError`, naming the 1-based line) on
  an edited row (``sha256`` mismatch), a deleted or reordered row (``seq`` / ``prev_sha256``
  break), a torn final line (no trailing newline), invalid JSON or a non-canonical line.
  **Limitation:** the chain is unkeyed, so anyone with write access to the file can delete
  rows from the end or edit a row and re-chain everything after it; the result is again a
  valid chain. The chain is tamper-evident only against heads recorded elsewhere: the shadow
  process appends every new head to the git-tracked ``agents/journal/jev-shadow-heads.md``,
  and once that file is committed and pushed, ``shadow-report`` catches a rewrite by checking
  every recorded ``(seq, sha256)``, that the last head covers the last row, and that the
  committed copy of the heads file is a prefix of the working one. The owner should commit
  the heads file regularly.
  ``expect_head`` (a last ``sha256`` kept anywhere else) is an extra check.
* **Deadlines.** The signal needs bar t's close, which is the same instant fill bar t+1 opens,
  so the deadline is ``fill_open + grace_s`` (owner decision; default :data:`DEFAULT_GRACE_S`).
  :func:`check_deadlines` reports any verdict written at or after the deadline, any ``missed``
  row declared before its deadline, and inconsistent decision rows. A bar that could not be
  judged in time is logged as ``missed``, never as a verdict and never filled in later.

Row schema (``kind``):

* ``genesis``: ``written_at``, ``start`` (first eligible ``fill_open``), ``interval``,
  ``grace_s``, ``prereg_journal``, ``prereg_sha256``, ``candidates``, ``model`` (requested),
  ``snapshot_version``, ``snapshot_fingerprint``, ``questions_sha256``, ``analysis`` (with
  ``evaluate_at``, ``strata`` and ``block_days``), ``btc_symbol``, ``btc_since``, ``env``
  (Python, numpy and pandas versions and the sha256 of ``agents/uv.lock``).
* ``bar``: ``signal_bar``, ``fill_open``, ``written_at``, ``status`` (``judged`` /
  ``no_signal`` / ``unavailable`` / ``missed``), ``checked``, ``signals``.
* ``decision``: ``signal_bar``, ``fill_open``, ``candidate``, ``written_at``, ``status``
  (``judged`` / ``unavailable`` / ``missed``), ``snapshot``, ``snapshot_sha256``, ``verdict``,
  ``model`` (the id Jev reports SERVING, ``null`` when it names none; never the requested
  alias), ``requested_model`` (the id the client asked for), ``latency_ms``, ``reason``.

Paper trading only; nothing here places orders or reads keys.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

GENESIS_PREV: str = "0" * 64
DEFAULT_GRACE_S: int = 900

CHAIN_KEYS: tuple[str, ...] = ("seq", "prev_sha256", "sha256")
BAR_STATUSES: frozenset[str] = frozenset({"judged", "no_signal", "unavailable", "missed"})
DECISION_STATUSES: frozenset[str] = frozenset({"judged", "unavailable", "missed"})


class ChainError(ValueError):
    """The decision log's hash chain does not verify (the message names the 1-based line)."""


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not allowed")


def canonical_json(obj: Mapping[str, Any]) -> str:
    """Canonical JSON: sorted keys, no spaces, no NaN/infinity, non-ASCII kept as UTF-8."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
    )


def row_sha256(row: Mapping[str, Any]) -> str:
    """sha256 hex of the canonical JSON of ``row`` without its ``sha256`` key."""
    body = {k: v for k, v in row.items() if k != "sha256"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def verify_chain(path: Path, *, expect_head: str | None = None) -> list[dict[str, Any]]:
    """Read and verify every row of the log at ``path``; return them in file order.

    A missing or empty file is an empty chain (``[]``). Raises :class:`ChainError` naming the
    1-based line on invalid JSON or UTF-8, a final line without a trailing newline (torn write),
    a non-object or non-canonical line, a ``seq`` gap / duplicate / reorder, a ``prev_sha256``
    mismatch, a ``sha256`` mismatch (edited row), a first row that is not ``genesis`` or a later
    ``genesis``.

    The chain cannot show that its last row(s) were deleted. When ``expect_head`` is given, the
    last row's ``sha256`` (:data:`GENESIS_PREV` for an empty chain) must equal it.
    """
    path = Path(path)
    data = path.read_bytes() if path.exists() else b""
    rows: list[dict[str, Any]] = []
    if data:
        lines = data.split(b"\n")
        if lines[-1] != b"":
            raise ChainError(
                f"{path}: line {len(lines)} has no trailing newline (torn or partial write)"
            )
        prev = GENESIS_PREV
        for index, raw in enumerate(lines[:-1]):
            row = _parse_line(path, index + 1, raw)
            _check_link(path, index, row, prev)
            prev = row["sha256"]
            rows.append(row)
    if expect_head is not None:
        head = rows[-1]["sha256"] if rows else GENESIS_PREV
        if head != expect_head:
            where = f"line {len(rows)}" if rows else "empty log"
            raise ChainError(
                f"{path}: head {head} ({where}) != expected head {expect_head}"
                " (rows deleted from the end, or appended since)"
            )
    return rows


def _parse_line(path: Path, line_no: int, raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        row = json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ChainError(f"{path}: line {line_no} is not valid JSON: {exc}") from exc
    if not isinstance(row, dict):
        raise ChainError(f"{path}: line {line_no} is a {type(row).__name__}, not a JSON object")
    try:
        canonical = canonical_json(row)
    except (TypeError, ValueError) as exc:
        raise ChainError(f"{path}: line {line_no} cannot be re-encoded: {exc}") from exc
    if canonical != text:
        raise ChainError(f"{path}: line {line_no} is not in canonical JSON form (edited)")
    return row


def _check_link(path: Path, index: int, row: dict[str, Any], prev: str) -> None:
    line_no = index + 1
    seq = row.get("seq")
    if not _is_int(seq):
        raise ChainError(f"{path}: line {line_no} has no integer seq (got {seq!r})")
    if seq != index:
        raise ChainError(
            f"{path}: line {line_no} has seq {seq}, expected {index}"
            " (row deleted, duplicated or reordered)"
        )
    if row.get("prev_sha256") != prev:
        raise ChainError(
            f"{path}: line {line_no} prev_sha256 {row.get('prev_sha256')!r} != previous row's"
            f" sha256 {prev} (row deleted, inserted or reordered)"
        )
    stored = row.get("sha256")
    if not isinstance(stored, str) or stored != row_sha256(row):
        raise ChainError(f"{path}: line {line_no} sha256 does not match its content (row edited)")
    kind = row.get("kind")
    if index == 0 and kind != "genesis":
        raise ChainError(f"{path}: line 1 must be the genesis row, got kind {kind!r}")
    if index > 0 and kind == "genesis":
        raise ChainError(f"{path}: line {line_no} is a second genesis row")


def _validate_new_row(index: int, row: Any) -> str:
    """Check one input row before anything touches disk; return its canonical body."""
    if not isinstance(row, Mapping):
        raise ValueError(f"new row {index} must be a mapping, got {type(row).__name__}")
    taken = [k for k in CHAIN_KEYS if k in row]
    if taken:
        raise ValueError(f"new row {index} must not set {taken}: the log assigns them")
    kind = row.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError(f"new row {index} needs a non-empty string 'kind', got {kind!r}")
    if any(not isinstance(k, str) for k in row):
        raise ValueError(f"new row {index} has a non-string key")
    try:
        return canonical_json(row)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"new row {index} is not JSON-serialisable (NaN/inf too): {exc}") from exc


@contextlib.contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class DecisionLog:
    """Writer and reader of one append-only, hash-chained decision log."""

    def __init__(self, path: Path) -> None:
        self.path: Path = Path(path)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def rows(self) -> list[dict[str, Any]]:
        """Every row, verified (:func:`verify_chain`)."""
        return verify_chain(self.path)

    def append(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Append one row; return it as stored (with ``seq``, ``prev_sha256``, ``sha256``)."""
        return self.append_many([row])[0]

    def append_many(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Append ``rows`` in one write after re-verifying the chain; return them as stored.

        Raises ``ValueError`` for a row that already has a chain key, has no ``kind`` or is not
        JSON-serialisable, for a first-ever row that is not ``genesis`` and for any later
        ``genesis``; :class:`ChainError` (leaving the file untouched) when the existing chain is
        broken.
        """
        for index, row in enumerate(rows):
            _validate_new_row(index, row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_path):
            existing = verify_chain(self.path)
            seq = len(existing)
            prev = existing[-1]["sha256"] if existing else GENESIS_PREV
            lines: list[str] = []
            stored: list[dict[str, Any]] = []
            for index, row in enumerate(rows):
                kind = row["kind"]
                if seq == 0 and kind != "genesis":
                    raise ValueError(
                        f"new row {index}: the first row of {self.path} must be kind 'genesis',"
                        f" got {kind!r}"
                    )
                if seq > 0 and kind == "genesis":
                    raise ValueError(
                        f"new row {index}: genesis is allowed only as the first row of"
                        f" {self.path} (it would be seq {seq})"
                    )
                body = dict(row)
                body["seq"] = seq
                body["prev_sha256"] = prev
                body["sha256"] = row_sha256(body)
                line = canonical_json(body)
                lines.append(line + "\n")
                stored.append(json.loads(line))
                prev = body["sha256"]
                seq += 1
            if not lines:
                return []
            created = not self.path.exists()
            with open(self.path, "ab") as handle:
                handle.write("".join(lines).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            if created:
                _fsync_dir(self.path.parent)
            return stored


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def snapshot_sha256(snapshot: Mapping[str, Any]) -> str:
    """sha256 hex of the canonical JSON of a decision's blind snapshot."""
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def check_deadlines(rows: Sequence[Mapping[str, Any]], grace_s: int) -> list[str]:
    """Return one human-readable problem per deadline or consistency violation ([] when clean).

    For every ``bar`` / ``decision`` row (deadline = ``fill_open + grace_s``):

    * ``signal_bar`` and ``fill_open`` must be ints with ``signal_bar < fill_open``, and
      ``written_at`` a finite number;
    * a row whose status is not ``missed`` must be written in ``[fill_open, deadline)``: not
      before its signal bar closed and not at or after the deadline;
    * a ``missed`` row may only be declared at or after its deadline;
    * a ``judged`` decision needs a ``verdict`` and a ``snapshot_sha256``; a ``missed`` or
      ``unavailable`` decision must carry no verdict;
    * a present ``snapshot`` must hash to ``snapshot_sha256`` (and a hash needs its snapshot);
    * no ``fill_open`` before the genesis ``start``, and at most one bar row per ``fill_open``
      and one decision per (``candidate``, ``fill_open``) (no re-asking Jev).

    Unknown kinds are reported too. Genesis rows are not deadline-checked.
    """
    if not _is_int(grace_s) or grace_s < 0:
        raise ValueError(f"grace_s must be an integer >= 0, got {grace_s!r}")
    problems: list[str] = []
    start: int | None = None
    seen_bars: set[int] = set()
    seen_decisions: set[tuple[str, int]] = set()
    for index, row in enumerate(rows):
        where = f"line {index + 1} (seq {row.get('seq')!r}, kind {row.get('kind')!r})"
        kind = row.get("kind")
        if kind == "genesis":
            if _is_int(row.get("start")):
                start = row["start"]
            continue
        if kind not in ("bar", "decision"):
            problems.append(f"{where}: unknown kind")
            continue
        problems.extend(_row_problems(where, row, grace_s, start, seen_bars, seen_decisions))
    return problems


def _row_problems(
    where: str,
    row: Mapping[str, Any],
    grace_s: int,
    start: int | None,
    seen_bars: set[int],
    seen_decisions: set[tuple[str, int]],
) -> list[str]:
    out: list[str] = []
    kind = row["kind"]
    signal_bar, fill_open, written_at = (
        row.get("signal_bar"),
        row.get("fill_open"),
        row.get("written_at"),
    )
    status = row.get("status")
    allowed = BAR_STATUSES if kind == "bar" else DECISION_STATUSES
    if status not in allowed:
        out.append(f"{where}: status {status!r} is not one of {sorted(allowed)}")
    if not _is_int(signal_bar) or not _is_int(fill_open):
        out.append(f"{where}: signal_bar and fill_open must be integer UNIX seconds")
        return out
    if not _is_num(written_at):
        out.append(f"{where}: written_at must be a finite number, got {written_at!r}")
        return out
    if signal_bar >= fill_open:
        out.append(f"{where}: signal_bar {signal_bar} is not before fill_open {fill_open}")
    if start is not None and fill_open < start:
        out.append(f"{where}: fill_open {fill_open} is before the genesis start {start}")
    deadline = fill_open + grace_s
    if status == "missed":
        if written_at < deadline:
            out.append(
                f"{where}: declared missed at {written_at} before its deadline {deadline}"
                f" (fill_open {fill_open} + grace {grace_s})"
            )
    else:
        if written_at >= deadline:
            out.append(
                f"{where}: status {status!r} written at {written_at}, at or after its deadline"
                f" {deadline} (fill_open {fill_open} + grace {grace_s}); must be 'missed'"
            )
        if written_at < fill_open:
            out.append(
                f"{where}: written at {written_at}, before its signal bar closed"
                f" (fill_open {fill_open})"
            )
    if kind == "bar":
        if fill_open in seen_bars:
            out.append(f"{where}: second bar row for fill_open {fill_open}")
        seen_bars.add(fill_open)
        return out
    candidate = row.get("candidate")
    key = (str(candidate), fill_open)
    if key in seen_decisions:
        out.append(f"{where}: second decision for candidate {candidate!r} at fill_open {fill_open}")
    seen_decisions.add(key)
    verdict, snapshot, snap_hash = (
        row.get("verdict"),
        row.get("snapshot"),
        row.get("snapshot_sha256"),
    )
    if status == "judged" and (verdict is None or snap_hash is None):
        out.append(f"{where}: judged decision without a verdict or snapshot_sha256")
    if status in ("missed", "unavailable") and verdict is not None:
        out.append(f"{where}: {status} decision carries a verdict")
    if snapshot is not None:
        if not isinstance(snapshot, Mapping):
            out.append(f"{where}: snapshot must be an object or null")
        elif snap_hash != snapshot_sha256(snapshot):
            out.append(f"{where}: snapshot_sha256 {snap_hash!r} does not match its snapshot")
    elif snap_hash is not None:
        out.append(f"{where}: snapshot_sha256 given without its snapshot (unverifiable)")
    return out
