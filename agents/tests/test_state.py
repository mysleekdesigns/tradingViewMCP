"""Run-state files: contract shape, atomic writes, and the resume sidecar."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lab.research.contract import BudgetUsage, RunBudget, RunState
from research import state as state_mod
from research.state import (
    MAX_EVENTS,
    IdeaProgress,
    ResumeState,
    RunStateStore,
    add_event,
    new_run_state,
    validate_run_state,
)

RUN_ID = "r-20261024-120000-abc123"


def _state() -> RunState:
    return new_run_state(
        run_id=RUN_ID,
        symbols=["BTCUSDT"],
        interval="4h",
        budget=RunBudget(2, 3, 100, 1, 5.0),
        used=BudgetUsage(hypotheses=0, proposals=0, backtests=0, holdout_checks=0, usd=0.0),
        journal_path="agents/journal/x.md",
    )


def test_new_state_has_exactly_the_contract_keys() -> None:
    s = _state()
    assert set(s) == set(RunState.__annotations__)
    assert set(s["used"]) == set(BudgetUsage.__annotations__)
    validate_run_state(s)


def test_validate_rejects_extra_or_missing_keys_and_bad_values() -> None:
    s = _state()
    with pytest.raises(ValueError, match="unknown"):
        validate_run_state({**s, "secret": "x"})
    broken = dict(s)
    del broken["phase"]
    with pytest.raises(ValueError, match="missing"):
        validate_run_state(broken)
    with pytest.raises(ValueError, match="outcome"):
        validate_run_state({**s, "outcome": "great"})
    with pytest.raises(ValueError, match="run_id"):
        validate_run_state({**s, "run_id": "../../etc/passwd"})


def test_events_are_capped_newest_last() -> None:
    s = _state()
    for i in range(MAX_EVENTS + 25):
        add_event(s, "design", f"event {i}")
    assert len(s["events"]) == MAX_EVENTS
    assert s["events"][-1]["message"] == f"event {MAX_EVENTS + 24}"
    assert s["events"][0]["message"] == "event 25"
    validate_run_state(s)


def test_store_round_trip_and_atomic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = RunStateStore(tmp_path)
    s = _state()
    p = store.write(s)
    assert p == tmp_path / f"{RUN_ID}.json"
    assert store.read(RUN_ID) == json.loads(p.read_text())

    # A failure mid-write leaves the old file intact and no temp files behind.
    s2 = dict(s)
    s2["phase"] = "critic"

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(state_mod.os, "replace", boom)
    with pytest.raises(OSError):
        store.write(s2)  # type: ignore[arg-type]
    monkeypatch.setattr(state_mod.os, "replace", os.replace)
    assert store.read(RUN_ID)["phase"] == "starting"
    assert [f.name for f in tmp_path.iterdir()] == [f"{RUN_ID}.json"]


def test_write_refuses_invalid_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RunStateStore(tmp_path).write({**_state(), "extra": 1})  # type: ignore[typeddict-unknown-key]
    assert not list(tmp_path.iterdir())


def test_resume_sidecar_round_trip(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    r = ResumeState(
        run_id=RUN_ID,
        symbols=["BTCUSDT"],
        interval="4h",
        family=None,
        label="t",
        usd_spent=1.25,
        hypotheses_started=1,
        scouted=True,
        ideas=[IdeaProgress(idea_id=3, title="a", status="in_progress", refinements=1)],
        sessions={"scout": "s1"},
    )
    store.write_resume(r)
    back = store.read_resume(RUN_ID)
    assert back == r
    assert isinstance(back.ideas[0], IdeaProgress)
    with pytest.raises(FileNotFoundError, match="cannot resume"):
        store.read_resume("r-20261024-120000-ffffff")


def test_lessons_dedupe_and_cap() -> None:
    r = ResumeState(run_id=RUN_ID, symbols=["X"], interval="4h", family=None, label="t")
    r.add_lessons(["a", "a", " ", *[f"l{i}" for i in range(40)]])
    assert r.lessons.count("a") <= 1
    assert len(r.lessons) == state_mod.MAX_LESSONS
    assert r.lessons[-1] == "l39"


def test_resume_reservation_round_trips_and_old_files_still_load(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path)
    r = ResumeState(
        run_id=RUN_ID,
        symbols=["BTCUSDT"],
        interval="4h",
        family=None,
        label="t",
        usd_reserved=2.5,
        reserved_key="designer:1:0",
        dsr_n=12,
    )
    store.write_resume(r)
    assert store.read_resume(RUN_ID) == r
    old = r.to_dict()
    for key in ("usd_reserved", "reserved_key", "dsr_n"):
        del old[key]  # a sidecar written before these fields existed
    back = ResumeState.from_dict(old)
    assert back.usd_reserved == 0.0 and back.reserved_key is None and back.dsr_n is None
