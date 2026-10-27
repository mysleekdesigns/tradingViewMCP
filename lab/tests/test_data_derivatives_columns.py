"""lab.data.derivatives_columns: drv_* columns attached to candles at each bar's close, the
load window, gaps, and the plan-input preflight (PLAN.md Phase 12)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lab.data.derivatives import FUNDING_COLUMNS, KLINE_COLUMNS, METRICS_COLUMNS, load_series
from lab.data.derivatives_align import align_dataset
from lab.data.derivatives_columns import (
    COLUMN_SOURCES,
    UNAVAILABLE_ATTR,
    DataUnavailableError,
    attach_derivatives,
    derivatives_cli_hint,
    load_margin,
    plan_derivative_columns,
    require_plan_inputs,
)
from lab.plan import parse_plan
from lab.registry.derivatives import (
    DERIVATIVES_COLUMNS,
    DRV_FUNDING_RATE,
    DRV_INDEX_CLOSE,
    DRV_LS_RATIO,
    DRV_MARK_CLOSE,
    DRV_OI,
    DRV_PREMIUM_CLOSE,
)
from lab.runner import DataUnavailableError as RunnerDataUnavailableError
from lab.stats.permutation import permute_candles
from tests.synthetic import make_candles

STALE = 2.0
H4 = 4 * 3600
CANDLE_START = "2024-02-01"
N_BARS = 120  # 20 days of 4h bars
# derivatives cover the candles with a margin either side (rows after the last close exist)
DRV_START = pd.Timestamp("2024-01-25", tz="UTC")
DRV_END = pd.Timestamp("2024-03-01", tz="UTC")
FUNDING_GAP = (pd.Timestamp("2024-02-08", tz="UTC"), pd.Timestamp("2024-02-10", tz="UTC"))

EMA_PLAN = {
    "name": "ema_cross",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"ema_fast": {"ema": {"period": 20}}, "ema_slow": {"ema": {"period": 50}}},
    "long_entry": "crosses_above(ema_fast, ema_slow)",
    "long_exit": "crosses_below(ema_fast, ema_slow)",
    "risk": {"position_size_pct": 100},
}


def drv_plan(**features: dict) -> dict:
    """EMA cross gated by the given derivatives features (alias -> {feature: params})."""
    gates = " and ".join(f"{alias} < 1000" for alias in features)
    return {
        **EMA_PLAN,
        "name": "ema_gated",
        "features": {**EMA_PLAN["features"], **features},
        "long_entry": f"crosses_above(ema_fast, ema_slow) and {gates}",
    }


# --------------------------------------------------------------------------- archive writers


def _write(frame: pd.DataFrame, folder: Path, label_fmt: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for label, part in frame.groupby(frame.index.strftime(label_fmt)):
        part.to_parquet(folder / f"{label}.parquet", engine="pyarrow")


def write_funding(root: Path, symbol: str = "BTCUSDT", *, seed: int = 1) -> pd.DataFrame:
    """8h settlements, a few ms late like the real archive, with a 2-day archive hole."""
    times = pd.date_range(DRV_START, DRV_END, freq="8h", tz="UTC", inclusive="left")
    times = times + pd.Timedelta(milliseconds=2)
    times = times[(times < FUNDING_GAP[0]) | (times >= FUNDING_GAP[1])]
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {
            FUNDING_COLUMNS[0]: rng.normal(1e-4, 5e-5, len(times)),
            FUNDING_COLUMNS[1]: np.full(len(times), 8.0),
        },
        index=pd.DatetimeIndex(times, name="time"),
    )
    _write(frame, root / "fundingRate" / symbol, "%Y-%m")
    return frame


def write_klines(
    root: Path, dataset: str, interval: str, symbol: str = "BTCUSDT", *, seed: int = 2
) -> pd.DataFrame:
    step = pd.Timedelta(interval)
    opens = pd.date_range(DRV_START, DRV_END, freq=step, tz="UTC", inclusive="left")
    rng = np.random.default_rng(seed)
    close = 40_000 * np.exp(np.cumsum(rng.normal(0, 0.01, len(opens))))
    frame = pd.DataFrame(
        {c: close * (1 + 0.001 * i) for i, c in enumerate(KLINE_COLUMNS)},
        index=pd.DatetimeIndex(opens, name="time"),
    )
    frame["close"] = close
    frame["close_time"] = opens + step - pd.Timedelta(milliseconds=1)
    _write(frame, root / dataset / symbol / interval, "%Y-%m")
    return frame


def write_metrics(root: Path, symbol: str = "BTCUSDT", *, seed: int = 3) -> pd.DataFrame:
    times = pd.date_range(DRV_START, DRV_END, freq="5min", tz="UTC", inclusive="left")
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {c: rng.uniform(1.0, 2.0, len(times)) for c in METRICS_COLUMNS},
        index=pd.DatetimeIndex(times, name="time"),
    )
    _write(frame, root / "metrics" / symbol, "%Y-%m-%d")
    return frame


def write_all(root: Path) -> None:
    write_funding(root)
    for i, dataset in enumerate(("premiumIndexKlines", "markPriceKlines", "indexPriceKlines")):
        write_klines(root, dataset, "4h", seed=10 + i)
    write_metrics(root)


def candles() -> pd.DataFrame:
    return make_candles(N_BARS, seed=4, start=CANDLE_START, price=40_000.0)


def attach(c: pd.DataFrame, root: Path, interval: str = "4h") -> pd.DataFrame:
    return attach_derivatives(c, "BTCUSDT", interval, data_dir=root, stale_after_intervals=STALE)


def expected_column(root: Path, column: str, bars: pd.DatetimeIndex, interval: str) -> np.ndarray:
    """``align_dataset`` on the WHOLE series on disk (no load window) for one column."""
    dataset, value = COLUMN_SOURCES[column]
    iv = interval if dataset.endswith("Klines") else None
    frame = load_series(dataset, "BTCUSDT", data_dir=root, interval=iv)
    bar_s = int(pd.Timedelta(interval).total_seconds())
    aligned = align_dataset(dataset, frame, bars, bar_s, stale_after_intervals=STALE)
    return aligned[value].to_numpy()


# --------------------------------------------------------------------------- attach


def test_attach_equals_align_dataset_on_the_whole_series_per_column(tmp_path: Path) -> None:
    write_all(tmp_path)
    c = candles()
    out = attach(c, tmp_path)
    assert [x for x in out.columns if x.startswith("drv_")] == list(DERIVATIVES_COLUMNS)
    for column in DERIVATIVES_COLUMNS:
        want = expected_column(tmp_path, column, c.index, "4h")
        np.testing.assert_array_equal(out[column].to_numpy(), want, err_msg=column)
        assert out[column].dtype == np.float64
        assert np.isfinite(out[column].to_numpy()).sum() > N_BARS // 2, column


def test_original_columns_are_untouched_and_the_input_is_not_mutated(tmp_path: Path) -> None:
    write_all(tmp_path)
    c = candles()
    before = c.copy()
    out = attach(c, tmp_path)
    pd.testing.assert_frame_equal(c, before)
    pd.testing.assert_frame_equal(out[list(c.columns)], before)
    assert UNAVAILABLE_ATTR not in out.attrs


def test_attached_values_hand_checked_at_one_bar(tmp_path: Path) -> None:
    funding = write_funding(tmp_path)
    c = candles()
    out = attach(c, tmp_path)
    # bar opening 2024-02-01 08:00 closes 12:00; the 08:00:00.002 settlement is the last
    # visible one (the 16:00 one is not), so that bar carries it.
    bar = pd.Timestamp("2024-02-01 08:00", tz="UTC")
    settle = pd.Timestamp("2024-02-01 08:00:00.002", tz="UTC")
    assert out.loc[bar, DRV_FUNDING_RATE] == funding.loc[settle, "funding_rate"]
    # the bar closing exactly at 08:00 does not see the 08:00:00.002 settlement (strict close)
    prev = pd.Timestamp("2024-02-01 04:00", tz="UTC")
    assert (
        out.loc[prev, DRV_FUNDING_RATE]
        == funding.loc[pd.Timestamp("2024-02-01 00:00:00.002", tz="UTC"), "funding_rate"]
    )


def test_wrong_kline_interval_leaves_the_column_absent(tmp_path: Path) -> None:
    write_klines(tmp_path, "premiumIndexKlines", "1h")
    out4h = attach(candles(), tmp_path, "4h")
    assert not any(x.startswith("drv_") for x in out4h.columns)
    c1h = make_candles(200, seed=5, interval="1h", start=CANDLE_START)
    out1h = attach(c1h, tmp_path, "1h")
    assert [x for x in out1h.columns if x.startswith("drv_")] == [DRV_PREMIUM_CLOSE]


def test_no_series_on_disk_returns_an_unchanged_copy(tmp_path: Path) -> None:
    c = candles()
    out = attach(c, tmp_path / "nothing")
    pd.testing.assert_frame_equal(out, c)
    assert out is not c


def test_only_series_on_disk_are_attached(tmp_path: Path) -> None:
    write_metrics(tmp_path)
    out = attach(candles(), tmp_path)
    assert [x for x in out.columns if x.startswith("drv_")] == [DRV_OI, DRV_LS_RATIO]


def test_rows_after_the_last_bar_close_are_never_used(tmp_path: Path) -> None:
    """Perturb every archive row stamped after the last bar's close: output is identical."""
    write_all(tmp_path)
    c = candles()
    before = attach(c, tmp_path)
    last_close = c.index[-1] + pd.Timedelta(seconds=H4)
    rng = np.random.default_rng(99)
    for path in tmp_path.rglob("*.parquet"):
        frame = pd.read_parquet(path)
        late = frame.index > last_close
        if not late.any():
            continue
        for col in frame.columns:
            if col != "close_time":
                frame.loc[late, col] = rng.normal(0, 1e6, int(late.sum()))
        frame.to_parquet(path, engine="pyarrow")
    after = attach(c, tmp_path)
    pd.testing.assert_frame_equal(after, before)


def test_rows_after_the_last_bar_close_are_not_even_loaded(tmp_path: Path) -> None:
    """Kline rows stamped after the last close lose their close_time (alignment refuses such
    rows outright): the column is still attached, so those rows were never handed to it."""
    write_klines(tmp_path, "markPriceKlines", "4h")
    c = candles()
    before = attach(c, tmp_path)
    last_close = c.index[-1] + pd.Timedelta(seconds=H4)
    for path in (tmp_path / "markPriceKlines").rglob("*.parquet"):
        frame = pd.read_parquet(path)
        frame.loc[frame.index > last_close, "close_time"] = pd.NaT
        frame.to_parquet(path, engine="pyarrow")
    after = attach(c, tmp_path)
    assert UNAVAILABLE_ATTR not in after.attrs
    pd.testing.assert_frame_equal(after, before)
    later = make_candles(N_BARS + 6, seed=4, start=CANDLE_START, price=40_000.0)
    assert DRV_MARK_CLOSE not in attach(later, tmp_path).columns  # those rows now in the window


def test_a_prefix_of_the_bars_sees_exactly_the_same_values(tmp_path: Path) -> None:
    """No look-ahead through the load window: attaching to the first k bars gives the first k
    rows of attaching to all bars."""
    write_all(tmp_path)
    c = candles()
    full = attach(c, tmp_path)
    for k in (1, 7, 60):
        part = attach(c.iloc[:k], tmp_path)
        pd.testing.assert_frame_equal(part, full.iloc[:k])


def test_load_margin_covers_the_first_bar(tmp_path: Path) -> None:
    """The first bar sees the same value as with the whole series loaded, even when it falls
    right after a data start long before it."""
    write_all(tmp_path)
    c = make_candles(30, seed=6, start="2024-02-20", price=40_000.0)
    out = attach(c, tmp_path)
    for column in DERIVATIVES_COLUMNS:
        want = expected_column(tmp_path, column, c.index, "4h")
        np.testing.assert_array_equal(out[column].to_numpy(), want, err_msg=column)
    assert load_margin("4h", STALE) >= pd.Timedelta(days=2)
    assert load_margin("1d", STALE) >= pd.Timedelta(days=2)


def test_archive_gaps_stay_nan_never_forward_filled(tmp_path: Path) -> None:
    write_funding(tmp_path)
    out = attach(candles(), tmp_path)
    rate = out[DRV_FUNDING_RATE]
    # 8h settlements, 16h staleness: bars closing more than 16h after the last settlement
    # before the hole are NaN until the first settlement after it.
    late = pd.Timedelta(milliseconds=2)
    last_before = FUNDING_GAP[0] - pd.Timedelta(hours=8) + late
    first_after = FUNDING_GAP[1] + late  # a bar closing at 00:00 does not see 00:00.002
    closes = rate.index + pd.Timedelta(seconds=H4)
    stale = (closes - last_before > pd.Timedelta(hours=16)) & (closes < first_after)
    assert stale.sum() >= 6
    assert rate[stale].isna().all()
    assert rate[~stale].notna().all()


def test_unalignable_series_is_absent_with_its_reason(tmp_path: Path) -> None:
    frame = write_klines(tmp_path, "markPriceKlines", "4h")
    old = frame.drop(columns=["close_time"])  # Parquet written before derivatives schema 2
    folder = tmp_path / "markPriceKlines" / "BTCUSDT" / "4h"
    for path in folder.glob("*.parquet"):
        path.unlink()
    _write(old, folder, "%Y-%m")
    out = attach(candles(), tmp_path)
    assert DRV_MARK_CLOSE not in out.columns
    assert "close_time" in out.attrs[UNAVAILABLE_ATTR][DRV_MARK_CLOSE]
    plan = drv_plan(basis={"basis_pct": {}})
    with pytest.raises(DataUnavailableError, match="--refresh"):
        require_plan_inputs(plan, out, symbol="BTCUSDT", interval="4h")


def test_permutation_null_keeps_the_real_derivatives_timeline(tmp_path: Path) -> None:
    """permute_candles shuffles OHLC only: the null pairs a permuted price path with the REAL
    derivatives series (documented in lab.data.derivatives_columns)."""
    write_all(tmp_path)
    out = attach(candles(), tmp_path)
    perm = permute_candles(out, seed=3)
    assert not np.array_equal(perm["close"].to_numpy(), out["close"].to_numpy())
    pd.testing.assert_frame_equal(perm[list(DERIVATIVES_COLUMNS)], out[list(DERIVATIVES_COLUMNS)])


# --------------------------------------------------------------------------- plan inputs


def test_plan_derivative_columns_reads_the_feature_names() -> None:
    plan = drv_plan(fz={"funding_zscore": {"period": 30}}, basis={"basis_pct": {}})
    assert plan_derivative_columns(plan) == (DRV_FUNDING_RATE, DRV_MARK_CLOSE, DRV_INDEX_CLOSE)
    assert plan_derivative_columns(json.dumps(plan)) == plan_derivative_columns(plan)
    assert plan_derivative_columns(parse_plan(plan)) == plan_derivative_columns(plan)
    oi = drv_plan(oi={"oi_change": {}}, ls={"long_short_ratio": {}}, pi={"premium_index": {}})
    assert plan_derivative_columns(oi) == (DRV_PREMIUM_CLOSE, DRV_OI, DRV_LS_RATIO)


def test_plans_without_derivatives_need_nothing() -> None:
    assert plan_derivative_columns(EMA_PLAN) == ()
    assert plan_derivative_columns({"kind": "portfolio", "features": {}}) == ()
    assert plan_derivative_columns("not json") == ()
    require_plan_inputs(EMA_PLAN, candles(), symbol="BTCUSDT", interval="4h")  # no raise


def test_missing_column_names_it_and_the_exact_cli() -> None:
    plan = drv_plan(fz={"funding_zscore": {}}, basis={"basis_pct": {}})
    c = candles()
    with pytest.raises(DataUnavailableError) as info:
        require_plan_inputs(plan, c, symbol="BTCUSDT", interval="4h")
    msg = str(info.value)
    for column in (DRV_FUNDING_RATE, DRV_MARK_CLOSE, DRV_INDEX_CLOSE):
        assert column in msg
    assert (
        "`cd lab && uv run python -m lab.data.derivatives download BTCUSDT --datasets "
        "fundingRate,markPriceKlines,indexPriceKlines --interval 4h --start 2024-02 "
        "--end 2024-02`"
    ) in msg
    assert isinstance(info.value, RunnerDataUnavailableError)  # one class, runner re-exports


def test_cli_hint_adds_interval_only_for_klines() -> None:
    assert derivatives_cli_hint("ETHUSDT", "1h", [DRV_OI, DRV_LS_RATIO]) == (
        "cd lab && uv run python -m lab.data.derivatives download ETHUSDT --datasets metrics "
        "--start YYYY-MM --end YYYY-MM"
    )
    assert "--interval 1h" in derivatives_cli_hint("ETHUSDT", "1h", [DRV_PREMIUM_CLOSE])


def test_all_nan_column_in_the_frame_is_unavailable() -> None:
    plan = drv_plan(oi={"oi_change": {}})
    c = candles()
    c[DRV_OI] = np.nan
    with pytest.raises(DataUnavailableError, match="drv_oi has no value") as info:
        require_plan_inputs(plan, c, symbol="BTCUSDT", interval="4h")
    assert "--datasets metrics --start" in str(info.value)
    assert "--interval" not in str(info.value)
    c.iloc[-1, c.columns.get_loc(DRV_OI)] = 1.5
    require_plan_inputs(plan, c, symbol="BTCUSDT", interval="4h")  # one value is enough


def test_present_columns_pass(tmp_path: Path) -> None:
    write_all(tmp_path)
    out = attach(candles(), tmp_path)
    plan = drv_plan(fz={"funding_zscore": {}}, basis={"basis_pct": {}}, oi={"oi_change": {}})
    require_plan_inputs(plan, out, symbol="BTCUSDT", interval="4h")
