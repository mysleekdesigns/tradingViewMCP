"""Backtest report runner used by the API and UI (PLAN.md Phase 7 How §4-6, §9).

``run_backtest_report`` turns a raw plan into the ``BacktestReport`` JSON the web panel draws:
net + gross metrics, benchmarks, DSR/PSR, PBO, bootstrap CI, walk-forward test-fold metrics
(``walk_forward_kind = "fixed_params"``: the plan's own parameters on each test fold, not a
tuned out-of-sample result), trades, equity curves, warnings, and a disclaimer.
``run_adversarial_report`` runs the adversarial checks. Both work on the **design window only**:

* Window = ``[max(design_start, first bar), holdout_start)`` (or up to the last bar if the
  data ends earlier). Candles are loaded with ``end = holdout_start`` so holdout rows are never
  even read; ``assert_no_holdout`` guards the frame anyway.
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

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from lab.config import LabConfig
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
from lab.plan import compile_plan, parse_plan, plan_hash, plan_to_dict, tunable_params, with_params
from lab.plan.validator import PlanValidationError
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
    BacktestResult,
    CostModel,
    Metrics,
    ParamSpec,
    Plan,
    TrialRecord,
    TrialSource,
    Window,
    bars_per_year,
    interval_seconds,
)

__all__ = [
    "CandleLoader",
    "DataUnavailableError",
    "archive_cli_hint",
    "default_loader",
    "neighbourhood_overrides",
    "pbo_split_count",
    "run_adversarial_report",
    "run_backtest_report",
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


class DataUnavailableError(RuntimeError):
    """No candles for the plan's symbol/interval in the design window; ``str()`` names the CLI."""


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
    live Binance bars without checksums and is deliberately never mixed in for research."""

    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        folder = Path(cfg.parquet_dir) / symbol / interval
        if not folder.is_dir() or not any(folder.glob("*.parquet")):
            raise DataUnavailableError(
                f"no archive data for {symbol} {interval}; download it with "
                f"`{archive_cli_hint(symbol, interval)}`"
            )
        return load_candles(symbol, interval, parquet_dir=cfg.parquet_dir, end=end)

    return load


@dataclass(frozen=True)
class _Prepared:
    plan: Plan
    plan_hash: str
    plan_dict: dict[str, Any]
    history: pd.DataFrame  # design view: every bar < holdout_start (and < window end)
    window: Window
    candles: pd.DataFrame  # history sliced to the window


def _design_window(history: pd.DataFrame, cfg: LabConfig, interval: str) -> Window | None:
    if history.empty:
        return None
    start = max(cfg.splits.design_start, history.index[0])
    last_end = history.index[-1] + pd.Timedelta(seconds=interval_seconds(interval))
    end = min(cfg.splits.holdout_start, last_end)
    if end <= start:
        return None
    return Window(start=start, end=end, kind="design")


def _prepare(raw_plan: Mapping[str, Any] | str, cfg: LabConfig, loader: CandleLoader) -> _Prepared:
    plan = parse_plan(raw_plan)  # PlanValidationError lists every problem
    raw = loader(plan.symbol, plan.interval, cfg.splits.holdout_start)
    history = design_view(raw, cfg.splits)
    assert_no_holdout(history, cfg.splits)
    window = _design_window(history, cfg, plan.interval)
    candles = slice_window(history, window) if window is not None else history.iloc[:0]
    if window is None or len(candles) < 2:
        raise DataUnavailableError(
            f"no {plan.symbol} {plan.interval} bars in the design window "
            f"[{cfg.splits.design_start.date()}, {cfg.splits.holdout_start.date()}); download "
            f"them with `{archive_cli_hint(plan.symbol, plan.interval)}`"
        )
    history = history.loc[history.index < window.end]
    return _Prepared(plan, plan_hash(plan), plan_to_dict(plan), history, window, candles)


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
    prep = _prepare(raw_plan, cfg, loader or default_loader(cfg))
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
    prep = _prepare(raw_plan, cfg, loader or default_loader(cfg))
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
    out = to_jsonable(out)
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    return out
