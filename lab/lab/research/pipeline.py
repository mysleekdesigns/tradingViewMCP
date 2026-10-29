"""Design-window research evidence: the ONE way a backtest or adversarial report becomes
evidence for a research run (PLAN.md Phase 8).

Both the ``strategy-lab`` MCP server and the agents' seed calibration call these, so every
recorded report has the same shape:

* hidden keys (trade list, equity curves) stripped, ``n_trades_listed`` kept, long lists
  compacted, so agents are never handed curves they could overfit to by eye;
* backtest evidence carries a ``regimes`` block computed with the run's FROZEN regime
  definition (``SuccessCriteria.regime_window_days`` / ``regime_threshold``), which the
  ``min_profitable_regimes`` criterion reads;
* evidence is keyed by ``plan_hash(parse_plan(raw_plan))``, the plan as submitted, and every
  payload carries the plan's ``logic_hash`` (:mod:`lab.research.logic`) so criteria can gather
  the results of one hypothesis across re-tuned or renamed copies;
* :func:`crosscheck_evidence` re-runs a backtested plan in the event-driven engine
  (nautilus_trader) on the same design window and records whether the engines agree.

Risk overlays (PLAN.md Phase 15): an overlay plan's backtest evidence drops the run-local DSR and
adds the ``"overlay"`` block (:func:`lab.research.overlay.overlay_block`) that
:mod:`lab.research.overlay_criteria` judges; the cross-check accepts a risk-overlay run. Every
overlay entry point first applies :func:`lab.research.overlay_criteria.check_overlay_untuned`
(one published parameter set per overlay logic, across every overlay run), before any trial.

Only design-window bars are ever loaded (``loader(..., holdout_start)``); the holdout is
reachable only through :mod:`lab.research.holdout`. Callers own budgets and locking.

Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from lab.config import LabConfig
from lab.crosscheck.nautilus import crosscheck
from lab.data.derivatives_columns import require_plan_inputs
from lab.engine.splits import design_view, slice_window
from lab.ledger import Ledger
from lab.plan import parse_plan, plan_hash
from lab.report import num
from lab.research.contract import SuccessCriteria
from lab.research.criteria import frozen_criteria, regime_breakdown
from lab.research.evidence import compact_report, record_report
from lab.research.logic import logic_hash
from lab.research.overlay import overlay_block
from lab.research.overlay_criteria import check_overlay_untuned
from lab.research.runs import frozen_overlay_criteria, frozen_question
from lab.runner import (
    CandleLoader,
    DataUnavailableError,
    derivatives_series,
    plan_design_window,
    run_adversarial_report,
    run_backtest_report,
)
from lab.types import Plan, Window

__all__ = [
    "HIDDEN_REPORT_KEYS",
    "adversarial_evidence",
    "backtest_evidence",
    "crosscheck_evidence",
    "strip_report",
]

# Report keys never handed to an agent: eyeballing curves and trade lists invites overfitting.
HIDDEN_REPORT_KEYS = frozenset({"trades", "equity", "benchmark_equity"})


def strip_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """``report`` without :data:`HIDDEN_REPORT_KEYS` (plus ``n_trades_listed`` when it had a
    trade list), compacted with :func:`compact_report`."""
    out = {k: v for k, v in report.items() if k not in HIDDEN_REPORT_KEYS}
    if isinstance(report.get("trades"), list):
        out["n_trades_listed"] = len(report["trades"])
    return compact_report(out)


def _regimes(
    ledger: Ledger,
    plan: Plan,
    report: Mapping[str, Any],
    *,
    cfg: LabConfig,
    loader: CandleLoader,
    criteria: SuccessCriteria,
) -> dict[str, Any]:
    """Regime breakdown of the report's design-window trial, or ``{"error": ...}`` (the
    criterion then reads as missing, never as a pass)."""
    try:
        rec = ledger.get(int(report["trial_id"]), include_returns=True)
        if rec.returns is None:
            raise ValueError("trial has no stored returns")
        history = design_view(
            loader(plan.symbol, plan.interval, cfg.splits.holdout_start), cfg.splits
        )
        return regime_breakdown(
            history,
            rec.window_start,
            rec.window_end,
            rec.returns,
            plan.interval,
            window_days=criteria.regime_window_days,
            threshold=criteria.regime_threshold,
        )
    except (ValueError, KeyError, TypeError, DataUnavailableError) as exc:
        return {"error": f"regime breakdown unavailable: {exc}"}


def backtest_evidence(
    raw_plan: Mapping[str, Any] | str,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader,
) -> dict[str, Any]:
    """Run the design-window backtest report for ``raw_plan`` under research run ``run_id``,
    strip it, add the ``regimes`` block (the run's frozen regime definition), record it as
    ``"backtest"`` evidence, and return the recorded dict (it keeps ``trial_id``).

    Raises ``ValueError`` before any trial runs if ``run_id`` was never started (no frozen
    criteria), plus whatever ``run_backtest_report`` raises (``PlanValidationError``,
    ``DataUnavailableError``)."""
    plan = parse_plan(raw_plan)
    h = plan_hash(plan)
    if plan.kind == "overlay":
        return _overlay_backtest_evidence(
            raw_plan, plan, h, run_id=run_id, cfg=cfg, ledger=ledger, loader=loader
        )
    criteria = frozen_criteria(ledger, run_id)  # no frozen bar, no trials
    report = run_backtest_report(
        raw_plan, cfg=cfg, ledger=ledger, run_id=run_id, loader=loader, run_id_assigned=True
    )
    out = strip_report(report)
    out["regimes"] = _regimes(ledger, plan, report, cfg=cfg, loader=loader, criteria=criteria)
    out["logic_hash"] = logic_hash(plan)
    record_report(ledger, run_id, h, "backtest", out)
    return out


OVERLAY_EVIDENCE_NOTE = (
    "risk-overlay run: the rows are judged on the overlay block (get_report); the run-local "
    "deflated Sharpe is dropped (overlay trials count in the edge question's trial count N but "
    "never in its V[SR] pools), and the remaining report fields are information only"
)


def _overlay_backtest_evidence(
    raw_plan: Mapping[str, Any] | str,
    plan: Plan,
    h: str,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader,
) -> dict[str, Any]:
    """``backtest_evidence`` for an overlay plan (PLAN.md Phase 15): the lab's design-window
    report (its base trial is the ledgered overlay trial), without the run-local DSR, plus the
    ``"overlay"`` block (:func:`lab.research.overlay.overlay_block`, with the run's FROZEN
    overlay rows' CDaR level, episode depth and bootstrap block) whose 1x rerun is checked
    against that base trial. Refused before any trial when the run is not a risk-overlay run."""
    criteria = frozen_overlay_criteria(ledger, run_id)  # no frozen rows, no trials
    check_overlay_untuned(ledger, plan)  # one parameter set per logic
    report = run_backtest_report(
        raw_plan, cfg=cfg, ledger=ledger, run_id=run_id, loader=loader, run_id_assigned=True
    )
    out = strip_report(report)
    stats = out.get("stats")
    if isinstance(stats, dict):
        out["stats"] = {k: v for k, v in stats.items() if k != "dsr"}
    out["overlay"] = overlay_block(
        plan,
        cfg=cfg,
        ledger=ledger,
        run_id=run_id,
        loader=loader,
        criteria=criteria,
        trial_id=int(report["trial_id"]),
    )
    out["overlay_note"] = OVERLAY_EVIDENCE_NOTE
    out["logic_hash"] = logic_hash(plan)
    record_report(ledger, run_id, h, "backtest", out)
    return out


def adversarial_evidence(
    raw_plan: Mapping[str, Any] | str,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader,
) -> dict[str, Any]:
    """Run the design-window adversarial report for ``raw_plan`` under ``run_id``, strip it,
    record it as ``"adversarial"`` evidence, and return the recorded dict."""
    plan = parse_plan(raw_plan)
    h = plan_hash(plan)
    if plan.kind == "overlay":
        check_overlay_untuned(ledger, plan)  # one parameter set per logic
    report = run_adversarial_report(raw_plan, cfg=cfg, ledger=ledger, run_id=run_id, loader=loader)
    out = strip_report(report)
    out["logic_hash"] = logic_hash(plan)
    record_report(ledger, run_id, h, "adversarial", out)
    return out


def crosscheck_evidence(
    raw_plan: Mapping[str, Any] | str,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader,
) -> dict[str, Any]:
    """Re-run ``raw_plan`` in the event-driven engine (:func:`lab.crosscheck.nautilus.crosscheck`)
    on the design window of its latest ``backtest`` evidence in ``run_id``, with
    ``cfg.costs`` / ``cfg.init_cash``, and record ``"crosscheck"`` evidence::

        {"matched": bool, "available": bool, "reason": str | None, "logic_hash": str,
         "n_trades_vectorbt": int, "n_trades_event": int, "matched_trades": int,
         "return_diff_pct": float | None, "notes": [str, ...]}

    ``matched`` is True only when nautilus_trader is available and the engines agree. Raises
    ``ValueError`` (nothing recorded) when the run never started or the plan has no backtest
    evidence in the run (the cross-check confirms an existing backtest; it is not a new one),
    ``DataUnavailableError`` (nothing recorded) when derivatives columns the plan reads are
    missing, and ``GateBaseRefusedError`` (a ``PlanValidationError``, nothing recorded) when a
    gated plan's price clause does not select. Only design-window bars are loaded."""
    plan = parse_plan(raw_plan)
    h = plan_hash(plan)
    if frozen_question(ledger, run_id) == "risk_overlay":  # the run must exist
        frozen_overlay_criteria(ledger, run_id)
        if plan.kind == "overlay":
            check_overlay_untuned(ledger, plan)  # one parameter set per logic
    else:
        frozen_criteria(ledger, run_id)
    rows = ledger.evidence(run_id, plan_hash=h, kind="backtest")
    if not rows:
        raise ValueError(
            f"plan {h[:12]} has no backtest evidence in run {run_id}; run run_backtest first "
            "(the cross-check re-runs that backtest in the event-driven engine)"
        )
    window_json = (
        rows[-1]["payload"].get("window") if isinstance(rows[-1]["payload"], dict) else None
    )
    start = window_json.get("start") if isinstance(window_json, dict) else None
    end = window_json.get("end") if isinstance(window_json, dict) else None
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        raise ValueError(f"the backtest evidence of plan {h[:12]} has no design window")
    window = Window(
        start=pd.Timestamp(start, unit="s", tz="UTC"),
        end=pd.Timestamp(end, unit="s", tz="UTC"),
        kind="design",
    )
    if window.end > cfg.splits.holdout_start:
        raise ValueError(f"backtest window of plan {h[:12]} reaches the locked holdout")
    candles = design_view(loader(plan.symbol, plan.interval, cfg.splits.holdout_start), cfg.splits)
    # a plan whose derivatives inputs are gone raises here, before anything is recorded
    require_plan_inputs(
        plan, slice_window(candles, window), symbol=plan.symbol, interval=plan.interval
    )
    # a derivatives plan must still be scorable and its price clause must select (raises
    # GateBaseRefusedError / DataUnavailableError, nothing recorded); the window itself is the
    # backtest evidence's, which already starts at the plan's scored start
    plan_design_window(plan, candles, cfg, series=derivatives_series(cfg, ledger, run_id))
    res = crosscheck(plan, candles, costs=cfg.costs, init_cash=cfg.init_cash, window=window)
    out: dict[str, Any] = {
        "matched": bool(res.available and res.agree),
        "available": bool(res.available),
        "reason": res.reason,
        "logic_hash": logic_hash(plan),
        "n_trades_vectorbt": int(res.n_trades_vectorbt),
        "n_trades_event": int(res.n_trades_event),
        "matched_trades": int(res.matched_trades),
        "return_diff_pct": num(res.return_diff_pct),
        "notes": [str(n) for n in res.notes],
    }
    record_report(ledger, run_id, h, "crosscheck", out)
    return compact_report(out)
