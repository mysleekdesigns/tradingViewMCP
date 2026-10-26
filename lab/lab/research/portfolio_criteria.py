"""Judge a portfolio plan against a research run's FROZEN success criteria (PLAN.md Phase 10).

:func:`lab.research.criteria.evaluate_plan` routes here when the plan is a portfolio plan.
Every :class:`~lab.research.contract.SuccessCriteria` row is answered from portfolio evidence
(same pass / fail / missing rules; missing is never a pass), plus three portfolio-only rows
from the run's frozen optional ``[portfolio]`` criteria table:

==============================  ============================================================
``wf_oos_sharpe_min``           as single-symbol: MIN stitched OOS net Sharpe over every
                                ``walk_forward`` of the behaviour family in every run
``dsr_min``                     as single-symbol: re-deflated with the global N and the
                                largest V[SR] (portfolio trials only share a cost model with
                                portfolio trials, so V[SR] is over portfolio plans)
``pbo_max``                     as single-symbol: max of the neighbourhood PBO and the
                                sweep-CSCV PBO of the family's sweeps (sweep basis required)
``beat_buy_hold``               net Sharpe above BTC buy-and-hold (from the first decision)
``beat_vol_target``             net Sharpe above vol-targeted BTC (from the first decision)
``random_entry_percentile_min`` Sharpe percentile vs the random-selection null (same members,
                                decision rows, breadth, turnover, weighting, nearest
                                liquidity rank with a vol-tercile preference, liquidity
                                caps, engine and costs); undefined paths are
                                dropped, and with fewer than half valid the row is missing
``cost_stress_factor``          ``adversarial``: net total return > 0 with fees, slippage AND
                                impact scaled by the factor
``require_gradual_delay_decay`` ``adversarial``: the same rebalance rows deciding on inputs
                                1/3/5 rebalance periods stale (delay rows = periods x
                                ``rebalance.every``); the shared single-symbol rule reads
                                the rows
``max_drawdown_max``            net max drawdown from the first decision
``min_trades``                  REBALANCES with at least one fill (a portfolio has no trades)
``min_profitable_regimes``      regime breakdown with BTC as the market (frozen definition)
``neighbourhood_pct``           every ±pct one-at-a-time neighbour of the portfolio tunables
                                ran (``[portfolio] neighbourhood_max_trials``) and is
                                profitable; an int whose step rounds back to its value (e.g.
                                ``min_names = 1``) leaves that side missing
``require_holdout_pass``        as single-symbol (one holdout per behaviour family, ever)
``require_crosscheck_match``    the independent accounting replay matched the engine
``beat_equal_weight``           ``[portfolio]``: net Sharpe above the equal-weight universe
``alpha_p_max``                 ``[portfolio]``: HAC p-value of the net alpha to BTC <= it
``alpha_min_annual``            ``[portfolio]``: annualised net alpha to BTC > it
==============================  ============================================================

Without a frozen ``[portfolio]`` table the three portfolio rows are "missing", so
``pre_holdout_passed`` is False (fail closed) and the note tells the owner to install it.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import fields
from typing import Any

from lab.config import LabConfig
from lab.ledger import SELECTION_SOURCES, Ledger
from lab.plan import portfolio as pf
from lab.plan.validator import PlanValidationError
from lab.portfolio_runner import costs_json_for, impact_model, portfolio_param_specs
from lab.report import num, seconds
from lab.research.contract import PortfolioCriteria, SuccessCriteria
from lab.research.criteria import (
    CRITERION_PARAMS,
    PRE_HOLDOUT_EXCLUDED,
    CriteriaReport,
    CriterionRow,
    _beat_row,
    _cost_row,
    _dd_row,
    _delay_row,
    _dsr_row,
    _fmt,
    _get,
    _holdout_row,
    _metric,
    _missing,
    _pbo_row,
    _regime_row,
    _row,
    _side_gaps,
    _wf_row,
)
from lab.research.family import FamilyInfo, family_evidence
from lab.research.logic import as_portfolio_plan
from lab.research.portfolio import portfolio_family
from lab.research.runs import budget_status, check_universe_manifest
from lab.runner import CandleLoader, default_loader, neighbourhood_overrides
from lab.types import TrialRecord

__all__ = [
    "PORTFOLIO_ROWS",
    "evaluate_portfolio_plan",
    "frozen_portfolio_criteria",
    "is_portfolio_evidence",
]

PORTFOLIO_ROWS: tuple[str, ...] = ("beat_equal_weight", "alpha_p_max", "alpha_min_annual")
_UNCAPPED = 1_000_000
_INSTALL = (
    "the run's frozen success criteria have no [portfolio] table, so this portfolio bar is not "
    "set (fail closed); the owner adds [portfolio] (see agents/config/success_criteria."
    "proposed.toml) to lab/config/success_criteria.toml and starts a new run"
)


def frozen_portfolio_criteria(ledger: Ledger, run_id: str) -> PortfolioCriteria | None:
    """The ``[portfolio]`` criteria frozen when ``run_id`` started, or None."""
    run = ledger.research_run(run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} was never started; call start_run first")
    raw = run["config"].get("portfolio") if isinstance(run["config"], dict) else None
    if not isinstance(raw, Mapping):
        return None
    names = {f.name for f in fields(PortfolioCriteria)}
    if set(raw) != names:
        raise ValueError(
            f"research run {run_id!r} froze [portfolio] criteria keys that do not match this "
            f"lab ({sorted(raw)} vs {sorted(names)}); start a new run"
        )
    return PortfolioCriteria(**raw)


def is_portfolio_evidence(
    ledger: Ledger, run_id: str, plan_hash: str, bt: Mapping[str, Any] | None
) -> bool:
    """True when ``plan_hash`` is a portfolio plan: its backtest evidence says so, or any of
    its evidence rows or its base trial carries a portfolio plan."""
    if bt is not None and (
        bt.get("kind") == pf.PORTFOLIO_KIND or pf.is_portfolio_plan(bt.get("plan"))
    ):
        return True
    for row in ledger.evidence(run_id, plan_hash=plan_hash):
        payload = row["payload"]
        if isinstance(payload, Mapping) and (
            payload.get("kind") == pf.PORTFOLIO_KIND or pf.is_portfolio_plan(payload.get("plan"))
        ):
            return True
    base = ledger.latest_research_base_trial(plan_hash)
    return base is not None and pf.is_portfolio_plan(base.plan_json)


def _plan_of(
    ledger: Ledger, run_id: str, plan_hash: str, bt: Mapping[str, Any] | None
) -> pf.PortfolioPlan | None:
    candidates: list[Any] = []
    if bt is not None:
        candidates.append(bt.get("plan"))
    for row in ledger.evidence(run_id, plan_hash=plan_hash):
        if isinstance(row["payload"], Mapping):
            candidates.append(row["payload"].get("plan"))
    base = ledger.latest_research_base_trial(plan_hash)
    if base is not None:
        candidates.append(base.plan_json)
    for c in candidates:
        if c is None or not pf.is_portfolio_plan(c):
            continue
        try:
            plan = as_portfolio_plan(c)
        except PlanValidationError:
            continue
        if pf.portfolio_plan_hash(plan) == plan_hash:
            return plan
    return None


# --------------------------------------------------------------------------- rows


def _random_selection_row(c: SuccessCriteria, bt: Mapping[str, Any] | None) -> CriterionRow:
    name, thr = "random_entry_percentile_min", c.random_entry_percentile_min
    if bt is None:
        return _missing(name, thr, "no backtest evidence: run run_backtest")
    paths = _get(bt, "benchmarks", "random_selection", "n_paths")
    if not paths:
        return _missing(name, thr, "the random-selection null was not run (no decision?)")
    valid = _get(bt, "benchmarks", "random_selection", "n_valid_paths")
    if isinstance(valid, bool) or not isinstance(valid, int) or 2 * valid < int(paths):
        return _missing(
            name,
            thr,
            f"only {valid if isinstance(valid, int) else 'an unknown number'} of {paths} "
            "random-selection paths have a defined Sharpe (fewer than half), so the null is "
            "not established (fail closed)",
        )
    pct = num(_get(bt, "benchmarks", "random_selection", "percentile"))
    basis = (
        f"Sharpe percentile vs {valid} of {paths} random-selection portfolios with a defined "
        "Sharpe (same members, decision rows, breadth, kept names, weighting, nearest liquidity "
        "rank with a vol-tercile preference where the band has room, and liquidity caps; same "
        "engine and costs; scored from the first decision; the report's vol-only sidebar is "
        "descriptive and not read here)"
    )
    if pct is None:
        return _row(name, "fail", None, thr, f"percentile undefined; {basis}")
    return _row(name, "pass" if pct >= thr else "fail", pct, thr, basis)


def _portfolio_delay_row(c: SuccessCriteria, adv: Mapping[str, Any] | None) -> CriterionRow:
    """The shared :func:`lab.research.criteria._delay_row` (unchanged rule, reading delays in
    rows), with a note that a portfolio's delays are whole rebalance periods of stale inputs."""
    row = _delay_row(c, dict(adv) if adv is not None else None)
    rows = _get(adv, "delay", "rows") if adv is not None else None
    periods = [
        r.get("delay_periods") for r in (rows or []) if isinstance(r, Mapping) and "delay" in r
    ]
    if periods and all(isinstance(p, int) for p in periods):
        row["note"] = (
            f"{row['note']}; portfolio delays are {'/'.join(str(p) for p in periods)} "
            "rebalance periods of stale inputs on the same rebalance rows"
        )
    return row


def _rebalances_row(c: SuccessCriteria, bt: Mapping[str, Any] | None) -> CriterionRow:
    name, thr = "min_trades", c.min_trades
    if bt is None:
        return _missing(name, thr, "no backtest evidence: run run_backtest")
    n = _get(bt, "portfolio", "n_rebalances_filled")
    if isinstance(n, bool) or not isinstance(n, int | float):
        return _missing(name, thr, "filled-rebalance count not in the backtest evidence")
    return _row(
        name,
        "pass" if n >= thr else "fail",
        float(n),
        thr,
        "portfolio plan: rebalances with at least one fill on the design window (from the first "
        "decision); min_trades counts rebalances, not coin trades",
    )


def _neighbourhood_row(
    c: SuccessCriteria,
    ledger: Ledger,
    run_id: str,
    plan: pf.PortfolioPlan | None,
    bt: Mapping[str, Any] | None,
    cfg: LabConfig,
) -> CriterionRow:
    name, pct = "neighbourhood_pct", c.neighbourhood_pct
    if pct == 0:
        return _row(name, "pass", None, pct, "not required by this run's criteria")
    if bt is None or plan is None:
        return _missing(name, pct, "no backtest evidence: run run_backtest")
    specs = portfolio_param_specs(plan)
    steps = tuple(s for s in cfg.stats.neighbourhood_steps if abs(s - 1.0) <= pct + 1e-9)
    reach = max((abs(s - 1.0) for s in cfg.stats.neighbourhood_steps), default=0.0)
    lost, clipped = _side_gaps(specs, steps)
    if lost:
        return _missing(
            name,
            pct,
            f"no distinct neighbour for {', '.join(lost)} (the step rounds back to the integer "
            "value, e.g. min_names = 1), so robustness on that side is not established",
        )
    expected: dict[str, Mapping[str, float]] = {}
    for ov in neighbourhood_overrides(specs, steps, _UNCAPPED):
        try:
            expected[pf.portfolio_plan_hash(pf.with_params(plan, ov))] = ov
        except PlanValidationError:
            continue
    start, end = _get(bt, "window", "start"), _get(bt, "window", "end")
    trial_id = _get(bt, "trial_id")
    costs = costs_json_for(cfg.costs, impact_model(cfg))  # the lab's current cost model
    if isinstance(trial_id, int) and not isinstance(trial_id, bool):
        with contextlib.suppress(ValueError):  # unknown trial id: keep the lab's cost model
            costs = ledger.get(trial_id).costs_json
    found: dict[str, TrialRecord] = {}
    for rec in ledger.list(run_id=run_id, limit=ledger.count(run_id)):  # newest first
        if (
            rec.source == "neighbourhood"
            and rec.plan_hash in expected
            and rec.plan_hash not in found
            and rec.costs_json == costs
            and seconds(rec.window_start) == start
            and seconds(rec.window_end) == end
        ):
            found[rec.plan_hash] = rec
    if not found:
        return _missing(name, pct, "no neighbourhood trials of this plan in the ledger")
    if len(found) < len(expected):
        cap = cfg.portfolio.neighbourhood_max_trials
        why = "an older backtest, or trials run with other costs"
        if cap - 1 < len(expected):
            why = f"lab.toml [portfolio] neighbourhood_max_trials = {cap} caps the grid; or {why}"
        return _missing(
            name,
            pct,
            f"only {len(found)} of {len(expected)} expected ±{pct:.0%} neighbours were run ({why})",
        )
    profitable = 0
    for rec in found.values():
        ret = num(_metric(rec, "total_return")) if rec.status == "ok" else None
        profitable += int(ret is not None and ret > 0)
    note = (
        f"{profitable} of {len(found)} one-at-a-time ±{pct:.0%} neighbours of the portfolio "
        "tunables profitable (net total return > 0 from each one's first decision)"
    )
    if clipped:
        note += f"; clipped at a bound: {', '.join(clipped)}"
    if reach + 1e-9 < pct:
        note += f"; the lab's grid only reaches ±{reach:.0%}"
    note += "; rebalance.offset is fixed (run_adversarial reports other offsets)"
    return _row(
        name, "pass" if profitable == len(found) else "fail", profitable / len(found), pct, note
    )


def _crosscheck_row(c: SuccessCriteria, cross: Mapping[str, Any] | None) -> CriterionRow:
    name, required = "require_crosscheck_match", c.require_crosscheck_match
    if not required:
        return _row(name, "pass", None, required, "not required by this run's criteria")
    if cross is None or not isinstance(cross.get("matched"), bool):
        return _missing(
            name,
            required,
            "no accounting cross-check evidence yet: run_crosscheck (a holdout check needs a "
            "matched cross-check first)",
        )
    ok = bool(cross["matched"])
    detail = (
        f"independent replay vs engine: max relative equity difference "
        f"{_fmt(num(cross.get('max_rel_equity_diff')))} (tolerance "
        f"{_fmt(num(cross.get('tolerance')))}), orders {cross.get('n_orders_engine')} vs "
        f"{cross.get('n_orders_replay')}"
    )
    return _row(name, "pass" if ok else "fail", ok, required, f"matched = {ok}; {detail}")


def _equal_weight_row(pc: PortfolioCriteria | None, bt: Mapping[str, Any] | None) -> CriterionRow:
    name = "beat_equal_weight"
    if pc is None:
        return _missing(name, None, _INSTALL)
    if not pc.beat_equal_weight:
        return _row(name, "pass", None, False, "not required by this run's criteria")
    if bt is None:
        return _missing(name, True, "no backtest evidence: run run_backtest")
    mine = num(_get(bt, "metrics", "net", "sharpe"))
    other = num(_get(bt, "benchmarks", "equal_weight", "sharpe"))
    if other is None:
        return _missing(name, True, "equal-weight universe Sharpe is undefined on this window")
    if mine is None:
        return _row(name, "fail", False, True, f"net Sharpe undefined vs equal-weight {other:.2f}")
    ok = mine > other
    return _row(
        name,
        "pass" if ok else "fail",
        ok,
        True,
        f"net Sharpe {mine:.2f} vs the equal-weight universe {other:.2f} (same decision rows, "
        "costs, impact and liquidity caps; from the first decision)",
    )


def _alpha_rows(
    pc: PortfolioCriteria | None, bt: Mapping[str, Any] | None
) -> tuple[CriterionRow, CriterionRow]:
    if pc is None:
        return _missing("alpha_p_max", None, _INSTALL), _missing("alpha_min_annual", None, _INSTALL)
    if bt is None:
        note = "no backtest evidence: run run_backtest"
        return (
            _missing("alpha_p_max", pc.alpha_p_max, note),
            _missing("alpha_min_annual", pc.alpha_min_annual, note),
        )
    ab = _get(bt, "alpha_beta")
    if not isinstance(ab, Mapping) or "alpha_annual" not in ab:
        why = _get(ab, "error") if isinstance(ab, Mapping) else None
        note = f"alpha/beta to BTC unavailable ({why or 'no BTC candles or no decision'})"
        return (
            _missing("alpha_p_max", pc.alpha_p_max, note),
            _missing("alpha_min_annual", pc.alpha_min_annual, note),
        )
    p = num(ab.get("alpha_p"))
    a = num(ab.get("alpha_annual"))
    basis = (
        f"OLS of net per-row returns on BTC close-to-close returns from the first decision, "
        f"n = {ab.get('n')}, beta {_fmt(num(ab.get('beta')))}, HAC t {_fmt(num(ab.get('alpha_t')))}"
        f" ({ab.get('hac_lags')} lags); alpha annualised x 365-day bars"
    )
    p_row = (
        _row("alpha_p_max", "fail", None, pc.alpha_p_max, f"p-value undefined; {basis}")
        if p is None
        else _row(
            "alpha_p_max", "pass" if p <= pc.alpha_p_max else "fail", p, pc.alpha_p_max, basis
        )
    )
    a_row = (
        _row("alpha_min_annual", "fail", None, pc.alpha_min_annual, f"alpha undefined; {basis}")
        if a is None
        else _row(
            "alpha_min_annual",
            "pass" if a > pc.alpha_min_annual else "fail",
            a,
            pc.alpha_min_annual,
            basis,
        )
    )
    return p_row, a_row


# --------------------------------------------------------------------------- public API


def evaluate_portfolio_plan(
    ledger: Ledger,
    run_id: str,
    plan_hash: str,
    cfg: LabConfig,
    criteria: SuccessCriteria,
    *,
    loader: CandleLoader | None = None,
) -> CriteriaReport:
    """Judge portfolio plan ``plan_hash`` (module docstring). Raises ``ValueError`` when the
    run's frozen universe manifest no longer matches the one on disk."""
    c = criteria
    check_universe_manifest(ledger, run_id, cfg)
    pc = frozen_portfolio_criteria(ledger, run_id)
    load = loader or default_loader(cfg)

    def latest(kind: str) -> dict[str, Any] | None:
        rows = ledger.evidence(run_id, plan_hash=plan_hash, kind=kind)
        payload = rows[-1]["payload"] if rows else None
        return payload if isinstance(payload, dict) else None

    bt, adv, cross = latest("backtest"), latest("adversarial"), latest("crosscheck")
    plan = _plan_of(ledger, run_id, plan_hash, bt)
    if plan is not None:
        family = portfolio_family(ledger, plan, plan_hash, cfg, load)
    else:
        family = FamilyInfo(logic_hashes=set(), plan_hashes={plan_hash})
    wfs = [row["payload"] for row in family_evidence(ledger, family, "walk_forward")]
    sweeps = family_evidence(ledger, family, "sweep")
    dsr_row, _ = _dsr_row(c, ledger, run_id, plan_hash, bt, cfg)
    by_name: dict[str, CriterionRow] = {
        "wf_oos_sharpe_min": _wf_row(c, wfs, family),
        "dsr_min": dsr_row,
        "pbo_max": _pbo_row(c, ledger, bt, sweeps, cfg),
        "beat_buy_hold": _beat_row(
            "beat_buy_hold", c.beat_buy_hold, bt, "btc_buy_and_hold", "BTC buy-and-hold"
        ),
        "beat_vol_target": _beat_row(
            "beat_vol_target", c.beat_vol_target, bt, "btc_vol_target", "vol-targeted BTC"
        ),
        "random_entry_percentile_min": _random_selection_row(c, bt),
        "cost_stress_factor": _cost_row(c, adv),
        "require_gradual_delay_decay": _portfolio_delay_row(c, adv),
        "max_drawdown_max": _dd_row(c, bt),
        "min_trades": _rebalances_row(c, bt),
        "min_profitable_regimes": _regime_row(c, bt),
        "neighbourhood_pct": _neighbourhood_row(c, ledger, run_id, plan, bt, cfg),
        "require_holdout_pass": _holdout_row(c, ledger, run_id, plan_hash, family),
        "require_crosscheck_match": _crosscheck_row(c, cross),
    }
    rows = [by_name[f.name] for f in fields(SuccessCriteria) if f.name not in CRITERION_PARAMS]
    alpha_p, alpha_min = _alpha_rows(pc, bt)
    rows += [_equal_weight_row(pc, bt), alpha_p, alpha_min]
    return CriteriaReport(
        run_id=run_id,
        plan_hash=plan_hash,
        pre_holdout_passed=all(
            r["status"] == "pass" for r in rows if r["criterion"] not in PRE_HOLDOUT_EXCLUDED
        ),
        passed=all(r["status"] == "pass" for r in rows),
        rows=rows,
        missing=[r["criterion"] for r in rows if r["status"] == "missing"],
        ledger_n=ledger.count_distinct_plans_research(sources=SELECTION_SOURCES),
        budget=budget_status(ledger, run_id, cfg),
    )
