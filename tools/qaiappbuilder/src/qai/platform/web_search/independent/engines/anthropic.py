# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Anthropic (Claude) web-search engine — LLM-mediated, keyed HTTP API.

Claude performs the search itself: the request declares the native
``web_search_20250305`` server tool on the Messages API
(``POST /v1/messages?beta=true``, ``anthropic-version: 2023-06-01``,
``anthropic-beta: web-search-2025-03-05``), Anthropic runs the searches
server-side, and the response is a *block stream* mixing the model's prose with
the pages it consulted. Both beta markers are load-bearing — the reference implementation sets them
in
``buildAnthropicUrl`` / ``buildAnthropicSearchHeaders``, and without them the
server tool is not enabled. This engine reproduces the reference implementation's
``providers/anthropic.ts`` request and its content-block walk.

The block walk collects sources from two block types:

* ``web_search_tool_result`` — its ``content`` holds ``web_search_result``
  entries (title, url, ``page_age``). These are the pages Claude retrieved, in
  retrieval order, and they define the hit order.
* ``text`` — carries the prose plus ``web_search_result_location`` citations,
  whose ``cited_text`` is the specific passage Claude used. Those become the hit
  snippets, keyed by URL; a retrieved page with no citation falls back to
  ``page_age`` (the only other per-source metadata Anthropic returns) and then
  to the leading slice of the answer.

A cited URL that never appeared in a tool result is still appended, so a citation
can never be silently dropped.

Why an answer with zero sources is an *error*, not an empty list
---------------------------------------------------------------
Our :class:`Engine` protocol returns only hits — there is no answer field — so a
prose answer with nothing to cite is information the aggregator structurally
cannot carry. Returning ``[]`` would assert "this engine legitimately found
nothing", which is false: it would let a *successful* Claude call read as a null
result while leaving the engine's health score untouched, and the user would see
neither the answer nor an explanation. We raise :class:`EngineError` instead, so
the outcome is attributed to this engine with a message naming the cause.

Anthropic's own failure modes need care because **a failed search still returns
HTTP 200**: the error lands in the body, either as a top-level ``type:
"error"``/``error`` object or as a ``web_search_tool_result`` whose content is a
``web_search_tool_result_error``. Parsing either into ``[]`` would tell the
aggregator "nothing found" and spare a broken engine any penalty, so both raise
— :class:`EngineBlockedError` for overload / rate-limit / ``max_uses`` signals,
otherwise :class:`EngineError`.

Recency has no parameter on this tool (it exposes only ``max_uses``, domain
allow/block lists and ``user_location``), so ``recency`` is **ignored
silently**, exactly as the reference implementation does (the reference's anthropic.ts never reads
it). Rewriting
the query text to fake a date filter would corrupt the search terms Claude
actually searches for.

API docs: https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/web-search-tool
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

__all__ = ["AnthropicEngine"]

#: the reference implementation builds this via ``buildAnthropicUrl``, which appends ``?beta=true``
#: so
#: the gateway admits the beta server tool (the reference's anthropic-auth.ts:89-93).
_ENDPOINT = "https://api.anthropic.com/v1/messages?beta=true"

#: Pinned by the reference implementation's ``DEFAULT_MODEL`` — the cheap fast model is right for
#: search.
_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 4096
_API_VERSION = "2023-06-01"

#: the reference implementation's ``buildAnthropicSearchHeaders`` passes
#: ``extraBetas: ["web-search-2025-03-05"]``, which lands in ``anthropic-beta``
#: (the reference's anthropic-auth.ts:75-84, anthropic.ts:224-227, 310-318). Without it the
#: server tool is not enabled and the request is rejected outright.
_BETA = "web-search-2025-03-05"

_WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
_WEB_SEARCH_TOOL_NAME = "web_search"

_SYSTEM_PROMPT = (
    "You are a web search engine. Search the web for the user's query and "
    "answer it concisely, citing every source you used."
)

#: the reference implementation sends no result count upstream — the web_search tool has no such
#: field —
#: and slices the mapped sources to ``numSearchResults`` afterwards
#: (the reference's anthropic.ts:322-325). ``clampNumResults`` is not applied on this path,
#: so a zero/absent count means "no cap", matching the reference implementation's falsy check.
_DEFAULT_COUNT = 10

_FALLBACK_SNIPPET_CHARS = 200

_RESULT_BLOCK_TYPE = "web_search_tool_result"
_RESULT_ENTRY_TYPE = "web_search_result"
_RESULT_ERROR_TYPE = "web_search_tool_result_error"
_TEXT_BLOCK_TYPE = "text"
_CITATION_TYPE = "web_search_result_location"

#: Upstream soft-failure codes that mean "try again later", not "bad query".
#: ``max_uses_exceeded`` is Anthropic's per-request search budget running out —
#: a throttle, so it is a block rather than a generic failure.
_BLOCKED_MARKERS = (
    "overloaded",
    "rate_limit",
    "rate limit",
    "too_many_requests",
    "max_uses_exceeded",
    "unavailable",
)
_AUTH_MARKERS = ("authentication", "permission", "invalid_api_key", "invalid x-api-key")


def _cap(count: int) -> int:
    """Result cap: the reference implementation slices only when ``numSearchResults`` is truthy."""
    return count if count > 0 else _DEFAULT_COUNT


def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _error_text(value: Any) -> str:
    """Flatten an error payload (string or object) into lowercase text."""
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, dict):
        return " ".join(
            item.lower() for item in value.values() if isinstance(item, str)
        )
    return ""


class _SourceCollector:
    """Retrieved pages in retrieval order, plus citation text keyed by URL."""

    __slots__ = ("_seen", "answer_parts", "cited_text", "titles", "urls")

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.urls: list[str] = []
        self.titles: list[str] = []
        #: Per-URL snippet: the citation's ``cited_text`` when one exists, else
        #: the source's ``page_age``.
        self.cited_text: dict[str, str] = {}
        self.answer_parts: list[str] = []

    def add_source(self, url: Any, title: Any, page_age: Any) -> None:
        trimmed = self._register(url, title)
        if trimmed is None:
            return
        if isinstance(page_age, str) and page_age.strip():
            self.cited_text.setdefault(trimmed, page_age.strip())

    def add_citation(self, url: Any, title: Any, cited_text: Any) -> None:
        trimmed = self._register(url, title)
        if trimmed is None:
            return
        if isinstance(cited_text, str) and cited_text.strip():
            # A citation is more specific than ``page_age``, so it overwrites.
            self.cited_text[trimmed] = cited_text.strip()

    def _register(self, url: Any, title: Any) -> str | None:
        """Record a URL if new; return its trimmed form, or ``None`` if unusable."""
        if not isinstance(url, str):
            return None
        trimmed = url.strip()
        if not trimmed:
            return None
        if trimmed not in self._seen:
            self._seen.add(trimmed)
            self.urls.append(trimmed)
            self.titles.append(
                title.strip() if isinstance(title, str) and title.strip() else trimmed
            )
        return trimmed

    @property
    def answer(self) -> str:
        return "\n\n".join(self.answer_parts).strip()


class AnthropicEngine:
    """Claude web search over the Messages API's native ``web_search`` tool."""

    engine_id: str = "anthropic"
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
        cap = _cap(query.count)
        # ``recency`` is intentionally unused: the web_search tool exposes no
        # time-window parameter (see module docstring).
        body: dict[str, Any] = {
            "model": _MODEL,
            "max_tokens": _MAX_TOKENS,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": query.query}],
            "tools": [
                {
                    "type": _WEB_SEARCH_TOOL_TYPE,
                    "name": _WEB_SEARCH_TOOL_NAME,
                }
            ],
        }
        headers = {
            "x-api-key": self._credential,
            "anthropic-version": _API_VERSION,
            "anthropic-beta": _BETA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        response = await self._http.post(_ENDPOINT, headers=headers, json=body)
        payload = response.json()
        if not isinstance(payload, dict):
            raise EngineBlockedError(
                self.engine_id, "Messages API returned a non-object body"
            )
        self._guard_response_error(payload)

        collector = self._walk(payload)
        if not collector.urls:
            self._raise_no_sources(collector.answer)

        fallback = collector.answer[:_FALLBACK_SNIPPET_CHARS]
        return [
            EngineHit(
                title=collector.titles[rank],
                url=url,
                snippet=collector.cited_text.get(url) or fallback,
                rank=rank,
            )
            for rank, url in enumerate(collector.urls[:cap])
        ]

    def _walk(self, payload: dict[str, Any]) -> _SourceCollector:
        """Collect sources, citations and prose from the content blocks."""
        collector = _SourceCollector()
        for block in _blocks(payload):
            block_type = block.get("type")
            if block_type == _RESULT_BLOCK_TYPE:
                self._walk_tool_result(block, collector)
            elif block_type == _TEXT_BLOCK_TYPE:
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    collector.answer_parts.append(text.strip())
                for citation in block.get("citations") or ():
                    if not isinstance(citation, dict):
                        continue
                    if citation.get("type") != _CITATION_TYPE:
                        continue
                    collector.add_citation(
                        citation.get("url"),
                        citation.get("title"),
                        citation.get("cited_text"),
                    )
        return collector

    def _walk_tool_result(
        self, block: dict[str, Any], collector: _SourceCollector
    ) -> None:
        """Harvest one ``web_search_tool_result`` block, or raise on its error.

        A failed server-side search is reported *inside* a 200 response as a
        ``web_search_tool_result_error``. Skipping it would leave zero sources and
        surface as a spurious "no citable sources" error, so classify it here
        where the upstream code is still visible.
        """
        content = block.get("content")
        if isinstance(content, dict):
            if content.get("type") == _RESULT_ERROR_TYPE:
                self._raise_for_error(content.get("error_code"), "web search failed")
            return
        for entry in content or ():
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            if entry_type == _RESULT_ERROR_TYPE:
                self._raise_for_error(entry.get("error_code"), "web search failed")
            if entry_type != _RESULT_ENTRY_TYPE:
                continue
            collector.add_source(
                entry.get("url"), entry.get("title"), entry.get("page_age")
            )

    def _guard_response_error(self, payload: dict[str, Any]) -> None:
        """Reject a top-level upstream error that arrived with HTTP 200."""
        if payload.get("type") == "error":
            self._raise_for_error(payload.get("error"), "Messages API error")
        error = payload.get("error")
        if error:
            self._raise_for_error(error, "Messages API error")

    def _raise_for_error(self, error: Any, prefix: str) -> None:
        """Map an upstream error payload onto the engine error taxonomy."""
        text = _error_text(error) or "unspecified"
        if any(marker in text for marker in _AUTH_MARKERS):
            raise EngineAuthError(self.engine_id, f"{prefix}: {text}")
        if any(marker in text for marker in _BLOCKED_MARKERS):
            raise EngineBlockedError(self.engine_id, f"{prefix}: {text}")
        raise EngineError(self.engine_id, f"{prefix}: {text}")

    def _raise_no_sources(self, answer: str) -> None:
        """No sources: distinguish "answered but uncitable" from "not a result"."""
        if answer:
            raise EngineError(
                self.engine_id,
                "Claude returned an answer but no citable sources; the aggregator "
                "merges hits, not prose, so there is nothing to contribute",
            )
        raise EngineBlockedError(
            self.engine_id,
            "Messages API returned neither an answer nor sources; treating the "
            "empty body as an upstream refusal rather than a zero-result search",
        )
