"""FastAPI service: routes under /lab, contract shapes, status codes, host guard (offline)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lab.api import create_app, host_allowed
from lab.config import LabConfig, load_config
from lab.ledger import Ledger
from lab.registry import REGISTRY
from tests.synthetic import make_candles

EMA_PLAN = {
    "name": "ema_cross",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {"ema_fast": {"ema": {"period": 20}}, "ema_slow": {"ema": {"period": 50}}},
    "long_entry": "crosses_above(ema_fast, ema_slow)",
    "long_exit": "crosses_below(ema_fast, ema_slow)",
    "risk": {"position_size_pct": 100},
}
CANDLES = make_candles(3300, seed=17, start="2018-07-01", price=6000.0, vol=0.012)


def fake_loader(symbol: str, interval: str, end: pd.Timestamp) -> pd.DataFrame:
    return CANDLES.loc[CANDLES.index < end]


def small_cfg(tmp_path: Path) -> LabConfig:
    base = load_config()
    return replace(
        base,
        data_dir=tmp_path / "data",
        splits=replace(base.splits, wf_train_months=6, wf_test_months=3, wf_step_months=3),
        benchmarks=replace(base.benchmarks, random_entry_paths=10),
        stats=replace(base.stats, bootstrap_samples=100, monte_carlo_paths=50, permutation_paths=5),
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        small_cfg(tmp_path), ledger_path=tmp_path / "ledger.db", loader=fake_loader, warm_up=False
    )
    with TestClient(app, base_url="http://127.0.0.1:8788") as c:
        yield c


@pytest.fixture
def no_data_client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(small_cfg(tmp_path), ledger_path=tmp_path / "ledger.db", warm_up=False)
    with TestClient(app, base_url="http://localhost:8788") as c:
        yield c


# --------------------------------------------------------------------------- basics


def test_health(client: TestClient) -> None:
    r = client.get("/lab/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and isinstance(body["version"], str) and body["version"]


def test_routes_live_under_lab_only(client: TestClient) -> None:
    assert client.get("/health").status_code == 404


def test_registry_lists_every_feature_with_params(client: TestClient) -> None:
    body = client.get("/lab/registry").json()
    names = [f["name"] for f in body["features"]]
    assert names == list(REGISTRY)
    ema = next(f for f in body["features"] if f["name"] == "ema")
    assert set(ema) == {"name", "description", "outputs", "params"}
    assert ema["outputs"] == ["value"]
    (period,) = ema["params"]
    assert set(period) == {"name", "kind", "default", "low", "high", "description"}
    assert period["name"] == "period" and period["kind"] == "int"


def test_validate_ok_and_errors(client: TestClient) -> None:
    assert client.post("/lab/validate", json={"plan": EMA_PLAN}).json() == {
        "ok": True,
        "errors": [],
    }
    bad = dict(EMA_PLAN, interval="7m")
    r = client.post("/lab/validate", json={"plan": bad})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["errors"][0]["path"] == "interval"
    assert set(body["errors"][0]) == {"path", "message"}


def test_validate_accepts_plan_json_text(client: TestClient) -> None:
    assert client.post("/lab/validate", json={"plan": json.dumps(EMA_PLAN)}).json()["ok"] is True


# --------------------------------------------------------------------------- backtest


def test_backtest_returns_the_report(client: TestClient) -> None:
    r = client.post("/lab/backtest", json={"plan": EMA_PLAN, "run_id": "api-test"})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["run_id"] == "api-test" and rep["symbol"] == "BTCUSDT"
    assert rep["window"]["kind"] == "design"
    assert rep["metrics"]["net"]["n_trades"] == len(rep["trades"])
    assert rep["equity"][0]["value"] == 1.0
    assert rep["trial_id"] >= 1


def test_backtest_defaults_run_id_to_ui(client: TestClient) -> None:
    assert client.post("/lab/backtest", json={"plan": EMA_PLAN}).json()["run_id"] == "ui"


def test_backtest_invalid_plan_is_422_with_errors(client: TestClient) -> None:
    r = client.post("/lab/backtest", json={"plan": dict(EMA_PLAN, long_entry="ema_fats > 1")})
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert errors and errors[0]["path"] == "long_entry" and "ema_fats" in errors[0]["message"]


def test_bad_request_body_is_422_with_errors(client: TestClient) -> None:
    r = client.post("/lab/backtest", json={"plan": EMA_PLAN, "run_id": "bad id with spaces"})
    assert r.status_code == 422
    assert r.json()["errors"][0]["path"] == "run_id"
    assert client.post("/lab/backtest", json={}).status_code == 422


def test_missing_data_is_503_with_the_download_command(no_data_client: TestClient) -> None:
    r = no_data_client.post("/lab/backtest", json={"plan": EMA_PLAN})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "cd lab && uv run python -m lab.data.archive BTCUSDT 4h 2018-01 " in detail


def test_adversarial_endpoint(client: TestClient) -> None:
    r = client.post("/lab/adversarial", json={"plan": EMA_PLAN, "run_id": "adv"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"delay", "sign_flip", "cost_stress", "monte_carlo", "permutation"} <= set(body)
    assert len(body["verdicts"]) == 5


# --------------------------------------------------------------------------- ledger


def test_ledger_lists_newest_first_with_paging_and_run_filter(client: TestClient) -> None:
    rep = client.post("/lab/backtest", json={"plan": EMA_PLAN, "run_id": "page"}).json()
    client.post("/lab/backtest", json={"plan": EMA_PLAN, "run_id": "other"})
    page = client.get("/lab/ledger", params={"run_id": "page", "limit": 3}).json()
    total = page["total"]
    assert total > 3 and len(page["items"]) == 3
    ids = [i["id"] for i in page["items"]]
    assert ids == sorted(ids, reverse=True)
    assert all(i["run_id"] == "page" for i in page["items"])
    rest = client.get("/lab/ledger", params={"run_id": "page", "limit": 100, "offset": 3}).json()
    all_ids = ids + [i["id"] for i in rest["items"]]
    assert len(all_ids) == total and rep["trial_id"] in all_ids
    everything = client.get("/lab/ledger").json()
    assert everything["total"] > total


def test_ledger_item_shape(client: TestClient) -> None:
    rep = client.post("/lab/backtest", json={"plan": EMA_PLAN, "run_id": "shape"}).json()
    items = client.get("/lab/ledger", params={"run_id": "shape", "limit": 100}).json()["items"]
    api = next(i for i in items if i["id"] == rep["trial_id"])
    assert set(api) == {
        "id", "run_id", "created_at", "source", "status", "plan_hash", "plan_name", "symbol",
        "interval", "window_kind", "sharpe", "total_return", "max_drawdown", "n_trades", "error",
    }  # fmt: skip
    assert api["source"] == "api" and api["status"] == "ok" and api["plan_name"] == "ema_cross"
    assert api["sharpe"] == pytest.approx(rep["metrics"]["net"]["sharpe"])  # annualised
    assert isinstance(api["created_at"], int) and api["created_at"] > 1_700_000_000
    assert {i["source"] for i in items} == {"api", "benchmark", "neighbourhood"}


def test_ledger_detail_and_404(client: TestClient) -> None:
    rep = client.post("/lab/backtest", json={"plan": EMA_PLAN}).json()
    detail = client.get(f"/lab/ledger/{rep['trial_id']}").json()
    assert detail["plan"]["name"] == "ema_cross" and detail["plan_hash"] == rep["plan_hash"]
    assert detail["metrics"]["n_trades"] == rep["metrics"]["net"]["n_trades"]
    r = client.get("/lab/ledger/999999")
    assert r.status_code == 404 and "999999" in r.json()["detail"]


def test_ledger_rows_persist_in_the_configured_database(client: TestClient, tmp_path: Path) -> None:
    client.post("/lab/backtest", json={"plan": EMA_PLAN, "run_id": "persist"})
    with Ledger(tmp_path / "ledger.db") as led:
        assert led.count("persist") > 0


# --------------------------------------------------------------------------- host guard


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "localhost.attacker"])
def test_foreign_host_headers_are_forbidden(client: TestClient, host: str) -> None:
    r = client.get("/lab/health", headers={"host": host})
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("localhost", True),
        ("localhost:5173", True),
        ("127.0.0.1:8788", True),
        ("[::1]:8788", True),
        ("[::1]", True),
        ("LOCALHOST:5173", True),
        ("localhost:", False),
        ("localhost:abc", False),
        ("[::2]:8788", False),
        ("0.0.0.0:8788", False),
        ("", False),
        (None, False),
    ],
)
def test_host_allowlist(host: str | None, ok: bool) -> None:
    assert host_allowed(host) is ok


def test_vite_proxied_host_is_allowed(client: TestClient) -> None:
    assert client.get("/lab/health", headers={"host": "localhost:5173"}).status_code == 200


def _holdout_row(tmp_path: Path, run_id: str) -> int:
    with Ledger(tmp_path / "ledger.db") as led:
        design = led.list(run_id=run_id, limit=1)[0]
        return led.record(
            replace(
                design,
                id=None,
                created_at=None,
                window_kind="holdout",
                metrics_json='{"sharpe": 9.9, "total_return": 0.5}',
            )
        )


def test_ledger_list_never_serves_holdout_trials(client: TestClient, tmp_path: Path) -> None:
    client.post("/lab/backtest", json={"plan": EMA_PLAN, "run_id": "hold"})
    before = client.get("/lab/ledger", params={"run_id": "hold", "limit": 500}).json()
    everything_before = client.get("/lab/ledger").json()["total"]
    hidden = _holdout_row(tmp_path, "hold")
    after = client.get("/lab/ledger", params={"run_id": "hold", "limit": 500}).json()
    assert after["total"] == before["total"]  # the total excludes holdout rows too
    assert hidden not in [i["id"] for i in after["items"]]
    assert all(i["window_kind"] != "holdout" for i in after["items"])
    assert client.get("/lab/ledger").json()["total"] == everything_before
    # paging stays consistent with the filtered total
    page = client.get("/lab/ledger", params={"run_id": "hold", "limit": 2, "offset": 0}).json()
    assert page["items"][0]["id"] == after["items"][0]["id"]


def test_ledger_detail_of_a_holdout_trial_is_404(client: TestClient, tmp_path: Path) -> None:
    client.post("/lab/backtest", json={"plan": EMA_PLAN, "run_id": "hold"})
    hidden = _holdout_row(tmp_path, "hold")
    r = client.get(f"/lab/ledger/{hidden}")
    assert r.status_code == 404 and r.json() == {
        "detail": f"no trial with id {hidden} in the ledger"
    }
    assert "9.9" not in r.text
