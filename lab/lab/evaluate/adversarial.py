"""Adversarial checks (PLAN.md Phase 7 How §6): try to break a result before believing it.

* **Signal delay** 1/3/5 bars (engine ``delay=``): a real edge decays gradually. One that
  vanishes after a single bar smells of look-ahead or fill-timing luck; one that survives
  5 bars *unchanged* is just as suspicious.
* **Sign flip**: the same signals traded short. If the opposite rule does as well, the rule's
  direction carries no information. A short that cannot be covered is reported as "blown".
* **Cost stress** x2 / x4 (``CostModel.scaled``): if the edge dies at x2 the cost assumption is
  doing the work.
* **Monte Carlo** trade-order reshuffle of the per-trade *equity* returns
  (``return_pct * position_size_pct / 100``): is the realised drawdown typical of the trades?
* **Permutation null** through the full pipeline (:func:`permute_history`, after Masters):
  keep every warm-up bar before the window unchanged, permute only the window's bars (anchored
  on the last warm-up close, so the window's total log return is exactly the real one),
  recompute the signals from scratch, run the engine on the same window, and rank the real
  Sharpe against the null Sharpes. Permuting the warm-up into the window would change the
  window's drift (a bear warm-up drags a bull window down) and bias the null.

Signals are always computed on ``history_through(window)`` (earlier bars are warm-up) and then
sliced to the window, exactly like the report runner; the same history is the engine's
``cost_history`` so the window's slippage is warmed up too. No I/O, no ledger: the runner
records the variants. Results are historical and not forward-looking.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd

from lab.engine import run_signals
from lab.engine.splits import assert_no_holdout, history_through, slice_window
from lab.plan import compile_plan
from lab.report import ordinal
from lab.stats import (
    MonteCarloResult,
    compute_metrics,
    percentile_rank,
    permutation_p_value,
    permute_candles,
    sharpe_per_bar,
    trade_reshuffle,
)
from lab.types import (
    BacktestResult,
    CostModel,
    Direction,
    Metrics,
    Plan,
    RiskSpec,
    Window,
)

if TYPE_CHECKING:
    from lab.config import LabConfig

__all__ = [
    "AdversarialReport",
    "SignalFn",
    "VariantResult",
    "adversarial_checks",
    "adversarial_checks_signals",
    "cost_stress_verdict",
    "delay_check",
    "delay_verdict",
    "monte_carlo_verdict",
    "permutation_verdict",
    "permute_history",
    "plan_signal_fn",
    "run_variant",
    "sign_flip_verdict",
    "trade_equity_returns",
]

SignalFn = Callable[[pd.DataFrame], tuple[pd.Series, pd.Series]]
VariantStatus = Literal["ok", "blown", "error"]

OK = "✓"
WARN = "⚠"

# Verdict thresholds (they only change the wording of a verdict, never a number):
DELAY_VANISH_RATIO = 0.25  # 1-bar-delayed Sharpe below 25% of the original = "vanishes"
DELAY_UNCHANGED_RATIO = 0.9  # 5-bar-delayed Sharpe still >= 90% of the original = "unchanged"
PERMUTATION_ALPHA = 0.05  # one-sided p-value needed to call the null beaten
_BLOWN_MARKER = "equity exhausted"  # engine message when a short cannot be covered


@dataclass(frozen=True)
class VariantResult:
    """One engine run of a variant. ``sharpe`` is annualised (365d); NaN when not ok."""

    check: str  # "base", "delay", "sign_flip", "cost_stress"
    label: str  # e.g. "delay=3", "cost x2"
    params: Mapping[str, float | int | str]
    status: VariantStatus
    metrics: Metrics | None
    returns: np.ndarray | None  # per-bar net returns on the window
    sharpe: float
    sharpe_per_bar: float
    error: str | None = None
    result: BacktestResult | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class AdversarialReport:
    window: Window
    base: VariantResult
    delays: tuple[VariantResult, ...]
    sign_flip: VariantResult
    cost_stress: tuple[VariantResult, ...]
    monte_carlo: MonteCarloResult
    monte_carlo_actual_drawdown: float  # max drawdown of the trades in their real order
    permutation_sharpes: np.ndarray  # annualised null Sharpes (NaN where undefined)
    permutation_p_value: float
    permutation_percentile: float
    verdicts: Mapping[str, str]  # delay, sign_flip, cost_stress, monte_carlo, permutation
    seed: int


# --------------------------------------------------------------------------- engine runs


def run_variant(
    candles: pd.DataFrame,
    entries: pd.Series,
    exits: pd.Series,
    *,
    interval: str,
    costs: CostModel,
    risk: RiskSpec,
    init_cash: float,
    check: str,
    label: str,
    params: Mapping[str, float | int | str] | None = None,
    direction: Direction = "long",
    delay: int = 0,
    cost_history: pd.DataFrame | None = None,
) -> VariantResult:
    """Run one variant; a short whose equity is exhausted becomes ``status="blown"``, any
    other engine ``ValueError`` ``status="error"`` (both with the message), never an
    exception. ``cost_history`` is passed to the engine (slippage warm-up)."""
    try:
        res = run_signals(
            candles,
            entries,
            exits,
            costs=costs,
            risk=risk,
            direction=direction,
            init_cash=init_cash,
            delay=delay,
            cost_history=cost_history,
        )
    except ValueError as exc:
        status: VariantStatus = "blown" if _BLOWN_MARKER in str(exc) else "error"
        return VariantResult(
            check, label, dict(params or {}), status, None, None, math.nan, math.nan, str(exc)
        )
    m = compute_metrics(res, interval)
    returns = res.returns.to_numpy(dtype=float)
    return VariantResult(
        check=check,
        label=label,
        params=dict(params or {}),
        status="ok",
        metrics=m,
        returns=returns,
        sharpe=float(m.sharpe),
        sharpe_per_bar=float(sharpe_per_bar(returns)),
        result=res,
    )


def delay_check(
    candles: pd.DataFrame,
    entries: pd.Series,
    exits: pd.Series,
    *,
    interval: str,
    costs: CostModel,
    risk: RiskSpec,
    delays: Sequence[int],
    init_cash: float = 10_000.0,
    cost_history: pd.DataFrame | None = None,
) -> tuple[VariantResult, tuple[VariantResult, ...], str]:
    """``(base, delayed variants, verdict)`` for the given signals (no plan needed, so a test
    can feed hand-built signals, including deliberately leaky ones)."""
    base = run_variant(
        candles,
        entries,
        exits,
        interval=interval,
        costs=costs,
        risk=risk,
        init_cash=init_cash,
        check="base",
        label="base",
        cost_history=cost_history,
    )
    delayed = tuple(
        run_variant(
            candles,
            entries,
            exits,
            interval=interval,
            costs=costs,
            risk=risk,
            init_cash=init_cash,
            check="delay",
            label=f"delay={d}",
            params={"variant": "delay", "delay": int(d)},
            delay=int(d),
            cost_history=cost_history,
        )
        for d in delays
    )
    verdict = delay_verdict(
        base.sharpe, {int(d): v.sharpe for d, v in zip(delays, delayed, strict=True)}
    )
    return base, delayed, verdict


# --------------------------------------------------------------------------- verdicts


def _fmt(x: float) -> str:
    return "n/a" if not math.isfinite(x) else f"{x:.2f}"


def delay_verdict(base_sharpe: float, delayed: Mapping[int, float]) -> str:
    """Plain-language verdict on Sharpe vs signal delay (annualised Sharpes)."""
    if not delayed:
        return f"delay: {WARN} not run"
    trail = " -> ".join(
        [f"0 bars {_fmt(base_sharpe)}", *(f"{d} {_fmt(s)}" for d, s in sorted(delayed.items()))]
    )
    if not math.isfinite(base_sharpe) or base_sharpe <= 0:
        return f"delay: no edge to test at 0 bars ({trail})"
    first, last = min(delayed), max(delayed)
    s_first, s_last = delayed[first], delayed[last]
    if not math.isfinite(s_first) or s_first < DELAY_VANISH_RATIO * base_sharpe:
        return (
            f"delay: {WARN} edge vanishes after {first} bar{'s' if first != 1 else ''} "
            f"(look-ahead or fill-timing artefact?) ({trail})"
        )
    if last > first and math.isfinite(s_last) and s_last >= DELAY_UNCHANGED_RATIO * base_sharpe:
        return f"delay: {WARN} suspicious, edge unchanged at {last} bars ({trail})"
    return f"delay: {OK} edge decays gradually ({trail})"


def sign_flip_verdict(base: VariantResult, flipped: VariantResult) -> str:
    if flipped.status == "blown":
        return f"sign flip: {OK} the flipped (short) rule blew up: equity exhausted"
    if flipped.status != "ok":
        return f"sign flip: {WARN} could not run ({flipped.error})"
    b, f = base.sharpe, flipped.sharpe
    if not math.isfinite(b):
        return f"sign flip: {WARN} base Sharpe undefined (flipped {_fmt(f)})"
    if b > 0 and (not math.isfinite(f) or f < 0):
        return f"sign flip: {OK} flipping the signal loses money ({_fmt(b)} -> {_fmt(f)})"
    if math.isfinite(f) and f >= b:
        return (
            f"sign flip: {WARN} the opposite rule does as well or better "
            f"({_fmt(b)} -> {_fmt(f)}): the direction carries no information"
        )
    return f"sign flip: {WARN} both directions are weak ({_fmt(b)} -> {_fmt(f)})"


def cost_stress_verdict(base_sharpe: float, stressed: Mapping[float, float]) -> str:
    trail = ", ".join(
        [f"x1 {_fmt(base_sharpe)}", *(f"x{k:g} {_fmt(s)}" for k, s in sorted(stressed.items()))]
    )
    if not math.isfinite(base_sharpe) or base_sharpe <= 0:
        return f"cost stress: no edge at base costs ({trail})"
    for factor, s in sorted(stressed.items()):
        if not math.isfinite(s) or s <= 0:
            return (
                f"cost stress: {WARN} edge disappears at x{factor:g} costs, the cost "
                f"assumption is doing the work ({trail})"
            )
    worst = max(stressed) if stressed else 1.0
    return f"cost stress: {OK} edge survives x{worst:g} costs ({trail})"


def monte_carlo_verdict(mc: MonteCarloResult, actual_drawdown: float, n_trades: int) -> str:
    if n_trades < 2:
        return f"monte carlo: {WARN} fewer than 2 trades, nothing to reshuffle"
    p5, p95 = mc.max_drawdown["p5"], mc.max_drawdown["p95"]
    if actual_drawdown > p95:
        return (
            f"monte carlo: {WARN} realised trade drawdown {actual_drawdown:.1%} is milder than "
            f"95% of {mc.n_paths} reshuffles (worst 5% {p5:.1%}): the order flattered the curve"
        )
    return (
        f"monte carlo: {OK} realised trade drawdown {actual_drawdown:.1%} is typical; worst 5% "
        f"of {mc.n_paths} reshuffles reach {p5:.1%}"
    )


def permutation_verdict(p_value: float, percentile: float, n_valid: int) -> str:
    if n_valid == 0 or not math.isfinite(p_value):
        return f"permutation: {WARN} null undefined (no finite null Sharpe)"
    if p_value < PERMUTATION_ALPHA:
        return (
            f"permutation: {OK} beats the full-pipeline permutation null "
            f"(p={p_value:.3f}, {ordinal(round(percentile))} percentile of {n_valid})"
        )
    return (
        f"permutation: {WARN} indistinguishable from permuted prices "
        f"(p={p_value:.3f}, {ordinal(round(percentile))} percentile of {n_valid})"
    )


# --------------------------------------------------------------------------- helpers


def trade_equity_returns(result: BacktestResult, position_size_pct: float) -> np.ndarray:
    """Per-trade return on *equity*: ``return_pct`` scaled by the share of equity committed."""
    scale = float(position_size_pct) / 100.0
    return np.array([t.return_pct * scale for t in result.trades], dtype=float)


def _sequence_drawdown(trade_returns: np.ndarray) -> float:
    if trade_returns.size == 0:
        return 0.0
    equity = np.concatenate([[1.0], np.cumprod(1.0 + trade_returns)])
    peak = np.maximum.accumulate(equity)
    return float((equity / peak - 1.0).min())


def permute_history(history: pd.DataFrame, window: Window, *, seed: int) -> pd.DataFrame:
    """``history`` with only the window's bars permuted (Masters' anchored permutation).

    ``k0`` = number of rows before ``window.start``. Rows ``[0, k0 - 1)`` are returned
    unchanged; ``permute_candles`` runs on rows ``[k0 - 1, end)``, which keeps row ``k0 - 1``
    (the last warm-up bar) as its anchor and shuffles the window bars' shapes. The window's
    total log return ``ln(close[-1] / close[k0 - 1])`` is therefore preserved (up to float
    rounding) and the warm-up the signals see is the real one. With no warm-up (``k0 == 0``)
    the whole frame is permuted with bar 0 as the anchor.
    """
    k0 = int((history.index < window.start).sum())
    cut = max(k0 - 1, 0)
    return pd.concat([history.iloc[:cut], permute_candles(history.iloc[cut:], seed=seed)])


def _window_signals(
    signal_fn: SignalFn, history: pd.DataFrame, candles: pd.DataFrame
) -> tuple[pd.Series, pd.Series]:
    entries, exits = signal_fn(history)
    return (
        entries.loc[candles.index].astype(bool),
        exits.loc[candles.index].astype(bool),
    )


# --------------------------------------------------------------------------- orchestration


def adversarial_checks_signals(
    signal_fn: SignalFn,
    history: pd.DataFrame,
    window: Window,
    *,
    cfg: LabConfig,
    interval: str,
    risk: RiskSpec,
    seed: int,
) -> AdversarialReport:
    """Every adversarial check for an arbitrary causal ``signal_fn(candles) -> (entries,
    exits)``. ``history`` may hold warm-up bars before ``window``; nothing at or after
    ``window.end`` is used, and nothing in the locked holdout is accepted."""
    hist = history_through(history, window)
    assert_no_holdout(hist, cfg.splits)
    candles = slice_window(hist, window)
    if len(candles) < 2:
        raise ValueError(f"window {window.start}..{window.end} has {len(candles)} bars; need >= 2")
    costs, init_cash = cfg.costs, cfg.init_cash
    entries, exits = _window_signals(signal_fn, hist, candles)

    base, delays, delay_text = delay_check(
        candles,
        entries,
        exits,
        interval=interval,
        costs=costs,
        risk=risk,
        delays=cfg.stats.adversarial_delays,
        init_cash=init_cash,
        cost_history=hist,
    )
    flipped = run_variant(
        candles,
        entries,
        exits,
        costs=costs,
        check="sign_flip",
        label="sign flip",
        params={"variant": "sign_flip", "direction": "short"},
        direction="short",
        interval=interval,
        risk=risk,
        init_cash=init_cash,
        cost_history=hist,
    )
    stressed = tuple(
        run_variant(
            candles,
            entries,
            exits,
            costs=costs.scaled(f),
            check="cost_stress",
            label=f"cost x{f:g}",
            params={"variant": "cost_stress", "factor": float(f)},
            interval=interval,
            risk=risk,
            init_cash=init_cash,
            cost_history=hist,
        )
        for f in cfg.stress_factors
    )

    if base.result is not None:
        trade_r = trade_equity_returns(base.result, risk.position_size_pct)
    else:
        trade_r = np.empty(0)
    mc = trade_reshuffle(trade_r, n_paths=cfg.stats.monte_carlo_paths, seed=seed)
    actual_dd = _sequence_drawdown(trade_r)

    null = np.full(cfg.stats.permutation_paths, np.nan)
    for k in range(cfg.stats.permutation_paths):
        perm = permute_history(hist, window, seed=seed + 1 + k)
        p_candles = slice_window(perm, window)
        p_entries, p_exits = _window_signals(signal_fn, perm, p_candles)
        v = run_variant(
            p_candles,
            p_entries,
            p_exits,
            costs=costs,
            check="permutation",
            label=f"perm {k}",
            interval=interval,
            risk=risk,
            init_cash=init_cash,
            cost_history=perm,
        )
        null[k] = v.sharpe
    n_valid = int(np.isfinite(null).sum())
    if math.isfinite(base.sharpe) and n_valid:
        p_value = float(permutation_p_value(base.sharpe, null))
        pct = float(percentile_rank(base.sharpe, null))
    else:
        p_value, pct = math.nan, math.nan

    verdicts = {
        "delay": delay_text,
        "sign_flip": sign_flip_verdict(base, flipped),
        "cost_stress": cost_stress_verdict(
            base.sharpe,
            {float(f): v.sharpe for f, v in zip(cfg.stress_factors, stressed, strict=True)},
        ),
        "monte_carlo": monte_carlo_verdict(mc, actual_dd, int(trade_r.size)),
        "permutation": permutation_verdict(p_value, pct, n_valid),
    }
    return AdversarialReport(
        window=window,
        base=base,
        delays=delays,
        sign_flip=flipped,
        cost_stress=stressed,
        monte_carlo=mc,
        monte_carlo_actual_drawdown=actual_dd,
        permutation_sharpes=null,
        permutation_p_value=p_value,
        permutation_percentile=pct,
        verdicts=verdicts,
        seed=int(seed),
    )


def plan_signal_fn(plan: Plan) -> SignalFn:
    """``candles -> (entries, exits)`` compiled from ``plan`` (registry features are causal)."""

    def fn(candles: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        sig = compile_plan(plan, candles)
        return sig.entries, sig.exits

    return fn


def adversarial_checks(
    plan: Plan, history: pd.DataFrame, window: Window, *, cfg: LabConfig, seed: int
) -> AdversarialReport:
    """Every adversarial check for a validated ``plan`` (see the module docstring)."""
    return adversarial_checks_signals(
        plan_signal_fn(plan),
        history,
        window,
        cfg=cfg,
        interval=plan.interval,
        risk=plan.risk,
        seed=seed,
    )
