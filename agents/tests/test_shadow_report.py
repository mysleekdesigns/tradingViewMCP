"""Shadow-judge report (Phase 17): fill matching, the frozen evaluation time, the frozen
minimum sample, the cluster permutation test, the pinned model, unavailable/missed accounting,
read-only DB loading, and deterministic Markdown. All fixtures are hand-built (offline), with
known answers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from research.paper.compare import ForwardTrade
from research.paper.shadow_report import (
    DEFAULT_ANALYSIS,
    REPORT_DISCLAIMER,
    FinalReportRefusedError,
    MatchedTrade,
    build_report,
    cluster_permutation_test,
    current_env,
    frozen_analysis,
    informative_counts,
    load_trades,
    match_fills,
    model_pin,
    render_report_markdown,
    strata_summary,
    trades_sha256,
    within_block_effect,
)

H4 = 4 * 3600
START = 1_800_000_000  # a 4h bar boundary (1_800_000_000 / 14_400 = 125_000)
CAND = "hc-btc"
CAND2 = "hc-eth"
EVAL = START + 900 * H4  # the frozen evaluation time of the fixture genesis


def genesis(**analysis: Any) -> dict[str, Any]:
    return {
        "kind": "genesis",
        "written_at": START - 600,
        "start": START,
        "interval": "4h",
        "grace_s": 120,
        "candidates": [
            {"candidate": CAND, "symbol": "BTCUSDT", "plan_hash": "a" * 64},
            {"candidate": CAND2, "symbol": "ETHUSDT", "plan_hash": "b" * 64},
        ],
        "model": "jev-latest",  # the requested alias; decisions name the served id
        "analysis": {**frozen_analysis(EVAL), **analysis},
    }


def fill_open(k: int) -> int:
    return START + (k + 1) * H4


def decision(
    k: int,
    status: str = "judged",
    choice: str | None = "take",
    cand: str = CAND,
    model: str | None = "jev-test",
) -> dict[str, Any]:
    verdict = None if status != "judged" else {"choice": choice, "score": 0.6, "fakeout": 0.3}
    return {
        "kind": "decision",
        "signal_bar": START + k * H4,
        "fill_open": fill_open(k),
        "candidate": cand,
        "written_at": fill_open(k) - 60,
        "status": status,
        "snapshot": {},
        "snapshot_sha256": "0" * 64,
        "verdict": verdict,
        "model": model,
        "latency_ms": 10,
        "reason": None if status == "judged" else status,
    }


def bar(k: int, status: str = "judged") -> dict[str, Any]:
    return {
        "kind": "bar",
        "signal_bar": START + k * H4,
        "fill_open": fill_open(k),
        "written_at": fill_open(k) - 60,
        "status": status,
        "checked": [CAND, CAND2],
        "signals": [],
    }


def trade(
    open_s: int, ret: float | None = 0.01, *, is_open: bool = False, close_s: int | None = None
) -> ForwardTrade:
    closed = not is_open
    return ForwardTrade(
        pair="BTC/USDT",
        is_open=is_open,
        open_time=pd.Timestamp(open_s, unit="s", tz="UTC"),
        open_rate=100.0,
        close_time=pd.Timestamp(close_s or open_s + 3 * H4, unit="s", tz="UTC") if closed else None,
        close_rate=100.0 * (1 + (ret or 0.0)) if closed else None,
        close_profit=ret if closed else None,
        exit_reason="exit_signal" if closed else None,
    )


def scenario(
    take: list[float], skip: list[float], **analysis: Any
) -> tuple[list[dict[str, Any]], dict[str, list[ForwardTrade]]]:
    """One judged decision + matching closed trade per return; take first, then skip."""
    rows: list[dict[str, Any]] = [genesis(**analysis)]
    trades: list[ForwardTrade] = []
    for k, (choice, ret) in enumerate([("take", r) for r in take] + [("skip", r) for r in skip]):
        rows.append(bar(k))
        rows.append(decision(k, choice=choice))
        trades.append(trade(fill_open(k) + 5, ret))
    return rows, {CAND: trades, CAND2: []}


ASOF = START + 1000 * H4  # after EVAL: the effective cutoff is EVAL


# --------------------------------------------------------------------------- outcomes


def test_inconclusive_below_minimum_total() -> None:
    rows, trades = scenario([0.02] * 15, [-0.01] * 14)
    rep = build_report(rows, trades, asof=ASOF)
    assert (rep.take.n, rep.skip.n) == (15, 14)
    assert rep.outcome == "inconclusive"
    assert rep.p_value is None
    assert any("29 closed judged trades < the minimum 30" in r for r in rep.reasons)


def test_inconclusive_with_30_trades_but_7_in_one_arm() -> None:
    rows, trades = scenario([0.02] * 23, [-0.01] * 7)
    rep = build_report(rows, trades, asof=ASOF)
    assert rep.take.n + rep.skip.n == 30
    assert rep.outcome == "inconclusive"
    assert rep.p_value is None
    assert any("skip arm has 7 closed trades < the minimum 8" in r for r in rep.reasons)
    assert not any("closed judged trades <" in r for r in rep.reasons)


def test_large_clear_effect_is_take_better_with_small_p() -> None:
    take = [0.05 + 0.001 * (i % 5) for i in range(20)]
    skip = [-0.05 - 0.001 * (i % 5) for i in range(20)]
    rows, trades = scenario(take, skip)
    rep = build_report(rows, trades, asof=ASOF)
    assert rep.outcome == "take better"
    assert rep.p_value is not None and rep.p_value < 0.001
    assert rep.diff == pytest.approx(0.104)  # 0.052 - (-0.052)
    assert rep.take.mean == pytest.approx(0.052)
    assert rep.skip.mean == pytest.approx(-0.052)
    worse = build_report(*scenario(skip, take), asof=ASOF)
    assert worse.outcome == "take worse"


def test_identical_arms_give_no_information() -> None:
    vals = [0.03, -0.02, 0.01, -0.04, 0.05, 0.0, -0.01, 0.02, 0.04, -0.03]
    rows, trades = scenario(vals + vals[:5], vals + vals[:5])
    rep = build_report(rows, trades, asof=ASOF)
    assert rep.diff == pytest.approx(0.0)
    assert rep.p_value == pytest.approx(1.0)
    assert rep.outcome == "no information"


def test_analysis_comes_from_genesis_not_defaults() -> None:
    take = [0.05, 0.06, 0.05, 0.06]
    skip = [-0.05, -0.06, -0.05, -0.06]
    default = build_report(*scenario(take, skip), asof=ASOF)
    assert default.outcome == "inconclusive"
    frozen = build_report(
        *scenario(take, skip, min_judged_trades=8, min_per_arm=4, alpha=0.1), asof=ASOF
    )
    assert frozen.analysis["min_per_arm"] == 4
    assert frozen.p_value is not None  # 8 choose 4 = 70 splits; 2 as extreme -> p ~ 0.03
    assert frozen.outcome == "take better"


def test_missing_genesis_or_analysis_raises() -> None:
    rows, trades = scenario([0.01], [0.02])
    with pytest.raises(ValueError, match="genesis"):
        build_report(rows[1:], trades, asof=ASOF)
    no_eval = {k: v for k, v in rows[0]["analysis"].items() if k != "evaluate_at"}
    with pytest.raises(ValueError, match=r"missing keys \['evaluate_at'\]"):
        build_report([{**rows[0], "analysis": no_eval}, *rows[1:]], trades, asof=ASOF)
    old_test = {**rows[0]["analysis"], "test": "permutation_two_sided"}
    with pytest.raises(ValueError, match="cluster_permutation_two_sided"):
        build_report([{**rows[0], "analysis": old_test}, *rows[1:]], trades, asof=ASOF)
    no_analysis = {k: v for k, v in rows[0].items() if k != "analysis"}
    with pytest.raises(ValueError, match="analysis"):
        build_report([no_analysis, *rows[1:]], trades, asof=ASOF)
    partial = {**rows[0], "analysis": {"metric": DEFAULT_ANALYSIS["metric"]}}
    with pytest.raises(ValueError, match="missing keys"):
        build_report([partial, *rows[1:]], trades, asof=ASOF)
    with pytest.raises(ValueError, match="genesis"):
        build_report([], trades, asof=ASOF)


def test_frozen_analysis_is_the_defaults_plus_evaluate_at() -> None:
    got = frozen_analysis(EVAL)
    assert got == {**DEFAULT_ANALYSIS, "evaluate_at": EVAL}
    assert got["test"] == "cluster_permutation_two_sided" and got["cluster"] == "fill_open"
    assert got["model_rule"] == "first_served_model_major_minor"
    assert got["evaluate_after_days"] == 14
    assert got["strata"] == "same_size_within_block" and got["block_days"] == 7
    with pytest.raises(ValueError, match="evaluate_at"):
        frozen_analysis(float(EVAL))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- evaluation time


def test_interim_before_evaluate_at_has_no_test_and_no_outcome() -> None:
    take = [0.05 + 0.001 * (i % 5) for i in range(20)]
    skip = [-0.05 - 0.001 * (i % 5) for i in range(20)]
    rows, trades = scenario(take, skip)
    rep = build_report(rows, trades, asof=EVAL - 1)
    assert rep.outcome == "interim"
    assert rep.p_value is None
    assert rep.asof == EVAL - 1 and rep.evaluate_at == EVAL
    assert rep.diff == pytest.approx(0.104)  # still shown, as a description
    assert any("read only at" in r and str(EVAL) in r for r in rep.reasons)
    md = render_report_markdown(rep)
    assert "**Interim look: not the pre-registered test; no outcome, no p-value**" in md
    assert "take better" not in md and "take worse" not in md
    assert "**Outcome:" not in md
    assert "descriptive only" in md
    assert "permutation p-value of |T|: -." in md
    # the same data at the evaluation time is the pre-registered read
    assert build_report(rows, trades, asof=EVAL).outcome == "take better"


def _late_rows_and_trades() -> tuple[list[dict[str, Any]], dict[str, list[ForwardTrade]]]:
    rows, trades = scenario([0.02] * 3, [-0.01] * 3)
    late = 950  # decided and filled after the evaluation time
    rows += [bar(late), decision(late, choice="skip")]
    trades[CAND].append(trade(fill_open(late) + 5, 0.4))
    return rows, trades


def test_any_asof_at_or_after_evaluate_at_gives_the_same_report() -> None:
    rows, trades = _late_rows_and_trades()
    at = build_report(rows, trades, asof=EVAL)
    later = build_report(rows, trades, asof=EVAL + 500 * H4)
    assert at == later
    assert at.asof == EVAL
    assert render_report_markdown(at) == render_report_markdown(later)
    assert (at.take.n, at.skip.n) == (3, 3)  # the late decision and trade are ignored
    assert at.decisions["judged"] == 6 and at.fills_without_decision == 0


def test_trade_still_open_at_evaluate_at_is_excluded_and_counted() -> None:
    rows, trades = scenario([0.02] * 3, [-0.01] * 3)
    rows += [decision(895, choice="take"), decision(896, choice="skip")]
    # closes after evaluate_at, and exactly at it: neither closed *before* evaluate_at
    trades[CAND] += [
        trade(fill_open(895) + 5, 0.9, close_s=EVAL + H4),
        trade(fill_open(896) + 5, -0.9, close_s=EVAL),
    ]
    rep = build_report(rows, trades, asof=EVAL + 100 * H4)
    assert rep.judged_open == 2
    assert (rep.take.n, rep.skip.n) == (3, 3)
    assert [t.net_return for t in rep.trades if t.is_open] == [None, None]
    assert "still open: 2" in render_report_markdown(rep)


# --------------------------------------------------------------------------- clusters, model


def test_clusters_mixed_clusters_and_per_candidate_counts() -> None:
    rows = [genesis()]
    trades: dict[str, list[ForwardTrade]] = {CAND: [], CAND2: []}
    # bar 0: both coins, take + skip (mixed); bar 1: both coins, take + take; bar 2: one skip
    for k, cand, choice in [
        (0, CAND, "take"),
        (0, CAND2, "skip"),
        (1, CAND, "take"),
        (1, CAND2, "take"),
        (2, CAND2, "skip"),
    ]:
        rows.append(decision(k, choice=choice, cand=cand))
        trades[cand].append(trade(fill_open(k) + 5, 0.01))
    rep = build_report(rows, trades, asof=ASOF)
    assert (rep.take.n, rep.skip.n) == (3, 2)
    assert rep.clusters == 3
    assert rep.mixed_clusters == 1
    assert rep.per_candidate == {CAND: {"take": 2, "skip": 0}, CAND2: {"take": 1, "skip": 2}}
    md = render_report_markdown(rep)
    assert "n = 5 trades in 3 clusters" in md and "1 with both arms" in md
    assert f"| {CAND} | 2 | 0 |" in md and f"| {CAND2} | 1 | 2 |" in md
    assert "coin mix" in md


def test_other_model_decisions_are_excluded_and_counted() -> None:
    rows, trades = scenario([0.02, 0.03], [-0.01])
    rows.append(decision(5, choice="take", model="jev-other"))
    trades[CAND].append(trade(fill_open(5) + 5, 0.5))
    rep = build_report(rows, trades, asof=ASOF)
    assert rep.model == "jev-test"
    assert rep.model_mismatch == 1
    assert (rep.take.n, rep.skip.n) == (2, 1)
    assert rep.take.mean == pytest.approx(0.025)
    assert rep.fills_without_decision == 0  # its fill still belongs to a decision row
    assert len(rep.trades) == 3
    assert any("1 judged decisions by a model other than" in r for r in rep.reasons)
    assert "first judged decision, major.minor): `jev-test`" in render_report_markdown(rep)
    # the first judged decision by fill bar (not by row order) pins the model
    early = decision(-1, choice="skip", model="jev-other")
    swapped = build_report(
        [*rows, early],  # written last, but its fill bar is the earliest
        {CAND: [trade(fill_open(-1) + 5, 0.1), *trades[CAND]], CAND2: []},
        asof=ASOF,
    )
    assert swapped.model == "jev-other"
    assert swapped.model_mismatch == 3  # the three jev-test decisions
    assert (swapped.take.n, swapped.skip.n) == (1, 1)  # decisions 5 and -1
    assert swapped.take.mean == pytest.approx(0.5)


def test_judged_decision_without_served_model_is_excluded_and_counted() -> None:
    for bad in (None, "", "  "):
        rows, trades = scenario([0.02, 0.03], [-0.01])
        rows.append(decision(5, choice="take", model=bad))
        trades[CAND].append(trade(fill_open(5) + 5, 0.5))
        rep = build_report(rows, trades, asof=ASOF)  # no longer raises
        assert rep.model == "jev-test"
        assert rep.no_served_model == 1 and rep.model_mismatch == 0
        assert (rep.take.n, rep.skip.n) == (2, 1)
        assert rep.fills_without_decision == 0  # its fill still belongs to a decision row
        assert any("1 judged decisions with no served model id" in r for r in rep.reasons)
        assert "with no served model id 1" in render_report_markdown(rep)
    # the first judged decision without a served id does not pin "no model": the first one
    # with a served id does
    rows = [genesis(), decision(0, model=None), decision(1, choice="skip", model="jev-test")]
    rep = build_report(
        rows, {CAND: [trade(fill_open(0) + 5), trade(fill_open(1) + 5)], CAND2: []}, asof=ASOF
    )
    assert rep.model == "jev-test" and rep.no_served_model == 1
    assert (rep.take.n, rep.skip.n) == (0, 1)
    # unavailable / missed rows carry no verdict and need no model
    rows = [genesis(), decision(0, "unavailable", model=None)]
    assert build_report(rows, {CAND: []}, asof=ASOF).model is None


def test_model_pin_normalises_patch_releases_to_major_minor() -> None:
    assert model_pin("jev-1.13.0") == "jev-1.13" == model_pin("jev-1.13.1")
    assert model_pin(" jev-1.13.7 ") == "jev-1.13"
    assert model_pin("jev-1.14.0") == "jev-1.14"
    assert model_pin("jev-latest") == "jev-latest"  # not name-major.minor.patch: the full id
    assert model_pin("jev-1.13") == "jev-1.13"
    assert model_pin("jev-1.13.0-rc1") == "jev-1.13.0-rc1"
    assert model_pin(None) is None and model_pin("") is None and model_pin("  ") is None
    rows, trades = scenario(
        [0.02, 0.03],
        [-0.01],
    )
    rows = [rows[0], *({**r, "model": "jev-1.13.0"} if r["kind"] == "decision" else r
                       for r in rows[1:])]  # fmt: skip
    rows.append(decision(5, choice="skip", model="jev-1.13.1"))  # a patch release: same pin
    rows.append(decision(6, choice="take", model="jev-1.14.0"))  # a minor release: excluded
    trades[CAND] += [trade(fill_open(5) + 5, -0.02), trade(fill_open(6) + 5, 0.4)]
    rep = build_report(rows, trades, asof=ASOF)
    assert rep.model == "jev-1.13"
    assert (rep.take.n, rep.skip.n) == (2, 2)
    assert rep.model_mismatch == 1 and rep.no_served_model == 0


def test_chain_head_and_row_count_name_the_prefix_used() -> None:
    rows, trades = _late_rows_and_trades()
    rows = [{**r, "sha256": f"{i:064x}"} for i, r in enumerate(rows)]
    rep = build_report(rows, trades, asof=ASOF)
    used = len(rows) - 2  # the late bar and decision rows are written after evaluate_at
    assert rep.n_rows == used
    assert rep.chain_head == f"{used - 1:064x}"
    md = render_report_markdown(rep)
    assert f"chain head `{used - 1:064x}`" in md and f"{used} rows used" in md
    bare = build_report([{k: v for k, v in r.items() if k != "sha256"} for r in rows], trades,
                        asof=ASOF)  # fmt: skip
    assert bare.chain_head is None and "chain head `-`" in render_report_markdown(bare)


# --------------------------------------------------------------------------- accounting


def test_unavailable_and_missed_counted_but_excluded_from_arms() -> None:
    rows, trades = scenario([0.01, 0.02], [0.03])
    rows += [bar(10, "unavailable"), decision(10, "unavailable"), bar(11, "missed")]
    rows += [decision(11, "missed"), bar(12, "no_signal")]
    # the bots trade regardless of the judge: fills exist for the unavailable / missed bars
    trades[CAND] += [trade(fill_open(10) + 5, 0.5), trade(fill_open(11) + 5, -0.5)]
    rep = build_report(rows, trades, asof=ASOF)
    assert rep.decisions == {"judged": 3, "unavailable": 1, "missed": 1}
    assert rep.bars == {"judged": 3, "no_signal": 1, "unavailable": 1, "missed": 1}
    assert (rep.take.n, rep.skip.n) == (2, 1)
    assert rep.take.mean == pytest.approx(0.015)
    assert rep.skip.mean == pytest.approx(0.03)
    assert {t.arm for t in rep.trades} == {"take", "skip"}
    assert len(rep.trades) == 3
    assert rep.fills_without_decision == 0  # those fills belong to a decision row
    md = render_report_markdown(rep)
    assert "| Decisions | judged | unavailable | missed |" in md
    assert "| count | 3 | 1 | 1 |" in md


def test_judged_without_fill_and_fill_without_decision() -> None:
    rows, trades = scenario([0.01], [0.02])
    rows.append(decision(5, choice="skip"))  # bot already in a position: no paper entry
    trades[CAND2] = [trade(fill_open(7) + 5, 0.04)]  # a paper entry nobody judged
    trades[CAND].append(trade(START - H4, 0.04))  # before the shadow start: ignored
    rep = build_report(rows, trades, asof=ASOF)
    assert rep.judged_no_fill == 1
    assert rep.fills_without_decision == 1
    assert (rep.take.n, rep.skip.n) == (1, 1)


def test_open_trade_excluded_from_arms() -> None:
    rows, trades = scenario([0.01, 0.02], [0.03])
    rows.append(decision(6, choice="take"))
    trades[CAND].append(trade(fill_open(6) + 5, is_open=True))
    # closed in the DB, but only after asof: open as of the report
    rows.append(decision(7, choice="skip"))
    asof = fill_open(7) + H4
    trades[CAND].append(trade(fill_open(7) + 5, 0.9, close_s=asof + H4))
    rep = build_report(rows, trades, asof=asof)
    assert rep.judged_open == 2
    assert (rep.take.n, rep.skip.n) == (2, 1)
    opened = [t for t in rep.trades if t.is_open]
    assert [t.net_return for t in opened] == [None, None]


def test_a_trade_is_matched_once_only() -> None:
    decisions = [decision(0, choice="take"), decision(1, choice="skip")]
    one = {CAND: [trade(fill_open(0) + H4 - 10, 0.02)]}
    # with a two-bar window both decisions could claim the trade; only the first does
    got = match_fills(decisions, one, 2 * H4)
    f0 = fill_open(0)
    assert got == [
        MatchedTrade(CAND, START, f0, "take", 0.02, False, f0 + H4 - 10, f0 + 4 * H4 - 10),
    ]
    # two trades in one fill bar: one matched, the other is a fill without a decision
    rows = [genesis(), decision(0, choice="take")]
    two = {CAND: [trade(fill_open(0) + 5, 0.01), trade(fill_open(0) + 50, 0.03)]}
    rep = build_report(rows, two, asof=ASOF)
    assert len(rep.trades) == 1 and rep.trades[0].net_return == pytest.approx(0.01)
    assert rep.fills_without_decision == 1


def test_fill_outside_the_fill_bar_is_not_matched() -> None:
    decisions = [decision(0, choice="take")]
    lo = fill_open(0)
    assert match_fills(decisions, {CAND: [trade(lo + H4)]}, H4) == []  # next bar
    assert match_fills(decisions, {CAND: [trade(lo - 1)]}, H4) == []  # before the fill bar
    assert match_fills(decisions, {CAND2: [trade(lo + 5)]}, H4) == []  # another candidate
    assert len(match_fills(decisions, {CAND: [trade(lo)]}, H4)) == 1
    assert len(match_fills(decisions, {CAND: [trade(lo + H4 - 1)]}, H4)) == 1


def test_rows_written_at_or_after_asof_are_ignored() -> None:
    rows, trades = scenario([0.01, 0.02], [0.03])
    asof = fill_open(1) - 60  # decision 1 is written exactly at asof
    rep = build_report(rows, trades, asof=asof)
    assert rep.decisions["judged"] == 1
    assert rep.bars["judged"] == 1
    assert rep.fills_without_decision == 0  # the later trades opened after asof


def test_float_written_at_as_the_log_writes_it_is_accepted() -> None:
    rows, trades = scenario([0.01, 0.02], [0.03])
    floats = [{**r, "written_at": r["written_at"] + 0.25} if i else r for i, r in enumerate(rows)]
    assert render_report_markdown(build_report(floats, trades, asof=ASOF)) == (
        render_report_markdown(build_report(rows, trades, asof=ASOF))
    )
    with pytest.raises(ValueError, match="written_at"):
        build_report([rows[0], {**rows[1], "written_at": float("nan")}], trades, asof=ASOF)


def test_judged_decision_without_verdict_raises() -> None:
    rows = [genesis(), decision(0, choice=None)]
    with pytest.raises(ValueError, match="take/skip"):
        build_report(rows, {CAND: [trade(fill_open(0) + 5)]}, asof=ASOF)


# --------------------------------------------------------------------------- cluster test


def singletons(take: list[float], skip: list[float]) -> list[tuple[int, int, str, float]]:
    """One block, one trade per cluster: the ordinary two-sample permutation test."""
    vals = [("take", r) for r in take] + [("skip", r) for r in skip]
    return [(0, k, arm, r) for k, (arm, r) in enumerate(vals)]


def test_cluster_permutation_test_deterministic_and_bounded() -> None:
    trades = singletons([0.03, -0.01, 0.02, 0.05, -0.02], [0.01, -0.03, 0.0, -0.01, 0.02, -0.04])
    p1 = cluster_permutation_test(trades, n_permutations=2000, seed=17)
    p2 = cluster_permutation_test(list(reversed(trades)), n_permutations=2000, seed=17)
    assert p1 == p2  # same seed, any input order
    assert 0.0 < p1 <= 1.0
    same = singletons([1.0] * 3, [1.0] * 3)
    assert cluster_permutation_test(same, n_permutations=50, seed=1) == 1.0
    # singletons: 2 of C(20, 10) = 184756 splits are as extreme, so almost no draw hits
    tiny = cluster_permutation_test(singletons([1.0] * 10, [-1.0] * 10), n_permutations=999, seed=3)
    assert 1 / 1000 <= tiny <= 3 / 1000
    with pytest.raises(ValueError, match="both arms"):
        cluster_permutation_test([(0, 0, "skip", 0.1)], n_permutations=10, seed=1)
    with pytest.raises(ValueError, match="n_permutations"):
        cluster_permutation_test(singletons([0.1], [0.2]), n_permutations=0, seed=1)
    with pytest.raises(ValueError, match="arm"):
        cluster_permutation_test(
            [(0, 0, "maybe", 0.1), (0, 1, "take", 0.1)], n_permutations=5, seed=1
        )
    with pytest.raises(ValueError, match="finite"):
        cluster_permutation_test(singletons([float("nan")], [0.2]), n_permutations=5, seed=1)


def test_batched_bars_are_one_cluster_not_ten_independent_trades() -> None:
    """2 bars x 5 all-take wins and 2 bars x 5 all-skip losses. Treated as 20 iid trades the
    split is as extreme as 2 of C(20, 10) = 184756 splits (p ~ 1e-5). As 4 equal-size clusters
    only C(4, 2) = 6 label arrangements exist and 2 of them (observed and mirrored) are as
    extreme, so p ~ 1/3; any honest cluster test must give p >= 1/6 here."""
    take = [(0, bar_, "take", 0.05) for bar_ in (0, 1) for _ in range(5)]
    skip = [(0, bar_, "skip", -0.05) for bar_ in (2, 3) for _ in range(5)]
    clustered = take + skip
    p = cluster_permutation_test(clustered, n_permutations=2000, seed=17)
    assert p >= 1 / 6
    assert p == pytest.approx(1 / 3, abs=0.05)  # 2000 draws: sd of the share ~ 0.011
    iid = singletons([0.05] * 10, [-0.05] * 10)
    assert cluster_permutation_test(iid, n_permutations=2000, seed=17) < 0.01
    assert cluster_permutation_test(clustered, n_permutations=2000, seed=17) == p


def test_mixed_cluster_labels_are_reordered_within_the_cluster() -> None:
    # one bar judges one take and one skip; under the null either may be the take
    mixed = [(0, 0, "take", 0.05), (0, 0, "skip", -0.05)]
    p = cluster_permutation_test(mixed, n_permutations=999, seed=5)
    assert p == pytest.approx(1.0)  # |d*| = |d_obs| on both orders


# --------------------------------------------------------------------------- loading


def _write_db(path: Path, rows: list[tuple[Any, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN, "
        "open_date DATETIME, open_rate FLOAT, close_date DATETIME, close_rate FLOAT, "
        "amount FLOAT, fee_open FLOAT, fee_close FLOAT, close_profit FLOAT, exit_reason TEXT)"
    )
    conn.executemany(
        "INSERT INTO trades (pair, is_open, open_date, open_rate, close_date, close_rate, "
        "amount, fee_open, fee_close, close_profit, exit_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _iso(s: int) -> str:
    return pd.Timestamp(s, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M:%S.%f")


def test_load_trades_reads_pair_and_notes_missing_db(tmp_path: Path) -> None:
    f0, f1 = fill_open(0), fill_open(1)
    _write_db(
        tmp_path / CAND / "dryrun.sqlite",
        [
            ("BTC/USDT", 0, _iso(f0 + 3), 100.0, _iso(f0 + H4 * 2), 104.0, 1.0, 0.001, 0.001,
             0.038, "exit_signal"),
            ("ETH/USDT", 0, _iso(f0 + 3), 10.0, _iso(f0 + H4), 9.0, 1.0, 0.001, 0.001, -0.1,
             "exit_signal"),
            ("BTC/USDT", 1, _iso(f1 + 3), 105.0, None, None, 1.0, 0.001, None, None, None),
        ],
    )  # fmt: skip
    loaded = load_trades(tmp_path, genesis())
    trades, notes = loaded.trades, loaded.notes
    assert [t.pair for t in trades[CAND]] == ["BTC/USDT", "BTC/USDT"]
    assert trades[CAND][0].net_return() == pytest.approx(0.038)
    assert trades[CAND][1].is_open
    assert trades[CAND2] == []
    assert len(notes) == 1 and CAND2 in notes[0] and "not found" in notes[0]
    assert loaded.unreadable == frozenset({CAND2})  # structured, not parsed from the text


def test_load_trades_unreadable_db_is_a_note(tmp_path: Path) -> None:
    bad = tmp_path / CAND / "dryrun.sqlite"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"this is not a sqlite database at all, just bytes" * 4)
    loaded = load_trades(tmp_path, genesis())
    assert loaded.trades[CAND] == [] and loaded.trades[CAND2] == []
    assert len(loaded.notes) == 2
    assert loaded.unreadable == frozenset({CAND, CAND2})


def test_report_reproduces_from_jsonl_and_db_alone(tmp_path: Path) -> None:
    rows, _ = scenario([], [])
    db_rows: list[tuple[Any, ...]] = []
    for k, (choice, ret) in enumerate([("take", 0.02), ("skip", -0.01), ("take", 0.03)]):
        rows += [bar(k), decision(k, choice=choice)]
        f = fill_open(k)
        db_rows.append(
            ("BTC/USDT", 0, _iso(f + 3), 100.0, _iso(f + H4), 100.0 * (1 + ret), 1.0, 0.001,
             0.001, ret, "exit_signal")
        )  # fmt: skip
    rows.append(decision(3, "unavailable"))
    _write_db(tmp_path / CAND / "dryrun.sqlite", db_rows)
    _write_db(tmp_path / CAND2 / "dryrun.sqlite", [])  # readable, no trades: fine
    log = tmp_path / "decisions.jsonl"
    log.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))

    def run() -> str:
        loaded = [json.loads(line) for line in log.read_text().splitlines()]
        got = load_trades(tmp_path, loaded[0])
        return render_report_markdown(
            build_report(loaded, got.trades, asof=ASOF, unreadable=got.unreadable)
        )

    md = run()
    assert md == run()
    rep = build_report(rows, load_trades(tmp_path, rows[0]).trades, asof=ASOF)
    assert (rep.take.n, rep.skip.n) == (2, 1)
    assert rep.take.mean == pytest.approx(0.025)
    assert rep.decisions["unavailable"] == 1
    assert "| hc-btc | " in md and "+2.00%" in md


# --------------------------------------------------------------------------- markdown


def test_render_is_byte_identical_and_carries_the_disclaimer() -> None:
    rows, trades = scenario([0.02] * 5, [-0.01] * 5)
    a = render_report_markdown(build_report(rows, trades, asof=ASOF))
    b = render_report_markdown(build_report(rows, trades, asof=ASOF))
    assert a == b
    assert REPORT_DISCLAIMER in a
    assert "**Outcome: inconclusive**" in a
    assert "correct outcome" in a and "no edge is claimed" in a
    assert str(EVAL) in a and str(ASOF) not in a  # the cutoff is the frozen evaluate_at
    assert "descriptive only" in a
    for key in (*DEFAULT_ANALYSIS, "evaluate_at"):
        assert f"- {key}: " in a

    same = [0.01, -0.01] * 8
    none_md = render_report_markdown(build_report(*scenario(same, same), asof=ASOF))
    assert "**Outcome: no information**" in none_md and "correct outcome" in none_md

    take = [0.05 + 0.001 * i for i in range(20)]
    skip = [-0.05 - 0.001 * i for i in range(20)]
    pos = render_report_markdown(build_report(*scenario(take, skip), asof=ASOF))
    assert "**Outcome: take better**" in pos
    assert "second, separately pre-registered forward test" in pos
    assert "never trading" in pos


# --------------------------------------------------------------------------- round-2 fixes


def test_final_report_refuses_an_unreadable_dryrun_db_but_interim_shows_it(
    tmp_path: Path,
) -> None:
    """A missing / locked / corrupt DB must never silently drop a candidate from the final
    report: it refuses (retry later). The interim look carries the note instead."""
    rows, _ = scenario([], [])
    f = fill_open(0)
    rows += [bar(0), decision(0, choice="take")]
    _write_db(
        tmp_path / CAND / "dryrun.sqlite",
        [("BTC/USDT", 0, _iso(f + 3), 100.0, _iso(f + H4), 101.0, 1.0, 0.001, 0.001, 0.01,
          "exit_signal")],
    )  # fmt: skip
    bad = tmp_path / CAND2 / "dryrun.sqlite"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"locked or corrupt, not sqlite" * 8)
    loaded = load_trades(tmp_path, rows[0])
    assert loaded.unreadable == frozenset({CAND2})
    with pytest.raises(FinalReportRefusedError, match=rf"final report refused: {CAND2} dry-run "
                       r"DB unreadable; retry"):  # fmt: skip
        build_report(rows, loaded.trades, asof=ASOF, notes=loaded.notes,
                     unreadable=loaded.unreadable)  # fmt: skip
    interim = build_report(
        rows, loaded.trades, asof=EVAL - 1, notes=loaded.notes, unreadable=loaded.unreadable
    )
    md = render_report_markdown(interim)
    assert interim.outcome == "interim" and interim.notes == loaded.notes
    assert "## Notes" in md and "could not be read" in md and CAND2 in md
    assert any("unreadable" in r and CAND2 in r for r in interim.reasons)
    # once the DB reads (zero trades is fine) the final report is built
    bad.unlink()
    _write_db(bad, [])
    fixed = load_trades(tmp_path, rows[0])
    assert fixed.unreadable == frozenset() and fixed.notes == []
    final = build_report(rows, fixed.trades, asof=ASOF, notes=fixed.notes,
                         unreadable=fixed.unreadable)  # fmt: skip
    assert final.outcome == "inconclusive" and "## Notes" not in render_report_markdown(final)


def test_trades_sha256_names_the_matched_trade_list_and_is_stable_after_evaluate_at() -> None:
    rows, trades = _late_rows_and_trades()
    at = build_report(rows, trades, asof=EVAL)
    later = build_report(rows, trades, asof=EVAL + 300 * H4)
    assert at.trades_sha256 == later.trades_sha256 == trades_sha256(at.trades)
    assert f"Matched trades sha256 `{at.trades_sha256}`" in render_report_markdown(at)
    body = [
        {
            "arm": m.arm,
            "candidate": m.candidate,
            "close_time": m.close_time,
            "fill_open": m.fill_open,
            "net_return": m.net_return,
            "open_time": m.open_time,
            "signal_bar": m.signal_bar,
        }
        for m in at.trades
    ]
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert at.trades_sha256 == hashlib.sha256(text.encode()).hexdigest()
    # a changed return changes the hash
    changed = {CAND: [*trades[CAND][:-2], trade(fill_open(len(trades[CAND]) - 2) + 5, 0.5),
                      trades[CAND][-1]], CAND2: []}  # fmt: skip
    assert build_report(rows, changed, asof=EVAL).trades_sha256 != at.trades_sha256


def test_regime_persisting_across_weeks_is_not_mistaken_for_discrimination() -> None:
    """Week 0: every setup taken and every trade wins; week 1: every setup skipped and every
    trade loses. A label that only tracks the week (a regime) is perfectly 'right' by the iid
    test, but inside each week there is nothing to permute: p = 1 exactly."""
    week0 = [(0, k, "take", 0.04 + 0.001 * k) for k in range(10)]
    week1 = [(1, 100 + k, "skip", -0.04 - 0.001 * k) for k in range(10)]
    stratified = cluster_permutation_test(week0 + week1, n_permutations=2000, seed=17)
    assert stratified == pytest.approx(1.0)
    assert strata_summary(week0 + week1) == (2, 0)
    pooled = [(0, k, arm, r) for _, k, arm, r in week0 + week1]
    assert cluster_permutation_test(pooled, n_permutations=2000, seed=17) < 0.001
    assert strata_summary(pooled) == (1, 1)


def _centred(trades: list[tuple[int, int, str, float]], take: set[int]) -> float:
    """The spec's centred statistic computed from its definition, independently of the code:
    d(S) - d(E0[S]) with d(S) = S / n_take - (total - S) / n_skip and E0[S] = sum over blocks
    of (n_take_b / n_b) * sum_b (singleton clusters: one stratum per block)."""
    rets = [r for *_, r in trades]
    n_take, n_skip, total = len(take), len(trades) - len(take), sum(rets)
    centre = 0.0
    for b in {blk for blk, *_ in trades}:
        idx = [i for i, (blk, *_) in enumerate(trades) if blk == b]
        centre += sum(1 for i in idx if i in take) / len(idx) * sum(rets[i] for i in idx)

    def d(s: float) -> float:
        return s / n_take - (total - s) / n_skip

    return d(sum(rets[i] for i in take)) - d(centre)


def _exact_p(trades: list[tuple[int, int, str, float]], stat: Any) -> float:
    """Exact stratified p: every arrangement keeping each block's take count (singletons)."""
    import itertools

    blocks = sorted({b for b, *_ in trades})
    per = [
        itertools.combinations(
            [i for i, (blk, *_) in enumerate(trades) if blk == b],
            sum(1 for blk, _, a, _ in trades if blk == b and a == "take"),
        )
        for b in blocks
    ]
    obs = abs(stat(trades, {i for i, (_, _, a, _) in enumerate(trades) if a == "take"}))
    hits = total = 0
    for combo in itertools.product(*(list(c) for c in per)):
        take = {i for part in combo for i in part}
        total += 1
        hits += abs(stat(trades, take)) >= obs - 1e-12
    return hits / total


def _pooled(trades: list[tuple[int, int, str, float]], take: set[int]) -> float:
    rets = [r for *_, r in trades]
    return float(
        np.mean([rets[i] for i in take])
        - np.mean([rets[i] for i in range(len(rets)) if i not in take])
    )


def test_within_block_discrimination_is_detected_and_matches_exact_enumeration() -> None:
    trades = [
        (0, 0, "take", 0.03), (0, 1, "take", 0.02), (0, 2, "skip", -0.01), (0, 3, "skip", 0.0),
        (1, 4, "take", 0.01), (1, 5, "skip", -0.03), (1, 6, "skip", -0.02),
    ]  # fmt: skip
    # exact: C(4, 2) arrangements in block 0 x C(3, 1) in block 1 = 18, take count per block
    # fixed; the take shares differ (1/2 vs 1/3), so centring matters here
    exact = _exact_p(trades, _centred)
    p = cluster_permutation_test(trades, n_permutations=20000, seed=17)
    # 20000 draws: the Monte Carlo sd of p is at most 0.0035, so 0.01 is about 3 sd
    assert p == pytest.approx(exact, abs=0.01)
    assert exact < 0.2
    assert strata_summary(trades) == (2, 2)
    # T is the spec's d(S) - d(E0[S]) divided by a constant: a weighted mean of the
    # within-block differences (block 0: 0.025 - (-0.005) = 0.03, weight 1; block 1:
    # 0.01 - (-0.025) = 0.035, weight 2/3), so (0.03 + 0.035 * 2/3) / (5/3) = 0.032
    assert within_block_effect(trades) == pytest.approx(0.032)
    with pytest.raises(ValueError, match="blocks"):
        cluster_permutation_test([(0, 1, "take", 0.1), (1, 1, "skip", 0.2)], n_permutations=5,
                                 seed=1)  # fmt: skip


OFFSETS = (-0.006, -0.004, -0.002, 0.002, 0.004, 0.006)  # mean 0


def mirror() -> list[tuple[int, int, str, float]]:
    """The reviewer's mirror (W-r3-1): two weeks of 7 singleton clusters. Week 0 is good
    (about +5%) with 1 take and 6 skips, week 1 bad (about -5%) with 6 takes and 1 skip; inside
    each week the take beats the skips by exactly 2 pp. Pooled, take looks 5 pp WORSE."""
    week0 = [(0, 0, "take", 0.07)] + [(0, 1 + i, "skip", 0.05 + o) for i, o in enumerate(OFFSETS)]
    week1 = [(1, 10 + i, "take", -0.03 + o) for i, o in enumerate(OFFSETS)] + [
        (1, 20, "skip", -0.05)
    ]
    return week0 + week1


def confounded_mirror() -> list[tuple[int, int, str, float]]:
    """Take share high in the good week, no effect inside either week."""
    week0 = [(0, i, "take", 0.05 + o) for i, o in enumerate(OFFSETS)] + [(0, 6, "skip", 0.05)]
    week1 = [(1, 10, "take", -0.05)] + [(1, 11 + i, "skip", -0.05 + o) for i, o in
                                         enumerate(OFFSETS)]  # fmt: skip
    return week0 + week1


def test_centred_statistic_sees_a_within_week_effect_the_pooled_one_hides() -> None:
    trades = mirror()
    take = {i for i, (_, _, a, _) in enumerate(trades) if a == "take"}
    assert _pooled(trades, take) == pytest.approx((0.07 - 0.18) / 7 - 0.25 / 7)  # -5.14 pp
    assert within_block_effect(trades) == pytest.approx(0.02)
    # exact: 7 x 7 = 49 arrangements; the observed one is the only one with |T| this large
    exact = _exact_p(trades, _centred)
    assert exact == pytest.approx(1 / 49)
    p = cluster_permutation_test(trades, n_permutations=10000, seed=17)
    assert p == pytest.approx(exact, abs=0.005)  # MC sd of p ~ 0.0014 at 10000 draws
    assert p < 0.05
    # the uncentred |d| of round 3 was blind here: its null centre is far from 0
    assert _exact_p(trades, _pooled) > 0.5


def mirror_rows(
    trades: list[tuple[int, int, str, float]], **analysis: Any
) -> tuple[list[dict[str, Any]], dict[str, list[ForwardTrade]]]:
    """Report rows for (block, key, arm, ret) singletons: block b uses fill bars k = 42 b + j
    (42 4h bars = the frozen 7-day block counted from the start)."""
    rows: list[dict[str, Any]] = [genesis(**analysis)]
    fills: list[ForwardTrade] = []
    used: dict[int, int] = {}
    for b, _key, arm, ret in trades:
        j = used.get(b, 0)
        used[b] = j + 1
        k = 42 * b + j - 1 if b else j  # (k + 1) // 42 == b
        rows += [bar(k), decision(k, choice=arm)]
        fills.append(trade(fill_open(k) + 5, ret))
    return rows, {CAND: fills, CAND2: []}


def test_report_headline_is_the_centred_effect_and_the_pooled_diff_is_descriptive() -> None:
    rows, fills = mirror_rows(mirror(), min_judged_trades=14, min_per_arm=7)
    rep = build_report(rows, fills, asof=ASOF)
    assert rep.blocks == 2 and rep.informative_strata == 2
    assert rep.effect == pytest.approx(0.02)
    assert rep.diff is not None and rep.diff < -0.05
    assert rep.p_value is not None and rep.p_value < 0.05
    assert rep.outcome == "take better"  # the sign comes from T, not from the pooled diff
    md = render_report_markdown(rep)
    assert "(T, the tested statistic and the headline effect): +2.00 pp" in md
    assert "Pooled difference take - skip: -5.14 pp (descriptive only" in md

    confounded = build_report(
        *mirror_rows(confounded_mirror(), min_judged_trades=14, min_per_arm=7), asof=ASOF
    )
    assert confounded.diff is not None and confounded.diff > 0.07  # pooled: take looks better
    assert confounded.effect == pytest.approx(0.0, abs=1e-12)
    assert confounded.outcome != "take better"
    assert confounded.outcome == "no information" and confounded.p_value == pytest.approx(1.0)


def test_null_with_returns_shuffled_within_blocks_keeps_the_rejection_rate_near_alpha() -> None:
    """Labels fixed with very different take shares (week 0: 3 of 12 taken, week 1: 9 of 12)
    and week means +5% / -5%; under the null the returns are shuffled within each block, so
    labels carry no information inside a week. A valid test rejects at p < alpha about alpha
    of the time. 1000 replications x 199 permutations (seeded) keeps it fast (~0.1 s).
    Tolerance: the rate's binomial sd at 0.05 is sqrt(0.05 * 0.95 / 1000) ~ 0.0069, so
    [0.025, 0.075] is about +-3.6 sd (the exact within-block test of this design rejects
    ~0.048 of the time; a permutation test is exact under exchangeability)."""
    rng = np.random.default_rng(20261029)
    labels = [(0, "take")] * 3 + [(0, "skip")] * 9 + [(1, "take")] * 9 + [(1, "skip")] * 3
    reps, rejections = 1000, 0
    for r in range(reps):
        week = {0: list(0.05 + 0.01 * rng.standard_normal(12)),
                1: list(-0.05 + 0.01 * rng.standard_normal(12))}  # fmt: skip
        for b in week:
            rng.shuffle(week[b])
        trades = [(b, i, arm, week[b].pop()) for i, (b, arm) in enumerate(labels)]
        rejections += cluster_permutation_test(trades, n_permutations=199, seed=r) < 0.05
    rate = rejections / reps
    assert 0.025 <= rate <= 0.075


def test_singleton_clusters_in_one_block_reduce_to_the_ordinary_two_sample_test() -> None:
    take, skip = [0.03, -0.01, 0.02, 0.05], [0.01, -0.03, 0.0, -0.02]
    trades = singletons(take, skip)
    assert within_block_effect(trades) == pytest.approx(np.mean(take) - np.mean(skip))
    exact = _exact_p(trades, _pooled)  # ordinary two-sample: all C(8, 4) = 70 splits
    p = cluster_permutation_test(trades, n_permutations=20000, seed=17)
    assert p == pytest.approx(exact, abs=0.01)  # MC sd of p <= 0.0035 at 20000 draws
    assert exact == pytest.approx(_exact_p(trades, _centred))


def test_the_centred_statistic_is_frozen_in_the_genesis_analysis() -> None:
    assert DEFAULT_ANALYSIS["statistic"] == "centred_within_block_diff"
    rows, fills = scenario([0.01], [0.02], statistic="pooled_diff")
    with pytest.raises(ValueError, match="statistic"):
        build_report(rows, fills, asof=ASOF)
    rows, fills = scenario([0.01], [0.02])
    del rows[0]["analysis"]["statistic"]
    with pytest.raises(ValueError, match="missing keys"):
        build_report(rows, fills, asof=ASOF)


def test_served_model_equal_to_the_requested_alias_counts_as_no_served_model() -> None:
    rows = [genesis()]  # requested alias "jev-latest"
    fills = []
    for k, model in enumerate(["jev-1.13.0", "jev-latest", " jev-latest ", "jev-1.13.0"]):
        rows += [bar(k), decision(k, choice="take" if k % 2 else "skip", model=model)]
        fills.append(trade(fill_open(k) + 5, 0.01))
    rep = build_report(rows, {CAND: fills, CAND2: []}, asof=ASOF)
    assert rep.model == "jev-1.13"
    assert rep.no_served_model == 2 and rep.alias_served == 2
    assert rep.take.n + rep.skip.n == 2
    assert any(r.startswith("WARNING: 2 judged decisions report the requested alias") for r in
               rep.reasons)  # fmt: skip
    assert "**WARNING, an alias is not a served model id**" in render_report_markdown(rep)
    # a full version requested and served is a served id, not an alias
    pinned = [{**genesis(), "model": "jev-1.13.0"}]
    for k in range(2):
        pinned += [bar(k), decision(k, choice="take", model="jev-1.13.0")]
    rep2 = build_report(pinned, {CAND: fills[:2], CAND2: []}, asof=ASOF)
    assert rep2.model == "jev-1.13" and rep2.no_served_model == 0 and rep2.alias_served == 0


def test_recovered_heads_inside_the_prefix_are_listed_with_their_lag() -> None:
    rows, fills = scenario([0.01, 0.02], [0.0])
    plain = render_report_markdown(build_report(rows, fills, asof=ASOF))
    assert "Recovered head lines (appended after a crash" in plain and "written_at): none." in plain
    rep = build_report(rows, fills, asof=ASOF, recovered_heads=[(4, 42.4), (2, None), (99, 1.0)])
    assert rep.recovered_heads == [(2, None), (4, 42.4)]  # seq 99 is not in the prefix used
    md = render_report_markdown(rep)
    assert "written_at): seq 2 (lag unknown), seq 4 (lag 42 s)." in md


def test_report_blocks_follow_the_frozen_block_days_from_the_genesis_start() -> None:
    take = [0.05 + 0.001 * (i % 5) for i in range(20)]
    skip = [-0.05 - 0.001 * (i % 5) for i in range(20)]
    week = build_report(*scenario(take, skip), asof=ASOF)  # 40 bars: all in block 0
    assert (week.blocks, week.informative_strata) == (1, 1)
    assert "1 blocks of 7 days, 1 informative strata" in render_report_markdown(week)
    # 1-day blocks (6 bars): block = (k + 1) // 6, only block 3 (k 17..22) mixes both labels
    daily = build_report(*scenario(take, skip, block_days=1), asof=ASOF)
    assert (daily.blocks, daily.informative_strata) == (7, 1)
    # the week runs the test on all 40 trades; the daily blocks leave only block 3's 6 trades
    # (3 take, 3 skip) carrying weight, below the frozen minimum: inconclusive, no test run
    assert (week.informative_trades, week.outcome) == (40, "take better")
    assert week.p_value is not None
    assert (daily.take.n, daily.skip.n, daily.informative_trades) == (20, 20, 6)
    assert daily.outcome == "inconclusive" and daily.p_value is None
    assert any(
        "6 closed judged trades in informative strata" in r and "< the minimum 30" in r
        for r in daily.reasons
    )
    for arm in ("take", "skip"):
        assert f"{arm} arm has 3 trades in informative strata < the minimum 8" in daily.reasons
    assert "1 informative strata (" in render_report_markdown(daily)
    assert "6 trades in them" in render_report_markdown(daily)
    with pytest.raises(ValueError, match="strata"):
        build_report(*scenario(take, skip, strata="none"), asof=ASOF)
    with pytest.raises(ValueError, match="block_days"):
        build_report(*scenario(take, skip, block_days=0), asof=ASOF)


def test_trivial_btc_regime_split_is_reported_descriptively() -> None:
    rows, trades = scenario([0.04, 0.02, -0.01], [0.01, -0.03])
    trends = [1.5, 0.0, -2.0, 3.0, None]  # per decision, in order
    i = 0
    for r in rows:
        if r["kind"] == "decision":
            r["snapshot"] = {} if trends[i] is None else {"btc_trend_pct": trends[i]}
            i += 1
    rep = build_report(rows, trades, asof=ASOF)
    assert rep.regime_up.n == 2 and rep.regime_up.mean == pytest.approx(0.025)  # 0.04, 0.01
    assert rep.regime_down.n == 2 and rep.regime_down.mean == pytest.approx(0.005)  # 0.02, -0.01
    assert rep.regime_unknown == 1
    md = render_report_markdown(rep)
    assert "## Trivial regime rule (descriptive)" in md and "Descriptive only, no test" in md
    assert "| btc_trend_pct > 0 | 2 | +2.50% |" in md
    assert "| btc_trend_pct <= 0 | 2 | +0.50% |" in md
    assert "Difference (> 0) - (<= 0): +2.00 pp" in md


def test_environment_drift_is_shown_and_numpy_or_pandas_change_adds_a_reason() -> None:
    env = {"python": "3.12.1", "numpy": "2.1.0", "pandas": "2.2.3", "uv_lock_sha256": "a" * 64}
    rows, trades = scenario([0.01], [0.02])
    rows[0]["env"] = env
    same = build_report(rows, trades, asof=ASOF, env=env)
    assert same.genesis_env == env and same.env == env
    assert not any("environment differs" in r for r in same.reasons)
    md = render_report_markdown(same)
    assert "## Environment" in md and "| numpy | `2.1.0` | `2.1.0` |" in md
    moved = build_report(rows, trades, asof=ASOF, env={**env, "numpy": "2.3.0"})
    assert any("numpy 2.1.0 -> 2.3.0" in r and "permutation stream" in r for r in moved.reasons)
    assert moved.outcome == "inconclusive"  # a reason line, never a refusal
    lock_only = build_report(rows, trades, asof=ASOF, env={**env, "uv_lock_sha256": "b" * 64})
    assert not any("environment differs" in r for r in lock_only.reasons)
    bare = build_report(scenario([0.01], [0.02])[0], trades, asof=ASOF, env=env)
    assert bare.genesis_env is None and "| numpy | not recorded | `2.1.0` |" in (
        render_report_markdown(bare)
    )
    now = current_env()
    assert set(now) == {"python", "numpy", "pandas", "uv_lock_sha256"}
    assert now["numpy"] == np.__version__ and now["pandas"] == pd.__version__
    assert build_report(rows, trades, asof=ASOF).env == now  # default: the current env


# --------------------------------------------------------------------------- informative strata


def test_minimum_sample_must_hold_inside_informative_strata() -> None:
    """Quant review W-r4-1: 34 closed trades (17 per arm) pass the overall minimum, but week 0
    holds only takes and week 1 only skips, so only week 2's 4 trades carry weight in the
    centred statistic. Below the minimum inside informative strata the outcome is
    "inconclusive" (never "no information") and no test is run."""
    trades = (
        [(0, i, "take", 0.05 + 0.001 * i) for i in range(15)]
        + [(1, 100 + i, "skip", -0.05 - 0.001 * i) for i in range(15)]
        + [(2, 200, "take", 0.01), (2, 201, "take", 0.02)]
        + [(2, 202, "skip", 0.0), (2, 203, "skip", -0.01)]
    )
    assert strata_summary(trades) == (3, 1)
    assert informative_counts(trades) == (2, 2)
    rep = build_report(*mirror_rows(trades), asof=ASOF)
    assert (rep.take.n, rep.skip.n) == (17, 17)
    assert (rep.blocks, rep.informative_strata, rep.informative_trades) == (3, 1, 4)
    assert rep.outcome == "inconclusive" and rep.p_value is None
    assert not any("closed judged trades <" in r for r in rep.reasons)  # the overall gate passes
    assert not any("closed trades < the minimum" in r for r in rep.reasons)
    assert (
        "4 closed judged trades in informative strata (blocks and cluster sizes holding both "
        "labels) < the minimum 30"
    ) in rep.reasons
    assert "take arm has 2 trades in informative strata < the minimum 8" in rep.reasons
    assert "skip arm has 2 trades in informative strata < the minimum 8" in rep.reasons
    md = render_report_markdown(rep)
    assert "**Outcome: inconclusive**" in md and "4 trades in them" in md


def test_a_single_cluster_holding_both_labels_is_an_informative_stratum() -> None:
    """Quant review S-r4-1: one fill bar judging a take and a skip is a stratum of one cluster,
    but the within-cluster shuffle moves it, so it counts as informative. Single-label strata
    (one take alone, a two-skip cluster) do not."""
    trades = [
        (0, 0, "take", 0.05), (0, 0, "skip", -0.05),  # stratum (block 0, size 2): both labels
        (0, 1, "take", 0.02),  # stratum (block 0, size 1): take only
        (1, 2, "skip", 0.01), (1, 2, "skip", 0.0),  # stratum (block 1, size 2): skip only
    ]  # fmt: skip
    assert strata_summary(trades) == (2, 1)
    assert informative_counts(trades) == (1, 1)
    assert strata_summary(trades[:2]) == (1, 1) and informative_counts(trades[:2]) == (1, 1)
    assert informative_counts(trades[2:]) == (0, 0)
    # the same cluster in a report: one bar where hc-btc is taken and hc-eth skipped
    rows = [genesis(), bar(0), decision(0, choice="take"), decision(0, choice="skip", cand=CAND2)]
    fills = {CAND: [trade(fill_open(0) + 5, 0.05)], CAND2: [trade(fill_open(0) + 5, -0.05)]}
    rep = build_report(rows, fills, asof=ASOF)
    assert (rep.clusters, rep.mixed_clusters) == (1, 1)
    assert (rep.blocks, rep.informative_strata, rep.informative_trades) == (1, 1, 2)
    assert rep.outcome == "inconclusive" and rep.p_value is None  # 2 < 30, but informative


def test_a_sample_passing_the_informative_gate_runs_the_test() -> None:
    """Week 0 mixes 15 takes and 15 skips (exactly the minimum 30, 15 per arm, inside an
    informative stratum); week 1 adds 10 take-only trades that carry no weight. The gate
    passes, so the frozen test runs as before."""
    trades = (
        [(0, i, "take", 0.05 + 0.001 * (i % 5)) for i in range(15)]
        + [(0, 20 + i, "skip", -0.05 - 0.001 * (i % 5)) for i in range(15)]
        + [(1, 100 + i, "take", 0.0) for i in range(10)]
    )
    assert informative_counts(trades) == (15, 15)
    rep = build_report(*mirror_rows(trades), asof=ASOF)
    assert (rep.take.n, rep.skip.n) == (25, 15)
    assert (rep.blocks, rep.informative_strata, rep.informative_trades) == (2, 1, 30)
    assert not any("informative strata" in r for r in rep.reasons)
    assert rep.p_value is not None and rep.p_value < 0.001
    assert rep.outcome == "take better"
    assert rep.effect == pytest.approx(within_block_effect(trades))
