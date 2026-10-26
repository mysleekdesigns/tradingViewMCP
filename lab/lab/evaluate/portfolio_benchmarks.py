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
   credited as selection skill. Given a trailing dollar-volume panel (``liquidity_panel``),
   every name the null replaces (fresh and kept) is drawn from a narrow band of liquidity
   ranks around the strategy name it stands in for, so a tilt toward illiquid small caps
   (different drift, different costs) is not credited as skill either, including a tilt to
   the very least liquid coins (see **Nearest-rank liquidity matching** below). Only the
   choice of coins within those constraints differs. Mean turnover of both is reported side
   by side.

**Volatility terciles** (no ``liquidity_panel``: the default and the volatility-only call).
On each decision row every name in the draw pool or held by the strategy gets a volatility
tercile 0..2 by rank within that set (stable on column order; tercile 0 = least volatile; a
non-finite value gets no tercile). Each of the strategy's fresh names is matched, greedily, by
one null draw without replacement from the names the null is not holding: first from the same
volatility tercile (level ``vol``; buckets in ascending order, each taking ``min(unmatched
strategy names in the bucket, free names in the bucket, draws still needed)``), then from any
remaining name (``pool``; also any fresh draw the null needs beyond the strategy's fresh names
and any top-up from its previous names). Retention is a uniform draw from the null's
survivors. ``match_fallbacks`` counts the draws per level (``MATCH_LEVELS`` keys; ``joint``
and ``liquidity`` stay 0 here). This path is bit-identical to the a5fa079 null, so every
result recorded with it reproduces.

**Nearest-rank liquidity matching** (``liquidity_panel`` given; the portfolio runner always
passes it). On each decision row ``t`` the names in the draw pool or held by the strategy that
have a finite liquidity value at row ``t`` get a liquidity rank ``0..n-1`` (ascending, 0 =
least liquid; ties keep column order, so ranks are unique; only the panel's row ``t`` is read,
so a dollar-volume or a liquidity-share panel gives the same draws). Then:

1. Targets: the strategy's *kept* names (held on its previous decision row and this one)
   are matched first, by the null's own survivors (its previous picks still in the pool;
   ``min(kept, survivors)`` of them); then its *fresh* names, by names in the pool the null
   is not holding. Within each group the targets are processed in a uniformly random order
   from the path's RNG (so neither end of the liquidity range is systematically served
   first and starved of nearby names). When the null has fewer survivors than the strategy
   kept names (membership churn: some of its old names left the pool), the kept names left
   over join the fresh targets, so every strategy name still gets a band-matched stand-in
   instead of a uniform pool draw (package H4). Only when the null can place fewer names
   than the strategy has are some targets left unmatched.
2. Band: for a target of rank ``r``, the candidates are the still-free names (not drawn by
   the null on this row yet) with a liquidity rank in ``[r - h, r + h]``, ``h =
   (liquidity_window - 1) // 2`` (``liquidity_window`` is odd: the band holds at most
   ``liquidity_window`` rank positions centred on ``r``). The band is clipped at the ends of
   the ranking, not shifted inward, so a target at the least liquid end is matched from the
   least liquid names only (a shifted band would pull the null toward the middle; synthetic
   probe: twice the premium gap). The band may contain the strategy's own name: the pool is
   the eligible set, as before (see **Overlap**). If no free name is in the band, the
   candidates widen to the free ranked name(s) at the smallest rank gap (count ``widened``);
   a band with some but fewer than ``liquidity_window`` free names counts ``narrow`` (names
   taken by the null, or the band clipped at an end of the ranking).
3. Volatility as a secondary preference (when a volatility panel or ``inverse_vol`` is
   given): the candidates in the target's volatility tercile (terciles as above). The draw
   is restricted to them only when they include at least ``VOL_PREFERENCE_MIN_OTHERS`` (2)
   names *other than the target itself* (``vol_met``); with fewer it is from every candidate
   (``vol_thin``), as it is when the target has no volatility tercile (``vol_missed``).
   Liquidity is never given up for volatility. Why the threshold (package H4): inside a
   5-rank band the target's tercile usually holds the target and about one other name, so
   always restricting (the H3 rule) made the null copy the strategy's own name on about
   half its fresh draws and halved its power (**Overlap**). With the threshold the
   restriction can only leave the target as one of at least three names.
4. Draw one candidate uniformly at random (count ``rank``; the |rank gap| goes into
   ``rank_gap``). A target with no rank, or a row with no free ranked name, draws uniformly
   from the free names (``pool``); so do any draws beyond the targets and any top-up from
   the null's other old names. So the rule never fails and never draws a name twice.

``match_fallbacks`` (fresh draws) and ``kept_match_fallbacks`` (kept names) hold these
counts (``RANK_MATCH_KEYS``; ``rank + pool`` = every draw, the others are sub-counts of
``rank``), summed over rows and paths. :class:`RandomSelectionNull`'s ``rank_gap`` is the mean
|liquidity rank gap| between each draw and its target. Why a band of ranks and not liquidity
terciles (package H2): terciles only match *between* buckets, so a no-skill strategy leaning to
the very least liquid coins *within* the bottom tercile earned more of a small-cap premium than
its tercile-matched null, and on long histories (1,200-2,400 daily bars) the p-values of such a
strategy over-rejected. Why a band and not "the ``w`` nearest free names": with ~5 survivors a
5-name window holds every survivor, so retention became uniform again, and a fresh window
pushed outward whenever the null already held the nearby names (synthetic probe: worse than
terciles).

**Kept names**. Without ``liquidity_panel``, retention keeps the original uniform draw from the
survivors (above). With it the null's kept names are matched to the strategy's kept names by
the band rule: a strategy keeps a name *because* of the persistent part of its score, and when
that part is a liquidity tilt (a score is usually persistent: a 28-day momentum score barely
moves in a week) its kept names sit deeper in the tilt than its fresh names, so a null that
kept its own names uniformly at random would hold a shallower book (H2).

**Overlap** (``overlap``, ``overlap_chance``). On each row the null draws on, the share of its
held names that the strategy also holds, averaged over rows and paths, beside the share a
uniform draw of the same breadth from the pool would have (``|held & pool| / |pool|``). A
tight band lets the null copy the strategy's own names (the band always contains the target
when the null does not hold it, and a copied name tends to be kept while the strategy keeps
it); the more it copies, the less a real selection skill can show against it, so a high
overlap means the test has less power, not that the strategy is more skilled. The report shows
both numbers, and the portfolio report adds a descriptive volatility-only null beside the
criterion (more powerful, less strict on liquidity). Measured power (package H4, quant
reviewer's probe): an H1-like world (30 coins, weekly top quintile of the 28-day return, a
planted causal edge ``B`` times the cross-sectional z-score of the 28-day return per day),
2,450 daily bars, 60 seeds x 100 paths, p <= 0.05 counts; in brackets the same with dollar
volume co-moving with the 28-day return. B = 0.0004 (about +4%/yr net over equal weight):
volatility-only null 30/60 (30/60), nearest rank 24/60 (26/60); under the H3 rule (step 3
without the threshold) 16/60 (11/60). B = 0.0002: 11 (11), 11 (12), H3 5 (7). No skill:
5 (5), 3 (4), H3 1 (4). P(a fresh null draw is one of the strategy's names): volatility-only
0.25, nearest rank 0.36, H3 0.54 (no preference at all: 0.34, power 26/60 (25/60)). So the
liquidity-matched null still costs some power against a real selection skill (an eighth to a
fifth fewer rejections at B = 0.0004); the volatility-only sidebar shows how much.

**Window** (``liquidity_window``, ``lab.toml [portfolio] null_liquidity_window``, default 5).
Calibration probe (package H4 rule): 2,400 daily bars, 30 coins, a no-skill AR(0.95) score
leaning to the least liquid coins with a premium of 0.2% a day on the 5 least liquid only, 40
paths, two 80-seed blocks (seeds 3000-3079 / 3100-3179), p <= 0.05 counts of 80 and mean p:
3 ranks 2 / 9 (0.50 / 0.46), 5 ranks 7 / 9 (0.46 / 0.45), 7 ranks 6 / 10 (0.41 / 0.40);
zero-premium control at 5 ranks 4 / 4 (0.54 / 0.53). Power (Overlap probe, B = 0.0004, p <=
0.05 of 60, plain / co-moving dollar volume): 3 ranks 20 / 21, 5 ranks 24 / 26, 7 ranks 25 /
25. So 5 keeps most of 7's power and lets less of a concentrated small-cap premium through
(p <= 0.10: 24 of 160 at 5, 32 at 7, nominal 16); 3 is a little better calibrated in that
extreme case and loses power. Residual: in that case the band, clipped at the least liquid
end, still lets part of the premium through (16 of 160 at p <= 0.05 against 8 for the
zero-premium control; H3's copying rule 11). In a volatility-tilt probe (no skill, 20
driftless coins with 1-7% daily vol, so 28-day momentum leans to volatile coins; 1,200 bars,
80 seeds, 40 paths, no banding or cap) the null is calibrated when illiquid coins are the
volatile ones (5 / 80 at p <= 0.05, 13 at 0.10; volatility-only null 5 and 11) but
over-rejects somewhat when liquidity is unrelated to volatility (10 / 80 and 16 / 80): a 5-rank
band then seldom holds two other names of the target's tercile, so volatility is often
unmatched (no preference at all: 9 and 12; H3's rule 2 and 7, by copying). In an H1-like world
WITH banding and the liquidity cap (30 coins, weekly 28-day momentum top quintile, exit band
0.4, cap x10, liquidity-vol correlation 0.5, 2,000 bars, 80 seeds) the preference applies on
only ~23% of draws, and when volatile coins earn a sigma^2/2 premium a no-skill book is
rejected 9 / 80 at p <= 0.05 (H3's rule 2-3, volatility-only 5); without that premium it is
calibrated (4 / 80). H3's rule is not the cure: against a +4%/yr edge it rejects 6 / 80
against 14 for this rule and 13 for volatility-only. So read a pass here together with the
volatility-only sidebar and ``book_mix``: a strategy whose top-volatility-tercile share exceeds
the null's by more than 0.05 while the sidebar's p > 0.05 has a volatility tilt not excluded.

**Book mix** (``book_mix``): on each decision row the null draws on, the share of the held
names in each liquidity and each volatility tercile (count-weighted and weight-weighted, among
the held names that have a tercile), averaged over those rows, for the strategy and for the
null (mean over paths), so a reader can check the null's whole book, not just its fresh draws,
matches the strategy's. Volatility terciles there are by ``vol_panel`` (or by ``1 / inverse_vol``
when only that is given): tercile 0 = least volatile.

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
     and volatility terciles (and, given ``liquidity_panel``, liquidity ranks) are matched as
     without the cap.
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
  terciles (and, given ``liquidity_panel``, liquidity ranks) and *pre-cap* total, and its post-cap
  exposure (how much the caps bite) follows from the liquidity of the names it drew, just as
  the strategy's does from its own. Exposure
  is therefore no longer matched row by row: :class:`RandomSelectionNull` reports the mean
  decision-row exposure of the strategy and of each path side by side (``strategy_exposure``,
  ``exposure``). Without the cap every rule above is the unchanged original: the null is
  scaled to the strategy's post-cap row sum and capped at ``max_weight`` only.

Pure: no I/O, no prints, no module state; randomness only through an explicit seed.
Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
    # Fresh draws per match outcome, summed over rows and paths (module docstring, **Volatility
    # terciles** / **Nearest-rank liquidity matching**: keys ``MATCH_LEVELS`` without a
    # liquidity panel, ``RANK_MATCH_KEYS`` with one). Empty when not computed.
    match_fallbacks: dict[str, int] = field(default_factory=dict)
    # Kept names per match outcome (``RANK_MATCH_KEYS``), summed over rows and paths (module
    # docstring, **Kept names**); empty when retention is the legacy uniform draw.
    kept_match_fallbacks: dict[str, int] = field(default_factory=dict)
    # {"strategy": {...}, "null": {...}} tercile shares of the held book (module docstring,
    # **Book mix**; keys ``"<panel>_<count|weight>"``, three shares each). Empty when not computed.
    book_mix: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    # Mean over paths of each path's mean share of its held names the strategy also holds on
    # the row (module docstring, **Overlap**), and the same share for a uniform draw from the
    # row's pool (``k / pool``, the chance level). NaN when not computed.
    overlap: float = float("nan")
    overlap_chance: float = float("nan")
    # Mean |liquidity rank gap| between each nearest-rank draw and its strategy name (mean over
    # paths of each path's mean; module docstring, **Nearest-rank liquidity matching**). NaN
    # without a liquidity panel.
    rank_gap: float = float("nan")

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

# Tercile levels of :func:`_draw_fresh`, in fallback order. The volatility-only path uses
# ``vol`` and ``pool`` (module docstring, **Volatility terciles**); ``joint`` and ``liquidity``
# are package H2's liquidity-tercile rule, superseded by nearest-rank matching and kept only so
# the calibration tests can compare the two.
MATCH_LEVELS = ("joint", "liquidity", "vol", "pool")


def _tercile_buckets(values: np.ndarray, pool: np.ndarray) -> np.ndarray:
    """Tercile 0..2 by ``values`` (ascending) among pool names with a finite value; -1
    elsewhere. Ties keep column order."""
    out = np.full(len(values), -1)
    with np.errstate(invalid="ignore"):
        ok = np.flatnonzero(pool & np.isfinite(values))
    if len(ok):
        order = ok[np.argsort(values[ok], kind="stable")]
        out[order] = np.arange(len(order)) * _TERCILES // len(order)
    return out


def _draw_fresh(
    rng: np.random.Generator,
    fresh_pool: np.ndarray,
    need: int,
    targets: np.ndarray,
    vol_b: np.ndarray | None,
    liq_b: np.ndarray | None,
    counts: dict[str, int],
) -> np.ndarray:
    """``need`` distinct names from ``fresh_pool`` (sorted), matched to the strategy's names
    ``targets`` level by level (``MATCH_LEVELS``: joint, liquidity, vol, pool; each level
    skipped when its terciles are not given; module docstring, **Volatility terciles**); adds
    the number placed at each level to ``counts``. The null calls it with volatility terciles
    only, for its fresh draws (pool = unheld names, targets = the strategy's fresh names);
    with ``liq_b`` it is package H2's tercile rule, which the tests call for both fresh and
    kept names to compare it with nearest-rank matching."""
    avail = fresh_pool
    picked: list[int] = []
    levels: list[tuple[str, list[np.ndarray]]] = []
    if vol_b is not None and liq_b is not None:
        levels.append(("joint", [vol_b, liq_b]))
    if liq_b is not None:
        levels.append(("liquidity", [liq_b]))
    if vol_b is not None:
        levels.append(("vol", [vol_b]))
    pending = targets
    for name, panels in levels:
        keys = np.stack([b[pending] for b in panels], axis=1) if len(pending) else None
        if keys is None:
            break
        ok = (keys >= 0).all(axis=1)
        placed = np.zeros(len(pending), dtype=bool)
        for key in np.unique(keys[ok], axis=0):
            room = need - len(picked)
            if room <= 0:
                break
            mine = np.flatnonzero(ok & (keys == key).all(axis=1))
            in_bucket = np.ones(len(avail), dtype=bool)
            for b, k in zip(panels, key, strict=True):
                in_bucket &= b[avail] == k
            cand = avail[in_bucket]
            take = min(len(mine), len(cand), room)
            if take > 0:
                chosen = rng.choice(cand, size=take, replace=False)
                picked += chosen.tolist()
                avail = np.setdiff1d(avail, chosen)
                placed[mine[:take]] = True
                counts[name] += take
        pending = pending[~placed]
    short = min(need - len(picked), len(avail))
    if short > 0:
        picked += rng.choice(avail, size=short, replace=False).tolist()
        counts["pool"] += short
    return np.asarray(picked, dtype=int)


RANK_MATCH_KEYS = ("rank", "pool", "widened", "narrow", "vol_met", "vol_thin", "vol_missed")
"""Counts of the nearest-rank matcher (module docstring, **Nearest-rank liquidity matching**):
``rank`` + ``pool`` = every draw; ``widened``, ``narrow``, ``vol_met``, ``vol_thin`` and
``vol_missed`` are sub-counts of ``rank`` (with a volatility panel, ``vol_met + vol_thin +
vol_missed = rank``)."""


def _liquidity_ranks(values: np.ndarray, ranked: np.ndarray) -> np.ndarray:
    """Rank 0..n-1 by ``values`` (ascending: 0 = least liquid) among ``ranked`` names with a
    finite value; -1 elsewhere. Ties keep column order, so ranks are unique. (The liquidity
    tercile of a name is ``rank * 3 // n``, exactly :func:`_tercile_buckets`.)"""
    out = np.full(len(values), -1)
    with np.errstate(invalid="ignore"):
        ok = np.flatnonzero(ranked & np.isfinite(values))
    if len(ok):
        out[ok[np.argsort(values[ok], kind="stable")]] = np.arange(len(ok))
    return out


class _GapAcc:
    """Running sum of |liquidity rank gap| over one path's nearest-rank draws."""

    def __init__(self) -> None:
        self.total = 0
        self.n = 0

    def mean(self) -> float:
        return self.total / self.n if self.n else float("nan")


@dataclass(frozen=True)
class _RankMatch:
    """One decision row's liquidity ranks, the window size and the path's gap accumulator."""

    ranks: np.ndarray
    window: int
    gaps: _GapAcc


def _match_nearest(
    rng: np.random.Generator,
    avail: np.ndarray,
    need: int,
    targets: np.ndarray,
    vol_b: np.ndarray | None,
    match: _RankMatch,
    counts: dict[str, int],
) -> tuple[np.ndarray, np.ndarray]:
    """``need`` distinct names from ``avail`` (sorted), each strategy name in ``targets`` matched
    by liquidity rank (module docstring, **Nearest-rank liquidity matching**); adds to
    ``counts`` (``RANK_MATCH_KEYS``) and the gaps to ``match.gaps``. Used for fresh draws (avail
    = unheld names, targets = the strategy's fresh names plus any kept names the survivors
    could not match) and kept names (avail = the null's survivors, targets = the strategy's
    kept names). Returns ``(picks, unmatched targets)``: the targets left over when ``need`` is
    smaller than ``len(targets)`` (in the random order they were processed in)."""
    ranks, half = match.ranks, match.window // 2
    left = np.ones(len(avail), dtype=bool)
    picked: list[int] = []
    order = targets[rng.permutation(len(targets))] if len(targets) else targets
    for t in order[:need]:
        r = int(ranks[t])
        cand = avail[left & (ranks[avail] >= 0)]
        if r < 0 or len(cand) == 0:  # no rank to match on: any free name
            j = int(rng.choice(avail[left]))
            counts["pool"] += 1
        else:
            gap = np.abs(ranks[cand] - r)
            band = cand[gap <= half]
            if len(band) == 0:  # every name in the band is taken: the nearest free one(s)
                band = cand[gap == gap.min()]
                counts["widened"] += 1
            elif len(band) < match.window:
                counts["narrow"] += 1
            if vol_b is not None:
                band = _prefer_vol(band, t, vol_b, counts)
            j = int(rng.choice(band))
            counts["rank"] += 1
            match.gaps.total += abs(int(ranks[j]) - r)
            match.gaps.n += 1
        picked.append(j)
        left[np.searchsorted(avail, j)] = False
    short = min(need - len(picked), int(left.sum()))
    if short > 0:  # more draws than strategy names: uniform over what is left
        picked += rng.choice(avail[left], size=short, replace=False).tolist()
        counts["pool"] += short
    return np.asarray(picked, dtype=int), order[need:]


VOL_PREFERENCE_MIN_OTHERS = 2
"""The volatility-tercile preference inside the band applies only when the band holds at least
this many free names in the target's tercile *other than the target itself* (module
docstring, **Nearest-rank liquidity matching** step 3): with fewer, restricting the draw left
about two candidates, one of them the strategy's own name, so the null copied the strategy
(package H4 probe: P(fresh draw is a strategy name) 0.55)."""


def _prefer_vol(
    band: np.ndarray, target: int, vol_b: np.ndarray, counts: dict[str, int]
) -> np.ndarray:
    """The band restricted to the target's volatility tercile when that leaves at least
    ``VOL_PREFERENCE_MIN_OTHERS`` names other than the target (``vol_met``); else the whole
    band (``vol_thin``: too few such names; ``vol_missed``: the target has no tercile)."""
    if vol_b[target] < 0:
        counts["vol_missed"] += 1
        return band
    same = band[vol_b[band] == vol_b[target]]
    if int((same != target).sum()) >= VOL_PREFERENCE_MIN_OTHERS:
        counts["vol_met"] += 1
        return same
    counts["vol_thin"] += 1
    return band


def _retain(
    rng: np.random.Generator,
    survivors: np.ndarray,
    n_keep: int,
    kept_strategy: np.ndarray,
    vol_b: np.ndarray | None,
    match: _RankMatch | None,
    counts: dict[str, int],
) -> tuple[np.ndarray, np.ndarray]:
    """The ``n_keep`` names the null keeps of its ``survivors`` (module docstring, **Kept
    names**): matched to the strategy's kept names ``kept_strategy`` by nearest liquidity rank
    when a liquidity panel is given, else the legacy uniform draw (same RNG calls as the
    a5fa079 null). Also returns the strategy's kept names left unmatched (fewer survivors
    than kept names; always empty for the legacy draw), which the fresh draw then matches."""
    if match is None:
        return rng.choice(survivors, size=n_keep, replace=False), kept_strategy[:0]
    return _match_nearest(rng, survivors, n_keep, kept_strategy, vol_b, match, counts)


MIX_PANELS = ("liquidity", "vol")  # book-mix panels (module docstring, **Book mix**)
_MIX_WAYS = ("count", "weight")
_SIDES = ("strategy", "null")


class _MixAcc:
    """Running sums of per-row tercile shares (module docstring, **Book mix**)."""

    def __init__(self) -> None:
        self.total = np.zeros((len(_SIDES), len(MIX_PANELS), len(_MIX_WAYS), _TERCILES))
        self.rows = np.zeros((len(_SIDES), len(MIX_PANELS), len(_MIX_WAYS)), dtype=int)

    def add(
        self, side: int, panel: int, buckets: np.ndarray, idx: np.ndarray, w: np.ndarray
    ) -> None:
        """One row: ``idx`` the held names, ``w`` their weights (same order)."""
        b = buckets[idx]
        ok = b >= 0
        if not ok.any():
            return
        self.total[side, panel, 0] += np.bincount(b[ok], minlength=_TERCILES) / ok.sum()
        self.rows[side, panel, 0] += 1
        wsum = float(w[ok].sum())
        if wsum > 0:
            self.total[side, panel, 1] += (
                np.bincount(b[ok], weights=w[ok], minlength=_TERCILES) / wsum
            )
            self.rows[side, panel, 1] += 1

    def result(self, panels: tuple[bool, bool]) -> dict[str, dict[str, list[float]]]:
        out: dict[str, dict[str, list[float]]] = {}
        for s, side in enumerate(_SIDES):
            out[side] = {}
            for p, panel in enumerate(MIX_PANELS):
                if not panels[p]:
                    continue
                for k, way in enumerate(_MIX_WAYS):
                    n = self.rows[s, p, k]
                    share = self.total[s, p, k] / n if n else np.full(_TERCILES, np.nan)
                    out[side][f"{panel}_{way}"] = [float(x) for x in share]
        return out


@dataclass(frozen=True)
class NullPath:
    """One random-selection path: its target weights, the fresh draws per match outcome
    (``MATCH_LEVELS`` keys on the volatility-only path, ``RANK_MATCH_KEYS`` with a liquidity
    panel), the kept names per outcome (empty under the legacy uniform retention, **Kept
    names**), the book's tercile mix for the strategy and this path (**Book mix**), the mean
    overlap with the strategy's book and its chance level (**Overlap**) and the mean
    |liquidity rank gap| of the nearest-rank draws (NaN without a liquidity panel)."""

    weights: pd.DataFrame
    match_fallbacks: dict[str, int]
    kept_match_fallbacks: dict[str, int] = field(default_factory=dict)
    book_mix: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    overlap: float = float("nan")
    overlap_chance: float = float("nan")
    rank_gap: float = float("nan")


def _check_window(liquidity_window: int) -> int:
    if (
        isinstance(liquidity_window, bool)
        or not isinstance(liquidity_window, int | np.integer)
        or liquidity_window < 1
        or liquidity_window % 2 == 0
    ):
        raise ValueError(
            f"liquidity_window must be an odd int >= 1 (a band of rank positions centred on "
            f"the strategy name's rank), got {liquidity_window!r}"
        )
    return int(liquidity_window)


def random_selection_path(
    strategy_weights: pd.DataFrame,
    members: pd.DataFrame,
    rng: np.random.Generator,
    *,
    inverse_vol: pd.DataFrame | None = None,
    max_weight: float = 1.0,
    vol_panel: pd.DataFrame | None = None,
    liquidity_share: pd.DataFrame | None = None,
    liquidity_cap_mult: float | None = None,
    liquidity_panel: pd.DataFrame | None = None,
    liquidity_window: int = 5,
) -> NullPath:
    """:func:`random_selection_weights` plus its match counts, book mix, overlap and rank gap
    (:class:`NullPath`)."""
    window = _check_window(liquidity_window)
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
    bucket_liq = None
    if liquidity_panel is not None:
        _check_panel("liquidity_panel", liquidity_panel, like=strategy_weights)
        try:
            bucket_liq = liquidity_panel.to_numpy(dtype=float)
        except (TypeError, ValueError) as exc:
            raise ValueError("liquidity_panel must hold numbers only") from exc
    # Book-mix volatility terciles (reporting only): by vol_panel, else by 1 / inverse_vol.
    if vol_panel is not None or sizing is None:
        mix_vol = None
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            mix_vol = 1.0 / sizing
    keys = MATCH_LEVELS if bucket_liq is None else RANK_MATCH_KEYS
    counts = dict.fromkeys(keys, 0)
    kept_counts = dict.fromkeys(keys, 0)
    gaps = _GapAcc()
    overlap = np.zeros(2)  # running sums: overlap share, chance share
    overlap_rows = 0
    mix = _MixAcc()
    out = np.full_like(w, np.nan)
    prev_strategy = np.zeros(w.shape[1], dtype=bool)
    prev_null = np.zeros(w.shape[1], dtype=bool)
    for i in np.flatnonzero(~np.isnan(w).all(axis=1)):
        row = np.nan_to_num(w[i], nan=0.0)
        held = row > 0
        k = int(held.sum())
        kept_strategy = np.flatnonzero(held & prev_strategy)  # before prev_strategy moves on
        kept = len(kept_strategy)
        fresh_strategy = np.flatnonzero(held & ~prev_strategy)
        out[i] = 0.0
        prev_strategy = held
        pool = m[i]
        if k == 0 or not pool.any():
            prev_null = np.zeros_like(prev_null)
            continue
        ranked = pool | held
        vol_b = None if bucket_vol is None else _tercile_buckets(bucket_vol[i], ranked)
        liq_b = None if bucket_liq is None else _tercile_buckets(bucket_liq[i], ranked)
        match = (
            None
            if bucket_liq is None
            else _RankMatch(_liquidity_ranks(bucket_liq[i], ranked), window, gaps)
        )
        survivors = np.flatnonzero(prev_null & pool)
        n_keep = min(kept, len(survivors))
        keep, unmatched = _retain(rng, survivors, n_keep, kept_strategy, vol_b, match, kept_counts)
        fresh_pool = np.flatnonzero(pool & ~prev_null)
        need = min(k - len(keep), len(fresh_pool))
        if match is None:
            fresh = _draw_fresh(rng, fresh_pool, need, fresh_strategy, vol_b, None, counts)
        else:  # kept names the survivors could not match are matched by fresh draws
            targets = np.concatenate([fresh_strategy, unmatched]).astype(int)
            fresh, _ = _match_nearest(rng, fresh_pool, need, targets, vol_b, match, counts)
        picks = np.concatenate([keep, fresh]).astype(int)
        short = k - len(picks)
        if short > 0:  # not enough unheld names: top up from the null's other old names
            rest = np.setdiff1d(np.flatnonzero(pool), picks)
            extra = rng.choice(rest, size=min(short, len(rest)), replace=False)
            picks = np.concatenate([picks, extra]).astype(int)
            counts["pool"] += len(extra)
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
        overlap += (held[picks].mean(), (held & pool).sum() / pool.sum())
        overlap_rows += 1
        mix_vol_b = vol_b if mix_vol is None else _tercile_buckets(mix_vol[i], ranked)
        held_idx = np.flatnonzero(held)
        for p, b in enumerate((liq_b, mix_vol_b)):
            if b is not None:
                mix.add(0, p, b, held_idx, row[held_idx])
                mix.add(1, p, b, picks, out[i, picks])
    weights = pd.DataFrame(out, index=strategy_weights.index, columns=strategy_weights.columns)
    ov = overlap / overlap_rows if overlap_rows else np.full(2, np.nan)
    return NullPath(
        weights=weights,
        match_fallbacks=counts,
        kept_match_fallbacks=kept_counts if bucket_liq is not None else {},
        book_mix=mix.result((bucket_liq is not None, bucket_vol is not None)),
        overlap=float(ov[0]),
        overlap_chance=float(ov[1]),
        rank_gap=gaps.mean(),
    )


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
    liquidity_panel: pd.DataFrame | None = None,
    liquidity_window: int = 5,
) -> pd.DataFrame:
    """One null path (see the module docstring). On each strategy decision row with k names,
    of which ``kept`` were also in the strategy's previous decision: keep ``kept`` random names
    of the null's own previous picks that are still eligible, then draw the rest from eligible
    names it did not hold (falling back to any eligible name). Weights follow the strategy's
    rule: equal, or proportional to 1/vol when ``inverse_vol`` is given (then only names with
    finite vol > 0 are eligible, as in ``top_quantile_weights``), scaled to the strategy's row
    sum and capped at ``max_weight``. ``vol_panel`` (default: ``inverse_vol``) is a trailing
    volatility panel known at each row; with it, fresh names match the strategy's fresh names'
    counts per volatility tercile of the eligible pool. ``liquidity_panel`` is a trailing
    liquidity panel known at each row (dollar volume or liquidity share: only its rank within
    the row's pool matters); with it, every replaced name (fresh and kept) is drawn from the
    band of ``liquidity_window`` liquidity ranks centred on the strategy name it stands in
    for, preferring the same volatility tercile within the band (module docstring,
    **Nearest-rank liquidity matching**). With
    ``liquidity_share`` and ``liquidity_cap_mult`` (both or neither; the strategy's own
    values), only names with a finite share > 0 are drawn, the pre-cap total is the
    strategy's, and each pick is capped at ``min(max_weight, mult * share)`` with the excess in
    cash (module docstring, steps 1-3). NaN rows stay NaN (hold)."""
    return random_selection_path(
        strategy_weights,
        members,
        rng,
        inverse_vol=inverse_vol,
        max_weight=max_weight,
        vol_panel=vol_panel,
        liquidity_share=liquidity_share,
        liquidity_cap_mult=liquidity_cap_mult,
        liquidity_panel=liquidity_panel,
        liquidity_window=liquidity_window,
    ).weights


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
    liquidity_panel: pd.DataFrame | None = None,
    liquidity_window: int = 5,
) -> RandomSelectionNull:
    """Sharpe, total return, annual turnover and mean decision exposure of ``n_paths``
    random-selection portfolios (module docstring). Pass the strategy's own ``members`` mask,
    weighting inputs, a trailing ``vol_panel`` and ``liquidity_panel``, liquidity cap and
    engine settings (``costs`` and ``impact`` included) so only the choice of coins differs.
    ``match_fallbacks`` sums the per-level match counts over the paths."""
    _check_panel("strategy_weights", strategy_weights)
    _check_members(members)
    _check_panel("members", members, like=strategy_weights)
    if isinstance(n_paths, bool) or not isinstance(n_paths, int) or n_paths < 1:
        raise ValueError(f"n_paths must be an int >= 1, got {n_paths!r}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed must be an int, got {seed!r}")
    rng = np.random.default_rng(seed)
    drawn = [
        random_selection_path(
            strategy_weights,
            members,
            rng,
            inverse_vol=inverse_vol,
            max_weight=max_weight,
            vol_panel=vol_panel,
            liquidity_share=liquidity_share,
            liquidity_cap_mult=liquidity_cap_mult,
            liquidity_panel=liquidity_panel,
            liquidity_window=liquidity_window,
        )
        for _ in range(n_paths)
    ]
    paths = [d.weights for d in drawn]
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
        match_fallbacks=sum_match_fallbacks(d.match_fallbacks for d in drawn),
        kept_match_fallbacks=sum_kept_match_fallbacks(drawn),
        book_mix=mean_book_mix(drawn),
        overlap=mean_path_stat(d.overlap for d in drawn),
        overlap_chance=mean_path_stat(d.overlap_chance for d in drawn),
        rank_gap=mean_path_stat(d.rank_gap for d in drawn),
    )


def mean_path_stat(values: Iterable[float]) -> float:
    """Mean of the finite per-path values (overlap, chance overlap, rank gap); NaN if none."""
    arr = np.asarray(list(values), dtype=float)
    arr = arr[np.isfinite(arr)]
    return float(arr.mean()) if arr.size else float("nan")


def sum_match_fallbacks(counts: Iterable[Mapping[str, int]]) -> dict[str, int]:
    """Match counts summed over paths. Volatility-only paths: every ``MATCH_LEVELS`` key
    present (also for no paths). Nearest-rank paths: every ``RANK_MATCH_KEYS`` key, then any
    other key seen (in first-seen order)."""
    counts = list(counts)
    seen = [key for c in counts for key in c]
    if all(key in MATCH_LEVELS for key in seen):
        keys: list[str] = list(MATCH_LEVELS)
    else:
        extra = [key for key in dict.fromkeys(seen) if key not in RANK_MATCH_KEYS]
        keys = list(RANK_MATCH_KEYS) + extra
    total = dict.fromkeys(keys, 0)
    for c in counts:
        for key in keys:
            total[key] += int(c.get(key, 0))
    return total


def sum_kept_match_fallbacks(paths: Iterable[NullPath]) -> dict[str, int]:
    """Kept-name match counts summed over paths; empty when no path matched its kept names
    (legacy uniform retention, module docstring **Kept names**)."""
    kept = [p.kept_match_fallbacks for p in paths if p.kept_match_fallbacks]
    return sum_match_fallbacks(kept) if kept else {}


def mean_book_mix(paths: Iterable[NullPath]) -> dict[str, dict[str, list[float]]]:
    """``{"strategy": ..., "null": ...}`` book tercile mix (module docstring, **Book mix**):
    the strategy's (the same on every path) and the null's averaged over paths, each key
    ``"<panel>_<count|weight>"`` -> three shares (NaN where no row had a ranked held name).
    Empty when there are no paths or no panel."""
    mixes = [p.book_mix for p in paths if p.book_mix]
    if not mixes:
        return {}
    null: dict[str, list[float]] = {}
    for key in mixes[0]["null"]:
        stack = np.array([m["null"][key] for m in mixes], dtype=float)
        n = np.isfinite(stack).sum(axis=0)
        avg = np.where(n > 0, np.nansum(stack, axis=0) / np.maximum(n, 1), np.nan)
        null[key] = [float(x) for x in avg]
    return {"strategy": {k: list(v) for k, v in mixes[0]["strategy"].items()}, "null": null}
