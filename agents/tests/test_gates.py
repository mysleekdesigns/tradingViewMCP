"""Dedupe, sweep triage, and done-check gates: Jev happy paths and fail-open fallbacks."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from research.gates import (
    MAX_CHOICE_OPTIONS,
    DoneVerdict,
    DuplicateVerdict,
    check_duplicate,
    done_check,
    triage_sweep,
)
from research.jev import JevClient

EXISTING = [
    (7, "Long when price breaks a 20-day high after volatility compression."),
    (9, "RSI 2 mean reversion"),
]


def client(rec) -> JevClient:
    return JevClient(transport=rec.transport, sleep=lambda _s: None)


def choice_answer(choice: str, probs: dict[str, float]):
    def answer(_qid, q):
        full = dict.fromkeys(q["criteria"], 0.0)
        full.update(probs)
        return {
            "type": "choice",
            "choice": choice,
            "confidence": probs[choice],
            "probabilities": full,
        }

    return answer


# ------------------------------------------------------------------ dedupe


def test_dedupe_flags_confident_duplicate(jev_api_key, jev_recorder, jev_answer_all):
    rec = jev_recorder(lambda r: jev_answer_all(r, choice_answer("7", {"7": 0.9, "none": 0.1})))
    verdict = check_duplicate(client(rec), "Buy 20-day breakouts after a squeeze", EXISTING)
    assert verdict == DuplicateVerdict(duplicate_of=7, probability=0.9, decided_by="jev")
    body = rec.bodies()[0]
    q = body["questions"]["is_duplicate_of"]
    assert q["type"] == "choice"
    assert set(q["criteria"]) == {"7", "9", "none"}
    assert "Buy 20-day breakouts" in body["state"]["new_hypothesis"]


def test_dedupe_below_threshold_is_not_duplicate(jev_api_key, jev_recorder, jev_answer_all):
    probs = {"7": 0.6, "9": 0.1, "none": 0.3}
    rec = jev_recorder(lambda r: jev_answer_all(r, choice_answer("7", probs)))
    verdict = check_duplicate(client(rec), "something", EXISTING)
    assert verdict == DuplicateVerdict(duplicate_of=None, probability=0.6, decided_by="jev")


def test_dedupe_none_choice(jev_api_key, jev_recorder, jev_answer_all):
    probs = {"7": 0.05, "9": 0.03, "none": 0.92}
    rec = jev_recorder(lambda r: jev_answer_all(r, choice_answer("none", probs)))
    verdict = check_duplicate(client(rec), "funding carry", EXISTING)
    assert verdict.duplicate_of is None and verdict.decided_by == "jev"
    assert verdict.probability == pytest.approx(0.05)


def test_dedupe_uses_confidence_when_no_probabilities(jev_api_key, jev_recorder):
    body = {"answers": {"is_duplicate_of": {"type": "choice", "choice": "9", "confidence": 0.85}}}
    rec = jev_recorder(lambda r: httpx.Response(200, json=body))
    verdict = check_duplicate(client(rec), "RSI(2) dip buying", EXISTING)
    assert verdict == DuplicateVerdict(duplicate_of=9, probability=0.85, decided_by="jev")


def test_dedupe_limits_options_to_255(jev_api_key, jev_recorder, jev_answer_all):
    rec = jev_recorder(lambda r: jev_answer_all(r, None))
    many = [(i, f"idea {i}") for i in range(300)] + [(3, "duplicate id ignored")]
    check_duplicate(client(rec), "x", many)
    criteria = rec.bodies()[0]["questions"]["is_duplicate_of"]["criteria"]
    assert len(criteria) == MAX_CHOICE_OPTIONS
    assert "none" in criteria and "253" in criteria and "254" not in criteria


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(529),
        lambda r: httpx.Response(401),
        lambda r: httpx.Response(200, content=b"<html>"),
        lambda r: httpx.Response(200, json={"answers": {}}),
    ],
    ids=["529-exhausted", "401", "bad-json", "missing-answer"],
)
def test_dedupe_falls_back_on_jev_failure(jev_api_key, jev_recorder, handler, caplog):
    rec = jev_recorder(handler)
    with caplog.at_level(logging.WARNING, logger="research.gates"):
        verdict = check_duplicate(client(rec), "x", EXISTING)
    assert verdict == DuplicateVerdict(duplicate_of=None, probability=None, decided_by="fallback")
    assert "dedupe falling back" in caplog.text


def test_dedupe_timeout_falls_back(jev_api_key, jev_recorder):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert check_duplicate(client(jev_recorder(handler)), "x", EXISTING).decided_by == "fallback"


def test_dedupe_disabled_or_empty_makes_no_request(monkeypatch, jev_recorder):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rec = jev_recorder(lambda r: httpx.Response(200))
    assert check_duplicate(client(rec), "x", EXISTING).decided_by == "fallback"
    assert check_duplicate(None, "x", EXISTING).decided_by == "fallback"
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key")
    assert check_duplicate(client(rec), "x", []).duplicate_of is None
    assert rec.requests == []


# ------------------------------------------------------------------ sweep triage


def smooth_sweep() -> list[dict]:
    """1-D sweep over `lookback` 10..100; Sharpe rises gently, one lone spike at 50,
    and one trial with too few trades at 100."""
    trials = []
    for k, lookback in enumerate(range(10, 101, 10)):
        trials.append(
            {
                "params": {"lookback": lookback, "_study": {"ignored": True}},
                "sharpe": 0.5 + 0.01 * k,
                "n_trades": 120,
                "status": "ok",
            }
        )
    trials[4]["sharpe"] = 2.5  # lookback 50: isolated spike
    trials[9]["n_trades"] = 12  # lookback 100: too few trades
    return trials


def test_triage_fallback_uses_python_rules(monkeypatch, jev_recorder):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rec = jev_recorder(lambda r: httpx.Response(200))
    rows = triage_sweep(client(rec), smooth_sweep())
    assert rec.requests == []
    assert [r.index for r in rows] == list(range(10))
    flags = [r.flag for r in rows]
    assert flags[4] == "suspicious" and flags[9] == "suspicious"
    assert all(f == "unknown" for i, f in enumerate(flags) if i not in (4, 9))
    assert any("isolated spike" in s for s in rows[4].reasons)
    assert rows[9].reasons == ["low trade count: 12 < 30"]


def test_triage_spike_uses_neighbour_median_plus_two_mads():
    # Neighbours of the middle trial have Sharpe 1.0 +/- 0.1 -> median 1.0, MAD 0.1.
    sharpes = [0.9, 1.1, 0.9, 1.1, None, 0.9, 1.1, 0.9, 1.1]

    def trials_with(mid: float) -> list[dict]:
        out = []
        for x, s in enumerate(sharpes):
            out.append({"params": {"x": x}, "sharpe": mid if s is None else s, "n_trades": 100})
        return out

    assert triage_sweep(None, trials_with(1.21))[4].flag == "suspicious"  # > 1.0 + 0.2
    assert triage_sweep(None, trials_with(1.19))[4].flag == "unknown"


def test_triage_flat_surface_uses_mad_floor():
    trials = [{"params": {"x": x}, "sharpe": 1.0, "n_trades": 100} for x in range(9)]
    trials[4]["sharpe"] = 1.05  # tiny excess on a flat plateau: not a spike
    assert triage_sweep(None, trials)[4].flag == "unknown"
    trials[4]["sharpe"] = 1.2
    assert triage_sweep(None, trials)[4].flag == "suspicious"


def test_triage_bad_trials_are_unknown_and_never_raise():
    trials = [
        {"params": {"x": 1}, "sharpe": float("nan"), "n_trades": 50},
        {"params": {"x": 2}, "sharpe": -1e9, "n_trades": 0, "status": "error"},
        {"params": {"x": 3}, "sharpe_per_bar": 0.01},
        {"params": "garbage", "sharpe": "n/a", "n_trades": True},
    ]
    rows = triage_sweep(None, trials)
    assert [r.flag for r in rows] == ["unknown", "unknown", "unknown", "unknown"]
    assert rows[0].reasons == ["missing or non-finite Sharpe"]
    assert rows[1].reasons == ["trial status 'error'"]
    assert rows[2].reasons == ["missing trade count"]
    assert triage_sweep(None, []) == []


def test_triage_mixed_categorical_params():
    trials = [
        {"params": {"x": x, "mode": m}, "sharpe": 1.0, "n_trades": 80}
        for m in ("a", "b")
        for x in range(5)
    ]
    rows = triage_sweep(None, trials)
    assert all(r.flag == "unknown" for r in rows)


def test_triage_with_jev_labels_from_python_facts(jev_api_key, jev_recorder, jev_answer_all):
    def answer(qid, _q):
        i = int(qid[1:].split("_")[0])
        # Jev calls trial 2 suspicious, and (wrongly) everything else ok, including the spike.
        return {"type": "noul", "noul": 0.8 if i == 2 else 0.1}

    rec = jev_recorder(lambda r: jev_answer_all(r, answer))
    rows = triage_sweep(client(rec), smooth_sweep())
    assert len(rec.requests) == 1
    flags = [r.flag for r in rows]
    assert flags[2] == "suspicious" and any("jev" in s for s in rows[2].reasons)
    assert flags[4] == "suspicious"  # hard Python rule overrides Jev's "ok"
    assert flags[9] == "suspicious"
    assert all(flags[i] == "ok" for i in (0, 1, 3, 5, 6, 7, 8))

    state = rec.bodies()[0]["state"]
    fact = state["trials"][4]
    assert fact["isolated_spike"] is True
    # 8 nearest to lookback 50 are k=0..3 and 5..8 (Sharpe 0.50-0.53, 0.55-0.58): median 0.54.
    assert fact["neighbour_median_sharpe"] == pytest.approx(0.54)
    assert fact["sharpe_minus_neighbour_median"] == pytest.approx(2.5 - 0.54)
    assert state["trials"][9]["low_trade_count"] is True
    assert "_study" not in json.dumps(state)


def test_triage_chunks_large_sweeps(jev_api_key, jev_recorder, jev_answer_all):
    rec = jev_recorder(lambda r: jev_answer_all(r, None))
    trials = [{"params": {"x": x}, "sharpe": 1.0, "n_trades": 100} for x in range(150)]
    rows = triage_sweep(client(rec), trials)
    assert len(rec.requests) == 2 and len(rows) == 150
    assert [r.index for r in rows] == list(range(150))


def test_triage_jev_failure_falls_back_per_chunk(jev_api_key, jev_recorder, caplog):
    rec = jev_recorder(lambda r: httpx.Response(529))
    with caplog.at_level(logging.WARNING, logger="research.gates"):
        rows = triage_sweep(client(rec), smooth_sweep())
    assert len(rec.requests) == 3  # one call, two retries
    assert [r.flag for r in rows].count("suspicious") == 2
    assert [r.flag for r in rows].count("unknown") == 8
    assert "sweep-triage falling back" in caplog.text


# ------------------------------------------------------------------ done-check

SUMMARY = {"hypotheses_tried": 6, "hypotheses_budget": 6, "candidates": 0, "backtests_left": 3}


@pytest.mark.parametrize(("p", "done"), [(0.9, True), (0.2, False), (0.5, True)])
def test_done_check_happy_path(jev_api_key, jev_recorder, jev_answer_all, p, done):
    rec = jev_recorder(lambda r: jev_answer_all(r, lambda _q, _d: {"type": "noul", "noul": p}))
    verdict = done_check(client(rec), SUMMARY)
    assert verdict == DoneVerdict(done=done, probability=p, decided_by="jev")
    body = rec.bodies()[0]
    assert body["state"] == SUMMARY
    assert body["questions"]["should_stop"]["type"] == "noul"


@pytest.mark.parametrize(
    "handler",
    [lambda r: httpx.Response(529), lambda r: httpx.Response(200, json={"answers": {}})],
    ids=["529", "missing-answer"],
)
def test_done_check_falls_back(jev_api_key, jev_recorder, handler, caplog):
    with caplog.at_level(logging.WARNING, logger="research.gates"):
        verdict = done_check(client(jev_recorder(handler)), SUMMARY)
    assert verdict == DoneVerdict(done=None, probability=None, decided_by="fallback")
    assert "done-check falling back" in caplog.text


def test_done_check_disabled_makes_no_request(monkeypatch, jev_recorder):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rec = jev_recorder(lambda r: httpx.Response(200))
    assert done_check(client(rec), SUMMARY).done is None
    assert done_check(None, SUMMARY).decided_by == "fallback"
    assert rec.requests == []


def test_gates_never_log_the_key(jev_api_key, jev_recorder, caplog):
    rec = jev_recorder(lambda r: httpx.Response(401, json={"detail": jev_api_key}))
    with caplog.at_level(logging.DEBUG):
        results = [
            check_duplicate(client(rec), "x", EXISTING),
            triage_sweep(client(rec), smooth_sweep()),
            done_check(client(rec), SUMMARY),
        ]
    assert jev_api_key not in caplog.text
    assert jev_api_key not in repr(results)
