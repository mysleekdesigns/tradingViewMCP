"""Read-only Research endpoints (PLAN.md Phase 8, Research page backend). Offline: tmp dirs, a
tmp ledger, a tmp idea library built from ``IDEAS_SCHEMA``."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lab.api import create_app
from lab.config import load_config
from lab.ledger import Ledger
from lab.research import readers
from lab.research.contract import IDEAS_SCHEMA, ResearchConfig, RunBudget, SuccessCriteria
from lab.research.runs import start_research_run

CRITERIA = SuccessCriteria(
    wf_oos_sharpe_min=1.0,
    dsr_min=0.95,
    pbo_max=0.2,
    beat_buy_hold=True,
    beat_vol_target=True,
    random_entry_percentile_min=95.0,
    cost_stress_factor=2.0,
    require_gradual_delay_decay=True,
    max_drawdown_max=0.25,
    min_trades=50,
    min_profitable_regimes=2,
    regime_window_days=90,
    regime_threshold=0.10,
    neighbourhood_pct=20.0,
    require_holdout_pass=True,
    require_crosscheck_match=True,
)
BUDGET = RunBudget(
    max_hypotheses=5, max_proposals=10, max_backtests=200, max_holdout_checks=2, max_usd=5.0
)
RESEARCH = ResearchConfig(criteria=CRITERIA, budget=BUDGET, sha256="ab" * 32)


@dataclass
class Dirs:
    root: Path
    ledger: Path
    journal: Path
    ideas: Path
    runs: Path
    paper: Path


@pytest.fixture
def dirs(tmp_path: Path) -> Dirs:
    return Dirs(
        root=tmp_path,
        ledger=tmp_path / "ledger.db",
        journal=tmp_path / "agents" / "journal",
        ideas=tmp_path / "agents" / "data" / "ideas.sqlite",
        runs=tmp_path / "agents" / "data" / "runs",
        paper=tmp_path / "agents" / "data" / "paper",
    )


@pytest.fixture
def client(dirs: Dirs) -> Iterator[TestClient]:
    app = create_app(
        load_config(),
        ledger_path=dirs.ledger,
        warm_up=False,
        journal_dir=dirs.journal,
        ideas_db_path=dirs.ideas,
        run_state_dir=dirs.runs,
        paper_dir=dirs.paper,
    )
    with TestClient(app, base_url="http://127.0.0.1:8788") as c:
        yield c


def _start_run(dirs: Dirs, label: str, now: datetime) -> str:
    with Ledger(dirs.ledger) as led:
        return start_research_run(led, RESEARCH, label=label, now=now)


def _state(run_id: str, started_at: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "symbols": ["BTCUSDT"],
        "interval": "4h",
        "started_at": started_at,
        "updated_at": started_at,
        "phase": "testing",
        "outcome": "running",
        "budget": {"max_proposals": 10},
        "used": {"hypotheses": 1, "proposals": 0, "backtests": 0, "holdout_checks": 0, "usd": 0.1},
        "current_idea_id": 1,
        "candidates": candidates,
        "journal_path": None,
        "events": [{"at": started_at, "kind": "scout", "message": "searching"}],
    }


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _candidate(idea_id: int, plan_hash: str) -> dict[str, Any]:
    return {
        "idea_id": idea_id,
        "plan_hash": plan_hash,
        "plan": {"name": "x"},
        "trial_id": 7,
        "summary": "ok",
    }


# --------------------------------------------------------------------------- empty state


def test_every_research_endpoint_is_empty_without_data(client: TestClient, dirs: Dirs) -> None:
    assert not dirs.journal.exists() and not dirs.ideas.exists()
    assert client.get("/lab/research/runs").json() == {"runs": []}
    assert client.get("/lab/research/ideas").json() == {"ideas": []}
    assert client.get("/lab/research/journal").json() == {"entries": []}
    assert client.get("/lab/research/candidates").json() == {"candidates": []}
    assert client.get("/lab/research/paper").json() == {"comparisons": []}
    assert client.get("/lab/research/journal/missing.md").status_code == 404


def test_readers_never_create_missing_files(client: TestClient, dirs: Dirs) -> None:
    for path in ("ideas", "journal", "candidates", "paper"):
        client.get(f"/lab/research/{path}")
    assert not dirs.ideas.exists()
    assert not (dirs.root / "agents").exists()


def test_host_allowlist_applies_to_research_routes(client: TestClient) -> None:
    r = client.get("/lab/research/runs", headers={"host": "evil.example:8788"})
    assert r.status_code == 403
    r = client.get("/lab/research/journal", headers={"host": "attacker.test"})
    assert r.status_code == 403


def test_research_routes_are_get_only(client: TestClient) -> None:
    for path in ("runs", "ideas", "journal", "candidates", "paper"):
        assert client.post(f"/lab/research/{path}", json={}).status_code == 405


# --------------------------------------------------------------------------- runs


def test_runs_newest_first_with_usage_and_state(client: TestClient, dirs: Dirs) -> None:
    old = _start_run(dirs, "first", datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    new = _start_run(dirs, "second", datetime(2026, 10, 2, 12, 0, tzinfo=UTC))
    state = _state(new, "2026-10-02T12:00:00+00:00", [])
    _write(dirs.runs / f"{new}.json", json.dumps(state))

    body = client.get("/lab/research/runs").json()
    assert [r["run_id"] for r in body["runs"]] == [new, old]
    item = body["runs"][0]
    assert set(item) == {
        "run_id",
        "created_at",
        "label",
        "config_sha256",
        "criteria",
        "budget",
        "usage",
        "state",
    }
    assert item["label"] == "second"
    assert item["config_sha256"] == "ab" * 32
    assert isinstance(item["created_at"], int)
    assert abs(item["created_at"] - int(datetime.now(UTC).timestamp())) < 120
    assert item["criteria"]["dsr_min"] == 0.95
    assert item["budget"]["max_backtests"] == 200
    assert item["usage"] == {
        "budget": {"proposals": 10, "backtests": 200, "holdout_checks": 2},
        "used": {"proposals": 0, "backtests": 0, "holdout_checks": 0},
        "remaining": {"proposals": 10, "backtests": 200, "holdout_checks": 2},
    }
    assert item["state"] == state
    assert body["runs"][1]["state"] is None


def test_single_run_and_unknown_run(client: TestClient, dirs: Dirs) -> None:
    run_id = _start_run(dirs, "solo", datetime(2026, 10, 3, 9, 30, tzinfo=UTC))
    r = client.get(f"/lab/research/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["run_id"] == run_id and r.json()["label"] == "solo"
    assert client.get("/lab/research/runs/r-20990101-000000-abcdef").status_code == 404
    assert client.get("/lab/research/runs/bad%20id!").status_code == 422


def test_malformed_run_state_is_ignored(client: TestClient, dirs: Dirs) -> None:
    run_id = _start_run(dirs, "broken", datetime(2026, 10, 4, 0, 0, tzinfo=UTC))
    _write(dirs.runs / f"{run_id}.json", "{not json")
    assert client.get(f"/lab/research/runs/{run_id}").json()["state"] is None
    _write(dirs.runs / f"{run_id}.json", "[1, 2]")  # valid JSON, not an object
    assert client.get(f"/lab/research/runs/{run_id}").json()["state"] is None
    other = _state("r-20000101-000000-000000", "2026-10-04T00:00:00Z", [])
    _write(dirs.runs / f"{run_id}.json", json.dumps(other))  # records another run
    assert client.get(f"/lab/research/runs/{run_id}").json()["state"] is None


def test_malformed_frozen_budget_gives_null_usage(client: TestClient, dirs: Dirs) -> None:
    with Ledger(dirs.ledger) as led:
        led.start_research_run(
            "r-20261005-000000-aaaaaa",
            label="odd",
            config_json=json.dumps({"criteria": {}, "budget": {"unexpected": 1}}),
            config_sha256="00",
        )
    item = client.get("/lab/research/runs/r-20261005-000000-aaaaaa").json()
    assert item["usage"] is None and item["budget"] == {"unexpected": 1}


# --------------------------------------------------------------------------- ideas


def _ideas_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        for stmt in IDEAS_SCHEMA:
            conn.execute(stmt)
        rows = [
            ("2026-10-01T10:00:00+00:00", "r-a", "trend", "Old", "dk1", "dead", "[]"),
            ("2026-10-02T10:00:00+00:00", "r-a", "risk", "Mid", "dk2", "testing", '["h1","h2"]'),
            ("2026-10-03T10:00:00.123456+00:00", "r-b", "trend", "New", "dk3", "new", "oops"),
        ]
        for created, run_id, family, title, dk, status, hashes in rows:
            conn.execute(
                "INSERT INTO ideas (created_at, run_id, family, title, hypothesis, rationale,"
                " dedupe_key, status, verdict_note, plan_hashes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    created,
                    run_id,
                    family,
                    title,
                    f"{title} hyp",
                    f"{title} why",
                    dk,
                    status,
                    None,
                    hashes,
                ),
            )
        conn.execute(
            "INSERT INTO sources (idea_id, created_at, url, title, untrusted)"
            " VALUES (2, '2026-10-02T10:00:00+00:00', 'https://a.example/p', 'A', 1)"
        )
        conn.execute(
            "INSERT INTO sources (idea_id, created_at, url, title, untrusted)"
            " VALUES (2, '2026-10-02T10:00:00+00:00', 'https://arxiv.org/x', NULL, 0)"
        )
    conn.close()


def test_ideas_newest_first_with_sources_and_seconds(client: TestClient, dirs: Dirs) -> None:
    _ideas_db(dirs.ideas)
    ideas = client.get("/lab/research/ideas").json()["ideas"]
    assert [i["title"] for i in ideas] == ["New", "Mid", "Old"]
    mid = ideas[1]
    assert mid == {
        "id": 2,
        "created_at": int(datetime(2026, 10, 2, 10, tzinfo=UTC).timestamp()),
        "run_id": "r-a",
        "family": "risk",
        "title": "Mid",
        "hypothesis": "Mid hyp",
        "rationale": "Mid why",
        "status": "testing",
        "verdict_note": None,
        "plan_hashes": ["h1", "h2"],
        "sources": [
            {"url": "https://a.example/p", "title": "A", "untrusted": True},
            {"url": "https://arxiv.org/x", "title": None, "untrusted": False},
        ],
    }
    assert ideas[0]["plan_hashes"] == []  # malformed JSON column reads as empty
    assert ideas[0]["created_at"] == int(datetime(2026, 10, 3, 10, tzinfo=UTC).timestamp())
    assert ideas[2]["sources"] == []


def test_ideas_filters(client: TestClient, dirs: Dirs) -> None:
    _ideas_db(dirs.ideas)
    by_run = client.get("/lab/research/ideas", params={"run_id": "r-a"}).json()["ideas"]
    assert [i["title"] for i in by_run] == ["Mid", "Old"]
    both = client.get("/lab/research/ideas", params={"run_id": "r-a", "status": "dead"}).json()
    assert [i["title"] for i in both["ideas"]] == ["Old"]


def test_ideas_db_is_opened_read_only(dirs: Dirs) -> None:
    _ideas_db(dirs.ideas)
    before = dirs.ideas.read_bytes()
    assert len(readers.read_ideas(dirs.ideas)) == 3
    assert dirs.ideas.read_bytes() == before
    conn = readers._ro_connect(dirs.ideas)
    assert conn is not None
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM ideas")
    conn.close()


def test_ideas_cap_at_500(dirs: Dirs) -> None:
    dirs.ideas.parent.mkdir(parents=True)
    conn = sqlite3.connect(dirs.ideas)
    with conn:
        for stmt in IDEAS_SCHEMA:
            conn.execute(stmt)
        conn.executemany(
            "INSERT INTO ideas (created_at, family, title, hypothesis, rationale, dedupe_key)"
            " VALUES ('2026-10-01T00:00:00+00:00', 'f', 't', 'h', 'r', ?)",
            [(f"k{i}",) for i in range(520)],
        )
    conn.close()
    assert len(readers.read_ideas(dirs.ideas)) == 500
    assert len(readers.read_ideas(dirs.ideas, limit=10_000)) == 500


def test_ideas_db_without_tables_or_garbage_is_empty(client: TestClient, dirs: Dirs) -> None:
    dirs.ideas.parent.mkdir(parents=True)
    sqlite3.connect(dirs.ideas).close()  # valid empty database, no tables
    assert client.get("/lab/research/ideas").json() == {"ideas": []}
    dirs.ideas.write_bytes(b"this is not a sqlite file at all" * 10)
    assert client.get("/lab/research/ideas").json() == {"ideas": []}


# --------------------------------------------------------------------------- journal

JOURNAL_A = """---
run_id: r-20261001-120000-abcdef
date: 2026-10-01
outcome: "no_edge"
idea_ids: [3, 4, x]
---

Intro line.

# Vol-target overlay on BTC 4h

Body text.
"""


def test_journal_list_parses_frontmatter(client: TestClient, dirs: Dirs) -> None:
    _write(dirs.journal / "2026-10-01-r1.md", JOURNAL_A)
    _write(dirs.journal / "2026-10-02-r2.md", "---\ntitle: From meta\noutcome: candidate\n---\n")
    _write(dirs.journal / "2026-09-30-plain.md", "No frontmatter, no heading.\n")
    _write(dirs.journal / "notes.txt", "not markdown")
    (dirs.journal / "sub").mkdir()
    _write(dirs.journal / "sub" / "2027-nested.md", "# nested")

    entries = client.get("/lab/research/journal").json()["entries"]
    assert [e["name"] for e in entries] == [
        "2026-10-02-r2.md",
        "2026-10-01-r1.md",
        "2026-09-30-plain.md",
    ]
    r2, r1, plain = entries
    assert r1 == {
        "name": "2026-10-01-r1.md",
        "title": "Vol-target overlay on BTC 4h",
        "run_id": "r-20261001-120000-abcdef",
        "date": "2026-10-01",
        "outcome": "no_edge",
        "idea_ids": [3, 4],
        "size": len(JOURNAL_A.encode()),
    }
    assert r2["title"] == "From meta" and r2["outcome"] == "candidate" and r2["idea_ids"] == []
    assert plain["title"] == "2026-09-30-plain" and plain["run_id"] is None


def test_journal_entry_returns_markdown(client: TestClient, dirs: Dirs) -> None:
    _write(dirs.journal / "2026-10-01-r1.md", JOURNAL_A)
    r = client.get("/lab/research/journal/2026-10-01-r1.md")
    assert r.status_code == 200
    assert r.json() == {"name": "2026-10-01-r1.md", "markdown": JOURNAL_A}


@pytest.mark.parametrize(
    "path",
    [
        "/lab/research/journal/..%2Fsecret.md",
        "/lab/research/journal/%2E%2E%2Fsecret.md",
        "/lab/research/journal/..secret.md",
        "/lab/research/journal/%2Fetc%2Fpasswd",
        "/lab/research/journal/.hidden.md",
        "/lab/research/journal/notes.txt",
        "/lab/research/journal/a%00.md",
    ],
)
def test_journal_traversal_is_rejected(client: TestClient, dirs: Dirs, path: str) -> None:
    _write(dirs.journal / "ok.md", "# ok")
    _write(dirs.journal.parent / "secret.md", "# secret")
    _write(dirs.journal / ".hidden.md", "# hidden")
    _write(dirs.journal / "notes.txt", "txt")
    r = client.get(path)
    assert r.status_code == 404
    assert "secret" not in r.text


def test_journal_symlink_escaping_is_rejected(client: TestClient, dirs: Dirs) -> None:
    outside = _write(dirs.root / "outside.md", "# outside secret")
    dirs.journal.mkdir(parents=True)
    os.symlink(outside, dirs.journal / "link.md")
    r = client.get("/lab/research/journal/link.md")
    assert r.status_code == 404
    names = [e["name"] for e in client.get("/lab/research/journal").json()["entries"]]
    assert "link.md" not in names


def test_journal_dir_that_is_a_symlink_still_contains_reads(dirs: Dirs) -> None:
    real = dirs.root / "real_journal"
    _write(real / "a.md", "# A")
    dirs.journal.parent.mkdir(parents=True)
    os.symlink(real, dirs.journal)
    assert readers.read_journal(dirs.journal, "a.md") == "# A"
    assert readers.journal_path(dirs.journal, "../real_journal/a.md") is None


def test_journal_entry_over_2mb_is_refused(client: TestClient, dirs: Dirs) -> None:
    _write(dirs.journal / "big.md", "x" * (readers.MAX_JOURNAL_BYTES + 1))
    assert client.get("/lab/research/journal/big.md").status_code == 413
    assert client.get("/lab/research/journal").json()["entries"][0]["name"] == "big.md"


def test_parse_frontmatter_edge_cases() -> None:
    assert readers.parse_frontmatter("---\nno close\n") == ({}, "---\nno close\n")
    meta, body = readers.parse_frontmatter("---\na: 1\nb: []\n  nested: x\nc: 'q'\n---\nbody")
    assert meta == {"a": "1", "b": [], "c": "q"} and body == "body"


# --------------------------------------------------------------------------- candidates


def test_candidates_collected_newest_run_first(client: TestClient, dirs: Dirs) -> None:
    old, new = "r-20261001-000000-aaaaaa", "r-20261002-000000-bbbbbb"
    _write(
        dirs.runs / f"{old}.json",
        json.dumps(_state(old, "2026-10-01T00:00:00Z", [_candidate(1, "p1")])),
    )
    _write(
        dirs.runs / f"{new}.json",
        json.dumps(
            _state(new, "2026-10-02T00:00:00Z", [_candidate(2, "p2"), "junk", _candidate(3, "p3")])
        ),
    )
    _write(dirs.runs / "r-20261003-000000-cccccc.json", "{broken")
    _write(dirs.runs / "notes.txt", "ignored")

    cands = client.get("/lab/research/candidates").json()["candidates"]
    assert [(c["run_id"], c["plan_hash"]) for c in cands] == [
        (new, "p2"),
        (new, "p3"),
        (old, "p1"),
    ]
    assert cands[0] == {**_candidate(2, "p2"), "run_id": new}


# --------------------------------------------------------------------------- paper


def _comparison(candidate: str, day: str) -> dict[str, Any]:
    stats = {"n_trades": 3, "total_return": 0.01, "win_rate": 0.5, "mean_trade_return": 0.003}
    return {
        "candidate": candidate,
        "plan_hash": "ph",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "period_start": 1_790_000_000,
        "period_end": 1_790_600_000,
        "generated_at": f"{day}T00:00:00Z",
        "forward": stats,
        "backtest": stats,
        "entry_match_rate": 1.0,
        "mean_entry_slippage_bps": 2.5,
        "verdict": "consistent",
        "notes": [day],
    }


def test_paper_latest_comparison_per_candidate(client: TestClient, dirs: Dirs) -> None:
    for day in ("2026-10-05", "2026-10-12"):
        _write(dirs.paper / "cand-a" / f"comparison-{day}.json", json.dumps(_comparison("a", day)))
    _write(dirs.paper / "cand-b" / "comparison-2026-10-05.json", json.dumps(_comparison("b", "x")))
    _write(dirs.paper / "cand-b" / "comparison-2026-10-19.json", "{malformed")  # falls back
    _write(dirs.paper / "cand-c" / "notes.json", "{}")  # no comparison file
    _write(dirs.paper / "stray.json", "{}")

    comps = client.get("/lab/research/paper").json()["comparisons"]
    assert [(c["candidate"], c["notes"]) for c in comps] == [("a", ["2026-10-12"]), ("b", ["x"])]
    assert comps[0] == _comparison("a", "2026-10-12")


# --------------------------------------------------------------------------- helpers


def test_iso_to_seconds() -> None:
    assert readers.iso_to_seconds("1970-01-01T00:01:00Z") == 60
    assert readers.iso_to_seconds("1970-01-01T00:01:00") == 60  # naive means UTC
    assert readers.iso_to_seconds("1970-01-01T02:00:00+02:00") == 0
    assert readers.iso_to_seconds("garbage") is None
    assert readers.iso_to_seconds(None) is None
