"""CLI: ``uv run python -m lab.search --plan plan.json --run-id my-run [--trials 50]
[--seed 7] [--min-trades 30] [--walk-forward]``.

Loads the plan's candles from ``lab/data/klines`` (with the aligned ``drv_*`` derivatives
columns, via ``lab.runner.default_loader``), drops the locked holdout, and runs an Optuna
search on the design window (or walk-forward optimisation on the configured folds). Every trial
is written to ``lab/data/ledger.db``. The CLI searches every tunable param over its full
validation bounds (dev use); Phase 8 research calls the API with an explicit space.
Results are historical and not forward-looking.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import optuna

from lab.config import DEFAULT_CONFIG_PATH, load_config
from lab.engine.splits import design_view, walk_forward_folds
from lab.ledger import Ledger
from lab.plan import parse_plan, tunable_params
from lab.runner import (
    default_loader,
    derivatives_period_caps,
    gate_base_trial_check,
    plan_design_window,
)
from lab.search.optuna_search import optuna_search, walk_forward_optimize


def _fmt(x: float) -> str:
    return "n/a" if not math.isfinite(x) else f"{x:.3f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.search", description=__doc__)
    parser.add_argument("--plan", required=True, type=Path, help="plan JSON file")
    parser.add_argument("--run-id", required=True, help="ledger run id (DSR counts its trials)")
    parser.add_argument("--trials", type=int, default=None, help="trials (per fold with WF)")
    parser.add_argument("--seed", type=int, default=None, help="TPE sampler seed")
    parser.add_argument(
        "--min-trades",
        type=int,
        default=None,
        help="trials with fewer trades are failed for Optuna (default: config optuna.min_trades)",
    )
    parser.add_argument("--walk-forward", action="store_true", help="walk-forward optimisation")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    plan = parse_plan(json.loads(args.plan.read_text()))
    trials = cfg.optuna_trials if args.trials is None else args.trials
    min_trades = cfg.optuna_min_trades if args.min_trades is None else args.min_trades
    seed = cfg.optuna_seed if args.seed is None else args.seed
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    # default_loader: Parquet archive before the holdout, plus the aligned drv_* derivatives
    # columns (derivatives plans need them); a missing download raises before any trial
    candles = design_view(
        default_loader(cfg)(plan.symbol, plan.interval, cfg.splits.holdout_start), cfg.splits
    )
    # the plan's scored window: derivatives plans start where their inputs exist and pass the
    # gate-base check; missing inputs raise before any trial (same rule as the MCP tools)
    design, scoring = plan_design_window(plan, candles, cfg)
    # every trial of a derivatives plan must keep a selective price clause (same rule as MCP
    # run_sweep / walk_forward): a failing trial is ledgered as an error and never selected
    trial_check = gate_base_trial_check(candles, design, cfg) if scoring is not None else None
    # a derivatives look-back never searches past [derivatives] warmup_days (the shared scored
    # start leaves room for that much only); price-only plans keep the full default space
    caps = derivatives_period_caps(plan, cfg)
    space = (
        {
            k: (s.low, float(min(s.high, caps[k])) if k in caps else s.high)
            for k, s in tunable_params(plan).items()
        }
        if caps
        else None
    )
    with Ledger(cfg.ledger_path) as ledger:
        if args.walk_forward:
            s = cfg.splits
            folds = walk_forward_folds(
                design,
                train_months=s.wf_train_months,
                test_months=s.wf_test_months,
                step_months=s.wf_step_months,
                anchored=s.wf_anchored,
            )
            wf = walk_forward_optimize(
                plan,
                candles,
                folds,
                ledger=ledger,
                run_id=args.run_id,
                cfg=cfg,
                n_trials_per_fold=trials,
                seed=seed,
                space=space,
                min_trades=min_trades,
                trial_check=trial_check,
            )
            print(f"walk-forward {plan.name} {plan.symbol} {plan.interval}: {len(wf.folds)} folds")
            for f in wf.folds:
                print(
                    f"  fold {f.fold}: train {f.train.start.date()}..{f.train.end.date()} "
                    f"sharpe {_fmt(f.train_sharpe)} | test {f.test.start.date()}.."
                    f"{f.test.end.date()} sharpe {_fmt(f.test_metrics.sharpe)} "
                    f"trades {f.test_metrics.n_trades} params {f.best_params} "
                    f"({f.search.n_failed}/{f.search.n_trials} train trials failed)"
                    + ("" if f.selected else " (no selectable train trial: plan defaults)")
                )
            m = wf.oos_metrics
            print(
                f"stitched OOS: sharpe {_fmt(m.sharpe)} total return {_fmt(m.total_return)} "
                f"max dd {_fmt(m.max_drawdown)} trades {m.n_trades} bars {m.bars}"
            )
            charged = sum(1 for c in wf.boundary_charges if c > 0)
            print(
                f"fold boundaries: {charged} open position(s) charged exit fee + slippage when "
                "stitching; the exit is priced at the last close with that bar's slippage rate, "
                "a small approximation of the real next-open fill"
            )
            print(f"ledger: {wf.n_train_trials} train + {len(wf.folds)} test trials")
        else:
            res = optuna_search(
                plan,
                candles,
                design,
                ledger=ledger,
                run_id=args.run_id,
                cfg=cfg,
                n_trials=trials,
                seed=seed,
                space=space,
                min_trades=min_trades,
                trial_check=trial_check,
            )
            print(
                f"optuna {res.study_name}: {res.n_trials} trials ({res.n_failed} failed, "
                f"incl. < {min_trades} trades)"
            )
            print(f"best design sharpe {_fmt(res.best_value)} params {res.best_params}")
        print(f"run {args.run_id}: {ledger.count(args.run_id)} ledger trials in total")
    print("Results are historical and not forward-looking; the best of many trials is biased up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
