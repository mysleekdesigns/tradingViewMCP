"""Attach aligned ``drv_*`` derivatives columns to a candle frame; check a plan's inputs
(PLAN.md Phase 12).

``attach_derivatives`` adds, for each registry derivatives column whose archive series exists on
disk, that series aligned to each spot bar's CLOSE with
:func:`lab.data.derivatives_align.align_dataset` (a funding settlement / kline / metrics row
is visible only at bars whose close is at or after its visibility time; NaN where the archive
has no visible, fresh row; never forward-filled):

==================== ======================================================
column               source (``lab.data.derivatives``)
==================== ======================================================
``drv_funding_rate`` ``fundingRate.funding_rate``
``drv_premium_close`` ``premiumIndexKlines/{interval}.close``
``drv_mark_close``   ``markPriceKlines/{interval}.close``
``drv_index_close``  ``indexPriceKlines/{interval}.close``
``drv_oi``           ``metrics.sum_open_interest`` (contracts)
``drv_ls_ratio``     ``metrics.count_long_short_ratio``
==================== ======================================================

Kline columns are attached only from the series at exactly the candles' interval (a 1h premium
kline on 4h bars would be a different quantity). Only archive rows stamped in
``[first bar open - margin, last bar close]`` are loaded, so a design-window load (bars before
``holdout_start``) never reads a row stamped after its last bar's close. The margin only has to
cover the staleness limit (``stale_after_intervals`` x the series' own step), so the first bar
sees the same value it would with the whole series loaded.

``stamped_before`` additionally drops every row whose archive timestamp is at or after it.
``lab.runner.default_loader`` passes the load's ``end`` (``holdout_start`` for every design-window
load): on real BTCUSDT data the funding settlement stamped 2025-10-01 00:00:00.000 is visible at
the close of the last design bar (opening 2025-09-30 20:00 on 4h bars), point in time correct but
stamped AT ``holdout_start``, so design loads conservatively leave it out (that bar keeps the
16:00 settlement, well inside the staleness limit). Metrics rows are visible 5 minutes after
their stamp, so the last design bar already used the 23:55 row, never the 00:00 one.

A series that exists but cannot be aligned (e.g. kline Parquet written before derivatives schema
2, conflicting rows) is left out rather than breaking every load; the reason is kept in
``frame.attrs[UNAVAILABLE_ATTR]`` and :func:`require_plan_inputs` reports it for plans that need
the column.

The permutation null (``lab.stats.permutation.permute_candles``) copies the frame and shuffles
OHLC only, so the ``drv_*`` columns stay on their REAL timestamps while the bar returns move to
other bars. That breaks the link between a gate's state at bar t and the return that follows
it: the null is fair to a gated plan, and a derivatives timing edge (the gate keeping the plan
out before bad bars) must beat it like any other signal.

:func:`require_holdout_coverage` refuses a holdout check when a column the plan reads does not
cover the holdout: it starts later than the holdout's first bar or stops before its last bar by
more than the column's staleness limit, or the manifest records an archive gap inside the
holdout longer than that limit (a gate that is NaN there would silently keep the plan flat).

Scored start (PLAN.md Phase 12): every derivatives plan on a (symbol, interval) is scored from
ONE start, :func:`derivatives_start_floor` = the latest manifest ``first`` timestamp across the
five archive series behind the six ``drv_*`` columns (fundingRate, metrics and the three kline
datasets at that interval) plus ``[derivatives] warmup_days``. Inside a research run the
``first`` values come from the run's frozen snapshot, so the start cannot move mid-run; it never
depends on which derivatives features a plan uses or their periods. A missing series refuses
every derivatives plan on that symbol/interval (:class:`DataUnavailableError` naming the CLI).

Original candle columns and values are never modified (a copy is returned), so a plan without
derivatives features computes exactly what it computed before.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from lab.data import derivatives as drv_data
from lab.data.derivatives_align import (
    FUNDING_DATASET,
    FUNDING_FALLBACK_INTERVAL,
    METRICS_DATASET,
    METRICS_INTERVAL,
    align_dataset,
)
from lab.registry.derivatives import (
    DERIVATIVES_COLUMNS,
    DERIVATIVES_INPUTS,
    DRV_FUNDING_RATE,
    DRV_INDEX_CLOSE,
    DRV_LS_RATIO,
    DRV_MARK_CLOSE,
    DRV_OI,
    DRV_PREMIUM_CLOSE,
)
from lab.types import Plan, interval_seconds

__all__ = [
    "COLUMN_SOURCES",
    "SCORED_START_DATASETS",
    "UNAVAILABLE_ATTR",
    "DataUnavailableError",
    "DerivativeColumns",
    "attach_columns",
    "attach_derivatives",
    "derivative_columns",
    "derivatives_cli_hint",
    "derivatives_start_floor",
    "load_margin",
    "manifest_series",
    "max_derivatives_period",
    "plan_derivative_columns",
    "require_holdout_coverage",
    "require_plan_inputs",
    "required_series_keys",
    "source_step",
    "staleness_limit",
]


class DataUnavailableError(RuntimeError):
    """Data a backtest needs is not on disk (candles or derivatives); ``str()`` names the CLI.

    Raised before any trial is recorded, so a missing download never spends a ledger row.
    ``lab.runner.DataUnavailableError`` is this class."""


# column -> (archive dataset, value column in the loaded frame)
COLUMN_SOURCES: Mapping[str, tuple[str, str]] = {
    DRV_FUNDING_RATE: ("fundingRate", "funding_rate"),
    DRV_PREMIUM_CLOSE: ("premiumIndexKlines", "close"),
    DRV_MARK_CLOSE: ("markPriceKlines", "close"),
    DRV_INDEX_CLOSE: ("indexPriceKlines", "close"),
    DRV_OI: ("metrics", "sum_open_interest"),
    DRV_LS_RATIO: ("metrics", "count_long_short_ratio"),
}
if set(COLUMN_SOURCES) != set(DERIVATIVES_COLUMNS):  # pragma: no cover - import-time guard
    raise ImportError("COLUMN_SOURCES must cover exactly lab.registry.derivatives columns")

#: ``frame.attrs`` key: ``{column: reason}`` for series on disk that could not be aligned.
UNAVAILABLE_ATTR = "drv_unavailable"

# A funding interval is at most 8h on Binance today; a day bounds any interval it could take.
_LONGEST_SOURCE_STEP = pd.Timedelta(days=1)
_SAFETY = pd.Timedelta(days=1)


def _dataset_interval(dataset: str, interval: str) -> str | None:
    return interval if dataset in drv_data.KLINE_DATASETS else None


def load_margin(interval: str, stale_after_intervals: float) -> pd.Timedelta:
    """How far before the first bar's OPEN archive rows are loaded.

    A row is used at a bar only while ``close - visible_at <= stale x its own step``, and every
    row's own timestamp is at or before its visibility time, so rows stamped earlier than
    ``first open + bar - stale x step`` can never be used. ``step`` is the bar interval for
    klines (attached only at the bar interval), the funding interval (<= 1 day) or 5 minutes
    (metrics); one extra day of slack is added."""
    bar = pd.Timedelta(seconds=interval_seconds(interval))
    return max(bar, _LONGEST_SOURCE_STEP) * float(stale_after_intervals) + _SAFETY


def _series_on_disk(data_dir: Path, dataset: str, symbol: str, interval: str) -> bool:
    try:
        folder = drv_data.series_dir(
            data_dir, dataset, symbol, _dataset_interval(dataset, interval)
        )
    except ValueError:  # symbol or interval the archive cannot hold
        return False
    return folder.is_dir() and any(p.is_file() for p in folder.glob("*.parquet"))


class DerivativeColumns:
    """Aligned ``drv_*`` arrays for one bar index (what :func:`attach_columns` adds)."""

    __slots__ = ("bar_times", "unavailable", "values")

    def __init__(
        self,
        bar_times: pd.DatetimeIndex,
        values: Mapping[str, np.ndarray],
        unavailable: Mapping[str, str],
    ) -> None:
        self.bar_times = bar_times
        self.values = dict(values)
        self.unavailable = dict(unavailable)


def derivative_columns(
    bar_times: pd.DatetimeIndex,
    symbol: str,
    interval: str,
    *,
    data_dir: Path,
    stale_after_intervals: float,
    stamped_before: pd.Timestamp | None = None,
) -> DerivativeColumns:
    """The aligned ``drv_*`` arrays for spot bars opening at ``bar_times`` (UTC, sorted).

    Only series present on disk are returned; each dataset is loaded once over
    ``[bar_times[0] - load_margin, bar_times[-1] + interval]`` (the last bar's close), minus
    every row stamped at or after ``stamped_before`` (module docstring)."""
    bar_s = interval_seconds(interval)
    idx = pd.DatetimeIndex(bar_times)
    values: dict[str, np.ndarray] = {}
    unavailable: dict[str, str] = {}
    by_dataset: dict[str, list[str]] = {}
    for column in DERIVATIVES_COLUMNS:
        dataset, _ = COLUMN_SOURCES[column]
        by_dataset.setdefault(dataset, []).append(column)
    for dataset, columns in by_dataset.items():
        if not _series_on_disk(data_dir, dataset, symbol, interval):
            continue
        if len(idx) == 0:
            for column in columns:
                values[column] = np.empty(0, dtype=np.float64)
            continue
        try:
            frame = drv_data.load_series(
                dataset,
                symbol,
                data_dir=data_dir,
                interval=_dataset_interval(dataset, interval),
                start=idx[0] - load_margin(interval, stale_after_intervals),
                end=idx[-1] + pd.Timedelta(seconds=bar_s),  # the last bar's close, inclusive
            )
            if stamped_before is not None:
                frame = frame.loc[frame.index < pd.Timestamp(stamped_before)]
            aligned = align_dataset(
                dataset,
                frame,
                idx,
                bar_s,
                interval_seconds=bar_s if dataset in drv_data.KLINE_DATASETS else None,
                stale_after_intervals=stale_after_intervals,
            )
        except ValueError as exc:
            for column in columns:
                unavailable[column] = f"{dataset} for {symbol} could not be aligned: {exc}"
            continue
        for column in columns:
            values[column] = aligned[COLUMN_SOURCES[column][1]].to_numpy(dtype=np.float64)
    return DerivativeColumns(idx, values, unavailable)


def attach_columns(candles: pd.DataFrame, columns: DerivativeColumns) -> pd.DataFrame:
    """A copy of ``candles`` with ``columns`` added (they must be computed on its index)."""
    if not candles.index.equals(columns.bar_times):
        raise ValueError("derivative columns were computed for a different bar index")
    out = candles.copy()
    for column in DERIVATIVES_COLUMNS:
        if column in columns.values:
            out[column] = columns.values[column].copy()  # a cached array is never shared
    if columns.unavailable:
        out.attrs[UNAVAILABLE_ATTR] = dict(columns.unavailable)
    return out


def attach_derivatives(
    candles: pd.DataFrame,
    symbol: str,
    interval: str,
    *,
    data_dir: Path,
    stale_after_intervals: float,
    stamped_before: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Copy of ``candles`` plus the ``drv_*`` columns whose series exist under ``data_dir``,
    aligned to each bar's close (module docstring). Original columns are untouched."""
    cols = derivative_columns(
        pd.DatetimeIndex(candles.index),
        symbol,
        interval,
        data_dir=data_dir,
        stale_after_intervals=stale_after_intervals,
        stamped_before=stamped_before,
    )
    return attach_columns(candles, cols)


# --------------------------------------------------------------------------- plan inputs


def _feature_names(plan: Plan | Mapping[str, Any] | str) -> list[str]:
    if isinstance(plan, Plan):
        return [ref.feature for ref in plan.features]
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except (ValueError, RecursionError):
            return []
    if not isinstance(plan, Mapping) or plan.get("kind") is not None:
        return []  # portfolio plans (``"kind": "portfolio"``) carry no derivatives inputs
    features = plan.get("features")
    if not isinstance(features, Mapping):
        return []
    names: list[str] = []
    for body in features.values():
        if isinstance(body, Mapping):
            names.extend(str(k) for k in body)
    return names


def plan_derivative_columns(plan: Plan | Mapping[str, Any] | str) -> tuple[str, ...]:
    """The ``drv_*`` columns ``plan``'s features read, in ``DERIVATIVES_COLUMNS`` order; ``()``
    for portfolio plans and plans without derivatives features."""
    needed: set[str] = set()
    for name in _feature_names(plan):
        needed.update(DERIVATIVES_INPUTS.get(name, ()))
    return tuple(c for c in DERIVATIVES_COLUMNS if c in needed)


def _month(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y-%m")


def _last_full_month() -> str:
    first = pd.Timestamp.now(tz="UTC").normalize().replace(day=1)
    return (first - pd.Timedelta(days=1)).strftime("%Y-%m")


def derivatives_cli_hint(
    symbol: str,
    interval: str,
    columns: tuple[str, ...] | list[str],
    *,
    start: str = "YYYY-MM",
    end: str = "YYYY-MM",
) -> str:
    """The exact download command for ``columns`` (``--interval`` only for kline datasets)."""
    datasets: list[str] = []
    for column in columns:
        dataset = COLUMN_SOURCES[column][0]
        if dataset not in datasets:
            datasets.append(dataset)
    iv = f" --interval {interval}" if any(d in drv_data.KLINE_DATASETS for d in datasets) else ""
    return (
        f"cd lab && uv run python -m lab.data.derivatives download {symbol} "
        f"--datasets {','.join(datasets)}{iv} --start {start} --end {end}"
    )


def require_plan_inputs(
    plan: Plan | Mapping[str, Any] | str,
    candles: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
) -> None:
    """Raise :class:`DataUnavailableError` when ``candles`` lack a ``drv_*`` column ``plan``
    needs, or carry it with no finite value at all; the message names each column and the
    download CLI. Call it before any trial is recorded. No-op for plans without derivatives."""
    needed = plan_derivative_columns(plan)
    if not needed:
        return
    reasons = candles.attrs.get(UNAVAILABLE_ATTR, {}) if hasattr(candles, "attrs") else {}
    problems: list[str] = []
    bad: list[str] = []
    for column in needed:
        if column not in candles.columns:
            why = reasons.get(column) if isinstance(reasons, Mapping) else None
            problems.append(f"{column} is not loaded" + (f" ({why})" if why else ""))
            bad.append(column)
        elif not np.isfinite(candles[column].to_numpy(dtype=np.float64)).any():
            problems.append(f"{column} has no value in the {len(candles)} bars used")
            bad.append(column)
    if not bad:
        return
    if len(candles):
        start = _month(candles.index[0])
        end = min(_month(candles.index[-1]), _last_full_month())
    else:
        start = end = "YYYY-MM"
    dataset_text = ", ".join(sorted({COLUMN_SOURCES[c][0] for c in bad}))
    raise DataUnavailableError(
        f"plan needs derivatives data for {symbol} {interval} that is missing: "
        f"{'; '.join(problems)}. Download {dataset_text} with "
        f"`{derivatives_cli_hint(symbol, interval, bad, start=start, end=end)}` "
        "(nothing was recorded in the ledger)"
    )


def source_step(column: str, interval: str) -> pd.Timedelta:
    """The sampling step of ``column``'s archive series: the funding interval (8h, Binance's
    default; a 4h switch only shortens it), the bar interval for klines (attached only at the
    bar interval), 5 minutes for metrics."""
    dataset = COLUMN_SOURCES[column][0]
    if dataset == FUNDING_DATASET:
        return FUNDING_FALLBACK_INTERVAL
    if dataset == METRICS_DATASET:
        return METRICS_INTERVAL
    return pd.Timedelta(seconds=interval_seconds(interval))


def staleness_limit(column: str, interval: str, stale_after_intervals: float) -> pd.Timedelta:
    """How long ``column`` may go without a fresh value on ``interval`` bars before it counts as
    missing: ``max(stale_after_intervals x source_step(column), one bar)``."""
    bar = pd.Timedelta(seconds=interval_seconds(interval))
    return max(source_step(column, interval) * float(stale_after_intervals), bar)


def _gap_bounds(gap: Any) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """``(from, to)`` of one manifest gap entry (``{"from", "to"}``, ISO UTC), or None."""
    if not isinstance(gap, Mapping):
        return None
    try:
        lo = pd.Timestamp(gap["from"])
        hi = pd.Timestamp(gap["to"])
    except (KeyError, TypeError, ValueError):
        return None
    lo = lo.tz_localize("UTC") if lo.tzinfo is None else lo.tz_convert("UTC")
    hi = hi.tz_localize("UTC") if hi.tzinfo is None else hi.tz_convert("UTC")
    return lo, hi


def require_holdout_coverage(
    plan: Plan | Mapping[str, Any] | str,
    candles: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
    stale_after_intervals: float,
    series: Mapping[str, Any] | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> None:
    """Raise :class:`DataUnavailableError` when the derivatives archive does not cover
    ``candles`` (the holdout slice, ``[start, end)``; default: its first bar to its last bar's
    close). For each ``drv_*`` column ``plan`` reads, with ``limit`` = :func:`staleness_limit`:

    * it is missing or all-NaN there (:func:`require_plan_inputs`);
    * its FIRST finite value is at a bar later than ``start + limit`` (the archive begins inside
      the holdout);
    * its LAST finite value is at a bar earlier than the last bar ``- limit``;
    * a gap recorded for its archive series in ``series`` (manifest / frozen snapshot entries,
      ``{key: {"gaps": [{"from", "to"}, ...]}}``) overlaps ``[start, end)`` by more than
      ``limit``.

    No-op for plans without derivatives features."""
    require_plan_inputs(plan, candles, symbol=symbol, interval=interval)
    needed = plan_derivative_columns(plan)
    if not needed or not len(candles):
        return
    bar = pd.Timedelta(seconds=interval_seconds(interval))
    first_bar = pd.Timestamp(candles.index[0])
    last_bar = pd.Timestamp(candles.index[-1])
    lo = first_bar if start is None else pd.Timestamp(start)
    hi = last_bar + bar if end is None else pd.Timestamp(end)
    short: list[str] = []
    for column in needed:
        finite = np.flatnonzero(np.isfinite(candles[column].to_numpy(dtype=np.float64)))
        first_finite = pd.Timestamp(candles.index[finite[0]])
        last_finite = pd.Timestamp(candles.index[finite[-1]])
        limit = staleness_limit(column, interval, stale_after_intervals)
        if first_finite > lo + limit:
            short.append(
                f"{column} starts at the bar opening {first_finite.isoformat()} but the holdout "
                f"starts at {lo.isoformat()} (staleness limit {limit})"
            )
        if last_finite < last_bar - limit:
            short.append(
                f"{column} ends at the bar opening {last_finite.isoformat()} but the data runs to "
                f"{last_bar.isoformat()} (staleness limit {limit})"
            )
        if series is None:
            continue
        dataset = COLUMN_SOURCES[column][0]
        key = drv_data.series_key(dataset, symbol, _dataset_interval(dataset, interval))
        entry = series.get(key)
        gaps = entry.get("gaps") if isinstance(entry, Mapping) else None
        for gap in gaps if isinstance(gaps, list | tuple) else ():
            bounds = _gap_bounds(gap)
            if bounds is None:
                continue
            overlap = min(bounds[1], hi) - max(bounds[0], lo)
            if overlap > limit:
                short.append(
                    f"{column}: the archive series {key} has a gap from {bounds[0].isoformat()} "
                    f"to {bounds[1].isoformat()}, {overlap} of it inside the holdout (staleness "
                    f"limit {limit})"
                )
    if not short:
        return
    month_from = _month(lo)
    month_to = min(_month(last_bar), _last_full_month())
    raise DataUnavailableError(
        f"the derivatives archive for {symbol} {interval} does not cover the holdout: "
        f"{'; '.join(short)}. Download the missing months with "
        f"`{derivatives_cli_hint(symbol, interval, needed, start=month_from, end=month_to)}`"
    )


# --------------------------------------------------------------------------- scored start

#: the archive series behind the six ``drv_*`` columns (klines at the bar interval)
SCORED_START_DATASETS: tuple[str, ...] = tuple(dict.fromkeys(d for d, _ in COLUMN_SOURCES.values()))


def required_series_keys(symbol: str, interval: str) -> tuple[str, ...]:
    """Manifest keys of every derivatives series a (symbol, interval) scored start needs."""
    return tuple(
        drv_data.series_key(d, symbol, _dataset_interval(d, interval))
        for d in SCORED_START_DATASETS
    )


def manifest_series(data_dir: Path) -> dict[str, Any] | None:
    """The ``series`` mapping of ``{data_dir}/manifest.json`` now, or None without a manifest."""
    path = Path(data_dir) / drv_data.MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    raw = data.get("series") if isinstance(data, Mapping) else None
    return dict(raw) if isinstance(raw, Mapping) else None


def _utc(value: Any) -> pd.Timestamp | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = pd.Timestamp(value)
    except ValueError:
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def derivatives_start_floor(
    symbol: str,
    interval: str,
    series: Mapping[str, Any] | None,
    *,
    warmup_days: float,
    cli_start: str = "YYYY-MM",
    cli_end: str = "YYYY-MM",
) -> pd.Timestamp:
    """The earliest time any derivatives plan on (``symbol``, ``interval``) may be scored from:
    the LATEST ``first`` timestamp across :func:`required_series_keys` in ``series`` (manifest
    or frozen-snapshot entries) plus ``warmup_days``. The caller snaps it to the first bar at or
    after it (and never before ``design_start``). Identical for every derivatives plan,
    whatever features or periods it uses.

    Raises :class:`DataUnavailableError` naming the download CLI when ``series`` is None (no
    manifest) or any required series is absent or empty."""
    keys = required_series_keys(symbol, interval)
    firsts: dict[str, pd.Timestamp] = {}
    missing: list[str] = []
    for key in keys:
        entry = series.get(key) if isinstance(series, Mapping) else None
        first = _utc(entry.get("first")) if isinstance(entry, Mapping) else None
        if first is None:
            missing.append(key)
        else:
            firsts[key] = first
    if missing:
        where = "the derivatives manifest" if series is not None else "a derivatives manifest"
        hint = derivatives_cli_hint(
            symbol, interval, DERIVATIVES_COLUMNS, start=cli_start, end=cli_end
        )
        raise DataUnavailableError(
            f"every derivatives plan on {symbol} {interval} is scored from one start (the latest "
            f"first timestamp of {', '.join(keys)} + [derivatives] warmup_days), but "
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} missing from {where}; "
            "download them with "
            f"`{hint}` "
            "(nothing was recorded in the ledger)"
        )
    return max(firsts.values()) + pd.Timedelta(days=float(warmup_days))


def max_derivatives_period(interval: str, warmup_days: float) -> int:
    """The longest derivatives look-back in bars of ``interval`` that fits in ``warmup_days``."""
    return math.floor(float(warmup_days) * 86_400 / interval_seconds(interval) + 1e-9)
