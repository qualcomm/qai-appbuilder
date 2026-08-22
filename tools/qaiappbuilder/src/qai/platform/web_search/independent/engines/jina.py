# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Jina Reader engine (keyed HTTP API).

Calls the Jina Reader search endpoint, which takes the query in the **URL path**
(``GET https://s.jina.ai/<percent-encoded query>``) rather than as a query
parameter, and maps the ``data`` array onto :class:`EngineHit`. Each entry
carries ``title`` / ``url`` / ``content``; ``content`` is Reader-cleaned page
text, so it lands in ``snippet`` and is typically far longer than a SERP
snippet.

Headers reproduce the reference implementation exactly: ``Accept: application/json`` (Reader
defaults to
markdown -- without this the body is not JSON at all) plus
``Authorization: Bearer <key>``. the reference implementation sets no ``X-Respond-With`` or other
``X-*`` Reader control header on the search path, so neither does this engine;
adding one would change the response shape away from the ``data[]`` envelope
the mapping below depends on.

Recency is ignored, silently. the reference implementation's Jina search params carry no recency
field
and the endpoint exposes no date filter, so there is nothing to map it onto;
rewriting the query text to fake one would corrupt the search rather than
restrict it.

Jina answers a request without a usable key with HTTP 401, which the shared
:class:`HttpClient` maps to :class:`EngineAuthError`; 429 becomes
:class:`EngineBlockedError`. A body that is not a ``data[]`` envelope is an
upstream refusal, not a zero-result search -- parsing it to an empty list would
tell the aggregator "legitimately found nothing" and leave the engine
unpenalized and permanently in rotation -- so it raises
:class:`EngineBlockedError`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

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

__all__ = ["JinaEngine"]

_ENDPOINT = "https://s.jina.ai"


class JinaEngine:
    """Jina Reader search over its keyed REST API."""

    engine_id: str = "jina"
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
        # ``safe=""`` percent-encodes "/" and every other reserved character, so
        # a query containing a slash cannot escape into extra path segments.
        url = f"{_ENDPOINT}/{quote(query.query, safe='')}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._credential}",
        }
        response = await self._http.get(url, headers=headers)
        results = self._results(response.json())

        hits: list[EngineHit] = []
        for item in results:
            if len(hits) >= query.count:
                break
            if not isinstance(item, dict):
                continue
            url_value = item.get("url") or ""
            if not url_value:
                continue
            hits.append(
                EngineHit(
                    title=item.get("title") or url_value,
                    url=url_value,
                    snippet=item.get("content") or "",
                    rank=len(hits),
                )
            )
        return hits

    def _results(self, payload: Any) -> list[Any]:
        """Return ``data`` after validating the envelope.

        Raises :class:`EngineBlockedError` for any 200 body that is not a real
        result envelope, so an upstream refusal is never mistaken for an empty
        but legitimate result set.
        """
        if not isinstance(payload, dict):
            raise EngineBlockedError(self.engine_id, "Jina returned a non-object body")
        results = payload.get("data")
        if not isinstance(results, list):
            detail = (
                payload.get("message")
                or payload.get("detail")
                or payload.get("error")
                or "unspecified"
            )
            raise EngineBlockedError(
                self.engine_id, f"Jina refused the search: {detail}"
            )
        return results
