"""Seeded synthetic candles for offline tests (testing rule: no network, explicit seeds)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pandas as pd

from lab.data.derivatives import (
    FUNDING_COLUMNS,
    KLINE_COLUMNS,
    KLINE_DATASETS,
    METRICS_COLUMNS,
    build_manifest,
)
from lab.registry.derivatives import (
    DERIVATIVES_COLUMNS,
    DRV_FUNDING_RATE,
    DRV_INDEX_CLOSE,
    DRV_LS_RATIO,
    DRV_MARK_CLOSE,
    DRV_OI,
    DRV_PREMIUM_CLOSE,
)
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


# Positional fractions of the NaN rows ``with_derivatives`` punches into each drv_* column
# (archive gaps), spread so every column has one and windows clear of them remain.
_DRV_GAP_AT: dict[str, tuple[float, ...]] = {
    DRV_FUNDING_RATE: (0.07,),  # two rows: one missing 8h settlement on 4h bars
    DRV_OI: (0.17,),
    DRV_MARK_CLOSE: (0.25,),
    DRV_INDEX_CLOSE: (0.255,),
    DRV_PREMIUM_CLOSE: (0.33,),
    DRV_LS_RATIO: (0.42,),
}


def with_derivatives(candles: pd.DataFrame, *, seed: int, gaps: bool = True) -> pd.DataFrame:
    """Copy of ``candles`` with the six aligned ``drv_*`` derivatives columns added.

    * funding: piecewise constant over 2 bars (an 8h settlement on 4h bars), mean 1e-4 with
      some negative settlements;
    * mark / index close: within a few basis points of ``close``, always positive;
    * premium close: a small signed fraction;
    * open interest: a positive random walk around 1e5 contracts;
    * long/short ratio: between 1 and 2.

    With ``gaps`` (default) each column gets a NaN row (funding: two) at a fixed fraction of
    the frame, modelling archive gaps that must stay NaN downstream.
    """
    rng = np.random.default_rng(seed)
    out = candles.copy()
    n = len(out)
    close = out["close"].to_numpy(dtype=np.float64)
    funding = np.repeat(rng.normal(1e-4, 2e-4, (n + 1) // 2), 2)[:n]
    out[DRV_FUNDING_RATE] = funding
    out[DRV_PREMIUM_CLOSE] = rng.normal(5e-5, 3e-4, n)
    out[DRV_MARK_CLOSE] = close * (1.0 + rng.normal(3e-4, 5e-4, n))
    out[DRV_INDEX_CLOSE] = close * (1.0 + rng.normal(0.0, 3e-4, n))
    out[DRV_OI] = 1e5 * np.exp(np.cumsum(rng.normal(0.0, 0.02, n)))
    out[DRV_LS_RATIO] = rng.uniform(1.0, 2.0, n)
    if gaps and n:
        for col, fractions in _DRV_GAP_AT.items():
            for frac in fractions:
                at = int(frac * n)
                rows = [at, at + 1] if col == DRV_FUNDING_RATE else [at]
                rows = [r for r in rows if r < n]
                out.iloc[rows, out.columns.get_loc(col)] = np.nan
    return out


def perturb_after(candles: pd.DataFrame, t: int, *, seed: int = 99) -> pd.DataFrame:
    """Copy of ``candles`` with every row after position ``t`` replaced by different prices.

    The perturbation test: outputs at rows ``<= t`` must be bit-identical on the original and
    the perturbed frame. When the frame carries ``drv_*`` derivatives columns, their values
    after ``t`` are rescaled too and about 5% of them become NaN (new future gaps), so a
    feature that peeks ahead, or back-fills a gap, fails.
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
    # Drawn after the price draws, so frames without derivatives columns perturb as before.
    for col in DERIVATIVES_COLUMNS:
        if col not in out.columns:
            continue
        future = out[col].to_numpy(dtype=np.float64)[t + 1 :] * np.exp(rng.normal(0.0, 0.3, k))
        future[rng.random(k) < 0.05] = np.nan
        out.iloc[t + 1 :, out.columns.get_loc(col)] = future
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


def _write_parts(frame: pd.DataFrame, folder: Path, label_fmt: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for label, part in frame.groupby(frame.index.strftime(label_fmt)):
        part.to_parquet(folder / f"{label}.parquet", engine="pyarrow")


def write_derivatives_archive(
    root: Path,
    symbol: str = "BTCUSDT",
    interval: str = "4h",
    *,
    first: str | pd.Timestamp,
    end: str | pd.Timestamp | None = None,
    firsts: Mapping[str, str | pd.Timestamp] | None = None,
    seed: int = 0,
    gap_factor: float = 1.5,
) -> dict:
    """A small on-disk derivatives archive for (``symbol``, ``interval``): fundingRate (8h) and
    the three kline datasets (at ``interval``) from ``first`` to ``end`` (default one day later),
    metrics (5 min) for one day from its first; ``firsts`` overrides a dataset's first timestamp.
    Rebuilds and returns ``{root}/manifest.json`` (``build_manifest``).

    Tests inject their ``drv_*`` columns through their own loader; the archive gives the manifest
    the series entries (``first``, ``gaps``, content) the shared scored start and the run
    snapshot read."""
    rng = np.random.default_rng(seed)
    firsts = dict(firsts or {})

    def span(dataset: str, step: pd.Timedelta, days: float | None = None) -> pd.DatetimeIndex:
        lo = pd.Timestamp(firsts.get(dataset, first))
        lo = lo.tz_localize("UTC") if lo.tzinfo is None else lo
        hi = lo + pd.Timedelta(days=days) if days is not None else None
        if hi is None:
            hi = pd.Timestamp(end) if end is not None else lo + pd.Timedelta(days=1)
            hi = hi.tz_localize("UTC") if hi.tzinfo is None else hi
        return pd.date_range(lo, hi, freq=step, inclusive="left", name="time")

    t = span("fundingRate", pd.Timedelta(hours=8))
    funding = pd.DataFrame(
        {
            FUNDING_COLUMNS[0]: rng.normal(1e-4, 5e-5, len(t)),
            FUNDING_COLUMNS[1]: np.full(len(t), 8.0),
        },
        index=t,
    )
    _write_parts(funding, Path(root) / "fundingRate" / symbol, "%Y-%m")
    step = pd.Timedelta(seconds=interval_seconds(interval))
    for dataset in sorted(KLINE_DATASETS):
        opens = span(dataset, step)
        close = 40_000 * np.exp(np.cumsum(rng.normal(0, 0.01, len(opens))))
        frame = pd.DataFrame({c: close for c in KLINE_COLUMNS}, index=opens)
        frame["close_time"] = opens + step - pd.Timedelta(milliseconds=1)
        _write_parts(frame, Path(root) / dataset / symbol / interval, "%Y-%m")
    t = span("metrics", pd.Timedelta(minutes=5), days=1.0)
    metrics = pd.DataFrame({c: rng.uniform(1.0, 2.0, len(t)) for c in METRICS_COLUMNS}, index=t)
    _write_parts(metrics, Path(root) / "metrics" / symbol, "%Y-%m-%d")
    return build_manifest(Path(root), gap_factor=gap_factor)
