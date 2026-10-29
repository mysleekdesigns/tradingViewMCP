"""Look-ahead-safe price/volume features for the plan registry.

Every ``compute`` here is a trailing calculation: the value at bar t reads candle rows at or
before t only (rolling windows ending at t, SMA-seeded recursive smoothers, ``shift`` with a
positive lag). ``tests/test_registry_lookahead.py`` proves it for every registered feature
with the perturbation test.

Conventions shared with the web chart (``web/src/indicators/``), so the UI and the lab agree:

* EMA and Wilder's RMA are seeded with the SMA of the first ``period`` inputs (TradingView
  ``ta.ema`` / ``ta.rma``), first value at bar ``period - 1`` of their input.
* Bollinger Bands and the z-score use the population standard deviation (divide by N).
* RSI follows ``ta.rsi``: avg loss 0 -> 100 (including a flat window), avg gain 0 -> 0.

Windows count **bars (rows)**, not wall-clock time: an absent bar (gap) is simply not in the
window, never forward-filled. ``tradable=False`` rows are ordinary inputs here; the engine is
what refuses to fill on them.

Pure module: DataFrames in, DataFrames out, no I/O, no globals mutated.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping

import numpy as np
import pandas as pd
from pandas.api.typing import Rolling

from lab.types import FeatureCompute, FeatureSpec, ParamSpec

MAX_PERIOD = 1_000  # longest look-back an agent may request; ~167 days of 4h bars

# Expanding percentile ranks (``vol_rank``, ``premium_mean_rank``) are NaN until their input has
# this many valid observations: a "top decile" read off fewer than 20 values is 1-2 points.
# 20 is also small enough that a 7-day derivatives window plus this history fits in the lab's
# 30-day [derivatives] warmup_days on 1d bars (7 + 20 - 1 = 26 bars) and on 4h bars
# (42 + 20 - 1 = 61 bars, about 10 days).
EXPANDING_RANK_MIN_HISTORY = 20

# --------------------------------------------------------------------------- params


def fill_params(spec: FeatureSpec, params: Mapping[str, float]) -> dict[str, float]:
    """Validate ``params`` against ``spec`` and fill defaults.

    Returns a new dict with every parameter of ``spec``: ``int`` params as Python ``int``,
    ``float`` params as Python ``float``. Raises ``ValueError`` naming the feature and the
    parameter for an unknown name, a non-numeric or non-finite value, a non-integral value
    for an ``int`` param, or a value outside ``[low, high]``.
    """
    known = {p.name: p for p in spec.params}
    unknown = sorted(str(k) for k in params if k not in known)
    if unknown:
        raise ValueError(
            f"feature {spec.name!r} has no parameter(s) {unknown}; "
            f"its parameters are {sorted(known) or 'none'}"
        )
    out: dict[str, float] = {}
    for p in spec.params:
        raw = params.get(p.name, p.default)
        if isinstance(raw, bool) or not isinstance(raw, int | float | np.integer | np.floating):
            raise ValueError(
                f"feature {spec.name!r} parameter {p.name!r} must be a number, got {raw!r}"
            )
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(
                f"feature {spec.name!r} parameter {p.name!r} must be finite, got {raw!r}"
            )
        if p.kind == "int" and not value.is_integer():
            raise ValueError(
                f"feature {spec.name!r} parameter {p.name!r} must be a whole number, got {raw!r}"
            )
        if not p.low <= value <= p.high:
            raise ValueError(
                f"feature {spec.name!r} parameter {p.name!r}={raw!r} is out of range "
                f"[{_fmt(p.low)}, {_fmt(p.high)}]"
            )
        out[p.name] = int(value) if p.kind == "int" else value
    return out


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def _int(params: Mapping[str, float], name: str) -> int:
    return int(params[name])


# --------------------------------------------------------------------------- helpers


def _col(candles: pd.DataFrame, name: str) -> np.ndarray:
    return candles[name].to_numpy(dtype=np.float64)


def _frame(candles: pd.DataFrame, spec_outputs: tuple[str, ...], *cols: np.ndarray) -> pd.DataFrame:
    """Output frame on ``candles.index`` with exactly ``spec_outputs`` as float64 columns."""
    data = {
        name: np.asarray(col, dtype=np.float64)
        for name, col in zip(spec_outputs, cols, strict=True)
    }
    return pd.DataFrame(data, index=candles.index, columns=list(spec_outputs))


def _rolling(x: np.ndarray, period: int) -> Rolling:
    return pd.Series(x, dtype=np.float64).rolling(period, min_periods=period)


def seeded_ema(x: np.ndarray, period: int, alpha: float) -> np.ndarray:
    """SMA-seeded exponential smoother (TradingView ``ta.ema`` / ``ta.rma``).

    Leading NaNs in ``x`` are skipped: with the first finite value at position ``f``, the
    seed is the mean of ``x[f : f + period]`` placed at ``f + period - 1``, then
    ``y_t = (1 - alpha) * y_{t-1} + alpha * x_t``. Everything before the seed is NaN.
    Values after ``f`` must be finite. The recursion only reads earlier rows, so ``y_t``
    depends on ``x[: t + 1]`` alone.
    """
    n = x.shape[0]
    out = np.full(n, np.nan)
    finite = np.flatnonzero(~np.isnan(x))
    if finite.size == 0:
        return out
    start = int(finite[0])
    seed_at = start + period - 1
    if seed_at >= n:
        return out
    tail = np.array(x[seed_at:], dtype=np.float64, copy=True)
    tail[0] = x[start : seed_at + 1].mean()
    out[seed_at:] = pd.Series(tail).ewm(alpha=alpha, adjust=False).mean().to_numpy()
    return out


def expanding_pct_rank(x: np.ndarray, min_history: int = EXPANDING_RANK_MIN_HISTORY) -> np.ndarray:
    """Percentile rank of ``x[t]`` in its own expanding history, in (0, 1].

    ``rank[t] = #{j <= t valid : x[j] <= x[t]} / #{j <= t valid}`` (ties count as at or below,
    so the latest value is included and the rank is never 0). NaN where ``x[t]`` is NaN and
    until ``min_history`` valid values have been seen; NaN rows are left out of the history
    (never filled). Reads rows at or before t only. "Expanding" means from the first row of the
    frame passed in, so the value depends on where that history starts (as in the literature's
    expanding-window deciles), never on rows after t.
    """
    series = pd.Series(np.asarray(x, dtype=np.float64), dtype=np.float64)
    ranked = series.expanding(min_periods=min_history).rank(method="max", pct=True)
    out = np.array(ranked.to_numpy(dtype=np.float64), dtype=np.float64, copy=True)
    out[np.isnan(series.to_numpy())] = np.nan
    return out


def _ema_alpha(period: int) -> float:
    return 2.0 / (period + 1.0)


def _wilder_alpha(period: int) -> float:
    return 1.0 / period


def _period_param(default: int, low: int = 1, what: str = "Look-back length in bars.") -> ParamSpec:
    return ParamSpec("period", "int", default, low, MAX_PERIOD, what)


# --------------------------------------------------------------------------- features

_VALUE = ("value",)


def _sma(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = _int(params, "period")
    return _frame(candles, _VALUE, _rolling(_col(candles, "close"), p).mean().to_numpy())


def _ema(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = _int(params, "period")
    return _frame(candles, _VALUE, seeded_ema(_col(candles, "close"), p, _ema_alpha(p)))


def _rsi(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = _int(params, "period")
    close = _col(candles, "close")
    change = np.diff(close, prepend=np.nan)  # NaN at bar 0: no previous close
    avg_gain = seeded_ema(np.maximum(change, 0.0), p, _wilder_alpha(p))
    avg_loss = seeded_ema(np.maximum(-change, 0.0), p, _wilder_alpha(p))
    with np.errstate(divide="ignore", invalid="ignore"):
        rs_rsi = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    rsi = np.where(avg_loss == 0.0, 100.0, np.where(avg_gain == 0.0, 0.0, rs_rsi))
    rsi[np.isnan(avg_gain) | np.isnan(avg_loss)] = np.nan
    return _frame(candles, _VALUE, rsi)


def true_range(candles: pd.DataFrame) -> np.ndarray:
    """``max(high - low, |high - prev close|, |low - prev close|)``; bar 0 is ``high - low``."""
    high, low, close = _col(candles, "high"), _col(candles, "low"), _col(candles, "close")
    prev_close = np.concatenate(([np.nan], close[:-1])) if close.size else close
    return np.fmax(high - low, np.fmax(np.abs(high - prev_close), np.abs(low - prev_close)))


def _atr(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = _int(params, "period")
    return _frame(candles, _VALUE, seeded_ema(true_range(candles), p, _wilder_alpha(p)))


_BB_OUT = ("mid", "upper", "lower")


def _bollinger(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = _int(params, "period")
    mult = float(params["mult"])
    roll = _rolling(_col(candles, "close"), p)
    mid = roll.mean().to_numpy()
    dev = mult * roll.std(ddof=0).to_numpy()
    return _frame(candles, _BB_OUT, mid, mid + dev, mid - dev)


_MACD_OUT = ("macd", "signal", "hist")


def _macd(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    fast, slow, sig = _int(params, "fast"), _int(params, "slow"), _int(params, "signal")
    close = _col(candles, "close")
    macd = seeded_ema(close, fast, _ema_alpha(fast)) - seeded_ema(close, slow, _ema_alpha(slow))
    signal = seeded_ema(macd, sig, _ema_alpha(sig))
    return _frame(candles, _MACD_OUT, macd, signal, macd - signal)


_DONCHIAN_OUT = ("upper", "lower")


def _donchian(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = _int(params, "period")
    upper = _rolling(_col(candles, "high"), p).max().to_numpy()
    lower = _rolling(_col(candles, "low"), p).min().to_numpy()
    return _frame(candles, _DONCHIAN_OUT, upper, lower)


def _roc(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = _int(params, "period")
    close = _col(candles, "close")
    past = np.full_like(close, np.nan)
    if close.size > p:
        past[p:] = close[:-p]
    return _frame(candles, _VALUE, 100.0 * (close - past) / past)


def _realized_vol(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = _int(params, "period")
    log_ret = np.diff(np.log(_col(candles, "close")), prepend=np.nan)
    return _frame(candles, _VALUE, _rolling(log_ret, p).std(ddof=1).to_numpy())


def _vol_rank(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    vol = _realized_vol(candles, params)["value"].to_numpy(dtype=np.float64)
    return _frame(candles, _VALUE, expanding_pct_rank(vol))


def _zscore(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = _int(params, "period")
    close = _col(candles, "close")
    roll = _rolling(close, p)
    mean = roll.mean().to_numpy()
    std = roll.std(ddof=0).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (close - mean) / std
    z = np.where(std == 0.0, 0.0, z)  # a flat window: price sits exactly on its mean
    return _frame(candles, _VALUE, z)


def _volume_sma(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = _int(params, "period")
    return _frame(candles, _VALUE, _rolling(_col(candles, "volume"), p).mean().to_numpy())


# --------------------------------------------------------------------------- specs


def make_spec(
    name: str,
    description: str,
    params: tuple[ParamSpec, ...],
    outputs: tuple[str, ...],
    compute: FeatureCompute,
    warmup: Callable[[Mapping[str, float]], float],
) -> FeatureSpec:
    """Build a spec whose ``compute``/``warmup`` fill and validate params first, so calling
    them directly with partial params is as safe as calling them through the plan."""
    probe = FeatureSpec(name, description, params, outputs, compute, lambda _p: 0)

    def checked_compute(candles: pd.DataFrame, p: Mapping[str, float]) -> pd.DataFrame:
        return compute(candles, fill_params(probe, p))

    def checked_warmup(p: Mapping[str, float]) -> int:
        return int(warmup(fill_params(probe, p)))

    return FeatureSpec(name, description, params, outputs, checked_compute, checked_warmup)


SMA = make_spec(
    "sma",
    "Simple moving average of close over the last `period` bars, including bar t.",
    (_period_param(20),),
    _VALUE,
    _sma,
    lambda p: p["period"] - 1,
)

EMA = make_spec(
    "ema",
    "Exponential moving average of close (alpha 2/(period+1)), seeded with the SMA of the "
    "first `period` closes like TradingView's ta.ema.",
    (_period_param(20),),
    _VALUE,
    _ema,
    lambda p: p["period"] - 1,
)

RSI = make_spec(
    "rsi",
    "Wilder's RSI of close on a 0-100 scale (TradingView ta.rsi); above 70 is commonly read as "
    "overbought and below 30 as oversold.",
    (_period_param(14),),
    _VALUE,
    _rsi,
    lambda p: p["period"],
)

ATR = make_spec(
    "atr",
    "Average true range in price units: Wilder-smoothed max(high-low, |high-prev close|, "
    "|low-prev close|), a volatility measure for stops and position sizing.",
    (_period_param(14),),
    _VALUE,
    _atr,
    lambda p: p["period"] - 1,
)

BOLLINGER = make_spec(
    "bollinger",
    "Bollinger Bands on close: mid = SMA(period), upper/lower = mid +/- mult x population "
    "standard deviation of the same window (TradingView convention).",
    (
        _period_param(20),
        ParamSpec("mult", "float", 2.0, 0.1, 10.0, "Band width in standard deviations."),
    ),
    _BB_OUT,
    _bollinger,
    lambda p: p["period"] - 1,
)

MACD = make_spec(
    "macd",
    "MACD of close: macd = EMA(fast) - EMA(slow), signal = EMA(signal) of macd, "
    "hist = macd - signal; a positive hist means momentum is rising.",
    (
        ParamSpec("fast", "int", 12, 1, MAX_PERIOD, "Fast EMA length in bars."),
        ParamSpec("slow", "int", 26, 1, MAX_PERIOD, "Slow EMA length in bars."),
        ParamSpec("signal", "int", 9, 1, MAX_PERIOD, "Signal-line EMA length in bars."),
    ),
    _MACD_OUT,
    _macd,
    lambda p: max(p["fast"], p["slow"]) + p["signal"] - 2,
)

DONCHIAN = make_spec(
    "donchian",
    "Donchian channel: upper = highest high and lower = lowest low of the last `period` bars "
    "INCLUDING bar t, so close can never exceed bar t's own upper; for a breakout rule compare "
    "close with the previous bar's channel (prev(...)), not the current one.",
    (_period_param(20),),
    _DONCHIAN_OUT,
    _donchian,
    lambda p: p["period"] - 1,
)

ROC = make_spec(
    "roc",
    "Rate of change of close over `period` bars in percent (5.0 means +5%), "
    "100 x (close - close[period bars ago]) / close[period bars ago].",
    (_period_param(10),),
    _VALUE,
    _roc,
    lambda p: p["period"],
)

REALIZED_VOL = make_spec(
    "realized_vol",
    "Sample standard deviation of log close-to-close returns over the last `period` bars, per "
    "bar and NOT annualised (multiply by sqrt(bars per year) yourself if needed).",
    (_period_param(20, low=2),),
    _VALUE,
    _realized_vol,
    lambda p: p["period"],
)

VOL_RANK = make_spec(
    "vol_rank",
    "Expanding percentile rank in (0, 1] of realized_vol(`period`) within its own history "
    "through bar t (0.9: only 10% of past bars were more volatile), NaN until "
    f"{EXPANDING_RANK_MIN_HISTORY} values exist. A volatility-regime measure for risk "
    "overlays, such as flat while vol_rank > 0.9.",
    (_period_param(30, low=2),),
    _VALUE,
    _vol_rank,
    lambda p: p["period"] + EXPANDING_RANK_MIN_HISTORY - 1,
)

ZSCORE = make_spec(
    "zscore",
    "How many population standard deviations close sits above (+) or below (-) its "
    "`period`-bar SMA; 0 when the window is flat.",
    (_period_param(20, low=2),),
    _VALUE,
    _zscore,
    lambda p: p["period"] - 1,
)

VOLUME_SMA = make_spec(
    "volume_sma",
    "Simple moving average of volume (base-asset units) over the last `period` bars; compare "
    "with `volume` to spot unusual activity.",
    (_period_param(20),),
    _VALUE,
    _volume_sma,
    lambda p: p["period"] - 1,
)

PRICE_FEATURES: tuple[FeatureSpec, ...] = (
    SMA,
    EMA,
    RSI,
    ATR,
    BOLLINGER,
    MACD,
    DONCHIAN,
    ROC,
    REALIZED_VOL,
    ZSCORE,
    VOLUME_SMA,
    VOL_RANK,
)
