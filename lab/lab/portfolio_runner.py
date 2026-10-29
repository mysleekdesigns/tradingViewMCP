"""Portfolio (multi-coin) research pipeline: universe data, ledgered backtests, sweeps,
walk-forward, adversarial checks, cross-check and the holdout check (PLAN.md Phase 10).

Single-symbol plans never come here: callers route on
:func:`lab.plan.portfolio.is_portfolio_plan`. Everything single-symbol stays in
:mod:`lab.runner` / :mod:`lab.search.optuna_search` unchanged.

Data (design-window tools never hold a holdout row):

* the universe table (:func:`lab.data.universe_build.load_universe_ranks`) is read with
  ``time < end`` (``end = holdout_start`` for every design-window tool); members at row t are the
  point-in-time top ``[universe] top_n`` by trailing dollar volume among eligible coins;
* candles of every symbol that is ever a member before ``end`` come from the same loader as
  single-symbol research (``loader(symbol, interval, end)``), so the holdout is never loaded;
* the universe manifest's sha256 is frozen into the research run at ``start_run``
  (:func:`lab.research.runs.check_universe_manifest`); every portfolio tool refuses once it
  changed, and when the manifest's build settings differ from lab.toml ``[universe]``.

Windows: the portfolio design window is ``[max(design_start, first row with any member),
min(holdout_start, last bar end, last universe rank row + 1 bar))``, the same for every
portfolio plan (so their ledger returns
are commensurable for DSR's V[SR], PBO matrices and behaviour families). Signals are built on
the history through a window's end (:func:`lab.plan.portfolio.build_signals`, causal), decisions
outside the window are dropped, and the engine (:func:`lab.engine.portfolio.run_portfolio`) runs
on every bar through the window end (earlier bars warm up slippage and impact inputs; nothing is
held before the first decision). Report metrics and EVERY benchmark are scored from the
strategy's first decision row in the window (``scored_from``): the first schedule row on which
it COULD have invested (at least ``min_names`` sizable eligible names), whether or not its
dispersion gate was open, so gate-closed cash rows count against it. The ledger rows keep the
whole window's per-row returns (zeros before the first decision) so trials stay commensurable.

Stale universe: members after the rank table's last row would be "no member" (all cash), so a
window never runs past it. The design window is cut at the last rank row (with a warning in the
report); the holdout check refuses before anything is spent (:class:`StaleUniverseError`) and
tells the owner to rebuild the universe.

Rebalance offset: the research pipeline pins ``rebalance.offset`` at 0
(:func:`lab.plan.portfolio.pinned_offset_errors`, applied by :func:`parse_portfolio_plan`).
run_adversarial still runs the other offsets, as a descriptive timing-luck table recorded as
``"adversarial"`` rows (never a selection source) under the submitted plan's hash.

Costs: lab.toml ``[costs]`` fee + volatility-scaled slippage, plus the square-root participation
impact from ``[portfolio]`` (:class:`lab.engine.costs.ImpactModel`). Impact is sized on a FIXED
book of ``impact_aum_usd``, not on current equity. Every portfolio ledger row stores this full
cost model in ``costs_json`` (with an ``"impact"`` key), so DSR's V[SR] never mixes portfolio
and single-symbol cost assumptions.

Ledger policy (same sources as single-symbol): the plan's design-window run -> ``"api"``;
benchmarks -> one ``"benchmark"`` row each; ±pct neighbours -> ``"neighbourhood"``; Optuna
trials -> ``"optuna"`` (infeasible ``exit_quantile < quantile`` combinations are recorded as
``error`` rows and pruned); adversarial variants -> ``"adversarial"``; the holdout -> ``"manual"``
with window kind ``"holdout"``. The ledger symbol is the pseudo-symbol
:func:`universe_symbol` (``universe:<QUOTE>``).

Results are historical and not forward-looking.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

import numpy as np
import optuna
import pandas as pd

from lab.config import LabConfig
from lab.crosscheck.portfolio import compare_equity, replay_portfolio
from lab.data.universe import segment_starts as listing_segment_starts
from lab.data.universe import trailing_dollar_volume
from lab.data.universe_build import load_universe_ranks
from lab.engine import run_weights
from lab.engine.costs import ImpactModel, impact_fraction, impact_inputs, slippage_series
from lab.engine.portfolio import PortfolioResult, net_returns_for, run_portfolio
from lab.engine.splits import slice_window, walk_forward_folds
from lab.evaluate.adversarial import cost_stress_verdict, delay_verdict
from lab.evaluate.alpha_beta import alpha_beta, alpha_beta_json
from lab.evaluate.benchmarks import buy_and_hold, vol_target_weights
from lab.evaluate.portfolio_benchmarks import (
    NullPath,
    annual_turnover,
    equal_weight_universe,
    mean_book_mix,
    mean_path_stat,
    random_selection_path,
    sum_kept_match_fallbacks,
    sum_match_fallbacks,
)
from lab.ledger import SELECTION_SOURCES, Ledger, metrics_to_json
from lab.plan import portfolio as pf
from lab.plan.validator import PlanValidationError
from lab.registry import cross_section as xs
from lab.report import metrics_dict, num, seconds, to_jsonable
from lab.runner import CandleLoader, DataUnavailableError, neighbourhood_overrides, pbo_split_count
from lab.search.optuna_search import (
    FAILED_OBJECTIVE,
    make_tpe_sampler,
    params_json,
    study_meta,
)
from lab.stats import (
    deflated_sharpe,
    metrics_from_returns,
    pbo_cscv,
    sharpe_per_bar,
    stationary_bootstrap_ci,
)
from lab.stats.permutation import percentile_rank
from lab.types import (
    CostModel,
    Metrics,
    ParamSpec,
    PlanError,
    TrialRecord,
    TrialSource,
    Window,
    WindowKind,
    bars_per_year,
    interval_seconds,
)

__all__ = [
    "BENCHMARK_BASE",
    "PortfolioRun",
    "StaleUniverseError",
    "UniverseData",
    "adversarial_cost",
    "backtest_cost",
    "costs_json_for",
    "design_window",
    "family_loader",
    "impact_model",
    "load_universe_data",
    "parse_portfolio_plan",
    "portfolio_adversarial_report",
    "portfolio_backtest_report",
    "portfolio_crosscheck",
    "portfolio_holdout_evaluation",
    "portfolio_param_specs",
    "portfolio_sweep",
    "portfolio_walk_forward",
    "resolve_portfolio_space",
    "run_on_window",
    "score_from",
    "stale_universe_note",
    "universe_symbol",
]

BENCHMARK_BASE = "BTC"  # the market benchmark coin: BTC<quote>
ADVERSARIAL_OFFSETS_MAX = 6  # other rebalance weekdays tried by run_adversarial (timing luck)
REBALANCE_LOG_COLUMNS = ("dispersion", "gate_open", "n_names", "target_exposure", "names")
DISCLAIMER_HEAD = (
    "Historical simulation of a long-only multi-coin portfolio on Binance spot data; not "
    "forward-looking."
)
IMPACT_NOTE = (
    "Participation impact is sized on a fixed book of impact_aum_usd (lab.toml [portfolio]), "
    "not on the simulated equity: trade value = weight change x impact_aum_usd."
)
REBUILD_HINT = "rebuild it with `uv run python -m lab.data.universe_build build`"
CROSSCHECK_SCOPE = (
    "accounting only: the replay reuses the strategy's own target weights and the lab's cost "
    "functions (slippage, impact), so it checks fills, fees, cash and marks; it cannot catch "
    "signal look-ahead (the weights are taken as given) or a bug in the cost model itself"
)
TIMING_LUCK_NOTE = (
    "descriptive only: the same plan rebalanced on other weekdays (rebalance.offset). The lab "
    "pins the offset at 0, so these variants cannot be resubmitted; they are recorded as "
    "'adversarial' ledger rows (never a selection source). A wide spread means the result "
    "depends on which weekday the book trades: timing luck, not skill."
)
DELAY_NOTE = (
    "stale information on the SAME rebalance rows: every input to the decision at schedule row "
    "t (scores, members, dispersion gate, inverse-vol panel, liquidity shares) is the one of row "
    "t - delay. Delays are whole rebalance periods (delay_periods x rebalance.every rows): a few "
    "days of staleness barely changes a multi-week momentum signal, so a rows-based check "
    "would call an honest slow signal 'unchanged'; shifting the finished weights instead would "
    "move the rebalance weekday and measure timing luck."
)
WALK_FORWARD_NOTE = (
    "oos_metrics stitch every fold's test window (params chosen on the train window only), net "
    "of fees, slippage and impact; positions open at a fold boundary are charged their exit "
    "(fee + slippage + impact on the held weights)."
)


# --------------------------------------------------------------------------- config pieces


def universe_symbol(cfg: LabConfig) -> str:
    """Ledger pseudo-symbol of portfolio trials: ``universe:<QUOTE>``."""
    return f"universe:{cfg.universe.quote}"


def benchmark_symbol(cfg: LabConfig) -> str:
    return f"{BENCHMARK_BASE}{cfg.universe.quote}"


def impact_model(cfg: LabConfig, factor: float = 1.0) -> ImpactModel:
    """The ``[portfolio]`` participation impact, scaled by ``factor`` for cost stress (Y and
    the cap scale; ADV / volatility windows and the book size do not)."""
    p = cfg.portfolio
    base = ImpactModel(
        coef=p.impact_coef,
        aum_usd=p.impact_aum_usd,
        adv_days=p.impact_adv_days,
        vol_days=p.impact_vol_days,
        cap=p.impact_cap,
    )
    return base if factor == 1.0 else base.scaled(factor)


def costs_json_for(costs: CostModel, impact: ImpactModel | None) -> str:
    """``costs_json`` of a portfolio trial: the cost model plus the impact model (sorted keys,
    compact), so portfolio Sharpes are only ever compared with each other for V[SR]."""
    data: dict[str, Any] = {**asdict(costs), "impact": asdict(impact) if impact else None}
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _costs_block(cfg: LabConfig, costs: CostModel, impact: ImpactModel | None) -> dict[str, Any]:
    return {
        "fee_rate": costs.fee_rate,
        "slippage_rate": costs.slippage_rate,
        "slippage_vol_mult": costs.slippage_vol_mult,
        "slippage_cap": costs.slippage_cap,
        "impact": asdict(impact) if impact is not None else None,
        "impact_note": IMPACT_NOTE,
        "min_rebalance": cfg.portfolio.min_rebalance,
    }


def _disclaimer(cfg: LabConfig) -> str:
    c = cfg.costs
    p = cfg.portfolio
    return (
        f"{DISCLAIMER_HEAD} Costs: fee {c.fee_rate:.3%} + slippage {c.slippage_rate:.3%} per "
        f"side (vol-widened, capped at {c.slippage_cap:.2%}) + square-root impact (Y "
        f"{p.impact_coef:g}, capped at {p.impact_cap:.0%} per fill). {IMPACT_NOTE} Design window "
        f"only; the locked holdout (from {cfg.splits.holdout_start.date()}) is not shown."
    )


# --------------------------------------------------------------------------- plans


def parse_portfolio_plan(raw: Mapping[str, Any] | str, cfg: LabConfig) -> pf.PortfolioPlan:
    """Validate against the universe interval, then apply the research pipeline's pinned
    rebalance offset (:func:`lab.plan.portfolio.pinned_offset_errors`);
    :class:`PlanValidationError` lists every problem (``path: message``). Every portfolio tool
    (and the holdout check) parses through here."""
    res = pf.validate_portfolio_plan(raw, interval=cfg.universe.interval)
    if res.plan is None:
        errors = []
        for e in res.errors:
            path, _, message = e.partition(": ")
            errors.append(PlanError(path if message else "$", message or e))
        raise PlanValidationError(errors)
    pinned = pf.pinned_offset_errors(res.plan)
    if pinned:
        raise PlanValidationError(pinned)
    return res.plan


def _plan_json(plan: pf.PortfolioPlan) -> str:
    return json.dumps(pf.plan_to_dict(plan), sort_keys=True, separators=(",", ":"))


def portfolio_param_specs(plan: pf.PortfolioPlan) -> dict[str, ParamSpec]:
    """:func:`lab.plan.portfolio.tunable_params` as ``ParamSpec`` (default = current value)."""
    values = pf.tunable_values(plan)
    return {
        k: ParamSpec(
            name=k,
            kind="int" if kind == "int" else "float",
            default=float(values[k]),
            low=float(lo),
            high=float(hi),
            description=k,
        )
        for k, (lo, hi, kind) in pf.tunable_params(plan).items()
    }


def resolve_portfolio_space(
    plan: pf.PortfolioPlan, space: Mapping[str, tuple[float, float]]
) -> dict[str, ParamSpec]:
    """Searched ranges as ``ParamSpec`` (low/high = the range). ``ValueError`` naming the key for
    an unknown key, a range outside the param's bounds, ``low > high`` or non-integer int
    bounds. ``rebalance.offset`` is never tunable (it fits the calendar)."""
    specs = portfolio_param_specs(plan)
    if not isinstance(space, Mapping) or not space:
        raise ValueError("space must be a non-empty mapping of param key -> (low, high)")
    out: dict[str, ParamSpec] = {}
    for key, bounds in space.items():
        spec = specs.get(key)
        if spec is None:
            note = (
                " (rebalance.offset is fixed: tuning the weekday fits the calendar)"
                if (key == "rebalance.offset")
                else ""
            )
            raise ValueError(
                f"space key {key!r} is not a tunable param of this portfolio plan{note}; "
                f"tunable: {', '.join(specs)}"
            )
        low, high = float(bounds[0]), float(bounds[1])
        if not (math.isfinite(low) and math.isfinite(high)) or low > high:
            raise ValueError(f"space[{key!r}] needs finite low <= high, got {bounds!r}")
        if low < spec.low or high > spec.high:
            raise ValueError(
                f"space[{key!r}] range [{low:g}, {high:g}] is outside the param's bounds "
                f"[{spec.low:g}, {spec.high:g}]"
            )
        if spec.kind == "int" and not (low.is_integer() and high.is_integer()):
            raise ValueError(f"space[{key!r}] is an int param; bounds must be whole numbers")
        out[key] = replace(spec, low=low, high=high)
    return out


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class UniverseData:
    """The point-in-time universe on bars before ``end``."""

    candles: dict[str, pd.DataFrame]  # every symbol that is ever a member before end
    closes: pd.DataFrame  # union index x sorted symbols, NaN = no bar
    members: pd.DataFrame  # bool, top_n by trailing dollar volume among eligible coins
    dollar_volume: pd.DataFrame  # trailing median dollar volume, aligned with closes
    segment_starts: dict[str, pd.DatetimeIndex]  # new listing segments (never a first bar)
    btc: pd.DataFrame | None  # BTC<quote> candles before end (benchmarks, regimes, beta)
    btc_error: str | None
    end: pd.Timestamp
    manifest_settings: dict[str, Any]
    ranks_end: pd.Timestamp | None = None  # last row of the rank table before ``end``

    @property
    def index(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.closes.index)

    @property
    def liquidity(self) -> pd.DataFrame:
        """Each member's share of the members' trailing dollar volume (liquidity caps)."""
        return xs.liquidity_share(self.dollar_volume, self.members)

    def through(self, end: pd.Timestamp) -> UniverseData:
        """The same universe restricted to bars before ``end``."""
        if end >= self.end:
            return self
        keep = self.closes.index < end
        candles: dict[str, pd.DataFrame] = {}
        for s, c in self.candles.items():
            part = c.loc[c.index < end]
            if len(part):
                candles[s] = part
        cols = sorted(candles)
        closes = self.closes.loc[keep, cols]
        rows = pd.DatetimeIndex(closes.index)
        return UniverseData(
            candles=candles,
            closes=closes,
            members=self.members.loc[keep, cols],
            dollar_volume=self.dollar_volume.loc[keep, cols],
            segment_starts={
                s: v[v < end]
                for s, v in self.segment_starts.items()
                if s in candles and (v < end).any()
            },
            btc=None if self.btc is None else self.btc.loc[self.btc.index < end],
            btc_error=self.btc_error,
            end=end if len(rows) == 0 else min(end, self.end),
            manifest_settings=self.manifest_settings,
            ranks_end=self.ranks_end,
        )


_BUILD_KEYS = (
    "quote",
    "interval",
    "volume_window_days",
    "min_history_days",
    "relist_gap_days",
    "relist_jump",
    "exclude_bases",
)


def _check_build_settings(meta: Mapping[str, Any], cfg: LabConfig) -> dict[str, Any]:
    settings = meta.get("settings")
    if not isinstance(settings, Mapping):
        raise ValueError("the universe manifest has no settings; rebuild the universe")
    u = asdict(cfg.universe)
    diff = []
    for key in _BUILD_KEYS:
        want = list(u[key]) if isinstance(u[key], tuple) else u[key]
        if settings.get(key) != want:
            diff.append(f"{key} (built {settings.get(key)!r}, lab.toml {want!r})")
    if diff:
        raise ValueError(
            "the built universe does not match lab.toml [universe]: "
            + "; ".join(diff)
            + ". Rebuild it with `uv run python -m lab.data.universe_build build`"
        )
    return dict(settings)


def _new_segments(candles: pd.DataFrame, cfg: LabConfig) -> pd.DatetimeIndex:
    u = cfg.universe
    starts = listing_segment_starts(
        candles,
        relist_gap_days=u.relist_gap_days,
        relist_jump=u.relist_jump,
        interval=u.interval,
    )
    uniq = pd.DatetimeIndex(starts.unique()).sort_values()
    return pd.DatetimeIndex(uniq[uniq > candles.index[0]], name="time")


def load_universe_data(cfg: LabConfig, loader: CandleLoader, *, end: pd.Timestamp) -> UniverseData:
    """The universe on bars before ``end`` (module docstring). Raises
    :class:`~lab.runner.DataUnavailableError` when the universe or a member's candles are
    missing, and ``ValueError`` when the built universe does not match lab.toml."""
    u = cfg.universe
    try:
        ranks, meta = load_universe_ranks(cfg.universe_dir, u.quote, u.interval, end=end)
    except ValueError as exc:
        raise DataUnavailableError(str(exc)) from None
    settings = _check_build_settings(meta, cfg)
    member_wide = ranks.le(u.top_n)
    symbols = sorted(str(s) for s in member_wide.columns if bool(member_wide[s].any()))
    if not symbols:
        raise DataUnavailableError(
            f"the {u.quote} {u.interval} universe has no top-{u.top_n} member before {end.date()}"
        )
    candles: dict[str, pd.DataFrame] = {}
    for s in symbols:
        raw = loader(s, u.interval, end)
        frame = raw.loc[raw.index < end]
        if frame.empty:
            raise DataUnavailableError(
                f"{s} is a universe member before {end.date()} but has no {u.interval} candles; "
                "a missing member would bring survivorship back. Download it with `uv run python "
                "-m lab.data.universe_build download`"
            )
        candles[s] = frame
    closes = xs.close_panel(candles)
    members = (
        member_wide.reindex(index=closes.index, columns=closes.columns, fill_value=False)
        .fillna(False)
        .astype(bool)
    )
    dollar = trailing_dollar_volume(candles, window_days=u.volume_window_days)
    dollar = dollar.reindex(index=closes.index, columns=closes.columns)
    segs: dict[str, pd.DatetimeIndex] = {}
    for s, frame in candles.items():
        new = _new_segments(frame, cfg)
        if len(new):
            segs[s] = new
    btc: pd.DataFrame | None = None
    btc_error: str | None = None
    try:
        raw_btc = loader(benchmark_symbol(cfg), u.interval, end)
        btc = raw_btc.loc[raw_btc.index < end]
        if btc.empty:
            btc, btc_error = None, f"no {benchmark_symbol(cfg)} {u.interval} candles"
    except (DataUnavailableError, ValueError) as exc:
        btc_error = str(exc)
    return UniverseData(
        candles=candles,
        closes=closes,
        members=members,
        dollar_volume=dollar,
        segment_starts=segs,
        btc=btc,
        btc_error=btc_error,
        end=end,
        manifest_settings=settings,
        ranks_end=pd.Timestamp(ranks.index[-1]) if len(ranks.index) else None,
    )


class StaleUniverseError(DataUnavailableError):
    """The universe rank table ends before the candles: rows after it would have no members."""


def _ranks_cutoff(data: UniverseData, cfg: LabConfig) -> pd.Timestamp | None:
    """Exclusive end of the rows the rank table covers (last rank row + 1 bar), or None when
    it covers every loaded candle row."""
    idx = data.index
    if data.ranks_end is None or len(idx) == 0 or data.ranks_end >= idx[-1]:
        return None
    return data.ranks_end + pd.Timedelta(seconds=interval_seconds(cfg.universe.interval))


def stale_universe_note(data: UniverseData, cfg: LabConfig) -> str | None:
    """A warning when the universe rank table ends before the loaded candles (the universe
    was built before the candles were refreshed), else None."""
    cut = _ranks_cutoff(data, cfg)
    if cut is None:
        return None
    assert data.ranks_end is not None
    return (
        f"the universe rank table ends at {data.ranks_end.date()} but candles run to "
        f"{data.index[-1].date()}: rows after the rank table have no members, so the window "
        f"stops at {cut.date()}; {REBUILD_HINT}"
    )


def design_window(data: UniverseData, cfg: LabConfig) -> Window:
    """``[max(design_start, first row with a member), min(holdout_start, last bar end, last
    rank row + 1 bar))``: a stale rank table cuts the window (see :func:`stale_universe_note`)
    instead of filling its tail with cash."""
    idx = data.index
    any_member = data.members.to_numpy(dtype=bool).any(axis=1)
    if not any_member.any():
        raise DataUnavailableError("no universe member in the design window")
    first = pd.Timestamp(idx[int(np.flatnonzero(any_member)[0])])
    start = max(cfg.splits.design_start, first)
    last_end = idx[-1] + pd.Timedelta(seconds=interval_seconds(cfg.universe.interval))
    end = min(cfg.splits.holdout_start, last_end)
    cut = _ranks_cutoff(data, cfg)
    if cut is not None:
        end = min(end, cut)
    if end <= start or int(((idx >= start) & (idx < end)).sum()) < 2:
        raise DataUnavailableError(
            f"fewer than 2 universe bars in the design window [{start.date()}, {end.date()})"
        )
    return Window(start=start, end=end, kind="design")


def family_loader(cfg: LabConfig, loader: CandleLoader) -> CandleLoader:
    """A loader for :func:`lab.research.family.behaviour_family`: the portfolio pseudo-symbol
    gets a frame on the universe's union index (only its bar times are used); any other symbol
    goes to ``loader``."""
    pseudo = universe_symbol(cfg)

    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        if symbol != pseudo:
            return loader(symbol, interval, end)
        data = load_universe_data(cfg, loader, end=end)
        return pd.DataFrame(index=data.index)

    return load


# --------------------------------------------------------------------------- one run


@dataclass(frozen=True)
class PortfolioRun:
    plan: pf.PortfolioPlan
    window: Window
    signals: pf.PortfolioSignals
    weights: pd.DataFrame  # decisions inside the window only (others all-NaN = hold)
    result: PortfolioResult  # engine run on every bar through window.end
    returns: pd.Series  # net per-row returns on the window (zeros before the first decision)
    first_decision: pd.Timestamp | None
    decisions: pd.DatetimeIndex  # decision rows inside the window
    n_filled: int  # decisions with at least one rebalance fill before the next decision
    liquidity: pd.DataFrame
    view: UniverseData

    @property
    def scored(self) -> pd.Series:
        return score_from(self.returns, self.first_decision)


def score_from(returns: pd.Series, first: pd.Timestamp | None) -> pd.Series:
    """``returns`` from the strategy's first decision row on (every report metric and every
    benchmark uses this span). Empty when there was no decision."""
    if first is None:
        return returns.iloc[:0]
    return returns.loc[returns.index >= first]


def _decision_weights(
    sig: pf.PortfolioSignals, window: Window
) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    w = sig.weights.copy()
    idx = w.index
    outside = (idx < window.start) | (idx >= window.end)
    w.loc[outside] = np.nan
    decided = ~w.isna().all(axis=1)
    return w, pd.DatetimeIndex(idx[decided.to_numpy()])


def _first_decision(sig: pf.PortfolioSignals, decisions: pd.DatetimeIndex) -> pd.Timestamp | None:
    """First decision row in the window on which the strategy could have invested (at least
    ``min_names`` sizable eligible names, gate open or not: ``PortfolioSignals.investable``)."""
    if len(decisions) == 0:
        return None
    can = sig.investable.reindex(decisions, fill_value=False).to_numpy(dtype=bool)
    rows = np.flatnonzero(can)
    return pd.Timestamp(decisions[rows[0]]) if rows.size else None


def _filled_decisions(decisions: pd.DatetimeIndex, orders: pd.DataFrame) -> int:
    """Decisions followed by at least one rebalance fill before the next decision."""
    if len(decisions) == 0 or orders.empty:
        return 0
    fills = orders.loc[orders["reason"] == "rebalance", "time"]
    if fills.empty:
        return 0
    times = pd.DatetimeIndex(fills).asi8
    d = decisions.asi8
    owner = np.searchsorted(d, times, side="left") - 1  # last decision strictly before the fill
    owner = owner[owner >= 0]
    return int(np.unique(owner).size)


def run_on_window(
    plan: pf.PortfolioPlan,
    data: UniverseData,
    window: Window,
    cfg: LabConfig,
    *,
    costs: CostModel | None = None,
    impact: ImpactModel | None = None,
    signal_delay: int = 0,
) -> PortfolioRun:
    """Signals on the history through ``window.end``, decisions inside the window, engine on
    every bar through ``window.end``. ``signal_delay`` rows of stale information (same
    rebalance rows, every input from ``signal_delay`` rows earlier; see
    :func:`lab.plan.portfolio.build_signals`) is the adversarial delay check."""
    view = data.through(window.end)
    sig = pf.build_signals(
        plan,
        view.closes,
        view.members,
        dollar_volume=view.dollar_volume,
        liquidity_cap_mult=cfg.universe.liquidity_cap_mult,
        signal_delay=signal_delay,
    )
    w, decisions = _decision_weights(sig, window)
    result = run_portfolio(
        view.candles,
        w,
        costs=costs if costs is not None else cfg.costs,
        init_cash=cfg.init_cash,
        min_rebalance=cfg.portfolio.min_rebalance,
        segment_starts=view.segment_starts or None,
        impact=impact if impact is not None else impact_model(cfg),
    )
    returns = slice_window(result.returns.to_frame(), window)["returns"]
    first = _first_decision(sig, decisions)
    scored_decisions = decisions[decisions >= first] if first is not None else decisions[:0]
    return PortfolioRun(
        plan=plan,
        window=window,
        signals=sig,
        weights=w,
        result=result,
        returns=returns,
        first_decision=first,
        decisions=scored_decisions,
        n_filled=_filled_decisions(scored_decisions, result.orders),
        liquidity=view.liquidity,
        view=view,
    )


def _metrics(returns: pd.Series, interval: str, n_trades: int = 0) -> Metrics:
    m = metrics_from_returns(returns, interval)
    return replace(m, n_trades=int(n_trades))


def _run_metrics(run: PortfolioRun, returns: pd.Series | None = None) -> Metrics:
    """Metrics of ``returns`` (default: the run's scored net returns) with ``n_trades`` = filled
    rebalances, ``exposure`` = share of scored rows holding any coin at the close, and
    ``turnover`` = annual traded notional / equity over the scored rows."""
    r = run.scored if returns is None else returns
    interval = run.plan.interval
    m = _metrics(r, interval, run.n_filled)
    if r.empty:
        return m
    held = run.result.weights.loc[r.index].to_numpy(dtype=float).sum(axis=1)
    turn = run.result.turnover.loc[r.index]
    return replace(
        m, exposure=float(np.mean(held > 1e-12)), turnover=annual_turnover(turn, interval)
    )


# --------------------------------------------------------------------------- ledger


def _record(
    ledger: Ledger,
    *,
    run_id: str,
    source: TrialSource,
    plan_hash_: str,
    plan_json: str,
    params: Mapping[str, Any],
    cfg: LabConfig,
    window: Window,
    costs_json: str,
    metrics: Metrics | None = None,
    metrics_json: str | None = None,
    returns: np.ndarray | None = None,
    n_trades: int = 0,
    error: str | None = None,
    window_kind: WindowKind | None = None,
    sharpe_bar: float | None = None,
) -> int:
    if metrics_json is None:
        metrics_json = metrics_to_json(metrics) if metrics is not None else "{}"
    if sharpe_bar is None and returns is not None and returns.size >= 2:
        sharpe_bar = float(sharpe_per_bar(returns))
    if sharpe_bar is not None and not math.isfinite(sharpe_bar):
        sharpe_bar = None
    return ledger.record(
        TrialRecord(
            run_id=run_id,
            source=source,
            status="error" if error is not None else "ok",
            plan_hash=plan_hash_,
            plan_json=plan_json,
            params_json=json.dumps(to_jsonable(dict(params)), sort_keys=True),
            symbol=universe_symbol(cfg),
            interval=cfg.universe.interval,
            window_kind=window_kind or window.kind,
            window_start=window.start,
            window_end=window.end,
            metrics_json=metrics_json,
            sharpe_per_bar=sharpe_bar,
            n_trades=int(n_trades),
            error=error,
            costs_json=costs_json,
            returns=returns,
        )
    )


def _benchmark_identity(name: str, cfg: LabConfig, params: Mapping[str, Any]) -> tuple[str, str]:
    obj = {
        "name": f"benchmark:{name}",
        "benchmark": name,
        "symbol": universe_symbol(cfg),
        "interval": cfg.universe.interval,
        "params": to_jsonable(dict(params)),
    }
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), text


def _record_benchmark(
    ledger: Ledger,
    run_id: str,
    cfg: LabConfig,
    window: Window,
    name: str,
    params: Mapping[str, Any],
    costs_json: str,
    *,
    metrics: Metrics | None = None,
    metrics_json: str | None = None,
    returns: np.ndarray | None = None,
    sharpe_bar: float | None = None,
) -> int:
    h, text = _benchmark_identity(name, cfg, params)
    return _record(
        ledger,
        run_id=run_id,
        source="benchmark",
        plan_hash_=h,
        plan_json=text,
        params=params,
        cfg=cfg,
        window=window,
        costs_json=costs_json,
        metrics=metrics,
        metrics_json=metrics_json,
        returns=returns,
        sharpe_bar=sharpe_bar,
    )


def _on_window(series: pd.Series, run: PortfolioRun) -> np.ndarray:
    """``series`` on the run's window rows (0 where it has no value), as ledger returns."""
    return series.reindex(run.returns.index, fill_value=0.0).fillna(0.0).to_numpy(dtype=float)


# --------------------------------------------------------------------------- benchmarks


def _btc_benchmarks(
    run: PortfolioRun, cfg: LabConfig
) -> tuple[pd.Series | None, pd.Series | None, str | None]:
    """BTC buy-and-hold and vol-targeted BTC net returns on the strategy's scored rows (rows
    without a BTC bar return 0), both entering at the first decision with costs paid; vol
    estimates warm up on the history before it."""
    btc = run.view.btc
    first = run.first_decision
    if btc is None or first is None:
        return None, None, run.view.btc_error or "no decision"
    history = btc.loc[btc.index < run.window.end]
    span = history.loc[history.index >= first]
    if len(span) < 2:
        return None, None, f"fewer than 2 {benchmark_symbol(cfg)} bars after the first decision"
    target = run.scored.index
    bh = buy_and_hold(span, costs=cfg.costs, init_cash=cfg.init_cash, cost_history=history)
    b = cfg.benchmarks
    vt_w = vol_target_weights(
        history,
        cfg.universe.interval,
        target_annual=b.vol_target_annual,
        lookback_days=b.vol_lookback_days,
        max_leverage=b.vol_max_leverage,
    ).loc[span.index]
    vt = run_weights(
        span,
        vt_w,
        costs=cfg.costs,
        init_cash=cfg.init_cash,
        min_rebalance=b.vol_min_rebalance,
        cost_history=history,
    )
    bh_r = bh.returns.reindex(target, fill_value=0.0).rename("returns")
    vt_r = vt.returns.reindex(target, fill_value=0.0).rename("returns")
    return bh_r, vt_r, None


def _btc_close_returns(run: PortfolioRun) -> pd.Series | None:
    btc = run.view.btc
    if btc is None or len(btc) < 2:
        return None
    r = btc["close"].astype(float).pct_change()
    return r.reindex(run.returns.index)  # NaN where BTC has no bar: dropped, never filled


@dataclass(frozen=True)
class _NullResult:
    sharpe: np.ndarray
    total_return: np.ndarray
    turnover: np.ndarray
    exposure: np.ndarray  # mean target exposure (sum of target weights) over decisions
    n_paths: int
    seed: int
    match_fallbacks: dict[str, int]  # fresh null draws per match outcome, all paths
    kept_match_fallbacks: dict[str, int]  # kept null names per match outcome, all paths
    book_mix: dict[str, dict[str, list[float]]]  # strategy vs null tercile mix of the book
    overlap: float  # mean share of the null's names the strategy also holds
    overlap_chance: float  # the same share for a uniform draw from the pool
    rank_gap: float  # mean |liquidity rank gap| between each null draw and its target


def null_rank(real: float, null: np.ndarray) -> tuple[float, float, bool]:
    """``(percentile, p_value, enough_valid)`` of ``real`` against the null Sharpes with ONE
    rule for undefined paths: NaN paths are dropped from both (percentile = % of valid paths
    strictly below ``real``; p = ``(valid paths >= real + 1) / (valid paths + 1)``). With fewer
    than half the paths valid (or ``real`` undefined) both are NaN and ``enough_valid`` says
    whether the half-valid floor was met."""
    arr = np.asarray(null, dtype=float)
    valid = arr[np.isfinite(arr)]
    enough = arr.size > 0 and 2 * valid.size >= arr.size
    if not enough or not math.isfinite(real) or valid.size == 0:
        return math.nan, math.nan, bool(enough)
    pct = percentile_rank(real, valid)
    p = (int(np.sum(valid >= real)) + 1) / (valid.size + 1)
    return float(pct), float(p), True


def _target_exposure(w: pd.DataFrame, first: pd.Timestamp | None) -> float:
    if first is None:
        return math.nan
    rows = w.loc[w.index >= first]
    vals = rows.to_numpy(dtype=float)
    decided = ~np.isnan(vals).all(axis=1)
    if not decided.any():
        return math.nan
    return float(np.nansum(vals[decided], axis=1).mean())


def _random_selection(run: PortfolioRun, cfg: LabConfig, seed: int) -> _NullResult:
    """The random-selection null (:func:`lab.evaluate.portfolio_benchmarks.
    random_selection_weights`: same members, decision rows, breadth, kept names, total weight,
    weighting rule, liquidity rank band with a volatility-tercile preference, and liquidity
    caps), each path run through the same engine and costs and scored from the strategy's
    first decision row like the strategy.

    The liquidity ranks come from ``view.dollar_volume``: the causal trailing median dollar
    volume (``lab.data.universe.trailing_dollar_volume``, row t reads bars <= t) behind the
    strategy's liquidity shares and its top-N membership. Only its rank within each row's pool
    is used. Every name the null replaces, fresh or kept, is drawn from the band of
    ``[portfolio] null_liquidity_window`` ranks centred on the strategy name it stands in for
    (``portfolio_benchmarks`` module docstring, **Nearest-rank liquidity matching**).

    The null is only drawn for the undelayed run (the report's own plan run): every null input
    here (members, liquidity share, vol panel, inverse vol, dollar volume) is read at the
    decision row, so a ``signal_delay`` run is a ValueError rather than a null whose inputs are
    fresher than the strategy's."""
    n_paths = cfg.portfolio.null_paths
    view = run.view
    liquidity_panel = view.dollar_volume.reindex(
        index=run.weights.index, columns=run.weights.columns
    )
    drawn = _null_paths(run, cfg, seed, n_paths, liquidity_panel)
    sharpe, total, turn, expo = _score_null_paths(run, cfg, [d.weights for d in drawn])
    return _NullResult(
        sharpe=sharpe,
        total_return=total,
        turnover=turn,
        exposure=expo,
        n_paths=n_paths,
        seed=seed,
        match_fallbacks=sum_match_fallbacks(d.match_fallbacks for d in drawn),
        kept_match_fallbacks=sum_kept_match_fallbacks(drawn),
        book_mix=mean_book_mix(drawn),
        overlap=mean_path_stat(d.overlap for d in drawn),
        overlap_chance=mean_path_stat(d.overlap_chance for d in drawn),
        rank_gap=mean_path_stat(d.rank_gap for d in drawn),
    )


def _null_paths(
    run: PortfolioRun,
    cfg: LabConfig,
    seed: int,
    n_paths: int,
    liquidity_panel: pd.DataFrame | None,
) -> list[NullPath]:
    """``n_paths`` null paths from ``default_rng(seed)`` with the run's members, weighting
    inputs, vol panel and liquidity caps; nearest-rank liquidity matching when
    ``liquidity_panel`` is given, else the volatility-only null. Refuses a delayed run (see
    :func:`_random_selection`)."""
    delay = int(run.signals.signal_delay)
    if delay != 0:
        raise ValueError(
            f"the random-selection null needs an undelayed run, got signal_delay={delay}"
        )
    rng = np.random.default_rng(seed)
    return [
        random_selection_path(
            run.weights,
            run.view.members,
            rng,
            inverse_vol=run.signals.inverse_vol,
            max_weight=run.plan.max_weight,
            vol_panel=run.signals.vol_panel,
            liquidity_share=run.liquidity,
            liquidity_cap_mult=cfg.universe.liquidity_cap_mult,
            liquidity_panel=liquidity_panel,
            liquidity_window=cfg.portfolio.null_liquidity_window,
        )
        for _ in range(n_paths)
    ]


def _score_null_paths(
    run: PortfolioRun, cfg: LabConfig, paths: list[pd.DataFrame]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(sharpe, total_return, annual turnover, mean target exposure)`` per path: each run
    through the strategy's engine, costs and impact, scored from its first decision row on the
    run's window like the strategy."""
    view = run.view
    runs = net_returns_for(
        view.candles,
        paths,
        costs=cfg.costs,
        init_cash=cfg.init_cash,
        min_rebalance=cfg.portfolio.min_rebalance,
        segment_starts=view.segment_starts or None,
        impact=impact_model(cfg),
    )
    interval = cfg.universe.interval
    sharpe, total, turn, expo = [], [], [], []
    for path, r in zip(paths, runs, strict=True):
        scored = score_from(
            slice_window(r.returns.to_frame(), run.window)["returns"], run.first_decision
        )
        m = metrics_from_returns(scored, interval)
        sharpe.append(m.sharpe)
        total.append(m.total_return)
        t = (
            r.turnover.loc[r.turnover.index >= run.first_decision]
            if run.first_decision
            else r.turnover
        )
        turn.append(annual_turnover(t, interval))
        expo.append(_target_exposure(path, run.first_decision))
    return (
        np.asarray(sharpe, dtype=float),
        np.asarray(total, dtype=float),
        np.asarray(turn, dtype=float),
        np.asarray(expo, dtype=float),
    )


VOL_ONLY_SIDEBAR_LABEL = (
    "descriptive, not a criterion; less strict on liquidity, more powerful against selection skill"
)


def _vol_only_sidebar(run: PortfolioRun, cfg: LabConfig, seed: int, real: float) -> dict[str, Any]:
    """The report's descriptive volatility-only null (``liquidity_panel=None``; same members,
    weighting inputs, vol panel, caps, costs, impact, ``n_paths`` and seed as the criterion's
    null). It does not match liquidity, so a tilt toward illiquid coins can pass it as skill;
    it copies the strategy's names less, so it sees a real selection skill more often (package
    H4 probe). Never read by a criterion row: the criterion is the liquidity-matched null."""
    n_paths = cfg.portfolio.null_paths
    drawn = _null_paths(run, cfg, seed, n_paths, None)
    sharpe, _, turn, _ = _score_null_paths(run, cfg, [d.weights for d in drawn])
    finite = sharpe[np.isfinite(sharpe)]
    pct, p_value, enough = null_rank(real, sharpe)
    return {
        "label": VOL_ONLY_SIDEBAR_LABEL,
        "n_paths": int(n_paths),
        "n_valid_paths": int(finite.size),
        "enough_valid_paths": enough,
        "seed": int(seed),
        "sharpe_median": num(np.median(finite)) if finite.size else None,
        "percentile": num(pct),
        "p_value": num(p_value),
        "annual_turnover_null_median": num(np.nanmedian(turn)) if turn.size else None,
        "overlap_null": num(mean_path_stat(d.overlap for d in drawn)),
        "matched_on": (
            "as the criterion's null but without liquidity matching: fresh names from the "
            "strategy's fresh names' volatility terciles, kept names uniform from the null's "
            "own survivors"
        ),
    }


def book_mix_json(mix: Mapping[str, Mapping[str, list[float]]]) -> dict[str, Any]:
    """``book_mix`` for the report: every share a JSON number, NaN as None."""
    return {side: {k: [num(x) for x in v] for k, v in d.items()} for side, d in mix.items()}


def _equal_weight(run: PortfolioRun, cfg: LabConfig) -> tuple[pd.Series, pd.Series]:
    """Equal-weight every member on the strategy's own decision rows from its first decision,
    through the same engine, costs, impact and liquidity caps. Returns (scored returns,
    scored turnover)."""
    view = run.view
    schedule = pd.Series(run.weights.index.isin(run.decisions), index=run.weights.index)
    res = equal_weight_universe(
        view.candles,
        view.members,
        schedule.astype(bool),
        costs=cfg.costs,
        init_cash=cfg.init_cash,
        min_rebalance=cfg.portfolio.min_rebalance,
        segment_starts=view.segment_starts or None,
        impact=impact_model(cfg),
        liquidity_share=run.liquidity,
        liquidity_cap_mult=cfg.universe.liquidity_cap_mult,
    )
    r = score_from(slice_window(res.returns.to_frame(), run.window)["returns"], run.first_decision)
    t = res.turnover.loc[r.index] if len(r) else res.turnover.iloc[:0]
    return r, t


# --------------------------------------------------------------------------- report pieces


def _rebalance_log(run: PortfolioRun) -> dict[str, Any]:
    """Per decision row (from the first decision): dispersion, gate state, names held."""
    disp = run.signals.dispersion
    gate = run.signals.gate
    rows: dict[str, list[Any]] = {}
    w = run.weights
    for t in run.decisions:
        row = w.loc[t]
        held = sorted(str(s) for s, v in row.items() if pd.notna(v) and float(v) > 0)
        rows[pd.Timestamp(t).strftime("%Y-%m-%dT%H:%MZ")] = [
            num(round(float(disp.loc[t]), 6)) if pd.notna(disp.loc[t]) else None,
            None if gate is None else bool(gate.loc[t]),
            len(held),
            num(round(float(np.nansum(row.to_numpy(dtype=float))), 6)),
            " ".join(held),
        ]
    return {"columns": list(REBALANCE_LOG_COLUMNS), "rows": rows}


def _period_returns(returns: pd.Series, decisions: pd.DatetimeIndex) -> np.ndarray:
    """Compounded return of the rows after each decision up to and including the next one."""
    if len(decisions) == 0 or returns.empty:
        return np.empty(0)
    idx = returns.index.asi8
    owner = np.searchsorted(decisions.asi8, idx, side="left") - 1
    vals = np.log1p(np.clip(returns.to_numpy(dtype=float), -0.999999, None))
    out = np.zeros(len(decisions))
    ok = owner >= 0
    np.add.at(out, owner[ok], vals[ok])
    return np.expm1(out)


def _dispersion_terciles(
    run: PortfolioRun, others: Mapping[str, pd.Series | None]
) -> dict[str, Any]:
    """Per-period returns grouped by the dispersion tercile at each decision (descriptive and
    ex post: the tercile cut points use every decision's dispersion), for the strategy and the
    benchmarks, so a critic can see whether an edge lives only in high-dispersion periods."""
    disp = run.signals.dispersion.reindex(run.decisions).to_numpy(dtype=float)
    finite = np.isfinite(disp)
    if finite.sum() < 3:
        return {"note": "fewer than 3 decisions with a defined dispersion", "terciles": []}
    cuts = np.quantile(disp[finite], [1 / 3, 2 / 3])
    label = np.where(~finite, -1, np.searchsorted(cuts, disp, side="right"))
    series = {"strategy": run.scored, **{k: v for k, v in others.items() if v is not None}}
    per = {k: _period_returns(v, run.decisions) for k, v in series.items()}
    out = []
    for k, name in enumerate(("low", "mid", "high")):
        sel = label == k
        row: dict[str, Any] = {
            "tercile": name,
            "decisions": int(sel.sum()),
            "dispersion_min": num(disp[sel].min()) if sel.any() else None,
            "dispersion_max": num(disp[sel].max()) if sel.any() else None,
        }
        for key, vals in per.items():
            row[f"{key}_total_return"] = num(np.prod(1.0 + vals[sel]) - 1.0) if sel.any() else None
            row[f"{key}_mean_period_return"] = num(vals[sel].mean()) if sel.any() else None
        out.append(row)
    return {
        "basis": (
            "decision rows from the first decision, split into terciles of the cross-sectional "
            "dispersion at the decision (cut points from every decision, ex post: descriptive "
            "only); period = rows after a decision up to the next"
        ),
        "terciles": out,
    }


def _segment_exits(run: PortfolioRun) -> dict[str, Any]:
    exits = run.result.segment_exits
    if exits.empty:
        return {"n": 0, "rows": {}}
    exits = exits.loc[pd.DatetimeIndex(exits["time"]) >= run.window.start]
    rows = {}
    for r in exits.itertuples(index=False):
        key = f"{pd.Timestamp(r.time).strftime('%Y-%m-%d')}:{r.symbol}"
        rows[key] = {
            "exit_close": num(r.exit_close),
            "reopen_time": seconds(pd.Timestamp(r.reopen_time)),
            "reopen_open": num(r.reopen_open),
            "reopen_ratio": num(r.reopen_ratio),
        }
    return {
        "n": len(rows),
        "rows": rows,
        "note": (
            "held positions force-sold at the close before a new listing segment (a "
            "redenomination or reused ticker); a reopen_ratio far from a power of ten means the "
            "exit dodged a move during the suspension, which is never skill"
        ),
    }


def _regimes(
    run: PortfolioRun, cfg: LabConfig, window_days: int, threshold: float
) -> dict[str, Any]:
    """Regime breakdown of the scored net returns with BTC as the market (labels from the
    run's frozen regime definition). Portfolio rows without a BTC bar are compounded into the
    next BTC bar."""
    from lab.research.criteria import regime_breakdown  # lazy: criteria imports this module

    btc = run.view.btc
    scored = run.scored
    if btc is None or scored.empty:
        return {"error": f"regime breakdown unavailable: {run.view.btc_error or 'no decision'}"}
    history = btc.loc[btc.index < run.window.end]
    start = scored.index[0]
    bars = history.index[history.index >= start]
    if len(bars) < 2:
        return {"error": "regime breakdown unavailable: too few BTC bars"}
    owner = np.searchsorted(bars.asi8, scored.index.asi8, side="left")
    vals = np.log1p(scored.to_numpy(dtype=float))
    keep = owner < len(bars)
    sums = np.zeros(len(bars))
    np.add.at(sums, owner[keep], vals[keep])
    try:
        out = regime_breakdown(
            history,
            start,
            run.window.end,
            np.expm1(sums),
            cfg.universe.interval,
            window_days=window_days,
            threshold=threshold,
        )
    except ValueError as exc:
        return {"error": f"regime breakdown unavailable: {exc}"}
    out["market"] = benchmark_symbol(cfg)
    out["rows_after_last_btc_bar"] = int((~keep).sum())
    return out


def _neighbours(
    run: PortfolioRun, cfg: LabConfig
) -> list[tuple[dict[str, float], pf.PortfolioPlan]]:
    specs = portfolio_param_specs(run.plan)
    base_hash = pf.portfolio_plan_hash(run.plan)
    out: list[tuple[dict[str, float], pf.PortfolioPlan]] = []
    seen = {base_hash}
    for ov in neighbourhood_overrides(
        specs, cfg.stats.neighbourhood_steps, cfg.portfolio.neighbourhood_max_trials
    ):
        try:
            variant = pf.with_params(run.plan, ov)
        except PlanValidationError:
            continue  # not a valid configuration (e.g. exit_quantile < quantile), not a trial
        h = pf.portfolio_plan_hash(variant)
        if h in seen:
            continue
        seen.add(h)
        out.append((ov, variant))
    return out


def _warnings(
    net: Metrics,
    gross: Metrics,
    run: PortfolioRun,
    vt_sharpe: float | None,
    ew_sharpe: float | None,
    alpha: Mapping[str, Any] | None,
) -> list[str]:
    out: list[str] = []
    if run.n_filled < 100:
        out.append(f"Only {run.n_filled} rebalances with a fill (< 100): too few to mean much.")
    if (
        math.isfinite(gross.sharpe)
        and math.isfinite(net.sharpe)
        and gross.sharpe - net.sharpe > 0.3
    ):
        out.append(
            f"Costs cut the Sharpe from {gross.sharpe:.2f} gross to {net.sharpe:.2f} net; the "
            "edge depends heavily on the cost assumption (turnover is the first-order risk)."
        )
    if vt_sharpe is not None and math.isfinite(net.sharpe) and net.sharpe < vt_sharpe:
        out.append(f"Net Sharpe {net.sharpe:.2f} is below vol-targeted BTC ({vt_sharpe:.2f}).")
    if ew_sharpe is not None and math.isfinite(net.sharpe) and net.sharpe < ew_sharpe:
        out.append(
            f"Net Sharpe {net.sharpe:.2f} is below the equal-weight universe ({ew_sharpe:.2f}): "
            "the selection adds nothing over owning the liquid market."
        )
    if alpha is not None:
        p = alpha.get("alpha_p")
        if p is None or p > 0.05:
            out.append(
                "Alpha to BTC is not significant at 5% (HAC t-test on net returns); a long-only "
                "top quantile mostly carries BTC's beta."
            )
    out.append(IMPACT_NOTE)
    return out


# --------------------------------------------------------------------------- backtest report


def backtest_cost(cfg: LabConfig) -> int:
    """Worst-case ledger rows of :func:`portfolio_backtest_report`."""
    return 1 + 4 + max(0, cfg.portfolio.neighbourhood_max_trials - 1)


def portfolio_backtest_report(
    raw_plan: Mapping[str, Any] | str,
    *,
    cfg: LabConfig,
    ledger: Ledger,
    run_id: str,
    loader: CandleLoader,
    seed: int | None = None,
    regime: tuple[int, float] | None = None,
) -> dict[str, Any]:
    """Design-window backtest report of a portfolio plan (module docstring). Every run is a
    ledger row; raises :class:`PlanValidationError` / :class:`DataUnavailableError`.
    ``regime = (window_days, threshold)`` (the research run's frozen regime definition) adds
    a ``regimes`` block with BTC as the market."""
    t0 = time.perf_counter()
    plan = parse_portfolio_plan(raw_plan, cfg)
    h = pf.portfolio_plan_hash(plan)
    plan_dict = pf.plan_to_dict(plan)
    plan_json = _plan_json(plan)
    interval = cfg.universe.interval
    seed = cfg.optuna_seed if seed is None else seed
    impact = impact_model(cfg)
    cj = costs_json_for(cfg.costs, impact)
    data = load_universe_data(cfg, loader, end=cfg.splits.holdout_start)
    window = design_window(data, cfg)
    stale = stale_universe_note(data, cfg)

    # 1. the plan itself
    try:
        run = run_on_window(plan, data, window, cfg)
    except ValueError as exc:
        _record(
            ledger,
            run_id=run_id,
            source="api",
            plan_hash_=h,
            plan_json=plan_json,
            params={},
            cfg=cfg,
            window=window,
            costs_json=cj,
            error=str(exc),
        )
        raise
    scored = run.scored
    net = _run_metrics(run)
    gross_scored = score_from(
        slice_window(run.result.gross_returns.to_frame(), window)["gross_returns"],
        run.first_decision,
    )
    gross = replace(_run_metrics(run, gross_scored), turnover=net.turnover)
    base_returns = run.returns.to_numpy(dtype=float)
    trial_id = _record(
        ledger,
        run_id=run_id,
        source="api",
        plan_hash_=h,
        plan_json=plan_json,
        params={},
        cfg=cfg,
        window=window,
        costs_json=cj,
        metrics=net,
        returns=base_returns,
        n_trades=run.n_filled,
    )
    turnover = (
        run.result.turnover.loc[scored.index] if len(scored) else run.result.turnover.iloc[:0]
    )
    strat_turnover = annual_turnover(turnover, interval) if len(turnover) else math.nan

    # 2. benchmarks, all scored from the first decision row
    benchmarks: dict[str, Any] = {}
    ew_r: pd.Series | None = None
    bh_r = vt_r = None
    rnd: _NullResult | None = None
    if run.first_decision is not None:
        ew_r, ew_t = _equal_weight(run, cfg)
        ew_m = _metrics(ew_r, interval)
        benchmarks["equal_weight"] = {
            **metrics_dict(ew_m),
            "annual_turnover": num(annual_turnover(ew_t, interval)) if len(ew_t) else None,
        }
        _record_benchmark(
            ledger,
            run_id,
            cfg,
            window,
            "equal_weight",
            {"schedule": "strategy decisions"},
            cj,
            metrics=ew_m,
            returns=_on_window(ew_r, run),
        )
        bh_r, vt_r, btc_err = _btc_benchmarks(run, cfg)
        if bh_r is not None and vt_r is not None:
            bh_m = _metrics(bh_r, interval)
            vt_m = _metrics(vt_r, interval)
            benchmarks["btc_buy_and_hold"] = metrics_dict(bh_m)
            benchmarks["btc_vol_target"] = metrics_dict(vt_m)
            b = cfg.benchmarks
            _record_benchmark(
                ledger,
                run_id,
                cfg,
                window,
                "btc_buy_and_hold",
                {},
                cj,
                metrics=bh_m,
                returns=_on_window(bh_r, run),
            )
            _record_benchmark(
                ledger,
                run_id,
                cfg,
                window,
                "btc_vol_target",
                {
                    "target_annual": b.vol_target_annual,
                    "lookback_days": b.vol_lookback_days,
                    "max_leverage": b.vol_max_leverage,
                    "min_rebalance": b.vol_min_rebalance,
                },
                cj,
                metrics=vt_m,
                returns=_on_window(vt_r, run),
            )
        else:
            benchmarks["btc_buy_and_hold"] = {"error": btc_err}
            benchmarks["btc_vol_target"] = {"error": btc_err}
        rnd = _random_selection(run, cfg, seed)
        finite = rnd.sharpe[np.isfinite(rnd.sharpe)]
        pct, p_value, enough = null_rank(net.sharpe, rnd.sharpe)
        benchmarks["random_selection"] = {
            "n_paths": int(rnd.n_paths),
            "n_valid_paths": int(finite.size),
            "enough_valid_paths": enough,
            "seed": int(rnd.seed),
            "sharpe_median": num(np.median(finite)) if finite.size else None,
            "sharpe_p95": num(np.percentile(finite, 95)) if finite.size else None,
            "percentile": num(pct),
            "p_value": num(p_value),
            "nan_rule": (
                "paths with an undefined Sharpe are dropped from both the percentile and the "
                "p-value; with fewer than half the paths valid neither is reported and the "
                "random-selection criterion is missing (fail closed)"
            ),
            "annual_turnover_strategy": num(strat_turnover),
            "annual_turnover_null_median": num(np.nanmedian(rnd.turnover)),
            "target_exposure_strategy": num(_target_exposure(run.weights, run.first_decision)),
            "target_exposure_null_mean": num(np.nanmean(rnd.exposure)),
            "matched_on": (
                "decision rows, members, breadth, kept names, total target weight, weighting "
                "rule, liquidity rank (trailing dollar volume; each fresh and kept name drawn "
                f"from the {cfg.portfolio.null_liquidity_window} ranks centred on the "
                "strategy name it replaces) with a volatility-tercile preference inside that "
                "band when it holds at least two same-tercile names besides the strategy's "
                "own, max_weight and liquidity caps; same engine and costs"
            ),
            "liquidity_window": cfg.portfolio.null_liquidity_window,
            "match_fallbacks": dict(rnd.match_fallbacks),
            "match_fallbacks_note": (
                "fresh null draws, summed over decision rows and paths: rank = drawn from the "
                "liquidity rank band around the strategy's fresh pick, pool = drawn from any "
                "free eligible name (no rank to match, or extra draws the null needed); of "
                "the rank draws, widened = no free name was in the band so the nearest free "
                "rank was used, narrow = the band had fewer free names than its width, "
                "vol_met = drawn from the strategy pick's volatility tercile (the band held "
                "at least two such names besides the pick itself), vol_thin = it held fewer "
                "so the whole band was used (restricting would mostly copy the strategy's own "
                "name), vol_missed = the pick had no volatility tercile; fresh counts include "
                "the strategy's kept names the null's survivors could not match; a high pool "
                "or widened share means the liquidity matching held poorly"
            ),
            "kept_match_fallbacks": dict(rnd.kept_match_fallbacks),
            "kept_match_fallbacks_note": (
                "the null's kept names (as many of its own previous picks as the strategy "
                "kept) matched to the strategy's kept names, same counts; with only a few "
                "survivors to choose from, narrow is the norm and widened is common"
            ),
            "liquidity_rank_gap_mean": num(rnd.rank_gap),
            "liquidity_rank_gap_note": (
                "mean |liquidity rank gap| between each rank-matched null draw and the "
                "strategy name it replaced (0 = same rank), over draws, then paths"
            ),
            "overlap_null": num(rnd.overlap),
            "overlap_chance": num(rnd.overlap_chance),
            "overlap_note": (
                "mean share of the null's held names the strategy also holds on the row, "
                "against the share a uniform draw from the pool would have; the null may "
                "draw the strategy's own names, so a high overlap means the test has less "
                "power to see a real selection skill, not that the strategy is more skilled"
            ),
            "vol_only_sidebar": _vol_only_sidebar(run, cfg, seed, net.sharpe),
            "book_mix": book_mix_json(rnd.book_mix),
            "book_mix_note": (
                "share of held names in each tercile (index 0 = least liquid / least "
                "volatile), count- and weight-weighted, averaged over the decision rows the "
                "null draws on; strategy vs null (mean over paths). The null's book should "
                "sit in the same terciles as the strategy's"
            ),
        }
        _record_benchmark(
            ledger,
            run_id,
            cfg,
            window,
            "random_selection",
            {"n_paths": rnd.n_paths, "seed": seed},
            cj,
            metrics_json=json.dumps(
                {
                    "sharpe": benchmarks["random_selection"]["sharpe_median"],
                    "sharpe_p95": benchmarks["random_selection"]["sharpe_p95"],
                    "percentile": benchmarks["random_selection"]["percentile"],
                },
                sort_keys=True,
            ),
        )

    # 3. ±pct neighbourhood -> neighbourhood PBO
    columns = [base_returns]
    for ov, variant in _neighbours(run, cfg):
        vh = pf.portfolio_plan_hash(variant)
        try:
            vrun = run_on_window(variant, data, window, cfg)
        except ValueError as exc:
            _record(
                ledger,
                run_id=run_id,
                source="neighbourhood",
                plan_hash_=vh,
                plan_json=_plan_json(variant),
                params=ov,
                cfg=cfg,
                window=window,
                costs_json=cj,
                error=str(exc),
            )
            continue
        vm = _run_metrics(vrun)
        r = vrun.returns.to_numpy(dtype=float)
        _record(
            ledger,
            run_id=run_id,
            source="neighbourhood",
            plan_hash_=vh,
            plan_json=_plan_json(variant),
            params=ov,
            cfg=cfg,
            window=window,
            costs_json=cj,
            metrics=vm,
            returns=r,
            n_trades=vrun.n_filled,
        )
        columns.append(r)
    n_splits = pbo_split_count(len(base_returns), cfg.stats.pbo_splits)
    pbo_value: float | None = None
    if len(columns) >= 2 and n_splits >= 2:
        matrix = np.column_stack(columns)
        if np.isfinite(matrix).all():
            pbo_value = num(pbo_cscv(matrix, n_splits=n_splits).pbo)

    # 4. run-local DSR (get_report's row is authoritative) and bootstrap CI
    ppy = bars_per_year(interval)
    n_trials = max(
        1, ledger.count_distinct_plans(run_id, sources=SELECTION_SOURCES, interval=interval)
    )
    variance = ledger.selection_sharpe_variance(
        run_id,
        sources=SELECTION_SOURCES,
        interval=interval,
        window_start=window.start,
        window_end=window.end,
        costs_json=cj,
    )
    scored_arr = scored.to_numpy(dtype=float)
    dsr = deflated_sharpe(scored_arr, n_trials=n_trials, sharpe_variance=variance)
    st = cfg.stats
    ci = (
        stationary_bootstrap_ci(
            scored_arr,
            lambda r: sharpe_per_bar(r) * math.sqrt(ppy),
            n_samples=st.bootstrap_samples,
            mean_block=st.bootstrap_mean_block,
            confidence=st.confidence,
            seed=seed,
        )
        if scored_arr.size >= 2
        else None
    )

    # 5. alpha / beta to BTC on net returns from the first decision
    ab_json: dict[str, Any] | None = None
    btc_r = _btc_close_returns(run)
    if btc_r is not None and run.first_decision is not None:
        try:
            ab = alpha_beta(
                run.returns,
                btc_r,
                interval=interval,
                rolling_days=cfg.portfolio.alpha_rolling_days,
                start=run.first_decision,
            )
            ab_json = {
                **alpha_beta_json(ab, rolling_points=12),
                "benchmark": f"{benchmark_symbol(cfg)} close-to-close",
                "basis": "net per-row returns from the first decision; HAC (Newey-West) t-test",
                "rolling_days": cfg.portfolio.alpha_rolling_days,
            }
        except ValueError as exc:
            ab_json = {"error": str(exc)}

    vt_sharpe = num(_get(benchmarks, "btc_vol_target", "sharpe"))
    ew_sharpe = num(_get(benchmarks, "equal_weight", "sharpe"))
    report: dict[str, Any] = {
        "kind": pf.PORTFOLIO_KIND,
        "trial_id": trial_id,
        "run_id": run_id,
        "plan_hash": h,
        "plan": plan_dict,
        "plan_logic": pf.plan_logic(plan),
        "symbol": universe_symbol(cfg),
        "interval": interval,
        "window": {
            "kind": "design",
            "start": seconds(window.start),
            "end": seconds(window.end),
            "universe_stale": stale,
        },
        "scored_from": seconds(run.first_decision) if run.first_decision is not None else None,
        "scored_from_rule": (
            "first rebalance row on which the plan could have invested (at least min_names "
            "sizable eligible names), gate open or not; every benchmark starts on it too"
        ),
        "universe": {
            "quote": cfg.universe.quote,
            "top_n": cfg.universe.top_n,
            "liquidity_cap_mult": cfg.universe.liquidity_cap_mult,
            "symbols_ever_member": len(data.candles),
        },
        "costs": _costs_block(cfg, cfg.costs, impact),
        "metrics": {"net": metrics_dict(net), "gross": metrics_dict(gross)},
        "portfolio": {
            "n_rebalances": len(run.decisions),
            "n_rebalances_filled": int(run.n_filled),
            "n_orders": len(run.result.orders),
            "annual_turnover": num(strat_turnover),
            "fees_paid": num(run.result.fees_paid),
            "impact_cost": num(run.result.impact_cost),
            "fees_paid_pct_of_initial": num(run.result.fees_paid / cfg.init_cash),
            "impact_cost_pct_of_initial": num(run.result.impact_cost / cfg.init_cash),
            "target_exposure_mean": num(_target_exposure(run.weights, run.first_decision)),
            "n_trades_meaning": "metrics.net.n_trades = rebalances with at least one fill",
        },
        "benchmarks": benchmarks,
        "alpha_beta": ab_json,
        "stats": {
            "dsr": {
                "dsr": num(dsr.dsr),
                "psr": num(dsr.psr),
                "n_trials": int(n_trials),
                "sr0_annual": num(dsr.sr0_per_bar * math.sqrt(ppy)),
            },
            "pbo": {
                "pbo": pbo_value,
                "n_trials": len(columns),
                "n_splits": int(n_splits if pbo_value is not None else 0),
                "basis": "neighbourhood",
            },
            "sharpe_ci": (
                {"low": num(ci.low), "high": num(ci.high), "level": float(ci.level)}
                if ci is not None
                else None
            ),
        },
        "segment_exits": _segment_exits(run),
        "rebalance_log": _rebalance_log(run),
        "dispersion_terciles": _dispersion_terciles(
            run, {"equal_weight": ew_r, "btc_buy_and_hold": bh_r}
        ),
        "regimes": (
            _regimes(run, cfg, regime[0], regime[1])
            if regime is not None
            else {"error": "regime breakdown needs the research run's regime definition"}
        ),
        "not_applicable": {
            "fixed_param_walk_forward": "use walk_forward (tuned on train folds only)",
        },
        "warnings": ([stale] if stale else [])
        + _warnings(net, gross, run, vt_sharpe, ew_sharpe, ab_json),
        "disclaimer": _disclaimer(cfg),
        "elapsed_ms": 0,
    }
    report = to_jsonable(report)
    report["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    return report


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    return obj


# --------------------------------------------------------------------------- Optuna


@dataclass(frozen=True)
class PortfolioSearch:
    best_params: dict[str, float] | None
    best_value: float
    best_plan: pf.PortfolioPlan | None
    n_trials: int
    n_failed: int
    n_pruned: int
    trial_ids: tuple[int, ...]
    study_name: str
    window: Window


def _suggest(trial: optuna.Trial, space: Mapping[str, ParamSpec]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, spec in space.items():
        if spec.kind == "int":
            out[key] = float(trial.suggest_int(key, int(spec.low), int(spec.high)))
        else:
            out[key] = float(trial.suggest_float(key, spec.low, spec.high))
    return out


def portfolio_sweep(
    plan: pf.PortfolioPlan,
    data: UniverseData,
    window: Window,
    *,
    ledger: Ledger,
    run_id: str,
    cfg: LabConfig,
    n_trials: int,
    seed: int,
    space: Mapping[str, tuple[float, float]],
    min_trades: int,
    window_kind: WindowKind = "design",
) -> PortfolioSearch:
    """Seeded TPE search maximising the scored net Sharpe on ``window``. Every trial is a
    ledger row (``"optuna"``, with the ``_study`` design in params_json). A combination with
    ``exit_quantile < quantile`` is infeasible: recorded as an error row and pruned. A trial
    with fewer than ``min_trades`` filled rebalances, or an undefined Sharpe, is never
    selected."""
    if window.end > cfg.splits.holdout_start:
        raise ValueError("tuning must stay inside the design window")
    if isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1:
        raise ValueError(f"n_trials must be a positive int, got {n_trials!r}")
    resolved = resolve_portfolio_space(plan, space)
    interval = cfg.universe.interval
    impact = impact_model(cfg)
    cj = costs_json_for(cfg.costs, impact)
    study_name = (
        f"{run_id}:{plan.name}:{window_kind}:"
        f"{window.start.strftime('%Y%m%d')}-{window.end.strftime('%Y%m%d')}:seed{seed}"
    )
    meta = study_meta(
        study_name, resolved, explicit=True, seed=seed, n_trials=n_trials, min_trades=min_trades
    )
    ids: list[int] = []
    sharpes: list[float] = []
    params_by: list[dict[str, float]] = []
    pruned = 0

    def objective(trial: optuna.Trial) -> float:
        nonlocal pruned
        params = _suggest(trial, resolved)
        try:
            concrete = pf.with_params(plan, params)
        except PlanValidationError as exc:
            ids.append(
                _record_optuna_error(
                    ledger,
                    run_id,
                    plan,
                    params,
                    meta,
                    cfg,
                    window,
                    cj,
                    window_kind,
                    f"infeasible: {exc}",
                    trial=trial.number,
                )
            )
            sharpes.append(math.nan)
            params_by.append(params)
            pruned += 1
            raise optuna.TrialPruned(str(exc)) from None
        try:
            run = run_on_window(concrete, data, window, cfg)
            m = _run_metrics(run)
        except Exception as exc:  # every failure is an error row, never a lost trial
            ids.append(
                _record_optuna_error(
                    ledger,
                    run_id,
                    concrete,
                    params,
                    meta,
                    cfg,
                    window,
                    cj,
                    window_kind,
                    f"{type(exc).__name__}: {exc}",
                    trial=trial.number,
                )
            )
            sharpes.append(math.nan)
            params_by.append(params)
            return FAILED_OBJECTIVE
        returns = run.returns.to_numpy(dtype=float)
        ids.append(
            ledger.record(
                TrialRecord(
                    run_id=run_id,
                    source="optuna",
                    status="ok",
                    plan_hash=pf.portfolio_plan_hash(concrete),
                    plan_json=_plan_json(concrete),
                    params_json=params_json(params, meta, trial=trial.number),
                    symbol=universe_symbol(cfg),
                    interval=interval,
                    window_kind=window_kind,
                    window_start=window.start,
                    window_end=window.end,
                    metrics_json=metrics_to_json(m),
                    sharpe_per_bar=_finite(sharpe_per_bar(returns)),
                    n_trades=int(run.n_filled),
                    costs_json=cj,
                    returns=returns,
                )
            )
        )
        params_by.append(params)
        if run.n_filled < min_trades or not math.isfinite(m.sharpe):
            sharpes.append(math.nan)
            return FAILED_OBJECTIVE
        sharpes.append(float(m.sharpe))
        return float(m.sharpe)

    study = optuna.create_study(
        direction="maximize", sampler=make_tpe_sampler(seed), study_name=study_name
    )
    study.optimize(objective, n_trials=n_trials, catch=())
    finite = [i for i, s in enumerate(sharpes) if math.isfinite(s)]
    if finite:
        best = max(finite, key=lambda i: (sharpes[i], -i))
        best_params: dict[str, float] | None = params_by[best]
        best_plan: pf.PortfolioPlan | None = pf.with_params(plan, params_by[best])
        best_value = sharpes[best]
    else:
        best_params, best_plan, best_value = None, None, math.nan
    return PortfolioSearch(
        best_params=best_params,
        best_value=best_value,
        best_plan=best_plan,
        n_trials=len(ids),
        n_failed=len(sharpes) - len(finite),
        n_pruned=pruned,
        trial_ids=tuple(ids),
        study_name=study_name,
        window=window,
    )


def _finite(x: float) -> float | None:
    return float(x) if math.isfinite(x) else None


def _record_optuna_error(
    ledger: Ledger,
    run_id: str,
    plan: pf.PortfolioPlan,
    params: Mapping[str, float],
    meta: Mapping[str, object],
    cfg: LabConfig,
    window: Window,
    costs_json: str,
    window_kind: WindowKind,
    message: str,
    trial: int | None = None,
) -> int:
    return ledger.record(
        TrialRecord(
            run_id=run_id,
            source="optuna",
            status="error",
            plan_hash=pf.portfolio_plan_hash(plan),
            plan_json=_plan_json(plan),
            params_json=params_json(params, meta, trial=trial),
            symbol=universe_symbol(cfg),
            interval=cfg.universe.interval,
            window_kind=window_kind,
            window_start=window.start,
            window_end=window.end,
            metrics_json="{}",
            sharpe_per_bar=None,
            n_trades=0,
            error=message,
            costs_json=costs_json,
        )
    )


# --------------------------------------------------------------------------- walk-forward


@dataclass(frozen=True)
class PortfolioFold:
    fold: int
    train: Window
    test: Window
    best_params: dict[str, float]
    selected: bool
    train_sharpe: float
    test_metrics: Metrics
    test_trial_id: int
    search: PortfolioSearch


@dataclass(frozen=True)
class PortfolioWalkForward:
    folds: tuple[PortfolioFold, ...]
    oos_returns: pd.Series
    oos_metrics: Metrics
    n_train_trials: int
    boundary_charges: tuple[float, ...]


def walk_forward_cost(n_folds: int, n_trials_per_fold: int) -> int:
    return (n_trials_per_fold + 1) * n_folds


def portfolio_folds(window: Window, cfg: LabConfig) -> list[Any]:
    s = cfg.splits
    return walk_forward_folds(
        window,
        train_months=s.wf_train_months,
        test_months=s.wf_test_months,
        step_months=s.wf_step_months,
        anchored=s.wf_anchored,
    )


def _boundary_charge(run: PortfolioRun, cfg: LabConfig, impact: ImpactModel) -> float:
    """Fraction of equity an exit of every position held at the window's last close would cost:
    sum over held symbols of ``w x (1 - (1 - slip - impact)(1 - fee))`` with the symbol's
    last-bar slippage and its impact inputs at that bar (both causal)."""
    held = run.result.weights.iloc[-1]
    total = 0.0
    for symbol, w in held.items():
        weight = float(w)
        if not weight > 0:
            continue
        candles = run.view.candles[str(symbol)]
        slip = float(slippage_series(candles, cfg.costs)[-1])
        starts = run.view.segment_starts.get(str(symbol))
        adv, sigma = impact_inputs(
            candles, impact, segment_starts=starts if starts is not None and len(starts) else None
        )
        frac = float(
            impact_fraction(np.array([weight * impact.aum_usd]), adv[-1:], sigma[-1:], impact)[0]
        )
        total += weight * (1.0 - (1.0 - slip - frac) * (1.0 - cfg.costs.fee_rate))
    return total


def portfolio_walk_forward(
    plan: pf.PortfolioPlan,
    data: UniverseData,
    folds: list[Any],
    *,
    ledger: Ledger,
    run_id: str,
    cfg: LabConfig,
    n_trials_per_fold: int,
    seed: int,
    space: Mapping[str, tuple[float, float]],
    min_trades: int,
) -> PortfolioWalkForward:
    """Per fold: :func:`portfolio_sweep` on the train window only (``walk_forward_train``), then
    the chosen params once on the test window (``walk_forward_test``, an ``"optuna"`` row).
    Test returns are stitched with the exit of positions open at each boundary charged."""
    if not folds:
        raise ValueError("no walk-forward fold fits in the portfolio design window")
    resolve_portfolio_space(plan, space)
    interval = cfg.universe.interval
    impact = impact_model(cfg)
    cj = costs_json_for(cfg.costs, impact)
    out: list[PortfolioFold] = []
    stitched: list[pd.Series] = []
    charges: list[float] = []
    for k, fold in enumerate(folds):
        search = portfolio_sweep(
            plan,
            data,
            fold.train,
            ledger=ledger,
            run_id=run_id,
            cfg=cfg,
            n_trials=n_trials_per_fold,
            seed=seed + fold.index,
            space=space,
            min_trades=min_trades,
            window_kind="walk_forward_train",
        )
        selected = search.best_plan is not None and search.best_params is not None
        chosen = search.best_plan if search.best_plan is not None else plan
        chosen_params = (
            dict(search.best_params)
            if search.best_params is not None
            else {k2: s.default for k2, s in resolve_portfolio_space(plan, space).items()}
        )
        test = Window(start=fold.test.start, end=fold.test.end, kind="walk_forward_test")
        run = run_on_window(chosen, data, test, cfg)
        m = _run_metrics(run, run.returns)
        meta = study_meta(
            search.study_name,
            resolve_portfolio_space(plan, space),
            explicit=True,
            seed=seed + fold.index,
            n_trials=n_trials_per_fold,
            min_trades=min_trades,
        )
        r = run.returns.to_numpy(dtype=float)
        test_id = ledger.record(
            TrialRecord(
                run_id=run_id,
                source="optuna",
                status="ok",
                plan_hash=pf.portfolio_plan_hash(chosen),
                plan_json=_plan_json(chosen),
                params_json=params_json(chosen_params, meta),
                symbol=universe_symbol(cfg),
                interval=interval,
                window_kind="walk_forward_test",
                window_start=test.start,
                window_end=test.end,
                metrics_json=metrics_to_json(m),
                sharpe_per_bar=_finite(sharpe_per_bar(r)) if r.size >= 2 else None,
                n_trades=int(run.n_filled),
                costs_json=cj,
                returns=r,
            )
        )
        out.append(
            PortfolioFold(
                fold=fold.index,
                train=fold.train,
                test=test,
                best_params=chosen_params,
                selected=selected,
                train_sharpe=search.best_value,
                test_metrics=m,
                test_trial_id=test_id,
                search=search,
            )
        )
        returns = run.returns
        charge = 0.0
        if k < len(folds) - 1 and len(run.result.weights):
            charge = _boundary_charge(run, cfg, impact)
            if charge:
                returns = returns.copy()
                returns.iloc[-1] = (1.0 + returns.iloc[-1]) * (1.0 - charge) - 1.0
        charges.append(charge)
        stitched.append(returns)
    oos = pd.concat(stitched).rename("oos_returns")
    return PortfolioWalkForward(
        folds=tuple(out),
        oos_returns=oos,
        oos_metrics=_metrics(oos, interval, sum(f.test_metrics.n_trades for f in out)),
        n_train_trials=sum(f.search.n_trials for f in out),
        boundary_charges=tuple(charges),
    )


# --------------------------------------------------------------------------- adversarial


def _offset_variants(plan: pf.PortfolioPlan, cfg: LabConfig) -> list[pf.PortfolioPlan]:
    """The same plan rebalancing on up to :data:`ADVERSARIAL_OFFSETS_MAX` other offsets (evenly
    spread): a schedule that only works on one weekday is timing luck. Built here with the
    plan-type validator (which allows ``0 <= offset < every``); the pipeline never accepts them
    back (:func:`parse_portfolio_plan` pins the offset at 0)."""
    others = [o for o in range(plan.rebalance_every) if o != plan.rebalance_offset]
    if len(others) > ADVERSARIAL_OFFSETS_MAX:
        pos = np.unique(np.round(np.linspace(0, len(others) - 1, ADVERSARIAL_OFFSETS_MAX)))
        others = [others[int(i)] for i in pos]
    out = []
    for off in others:
        data = pf.plan_to_dict(plan)
        data["rebalance"]["offset"] = off
        res = pf.validate_portfolio_plan(data, interval=cfg.universe.interval)
        if res.plan is not None:
            out.append(res.plan)
    return out


def adversarial_cost(cfg: LabConfig, plan: pf.PortfolioPlan | None = None) -> int:
    offsets = ADVERSARIAL_OFFSETS_MAX
    if plan is not None:
        offsets = min(ADVERSARIAL_OFFSETS_MAX, max(0, plan.rebalance_every - 1))
    return 1 + len(cfg.stats.adversarial_delays) + len(cfg.stress_factors) + offsets


def portfolio_adversarial_report(
    raw_plan: Mapping[str, Any] | str,
    *,
    cfg: LabConfig,
    ledger: Ledger,
    run_id: str,
    loader: CandleLoader,
) -> dict[str, Any]:
    """Adversarial checks with a portfolio meaning, every variant a ledger row
    (``"adversarial"``, the base plan's hash):

    * stale information, 1/3/5 REBALANCE PERIODS (``[stats] adversarial_delays`` x
      ``rebalance.every`` rows): the same rebalance rows decide on inputs that many rows old
      (:data:`DELAY_NOTE`; should decay gradually, neither vanish nor stay unchanged). Each row
      reports ``delay`` in rows (what ``require_gradual_delay_decay`` reads) and
      ``delay_periods``;
    * cost stress x ``stress_factors``: fees, slippage AND impact scaled (net return must stay
      positive at the criteria's factor);
    * timing luck: the same plan on the other rebalance weekdays (``rebalance_offset`` block,
      :data:`TIMING_LUCK_NOTE`). Descriptive only: the offset is pinned at 0, the variants
      cannot be resubmitted, and their rows are ``"adversarial"`` (never a selection source).

    Not applicable (listed in ``not_applicable``): the sign flip (a long-only book cannot
    short; the random-selection null asks whether the ranking beats chance), the Monte Carlo
    trade reshuffle (a portfolio has no discrete trades to reorder), and the permutation null
    (replaced by the random-selection null in run_backtest)."""
    t0 = time.perf_counter()
    plan = parse_portfolio_plan(raw_plan, cfg)
    h = pf.portfolio_plan_hash(plan)
    plan_json = _plan_json(plan)
    interval = cfg.universe.interval
    data = load_universe_data(cfg, loader, end=cfg.splits.holdout_start)
    window = design_window(data, cfg)
    base_impact = impact_model(cfg)

    def one(
        params: Mapping[str, Any],
        *,
        variant: pf.PortfolioPlan = plan,
        costs: CostModel | None = None,
        impact: ImpactModel | None = None,
        signal_delay: int = 0,
        source: TrialSource = "adversarial",
    ) -> dict[str, Any]:
        c = costs or cfg.costs
        imp = impact or base_impact
        vcj = costs_json_for(c, imp)
        try:
            run = run_on_window(
                variant, data, window, cfg, costs=c, impact=imp, signal_delay=signal_delay
            )
        except ValueError as exc:
            tid = _record(
                ledger,
                run_id=run_id,
                source=source,
                plan_hash_=h,
                plan_json=plan_json,
                params=params,
                cfg=cfg,
                window=window,
                costs_json=vcj,
                error=str(exc),
            )
            return {
                "trial_id": tid,
                "status": "error",
                "error": str(exc),
                "sharpe": None,
                "total_return": None,
                "max_drawdown": None,
                "n_trades": 0,
            }
        m = _run_metrics(run)
        tid = _record(
            ledger,
            run_id=run_id,
            source=source,
            plan_hash_=h,
            plan_json=plan_json,
            params=params,
            cfg=cfg,
            window=window,
            costs_json=vcj,
            metrics=m,
            returns=run.returns.to_numpy(dtype=float),
            n_trades=run.n_filled,
        )
        return {
            "trial_id": tid,
            "status": "ok",
            "error": None,
            "sharpe": num(m.sharpe),
            "total_return": num(m.total_return),
            "max_drawdown": num(m.max_drawdown),
            "n_trades": int(run.n_filled),
        }

    base = one({}, source="api")
    every = plan.rebalance_every
    delays = [
        {
            "delay": int(k) * every,
            "delay_periods": int(k),
            **one(
                {"delay": int(k) * every, "delay_periods": int(k)},
                signal_delay=int(k) * every,
            ),
        }
        for k in cfg.stats.adversarial_delays
    ]
    stress = [
        {
            "factor": float(f),
            **one(
                {"factor": float(f)},
                costs=cfg.costs.scaled(float(f)),
                impact=impact_model(cfg, float(f)),
            ),
        }
        for f in cfg.stress_factors
    ]
    offsets = [
        {
            "offset": v.rebalance_offset,
            **one({"timing_luck_offset": v.rebalance_offset}, variant=v),
        }
        for v in _offset_variants(plan, cfg)
    ]
    base_sharpe = base["sharpe"] if base["sharpe"] is not None else math.nan
    delay_v = delay_verdict(
        base_sharpe,
        {r["delay"]: (r["sharpe"] if r["sharpe"] is not None else math.nan) for r in delays},
    )
    cost_v = cost_stress_verdict(
        base_sharpe,
        {r["factor"]: (r["sharpe"] if r["sharpe"] is not None else math.nan) for r in stress},
    )
    off_sharpes = [r["sharpe"] for r in offsets if r["sharpe"] is not None]
    every_sharpe = off_sharpes + ([base_sharpe] if math.isfinite(base_sharpe) else [])
    spread = (max(every_sharpe) - min(every_sharpe)) if len(every_sharpe) >= 2 else None
    if not offsets:
        off_v = "timing luck: not applicable (rebalance every row)"
    elif not off_sharpes:
        off_v = "timing luck: no other offset produced a Sharpe"
    else:
        positive = sum(1 for s in off_sharpes if s > 0)
        off_v = (
            f"timing luck (descriptive, not resubmittable): {positive} of {len(off_sharpes)} "
            f"other rebalance weekdays have a positive Sharpe (min {min(off_sharpes):.2f}, max "
            f"{max(off_sharpes):.2f}; the lab's offset 0: {base_sharpe:.2f}; spread "
            f"{spread:.2f})"
        )
    out: dict[str, Any] = {
        "kind": pf.PORTFOLIO_KIND,
        "trial_id": base["trial_id"],
        "run_id": run_id,
        "plan_hash": h,
        "plan": pf.plan_to_dict(plan),
        "symbol": universe_symbol(cfg),
        "interval": interval,
        "window": {"kind": "design", "start": seconds(window.start), "end": seconds(window.end)},
        "costs": _costs_block(cfg, cfg.costs, base_impact),
        "base": {k: v for k, v in base.items() if k != "trial_id"},
        "delay": {
            "rows": delays,
            "verdict": delay_v,
            "unit": f"rows; delay_periods x rebalance.every ({every})",
            "note": DELAY_NOTE,
        },
        "cost_stress": {
            "rows": stress,
            "verdict": cost_v,
            "scaled": "fee, slippage (rate, vol multiple, cap) and impact (Y and cap)",
        },
        "rebalance_offset": {
            "rows": offsets,
            "verdict": off_v,
            "resubmittable": False,
            "lab_offset": pf.LAB_REBALANCE_OFFSET,
            "sharpe_min": num(min(every_sharpe)) if every_sharpe else None,
            "sharpe_max": num(max(every_sharpe)) if every_sharpe else None,
            "sharpe_spread": num(spread) if spread is not None else None,
            "note": TIMING_LUCK_NOTE,
        },
        "not_applicable": {
            "sign_flip": "a long-only portfolio cannot short; the random-selection null tests "
            "whether the ranking beats chance",
            "monte_carlo": "no discrete trades to reshuffle in a rebalanced book",
            "permutation": "replaced by the random-selection null in run_backtest",
        },
        "verdicts": [delay_v, cost_v, off_v],
        "disclaimer": _disclaimer(cfg),
        "elapsed_ms": 0,
    }
    out = to_jsonable(out)
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    return out


# --------------------------------------------------------------------------- cross-check


def portfolio_crosscheck(
    plan: pf.PortfolioPlan, cfg: LabConfig, loader: CandleLoader, window: Window
) -> dict[str, Any]:
    """Re-run the plan's design-window decisions through the independent accounting replay
    (:mod:`lab.crosscheck.portfolio`) and compare net equity with the engine at every row.
    Scope (:data:`CROSSCHECK_SCOPE`): accounting only; the replay takes the strategy's weights
    and the lab's cost functions as given, so it cannot catch signal look-ahead or a cost-model
    bug."""
    data = load_universe_data(cfg, loader, end=cfg.splits.holdout_start)
    if window.end > cfg.splits.holdout_start:
        raise ValueError("the backtest window reaches the locked holdout")
    impact = impact_model(cfg)
    run = run_on_window(plan, data, window, cfg)
    replay = replay_portfolio(
        run.view.candles,
        run.weights,
        costs=cfg.costs,
        init_cash=cfg.init_cash,
        min_rebalance=cfg.portfolio.min_rebalance,
        segment_starts=run.view.segment_starts or None,
        impact=impact,
    )
    cmp = compare_equity(run.result.equity, replay.equity)
    n_engine = len(run.result.orders)
    matched = bool(cmp.matched and n_engine == replay.n_orders)
    notes = []
    if n_engine != replay.n_orders:
        notes.append(f"order counts differ: engine {n_engine}, replay {replay.n_orders}")
    return {
        "matched": matched,
        "available": True,
        "reason": None,
        "engine": "independent python replay (lab.crosscheck.portfolio)",
        "scope": CROSSCHECK_SCOPE,
        "max_rel_equity_diff": num(cmp.max_rel_diff),
        "worst_time": seconds(cmp.worst_time) if cmp.worst_time is not None else None,
        "tolerance": cmp.tolerance,
        "final_equity_engine": num(cmp.final_engine),
        "final_equity_replay": num(cmp.final_replay),
        "n_orders_engine": n_engine,
        "n_orders_replay": int(replay.n_orders),
        "fees_engine": num(run.result.fees_paid),
        "fees_replay": num(replay.fees_paid),
        "impact_engine": num(run.result.impact_cost),
        "impact_replay": num(replay.impact_cost),
        "notes": notes,
    }


# --------------------------------------------------------------------------- holdout


def portfolio_holdout_evaluation(
    plan: pf.PortfolioPlan,
    cfg: LabConfig,
    loader: CandleLoader,
    *,
    now: pd.Timestamp | None = None,
) -> tuple[Callable[[], tuple[PortfolioRun, Metrics, int]], Window, int]:
    """``(evaluate, holdout window, design rows)`` for the holdout check: data through the
    last closed bar is loaded here (raises :class:`DataUnavailableError` when fewer than 2
    universe rows follow ``holdout_start``, and :class:`StaleUniverseError` when the universe
    rank table ends before the candles, so a stale universe never spends a check on cash), and
    ``evaluate()`` runs the plan with decisions inside the holdout window only. Only
    :mod:`lab.research.portfolio` calls this, BEFORE the check is spent; ``evaluate()`` runs
    after."""
    step = pd.Timedelta(seconds=interval_seconds(cfg.universe.interval))
    end = (now or pd.Timestamp.now(tz="UTC")).floor(step)
    data = load_universe_data(cfg, loader, end=end)
    start = cfg.splits.holdout_start
    idx = data.index
    if int((idx >= start).sum()) < 2:
        raise DataUnavailableError(
            f"no universe bars after holdout_start {start.date()}; download recent months with "
            "`uv run python -m lab.data.universe_build download` and rebuild the universe"
        )
    if _ranks_cutoff(data, cfg) is not None:
        assert data.ranks_end is not None
        raise StaleUniverseError(
            f"the universe rank table ends at {data.ranks_end.date()} but candles run to "
            f"{idx[-1].date()}: the holdout rows after it would have no members and be scored "
            f"as all cash. Have the owner {REBUILD_HINT} (and start a new research run, since "
            "the manifest is frozen into this one)"
        )
    window = Window(start=start, end=idx[-1] + step, kind="holdout")
    design = design_window(data.through(start), cfg)
    design_rows = int(((idx >= design.start) & (idx < design.end)).sum())

    def evaluate() -> tuple[PortfolioRun, Metrics, int]:
        run = run_on_window(plan, data, window, cfg)
        holdout_rows = int(((idx >= window.start) & (idx < window.end)).sum())
        return run, _run_metrics(run), holdout_rows

    return evaluate, window, design_rows
