"""Judge one overlay plan against a risk-overlay run's FROZEN rows (PLAN.md Phase 15).

The question, pre-registered in ``lab/config/overlay_criteria.toml`` and frozen at
``start_run(question="risk_overlay")``: does ONE slow overlay on buy-and-hold of one symbol cut
drawdown, without a significant Sharpe loss, with a break-even cost of at least twice the base
cost? A pass is reported as a risk overlay (smaller drawdowns at a similar risk-adjusted return),
never as a return claim, and it still needs 4-8 weeks of paper trading.

:func:`evaluate_overlay_plan` reads the run's frozen :class:`OverlayCriteria` (never the current
file) and the plan's latest ``backtest`` evidence, whose ``"overlay"`` block
(:func:`lab.research.overlay.overlay_block`) holds the raw numbers. The rows, in
:data:`OVERLAY_ROWS` order (status pass / fail / missing / not_testable; only pass is a pass):

==================================  ==========================================================
``cdar_vs_buy_hold_max``            CDaR(overlay) / CDaR(buy-and-hold) at base costs <= value;
                                    not_testable with too few drawdown episodes
``beat_exposure_matched``           CDaR(overlay) < CDaR(exposure-matched buy-and-hold), point
                                    below 0 and paired-bootstrap p <= alpha / N_overlay;
                                    not_testable with too few drawdown episodes
``min_episodes_improved``           in strictly more than ``min_episodes_improved_share`` of
                                    buy-and-hold's deep episodes, the overlay's max drawdown
                                    inside [peak, recovery] (from its own equity at the peak) is
                                    below the exposure-matched series' (3 episodes need 2, 4
                                    need 3); not_testable with too few drawdown episodes
``sharpe_non_inferiority``          one-sided lower bound (level 1 - alpha) of Sharpe(overlay) -
                                    Sharpe(buy-and-hold) >= -value AND the bootstrap
                                    non-inferiority p <= alpha / N_overlay (see below)
``break_even_cost_multiple_min``    the largest cost multiple up to which the four rows above
                                    all hold (:func:`lab.stats.paired.break_even_cost_multiple`
                                    over :func:`lab.research.overlay.cost_multiples`) >= value
``min_drawdown_episodes``           buy-and-hold's episodes (peak to recovery) deeper than
                                    ``episode_depth_min`` in the scored window >= value; fewer is
                                    not_testable (never a pass)
``min_avg_exposure``                the overlay's average exposure >= value
``wf_rows_hold``                    a SUB-WINDOW ROBUSTNESS CHECK on stitched fixed-parameter
                                    test folds (same design bars, not out-of-sample): the cdar,
                                    exposure-matched, episodes-improved, Sharpe, break-even and
                                    exposure rows on the folds. The episode-based rows (cdar,
                                    exposure-matched, episodes-improved, break-even) are judged
                                    there only when the stitched folds themselves hold at least
                                    ``min_drawdown_episodes`` deep episodes, else not_testable;
                                    status fail > not_testable > missing > pass
``pbo_max``                         pass only when nothing was selected in-sample: nothing is
                                    swept (overlays are untuned by rule) AND the plan's
                                    behaviour family (any overlay run) holds no other submitted
                                    overlay; more than one alike overlay submitted is a fail
``require_holdout_pass``            as for every plan: the plan's own holdout evidence, else fail
                                    when its behaviour family's single check is spent (either
                                    question, any run)
``require_crosscheck_match``        as for every plan: a matched event-driven cross-check
==================================  ==========================================================

Multiple testing: ``N_overlay`` (:func:`overlay_trial_count`) is the number of distinct overlay
logics tried in EVERY risk-overlay run (selection-source trials, ok and error; a new run never
resets it), and ``alpha_adjusted = alpha / N_overlay`` (Bonferroni). Each logic may be tried
with ONE published parameter set only, across every overlay run: before anything is recorded,
:func:`check_overlay_untuned` refuses a second plan hash of a submitted logic, a submission
whose registry features match an earlier overlay's with other parameters, and a logic whose
parameters were already tried (more than one set, or another set) in an edge run. Alike
copies on other features are caught after the backtest by ``pbo_max``. When
``alpha_adjusted < 1 / (1 + B)`` (``B`` bootstrap samples) no bootstrap p can reach it: the
rows say "underpowered" and fail (never a pass). The Sharpe
non-inferiority test is divided too: a false "risk overlay" claim needs only ONE of the two
tests to be wrong (a plan whose CDaR gain is real but whose Sharpe loss is not), so with N
plans the chance that some plan passes on a wrongly accepted Sharpe would reach N x alpha
otherwise. Dividing both is the conservative choice.

Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, NotRequired, TypedDict

from lab.config import LabConfig
from lab.ledger import SELECTION_SOURCES, Ledger
from lab.plan import parse_plan, plan_hash
from lab.plan.validator import PlanValidationError
from lab.registry import REGISTRY
from lab.report import num
from lab.research.contract import OverlayCriteria
from lab.research.criteria import (
    CriterionRow,
    CriterionStatus,
    criterion_row,
    crosscheck_row,
    holdout_row,
    latest_evidence,
    plan_derivatives_dict,
    plan_family,
)
from lab.research.family import FamilyInfo, plan_logic
from lab.research.logic import feature_multiset, logic_hash
from lab.research.runs import (
    budget_status,
    check_derivatives_snapshot,
    check_lab_fingerprint,
    frozen_overlay_criteria,
    overlay_run_ids,
)
from lab.runner import CandleLoader
from lab.stats.paired import break_even_cost_multiple
from lab.types import Plan

__all__ = [
    "N_OVERLAY_BASIS",
    "OVERLAY_PRE_HOLDOUT_EXCLUDED",
    "OVERLAY_ROWS",
    "OVERLAY_SUBMISSION_SOURCES",
    "OverlayReport",
    "OverlaySubmission",
    "alike_overlay_submissions",
    "check_overlay_untuned",
    "evaluate_overlay_plan",
    "overlay_submission_list",
    "overlay_submissions",
    "overlay_trial_count",
    "param_key",
]

OVERLAY_ROWS: tuple[str, ...] = (
    "cdar_vs_buy_hold_max",
    "beat_exposure_matched",
    "min_episodes_improved",
    "sharpe_non_inferiority",
    "break_even_cost_multiple_min",
    "min_drawdown_episodes",
    "min_avg_exposure",
    "wf_rows_hold",
    "pbo_max",
    "require_holdout_pass",
    "require_crosscheck_match",
)
# Rows that do not gate the holdout check (they are the holdout itself).
OVERLAY_PRE_HOLDOUT_EXCLUDED: tuple[str, ...] = ("require_holdout_pass",)
# The rows re-judged on the stitched walk-forward test folds (wf_rows_hold).
_WF_ROWS: tuple[str, ...] = (
    "cdar_vs_buy_hold_max",
    "beat_exposure_matched",
    "min_episodes_improved",
    "sharpe_non_inferiority",
    "break_even_cost_multiple_min",
    "min_avg_exposure",
)
# The rows that compare drawdowns, so need enough deep buy-and-hold episodes to be judged.
_EPISODE_ROWS: tuple[str, ...] = (
    "cdar_vs_buy_hold_max",
    "beat_exposure_matched",
    "min_episodes_improved",
    "break_even_cost_multiple_min",
)
# Trial sources that are a SUBMISSION of a plan (run_backtest / run_adversarial base trials and
# walk-forward folds: "api"; the holdout trial: "manual"). Neighbourhood and adversarial
# variants are generated by the lab itself and are never submissions.
OVERLAY_SUBMISSION_SOURCES: tuple[str, ...] = ("api", "manual")
N_OVERLAY_BASIS = (
    "N_overlay = distinct overlay logics across every overlay run (name, aliases, parameter "
    "values and rule literals ignored; ok and error trials); each logic may be tried with one "
    "published parameter set only, so this is also the number of overlay configurations "
    "submitted. A new run never resets it. alpha_adjusted = alpha / N_overlay (Bonferroni), "
    "used by the exposure-matched and Sharpe non-inferiority tests"
)
WF_NOTE = (
    "sub-window robustness check on stitched fixed-parameter test folds (same design bars, not "
    "out-of-sample)"
)
REPORT_NOTE = (
    "A pass is a risk overlay (smaller drawdowns than buy-and-hold and than buy-and-hold scaled "
    "to the same exposure, without a significant Sharpe loss), never a return claim; it still "
    "needs 4-8 weeks of paper trading. not_testable is never a pass. wf_rows_hold is a "
    "sub-window robustness check on stitched fixed-parameter test folds (same design bars, not "
    "out-of-sample). Each overlay logic may be tried with one published parameter set only. "
    "Results are historical and not forward-looking."
)
_NO_BACKTEST = "no backtest evidence with an overlay block: run run_backtest"


class OverlayReport(TypedDict):
    run_id: str
    plan_hash: str
    question: str  # always "risk_overlay"
    pre_holdout_passed: bool  # every row except the holdout is "pass" (cross-check included)
    passed: bool  # every row is "pass"
    rows: list[CriterionRow]
    missing: list[str]
    not_testable: list[str]
    n_overlay: int
    n_overlay_basis: str
    alpha: float
    alpha_adjusted: float
    budget: dict[str, Any]
    note: str
    overlay: NotRequired[dict[str, Any] | None]  # the measured block (raw numbers)


# --------------------------------------------------------------------------- N_overlay


def overlay_trial_count(ledger: Ledger) -> int:
    """N_overlay: distinct ``logic_hash`` of the plans of every selection-source trial (ok and
    error) in every risk-overlay run (:func:`lab.research.runs.overlay_run_ids`), at least 1.
    A trial whose plan JSON cannot be parsed counts as its own logic (it can only raise N)."""
    logics: set[str] = set()
    for rid in sorted(overlay_run_ids(ledger)):
        for rec in ledger.list(run_id=rid, limit=ledger.count(rid)):
            if rec.source not in SELECTION_SOURCES:
                continue
            lh = plan_logic(rec.plan_json)
            logics.add(lh if lh is not None else f"plan:{rec.plan_hash}")
    return max(1, len(logics))


ParamKey = tuple[str, tuple[tuple[str, tuple[tuple[str, float], ...]], ...]]


def param_key(plan: Plan) -> ParamKey:
    """The plan's parameter set: its interval and the sorted ``(registry feature, params)``
    pairs, every param filled with its registry default (aliases, names and rules ignored)."""
    feats = []
    for f in plan.features:
        spec = REGISTRY.get(f.feature)
        full = {p.name: float(p.default) for p in spec.params} if spec is not None else {}
        full.update({k: float(v) for k, v in f.params.items()})
        feats.append((f.feature, tuple(sorted(full.items()))))
    return (str(plan.interval), tuple(sorted(feats)))


@lru_cache(maxsize=65_536)
def _parsed(plan_json: str) -> tuple[str, tuple[str, ...], ParamKey] | None:
    """``(logic hash, feature multiset, parameter key)`` of a stored plan; None if it is not a
    single-symbol plan that parses (portfolio plans have no overlay twin)."""
    try:
        plan = parse_plan(plan_json)
        return logic_hash(plan), feature_multiset(plan), param_key(plan)
    except (PlanValidationError, ValueError, TypeError, AttributeError):
        return None


@dataclass(frozen=True)
class OverlaySubmission:
    """One plan submitted in a risk-overlay run (its first submitted trial)."""

    plan_hash: str
    run_id: str
    logic_hash: str
    features: tuple[str, ...]
    params: ParamKey


def overlay_submission_list(ledger: Ledger) -> list[OverlaySubmission]:
    """Every distinct plan submitted (a trial whose source is in
    :data:`OVERLAY_SUBMISSION_SOURCES`, ok or error, any window) in every risk-overlay run,
    oldest first. Plans whose JSON does not parse are skipped (no logic to share)."""
    out: list[OverlaySubmission] = []
    for h, pj, rid in ledger.first_plan_trials(
        sources=OVERLAY_SUBMISSION_SOURCES, overlay_runs=True
    ):
        got = _parsed(pj)
        if got is not None:
            out.append(OverlaySubmission(h, rid, got[0], got[1], got[2]))
    return out


def overlay_submissions(ledger: Ledger) -> dict[str, tuple[str, str]]:
    """``logic_hash -> (plan_hash, run_id)`` of the FIRST submitted plan of each logic in every
    risk-overlay run (:func:`overlay_submission_list`)."""
    first: dict[str, tuple[str, str]] = {}
    for s in overlay_submission_list(ledger):
        first.setdefault(s.logic_hash, (s.plan_hash, s.run_id))
    return first


def _show_params(key: ParamKey) -> str:
    interval, feats = key
    inner = ", ".join(
        f"{name}({', '.join(f'{k}={v:g}' for k, v in params)})" for name, params in feats
    )
    return f"{inner} on {interval}"


def check_overlay_untuned(ledger: Ledger, plan: Plan) -> None:
    """``ValueError`` unless ``plan`` (an overlay) is the ONE published parameter set of its
    rule. Called before anything is run or recorded. Refused when, in the ledger:

    1. its logic (:func:`lab.research.logic.logic_hash`: names, aliases, parameter values and
       rule literals ignored) was already submitted in ANY risk-overlay run as another plan
       hash (a renamed or re-parameterised copy);
    2. an earlier overlay submission (any risk-overlay run) uses the same registry-feature
       multiset (feature names, aliases and params ignored) with a DIFFERENT parameter set
       (:func:`param_key`: feature params with defaults, and the interval). This catches
       rewrites of one rule the logic hash cannot see (``ma > close``, ``not (close >= ma)``,
       ``close / ma < 1``). The same features with identical params are allowed: a genuinely
       different rule on the same published features;
    3. selection-source trials of the same logic in any OTHER run (edge research runs, the web
       UI) used more than one parameter set, or one that differs from this plan's: the logic
       was already tuned there (an edge sweep of the signal twin), so its parameters are not
       the untouched published ones. Rule 2 is NOT applied to edge runs: they legitimately
       used the same features with many parameters in other logics.

    Submissions are the trials of :data:`OVERLAY_SUBMISSION_SOURCES`; the lab's neighbourhood
    and adversarial variants never are. Behaviour-alike copies on other features (an ema copy
    of an sma rule) are caught after the backtest by the ``pbo_max`` row instead."""
    h, lh = plan_hash(plan), logic_hash(plan)
    multiset, key = feature_multiset(plan), param_key(plan)
    subs = overlay_submission_list(ledger)
    tail = (
        "Each overlay rule may be tried with one published parameter set only, across every "
        "overlay run. Nothing was run or recorded"
    )
    for s in subs:
        if s.logic_hash == lh and s.plan_hash != h:
            raise ValueError(
                f"overlays use published parameters, untuned: logic already tried as plan "
                f"{s.plan_hash[:12]} in run {s.run_id} (this plan is {h[:12]}, logic "
                f"{lh[:12]}); a renamed or re-parameterised copy is the same logic. {tail}"
            )
    for s in subs:
        if s.features == multiset and s.params != key:
            raise ValueError(
                f"overlays use published parameters, untuned: the same features "
                f"({', '.join(multiset)}) were already submitted with other parameters as plan "
                f"{s.plan_hash[:12]} in run {s.run_id} ({_show_params(s.params)}; this plan: "
                f"{_show_params(key)}). A rewrite of a rule with new parameters is tuning on "
                f"seen data; the same features are allowed only with identical parameters. {tail}"
            )
    edge: dict[ParamKey, str] = {}
    for _h, pj, rid in ledger.first_plan_trials(sources=SELECTION_SOURCES, overlay_runs=False):
        got = _parsed(pj)
        if got is not None and got[0] == lh:
            edge.setdefault(got[2], rid)
    if len(edge) > 1 or (len(edge) == 1 and key not in edge):
        first_run = next(iter(edge.values()))
        raise ValueError(
            f"overlays use published parameters, untuned: logic already tuned in edge run "
            f"{first_run} ({len(edge)} parameter set(s) of logic {lh[:12]} were tried outside "
            f"the overlay runs: {'; '.join(_show_params(k) for k in edge)}; this plan: "
            f"{_show_params(key)}). Its parameters were already selected on the design window, "
            f"so it cannot be asked as an untuned overlay. {tail}"
        )


# --------------------------------------------------------------------------- rows


def _f(x: Any) -> str:
    v = num(x)
    return "n/a" if v is None else f"{v:.4g}"


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    return obj


def _row(
    name: str, status: CriterionStatus, value: Any, threshold: float | bool | None, note: str
) -> CriterionRow:
    if isinstance(value, bool) or (isinstance(value, int) and not isinstance(value, bool)):
        v: float | bool | None = value  # counts stay integers
    else:
        v = num(value)
    return criterion_row(name, status, v, threshold, note)


def _at(by_cost: Any, multiple: float) -> Mapping[str, Any] | None:
    if not isinstance(by_cost, list):
        return None
    for row in by_cost:
        if isinstance(row, Mapping) and num(row.get("multiple")) == float(multiple):
            return row
    return None


def _cdar_holds(c: OverlayCriteria, r: Mapping[str, Any]) -> bool:
    ratio = num(r.get("cdar_ratio"))
    return ratio is not None and ratio <= c.cdar_vs_buy_hold_max


def _beat_holds(c: OverlayCriteria, r: Mapping[str, Any], alpha_adj: float) -> bool:
    if not c.beat_exposure_matched:
        return True
    point, p = num(r.get("cdar_point")), num(r.get("cdar_p_value"))
    return point is not None and point < 0.0 and p is not None and p <= alpha_adj


def _episode_counts(r: Mapping[str, Any] | None) -> tuple[int, int] | None:
    """``(qualifying, improved)`` episode counts of one by-cost row, None when absent."""
    if r is None:
        return None
    q, imp = r.get("episodes_qualifying"), r.get("episodes_improved")
    if isinstance(q, bool) or isinstance(imp, bool):
        return None
    if not isinstance(q, int) or not isinstance(imp, int):
        return None
    return q, imp


def _improved_share_holds(c: OverlayCriteria, qualifying: int, improved: int) -> bool:
    """Strictly more than ``min_episodes_improved_share`` of the qualifying episodes improved
    (3 episodes need 2, 4 need 3 at 0.5), with at least ``min_drawdown_episodes`` of them."""
    return (
        qualifying >= c.min_drawdown_episodes
        and qualifying > 0
        and improved / qualifying > c.min_episodes_improved_share
    )


def _episodes_holds(c: OverlayCriteria, r: Mapping[str, Any]) -> bool:
    counts = _episode_counts(r)
    return counts is not None and _improved_share_holds(c, *counts)


def _sharpe_holds(c: OverlayCriteria, r: Mapping[str, Any], alpha_adj: float) -> bool:
    lower, p = num(r.get("sharpe_lower")), num(r.get("sharpe_ni_p_value"))
    return (
        lower is not None
        and lower >= -c.sharpe_non_inferiority
        and p is not None
        and p <= alpha_adj
    )


def _underpowered(alpha_adj: float, n_samples: int | None) -> str:
    """The note suffix when no bootstrap p can reach ``alpha_adj`` (empty otherwise)."""
    if n_samples is None or n_samples < 1:
        return ""
    floor = 1.0 / (1.0 + n_samples)
    if not alpha_adj < floor:
        return ""
    return (
        f"; underpowered: smallest attainable p is 1/(1+B) = 1/(1+{n_samples}) = {_f(floor)} > "
        f"alpha/N_overlay = {_f(alpha_adj)}, so this test cannot pass (a fail, never a pass)"
    )


def _need(c: OverlayCriteria, qualifying: int) -> int:
    """Smallest improved count that is strictly above the share of ``qualifying`` episodes."""
    need = int(c.min_episodes_improved_share * qualifying) + 1
    while need > 0 and (need - 1) / max(qualifying, 1) > c.min_episodes_improved_share:
        need -= 1  # guards float rounding of share * qualifying
    return need


def _window_rows(
    c: OverlayCriteria,
    block: Mapping[str, Any],
    alpha_adj: float,
    *,
    testable: bool,
    few: str,
    where: str,
    n_samples: int | None,
) -> dict[str, CriterionRow]:
    """The cdar / exposure-matched / episodes-improved / Sharpe / break-even / exposure rows on
    one measured window (the design window's block or its ``wf_fixed`` part). ``testable``
    False (too few deep buy-and-hold drawdown episodes) makes the episode-based rows
    (:data:`_EPISODE_ROWS`) not_testable with the note ``few``."""
    by_cost = block.get("by_cost")
    base = _at(by_cost, 1.0)
    weak = _underpowered(alpha_adj, n_samples)
    out: dict[str, CriterionRow] = {}

    name = "cdar_vs_buy_hold_max"
    ratio = num(block.get("cdar_ratio_vs_buy_hold"))
    cd = block.get("cdar") if isinstance(block.get("cdar"), Mapping) else {}
    detail = (
        f"CDaR at {c.cdar_confidence:.0%} overlay {_f(_get(cd, 'overlay'))} vs buy-and-hold "
        f"{_f(_get(cd, 'buy_hold'))} at base costs"
    )
    if not testable:
        out[name] = _row(name, "not_testable", ratio, c.cdar_vs_buy_hold_max, few)
    elif ratio is None:
        out[name] = _row(
            name,
            "not_testable",
            None,
            c.cdar_vs_buy_hold_max,
            f"{where}: buy-and-hold has no drawdown, so the ratio is undefined",
        )
    else:
        ok = ratio <= c.cdar_vs_buy_hold_max
        out[name] = _row(
            name,
            "pass" if ok else "fail",
            ratio,
            c.cdar_vs_buy_hold_max,
            f"{where}: {detail}; ratio {_f(ratio)} must be <= {c.cdar_vs_buy_hold_max:g}",
        )

    name = "beat_exposure_matched"
    p = num(base.get("cdar_p_value")) if base is not None else None
    point = num(base.get("cdar_point")) if base is not None else None
    if not c.beat_exposure_matched:
        out[name] = _row(name, "pass", None, False, "not required by this run's criteria")
    elif not testable:
        out[name] = _row(name, "not_testable", p, alpha_adj, few)
    elif base is None or p is None or point is None:
        out[name] = _row(name, "missing", None, alpha_adj, f"{where}: no 1x bootstrap row")
    else:
        ok = _beat_holds(c, base, alpha_adj) and not weak
        out[name] = _row(
            name,
            "pass" if ok else "fail",
            p,
            alpha_adj,
            f"{where}: CDaR overlay - CDaR exposure-matched buy-and-hold "
            f"{_f(point)} (bootstrap bias {_f(base.get('cdar_bias'))}; matched CDaR "
            f"{_f(_get(cd, 'exposure_matched'))} at average exposure "
            f"{_f(block.get('avg_exposure'))}); needs a point below 0 and paired-bootstrap "
            f"p {_f(p)} <= alpha / N_overlay = {_f(alpha_adj)}{weak}",
        )

    name = "min_episodes_improved"
    counts = _episode_counts(base)
    share_t = c.min_episodes_improved_share
    if not testable:
        out[name] = _row(name, "not_testable", None, share_t, few)
    elif counts is None:
        out[name] = _row(name, "missing", None, share_t, f"{where}: no 1x episode comparison")
    else:
        q, imp = counts
        share = imp / q if q > 0 else None
        ok = _improved_share_holds(c, q, imp)
        out[name] = _row(
            name,
            "pass" if ok else "fail",
            share,
            share_t,
            f"{where}: the overlay's max drawdown inside the episode (from its own equity at "
            f"the peak) is below exposure-matched buy-and-hold's in {imp} of {q} buy-and-hold "
            f"episode(s) deeper than {c.episode_depth_min:.0%}; needs a share strictly above "
            f"{share_t:g} ({_need(c, q)} of {q})",
        )

    name = "sharpe_non_inferiority"
    lower = num(base.get("sharpe_lower")) if base is not None else None
    ni_p = num(base.get("sharpe_ni_p_value")) if base is not None else None
    if base is None or lower is None or ni_p is None:
        out[name] = _row(
            name,
            "missing",
            None,
            -c.sharpe_non_inferiority,
            f"{where}: the Sharpe gap bootstrap is undefined (no 1x row, or zero variance)",
        )
    else:
        ok = _sharpe_holds(c, base, alpha_adj) and not weak
        sh = block.get("sharpe") if isinstance(block.get("sharpe"), Mapping) else {}
        out[name] = _row(
            name,
            "pass" if ok else "fail",
            lower,
            -c.sharpe_non_inferiority,
            f"{where}: Sharpe overlay {_f(_get(sh, 'overlay'))} - buy-and-hold "
            f"{_f(_get(sh, 'buy_hold'))} = {_f(base.get('sharpe_point'))} (365-day "
            f"annualised; bootstrap bias {_f(base.get('sharpe_bias'))}); one-sided "
            f"{1 - c.alpha:.0%} lower bound {_f(lower)} must be >= "
            f"{-c.sharpe_non_inferiority:g}, and the non-inferiority p {_f(ni_p)} (share of "
            f"resampled gaps <= {-c.sharpe_non_inferiority:g}) must be <= alpha / N_overlay = "
            f"{_f(alpha_adj)} (divided like the drawdown test: a false pass needs only one of "
            f"the two to be wrong){weak}",
        )

    name = "break_even_cost_multiple_min"
    if not testable:
        out[name] = _row(name, "not_testable", None, c.break_even_cost_multiple_min, few)
    elif not isinstance(by_cost, list) or base is None:
        out[name] = _row(
            name, "missing", None, c.break_even_cost_multiple_min, f"{where}: no by-cost rows"
        )
    else:
        holds: dict[float, bool] = {}
        for r in by_cost:
            m = num(r.get("multiple")) if isinstance(r, Mapping) else None
            if m is None or m <= 0.0:
                continue
            holds[m] = bool(
                not weak
                and _cdar_holds(c, r)
                and _beat_holds(c, r, alpha_adj)
                and _episodes_holds(c, r)
                and _sharpe_holds(c, r, alpha_adj)
            )
        be = break_even_cost_multiple(holds)
        ok = be is not None and be >= c.break_even_cost_multiple_min
        grid = ", ".join(f"{m:g}x {'holds' if v else 'fails'}" for m, v in sorted(holds.items()))
        out[name] = _row(
            name,
            "pass" if ok else "fail",
            be,
            c.break_even_cost_multiple_min,
            f"{where}: largest cost multiple up to which the CDaR, exposure-matched, "
            f"episodes-improved and Sharpe rows all hold = {_f(be)} (needs >= "
            f"{c.break_even_cost_multiple_min:g}); {grid}{weak}",
        )

    name = "min_avg_exposure"
    exposure = num(block.get("avg_exposure"))
    if exposure is None:
        out[name] = _row(name, "missing", None, c.min_avg_exposure, f"{where}: no exposure")
    else:
        ok = exposure >= c.min_avg_exposure
        out[name] = _row(
            name,
            "pass" if ok else "fail",
            exposure,
            c.min_avg_exposure,
            f"{where}: invested on {exposure:.1%} of the bars after the first decision (an "
            f"overlay must hold the coin at least {c.min_avg_exposure:.0%} of the time)",
        )
    return out


def _episode_count(block: Mapping[str, Any]) -> int | None:
    count = _get(block, "episodes", "buy_hold_count")
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def _few(c: OverlayCriteria, where: str, count: int | None) -> str:
    return (
        f"{where}: buy-and-hold has {count if count is not None else 'no counted'} drawdown "
        f"episode(s) deeper than {c.episode_depth_min:.0%}, fewer than the "
        f"{c.min_drawdown_episodes} needed to compare drawdowns (not_testable, never a pass)"
    )


def _episodes_row(c: OverlayCriteria, block: Mapping[str, Any]) -> tuple[CriterionRow, bool]:
    name = "min_drawdown_episodes"
    count = _episode_count(block)
    if count is None:
        return _row(name, "missing", None, c.min_drawdown_episodes, _NO_BACKTEST), False
    ok = count >= c.min_drawdown_episodes
    note = (
        f"buy-and-hold has {count} drawdown episode(s) (peak to recovery) deeper than "
        f"{c.episode_depth_min:.0%} in the scored window; at least {c.min_drawdown_episodes} "
        "are needed to compare drawdowns (fewer = not_testable, never a pass)"
    )
    return _row(name, "pass" if ok else "not_testable", count, c.min_drawdown_episodes, note), ok


def _wf_row(
    c: OverlayCriteria,
    block: Mapping[str, Any],
    alpha_adj: float,
    testable: bool,
    n_samples: int | None,
) -> CriterionRow:
    """``wf_rows_hold``: a sub-window robustness check on the stitched fixed-parameter test
    folds (same design bars, not out-of-sample). The episode-based rows are judged on the folds
    only when the design window is testable AND the stitched folds themselves hold at least
    ``min_drawdown_episodes`` deep buy-and-hold episodes; otherwise they are not_testable there.
    Status: any fail -> fail, else any not_testable -> not_testable (never a pass), else any
    missing -> missing, else pass."""
    name = "wf_rows_hold"
    if not c.wf_rows_hold:
        return _row(name, "pass", None, False, "not required by this run's criteria")
    wf = block.get("wf_fixed")
    if not isinstance(wf, Mapping) or "error" in wf:
        reason = wf.get("error") if isinstance(wf, Mapping) else "no wf_fixed block"
        return _row(
            name, "missing", None, True, f"{WF_NOTE}: walk-forward test folds unavailable: {reason}"
        )
    where = "walk-forward test folds"
    fold_count = _episode_count(wf)
    fold_ok = fold_count is not None and fold_count >= c.min_drawdown_episodes
    if not testable:
        few = _few(c, "design window", _episode_count(block))
    else:
        few = _few(c, "stitched walk-forward test folds", fold_count)
    rows = _window_rows(
        c,
        wf,
        alpha_adj,
        testable=testable and fold_ok,
        few=few,
        where=where,
        n_samples=n_samples,
    )
    statuses = {k: rows[k]["status"] for k in _WF_ROWS}
    if any(s == "fail" for s in statuses.values()):
        status: CriterionStatus = "fail"
    elif any(s == "not_testable" for s in statuses.values()):
        status = "not_testable"
    elif any(s == "missing" for s in statuses.values()):
        status = "missing"
    else:
        status = "pass"
    folds = wf.get("folds")
    n_folds = len(folds) if isinstance(folds, list) else 0
    judged = [k for k, s in statuses.items() if s in ("pass", "fail")]
    untestable = [k for k, s in statuses.items() if s == "not_testable"]
    detail = "; ".join(f"{k} {s} ({_f(rows[k]['value'])})" for k, s in statuses.items())
    fold_eps = "n/a" if fold_count is None else str(fold_count)
    return _row(
        name,
        status,
        status == "pass",
        True,
        f"{WF_NOTE}: the rows on {n_folds} stitched fixed-parameter walk-forward test fold(s) "
        f"({wf.get('bars')} bars; nothing tuned; {fold_eps} buy-and-hold episode(s) deeper "
        f"than {c.episode_depth_min:.0%} in the stitched folds, {c.min_drawdown_episodes} "
        f"needed to judge the episode-based rows there). Judged: {', '.join(judged) or 'none'}; "
        f"not_testable: {', '.join(untestable) or 'none'} (not_testable is never a pass). "
        f"{detail}",
    )


def alike_overlay_submissions(ledger: Ledger, plan_hash_: str, family: FamilyInfo) -> list[str]:
    """Distinct overlay plan hashes submitted in any risk-overlay run (``plan_hash_`` included
    when it was submitted) that belong to the plan's behaviour family: its own logic, a logic
    whose design-window returns correlate at or above ``family_return_corr``, or a same-feature
    logic whose comparison was unknown (fail closed). Sorted."""
    logics = set(family.logic_hashes) | set(family.uncertain)
    return sorted(
        {
            s.plan_hash
            for s in overlay_submission_list(ledger)
            if s.plan_hash == plan_hash_
            or s.plan_hash in family.plan_hashes
            or s.logic_hash in logics
        }
    )


def _pbo_row(
    c: OverlayCriteria, ledger: Ledger, plan_hash_: str, family: FamilyInfo
) -> CriterionRow:
    """``pbo_max``: nothing is swept in an overlay run, so the row passes ONLY when the plan's
    behaviour family holds at most one submitted overlay (this one). More than one alike
    overlay (a rewrite, a same-feature copy or a correlated copy that the submission rules let
    through) means one was chosen among several in-sample, which no PBO here measures: fail."""
    alike = alike_overlay_submissions(ledger, plan_hash_, family)
    if len(alike) > 1:
        return _row(
            "pbo_max",
            "fail",
            None,
            c.pbo_max,
            f"{len(alike)} alike overlays submitted: {', '.join(h[:12] for h in alike)} (the "
            "plan's behaviour family across every overlay run: the same logic, or design-window "
            "returns correlated at or above family_return_corr). Choosing among alike overlays "
            "is a selection in-sample that no PBO here measures, so this row fails",
        )
    return _row(
        "pbo_max",
        "pass",
        None,
        c.pbo_max,
        "nothing swept: overlays are untuned by rule (run_sweep and walk_forward are refused in "
        "risk-overlay runs, and the plan's behaviour family holds no other submitted overlay), "
        "so no parameter was selected in-sample",
    )


def _missing_rows(c: OverlayCriteria, alpha_adj: float) -> dict[str, CriterionRow]:
    thresholds: dict[str, float | bool] = {
        "cdar_vs_buy_hold_max": c.cdar_vs_buy_hold_max,
        "beat_exposure_matched": alpha_adj,
        "min_episodes_improved": c.min_episodes_improved_share,
        "sharpe_non_inferiority": -c.sharpe_non_inferiority,
        "break_even_cost_multiple_min": c.break_even_cost_multiple_min,
        "min_drawdown_episodes": c.min_drawdown_episodes,
        "min_avg_exposure": c.min_avg_exposure,
        "wf_rows_hold": c.wf_rows_hold,
    }
    return {k: _row(k, "missing", None, v, _NO_BACKTEST) for k, v in thresholds.items()}


# --------------------------------------------------------------------------- public API


def evaluate_overlay_plan(
    ledger: Ledger,
    run_id: str,
    plan_hash: str,
    cfg: LabConfig,
    *,
    loader: CandleLoader | None = None,
) -> OverlayReport:
    """Judge ``plan_hash`` in risk-overlay run ``run_id`` against the run's frozen overlay rows
    (module docstring). Raises ``ValueError`` if the run never started, is not a risk-overlay
    run, its frozen lab.toml fingerprint differs from ``cfg``, or (a derivatives overlay) its
    frozen derivatives snapshot differs from the archive on disk."""
    c = frozen_overlay_criteria(ledger, run_id)
    check_lab_fingerprint(ledger, run_id, cfg)
    bt = latest_evidence(ledger, run_id, plan_hash, "backtest")
    drv_plan = plan_derivatives_dict(ledger, run_id, plan_hash, bt)
    if drv_plan is not None:
        check_derivatives_snapshot(ledger, run_id, cfg, drv_plan)
    cross = latest_evidence(ledger, run_id, plan_hash, "crosscheck")
    family = plan_family(ledger, run_id, plan_hash, bt, cfg, loader)
    n_overlay = overlay_trial_count(ledger)
    alpha_adj = float(c.alpha) / n_overlay
    block = bt.get("overlay") if isinstance(bt, Mapping) else None
    block = block if isinstance(block, Mapping) else None

    by_name: dict[str, CriterionRow]
    if block is None:
        by_name = _missing_rows(c, alpha_adj)
    else:
        episodes, testable = _episodes_row(c, block)
        n_samples = _get(block, "bootstrap", "n_samples")
        n_b = n_samples if isinstance(n_samples, int) and not isinstance(n_samples, bool) else None
        by_name = _window_rows(
            c,
            block,
            alpha_adj,
            testable=testable,
            few=_few(c, "design window", _episode_count(block)),
            where="design window",
            n_samples=n_b,
        )
        by_name["min_drawdown_episodes"] = episodes
        by_name["wf_rows_hold"] = _wf_row(c, block, alpha_adj, testable, n_b)
    by_name["pbo_max"] = _pbo_row(c, ledger, plan_hash, family)
    by_name["require_holdout_pass"] = holdout_row(
        c.require_holdout_pass, ledger, run_id, plan_hash, family
    )
    by_name["require_crosscheck_match"] = crosscheck_row(c.require_crosscheck_match, cross)
    rows = [by_name[k] for k in OVERLAY_ROWS]
    out = OverlayReport(
        run_id=run_id,
        plan_hash=plan_hash,
        question="risk_overlay",
        pre_holdout_passed=all(
            r["status"] == "pass"
            for r in rows
            if r["criterion"] not in OVERLAY_PRE_HOLDOUT_EXCLUDED
        ),
        passed=all(r["status"] == "pass" for r in rows),
        rows=rows,
        missing=[r["criterion"] for r in rows if r["status"] == "missing"],
        not_testable=[r["criterion"] for r in rows if r["status"] == "not_testable"],
        n_overlay=n_overlay,
        n_overlay_basis=N_OVERLAY_BASIS,
        alpha=float(c.alpha),
        alpha_adjusted=alpha_adj,
        budget=budget_status(ledger, run_id, cfg),
        note=REPORT_NOTE,
        overlay=dict(block) if block is not None else None,
    )
    return out
