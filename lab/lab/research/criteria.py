"""Judge one plan against a research run's FROZEN success criteria (PLAN.md Phase 8).

:func:`evaluate_plan` reads the criteria snapshotted into the ledger when the run started
(never the current ``success_criteria.toml``, so the bar cannot move mid-run) and the latest
evidence of each kind recorded for the plan's ``plan_hash`` (the hash of the plan as submitted,
before any Optuna parameters). Walk-forward, sweep-PBO, and holdout evidence are gathered across
the plan's *behaviour family* (:mod:`lab.research.family`: its logic hash, without cosmetics,
tuned numbers, or the interval, plus every logic whose design-window returns correlate with it
at or above lab.toml ``[research] family_return_corr``) in EVERY research run, so a re-tuned,
renamed, no-op-padded, or re-run copy of one hypothesis cannot shop for its best result. The run
must have frozen the current
lab.toml fingerprint (:func:`lab.research.runs.check_lab_fingerprint`), else ``ValueError``.
It returns one row per :class:`SuccessCriteria` field except the regime-definition parameters
in ``CRITERION_PARAMS``:

* ``"pass"`` / ``"fail"`` when the evidence answers the question;
* ``"missing"`` when it does not (no evidence of that kind yet, or the statistic could not be
  computed). Missing is never a pass.

Where each row comes from:

==============================  ============================================================
``wf_oos_sharpe_min``           the MINIMUM stitched out-of-sample net Sharpe over every
                                ``walk_forward`` result of the plan's behaviour family in
                                every research run (the latest or the best would reward
                                repeating walk-forward, here or in a new run)
``dsr_min``                     re-deflated now with the global research trial count (below)
``pbo_max``                     the MAX of the ``backtest`` evidence's PBO (CSCV over the ±20%
                                neighbourhood) and CSCV over the design-window Optuna trials of
                                every ``sweep`` of the plan's behaviour family (any research
                                run) with at least lab.toml ``[research] min_pbo_trials`` usable
                                trials. The sweep basis is REQUIRED (the neighbourhood alone
                                measures stability, not selection): missing without it, and
                                "sweep too small for PBO" when every sweep is under the floor
``beat_buy_hold``               ``backtest``: net Sharpe above buy-and-hold's
``beat_vol_target``             ``backtest``: net Sharpe above vol-targeted buy-and-hold's
``random_entry_percentile_min`` ``backtest``: percentile vs the same-trade-count random null
``cost_stress_factor``          ``adversarial``: net total return > 0 at that cost multiple
``require_gradual_delay_decay`` ``adversarial``: delay rows (same rule as the delay verdict)
``max_drawdown_max``            ``backtest``: net max drawdown
``min_trades``                  ``backtest``: net trade count
``min_profitable_regimes``      ``backtest`` evidence's ``regimes`` block, added by
                                ``lab.research.pipeline.backtest_evidence`` with
                                :func:`regime_breakdown` and the run's frozen
                                ``regime_window_days`` / ``regime_threshold`` (the lab's report
                                itself has no regime breakdown)
``neighbourhood_pct``           the ledger's ``neighbourhood`` trials of this plan (same costs);
                                missing unless every neighbour of the full grid was run and
                                every tunable has its -pct and +pct neighbour (a side clipped
                                at a validator bound, or a derivatives look-back clipped at the
                                ``[derivatives] warmup_days`` cap the runner applies, is noted;
                                a side that rounds back to the integer base value is missing);
                                rule literals are not perturbed (the note lists them)
``require_holdout_pass``        ``holdout`` evidence ``{"passed": bool}``; fail if another
                                plan of the behaviour family used the (single) holdout check
``require_crosscheck_match``    ``crosscheck`` evidence ``{"matched", "available", ...}``:
                                unavailable engine -> missing, matched -> pass, else fail. It
                                gates the holdout (``pre_holdout_passed`` includes it), so a
                                holdout check needs a matched cross-check first
==============================  ============================================================

DSR: the report's DSR was deflated with the trial count at the time it ran; every later trial
raises the bar. So the DSR row recomputes it from the plan's design-window trial (its per-bar
returns are in the ledger) with ``N`` = distinct plans tried in EVERY run of the ledger
(research runs AND the web UI's Strategy Tester / ad-hoc API runs; all symbols and intervals;
``Ledger.count_distinct_plans_research``, a new run never resets the bar) and ``V[SR]`` = the
largest of (a) the run's variance of commensurable Sharpes (same interval, window, and costs;
latest per distinct plan), (b) the same over EVERY research run
(``Ledger.selection_sharpe_variance_research``), and (c) a cross-interval pool: every research
run's latest design-window Sharpe per distinct plan with the same costs on ANY interval (and
that interval's design window, which may differ from this plan's), annualised with its own
365-day bar count and rescaled to this plan's per-bar scale, so moving a winner to a fresh
interval cannot shrink the variance it is deflated with. With fewer than lab.toml
``[research] min_variance_plans`` plans in the pool the row is "missing" (V would be noise),
and with N > 1 and no estimable ``V[SR]`` the DSR would silently equal the PSR, so the row is
"missing" too.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Any, Literal, NotRequired, TypedDict

import numpy as np
import pandas as pd

from lab.config import LabConfig
from lab.data.derivatives_columns import plan_derivative_columns
from lab.evaluate.adversarial import DELAY_UNCHANGED_RATIO, DELAY_VANISH_RATIO
from lab.ledger import SELECTION_SOURCES, Ledger, costs_to_json
from lab.plan import parse_plan, plan_hash, tunable_params, with_params
from lab.plan.validator import PlanValidationError
from lab.registry.regime import BEAR, BULL, regime_labels
from lab.report import num, seconds
from lab.research.contract import SuccessCriteria
from lab.research.family import (
    FamilyInfo,
    behaviour_family,
    family_evidence,
    family_holdout_conflict,
)
from lab.research.logic import literal_vector, logic_hash
from lab.research.runs import budget_status, check_derivatives_snapshot, check_lab_fingerprint
from lab.runner import (
    CandleLoader,
    derivatives_period_caps,
    neighbourhood_overrides,
    pbo_split_count,
)
from lab.stats import deflated_sharpe, pbo_cscv
from lab.types import TrialRecord, bars_per_year, interval_seconds

__all__ = [
    "CRITERION_PARAMS",
    "PRE_HOLDOUT_EXCLUDED",
    "CriteriaReport",
    "CriterionRow",
    "CriterionStatus",
    "evaluate_plan",
    "frozen_criteria",
    "regime_breakdown",
]

CriterionStatus = Literal["pass", "fail", "missing"]

# Rows that do not gate the holdout check (they are the holdout and what comes after it).
# The event-driven cross-check is NOT excluded: a holdout check is only spent on a plan whose
# cross-check ran and matched (an unavailable engine leaves the row "missing", so the holdout
# stays refused until nautilus_trader is installed).
PRE_HOLDOUT_EXCLUDED: tuple[str, ...] = ("require_holdout_pass",)

# A V[SR] at or below this is float noise around zero (identical Sharpes give ~1e-34, not 0):
# a per-bar Sharpe spread of 1e-9 is no spread, and the DSR would silently equal the PSR.
_V_FLOOR = 1e-18

# SuccessCriteria fields that parameterise another criterion's row instead of being a criterion
# of their own: the regime definition (trailing window and +/- threshold) used by
# ``min_profitable_regimes``. They are frozen with the rest of the criteria when a run starts.
CRITERION_PARAMS: tuple[str, ...] = ("regime_window_days", "regime_threshold")

# Market regimes for the "profitable in 2 of 3 regimes" criterion: a bar's regime is the
# trailing ``regime_window_days`` simple return against +/- ``regime_threshold`` (both from the
# run's frozen success criteria). Labels are causal (hedgeFund's label_regimes via
# lab.registry.regime.regime_labels).
_SIDEWAYS = 3 - BEAR - BULL  # hedgeFund's states are Bear 0, Sideways 1, Bull 2
_REGIME_NAMES: dict[int, str] = {BEAR: "bear", _SIDEWAYS: "sideways", BULL: "bull"}
_REGIME_FEATURES = frozenset({"regime_state", "regime_signal"})


class CriterionRow(TypedDict):
    criterion: str
    status: CriterionStatus
    value: float | bool | None
    threshold: float | bool | None
    note: str


class CriteriaReport(TypedDict):
    run_id: str
    plan_hash: str
    pre_holdout_passed: bool  # every row except the holdout is "pass" (cross-check included)
    passed: bool  # every row is "pass"
    rows: list[CriterionRow]
    missing: list[str]  # criteria with status "missing"
    ledger_n: int  # DSR N: distinct plans tried in every run of the ledger (global)
    budget: dict[str, Any]
    # derivatives plans only: facts that are NOT criteria (the ungated price-only baseline)
    informational: NotRequired[dict[str, Any]]


# --------------------------------------------------------------------------- regimes


def regime_breakdown(
    history: pd.DataFrame,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    returns: np.ndarray | pd.Series,
    interval: str,
    *,
    window_days: int,
    threshold: float,
) -> dict[str, Any]:
    """Per-regime summary of a backtest's per-bar net ``returns`` on ``[window_start,
    window_end)``.

    A bar is bull when the trailing ``window_days`` simple return is above ``+threshold``, bear
    when below ``-threshold``, else sideways. Pass the run's frozen
    ``SuccessCriteria.regime_window_days`` / ``regime_threshold`` (:func:`frozen_criteria`).

    ``history`` holds the candles through the window end (earlier bars warm the labels up);
    ``returns`` has one value per window bar. Bar t's return (close t-1 to close t) is assigned
    to the regime known at close t-1. Returns ``{"basis", "regimes": {bear|sideways|bull:
    {"bars", "active_bars", "total_return"}}, "unlabelled_bars"}``; ``active_bars`` counts bars
    with a non-zero return (the strategy was exposed or paid costs). Raises ``ValueError`` if
    ``returns`` does not match the window's bar count, or ``window_days`` / ``threshold`` is not
    a positive integer / non-negative number.
    """
    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
        raise ValueError(f"window_days must be a positive integer, got {window_days!r}")
    if isinstance(threshold, bool) or not math.isfinite(threshold) or threshold < 0:
        raise ValueError(f"threshold must be a finite number >= 0, got {threshold!r}")
    hist = history.loc[history.index < window_end]
    in_window = np.asarray(hist.index >= window_start)
    r = np.asarray(returns, dtype=np.float64)
    if int(in_window.sum()) != r.shape[0]:
        raise ValueError(
            f"returns has {r.shape[0]} values but the window has {int(in_window.sum())} bars"
        )
    window_bars = max(1, round(window_days * 86_400 / interval_seconds(interval)))
    labels = regime_labels(hist["close"].astype(np.float64), window_bars, float(threshold))
    prev = np.empty_like(labels)
    prev[0] = -1
    prev[1:] = labels[:-1]
    prev = prev[in_window]
    regimes: dict[str, dict[str, Any]] = {}
    for state, name in _REGIME_NAMES.items():
        sel = r[prev == state]
        regimes[name] = {
            "bars": int(sel.size),
            "active_bars": int(np.count_nonzero(sel)),
            "total_return": num(np.prod(1.0 + sel) - 1.0) if sel.size else None,
        }
    return {
        "basis": (
            f"trailing {window_days}-day simple return ({window_bars} bars): above "
            f"+{threshold:.0%} bull, below -{threshold:.0%} bear, else sideways (regime "
            "definition from the run's frozen success criteria); bar t's return is assigned to "
            "the regime at close t-1"
        ),
        "regimes": regimes,
        "unlabelled_bars": int(np.count_nonzero(prev < 0)),
    }


# --------------------------------------------------------------------------- helpers


def frozen_criteria(ledger: Ledger, run_id: str) -> SuccessCriteria:
    """The criteria snapshotted when ``run_id`` started. ``ValueError`` if it never started."""
    run = ledger.research_run(run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} was never started; call start_run first")
    frozen = run["config"]["criteria"]
    names = {f.name for f in fields(SuccessCriteria)}
    if set(frozen) != names:
        raise ValueError(
            f"research run {run_id!r} froze criteria keys that do not match this lab's "
            f"SuccessCriteria (missing {sorted(names - set(frozen))}, unknown "
            f"{sorted(set(frozen) - names)}); start a new run"
        )
    return SuccessCriteria(**frozen)


def _latest(ledger: Ledger, run_id: str, plan_hash_: str, kind: str) -> dict[str, Any] | None:
    rows = ledger.evidence(run_id, plan_hash=plan_hash_, kind=kind)
    if not rows:
        return None
    payload = rows[-1]["payload"]
    return payload if isinstance(payload, dict) else None


def _get(obj: Any, *path: str | int) -> Any:
    for key in path:
        if isinstance(obj, Mapping) and isinstance(key, str):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return None
    return obj


def _row(
    name: str,
    status: CriterionStatus,
    value: float | bool | None,
    threshold: float | bool | None,
    note: str,
) -> CriterionRow:
    if isinstance(value, float) and not math.isfinite(value):
        value = None
    return CriterionRow(criterion=name, status=status, value=value, threshold=threshold, note=note)


def _missing(name: str, threshold: float | bool | None, note: str) -> CriterionRow:
    return _row(name, "missing", None, threshold, note)


def _fmt(x: float | None) -> str:
    return "n/a" if x is None or not math.isfinite(x) else f"{x:.3g}"


# --------------------------------------------------------------------------- rows


def _plan_logic(
    ledger: Ledger, run_id: str, plan_hash_: str, bt: dict[str, Any] | None
) -> str | None:
    """The plan's logic hash: from the backtest evidence's plan, else from any evidence row of
    ``plan_hash_`` that recorded one; None when neither exists."""
    plan_dict = _get(bt, "plan") if bt is not None else None
    if isinstance(plan_dict, Mapping):
        try:
            return logic_hash(plan_dict)
        except PlanValidationError:
            pass
    for row in ledger.evidence(run_id, plan_hash=plan_hash_):
        lh = _get(row["payload"], "logic_hash")
        if isinstance(lh, str) and lh:
            return lh
    return None


def _derivatives_plan(
    ledger: Ledger, run_id: str, plan_hash_: str, bt: dict[str, Any] | None
) -> Mapping[str, Any] | str | None:
    """The plan (dict, or plan JSON) when it reads a derivatives column, else None: its plan
    from the backtest evidence, else from any evidence row of ``plan_hash_`` in the run, else
    from its newest design trial. The snapshot check compares the series THIS plan reads."""
    candidates: list[Any] = [_get(bt, "plan") if bt is not None else None]
    candidates += [
        _get(row["payload"], "plan") for row in ledger.evidence(run_id, plan_hash=plan_hash_)
    ]
    for plan_dict in candidates:
        if isinstance(plan_dict, Mapping):
            return plan_dict if plan_derivative_columns(plan_dict) else None
    base = ledger.latest_research_base_trial(plan_hash_)
    if base is not None and plan_derivative_columns(base.plan_json):
        return base.plan_json
    return None


def _family(
    ledger: Ledger,
    run_id: str,
    plan_hash_: str,
    bt: dict[str, Any] | None,
    cfg: LabConfig,
    loader: CandleLoader | None = None,
) -> FamilyInfo:
    """The plan's behaviour family (:func:`lab.research.family.behaviour_family`), with the
    plan from the backtest evidence when it is there; else from the ledger. With no symbol
    known at all, the family is the plan's own logic (if any) and its own hash."""
    plan_dict = _get(bt, "plan") if bt is not None else None
    plan: Mapping[str, Any] | None = None
    symbol: str | None = None
    if isinstance(plan_dict, Mapping):
        try:
            parsed = parse_plan(plan_dict)
            plan, symbol = plan_dict, parsed.symbol
        except PlanValidationError:
            plan = None
    if symbol is None:
        base = ledger.latest_research_base_trial(plan_hash_)
        symbol = base.symbol if base is not None else None
    if symbol is None:
        lh = _plan_logic(ledger, run_id, plan_hash_, bt)
        return FamilyInfo(logic_hashes={lh} if lh else set(), plan_hashes={plan_hash_})
    return behaviour_family(
        ledger, symbol=symbol, plan_hash=plan_hash_, cfg=cfg, plan=plan, loader=loader
    )


def _wf_row(c: SuccessCriteria, wfs: list[dict[str, Any]], family: FamilyInfo) -> CriterionRow:
    name, thr = "wf_oos_sharpe_min", c.wf_oos_sharpe_min
    if not wfs:
        return _missing(name, thr, "no walk_forward evidence: run walk_forward")
    sharpes = [num(_get(wf, "oos_metrics", "sharpe")) for wf in wfs]
    basis = (
        f"minimum stitched out-of-sample net Sharpe over {len(wfs)} walk_forward result(s) of "
        f"this plan's behaviour family in every research run ({family.note()}; params tuned "
        "on each train window only; repeating walk_forward, here or in a new run, can only "
        "lower it)"
    )
    if any(v is None for v in sharpes):
        return _row(name, "fail", None, thr, f"{basis}: a stitched Sharpe is undefined")
    worst = min(v for v in sharpes if v is not None)
    return _row(name, "pass" if worst >= thr else "fail", worst, thr, basis)


def _trial(ledger: Ledger, bt: dict[str, Any], run_id: str, plan_hash_: str) -> TrialRecord | None:
    trial_id = _get(bt, "trial_id")
    if isinstance(trial_id, bool) or not isinstance(trial_id, int):
        return None
    try:
        rec = ledger.get(trial_id, include_returns=True)
    except ValueError:
        return None
    if rec.run_id != run_id or rec.plan_hash != plan_hash_ or rec.status != "ok":
        return None
    if rec.returns is None or rec.returns.size < 2:
        return None
    return rec


def _pooled_variance(ledger: Ledger, costs_json: str, interval: str) -> tuple[float, int, int]:
    """Cross-interval pooled ``V[SR]`` on ``interval``'s per-bar scale: the latest design-window
    Sharpe of every distinct plan (selection sources, every research run, the same costs, any
    interval and design window), each annualised with its own interval's 365-day bar count and
    brought back to ``interval``'s per-bar scale (``SR * sqrt(bpy(own) / bpy(interval))``).
    Returns ``(variance or NaN, plans pooled, intervals pooled)``."""
    rows = ledger.research_design_sharpes(sources=SELECTION_SOURCES, costs_json=costs_json)
    target = bars_per_year(interval)
    values = np.array(
        [sr * math.sqrt(bars_per_year(iv) / target) for _, iv, sr in rows], dtype=np.float64
    )
    values = values[np.isfinite(values)]
    variance = float(np.var(values, ddof=1)) if values.size >= 2 else math.nan
    return variance, int(values.size), len({iv for _, iv, _ in rows})


def _dsr_row(
    c: SuccessCriteria,
    ledger: Ledger,
    run_id: str,
    plan_hash_: str,
    bt: dict[str, Any] | None,
    cfg: LabConfig,
) -> tuple[CriterionRow, int | None]:
    name, thr = "dsr_min", c.dsr_min
    if bt is None:
        return _missing(name, thr, "no backtest evidence: run run_backtest"), None
    rec = _trial(ledger, bt, run_id, plan_hash_)
    reported_n = _get(bt, "stats", "dsr", "n_trials")
    if rec is None:
        return (
            _missing(
                name,
                thr,
                "the backtest's ledger trial (with returns) is not available in this run, so "
                "DSR cannot be re-deflated with the run's current trial count; rerun "
                "run_backtest",
            ),
            None,
        )
    n = max(1, ledger.count_distinct_plans_research(sources=SELECTION_SOURCES))
    scope: dict[str, Any] = {
        "interval": rec.interval,
        "costs_json": rec.costs_json,
        "window_start": rec.window_start,
        "window_end": rec.window_end,
    }
    run_sharpes = ledger.latest_sharpes_per_plan(run_id, **scope)
    all_sharpes = ledger.latest_sharpes_per_plan_research(**scope)
    run_v = ledger.selection_sharpe_variance(run_id, **scope)
    all_v = ledger.selection_sharpe_variance_research(**scope)
    pooled_v, n_pooled, n_intervals = _pooled_variance(ledger, rec.costs_json, rec.interval)
    finite = [v for v in (run_v, all_v, pooled_v) if math.isfinite(v)]
    variance = max(finite) if finite else math.nan
    need = cfg.research.min_variance_plans
    basis = (
        f"re-deflated now with N = {n} distinct plans tried in every run of the lab (research "
        f"runs and the web UI; all symbols and intervals; the report used N = {reported_n}) "
        f"and V[SR] {_fmt(variance)} = max(V over {len(run_sharpes)} commensurable trial "
        f"Sharpes in this run {_fmt(run_v)}, V over {len(all_sharpes)} in every research run "
        f"{_fmt(all_v)} (same interval, design window, and costs), V over {n_pooled} plans "
        f"pooled across {n_intervals} interval(s) {_fmt(pooled_v)} (every research run, same "
        "costs, any design window; each Sharpe annualised with 365-day bars of its interval and "
        f"rescaled to {rec.interval} bars)); latest Sharpe per distinct plan"
    )
    if n_pooled < need:
        return (
            _missing(
                name,
                thr,
                f"{basis}: only {n_pooled} commensurable plans, fewer than lab.toml [research] "
                f"min_variance_plans = {need}, so V[SR] is too noisy to deflate with; run more "
                "commensurable trials (a sweep) or treat as not significant",
            ),
            n,
        )
    if n > 1 and (not math.isfinite(variance) or variance <= _V_FLOOR):
        return (
            _missing(
                name,
                thr,
                f"{basis}: V[SR] is not estimable, so the DSR would equal the PSR (no "
                "deflation); run more commensurable trials or treat as not significant",
            ),
            n,
        )
    returns = rec.returns if rec.returns is not None else np.empty(0)
    dsr = num(deflated_sharpe(returns, n_trials=n, sharpe_variance=variance).dsr)
    if dsr is None:
        return _row(name, "fail", None, thr, f"{basis}; DSR undefined"), n
    return _row(name, "pass" if dsr >= thr else "fail", dsr, thr, basis), n


def _sweep_pbo(
    ledger: Ledger, sweeps: list[dict[str, Any]], cfg: LabConfig
) -> tuple[float | None, int, int, int]:
    """CSCV PBO over the design-window Optuna trials of ``sweeps`` (evidence rows with their
    ``run_id``; their study names), one matrix per distinct (run, sweep window); the MAX over
    those with at least ``cfg.research.min_pbo_trials`` usable trials where it is computable.
    Returns ``(pbo or None, trials used, studies, matrices too small)``."""
    by_window: dict[tuple[str, int, int], set[str]] = {}
    for row in sweeps:
        sw = row["payload"]
        start, end = _get(sw, "window", "start"), _get(sw, "window", "end")
        studies = _get(sw, "study")
        names = [studies] if isinstance(studies, str) else studies
        if not isinstance(start, int) or not isinstance(end, int) or not isinstance(names, list):
            continue
        by_window.setdefault((str(row["run_id"]), start, end), set()).update(str(n) for n in names)
    worst: float | None = None
    used = 0
    too_small = 0
    floor = max(2, cfg.research.min_pbo_trials)
    for (run_id, start, end), names in by_window.items():
        matrix, ids = ledger.returns_matrix(
            run_id,
            window_start=pd.Timestamp(start, unit="s", tz="UTC"),
            window_end=pd.Timestamp(end, unit="s", tz="UTC"),
            sources=("optuna",),
            studies=names,
        )
        if len(ids) < floor:
            too_small += 1
            continue
        n_splits = pbo_split_count(matrix.shape[0], cfg.stats.pbo_splits)
        if n_splits < 2 or not np.isfinite(matrix).all():
            continue
        value = num(pbo_cscv(matrix, n_splits=n_splits).pbo)
        if value is not None:
            worst = value if worst is None else max(worst, value)
            used += len(ids)
    return worst, used, sum(len(v) for v in by_window.values()), too_small


def _pbo_row(
    c: SuccessCriteria,
    ledger: Ledger,
    bt: dict[str, Any] | None,
    sweeps: list[dict[str, Any]],
    cfg: LabConfig,
) -> CriterionRow:
    name, thr = "pbo_max", c.pbo_max
    neigh = num(_get(bt, "stats", "pbo", "pbo")) if bt is not None else None
    n_neigh = _get(bt, "stats", "pbo", "n_trials") if bt is not None else None
    swept, n_swept, n_studies, too_small = _sweep_pbo(ledger, sweeps, cfg)
    floor = cfg.research.min_pbo_trials
    if swept is None:
        if too_small:
            return _missing(
                name,
                thr,
                f"sweep too small for PBO: {too_small} sweep matrix(es) of this plan's behaviour "
                f"family have fewer than lab.toml [research] min_pbo_trials = {floor} usable "
                "design-window Optuna trials (with fewer columns than CSCV splits the in-sample "
                f"'best' is close to a coin flip). run_sweep with n_trials >= {floor}",
            )
        return _missing(
            name,
            thr,
            f"PBO needs the sweep basis (CSCV over the design-window Optuna trials of this "
            f"plan's behaviour family; the ±20% neighbourhood alone measures stability, not "
            f"selection): {len(sweeps)} sweep(s) of the family with too few usable trials. "
            "run_sweep first",
        )
    bases = [
        f"sweep CSCV {_fmt(swept)} over {n_swept} design-window Optuna trials of "
        f"{n_studies} study(ies) of this behaviour family (every research run; matrices "
        f"with >= {floor} usable trials)"
    ]
    if too_small:
        bases[0] += f"; {too_small} smaller sweep matrix(es) ignored"
    if neigh is not None:
        bases.append(
            f"neighbourhood CSCV {_fmt(neigh)} over the base plan and {n_neigh} ±20% trials"
        )
    value = max(v for v in (neigh, swept) if v is not None)
    note = "max of " + "; ".join(bases)
    if neigh is None:
        note += " (neighbourhood basis not computable)"
    return _row(name, "pass" if value <= thr else "fail", value, thr, note)


def _beat_row(
    name: str, required: bool, bt: dict[str, Any] | None, bench: str, label: str
) -> CriterionRow:
    if not required:
        return _row(name, "pass", None, required, "not required by this run's criteria")
    if bt is None:
        return _missing(name, required, "no backtest evidence: run run_backtest")
    mine = num(_get(bt, "metrics", "net", "sharpe"))
    other = num(_get(bt, "benchmarks", bench, "sharpe"))
    if other is None:
        return _missing(name, required, f"{label} Sharpe is undefined on this window")
    if mine is None:
        return _row(name, "fail", False, required, f"net Sharpe undefined vs {label} {other:.2f}")
    ok = mine > other
    return _row(
        name,
        "pass" if ok else "fail",
        ok,
        required,
        f"net Sharpe {mine:.2f} vs {label} {other:.2f}",
    )


def _random_row(c: SuccessCriteria, bt: dict[str, Any] | None) -> CriterionRow:
    name, thr = "random_entry_percentile_min", c.random_entry_percentile_min
    if bt is None:
        return _missing(name, thr, "no backtest evidence: run run_backtest")
    paths = _get(bt, "benchmarks", "random_entry", "n_paths")
    if not paths:
        return _missing(name, thr, "the random-entry null was not run (0 paths)")
    pct = num(_get(bt, "benchmarks", "random_entry", "percentile"))
    if pct is None:
        return _row(name, "fail", None, thr, f"percentile undefined vs {paths} random paths")
    return _row(
        name,
        "pass" if pct >= thr else "fail",
        pct,
        thr,
        f"Sharpe percentile vs {paths} random-entry paths with the same trade count",
    )


def _cost_row(c: SuccessCriteria, adv: dict[str, Any] | None) -> CriterionRow:
    name, thr = "cost_stress_factor", c.cost_stress_factor
    if adv is None:
        return _missing(name, thr, "no adversarial evidence: run run_adversarial")
    if thr <= 1.0:
        row: Any = _get(adv, "base")
        label = "base costs"
    else:
        rows = _get(adv, "cost_stress", "rows") or []
        row = next(
            (
                r
                for r in rows
                if isinstance(r, Mapping)
                and num(r.get("factor")) is not None
                and math.isclose(float(r["factor"]), thr)
            ),
            None,
        )
        label = f"x{thr:g} costs"
        if row is None:
            factors = [r.get("factor") for r in rows if isinstance(r, Mapping)]
            return _missing(name, thr, f"x{thr:g} cost stress was not run (factors: {factors})")
    status = _get(row, "status")
    ret = num(_get(row, "total_return"))
    if status != "ok" or ret is None:
        return _row(name, "fail", ret, thr, f"run at {label} did not complete ({status})")
    return _row(name, "pass" if ret > 0 else "fail", ret, thr, f"net total return at {label}")


def _delay_row(c: SuccessCriteria, adv: dict[str, Any] | None) -> CriterionRow:
    name, required = "require_gradual_delay_decay", c.require_gradual_delay_decay
    if not required:
        return _row(name, "pass", None, required, "not required by this run's criteria")
    if adv is None:
        return _missing(name, required, "no adversarial evidence: run run_adversarial")
    base = num(_get(adv, "base", "sharpe"))
    rows = [r for r in (_get(adv, "delay", "rows") or []) if isinstance(r, Mapping)]
    delays = {int(r["delay"]): num(r.get("sharpe")) for r in rows if "delay" in r}
    if not delays:
        return _missing(name, required, "no signal-delay rows in the adversarial evidence")
    trail = ", ".join(f"{d} bars {_fmt(s)}" for d, s in sorted(delays.items()))
    note = f"Sharpe 0 bars {_fmt(base)}, {trail}"
    if base is None or base <= 0:
        return _row(name, "fail", False, required, f"no edge to decay ({note})")
    first, last = min(delays), max(delays)
    s_first, s_last = delays[first], delays[last]
    if s_first is None or s_first < DELAY_VANISH_RATIO * base:
        return _row(name, "fail", False, required, f"edge vanishes after {first} bar(s) ({note})")
    if last > first and s_last is not None and s_last >= DELAY_UNCHANGED_RATIO * base:
        return _row(
            name, "fail", False, required, f"suspicious: edge unchanged at {last} bars ({note})"
        )
    return _row(name, "pass", True, required, f"edge decays gradually ({note})")


def _dd_row(c: SuccessCriteria, bt: dict[str, Any] | None) -> CriterionRow:
    name, thr = "max_drawdown_max", c.max_drawdown_max
    if bt is None:
        return _missing(name, thr, "no backtest evidence: run run_backtest")
    dd = num(_get(bt, "metrics", "net", "max_drawdown"))
    if dd is None:
        return _row(name, "fail", None, thr, "max drawdown undefined")
    dd = abs(dd)
    return _row(
        name,
        "pass" if dd <= thr else "fail",
        dd,
        thr,
        "net max drawdown (fraction) on the design window",
    )


def _trades_row(c: SuccessCriteria, bt: dict[str, Any] | None) -> CriterionRow:
    name, thr = "min_trades", c.min_trades
    if bt is None:
        return _missing(name, thr, "no backtest evidence: run run_backtest")
    n = _get(bt, "metrics", "net", "n_trades")
    if isinstance(n, bool) or not isinstance(n, int | float):
        return _missing(name, thr, "trade count not in the backtest evidence")
    return _row(name, "pass" if n >= thr else "fail", float(n), thr, "trades on the design window")


def _regime_gate(plan_dict: Any) -> str | None:
    """Alias of a regime feature referenced in ``long_entry`` (an explicitly regime-gated
    plan), or None."""
    if not isinstance(plan_dict, Mapping):
        return None
    entry = str(plan_dict.get("long_entry", ""))
    feats = plan_dict.get("features")
    if not isinstance(feats, Mapping):
        return None
    for alias, spec in feats.items():
        if isinstance(spec, Mapping) and _REGIME_FEATURES & set(spec) and alias in _tokens(entry):
            return str(alias)
    return None


def _tokens(expr: str) -> list[str]:
    out: list[str] = []
    cur = ""
    for ch in expr:
        if ch.isalnum() or ch in "_.":
            cur += ch
        else:
            if cur:
                out.append(cur.split(".", 1)[0])
            cur = ""
    if cur:
        out.append(cur.split(".", 1)[0])
    return out


def _regime_row(c: SuccessCriteria, bt: dict[str, Any] | None) -> CriterionRow:
    name, thr = "min_profitable_regimes", c.min_profitable_regimes
    if thr == 0:
        return _row(name, "pass", None, thr, "not required by this run's criteria")
    if bt is None:
        return _missing(name, thr, "no backtest evidence: run run_backtest")
    block = _get(bt, "regimes", "regimes")
    if not isinstance(block, Mapping) or not block:
        return _missing(
            name,
            thr,
            "the backtest evidence has no regime breakdown (the lab report has none; "
            "run_backtest adds it)",
        )
    profitable = [k for k, v in block.items() if (num(_get(v, "total_return")) or 0.0) > 0.0]
    active = [k for k, v in block.items() if (_get(v, "active_bars") or 0) > 0]
    summary = ", ".join(
        f"{k} {_fmt(num(_get(v, 'total_return')))} ({_get(v, 'active_bars')} active bars)"
        for k, v in block.items()
    )
    gate = _regime_gate(_get(bt, "plan"))
    if gate is not None:
        ok = bool(active) and set(active) <= set(profitable)
        return _row(
            name,
            "pass" if ok else "fail",
            float(len(profitable)),
            thr,
            f"explicitly regime-gated (long_entry uses {gate!r}): every regime it trades in "
            f"must be profitable; {summary}. {_get(bt, 'regimes', 'basis')}",
        )
    return _row(
        name,
        "pass" if len(profitable) >= thr else "fail",
        float(len(profitable)),
        thr,
        f"{summary}. {_get(bt, 'regimes', 'basis')}",
    )


# No cap when listing the neighbourhood grid the criterion expects (the runner's cap is
# lab.toml neighbourhood_max_trials; a capped grid is reported as missing, never as a pass).
_UNCAPPED = 1_000_000


def _side_gaps(
    tunables: Mapping[str, Any], steps: tuple[float, ...]
) -> tuple[list[str], list[str]]:
    """``(missing, clipped)`` sides: for each tunable, the outermost step below and above 1
    must give a value distinct from the base. A side whose value only equals the base because
    it was clipped at a validator bound is ``clipped`` (covered, noted); one that rounds back
    to the integer base value is ``missing``."""
    missing: list[str] = []
    clipped: list[str] = []
    lows = [s for s in steps if s < 1.0]
    highs = [s for s in steps if s > 1.0]
    sides = [(label, fn(group)) for label, fn, group in (("-", min, lows), ("+", max, highs))]
    for key, spec in tunables.items():
        for label, step in sides:
            if step is None:
                continue
            raw = spec.default * step
            v = float(round(raw)) if spec.kind == "int" else float(raw)
            bounded = float(min(max(v, spec.low), spec.high))
            if bounded != spec.default:
                continue
            side = f"{key} {label}{abs(step - 1.0):.0%}"
            (clipped if bounded != v else missing).append(side)
    return missing, clipped


def _neighbourhood_row(
    c: SuccessCriteria,
    ledger: Ledger,
    run_id: str,
    plan_hash_: str,
    bt: dict[str, Any] | None,
    cfg: LabConfig,
) -> CriterionRow:
    name, pct = "neighbourhood_pct", c.neighbourhood_pct
    if pct == 0:
        return _row(name, "pass", None, pct, "not required by this run's criteria")
    if bt is None:
        return _missing(name, pct, "no backtest evidence: run run_backtest")
    try:
        plan = parse_plan(_get(bt, "plan") or {})
    except PlanValidationError:
        return _missing(name, pct, "the backtest evidence has no valid plan")
    tunables = tunable_params(plan)
    if not tunables:
        return _missing(name, pct, "plan has no tunable parameters; robustness is untestable")
    steps = tuple(s for s in cfg.stats.neighbourhood_steps if abs(s - 1.0) <= pct + 1e-9)
    reach = max((abs(s - 1.0) for s in cfg.stats.neighbourhood_steps), default=0.0)
    # a derivatives look-back never grows past lab.toml [derivatives] warmup_days: the runner
    # clips those neighbours there (lab.runner._run_neighbourhood), so the EXPECTED grid is
    # built with the same caps (expected == run); a side clipped by the cap is labelled as such
    capped = dict(tunables)
    for key, cap in derivatives_period_caps(plan, cfg).items():
        if key in capped:
            capped[key] = replace(capped[key], high=float(min(capped[key].high, cap)))
    lost, clipped = _side_gaps(capped, steps)
    _, bound_clipped = _side_gaps(tunables, steps)
    cap_clipped = [side for side in clipped if side not in bound_clipped]
    clipped = [side for side in clipped if side in bound_clipped]
    tunables = capped
    if lost:
        return _missing(
            name,
            pct,
            f"no distinct neighbour for {', '.join(lost)} (the step rounds back to the integer "
            "base value), so robustness on that side is not established",
        )
    expected: dict[str, dict[str, float]] = {}
    for ov in neighbourhood_overrides(tunables, steps, _UNCAPPED):
        try:
            expected[plan_hash(with_params(plan, ov))] = ov
        except PlanValidationError:
            continue
    start, end = _get(bt, "window", "start"), _get(bt, "window", "end")
    base = _trial(ledger, bt, run_id, plan_hash_)
    costs = base.costs_json if base is not None else costs_to_json(cfg.costs)
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
        cap = cfg.stats.neighbourhood_max_trials
        reasons = ["an older backtest, or trials run with other costs"]
        if cap - 1 < len(expected):
            reasons.insert(
                0, f"lab.toml neighbourhood_max_trials = {cap} caps the grid at {cap - 1}"
            )
        why = "; or ".join(reasons)
        return _missing(
            name,
            pct,
            f"only {len(found)} of {len(expected)} expected ±{pct:.0%} neighbours were run on "
            f"the design window with the same costs ({why}); robustness is not established",
        )
    profitable = 0
    for rec in found.values():
        ret = None
        if rec.status == "ok":
            ret = num(_metric(rec, "total_return"))
        profitable += int(ret is not None and ret > 0)
    share = profitable / len(found)
    note = (
        f"{profitable} of {len(found)} one-at-a-time ±{pct:.0%} neighbours profitable (net "
        f"total return > 0; every tunable has both sides)"
    )
    if clipped:
        note += f"; clipped at a validator bound (no neighbour beyond it): {', '.join(clipped)}"
    if cap_clipped:
        note += (
            "; clipped at the [derivatives] warmup_days look-back cap (no neighbour beyond it): "
            + ", ".join(cap_clipped)
        )
    if reach + 1e-9 < pct:
        note += f"; the lab's grid only reaches ±{reach:.0%}"
    literals = literal_vector(plan)
    if literals:
        note += "; rule literals (thresholds, lags) are NOT perturbed: " + ", ".join(
            f"{v:g}" for v in literals
        )
    return _row(name, "pass" if profitable == len(found) else "fail", share, pct, note)


def _metric(rec: TrialRecord, key: str) -> float | None:
    try:
        data = json.loads(rec.metrics_json)
    except json.JSONDecodeError:
        return None
    return num(data.get(key)) if isinstance(data, dict) else None


def _holdout_row(
    c: SuccessCriteria,
    ledger: Ledger,
    run_id: str,
    plan_hash_: str,
    family: FamilyInfo,
) -> CriterionRow:
    name, required = "require_holdout_pass", c.require_holdout_pass
    if not required:
        return _row(name, "pass", None, required, "not required by this run's criteria")
    own = _latest(ledger, run_id, plan_hash_, "holdout")
    if own is not None and isinstance(own.get("passed"), bool):
        ok = bool(own["passed"])
        return _row(name, "pass" if ok else "fail", ok, required, f"locked holdout: passed = {ok}")
    conflict = family_holdout_conflict(ledger, family)
    if conflict is not None:
        return _row(
            name,
            "fail",
            False,
            required,
            f"{conflict}: the hypothesis's single holdout check is used; this variant can never "
            "be holdout-checked",
        )
    return _missing(name, required, "no locked holdout evidence yet")


def _crosscheck_row(c: SuccessCriteria, cross: dict[str, Any] | None) -> CriterionRow:
    name, required = "require_crosscheck_match", c.require_crosscheck_match
    if not required:
        return _row(name, "pass", None, required, "not required by this run's criteria")
    if cross is None or not isinstance(cross.get("matched"), bool):
        return _missing(
            name,
            required,
            "no event-driven cross-check evidence yet: run_crosscheck (a holdout check needs a "
            "matched cross-check first)",
        )
    if cross.get("available") is False:
        return _missing(
            name,
            required,
            f"event-driven cross-check unavailable: {cross.get('reason')}; the holdout stays "
            "refused until the engine (nautilus_trader) is installed and the cross-check matches",
        )
    ok = bool(cross["matched"])
    detail = (
        f"{cross.get('matched_trades')} of {cross.get('n_trades_vectorbt')} vectorbt trades "
        f"matched by {cross.get('n_trades_event')} event-driven trades, return diff "
        f"{_fmt(num(cross.get('return_diff_pct')))}%"
    )
    return _row(
        name,
        "pass" if ok else "fail",
        ok,
        required,
        f"event-driven cross-check: matched = {ok}; {detail}",
    )


# --------------------------------------------------------------------------- public API


def evaluate_plan(
    ledger: Ledger,
    run_id: str,
    plan_hash: str,
    cfg: LabConfig,
    *,
    loader: CandleLoader | None = None,
) -> CriteriaReport:
    """Judge ``plan_hash`` in research run ``run_id`` against the run's frozen criteria (module
    docstring; a portfolio plan is judged by
    :func:`lab.research.portfolio_criteria.evaluate_portfolio_plan`, which adds its rows).
    ``loader`` rebuilds bar times when the behaviour family compares trials across intervals
    or windows (default: the lab's parquet loader). Raises ``ValueError`` if the run was never
    started or its frozen lab.toml fingerprint is missing or differs from ``cfg``, and, for a
    plan that uses a derivatives feature, when the run froze no derivatives snapshot or it
    differs from the archive on disk (:func:`lab.research.runs.check_derivatives_snapshot`)."""
    c = frozen_criteria(ledger, run_id)
    check_lab_fingerprint(ledger, run_id, cfg)
    bt = _latest(ledger, run_id, plan_hash, "backtest")
    from lab.research import portfolio_criteria as pc  # lazy: it imports this module

    if pc.is_portfolio_evidence(ledger, run_id, plan_hash, bt):
        return pc.evaluate_portfolio_plan(ledger, run_id, plan_hash, cfg, c, loader=loader)
    drv_plan = _derivatives_plan(ledger, run_id, plan_hash, bt)
    if drv_plan is not None:
        # the run's frozen snapshot of the series this plan reads must still be the data on
        # disk (see runs.py)
        check_derivatives_snapshot(ledger, run_id, cfg, drv_plan)
    adv = _latest(ledger, run_id, plan_hash, "adversarial")
    cross = _latest(ledger, run_id, plan_hash, "crosscheck")
    family = _family(ledger, run_id, plan_hash, bt, cfg, loader)
    wfs = [row["payload"] for row in family_evidence(ledger, family, "walk_forward")]
    sweeps = family_evidence(ledger, family, "sweep")

    dsr_row, _ = _dsr_row(c, ledger, run_id, plan_hash, bt, cfg)
    by_name: dict[str, CriterionRow] = {
        "wf_oos_sharpe_min": _wf_row(c, wfs, family),
        "dsr_min": dsr_row,
        "pbo_max": _pbo_row(c, ledger, bt, sweeps, cfg),
        "beat_buy_hold": _beat_row(
            "beat_buy_hold", c.beat_buy_hold, bt, "buy_and_hold", "buy-and-hold"
        ),
        "beat_vol_target": _beat_row(
            "beat_vol_target", c.beat_vol_target, bt, "vol_target", "vol-targeted buy-and-hold"
        ),
        "random_entry_percentile_min": _random_row(c, bt),
        "cost_stress_factor": _cost_row(c, adv),
        "require_gradual_delay_decay": _delay_row(c, adv),
        "max_drawdown_max": _dd_row(c, bt),
        "min_trades": _trades_row(c, bt),
        "min_profitable_regimes": _regime_row(c, bt),
        "neighbourhood_pct": _neighbourhood_row(c, ledger, run_id, plan_hash, bt, cfg),
        "require_holdout_pass": _holdout_row(c, ledger, run_id, plan_hash, family),
        "require_crosscheck_match": _crosscheck_row(c, cross),
    }
    rows = [by_name[f.name] for f in fields(SuccessCriteria) if f.name not in CRITERION_PARAMS]
    out = CriteriaReport(
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
    ungated = ungated_baseline(bt)
    if ungated is not None:  # derivatives plans only; never a row, never pass/fail
        out["informational"] = {"ungated_baseline": ungated}
    return out


_BASELINE_KEYS = ("sharpe", "total_return", "max_drawdown", "n_trades", "exposure")


def ungated_baseline(bt: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The backtest evidence's ``benchmarks.ungated`` block (:func:`lab.runner.ungated_plan`)
    as an informational comparison: the plan's and the ungated baseline's net Sharpe, total
    return, max drawdown, trades and exposure side by side, the gate's binding share and the
    plan-minus-ungated differences. None when the evidence has no such block (price-only
    plans). It is never a criterion row and never changes pass/fail."""
    block = _get(bt, "benchmarks", "ungated")
    if not isinstance(block, Mapping):
        return None
    note = (
        "informational, NOT a success criterion (the run's frozen criteria are unchanged): "
        "the same plan without its derivatives entry clauses, same costs and scored window; "
        "not a ledger trial, not a candidate, never holdout-checkable"
    )
    if "error" in block:
        return {"error": block["error"], "note": note}
    net = _get(bt, "metrics", "net")
    ungated = block.get("metrics")
    return {
        "price_clause": block.get("price_clause"),
        "gate_features": block.get("gate_features"),
        "plan": {k: _get(net, k) for k in _BASELINE_KEYS},
        "ungated": {k: _get(ungated, k) for k in _BASELINE_KEYS},
        "gate_binding_share": block.get("gate_binding_share"),
        "gate_binding_bars": block.get("gate_binding_bars"),
        "price_clause_entry_bars": block.get("price_clause_entry_bars"),
        "plan_minus_ungated": block.get("plan_minus_ungated"),
        "note": note,
    }
