"""Minimal client for TypeSafe's Jev decision model (``POST /v1/systemone``).

Jev answers typed questions (``noul`` = P(yes), ``choice``, ``score``) about a ``state`` and
returns probabilities. It cannot write text or code, and TypeSafe documents it as unreliable for
arithmetic, dates, and counting, so callers compute those in Python and give Jev the facts.

Everything here fails open: :meth:`JevClient.ask` returns ``None`` for any problem (no key,
network error, timeout, HTTP error, bad JSON, missing answers) and never raises. Gates treat
``None`` as "Jev unavailable" and fall back to their Python rules.

The API key comes from ``TYPESAFE_API_KEY`` (or the constructor). It is kept in a wrapper whose
``repr`` is redacted and is scrubbed from every log line, so it never reaches logs, exceptions,
reprs, or return values.
"""

from __future__ import annotations

import json
import logging
import math
import numbers
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ENV_KEY = "TYPESAFE_API_KEY"
DEFAULT_BASE_URL = "https://api.typesafe.ai"
ENDPOINT = "/v1/systemone"

_RETRY_STATUSES = frozenset({429, 529})
_BACKOFF_BASE_S = 0.5
_BACKOFF_CAP_S = 8.0
_QUESTION_TYPES = frozenset({"noul", "choice", "score"})


class _Secret:
    """Holds the API key; ``repr``/``str`` never show it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


@dataclass(frozen=True)
class JevResponse:
    """A validated Jev reply: every asked question id has a well-formed answer."""

    model: str
    answers: dict[str, dict[str, Any]]
    usage: dict[str, int]

    def noul(self, qid: str) -> float | None:
        """P(yes) for a ``noul`` question, or ``None`` if absent."""
        ans = self.answers.get(qid)
        if not ans or ans.get("type") != "noul":
            return None
        return float(ans["noul"])

    def choice(self, qid: str) -> tuple[str, float | None, dict[str, float]] | None:
        """``(choice, confidence, probabilities)`` for a ``choice`` question."""
        ans = self.answers.get(qid)
        if not ans or ans.get("type") != "choice":
            return None
        conf = ans.get("confidence")
        probs = ans.get("probabilities") or {}
        return (
            str(ans["choice"]),
            float(conf) if _is_prob(conf) else None,
            {str(k): float(v) for k, v in probs.items() if _is_prob(v)},
        )


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v))


def _is_prob(v: object) -> bool:
    return _is_number(v) and 0.0 <= float(v) <= 1.0  # type: ignore[arg-type]


class JevClient:
    """Fail-open Jev client. ``ask`` returns ``None`` instead of raising."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "jev-latest",
        timeout_s: float = 10.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get(ENV_KEY)
        self._key: _Secret | None = _Secret(key.strip()) if key and key.strip() else None
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = float(timeout_s)
        self.max_retries = max(0, int(max_retries))
        self._transport = transport
        self._sleep = sleep

    def __repr__(self) -> str:
        return (
            f"JevClient(base_url={self.base_url!r}, model={self.model!r}, "
            f"enabled={self.enabled}, key=<redacted>)"
        )

    @property
    def enabled(self) -> bool:
        return self._key is not None

    # ------------------------------------------------------------------ helpers

    def _redact(self, text: str) -> str:
        if self._key is not None:
            secret = self._key.reveal()
            if secret:
                text = text.replace(secret, "<redacted>")
        return text

    def _unavailable(self, reason: str) -> None:
        logger.warning("jev unavailable: %s", self._redact(reason))

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2**attempt))
        if response is not None:
            retry_after = response.headers.get("retry-after", "")
            try:
                ra = float(retry_after)
            except ValueError:
                ra = -1.0
            if math.isfinite(ra) and ra >= 0:
                delay = min(_BACKOFF_CAP_S, max(delay, ra))
        return delay

    # ------------------------------------------------------------------ public

    def ask(
        self,
        state: str | Mapping[str, Any] | Sequence[Any],
        questions: Mapping[str, Mapping[str, Any]],
    ) -> JevResponse | None:
        """Ask Jev ``questions`` about ``state``. ``None`` means unavailable; never raises."""
        try:
            return self._ask(state, questions)
        except Exception as exc:  # fail open on anything unexpected
            self._unavailable(f"unexpected {type(exc).__name__}")
            return None

    def _ask(
        self,
        state: str | Mapping[str, Any] | Sequence[Any],
        questions: Mapping[str, Mapping[str, Any]],
    ) -> JevResponse | None:
        if self._key is None:
            self._unavailable(f"disabled ({ENV_KEY} not set)")
            return None
        if not questions:
            self._unavailable("no questions")
            return None
        for qid, q in questions.items():
            if not isinstance(qid, str) or q.get("type") not in _QUESTION_TYPES:
                self._unavailable("malformed question")
                return None

        payload = {
            "model": self.model,
            "state": state if isinstance(state, str) else _jsonable(state),
            "questions": {qid: dict(q) for qid, q in questions.items()},
        }
        try:
            body = json.dumps(payload, allow_nan=False, default=str)
        except (TypeError, ValueError):
            self._unavailable("state is not JSON-serialisable")
            return None

        headers = {
            "Authorization": f"Bearer {self._key.reveal()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{self.base_url}{ENDPOINT}"
        response: httpx.Response | None = None
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout_s) as client:
                for attempt in range(self.max_retries + 1):
                    try:
                        response = client.post(url, content=body, headers=headers)
                    except httpx.TimeoutException as exc:
                        self._unavailable(f"timeout ({type(exc).__name__})")
                        return None
                    except httpx.HTTPError as exc:
                        self._unavailable(f"transport error ({type(exc).__name__})")
                        return None
                    if response.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                        delay = self._backoff(attempt, response)
                        logger.info(
                            "jev HTTP %d, retry %d/%d in %.2fs",
                            response.status_code,
                            attempt + 1,
                            self.max_retries,
                            delay,
                        )
                        self._sleep(delay)
                        continue
                    break
        except httpx.HTTPError as exc:
            self._unavailable(f"client error ({type(exc).__name__})")
            return None

        if response is None:
            self._unavailable("no response")
            return None
        if response.status_code != 200:
            self._unavailable(f"HTTP {response.status_code}")
            return None
        try:
            data = response.json()
        except (ValueError, UnicodeDecodeError):
            self._unavailable("response is not JSON")
            return None
        return self._validate(data, questions)

    def _validate(
        self, data: object, questions: Mapping[str, Mapping[str, Any]]
    ) -> JevResponse | None:
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            self._unavailable("response has no answers object")
            return None
        raw_answers: dict[str, Any] = data["answers"]
        answers: dict[str, dict[str, Any]] = {}
        for qid, q in questions.items():
            ans = raw_answers.get(qid)
            problem = _answer_problem(ans, q)
            if problem:
                self._unavailable(f"answer {qid!r}: {problem}")
                return None
            answers[qid] = dict(ans)  # type: ignore[arg-type]
        usage_raw = data.get("usage")
        usage: dict[str, int] = {}
        if isinstance(usage_raw, dict):
            usage = {
                str(k): int(v)
                for k, v in usage_raw.items()
                if isinstance(v, int) and not isinstance(v, bool)
            }
        model = data.get("model")
        return JevResponse(
            model=model if isinstance(model, str) else "", answers=answers, usage=usage
        )


def _answer_problem(ans: object, question: Mapping[str, Any]) -> str | None:
    """Return why ``ans`` is not a valid answer to ``question``, or ``None`` if it is."""
    if not isinstance(ans, dict):
        return "missing"
    qtype = question.get("type")
    if ans.get("type") != qtype:
        return "type mismatch"
    if qtype == "noul":
        return None if _is_prob(ans.get("noul")) else "noul is not a probability"
    if qtype == "choice":
        choice = ans.get("choice")
        criteria = question.get("criteria")
        if not isinstance(choice, str):
            return "choice missing"
        if isinstance(criteria, Mapping) and choice not in criteria:
            return "choice not among options"
        probs = ans.get("probabilities")
        if probs is not None and (
            not isinstance(probs, dict) or not all(_is_prob(v) for v in probs.values())
        ):
            return "bad probabilities"
        return None
    if qtype == "score":
        return None if _is_number(ans.get("score")) else "score is not a number"
    return "unknown type"


def _jsonable(value: Any) -> Any:
    """Convert mappings/sequences to plain JSON types; non-finite floats become ``None``."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, numbers.Integral):  # numpy integers
        return int(value)
    if isinstance(value, numbers.Real):  # numpy floats
        f = float(value)
        return f if math.isfinite(f) else None
    return str(value)
