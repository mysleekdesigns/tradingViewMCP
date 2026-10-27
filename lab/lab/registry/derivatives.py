"""Derivatives risk-filter features for the plan registry (PLAN.md Phase 12).

Six features read USD-M futures archive series that the data layer has already aligned to the
spot bar's *close* (``lab.data.derivatives_align``): a funding settlement, premium/mark/index
kline or metrics row is visible only at bars whose close is at or after its own timestamp. The
aligned values arrive as ``drv_*`` columns on the candles frame (names below), so every
feature here is a trailing calculation on rows at or before bar t, and
``tests/test_registry_lookahead.py`` runs the perturbation test on each of them.

These are **exposure or crash-risk filters, not direction signals**: crowded positive funding,
a rich basis or a jump in open interest says leverage is building, not which way price goes
next. The plan validator only accepts them as extra ``and`` conjuncts of a spot long-only
entry rule.

Gaps: a missing archive value is NaN in its ``drv_*`` column and stays NaN in every output
that reads it (never forward-filled). A window that contains a NaN is NaN. Windows count
rows (bars), not wall-clock time, like every other registry feature.

Pure module: DataFrames in, DataFrames out, no I/O, no globals mutated.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import numpy as np
import pandas as pd

from lab.registry.features import MAX_PERIOD, make_spec
from lab.types import FeatureSpec, ParamSpec

DRV_FUNDING_RATE = "drv_funding_rate"  # fundingRate.funding_rate aligned to bar close
DRV_PREMIUM_CLOSE = "drv_premium_close"  # premiumIndexKlines close
DRV_MARK_CLOSE = "drv_mark_close"  # markPriceKlines close
DRV_INDEX_CLOSE = "drv_index_close"  # indexPriceKlines close
DRV_OI = "drv_oi"  # metrics.sum_open_interest (contracts, not USD value)
DRV_LS_RATIO = "drv_ls_ratio"  # metrics.count_long_short_ratio (global accounts)

DERIVATIVES_COLUMNS: tuple[str, ...] = (
    DRV_FUNDING_RATE,
    DRV_PREMIUM_CLOSE,
    DRV_MARK_CLOSE,
    DRV_INDEX_CLOSE,
    DRV_OI,
    DRV_LS_RATIO,
)

# registry feature -> the drv_* columns it reads (the data layer attaches exactly these)
DERIVATIVES_INPUTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "funding_rate": (DRV_FUNDING_RATE,),
        "funding_zscore": (DRV_FUNDING_RATE,),
        "premium_index": (DRV_PREMIUM_CLOSE,),
        "basis_pct": (DRV_MARK_CLOSE, DRV_INDEX_CLOSE),
        "oi_change": (DRV_OI,),
        "long_short_ratio": (DRV_LS_RATIO,),
    }
)

DERIVATIVES_FEATURES: frozenset[str] = frozenset(DERIVATIVES_INPUTS)

FILTER_NOTE = "exposure or crash-risk filter, not a direction signal"
DOWNLOAD_HINT = (
    "cd lab && uv run python -m lab.data.derivatives download <SYMBOL> "
    "--interval <INTERVAL> --start YYYY-MM --end YYYY-MM"
)

_VALUE = ("value",)

# A population z-score over p values is bounded by sqrt(p - 1) (one outlier among p - 1 equal
# values), so for p <= 5 a threshold like ``fz < 2`` is true on every bar: a constant gate. From
# 12 rows the bound is 3.3, above any usual threshold, and 12 is 2 days of 4h bars.
FUNDING_ZSCORE_MIN_PERIOD = 12


def _drv(candles: pd.DataFrame, column: str) -> np.ndarray:
    """The aligned ``column`` as float64.

    Raises ``ValueError`` naming the column and the download CLI when it is absent.
    """
    if column not in candles.columns:
        raise ValueError(
            f"candles have no {column!r} column: derivatives data is not loaded for this "
            f"symbol/interval; download it with `{DOWNLOAD_HINT}`"
        )
    return candles[column].to_numpy(dtype=np.float64)


def _frame(candles: pd.DataFrame, value: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(
        {"value": np.asarray(value, dtype=np.float64)}, index=candles.index, columns=list(_VALUE)
    )


def _lagged(x: np.ndarray, period: int) -> np.ndarray:
    """``x`` shifted down by ``period`` rows (row t holds ``x[t - period]``), NaN-padded."""
    out = np.full_like(x, np.nan)
    if x.size > period:
        out[period:] = x[:-period]
    return out


# --------------------------------------------------------------------------- features


def _funding_rate(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    return _frame(candles, _drv(candles, DRV_FUNDING_RATE))


def _funding_zscore(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = int(params["period"])
    funding = _drv(candles, DRV_FUNDING_RATE)
    roll = pd.Series(funding, dtype=np.float64).rolling(p, min_periods=p)
    mean = roll.mean().to_numpy()
    std = roll.std(ddof=0).to_numpy()
    flat = (roll.max() == roll.min()).to_numpy()  # exact test; NaN windows compare False
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (funding - mean) / std
    z = np.where(flat, 0.0, z)  # a flat window: funding sits exactly on its mean
    z[np.isnan(mean)] = np.nan  # warm-up and any window holding a NaN (a gap) stay NaN
    return _frame(candles, z)


def _premium_index(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    return _frame(candles, _drv(candles, DRV_PREMIUM_CLOSE))


def _basis_pct(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    mark = _drv(candles, DRV_MARK_CLOSE)
    index = _drv(candles, DRV_INDEX_CLOSE)
    with np.errstate(divide="ignore", invalid="ignore"):
        basis = 100.0 * (mark - index) / index
    basis[~(index > 0.0)] = np.nan  # index <= 0 or NaN; a NaN mark already gives NaN
    return _frame(candles, basis)


def _oi_change(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    p = int(params["period"])
    oi = _drv(candles, DRV_OI)
    past = _lagged(oi, p)
    with np.errstate(divide="ignore", invalid="ignore"):
        change = 100.0 * (oi / past - 1.0)
    change[~(past > 0.0)] = np.nan  # past <= 0 or NaN; a NaN current value already gives NaN
    return _frame(candles, change)


def _long_short_ratio(candles: pd.DataFrame, params: Mapping[str, float]) -> pd.DataFrame:
    return _frame(candles, _drv(candles, DRV_LS_RATIO))


# --------------------------------------------------------------------------- specs

FUNDING_RATE = make_spec(
    "funding_rate",
    "USD-M perpetual funding rate (fraction per settlement, 0.0001 = 0.01%) of the latest "
    "settlement at or before bar t's close; positive means longs pay shorts (crowded longs). "
    f"An {FILTER_NOTE}.",
    (),
    _VALUE,
    _funding_rate,
    lambda _p: 0,
)

FUNDING_ZSCORE = make_spec(
    "funding_zscore",
    "Population z-score of the funding rate over the last `period` bars including bar t "
    "(180 = 30 days of 4h bars); 0 when the window is flat, NaN when the window holds a gap. "
    f"An {FILTER_NOTE}.",
    (
        ParamSpec(
            "period",
            "int",
            180,
            FUNDING_ZSCORE_MIN_PERIOD,
            MAX_PERIOD,
            "Trailing window in bars (30 days of 4h bars).",
        ),
    ),
    _VALUE,
    _funding_zscore,
    lambda p: p["period"] - 1,
)

PREMIUM_INDEX = make_spec(
    "premium_index",
    "USD-M premium index (fraction, (perp - index) / index as Binance computes it for funding) "
    f"from the premium-index kline closing at or before bar t's close. An {FILTER_NOTE}.",
    (),
    _VALUE,
    _premium_index,
    lambda _p: 0,
)

BASIS_PCT = make_spec(
    "basis_pct",
    "Perpetual basis in percent, 100 x (mark - index) / index from the mark- and index-price "
    "klines closing at or before bar t's close (0.1 means mark 0.1% over index). "
    f"An {FILTER_NOTE}.",
    (),
    _VALUE,
    _basis_pct,
    lambda _p: 0,
)

OI_CHANGE = make_spec(
    "oi_change",
    "Trailing change in USD-M open interest (contracts) over `period` bars in percent "
    "(6 = 1 day of 4h bars; 5.0 means +5%), 100 x (oi / oi[period bars ago] - 1). "
    f"An {FILTER_NOTE}.",
    (ParamSpec("period", "int", 6, 1, MAX_PERIOD, "Look-back in bars (6 = 1 day of 4h bars)."),),
    _VALUE,
    _oi_change,
    lambda p: p["period"],
)

LONG_SHORT_RATIO = make_spec(
    "long_short_ratio",
    "Binance global long/short account ratio (accounts net long / accounts net short) of the "
    f"latest 5-minute metrics row at or before bar t's close. An {FILTER_NOTE}.",
    (),
    _VALUE,
    _long_short_ratio,
    lambda _p: 0,
)

DERIVATIVES_FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FUNDING_RATE,
    FUNDING_ZSCORE,
    PREMIUM_INDEX,
    BASIS_PCT,
    OI_CHANGE,
    LONG_SHORT_RATIO,
)

__all__ = [
    "BASIS_PCT",
    "DERIVATIVES_COLUMNS",
    "DERIVATIVES_FEATURES",
    "DERIVATIVES_FEATURE_SPECS",
    "DERIVATIVES_INPUTS",
    "DOWNLOAD_HINT",
    "DRV_FUNDING_RATE",
    "DRV_INDEX_CLOSE",
    "DRV_LS_RATIO",
    "DRV_MARK_CLOSE",
    "DRV_OI",
    "DRV_PREMIUM_CLOSE",
    "FILTER_NOTE",
    "FUNDING_RATE",
    "FUNDING_ZSCORE",
    "FUNDING_ZSCORE_MIN_PERIOD",
    "LONG_SHORT_RATIO",
    "OI_CHANGE",
    "PREMIUM_INDEX",
]
