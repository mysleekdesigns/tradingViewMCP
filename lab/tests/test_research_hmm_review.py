"""Phase 20 review fixes: the HMM label diagnostic in reports and the cap on HMM overlay logics.

Synthetic data only: hand-built ``hmm_series_*`` columns on seeded candles, tmp ledgers, the real
MCP server. The label flag is added here by its agreed name, so these tests do not depend on the
data layer producing it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from lab.mcp_server import INSTRUCTIONS
from lab.registry.hmm import HMM_BEAR_PROB_COLUMN
from lab.research.episodes import (
    HMM_DIAGNOSTICS_NOTE,
    hmm_label_diagnostics,
    nothing_to_read,
    persist_settings,
    report_blocks,
)
from lab.research.overlay_criteria import MAX_HMM_OVERLAY_LOGICS
from tests.synthetic import with_hmm
from tests.test_mcp_overlay_pipeline import OverlayLab
from tests.test_mcp_server import HOLDOUT_START, TREND_PLAN, error_text, ok
from tests.test_research_overlay import CRASH_CANDLES, MA_OVERLAY

FLAG = "hmm_series_bear_high_vol"  # lab.registry.hmm.HMM_BEAR_HIGH_VOL_COLUMN
HOLDOUT = pd.Timestamp(HOLDOUT_START, tz="UTC")


def hmm_overlay(name: str, rule: str) -> dict[str, Any]:
    return {
        "kind": "overlay",
        "name": name,
        "symbol": "BTCUSDT",
        "interval": "4h",
        "features": {"b": {"hmm_bear_prob": {}}},
        "flat_when": rule,
    }


MA_HMM = hmm_overlay("flat_bear", "b > 0.92")
EDGE_GATE: dict[str, Any] = {
    "name": "ema_hmm_gate",
    "symbol": "BTCUSDT",
    "interval": "4h",
    "features": {
        "f": {"ema": {"period": 20}},
        "s": {"ema": {"period": 50}},
        "b": {"hmm_bear_prob": {}},
    },
    "long_entry": "f > s and b < 0.5",
    "long_exit": "f < s",
}


def flagged_candles() -> pd.DataFrame:
    """``with_hmm`` candles (10% warm-up, one later gap) plus the label flag: high-vol bear for
    the first half of the decided bars, calm for the next quarter, high-vol again after."""
    out = with_hmm(CRASH_CANDLES, seed=1)
    decided = np.flatnonzero(np.isfinite(out[HMM_BEAR_PROB_COLUMN].to_numpy()))
    flag = np.full(len(out), np.nan)
    flag[decided] = 1.0
    flag[decided[len(decided) // 2 : 3 * len(decided) // 4]] = 0.0
    out[FLAG] = flag
    return out


# --------------------------------------------------------------------------- the diagnostic


def test_diagnostic_has_the_hand_computed_answer() -> None:
    nan = float("nan")
    out = hmm_label_diagnostics(np.array([nan, nan, 1.0, 1.0, 0.0, nan, 0.0, 1.0]))
    assert out["bars"] == 5
    assert out["share_bear_is_high_vol"] == pytest.approx(3 / 5)
    assert out["flips"] == 2  # a gap between two calm bars is not a flip
    assert out["n_flat_warmup_bars"] == 2
    assert out["note"] == HMM_DIAGNOSTICS_NOTE
    assert "information only" in out["note"]


def test_diagnostic_omits_warmup_when_there_is_none() -> None:
    assert "n_flat_warmup_bars" not in hmm_label_diagnostics(np.array([0.0, 1.0]))


def test_diagnostic_with_no_decision_is_null_not_nan() -> None:
    nan = float("nan")
    out = hmm_label_diagnostics(np.array([nan, nan]))
    assert out["bars"] == 0 and out["flips"] == 0
    assert out["share_bear_is_high_vol"] is None
    assert out["n_flat_warmup_bars"] == 2


def test_report_blocks_pass_the_diagnostic_through_only_when_recorded() -> None:
    table = {"rows": [], "dropped_for_holdout": []}
    without = report_blocks({"episodes_table": table})
    assert "hmm_diagnostics" not in without and nothing_to_read(without)
    block = {"bars": 3}
    with_block = report_blocks({"episodes_table": table, "hmm_diagnostics": block})
    assert with_block["hmm_diagnostics"] == block
    assert not nothing_to_read(with_block)


def expected(frame: pd.DataFrame, plan_window_start: int) -> dict[str, Any]:
    start = pd.Timestamp(plan_window_start, unit="s", tz="UTC")
    return hmm_label_diagnostics(
        frame.loc[(frame.index >= start) & (frame.index < HOLDOUT), FLAG].to_numpy()
    )


@pytest.mark.parametrize(
    "plan", [hmm_overlay("flat_bear", "b > 0.92"), EDGE_GATE], ids=["overlay", "edge"]
)
def test_diagnostic_through_the_real_evidence_path(tmp_path: Path, plan: dict[str, Any]) -> None:
    frame = flagged_candles()
    lab = OverlayLab(tmp_path, candles=frame)
    run_id = lab.overlay() if plan.get("kind") == "overlay" else lab.edge()
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": plan})
    want = expected(frame, bt["window"]["start"])
    assert bt["hmm_diagnostics"] == want
    assert want["flips"] == 2 and want["bars"] > 0
    assert lab.report(run_id, bt["plan_hash"])["hmm_diagnostics"] == want


@pytest.mark.parametrize(
    "plan", [hmm_overlay("flat_bear", "b > 0.92"), EDGE_GATE], ids=["overlay", "edge"]
)
def test_diagnostic_is_not_a_trial_and_not_a_criterion(
    tmp_path: Path, plan: dict[str, Any]
) -> None:
    """With and without the flag column: same trials, same evidence rows, same criterion rows."""
    out = []
    for name, frame in (
        ("flag", flagged_candles()),
        ("noflag", flagged_candles().drop(columns=FLAG)),
    ):
        (tmp_path / name).mkdir()
        lab = OverlayLab(tmp_path / name, candles=frame)
        run_id = lab.overlay() if plan.get("kind") == "overlay" else lab.edge()
        bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": plan})
        with lab.ledger() as led:
            counts = led.count(run_id), len(led.evidence(run_id))
        out.append((bt, counts, lab.report(run_id, bt["plan_hash"])))
    (with_bt, with_counts, with_report), (no_bt, no_counts, no_report) = out
    assert with_counts == no_counts
    assert with_report["rows"] == no_report["rows"]
    assert "error" not in with_bt["hmm_diagnostics"]
    assert FLAG in no_bt["hmm_diagnostics"]["error"], "a missing column is said, not hidden"
    assert no_report["hmm_diagnostics"] == no_bt["hmm_diagnostics"]


def test_the_diagnostic_never_reads_a_holdout_bar(tmp_path: Path) -> None:
    a_frame, b_frame = flagged_candles(), flagged_candles()
    b_frame.loc[b_frame.index >= HOLDOUT, FLAG] = 0.0
    assert (a_frame[FLAG] != b_frame[FLAG]).any()
    got = []
    for name, frame in (("a", a_frame), ("b", b_frame)):
        (tmp_path / name).mkdir()
        lab = OverlayLab(tmp_path / name, candles=frame)
        bt = ok(lab.server, "run_backtest", {"run_id": lab.overlay(), "plan": MA_HMM})
        got.append(bt["hmm_diagnostics"])
        assert set(lab.calls) == {HOLDOUT}
    assert got[0] == got[1]


def test_plan_without_hmm_features_has_no_key(tmp_path: Path) -> None:
    lab = OverlayLab(tmp_path)
    run_id = lab.edge()
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": TREND_PLAN})
    assert "hmm_diagnostics" not in bt
    assert "hmm_diagnostics" not in lab.report(run_id, bt["plan_hash"])


# --------------------------------------------------------------------------- the slate cap


def test_slate_constant_is_part_d() -> None:
    assert MAX_HMM_OVERLAY_LOGICS == 2
    assert "at most 2" in " ".join(INSTRUCTIONS.split())


def _counts(lab: OverlayLab, run_id: str) -> tuple[int, int]:
    with lab.ledger() as led:
        return led.count(run_id), len(led.evidence(run_id))


def test_third_hmm_overlay_rewrite_is_refused_before_any_trial(tmp_path: Path) -> None:
    lab = OverlayLab(tmp_path, candles=flagged_candles())
    run_id = lab.overlay()
    first = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": MA_HMM})
    second = ok(
        lab.server,
        "run_backtest",
        {"run_id": run_id, "plan": hmm_overlay("rewrite_1", "not (b <= 0.8)")},
    )
    before = _counts(lab, run_id)
    text = error_text(
        lab.server,
        "run_backtest",
        {"run_id": run_id, "plan": hmm_overlay("rewrite_2", "1 - b < 0.2")},
    )
    assert "at most 2" in text and "HMM" in text and "Part D" in text
    assert first["logic_hash"][:12] in text and second["logic_hash"][:12] in text
    assert _counts(lab, run_id) == before


def test_the_cap_spans_every_overlay_run(tmp_path: Path) -> None:
    lab = OverlayLab(tmp_path, candles=flagged_candles())
    run_id = lab.overlay()
    for name, rule in (("a", "b > 0.92"), ("c", "not (b <= 0.8)")):
        ok(lab.server, "run_backtest", {"run_id": run_id, "plan": hmm_overlay(name, rule)})
    other = lab.overlay()
    text = error_text(
        lab.server, "run_backtest", {"run_id": other, "plan": hmm_overlay("d", "1 - b < 0.2")}
    )
    assert "at most 2" in text
    assert _counts(lab, other) == (0, 0)


def test_resubmitting_a_slate_logic_is_left_to_the_existing_rules(tmp_path: Path) -> None:
    lab = OverlayLab(tmp_path, candles=flagged_candles())
    run_id = lab.overlay()
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": MA_HMM})
    ok(
        lab.server,
        "run_backtest",
        {"run_id": run_id, "plan": hmm_overlay("rewrite_1", "not (b <= 0.8)")},
    )
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": MA_HMM})  # the same plan again
    text = error_text(  # same logic, new literal: rule 1, not the cap
        lab.server, "run_backtest", {"run_id": run_id, "plan": hmm_overlay("x", "b > 0.8")}
    )
    assert "logic already tried" in text and "at most 2" not in text


def test_non_hmm_overlay_is_unaffected_by_a_full_slate(tmp_path: Path) -> None:
    lab = OverlayLab(tmp_path, candles=flagged_candles())
    run_id = lab.overlay()
    for name, rule in (("a", "b > 0.92"), ("c", "not (b <= 0.8)")):
        ok(lab.server, "run_backtest", {"run_id": run_id, "plan": hmm_overlay(name, rule)})
    bt = ok(lab.server, "run_backtest", {"run_id": run_id, "plan": MA_OVERLAY})
    assert "hmm_diagnostics" not in bt


# --------------------------------------------------------------------------- real persist


def test_persist_settings_on_real_rules() -> None:
    plan = {**TREND_PLAN, "long_entry": "persist(ema_fast > ema_slow, 3)"}
    plan["long_exit"] = "persist(ema_fast < ema_slow, 2)"
    assert persist_settings(plan) == [3, 2]
    overlay = {**MA_OVERLAY, "flat_when": "persist(close < ma, 4)"}
    assert persist_settings(overlay) == [4]


# --------------------------------------------------------------------------- old runs


def old_run(lab: OverlayLab, monkeypatch: pytest.MonkeyPatch, *, overlay: bool) -> str:
    """A run started as if before Phase 20: nothing frozen under ``hmm``."""
    with monkeypatch.context() as patch:
        patch.setattr("lab.research.runs.hmm_values", lambda _cfg: None)
        return lab.overlay() if overlay else lab.edge()


@pytest.mark.parametrize(
    ("tool", "extra"),
    [
        ("run_backtest", {}),
        ("run_sweep", {"space": {"f.period": {"low": 10, "high": 30}}, "n_trials": 2}),
        ("walk_forward", {"space": {"f.period": {"low": 10, "high": 30}}, "n_trials_per_fold": 2}),
        ("run_adversarial", {}),
        ("run_crosscheck", {}),
        ("holdout_check", {}),
    ],
)
def test_every_plan_tool_refuses_an_hmm_plan_in_an_old_edge_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, extra: dict[str, Any]
) -> None:
    lab = OverlayLab(tmp_path, candles=flagged_candles())
    run_id = old_run(lab, monkeypatch, overlay=False)
    before = _counts(lab, run_id)
    text = error_text(lab.server, tool, {"run_id": run_id, "plan": EDGE_GATE, **extra})
    assert "froze no HMM settings" in text
    assert _counts(lab, run_id) == before


@pytest.mark.parametrize("tool", ["run_backtest", "run_adversarial", "run_crosscheck"])
def test_every_overlay_tool_refuses_an_hmm_plan_in_an_old_overlay_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    lab = OverlayLab(tmp_path, candles=flagged_candles())
    run_id = old_run(lab, monkeypatch, overlay=True)
    text = error_text(lab.server, tool, {"run_id": run_id, "plan": MA_HMM})
    assert "froze no HMM settings" in text


def test_a_plan_without_hmm_features_still_runs_in_an_old_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = OverlayLab(tmp_path, candles=flagged_candles())
    run_id = old_run(lab, monkeypatch, overlay=True)
    ok(lab.server, "run_backtest", {"run_id": run_id, "plan": MA_OVERLAY})
