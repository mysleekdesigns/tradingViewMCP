"""Optuna parameter search wired to the trial ledger (PLAN.md Phase 7 How §4, §7).

The agent (or the owner) picks the idea and the search ranges; Optuna does the number
crunching. Every Optuna trial is a ledger row, so the deflated Sharpe's trial count and the
PBO return matrix see everything that was tried:

* ``ok`` trials store the metrics, the per-bar net returns (for PBO), the non-annualised
  Sharpe (for DSR) and the cost model (``costs_json``). A trial whose Sharpe is undefined (no
  trades, zero variance) or that made fewer than ``min_trades`` trades is still an ``ok``
  backtest in the ledger with its real metrics, but Optuna sees the failure sentinel, the trial
  counts as failed in :class:`SearchResult`, and it can never be selected as the best.
* An optional ``trial_check`` (the derivatives gate-base check,
  :func:`lab.runner.gate_base_trial_check`) runs on each trial's concrete plan before its
  backtest; a trial it rejects is an ``error`` row with the reason, like any exception below.
* Any exception while building, compiling, or running a trial's plan is recorded as an
  ``error`` row with the suggested params, then reported to Optuna as the failure sentinel.
* Every row's ``params_json`` carries the study's design under the reserved key ``"_study"``:
  ``{"study", "space" ({key: [low, high]}), "space_explicit", "seed", "n_trials",
  "min_trades"}``, so the ledger alone shows what was searched and how hard. It lives in
  ``params_json`` rather than in a summary row because a summary row with a pseudo-plan would
  add a distinct ``plan_hash`` and inflate the DSR's N. Phase 8 passes ``require_space=True``
  so the search space is always stated explicitly, never the full validation bounds by default.

Holdout guard: both entry points call :func:`lab.engine.splits.assert_no_holdout` on the
candles they receive and refuse windows that reach ``holdout_start``. Signals for a window are
computed on the history through the window's end (warm-up from earlier bars, nothing after the
window is visible) and the engine runs on the window slice only, with the same history as
``cost_history`` so vol-scaled slippage is warmed up on the window's first bars too.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import optuna
import pandas as pd

from lab.config import LabConfig
from lab.engine import run_signals, slippage_series
from lab.engine.splits import assert_no_holdout, history_through, slice_window
from lab.ledger import Ledger, costs_to_json, metrics_to_json
from lab.plan import compile_plan, plan_hash, plan_to_dict, tunable_params, with_params
from lab.stats import compute_metrics, metrics_from_returns, sharpe_per_bar
from lab.types import (
    BacktestResult,
    Metrics,
    ParamSpec,
    Plan,
    Trade,
    TrialRecord,
    WalkForwardFold,
    Window,
    WindowKind,
)

# Value Optuna sees for a failed trial (error, or undefined Sharpe). TPE splits trials into
# "good" and "bad" by rank, so any finite value below every real Sharpe works; a finite number
# avoids Optuna treating the trial as FAIL and dropping it from the model entirely.
FAILED_OBJECTIVE = -1e9

# Fallback minimum trades for a trial to be selectable when callers don't pass one; the CLI and
# Phase 8 tools read config/lab.toml optuna.min_trades (same value and justification).
MIN_TRADES = 30

STUDY_KEY = "_study"  # reserved params_json key holding the study's design (never a param)

SearchSpace = Mapping[str, tuple[float, float]]

# A per-trial admissibility check on the concrete plan: raise (any Exception) to fail the trial
# before its backtest (recorded as an ``error`` row with the message, never selected).
TrialCheck = Callable[[Plan], None]


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class SearchResult:
    """Outcome of one Optuna study on one window.

    ``best_params``/``best_plan`` are ``None`` and ``best_value`` is NaN when no trial produced
    a finite Sharpe. ``trial_ids`` are the ledger ids of every trial, in trial order.
    """

    best_params: dict[str, float] | None
    best_value: float
    best_plan: Plan | None
    n_trials: int
    n_failed: int
    trial_ids: tuple[int, ...]
    study_name: str
    window: Window


@dataclass(frozen=True)
class FoldResult:
    """One walk-forward fold: params chosen on ``train`` only, then evaluated once on ``test``.

    ``selected`` is False when no train trial had a finite Sharpe; the fold then falls back to
    the plan's own parameter values (a selection-free choice that never looks at the test).
    """

    fold: int
    train: Window
    test: Window
    best_params: dict[str, float]
    selected: bool
    train_sharpe: float
    test_metrics: Metrics
    test_trial_id: int
    search: SearchResult


@dataclass(frozen=True)
class WalkForwardResult:
    """Walk-forward optimisation. ``oos_returns`` stitches every fold's TEST returns (each test
    starts flat; bar 0 of each test is 0) and ``oos_metrics`` is the honest out-of-sample number.

    Fold boundaries: a position still open at the end of a test window is marked at its last
    close without an exit fee (engine convention), and the next fold starts flat, so stitching
    would get a free exit at every boundary, a small optimistic bias. To remove it, the last
    stitched return of every fold except the final one is charged the exit cost of its open
    position: ``|w| x (1 - (1 - s)(1 - f))`` of equity for a long (``|w| x ((1 + s)(1 + f) - 1)``
    for a short), with ``w`` the exposure at the last close, ``f`` the fee rate and ``s`` the
    window's last-bar slippage rate (the closest causal rate; the real exit would fill at the
    next bar's open). ``boundary_charges`` holds that fraction per fold (0.0 when flat, and for
    the final fold, whose open trade is reported as ``"end"`` like any backtest).
    ``FoldResult.test_metrics`` and the ledger's test rows are the uncharged window backtests.
    """

    folds: tuple[FoldResult, ...]
    oos_returns: pd.Series
    oos_metrics: Metrics
    n_train_trials: int
    test_trial_ids: tuple[int, ...]
    boundary_charges: tuple[float, ...] = ()


# --------------------------------------------------------------------------- validation


def resolve_space(plan: Plan, space: SearchSpace | None = None) -> dict[str, ParamSpec]:
    """Search space as ``{key: ParamSpec}`` with ``low``/``high`` set to the searched range.

    Default: every :func:`lab.plan.tunable_params` key with its full validation bounds.
    ``space`` replaces the default with only the keys it names, each narrowed to its
    ``(low, high)``. Raises ``ValueError`` naming the key for an unknown key, a range outside
    the param's bounds, ``low > high``, or non-integer bounds on an int param.
    """
    tunables = tunable_params(plan)
    if space is None:
        if not tunables:
            raise ValueError(f"plan {plan.name!r} has no tunable params to search")
        return dict(tunables)
    if not isinstance(space, Mapping) or not space:
        raise ValueError("space must be a non-empty mapping of param key -> (low, high)")
    out: dict[str, ParamSpec] = {}
    for key, bounds in space.items():
        spec = tunables.get(key)
        if spec is None:
            raise ValueError(
                f"space key {key!r} is not a tunable param of plan {plan.name!r}; "
                f"tunable: {', '.join(tunables) or '(none)'}"
            )
        if not isinstance(bounds, tuple | list) or len(bounds) != 2:
            raise ValueError(f"space[{key!r}] must be a (low, high) pair, got {bounds!r}")
        low, high = bounds
        if not all(_is_finite_number(v) for v in (low, high)):
            raise ValueError(f"space[{key!r}] bounds must be finite numbers, got {bounds!r}")
        low_f, high_f = float(low), float(high)
        if low_f > high_f:
            raise ValueError(f"space[{key!r}] low {low_f:g} must be <= high {high_f:g}")
        if low_f < spec.low or high_f > spec.high:
            raise ValueError(
                f"space[{key!r}] range [{low_f:g}, {high_f:g}] is outside the param's bounds "
                f"[{spec.low:g}, {spec.high:g}]"
            )
        if spec.kind == "int" and not (low_f.is_integer() and high_f.is_integer()):
            raise ValueError(
                f"space[{key!r}] is an int param; bounds must be whole numbers, got {bounds!r}"
            )
        out[key] = ParamSpec(
            name=spec.name,
            kind=spec.kind,
            default=spec.default,
            low=low_f,
            high=high_f,
            description=spec.description,
        )
    return out


def _is_finite_number(v: object) -> bool:
    return (
        not isinstance(v, bool)
        and isinstance(v, int | float | np.integer | np.floating)
        and math.isfinite(float(v))
    )


def _check_positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | np.integer) or value <= 0:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return int(value)


def _check_min_trades(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | np.integer) or value < 0:
        raise ValueError(f"min_trades must be an int >= 0, got {value!r}")
    return int(value)


def _check_seed(seed: object) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int | np.integer):
        raise ValueError(f"seed must be an int, got {seed!r}")
    return int(seed)


def _check_window_before_holdout(window: Window, cfg: LabConfig, name: str) -> None:
    if not isinstance(window, Window):
        raise ValueError(f"{name} must be a Window, got {type(window).__name__}")
    if window.end > cfg.splits.holdout_start:
        raise ValueError(
            f"{name} ends {window.end.isoformat()}, after holdout_start "
            f"{cfg.splits.holdout_start.isoformat()}; tuning must stay inside the design window"
        )


def _check_space_given(space: SearchSpace | None, require_space: bool) -> None:
    if not isinstance(require_space, bool):
        raise ValueError(f"require_space must be a bool, got {require_space!r}")
    if require_space and space is None:
        raise ValueError(
            "space is required (require_space=True): pass {param key: (low, high)} for every "
            "param to search; the default full validation bounds are not allowed here"
        )


def _check_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(f"run_id must be a non-empty string, got {run_id!r}")
    return run_id


# --------------------------------------------------------------------------- evaluation


def evaluate_on_window(
    plan: Plan, candles: pd.DataFrame, window: Window, cfg: LabConfig
) -> BacktestResult:
    """Backtest ``plan`` on ``window``: signals from the history through ``window.end`` (so
    indicators are warmed up at the window start), engine run on the window slice with that
    history as ``cost_history`` (vol-scaled slippage warmed up the same way)."""
    history = history_through(candles, window)
    signals = compile_plan(plan, history)
    bars = slice_window(history, window)
    if len(bars) == 0:
        raise ValueError(
            f"window [{window.start.isoformat()}, {window.end.isoformat()}) contains no bars"
        )
    return run_signals(
        bars,
        signals.entries.loc[bars.index],
        signals.exits.loc[bars.index],
        costs=cfg.costs,
        risk=plan.risk,
        init_cash=cfg.init_cash,
        cost_history=history,
    )


def _plan_json(plan: Plan) -> str:
    return json.dumps(plan_to_dict(plan), sort_keys=True, separators=(",", ":"))


def _params_json(params: Mapping[str, float], study: Mapping[str, object]) -> str:
    clean: dict[str, object] = {
        k: (float(v) if math.isfinite(float(v)) else None) for k, v in params.items()
    }
    clean[STUDY_KEY] = dict(study)
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _study_meta(
    study_name: str,
    space: Mapping[str, ParamSpec],
    *,
    explicit: bool,
    seed: int,
    n_trials: int,
    min_trades: int,
) -> dict[str, object]:
    """The ``"_study"`` entry of every row's ``params_json`` (see the module docstring)."""
    return {
        "study": study_name,
        "space": {k: [float(s.low), float(s.high)] for k, s in space.items()},
        "space_explicit": explicit,
        "seed": seed,
        "n_trials": n_trials,
        "min_trades": min_trades,
    }


def _record_ok(
    ledger: Ledger,
    *,
    run_id: str,
    plan: Plan,
    params: Mapping[str, float],
    study: Mapping[str, object],
    cfg: LabConfig,
    window: Window,
    window_kind: WindowKind,
    result: BacktestResult,
    metrics: Metrics,
) -> int:
    returns = result.returns.to_numpy(dtype=float)
    return ledger.record(
        TrialRecord(
            run_id=run_id,
            source="optuna",
            status="ok",
            plan_hash=plan_hash(plan),
            plan_json=_plan_json(plan),
            params_json=_params_json(params, study),
            symbol=plan.symbol,
            interval=plan.interval,
            window_kind=window_kind,
            window_start=window.start,
            window_end=window.end,
            metrics_json=metrics_to_json(metrics),
            sharpe_per_bar=sharpe_per_bar(returns),
            n_trades=len(result.trades),
            costs_json=costs_to_json(cfg.costs),
            returns=returns,
        )
    )


def _record_error(
    ledger: Ledger,
    *,
    run_id: str,
    plan: Plan,
    params: Mapping[str, float],
    study: Mapping[str, object],
    cfg: LabConfig,
    window: Window,
    window_kind: WindowKind,
    exc: BaseException,
) -> int:
    return ledger.record(
        TrialRecord(
            run_id=run_id,
            source="optuna",
            status="error",
            plan_hash=plan_hash(plan),
            plan_json=_plan_json(plan),
            params_json=_params_json(params, study),
            symbol=plan.symbol,
            interval=plan.interval,
            window_kind=window_kind,
            window_start=window.start,
            window_end=window.end,
            metrics_json="{}",
            sharpe_per_bar=None,
            n_trades=0,
            error=f"{type(exc).__name__}: {exc}",
            costs_json=costs_to_json(cfg.costs),
        )
    )


# Public names for other searches that write the same ledger rows (lab.portfolio_runner): the
# "_study" design in params_json is what the sweep-PBO basis and the walk-forward lineage read.
params_json = _params_json
study_meta = _study_meta


def _suggest(trial: optuna.Trial, space: Mapping[str, ParamSpec]) -> dict[str, float]:
    params: dict[str, float] = {}
    for key, spec in space.items():
        if spec.kind == "int":
            params[key] = float(trial.suggest_int(key, int(spec.low), int(spec.high)))
        else:
            params[key] = float(trial.suggest_float(key, spec.low, spec.high))
    return params


# --------------------------------------------------------------------------- search


def optuna_search(
    plan: Plan,
    candles: pd.DataFrame,
    window: Window,
    *,
    ledger: Ledger,
    run_id: str,
    cfg: LabConfig,
    n_trials: int,
    seed: int,
    space: SearchSpace | None = None,
    min_trades: int = MIN_TRADES,
    require_space: bool = False,
    objective: Literal["sharpe"] = "sharpe",
    window_kind: WindowKind = "design",
    trial_check: TrialCheck | None = None,
) -> SearchResult:
    """Maximise the annualised net Sharpe of ``plan`` on ``window`` with a seeded TPE sampler.

    ``candles`` must exclude the locked holdout (``ValueError`` otherwise). Every trial,
    including failures, is written to ``ledger`` with ``source="optuna"`` and ``window_kind``.
    A trial with fewer than ``min_trades`` trades (open trade at the end included) keeps its
    real metrics in the ledger but is failed for Optuna and never selected. With
    ``require_space=True`` a missing ``space`` is a ``ValueError`` (Phase 8 research must state
    its ranges). The same ``seed`` and inputs give the same trials and the same best params.

    ``trial_check`` (e.g. :func:`lab.runner.gate_base_trial_check` for a derivatives-gated plan)
    runs on each trial's concrete plan BEFORE its backtest; if it raises, the trial is an
    ``error`` ledger row carrying the reason (it counts in DSR's N), Optuna sees the failure
    sentinel, and the trial is never selected.
    """
    assert_no_holdout(candles, cfg.splits)
    _check_window_before_holdout(window, cfg, "window")
    run_id = _check_run_id(run_id)
    n_trials = _check_positive_int(n_trials, "n_trials")
    seed = _check_seed(seed)
    min_trades = _check_min_trades(min_trades)
    if objective != "sharpe":
        raise ValueError(f"objective must be 'sharpe', got {objective!r}")
    _check_space_given(space, require_space)
    resolved = resolve_space(plan, space)
    study_name = (
        f"{run_id}:{plan.name}:{window_kind}:"
        f"{window.start.strftime('%Y%m%d')}-{window.end.strftime('%Y%m%d')}:seed{seed}"
    )
    study_meta = _study_meta(
        study_name,
        resolved,
        explicit=space is not None,
        seed=seed,
        n_trials=n_trials,
        min_trades=min_trades,
    )

    trial_ids: list[int] = []
    sharpes: list[float] = []  # finite Sharpe or NaN, per trial
    params_by_trial: list[dict[str, float]] = []

    def objective_fn(trial: optuna.Trial) -> float:
        params: dict[str, float] = {}
        concrete = plan
        try:
            params = _suggest(trial, resolved)
            concrete = with_params(plan, params)
            if trial_check is not None:
                trial_check(concrete)  # raises: an error row, no backtest
            result = evaluate_on_window(concrete, candles, window, cfg)
            metrics = compute_metrics(result, concrete.interval)
        except Exception as exc:  # every failure becomes an error row, never a lost trial
            trial_ids.append(
                _record_error(
                    ledger,
                    run_id=run_id,
                    plan=concrete,
                    params=params,
                    study=study_meta,
                    cfg=cfg,
                    window=window,
                    window_kind=window_kind,
                    exc=exc,
                )
            )
            sharpes.append(math.nan)
            params_by_trial.append(params)
            trial.set_user_attr("failed", f"{type(exc).__name__}: {exc}")
            return FAILED_OBJECTIVE
        trial_ids.append(
            _record_ok(
                ledger,
                run_id=run_id,
                plan=concrete,
                params=params,
                study=study_meta,
                cfg=cfg,
                window=window,
                window_kind=window_kind,
                result=result,
                metrics=metrics,
            )
        )
        params_by_trial.append(params)
        sharpe = float(metrics.sharpe)
        n_trades = len(result.trades)
        if n_trades < min_trades:
            sharpes.append(math.nan)
            trial.set_user_attr("failed", f"{n_trades} trades < min_trades {min_trades}")
            return FAILED_OBJECTIVE
        if not math.isfinite(sharpe):
            sharpes.append(math.nan)
            trial.set_user_attr("failed", "undefined Sharpe (no trades or zero variance)")
            return FAILED_OBJECTIVE
        sharpes.append(sharpe)
        return sharpe

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=seed),
        study_name=study_name,
    )
    study.optimize(objective_fn, n_trials=n_trials, catch=())

    finite = [i for i, s in enumerate(sharpes) if math.isfinite(s)]
    if finite:
        best_i = max(finite, key=lambda i: (sharpes[i], -i))  # earliest trial wins ties
        best_params: dict[str, float] | None = params_by_trial[best_i]
        best_value = sharpes[best_i]
        best_plan: Plan | None = with_params(plan, params_by_trial[best_i])
    else:
        best_params, best_value, best_plan = None, math.nan, None
    return SearchResult(
        best_params=best_params,
        best_value=best_value,
        best_plan=best_plan,
        n_trials=len(trial_ids),
        n_failed=len(sharpes) - len(finite),
        trial_ids=tuple(trial_ids),
        study_name=study_name,
        window=window,
    )


# --------------------------------------------------------------------------- walk-forward


def _check_folds(folds: Sequence[WalkForwardFold], cfg: LabConfig) -> list[WalkForwardFold]:
    if not isinstance(folds, Sequence) or len(folds) == 0:
        raise ValueError("folds must be a non-empty sequence of WalkForwardFold")
    out = list(folds)
    for k, fold in enumerate(out):
        if not isinstance(fold, WalkForwardFold):
            raise ValueError(f"folds[{k}] must be a WalkForwardFold, got {type(fold).__name__}")
        _check_window_before_holdout(fold.train, cfg, f"folds[{k}].train")
        _check_window_before_holdout(fold.test, cfg, f"folds[{k}].test")
        if fold.train.end > fold.test.start:
            raise ValueError(
                f"folds[{k}] train ends {fold.train.end.isoformat()} after its test starts "
                f"{fold.test.start.isoformat()}; selection would see test data"
            )
        if k and fold.test.start < out[k - 1].test.end:
            raise ValueError(
                f"folds[{k}] test window overlaps folds[{k - 1}]; stitched out-of-sample "
                "returns need non-overlapping, increasing test windows"
            )
    return out


def walk_forward_optimize(
    plan: Plan,
    candles: pd.DataFrame,
    folds: Sequence[WalkForwardFold],
    *,
    ledger: Ledger,
    run_id: str,
    cfg: LabConfig,
    n_trials_per_fold: int,
    seed: int,
    space: SearchSpace | None = None,
    min_trades: int = MIN_TRADES,
    require_space: bool = False,
    trial_check: TrialCheck | None = None,
) -> WalkForwardResult:
    """Walk-forward optimisation: per fold, search on the TRAIN window only (trials recorded as
    ``walk_forward_train``), then evaluate the chosen params once on the TEST window (recorded
    as ``walk_forward_test``). Fold ``i`` uses sampler seed ``seed + i``. ``min_trades`` and
    ``require_space`` apply to every fold's search (see :func:`optuna_search`); the test
    evaluation itself is never filtered. ``trial_check`` applies to every fold's train trials
    (a failing trial is never chosen for the test); the fallback when no train trial is
    selectable is the plan's own values, which the caller has already checked.

    The test evaluation computes signals on history through the test end (train bars serve as
    warm-up) and starts flat. ``candles`` must exclude the locked holdout. See
    :class:`WalkForwardResult` for the exit charge at fold boundaries.
    """
    assert_no_holdout(candles, cfg.splits)
    run_id = _check_run_id(run_id)
    n_trials_per_fold = _check_positive_int(n_trials_per_fold, "n_trials_per_fold")
    seed = _check_seed(seed)
    min_trades = _check_min_trades(min_trades)
    _check_space_given(space, require_space)
    checked = _check_folds(folds, cfg)
    resolve_space(plan, space)  # fail before any trial if the space is invalid

    fold_results: list[FoldResult] = []
    test_returns: list[pd.Series] = []
    test_positions: list[pd.Series] = []
    test_trades: list[Trade] = []
    charges: list[float] = []
    for k, fold in enumerate(checked):
        search = optuna_search(
            plan,
            candles,
            fold.train,
            ledger=ledger,
            run_id=run_id,
            cfg=cfg,
            n_trials=n_trials_per_fold,
            seed=seed + fold.index,
            space=space,
            min_trades=min_trades,
            window_kind="walk_forward_train",
            trial_check=trial_check,
        )
        selected = search.best_plan is not None and search.best_params is not None
        if search.best_plan is not None and search.best_params is not None:
            chosen_plan, chosen_params = search.best_plan, search.best_params
        else:  # nothing tradeable in train: fall back to the plan's own values
            chosen_plan = plan
            chosen_params = {k: s.default for k, s in resolve_space(plan, space).items()}
        result = evaluate_on_window(chosen_plan, candles, fold.test, cfg)
        metrics = compute_metrics(result, chosen_plan.interval)
        test_meta = _study_meta(
            search.study_name,
            resolve_space(plan, space),
            explicit=space is not None,
            seed=seed + fold.index,
            n_trials=n_trials_per_fold,
            min_trades=min_trades,
        )
        test_id = _record_ok(
            ledger,
            run_id=run_id,
            plan=chosen_plan,
            params=chosen_params,
            study=test_meta,
            cfg=cfg,
            window=fold.test,
            window_kind="walk_forward_test",
            result=result,
            metrics=metrics,
        )
        fold_results.append(
            FoldResult(
                fold=fold.index,
                train=fold.train,
                test=fold.test,
                best_params=dict(chosen_params),
                selected=selected,
                train_sharpe=search.best_value,
                test_metrics=metrics,
                test_trial_id=test_id,
                search=search,
            )
        )
        returns = result.returns
        charge = 0.0
        if k < len(checked) - 1:  # the next fold restarts flat: charge this fold's exit
            charge = _boundary_charge(candles, fold.test, result, cfg)
            if charge:
                returns = returns.copy()
                returns.iloc[-1] = (1.0 + returns.iloc[-1]) * (1.0 - charge) - 1.0
        charges.append(charge)
        test_returns.append(returns)
        test_positions.append(result.position)
        test_trades.extend(result.trades)

    oos_returns = pd.concat(test_returns).rename("oos_returns")
    oos_position = pd.concat(test_positions)
    oos_metrics = metrics_from_returns(
        oos_returns, plan.interval, position=oos_position, trades=test_trades
    )
    return WalkForwardResult(
        folds=tuple(fold_results),
        oos_returns=oos_returns,
        oos_metrics=oos_metrics,
        n_train_trials=sum(f.search.n_trials for f in fold_results),
        test_trial_ids=tuple(f.test_trial_id for f in fold_results),
        boundary_charges=tuple(charges),
    )


def _boundary_charge(
    candles: pd.DataFrame, window: Window, result: BacktestResult, cfg: LabConfig
) -> float:
    """Fraction of equity an exit of the position open at the window's last close would cost
    (fee + slippage on its traded value); 0.0 when flat. See :class:`WalkForwardResult`."""
    w = float(result.position.iloc[-1])
    if w == 0.0 or not math.isfinite(w):
        return 0.0
    history = history_through(candles, window)
    s = float(slippage_series(history, cfg.costs)[-1])
    f = float(cfg.costs.fee_rate)
    per_unit = 1.0 - (1.0 - s) * (1.0 - f) if w > 0 else (1.0 + s) * (1.0 + f) - 1.0
    return abs(w) * per_unit
