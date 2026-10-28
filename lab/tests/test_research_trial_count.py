"""Phase 11 effective trial count from the ledger (lab.research.trial_count). Offline: hand-recorded
trials on synthetic, seeded return series with known answers (identical plans -> 1, independent
plans -> about N), plus gapped synthetic candles for the bar-time rebuild."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.config import LabConfig
from lab.ledger import Ledger
from lab.research.family import _to_grid
from lab.research.trial_count import (
    TRIAL_COUNT_GRID_SECONDS,
    TRIAL_COUNT_MIN_OVERLAP_DAYS,
    TRIAL_COUNT_NULL_SEED,
    _DailyGrid,
    clear_trial_count_cache,
    trial_counts,
)
from lab.types import TrialRecord, interval_seconds
from tests.synthetic import make_candles
from tests.test_mcp_server import small_cfg

DAY = 86_400
# Design window of small_cfg is [2018-07-01, 2019-10-01); 400 days fit inside it.
START = int(pd.Timestamp("2018-07-01", tz="UTC").timestamp())
DAYS = 400


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clear_trial_count_cache()
    yield
    clear_trial_count_cache()


@pytest.fixture
def cfg(tmp_path: Path) -> LabConfig:
    return small_cfg(tmp_path)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.db") as led:
        yield led


def no_candles(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
    raise ValueError(f"no candles for {symbol} {interval} in this test")


def record(
    ledger: Ledger,
    plan: str,
    returns: np.ndarray | None,
    *,
    run_id: str = "r1",
    symbol: str = "BTCUSDT",
    interval: str = "4h",
    start: int = START,
    end: int | None = None,
    kind: str = "design",
    source: str = "api",
) -> int:
    """One trial of plan ``plan`` (hash = the name repeated); ``returns`` None = error trial.
    ``end`` defaults to the contiguous window holding exactly ``len(returns)`` bars."""
    step = interval_seconds(interval)
    if end is None:
        end = start + step * (len(returns) if returns is not None else 10)
    ok = returns is not None
    return ledger.record(
        TrialRecord(
            run_id=run_id,
            source=source,  # type: ignore[arg-type]
            status="ok" if ok else "error",
            plan_hash=(plan * 64)[:64],
            plan_json="{}",
            params_json="{}",
            symbol=symbol,
            interval=interval,
            window_kind=kind,  # type: ignore[arg-type]
            window_start=pd.Timestamp(start, unit="s", tz="UTC"),
            window_end=pd.Timestamp(end, unit="s", tz="UTC"),
            metrics_json="{}",
            sharpe_per_bar=None,
            n_trades=0,
            error=None if ok else "boom",
            returns=returns,
        )
    )


def bars(days: int, interval: str) -> int:
    return days * DAY // interval_seconds(interval)


def noise(seed: int, n: int, scale: float = 0.01) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, scale, n)


def split_4h_into_1h(r4: np.ndarray, seed: int) -> np.ndarray:
    """1h simple returns whose four log returns per 4h bar sum to that bar's log return: the
    same position held on a finer bar size."""
    rng = np.random.default_rng(seed)
    log4 = np.log1p(r4)
    parts = rng.normal(0.0, 0.004, (r4.size, 4))
    parts[:, 3] = log4 - parts[:, :3].sum(axis=1)
    return np.expm1(parts.reshape(-1))


# --------------------------------------------------------------------------- known answers


def test_identical_plans_count_as_one(ledger: Ledger, cfg: LabConfig) -> None:
    r = noise(1, bars(DAYS, "4h"))
    for name in "abcde":
        record(ledger, name, r)
    tc = trial_counts(ledger, cfg, loader=no_candles)
    assert tc.nominal == 5 and tc.n_with_returns == 5 and tc.n_without_returns == 0
    assert tc.effective == pytest.approx(1.0, abs=1e-9)  # float rounding only


def test_same_strategy_on_1h_and_4h_counts_as_one(ledger: Ledger, cfg: LabConfig) -> None:
    """Aligned by timestamp on the daily grid, a 1h and a 4h series holding the same position
    have identical daily log returns."""
    r4 = noise(2, bars(DAYS, "4h"))
    record(ledger, "a", r4, interval="4h")
    record(ledger, "b", split_4h_into_1h(r4, seed=3), interval="1h")
    tc = trial_counts(ledger, cfg, loader=no_candles)
    assert tc.nominal == 2
    assert tc.effective == pytest.approx(1.0, abs=1e-6)  # expm1/log1p round trip only


def test_independent_plans_count_about_n(ledger: Ledger, cfg: LabConfig) -> None:
    """M independent series over T days: the sample correlations are ~N(0, 1/T), so the
    expected ratio is about M / (1 + (M - 1) / T) = 9.78 here; we accept >= 0.95 M."""
    m = 10
    for j in range(m):
        record(ledger, chr(ord("a") + j), noise(100 + j, bars(DAYS, "4h")))
    tc = trial_counts(ledger, cfg, loader=no_candles)
    assert tc.nominal == m and tc.n_unknown_pairs == 0
    assert 0.95 * m <= tc.effective <= m


def test_plan_with_only_an_error_trial_counts_as_independent(
    ledger: Ledger, cfg: LabConfig
) -> None:
    r = noise(4, bars(DAYS, "4h"))
    for name in "abc":
        record(ledger, name, r)
    record(ledger, "z", None)
    tc = trial_counts(ledger, cfg, loader=no_candles)
    assert tc.nominal == 4 and tc.n_with_returns == 3 and tc.n_without_returns == 1
    assert tc.effective == pytest.approx(2.0, abs=1e-9)
    assert "1 of 4 plans" in tc.basis


def test_holdout_trials_are_never_read(ledger: Ledger, cfg: LabConfig) -> None:
    r = noise(5, bars(DAYS, "4h"))
    design_ids = [record(ledger, name, r) for name in "abc"]
    before = trial_counts(ledger, cfg, loader=no_candles)
    holdout_start = int(cfg.splits.holdout_start.timestamp())
    wild = noise(6, bars(30, "4h"), scale=0.5)
    # newest trial of plan "a" is a holdout trial with wild returns; plan "h" only has one
    held = [
        record(ledger, "a", wild, start=holdout_start, kind="holdout", source="manual"),
        record(ledger, "h", wild, start=holdout_start, kind="holdout", source="manual"),
    ]
    read: list[int] = []
    original = ledger.trial_returns

    def spy(trial_id: int) -> np.ndarray | None:
        read.append(trial_id)
        return original(trial_id)

    ledger.trial_returns = spy  # type: ignore[method-assign]
    after = trial_counts(ledger, cfg, loader=no_candles)
    assert not set(read) & set(held) and set(read) == set(design_ids)
    # plan "h" exists (nominal counts every window) but its holdout returns are not used
    assert after.nominal == before.nominal + 1
    assert after.n_without_returns == 1
    assert after.effective == pytest.approx(before.effective + 1.0, abs=1e-9)


def test_a_custom_window_running_past_the_holdout_is_never_read(
    ledger: Ledger, cfg: LabConfig
) -> None:
    """The date guard (not only window_kind = holdout): a custom-window trial whose window ends
    after holdout_start is never read, even as a plan's newest trial; a plan with only such a
    trial counts as one independent plan without returns."""
    r = noise(12, bars(DAYS, "4h"))
    design_ids = [record(ledger, name, r) for name in "abc"]
    holdout_start = int(cfg.splits.holdout_start.timestamp())
    start = holdout_start - 20 * DAY  # 20 days before the holdout, 40 days into it
    wild = noise(13, bars(60, "4h"), scale=0.5)
    crossing = [
        record(ledger, "a", wild, start=start, kind="custom"),
        record(ledger, "x", wild, start=start, kind="custom"),
    ]
    read: list[int] = []
    original = ledger.trial_returns

    def spy(trial_id: int) -> np.ndarray | None:
        read.append(trial_id)
        return original(trial_id)

    ledger.trial_returns = spy  # type: ignore[method-assign]
    tc = trial_counts(ledger, cfg, loader=no_candles)
    assert not set(read) & set(crossing) and set(read) == set(design_ids)
    assert tc.nominal == 4 and tc.n_with_returns == 3 and tc.n_without_returns == 1
    assert tc.effective == pytest.approx(2.0, abs=1e-9)


def test_null_is_reported_beside_the_ratio(ledger: Ledger, cfg: LabConfig) -> None:
    """Three copies of one plan + two independent plans: the ratio is 25/11 (~2.3), the
    time-shuffled null of the same matrix about what 5 independent plans score over 400 days
    (5 / (1 + 4/399) = 4.95), plus the plan without returns (one each in both)."""
    shared = noise(14, bars(DAYS, "4h"))
    for name in "abc":
        record(ledger, name, shared)
    record(ledger, "x", noise(15, bars(DAYS, "4h")))
    record(ledger, "y", noise(16, bars(DAYS, "4h")))
    record(ledger, "z", None)
    tc = trial_counts(ledger, cfg, loader=no_candles)
    # the first day is incomplete (its first bar has no predecessor), so T = DAYS - 1
    assert (tc.n_with_returns, tc.rows, tc.nominal) == (5, DAYS - 1, 6)
    assert tc.effective == pytest.approx(25 / 11 + 1, abs=0.15)
    assert tc.effective_null == pytest.approx(5 / (1 + 4 / (DAYS - 1)) + 1, abs=0.1)
    assert tc.effective < tc.effective_null <= tc.nominal
    assert f"seed {TRIAL_COUNT_NULL_SEED}" in tc.basis


def test_with_nothing_to_correlate_the_null_equals_the_count(
    ledger: Ledger, cfg: LabConfig
) -> None:
    record(ledger, "a", None)
    record(ledger, "b", None)
    tc = trial_counts(ledger, cfg, loader=no_candles)
    assert (tc.effective, tc.effective_null, tc.rows) == (2.0, 2.0, 0)


def test_per_run_and_global_scopes(ledger: Ledger, cfg: LabConfig) -> None:
    shared = noise(7, bars(DAYS, "4h"))
    for name in "abc":
        record(ledger, name, shared, run_id="r1")
    record(ledger, "a", shared, run_id="r2")  # plan "a" again in another run: one plan
    record(ledger, "x", noise(8, bars(DAYS, "4h")), run_id="r2")
    record(ledger, "y", noise(9, bars(DAYS, "4h")), run_id="r2")
    r1 = trial_counts(ledger, cfg, run_id="r1", loader=no_candles)
    r2 = trial_counts(ledger, cfg, run_id="r2", loader=no_candles)
    everything = trial_counts(ledger, cfg, loader=no_candles)
    assert (r1.scope, r1.nominal) == ("r1", ledger.count_distinct_plans("r1")) == ("r1", 3)
    assert (r2.scope, r2.nominal) == ("r2", ledger.count_distinct_plans("r2")) == ("r2", 3)
    assert everything.scope == "all runs"
    assert everything.nominal == ledger.count_distinct_plans_research() == 5
    assert r1.effective == pytest.approx(1.0, abs=1e-9)
    assert r2.effective == pytest.approx(3.0, abs=0.15)  # three independent series
    # a block of 3 copies + 2 independent plans: eigenvalues (3, 1, 1, 0, 0), so the
    # participation ratio is 5^2 / (9 + 1 + 1) = 25/11 (unequal blocks do not count as 3)
    assert everything.effective == pytest.approx(25 / 11, abs=0.15)


def test_cache_is_reused_and_invalidated_by_a_new_trial(ledger: Ledger, cfg: LabConfig) -> None:
    r = noise(10, bars(DAYS, "4h"))
    record(ledger, "a", r)
    first = trial_counts(ledger, cfg, loader=no_candles)
    assert trial_counts(ledger, cfg, loader=no_candles) is first
    record(ledger, "b", noise(11, bars(DAYS, "4h")))
    second = trial_counts(ledger, cfg, loader=no_candles)
    assert second is not first and second.nominal == 2
    assert second.effective == pytest.approx(2.0, abs=0.1)


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_effective_never_exceeds_nominal(ledger: Ledger, cfg: LabConfig, seed: int) -> None:
    """Random mixes of copies, anti-correlated copies, independent plans, short windows (unknown
    pairs) and error trials, in two runs."""
    rng = np.random.default_rng(seed)
    base = noise(seed + 50, bars(DAYS, "4h"))
    for j in range(12):
        run = "r1" if j % 2 else "r2"
        kind = rng.integers(0, 5)
        name = f"p{j:02d}"
        if kind == 0:
            record(ledger, name, base + rng.normal(0, 0.002, base.size), run_id=run)
        elif kind == 1:
            record(ledger, name, -0.5 * base, run_id=run)
        elif kind == 2:
            record(ledger, name, noise(seed * 100 + j, bars(DAYS, "4h")), run_id=run)
        elif kind == 3:
            record(ledger, name, noise(seed * 100 + j, bars(40, "4h")), run_id=run)
        else:
            record(ledger, name, None, run_id=run)
    for run_id in (None, "r1", "r2"):
        tc = trial_counts(ledger, cfg, run_id=run_id, loader=no_candles)
        assert 0.0 < tc.effective <= tc.nominal + 1e-9
        assert tc.n_with_returns + tc.n_without_returns == tc.nominal


def test_pairs_with_too_little_overlap_count_as_independent(ledger: Ledger, cfg: LabConfig) -> None:
    """The same position on two windows overlapping fewer than 60 days: unknown, so 2."""
    r = noise(12, bars(200, "4h"))
    record(ledger, "a", r[: bars(100, "4h")])
    later = START + (100 - (TRIAL_COUNT_MIN_OVERLAP_DAYS - 10)) * DAY
    offset = bars(100 - (TRIAL_COUNT_MIN_OVERLAP_DAYS - 10), "4h")
    record(ledger, "b", r[offset : offset + bars(100, "4h")], start=later)
    tc = trial_counts(ledger, cfg, loader=no_candles)
    assert tc.n_unknown_pairs == 1
    assert tc.effective == pytest.approx(2.0, abs=1e-9)
    assert tc.min_overlap_days == TRIAL_COUNT_MIN_OVERLAP_DAYS
    assert tc.grid_seconds == TRIAL_COUNT_GRID_SECONDS


# --------------------------------------------------------------------------- bar times


def gapped_candles(interval: str) -> pd.DataFrame:
    """Seeded candles from 2018-07-01 with two missing bars (a maintenance gap)."""
    candles = make_candles(
        bars(DAYS + 10, interval), seed=13, interval=interval, start="2018-07-01"
    )
    return candles.drop(candles.index[[500, 501]])


def loader_of(frames: dict[tuple[str, str], pd.DataFrame]) -> Any:
    def load(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
        frame = frames[(symbol, interval)]
        return frame.loc[frame.index < end]

    return load


def test_gapped_series_is_rebuilt_from_the_candles_and_aligned_by_timestamp(
    ledger: Ledger, cfg: LabConfig
) -> None:
    """A 1h series with a gap (bar times from the candles) and the 4h series holding the same
    position (contiguous) are one trial; the days touched by the gap are simply left out."""
    candles = gapped_candles("1h")
    end = START + DAYS * DAY
    times = candles.index[(candles.index >= pd.Timestamp(START, unit="s", tz="UTC"))]
    times = times[times < pd.Timestamp(end, unit="s", tz="UTC")]
    r4 = noise(14, bars(DAYS, "4h"))
    r1 = split_4h_into_1h(r4, seed=15)
    full = pd.date_range(pd.Timestamp(START, unit="s", tz="UTC"), periods=r1.size, freq="1h")
    r1_gapped = r1[full.isin(times)]
    assert r1_gapped.size == r1.size - 2
    record(ledger, "a", r4, interval="4h")
    record(ledger, "b", r1_gapped, interval="1h", end=end)
    calls: list[tuple[str, str]] = []
    load = loader_of({("BTCUSDT", "1h"): candles})

    def counting(symbol: str, interval: str, end_ts: pd.Timestamp) -> pd.DataFrame:
        calls.append((symbol, interval))
        return load(symbol, interval, end_ts)

    tc = trial_counts(ledger, cfg, loader=counting)
    assert calls == [("BTCUSDT", "1h")]  # the contiguous 4h series needed no candles
    assert tc.n_with_returns == 2
    assert tc.effective == pytest.approx(1.0, abs=1e-6)


def test_series_whose_bar_times_cannot_be_rebuilt_counts_as_independent(
    ledger: Ledger, cfg: LabConfig
) -> None:
    candles = gapped_candles("1h")
    end = START + DAYS * DAY
    r = noise(16, bars(DAYS, "1h") - 2, scale=0.003)
    record(ledger, "a", r, interval="1h", end=end)  # bar times need the candles
    record(ledger, "b", r[:-5], interval="1h", end=end)  # length no longer matches them
    record(ledger, "c", r, interval="1h", end=end, symbol="ETHUSDT")  # no candles at all
    tc = trial_counts(ledger, cfg, loader=loader_of({("BTCUSDT", "1h"): candles}))
    assert tc.nominal == 3 and tc.n_with_returns == 1 and tc.n_without_returns == 2
    assert tc.effective == pytest.approx(3.0, abs=1e-9)


def test_portfolio_plans_on_contiguous_daily_rows_need_no_candles(
    ledger: Ledger, cfg: LabConfig
) -> None:
    """Portfolio rows are one per calendar day of the window, so a series filling every day of
    its window has those days as bar times; ``universe:USDT`` has no candles of its own."""
    r = noise(17, DAYS, scale=0.02)
    record(ledger, "a", r, symbol="universe:USDT", interval="1d")
    record(ledger, "b", r, symbol="universe:USDT", interval="1d")
    tc = trial_counts(ledger, cfg, loader=no_candles)
    assert tc.n_with_returns == 2 and tc.effective == pytest.approx(1.0, abs=1e-9)


def test_daily_grid_matches_family_to_grid() -> None:
    """The cached bucketing is ``family._to_grid`` on the daily grid, bit for bit."""
    step = interval_seconds("1h")
    ts = START + step * np.arange(bars(30, "1h"), dtype=np.int64)
    ts = np.delete(ts, [30, 31, 300])  # gaps on day 1 and day 12: those days are incomplete
    logret = np.log1p(noise(18, ts.size))
    want_days, want_sums = _to_grid(ts, logret, step, TRIAL_COUNT_GRID_SECONDS)
    days, sums = _DailyGrid.build(ts, step).apply(logret)
    np.testing.assert_array_equal(days, want_days)
    np.testing.assert_array_equal(sums, want_sums)
    assert days.size == 30 - 3  # day 0 (bar 0 has no predecessor) and the two gap days dropped
