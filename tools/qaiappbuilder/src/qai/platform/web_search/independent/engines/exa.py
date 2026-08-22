# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Exa engine (keyed REST API with a keyless MCP fallback).

Exa is the only engine here that works both ways: with a credential it posts to
the ``/search`` REST API, and without one it calls Exa's public MCP endpoint
(``mcp.exa.ai``) over JSON-RPC. The fallback is why this engine is registered
``requires_credential = false`` — dropping it would lose a working keyless
source, so the constructor must not reject a missing credential the way the
purely keyed engines do.

The MCP transport is deliberately awkward: it answers ``tools/call`` with either
Server-Sent Events or a plain JSON body, and the payload carrying the results
may be a structured object *or* a block of human-readable text under
``content[].text``. Both shapes are handled, matching the reference implementation — a
structured probe first, then the labelled-text parser.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from qai.platform.web_search.independent.engines.base import (
    EngineHit,
    EngineQuery,
    EngineType,
)
from qai.platform.web_search.independent.errors import EngineError
from qai.platform.web_search.independent.http_client import HttpClient

__all__ = ["ExaEngine"]

_ENGINE_ID = "exa"
_API_URL = "https://api.exa.ai/search"
_MCP_URL = "https://mcp.exa.ai/mcp"

#: MCP tool exposed by Exa's public endpoint.
_MCP_TOOL = "web_search_exa"

#: Default search mode. the reference implementation normalizes an unset type to ``auto`` and maps
#: the
#: legacy ``keyword`` alias onto ``fast``; we only ever send ``auto``.
_SEARCH_TYPE = "auto"


class ExaEngine:
    """Exa search over its REST API, falling back to the public MCP endpoint."""

    engine_id: str = _ENGINE_ID
    engine_type: EngineType = "http_api"

    __slots__ = ("_credential", "_http")

    def __init__(
        self,
        *,
        credential: str | None = None,
        http_client: HttpClient | None = None,
    ) -> None:
        # No EngineAuthError on a missing credential: the MCP fallback is a
        # first-class path, not a degraded one.
        self._credential = credential
        self._http = http_client if http_client is not None else HttpClient(_ENGINE_ID)

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        credential = query.credential or self._credential
        if credential:
            results = await self._search_api(query, credential)
        else:
            results = await self._search_mcp(query)
        return self._to_hits(results, query.count)

    # ---- keyed REST path -------------------------------------------------

    async def _search_api(self, query: EngineQuery, credential: str) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "query": query.query,
            "numResults": query.count,
            "type": _SEARCH_TYPE,
            # Ask for per-result summaries; they become our snippet.
            "contents": {"summary": {"query": query.query}},
        }
        response = await self._http.post(
            _API_URL,
            headers={"Content-Type": "application/json", "x-api-key": credential},
            json=body,
        )
        payload = response.json()
        return self._results_of(payload)

    # ---- keyless MCP path ------------------------------------------------

    async def _search_mcp(self, query: EngineQuery) -> list[dict[str, Any]]:
        request = {
            "jsonrpc": "2.0",
            # Correlation id only; Exa echoes it back and we never match on it.
            "id": secrets.token_hex(8),
            "method": "tools/call",
            "params": {
                "name": _MCP_TOOL,
                "arguments": {"query": query.query, "num_results": query.count},
            },
        }
        response = await self._http.post(
            f"{_MCP_URL}?tools={_MCP_TOOL}",
            headers={
                "Content-Type": "application/json",
                # The endpoint may answer as SSE even for a unary call.
                "Accept": "application/json, text/event-stream",
            },
            json=request,
        )
        envelope = _parse_jsonrpc(response.text)
        if envelope is None:
            raise EngineError(_ENGINE_ID, "Exa MCP returned an unparsable response")
        error = envelope.get("error")
        if isinstance(error, dict):
            message = error.get("message") or "unknown error"
            # A JSON-RPC error rides on HTTP 200, so status alone cannot catch it.
            raise EngineError(_ENGINE_ID, f"Exa MCP error: {message}")

        result = envelope.get("result")
        structured = _normalize_mcp_payload(result)
        results = self._results_of(structured)
        if results:
            return results
        # No structured results: fall back to Exa's labelled-text rendering.
        return _parse_text_payload(result)

    # ---- shared mapping --------------------------------------------------

    @staticmethod
    def _results_of(payload: object) -> list[dict[str, Any]]:
        """Return the ``results`` array from an Exa payload, else empty."""
        if not isinstance(payload, dict):
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        return [item for item in results if isinstance(item, dict)]

    @staticmethod
    def _to_hits(results: list[dict[str, Any]], count: int) -> list[EngineHit]:
        hits: list[EngineHit] = []
        for item in results:
            if len(hits) >= count:
                break
            url = item.get("url") or ""
            if not url:
                continue
            highlights = item.get("highlights")
            joined = (
                " ".join(h for h in highlights if isinstance(h, str))
                if isinstance(highlights, list)
                else ""
            )
            snippet = item.get("summary") or item.get("text") or joined or ""
            hits.append(
                EngineHit(
                    title=item.get("title") or url,
                    url=url,
                    snippet=snippet,
                    rank=len(hits),
                )
            )
        return hits


def _parse_jsonrpc(text: str) -> dict[str, Any] | None:
    """Decode a JSON-RPC envelope from a plain JSON or SSE response body.

    Exa answers ``tools/call`` either as a JSON object or as an SSE stream whose
    ``data:`` lines carry the JSON. Trying plain JSON first keeps the common case
    cheap; the SSE scan then takes the last complete ``data:`` payload, which is
    the final (and for a unary call, only) message.
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        decoded = json.loads(stripped)
    except ValueError:
        decoded = None
    if isinstance(decoded, dict):
        return decoded

    envelope: dict[str, Any] | None = None
    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:") :].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            candidate = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            envelope = candidate
    return envelope


def _normalize_mcp_payload(result: object) -> object:
    """Find the results-bearing object inside an MCP ``result``.

    MCP wraps tool output inconsistently: the payload may sit at
    ``structuredContent``, ``data``, ``result``, the root itself, or JSON encoded
    inside a ``content[].text`` block. Probe each in that order — the same order
    the reference implementation uses — and return the first that carries ``results``.
    """
    if not isinstance(result, dict):
        return result

    candidates: list[object] = []
    for key in ("structuredContent", "data", "result"):
        if key in result:
            candidates.append(result[key])
    candidates.append(result)

    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                candidates.append(json.loads(text))
            except ValueError:
                continue

    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get("results"), list):
            return candidate
    return result


def _parse_text_payload(result: object) -> list[dict[str, Any]]:
    """Parse Exa's labelled-text rendering into result dicts.

    The public MCP endpoint does not return structured ``results``; it renders
    each hit as a labelled block::

        Title: Something
        URL: https://example.com
        Published: 2023-11-28T19:25:47.000Z
        Author: N/A
        Highlights:
        <the excerpt that becomes our snippet>
        ...

    Body text arrives under ``Highlights:`` (occasionally ``Text:`` on the keyed
    API's rendering), and the date label is ``Published``, not ``Published
    Date``. Reading only ``Text:``/``Published Date:`` silently yielded empty
    snippets for every keyless hit — the caller then had nothing but a title and
    had to fetch each page itself.

    Blocks are separated by a blank line before the next ``Title:``. A block is
    kept when it yields at least a title, a URL, or body text.
    """
    if not isinstance(result, dict):
        return []
    content = result.get("content")
    if not isinstance(content, list):
        return []

    blocks: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            blocks.append(text.replace("\r\n", "\n").replace("\r", "\n").strip())
    if not blocks:
        return []

    results: list[dict[str, Any]] = []
    for section in _split_sections("\n\n".join(blocks)):
        title = _labelled_field(section, "Title")
        url = _labelled_field(section, "URL")
        body = _block_body(section)
        if not (title or url or body):
            continue
        author = _labelled_field(section, "Author")
        results.append(
            {
                "title": title,
                "url": url,
                # Exa writes a literal "N/A" when it has no author.
                "author": "" if author == "N/A" else author,
                "publishedDate": _labelled_field(section, "Published")
                or _labelled_field(section, "Published Date"),
                "text": body,
            }
        )
    return results


def _split_sections(text: str) -> list[str]:
    """Split labelled text into per-result sections starting at ``Title:``."""
    sections: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.startswith("Title:") and current:
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())
    return [s for s in sections if s.startswith("Title:")]


def _labelled_field(section: str, label: str) -> str:
    """Return the value of a ``<label>: value`` line, or ``""``."""
    prefix = f"{label}:"
    for line in section.split("\n"):
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


#: Labels that introduce the block's body text, most-specific first. The public
#: MCP endpoint uses ``Highlights``; the keyed API's rendering uses ``Text``.
_BODY_LABELS = ("Highlights", "Text")

#: Cap on the extracted body. Exa's ``Highlights`` block can carry a page's
#: whole README; a snippet that long crowds out other engines' hits once the
#: aggregator merges them, and the caller can always fetch the URL for more.
_MAX_BODY_CHARS = 600


def _block_body(section: str) -> str:
    """Return the block's body text, truncated to :data:`_MAX_BODY_CHARS`.

    The body is everything following a body label to the end of the block, so it
    may span many lines. The label appearing EARLIEST wins, so a block carrying
    both ``Highlights`` and ``Text`` is read from whichever came first rather
    than from whichever :data:`_BODY_LABELS` happens to list first.
    """
    best = -1
    body_start = 0
    for label in _BODY_LABELS:
        # A label sits on its own line, so it is either at the block start or
        # preceded by a newline; matching bare "Label:" would also hit
        # "Published Date:" style suffixes inside other values.
        if section.startswith(f"{label}:"):
            index, offset = 0, len(label) + 1
        else:
            index = section.find(f"\n{label}:")
            offset = len(label) + 2
        if index >= 0 and (best < 0 or index < best):
            best, body_start = index, index + offset
    if best < 0:
        return ""
    body = section[body_start:].strip()
    if len(body) <= _MAX_BODY_CHARS:
        return body
    return body[:_MAX_BODY_CHARS].rstrip() + "…"
