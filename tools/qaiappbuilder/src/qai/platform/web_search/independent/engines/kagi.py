# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Kagi engine (keyed HTTP API).

Calls the Kagi **V1** Search API (``POST /api/v1/search``, the public-preview
successor to the sunset V0 ``GET`` endpoint) with a ``Bearer`` token and maps
its categorized result buckets onto :class:`EngineHit`.

V1 returns ``data`` as an *object of named buckets* rather than the legacy flat
array. Buckets are emitted in the reference implementation's order -- ``search``, then ``video``,
``news`` and ``infobox`` -- with the same ``[Video]`` / ``[News]`` / ``[Info]``
title tags so a non-web result stays recognizable after the aggregator merges
engines. ``adjacent_question`` / ``related_search`` / ``direct_answer`` are
related-query and answer buckets, not results, and are skipped.

Recency maps onto ``filters.after`` as a ``YYYY-MM-DD`` UTC date computed by
:func:`_recency_to_date`, reproducing the reference implementation's ``recencyToDate`` arithmetic --
including its JavaScript ``Date`` setter semantics, where a day-of-month that
does not exist in the target month rolls *forward* (2025-03-31 minus one month
=> 2025-03-03, since Feb has 28 days) instead of clamping.

Balance/quota problems reach the aggregator as a failure by two routes:

* as an HTTP status -- ``401``/``402``/``403`` become :class:`EngineAuthError`
  and ``429`` becomes :class:`EngineBlockedError` via the shared
  :class:`HttpClient` taxonomy;
* as an ``error[]`` array on an HTTP **200**. That body is a refusal, not a
  zero-result search: parsing it to an empty list would tell the aggregator
  "legitimately found nothing" and leave an out-of-balance engine unpenalized
  and permanently in rotation. Credential/balance wording therefore raises
  :class:`EngineAuthError` (the 200-flavoured twin of the 402) and any other
  ``error[]`` raises :class:`EngineBlockedError`.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from qai.platform.web_search.independent.engines.base import (
    EngineHit,
    EngineQuery,
    EngineType,
    Recency,
)
from qai.platform.web_search.independent.errors import (
    EngineAuthError,
    EngineBlockedError,
)
from qai.platform.web_search.independent.http_client import HttpClient

__all__ = ["KagiEngine"]

_ENDPOINT = "https://kagi.com/api/v1/search"

#: the reference implementation clamps ``num_results`` into [1, 40] before sending it as ``limit``.
_MAX_COUNT = 40
_MIN_COUNT = 1

#: V1 workflow selecting web results (vs. ``images``/``videos``/``news``/...).
_WORKFLOW = "search"

#: Result buckets the reference implementation maps into sources, with the title tag it prefixes.
#: Order is significant: it is the rank order of the merged hit list.
_RESULT_BUCKETS: tuple[tuple[str, str], ...] = (
    ("search", ""),
    ("video", "[Video]"),
    ("news", "[News]"),
    ("infobox", "[Info]"),
)

#: Credential/balance wording in a 200-delivered ``error[]``. Mirrors the reference implementation's
#: ``CREDIT_BODY_PATTERN`` and adds Kagi's own "balance" phrasing.
_QUOTA_PATTERN = re.compile(
    r"credits?\s*(?:exhausted|exceeded)|quota|insufficient|balance|unauthorized",
    re.IGNORECASE,
)


class KagiEngine:
    """Kagi search over its keyed V1 REST API."""

    engine_id: str = "kagi"
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
        body: dict[str, Any] = {
            "query": query.query,
            "workflow": _WORKFLOW,
            "limit": wanted,
        }
        if query.recency is not None:
            body["filters"] = {"after": _recency_to_date(query.recency)}
        headers = {
            "Authorization": f"Bearer {self._credential}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        response = await self._http.post(_ENDPOINT, headers=headers, json=body)
        data = self._data(response.json())

        hits: list[EngineHit] = []
        for bucket, tag in _RESULT_BUCKETS:
            items = data.get(bucket)
            if not isinstance(items, list):
                continue
            for item in items:
                if len(hits) >= wanted:
                    return hits
                hit = self._to_hit(item, tag, len(hits))
                if hit is not None:
                    hits.append(hit)
        return hits

    @staticmethod
    def _to_hit(item: Any, tag: str, rank: int) -> EngineHit | None:
        """Map one bucket entry onto a hit, or ``None`` when it carries no URL."""
        if not isinstance(item, dict):
            return None
        url = item.get("url") or ""
        if not url:
            return None
        title = item.get("title") or url
        return EngineHit(
            title=f"{tag} {title}" if tag else title,
            url=url,
            snippet=item.get("snippet") or "",
            rank=rank,
        )

    def _data(self, payload: Any) -> dict[str, Any]:
        """Return the ``data`` bucket object after validating the envelope.

        Raises on any 200 body that is not a real result envelope so an upstream
        refusal is never mistaken for a legitimately empty result set:
        :class:`EngineAuthError` when ``error[]`` reads as a credential/balance
        rejection, otherwise :class:`EngineBlockedError`.
        """
        if not isinstance(payload, dict):
            raise EngineBlockedError(self.engine_id, "Kagi returned a non-object body")
        errors = payload.get("error")
        if errors:
            self._raise_api_error(payload, errors)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise EngineBlockedError(
                self.engine_id,
                "Kagi response carried no data bucket object "
                f"(keys: {sorted(payload)[:5]})",
            )
        return data

    def _raise_api_error(self, payload: dict[str, Any], errors: Any) -> None:
        """Classify a 200-delivered ``error`` field and raise."""
        message = _error_message(payload, errors) or "unspecified"
        if _QUOTA_PATTERN.search(message):
            raise EngineAuthError(
                self.engine_id, f"Kagi rejected the search on credentials: {message}"
            )
        raise EngineBlockedError(self.engine_id, f"Kagi refused the search: {message}")


def _error_message(payload: dict[str, Any], errors: Any) -> str:
    """Extract the most specific error text, mirroring the reference implementation's extractor
    order.

    the reference implementation reads top-level ``message``/``detail`` first, then a string
    ``error``,
    then the first ``message``/``msg`` across an ``error[]`` array.
    """
    for key in ("message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(errors, str) and errors.strip():
        return errors.strip()
    if isinstance(errors, list):
        for entry in errors:
            if not isinstance(entry, dict):
                continue
            for key in ("message", "msg"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _recency_to_date(recency: Recency) -> str:
    """Return the ``YYYY-MM-DD`` UTC date ``recency`` before now.

    Reproduces the reference implementation's ``recencyToDate``, which mutates a JavaScript ``Date``
    through ``setUTCDate``/``setUTCMonth``/``setUTCFullYear``. Those setters
    *normalize* rather than clamp an out-of-range day-of-month, so a month or
    year step off a long month rolls into the following month (2025-03-31 minus
    one month => 2025-03-03; 2024-02-29 minus one year => 2023-03-01). UTC keeps
    the window deterministic regardless of host timezone.
    """
    today = datetime.now(UTC).date()
    if recency == "day":
        shifted = today - timedelta(days=1)
    elif recency == "week":
        shifted = today - timedelta(days=7)
    elif recency == "month":
        shifted = _js_set_month(today, today.year, today.month - 1)
    else:
        shifted = _js_set_month(today, today.year - 1, today.month)
    return shifted.isoformat()


def _js_set_month(source: date, year: int, month: int) -> date:
    """Rebuild ``source``'s day-of-month in ``year``/``month``, JS-style.

    ``month`` may be ``0`` (December of the previous year), matching the
    underflow ``setUTCMonth(getUTCMonth() - 1)`` produces in January. A day past
    the target month's end normalizes forward, exactly as the JS setters do.
    """
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(source.day, calendar.monthrange(year, month)[1])
    overflow = source.day - day
    return date(year, month, day) + timedelta(days=overflow)
