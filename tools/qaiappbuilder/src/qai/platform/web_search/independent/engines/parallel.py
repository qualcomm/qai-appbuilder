# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Parallel AI search engine (keyed HTTP API).

Posts to Parallel's beta search endpoint with an ``x-api-key`` credential and
the ``parallel-beta`` opt-in header, then maps the returned ``results`` onto
:class:`EngineHit`.

Wire format (mirrors the reference implementation's ``providers/parallel.ts`` +
``web/parallel.ts``):

* ``POST https://api.parallel.ai/v1beta/search``
* headers ``x-api-key``, ``parallel-beta: search-extract-2025-10-10``
* body ``{objective, search_queries, mode: "fast", excerpts: {...}}`` — the
  caller's query doubles as the natural-language ``objective`` and as the single
  entry of the ``search_queries`` list, exactly as the reference implementation does.
* each result carries an ``excerpts`` *list*; the excerpts are joined with a
  blank line into our single ``snippet`` string. ``title`` falls back to the
  URL when absent. Url-less entries are dropped, as upstream does.

Parallel exposes no result-count or freshness parameter, so the count is
enforced client-side: it is clamped with the reference implementation's ``clampNumResults(n, 10,
40)``
(a non-positive request becomes ``10``) and the mapped hits are cut to it.
``recency`` is **ignored silently** — the reference implementation sends no freshness field for this
provider, and dropping the filter is strictly better than dropping the engine
or mutating the query text to approximate one.
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

__all__ = ["ParallelEngine"]

_ENDPOINT = "https://api.parallel.ai/v1beta/search"
_BETA_HEADER = "search-extract-2025-10-10"
_MODE = "fast"
_MAX_CHARS_PER_RESULT = 10_000
# the reference implementation clamps the requested count with ``clampNumResults(n, 10, 40)`` before
# slicing the mapped sources; Parallel itself takes no count parameter, so the
# clamp is purely client-side.
_DEFAULT_COUNT = 10
_MAX_COUNT = 40
_EXCERPT_SEPARATOR = "\n\n"


def _clamp_count(count: int) -> int:
    """Mirror the reference implementation's ``clampNumResults(count, 10, 40)``."""
    if count <= 0:
        return _DEFAULT_COUNT
    return min(_MAX_COUNT, max(1, count))


def _snippet(item: dict[str, Any]) -> str:
    """Join a result's ``excerpts`` list into one snippet string.

    the reference implementation filters the list by *type* only (``getStringArray``) and joins with
    a
    blank line, so an empty excerpt is preserved as a blank paragraph rather
    than dropped; this mirrors that byte for byte.
    """
    raw = item.get("excerpts")
    if not isinstance(raw, list):
        return ""
    return _EXCERPT_SEPARATOR.join(text for text in raw if isinstance(text, str))


class ParallelEngine:
    """Parallel AI search over its keyed beta REST API."""

    engine_id: str = "parallel"
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
        body: dict[str, Any] = {
            "objective": query.query,
            "search_queries": [query.query],
            "mode": _MODE,
            "excerpts": {"max_chars_per_result": _MAX_CHARS_PER_RESULT},
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": self._credential,
            "parallel-beta": _BETA_HEADER,
        }
        response = await self._http.post(_ENDPOINT, headers=headers, json=body)
        results = self._results(response.text)
        limit = _clamp_count(query.count)
        hits: list[EngineHit] = []
        for item in results:
            if len(hits) >= limit:
                break
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            # the reference implementation reads the title through ``getString`` — a non-string
            # title is
            # discarded rather than coerced, so the URL fallback applies.
            title = item.get("title")
            hits.append(
                EngineHit(
                    title=title if isinstance(title, str) and title else url,
                    url=url,
                    snippet=_snippet(item),
                    rank=len(hits),
                )
            )
        return hits

    def _results(self, text: str) -> list[Any]:
        """Return the ``results`` list, or raise when the body is not results.

        A 200 that is not a JSON object carrying a ``results`` list is an
        upstream refusal / error envelope (Parallel answers quota and abuse
        rejections with a ``detail`` body). Returning ``[]`` there would tell
        the aggregator "legitimately found nothing" and leave the engine
        unpenalized, so it raises :class:`EngineBlockedError` instead.
        """
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise EngineBlockedError(
                self.engine_id, "Parallel returned a non-JSON body"
            ) from exc
        if not isinstance(payload, dict):
            raise EngineBlockedError(
                self.engine_id, "Parallel returned a non-object payload"
            )
        results = payload.get("results")
        if not isinstance(results, list):
            raise EngineBlockedError(
                self.engine_id,
                "Parallel response carried no results list "
                f"(keys: {sorted(payload)[:5]})",
            )
        return results
