# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Keenable engine (HTTP API with a keyless fallback).

Posts to Keenable Search and maps ``results`` onto :class:`EngineHit`.
Without a credential the public ``/v1/search/public`` endpoint is used
and the required ``X-Keenable-Title`` header is sent. With a credential
the keyed ``/v1/search`` endpoint is used instead.

The engine is off by default (``enabled_by_default = false`` in
``search_config.toml``). A 200 body that is not a ``results`` envelope
raises :class:`EngineBlockedError` rather than looking like a legitimate
empty result set.
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

__all__ = ["KeenableEngine"]

_PUBLIC_ENDPOINT = "https://api.keenable.ai/v1/search/public"
_KEYED_ENDPOINT = "https://api.keenable.ai/v1/search"
_APP_TITLE = "qai-appbuilder"
_DEFAULT_COUNT = 5
_MAX_COUNT = 20
_MIN_COUNT = 1


def _clamp_count(count: int) -> int:
    if count <= 0:
        return _DEFAULT_COUNT
    return min(_MAX_COUNT, max(_MIN_COUNT, count))


class KeenableEngine:
    """Keenable search over its public or keyed REST API."""

    engine_id: str = "keenable"
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
        wanted = _clamp_count(query.count)
        if self._credential:
            url = _KEYED_ENDPOINT
        else:
            url = _PUBLIC_ENDPOINT
        headers = {
            "Content-Type": "application/json",
            "X-Keenable-Title": _APP_TITLE,
        }
        if self._credential:
            headers["X-API-Key"] = self._credential
        body: dict[str, Any] = {"query": query.query, "max_results": wanted}
        response = await self._http.post(url, headers=headers, json=body)
        hits: list[EngineHit] = []
        for item in self._results(response.json()):
            if len(hits) >= wanted:
                break
            if not isinstance(item, dict):
                continue
            url_value = item.get("url") or item.get("link") or ""
            if not url_value:
                continue
            hits.append(
                EngineHit(
                    title=item.get("title") or url_value,
                    url=url_value,
                    snippet=item.get("snippet") or item.get("content") or item.get("description") or "",
                    rank=len(hits),
                )
            )
        return hits

    def _results(self, payload: Any) -> list[Any]:
        if not isinstance(payload, dict):
            raise EngineBlockedError(self.engine_id, "Keenable returned a non-object body")
        results = payload.get("results")
        if not isinstance(results, list):
            detail = payload.get("detail") or payload.get("error") or "unspecified"
            raise EngineBlockedError(
                self.engine_id, f"Keenable refused the search: {detail}"
            )
        return results
