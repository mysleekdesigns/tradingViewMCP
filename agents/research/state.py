"""Run-state files for the research orchestrator.

* ``RUN_STATE_DIR/<run_id>.json``: the contract :class:`lab.research.contract.RunState` read by
  the Research page. Rewritten atomically (temp file in the same directory + ``os.replace``) so
  a reader never sees a half-written file.
* ``RUN_STATE_DIR/<run_id>.resume.json``: an orchestrator-private sidecar (:class:`ResumeState`)
  with per-idea progress, SDK session ids, dollars spent, and critic lessons, so ``--resume``
  continues with the next unfinished idea. Budgets on resume still come from the ledger's frozen
  snapshot and the ledger's own counts; the sidecar only restores dollars and hypotheses, which
  the ledger cannot see. Before every model call the call's dollar cap is persisted as a
  reservation (``usd_reserved``) and settled to the actual cost afterwards, so a crash mid-call
  is charged at the cap when the run resumes.

Nothing here stores secrets: only ids, counts, text the agents produced, and sanitized snippets.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast, get_args

from lab.research.contract import (
    RESEARCH_RUN_ID_PATTERN,
    RUN_STATE_DIR,
    BudgetUsage,
    RunBudget,
    RunOutcome,
    RunPhase,
    RunState,
)

MAX_EVENTS = 200
MAX_LESSONS = 30
RUN_STATE_KEYS: frozenset[str] = frozenset(RunState.__annotations__)
BUDGET_USAGE_KEYS: frozenset[str] = frozenset(BudgetUsage.__annotations__)
RUN_PHASES: tuple[str, ...] = get_args(RunPhase)
RUN_OUTCOMES: tuple[str, ...] = get_args(RunOutcome)
_RUN_ID_RE = re.compile(RESEARCH_RUN_ID_PATTERN)


def iso_now(now: datetime | None = None) -> str:
    """ISO-8601 UTC, second precision, ``Z`` suffix."""
    t = (now or datetime.now(UTC)).astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def check_run_id(run_id: str) -> str:
    """Server-assigned research run ids only; also keeps file names inside the state dir."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError(f"run_id {run_id!r} does not match {RESEARCH_RUN_ID_PATTERN}")
    return run_id


def atomic_write_json(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` via a temp file in the same directory + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, allow_nan=False)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def validate_run_state(state: Any) -> RunState:
    """Raise ``ValueError`` unless ``state`` has exactly the contract ``RunState`` shape."""
    if not isinstance(state, dict):
        raise ValueError(f"RunState must be a dict, got {type(state).__name__}")
    keys = set(state)
    if keys != RUN_STATE_KEYS:
        raise ValueError(
            f"RunState keys mismatch: missing {sorted(RUN_STATE_KEYS - keys)}, "
            f"unknown {sorted(keys - RUN_STATE_KEYS)}"
        )
    check_run_id(state["run_id"])
    used = state["used"]
    if not isinstance(used, dict) or set(used) != BUDGET_USAGE_KEYS:
        raise ValueError(f"RunState.used must have keys {sorted(BUDGET_USAGE_KEYS)}")
    if state["phase"] not in RUN_PHASES:
        raise ValueError(f"RunState.phase {state['phase']!r} not in {RUN_PHASES}")
    if state["outcome"] not in RUN_OUTCOMES:
        raise ValueError(f"RunState.outcome {state['outcome']!r} not in {RUN_OUTCOMES}")
    if not isinstance(state["events"], list) or len(state["events"]) > MAX_EVENTS:
        raise ValueError(f"RunState.events must be a list of at most {MAX_EVENTS}")
    for ev in state["events"]:
        if not isinstance(ev, dict) or set(ev) != {"at", "kind", "message"}:
            raise ValueError(f"RunState event {ev!r} must have keys at, kind, message")
    if not isinstance(state["symbols"], list) or not state["symbols"]:
        raise ValueError("RunState.symbols must be a non-empty list")
    if not isinstance(state["candidates"], list):
        raise ValueError("RunState.candidates must be a list")
    return cast(RunState, state)


def new_run_state(
    *,
    run_id: str,
    symbols: list[str],
    interval: str,
    budget: RunBudget,
    used: BudgetUsage,
    journal_path: str | None,
    now: datetime | None = None,
) -> RunState:
    at = iso_now(now)
    return RunState(
        run_id=check_run_id(run_id),
        symbols=list(symbols),
        interval=interval,
        started_at=at,
        updated_at=at,
        phase="starting",
        outcome="running",
        budget=dict(asdict(budget)),
        used=used,
        current_idea_id=None,
        candidates=[],
        journal_path=journal_path,
        events=[],
    )


def add_event(state: RunState, kind: str, message: str, now: datetime | None = None) -> None:
    """Append an event (newest last), keeping only the latest :data:`MAX_EVENTS`."""
    at = iso_now(now)
    state["events"].append({"at": at, "kind": kind, "message": message[:2000]})
    if len(state["events"]) > MAX_EVENTS:
        del state["events"][: len(state["events"]) - MAX_EVENTS]
    state["updated_at"] = at


# --------------------------------------------------------------------------- resume sidecar

IdeaProgressStatus = Literal["pending", "in_progress", "done"]


@dataclass
class IdeaProgress:
    """Per-idea progress. ``context`` holds the screened, sanitized scout text the designer
    sees (hypothesis, rationale, building blocks, kept source snippets)."""

    idea_id: int
    title: str
    status: IdeaProgressStatus = "pending"
    refinements: int = 0
    plan_hashes: list[str] = field(default_factory=list)
    final_status: str | None = None  # idea-library status when done
    reason: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    last_plan: dict[str, Any] | None = None
    feedback: list[str] = field(default_factory=list)  # critic reasons for the next refinement


@dataclass
class ResumeState:
    run_id: str
    symbols: list[str]
    interval: str
    family: str | None
    label: str
    journal_path: str | None = None  # absolute
    usd_spent: float = 0.0
    # Dollar reservation for the model call in flight: the call's cap, written before the call
    # starts and settled to the actual cost after it. A crash mid-call leaves it here, and
    # ``--resume`` charges it in full (an unknown cost is never counted as zero).
    usd_reserved: float = 0.0
    reserved_key: str | None = None
    hypotheses_started: int = 0
    scouted: bool = False
    ideas: list[IdeaProgress] = field(default_factory=list)
    sessions: dict[str, str] = field(default_factory=dict)  # "<role>:<idea>:<round>" -> id
    lessons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # [{"candidate": CandidateSummary, "criteria_rows": [...]}]
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # Largest DSR N the criteria evaluator reported in this run (distinct plans across every
    # research run, the global search count); None until the first evaluation.
    dsr_n: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ResumeState:
        data = dict(raw)
        data["ideas"] = [IdeaProgress(**i) for i in data.get("ideas", [])]
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"resume file has unknown keys {unknown}")
        return cls(**data)

    def add_lessons(self, lessons: list[str]) -> None:
        for lesson in lessons:
            text = str(lesson).strip()
            if text and text not in self.lessons:
                self.lessons.append(text[:500])
        del self.lessons[: max(0, len(self.lessons) - MAX_LESSONS)]


class RunStateStore:
    """Reads and writes run-state files under ``state_dir`` (default ``RUN_STATE_DIR``)."""

    def __init__(self, state_dir: Path | str = RUN_STATE_DIR) -> None:
        self.state_dir = Path(state_dir)

    def path(self, run_id: str) -> Path:
        return self.state_dir / f"{check_run_id(run_id)}.json"

    def resume_path(self, run_id: str) -> Path:
        return self.state_dir / f"{check_run_id(run_id)}.resume.json"

    def write(self, state: RunState) -> Path:
        validate_run_state(state)
        p = self.path(state["run_id"])
        atomic_write_json(p, state)
        return p

    def read(self, run_id: str) -> RunState:
        p = self.path(run_id)
        if not p.is_file():
            raise FileNotFoundError(f"no run state for {run_id} at {p}")
        return validate_run_state(json.loads(p.read_text(encoding="utf-8")))

    def write_resume(self, resume: ResumeState) -> Path:
        p = self.resume_path(resume.run_id)
        atomic_write_json(p, resume.to_dict())
        return p

    def read_resume(self, run_id: str) -> ResumeState:
        p = self.resume_path(run_id)
        if not p.is_file():
            raise FileNotFoundError(f"no resume file for {run_id} at {p}; cannot resume")
        return ResumeState.from_dict(json.loads(p.read_text(encoding="utf-8")))
