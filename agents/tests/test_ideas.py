"""Idea library: dedupe layers, statuses, plans, and the shared SQLite schema."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from lab.research.contract import IDEA_STATUSES, IDEAS_SCHEMA
from research.ideas import (
    NEAR_DUPLICATE_JACCARD,
    IdeaLibrary,
    SourceRecord,
    jaccard,
    normalise_text,
)

HYP = "Trend persists after volatility compression on 1h bars when ATR 14 is in its lowest decile"


def _add(lib: IdeaLibrary, hypothesis: str = HYP, **kw: object) -> object:
    args: dict[str, object] = {
        "run_id": "r-20261024-120000-abcdef",
        "family": "trend",
        "title": "Vol compression breakout",
        "hypothesis": hypothesis,
        "rationale": "Breakouts after quiet periods are driven by forced repositioning.",
    }
    args.update(kw)
    return lib.add_idea(**args)  # type: ignore[arg-type]


@pytest.fixture
def lib(tmp_path: Path) -> IdeaLibrary:
    library = IdeaLibrary(tmp_path / "data" / "ideas.sqlite")
    yield library  # type: ignore[misc]
    library.close()


def test_normalise_and_dedupe_key() -> None:
    assert normalise_text("  EMA-20,  crosses   ABOVE ema 50! ") == "ema 20 crosses above ema 50"
    a = IdeaLibrary.dedupe_key("EMA-20 crosses above EMA 50.")
    b = IdeaLibrary.dedupe_key("ema 20   crosses above ema 50")
    assert a == b and re.fullmatch(r"[0-9a-f]{64}", a)
    # digits are kept: different lookbacks are different ideas
    assert IdeaLibrary.dedupe_key("ema 20 above ema 50") != IdeaLibrary.dedupe_key(
        "ema 20 above ema 100"
    )


def test_jaccard_known_values() -> None:
    assert jaccard("a b c d", "a b c d") == 1.0
    assert jaccard("a b c d", "a b c e") == pytest.approx(3 / 5)
    assert jaccard("a b", "c d") == 0.0


def test_new_idea_inserted_with_sources(lib: IdeaLibrary) -> None:
    res = _add(
        lib,
        sources=[
            SourceRecord(url="https://example.org/paper", title="Paper", screen={"p_hype": 0.1})
        ],
    )
    assert res.duplicate_of is None and res.reason is None and res.idea_id is not None
    idea = lib.get(res.idea_id)
    assert idea.status == "new" and idea.plan_hashes == [] and idea.hypothesis == HYP
    assert idea.dedupe_key == IdeaLibrary.dedupe_key(HYP)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", idea.created_at)
    assert idea.sources == [
        SourceRecord(url="https://example.org/paper", title="Paper", screen={"p_hype": 0.1})
    ]
    assert idea.sources[0].untrusted is True


def test_exact_duplicate_not_inserted(lib: IdeaLibrary) -> None:
    first = _add(lib)
    dup = _add(
        lib,
        hypothesis=HYP.upper() + "!!",
        title="Other title",
        sources=[SourceRecord(url="https://example.org/2")],
    )
    assert dup.idea_id is None and dup.duplicate_of == first.idea_id
    assert dup.reason is not None and dup.reason.startswith("exact")
    assert len(lib.list()) == 1
    # the duplicate's source is kept on the original for provenance
    assert [s.url for s in lib.get(first.idea_id).sources] == ["https://example.org/2"]


def test_near_duplicate_by_jaccard(lib: IdeaLibrary) -> None:
    base = "long when close crosses above the upper donchian channel of 20 bars and exit below"
    first = _add(lib, hypothesis=base)
    near = base + " it"  # one extra token: 14/15 >= 0.8
    assert jaccard(base, near) >= NEAR_DUPLICATE_JACCARD
    dup = _add(lib, hypothesis=near)
    assert dup.idea_id is None and dup.duplicate_of == first.idea_id
    assert dup.reason is not None and dup.reason.startswith("near")


def test_checker_gate_and_fail_open(lib: IdeaLibrary) -> None:
    first = _add(lib)
    other = "Funding rate extremes mean-revert within three days on perpetual futures"
    seen: list[tuple[str, Sequence[tuple[int, str]]]] = []

    def checker(hyp: str, existing: Sequence[tuple[int, str]]) -> int | None:
        seen.append((hyp, existing))
        return first.idea_id

    dup = _add(lib, hypothesis=other, duplicate_checker=checker)
    assert dup.idea_id is None and dup.duplicate_of == first.idea_id
    assert dup.reason is not None and dup.reason.startswith("checker")
    assert seen == [(other, [(first.idea_id, HYP)])]

    # returns None -> inserted
    res = _add(lib, hypothesis=other, duplicate_checker=lambda h, e: None)
    assert res.idea_id is not None and res.duplicate_of is None

    # gate is down -> fail open
    def broken(hyp: str, existing: Sequence[tuple[int, str]]) -> int | None:
        raise ConnectionError("jev down")

    third = "RSI crosses below 30 on 4h bars then mean reverts over the next day"
    assert _add(lib, hypothesis=third, duplicate_checker=broken).idea_id is not None

    # unknown id from the gate is ignored
    fourth = "Weekend volume dries up so breakouts on Saturday fail more often than weekday"
    assert _add(lib, hypothesis=fourth, duplicate_checker=lambda h, e: 999).idea_id is not None


def test_no_duplicate_distinct_ideas(lib: IdeaLibrary) -> None:
    a = _add(lib)
    b = _add(lib, hypothesis="Volatility targeting at 20% annualised reduces drawdown in bears")
    assert a.idea_id != b.idea_id and None not in (a.idea_id, b.idea_id)
    assert [i.id for i in lib.list()] == [a.idea_id, b.idea_id]


def test_statuses_and_terminal_dead(lib: IdeaLibrary) -> None:
    idea_id = _add(lib).idea_id
    with pytest.raises(ValueError, match="unknown idea status"):
        lib.set_status(idea_id, "winner")
    lib.set_status(idea_id, "testing")
    lib.set_status(idea_id, "promising", note="smooth parameter surface")
    assert lib.get(idea_id).verdict_note == "smooth parameter surface"
    lib.set_status(idea_id, "dead", note="holdout check spent; idea marked dead")
    for status in IDEA_STATUSES:
        if status != "dead":
            with pytest.raises(ValueError, match="terminal"):
                lib.set_status(idea_id, status)
    assert lib.get(idea_id).status == "dead"
    with pytest.raises(KeyError):
        lib.set_status(12345, "testing")


def test_list_filters(lib: IdeaLibrary) -> None:
    a = _add(lib, run_id="r-1").idea_id
    b = _add(lib, run_id="r-2", hypothesis="Totally different idea about funding rates").idea_id
    lib.set_status(b, "testing")
    assert [i.id for i in lib.list(run_id="r-1")] == [a]
    assert [i.id for i in lib.list(status="testing")] == [b]
    assert lib.list(run_id="r-1", status="testing") == []
    with pytest.raises(ValueError):
        lib.list(status="bogus")


def test_attach_plan_idempotent(lib: IdeaLibrary) -> None:
    idea_id = _add(lib).idea_id
    lib.attach_plan(idea_id, "aaa111")
    lib.attach_plan(idea_id, "bbb222")
    lib.attach_plan(idea_id, "aaa111")
    assert lib.get(idea_id).plan_hashes == ["aaa111", "bbb222"]
    with pytest.raises(KeyError):
        lib.attach_plan(999, "ccc")


def test_rejects_empty_fields(lib: IdeaLibrary) -> None:
    with pytest.raises(ValueError, match="hypothesis"):
        _add(lib, hypothesis="   ")


def test_db_readable_with_plain_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "ideas.sqlite"
    with IdeaLibrary(path) as lib:
        idea_id = _add(lib, sources=[SourceRecord(url="https://e.org", screen={"x": 1})]).idea_id
        lib.attach_plan(idea_id, "abc")

    # Columns match the shared schema exactly (the lab API reads this file).
    ref = sqlite3.connect(":memory:")
    for stmt in IDEAS_SCHEMA:
        ref.execute(stmt)
    conn = sqlite3.connect(path)
    for table in ("ideas", "sources"):
        got = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        want = [r[1] for r in ref.execute(f"PRAGMA table_info({table})")]
        assert got == want
    row = conn.execute("SELECT status, plan_hashes, dedupe_key FROM ideas").fetchone()
    assert row[0] == "new" and json.loads(row[1]) == ["abc"]
    src = conn.execute("SELECT idea_id, url, screen_json, untrusted FROM sources").fetchone()
    assert src == (idea_id, "https://e.org", '{"x": 1}', 1)
    conn.close()


def test_memory_db() -> None:
    with IdeaLibrary(":memory:") as lib:
        assert _add(lib).idea_id == 1
