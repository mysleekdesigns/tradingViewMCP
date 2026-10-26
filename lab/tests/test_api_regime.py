"""POST /lab/regime: the chart's regime ribbon (hedgeFund labels, never back-dated), offline."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from lab.api import MAX_REGIME_BARS, create_app
from lab.config import load_config
from lab.ledger import Ledger
from lab.registry.regime import regime_labels
from tests.synthetic import make_candles

CANDLES = make_candles(600, seed=11, interval="4h", vol=0.02)
TIMES = [int(t.timestamp()) for t in CANDLES.index]
CLOSES = CANDLES["close"].tolist()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(load_config(), ledger_path=tmp_path / "ledger.db", warm_up=False)
    with TestClient(app, base_url="http://127.0.0.1:8788") as c:
        yield c


def post(client: TestClient, **body: Any) -> Any:
    return client.post("/lab/regime", json=body)


def states(client: TestClient, times: list[int], closes: list[float], **kw: Any) -> list[Any]:
    r = post(client, times=times, closes=closes, **kw)
    assert r.status_code == 200, r.text
    return [p["state"] for p in r.json()["points"]]


def error_paths(r: Any) -> list[str]:
    assert r.status_code == 422, r.text
    return [e["path"] for e in r.json()["errors"]]


# --------------------------------------------------------------------------- contract


def test_hand_calculated_labels_and_response_shape(client: TestClient) -> None:
    # window 2, threshold 5%: 104/100-1 = +4% -> 0; 110/100-1 = +10% -> +1;
    # 100/104-1 = -3.8% -> 0; 95/110-1 = -13.6% -> -1. The first 2 bars have no 2-bar return.
    times = [1_700_000_000 + 3600 * i for i in range(6)]
    r = post(client, times=times, closes=[100, 100, 104, 110, 100, 95], window_bars=2)
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "points": [
            {"time": t, "state": s} for t, s in zip(times, [None, None, 0, 1, 0, -1], strict=True)
        ],
        "window_bars": 2,
        "threshold": 0.05,
        "engine": "hedgefund.regime.labels.label_regimes",
    }


def test_defaults_match_the_registry_feature(client: TestClient) -> None:
    body = post(client, times=TIMES, closes=CLOSES).json()
    assert body["window_bars"] == 20 and body["threshold"] == 0.05
    assert len(body["points"]) == len(TIMES)
    assert [p["time"] for p in body["points"]] == TIMES


def test_warm_up_bars_are_null_and_the_rest_labelled(client: TestClient) -> None:
    out = states(client, TIMES, CLOSES, window_bars=30)
    assert out[:30] == [None] * 30
    assert all(s in (-1, 0, 1) for s in out[30:])
    assert {-1, 0, 1} <= set(out[30:])  # the synthetic walk visits every regime


def test_fewer_bars_than_the_window_are_all_null(client: TestClient) -> None:
    assert states(client, TIMES[:20], CLOSES[:20], window_bars=20) == [None] * 20
    assert states(client, [], []) == []


@pytest.mark.parametrize(("window", "threshold"), [(20, 0.05), (7, 0.02), (1, 0.001)])
def test_agrees_with_regime_labels(client: TestClient, window: int, threshold: float) -> None:
    ref = regime_labels(CANDLES["close"], window, threshold)
    expected = [None if v < 0 else int(v) - 1 for v in ref]
    assert states(client, TIMES, CLOSES, window_bars=window, threshold=threshold) == expected


def test_gaps_are_not_filled_window_counts_rows(client: TestClient) -> None:
    # A missing bar (a 3h gap) is not forward-filled: the 2-bar window spans 2 rows.
    times = [0, 3600, 3 * 3600 * 2, 3 * 3600 * 3]
    assert states(client, times, [100, 100, 110, 110], window_bars=2) == [None, None, 1, 1]


def test_writes_nothing_to_the_ledger(client: TestClient, tmp_path: Path) -> None:
    states(client, TIMES, CLOSES)
    with Ledger(tmp_path / "ledger.db") as led:
        assert led.count(None) == 0


# --------------------------------------------------------------------------- look-ahead


@pytest.mark.parametrize("k", [0, 19, 20, 21, 250, 598])
def test_prefix_labels_equal_full_series_labels(client: TestClient, k: int) -> None:
    full = states(client, TIMES, CLOSES)
    assert states(client, TIMES[: k + 1], CLOSES[: k + 1]) == full[: k + 1]


@pytest.mark.parametrize("k", [0, 19, 20, 21, 250, 598])
def test_perturbing_closes_after_k_leaves_labels_up_to_k_unchanged(
    client: TestClient, k: int
) -> None:
    full = states(client, TIMES, CLOSES)
    rng = np.random.default_rng(k)
    perturbed = list(CLOSES)
    for i in range(k + 1, len(perturbed)):
        perturbed[i] *= float(rng.uniform(0.3, 3.0))
    out = states(client, TIMES, perturbed)
    assert out[: k + 1] == full[: k + 1]
    if k < len(CLOSES) - 50:
        assert out[k + 1 :] != full[k + 1 :]  # the perturbation did reach the later labels


# --------------------------------------------------------------------------- validation


def test_length_mismatch(client: TestClient) -> None:
    assert error_paths(post(client, times=TIMES[:5], closes=CLOSES[:4])) == ["closes"]


@pytest.mark.parametrize(
    "times",
    [[3, 2, 4], [1, 1, 2]],
    ids=["descending", "duplicate"],
)
def test_times_must_be_strictly_ascending(client: TestClient, times: list[int]) -> None:
    assert error_paths(post(client, times=times, closes=[1.0, 2.0, 3.0])) == ["times"]


def test_times_must_be_integers(client: TestClient) -> None:
    assert error_paths(post(client, times=[1, 2.5, 3], closes=[1.0, 2.0, 3.0])) == ["times.1"]
    assert error_paths(post(client, times=[1, "2", 3], closes=[1.0, 2.0, 3.0])) == ["times.1"]


@pytest.mark.parametrize("bad", [0.0, -1.0, "NaN", "Infinity"])
def test_closes_must_be_positive_and_finite(client: TestClient, bad: Any) -> None:
    raw = '{"times": [1, 2, 3], "closes": [1.0, %s, 3.0]}' % (
        bad if isinstance(bad, str) else repr(bad)
    )
    r = client.post("/lab/regime", content=raw, headers={"Content-Type": "application/json"})
    assert error_paths(r) == ["closes.1"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window_bars", 0),
        ("window_bars", 5001),
        ("window_bars", 2.5),
        ("threshold", 0.0005),
        ("threshold", 1.5),
    ],
)
def test_parameter_bounds(client: TestClient, field: str, value: Any) -> None:
    body = {"times": TIMES[:30], "closes": CLOSES[:30], field: value}
    assert error_paths(client.post("/lab/regime", json=body)) == [field]


def test_bar_limit(client: TestClient) -> None:
    n = MAX_REGIME_BARS + 1
    times = list(range(n))
    closes = [100.0] * n
    assert set(error_paths(post(client, times=times, closes=closes))) == {"times", "closes"}
    ok = states(client, times[:MAX_REGIME_BARS], closes[:MAX_REGIME_BARS])
    assert len(ok) == MAX_REGIME_BARS


def test_missing_fields(client: TestClient) -> None:
    assert set(error_paths(post(client))) == {"times", "closes"}


def test_host_guard_applies(tmp_path: Path) -> None:
    app = create_app(load_config(), ledger_path=tmp_path / "ledger.db", warm_up=False)
    with TestClient(app, base_url="http://evil.example") as c:
        r = c.post("/lab/regime", json={"times": [1], "closes": [1.0]})
    assert r.status_code == 403
