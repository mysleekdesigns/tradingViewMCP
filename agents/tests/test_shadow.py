"""Jev shadow judge process (Phase 17): pre-registration before any decision, signals equal the
lab's ``compile_plan``, one batched Jev call per signalled bar, the deadline rule (late ->
missed), Jev unavailable never a skip, eligibility windows, and no effect on the paper bots
(byte-identical files, the dry-run DB never opened). Everything runs in ``tmp_path`` on seeded
synthetic candles, a fake candle loader, a fake clock and a MockTransport Jev: fully offline."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest

from lab.plan import compile_plan, parse_plan
from lab.types import CostModel
from research.jev import JevClient
from research.journal import parse_frontmatter
from research.paper import shadow
from research.paper.freqtrade import prepare_candidate
from research.paper.protocol import (
    DesignStats,
    LedgerLink,
    PaperProtocol,
    build_protocol,
    journal_path,
    load_protocol_config,
    register_protocol,
)
from research.paper.shadow import (
    HEADS_FILE,
    SNAPSHOT_EXTRA_BARS,
    ShadowRefusedError,
    decisions_path,
    heads_path,
    read_heads,
    run_loop,
    run_once,
    shadow_dir,
)
from research.paper.shadow_log import (
    ChainError,
    canonical_json,
    check_deadlines,
    row_sha256,
    verify_chain,
)
from research.paper.shadow_report import build_report
from research.paper.shadow_snapshot import (
    CONTEXT,
    MIN_HISTORY_BARS,
    QUESTIONS_SHA256,
    SNAPSHOT_FIELDS,
    snapshot_fingerprint,
)

H4 = 4 * 3600
GRACE = 900
FAKE_KEY = "fake-typesafe-key-for-tests-0000"
COSTS = CostModel(fee_rate=0.001, slippage_rate=0.0005, slippage_vol_mult=0.0, slippage_cap=0.01)
LINK = LedgerLink(
    run_id="r-20261101-000000-abcdef",
    trial_id=1,
    window_start=int(pd.Timestamp("2024-01-01", tz="UTC").timestamp()),
    window_end=int(pd.Timestamp("2025-01-01", tz="UTC").timestamp()),
    wf_oos_sharpes=(0.4,),
    wf_evidence_ids=(3,),
    wf_run_ids=("r-20261101-000000-abcdef",),
)
CREATED_A = datetime(2026, 11, 2, 10, 30, 7, tzinfo=UTC)  # protocol start 2026-11-02 12:00
CREATED_B = CREATED_A + timedelta(days=1)  # six bars later
CAND_A = "hc-btc-test"
CAND_B = "hc-eth-test"


def plan_for(symbol: str, entry: str) -> dict[str, Any]:
    return {
        "name": f"shadow test {symbol}",
        "symbol": symbol,
        "interval": "4h",
        "features": {"fast": {"ema": {"period": 8}}, "slow": {"ema": {"period": 30}}},
        "long_entry": entry,
        "long_exit": "fast < slow",
        "risk": {"position_size_pct": 100},
    }


PLAN_A = plan_for("BTCUSDT", "fast > slow and close > fast")
PLAN_B = plan_for("ETHUSDT", "crosses_above(fast, slow)")


def design() -> DesignStats:
    n = 365 * 6
    index = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    rng = np.random.default_rng(3)
    returns = rng.normal(0.0005, 0.01, n)
    position = np.asarray([1.0 if (i // 10) % 3 == 0 else 0.0 for i in range(n)])
    starts = [i for i in range(n) if position[i] and (i == 0 or not position[i - 1])]
    return DesignStats(
        interval="4h",
        index=index,
        returns=returns,
        position=position,
        entry_times=pd.DatetimeIndex(index[starts]),
        window_start=index[0],
        window_end=index[-1] + pd.Timedelta(hours=4),
    )


def register(
    paper: Path, journal: Path, plan: dict[str, Any], candidate: str, created: datetime
) -> PaperProtocol:
    """A real registration (protocol.json + journal entry) and the real freqtrade files."""
    cfg = replace(load_protocol_config(), min_days=2.0, max_days=2.0)  # 12-bar window
    p = build_protocol(
        candidate=candidate,
        plan=plan,
        design=design(),
        costs=COSTS,
        config=cfg,
        created_at=created,
        link=LINK,
        claimed_sharpe_annual=1.0,
        claim_source="owner_supplied",
        claim_note="test claim",
    )
    register_protocol(p, paper_dir=paper, journal_dir=journal)
    prepare_candidate(
        plan, candidate=candidate, paper_dir=paper, warm_from=p.data.warm_from, start=p.start
    )
    return p


def synthetic(first: int, n: int, seed: int) -> pd.DataFrame:
    """Seeded random-walk 4h candles from ``first`` (UNIX s)."""
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, n)))
    open_ = np.concatenate(([100.0], close[:-1]))
    index = pd.DatetimeIndex(pd.to_datetime(first + H4 * np.arange(n), unit="s", utc=True))
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.003,
            "low": np.minimum(open_, close) * 0.997,
            "close": close,
            "volume": rng.uniform(50.0, 150.0, n),
            "tradable": True,
        },
        index=index,
    )


class Clock:
    def __init__(self, now: float) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


@dataclass
class Market:
    """Fake candle source: a bar is visible once it has closed by the fake clock (plus
    ``delay``), like the public klines host; ``fail`` makes every load raise."""

    frames: dict[str, pd.DataFrame]
    clock: Clock
    delay: float = 0.0
    fail: bool = False
    specs: list[dict[str, Any]] = field(default_factory=list)

    def loader_for(self, spec: Mapping[str, Any]) -> Callable[[str, str, pd.Timestamp], Any]:
        self.specs.append(dict(spec))
        since = pd.Timestamp(int(spec["since"]), unit="s", tz="UTC")

        def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
            assert interval == "4h"
            if self.fail:
                raise httpx.ConnectError("network down (test)")
            f = self.frames[symbol]
            closes = f.index + pd.Timedelta(hours=4)
            limit = pd.Timestamp(self.clock() - self.delay, unit="s", tz="UTC")
            return f.loc[(f.index >= since) & (f.index < end) & (closes <= limit)]

        return load


def take_first(request: httpx.Request) -> httpx.Response:
    """Jev answers: s1 take, every other label skip."""
    body = json.loads(request.content)
    answers: dict[str, Any] = {}
    for qid, q in body["questions"].items():
        label = qid.split("_")[0]
        if q["type"] == "choice":
            choice = "take" if label == "s1" else "skip"
            answers[qid] = {
                "type": "choice",
                "choice": choice,
                "confidence": 0.6,
                "probabilities": {"take": 0.6, "skip": 0.4}
                if choice == "take"
                else {"take": 0.4, "skip": 0.6},
            }
        elif q["type"] == "noul":
            answers[qid] = {"type": "noul", "noul": 0.3}
        else:
            answers[qid] = {"type": "score", "score": 0.7, "confidence": 0.5}
    return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers, "usage": {}})


@dataclass
class World:
    paper: Path
    journal: Path
    a: PaperProtocol
    b: PaperProtocol
    market: Market
    clock: Clock
    requests: list[httpx.Request]
    handler: list[Callable[[httpx.Request], httpx.Response]]

    def client(self, api_key: str | None = FAKE_KEY, model: str = "jev-latest") -> JevClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.handler[0](request)

        return JevClient(
            api_key=api_key,
            model=model,
            transport=httpx.MockTransport(handle),
            max_retries=0,
            sleep=lambda _s: None,
        )

    def run(self, **kw: Any) -> shadow.RunSummary:
        client = kw.pop("client", None) or self.client()
        return run_once(
            self.paper,
            self.journal,
            client=client,
            loader_for=self.market.loader_for,
            now_fn=self.clock,
            grace_s=kw.pop("grace_s", GRACE),
            **kw,
        )

    def rows(self) -> list[dict[str, Any]]:
        return verify_chain(decisions_path(self.paper))

    @property
    def first_fill(self) -> int:
        return self.a.start + H4  # the first fill bar whose signal bar is in A's window

    def expected_signal(self, proto: PaperProtocol, plan: dict[str, Any], f: int) -> bool:
        frame = self.market.frames[proto.symbol]
        lo = pd.Timestamp(proto.data.warm_from, unit="s", tz="UTC")
        hi = pd.Timestamp(f, unit="s", tz="UTC")
        hist = frame.loc[(frame.index >= lo) & (frame.index < hi)]
        t = pd.Timestamp(f - H4, unit="s", tz="UTC")
        return bool(compile_plan(parse_plan(plan), hist).entries.loc[t])


def make_world(tmp_path: Path, seed_a: int = 11, seed_b: int = 12) -> World:
    paper, journal = tmp_path / "paper", tmp_path / "journal"
    a = register(paper, journal, PLAN_A, CAND_A, CREATED_A)
    b = register(paper, journal, PLAN_B, CAND_B, CREATED_B)
    # history from before the snapshot's extra bars (snapshot_since = warm_from - 300 bars)
    first = min(a.data.warm_from, b.data.warm_from) - (SNAPSHOT_EXTRA_BARS + 10) * H4
    n = (b.decision_end - first) // H4 + 20
    frames = {"BTCUSDT": synthetic(first, n, seed_a), "ETHUSDT": synthetic(first, n, seed_b)}
    clock = Clock(a.start - 3600)  # genesis an hour before A's start: genesis start = A.start
    return World(paper, journal, a, b, Market(frames, clock), clock, [], [take_first])


def walk(world: World, fills: range, offset: float = 30.0) -> list[shadow.RunSummary]:
    """One run_once per fill bar, ``offset`` seconds after it opens (inside the grace)."""
    out = []
    for f in fills:
        world.clock.now = f + offset
        out.append(world.run())
    return out


def bars(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {r["fill_open"]: r for r in rows if r["kind"] == "bar"}


def decisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["kind"] == "decision"]


def file_hashes(world: World) -> dict[str, str]:
    """sha256 of every file in each candidate folder and of each candidate journal entry."""
    out: dict[str, str] = {}
    for proto in (world.a, world.b):
        folder = world.paper / proto.candidate
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        jpath = journal_path(proto, world.journal)
        out[str(jpath)] = hashlib.sha256(jpath.read_bytes()).hexdigest()
    return out


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


# --------------------------------------------------------------------------- watches only


def test_bot_files_are_byte_identical_and_the_dryrun_db_is_never_opened(world: World) -> None:
    before = file_hashes(world)
    listing = {p.name for p in world.paper.iterdir()}
    names = {
        "TvPaperHcBtcTest.py",
        "config.json",
        "meta.json",
        "plan.json",
        "protocol.json",
    }
    assert names <= {p.name for p in (world.paper / CAND_A).iterdir()}
    world.run()  # genesis
    walk(world, range(world.first_fill, world.b.decision_end + 2 * H4, H4))
    world.clock.now = world.b.decision_end + 10 * H4  # a late run too (missed bars)
    world.run()
    assert file_hashes(world) == before
    for proto in (world.a, world.b):
        assert not (world.paper / proto.candidate / "dryrun.sqlite").exists()
    assert {p.name for p in world.paper.iterdir()} == listing | {"jev-shadow"}
    assert not (shadow_dir(world.paper) / "protocol.json").exists()
    new_journal = {p.name for p in world.journal.iterdir()} - {
        journal_path(world.a, world.journal).name,
        journal_path(world.b, world.journal).name,
    }
    assert len(new_journal) == 2 and HEADS_FILE in new_journal
    (name,) = new_journal - {HEADS_FILE}
    assert name.endswith("-multi-jev-shadow.md") and "-paper-" not in name
    assert "-paper-" not in HEADS_FILE


# --------------------------------------------------------------------------- signals


def test_signals_equal_compile_plan_and_one_jev_call_per_signalled_bar(world: World) -> None:
    world.run()
    fills = range(world.first_fill, world.b.decision_end + H4, H4)
    summaries = walk(world, fills)
    rows = world.rows()
    by_fill = bars(rows)
    assert sorted(by_fill) == list(fills)
    n_signal_bars = 0
    for f, summary in zip(fills, summaries, strict=True):
        bar = by_fill[f]
        assert [o.fill_open for o in summary.bars] == [f]
        expected = []
        for proto, plan in ((world.a, PLAN_A), (world.b, PLAN_B)):
            if proto.candidate in bar["checked"] and world.expected_signal(proto, plan, f):
                expected.append(proto.candidate)
        assert bar["signals"] == sorted(expected)
        mine = [d for d in decisions(rows) if d["fill_open"] == f]
        assert sorted(d["candidate"] for d in mine) == sorted(expected)
        if expected:
            n_signal_bars += 1
            assert bar["status"] == "judged"
            assert all(d["status"] == "judged" for d in mine)
        else:
            assert bar["status"] == "no_signal"
    assert n_signal_bars >= 2, "the fixture must have bars with signals"
    assert n_signal_bars < len(fills), "the fixture must have no-signal bars"
    assert len(world.requests) == n_signal_bars  # one batched call per bar with signals
    assert check_deadlines(rows, GRACE) == []


def test_the_request_is_blind_and_batched(world: World) -> None:
    world.run()
    walk(world, range(world.first_fill, world.b.decision_end + H4, H4))
    rows = world.rows()
    for request in world.requests:
        body = json.loads(request.content)
        assert body["state"]["context"] == CONTEXT  # the fixed context names BTC generically
        text = json.dumps(body["state"]["setups"]) + json.dumps(body["questions"])
        for leak in ("BTC", "ETH", "USDT", CAND_A, CAND_B, "2026", "17936"):
            assert leak not in text
    for d in decisions(rows):
        assert tuple(d["snapshot"]) == tuple(sorted(SNAPSHOT_FIELDS))
        assert d["verdict"]["choice"] in ("take", "skip")
        assert d["model"] == "jev-1.13.0"
    two = [f for f, b in bars(rows).items() if len(b["signals"]) == 2]
    if two:  # both candidates on one bar: ONE request with two setups
        body_sizes = [len(json.loads(r.content)["state"]["setups"]) for r in world.requests]
        assert max(body_sizes) == 2


def test_eligibility_follows_each_protocol_window(world: World) -> None:
    world.run()
    fills = range(world.a.start, world.b.decision_end + 3 * H4, H4)
    walk(world, fills)
    by_fill = bars(world.rows())
    for f in fills:
        t = f - H4
        want = [p.candidate for p in (world.a, world.b) if p.start <= t < p.decision_end]
        if want:
            assert by_fill[f]["checked"] == sorted(want)
        else:
            assert f not in by_fill  # nothing to check: no row
    assert world.a.start not in by_fill  # its signal bar is before A's protocol start
    t_end_a = world.a.decision_end
    assert CAND_A not in by_fill[t_end_a + H4]["checked"]  # t == decision_end is excluded
    assert by_fill[world.a.start + H4]["checked"] == [CAND_A]  # B not started yet


# --------------------------------------------------------------------------- deadline


def test_a_verdict_arriving_after_the_deadline_is_logged_missed(world: World) -> None:
    world.run()
    # find a bar with a signal
    f = next(
        f
        for f in range(world.first_fill, world.a.decision_end, H4)
        if world.expected_signal(world.a, PLAN_A, f)
    )
    walk(world, range(world.first_fill, f, H4))

    def slow(request: httpx.Request) -> httpx.Response:
        world.clock.now = f + GRACE  # the answer arrives exactly at the deadline
        return take_first(request)

    world.handler[0] = slow
    world.clock.now = f + 60
    world.run()
    rows = world.rows()
    bar = bars(rows)[f]
    assert bar["status"] == "missed" and bar["reason"] == "verdict after deadline"
    late = [d for d in decisions(rows) if d["fill_open"] == f]
    assert late and all(d["status"] == "missed" and d["verdict"] is None for d in late)
    assert all(d["snapshot"] is not None and d["written_at"] >= f + GRACE for d in late)
    assert check_deadlines(rows, GRACE) == []


def test_a_run_after_the_deadline_logs_missed_without_asking_jev(world: World) -> None:
    world.run()
    fills = range(world.first_fill, world.first_fill + 6 * H4, H4)
    world.clock.now = fills[-1] + GRACE + 1  # every one of these bars is past its deadline
    world.run()
    rows = world.rows()
    by_fill = bars(rows)
    assert sorted(by_fill) == list(fills)
    assert world.requests == []
    assert any(by_fill[f]["signals"] for f in fills), "the fixture must have a missed signal"
    for f in fills:
        assert by_fill[f]["status"] == "missed"
        want = [CAND_A] if world.expected_signal(world.a, PLAN_A, f) else []
        assert by_fill[f]["signals"] == want  # signals still computed, for the counts
        for d in decisions(rows):
            if d["fill_open"] == f:
                assert d["status"] == "missed" and d["snapshot"] is None
                assert d["snapshot_sha256"] is None and d["verdict"] is None
    assert check_deadlines(rows, GRACE) == []


def test_no_verdict_is_ever_written_at_or_after_its_deadline(world: World) -> None:
    world.run()
    fills = range(world.first_fill, world.b.decision_end + H4, H4)
    for i, f in enumerate(fills):
        world.clock.now = f + (30 if i % 3 else GRACE - 1)  # some right at the edge
        world.run()
    rows = world.rows()
    for r in rows[1:]:
        if r["status"] != "missed":
            assert r["fill_open"] <= r["written_at"] < r["fill_open"] + GRACE
    assert check_deadlines(rows, GRACE) == []


def test_missing_signal_candle_waits_then_becomes_missed(world: World) -> None:
    world.run()
    f = world.first_fill
    world.market.delay = 3600.0  # the exchange has not published the close yet
    world.clock.now = f + 30
    summary = world.run()
    assert summary.bars == [] and [p[0] for p in summary.pending] == [f]
    assert "no closed candle for the signal bar" in summary.pending[0][1]
    assert len(world.rows()) == 1  # nothing written for the bar
    world.clock.now = f + GRACE  # still unpublished at the deadline
    world.run()
    bar = bars(world.rows())[f]
    assert bar["status"] == "missed" and bar["unknown"] == [CAND_A]
    assert "no closed candle for the signal bar" in bar["reason"]
    assert check_deadlines(world.rows(), GRACE) == []


def test_network_errors_write_nothing_before_the_deadline(world: World) -> None:
    world.run()
    f = world.first_fill
    world.market.fail = True
    world.clock.now = f + 30
    summary = world.run()
    assert summary.pending and len(world.rows()) == 1
    world.clock.now = f + GRACE + 5
    world.run()
    bar = bars(world.rows())[f]
    assert bar["status"] == "missed" and bar["signals"] == []
    assert "candles not loaded" in bar["reason"]
    assert check_deadlines(world.rows(), GRACE) == []


# --------------------------------------------------------------------------- unavailable


def _signal_fills(world: World) -> list[int]:
    return [
        f
        for f in range(world.first_fill, world.a.decision_end, H4)
        if world.expected_signal(world.a, PLAN_A, f)
    ]


def test_no_api_key_logs_unavailable_never_skip(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    client = world.client(api_key="")
    assert not client.enabled
    world.run(client=client)
    for f in range(world.first_fill, world.a.decision_end, H4):
        world.clock.now = f + 30
        world.run(client=client)
    rows = world.rows()
    ds = decisions(rows)
    assert ds and all(d["status"] == "unavailable" and d["verdict"] is None for d in ds)
    assert all(d["reason"] == "jev unavailable" for d in ds)
    assert world.requests == []
    assert all(b["status"] in ("unavailable", "no_signal") for b in bars(rows).values())
    assert check_deadlines(rows, GRACE) == []


def test_jev_http_500_logs_unavailable(world: World) -> None:
    world.handler[0] = lambda _r: httpx.Response(500, json={"error": "down"})
    world.run()
    fills = _signal_fills(world)[:2]
    for f in fills:
        world.clock.now = f + 30
        world.run()
    rows = world.rows()
    for f in fills:
        assert bars(rows)[f]["status"] == "unavailable"
    ds = decisions(rows)
    assert ds and all(d["status"] == "unavailable" and d["verdict"] is None for d in ds)
    assert all(d["snapshot"] is not None for d in ds)
    assert check_deadlines(rows, GRACE) == []


# --------------------------------------------------------------------------- chain


def test_editing_or_deleting_a_row_breaks_the_chain(world: World, tmp_path: Path) -> None:
    world.run()
    walk(world, range(world.first_fill, world.first_fill + 8 * H4, H4))
    path = decisions_path(world.paper)
    rows = verify_chain(path)
    assert len(rows) > 4
    lines = path.read_bytes().split(b"\n")[:-1]

    edited = tmp_path / "edited.jsonl"
    row = json.loads(lines[2])
    row["status"] = "judged" if row["status"] != "judged" else "no_signal"
    bad = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    edited.write_bytes(b"\n".join([*lines[:2], bad, *lines[3:]]) + b"\n")
    with pytest.raises(ChainError):
        verify_chain(edited)

    deleted = tmp_path / "deleted.jsonl"
    deleted.write_bytes(b"\n".join([*lines[:2], *lines[3:]]) + b"\n")
    with pytest.raises(ChainError):
        verify_chain(deleted)

    truncated = tmp_path / "truncated.jsonl"
    truncated.write_bytes(b"\n".join(lines[:-1]) + b"\n")
    with pytest.raises(ChainError):
        verify_chain(truncated, expect_head=rows[-1]["sha256"])


def test_a_broken_chain_stops_the_judge(world: World) -> None:
    world.run()
    walk(world, range(world.first_fill, world.first_fill + 2 * H4, H4))
    path = decisions_path(world.paper)
    lines = path.read_bytes().split(b"\n")
    row = json.loads(lines[1])
    row["written_at"] -= 1.0  # back-date one row
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(b"\n".join(lines))
    world.clock.now = world.first_fill + 2 * H4 + 30
    with pytest.raises(ChainError):
        world.run()


# --------------------------------------------------------------------------- pre-registration


def test_preregistration_is_written_before_genesis_and_every_decision(world: World) -> None:
    summary = world.run()
    assert summary.created
    rows = world.rows()
    assert len(rows) == 1 and rows[0]["kind"] == "genesis"
    genesis = rows[0]
    jpath = world.journal / genesis["prereg_journal"]
    text = jpath.read_text(encoding="utf-8")
    front = parse_frontmatter(text)
    assert front["run_id"] == "jev-shadow" and front["outcome"] == "pending"
    assert front["idea_ids"] == [] and "jev-shadow" in front["tags"]
    assert hashlib.sha256(jpath.read_bytes()).hexdigest() == genesis["prereg_sha256"]
    assert CONTEXT in text and QUESTIONS_SHA256 in text
    assert "inconclusive" in text and "no information" in text
    assert "Results are historical, not forward-looking." in text
    for proto in (world.a, world.b):
        assert proto.plan_hash in text
    assert genesis["start"] == world.a.start  # first bar boundary strictly after now
    assert genesis["start"] > genesis["written_at"]
    assert genesis["model"] == "jev-latest" and genesis["grace_s"] == GRACE
    assert [c["candidate"] for c in genesis["candidates"]] == [CAND_A, CAND_B]
    extra = SNAPSHOT_EXTRA_BARS * H4
    since = {c["candidate"]: c["snapshot_since"] for c in genesis["candidates"]}
    assert since == {CAND_A: world.a.data.warm_from - extra, CAND_B: world.b.data.warm_from - extra}
    assert genesis["btc_since"] == min(since.values())
    assert genesis["analysis"]["min_judged_trades"] == 30
    assert genesis["analysis"]["min_per_arm"] == 8
    assert genesis["analysis"]["test"] == "cluster_permutation_two_sided"
    assert genesis["analysis"]["model_rule"] == "first_served_model_major_minor"
    assert genesis["analysis"]["strata"] == "same_size_within_block"
    assert genesis["analysis"]["block_days"] == 7
    assert genesis["env"] == shadow.current_env()  # python, numpy, pandas, uv.lock sha256
    assert f"numpy `{np.__version__}`" in text and f"pandas `{pd.__version__}`" in text
    assert genesis["snapshot_fingerprint"] == snapshot_fingerprint()
    assert "Evaluation time" in text and "interim look" in text
    assert "same size in the same block" in text and "first judged decision" in text
    assert "major.minor" in text and "`jev-1.13.0` and" in text and "no served id" in text
    assert "discriminates between setups of the same" in text
    assert "BTC setup" in text and "not look-ahead" in text
    assert snapshot_fingerprint() in text and HEADS_FILE in text
    # round 4: the tested statistic is the centred within-block difference, defined in full
    assert genesis["analysis"]["statistic"] == "centred_within_block_diff"
    assert "statistic `centred_within_block_diff`" in text
    assert "E0[S] = sum over s of (n_take_s / n_s) * R_s" in text
    assert "T = (d(S) - d(E0[S])) / c" in text and "Why" in text and "one-sided" in text
    assert "by the sign of T_obs" in text and "description only" in text
    assert "names no version and counts" in text and "as no served id" in text
    assert "final report (at or after the evaluation time) is refused unless" in text

    walk(world, range(world.first_fill, world.first_fill + 6 * H4, H4))
    rows = world.rows()
    assert sum(r["kind"] == "genesis" for r in rows) == 1  # never a second genesis
    assert all(r["written_at"] > genesis["written_at"] for r in rows[1:])
    assert jpath.read_bytes() == text.encode("utf-8")  # never edited


def test_editing_the_preregistration_makes_the_next_run_refuse(world: World) -> None:
    world.run()
    jpath = world.journal / world.rows()[0]["prereg_journal"]
    jpath.write_text(jpath.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    world.clock.now = world.first_fill + 30
    with pytest.raises(ShadowRefusedError, match="pre-registration changed"):
        world.run()
    assert len(world.rows()) == 1


def test_a_leftover_preregistration_without_a_log_refuses(world: World) -> None:
    world.run()
    decisions_path(world.paper).unlink()
    with pytest.raises(ShadowRefusedError, match="already exists"):
        world.run()


def test_a_changed_candidate_protocol_or_plan_makes_the_run_refuse(world: World) -> None:
    world.run()
    world.clock.now = world.first_fill + 30
    ppath = world.paper / CAND_B / "protocol.json"
    original = ppath.read_bytes()
    data = json.loads(original)
    data["decision_end"] += H4
    ppath.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ShadowRefusedError, match=CAND_B):
        world.run()
    ppath.write_bytes(original)
    plan_path = world.paper / CAND_A / "plan.json"
    plan_path.write_text(json.dumps(plan_for("BTCUSDT", "fast > slow")), encoding="utf-8")
    with pytest.raises(ShadowRefusedError, match=CAND_A):
        world.run()
    assert len(world.rows()) == 1


def test_a_candidate_registered_after_genesis_is_ignored(world: World) -> None:
    world.run()
    late = register(
        world.paper,
        world.journal,
        plan_for("SOLUSDT", "close > slow"),
        "hc-sol-test",
        CREATED_A,
    )
    world.clock.now = world.first_fill + 30
    summary = world.run()
    assert any("hc-sol-test" in w and "after the shadow genesis" in w for w in summary.warnings)
    assert all(late.candidate not in b["checked"] for b in bars(world.rows()).values())
    assert summary.candidates == [CAND_A, CAND_B]


def test_grace_and_model_are_frozen_by_genesis(world: World) -> None:
    world.run()
    with pytest.raises(ShadowRefusedError, match="grace"):
        world.run(grace_s=600)
    with pytest.raises(ShadowRefusedError, match="grace_s must be"):
        world.run(grace_s=-1)
    other = JevClient(
        api_key=FAKE_KEY, model="jev-other", transport=httpx.MockTransport(take_first)
    )
    with pytest.raises(ShadowRefusedError, match="model"):
        world.run(client=other)


def test_only_one_shadow_process_at_a_time(world: World) -> None:
    world.run()
    with (
        shadow._process_lock(shadow_dir(world.paper)),
        pytest.raises(ShadowRefusedError, match="another shadow process"),
    ):
        world.run()


def test_no_candidate_refuses_and_writes_nothing(tmp_path: Path) -> None:
    clock = Clock(1_800_000_000)
    with pytest.raises(ShadowRefusedError, match="no verified registered candidate"):
        run_once(
            tmp_path / "paper",
            tmp_path / "journal",
            client=JevClient(api_key=FAKE_KEY),
            loader_for=lambda _s: lambda *_a: pd.DataFrame(),
            now_fn=clock,
        )
    assert not decisions_path(tmp_path / "paper").exists()
    assert not (tmp_path / "journal").exists()


# --------------------------------------------------------------------------- loop


def test_run_loop_survives_a_failed_run_and_retries_soon(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.run()
    calls: list[int] = []
    real = shadow.run_once

    def flaky(*args: Any, **kwargs: Any) -> shadow.RunSummary:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return real(*args, **kwargs)

    monkeypatch.setattr(shadow, "run_once", flaky)
    sleeps: list[float] = []
    errors: list[Exception] = []
    world.clock.now = world.first_fill + 30
    code = run_loop(
        world.paper,
        world.journal,
        client=world.client(),
        loader_for=world.market.loader_for,
        now_fn=world.clock,
        grace_s=GRACE,
        sleep_fn=sleeps.append,
        on_error=errors.append,
        max_iterations=3,
    )
    assert code == 0 and len(calls) == 3 and len(errors) == 1
    assert sleeps[0] == pytest.approx(60.0)  # a failed run retries after retry_s
    assert sleeps[1] == pytest.approx(world.first_fill + H4 + 20 - world.clock.now)
    assert world.first_fill in bars(world.rows())


def test_run_loop_exits_cleanly_on_ctrl_c(world: World) -> None:
    def interrupt(_s: float) -> None:
        raise KeyboardInterrupt

    code = run_loop(
        world.paper,
        world.journal,
        client=world.client(),
        loader_for=world.market.loader_for,
        now_fn=world.clock,
        sleep_fn=interrupt,
    )
    assert code == 0


def test_run_loop_stops_on_a_refusal(world: World) -> None:
    world.run()
    jpath = world.journal / world.rows()[0]["prereg_journal"]
    jpath.write_text("edited", encoding="utf-8")
    with pytest.raises(ShadowRefusedError):
        run_loop(
            world.paper,
            world.journal,
            client=world.client(),
            loader_for=world.market.loader_for,
            now_fn=world.clock,
            sleep_fn=lambda _s: None,
            max_iterations=2,
        )


def test_default_loader_is_the_frozen_public_source(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_public(*, since: pd.Timestamp, host: str) -> str:
        seen.update(since=since, host=host)
        return "loader"

    monkeypatch.setattr(shadow, "public_kline_loader", fake_public)
    spec = {"candle_source": "api", "candle_host": "data-api.binance.vision", "since": 3600}
    assert shadow.default_loader_for(spec) == "loader"
    assert seen == {"since": pd.Timestamp(3600, unit="s", tz="UTC"), "host": spec["candle_host"]}


# --------------------------------------------------------------------------- evaluation time


def test_genesis_evaluate_at_is_the_latest_decision_end_plus_14_days(world: World) -> None:
    world.run()
    analysis = world.rows()[0]["analysis"]
    assert analysis["evaluate_after_days"] == 14
    latest = max(world.a.decision_end, world.b.decision_end)
    assert latest == world.b.decision_end
    assert analysis["evaluate_at"] == latest + 14 * 86400


# --------------------------------------------------------------------------- tamper anchor


def rechain(path: Path, index: int, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Edit row ``index`` and re-chain every row after it: the attack an unkeyed chain cannot
    see on its own (the result is a valid chain)."""
    rows = verify_chain(path)
    prev = rows[index - 1]["sha256"]
    out = [canonical_json(r) for r in rows[:index]]
    for i, row in enumerate(rows[index:], start=index):
        body = {k: v for k, v in row.items() if k != "sha256"}
        if i == index:
            mutate(body)
        body["prev_sha256"] = prev
        body["sha256"] = row_sha256(body)
        prev = body["sha256"]
        out.append(canonical_json(body))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    verify_chain(path)  # still a valid chain


def flip_first_verdict(path: Path) -> int:
    """Re-chain the log with the first judged verdict flipped; return its row index."""
    rows = verify_chain(path)
    index = next(
        i for i, r in enumerate(rows) if r["kind"] == "decision" and r["status"] == "judged"
    )

    def flip(body: dict[str, Any]) -> None:
        verdict = dict(body["verdict"])
        verdict["choice"] = "skip" if verdict["choice"] == "take" else "take"
        body["verdict"] = verdict

    rechain(path, index, flip)
    return index


def test_heads_file_gets_one_line_per_append(world: World) -> None:
    world.run()
    hpath = heads_path(world.journal)
    text = hpath.read_text(encoding="utf-8")
    assert "Append-only" in text and "Commit this file" in text
    fills = range(world.first_fill, world.first_fill + 6 * H4, H4)
    walk(world, fills)
    rows = world.rows()
    heads = read_heads(hpath)
    assert len(heads) == 1 + len(fills)  # genesis + one per bar written
    assert heads[0] == (0, rows[0]["sha256"])
    assert heads[-1] == (len(rows) - 1, rows[-1]["sha256"])
    for seq, sha in heads:
        assert rows[seq]["sha256"] == sha
        assert rows[seq]["kind"] in ("genesis", "bar")  # the last row of each append
    lines = [x for x in hpath.read_text(encoding="utf-8").splitlines() if x.startswith("- ")]
    assert lines[-1].endswith(f"seq {len(rows) - 1} rows {len(rows)} head {rows[-1]['sha256']}")
    assert hpath.read_text(encoding="utf-8").startswith(text)  # appended, never rewritten


def test_a_rechained_log_is_refused_by_the_next_run(world: World) -> None:
    world.run()
    walk(world, range(world.first_fill, world.a.decision_end, H4))
    flip_first_verdict(decisions_path(world.paper))
    world.clock.now = world.a.decision_end + 30
    with pytest.raises(ShadowRefusedError, match="rewritten after head"):
        world.run()


def test_rows_deleted_from_the_end_are_refused(world: World) -> None:
    world.run()
    walk(world, range(world.first_fill, world.first_fill + 4 * H4, H4))
    path = decisions_path(world.paper)
    lines = path.read_bytes().split(b"\n")[:-1]
    path.write_bytes(b"\n".join(lines[:-1]) + b"\n")  # the last bar row: still a valid chain
    verify_chain(path)
    with pytest.raises(ShadowRefusedError, match="rows deleted"):
        world.run()


def test_a_missing_heads_file_is_refused_once_there_are_decisions(world: World) -> None:
    world.run()
    hpath = heads_path(world.journal)
    hpath.unlink()  # only the genesis so far: recreated (a crash before the heads file)
    world.clock.now = world.first_fill + 30
    world.run()
    assert read_heads(hpath)[0] == (0, world.rows()[0]["sha256"])
    hpath.unlink()
    world.clock.now = world.first_fill + H4 + 30
    with pytest.raises(ShadowRefusedError, match="heads file"):
        world.run()


def _drop_last_head_lines(world: World, n: int) -> None:
    """Simulate a crash after the log append but before its head line(s) were written."""
    hpath = heads_path(world.journal)
    lines = hpath.read_text(encoding="utf-8").splitlines(keepends=True)
    head_idx = [i for i, line in enumerate(lines) if line.startswith("- ")]
    drop = set(head_idx[-n:])
    hpath.write_text("".join(x for i, x in enumerate(lines) if i not in drop), encoding="utf-8")


def test_a_crash_before_the_head_line_is_recovered_once_and_marked(world: World) -> None:
    world.run()
    walk(world, range(world.first_fill, world.first_fill + 3 * H4, H4))
    _drop_last_head_lines(world, 1)
    rows = world.rows()
    hpath = heads_path(world.journal)
    assert shadow.tip_problem(rows, read_heads(hpath)) is not None
    before = hpath.read_text(encoding="utf-8")
    world.clock.now = world.first_fill + 3 * H4 + 30
    summary = world.run()
    assert any("marked 'recovered'" in w and w.startswith("WARNING:") for w in summary.warnings)
    text = hpath.read_text(encoding="utf-8")
    assert text.startswith(before)  # appended, never rewritten
    entries = shadow.read_head_entries(hpath)
    recovered = [e for e in entries if e.recovered]
    assert len(recovered) == 1 and recovered[0].seq == len(rows) - 1
    assert recovered[0].sha256 == rows[-1]["sha256"]
    assert next(x for x in text.splitlines() if x.endswith(" recovered")).startswith("- ")
    rows = world.rows()
    assert len(rows) > recovered[0].seq + 1  # the run went on and logged its own bar
    assert shadow.tip_problem(rows, read_heads(hpath)) is None
    again = world.run()  # nothing uncovered now: no second recovery
    assert not any("recovered" in w for w in again.warnings)
    assert sum(e.recovered for e in shadow.read_head_entries(hpath)) == 1


def test_more_than_one_unanchored_write_is_refused_not_recovered(world: World) -> None:
    world.run()
    walk(world, range(world.first_fill, world.first_fill + 3 * H4, H4))
    _drop_last_head_lines(world, 2)
    before = heads_path(world.journal).read_bytes()
    world.clock.now = world.first_fill + 3 * H4 + 30
    with pytest.raises(ShadowRefusedError, match="More than one write is unanchored"):
        world.run()
    assert heads_path(world.journal).read_bytes() == before


def test_a_refused_run_writes_no_recovered_head_line(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grace, model, candidate and fingerprint refusals run BEFORE the crash recovery appends
    its head line, so a refused run leaves the heads file (and the log) byte-identical."""
    world.run()
    walk(world, range(world.first_fill, world.first_fill + 3 * H4, H4))
    _drop_last_head_lines(world, 1)  # a crash before the head line: recoverable
    hpath, log = heads_path(world.journal), decisions_path(world.paper)
    heads_before, log_before = hpath.read_bytes(), log.read_bytes()
    world.clock.now = world.first_fill + 3 * H4 + 30
    other = JevClient(
        api_key=FAKE_KEY, model="jev-other", transport=httpx.MockTransport(take_first)
    )
    with pytest.raises(ShadowRefusedError, match="grace"):
        world.run(grace_s=GRACE + 1)
    with pytest.raises(ShadowRefusedError, match="model is frozen"):
        world.run(client=other)
    with monkeypatch.context() as m:
        m.setattr(shadow, "snapshot_fingerprint", lambda: "f" * 64)
        with pytest.raises(ShadowRefusedError, match="snapshot fingerprint changed"):
            world.run()
    protocol = world.paper / CAND_B / "protocol.json"
    good = protocol.read_bytes()
    data = json.loads(good)
    data["decision_end"] += H4
    protocol.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ShadowRefusedError, match=CAND_B):
        world.run()
    protocol.write_bytes(good)
    assert hpath.read_bytes() == heads_before and log.read_bytes() == log_before
    summary = world.run()  # nothing refused now: the recovery happens
    assert any("marked 'recovered'" in w for w in summary.warnings)
    assert sum(e.recovered for e in shadow.read_head_entries(hpath)) == 1


def test_a_refused_run_does_not_recreate_a_missing_heads_file(world: World) -> None:
    world.run()
    hpath = heads_path(world.journal)
    hpath.unlink()  # a crash between the genesis row and the heads file
    world.clock.now = world.first_fill + 30
    with pytest.raises(ShadowRefusedError, match="grace"):
        world.run(grace_s=GRACE + 1)
    assert not hpath.exists()
    world.run()
    assert read_heads(hpath)[0] == (0, world.rows()[0]["sha256"])


def test_a_header_only_heads_file_with_decisions_is_refused(world: World) -> None:
    world.run()
    walk(world, range(world.first_fill, world.first_fill + 2 * H4, H4))
    heads_path(world.journal).write_text(shadow.HEADS_HEADER, encoding="utf-8")
    world.clock.now = world.first_fill + 2 * H4 + 30
    with pytest.raises(ShadowRefusedError, match="behind the log"):
        world.run()


def test_a_changed_snapshot_fingerprint_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.run()
    monkeypatch.setattr(shadow, "snapshot_fingerprint", lambda: "f" * 64)
    world.clock.now = world.first_fill + 30
    with pytest.raises(ShadowRefusedError, match="snapshot fingerprint changed"):
        world.run()
    assert len(world.rows()) == 1


# --------------------------------------------------------------------------- model


def test_judged_rows_log_the_served_model_and_the_requested_one(world: World) -> None:
    world.run()
    walk(world, range(world.first_fill, world.a.decision_end, H4))
    rows = world.rows()
    judged = [d for d in decisions(rows) if d["status"] == "judged"]
    assert judged
    assert all(d["model"] == "jev-1.13.0" and d["requested_model"] == "jev-latest" for d in judged)


def test_no_served_model_is_logged_null_with_a_loud_warning(world: World) -> None:
    """Jev names no model: the row's served ``model`` is null (never the requested alias),
    the requested id is kept apart, every such bar warns, and the report excludes the
    decisions from both arms (counted as no_served_model)."""

    def no_model(request: httpx.Request) -> httpx.Response:
        body = json.loads(take_first(request).content)
        body["model"] = ""
        return httpx.Response(200, json=body)

    world.handler[0] = no_model
    world.run(client=world.client(model="jev-pinned-test"))
    warned: list[str] = []
    for f in range(world.first_fill, world.a.decision_end, H4):
        world.clock.now = f + 30
        summary = world.run(client=world.client(model="jev-pinned-test"))
        warned += [w for w in summary.warnings if "served no model id" in w]
    rows = world.rows()
    assert rows[0]["model"] == "jev-pinned-test"
    judged = [d for d in decisions(rows) if d["status"] == "judged"]
    assert judged and all(d["model"] is None for d in judged)
    assert all(d["requested_model"] == "jev-pinned-test" for d in judged)
    assert len(warned) == len({d["fill_open"] for d in judged})
    assert all(w.startswith("WARNING:") for w in warned)
    assert json.loads(world.requests[0].content)["model"] == "jev-pinned-test"
    assert check_deadlines(rows, GRACE) == []
    rep = build_report(rows, {CAND_A: [], CAND_B: []}, asof=world.clock.now)
    assert rep.model is None and rep.no_served_model == len(judged)


# --------------------------------------------------------------------------- windows


def test_signal_uses_compares_window_and_the_snapshot_a_longer_one(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.run()
    warm = world.a.data.warm_from
    gap = pd.to_datetime([warm + k * H4 for k in range(20, 25)], unit="s", utc=True)
    for sym, frame in list(world.market.frames.items()):
        world.market.frames[sym] = frame.drop(gap)  # a 5-bar gap inside the warm-up
    t = world.first_fill - H4
    frame = world.market.frames["BTCUSDT"]
    in_warmup = frame.loc[(frame.index >= pd.Timestamp(warm, unit="s", tz="UTC"))]
    in_warmup = in_warmup.loc[in_warmup.index <= pd.Timestamp(t, unit="s", tz="UTC")]
    assert len(in_warmup) < MIN_HISTORY_BARS  # compare's window alone is too short now

    plan_starts: list[int] = []
    snap_starts: list[tuple[int, int, int, int]] = []
    real_compile, real_snapshot = shadow.compile_plan, shadow.blind_snapshot

    def record_compile(plan: Any, hist: pd.DataFrame) -> Any:
        plan_starts.append(int(hist.index[0].timestamp()))
        real_compile(plan, hist)  # the real one must cope with the gap too
        return SimpleNamespace(entries=pd.Series(True, index=hist.index))  # force a signal

    def record_snapshot(candles: pd.DataFrame, btc: pd.DataFrame, at: pd.Timestamp) -> Any:
        snap_starts.append(
            (
                int(candles.index[0].timestamp()),
                int(btc.index[0].timestamp()),
                int(candles.index[-1].timestamp()),
                int(btc.index[-1].timestamp()),
            )
        )
        return real_snapshot(candles, btc, at)

    monkeypatch.setattr(shadow, "compile_plan", record_compile)
    monkeypatch.setattr(shadow, "blind_snapshot", record_snapshot)
    world.clock.now = world.first_fill + 30
    summary = world.run()
    assert [b.status for b in summary.bars] == ["judged"] and summary.pending == []
    genesis = world.rows()[0]
    spec = next(c for c in genesis["candidates"] if c["candidate"] == CAND_A)
    assert plan_starts == [warm]  # exactly compare's warm-up start
    assert spec["snapshot_since"] == warm - SNAPSHOT_EXTRA_BARS * H4
    assert snap_starts == [(spec["snapshot_since"], genesis["btc_since"], t, t)]
    (d,) = decisions(world.rows())
    assert d["status"] == "judged" and d["candidate"] == CAND_A


# --------------------------------------------------------------------------- look-ahead


@dataclass
class LeakyMarket(Market):
    """A broken source: ignores ``end`` and the clock, returns the forming bar and every later
    bar, and scales the prices and volume of every row at or after the forming bar."""

    max_seen: list[int] = field(default_factory=list)

    def loader_for(self, spec: Mapping[str, Any]) -> Callable[[str, str, pd.Timestamp], Any]:
        since = pd.Timestamp(int(spec["since"]), unit="s", tz="UTC")

        def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
            frame = self.frames[symbol].copy()
            forming = pd.Timestamp(math.floor(self.clock() / H4) * H4, unit="s", tz="UTC")
            later = frame.index >= forming
            frame.loc[later, ["open", "high", "low", "close"]] *= 1.7
            frame.loc[later, "volume"] *= 3.0
            out = frame.loc[frame.index >= since]
            self.max_seen.append(int(out.index[-1].timestamp()) - int(forming.timestamp()))
            return out

        return load


def test_future_and_forming_bars_change_nothing_logged_or_sent(tmp_path: Path) -> None:
    clean = make_world(tmp_path / "clean")
    leaky = make_world(tmp_path / "leaky")
    leaky.market = LeakyMarket(leaky.market.frames, leaky.clock)
    for w in (clean, leaky):
        w.run()
        walk(w, range(w.first_fill, w.b.decision_end + H4, H4))
    assert leaky.market.max_seen and min(leaky.market.max_seen) > 0  # it did leak

    def logged(w: World) -> tuple[Any, Any]:
        rows = w.rows()
        bar_view = [(b["fill_open"], b["status"], b["signals"]) for b in bars(rows).values()]
        dec_view = [
            (
                d["candidate"],
                d["fill_open"],
                d["status"],
                d["snapshot"],
                d["snapshot_sha256"],
                d["verdict"],
            )
            for d in decisions(rows)
        ]
        return bar_view, dec_view

    assert logged(leaky) == logged(clean)
    assert any(b[2] for b in logged(clean)[0]), "the fixture must have signals"
    assert clean.requests and [r.content for r in leaky.requests] == [
        r.content for r in clean.requests
    ]
