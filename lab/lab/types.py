"""Shared types for the lab. Every package codes against these; change them only in one place.

Conventions (PLAN.md Phase 7, `.claude/rules/no-lookahead.md`):

* **Candles** are a ``pd.DataFrame`` indexed by a tz-aware UTC ``DatetimeIndex`` named ``"time"``
  (the bar's *open* time), strictly increasing and unique, with float64 columns
  ``open, high, low, close, volume`` and a bool column ``tradable``.
  Missing bars are **absent** (never forward-filled, never NaN rows). Bars the exchange
  printed with zero volume are kept but ``tradable=False``; the engine never fills on them.
* A **signal** at bar t is computed from data through bar t's close. The engine fills it at
  the open of the next tradable bar (t+1 or later). Signals are bool Series on the candle index.
* **Annualisation** uses 365 days: ``bars_per_year(interval) = 365 * 86400 / interval_seconds``.
* All money amounts are in quote currency (USDT). Rates are fractions (0.001 = 0.1%).
* JSON (ledger, API): non-finite floats (NaN, inf) are encoded as ``null``.

Results are historical and not forward-looking.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

CANDLE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
PRICE_INPUTS: tuple[str, ...] = CANDLE_COLUMNS  # names a plan expression may use directly

SECONDS_PER_DAY = 86_400
DAYS_PER_YEAR = 365  # crypto trades every day; never 252

INTERVAL_SECONDS: Mapping[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1_800,
    "1h": 3_600,
    "2h": 7_200,
    "4h": 14_400,
    "6h": 21_600,
    "8h": 28_800,
    "12h": 43_200,
    "1d": 86_400,
}

Interval = str  # one of INTERVAL_SECONDS
Direction = Literal["long", "short"]
ExitReason = Literal["signal", "stop_loss", "take_profit", "end"]
WindowKind = Literal["design", "walk_forward_train", "walk_forward_test", "holdout", "custom"]
TrialSource = Literal["manual", "api", "optuna", "neighbourhood", "adversarial", "benchmark"]
TrialStatus = Literal["ok", "error"]
# "signal": an edge plan (long_entry / long_exit / risk). "overlay": a risk overlay on
# buy-and-hold of one symbol (PLAN.md Phase 15): flat at bar t+1's open iff its ``flat_when``
# rule holds at bar t's close, otherwise 100% invested; exposure is 0 or 1 only.
PlanKind = Literal["signal", "overlay"]


def interval_seconds(interval: Interval) -> int:
    """Seconds per bar; raises ``ValueError`` naming the interval if it is unknown."""
    try:
        return INTERVAL_SECONDS[interval]
    except KeyError:
        raise ValueError(
            f"unknown interval {interval!r}; expected one of {sorted(INTERVAL_SECONDS)}"
        ) from None


def bars_per_year(interval: Interval) -> float:
    """Bars in a 365-day year for ``interval`` (4h -> 2190.0)."""
    return DAYS_PER_YEAR * SECONDS_PER_DAY / interval_seconds(interval)


def validate_candles(candles: pd.DataFrame) -> None:
    """Raise ``ValueError`` if ``candles`` breaks the module's candle convention."""
    missing = [c for c in (*CANDLE_COLUMNS, "tradable") if c not in candles.columns]
    if missing:
        raise ValueError(f"candles missing columns {missing}")
    idx = candles.index
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
        raise ValueError("candles index must be a tz-aware UTC DatetimeIndex")
    if str(idx.tz) != "UTC":
        raise ValueError(f"candles index must be UTC, got {idx.tz}")
    if not idx.is_monotonic_increasing or not idx.is_unique:
        raise ValueError("candles index must be strictly increasing and unique")
    if candles[list(CANDLE_COLUMNS)].isna().to_numpy().any():
        raise ValueError("candles contain NaN prices/volume; missing bars must be absent rows")


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class ParamSpec:
    """One numeric feature parameter. ``low``/``high`` are inclusive validation bounds."""

    name: str
    kind: Literal["int", "float"]
    default: float
    low: float
    high: float
    description: str


FeatureCompute = Callable[[pd.DataFrame, Mapping[str, float]], pd.DataFrame]


@dataclass(frozen=True)
class FeatureSpec:
    """A registry feature.

    ``compute(candles, params)`` returns a DataFrame on ``candles.index`` with exactly the
    columns in ``outputs``; values during warm-up are NaN. Output at bar t depends only on
    candle rows at or before t (enforced by the perturbation test). Params arrive validated
    and filled with defaults. ``warmup(params)`` is the number of leading bars that may be NaN.
    """

    name: str
    description: str
    params: tuple[ParamSpec, ...]
    outputs: tuple[str, ...]  # ("value",) for single-output features
    compute: FeatureCompute
    warmup: Callable[[Mapping[str, float]], int]


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True)
class RiskSpec:
    """Position sizing and protective exits. Percentages are 0-100 like the plan JSON."""

    position_size_pct: float = 100.0  # share of equity per entry, (0, 100]
    stop_loss_pct: float | None = None  # e.g. 3 -> exit when price falls 3% below entry
    take_profit_pct: float | None = None


@dataclass(frozen=True)
class FeatureRef:
    """A plan feature: alias -> registry feature with explicit params."""

    alias: str
    feature: str
    params: Mapping[str, float]


@dataclass(frozen=True)
class Plan:
    """A validated strategy plan (see ``lab.plan``). Long/flat only: Binance spot has no shorts.

    ``kind="overlay"`` (PLAN.md Phase 15) is a risk overlay on buy-and-hold: ``long_exit`` holds
    its ``flat_when`` rule, ``long_entry`` is ``not (flat_when)`` and ``risk`` is always the
    default (100% of equity, no stops). :func:`lab.plan.compile_plan` turns it into
    ``exits = flat_when`` (NaN -> False) and ``entries = ~exits``, so a missing value keeps the
    position invested (it falls back to the base). ``kind`` is the LAST field with a default so
    every existing constructor call is unchanged.
    """

    name: str
    symbol: str
    interval: Interval
    features: tuple[FeatureRef, ...]
    long_entry: str
    long_exit: str
    risk: RiskSpec = field(default_factory=RiskSpec)
    description: str = ""
    kind: PlanKind = "signal"


@dataclass(frozen=True)
class PlanError:
    """One validation problem. ``path`` is a JSON path like ``features.ema_fast.ema.period``."""

    path: str
    message: str


@dataclass(frozen=True)
class CompiledSignals:
    """Output of ``lab.plan.compile_plan``: bool signals at bar close plus the feature frame."""

    entries: pd.Series  # bool, True where long_entry holds at bar t's close
    exits: pd.Series  # bool
    features: pd.DataFrame  # columns "alias" or "alias.output"
    warmup_bars: int  # leading bars where signals are forced False


# --------------------------------------------------------------------------- engine


@dataclass(frozen=True)
class CostModel:
    """Costs per side on traded value. Defaults come from ``config/lab.toml``."""

    fee_rate: float = 0.001  # Binance spot taker fee, 0.1%
    slippage_rate: float = 0.0005  # base slippage, 0.05% per side
    # Extra slippage per unit of (bar range / close) above its trailing median; 0 = flat.
    slippage_vol_mult: float = 0.0
    # Upper bound on the per-side slippage fraction. Default 1.0 = uncapped so an explicit
    # CostModel means what it says; config/lab.toml sets the research cap.
    slippage_cap: float = 1.0

    def scaled(self, factor: float) -> CostModel:
        """Every cost multiplied by ``factor`` (cost stress x2, x4)."""
        if factor < 0:
            raise ValueError(f"cost factor must be >= 0, got {factor}")
        return CostModel(
            self.fee_rate * factor,
            self.slippage_rate * factor,
            self.slippage_vol_mult * factor,
            self.slippage_cap * factor,
        )


ZERO_COSTS = CostModel(0.0, 0.0, 0.0)


@dataclass(frozen=True)
class Trade:
    direction: Direction
    entry_time: pd.Timestamp  # bar whose open filled the entry
    entry_price: float  # fill price including slippage
    exit_time: pd.Timestamp | None  # None if still open at the window end (marked "end")
    exit_price: float | None
    size: float  # units of base asset
    pnl: float  # net of fees and slippage, quote currency
    return_pct: float  # net return on the entry notional, fraction (0.02 = +2%)
    fees: float  # fees paid on both sides, quote currency
    exit_reason: ExitReason
    bars_held: int


@dataclass(frozen=True)
class BacktestResult:
    """Engine output on the candles' index. ``returns``/``gross_returns`` are per-bar simple
    returns of equity marked at each bar's close; bar 0's return is 0."""

    equity: pd.Series
    returns: pd.Series  # net of costs
    gross_returns: pd.Series  # same orders with ZERO_COSTS
    position: pd.Series  # exposure as a fraction of equity at bar close (0..1 long, <0 short)
    trades: tuple[Trade, ...]
    fees_paid: float
    init_cash: float


# --------------------------------------------------------------------------- stats


@dataclass(frozen=True)
class Metrics:
    """Headline metrics. Ratios annualised with 365 days x bars/day; fractions, not percent."""

    total_return: float
    cagr: float
    volatility: float  # annualised stdev of per-bar returns
    sharpe: float  # annualised, rf = 0
    sortino: float
    max_drawdown: float  # negative fraction, e.g. -0.25
    win_rate: float  # share of closed trades with pnl > 0; NaN with no trades
    profit_factor: float  # gross wins / gross losses over trades; inf with no losses
    n_trades: int
    exposure: float  # share of bars with a non-zero position
    turnover: float  # annualised one-way turnover (sum |delta position| per year)
    bars: int


# --------------------------------------------------------------------------- splits + ledger


@dataclass(frozen=True)
class Window:
    """Half-open time window [start, end) on bar open times."""

    start: pd.Timestamp
    end: pd.Timestamp
    kind: WindowKind


@dataclass(frozen=True)
class WalkForwardFold:
    index: int
    train: Window
    test: Window


@dataclass(frozen=True)
class TrialRecord:
    """One ledger row. ``id``/``created_at`` are set by the ledger on insert."""

    run_id: str
    source: TrialSource
    status: TrialStatus
    plan_hash: str  # sha256 of the canonical plan JSON (sorted keys, no whitespace)
    plan_json: str
    params_json: str  # the tuned parameters for this trial, "{}" if none
    symbol: str
    interval: Interval
    window_kind: WindowKind
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    metrics_json: str  # json of asdict(Metrics); non-finite floats as null; "{}" on error
    sharpe_per_bar: float | None  # NON-annualised Sharpe, the statistic DSR needs
    n_trades: int
    error: str | None = None
    # json of the CostModel the trial ran with (asdict); "{}" for summary rows without one.
    costs_json: str = "{}"
    returns: np.ndarray | None = None  # per-bar net returns, stored compressed (for PBO)
    id: int | None = None
    created_at: pd.Timestamp | None = None
