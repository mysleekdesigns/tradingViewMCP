"""POST /lab/regime with model "hmm": the chart's HMM ribbon, offline, with a fake fit.

``hmm_series`` is patched: these tests pin the API's contract (body rules, archive prefix, one
point per posted bar, JSON safety). The causal fit has its own tests beside its code.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lab.api import HMM_ENGINE, create_app, latest_bear_is_high_vol
from lab.config import LabConfig, load_config
from lab.ledger import Ledger
from lab.types import HmmSettings

SETTINGS = HmmSettings(
    n_states=2,
    vol_window_bars=4,
    train_bars=20,
    refit_every_bars=5,
    seed=7,
    restarts=2,
    n_iter=10,
)
BARS_NEEDED = 25  # train_bars + vol_window_bars + 1
H4 = 14_400
T0 = 1_700_000_000 - 1_700_000_000 % H4


# Rows before the posted bars in the default archive: every request has a fixed first row.
ARCHIVE_BARS = 40


@dataclass(frozen=True)
class FakeSeries:
    state: np.ndarray
    bear_prob: np.ndarray
    state_prob: np.ndarray
    warmup_bars: int
    bear_high_vol: np.ndarray = field(default_factory=lambda: np.empty(0))


class FakeBusyError(RuntimeError):
    """Stands in for ``lab.data.hmm_columns.HmmBusyError``."""


@dataclass(frozen=True)
class FitCall:
    close: pd.Series
    settings: HmmSettings
    bar_seconds: int
    cache_dir: Path | None
    wait_seconds: float | None
    observed: np.ndarray | None


class FakeFit:
    """Stands in for ``hmm_series``; remembers every call and answers with ``answer(close)``."""

    def __init__(self) -> None:
        self.calls: list[FitCall] = []
        self.answer: Callable[[pd.Series], FakeSeries] = self.default

    @staticmethod
    def default(close: pd.Series) -> FakeSeries:
        n = len(close)
        warmup = 10
        state = (np.arange(n) % 2).astype(np.float64)
        prob = 0.5 + 0.4 * (np.arange(n) % 3) / 2
        state[:warmup] = np.nan
        prob[:warmup] = np.nan
        return FakeSeries(state, np.full(n, np.nan), prob, warmup)

    def __call__(
        self,
        close: pd.Series,
        settings: HmmSettings,
        *,
        bar_seconds: int,
        cache_dir: Path | None,
        wait_seconds: float | None = None,
        observed: np.ndarray | None = None,
    ) -> FakeSeries:
        self.calls.append(
            FitCall(close.copy(), settings, bar_seconds, cache_dir, wait_seconds, observed)
        )
        return self.answer(close)


@pytest.fixture
def fit(monkeypatch: pytest.MonkeyPatch) -> FakeFit:
    # The handler imports these lazily, so a fake module is enough (and the real one is never
    # loaded here).
    fake = FakeFit()
    module = types.ModuleType("lab.data.hmm_columns")
    module.hmm_series = fake  # type: ignore[attr-defined]
    module.HmmBusyError = FakeBusyError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lab.data.hmm_columns", module)
    return fake


def config(tmp_path: Path, hmm: HmmSettings | None = SETTINGS) -> LabConfig:
    return dataclasses.replace(load_config(), data_dir=tmp_path / "data", hmm=hmm)


def make_client(cfg: LabConfig, tmp_path: Path) -> TestClient:
    app = create_app(cfg, ledger_path=tmp_path / "ledger.db", warm_up=False)
    return TestClient(app, base_url="http://127.0.0.1:8788")


def write_archive(
    cfg: LabConfig,
    closes: pd.Series,
    *,
    symbol: str = "BTCUSDT",
    interval: str = "4h",
    volume: float | list[float] = 1.0,
) -> None:
    folder = cfg.parquet_dir / symbol / interval
    folder.mkdir(parents=True)
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes,
            "volume": volume,
            "tradable": True,
        },
        index=closes.index,
    )
    frame.to_parquet(folder / "2023-11.parquet", engine="pyarrow", index=True)


def archive_closes(n: int, end: int, step: int = H4) -> pd.Series:
    """``n`` bars ending one bar before ``end`` (UNIX seconds), closes 500, 501, ..."""
    index = pd.to_datetime([end - (n - i) * step for i in range(n)], unit="s", utc=True)
    return pd.Series([500.0 + i for i in range(n)], index=index, name="close")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(ARCHIVE_BARS, T0))
    with make_client(cfg, tmp_path) as c:
        yield c


def bars(n: int, start: int = T0, step: int = H4) -> tuple[list[int], list[float]]:
    times = [start + i * step for i in range(n)]
    closes = [100.0 + i for i in range(n)]
    return times, closes


def hmm_body(n: int = 30, **over: Any) -> dict[str, Any]:
    times, closes = bars(n)
    return {
        "times": times,
        "closes": closes,
        "model": "hmm",
        "symbol": "BTCUSDT",
        "interval": "4h",
        **over,
    }


def post(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post("/lab/regime", json=body)


def errors_of(r: Any) -> dict[str, str]:
    assert r.status_code == 422, r.text
    return {e["path"]: e["message"] for e in r.json()["errors"]}


def assert_not_fitted(body: dict[str, Any], fit: FakeFit, *names: str) -> None:
    """A 200 with every point null, the reason naming ``names``, and no fit."""
    assert fit.calls == []
    assert all(p["state"] is None and p["prob"] is None for p in body["points"])
    for name in names:
        assert name in body["unavailable"]


# --------------------------------------------------------------------------- body rules


@pytest.mark.parametrize("missing", ["symbol", "interval"])
def test_hmm_needs_symbol_and_interval(client: TestClient, fit: FakeFit, missing: str) -> None:
    body = hmm_body()
    del body[missing]
    assert missing in errors_of(post(client, body))
    assert fit.calls == []


def test_a_missing_symbol_and_interval_are_both_named(client: TestClient, fit: FakeFit) -> None:
    body = hmm_body()
    del body["symbol"], body["interval"]
    assert set(errors_of(post(client, body))) == {"symbol", "interval"}


@pytest.mark.parametrize("interval", ["1m", "5m", "30m"])
def test_an_interval_below_1h_is_refused_naming_the_minimum(
    client: TestClient, fit: FakeFit, interval: str
) -> None:
    errors = errors_of(post(client, hmm_body(interval=interval)))
    assert "1h" in errors["interval"]
    assert fit.calls == []


@pytest.mark.parametrize(("interval", "step"), [("1h", 3_600), ("4h", H4), ("1d", 86_400)])
def test_intervals_from_1h_up_are_fitted(
    tmp_path: Path, fit: FakeFit, interval: str, step: int
) -> None:
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(ARCHIVE_BARS, T0, step), interval=interval)
    times, closes = bars(30, step=step)
    with make_client(cfg, tmp_path) as c:
        r = post(c, hmm_body(times=times, closes=closes, interval=interval))
    assert r.status_code == 200
    assert fit.calls[0].bar_seconds == step


def test_an_unknown_interval_is_a_field_error(client: TestClient, fit: FakeFit) -> None:
    assert "interval" in errors_of(post(client, hmm_body(interval="3d")))


@pytest.mark.parametrize("symbol", ["../../etc", "btcusdt", "A", "BTC/USDT", "BTC USDT\n"])
def test_a_symbol_that_is_no_chart_symbol_never_reaches_the_disk(
    client: TestClient, fit: FakeFit, symbol: str
) -> None:
    assert "symbol" in errors_of(post(client, hmm_body(symbol=symbol)))


def test_times_outside_the_unix_range_are_a_field_error(client: TestClient, fit: FakeFit) -> None:
    body = hmm_body()
    body["times"] = [10**13 + i for i in range(len(body["closes"]))]
    assert "times" in errors_of(post(client, body))


def test_an_unknown_model_is_refused(client: TestClient) -> None:
    assert "model" in errors_of(post(client, hmm_body(model="garch")))


def test_503_with_a_plain_reason_when_no_hmm_settings(tmp_path: Path, fit: FakeFit) -> None:
    with make_client(config(tmp_path, hmm=None), tmp_path) as c:
        r = post(c, hmm_body())
    assert r.status_code == 503
    assert "regime.hmm" in r.json()["detail"]
    assert fit.calls == []


# --------------------------------------------------------------------------- a fit that fails


def test_a_failed_fit_answers_503_with_the_reason(client: TestClient, fit: FakeFit) -> None:
    # hedgeFund's fit_hmm_features raises RuntimeError when every Baum-Welch restart fails.
    def boom(_: pd.Series) -> FakeSeries:
        raise RuntimeError("every Baum-Welch restart failed for seeds (7, 8)")

    fit.answer = boom
    r = post(client, hmm_body())
    assert r.status_code == 503
    assert "every Baum-Welch restart failed" in r.json()["detail"]
    assert "busy" not in r.json()


def test_a_bad_input_to_the_fit_answers_503_with_the_reason(
    client: TestClient, fit: FakeFit
) -> None:
    def boom(_: pd.Series) -> FakeSeries:
        raise ValueError("bar_seconds must be positive")

    fit.answer = boom
    r = post(client, hmm_body())
    assert r.status_code == 503
    assert "bar_seconds" in r.json()["detail"]


def test_a_running_fit_answers_503_marked_busy(client: TestClient, fit: FakeFit) -> None:
    def busy(_: pd.Series) -> FakeSeries:
        raise FakeBusyError("lock held")

    fit.answer = busy
    r = post(client, hmm_body())
    assert r.status_code == 503
    assert r.json() == {"detail": "another HMM fit is running", "busy": True}


def test_the_handler_waits_two_seconds_for_a_running_fit(client: TestClient, fit: FakeFit) -> None:
    assert post(client, hmm_body()).status_code == 200
    assert fit.calls[0].wait_seconds == 2.0


# --------------------------------------------------------------------------- response shape


def test_response_shape_settings_and_cache_dir(tmp_path: Path, fit: FakeFit) -> None:
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(ARCHIVE_BARS, T0))
    with make_client(cfg, tmp_path) as c:
        r = post(c, hmm_body(30))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {k: v for k, v in body.items() if k != "points"} == {
        "model": "hmm",
        "engine": HMM_ENGINE,
        "n_states": 2,
        "warmup_bars": 10,
        "bars_needed": BARS_NEEDED,
        "bars_have": ARCHIVE_BARS + 30,
        "bear_is_high_vol": None,
    }
    assert HMM_ENGINE == "lab.registry.hmm.causal_hmm"
    call = fit.calls[0]
    assert call.settings == SETTINGS
    assert call.bar_seconds == H4
    assert call.cache_dir == cfg.hmm_cache_dir


@pytest.mark.parametrize(
    ("flags", "want"),
    [
        ([1.0, 0.0], False),  # the newest decided bar's fit, not an average
        ([0.0, 1.0], True),
        ([0.0, 1.0, np.nan], True),  # a gap is skipped, not read as an answer
        ([np.nan, np.nan], None),
        ([], None),
    ],
)
def test_the_newest_decided_bar_says_whether_bear_is_the_volatile_state(
    flags: list[float], want: bool | None
) -> None:
    assert latest_bear_is_high_vol(np.asarray(flags, dtype=float)) is want


def test_the_answer_only_reads_the_posted_bars_label_meaning(tmp_path: Path, fit: FakeFit) -> None:
    def answer(close: pd.Series) -> FakeSeries:
        out = FakeFit.default(close)
        flags = np.full(len(close), np.nan)
        flags[: len(close) - 30] = 1.0  # decided in the archive prefix, nothing on the chart
        return replace(out, bear_high_vol=flags)

    fit.answer = answer
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(ARCHIVE_BARS, T0))
    with make_client(cfg, tmp_path) as c:
        assert post(c, hmm_body(30)).json()["bear_is_high_vol"] is None


def test_warm_up_bars_are_null_and_decided_bars_carry_the_state_prob(
    client: TestClient, fit: FakeFit
) -> None:
    def warm_up_inside_the_posted_bars(close: pd.Series) -> FakeSeries:
        out = FakeFit.default(close)
        warmup = ARCHIVE_BARS + 10
        state, prob = out.state.copy(), out.state_prob.copy()
        state[:warmup] = np.nan
        prob[:warmup] = np.nan
        return FakeSeries(state, out.bear_prob, prob, warmup)

    fit.answer = warm_up_inside_the_posted_bars
    points = post(client, hmm_body(30)).json()["points"]
    assert len(points) == 30
    assert points[9] == {"time": T0 + 9 * H4, "state": None, "prob": None}
    # Fake: id alternates 0, 1 by fitted row; prob 0.5, 0.7, 0.9 by fitted row modulo 3.
    assert points[10] == {"time": T0 + 10 * H4, "state": -1, "prob": pytest.approx(0.9)}
    assert points[11] == {"time": T0 + 11 * H4, "state": 1, "prob": pytest.approx(0.5)}
    assert points[12] == {"time": T0 + 12 * H4, "state": -1, "prob": pytest.approx(0.7)}


def test_three_states_map_lowest_to_bear_highest_to_bull_middle_to_sideways(
    tmp_path: Path, fit: FakeFit
) -> None:
    fit.answer = lambda close: FakeSeries(
        (np.arange(len(close)) % 3).astype(np.float64),
        np.zeros(len(close)),
        np.full(len(close), 0.6),
        0,
    )
    cfg = config(tmp_path, hmm=dataclasses.replace(SETTINGS, n_states=3))
    write_archive(cfg, archive_closes(39, T0))  # row 39 is the first posted bar: id 0
    with make_client(cfg, tmp_path) as c:
        body = post(c, hmm_body(30)).json()
    assert body["n_states"] == 3
    assert [p["state"] for p in body["points"][:3]] == [-1, 0, 1]


def test_non_finite_values_never_reach_the_json(client: TestClient, fit: FakeFit) -> None:
    def odd(close: pd.Series) -> FakeSeries:
        n = len(close)
        state = np.ones(n)
        prob = np.full(n, 0.8)
        at = ARCHIVE_BARS
        state[at] = np.nan
        prob[at + 1] = np.nan
        prob[at + 2] = np.inf
        state[at + 3] = -np.inf
        return FakeSeries(state, np.full(n, np.nan), prob, 0)

    fit.answer = odd
    r = post(client, hmm_body(30))
    assert r.status_code == 200

    def refuse(token: str) -> None:
        raise AssertionError(f"non-finite {token} in JSON")

    points = json.loads(r.text, parse_constant=refuse)["points"]
    assert [p["state"] for p in points[:5]] == [None, None, None, None, 1]
    assert [p["prob"] for p in points[:5]] == [None, None, None, None, 0.8]


# --------------------------------------------------------------------------- archive prefix


def test_the_archive_prefix_is_fitted_but_not_returned(client: TestClient, fit: FakeFit) -> None:
    body = post(client, hmm_body(12)).json()
    close = fit.calls[0].close
    assert len(close) == ARCHIVE_BARS + 12
    assert close.index.is_monotonic_increasing and close.index.is_unique
    assert close.iloc[0] == 500.0 and close.iloc[ARCHIVE_BARS - 1] == 539.0
    assert close.iloc[ARCHIVE_BARS:].tolist() == [100.0 + i for i in range(12)]
    assert body["bars_have"] == ARCHIVE_BARS + 12
    assert [p["time"] for p in body["points"]] == [T0 + i * H4 for i in range(12)]
    # Row 40 is the first posted bar: the fake's warm-up (10 rows) ended inside the prefix.
    assert body["points"][0]["state"] == -1
    assert body["points"][0]["prob"] == pytest.approx(0.7)
    assert "unavailable" not in body


def test_only_archive_rows_strictly_before_the_first_posted_bar_are_used(
    tmp_path: Path, fit: FakeFit
) -> None:
    # The archive runs on past the posted bars: nothing at or after the first one may enter,
    # and neither may any archive row after the last posted bar (replay's cursor).
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(60, T0 + 30 * H4))
    with make_client(cfg, tmp_path) as c:
        body = post(c, hmm_body(5)).json()
    close = fit.calls[0].close
    assert len(close) == 35  # 30 archive rows before T0, 5 posted
    assert close.index[-1] == pd.Timestamp(T0 + 4 * H4, unit="s", tz="UTC")
    assert close.iloc[30:].tolist() == [100.0 + i for i in range(5)]
    assert body["bars_have"] == 35


def test_archive_rows_without_trades_are_not_observed_and_posted_rows_are(
    tmp_path: Path, fit: FakeFit
) -> None:
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(4, T0), volume=[1.0, 0.0, 2.5, 0.0])
    with make_client(cfg, tmp_path) as c:
        post(c, hmm_body(3))
    observed = fit.calls[0].observed
    assert observed is not None
    assert observed.dtype == np.bool_
    assert observed.tolist() == [True, False, True, False, True, True, True]


# --------------------------------------------------------------------------- a fixed first row


def test_paging_left_leaves_labels_on_shared_bars_identical(tmp_path: Path, fit: FakeFit) -> None:
    # The fake refits on a schedule counted from the first row it gets, like the real one: if a
    # moved start reached the fit, every label after it would shift.
    def counts_rows_from_the_first(close: pd.Series) -> FakeSeries:
        n = len(close)
        rows = np.arange(n)
        return FakeSeries(
            ((rows // 7) % 2).astype(np.float64), np.zeros(n), 0.5 + (rows % 5) / 10, 0
        )

    fit.answer = counts_rows_from_the_first
    cfg = config(tmp_path)
    archive = archive_closes(120, T0 + 50 * H4)  # overlaps the chart: rows up to T0 + 49 bars
    write_archive(cfg, archive)
    start = T0 + 10 * H4

    def posted_from(first: int, last_exclusive: int) -> dict[str, Any]:
        times = [first + i * H4 for i in range((last_exclusive - first) // H4)]
        stamps = pd.to_datetime(times, unit="s", utc=True)
        closes = [float(archive.get(t, 700.0 + i)) for i, t in enumerate(stamps)]
        return hmm_body(times=times, closes=closes)

    end = T0 + 60 * H4  # runs past the archive's end: the newest bars are the chart's own
    with make_client(cfg, tmp_path) as c:
        wide = post(c, posted_from(start, end)).json()
        paged = post(c, posted_from(start - 15 * H4, end)).json()

    assert wide["bars_have"] == paged["bars_have"]
    shared = {p["time"]: p for p in paged["points"]}
    assert len(wide["points"]) == 50
    assert all(p == shared[p["time"]] for p in wide["points"])
    assert len(paged["points"]) == 65


def test_a_gap_between_archive_and_chart_is_not_fitted(tmp_path: Path, fit: FakeFit) -> None:
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(40, T0 - 3 * H4))
    with make_client(cfg, tmp_path) as c:
        r = post(c, hmm_body(30))
    assert r.status_code == 200
    body = r.json()
    last = pd.Timestamp(T0 - 4 * H4, unit="s", tz="UTC")
    first = pd.Timestamp(T0, unit="s", tz="UTC")
    assert_not_fitted(body, fit, "BTCUSDT 4h", f"{last:%Y-%m-%d %H:%M}", f"{first:%Y-%m-%d %H:%M}")
    assert len(body["points"]) == 30
    assert body["bars_have"] == 30
    assert body["model"] == "hmm"


def test_an_archive_ending_exactly_one_bar_before_the_chart_is_contiguous(
    client: TestClient, fit: FakeFit
) -> None:
    assert "unavailable" not in post(client, hmm_body(30)).json()
    assert len(fit.calls) == 1


def test_a_market_gap_inside_an_overlapping_archive_is_not_a_join_gap(
    tmp_path: Path, fit: FakeFit
) -> None:
    # The archive holds the chart's first bar, with a hole before it: that hole is the market's
    # own and the same for every request, so the fit runs.
    cfg = config(tmp_path)
    closes = pd.concat([archive_closes(20, T0 - 5 * H4), archive_closes(3, T0 + 3 * H4)])
    write_archive(cfg, closes)
    with make_client(cfg, tmp_path) as c:
        body = post(c, hmm_body(30)).json()
    assert "unavailable" not in body
    assert body["bars_have"] == 50


def test_no_archive_is_not_fitted_and_names_what_is_missing(tmp_path: Path, fit: FakeFit) -> None:
    with make_client(config(tmp_path), tmp_path) as c:
        r = post(c, hmm_body(30))
    assert r.status_code == 200
    assert_not_fitted(r.json(), fit, "BTCUSDT 4h", "archive")


def test_an_archive_of_another_symbol_is_not_used(tmp_path: Path, fit: FakeFit) -> None:
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(40, T0), symbol="ETHUSDT")
    with make_client(cfg, tmp_path) as c:
        assert_not_fitted(post(c, hmm_body(30)).json(), fit, "BTCUSDT")


def test_a_folder_without_month_files_is_no_archive(tmp_path: Path, fit: FakeFit) -> None:
    cfg = config(tmp_path)
    (cfg.parquet_dir / "BTCUSDT" / "4h").mkdir(parents=True)
    with make_client(cfg, tmp_path) as c:
        r = post(c, hmm_body(30))
    assert r.status_code == 200
    assert_not_fitted(r.json(), fit, "archive")


def test_a_chart_that_starts_before_the_archive_is_not_fitted(tmp_path: Path, fit: FakeFit) -> None:
    # Its first row would be wherever the chart has paged to.
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(40, T0 + 60 * H4))
    with make_client(cfg, tmp_path) as c:
        r = post(c, hmm_body(5))
    assert r.status_code == 200
    assert_not_fitted(r.json(), fit, "starts after")


def test_a_chart_that_starts_on_the_archive_first_bar_is_fitted(
    tmp_path: Path, fit: FakeFit
) -> None:
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(40, T0 + 40 * H4))
    with make_client(cfg, tmp_path) as c:
        body = post(c, hmm_body(5)).json()
    assert "unavailable" not in body
    assert body["bars_have"] == 5
    assert fit.calls[0].observed is not None
    assert fit.calls[0].observed.tolist() == [True] * 5


def test_too_little_history_answers_null_points_and_both_counts(
    tmp_path: Path, fit: FakeFit
) -> None:
    # The real fit reaches no decision on so few rows: every output is NaN, warm-up is all rows.
    fit.answer = lambda close: FakeSeries(
        np.full(len(close), np.nan),
        np.full(len(close), np.nan),
        np.full(len(close), np.nan),
        len(close),
    )
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(4, T0))
    with make_client(cfg, tmp_path) as c:
        body = post(c, hmm_body(BARS_NEEDED - 5)).json()
    assert body["bars_have"] == BARS_NEEDED - 1
    assert body["bars_needed"] == BARS_NEEDED
    assert len(body["points"]) == BARS_NEEDED - 5
    assert all(p["state"] is None and p["prob"] is None for p in body["points"])
    assert body["warmup_bars"] == BARS_NEEDED - 1
    assert "unavailable" not in body


def test_empty_request_answers_with_no_points(client: TestClient, fit: FakeFit) -> None:
    body = post(client, hmm_body(0)).json()
    assert body["points"] == []
    assert body["bars_have"] == 0
    assert fit.calls == []


# --------------------------------------------------------------------------- no look-ahead


def test_later_posted_bars_leave_earlier_points_identical_for_a_causal_fit(
    client: TestClient, fit: FakeFit
) -> None:
    # The handler adds no look-ahead of its own: with a causal fit (fake: each bar's output is a
    # function of closes at or before it), changing the bars after t keeps every point up to t.
    def causal(close: pd.Series) -> FakeSeries:
        running = close.to_numpy().cumsum()
        return FakeSeries(
            (running.astype(np.int64) % 2).astype(np.float64),
            np.zeros(len(close)),
            (running % 7) / 14 + 0.5,
            0,
        )

    fit.answer = causal
    base = hmm_body(30)
    changed = {**base, "closes": [*base["closes"][:20], *[999.0 + i for i in range(10)]]}
    a = post(client, base).json()["points"]
    b = post(client, changed).json()["points"]
    assert a[:20] == b[:20]
    assert a[20:] != b[20:]


# --------------------------------------------------------------------------- side effects


def test_hmm_is_display_only_no_ledger_write(tmp_path: Path, fit: FakeFit) -> None:
    db = tmp_path / "ledger.db"
    cfg = config(tmp_path)
    write_archive(cfg, archive_closes(ARCHIVE_BARS, T0))
    with make_client(cfg, tmp_path) as c:
        assert post(c, hmm_body(30)).status_code == 200
    assert len(fit.calls) == 1
    with Ledger(db) as ledger:
        assert ledger.count() == 0


# --------------------------------------------------------------------------- Markov unchanged


def test_markov_response_is_byte_identical_with_and_without_the_new_fields(
    tmp_path: Path,
) -> None:
    times = [1_700_000_000 + 3600 * i for i in range(6)]
    plain = {"times": times, "closes": [100, 100, 104, 110, 100, 95], "window_bars": 2}
    with make_client(config(tmp_path), tmp_path) as c:
        a = post(c, plain)
        b = post(c, {**plain, "model": "markov"})
        d = post(c, {**plain, "model": "markov", "symbol": "BTCUSDT", "interval": "1m"})
    assert a.status_code == 200
    assert a.content == b.content == d.content
    assert a.content == (
        b'{"points":[{"time":1700000000,"state":null},{"time":1700003600,"state":null},'
        b'{"time":1700007200,"state":0},{"time":1700010800,"state":1},'
        b'{"time":1700014400,"state":0},{"time":1700018000,"state":-1}],'
        b'"window_bars":2,"threshold":0.05,"engine":"hedgefund.regime.labels.label_regimes"}'
    )


def test_markov_never_imports_the_hmm_fit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "lab.data.hmm_columns", None)  # importing it would raise
    times, closes = bars(30)
    with make_client(config(tmp_path), tmp_path) as c:
        assert post(c, {"times": times, "closes": closes}).status_code == 200


def test_a_no_trade_bar_means_the_same_posted_or_in_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fit: FakeFit
) -> None:
    """Paging left moves a bar from the posted rows to the prefix: it must stay unobserved, or
    the refit positions would shift between the two requests."""
    from lab.api import RegimeBody, hmm_regime

    cfg = dataclasses.replace(load_config(), data_dir=tmp_path, hmm=SETTINGS)
    index = pd.date_range("2024-01-01", periods=40, freq="4h", tz="UTC", name="time")
    volume = np.ones(40)
    volume[30] = 0.0
    frame = pd.DataFrame({"close": np.linspace(100.0, 139.0, 40), "volume": volume}, index=index)
    (tmp_path / "klines" / "BTCUSDT" / "4h").mkdir(parents=True)

    def load(*_args: Any, end: pd.Timestamp, **_kw: Any) -> pd.DataFrame:
        return frame[frame.index < end]

    monkeypatch.setattr("lab.api.load_candles", load)
    secs = index.as_unit("s").asi8.tolist()
    for start in (25, 35):  # the no-trade bar is posted, then it is in the prefix
        body = RegimeBody(
            times=secs[start:],
            closes=frame["close"].iloc[start:].tolist(),
            model="hmm",
            symbol="BTCUSDT",
            interval="4h",
        )
        assert isinstance(hmm_regime(cfg, body), dict)
    assert len(fit.calls) == 2
    for call in fit.calls:
        assert call.observed is not None
        assert np.flatnonzero(~call.observed).tolist() == [30]
