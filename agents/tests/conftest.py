"""Shared pytest fixtures. Jev fixtures are prefixed ``jev_`` and fully offline (MockTransport)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

# Obviously fake; used to prove the key never leaks into logs, reprs, or results.
JEV_FAKE_KEY = "fake-typesafe-key-for-tests-0000"


@pytest.fixture
def jev_api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a fake TYPESAFE_API_KEY for the test and return it."""
    monkeypatch.setenv("TYPESAFE_API_KEY", JEV_FAKE_KEY)
    return JEV_FAKE_KEY


@pytest.fixture
def jev_sleeps() -> list[float]:
    """Records backoff delays instead of sleeping (pass ``jev_sleeps.append`` as ``sleep``)."""
    return []


AnswerFn = Callable[[str, dict[str, Any]], dict[str, Any]]


def _default_answer(qid: str, question: dict[str, Any]) -> dict[str, Any]:
    qtype = question["type"]
    if qtype == "noul":
        return {"type": "noul", "noul": 0.5}
    if qtype == "choice":
        opts = list(question["criteria"])
        p = 1.0 / len(opts)
        return {
            "type": "choice",
            "choice": opts[-1],
            "confidence": p,
            "probabilities": dict.fromkeys(opts, p),
        }
    return {"type": "score", "score": 0.0, "confidence": 0.5}


@pytest.fixture
def jev_answer_all() -> Callable[[httpx.Request, AnswerFn | None], httpx.Response]:
    """Build a 200 response answering every question in ``request`` via ``answer(qid, q)``."""

    def build(request: httpx.Request, answer: AnswerFn | None = None) -> httpx.Response:
        body = json.loads(request.content)
        fn = answer or _default_answer
        answers = {qid: fn(qid, q) for qid, q in body["questions"].items()}
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": 0},
            },
        )

    return build


class JevRecorder:
    """A MockTransport that records every request and delegates to ``handler``."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def jev_recorder() -> Callable[[Callable[[httpx.Request], httpx.Response]], JevRecorder]:
    """Factory: ``rec = jev_recorder(handler)``; pass ``rec.transport`` to ``JevClient``."""
    return JevRecorder
