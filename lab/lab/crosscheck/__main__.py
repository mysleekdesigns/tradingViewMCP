"""CLI: ``uv run python -m lab.crosscheck --plan plan.json [--run-id crosscheck-dev]
[--tolerance 0.5]``.

Re-runs the plan on the design window of the local data (``lab/data/klines``, holdout excluded)
in nautilus_trader and compares it with the vectorbt engine, once with the configured costs and
once with the same fees but no slippage (the mechanics pass), and attributes the configured
gap to vectorbt's stop slippage. The configured vectorbt backtest is recorded in the trial
ledger (source ``manual``, window ``design``, its ``costs_json``); the nautilus re-runs and the
mechanics pass reproduce that same trial, not new ones, so they are not counted again.
Results are historical and not forward-looking.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from lab.config import DEFAULT_CONFIG_PATH, load_config
from lab.crosscheck.nautilus import CrossCheckResult, crosscheck
from lab.data.store import load_candles
from lab.engine.splits import design_view, design_window
from lab.ledger import Ledger, costs_to_json, metrics_to_json
from lab.plan import parse_plan, plan_hash, plan_to_dict
from lab.stats import compute_metrics, sharpe_per_bar
from lab.types import CostModel, Plan, TrialRecord, Window


def _pct(x: float | None, digits: int = 4) -> str:
    return "n/a" if x is None or not math.isfinite(x) else f"{x:.{digits}f}%"


def _ret(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:+.2f}%"


def format_result(res: CrossCheckResult) -> str:
    """Human-readable summary of a cross-check."""
    if not res.available:
        return f"cross-check unavailable: {res.reason}"
    lines = [
        f"engine: {res.engine}   agree: {res.agree}   (tolerance {res.tolerance_pct}%)",
        f"configured costs: agree {res.agree_configured}",
        f"  trades: vectorbt {res.n_trades_vectorbt}, nautilus {res.n_trades_event}, "
        f"matched by entry bar {res.matched_trades}",
        f"  max entry price diff {_pct(res.max_entry_price_diff_pct)}, "
        f"max exit price diff {_pct(res.max_exit_price_diff_pct)}",
        f"  total return: vectorbt {_ret(res.total_return_vectorbt)}, "
        f"nautilus {_ret(res.total_return_event)}, "
        f"log-equity diff {_pct(res.return_diff_pct)}",
        f"  stop/TP slippage explains {_pct(res.slippage_explained_pct)}, residual "
        f"{_pct(res.attribution_residual_pct)}, max per-exit diff "
        f"{_pct(res.max_stop_attribution_diff_pct)} -> explains gap: {res.slippage_explains_gap}",
        "mechanics (fees kept, slippage 0): agree "
        f"{res.mechanics_agree}"
        + ("" if res.mechanics_ran_separately else " (no slippage configured: same pass)"),
        f"  total return: vectorbt {_ret(res.mechanics_total_return_vectorbt)}, "
        f"nautilus {_ret(res.mechanics_total_return_event)}, "
        f"log-equity diff {_pct(res.mechanics_return_diff_pct)}, "
        f"max exit price diff {_pct(res.mechanics_max_exit_price_diff_pct)}",
        f"price tick: 1e-{res.price_precision}",
        "notes:",
        *(f"  - {n}" for n in res.notes),
    ]
    return "\n".join(lines)


def _record(
    ledger: Ledger,
    run_id: str,
    plan: Plan,
    window: Window,
    res: CrossCheckResult,
    costs: CostModel,
) -> int:
    vb = res.vectorbt_result
    if vb is None:
        raise ValueError("cross-check has no vectorbt result to record")
    returns = vb.returns.to_numpy(dtype=float)
    return ledger.record(
        TrialRecord(
            run_id=run_id,
            source="manual",
            status="ok",
            plan_hash=plan_hash(plan),
            plan_json=json.dumps(plan_to_dict(plan), sort_keys=True, separators=(",", ":")),
            params_json="{}",
            symbol=plan.symbol,
            interval=plan.interval,
            window_kind="design",
            window_start=window.start,
            window_end=window.end,
            metrics_json=metrics_to_json(compute_metrics(vb, plan.interval)),
            sharpe_per_bar=sharpe_per_bar(returns),
            n_trades=len(vb.trades),
            costs_json=costs_to_json(costs),
            returns=np.asarray(returns),
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.crosscheck", description=__doc__)
    parser.add_argument("--plan", required=True, type=Path, help="plan JSON file")
    parser.add_argument("--run-id", default="crosscheck-dev", help="ledger run id")
    parser.add_argument("--tolerance", type=float, default=0.5, help="agreement tolerance, %%")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    plan = parse_plan(json.loads(args.plan.read_text()))
    candles = design_view(
        load_candles(plan.symbol, plan.interval, parquet_dir=cfg.parquet_dir), cfg.splits
    )
    window = design_window(cfg.splits)
    res = crosscheck(
        plan,
        candles,
        costs=cfg.costs,
        init_cash=cfg.init_cash,
        tolerance_pct=args.tolerance,
        window=window,
    )
    print(
        f"{plan.name} {plan.symbol} {plan.interval} design window "
        f"{window.start.date()}..{window.end.date()} (holdout excluded)"
    )
    print(format_result(res))
    if res.available:
        with Ledger(cfg.ledger_path) as ledger:
            trial_id = _record(ledger, args.run_id, plan, window, res, cfg.costs)
        print(f"vectorbt run recorded as ledger trial {trial_id} (run {args.run_id})")
    print("Results are historical and not forward-looking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
