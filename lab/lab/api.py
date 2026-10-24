"""Local FastAPI service for the web backtest panel (PLAN.md Phase 7 How §9).

Run: ``uv run python -m lab.api`` (host/port from ``config/lab.toml``, 127.0.0.1:8788) or
``uv run uvicorn lab.api:app --host 127.0.0.1 --port 8788``. The web app reaches it through the
Vite proxy at same-origin ``/lab``, so there is no CORS. Every route lives under ``/lab``.

Safety: loopback bind only; a Host-header allowlist (localhost / 127.0.0.1 / [::1], any port)
answers 403 otherwise, so a DNS-rebinding page cannot drive the API. Backtests use the design
window only; the locked holdout is not reachable from here. Nothing here trades.

Concurrency: endpoints are sync (FastAPI runs them in a threadpool). Each request opens its own
:class:`~lab.ledger.Ledger` in the thread that serves it (a sqlite3 connection is bound to its
thread) and closes it afterwards; backtests are serialised by a lock so two reports in the same
run cannot interleave their trial counts.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lab.config import LabConfig, load_config
from lab.engine import run_signals, run_weights
from lab.ledger import Ledger
from lab.plan import validate_plan
from lab.plan.validator import PlanValidationError
from lab.registry import REGISTRY
from lab.report import ledger_detail, ledger_item
from lab.runner import (
    CandleLoader,
    DataUnavailableError,
    run_adversarial_report,
    run_backtest_report,
)
from lab.types import ZERO_COSTS

__all__ = ["ALLOWED_HOSTS", "app", "create_app", "host_allowed", "main"]

ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
LOOPBACK_BIND = frozenset({"127.0.0.1", "::1", "localhost"})
RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"
MAX_LEDGER_PAGE = 500


def _version() -> str:
    try:
        return pkg_version("lab")
    except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
        return "0.0.0"


def host_allowed(host: str | None) -> bool:
    """True for ``localhost``, ``127.0.0.1``, or ``[::1]`` with an optional numeric port."""
    if not host:
        return False
    h = host.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        if end < 0:
            return False
        name, rest = h[: end + 1], h[end + 1 :]
    else:
        name, _, port = h.partition(":")
        rest = f":{port}" if port or h.endswith(":") else ""
    if rest and not (rest.startswith(":") and rest[1:].isdigit()):
        return False
    return name in ALLOWED_HOSTS


class PlanBody(BaseModel):
    plan: dict[str, Any] | str


class RunBody(PlanBody):
    run_id: str = Field(default="ui", pattern=RUN_ID_PATTERN)


def _warm_up() -> None:
    """Compile the numba/vectorbt paths once so the first backtest is not ~1.5 s slower."""
    idx = pd.date_range("2024-01-01", periods=8, freq="4h", tz="UTC", name="time")
    px = np.linspace(100.0, 107.0, 8)
    candles = pd.DataFrame(
        {
            "open": px,
            "high": px * 1.01,
            "low": px * 0.99,
            "close": px,
            "volume": np.ones(8),
            "tradable": np.ones(8, dtype=bool),
        },
        index=idx,
    )
    sig = pd.Series([False, True, False, False, True, False, False, False], index=idx)
    run_signals(candles, sig, sig.shift(2, fill_value=False), costs=ZERO_COSTS)
    w = pd.Series(np.nan, index=idx)
    w.iloc[0] = 1.0
    run_weights(candles, w, costs=ZERO_COSTS)


def _errors_json(errors: list[Any]) -> list[dict[str, str]]:
    return [{"path": e.path, "message": e.message} for e in errors]


def create_app(
    cfg: LabConfig | None = None,
    *,
    ledger_path: Path | str | None = None,
    loader: CandleLoader | None = None,
    warm_up: bool = True,
) -> FastAPI:
    """Build the app. Tests inject ``cfg``, a tmp ``ledger_path``, and a synthetic ``loader``."""
    config = cfg if cfg is not None else load_config()
    db_path = Path(ledger_path) if ledger_path is not None else config.ledger_path
    report_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if warm_up:
            _warm_up()
        with Ledger(db_path):  # create/migrate the database once at startup
            pass
        yield

    app = FastAPI(
        title="tradingView lab",
        version=_version(),
        lifespan=lifespan,
        docs_url="/lab/docs",
        redoc_url=None,
        openapi_url="/lab/openapi.json",
    )
    app.state.cfg = config
    app.state.ledger_path = db_path
    app.state.loader = loader

    @contextmanager
    def open_ledger() -> Iterator[Ledger]:
        led = Ledger(db_path)
        try:
            yield led
        finally:
            led.close()

    @app.middleware("http")
    async def host_guard(request: Request, call_next: Any) -> Any:
        if not host_allowed(request.headers.get("host")):
            return JSONResponse(
                status_code=403,
                content={"detail": "forbidden host; the lab API serves localhost only"},
            )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def request_errors(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "path": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                "message": str(err.get("msg", "invalid request")),
            }
            for err in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"errors": errors})

    @app.get("/lab/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": _version()}

    @app.get("/lab/registry")
    def registry() -> dict[str, Any]:
        return {
            "features": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "outputs": list(spec.outputs),
                    "params": [
                        {
                            "name": p.name,
                            "kind": p.kind,
                            "default": p.default,
                            "low": p.low,
                            "high": p.high,
                            "description": p.description,
                        }
                        for p in spec.params
                    ],
                }
                for spec in REGISTRY.values()
            ]
        }

    @app.post("/lab/validate")
    def validate(body: PlanBody) -> dict[str, Any]:
        _, errors = validate_plan(body.plan)
        return {"ok": not errors, "errors": _errors_json(errors)}

    def _run(fn: Any, body: RunBody) -> Any:
        try:
            with report_lock, open_ledger() as led:
                return fn(body.plan, cfg=config, ledger=led, run_id=body.run_id, loader=loader)
        except PlanValidationError as exc:
            return JSONResponse(status_code=422, content={"errors": _errors_json(exc.errors)})
        except DataUnavailableError as exc:
            return JSONResponse(status_code=503, content={"detail": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=500, content={"detail": f"backtest failed: {exc}"})

    @app.post("/lab/backtest")
    def backtest(body: RunBody) -> Any:
        return _run(run_backtest_report, body)

    @app.post("/lab/adversarial")
    def adversarial(body: RunBody) -> Any:
        return _run(run_adversarial_report, body)

    @app.get("/lab/ledger")
    def ledger_list(
        run_id: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=50, ge=0, le=MAX_LEDGER_PAGE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        run = run_id or None
        with open_ledger() as led:
            total = led.count(run)
            items = led.list(run_id=run, limit=limit, offset=offset)  # newest first (id DESC)
        return {"total": total, "items": [ledger_item(r) for r in items]}

    @app.get("/lab/ledger/{trial_id}")
    def ledger_get(trial_id: int) -> Any:
        with open_ledger() as led:
            try:
                rec = led.get(trial_id)
            except ValueError as exc:
                return JSONResponse(status_code=404, content={"detail": str(exc)})
        return ledger_detail(rec)

    return app


def main() -> None:
    """Serve on the configured loopback host/port (refuses a non-loopback host)."""
    import uvicorn

    cfg = load_config()
    if cfg.api_host not in LOOPBACK_BIND:
        raise ValueError(f"api.host {cfg.api_host!r} must be a loopback address (127.0.0.1)")
    uvicorn.run(create_app(cfg), host=cfg.api_host, port=cfg.api_port)


app = create_app()

if __name__ == "__main__":
    main()
