"""Blind snapshot + batched Jev judge (Phase 17). Offline: synthetic candles, MockTransport."""

from __future__ import annotations

import hashlib
import json
import logging
import math

import httpx
import numpy as np
import pandas as pd
import pytest

import research.paper.shadow_snapshot as shadow_snapshot
from research.jev import JevClient, JevResponse
from research.paper.shadow_snapshot import (
    CONTEXT,
    MIN_HISTORY_BARS,
    QUESTIONS_SHA256,
    SNAPSHOT_FIELDS,
    SNAPSHOT_FINGERPRINT_INPUTS_SEED,
    SNAPSHOT_VERSION,
    JevVerdict,
    blind_snapshot,
    build_questions,
    build_state,
    judge,
    label_setups,
    parse_verdicts,
    snapshot_fingerprint,
    snapshot_sha256,
)

START = pd.Timestamp("2026-01-01", tz="UTC")


def frame(close, high=None, low=None, volume=None, open_=None) -> pd.DataFrame:
    close = np.asarray(close, dtype=np.float64)
    n = close.size
    idx = pd.date_range(START, periods=n, freq="4h", tz="UTC")
    return pd.DataFrame(
        {
            "open": close if open_ is None else np.asarray(open_, dtype=np.float64),
            "high": close if high is None else np.asarray(high, dtype=np.float64),
            "low": close if low is None else np.asarray(low, dtype=np.float64),
            "close": close,
            "volume": np.full(n, 5.0) if volume is None else np.asarray(volume, dtype=np.float64),
        },
        index=idx,
    )


def random_frame(n: int, seed: int, base: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = base * np.exp(np.cumsum(rng.normal(0.0, 0.02, n)))
    open_ = np.concatenate(([base], close[:-1]))
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.0, 0.01, n))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.0, 0.01, n))
    volume = rng.uniform(100.0, 1000.0, n)
    return frame(close, high=high, low=low, volume=volume, open_=open_)


# ----------------------------------------------------------- independent reference (plain Python)


def ref_ema(xs: list[float], span: int) -> float:
    a = 2.0 / (span + 1.0)
    e = xs[0]
    for x in xs[1:]:
        e = a * x + (1.0 - a) * e
    return e


def ref_wilder(xs: list[float], period: int) -> float:
    e = sum(xs[:period]) / period
    for x in xs[period:]:
        e = (e * (period - 1) + x) / period
    return e


def ref_snapshot(df: pd.DataFrame, btc: pd.DataFrame) -> dict[str, float | int]:
    c = [float(x) for x in df["close"]]
    h = [float(x) for x in df["high"]]
    lo = [float(x) for x in df["low"]]
    v = [float(x) for x in df["volume"]]
    b = [float(x) for x in btc["close"]]
    tr = [h[0] - lo[0]] + [
        max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))
    ]
    gains = [max(c[i] - c[i - 1], 0.0) for i in range(1, len(c))]
    losses = [max(c[i - 1] - c[i], 0.0) for i in range(1, len(c))]
    g, lo_avg = ref_wilder(gains, 14), ref_wilder(losses, 14)
    rsi = 100.0 if lo_avg == 0 else 100.0 - 100.0 / (1.0 + g / lo_avg)
    return {
        "ret_1_pct": round((c[-1] / c[-2] - 1) * 100, 1),
        "ret_6_pct": round((c[-1] / c[-7] - 1) * 100, 1),
        "ret_42_pct": round((c[-1] / c[-43] - 1) * 100, 1),
        "atr14_pct": round(ref_wilder(tr, 14) / c[-1] * 100, 2),
        "dist_ema20_pct": round((c[-1] / ref_ema(c, 20) - 1) * 100, 1),
        "dist_ema50_pct": round((c[-1] / ref_ema(c, 50) - 1) * 100, 1),
        "dist_ema200_pct": round((c[-1] / ref_ema(c, 200) - 1) * 100, 1),
        "rsi14": round(rsi),
        "vol_ratio_20": round(v[-1] / (sum(v[-21:-1]) / 20), 1),
        "btc_trend_pct": round((b[-1] / ref_ema(b, 50) - 1) * 100, 1),
        "btc_ret_42_pct": round((b[-1] / b[-43] - 1) * 100, 1),
    }


# ------------------------------------------------------------------------------- snapshot


def test_constants():
    assert SNAPSHOT_VERSION == 1
    assert MIN_HISTORY_BARS == 201
    assert SNAPSHOT_FIELDS == (
        "ret_1_pct",
        "ret_6_pct",
        "ret_42_pct",
        "atr14_pct",
        "dist_ema20_pct",
        "dist_ema50_pct",
        "dist_ema200_pct",
        "rsi14",
        "vol_ratio_20",
        "btc_trend_pct",
        "btc_ret_42_pct",
    )


def test_hand_calculated_jump_bar():
    # Flat at 100 for 249 bars, then one bar closing at 110 (high=low=close), volume 5 -> 10.
    n = 250
    close = np.full(n, 100.0)
    close[-1] = 110.0
    volume = np.full(n, 5.0)
    volume[-1] = 10.0
    df = frame(close, volume=volume)
    btc_close = np.full(n, 50.0)
    btc_close[-42:] = 55.0  # +10% over the last 42 bars
    btc = frame(btc_close)
    snap = blind_snapshot(df, btc, df.index[-1])

    def dist(span: int) -> float:  # EMA = 100 + 10 * alpha after one jump
        return round((110.0 / (100.0 + 10.0 * 2.0 / (span + 1.0)) - 1.0) * 100.0, 1)

    btc_ema50 = 50.0
    for _ in range(42):
        btc_ema50 = (2 / 51) * 55.0 + (49 / 51) * btc_ema50
    assert snap == {
        "ret_1_pct": 10.0,
        "ret_6_pct": 10.0,
        "ret_42_pct": 10.0,
        "atr14_pct": round((10.0 / 14.0) / 110.0 * 100.0, 2),  # TR 0 then 10 -> ATR 10/14
        "dist_ema20_pct": dist(20),
        "dist_ema50_pct": dist(50),
        "dist_ema200_pct": dist(200),
        "rsi14": 100,  # no losses
        "vol_ratio_20": 2.0,
        "btc_trend_pct": round((55.0 / btc_ema50 - 1.0) * 100.0, 1),
        "btc_ret_42_pct": 10.0,
    }
    assert snap["atr14_pct"] == 0.65
    assert snap["dist_ema20_pct"] == 9.0


def test_matches_independent_reference_on_random_walk():
    df = random_frame(300, seed=7)
    btc = random_frame(300, seed=11, base=30000.0)
    at = df.index[260]
    snap = blind_snapshot(df, btc, at)
    assert snap == ref_snapshot(df.loc[:at], btc.loc[:at])


def test_rsi_rounds_to_int_and_flat_tail_is_zero_not_negative_zero():
    n = 230
    df = frame(np.full(n, 100.0))
    snap = blind_snapshot(df, df, df.index[-1])
    for key in ("ret_1_pct", "dist_ema20_pct", "btc_trend_pct", "atr14_pct"):
        assert snap[key] == 0.0
        assert math.copysign(1.0, snap[key]) == 1.0
    assert snap["rsi14"] == 100  # avg loss 0 -> 100 (ta.rsi convention)
    assert isinstance(snap["rsi14"], int)


def test_non_finite_becomes_none():
    df = random_frame(230, seed=3)
    df.iloc[-21:-1, df.columns.get_loc("volume")] = 0.0  # mean of previous 20 volumes is 0
    df.iloc[-2, df.columns.get_loc("close")] = np.nan
    snap = blind_snapshot(df, random_frame(230, seed=4), df.index[-1])
    assert snap["vol_ratio_20"] is None
    assert snap["ret_1_pct"] is None
    json.dumps(snap, allow_nan=False)


def test_no_look_ahead_rows_after_at_are_ignored_in_both_frames():
    df = random_frame(260, seed=21)
    btc = random_frame(260, seed=22, base=30000.0)
    at = df.index[230]
    base = blind_snapshot(df, btc, at)
    after = df.index > at
    for col, scale in (("open", 3.0), ("high", 5.0), ("low", 0.1), ("close", 7.0)):
        df.loc[after, col] *= scale
        btc.loc[after, col] *= 1.0 / scale
    df.loc[after, "volume"] = 1e12
    btc.loc[after, "volume"] = 0.0
    assert blind_snapshot(df, btc, at) == base
    # Truncating everything after `at` (fewer rows) is also identical.
    assert blind_snapshot(df.loc[:at], btc.loc[:at], at) == base
    # Sanity: a change AT bar `at` does move the snapshot.
    df.loc[at, "close"] *= 1.05
    assert blind_snapshot(df, btc, at) != base


def test_snapshot_is_scale_free_no_price_or_volume_level():
    df = random_frame(240, seed=5)
    btc = random_frame(240, seed=6, base=30000.0)
    at = df.index[-1]
    base = blind_snapshot(df, btc, at)
    scaled = df.copy()
    scaled[["open", "high", "low", "close"]] *= 1024.0
    scaled["volume"] *= 4096.0
    btc_scaled = btc.copy()
    btc_scaled[["open", "high", "low", "close"]] *= 0.5
    assert blind_snapshot(scaled, btc_scaled, at) == base


def test_output_keys_and_types_are_blind():
    df = random_frame(240, seed=8)
    btc = random_frame(240, seed=9, base=30000.0)
    snap = blind_snapshot(df, btc, df.index[-1])
    assert tuple(snap) == SNAPSHOT_FIELDS
    for v in snap.values():
        assert v is None or (isinstance(v, int | float) and not isinstance(v, bool | str))
        assert not isinstance(v, np.generic)
    text = json.dumps(snap, allow_nan=False)
    assert "2026" not in text and "USDT" not in text and ":00" not in text


def test_missing_at_names_the_frame():
    df = random_frame(240, seed=1)
    btc = random_frame(240, seed=2).iloc[:-1]
    with pytest.raises(ValueError, match="btc"):
        blind_snapshot(df, btc, df.index[-1])
    with pytest.raises(ValueError, match="candles"):
        blind_snapshot(df.iloc[:-1], random_frame(240, seed=2), df.index[-1])


def test_too_short_history():
    df = random_frame(240, seed=1)
    btc = random_frame(240, seed=2)
    with pytest.raises(ValueError, match="need at least 201"):
        blind_snapshot(df, btc, df.index[MIN_HISTORY_BARS - 2])  # 200 rows up to at
    assert blind_snapshot(df, btc, df.index[MIN_HISTORY_BARS - 1])  # exactly 201 rows
    with pytest.raises(ValueError, match="btc has 150 bars"):
        blind_snapshot(df, btc.iloc[90:], df.index[239])


def test_naive_at_rejected():
    df = random_frame(240, seed=1)
    with pytest.raises(ValueError, match="tz-aware"):
        blind_snapshot(df, df, df.index[-1].tz_localize(None))


def test_snapshot_sha256_is_canonical_json():
    snap = {"b": 1.5, "a": None, "c": 3}
    expected = hashlib.sha256(b'{"a":null,"b":1.5,"c":3}').hexdigest()
    assert snapshot_sha256(snap) == expected
    assert snapshot_sha256(dict(reversed(list(snap.items())))) == expected
    with pytest.raises(ValueError):
        snapshot_sha256({"x": float("nan")})


# ------------------------------------------------------------------------------- request


def snaps() -> dict[str, dict[str, float | int | None]]:
    out = {}
    for i, name in enumerate(["hc-btc", "hc-eth", "hc-sol"]):
        out[name] = blind_snapshot(
            random_frame(240, seed=40 + i),
            random_frame(240, seed=99),
            START + pd.Timedelta(hours=4 * 239),
        )
    return out


def test_labels_follow_hash_order_not_insertion_order():
    s = snaps()
    labels = label_setups(s)
    assert sorted(labels.values()) == ["s1", "s2", "s3"]
    assert label_setups(dict(reversed(list(s.items())))) == labels
    by_hash = sorted(s, key=lambda name: (snapshot_sha256(s[name]), name))
    assert [labels[name] for name in by_hash] == ["s1", "s2", "s3"]


def test_identical_snapshots_tie_break_by_name():
    snap = {"x": 1.0}
    assert label_setups({"b": snap, "a": snap}) == {"a": "s1", "b": "s2"}


def test_state_and_questions_shape():
    state = build_state({"s2": {"x": 2}, "s1": {"x": 1}})
    assert state == {"context": CONTEXT, "setups": {"s1": {"x": 1}, "s2": {"x": 2}}}
    assert list(state["setups"]) == ["s1", "s2"]
    q = build_questions(["s1", "s2"])
    assert list(q) == ["s1_take", "s1_fakeout", "s1_conf", "s2_take", "s2_fakeout", "s2_conf"]
    assert q["s1_take"]["type"] == "choice"
    assert set(q["s1_take"]["criteria"]) == {"take", "skip"}
    assert q["s1_fakeout"]["type"] == "noul"
    assert q["s1_conf"]["type"] == "score"
    assert "s2" in q["s2_take"]["instructions"]


def test_questions_sha256_pins_context_and_texts():
    canon = json.dumps(
        {"context": CONTEXT, "questions": build_questions(["L"])},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    )
    assert hashlib.sha256(canon.encode("utf-8")).hexdigest() == QUESTIONS_SHA256


# ------------------------------------------------------------------------------- judge


def client(rec, **kw) -> JevClient:
    return JevClient(transport=rec.transport, sleep=lambda _s: None, **kw)


def take_answer(qid, q):
    if q["type"] == "choice":
        return {
            "type": "choice",
            "choice": "take",
            "confidence": 0.7,
            "probabilities": {"take": 0.7, "skip": 0.3},
        }
    if q["type"] == "noul":
        return {"type": "noul", "noul": 0.25}
    return {"type": "score", "score": 0.6, "confidence": 0.5}


def test_judge_one_batched_request_with_all_questions(jev_api_key, jev_recorder, jev_answer_all):
    rec = jev_recorder(lambda r: jev_answer_all(r, take_answer))
    s = snaps()
    ticks = iter([100.0, 100.25])
    verdicts, model, latency = judge(client(rec), s, clock=lambda: next(ticks))
    assert len(rec.requests) == 1
    body = rec.bodies()[0]
    assert set(body["questions"]) == {
        f"s{i}_{kind}" for i in (1, 2, 3) for kind in ("take", "fakeout", "conf")
    }
    labels = label_setups(s)
    assert body["state"]["context"] == CONTEXT
    assert body["state"]["setups"] == {labels[n]: s[n] for n in s}
    raw = rec.requests[0].content.decode()
    for name in s:  # candidate names never reach Jev
        assert name not in raw
    assert model == "jev-1.13.0"
    assert latency == 250
    assert verdicts == {
        name: JevVerdict(choice="take", choice_confidence=0.7, fakeout=0.25, score=0.6)
        for name in s
    }
    assert verdicts["hc-btc"].as_dict() == {
        "choice": "take",
        "choice_confidence": 0.7,
        "fakeout": 0.25,
        "score": 0.6,
    }


def test_judge_maps_each_label_back_to_its_candidate(jev_api_key, jev_recorder, jev_answer_all):
    s = snaps()
    labels = label_setups(s)
    skip_label = labels["hc-eth"]

    def answer(qid, q):
        if qid == f"{skip_label}_take":
            return {"type": "choice", "choice": "skip", "confidence": 0.9}
        return take_answer(qid, q)

    rec = jev_recorder(lambda r: jev_answer_all(r, answer))
    verdicts, _model, _lat = judge(client(rec), s)
    assert verdicts is not None
    assert verdicts["hc-eth"].choice == "skip"
    assert verdicts["hc-btc"].choice == "take"
    assert verdicts["hc-sol"].choice == "take"


def test_judge_empty_makes_no_request(jev_api_key, jev_recorder, jev_answer_all):
    rec = jev_recorder(lambda r: jev_answer_all(r, take_answer))
    assert judge(client(rec), {}) == ({}, None, 0)
    assert rec.requests == []


def test_judge_returns_the_served_model_never_the_requested_alias(
    jev_api_key, jev_recorder, jev_answer_all
):
    """A reply without a served model id gives ``None``, not the requested alias: the alias
    (e.g. ``jev-latest``) floats, so it must never pin the judge (Phase 17 review)."""
    for served in (None, "", "   "):

        def handler(r: httpx.Request, served: str | None = served) -> httpx.Response:
            body = json.loads(jev_answer_all(r, take_answer).content)
            if served is None:
                body.pop("model")
            else:
                body["model"] = served
            return httpx.Response(200, json=body)

        rec = jev_recorder(handler)
        verdicts, model, _lat = judge(client(rec, model="jev-pinned"), snaps())
        assert verdicts is not None  # the answers are valid: judged, but with no served id
        assert model is None

    def served_handler(r: httpx.Request) -> httpx.Response:
        body = json.loads(jev_answer_all(r, take_answer).content)
        body["model"] = "jev-1.13.2"
        return httpx.Response(200, json=body)

    rec = jev_recorder(served_handler)
    _v, model, _lat = judge(client(rec, model="jev-latest"), snaps())
    assert model == "jev-1.13.2"


def test_judge_invalid_answer_on_one_label_is_unavailable(
    jev_api_key, jev_recorder, jev_answer_all
):
    def answer(qid, q):
        if qid == "s2_fakeout":
            return {"type": "noul", "noul": 1.7}  # not a probability
        return take_answer(qid, q)

    rec = jev_recorder(lambda r: jev_answer_all(r, answer))
    verdicts, model, _lat = judge(client(rec), snaps())
    assert len(rec.requests) == 1
    assert verdicts is None
    assert model is None


def test_judge_http_error_is_unavailable(jev_api_key, jev_recorder):
    rec = jev_recorder(lambda _r: httpx.Response(500))
    verdicts, model, latency = judge(client(rec, max_retries=0), snaps())
    assert (verdicts, model) == (None, None)
    assert latency >= 0


def test_judge_without_key_makes_no_request(monkeypatch, jev_recorder, jev_answer_all):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rec = jev_recorder(lambda r: jev_answer_all(r, take_answer))
    verdicts, model, _lat = judge(client(rec), snaps())
    assert verdicts is None and model is None
    assert rec.requests == []


def resp_with(answers: dict) -> JevResponse:
    return JevResponse(model="jev-x", answers=answers, usage={})


GOOD = {
    "s1_take": {"type": "choice", "choice": "skip", "confidence": 0.8},
    "s1_fakeout": {"type": "noul", "noul": 0.6},
    "s1_conf": {"type": "score", "score": 0.4},
    "s2_take": {"type": "choice", "choice": "take"},
    "s2_fakeout": {"type": "noul", "noul": 0.1},
    "s2_conf": {"type": "score", "score": 0.9},
}


def test_parse_verdicts_happy():
    out = parse_verdicts(resp_with(GOOD), ["s1", "s2"])
    assert out == {
        "s1": JevVerdict("skip", 0.8, 0.6, 0.4),
        "s2": JevVerdict("take", None, 0.1, 0.9),
    }


def test_parse_verdicts_none_response():
    assert parse_verdicts(None, ["s1"]) is None


@pytest.mark.parametrize(
    ("qid", "bad"),
    [
        ("s2_take", None),
        ("s2_take", {"type": "choice", "choice": "maybe"}),
        ("s2_fakeout", None),
        ("s2_fakeout", {"type": "score", "score": 0.1}),
        ("s2_conf", None),
        ("s2_conf", {"type": "score", "score": float("inf")}),
        ("s2_conf", {"type": "score", "score": "0.5"}),
        ("s2_conf", {"type": "score", "score": True}),
    ],
)
def test_parse_verdicts_any_bad_label_is_none(qid, bad):
    answers = dict(GOOD)
    if bad is None:
        answers.pop(qid)
    else:
        answers[qid] = bad
    assert parse_verdicts(resp_with(answers), ["s1", "s2"]) is None


def test_fake_key_never_logged_or_returned(jev_api_key, jev_recorder, jev_answer_all, caplog):
    responses = iter(
        [
            jev_answer_all,
            lambda r, _a=None: httpx.Response(401, text=f"bad key {jev_api_key}"),
            lambda r, _a=None: httpx.Response(200, text=f"not json {jev_api_key}"),
        ]
    )
    handler = {"fn": next(responses)}
    rec = jev_recorder(lambda r: handler["fn"](r, take_answer))
    results = []
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            results.append(judge(client(rec), snaps()))
            handler["fn"] = next(responses, handler["fn"])
    assert len(rec.requests) == 3
    assert jev_api_key not in caplog.text
    assert jev_api_key not in repr(results)
    assert "unavailable" in caplog.text


# ------------------------------------------------------------------------------- fingerprint


def fp_frame(rng: np.random.Generator, base: float) -> pd.DataFrame:
    # The documented recipe: 320 synthetic 4h bars from 2024-01-01 00:00 UTC, drawn in order
    # normal(0, 0.02) log-returns, uniform(0, 0.01) high wick, uniform(0, 0.01) low wick,
    # uniform(100, 1000) volume; open = previous close (base for the first bar).
    n = 320
    close = base * np.exp(np.cumsum(rng.normal(0.0, 0.02, n)))
    open_ = np.concatenate(([base], close[:-1]))
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.0, 0.01, n))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.0, 0.01, n))
    volume = rng.uniform(100.0, 1000.0, n)
    idx = pd.date_range(pd.Timestamp("2024-01-01", tz="UTC"), periods=n, freq="4h")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )


@pytest.fixture
def fresh_fingerprint():
    snapshot_fingerprint.cache_clear()
    yield
    snapshot_fingerprint.cache_clear()


def test_fingerprint_is_stable_sha256_hex(fresh_fingerprint):
    fp = snapshot_fingerprint()
    assert len(fp) == 64 and all(ch in "0123456789abcdef" for ch in fp)
    assert snapshot_fingerprint() == fp
    snapshot_fingerprint.cache_clear()
    assert snapshot_fingerprint() == fp  # recomputed, not just cached
    assert isinstance(SNAPSHOT_FINGERPRINT_INPUTS_SEED, int)


def test_fingerprint_equals_hand_recomputation_from_documented_inputs(fresh_fingerprint, caplog):
    rng = np.random.default_rng(SNAPSHOT_FINGERPRINT_INPUTS_SEED)
    candles = fp_frame(rng, 100.0)
    btc = fp_frame(rng, 30000.0)
    snaps_at = [blind_snapshot(candles, btc, candles.index[i]) for i in range(250, 300)]
    # Each uses only rows up to its signal bar, and matches the plain-Python reference.
    for i in (250, 299):
        cut = slice(None, i + 1)
        assert snaps_at[i - 250] == ref_snapshot(candles.iloc[cut], btc.iloc[cut])

    valid = {
        "s1_take": {
            "type": "choice",
            "choice": "skip",
            "confidence": 0.8,
            "probabilities": {"take": 0.2, "skip": 0.8},
        },
        "s1_fakeout": {"type": "noul", "noul": 0.6},
        "s1_conf": {"type": "score", "score": 0.4},
        "s2_take": {"type": "choice", "choice": "take"},
        "s2_fakeout": {"type": "noul", "noul": 0.1},
        "s2_conf": {"type": "score", "score": 0.9},
    }
    invalid = {**valid, "s2_conf": {"type": "score"}}  # missing score
    payload = {
        "version": SNAPSHOT_VERSION,
        "fields": list(SNAPSHOT_FIELDS),
        "min_history": MIN_HISTORY_BARS,
        "snapshot": snaps_at,
        "labels": label_setups(
            {"a": {"x": 1.0, "y": None}, "b": {"x": -2.5, "y": 3}, "c": {"x": 1.0, "y": None}}
        ),
        "parse": {
            "valid": {
                "s1": {"choice": "skip", "choice_confidence": 0.8, "fakeout": 0.6, "score": 0.4},
                "s2": {"choice": "take", "choice_confidence": None, "fakeout": 0.1, "score": 0.9},
            },
            "invalid": None,
        },
    }
    assert parse_verdicts(resp_with(invalid), ["s1", "s2"]) is None
    assert {k: v.as_dict() for k, v in parse_verdicts(resp_with(valid), ["s1", "s2"]).items()} == (
        payload["parse"]["valid"]
    )
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        fp = snapshot_fingerprint()
    assert fp == hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert "jev shadow" not in caplog.text  # the deliberate invalid parse does not log


def shifted(name: str):
    """Wrap module helper ``name`` so its integer look-back argument is one bar longer."""
    original = getattr(shadow_snapshot, name)
    return lambda x, n: original(x, n + 1)


@pytest.mark.parametrize(
    ("name", "make_value"),
    [
        ("_BTC_EMA_SPAN", lambda: 51),
        ("_RSI_PERIOD", lambda: 13),
        ("_ATR_PERIOD", lambda: 15),
        ("_VOL_WINDOW", lambda: 21),
        ("_BTC_RETURN_BARS", lambda: 41),
        ("MIN_HISTORY_BARS", lambda: 202),
        ("SNAPSHOT_VERSION", lambda: 2),
        ("SNAPSHOT_FIELDS", lambda: (*SNAPSHOT_FIELDS[:-1], "btc_ret_41_pct")),
        ("TAKE_CRITERIA", lambda: {"take": "only take is valid"}),
        ("_rounded", lambda: lambda v, d: None if not math.isfinite(v) else round(v, d + 1)),
        ("_wilder", lambda: shifted("_wilder")),
        ("_ema_dist_pct", lambda: shifted("_ema_dist_pct")),  # every EMA span + 1
        ("_pct_return", lambda: shifted("_pct_return")),  # every return look-back + 1
        ("label_setups", lambda: lambda snaps: {n: f"s{i}" for i, n in enumerate(snaps, 1)}),
    ],
)
def test_fingerprint_moves_when_a_formula_changes(fresh_fingerprint, monkeypatch, name, make_value):
    base = snapshot_fingerprint()
    monkeypatch.setattr(shadow_snapshot, name, make_value())
    snapshot_fingerprint.cache_clear()
    if name == "SNAPSHOT_FIELDS":  # blind_snapshot's own key check refuses the changed list
        with pytest.raises(AssertionError):
            snapshot_fingerprint()
    else:
        assert snapshot_fingerprint() != base
    monkeypatch.undo()
    snapshot_fingerprint.cache_clear()
    assert snapshot_fingerprint() == base
