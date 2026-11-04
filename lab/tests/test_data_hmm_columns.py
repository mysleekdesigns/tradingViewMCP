"""The causal HMM's cache, columns and wiring (PLAN.md Phase 20 Part B).

Fit and series caches, the loader, the compiled warm-up, gate handling, the permutation null and
the frozen run settings. Synthetic prices only, and every path lives under ``tmp_path``.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import json
import pickle
import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import lab.data.hmm_columns as hc
from lab.config import LabConfig
from lab.data.derivatives_columns import DataUnavailableError
from lab.data.hmm_columns import (
    FIT_CACHE_NAME,
    HMM_UNAVAILABLE_ATTR,
    HmmBusyError,
    attach_hmm_columns,
    hmm_scored_start,
    hmm_series,
    hmm_stack,
    plan_hmm_features,
    require_hmm_columns,
)
from lab.evaluate.adversarial import permute_history
from lab.ledger import Ledger
from lab.plan import compile_plan, parse_plan
from lab.plan.validator import gate_base_expressions
from lab.registry.hmm import (
    HMM_ALGO_VERSION,
    HMM_BEAR_HIGH_VOL_COLUMN,
    HMM_BEAR_PROB_COLUMN,
    HMM_COLUMNS,
    HMM_STATE_COLUMN,
    HMM_STATE_PROB_COLUMN,
    FittedBlock,
    causal_hmm,
    fit_block,
    refit_blocks,
)
from lab.research.contract import load_research_config
from lab.research.overlay import _decision_window
from lab.research.runs import (
    check_lab_fingerprint,
    hmm_values,
    lab_fingerprint,
    lab_values,
    start_research_run,
)
from lab.runner import (
    GateBaseRefusedError,
    check_gate_base,
    default_loader,
    gate_base_share,
    plan_design_window,
    run_adversarial_report,
    run_backtest_report,
    ungated_plan,
)
from lab.types import Episode, HmmSettings, Window, interval_seconds
from tests.synthetic import make_candles, with_hmm
from tests.test_mcp_server import small_cfg as mcp_small_cfg
from tests.test_mcp_server import write_criteria
from tests.test_runner import EMA_PLAN, small_cfg

BAR = interval_seconds("4h")
SETTINGS = HmmSettings(
    n_states=2,
    vol_window_bars=10,
    train_bars=200,
    refit_every_bars=42,
    seed=7,
    restarts=1,
    n_iter=50,
    symbols=("BTCUSDT",),
    intervals=("4h",),
)
N_BARS = 340
N_FITS = 4  # decisions 210..339, one fit every 42
CANDLES = make_candles(3300, seed=17, start="2018-07-01", price=6000.0, vol=0.012)

GATED_PLAN: dict[str, Any] = {
    **EMA_PLAN,
    "name": "ema_hmm_gate",
    "features": {**EMA_PLAN["features"], "bear": {"hmm_bear_prob": {}}},
    "long_entry": "crosses_above(ema_fast, ema_slow) and bear < 0.5",
}
TRIVIAL_GATED_PLAN: dict[str, Any] = {
    **GATED_PLAN,
    "name": "volume_hmm_gate",
    "features": {"ema_slow": EMA_PLAN["features"]["ema_slow"], "bear": {"hmm_bear_prob": {}}},
    "long_entry": "volume >= 0 and bear < 0.5",
    "long_exit": "close < ema_slow",
}
OVERLAY_PLAN: dict[str, Any] = {
    "kind": "overlay",
    "name": "hmm_overlay",
    "description": "flat while the bear state is likely",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"bear": {"hmm_bear_prob": {}}},
    "flat_when": "bear > 0.5",
}


def close_series(n: int = N_BARS, *, seed: int = 3) -> pd.Series:
    return make_candles(n, seed=seed, vol=0.02)["close"]


def loader_of(candles: pd.DataFrame) -> Any:
    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        return candles.loc[candles.index < end]

    return load


@pytest.fixture(autouse=True)
def empty_series_cache() -> Iterator[None]:
    hc._series_cache.clear()
    yield
    hc._series_cache.clear()


@pytest.fixture(autouse=True)
def serial_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fit in this process unless a test asks for the pool: spawning costs seconds, and the
    fit counter below cannot cross a process boundary."""
    monkeypatch.setattr(hc, "_pool_workers", lambda: 1)


class FitCounter:
    """Stands in for ``fit_block`` in :mod:`lab.data.hmm_columns` and counts the real fits."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        monkeypatch.setattr(hc, "fit_block", self)

    def __call__(
        self, block: np.ndarray, columns: tuple[str, ...], settings: HmmSettings
    ) -> FittedBlock:
        self.calls += 1
        return fit_block(block, columns, settings)


@pytest.fixture
def fits(monkeypatch: pytest.MonkeyPatch) -> FitCounter:
    return FitCounter(monkeypatch)


def series_arrays(out: Any) -> tuple[np.ndarray, ...]:
    return (out.state, out.bear_prob, out.state_prob, out.bear_high_vol)


def assert_same_series(a: Any, b: Any) -> None:
    for left, right in zip(series_arrays(a), series_arrays(b), strict=True):
        assert np.array_equal(left, right, equal_nan=True)
    assert a.warmup_bars == b.warmup_bars


# --------------------------------------------------------------------------- series and fits


def test_series_equals_the_uncached_computation(tmp_path: Path) -> None:
    close = close_series()
    got = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert_same_series(got, causal_hmm(close, SETTINGS, bar_seconds=BAR))


def test_second_call_does_no_fit_and_takes_milliseconds(tmp_path: Path, fits: FitCounter) -> None:
    close = close_series()
    first = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == N_FITS
    start = time.perf_counter()
    again = hmm_series(close.copy(), SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert time.perf_counter() - start < 0.05
    assert fits.calls == N_FITS
    assert again is first


def test_a_new_process_reads_every_fit_from_disk(tmp_path: Path, fits: FitCounter) -> None:
    close = close_series()
    first = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    hc._series_cache.clear()  # what a new process starts with
    second = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == N_FITS
    assert_same_series(first, second)


def test_cache_dir_none_writes_nothing_and_keeps_the_series_in_memory(
    tmp_path: Path, fits: FitCounter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    close = close_series()
    hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=None)
    hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=None)
    assert fits.calls == N_FITS
    assert not list(tmp_path.iterdir())


def test_a_new_bar_needs_at_most_one_new_fit(tmp_path: Path, fits: FitCounter) -> None:
    close = close_series(N_BARS + 50)
    hmm_series(close.iloc[:N_BARS], SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == N_FITS
    hmm_series(close.iloc[: N_BARS + 1], SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == N_FITS  # no refit bar crossed
    next_refit = 210 + N_FITS * SETTINGS.refit_every_bars
    full = hmm_series(close.iloc[: next_refit + 1], SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == N_FITS + 1
    assert_same_series(full, causal_hmm(close.iloc[: next_refit + 1], SETTINGS, bar_seconds=BAR))


@pytest.mark.parametrize(
    "changed",
    [{"n_iter": 51}, {"seed": 8}, {"restarts": 2}, {"n_states": 3}],
)
def test_a_changed_setting_refits(
    tmp_path: Path, fits: FitCounter, changed: dict[str, int]
) -> None:
    close = close_series()
    hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    hmm_series(close, replace(SETTINGS, **changed), bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == 2 * N_FITS


def test_symbols_and_intervals_are_not_part_of_the_key(tmp_path: Path, fits: FitCounter) -> None:
    close = close_series()
    hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    hmm_series(close, replace(SETTINGS, symbols=("ETHUSDT",)), bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == N_FITS


@pytest.mark.parametrize(
    "stack_entry",
    ["hedgefund_hmm_sources_sha256", "numpy", "scipy", "scikit-learn", "hmmlearn"],
)
def test_each_part_of_the_code_stack_is_in_the_key(
    tmp_path: Path, fits: FitCounter, monkeypatch: pytest.MonkeyPatch, stack_entry: str
) -> None:
    close = close_series()
    hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    hc._series_cache.clear()
    changed = {**hmm_stack(), stack_entry: "something else"}
    monkeypatch.setattr(hc, "hmm_stack", lambda: changed)
    hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == 2 * N_FITS


def test_the_algo_version_is_in_the_key(
    tmp_path: Path, fits: FitCounter, monkeypatch: pytest.MonkeyPatch
) -> None:
    close = close_series()
    hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    hc._series_cache.clear()
    monkeypatch.setattr(hc, "HMM_ALGO_VERSION", HMM_ALGO_VERSION + 1)
    hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == 2 * N_FITS


def test_the_stack_hashes_hedgefunds_three_sources_and_names_the_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = hmm_stack()
    assert set(stack) == {
        "hedgefund_hmm_sources_sha256",
        "numpy",
        "scipy",
        "scikit-learn",
        "hmmlearn",
    }
    assert stack["numpy"] == np.__version__
    folder = tmp_path / "regime"
    folder.mkdir()
    for name in ("hmm.py", "hmm_filter.py", "hmm_features.py"):
        (folder / name).write_text(name)
    monkeypatch.setattr(hc.hedgefund_filter, "__file__", str(folder / "hmm_filter.py"))
    hc._stack.cache_clear()
    first = hmm_stack()
    assert first != stack
    (folder / "hmm_features.py").write_text("edited")
    hc._stack.cache_clear()
    assert hmm_stack()["hedgefund_hmm_sources_sha256"] != first["hedgefund_hmm_sources_sha256"]
    monkeypatch.undo()
    hc._stack.cache_clear()
    assert hmm_stack() == stack


def test_fits_are_raw_bytes_with_shapes_and_round_trip_exactly(tmp_path: Path) -> None:
    captured: list[FittedBlock] = []
    original = hc._FitStore.put

    def spy(self: Any, key: str, fitted: FittedBlock) -> None:
        captured.append(fitted)
        original(self, key, fitted)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hc._FitStore, "put", spy)
        hmm_series(close_series(), SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    conn = sqlite3.connect(tmp_path / FIT_CACHE_NAME)
    rows = conn.execute(
        "SELECT n_states, n_features, startprob, transmat, means, covars, posterior FROM fits"
    ).fetchall()
    conn.close()
    assert len(rows) == len(captured) == N_FITS
    for row in rows:
        assert row[:2] == (2, 2)
        assert [len(blob) for blob in row[2:]] == [16, 32, 32, 64, 16]  # float64, not a pickle
    decoded = [hc._fitted_from_row(row) for row in rows]
    for want in captured:
        assert any(
            d is not None
            and np.array_equal(d.params.transmat, want.params.transmat)
            and np.array_equal(d.params.covars, want.params.covars)
            and np.array_equal(d.params.means, want.params.means)
            and np.array_equal(d.params.startprob, want.params.startprob)
            and np.array_equal(d.posterior, want.posterior)
            for d in decoded
        )


def test_a_cached_series_replays_no_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    close = close_series()
    first = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    hc._series_cache.clear()
    monkeypatch.setattr("lab.registry.hmm.forward_filter", None)  # a replay would raise
    second = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert_same_series(first, second)


@pytest.mark.parametrize(
    "update",
    [
        "UPDATE fits SET transmat = x'00ff'",
        "UPDATE fits SET covars = NULL",
        "UPDATE fits SET n_states = 3",
        "UPDATE fits SET startprob = zeroblob(16)",  # valid bytes, not a probability vector
        "UPDATE fits SET posterior = zeroblob(16)",
        "UPDATE fits SET posterior = NULL",
    ],
)
def test_unreadable_rows_fall_back_to_refitting(
    tmp_path: Path, fits: FitCounter, update: str
) -> None:
    close = close_series()
    first = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    conn = sqlite3.connect(tmp_path / FIT_CACHE_NAME)
    with conn:
        conn.execute(update)
    conn.close()
    hc._series_cache.clear()
    second = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == 2 * N_FITS
    assert_same_series(first, second)


def test_a_cache_file_that_is_not_a_database_still_gives_the_series(
    tmp_path: Path, fits: FitCounter
) -> None:
    (tmp_path / FIT_CACHE_NAME).write_bytes(b"not a sqlite file" * 100)
    close = close_series()
    got = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == N_FITS
    assert_same_series(got, causal_hmm(close, SETTINGS, bar_seconds=BAR))


def test_two_callers_never_fit_the_same_series_twice(tmp_path: Path, fits: FitCounter) -> None:
    close = close_series()
    results: list[Any] = []

    def call() -> None:
        results.append(hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert fits.calls == N_FITS
    assert len(results) == 4
    assert all(r is results[0] for r in results)


def test_the_series_cache_is_bounded(fits: FitCounter) -> None:
    settings = replace(SETTINGS, train_bars=1_000)  # no fit runs: only the bound is under test
    for k in range(hc._SERIES_CACHE_SIZE + 3):
        hmm_series(close_series(30, seed=k), settings, bar_seconds=BAR, cache_dir=None)
    assert len(hc._series_cache) == hc._SERIES_CACHE_SIZE
    assert fits.calls == 0


def test_cached_arrays_are_read_only(tmp_path: Path) -> None:
    got = hmm_series(close_series(), SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    for values in series_arrays(got):
        with pytest.raises(ValueError, match="read-only"):
            values[0] = 1.0


# --------------------------------------------------------------------------- fit pool


def real_pool(monkeypatch: pytest.MonkeyPatch, workers: int = 2) -> None:
    monkeypatch.setattr(hc, "_pool_workers", lambda: workers)


class PickledPool:
    """Each fit crosses pickle both ways, as it does to a worker, and runs in this process, so
    a sandbox that denies semaphores can still check what a worker receives and returns."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def __enter__(self) -> PickledPool:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def submit(self, fn: Any, *args: Any) -> Future[Any]:
        fn, args = pickle.loads(pickle.dumps((fn, args)))
        future: Future[Any] = Future()
        future.set_result(pickle.loads(pickle.dumps(fn(*args))))
        return future


def test_what_crosses_to_a_worker_gives_a_bit_identical_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    close = close_series()
    block = refit_blocks(close, SETTINGS, bar_seconds=BAR)[0].block
    assert pickle.loads(pickle.dumps(block)).flags.f_contiguous == block.flags.f_contiguous
    serial = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=None)
    hc._series_cache.clear()
    real_pool(monkeypatch)
    monkeypatch.setattr(hc, "ProcessPoolExecutor", PickledPool)
    pooled = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert not caplog.records
    assert_same_series(serial, pooled)


@pytest.mark.slow
def test_pool_and_serial_give_bit_identical_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Real spawned workers: this one needs semaphores, so it fails under a sandbox that denies
    them, and that failure is the signal. There the code falls back to serial."""
    close = close_series()
    serial = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=None)
    hc._series_cache.clear()
    real_pool(monkeypatch)
    spawned: list[int] = []
    original = hc.ProcessPoolExecutor

    def counting_pool(*args: Any, **kwargs: Any) -> Any:
        pool = original(*args, **kwargs)
        spawned.append(kwargs["max_workers"])
        return pool

    monkeypatch.setattr(hc, "ProcessPoolExecutor", counting_pool)
    pooled = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert spawned == [2]
    assert "fit pool failed" not in caplog.text  # the pool really ran, not the fallback
    assert_same_series(serial, pooled)
    conn = sqlite3.connect(tmp_path / FIT_CACHE_NAME)
    assert conn.execute("SELECT count(*) FROM fits").fetchone() == (N_FITS,)
    conn.close()


class DeniedPool:
    """A sandbox that refuses semaphores: the pool cannot even start."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(1, "Operation not permitted")


class BreaksAfterOnePool:
    """A pool whose workers die after the first fit."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.submitted = 0

    def __enter__(self) -> BreaksAfterOnePool:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def submit(self, fn: Any, *args: Any) -> Future[Any]:
        self.submitted += 1
        future: Future[Any] = Future()
        if self.submitted == 1:
            future.set_result(fn(*args))
        else:
            future.set_exception(BrokenProcessPool("a worker died"))
        return future


@pytest.mark.parametrize("pool", [DeniedPool, BreaksAfterOnePool])
def test_a_pool_that_fails_falls_back_to_serial_with_one_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    pool: type,
) -> None:
    close = close_series()
    want = causal_hmm(close, SETTINGS, bar_seconds=BAR)
    fits = FitCounter(monkeypatch)
    real_pool(monkeypatch)
    monkeypatch.setattr(hc, "ProcessPoolExecutor", pool)
    with caplog.at_level("WARNING", logger=hc.__name__):
        got = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert_same_series(got, want)
    assert N_FITS <= fits.calls <= N_FITS + 1  # at most the one fit the broken pool finished
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "fit pool failed" in caplog.text


@pytest.mark.parametrize("error", [RuntimeError, ValueError])
def test_one_failed_block_raises_after_every_finished_fit_is_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    class OneBlockFailsPool(BreaksAfterOnePool):
        def submit(self, fn: Any, *args: Any) -> Future[Any]:
            self.submitted += 1
            future: Future[Any] = Future()
            if self.submitted == 2:
                future.set_exception(error("every Baum-Welch restart failed"))
            else:
                future.set_result(fn(*args))
            return future

    close = close_series()
    fits = FitCounter(monkeypatch)
    real_pool(monkeypatch)
    monkeypatch.setattr(hc, "ProcessPoolExecutor", OneBlockFailsPool)
    with pytest.raises(error, match="restart failed"):
        hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == N_FITS - 1
    conn = sqlite3.connect(tmp_path / FIT_CACHE_NAME)
    assert conn.execute("SELECT count(*) FROM fits").fetchone() == (N_FITS - 1,)
    conn.close()
    # the next request repeats only the block that failed, serially
    monkeypatch.setattr(hc, "_pool_workers", lambda: 1)
    got = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == N_FITS
    assert_same_series(got, causal_hmm(close, SETTINGS, bar_seconds=BAR))


@pytest.mark.parametrize("misses", [0, 1, 2])
def test_one_or_two_misses_never_start_a_pool(
    tmp_path: Path, fits: FitCounter, monkeypatch: pytest.MonkeyPatch, misses: int
) -> None:
    close = close_series(N_BARS + 2 * SETTINGS.refit_every_bars)
    next_refit = 210 + N_FITS * SETTINGS.refit_every_bars
    hmm_series(close.iloc[:N_BARS], SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    real_pool(monkeypatch, workers=4)
    monkeypatch.setattr(hc, "ProcessPoolExecutor", DeniedPool)  # starting one would warn
    grown = close.iloc[: next_refit + (misses - 1) * SETTINGS.refit_every_bars + 1]
    if misses == 0:
        grown = close.iloc[: N_BARS + 1]
    hc._series_cache.clear()
    hmm_series(grown, SETTINGS, bar_seconds=BAR, cache_dir=tmp_path)
    assert fits.calls == N_FITS + misses


@pytest.mark.parametrize(
    ("cpus", "want"), [(None, 1), (1, 0), (2, 1), (3, 2), (5, 4), (8, 4), (64, 4)]
)
def test_the_pool_leaves_one_cpu_and_never_exceeds_four_workers(
    monkeypatch: pytest.MonkeyPatch, cpus: int | None, want: int
) -> None:
    monkeypatch.undo()  # the autouse serial_fits fixture replaced _pool_workers
    monkeypatch.setattr(hc.os, "cpu_count", lambda: cpus)
    assert hc._pool_workers() == want


# --------------------------------------------------------------------------- locks


class BlockingFit:
    """Stands in for ``fit_block`` and holds the fit lock until the test releases it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        monkeypatch.setattr(hc, "fit_block", self)

    def __call__(
        self, block: np.ndarray, columns: tuple[str, ...], settings: HmmSettings
    ) -> FittedBlock:
        self.entered.set()
        assert self.release.wait(30.0)
        return fit_block(block, columns, settings)


def in_thread(call: Any) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []
    thread = threading.Thread(target=lambda: out.append(call()))
    thread.start()
    return thread, out


def test_a_cached_read_never_waits_behind_a_running_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    cached_close = close_series(seed=4)
    first = hmm_series(cached_close, SETTINGS, bar_seconds=BAR, cache_dir=None)
    blocking = BlockingFit(monkeypatch)
    close = close_series(seed=5)
    thread, _ = in_thread(
        lambda: hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=None),
    )
    try:
        assert blocking.entered.wait(30.0)
        start = time.perf_counter()
        again = hmm_series(cached_close, SETTINGS, bar_seconds=BAR, cache_dir=None)
        assert time.perf_counter() - start < 0.5
        assert again is first
    finally:
        blocking.release.set()
        thread.join()


def test_wait_seconds_raises_busy_and_a_later_call_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocking = BlockingFit(monkeypatch)
    close = close_series(seed=5)
    other = close_series(seed=6)
    thread, _ = in_thread(
        lambda: hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=None),
    )
    try:
        assert blocking.entered.wait(30.0)
        with pytest.raises(HmmBusyError, match="another HMM computation"):
            hmm_series(other, SETTINGS, bar_seconds=BAR, cache_dir=None, wait_seconds=0.01)
    finally:
        blocking.release.set()
        thread.join()
    assert issubclass(HmmBusyError, RuntimeError)
    assert hmm_series(other, SETTINGS, bar_seconds=BAR, cache_dir=None, wait_seconds=30.0)


def test_a_waiter_for_the_same_series_reuses_the_result_without_fitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocking = BlockingFit(monkeypatch)
    close = close_series(seed=5)
    thread, firsts = in_thread(
        lambda: hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=None),
    )
    assert blocking.entered.wait(30.0)
    waiter, seconds = in_thread(
        lambda: hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=None),
    )
    blocking.release.set()
    thread.join()
    waiter.join()
    assert seconds[0] is firsts[0]


def test_the_lock_is_released_when_a_fit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fails(*_args: object) -> None:
        raise RuntimeError("every Baum-Welch restart failed")

    monkeypatch.setattr(hc, "fit_block", fails)
    with pytest.raises(RuntimeError, match="restart failed"):
        hmm_series(close_series(), SETTINGS, bar_seconds=BAR, cache_dir=None)
    assert not hc._fit_lock.locked()
    assert not hc._series_lock.locked()


# --------------------------------------------------------------------------- attach


def test_attach_adds_the_three_columns_on_a_copy(tmp_path: Path) -> None:
    candles = make_candles(N_BARS, seed=3, vol=0.02)
    snapshot = candles.copy()
    got = attach_hmm_columns(candles, SETTINGS, interval="4h", cache_dir=tmp_path)
    pd.testing.assert_frame_equal(candles, snapshot)
    pd.testing.assert_frame_equal(got[list(candles.columns)], snapshot)
    assert list(got.columns) == [*candles.columns, *HMM_COLUMNS]
    want = causal_hmm(candles["close"], SETTINGS, bar_seconds=BAR)
    assert np.array_equal(got[HMM_STATE_COLUMN].to_numpy(), want.state, equal_nan=True)
    assert np.array_equal(got[HMM_BEAR_PROB_COLUMN].to_numpy(), want.bear_prob, equal_nan=True)
    assert np.array_equal(got[HMM_STATE_PROB_COLUMN].to_numpy(), want.state_prob, equal_nan=True)


def test_attach_marks_zero_volume_rows_as_not_observed(tmp_path: Path) -> None:
    candles = make_candles(N_BARS, seed=3, vol=0.02)
    plain = attach_hmm_columns(candles, SETTINGS, interval="4h", cache_dir=tmp_path)
    row = 280
    quiet = candles.copy()
    quiet.iloc[row, quiet.columns.get_loc("volume")] = 0.0
    got = attach_hmm_columns(quiet, SETTINGS, interval="4h", cache_dir=tmp_path)
    mask = np.ones(N_BARS, dtype=bool)
    mask[row] = False
    want = causal_hmm(candles["close"], SETTINGS, bar_seconds=BAR, observed=mask)
    assert np.array_equal(got[HMM_BEAR_PROB_COLUMN].to_numpy(), want.bear_prob, equal_nan=True)
    assert np.array_equal(
        got[HMM_BEAR_HIGH_VOL_COLUMN].to_numpy(), want.bear_high_vol, equal_nan=True
    )
    assert np.isnan(got[HMM_BEAR_PROB_COLUMN].to_numpy()[row])
    # same closes, different mask: a different series, so the cache key must differ
    assert not np.array_equal(
        got[HMM_BEAR_PROB_COLUMN].to_numpy(),
        plain[HMM_BEAR_PROB_COLUMN].to_numpy(),
        equal_nan=True,
    )


def test_an_all_observed_mask_shares_the_series_cache_entry(fits: FitCounter) -> None:
    close = close_series()
    first = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=None)
    observed = np.ones(len(close), dtype=bool)
    again = hmm_series(close, SETTINGS, bar_seconds=BAR, cache_dir=None, observed=observed)
    assert again is first
    assert fits.calls == N_FITS


def test_editing_an_attached_frame_leaves_the_cache_alone(tmp_path: Path) -> None:
    candles = make_candles(N_BARS, seed=3, vol=0.02)
    got = attach_hmm_columns(candles, SETTINGS, interval="4h", cache_dir=tmp_path)
    got.loc[got.index[-1], HMM_BEAR_PROB_COLUMN] = -1.0
    again = attach_hmm_columns(candles, SETTINGS, interval="4h", cache_dir=tmp_path)
    assert again[HMM_BEAR_PROB_COLUMN].iloc[-1] != -1.0


def test_design_and_holdout_views_agree_on_every_design_bar(tmp_path: Path) -> None:
    candles = make_candles(N_BARS + 60, seed=3, vol=0.02)
    design = candles.iloc[:N_BARS]
    short = attach_hmm_columns(design, SETTINGS, interval="4h", cache_dir=tmp_path)
    long = attach_hmm_columns(candles, SETTINGS, interval="4h", cache_dir=tmp_path)
    for column in HMM_COLUMNS:
        left = short[column].to_numpy()
        right = long[column].to_numpy()[:N_BARS]
        assert np.array_equal(left, right, equal_nan=True), column


# --------------------------------------------------------------------------- plan inputs


@pytest.mark.parametrize(
    ("plan", "want"),
    [
        (EMA_PLAN, ()),
        (GATED_PLAN, ("hmm_bear_prob",)),
        (json.dumps(GATED_PLAN), ("hmm_bear_prob",)),
        (parse_plan(GATED_PLAN), ("hmm_bear_prob",)),
        (OVERLAY_PLAN, ("hmm_bear_prob",)),
        (parse_plan(OVERLAY_PLAN), ("hmm_bear_prob",)),
        (
            {"features": {"a": {"hmm_bear_prob": {}}, "b": {"hmm_state": {}}}},
            ("hmm_state", "hmm_bear_prob"),
        ),
        ({"kind": "portfolio", "features": {"a": {"hmm_state": {}}}}, ()),
        ("not json", ()),
        ({"features": "nope"}, ()),
    ],
)
def test_plan_hmm_features(plan: Any, want: tuple[str, ...]) -> None:
    assert plan_hmm_features(plan) == want


def test_require_hmm_columns_names_symbol_interval_and_section() -> None:
    candles = make_candles(30, seed=1)
    with pytest.raises(DataUnavailableError) as info:
        require_hmm_columns(GATED_PLAN, candles, symbol="ETHUSDT", interval="1h")
    message = str(info.value)
    assert "ETHUSDT" in message and "1h" in message and "[regime.hmm]" in message
    assert HMM_BEAR_PROB_COLUMN in message
    assert "nothing was recorded in the ledger" in message


def test_require_hmm_columns_refuses_a_column_that_is_all_warm_up() -> None:
    candles = with_hmm(make_candles(30, seed=1), seed=1)
    candles[HMM_BEAR_PROB_COLUMN] = np.nan
    with pytest.raises(DataUnavailableError, match="hmm_bear_prob has no value"):
        require_hmm_columns(GATED_PLAN, candles, symbol="BTCUSDT", interval="4h")


def test_require_hmm_columns_passes_a_plan_that_does_not_need_them() -> None:
    bare = make_candles(30, seed=1)
    require_hmm_columns(EMA_PLAN, bare, symbol="BTCUSDT", interval="4h")
    require_hmm_columns(GATED_PLAN, with_hmm(bare, seed=1), symbol="BTCUSDT", interval="4h")


# --------------------------------------------------------------------------- default_loader


def write_candles(cfg: LabConfig, candles: pd.DataFrame, symbol: str = "BTCUSDT") -> None:
    folder = cfg.parquet_dir / symbol / "4h"
    folder.mkdir(parents=True)
    for month, part in candles.groupby(candles.index.strftime("%Y-%m")):
        part.to_parquet(folder / f"{month}.parquet")


@pytest.fixture
def disk_cfg(tmp_path: Path) -> LabConfig:
    cfg = replace(small_cfg(tmp_path), hmm=SETTINGS)
    write_candles(cfg, make_candles(N_BARS, seed=4, start="2024-02-01", price=40_000.0))
    return cfg


def test_default_loader_attaches_on_exactly_the_frame_it_returned(disk_cfg: LabConfig) -> None:
    """A design load's columns are those of its own frame: no holdout close reaches them."""
    load = default_loader(disk_cfg)
    cut = pd.Timestamp("2024-03-30", tz="UTC")
    early = load("BTCUSDT", "4h", cut)
    assert early.index[-1] < cut
    want = causal_hmm(early["close"], SETTINGS, bar_seconds=BAR)
    assert np.array_equal(early[HMM_BEAR_PROB_COLUMN].to_numpy(), want.bear_prob, equal_nan=True)
    assert (disk_cfg.hmm_cache_dir / FIT_CACHE_NAME).is_file()


def test_default_loader_design_load_is_blind_to_later_closes(disk_cfg: LabConfig) -> None:
    """Change every close at or after the cut: the design load must not move at all."""
    cut = pd.Timestamp("2024-03-30", tz="UTC")
    base = default_loader(disk_cfg)("BTCUSDT", "4h", cut)
    frame = pd.concat(
        pd.read_parquet(p) for p in sorted((disk_cfg.parquet_dir / "BTCUSDT/4h").glob("*.parquet"))
    )
    frame.loc[frame.index >= cut, "close"] *= 3.0
    for month, part in frame.groupby(frame.index.strftime("%Y-%m")):
        part.to_parquet(disk_cfg.parquet_dir / "BTCUSDT" / "4h" / f"{month}.parquet")
    again = default_loader(disk_cfg)("BTCUSDT", "4h", cut)
    pd.testing.assert_frame_equal(base, again)


def failing_fit(*_args: object) -> None:
    raise RuntimeError("every Baum-Welch restart failed")


def failing_load(disk_cfg: LabConfig, monkeypatch: pytest.MonkeyPatch) -> pd.DataFrame:
    monkeypatch.setattr(hc, "fit_block", failing_fit)
    return default_loader(disk_cfg)("BTCUSDT", "4h", pd.Timestamp("2025-01-01", tz="UTC"))


def test_a_failed_fit_leaves_the_load_usable_with_the_reason_recorded(
    disk_cfg: LabConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="lab.runner"):
        got = failing_load(disk_cfg, monkeypatch)
    assert not set(HMM_COLUMNS) & set(got.columns)
    assert len(got) == N_BARS
    assert "restart failed" in got.attrs[HMM_UNAVAILABLE_ATTR]
    assert "no HMM columns for BTCUSDT 4h" in caplog.text


def test_a_value_error_from_the_fit_is_contained_too(
    disk_cfg: LabConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad(*_args: object) -> None:
        raise ValueError("x has 0 rows")

    monkeypatch.setattr(hc, "fit_block", bad)
    got = default_loader(disk_cfg)("BTCUSDT", "4h", pd.Timestamp("2025-01-01", tz="UTC"))
    assert "x has 0 rows" in got.attrs[HMM_UNAVAILABLE_ATTR]


def test_a_failed_fit_refuses_hmm_plans_quoting_it_and_no_other_plan(
    disk_cfg: LabConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = failing_load(disk_cfg, monkeypatch)
    with pytest.raises(DataUnavailableError, match="restart failed"):
        plan_design_window(parse_plan(GATED_PLAN), history, disk_cfg)
    with pytest.raises(DataUnavailableError, match="restart failed"):
        plan_design_window(parse_plan(OVERLAY_PLAN), history, disk_cfg)
    window, scoring = plan_design_window(parse_plan(EMA_PLAN), history, disk_cfg)
    assert scoring is None
    assert window.start == history.index[0]


def test_a_failed_fit_leaves_a_non_hmm_report_running(
    disk_cfg: LabConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(hc, "fit_block", failing_fit)
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_backtest_report(EMA_PLAN, cfg=disk_cfg, ledger=ledger, run_id="t")
    assert "metrics" in report


@pytest.mark.parametrize(
    "settings",
    [
        None,
        replace(SETTINGS, symbols=("ETHUSDT",)),
        replace(SETTINGS, intervals=("1d",)),
    ],
)
def test_default_loader_skips_series_outside_the_settings(
    disk_cfg: LabConfig, settings: HmmSettings | None
) -> None:
    got = default_loader(replace(disk_cfg, hmm=settings))(
        "BTCUSDT", "4h", pd.Timestamp("2025-01-01", tz="UTC")
    )
    assert not set(HMM_COLUMNS) & set(got.columns)
    assert not disk_cfg.hmm_cache_dir.exists()


# --------------------------------------------------------------------------- compiled warm-up


def warmed_candles(first_finite: int) -> pd.DataFrame:
    candles = with_hmm(CANDLES, seed=1, gaps=False)
    candles.iloc[:first_finite, [candles.columns.get_loc(c) for c in HMM_COLUMNS]] = np.nan
    return candles


def test_hmm_warm_up_is_the_first_finite_row() -> None:
    candles = warmed_candles(1500)
    plan = parse_plan(GATED_PLAN)
    sig = compile_plan(plan, candles)
    assert sig.warmup_bars == 1500 + 1  # one more for crosses_above
    assert not sig.entries.iloc[: sig.warmup_bars].any()


def test_overlay_warm_up_keeps_it_out_of_the_market_until_the_first_decision() -> None:
    candles = warmed_candles(1500)
    sig = compile_plan(parse_plan(OVERLAY_PLAN), candles)
    assert sig.warmup_bars == 1500
    assert not sig.entries.iloc[:1500].any() and not sig.exits.iloc[:1500].any()
    assert (sig.entries.iloc[1500:] ^ sig.exits.iloc[1500:]).all()


def test_overlay_decision_window_starts_at_the_first_fit_not_at_the_design_start() -> None:
    """Otherwise warm-up bars count as buy-and-hold and dilute the scored window."""
    first_finite = 1500
    candles = warmed_candles(first_finite)
    assert candles.index[first_finite] > pd.Timestamp("2019-01-01", tz="UTC")
    sig = compile_plan(parse_plan(OVERLAY_PLAN), candles)
    window = Window(
        start=pd.Timestamp("2019-01-01", tz="UTC"), end=candles.index[-1], kind="design"
    )
    assert _decision_window(candles, window, sig.warmup_bars).start == candles.index[first_finite]


def test_a_plan_with_no_hmm_output_never_signals() -> None:
    candles = warmed_candles(len(CANDLES))
    sig = compile_plan(parse_plan(GATED_PLAN), candles)
    assert sig.warmup_bars >= len(candles)
    assert not sig.entries.any() and not sig.exits.any()


def test_other_plans_keep_their_warm_up_with_the_columns_attached() -> None:
    plan = parse_plan(EMA_PLAN)
    bare = compile_plan(plan, CANDLES)
    with_columns = compile_plan(plan, warmed_candles(1500))
    assert bare.warmup_bars == with_columns.warmup_bars
    assert bare.warmup_bars < 1500
    pd.testing.assert_series_equal(bare.entries, with_columns.entries)


# --------------------------------------------------------------------------- gates and windows


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


def design_history(cfg: LabConfig, candles: pd.DataFrame) -> pd.DataFrame:
    return candles.loc[candles.index < cfg.splits.holdout_start]


def report_end(report: dict[str, Any]) -> pd.Timestamp:
    return pd.Timestamp(report["window"]["end"], unit="s", tz="UTC")


FIRST_FINITE = 1500  # 2019-03: after the design start, so warm-up would enter a plain window


def test_scored_start_is_the_first_finite_bar_and_absent_without_one() -> None:
    candles = warmed_candles(FIRST_FINITE)
    assert hmm_scored_start(candles) == candles.index[FIRST_FINITE]
    assert hmm_scored_start(CANDLES) is None
    assert hmm_scored_start(warmed_candles(len(CANDLES))) is None


@pytest.mark.parametrize("raw", [GATED_PLAN, OVERLAY_PLAN], ids=["edge", "overlay"])
def test_an_hmm_plan_is_scored_from_the_first_finite_bar(
    cfg: LabConfig, raw: dict[str, Any]
) -> None:
    history = design_history(cfg, warmed_candles(FIRST_FINITE))
    window, scoring = plan_design_window(parse_plan(raw), history, cfg)
    assert window.start == history.index[FIRST_FINITE]
    assert window.start > cfg.splits.design_start
    assert scoring is not None and scoring["note"].startswith("HMM plan")
    assert scoring["scored_from"] == int(window.start.timestamp())
    assert scoring["design_start_utc"] == "2019-01-01T00:00:00Z"


def test_one_start_whichever_hmm_feature_a_plan_uses(cfg: LabConfig) -> None:
    history = design_history(cfg, warmed_candles(FIRST_FINITE))
    state_plan = {
        **OVERLAY_PLAN,
        "features": {"regime": {"hmm_state": {}}},
        "flat_when": "regime < 0.5",
    }
    first = plan_design_window(parse_plan(OVERLAY_PLAN), history, cfg)[0]
    second = plan_design_window(parse_plan(state_plan), history, cfg)[0]
    assert first == second


def test_a_plan_without_hmm_features_keeps_exactly_its_window(cfg: LabConfig) -> None:
    window, scoring = plan_design_window(
        parse_plan(EMA_PLAN), design_history(cfg, warmed_candles(FIRST_FINITE)), cfg
    )
    bare = plan_design_window(parse_plan(EMA_PLAN), design_history(cfg, CANDLES), cfg)
    assert (window, scoring) == bare
    assert scoring is None


def test_warm_up_before_the_design_start_leaves_the_start_alone(cfg: LabConfig) -> None:
    history = design_history(cfg, warmed_candles(300))
    plan = parse_plan(GATED_PLAN)
    window, scoring = plan_design_window(plan, history, cfg)
    assert window == plan_design_window(parse_plan(EMA_PLAN), history, cfg)[0]
    assert scoring is not None and "price_clause_true_share" in scoring


def test_only_one_finite_bar_is_refused_before_any_trial(cfg: LabConfig) -> None:
    candles = warmed_candles(1)
    history = design_history(cfg, candles)
    candles = history.copy()
    columns = [candles.columns.get_loc(c) for c in HMM_COLUMNS]
    candles.iloc[:-1, columns] = np.nan
    with pytest.raises(DataUnavailableError, match="HMM scored start"):
        plan_design_window(parse_plan(GATED_PLAN), candles, cfg)


def test_gate_binding_share_and_every_metric_exclude_warm_up_bars(
    cfg: LabConfig, tmp_path: Path
) -> None:
    candles = warmed_candles(FIRST_FINITE)
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_backtest_report(
            GATED_PLAN, cfg=cfg, ledger=ledger, run_id="t", loader=loader_of(candles)
        )
    history = design_history(cfg, candles)
    start = history.index[FIRST_FINITE]
    assert report["window"]["start"] == int(start.timestamp())
    expected_bars = int((history.index >= start).sum())
    assert report["metrics"]["net"]["bars"] == expected_bars

    block = report["benchmarks"]["ungated"]
    base = compile_plan(parse_plan(block["plan"]), history).entries.to_numpy()
    gated = compile_plan(parse_plan(GATED_PLAN), history).entries.to_numpy()
    scored = (history.index >= start) & (history.index < report_end(report))
    assert block["price_clause_entry_bars"] == int((base & scored).sum())
    assert block["gate_binding_bars"] == int((base & ~gated & scored).sum())
    in_warm_up = int((base & ~scored & (history.index >= cfg.splits.design_start)).sum())
    assert in_warm_up > 0  # counting those bars as blocked would inflate the share


def test_the_base_trial_starts_at_the_scored_start(cfg: LabConfig, tmp_path: Path) -> None:
    candles = warmed_candles(FIRST_FINITE)
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_backtest_report(
            GATED_PLAN, cfg=cfg, ledger=ledger, run_id="t", loader=loader_of(candles)
        )
        trial = ledger.get(report["trial_id"])
    assert trial is not None
    assert trial.window_start == candles.index[FIRST_FINITE]


def test_an_always_true_price_clause_beside_an_hmm_gate_is_refused(cfg: LabConfig) -> None:
    history = design_history(cfg, warmed_candles(300))
    plan = parse_plan(TRIVIAL_GATED_PLAN)
    with pytest.raises(GateBaseRefusedError, match=r"HMM gate\(s\) `bear`"):
        plan_design_window(plan, history, cfg)
    window = plan_design_window(parse_plan(EMA_PLAN), history, cfg)[0]
    with pytest.raises(GateBaseRefusedError, match="de-facto entry signal"):
        check_gate_base(plan, history, window, cfg)


def test_gate_base_expressions_and_ungated_plan_strip_the_hmm_clause(cfg: LabConfig) -> None:
    plan = parse_plan(GATED_PLAN)
    assert gate_base_expressions(plan) is not None
    stripped = ungated_plan(plan)
    assert stripped is not None
    assert stripped.long_entry == "crosses_above(ema_fast, ema_slow)"
    assert {ref.feature for ref in stripped.features} == {"ema"}
    assert plan_hmm_features(stripped) == ()
    history = design_history(cfg, warmed_candles(300))
    window = plan_design_window(parse_plan(EMA_PLAN), history, cfg)[0]
    share = gate_base_share(plan, history, window)
    assert share is not None and 0.0 < share["price_clause_true_share"] < 0.9


def test_report_has_the_ungated_baseline_and_a_missing_column_records_nothing(
    cfg: LabConfig, tmp_path: Path
) -> None:
    candles = warmed_candles(300)
    with Ledger(tmp_path / "l.db") as ledger:
        with pytest.raises(DataUnavailableError, match=r"\[regime\.hmm\]"):
            run_backtest_report(GATED_PLAN, cfg=cfg, ledger=ledger, loader=loader_of(CANDLES))
        assert ledger.count() == 0
        report = run_backtest_report(
            GATED_PLAN, cfg=cfg, ledger=ledger, run_id="t", loader=loader_of(candles)
        )
    block = report["benchmarks"]["ungated"]
    assert block["gate_features"] == ["bear"]
    assert block["price_clause"] == "crosses_above(ema_fast, ema_slow)"
    assert report["scoring"]["note"].startswith("HMM plan")


# --------------------------------------------------------------------------- permutation null


def test_permutation_keeps_the_real_columns_and_a_path_never_refits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candles = warmed_candles(300)
    window = Window(start=candles.index[1200], end=candles.index[-1], kind="design")
    permuted = permute_history(candles, window, seed=5)
    assert not np.array_equal(permuted["close"].to_numpy(), candles["close"].to_numpy())
    for column in HMM_COLUMNS:
        left = permuted[column].to_numpy()
        assert np.array_equal(left, candles[column].to_numpy(), equal_nan=True), column

    def no_fit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a permutation path must not refit")

    monkeypatch.setattr(hc, "fit_block", no_fit)
    monkeypatch.setattr(hc, "causal_hmm", no_fit)
    assert compile_plan(parse_plan(GATED_PLAN), permuted).warmup_bars == 301


def test_adversarial_report_runs_a_gated_plan_without_a_refit(
    cfg: LabConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_fit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the permutation null must not refit")

    monkeypatch.setattr(hc, "fit_block", no_fit)
    monkeypatch.setattr(hc, "causal_hmm", no_fit)
    with Ledger(tmp_path / "l.db") as ledger:
        report = run_adversarial_report(
            GATED_PLAN, cfg=cfg, ledger=ledger, loader=loader_of(warmed_candles(300))
        )
    assert report["scoring"]["note"].startswith("HMM plan")


# --------------------------------------------------------------------------- frozen settings


EPISODE = Episode(
    "covid", pd.Timestamp("2020-03-01", tz="UTC"), pd.Timestamp("2020-04-01", tz="UTC")
)


@pytest.fixture
def research_cfg(tmp_path: Path) -> LabConfig:
    return replace(mcp_small_cfg(tmp_path), hmm=SETTINGS, episodes=(EPISODE,))


def start(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> str:
    research = load_research_config(write_criteria(tmp_path / "c.toml"))
    return start_research_run(ledger, research, label="hmm", cfg=cfg)


def frozen_config(ledger: Ledger, run_id: str) -> dict[str, Any]:
    run = ledger.research_run(run_id)
    assert run is not None
    return run["config"]


def test_settings_and_episodes_freeze_beside_the_fingerprint(
    tmp_path: Path, research_cfg: LabConfig
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        config = frozen_config(ledger, start(ledger, tmp_path, research_cfg))
    assert config["hmm"] == {
        "settings": {**SETTINGS.fit_key(), "symbols": ["BTCUSDT"], "intervals": ["4h"]},
        "algo_version": HMM_ALGO_VERSION,
        "stack": hmm_stack(),
        "gate_base_max_true_frac": research_cfg.derivatives.gate_base_max_true_frac,
    }
    assert config["episodes"] == [
        ["covid", "2020-03-01T00:00:00+00:00", "2020-04-01T00:00:00+00:00"]
    ]
    assert "hmm" not in lab_values(research_cfg)
    assert "episodes" not in lab_values(research_cfg)


def test_nothing_is_frozen_when_nothing_is_set(tmp_path: Path, research_cfg: LabConfig) -> None:
    bare = replace(research_cfg, hmm=None, episodes=())
    assert hmm_values(bare) is None
    with Ledger(tmp_path / "l.db") as ledger:
        config = frozen_config(ledger, start(ledger, tmp_path, bare))
    assert "hmm" not in config and "episodes" not in config


def test_the_fingerprint_ignores_hmm_settings_and_episodes(research_cfg: LabConfig) -> None:
    bare = replace(research_cfg, hmm=None, episodes=())
    assert lab_fingerprint(bare)["fingerprint"] == lab_fingerprint(research_cfg)["fingerprint"]


def test_unchanged_settings_pass(tmp_path: Path, research_cfg: LabConfig) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        check_lab_fingerprint(ledger, start(ledger, tmp_path, research_cfg), research_cfg)


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"hmm": replace(SETTINGS, train_bars=201)}, "train_bars 200 -> 201"),
        ({"hmm": replace(SETTINGS, symbols=("ETHUSDT",))}, "symbols"),
        ({"hmm": None}, "n_states 2 -> None"),
        ({"episodes": ()}, "[episodes]"),
    ],
)
def test_a_changed_setting_is_refused_and_named(
    tmp_path: Path, research_cfg: LabConfig, change: dict[str, Any], named: str
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        run_id = start(ledger, tmp_path, research_cfg)
        with pytest.raises(ValueError, match="start a new run") as info:
            check_lab_fingerprint(ledger, run_id, replace(research_cfg, **change))
    assert named in str(info.value)


def test_a_changed_algorithm_version_is_refused(
    tmp_path: Path, research_cfg: LabConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        run_id = start(ledger, tmp_path, research_cfg)
        monkeypatch.setattr("lab.research.runs.HMM_ALGO_VERSION", HMM_ALGO_VERSION + 1)
        changed = f"HMM_ALGO_VERSION {HMM_ALGO_VERSION} -> {HMM_ALGO_VERSION + 1}"
        with pytest.raises(ValueError, match=changed):
            check_lab_fingerprint(ledger, run_id, research_cfg)


@pytest.mark.parametrize(
    "entry", ["hedgefund_hmm_sources_sha256", "numpy", "scipy", "scikit-learn", "hmmlearn"]
)
def test_a_changed_code_stack_is_refused_and_named(
    tmp_path: Path, research_cfg: LabConfig, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        run_id = start(ledger, tmp_path, research_cfg)
        changed = {**hmm_stack(), entry: "something else"}
        monkeypatch.setattr("lab.research.runs.hmm_stack", lambda: changed)
        with pytest.raises(ValueError, match="start a new run") as info:
            check_lab_fingerprint(ledger, run_id, research_cfg)
    assert entry in str(info.value)


def test_a_changed_gate_limit_is_refused_and_named(tmp_path: Path, research_cfg: LabConfig) -> None:
    moved = replace(
        research_cfg,
        derivatives=replace(research_cfg.derivatives, gate_base_max_true_frac=0.5),
    )
    with Ledger(tmp_path / "l.db") as ledger:
        run_id = start(ledger, tmp_path, research_cfg)
        with pytest.raises(ValueError, match=r"gate_base_max_true_frac 0\.9 -> 0\.5"):
            check_lab_fingerprint(ledger, run_id, moved)


def test_a_run_without_the_keys_passes_whatever_the_settings_are_now(
    tmp_path: Path, research_cfg: LabConfig
) -> None:
    research = load_research_config(write_criteria(tmp_path / "old.toml"))
    config = json.loads(research.to_json())
    config["lab"] = lab_fingerprint(research_cfg)
    with Ledger(tmp_path / "l.db") as ledger:
        ledger.start_research_run(
            "r-20261001-000000-abcdef",
            label="old",
            config_json=json.dumps(config, sort_keys=True),
            config_sha256=research.sha256,
        )
        changed = replace(research_cfg, hmm=replace(SETTINGS, train_bars=999), episodes=())
        check_lab_fingerprint(ledger, "r-20261001-000000-abcdef", changed)


OLD_RUN = "r-20261001-000000-abcdef"


def start_old_run(ledger: Ledger, tmp_path: Path, cfg: LabConfig) -> None:
    research = load_research_config(write_criteria(tmp_path / "old.toml"))
    config = json.loads(research.to_json())
    config["lab"] = lab_fingerprint(cfg)
    ledger.start_research_run(
        OLD_RUN,
        label="old",
        config_json=json.dumps(config, sort_keys=True),
        config_sha256=research.sha256,
    )


@pytest.mark.parametrize(
    "plan", [GATED_PLAN, OVERLAY_PLAN, parse_plan(GATED_PLAN)], ids=["dict", "overlay", "parsed"]
)
def test_an_hmm_plan_is_refused_in_a_run_that_froze_no_settings(
    tmp_path: Path, research_cfg: LabConfig, plan: Any
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        start_old_run(ledger, tmp_path, research_cfg)
        with pytest.raises(ValueError, match=r"froze no HMM settings.*start a new run"):
            check_lab_fingerprint(ledger, OLD_RUN, research_cfg, plan=plan)


def test_a_plan_without_hmm_features_stays_accepted_in_an_old_run(
    tmp_path: Path, research_cfg: LabConfig
) -> None:
    with Ledger(tmp_path / "l.db") as ledger:
        start_old_run(ledger, tmp_path, research_cfg)
        check_lab_fingerprint(ledger, OLD_RUN, research_cfg, plan=EMA_PLAN)
        check_lab_fingerprint(ledger, OLD_RUN, research_cfg)


def test_a_backtest_of_an_hmm_plan_in_an_old_run_records_nothing(tmp_path: Path) -> None:
    cfg = small_cfg(tmp_path)
    with Ledger(tmp_path / "l.db") as ledger:
        start_old_run(ledger, tmp_path, cfg)
        with pytest.raises(DataUnavailableError, match="froze no HMM settings"):
            run_backtest_report(
                GATED_PLAN,
                cfg=cfg,
                ledger=ledger,
                run_id=OLD_RUN,
                loader=loader_of(warmed_candles(300)),
            )
        assert ledger.count(OLD_RUN) == 0
