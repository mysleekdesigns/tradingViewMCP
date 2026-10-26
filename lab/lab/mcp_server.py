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
* ``get_ledger``, ``get_report``, ``get_budget``: read the run (holdout trials hidden).
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
from lab.engine.splits import design_view, design_window, walk_forward_folds
from lab.ledger import SELECTION_SOURCES, Ledger
from lab.plan import parse_plan, plan_hash, plan_to_dict, tunable_params
from lab.plan import portfolio as pf
from lab.plan import validate_plan as _validate_plan
from lab.plan.validator import PlanValidationError, format_errors
from lab.registry import REGISTRY
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


def _design(plan: Plan, cfg: LabConfig, loader: CandleLoader) -> tuple[pd.DataFrame, Window]:
    """Design-view candles (every bar < holdout_start) and the plan's design window, the same
    window ``run_backtest_report`` uses: ``[max(design_start, first bar), min(holdout_start,
    last bar end))``."""
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
            return candles, Window(start=start, end=end, kind="design")
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
    plan: Plan, space: Mapping[str, Mapping[str, Any]], criteria: SuccessCriteria
) -> dict[str, tuple[float, float]]:
    """Validate a search space: tunable keys only, no step (except 1 for ints), ``low < high``,
    and width ``high - low >= 2 * neighbourhood_pct * max(|mid|, tiny)`` with the run's FROZEN
    ``neighbourhood_pct`` (a range narrower than the ±pct robustness band cannot show that the
    surface around the chosen value is smooth)."""
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

    @server.tool(annotations=_WRITES)
    def start_run(label: str) -> dict[str, Any]:
        """Start a research run: freezes the owner's success criteria and budget into the
        ledger and returns the server-assigned run_id to pass to every other tool."""
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
        return out

    # ------------------------------------------------------------------ free tools

    @server.tool(annotations=_READ_ONLY)
    def list_features() -> dict[str, Any]:
        """Every registry feature a plan may use: name, description, outputs, and params
        (kind, default, inclusive low/high bounds)."""
        return {
            "features": [
                {
                    "name": spec.name,
                    "description": spec.description,
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

    @server.tool(annotations=_READ_ONLY)
    def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
        """Validate a plan (free: no ledger trial, no budget). Returns errors with JSON paths,
        or the plan_hash and the tunable params (keys usable in run_sweep's space). Portfolio
        plans ("kind": "portfolio") are validated against the universe interval."""
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
        if parsed is None:
            return {
                "valid": False,
                "errors": [{"path": e.path, "message": e.message} for e in errors],
            }
        return {
            "valid": True,
            "errors": [],
            "plan_hash": plan_hash(parsed),
            "tunable_params": {
                k: {
                    "kind": s.kind,
                    "default": s.default,
                    "low": s.low,
                    "high": s.high,
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

    @server.tool(annotations=_WRITES)
    def run_backtest(run_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Backtest a plan on the design window: net/gross metrics, benchmarks (buy-and-hold,
        vol-targeted, random entry), a run-local DSR (dsr_run_local; get_report's DSR row, with
        the global trial count and variance, is authoritative), PBO over the ±20%
        neighbourhood, fixed-param walk-forward folds, regime breakdown, warnings. Every run is
        ledger trials; the report is evidence for get_report. Portfolio plans: equal-weight
        universe, BTC buy-and-hold, vol-targeted BTC and random-selection null benchmarks
        (all from the first decision), alpha/beta to BTC, turnover, fees and impact, segment
        exits, and dispersion + gate state per rebalance."""
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
        _, h = _plan_or_error(plan)
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
            guard(led, run_id, h, backtest_cost(), "run_backtest")
            out = lab_call(
                lambda: backtest_evidence(plan, run_id=run_id, cfg=config, ledger=led, loader=load)
            )
            out = _relabel_run_local_dsr(out)
            out["budget"] = budget_status(led, run_id, config)
        return out

    @server.tool(annotations=_WRITES)
    def run_adversarial(run_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Adversarial checks on the design window: signal delay 1/3/5 bars, sign flip, cost
        stress, Monte Carlo trade reshuffle, permutation null. Every variant is a ledger
        trial; the report is evidence for get_report. Portfolio plans: stale inputs 1/3/5
        rebalance periods old on the same rebalance rows, cost stress (fees, slippage and
        impact), and a descriptive timing-luck table of the other rebalance weekdays (the lab
        pins rebalance.offset at 0, so those variants cannot be resubmitted)."""
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
        _, h = _plan_or_error(plan)
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
            guard(led, run_id, h, adversarial_cost(), "run_adversarial")
            out = lab_call(
                lambda: adversarial_evidence(
                    plan, run_id=run_id, cfg=config, ledger=led, loader=load
                )
            )
            out["budget"] = budget_status(led, run_id, config)
        return out

    @server.tool(annotations=_WRITES)
    def run_crosscheck(run_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Re-run an already backtested plan in the event-driven engine (nautilus_trader) on
        the same design window and compare trades and returns with the vectorbt engine.
        matched=true only when the engine is available and both agree. Needs run_backtest
        evidence for this exact plan first; counts as 1 backtest for the budget guard.
        Portfolio plans: an independent accounting replay recomputes net equity from the same
        weights, candles, costs and impact and must match within 1e-8 (relative)."""
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
        _, h = _plan_or_error(plan)
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
            guard(led, run_id, h, 1, "run_crosscheck")
            out = lab_call(
                lambda: crosscheck_evidence(
                    plan, run_id=run_id, cfg=config, ledger=led, loader=load
                )
            )
            out["budget"] = budget_status(led, run_id, config)
        return out

    @server.tool(annotations=_WRITES)
    def run_sweep(
        run_id: str, plan: dict[str, Any], space: dict[str, SpaceRange], n_trials: int
    ) -> dict[str, Any]:
        """Optuna (seeded TPE) search maximising the net Sharpe on the design window over the
        explicit ranges in space ({tunable key: {low, high}}). Every trial is a ledger trial
        and raises the DSR bar; the best of many trials is biased upward. Portfolio plans:
        space keys are the tunable paths validate_plan lists."""
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
            rng = _space(parsed, space, frozen_criteria(led, run_id))
            guard(led, run_id, h, n_trials, f"run_sweep with n_trials={n_trials}")
            candles, window = _design(parsed, config, load)
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
            record_report(led, run_id, h, "sweep", out)
            out = compact_report(out)
            out["budget"] = budget_status(led, run_id, config)
        return out

    @server.tool(annotations=_WRITES)
    def walk_forward(
        run_id: str, plan: dict[str, Any], space: dict[str, SpaceRange], n_trials_per_fold: int
    ) -> dict[str, Any]:
        """Walk-forward optimisation inside the design window: per fold, Optuna searches space
        on the train window only, then the chosen params run once on the test window. The
        stitched out-of-sample net Sharpe is what the success criteria judge. Portfolio plans
        follow the same lineage rule (cover every value the behaviour family tried)."""
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
            rng = _space(parsed, space, frozen_criteria(led, run_id))
            lineage_notes = _lineage(led, parsed, h, rng, config, load)
            candles, window = _design(parsed, config, load)
            try:
                folds = _folds(window, config)
            except ValueError as exc:
                raise ToolError(str(exc)) from None
            cost = (n_trials_per_fold + 1) * len(folds)
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

    @server.tool(annotations=_READ_ONLY)
    def get_ledger(run_id: str, limit: int = 50) -> dict[str, Any]:
        """This run's trials, newest first, as summary rows (no returns), plus counts and the
        DSR trial count N. Holdout trials are not listed."""
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
            return {
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

    @server.tool(annotations=_READ_ONLY)
    def get_report(run_id: str, plan_hash: str) -> dict[str, Any]:
        """Judge a plan (by plan_hash of the plan as submitted) against the run's frozen
        success criteria: one row per criterion with pass / fail / missing, the DSR N, and the
        budget. pre_holdout_passed must be true before holdout_check."""
        with open_ledger() as led:
            check_run(led, run_id)
            try:
                return dict(evaluate_plan(led, run_id, plan_hash, config, loader=load))
            except ValueError as exc:
                raise ToolError(str(exc)) from None

    @server.tool(annotations=_READ_ONLY)
    def get_budget(run_id: str) -> dict[str, Any]:
        """The run's frozen budget, what has been used, and what remains (proposals,
        backtests, holdout checks)."""
        with open_ledger() as led:
            check_run(led, run_id)
            return budget_status(led, run_id, config)

    # ------------------------------------------------------------------ holdout

    @server.tool(annotations=_WRITES)
    def holdout_check(run_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Spend one locked-holdout check on a plan that passes every pre-holdout criterion.
        Returns pass/fail and the checks left only (no metrics, not even which rule failed).
        One check per hypothesis logic, ever, in any run: a plan that fails is dead, and a
        renamed or re-tuned copy is the same hypothesis. Refused, without spending, if any
        guard fails."""
        if not holdout_enabled:
            raise ToolError(
                "holdout_check is disabled on this server (STRATEGY_LAB_DISABLE_HOLDOUT=1): "
                "the orchestrator runs holdout checks itself. Report the candidate instead."
            )
        if pf.is_portfolio_plan(plan):
            _portfolio_or_error(plan, config)
        else:
            _plan_or_error(plan)
        with open_ledger() as led:
            check_run(led, run_id)
        with run_lock(run_id), open_ledger() as led:
            check_run(led, run_id)
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
