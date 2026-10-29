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

Risk-overlay research runs (Phase 15, frozen ``"question": "risk_overlay"``; owner decision
2026-10-29, "count in N, not V[SR]"): their trials COUNT in the research-wide trial count N
(:meth:`Ledger.count_distinct_plans_research`, and so in the effective-N plan set) exactly like
any other trial, so a timing rule explored as an overlay and later ported to an edge plan was
still tried; they are EXCLUDED from every research-wide V[SR] / Sharpe pool
(:meth:`Ledger.latest_sharpes_per_plan_research`, :meth:`Ledger.research_design_sharpes`),
because a buy-and-hold overlay's Sharpe is not a selection Sharpe of the edge search. They also
count toward the overlay question's own N_overlay.

Phase 8 research runs judge candidates with a *global* ``N``:
:meth:`Ledger.count_distinct_plans_research` counts distinct plans across EVERY run of the
ledger (research runs and the web UI's / ad-hoc API runs; all symbols, all intervals), so
starting a new run, or trying a configuration in the Strategy Tester first, never resets the
multiple-testing bar. Its ``V[SR]`` is the largest of the run's variance (above),
:meth:`Ledger.selection_sharpe_variance_research` (the same commensurability filters: interval,
window, costs, no ``walk_forward_test``, applied to the latest Sharpe per distinct plan across
EVERY research run, holdout rows excluded), and a cross-interval pool built from
:meth:`Ledger.research_design_sharpes` (``lab.research.criteria``), so a fresh run that submits
only a winner found elsewhere, or on another interval, is deflated with the variance of the
search that found it.

Thread safety: a ``Ledger`` wraps one ``sqlite3`` connection created with the default
``check_same_thread=True``, so it must be used on the thread that opened it. Servers open one
``Ledger`` per request (WAL + ``busy_timeout`` make concurrent connections safe).

The ledger also enforces the locked-holdout budget: :meth:`Ledger.record_holdout_check` runs
inside ``BEGIN IMMEDIATE`` so two processes cannot both spend the last check, and, given a
``logic_hash`` (:func:`lab.research.logic.logic_hash`) and the plan's behaviour family
(:mod:`lab.research.family`: logic and plan hashes), refuses a hypothesis any of whose family
members was already holdout-checked in ANY run (a hypothesis gets one holdout check, ever).

Per-run hash chain (Phase 13): every trial has a ``trial_chain`` row whose ``hash`` is
:func:`trial_hash` of the previous trial's hash in the same run (``None`` for the run's first
trial) and the trial's stored content (every ``trials`` column; ``returns_blob`` by its sha256).
:meth:`Ledger.record` inserts the trial and its chain link in ONE ``BEGIN IMMEDIATE``
transaction, reading the run's head inside the write lock, so two processes cannot fork a
chain. :meth:`Ledger.verify_chain` recomputes every link in id order and names the first trial
whose content, link, or chain row does not match, and :meth:`Ledger.chain_head` is the value a
journal can quote. ``trial_chain`` is append-only like the other tables. Trials written before
the chain existed (or by an older process that does not chain) are chained lazily, in id order
per run, when a ``Ledger`` opens and before each ``record`` of that run (``backfilled = 1``).
A backfilled link is tamper evidence only from the backfill time onward: it proves nothing
about what the row held before it was chained. Only a run's unchained *tail* (ids above its
last chained trial) is ever backfilled, so a chain row missing in the middle of a chain stays
a detected gap instead of being silently re-created. The chain is tamper *evidence*, not
prevention: someone who can drop the triggers can also rewrite every later link, so quote
:meth:`Ledger.chain_head` in the journal to pin the chain's end.

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
import hashlib
import json
import math
import sqlite3
import zlib
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
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


class HoldoutAlreadyCheckedError(RuntimeError):
    """Raised when a plan's logic was already holdout-checked (in any run). Nothing recorded."""


# Sources that are *selection* trials: configurations a person, the API, Optuna, or the ±20%
# neighbourhood grid tried while looking for a strategy. DSR's N counts distinct plans among
# these. Adversarial variants and benchmark/null summaries are recorded but are not candidates.
SELECTION_SOURCES: tuple[str, ...] = ("manual", "api", "optuna", "neighbourhood")
# Risk-overlay research runs (PLAN.md Phase 15): their frozen config has "question":
# "risk_overlay". Owner decision 2026-10-29 ("count in N, not V[SR]"): their trials COUNT in the
# research-wide trial count N and the effective-N plan set exactly like any other trial, but are
# EXCLUDED from every research-wide V[SR] / Sharpe pool (a buy-and-hold overlay's Sharpe is not
# a selection Sharpe of the edge search). They also count toward N_overlay
# (lab.research.overlay_criteria). Behaviour families and holdout checks span both questions
# (a hypothesis gets one holdout check, whatever it was asked as). With no overlay run in the
# ledger this subquery is empty and every pool is unchanged.
_OVERLAY_RUNS_SQL = (
    "SELECT run_id FROM research_runs WHERE json_valid(config_json)"
    " AND json_extract(config_json, '$.question') = 'risk_overlay'"
)


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
    (
        3,
        "research_runs",
        # Phase 8: each research run snapshots its success criteria + budget when it starts
        # (so the bar cannot move mid-run), and stores the evidence (report JSON) that the
        # criteria are judged on. Both append-only.
        (
            """
            CREATE TABLE research_runs (
                run_id          TEXT PRIMARY KEY,
                created_at      TEXT NOT NULL,
                label           TEXT NOT NULL,
                config_json     TEXT NOT NULL,
                config_sha256   TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE research_evidence (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT NOT NULL,
                run_id      TEXT NOT NULL REFERENCES research_runs(run_id),
                plan_hash   TEXT NOT NULL,
                kind        TEXT NOT NULL,
                payload     TEXT NOT NULL
            )
            """,
            "CREATE INDEX research_evidence_plan ON research_evidence (run_id, plan_hash, kind)",
            """
            CREATE TRIGGER research_runs_no_update BEFORE UPDATE ON research_runs
            BEGIN SELECT RAISE(ABORT, 'research runs are append-only'); END
            """,
            """
            CREATE TRIGGER research_runs_no_delete BEFORE DELETE ON research_runs
            BEGIN SELECT RAISE(ABORT, 'research runs are append-only'); END
            """,
            """
            CREATE TRIGGER research_evidence_no_update BEFORE UPDATE ON research_evidence
            BEGIN SELECT RAISE(ABORT, 'research evidence is append-only'); END
            """,
            """
            CREATE TRIGGER research_evidence_no_delete BEFORE DELETE ON research_evidence
            BEGIN SELECT RAISE(ABORT, 'research evidence is append-only'); END
            """,
        ),
    ),
    (
        4,
        "holdout_logic_hash",
        # Phase 8: the holdout is spent per hypothesis *logic* (lab.research.logic), across all
        # runs, so renaming a plan or starting a new run cannot buy a second look.
        (
            "ALTER TABLE holdout_checks ADD COLUMN logic_hash TEXT",
            "CREATE INDEX holdout_checks_logic ON holdout_checks (logic_hash)",
        ),
    ),
    (
        5,
        "trial_hash_chain",
        # Phase 13: per-run hash chain over trials (tamper evidence). Rows already in the
        # ledger are chained by ``Ledger._ensure_chain`` after migrating (``backfilled = 1``).
        (
            """
            CREATE TABLE trial_chain (
                trial_id    INTEGER PRIMARY KEY REFERENCES trials(id),
                run_id      TEXT    NOT NULL,
                prev_hash   TEXT,
                hash        TEXT    NOT NULL,
                backfilled  INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX trial_chain_run ON trial_chain (run_id, trial_id)",
            """
            CREATE TRIGGER trial_chain_no_update BEFORE UPDATE ON trial_chain
            BEGIN SELECT RAISE(ABORT, 'trial hash chain is append-only'); END
            """,
            """
            CREATE TRIGGER trial_chain_no_delete BEFORE DELETE ON trial_chain
            BEGIN SELECT RAISE(ABORT, 'trial hash chain is append-only'); END
            """,
        ),
    ),
)

# Kinds of research evidence (report JSON) stored per (run_id, plan_hash).
EVIDENCE_KINDS: tuple[str, ...] = (
    "backtest",
    "adversarial",
    "sweep",
    "walk_forward",
    "crosscheck",
    "holdout",
)

# Window kinds whose Sharpe never enters the DSR variance (out-of-sample fold results).
_VARIANCE_EXCLUDED_KINDS: tuple[str, ...] = ("walk_forward_test",)

# Window kinds a plan's returns may come from for the effective trial count, best first (the
# design window is the common, longest one; walk-forward train windows are longer than test
# folds). ``holdout`` is deliberately absent: the effective count never reads the holdout.
_RETURN_WINDOW_PREFERENCE: tuple[str, ...] = (
    "design",
    "walk_forward_train",
    "walk_forward_test",
    "custom",
)


# --------------------------------------------------------------------------- hash chain

# Every ``trials`` column that a chain link hashes (``returns_blob`` is hashed via its sha256).
_HASHED_COLUMNS: tuple[str, ...] = (
    "id",
    "created_at",
    "run_id",
    "source",
    "status",
    "plan_hash",
    "plan_json",
    "params_json",
    "symbol",
    "interval",
    "window_kind",
    "window_start",
    "window_end",
    "metrics_json",
    "sharpe_per_bar",
    "n_trades",
    "error",
    "n_returns",
    "returns_blob",
    "costs_json",
)


def _canonical_value(value: Any) -> Any:
    """A JSON-encodable stand-in for one stored column value (bytes become their sha256)."""
    if isinstance(value, bytes | bytearray | memoryview):
        return {"sha256": hashlib.sha256(bytes(value)).hexdigest()}
    return value


def trial_hash(prev_hash: str | None, row: Mapping[str, Any]) -> str:
    """sha256 hex of ``(prev_hash or "")`` + the canonical JSON of the trial's stored content.

    ``row`` is a ``trials`` row (``sqlite3.Row`` or a dict of every column, as stored). The
    content is ``{column: value}`` over every ``trials`` column, with ``returns_blob`` replaced
    by ``returns_sha256`` (the sha256 hex of the stored blob, or null), encoded with
    ``sort_keys=True`` and compact separators. Raises ``ValueError`` naming a missing column.
    """
    if prev_hash is not None and not isinstance(prev_hash, str):
        raise ValueError(f"prev_hash must be a str or None, got {type(prev_hash).__name__}")
    keys = set(row.keys())
    missing = [c for c in _HASHED_COLUMNS if c not in keys]
    if missing:
        raise ValueError(f"row is missing trials column(s) {missing}")
    content: dict[str, Any] = {}
    for col in _HASHED_COLUMNS:
        value = row[col]
        if col == "returns_blob":
            content["returns_sha256"] = (
                None if value is None else hashlib.sha256(bytes(value)).hexdigest()
            )
        else:
            content[col] = _canonical_value(value)
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(((prev_hash or "") + text).encode("utf-8")).hexdigest()


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
        # The resolved database file (None for ":memory:"); lets callers key per-ledger caches.
        self.path: Path | None
        if str(path) == ":memory:":
            target = ":memory:"
            self.path = None
        else:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            target = str(p)
            self.path = p.resolve()
        # Autocommit mode; every write manages its own explicit transaction.
        self._conn = sqlite3.connect(target, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.schema_version = self._migrate()
        self._ensure_chain()

    @classmethod
    def open_readonly(cls, path: Path | str, *, min_schema_version: int = 1) -> Ledger:
        """Open an existing ledger strictly read-only (SQLite ``mode=ro``): no migrations, no
        hash-chain backfill, no journal-mode change, never creates the file. Any write raises
        ``sqlite3.OperationalError``. ``ValueError`` if the file is missing or its schema is
        older than ``min_schema_version``."""
        p = Path(path)
        if not p.is_file():
            raise ValueError(f"ledger {p} not found")
        self = cls.__new__(cls)
        self.path = p.resolve()
        self._conn = sqlite3.connect(
            f"{self.path.as_uri()}?mode=ro", uri=True, timeout=30.0, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        try:
            row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        except sqlite3.DatabaseError as exc:
            self._conn.close()
            text = str(exc).lower()
            if "readonly" in text or "unable to open" in text:
                # A WAL ledger read without its -wal/-shm files needs to create them.
                raise ValueError(
                    f"cannot open ledger {p} read-only: SQLite needs a writable folder for its "
                    "WAL files (-wal/-shm); copy the ledger to a writable folder first"
                ) from exc
            raise ValueError(f"{p} is not a trial ledger: {exc}") from exc
        version = int(row[0]) if row and row[0] is not None else 0
        if version < min_schema_version:
            self._conn.close()
            raise ValueError(
                f"ledger {p} is at schema {version}, needs >= {min_schema_version}; open it once "
                "with Ledger(path) (the lab does on its next run) to migrate"
            )
        self.schema_version = version
        return self

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

    def _ensure_chain(self) -> None:
        """Backfill every run's unchained tail (``trial_chain`` comes from migration 5), in id
        order, inside one ``BEGIN IMMEDIATE`` (see the module docstring). A ledger with nothing
        to chain is checked with a plain read and skips this lock (``_migrate`` still takes a
        brief one per migration; :meth:`open_readonly` takes none)."""
        conn = self._conn
        unchained = (
            "SELECT DISTINCT t.run_id FROM trials t WHERE t.id > ("
            " SELECT COALESCE(MAX(c.trial_id), 0) FROM trial_chain c"
            " WHERE c.run_id = t.run_id)"
        )
        if not conn.execute(unchained + " LIMIT 1").fetchone():
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read inside the write lock: another process may have chained them meanwhile.
            runs = [r[0] for r in conn.execute(unchained).fetchall()]
            for run_id in runs:
                self._chain_tail(run_id)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _chain_tail(self, run_id: str) -> str | None:
        """Chain ``run_id``'s trials above its last chained trial (``backfilled = 1``) and
        return the run's head. Caller must hold a ``BEGIN IMMEDIATE`` write lock."""
        conn = self._conn
        last = conn.execute(
            "SELECT trial_id, hash FROM trial_chain WHERE run_id = ?"
            " ORDER BY trial_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        head: str | None = None if last is None else str(last["hash"])
        after = 0 if last is None else int(last["trial_id"])
        rows = conn.execute(
            "SELECT * FROM trials WHERE run_id = ? AND id > ? ORDER BY id", (run_id, after)
        ).fetchall()
        for row in rows:
            digest = trial_hash(head, row)
            conn.execute(
                "INSERT INTO trial_chain (trial_id, run_id, prev_hash, hash, backfilled)"
                " VALUES (?, ?, ?, ?, 1)",
                (int(row["id"]), run_id, head, digest),
            )
            head = digest
        return head

    # ------------------------------------------------------------------ trials

    def record(self, rec: TrialRecord) -> int:
        """Insert ``rec`` as a new row and return its id. ``created_at`` is now (UTC).

        The trial and its ``trial_chain`` link are written in one ``BEGIN IMMEDIATE``
        transaction (any unchained tail of the run is backfilled first)."""
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

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # The run's head is read inside the write lock, so no other connection can append
            # to this run between reading it and linking to it (no forked chains).
            head = self._chain_tail(rec.run_id)
            trial_id = self._insert_trial(
                rec, start, end, plan_json, params_json, costs_json, sharpe, n_returns, blob
            )
            row = conn.execute("SELECT * FROM trials WHERE id = ?", (trial_id,)).fetchone()
            conn.execute(
                "INSERT INTO trial_chain (trial_id, run_id, prev_hash, hash, backfilled)"
                " VALUES (?, ?, ?, ?, 0)",
                (trial_id, rec.run_id, head, trial_hash(head, row)),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return trial_id

    def _insert_trial(
        self,
        rec: TrialRecord,
        start: int,
        end: int,
        plan_json: str,
        params_json: str,
        costs_json: str,
        sharpe: float | None,
        n_returns: int | None,
        blob: bytes | None,
    ) -> int:
        """The raw ``INSERT INTO trials`` of :meth:`record` (validated values; caller holds the
        write lock and writes the chain link)."""
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

    # ------------------------------------------------------------------ hash chain (Phase 13)

    @staticmethod
    def _check_run_id_arg(run_id: object) -> str:
        if not isinstance(run_id, str):
            raise ValueError(f"run_id must be a str, got {type(run_id).__name__}")
        return run_id

    def chain_head(self, run_id: str) -> str | None:
        """The stored hash of ``run_id``'s last chained trial (highest trial id), or None when
        the run has no chained trials. Not verified; see :meth:`verify_chain`."""
        run_id = self._check_run_id_arg(run_id)
        row = self._conn.execute(
            "SELECT hash FROM trial_chain WHERE run_id = ? ORDER BY trial_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else str(row["hash"])

    def verify_chain(self, run_id: str) -> dict[str, Any]:
        """Recompute ``run_id``'s hash chain from its ``trials`` rows, in id order.

        Returns ``{"run_id", "ok", "n_trials", "head", "first_bad_trial_id", "error"}``:
        ``n_trials`` counts the run's trial rows, ``head`` is :meth:`chain_head` (the stored
        head, meaningful as evidence only when ``ok``), and on failure ``first_bad_trial_id``
        is the lowest trial id whose link does not hold (content changed, chain row missing,
        wrong ``prev_hash``, or a chain row whose trial is gone or belongs to another run) with
        a one-line ``error``. Read-only; never raises for a tampered chain. Raises
        ``ValueError`` only when ``run_id`` is not a str.
        """
        run_id = self._check_run_id_arg(run_id)
        conn = self._conn
        trials = conn.execute(
            "SELECT * FROM trials WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        links = {
            int(r["trial_id"]): r
            for r in conn.execute(
                "SELECT trial_id, run_id, prev_hash, hash FROM trial_chain WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        }
        failures: builtins.list[tuple[int, str]] = []
        prev: str | None = None
        for row in trials:
            tid = int(row["id"])
            link = links.pop(tid, None)
            if link is None:
                failures.append((tid, f"trial {tid} has no chain row"))
                break
            if link["prev_hash"] != prev:
                failures.append(
                    (tid, f"trial {tid}'s chain row does not link to the previous trial's hash")
                )
                break
            try:
                expected = trial_hash(prev, row)
            except (TypeError, ValueError) as exc:  # tampered column of an unhashable type
                failures.append((tid, f"trial {tid}'s content cannot be hashed: {exc}"))
                break
            if link["hash"] != expected:
                failures.append(
                    (tid, f"trial {tid}'s content does not match its chain hash (changed)")
                )
                break
            prev = str(link["hash"])
        # Chain rows of this run with no trial row in it (trial deleted, or moved to another run).
        trial_ids = {int(r["id"]) for r in trials}
        orphans = sorted(tid for tid in links if tid not in trial_ids)
        if orphans:
            failures.append(
                (orphans[0], f"chain row for trial {orphans[0]} has no trial row in this run")
            )
        first = min(failures, default=None)
        return {
            "run_id": run_id,
            "ok": first is None,
            "n_trials": len(trials),
            "head": self.chain_head(run_id),
            "first_bad_trial_id": None if first is None else first[0],
            "error": None if first is None else first[1],
        }

    def get(self, trial_id: int, *, include_returns: bool = False) -> TrialRecord:
        """The trial with ``trial_id``; ``ValueError`` naming the id if it does not exist."""
        cols = _COLUMNS + (", returns_blob" if include_returns else ", NULL AS returns_blob")
        row = self._conn.execute(
            f"SELECT {cols} FROM trials WHERE id = ?", (int(trial_id),)
        ).fetchone()
        if row is None:
            raise ValueError(f"no trial with id {trial_id} in the ledger")
        return _row_to_record(row, include_returns=include_returns)

    @staticmethod
    def _check_kinds(kinds: Iterable[str]) -> builtins.list[str]:
        out = sorted(set(kinds))
        bad = [k for k in out if k not in _WINDOW_KINDS]
        if bad:
            raise ValueError(f"window kinds {bad} must be among {sorted(_WINDOW_KINDS)}")
        return out

    def list(
        self,
        *,
        run_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_returns: bool = False,
        exclude_window_kinds: Iterable[str] = (),
    ) -> builtins.list[TrialRecord]:
        """Trials newest first, optionally for one run and without rows whose ``window_kind``
        is in ``exclude_window_kinds`` (e.g. ``("holdout",)`` for anything an agent or the UI
        may see)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"limit must be an int >= 0, got {limit!r}")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError(f"offset must be an int >= 0, got {offset!r}")
        cols = _COLUMNS + (", returns_blob" if include_returns else ", NULL AS returns_blob")
        where, args = self._where(run_id, None, exclude_window_kinds)
        rows = self._conn.execute(
            f"SELECT {cols} FROM trials {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
        return [_row_to_record(r, include_returns=include_returns) for r in rows]

    def _where(
        self,
        run_id: str | None,
        status: str | None,
        exclude_window_kinds: Iterable[str],
    ) -> tuple[str, builtins.list[str]]:
        clauses: builtins.list[str] = []
        args: builtins.list[str] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            args.append(run_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        kinds = self._check_kinds(exclude_window_kinds)
        if kinds:
            clauses.append(f"window_kind NOT IN ({', '.join('?' * len(kinds))})")
            args.extend(kinds)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), args

    def count(
        self,
        run_id: str | None = None,
        *,
        status: TrialStatus | None = None,
        exclude_window_kinds: Iterable[str] = (),
    ) -> int:
        """Number of trials, optionally filtered by run and status and without rows whose
        ``window_kind`` is in ``exclude_window_kinds`` (the raw row count; DSR's N is
        :meth:`count_distinct_plans`)."""
        if status is not None and status not in _STATUSES:
            raise ValueError(f"status {status!r} must be one of {sorted(_STATUSES)}")
        where, args = self._where(run_id, status, exclude_window_kinds)
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

    def count_distinct_plans_research(self, *, sources: Iterable[str] = SELECTION_SOURCES) -> int:
        """Distinct ``plan_hash`` values among trials (ok and error) whose source is in
        ``sources`` in EVERY run of the ledger: research runs AND non-research runs (the
        Strategy Tester's ``"ui"`` run, ad-hoc ``POST /lab/backtest`` runs), all symbols, all
        intervals, all windows.

        This is the Phase 8 DSR ``N``: every configuration ever tried in this lab, so a new run,
        another interval, or a configuration first tried in the web UI never resets the
        multiple-testing bar. (The name is kept for ``agents/``; before round 4 it counted
        research runs only.) Trials of risk-overlay research runs (Phase 15) count here like any
        other trial (owner decision 2026-10-29: "count in N, not V[SR]"); they stay out of the
        V[SR] pools.
        """
        srcs = self._check_sources(sources)
        sql = (
            "SELECT COUNT(DISTINCT plan_hash) FROM trials"
            f" WHERE source IN ({', '.join('?' * len(srcs))})"
        )
        return int(self._conn.execute(sql, srcs).fetchone()[0])

    def _latest_sharpes(
        self,
        run_id: str | None,
        *,
        sources: Iterable[str],
        interval: str | None,
        window_start: pd.Timestamp | None,
        window_end: pd.Timestamp | None,
        costs_json: str | None,
    ) -> dict[str, float]:
        """Shared body of :meth:`latest_sharpes_per_plan` (``run_id`` given) and
        :meth:`latest_sharpes_per_plan_research` (``run_id`` None: every research run, and
        ``holdout`` rows are excluded as well)."""
        srcs = self._check_sources(sources)
        excluded = _VARIANCE_EXCLUDED_KINDS + (() if run_id is not None else ("holdout",))
        sql = (
            "SELECT plan_hash, sharpe_per_bar FROM trials"
            " WHERE status = 'ok' AND sharpe_per_bar IS NOT NULL"
            f" AND source IN ({', '.join('?' * len(srcs))})"
            f" AND window_kind NOT IN ({', '.join('?' * len(excluded))})"
        )
        args: builtins.list[str | int] = [*srcs, *excluded]
        if run_id is not None:
            sql += " AND run_id = ?"
            args.append(run_id)
        else:
            sql += " AND run_id IN (SELECT run_id FROM research_runs)"
            sql += f" AND run_id NOT IN ({_OVERLAY_RUNS_SQL})"
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
        return self._latest_sharpes(
            str(run_id),
            sources=sources,
            interval=interval,
            window_start=window_start,
            window_end=window_end,
            costs_json=costs_json,
        )

    def latest_sharpes_per_plan_research(
        self,
        *,
        sources: Iterable[str] = SELECTION_SOURCES,
        interval: str | None = None,
        window_start: pd.Timestamp | None = None,
        window_end: pd.Timestamp | None = None,
        costs_json: str | None = None,
    ) -> dict[str, float]:
        """Like :meth:`latest_sharpes_per_plan` but over EVERY registered research run (the
        latest Sharpe per distinct plan wherever it ran), without ``walk_forward_test`` and
        ``holdout`` rows. Trials of non-research runs (web UI, ad-hoc API) and of risk-overlay
        research runs are not included."""
        return self._latest_sharpes(
            None,
            sources=sources,
            interval=interval,
            window_start=window_start,
            window_end=window_end,
            costs_json=costs_json,
        )

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
        return _variance(latest)

    def selection_sharpe_variance_research(
        self,
        *,
        sources: Iterable[str] = SELECTION_SOURCES,
        interval: str | None = None,
        window_start: pd.Timestamp | None = None,
        window_end: pd.Timestamp | None = None,
        costs_json: str | None = None,
    ) -> float:
        """Sample variance (ddof=1) of :meth:`latest_sharpes_per_plan_research`: the Phase 8
        ``V[SR]`` over every research run's commensurable Sharpes (same filters), so a fresh
        run holding only a winner cannot shrink the variance it is deflated with. NaN with
        fewer than two plans."""
        latest = self.latest_sharpes_per_plan_research(
            sources=sources,
            interval=interval,
            window_start=window_start,
            window_end=window_end,
            costs_json=costs_json,
        )
        return _variance(latest)

    def returns_matrix(
        self,
        run_id: str,
        *,
        window_start: pd.Timestamp,
        window_end: pd.Timestamp,
        sources: Iterable[str] | None = None,
        studies: Iterable[str] | None = None,
    ) -> tuple[np.ndarray, builtins.list[int]]:
        """``T x N`` per-bar returns of the run's ok trials on exactly ``[window_start,
        window_end)`` (for PBO/CSCV), columns in id order, plus the column ids.

        ``sources`` optionally restricts the trial sources; ``studies`` optionally restricts to
        Optuna trials whose ``params_json["_study"]["study"]`` is one of the given study names
        (an empty ``studies`` matches nothing). Only trials with stored returns count. If their
        lengths differ, the most common length is kept (ties: the longer); the returned ids say
        exactly which trials were used. With no trials the matrix has shape ``(0, 0)``.
        """
        start = _ts_to_seconds(window_start, "window_start")
        end = _ts_to_seconds(window_end, "window_end")
        sql = (
            "SELECT id, n_returns, returns_blob FROM trials"
            " WHERE run_id = ? AND status = 'ok' AND window_start = ? AND window_end = ?"
            " AND returns_blob IS NOT NULL"
        )
        args: builtins.list[str | int] = [run_id, start, end]
        if sources is not None:
            srcs = self._check_sources(sources)
            sql += f" AND source IN ({', '.join('?' * len(srcs))})"
            args.extend(srcs)
        if studies is not None:
            names = sorted({str(n) for n in studies})
            if not names:
                return np.empty((0, 0), dtype=np.float64), []
            marks = ", ".join("?" * len(names))
            sql += (
                " AND json_valid(params_json)"
                f" AND json_extract(params_json, '$._study.study') IN ({marks})"
            )
            args.extend(names)
        rows = self._conn.execute(sql + " ORDER BY id", args).fetchall()
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

    # ------------------------------------------------------------------ research-wide reads

    def iter_research_window_trials(
        self,
        *,
        symbol: str,
        interval: str,
        window_start: pd.Timestamp,
        window_end: pd.Timestamp,
        sources: Iterable[str] = ("api",),
    ) -> Iterator[TrialRecord]:
        """Ok ``window_kind="design"`` trials with stored returns, whose source is in
        ``sources``, in ANY research run on exactly this symbol, interval, and window, oldest
        first, with returns. A generator: rows are decoded one at a time."""
        srcs = self._check_sources(sources)
        sql = (
            f"SELECT {_COLUMNS}, returns_blob FROM trials"
            f" WHERE source IN ({', '.join('?' * len(srcs))})"
            " AND window_kind = 'design' AND status = 'ok'"
            " AND returns_blob IS NOT NULL AND symbol = ? AND interval = ?"
            " AND window_start = ? AND window_end = ?"
            " AND run_id IN (SELECT run_id FROM research_runs) ORDER BY id"
        )
        args: builtins.list[str | int] = [
            *srcs,
            symbol,
            interval,
            _ts_to_seconds(window_start, "window_start"),
            _ts_to_seconds(window_end, "window_end"),
        ]
        for row in self._conn.execute(sql, args):
            yield _row_to_record(row, include_returns=True)

    def latest_research_base_trial(self, plan_hash: str) -> TrialRecord | None:
        """The newest submitted-plan base trial of ``plan_hash`` (source ``"api"``, design
        window, ok, returns stored) in any research run, with returns; None if there is none."""
        row = self._conn.execute(
            f"SELECT {_COLUMNS}, returns_blob FROM trials"
            " WHERE plan_hash = ? AND source = 'api' AND window_kind = 'design'"
            " AND status = 'ok' AND returns_blob IS NOT NULL"
            " AND run_id IN (SELECT run_id FROM research_runs) ORDER BY id DESC LIMIT 1",
            (plan_hash,),
        ).fetchone()
        return None if row is None else _row_to_record(row, include_returns=True)

    def research_plans(
        self,
        *,
        symbol: str | None = None,
        sources: Iterable[str] = SELECTION_SOURCES,
        exclude_window_kinds: Iterable[str] = ("holdout",),
        research_only: bool = True,
    ) -> dict[str, str]:
        """``plan_hash -> plan_json`` of every distinct plan tried (ok or error) in ANY research
        run (``research_only=False``: in ANY run, the web UI's included) by a trial whose source
        is in ``sources`` and whose window kind is not excluded (default: everything but the
        locked holdout), optionally for one symbol."""
        srcs = self._check_sources(sources)
        kinds = self._check_kinds(exclude_window_kinds)
        sql = (
            "SELECT plan_hash, MAX(plan_json) FROM trials"
            f" WHERE source IN ({', '.join('?' * len(srcs))})"
        )
        if research_only:
            sql += " AND run_id IN (SELECT run_id FROM research_runs)"
        args: builtins.list[str] = [*srcs]
        if kinds:
            sql += f" AND window_kind NOT IN ({', '.join('?' * len(kinds))})"
            args.extend(kinds)
        if symbol is not None:
            sql += " AND symbol = ?"
            args.append(symbol)
        rows = self._conn.execute(sql + " GROUP BY plan_hash", args).fetchall()
        return {r[0]: r[1] for r in rows}

    def first_plan_trials(
        self, *, sources: Iterable[str], overlay_runs: bool
    ) -> builtins.list[tuple[str, str, str]]:
        """``(plan_hash, plan_json, run_id)`` of the FIRST trial (lowest id) of every distinct plan
        whose source is in ``sources`` (ok or error, any window), in the risk-overlay research
        runs (``overlay_runs=True``) or in every other run of the ledger (``False``: edge
        research runs, the web UI's and ad-hoc runs), oldest first. Used by the overlay
        one-parameter-set rule (:func:`lab.research.overlay_criteria.check_overlay_untuned`)."""
        srcs = self._check_sources(sources)
        scope = "IN" if overlay_runs else "NOT IN"
        sql = (
            "SELECT plan_hash, MIN(id) AS first_id, plan_json, run_id FROM trials"
            f" WHERE source IN ({', '.join('?' * len(srcs))})"
            f" AND run_id {scope} ({_OVERLAY_RUNS_SQL})"
            " GROUP BY plan_hash ORDER BY first_id"
        )
        return [(r[0], r[2], r[3]) for r in self._conn.execute(sql, srcs).fetchall()]

    def research_study_spaces(
        self, *, symbol: str | None = None, research_only: bool = True
    ) -> builtins.list[tuple[str, str]]:
        """``(plan_json, space_json)`` once per Optuna study in ANY research run
        (``research_only=False``: in ANY run) on any window except the holdout: the declared
        search space (``params_json["_study"]["space"]``, keyed like the plan's tunables) and
        one trial's plan, which names the aliases."""
        sql = (
            "SELECT MAX(plan_json), MAX(json_extract(params_json, '$._study.space')) FROM trials"
            " WHERE source = 'optuna' AND window_kind != 'holdout' AND json_valid(params_json)"
            " AND json_extract(params_json, '$._study.study') IS NOT NULL"
        )
        if research_only:
            sql += " AND run_id IN (SELECT run_id FROM research_runs)"
        args: builtins.list[str] = []
        if symbol is not None:
            sql += " AND symbol = ?"
            args.append(symbol)
        sql += " GROUP BY run_id, json_extract(params_json, '$._study.study')"
        return [
            (r[0], r[1]) for r in self._conn.execute(sql, args).fetchall() if isinstance(r[1], str)
        ]

    def max_trial_id(self) -> int:
        """The largest trial id (0 for an empty ledger). Trials are append-only, so any result
        computed from the trials is still valid while this number is unchanged."""
        row = self._conn.execute("SELECT MAX(id) FROM trials").fetchone()
        return int(row[0] or 0)

    def design_trial_meta(
        self, *, symbol: str, sources: Iterable[str]
    ) -> builtins.list[dict[str, Any]]:
        """Ok ``window_kind="design"`` trials with stored returns on ``symbol`` whose source is
        in ``sources``, in ANY run (research or not), NEWEST first, WITHOUT their returns:
        ``{id, run_id, plan_hash, plan_json, interval, window_start, window_end (UNIX seconds),
        n_returns, costs_json}``. Pair with :meth:`trial_returns` to decode only the rows a
        caller still needs."""
        srcs = self._check_sources(sources)
        sql = (
            "SELECT id, run_id, plan_hash, plan_json, interval, window_start, window_end,"
            " n_returns, costs_json FROM trials"
            f" WHERE source IN ({', '.join('?' * len(srcs))})"
            " AND window_kind = 'design' AND status = 'ok' AND returns_blob IS NOT NULL"
            " AND symbol = ? ORDER BY id DESC"
        )
        return [
            {
                "id": int(r["id"]),
                "run_id": r["run_id"],
                "plan_hash": r["plan_hash"],
                "plan_json": r["plan_json"],
                "interval": r["interval"],
                "window_start": int(r["window_start"]),
                "window_end": int(r["window_end"]),
                "n_returns": r["n_returns"],
                "costs_json": r["costs_json"],
            }
            for r in self._conn.execute(sql, [*srcs, symbol]).fetchall()
        ]

    def plan_return_trials(
        self,
        *,
        sources: Iterable[str] = SELECTION_SOURCES,
        run_id: str | None = None,
        holdout_start: pd.Timestamp,
    ) -> tuple[builtins.list[dict[str, Any]], set[str]]:
        """One returns-bearing trial per distinct plan of the DSR's nominal ``N`` (Phase 11
        effective-N diagnostic), WITHOUT the returns.

        The plan set is exactly the one :meth:`count_distinct_plans_research` (``run_id`` None:
        every run, risk-overlay runs included, every window, ok and error rows) or
        :meth:`count_distinct_plans` (``run_id``
        given) counts for ``sources``, so ``len(meta) + len(missing)`` equals that count. For each
        plan the trial is its LATEST ok trial with stored returns whose source is in ``sources``
        (an ``adversarial`` or ``benchmark`` row filed under the same hash is a different
        series), from ``run_id`` when given, on a window kind in this preference order:
        ``design``, ``walk_forward_train``, ``walk_forward_test``, ``custom``. ``holdout`` rows
        are NEVER read, and neither is ANY trial whose ``window_end`` is after
        ``holdout_start`` (tz-aware UTC; the lab's ``cfg.splits.holdout_start``), whatever its
        ``window_kind``: a ``custom`` window that runs into the holdout is never chosen.

        Returns ``(meta, missing)``: ``meta`` holds ``{plan_hash, trial_id, run_id, symbol,
        interval, window_start, window_end (UNIX seconds), n_returns, window_kind}`` per plan
        that has such a trial, in plan-hash order; ``missing`` is every plan hash of the set
        without one (only error trials, or returns only on or past the holdout). Decode a
        trial's returns with :meth:`trial_returns`.
        """
        srcs = self._check_sources(sources)
        guard = _ts_to_seconds(holdout_start, "holdout_start")
        marks = ", ".join("?" * len(srcs))
        scope_sql = ""
        scope_args: builtins.list[str] = []
        if run_id is not None:
            scope_sql = " AND run_id = ?"
            scope_args.append(self._check_run_id_arg(run_id))
        plans = {
            r[0]
            for r in self._conn.execute(
                f"SELECT DISTINCT plan_hash FROM trials WHERE source IN ({marks}){scope_sql}",
                [*srcs, *scope_args],
            )
        }
        preference = ", ".join(f"'{k}'" for k in _RETURN_WINDOW_PREFERENCE)
        rank_sql = " ".join(f"WHEN '{k}' THEN {i}" for i, k in enumerate(_RETURN_WINDOW_PREFERENCE))
        sql = (
            "SELECT plan_hash, id, run_id, symbol, interval, window_start, window_end,"
            " n_returns, window_kind FROM trials"
            f" WHERE source IN ({marks}){scope_sql}"
            " AND status = 'ok' AND returns_blob IS NOT NULL AND n_returns > 0"
            f" AND window_kind IN ({preference}) AND window_end <= ?"
            f" ORDER BY plan_hash, CASE window_kind {rank_sql} END, id DESC"
        )
        chosen: dict[str, dict[str, Any]] = {}
        for r in self._conn.execute(sql, [*srcs, *scope_args, guard]):
            if r["plan_hash"] in chosen:
                continue  # rows are ordered best-first within each plan
            chosen[r["plan_hash"]] = {
                "plan_hash": r["plan_hash"],
                "trial_id": int(r["id"]),
                "run_id": r["run_id"],
                "symbol": r["symbol"],
                "interval": r["interval"],
                "window_start": int(r["window_start"]),
                "window_end": int(r["window_end"]),
                "n_returns": int(r["n_returns"]),
                "window_kind": r["window_kind"],
            }
        meta = [chosen[h] for h in sorted(chosen) if h in plans]
        return meta, plans - set(chosen)

    def trial_returns(self, trial_id: int) -> np.ndarray | None:
        """The stored per-bar returns of one trial (None when it has none); ``ValueError`` if
        the id does not exist."""
        row = self._conn.execute(
            "SELECT n_returns, returns_blob FROM trials WHERE id = ?", (int(trial_id),)
        ).fetchone()
        if row is None:
            raise ValueError(f"no trial with id {trial_id} in the ledger")
        return _decode_returns(row["n_returns"], row["returns_blob"])

    def research_design_sharpes(
        self, *, sources: Iterable[str] = SELECTION_SOURCES, costs_json: str
    ) -> builtins.list[tuple[str, str, float]]:
        """``(plan_hash, interval, sharpe_per_bar)`` of the LATEST ok ``window_kind="design"``
        trial with a finite Sharpe per distinct plan whose source is in ``sources`` and whose
        cost model is exactly ``costs_json``, in EVERY research run (any interval, any design
        window; risk-overlay runs excluded). The DSR's cross-interval pooled ``V[SR]``
        annualises these per interval."""
        srcs = self._check_sources(sources)
        sql = (
            "SELECT plan_hash, interval, sharpe_per_bar FROM trials"
            " WHERE status = 'ok' AND sharpe_per_bar IS NOT NULL AND window_kind = 'design'"
            f" AND source IN ({', '.join('?' * len(srcs))}) AND costs_json = ?"
            " AND run_id IN (SELECT run_id FROM research_runs)"
            f" AND run_id NOT IN ({_OVERLAY_RUNS_SQL}) ORDER BY id"
        )
        latest: dict[str, tuple[str, float]] = {}
        args = [*srcs, _check_json(costs_json, "costs_json")]
        for row in self._conn.execute(sql, args).fetchall():
            value = float(row[2])
            if math.isfinite(value):
                latest[row[0]] = (row[1], value)
        return [(h, iv, v) for h, (iv, v) in latest.items()]

    # ------------------------------------------------------------------ holdout budget

    def holdout_checks_used(self, run_id: str) -> int:
        """Holdout checks already spent by ``run_id``."""
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM holdout_checks WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )

    def holdout_logic_checked(self, logic_hash: str) -> bool:
        """True when any run already spent a holdout check on ``logic_hash``."""
        row = self._conn.execute(
            "SELECT 1 FROM holdout_checks WHERE logic_hash = ? LIMIT 1", (logic_hash,)
        ).fetchone()
        return row is not None

    def holdout_checked(self) -> builtins.list[dict[str, Any]]:
        """Every spent holdout check, oldest first: ``{run_id, plan_hash, logic_hash (as
        stored, may be None or an older logic version), plan_json (of the check's holdout
        trial, None if it was never recorded)}``. Lets callers recompute the logic of each
        checked plan with the current canonical form."""
        rows = self._conn.execute(
            "SELECT h.run_id, h.plan_hash, h.logic_hash,"
            " (SELECT t.plan_json FROM trials t WHERE t.run_id = h.run_id"
            "  AND t.plan_hash = h.plan_hash AND t.window_kind = 'holdout'"
            "  ORDER BY t.id LIMIT 1) AS plan_json"
            " FROM holdout_checks h ORDER BY h.id"
        ).fetchall()
        return [
            {
                "run_id": r[0],
                "plan_hash": r[1],
                "logic_hash": r[2],
                "plan_json": r[3],
            }
            for r in rows
        ]

    def record_holdout_check(
        self,
        run_id: str,
        plan_hash: str,
        *,
        budget: int,
        logic_hash: str | None = None,
        family_logic_hashes: Iterable[str] = (),
        family_plan_hashes: Iterable[str] = (),
    ) -> int:
        """Spend one holdout check for ``run_id`` and return the new used count.

        Runs under ``BEGIN IMMEDIATE`` (a write lock taken before reading anything), so
        concurrent connections cannot both spend the last check or both check one logic.
        Raises :class:`HoldoutAlreadyCheckedError` when ANY run already has a check whose
        stored logic hash is ``logic_hash`` or one of ``family_logic_hashes``, or whose plan
        hash is one of ``family_plan_hashes`` (the plan's behaviour family,
        :mod:`lab.research.family`), and :class:`HoldoutBudgetExceeded` when ``used >=
        budget``; nothing is recorded then.
        """
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError(f"budget must be an int >= 0, got {budget!r}")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"run_id must be a non-empty string, got {run_id!r}")
        if logic_hash is not None and (not isinstance(logic_hash, str) or not logic_hash):
            raise ValueError(f"logic_hash must be a non-empty string or None, got {logic_hash!r}")
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            if logic_hash is not None:
                prior = conn.execute(
                    "SELECT run_id FROM holdout_checks WHERE logic_hash = ? ORDER BY id LIMIT 1",
                    (logic_hash,),
                ).fetchone()
                if prior is not None:
                    raise HoldoutAlreadyCheckedError(
                        f"logic {logic_hash[:12]} was already holdout-checked (run "
                        f"{prior[0]}); a hypothesis gets one holdout check, ever"
                    )
            for column, values in (
                ("logic_hash", sorted(set(family_logic_hashes))),
                ("plan_hash", sorted(set(family_plan_hashes))),
            ):
                if not values:
                    continue
                prior = conn.execute(
                    f"SELECT run_id, {column} FROM holdout_checks"
                    f" WHERE {column} IN ({', '.join('?' * len(values))}) ORDER BY id LIMIT 1",
                    values,
                ).fetchone()
                if prior is not None:
                    raise HoldoutAlreadyCheckedError(
                        f"a plan of the same behaviour family ({column.split('_')[0]} "
                        f"{str(prior[1])[:12]}) was already holdout-checked (run {prior[0]}); "
                        "a hypothesis gets one holdout check, ever"
                    )
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
                "INSERT INTO holdout_checks (created_at, run_id, plan_hash, budget, logic_hash)"
                " VALUES (?, ?, ?, ?, ?)",
                (_now_iso(), run_id, plan_hash, budget, logic_hash),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return used + 1

    # ------------------------------------------------------------------ research runs (Phase 8)

    def start_research_run(
        self, run_id: str, *, label: str, config_json: str, config_sha256: str
    ) -> None:
        """Register a research run with its frozen success criteria + budget. Raises
        ``ValueError`` if ``run_id`` already exists (a run's bar is set exactly once)."""
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"run_id must be a non-empty string, got {run_id!r}")
        _check_json(config_json, "config_json")
        try:
            self._conn.execute(
                "INSERT INTO research_runs (run_id, created_at, label, config_json,"
                " config_sha256) VALUES (?, ?, ?, ?, ?)",
                (run_id, _now_iso(), str(label), config_json, str(config_sha256)),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"research run {run_id!r} already exists") from exc

    def research_run(self, run_id: str) -> dict[str, Any] | None:
        """``{run_id, created_at (pd.Timestamp), label, config (dict), config_sha256}`` or
        None when the run was never started."""
        row = self._conn.execute(
            "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _research_run_dict(row)

    def research_runs(self) -> builtins.list[dict[str, Any]]:
        """Every research run (shape of :meth:`research_run`), oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM research_runs ORDER BY created_at, run_id"
        ).fetchall()
        return [_research_run_dict(r) for r in rows]

    def record_evidence(self, run_id: str, plan_hash: str, kind: str, payload_json: str) -> int:
        """Append one evidence row (a report JSON) for ``plan_hash`` in research run
        ``run_id`` and return its id. ``kind`` is one of :data:`EVIDENCE_KINDS`."""
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"kind must be one of {EVIDENCE_KINDS}, got {kind!r}")
        if self.research_run(run_id) is None:
            raise ValueError(f"research run {run_id!r} was never started")
        _check_json(payload_json, "payload_json")
        cur = self._conn.execute(
            "INSERT INTO research_evidence (created_at, run_id, plan_hash, kind, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            (_now_iso(), run_id, plan_hash, kind, payload_json),
        )
        return int(cur.lastrowid or 0)

    def count_evidence_plans(self, run_id: str) -> int:
        """Distinct ``plan_hash`` values with any evidence in research run ``run_id``."""
        return int(
            self._conn.execute(
                "SELECT COUNT(DISTINCT plan_hash) FROM research_evidence WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )

    def evidence(
        self, run_id: str | None, *, plan_hash: str | None = None, kind: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Evidence rows ``{id, created_at, run_id, plan_hash, kind, payload (parsed)}``,
        oldest first; filter by ``plan_hash`` and/or ``kind``. ``run_id=None`` reads every
        research run's evidence."""
        clauses: builtins.list[str] = []
        args: builtins.list[str] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            args.append(run_id)
        if plan_hash is not None:
            clauses.append("plan_hash = ?")
            args.append(plan_hash)
        if kind is not None:
            clauses.append("kind = ?")
            args.append(kind)
        rows = self._conn.execute(
            "SELECT * FROM research_evidence"
            + (f" WHERE {' AND '.join(clauses)}" if clauses else "")
            + " ORDER BY id",
            args,
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "created_at": _iso_to_ts(r["created_at"]),
                "run_id": r["run_id"],
                "plan_hash": r["plan_hash"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]


def _variance(latest: dict[str, float]) -> float:
    values = np.array(list(latest.values()), dtype=np.float64)
    if len(values) < 2:
        return math.nan
    return float(np.var(values, ddof=1))


def _research_run_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "created_at": _iso_to_ts(row["created_at"]),
        "label": row["label"],
        "config": json.loads(row["config_json"]),
        "config_sha256": row["config_sha256"],
    }
