"""``paper_trade.py shadow`` / ``shadow-report`` end to end in ``tmp_path`` (Phase 17): the
shadow CLI pre-registers and logs bars (judged, unavailable, missed), and the report reproduces
byte for byte from only the JSONL and fixture dry-run DBs, counts unavailable and missed
decisions, is an interim look before the frozen evaluation time and "inconclusive" below the
frozen minimum sample after it, refuses (exit 2) a rewritten log, a missing or edited
pre-registration, changed snapshot / question hashes and an ``--asof`` later than the wall
clock, and never overwrites a written report.
Offline: fake candles, a fake clock and a MockTransport Jev."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import paper_trade
import pytest

from research.jev import JevClient
from research.paper import shadow
from research.paper.shadow import HEADS_FILE, decisions_path, heads_path, report_name
from research.paper.shadow_log import DecisionLog, verify_chain
from research.paper.shadow_report import build_report, load_trades, render_report_markdown
from tests.test_shadow import (
    CAND_A,
    CAND_B,
    FAKE_KEY,
    GRACE,
    H4,
    PLAN_A,
    World,
    flip_first_verdict,
    make_world,
    take_first,
)


def _iso(s: int) -> str:
    return pd.Timestamp(s, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M:%S.%f")


def write_db(path: Path, rows: list[tuple[Any, ...]]) -> None:
    """A dry-run DB with freqtrade's ``trades`` columns (the ones ``compare`` reads)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN, "
        "open_date DATETIME, open_rate FLOAT, close_date DATETIME, close_rate FLOAT, "
        "amount FLOAT, fee_open FLOAT, fee_close FLOAT, close_profit FLOAT, exit_reason TEXT)"
    )
    conn.executemany(
        "INSERT INTO trades (pair, is_open, open_date, open_rate, close_date, close_rate, "
        "amount, fee_open, fee_close, close_profit, exit_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def cli_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    world = make_world(tmp_path)
    monkeypatch.setattr(shadow, "make_client", lambda model="jev-latest": world.client(model=model))
    monkeypatch.setattr(shadow, "default_loader_for", world.market.loader_for)
    monkeypatch.setattr(shadow, "wall_clock", world.clock)
    return world


def cli(world: World, *args: str, paper: Path | None = None, journal: Path | None = None) -> int:
    return paper_trade.main(
        [
            "--paper-dir",
            str(paper or world.paper),
            "--journal-dir",
            str(journal or world.journal),
            *args,
        ]
    )


def run_scenario(world: World) -> tuple[int, int]:
    """Genesis, then one `shadow --once` per bar of A's window: one bar with Jev down (HTTP
    500), one bar first seen after its deadline. Returns (unavailable bar, missed bar)."""
    assert cli(world, "shadow", "--once") == 0
    signal_fills = [
        f
        for f in range(world.first_fill, world.a.decision_end, H4)
        if world.expected_signal(world.a, PLAN_A, f)
    ]
    down, late = signal_fills[0], signal_fills[1]
    for f in range(world.first_fill, world.a.decision_end + H4, H4):
        if f == late:
            continue  # nobody ran during this bar's grace
        world.clock.now = f + 30  # after `late`, this run logs it missed (past its deadline)
        world.handler[0] = (lambda _r: httpx.Response(500, json={})) if f == down else take_first
        assert cli(world, "shadow", "--once") == 0
    return down, late


def fixture_dbs(world: World) -> None:
    """One closed paper trade per decision (opened inside its fill bar), returns alternating."""
    rows = verify_chain(decisions_path(world.paper))
    per: dict[str, list[tuple[Any, ...]]] = {CAND_A: [], CAND_B: []}
    pairs = {CAND_A: "BTC/USDT", CAND_B: "ETH/USDT"}
    for i, d in enumerate(r for r in rows if r["kind"] == "decision"):
        f, cand = d["fill_open"], d["candidate"]
        ret = 0.02 if i % 2 else -0.01
        per[cand].append(
            (pairs[cand], 0, _iso(f + 5), 100.0, _iso(f + 2 * H4), 100.0 * (1 + ret), 1.0,
             0.001, 0.001, ret, "exit_signal")
        )  # fmt: skip
    for cand, trades in per.items():
        write_db(world.paper / cand / "dryrun.sqlite", trades)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, timeout=30,
    )  # fmt: skip


def commit_heads(journal: Path, message: str = "heads") -> None:
    """Commit ``journal``'s heads file in a temp git repo rooted at its parent (created on
    first use): a final report needs the committed copy to anchor the log's tip."""
    root = journal.parent
    if not (root / ".git").exists():
        _git(root, "init", "-q")
    _git(root, "add", f"{journal.name}/{HEADS_FILE}")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)


def evaluate_at(world: World) -> int:
    return int(verify_chain(decisions_path(world.paper))[0]["analysis"]["evaluate_at"])


def iso(ts: int) -> str:
    return pd.Timestamp(ts, unit="s", tz="UTC").isoformat()


def advance_clock(world: World, ts: int) -> None:
    """Move the fake wall clock forward to at least ``ts`` (never back): ``shadow-report``
    refuses an ``--asof`` later than the wall clock, so a test reading a later asof first lets
    that time pass."""
    world.clock.now = max(world.clock.now, float(ts))


def expected_markdown(paper: Path, asof: int) -> str:
    rows = verify_chain(decisions_path(paper))
    got = load_trades(paper, rows[0])
    return render_report_markdown(
        build_report(rows, got.trades, asof=asof, notes=got.notes, unreadable=got.unreadable)
    )


# --------------------------------------------------------------------------- shadow


def test_shadow_once_prints_start_candidates_and_one_line_per_bar(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(cli_world, "shadow", "--once") == 0
    out = capsys.readouterr().out
    assert "pre-registered:" in out and "-multi-jev-shadow.md" in out
    assert "shadow start" in out and CAND_A in out and CAND_B in out
    cli_world.clock.now = cli_world.first_fill + 30
    assert cli(cli_world, "shadow", "--once") == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if "fill bar:" in line]
    assert len(lines) == 1
    assert any(s in lines[0] for s in ("judged", "no_signal"))
    if "judged" in lines[0]:
        assert "verdicts:" in lines[0] and ("take" in lines[0] or "skip" in lines[0])


def test_shadow_without_key_warns_loudly_and_logs_unavailable(
    cli_world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(shadow, "make_client", lambda model="jev-latest": JevClient(api_key=""))
    assert cli(cli_world, "shadow", "--once") == 0
    for f in range(cli_world.first_fill, cli_world.a.decision_end, H4):
        cli_world.clock.now = f + 30
        assert cli(cli_world, "shadow", "--once") == 0
    err = capsys.readouterr().err
    assert "WARNING: TYPESAFE_API_KEY is not set" in err and "unavailable" in err
    rows = verify_chain(decisions_path(cli_world.paper))
    ds = [r for r in rows if r["kind"] == "decision"]
    assert ds and {d["status"] for d in ds} == {"unavailable"}
    assert FAKE_KEY not in decisions_path(cli_world.paper).read_text(encoding="utf-8")


def test_shadow_refuses_with_exit_2_after_the_preregistration_changed(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(cli_world, "shadow", "--once") == 0
    genesis = verify_chain(decisions_path(cli_world.paper))[0]
    (cli_world.journal / genesis["prereg_journal"]).write_text("edited", encoding="utf-8")
    assert cli(cli_world, "shadow", "--once") == 2
    assert "pre-registration changed" in capsys.readouterr().err


# --------------------------------------------------------------------------- report


def test_report_reproduces_from_the_jsonl_and_dryrun_dbs_alone(
    cli_world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    down, late = run_scenario(cli_world)
    rows = verify_chain(decisions_path(cli_world.paper))
    bars = {r["fill_open"]: r for r in rows if r["kind"] == "bar"}
    assert bars[down]["status"] == "unavailable" and bars[late]["status"] == "missed"
    fixture_dbs(cli_world)
    asof = evaluate_at(cli_world) + 5 * H4  # after the evaluation time: the frozen test
    advance_clock(cli_world, asof)
    commit_heads(cli_world.journal)  # a final report needs the committed anchor
    capsys.readouterr()

    assert cli(cli_world, "shadow-report", "--asof", iso(asof)) == 0
    out = capsys.readouterr().out
    markdown = expected_markdown(cli_world.paper, asof)
    assert markdown in out
    assert "**Outcome: inconclusive**" in out
    ds = [r for r in rows if r["kind"] == "decision"]
    counts = {s: sum(d["status"] == s for d in ds) for s in ("judged", "unavailable", "missed")}
    assert counts["unavailable"] >= 1 and counts["missed"] >= 1
    assert f"| count | {counts['judged']} | {counts['unavailable']} | {counts['missed']} |" in out
    head = rows[-1]["sha256"]
    assert f"chain head sha256: {head}" in out

    # a fresh folder with ONLY the JSONL, the dry-run DBs and the two journal anchors (the
    # pre-registration and the heads file): no candidate files, no candles, the same markdown
    fresh, fresh_journal = tmp_path / "fresh", tmp_path / "fresh-journal"
    (fresh / "jev-shadow").mkdir(parents=True)
    shutil.copy(decisions_path(cli_world.paper), fresh / "jev-shadow" / "decisions.jsonl")
    for cand in (CAND_A, CAND_B):
        (fresh / cand).mkdir()
        shutil.copy(cli_world.paper / cand / "dryrun.sqlite", fresh / cand / "dryrun.sqlite")
    fresh_journal.mkdir()
    for name in (rows[0]["prereg_journal"], HEADS_FILE):
        shutil.copy(cli_world.journal / name, fresh_journal / name)
    commit_heads(fresh_journal)
    assert (
        cli(cli_world, "shadow-report", "--asof", iso(asof), paper=fresh, journal=fresh_journal)
        == 0
    )
    assert markdown in capsys.readouterr().out

    # without the pre-registration the report refuses (fail closed)
    assert (
        cli(cli_world, "shadow-report", "--asof", iso(asof), paper=fresh, journal=tmp_path / "x")
        == 2
    )
    assert "is missing" in capsys.readouterr().err

    assert cli(cli_world, "shadow-report", "--asof", iso(asof), "--expect-head", head) == 0
    capsys.readouterr()
    assert cli(cli_world, "shadow-report", "--asof", iso(asof), "--expect-head", "0" * 64) == 2
    assert "expected head" in capsys.readouterr().err


def test_a_report_before_the_evaluation_time_is_an_interim_look(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    fixture_dbs(cli_world)
    asof = cli_world.a.decision_end + 10 * H4
    assert asof < evaluate_at(cli_world)
    advance_clock(cli_world, asof)
    capsys.readouterr()
    assert cli(cli_world, "shadow-report", "--asof", iso(asof)) == 0
    out = capsys.readouterr().out
    assert "Interim look" in out and "**Outcome:" not in out
    assert expected_markdown(cli_world.paper, asof) in out


def test_report_write_names_interim_and_final_reports_and_is_exclusive(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    fixture_dbs(cli_world)
    eva = evaluate_at(cli_world)
    early = cli_world.a.decision_end + 3 * H4
    advance_clock(cli_world, early)
    assert cli(cli_world, "shadow-report", "--asof", iso(early), "--write") == 0
    interim = shadow.shadow_dir(cli_world.paper) / report_name(early, eva)
    assert interim.name.startswith("interim-") and interim.is_file()
    assert interim.read_text(encoding="utf-8") == expected_markdown(cli_world.paper, early)

    late = eva + 2 * H4
    advance_clock(cli_world, late + H4)  # the last read below asks for late + H4
    commit_heads(cli_world.journal)
    assert cli(cli_world, "shadow-report", "--asof", iso(late), "--write") == 0
    final = shadow.shadow_dir(cli_world.paper) / report_name(late, eva)
    assert final.name == report_name(eva, eva) and final.name.startswith("report-")
    stamp = pd.Timestamp(eva, unit="s", tz="UTC").strftime("%Y%m%dT%H%MZ")
    assert final.name == f"report-{stamp}.md"
    first = final.read_bytes()
    assert first.decode("utf-8") == expected_markdown(cli_world.paper, late)
    assert expected_markdown(cli_world.paper, late) == expected_markdown(cli_world.paper, eva)
    capsys.readouterr()
    # any later asof is the same report: the second write is refused, nothing changes
    assert cli(cli_world, "shadow-report", "--asof", iso(late + H4), "--write") == 2
    assert "never overwritten" in capsys.readouterr().err
    assert final.read_bytes() == first
    assert cli(cli_world, "shadow-report", "--asof", iso(early), "--write") == 2
    assert interim.read_text(encoding="utf-8") == expected_markdown(cli_world.paper, early)


def test_report_refuses_a_rechained_log(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    flip_first_verdict(decisions_path(cli_world.paper))
    verify_chain(decisions_path(cli_world.paper))  # the chain alone does not see it
    assert cli(cli_world, "shadow-report") == 2
    assert "the log was rewritten after head" in capsys.readouterr().err


def test_report_refuses_a_missing_heads_file(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    heads_path(cli_world.journal).unlink()
    assert cli(cli_world, "shadow-report") == 2
    assert "heads file" in capsys.readouterr().err


def test_report_refuses_an_edited_preregistration(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    genesis = verify_chain(decisions_path(cli_world.paper))[0]
    jpath = cli_world.journal / genesis["prereg_journal"]
    jpath.write_text(jpath.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    assert cli(cli_world, "shadow-report") == 2
    assert "pre-registration changed" in capsys.readouterr().err


def test_report_refuses_changed_snapshot_or_question_hashes(
    cli_world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    capsys.readouterr()
    with monkeypatch.context() as m:
        m.setattr(shadow, "snapshot_fingerprint", lambda: "e" * 64)
        assert cli(cli_world, "shadow-report") == 2
        assert "snapshot fingerprint changed" in capsys.readouterr().err
    with monkeypatch.context() as m:
        m.setattr(shadow, "QUESTIONS_SHA256", "d" * 64)
        assert cli(cli_world, "shadow-report") == 2
        assert "question texts changed" in capsys.readouterr().err
    assert cli(cli_world, "shadow-report") == 0


def test_shadow_model_flag_is_frozen_into_the_genesis(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(cli_world, "shadow", "--once", "--model", "jev-1.13.0") == 0
    genesis = verify_chain(decisions_path(cli_world.paper))[0]
    assert genesis["model"] == "jev-1.13.0"
    text = (cli_world.journal / genesis["prereg_journal"]).read_text(encoding="utf-8")
    assert "Requested Jev model: `jev-1.13.0`" in text
    cli_world.clock.now = cli_world.first_fill + 30
    capsys.readouterr()
    assert cli(cli_world, "shadow", "--once") == 2  # the default model differs: refused
    assert "model is frozen" in capsys.readouterr().err
    assert cli(cli_world, "shadow", "--once", "--model", "jev-1.13.0") == 0
    assert cli(cli_world, "shadow", "--once", "--model", " ") == 2


def test_report_refuses_a_tampered_log(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    path = decisions_path(cli_world.paper)
    lines = path.read_bytes().split(b"\n")
    del lines[2]
    path.write_bytes(b"\n".join(lines))
    assert cli(cli_world, "shadow-report") == 2
    assert "does not verify" in capsys.readouterr().err


def test_report_refuses_a_log_with_a_deadline_problem(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A chain-valid log whose verdict was written after its deadline (e.g. a forged row
    appended through the log API) is reported as a problem and exits 2."""
    run_scenario(cli_world)
    rows = verify_chain(decisions_path(cli_world.paper))
    last_fill = max(r["fill_open"] for r in rows if r["kind"] == "bar")
    forged_f = last_fill + H4
    DecisionLog(decisions_path(cli_world.paper)).append(
        {
            "kind": "bar",
            "signal_bar": forged_f - H4,
            "fill_open": forged_f,
            "written_at": float(forged_f + GRACE + 10),
            "status": "no_signal",
            "checked": [CAND_A],
            "signals": [],
        }
    )
    assert cli(cli_world, "shadow-report") == 2
    assert "must be 'missed'" in capsys.readouterr().err


def test_report_without_a_log_exits_2(cli_world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(cli_world, "shadow-report") == 2
    assert "has not started" in capsys.readouterr().err


# --------------------------------------------------------------------------- heads file gaps


def test_report_refuses_a_tip_no_head_covers_and_counts_recovered_heads(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    hpath = heads_path(cli_world.journal)
    full = hpath.read_text(encoding="utf-8")
    hpath.write_text(shadow.HEADS_HEADER, encoding="utf-8")  # header only, decisions present
    capsys.readouterr()
    assert cli(cli_world, "shadow-report") == 2
    assert "behind the log" in capsys.readouterr().err
    # a crash between the log write and its head line: the report refuses, `shadow` recovers
    lines = full.splitlines(keepends=True)
    hpath.write_text("".join(lines[:-1]), encoding="utf-8")
    assert cli(cli_world, "shadow-report") == 2
    assert "behind the log" in capsys.readouterr().err
    assert cli(cli_world, "shadow", "--once") == 0
    out = capsys.readouterr().out
    assert "WARNING: the heads file did not cover" in out and "recovered" in out
    assert cli(cli_world, "shadow-report") == 0
    assert "1 recovered after a crash" in capsys.readouterr().out


def test_report_checks_the_heads_file_against_its_committed_copy(
    cli_world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    hpath = heads_path(cli_world.journal)
    _git(tmp_path, "init", "-q")  # the journal is a subfolder: HEAD:./<name> is repo-relative
    capsys.readouterr()
    assert cli(cli_world, "shadow-report") == 0  # a repo, but the file is not committed yet
    cap = capsys.readouterr()
    assert "not committed yet" in cap.err and "skipped" in cap.err
    _git(tmp_path, "add", "journal/" + HEADS_FILE)
    _git(tmp_path, "commit", "-q", "-m", "heads")
    assert cli(cli_world, "shadow-report") == 0
    assert "0 head lines not yet committed" in capsys.readouterr().out
    for f in range(cli_world.a.decision_end + H4, cli_world.a.decision_end + 3 * H4, H4):
        cli_world.clock.now = f + 30
        assert cli(cli_world, "shadow", "--once") == 0
    capsys.readouterr()
    assert cli(cli_world, "shadow-report") == 0
    pending = shadow.read_head_entries(hpath)
    assert len(pending) >= 1
    out = capsys.readouterr().out
    assert "head lines not yet committed" in out and "0 head lines not yet" not in out
    # edit a committed line (its timestamp only: every head still verifies against the log)
    text = hpath.read_text(encoding="utf-8")
    first = next(line for line in text.splitlines() if line.startswith("- "))
    edited = first.replace("- 20", "- 19", 1)
    assert edited != first
    hpath.write_text(text.replace(first, edited, 1), encoding="utf-8")
    assert (
        shadow.head_problems(
            verify_chain(decisions_path(cli_world.paper)), shadow.read_heads(hpath)
        )
        == []
    )
    assert cli(cli_world, "shadow-report") == 2
    assert "differs from the committed copy" in capsys.readouterr().err
    got = shadow.check_heads_committed(hpath)
    assert got.problem is not None and got.uncommitted is None


def test_report_without_git_warns_and_continues(
    cli_world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)

    def no_git(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(shadow.subprocess, "run", no_git)
    capsys.readouterr()
    assert cli(cli_world, "shadow-report") == 0
    assert "git is unavailable" in capsys.readouterr().err

    def slow_git(*_a: Any, **_k: Any) -> Any:
        raise subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr(shadow.subprocess, "run", slow_git)
    assert cli(cli_world, "shadow-report") == 0
    assert "git is unavailable" in capsys.readouterr().err


def test_final_report_exits_2_while_a_dryrun_db_is_unreadable(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    fixture_dbs(cli_world)
    db = cli_world.paper / CAND_B / "dryrun.sqlite"
    good = db.read_bytes()
    db.write_bytes(b"not a database" * 10)
    final = evaluate_at(cli_world) + H4
    advance_clock(cli_world, final)
    commit_heads(cli_world.journal)
    capsys.readouterr()
    assert cli(cli_world, "shadow-report", "--asof", iso(final), "--write") == 2
    err = capsys.readouterr().err
    assert f"final report refused: {CAND_B} dry-run DB unreadable; retry" in err
    assert not list(shadow.shadow_dir(cli_world.paper).glob("report-*.md"))  # nothing written
    early = cli_world.a.decision_end + 2 * H4
    assert cli(cli_world, "shadow-report", "--asof", iso(early)) == 0  # interim: a note
    out = capsys.readouterr().out
    assert "## Notes" in out and CAND_B in out and "could not be read" in out
    db.write_bytes(good)
    assert cli(cli_world, "shadow-report", "--asof", iso(final)) == 0
    out = capsys.readouterr().out
    assert "Matched trades sha256 `" in out and "## Notes" not in out


# --------------------------------------------------------------------------- round-4 fixes


def test_final_report_fails_closed_without_a_committed_heads_file(
    cli_world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a git checkout (or git missing): an interim look warns and goes on, the final read
    refuses, because the committed copy is the anchor the final report relies on."""
    run_scenario(cli_world)
    fixture_dbs(cli_world)
    final, early = evaluate_at(cli_world) + H4, cli_world.a.decision_end + 2 * H4
    advance_clock(cli_world, final)
    capsys.readouterr()
    assert cli(cli_world, "shadow-report", "--asof", iso(early)) == 0
    assert "not committed yet" in capsys.readouterr().err
    assert cli(cli_world, "shadow-report", "--asof", iso(final), "--write") == 2
    err = capsys.readouterr().err
    assert "final report refused: the committed heads check could not run" in err
    assert not list(shadow.shadow_dir(cli_world.paper).glob("report-*.md"))

    def no_git(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("git")

    commit_heads(cli_world.journal)
    with monkeypatch.context() as m:
        m.setattr(shadow.subprocess, "run", no_git)
        assert cli(cli_world, "shadow-report", "--asof", iso(early)) == 0
        assert "git is unavailable" in capsys.readouterr().err
        assert cli(cli_world, "shadow-report", "--asof", iso(final)) == 2
        assert "git is unavailable" in capsys.readouterr().err
    assert cli(cli_world, "shadow-report", "--asof", iso(final)) == 0


def test_final_report_refuses_uncommitted_head_lines_until_they_are_committed(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    fixture_dbs(cli_world)
    commit_heads(cli_world.journal)
    for f in range(cli_world.a.decision_end + H4, cli_world.a.decision_end + 3 * H4, H4):
        cli_world.clock.now = f + 30
        assert cli(cli_world, "shadow", "--once") == 0
    rows = verify_chain(decisions_path(cli_world.paper))
    check = shadow.check_heads_committed(heads_path(cli_world.journal))
    assert check.problem is None and check.uncommitted == 2
    assert check.committed_last_seq is not None and check.committed_last_seq < len(rows) - 1
    assert check.history_problem is None and check.versions == 1
    final, early = evaluate_at(cli_world) + H4, cli_world.a.decision_end + 2 * H4
    advance_clock(cli_world, final)
    capsys.readouterr()
    assert cli(cli_world, "shadow-report", "--asof", iso(early)) == 0  # interim: a note
    assert "2 head lines not yet committed" in capsys.readouterr().out
    assert cli(cli_world, "shadow-report", "--asof", iso(final)) == 2
    err = capsys.readouterr().err
    assert "final report refused: 2 head lines are not committed yet" in err
    assert f"is seq {len(rows) - 1}; commit and push" in err  # the committed copy is behind
    commit_heads(cli_world.journal, "more heads")
    assert cli(cli_world, "shadow-report", "--asof", iso(final)) == 0
    assert "0 head lines not yet committed" in capsys.readouterr().out
    again = shadow.check_heads_committed(heads_path(cli_world.journal))
    assert again.committed_last_seq == len(rows) - 1 and again.versions == 2


def test_a_committed_history_that_is_not_append_only_refuses_the_final_report(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A committed head line edited in one commit and restored in the next: HEAD is again a
    prefix of the working file, but the history shows the rewrite."""
    run_scenario(cli_world)
    fixture_dbs(cli_world)
    hpath = heads_path(cli_world.journal)
    original = hpath.read_text(encoding="utf-8")
    commit_heads(cli_world.journal, "v1")
    first = next(line for line in original.splitlines() if line.startswith("- "))
    hpath.write_text(original.replace(first, first.replace("- 20", "- 19", 1), 1), "utf-8")
    commit_heads(cli_world.journal, "v2 (edited)")
    hpath.write_text(original, encoding="utf-8")
    commit_heads(cli_world.journal, "v3 (restored)")
    check = shadow.check_heads_committed(hpath)
    assert check.problem is None and check.uncommitted == 0 and check.versions == 3
    assert check.history_problem is not None and "not append-only" in check.history_problem
    final, early = evaluate_at(cli_world) + H4, cli_world.a.decision_end + 2 * H4
    advance_clock(cli_world, final)
    capsys.readouterr()
    assert cli(cli_world, "shadow-report", "--asof", iso(early)) == 0  # interim: a warning
    assert "not append-only" in capsys.readouterr().err
    assert cli(cli_world, "shadow-report", "--asof", iso(final), "--write") == 2
    err = capsys.readouterr().err
    assert "final report refused: the committed history" in err and "not append-only" in err
    assert not list(shadow.shadow_dir(cli_world.paper).glob("report-*.md"))


def test_report_lists_recovered_heads_with_their_lag(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_scenario(cli_world)
    hpath = heads_path(cli_world.journal)
    lines = hpath.read_text(encoding="utf-8").splitlines(keepends=True)
    hpath.write_text("".join(lines[:-1]), encoding="utf-8")  # a crash before the head line
    rows = verify_chain(decisions_path(cli_world.paper))
    seq = len(rows) - 1
    cli_world.clock.now = rows[-1]["written_at"] + 3 * 3600 + 7  # recovered 3 h later
    assert cli(cli_world, "shadow", "--once") == 0
    entries = shadow.read_head_entries(hpath)
    rec = [e for e in entries if e.recovered]
    assert [e.seq for e in rec] == [seq]
    lag = rec[0].at - rows[-1]["written_at"]  # head lines carry whole seconds
    assert 3 * 3600 + 6 <= lag <= 3 * 3600 + 7
    lags = shadow.recovered_lags(entries, verify_chain(decisions_path(cli_world.paper)))
    assert lags == [(seq, pytest.approx(lag))]
    capsys.readouterr()
    asof = int(cli_world.clock.now) + 60
    advance_clock(cli_world, asof)
    assert cli(cli_world, "shadow-report", "--asof", iso(asof)) == 0
    out = capsys.readouterr().out
    assert f"1 recovered after a crash: seq {seq} lag {lag:.0f} s" in out
    assert f"written_at): seq {seq} (lag {lag:.0f} s)." in out


def test_a_future_asof_is_refused_and_writes_nothing(
    cli_world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """Quant review C-r4-1: an ``--asof`` later than the wall clock would read the
    pre-registered final test early on today's data; it exits 2 and writes no report."""
    run_scenario(cli_world)
    fixture_dbs(cli_world)
    commit_heads(cli_world.journal)
    eva = evaluate_at(cli_world)
    assert cli_world.clock.now < eva  # the clock is before the evaluation time
    for asof in (eva, eva + 5 * H4):
        capsys.readouterr()
        assert cli(cli_world, "shadow-report", "--asof", iso(asof), "--write") == 2
        err = capsys.readouterr().err
        assert "cannot build the shadow report" in err and "is in the future" in err
    sdir = shadow.shadow_dir(cli_world.paper)
    assert not list(sdir.glob("report-*.md")) and not list(sdir.glob("interim-*.md"))
    # once that time has passed, the same command builds and writes the final report
    advance_clock(cli_world, eva)
    assert cli(cli_world, "shadow-report", "--asof", iso(eva), "--write") == 0
    assert (sdir / report_name(eva, eva)).is_file()
