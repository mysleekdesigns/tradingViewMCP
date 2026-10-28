"""The idea library: hypotheses, their web sources, and dedupe (PLAN.md Phase 8, loop steps 1-2).

SQLite at ``IDEAS_DB_PATH`` with the shared ``IDEAS_SCHEMA`` from ``lab.research.contract``
(``lab.api`` reads the same file). Ideas move through ``IDEA_STATUSES`` as they are tested;
``dead`` is terminal, so a hypothesis that failed its holdout check can never be revived and
re-tuned. Sources are append-only and stored as untrusted by default: scraped pages are data,
never instructions.

Dedupe has three layers, cheapest first:

1. exact match on ``dedupe_key`` (sha256 of the normalised hypothesis text),
2. local near-duplicate: token-set Jaccard similarity >= ``NEAR_DUPLICATE_JACCARD``,
3. an optional ``duplicate_checker`` (the Jev dedupe gate). It fails open: an exception or an
   id that is not in the library counts as "not a duplicate".

A duplicate is not inserted; its sources are appended to the original idea so provenance is
kept, and :class:`AddResult` names the original.

Failure tags (PLAN.md Phase 13): when the orchestrator abandons an idea after a Critic verdict,
the Critic's reasons are stored as tags from the closed vocabulary :data:`FAILURE_TAGS` in the
``idea_failure_tags`` table (an agents-only migration, :data:`FAILURE_TAGS_SCHEMA`, applied on
open beside the shared ``IDEAS_SCHEMA``). :meth:`IdeaLibrary.family_failure_tags` counts them
per family across every run, so the Designer sees why earlier plans in the same family failed
before it proposes a new one. Tags are our own enum values, never scout text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from lab.research.contract import IDEA_STATUSES, IDEAS_DB_PATH, IDEAS_SCHEMA

__all__ = [
    "FAILURE_TAGS",
    "FAILURE_TAGS_SCHEMA",
    "FAILURE_TAG_MEANINGS",
    "NEAR_DUPLICATE_JACCARD",
    "AddResult",
    "DuplicateChecker",
    "Idea",
    "IdeaLibrary",
    "SourceRecord",
    "family_key",
    "jaccard",
    "normalise_text",
]

log = logging.getLogger(__name__)

NEAR_DUPLICATE_JACCARD = 0.8
TERMINAL_STATUSES: frozenset[str] = frozenset({"dead"})

_NON_WORD = re.compile(r"[^0-9a-z]+")

# Why a plan was abandoned, as the Critic tags it (one closed vocabulary shared by the Critic's
# output schema, the idea library, and the Designer prompt). ``other`` is the fallback when the
# Critic gave no tag.
FAILURE_TAGS: tuple[str, ...] = (
    "cost_bound",
    "regime_only",
    "few_trades",
    "beta_only",
    "parameter_spike",
    "no_signal",
    "walk_forward_decay",
    "fragile_costs",
    "other",
)
FAILURE_TAG_MEANINGS: dict[str, str] = {
    "cost_bound": "gross edge exists but fees and slippage eat it (net Sharpe near or below zero)",
    "regime_only": "works in one regime or period only (one bull run, one crash)",
    "few_trades": "too few trades for the statistics to mean anything",
    "beta_only": "returns are explained by exposure to the market (lagged or levered buy-and-hold)",
    "parameter_spike": "only an isolated parameter spike works; neighbours do not",
    "no_signal": "no edge at all: indistinguishable from the random-entry null",
    "walk_forward_decay": "in-sample edge that decays or vanishes out of sample in walk-forward",
    "fragile_costs": "positive at base costs but negative under the cost stress (x2 costs)",
    "other": "none of the above; the Critic's reasons say why",
}

# Agents-only migration, applied after the shared IDEAS_SCHEMA (the lab API ignores it). One row
# per (idea, run, tag): re-recording the same verdict is idempotent.
FAILURE_TAGS_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS idea_failure_tags (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        idea_id     INTEGER NOT NULL REFERENCES ideas(id),
        run_id      TEXT,
        created_at  TEXT NOT NULL,
        tag         TEXT NOT NULL,
        UNIQUE (idea_id, run_id, tag)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idea_failure_tags_idea ON idea_failure_tags (idea_id)",
)

_IDEA_COLUMNS = (
    "id, created_at, run_id, family, title, hypothesis, rationale, dedupe_key, status, "
    "verdict_note, plan_hashes"
)


@dataclass(frozen=True)
class SourceRecord:
    """A web source behind an idea. ``screen`` holds the source-gate answers (e.g. Jev's
    ``is_specific_rule`` / ``contains_injected_instructions`` probabilities)."""

    url: str
    title: str | None = None
    screen: Mapping[str, Any] = field(default_factory=dict)
    untrusted: bool = True


@dataclass(frozen=True)
class Idea:
    id: int
    created_at: str
    run_id: str | None
    family: str
    title: str
    hypothesis: str
    rationale: str
    dedupe_key: str
    status: str
    verdict_note: str | None
    plan_hashes: list[str]
    sources: list[SourceRecord]


@dataclass(frozen=True)
class AddResult:
    """``idea_id`` is the new row's id, or None when the idea was a duplicate (not inserted);
    then ``duplicate_of`` is the original idea and ``reason`` says which layer matched."""

    idea_id: int | None
    duplicate_of: int | None
    reason: str | None


# (hypothesis, [(id, hypothesis), ...]) -> id of the duplicate, or None.
DuplicateChecker = Callable[[str, Sequence[tuple[int, str]]], int | None]


def normalise_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Digits are kept, so "EMA 20" and
    "EMA 50" stay different ideas."""
    return " ".join(_NON_WORD.sub(" ", text.lower()).split())


def family_key(family: str) -> str:
    """The family as compared across runs: :func:`normalise_text` (case, punctuation, and
    whitespace do not split a family)."""
    return normalise_text(family)


def jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity of two texts after :func:`normalise_text`."""
    ta, tb = set(normalise_text(a).split()), set(normalise_text(b).split())
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value.strip()


class IdeaLibrary:
    """SQLite-backed idea library. Use as a context manager or call :meth:`close`."""

    def __init__(self, path: Path | str = IDEAS_DB_PATH) -> None:
        target = str(path)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(target, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if target != ":memory:":
            # The lab API reads this file while a run writes it.
            self._conn.execute("PRAGMA journal_mode = WAL")
        with self._conn:
            for statement in (*IDEAS_SCHEMA, *FAILURE_TAGS_SCHEMA):
                self._conn.execute(statement)

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> IdeaLibrary:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ dedupe

    @staticmethod
    def dedupe_key(text: str) -> str:
        """sha256 hex of the normalised text (see :func:`normalise_text`)."""
        return hashlib.sha256(normalise_text(text).encode("utf-8")).hexdigest()

    def _existing_hypotheses(self) -> list[tuple[int, str]]:
        rows = self._conn.execute("SELECT id, hypothesis FROM ideas ORDER BY id").fetchall()
        return [(int(r["id"]), str(r["hypothesis"])) for r in rows]

    def _find_duplicate(
        self, hypothesis: str, key: str, checker: DuplicateChecker | None
    ) -> tuple[int, str] | None:
        row = self._conn.execute("SELECT id FROM ideas WHERE dedupe_key = ?", (key,)).fetchone()
        if row is not None:
            return int(row["id"]), "exact: same normalised hypothesis"

        existing = self._existing_hypotheses()
        best: tuple[float, int] | None = None
        for idea_id, other in existing:
            score = jaccard(hypothesis, other)
            if score >= NEAR_DUPLICATE_JACCARD and (best is None or score > best[0]):
                best = (score, idea_id)
        if best is not None:
            return best[1], f"near: token Jaccard {best[0]:.2f} >= {NEAR_DUPLICATE_JACCARD}"

        if checker is not None and existing:
            try:
                found = checker(hypothesis, existing)
            except Exception:  # the Jev gate fails open, never blocks a run
                log.warning("duplicate checker failed; treating idea as new", exc_info=True)
                return None
            known = {idea_id for idea_id, _ in existing}
            if isinstance(found, int) and not isinstance(found, bool) and found in known:
                return found, "checker: duplicate gate matched an existing idea"
            if found is not None:
                log.warning("duplicate checker returned unknown id %r; ignoring", found)
        return None

    # ------------------------------------------------------------------ writes

    def _insert_sources(self, idea_id: int, sources: Sequence[SourceRecord]) -> None:
        now = _now()
        for src in sources:
            url = _require_text("source url", src.url)
            self._conn.execute(
                "INSERT INTO sources (idea_id, created_at, url, title, screen_json, untrusted)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    idea_id,
                    now,
                    url,
                    src.title,
                    json.dumps(dict(src.screen), sort_keys=True, default=str),
                    1 if src.untrusted else 0,
                ),
            )

    def add_idea(
        self,
        *,
        run_id: str | None,
        family: str,
        title: str,
        hypothesis: str,
        rationale: str,
        sources: Sequence[SourceRecord] = (),
        duplicate_checker: DuplicateChecker | None = None,
    ) -> AddResult:
        """Insert a hypothesis unless it duplicates one already in the library."""
        family = _require_text("family", family)
        title = _require_text("title", title)
        hypothesis = _require_text("hypothesis", hypothesis)
        rationale = _require_text("rationale", rationale)
        key = self.dedupe_key(hypothesis)

        dup = self._find_duplicate(hypothesis, key, duplicate_checker)
        if dup is not None:
            original, reason = dup
            with self._conn:
                self._insert_sources(original, sources)
            return AddResult(idea_id=None, duplicate_of=original, reason=reason)

        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO ideas (created_at, run_id, family, title, hypothesis, rationale,"
                    " dedupe_key, status, plan_hashes) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', '[]')",
                    (_now(), run_id, family, title, hypothesis, rationale, key),
                )
                idea_id = int(cur.lastrowid or 0)
                self._insert_sources(idea_id, sources)
        except sqlite3.IntegrityError:
            # Another writer inserted the same key between the check and the insert.
            row = self._conn.execute("SELECT id FROM ideas WHERE dedupe_key = ?", (key,)).fetchone()
            if row is None:
                raise
            with self._conn:
                self._insert_sources(int(row["id"]), sources)
            return AddResult(None, int(row["id"]), "exact: same normalised hypothesis")
        return AddResult(idea_id=idea_id, duplicate_of=None, reason=None)

    def _status_of(self, idea_id: int) -> str:
        row = self._conn.execute("SELECT status FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        if row is None:
            raise KeyError(f"no idea with id {idea_id}")
        return str(row["status"])

    def set_status(self, idea_id: int, status: str, note: str | None = None) -> None:
        """Move an idea to ``status``. ``dead`` is terminal: a dead idea cannot be revived."""
        if status not in IDEA_STATUSES:
            raise ValueError(f"unknown idea status {status!r}; expected one of {IDEA_STATUSES}")
        current = self._status_of(idea_id)
        if current in TERMINAL_STATUSES and status != current:
            raise ValueError(
                f"idea {idea_id} is {current!r}, which is terminal (no re-tuning after a failed "
                "holdout check)"
            )
        with self._conn:
            if note is None:
                self._conn.execute("UPDATE ideas SET status = ? WHERE id = ?", (status, idea_id))
            else:
                self._conn.execute(
                    "UPDATE ideas SET status = ?, verdict_note = ? WHERE id = ?",
                    (status, note, idea_id),
                )

    def attach_plan(self, idea_id: int, plan_hash: str) -> None:
        """Record that ``plan_hash`` was designed for this idea (idempotent, order kept)."""
        plan_hash = _require_text("plan_hash", plan_hash)
        with self._conn:
            row = self._conn.execute(
                "SELECT plan_hashes FROM ideas WHERE id = ?", (idea_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no idea with id {idea_id}")
            hashes = list(json.loads(row["plan_hashes"] or "[]"))
            if plan_hash not in hashes:
                hashes.append(plan_hash)
                self._conn.execute(
                    "UPDATE ideas SET plan_hashes = ? WHERE id = ?",
                    (json.dumps(hashes), idea_id),
                )

    def add_failure_tags(
        self, idea_id: int, tags: Sequence[str], *, run_id: str | None = None
    ) -> None:
        """Record why ``idea_id`` was abandoned, as tags from :data:`FAILURE_TAGS`. Every tag is
        validated before anything is written; duplicates (same idea, run, and tag) are ignored.
        ``run_id`` defaults to the idea's own run."""
        if isinstance(tags, str):
            raise ValueError("tags must be a sequence of tag names, not a single string")
        clean: list[str] = []
        for tag in tags:
            if tag not in FAILURE_TAGS:
                raise ValueError(f"unknown failure tag {tag!r}; expected one of {FAILURE_TAGS}")
            if tag not in clean:
                clean.append(tag)
        row = self._conn.execute("SELECT run_id FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        if row is None:
            raise KeyError(f"no idea with id {idea_id}")
        run = run_id if run_id is not None else row["run_id"]
        now = _now()
        with self._conn:
            for tag in clean:
                self._conn.execute(
                    "INSERT OR IGNORE INTO idea_failure_tags (idea_id, run_id, created_at, tag)"
                    " VALUES (?, ?, ?, ?)",
                    (idea_id, run, now, tag),
                )

    # ------------------------------------------------------------------ reads

    def failure_tags(self, idea_id: int) -> list[str]:
        """The idea's failure tags, in the order first recorded, each once."""
        rows = self._conn.execute(
            "SELECT tag FROM idea_failure_tags WHERE idea_id = ? ORDER BY id", (idea_id,)
        ).fetchall()
        return list(dict.fromkeys(str(r["tag"]) for r in rows))

    def family_failure_tags(self, family: str) -> dict[str, int]:
        """``{tag: number of ideas}`` for every idea in ``family`` (compared by
        :func:`family_key`) across every run. An idea counts once per tag, however many runs
        tagged it. Most frequent first, ties in :data:`FAILURE_TAGS` order."""
        key = family_key(family)
        if not key:
            return {}
        rows = self._conn.execute(
            "SELECT DISTINCT t.idea_id, t.tag, i.family FROM idea_failure_tags t"
            " JOIN ideas i ON i.id = t.idea_id"
        ).fetchall()
        counts: dict[str, int] = {}
        for r in rows:
            if family_key(str(r["family"])) == key:
                counts[str(r["tag"])] = counts.get(str(r["tag"]), 0) + 1
        order = {t: i for i, t in enumerate(FAILURE_TAGS)}
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], order.get(kv[0], 99))))

    def _sources_for(self, idea_id: int) -> list[SourceRecord]:
        rows = self._conn.execute(
            "SELECT url, title, screen_json, untrusted FROM sources WHERE idea_id = ? ORDER BY id",
            (idea_id,),
        ).fetchall()
        return [
            SourceRecord(
                url=str(r["url"]),
                title=r["title"],
                screen=json.loads(r["screen_json"] or "{}"),
                untrusted=bool(r["untrusted"]),
            )
            for r in rows
        ]

    def _to_idea(self, row: sqlite3.Row) -> Idea:
        idea_id = int(row["id"])
        return Idea(
            id=idea_id,
            created_at=str(row["created_at"]),
            run_id=row["run_id"],
            family=str(row["family"]),
            title=str(row["title"]),
            hypothesis=str(row["hypothesis"]),
            rationale=str(row["rationale"]),
            dedupe_key=str(row["dedupe_key"]),
            status=str(row["status"]),
            verdict_note=row["verdict_note"],
            plan_hashes=list(json.loads(row["plan_hashes"] or "[]")),
            sources=self._sources_for(idea_id),
        )

    def get(self, idea_id: int) -> Idea:
        row = self._conn.execute(
            f"SELECT {_IDEA_COLUMNS} FROM ideas WHERE id = ?", (idea_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no idea with id {idea_id}")
        return self._to_idea(row)

    def list(self, *, run_id: str | None = None, status: str | None = None) -> list[Idea]:
        """Ideas in insertion order, optionally filtered by run and status."""
        if status is not None and status not in IDEA_STATUSES:
            raise ValueError(f"unknown idea status {status!r}")
        clauses: list[str] = []
        args: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            args.append(run_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {_IDEA_COLUMNS} FROM ideas{where} ORDER BY id", args
        ).fetchall()
        return [self._to_idea(r) for r in rows]
