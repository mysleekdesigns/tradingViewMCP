"""Append-only SQLite trial ledger and the locked-holdout budget."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lab.ledger import (
    _MIGRATIONS,
    HoldoutAlreadyCheckedError,
    HoldoutBudgetExceeded,
    Ledger,
    costs_to_json,
    metrics_from_json,
    metrics_to_json,
)
from lab.types import CostModel, Metrics, TrialRecord, TrialSource, TrialStatus, WindowKind


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


W_START = ts("2021-01-01")
W_END = ts("2021-07-01")


def make_record(
    *,
    run_id: str = "run-a",
    status: TrialStatus = "ok",
    source: TrialSource = "manual",
    sharpe: float | None = 0.05,
    returns: np.ndarray | None = None,
    window_start: pd.Timestamp = W_START,
    window_end: pd.Timestamp = W_END,
    error: str | None = None,
) -> TrialRecord:
    return TrialRecord(
        run_id=run_id,
        source=source,
        status=status,
        plan_hash="ab" * 32,
        plan_json='{"name":"ema_cross"}',
        params_json='{"fast":20}',
        symbol="BTCUSDT",
        interval="4h",
        window_kind="walk_forward_test",
        window_start=window_start,
        window_end=window_end,
        metrics_json='{"sharpe":1.2}' if status == "ok" else "{}",
        sharpe_per_bar=sharpe,
        n_trades=7,
        error=error,
        returns=returns,
    )


def metrics(**overrides: float) -> Metrics:
    base = dict(
        total_return=0.1,
        cagr=0.05,
        volatility=0.4,
        sharpe=1.0,
        sortino=1.5,
        max_drawdown=-0.2,
        win_rate=0.55,
        profit_factor=1.3,
        n_trades=12,
        exposure=0.4,
        turnover=10.0,
        bars=1000,
    )
    base.update(overrides)
    return Metrics(**base)  # type: ignore[arg-type]


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    led = Ledger(tmp_path / "ledger.db")
    yield led
    led.close()


# --------------------------------------------------------------------------- json


def test_metrics_json_encodes_nan_and_inf_as_null() -> None:
    m = metrics(win_rate=math.nan, profit_factor=math.inf, sortino=-math.inf)
    text = metrics_to_json(m)
    assert "NaN" not in text and "Infinity" not in text
    parsed = metrics_from_json(text)
    assert parsed["win_rate"] is None
    assert parsed["profit_factor"] is None
    assert parsed["sortino"] is None
    assert parsed["sharpe"] == 1.0
    assert parsed["n_trades"] == 12


def test_metrics_from_json_rejects_nan_tokens_and_non_objects() -> None:
    with pytest.raises(ValueError, match="NaN"):
        metrics_from_json('{"sharpe": NaN}')
    with pytest.raises(ValueError, match="object"):
        metrics_from_json("[1, 2]")


# --------------------------------------------------------------------------- record / get


def test_record_get_round_trip_is_exact(ledger: Ledger) -> None:
    rng = np.random.default_rng(3)
    rets = rng.normal(0, 0.01, 500)
    rets[10] = -0.0  # signed zero and subnormal survive a bit-exact round trip
    rets[11] = 5e-324
    before = pd.Timestamp.now(tz="UTC")
    rec = make_record(returns=rets, error=None)
    trial_id = ledger.record(rec)
    after = pd.Timestamp.now(tz="UTC")

    got = ledger.get(trial_id, include_returns=True)
    assert got.id == trial_id
    assert got.created_at is not None
    assert str(got.created_at.tz) == "UTC"
    assert before - pd.Timedelta(seconds=1) <= got.created_at <= after
    assert got.window_start == W_START and got.window_end == W_END
    assert str(got.window_start.tz) == "UTC"
    assert got.returns is not None
    assert got.returns.dtype == np.float64
    assert got.returns.tobytes() == rets.tobytes()
    assert replace(got, id=None, created_at=None, returns=None) == replace(rec, returns=None)


def test_get_without_returns_leaves_returns_none(ledger: Ledger) -> None:
    trial_id = ledger.record(make_record(returns=np.zeros(5)))
    assert ledger.get(trial_id).returns is None


def test_get_missing_id_raises_value_error_naming_it(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="42"):
        ledger.get(42)


def test_non_finite_sharpe_per_bar_is_stored_as_null(ledger: Ledger) -> None:
    a = ledger.record(make_record(sharpe=math.nan))
    b = ledger.record(make_record(sharpe=math.inf))
    assert ledger.get(a).sharpe_per_bar is None
    assert ledger.get(b).sharpe_per_bar is None


def test_record_rejects_nan_tokens_in_metrics_json(ledger: Ledger) -> None:
    rec = replace(make_record(), metrics_json='{"sharpe": NaN}')
    with pytest.raises(ValueError, match="metrics_json"):
        ledger.record(rec)


def test_record_rejects_naive_window_timestamps(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="window_start"):
        ledger.record(make_record(window_start=pd.Timestamp("2021-01-01")))


def test_record_rejects_record_that_already_has_an_id(ledger: Ledger) -> None:
    got = ledger.get(ledger.record(make_record()))
    with pytest.raises(ValueError, match="id"):
        ledger.record(got)


def test_error_trials_are_recorded(ledger: Ledger) -> None:
    trial_id = ledger.record(make_record(status="error", sharpe=None, error="boom"))
    got = ledger.get(trial_id)
    assert (got.status, got.error, got.metrics_json) == ("error", "boom", "{}")


# --------------------------------------------------------------------------- list / count / runs


def test_list_is_newest_first_with_paging(ledger: Ledger) -> None:
    ids = [ledger.record(make_record()) for _ in range(5)]
    ledger.record(make_record(run_id="run-b"))
    assert [r.id for r in ledger.list(run_id="run-a")] == ids[::-1]
    assert [r.id for r in ledger.list(run_id="run-a", limit=2, offset=1)] == [ids[3], ids[2]]
    assert len(ledger.list()) == 6


def test_count_by_run_and_status(ledger: Ledger) -> None:
    for _ in range(3):
        ledger.record(make_record())
    ledger.record(make_record(status="error", sharpe=None, error="x"))
    ledger.record(make_record(run_id="run-b", source="optuna"))
    assert ledger.count() == 5
    assert ledger.count("run-a") == 4  # DSR's N counts error trials too
    assert ledger.count("run-a", status="ok") == 3
    assert ledger.count("run-a", status="error") == 1
    assert ledger.count("run-b") == 1
    assert ledger.count("nope") == 0


def test_runs_summarises_each_run(ledger: Ledger) -> None:
    ledger.record(make_record(run_id="run-a"))
    ledger.record(make_record(run_id="run-a"))
    ledger.record(make_record(run_id="run-b"))
    runs = ledger.runs()
    assert [(r["run_id"], r["n_trials"]) for r in runs] == [("run-a", 2), ("run-b", 1)]
    assert runs[0]["first_created_at"] <= runs[0]["last_created_at"]


# --------------------------------------------------------------------------- statistics inputs


def test_sharpe_variance_matches_hand_calculation(ledger: Ledger) -> None:
    for s in (0.01, 0.02, 0.03, 0.06):
        ledger.record(make_record(sharpe=s))
    ledger.record(make_record(sharpe=math.nan))  # stored NULL, excluded
    ledger.record(make_record(status="error", sharpe=9.0, error="x"))  # error, excluded
    ledger.record(make_record(run_id="run-b", sharpe=5.0))  # other run, excluded
    # mean 0.03; squared deviations 4e-4, 1e-4, 0, 9e-4 -> sum 1.4e-3 / (4 - 1)
    assert ledger.sharpe_variance("run-a") == pytest.approx(1.4e-3 / 3, rel=1e-12)


def test_sharpe_variance_is_nan_with_fewer_than_two_trials(ledger: Ledger) -> None:
    ledger.record(make_record(sharpe=0.1))
    assert math.isnan(ledger.sharpe_variance("run-a"))


def test_returns_matrix_aligns_ok_trials_on_the_exact_window(ledger: Ledger) -> None:
    a = np.array([0.01, -0.02, 0.03])
    b = np.array([0.00, 0.01, -0.01])
    id_a = ledger.record(make_record(returns=a))
    ledger.record(make_record(status="error", sharpe=None, returns=np.ones(3), error="x"))
    ledger.record(make_record(returns=np.ones(3), window_end=ts("2021-08-01")))
    ledger.record(make_record(returns=np.ones(3), window_start=ts("2020-12-01")))
    ledger.record(make_record(run_id="run-b", returns=np.ones(3)))
    ledger.record(make_record(returns=None))
    id_b = ledger.record(make_record(returns=b))

    matrix, ids = ledger.returns_matrix("run-a", window_start=W_START, window_end=W_END)
    assert ids == [id_a, id_b]
    assert matrix.shape == (3, 2)
    np.testing.assert_array_equal(matrix[:, 0], a)
    np.testing.assert_array_equal(matrix[:, 1], b)


def test_returns_matrix_keeps_the_common_length_only(ledger: Ledger) -> None:
    ids_3 = [ledger.record(make_record(returns=np.full(3, i))) for i in range(2)]
    ledger.record(make_record(returns=np.zeros(4)))
    matrix, ids = ledger.returns_matrix("run-a", window_start=W_START, window_end=W_END)
    assert ids == ids_3
    assert matrix.shape == (3, 2)


def test_returns_matrix_is_empty_without_trials(ledger: Ledger) -> None:
    matrix, ids = ledger.returns_matrix("run-a", window_start=W_START, window_end=W_END)
    assert matrix.shape == (0, 0) and ids == []


# --------------------------------------------------------------------------- holdout budget


def test_holdout_budget_allows_three_then_refuses_the_fourth(ledger: Ledger) -> None:
    assert [ledger.record_holdout_check("run-a", "h", budget=3) for _ in range(3)] == [1, 2, 3]
    with pytest.raises(HoldoutBudgetExceeded, match="run-a"):
        ledger.record_holdout_check("run-a", "h", budget=3)
    assert ledger.holdout_checks_used("run-a") == 3


def test_holdout_budget_is_per_run(ledger: Ledger) -> None:
    for _ in range(3):
        ledger.record_holdout_check("run-a", "h", budget=3)
    assert ledger.record_holdout_check("run-b", "h", budget=3) == 1
    assert ledger.holdout_checks_used("run-b") == 1


def test_two_connections_cannot_both_exceed_the_budget(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path) as one, Ledger(path) as two:
        assert one.record_holdout_check("run-a", "h", budget=3) == 1
        assert two.record_holdout_check("run-a", "h", budget=3) == 2
        assert one.record_holdout_check("run-a", "h", budget=3) == 3
        with pytest.raises(HoldoutBudgetExceeded):
            two.record_holdout_check("run-a", "h", budget=3)
        with pytest.raises(HoldoutBudgetExceeded):
            one.record_holdout_check("run-a", "h", budget=3)
        assert one.holdout_checks_used("run-a") == two.holdout_checks_used("run-a") == 3


def test_concurrent_holdout_checks_spend_exactly_the_budget(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    Ledger(path).close()  # migrate once up front
    n_workers = 8
    barrier = threading.Barrier(n_workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        with Ledger(path) as led:
            barrier.wait()
            try:
                led.record_holdout_check("run-a", "h", budget=3)
                result = "ok"
            except HoldoutBudgetExceeded:
                result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ok"] * 3 + ["refused"] * 5
    with Ledger(path) as led:
        assert led.holdout_checks_used("run-a") == 3


def test_negative_budget_is_rejected(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="budget"):
        ledger.record_holdout_check("run-a", "h", budget=-1)


# --------------------------------------------------------------------------- persistence / schema


def test_trials_and_holdout_checks_persist_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "ledger.db"  # parent dirs are created
    with Ledger(path) as led:
        trial_id = led.record(make_record(returns=np.arange(4, dtype=float)))
        led.record_holdout_check("run-a", "h", budget=3)
    with Ledger(path) as led:
        got = led.get(trial_id, include_returns=True)
        assert got.returns is not None
        np.testing.assert_array_equal(got.returns, np.arange(4, dtype=float))
        assert led.count("run-a") == 1
        assert led.holdout_checks_used("run-a") == 1


def test_migrations_are_idempotent_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    for _ in range(3):
        with Ledger(path) as led:
            assert led.schema_version == 5
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT version, name FROM schema_version").fetchall()
    conn.close()
    assert rows == [
        (1, "init"),
        (2, "costs_json"),
        (3, "research_runs"),
        (4, "holdout_logic_hash"),
        (5, "trial_hash_chain"),
    ]


def test_database_uses_wal(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as led:
        mode = led._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_in_memory_ledger_works() -> None:
    with Ledger(":memory:") as led:
        trial_id = led.record(make_record())
        assert led.get(trial_id).run_id == "run-a"


# --------------------------------------------------------------------------- append-only


def test_ledger_has_no_update_or_delete_api() -> None:
    public = [name for name in dir(Ledger) if not name.startswith("_")]
    forbidden = ("update", "delete", "remove", "clear", "drop", "edit", "set", "replace")
    assert [n for n in public if any(f in n.lower() for f in forbidden)] == []


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE trials SET sharpe_per_bar = 9 WHERE id = 1",
        "DELETE FROM trials",
        "UPDATE holdout_checks SET run_id = 'other'",
        "DELETE FROM holdout_checks",
    ],
)
def test_database_rejects_updates_and_deletes(tmp_path: Path, sql: str) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path) as led:
        led.record(make_record())
        led.record_holdout_check("run-a", "h", budget=3)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)
    finally:
        conn.close()
    with Ledger(path) as led:
        assert led.count() == 1 and led.holdout_checks_used("run-a") == 1


def test_stored_metrics_json_is_parseable_by_strict_json(ledger: Ledger) -> None:
    rec = replace(make_record(), metrics_json=metrics_to_json(metrics(win_rate=math.nan)))
    got = ledger.get(ledger.record(rec))
    assert json.loads(got.metrics_json)["win_rate"] is None


# --------------------------------------------------------------------------- selection (DSR N)


def _plan_record(
    plan_hash: str,
    *,
    source: TrialSource = "api",
    sharpe: float | None = 0.05,
    status: TrialStatus = "ok",
    run_id: str = "run-a",
    interval: str = "4h",
    window_kind: WindowKind = "design",
    window_start: pd.Timestamp = W_START,
    window_end: pd.Timestamp = W_END,
    costs_json: str = "{}",
) -> TrialRecord:
    return replace(
        make_record(
            source=source,
            sharpe=sharpe,
            status=status,
            run_id=run_id,
            window_start=window_start,
            window_end=window_end,
        ),
        plan_hash=plan_hash,
        interval=interval,
        window_kind=window_kind,
        costs_json=costs_json,
    )


def test_count_distinct_plans_ignores_repeats_and_non_selection_sources(ledger: Ledger) -> None:
    ledger.record(_plan_record("a"))
    ledger.record(_plan_record("a"))  # same plan re-run: not a new configuration
    ledger.record(_plan_record("b", source="neighbourhood"))
    ledger.record(_plan_record("c", source="optuna", status="error", sharpe=None))
    ledger.record(_plan_record("d", source="adversarial"))
    ledger.record(_plan_record("e", source="benchmark"))
    ledger.record(_plan_record("f", run_id="run-b"))
    assert ledger.count_distinct_plans("run-a") == 3  # a, b, c (errors were still tried)
    assert ledger.count_distinct_plans("run-a", sources=["benchmark"]) == 1
    assert ledger.count("run-a") == 6


def test_count_distinct_plans_can_filter_by_interval(ledger: Ledger) -> None:
    ledger.record(_plan_record("a"))
    ledger.record(_plan_record("b", interval="1d"))
    assert ledger.count_distinct_plans("run-a", interval="4h") == 1
    assert ledger.count_distinct_plans("run-a") == 2


def test_selection_sharpe_variance_uses_the_latest_sharpe_per_plan(ledger: Ledger) -> None:
    ledger.record(_plan_record("a", sharpe=0.50))  # superseded by the later run of "a"
    ledger.record(_plan_record("a", sharpe=0.01))
    ledger.record(_plan_record("b", sharpe=0.03))
    ledger.record(_plan_record("c", sharpe=0.9, source="adversarial"))  # not a selection trial
    ledger.record(_plan_record("d", sharpe=None, status="error"))
    assert ledger.latest_sharpes_per_plan("run-a") == {"a": 0.01, "b": 0.03}
    # hand: values 0.01, 0.03 -> mean 0.02, var(ddof=1) = (1e-4 + 1e-4) / 1 = 2e-4
    assert ledger.selection_sharpe_variance("run-a") == pytest.approx(2e-4, rel=1e-12)


def test_selection_sharpe_variance_is_nan_with_one_plan(ledger: Ledger) -> None:
    ledger.record(_plan_record("a", sharpe=0.01))
    ledger.record(_plan_record("a", sharpe=0.02))
    assert math.isnan(ledger.selection_sharpe_variance("run-a"))


def test_selection_helpers_reject_unknown_sources(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="sources"):
        ledger.count_distinct_plans("run-a", sources=["holdout"])
    with pytest.raises(ValueError, match="at least one"):
        ledger.count_distinct_plans("run-a", sources=[])


# --------------------------------------------------------------------------- variance scope


def test_walk_forward_test_rows_never_change_the_design_variance(ledger: Ledger) -> None:
    ledger.record(_plan_record("a", sharpe=0.01))
    ledger.record(_plan_record("b", sharpe=0.03))
    before = ledger.selection_sharpe_variance("run-a", window_start=W_START, window_end=W_END)
    # A later out-of-sample fold of plan "a" with a wild Sharpe, on the same and on another
    # window: neither may replace "a"'s design Sharpe nor add a value.
    for start, end in ((W_START, W_END), (ts("2021-07-01"), ts("2022-01-01"))):
        ledger.record(
            _plan_record(
                "a",
                source="optuna",
                sharpe=5.0,
                window_kind="walk_forward_test",
                window_start=start,
                window_end=end,
            )
        )
    after = ledger.selection_sharpe_variance("run-a", window_start=W_START, window_end=W_END)
    assert after == before == pytest.approx(2e-4, rel=1e-12)
    assert ledger.selection_sharpe_variance("run-a") == pytest.approx(2e-4, rel=1e-12)
    assert ledger.count_distinct_plans("run-a") == 2  # N still counts every window


def test_variance_only_combines_trials_on_the_same_window(ledger: Ledger) -> None:
    other = (ts("2020-01-01"), ts("2020-07-01"))
    ledger.record(_plan_record("a", sharpe=0.01))
    ledger.record(_plan_record("b", sharpe=0.03))
    ledger.record(_plan_record("c", sharpe=0.5, window_start=other[0], window_end=other[1]))
    assert ledger.latest_sharpes_per_plan("run-a", window_start=W_START, window_end=W_END) == {
        "a": 0.01,
        "b": 0.03,
    }
    with pytest.raises(ValueError, match="together"):
        ledger.selection_sharpe_variance("run-a", window_start=W_START)


def test_variance_only_combines_trials_with_the_same_costs(ledger: Ledger) -> None:
    base = costs_to_json(CostModel())
    stressed = costs_to_json(CostModel().scaled(4.0))
    ledger.record(_plan_record("a", sharpe=0.01, costs_json=base))
    ledger.record(_plan_record("b", sharpe=0.03, costs_json=base))
    ledger.record(_plan_record("c", sharpe=-0.4, costs_json=stressed))
    assert set(ledger.latest_sharpes_per_plan("run-a", costs_json=base)) == {"a", "b"}
    assert ledger.selection_sharpe_variance("run-a", costs_json=base) == pytest.approx(
        2e-4, rel=1e-12
    )
    assert ledger.count_distinct_plans("run-a") == 3


def test_costs_json_round_trips_and_is_canonical(ledger: Ledger) -> None:
    costs = CostModel(0.001, 0.0005, 0.1, 0.01)
    text = costs_to_json(costs)
    assert text == json.dumps(
        {"fee_rate": 0.001, "slippage_cap": 0.01, "slippage_rate": 0.0005,
         "slippage_vol_mult": 0.1},
        sort_keys=True,
    )  # fmt: skip
    trial_id = ledger.record(replace(make_record(), costs_json=text))
    assert ledger.get(trial_id).costs_json == text
    with pytest.raises(ValueError, match="costs_json"):
        ledger.record(replace(make_record(), costs_json="{not json"))


def test_migration_2_adds_costs_json_to_an_existing_v1_ledger(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "CREATE TABLE schema_version ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    version, name, statements = _MIGRATIONS[0]
    for sql in statements:
        conn.execute(sql)
    conn.execute(
        "INSERT INTO schema_version VALUES (?, ?, ?)", (version, name, "2026-01-01T00:00:00")
    )
    conn.execute(
        "INSERT INTO trials (created_at, run_id, source, status, plan_hash, plan_json,"
        " params_json, symbol, interval, window_kind, window_start, window_end, metrics_json,"
        " sharpe_per_bar, n_trades) VALUES ('2026-01-01T00:00:00+00:00', 'old', 'api', 'ok',"
        " 'h', '{}', '{}', 'BTCUSDT', '4h', 'design', 0, 14400, '{}', 0.1, 0)"
    )
    conn.close()
    with Ledger(path) as led:
        assert led.schema_version == 5  # every later migration applies on top of v1
        (old,) = led.list(run_id="old")
        assert old.costs_json == "{}"
        new_id = led.record(replace(make_record(), costs_json=costs_to_json(CostModel())))
        assert led.get(new_id).costs_json == costs_to_json(CostModel())


# --------------------------------------------------------------------------- Phase 8 additions


def _research(led: Ledger, run_id: str) -> None:
    led.start_research_run(run_id, label="t", config_json="{}", config_sha256="x")


def test_count_distinct_plans_research_spans_every_run(ledger: Ledger) -> None:
    """DSR N for research: distinct plans across ALL runs (research runs and the web UI's; any
    interval/symbol); a new run does not reset it; non-selection sources do not count."""
    _research(ledger, "r1")
    _research(ledger, "r2")
    ledger.record(replace(make_record(run_id="r1"), plan_hash="a" * 64))
    ledger.record(replace(make_record(run_id="r1"), plan_hash="b" * 64, interval="1h"))
    ledger.record(
        replace(make_record(run_id="r2", status="error", sharpe=None), plan_hash="c" * 64)
    )
    ledger.record(replace(make_record(run_id="r2"), plan_hash="a" * 64))  # repeat: no inflation
    ledger.record(replace(make_record(run_id="r2", source="benchmark"), plan_hash="d" * 64))
    ledger.record(replace(make_record(run_id="ui-run"), plan_hash="e" * 64))  # web UI: counts
    assert ledger.count_distinct_plans_research() == 4
    assert ledger.count_distinct_plans("r2") == 2
    _research(ledger, "r3")  # a fresh run starts at the global N, not 0
    assert ledger.count_distinct_plans_research() == 4
    assert ledger.count_distinct_plans_research(sources=("benchmark",)) == 1


def test_list_and_count_can_exclude_window_kinds(ledger: Ledger) -> None:
    ledger.record(make_record())
    ledger.record(replace(make_record(), window_kind="holdout"))
    ledger.record(replace(make_record(), window_kind="design"))
    assert ledger.count("run-a") == 3
    assert ledger.count("run-a", exclude_window_kinds=("holdout",)) == 2
    kinds = [r.window_kind for r in ledger.list(run_id="run-a", exclude_window_kinds=["holdout"])]
    assert sorted(kinds) == ["design", "walk_forward_test"]
    with pytest.raises(ValueError, match="window kinds"):
        ledger.count(exclude_window_kinds=("nope",))


def test_holdout_check_refuses_a_logic_already_checked_in_any_run(ledger: Ledger) -> None:
    assert ledger.record_holdout_check("r1", "p1", budget=3, logic_hash="L") == 1
    assert ledger.holdout_logic_checked("L") and not ledger.holdout_logic_checked("M")
    with pytest.raises(HoldoutAlreadyCheckedError, match="already holdout-checked"):
        ledger.record_holdout_check("r2", "p2", budget=3, logic_hash="L")  # another run
    with pytest.raises(HoldoutAlreadyCheckedError):
        ledger.record_holdout_check("r1", "p3", budget=3, logic_hash="L")  # same run
    assert ledger.holdout_checks_used("r2") == 0 and ledger.holdout_checks_used("r1") == 1
    assert ledger.record_holdout_check("r2", "p2", budget=3, logic_hash="M") == 1
    assert ledger.record_holdout_check("r2", "p4", budget=3) == 2  # no logic: budget only


def test_concurrent_holdout_checks_of_one_logic_spend_once(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    Ledger(path).close()
    results: list[str] = []

    def spend(run_id: str) -> None:
        with Ledger(path) as led:
            try:
                led.record_holdout_check(run_id, "p", budget=5, logic_hash="L")
                results.append("ok")
            except HoldoutAlreadyCheckedError:
                results.append("refused")

    threads = [threading.Thread(target=spend, args=(f"r{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok"] + ["refused"] * 5


def test_returns_matrix_filters_by_source_and_study(ledger: Ledger) -> None:
    def study_params(name: str) -> str:
        return json.dumps({"x": 1, "_study": {"study": name}})

    r = np.arange(6, dtype=float)
    a = ledger.record(
        replace(make_record(source="optuna", returns=r), params_json=study_params("A"))
    )
    b = ledger.record(
        replace(make_record(source="optuna", returns=r), params_json=study_params("B"))
    )
    ledger.record(replace(make_record(source="optuna", returns=r), params_json=study_params("C")))
    ledger.record(make_record(source="neighbourhood", returns=r))
    _, ids = ledger.returns_matrix("run-a", window_start=W_START, window_end=W_END)
    assert len(ids) == 4
    _, ids = ledger.returns_matrix(
        "run-a", window_start=W_START, window_end=W_END, sources=("optuna",), studies={"A", "B"}
    )
    assert ids == [a, b]
    m, ids = ledger.returns_matrix("run-a", window_start=W_START, window_end=W_END, studies=[])
    assert m.shape == (0, 0) and ids == []


# --------------------------------------------------------------------------- plan_return_trials

# The design split's holdout start for these tests: every default window ends exactly on it
# (window_end == holdout_start is allowed: bars are [start, end)).
HOLDOUT = W_END


def _plan(ledger: Ledger, h: str, **over: object) -> int:
    """Record one trial of plan ``h`` (design window, ok, 6 returns unless overridden)."""
    base = make_record(returns=np.full(6, 0.01))
    fields: dict[str, object] = {"plan_hash": h * 64, "window_kind": "design", **over}
    if fields.get("status") == "error":
        fields.update(returns=None, sharpe_per_bar=None, metrics_json="{}", error="boom")
    return ledger.record(replace(base, **fields))  # type: ignore[arg-type]


def test_plan_return_trials_covers_the_nominal_plan_set(ledger: Ledger) -> None:
    """Phase 11: one returns trial per plan of the nominal N (global and per run); plans
    without a usable non-holdout selection trial are listed as missing, never dropped."""
    _research(ledger, "r1")
    _plan(ledger, "a", run_id="r1")
    _plan(ledger, "b", run_id="r1", status="error")  # error only
    _plan(ledger, "c", run_id="r2", window_kind="holdout", source="manual")  # holdout only
    _plan(ledger, "d", run_id="r2", status="error")
    _plan(ledger, "d", run_id="r2", source="adversarial")  # same hash, not a selection source
    _plan(ledger, "e", run_id="ui", window_kind="custom")
    _plan(ledger, "f", run_id="r2", source="benchmark")  # not in the plan set at all
    meta, missing = ledger.plan_return_trials(holdout_start=HOLDOUT)
    assert {m["plan_hash"] for m in meta} == {"a" * 64, "e" * 64}
    assert missing == {"b" * 64, "c" * 64, "d" * 64}
    assert len(meta) + len(missing) == ledger.count_distinct_plans_research() == 5
    for run in ("r1", "r2", "ui"):
        meta, missing = ledger.plan_return_trials(run_id=run, holdout_start=HOLDOUT)
        assert len(meta) + len(missing) == ledger.count_distinct_plans(run)
    meta, missing = ledger.plan_return_trials(run_id="r2", holdout_start=HOLDOUT)
    assert meta == [] and missing == {"c" * 64, "d" * 64}


def test_plan_return_trials_prefers_design_then_train_then_test_and_never_the_holdout(
    ledger: Ledger,
) -> None:
    design = _plan(ledger, "a", source="api")
    _plan(ledger, "a", source="api", window_kind="walk_forward_train")  # newer, worse kind
    _plan(ledger, "a", source="manual", window_kind="holdout")  # newest: never read
    test = _plan(ledger, "b", source="optuna", window_kind="walk_forward_test")
    train_old = _plan(ledger, "b", source="optuna", window_kind="walk_forward_train")
    train_new = _plan(ledger, "b", source="optuna", window_kind="walk_forward_train")
    _plan(ledger, "c", window_kind="custom")
    newest_design = _plan(ledger, "c")
    meta, missing = ledger.plan_return_trials(holdout_start=HOLDOUT)
    assert missing == set()
    by_plan = {m["plan_hash"][0]: m for m in meta}
    assert by_plan["a"]["trial_id"] == design and by_plan["a"]["window_kind"] == "design"
    assert by_plan["b"]["trial_id"] == train_new and train_new > train_old > test
    assert by_plan["c"]["trial_id"] == newest_design
    assert by_plan["a"] == {
        "plan_hash": "a" * 64,
        "trial_id": design,
        "run_id": "run-a",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "window_start": int(W_START.timestamp()),
        "window_end": int(W_END.timestamp()),
        "n_returns": 6,
        "window_kind": "design",
    }
    np.testing.assert_array_equal(ledger.trial_returns(design), np.full(6, 0.01))


def test_plan_return_trials_rejects_unknown_sources_and_bad_run_ids(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="sources"):
        ledger.plan_return_trials(sources=("nope",), holdout_start=HOLDOUT)
    with pytest.raises(ValueError, match="run_id"):
        ledger.plan_return_trials(run_id=3, holdout_start=HOLDOUT)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="holdout_start"):
        ledger.plan_return_trials(holdout_start=pd.Timestamp("2021-07-01"))  # naive


def test_plan_return_trials_never_reads_a_window_that_runs_past_the_holdout(
    ledger: Ledger,
) -> None:
    """A date guard, not only a window_kind filter: a ``custom`` (or any non-holdout kind)
    window that ends after holdout_start is never chosen, even when it is the plan's newest
    or best-ranked trial; a plan with only such trials is missing."""
    late = W_END + pd.Timedelta(days=30)
    safe = _plan(ledger, "a", window_kind="custom")
    _plan(ledger, "a", window_kind="custom", window_end=late)  # newer, crosses the holdout
    _plan(ledger, "b", window_kind="custom", window_end=late)  # only a crossing trial
    _plan(ledger, "c", window_kind="design", window_end=late)  # best kind, still crossing
    meta, missing = ledger.plan_return_trials(holdout_start=HOLDOUT)
    assert [m["trial_id"] for m in meta] == [safe]
    assert missing == {"b" * 64, "c" * 64}
    assert all(m["window_end"] <= int(HOLDOUT.timestamp()) for m in meta)
    # with a later holdout the same rows are readable again (the guard is the date)
    meta, missing = ledger.plan_return_trials(holdout_start=late)
    assert {m["plan_hash"][0] for m in meta} == {"a", "b", "c"} and missing == set()
