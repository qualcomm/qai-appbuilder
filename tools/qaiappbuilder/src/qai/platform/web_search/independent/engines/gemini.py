# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Gemini grounding engine (keyed HTTP API).

Uses the Google AI Studio ``generateContent`` endpoint with the built-in
``googleSearch`` tool. The grounded sources returned in
``groundingMetadata.groundingChunks`` become :class:`EngineHit`; each hit's
snippet is the grounded text segment that cites it, falling back to the leading
slice of the generated answer when no segment maps to the chunk.

This engine is LLM-mediated: the model decides whether to search at all, so a
200 that carries no grounded sources is an upstream refusal (safety block,
recitation stop, or the model simply answering from parametric memory) rather
than a zero-result search. Returning ``[]`` there would tell the aggregator
"legitimately found nothing" and leave the engine unpenalized and permanently in
rotation, so those bodies raise -- :class:`EngineError` when the model answered
but produced nothing citable, :class:`EngineBlockedError` when it produced
neither. This mirrors :mod:`~.xai` and :mod:`~.anthropic`, the other two
LLM-mediated engines.

Recency has no parameter on this API -- Google Search grounding exposes no
freshness knob -- so ``recency`` is **ignored silently**, exactly as the reference implementation
does
(the reference's gemini.ts never reads it). Rewriting the query text to fake a date filter
would corrupt the search terms for every engine-level consumer.
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

__all__ = ["GeminiEngine"]

#: the reference implementation's ``DEFAULT_MODEL`` for search grounding (the reference's
#: gemini.ts:31).
_MODEL = "gemini-2.5-flash"
_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent"
)
_FALLBACK_SNIPPET_CHARS = 200

#: ``finishReason`` values that mean the model was cut off rather than done.
#: They arrive on a 200 with no usable grounding, so they must not read as a
#: zero-result search.
_REFUSAL_FINISH_REASONS = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
)


def _answer_text(candidate: dict[str, Any]) -> str:
    content = candidate.get("content")
    parts = content.get("parts", []) if isinstance(content, dict) else []
    return "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _snippet_by_chunk(metadata: dict[str, Any], answer: str) -> dict[int, str]:
    """Map each grounding-chunk index to the answer segment that cites it.

    Mirrors the reference implementation's citation attribution (the reference's gemini.ts:237-253):
    every
    ``groundingSupports`` entry names the chunks its segment cites, and the reference implementation
    uses
    ``segment.text`` as the cited text. We additionally recover the segment from
    the answer's ``startIndex``/``endIndex`` when ``text`` is absent, because
    :class:`EngineHit` requires a snippet where the reference implementation's ``SearchSource`` does
    not.
    """
    snippets: dict[int, str] = {}
    for support in metadata.get("groundingSupports", []):
        if not isinstance(support, dict):
            continue
        segment = support.get("segment")
        if not isinstance(segment, dict):
            segment = {}
        start = segment.get("startIndex", 0)
        end = segment.get("endIndex", len(answer))
        text = segment.get("text") or answer[start:end]
        for chunk_index in support.get("groundingChunkIndices", []):
            if isinstance(chunk_index, int):
                snippets.setdefault(chunk_index, text)
    return snippets


class GeminiEngine:
    """Gemini grounding search over the AI Studio ``generateContent`` API."""

    engine_id: str = "gemini"
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
        body: dict[str, Any] = {
            # the reference implementation tags the turn with an explicit role (the reference's
            # gemini.ts:445-450).
            "contents": [{"role": "user", "parts": [{"text": query.query}]}],
            # the reference implementation's ``buildGeminiRequestTools`` emits the camelCase proto
            # field
            # ``googleSearch`` with an empty config object (the reference's gemini.ts:69).
            "tools": [{"googleSearch": {}}],
        }
        headers = {
            "x-goog-api-key": self._credential,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        response = await self._http.post(_ENDPOINT, headers=headers, json=body)
        candidate = self._candidate(response.json())
        answer = _answer_text(candidate)
        metadata = candidate.get("groundingMetadata")
        if not isinstance(metadata, dict):
            metadata = {}
        chunks = metadata.get("groundingChunks")
        if not isinstance(chunks, list) or not chunks:
            self._raise_no_sources(answer, candidate)

        fallback = answer[:_FALLBACK_SNIPPET_CHARS]
        snippet_by_chunk = _snippet_by_chunk(metadata, answer)
        hits: list[EngineHit] = []
        seen: set[str] = set()
        # ``chunk_index`` keys the grounding-support lookup and MUST stay the
        # chunk's own position -- that is the index ``groundingChunkIndices``
        # refers to, so it is the only key that attributes a cited segment to the
        # right source (same indexing the reference implementation uses at gemini.ts:243). ``rank``
        # is the
        # output position and must stay dense. Reusing one counter for both
        # leaves a gap in the ranks wherever a chunk is dropped, which the
        # aggregator's rank-fusion reads as a phantom result.
        for chunk_index, chunk in enumerate(chunks):
            if len(hits) >= query.count:
                break
            if not isinstance(chunk, dict):
                continue
            web = chunk.get("web")
            if not isinstance(web, dict):
                continue
            url = web.get("uri", "")
            if not url:
                continue
            # the reference implementation de-duplicates grounded sources by url (the reference's
            # gemini.ts:226-232);
            # the same page is commonly cited by several supports.
            if url in seen:
                continue
            seen.add(url)
            hits.append(
                EngineHit(
                    title=web.get("title") or url,
                    url=url,
                    snippet=snippet_by_chunk.get(chunk_index, fallback),
                    rank=len(hits),
                )
            )
        if not hits:
            self._raise_no_sources(answer, candidate)
        return hits

    def _candidate(self, payload: Any) -> dict[str, Any]:
        """Return the first candidate after validating the envelope.

        A body with no candidates is never a zero-result search: the Developer
        API drops candidates when the prompt itself is blocked (reporting it in
        ``promptFeedback``) or when the request failed soft on a 200.
        """
        if not isinstance(payload, dict):
            raise EngineBlockedError(
                self.engine_id, "Gemini returned a non-object body"
            )
        feedback = payload.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise EngineBlockedError(
                self.engine_id,
                f"Gemini blocked the prompt: {feedback['blockReason']}",
            )
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("status") or "unspecified"
            raise EngineError(self.engine_id, f"Gemini API error: {message}")
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise EngineBlockedError(
                self.engine_id,
                "Gemini returned no candidates; treating the empty body as an "
                "upstream refusal rather than a zero-result search",
            )
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise EngineBlockedError(
                self.engine_id, "Gemini returned a non-object candidate"
            )
        return candidate

    def _raise_no_sources(self, answer: str, candidate: dict[str, Any]) -> None:
        """No grounded sources: classify the refusal instead of returning ``[]``."""
        finish = candidate.get("finishReason")
        if isinstance(finish, str) and finish.upper() in _REFUSAL_FINISH_REASONS:
            raise EngineBlockedError(
                self.engine_id, f"Gemini stopped the response early: {finish}"
            )
        if answer:
            raise EngineError(
                self.engine_id,
                "Gemini answered without grounding the response in any web "
                "source; the aggregator merges hits, not prose, so there is "
                "nothing to contribute",
            )
        raise EngineBlockedError(
            self.engine_id,
            "Gemini returned neither an answer nor grounded sources; treating "
            "the empty body as an upstream refusal rather than a zero-result "
            "search",
        )
