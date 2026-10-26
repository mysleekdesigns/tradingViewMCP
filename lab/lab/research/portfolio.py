"""Portfolio plans in the ledgered research pipeline (PLAN.md Phase 10): evidence, sweeps,
walk-forward with lineage, families and the locked-holdout check.

The single-symbol pipeline (:mod:`lab.research.pipeline`, :mod:`lab.research.holdout`) is
unchanged; callers route on :func:`lab.plan.portfolio.is_portfolio_plan`. Every function here
first checks that the run froze a universe manifest that still matches the one on disk
(:func:`lab.research.runs.check_universe_manifest`), so evidence never mixes two universes.

Identity: evidence is keyed by :func:`lab.plan.portfolio.portfolio_plan_hash` and carries the
plan's :func:`lab.research.logic.portfolio_logic_hash` (from
:func:`lab.plan.portfolio.plan_logic`: score feature, weighting, banding on/off, gate on/off).
The behaviour family (:func:`lab.research.family.behaviour_family`) adds every portfolio logic
whose submitted design-window returns correlate with the plan's at or above ``[research]
family_return_corr``, so a no-op band or an always-open gate does not buy a second holdout.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import pandas as pd

from lab.config import LabConfig
from lab.ledger import (
    SELECTION_SOURCES,
    HoldoutAlreadyCheckedError,
    HoldoutBudgetExceededError,
    Ledger,
    metrics_to_json,
)
from lab.plan import portfolio as pf
from lab.portfolio_runner import (
    StaleUniverseError,
    costs_json_for,
    design_window,
    family_loader,
    impact_model,
    load_universe_data,
    parse_portfolio_plan,
    portfolio_adversarial_report,
    portfolio_backtest_report,
    portfolio_crosscheck,
    portfolio_folds,
    portfolio_holdout_evaluation,
    portfolio_param_specs,
    portfolio_sweep,
    portfolio_walk_forward,
    universe_symbol,
)
from lab.report import metrics_dict, num, seconds
from lab.research.evidence import compact_report, record_report
from lab.research.family import FamilyInfo, behaviour_family
from lab.research.logic import portfolio_logic_hash
from lab.research.runs import check_lab_fingerprint, check_universe_manifest, holdout_budget
from lab.runner import CandleLoader
from lab.stats import sharpe_per_bar
from lab.types import TrialRecord, Window

__all__ = [
    "PortfolioRefusedError",
    "portfolio_adversarial_evidence",
    "portfolio_backtest_evidence",
    "portfolio_crosscheck_evidence",
    "portfolio_family",
    "portfolio_lineage_check",
    "portfolio_space",
    "portfolio_sweep_evidence",
    "portfolio_walk_forward_evidence",
    "run_portfolio_holdout_check",
]

HIDDEN_KEYS = frozenset({"trades", "equity", "benchmark_equity"})
_EPS = 1e-9


class PortfolioRefusedError(RuntimeError):
    """A portfolio holdout check was refused before anything was spent or evaluated."""


def _strip(report: Mapping[str, Any]) -> dict[str, Any]:
    return compact_report({k: v for k, v in report.items() if k not in HIDDEN_KEYS})


def _window_json(w: Window) -> dict[str, Any]:
    return {"kind": w.kind, "start": seconds(w.start), "end": seconds(w.end)}


# --------------------------------------------------------------------------- families


def portfolio_family(
    ledger: Ledger,
    plan: pf.PortfolioPlan,
    plan_hash: str,
    cfg: LabConfig,
    loader: CandleLoader,
) -> FamilyInfo:
    """The plan's behaviour family among portfolio trials (pseudo-symbol
    :func:`lab.portfolio_runner.universe_symbol`)."""
    return behaviour_family(
        ledger,
        symbol=universe_symbol(cfg),
        plan_hash=plan_hash,
        cfg=cfg,
        plan=plan,
        loader=family_loader(cfg, loader),
    )


# --------------------------------------------------------------------------- spaces


def portfolio_space(
    plan: pf.PortfolioPlan, space: Mapping[str, Mapping[str, Any]], neighbourhood_pct: float
) -> dict[str, tuple[float, float]]:
    """Validate a search space ``{path: {low, high}}`` like single-symbol sweeps: tunable paths
    only (never ``rebalance.offset``), no step (except 1 for ints), ``low < high``, inside the
    path's bounds, and at least ``2 x neighbourhood_pct x |mid|`` wide (the run's frozen
    criteria). Raises ``ValueError`` naming the key."""
    if not space:
        raise ValueError(
            "space must name at least one tunable param: {path: {low, high}}; validate_plan "
            "lists the tunable paths and their bounds"
        )
    specs = portfolio_param_specs(plan)
    out: dict[str, tuple[float, float]] = {}
    for key, rng in space.items():
        spec = specs.get(key)
        if spec is None:
            hint = (
                " (rebalance.offset is fixed: tuning the weekday fits the calendar)"
                if key == "rebalance.offset"
                else ""
            )
            raise ValueError(
                f"space key {key!r} is not a tunable param of this portfolio plan{hint}; "
                f"tunable: {', '.join(specs)}"
            )
        step = rng.get("step")
        if step is not None and not (spec.kind == "int" and step == 1):
            raise ValueError(
                f"space[{key!r}].step = {step!r} is not supported: int params are searched at "
                "step 1 and float params continuously; omit step"
            )
        low, high = float(rng["low"]), float(rng["high"])
        if not low < high:
            raise ValueError(f"space[{key!r}] needs low < high, got low={low:g} high={high:g}")
        if low < spec.low - _EPS or high > spec.high + _EPS:
            raise ValueError(
                f"space[{key!r}] = [{low:g}, {high:g}] is outside the bounds "
                f"[{spec.low:g}, {spec.high:g}]"
            )
        mid = (low + high) / 2.0
        need = 2.0 * neighbourhood_pct * max(abs(mid), _EPS)
        if high - low < need:
            raise ValueError(
                f"space[{key!r}] = [{low:g}, {high:g}] is {high - low:g} wide; this run's frozen "
                f"neighbourhood_pct {neighbourhood_pct:g} needs at least {need:g}. Search a "
                "wider range"
            )
        out[key] = (low, high)
    return out


def portfolio_lineage_check(
    plan: pf.PortfolioPlan,
    space: Mapping[str, tuple[float, float]],
    ranges: Mapping[str, tuple[float, float, int]],
) -> tuple[list[str], list[str]]:
    """``(problems, notes)``: like :func:`lab.research.family.wf_lineage_check` for portfolio
    plans. A searched path must span every value its behaviour family already tried (clamped to
    the searchable bounds); an unsearched path must never have been tried at another value.
    Portfolio plans have no rule literals (every number is a tunable or the fixed offset)."""
    specs = portfolio_param_specs(plan)
    problems: list[str] = []
    notes: list[str] = []
    for key in sorted(ranges):
        lo_t, hi_t, n = ranges[key]
        spec = specs.get(key)
        lo_req, hi_req = lo_t, hi_t
        if spec is not None:
            lo_req, hi_req = max(lo_t, spec.low), min(hi_t, spec.high)
            if lo_req > hi_req:
                lo_req = hi_req = min(max(spec.default, spec.low), spec.high)
            if lo_req != lo_t or hi_req != hi_t:
                notes.append(
                    f"{key}: tried values [{lo_t:g}, {hi_t:g}] reach outside the searchable "
                    f"bounds [{spec.low:g}, {spec.high:g}]; the required coverage is clamped to "
                    f"[{lo_req:g}, {hi_req:g}]"
                )
        if key in space:
            lo, hi = float(space[key][0]), float(space[key][1])
            if lo > lo_req + _EPS or hi < hi_req - _EPS:
                problems.append(
                    f"{key} [{lo:g}, {hi:g}] does not cover [{lo_req:g}, {hi_req:g}], the values "
                    f"this behaviour family already tried ({n} values)"
                )
        elif spec is not None and (lo_req < spec.default - _EPS or hi_req > spec.default + _EPS):
            problems.append(
                f"{key} is not searched (fixed at {spec.default:g}) but this behaviour family "
                f"already tried it over [{lo_req:g}, {hi_req:g}]; search at least that range"
            )
    return problems, notes


# --------------------------------------------------------------------------- evidence


def portfolio_backtest_evidence(
    raw_plan: Mapping[str, Any] | str,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader,
) -> dict[str, Any]:
    """Run :func:`lab.portfolio_runner.portfolio_backtest_report` under ``run_id``, add the
    regime breakdown (the run's frozen regime definition, BTC as the market) and the logic
    hash, record it as ``"backtest"`` evidence and return the recorded dict."""
    from lab.research.criteria import frozen_criteria

    plan = parse_portfolio_plan(raw_plan, cfg)
    h = pf.portfolio_plan_hash(plan)
    criteria = frozen_criteria(ledger, run_id)  # no frozen bar, no trials
    check_lab_fingerprint(ledger, run_id, cfg)
    check_universe_manifest(ledger, run_id, cfg)
    report = portfolio_backtest_report(
        pf.plan_to_dict(plan),
        cfg=cfg,
        ledger=ledger,
        run_id=run_id,
        loader=loader,
        regime=(criteria.regime_window_days, criteria.regime_threshold),
    )
    out = _strip(report)
    out["logic_hash"] = portfolio_logic_hash(plan)
    record_report(ledger, run_id, h, "backtest", out)
    return out


def portfolio_adversarial_evidence(
    raw_plan: Mapping[str, Any] | str,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader,
) -> dict[str, Any]:
    """Adversarial report (:func:`lab.portfolio_runner.portfolio_adversarial_report`) recorded
    as ``"adversarial"`` evidence."""
    plan = parse_portfolio_plan(raw_plan, cfg)
    h = pf.portfolio_plan_hash(plan)
    check_universe_manifest(ledger, run_id, cfg)
    report = portfolio_adversarial_report(
        pf.plan_to_dict(plan), cfg=cfg, ledger=ledger, run_id=run_id, loader=loader
    )
    out = _strip(report)
    out["logic_hash"] = portfolio_logic_hash(plan)
    record_report(ledger, run_id, h, "adversarial", out)
    return out


def portfolio_crosscheck_evidence(
    raw_plan: Mapping[str, Any] | str,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader,
) -> dict[str, Any]:
    """Re-run the plan's latest backtest window through the independent accounting replay and
    record ``"crosscheck"`` evidence ``{matched, available, reason, logic_hash, ...}``. Needs
    ``backtest`` evidence of this exact plan in the run (``ValueError`` otherwise)."""
    from lab.research.criteria import frozen_criteria

    plan = parse_portfolio_plan(raw_plan, cfg)
    h = pf.portfolio_plan_hash(plan)
    frozen_criteria(ledger, run_id)
    check_universe_manifest(ledger, run_id, cfg)
    rows = ledger.evidence(run_id, plan_hash=h, kind="backtest")
    if not rows:
        raise ValueError(
            f"plan {h[:12]} has no backtest evidence in run {run_id}; run run_backtest first "
            "(the cross-check re-runs that backtest in an independent accounting replay)"
        )
    win = rows[-1]["payload"].get("window") if isinstance(rows[-1]["payload"], dict) else None
    start = win.get("start") if isinstance(win, dict) else None
    end = win.get("end") if isinstance(win, dict) else None
    if not isinstance(start, int) or not isinstance(end, int) or end <= start:
        raise ValueError(f"the backtest evidence of plan {h[:12]} has no design window")
    window = Window(
        start=pd.Timestamp(start, unit="s", tz="UTC"),
        end=pd.Timestamp(end, unit="s", tz="UTC"),
        kind="design",
    )
    out = portfolio_crosscheck(plan, cfg, loader, window)
    out["logic_hash"] = portfolio_logic_hash(plan)
    record_report(ledger, run_id, h, "crosscheck", out)
    return compact_report(out)


def portfolio_sweep_evidence(
    raw_plan: Mapping[str, Any] | str,
    space: Mapping[str, tuple[float, float]],
    n_trials: int,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader,
) -> dict[str, Any]:
    """Optuna sweep of a portfolio plan on the design window (every trial ledgered), recorded
    as ``"sweep"`` evidence (its study name feeds the sweep-PBO basis)."""
    plan = parse_portfolio_plan(raw_plan, cfg)
    h = pf.portfolio_plan_hash(plan)
    check_universe_manifest(ledger, run_id, cfg)
    data = load_universe_data(cfg, loader, end=cfg.splits.holdout_start)
    window = design_window(data, cfg)
    res = portfolio_sweep(
        plan,
        data,
        window,
        ledger=ledger,
        run_id=run_id,
        cfg=cfg,
        n_trials=n_trials,
        seed=cfg.optuna_seed,
        space=space,
        min_trades=cfg.optuna_min_trades,
    )
    out: dict[str, Any] = {
        "kind": pf.PORTFOLIO_KIND,
        "run_id": run_id,
        "plan_hash": h,
        "logic_hash": portfolio_logic_hash(plan),
        "plan": pf.plan_to_dict(plan),
        "study_name": res.study_name,
        "study": res.study_name,
        "window": _window_json(window),
        "space": {k: [lo, hi] for k, (lo, hi) in space.items()},
        "space_canonical": {k: [float(lo), float(hi)] for k, (lo, hi) in space.items()},
        "seed": cfg.optuna_seed,
        "min_trades": cfg.optuna_min_trades,
        "n_trials": res.n_trials,
        "n_failed": res.n_failed,
        "n_pruned_infeasible": res.n_pruned,
        "best_params": res.best_params,
        "best_sharpe": num(res.best_value),
        "best_plan_hash": pf.portfolio_plan_hash(res.best_plan) if res.best_plan else None,
        "best_plan": pf.plan_to_dict(res.best_plan) if res.best_plan else None,
        "dsr_n": ledger.count_distinct_plans_research(sources=SELECTION_SOURCES),
        "note": (
            "best_sharpe is the maximum of n_trials in-sample results (scored from each trial's "
            "first decision) and is biased upward; judge the plan with walk_forward and "
            f"get_report. Trials with fewer than {cfg.optuna_min_trades} filled rebalances are "
            "never selected; exit_quantile < quantile combinations are pruned (error rows)."
        ),
    }
    record_report(ledger, run_id, h, "sweep", out)
    return compact_report(out)


def portfolio_walk_forward_evidence(
    raw_plan: Mapping[str, Any] | str,
    space: Mapping[str, tuple[float, float]],
    n_trials_per_fold: int,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader,
    lineage_notes: list[str],
) -> dict[str, Any]:
    """Walk-forward optimisation of a portfolio plan (train-only Optuna, one test run per
    fold), recorded as ``"walk_forward"`` evidence; ``oos_metrics.sharpe`` is what the
    ``wf_oos_sharpe_min`` criterion reads. The caller checks the lineage first."""
    plan = parse_portfolio_plan(raw_plan, cfg)
    h = pf.portfolio_plan_hash(plan)
    check_universe_manifest(ledger, run_id, cfg)
    data = load_universe_data(cfg, loader, end=cfg.splits.holdout_start)
    window = design_window(data, cfg)
    folds = portfolio_folds(window, cfg)
    wf = portfolio_walk_forward(
        plan,
        data,
        folds,
        ledger=ledger,
        run_id=run_id,
        cfg=cfg,
        n_trials_per_fold=n_trials_per_fold,
        seed=cfg.optuna_seed,
        space=space,
        min_trades=cfg.optuna_min_trades,
    )
    out: dict[str, Any] = {
        "kind": pf.PORTFOLIO_KIND,
        "run_id": run_id,
        "plan_hash": h,
        "logic_hash": portfolio_logic_hash(plan),
        "plan": pf.plan_to_dict(plan),
        "study": [f.search.study_name for f in wf.folds],
        "window": _window_json(window),
        "space": {k: [lo, hi] for k, (lo, hi) in space.items()},
        "space_canonical": {k: [float(lo), float(hi)] for k, (lo, hi) in space.items()},
        "oos_metrics": metrics_dict(wf.oos_metrics),
        "folds": [
            {
                "fold": f.fold,
                "train": _window_json(f.train),
                "test": _window_json(f.test),
                "best_params": f.best_params,
                "selected": f.selected,
                "train_sharpe": num(f.train_sharpe),
                "test_sharpe": num(f.test_metrics.sharpe),
                "test_total_return": num(f.test_metrics.total_return),
                "test_rebalances_filled": int(f.test_metrics.n_trades),
            }
            for f in wf.folds
        ],
        "n_train_trials": wf.n_train_trials,
        "lineage_notes": lineage_notes,
        "boundary_charges": list(wf.boundary_charges),
        "note": (
            "oos_metrics stitch every fold's test window (params chosen on train only), net of "
            "fees, slippage and impact; positions open at a fold boundary are charged their "
            "exit. oos n_trades = filled rebalances."
        ),
    }
    record_report(ledger, run_id, h, "walk_forward", out)
    return compact_report(out)


# --------------------------------------------------------------------------- holdout


def run_portfolio_holdout_check(
    raw_plan: Mapping[str, Any] | str,
    *,
    run_id: str,
    cfg: LabConfig,
    ledger: Ledger,
    loader: CandleLoader,
) -> dict[str, Any]:
    """The locked-holdout check for a portfolio plan, with the same guards, budget, family
    refusal and pass/fail-only answer as :func:`lab.research.holdout.run_holdout_check`.

    Pass rule: net Sharpe (scored from the first holdout decision) >= ``wf_oos_sharpe_min``,
    max drawdown <= ``max_drawdown_max``, net total return > 0, and at least
    ``holdout_min_trades(min_trades, holdout rows, design rows)`` filled rebalances.
    Raises :class:`~lab.research.holdout.HoldoutRefusedError` (nothing spent) when a guard
    refuses, including a stale universe rank table that ends before the holdout candles (the
    tail would be scored as cash; the owner rebuilds the universe), and
    :class:`~lab.runner.DataUnavailableError` when there is no holdout data. A plan whose
    ``rebalance.offset`` is not the lab's pinned 0 is rejected by
    :func:`~lab.portfolio_runner.parse_portfolio_plan` before any guard runs."""
    from lab.research.criteria import PRE_HOLDOUT_EXCLUDED, evaluate_plan, frozen_criteria
    from lab.research.holdout import (
        HOLDOUT_SOURCE,
        HoldoutRefusedError,
        _already_checked,
        _failed_crosscheck,
        holdout_min_trades,
    )

    plan = parse_portfolio_plan(raw_plan, cfg)
    h = pf.portfolio_plan_hash(plan)
    lh = portfolio_logic_hash(plan)
    if ledger.research_run(run_id) is None:
        raise HoldoutRefusedError(f"research run {run_id!r} was never started; call start_run")
    try:
        check_lab_fingerprint(ledger, run_id, cfg)
        check_universe_manifest(ledger, run_id, cfg)
    except ValueError as exc:
        raise HoldoutRefusedError(f"{exc}; no check was spent") from exc
    if ledger.holdout_logic_checked(lh) or _already_checked(ledger, run_id, h):
        raise HoldoutRefusedError(
            f"plan {h[:12]} (logic {lh[:12]}) was already holdout-checked; a hypothesis gets "
            "one holdout check, ever, in any run (if it failed it is dead; renaming or "
            "re-tuning does not make it a new hypothesis)"
        )
    from lab.research.family import family_holdout_conflict

    family = portfolio_family(ledger, plan, h, cfg, loader)
    conflict = family_holdout_conflict(ledger, family)
    if conflict is not None:
        raise HoldoutRefusedError(
            f"{conflict}; so plan {h[:12]} counts as that hypothesis, whose single holdout "
            "check is spent (a no-op band or gate, or re-tuning, does not make a new one). No "
            "check was spent"
        )
    failed_in = _failed_crosscheck(ledger, h)
    if failed_in is not None:
        raise HoldoutRefusedError(
            f"plan {h[:12]} failed the accounting cross-check (run {failed_in}), so it can "
            "never become a candidate; no holdout check was spent"
        )
    report = evaluate_plan(ledger, run_id, h, cfg, loader=loader)
    if not report["pre_holdout_passed"]:
        blocking = [
            f"{r['criterion']} ({r['status']})"
            for r in report["rows"]
            if r["status"] != "pass" and r["criterion"] not in PRE_HOLDOUT_EXCLUDED
        ]
        raise HoldoutRefusedError(
            f"plan {h[:12]} does not pass the pre-holdout success criteria yet, so no holdout "
            f"check was spent: {', '.join(blocking)}. See get_report for details."
        )
    try:
        evaluate, window, design_rows = portfolio_holdout_evaluation(plan, cfg, loader)
    except StaleUniverseError as exc:
        raise HoldoutRefusedError(f"{exc}. No holdout check was spent") from exc
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
    impact = impact_model(cfg)
    base = {
        "run_id": run_id,
        "source": HOLDOUT_SOURCE,
        "plan_hash": h,
        "plan_json": json.dumps(pf.plan_to_dict(plan), sort_keys=True),
        "params_json": json.dumps({"holdout_check": used}),
        "symbol": universe_symbol(cfg),
        "interval": cfg.universe.interval,
        "window_kind": "holdout",
        "window_start": window.start,
        "window_end": window.end,
        "costs_json": costs_json_for(cfg.costs, impact),
    }
    passed = False
    try:
        run, metrics, holdout_rows = evaluate()
    except ValueError as exc:
        ledger.record(
            TrialRecord(
                **base,  # type: ignore[arg-type]
                status="error",
                metrics_json="{}",
                sharpe_per_bar=None,
                n_trades=0,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
    else:
        returns = run.returns.to_numpy(dtype=float)
        spb = sharpe_per_bar(returns) if returns.size >= 2 else math.nan
        ledger.record(
            TrialRecord(
                **base,  # type: ignore[arg-type]
                status="ok",
                metrics_json=metrics_to_json(metrics),
                sharpe_per_bar=spb if math.isfinite(spb) else None,
                n_trades=int(run.n_filled),
                returns=returns,
            )
        )
        sharpe, dd, ret = num(metrics.sharpe), num(metrics.max_drawdown), num(metrics.total_return)
        need = holdout_min_trades(crit.min_trades, holdout_rows, design_rows)
        passed = (
            sharpe is not None
            and sharpe >= crit.wf_oos_sharpe_min
            and dd is not None
            and abs(dd) <= crit.max_drawdown_max
            and ret is not None
            and ret > 0
            and int(run.n_filled) >= need
        )
    ledger.record_evidence(
        run_id, h, "holdout", json.dumps({"passed": passed, "logic_hash": lh}, sort_keys=True)
    )
    return {"passed": passed, "checks_used": used, "checks_left": max(0, budget - used)}
