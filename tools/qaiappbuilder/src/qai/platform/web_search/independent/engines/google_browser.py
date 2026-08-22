# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Google SERP scrape via headless Chromium (the zero-credential fallback).

This is the "always at least one path returns results" engine. A pure-HTTP
Google request trips the ``unusual traffic`` challenge in the target network,
but a stealth-patched headless Chromium clears it and returns a full SERP in
~11 s. Slow, so it is never the primary path — the aggregator gives browser
engines a longer minimum wait — but reliable.

Navigation is two-step: hit the homepage first to pick up consent/NID cookies,
then the ``/search`` results page. The HTML is read from the live page and
parsed with the shared :func:`~..html_parser.parse_html` helper so selector
logic stays uniform with the HTTP engines.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse

from ..browser import BrowserLifecycle, apply_stealth, is_available
from ..browser_fetch import fetch_or_browse
from ..errors import EngineBlockedError, EngineError, EngineTimeoutError
from ..html_parser import parse_html
from .base import EngineHit, EngineQuery, Recency

if TYPE_CHECKING:
    from ..http_client import HttpClient

__all__ = ["GoogleBrowserEngine", "is_available"]

_ENGINE_ID = "google_browser"

#: Google's Hong Kong host. Chosen because it works on BOTH routes: with the
#: corporate proxy (US egress) and without it. ``www.google.com`` on the direct
#: route geo-redirects here anyway and then answers with the unusual-traffic
#: interstitial, so targeting it directly removes a redirect hop and a failure
#: mode without losing anything.
_HOMEPAGE_URL = "https://www.google.com.hk/"
_SEARCH_URL = "https://www.google.com.hk/search"

#: CSS selector for the results container; presence signals the SERP rendered.
_RESULTS_CONTAINER = "a h3"

#: Snippet selectors in priority order (first match wins).
_SNIPPET_SELECTORS = (
    "[data-sncf='1'] .VwiC3b",
    ".VwiC3b",
    ".IsZvec",
    ".BNeawe.s3v9rd",
    "[data-sncf='1']",
)

#: Wait budget (ms) for the results container to appear after navigation.
_RESULTS_WAIT_MS = 15_000
#: Overall page navigation budget (ms): homepage + search + first result block.
_NAV_TIMEOUT_MS = 30_000

#: Markers that identify a bot-detection challenge instead of a real SERP.
_SORRY_PATH_MARKER = "/sorry/"
_UNUSUAL_TRAFFIC_MARKER = "unusual traffic from your computer network"
_JS_CHALLENGE_MARKER = "/httpservice/retry/enablejs"
_RECAPTCHA_MARKER = "g-recaptcha"

#: recency -> Google ``tbs=qdr:<unit>`` value.
_RECENCY_TBS = {
    "day": "qdr:d",
    "week": "qdr:w",
    "month": "qdr:m",
    "year": "qdr:y",
}

_HTTP_URL_PREFIX = "http"

#: Regex to strip trailing "Read more" from snippets.
_READ_MORE_RE = re.compile(r"\s*Read more$", re.IGNORECASE)



#: Hosts that belong to Google itself. A result pointing at one of these is
#: internal navigation (images / maps / the /url redirect wrapper), not a search
#: result. Checked as a suffix set rather than ``endswith("google.com")`` because
#: the engine targets the ``.hk`` host: a bare ``google.com`` test would fail to
#: recognise ``www.google.com.hk`` as Google's own and let its self-links through
#: as if they were results.
_GOOGLE_HOST_SUFFIXES = (".google.com", ".google.com.hk")
_GOOGLE_HOSTS = frozenset({"google.com", "google.com.hk"})


def _is_google_host(host: str) -> bool:
    """Whether ``host`` is Google's own (any of the domains this engine touches)."""
    host = host.lower()
    return host in _GOOGLE_HOSTS or host.endswith(_GOOGLE_HOST_SUFFIXES)

class GoogleBrowserEngine:
    """Scrape Google's SERP through a stealth headless Chromium page.

    Constructed with the uniform engine signature; ``http_client`` and
    ``credential`` are accepted for parity with the HTTP engines but unused —
    this engine drives the shared :class:`BrowserLifecycle` instead.
    """

    engine_id = _ENGINE_ID
    engine_type = "browser"

    __slots__ = ("_lifecycle", "_http")

    def __init__(
        self,
        *,
        http_client: HttpClient | None = None,
        credential: str | None = None,
        lifecycle: BrowserLifecycle | None = None,
    ) -> None:
        del credential  # parity-only; browser engine uses lifecycle
        self._http = http_client
        self._lifecycle = lifecycle or BrowserLifecycle()

    @staticmethod
    def is_available() -> bool:
        """Whether Playwright is importable, i.e. this engine may be built."""
        return is_available()

    def _search_url(self, query: str, recency: Recency | None) -> str:
        params: dict[str, str] = {
            "q": query,
            "hl": "en",
            "gl": "us",
            "udm": "14",
            "pws": "0",
        }
        if recency is not None and recency in _RECENCY_TBS:
            params["tbs"] = _RECENCY_TBS[recency]
        return f"{_SEARCH_URL}?{urlencode(params)}"

    def _build_params(self, query: EngineQuery) -> dict[str, str]:
        """Build the query parameter dict (for fetch_or_browse)."""
        params: dict[str, str] = {
            "q": query.query,
            "hl": "en",
            "gl": "us",
            "udm": "14",
            "pws": "0",
        }
        if query.recency is not None and query.recency in _RECENCY_TBS:
            params["tbs"] = _RECENCY_TBS[query.recency]
        return params

    @staticmethod
    def _unwrap_url(href: str) -> str | None:
        """Extract the real target URL from Google's /url?q=... redirect wrapper.

        Returns the unwrapped URL, the original href if it is already a direct
        external link, or ``None`` if the href is a google.com self-link that
        should be filtered out.
        """
        try:
            parsed = urlparse(href)
        except ValueError:
            return None

        host = (parsed.hostname or "").lower()

        # Google redirect wrapper: /url?q=<target> — can be absolute or relative
        if parsed.path == "/url" and (not host or _is_google_host(host)):
            qs = parse_qs(parsed.query)
            targets = qs.get("q") or qs.get("url")
            if targets and targets[0].startswith(_HTTP_URL_PREFIX):
                return targets[0]
            return None

        # Other Google self-links (images, maps, etc.) — filter out
        if _is_google_host(host):
            return None

        # Already a direct URL
        if href.startswith(_HTTP_URL_PREFIX):
            return href

        return None

    @staticmethod
    def _is_challenge(html: str, status: int) -> bool:
        """Whether the plain-HTTP response should escalate to the browser.

        Aligned with reference implementation: checks status, /sorry/, unusual traffic,
        g-recaptcha, and the JS-challenge enablejs marker.
        """
        if status == 403 or status == 429:
            return True
        if status != 200:
            return True
        lowered = html.lower()
        if _SORRY_PATH_MARKER in lowered:
            return True
        if _UNUSUAL_TRAFFIC_MARKER in lowered:
            return True
        if _RECAPTCHA_MARKER in lowered:
            return True
        # JS challenge: Google returns enablejs script with no real results
        if _JS_CHALLENGE_MARKER in lowered and "<h3" not in lowered:
            return True
        # No <h3 means JS challenge / empty page
        if "<h3" not in lowered:
            return True
        return False

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        """Render the SERP in the headless browser and return parsed hits.

        Browser-ONLY by design — there is no plain-HTTP attempt. Google answers
        a credential-free HTTP GET with its "enable JavaScript" interstitial
        (~91 KB carrying three anchors and zero ``h3``), never results, so the
        old fetch-then-escalate chain spent a full request/parse round trip on a
        response that could not succeed before escalating anyway. Verified on
        both the direct and the proxied route, with and without ``udm=14``.
        """
        if not is_available():
            raise EngineError(_ENGINE_ID, "playwright is not installed")
        try:
            return await self._run(query)
        except (EngineBlockedError, EngineError):
            raise
        except TimeoutError as exc:
            raise EngineTimeoutError(_ENGINE_ID, str(exc)) from exc
        except Exception as exc:
            self._raise_for_playwright_timeout(exc)
            raise EngineError(_ENGINE_ID, f"browser navigation failed: {exc}") from exc

    @staticmethod
    def _raise_for_playwright_timeout(exc: Exception) -> None:
        if type(exc).__name__ == "TimeoutError":
            raise EngineTimeoutError(_ENGINE_ID, str(exc)) from exc

    async def _run(self, query: EngineQuery) -> list[EngineHit]:
        """Full browser-only path (kept as ultimate fallback)."""
        async with self._lifecycle.page() as page:
            page.set_default_timeout(_NAV_TIMEOUT_MS)
            page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
            await apply_stealth(page)
            await page.goto(_HOMEPAGE_URL, wait_until="domcontentloaded")
            await page.goto(
                self._search_url(query.query, query.recency),
                wait_until="domcontentloaded",
            )
            await self._guard_challenge(page)
            await self._wait_for_results(page)
            html = await page.content()
        self._raise_if_challenge_html(html)
        return self._parse(html, query.count)

    async def _guard_challenge(self, page: object) -> None:
        current = getattr(page, "url", "") or ""
        if _SORRY_PATH_MARKER in current:
            raise EngineBlockedError(_ENGINE_ID, "google served a /sorry challenge")

    async def _wait_for_results(self, page: object) -> None:
        wait_for = getattr(page, "wait_for_selector", None)
        if wait_for is None:
            return
        try:
            await wait_for(_RESULTS_CONTAINER, timeout=_RESULTS_WAIT_MS)
        except Exception as exc:
            # No results container may simply mean an empty SERP; challenge
            # detection on the HTML below decides whether this is a block.
            if type(exc).__name__ == "TimeoutError":
                return
            raise

    def _raise_if_challenge_html(self, html: str) -> None:
        lowered = html.lower()
        if _UNUSUAL_TRAFFIC_MARKER in lowered:
            raise EngineBlockedError(_ENGINE_ID, "google unusual-traffic challenge")
        if _RECAPTCHA_MARKER in lowered:
            raise EngineBlockedError(_ENGINE_ID, "google recaptcha challenge")

    def _parse(self, html: str, count: int) -> list[EngineHit]:
        """Parse the SERP by selecting result anchors that wrap a heading.

        Google wraps each organic result's ``h3`` heading in the result anchor,
        so the anchor is the natural unit: it carries the href and encloses both
        the title and the snippet. the reference implementation reaches it with
        ``heading.closest("a")``,
        a DOM API the :mod:`html_parser` surface deliberately does not expose
        (:class:`~qai.platform.web_search.independent.html_parser.Node` is only
        ``css`` / ``css_first`` / ``text`` / ``attr`` — no parent links), so this
        walks anchors top-down and keeps the ones enclosing a heading.

        Deliberately avoids a ``a:has(h3)`` selector: ``:has()`` support differs
        between the selectolax/Lexbor and lxml backends that
        :func:`~...html_parser.parse_html` picks between, and an unsupported
        selector would silently yield zero hits on one of them.
        """
        document = parse_html(html)
        hits: list[EngineHit] = []
        for anchor in document.css("a"):
            if len(hits) >= count:
                break
            hit = self._parse_anchor(anchor, rank=len(hits))
            if hit is not None:
                hits.append(hit)
        return hits

    def _parse_anchor(self, anchor: object, *, rank: int) -> EngineHit | None:
        """Parse one result anchor (an ``<a>`` wrapping an ``<h3>``)."""
        heading = anchor.css_first("h3")  # type: ignore[attr-defined]
        if heading is None:
            return None
        title = heading.text(strip=True)
        if not title:
            return None

        href = anchor.attr("href")  # type: ignore[attr-defined]
        if not href:
            return None
        url = self._unwrap_url(href)
        if not url or not url.startswith(_HTTP_URL_PREFIX):
            return None

        return EngineHit(
            title=title,
            url=url,
            snippet=self._extract_snippet(anchor),
            rank=rank,
        )

    @staticmethod
    def _extract_snippet(anchor: object) -> str:
        """Return the first snippet match found within the result anchor."""
        css_first = getattr(anchor, "css_first", None)
        if css_first is None:
            return ""
        for selector in _SNIPPET_SELECTORS:
            node = css_first(selector)
            if node is None:
                continue
            text = node.text(strip=True)
            if text:
                return _READ_MORE_RE.sub("", text)
        return ""
