"""Cross-sectional (panel) features for multi-coin research (Phase 10, H1).

H1: long-only top quintile of the top-N liquid coins ranked on trailing 28-day return, weekly
rebalance, equal or inverse-volatility weights. These are the building blocks:

* :func:`close_panel` - closes on the union of bar times, NaN where a symbol has no bar.
* :func:`trailing_return`, :func:`trailing_volatility` - per-symbol trailing statistics.
* :func:`cross_sectional_rank`, :func:`cross_sectional_percentile` - rank within the eligible
  set (e.g. ``top_n_members(liquidity_rank(...), n)`` from :mod:`lab.data.universe`).
* :func:`rebalance_rows` - a deterministic, position-based rebalance schedule.
* :func:`top_quantile_weights` - target weights decided at each schedule row's close.

Panel convention: a wide ``DataFrame`` whose index is a strictly increasing, unique
``DatetimeIndex`` (the union of every symbol's bar times) and whose columns are symbols. NaN
means "no bar" and is never forward-filled. Look-backs count **rows** of that union index; for
daily bars a row is a day, and a hole (a day on which a symbol printed nothing) is a NaN row
inside the window, not a skipped one.

No look-ahead: every output at row t reads panel rows at or before t only, and cross-sectional
operations read row t alone. ``tests/test_registry_cross_section.py`` proves it with the
perturbation test (change every price after t, delete later bars, add a symbol listed after t).
Weights are the decision at row t's close; the portfolio engine fills them at t+1's open.

These are panel functions, not single-symbol ``FeatureSpec`` entries, so they are deliberately
not in ``REGISTRY``. Pure module: DataFrames in, DataFrames out, no I/O, no globals.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from lab.types import validate_candles

# ceil(quantile * k) must not round 0.14 * 50 = 7.000000000000001 up to 8.
_CEIL_TOL = 1e-9

# --------------------------------------------------------------------------- validation


def _check_int(name: str, value: object, low: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | np.integer):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    out = int(value)
    if out < low:
        raise ValueError(f"{name} must be >= {low}, got {out}")
    return out


def _check_panel(name: str, panel: object) -> None:
    if not isinstance(panel, pd.DataFrame):
        raise ValueError(f"{name} must be a pandas DataFrame, got {type(panel).__name__}")
    idx = panel.index
    if not isinstance(idx, pd.DatetimeIndex):
        raise ValueError(f"{name} index must be a DatetimeIndex of bar times")
    if not idx.is_unique or not idx.is_monotonic_increasing:
        raise ValueError(f"{name} index must be strictly increasing and unique")
    if not panel.columns.is_unique:
        raise ValueError(f"{name} has duplicate symbol columns")


def _floats(name: str, panel: pd.DataFrame) -> np.ndarray:
    _check_panel(name, panel)
    try:
        return panel.to_numpy(dtype=np.float64, copy=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must hold numbers only") from exc


def _bools(name: str, panel: pd.DataFrame) -> np.ndarray:
    _check_panel(name, panel)
    if not all(pd.api.types.is_bool_dtype(dtype) for dtype in panel.dtypes):
        raise ValueError(f"{name} must be a bool frame (True = eligible); fill missing with False")
    return panel.to_numpy(dtype=bool)


def _check_aligned(name: str, other: pd.DataFrame, ref_name: str, ref: pd.DataFrame) -> None:
    if list(other.columns) != list(ref.columns):
        raise ValueError(
            f"{name} columns must equal {ref_name} columns in the same order; "
            f"got {list(other.columns)} vs {list(ref.columns)} (use {name}[{ref_name}.columns])"
        )
    if not other.index.equals(ref.index):
        raise ValueError(f"{name} index must equal {ref_name} index (same bar times)")


def _like(ref: pd.DataFrame, values: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(values, index=ref.index, columns=ref.columns, dtype=np.float64)


# --------------------------------------------------------------------------- panel


def close_panel(candles_by_symbol: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Closes of every symbol on the union of their bar times.

    Columns are the symbols sorted by name; the index (named ``time``) is the sorted union of
    every frame's index. A symbol with no bar at a time is NaN there, never forward-filled. An
    empty frame gives an all-NaN column. Each frame must pass ``validate_candles``.
    """
    if not isinstance(candles_by_symbol, Mapping) or not candles_by_symbol:
        raise ValueError("candles_by_symbol must be a non-empty mapping of symbol -> candles")
    for symbol, frame in candles_by_symbol.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"symbol keys must be non-empty strings, got {symbol!r}")
        if not isinstance(frame, pd.DataFrame):
            raise ValueError(f"candles for {symbol!r} must be a DataFrame")
        try:
            validate_candles(frame)
        except ValueError as exc:
            raise ValueError(f"candles for {symbol!r}: {exc}") from exc
    indexes = [f.index for f in candles_by_symbol.values() if len(f)]
    union = pd.DatetimeIndex([], tz="UTC")
    for idx in indexes:
        union = union.union(idx) if len(union) else idx
    union = pd.DatetimeIndex(union, name="time")
    columns = {
        symbol: candles_by_symbol[symbol]["close"].astype(np.float64).reindex(union)
        for symbol in sorted(candles_by_symbol)
    }
    return pd.DataFrame(columns, index=union, dtype=np.float64)


# --------------------------------------------------------------------------- per-symbol


def trailing_return(closes: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Simple return over the last ``lookback`` rows: ``close_t / close_{t - lookback} - 1``.

    Rows of the union index, not bars of each symbol: for daily data ``lookback=28`` is 28
    days. NaN when either end is missing, non-finite, or ``<= 0``, and for the first
    ``lookback`` rows. ``lookback >= 1``.
    """
    lookback = _check_int("lookback", lookback, 1)
    c = _floats("closes", closes)
    prev = np.full_like(c, np.nan)
    if lookback < len(c):
        prev[lookback:] = c[:-lookback]
    with np.errstate(divide="ignore", invalid="ignore"):
        ok = np.isfinite(c) & np.isfinite(prev) & (c > 0) & (prev > 0)
        out = np.where(ok, c / prev - 1.0, np.nan)
    return _like(closes, out)


def log_returns(closes: pd.DataFrame) -> pd.DataFrame:
    """One-row log returns ``log(close_t / close_{t-1})``; NaN unless both closes exist, are
    finite and ``> 0``. A hole breaks the return on both sides; nothing is filled."""
    c = _floats("closes", closes)
    prev = np.full_like(c, np.nan)
    prev[1:] = c[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        ok = np.isfinite(c) & np.isfinite(prev) & (c > 0) & (prev > 0)
        out = np.where(ok, np.log(c / prev), np.nan)
    return _like(closes, out)


def trailing_volatility(
    closes: pd.DataFrame, lookback: int, *, min_periods: int | None = None
) -> pd.DataFrame:
    """Sample standard deviation (``ddof=1``) of the one-row log returns in the last
    ``lookback`` rows (rows ``t - lookback + 1 .. t``), per symbol.

    Only returns between two consecutive rows that both have a close count (see
    :func:`log_returns`); a hole removes the return into and out of it and is never filled.
    NaN when fewer than ``min_periods`` returns are available. **Not annualized**: this is
    per-row volatility (per day for daily bars); multiply by ``sqrt(365)`` for a daily panel
    if an annual figure is wanted. ``lookback >= 2``; ``min_periods`` defaults to
    ``max(2, ceil(lookback / 2))`` and must lie in ``[2, lookback]``.

    Each row is computed from its own window alone, so values are bit-reproducible regardless
    of what comes later.
    """
    lookback = _check_int("lookback", lookback, 2)
    if min_periods is None:
        min_periods = max(2, math.ceil(lookback / 2))
    min_periods = _check_int("min_periods", min_periods, 2)
    if min_periods > lookback:
        raise ValueError(f"min_periods must be <= lookback ({lookback}), got {min_periods}")
    r = log_returns(closes).to_numpy(dtype=np.float64)
    n, m = r.shape
    padded = np.vstack([np.full((lookback - 1, m), np.nan), r])
    windows = sliding_window_view(padded, lookback, axis=0)  # (n, m, lookback)
    finite = np.isfinite(windows)
    count = finite.sum(axis=-1)
    x = np.where(finite, windows, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = x.sum(axis=-1) / count
        dev = np.where(finite, windows - mean[..., None], 0.0)
        var = (dev * dev).sum(axis=-1) / (count - 1)
        out = np.where(count >= min_periods, np.sqrt(var), np.nan)
    return _like(closes, out.reshape(n, m))


# --------------------------------------------------------------------------- cross-section


def cross_sectional_rank(
    values: pd.DataFrame, mask: pd.DataFrame, *, ascending: bool = False
) -> pd.DataFrame:
    """Rank within each row among symbols where ``mask`` is True and the value is finite.

    ``1`` is the best: the largest value when ``ascending=False`` (default), the smallest
    when ``ascending=True``. NaN for every other cell. Ties go to the earlier column
    (``method="first"``). ``mask`` must be a bool frame with exactly the same index and
    columns (same order) as ``values``.
    """
    v = _floats("values", values)
    mk = _bools("mask", mask)
    _check_aligned("mask", mask, "values", values)
    keep = mk & np.isfinite(v)
    kept = _like(values, np.where(keep, v, np.nan))
    ranked = kept.rank(axis=1, ascending=bool(ascending), method="first", na_option="keep")
    return _like(values, ranked.to_numpy(dtype=np.float64))


def cross_sectional_percentile(
    values: pd.DataFrame, mask: pd.DataFrame, *, ascending: bool = False
) -> pd.DataFrame:
    """:func:`cross_sectional_rank` divided by the number of ranked symbols in the row.

    In ``(0, 1]``: ``1/k`` for the best of ``k`` ranked symbols, ``1.0`` for the worst (so
    "top quintile" is ``percentile <= 0.2``). NaN where the rank is NaN.
    """
    rank = cross_sectional_rank(values, mask, ascending=ascending).to_numpy(dtype=np.float64)
    count = np.isfinite(rank).sum(axis=1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(np.isfinite(rank), rank / count, np.nan)
    return _like(values, out)


# --------------------------------------------------------------------------- portfolio


def rebalance_rows(index: pd.DatetimeIndex, *, every: int, offset: int = 0) -> pd.Series:
    """Bool series on ``index``: True at row positions ``offset, offset + every, ...``.

    Position-based and data-independent, so it is deterministic and adding rows after t never
    moves a rebalance at or before t. On a daily union index ``every=7`` is weekly as long as
    the index has no all-symbol holes (each missing day shifts later rebalances by one row).
    ``every >= 1``, ``offset >= 0``; an ``offset`` past the end gives no rebalance.
    """
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError(f"index must be a DatetimeIndex, got {type(index).__name__}")
    every = _check_int("every", every, 1)
    offset = _check_int("offset", offset, 0)
    pos = np.arange(len(index))
    flags = (pos >= offset) & ((pos - offset) % every == 0)
    return pd.Series(flags, index=index, name="rebalance", dtype=bool)


def _cap_sum_at_one(w: np.ndarray) -> np.ndarray:
    """Nudge the largest weight down by ulps until the exact sum (``math.fsum``) is ``<= 1``.

    A rounding guard only: ``fl(1/10)`` is a hair above 0.1, so ten of them sum above 1.
    """
    while math.fsum(w.tolist()) > 1.0:
        i = int(np.argmax(w))
        w[i] = np.nextafter(w[i], 0.0)
    return w


def top_quantile_weights(
    scores: pd.DataFrame,
    mask: pd.DataFrame,
    schedule: pd.Series,
    *,
    quantile: float,
    min_names: int = 1,
    inverse_vol: pd.DataFrame | None = None,
    max_weight: float = 1.0,
) -> pd.DataFrame:
    """Long-only target weights for the top ``quantile`` of the eligible set, decided at the
    close of each schedule row (the engine fills them at the next row's open).

    On a row where ``schedule`` is True:

    1. Eligible symbols: ``mask`` True and ``scores`` finite. With ``inverse_vol`` given, a
       symbol must also have a finite ``inverse_vol > 0`` there; one without is removed from
       the eligible set *before* ranking (it cannot be sized), so the next-best symbol takes
       its place and ``k`` counts sizable symbols only.
    2. With ``k`` eligible: if ``k < min_names`` the row is all zeros (all cash). Otherwise
       pick ``n = max(ceil(quantile * k), min_names)`` symbols with the highest scores, ties to
       the earlier column (as :func:`cross_sectional_rank`). The ceiling tolerates float
       noise of 1e-9, so ``0.14 * 50`` (``7.000000000000001``) picks 7.
    3. Weights are ``1 / n`` each, or proportional to ``1 / vol`` (``inverse_vol`` holds the
       volatility, e.g. :func:`trailing_volatility`, not its reciprocal) and summing to 1.
    4. Each weight is capped at ``max_weight``; the excess stays in **cash** and is not
       redistributed. A rounding guard keeps every row's exact sum (``math.fsum``) ``<= 1``;
       a plain float sum of the row may still read ``1 + a few ulps``.

    Every unpicked symbol gets 0.0 on a schedule row (sell it). Rows where ``schedule`` is
    False are all-NaN, meaning "hold the current positions". Output index and columns equal
    ``scores``'; ``mask``, ``schedule`` (bool, same index) and ``inverse_vol`` must align
    exactly. ``0 < quantile <= 1``, ``min_names >= 1``, ``0 < max_weight <= 1``.
    """
    s = _floats("scores", scores)
    mk = _bools("mask", mask)
    _check_aligned("mask", mask, "scores", scores)
    if not isinstance(schedule, pd.Series) or not pd.api.types.is_bool_dtype(schedule.dtype):
        raise ValueError("schedule must be a bool Series (see rebalance_rows)")
    if not schedule.index.equals(scores.index):
        raise ValueError("schedule index must equal scores index (same bar times)")
    if isinstance(quantile, bool) or not isinstance(quantile, int | float | np.number):
        raise ValueError(f"quantile must be a number in (0, 1], got {quantile!r}")
    if not (math.isfinite(float(quantile)) and 0.0 < float(quantile) <= 1.0):
        raise ValueError(f"quantile must be in (0, 1], got {quantile!r}")
    min_names = _check_int("min_names", min_names, 1)
    if isinstance(max_weight, bool) or not isinstance(max_weight, int | float | np.number):
        raise ValueError(f"max_weight must be a number in (0, 1], got {max_weight!r}")
    if not (math.isfinite(float(max_weight)) and 0.0 < float(max_weight) <= 1.0):
        raise ValueError(f"max_weight must be in (0, 1], got {max_weight!r}")
    vol: np.ndarray | None = None
    if inverse_vol is not None:
        vol = _floats("inverse_vol", inverse_vol)
        _check_aligned("inverse_vol", inverse_vol, "scores", scores)

    q = float(quantile)
    cap = float(max_weight)
    out = np.full(s.shape, np.nan)
    for i in np.flatnonzero(schedule.to_numpy(dtype=bool)):
        row = np.zeros(s.shape[1])
        eligible = mk[i] & np.isfinite(s[i])
        if vol is not None:
            with np.errstate(invalid="ignore"):
                eligible &= np.isfinite(vol[i]) & (vol[i] > 0)
        cols = np.flatnonzero(eligible)
        k = cols.size
        if k >= min_names:
            n = max(math.ceil(q * k - _CEIL_TOL), min_names)
            order = cols[np.argsort(-s[i, cols], kind="stable")]  # ties: earlier column first
            picked = order[:n]
            if vol is None:
                w = np.full(n, 1.0 / n)
            else:
                inv = 1.0 / vol[i, picked]
                w = inv / inv.sum()
            row[picked] = _cap_sum_at_one(np.minimum(w, cap))
        out[i] = row
    return _like(scores, out)
