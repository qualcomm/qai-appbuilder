# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Synthetic engine (keyed HTTP API).

Synthetic exposes a zero-data-retention web search API aimed at coding agents.

Wire format (mirrors the reference implementation's ``providers/synthetic.ts``):

* ``POST https://api.synthetic.new/v2/search``
* headers ``Authorization: Bearer <key>``, ``Content-Type: application/json``
* body ``{"query": <query>}`` — the query is the only field the reference implementation sends.
* response ``{"results": [{url, title, text, published}]}``; ``text`` becomes
  our ``snippet`` and ``title`` falls back to the URL. Results without a ``url``
  are dropped, as upstream does.

The API takes neither a result count nor a freshness parameter, so the count is
enforced client-side. the reference implementation applies **no** ``clampNumResults`` here: it
slices the
mapped sources only when the requested count is truthy, so a non-positive count
returns every mapped result. ``recency`` is **ignored silently** — the reference implementation
sends no
freshness field for this provider, and dropping the filter is strictly better
than dropping the engine or mutating the query text to approximate one.
"""

from __future__ import annotations

import json
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

__all__ = ["SyntheticEngine"]

_ENDPOINT = "https://api.synthetic.new/v2/search"


def _limit(count: int) -> int | None:
    """Mirror the reference implementation's ``numResults ? sources.slice(0, numResults) :
    sources``.

    the reference implementation applies no ``clampNumResults`` here — it slices only when the
    requested
    count is truthy, so a non-positive count means "return everything".
    """
    return count if count > 0 else None


class SyntheticEngine:
    """Synthetic web search over its keyed REST API."""

    engine_id: str = "synthetic"
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
        headers = {
            "Authorization": f"Bearer {self._credential}",
            "Content-Type": "application/json",
        }
        response = await self._http.post(
            _ENDPOINT, headers=headers, json={"query": query.query}
        )
        results = self._results(response.text)
        limit = _limit(query.count)
        hits: list[EngineHit] = []
        for item in results:
            if limit is not None and len(hits) >= limit:
                break
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = item.get("title")
            text = item.get("text")
            hits.append(
                EngineHit(
                    title=title if isinstance(title, str) and title else url,
                    url=url,
                    snippet=text if isinstance(text, str) else "",
                    rank=len(hits),
                )
            )
        return hits

    def _results(self, text: str) -> list[Any]:
        """Return the ``results`` list, or raise when the body is not results.

        A 200 whose body is not a JSON object carrying a ``results`` list is an
        upstream refusal / error envelope, not an empty result set. Mapping it
        to ``[]`` would tell the aggregator the engine legitimately found
        nothing and leave it unpenalized, so raise
        :class:`EngineBlockedError` instead.
        """
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise EngineBlockedError(
                self.engine_id, "Synthetic returned a non-JSON body"
            ) from exc
        if not isinstance(payload, dict):
            raise EngineBlockedError(
                self.engine_id, "Synthetic returned a non-object payload"
            )
        results = payload.get("results")
        if not isinstance(results, list):
            raise EngineBlockedError(
                self.engine_id,
                "Synthetic response carried no results list "
                f"(keys: {sorted(payload)[:5]})",
            )
        return results
