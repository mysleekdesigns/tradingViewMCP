"""Jev shadow decision log (PLAN.md Phase 17): hash-chain round trip, tamper detection (edit,
delete, reorder, torn write, tail deletion via expect_head), append refusal on a broken chain,
genesis rules, NaN refusal, and the deadline checks (fill_open + grace, owner decision)."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import threading
from pathlib import Path
from typing import Any

import pytest

from research.paper.shadow_log import (
    DEFAULT_GRACE_S,
    GENESIS_PREV,
    ChainError,
    DecisionLog,
    canonical_json,
    check_deadlines,
    row_sha256,
    snapshot_sha256,
    verify_chain,
)

INTERVAL = 4 * 3600
START = 1_793_318_400  # an arbitrary 4h boundary
SNAPSHOT = {"ret_1": 0.0012, "ret_6": -0.0041, "rsi": 47.3, "atr_pct": 1.8}


def genesis(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": "genesis",
        "written_at": START - 3600.5,
        "start": START,
        "interval": "4h",
        "grace_s": DEFAULT_GRACE_S,
        "prereg_journal": "agents/journal/2026-10-29-hc-jev-shadow.md",
        "prereg_sha256": "a" * 64,
        "candidates": ["hc-btc", "hc-eth"],
        "model": "jev-test",
        "snapshot_version": 1,
        "analysis": {"metric": "mean net trade return, take vs skip", "min_trades": 30},
    }
    row.update(over)
    return row


def bar(fill_open: int, written_at: float, status: str = "judged", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": "bar",
        "signal_bar": fill_open - INTERVAL,
        "fill_open": fill_open,
        "written_at": written_at,
        "status": status,
        "checked": ["hc-btc", "hc-eth"],
        "signals": ["hc-btc"] if status == "judged" else [],
    }
    row.update(over)
    return row


def decision(
    fill_open: int, written_at: float, status: str = "judged", **over: Any
) -> dict[str, Any]:
    judged = status == "judged"
    row: dict[str, Any] = {
        "kind": "decision",
        "signal_bar": fill_open - INTERVAL,
        "fill_open": fill_open,
        "candidate": "hc-btc",
        "written_at": written_at,
        "status": status,
        "snapshot": dict(SNAPSHOT) if status != "missed" else None,
        "snapshot_sha256": snapshot_sha256(SNAPSHOT) if status != "missed" else None,
        "verdict": {"choice": "take", "noul": 0.2, "score": 0.7} if judged else None,
        "model": "jev-test" if judged else None,
        "latency_ms": 1234 if judged else None,
        "reason": None if judged else f"{status} for test",
    }
    row.update(over)
    return row


def sample_rows() -> list[dict[str, Any]]:
    """Genesis + two bars, each with a judged and an unavailable decision, + a missed bar."""
    f1, f2, f3 = START, START + INTERVAL, START + 2 * INTERVAL
    return [
        genesis(),
        bar(f1, f1 + 12.5),
        decision(f1, f1 + 13.0),
        bar(f2, f2 + 20.0),
        decision(f2, f2 + 21.0, status="unavailable", candidate="hc-btc"),
        bar(f3, f3 + DEFAULT_GRACE_S + 5.0, status="missed", checked=[], signals=[]),
    ]


@pytest.fixture
def log(tmp_path: Path) -> DecisionLog:
    log = DecisionLog(tmp_path / "jev-shadow" / "decisions.jsonl")
    log.append_many(sample_rows())
    return log


def lines_of(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


# --- format and round trip -------------------------------------------------------------------


def test_canonical_json_is_sorted_compact_and_utf8() -> None:
    assert canonical_json({"b": 1, "a": "é", "c": [1.5, None]}) == '{"a":"é","b":1,"c":[1.5,null]}'


def test_row_sha256_ignores_its_own_sha256_key() -> None:
    row = {"kind": "bar", "seq": 1, "prev_sha256": GENESIS_PREV}
    expected = hashlib.sha256(canonical_json(row).encode()).hexdigest()
    assert row_sha256(row) == expected
    assert row_sha256({**row, "sha256": "x" * 64}) == expected


def test_round_trip_assigns_seq_prev_and_sha(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "deep" / "dir" / "decisions.jsonl")
    assert log.rows() == []
    first = log.append(genesis())
    stored = log.append_many(sample_rows()[1:])
    rows = log.rows()
    assert rows == [first, *stored]
    assert [r["seq"] for r in rows] == list(range(len(sample_rows())))
    assert rows[0]["prev_sha256"] == GENESIS_PREV
    for prev, row in itertools.pairwise(rows):
        assert row["prev_sha256"] == prev["sha256"]
    for row in rows:
        assert row["sha256"] == row_sha256(row)
    for raw, row in zip(sample_rows(), rows, strict=True):
        assert {k: row[k] for k in raw} == raw
    assert log.lock_path.name == "decisions.jsonl.lock"
    assert log.lock_path.exists()


def test_file_is_one_canonical_line_per_row_ending_in_newline(log: DecisionLog) -> None:
    text = log.path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    rows = log.rows()
    assert text == "".join(canonical_json(r) + "\n" for r in rows)


def test_append_only_grows_the_file_and_keeps_the_prefix(log: DecisionLog) -> None:
    before = log.path.read_bytes()
    f4 = START + 3 * INTERVAL
    log.append(bar(f4, f4 + 1.0, status="no_signal", signals=[]))
    after = log.path.read_bytes()
    assert after.startswith(before)
    assert after.count(b"\n") == before.count(b"\n") + 1


def test_append_many_of_nothing_writes_nothing(log: DecisionLog) -> None:
    before = log.path.read_bytes()
    assert log.append_many([]) == []
    assert log.path.read_bytes() == before


def test_missing_or_empty_file_is_an_empty_chain(tmp_path: Path) -> None:
    assert verify_chain(tmp_path / "absent.jsonl") == []
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert verify_chain(empty) == []
    assert verify_chain(empty, expect_head=GENESIS_PREV) == []


# --- tamper detection ------------------------------------------------------------------------


def _edit(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if value is None:
        return "x"
    if isinstance(value, list):
        return [*value, "x"]
    if isinstance(value, dict):
        return {**value, "zz_injected": 1}
    raise AssertionError(f"unhandled {value!r}")


def test_editing_any_field_of_any_row_fails_naming_the_line(log: DecisionLog) -> None:
    original = lines_of(log.path)
    cases = 0
    for index, line in enumerate(original):
        row = json.loads(line)
        for key in row:
            edited = dict(row)
            edited[key] = _edit(row[key])
            tampered = [*original]
            tampered[index] = canonical_json(edited) + "\n"
            log.path.write_text("".join(tampered), encoding="utf-8")
            with pytest.raises(ChainError, match=rf"line {index + 1}\b"):
                verify_chain(log.path)
            cases += 1
    assert cases > 50


def test_editing_a_field_and_rehashing_the_row_still_fails(log: DecisionLog) -> None:
    original = lines_of(log.path)
    row = json.loads(original[2])
    row["verdict"] = {"choice": "skip", "noul": 0.9, "score": 0.1}
    row["sha256"] = row_sha256(row)
    log.path.write_text(
        "".join([*original[:2], canonical_json(row) + "\n", *original[3:]]), encoding="utf-8"
    )
    with pytest.raises(ChainError, match=r"line 4\b.*prev_sha256"):
        verify_chain(log.path)


def test_whitespace_only_edit_fails(log: DecisionLog) -> None:
    original = lines_of(log.path)
    original[1] = original[1].replace(",", ", ", 1)
    log.path.write_text("".join(original), encoding="utf-8")
    with pytest.raises(ChainError, match=r"line 2\b.*canonical"):
        verify_chain(log.path)


@pytest.mark.parametrize("deleted", [0, 1, 2, 3, 4])
def test_deleting_a_row_other_than_the_last_fails(log: DecisionLog, deleted: int) -> None:
    original = lines_of(log.path)
    log.path.write_text("".join(original[:deleted] + original[deleted + 1 :]), encoding="utf-8")
    with pytest.raises(ChainError, match=rf"line {deleted + 1}\b"):
        verify_chain(log.path)


def test_deleting_the_last_row_is_caught_only_with_expect_head(log: DecisionLog) -> None:
    head = log.rows()[-1]["sha256"]
    original = lines_of(log.path)
    log.path.write_text("".join(original[:-1]), encoding="utf-8")
    assert len(verify_chain(log.path)) == len(original) - 1  # the chain alone cannot tell
    with pytest.raises(ChainError, match="expected head"):
        verify_chain(log.path, expect_head=head)


def test_deleting_every_row_is_caught_with_expect_head(log: DecisionLog) -> None:
    head = log.rows()[-1]["sha256"]
    log.path.write_bytes(b"")
    with pytest.raises(ChainError, match="empty log"):
        verify_chain(log.path, expect_head=head)


def test_expect_head_passes_on_the_intact_log(log: DecisionLog) -> None:
    rows = log.rows()
    assert verify_chain(log.path, expect_head=rows[-1]["sha256"]) == rows


def test_reordering_rows_fails(log: DecisionLog) -> None:
    original = lines_of(log.path)
    swapped = [*original]
    swapped[2], swapped[3] = swapped[3], swapped[2]
    log.path.write_text("".join(swapped), encoding="utf-8")
    with pytest.raises(ChainError, match=r"line 3\b.*seq"):
        verify_chain(log.path)


def test_duplicated_row_fails(log: DecisionLog) -> None:
    original = lines_of(log.path)
    log.path.write_text("".join([*original[:3], original[2], *original[3:]]), encoding="utf-8")
    with pytest.raises(ChainError, match=r"line 4\b.*seq"):
        verify_chain(log.path)


def test_torn_final_line_fails(log: DecisionLog) -> None:
    data = log.path.read_bytes()
    log.path.write_bytes(data[:-1])
    with pytest.raises(ChainError, match=r"line 6\b.*trailing newline"):
        verify_chain(log.path)
    log.path.write_bytes(data + b'{"kind":"bar","se')
    with pytest.raises(ChainError, match=r"line 7\b.*trailing newline"):
        verify_chain(log.path)


def test_invalid_json_and_non_object_lines_fail(log: DecisionLog) -> None:
    data = log.path.read_bytes()
    log.path.write_bytes(data + b"not json\n")
    with pytest.raises(ChainError, match=r"line 7\b.*not valid JSON"):
        verify_chain(log.path)
    log.path.write_bytes(data + b"[1,2]\n")
    with pytest.raises(ChainError, match=r"line 7\b.*not a JSON object"):
        verify_chain(log.path)
    log.path.write_bytes(data + b"\n")
    with pytest.raises(ChainError, match=r"line 7\b"):
        verify_chain(log.path)


def test_nan_written_by_hand_fails_verification(log: DecisionLog) -> None:
    original = lines_of(log.path)
    original[2] = original[2].replace('"latency_ms":1234', '"latency_ms":NaN')
    log.path.write_text("".join(original), encoding="utf-8")
    with pytest.raises(ChainError, match=r"line 3\b.*not valid JSON"):
        verify_chain(log.path)


def _chain_by_hand(rows: list[dict[str, Any]]) -> str:
    prev, out = GENESIS_PREV, []
    for seq, raw in enumerate(rows):
        body = {**raw, "seq": seq, "prev_sha256": prev}
        body["sha256"] = row_sha256(body)
        prev = body["sha256"]
        out.append(canonical_json(body) + "\n")
    return "".join(out)


def test_verify_rejects_a_hand_built_chain_without_genesis(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text(_chain_by_hand([bar(START, START + 1.0)]), encoding="utf-8")
    with pytest.raises(ChainError, match=r"line 1\b.*genesis"):
        verify_chain(path)


def test_verify_rejects_a_hand_built_second_genesis(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text(_chain_by_hand([genesis(), bar(START, START + 1.0), genesis()]), "utf-8")
    with pytest.raises(ChainError, match=r"line 3\b.*second genesis"):
        verify_chain(path)


# --- append refusals -------------------------------------------------------------------------


def test_appending_to_a_broken_chain_raises_and_leaves_the_file_byte_identical(
    log: DecisionLog,
) -> None:
    original = lines_of(log.path)
    row = json.loads(original[2])
    row["verdict"] = {"choice": "skip", "noul": 0.0, "score": 1.0}
    original[2] = canonical_json(row) + "\n"
    log.path.write_text("".join(original), encoding="utf-8")
    before = log.path.read_bytes()
    f4 = START + 3 * INTERVAL
    with pytest.raises(ChainError, match=r"line 3\b"):
        log.append(bar(f4, f4 + 1.0, status="no_signal", signals=[]))
    assert log.path.read_bytes() == before


def test_appending_after_a_torn_write_raises_and_leaves_the_file_byte_identical(
    log: DecisionLog,
) -> None:
    torn = log.path.read_bytes() + b'{"kind":"de'
    log.path.write_bytes(torn)
    with pytest.raises(ChainError, match="trailing newline"):
        log.append(bar(START + 3 * INTERVAL, START + 3 * INTERVAL + 1.0))
    assert log.path.read_bytes() == torn


def test_first_row_must_be_genesis(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decisions.jsonl")
    with pytest.raises(ValueError, match="must be kind 'genesis'"):
        log.append(bar(START, START + 1.0))
    assert not log.path.exists() or log.path.read_bytes() == b""


def test_second_genesis_is_rejected(log: DecisionLog) -> None:
    before = log.path.read_bytes()
    with pytest.raises(ValueError, match="genesis is allowed only as the first row"):
        log.append(genesis())
    assert log.path.read_bytes() == before


def test_two_genesis_rows_in_one_batch_are_rejected(tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decisions.jsonl")
    with pytest.raises(ValueError, match="genesis is allowed only as the first row"):
        log.append_many([genesis(), genesis()])
    assert log.rows() == []


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_rejected(log: DecisionLog, bad: float) -> None:
    before = log.path.read_bytes()
    f4 = START + 3 * INTERVAL
    with pytest.raises(ValueError, match="not JSON-serialisable"):
        log.append(decision(f4, f4 + 1.0, latency_ms=bad))
    with pytest.raises(ValueError, match="not JSON-serialisable"):
        log.append(decision(f4, f4 + 1.0, snapshot={"rsi": bad}))
    assert log.path.read_bytes() == before


def test_unserialisable_value_is_rejected(log: DecisionLog) -> None:
    with pytest.raises(ValueError, match="not JSON-serialisable"):
        log.append(bar(START + 3 * INTERVAL, START + 3 * INTERVAL + 1.0, checked={1, 2}))


@pytest.mark.parametrize("key", ["seq", "prev_sha256", "sha256"])
def test_rows_that_set_chain_keys_are_rejected(log: DecisionLog, key: str) -> None:
    with pytest.raises(ValueError, match=key):
        log.append({**bar(START + 3 * INTERVAL, START + 3 * INTERVAL + 1.0), key: 0})


def test_rows_without_kind_are_rejected(log: DecisionLog) -> None:
    row = bar(START + 3 * INTERVAL, START + 3 * INTERVAL + 1.0)
    del row["kind"]
    with pytest.raises(ValueError, match="kind"):
        log.append(row)
    with pytest.raises(ValueError, match="kind"):
        log.append({**row, "kind": ""})


def test_a_bad_row_in_a_batch_writes_nothing(log: DecisionLog) -> None:
    before = log.path.read_bytes()
    f4 = START + 3 * INTERVAL
    with pytest.raises(ValueError):
        log.append_many([bar(f4, f4 + 1.0), decision(f4, f4 + 2.0, latency_ms=math.nan)])
    assert log.path.read_bytes() == before


# --- deadlines -------------------------------------------------------------------------------


def test_clean_log_has_no_deadline_problems(log: DecisionLog) -> None:
    assert check_deadlines(log.rows(), DEFAULT_GRACE_S) == []


def test_verdict_at_fill_open_plus_899_passes_and_plus_900_fails() -> None:
    on_time = [genesis(), bar(START, START + 899.0), decision(START, START + 899.0)]
    assert check_deadlines(on_time, 900) == []
    late = [genesis(), bar(START, START + 899.0), decision(START, START + 900.0)]
    problems = check_deadlines(late, 900)
    assert len(problems) == 1
    assert "line 3" in problems[0]
    assert "deadline" in problems[0]


def test_late_bar_row_is_caught() -> None:
    problems = check_deadlines([genesis(), bar(START, START + 900.0)], 900)
    assert len(problems) == 1
    assert "'judged'" in problems[0] and "must be 'missed'" in problems[0]


def test_late_unavailable_row_is_caught() -> None:
    rows = [genesis(), decision(START, START + 5000.0, status="unavailable")]
    assert any("deadline" in p for p in check_deadlines(rows, 900))


def test_missed_declared_before_its_deadline_is_caught() -> None:
    early = [genesis(), decision(START, START + 899.0, status="missed")]
    problems = check_deadlines(early, 900)
    assert len(problems) == 1
    assert "declared missed" in problems[0]
    assert check_deadlines([genesis(), decision(START, START + 900.0, status="missed")], 900) == []


def test_verdict_on_an_unavailable_or_missed_row_is_caught() -> None:
    verdict = {"choice": "skip", "noul": 0.5, "score": 0.5}
    for status, written in (("unavailable", START + 10.0), ("missed", START + 2000.0)):
        rows = [genesis(), decision(START, written, status=status, verdict=verdict)]
        problems = check_deadlines(rows, 900)
        assert len(problems) == 1
        assert f"{status} decision carries a verdict" in problems[0]


def test_snapshot_hash_mismatch_is_caught() -> None:
    rows = [genesis(), decision(START, START + 10.0, snapshot={**SNAPSHOT, "rsi": 47.4})]
    problems = check_deadlines(rows, 900)
    assert len(problems) == 1
    assert "does not match its snapshot" in problems[0]


def test_snapshot_hash_without_snapshot_is_caught() -> None:
    rows = [genesis(), decision(START, START + 10.0, status="unavailable", snapshot=None)]
    assert any("without its snapshot" in p for p in check_deadlines(rows, 900))


def test_judged_decision_without_verdict_or_hash_is_caught() -> None:
    no_verdict = [genesis(), decision(START, START + 10.0, verdict=None)]
    no_hash = [genesis(), decision(START, START + 10.0, snapshot=None, snapshot_sha256=None)]
    for rows in (no_verdict, no_hash):
        assert any("judged decision without" in p for p in check_deadlines(rows, 900))


def test_signal_bar_not_before_fill_open_is_caught() -> None:
    rows = [genesis(), bar(START, START + 10.0, signal_bar=START)]
    assert any("is not before fill_open" in p for p in check_deadlines(rows, 900))


def test_row_written_before_its_signal_bar_closed_is_caught() -> None:
    rows = [genesis(), decision(START, START - 1.0)]
    assert any("before its signal bar closed" in p for p in check_deadlines(rows, 900))


def test_row_before_the_genesis_start_is_caught() -> None:
    early = START - INTERVAL
    rows = [genesis(), bar(early, early + 10.0)]
    assert any("before the genesis start" in p for p in check_deadlines(rows, 900))


def test_re_judging_the_same_candidate_and_bar_is_caught() -> None:
    rows = [genesis(), decision(START, START + 10.0), decision(START, START + 20.0)]
    problems = check_deadlines(rows, 900)
    assert len(problems) == 1 and "second decision" in problems[0]
    rows = [genesis(), bar(START, START + 10.0), bar(START, START + 20.0)]
    assert any("second bar row" in p for p in check_deadlines(rows, 900))


def test_bad_status_types_and_kinds_are_reported() -> None:
    rows = [
        genesis(),
        bar(START, START + 1.0, status="skip"),
        decision(START, START + 1.0, status="no_signal"),
        {**bar(START + INTERVAL, START + INTERVAL + 1.0), "fill_open": str(START + INTERVAL)},
        bar(START + 2 * INTERVAL, math.nan),
        {"kind": "note"},
    ]
    problems = check_deadlines(rows, 900)
    assert any("line 2" in p and "status 'skip'" in p for p in problems)
    assert any("line 3" in p and "status 'no_signal'" in p for p in problems)
    assert any("line 4" in p and "integer UNIX seconds" in p for p in problems)
    assert any("line 5" in p and "finite number" in p for p in problems)
    assert any("line 6" in p and "unknown kind" in p for p in problems)


@pytest.mark.parametrize("grace", [-1, 1.5, True])
def test_bad_grace_is_rejected(grace: Any) -> None:
    with pytest.raises(ValueError, match="grace_s"):
        check_deadlines([genesis()], grace)


def test_concurrent_appenders_keep_one_valid_chain(log: DecisionLog) -> None:
    def worker(offset: int) -> None:
        writer = DecisionLog(log.path)  # own handle, own lock fd: contends on the flock
        for i in range(10):
            f = START + (3 + offset * 10 + i) * INTERVAL
            writer.append(bar(f, f + 1.0, status="no_signal", signals=[]))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = log.rows()
    assert len(rows) == len(sample_rows()) + 40
    assert check_deadlines(rows, DEFAULT_GRACE_S) == []
