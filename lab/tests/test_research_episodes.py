"""The per-episode table and turnover block of ``get_report`` (PLAN.md Phase 20, Parts C, G).

Synthetic data only. Known answers for the pure helpers, then real runs through the MCP server
on a tmp ledger: the evidence path, a golden report, and the rule that the table is information
only (no ledger row, no criterion row, no holdout bar).

Regenerate the golden only when the fixture is absent (the protect-files hook freezes existing
fixtures): ``LAB_REGEN_GOLDEN=1 uv run pytest -q tests/test_research_episodes.py``.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import lab.research.runs as runs
from lab.config import LabConfig
from lab.engine import run_signals
from lab.ledger import Ledger
from lab.mcp_server import INSTRUCTIONS, build_server
from lab.plan import parse_plan, plan_hash
from lab.research.criteria import evaluate_plan
from lab.research.episodes import (
    EPISODES_INFORMATION_NOTE,
    EPISODES_PORTFOLIO_NOTE,
    EPISODES_PREDATE_NOTE,
    NO_BARS_NOTE,
    episode_table,
    episodes_evidence,
    invested_flags,
    nothing_to_read,
    persist_settings,
    portfolio_blocks,
    report_blocks,
    turnover_block,
    usable_episodes,
)
from lab.research.evidence import record_report
from lab.research.overlay import overlay_block_and_pair
from lab.research.runs import frozen_overlay_criteria
from lab.types import CostModel, Episode, RiskSpec, bars_per_year
from tests.test_mcp_server import (
    HOLDOUT_START,
    TREND_PLAN,
    Lab,
    loader_for,
    ok,
    small_cfg,
    trend_candles,
    write_criteria,
)
from tests.test_research_overlay import CRASH_CANDLES, MA_OVERLAY, overlay_cfg, overlay_toml

FIXTURE = Path(__file__).parent / "fixtures" / "episodes_report_golden.json"
RUN_ID = "r-20261104-000000-abc123"
REGEN_ENV = "LAB_REGEN_GOLDEN"
HOLDOUT = pd.Timestamp(HOLDOUT_START, tz="UTC")


def ts(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


def ep(name: str, start: str, end: str) -> Episode:
    return Episode(name, ts(start), ts(end))


EPISODES = (
    ep("before_the_data", "2017-01-01", "2017-02-01"),
    ep("winter_2019", "2019-01-01", "2019-02-01"),
    ep("touches_holdout", "2019-09-01", "2019-10-01"),
    ep("crosses_holdout", "2019-09-15", "2019-10-15"),
    ep("inside_holdout", "2019-11-01", "2019-12-01"),
)


# --------------------------------------------------------------------------- holdout clipping


def test_usable_episodes_clips_at_the_holdout_boundary() -> None:
    kept = usable_episodes(EPISODES, HOLDOUT)
    assert [e.name for e in kept] == ["before_the_data", "winter_2019", "touches_holdout"]


def test_episode_ending_one_second_after_the_holdout_is_dropped() -> None:
    late = Episode("late", ts("2019-09-01"), HOLDOUT + pd.Timedelta(seconds=1))
    on_time = Episode("on_time", ts("2019-09-01"), HOLDOUT)
    assert usable_episodes((late, on_time), HOLDOUT) == (on_time,)


def test_usable_episodes_does_not_shorten_what_it_keeps() -> None:
    assert usable_episodes(EPISODES[:3], HOLDOUT) == EPISODES[:3]


def test_evidence_refuses_bars_at_or_after_the_holdout() -> None:
    index = pd.date_range("2019-09-30 20:00", periods=2, freq="4h", tz="UTC")
    assert index[-1] == HOLDOUT
    with pytest.raises(ValueError, match="locked holdout"):
        episodes_evidence((), HOLDOUT, index, np.zeros(2), np.zeros(2), np.zeros(2), [])


def test_evidence_lists_holdout_dropped_episodes_by_name() -> None:
    index = pd.date_range("2019-01-01", periods=10, freq="1D", tz="UTC")
    out = episodes_evidence(EPISODES, HOLDOUT, index, np.zeros(10), np.zeros(10), np.zeros(10), [])
    assert [r["name"] for r in out["rows"]] == [
        "before_the_data",
        "winter_2019",
        "touches_holdout",
    ]
    assert out["dropped_for_holdout"] == ["crosses_holdout", "inside_holdout"]


# --------------------------------------------------------------------------- the table


def _table(
    r_s: list[float],
    r_b: list[float],
    invested: list[int],
    entries: list[pd.Timestamp],
    episode: Episode,
) -> dict[str, Any]:
    index = pd.date_range("2020-01-01", periods=len(r_s), freq="1D", tz="UTC")
    rows = episode_table(
        [episode], index, np.array(r_s), np.array(r_b), np.array(invested, dtype=float), entries
    )
    assert len(rows) == 1
    return rows[0]


def test_row_has_the_hand_computed_answer() -> None:
    row = _table(
        [0.5, 0.10, -0.50, 0.20, 0.5],
        [0.5, -0.10, -0.10, 0.10, 0.5],
        [1, 1, 1, 0, 0],
        [ts("2019-12-31"), ts("2020-01-03"), ts("2020-01-04"), ts("2020-01-05")],
        ep("e", "2020-01-02", "2020-01-05"),  # bars 1, 2, 3
    )
    assert row["bars"] == 3
    assert row["strategy_return"] == pytest.approx(1.10 * 0.50 * 1.20 - 1.0)
    assert row["buy_hold_return"] == pytest.approx(0.90 * 0.90 * 1.10 - 1.0)
    assert row["strategy_max_dd"] == pytest.approx(0.5)  # 1.10 -> 0.55
    assert row["buy_hold_max_dd"] == pytest.approx(1.0 - 0.81)  # from 1.0 before the first bar
    assert row["exposure"] == pytest.approx(2 / 3)
    assert row["trades"] == 2  # 01-03 and 01-04; 12-31 is before, [start, end) leaves out 01-05
    assert "note" not in row


def test_max_drawdown_starts_inside_the_episode() -> None:
    """A drop before the episode's first bar cannot deepen the episode's drawdown."""
    row = _table(
        [-0.90, 0.10, -0.10],
        [-0.90, 0.10, -0.10],
        [1, 1, 1],
        [],
        ep("e", "2020-01-02", "2020-01-04"),
    )
    assert row["strategy_max_dd"] == pytest.approx(0.10)


def test_episode_with_no_scored_bars_keeps_a_row_of_nulls() -> None:
    row = _table([0.1, 0.1], [0.1, 0.1], [1, 1], [], ep("gone", "2015-01-01", "2015-02-01"))
    assert row["bars"] == 0
    assert row["note"] == NO_BARS_NOTE
    for key in (
        "strategy_return",
        "buy_hold_return",
        "strategy_max_dd",
        "buy_hold_max_dd",
        "exposure",
        "trades",
    ):
        assert row[key] is None, key


def test_non_finite_values_are_null_and_never_nan() -> None:
    row = _table([math.nan, 0.1], [0.1, 0.1], [1, 1], [], ep("e", "2020-01-01", "2020-01-03"))
    assert row["strategy_return"] is None and row["strategy_max_dd"] is None
    assert row["buy_hold_return"] == pytest.approx(0.21)
    json.dumps(row, allow_nan=False)


def test_table_refuses_misaligned_series() -> None:
    index = pd.date_range("2020-01-01", periods=3, freq="1D", tz="UTC")
    with pytest.raises(ValueError, match="aligned"):
        episode_table([], index, np.zeros(2), np.zeros(3), np.zeros(3), [])


def test_invested_flags_match_the_engine_position() -> None:
    """Flags derived from the trade list equal the engine's ``position != 0``, stops included."""
    candles = trend_candles(n=400)
    entries = pd.Series(False, index=candles.index)
    exits = pd.Series(False, index=candles.index)
    entries.iloc[[10, 60, 200, 390]] = True
    exits.iloc[[40, 120, 260]] = True
    res = run_signals(
        candles,
        entries,
        exits,
        costs=CostModel(fee_rate=0.001, slippage_rate=0.0005),
        risk=RiskSpec(stop_loss_pct=2.0),
    )
    assert any(t.exit_reason == "stop_loss" for t in res.trades), res.trades
    assert any(t.exit_time is None for t in res.trades), "the last trade must stay open"
    spans = [(t.entry_time, t.exit_time) for t in res.trades]
    np.testing.assert_array_equal(
        invested_flags(candles.index, spans), res.position.to_numpy() != 0.0
    )


# --------------------------------------------------------------------------- report blocks


def test_turnover_block_uses_the_365_day_bar_count() -> None:
    bt = {
        "interval": "4h",
        "metrics": {"net": {"n_trades": 20, "bars": 4380, "turnover": 19.5}},
    }
    block = turnover_block(bt)
    assert bars_per_year("4h") == 2190.0
    assert block["trades_per_year"] == pytest.approx(10.0)  # 20 trades in two 365-day years
    assert block["traded_value_per_year"] == 19.5
    assert block["persist_settings"] == []


def test_turnover_block_is_null_when_the_metrics_cannot_answer() -> None:
    assert turnover_block({})["trades_per_year"] is None
    assert turnover_block({})["traded_value_per_year"] is None
    bars_zero = {"interval": "4h", "metrics": {"net": {"n_trades": 0, "bars": 0}}}
    assert turnover_block(bars_zero)["trades_per_year"] is None


def test_persist_settings_read_each_rule_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge plans read both rules; an overlay's entry is its negated rule, so only flat_when."""

    class Expr:
        def __init__(self, ks: tuple[int, ...]) -> None:
            self.persist_ks = ks

    monkeypatch.setattr(
        "lab.research.episodes.checked_expressions", lambda plan: (Expr((3,)), Expr((5, 2)))
    )
    assert persist_settings(TREND_PLAN) == [3, 5, 2]
    overlay = {
        "kind": "overlay",
        "name": "o",
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {"s": {"sma": {"period": 50}}},
        "flat_when": "close < s",
    }
    assert persist_settings(overlay) == [5, 2]


def test_persist_settings_of_an_unusable_plan_are_empty() -> None:
    assert persist_settings(None) == []
    assert persist_settings({"name": "broken"}) == []


def test_evidence_without_a_table_reports_null_with_a_note() -> None:
    blocks = report_blocks({"metrics": {}})
    assert blocks["episodes"] is None
    assert blocks["episodes_note"] == EPISODES_PREDATE_NOTE
    assert "turnover" in blocks
    assert not nothing_to_read(blocks)


def test_no_evidence_adds_nothing() -> None:
    assert report_blocks(None) == {}
    assert nothing_to_read({})


def test_portfolio_plans_get_one_line() -> None:
    assert portfolio_blocks() == {"episodes": None, "episodes_note": EPISODES_PORTFOLIO_NOTE}


def test_a_table_error_is_shown_not_hidden() -> None:
    blocks = report_blocks({"episodes_table": {"error": "boom"}})
    assert blocks["episodes"]["error"] == "boom"
    assert not nothing_to_read(blocks)


# --------------------------------------------------------------------------- real runs


def _server(tmp_path: Path, cfg: LabConfig, candles: pd.DataFrame, calls: list[Any]) -> Any:
    return build_server(
        cfg=cfg,
        ledger_path=tmp_path / "ledger.db",
        loader=loader_for(candles, calls),
        research_config_path=write_criteria(tmp_path / "criteria.toml"),
    )


def _run(
    tmp_path: Path, *, episodes: tuple[Episode, ...] = EPISODES, candles: pd.DataFrame | None = None
) -> dict[str, Any]:
    """A backtest and report of ``TREND_PLAN`` through the real server, on synthetic candles."""
    cfg = replace(small_cfg(tmp_path), episodes=episodes)
    calls: list[pd.Timestamp] = []
    server = _server(tmp_path, cfg, trend_candles() if candles is None else candles, calls)
    run_id = ok(server, "start_run", {"label": "episodes"})["run_id"]
    bt = ok(server, "run_backtest", {"run_id": run_id, "plan": TREND_PLAN})
    h = plan_hash(parse_plan(TREND_PLAN))
    report = ok(server, "get_report", {"run_id": run_id, "plan_hash": h})
    return {
        "run_id": run_id,
        "cfg": cfg,
        "server": server,
        "plan_hash": h,
        "calls": calls,
        "backtest": bt,
        "report": report,
        "ledger_path": tmp_path / "ledger.db",
    }


def test_golden_report_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A table row, a no-bars row and holdout-dropped episodes, pinned end to end."""
    monkeypatch.setattr(runs, "new_run_id", lambda now=None: RUN_ID)
    out = _run(tmp_path)
    assert out["run_id"] == RUN_ID
    snapshot = {
        "episodes": out["report"]["episodes"],
        "turnover": out["report"]["turnover"],
        "evidence_table": out["backtest"]["episodes_table"],
    }
    actual = json.dumps(snapshot, sort_keys=True, indent=1) + "\n"
    if not FIXTURE.exists():
        if os.environ.get(REGEN_ENV) == "1":
            FIXTURE.write_text(actual)
            pytest.fail(f"wrote {FIXTURE.name}; re-run without {REGEN_ENV} to compare against it")
        pytest.fail(f"{FIXTURE} is missing; it is captured once with {REGEN_ENV}=1")
    assert actual == FIXTURE.read_text()


def test_real_run_table_has_each_kind_of_row(tmp_path: Path) -> None:
    out = _run(tmp_path)
    episodes = out["report"]["episodes"]
    assert (
        episodes["note"]
        == EPISODES_INFORMATION_NOTE
        == ("information only: not a criterion, not a trial")
    )
    by_name = {r["name"]: r for r in episodes["rows"]}
    assert by_name["before_the_data"]["bars"] == 0
    assert by_name["before_the_data"]["note"] == NO_BARS_NOTE
    winter = by_name["winter_2019"]
    assert winter["bars"] == 31 * 6
    assert winter["strategy_return"] is not None and winter["buy_hold_return"] is not None
    assert 0.0 <= winter["exposure"] <= 1.0
    assert by_name["touches_holdout"]["bars"] == 30 * 6
    assert episodes["dropped_for_holdout"] == ["crosses_holdout", "inside_holdout"]


def test_real_run_turnover_is_the_base_trials(tmp_path: Path) -> None:
    out = _run(tmp_path)
    net = out["backtest"]["metrics"]["net"]
    turnover = out["report"]["turnover"]
    assert turnover["traded_value_per_year"] == net["turnover"]
    assert turnover["trades_per_year"] == pytest.approx(
        net["n_trades"] * bars_per_year("4h") / net["bars"]
    )
    assert turnover["persist_settings"] == []


def test_the_table_is_not_a_trial_and_not_a_criterion(tmp_path: Path) -> None:
    (tmp_path / "with").mkdir()
    (tmp_path / "without").mkdir()
    a = _run(tmp_path / "with")
    b = _run(tmp_path / "without", episodes=())
    with Ledger(a["ledger_path"]) as la, Ledger(b["ledger_path"]) as lb:
        assert la.count(a["run_id"]) == lb.count(b["run_id"])
        assert len(la.evidence(a["run_id"])) == len(lb.evidence(b["run_id"]))
    assert a["report"]["rows"] == b["report"]["rows"]
    assert a["report"]["pre_holdout_passed"] == b["report"]["pre_holdout_passed"]
    assert a["report"]["statistics"] == b["report"]["statistics"]
    assert not {"episodes", "episodes_table"} & {r["criterion"] for r in a["report"]["rows"]}


def test_the_table_never_reads_a_holdout_bar(tmp_path: Path) -> None:
    """Rewriting every bar from the holdout on leaves the table bit-identical, and the only end
    ever asked of the loader is ``holdout_start``."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    candles = trend_candles()
    changed = candles.copy()
    mask = changed.index >= HOLDOUT
    assert mask.any()
    for col in ("open", "high", "low", "close"):
        changed.loc[mask, col] = changed.loc[mask, col] * 3.0
    a = _run(tmp_path / "a", candles=candles)
    b = _run(tmp_path / "b", candles=changed)
    assert a["backtest"]["episodes_table"] == b["backtest"]["episodes_table"]
    assert set(a["calls"]) == {HOLDOUT}


def test_edge_report_with_no_episode_row_and_no_persist_keeps_its_shape(tmp_path: Path) -> None:
    """The real ``[episodes]`` all post-date the tests' 2019-10 holdout: the case
    ``tests/test_report_golden.py`` pins. The evidence still lists them."""
    lab = Lab(tmp_path)
    run_id = lab.start()
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": TREND_PLAN})
    assert bt["episodes_table"]["rows"] == []
    assert len(bt["episodes_table"]["dropped_for_holdout"]) == len(lab.cfg.episodes) > 0
    h = plan_hash(parse_plan(TREND_PLAN))
    report = ok(lab.server, "get_report", {"run_id": run_id, "plan_hash": h})
    assert not {"episodes", "episodes_note", "turnover"} & set(report)


def test_evidence_that_predates_the_table_reads_null(tmp_path: Path) -> None:
    out = _run(tmp_path)
    with Ledger(out["ledger_path"]) as led:
        stored = led.evidence(out["run_id"], plan_hash=out["plan_hash"], kind="backtest")
        old = {k: v for k, v in stored[-1]["payload"].items() if k != "episodes_table"}
        record_report(led, out["run_id"], out["plan_hash"], "backtest", old)
    args = {"run_id": out["run_id"], "plan_hash": out["plan_hash"]}
    report = ok(out["server"], "get_report", args)
    assert report["episodes"] is None
    assert report["episodes_note"] == EPISODES_PREDATE_NOTE
    assert report["rows"] == out["report"]["rows"]
    assert report["turnover"] == out["report"]["turnover"]


def test_get_report_re_runs_nothing(tmp_path: Path) -> None:
    out = _run(tmp_path)
    with Ledger(out["ledger_path"]) as led:
        before = led.count(out["run_id"]), len(led.evidence(out["run_id"]))
        report = evaluate_plan(led, out["run_id"], out["plan_hash"], out["cfg"])
        assert report["episodes"] == out["report"]["episodes"]
        assert (led.count(out["run_id"]), len(led.evidence(out["run_id"]))) == before


def test_a_table_failure_keeps_the_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base trial is already ledgered: an information-only failure must not lose it."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr("lab.research.pipeline.episodes_evidence", boom)
    bt = _run(tmp_path)["backtest"]
    assert "boom" in bt["episodes_table"]["error"]
    assert bt["metrics"]["net"]["bars"] > 0


def _overlay_run(tmp_path: Path, episodes: tuple[Episode, ...]) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = replace(overlay_cfg(tmp_path), episodes=episodes)
    server = build_server(
        cfg=cfg,
        ledger_path=tmp_path / "ledger.db",
        loader=loader_for(CRASH_CANDLES),
        research_config_path=write_criteria(tmp_path / "criteria.toml"),
        overlay_config_path=overlay_toml(tmp_path),
    )
    start = {"label": "ov", "question": "risk_overlay"}
    run_id = ok(server, "start_run", start)["run_id"]
    bt = ok(server, "run_backtest", {"run_id": run_id, "plan": MA_OVERLAY})
    args = {"run_id": run_id, "plan_hash": bt["plan_hash"]}
    return {
        "run_id": run_id,
        "cfg": cfg,
        "backtest": bt,
        "report": ok(server, "get_report", args),
        "ledger_path": tmp_path / "ledger.db",
    }


def test_overlay_table_reads_the_overlay_blocks_own_bars(tmp_path: Path) -> None:
    out = _overlay_run(tmp_path, EPISODES)
    with Ledger(out["ledger_path"]) as led:
        _, pair = overlay_block_and_pair(
            MA_OVERLAY,
            cfg=out["cfg"],
            ledger=led,
            run_id=out["run_id"],
            loader=loader_for(CRASH_CANDLES),
            criteria=frozen_overlay_criteria(led, out["run_id"]),
        )
    episodes = out["report"]["episodes"]
    assert episodes["note"] == EPISODES_INFORMATION_NOTE
    assert episodes["dropped_for_holdout"] == ["crosses_holdout", "inside_holdout"]
    row = next(r for r in episodes["rows"] if r["name"] == "winter_2019")
    inside = (pair.index >= ts("2019-01-01")) & (pair.index < ts("2019-02-01"))
    assert row["bars"] == int(inside.sum())
    assert row["strategy_return"] == pytest.approx(np.prod(1.0 + pair.overlay_returns[inside]) - 1)
    assert row["buy_hold_return"] == pytest.approx(np.prod(1.0 + pair.buy_hold_returns[inside]) - 1)
    assert row["exposure"] == pytest.approx(pair.invested[inside].mean())
    assert (
        out["report"]["turnover"]["traded_value_per_year"]
        == (out["backtest"]["metrics"]["net"]["turnover"])
    )


def test_overlay_table_is_not_a_trial_and_not_a_criterion(tmp_path: Path) -> None:
    a = _overlay_run(tmp_path / "with", EPISODES)
    b = _overlay_run(tmp_path / "without", ())
    with Ledger(a["ledger_path"]) as la, Ledger(b["ledger_path"]) as lb:
        assert la.count(a["run_id"]) == lb.count(b["run_id"])
    assert a["report"]["rows"] == b["report"]["rows"]
    assert a["report"]["overlay"] == b["report"]["overlay"]


def test_overlay_report_lists_episodes_dropped_for_the_holdout(tmp_path: Path) -> None:
    out = _overlay_run(tmp_path, EPISODES[3:])
    episodes = out["report"]["episodes"]
    assert episodes["rows"] == []
    assert episodes["dropped_for_holdout"] == ["crosses_holdout", "inside_holdout"]


def test_instructions_teach_persist_and_hmm_filters() -> None:
    text = " ".join(INSTRUCTIONS.split())
    assert "persist(<condition>, k)" in text
    assert "hmm_bear_prob" in text and "never direction signals" in text
    assert "not a criterion, not a trial" in text
