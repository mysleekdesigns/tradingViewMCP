"""JSON serialisation for the lab API (``BacktestReport`` / ``LedgerItem`` in
``web/src/data/labTypes.ts``).

Conventions: times are integer UNIX seconds (UTC); non-finite floats become ``null``; ratios
are fractions; equity curves are normalised to 1.0 at the window start. No I/O.
Results are historical and not forward-looking.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

import numpy as np
import pandas as pd

from lab.types import Metrics, Trade, TrialRecord

__all__ = [
    "LAB_METRIC_KEYS",
    "empty_metrics",
    "equity_points",
    "ledger_detail",
    "ledger_item",
    "metrics_dict",
    "num",
    "seconds",
    "to_jsonable",
    "trade_dict",
]

LAB_METRIC_KEYS: tuple[str, ...] = (
    "total_return",
    "cagr",
    "volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "win_rate",
    "profit_factor",
    "n_trades",
    "exposure",
    "turnover",
    "bars",
)
_INT_METRICS = frozenset({"n_trades", "bars"})


def num(value: object) -> float | None:
    """A finite float, or ``None`` for NaN/inf/None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def seconds(ts: pd.Timestamp) -> int:
    """UNIX seconds of a tz-aware timestamp."""
    if ts.tzinfo is None:
        raise ValueError(f"timestamp {ts} must be tz-aware UTC")
    return int(ts.tz_convert("UTC").value // 1_000_000_000)  # .value is always nanoseconds


def to_jsonable(value: Any) -> Any:
    """Deep copy with numpy scalars/arrays converted and non-finite floats as ``None``."""
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [to_jsonable(v) for v in value.tolist()]
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if isinstance(value, int | np.integer):
        return int(value)
    if isinstance(value, float | np.floating):
        return num(value)
    if isinstance(value, pd.Timestamp):
        return seconds(value)
    return value


def metrics_dict(m: Metrics) -> dict[str, float | int | None]:
    """``LabMetrics``: every metric, non-finite as ``None``; counts as ints."""
    raw = asdict(m)
    return {k: (int(raw[k]) if k in _INT_METRICS else num(raw[k])) for k in LAB_METRIC_KEYS}


def empty_metrics() -> dict[str, float | int | None]:
    return {k: (0 if k in _INT_METRICS else None) for k in LAB_METRIC_KEYS}


def equity_points(equity: pd.Series, init_cash: float) -> list[dict[str, float | int]]:
    """``[{time, value}]`` with ``value = equity / init_cash`` (1.0 at the window start)."""
    times = (equity.index.as_unit("s").asi8).tolist()
    values = (equity.to_numpy(dtype=float) / float(init_cash)).tolist()
    return [
        {"time": int(t), "value": round(v, 10)}
        for t, v in zip(times, values, strict=True)
        if math.isfinite(v)
    ]


def trade_dict(t: Trade) -> dict[str, Any]:
    return {
        "entry_time": seconds(t.entry_time),
        "entry_price": num(t.entry_price),
        "exit_time": seconds(t.exit_time) if t.exit_time is not None else None,
        "exit_price": num(t.exit_price),
        "pnl": num(t.pnl),
        "return_pct": num(t.return_pct),
        "exit_reason": t.exit_reason,
    }


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def ledger_item(rec: TrialRecord) -> dict[str, Any]:
    """``LedgerItem``: ``sharpe`` is the annualised Sharpe from the stored metrics."""
    plan = _loads(rec.plan_json)
    metrics = _loads(rec.metrics_json)
    metrics = metrics if isinstance(metrics, dict) else {}
    name = plan.get("name") if isinstance(plan, dict) else None
    return {
        "id": int(rec.id) if rec.id is not None else 0,
        "run_id": rec.run_id,
        "created_at": seconds(rec.created_at) if rec.created_at is not None else 0,
        "source": rec.source,
        "status": rec.status,
        "plan_hash": rec.plan_hash,
        "plan_name": str(name) if name is not None else "",
        "symbol": rec.symbol,
        "interval": rec.interval,
        "window_kind": rec.window_kind,
        "sharpe": num(metrics.get("sharpe")),
        "total_return": num(metrics.get("total_return")),
        "max_drawdown": num(metrics.get("max_drawdown")),
        "n_trades": int(rec.n_trades),
        "error": rec.error,
    }


def ledger_detail(rec: TrialRecord) -> dict[str, Any]:
    """``LedgerItem`` plus the stored ``plan`` object, ``params``, window, and ``metrics``
    (``None`` for error rows that have none)."""
    item = ledger_item(rec)
    plan = _loads(rec.plan_json)
    params = _loads(rec.params_json)
    metrics = _loads(rec.metrics_json)
    item["plan"] = plan if isinstance(plan, dict) else {}
    item["params"] = params if isinstance(params, dict) else {}
    item["metrics"] = to_jsonable(metrics) if isinstance(metrics, dict) and metrics else None
    item["window_start"] = seconds(rec.window_start)
    item["window_end"] = seconds(rec.window_end)
    return item


def ordinal(n: int) -> str:
    """English ordinal: "1st", "2nd", "3rd", "11th".."13th", "21st", "82nd", "83rd"."""
    suffix = "th" if n % 100 in (11, 12, 13) else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
