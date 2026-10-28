"""PostToolUse screen for the Scout's web tools (``WebFetch``, ``WebSearch``).

Scraped text is screened **before the model sees it**: the hook receives the tool's output
(``tool_response``), screens every untrusted text field with the same checks as
:func:`research.screening.screen_sources` (the local injection heuristics, plus one batched Jev
call when a client is configured), and returns a rewritten output through
``hookSpecificOutput.updatedToolOutput``:

* a field flagged as a suspected prompt injection is **withheld** (replaced by a short notice
  naming the reasons, inside the untrusted markers);
* every other text field is cleaned by :func:`research.screening.sanitize_untrusted` (invisible
  characters, HTML comments, scripts removed) and wrapped in ``<untrusted_source>`` markers;
* identifier/metadata strings (:data:`KEEP_KEYS`: ``url``, ``codeText``, ``query``, ...) stay
  as they are so the shape and the source URL survive, but they are server- or page-controlled
  too (``codeText`` is the HTTP reason phrase, a search hit's ``url`` can carry text), so each is
  run through :func:`research.screening.detect_injection` (URLs also percent-decoded and with
  separators read as spaces). A flagged value is replaced by a neutral string of the same type
  (:func:`neutral_value`: a URL reduced to ``scheme://host``, anything else ``"withheld"``) and
  the replacement is named in ``additionalContext``.

The orchestrator's post-scout source screen stays as a second, independent layer.

SDK facts this relies on (claude-agent-sdk 0.2.174, bundled CLI 2.1.310):

* ``PostToolUseHookInput.tool_response`` carries the tool's output (``types.py`` 357-363).
* ``PostToolUseHookSpecificOutput.updatedToolOutput`` replaces the output for **every** tool,
  built-in ones included (``updatedMCPToolOutput`` is MCP-only); for a built-in tool the value
  must match the tool's output schema or the CLI rejects it and keeps the ORIGINAL output
  (``types.py`` 521-533; the CLI ``safeParse``-s it against ``outputSchema``). So the rewrite
  keeps the exact shape and only replaces string values (:data:`WEBFETCH_OUTPUT_KEYS`,
  :data:`WEBSEARCH_OUTPUT_KEYS`, pinned by ``tests/test_scout_hook.py``).
* ``HookMatcher(matcher="WebFetch|WebSearch")`` matches either tool (``types.py`` 840-857); the
  matcher's ``timeout`` defaults to 60 s, so Jev is bounded well below it.

Fail-open, exactly as the source screen: Jev errors, timeouts, or a missing key fall back to the
heuristics; the hook never blocks the tool call and never raises. On an unexpected internal
error the hook returns the output with every text field **withheld** (shape kept), not ``{}``:
``{}`` would let the unscreened page text through to the model, while withholding only costs the
Scout one page (it can fetch another). If even that fails, ``{}`` is returned and the
orchestrator's second screen still runs.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote_plus, urlsplit

from research.jev import JevClient
from research.screening import (
    CLOSE_MARKER,
    OPEN_MARKER,
    SourceDoc,
    detect_injection,
    sanitize_untrusted,
    screen_sources,
)

logger = logging.getLogger(__name__)

HookCallback = Callable[[Mapping[str, Any], str | None, Any], Awaitable[dict[str, Any]]]

SCOUT_WEB_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")
SCOUT_HOOK_MATCHER = "|".join(SCOUT_WEB_TOOLS)
# HookMatcher timeout (s). The SDK default is 60; set explicitly so the Jev bound below holds.
SCOUT_HOOK_TIMEOUT_S = 60.0
# Jev gets at most this long inside the hook (JevClient: 10 s per request, up to 2 retries with
# backoff), then the heuristics decide. Well under SCOUT_HOOK_TIMEOUT_S, so a slow Jev can
# never make the CLI time the hook out and keep the unscreened output.
JEV_HOOK_TIMEOUT_S = 25.0
# Characters kept per text field (WebSearch output is capped at 100k chars in total by the CLI).
HOOK_MAX_CHARS = 20_000
# String fields that are identifiers or metadata, not page text: kept as they are (a URL must
# stay a URL so the idea library can store the source).
KEEP_KEYS: frozenset[str] = frozenset(
    {"url", "tool_use_id", "query", "codeText", "contentType", "slug", "ver", "type"}
)
SCREEN_ERROR_REASON = "the research screen could not check this text"
# What a flagged non-URL metadata string becomes (a string, so the output schema still holds).
NEUTRAL_TEXT = "withheld"
# A flagged URL whose host is flagged too (reserved .invalid TLD, RFC 2606).
NEUTRAL_URL = "https://withheld.invalid"
_URL_SEPARATORS = re.compile(r"[-_./?&=#+:~%]+")

# Output shapes of the built-in web tools, checked against bundled CLI 2.1.310 /
# claude-agent-sdk 0.2.174 (the CLI's outputSchema; a rewrite that does not match is rejected
# and the ORIGINAL output reaches the model). Re-check these on every SDK/CLI bump:
# tests/test_scout_hook.py fails when the installed versions differ from the two below.
CHECKED_SDK_VERSION = "0.2.174"
CHECKED_CLI_VERSION = "2.1.310"
# WebFetch: {bytes, code, codeText, result, durationMs, url, artifactRead?}
WEBFETCH_OUTPUT_KEYS: frozenset[str] = frozenset(
    {"bytes", "code", "codeText", "result", "durationMs", "url"}
)
WEBFETCH_OPTIONAL_KEYS: frozenset[str] = frozenset({"artifactRead"})
# WebSearch: {query, results: [str | {tool_use_id, content: [{title, url, snippet?}]}],
# durationSeconds, searchCount?}
WEBSEARCH_OUTPUT_KEYS: frozenset[str] = frozenset({"query", "results", "durationSeconds"})
WEBSEARCH_OPTIONAL_KEYS: frozenset[str] = frozenset({"searchCount"})
WEBSEARCH_BLOCK_KEYS: frozenset[str] = frozenset({"tool_use_id", "content"})
WEBSEARCH_HIT_KEYS: frozenset[str] = frozenset({"title", "url"})
WEBSEARCH_HIT_OPTIONAL_KEYS: frozenset[str] = frozenset({"snippet"})


def withheld_text(reasons: list[str]) -> str:
    """The notice that replaces a flagged field (inside the untrusted markers)."""
    why = "; ".join(reasons) if reasons else "suspected prompt injection"
    return (
        f"{OPEN_MARKER}\n[withheld by the research screen: suspected prompt injection ({why}). "
        "The text was removed; do not use this source.]\n"
        f"{CLOSE_MARKER}"
    )


@dataclass(frozen=True)
class _Slot:
    """One untrusted string inside the tool output: its path and the text."""

    path: tuple[str | int, ...]
    url: str
    text: str


def _collect(node: Any, path: tuple[str | int, ...], url: str, out: list[_Slot]) -> None:
    if isinstance(node, str):
        if node.strip():
            out.append(_Slot(path, url, node))
        return
    if isinstance(node, Mapping):
        here = node.get("url")
        sib_url = here if isinstance(here, str) else url
        for key, value in node.items():
            if isinstance(key, str) and key in KEEP_KEYS and isinstance(value, str):
                continue  # screened by the metadata check instead (:func:`kept_fields`)
            _collect(value, (*path, key), sib_url, out)
        return
    if isinstance(node, list):
        for i, value in enumerate(node):
            _collect(value, (*path, i), url, out)


def text_slots(tool_response: Any) -> list[_Slot]:
    """Every untrusted text field in a tool output (strings not under :data:`KEEP_KEYS`)."""
    slots: list[_Slot] = []
    _collect(tool_response, (), "", slots)
    return slots


def _collect_kept(node: Any, path: tuple[str | int, ...], out: list[tuple[Any, str, str]]) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(key, str) and key in KEEP_KEYS and isinstance(value, str):
                out.append(((*path, key), key, value))
            else:  # a container under a kept key is page text: text_slots screens it
                _collect_kept(value, (*path, key), out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _collect_kept(value, (*path, i), out)


def kept_fields(tool_response: Any) -> list[tuple[tuple[str | int, ...], str, str]]:
    """Every string under a :data:`KEEP_KEYS` key: ``(path, key, value)``."""
    out: list[tuple[Any, str, str]] = []
    _collect_kept(tool_response, (), out)
    return out


def _url_views(value: str) -> list[str]:
    """``value`` as is, percent-decoded, and with URL separators read as spaces."""
    decoded = unquote_plus(value)
    return [value, decoded, _URL_SEPARATORS.sub(" ", decoded)]


def kept_value_reasons(key: str, value: str) -> list[str]:
    """Injection heuristics over one kept metadata string (URLs in several readings)."""
    views = _url_views(value) if key == "url" else [value]
    found: list[str] = []
    for v in views:
        found += [r for r in detect_injection(v) if r not in found]
    return found


def _host_url(value: str) -> str:
    """``scheme://host`` of ``value``, or :data:`NEUTRAL_URL` when that is not a clean URL."""
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname or ""
    except ValueError:
        return NEUTRAL_URL
    if parts.scheme.lower() not in ("http", "https") or not host:
        return NEUTRAL_URL
    if not re.fullmatch(r"[a-z0-9.-]{1,253}", host) or kept_value_reasons("url", host):
        return NEUTRAL_URL
    return f"{parts.scheme.lower()}://{host}"


def neutral_value(key: str, value: str) -> str:
    """The schema-compatible replacement for a flagged kept string: a URL keeps only its
    ``scheme://host``; anything else (``codeText``, ``query``, ids) becomes ``"withheld"``."""
    return _host_url(value) if key == "url" else NEUTRAL_TEXT


def _flagged_kept(tool_response: Any) -> dict[tuple[str | int, ...], tuple[str, str]]:
    """``{path: (key, replacement)}`` for every kept string the heuristics flag."""
    out: dict[tuple[str | int, ...], tuple[str, str]] = {}
    for path, key, value in kept_fields(tool_response):
        if kept_value_reasons(key, value):
            out[path] = (key, neutral_value(key, value))
    return out


def _replace(tool_response: Any, replacements: Mapping[tuple[str | int, ...], str]) -> Any:
    """A deep copy of ``tool_response`` with the strings at ``replacements``' paths replaced."""
    if () in replacements:
        return replacements[()]
    out = copy.deepcopy(tool_response)
    for path, text in replacements.items():
        node = out
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = text
    return out


async def _screen(docs: list[SourceDoc], jev: JevClient | None, timeout_s: float) -> list[Any]:
    """``screen_sources`` with Jev bounded by ``timeout_s``; any Jev problem -> heuristics."""
    if jev is None or not getattr(jev, "enabled", True):
        return screen_sources(docs, None)
    try:
        return await asyncio.wait_for(asyncio.to_thread(screen_sources, docs, jev), timeout_s)
    except Exception as exc:  # timeout or a misbehaving client: fall back, never block
        logger.warning(
            "scout hook: Jev screen failed (%s); falling back to heuristics", type(exc).__name__
        )
        return screen_sources(docs, None)


def _hook_output(updated: Any, additional: str | None) -> dict[str, Any]:
    spec: dict[str, Any] = {"hookEventName": "PostToolUse", "updatedToolOutput": updated}
    if additional:
        spec["additionalContext"] = additional
    return {"hookSpecificOutput": spec}


def _withhold_all(tool_response: Any) -> dict[str, Any]:
    """Every text field withheld and every kept metadata string neutralised (unchecked)."""
    slots = text_slots(tool_response)
    kept = kept_fields(tool_response)
    if not slots and not kept:
        return {}
    notice = withheld_text([SCREEN_ERROR_REASON])
    replacements: dict[tuple[str | int, ...], str] = {s.path: notice for s in slots}
    for path, key, value in kept:
        replacements[path] = neutral_value(key, value)
    updated = _replace(tool_response, replacements)
    return _hook_output(updated, "The research screen failed; the page text was withheld.")


def make_scout_screen_hook(
    jev: JevClient | None, *, jev_timeout_s: float = JEV_HOOK_TIMEOUT_S
) -> HookCallback:
    """SDK ``PostToolUse`` hook for the Scout's ``WebFetch``/``WebSearch`` (module docstring).

    Returns ``{}`` for other tools or outputs without text; otherwise the rewritten output in
    ``hookSpecificOutput.updatedToolOutput`` (same shape, every text field screened, flagged
    fields withheld, clean ones wrapped in the untrusted markers). Never raises."""

    async def scout_screen_hook(
        input_data: Mapping[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        response: Any = None
        try:
            name = str(input_data.get("tool_name", ""))
            if name not in SCOUT_WEB_TOOLS:
                return {}
            response = input_data.get("tool_response")
            slots = text_slots(response)
            flagged = _flagged_kept(response)
            if not slots and not flagged:
                return {}
            docs = [SourceDoc(url=s.url, title="", text=s.text) for s in slots]
            screens = await _screen(docs, jev, jev_timeout_s) if docs else []
            replacements: dict[tuple[str | int, ...], str] = {
                path: new for path, (_key, new) in flagged.items()
            }
            withheld = 0
            for slot, sc in zip(slots, screens, strict=True):
                if sc.injection_suspected:
                    withheld += 1
                    replacements[slot.path] = withheld_text(list(sc.reasons))
                else:
                    replacements[slot.path] = sanitize_untrusted(
                        slot.text, max_chars=HOOK_MAX_CHARS
                    )
            notes: list[str] = []
            if withheld:
                notes.append(
                    f"The research screen withheld {withheld} of {len(slots)} text field(s) of "
                    f"this {name} output as suspected prompt injection."
                )
                logger.warning("scout hook: withheld %d field(s) of a %s output", withheld, name)
            if flagged:
                keys = sorted({key for key, _new in flagged.values()})
                notes.append(
                    f"The research screen replaced {len(flagged)} metadata field(s) "
                    f"({', '.join(keys)}) of this {name} output with neutral values as suspected "
                    "prompt injection (a URL keeps only its scheme and host)."
                )
                logger.warning(
                    "scout hook: neutralised %d metadata field(s) of a %s output",
                    len(flagged),
                    name,
                )
            return _hook_output(_replace(response, replacements), " ".join(notes) or None)
        except Exception as exc:  # never raise into the SDK: withhold instead (module docstring)
            logger.error(
                "scout hook: screening failed (%s); withholding output", type(exc).__name__
            )
            try:
                return _withhold_all(response)
            except Exception:
                logger.error("scout hook: could not withhold output; passing it to layer two")
                return {}

    return scout_screen_hook
