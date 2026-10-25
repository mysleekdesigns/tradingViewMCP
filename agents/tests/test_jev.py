"""JevClient: request shape, retries, fail-open paths, and key secrecy. Offline only."""

from __future__ import annotations

import json
import logging
import math

import httpx
import numpy as np
import pytest

from research.jev import JevClient, JevResponse

NOUL_Q = {"q1": {"type": "noul", "instructions": "Is it?"}}
OK_BODY = {
    "model": "jev-1.13.0",
    "answers": {"q1": {"type": "noul", "noul": 0.98}},
    "usage": {"input_tokens": 12, "output_tokens": 0},
}


def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=OK_BODY)


def make(rec, sleeps=None, **kw) -> JevClient:
    return JevClient(
        transport=rec.transport,
        sleep=(sleeps.append if sleeps is not None else None) or (lambda _s: None),
        **kw,
    )


def test_happy_path_request_shape_and_parse(jev_api_key, jev_recorder):
    rec = jev_recorder(ok)
    client = make(rec)
    assert client.enabled
    resp = client.ask({"x": 1}, NOUL_Q)
    assert isinstance(resp, JevResponse)
    assert resp.model == "jev-1.13.0"
    assert resp.noul("q1") == pytest.approx(0.98)
    assert resp.usage == {"input_tokens": 12, "output_tokens": 0}
    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.typesafe.ai/v1/systemone"
    assert req.headers["authorization"] == f"Bearer {jev_api_key}"
    assert req.headers["content-type"] == "application/json"
    body = json.loads(req.content)
    assert body == {"model": "jev-latest", "state": {"x": 1}, "questions": NOUL_Q}


def test_string_state_and_custom_base_url(jev_api_key, jev_recorder):
    rec = jev_recorder(ok)
    client = make(rec, base_url="http://jev.local/", model="jev-1.13.0")
    assert client.ask("plain text state", NOUL_Q) is not None
    assert str(rec.requests[0].url) == "http://jev.local/v1/systemone"
    body = rec.bodies()[0]
    assert body["state"] == "plain text state"
    assert body["model"] == "jev-1.13.0"


def test_disabled_without_env_key_makes_no_request(monkeypatch, jev_recorder, caplog):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rec = jev_recorder(ok)
    client = make(rec)
    assert not client.enabled
    with caplog.at_level(logging.WARNING, logger="research.jev"):
        assert client.ask("s", NOUL_Q) is None
    assert rec.requests == []
    assert "disabled" in caplog.text


def test_blank_key_counts_as_disabled(monkeypatch, jev_recorder):
    monkeypatch.setenv("TYPESAFE_API_KEY", "   ")
    rec = jev_recorder(ok)
    assert not make(rec).enabled


def test_explicit_key_beats_env(monkeypatch, jev_recorder):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rec = jev_recorder(ok)
    client = make(rec, api_key="explicit-fake-key")
    assert client.ask("s", NOUL_Q) is not None
    assert rec.requests[0].headers["authorization"] == "Bearer explicit-fake-key"


@pytest.mark.parametrize("status", [400, 401, 413, 415, 500])
def test_non_retryable_errors_return_none_without_retry(jev_api_key, jev_recorder, status):
    sleeps: list[float] = []
    rec = jev_recorder(lambda r: httpx.Response(status, json={"detail": "nope"}))
    assert make(rec, sleeps).ask("s", NOUL_Q) is None
    assert len(rec.requests) == 1
    assert sleeps == []


def test_422_with_detail_array_returns_none(jev_api_key, jev_recorder):
    detail = {"detail": [{"loc": ["body", "questions"], "msg": "bad", "type": "value_error"}]}
    rec = jev_recorder(lambda r: httpx.Response(422, json=detail))
    assert make(rec).ask("s", NOUL_Q) is None
    assert len(rec.requests) == 1


@pytest.mark.parametrize("status", [429, 529])
def test_retryable_then_success(jev_api_key, jev_recorder, jev_sleeps, status):
    replies = iter([httpx.Response(status), httpx.Response(200, json=OK_BODY)])
    rec = jev_recorder(lambda r: next(replies))
    resp = make(rec, jev_sleeps).ask("s", NOUL_Q)
    assert resp is not None and resp.noul("q1") == pytest.approx(0.98)
    assert len(rec.requests) == 2
    assert jev_sleeps == [0.5]


def test_529_exhausted_returns_none_with_exponential_backoff(jev_api_key, jev_recorder, jev_sleeps):
    rec = jev_recorder(lambda r: httpx.Response(529))
    assert make(rec, jev_sleeps, max_retries=2).ask("s", NOUL_Q) is None
    assert len(rec.requests) == 3
    assert jev_sleeps == [0.5, 1.0]


def test_retry_after_header_is_honoured_and_capped(jev_api_key, jev_recorder, jev_sleeps):
    replies = iter(
        [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(429, headers={"Retry-After": "999"}),
            httpx.Response(200, json=OK_BODY),
        ]
    )
    rec = jev_recorder(lambda r: next(replies))
    assert make(rec, jev_sleeps).ask("s", NOUL_Q) is not None
    assert jev_sleeps == [3.0, 8.0]


def test_zero_retries(jev_api_key, jev_recorder, jev_sleeps):
    rec = jev_recorder(lambda r: httpx.Response(429))
    assert make(rec, jev_sleeps, max_retries=0).ask("s", NOUL_Q) is None
    assert len(rec.requests) == 1 and jev_sleeps == []


def _raise(exc_type):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("boom", request=request)

    return handler


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError])
def test_timeouts_and_transport_errors_return_none(jev_api_key, jev_recorder, exc, caplog):
    rec = jev_recorder(_raise(exc))
    with caplog.at_level(logging.WARNING, logger="research.jev"):
        assert make(rec).ask("s", NOUL_Q) is None
    assert len(rec.requests) == 1
    assert "jev unavailable" in caplog.text


def test_unexpected_exception_is_swallowed(jev_api_key, jev_recorder):
    def handler(request):
        raise RuntimeError("weird")

    assert make(jev_recorder(handler)).ask("s", NOUL_Q) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json {"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"model": "m"}),
        httpx.Response(200, json={"answers": {}}),
        httpx.Response(200, json={"answers": {"other": {"type": "noul", "noul": 0.5}}}),
        httpx.Response(200, json={"answers": {"q1": {"type": "score", "score": 1.0}}}),
        httpx.Response(200, json={"answers": {"q1": {"type": "noul", "noul": 1.5}}}),
        httpx.Response(200, json={"answers": {"q1": {"type": "noul", "noul": "0.9"}}}),
        httpx.Response(200, json={"answers": {"q1": {"type": "noul"}}}),
    ],
    ids=[
        "bad-json",
        "list",
        "no-answers",
        "empty-answers",
        "wrong-id",
        "type-mismatch",
        "out-of-range",
        "string-prob",
        "missing-value",
    ],
)
def test_malformed_responses_return_none(jev_api_key, jev_recorder, response):
    assert make(jev_recorder(lambda r: response)).ask("s", NOUL_Q) is None


def test_choice_and_score_validation(jev_api_key, jev_recorder):
    questions = {
        "c": {"type": "choice", "instructions": "pick", "criteria": {"a": "A", "b": "B"}},
        "s": {"type": "score", "instructions": "rate", "criteria": ["low", "high"]},
    }
    good = {
        "answers": {
            "c": {
                "type": "choice",
                "choice": "b",
                "confidence": 0.7,
                "probabilities": {"a": 0.3, "b": 0.7},
            },
            "s": {"type": "score", "score": 0.6, "confidence": 0.55},
        }
    }
    resp = make(jev_recorder(lambda r: httpx.Response(200, json=good))).ask("s", questions)
    assert resp is not None
    assert resp.choice("c") == ("b", 0.7, {"a": 0.3, "b": 0.7})
    assert resp.noul("c") is None
    assert resp.model == "" and resp.usage == {}

    bad = json.loads(json.dumps(good))
    bad["answers"]["c"]["choice"] = "zzz"
    assert make(jev_recorder(lambda r: httpx.Response(200, json=bad))).ask("s", questions) is None


def test_bad_questions_make_no_request(jev_api_key, jev_recorder):
    rec = jev_recorder(ok)
    client = make(rec)
    assert client.ask("s", {}) is None
    assert client.ask("s", {"q": {"type": "essay"}}) is None
    assert rec.requests == []


def test_state_serialises_numpy_and_non_finite(jev_api_key, jev_recorder):
    rec = jev_recorder(ok)
    state = {"a": np.float64(1.5), "b": np.int64(3), "c": math.nan, "d": (1, 2), "e": object}
    assert make(rec).ask(state, NOUL_Q) is not None
    sent = rec.bodies()[0]["state"]
    assert sent["a"] == 1.5 and sent["b"] == 3 and sent["c"] is None and sent["d"] == [1, 2]
    assert isinstance(sent["e"], str)


def test_key_never_in_logs_reprs_or_results(jev_api_key, jev_recorder, caplog):
    def leaky(request: httpx.Request) -> httpx.Response:
        # A transport error whose message echoes the auth header must still not leak.
        raise httpx.ConnectError(f"failed {request.headers['authorization']}", request=request)

    handlers = [
        ok,
        leaky,
        _raise(httpx.ReadTimeout),
        lambda r: httpx.Response(401, json={"detail": f"bad key {jev_api_key}"}),
        lambda r: httpx.Response(529),
        lambda r: httpx.Response(200, content=b"{"),
    ]
    results = []
    recs = []
    with caplog.at_level(logging.DEBUG):
        for h in handlers:
            rec = jev_recorder(h)
            recs.append(rec)
            client = make(rec)
            results.append(client.ask({"k": "v"}, NOUL_Q))
            assert jev_api_key not in repr(client)
            assert jev_api_key not in str(client)
            assert jev_api_key not in repr(vars(client))
    assert jev_api_key not in caplog.text
    assert jev_api_key not in repr(results)
    for rec in recs:
        for req in rec.requests:
            assert jev_api_key not in repr(req)
            assert jev_api_key not in repr(req.headers)
            assert jev_api_key not in req.content.decode()
