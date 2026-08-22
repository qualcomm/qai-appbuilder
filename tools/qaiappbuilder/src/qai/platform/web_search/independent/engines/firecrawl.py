# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Firecrawl engine (HTTP API with a keyless fallback).

Posts to Firecrawl's ``/v2/search`` endpoint and maps ``data.web`` onto
:class:`EngineHit`. Recency maps onto Google's ``tbs`` (time-based search)
syntax, which Firecrawl forwards to its upstream SERP.

Unlike the other keyed engines here, Firecrawl accepts unauthenticated
searches: omitting the ``Authorization`` header runs the request in Firecrawl's
free keyless mode. A missing credential therefore takes that path instead of
raising :class:`EngineAuthError` -- the engine is usable either way, and the
loader is free to configure it without a secret.

A refusal that arrives as HTTP 200 (``success: false``, or any body without the
``data.web`` envelope) raises :class:`EngineBlockedError`: parsing it to an
empty list would tell the aggregator "legitimately found nothing" and leave the
engine unpenalized and permanently in rotation.

API docs: https://docs.firecrawl.dev/api-reference/endpoint/search
"""

from __future__ import annotations

from typing import Any

from qai.platform.web_search.independent.engines.base import (
    EngineHit,
    EngineQuery,
    EngineType,
)
from qai.platform.web_search.independent.errors import EngineBlockedError
from qai.platform.web_search.independent.http_client import HttpClient

__all__ = ["FirecrawlEngine"]

_ENDPOINT = "https://api.firecrawl.dev/v2/search"
_MAX_COUNT = 100  # Firecrawl caps ``limit`` at 100
_MIN_COUNT = 1

# Google ``tbs`` (time-based search) values mapped from our recency enum.
_TBS_BY_RECENCY = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}

# Only the web source is requested; Firecrawl can also return news/images, which
# this engine does not surface.
_SOURCES = [{"type": "web"}]


class FirecrawlEngine:
    """Firecrawl search over its REST API, keyed or keyless."""

    engine_id: str = "firecrawl"
    engine_type: EngineType = "http_api"

    __slots__ = ("_credential", "_http")

    def __init__(
        self,
        *,
        credential: str | None = None,
        http_client: HttpClient | None = None,
    ) -> None:
        # No credential check: ``None`` is a supported mode, not an error.
        self._credential = credential
        self._http = http_client if http_client is not None else HttpClient(self.engine_id)

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        limit = min(max(query.count, _MIN_COUNT), _MAX_COUNT)
        body: dict[str, Any] = {
            "query": query.query,
            "limit": limit,
            "sources": _SOURCES,
        }
        if query.recency is not None:
            body["tbs"] = _TBS_BY_RECENCY[query.recency]
        headers = {"Content-Type": "application/json"}
        if self._credential is not None:
            headers["Authorization"] = f"Bearer {self._credential}"
        response = await self._http.post(_ENDPOINT, headers=headers, json=body)
        return self._parse(response.json(), limit)

    def _parse(self, payload: Any, limit: int) -> list[EngineHit]:
        results = self._web_results(payload)
        hits: list[EngineHit] = []
        for item in results:
            if len(hits) >= limit:
                break
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            if not url:
                continue
            snippet = item.get("description") or item.get("markdown") or ""
            hits.append(
                EngineHit(
                    title=item.get("title") or url,
                    url=url,
                    snippet=snippet,
                    rank=len(hits),
                )
            )
        return hits

    def _web_results(self, payload: Any) -> list[Any]:
        """Return ``data.web`` after validating the envelope.

        Raises :class:`EngineBlockedError` for anything that is not a real
        result envelope -- Firecrawl reports quota refusals and keyless-mode
        rejections as HTTP 200 with ``success: false``.
        """
        if not isinstance(payload, dict):
            raise EngineBlockedError(
                self.engine_id, "Firecrawl returned a non-object body"
            )
        if payload.get("success") is False:
            detail = payload.get("error") or payload.get("warning") or "unspecified"
            raise EngineBlockedError(
                self.engine_id, f"Firecrawl refused the search: {detail}"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise EngineBlockedError(
                self.engine_id, "Firecrawl response has no 'data' envelope"
            )
        web = data.get("web")
        if not isinstance(web, list):
            raise EngineBlockedError(
                self.engine_id, "Firecrawl response has no 'data.web' result list"
            )
        return web
