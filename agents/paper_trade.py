"""Paper-trade a research candidate with freqtrade dry-run, and compare it with its backtest.

Usage (from ``agents/``)::

    uv run python paper_trade.py prepare --plan-file plan.json --candidate NAME \
        [--claimed-sharpe 1.2] [--ledger PATH] [--candle-source api|archive] \
        [--exchange binance] [--public-api-host data-api.binance.vision] [--launch]
    uv run python paper_trade.py compare --candidate NAME [--end ...]
    uv run python paper_trade.py shadow [--once] [--grace-minutes 15] [--settle-seconds 20] \
        [--model jev-latest]
    uv run python paper_trade.py shadow-report [--asof ISO] [--write] [--expect-head SHA256]

``prepare`` requires the plan in the lab ledger (a research-run design-window trial of its
``plan_hash``; the ledger is opened strictly read-only). The lab's behaviour family (same logic,
or design-window returns correlated >= the family threshold, e.g. a no-op-clause twin) is the
hypothesis: a family whose locked-split check did not pass is refused, and so is a family member
of an already registered candidate. Whether that check passed or was never run is frozen into
the protocol (``locked_split_check``); without a passed check a paper pass is never a success
candidate. It takes the claimed Sharpe from the ledger's walk-forward
OOS evidence (or ``--claimed-sharpe``, recorded as owner-supplied; never the design-window
Sharpe), backtests the plan on the lab's design window (nothing is ledgered), and freezes the
pre-registered protocol (see :mod:`research.paper.protocol`) into
``agents/data/paper/<NAME>/protocol.json`` and the journal entry
``agents/journal/<date>-<symbol>-paper-<NAME>.md``, both created exclusively and BEFORE any
freqtrade file is written or the bot can trade. Then it writes ``agents/data/paper/<NAME>/`` and
prints the ``freqtrade trade --dry-run`` command (``--launch`` starts it). It refuses when the
dry-run DB already holds trades, or when the same plan or logic (or the same journal name) is
registered under another candidate name in the paper folder or the journal.

``compare`` checks ``protocol.json`` against the sha256 in the journal entry (a mismatch refuses:
the protocol changed after registration), loads candles from the FROZEN source with the frozen
warm-up, replays the plan with the lab engine over the same candles (frozen cost model),
evaluates the dry-run bot against the protocol (pass / fail / continue / inconclusive), appends
a dated verdict line to the journal entry and only then writes ``comparison-<date>.json`` +
``.md``. The first final verdict is written once; later runs print it and change nothing, except
that a comparison file named by the final verdict's journal line but missing (the write failed
after the verdict was recorded) is recreated, never touching the verdict or the journal. Errors
(network, rate limit, a locked dry-run DB, bad files) exit 2 with a "cannot compare" message.

``shadow`` runs the Jev shadow judge (PLAN.md Phase 17; :mod:`research.paper.shadow`): it
watches the registered paper candidates and never steers them. The first run writes the
pre-registration journal entry ``agents/journal/<date>-multi-jev-shadow.md``, the genesis row
of ``agents/data/paper/jev-shadow/decisions.jsonl`` and the append-only heads file
``agents/journal/jev-shadow-heads.md`` (one chain head per write; commit it with every push);
then, after each bar close, it recomputes every candidate's entry signal with the lab's
``compile_plan`` and asks Jev (``--model``, frozen on the first run; ``TYPESAFE_API_KEY`` from
the environment; without it every signal is logged ``unavailable``) about the signalled setups
before the fill bar's open + grace, else logs them ``missed``. Each decision row logs the
model id Jev served (null when it names none, with a loud warning) and the requested one. A run
that finds the log's last write without a head line (a crash between the two) re-anchors it
with a head line marked ``recovered``, only if every recorded head verifies. ``shadow-report``
verifies the hash chain, every recorded head, that the last head covers the log's last row, that
the committed heads file (``git show HEAD:``) is a prefix of the working one (a warning when git
or the committed copy is unavailable), the deadlines, the pre-registration and the frozen
snapshot and question hashes (any failure exits 2), and prints the pre-registered take-vs-skip
analysis from the JSONL and the dry-run DBs (``--write`` saves ``jev-shadow/interim-<asof>.md``
before the frozen evaluation time and ``jev-shadow/report-<evaluate_at>.md`` at or after it,
once). A final report exits 2 while any candidate's dry-run DB cannot be read (retry later).

Paper trading only: the config is always ``dry_run: true`` with no exchange keys. Results are
historical, not forward-looking.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from lab.config import load_config
from lab.data.derivatives_columns import DataUnavailableError
from lab.plan import parse_plan, plan_hash
from lab.plan.validator import PlanValidationError
from lab.research.contract import JOURNAL_DIR, PAPER_DIR, PaperComparison
from lab.runner import default_loader
from lab.types import Plan, interval_seconds
from research.paper.compare import (
    backtest_same_period,
    compare,
    comparison_path,
    frozen_costs,
    modelled_slippage,
    observe_paper,
    public_kline_loader,
    read_dryrun_trades,
    render_verdict_markdown,
    write_comparison,
    write_missing_comparison,
)
from research.paper.freqtrade import (
    PreparedPaper,
    check_candidate_name,
    class_name_for,
    launch,
    prepare_candidate,
    render_config,
    strategy_frozen_value,
    symbol_to_pair,
)
from research.paper.protocol import (
    DEFAULT_CANDLE_HOST,
    DEFAULT_PROTOCOL_CONFIG,
    FinalVerdictRecordedError,
    ObservedPaper,
    PaperProtocol,
    ProtocolChangedError,
    append_verdict,
    build_protocol,
    check_not_registered_elsewhere,
    complete_registration,
    design_stats,
    evaluate,
    load_protocol_config,
    load_registered_protocol,
    protocol_path,
    protocol_sha256,
    read_ledger_link,
    recorded_final_comparison,
    recorded_final_verdict,
    register_protocol,
    resolve_claim,
)
from research.paper.protocol import Verdict as ProtocolVerdict  # vs compare's replay verdict
from research.paper.shadow_log import DEFAULT_GRACE_S

DEFAULT_PUBLIC_HOST = DEFAULT_CANDLE_HOST


def _floor(ts: pd.Timestamp, interval: str) -> pd.Timestamp:
    step = interval_seconds(interval)
    return pd.Timestamp(int(ts.timestamp()) // step * step, unit="s", tz="UTC")


def _utc(text: str) -> pd.Timestamp:
    t = pd.Timestamp(text)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def journal_dir(args: argparse.Namespace) -> Path:
    """``--journal-dir`` if given; else the committed journal for the default paper dir, and
    ``<paper-dir>/journal`` for any other paper dir (tests and scratch runs never write into
    the real journal)."""
    if args.journal_dir is not None:
        return Path(args.journal_dir)
    if Path(args.paper_dir).resolve() == Path(PAPER_DIR).resolve():
        return JOURNAL_DIR
    return Path(args.paper_dir) / "journal"


def dryrun_trade_count(db_path: Path) -> int:
    """Rows in the dry-run DB's ``trades`` table (0 if the file or table does not exist)."""
    if not db_path.is_file():
        return 0
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'trades'"
        ).fetchone()
        if not found:
            return 0
        return int(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
    finally:
        conn.close()


def _db_path(args: argparse.Namespace) -> Path:
    folder = Path(args.paper_dir) / args.candidate
    meta_path = folder / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    return Path(meta.get("db_path") or folder / "dryrun.sqlite")


def _print_protocol(protocol: PaperProtocol) -> None:
    print(f"protocol {protocol_sha256(protocol)[:12]} ({protocol.mode}):")
    for i, rule in enumerate(protocol.rules, 1):
        print(f"  {i}. {rule}")


def _freqtrade_kwargs(args: argparse.Namespace) -> dict[str, object]:
    return {
        "exchange": args.exchange,
        "public_api_host": args.public_api_host,
        "stake_amount": args.stake_amount,
        "dry_run_wallet": args.wallet,
    }


def _check_freqtrade_files(args: argparse.Namespace, plan: Plan) -> None:
    """Everything ``prepare_candidate`` would refuse, checked BEFORE the protocol is registered
    and without writing: the config renders (pair, stake, host) and the folder does not hold
    another plan."""
    folder = Path(args.paper_dir) / args.candidate
    symbol_to_pair(plan.symbol)
    render_config(
        plan=plan,
        db_path=folder / "dryrun.sqlite",
        exchange=args.exchange,
        public_api_host=args.public_api_host,
        stake_amount=args.stake_amount,
        dry_run_wallet=args.wallet,
    )
    meta_path = folder / "meta.json"
    if meta_path.is_file():
        old = json.loads(meta_path.read_text(encoding="utf-8")).get("plan_hash")
        if old not in (None, plan_hash(plan)):
            raise ValueError(
                f"{folder} already holds plan {str(old)[:12]}; use a new candidate name"
            )


def cmd_prepare(args: argparse.Namespace) -> int:
    try:
        raw = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read plan file {args.plan_file}: {exc}", file=sys.stderr)
        return 2
    plan_raw = raw.get("plan", raw) if isinstance(raw, dict) else raw  # seed files wrap the plan
    try:
        check_candidate_name(args.candidate)
        plan = parse_plan(plan_raw)
    except (PlanValidationError, ValueError) as exc:
        print(f"cannot prepare candidate: {exc}", file=sys.stderr)
        return 2

    if protocol_path(args.paper_dir, args.candidate).exists():
        return _reprepare(args, plan)

    # 1. a new registration: one forward test per hypothesis, and never after a trade
    try:
        check_not_registered_elsewhere(
            args.paper_dir, args.candidate, plan, journal_dir=journal_dir(args)
        )
        _check_freqtrade_files(args, plan)
        db_path = _db_path(args)
        n_trades = dryrun_trade_count(db_path)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"refusing to prepare: {exc}", file=sys.stderr)
        return 2
    if n_trades:
        print(
            f"{db_path} already holds {n_trades} trades: the protocol must be registered before "
            "the first dry-run trade; use a new candidate name",
            file=sys.stderr,
        )
        return 2

    # 2. the ledger link and the claim (strictly read only), then freeze the protocol from the
    #    design-window backtest (nothing is ledgered)
    try:
        config = load_protocol_config(args.protocol_config)
        cfg = load_config()
        loader = default_loader(cfg)
        # the lab's behaviour family decides what is the same hypothesis (a no-op twin too)
        link = read_ledger_link(args.ledger or cfg.ledger_path, plan, cfg=cfg, loader=loader)
        check_not_registered_elsewhere(
            args.paper_dir, args.candidate, plan, journal_dir=journal_dir(args), family=link
        )
        claimed, claim_source, claim_note = resolve_claim(link, args.claimed_sharpe)
        design = design_stats(plan, cfg, loader)
        protocol = build_protocol(
            candidate=args.candidate,
            plan=plan,
            design=design,
            costs=cfg.costs,
            config=config,
            created_at=datetime.now(UTC),
            link=link,
            claimed_sharpe_annual=claimed,
            claim_source=claim_source,
            claim_note=claim_note,
            candle_source=args.candle_source,
            candle_host=DEFAULT_CANDLE_HOST,
        )
    except (OSError, ValueError, sqlite3.Error, DataUnavailableError) as exc:
        # DataUnavailableError: candles (or derivatives) for the design window are not on disk
        print(f"cannot build the paper-trade protocol: {exc}", file=sys.stderr)
        return 2

    # 3. register protocol.json + the journal entry exclusively, THEN the freqtrade files
    try:
        ppath, jpath = register_protocol(
            protocol, paper_dir=args.paper_dir, journal_dir=journal_dir(args)
        )
    except (FileExistsError, ValueError) as exc:
        print(f"cannot register the protocol: {exc}", file=sys.stderr)
        return 2
    print(f"protocol registered: {ppath}")
    print(f"journal entry: {jpath}")
    _print_protocol(protocol)
    return _write_files(args, plan, protocol)


def _write_files(args: argparse.Namespace, plan: Plan, protocol: PaperProtocol) -> int:
    """The freqtrade files, rendered AFTER the protocol is registered: the strategy carries the
    protocol's frozen ``data.warm_from`` (WARM_FROM) and ``start`` (START), so the bot computes
    its indicators on the same history as the lab replay in ``compare`` and enters only from
    the replay's first signal bar."""
    try:
        prepared = prepare_candidate(
            plan,
            candidate=args.candidate,
            paper_dir=args.paper_dir,
            warm_from=protocol.data.warm_from,
            start=protocol.start,
            **_freqtrade_kwargs(args),
        )
    except (PlanValidationError, ValueError, OSError) as exc:
        print(
            f"the protocol is registered but the freqtrade files could not be written: {exc}; "
            f"fix the cause and re-run `prepare --candidate {args.candidate}` with the same plan "
            "(the registered protocol is kept unchanged)",
            file=sys.stderr,
        )
        return 2
    print(f"prepared {prepared.dir}")
    return _finish(args, prepared)


def _reprepare(args: argparse.Namespace, plan: Plan) -> int:
    """A candidate whose protocol is already registered: the protocol is kept byte for byte
    (never rebuilt or overwritten), an interrupted registration (protocol.json without its
    journal entry, no trades yet) is completed, the freqtrade files are refreshed and
    ``--launch`` may restart the bot. Refused when the plan, ``--claimed-sharpe`` or
    ``--candle-source`` differs (new candidate name)."""
    try:
        written = None
        if dryrun_trade_count(_db_path(args)) == 0:
            written = complete_registration(args.paper_dir, args.candidate, journal_dir(args))
        protocol, jpath = load_registered_protocol(
            args.paper_dir, args.candidate, journal_dir(args)
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        # OSError: protocol/journal missing or exists; ValueError: a changed protocol, a corrupt
        # meta.json (JSONDecodeError) or unreadable journal frontmatter; sqlite3.Error: the
        # dry-run DB is locked or corrupt
        print(
            f"refusing to prepare: {exc}. If the dry-run bot already traded, the registration "
            f"cannot be completed: move {protocol_path(args.paper_dir, args.candidate).parent} "
            "aside and register under a new candidate name",
            file=sys.stderr,
        )
        return 2
    if written is not None:
        print(f"completed an interrupted registration: wrote {written}")
    changes: list[str] = []
    if plan_hash(plan) != protocol.plan_hash:
        changes.append("plan")
    if args.claimed_sharpe is not None and not (
        protocol.sharpe.claim_source == "owner_supplied"
        and protocol.sharpe.claimed_annual == args.claimed_sharpe
    ):
        changes.append("--claimed-sharpe")
    if args.candle_source != protocol.data.candle_source:
        changes.append("--candle-source")
    if changes:
        print(
            f"{protocol_path(args.paper_dir, args.candidate)} already registers this candidate; "
            f"{', '.join(changes)} would change it, and a registered protocol is never "
            "overwritten: use a new candidate name",
            file=sys.stderr,
        )
        return 2
    print(f"protocol already registered (kept unchanged): {jpath}")
    _print_protocol(protocol)
    return _write_files(args, plan, protocol)


def _finish(args: argparse.Namespace, prepared: PreparedPaper) -> int:
    print("dry-run command:")
    print("  " + " ".join(prepared.command))
    print("Start the bot now: the protocol clock runs from the next bar after registration.")
    if args.public_api_host:
        print(
            "NOTE: the public-API host override works around api.binance.com returning HTTP 451 "
            "(verified with freqtrade 2026.9); check the bot log shows candles arriving."
        )
    if args.launch:
        result = launch(prepared)
        print(result.message)
        return 0 if result.started else 1
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    try:
        return _compare(args)
    except FinalVerdictRecordedError as exc:
        print(f"final verdict already recorded: {exc}")
        return 0
    except (ValueError, OSError, httpx.HTTPError, sqlite3.Error, DataUnavailableError) as exc:
        # httpx.HTTPError: a network drop or a 418/429/451/5xx from the public klines host;
        # sqlite3.Error: e.g. "database is locked" while freqtrade writes the dry-run DB;
        # DataUnavailableError: the frozen archive source has no candles for the period
        print(f"cannot compare: {exc}", file=sys.stderr)
        return 2


def _compare(args: argparse.Namespace) -> int:
    check_candidate_name(args.candidate)
    folder = Path(args.paper_dir) / args.candidate
    plan_path = folder / "plan.json"
    if not plan_path.is_file():
        print(f"{plan_path} not found; run `prepare` first", file=sys.stderr)
        return 2
    try:
        protocol, jpath = load_registered_protocol(
            args.paper_dir, args.candidate, journal_dir(args)
        )
    except FileNotFoundError as exc:
        print(f"{exc} (no registered protocol to judge against)", file=sys.stderr)
        return 2
    except ProtocolChangedError as exc:
        print(f"refusing to compare: {exc}", file=sys.stderr)
        return 2
    recorded = recorded_final_verdict(jpath)
    missing: list[Path] = []
    if recorded is not None:
        print(
            f"final verdict already recorded: {recorded['outcome']} (data to "
            f"{recorded['verdict_asof']}, protocol sha256 "
            f"{str(recorded['verdict_protocol_sha256'])[:12]}); it is never changed"
        )
        print(f"journal: {jpath}")
        name = recorded_final_comparison(jpath)
        if name is not None:
            named = folder / name
            missing = [p for p in (named, named.with_suffix(".md")) if not p.exists()]
        if not missing:
            return 0
        print(
            f"the comparison the final verdict names is missing "
            f"({', '.join(p.name for p in missing)}): recreating only that, from the frozen "
            "protocol; the verdict and the journal are not changed"
        )
    data = protocol.data
    if args.source is not None and args.source != data.candle_source:
        print(
            f"refusing to compare: the protocol froze the candle source {data.candle_source!r}; "
            f"--source {args.source} would judge the bot on other data",
            file=sys.stderr,
        )
        return 2
    if args.public_api_host is not None and args.public_api_host != data.candle_host:
        print(
            f"refusing to compare: the protocol froze the candle host {data.candle_host!r}",
            file=sys.stderr,
        )
        return 2
    problem = _strategy_frozen_problem(
        folder, args.candidate, {"WARM_FROM": data.warm_from, "START": protocol.start}
    )
    if problem is not None:
        print(f"refusing to compare: {problem}", file=sys.stderr)
        return 2
    plan = parse_plan(json.loads(plan_path.read_text(encoding="utf-8")))
    if plan_hash(plan) != protocol.plan_hash:
        print(
            f"refusing to compare: plan.json ({plan_hash(plan)[:12]}) is not the registered "
            f"plan ({protocol.plan_hash[:12]})",
            file=sys.stderr,
        )
        return 2
    now = _floor(pd.Timestamp(datetime.now(UTC)), plan.interval)
    if recorded is not None:
        return _rewrite_comparison(args, protocol, plan, jpath, recorded, now, missing)
    requested = min(_floor(_utc(args.end), plan.interval), now) if args.end else now
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    end = min(requested, pd.Timestamp(protocol.decision_end, unit="s", tz="UTC"))
    if end <= start:
        print(f"the protocol starts at {start}; nothing to judge yet (verdict: continue)")
        return 0
    result, observed, verdict = _judge(args, protocol, plan, requested)
    path = comparison_path(result, args.paper_dir)
    # the journal line first (under its lock, write-once for a final verdict), then the files
    line = append_verdict(jpath, verdict, asof=observed.asof, comparison=path.name)
    print(f"protocol verdict: {verdict.verdict}")
    print(f"  {verdict.headline}")
    for alert in verdict.alerts:
        print(f"  alert: {alert}")
    print(f"journal: {jpath}")
    print(f"  {line}")
    try:
        write_comparison(result, args.paper_dir, verdict=verdict)
    except (OSError, ValueError) as exc:
        again = (
            "re-run `compare` to write the missing comparison file (the recorded verdict is kept)"
            if verdict.final
            else "the next `compare` writes a new one"
        )
        print(f"verdict recorded; comparison not written: {exc}; {again}", file=sys.stderr)
        return 2
    print(f"replay check: {result['verdict']}")
    print(f"written: {path} (+ .md)")
    if args.verbose:
        print("\n".join(render_verdict_markdown(verdict)))
    print("Results are historical, not forward-looking.")
    return 0


def _strategy_frozen_problem(folder: Path, candidate: str, expected: dict[str, int]) -> str | None:
    """Why the dry-run strategy cannot be judged against the protocol's replay, or None: its
    file must exist and render each frozen value (``WARM_FROM``: the protocol's
    ``data.warm_from``, the first bar indicators are computed from; ``START``: its ``start``,
    the first bar an entry signal counts on) exactly once, equal to the protocol's."""
    path = folder / f"{class_name_for(candidate)}.py"
    again = f"re-run `prepare --candidate {candidate}` with the same plan (the protocol is kept)"
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"{path} not found; {again}"
    for name, want in expected.items():
        try:
            found = strategy_frozen_value(source, name)
        except ValueError as exc:
            return f"{path}: {exc}; {again}"
        if found != want:
            return (
                f"{path} has {name} {found}, the protocol's replay uses {want}: the bot would "
                f"not trade the backtest's history; {again}"
            )
    return None


def _judge(
    args: argparse.Namespace, protocol: PaperProtocol, plan: Plan, requested: pd.Timestamp
) -> tuple[PaperComparison, ObservedPaper, ProtocolVerdict]:
    """Load the frozen candles, replay the plan with the frozen costs, and judge the dry-run
    bot against the protocol as of ``requested`` (nothing is written)."""
    data = protocol.data
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    end = min(requested, pd.Timestamp(protocol.decision_end, unit="s", tz="UTC"))
    forward = read_dryrun_trades(_db_path(args), pair=symbol_to_pair(plan.symbol))

    cfg = load_config()
    warm_from = pd.Timestamp(data.warm_from, unit="s", tz="UTC")  # the strategy's WARM_FROM
    if data.candle_source == "archive":
        base_loader = default_loader(cfg)
    else:
        base_loader = public_kline_loader(since=warm_from, host=data.candle_host)
    history = base_loader(plan.symbol, plan.interval, end)
    # the frozen warm-up: exactly the bars from start - warmup_bars, whatever the source holds
    history = history.loc[(history.index >= warm_from) & (history.index < end)]
    candles = history.loc[history.index >= start]

    # the lab replay on the same candles with the frozen cost model, never today's lab.toml:
    # the timing match and the shortfall's modelled fill both come from it
    costs = frozen_costs(protocol)
    backtest = backtest_same_period(
        plan, start=start, end=end, cfg=cfg, loader=lambda *a: history, costs=costs
    )
    result = compare(
        forward,
        backtest,
        interval=plan.interval,
        candles=candles,
        candidate=args.candidate,
        plan_hash=plan_hash(plan),
        symbol=plan.symbol,
        start=start,
        end=end,
        now=datetime.now(UTC),
    )
    slippage = modelled_slippage(history, costs)
    observed = observe_paper(
        protocol, forward, history, lab_trades=backtest, slippage=slippage, asof=requested
    )
    return result, observed, evaluate(protocol, observed)


def _rewrite_comparison(
    args: argparse.Namespace,
    protocol: PaperProtocol,
    plan: Plan,
    jpath: Path,
    recorded: Mapping[str, Any],
    now: pd.Timestamp,
    missing: list[Path],
) -> int:
    """Recreate ONLY the missing comparison file(s) the final verdict's journal line names.

    The verdict is judged again as of the recorded ``verdict_asof`` (an early final verdict) or
    now (a verdict at the decision bar, judged after it), and the files are written only when
    the recomputation reproduces the recorded outcome, data end and protocol sha256; a file
    that would contradict the journal is never written. The journal is not touched."""
    asof = _utc(str(recorded["verdict_asof"]))
    decision = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    requested = asof if asof < decision else max(now, asof)
    result, observed, verdict = _judge(args, protocol, plan, requested)
    same = (
        verdict.verdict == recorded["outcome"]
        and observed.asof == int(asof.timestamp())
        and verdict.protocol_sha256 == recorded["verdict_protocol_sha256"]
    )
    if not same:
        print(
            f"cannot recreate the comparison: judged again it gives {verdict.verdict!r} with "
            f"data to {pd.Timestamp(observed.asof, unit='s', tz='UTC')}, but the journal "
            f"records {recorded['outcome']!r} with data to {recorded['verdict_asof']} (the "
            "candles or the dry-run DB changed since); nothing written",
            file=sys.stderr,
        )
        return 2
    result["notes"].append(
        f"Recreated {result['generated_at']}: the original write failed after the final "
        f"verdict was recorded in {jpath.name}; judged again from the frozen protocol with "
        "data to the recorded verdict's date. The journal's verdict is the record."
    )
    written = write_missing_comparison(
        result, args.paper_dir, name=missing[0].with_suffix(".json").name, verdict=verdict
    )
    for path in written:
        print(f"written: {path}")
    print("Results are historical, not forward-looking.")
    return 0


def cmd_shadow(args: argparse.Namespace) -> int:
    """The Jev shadow judge (watches, never steers); see :mod:`research.paper.shadow`."""
    from research.paper import shadow

    grace_s = round(args.grace_minutes * 60)
    return shadow.cli_shadow(
        Path(args.paper_dir),
        journal_dir(args),
        once=args.once,
        grace_s=grace_s,
        settle_s=args.settle_seconds,
        model=args.model,
        out=print,
        err=lambda text: print(text, file=sys.stderr),
    )


def cmd_shadow_report(args: argparse.Namespace) -> int:
    """The pre-registered take-vs-skip report from the JSONL and the dry-run DBs alone."""
    from research.paper import shadow

    return shadow.cli_shadow_report(
        Path(args.paper_dir),
        journal_dir(args),
        asof=args.asof,
        write=args.write,
        expect_head=args.expect_head,
        out=print,
        err=lambda text: print(text, file=sys.stderr),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--paper-dir", type=Path, default=PAPER_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--journal-dir", type=Path, default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="register the protocol, write the dry-run strategy+config")
    p.add_argument("--plan-file", required=True, help="plan JSON (or a seed file)")
    p.add_argument("--candidate", required=True, help="folder name under agents/data/paper/")
    p.add_argument("--exchange", default="binance")
    p.add_argument("--public-api-host", default=None, help="e.g. data-api.binance.vision")
    p.add_argument("--stake-amount", type=float, default=100.0)
    p.add_argument("--wallet", type=float, default=1000.0, help="dry-run wallet (stake currency)")
    p.add_argument("--launch", action="store_true", help="start the freqtrade dry-run bot")
    p.add_argument(
        "--claimed-sharpe",
        type=float,
        default=None,
        help="annualised (365-day) Sharpe the candidate claims, recorded as owner-supplied; "
        "default: the minimum walk-forward OOS Sharpe of the plan in the ledger (refused "
        "without either; the design-window Sharpe is never the claim)",
    )
    p.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="lab ledger to read the plan's research design trial and walk-forward evidence "
        "from (default: the lab's ledger.db; opened strictly read-only)",
    )
    p.add_argument(
        "--candle-source",
        choices=("api", "archive"),
        default="api",
        help="candle source compare will use, frozen in the protocol (default: the public API)",
    )
    p.add_argument(
        "--protocol-config", type=Path, default=DEFAULT_PROTOCOL_CONFIG, help=argparse.SUPPRESS
    )
    p.set_defaults(func=cmd_prepare)

    c = sub.add_parser("compare", help="judge the dry-run bot against its frozen protocol")
    c.add_argument("--candidate", required=True)
    c.add_argument(
        "--end",
        default=None,
        help="UTC date/time to judge up to; default: the last bar boundary (the protocol fixes "
        "the start and the decision bar)",
    )
    c.add_argument(
        "--source",
        choices=("api", "archive"),
        default=None,
        help="must match the protocol's frozen candle source (default: the frozen one)",
    )
    c.add_argument(
        "--public-api-host", default=None, help="must match the protocol's frozen candle host"
    )
    c.add_argument("--verbose", action="store_true", help="print every check")
    c.set_defaults(func=cmd_compare)

    s = sub.add_parser(
        "shadow", help="Jev shadow judge: log a verdict per HC signal (watches, never steers)"
    )
    s.add_argument("--once", action="store_true", help="process the closed bars once and exit")
    s.add_argument(
        "--grace-minutes",
        type=float,
        default=DEFAULT_GRACE_S / 60,
        help="minutes after the fill bar's open a verdict may still be written (frozen into "
        "the genesis row on the first run; later runs must pass the same value)",
    )
    s.add_argument(
        "--settle-seconds",
        type=float,
        default=20.0,
        help="seconds to wait after a bar boundary before loading its closed candle",
    )
    s.add_argument(
        "--model",
        default="jev-latest",
        help="Jev model to request (default: jev-latest, the client default); frozen into the "
        "genesis row on the first run, later runs must pass the same value. The served model "
        "id is logged per decision and the first judged one is pinned for the analysis",
    )
    s.set_defaults(func=cmd_shadow)

    r = sub.add_parser("shadow-report", help="Jev take vs skip, from the JSONL and dry-run DBs")
    r.add_argument("--asof", default=None, help="UTC date/time of the report (default: now)")
    r.add_argument(
        "--write",
        action="store_true",
        help="write jev-shadow/interim-<asof>.md before the frozen evaluation time, else "
        "jev-shadow/report-<evaluate_at>.md (never overwrites)",
    )
    r.add_argument(
        "--expect-head",
        default=None,
        help="sha256 of the last row recorded elsewhere: an extra check beside the heads file",
    )
    r.set_defaults(func=cmd_shadow_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
