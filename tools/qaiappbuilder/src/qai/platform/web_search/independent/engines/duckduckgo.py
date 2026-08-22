# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""DuckDuckGo engine (keyless HTML scrape).

DuckDuckGo offers a JavaScript-free HTML frontend at
``https://html.duckduckgo.com/html/`` that answers a ``POST`` with form data
``q=<query>`` and returns server-rendered results.  No credential or API key is
required.

The engine fetches the results page with a browser-like header set, parses the
``div.result`` blocks, and maps each onto an :class:`EngineHit`.  Result links
are wrapped in a ``//duckduckgo.com/l/?uddg=<encoded_url>`` redirect that
:func:`_unwrap_ddg_url` recovers via the ``uddg`` query parameter.  An anomaly
/ anti-bot interstitial (``anomaly-modal`` or ``anomaly.js`` in the body)
raises :class:`EngineBlockedError` so the aggregator penalizes and skips the
engine.

Pagination
----------
One HTML page carries ~10 results, so a request for more than that used to be
silently truncated.  DDG's next page is not a URL: the response embeds a
``<form>`` whose hidden inputs (an ``s`` offset plus a per-session ``vqd``
token) must be POSTed back verbatim.  :meth:`_continuation_data` lifts those
fields and :meth:`search` follows them until the requested count is satisfied.
The loop stops on any of: enough hits, no continuation form, a page that adds
no new URL, or :data:`_MAX_PAGES` — so a malformed continuation cannot spin.
"""

from __future__ import annotations

import logging
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from qai.platform.web_search.independent.browser_fetch import fetch_or_browse
from qai.platform.web_search.independent.engines.base import (
    EngineHit,
    EngineQuery,
    EngineType,
    Recency,
)
from qai.platform.web_search.independent.html_parser import parse_html
from qai.platform.web_search.independent.http_client import HttpClient
from qai.platform.web_search.independent.url_utils import is_http_url

__all__ = ["DuckDuckGoEngine"]

_log = logging.getLogger(__name__)

_ENGINE_ID = "duckduckgo"
_SEARCH_URL = "https://html.duckduckgo.com/html/"
_BASE_URL = "https://duckduckgo.com/"

_RESULT_SELECTOR = "div.result"
_TITLE_SELECTOR = "a.result__a"
_SNIPPET_SELECTORS = ("a.result__snippet", "div.result__snippet")

# DDG ``df`` (date filter) parameter values corresponding to recency.
_RECENCY_MAP: dict[Recency, str] = {
    "day": "d",
    "week": "w",
    "month": "m",
    "year": "y",
}

# Substrings that, when present in the response body, indicate an anomaly /
# anti-bot challenge rather than a genuine results page.
_BLOCK_MARKERS = ("anomaly-modal", "anomaly.js")

# Hidden inputs that identify DDG's next-page form (as opposed to the search
# box form or the CAPTCHA form, which also carry inputs): ``s`` is the result
# offset, ``vqd`` the per-session token DDG requires on continuation POSTs.
_CONTINUATION_FIELDS = ("s", "vqd")

# Input types that carry UI labels rather than form data. DDG's next-page form
# includes a ``type="submit"`` button whose value is the "Next" caption; posting
# that back as a parameter would corrupt the continuation.
_NON_DATA_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})

# Upper bound on pages followed for one query. DDG serves ~10 hits per page and
# the aggregator caps requests at 20, so 3 covers the range with headroom while
# bounding worst-case latency (each page is a full round trip).
_MAX_PAGES = 3

# DDG may respond with HTTP 202 (Accepted) which is normal — include it in OK.
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


def _unwrap_ddg_url(href: str) -> str:
    """Recover the real destination from a DDG redirect wrapper.

    DDG wraps result links as ``//duckduckgo.com/l/?uddg=<percent-encoded>&...``
    or ``/l/?uddg=...``.  The ``uddg`` query param holds the target URL.
    If the href is not a redirect wrapper it is returned as-is.
    """
    parsed = urlparse(href)
    # The redirect path is always ``/l/`` (with optional trailing slash).
    if parsed.path.rstrip("/") != "/l":
        return href
    qs = parse_qs(parsed.query)
    uddg = qs.get("uddg")
    if uddg:
        return unquote(uddg[0])
    return href


class DuckDuckGoEngine:
    """Keyless DuckDuckGo HTML scrape implementing the ``Engine`` protocol."""

    __slots__ = ("_http",)

    engine_id: str = _ENGINE_ID
    engine_type: EngineType = "http_keyless"

    def __init__(
        self,
        *,
        credential: str | None = None,  # keyless engine ignores it
        http_client: HttpClient | None = None,
    ) -> None:
        # ``credential`` is meaningless for a keyless engine; the loader may
        # pass one but it is ignored.  A missing client is self-provisioned so
        # tests can inject a mock while production stays zero-config.
        self._http = http_client if http_client is not None else HttpClient(_ENGINE_ID)

    def _build_data(self, query: EngineQuery) -> dict[str, str]:
        """Build the POST form-data payload.

        Aligned with reference: includes ``kl`` (locale/region) and ``b`` (pagination
        offset, empty for first page) alongside the query and recency filter.
        """
        data: dict[str, str] = {"q": query.query, "kl": "us-en", "b": ""}
        if query.recency is not None:
            data["df"] = _RECENCY_MAP[query.recency]
        return data

    @staticmethod
    def _is_challenge(html: str, status: int) -> bool:
        """Whether the response is an anti-bot anomaly page (fallback trigger).

        DDG surfaces anomaly pages containing ``anomaly-modal`` or references to
        ``anomaly.js`` when it suspects automated traffic.  HTTP 202 is normal
        for DDG and should NOT be treated as a block.
        """
        if not (_HTTP_OK_MIN <= status < _HTTP_OK_MAX):
            return True
        lowered = html.lower()
        return any(marker in lowered for marker in _BLOCK_MARKERS)

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        # Plain HTTP POST first; on an anomaly page (or transport failure) the
        # upgrade chain re-runs the navigation through a headless browser, and
        # raises EngineBlockedError only when the browser also cannot recover.
        #
        # One page carries ~10 hits, so a larger ``count`` follows DDG's
        # continuation form. Dedup spans pages because DDG repeats a hit across
        # page boundaries when the underlying result set shifts mid-query.
        data = self._build_data(query)
        hits: list[EngineHit] = []
        seen: set[str] = set()
        stop = "page_cap"
        pages = 0
        try:
            for _page in range(_MAX_PAGES):
                body, _status, _final_url = await fetch_or_browse(
                    _SEARCH_URL,
                    method="POST",
                    data=data,
                    engine_id=_ENGINE_ID,
                    should_fallback=self._is_challenge,
                    http_client=self._http,
                )
                pages += 1
                document = parse_html(body)
                before = len(hits)
                self._collect(document, hits, seen, query.count)
                _log.info(
                    "duckduckgo page %d: +%d new hits (total %d/%d)",
                    pages,
                    len(hits) - before,
                    len(hits),
                    query.count,
                )
                if len(hits) >= query.count:
                    stop = "count_reached"
                    break
                # A page that contributed no new URL means the continuation is
                # looping or exhausted; stop rather than burn another round trip.
                if len(hits) == before:
                    stop = "no_new_urls"
                    break
                continuation = self._continuation_data(document)
                if continuation is None:
                    stop = "no_continuation_form"
                    break
                data = continuation
        except Exception as exc:
            # A block/transport failure on page 1 is the common case and the
            # summary must still be emitted: without it the engine goes fully
            # silent, and "blocked upstream" looks identical to "pagination
            # never ran" when reading the log.
            _log.info(
                "duckduckgo pagination: pages=%d hits=%d requested=%d stop=%s(%s)",
                pages,
                len(hits),
                query.count,
                type(exc).__name__,
                str(exc)[:120],
            )
            raise
        # One line per search answering "did pagination run, and why did it
        # stop" — the two questions a truncated result set raises.
        _log.info(
            "duckduckgo pagination: pages=%d hits=%d requested=%d stop=%s",
            pages,
            len(hits),
            query.count,
            stop,
        )
        return hits

    def _collect(
        self,
        document: object,
        hits: list[EngineHit],
        seen: set[str],
        count: int,
    ) -> None:
        """Append this page's new hits to ``hits`` (rank continues across pages)."""
        for block in document.css(_RESULT_SELECTOR):  # type: ignore[attr-defined]
            if len(hits) >= count:
                return
            hit = self._parse_block(block, len(hits))
            if hit is None or hit.url in seen:
                continue
            seen.add(hit.url)
            hits.append(hit)

    @staticmethod
    def _continuation_data(document: object) -> dict[str, str] | None:
        """Return the next-page POST payload from DDG's continuation form.

        DDG exposes no next-page URL: the offset ``s`` and session ``vqd`` live
        in hidden inputs that must be echoed back (alongside opaque fields like
        ``nextParams`` / ``dc`` / ``api``, forwarded verbatim — DDG rejects a
        continuation that drops them). Several forms are present (the search
        box, and on a challenge page the CAPTCHA), so the one carrying every
        field in :data:`_CONTINUATION_FIELDS` identifies the real one.

        The form also holds a ``type="submit"`` button; its value is the "Next"
        label, not a query parameter, so non-data input types are skipped rather
        than relying on the button having no ``name``.

        Returns ``None`` when this page has no next page.
        """
        for form in document.css("form"):  # type: ignore[attr-defined]
            fields: dict[str, str] = {}
            for node in form.css("input"):
                name = node.attr("name")
                if not name:
                    continue
                if (node.attr("type") or "").lower() in _NON_DATA_INPUT_TYPES:
                    continue
                fields[name] = node.attr("value") or ""
            if all(key in fields for key in _CONTINUATION_FIELDS):
                return fields
        return None

    @staticmethod
    def _parse_block(block: object, rank: int) -> EngineHit | None:
        # ``block`` is a parser ``Node``; typed as object to avoid importing the
        # protocol into the signature (it is only reached through ``css``).
        title_node = block.css_first(_TITLE_SELECTOR)  # type: ignore[attr-defined]
        if title_node is None:
            return None
        href = title_node.attr("href")
        if not href:
            return None
        url = _unwrap_ddg_url(href)
        if not is_http_url(url):
            url = urljoin(_BASE_URL, url)
        if not is_http_url(url):
            return None
        # DDG wraps query terms in <b> tags; unescape HTML entities in title.
        title = unescape(title_node.text(strip=True))
        snippet = ""
        for selector in _SNIPPET_SELECTORS:
            snippet_node = block.css_first(selector)  # type: ignore[attr-defined]
            if snippet_node is not None:
                snippet = unescape(snippet_node.text(strip=True))
                break
        return EngineHit(title=title, url=url, snippet=snippet, rank=rank)
