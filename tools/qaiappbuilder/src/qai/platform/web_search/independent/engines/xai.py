# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""xAI (Grok) web-search engine — LLM-mediated, keyed HTTP API.

Unlike a SERP engine, xAI does not return a result list: Grok runs the search
server-side through its native ``web_search`` tool and answers in prose, and the
pages it consulted come back as *citations*. This engine posts to the xAI
Responses API (``POST /v1/responses``) with ``tools: [{"type": "web_search"}]``
exactly as the reference implementation's ``providers/xai.ts`` does, then harvests every citation
into
:class:`EngineHit`.

Citations arrive in two shapes and both are collected, in the reference implementation's order:

* ``url_citation`` annotations, which may hang off the response itself, off an
  ``output`` item, or off a content part inside an item. These carry a title and
  the cited text, so they make the richest hits.
* a flat ``citations`` array of bare URLs, appended after the annotations.

De-duplication is by URL, so an annotation always wins over the bare URL for the
same page; ``rank`` is the resulting 0-based order and the list is capped to
``count``.

Why an answer with zero citations is an *error*, not an empty list
-----------------------------------------------------------------
Our :class:`Engine` protocol returns only hits — there is no answer field, so a
prose answer with nothing to cite is information the aggregator structurally
cannot carry. Returning ``[]`` would assert "this engine legitimately found
nothing", which is false and would make the aggregator treat a *successful*
Grok call as a null result while leaving its health score untouched. We raise
:class:`EngineError` instead: the call is a real failure *for this port*, is
attributed to the engine, and the message names the cause. Symmetrically, a 200
whose body carries neither an answer nor citations is not a search result at all
but an upstream refusal, and raises :class:`EngineBlockedError` per the
engine-layer rule that a refusal must never be parsed into an empty list.

Recency has no parameter on this API — xAI's ``web_search`` tool exposes only
domain filters and image toggles — so ``recency`` is **ignored silently**,
exactly as the reference implementation does (the reference's xai.ts never reads it). Rewriting the
query text to
fake a date filter would corrupt the search terms the model actually searches
for.

API docs: https://docs.x.ai/developers/tools/web-search
"""

from __future__ import annotations

from typing import Any

from qai.platform.web_search.independent.engines.base import (
    EngineHit,
    EngineQuery,
    EngineType,
)
from qai.platform.web_search.independent.errors import (
    EngineAuthError,
    EngineBlockedError,
    EngineError,
)
from qai.platform.web_search.independent.http_client import HttpClient

__all__ = ["XaiEngine"]

_ENDPOINT = "https://api.x.ai/v1/responses"

#: the reference implementation pins the same model for web search (``XAI_WEB_SEARCH_MODEL``).
_MODEL = "grok-4.5"

#: grok-4.5 defaults ``reasoning.effort`` to "high"; the reference implementation pins "low" because
#: a
#: search round-trip is latency-sensitive simple tool calling, not deep
#: reasoning, and runs under a hard 60s deadline.
_REASONING_EFFORT = "low"

#: the reference implementation clamps ``num_results`` into [1, 30], falling back to 10 when it is
#: absent/zero, then caps the mapped sources at that value
#: (the reference's xai.ts:18-19, 289, 198-207).
_DEFAULT_COUNT = 10
_MAX_COUNT = 30
_MIN_COUNT = 1

_SYSTEM_PROMPT = (
    "You are a web search engine. Search the web for the user's query and "
    "answer it concisely, citing every source you used."
)

#: Snippet fallback for a citation that carries no cited text, mirroring
#: ``GeminiEngine``: the leading slice of the answer is better context than "".
_FALLBACK_SNIPPET_CHARS = 200

_URL_CITATION_TYPE = "url_citation"
_TEXT_PART_TYPES = frozenset({"output_text", "text"})

#: Upstream soft-failure markers that arrive with HTTP 200 instead of a status.
_OVERLOADED_MARKERS = ("overloaded", "rate_limit", "rate limit", "too many requests")


def _clamp_count(count: int) -> int:
    """Mirror the reference implementation's ``clampNumResults(count, 10, 30)``."""
    if count <= 0:
        return _DEFAULT_COUNT
    return min(_MAX_COUNT, max(_MIN_COUNT, count))


def _answer_text(payload: dict[str, Any]) -> str:
    """Reconstruct the prose answer, preferring the flat ``output_text``."""
    top_level = payload.get("output_text")
    if isinstance(top_level, str) and top_level.strip():
        return top_level.strip()

    parts: list[str] = []
    for item in payload.get("output") or ():
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or ():
            if not isinstance(part, dict) or part.get("type") not in _TEXT_PART_TYPES:
                continue
            text = part.get("output_text") or part.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


class _CitationCollector:
    """Accumulates unique citations in first-seen order."""

    __slots__ = ("_seen", "titles", "urls", "texts")

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.urls: list[str] = []
        self.titles: list[str] = []
        self.texts: list[str] = []

    def add(self, url: Any, title: Any = None, cited_text: Any = None) -> None:
        if not isinstance(url, str):
            return
        trimmed = url.strip()
        if not trimmed or trimmed in self._seen:
            return
        self._seen.add(trimmed)
        self.urls.append(trimmed)
        self.titles.append(title.strip() if isinstance(title, str) and title.strip() else trimmed)
        self.texts.append(cited_text.strip() if isinstance(cited_text, str) else "")

    def add_annotations(self, annotations: Any) -> None:
        for annotation in annotations or ():
            if not isinstance(annotation, dict):
                continue
            if annotation.get("type") != _URL_CITATION_TYPE:
                continue
            self.add(
                annotation.get("url"),
                annotation.get("title"),
                annotation.get("cited_text") or annotation.get("text"),
            )


def _collect(payload: dict[str, Any]) -> _CitationCollector:
    """Walk every place xAI hides a citation, in the reference implementation's order."""
    collector = _CitationCollector()
    collector.add_annotations(payload.get("annotations"))
    for item in payload.get("output") or ():
        if not isinstance(item, dict):
            continue
        collector.add_annotations(item.get("annotations"))
        for part in item.get("content") or ():
            if isinstance(part, dict):
                collector.add_annotations(part.get("annotations"))
    for url in payload.get("citations") or ():
        collector.add(url)
    return collector


def _error_text(payload: dict[str, Any]) -> str:
    """Flatten an upstream ``error`` field (string or object) to lowercase text."""
    error = payload.get("error")
    if isinstance(error, str):
        return error.lower()
    if isinstance(error, dict):
        return " ".join(
            value.lower() for value in error.values() if isinstance(value, str)
        )
    return ""


class XaiEngine:
    """Grok web search over the xAI Responses API."""

    engine_id: str = "xai"
    engine_type: EngineType = "http_api"

    __slots__ = ("_credential", "_http")

    def __init__(
        self,
        *,
        credential: str | None = None,
        http_client: HttpClient | None = None,
    ) -> None:
        if credential is None:
            raise EngineAuthError(self.engine_id, f"{self.engine_id} requires credential")
        self._credential = credential
        self._http = http_client if http_client is not None else HttpClient(self.engine_id)

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        wanted = _clamp_count(query.count)
        # ``recency`` is intentionally unused: the Responses API's ``web_search``
        # tool exposes no time-window parameter (see module docstring).
        body: dict[str, Any] = {
            "model": _MODEL,
            "input": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": query.query},
            ],
            "tools": [{"type": "web_search"}],
            "reasoning": {"effort": _REASONING_EFFORT},
        }
        headers = {
            "Authorization": f"Bearer {self._credential}",
            "Content-Type": "application/json",
        }
        response = await self._http.post(_ENDPOINT, headers=headers, json=body)
        payload = response.json()
        if not isinstance(payload, dict):
            raise EngineBlockedError(
                self.engine_id, "Responses API returned a non-object body"
            )
        self._guard_soft_failure(payload)

        collector = _collect(payload)
        answer = _answer_text(payload)
        if not collector.urls:
            self._raise_no_citations(answer)

        fallback = answer[:_FALLBACK_SNIPPET_CHARS]
        return [
            EngineHit(
                title=collector.titles[rank],
                url=url,
                snippet=collector.texts[rank] or fallback,
                rank=rank,
            )
            for rank, url in enumerate(collector.urls[:wanted])
        ]

    def _guard_soft_failure(self, payload: dict[str, Any]) -> None:
        """Reject an upstream refusal that arrived as HTTP 200.

        The aggregator reads ``[]`` as "legitimately found nothing" and leaves the
        engine's health score alone, so a refusal must never reach the mapping
        step. xAI signals overload/throttling in an ``error`` field with a 200 on
        the wire more often than with a 429.
        """
        error = _error_text(payload)
        if not error:
            return
        if any(marker in error for marker in _OVERLOADED_MARKERS):
            raise EngineBlockedError(
                self.engine_id, f"Responses API reported overload: {error}"
            )
        raise EngineBlockedError(self.engine_id, f"Responses API returned an error: {error}")

    def _raise_no_citations(self, answer: str) -> None:
        """No citations: distinguish "answered but uncitable" from "not a result"."""
        if answer:
            raise EngineError(
                self.engine_id,
                "Grok returned an answer but no citable sources; the aggregator "
                "merges hits, not prose, so there is nothing to contribute",
            )
        raise EngineBlockedError(
            self.engine_id,
            "Responses API returned neither an answer nor citations; treating the "
            "empty body as an upstream refusal rather than a zero-result search",
        )
