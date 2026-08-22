# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Baidu engine (keyless HTML scrape).

Baidu answers a plain HTTP ``GET`` on ``/s?wd=<query>`` with a large
server-rendered SERP — no credential, no JavaScript. It widens China-region
coverage where the western keyless engines return little.

The engine fetches the results page with a browser-like header set, parses the
``div.c-container`` result blocks, and maps each onto an :class:`EngineHit`.
Baidu wraps every result link in an opaque ``/link?url=<token>`` redirect. The
token cannot be decoded locally, but the endpoint answers ``HEAD`` with a 302 to
the real target, so the wrappers are resolved concurrently after parsing — a
wrapper URL is unusable downstream (uncitable, indistinguishable from a content
farm, and invisible to cross-engine dedup). Resolution is best-effort: a failed
lookup keeps the wrapper instead of dropping the hit. A
CAPTCHA / security-verification interstitial ("百度安全验证" or a validation
body) raises :class:`EngineBlockedError` so the aggregator penalizes and skips
the engine.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from urllib.parse import urljoin, urlsplit

from qai.platform.web_search.independent.browser_fetch import fetch_or_browse
from qai.platform.web_search.independent.engines.base import (
    EngineHit,
    EngineQuery,
    EngineType,
)
from qai.platform.web_search.independent.html_parser import parse_html
from qai.platform.web_search.independent.http_client import HttpClient
from qai.platform.web_search.independent.url_utils import is_http_url

__all__ = ["BaiduEngine"]

_ENGINE_ID = "baidu"
_SEARCH_URL = "https://www.baidu.com/s"
_BASE_URL = "https://www.baidu.com/"

_RESULT_SELECTOR = "div.c-container"
_TITLE_SELECTOR = "h3 > a"
_SNIPPET_SELECTORS = (".c-abstract", "[class*=content]")

# Substrings that, when present in the response body, indicate a security
# verification / CAPTCHA interstitial rather than a genuine results page.
_BLOCK_MARKERS = ("百度安全验证", "验证码", "security verification")

# Successful HTTP status band; anything outside it is treated as blocked.
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


class BaiduEngine:
    """Keyless Baidu HTML scrape implementing the ``Engine`` protocol."""

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
        # pass one but it is ignored. A missing client is self-provisioned so
        # tests can inject a mock while production stays zero-config.
        self._http = http_client if http_client is not None else HttpClient(_ENGINE_ID)

    @staticmethod
    def _is_challenge(html: str, status: int) -> bool:
        """Whether the response is a security-verification / CAPTCHA challenge.

        A "百度安全验证" / validation interstitial body or any non-2xx status
        means the plain fetch was blocked and the browser upgrade should run.
        """
        if not (_HTTP_OK_MIN <= status < _HTTP_OK_MAX):
            return True
        return any(marker in html for marker in _BLOCK_MARKERS)

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        # Plain HTTP first; on a security-verification challenge (or transport
        # failure) the upgrade chain re-runs the navigation through a headless
        # browser, raising EngineBlockedError only when it too cannot recover.
        body, _status, _final_url = await fetch_or_browse(
            _SEARCH_URL,
            params={"wd": query.query},
            engine_id=_ENGINE_ID,
            should_fallback=self._is_challenge,
            http_client=self._http,
        )
        return await self._resolve_wrappers(self._parse(body, query.count))

    async def _resolve_wrappers(self, hits: list[EngineHit]) -> list[EngineHit]:
        """Replace Baidu's ``/link?url=`` wrappers with their target URLs.

        The wrapper token is opaque, but the endpoint answers a plain ``HEAD``
        with ``302 Location: <target>``, so one cheap request per hit recovers
        the real URL. Worth doing: a wrapper URL is unusable downstream — an
        agent cannot cite it, the caller cannot tell github.com from a content
        farm, and :func:`dedup_key` cannot match the same page found by another
        engine, so every Baidu hit survives cross-engine dedup as a false
        unique.

        Resolution is best-effort and bounded: failures keep the wrapper rather
        than dropping the hit, and all lookups run concurrently so the added
        latency is one round-trip, not one per hit.
        """
        targets = [i for i, h in enumerate(hits) if _is_wrapper(h.url)]
        if not targets:
            return hits
        resolved = await asyncio.gather(
            *(self._http.redirect_target(hits[i].url) for i in targets),
            return_exceptions=True,
        )
        out = list(hits)
        for i, target in zip(targets, resolved, strict=True):
            if isinstance(target, str) and target:
                out[i] = replace(out[i], url=target)
        return out

    def _parse(self, body: str, count: int) -> list[EngineHit]:
        document = parse_html(body)
        hits: list[EngineHit] = []
        for block in document.css(_RESULT_SELECTOR):
            if len(hits) >= count:
                break
            hit = self._parse_block(block, len(hits))
            if hit is not None:
                hits.append(hit)
        return hits

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
        # Baidu wraps links in an encrypted ``/link?url=`` redirect that cannot
        # be resolved offline; keep the wrapper URL verbatim.
        url = href if is_http_url(href) else urljoin(_BASE_URL, href)
        if not is_http_url(url):
            return None
        title = title_node.text(strip=True)
        snippet = ""
        for selector in _SNIPPET_SELECTORS:
            snippet_node = block.css_first(selector)  # type: ignore[attr-defined]
            if snippet_node is not None:
                snippet = snippet_node.text(strip=True)
                break
        return EngineHit(title=title, url=url, snippet=snippet, rank=rank)


def _is_wrapper(url: str) -> bool:
    """Whether ``url`` is Baidu's ``/link`` redirect rather than a real target.

    Matched on host + path so a genuine result that merely happens to live on a
    ``baidu.com`` subdomain is not mistaken for a wrapper.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.path == "/link" and (host == "baidu.com" or host.endswith(".baidu.com"))
