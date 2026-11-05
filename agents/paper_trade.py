"""Paper-trade a research candidate with freqtrade dry-run, and compare it with its backtest.

Usage (from ``agents/``)::

    uv run python paper_trade.py prepare --plan-file plan.json --candidate NAME \
        [--claimed-sharpe 1.2] [--ledger PATH] [--candle-source api|archive] \
        [--exchange binance] [--public-api-host data-api.binance.vision] [--launch] \
        [--risk-config PATH]
    uv run python paper_trade.py compare --candidate NAME [--end ...]
    uv run python paper_trade.py compare --portfolio [--candidates a,b] [--end ...]
    uv run python paper_trade.py guard --candidate NAME [--watch]
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

``prepare --risk-config PATH`` (PLAN.md Phase 20; template ``config/paper_risk.example.toml``)
freezes ``risk_controls`` and/or ``sizing`` into a new protocol, plus ``paper_config``: the stake,
wallet, ``max_open_trades``, exchange and public host ``config.json`` is rendered from, so the
sha256 covers them whichever block the test has. A registered candidate keeps all of it: another
size is a new test. A ``--stake-amount`` or ``--wallet`` against a frozen value is refused, on a
re-prepare too and whether or not ``config.json`` still exists, and ``compare``, ``guard`` and a
re-prepare refuse a ``config.json`` that differs from a fresh render in any key. A moved folder
gets its own message. A re-prepare renders ``plan.json``, the strategy, ``config.json`` and
``meta.json`` again and keeps the protocol; on a test with trades it appends a journal line for any
file that changed. With either block ``prepare`` refuses averaging down in what it writes and in
any strategy already in the folder. With sizing it checks the plan has a stop and refuses a step
up without its ``prior_test``'s proof: the very same plan, keeping the guards that plan passed
under (:func:`research.paper.risk.check_size_step`). Every ``prepare`` refuses while the folder
holds ``halt.lock``. Without the flag nothing changes.

``compare`` of a protocol with protections first runs ``freqtrade backtesting
--enable-protections`` (:mod:`research.paper.protected_backtest`), then replays the lab with
entries off on the bars its recorded locks block, so positions and later entries follow the real
path. Every journal line carries masked/total; above the frozen ``max_blocked_share`` the best
verdict at the decision bar is a final ``inconclusive``. With sizing it sizes the replay with the
same stake function. A fail is never demoted. A paper stake that differs stops nothing: it is named
in the verdict and the journal, a rule that fails is a ``fail``, and otherwise the decision bar
gives a final ``inconclusive``. Freqtrade missing or failing, or a backtest that did not cover the
period, leaves only the replay checks unknown, never an unprotected comparison: entries, exposure
or Sharpe failing is a ``fail`` at any time; otherwise it exits 2 until the decision bar plus the
frozen ``data_grace_days``, then a final ``inconclusive`` naming why. A strategy that is not the
fresh render of the protocol, the config above or a changed ``plan.json`` exits 2 until that same
moment, then a final ``inconclusive`` naming the void without judging (no comparison file). With a
circuit breaker it applies the thresholds itself, whether or not ``guard`` ran: a trip is a final
``fail`` with its own reason, on a void test too, and the protected backtest is then skipped.
Protections and sizing never run for a protocol without them.

``compare --portfolio`` prints N_eff on the coins' own daily returns (the figure to quote), the
same on the bots' returns, pairwise correlations and the days each pair was in the market together
(information only). By default it reads the largest cohort of tests with one start and names the
others as excluded. It writes nothing: no journal line, no comparison file, no verdict.

``guard`` (:mod:`research.paper.guard`) checks a test's frozen circuit breaker once, or after each
bar with ``--watch``. On a trip it writes ``halt.lock``, stops the verified dry-run process and
appends a halt line to the journal. Exit 0 clear, 1 halted, 2 refused or cannot check, candles
that stop before the last closed bar included. ``--watch`` retries network failures, a locked DB
and late candles (the newest bar a few times at once, then at the next bar), and stops with exit 2
on anything else, a ``config.json`` that is not the frozen one included, and on a 403 at twelve
checks in a row. A halted folder still gets its stop. The breaker acts only while it runs:
``--launch`` does not start it, and ``prepare`` prints the command. A protocol without a circuit
breaker is refused and nothing changes. Only the owner deletes ``halt.lock``, outside Claude Code.

Paper trading only: the config is always ``dry_run: true`` with no exchange keys. Results are
historical, not forward-looking.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from itertools import zip_longest
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from lab.config import load_config
from lab.data.derivatives_columns import DataUnavailableError
from lab.plan import parse_plan, plan_hash
from lab.plan.validator import PlanValidationError
from lab.research.contract import JOURNAL_DIR, PAPER_DIR, PaperComparison
from lab.research.logic import logic_hash
from lab.runner import default_loader
from lab.types import Plan, interval_seconds
from research.paper import guard as guard_module
from research.paper.compare import (
    SIZING_LINE_PREFIX,
    StakeMismatchError,
    backtest_same_period,
    compare,
    comparison_path,
    daily_coin_returns,
    daily_in_market,
    daily_paper_returns,
    frozen_costs,
    modelled_slippage,
    observe_paper,
    public_kline_loader,
    read_dryrun_trades,
    render_verdict_markdown,
    size_both_sides,
    sizing_line,
    write_comparison,
    write_missing_comparison,
)
from research.paper.freqtrade import (
    DB_URL_KEY,
    UNLIMITED_STAKE,
    PreparedPaper,
    check_candidate_name,
    check_paper_config,
    class_name_for,
    config_differences,
    fresh_config,
    frozen_settings,
    launch,
    prepare_candidate,
    render_config,
    render_strategy,
    sizing_max_stake,
    strategy_frozen_value,
    symbol_to_pair,
)
from research.paper.portfolio import portfolio_report, render_portfolio
from research.paper.protected_backtest import (
    BLOCKED_LINE_PREFIX,
    ProtectedBacktestError,
    blocked_note,
    blocked_unknown_note,
    has_protections,
    masked_replay,
    run_protected_backtest,
)
from research.paper.protocol import (
    DEFAULT_CANDLE_HOST,
    DEFAULT_PROTOCOL_CONFIG,
    RISK_PROTOCOL_VERSION,
    FinalVerdictRecordedError,
    ObservedPaper,
    PaperProtocol,
    ProtocolChangedError,
    append_rewrite,
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
    size_steps_from,
    void_verdict,
)
from research.paper.protocol import Verdict as ProtocolVerdict  # vs compare's replay verdict
from research.paper.risk import (
    HALT_LOCK,
    PaperConfig,
    PriorTest,
    RiskConfig,
    RiskControls,
    Sizing,
    check_no_averaging_down,
    check_not_halted,
    check_size_step,
    check_sizing_against_plan,
    frozen_wallet,
    load_risk_config,
)
from research.paper.shadow_log import DEFAULT_GRACE_S

DEFAULT_PUBLIC_HOST = DEFAULT_CANDLE_HOST
DEFAULT_STAKE_AMOUNT = 100.0
DEFAULT_WALLET = 1000.0
SHADOW_FOLDER = "jev-shadow"  # the shadow judge's own folder is not a paper test
ONE_LINE_CHARS = 300  # a reason on a journal line: enough to name it, freqtrade's stderr is longer


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


def _freqtrade_kwargs(
    args: argparse.Namespace,
    risk_controls: RiskControls | None = None,
    sizing: Sizing | None = None,
) -> dict[str, Any]:
    """Config settings from the flags. A frozen wallet is the default; a flag that contradicts it
    is refused first (:func:`_check_stake_args`)."""
    wallet = args.wallet if args.wallet is not None else frozen_wallet(risk_controls, sizing)
    return {
        "exchange": args.exchange,
        "public_api_host": args.public_api_host,
        "stake_amount": DEFAULT_STAKE_AMOUNT if args.stake_amount is None else args.stake_amount,
        "dry_run_wallet": DEFAULT_WALLET if wallet is None else wallet,
    }


def _check_freqtrade_files(args: argparse.Namespace, plan: Plan, risk: RiskConfig | None) -> None:
    """Everything ``prepare_candidate`` would refuse, checked BEFORE the protocol is registered
    and without writing: the config renders (pair, stake, host) and the folder does not hold
    another plan."""
    folder = Path(args.paper_dir) / args.candidate
    controls, sizing = (None, None) if risk is None else (risk.risk_controls, risk.sizing)
    symbol_to_pair(plan.symbol)
    render_config(
        plan=plan,
        db_path=folder / "dryrun.sqlite",
        sizing=sizing,
        **_freqtrade_kwargs(args, controls, sizing),
    )
    meta_path = folder / "meta.json"
    if meta_path.is_file():
        old = json.loads(meta_path.read_text(encoding="utf-8")).get("plan_hash")
        if old not in (None, plan_hash(plan)):
            raise ValueError(
                f"{folder} already holds plan {str(old)[:12]}; use a new candidate name"
            )


def _check_stake_args(
    args: argparse.Namespace, risk_controls: RiskControls | None, sizing: Sizing | None
) -> None:
    """``--stake-amount`` and ``--wallet`` may not contradict a frozen block."""
    if sizing is not None:
        largest = sizing_max_stake(sizing)
        if args.stake_amount is not None and args.stake_amount != largest:
            raise ValueError(
                f"--stake-amount {args.stake_amount:g} contradicts the sizing block: stakes come "
                f"from it, and its maximum stake is {largest:g}"
            )
        if args.wallet is not None and args.wallet != sizing.wallet:
            raise ValueError(
                f"--wallet {args.wallet:g} contradicts the sizing block's wallet {sizing.wallet:g}"
            )
    wallet = frozen_wallet(risk_controls, sizing)
    if wallet is not None and args.wallet is not None and args.wallet != wallet:
        raise ValueError(
            f"--wallet {args.wallet:g} contradicts the circuit breaker's frozen wallet {wallet:g}"
        )


def _frozen_config(args: argparse.Namespace, risk: RiskConfig | None) -> PaperConfig | None:
    """What a new registration freezes of ``config.json``; None without a block (version 7)."""
    if risk is None:
        return None
    settings = _freqtrade_kwargs(args, risk.risk_controls, risk.sizing)
    return PaperConfig(
        exchange=settings["exchange"],
        public_api_host=settings["public_api_host"],
        stake_amount=UNLIMITED_STAKE if risk.sizing else settings["stake_amount"],
        dry_run_wallet=settings["dry_run_wallet"],
        max_open_trades=risk.sizing.max_open_trades if risk.sizing else 1,
    )


def _config_problem(
    folder: Path, candidate: str, protocol: PaperProtocol, plan: Plan, *, moved_ok: bool = False
) -> str | None:
    """Why ``config.json`` is not what the frozen ``paper_config`` renders, or None. The whole file
    counts: ``fee``, ``dry_run_wallet`` and ``stake_amount`` all change what the protected backtest
    does. ``db_url`` is the one key that holds a path, so a moved folder is named as such rather
    than as an edit; ``moved_ok`` (a re-prepare, which rewrites that key) lets it pass. A later
    change to :func:`research.paper.freqtrade.render_config` also shows here, and refuses, like the
    strategy check. Never read for a version 7 protocol."""
    frozen = protocol.paper_config
    if frozen is None:
        return None
    path = folder / "config.json"
    fresh = fresh_config(
        plan, candidate=candidate, folder=folder, paper=frozen, sizing=protocol.sizing
    )
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as exc:
        return f"{path}: {exc}; {_again(candidate)}"
    differ = config_differences(found, fresh)
    edited = [key for key in differ if key != DB_URL_KEY]
    if edited:
        return (
            f"{path} differs from the frozen config on {edited}: a registered version "
            f"{protocol.version} test keeps its config; {_restore(candidate)}"
        )
    if differ and not moved_ok:
        return (
            f"{path} points its dry-run DB at {found.get(DB_URL_KEY)}, and this folder's is "
            f"{fresh[DB_URL_KEY]}: the folder was moved, or the file edited. Re-run `prepare "
            f"--candidate {candidate}` with the same plan: it renders plan.json, the strategy, "
            "config.json and meta.json again (the protocol is kept)"
        )
    return None


def _check_frozen_flags(args: argparse.Namespace, protocol: PaperProtocol, plan: Plan) -> None:
    """A re-prepare of a version 8 test may not change its config: a flag that differs from the
    frozen value is refused whether or not ``config.json`` is still there, so deleting the file
    frees nothing, and a file that differs is refused too. Nothing to do for version 7."""
    frozen = protocol.paper_config
    if frozen is None:
        return
    differ = [
        flag
        for flag, value, have in (
            ("--exchange", args.exchange, frozen.exchange),
            ("--public-api-host", args.public_api_host, frozen.public_api_host),
            (
                "--stake-amount",
                None if protocol.sizing else args.stake_amount,
                frozen.stake_amount,
            ),
            ("--wallet", args.wallet, frozen.dry_run_wallet),
        )
        if value is not None and value != have
    ]
    if differ:
        raise ValueError(
            f"{', '.join(differ)} would change the frozen config; a registered version "
            f"{protocol.version} test keeps its config: drop the flag (the same plan under a new "
            "candidate name is refused)"
        )
    folder = Path(args.paper_dir) / args.candidate
    if (folder / "config.json").is_file():
        problem = _config_problem(folder, args.candidate, protocol, plan, moved_ok=True)
        if problem is not None:
            raise ValueError(problem)


def _screen_averaging_down(
    args: argparse.Namespace,
    plan: Plan,
    risk_controls: RiskControls | None,
    sizing: Sizing | None,
) -> None:
    """The no averaging down rule, on what ``prepare`` is about to write and on any strategy
    already in the folder. Only with a block (version 8): a version 7 test is never screened."""
    if risk_controls is None and sizing is None:
        return
    folder = Path(args.paper_dir) / args.candidate
    class_name = class_name_for(args.candidate)
    source = render_strategy(
        plan, class_name=class_name, risk_controls=risk_controls, sizing=sizing
    )
    config = render_config(
        plan=plan,
        db_path=folder / "dryrun.sqlite",
        sizing=sizing,
        **_freqtrade_kwargs(args, risk_controls, sizing),
    )
    check_no_averaging_down(source, config)
    existing = folder / f"{class_name}.py"
    if existing.is_file():
        check_no_averaging_down(existing.read_text(encoding="utf-8"), config)


def _check_size_step(
    args: argparse.Namespace, plan: Plan, sizing: Sizing, risk_controls: RiskControls | None
) -> None:
    """A step up needs its ``prior_test``'s proof, read from what is registered on disk."""
    prior = None
    if sizing.prior_test is not None:
        name = sizing.prior_test
        found, jpath = load_registered_protocol(args.paper_dir, name, journal_dir(args))
        recorded = recorded_final_verdict(jpath)
        stepped = size_steps_from(args.paper_dir, journal_dir(args), name)
        prior = PriorTest(
            name=name,
            sizing=found.sizing,
            risk_controls=found.risk_controls,
            decision_days=found.decision_days,
            final_verdict=None if recorded is None else str(recorded["outcome"]),
            logic_hash=found.logic_hash,
            plan_hash=found.plan_hash,
            mode=found.mode,
            locked_split_check=found.locked_split_check,
            halted=(Path(args.paper_dir) / name / HALT_LOCK).exists(),
            unlocked=tuple(n for n in stepped if n != args.candidate),
        )
    check_size_step(
        sizing,
        prior,
        plan_hash=plan_hash(plan),
        logic_hash=logic_hash(plan),
        stop_loss_pct=float(plan.risk.stop_loss_pct or 0.0),
        risk_controls=risk_controls,
    )


def _check_new_risk(args: argparse.Namespace, plan: Plan, risk: RiskConfig | None) -> None:
    """Everything a new registration's ``--risk-config`` must satisfy (nothing is written)."""
    if risk is None:
        return
    if risk.sizing is not None:
        check_sizing_against_plan(risk.sizing, plan.risk.stop_loss_pct)
    _check_stake_args(args, risk.risk_controls, risk.sizing)
    if risk.sizing is not None:
        _check_size_step(args, plan, risk.sizing, risk.risk_controls)
    _screen_averaging_down(args, plan, risk.risk_controls, risk.sizing)


def cmd_prepare(args: argparse.Namespace) -> int:
    try:
        raw = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read plan file {args.plan_file}: {exc}", file=sys.stderr)
        return 2
    plan_raw = raw.get("plan", raw) if isinstance(raw, dict) else raw  # seed files wrap the plan
    try:
        check_candidate_name(args.candidate)
        check_not_halted(Path(args.paper_dir) / args.candidate)
        plan = parse_plan(plan_raw)
        risk = None if args.risk_config is None else load_risk_config(args.risk_config)
    except (PlanValidationError, ValueError) as exc:
        print(f"cannot prepare candidate: {exc}", file=sys.stderr)
        return 2

    if protocol_path(args.paper_dir, args.candidate).exists():
        return _reprepare(args, plan, risk)

    # 1. a new registration: one forward test per hypothesis, and never after a trade
    step_of = risk.sizing.prior_test if risk is not None and risk.sizing is not None else None
    try:
        _check_new_risk(args, plan, risk)  # proves the step before any write
        check_not_registered_elsewhere(
            args.paper_dir,
            args.candidate,
            plan,
            journal_dir=journal_dir(args),
            size_step_of=step_of,
        )
        _check_freqtrade_files(args, plan, risk)
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
            args.paper_dir,
            args.candidate,
            plan,
            journal_dir=journal_dir(args),
            family=link,
            size_step_of=step_of,
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
            risk_controls=None if risk is None else risk.risk_controls,
            sizing=None if risk is None else risk.sizing,
            paper_config=_frozen_config(args, risk),
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


def _write_files(
    args: argparse.Namespace,
    plan: Plan,
    protocol: PaperProtocol,
    *,
    after_write: Callable[[], None] = lambda: None,
) -> int:
    """The freqtrade files, rendered AFTER the protocol is registered: the strategy carries the
    protocol's frozen ``data.warm_from`` (WARM_FROM) and ``start`` (START), so the bot computes
    its indicators on the same history as the lab replay in ``compare`` and enters only from
    the replay's first signal bar. A frozen ``paper_config`` replaces the flags. ``after_write``
    runs once the writes stop, a partial one included, and before any launch."""
    frozen = protocol.paper_config
    settings = (
        _freqtrade_kwargs(args, protocol.risk_controls, protocol.sizing)
        if frozen is None
        else frozen_settings(frozen)
    )
    try:
        prepared = prepare_candidate(
            plan,
            candidate=args.candidate,
            paper_dir=args.paper_dir,
            warm_from=protocol.data.warm_from,
            start=protocol.start,
            risk_controls=protocol.risk_controls,
            sizing=protocol.sizing,
            **settings,
        )
    except (PlanValidationError, ValueError, OSError) as exc:
        print(
            f"the protocol is registered but the freqtrade files could not be written: {exc}; "
            f"fix the cause and re-run `prepare --candidate {args.candidate}` with the same plan "
            "(the registered protocol is kept unchanged)",
            file=sys.stderr,
        )
        return 2
    finally:
        after_write()
    print(f"prepared {prepared.dir}")
    return _finish(args, prepared, protocol)


def _read_or_none(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _journal_rewrites(jpath: Path, before: Mapping[Path, bytes | None], trades: int) -> None:
    """One journal line naming the files a re-prepare changed under a bot that has traded: compare
    reads the fresh files, so the record of what the bot ran on is here. It goes under its own
    heading, outside the protocol's sha256 and the verdict lines."""
    changed = [path.name for path, old in before.items() if _read_or_none(path) != old]
    if changed:
        append_rewrite(
            jpath,
            f"- {datetime.now(UTC).isoformat(timespec='seconds')}: re-prepare rewrote "
            f"{', '.join(changed)}, which differed from the render, with {trades} trades in the "
            "dry-run DB. Those trades may have run on the file as it was.",
        )


def _reprepare(args: argparse.Namespace, plan: Plan, risk: RiskConfig | None = None) -> int:
    """A candidate whose protocol is already registered: the protocol is kept byte for byte
    (never rebuilt or overwritten), an interrupted registration (protocol.json without its
    journal entry, no trades yet) is completed, the freqtrade files are refreshed and
    ``--launch`` may restart the bot. Refused when the plan, ``--claimed-sharpe``,
    ``--candle-source`` or ``--risk-config`` differs (new candidate name), or, for a version 8
    protocol, when a flag or the file would change its config (:func:`_check_frozen_flags`). A
    version 8 test with trades gets a journal line for each re-prepare that changes a file."""
    try:
        written = None
        trades = dryrun_trade_count(_db_path(args))
        if trades == 0:
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
    if risk is not None and (
        risk.risk_controls != protocol.risk_controls or risk.sizing != protocol.sizing
    ):
        changes.append("--risk-config")  # a different size is a new test
    if changes:
        print(
            f"{protocol_path(args.paper_dir, args.candidate)} already registers this candidate; "
            f"{', '.join(changes)} would change it, and a registered protocol is never "
            "overwritten: use a new candidate name",
            file=sys.stderr,
        )
        return 2
    try:
        _check_stake_args(args, protocol.risk_controls, protocol.sizing)
        _check_frozen_flags(args, protocol, plan)
        _screen_averaging_down(args, plan, protocol.risk_controls, protocol.sizing)
    except (OSError, ValueError) as exc:
        print(f"refusing to prepare: {exc}", file=sys.stderr)
        return 2
    print(f"protocol already registered (kept unchanged): {jpath}")
    _print_protocol(protocol)
    if protocol.version < RISK_PROTOCOL_VERSION or trades == 0:
        return _write_files(args, plan, protocol)
    folder = Path(args.paper_dir) / args.candidate
    names = ("plan.json", f"{class_name_for(args.candidate)}.py", "config.json", "meta.json")
    before = {folder / name: _read_or_none(folder / name) for name in names}
    return _write_files(
        args, plan, protocol, after_write=partial(_journal_rewrites, jpath, before, trades)
    )


def _has_breaker(protocol: PaperProtocol) -> bool:
    controls = protocol.risk_controls
    return controls is not None and controls.circuit_breaker is not None


def _finish(args: argparse.Namespace, prepared: PreparedPaper, protocol: PaperProtocol) -> int:
    print("dry-run command:")
    print("  " + " ".join(prepared.command))
    print("Start the bot now: the protocol clock runs from the next bar after registration.")
    if _has_breaker(protocol):
        paper = "" if Path(args.paper_dir) == Path(PAPER_DIR) else f"--paper-dir {args.paper_dir} "
        print("CIRCUIT BREAKER: it acts only while this runs, and --launch does not start it:")
        print(f"  uv run python paper_trade.py {paper}guard --candidate {args.candidate} --watch")
    if args.public_api_host:
        print(
            "NOTE: the public-API host override works around api.binance.com returning HTTP 451 "
            "(verified with freqtrade 2026.9); check the bot log shows candles arriving."
        )
    if args.launch:
        try:
            result = (
                launch(prepared, pid_file=prepared.dir / guard_module.BOT_PID)
                if _has_breaker(protocol)
                else launch(prepared)
            )
        except ValueError as exc:  # e.g. halt.lock appeared since the first check
            print(f"refusing to launch: {exc}", file=sys.stderr)
            return 2
        print(result.message)
        return 0 if result.started else 1
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    try:
        return _portfolio(args) if args.portfolio else _compare(args)
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
    plan, problem = _frozen_files(folder, args.candidate, protocol)
    if problem is not None or plan is None:
        if recorded is None and _voiding(folder, args.candidate, protocol, plan):
            grace_end = _grace_end(protocol)
            if _requested(args, protocol.interval) >= grace_end:
                return _record_void(args, protocol, jpath, str(problem))
            problem = (
                f"{problem}. Or leave it: from {grace_end} `compare` records a final inconclusive "
                "naming this void, and judges nothing"
            )
        print(f"refusing to compare: {problem}", file=sys.stderr)
        return 2
    now = _floor(pd.Timestamp(datetime.now(UTC)), plan.interval)
    if recorded is not None:
        return _rewrite_comparison(args, protocol, plan, jpath, recorded, now, missing)
    requested = _requested(args, plan.interval)
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    end = min(requested, pd.Timestamp(protocol.decision_end, unit="s", tz="UTC"))
    if end <= start:
        print(f"the protocol starts at {start}; nothing to judge yet (verdict: continue)")
        return 0
    result, observed, verdict = _judge(args, protocol, plan, requested)
    path = comparison_path(result, args.paper_dir)
    # the journal line first (under its lock, write-once for a final verdict), then the files
    prefixes = (BLOCKED_LINE_PREFIX, SIZING_LINE_PREFIX)
    note = " ".join(n for n in result["notes"] if n.startswith(prefixes))
    line = append_verdict(jpath, verdict, asof=observed.asof, comparison=path.name, note=note)
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


def _requested(args: argparse.Namespace, interval: str) -> pd.Timestamp:
    """The bar ``compare`` judges as of: ``--end``, never past the last closed bar."""
    now = _floor(pd.Timestamp(datetime.now(UTC)), interval)
    return min(_floor(_utc(args.end), interval), now) if args.end else now


def _frozen_files(
    folder: Path, candidate: str, protocol: PaperProtocol
) -> tuple[Plan | None, str | None]:
    """The registered plan and why the folder's files cannot be judged against it, or None; the
    plan is None when ``plan.json`` is that reason."""
    data = protocol.data
    problem = _strategy_frozen_problem(
        folder, candidate, {"WARM_FROM": data.warm_from, "START": protocol.start}
    )
    if problem is not None:
        return None, problem
    path = folder / "plan.json"
    try:
        plan = parse_plan(json.loads(path.read_text(encoding="utf-8")))
    except ValueError as exc:
        if protocol.version < RISK_PROTOCOL_VERSION:
            raise
        return None, f"{path}: {exc}"
    if plan_hash(plan) != protocol.plan_hash:
        return None, (
            f"plan.json ({plan_hash(plan)[:12]}) is not the registered plan "
            f"({protocol.plan_hash[:12]})"
        )
    return plan, _risk_frozen_problem(folder, candidate, protocol, plan)


def _voiding(folder: Path, candidate: str, protocol: PaperProtocol, plan: Plan | None) -> bool:
    """Whether a problem from :func:`_frozen_files` can void this test: any file that no longer
    matches a version 8 protocol, but not a moved folder, which says nothing about the data and
    which a re-prepare repairs. Version 7 never voids."""
    if protocol.version < RISK_PROTOCOL_VERSION:
        return False
    if plan is None:
        return True
    return _risk_frozen_problem(folder, candidate, protocol, plan, moved_ok=True) is not None


def _record_void(args: argparse.Namespace, protocol: PaperProtocol, jpath: Path, why: str) -> int:
    """The frozen grace is over and the files still do not match: record the final verdict without
    judging, because the paper data cannot be trusted. Only the breaker is read, so a trip is
    still a ``fail``; candles or a DB that cannot be read exit 2 like any compare."""
    decision = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    trip = None
    if _has_breaker(protocol):
        folder = Path(args.paper_dir) / args.candidate
        # the test's own DB, never `meta.json`'s: that file may be what made this test void
        pair = symbol_to_pair(protocol.symbol)
        trades = read_dryrun_trades(folder / "dryrun.sqlite", pair=pair)
        candles = _closed_candles(protocol, decision)
        trip = guard_module.trip_reason(protocol, folder, trades, candles, end=decision)
    for advice in (_restore(args.candidate), _again(args.candidate)):
        why = why.removesuffix(f"; {advice}")
    verdict = void_verdict(protocol, " ".join(why.split()), breaker_trip=trip)
    line = append_verdict(jpath, verdict, asof=protocol.decision_end)
    print(f"protocol verdict: {verdict.verdict}")
    print(f"  {verdict.headline}")
    print(f"journal: {jpath}")
    print(f"  {line}")
    print("Nothing was judged, so no comparison file was written.")
    print("Results are historical, not forward-looking.")
    return 0


def _again(candidate: str) -> str:
    return f"re-run `prepare --candidate {candidate}` with the same plan (the protocol is kept)"


def _restore(candidate: str) -> str:
    """The way back from an edited frozen file. A new name is no way out: the duplicate check
    refuses the same plan under one."""
    return (
        f"restore the file: delete it and re-run `prepare --candidate {candidate}` with the same "
        "plan (the journal records the rewrite once the bot has traded); the same plan under a "
        "new candidate name is refused"
    )


def _first_difference(found: str, fresh: str) -> int:
    """The 1-based line where two different texts first part."""
    pairs = zip_longest(found.splitlines(keepends=True), fresh.splitlines(keepends=True))
    return next(n for n, (a, b) in enumerate(pairs, 1) if a != b)


def _strategy_frozen_problem(folder: Path, candidate: str, expected: dict[str, int]) -> str | None:
    """Why the dry-run strategy cannot be judged against the protocol's replay, or None: its
    file must exist and render each frozen value (``WARM_FROM``: the protocol's
    ``data.warm_from``, the first bar indicators are computed from; ``START``: its ``start``,
    the first bar an entry signal counts on) exactly once, equal to the protocol's."""
    path = folder / f"{class_name_for(candidate)}.py"
    again = _again(candidate)
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


def _risk_frozen_problem(
    folder: Path, candidate: str, protocol: PaperProtocol, plan: Plan, *, moved_ok: bool = False
) -> str | None:
    """Why a protocol's frozen blocks are not what the bot runs, or None: the strategy file must
    be, byte for byte, a fresh render of the protocol, and ``config.json`` the fresh render of its
    frozen config (:func:`_config_problem`). Never read for a protocol without blocks."""
    if protocol.risk_controls is None and protocol.sizing is None:
        return None
    class_name = class_name_for(candidate)
    path = folder / f"{class_name}.py"
    fresh = render_strategy(
        plan,
        class_name=class_name,
        warm_from=protocol.data.warm_from,
        start=protocol.start,
        risk_controls=protocol.risk_controls,
        sizing=protocol.sizing,
    )
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        return f"{path}: {exc}; {_again(candidate)}"
    if source != fresh:
        return (
            f"{path} is not the strategy this protocol renders (first difference at line "
            f"{_first_difference(source, fresh)}); {_restore(candidate)}"
        )
    problem = _config_problem(folder, candidate, protocol, plan, moved_ok=moved_ok)
    return problem if problem is not None else _meta_db_problem(folder, candidate, moved_ok)


def _meta_db_problem(folder: Path, candidate: str, moved_ok: bool) -> str | None:
    """Why ``meta.json`` does not name this test's own dry-run DB, or None. ``_db_path`` trusts that
    file and no freeze covers it, so pointing it at a clean DB would judge another bot's trades.
    A moved folder may still name the old path, but only the one ``config.json`` holds too. A
    missing file is no problem: ``_db_path`` then uses the folder's DB, and re-prepare rewrites
    it."""
    path = folder / "meta.json"
    if not path.is_file():
        return None
    try:
        named = json.loads(path.read_text(encoding="utf-8")).get("db_path")
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError) as exc:
        return f"{path}: {exc}; {_again(candidate)}"
    allowed = {(folder / "dryrun.sqlite").resolve()}
    url = str(config.get("db_url", ""))
    if moved_ok and url.startswith("sqlite:///"):
        allowed.add(Path(url.removeprefix("sqlite:///")).resolve())
    if named is None or Path(named).resolve() in allowed:
        return None
    return f"{path} names another dry-run DB ({named}); {_restore(candidate)}"


def _one_line(exc: Exception) -> str:
    """An error as text for one journal line: freqtrade's stderr has newlines, and a verdict's
    line must stay one line for ``recorded_final_comparison`` to read it."""
    text = " ".join(str(exc).split())
    if len(text) <= ONE_LINE_CHARS:
        return text
    # the head says what failed, the tail of stderr says why
    return f"{text[: ONE_LINE_CHARS // 3]} ... {text[-ONE_LINE_CHARS * 2 // 3 :]}"


def _grace_end(protocol: PaperProtocol) -> pd.Timestamp:
    """When the frozen grace after the decision bar is over: from here a side that still cannot be
    judged ends in a final ``inconclusive``, the rule for missing data."""
    decision = pd.Timestamp(protocol.decision_end, unit="s", tz="UTC")
    return decision + pd.Timedelta(days=protocol.data.grace_days)


def _judge(
    args: argparse.Namespace, protocol: PaperProtocol, plan: Plan, requested: pd.Timestamp
) -> tuple[PaperComparison, ObservedPaper, ProtocolVerdict]:
    """Load the frozen candles, replay the plan with the frozen costs, and judge the dry-run
    bot against the protocol as of ``requested`` (nothing is written). A failed protected backtest
    is re-raised, for exit 2, unless what is left to judge gives a final verdict."""
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

    same_period = partial(
        backtest_same_period,
        plan,
        start=start,
        end=end,
        cfg=cfg,
        loader=lambda *a: history,
        costs=costs,
    )
    backtest = same_period()
    notes: list[str] = []
    masked, total = 0, 0
    bridge_failure: ProtectedBacktestError | None = None
    stake_problem: str | None = None
    folder = Path(args.paper_dir) / args.candidate
    trip = guard_module.trip_reason(protocol, folder, forward, candles, end=end)
    if has_protections(protocol) and trip is not None:
        # the verdict is the trip's: a bridge failure here must not hide it behind exit 2
        notes.append(blocked_unknown_note("the circuit breaker tripped first."))
    elif has_protections(protocol):
        # paper and backtest trade under the same protections; a failed bridge leaves the replay
        # checks unknown and the rest judged, and exits 2 below unless that gives a final verdict
        try:
            run = run_protected_backtest(
                protocol,
                folder,
                class_name=class_name_for(args.candidate),
                interval=plan.interval,
                end=end,
            )
            protected = masked_replay(
                backtest,
                run,
                lambda locks: same_period(locks=locks),
                interval=plan.interval,
                tolerance_bars=protocol.matching.tolerance_bars,
            )
        except ProtectedBacktestError as exc:
            bridge_failure = exc
            notes.append(blocked_unknown_note(f"{_one_line(exc)}."))
        else:
            backtest, masked, total = protected.trades, protected.masked, protected.total
            notes.append(blocked_note(masked, total))
    if protocol.sizing is not None:
        try:
            sized_replay, sized_paper = size_both_sides(
                forward,
                backtest,
                history,
                protocol.sizing,
                stop_distance=float(plan.risk.stop_loss_pct or 0.0) / 100.0,
                interval=plan.interval,
                tolerance_bars=protocol.matching.tolerance_bars,
                start=start,
                end=end,
            )
            line = sizing_line(protocol.sizing, sized_replay, sized_paper)
            unmasked = " Its replay figures are unmasked." if bridge_failure else ""
            notes.append(line + unmasked)
        except StakeMismatchError as exc:
            if trip is None:
                stake_problem = f"paper stake: {_one_line(exc)}"
                notes.append(f"{SIZING_LINE_PREFIX} not computed: {_one_line(exc)}.")
            else:
                notes.append(f"{SIZING_LINE_PREFIX} not computed after the breaker tripped: {exc}")
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
    result["notes"].extend(notes)
    slippage = modelled_slippage(history, costs)
    observed = observe_paper(
        protocol, forward, history, lab_trades=backtest, slippage=slippage, asof=requested
    )
    observed = replace(
        observed,
        breaker_trip=trip,
        blocked_entries=masked,
        replay_entries=total,
        replay_unknown=(
            None if bridge_failure is None else f"protected backtest: {_one_line(bridge_failure)}"
        ),
        stake_problem=stake_problem,
    )
    verdict = evaluate(protocol, observed)
    if bridge_failure is not None and not verdict.final:
        raise bridge_failure  # before the grace, and nothing else failed: cannot compare
    return result, observed, verdict


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


def _closed_candles(protocol: PaperProtocol, end: pd.Timestamp) -> pd.DataFrame:
    """Closed candles in ``[protocol.start, end)`` from the protocol's frozen source."""
    start = pd.Timestamp(protocol.start, unit="s", tz="UTC")
    if end <= start:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "tradable"])
    data = protocol.data
    if data.candle_source == "archive":
        loader = default_loader(load_config())
    else:
        loader = public_kline_loader(since=start, host=data.candle_host)
    history = loader(protocol.symbol, protocol.interval, end)
    return history.loc[(history.index >= start) & (history.index < end)]


def _portfolio_names(args: argparse.Namespace) -> list[str]:
    if args.candidates is not None:
        return [check_candidate_name(n.strip()) for n in args.candidates.split(",") if n.strip()]
    root = Path(args.paper_dir)
    folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    return [
        p.name for p in folders if p.name != SHADOW_FOLDER and protocol_path(root, p.name).is_file()
    ]


def _cohort(protocols: Mapping[str, PaperProtocol]) -> tuple[list[str], dict[str, str]]:
    """The largest group of tests with one ``start`` (a tie goes to the earliest) and why each
    other test is out: the common dates are the latest start's, so a late entrant would cut the
    window of every test, and an early one has days the others lack."""
    groups: dict[int, list[str]] = {}
    for name, protocol in protocols.items():
        groups.setdefault(protocol.start, []).append(name)
    if not groups:
        return [], {}
    start = max(sorted(groups), key=lambda s: len(groups[s]))
    when = pd.Timestamp(start, unit="s", tz="UTC")
    out = {
        name: f"started {pd.Timestamp(protocol.start, unit='s', tz='UTC')}, the cohort {when}"
        for name, protocol in protocols.items()
        if protocol.start != start
    }
    return groups[start], out


def _portfolio(args: argparse.Namespace) -> int:
    """N_eff of the coins and of the bots, on one cohort's dates. Writes nothing."""
    protocols = {
        name: load_registered_protocol(args.paper_dir, name, journal_dir(args))[0]
        for name in _portfolio_names(args)
    }
    names, others = (list(protocols), {}) if args.candidates is not None else _cohort(protocols)
    daily: dict[str, pd.Series] = {}
    coins: dict[str, pd.Series] = {}
    in_market: dict[str, pd.Series] = {}
    for name in names:
        protocol = protocols[name]
        folder = Path(args.paper_dir) / name
        plan = parse_plan(json.loads((folder / "plan.json").read_text(encoding="utf-8")))
        now = _floor(pd.Timestamp(datetime.now(UTC)), plan.interval)
        requested = min(_floor(_utc(args.end), plan.interval), now) if args.end else now
        end = min(requested, pd.Timestamp(protocol.decision_end, unit="s", tz="UTC"))
        one = argparse.Namespace(paper_dir=args.paper_dir, candidate=name)
        trades = read_dryrun_trades(_db_path(one), pair=symbol_to_pair(plan.symbol))
        candles = _closed_candles(protocol, end)
        kept = [t for t in trades if t.open_time < end]
        daily[name] = daily_paper_returns(kept, candles, interval=plan.interval)
        coins[name] = daily_coin_returns(candles, interval=plan.interval)
        in_market[name] = daily_in_market(kept, candles, interval=plan.interval)
    report = portfolio_report(daily, coin_returns=coins, in_market=in_market, excluded=others)
    print(render_portfolio(report), end="")
    print("Complete UTC days only; a day missing a bar is left out, never filled.")
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    """The test's frozen circuit breaker; see :mod:`research.paper.guard`."""
    try:
        return _guard(args)
    except (ValueError, OSError, httpx.HTTPError, sqlite3.Error, DataUnavailableError) as exc:
        print(f"cannot guard: {exc}", file=sys.stderr)
        return guard_module.EXIT_ERROR


def _guard(args: argparse.Namespace) -> int:
    check_candidate_name(args.candidate)
    protocol, jpath = load_registered_protocol(args.paper_dir, args.candidate, journal_dir(args))
    why = guard_module.refuse_reason(protocol)
    if why is not None:
        print(f"refusing to guard: {why}", file=sys.stderr)
        return guard_module.EXIT_ERROR
    folder = Path(args.paper_dir) / args.candidate
    plan = parse_plan(json.loads((folder / "plan.json").read_text(encoding="utf-8")))
    if plan_hash(plan) != protocol.plan_hash:
        print(
            f"refusing to guard: {folder / 'plan.json'} is not the registered plan", file=sys.stderr
        )
        return guard_module.EXIT_ERROR
    config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    check_paper_config(config)
    pair = symbol_to_pair(plan.symbol)

    def check() -> int:
        # an edited config ends the guard loudly; a halted folder still gets its stop
        # as in compare: guarding a clean DB named in meta.json would read clear for ever
        problem = _config_problem(folder, args.candidate, protocol, plan) or _meta_db_problem(
            folder, args.candidate, False
        )
        if problem is not None and not (folder / HALT_LOCK).exists():
            raise ValueError(problem)
        end = _floor(pd.Timestamp(datetime.now(UTC)), plan.interval)
        return guard_module.guard_once(
            protocol,
            folder,
            jpath,
            end=end,
            load_trades=lambda: read_dryrun_trades(_db_path(args), pair=pair),
            load_candles=lambda: _closed_candles(protocol, end),
        )

    if not args.watch:
        return check()
    return guard_module.watch(
        check,
        interval=plan.interval,
        now=lambda: datetime.now(UTC).timestamp(),
        sleep=time.sleep,
        err=lambda text: print(text, file=sys.stderr),
    )


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
    p.add_argument(
        "--stake-amount",
        type=float,
        default=None,
        help=f"fixed stake (default {DEFAULT_STAKE_AMOUNT:g}); with a sizing block it must not "
        "be set to anything but the block's maximum stake; a registered version 8 test keeps "
        "its frozen one",
    )
    p.add_argument(
        "--wallet",
        type=float,
        default=None,
        help=f"dry-run wallet in the stake currency (default {DEFAULT_WALLET:g}); it must not "
        "differ from a frozen wallet (sizing block or circuit breaker), and a registered "
        "version 8 test keeps its frozen one",
    )
    p.add_argument(
        "--risk-config",
        type=Path,
        default=None,
        help="paper_risk TOML (template: config/paper_risk.example.toml) frozen into a new "
        "protocol; a registered candidate refuses other values: a different size is a new test",
    )
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
    c.add_argument("--candidate", default=None, help="required unless --portfolio is given")
    c.add_argument(
        "--portfolio",
        action="store_true",
        help="print daily-return correlations and N_eff across the paper tests (information "
        "only: writes nothing and changes no verdict)",
    )
    c.add_argument(
        "--candidates",
        default=None,
        help="with --portfolio: comma-separated tests (default: the largest cohort of tests "
        "with one start; the others are named as excluded)",
    )
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

    g = sub.add_parser("guard", help="check a test's frozen circuit breaker; halts it on a trip")
    g.add_argument("--candidate", required=True)
    g.add_argument("--watch", action="store_true", help="check again after each bar close")
    g.set_defaults(func=cmd_guard)

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
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "compare":
        if args.portfolio and args.candidate is not None:
            parser.error("compare: --portfolio takes --candidates, not --candidate")
        if not args.portfolio and args.candidates is not None:
            parser.error("compare: --candidates needs --portfolio")
        if not args.portfolio and args.candidate is None:
            parser.error("compare: the following arguments are required: --candidate")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
