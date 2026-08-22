# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Mojeek engine (keyless HTML scrape).

Mojeek runs an independent index and answers a plain HTTP ``GET`` on
``/search`` with server-rendered HTML — no credential, no JavaScript. It is the
first-phase fast lane whose result domain overlaps little with the browser
engine, widening coverage.

The engine fetches the results page with a browser-like header set, parses the
standard result list, and maps each block onto an :class:`EngineHit`. A
CAPTCHA / anti-bot interstitial (ALTCHA challenge, ``robot`` marker, or a 429
that surfaces as a challenge body) raises :class:`EngineBlockedError` so the
aggregator penalizes and skips the engine.
"""

from __future__ import annotations

from urllib.parse import urlparse, urljoin

from qai.platform.web_search.independent.browser_fetch import fetch_or_browse
from qai.platform.web_search.independent.engines.base import (
    EngineHit,
    EngineQuery,
    EngineType,
    Recency,
)
from qai.platform.web_search.independent.errors import EngineBlockedError
from qai.platform.web_search.independent.html_parser import parse_html
from qai.platform.web_search.independent.http_client import HttpClient
from qai.platform.web_search.independent.url_utils import is_http_url

__all__ = ["MojeekEngine"]

_ENGINE_ID = "mojeek"
# the reference implementation uses mojeek.de (German TLD) — less likely to be geo-blocked from
# Asian IPs.
_SEARCH_URL = "https://www.mojeek.de/search"
_BASE_URL = "https://www.mojeek.de/"
_HOME_URL = "https://www.mojeek.de/?arc=none&lang=en&lb=en&theme=dark"

_RESULT_SELECTOR = "ul.results-standard"
_LI_SELECTOR = "ul.results-standard > li"
_SNIPPET_SELECTOR = "p.s"

# ALTCHA is a proof-of-work challenge: the browser hashes for tens of seconds
# before the widget self-verifies and Mojeek redirects to the results.
#
# Budget deliberately BELOW the reference implementation's 45s. That value is
# per-wait, and this engine performs two of them (navigation, then the result
# selector) inside a 2-attempt retry loop that already carries a 30s navigation
# budget per attempt — worst case ~240s, measured at 187s live. The aggregator's
# own hard deadline is 30s (search_config.toml), so anything beyond that cannot
# reach the user anyway: it only pins the shared single-context browser and
# starves the other engines that could still answer. 12s is enough for the PoW
# to land on a warm browser and keeps the whole engine inside one deadline.
_CAPTCHA_SOLVE_TIMEOUT_MS = 12_000

# the reference implementation uses ``since`` with literal recency tokens (same vocabulary as the
# query
# parameter) — Mojeek accepts "day", "week", "month", "year" verbatim.
_RECENCY_SINCE: dict[Recency, str] = {
    "day": "day",
    "week": "week",
    "month": "month",
    "year": "year",
}

# Substrings that, when present in the response body, indicate an anti-bot
# interstitial rather than a genuine results page.
_BLOCK_MARKERS = ("altcha-widget", "altcha", "captcha-wrap", "automated queries")

# Domains belonging to Mojeek itself — results pointing here are self-links
# (e.g. related-search suggestions) and should be filtered out.
_MOJEEK_DOMAINS = {"mojeek.com", "mojeek.co.uk", "mojeek.fr", "mojeek.de"}

# Successful HTTP status band; anything outside it is treated as blocked.
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


class MojeekEngine:
    """Keyless Mojeek HTML scrape implementing the ``Engine`` protocol."""

    __slots__ = ("_http",)

    engine_id: str = _ENGINE_ID
    engine_type: EngineType = "http_keyless"

    def __init__(self, *, http_client: HttpClient | None = None) -> None:
        # ``credential`` is meaningless for a keyless engine; the loader may
        # pass one but it is ignored. A missing client is self-provisioned so
        # tests can inject a mock while production stays zero-config.
        self._http = http_client if http_client is not None else HttpClient(_ENGINE_ID)

    def _build_params(self, query: EngineQuery) -> dict[str, str]:
        """Build query params aligned with reference implementation.

        the reference implementation sends: q, t (result count), arc=none, lang=en, lb=en,
        theme=dark,
        and optionally since=<recency token>.
        """
        params: dict[str, str] = {
            "q": query.query,
            "t": str(query.count),
            "arc": "none",
            "lang": "en",
            "lb": "en",
            "theme": "dark",
        }
        if query.recency is not None:
            params["since"] = _RECENCY_SINCE[query.recency]
        return params

    @staticmethod
    def _is_challenge(html: str, status: int) -> bool:
        """Whether the response is an anti-bot challenge (the fallback trigger).

        A challenge marker (ALTCHA widget, captcha wrap, or automated-queries
        text) is only treated as a block when the standard results list is also
        absent — this avoids false positives on pages that happen to contain the
        marker text within legitimate results.
        """
        if not (_HTTP_OK_MIN <= status < _HTTP_OK_MAX):
            return True
        lowered = html.lower()
        has_markers = any(m in lowered for m in _BLOCK_MARKERS)
        has_results = "results-standard" in lowered
        return has_markers and not has_results

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        # Plain HTTP first; on an anti-bot challenge (or transport failure) the
        # upgrade chain re-runs the navigation through a headless browser.
        # Aligned with reference: randomize_headers=False, attempts=2, retry_delay=1s,
        # and after_navigation for ALTCHA solving.
        body, _status, _final_url = await fetch_or_browse(
            _SEARCH_URL,
            params=self._build_params(query),
            engine_id=_ENGINE_ID,
            should_fallback=self._is_challenge,
            home_url=_HOME_URL,
            ready_selector=_RESULT_SELECTOR,
            referer=_HOME_URL,
            http_client=self._http,
            randomize_headers=False,
            after_navigation=_solve_altcha,
            attempts=2,
            retry_delay_ms=1000,
        )
        # The browser upgrade can still land on a robot wall: Mojeek serves the
        # "automated queries" refusal as a 403 body and the ALTCHA interstitial
        # as a 200.  ``fetch_or_browse`` returns the last body it saw rather
        # than raising, so re-check it here — otherwise the wall silently parses
        # to zero hits and looks like a legitimately empty result set.
        if self._is_challenge(body, _HTTP_OK_MIN):
            raise EngineBlockedError(
                _ENGINE_ID,
                "Mojeek blocked the request with its automated-queries wall; "
                "it rate-limits scripted searches from datacenter/shared-egress IPs",
            )
        return self._parse(body, query.count)

    def _parse(self, body: str, count: int) -> list[EngineHit]:
        document = parse_html(body)
        hits: list[EngineHit] = []
        for block in document.css(_LI_SELECTOR):
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
        title_node = block.css_first("h2 a.title") or block.css_first("a.title")  # type: ignore[attr-defined]
        if title_node is None:
            return None
        href = title_node.attr("href")
        if not href:
            return None
        url = href if is_http_url(href) else urljoin(_BASE_URL, href)
        if not is_http_url(url):
            return None
        if _is_mojeek_domain(url):
            return None
        title = title_node.text(strip=True)
        snippet_node = block.css_first(_SNIPPET_SELECTOR)  # type: ignore[attr-defined]
        snippet = snippet_node.text(strip=True) if snippet_node is not None else ""
        return EngineHit(title=title, url=url, snippet=snippet, rank=rank)


def _is_mojeek_domain(url: str) -> bool:
    """Return True if *url* points to a Mojeek-owned domain (or subdomain)."""
    hostname = urlparse(url).hostname
    if hostname is None:
        return False
    hostname = hostname.lower()
    for domain in _MOJEEK_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


async def _solve_altcha(page: object) -> None:
    """Solve Mojeek's ALTCHA interstitial if present.

    Aligned with the reference implementation: return early when results are already
    visible, otherwise click the ALTCHA checkbox and wait for the verified
    redirect to populate the result list.

    ALTCHA is a proof-of-work challenge — the browser must burn CPU hashing
    before the widget self-verifies and Mojeek redirects.  The wait budget is
    therefore 45s (the reference implementation's ``CAPTCHA_SOLVE_TIMEOUT_MS``), not the ordinary
    ready-selector budget; a shorter wait abandons a challenge that was about
    to succeed.
    """
    # If results are already visible, no challenge to solve.
    query_selector = getattr(page, "query_selector", None)
    if query_selector is None:
        return
    results = await query_selector("ul.results-standard li")
    if results is not None:
        return

    # Look for the ALTCHA checkbox widget.
    checkbox = await query_selector("altcha-widget input[type=checkbox]")
    if checkbox is None:
        return

    click = getattr(checkbox, "click", None)
    if click is None:
        return

    # Arm the navigation wait before clicking: the verified redirect can land
    # before a post-click wait would be registered.
    expect_navigation = getattr(page, "expect_navigation", None)
    if expect_navigation is not None:
        try:
            async with expect_navigation(
                wait_until="domcontentloaded", timeout=_CAPTCHA_SOLVE_TIMEOUT_MS
            ):
                await click()
        except Exception:
            pass  # No redirect fired; the selector wait below still decides.
    else:
        await click()

    # Wait for the results to appear after the challenge is solved.
    wait_for_selector = getattr(page, "wait_for_selector", None)
    if wait_for_selector is not None:
        try:
            await wait_for_selector(
                "ul.results-standard li", timeout=_CAPTCHA_SOLVE_TIMEOUT_MS
            )
        except Exception:
            pass  # Challenge solving may have failed; let the caller decide.
