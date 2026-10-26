"""Benchmarks for multi-coin portfolio strategies (PLAN.md Phase 10).

A cross-sectional strategy must beat three things, all through the same engine and costs:

1. **Equal-weight universe** (:func:`equal_weight_universe`): every eligible member, equal
   weight, rebalanced on the strategy's schedule. Separates stock-picking from simply owning the
   liquid crypto market.
2. **Vol-targeted BTC** (:func:`btc_vol_target_returns`): the lab's single-asset benchmark,
   reindexed to the portfolio calendar, so a portfolio result is comparable with every earlier run.
3. **Random-selection null** (:func:`random_selection_null`): on each of the strategy's decision
   rows the null holds as many members as the strategy, with the same total weight (so the same
   cash share) and the same weighting rule (equal or inverse-vol). It also keeps as many of its
   *own* current names as the strategy kept of its previous names, drawing only the rest anew,
   so its turnover and costs match the strategy's (a null that redraws everything every week
   pays about twice the costs of a 28-day momentum book and hands the strategy a fake edge).
   Given a trailing-volatility panel, fresh names are drawn from the same volatility terciles
   as the strategy's fresh names, so a tilt toward volatile coins (which momentum has) is not
   credited as selection skill. Only the choice of coins within those constraints differs.
   Mean turnover of both is reported side by side.

Pure: no I/O, no prints, no module state; randomness only through an explicit seed.
Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from lab.engine.portfolio import PortfolioResult, net_returns_for, run_portfolio
from lab.evaluate.benchmarks import vol_target_buy_and_hold
from lab.stats.metrics import metrics_from_returns
from lab.types import CostModel, bars_per_year


def _check_panel(name: str, frame: pd.DataFrame, like: pd.DataFrame | None = None) -> None:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"{name} must be a DataFrame, got {type(frame).__name__}")
    if like is not None and (
        not frame.index.equals(like.index) or list(frame.columns) != list(like.columns)
    ):
        raise ValueError(f"{name} must have the same index and columns as the strategy weights")


def _check_members(members: pd.DataFrame) -> None:
    _check_panel("members", members)
    if not all(pd.api.types.is_bool_dtype(t) for t in members.dtypes):
        raise ValueError("members must be a bool frame (e.g. eligible & top-N liquid)")


def equal_weight_weights(members: pd.DataFrame, schedule: pd.Series) -> pd.DataFrame:
    """Decision rows (``schedule`` True): 1/k on each of the k members, 0 elsewhere (all 0 if
    k = 0). Other rows all NaN (hold)."""
    _check_members(members)
    if not isinstance(schedule, pd.Series) or not schedule.index.equals(members.index):
        raise ValueError("schedule must be a bool Series on members.index")
    if not pd.api.types.is_bool_dtype(schedule):
        raise ValueError(f"schedule must be bool, got dtype {schedule.dtype}")
    m = members.to_numpy(dtype=bool)
    k = m.sum(axis=1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(k > 0, m / np.maximum(k, 1), 0.0)
    w[~schedule.to_numpy(dtype=bool)] = np.nan
    return pd.DataFrame(w, index=members.index, columns=members.columns)


def equal_weight_universe(
    candles_by_symbol: Mapping[str, pd.DataFrame],
    members: pd.DataFrame,
    schedule: pd.Series,
    *,
    costs: CostModel,
    init_cash: float = 10_000.0,
    min_rebalance: float = 0.0,
    segment_starts: Mapping[str, pd.DatetimeIndex] | None = None,
) -> PortfolioResult:
    """Equal-weight every member on the schedule, through :func:`run_portfolio`."""
    return run_portfolio(
        candles_by_symbol,
        equal_weight_weights(members, schedule),
        costs=costs,
        init_cash=init_cash,
        min_rebalance=min_rebalance,
        segment_starts=segment_starts,
    )


def btc_vol_target_returns(
    btc_candles: pd.DataFrame,
    index: pd.DatetimeIndex,
    interval: str,
    *,
    costs: CostModel,
    target_annual: float,
    lookback_days: float,
    max_leverage: float,
) -> pd.Series:
    """Net returns of vol-targeted buy-and-hold BTC, reindexed to the portfolio calendar
    ``index``. A calendar row with no BTC bar gets return 0 (equity unchanged); a BTC bar not
    in ``index`` is a ValueError, since its return would be lost."""
    extra = btc_candles.index.difference(index)
    if len(extra):
        raise ValueError(f"btc_candles has {len(extra)} bar(s) outside the portfolio index")
    res = vol_target_buy_and_hold(
        btc_candles,
        interval,
        costs=costs,
        target_annual=target_annual,
        lookback_days=lookback_days,
        max_leverage=max_leverage,
    )
    return res.returns.reindex(index, fill_value=0.0).rename("returns")


def annual_turnover(turnover: pd.Series, interval: str) -> float:
    """Mean traded notional per year as a multiple of equity (sum of the engine's per-bar
    turnover, scaled by bars per year over the bars covered)."""
    if len(turnover) == 0:
        return float("nan")
    return float(turnover.sum()) * bars_per_year(interval) / len(turnover)


@dataclass(frozen=True)
class RandomSelectionNull:
    sharpe: np.ndarray  # one annualised net Sharpe per path (NaN if undefined)
    total_return: np.ndarray
    turnover: np.ndarray  # annual turnover per path (x equity)
    n_paths: int
    seed: int

    def p_value(self, strategy_sharpe: float) -> float:
        """Share of paths whose Sharpe is >= the strategy's, counting the strategy itself
        ((b + 1) / (n + 1), so never 0). NaN paths count as not beating it."""
        if not np.isfinite(strategy_sharpe):
            return float("nan")
        beat = int(np.sum(np.nan_to_num(self.sharpe, nan=-np.inf) >= strategy_sharpe))
        return (beat + 1) / (self.n_paths + 1)


def _null_weights(
    picks: np.ndarray, total: float, vol_row: np.ndarray | None, max_weight: float
) -> np.ndarray:
    raw = np.ones(len(picks)) if vol_row is None else 1.0 / vol_row[picks]
    w = raw / raw.sum() * total
    return np.minimum(w, max_weight)


_TERCILES = (
    3  # volatility buckets: the top quintile of 30 coins picks ~6, so finer buckets go empty
)


def _vol_buckets(vol_row: np.ndarray, pool: np.ndarray) -> np.ndarray:
    """Tercile 0..2 by volatility among pool names with finite vol; -1 elsewhere."""
    out = np.full(len(vol_row), -1)
    with np.errstate(invalid="ignore"):
        ok = np.flatnonzero(pool & np.isfinite(vol_row))
    if len(ok):
        order = ok[np.argsort(vol_row[ok], kind="stable")]
        out[order] = np.arange(len(order)) * _TERCILES // len(order)
    return out


def _draw_fresh(
    rng: np.random.Generator,
    fresh_pool: np.ndarray,
    need: int,
    want: np.ndarray | None,
    buckets: np.ndarray | None,
) -> np.ndarray:
    """``need`` names from ``fresh_pool``; with ``buckets``, first ``want[b]`` from each tercile
    b, then any shortfall from the rest of the pool."""
    if buckets is None or want is None:
        return rng.choice(fresh_pool, size=need, replace=False)
    picked: list[int] = []
    for b in range(_TERCILES):
        cand = fresh_pool[buckets[fresh_pool] == b]
        take = min(int(want[b]), len(cand), need - len(picked))
        if take > 0:
            picked += rng.choice(cand, size=take, replace=False).tolist()
    rest = np.setdiff1d(fresh_pool, picked)
    short = min(need - len(picked), len(rest))
    if short > 0:
        picked += rng.choice(rest, size=short, replace=False).tolist()
    return np.asarray(picked, dtype=int)


def random_selection_weights(
    strategy_weights: pd.DataFrame,
    members: pd.DataFrame,
    rng: np.random.Generator,
    *,
    inverse_vol: pd.DataFrame | None = None,
    max_weight: float = 1.0,
    vol_panel: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One null path (see the module docstring). On each strategy decision row with k names,
    of which ``kept`` were also in the strategy's previous decision: keep ``kept`` random names
    of the null's own previous picks that are still eligible, then draw the rest from eligible
    names it did not hold (falling back to any eligible name). Weights follow the strategy's
    rule: equal, or proportional to 1/vol when ``inverse_vol`` is given (then only names with
    finite vol > 0 are eligible, as in ``top_quantile_weights``), scaled to the strategy's row
    sum and capped at ``max_weight``. ``vol_panel`` (default: ``inverse_vol``) is a trailing
    volatility panel known at each row; with it, fresh names match the strategy's fresh names'
    counts per volatility tercile of the eligible pool. NaN rows stay NaN (hold)."""
    if not 0 < max_weight <= 1:
        raise ValueError(f"max_weight must be in (0, 1], got {max_weight!r}")
    w = strategy_weights.to_numpy(dtype=float)
    m = members.to_numpy(dtype=bool)
    sizing = None
    if inverse_vol is not None:
        _check_panel("inverse_vol", inverse_vol, like=strategy_weights)
        sizing = inverse_vol.to_numpy(dtype=float)
        with np.errstate(invalid="ignore"):
            m = m & np.isfinite(sizing) & (sizing > 0)
    if vol_panel is not None:
        _check_panel("vol_panel", vol_panel, like=strategy_weights)
        bucket_vol = vol_panel.to_numpy(dtype=float)
    else:
        bucket_vol = sizing
    out = np.full_like(w, np.nan)
    prev_strategy = np.zeros(w.shape[1], dtype=bool)
    prev_null = np.zeros(w.shape[1], dtype=bool)
    for i in np.flatnonzero(~np.isnan(w).all(axis=1)):
        row = np.nan_to_num(w[i], nan=0.0)
        held = row > 0
        k = int(held.sum())
        kept = int((held & prev_strategy).sum())
        fresh_strategy = np.flatnonzero(held & ~prev_strategy)
        out[i] = 0.0
        prev_strategy = held
        pool = m[i]
        if k == 0 or not pool.any():
            prev_null = np.zeros_like(prev_null)
            continue
        buckets = want = None
        if bucket_vol is not None:
            buckets = _vol_buckets(bucket_vol[i], pool | held)
            want = np.bincount(
                buckets[fresh_strategy][buckets[fresh_strategy] >= 0], minlength=_TERCILES
            )
        survivors = np.flatnonzero(prev_null & pool)
        keep = rng.choice(survivors, size=min(kept, len(survivors)), replace=False)
        fresh_pool = np.flatnonzero(pool & ~prev_null)
        need = min(k - len(keep), len(fresh_pool))
        fresh = _draw_fresh(rng, fresh_pool, need, want, buckets)
        picks = np.concatenate([keep, fresh]).astype(int)
        short = k - len(picks)
        if short > 0:  # not enough unheld names: top up from the null's other old names
            rest = np.setdiff1d(np.flatnonzero(pool), picks)
            extra = rng.choice(rest, size=min(short, len(rest)), replace=False)
            picks = np.concatenate([picks, extra]).astype(int)
        total = min(float(row.sum()), 1.0)
        out[i, picks] = _null_weights(
            picks, total, None if sizing is None else sizing[i], max_weight
        )
        prev_null = np.zeros_like(prev_null)
        prev_null[picks] = True
    return pd.DataFrame(out, index=strategy_weights.index, columns=strategy_weights.columns)


def random_selection_null(
    candles_by_symbol: Mapping[str, pd.DataFrame],
    strategy_weights: pd.DataFrame,
    members: pd.DataFrame,
    interval: str,
    *,
    costs: CostModel,
    n_paths: int,
    seed: int,
    inverse_vol: pd.DataFrame | None = None,
    max_weight: float = 1.0,
    vol_panel: pd.DataFrame | None = None,
    init_cash: float = 10_000.0,
    min_rebalance: float = 0.0,
    segment_starts: Mapping[str, pd.DatetimeIndex] | None = None,
) -> RandomSelectionNull:
    """Sharpe, total return and annual turnover of ``n_paths`` random-selection portfolios
    (module docstring). Pass the strategy's own ``members`` mask, weighting inputs, a trailing
    ``vol_panel`` and engine settings so only the choice of coins differs."""
    _check_panel("strategy_weights", strategy_weights)
    _check_members(members)
    _check_panel("members", members, like=strategy_weights)
    if isinstance(n_paths, bool) or not isinstance(n_paths, int) or n_paths < 1:
        raise ValueError(f"n_paths must be an int >= 1, got {n_paths!r}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed must be an int, got {seed!r}")
    rng = np.random.default_rng(seed)
    paths = [
        random_selection_weights(
            strategy_weights,
            members,
            rng,
            inverse_vol=inverse_vol,
            max_weight=max_weight,
            vol_panel=vol_panel,
        )
        for _ in range(n_paths)
    ]
    runs = net_returns_for(
        candles_by_symbol,
        paths,
        costs=costs,
        init_cash=init_cash,
        min_rebalance=min_rebalance,
        segment_starts=segment_starts,
    )
    metrics = [metrics_from_returns(r.returns, interval) for r in runs]
    return RandomSelectionNull(
        sharpe=np.array([m.sharpe for m in metrics], dtype=float),
        total_return=np.array([m.total_return for m in metrics], dtype=float),
        turnover=np.array([annual_turnover(r.turnover, interval) for r in runs], dtype=float),
        n_paths=n_paths,
        seed=seed,
    )
