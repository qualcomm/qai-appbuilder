# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Tavily engine (keyed HTTP API).

Posts to the Tavily search API with a bearer token and maps its ``results``
onto :class:`EngineHit`. Recency values map directly onto Tavily's
``time_range``; the LLM ``answer`` field is not surfaced in this phase.

Like the reference implementation, a recency-filtered search that comes back with nothing renderable
is
retried once WITHOUT ``time_range``: the filter is a nice-to-have, and silently
returning zero hits because the window was too narrow is strictly worse than
returning slightly older ones. A 200 body that is not a ``results`` envelope at
all is an upstream refusal, not a zero-result search, so it raises
:class:`EngineBlockedError` rather than degrading to ``[]``.
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

__all__ = ["TavilyEngine"]

_ENDPOINT = "https://api.tavily.com/search"
_SEARCH_DEPTH = "basic"
#: the reference implementation clamps ``num_results`` into [1, 20], falling back to 5 when it is
#: absent/zero, before sending it as ``max_results`` (the reference's tavily.ts:16-17, 73).
_DEFAULT_COUNT = 5
_MAX_COUNT = 20
_MIN_COUNT = 1


def _clamp_count(count: int) -> int:
    """Mirror the reference implementation's ``clampNumResults(count, 5, 20)``."""
    if count <= 0:
        return _DEFAULT_COUNT
    return min(_MAX_COUNT, max(_MIN_COUNT, count))


class TavilyEngine:
    """Tavily search over its keyed REST API."""

    engine_id: str = "tavily"
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
        hits = await self._search_once(query.query, wanted, query.recency)
        if hits or query.recency is None:
            return hits
        # the reference implementation retries the unfiltered search when the recency-filtered one
        # has
        # nothing renderable (the reference's tavily.ts:174-178). ``time_range`` narrowing to
        # empty is common for technical queries, and older-but-present results
        # beat none.
        return await self._search_once(query.query, wanted, None)

    async def _search_once(
        self, text: str, wanted: int, recency: str | None
    ) -> list[EngineHit]:
        body: dict[str, Any] = {
            "query": text,
            "search_depth": _SEARCH_DEPTH,
            "max_results": wanted,
            # the reference implementation requests the "advanced" answer tier, not a bare boolean.
            "include_answer": "advanced",
            "include_raw_content": False,
        }
        if recency is not None:
            # ``topic`` and ``time_range`` are orthogonal upstream. Recency is a
            # pure temporal filter and must NOT also narrow the index to
            # news-only, which would break technical queries (release notes,
            # docs, GitHub) whenever a caller sets recency.
            body["time_range"] = recency
        headers = {
            "Authorization": f"Bearer {self._credential}",
            "Content-Type": "application/json",
        }
        response = await self._http.post(_ENDPOINT, headers=headers, json=body)
        hits: list[EngineHit] = []
        for item in self._results(response.json()):
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
                    snippet=item.get("content") or "",
                    # Dense rank over kept hits; a gap would read as a phantom
                    # result to the aggregator's rank-fusion.
                    rank=len(hits),
                )
            )
        return hits

    def _results(self, payload: Any) -> list[Any]:
        """Return the ``results`` list after validating the envelope.

        Raises :class:`EngineBlockedError` for any 200 body that is not a real
        result envelope, so an upstream refusal is never mistaken for an empty
        but legitimate result set (which the aggregator leaves unpenalized).
        """
        if not isinstance(payload, dict):
            raise EngineBlockedError(
                self.engine_id, "Tavily returned a non-object body"
            )
        results = payload.get("results")
        if not isinstance(results, list):
            detail = payload.get("detail") or payload.get("error") or "unspecified"
            raise EngineBlockedError(
                self.engine_id, f"Tavily refused the search: {detail}"
            )
        return results
