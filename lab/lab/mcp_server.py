"""``strategy-lab`` MCP server (PLAN.md Phase 8 implementation step 1).

Run: ``uv run python -m lab.mcp_server`` (stdio). Register with Claude Code:
``claude mcp add strategy-lab -- uv --directory lab run python -m lab.mcp_server``.

Tools (all return compact JSON: never equity curves or trade lists):

* ``start_run(label)``: freeze the owner's success criteria + budget into the ledger under a
  server-assigned run id.
* ``list_features()``, ``validate_plan(plan)``: registry and validator (no ledger, no budget).
* ``run_backtest``, ``run_sweep``, ``walk_forward``, ``run_adversarial``: design window only;
  every trial goes to the ledger and the report is recorded as research evidence for the
  submitted plan's ``plan_hash`` (with its ``logic_hash``, :mod:`lab.research.logic`).
* ``run_crosscheck``: re-run a backtested plan in the event-driven engine (design window).
* ``get_ledger``, ``get_report``, ``get_budget``: read the run (holdout trials hidden);
  ``get_ledger(..., verify_chain=True)`` adds :meth:`lab.ledger.Ledger.verify_chain`.

Tool descriptions are the dedented docstrings, each in the audited shape (purpose, Use when,
Not for, Params, Example, Limits; PLAN.md Phase 13), checked by ``tests/test_mcp_descriptions.py``.
* ``holdout_check(run_id, plan)``: the locked holdout, budgeted server-side; pass/fail only.

Portfolio plans (``"kind": "portfolio"``, PLAN.md Phase 10) go through the same tools, routed
to :mod:`lab.research.portfolio` / :mod:`lab.portfolio_runner`: the universe comes from lab.toml
``[universe]`` (its manifest sha256 is frozen at ``start_run`` and every portfolio call refuses
once it changed), benchmarks are the equal-weight universe, BTC buy-and-hold, vol-targeted BTC
and the random-selection null (all scored from the plan's first decision), the cross-check is an
independent accounting replay (accounting only: it cannot catch signal look-ahead or a cost-model
bug), and ``get_report`` adds the ``[portfolio]`` criteria rows. ``rebalance.offset`` is pinned
at 0 (validate_plan and every portfolio tool reject another value; run_adversarial's
other-weekday table is descriptive timing luck).
Single-symbol plans behave exactly as before.

Budgets are enforced here, before anything runs: an unknown run id, a run whose frozen lab.toml
fingerprint differs from the server's, a new plan beyond ``max_proposals``, or a call whose
worst-case trial count exceeds the remaining ``max_backtests`` is refused with the numbers.
Guard + run hold a thread lock AND an exclusive ``fcntl.flock`` on
``<ledger dir>/locks/<run_id>.lock``, so two server processes on one ledger serialize per run
and cannot both pass the guard. The holdout budget is also enforced inside the ledger
(``BEGIN IMMEDIATE``).

Search spaces (``run_sweep`` / ``walk_forward``) must be honest: ``low < high`` and at least
``2 x neighbourhood_pct x |mid|`` wide (the run's frozen criteria). A ``walk_forward`` needs a
``run_backtest`` of the exact plan first (its base design-window returns define the plan's
behaviour family, :mod:`lab.research.family`), and its space must cover, for every tunable,
every value any trial of the behaviour family ever tried (backtests, neighbours, sweeps, earlier
walk-forwards; any research run) and every family sweep's declared range; a tunable it does not
search must never have been tried at another value (tried values outside a tunable's searchable
bounds are clamped to them, with a note). Rule literals (thresholds) cannot be searched, so a
walk-forward is refused when the family tried its logic with more than one literal vector. So
its out-of-sample folds cannot replay an in-sample choice made by a sweep, by hand-picked
backtests, in another run, in the web UI, or on a no-op variant.

Environment (read by :func:`main` only):

* ``STRATEGY_LAB_RUN_ID``: pin the server to one run; ``run_id`` arguments must equal it and
  ``start_run`` is refused.
* ``STRATEGY_LAB_DISABLE_HOLDOUT=1``: refuse ``holdout_check`` (the autonomous orchestrator
  runs the holdout itself, in Python, outside the agent's reach).

Nothing here trades. Results are historical and not forward-looking.
"""

from __future__ import annotations

import fcntl
import inspect
import logging
import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, NotRequired, TypedDict, TypeVar

import optuna
import pandas as pd
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from lab import portfolio_runner as pr
from lab.config import LabConfig, load_config
from lab.data.derivatives_columns import plan_derivative_columns, require_plan_inputs
from lab.engine.splits import design_view, design_window, walk_forward_folds
from lab.ledger import SELECTION_SOURCES, Ledger
from lab.plan import parse_plan, plan_hash, plan_to_dict, tunable_params
from lab.plan import portfolio as pf
from lab.plan import validate_plan as _validate_plan
from lab.plan.validator import PlanValidationError, format_errors
from lab.registry import REGISTRY
from lab.registry.derivatives import DERIVATIVES_FEATURES
from lab.report import ledger_item, metrics_dict, num, seconds
from lab.research.contract import (
    SUCCESS_CRITERIA_PATH,
    ResearchConfigError,
    SuccessCriteria,
    load_research_config,
)
from lab.research.criteria import evaluate_plan, frozen_criteria
from lab.research.evidence import compact_report, record_report
from lab.research.family import (
    behaviour_family,
    tried_literals,
    tried_ranges,
    tried_ranges_portfolio,
    wf_lineage_check,
)
from lab.research.holdout import HoldoutRefusedError, run_holdout_check
from lab.research.logic import canonical_space, logic_hash
from lab.research.pipeline import adversarial_evidence, backtest_evidence, crosscheck_evidence
from lab.research.portfolio import (
    portfolio_adversarial_evidence,
    portfolio_backtest_evidence,
    portfolio_crosscheck_evidence,
    portfolio_family,
    portfolio_lineage_check,
    portfolio_space,
    portfolio_sweep_evidence,
    portfolio_walk_forward_evidence,
)
from lab.research.runs import (
    budget_status,
    check_derivatives_snapshot,
    check_lab_fingerprint,
    check_universe_manifest,
    holdout_budget,
    start_research_run,
    universe_manifest,
)
from lab.runner import (
    CandleLoader,
    DataUnavailableError,
    archive_cli_hint,
    default_loader,
    derivatives_lookback_errors,
    derivatives_period_caps,
    derivatives_series,
    gate_base_trial_check,
    plan_design_window,
)
from lab.search.optuna_search import optuna_search, walk_forward_optimize
from lab.types import Plan, Window, interval_seconds

__all__ = ["TOOL_NAMES", "SpaceRange", "build_server", "main"]

TOOL_NAMES: tuple[str, ...] = (
    "start_run",
    "list_features",
    "validate_plan",
    "run_backtest",
    "run_sweep",
    "walk_forward",
    "run_adversarial",
    "run_crosscheck",
    "get_ledger",
    "get_report",
    "get_budget",
    "holdout_check",
)
MAX_LEDGER_PAGE = 500
MAX_LABEL_LEN = 200
DERIVATIVES_GATE_NOTE = (
    "exposure or crash-risk filter, not a direction signal: may only gate long_entry as an "
    "`and` conjunct beside a price signal (e.g. close > ema_slow and funding_zscore < 2), never "
    "in long_exit"
)

INSTRUCTIONS = """\
strategy-lab: an honest crypto backtesting lab. Nothing here trades.
Loop: start_run(label) once -> list_features -> validate_plan until valid -> run_backtest ->
run_sweep (explicit ranges) / walk_forward (must cover every value already tried) ->
run_adversarial ->
run_crosscheck -> get_report (criteria rows, frozen at start_run) -> holdout_check only when
get_report says pre_holdout_passed.
Plans contain no prices or levels: validate_plan rejects comparing a price- or volume-scale
value with a fixed number (close > 50000, volume > 1000); compare like with like
(close > ema_slow, volume > 2 * vol_avg) or use ratios (close / ema_slow > 1.02, rsi < 30).
walk_forward needs a run_backtest of the exact plan first; get_report's PBO needs a run_sweep
with enough trials. Rule thresholds are fixed hypotheses: hand-tuning a literal across plans
(rsi < 30, then rsi < 25) blocks walk_forward for that logic (any name, any params) and for
any plan whose design-window returns correlate with one of its plans at or above lab.toml
[research] literal_lineage_corr (0.95 by default; e.g. a no-op or cosmetic copy). A different
logic that merely trends alike (below that bar) is not blocked by those literals.
holdout_check needs a matched run_crosscheck first.
Derivatives features (list_features role "gate": funding, premium, basis, open interest,
long/short ratio) are exposure or crash-risk filters, not direction signals: they may only gate
long_entry as an `and` conjunct beside a price signal (close > ema_slow and funding_zscore < 2),
never in long_exit, and the price clause must select on its own (a plan whose price clauses
alone are true on >= 90% of bars, e.g. close > 0, is refused before any trial; every
run_sweep / walk_forward trial is checked the same way on its own params, and a trial that
fails is a ledgered failed trial, never selected). run_backtest / get_report add an ungated
baseline (the same plan without its derivatives clauses, same window and costs) and the gate's
binding share as information only: not a criterion, not a trial, never holdout-checkable, so
never submit a price-only twin to compare. Such plans are
scored from ONE start per symbol/interval shared by every derivatives plan (the latest start of
its derivatives archive series + [derivatives] warmup_days, frozen with the run; report
"scoring" block; benchmarks and folds start there too), so a derivatives feature's period x bar
length must fit in warmup_days (e.g. funding_zscore period <= 180 on 4h with 30 days).
start_run freezes the derivatives archive per series; re-downloading or editing a series the
plan reads, or a changed [derivatives] setting, needs a new run for such plans.
Portfolio plans ({"kind": "portfolio", ...}; list_features shows the schema): long-only top
quantile of the lab.toml [universe] (point-in-time top-N coins by trailing dollar volume), same
tools and budgets; min_trades counts filled rebalances; benchmarks are the equal-weight
universe, BTC buy-and-hold, vol-targeted BTC and a random-selection null, all scored from the
plan's first decision (the first rebalance on which it could have invested, gate or not);
rebalance.offset is fixed by the lab at 0 (run_adversarial's other-weekday table is timing
luck, never resubmittable); get_report adds beat_equal_weight and alpha-to-BTC rows
([portfolio] criteria; missing without them).
Plans that trade alike are ONE hypothesis: the same logic re-tuned or on any interval, and
plans whose design-window returns correlate at or above the family threshold
([research] family_return_corr, 0.8 by default; aligned by timestamp across intervals, so a
no-op clause or a stop that never triggers does not by itself make a new hypothesis). They
share walk-forward history
and a single holdout check, in every run; a same-feature plan whose returns cannot be
compared is treated as the same hypothesis for the holdout (fail closed).
Rules: every call is a ledger trial and raises the deflated-Sharpe bar; budgets (proposals,
backtests, holdout checks) are enforced server-side, see get_budget. All research uses the
design window; the holdout is reachable only via holdout_check, which returns pass/fail only.
A hypothesis gets one holdout check ever (renaming or re-tuning the same logic, or a copy that
trades alike as above, does not make a new one); if it fails it is dead. "No edge found" is a
correct outcome.
Results are historical and not forward-looking."""

_T = TypeVar("_T")
_F = TypeVar("_F", bound=Callable[..., Any])
_RUN_ID_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TINY = 1e-9  # floor for |mid| so a range around 0 still needs a positive width
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


class SpaceRange(TypedDict):
    """Search range of one tunable param (``validate_plan`` lists the keys and bounds)."""

    low: float
    high: float
    step: NotRequired[float | None]  # only None (or 1 for int params) is supported


# --------------------------------------------------------------------------- helpers


def _portfolio_or_error(raw: Mapping[str, Any], cfg: LabConfig) -> tuple[pf.PortfolioPlan, str]:
    try:
        plan = pr.parse_portfolio_plan(raw, cfg)
    except PlanValidationError as exc:
        raise ToolError(
            "portfolio plan is invalid; fix these and resubmit (validate_plan is free):\n"
            + format_errors(exc.errors)
        ) from None
    return plan, pf.portfolio_plan_hash(plan)


def _plan_or_error(raw: Mapping[str, Any]) -> tuple[Plan, str]:
    try:
        plan = parse_plan(raw)
    except PlanValidationError as exc:
        raise ToolError(
            "plan is invalid; fix these and resubmit (validate_plan is free):\n"
            + format_errors(exc.errors)
        ) from None
    return plan, plan_hash(plan)


def _design(
    plan: Plan,
    cfg: LabConfig,
    loader: CandleLoader,
    series: Mapping[str, Any] | None = None,
) -> tuple[pd.DataFrame, Window, dict[str, Any] | None]:
    """Design-view candles (every bar < holdout_start), the plan's SCORED design window, and
    its ``scoring`` block: the window ``run_backtest_report`` uses (``[max(design_start, first
    bar), min(holdout_start, last bar end))``; for a derivatives plan from the one scored start
    of its symbol/interval, from ``series`` (the run's frozen snapshot entries,
    :func:`lab.runner.derivatives_series`), after the trivially-true-price-clause check,
    :func:`lab.runner.plan_design_window`), computed once per call (a walk-forward's folds all
    start from it, identical for every derivatives plan in the run). Every refusal is a
    ToolError before any ledger row."""
    try:
        raw = loader(plan.symbol, plan.interval, cfg.splits.holdout_start)
    except DataUnavailableError as exc:
        raise ToolError(str(exc)) from None
    candles = design_view(raw, cfg.splits)
    if len(candles) >= 2:
        start = max(cfg.splits.design_start, candles.index[0])
        last_end = candles.index[-1] + pd.Timedelta(seconds=interval_seconds(plan.interval))
        end = min(cfg.splits.holdout_start, last_end)
        if end > start and int(((candles.index >= start) & (candles.index < end)).sum()) >= 2:
            try:
                window, scoring = plan_design_window(plan, candles, cfg, series=series)
            except DataUnavailableError as exc:
                raise ToolError(str(exc)) from None
            except PlanValidationError as exc:
                raise ToolError(
                    "plan is refused; fix these and resubmit:\n" + format_errors(exc.errors)
                ) from None
            return candles, window, scoring
    raise ToolError(
        f"no {plan.symbol} {plan.interval} bars in the design window "
        f"[{cfg.splits.design_start.date()}, {cfg.splits.holdout_start.date()}); the owner can "
        f"download them with `{archive_cli_hint(plan.symbol, plan.interval)}`"
    )


def _folds(window: Window, cfg: LabConfig) -> list[Any]:
    s = cfg.splits
    return walk_forward_folds(
        window,
        train_months=s.wf_train_months,
        test_months=s.wf_test_months,
        step_months=s.wf_step_months,
        anchored=s.wf_anchored,
    )


def _window_json(w: Window) -> dict[str, Any]:
    return {"kind": w.kind, "start": seconds(w.start), "end": seconds(w.end)}


def _space(
    plan: Plan,
    space: Mapping[str, Mapping[str, Any]],
    criteria: SuccessCriteria,
    caps: Mapping[str, int] | None = None,
) -> dict[str, tuple[float, float]]:
    """Validate a search space: tunable keys only, no step (except 1 for ints), ``low < high``,
    width ``high - low >= 2 * neighbourhood_pct * max(|mid|, tiny)`` with the run's FROZEN
    ``neighbourhood_pct`` (a range narrower than the ±pct robustness band cannot show that the
    surface around the chosen value is smooth), and a derivatives look-back's ``high`` within
    ``caps`` (:func:`lab.runner.derivatives_period_caps`: ``[derivatives] warmup_days``)."""
    if not space:
        raise ToolError(
            "space must name at least one tunable param: {key: {low, high}}; validate_plan "
            "lists the tunable keys and their bounds"
        )
    tunables = tunable_params(plan)
    pct = criteria.neighbourhood_pct
    out: dict[str, tuple[float, float]] = {}
    for key, rng in space.items():
        spec = tunables.get(key)
        if spec is None:
            raise ToolError(
                f"space key {key!r} is not a tunable param of this plan; tunable: "
                f"{', '.join(tunables) or '(none)'}"
            )
        step = rng.get("step")
        if step is not None and not (spec.kind == "int" and step == 1):
            raise ToolError(
                f"space[{key!r}].step = {step!r} is not supported: int params are searched at "
                "step 1 and float params continuously; omit step"
            )
        low, high = float(rng["low"]), float(rng["high"])
        if not low < high:
            raise ToolError(f"space[{key!r}] needs low < high, got low={low:g} high={high:g}")
        cap = (caps or {}).get(key)
        if cap is not None and high > cap:
            raise ToolError(
                f"space[{key!r}] high {high:g} is a derivatives look-back longer than lab.toml "
                f"[derivatives] warmup_days allows on {plan.interval} bars; use high <= {cap} "
                "(every derivatives plan is scored from one shared start that leaves that much "
                "warm-up)"
            )
        mid = (low + high) / 2.0
        need = 2.0 * pct * max(abs(mid), _TINY)
        if high - low < need:
            raise ToolError(
                f"space[{key!r}] = [{low:g}, {high:g}] is {high - low:g} wide; this run's frozen "
                f"neighbourhood_pct {pct:g} needs at least {need:g} (2 x {pct:g} x |mid| "
                f"{abs(mid):g}). Search a wider range"
            )
        out[key] = (rng["low"], rng["high"])
    return out


def _lineage(
    led: Ledger,
    plan: Plan,
    h: str,
    rng: Mapping[str, tuple[float, float]],
    cfg: LabConfig,
    loader: CandleLoader | None = None,
) -> list[str]:
    """Refuse a walk-forward of ``plan`` (hash ``h``) without a base design-window backtest, or
    whose space ``rng`` does not cover what its behaviour family already tried, or whose family
    hand-tuned rule literals (module docstring; :func:`lab.research.family.wf_lineage_check`).
    Returns the lineage notes (e.g. a tried value clamped to a searchable bound)."""
    family = behaviour_family(
        led, symbol=plan.symbol, plan_hash=h, cfg=cfg, plan=plan, loader=loader
    )
    if family.base_trial_id is None:
        raise ToolError(
            f"walk_forward needs a run_backtest of this exact plan ({h[:12]}) first: its "
            "design-window returns identify which earlier trials belong to the same "
            "hypothesis, and the walk-forward space must cover what they tried"
        )
    problems, notes = wf_lineage_check(
        plan, rng, tried_ranges(led, family, plan), tried_literals(led, family, plan)
    )
    if problems:
        raise ToolError(
            "walk_forward must search at least every value this hypothesis's behaviour family "
            "already tried (backtests, neighbours, sweeps, and walk-forwards of this logic or of "
            "plans whose design-window returns correlate with it, in any run, the web UI's "
            "included), and neither this logic nor a near-identical one (returns correlated >= "
            "lab.toml [research] literal_lineage_corr) may have hand-tuned rule literals; "
            "otherwise its out-of-sample folds replay an in-sample choice: " + "; ".join(problems)
        )
    return notes


def _relabel_run_local_dsr(out: dict[str, Any]) -> dict[str, Any]:
    """``out`` with the report's DSR moved from ``stats.dsr`` to ``dsr_run_local`` plus a note:
    it was deflated with this run's trial count and variance only, so it is not the number the
    success criteria judge."""
    stats = out.get("stats")
    if not isinstance(stats, dict) or "dsr" not in stats:
        return out
    stats = dict(stats)
    dsr = stats.pop("dsr")
    return {
        **out,
        "stats": stats,
        "dsr_run_local": {
            "value": dsr,
            "note": (
                "deflated with THIS run's trial count and Sharpe variance only, so it is "
                "optimistic; get_report's dsr_min row (N = distinct plans across every research "
                "run, V[SR] = max of this run's and every research run's) is authoritative"
            ),
        },
    }


# --------------------------------------------------------------------------- server


def build_server(
    *,
    cfg: LabConfig | None = None,
    ledger_path: Path | str | None = None,
    loader: CandleLoader | None = None,
    research_config_path: Path | str = SUCCESS_CRITERIA_PATH,
    pinned_run_id: str | None = None,
    holdout_enabled: bool = True,
) -> MCPServer:
    """Build the server. Tests inject ``cfg``, a tmp ``ledger_path``, a synthetic ``loader``,
    and a tmp criteria file; :func:`main` passes the real ones."""
    config = cfg if cfg is not None else load_config()
    db_path = Path(ledger_path) if ledger_path is not None else config.ledger_path
    load = loader or default_loader(config)
    lock = threading.Lock()  # one lab call at a time: budget check + run must not interleave
    lock_dir = db_path.parent / "locks"
    server = MCPServer("strategy-lab", instructions=INSTRUCTIONS)

    def tool(annotations: ToolAnnotations) -> Callable[[_F], _F]:
        """Register ``fn`` as a tool described by its dedented docstring. Every docstring has
        the audited shape (purpose, Use when, Not for, Params, Example, Limits; checked by
        tests/test_mcp_descriptions.py), so the client sees it without Python indentation."""

        def deco(fn: _F) -> _F:
            text = inspect.cleandoc(fn.__doc__ or "")
            return server.tool(annotations=annotations, description=text)(fn)

        return deco

    @contextmanager
    def run_lock(run_id: str) -> Iterator[None]:
        """This process's thread lock plus an exclusive flock on the run's lock file, so guard +
        run never interleave with another thread or another server process on this ledger."""
        if not _RUN_ID_SAFE.fullmatch(run_id):
            raise ToolError(f"run_id {run_id!r} is not a valid research run id")
        with lock:
            lock_dir.mkdir(parents=True, exist_ok=True)
            with open(lock_dir / f"{run_id}.lock", "a+b") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def open_ledger() -> Iterator[Ledger]:
        led = Ledger(db_path)  # one connection per call, on the worker thread serving it
        try:
            yield led
        finally:
            led.close()

    def check_run(led: Ledger, run_id: str) -> None:
        if pinned_run_id is not None and run_id != pinned_run_id:
            raise ToolError(
                f"this server is pinned to research run {pinned_run_id}; use that run_id"
            )
        if led.research_run(run_id) is None:
            raise ToolError(f"research run {run_id!r} was never started; call start_run first")
        try:
            check_lab_fingerprint(led, run_id, config)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    def check_universe(led: Ledger, run_id: str) -> None:
        """Portfolio calls: the run's frozen universe manifest must match the one on disk."""
        try:
            check_universe_manifest(led, run_id, config)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    def check_derivatives(led: Ledger, run_id: str, plan: Plan) -> None:
        """Plans that read a derivatives column: the run's frozen derivatives snapshot of the
        series this plan reads (manifest entries, content on disk, staleness rule) must match
        the archive (:func:`lab.research.runs.check_derivatives_snapshot`). Checked before any
        trial is recorded; plans without derivatives features skip it."""
        if not plan_derivative_columns(plan):
            return
        try:
            check_derivatives_snapshot(led, run_id, config, plan)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    def require_inputs(plan: Plan, candles: pd.DataFrame) -> None:
        """A derivatives plan's ``drv_*`` inputs must be loaded (before anything is recorded):
        a missing download is a ToolError naming the CLI, never an error trial."""
        try:
            require_plan_inputs(plan, candles, symbol=plan.symbol, interval=plan.interval)
        except DataUnavailableError as exc:
            raise ToolError(str(exc)) from None

    def guard(led: Ledger, run_id: str, h: str, cost: int, what: str) -> None:
        """Refuse (before anything runs or is recorded) when ``what`` would exceed the run's
        frozen budget. ``cost`` is the worst-case number of ledger trials it records."""
        status = budget_status(led, run_id, config)
        limits, used, left = status["budget"], status["used"], status["remaining"]
        known = {e["plan_hash"] for e in led.evidence(run_id)}
        if h not in known and used["proposals"] >= limits["proposals"]:
            raise ToolError(
                f"proposal budget exhausted: {used['proposals']} of {limits['proposals']} "
                f"distinct plans already submitted in run {run_id}; this plan ({h[:12]}) is "
                "new. Work with an already-submitted plan or end the run (no edge found is a "
                "correct outcome)."
            )
        if used["backtests"] >= limits["backtests"]:
            raise ToolError(
                f"backtest budget exhausted: {used['backtests']} of {limits['backtests']} "
                f"ledger trials used in run {run_id}"
            )
        if cost > left["backtests"]:
            raise ToolError(
                f"{what} would record up to {cost} ledger trials but only {left['backtests']} "
                f"of {limits['backtests']} remain in run {run_id}; ask for fewer trials"
            )

    def lab_call(fn: Callable[[], _T]) -> _T:
        try:
            return fn()
        except ToolError:
            raise
        except PlanValidationError as exc:
            raise ToolError("plan is invalid:\n" + format_errors(exc.errors)) from None
        except DataUnavailableError as exc:
            raise ToolError(str(exc)) from None
        except ValueError as exc:
            raise ToolError(f"lab call failed: {exc}") from None

    # ------------------------------------------------------------------ run lifecycle

    @tool(_WRITES)
    def start_run(label: str) -> dict[str, Any]:
        """Start a research run: freeze the owner's success criteria and budget (with their
        sha256), the lab.toml fingerprint, the universe manifest and the derivatives archive
        snapshot into the ledger, and return the server-assigned run_id.
        Use when: once, before any run-scoped tool; every later call passes this run_id.
        Not for: resetting budgets or the DSR trial count (N spans every research run); a
        server pinned by STRATEGY_LAB_RUN_ID refuses it (use the pinned run_id).
        Params: label — short human name for the run, 1-200 characters.
        Example: start_run(label="btc-4h-trend-pullback")
        Limits: refused without lab/config/success_criteria.toml (no criteria, no research);
        the frozen criteria (including dsr_trial_count; only "nominal" is accepted) and budget
        never change mid-run."""
        if pinned_run_id is not None:
            raise ToolError(
                f"this server is pinned to research run {pinned_run_id}; use that run_id "
                "instead of starting a new run"
            )
        text = label.strip()
        if not text or len(text) > MAX_LABEL_LEN:
            raise ToolError(f"label must be 1-{MAX_LABEL_LEN} characters")
        try:
            research = load_research_config(research_config_path)
        except ResearchConfigError as exc:
            raise ToolError(f"cannot start a research run without a success bar: {exc}") from None
        with lock, open_ledger() as led:
            run_id = start_research_run(led, research, label=text, cfg=config)
            hb = holdout_budget(led, run_id, config)
        out: dict[str, Any] = {
            "run_id": run_id,
            "criteria": asdict(research.criteria),
            "budget": asdict(research.budget),
            "holdout_budget": hb,
            "config_sha256": research.sha256,
        }
        if research.portfolio is not None:
            out["portfolio_criteria"] = asdict(research.portfolio)
        manifest = universe_manifest(config)
        if manifest is not None:
            out["universe_manifest_sha256"] = manifest["sha256"]
        with open_ledger() as led:
            run = led.research_run(run_id)
        frozen = run["config"].get("derivatives") if run is not None else None
        out["derivatives_manifest_sha256"] = (
            frozen.get("manifest_sha256") if isinstance(frozen, dict) else None
        )
        return out

    # ------------------------------------------------------------------ free tools

    @tool(_READ_ONLY)
    def list_features() -> dict[str, Any]:
        """List every registry feature a plan may use (name, description, outputs, params with
        kind, default and inclusive low/high bounds) plus the portfolio plan schema.
        Use when: designing a plan, before validate_plan; plans may use these features only.
        Not for: checking a plan (validate_plan) or adding features (a human adds registry
        features, with look-ahead tests).
        Params: none.
        Example: list_features()
        Limits: free (no ledger trial, no budget). Features with "role": "gate" (funding,
        premium, basis, open interest, long/short ratio) are exposure filters, not signals:
        only an `and` conjunct of long_entry beside a price signal, never in long_exit."""
        return {
            "features": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    **(
                        {"role": "gate", "note": DERIVATIVES_GATE_NOTE}
                        if spec.name in DERIVATIVES_FEATURES
                        else {}
                    ),
                    "outputs": list(spec.outputs),
                    "params": [
                        {
                            "name": p.name,
                            "kind": p.kind,
                            "default": p.default,
                            "low": p.low,
                            "high": p.high,
                            "description": p.description,
                        }
                        for p in spec.params
                    ],
                }
                for spec in REGISTRY.values()
            ],
            "portfolio": {
                "note": (
                    'A portfolio plan has "kind": "portfolio" and none of the single-symbol keys: '
                    "a long-only top quantile of the lab.toml [universe] (point-in-time top-"
                    f"{config.universe.top_n} {config.universe.quote} coins by trailing dollar "
                    f"volume, interval {config.universe.interval}), ranked on a score feature, "
                    "rebalanced every N rows; optional rank banding (selection.exit_quantile) "
                    "and dispersion gate. Weights are capped at "
                    f"{config.universe.liquidity_cap_mult:g}x each coin's liquidity share."
                ),
                "score_features": [
                    {
                        "name": s.name,
                        "description": s.description,
                        "lookback_low": s.lookback_low,
                        "lookback_high": s.lookback_high,
                    }
                    for s in pf.SCORE_FEATURES.values()
                ],
                "schema": pf.portfolio_plan_json_schema(),
            },
        }

    @tool(_READ_ONLY)
    def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
        """Validate a plan: return errors with JSON paths, or its plan_hash and tunable params
        (the space keys run_sweep / walk_forward accept, with bounds).
        Use when: after every plan edit, until valid, before any run_* tool.
        Not for: evidence of performance; it runs nothing.
        Params: plan — plan JSON: name, symbol, interval, features, long_entry, long_exit,
        optional risk; or {"kind": "portfolio", ...} (schema in list_features).
        Example: validate_plan(plan={"name": "ema_trend", "symbol": "BTCUSDT", "interval":
        "4h", "features": {"f": {"ema": {"period": 20}}, "s": {"ema": {"period": 50}}},
        "long_entry": "f > s", "long_exit": "f < s"})
        Limits: free (no ledger trial, no budget). No prices or levels: comparing a price- or
        volume-scale value with a fixed number (close > 50000) is rejected; use ratios or like
        with like. A derivatives look-back must fit [derivatives] warmup_days. Portfolio plans
        are validated on the universe interval; rebalance.offset is fixed at 0."""
        if pf.is_portfolio_plan(plan):
            res = pf.validate_portfolio_plan(plan, interval=config.universe.interval)
            if res.plan is None:
                return {
                    "valid": False,
                    "kind": pf.PORTFOLIO_KIND,
                    "errors": [
                        {"path": e.partition(": ")[0], "message": e.partition(": ")[2] or e}
                        for e in res.errors
                    ],
                }
            pinned = pf.pinned_offset_errors(res.plan)
            if pinned:  # the lab pins the rebalance weekday; every portfolio tool refuses too
                return {
                    "valid": False,
                    "kind": pf.PORTFOLIO_KIND,
                    "errors": [{"path": e.path, "message": e.message} for e in pinned],
                }
            specs = pr.portfolio_param_specs(res.plan)
            return {
                "valid": True,
                "kind": pf.PORTFOLIO_KIND,
                "errors": [],
                "plan_hash": pf.portfolio_plan_hash(res.plan),
                "plan_logic": pf.plan_logic(res.plan),
                "tunable_params": {
                    k: {"kind": s.kind, "default": s.default, "low": s.low, "high": s.high}
                    for k, s in specs.items()
                },
                "note": (
                    "rebalance.offset is fixed by the lab at 0 (not tunable, not "
                    "resubmittable); a sweep must keep "
                    "selection.exit_quantile >= selection.quantile (other combinations are "
                    "pruned as infeasible, still ledgered)"
                ),
            }
        parsed, errors = _validate_plan(plan)
        if parsed is not None:  # a derivatives look-back must fit in [derivatives] warmup_days
            errors = derivatives_lookback_errors(parsed, config)
            if errors:
                parsed = None
        if parsed is None:
            return {
                "valid": False,
                "errors": [{"path": e.path, "message": e.message} for e in errors],
            }
        caps = derivatives_period_caps(parsed, config)
        return {
            "valid": True,
            "errors": [],
            "plan_hash": plan_hash(parsed),
            "tunable_params": {
                k: {
                    "kind": s.kind,
                    "default": s.default,
                    "low": s.low,
                    "high": float(min(s.high, caps[k])) if k in caps else s.high,
                    "description": s.description,
                }
                for k, s in tunable_params(parsed).items()
            },
        }

    # ------------------------------------------------------------------ lab runs

    def backtest_cost() -> int:
        try:
            n_folds = len(_folds(design_window(config.splits), config))
        except ValueError:
            n_folds = 0
        # base + 3 benchmark rows + neighbourhood grid + fixed-param walk-forward folds
        return 1 + 3 + max(0, config.stats.neighbourhood_max_trials - 1) + n_folds

    def adversarial_cost() -> int:
        # base + delays + sign flip + cost stress + permutation-null summary row
        return 1 + len(config.stats.adversarial_delays) + 1 + len(config.stress_factors) + 1

    @tool(_WRITES)
    def run_backtest(run_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Backtest a plan on the design window, recording ledger trials and evidence for
        get_report.
        Use when: first test of each validated plan; walk_forward and run_crosscheck need it
        for the exact plan.
        Not for: tuning (run_sweep), judging (get_report), or the holdout (holdout_check).
        Params: run_id — from start_run; plan — the plan JSON as validated.
        Example: run_backtest(run_id="r-20261028-120000-a1b2c3", plan={...})
        Limits: records base + 3 benchmark + ±20% neighbour + fixed-param fold trials (refused
        before running if over budget); each raises the DSR bar. Returns net/gross metrics,
        benchmarks (buy-and-hold, vol-targeted, random entry), PBO, folds, regimes, warnings;
        dsr_run_local is optimistic, get_report's DSR row is authoritative. Derivatives plans
        add an ungated baseline and the gate's binding share as information only (never submit
        a price-only twin). Portfolio: equal-weight, BTC, vol-targeted BTC and random-selection
        benchmarks from the first decision, alpha/beta to BTC, turnover, costs, gate state."""
        if pf.is_portfolio_plan(plan):
            _, ph = _portfolio_or_error(plan, config)
            with open_ledger() as led:
                check_run(led, run_id)
            with run_lock(run_id), open_ledger() as led:
                check_run(led, run_id)
                check_universe(led, run_id)
                guard(led, run_id, ph, pr.backtest_cost(config), "run_backtest")
                out = lab_call(
                    lambda: portfolio_backtest_evidence(
                        plan, run_id=run_id, cfg=config, ledger=led, loader=load
                    )
                )
                out = _relabel_run_local_dsr(out)
                out["budget"] = budget_status(led, run_id, config)
            return out
        parsed, h = _plan_or_error(plan)
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
            check_derivatives(led, run_id, parsed)
            guard(led, run_id, h, backtest_cost(), "run_backtest")
            out = lab_call(
                lambda: backtest_evidence(plan, run_id=run_id, cfg=config, ledger=led, loader=load)
            )
            out = _relabel_run_local_dsr(out)
            out["budget"] = budget_status(led, run_id, config)
        return out

    @tool(_WRITES)
    def run_adversarial(run_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Stress a plan on the design window: signal delay 1/3/5 bars, sign flip, cost
        stress, Monte Carlo trade reshuffle and a permutation null.
        Use when: after run_backtest / walk_forward, before run_crosscheck; get_report's delay
        and cost-stress rows need it.
        Not for: finding better variants; results are evidence, not candidates.
        Params: run_id — from start_run; plan — the plan JSON as validated.
        Example: run_adversarial(run_id="r-20261028-120000-a1b2c3", plan={...})
        Limits: every variant is a ledger trial (refused before running if over budget).
        Portfolio: stale inputs 1/3/5 rebalances old, cost stress (fees, slippage, impact) and
        a descriptive timing-luck table of other rebalance weekdays (rebalance.offset is
        pinned at 0, so those variants cannot be resubmitted)."""
        if pf.is_portfolio_plan(plan):
            pplan, ph = _portfolio_or_error(plan, config)
            with open_ledger() as led:
                check_run(led, run_id)
            with run_lock(run_id), open_ledger() as led:
                check_run(led, run_id)
                check_universe(led, run_id)
                guard(led, run_id, ph, pr.adversarial_cost(config, pplan), "run_adversarial")
                out = lab_call(
                    lambda: portfolio_adversarial_evidence(
                        plan, run_id=run_id, cfg=config, ledger=led, loader=load
                    )
                )
                out["budget"] = budget_status(led, run_id, config)
            return out
        parsed, h = _plan_or_error(plan)
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
            check_derivatives(led, run_id, parsed)
            guard(led, run_id, h, adversarial_cost(), "run_adversarial")
            out = lab_call(
                lambda: adversarial_evidence(
                    plan, run_id=run_id, cfg=config, ledger=led, loader=load
                )
            )
            out["budget"] = budget_status(led, run_id, config)
        return out

    @tool(_WRITES)
    def run_crosscheck(run_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Re-run a backtested plan in the event-driven engine (nautilus_trader) on the same
        design window and compare its trades and returns with the vectorbt engine.
        Use when: a plan looks promising, before get_report; holdout_check needs matched=true.
        Not for: plans without run_backtest evidence for this exact plan (refused).
        Params: run_id — from start_run; plan — the exact plan JSON that was backtested.
        Example: run_crosscheck(run_id="r-20261028-120000-a1b2c3", plan={...})
        Limits: counts as 1 backtest for the budget. matched=true only when the engine is
        installed and both agree. Portfolio: an independent accounting replay of the same
        weights, candles, costs and impact must match within 1e-8 (relative); it checks
        accounting only, not signal look-ahead."""
        if pf.is_portfolio_plan(plan):
            _, ph = _portfolio_or_error(plan, config)
            with open_ledger() as led:
                check_run(led, run_id)
            with run_lock(run_id), open_ledger() as led:
                check_run(led, run_id)
                check_universe(led, run_id)
                guard(led, run_id, ph, 1, "run_crosscheck")
                out = lab_call(
                    lambda: portfolio_crosscheck_evidence(
                        plan, run_id=run_id, cfg=config, ledger=led, loader=load
                    )
                )
                out["budget"] = budget_status(led, run_id, config)
            return out
        parsed, h = _plan_or_error(plan)
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
            check_derivatives(led, run_id, parsed)
            guard(led, run_id, h, 1, "run_crosscheck")
            out = lab_call(
                lambda: crosscheck_evidence(
                    plan, run_id=run_id, cfg=config, ledger=led, loader=load
                )
            )
            out["budget"] = budget_status(led, run_id, config)
        return out

    @tool(_WRITES)
    def run_sweep(
        run_id: str, plan: dict[str, Any], space: dict[str, SpaceRange], n_trials: int
    ) -> dict[str, Any]:
        """Search a plan's tunable params with seeded Optuna TPE, maximising net Sharpe on the
        design window over explicit ranges.
        Use when: after run_backtest, to tune numbers; get_report's PBO needs enough trials.
        Not for: rule literals (thresholds in long_entry/long_exit are fixed hypotheses) or
        judging (best_sharpe is the max of n_trials and biased upward; use walk_forward).
        Params: run_id — from start_run; plan — the plan JSON as validated; space —
        {tunable key from validate_plan: {"low", "high"}}; n_trials — trials to run (>= 1).
        Example: run_sweep(run_id="r-20261028-120000-a1b2c3", plan={...},
        space={"ema_fast.period": {"low": 10, "high": 30}}, n_trials=40)
        Limits: low < high and width >= 2 x neighbourhood_pct x |mid|; every trial is a
        ledger trial and raises the DSR bar; refused if n_trials exceeds the backtests left.
        Trials below min trades are never selected; derivatives trials whose price clause
        fails are ledgered as failed."""
        if pf.is_portfolio_plan(plan):
            pplan, ph = _portfolio_or_error(plan, config)
            if isinstance(n_trials, bool) or n_trials < 1:
                raise ToolError(f"n_trials must be a positive integer, got {n_trials!r}")
            with open_ledger() as led:
                check_run(led, run_id)
            with run_lock(run_id), open_ledger() as led:
                check_run(led, run_id)
                check_universe(led, run_id)
                try:
                    prng = portfolio_space(
                        pplan, space, frozen_criteria(led, run_id).neighbourhood_pct
                    )
                except ValueError as exc:
                    raise ToolError(str(exc)) from None
                guard(led, run_id, ph, n_trials, f"run_sweep with n_trials={n_trials}")
                out = lab_call(
                    lambda: portfolio_sweep_evidence(
                        plan, prng, n_trials, run_id=run_id, cfg=config, ledger=led, loader=load
                    )
                )
                out["budget"] = budget_status(led, run_id, config)
            return out
        parsed, h = _plan_or_error(plan)
        lh = logic_hash(parsed)
        if isinstance(n_trials, bool) or n_trials < 1:
            raise ToolError(f"n_trials must be a positive integer, got {n_trials!r}")
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
            check_derivatives(led, run_id, parsed)
            rng = _space(
                parsed,
                space,
                frozen_criteria(led, run_id),
                derivatives_period_caps(parsed, config),
            )
            guard(led, run_id, h, n_trials, f"run_sweep with n_trials={n_trials}")
            candles, window, scoring = _design(
                parsed, config, load, derivatives_series(config, led, run_id)
            )
            require_inputs(parsed, candles)
            # derivatives plans: every trial's price clause must still select bars
            check = gate_base_trial_check(candles, window, config) if scoring else None
            res = lab_call(
                lambda: optuna_search(
                    parsed,
                    candles,
                    window,
                    ledger=led,
                    run_id=run_id,
                    cfg=config,
                    n_trials=n_trials,
                    seed=config.optuna_seed,
                    space=rng,
                    min_trades=config.optuna_min_trades,
                    require_space=True,
                    trial_check=check,
                )
            )
            out: dict[str, Any] = {
                "run_id": run_id,
                "plan_hash": h,
                "logic_hash": lh,
                "plan": plan_to_dict(parsed),
                "study_name": res.study_name,
                "study": res.study_name,
                "window": _window_json(window),
                "space": {k: [lo, hi] for k, (lo, hi) in rng.items()},
                "space_canonical": canonical_space(parsed, rng),
                "seed": config.optuna_seed,
                "min_trades": config.optuna_min_trades,
                "n_trials": res.n_trials,
                "n_failed": res.n_failed,
                "best_params": res.best_params,
                "best_sharpe": num(res.best_value),
                "best_plan_hash": plan_hash(res.best_plan) if res.best_plan else None,
                "best_plan": plan_to_dict(res.best_plan) if res.best_plan else None,
                "dsr_n": led.count_distinct_plans_research(sources=SELECTION_SOURCES),
                "note": (
                    "best_sharpe is the maximum of n_trials in-sample results and is biased "
                    "upward; judge the plan with walk_forward and get_report. Trials with "
                    f"fewer than {config.optuna_min_trades} trades are never selected."
                ),
            }
            if scoring is not None:
                out["scoring"] = scoring
            record_report(led, run_id, h, "sweep", out)
            out = compact_report(out)
            out["budget"] = budget_status(led, run_id, config)
        return out

    @tool(_WRITES)
    def walk_forward(
        run_id: str, plan: dict[str, Any], space: dict[str, SpaceRange], n_trials_per_fold: int
    ) -> dict[str, Any]:
        """Walk-forward optimisation inside the design window: per fold Optuna searches space
        on the train window only, then the chosen params run once on the test window.
        Use when: after run_backtest (and any run_sweep) of this exact plan; the stitched
        out-of-sample net Sharpe is what the success criteria judge.
        Not for: plans without a run_backtest of the exact plan, or logic whose rule literals
        were hand-tuned across plans (both refused).
        Params: run_id — from start_run; plan — the plan JSON as backtested; space —
        {tunable key: {"low", "high"}}; n_trials_per_fold — train trials per fold (>= 1).
        Example: walk_forward(run_id="r-20261028-120000-a1b2c3", plan={...},
        space={"ema_fast.period": {"low": 10, "high": 30}}, n_trials_per_fold=20)
        Limits: space must cover every value the behaviour family (same logic, or returns
        correlated >= [research] family_return_corr, any run) already tried; costs
        folds x (n_trials_per_fold + 1) ledger trials, refused if over budget."""
        if pf.is_portfolio_plan(plan):
            return portfolio_walk_forward_tool(run_id, plan, space, n_trials_per_fold)
        parsed, h = _plan_or_error(plan)
        lh = logic_hash(parsed)
        if isinstance(n_trials_per_fold, bool) or n_trials_per_fold < 1:
            raise ToolError(
                f"n_trials_per_fold must be a positive integer, got {n_trials_per_fold!r}"
            )
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
            check_derivatives(led, run_id, parsed)
            rng = _space(
                parsed,
                space,
                frozen_criteria(led, run_id),
                derivatives_period_caps(parsed, config),
            )
            candles, window, scoring = _design(
                parsed, config, load, derivatives_series(config, led, run_id)
            )
            require_inputs(parsed, candles)  # a missing download first, before lineage advice
            lineage_notes = _lineage(led, parsed, h, rng, config, load)
            try:
                folds = _folds(window, config)
            except ValueError as exc:
                raise ToolError(str(exc)) from None
            cost = (n_trials_per_fold + 1) * len(folds)
            # derivatives plans: every train trial's price clause must still select bars on
            # the scored design window (a failing trial is ledgered failed, never chosen)
            check = gate_base_trial_check(candles, window, config) if scoring else None
            guard(
                led,
                run_id,
                h,
                cost,
                f"walk_forward ({len(folds)} folds x ({n_trials_per_fold} train + 1 test))",
            )
            wf = lab_call(
                lambda: walk_forward_optimize(
                    parsed,
                    candles,
                    folds,
                    ledger=led,
                    run_id=run_id,
                    cfg=config,
                    n_trials_per_fold=n_trials_per_fold,
                    seed=config.optuna_seed,
                    space=rng,
                    min_trades=config.optuna_min_trades,
                    require_space=True,
                    trial_check=check,
                )
            )
            out: dict[str, Any] = {
                "run_id": run_id,
                "plan_hash": h,
                "logic_hash": lh,
                "plan": plan_to_dict(parsed),
                "study": [f.search.study_name for f in wf.folds],
                "window": _window_json(window),
                "space": {k: [lo, hi] for k, (lo, hi) in rng.items()},
                "space_canonical": canonical_space(parsed, rng),
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
                        "test_n_trades": int(f.test_metrics.n_trades),
                    }
                    for f in wf.folds
                ],
                "n_train_trials": wf.n_train_trials,
                "lineage_notes": lineage_notes,
                "boundary_charges": list(wf.boundary_charges),
                "note": (
                    "oos_metrics stitch every fold's test window (params chosen on train only), "
                    "net of costs; open positions are charged an exit at fold boundaries."
                ),
            }
            if scoring is not None:
                out["scoring"] = scoring
            record_report(led, run_id, h, "walk_forward", out)
            out = compact_report(out)
            out["budget"] = budget_status(led, run_id, config)
        return out

    def portfolio_walk_forward_tool(
        run_id: str, plan: dict[str, Any], space: dict[str, SpaceRange], n_trials_per_fold: int
    ) -> dict[str, Any]:
        pplan, ph = _portfolio_or_error(plan, config)
        if isinstance(n_trials_per_fold, bool) or n_trials_per_fold < 1:
            raise ToolError(
                f"n_trials_per_fold must be a positive integer, got {n_trials_per_fold!r}"
            )
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
            check_universe(led, run_id)
            try:
                prng = portfolio_space(pplan, space, frozen_criteria(led, run_id).neighbourhood_pct)
            except ValueError as exc:
                raise ToolError(str(exc)) from None
            family = portfolio_family(led, pplan, ph, config, load)
            if family.base_trial_id is None:
                raise ToolError(
                    f"walk_forward needs a run_backtest of this exact plan ({ph[:12]}) first: its "
                    "design-window returns identify which earlier trials belong to the same "
                    "hypothesis, and the walk-forward space must cover what they tried"
                )
            ranges = tried_ranges_portfolio(led, family, pplan, pr.universe_symbol(config))
            problems, notes = portfolio_lineage_check(pplan, prng, ranges)
            if problems:
                raise ToolError(
                    "walk_forward must search at least every value this hypothesis's behaviour "
                    "family already tried (backtests, neighbours, sweeps, and walk-forwards of "
                    "this logic or of plans whose design-window returns correlate with it, in any "
                    "run); otherwise its out-of-sample folds replay an in-sample choice: "
                    + "; ".join(problems)
                )

            def n_folds() -> int:
                data = pr.load_universe_data(config, load, end=config.splits.holdout_start)
                return len(pr.portfolio_folds(pr.design_window(data, config), config))

            folds = lab_call(n_folds)
            if folds == 0:
                raise ToolError("no walk-forward fold fits in the portfolio design window")
            cost = pr.walk_forward_cost(folds, n_trials_per_fold)
            guard(
                led,
                run_id,
                ph,
                cost,
                f"walk_forward ({folds} folds x ({n_trials_per_fold} train + 1 test))",
            )
            out = lab_call(
                lambda: portfolio_walk_forward_evidence(
                    plan,
                    prng,
                    n_trials_per_fold,
                    run_id=run_id,
                    cfg=config,
                    ledger=led,
                    loader=load,
                    lineage_notes=notes,
                )
            )
            out["budget"] = budget_status(led, run_id, config)
        return out

    # ------------------------------------------------------------------ reads

    @tool(_READ_ONLY)
    def get_ledger(run_id: str, limit: int = 50, verify_chain: bool = False) -> dict[str, Any]:
        """List this run's trials newest first as summary rows (no returns), with counts by
        source and status and the DSR trial count N.
        Use when: auditing what was tried, for the journal's trial links, or to prove the
        ledger is untampered (verify_chain).
        Not for: judging a plan (get_report) or budgets (get_budget).
        Params: run_id — from start_run; limit — rows to return, 0-500 (default 50);
        verify_chain — true to add "chain": the run's trial hash-chain check (ok, n_trials,
        head, first_bad_trial_id, error).
        Example: get_ledger(run_id="r-20261028-120000-a1b2c3", limit=20, verify_chain=True)
        Limits: read-only, free. Holdout trials are never listed. dsr_n counts distinct plans
        across every research run; a new run does not reset it."""
        if isinstance(limit, bool) or not 0 <= limit <= MAX_LEDGER_PAGE:
            raise ToolError(f"limit must be 0-{MAX_LEDGER_PAGE}, got {limit!r}")
        with open_ledger() as led:
            check_run(led, run_id)
            recs = [
                r
                for r in led.list(run_id=run_id, limit=led.count(run_id))
                if r.window_kind != "holdout"
            ]
            by_source: dict[str, int] = {}
            by_status: dict[str, int] = {}
            for r in recs:
                by_source[r.source] = by_source.get(r.source, 0) + 1
                by_status[r.status] = by_status.get(r.status, 0) + 1
            out: dict[str, Any] = {
                "run_id": run_id,
                "total": len(recs),
                "by_source": by_source,
                "by_status": by_status,
                "dsr_n": led.count_distinct_plans_research(sources=SELECTION_SOURCES),
                "dsr_n_basis": (
                    "distinct plans tried across every research run (all symbols and "
                    "intervals); a new run does not reset it"
                ),
                "run_distinct_plans": led.count_distinct_plans(run_id, sources=SELECTION_SOURCES),
                "items": [ledger_item(r) for r in recs[:limit]],
            }
            if verify_chain:  # tamper evidence for the journal's "every trial in the ledger"
                out["chain"] = led.verify_chain(run_id)
            return out

    @tool(_READ_ONLY)
    def get_report(run_id: str, plan_hash: str) -> dict[str, Any]:
        """Judge a plan against the run's frozen success criteria: one row per criterion with
        pass / fail / missing, the DSR N, the budget, and a statistics block.
        Use when: after the backtest, sweep, walk-forward, adversarial and crosscheck
        evidence exists; pre_holdout_passed must be true before holdout_check.
        Not for: holdout results (never shown) or re-scoring with other thresholds.
        Params: run_id — from start_run; plan_hash — the plan_hash of the plan as submitted
        (from validate_plan or a run_* result).
        Example: get_report(run_id="r-20261028-120000-a1b2c3", plan_hash="3f9a...")
        Limits: read-only, free; criteria are the ones frozen at start_run. Missing evidence
        shows as missing. statistics (nominal N, an effective-N diagnostic beside its null,
        MinBTL, MinTRL, CPCV paths of the sweep: all trials, an upper bound, and random
        start-up trials) is information only, never pass/fail; dsr_min deflates with the
        nominal N. Derivatives plans add the ungated baseline as information only
        (not a criterion); portfolio plans add beat_equal_weight and alpha-to-BTC rows."""
        with open_ledger() as led:
            check_run(led, run_id)
            try:
                return dict(evaluate_plan(led, run_id, plan_hash, config, loader=load))
            except ValueError as exc:
                raise ToolError(str(exc)) from None

    @tool(_READ_ONLY)
    def get_budget(run_id: str) -> dict[str, Any]:
        """Show the run's frozen budget, what has been used, and what remains (proposals,
        backtests, holdout checks).
        Use when: before a sweep or walk-forward, to size n_trials to what is left.
        Not for: raising a budget; budgets are frozen at start_run.
        Params: run_id — from start_run.
        Example: get_budget(run_id="r-20261028-120000-a1b2c3")
        Limits: read-only, free. Budgets are enforced server-side: a call that could exceed
        them is refused before anything runs or is recorded."""
        with open_ledger() as led:
            check_run(led, run_id)
            return budget_status(led, run_id, config)

    # ------------------------------------------------------------------ holdout

    @tool(_WRITES)
    def holdout_check(run_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Spend one locked-holdout check on a plan that passes every pre-holdout criterion.
        Use when: get_report says pre_holdout_passed (including a matched run_crosscheck)
        and the strategy-critic review is done; the owner is asked each time.
        Not for: tuning or comparing plans; a failed hypothesis is dead, never re-tune on it.
        Params: run_id — from start_run; plan — the exact plan JSON get_report judged.
        Example: holdout_check(run_id="r-20261028-120000-a1b2c3", plan={...})
        Limits: returns pass/fail and checks left only (no metrics, not even which rule
        failed). One check per hypothesis ever, in any run: a renamed, re-tuned or
        alike-trading copy is the same hypothesis. Budgeted server-side; refused without
        spending if any guard fails; disabled when STRATEGY_LAB_DISABLE_HOLDOUT=1."""
        if not holdout_enabled:
            raise ToolError(
                "holdout_check is disabled on this server (STRATEGY_LAB_DISABLE_HOLDOUT=1): "
                "the orchestrator runs holdout checks itself. Report the candidate instead."
            )
        parsed: Plan | None = None
        if pf.is_portfolio_plan(plan):
            _portfolio_or_error(plan, config)
        else:
            parsed, _ = _plan_or_error(plan)
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
            if parsed is not None:
                try:
                    check_derivatives(led, run_id, parsed)
                except ToolError as exc:
                    raise ToolError(f"holdout check refused (no check spent): {exc}") from None
            try:
                return run_holdout_check(plan, run_id=run_id, cfg=config, ledger=led, loader=load)
            except HoldoutRefusedError as exc:
                raise ToolError(f"holdout check refused: {exc}") from None
            except DataUnavailableError as exc:
                raise ToolError(f"holdout check refused (no check spent): {exc}") from None

    return server


def main() -> None:
    """Serve on stdio with the real config, ledger, and success-criteria file."""
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    logging.basicConfig(level=logging.WARNING)  # stderr; stdout carries the MCP protocol
    pinned = os.environ.get("STRATEGY_LAB_RUN_ID") or None
    disable = os.environ.get("STRATEGY_LAB_DISABLE_HOLDOUT", "").strip().lower()
    server = build_server(
        cfg=load_config(),
        pinned_run_id=pinned,
        holdout_enabled=disable not in {"1", "true", "yes"},
    )
    server.run()


if __name__ == "__main__":
    main()
