"""The measured block of a risk overlay (PLAN.md Phase 15, the ``"risk_overlay"`` question).

An overlay plan (``{"kind": "overlay", ...}``) holds one symbol (exposure 1) and goes flat for
the next bar (exposure 0) whenever its ``flat_when`` rule is true at a bar's close. This module
measures it against buy-and-hold of the same symbol and against buy-and-hold scaled to the
overlay's own average exposure. It only measures: pass/fail, the Bonferroni divisor and every
threshold are applied at ``get_report`` time by :mod:`lab.research.overlay_criteria`.

Scored window. The overlay and buy-and-hold are scored on the SAME bars: from the first bar on
which the overlay could decide to be invested (its decision bar: the first bar of the plan's
scored design window, :func:`lab.runner.plan_design_window`, at or after the compiled plan's
warm-up; for a derivatives overlay that window already starts at the lab's one shared
derivatives scored start) to the design-window end. Both decide at that bar's close and fill at
the next bar's open, so bar 0 of the window is flat for both. Features (the expanding ranks
``vol_rank`` and ``premium_mean_rank`` above all) are computed ONCE on the full design history
and then sliced to the scored window, never recomputed from a window start: an expanding rank
depends on where its frame starts. The holdout view (:func:`overlay_holdout_view`) does the same
on design + holdout history and scores holdout bars only.

What :func:`overlay_block` measures, at base costs (``cfg.costs``) and at every multiple in
:func:`cost_multiples` (``OVERLAY_COST_MULTIPLES`` plus the frozen
``break_even_cost_multiple_min``), with the plan's fixed (published) parameters:

* ``avg_exposure``: the share of bars after the decision bar on which the overlay holds the
  coin at the close (``position > 0``);
* CDaR (:func:`lab.stats.drawdown.cdar`, Chekhlov-Uryasev-Zabarankin, bar level, at the frozen
  ``cdar_confidence``) of the overlay, buy-and-hold and the exposure-matched benchmark
  (:func:`lab.stats.drawdown.exposure_matched_returns`: ``e * r_buy_hold`` rebalanced every bar
  at zero cost, ``e`` = ``avg_exposure``). Zero cost and perfect rebalancing favour the
  benchmark; its drawdown is about ``1 - (1 - D)^e``, a little deeper than ``e * D`` but much
  shallower than ``D``, so beating it is the stricter comparison;
* max drawdowns, annualised Sharpe (365-day bars), and buy-and-hold's drawdown episodes (peak
  to recovery) deeper than ``episode_depth_min``. For each such episode the window is
  ``[peak, recovery]`` (to the window end if unrecovered), and ``overlay_max_dd`` /
  ``matched_max_dd`` are the max drawdowns of the overlay and of the exposure-matched series
  INSIDE that window, each measured from its own equity at the episode's peak bar (the returns
  after the peak bar through the recovery bar, the running peak starting at that equity);
  ``improved`` = ``overlay_max_dd < matched_max_dd``. Every by-cost row carries
  ``episodes_qualifying`` / ``episodes_improved`` for its own series;
* paired stationary-bootstrap tests (:mod:`lab.stats.paired`, one index matrix shared by both
  series, ``n_samples`` = lab.toml ``[stats] bootstrap_samples``, mean block =
  :func:`lab.stats.paired.bootstrap_block_bars` of the frozen ``bootstrap_mean_block_days``,
  explicit ``seed``): CDaR(overlay) - CDaR(matched) with its one-sided p (H1: lower), and
  Sharpe(overlay) - Sharpe(buy-and-hold) with its one-sided lower bound at level ``1 - alpha``
  and the non-inferiority p ``(1 + #{d* <= -sharpe_non_inferiority}) / (1 + n_valid)``
  (H1: the gap is above minus the margin). Each test also reports its bootstrap
  ``bias = mean(d*) - point`` (``cdar_bias``, ``sharpe_bias``), information only;
* ``wf_fixed``: the same core numbers on the stitched walk-forward TEST folds of the scored
  window (the lab's fold splitter, ``lab.engine.splits.walk_forward_folds``; nothing is tuned,
  the parameters stay fixed; every fold starts flat and decides on its first bar, like the
  benchmark). This is a SUB-WINDOW ROBUSTNESS CHECK on the same design bars, not an
  out-of-sample test: nothing was selected on the train windows, and the parameters are the
  published ones the design window was already scored with.

Only the base trial ``run_backtest`` recorded is a ledger trial. The cost-multiple reruns and the
fold reruns are diagnostics of that same trial (same plan, same bars), not new trials, so they
are not ledgered; the 1x rerun is checked to equal the ledgered trial's returns bar for bar.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from lab.config import LabConfig
from lab.data.derivatives_columns import plan_derivative_columns
from lab.engine import run_signals
from lab.engine.splits import design_view, slice_window, walk_forward_folds
from lab.evaluate.benchmarks import buy_and_hold
from lab.ledger import Ledger
from lab.plan import compile_plan, parse_plan
from lab.report import num, seconds
from lab.research.contract import OverlayCriteria
from lab.runner import DEFAULT_SEED, CandleLoader, derivatives_series, plan_design_window
from lab.stats.drawdown import (
    DrawdownEpisode,
    cdar,
    count_drawdown_episodes,
    drawdown_episodes,
    drawdown_series,
    exposure_matched_returns,
)
from lab.stats.metrics import sharpe_per_bar
from lab.stats.paired import (
    PairedBootstrap,
    bootstrap_block_bars,
    paired_stationary_bootstrap,
)
from lab.types import BacktestResult, CompiledSignals, CostModel, Plan, Window, bars_per_year

__all__ = [
    "OVERLAY_BLOCK_VERSION",
    "OVERLAY_COST_MULTIPLES",
    "OverlayPair",
    "cost_multiples",
    "episode_comparison",
    "overlay_block",
    "overlay_holdout_view",
]

OVERLAY_BLOCK_VERSION = 1
# Cost multiples the drawdown and Sharpe numbers are re-measured at (the break-even walk stops
# at the first multiple where a row no longer holds). 4x matches the edge question's harshest
# stress; the frozen break_even_cost_multiple_min is added when it is not on this grid.
OVERLAY_COST_MULTIPLES: tuple[float, ...] = (1.0, 1.5, 2.0, 3.0, 4.0)
# Absolute tolerance of the "1x rerun equals the ledgered trial" check (same engine, same bars,
# same inputs: the difference is exactly 0 in practice; this only absorbs float noise).
_LEDGER_MATCH_ATOL = 1e-12

NOTES: tuple[str, ...] = (
    "Overlay and buy-and-hold are scored on the same bars: from the overlay's first decision "
    "bar (after its warm-up; a derivatives overlay from the lab's shared derivatives scored "
    "start) to the design-window end. Both are flat on that first bar and fill at the next "
    "bar's open.",
    "Exposure-matched buy-and-hold = avg_exposure x buy-and-hold's per-bar return, rebalanced "
    "every bar at zero cost. Zero cost and perfect rebalancing favour this benchmark; its "
    "drawdown is about 1 - (1 - D)^e (slightly deeper than e x D), so beating it is the "
    "stricter drawdown comparison. Plain buy-and-hold alone proves nothing: holding less always "
    "cuts drawdown.",
    "Features are computed on the full available history and then sliced to the scored window "
    "(an expanding rank depends on where its frame starts).",
    "Cost-multiple and walk-forward fold reruns are diagnostics of the one ledgered trial (same "
    "plan, same bars), not new trials; the 1x rerun equals the ledgered trial's returns.",
    "Bootstrap p-values and bounds are raw here; the Bonferroni divisor N_overlay and every "
    "threshold are applied by get_report. bias = mean(d*) - point of each bootstrap is "
    "information only.",
    "Episodes: for each buy-and-hold drawdown deeper than depth_min, overlay_max_dd and "
    "matched_max_dd are the overlay's and exposure-matched buy-and-hold's max drawdowns inside "
    "[peak, recovery] (to the window end if unrecovered), each measured from its own equity at "
    "the episode peak; improved = overlay_max_dd < matched_max_dd.",
    "wf_fixed is a sub-window robustness check on stitched fixed-parameter test folds (same "
    "design bars, not out-of-sample).",
)


def cost_multiples(criteria: OverlayCriteria) -> tuple[float, ...]:
    """:data:`OVERLAY_COST_MULTIPLES` plus ``criteria.break_even_cost_multiple_min``, sorted."""
    grid = {float(m) for m in OVERLAY_COST_MULTIPLES}
    grid.add(float(criteria.break_even_cost_multiple_min))
    return tuple(sorted(grid))


@dataclass(frozen=True)
class OverlayPair:
    """The overlay and buy-and-hold run on one window with one cost model."""

    window: Window
    index: pd.DatetimeIndex
    overlay: BacktestResult
    buy_hold: BacktestResult

    @property
    def overlay_returns(self) -> np.ndarray:
        return self.overlay.returns.to_numpy(dtype=float)

    @property
    def buy_hold_returns(self) -> np.ndarray:
        return self.buy_hold.returns.to_numpy(dtype=float)

    @property
    def invested(self) -> np.ndarray:
        """1.0 where the overlay holds the coin at the bar's close, 0.0 otherwise."""
        return (self.overlay.position.to_numpy(dtype=float) > 0.0).astype(float)


def _run_pair(
    plan: Plan,
    sig: CompiledSignals,
    history: pd.DataFrame,
    window: Window,
    costs: CostModel,
    cfg: LabConfig,
) -> OverlayPair:
    """The overlay (signals compiled on ``history``) and buy-and-hold on ``window``'s bars of
    ``history``, with ``costs``; the vol-scaled slippage warms up on every earlier bar."""
    candles = slice_window(history, window)
    if len(candles) < 2:
        raise ValueError(
            f"fewer than 2 {plan.symbol} {plan.interval} bars in [{window.start}, {window.end})"
        )
    cost_history = history.loc[history.index < window.end]
    res = run_signals(
        candles,
        sig.entries.loc[candles.index],
        sig.exits.loc[candles.index],
        costs=costs,
        risk=plan.risk,
        init_cash=cfg.init_cash,
        cost_history=cost_history,
    )
    bh = buy_and_hold(candles, costs=costs, init_cash=cfg.init_cash, cost_history=cost_history)
    return OverlayPair(window=window, index=candles.index, overlay=res, buy_hold=bh)


def _decision_window(history: pd.DataFrame, window: Window, warmup_bars: int) -> Window:
    """``window`` moved forward to the overlay's first decision bar: the first bar of ``window``
    at or after the compiled plan's warm-up (signals are forced False before it)."""
    pos = max(int(history.index.searchsorted(window.start)), int(warmup_bars))
    last = int(history.index.searchsorted(window.end)) - 1
    if pos >= last:
        raise ValueError(
            f"the overlay's warm-up ({warmup_bars} bars) leaves fewer than 2 bars in the scored "
            f"window [{window.start}, {window.end})"
        )
    return Window(start=history.index[pos], end=window.end, kind=window.kind)


# --------------------------------------------------------------------------- statistics


@dataclass(frozen=True)
class _Boot:
    n_samples: int
    mean_block: int
    seed: int
    level: float  # one-sided bound level, 1 - alpha


def _paired_with_diffs(
    run: Callable[[Callable[[np.ndarray], float]], PairedBootstrap],
    stat: Callable[[np.ndarray], float],
    n_samples: int,
) -> tuple[PairedBootstrap, np.ndarray]:
    """Run one :mod:`lab.stats.paired` test and also return its finite resampled differences
    ``d*``. ``run`` calls the test with the statistic it is given; a recording wrapper keeps
    every value, in call order (``stat(a[i])`` then ``stat(b[i])`` per resample, then the two
    originals), and the recovered ``d*`` are checked against the test's own ``p_value`` and
    ``n_valid`` so a change in that order fails loudly instead of silently."""
    values: list[float] = []

    def recording(x: np.ndarray) -> float:
        v = float(stat(x))
        values.append(v)
        return v

    res = run(recording)
    if len(values) != 2 * n_samples + 2:
        raise RuntimeError(
            f"paired bootstrap called its statistic {len(values)} times, expected "
            f"{2 * n_samples + 2}"
        )
    vals = np.asarray(values[: 2 * n_samples], dtype=float)
    diffs = vals[0::2] - vals[1::2]
    valid = diffs[np.isfinite(diffs)]
    p = (1.0 + float(np.count_nonzero(valid >= 0.0))) / (1.0 + valid.size)
    if valid.size != res.n_valid or p != res.p_value:
        raise RuntimeError("recovered bootstrap differences do not reproduce the paired test")
    return res, valid


def _core(
    r_ov: np.ndarray,
    r_bh: np.ndarray,
    invested: np.ndarray,
    interval: str,
    criteria: OverlayCriteria,
) -> dict[str, Any]:
    """Point estimates on one aligned pair of return series (no bootstrap)."""
    a = float(criteria.cdar_confidence)
    exposure = float(np.mean(invested)) if invested.size else math.nan
    matched = exposure_matched_returns(r_bh, min(1.0, max(0.0, exposure)))
    c_ov, c_bh, c_m = cdar(r_ov, a), cdar(r_bh, a), cdar(matched, a)
    scale = math.sqrt(bars_per_year(interval))
    return {
        "avg_exposure": num(exposure),
        "cdar": {"overlay": num(c_ov), "buy_hold": num(c_bh), "exposure_matched": num(c_m)},
        "cdar_ratio_vs_buy_hold": num(c_ov / c_bh) if c_bh > 0.0 else None,
        "max_drawdown": {
            "overlay": num(float(drawdown_series(r_ov).max(initial=0.0))),
            "buy_hold": num(float(drawdown_series(r_bh).max(initial=0.0))),
            "exposure_matched": num(float(drawdown_series(matched).max(initial=0.0))),
        },
        "sharpe": {
            "overlay": num(sharpe_per_bar(r_ov) * scale),
            "buy_hold": num(sharpe_per_bar(r_bh) * scale),
        },
    }


def _tests(
    r_ov: np.ndarray,
    r_bh: np.ndarray,
    invested: np.ndarray,
    interval: str,
    criteria: OverlayCriteria,
    boot: _Boot,
) -> dict[str, Any]:
    """The paired-bootstrap numbers of one cost multiple, raw (thresholds and the Bonferroni
    divisor are applied at report time): CDaR(overlay) - CDaR(exposure-matched) with its
    one-sided p, and Sharpe(overlay) - Sharpe(buy-and-hold) with its one-sided lower bound at
    ``boot.level`` and its non-inferiority p against the frozen margin."""
    exposure = float(np.mean(invested)) if invested.size else 0.0
    matched = exposure_matched_returns(r_bh, min(1.0, max(0.0, exposure)))
    a = float(criteria.cdar_confidence)

    def cdar_stat(x: np.ndarray) -> float:  # == lab.stats.paired.cdar_difference_test's
        return cdar(x, a)

    cd, cd_diffs = _paired_with_diffs(
        lambda stat: paired_stationary_bootstrap(
            r_ov,
            matched,
            stat,
            n_samples=boot.n_samples,
            mean_block=boot.mean_block,
            seed=boot.seed,
            level=boot.level,
        ),
        cdar_stat,
        boot.n_samples,
    )
    scale = math.sqrt(bars_per_year(interval))

    def annual_sharpe(x: np.ndarray) -> float:  # == lab.stats.paired.sharpe_difference_test's
        return sharpe_per_bar(x) * scale

    sh, diffs = _paired_with_diffs(
        lambda stat: paired_stationary_bootstrap(
            r_ov,
            r_bh,
            stat,
            n_samples=boot.n_samples,
            mean_block=boot.mean_block,
            seed=boot.seed,
            level=boot.level,
        ),
        annual_sharpe,
        boot.n_samples,
    )
    margin = float(criteria.sharpe_non_inferiority)
    # H0: Sharpe gap <= -margin; small p = the gap is above -margin (non-inferior)
    ni_p = (1.0 + float(np.count_nonzero(diffs <= -margin))) / (1.0 + diffs.size)
    episodes = episode_comparison(r_ov, r_bh, matched, float(criteria.episode_depth_min))
    return {
        "cdar_point": num(cd.point),
        "cdar_p_value": num(cd.p_value),
        "cdar_n_valid": int(cd.n_valid),
        "cdar_bias": _bias(cd_diffs, cd.point),
        "sharpe_point": num(sh.point),
        "sharpe_lower": num(sh.lower),
        "sharpe_ni_p_value": num(ni_p),
        "sharpe_n_valid": int(sh.n_valid),
        "sharpe_bias": _bias(diffs, sh.point),
        "episodes_qualifying": len(episodes),
        "episodes_improved": sum(1 for _, ov, m in episodes if ov < m),
    }


def _bias(diffs: np.ndarray, point: float) -> float | None:
    """Bootstrap bias ``mean(d*) - point`` (None without finite resamples or point)."""
    if diffs.size == 0 or not math.isfinite(float(point)):
        return None
    return num(float(np.mean(diffs)) - float(point))


def episode_comparison(
    r_ov: np.ndarray, r_bh: np.ndarray, matched: np.ndarray, depth: float
) -> list[tuple[DrawdownEpisode, float, float]]:
    """``(episode, overlay_max_dd, matched_max_dd)`` for every buy-and-hold drawdown episode
    of ``r_bh`` strictly deeper than ``depth``, in time order. The episode's window is
    ``[peak, recovery]`` (to the end of the series if unrecovered); each max drawdown is
    measured from the series' own equity at the peak bar: the returns of bars ``peak + 1``
    through ``recovery`` (the initial equity when ``peak = -1``), the running peak starting at
    that equity. ``improved`` is ``overlay_max_dd < matched_max_dd`` (strict)."""
    if not (r_ov.size == r_bh.size == matched.size):
        raise ValueError("episode comparison needs aligned overlay, buy-and-hold, matched series")
    n = r_bh.size
    out: list[tuple[DrawdownEpisode, float, float]] = []
    for e in drawdown_episodes(r_bh):
        if not e.depth > depth:
            continue
        lo = e.peak + 1
        hi = e.recovery + 1 if e.recovery is not None else n
        ov = float(drawdown_series(r_ov[lo:hi]).max(initial=0.0))
        m = float(drawdown_series(matched[lo:hi]).max(initial=0.0))
        out.append((e, ov, m))
    return out


def _by_cost_row(
    multiple: float,
    r_ov: np.ndarray,
    r_bh: np.ndarray,
    invested: np.ndarray,
    interval: str,
    criteria: OverlayCriteria,
    boot: _Boot,
) -> dict[str, Any]:
    core = _core(r_ov, r_bh, invested, interval, criteria)
    return {
        "multiple": float(multiple),
        "cdar_ratio": core["cdar_ratio_vs_buy_hold"],
        "avg_exposure": core["avg_exposure"],
        **_tests(r_ov, r_bh, invested, interval, criteria, boot),
    }


def _window_json(window: Window, bars: int) -> dict[str, Any]:
    return {"start": seconds(window.start), "end": seconds(window.end), "bars": int(bars)}


def _episodes(
    r_bh: np.ndarray,
    r_ov: np.ndarray,
    invested: np.ndarray,
    index: pd.DatetimeIndex,
    depth: float,
) -> dict[str, Any]:
    """Buy-and-hold's drawdown episodes (peak to recovery) deeper than ``depth``, with bar open
    times in UNIX seconds (``recovery`` None = not recovered by the window end), each with the
    overlay's and the exposure-matched series' max drawdown inside it
    (:func:`episode_comparison`)."""
    exposure = float(np.mean(invested)) if invested.size else 0.0
    matched = exposure_matched_returns(r_bh, min(1.0, max(0.0, exposure)))
    deep = episode_comparison(r_ov, r_bh, matched, depth)
    return {
        "depth_min": float(depth),
        "buy_hold_count": int(count_drawdown_episodes(r_bh, depth)),
        "overlay_count": int(count_drawdown_episodes(r_ov, depth)),
        "improved_count": sum(1 for _, ov, m in deep if ov < m),
        "buy_hold": [
            {
                "peak": seconds(index[max(0, e.peak)]),
                "trough": seconds(index[e.trough]),
                "recovery": seconds(index[e.recovery]) if e.recovery is not None else None,
                "depth": num(e.depth),
                "overlay_max_dd": num(ov),
                "matched_max_dd": num(m),
                "improved": bool(ov < m),
            }
            for e, ov, m in deep
        ],
    }


def _boot(cfg: LabConfig, criteria: OverlayCriteria, interval: str, seed: int) -> _Boot:
    level = 1.0 - float(criteria.alpha)
    if not 0.5 < level < 1.0:
        raise ValueError(
            f"overlay criteria alpha = {criteria.alpha!r} gives a one-sided bound level "
            f"{level:g}; it must be strictly between 0.5 and 1"
        )
    return _Boot(
        n_samples=int(cfg.stats.bootstrap_samples),
        mean_block=bootstrap_block_bars(float(criteria.bootstrap_mean_block_days), interval),
        seed=int(seed),
        level=level,
    )


def _check_ledgered(ledger: Ledger, trial_id: int, r_ov: np.ndarray) -> None:
    """The 1x rerun must equal the ledgered base trial: its returns on the decision window are
    the trial's last bars, and every earlier bar of the trial (warm-up) is flat."""
    rec = ledger.get(int(trial_id), include_returns=True)
    stored = rec.returns
    if stored is None or stored.size < r_ov.size:
        raise ValueError(f"ledgered trial {trial_id} has no returns covering the overlay window")
    head, tail = stored[: stored.size - r_ov.size], stored[stored.size - r_ov.size :]
    if not (np.allclose(tail, r_ov, rtol=0.0, atol=_LEDGER_MATCH_ATOL) and not np.any(head)):
        raise ValueError(
            f"the overlay's 1x rerun does not reproduce ledgered trial {trial_id}; the overlay "
            "block was not recorded"
        )


def _measure(
    pairs: Mapping[float, OverlayPair] | Mapping[float, tuple[np.ndarray, np.ndarray, np.ndarray]],
    interval: str,
    criteria: OverlayCriteria,
    boot: _Boot,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Core numbers at 1x plus the by-cost rows, from ``multiple -> (overlay returns,
    buy-and-hold returns, invested flags)`` (the invested flags exclude each decision bar)."""
    rows: list[dict[str, Any]] = []
    core: dict[str, Any] = {}
    for m in sorted(pairs):
        r_ov, r_bh, inv = _series(pairs[m])
        if m == 1.0:
            core = _core(r_ov, r_bh, inv, interval, criteria)
        rows.append(_by_cost_row(m, r_ov, r_bh, inv, interval, criteria, boot))
    return core, rows


def _series(
    pair: OverlayPair | tuple[np.ndarray, np.ndarray, np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if isinstance(pair, OverlayPair):
        return pair.overlay_returns, pair.buy_hold_returns, pair.invested[1:]
    return pair


def _wf_fixed(
    plan: Plan,
    sig: CompiledSignals,
    history: pd.DataFrame,
    scored: Window,
    cfg: LabConfig,
    criteria: OverlayCriteria,
    boot: _Boot,
    multiples: tuple[float, ...],
) -> dict[str, Any]:
    """The same core numbers on the stitched fixed-parameter walk-forward test folds of the
    scored window (nothing tuned: the train windows are unused). ``{"error": ...}`` when no
    fold fits."""
    s = cfg.splits
    try:
        folds = walk_forward_folds(
            scored,
            train_months=s.wf_train_months,
            test_months=s.wf_test_months,
            step_months=s.wf_step_months,
            anchored=s.wf_anchored,
        )
    except ValueError as exc:
        return {"error": f"no walk-forward test fold: {exc}"}
    stitched: dict[float, tuple[list[np.ndarray], list[np.ndarray], list[np.ndarray]]] = {
        m: ([], [], []) for m in multiples
    }
    used: list[dict[str, Any]] = []
    bars = 0
    for fold in folds:
        if len(slice_window(history, fold.test)) < 2:
            continue
        for m in multiples:
            pair = _run_pair(plan, sig, history, fold.test, cfg.costs.scaled(m), cfg)
            ov, bh, inv = stitched[m]
            ov.append(pair.overlay_returns)
            bh.append(pair.buy_hold_returns)
            inv.append(pair.invested[1:])  # each fold's first bar is its decision bar
        n = len(slice_window(history, fold.test))
        bars += n
        used.append({"start": seconds(fold.test.start), "end": seconds(fold.test.end)})
    if not used:
        return {"error": "every walk-forward test fold has fewer than 2 bars"}
    series = {
        m: (np.concatenate(ov), np.concatenate(bh), np.concatenate(inv))
        for m, (ov, bh, inv) in stitched.items()
    }
    core, rows = _measure(series, plan.interval, criteria, boot)
    r_ov, r_bh, inv = series[1.0]
    exposure = float(np.mean(inv)) if inv.size else 0.0
    matched = exposure_matched_returns(r_bh, min(1.0, max(0.0, exposure)))
    deep = episode_comparison(r_ov, r_bh, matched, float(criteria.episode_depth_min))
    return {
        "bars": int(bars),
        "folds": used,
        **core,
        "episodes": {
            "depth_min": float(criteria.episode_depth_min),
            "buy_hold_count": int(count_drawdown_episodes(r_bh, criteria.episode_depth_min)),
            "overlay_count": int(count_drawdown_episodes(r_ov, criteria.episode_depth_min)),
            "improved_count": sum(1 for _, ov, m in deep if ov < m),
        },
        "by_cost": rows,
        "note": (
            "sub-window robustness check on stitched fixed-parameter test folds (same design "
            "bars, not out-of-sample): the plan's published parameters, nothing tuned or "
            "selected on the train windows; each fold starts flat and decides on its first "
            "bar, like buy-and-hold. Episodes are counted on the stitched fold returns"
        ),
    }


def overlay_block(
    plan: Plan | Mapping[str, Any] | str,
    *,
    cfg: LabConfig,
    ledger: Ledger,
    run_id: str,
    loader: CandleLoader,
    criteria: OverlayCriteria,
    seed: int = DEFAULT_SEED,
    trial_id: int | None = None,
) -> dict[str, Any]:
    """The backtest evidence's ``"overlay"`` block for an overlay plan on the design window
    (module docstring). ``trial_id`` is the base trial ``run_backtest`` just recorded: when
    given, the 1x rerun must reproduce its returns (``ValueError`` otherwise). Raw numbers only;
    :func:`lab.research.overlay_criteria.evaluate_overlay_plan` judges them. Only design-window
    bars are loaded."""
    p = plan if isinstance(plan, Plan) else parse_plan(plan)
    if p.kind != "overlay":
        raise ValueError(f"plan {p.name!r} is not an overlay plan (kind {p.kind!r})")
    history = design_view(loader(p.symbol, p.interval, cfg.splits.holdout_start), cfg.splits)
    series = derivatives_series(cfg, ledger, run_id) if plan_derivative_columns(p) else None
    window, _ = plan_design_window(p, history, cfg, series=series)
    history = history.loc[history.index < window.end]
    sig = compile_plan(p, history)  # once, on the full design history
    scored = _decision_window(history, window, sig.warmup_bars)
    multiples = cost_multiples(criteria)
    boot = _boot(cfg, criteria, p.interval, seed)
    pairs = {m: _run_pair(p, sig, history, scored, cfg.costs.scaled(m), cfg) for m in multiples}
    base = pairs[1.0]
    if trial_id is not None:
        _check_ledgered(ledger, trial_id, base.overlay_returns)
    core, rows = _measure(pairs, p.interval, criteria, boot)
    return {
        "version": OVERLAY_BLOCK_VERSION,
        "window": _window_json(scored, len(base.index)),
        "interval": p.interval,
        "warmup_bars": int(sig.warmup_bars),
        "cdar_confidence": float(criteria.cdar_confidence),
        "bootstrap": {
            "n_samples": boot.n_samples,
            "mean_block": boot.mean_block,
            "mean_block_days": float(criteria.bootstrap_mean_block_days),
            "seed": boot.seed,
            "level": boot.level,
        },
        **core,
        "episodes": _episodes(
            base.buy_hold_returns,
            base.overlay_returns,
            base.invested[1:],
            base.index,
            float(criteria.episode_depth_min),
        ),
        "by_cost": rows,
        "wf_fixed": _wf_fixed(p, sig, history, scored, cfg, criteria, boot, multiples),
        "notes": list(NOTES),
    }


def overlay_holdout_view(
    plan: Plan,
    candles: pd.DataFrame,
    window: Window,
    cfg: LabConfig,
    criteria: OverlayCriteria,
) -> tuple[OverlayPair, dict[str, Any]]:
    """Point estimates on the locked holdout at base costs: features compiled on ``candles``
    (design + holdout history, so every indicator and expanding rank is warmed up on the full
    history), only ``window``'s bars scored. Returns the pair (for the ledger's holdout trial)
    and ``{avg_exposure, cdar_ratio_vs_buy_hold, cdar, sharpe, max_drawdown}``; the caller turns
    them into pass/fail and never returns them."""
    history = candles.loc[candles.index < window.end]
    sig = compile_plan(plan, history)
    scored = _decision_window(history, window, sig.warmup_bars)
    pair = _run_pair(plan, sig, history, scored, cfg.costs, cfg)
    core = _core(
        pair.overlay_returns, pair.buy_hold_returns, pair.invested[1:], plan.interval, criteria
    )
    return pair, core
