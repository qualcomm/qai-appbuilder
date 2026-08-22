# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Brave Search engine (keyed HTTP API).

Calls the Brave Web Search REST API with a subscription token and maps its
``web.results`` onto :class:`EngineHit`. Recency is mapped onto Brave's
``freshness`` parameter; failures surface through the shared
:class:`HttpClient` error taxonomy.

A 200 body carrying no ``web`` bucket at all is an upstream refusal, not a
zero-result search: parsing it to an empty list would tell the aggregator
"legitimately found nothing" and leave the engine unpenalized and permanently
in rotation, so it raises :class:`EngineBlockedError`. A present-but-empty
``web`` bucket is a genuine zero-result search and still returns ``[]``.
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
)
from qai.platform.web_search.independent.http_client import HttpClient

__all__ = ["BraveEngine"]

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
#: the reference implementation clamps ``num_results`` into [1, 20] and falls back to 10 when it is
#: absent/zero before sending it as ``count`` (the reference's brave.ts:16-17, 75).
_DEFAULT_COUNT = 10
_MAX_COUNT = 20
_MIN_COUNT = 1
_FRESHNESS_BY_RECENCY = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}


def _clamp_count(count: int) -> int:
    """Mirror the reference implementation's ``clampNumResults(count, 10, 20)``."""
    if count <= 0:
        return _DEFAULT_COUNT
    return min(_MAX_COUNT, max(_MIN_COUNT, count))


class BraveEngine:
    """Brave Web Search over its keyed REST API."""

    engine_id: str = "brave"
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
        params: dict[str, Any] = {
            "q": query.query,
            "count": wanted,
            "extra_snippets": "true",
        }
        if query.recency is not None:
            params["freshness"] = _FRESHNESS_BY_RECENCY[query.recency]
        headers = {
            "X-Subscription-Token": self._credential,
            "Accept": "application/json",
        }
        response = await self._http.get(_ENDPOINT, headers=headers, params=params)
        results = self._results(response.json())
        hits: list[EngineHit] = []
        for item in results:
            if len(hits) >= wanted:
                break
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            if not url:
                continue
            hits.append(
                EngineHit(
                    title=item.get("title") or url,
                    url=url,
                    snippet=_build_snippet(item),
                    # Dense rank over kept hits: ``enumerate`` over the raw list
                    # leaves a gap wherever a url-less entry was dropped, and the
                    # aggregator's rank-fusion reads that gap as a phantom result.
                    rank=len(hits),
                )
            )
        return hits

    def _results(self, payload: Any) -> list[Any]:
        """Return ``web.results`` after validating the envelope.

        A missing ``web`` bucket means Brave did not answer the search at all
        (quota/plan refusals and upstream faults arrive this way on a 200), so it
        raises rather than degrading to ``[]`` -- the aggregator reads ``[]`` as
        "legitimately found nothing" and leaves the engine's health score alone.
        A present ``web`` bucket with a missing/empty ``results`` list IS a real
        zero-result search and maps to ``[]``.
        """
        if not isinstance(payload, dict):
            raise EngineBlockedError(self.engine_id, "Brave returned a non-object body")
        web = payload.get("web")
        if not isinstance(web, dict):
            detail = payload.get("error") or payload.get("message") or "unspecified"
            raise EngineBlockedError(
                self.engine_id, f"Brave response carried no 'web' bucket: {detail}"
            )
        results = web.get("results")
        return results if isinstance(results, list) else []


def _build_snippet(item: dict[str, Any]) -> str:
    """Join ``description`` with Brave's ``extra_snippets``, de-duplicated.

    The request asks for ``extra_snippets=true``, so dropping them discards data
    already paid for; Brave returns them as additional passages from the same
    page. Order and de-duplication follow the reference implementation.
    """
    parts: list[str] = []
    description = item.get("description")
    if isinstance(description, str) and description.strip():
        parts.append(description.strip())
    extra = item.get("extra_snippets")
    if isinstance(extra, list):
        for snippet in extra:
            if not isinstance(snippet, str):
                continue
            trimmed = snippet.strip()
            if trimmed and trimmed not in parts:
                parts.append(trimmed)
    return "\n".join(parts)
