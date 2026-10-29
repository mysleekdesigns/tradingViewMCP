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
returns are in the ledger) with the NOMINAL ``N`` (the run's frozen ``dsr_trial_count``, which
can only be "nominal"; a frozen "effective" fails closed): distinct plans tried in EVERY run of
the ledger (research runs AND the web UI's Strategy Tester / ad-hoc API runs; all symbols and
intervals; ``Ledger.count_distinct_plans_research``, a new run never resets the bar). The
participation-ratio effective N is never the DSR's N (:mod:`lab.stats.effective_n`); and
``V[SR]`` = the
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

Statistics (information only, never a row, never pass/fail): the report's ``statistics`` block
(:func:`plan_statistics`) shows the nominal N the DSR used beside the effective-N DIAGNOSTIC
and its time-shuffled null (globally and for this run; computed lazily, and a failure there is
reported as missing and never breaks the report), the minimum backtest length (MinBTL, for the
DSR's N at the plan's observed design-window net Sharpe), the minimum track record length
(MinTRL, from the PSR at the DSR's deflated benchmark SR0 and confidence ``dsr_min``; a DSR
pass implies at least MinTRL bars), and a CPCV out-of-sample Sharpe path distribution over the
behaviour family's design-window sweep trials (read from the ledger: no new backtests), twice:
``cpcv`` over every trial (an upper bound: TPE chose them with the whole design window, every
CPCV test bar included) and ``cpcv_startup`` over only the sampler's random start-up trials.
When ``dsr_min`` does not pass and the design window is below MinTRL or MinBTL, its note
states "design window too short".

Risk-overlay runs (PLAN.md Phase 15) are judged by :mod:`lab.research.overlay_criteria`, which
reuses the holdout and cross-check rows here (:func:`holdout_row`, :func:`crosscheck_row`) and
adds the ``"not_testable"`` status (never a pass). Their trials COUNT in this module's N like any
other trial (owner decision 2026-10-29, "count in N, not V[SR]"), but never enter its V[SR]
pools (:mod:`lab.ledger` excludes risk-overlay runs from every research-wide Sharpe pool).

Results are historical and not forward-looking.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import MISSING, asdict, dataclass, fields, replace
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
from lab.research.contract import DSR_EFFECTIVE_REFUSED, DSR_TRIAL_COUNTS, SuccessCriteria
from lab.research.family import (
    FamilyInfo,
    behaviour_family,
    family_evidence,
    family_holdout_conflict,
)
from lab.research.logic import literal_vector, logic_hash
from lab.research.runs import budget_status, check_derivatives_snapshot, check_lab_fingerprint
from lab.research.trial_count import TRIAL_COUNT_NULL_SEED, TrialCount, trial_counts
from lab.runner import (
    CandleLoader,
    derivatives_period_caps,
    neighbourhood_overrides,
    pbo_split_count,
)
from lab.stats import DSRResult, deflated_sharpe, pbo_cscv, sharpe_per_bar
from lab.stats.cpcv import cpcv_paths
from lab.stats.sharpe import min_backtest_length, min_track_record_length
from lab.types import TrialRecord, bars_per_year, interval_seconds

__all__ = [
    "CRITERION_PARAMS",
    "PRE_HOLDOUT_EXCLUDED",
    "CriteriaReport",
    "CriterionRow",
    "CriterionStatus",
    "criterion_row",
    "crosscheck_row",
    "evaluate_plan",
    "frozen_criteria",
    "holdout_row",
    "latest_evidence",
    "plan_derivatives_dict",
    "plan_family",
    "plan_statistics",
    "regime_breakdown",
]

# "not_testable" is used only by the risk-overlay rows (lab.research.overlay_criteria): the
# evidence exists but cannot answer the question (too few drawdown episodes). It is never a pass.
CriterionStatus = Literal["pass", "fail", "missing", "not_testable"]

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
# ``min_profitable_regimes``, and ``dsr_trial_count`` (which N ``dsr_min`` deflates with). They
# are frozen with the rest of the criteria when a run starts.
CRITERION_PARAMS: tuple[str, ...] = ("regime_window_days", "regime_threshold", "dsr_trial_count")

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
    # information only, never pass/fail (:func:`plan_statistics`): nominal N, the effective-N
    # diagnostic and its null, MinBTL, MinTRL and the CPCV path distributions
    statistics: NotRequired[dict[str, Any]]


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
    """The criteria snapshotted when ``run_id`` started. ``ValueError`` if it never started,
    or if the snapshot's keys do not fit :class:`SuccessCriteria`: an unknown key, or a missing
    key that has no default. A missing key WITH a default (``dsr_trial_count``, added after the
    first runs) reads as its default, so a run frozen before it existed stays on "nominal"."""
    run = ledger.research_run(run_id)
    if run is None:
        raise ValueError(f"research run {run_id!r} was never started; call start_run first")
    frozen = run["config"]["criteria"]
    names = {f.name for f in fields(SuccessCriteria)}
    required = {f.name for f in fields(SuccessCriteria) if f.default is MISSING}
    missing, unknown = sorted(required - set(frozen)), sorted(set(frozen) - names)
    if missing or unknown:
        raise ValueError(
            f"research run {run_id!r} froze criteria keys that do not match this lab's "
            f"SuccessCriteria (missing {missing}, unknown {unknown}); start a new run"
        )
    choice = frozen.get("dsr_trial_count", "nominal")
    if choice == "effective":
        raise ValueError(f"research run {run_id!r} froze {DSR_EFFECTIVE_REFUSED}; start a new run")
    if choice not in DSR_TRIAL_COUNTS:
        raise ValueError(
            f"research run {run_id!r} froze dsr_trial_count = {choice!r}, not one of "
            f"{list(DSR_TRIAL_COUNTS)}; start a new run"
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


def criterion_row(
    name: str,
    status: CriterionStatus,
    value: float | bool | None,
    threshold: float | bool | None,
    note: str,
) -> CriterionRow:
    """A :class:`CriterionRow` (a non-finite float value becomes None)."""
    return _row(name, status, value, threshold, note)


def latest_evidence(
    ledger: Ledger, run_id: str, plan_hash_: str, kind: str
) -> dict[str, Any] | None:
    """The newest evidence payload of ``kind`` for ``plan_hash_`` in ``run_id`` (or None)."""
    return _latest(ledger, run_id, plan_hash_, kind)


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


def plan_derivatives_dict(
    ledger: Ledger, run_id: str, plan_hash_: str, bt: dict[str, Any] | None
) -> Mapping[str, Any] | str | None:
    """The plan when it reads a derivatives column (its snapshot must be checked), else None."""
    return _derivatives_plan(ledger, run_id, plan_hash_, bt)


def plan_family(
    ledger: Ledger,
    run_id: str,
    plan_hash_: str,
    bt: dict[str, Any] | None,
    cfg: LabConfig,
    loader: CandleLoader | None = None,
) -> FamilyInfo:
    """The plan's behaviour family (any run, either research question)."""
    return _family(ledger, run_id, plan_hash_, bt, cfg, loader)


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


@dataclass(frozen=True)
class TrialN:
    """The DSR trial count for one ``get_report`` call (:func:`trial_n`): the nominal count of
    distinct plans tried in every run of the lab (``nominal``; ``n`` is it floored at 1). It
    never depends on the effective-N diagnostic, which the statistics block computes lazily.
    ``choice`` is the run's frozen ``dsr_trial_count`` (always "nominal")."""

    choice: str
    n: int
    nominal: int


def trial_n(c: SuccessCriteria, ledger: Ledger) -> TrialN:
    """The DSR's N: the nominal distinct-plan count over every run (:class:`TrialN`)."""
    nominal = ledger.count_distinct_plans_research(sources=SELECTION_SOURCES)
    return TrialN(c.dsr_trial_count, max(1, nominal), nominal)


def _n_basis(tn: TrialN) -> str:
    """How the DSR's N was chosen, for the dsr_min note."""
    return (
        f"N = {tn.n} distinct plans tried in every run of the lab (research runs and the web "
        "UI; all symbols and intervals; the run's frozen dsr_trial_count = "
        f'"{tn.choice}"; the effective-N diagnostic in statistics is never the DSR\'s N)'
    )


@dataclass(frozen=True)
class DsrEval:
    """The ``dsr_min`` row plus what the statistics block needs from it: the N it deflated
    with, the design trial (with returns) and the DSR result (its deflated benchmark SR0)."""

    row: CriterionRow
    n: int | None
    rec: TrialRecord | None
    result: DSRResult | None


def _dsr_eval(
    c: SuccessCriteria,
    ledger: Ledger,
    run_id: str,
    plan_hash_: str,
    bt: dict[str, Any] | None,
    cfg: LabConfig,
    tn: TrialN,
) -> DsrEval:
    name, thr = "dsr_min", c.dsr_min
    if bt is None:
        row = _missing(name, thr, "no backtest evidence: run run_backtest")
        return DsrEval(row, None, None, None)
    rec = _trial(ledger, bt, run_id, plan_hash_)
    reported_n = _get(bt, "stats", "dsr", "n_trials")
    if rec is None:
        row = _missing(
            name,
            thr,
            "the backtest's ledger trial (with returns) is not available in this run, so "
            "DSR cannot be re-deflated with the run's current trial count; rerun "
            "run_backtest",
        )
        return DsrEval(row, None, None, None)
    n = tn.n
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
        f"re-deflated now with {_n_basis(tn)}; the report used N = {reported_n}; "
        f"V[SR] {_fmt(variance)} = max(V over {len(run_sharpes)} commensurable trial "
        f"Sharpes in this run {_fmt(run_v)}, V over {len(all_sharpes)} in every research run "
        f"{_fmt(all_v)} (same interval, design window, and costs), V over {n_pooled} plans "
        f"pooled across {n_intervals} interval(s) {_fmt(pooled_v)} (every research run, same "
        "costs, any design window; each Sharpe annualised with 365-day bars of its interval and "
        f"rescaled to {rec.interval} bars)); latest Sharpe per distinct plan"
    )
    if n_pooled < need:
        row = _missing(
            name,
            thr,
            f"{basis}: only {n_pooled} commensurable plans, fewer than lab.toml [research] "
            f"min_variance_plans = {need}, so V[SR] is too noisy to deflate with; run more "
            "commensurable trials (a sweep) or treat as not significant",
        )
        return DsrEval(row, n, rec, None)
    if n > 1 and (not math.isfinite(variance) or variance <= _V_FLOOR):
        row = _missing(
            name,
            thr,
            f"{basis}: V[SR] is not estimable, so the DSR would equal the PSR (no "
            "deflation); run more commensurable trials or treat as not significant",
        )
        return DsrEval(row, n, rec, None)
    returns = rec.returns if rec.returns is not None else np.empty(0)
    result = deflated_sharpe(returns, n_trials=n, sharpe_variance=variance)
    dsr = num(result.dsr)
    if dsr is None:
        return DsrEval(_row(name, "fail", None, thr, f"{basis}; DSR undefined"), n, rec, result)
    row = _row(name, "pass" if dsr >= thr else "fail", dsr, thr, basis)
    return DsrEval(row, n, rec, result)


def _dsr_row(
    c: SuccessCriteria,
    ledger: Ledger,
    run_id: str,
    plan_hash_: str,
    bt: dict[str, Any] | None,
    cfg: LabConfig,
    tn: TrialN | None = None,
) -> tuple[CriterionRow, int | None]:
    """The ``dsr_min`` row and the N it deflated with (``tn`` default: :func:`trial_n`)."""
    tn = tn if tn is not None else trial_n(c, ledger)
    out = _dsr_eval(c, ledger, run_id, plan_hash_, bt, cfg, tn)
    return out.row, out.n


@dataclass(frozen=True)
class SweepMatrix:
    """Design-window Optuna trial returns of one (run, sweep window) of a behaviour family."""

    run_id: str
    start: int
    end: int
    studies: frozenset[str]
    matrix: np.ndarray  # T x M per-bar returns, columns in ledger id order
    ids: tuple[int, ...]


def sweep_matrices(ledger: Ledger, sweeps: list[dict[str, Any]]) -> list[SweepMatrix]:
    """One returns matrix per distinct (run, sweep window) of ``sweeps`` (evidence rows with
    their ``run_id``; their study names): the trials CSCV PBO and CPCV are computed on. Read
    once per report and shared by both."""
    by_window: dict[tuple[str, int, int], set[str]] = {}
    for row in sweeps:
        sw = row["payload"]
        start, end = _get(sw, "window", "start"), _get(sw, "window", "end")
        studies = _get(sw, "study")
        names = [studies] if isinstance(studies, str) else studies
        if not isinstance(start, int) or not isinstance(end, int) or not isinstance(names, list):
            continue
        by_window.setdefault((str(row["run_id"]), start, end), set()).update(str(n) for n in names)
    out: list[SweepMatrix] = []
    for (run_id, start, end), names in by_window.items():
        matrix, ids = ledger.returns_matrix(
            run_id,
            window_start=pd.Timestamp(start, unit="s", tz="UTC"),
            window_end=pd.Timestamp(end, unit="s", tz="UTC"),
            sources=("optuna",),
            studies=names,
        )
        out.append(SweepMatrix(run_id, start, end, frozenset(names), matrix, tuple(ids)))
    return out


def _sweep_pbo(
    ledger: Ledger,
    sweeps: list[dict[str, Any]],
    cfg: LabConfig,
    matrices: list[SweepMatrix] | None = None,
) -> tuple[float | None, int, int, int]:
    """CSCV PBO over the design-window Optuna trials of ``sweeps`` (evidence rows with their
    ``run_id``; their study names), one matrix per distinct (run, sweep window); the MAX over
    those with at least ``cfg.research.min_pbo_trials`` usable trials where it is computable.
    ``matrices`` are :func:`sweep_matrices` of ``sweeps`` when the caller already read them.
    Returns ``(pbo or None, trials used, studies, matrices too small)``."""
    mats = matrices if matrices is not None else sweep_matrices(ledger, sweeps)
    worst: float | None = None
    used = 0
    too_small = 0
    floor = max(2, cfg.research.min_pbo_trials)
    for m in mats:
        if len(m.ids) < floor:
            too_small += 1
            continue
        n_splits = pbo_split_count(m.matrix.shape[0], cfg.stats.pbo_splits)
        if n_splits < 2 or not np.isfinite(m.matrix).all():
            continue
        value = num(pbo_cscv(m.matrix, n_splits=n_splits).pbo)
        if value is not None:
            worst = value if worst is None else max(worst, value)
            used += len(m.ids)
    return worst, used, sum(len(m.studies) for m in mats), too_small


def _pbo_row(
    c: SuccessCriteria,
    ledger: Ledger,
    bt: dict[str, Any] | None,
    sweeps: list[dict[str, Any]],
    cfg: LabConfig,
    matrices: list[SweepMatrix] | None = None,
) -> CriterionRow:
    name, thr = "pbo_max", c.pbo_max
    neigh = num(_get(bt, "stats", "pbo", "pbo")) if bt is not None else None
    n_neigh = _get(bt, "stats", "pbo", "n_trials") if bt is not None else None
    swept, n_swept, n_studies, too_small = _sweep_pbo(ledger, sweeps, cfg, matrices)
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
    return holdout_row(c.require_holdout_pass, ledger, run_id, plan_hash_, family)


def holdout_row(
    required: bool,
    ledger: Ledger,
    run_id: str,
    plan_hash_: str,
    family: FamilyInfo,
) -> CriterionRow:
    """The ``require_holdout_pass`` row for either research question: the plan's own holdout
    evidence in the run, else "fail" when a member of its behaviour family (any run, either
    question) already used the single holdout check, else "missing"."""
    name = "require_holdout_pass"
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
    return crosscheck_row(c.require_crosscheck_match, cross)


def crosscheck_row(required: bool, cross: Mapping[str, Any] | None) -> CriterionRow:
    """The ``require_crosscheck_match`` row for either research question (unavailable engine
    -> missing, matched -> pass, else fail)."""
    name = "require_crosscheck_match"
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


# --------------------------------------------------------------------------- statistics


def _finite(x: float) -> float | None:
    return float(x) if math.isfinite(x) else None


def _jsonable(value: Any) -> Any:
    """Tuples to lists and non-finite floats to None, so the block serialises as plain JSON."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, float | np.floating):
        return _finite(float(value))
    return value


_DIAGNOSTIC_BASIS = (
    "Diagnostic only, NOT used for the DSR (which deflates with the nominal N above): the "
    "participation ratio of the plans' return correlations is dominated by the one market "
    "factor long-only crypto plans share, which V[SR] already reflects, so as N it would count "
    "that factor twice; it falls when near-copies are added, and with M plans over T days it is "
    "biased down (Marchenko-Pastur). effective_null is the same ratio after shuffling each "
    "plan's daily returns in time within its own observed days (seed {seed}): what M "
    "independent plans on these days would score. Read effective_diagnostic against "
    "effective_null, not against nominal. Method: {method}"
)


def _trial_count_block(tn: TrialN, ledger: Ledger, run_id: str, cfg: LabConfig) -> dict[str, Any]:
    """Nominal N (the DSR's) plus the effective-N diagnostic, computed here and only here. Any
    failure in the diagnostic (candles, sqlite, zlib, memory) is reported as ``missing`` and
    never reaches the nominal N, the dsr_min row or a holdout decision."""
    out: dict[str, Any] = {
        "nominal": tn.nominal,
        "run_nominal": ledger.count_distinct_plans(run_id, sources=SELECTION_SOURCES),
        "used_for_dsr": "nominal",
        "dsr_n": tn.n,
        "frozen_choice": tn.choice,
    }
    try:
        g: TrialCount = trial_counts(ledger, cfg)
        r: TrialCount = trial_counts(ledger, cfg, run_id=run_id)
    except Exception as exc:  # a diagnostic never breaks get_report or holdout_check
        out["missing"] = (
            f"effective-N diagnostic not computable: {type(exc).__name__}: {exc} (the DSR's "
            "nominal N above is unaffected)"
        )
        return out
    out.update(
        {
            "effective_diagnostic": _finite(g.effective),
            "effective_null": _finite(g.effective_null),
            "plans": g.n_with_returns,
            "rows": g.rows,
            "n_without_returns": g.n_without_returns,
            "run_effective_diagnostic": _finite(r.effective),
            "run_effective_null": _finite(r.effective_null),
            "run_plans": r.n_with_returns,
            "run_rows": r.rows,
            "basis": _DIAGNOSTIC_BASIS.format(
                seed=TRIAL_COUNT_NULL_SEED, method=g.basis.rstrip(". ") + ". Information only."
            ),
        }
    )
    return out


def _min_btl_block(dsr: DsrEval, tn: TrialN) -> dict[str, Any]:
    rec = dsr.rec
    if rec is None or rec.returns is None:
        return {"missing": "no design-window backtest trial with returns in this run: run_backtest"}
    bpy = bars_per_year(rec.interval)
    target = sharpe_per_bar(rec.returns) * math.sqrt(bpy)
    if not math.isfinite(target):
        return {"missing": "the design-window net Sharpe is undefined (no return variance)"}
    years = min_backtest_length(tn.n, target)
    design_bars = int(rec.returns.size)
    design_years = design_bars / bpy
    basis = (
        f"MinBTL = (E[max Z] of N = {tn.n} trials / annualised Sharpe)^2 years (Bailey, Borwein, "
        "Lopez de Prado & Zhu 2014): below it, the plan's observed design-window net Sharpe "
        f"{target:.3g} ({rec.interval}, 365-day year of {bpy:g} bars) is within what selecting "
        f"the best of N zero-skill trials produces. Design window {design_bars} bars = "
        f"{design_years:.3g} years"
    )
    if not math.isfinite(years):
        basis += "; infinite because the observed Sharpe is not positive"
    return {
        "years": _finite(years),
        "bars": _finite(years * bpy),
        "design_bars": design_bars,
        "design_years": design_years,
        "n_trials": tn.n,
        "target_sharpe_annual": target,
        "ok": bool(design_years >= years),
        "basis": basis + ". Information only.",
    }


def _min_trl_block(c: SuccessCriteria, dsr: DsrEval) -> dict[str, Any]:
    if dsr.rec is None or dsr.rec.returns is None:
        return {"missing": "no design-window backtest trial with returns in this run: run_backtest"}
    if dsr.result is None:
        return {
            "missing": "the dsr_min row has no deflated benchmark SR0 (it is missing: see its "
            "note), so MinTRL at that benchmark is not computable"
        }
    conf = c.dsr_min
    if not 0.0 < conf < 1.0:
        return {
            "missing": f"confidence = the frozen dsr_min {conf:g} is not strictly between 0 "
            "and 1, so MinTRL is not defined"
        }
    sr0 = dsr.result.sr0_per_bar
    bars = min_track_record_length(dsr.rec.returns, sr0, conf)
    if math.isnan(bars):
        return {"missing": "MinTRL undefined (non-finite moments or non-positive variance term)"}
    design_bars = int(dsr.rec.returns.size)
    basis = (
        "MinTRL = bars needed before the Probabilistic Sharpe Ratio against the DSR's deflated "
        f"benchmark SR0 = {sr0:.3g} per bar reaches the frozen dsr_min = {conf:g} (Bailey & "
        "Lopez de Prado 2012 eq. 13, with the plan's skew and kurtosis); with at least MinTRL "
        "bars the DSR reaches dsr_min (the same inversion)"
    )
    if not math.isfinite(bars):
        basis += "; infinite because the per-bar Sharpe is not above SR0"
    return {
        "bars": _finite(bars),
        "design_bars": design_bars,
        "target_sharpe_per_bar": sr0,
        "confidence": conf,
        "ok": bool(design_bars >= bars),
        "basis": basis + ". Information only.",
    }


# The ``cpcv`` block's label: its configurations were proposed by TPE, which saw every
# design-window bar (every CPCV test bar included) through the objective.
CPCV_UPPER_BOUND_BASIS = "upper bound: candidates were chosen by a search that saw every test bar"


def _sweep_for_cpcv(
    bt: dict[str, Any] | None, matrices: list[SweepMatrix]
) -> tuple[SweepMatrix | None, str | None]:
    """The sweep matrix CPCV is read from (the backtest's window first, then the largest), or
    ``(None, reason)``."""
    if not matrices:
        return None, (
            "no design-window sweep of this plan's behaviour family: run_sweep "
            "(CPCV reads its Optuna trials; it never runs new backtests)"
        )
    usable = [m for m in matrices if len(m.ids) >= 2 and np.isfinite(m.matrix).all()]
    if not usable:
        return None, (
            f"{len(matrices)} sweep matrix(es) of this plan's behaviour family, none "
            "with at least 2 usable design-window Optuna trials with finite returns"
        )
    start, end = _get(bt, "window", "start"), _get(bt, "window", "end")
    m = max(usable, key=lambda x: (x.start == start and x.end == end, len(x.ids), x.end, x.run_id))
    return m, None


_NO_NUMBER = object()  # sentinel: a row written before the trial number was recorded


def _startup_trial_ids(ledger: Ledger, m: SweepMatrix) -> tuple[frozenset[int], set[str]] | str:
    """Ledger ids of ``m``'s studies' trials that TPE drew at random: Optuna trial number below
    ``TPE_SAMPLER_SETTINGS["n_startup_trials"]`` within each study. A study whose rows all
    carry ``params_json["_study"]["trial"]`` (the recorded ``trial.number``) is read directly.
    A study whose rows carry none (written before the number was recorded) falls back to
    position: its rows on the sweep window (ok AND error: the lab records exactly one row per
    trial, in trial order, ``study.optimize`` running sequentially), restarting every
    ``n_trials`` rows (a repeated sweep reuses the study name). A study mixing both is refused.
    Returns ``(ids, methods)`` (``methods``: which of ``"recorded"`` / ``"ledger order"``
    numbered the studies), or the reason the numbers cannot be identified."""
    from lab.search.optuna_search import (  # lazy: imports optuna
        STUDY_KEY,
        TPE_SAMPLER_SETTINGS,
        TRIAL_NUMBER_KEY,
    )

    n_startup = int(TPE_SAMPLER_SETTINGS["n_startup_trials"])
    rows: dict[str, list[tuple[int, Any, Any]]] = {}
    for rec in ledger.list(
        run_id=m.run_id, limit=ledger.count(m.run_id), exclude_window_kinds=("holdout",)
    ):
        if rec.source != "optuna" or rec.id is None:
            continue
        if (int(rec.window_start.timestamp()), int(rec.window_end.timestamp())) != (m.start, m.end):
            continue
        try:
            params = json.loads(rec.params_json)
        except ValueError:
            continue
        meta = params.get(STUDY_KEY) if isinstance(params, dict) else None
        if isinstance(meta, dict) and meta.get("study") in m.studies:
            rows.setdefault(str(meta["study"]), []).append(
                (rec.id, meta.get("n_trials"), meta.get(TRIAL_NUMBER_KEY, _NO_NUMBER))
            )
    ids: set[int] = set()
    methods: set[str] = set()
    for name in sorted(m.studies):
        study_rows = sorted(rows.get(name, []))
        if not study_rows:
            return f"study {name} has no Optuna ledger rows on the sweep window"
        recorded = [num for _, _, num in study_rows if num is not _NO_NUMBER]
        if recorded:
            if len(recorded) != len(study_rows):
                return (
                    f"study {name} mixes ledger rows with and without a recorded Optuna trial "
                    "number, so the random start-up trials cannot be identified"
                )
            for trial_id, _, num in study_rows:
                if isinstance(num, bool) or not isinstance(num, int) or num < 0:
                    return (
                        f"study {name} has a ledger row whose recorded Optuna trial number is "
                        f"not a non-negative integer ({num!r})"
                    )
                if num < n_startup:
                    ids.add(trial_id)
            methods.add("recorded")
            continue
        number = 0
        for trial_id, n_trials, _ in study_rows:
            if isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1:
                return (
                    f"study {name}'s ledger rows carry no _study.n_trials, so Optuna trial "
                    "numbers (and the random start-up trials) cannot be identified"
                )
            if number < n_startup:
                ids.add(trial_id)
            number = (number + 1) % n_trials
        if number != 0:
            return (
                f"study {name} has {len(study_rows)} ledger rows on the sweep window, not whole "
                "repeats of its n_trials (an interrupted or overlapping sweep), so Optuna trial "
                "numbers cannot be identified"
            )
        methods.add("ledger order")
    return frozenset(ids), methods


def _cpcv_result(matrix: np.ndarray, interval: str, source: str, basis: str) -> dict[str, Any]:
    try:
        res = cpcv_paths(matrix, bars_per_year=bars_per_year(interval))
    except ValueError as exc:
        return {"missing": f"CPCV not computable on the sweep matrix: {exc}"}
    out = _jsonable(asdict(res))
    out["basis"] = basis
    out["configs_source"] = source
    return out


def _cpcv_blocks(
    ledger: Ledger, bt: dict[str, Any] | None, matrices: list[SweepMatrix]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(cpcv, cpcv_startup)``: the CPCV OOS Sharpe path distribution over every
    design-window Optuna trial of the family's sweep (an upper bound, see
    :data:`CPCV_UPPER_BOUND_BASIS`) and over only TPE's random start-up trials."""
    m, reason = _sweep_for_cpcv(bt, matrices)
    if m is None:
        return {"missing": reason}, {"missing": reason}
    try:
        interval = ledger.get(m.ids[0]).interval
    except ValueError as exc:
        reason = f"CPCV not computable on the sweep matrix: {exc}"
        return {"missing": reason}, {"missing": reason}
    span = (
        f"{pd.Timestamp(m.start, unit='s', tz='UTC'):%Y-%m-%d} to "
        f"{pd.Timestamp(m.end, unit='s', tz='UTC'):%Y-%m-%d}"
    )
    tail = (
        "In each split the in-sample best configuration is scored on its purged, embargoed "
        "test groups; the paths are those out-of-sample pieces stitched together. No new "
        "backtests, nothing recorded, and the DSR's N is unchanged (it counts configurations "
        "searched, not paths). Information only."
    )
    studies = ", ".join(sorted(m.studies))
    full = _cpcv_result(
        m.matrix,
        interval,
        f"{len(m.ids)} design-window Optuna trials ({interval}, {span}) of study(ies) "
        f"{studies} in run {m.run_id}: the plan's behaviour-family sweep, read from the ledger "
        f"(same bars and costs as those trials). {tail}",
        f"{CPCV_UPPER_BOUND_BASIS} (TPE proposed them from objective values over the whole "
        "design window, so the out-of-sample paths are not out of the search's sample)",
    )
    found = _startup_trial_ids(ledger, m)
    if isinstance(found, str):
        return full, {"missing": found}
    ids, methods = found
    # A rerun of a seeded study redraws the same start-up configurations: identical return
    # columns are one configuration, so CPCV-startup counts (and compares) each once.
    seen: set[bytes] = set()
    cols: list[int] = []
    n_dupes = 0
    for j, trial_id in enumerate(m.ids):
        if trial_id not in ids:
            continue
        key = np.ascontiguousarray(m.matrix[:, j]).tobytes()
        if key in seen:
            n_dupes += 1
            continue
        seen.add(key)
        cols.append(j)
    from lab.search.optuna_search import TPE_SAMPLER_SETTINGS  # lazy: imports optuna

    n_startup = int(TPE_SAMPLER_SETTINGS["n_startup_trials"])
    dupes = (
        f" ({n_dupes} more with returns identical to one of these, e.g. a rerun's redraw, "
        "counted once)"
        if n_dupes
        else ""
    )
    if len(cols) < 2:
        return full, {
            "missing": f"{len(cols)} usable random start-up trial(s) with distinct returns "
            f"(Optuna trial number < n_startup_trials = {n_startup}){dupes} in the sweep "
            "matrix; CPCV needs at least 2"
        }
    numbered = {
        "recorded": "by the recorded Optuna trial number",
        "ledger order": "by ledger order (rows from before the trial number was recorded: one "
        "row per trial, ok and error, sequential)",
    }
    how = " / ".join(numbered[k] for k in sorted(methods))
    startup = _cpcv_result(
        m.matrix[:, cols],
        interval,
        f"{len(cols)} of the {len(m.ids)} design-window Optuna trials above{dupes} "
        f"({interval}, {span}, study(ies) {studies}, run {m.run_id}): those with Optuna trial "
        f"number < n_startup_trials = {n_startup} within their study, numbered {how}. {tail}",
        "random start-up trials only: TPE drew them from the search space before using any "
        "objective value, so no test bar shaped which configurations are compared",
    )
    return full, startup


def plan_statistics(
    c: SuccessCriteria,
    ledger: Ledger,
    bt: dict[str, Any] | None,
    tn: TrialN,
    dsr: DsrEval,
    matrices: list[SweepMatrix],
    *,
    run_id: str,
    cfg: LabConfig,
) -> dict[str, Any]:
    """The report's ``statistics`` block (information only, never pass/fail): ``trial_count``
    (the nominal N the DSR used, globally and for this run, beside the effective-N diagnostic
    and its time-shuffled null), ``min_backtest_length`` (MinBTL for the DSR's N at the plan's
    observed design-window Sharpe), ``min_track_record_length`` (MinTRL at the DSR's deflated
    benchmark SR0 and confidence ``dsr_min``), ``cpcv`` (the OOS Sharpe path distribution over
    the family's sweep trials: an upper bound) and ``cpcv_startup`` (the same over only TPE's
    random start-up trials). A part that cannot be computed is ``{"missing": reason}``."""
    cpcv, cpcv_startup = _cpcv_blocks(ledger, bt, matrices)
    return {
        "trial_count": _trial_count_block(tn, ledger, run_id, cfg),
        "min_backtest_length": _min_btl_block(dsr, tn),
        "min_track_record_length": _min_trl_block(c, dsr),
        "cpcv": cpcv,
        "cpcv_startup": cpcv_startup,
        "note": (
            "information only: none of these is a criterion row and none changes pass/fail; a "
            "too-short design window is stated on the dsr_min row"
        ),
    }


def _length_reason(stats: Mapping[str, Any]) -> str | None:
    """ "design window too short: ..." when the window is below MinTRL and/or MinBTL (finite
    ones only: an infinite length is a Sharpe problem, not a length problem), else None."""
    parts: list[str] = []
    trl = stats.get("min_track_record_length", {})
    if trl.get("bars") is not None and trl.get("ok") is False:
        parts.append(
            f"{trl['design_bars']} bars < MinTRL {trl['bars']:.0f} bars at the deflated benchmark"
        )
    btl = stats.get("min_backtest_length", {})
    if btl.get("years") is not None and btl.get("ok") is False:
        parts.append(
            f"{btl['design_years']:.2f} years < MinBTL {btl['years']:.2f} years for "
            f"{btl['n_trials']} trials"
        )
    return "design window too short: " + "; ".join(parts) if parts else None


def with_length_reason(row: CriterionRow, stats: Mapping[str, Any]) -> CriterionRow:
    """The ``dsr_min`` row with the stated length reason appended when it did not pass and the
    design window is below MinTRL or MinBTL. Never changes its status."""
    reason = _length_reason(stats)
    if row["status"] == "pass" or reason is None:
        return row
    return CriterionRow(**{**row, "note": f"{row['note']}; {reason}"})


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
    matrices = sweep_matrices(ledger, sweeps)
    tn = trial_n(c, ledger)
    dsr = _dsr_eval(c, ledger, run_id, plan_hash, bt, cfg, tn)
    statistics = plan_statistics(c, ledger, bt, tn, dsr, matrices, run_id=run_id, cfg=cfg)

    by_name: dict[str, CriterionRow] = {
        "wf_oos_sharpe_min": _wf_row(c, wfs, family),
        "dsr_min": with_length_reason(dsr.row, statistics),
        "pbo_max": _pbo_row(c, ledger, bt, sweeps, cfg, matrices),
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
        statistics=statistics,
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
