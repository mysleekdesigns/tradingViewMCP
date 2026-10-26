"""The locked-holdout check (PLAN.md Phase 8 research loop step 6, "holdout check").

The holdout ``[holdout_start, data_end)`` is the only data no search, Optuna, or tuning code
ever reads. :func:`run_holdout_check` is the single place that touches it, and it is guarded:

1. the plan must validate;
2. the run must exist and have frozen the current lab.toml fingerprint
   (:func:`lab.research.runs.check_lab_fingerprint`);
3. no plan of the plan's *behaviour family* (:mod:`lab.research.family`: its logic hash, with
   cosmetics, aliases, tuned numbers, and the interval removed, plus every logic with a
   submitted design-window trial whose returns correlate with it at or above ``[research]
   family_return_corr``, compared bar by bar on one interval and, across intervals, after
   aligning bars by timestamp and summing the finer interval onto the coarser grid, so no-op
   clauses, never-triggering stops, and a rule moved to another interval with scaled periods do
   not make a new hypothesis) may have been holdout-checked, in ANY run, and this ``plan_hash``
   not in this run (a hypothesis that fails the holdout is dead; renaming, re-tuning, padding,
   another interval, or a new run cannot buy a second look). A same-feature plan whose returns
   could not be compared (no candles to rebuild its bar times, a length mismatch, too little
   overlap) counts as family here: unknown fails CLOSED;
4. the plan's event-driven cross-check must not have failed (any run): such a plan can never
   become a candidate, so a check is not spent on it;
5. the plan must already pass every pre-holdout success criterion of the run's FROZEN bar
   (:func:`lab.research.criteria.evaluate_plan`), INCLUDING a matched event-driven cross-check
   (with nautilus_trader unavailable that row is "missing" and the holdout stays refused), so a
   check is never spent on a plan that would fail anyway;
6. data must reach past ``holdout_start`` (checked before spending, nothing is computed);
7. one check is spent through :meth:`Ledger.record_holdout_check` (``BEGIN IMMEDIATE``, the
   server-side budget ``min(frozen run budget, lab.toml holdout_budget)`` and the logic-hash +
   family refusal, both race-free); an exhausted budget refuses here. Only then are signals computed
   (full history up to the last bar, so indicators are warmed up) and trades evaluated inside
   the holdout window with next-bar fills and the configured costs (365-day annualisation);
8. the trial goes to the ledger (``window_kind="holdout"``), evidence ``{"passed",
   "logic_hash"}`` is recorded, and the caller gets only pass/fail and the budget. Not even
   which rule failed is returned (that is directional feedback an agent could tune against).

Pass rule: net annualised Sharpe >= the run's ``wf_oos_sharpe_min``, max drawdown <=
``max_drawdown_max``, net total return > 0, and at least
``max(5, ceil(min_trades * holdout_bars / design_bars))`` trades (see
:func:`holdout_min_trades`).

Results are historical and not forward-looking.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import pandas as pd

from lab.config import LabConfig
from lab.engine.splits import holdout_window, slice_window
from lab.ledger import (
    HoldoutAlreadyCheckedError,
    HoldoutBudgetExceededError,
    Ledger,
    costs_to_json,
    metrics_to_json,
)
from lab.plan import parse_plan, plan_hash, plan_to_dict
from lab.plan.portfolio import is_portfolio_plan
from lab.report import num
from lab.research.criteria import PRE_HOLDOUT_EXCLUDED, evaluate_plan, frozen_criteria
from lab.research.family import behaviour_family, family_holdout_conflict
from lab.research.logic import logic_hash
from lab.research.runs import check_lab_fingerprint, holdout_budget
from lab.runner import CandleLoader, DataUnavailableError, archive_cli_hint, default_loader
from lab.search.optuna_search import evaluate_on_window
from lab.stats import compute_metrics, sharpe_per_bar
from lab.types import Plan, TrialRecord, interval_seconds

__all__ = [
    "HOLDOUT_MIN_TRADES_FLOOR",
    "HOLDOUT_SOURCE",
    "HoldoutRefusedError",
    "holdout_min_trades",
    "run_holdout_check",
]

# The ledger's TrialSource has no "holdout" value; window_kind="holdout" marks the row. "manual"
# is a selection source, but the row has the SAME plan_hash as the plan's design-window trial
# (holdout_check requires backtest evidence for it), so DSR's N (distinct plan hashes) does not
# change, and DSR's V[SR] / PBO only combine trials on the identical design window, so the
# holdout Sharpe never enters them.
HOLDOUT_SOURCE = "manual"


# A holdout pass needs at least this many trades however short the holdout is: with 1-4 trades
# the net Sharpe / return of the holdout is a handful of coin flips, not evidence.
HOLDOUT_MIN_TRADES_FLOOR = 5


class HoldoutRefusedError(RuntimeError):
    """The holdout check was refused before any check was spent or any holdout bar evaluated."""


def holdout_min_trades(min_trades: int, holdout_bars: int, design_bars: int) -> int:
    """Trades a holdout pass needs: ``max(5, ceil(min_trades * holdout_bars / design_bars))``.

    The run's ``min_trades`` criterion is a bar on the design window; the holdout is a
    different length, so the same trade *frequency* is required (the bar scaled by the ratio
    of bar counts), with :data:`HOLDOUT_MIN_TRADES_FLOOR` as the floor. With no design bars
    (``design_bars < 1``) only the floor applies."""
    if design_bars < 1:
        return HOLDOUT_MIN_TRADES_FLOOR
    return max(HOLDOUT_MIN_TRADES_FLOOR, math.ceil(min_trades * holdout_bars / design_bars))


def _already_checked(ledger: Ledger, run_id: str, h: str) -> bool:
    """This exact plan_hash already has holdout evidence or a holdout trial in the run."""
    if ledger.evidence(run_id, plan_hash=h, kind="holdout"):
        return True
    return any(
        r.window_kind == "holdout" and r.plan_hash == h
        for r in ledger.list(run_id=run_id, limit=ledger.count(run_id))
    )


def _failed_crosscheck(ledger: Ledger, h: str) -> str | None:
    """The run id of a recorded event-driven cross-check of ``h`` that ran and did NOT match
    (any research run), or None."""
    for row in ledger.evidence(None, plan_hash=h, kind="crosscheck"):
        payload = row["payload"]
        if (
            isinstance(payload, dict)
            and payload.get("available") is not False
            and payload.get("matched") is False
        ):
            return str(row["run_id"])
    return None


def _load_full(plan: Plan, cfg: LabConfig, loader: CandleLoader) -> tuple[pd.DataFrame, Any]:
    """Candles through the last closed bar, and the holdout window. Raises
    :class:`DataUnavailableError` when there are fewer than 2 bars after ``holdout_start``."""
    step = pd.Timedelta(seconds=interval_seconds(plan.interval))
    end = pd.Timestamp.now(tz="UTC").floor(step)  # bars opening before this have closed
    candles = loader(plan.symbol, plan.interval, end)
    candles = candles.loc[candles.index < end]
    start = cfg.splits.holdout_start
    if candles.empty or int((candles.index >= start).sum()) < 2:
        raise DataUnavailableError(
            f"no {plan.symbol} {plan.interval} bars after holdout_start {start.date()}; "
            f"download recent months with `{archive_cli_hint(plan.symbol, plan.interval)}`"
        )
    window = holdout_window(cfg.splits, candles.index[-1] + step)
    return candles, window


def run_holdout_check(
    raw_plan: Mapping[str, Any] | str,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader | None = None,
) -> dict[str, Any]:
    """Spend one locked-holdout check on ``raw_plan`` (module docstring for the guards).

    A portfolio plan (``"kind": "portfolio"``) goes to
    :func:`lab.research.portfolio.run_portfolio_holdout_check` (same guards and answer).

    Returns ONLY ``{"passed": bool, "checks_used": int, "checks_left": int}``. Raises
    :class:`~lab.plan.validator.PlanValidationError` for an invalid plan,
    :class:`HoldoutRefusedError` when a guard refuses (nothing spent), and
    :class:`DataUnavailableError` when there is no holdout data (nothing spent).
    """
    if is_portfolio_plan(raw_plan):
        from lab.research.portfolio import run_portfolio_holdout_check  # lazy: heavy imports

        return run_portfolio_holdout_check(
            raw_plan, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader or default_loader(cfg)
        )
    plan = parse_plan(raw_plan)
    h = plan_hash(plan)
    lh = logic_hash(plan)
    if ledger.research_run(run_id) is None:
        raise HoldoutRefusedError(f"research run {run_id!r} was never started; call start_run")
    try:
        check_lab_fingerprint(ledger, run_id, cfg)
    except ValueError as exc:
        raise HoldoutRefusedError(f"{exc}; no check was spent") from exc
    if ledger.holdout_logic_checked(lh) or _already_checked(ledger, run_id, h):
        raise HoldoutRefusedError(
            f"plan {h[:12]} (logic {lh[:12]}) was already holdout-checked; a hypothesis gets "
            "one holdout check, ever, in any run (if it failed it is dead; renaming or "
            "re-tuning does not make it a new hypothesis)"
        )
    load = loader or default_loader(cfg)
    family = behaviour_family(
        ledger, symbol=plan.symbol, plan_hash=h, cfg=cfg, plan=plan, loader=load
    )
    conflict = family_holdout_conflict(ledger, family)
    if conflict is not None:
        raise HoldoutRefusedError(
            f"{conflict}; so plan {h[:12]} counts as that hypothesis, whose single holdout "
            "check is spent. A hypothesis gets one holdout check, ever, in any run: re-tuning, "
            "a no-op clause, a stop that never triggers, or the same rule on another interval "
            "(returns compared after aligning bars by timestamp) does not make a new one. No "
            "check was spent"
        )
    failed_in = _failed_crosscheck(ledger, h)
    if failed_in is not None:
        raise HoldoutRefusedError(
            f"plan {h[:12]} failed the event-driven cross-check (run {failed_in}), so it can "
            "never become a candidate; no holdout check was spent"
        )
    report = evaluate_plan(ledger, run_id, h, cfg, loader=load)
    if not report["pre_holdout_passed"]:
        blocking = [
            f"{r['criterion']} ({r['status']})"
            for r in report["rows"]
            if r["status"] != "pass" and r["criterion"] not in PRE_HOLDOUT_EXCLUDED
        ]
        cross = next(
            (r for r in report["rows"] if r["criterion"] == "require_crosscheck_match"), None
        )
        extra = ""
        if cross is not None and cross["status"] == "missing":
            extra = f" The holdout needs a matched event-driven cross-check first: {cross['note']}."
        raise HoldoutRefusedError(
            f"plan {h[:12]} does not pass the pre-holdout success criteria yet, so no holdout "
            f"check was spent: {', '.join(blocking)}.{extra} See get_report for details."
        )
    candles, window = _load_full(plan, cfg, load)
    budget = holdout_budget(ledger, run_id, cfg)
    try:
        used = ledger.record_holdout_check(
            run_id,
            h,
            budget=budget,
            logic_hash=lh,
            family_logic_hashes=family.logic_hashes | set(family.uncertain),
            family_plan_hashes=family.plan_hashes,
        )
    except (HoldoutBudgetExceededError, HoldoutAlreadyCheckedError) as exc:
        raise HoldoutRefusedError(f"{exc}; no check was spent") from exc

    # ---- the check has been spent; from here on only pass/fail leaves this function
    crit = frozen_criteria(ledger, run_id)
    plan_json = json.dumps(plan_to_dict(plan), sort_keys=True)
    base = {
        "run_id": run_id,
        "source": HOLDOUT_SOURCE,
        "plan_hash": h,
        "plan_json": plan_json,
        "params_json": json.dumps({"holdout_check": used}),
        "symbol": plan.symbol,
        "interval": plan.interval,
        "window_kind": "holdout",
        "window_start": window.start,
        "window_end": window.end,
        "costs_json": costs_to_json(cfg.costs),
    }
    design = (candles.index >= cfg.splits.design_start) & (candles.index < cfg.splits.holdout_start)
    passed = False
    try:
        holdout_bars = len(slice_window(candles, window))
        if holdout_bars < 2:
            raise ValueError("fewer than 2 holdout bars")
        result = evaluate_on_window(plan, candles, window, cfg)
        metrics = compute_metrics(result, plan.interval)
    except ValueError as exc:
        ledger.record(
            TrialRecord(
                **base,
                status="error",
                metrics_json="{}",
                sharpe_per_bar=None,
                n_trades=0,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
    else:
        returns = result.returns.to_numpy(dtype=float)
        ledger.record(
            TrialRecord(
                **base,
                status="ok",
                metrics_json=metrics_to_json(metrics),
                sharpe_per_bar=sharpe_per_bar(returns),
                n_trades=len(result.trades),
                returns=returns,
            )
        )
        sharpe, dd, ret = num(metrics.sharpe), num(metrics.max_drawdown), num(metrics.total_return)
        need = holdout_min_trades(crit.min_trades, holdout_bars, int(design.sum()))
        passed = (
            sharpe is not None
            and sharpe >= crit.wf_oos_sharpe_min
            and dd is not None
            and math.isfinite(dd)
            and abs(dd) <= crit.max_drawdown_max
            and ret is not None
            and ret > 0
            and int(metrics.n_trades) >= need
        )
    ledger.record_evidence(
        run_id, h, "holdout", json.dumps({"passed": passed, "logic_hash": lh}, sort_keys=True)
    )
    return {"passed": passed, "checks_used": used, "checks_left": max(0, budget - used)}
