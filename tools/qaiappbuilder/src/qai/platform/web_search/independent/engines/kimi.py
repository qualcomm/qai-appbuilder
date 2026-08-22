# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Kimi Code search engine (keyed HTTP API).

Posts to the Kimi Code search API (``POST https://api.kimi.com/coding/v1/search``)
with a bearer token and maps its ``search_results`` onto :class:`EngineHit`.

.. important::

   **Which credential to provision.** This endpoint belongs to the *Kimi Code*
   membership service (``api.kimi.com``), which has a credential system
   entirely separate from the *Moonshot Open Platform* (``api.moonshot.ai``).
   A plain ``MOONSHOT_API_KEY`` is **NOT** accepted here — it 401s. Provision
   this engine's credential from the **Kimi Code Console**, i.e. the value the reference
   implementation
   reads from ``KIMI_SEARCH_API_KEY`` / ``MOONSHOT_SEARCH_API_KEY``.

Recency is not supported by this endpoint: the request body has no time-window
field (the reference implementation sends only ``text_query`` / ``limit`` / ``enable_page_crawling``
/
``timeout_seconds``), so ``EngineQuery.recency`` is ignored silently rather
than faked client-side.

Because the upstream can answer HTTP 200 with an error envelope instead of
results, :meth:`KimiEngine.search` validates that ``search_results`` is really
a list before mapping; a refusal body raises :class:`EngineBlockedError` so the
aggregator penalizes the engine instead of reading it as "found nothing".
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

__all__ = ["KimiEngine"]

_ENGINE_ID = "kimi"
_ENDPOINT = "https://api.kimi.com/coding/v1/search"

# reference: DEFAULT_NUM_RESULTS = 10, MAX_NUM_RESULTS = 20, clamped through
# ``clampNumResults`` (min 1).
_DEFAULT_COUNT = 10
_MAX_COUNT = 20

# the reference implementation sends this as ``timeout_seconds`` in the body — an upstream-side
# budget,
# independent of our own transport deadline in ``HttpClient``.
_UPSTREAM_TIMEOUT_SECONDS = 30

# the reference implementation passes ``include_content: false`` from the provider entry point, so
# page
# crawling stays off: it multiplies latency for content we do not surface.
_ENABLE_PAGE_CRAWLING = False


def _clamp_count(count: int) -> int:
    """Mirror the reference implementation's ``clampNumResults(count, 10, 20)``."""
    if count <= 0:
        return _DEFAULT_COUNT
    return min(_MAX_COUNT, max(1, count))


def _trimmed(value: Any) -> str:
    """Return ``value`` trimmed when it is a non-blank string, else ``""``."""
    if not isinstance(value, str):
        return ""
    return value.strip()


class KimiEngine:
    """Kimi Code web search over its keyed REST API.

    Requires a Kimi Code Console credential (see the module docstring); a
    Moonshot Open Platform key will be rejected upstream as 401 and surface as
    :class:`EngineAuthError`.
    """

    engine_id: str = _ENGINE_ID
    engine_type: EngineType = "http_api"

    __slots__ = ("_credential", "_http")

    def __init__(
        self,
        *,
        credential: str | None = None,
        http_client: HttpClient | None = None,
    ) -> None:
        if credential is None:
            raise EngineAuthError(
                _ENGINE_ID,
                "kimi requires a Kimi Code Console credential "
                "(KIMI_SEARCH_API_KEY / MOONSHOT_SEARCH_API_KEY); "
                "a plain MOONSHOT_API_KEY is not accepted by api.kimi.com",
            )
        self._credential = credential
        self._http = http_client if http_client is not None else HttpClient(_ENGINE_ID)

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        limit = _clamp_count(query.count)
        # ``recency`` is intentionally unused: the Kimi Code search body has no
        # time-window field (see module docstring).
        body: dict[str, Any] = {
            "text_query": query.query,
            "limit": limit,
            "enable_page_crawling": _ENABLE_PAGE_CRAWLING,
            "timeout_seconds": _UPSTREAM_TIMEOUT_SECONDS,
        }
        headers = {
            "Authorization": f"Bearer {self._credential}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        response = await self._http.post(_ENDPOINT, headers=headers, json=body)
        results = self._results_or_raise(response.json())
        hits: list[EngineHit] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = _trimmed(item.get("url"))
            if not url:
                continue
            # reference: snippet falls back to the crawled ``content`` field, and the
            # title falls back to the URL so a hit is never anonymous.
            snippet = _trimmed(item.get("snippet")) or _trimmed(item.get("content"))
            hits.append(
                EngineHit(
                    title=_trimmed(item.get("title")) or url,
                    url=url,
                    snippet=snippet,
                    rank=len(hits),
                )
            )
            if len(hits) == limit:
                break
        return hits

    @staticmethod
    def _results_or_raise(payload: Any) -> list[Any]:
        """Extract ``search_results``, refusing anything that is not results.

        A 200 response whose body is an error envelope (or is not even an
        object) is a refusal, not an empty result set: returning ``[]`` would
        tell the aggregator the engine legitimately found nothing and leave it
        unpenalized. Raise instead.
        """
        if not isinstance(payload, dict):
            raise EngineError(_ENGINE_ID, "kimi returned a non-object body")

        results = payload.get("search_results")
        if isinstance(results, list):
            return results

        # No usable results key — surface whatever the upstream said instead of
        # silently degrading to zero hits.
        message = (
            _trimmed(payload.get("message"))
            or _trimmed(payload.get("error_message"))
            or _trimmed(payload.get("msg"))
        )
        error = payload.get("error")
        if not message and isinstance(error, dict):
            message = _trimmed(error.get("message"))
        elif not message and isinstance(error, str):
            message = _trimmed(error)
        raise EngineBlockedError(
            _ENGINE_ID,
            f"kimi refused the request: {message}"
            if message
            else "kimi returned HTTP 200 without a search_results list",
        )
