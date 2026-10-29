"""Backtest report runner used by the API and UI (PLAN.md Phase 7 How §4-6, §9).

``run_backtest_report`` turns a raw plan into the ``BacktestReport`` JSON the web panel draws:
net + gross metrics, benchmarks, DSR/PSR, PBO, bootstrap CI, walk-forward test-fold metrics
(``walk_forward_kind = "fixed_params"``: the plan's own parameters on each test fold, not a
tuned out-of-sample result), trades, equity curves, warnings, and a disclaimer.
``run_adversarial_report`` runs the adversarial checks. Both work on the **design window only**:

* Window = ``[max(design_start, first bar), holdout_start)`` (or up to the last bar if the
  data ends earlier). Candles are loaded with ``end = holdout_start`` so holdout rows are never
  even read; ``assert_no_holdout`` guards the frame anyway.
* Derivatives plans (a ``drv_*`` input, PLAN.md Phase 12) are SCORED from ONE start per
  (symbol, interval), shared by every derivatives plan (:func:`derivatives_scored_start`): the
  latest ``first`` timestamp across the five derivatives archive series of that symbol/interval
  (fundingRate, metrics, premium/mark/index klines; from the run's frozen snapshot inside a
  research run, else the current manifest) plus ``[derivatives] warmup_days``, snapped to the
  first bar at or after it, never before ``design_start``. Which derivatives features a plan
  uses, and their periods, never move it: a derivatives look-back must fit inside
  ``warmup_days`` (:func:`derivatives_lookback_errors`), and any NaN left inside the window
  (archive gaps) scores flat. Like portfolio plans scored from their first decision, the plan,
  every benchmark and null, the neighbourhood, adversarial variants and walk-forward folds all
  use that window, so years of forced-flat bars before the archive cannot flatter drawdown. The
  report's ``scoring`` block states the start (UTC). Before any ledger row,
  :func:`check_gate_base` refuses a plan whose derivatives-free entry clauses alone are true on
  ``>= [derivatives] gate_base_max_true_frac`` of those bars. Plans without derivatives
  features are unaffected.
* Signals are computed on the history through the window end (earlier bars = warm-up), then
  candles and signals are sliced to the window and the engine runs on the slice. The same
  history is the engine's ``cost_history`` so vol-scaled slippage is warmed up as well.

Ledger policy (every backtest is recorded; the ledger is append-only):

* the base plan run -> source ``"api"`` (``"error"`` status with the message if it fails);
* each walk-forward test fold of the base plan -> source ``"api"``, window kind
  ``"walk_forward_test"`` on the fold's window, same ``plan_hash`` (so N does not change);
* each ±20% neighbourhood variant -> ``"neighbourhood"`` (errors recorded too);
* each adversarial variant (delays, sign flip, cost stress) -> ``"adversarial"``;
* each benchmark / null (buy-and-hold, vol-target, random-entry, permutation null) -> ONE
  summary row per report with source ``"benchmark"``.
* derivatives plans also get ``benchmarks.ungated`` (:func:`ungated_plan`: the same plan
  without its derivatives entry clauses, same costs and scored window, plus the gate's binding
  share): informational only and NOT recorded in the ledger (not a trial, not a candidate).
* every engine row stores ``costs_json`` (the cost model it ran with; cost-stress variants
  store the scaled model).
* **DSR's N** = number of DISTINCT ``plan_hash`` in the run among sources {manual, api, optuna,
  neighbourhood} of the plan's interval, on any window (ok and error rows: a configuration that
  crashed was still tried); re-running the same plan does not inflate it. N is scoped to the
  ``run_id``; while the client chooses the run_id (the UI sends ``"ui"``), the report warns
  that N counts only that run's trials. **V[SR]** = sample variance of the latest per-bar
  Sharpe per distinct plan with the same interval, the same design window, and the same cost
  model, never from ``walk_forward_test`` rows (only those Sharpes are commensurable with the
  one being deflated).

Results are historical and not forward-looking.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from lab.config import LabConfig
from lab.data.derivatives import manifest_sha256
from lab.data.derivatives_columns import (
    DataUnavailableError,
    DerivativeColumns,
    attach_columns,
    derivative_columns,
    derivatives_start_floor,
    manifest_series,
    max_derivatives_period,
    plan_derivative_columns,
    require_plan_inputs,
)
from lab.data.store import load_candles
from lab.engine import run_signals, run_weights
from lab.engine.splits import (
    assert_no_holdout,
    design_view,
    slice_window,
    walk_forward_folds,
)
from lab.evaluate.adversarial import AdversarialReport, VariantResult, adversarial_checks
from lab.evaluate.benchmarks import (
    RandomEntryResult,
    buy_and_hold,
    random_entry_null,
    vol_target_weights,
)
from lab.ledger import SELECTION_SOURCES, Ledger, costs_to_json, metrics_to_json
from lab.plan import (
    compile_plan,
    compute_features,
    evaluate_expression,
    parse_plan,
    plan_hash,
    plan_to_dict,
    tunable_params,
    with_params,
)
from lab.plan.validator import PlanValidationError, gate_base_expressions
from lab.registry import REGISTRY
from lab.registry.derivatives import DERIVATIVES_FEATURES
from lab.report import (
    equity_points,
    metrics_dict,
    num,
    ordinal,
    seconds,
    to_jsonable,
    trade_dict,
)
from lab.stats import (
    compute_metrics,
    deflated_sharpe,
    pbo_cscv,
    sharpe_per_bar,
    stationary_bootstrap_ci,
)
from lab.types import (
    PRICE_INPUTS,
    BacktestResult,
    CostModel,
    Metrics,
    ParamSpec,
    Plan,
    PlanError,
    TrialRecord,
    TrialSource,
    Window,
    bars_per_year,
    interval_seconds,
)

__all__ = [
    "CandleLoader",
    "DataUnavailableError",
    "DerivativesLookbackError",
    "GateBaseRefusedError",
    "GateBaseTrialError",
    "archive_cli_hint",
    "check_gate_base",
    "default_loader",
    "derivatives_lookback_errors",
    "derivatives_period_caps",
    "derivatives_scored_start",
    "derivatives_series",
    "gate_base_share",
    "gate_base_trial_check",
    "neighbourhood_overrides",
    "pbo_split_count",
    "plan_design_window",
    "run_adversarial_report",
    "run_backtest_report",
    "scoring_json",
    "ungated_plan",
]

CandleLoader = Callable[[str, str, pd.Timestamp], pd.DataFrame]
"""``loader(symbol, interval, end) -> candles`` with every bar strictly before ``end``."""

# Warning thresholds (wording only; they never change a number in the report). They mirror the
# Phase 8 bars so the UI flags the same things; the BINDING acceptance criteria will live in
# ``lab/config/success_criteria*`` (Phase 8, fixed before a run) and are not read from here.
MIN_TRADES = 100  # fewer trades than this mean very little (honest-backtesting skill)
COST_DRAG_SHARPE = 0.3  # gross - net annualised Sharpe above this: costs eat the edge
DSR_THRESHOLD = 0.95  # DSR below this is not significant after the run's trials
PBO_THRESHOLD = 0.2  # Phase 8 bar; here PBO is over the ±20% neighbourhood (see _warnings)
RANDOM_ENTRY_PERCENTILE = 95.0  # strategy must beat 95% of same-trade-count random entries
WALK_FORWARD_KIND = "fixed_params"  # report rows: the plan's own params on each test fold
PBO_BASIS = "neighbourhood"  # report PBO: CSCV over the base plan + its ±20% neighbours
WALK_FORWARD_WARNING = (
    "Walk-forward rows use the plan's fixed parameters on each test fold — not a tuned "
    "out-of-sample result (use `python -m lab.search --walk-forward` for that)."
)
DEFAULT_SEED = 7  # explicit seed for bootstrap / random-entry / permutation draws

DISCLAIMER_HEAD = "Historical simulation on Binance spot data; not forward-looking."


# ``DataUnavailableError`` (no candles in the design window, or derivatives inputs a plan needs
# are missing; ``str()`` names the CLI) lives in ``lab.data.derivatives_columns`` and is
# re-exported here so every existing ``from lab.runner import DataUnavailableError`` still works.


# --------------------------------------------------------------------------- data


def last_full_month(now: pd.Timestamp | None = None) -> str:
    """``YYYY-MM`` of the last complete calendar month (UTC)."""
    ts = pd.Timestamp.now(tz="UTC") if now is None else now.tz_convert("UTC")
    first = ts.normalize().replace(day=1)
    return (first - pd.Timedelta(days=1)).strftime("%Y-%m")


def archive_cli_hint(symbol: str, interval: str, now: pd.Timestamp | None = None) -> str:
    return (
        f"cd lab && uv run python -m lab.data.archive {symbol} {interval} 2018-01 "
        f"{last_full_month(now)}"
    )


def default_loader(cfg: LabConfig) -> CandleLoader:
    """Parquet-only loader (the verified Binance.com archive). The server's SQLite cache holds
    live Binance bars without checksums and is deliberately never mixed in for research.

    Every load also attaches the ``drv_*`` derivatives columns whose archive series exist under
    ``cfg.derivatives_dir`` (``lab.data.derivatives_columns.attach_derivatives``: aligned to each
    bar's close, loaded only up to the last bar's close, never forward-filled). The aligned
    columns are cached per closure, keyed on ``(symbol, interval, end, manifest sha256)`` and
    checked against the bar index, so a sweep does not re-read ~2,000 daily metrics files per
    call; rebuilding the manifest (the download CLI does) invalidates the cache. Without a
    manifest nothing is cached. Candles themselves are re-read every call, as before.

    Archive rows stamped at or after ``end`` are dropped (``stamped_before=end``), so a
    design-window load (``end = holdout_start``) uses no derivatives row stamped in the holdout,
    even one visible exactly at the last design bar's close."""
    cache: dict[tuple[str, str, pd.Timestamp, str], DerivativeColumns] = {}
    data_dir = Path(cfg.derivatives_dir)
    stale = cfg.derivatives.stale_after_intervals

    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        folder = Path(cfg.parquet_dir) / symbol / interval
        if not folder.is_dir() or not any(folder.glob("*.parquet")):
            raise DataUnavailableError(
                f"no archive data for {symbol} {interval}; download it with "
                f"`{archive_cli_hint(symbol, interval)}`"
            )
        candles = load_candles(symbol, interval, parquet_dir=cfg.parquet_dir, end=end)
        sha = manifest_sha256(data_dir)
        key = (symbol, interval, pd.Timestamp(end), sha) if sha is not None else None
        cols = cache.get(key) if key is not None else None
        if cols is None or not cols.bar_times.equals(candles.index):
            cols = derivative_columns(
                pd.DatetimeIndex(candles.index),
                symbol,
                interval,
                data_dir=data_dir,
                stale_after_intervals=stale,
                # rows stamped at/after the load's end never reach it (Note in
                # lab.data.derivatives_columns: design loads end at holdout_start)
                stamped_before=pd.Timestamp(end),
            )
            if key is not None:
                cache[key] = cols
        return attach_columns(candles, cols)

    return load


class GateBaseRefusedError(PlanValidationError):
    """A gated plan whose derivatives-free ``long_entry`` clauses are true on at least
    ``[derivatives] gate_base_max_true_frac`` of the scored design bars: the price clause does
    not select, so the derivatives filter would be the de-facto signal. Raised before any
    ledger row; a :class:`PlanValidationError` (path ``long_entry``), so the MCP server and the
    API report it as a plan error the agent must fix, never as an error trial."""


@dataclass(frozen=True)
class _Prepared:
    plan: Plan
    plan_hash: str
    plan_dict: dict[str, Any]
    history: pd.DataFrame  # design view: every bar < holdout_start (and < window end)
    window: Window  # the SCORED window (a derivatives plan's effective start, see below)
    candles: pd.DataFrame  # history sliced to the window
    scoring: dict[str, Any] | None = None  # derivatives plans: scoring_json(...)


def _design_window(history: pd.DataFrame, cfg: LabConfig, interval: str) -> Window | None:
    if history.empty:
        return None
    start = max(cfg.splits.design_start, history.index[0])
    last_end = history.index[-1] + pd.Timedelta(seconds=interval_seconds(interval))
    end = min(cfg.splits.holdout_start, last_end)
    if end <= start:
        return None
    return Window(start=start, end=end, kind="design")


class DerivativesLookbackError(PlanValidationError):
    """A derivatives feature looks back further than ``[derivatives] warmup_days``: the shared
    scored start leaves room for that much warm-up only (:func:`derivatives_scored_start`).
    Raised before any ledger row; a :class:`PlanValidationError` the agent must fix."""


def derivatives_period_caps(plan: Plan, cfg: LabConfig) -> dict[str, int]:
    """``{"alias.period": max bars}`` for every derivatives feature ``period`` param of ``plan``:
    the longest look-back on its interval that fits in ``[derivatives] warmup_days``."""
    cap = max_derivatives_period(plan.interval, cfg.derivatives.warmup_days)
    out: dict[str, int] = {}
    for ref in plan.features:
        if ref.feature not in DERIVATIVES_FEATURES:
            continue
        for p in REGISTRY[ref.feature].params:
            if p.name == "period":
                out[f"{ref.alias}.{p.name}"] = cap
    return out


def derivatives_lookback_errors(plan: Plan, cfg: LabConfig) -> list[PlanError]:
    """One error per derivatives feature whose ``period x bar length`` exceeds
    ``[derivatives] warmup_days`` (empty for plans without derivatives features)."""
    warm = cfg.derivatives.warmup_days
    bar_days = interval_seconds(plan.interval) / 86_400
    cap = max_derivatives_period(plan.interval, warm)
    errors: list[PlanError] = []
    for ref in plan.features:
        if ref.feature not in DERIVATIVES_FEATURES:
            continue
        for p in REGISTRY[ref.feature].params:
            if p.name != "period":
                continue
            period = int(ref.params.get(p.name, p.default))
            if period > cap:
                errors.append(
                    PlanError(
                        f"features.{ref.alias}.{ref.feature}.period",
                        f"{ref.feature} period {period} on {plan.interval} = "
                        f"{period * bar_days:.1f} days > [derivatives] warmup_days {warm:g}; "
                        f"use <= {cap} (every derivatives plan on {plan.symbol} "
                        f"{plan.interval} is scored from one shared start that leaves "
                        f"{warm:g} days of derivatives warm-up)",
                    )
                )
    return errors


def derivatives_series(
    cfg: LabConfig, ledger: Ledger | None = None, run_id: str | None = None
) -> Mapping[str, Any] | None:
    """The per-series derivatives entries the scored start (and the holdout's gap check) read:
    ``run_id``'s FROZEN snapshot series when it is a research run that froze them (so the start
    cannot move mid-run), else the current manifest's series (None without a manifest)."""
    if ledger is not None and run_id is not None:
        run = ledger.research_run(run_id)
        config = run["config"] if run is not None else None
        frozen = config.get("derivatives") if isinstance(config, dict) else None
        if isinstance(frozen, dict) and isinstance(frozen.get("series"), Mapping):
            return frozen["series"]
    return manifest_series(cfg.derivatives_dir)


def derivatives_scored_start(
    symbol: str,
    interval: str,
    history: pd.DataFrame,
    cfg: LabConfig,
    series: Mapping[str, Any] | None,
) -> pd.Timestamp:
    """The ONE scored start of every derivatives plan on (``symbol``, ``interval``): the first
    bar of ``history`` at or after ``max(design_start,``
    :func:`lab.data.derivatives_columns.derivatives_start_floor` ``)`` (latest series ``first``
    in ``series`` + ``warmup_days``). Raises :class:`DataUnavailableError` when a series is
    missing or no bar of ``history`` is that late."""
    floor = derivatives_start_floor(
        symbol,
        interval,
        series,
        warmup_days=cfg.derivatives.warmup_days,
        cli_start=cfg.splits.design_start.strftime("%Y-%m"),
        cli_end=last_full_month(),
    )
    at = max(floor, cfg.splits.design_start)
    idx = pd.DatetimeIndex(history.index)
    hits = np.flatnonzero(idx >= at)
    if not hits.size:
        raise DataUnavailableError(
            f"no {symbol} {interval} bar at or after the derivatives scored start "
            f"{_iso(at)} (latest derivatives series start + [derivatives] warmup_days "
            f"{cfg.derivatives.warmup_days:g}); nothing was recorded in the ledger"
        )
    return pd.Timestamp(idx[hits[0]])


def gate_base_share(plan: Plan, history: pd.DataFrame, window: Window) -> dict[str, Any] | None:
    """``{"price_clause", "price_clause_true_share", "bars"}``: the share of the bars of
    ``window`` (where it is defined) on which the conjunction of ``long_entry``'s
    derivatives-FREE top-level ``and`` clauses (:func:`lab.plan.validator.gate_base_expressions`)
    is true, evaluated on ``history``; None for plans without derivatives gates, or whose price
    clause is never defined in the window, and for risk overlays (no gate: ``flat_when`` may use
    derivatives features freely). Features only, no backtest."""
    if plan.kind == "overlay":
        return None
    exprs = gate_base_expressions(plan)
    if exprs is None:
        return None
    base, negated = exprs
    n = len(history)
    features = compute_features(plan, history)
    cols: dict[str, np.ndarray] = {c: history[c].to_numpy(dtype=np.float64) for c in PRICE_INPUTS}
    cols.update({c: features[c].to_numpy(dtype=np.float64) for c in features.columns})
    true = evaluate_expression(base, cols, n)
    false = evaluate_expression(negated, cols, n)
    idx = pd.DatetimeIndex(history.index)
    defined = (true | false) & (idx >= window.start) & (idx < window.end)
    bars = int(defined.sum())
    if bars == 0:
        return None
    share = float(true[defined].mean())
    return {"price_clause": base.source, "price_clause_true_share": share, "bars": bars}


def check_gate_base(
    plan: Plan, history: pd.DataFrame, window: Window, cfg: LabConfig
) -> dict[str, Any] | None:
    """Refuse a gated plan whose price clause does not select (PLAN.md Phase 12).

    Evaluates the conjunction of ``long_entry``'s derivatives-FREE top-level ``and`` clauses
    (:func:`lab.plan.validator.gate_base_expressions`) on ``history`` and, over the bars of
    ``window`` where it is defined, raises :class:`GateBaseRefusedError` when it is true on at
    least ``cfg.derivatives.gate_base_max_true_frac`` of them (``close > 0``, ``rsi < 101``,
    ``volume >= 0``: always true, so the gate alone would select). Returns ``{"price_clause",
    "price_clause_true_share", "bars"}``, or None for plans without derivatives gates (or whose
    price clause is never defined in the window); a no-op (None) for risk overlays."""
    if plan.kind == "overlay":
        return None
    info = gate_base_share(plan, history, window)
    if info is None:
        return None
    share, bars = info["price_clause_true_share"], info["bars"]
    limit = cfg.derivatives.gate_base_max_true_frac
    if share >= limit:
        gates = sorted(r.alias for r in plan.features if r.feature in DERIVATIVES_FEATURES)
        raise GateBaseRefusedError(
            [
                PlanError(
                    "long_entry",
                    f"the price clause `{info['price_clause']}` (the long_entry clauses without "
                    f"derivatives features) is true on {share:.1%} of the {bars} scored design "
                    f"bars from {window.start.date()}, at or above lab.toml [derivatives] "
                    f"gate_base_max_true_frac {limit:g}: it does not select bars, so the "
                    f"derivatives gate(s) {', '.join(f'`{g}`' for g in gates)} would be the "
                    "de-facto entry signal, and a derivatives feature is an exposure or "
                    "crash-risk filter, not a direction signal. Replace it with a price signal "
                    "that selects bars on its own (e.g. `close > ema_slow`, true on roughly half "
                    "the bars, or a crossover) and keep the gate as an extra `and` clause. "
                    "Nothing was recorded in the ledger",
                )
            ]
        )
    return info


class GateBaseTrialError(ValueError):
    """A sweep / walk-forward trial's parameters make the price clause ~always true
    (:func:`gate_base_trial_check`); the search records the trial as a failed (``error``) ledger
    row, so it counts in DSR's N and is never selected."""


def gate_base_trial_check(
    history: pd.DataFrame, window: Window, cfg: LabConfig
) -> Callable[[Plan], None]:
    """Per-trial gate-base check for Optuna sweeps and walk-forward folds
    (:func:`lab.search.optuna_search.optuna_search` ``trial_check``): the same rule as
    :func:`check_gate_base`, applied to each trial's concrete parameters on the plan's SCORED
    design ``window`` (``history`` = the design view), so tuning a price period cannot turn the
    price clause into an always-true one and let the derivatives gate become the entry signal.
    Raises :class:`GateBaseTrialError`; a no-op for plans without derivatives gates."""
    limit = cfg.derivatives.gate_base_max_true_frac

    def check(plan: Plan) -> None:
        if plan.kind == "overlay":  # no derivatives gate to check (PLAN.md Phase 15)
            return
        info = gate_base_share(plan, history, window)
        if info is None:
            return
        share = info["price_clause_true_share"]
        if share >= limit:
            raise GateBaseTrialError(
                f"gate-base check failed for these params: the price clause "
                f"`{info['price_clause']}` is true on {share:.1%} of the {info['bars']} scored "
                f"design bars from {window.start.date()}, at or above lab.toml [derivatives] "
                f"gate_base_max_true_frac {limit:g}, so the derivatives gate would be the "
                "de-facto entry signal; the trial is failed (never selectable), no backtest run"
            )

    return check


def _iso(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def scoring_json(
    design_start: pd.Timestamp, window: Window, gate: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The report's ``scoring`` block for a derivatives plan: where scoring starts and why."""
    out: dict[str, Any] = {
        "scored_from": seconds(window.start),
        "scored_from_utc": _iso(window.start),
        "design_start": seconds(design_start),
        "design_start_utc": _iso(design_start),
        "note": (
            "derivatives plan: scored from the one start shared by every derivatives plan on "
            "this symbol/interval (latest derivatives series start + [derivatives] "
            "warmup_days, frozen with the run's snapshot); the plan, buy-and-hold, vol-target, "
            "the random-entry and permutation nulls, the neighbourhood (PBO), adversarial "
            "variants, sweeps and walk-forward folds all start there"
        ),
    }
    if gate is not None:
        out.update(gate)
    return out


def plan_design_window(
    plan: Plan,
    history: pd.DataFrame,
    cfg: LabConfig,
    *,
    series: Mapping[str, Any] | None = None,
) -> tuple[Window, dict[str, Any] | None]:
    """The window ``plan`` is scored on, from its design view ``history`` (every bar before
    ``holdout_start``, warm-up included), and its ``scoring`` block (None for plans without
    derivatives features).

    ``[max(design_start, first bar), min(holdout_start, last bar end))``; for a plan that reads a
    derivatives column the start is :func:`derivatives_scored_start` (one start per symbol and
    interval, from ``series``: pass :func:`derivatives_series` inside a research run; None reads
    the current manifest), and :func:`check_gate_base` runs on that window. Raises
    :class:`DerivativesLookbackError` (a look-back longer than ``warmup_days``),
    :class:`DataUnavailableError` (no bars, missing inputs or series) or
    :class:`GateBaseRefusedError`, all before any ledger row. Plans without derivatives get
    exactly the window they always had."""
    lookback = derivatives_lookback_errors(plan, cfg)
    if lookback:
        raise DerivativesLookbackError(lookback)
    window = _design_window(history, cfg, plan.interval)
    candles = slice_window(history, window) if window is not None else history.iloc[:0]
    if window is None or len(candles) < 2:
        raise DataUnavailableError(
            f"no {plan.symbol} {plan.interval} bars in the design window "
            f"[{cfg.splits.design_start.date()}, {cfg.splits.holdout_start.date()}); download "
            f"them with `{archive_cli_hint(plan.symbol, plan.interval)}`"
        )
    # before any trial is recorded: a missing derivatives download must not spend a ledger row
    require_plan_inputs(plan, candles, symbol=plan.symbol, interval=plan.interval)
    if not plan_derivative_columns(plan):
        return window, None
    if series is None:
        series = manifest_series(cfg.derivatives_dir)
    start = derivatives_scored_start(plan.symbol, plan.interval, candles, cfg, series)
    scored = Window(start=max(window.start, start), end=window.end, kind="design")
    if int(((history.index >= scored.start) & (history.index < scored.end)).sum()) < 2:
        raise DataUnavailableError(
            f"fewer than 2 {plan.symbol} {plan.interval} design bars from the derivatives "
            f"scored start {_iso(scored.start)} to {scored.end.date()}; nothing was recorded "
            "in the ledger"
        )
    gate = check_gate_base(plan, history, scored, cfg)
    return scored, scoring_json(window.start, scored, gate)


def _prepare(
    raw_plan: Mapping[str, Any] | str,
    cfg: LabConfig,
    loader: CandleLoader,
    ledger: Ledger | None = None,
    run_id: str | None = None,
) -> _Prepared:
    plan = parse_plan(raw_plan)  # PlanValidationError lists every problem
    raw = loader(plan.symbol, plan.interval, cfg.splits.holdout_start)
    history = design_view(raw, cfg.splits)
    assert_no_holdout(history, cfg.splits)
    series = derivatives_series(cfg, ledger, run_id) if plan_derivative_columns(plan) else None
    window, scoring = plan_design_window(plan, history, cfg, series=series)
    candles = slice_window(history, window)
    history = history.loc[history.index < window.end]
    return _Prepared(plan, plan_hash(plan), plan_to_dict(plan), history, window, candles, scoring)


# --------------------------------------------------------------------------- engine helpers


def _signals(
    plan: Plan, history: pd.DataFrame, index: pd.DatetimeIndex
) -> tuple[pd.Series, pd.Series]:
    sig = compile_plan(plan, history)
    return sig.entries.loc[index], sig.exits.loc[index]


def _run_plan(plan: Plan, prep: _Prepared, cfg: LabConfig) -> BacktestResult:
    entries, exits = _signals(plan, prep.history, prep.candles.index)
    return run_signals(
        prep.candles,
        entries,
        exits,
        costs=cfg.costs,
        risk=plan.risk,
        init_cash=cfg.init_cash,
        cost_history=prep.history,
    )


def _record(
    ledger: Ledger,
    *,
    run_id: str,
    source: TrialSource,
    plan_hash_: str,
    plan_json: str,
    params: Mapping[str, Any],
    prep: _Prepared,
    metrics: Metrics | None = None,
    metrics_json: str | None = None,
    returns: np.ndarray | None = None,
    sharpe_bar: float | None = None,
    n_trades: int = 0,
    error: str | None = None,
    costs: CostModel | None = None,
    window: Window | None = None,
) -> int:
    win = prep.window if window is None else window
    if metrics_json is None:
        metrics_json = metrics_to_json(metrics) if metrics is not None else "{}"
    if sharpe_bar is None and returns is not None:
        sharpe_bar = float(sharpe_per_bar(returns))
    return ledger.record(
        TrialRecord(
            run_id=run_id,
            source=source,
            status="error" if error is not None else "ok",
            plan_hash=plan_hash_,
            plan_json=plan_json,
            params_json=json.dumps(to_jsonable(dict(params)), sort_keys=True),
            symbol=prep.plan.symbol,
            interval=prep.plan.interval,
            window_kind=win.kind,
            window_start=win.start,
            window_end=win.end,
            metrics_json=metrics_json,
            sharpe_per_bar=sharpe_bar,
            n_trades=int(n_trades),
            error=error,
            costs_json=costs_to_json(costs) if costs is not None else "{}",
            returns=returns,
        )
    )


def _canonical(obj: Mapping[str, Any]) -> str:
    return json.dumps(to_jsonable(dict(obj)), sort_keys=True, separators=(",", ":"))


def _benchmark_identity(name: str, prep: _Prepared, params: Mapping[str, Any]) -> tuple[str, str]:
    """(plan_hash, plan_json) for a benchmark pseudo-plan, so the ledger can tell them apart."""
    obj = {
        "name": f"benchmark:{name}",
        "benchmark": name,
        "symbol": prep.plan.symbol,
        "interval": prep.plan.interval,
        "params": dict(params),
    }
    text = _canonical(obj)
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), text


def _record_benchmark(
    ledger: Ledger,
    run_id: str,
    prep: _Prepared,
    name: str,
    params: Mapping[str, Any],
    *,
    metrics: Metrics | None = None,
    metrics_json: str | None = None,
    returns: np.ndarray | None = None,
    sharpe_bar: float | None = None,
    n_trades: int = 0,
    costs: CostModel | None = None,
) -> int:
    h, text = _benchmark_identity(name, prep, params)
    return _record(
        ledger,
        run_id=run_id,
        source="benchmark",
        plan_hash_=h,
        plan_json=text,
        params=params,
        prep=prep,
        metrics=metrics,
        metrics_json=metrics_json,
        returns=returns,
        sharpe_bar=sharpe_bar,
        n_trades=n_trades,
        costs=costs,
    )


# --------------------------------------------------------------------------- neighbourhood / PBO


def neighbourhood_overrides(
    tunables: Mapping[str, ParamSpec], steps: tuple[float, ...], max_trials: int
) -> list[dict[str, float]]:
    """One-at-a-time ±x% overrides around the base values, nearest steps first.

    ``value = base * step`` (ints rounded), clipped to the param bounds; values equal to the
    base or already produced are skipped. At most ``max_trials - 1`` overrides (the base plan
    is the remaining trial).
    """
    others = sorted({s for s in steps if s != 1.0}, key=lambda s: (abs(s - 1.0), s))
    out: list[dict[str, float]] = []
    seen: set[tuple[str, float]] = set()
    for step in others:
        for key, spec in tunables.items():
            v = spec.default * step
            if spec.kind == "int":
                v = float(round(v))
            v = float(min(max(v, spec.low), spec.high))
            if v == spec.default or (key, v) in seen:
                continue
            seen.add((key, v))
            out.append({key: v})
    return out[: max(0, max_trials - 1)]


def pbo_split_count(n_rows: int, max_splits: int) -> int:
    """Largest even ``S <= max_splits`` with ``S <= n_rows``; 0 if none (S >= 2)."""
    s = min(int(max_splits), int(n_rows))
    s -= s % 2
    return s if s >= 2 else 0


@dataclass(frozen=True)
class _Neighbour:
    overrides: Mapping[str, float]
    plan_hash: str
    returns: np.ndarray | None
    error: str | None


def _run_neighbourhood(
    prep: _Prepared, cfg: LabConfig, ledger: Ledger, run_id: str
) -> list[_Neighbour]:
    out: list[_Neighbour] = []
    tunables = tunable_params(prep.plan)
    # a derivatives look-back never grows past [derivatives] warmup_days (the shared scored
    # start leaves room for that much only): its neighbours are clipped there like at a bound
    for key, cap in derivatives_period_caps(prep.plan, cfg).items():
        if key in tunables:
            tunables[key] = replace(tunables[key], high=float(min(tunables[key].high, cap)))
    for ov in neighbourhood_overrides(
        tunables, cfg.stats.neighbourhood_steps, cfg.stats.neighbourhood_max_trials
    ):
        try:
            variant = with_params(prep.plan, ov)
        except PlanValidationError:
            continue  # not a valid configuration, so not a trial
        h = plan_hash(variant)
        if h == prep.plan_hash or any(n.plan_hash == h for n in out):
            continue
        plan_json = json.dumps(plan_to_dict(variant), sort_keys=True)
        try:
            res = _run_plan(variant, prep, cfg)
        except ValueError as exc:
            _record(
                ledger,
                run_id=run_id,
                source="neighbourhood",
                plan_hash_=h,
                plan_json=plan_json,
                params=ov,
                prep=prep,
                error=str(exc),
                costs=cfg.costs,
            )
            out.append(_Neighbour(ov, h, None, str(exc)))
            continue
        m = compute_metrics(res, prep.plan.interval)
        r = res.returns.to_numpy(dtype=float)
        _record(
            ledger,
            run_id=run_id,
            source="neighbourhood",
            plan_hash_=h,
            plan_json=plan_json,
            params=ov,
            prep=prep,
            metrics=m,
            returns=r,
            n_trades=m.n_trades,
            costs=cfg.costs,
        )
        out.append(_Neighbour(ov, h, r, None))
    return out


# --------------------------------------------------------------------------- report pieces


def _walk_forward(
    prep: _Prepared, cfg: LabConfig, ledger: Ledger, run_id: str, plan_json: str
) -> list[dict[str, Any]]:
    """The base plan (fixed params) on each walk-forward test fold; every fold run is a
    ledger row (source "api", window kind "walk_forward_test", the base plan's hash)."""
    s = cfg.splits
    folds = walk_forward_folds(
        prep.window,
        train_months=s.wf_train_months,
        test_months=s.wf_test_months,
        step_months=s.wf_step_months,
        anchored=s.wf_anchored,
    )
    sig = compile_plan(prep.plan, prep.history)
    rows: list[dict[str, Any]] = []
    for fold in folds:
        candles = slice_window(prep.candles, fold.test)
        if len(candles) < 2:
            rows.append(
                {
                    "fold": fold.index,
                    "test_start": seconds(fold.test.start),
                    "test_end": seconds(fold.test.end),
                    "sharpe": None,
                    "total_return": None,
                    "n_trades": 0,
                }
            )
            continue
        res = run_signals(
            candles,
            sig.entries.loc[candles.index],
            sig.exits.loc[candles.index],
            costs=cfg.costs,
            risk=prep.plan.risk,
            init_cash=cfg.init_cash,
            cost_history=prep.history.loc[prep.history.index < fold.test.end],
        )
        m = compute_metrics(res, prep.plan.interval)
        _record(
            ledger,
            run_id=run_id,
            source="api",
            plan_hash_=prep.plan_hash,
            plan_json=plan_json,
            params={"walk_forward_fold": fold.index},
            prep=prep,
            metrics=m,
            returns=res.returns.to_numpy(dtype=float),
            n_trades=m.n_trades,
            costs=cfg.costs,
            window=fold.test,
        )
        rows.append(
            {
                "fold": fold.index,
                "test_start": seconds(fold.test.start),
                "test_end": seconds(fold.test.end),
                "sharpe": num(m.sharpe),
                "total_return": num(m.total_return),
                "n_trades": int(m.n_trades),
            }
        )
    return rows


def _costs_text(cfg: LabConfig) -> str:
    c = cfg.costs
    text = f"fee {c.fee_rate:.3%} + slippage {c.slippage_rate:.3%} per side"
    if c.slippage_vol_mult:
        text += (
            f" (slippage widened x{c.slippage_vol_mult:g} on unusually wide bars, capped at "
            f"{c.slippage_cap:.2%} per side)"
        )
    return text + "."


def _disclaimer(cfg: LabConfig) -> str:
    return (
        f"{DISCLAIMER_HEAD} Costs: {_costs_text(cfg)} Design window only; the locked holdout "
        f"(from {cfg.splits.holdout_start.date()}) is not shown."
    )


def _warnings(
    *,
    net: Metrics,
    gross: Metrics,
    vol_target: Metrics,
    dsr: float,
    n_trials: int,
    pbo: float | None,
    pbo_trials: int,
    random_pct: float,
    random_paths: int,
    wf_rows: list[dict[str, Any]],
    run_id: str,
    run_id_assigned: bool,
) -> list[str]:
    out: list[str] = []
    if net.n_trades < MIN_TRADES:
        out.append(f"Only {net.n_trades} trades (< {MIN_TRADES}): too few to mean much.")
    if (
        math.isfinite(gross.sharpe)
        and math.isfinite(net.sharpe)
        and gross.sharpe - net.sharpe > COST_DRAG_SHARPE
    ):
        out.append(
            f"Costs cut the Sharpe from {gross.sharpe:.2f} gross to {net.sharpe:.2f} net; "
            "the edge depends heavily on the cost assumption."
        )
    if not math.isfinite(dsr) or dsr < DSR_THRESHOLD:
        shown = "n/a" if not math.isfinite(dsr) else f"{dsr:.2f}"
        out.append(
            f"DSR {shown} < {DSR_THRESHOLD}: not significant after {n_trials} trial"
            f"{'s' if n_trials != 1 else ''} in this run."
        )
    if run_id == "ui" or not run_id_assigned:
        out.append(
            f"DSR N = {n_trials} counts only the trials recorded under the client-chosen "
            f"run_id {run_id!r}; configurations tried in other runs or outside the lab are not "
            "deflated for."
        )
    if pbo is None:
        out.append(
            f"PBO not computed: needs >= 2 neighbourhood trials, got {pbo_trials}"
            " (plan has no tunable parameters?)."
        )
    elif pbo > PBO_THRESHOLD:
        out.append(
            f"PBO {pbo:.2f} over the ±20% parameter neighbourhood exceeds the Phase 8 bar of "
            f"{PBO_THRESHOLD:g} (neighbourhood PBO measures parameter stability, not search "
            "overfitting)"
        )
    if math.isfinite(net.sharpe) and math.isfinite(vol_target.sharpe):
        if net.sharpe < vol_target.sharpe:
            out.append(
                f"Net Sharpe {net.sharpe:.2f} is below volatility-targeted buy-and-hold "
                f"({vol_target.sharpe:.2f})."
            )
    elif not math.isfinite(net.sharpe):
        out.append("Net Sharpe is undefined (no variation in returns).")
    if random_paths and (not math.isfinite(random_pct) or random_pct < RANDOM_ENTRY_PERCENTILE):
        shown = "n/a" if not math.isfinite(random_pct) else ordinal(round(random_pct))
        out.append(
            f"Sharpe is at the {shown} percentile of {random_paths} random-entry paths with "
            f"the same trade count (< {RANDOM_ENTRY_PERCENTILE:.0f})."
        )
    if not wf_rows:
        out.append("No walk-forward fold fits in the design window.")
    else:
        out.append(WALK_FORWARD_WARNING)
    return out


# --------------------------------------------------------------------------- ungated baseline

UNGATED_NOTE = (
    "informational only: the same plan with every long_entry `and` clause that reads a "
    "derivatives feature removed (the price clause alone), run with the same costs on the same "
    "scored window. It is not a ledger trial, not a candidate, never holdout-checkable, and does "
    "not change DSR's N; it adds no pass/fail criterion. gate_binding_share = bars where the "
    "price clause wanted an entry but the gate blocked it (gate false or undefined) / bars "
    "where the price clause wanted an entry (entry signals, whether or not already in a "
    "position). plan_minus_ungated: max_drawdown is negative, so a positive difference means "
    "the gate reduced the drawdown"
)


def _expr_names(expr: str) -> set[str]:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def ungated_plan(plan: Plan) -> Plan | None:
    """``plan`` with every top-level ``and`` clause of ``long_entry`` that reads a derivatives
    feature removed (:func:`lab.plan.validator.gate_base_expressions`, the same price clause
    :func:`check_gate_base` judges), features no rule uses any more dropped, ``long_exit`` and
    risk unchanged. None when the plan has no derivatives feature or no derivatives-free entry
    clause, and always None for a risk overlay (no gate to strip; its derivatives features ARE
    the risk rule). Raises :class:`PlanValidationError` if the stripped plan does not validate."""
    if plan.kind == "overlay":
        return None
    exprs = gate_base_expressions(plan)
    if exprs is None:
        return None
    entry = exprs[0].source
    used = _expr_names(entry) | _expr_names(plan.long_exit)
    raw = plan_to_dict(plan)
    raw["long_entry"] = entry
    raw["features"] = {a: spec for a, spec in raw["features"].items() if a in used}
    return parse_plan(raw)


def _ungated_benchmark(prep: _Prepared, cfg: LabConfig, net: Metrics) -> dict[str, Any] | None:
    """The report's ``benchmarks.ungated`` block (:data:`UNGATED_NOTE`) for a single-symbol
    plan with derivatives features, or None for price-only plans and for risk overlays (their
    derivatives features ARE the risk rule; there is no gate to strip). Never touches the
    ledger."""
    if prep.scoring is None or prep.plan.kind == "overlay":
        return None
    gates = sorted(r.alias for r in prep.plan.features if r.feature in DERIVATIVES_FEATURES)
    try:
        stripped = ungated_plan(prep.plan)
    except PlanValidationError as exc:
        return {"error": f"the ungated plan does not validate: {exc}", "note": UNGATED_NOTE}
    if stripped is None:
        return {
            "error": "long_entry has no derivatives-free clause, so there is no price-only "
            "baseline to compare with",
            "gate_features": gates,
            "note": UNGATED_NOTE,
        }
    try:
        res = _run_plan(stripped, prep, cfg)
    except ValueError as exc:
        return {"error": f"the ungated plan failed to run: {exc}", "note": UNGATED_NOTE}
    um = compute_metrics(res, prep.plan.interval)
    gated_entries, _ = _signals(prep.plan, prep.history, prep.candles.index)
    price_entries, _ = _signals(stripped, prep.history, prep.candles.index)
    wanted = price_entries.to_numpy(dtype=bool)
    blocked = wanted & ~gated_entries.to_numpy(dtype=bool)
    n_wanted, n_blocked = int(wanted.sum()), int(blocked.sum())
    return {
        "plan": plan_to_dict(stripped),
        "price_clause": stripped.long_entry,
        "gate_features": gates,
        "metrics": metrics_dict(um),
        "price_clause_entry_bars": n_wanted,
        "gate_binding_bars": n_blocked,
        "gate_binding_share": (n_blocked / n_wanted) if n_wanted else None,
        "plan_minus_ungated": {
            "sharpe": num(net.sharpe - um.sharpe),
            "max_drawdown": num(net.max_drawdown - um.max_drawdown),
            "total_return": num(net.total_return - um.total_return),
        },
        "note": UNGATED_NOTE,
    }


def _random_entry_json(rnd: RandomEntryResult, real_sharpe: float) -> dict[str, Any]:
    return {
        "n_paths": int(rnd.n_paths),
        "sharpe_median": num(rnd.sharpe_median),
        "sharpe_p95": num(rnd.sharpe_p95),
        "percentile": num(rnd.percentile(real_sharpe)),
    }


# --------------------------------------------------------------------------- public API


def run_backtest_report(
    raw_plan: Mapping[str, Any] | str,
    *,
    cfg: LabConfig,
    ledger: Ledger,
    run_id: str = "ui",
    loader: CandleLoader | None = None,
    seed: int = DEFAULT_SEED,
    run_id_assigned: bool = False,
) -> dict[str, Any]:
    """The ``BacktestReport`` JSON for ``raw_plan`` on the design window (module docstring).

    ``run_id_assigned`` is True only when the caller (Phase 8: the server) assigned ``run_id``
    itself; otherwise the report warns that DSR's N counts only this client-chosen run.

    Raises :class:`PlanValidationError` for an invalid plan and :class:`DataUnavailableError`
    when the candles are missing. Engine failures are recorded as error trials and re-raised.
    """
    t0 = time.perf_counter()
    prep = _prepare(raw_plan, cfg, loader or default_loader(cfg), ledger, run_id)
    plan, interval = prep.plan, prep.plan.interval
    ppy = bars_per_year(interval)
    plan_json = json.dumps(prep.plan_dict, sort_keys=True)

    # 1. the plan itself (source "api")
    try:
        base = _run_plan(plan, prep, cfg)
    except ValueError as exc:
        _record(
            ledger,
            run_id=run_id,
            source="api",
            plan_hash_=prep.plan_hash,
            plan_json=plan_json,
            params={},
            prep=prep,
            error=str(exc),
            costs=cfg.costs,
        )
        raise
    net = compute_metrics(base, interval)
    gross = compute_metrics(base, interval, gross=True)
    base_returns = base.returns.to_numpy(dtype=float)
    trial_id = _record(
        ledger,
        run_id=run_id,
        source="api",
        plan_hash_=prep.plan_hash,
        plan_json=plan_json,
        params={},
        prep=prep,
        metrics=net,
        returns=base_returns,
        n_trades=net.n_trades,
        costs=cfg.costs,
    )

    # 2. benchmarks (one summary row each, source "benchmark")
    bh = buy_and_hold(
        prep.candles, costs=cfg.costs, init_cash=cfg.init_cash, cost_history=prep.history
    )
    bh_m = compute_metrics(bh, interval)
    _record_benchmark(
        ledger,
        run_id,
        prep,
        "buy_and_hold",
        {},
        metrics=bh_m,
        returns=bh.returns.to_numpy(dtype=float),
        n_trades=bh_m.n_trades,
        costs=cfg.costs,
    )
    b = cfg.benchmarks
    vt_params = {
        "target_annual": b.vol_target_annual,
        "lookback_days": b.vol_lookback_days,
        "max_leverage": b.vol_max_leverage,
        "min_rebalance": b.vol_min_rebalance,
    }
    vt_w = vol_target_weights(
        prep.history,
        interval,
        target_annual=b.vol_target_annual,
        lookback_days=b.vol_lookback_days,
        max_leverage=b.vol_max_leverage,
    ).loc[prep.candles.index]
    vt = run_weights(
        prep.candles,
        vt_w,
        costs=cfg.costs,
        init_cash=cfg.init_cash,
        min_rebalance=b.vol_min_rebalance,
        cost_history=prep.history,
    )
    vt_m = compute_metrics(vt, interval)
    _record_benchmark(
        ledger,
        run_id,
        prep,
        "vol_target",
        vt_params,
        metrics=vt_m,
        returns=vt.returns.to_numpy(dtype=float),
        n_trades=vt_m.n_trades,
        costs=cfg.costs,
    )
    rnd = random_entry_null(
        prep.candles,
        interval,
        n_trades=len(base.trades),
        holding_bars=[t.bars_held for t in base.trades],
        costs=cfg.costs,
        risk=plan.risk,
        n_paths=b.random_entry_paths,
        seed=seed,
        init_cash=cfg.init_cash,
        cost_history=prep.history,
    )
    rnd_json = _random_entry_json(rnd, net.sharpe)
    _record_benchmark(
        ledger,
        run_id,
        prep,
        "random_entry",
        {"n_paths": b.random_entry_paths, "n_trades": len(base.trades), "seed": seed},
        metrics_json=json.dumps(
            {
                "sharpe": rnd_json["sharpe_median"],
                "sharpe_p95": rnd_json["sharpe_p95"],
                "percentile": rnd_json["percentile"],
                "n_paths": rnd_json["n_paths"],
            },
            sort_keys=True,
        ),
        sharpe_bar=(rnd.sharpe_median / math.sqrt(ppy))
        if math.isfinite(rnd.sharpe_median)
        else None,
        n_trades=len(base.trades),
        costs=cfg.costs,
    )

    # 3. ±20% neighbourhood (source "neighbourhood") -> PBO
    neighbours = _run_neighbourhood(prep, cfg, ledger, run_id)
    columns = [base_returns, *(n.returns for n in neighbours if n.returns is not None)]
    pbo_trials = len(columns)
    n_splits = pbo_split_count(len(base_returns), cfg.stats.pbo_splits)
    pbo_value: float | None = None
    if pbo_trials >= 2 and n_splits >= 2:
        matrix = np.column_stack(columns)
        if np.isfinite(matrix).all():
            pbo_value = num(pbo_cscv(matrix, n_splits=n_splits).pbo)

    # 4. DSR: N over every window of this interval; V only from commensurable Sharpes
    #    (same interval, same design window, same costs; never walk-forward test folds).
    n_trials = max(
        1, ledger.count_distinct_plans(run_id, sources=SELECTION_SOURCES, interval=interval)
    )
    variance = ledger.selection_sharpe_variance(
        run_id,
        sources=SELECTION_SOURCES,
        interval=interval,
        window_start=prep.window.start,
        window_end=prep.window.end,
        costs_json=costs_to_json(cfg.costs),
    )
    dsr = deflated_sharpe(base_returns, n_trials=n_trials, sharpe_variance=variance)

    # 5. stationary-bootstrap CI on the annualised Sharpe
    st = cfg.stats
    ci = stationary_bootstrap_ci(
        base_returns,
        lambda r: sharpe_per_bar(r) * math.sqrt(ppy),
        n_samples=st.bootstrap_samples,
        mean_block=st.bootstrap_mean_block,
        confidence=st.confidence,
        seed=seed,
    )

    # 6. walk-forward test folds (fixed params, no tuning here; each fold is a ledger row)
    try:
        wf_rows = _walk_forward(prep, cfg, ledger, run_id, plan_json)
    except ValueError:
        wf_rows = []

    random_pct = rnd.percentile(net.sharpe)
    warnings = _warnings(
        net=net,
        gross=gross,
        vol_target=vt_m,
        dsr=dsr.dsr,
        n_trials=n_trials,
        pbo=pbo_value,
        pbo_trials=pbo_trials,
        random_pct=random_pct,
        random_paths=rnd.n_paths,
        wf_rows=wf_rows,
        run_id=run_id,
        run_id_assigned=run_id_assigned,
    )
    report: dict[str, Any] = {
        "trial_id": trial_id,
        "run_id": run_id,
        "plan_hash": prep.plan_hash,
        "plan": prep.plan_dict,
        "symbol": plan.symbol,
        "interval": interval,
        "window": {
            "kind": "design",
            "start": seconds(prep.window.start),
            "end": seconds(prep.window.end),
        },
        "costs": {
            "fee_rate": cfg.costs.fee_rate,
            "slippage_rate": cfg.costs.slippage_rate,
            "slippage_vol_mult": cfg.costs.slippage_vol_mult,
            "slippage_cap": cfg.costs.slippage_cap,
        },
        "metrics": {"net": metrics_dict(net), "gross": metrics_dict(gross)},
        "benchmarks": {
            "buy_and_hold": metrics_dict(bh_m),
            "vol_target": metrics_dict(vt_m),
            "random_entry": rnd_json,
        },
        "stats": {
            "dsr": {
                "dsr": num(dsr.dsr),
                "psr": num(dsr.psr),
                "n_trials": int(n_trials),
                "sr0_annual": num(dsr.sr0_per_bar * math.sqrt(ppy)),
            },
            "pbo": {
                "pbo": pbo_value,
                "n_trials": int(pbo_trials),
                "n_splits": int(n_splits if pbo_value is not None else 0),
                "basis": PBO_BASIS,
            },
            "sharpe_ci": {"low": num(ci.low), "high": num(ci.high), "level": float(ci.level)},
        },
        "walk_forward": wf_rows,
        "walk_forward_kind": WALK_FORWARD_KIND,
        "trades": [trade_dict(t) for t in base.trades],
        "equity": equity_points(base.equity, cfg.init_cash),
        "benchmark_equity": {
            "buy_and_hold": equity_points(bh.equity, cfg.init_cash),
            "vol_target": equity_points(vt.equity, cfg.init_cash),
        },
        "warnings": warnings,
        "disclaimer": _disclaimer(cfg),
        "elapsed_ms": 0,
    }
    if prep.scoring is not None:  # derivatives plans only: other reports are unchanged
        report["scoring"] = prep.scoring
        ungated = _ungated_benchmark(prep, cfg, net)  # informational, no ledger row
        if ungated is not None:
            report["benchmarks"]["ungated"] = ungated
    report = to_jsonable(report)
    report["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    return report


def _variant_json(v: VariantResult) -> dict[str, Any]:
    m = v.metrics
    return {
        "label": v.label,
        "status": v.status,
        "sharpe": num(v.sharpe),
        "total_return": num(m.total_return) if m is not None else None,
        "max_drawdown": num(m.max_drawdown) if m is not None else None,
        "n_trades": int(m.n_trades) if m is not None else 0,
        "error": v.error,
    }


def _variant_costs(v: VariantResult, cfg: LabConfig) -> CostModel:
    """The cost model a variant ran with (cost-stress variants scale the base model)."""
    if v.check == "cost_stress":
        return cfg.costs.scaled(float(v.params["factor"]))
    return cfg.costs


def _record_variant(
    ledger: Ledger, run_id: str, prep: _Prepared, plan_json: str, v: VariantResult, cfg: LabConfig
) -> int:
    return _record(
        ledger,
        run_id=run_id,
        source="adversarial",
        plan_hash_=prep.plan_hash,
        plan_json=plan_json,
        params=v.params,
        prep=prep,
        metrics=v.metrics,
        returns=v.returns,
        n_trades=v.metrics.n_trades if v.metrics is not None else 0,
        error=None if v.status == "ok" else (v.error or v.status),
        costs=_variant_costs(v, cfg),
    )


def run_adversarial_report(
    raw_plan: Mapping[str, Any] | str,
    *,
    cfg: LabConfig,
    ledger: Ledger,
    run_id: str = "ui",
    loader: CandleLoader | None = None,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Adversarial checks on the design window as JSON; every variant goes to the ledger.

    Shape: ``{trial_id, run_id, plan_hash, plan, symbol, interval, window, costs, base,
    delay: {rows, verdict}, sign_flip, cost_stress: {rows, verdict}, monte_carlo,
    permutation, verdicts, disclaimer, elapsed_ms}``. A "blown" sign flip is recorded with
    status "error" and reported, never raised.
    """
    t0 = time.perf_counter()
    prep = _prepare(raw_plan, cfg, loader or default_loader(cfg), ledger, run_id)
    plan_json = json.dumps(prep.plan_dict, sort_keys=True)
    rep: AdversarialReport = adversarial_checks(
        prep.plan, prep.history, prep.window, cfg=cfg, seed=seed
    )

    base = rep.base
    trial_id = _record(
        ledger,
        run_id=run_id,
        source="api",
        plan_hash_=prep.plan_hash,
        plan_json=plan_json,
        params={},
        prep=prep,
        metrics=base.metrics,
        returns=base.returns,
        n_trades=base.metrics.n_trades if base.metrics is not None else 0,
        error=None if base.status == "ok" else (base.error or base.status),
        costs=cfg.costs,
    )
    for v in (*rep.delays, rep.sign_flip, *rep.cost_stress):
        _record_variant(ledger, run_id, prep, plan_json, v, cfg)
    null = rep.permutation_sharpes
    finite = null[np.isfinite(null)]
    perm_json = {
        "n_paths": int(null.size),
        "n_valid": int(finite.size),
        "sharpe_median": num(np.median(finite)) if finite.size else None,
        "sharpe_p95": num(np.percentile(finite, 95)) if finite.size else None,
        "p_value": num(rep.permutation_p_value),
        "percentile": num(rep.permutation_percentile),
        "verdict": rep.verdicts["permutation"],
    }
    ppy = bars_per_year(prep.plan.interval)
    _record_benchmark(
        ledger,
        run_id,
        prep,
        "permutation_null",
        {"n_paths": int(null.size), "seed": seed},
        metrics_json=json.dumps(
            {
                "sharpe": perm_json["sharpe_median"],
                "sharpe_p95": perm_json["sharpe_p95"],
                "p_value": perm_json["p_value"],
                "percentile": perm_json["percentile"],
            },
            sort_keys=True,
        ),
        sharpe_bar=(
            perm_json["sharpe_median"] / math.sqrt(ppy)
            if perm_json["sharpe_median"] is not None
            else None
        ),
        n_trades=0,
        costs=cfg.costs,
    )
    mc = rep.monte_carlo
    out: dict[str, Any] = {
        "trial_id": trial_id,
        "run_id": run_id,
        "plan_hash": prep.plan_hash,
        "plan": prep.plan_dict,
        "symbol": prep.plan.symbol,
        "interval": prep.plan.interval,
        "window": {
            "kind": "design",
            "start": seconds(prep.window.start),
            "end": seconds(prep.window.end),
        },
        "costs": {
            "fee_rate": cfg.costs.fee_rate,
            "slippage_rate": cfg.costs.slippage_rate,
            "slippage_vol_mult": cfg.costs.slippage_vol_mult,
            "slippage_cap": cfg.costs.slippage_cap,
        },
        "base": _variant_json(base),
        "delay": {
            "rows": [{"delay": int(v.params["delay"]), **_variant_json(v)} for v in rep.delays],
            "verdict": rep.verdicts["delay"],
        },
        "sign_flip": {**_variant_json(rep.sign_flip), "verdict": rep.verdicts["sign_flip"]},
        "cost_stress": {
            "rows": [
                {"factor": float(v.params["factor"]), **_variant_json(v)} for v in rep.cost_stress
            ],
            "verdict": rep.verdicts["cost_stress"],
        },
        "monte_carlo": {
            "n_paths": int(mc.n_paths),
            "final_return": dict(mc.final_return),
            "max_drawdown": dict(mc.max_drawdown),
            "actual_max_drawdown": num(rep.monte_carlo_actual_drawdown),
            "verdict": rep.verdicts["monte_carlo"],
        },
        "permutation": perm_json,
        "verdicts": list(rep.verdicts.values()),
        "disclaimer": _disclaimer(cfg),
        "elapsed_ms": 0,
    }
    if prep.scoring is not None:  # derivatives plans only: other reports are unchanged
        out["scoring"] = prep.scoring
    out = to_jsonable(out)
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    return out
