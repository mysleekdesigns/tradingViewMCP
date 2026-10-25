"""Paper-trade a research candidate with freqtrade dry-run, and compare it with its backtest.

Usage (from ``agents/``)::

    uv run python paper_trade.py prepare --plan-file plan.json --candidate NAME \
        [--exchange binance] [--public-api-host data-api.binance.vision] [--launch]
    uv run python paper_trade.py compare --candidate NAME [--start 2026-10-01] [--end ...] \
        [--source api|archive]

``prepare`` writes ``agents/data/paper/<NAME>/`` and prints the ``freqtrade trade --dry-run``
command (``--launch`` starts it). ``compare`` replays the plan with the lab engine over the
paper period and writes ``comparison-<date>.json`` + ``.md`` next to the dry-run DB.

Paper trading only: the config is always ``dry_run: true`` with no exchange keys. Results are
historical, not forward-looking.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from lab.config import load_config
from lab.plan import parse_plan, plan_hash
from lab.plan.validator import PlanValidationError
from lab.research.contract import PAPER_DIR
from lab.runner import default_loader
from lab.types import interval_seconds
from research.paper.compare import (
    backtest_same_period,
    compare,
    public_kline_loader,
    read_dryrun_trades,
    write_comparison,
)
from research.paper.freqtrade import (
    check_candidate_name,
    launch,
    prepare_candidate,
    startup_candle_count,
    symbol_to_pair,
)

DEFAULT_PUBLIC_HOST = "data-api.binance.vision"


def _floor(ts: pd.Timestamp, interval: str) -> pd.Timestamp:
    step = interval_seconds(interval)
    return pd.Timestamp(int(ts.timestamp()) // step * step, unit="s", tz="UTC")


def _utc(text: str) -> pd.Timestamp:
    t = pd.Timestamp(text)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def cmd_prepare(args: argparse.Namespace) -> int:
    try:
        raw = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read plan file {args.plan_file}: {exc}", file=sys.stderr)
        return 2
    plan = raw.get("plan", raw) if isinstance(raw, dict) else raw  # seed files wrap the plan
    try:
        prepared = prepare_candidate(
            plan,
            candidate=args.candidate,
            paper_dir=args.paper_dir,
            exchange=args.exchange,
            public_api_host=args.public_api_host,
            stake_amount=args.stake_amount,
            dry_run_wallet=args.wallet,
        )
    except (PlanValidationError, ValueError) as exc:
        print(f"cannot prepare candidate: {exc}", file=sys.stderr)
        return 2
    print(f"prepared {prepared.dir}")
    print("dry-run command:")
    print("  " + " ".join(prepared.command))
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
    check_candidate_name(args.candidate)
    folder = Path(args.paper_dir) / args.candidate
    plan_path, meta_path = folder / "plan.json", folder / "meta.json"
    if not plan_path.is_file():
        print(f"{plan_path} not found; run `prepare` first", file=sys.stderr)
        return 2
    plan = parse_plan(json.loads(plan_path.read_text(encoding="utf-8")))
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    now = pd.Timestamp(datetime.now(UTC))
    end = _utc(args.end) if args.end else _floor(now, plan.interval)
    if args.start:
        start = _utc(args.start)
    elif meta.get("prepared_at"):
        start = _floor(_utc(meta["prepared_at"]), plan.interval)
    else:
        print("no --start and no meta.json prepared_at; pass --start", file=sys.stderr)
        return 2
    db_path = Path(meta.get("db_path") or folder / "dryrun.sqlite")
    try:
        forward = read_dryrun_trades(db_path, pair=symbol_to_pair(plan.symbol))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cfg = load_config()
    if args.source == "archive":
        base_loader = default_loader(cfg)
    else:
        warm = pd.Timedelta(seconds=interval_seconds(plan.interval)) * startup_candle_count(plan)
        base_loader = public_kline_loader(since=start - warm, host=args.public_api_host)
    cache: dict[tuple[str, str, pd.Timestamp], pd.DataFrame] = {}

    def loader(symbol: str, interval: str, until: pd.Timestamp) -> pd.DataFrame:
        key = (symbol, interval, until)
        if key not in cache:
            cache[key] = base_loader(symbol, interval, until)
        return cache[key]

    backtest = backtest_same_period(plan, start=start, end=end, cfg=cfg, loader=loader)
    history = loader(plan.symbol, plan.interval, end)
    candles = history.loc[(history.index >= start) & (history.index < end)]
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
    )
    path = write_comparison(result, args.paper_dir)
    print(f"verdict: {result['verdict']}")
    print(f"written: {path} (+ .md)")
    print("Results are historical, not forward-looking.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--paper-dir", type=Path, default=PAPER_DIR, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="write the freqtrade dry-run strategy + config")
    p.add_argument("--plan-file", required=True, help="plan JSON (or a seed file)")
    p.add_argument("--candidate", required=True, help="folder name under agents/data/paper/")
    p.add_argument("--exchange", default="binance")
    p.add_argument("--public-api-host", default=None, help="e.g. data-api.binance.vision")
    p.add_argument("--stake-amount", type=float, default=100.0)
    p.add_argument("--wallet", type=float, default=1000.0, help="dry-run wallet (stake currency)")
    p.add_argument("--launch", action="store_true", help="start freqtrade trade --dry-run")
    p.set_defaults(func=cmd_prepare)

    c = sub.add_parser("compare", help="weekly backtest-vs-dry-run comparison")
    c.add_argument("--candidate", required=True)
    c.add_argument("--start", default=None, help="UTC date/time; default: prepared_at")
    c.add_argument("--end", default=None, help="UTC date/time; default: last bar boundary")
    c.add_argument("--source", choices=("api", "archive"), default="api", help="candle source")
    c.add_argument("--public-api-host", default=DEFAULT_PUBLIC_HOST)
    c.set_defaults(func=cmd_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
