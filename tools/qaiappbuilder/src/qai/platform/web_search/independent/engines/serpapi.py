# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""SerpApi engine (keyed HTTP API).

Calls the SerpApi Google Search REST API with a subscription key and maps its
``organic_results`` onto :class:`EngineHit`. Recency is mapped onto Google's
``tbs`` (time-based search) parameter; transport and HTTP-status failures
surface through the shared :class:`HttpClient` error taxonomy (``401``/``403``
for a rejected key, ``429`` for "run out of searches").

There is no the reference implementation counterpart for this engine, so its contract is anchored on
the
engine-layer invariants instead: dense 0-based ranks over *kept* hits, and a
non-result response must raise rather than return ``[]``.

That second invariant needs care here, because SerpApi overloads one field for
two opposite meanings. A top-level ``error`` string accompanies *both* a real
search failure *and* a search that simply found nothing ("Google hasn't
returned any results for this query."). ``search_metadata.status``
disambiguates: ``Success`` means SerpApi processed the search — empty results
included — so that body is a genuine zero-result SERP and maps to ``[]``. Any
other status (``Error``/``Processing``/``Queued``/absent) is an upstream
refusal; returning ``[]`` there would tell the aggregator the engine
legitimately found nothing, leaving it unpenalized and permanently in rotation,
so it raises :class:`EngineBlockedError`.

API docs: https://serpapi.com/search-api
       https://serpapi.com/api-status-and-error-codes
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

__all__ = ["SerpApiEngine"]

_ENDPOINT = "https://serpapi.com/search"
_MAX_COUNT = 100  # SerpApi supports ``num`` up to 100
_MIN_COUNT = 1

#: ``search_metadata.status`` value meaning "processed" — which per SerpApi's
#: docs explicitly includes a successfully-executed search with zero results.
_STATUS_SUCCESS = "Success"

# Google ``tbs`` (time-based search) values mapped from our recency enum.
_TBS_BY_RECENCY = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}


class SerpApiEngine:
    """SerpApi (Google Search) over its keyed REST API."""

    engine_id: str = "serpapi"
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
        wanted = min(max(query.count, _MIN_COUNT), _MAX_COUNT)
        params: dict[str, Any] = {
            "engine": "google",
            "api_key": self._credential,
            "q": query.query,
            "num": wanted,
            "output": "json",
            "hl": "en",
        }
        if query.recency is not None:
            params["tbs"] = _TBS_BY_RECENCY[query.recency]
        response = await self._http.get(_ENDPOINT, params=params)
        results = self._results(response.json())

        hits: list[EngineHit] = []
        for item in results:
            if len(hits) >= wanted:
                break
            if not isinstance(item, dict):
                continue
            url = item.get("link") or ""
            if not url:
                continue
            hits.append(
                EngineHit(
                    title=item.get("title") or url,
                    url=url,
                    snippet=item.get("snippet") or "",
                    # Dense rank over kept hits; a gap left by a dropped result
                    # reads as a phantom result to the aggregator's rank-fusion.
                    rank=len(hits),
                )
            )
        return hits

    def _results(self, payload: Any) -> list[Any]:
        """Return ``organic_results`` after validating the envelope.

        A 200 carrying a top-level ``error`` is ambiguous in this API: it marks
        both a failed search and a successful search with no matches. Only
        ``search_metadata.status == "Success"`` licenses the empty-result
        reading; anything else is an upstream refusal and must raise, so the
        aggregator penalizes the engine instead of recording "found nothing".
        """
        if not isinstance(payload, dict):
            raise EngineBlockedError(
                self.engine_id, "SerpApi returned a non-object body"
            )
        error = payload.get("error")
        metadata = payload.get("search_metadata")
        status = metadata.get("status") if isinstance(metadata, dict) else None
        if error and status != _STATUS_SUCCESS:
            raise EngineBlockedError(
                self.engine_id, f"SerpApi refused the search: {error}"
            )
        results = payload.get("organic_results")
        if results is None:
            # A processed search with zero organic hits omits the array entirely
            # (``organic_results_state: "Fully empty"``); that is a real empty
            # SERP, not a refusal, as long as the status agrees.
            if error or status == _STATUS_SUCCESS:
                return []
            raise EngineBlockedError(
                self.engine_id,
                "SerpApi response carried no organic_results "
                f"(keys: {sorted(payload)[:5]})",
            )
        if not isinstance(results, list):
            raise EngineBlockedError(
                self.engine_id, "SerpApi organic_results was not a list"
            )
        return results
