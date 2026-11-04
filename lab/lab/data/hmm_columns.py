"""Cache the causal HMM and attach its columns to a candle frame (PLAN.md Phase 20 Part B).

:func:`attach_hmm_columns` adds ``hmm_series_state``, ``hmm_series_bear_prob``,
``hmm_series_state_prob`` and ``hmm_series_bear_high_vol`` (:func:`lab.registry.hmm.causal_hmm`),
computed on exactly the frame it receives, so a design load (``end = holdout_start``) never sees a
holdout close, with zero-volume rows marked as not observed. The registry's
``hmm_state`` and ``hmm_bear_prob`` read those columns, so agents read one series instead of
refitting.

Two cache levels, both keyed on content rather than on a file name, so a re-downloaded archive or
a changed ``[regime.hmm]`` setting cannot reuse an old result:

* fit level: SQLite ``{cache_dir}/fits.sqlite``. The key is the sha256 of the standardised
  training block's bytes, ``settings.fit_key()``, ``HMM_ALGO_VERSION`` and :func:`hmm_stack` (the
  sha256 of hedgeFund's three HMM source files, an editable dependency, plus the numpy, scipy,
  scikit-learn and hmmlearn versions).
  The value is the ``HMMParams`` arrays and the carried posterior as raw float64 bytes with their
  shape (no pickle). A new bar on a growing series, or a load with a later ``end``, therefore
  needs at most the fits it has not seen, and a hit replays nothing. An unreadable row, or an
  unusable cache file, falls back to refitting.
* series level: an in-process LRU keyed on the sha256 of the index (ns) and close bytes, any
  unobserved rows, plus the same settings. Its lock is held only to read or write the LRU, so a
  cached read never waits behind a running fit. One cold computation runs at a time under a
  separate lock, which re-checks the LRU, so two callers never fit one series twice.

Missing fits run before ``causal_hmm``: :func:`lab.registry.hmm.refit_blocks` lists the training
blocks, hits are read from SQLite, and the misses go to a small spawn process pool, or run
serially when there are only one or two (a growing series) or the pool cannot run. The path never
changes a result: a block's fit depends on its bytes and the seeds, and hedgeFund pins each fit
to one BLAS thread. A worker runs ``fit_block`` only, and it re-imports the parent's ``__main__``
module, so every entry point that can reach this code guards its main.

The permutation null (:func:`lab.evaluate.adversarial.permute_history`) copies the frame and
shuffles OHLC only, so the ``hmm_series_*`` columns stay on their real timestamps, exactly like
``drv_*``. A permutation path never refits. The null therefore tests timing against a fixed gate
path: the returns move and the gate does not. An HMM on returns and volatility mostly tracks
volatility, and shuffling destroys volatility clustering, so volatility timing alone can reject
this null without any return-direction edge.

A run freezes :func:`hmm_stack` beside the settings. :func:`require_frozen_hmm` refuses an HMM
plan in a run that froze neither.

Original candle columns and values are never modified (a copy is returned).
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import multiprocessing
import os
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from typing import Any

import hedgefund.regime.hmm_filter as hedgefund_filter
import numpy as np
import pandas as pd
from hedgefund.regime.hmm_filter import HMMParams

from lab.data.derivatives_columns import DataUnavailableError
from lab.plan.schema import HMM_FEATURE_NAMES
from lab.registry.hmm import (
    HMM_ALGO_VERSION,
    HMM_BEAR_HIGH_VOL_COLUMN,
    HMM_BEAR_PROB_COLUMN,
    HMM_STATE_COLUMN,
    HMM_STATE_PROB_COLUMN,
    FitFn,
    FittedBlock,
    HmmSeries,
    RefitBlock,
    causal_hmm,
    fit_block,
    refit_blocks,
)
from lab.types import HmmSettings, Plan, interval_seconds

__all__ = [
    "FIT_CACHE_NAME",
    "HMM_UNAVAILABLE_ATTR",
    "HmmBusyError",
    "attach_hmm_columns",
    "hmm_scored_start",
    "hmm_series",
    "hmm_stack",
    "plan_hmm_features",
    "require_frozen_hmm",
    "require_hmm_columns",
]

#: ``frame.attrs`` key: why the loader left the ``hmm_series_*`` columns out of this frame.
HMM_UNAVAILABLE_ATTR = "hmm_unavailable"
FIT_CACHE_NAME = "fits.sqlite"
_SERIES_CACHE_SIZE = 16  # design and holdout views of a few series, plus a chart's growing one
_SQLITE_TIMEOUT_SECONDS = 30.0  # the MCP server and the API can write the file at the same time
_MAX_POOL_WORKERS = 4  # at most four: the seven live paper bots share this machine
_SERIAL_MISSES = 2  # a growing series: spawning workers costs more than these fits
_POOL_FAILURES = (OSError, BrokenProcessPool, NotImplementedError)  # e.g. semaphores denied
_FIT_ARRAYS = ("startprob", "transmat", "means", "covars")
_HEDGEFUND_SOURCES = ("hmm.py", "hmm_filter.py", "hmm_features.py")  # the code behind every fit
_STACK_PACKAGES = ("numpy", "scipy", "scikit-learn", "hmmlearn")
_FEATURE_COLUMN: Mapping[str, str] = {
    "hmm_state": HMM_STATE_COLUMN,
    "hmm_bear_prob": HMM_BEAR_PROB_COLUMN,
}
if set(_FEATURE_COLUMN) != set(HMM_FEATURE_NAMES):  # pragma: no cover - import-time guard
    raise ImportError("_FEATURE_COLUMN must cover exactly lab.plan.schema.HMM_FEATURE_NAMES")

_log = logging.getLogger(__name__)
_series_lock = threading.Lock()  # guards _series_cache only, and never across a computation
_fit_lock = threading.Lock()  # one cold computation at a time
_series_cache: OrderedDict[str, HmmSeries] = OrderedDict()


class HmmBusyError(RuntimeError):
    """Another HMM computation held the fit lock past ``wait_seconds``. Nothing was computed."""


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not installed"


@functools.lru_cache(maxsize=1)
def _stack() -> tuple[tuple[str, str], ...]:
    digest = hashlib.sha256()
    folder = Path(str(hedgefund_filter.__file__)).parent
    for name in _HEDGEFUND_SOURCES:
        digest.update(name.encode("utf-8"))
        digest.update((folder / name).read_bytes())
    versions = tuple((name, _package_version(name)) for name in _STACK_PACKAGES)
    return (("hedgefund_hmm_sources_sha256", digest.hexdigest()), *versions)


def hmm_stack() -> dict[str, str]:
    """The code a fit depends on: the sha256 of hedgeFund's three HMM source files, and the
    numpy, scipy, scikit-learn and hmmlearn versions. Read once per process, which is also when
    that code was imported."""
    return dict(_stack())


def _settings_key(settings: HmmSettings) -> bytes:
    """Everything besides the data that changes a fit, as canonical JSON bytes."""
    key = {
        "fit": settings.fit_key(),
        "algo_version": HMM_ALGO_VERSION,
        "stack": hmm_stack(),
    }
    return json.dumps(key, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fit_key(block: np.ndarray, columns: tuple[str, ...], settings: HmmSettings) -> str:
    digest = hashlib.sha256()
    digest.update(np.asarray(block, dtype=np.float64).tobytes())
    digest.update(json.dumps([list(block.shape), list(columns)]).encode("utf-8"))
    digest.update(_settings_key(settings))
    return digest.hexdigest()


def _series_key(
    close: pd.Series, settings: HmmSettings, bar_seconds: int, observed: np.ndarray | None
) -> str:
    digest = hashlib.sha256()
    digest.update(pd.DatetimeIndex(close.index).as_unit("ns").asi8.tobytes())
    digest.update(close.to_numpy(dtype=np.float64).tobytes())
    digest.update(str(int(bar_seconds)).encode("utf-8"))
    if observed is not None:  # all-True is normalised to None: it is the same series
        digest.update(np.asarray(observed, dtype=bool).tobytes())
    digest.update(_settings_key(settings))
    return digest.hexdigest()


class _FitStore:
    """The SQLite fit cache behind one :func:`hmm_series` computation; ``path=None`` stores
    nothing.

    The file is opened on first use and never raises: a cache that cannot be read or written
    only costs a refit, and the result is the same."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def _connection(self, path: Path) -> sqlite3.Connection:
        if self._conn is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=_SQLITE_TIMEOUT_SECONDS)
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS fits (key TEXT PRIMARY KEY, n_states INTEGER, "
                    "n_features INTEGER, startprob BLOB, transmat BLOB, means BLOB, covars BLOB, "
                    "posterior BLOB)"
                )
                conn.commit()
            except sqlite3.Error:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def get(self, key: str) -> FittedBlock | None:
        if self._path is None:
            return None
        try:
            row = (
                self._connection(self._path)
                .execute(
                    "SELECT n_states, n_features, startprob, transmat, means, covars, posterior "
                    "FROM fits WHERE key = ?",
                    (key,),
                )
                .fetchone()
            )
        except (OSError, sqlite3.Error):
            return None
        return None if row is None else _fitted_from_row(row)

    def put(self, key: str, fitted: FittedBlock) -> None:
        if self._path is None:
            return
        params = fitted.params
        arrays = [np.asarray(getattr(params, name), dtype=np.float64) for name in _FIT_ARRAYS]
        arrays.append(np.asarray(fitted.posterior, dtype=np.float64))
        try:
            conn = self._connection(self._path)
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO fits VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, params.n_states, params.n_features, *(a.tobytes() for a in arrays)),
                )
        except (OSError, sqlite3.Error):
            return

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _fitted_from_row(row: tuple[Any, ...]) -> FittedBlock | None:
    """The stored fit, or None for a row that does not decode into a valid one."""
    n_states, n_features, *blobs = row
    shapes = (
        (n_states,),
        (n_states, n_states),
        (n_states, n_features),
        (n_states, n_features, n_features),
        (n_states,),
    )
    try:
        arrays = [
            np.frombuffer(blob, dtype=np.float64).reshape(shape).copy()
            for blob, shape in zip(blobs, shapes, strict=True)
        ]
        posterior = arrays.pop()
        fitted = FittedBlock(HMMParams(*arrays), posterior)
    except (TypeError, ValueError):
        return None
    valid = np.isfinite(posterior).all() and (posterior >= 0.0).all()
    return fitted if valid and np.isclose(posterior.sum(), 1.0) else None


def _pool_workers() -> int:
    """Pool size: one CPU stays free, and never above :data:`_MAX_POOL_WORKERS`."""
    return min(_MAX_POOL_WORKERS, (os.cpu_count() or 2) - 1)


def _fit_in_pool(
    missing: Mapping[str, RefitBlock],
    settings: HmmSettings,
    store: _FitStore,
    workers: int,
) -> dict[str, FittedBlock]:
    """The fits a spawn pool finished, each stored as it arrives. Spawn, not fork: this runs
    inside threaded servers. When the pool cannot start or breaks, one warning, and the caller
    fits the rest serially."""
    fitted: dict[str, FittedBlock] = {}
    context = multiprocessing.get_context("spawn")
    try:
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            futures = {
                pool.submit(fit_block, item.block, item.columns, settings): key
                for key, item in missing.items()
            }
            failure: Exception | None = None
            for future in as_completed(futures):
                try:
                    result = future.result()
                except _POOL_FAILURES:
                    raise
                except (RuntimeError, ValueError) as exc:  # one block: keep the finished fits
                    failure = failure or exc
                    continue
                key = futures[future]
                fitted[key] = result
                store.put(key, result)
            if failure is not None:
                raise failure
    except _POOL_FAILURES as exc:
        _log.warning(
            "HMM fit pool failed after %d of %d fits (%s: %s); fitting the rest serially",
            len(fitted),
            len(missing),
            type(exc).__name__,
            exc,
        )
    return fitted


def _fit_blocks(
    blocks: Sequence[RefitBlock], settings: HmmSettings, store: _FitStore
) -> dict[str, FittedBlock]:
    """``{fit key: fit}`` for ``blocks``: hits from ``store``, misses computed and stored."""
    fitted: dict[str, FittedBlock] = {}
    missing: dict[str, RefitBlock] = {}
    for item in blocks:
        key = _fit_key(item.block, item.columns, settings)
        if key in fitted or key in missing:
            continue
        hit = store.get(key)
        if hit is None:
            missing[key] = item
        else:
            fitted[key] = hit
    workers = _pool_workers()
    if len(missing) > _SERIAL_MISSES and workers > 1:
        fitted.update(_fit_in_pool(missing, settings, store, workers))
    for key, item in missing.items():
        if key not in fitted:
            fitted[key] = fit_block(item.block, item.columns, settings)
            store.put(key, fitted[key])
    return fitted


def _precomputed_fit(fitted: Mapping[str, FittedBlock]) -> FitFn:
    def fit(block: np.ndarray, columns: tuple[str, ...], settings: HmmSettings) -> FittedBlock:
        return fitted[_fit_key(block, columns, settings)]

    return fit


def _read_only(values: np.ndarray) -> np.ndarray:
    values.setflags(write=False)
    return values


def _cached_series(key: str) -> HmmSeries | None:
    with _series_lock:
        cached = _series_cache.get(key)
        if cached is not None:
            _series_cache.move_to_end(key)
        return cached


def _remember_series(key: str, series: HmmSeries) -> None:
    with _series_lock:
        _series_cache[key] = series
        while len(_series_cache) > _SERIES_CACHE_SIZE:
            _series_cache.popitem(last=False)


def _compute_series(
    close: pd.Series,
    settings: HmmSettings,
    bar_seconds: int,
    cache_dir: Path | None,
    observed: np.ndarray | None,
) -> HmmSeries:
    blocks = refit_blocks(close, settings, bar_seconds=bar_seconds, observed=observed)
    store = _FitStore(None if cache_dir is None else Path(cache_dir) / FIT_CACHE_NAME)
    try:
        fitted = _fit_blocks(blocks, settings, store)
    finally:
        store.close()
    computed = causal_hmm(
        close,
        settings,
        bar_seconds=bar_seconds,
        fit=_precomputed_fit(fitted),
        observed=observed,
    )
    return replace(
        computed,
        state=_read_only(computed.state),
        bear_prob=_read_only(computed.bear_prob),
        state_prob=_read_only(computed.state_prob),
        bear_high_vol=_read_only(computed.bear_high_vol),
    )


def hmm_series(
    close: pd.Series,
    settings: HmmSettings,
    *,
    bar_seconds: int,
    cache_dir: Path | None,
    observed: np.ndarray | None = None,
    wait_seconds: float | None = None,
) -> HmmSeries:
    """:func:`lab.registry.hmm.causal_hmm` through both cache levels (module docstring).

    The returned arrays are shared and read-only. ``cache_dir=None`` means in-memory only.
    ``observed`` (bool per row, None = all) marks rows that are not observations. A cached series
    returns at once. ``wait_seconds=None`` waits for a running computation; a number waits that
    long and then raises :class:`HmmBusyError`."""
    if observed is not None:
        observed = np.asarray(observed, dtype=bool)
        if observed.all():
            observed = None
    key = _series_key(close, settings, bar_seconds, observed)
    cached = _cached_series(key)
    if cached is not None:
        return cached
    if wait_seconds is None:
        _fit_lock.acquire()
    elif not _fit_lock.acquire(timeout=max(0.0, float(wait_seconds))):
        raise HmmBusyError(
            f"another HMM computation is running; gave up after {wait_seconds:g} s, try again"
        )
    try:
        cached = _cached_series(key)  # the computation this call waited for may be this series
        if cached is not None:
            return cached
        series = _compute_series(close, settings, bar_seconds, cache_dir, observed)
        _remember_series(key, series)
        return series
    finally:
        _fit_lock.release()


def attach_hmm_columns(
    candles: pd.DataFrame, settings: HmmSettings, *, interval: str, cache_dir: Path | None
) -> pd.DataFrame:
    """Copy of ``candles`` plus the ``hmm_series_*`` columns, computed on this frame. A
    zero-volume row is not an observation."""
    close = candles["close"].astype(np.float64)
    series = hmm_series(
        close,
        settings,
        bar_seconds=interval_seconds(interval),
        cache_dir=cache_dir,
        observed=candles["volume"].to_numpy(dtype=np.float64) > 0.0,
    )
    out = candles.copy()
    out[HMM_STATE_COLUMN] = series.state.copy()  # a cached array is never shared
    out[HMM_BEAR_PROB_COLUMN] = series.bear_prob.copy()
    out[HMM_STATE_PROB_COLUMN] = series.state_prob.copy()
    out[HMM_BEAR_HIGH_VOL_COLUMN] = series.bear_high_vol.copy()
    return out


def hmm_scored_start(history: pd.DataFrame) -> pd.Timestamp | None:
    """The first bar of ``history`` with a finite HMM output, read from the column so it is one
    start per symbol and interval whichever HMM feature a plan uses; None without one."""
    if HMM_BEAR_PROB_COLUMN not in history.columns:
        return None
    finite = np.flatnonzero(np.isfinite(history[HMM_BEAR_PROB_COLUMN].to_numpy(dtype=np.float64)))
    return pd.Timestamp(history.index[finite[0]]) if finite.size else None


def plan_hmm_features(plan: Plan | Mapping[str, Any] | str) -> tuple[str, ...]:
    """The HMM feature names ``plan`` uses, in ``HMM_FEATURE_NAMES`` order; ``()`` for portfolio
    plans, plans without HMM features and text that is not a plan. Overlay plans count."""
    used: set[str] = set()
    if isinstance(plan, Plan):
        used = {ref.feature for ref in plan.features}
    else:
        if isinstance(plan, str):
            try:
                plan = json.loads(plan)
            except (ValueError, RecursionError):
                return ()
        if not isinstance(plan, Mapping) or plan.get("kind") == "portfolio":
            return ()
        features = plan.get("features")
        if isinstance(features, Mapping):
            for body in features.values():
                if isinstance(body, Mapping):
                    used.update(str(name) for name in body)
    return tuple(name for name in HMM_FEATURE_NAMES if name in used)


def require_hmm_columns(
    plan: Plan | Mapping[str, Any] | str,
    candles: pd.DataFrame,
    *,
    symbol: str,
    interval: str,
) -> None:
    """Raise :class:`DataUnavailableError` when ``candles`` lack an ``hmm_series_*`` column
    ``plan`` reads, or carry it with no finite value at all. The message names the symbol, the
    interval and ``[regime.hmm]``. Call it before any trial is recorded. No-op for plans without
    HMM features."""
    problems: list[str] = []
    reason = candles.attrs.get(HMM_UNAVAILABLE_ATTR)
    for name in plan_hmm_features(plan):
        column = _FEATURE_COLUMN[name]
        if column not in candles.columns:
            why = f" ({reason})" if reason else ""
            problems.append(f"{name} reads {column}, which is not loaded{why}")
        elif not np.isfinite(candles[column].to_numpy(dtype=np.float64)).any():
            problems.append(f"{name} has no value in the {len(candles)} bars used")
    if problems:
        raise DataUnavailableError(
            f"plan needs the causal HMM for {symbol} {interval}, which is missing: "
            f"{'; '.join(problems)}. lab.toml [regime.hmm] lists the symbols and intervals the "
            "loader fits, and its train_bars sets the warm-up (nothing was recorded in the "
            "ledger)"
        )


def require_frozen_hmm(plan: Plan | Mapping[str, Any] | str, config: Any, *, run_id: str) -> None:
    """Raise ``ValueError`` when ``plan`` uses an HMM feature and the run's frozen ``config`` has
    no ``hmm`` key: its evidence could not be tied to the settings and code that made the series.
    No-op for plans without HMM features, so an old run keeps working for every other plan."""
    used = plan_hmm_features(plan)
    if used and not (isinstance(config, Mapping) and "hmm" in config):
        raise ValueError(
            f"research run {run_id!r} froze no HMM settings, so plans using {', '.join(used)} "
            "cannot run in it; start a new run"
        )
