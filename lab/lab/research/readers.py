"""Read-only readers behind the lab API's Research page (PLAN.md Phase 8).

Every reader takes its directory or database path as an argument (tests pass tmp paths; the
API defaults to the paths in :mod:`lab.research.contract`). Missing, unreadable, or malformed
inputs yield empty results or ``None``, never an exception, so the page degrades to "nothing
yet" instead of a 500. Nothing here writes: the idea library is opened with SQLite
``mode=ro``, and files are only read.

Journal names are validated and resolved so a request cannot read outside the journal
directory (no ``..``, no absolute paths, no symlinks).

All times returned are UNIX seconds (UTC), except fields copied verbatim from the orchestrator's
own JSON files (``RunState``, ``PaperComparison``), which keep their contract shapes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "JOURNAL_NAME_PATTERN",
    "MAX_IDEAS",
    "MAX_JOURNAL_BYTES",
    "JournalTooLargeError",
    "iso_to_seconds",
    "journal_path",
    "latest_paper_comparisons",
    "list_candidates",
    "list_journal",
    "list_run_states",
    "parse_frontmatter",
    "read_ideas",
    "read_journal",
    "read_run_state",
]

JOURNAL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.md$")
MAX_JOURNAL_BYTES = 2 * 1024 * 1024
MAX_STATE_BYTES = 5 * 1024 * 1024  # RunState events are capped at 200; 5 MB is generous
MAX_COMPARISON_BYTES = 1024 * 1024
MAX_IDEAS = 500
_HEAD_BYTES = 64 * 1024  # enough to hold a journal file's frontmatter and first heading
_STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\.json$")
_COMPARISON_NAME = re.compile(r"^comparison-\d{4}-\d{2}-\d{2}\.json$")


# --------------------------------------------------------------------------- helpers


def iso_to_seconds(text: object) -> int | None:
    """ISO-8601 text to UNIX seconds (naive means UTC). ``None`` when unparseable."""
    if not isinstance(text, str) or not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _read_json_dict(path: Path, max_bytes: int) -> dict[str, Any] | None:
    """A regular, non-symlink file holding a JSON object, else ``None``."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _plain_files(directory: Path) -> list[Path]:
    try:
        if not directory.is_dir():
            return []
        return [p for p in directory.iterdir() if not p.is_symlink() and p.is_file()]
    except OSError:
        return []


# --------------------------------------------------------------------------- idea library


def _ro_connect(db_path: Path) -> sqlite3.Connection | None:
    p = Path(db_path)
    if not p.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _json_list(text: object) -> list[Any]:
    if not isinstance(text, str):
        return []
    try:
        value = json.loads(text)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def read_ideas(
    db_path: Path | str,
    *,
    run_id: str | None = None,
    status: str | None = None,
    limit: int = MAX_IDEAS,
) -> list[dict[str, Any]]:
    """Ideas newest first (``created_at`` desc, then id desc), at most ``min(limit, 500)``,
    each with its ``sources``. ``[]`` when the database or its tables are missing."""
    conn = _ro_connect(Path(db_path))
    if conn is None:
        return []
    limit = max(0, min(int(limit), MAX_IDEAS))
    where: list[str] = []
    args: list[Any] = []
    if run_id:
        where.append("run_id = ?")
        args.append(run_id)
    if status:
        where.append("status = ?")
        args.append(status)
    sql = (
        "SELECT id, created_at, run_id, family, title, hypothesis, rationale, status,"
        " verdict_note, plan_hashes FROM ideas"
        + (f" WHERE {' AND '.join(where)}" if where else "")
        + " ORDER BY created_at DESC, id DESC LIMIT ?"
    )
    try:
        with closing(conn):
            rows = conn.execute(sql, (*args, limit)).fetchall()
            ids = [int(r["id"]) for r in rows]
            sources: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
            if ids:
                marks = ",".join("?" * len(ids))
                for s in conn.execute(
                    f"SELECT idea_id, url, title, untrusted FROM sources"
                    f" WHERE idea_id IN ({marks}) ORDER BY id",
                    ids,
                ):
                    sources[int(s["idea_id"])].append(
                        {"url": s["url"], "title": s["title"], "untrusted": bool(s["untrusted"])}
                    )
    except sqlite3.Error:
        return []
    return [
        {
            "id": int(r["id"]),
            "created_at": iso_to_seconds(r["created_at"]),
            "run_id": r["run_id"],
            "family": r["family"],
            "title": r["title"],
            "hypothesis": r["hypothesis"],
            "rationale": r["rationale"],
            "status": r["status"],
            "verdict_note": r["verdict_note"],
            "plan_hashes": [h for h in _json_list(r["plan_hashes"]) if isinstance(h, str)],
            "sources": sources[int(r["id"])],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- journal


def _scalar(text: str) -> str:
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    return t


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split simple YAML-ish frontmatter (between a leading ``---`` line and the next ``---``
    line) from the body. Supports ``key: value`` and ``key: [a, b]``; anything else is
    skipped. Returns ``({}, text)`` when there is no closed frontmatter block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, Any] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        if not key or line[0].isspace():
            continue
        value = raw.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = [_scalar(v) for v in inner.split(",") if v.strip()] if inner else []
        else:
            meta[key] = _scalar(value)
    return meta, "\n".join(lines[end + 1 :])


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            if title:
                return title
    return None


def _int_list(value: object) -> list[int]:
    items = value if isinstance(value, list) else [value] if value not in (None, "") else []
    out: list[int] = []
    for v in items:
        try:
            out.append(int(str(v).strip()))
        except ValueError:
            continue
    return out


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def journal_path(journal_dir: Path | str, name: str) -> Path | None:
    """The journal file for ``name`` if the name is valid and the file is a regular,
    non-symlink file directly inside ``journal_dir``; else ``None``."""
    if not isinstance(name, str) or not JOURNAL_NAME_PATTERN.fullmatch(name):
        return None
    base = Path(journal_dir)
    candidate = base / name
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None
        root = base.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if resolved.parent != root:
        return None
    return resolved


def list_journal(journal_dir: Path | str) -> list[dict[str, Any]]:
    """``*.md`` files directly in ``journal_dir``, newest name first, with frontmatter fields
    ``run_id``, ``date``, ``outcome``, ``idea_ids`` and a title (first ``# `` heading, then
    frontmatter ``title``, then the file stem)."""
    entries: list[dict[str, Any]] = []
    for p in _plain_files(Path(journal_dir)):
        if journal_path(journal_dir, p.name) is None:
            continue
        try:
            size = p.stat().st_size
            with p.open("rb") as fh:
                head = fh.read(_HEAD_BYTES).decode("utf-8", errors="replace")
        except OSError:
            continue
        meta, body = parse_frontmatter(head)
        title = _first_heading(body) or _opt_str(meta.get("title")) or p.stem
        entries.append(
            {
                "name": p.name,
                "title": title,
                "run_id": _opt_str(meta.get("run_id")),
                "date": _opt_str(meta.get("date")),
                "outcome": _opt_str(meta.get("outcome")),
                "idea_ids": _int_list(meta.get("idea_ids")),
                "size": size,
            }
        )
    entries.sort(key=lambda e: e["name"], reverse=True)
    return entries


class JournalTooLargeError(ValueError):
    """The journal file exceeds :data:`MAX_JOURNAL_BYTES`."""


def read_journal(journal_dir: Path | str, name: str) -> str | None:
    """Markdown text of a journal file, ``None`` if the name is invalid or the file is missing
    or outside the journal. Raises :class:`JournalTooLargeError` above 2 MB."""
    path = journal_path(journal_dir, name)
    if path is None:
        return None
    try:
        if path.stat().st_size > MAX_JOURNAL_BYTES:
            raise JournalTooLargeError(f"{name} is larger than {MAX_JOURNAL_BYTES} bytes")
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# --------------------------------------------------------------------------- run state


def _valid_state(state: dict[str, Any] | None, expected_run_id: str | None) -> bool:
    if state is None or not isinstance(state.get("run_id"), str):
        return False
    return expected_run_id is None or state["run_id"] == expected_run_id


def read_run_state(run_state_dir: Path | str, run_id: str) -> dict[str, Any] | None:
    """``<run_state_dir>/<run_id>.json`` as a dict, or ``None`` when missing, malformed, not
    a JSON object, or recording a different run id."""
    name = f"{run_id}.json"
    if not isinstance(run_id, str) or not _STATE_NAME.fullmatch(name):
        return None
    state = _read_json_dict(Path(run_state_dir) / name, MAX_STATE_BYTES)
    return state if _valid_state(state, run_id) else None


def list_run_states(run_state_dir: Path | str) -> list[dict[str, Any]]:
    """Every valid RunState file, newest run first (``started_at`` desc, then run id desc)."""
    states: list[dict[str, Any]] = []
    for p in _plain_files(Path(run_state_dir)):
        if not _STATE_NAME.fullmatch(p.name):
            continue
        state = _read_json_dict(p, MAX_STATE_BYTES)
        if _valid_state(state, p.name[: -len(".json")]):
            states.append(state)  # type: ignore[arg-type]
    states.sort(key=lambda s: (iso_to_seconds(s.get("started_at")) or 0, s["run_id"]), reverse=True)
    return states


def list_candidates(run_state_dir: Path | str) -> list[dict[str, Any]]:
    """Every ``CandidateSummary`` across RunState files plus its ``run_id``, newest run first."""
    out: list[dict[str, Any]] = []
    for state in list_run_states(run_state_dir):
        cands = state.get("candidates")
        if not isinstance(cands, list):
            continue
        out.extend({**c, "run_id": state["run_id"]} for c in cands if isinstance(c, dict))
    return out


# --------------------------------------------------------------------------- paper trading


def latest_paper_comparisons(paper_dir: Path | str) -> list[dict[str, Any]]:
    """The newest valid ``comparison-<YYYY-MM-DD>.json`` in each candidate folder of
    ``paper_dir``, ordered by folder name. Malformed newer files fall back to older ones."""
    base = Path(paper_dir)
    try:
        folders = (
            sorted(d for d in base.iterdir() if not d.is_symlink() and d.is_dir())
            if base.is_dir()
            else []
        )
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for folder in folders:
        files = sorted(
            (p for p in _plain_files(folder) if _COMPARISON_NAME.fullmatch(p.name)),
            key=lambda p: p.name,
            reverse=True,
        )
        for p in files:
            data = _read_json_dict(p, MAX_COMPARISON_BYTES)
            if data is not None:
                out.append(data)
                break
    return out
