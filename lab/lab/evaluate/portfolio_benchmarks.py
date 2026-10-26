"""Benchmarks for multi-coin portfolio strategies (PLAN.md Phase 10).

A cross-sectional strategy must beat three things, all through the same engine and costs
(including the optional participation ``impact``: pass the strategy's own
:class:`lab.engine.costs.ImpactModel` so the strategy and its benchmarks pay the same cost model):

1. **Equal-weight universe** (:func:`equal_weight_universe`): every eligible member, equal
   weight, rebalanced on the strategy's schedule. Separates stock-picking from simply owning the
   liquid crypto market.
2. **Vol-targeted BTC** (:func:`btc_vol_target_returns`): the lab's single-asset benchmark,
   reindexed to the portfolio calendar, so a portfolio result is comparable with every earlier run.
   It is single-symbol and never pays participation impact (BTC/ETH costs unchanged).
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

**Liquidity cap** (``liquidity_share`` + ``liquidity_cap_mult``, both or neither, the same
values the strategy's :func:`lab.registry.cross_section.top_quantile_weights` used). A capped
strategy puts little weight on illiquid coins and so pays little participation impact; a
benchmark that ignored the cap would hold illiquid coins at full weight, pay far more impact
and hand a no-skill strategy a fake edge. So both benchmarks replicate the strategy's
procedure rather than its post-cap totals:

* Equal weight: ``1/k`` over the ``k`` members with a finite liquidity share ``> 0``, each
  capped at ``mult * share[t, coin]``; the excess stays in cash (not redistributed).
* Random null, on each strategy decision row ``t`` (call the strategy's names there ``H``):

  1. Draw pool: members with a finite ``share > 0`` (and finite ``inverse_vol > 0`` when
     weighting by inverse vol), exactly the names the strategy could rank. Breadth (``|H|``
     names), retention (as many of the null's own previous names kept as the strategy kept)
     and volatility terciles are matched as without the cap.
  2. Pre-cap total ``T``: the strategy's weights before capping. Under the weighting rule
     (``raw = 1`` or ``1/vol``) an uncapped strategy name ``j`` has
     ``w[t, j] = T * raw_j / sum(raw over H)``, so ``T`` is read off the uncapped names:
     ``T = min(1, max over uncapped j in H of w[t, j] * sum(raw) / raw_j)``. A name counts
     as capped when ``w[t, j] >= (1 - 1e-9) * min(max_weight, mult * share[t, j])``. If every
     name in ``H`` is capped (or none can be sized), ``T = 1``: the rule of
     ``top_quantile_weights``, whose pre-cap weights always sum to 1.
  3. The null's picks get ``T * raw / sum(raw)`` and each is capped at
     ``min(max_weight, mult * share[t, pick])``; the excess stays in cash.

  So the null matches the strategy's dates, breadth, retention, weighting rule, volatility
  terciles and *pre-cap* total, and its post-cap exposure (how much the caps bite) follows
  from the liquidity of the names it drew, just as the strategy's does from its own. Exposure
  is therefore no longer matched row by row: :class:`RandomSelectionNull` reports the mean
  decision-row exposure of the strategy and of each path side by side (``strategy_exposure``,
  ``exposure``). Without the cap every rule above is the unchanged original: the null is
  scaled to the strategy's post-cap row sum and capped at ``max_weight`` only.

Pure: no I/O, no prints, no module state; randomness only through an explicit seed.
Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from lab.engine.costs import ImpactModel
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


def _check_liquidity(
    liquidity_share: pd.DataFrame | None,
    liquidity_cap_mult: float | None,
    like: pd.DataFrame,
    like_name: str,
) -> tuple[np.ndarray | None, float]:
    """``(share array, mult)``, or ``(None, 0.0)`` when the cap is off. Same rules as
    ``top_quantile_weights``: both or neither, ``share`` aligned with ``like``, ``mult``
    finite and > 0."""
    if (liquidity_share is None) != (liquidity_cap_mult is None):
        raise ValueError("liquidity_share and liquidity_cap_mult must be given together")
    if liquidity_share is None:
        return None, 0.0
    if not isinstance(liquidity_share, pd.DataFrame):
        raise ValueError(
            f"liquidity_share must be a DataFrame, got {type(liquidity_share).__name__}"
        )
    if not liquidity_share.index.equals(like.index) or list(liquidity_share.columns) != list(
        like.columns
    ):
        raise ValueError(f"liquidity_share must have the same index and columns as {like_name}")
    try:
        share = liquidity_share.to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("liquidity_share must hold numbers only") from exc
    if isinstance(liquidity_cap_mult, bool) or not isinstance(
        liquidity_cap_mult, int | float | np.number
    ):
        raise ValueError(f"liquidity_cap_mult must be a number > 0, got {liquidity_cap_mult!r}")
    mult = float(liquidity_cap_mult)
    if not (np.isfinite(mult) and mult > 0.0):
        raise ValueError(f"liquidity_cap_mult must be finite and > 0, got {liquidity_cap_mult!r}")
    return share, mult


def _sizable_share(share: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return np.isfinite(share) & (share > 0)


def equal_weight_weights(
    members: pd.DataFrame,
    schedule: pd.Series,
    *,
    liquidity_share: pd.DataFrame | None = None,
    liquidity_cap_mult: float | None = None,
) -> pd.DataFrame:
    """Decision rows (``schedule`` True): 1/k on each of the k members, 0 elsewhere (all 0 if
    k = 0). Other rows all NaN (hold).

    With ``liquidity_share`` and ``liquidity_cap_mult`` (both or neither, as in
    ``top_quantile_weights``): only members with a finite share ``> 0`` count (k is their
    number), and each weight is capped at ``liquidity_cap_mult * share[t, member]``; the
    excess stays in cash, not redistributed."""
    _check_members(members)
    if not isinstance(schedule, pd.Series) or not schedule.index.equals(members.index):
        raise ValueError("schedule must be a bool Series on members.index")
    if not pd.api.types.is_bool_dtype(schedule):
        raise ValueError(f"schedule must be bool, got dtype {schedule.dtype}")
    share, mult = _check_liquidity(liquidity_share, liquidity_cap_mult, members, "members")
    m = members.to_numpy(dtype=bool)
    if share is not None:
        m = m & _sizable_share(share)
    k = m.sum(axis=1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(k > 0, m / np.maximum(k, 1), 0.0)
    if share is not None:
        w = np.where(m, np.minimum(w, mult * np.where(m, share, 0.0)), 0.0)
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
    impact: ImpactModel | None = None,
    liquidity_share: pd.DataFrame | None = None,
    liquidity_cap_mult: float | None = None,
) -> PortfolioResult:
    """Equal-weight every member on the schedule, through :func:`run_portfolio` (same
    ``costs``, ``impact`` and liquidity cap as the strategy; see
    :func:`equal_weight_weights`)."""
    return run_portfolio(
        candles_by_symbol,
        equal_weight_weights(
            members,
            schedule,
            liquidity_share=liquidity_share,
            liquidity_cap_mult=liquidity_cap_mult,
        ),
        costs=costs,
        init_cash=init_cash,
        min_rebalance=min_rebalance,
        segment_starts=segment_starts,
        impact=impact,
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
    # Mean over the strategy's decision rows of each path's decision-row weight sum (the
    # invested share it asked for); with a liquidity cap this differs from the strategy's.
    exposure: np.ndarray = field(default_factory=lambda: np.empty(0))
    strategy_exposure: float = float("nan")  # the same mean for the strategy's own weights

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


_CAPPED_TOL = 1e-9  # a weight within this relative distance of its cap counts as capped


def _pre_cap_total(
    row: np.ndarray, vol_row: np.ndarray | None, limit_row: np.ndarray, held: np.ndarray
) -> float:
    """The strategy's pre-cap total ``T`` on one decision row (module docstring, step 2)."""
    idx = np.flatnonzero(held)
    raw = np.ones(len(idx)) if vol_row is None else 1.0 / vol_row[idx]
    w = row[idx]
    with np.errstate(divide="ignore", invalid="ignore"):
        implied = w * raw.sum() / raw
        free = w < limit_row[idx] * (1.0 - _CAPPED_TOL)
    ok = free & np.isfinite(implied)
    if not ok.any():
        return 1.0
    return min(float(implied[ok].max()), 1.0)


def _decision_exposure(weights: np.ndarray, rows: np.ndarray) -> float:
    if rows.size == 0:
        return float("nan")
    return float(np.nan_to_num(weights[rows], nan=0.0).sum(axis=1).mean())


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
    liquidity_share: pd.DataFrame | None = None,
    liquidity_cap_mult: float | None = None,
) -> pd.DataFrame:
    """One null path (see the module docstring). On each strategy decision row with k names,
    of which ``kept`` were also in the strategy's previous decision: keep ``kept`` random names
    of the null's own previous picks that are still eligible, then draw the rest from eligible
    names it did not hold (falling back to any eligible name). Weights follow the strategy's
    rule: equal, or proportional to 1/vol when ``inverse_vol`` is given (then only names with
    finite vol > 0 are eligible, as in ``top_quantile_weights``), scaled to the strategy's row
    sum and capped at ``max_weight``. ``vol_panel`` (default: ``inverse_vol``) is a trailing
    volatility panel known at each row; with it, fresh names match the strategy's fresh names'
    counts per volatility tercile of the eligible pool. With ``liquidity_share`` and
    ``liquidity_cap_mult`` (both or neither; the strategy's own values), only names with a
    finite share > 0 are drawn, the pre-cap total is the strategy's, and each pick is capped at
    ``min(max_weight, mult * share)`` with the excess in cash (module docstring, steps 1-3).
    NaN rows stay NaN (hold)."""
    if not 0 < max_weight <= 1:
        raise ValueError(f"max_weight must be in (0, 1], got {max_weight!r}")
    share, mult = _check_liquidity(
        liquidity_share, liquidity_cap_mult, strategy_weights, "the strategy weights"
    )
    w = strategy_weights.to_numpy(dtype=float)
    m = members.to_numpy(dtype=bool)
    if share is not None:
        m = m & _sizable_share(share)
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
        vol_row = None if sizing is None else sizing[i]
        if share is None:
            total = min(float(row.sum()), 1.0)
            out[i, picks] = _null_weights(picks, total, vol_row, max_weight)
        else:
            limit = np.minimum(max_weight, mult * share[i])  # NaN where share is NaN
            total = _pre_cap_total(row, vol_row, limit, held)
            out[i, picks] = np.minimum(_null_weights(picks, total, vol_row, 1.0), limit[picks])
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
    impact: ImpactModel | None = None,
    liquidity_share: pd.DataFrame | None = None,
    liquidity_cap_mult: float | None = None,
) -> RandomSelectionNull:
    """Sharpe, total return, annual turnover and mean decision exposure of ``n_paths``
    random-selection portfolios (module docstring). Pass the strategy's own ``members`` mask,
    weighting inputs, a trailing ``vol_panel``, liquidity cap and engine settings (``costs``
    and ``impact`` included) so only the choice of coins differs."""
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
            liquidity_share=liquidity_share,
            liquidity_cap_mult=liquidity_cap_mult,
        )
        for _ in range(n_paths)
    ]
    sw = strategy_weights.to_numpy(dtype=float)
    rows = np.flatnonzero(~np.isnan(sw).all(axis=1))
    runs = net_returns_for(
        candles_by_symbol,
        paths,
        costs=costs,
        init_cash=init_cash,
        min_rebalance=min_rebalance,
        segment_starts=segment_starts,
        impact=impact,
    )
    metrics = [metrics_from_returns(r.returns, interval) for r in runs]
    return RandomSelectionNull(
        sharpe=np.array([m.sharpe for m in metrics], dtype=float),
        total_return=np.array([m.total_return for m in metrics], dtype=float),
        turnover=np.array([annual_turnover(r.turnover, interval) for r in runs], dtype=float),
        n_paths=n_paths,
        seed=seed,
        exposure=np.array(
            [_decision_exposure(p.to_numpy(dtype=float), rows) for p in paths], dtype=float
        ),
        strategy_exposure=_decision_exposure(sw, rows),
    )
