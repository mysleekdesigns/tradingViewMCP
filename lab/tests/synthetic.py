"""Seeded synthetic candles for offline tests (testing rule: no network, explicit seeds)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from lab.types import interval_seconds


def make_candles(
    n: int = 500,
    *,
    seed: int = 0,
    interval: str = "4h",
    start: str = "2022-01-01",
    price: float = 100.0,
    vol: float = 0.01,
    drift: float = 0.0,
) -> pd.DataFrame:
    """Geometric random walk OHLCV on a regular UTC grid, all bars tradable."""
    rng = np.random.default_rng(seed)
    step = pd.Timedelta(seconds=interval_seconds(interval))
    index = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=n, freq=step, name="time")
    log_ret = rng.normal(drift, vol, n)
    close = price * np.exp(np.cumsum(log_ret))
    open_ = np.empty(n)
    open_[0] = price
    open_[1:] = close[:-1] * np.exp(rng.normal(0.0, vol / 10, n - 1))
    spread = np.abs(rng.normal(0.0, vol, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.uniform(10.0, 100.0, n)
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "tradable": np.ones(n, dtype=bool),
        },
        index=index,
    )


def candles_from_rows(
    rows: list[tuple[float, float, float, float]],
    *,
    interval: str = "4h",
    start: str = "2024-01-01",
    volume: float = 1.0,
) -> pd.DataFrame:
    """Tiny hand-written (open, high, low, close) bars for accounting tests."""
    step = pd.Timedelta(seconds=interval_seconds(interval))
    index = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=len(rows), freq=step, name="time")
    arr = np.asarray(rows, dtype=float)
    return pd.DataFrame(
        {
            "open": arr[:, 0],
            "high": arr[:, 1],
            "low": arr[:, 2],
            "close": arr[:, 3],
            "volume": np.full(len(rows), volume),
            "tradable": np.ones(len(rows), dtype=bool),
        },
        index=index,
    )


def perturb_after(candles: pd.DataFrame, t: int, *, seed: int = 99) -> pd.DataFrame:
    """Copy of ``candles`` with every row after position ``t`` replaced by different prices.

    The perturbation test: outputs at rows ``<= t`` must be bit-identical on the original and
    the perturbed frame.
    """
    rng = np.random.default_rng(seed)
    out = candles.copy()
    k = len(out) - (t + 1)
    if k <= 0:
        return out
    scale = np.exp(rng.normal(0.0, 0.2, k))
    for col in ("open", "high", "low", "close"):
        out.iloc[t + 1 :, out.columns.get_loc(col)] = out[col].to_numpy()[t + 1 :] * scale
    out.iloc[t + 1 :, out.columns.get_loc("high")] = (
        out[["open", "high", "low", "close"]].to_numpy()[t + 1 :].max(axis=1) * 1.01
    )
    out.iloc[t + 1 :, out.columns.get_loc("low")] = (
        out[["open", "high", "low", "close"]].to_numpy()[t + 1 :].min(axis=1) * 0.99
    )
    out.iloc[t + 1 :, out.columns.get_loc("volume")] = rng.uniform(1.0, 1000.0, k)
    # Future tradable flags change too, so a feature that peeks at later suspensions fails.
    out.iloc[t + 1 :, out.columns.get_loc("tradable")] = rng.random(k) > 0.2
    return out


def make_universe_candles(
    *,
    start: str = "2019-01-01",
    n: int = 900,
    seed: int = 11,
    drift: float = 0.002,
) -> dict[str, pd.DataFrame]:
    """A small seeded multi-coin daily universe for portfolio tests (offline, no network).

    Eight ``*USDT`` coins with different drifts, volatilities and time-varying dollar volume,
    so liquidity ranks and momentum ranks both change over time. Special cases:

    * ``BTCUSDT`` trades every day (the market benchmark);
    * ``DEADUSDT`` stops printing after 400 days (a delisting);
    * ``GAPUSDT`` rallies into a 40-day hole from day 300 and reopens 10x higher (a new listing
      segment, e.g. a redenomination), so a momentum book holds it into the break.

    Dollar volume is ~1e8-1e9 a day, so participation impact on a 1M book stays small.
    """
    specs = {
        "AAAUSDT": (drift * 1.5, 0.030, 3.0),
        "BBBUSDT": (drift * 0.5, 0.025, 2.0),
        "BTCUSDT": (drift, 0.020, 8.0),
        "CCCUSDT": (-drift * 0.5, 0.035, 2.5),
        "DEADUSDT": (-drift, 0.040, 1.5),
        "EEEUSDT": (drift * 2.0, 0.045, 1.0),
        "FFFUSDT": (0.0, 0.030, 1.2),
        "GAPUSDT": (drift * 3.0, 0.040, 2.2),
    }
    rng = np.random.default_rng(seed)
    out: dict[str, pd.DataFrame] = {}
    for k, (symbol, (mu, vol, liq)) in enumerate(specs.items()):
        c = make_candles(n, seed=seed + k, interval="1d", start=start, vol=vol, drift=mu)
        level = np.exp(np.cumsum(rng.normal(0.0, 0.05, n)))
        c["volume"] = c["volume"] * liq * 1e6 * level / c["close"].to_numpy()
        if symbol == "DEADUSDT":
            c = c.iloc[:400]
        if symbol == "GAPUSDT":
            ramp = np.exp(0.012 * np.clip(np.arange(n) - 250, 0, 50))  # momentum into the hole
            for col in ("open", "high", "low", "close"):
                c[col] = c[col] * ramp
            c["volume"] = c["volume"] / ramp
            after = c.iloc[340:].copy()
            for col in ("open", "high", "low", "close"):
                after[col] = after[col] * 10.0
            after["volume"] = after["volume"] / 10.0
            c = pd.concat([c.iloc[:300], after])
        out[symbol] = c
    return out
