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
