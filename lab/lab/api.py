"""Local FastAPI service for the web backtest panel (PLAN.md Phase 7 How §9).

Run: ``uv run python -m lab.api`` (host/port from ``config/lab.toml``, 127.0.0.1:8788) or
``uv run uvicorn lab.api:app --host 127.0.0.1 --port 8788``. The web app reaches it through the
Vite proxy at same-origin ``/lab``, so there is no CORS. Every route lives under ``/lab``.

Safety: loopback bind only; a Host-header allowlist (localhost / 127.0.0.1 / [::1], any port)
answers 403 otherwise, so a DNS-rebinding page cannot drive the API. Backtests use the design
window only; the locked holdout is not reachable from here. ``/lab/backtest`` and
``/lab/adversarial`` refuse (422) a ``run_id`` that is a Phase 8 research run id (pattern or
registered run), so nothing here can write trials into a research run. Nothing here trades.

Concurrency: endpoints are sync (FastAPI runs them in a threadpool). Each request opens its own
:class:`~lab.ledger.Ledger` in the thread that serves it (a sqlite3 connection is bound to its
thread) and closes it afterwards; backtests are serialised by a lock so two reports in the same
run cannot interleave their trial counts.

``POST /lab/regime`` labels chart bars with hedgeFund's regime engine for the chart's regime
ribbon. It is a display feature, not a trial: nothing is written to the ledger. Each label uses
only closes at or before its bar (``lab.registry.regime.regime_labels``), so appending bars never
changes an earlier label. With ``model = "hmm"`` the ribbon comes from the causal HMM instead
(``hmm_regime``): the archive before the first posted bar is prepended for the training window,
and only the posted bars are answered, each with the filtered probability of its state. Without
an archive that reaches the chart nothing is fitted and ``unavailable`` says why.

``POST /lab/derivatives`` serves the chart's optional funding and open-interest panes from the
lab's USD-M futures archive Parquet (``lab.data.derivatives``; historical only, no live futures
stream). Each value is the last archive row visible at the bar's close
(``lab.data.derivatives_align``): a funding settlement counts only at bars whose close is at or
after its own timestamp, an OI snapshot only from 5 minutes after its ``create_time`` (its
window convention is unverified), and a value older than its staleness limit is a gap
(null), never carried. Display only, like ``/lab/regime``: no ledger write and not a backtest, so
the design-window restriction does not apply. 503 when nothing is downloaded for the symbol.

Results are historical and not forward-looking.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
import pandas as pd
from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from lab.config import LabConfig, load_config
from lab.data import derivatives as derivatives_data
from lab.data.derivatives_align import METRICS_INTERVAL, METRICS_PUBLICATION_LAG, align_dataset
from lab.data.store import load_candles
from lab.engine import run_signals, run_weights
from lab.ledger import Ledger
from lab.plan import validate_plan
from lab.plan.schema import MIN_HMM_INTERVAL_SECONDS
from lab.plan.validator import PlanValidationError
from lab.registry import REGISTRY
from lab.registry.regime import REGIME_STATE, regime_labels
from lab.report import ledger_detail, ledger_item
from lab.research import readers
from lab.research.contract import (
    IDEAS_DB_PATH,
    JOURNAL_DIR,
    PAPER_DIR,
    RESEARCH_RUN_ID_PATTERN,
    RUN_STATE_DIR,
)
from lab.research.runs import budget_status
from lab.runner import (
    CandleLoader,
    DataUnavailableError,
    run_adversarial_report,
    run_backtest_report,
)
from lab.types import INTERVAL_SECONDS, ZERO_COSTS

__all__ = [
    "ALLOWED_HOSTS",
    "MAX_DERIVATIVES_BARS",
    "MAX_REGIME_BARS",
    "app",
    "create_app",
    "derivatives_series",
    "hmm_regime",
    "host_allowed",
    "main",
    "regime_points",
]

ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
LOOPBACK_BIND = frozenset({"127.0.0.1", "::1", "localhost"})
RUN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"
MAX_LEDGER_PAGE = 500
# Window kinds never served by /lab/ledger: locked-holdout trials (Phase 8 W2).
HIDDEN_WINDOW_KINDS: tuple[str, ...] = ("holdout",)


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


_log = logging.getLogger(__name__)

MAX_REGIME_BARS = 20_000
REGIME_ENGINE = "hedgefund.regime.labels.label_regimes"
HMM_ENGINE = "lab.registry.hmm.causal_hmm"
HMM_WAIT_SECONDS = 2.0  # how long a request waits for a running fit before answering busy
# Same bounds and defaults as the registry's regime_state feature.
_REGIME_PARAMS = {p.name: p for p in REGIME_STATE.params}
_REGIME_WINDOW = _REGIME_PARAMS["window_bars"]
_REGIME_THRESHOLD = _REGIME_PARAMS["threshold"]
# hedgeFund state index (0 Bear, 1 Sideways, 2 Bull) -> ribbon state -1 / 0 / +1.
_REGIME_STATE_VALUE = (-1, 0, 1)

PositiveClose = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class RegimeBody(BaseModel):
    """Closed bars to label: ascending unique UNIX-second times and their closes."""

    times: list[Annotated[int, Field(strict=True)]] = Field(max_length=MAX_REGIME_BARS)
    closes: list[PositiveClose] = Field(max_length=MAX_REGIME_BARS)
    model: Literal["markov", "hmm"] = "markov"
    # Required for model "hmm" only (hmm_regime); Markov never reads them.
    symbol: str | None = None
    interval: str | None = None
    window_bars: int = Field(
        default=int(_REGIME_WINDOW.default),
        ge=int(_REGIME_WINDOW.low),
        le=int(_REGIME_WINDOW.high),
        strict=True,
    )
    threshold: float = Field(
        default=float(_REGIME_THRESHOLD.default),
        ge=float(_REGIME_THRESHOLD.low),
        le=float(_REGIME_THRESHOLD.high),
        allow_inf_nan=False,
    )

    @field_validator("times")
    @classmethod
    def _ascending(cls, v: list[int]) -> list[int]:
        if any(b <= a for a, b in pairwise(v)):
            raise ValueError("times must be strictly ascending (unique UNIX seconds)")
        return v

    @field_validator("closes")
    @classmethod
    def _same_length(cls, v: list[float], info: ValidationInfo) -> list[float]:
        times = info.data.get("times")
        if times is not None and len(times) != len(v):
            raise ValueError(f"closes has {len(v)} values but times has {len(times)}")
        return v


def regime_points(
    times: list[int], closes: list[float], window_bars: int, threshold: float
) -> list[dict[str, int | None]]:
    """One ``{time, state}`` per bar; ``state`` is None during the ``window_bars`` warm-up."""
    if not times:
        return []
    close = pd.Series(
        np.asarray(closes, dtype=np.float64),
        index=pd.to_datetime(np.asarray(times, dtype=np.int64), unit="s", utc=True),
    )
    labels = regime_labels(close, window_bars, threshold)
    return [
        {"time": t, "state": _REGIME_STATE_VALUE[lab] if lab >= 0 else None}
        for t, lab in zip(times, labels.tolist(), strict=True)
    ]


MAX_DERIVATIVES_BARS = MAX_REGIME_BARS
SYMBOL_PATTERN = r"^[A-Z0-9]{2,30}$"
MAX_UNIX_SECONDS = 4_102_444_800  # 2100-01-01; keeps bar times inside pandas' datetime range
# Chart symbols, hyphen included (Coinbase's BTC-USD): the symbol becomes an archive folder name.
HMM_SYMBOL_PATTERN = re.compile(r"[A-Z0-9-]{2,30}")


def _hmm_request_errors(body: RegimeBody) -> list[dict[str, str]]:
    """Why a ``model = "hmm"`` request cannot run, as field errors; empty when it can."""
    errors: list[dict[str, str]] = []
    if body.symbol is None:
        errors.append({"path": "symbol", "message": "model hmm requires symbol"})
    elif HMM_SYMBOL_PATTERN.fullmatch(body.symbol) is None:
        errors.append({"path": "symbol", "message": f"invalid symbol {body.symbol!r}"})
    if body.interval is None:
        errors.append({"path": "interval", "message": "model hmm requires interval"})
    else:
        seconds = INTERVAL_SECONDS.get(body.interval)
        minimum = f"{MIN_HMM_INTERVAL_SECONDS // 3600}h"
        if seconds is None:
            errors.append(
                {
                    "path": "interval",
                    "message": (
                        f"unknown interval {body.interval!r}; "
                        f"expected one of {sorted(INTERVAL_SECONDS)}"
                    ),
                }
            )
        elif seconds < MIN_HMM_INTERVAL_SECONDS:
            errors.append(
                {
                    "path": "interval",
                    "message": f"the HMM needs {minimum} bars or longer, got {body.interval}",
                }
            )
    if body.times and not 0 <= body.times[0] <= body.times[-1] <= MAX_UNIX_SECONDS:
        errors.append({"path": "times", "message": "times must be UNIX seconds"})
    return errors


def _archive_prefix(
    config: LabConfig, symbol: str, interval: str, first_time: int, last_time: int
) -> tuple[pd.DataFrame | None, str | None]:
    """Archive rows through the chart's last bar, or None and why they cannot be used.

    The fit counts its refits from the first row it is given, so that row must not depend on how
    far the chart has paged: the archive has to reach the chart's first bar, with no hole at the
    join. Overlap is fine: rows from the first bar on only say which posted bars had no trades.
    Rows after the last posted bar are not even read, so replay's cursor stays the end of the data.
    """
    first = pd.Timestamp(first_time, unit="s", tz="UTC")
    missing = f"no {symbol} {interval} archive, which the HMM needs for its first bars"
    if not (config.parquet_dir / symbol / interval).is_dir():
        return None, missing
    try:
        candles = load_candles(
            symbol,
            interval,
            parquet_dir=config.parquet_dir,
            end=pd.Timestamp(last_time, unit="s", tz="UTC") + pd.Timedelta(seconds=1),
        )
    except ValueError as exc:
        _log.warning("HMM ribbon: archive of %s %s not used: %s", symbol, interval, exc)
        return None, missing
    if candles.empty or candles.index[0] > first:
        return None, f"the {symbol} {interval} archive starts after the chart's first bar"
    # An archive row at or after the first bar means the chart overlaps it: any hole just before
    # that row is the market's own, the same however far the chart has paged.
    if (
        candles.index[-1] < first
        and (first - candles.index[-1]).total_seconds() > (INTERVAL_SECONDS[interval])
    ):
        return None, (
            f"the {symbol} {interval} archive ends {candles.index[-1]:%Y-%m-%d %H:%M} UTC, "
            f"the chart starts {first:%Y-%m-%d %H:%M} UTC"
        )
    return candles, None


def hmm_regime(config: LabConfig, body: RegimeBody) -> Any:
    """``POST /lab/regime`` with ``model = "hmm"``: the causal HMM's ribbon for the posted bars.

    The chart loads far fewer bars than the training window, so the archive's rows strictly
    before the first posted bar go in front of the posted closes; only posted bars are returned.
    The chart sends closed bars and each output uses closes at or before its bar, so nothing is
    back-dated. Fewer rows than ``bars_needed`` means every point is null. Without an archive
    that reaches the chart (``_archive_prefix``) nothing is fitted: every point is null and
    ``unavailable`` says why, because labels fitted on a series that starts wherever the chart
    happens to start would change when the chart pages left. A bar the archive holds without
    trades is not an observation (as in the lab's own load), posted or not, so a bar does not
    change meaning when paging moves it from the posted rows to the prefix. Display only: no
    ledger write.
    """
    errors = _hmm_request_errors(body)
    if errors or body.symbol is None or body.interval is None:
        return JSONResponse(status_code=422, content={"errors": errors})
    settings = config.hmm
    if settings is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "the HMM ribbon is off: lab.toml has no [regime.hmm] settings"},
        )

    def answer(
        points: list[dict[str, Any]],
        warmup_bars: int,
        bars_have: int,
        bear_is_high_vol: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "points": points,
            "model": "hmm",
            "engine": HMM_ENGINE,
            "n_states": settings.n_states,
            "warmup_bars": warmup_bars,
            "bars_needed": settings.train_bars + settings.vol_window_bars + 1,
            "bars_have": bars_have,
            "bear_is_high_vol": bear_is_high_vol,
        }

    if not body.times:
        return answer([], 0, 0)
    archive, unavailable = _archive_prefix(
        config, body.symbol, body.interval, body.times[0], body.times[-1]
    )
    if archive is None:
        n = len(body.times)
        points = [{"time": t, "state": None, "prob": None} for t in body.times]
        return {**answer(points, n, n), "unavailable": unavailable}

    posted = pd.Series(
        np.asarray(body.closes, dtype=np.float64),
        index=pd.to_datetime(np.asarray(body.times, dtype=np.int64), unit="s", utc=True),
    )
    prefix = archive[archive.index < posted.index[0]]
    close = pd.concat([prefix["close"].astype(np.float64), posted]) if len(prefix) else posted
    no_trades = archive.index[archive["volume"].to_numpy() <= 0]
    observed = ~close.index.isin(no_trades)
    # Lazy: only this path pulls in the HMM fit (hmmlearn), never a Markov request.
    from lab.data.hmm_columns import HmmBusyError, hmm_series

    try:
        series = hmm_series(
            close,
            settings,
            bar_seconds=INTERVAL_SECONDS[body.interval],
            cache_dir=config.hmm_cache_dir,
            wait_seconds=HMM_WAIT_SECONDS,
            observed=observed,
        )
    except HmmBusyError:
        # ``busy`` tells the chart to ask again instead of showing a failure.
        return JSONResponse(
            status_code=503, content={"detail": "another HMM fit is running", "busy": True}
        )
    except (ValueError, RuntimeError) as exc:  # RuntimeError: every Baum-Welch restart failed
        return JSONResponse(status_code=503, content={"detail": f"HMM fit failed: {exc}"})
    first = len(close) - len(posted)
    return answer(
        [
            hmm_point(t, state, prob, settings.n_states)
            for t, state, prob in zip(
                body.times,
                series.state[first:].tolist(),
                series.state_prob[first:].tolist(),
                strict=True,
            )
        ],
        int(series.warmup_bars),
        len(close),
        latest_bear_is_high_vol(series.bear_high_vol[first:]),
    )


def latest_bear_is_high_vol(flags: np.ndarray) -> bool | None:
    """Whether the newest decided bar's "bear" (lowest mean return in its fit) is also that fit's
    higher-volatility state; None without a decision. On BTC 4h it often is not, so the chart
    says so instead of letting "Bear" read as the volatile regime."""
    decided = flags[np.isfinite(flags)]
    return bool(decided[-1] > 0.5) if decided.size else None


def hmm_point(time: int, state: float, prob: float, n_states: int) -> dict[str, Any]:
    """One ribbon point: lowest state id -1, highest +1, any middle id 0; null without a decision.

    ``prob`` is the filtered probability of ``state``. Non-finite values become null, so they
    never reach the JSON answer.
    """
    if not (math.isfinite(state) and math.isfinite(prob)):
        return {"time": time, "state": None, "prob": None}
    ribbon = -1 if state == 0 else 1 if state == n_states - 1 else 0
    return {"time": time, "state": ribbon, "prob": prob}


DERIVATIVES_SOURCE = "data.binance.vision futures/um archive (historical; no live stream)"
FUNDING_DATASET, FUNDING_VALUE = "fundingRate", "funding_rate"
OI_DATASET, OI_VALUE = "metrics", "sum_open_interest_value"
# Load margin before the first bar: no funding interval exceeds a day; OI rows are 5-minute and
# become visible METRICS_PUBLICATION_LAG after create_time, so their margin adds that lag.
_FUNDING_MARGIN_PER_INTERVAL = pd.Timedelta(hours=24)

BarTime = Annotated[int, Field(strict=True, ge=0, le=MAX_UNIX_SECONDS)]


class DerivativesBody(BaseModel):
    """Closed spot bars (open times, UNIX seconds, strictly ascending) to align funding and open
    interest to."""

    symbol: str = Field(strict=True, pattern=SYMBOL_PATTERN)
    times: list[BarTime] = Field(max_length=MAX_DERIVATIVES_BARS)
    bar_seconds: int = Field(strict=True, ge=60, le=604_800)

    @field_validator("times")
    @classmethod
    def _ascending(cls, v: list[int]) -> list[int]:
        if any(b <= a for a, b in pairwise(v)):
            raise ValueError("times must be strictly ascending (unique UNIX seconds)")
        return v


def _extent(data_dir: Path, dataset: str, symbol: str, column: str) -> dict[str, int | None]:
    """First / last archive timestamp (UNIX s) with a non-null ``column`` on disk, reading only
    the files needed from each end (file names sort chronologically)."""
    directory = derivatives_data.series_dir(data_dir, dataset, symbol)
    files = (
        sorted(p for p in directory.glob("*.parquet") if p.is_file()) if directory.is_dir() else []
    )

    def edge(paths: list[Path], last: bool) -> int | None:
        for path in paths:
            frame = pd.read_parquet(path, columns=[column], engine="pyarrow")
            times = pd.DatetimeIndex(frame.index)[frame[column].notna().to_numpy()]
            if len(times):
                ts = times.max() if last else times.min()
                return int(ts.tz_convert("UTC").floor("s").timestamp())
        return None

    return {"first": edge(files, last=False), "last": edge(files[::-1], last=True)}


def _points(times: list[int], values: pd.Series) -> list[dict[str, int | float | None]]:
    return [
        {"time": t, "value": None if np.isnan(v) else float(v)}
        for t, v in zip(times, values.to_numpy(dtype=np.float64).tolist(), strict=True)
    ]


def derivatives_series(
    config: LabConfig, symbol: str, times: list[int], bar_seconds: int
) -> dict[str, Any] | None:
    """Funding rate and OI value (USD) at each bar's close; ``None`` when neither series has any
    data on disk for ``symbol``. Point in time: see ``lab.data.derivatives_align``."""
    data_dir = config.derivatives_dir
    stale = config.derivatives.stale_after_intervals
    available = {
        "funding": _extent(data_dir, FUNDING_DATASET, symbol, FUNDING_VALUE),
        "open_interest": _extent(data_dir, OI_DATASET, symbol, OI_VALUE),
    }
    if all(a["first"] is None for a in available.values()):
        return None
    bar_times = pd.DatetimeIndex(
        pd.to_datetime(np.asarray(times, dtype=np.int64), unit="s", utc=True), name="time"
    )
    out: dict[str, Any] = {"symbol": symbol, "bar_seconds": bar_seconds}
    for key, dataset, column, margin in (
        ("funding", FUNDING_DATASET, FUNDING_VALUE, _FUNDING_MARGIN_PER_INTERVAL * stale),
        (
            "open_interest",
            OI_DATASET,
            OI_VALUE,
            METRICS_INTERVAL * stale + METRICS_PUBLICATION_LAG,
        ),
    ):
        if not times:
            out[key] = []
            continue
        frame = derivatives_data.load_series(
            dataset,
            symbol,
            data_dir=data_dir,
            start=bar_times[0] - margin,
            end=bar_times[-1] + pd.Timedelta(seconds=bar_seconds),  # last close, inclusive
        )
        aligned = align_dataset(dataset, frame, bar_times, bar_seconds, stale_after_intervals=stale)
        out[key] = _points(times, aligned[column])
    out["available"] = available
    out["source"] = DERIVATIVES_SOURCE
    return out


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
    journal_dir: Path | str = JOURNAL_DIR,
    ideas_db_path: Path | str = IDEAS_DB_PATH,
    run_state_dir: Path | str = RUN_STATE_DIR,
    paper_dir: Path | str = PAPER_DIR,
) -> FastAPI:
    """Build the app. Tests inject ``cfg``, a tmp ``ledger_path``, a synthetic ``loader``, and
    tmp research directories (journal, idea library, run state, paper comparisons)."""
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

    @app.post("/lab/regime")
    def regime(body: RegimeBody) -> Any:
        if body.model == "hmm":
            return hmm_regime(config, body)
        # Display only: no ledger write, no candle loading; labels only the bars the chart sent.
        return {
            "points": regime_points(body.times, body.closes, body.window_bars, body.threshold),
            "window_bars": body.window_bars,
            "threshold": body.threshold,
            "engine": REGIME_ENGINE,
        }

    @app.post("/lab/derivatives")
    def derivatives(body: DerivativesBody) -> Any:
        # Display only: no ledger write, not a trial, historical archive only (no live stream).
        # Not a backtest, so the design-window restriction does not apply.
        result = derivatives_series(config, body.symbol, body.times, body.bar_seconds)
        if result is None:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        f"no derivatives data for {body.symbol}; run `uv run python -m "
                        f"lab.data.derivatives download {body.symbol} --datasets "
                        "fundingRate,metrics --start YYYY-MM --end YYYY-MM` first"
                    )
                },
            )
        return result

    def _run(fn: Any, body: RunBody) -> Any:
        try:
            with report_lock, open_ledger() as led:
                # Phase 8 research runs are written only through the strategy-lab MCP server
                # (budget guard, lab.toml fingerprint, per-run lock); never from here.
                if (
                    re.fullmatch(RESEARCH_RUN_ID_PATTERN, body.run_id)
                    or led.research_run(body.run_id) is not None
                ):
                    return JSONResponse(
                        status_code=422,
                        content={
                            "errors": [
                                {
                                    "path": "run_id",
                                    "message": (
                                        f"run_id {body.run_id!r} is a Phase 8 research run id; "
                                        "research runs are written only through the "
                                        "strategy-lab MCP server. Use another run_id (the UI "
                                        "uses 'ui')"
                                    ),
                                }
                            ]
                        },
                    )
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
        # Locked-holdout trials are never served (their metrics would leak the holdout to
        # anything that reads the ledger, agents included); the total excludes them too.
        hidden = HIDDEN_WINDOW_KINDS
        with open_ledger() as led:
            total = led.count(run, exclude_window_kinds=hidden)
            items = led.list(
                run_id=run, limit=limit, offset=offset, exclude_window_kinds=hidden
            )  # newest first (id DESC)
        return {"total": total, "items": [ledger_item(r) for r in items]}

    @app.get("/lab/ledger/{trial_id}")
    def ledger_get(trial_id: int) -> Any:
        with open_ledger() as led:
            try:
                rec = led.get(trial_id)
            except ValueError as exc:
                return JSONResponse(status_code=404, content={"detail": str(exc)})
        if rec.window_kind in HIDDEN_WINDOW_KINDS:  # indistinguishable from a missing id
            return JSONResponse(
                status_code=404, content={"detail": f"no trial with id {trial_id} in the ledger"}
            )
        return ledger_detail(rec)

    # ------------------------------------------------------------------ research (read-only)
    # Back the web Research page. Nothing here starts runs, spends holdout checks, or writes;
    # missing directories or databases read as empty lists.

    def _run_item(led: Ledger, run: dict[str, Any]) -> dict[str, Any]:
        run_id = run["run_id"]
        conf = run["config"] if isinstance(run["config"], dict) else {}
        try:
            usage: dict[str, Any] | None = budget_status(led, run_id, config)
        except (TypeError, ValueError, KeyError):  # a malformed frozen budget
            usage = None
        return {
            "run_id": run_id,
            "created_at": int(run["created_at"].timestamp()),
            "label": run["label"],
            "config_sha256": run["config_sha256"],
            "criteria": conf.get("criteria", {}),
            "budget": conf.get("budget", {}),
            "usage": usage,
            "state": readers.read_run_state(run_state_dir, run_id),
        }

    @app.get("/lab/research/runs")
    def research_runs() -> dict[str, Any]:
        with open_ledger() as led:
            runs = list(reversed(led.research_runs()))  # ledger order is oldest first
            return {"runs": [_run_item(led, r) for r in runs]}

    @app.get("/lab/research/runs/{run_id}")
    def research_run(run_id: str = PathParam(pattern=RUN_ID_PATTERN)) -> Any:
        with open_ledger() as led:
            run = led.research_run(run_id)
            if run is None:
                return JSONResponse(
                    status_code=404, content={"detail": f"unknown research run {run_id!r}"}
                )
            return _run_item(led, run)

    @app.get("/lab/research/ideas")
    def research_ideas(
        run_id: str | None = Query(default=None, max_length=64),
        status: str | None = Query(default=None, max_length=32),
    ) -> dict[str, Any]:
        return {"ideas": readers.read_ideas(ideas_db_path, run_id=run_id, status=status)}

    @app.get("/lab/research/journal")
    def research_journal() -> dict[str, Any]:
        return {"entries": readers.list_journal(journal_dir)}

    @app.get("/lab/research/journal/{name}")
    def research_journal_entry(name: str) -> Any:
        try:
            markdown = readers.read_journal(journal_dir, name)
        except readers.JournalTooLargeError as exc:
            return JSONResponse(status_code=413, content={"detail": str(exc)})
        if markdown is None:
            return JSONResponse(status_code=404, content={"detail": "journal entry not found"})
        return {"name": name, "markdown": markdown}

    @app.get("/lab/research/candidates")
    def research_candidates() -> dict[str, Any]:
        return {"candidates": readers.list_candidates(run_state_dir)}

    @app.get("/lab/research/paper")
    def research_paper() -> dict[str, Any]:
        return {"comparisons": readers.latest_paper_comparisons(paper_dir)}

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
