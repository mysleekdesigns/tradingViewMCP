"""Per-run ledger hash chain (PLAN.md Phase 13): tamper evidence for every trial of a run."""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lab.ledger import _MIGRATIONS, Ledger, trial_hash
from lab.types import TrialRecord


def make_record(run_id: str = "run-a", *, sharpe: float = 0.05, n: int = 0) -> TrialRecord:
    return TrialRecord(
        run_id=run_id,
        source="manual",
        status="ok",
        plan_hash=f"{n:064x}",
        plan_json='{"name":"ema_cross"}',
        params_json=json.dumps({"fast": 20 + n}),
        symbol="BTCUSDT",
        interval="4h",
        window_kind="design",
        window_start=pd.Timestamp("2021-01-01", tz="UTC"),
        window_end=pd.Timestamp("2021-07-01", tz="UTC"),
        metrics_json='{"sharpe":1.2}',
        sharpe_per_bar=sharpe,
        n_trades=7,
        returns=np.arange(5, dtype=float) * 0.001 * (n + 1),
    )


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture
def ledger(path: Path) -> Iterator[Ledger]:
    with Ledger(path) as led:
        yield led


def raw(path: Path) -> sqlite3.Connection:
    """A plain connection that can drop the append-only triggers (simulated tampering)."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def recompute(path: Path, run_id: str) -> str | None:
    conn = raw(path)
    try:
        head: str | None = None
        for row in conn.execute("SELECT * FROM trials WHERE run_id = ? ORDER BY id", (run_id,)):
            head = trial_hash(head, row)
        return head
    finally:
        conn.close()


def test_empty_run_verifies_with_no_head(ledger: Ledger) -> None:
    got = ledger.verify_chain("never-used")
    assert got == {
        "run_id": "never-used",
        "ok": True,
        "n_trials": 0,
        "head": None,
        "first_bad_trial_id": None,
        "error": None,
    }
    assert ledger.chain_head("never-used") is None


def test_bad_run_id_type_raises(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="run_id"):
        ledger.verify_chain(42)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="run_id"):
        ledger.chain_head(None)  # type: ignore[arg-type]


def test_trial_hash_is_sha256_of_prev_plus_canonical_json() -> None:
    blob = b"\x00\x01"
    row = {
        "id": 1,
        "created_at": "2026-01-01T00:00:00.000000+00:00",
        "run_id": "r",
        "source": "manual",
        "status": "ok",
        "plan_hash": "h",
        "plan_json": "{}",
        "params_json": "{}",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "window_kind": "design",
        "window_start": 0,
        "window_end": 14400,
        "metrics_json": "{}",
        "sharpe_per_bar": 0.5,
        "n_trades": 3,
        "error": None,
        "n_returns": 2,
        "returns_blob": blob,
        "costs_json": "{}",
    }
    content = {k: v for k, v in row.items() if k != "returns_blob"}
    content["returns_sha256"] = hashlib.sha256(blob).hexdigest()
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    assert trial_hash(None, row) == hashlib.sha256(text.encode()).hexdigest()
    prev = "a" * 64
    assert trial_hash(prev, row) == hashlib.sha256((prev + text).encode()).hexdigest()
    # Any content change, or a different predecessor, changes the hash.
    assert trial_hash(None, {**row, "n_trades": 4}) != trial_hash(None, row)
    assert trial_hash(None, {**row, "returns_blob": b"\x00\x02"}) != trial_hash(None, row)
    assert trial_hash(None, {**row, "returns_blob": None}) != trial_hash(None, row)
    with pytest.raises(ValueError, match="costs_json"):
        trial_hash(None, {k: v for k, v in row.items() if k != "costs_json"})


def test_chain_links_each_trial_and_runs_stay_independent(ledger: Ledger, path: Path) -> None:
    ids: dict[str, list[int]] = {"a": [], "b": []}
    for n in range(4):  # interleave two runs
        ids["a"].append(ledger.record(make_record("run-a", n=n)))
        ids["b"].append(ledger.record(make_record("run-b", n=n + 10)))
    for run_id in ("run-a", "run-b"):
        got = ledger.verify_chain(run_id)
        assert got["ok"] is True, got
        assert got["n_trials"] == 4
        assert got["first_bad_trial_id"] is None and got["error"] is None
        assert got["head"] == ledger.chain_head(run_id) == recompute(path, run_id)
        assert got["head"] is not None

    conn = raw(path)
    links = conn.execute(
        "SELECT trial_id, run_id, prev_hash, hash, backfilled FROM trial_chain ORDER BY trial_id"
    ).fetchall()
    conn.close()
    by_run: dict[str, list[sqlite3.Row]] = {"run-a": [], "run-b": []}
    for link in links:
        by_run[link["run_id"]].append(link)
        assert link["backfilled"] == 0
    for run_id, key in (("run-a", "a"), ("run-b", "b")):
        chain = by_run[run_id]
        assert [c["trial_id"] for c in chain] == ids[key]
        assert chain[0]["prev_hash"] is None  # each run's chain starts fresh
        for prev, cur in itertools.pairwise(chain):
            assert cur["prev_hash"] == prev["hash"]
    assert ledger.chain_head("run-a") != ledger.chain_head("run-b")


def test_head_changes_with_each_trial(ledger: Ledger, path: Path) -> None:
    heads = []
    for n in range(3):
        ledger.record(make_record(n=n))
        heads.append(ledger.chain_head("run-a"))
        assert heads[-1] == recompute(path, "run-a")
    assert len(set(heads)) == 3


def test_tampered_trial_content_is_detected(ledger: Ledger, path: Path) -> None:
    ids = [ledger.record(make_record(n=n)) for n in range(4)]
    ledger.record(make_record("run-b", n=9))
    conn = raw(path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE trials SET metrics_json = '{\"sharpe\":9.9}' WHERE id = ?", (ids[2],))
    conn.execute("DROP TRIGGER trials_no_update")
    conn.execute("UPDATE trials SET metrics_json = '{\"sharpe\":9.9}' WHERE id = ?", (ids[2],))
    conn.close()
    got = ledger.verify_chain("run-a")
    assert got["ok"] is False
    assert got["first_bad_trial_id"] == ids[2]
    assert "content" in got["error"]
    assert got["n_trials"] == 4
    assert ledger.verify_chain("run-b")["ok"] is True  # the other run is untouched


def test_tampered_returns_blob_is_detected(ledger: Ledger, path: Path) -> None:
    ids = [ledger.record(make_record(n=n)) for n in range(3)]
    conn = raw(path)
    conn.execute("DROP TRIGGER trials_no_update")
    conn.execute("UPDATE trials SET returns_blob = ? WHERE id = ?", (b"forged", ids[0]))
    conn.close()
    got = ledger.verify_chain("run-a")
    assert (got["ok"], got["first_bad_trial_id"]) == (False, ids[0])


def test_deleted_chain_row_is_detected(ledger: Ledger, path: Path) -> None:
    ids = [ledger.record(make_record(n=n)) for n in range(4)]
    conn = raw(path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM trial_chain WHERE trial_id = ?", (ids[1],))
    conn.execute("DROP TRIGGER trial_chain_no_delete")
    conn.execute("DELETE FROM trial_chain WHERE trial_id = ?", (ids[1],))
    conn.close()
    got = ledger.verify_chain("run-a")
    assert (got["ok"], got["first_bad_trial_id"]) == (False, ids[1])
    assert "no chain row" in got["error"]
    # A gap in the middle is never re-created by the lazy backfill on reopen or on record.
    with Ledger(path) as again:
        again.record(make_record(n=7))
        assert again.verify_chain("run-a")["first_bad_trial_id"] == ids[1]


def test_deleted_trial_row_is_detected(ledger: Ledger, path: Path) -> None:
    ids = [ledger.record(make_record(n=n)) for n in range(4)]
    conn = raw(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TRIGGER trials_no_delete")
    conn.execute("DELETE FROM trials WHERE id = ?", (ids[1],))
    conn.close()
    got = ledger.verify_chain("run-a")
    assert (got["ok"], got["first_bad_trial_id"], got["n_trials"]) == (False, ids[1], 3)


def test_wrong_prev_link_is_detected(ledger: Ledger, path: Path) -> None:
    ids = [ledger.record(make_record(n=n)) for n in range(3)]
    conn = raw(path)
    conn.execute("DROP TRIGGER trial_chain_no_update")
    # Consistent hash for its own content, but linked to nothing (a forked/rewritten chain).
    row = conn.execute("SELECT * FROM trials WHERE id = ?", (ids[2],)).fetchone()
    conn.execute(
        "UPDATE trial_chain SET prev_hash = NULL, hash = ? WHERE trial_id = ?",
        (trial_hash(None, row), ids[2]),
    )
    conn.close()
    got = ledger.verify_chain("run-a")
    assert (got["ok"], got["first_bad_trial_id"]) == (False, ids[2])
    assert "previous" in got["error"]


def test_trial_moved_to_another_run_is_detected(ledger: Ledger, path: Path) -> None:
    ids = [ledger.record(make_record(n=n)) for n in range(3)]
    ledger.record(make_record("run-b", n=5))
    conn = raw(path)
    conn.execute("DROP TRIGGER trials_no_update")
    conn.execute("UPDATE trials SET run_id = 'run-b' WHERE id = ?", (ids[1],))
    conn.close()
    assert ledger.verify_chain("run-a")["first_bad_trial_id"] == ids[1]
    assert ledger.verify_chain("run-b")["first_bad_trial_id"] == ids[1]


def test_backfill_chains_a_ledger_written_before_the_chain(ledger: Ledger, path: Path) -> None:
    a = [ledger.record(make_record("run-a", n=n)) for n in range(3)]
    b = [ledger.record(make_record("run-b", n=n)) for n in range(2)]
    ledger.close()
    conn = raw(path)
    # As if written by the pre-chain ledger: schema v4, no trial_chain table.
    conn.execute("DROP TABLE trial_chain")
    conn.execute("DELETE FROM schema_version WHERE version = 5")
    conn.close()
    with Ledger(path) as led:
        for run_id, ids in (("run-a", a), ("run-b", b)):
            got = led.verify_chain(run_id)
            assert got["ok"] is True, got
            assert got["n_trials"] == len(ids)
            assert got["head"] == recompute(path, run_id)
        new_id = led.record(make_record("run-a", n=8))
        assert led.verify_chain("run-a")["ok"] is True
    conn = raw(path)
    flags = dict(conn.execute("SELECT trial_id, backfilled FROM trial_chain").fetchall())
    conn.close()
    assert {flags[i] for i in a + b} == {1}
    assert flags[new_id] == 0
    # Idempotent: reopening does not add or change links.
    with Ledger(path) as led:
        assert led.verify_chain("run-a")["head"] == recompute(path, "run-a")
    conn = raw(path)
    assert conn.execute("SELECT COUNT(*) FROM trial_chain").fetchone()[0] == len(a) + len(b) + 1
    conn.close()


def test_backfill_of_a_v1_ledger(tmp_path: Path) -> None:
    """A ledger created by migration 1 only, with raw-inserted trials, gets chained on open."""
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "CREATE TABLE schema_version ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    version, name, statements = _MIGRATIONS[0]
    for sql in statements:
        conn.execute(sql)
    conn.execute("INSERT INTO schema_version VALUES (?, ?, ?)", (version, name, "2026-01-01"))
    for i in range(3):
        conn.execute(
            "INSERT INTO trials (created_at, run_id, source, status, plan_hash, plan_json,"
            " params_json, symbol, interval, window_kind, window_start, window_end,"
            " metrics_json, sharpe_per_bar, n_trades) VALUES ('2026-01-01T00:00:00+00:00',"
            " 'old', 'api', 'ok', ?, '{}', '{}', 'BTCUSDT', '4h', 'design', 0, 14400, '{}',"
            " 0.1, 0)",
            (f"h{i}",),
        )
    conn.close()
    with Ledger(path) as led:
        got = led.verify_chain("old")
        assert (got["ok"], got["n_trials"]) == (True, 3)
        assert got["head"] == recompute(path, "old")


def test_unchained_tail_from_an_old_writer_is_chained_before_the_next_record(
    ledger: Ledger, path: Path
) -> None:
    """A process running pre-chain code appends a trial; the next record chains it first."""
    first = ledger.record(make_record(n=0))
    conn = raw(path)
    conn.execute(
        "INSERT INTO trials (created_at, run_id, source, status, plan_hash, plan_json,"
        " params_json, symbol, interval, window_kind, window_start, window_end, metrics_json,"
        " sharpe_per_bar, n_trades, costs_json) VALUES ('2026-01-01T00:00:00+00:00', 'run-a',"
        " 'api', 'ok', 'h', '{}', '{}', 'BTCUSDT', '4h', 'design', 0, 14400, '{}', 0.1, 0, '{}')"
    )
    legacy = conn.execute("SELECT MAX(id) FROM trials").fetchone()[0]
    conn.close()
    unchained = ledger.verify_chain("run-a")
    assert (unchained["ok"], unchained["first_bad_trial_id"]) == (False, legacy)
    last = ledger.record(make_record(n=1))
    got = ledger.verify_chain("run-a")
    assert got["ok"] is True and got["n_trials"] == 3
    assert got["head"] == recompute(path, "run-a")
    assert first < legacy < last


def test_concurrent_writers_do_not_fork_the_chain(path: Path) -> None:
    Ledger(path).close()
    n_workers, per_worker = 4, 5
    barrier = threading.Barrier(n_workers)
    errors: list[BaseException] = []

    def worker(k: int) -> None:
        try:
            with Ledger(path) as led:
                barrier.wait()
                for j in range(per_worker):
                    led.record(make_record(n=k * 100 + j))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with Ledger(path) as led:
        got = led.verify_chain("run-a")
        assert got["ok"] is True, got
        assert got["n_trials"] == n_workers * per_worker
        assert got["head"] == recompute(path, "run-a")
    conn = raw(path)
    prevs = [r[0] for r in conn.execute("SELECT prev_hash FROM trial_chain")]
    conn.close()
    assert len(prevs) == len(set(prevs))  # no two links share a predecessor: no fork


def test_open_readonly_never_writes_migrates_or_backfills(ledger: Ledger, path: Path) -> None:
    ids = [ledger.record(make_record("run-a", n=n)) for n in range(2)]
    ledger.close()
    conn = raw(path)
    conn.execute("DROP TRIGGER trial_chain_no_delete")
    conn.execute("DELETE FROM trial_chain WHERE trial_id = ?", (ids[1],))  # unchained tail
    conn.close()
    with Ledger.open_readonly(path) as led:
        assert led.schema_version == 5
        assert led.count("run-a") == 2
        assert led.verify_chain("run-a")["ok"] is False  # no backfill happened
        with pytest.raises(sqlite3.OperationalError):
            led.record(make_record("run-a", n=9))
    conn = raw(path)
    assert conn.execute("SELECT COUNT(*) FROM trial_chain").fetchone()[0] == 1
    conn.close()


def test_open_readonly_refuses_a_missing_file_or_an_old_schema(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(ValueError, match="not found"):
        Ledger.open_readonly(missing)
    assert not missing.exists()
    old = tmp_path / "v1.db"
    conn = sqlite3.connect(old, isolation_level=None)
    conn.execute(
        "CREATE TABLE schema_version ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    version, name, statements = _MIGRATIONS[0]
    for sql in statements:
        conn.execute(sql)
    conn.execute("INSERT INTO schema_version VALUES (?, ?, ?)", (version, name, "2026-01-01"))
    conn.close()
    with pytest.raises(ValueError, match="schema 1, needs >= 5"):
        Ledger.open_readonly(old, min_schema_version=5)


def test_open_readonly_in_an_unwritable_folder_says_why(ledger: Ledger, path: Path) -> None:
    ledger.record(make_record("run-a", n=0))
    ledger.close()  # checkpoints and removes -wal/-shm, so a reader must create them
    folder = path.parent
    folder.chmod(0o555)
    try:
        with pytest.raises(ValueError, match="needs a writable folder"):
            Ledger.open_readonly(path)
    finally:
        folder.chmod(0o755)
