"""Pre-registered risk controls and position sizing for FUTURE paper tests (PLAN.md Phase 20).

Parts E and E2: the pure logic behind a paper protocol's ``risk_controls`` and ``sizing`` blocks.
Everything here is dry-run bookkeeping: this module reads no exchange, holds no key, and places
nothing. The only file access is the TOML loader and the ``halt.lock`` functions.

Rules the code enforces:

* **Sizing is pre-registered.** A :class:`Sizing` is frozen before a test starts and never changes
  mid-test; a different size is a new test. Sizing controls risk. It does not create an edge and
  never rescues a plan that fails the lab.
* **Stake.** ``wallet * risk_per_trade_pct / 100 / stop_distance`` capped at ``max_stake_pct`` of a
  frozen wallet, so stakes never compound. With a volatility target the stake also scales by
  ``min(1, vol_target / realized_vol)`` and is clipped to ``[min_stake_pct, max_stake_pct]``. The
  scale is capped at 1 so that low volatility can never push risk above the registered value:
  volatility only ever shrinks or caps risk. "More exposure when implied volatility is high"
  (Sosnoff) is specific to selling options and is not adopted.
* **Conviction cap.** ``size_multiplier`` is in [1, 2]. It is a frozen number; no LLM (Jev or
  Claude) sets or changes it (judges watch and never steer).
* **No leverage.** ``max_total_exposure_pct <= 100`` and ``max_open_trades * max_stake_pct`` must
  fit inside it, with :data:`MIN_FUNDING_HEADROOM_PCT` to spare so that a loss cannot clip a stake.
* **Stakes an exchange takes.** ``min_stake_pct`` must give at least :data:`MIN_EXCHANGE_STAKE`.
* **Size steps need proof.** See :func:`check_size_step`: the very same plan, proven by its own
  pass, at a stake it can really reach and the prior could not.
* **The config is frozen too.** :class:`PaperConfig` is what ``config.json`` is rendered from; a
  version 8 test refuses any other file (``paper_trade.py``).
* **No averaging down.** See :func:`check_no_averaging_down`.
* **Circuit breaker.** Thresholds and the wallet they are measured from are frozen together.
  :func:`evaluate_breaker` finds the first bar at or beyond a threshold; :func:`write_halt_lock`
  records it; :func:`check_not_halted` refuses to continue. No delete function exists anywhere:
  only the owner removes ``halt.lock``, outside Claude Code.

Annualisation follows the crypto calendar: ``bars_per_year`` always comes from 365 days.
"""

from __future__ import annotations

import ast
import contextlib
import json
import math
import os
import re
import tempfile
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import MISSING, asdict, dataclass, fields
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HALT_LOCK = "halt.lock"
UNREADABLE_LOCK_REASON = "unreadable lock file"

RISK_PER_TRADE_MIN_PCT = 0.5  # PLAN.md Part E2: one value in 0.5-1.5% of the paper wallet
RISK_PER_TRADE_MAX_PCT = 1.5
MAX_SIZE_MULTIPLIER = 2.0  # PLAN.md Part E2: conviction is worth at most 2x base size
MAX_EXPOSURE_PCT = 100.0  # no leverage
MIN_PRIOR_DAYS = 90.0  # PLAN.md Part E2: at least 3 months at a size before stepping up
# Sized tests' stake_amount. A numeric one is checked against the balance BEFORE
# custom_stake_amount runs and raises when the balance is below it, which silently drops entries.
UNLIMITED_STAKE = "unlimited"
MAX_SIZE_STEP = 2.0  # PLAN.md Part E2: one 2x step at a time
# A full book at the maximum stake stays fundable after this loss (20 straight full stops at the
# 1% middle risk). Below it, freqtrade clips a stake to the balance and compare can never match it.
MIN_FUNDING_HEADROOM_PCT = 20.0
# Binance spot's 5 USDT NOTIONAL minimum x freqtrade's largest stop-loss reserve (1.5): below this
# freqtrade raises the stake or drops the entry, so the bot would not trade the registered size.
MIN_EXCHANGE_STAKE = 7.5

_SECONDS_PER_YEAR = 365 * 86_400  # crypto calendar
_BAR_STEP_TOLERANCE = 1e-9  # relative; only absorbs float rounding of 365d / bars_per_year
_THRESHOLD_TOLERANCE = 1e-12  # relative; a loss exactly at a threshold must not miss on rounding


# --------------------------------------------------------------------------- validation helpers


def _number(owner: str, name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{owner}.{name} must be a finite number, got {value!r}")
    return float(value)


def _positive(owner: str, name: str, value: Any) -> float:
    number = _number(owner, name, value)
    if number <= 0:
        raise ValueError(f"{owner}.{name} must be > 0, got {value!r}")
    return number


def _count(owner: str, name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{owner}.{name} must be an integer >= 1, got {value!r}")
    return value


def _normalise(instance: Any, name: str, value: Any) -> None:
    """Store a validated value on a frozen dataclass (ints become floats for a stable hash)."""
    object.__setattr__(instance, name, value)


def _build[T](cls: type[T], label: str, data: Any) -> T:
    """Construct ``cls`` from a mapping; an unknown or missing key is a ``ValueError``."""
    if not isinstance(data, Mapping):
        raise ValueError(f"{label} must be a table, got {data!r}")
    declared = fields(cls)
    unknown = sorted(set(data) - {f.name for f in declared})
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(map(str, unknown))}")
    missing = [f.name for f in declared if f.default is MISSING and f.name not in data]
    if missing:
        raise ValueError(f"{label} is missing keys: {', '.join(missing)}")
    return cls(**data)


# --------------------------------------------------------------------------- config dataclasses


@dataclass(frozen=True)
class Protections:
    """Freqtrade protections, frozen per test: candle counts and one drawdown fraction."""

    cooldown_candles: int  # CooldownPeriod stop_duration_candles
    stoploss_guard_lookback_candles: int
    stoploss_guard_trade_limit: int
    stoploss_guard_stop_candles: int
    max_drawdown_lookback_candles: int
    max_drawdown_trade_limit: int
    max_drawdown_stop_candles: int
    max_drawdown_allowed: float  # fraction, e.g. 0.2
    # The share of the unprotected replay's entries protections may mask. Above it the test mostly
    # measured the protections, and a final ``inconclusive`` is the best verdict.
    max_blocked_share: float

    def __post_init__(self) -> None:
        fractions = ("max_drawdown_allowed", "max_blocked_share")
        for f in fields(self):
            if f.name not in fractions:
                _count("protections", f.name, getattr(self, f.name))
        allowed = _positive("protections", "max_drawdown_allowed", self.max_drawdown_allowed)
        if allowed >= 1:
            raise ValueError(
                f"protections.max_drawdown_allowed is a fraction below 1 (0.2 = 20%), "
                f"got {self.max_drawdown_allowed!r}"
            )
        _normalise(self, "max_drawdown_allowed", allowed)
        share = _positive("protections", "max_blocked_share", self.max_blocked_share)
        if share >= 1:
            raise ValueError(
                f"protections.max_blocked_share is a fraction below 1 (0.33 = a third), got "
                f"{self.max_blocked_share!r}: at 1 or more it would cap nothing"
            )
        _normalise(self, "max_blocked_share", share)


@dataclass(frozen=True)
class CircuitBreaker:
    """Portfolio stop in percent: loss within a UTC day, and drawdown from the equity peak.

    ``wallet`` is required: thresholds mean nothing without the equity they are measured from, and
    a wallet read from a file outside the protocol's sha256 could move the bar after registration.
    """

    max_daily_loss_pct: float  # > 0
    max_drawdown_pct: float  # > 0
    wallet: float  # > 0, frozen paper wallet; guard and compare read only this one

    def __post_init__(self) -> None:
        for name in ("max_daily_loss_pct", "max_drawdown_pct"):
            value = _positive("circuit_breaker", name, getattr(self, name))
            if value > 100:
                raise ValueError(f"circuit_breaker.{name} is a percent <= 100, got {value!r}")
            _normalise(self, name, value)
        _normalise(self, "wallet", _positive("circuit_breaker", "wallet", self.wallet))


@dataclass(frozen=True)
class RiskControls:
    """The protocol's ``risk_controls`` block; at least one of the two must be set."""

    protections: Protections | None
    circuit_breaker: CircuitBreaker | None

    def __post_init__(self) -> None:
        if self.protections is None and self.circuit_breaker is None:
            raise ValueError("risk_controls needs protections, circuit_breaker, or both")


@dataclass(frozen=True)
class Sizing:
    """The protocol's ``sizing`` block. Every refusal names the rule it enforces."""

    risk_per_trade_pct: float  # 0.5 <= x <= 1.5, else ValueError
    justification: str  # required, non-empty, one line
    wallet: float  # frozen paper wallet; stakes never compound
    max_stake_pct: float
    min_stake_pct: float
    max_total_exposure_pct: float  # <= 100: no leverage
    max_open_trades: int
    size_multiplier: float = 1.0  # refused above 2 and below 1
    vol_target_annual: float | None = None  # both vol fields set, or both None
    vol_window_bars: int | None = None
    prior_test: str | None = None  # candidate name of the smaller-size test

    def __post_init__(self) -> None:
        risk = _number("sizing", "risk_per_trade_pct", self.risk_per_trade_pct)
        if not RISK_PER_TRADE_MIN_PCT <= risk <= RISK_PER_TRADE_MAX_PCT:
            raise ValueError(
                f"sizing.risk_per_trade_pct {self.risk_per_trade_pct!r} refused: the rule is one "
                f"value in {RISK_PER_TRADE_MIN_PCT}-{RISK_PER_TRADE_MAX_PCT}% of the paper wallet"
            )
        just = self.justification
        if not isinstance(just, str) or not just.strip() or "\n" in just or "\r" in just:
            raise ValueError(f"sizing.justification must be one non-empty line, got {just!r}")
        wallet = _number("sizing", "wallet", self.wallet)
        if wallet <= 0:
            raise ValueError(f"sizing.wallet must be > 0, got {self.wallet!r}")
        max_stake = _positive("sizing", "max_stake_pct", self.max_stake_pct)
        min_stake = _positive("sizing", "min_stake_pct", self.min_stake_pct)
        if wallet * min_stake / 100 < MIN_EXCHANGE_STAKE:
            raise ValueError(
                f"sizing.min_stake_pct {min_stake:g} refused: {min_stake:g}% of the {wallet:g} "
                f"wallet is below the {MIN_EXCHANGE_STAKE:g} stake an exchange order needs"
            )
        if min_stake > max_stake:
            raise ValueError(
                f"sizing.min_stake_pct {min_stake} > max_stake_pct {max_stake}: the stake clip "
                "needs min_stake_pct <= max_stake_pct"
            )
        exposure = _positive("sizing", "max_total_exposure_pct", self.max_total_exposure_pct)
        if exposure > MAX_EXPOSURE_PCT:
            raise ValueError(
                f"sizing.max_total_exposure_pct {self.max_total_exposure_pct!r} refused: the rule "
                f"is no leverage (at most {MAX_EXPOSURE_PCT:g}%)"
            )
        trades = _count("sizing", "max_open_trades", self.max_open_trades)
        if trades * max_stake > exposure:
            raise ValueError(
                f"sizing.max_open_trades {trades} x max_stake_pct {max_stake} = "
                f"{trades * max_stake:g}% is above max_total_exposure_pct {exposure:g}: the rule "
                "is an exposure cap that every full book must fit inside"
            )
        fundable = exposure * (1 - MIN_FUNDING_HEADROOM_PCT / 100)
        if trades * max_stake > fundable * (1 + _THRESHOLD_TOLERANCE):
            raise ValueError(
                f"sizing.max_open_trades {trades} x max_stake_pct {max_stake} = "
                f"{trades * max_stake:g}% is above {fundable:g}%: the rule is a full book that "
                f"stays fundable through a {MIN_FUNDING_HEADROOM_PCT:g}% loss of the "
                f"max_total_exposure_pct {exposure:g}, or freqtrade clips the stake"
            )
        multiplier = _number("sizing", "size_multiplier", self.size_multiplier)
        if not 1 <= multiplier <= MAX_SIZE_MULTIPLIER:
            raise ValueError(
                f"sizing.size_multiplier {self.size_multiplier!r} refused: the conviction cap "
                f"rule allows 1 to {MAX_SIZE_MULTIPLIER:g} x base size"
            )
        if self.prior_test is not None and (
            not isinstance(self.prior_test, str) or not self.prior_test.strip()
        ):
            raise ValueError(f"sizing.prior_test must be a test name, got {self.prior_test!r}")
        if multiplier > 1 and self.prior_test is None:
            raise ValueError(
                f"sizing.size_multiplier {multiplier:g} refused: the size-step rule needs "
                "prior_test (proof at a smaller size comes first)"
            )
        if (self.vol_target_annual is None) != (self.vol_window_bars is None):
            raise ValueError(
                "sizing.vol_target_annual and sizing.vol_window_bars must both be set or both "
                f"be absent, got {self.vol_target_annual!r} and {self.vol_window_bars!r}"
            )
        if self.vol_target_annual is not None:
            target = _positive("sizing", "vol_target_annual", self.vol_target_annual)
            _normalise(self, "vol_target_annual", target)
            if _count("sizing", "vol_window_bars", self.vol_window_bars) < 2:
                raise ValueError(
                    f"sizing.vol_window_bars must be >= 2 (a sample sd needs two returns), "
                    f"got {self.vol_window_bars!r}"
                )
        for name, value in (
            ("risk_per_trade_pct", risk),
            ("wallet", wallet),
            ("max_stake_pct", max_stake),
            ("min_stake_pct", min_stake),
            ("max_total_exposure_pct", exposure),
            ("size_multiplier", multiplier),
        ):
            _normalise(self, name, value)


@dataclass(frozen=True)
class PaperConfig:
    """The ``prepare`` settings a version 8 test freezes: ``config.json`` is rendered from these
    and nothing else, so ``fee``, ``dry_run_wallet`` or ``stake_amount`` cannot move outside the
    protocol's sha256 even for a test that freezes only protections or only a breaker."""

    exchange: str
    public_api_host: str | None
    stake_amount: float | str  # a fixed stake, or UNLIMITED_STAKE with a sizing block
    dry_run_wallet: float
    max_open_trades: int

    def __post_init__(self) -> None:
        if not isinstance(self.exchange, str) or not self.exchange:
            raise ValueError(f"paper_config.exchange must be a name, got {self.exchange!r}")
        host = self.public_api_host
        if host is not None and (not isinstance(host, str) or not host):
            raise ValueError(f"paper_config.public_api_host must be a host, got {host!r}")
        wallet = _positive("paper_config", "dry_run_wallet", self.dry_run_wallet)
        _normalise(self, "dry_run_wallet", wallet)
        _count("paper_config", "max_open_trades", self.max_open_trades)
        if self.stake_amount != UNLIMITED_STAKE:
            stake = _positive("paper_config", "stake_amount", self.stake_amount)
            if stake > wallet:
                raise ValueError(
                    f"paper_config.stake_amount {stake:g} is above the wallet {wallet:g}"
                )
            _normalise(self, "stake_amount", stake)


@dataclass(frozen=True)
class RiskConfig:
    """Parsed ``paper_risk`` TOML: either block may be absent, never both in one file."""

    risk_controls: RiskControls | None
    sizing: Sizing | None

    def __post_init__(self) -> None:
        check_one_wallet(self.risk_controls, self.sizing)


def frozen_wallet(controls: RiskControls | None, sizing: Sizing | None) -> float | None:
    """The wallet the blocks froze, or None when neither did (protections alone freeze none)."""
    breaker = None if controls is None else controls.circuit_breaker
    if breaker is not None:
        return breaker.wallet
    return None if sizing is None else sizing.wallet


def check_one_wallet(controls: RiskControls | None, sizing: Sizing | None) -> None:
    """One test has one wallet: the breaker's and the sizing block's must be the same number."""
    breaker = None if controls is None else controls.circuit_breaker
    if breaker is not None and sizing is not None and breaker.wallet != sizing.wallet:
        raise ValueError(
            f"circuit_breaker.wallet {breaker.wallet:g} differs from sizing.wallet "
            f"{sizing.wallet:g}: one test has one frozen wallet"
        )


def check_frozen_config(
    config: PaperConfig, controls: RiskControls | None, sizing: Sizing | None
) -> None:
    """A frozen config must say what the frozen blocks say: one wallet, and with sizing the
    settings :func:`research.paper.freqtrade.render_config` gives a sized bot."""
    wallet = frozen_wallet(controls, sizing)
    if wallet is not None and config.dry_run_wallet != wallet:
        raise ValueError(
            f"paper_config.dry_run_wallet {config.dry_run_wallet:g} differs from the frozen "
            f"wallet {wallet:g}: one test has one frozen wallet"
        )
    trades = 1 if sizing is None else sizing.max_open_trades
    if config.max_open_trades != trades:
        raise ValueError(
            f"paper_config.max_open_trades {config.max_open_trades} must be {trades} here"
        )
    if (config.stake_amount == UNLIMITED_STAKE) != (sizing is not None):
        raise ValueError(
            f"paper_config.stake_amount {config.stake_amount!r} refused: {UNLIMITED_STAKE!r} goes "
            "with a sizing block and only with one"
        )


# --------------------------------------------------------------------------- (de)serialisation


def risk_controls_to_dict(c: RiskControls) -> dict[str, Any]:
    """JSON-ready dict with both keys; an unset block is ``None``."""
    return {
        "protections": None if c.protections is None else asdict(c.protections),
        "circuit_breaker": None if c.circuit_breaker is None else asdict(c.circuit_breaker),
    }


def risk_controls_from_dict(d: Mapping[str, Any]) -> RiskControls:
    """Strict inverse of :func:`risk_controls_to_dict`; a block may be ``None`` or absent."""
    if not isinstance(d, Mapping):
        raise ValueError(f"risk_controls must be a table, got {d!r}")
    unknown = sorted(set(d) - {"protections", "circuit_breaker"})
    if unknown:
        raise ValueError(f"risk_controls has unknown keys: {', '.join(map(str, unknown))}")
    protections, breaker = d.get("protections"), d.get("circuit_breaker")
    return RiskControls(
        protections=None
        if protections is None
        else _build(Protections, "protections", protections),
        circuit_breaker=None
        if breaker is None
        else _build(CircuitBreaker, "circuit_breaker", breaker),
    )


def sizing_to_dict(s: Sizing) -> dict[str, Any]:
    """JSON-ready dict with every field; unset optional fields are ``None``."""
    return asdict(s)


def sizing_from_dict(d: Mapping[str, Any]) -> Sizing:
    """Strict inverse of :func:`sizing_to_dict`; optional fields may be ``None`` or absent."""
    return _build(Sizing, "sizing", d)


def paper_config_to_dict(c: PaperConfig) -> dict[str, Any]:
    """JSON-ready dict with every field."""
    return asdict(c)


def paper_config_from_dict(d: Mapping[str, Any]) -> PaperConfig:
    """Strict inverse of :func:`paper_config_to_dict`."""
    return _build(PaperConfig, "paper_config", d)


def load_risk_config(path: Path | str) -> RiskConfig:
    """Parse a ``paper_risk`` TOML file strictly: an unknown key or an empty file is refused."""
    source = Path(path)
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # TOMLDecodeError and UnicodeDecodeError are ValueErrors
        raise ValueError(f"cannot read risk config {source}: {exc}") from exc
    unknown = sorted(set(raw) - {"risk_controls", "sizing"})
    if unknown:
        raise ValueError(f"risk config {source} has unknown keys: {', '.join(unknown)}")
    if not raw:
        raise ValueError(f"risk config {source} sets neither risk_controls nor sizing")
    controls = raw.get("risk_controls")
    sizing = raw.get("sizing")
    return RiskConfig(
        risk_controls=None if controls is None else risk_controls_from_dict(controls),
        sizing=None if sizing is None else sizing_from_dict(sizing),
    )


# --------------------------------------------------------------------------- stake arithmetic


def sized_stake(
    *,
    stop_distance: float,
    wallet: float,
    risk_per_trade_pct: float,
    max_stake_pct: float,
    min_stake_pct: float,
    size_multiplier: float = 1.0,
    vol_target_annual: float | None = None,
    realized_vol_annual: float | None = None,
) -> float:
    """Stake for one entry, in wallet currency; embedded verbatim in generated strategies.

    Uses only builtins and ``math`` so ``inspect.getsource`` of this function runs alone and paper
    and backtest share the same stake arithmetic. ``stop_distance`` is a fraction (0.05 = 5%).

    base = wallet * risk_per_trade_pct / 100 / stop_distance. Without a volatility target the
    stake is min(base * size_multiplier, wallet * max_stake_pct / 100). With one, the stake is
    base * min(1, vol_target_annual / realized_vol_annual) * size_multiplier, clipped to the
    [min_stake_pct, max_stake_pct] share of the wallet. The scale stops at 1, so low volatility
    never raises risk above the registered value. A missing, non-finite or non-positive realised
    volatility gives the minimum stake: conservative, and no stale value is carried.
    """
    for name, value in (
        ("stop_distance", stop_distance),
        ("wallet", wallet),
        ("risk_per_trade_pct", risk_per_trade_pct),
        ("max_stake_pct", max_stake_pct),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a finite number > 0, got {value!r}")
    if stop_distance >= 1:
        raise ValueError(f"stop_distance is a fraction below 1 (0.05 = 5%), got {stop_distance!r}")
    if not math.isfinite(min_stake_pct) or not 0 <= min_stake_pct <= max_stake_pct:
        raise ValueError(
            f"min_stake_pct must be in [0, max_stake_pct {max_stake_pct!r}], got {min_stake_pct!r}"
        )
    if not 1 <= size_multiplier <= 2:
        raise ValueError(f"size_multiplier must be in [1, 2], got {size_multiplier!r}")
    base = wallet * risk_per_trade_pct / 100 / stop_distance
    largest = wallet * max_stake_pct / 100
    if vol_target_annual is None:
        return float(min(base * size_multiplier, largest))
    if not math.isfinite(vol_target_annual) or vol_target_annual <= 0:
        raise ValueError(
            f"vol_target_annual must be a finite number > 0, got {vol_target_annual!r}"
        )
    smallest = wallet * min_stake_pct / 100
    if (
        realized_vol_annual is None
        or not math.isfinite(realized_vol_annual)
        or realized_vol_annual <= 0
    ):
        return float(smallest)
    stake = base * min(1.0, vol_target_annual / realized_vol_annual) * size_multiplier
    return float(max(smallest, min(stake, largest)))


def realized_vol_annual(closes: pd.Series, window: int, bars_per_year: float) -> pd.Series:
    """Rolling annualised volatility of closed bars, indexed like ``closes``.

    The sample sd (ddof=1) of the last ``window`` bar log returns, times sqrt(bars_per_year). A
    value at t reads closes at or before t only, so a stake sized on bar t's value uses nothing
    later. ``window`` returns need ``window + 1`` closes: NaN until then, and NaN while the window
    holds a timestamp gap or a missing or non-positive close (never forward-filled).

    The bar length is 365 days / ``bars_per_year``, not read from the data, which keeps the gap
    test causal: a step longer than a bar is a gap; a shorter one means ``bars_per_year`` is for
    another interval and is a ``ValueError``.
    """
    if not isinstance(closes, pd.Series) or not isinstance(closes.index, pd.DatetimeIndex):
        raise ValueError("closes must be a pandas Series with a DatetimeIndex")
    if isinstance(window, bool) or not isinstance(window, int) or window < 2:
        raise ValueError(f"window must be an integer >= 2, got {window!r}")
    if not math.isfinite(bars_per_year) or bars_per_year <= 0:
        raise ValueError(f"bars_per_year must be a finite number > 0, got {bars_per_year!r}")
    index = closes.index
    if not (index.is_monotonic_increasing and index.is_unique):
        raise ValueError("closes must have a strictly ascending DatetimeIndex")
    bar_ns = _SECONDS_PER_YEAR * 1e9 / bars_per_year
    steps = np.diff(index.as_unit("ns").asi8).astype("float64")
    if (steps < bar_ns * (1 - _BAR_STEP_TOLERANCE)).any():
        raise ValueError(
            f"closes has a step shorter than one bar ({bar_ns / 1e9:g} s): "
            f"bars_per_year {bars_per_year!r} does not match the series"
        )
    n = len(closes)
    vol = np.full(n, np.nan)
    if n > window:
        prices = closes.to_numpy(dtype="float64", na_value=np.nan)
        valid = np.isfinite(prices) & (prices > 0)
        log_close = np.full(n, np.nan)
        log_close[valid] = np.log(prices[valid])
        returns = np.diff(log_close)
        returns[steps > bar_ns * (1 + _BAR_STEP_TOLERANCE)] = np.nan
        windows = np.lib.stride_tricks.sliding_window_view(returns, window)
        vol[window:] = windows.std(axis=1, ddof=1) * math.sqrt(bars_per_year)
    return pd.Series(vol, index=index, name="realized_vol_annual")


# --------------------------------------------------------------------------- pre-start checks


def check_sizing_against_plan(sizing: Sizing, stop_loss_pct: float | None) -> None:
    """Refuse a :class:`Sizing` on a plan without a usable stop, or whose stake the stop breaks.

    ``stop_loss_pct`` is the plan's percent (5 = 5%, ``risk.stop_loss_pct``); the stake needs
    ``stop_distance = stop_loss_pct / 100``. Risk per trade means nothing without a stop. With a
    volatility target the minimum stake may not exceed the base stake, or the floor itself would
    risk more than the registered value. Without one the stake is the base stake, which must reach
    :data:`MIN_EXCHANGE_STAKE`.
    """
    if (
        stop_loss_pct is None
        or isinstance(stop_loss_pct, bool)
        or not isinstance(stop_loss_pct, int | float)
        or not math.isfinite(stop_loss_pct)
        or not 0 < stop_loss_pct < 100
    ):
        raise ValueError(
            f"sizing with risk_per_trade_pct {sizing.risk_per_trade_pct:g}% refused: the rule is "
            f"that sized stakes need a stop, and stop_loss_pct {stop_loss_pct!r} is not in (0, 100)"
        )
    # the two percents cancel: wallet * (risk / 100) / (stop / 100)
    base = sizing.wallet * sizing.risk_per_trade_pct / stop_loss_pct * sizing.size_multiplier
    largest = sizing.wallet * sizing.max_stake_pct / 100
    smallest = sizing.wallet * sizing.min_stake_pct / 100
    if sizing.vol_target_annual is None and min(base, largest) < MIN_EXCHANGE_STAKE:
        raise ValueError(
            f"sizing refused: the stake {min(base, largest):g} is below the "
            f"{MIN_EXCHANGE_STAKE:g} an exchange order needs"
        )
    if sizing.vol_target_annual is not None and smallest > base:
        raise ValueError(
            f"sizing.min_stake_pct {sizing.min_stake_pct:g} refused: its stake {smallest:g} is "
            f"above the base stake {base:g}, so the floor would risk more than "
            f"risk_per_trade_pct {sizing.risk_per_trade_pct:g}% at a {stop_loss_pct:g}% stop"
        )


_MARTINGALE_NAME = re.compile(
    r"martingale|doubl(?:e|ing)_?down|averag(?:e|ing)_?down|(?:^|_)dca(?:_|$)", re.IGNORECASE
)
_NOT_OUTCOME = re.compile(r"stop_?loss|closing", re.IGNORECASE)  # protective stops, closing prices
_OUTCOME = re.compile(r"profit|loss|losing|pnl|drawdown", re.IGNORECASE)
_STAKE = re.compile(r"stake|size|multiplier", re.IGNORECASE)


def _names_read(root: ast.AST) -> Iterator[tuple[str, int]]:
    """Every identifier (and constant subscript key) under ``root``, with its line."""
    for node in ast.walk(root):
        line = getattr(node, "lineno", getattr(root, "lineno", 0))
        match node:
            case ast.Name(id=name) | ast.Attribute(attr=name) | ast.arg(arg=name):
                yield name, line
            case ast.keyword(arg=str() as name):
                yield name, line
            case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                yield name, line
            case ast.ClassDef(name=name):
                yield name, line
            case ast.Subscript(slice=ast.Constant(value=str() as name)):
                yield name, line


def _reads_outcome(root: ast.AST) -> tuple[str, int] | None:
    """The first identifier under ``root`` that reads a trade result, if any."""
    for name, line in _names_read(root):
        if _OUTCOME.search(_NOT_OUTCOME.sub("", name)):
            return name, line
    return None


def _assigned_name(target: ast.AST) -> str | None:
    match target:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
        case ast.Subscript(slice=ast.Constant(value=str() as name)):
            return name
    return None


def _assignments(root: ast.AST) -> Iterator[tuple[str, ast.expr | None, int]]:
    """(name, value, line) of each assignment under ``root``, dict literals included."""
    for node in ast.walk(root):
        match node:
            case ast.Assign(targets=targets, value=value):
                for target in targets:
                    if (name := _assigned_name(target)) is not None:
                        yield name, value, node.lineno
            case (
                ast.AnnAssign(target=target, value=value)
                | ast.AugAssign(target=target, value=value)
            ):
                if (name := _assigned_name(target)) is not None:
                    yield name, value, node.lineno
            case ast.Dict(keys=keys, values=values):
                for key, value in zip(keys, values, strict=True):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        yield key.value, value, node.lineno


def _averaging_down_signal(strategy_source: str) -> str | None:
    """Why ``strategy_source`` is refused, or None. Comments and docstrings are not read."""
    tree = ast.parse(strategy_source)
    for name, value, line in _assignments(tree):
        if name == "position_adjustment_enable" and not (
            isinstance(value, ast.Constant) and not value.value
        ):
            return f"line {line} sets position_adjustment_enable"
        if name == "adjust_trade_position":
            return f"line {line} assigns adjust_trade_position"
    for name, line in _names_read(tree):
        if _MARTINGALE_NAME.search(name):
            return f"line {line} names a Martingale pattern ({name})"
    for node in ast.walk(tree):
        match node:
            case (
                ast.FunctionDef(name="adjust_trade_position", lineno=line)
                | ast.AsyncFunctionDef(name="adjust_trade_position", lineno=line)
            ):
                return f"line {line} defines adjust_trade_position"
            case (
                ast.FunctionDef(name="custom_stake_amount")
                | ast.AsyncFunctionDef(name="custom_stake_amount")
            ):
                if (found := _reads_outcome(node)) is not None:
                    return f"line {found[1]}: custom_stake_amount reads a trade result ({found[0]})"
            case ast.If(test=test, body=body, orelse=orelse):
                if (found := _reads_outcome(test)) is not None:
                    for statement in (*body, *orelse):
                        for name, _, line in _assignments(statement):
                            if _STAKE.search(name):
                                return f"line {line} changes {name} on a trade result ({found[0]})"
    return None


def check_no_averaging_down(strategy_source: str, config: Mapping[str, Any]) -> None:
    """Refuse a strategy that can add to a loser or raise its stake after a loss.

    Whether arbitrary code raises its stake after a loss is not decidable, so this is a static
    screen of three signals, read from the parsed source (comments and docstrings do not count):

    1. ``position_adjustment_enable`` turned on, in ``config`` or in the source;
    2. ``adjust_trade_position`` defined or assigned;
    3. a Martingale pattern: an identifier that names one (martingale, double down, average down,
       DCA), a ``custom_stake_amount`` that reads a trade result (profit, loss, PnL, drawdown;
       protective stop-loss settings do not count), or an ``if`` on a trade result that assigns
       a stake, size or multiplier.

    Limit: logic spread across helpers, or built with ``exec`` or ``setattr``, can pass this
    screen. It stops the known forms and honest mistakes, not deliberate evasion. Generated
    strategies stay safe because ``prepare`` renders them from a fixed template. Source that does
    not parse cannot be screened and is refused.
    """
    rule = "no averaging down rule"
    if config.get("position_adjustment_enable"):
        raise ValueError(f"{rule}: config sets position_adjustment_enable")
    try:
        signal = _averaging_down_signal(strategy_source)
    except SyntaxError as exc:
        raise ValueError(f"{rule}: strategy source does not parse: {exc}") from exc
    if signal is not None:
        raise ValueError(f"{rule}: {signal}")


@dataclass(frozen=True)
class PriorTest:
    """What :func:`check_size_step` reads of the registered test a size step names."""

    name: str
    sizing: Sizing | None
    decision_days: float | None  # the frozen decision horizon
    final_verdict: str | None
    logic_hash: str
    plan_hash: str
    mode: str
    locked_split_check: str
    halted: bool  # ``halt.lock`` sits in its folder
    unlocked: tuple[str, ...]  # other tests that already stepped up from it
    risk_controls: RiskControls | None = None  # what its pass was earned under


def _real_sizes(sizing: Sizing) -> dict[str, float]:
    """Each number a test can scale its size through, in wallet currency (a wallet can grow too:
    a larger wallet at the same percentages is a larger test)."""
    return {
        "risk per trade": sizing.wallet * sizing.risk_per_trade_pct * sizing.size_multiplier,
        "maximum stake": sizing.wallet * sizing.max_stake_pct,
        "minimum stake": sizing.wallet * sizing.min_stake_pct,
    }


def _stake_range(sizing: Sizing, stop_loss_pct: float) -> tuple[float, float]:
    """The smallest and largest stake :func:`sized_stake` can give at this stop. A number that no
    stake reaches is not a size: without a volatility target ``min_stake_pct`` is never read, and
    a ``max_stake_pct`` above the base stake is never reached."""
    # the two percents cancel: wallet * (risk / 100) / (stop / 100)
    base = sizing.wallet * sizing.risk_per_trade_pct / stop_loss_pct * sizing.size_multiplier
    largest = min(base, sizing.wallet * sizing.max_stake_pct / 100)
    if sizing.vol_target_annual is None:
        return largest, largest
    return sizing.wallet * sizing.min_stake_pct / 100, largest


def _check_same_guards(risk_controls: RiskControls | None, prior: PriorTest) -> None:
    """A step may not drop or loosen what kept the prior's losses to what its pass measured."""
    if prior.risk_controls is None:
        return
    name = repr(prior.name)
    guard, breaker = prior.risk_controls.protections, prior.risk_controls.circuit_breaker
    new_guard = None if risk_controls is None else risk_controls.protections
    new_breaker = None if risk_controls is None else risk_controls.circuit_breaker
    if guard is not None and new_guard != guard:
        raise ValueError(
            f"size step rule: prior test {name} passed under protections this step does not keep"
        )
    if breaker is None:
        return
    if new_breaker is None:
        raise ValueError(
            f"size step rule: prior test {name} passed under a circuit breaker this step drops"
        )
    if (
        new_breaker.max_daily_loss_pct > breaker.max_daily_loss_pct
        or new_breaker.max_drawdown_pct > breaker.max_drawdown_pct
    ):
        raise ValueError(
            f"size step rule: the circuit breaker is looser than prior test {name}'s: "
            f"({new_breaker.max_daily_loss_pct:g}%, {new_breaker.max_drawdown_pct:g}%) vs "
            f"({breaker.max_daily_loss_pct:g}%, {breaker.max_drawdown_pct:g}%)"
        )


def check_size_step(
    sizing: Sizing,
    prior: PriorTest | None,
    *,
    plan_hash: str,
    logic_hash: str,
    stop_loss_pct: float,
    risk_controls: RiskControls | None = None,
) -> None:
    """Refuse a step up in size that the named prior test does not prove.

    No prior test at multiplier 1 is a first test at base size and passes. A step must name a
    registered prior, which must be the same plan at a proven size:

    * the same ``plan_hash`` as this test, not merely the same ``logic_hash``: logic drops feature
      parameters, rule literals, stop values and the interval, so EMA 5/60 with a 2.5% stop would
      otherwise borrow the pass of EMA 8/30 with a 10% stop and stake 4x as much at "2x";
    * a final verdict of ``pass``, a real test of the plan rather than of the plumbing: mode not
      ``implementation_only``, and a locked-split check that ``passed``, since any other state
      makes a pass "NOT a success candidate";
    * a decision horizon of at least 90 days, no ``halt.lock``, a frozen :class:`Sizing`, the same
      volatility target and window, and not already used: one proof unlocks one step;
    * each real size at most 2x the prior's: wallet x risk x multiplier, wallet x maximum stake and
      wallet x minimum stake;
    * the guards the prior passed under: the same protections, which shaped the path that passed,
      and its circuit breaker with no threshold looser; a step may add either;
    * a step that is a step: the smallest or the largest stake this plan can really reach
      (``stop_loss_pct`` is the plan's, the same as the prior's) strictly above the prior's. The
      identical plan at an identical stake would otherwise be a second test of one hypothesis.

    The duration is the prior test's frozen decision horizon, not the time it happened to stay
    open, so a default 28-56 day protocol (``paper_protocol.toml``) can never unlock a step up: a
    prior test needs a protocol registered with a horizon of at least 90 days.
    """
    if sizing.prior_test is None:
        if sizing.size_multiplier > 1:
            raise ValueError(
                f"size step rule: size_multiplier {sizing.size_multiplier:g} needs a prior_test"
            )
        return
    if prior is None or prior.name != sizing.prior_test:
        raise ValueError(
            f"size step rule: prior test {None if prior is None else prior.name!r} is not the "
            f"sizing block's prior_test {sizing.prior_test!r}"
        )
    rule, name = "size step rule: prior test", repr(prior.name)
    if prior.final_verdict != "pass":
        raise ValueError(f"{rule} {name} must have passed, its verdict is {prior.final_verdict!r}")
    if (
        prior.decision_days is None
        or not math.isfinite(prior.decision_days)
        or prior.decision_days < MIN_PRIOR_DAYS
    ):
        raise ValueError(
            f"{rule} {name} must have a decision horizon of at least {MIN_PRIOR_DAYS:g} days, "
            f"got {prior.decision_days!r}"
        )
    if prior.logic_hash != logic_hash:
        raise ValueError(
            f"{rule} {name} has other logic ({prior.logic_hash[:12]}, this plan is "
            f"{logic_hash[:12]}): only a pass of the same hypothesis proves a step"
        )
    if prior.plan_hash != plan_hash:
        raise ValueError(
            f"{rule} {name} is another plan ({prior.plan_hash[:12]}, this plan is "
            f"{plan_hash[:12]}): same rules with other numbers, stop or interval are a re-tune, "
            "and a pass proves only the plan that earned it"
        )
    if prior.mode == "implementation_only":
        raise ValueError(f"{rule} {name} is implementation_only: its Sharpe was never tested")
    if prior.locked_split_check != "passed":
        raise ValueError(
            f"{rule} {name} has locked_split_check {prior.locked_split_check!r}: such a pass is "
            "NOT a success candidate, so it proves no step"
        )
    if prior.halted:
        raise ValueError(f"{rule} {name} holds {HALT_LOCK}: a halted test proves no step")
    if prior.unlocked:
        raise ValueError(
            f"{rule} {name} already unlocked {', '.join(prior.unlocked)}: one proof, one step"
        )
    if prior.sizing is None:
        raise ValueError(f"{rule} {name} has no frozen sizing block")
    old = prior.sizing
    if (sizing.vol_target_annual, sizing.vol_window_bars) != (
        old.vol_target_annual,
        old.vol_window_bars,
    ):
        raise ValueError(
            f"size step rule: the volatility target and window must match prior test {name}: "
            f"({sizing.vol_target_annual!r}, {sizing.vol_window_bars!r}) vs "
            f"({old.vol_target_annual!r}, {old.vol_window_bars!r})"
        )
    _check_same_guards(risk_controls, prior)
    before = _real_sizes(old)
    for label, new_size in _real_sizes(sizing).items():
        if new_size / before[label] > MAX_SIZE_STEP * (1 + _THRESHOLD_TOLERANCE):
            raise ValueError(
                f"size step rule: {label} is {new_size / before[label]:g}x prior test {name}; at "
                f"most one {MAX_SIZE_STEP:g}x step at a time"
            )
    grew = zip(_stake_range(sizing, stop_loss_pct), _stake_range(old, stop_loss_pct), strict=True)
    if not any(new > was * (1 + _THRESHOLD_TOLERANCE) for new, was in grew):
        raise ValueError(
            f"size step rule: this plan can stake no more than under prior test {name} "
            f"({_stake_range(sizing, stop_loss_pct)} vs {_stake_range(old, stop_loss_pct)}): a "
            "step must be a step, and the same plan at the same size is a second test of one "
            "hypothesis"
        )


# --------------------------------------------------------------------------- circuit breaker


@dataclass(frozen=True)
class BreakerTrip:
    reason: str  # "daily_loss" | "drawdown"
    threshold: float  # percent
    value: float  # percent, positive = loss
    time: str  # ISO-8601 UTC of the bar


def _loss_pct(reference: float, equity: float) -> float:
    return (reference - equity) * 100 / reference  # not 1 - e/r: that misses 90 vs 100 at 10%


def _at_or_beyond(loss: float, threshold: float) -> bool:
    return loss >= threshold * (1 - _THRESHOLD_TOLERANCE)


def evaluate_breaker(equity: pd.Series, breaker: CircuitBreaker) -> BreakerTrip | None:
    """The first bar whose loss is at or beyond a threshold, or None when nothing trips.

    ``equity`` is marked equity at each bar close with an ascending tz-aware UTC DatetimeIndex.
    Daily loss at t compares with the last bar before t's UTC day (the first value during the
    first UTC day); drawdown compares with the running maximum through t. Daily loss is checked
    first when both trip on one bar. Each bar reads only bars at or before it. The walk stops at
    the first trip, so a daily reference of zero can never be reached: equity that low has
    already tripped the drawdown limit, which is at most 100%.
    """
    if not isinstance(equity, pd.Series) or not isinstance(equity.index, pd.DatetimeIndex):
        raise ValueError("equity must be a pandas Series with a DatetimeIndex")
    index = equity.index
    if str(index.tz) != "UTC":
        raise ValueError(f"equity index must be tz-aware UTC, got tz {index.tz!r}")
    if not (index.is_monotonic_increasing and index.is_unique):
        raise ValueError("equity must have a strictly ascending index")
    values = equity.to_numpy(dtype="float64", na_value=np.nan)
    if not np.isfinite(values).all():
        bad = index[~np.isfinite(values)][0]
        raise ValueError(f"equity has a non-finite value at {bad.isoformat()}")
    if len(values) == 0:
        return None
    if values[0] <= 0:
        raise ValueError(f"equity must start positive, got {values[0]!r}")
    peak = day_reference = float(values[0])
    day = index[0].date()
    previous = peak
    for stamp, raw in zip(index, values, strict=True):
        value = float(raw)
        if stamp.date() != day:
            day, day_reference = stamp.date(), previous
        peak = max(peak, value)
        daily = _loss_pct(day_reference, value)
        if _at_or_beyond(daily, breaker.max_daily_loss_pct):
            return BreakerTrip("daily_loss", breaker.max_daily_loss_pct, daily, stamp.isoformat())
        drawdown = _loss_pct(peak, value)
        if _at_or_beyond(drawdown, breaker.max_drawdown_pct):
            return BreakerTrip("drawdown", breaker.max_drawdown_pct, drawdown, stamp.isoformat())
        previous = value
    return None


def write_halt_lock(folder: Path, trip: BreakerTrip) -> Path:
    """Atomically and exclusively create ``folder/halt.lock``; an existing lock is left
    byte-identical.

    The content goes to a temporary file in the same folder, which is then hard-linked to the lock
    name: the link either appears whole or fails because a lock exists, so no reader sees a partial
    lock, a crash leaves no empty one, and nothing is ever overwritten. There is no delete function
    for the lock: only the owner removes it, outside Claude Code.
    """
    target = Path(folder)
    target.mkdir(parents=True, exist_ok=True)
    path = target / HALT_LOCK
    text = json.dumps(asdict(trip), indent=2, allow_nan=False) + "\n"
    prefix = f".{HALT_LOCK}."
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target, prefix=prefix) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        with contextlib.suppress(FileExistsError):  # the first trip stands
            os.link(handle.name, path)
    return path


def read_halt_lock(folder: Path) -> dict[str, Any] | None:
    """The lock's content, None when there is no lock, ``ValueError`` when it is unreadable."""
    path = Path(folder) / HALT_LOCK
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(lock, dict):
        raise ValueError(f"{path} is not a JSON object")
    return lock


def check_not_halted(folder: Path) -> None:
    """Refuse while ``folder/halt.lock`` exists; an unreadable lock still halts."""
    try:
        lock = read_halt_lock(folder)
    except ValueError:
        lock = {"reason": UNREADABLE_LOCK_REASON}
    if lock is not None:
        path = Path(folder) / HALT_LOCK
        raise ValueError(
            f"paper test is halted (reason: {lock.get('reason')!r}): only the owner removes "
            f"{path}, outside Claude Code"
        )
