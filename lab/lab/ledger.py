"""Append-only SQLite trial ledger (PLAN.md Phase 7 How §5).

Every backtest the lab runs is a row here: ok and error trials, every source (manual, api,
optuna, neighbourhood, adversarial, benchmark). Nothing is updated or deleted. SQLite triggers
reject UPDATE and DELETE on the data tables as a second line of defence.

DSR policy: the multiple-testing ``N`` is :meth:`Ledger.count_distinct_plans` (distinct
``plan_hash`` among the run's ``SELECTION_SOURCES`` trials on every window, so re-running one
plan does not inflate it), and ``V[SR_n]`` is :meth:`Ledger.selection_sharpe_variance` (latest
per-bar Sharpe per distinct plan). The variance only combines Sharpes that are commensurable
with the one being deflated: the same interval, the same ``[window_start, window_end)``, the
same cost model (``costs_json``), and never ``walk_forward_test`` rows (an out-of-sample fold
Sharpe must not replace a plan's selection Sharpe). :meth:`Ledger.count` is the raw row count.

Thread safety: a ``Ledger`` wraps one ``sqlite3`` connection created with the default
``check_same_thread=True``, so it must be used on the thread that opened it. Servers open one
``Ledger`` per request (WAL + ``busy_timeout`` make concurrent connections safe).

The ledger also enforces the locked-holdout budget: :meth:`Ledger.record_holdout_check` runs
inside ``BEGIN IMMEDIATE`` so two processes cannot both spend the last check.

Storage conventions:

* ``window_start`` / ``window_end`` are INTEGER UNIX seconds (UTC).
* ``created_at`` is an ISO-8601 UTC string with microseconds.
* ``returns`` are zlib-compressed little-endian float64 bytes (bit-identical round trip).
* ``sharpe_per_bar`` is REAL; non-finite values are stored as NULL (like JSON null).
* ``metrics_json`` encodes non-finite floats as ``null``.
* ``costs_json`` is :func:`costs_to_json` of the trial's ``CostModel`` (``"{}"`` for summary
  rows without one, and for rows written before migration 2).

Results are historical and not forward-looking.
"""

from __future__ import annotations

import builtins
import json
import math
import sqlite3
import zlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, get_args

import numpy as np
import pandas as pd

from lab.types import (
    INTERVAL_SECONDS,
    CostModel,
    Metrics,
    TrialRecord,
    TrialSource,
    TrialStatus,
    WindowKind,
)

_SOURCES: frozenset[str] = frozenset(get_args(TrialSource))
_STATUSES: frozenset[str] = frozenset(get_args(TrialStatus))
_WINDOW_KINDS: frozenset[str] = frozenset(get_args(WindowKind))
_RETURNS_DTYPE = np.dtype("<f8")


class HoldoutBudgetExceededError(RuntimeError):
    """Raised when a run has already used its locked-holdout budget."""


HoldoutBudgetExceeded = HoldoutBudgetExceededError  # name used by the shared interface

# Sources that are *selection* trials: configurations a person, the API, Optuna, or the ±20%
# neighbourhood grid tried while looking for a strategy. DSR's N counts distinct plans among
# these. Adversarial variants and benchmark/null summaries are recorded but are not candidates.
SELECTION_SOURCES: tuple[str, ...] = ("manual", "api", "optuna", "neighbourhood")


# --------------------------------------------------------------------------- migrations

# Append-only. Never edit a migration once it has been applied; add a new one instead.
# Each entry is (version, name, statements); statements run in one transaction.
_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "init",
        (
            """
            CREATE TABLE trials (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at      TEXT    NOT NULL,
                run_id          TEXT    NOT NULL,
                source          TEXT    NOT NULL,
                status          TEXT    NOT NULL,
                plan_hash       TEXT    NOT NULL,
                plan_json       TEXT    NOT NULL,
                params_json     TEXT    NOT NULL,
                symbol          TEXT    NOT NULL,
                interval        TEXT    NOT NULL,
                window_kind     TEXT    NOT NULL,
                window_start    INTEGER NOT NULL,
                window_end      INTEGER NOT NULL,
                metrics_json    TEXT    NOT NULL,
                sharpe_per_bar  REAL,
                n_trades        INTEGER NOT NULL,
                error           TEXT,
                n_returns       INTEGER,
                returns_blob    BLOB
            )
            """,
            "CREATE INDEX trials_run ON trials (run_id, status)",
            "CREATE INDEX trials_window ON trials (run_id, window_start, window_end)",
            """
            CREATE TABLE holdout_checks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT    NOT NULL,
                run_id      TEXT    NOT NULL,
                plan_hash   TEXT    NOT NULL,
                budget      INTEGER NOT NULL
            )
            """,
            "CREATE INDEX holdout_checks_run ON holdout_checks (run_id)",
            # Append-only: the database itself refuses edits and deletions.
            """
            CREATE TRIGGER trials_no_update BEFORE UPDATE ON trials
            BEGIN SELECT RAISE(ABORT, 'trial ledger is append-only'); END
            """,
            """
            CREATE TRIGGER trials_no_delete BEFORE DELETE ON trials
            BEGIN SELECT RAISE(ABORT, 'trial ledger is append-only'); END
            """,
            """
            CREATE TRIGGER holdout_checks_no_update BEFORE UPDATE ON holdout_checks
            BEGIN SELECT RAISE(ABORT, 'holdout check log is append-only'); END
            """,
            """
            CREATE TRIGGER holdout_checks_no_delete BEFORE DELETE ON holdout_checks
            BEGIN SELECT RAISE(ABORT, 'holdout check log is append-only'); END
            """,
        ),
    ),
    (
        2,
        "costs_json",
        # The cost model each trial ran with, so DSR's variance never mixes cost assumptions.
        ("ALTER TABLE trials ADD COLUMN costs_json TEXT NOT NULL DEFAULT '{}'",),
    ),
)

# Window kinds whose Sharpe never enters the DSR variance (out-of-sample fold results).
_VARIANCE_EXCLUDED_KINDS: tuple[str, ...] = ("walk_forward_test",)


# --------------------------------------------------------------------------- json helpers


def _null_non_finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _null_non_finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_null_non_finite(v) for v in value]
    return value


def _reject_constant(token: str) -> float:
    raise ValueError(f"metrics_json contains non-finite token {token!r}; encode as null")


def metrics_to_json(m: Metrics) -> str:
    """``asdict(m)`` as compact sorted-key JSON; NaN and +/-inf become ``null``."""
    if not isinstance(m, Metrics):
        raise ValueError(f"m must be a Metrics, got {type(m).__name__}")
    raw = {k: (float(v) if isinstance(v, np.floating) else v) for k, v in asdict(m).items()}
    raw = {k: (int(v) if isinstance(v, np.integer) else v) for k, v in raw.items()}
    return json.dumps(_null_non_finite(raw), sort_keys=True, separators=(",", ":"), allow_nan=False)


def costs_to_json(costs: CostModel) -> str:
    """``json.dumps(asdict(costs), sort_keys=True)``: the canonical ``TrialRecord.costs_json``."""
    if not isinstance(costs, CostModel):
        raise ValueError(f"costs must be a CostModel, got {type(costs).__name__}")
    return json.dumps(asdict(costs), sort_keys=True)


def metrics_from_json(s: str) -> dict[str, float | int | None]:
    """Parse a metrics JSON object. Raises ``ValueError`` on bad JSON, a non-object, NaN/Infinity
    tokens, or non-numeric values."""
    try:
        obj = json.loads(s, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"metrics_json is not valid JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise ValueError(f"metrics_json must be a JSON object, got {type(obj).__name__}")
    for key, value in obj.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise ValueError(f"metrics_json[{key!r}] must be a number or null, got {value!r}")
    return obj


# --------------------------------------------------------------------------- conversions


def _ts_to_seconds(ts: object, name: str) -> int:
    if not isinstance(ts, pd.Timestamp):
        raise ValueError(f"{name} must be a pd.Timestamp, got {type(ts).__name__}")
    if ts.tzinfo is None:
        raise ValueError(f"{name} must be tz-aware UTC, got naive {ts}")
    ns = ts.tz_convert("UTC").as_unit("ns").value
    seconds, rem = divmod(ns, 1_000_000_000)
    if rem:
        raise ValueError(f"{name} {ts} is not a whole second; bar times are UNIX seconds")
    return int(seconds)


def _seconds_to_ts(seconds: int) -> pd.Timestamp:
    return pd.Timestamp(seconds, unit="s", tz="UTC")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _iso_to_ts(text: str) -> pd.Timestamp:
    return pd.Timestamp(text).tz_convert("UTC")


def _encode_returns(returns: np.ndarray | None) -> tuple[int | None, bytes | None]:
    if returns is None:
        return None, None
    arr = np.asarray(returns)
    if arr.ndim != 1:
        raise ValueError(f"returns must be 1-D, got shape {arr.shape}")
    if not np.issubdtype(arr.dtype, np.floating) and not np.issubdtype(arr.dtype, np.integer):
        raise ValueError(f"returns must be numeric, got dtype {arr.dtype}")
    data = np.ascontiguousarray(arr, dtype=_RETURNS_DTYPE)
    return len(data), zlib.compress(data.tobytes())


def _decode_returns(n: int | None, blob: bytes | None) -> np.ndarray | None:
    if blob is None:
        return None
    arr = np.frombuffer(zlib.decompress(blob), dtype=_RETURNS_DTYPE).astype(np.float64)
    if n is not None and len(arr) != n:
        raise ValueError(f"stored returns length {len(arr)} does not match n_returns {n}")
    return arr


def _finite_or_none(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | np.floating | np.integer):
        raise ValueError(f"{name} must be a float or None, got {value!r}")
    v = float(value)
    return v if math.isfinite(v) else None


def _check_json(text: object, name: str) -> str:
    if not isinstance(text, str):
        raise ValueError(f"{name} must be a JSON string, got {type(text).__name__}")
    try:
        json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not valid JSON: {exc}") from None
    return text


_COLUMNS = (
    "id, created_at, run_id, source, status, plan_hash, plan_json, params_json, symbol, "
    "interval, window_kind, window_start, window_end, metrics_json, sharpe_per_bar, n_trades, "
    "error, n_returns, costs_json"
)


def _row_to_record(row: sqlite3.Row, *, include_returns: bool) -> TrialRecord:
    returns = _decode_returns(row["n_returns"], row["returns_blob"]) if include_returns else None
    return TrialRecord(
        run_id=row["run_id"],
        source=row["source"],
        status=row["status"],
        plan_hash=row["plan_hash"],
        plan_json=row["plan_json"],
        params_json=row["params_json"],
        symbol=row["symbol"],
        interval=row["interval"],
        window_kind=row["window_kind"],
        window_start=_seconds_to_ts(row["window_start"]),
        window_end=_seconds_to_ts(row["window_end"]),
        metrics_json=row["metrics_json"],
        sharpe_per_bar=row["sharpe_per_bar"],
        n_trades=row["n_trades"],
        error=row["error"],
        costs_json=row["costs_json"],
        returns=returns,
        id=row["id"],
        created_at=_iso_to_ts(row["created_at"]),
    )


# --------------------------------------------------------------------------- ledger


class Ledger:
    """Append-only trial ledger on SQLite. There are deliberately no update/delete methods."""

    def __init__(self, path: Path | str) -> None:
        if str(path) == ":memory:":
            target = ":memory:"
        else:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            target = str(p)
        # Autocommit mode; every write manages its own explicit transaction.
        self._conn = sqlite3.connect(target, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.schema_version = self._migrate()

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _migrate(self) -> int:
        conn = self._conn
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for version, name, statements in sorted(_MIGRATIONS):
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-read inside the write lock so two processes can't both apply a migration.
                current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                if current is not None and version <= current:
                    conn.execute("COMMIT")
                    continue
                for sql in statements:
                    conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, _now_iso()),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return int(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])

    # ------------------------------------------------------------------ trials

    def record(self, rec: TrialRecord) -> int:
        """Insert ``rec`` as a new row and return its id. ``created_at`` is now (UTC)."""
        if not isinstance(rec, TrialRecord):
            raise ValueError(f"rec must be a TrialRecord, got {type(rec).__name__}")
        if rec.id is not None:
            raise ValueError(
                f"rec.id is {rec.id}; the ledger assigns ids, record a fresh TrialRecord"
            )
        if not isinstance(rec.run_id, str) or not rec.run_id:
            raise ValueError(f"run_id must be a non-empty string, got {rec.run_id!r}")
        if rec.source not in _SOURCES:
            raise ValueError(f"source {rec.source!r} must be one of {sorted(_SOURCES)}")
        if rec.status not in _STATUSES:
            raise ValueError(f"status {rec.status!r} must be one of {sorted(_STATUSES)}")
        if rec.window_kind not in _WINDOW_KINDS:
            raise ValueError(
                f"window_kind {rec.window_kind!r} must be one of {sorted(_WINDOW_KINDS)}"
            )
        if rec.interval not in INTERVAL_SECONDS:
            raise ValueError(f"interval {rec.interval!r} must be one of {sorted(INTERVAL_SECONDS)}")
        start = _ts_to_seconds(rec.window_start, "window_start")
        end = _ts_to_seconds(rec.window_end, "window_end")
        if end <= start:
            raise ValueError(f"window_end {rec.window_end} must be after window_start")
        plan_json = _check_json(rec.plan_json, "plan_json")
        params_json = _check_json(rec.params_json, "params_json")
        costs_json = _check_json(rec.costs_json, "costs_json")
        metrics_from_json(rec.metrics_json)  # validates: object of numbers/nulls
        if isinstance(rec.n_trades, bool) or not isinstance(rec.n_trades, int | np.integer):
            raise ValueError(f"n_trades must be an int, got {rec.n_trades!r}")
        if rec.n_trades < 0:
            raise ValueError(f"n_trades must be >= 0, got {rec.n_trades}")
        sharpe = _finite_or_none(rec.sharpe_per_bar, "sharpe_per_bar")
        n_returns, blob = _encode_returns(rec.returns)

        cur = self._conn.execute(
            "INSERT INTO trials (created_at, run_id, source, status, plan_hash, plan_json,"
            " params_json, symbol, interval, window_kind, window_start, window_end,"
            " metrics_json, sharpe_per_bar, n_trades, error, n_returns, returns_blob,"
            " costs_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now_iso(),
                rec.run_id,
                rec.source,
                rec.status,
                rec.plan_hash,
                plan_json,
                params_json,
                rec.symbol,
                rec.interval,
                rec.window_kind,
                start,
                end,
                rec.metrics_json,
                sharpe,
                int(rec.n_trades),
                rec.error,
                n_returns,
                blob,
                costs_json,
            ),
        )
        trial_id = cur.lastrowid
        if trial_id is None:  # pragma: no cover - sqlite always sets it for INSERT
            raise RuntimeError("sqlite did not return a row id")
        return int(trial_id)

    def get(self, trial_id: int, *, include_returns: bool = False) -> TrialRecord:
        """The trial with ``trial_id``; ``ValueError`` naming the id if it does not exist."""
        cols = _COLUMNS + (", returns_blob" if include_returns else ", NULL AS returns_blob")
        row = self._conn.execute(
            f"SELECT {cols} FROM trials WHERE id = ?", (int(trial_id),)
        ).fetchone()
        if row is None:
            raise ValueError(f"no trial with id {trial_id} in the ledger")
        return _row_to_record(row, include_returns=include_returns)

    def list(
        self,
        *,
        run_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_returns: bool = False,
    ) -> builtins.list[TrialRecord]:
        """Trials newest first, optionally for one run."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"limit must be an int >= 0, got {limit!r}")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError(f"offset must be an int >= 0, got {offset!r}")
        cols = _COLUMNS + (", returns_blob" if include_returns else ", NULL AS returns_blob")
        where, args = ("WHERE run_id = ?", [run_id]) if run_id is not None else ("", [])
        rows = self._conn.execute(
            f"SELECT {cols} FROM trials {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
        return [_row_to_record(r, include_returns=include_returns) for r in rows]

    def count(self, run_id: str | None = None, *, status: TrialStatus | None = None) -> int:
        """Number of trials, optionally filtered by run and status. DSR's N is ``count(run_id)``."""
        if status is not None and status not in _STATUSES:
            raise ValueError(f"status {status!r} must be one of {sorted(_STATUSES)}")
        clauses: list[str] = []
        args: list[str] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            args.append(run_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return int(self._conn.execute(f"SELECT COUNT(*) FROM trials {where}", args).fetchone()[0])

    def runs(self) -> builtins.list[dict[str, Any]]:
        """One dict per run: ``run_id``, ``n_trials``, ``first_created_at``, ``last_created_at``
        (pd.Timestamp UTC), oldest run first."""
        rows = self._conn.execute(
            "SELECT run_id, COUNT(*) AS n, MIN(created_at) AS first, MAX(created_at) AS last"
            " FROM trials GROUP BY run_id ORDER BY MIN(id)"
        ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "n_trials": int(r["n"]),
                "first_created_at": _iso_to_ts(r["first"]),
                "last_created_at": _iso_to_ts(r["last"]),
            }
            for r in rows
        ]

    def sharpe_variance(self, run_id: str) -> float:
        """Sample variance (ddof=1) of ``sharpe_per_bar`` over the run's ok trials with a finite
        value. NaN with fewer than two such trials. (DSR's V[SR_n].)"""
        rows = self._conn.execute(
            "SELECT sharpe_per_bar FROM trials"
            " WHERE run_id = ? AND status = 'ok' AND sharpe_per_bar IS NOT NULL",
            (run_id,),
        ).fetchall()
        values = np.array([r[0] for r in rows], dtype=np.float64)
        values = values[np.isfinite(values)]
        if len(values) < 2:
            return math.nan
        return float(np.var(values, ddof=1))

    # ------------------------------------------------------------------ selection (DSR)

    @staticmethod
    def _check_sources(sources: Iterable[str]) -> builtins.list[str]:
        out = sorted(set(sources))
        if not out:
            raise ValueError("sources must name at least one trial source")
        bad = [s for s in out if s not in _SOURCES]
        if bad:
            raise ValueError(f"sources {bad} must be among {sorted(_SOURCES)}")
        return out

    def count_distinct_plans(
        self,
        run_id: str,
        *,
        sources: Iterable[str] = SELECTION_SOURCES,
        interval: str | None = None,
    ) -> int:
        """Distinct ``plan_hash`` values in ``run_id`` among trials whose source is in
        ``sources`` (ok **and** error trials: a configuration that crashed was still tried).

        This is the DSR's multiple-testing ``N``: the number of distinct configurations tried.
        Re-running an identical plan does not inflate it. ``interval`` optionally restricts the
        count to one bar size.
        """
        srcs = self._check_sources(sources)
        sql = (
            "SELECT COUNT(DISTINCT plan_hash) FROM trials"
            f" WHERE run_id = ? AND source IN ({', '.join('?' * len(srcs))})"
        )
        args: builtins.list[str] = [run_id, *srcs]
        if interval is not None:
            sql += " AND interval = ?"
            args.append(interval)
        return int(self._conn.execute(sql, args).fetchone()[0])

    def latest_sharpes_per_plan(
        self,
        run_id: str,
        *,
        sources: Iterable[str] = SELECTION_SOURCES,
        interval: str | None = None,
        window_start: pd.Timestamp | None = None,
        window_end: pd.Timestamp | None = None,
        costs_json: str | None = None,
    ) -> dict[str, float]:
        """``plan_hash -> sharpe_per_bar`` of the **latest** ok trial with a finite Sharpe for
        each distinct plan in ``run_id`` whose source is in ``sources``.

        ``walk_forward_test`` rows are always excluded. ``interval``, the exact window
        ``[window_start, window_end)`` (pass both or neither), and ``costs_json`` (exact string,
        see :func:`costs_to_json`) optionally restrict the trials; ``None`` = no filter.
        """
        srcs = self._check_sources(sources)
        sql = (
            "SELECT plan_hash, sharpe_per_bar FROM trials"
            f" WHERE run_id = ? AND status = 'ok' AND sharpe_per_bar IS NOT NULL"
            f" AND source IN ({', '.join('?' * len(srcs))})"
            f" AND window_kind NOT IN ({', '.join('?' * len(_VARIANCE_EXCLUDED_KINDS))})"
        )
        args: builtins.list[str | int] = [run_id, *srcs, *_VARIANCE_EXCLUDED_KINDS]
        if interval is not None:
            sql += " AND interval = ?"
            args.append(interval)
        if (window_start is None) != (window_end is None):
            raise ValueError("pass window_start and window_end together (or neither)")
        if window_start is not None and window_end is not None:
            sql += " AND window_start = ? AND window_end = ?"
            args.append(_ts_to_seconds(window_start, "window_start"))
            args.append(_ts_to_seconds(window_end, "window_end"))
        if costs_json is not None:
            sql += " AND costs_json = ?"
            args.append(_check_json(costs_json, "costs_json"))
        sql += " ORDER BY id"
        latest: dict[str, float] = {}
        for row in self._conn.execute(sql, args).fetchall():
            value = float(row[1])
            if math.isfinite(value):
                latest[row[0]] = value  # ascending id, so the newest row wins
        return latest

    def selection_sharpe_variance(
        self,
        run_id: str,
        *,
        sources: Iterable[str] = SELECTION_SOURCES,
        interval: str | None = None,
        window_start: pd.Timestamp | None = None,
        window_end: pd.Timestamp | None = None,
        costs_json: str | None = None,
    ) -> float:
        """Sample variance (ddof=1) of :meth:`latest_sharpes_per_plan` (one value per distinct
        plan, so repeated runs of one plan do not shrink it); NaN with fewer than two plans.

        DSR callers pass the interval, the window, and the costs of the Sharpe being deflated
        so only commensurable Sharpes are combined (``walk_forward_test`` rows never are).
        """
        latest = self.latest_sharpes_per_plan(
            run_id,
            sources=sources,
            interval=interval,
            window_start=window_start,
            window_end=window_end,
            costs_json=costs_json,
        )
        values = np.array(list(latest.values()), dtype=np.float64)
        if len(values) < 2:
            return math.nan
        return float(np.var(values, ddof=1))

    def returns_matrix(
        self, run_id: str, *, window_start: pd.Timestamp, window_end: pd.Timestamp
    ) -> tuple[np.ndarray, builtins.list[int]]:
        """``T x N`` per-bar returns of the run's ok trials on exactly ``[window_start,
        window_end)`` (for PBO/CSCV), columns in id order, plus the column ids.

        Only trials with stored returns count. If their lengths differ, the most common length
        is kept (ties: the longer); the returned ids say exactly which trials were used. With no
        trials the matrix has shape ``(0, 0)``.
        """
        start = _ts_to_seconds(window_start, "window_start")
        end = _ts_to_seconds(window_end, "window_end")
        rows = self._conn.execute(
            "SELECT id, n_returns, returns_blob FROM trials"
            " WHERE run_id = ? AND status = 'ok' AND window_start = ? AND window_end = ?"
            " AND returns_blob IS NOT NULL ORDER BY id",
            (run_id, start, end),
        ).fetchall()
        series: list[tuple[int, np.ndarray]] = []
        for r in rows:
            arr = _decode_returns(r["n_returns"], r["returns_blob"])
            if arr is not None:
                series.append((int(r["id"]), arr))
        if not series:
            return np.empty((0, 0), dtype=np.float64), []
        counts = Counter(len(a) for _, a in series)
        length = max(counts, key=lambda n: (counts[n], n))
        kept = [(i, a) for i, a in series if len(a) == length]
        matrix = np.column_stack([a for _, a in kept]).astype(np.float64, copy=False)
        return matrix, [i for i, _ in kept]

    # ------------------------------------------------------------------ holdout budget

    def holdout_checks_used(self, run_id: str) -> int:
        """Holdout checks already spent by ``run_id``."""
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM holdout_checks WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )

    def record_holdout_check(self, run_id: str, plan_hash: str, *, budget: int) -> int:
        """Spend one holdout check for ``run_id`` and return the new used count.

        Runs under ``BEGIN IMMEDIATE`` (a write lock taken before reading the count), so
        concurrent connections cannot both spend the last check. Raises
        :class:`HoldoutBudgetExceeded` when ``used >= budget``; nothing is recorded then.
        """
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError(f"budget must be an int >= 0, got {budget!r}")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"run_id must be a non-empty string, got {run_id!r}")
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            used = int(
                conn.execute(
                    "SELECT COUNT(*) FROM holdout_checks WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            if used >= budget:
                raise HoldoutBudgetExceededError(
                    f"run {run_id!r} has used {used} of {budget} holdout checks; budget exhausted"
                )
            conn.execute(
                "INSERT INTO holdout_checks (created_at, run_id, plan_hash, budget)"
                " VALUES (?, ?, ?, ?)",
                (_now_iso(), run_id, plan_hash, budget),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return used + 1
