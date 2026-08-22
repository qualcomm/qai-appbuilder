# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""TinyFish engine (keyed HTTP API).

Calls the TinyFish search API with an ``X-API-Key`` header and maps its
``results`` onto :class:`EngineHit`. Recency is converted to TinyFish's
``recency_minutes`` window.

Wire format (mirrors the reference implementation's ``providers/tinyfish.ts``):

* ``GET https://api.search.tinyfish.ai`` with query params ``query``,
  ``num_results``, ``page`` and (only when recency is set) ``recency_minutes``.
* header ``X-API-Key: <key>`` (**not** a bearer token) plus
  ``Accept: application/json``.
* the requested count is clamped to ``[1, 20]`` with ``10`` substituted for a
  non-positive request, matching the reference implementation's ``clampNumResults(n, 10, 20)``.
* TinyFish pages at ten results per request, so ``num_results`` carries the
  *page* size ``min(wanted, 10)`` and pages are fetched from ``page=0``
  upwards. The loop stops on the first of: the requested count collected, a
  page shorter than the page size (end of the result set), or ``page`` passing
  the inclusive cap of ``10``.
* ``title`` falls back to ``site_name`` and then to the URL; ``snippet`` maps
  straight across. Url-less entries are dropped, as upstream does.

the reference implementation additionally records ``site_name`` as the source's ``author``;
:class:`EngineHit` carries no author field, so that value only survives as the
title fallback.

A refusal that arrives as HTTP 200 (any body without a ``results`` list) raises
:class:`EngineBlockedError`: parsing it to an empty list would tell the
aggregator "legitimately found nothing", leaving the engine unpenalized.
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

__all__ = ["TinyfishEngine"]

_ENDPOINT = "https://api.search.tinyfish.ai"
# reference: DEFAULT_NUM_RESULTS = 10 doubles as the page size, MAX_NUM_RESULTS = 20,
# MAX_PAGE = 10 (an inclusive bound, so at most eleven requests).
_DEFAULT_COUNT = 10
_PAGE_SIZE = 10
_MAX_COUNT = 20
_MAX_PAGE = 10

# TinyFish expresses recency as a lookback window in minutes.
_RECENCY_MINUTES = {"day": 1440, "week": 10080, "month": 43200, "year": 525600}


def _clamp_count(count: int) -> int:
    """Mirror the reference implementation's ``clampNumResults(count, 10, 20)``."""
    if count <= 0:
        return _DEFAULT_COUNT
    return min(_MAX_COUNT, max(1, count))


class TinyfishEngine:
    """TinyFish search over its keyed REST API."""

    engine_id: str = "tinyfish"
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
        page_size = min(wanted, _PAGE_SIZE)
        headers = {"Accept": "application/json", "X-API-Key": self._credential}
        params: dict[str, Any] = {"query": query.query, "num_results": page_size}
        if query.recency is not None:
            params["recency_minutes"] = _RECENCY_MINUTES[query.recency]

        hits: list[EngineHit] = []
        for page in range(_MAX_PAGE + 1):
            if len(hits) >= wanted:
                break
            response = await self._http.get(
                _ENDPOINT, headers=headers, params={**params, "page": page}
            )
            results = self._results(response.json())
            self._append(hits, results, wanted)
            if len(results) < page_size:
                break
        return hits

    @staticmethod
    def _append(hits: list[EngineHit], results: list[Any], wanted: int) -> None:
        for item in results:
            if len(hits) >= wanted:
                return
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            if not url:
                continue
            site_name = item.get("site_name") or ""
            hits.append(
                EngineHit(
                    title=item.get("title") or site_name or url,
                    url=url,
                    snippet=item.get("snippet") or "",
                    rank=len(hits),
                )
            )

    def _results(self, payload: Any) -> list[Any]:
        """Return ``results`` after validating the envelope.

        Raises :class:`EngineBlockedError` for any 200 body that is not a real
        result envelope, so an upstream refusal is never mistaken for an empty
        but legitimate result set.
        """
        if not isinstance(payload, dict):
            raise EngineBlockedError(
                self.engine_id, "TinyFish returned a non-object body"
            )
        results = payload.get("results")
        if not isinstance(results, list):
            detail = payload.get("error") or payload.get("message") or "unspecified"
            raise EngineBlockedError(
                self.engine_id, f"TinyFish refused the search: {detail}"
            )
        return results
