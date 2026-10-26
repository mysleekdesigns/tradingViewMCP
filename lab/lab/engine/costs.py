"""Per-bar trading costs for the engine (PLAN.md Phase 7 How §3).

Costs are charged per side on traded value:

* **fee**: ``CostModel.fee_rate`` x traded value (Binance spot taker fee, 0.1% by default).
* **slippage**: the fill price is moved against the trader by a fraction of the reference
  price (the bar's open, or the stop price for stop fills). ``slippage_series`` gives that
  fraction for every bar.

Volatility-scaled slippage (``CostModel.slippage_vol_mult > 0``) widens the base rate when the
market has just been unusually wild. It is causal: the fraction applied to a fill at bar j's
open uses only bars up to and including j-1, because the fill bar's own high/low are not known
when its open trades::

    r[i]     = (high[i] - low[i]) / close[i]                      # relative bar range
    med[i]   = median(r[i - median_window + 1 .. i])              # NaN until the window is full
    slip[j]  = min(slippage_rate + slippage_vol_mult * max(0, r[j-1] - med[j-1]), slippage_cap)
    slip[0]  = slippage_rate; slip[j] = slippage_rate while med[j-1] is still NaN (warm-up)

``slippage_cap`` (>= ``slippage_rate``, checked by :func:`validate_costs`) bounds every bar's
fraction, so one freak bar range cannot charge an absurd fill. Callers that backtest a window
slice should compute the series on the longer history and slice it (the engine's
``cost_history=``), so the first ``median_window`` bars of the window already get the
vol-scaled rate instead of the warm-up base rate.

**Participation (market impact) cost, portfolio runs only** (PLAN.md Phase 10 safeguards).
:class:`ImpactModel` adds the square-root law on top of fee + slippage for multi-coin books,
where small coins cannot absorb a large order at the quoted price::

    frac = min(coef * sigma * sqrt(Q / ADV), cap)

* ``Q`` is the traded value in USD for a book of ``aum_usd`` (returns are scale-free, so the
  notional the participation is computed for must be explicit).
* ``ADV`` is the median of ``close * volume`` over the symbol's own bars in
  ``(t - adv_days, t]`` (the same definition as ``lab.data.universe.trailing_dollar_volume``),
  taken at the bar BEFORE the fill: a fill at bar j's open uses ``t = time[j-1]``. It is NaN
  (warm-up) until the symbol had a bar at or before the window's open end
  ``time[j-1] - adv_days``, so the window never reaches back before its listing.
* ``sigma`` is the sample std (ddof=1) of the last ``vol_days`` one-bar log returns
  ``log(close[i] / close[i-1])`` ending at row j-1 (NaN until ``vol_days`` returns exist).
* Unknown liquidity (``ADV`` or ``sigma`` NaN or <= 0) is charged ``cap``, never zero.

Impact is sized on the FIXED book ``aum_usd``, not on current equity: ``Q`` is the traded
fraction of equity times ``aum_usd`` at every fill, so a book that compounds (winners) is
charged as if it had stayed at ``aum_usd``, and a shrinking book as if it had not shrunk.
Choose ``aum_usd`` as the size the strategy would actually run at over the whole test.

Slippage plus impact moves a fill by at most ``slippage_cap + impact.cap`` of the reference
price (``slippage_rate + impact.cap`` when ``slippage_vol_mult`` is 0, since flat slippage
never reaches the cap), so that sum must stay below 1 (a sell would otherwise fill at a price
<= 0);
:func:`validate_impact` checks it when given the ``CostModel`` and the portfolio engine always
does. Cost stress (x2, x4) scales impact too: :meth:`ImpactModel.scaled` mirrors
:meth:`lab.types.CostModel.scaled` (``coef`` and ``cap`` times the factor).

Both inputs are per bar of the candles given: on daily bars ``ADV`` is daily dollar volume and
``sigma`` daily volatility. On k-bars-a-day candles both are per bar, and under square-root-of-
time scaling ``sigma_bar * sqrt(Q / ADV_bar) = sigma_day / sqrt(k) * sqrt(k Q / ADV_day)``, so
the charge is the same. Each listing segment (``segment_starts``) is its own market: both
inputs restart their warm-up there, so a redenomination jump never enters ``sigma``.

The single-symbol engine (``lab.engine.backtest``, :class:`lab.types.CostModel`,
:func:`slippage_series`) never uses impact: BTC/ETH single-symbol costs are unchanged.

Pure: arrays in, arrays out. Results are historical and not forward-looking.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd

from lab.types import CostModel, validate_candles


def validate_costs(costs: CostModel) -> None:
    """Raise ``ValueError`` naming the field if any cost rate is negative or non-finite, the
    fee is >= 1, or ``slippage_cap`` is below ``slippage_rate``."""
    for name in ("fee_rate", "slippage_rate", "slippage_vol_mult", "slippage_cap"):
        value = getattr(costs, name)
        if not np.isfinite(value) or value < 0:
            raise ValueError(f"costs.{name} must be a finite number >= 0, got {value!r}")
    if costs.fee_rate >= 1:
        raise ValueError(f"costs.fee_rate must be < 1 (a fraction), got {costs.fee_rate!r}")
    if costs.slippage_cap < costs.slippage_rate:
        raise ValueError(
            f"costs.slippage_cap ({costs.slippage_cap!r}) must be >= costs.slippage_rate "
            f"({costs.slippage_rate!r}); a cap below the base rate would silently lower it"
        )


def slippage_series(
    candles: pd.DataFrame, costs: CostModel, *, median_window: int = 100
) -> np.ndarray:
    """Slippage fraction applied to a fill at each bar's open (see the module docstring).

    ``slip[j]`` depends only on candle rows ``< j``, so changing bar j's own high/low/close
    never changes the slippage charged on bar j's open.
    """
    validate_candles(candles)
    validate_costs(costs)
    if isinstance(median_window, bool) or not isinstance(median_window, int) or median_window < 1:
        raise ValueError(f"median_window must be an int >= 1, got {median_window!r}")

    n = len(candles)
    cap = float(costs.slippage_cap)
    slip = np.full(n, float(costs.slippage_rate))
    if costs.slippage_vol_mult == 0.0 or n < 2:
        return slip

    high = candles["high"].to_numpy(dtype=float)
    low = candles["low"].to_numpy(dtype=float)
    close = candles["close"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rng = (high - low) / close
    rng = np.where(np.isfinite(rng), rng, np.nan)
    med = (
        pd.Series(rng)
        .rolling(median_window, min_periods=median_window)
        .median()
        .to_numpy(dtype=float)
    )
    excess = rng - med  # NaN during warm-up or where the range is undefined
    excess = np.where(np.isfinite(excess), np.maximum(excess, 0.0), 0.0)
    slip[1:] = np.minimum(costs.slippage_rate + costs.slippage_vol_mult * excess[:-1], cap)
    return slip


# --------------------------------------------------------------------------- market impact


@dataclass(frozen=True)
class ImpactModel:
    """Square-root participation cost for portfolio runs (see the module docstring)."""

    coef: float  # Y in impact = Y * sigma * sqrt(Q / ADV); finite >= 0
    aum_usd: float  # book size the participation is computed for (> 0)
    adv_days: int  # trailing window (days, >= 1) of the median daily dollar volume
    vol_days: int  # trailing window (rows, >= 2) of the std (ddof=1) of one-bar log returns
    cap: float  # max impact fraction per fill, finite in [0, 1)

    def scaled(self, factor: float) -> ImpactModel:
        """``coef`` and ``cap`` multiplied by ``factor`` (finite, > 0) for cost stress (x2, x4),
        mirroring :meth:`lab.types.CostModel.scaled`; ``aum_usd`` and the windows unchanged.
        The result is validated, so a stressed ``cap >= 1`` raises ``ValueError``."""
        if (
            isinstance(factor, bool)
            or not isinstance(factor, int | float | np.integer | np.floating)
            or not np.isfinite(factor)
            or factor <= 0
        ):
            raise ValueError(f"impact factor must be a finite number > 0, got {factor!r}")
        out = ImpactModel(
            coef=self.coef * factor,
            aum_usd=self.aum_usd,
            adv_days=self.adv_days,
            vol_days=self.vol_days,
            cap=self.cap * factor,
        )
        validate_impact(out)
        return out


def _is_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | np.integer)


def validate_impact(impact: ImpactModel, costs: CostModel | None = None) -> None:
    """Raise ``ValueError`` naming the field if any :class:`ImpactModel` value is out of range.

    With ``costs`` also require ``max_slip + impact.cap < 1``, where ``max_slip`` is the
    largest slippage :func:`slippage_series` can charge: ``costs.slippage_cap`` when
    ``slippage_vol_mult > 0``, and ``costs.slippage_rate`` when it is 0 (flat slippage never
    reaches the cap, and ``CostModel``'s default ``slippage_cap = 1.0`` means "uncapped").
    Slippage and impact together move a fill by up to that fraction, and at 1 or more a sell
    fills at a price <= 0. The error names both fields. ``costs`` itself is checked by
    :func:`validate_costs`, not here."""
    if not isinstance(impact, ImpactModel):
        raise ValueError(f"impact must be an ImpactModel, got {type(impact).__name__}")
    for name in ("coef", "aum_usd", "cap"):
        value = getattr(impact, name)
        if isinstance(value, bool) or not isinstance(value, int | float | np.integer | np.floating):
            raise ValueError(f"impact.{name} must be a finite number, got {value!r}")
    if not np.isfinite(impact.coef) or impact.coef < 0:
        raise ValueError(f"impact.coef must be a finite number >= 0, got {impact.coef!r}")
    if not np.isfinite(impact.aum_usd) or impact.aum_usd <= 0:
        raise ValueError(f"impact.aum_usd must be a finite number > 0, got {impact.aum_usd!r}")
    if not np.isfinite(impact.cap) or not 0 <= impact.cap < 1:
        raise ValueError(f"impact.cap must be a finite number in [0, 1), got {impact.cap!r}")
    if not _is_int(impact.adv_days) or impact.adv_days < 1:
        raise ValueError(f"impact.adv_days must be an int >= 1, got {impact.adv_days!r}")
    if not _is_int(impact.vol_days) or impact.vol_days < 2:
        raise ValueError(f"impact.vol_days must be an int >= 2, got {impact.vol_days!r}")
    if costs is not None:
        if not isinstance(costs, CostModel):
            raise ValueError(f"costs must be a CostModel, got {type(costs).__name__}")
        field = "slippage_cap" if costs.slippage_vol_mult > 0 else "slippage_rate"
        slip = float(getattr(costs, field))
        total = slip + float(impact.cap)
        if not total < 1:
            raise ValueError(
                f"costs.{field} ({slip!r}) + impact.cap ({impact.cap!r}) must be < 1: "
                f"slippage plus impact at {total!r} of the price would fill a sell at a "
                "price <= 0"
            )


def _segment_inputs(candles: pd.DataFrame, impact: ImpactModel) -> tuple[np.ndarray, np.ndarray]:
    """``impact_inputs`` for one listing segment (no breaks inside)."""
    n = len(candles)
    adv = np.full(n, np.nan)
    sigma = np.full(n, np.nan)
    if n < 2:
        return adv, sigma
    index = candles.index
    close = candles["close"].to_numpy(dtype=float)
    dollar = pd.Series(close * candles["volume"].to_numpy(dtype=float), index=index)
    window = pd.Timedelta(days=int(impact.adv_days))
    med = dollar.rolling(window).median().to_numpy(dtype=float)
    full = (index - window) >= index[0]  # the window starts at or after the first bar
    med = np.where(np.asarray(full), med, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        log_ret = np.full(n, np.nan)
        log_ret[1:] = np.log(close[1:] / close[:-1])
    sd = (
        pd.Series(log_ret)
        .rolling(int(impact.vol_days), min_periods=int(impact.vol_days))
        .std(ddof=1)
        .to_numpy(dtype=float)
    )
    adv[1:] = med[:-1]  # a fill at row j's open sees rows < j only
    sigma[1:] = sd[:-1]
    return adv, sigma


def impact_inputs(
    candles: pd.DataFrame,
    impact: ImpactModel,
    *,
    segment_starts: pd.DatetimeIndex | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """``(adv, sigma)`` per bar of this symbol's own candles, CAUSAL for a fill at bar j's open:
    both use only rows < j (rows up to j-1). NaN during warm-up (see the module docstring).

    ``segment_starts`` lists bar times where a new listing segment begins; each segment is
    computed on its own rows, so both inputs restart their warm-up at a segment's first bar.
    """
    validate_candles(candles)
    validate_impact(impact)
    n = len(candles)
    if segment_starts is None or len(segment_starts) == 0:
        return _segment_inputs(candles, impact)
    if not isinstance(segment_starts, pd.DatetimeIndex):
        raise ValueError(
            f"segment_starts must be a DatetimeIndex or None, got {type(segment_starts).__name__}"
        )
    pos = candles.index.get_indexer(segment_starts)
    if (pos < 0).any():
        raise ValueError(
            f"segment_starts has {segment_starts[pos < 0][0]}, which is not a bar of the candles"
        )
    cuts = np.unique(np.concatenate([[0], pos, [n]]))
    adv = np.full(n, np.nan)
    sigma = np.full(n, np.nan)
    for a, b in pairwise(cuts):
        adv[a:b], sigma[a:b] = _segment_inputs(candles.iloc[a:b], impact)
    return adv, sigma


def impact_fraction(
    trade_value_usd: np.ndarray,
    adv: np.ndarray,
    sigma: np.ndarray,
    impact: ImpactModel,
) -> np.ndarray:
    """``min(coef * sigma * sqrt(trade_value_usd / adv), cap)`` element-wise (broadcast);
    where ``adv`` or ``sigma`` is NaN or <= 0 the fraction is ``cap`` (unknown liquidity is
    charged the worst case, never zero). ``trade_value_usd`` must be finite and >= 0."""
    validate_impact(impact)
    q = np.asarray(trade_value_usd, dtype=float)
    v = np.asarray(adv, dtype=float)
    s = np.asarray(sigma, dtype=float)
    if not np.isfinite(q).all() or (q < 0).any():
        raise ValueError("trade_value_usd must be finite and >= 0")
    q, v, s = np.broadcast_arrays(q, v, s)
    with np.errstate(invalid="ignore"):
        ok = np.isfinite(v) & (v > 0) & np.isfinite(s) & (s > 0)
    raw = np.zeros(q.shape)
    raw[ok] = impact.coef * s[ok] * np.sqrt(q[ok] / v[ok])
    cap = float(impact.cap)
    return np.where(ok, np.minimum(raw, cap), cap)
