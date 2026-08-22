# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Bing engine (keyless HTML scrape).

Bing answers a plain HTTP ``GET`` on ``/search?q=<query>`` with server-rendered
HTML — no credential, no JavaScript.

Endpoint choice: ``www.bing.com``, NOT ``cn.bing.com``. Measured behind this
environment's egress proxy, any request that lands on Bing's China market
(``cn.bing.com``, which redirects adding ``mkt=zh-CN``, or an explicit
``mkt=zh-CN`` / ``setmkt=zh-CN`` / ``cc=CN``) returns a *result-shaped page for
an unrelated query* — Spanish translation pages, UPS tracking, Premier League
fixtures — i.e. a poisoned edge cache, not an error. Nothing in the response
distinguishes it from a genuine SERP, so it silently fed off-topic sources into
the aggregator. ``www.bing.com`` with no market parameter returns correct
results on the same proxy (5/5 relevant across repeated probes), and without a
proxy it geo-redirects to ``cn.bing.com`` and is equally correct, so this one
endpoint is right on both paths. Do NOT reintroduce a ``mkt`` parameter.

The engine fetches the results page with a browser-like header set, parses the
``li.b_algo`` result blocks, and maps each onto an :class:`EngineHit`. Result
links may be wrapped in a ``/ck/a?...&u=a1<b64>`` redirect that
:func:`unwrap_redirect` recovers. A challenge / verification interstitial
raises :class:`EngineBlockedError` so the aggregator penalizes and skips the
engine.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from urllib.parse import urljoin

from qai.platform.web_search.independent.browser_fetch import fetch_or_browse
from qai.platform.web_search.independent.engines.base import (
    EngineHit,
    EngineQuery,
    EngineType,
)
from qai.platform.web_search.independent.html_parser import parse_html
from qai.platform.web_search.independent.http_client import HttpClient
from qai.platform.web_search.independent.url_utils import (
    is_http_url,
    unwrap_redirect,
)

__all__ = ["BingEngine"]

_log = logging.getLogger(__name__)

_ENGINE_ID = "bing"
_SEARCH_URL = "https://www.bing.com/search"
_BASE_URL = "https://www.bing.com/"

_RESULT_SELECTOR = "li.b_algo"
#: Raw substring probed in the HTML body to detect a result-bearing page (the
#: soft-block heuristic in ``_is_challenge`` works on the raw string, before
#: parsing). Kept in sync with ``_RESULT_SELECTOR``'s class.
_RESULT_SELECTOR_MARKER = "b_algo"
_TITLE_SELECTOR = "h2 > a"
_SNIPPET_SELECTORS = (".b_caption p", "div.b_caption")

# Substrings that, when present in the response body, indicate a challenge /
# verification interstitial rather than a genuine results page.  Be specific —
# broad terms like "challenge" match innocuous JS (challenges.cloudflare.com,
# PoWChallengeSolver) and "验证" matches normal Chinese content.
_BLOCK_MARKERS = (
    "verify you are human",
    "unusual traffic from your computer",
    "验证您是真人",
    "automated queries",
)

# Successful HTTP status band; anything outside it is treated as blocked.
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


#: Minimum share of result rows that must mention at least one query term for
#: the page to be accepted as a genuine SERP.
#:
#: Bing answers some automated requests with a *decoy* page: HTTP 200, a
#: complete ``li.b_algo`` list, the query correctly echoed in the search box —
#: but ten results for an entirely unrelated topic (measured: an online timer,
#: Hotmail sign-in, Italian bus routes, Premier League fixtures). Repeating one
#: URL yields a different unrelated set each time, and ``cache-control`` is
#: ``private, max-age=0``, so it is a deliberate anti-scrape decoy, not a cache
#: fault. Nothing in the markup separates it from a real SERP — byte size, block
#: count and every structural marker match — so the only usable signal is that
#: the results do not answer the query.
#:
#: Measured overlap: genuine pages score 0.8-1.0, decoys 0.0. The threshold sits
#: just above zero because a false positive costs only one headless retry (which
#: is never served a decoy), while a false negative feeds off-topic sources into
#: the aggregator undetected.
_MIN_TERM_OVERLAP = 0.2

#: Result rows inspected when scoring overlap.
_OVERLAP_SAMPLE = 10

_LATIN_TERM_RE = re.compile(r"[A-Za-z0-9]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _query_terms(query: str) -> frozenset[str]:
    """Split ``query`` into matchable terms.

    Latin words are taken whole (2+ chars, so initials and stop-fragments do not
    match everything). CJK has no word delimiters, so each run is cut into
    overlapping bigrams — a bigram is the shortest unit that still carries
    meaning, and matching single characters would hit unrelated text constantly.
    """
    terms = {word.lower() for word in _LATIN_TERM_RE.findall(query)}
    for run in _CJK_RUN_RE.findall(query):
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return frozenset(terms)


def _is_decoy(html: str, terms: frozenset[str]) -> bool:
    """Whether this SERP's results are unrelated to the query that asked for it.

    Returns ``False`` when the check cannot be applied (no extractable terms, or
    no result rows) so this never becomes a second, weaker empty-page detector —
    :meth:`BingEngine._is_challenge` already owns that case.
    """
    if not terms:
        return False
    document = parse_html(html)
    haystacks: list[str] = []
    for block in document.css(_RESULT_SELECTOR)[:_OVERLAP_SAMPLE]:
        anchor = block.css_first(_TITLE_SELECTOR)
        if anchor is None:
            continue
        haystacks.append(
            f"{anchor.text(strip=True)} {anchor.attr('href') or ''}".lower()
        )
    if not haystacks:
        return False
    matched = sum(1 for hay in haystacks if any(term in hay for term in terms))
    overlap = matched / len(haystacks)
    if overlap >= _MIN_TERM_OVERLAP:
        return False
    _log.info(
        "bing decoy page detected: term_overlap=%.0f%% rows=%d — escalating to browser",
        overlap * 100,
        len(haystacks),
    )
    return True

class BingEngine:
    """Keyless Bing HTML scrape implementing the ``Engine`` protocol."""

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
        """Whether the plain-HTTP response should escalate to the browser.

        Escalate on any of:
        * a non-2xx status (hard block);
        * a verification-interstitial marker in the body;
        * a 2xx body that carries NO result block (``li.b_algo`` absent) — Bing
          serves a markerless consent / JS-shell "soft block" (HTTP 200, no
          challenge text, zero results) to plain HTTP from datacenter / gateway
          egress; the headless browser renders the real result list, so a
          result-less page is treated as a fallback trigger rather than a
          genuine empty SERP. (A truly empty query result still upgrades once
          and then parses to 0 hits — acceptable, and rare for real queries.)
        """
        if not (_HTTP_OK_MIN <= status < _HTTP_OK_MAX):
            return True
        lowered = html.lower()
        if any(marker in lowered for marker in _BLOCK_MARKERS):
            return True
        # Soft block: a 200 with no result-list marker at all.
        return _RESULT_SELECTOR_MARKER not in lowered

    def _fallback_predicate(self, query: str) -> Callable[[str, int], bool]:
        """Bind ``query`` into the escalation predicate for decoy detection.

        ``fetch_or_browse`` passes only ``(body, status)``, but recognising a
        decoy page needs the query it was supposed to answer, so the term set is
        captured in a closure instead of widening that contract.
        """
        terms = _query_terms(query)

        def _predicate(html: str, status: int) -> bool:
            if self._is_challenge(html, status):
                return True
            return _is_decoy(html, terms)

        return _predicate

    async def search(self, query: EngineQuery) -> list[EngineHit]:
        # Plain HTTP first; on an anti-bot challenge, a decoy page, or a
        # transport failure the upgrade chain re-runs the navigation through a
        # headless browser, and raises EngineBlockedError only when the browser
        # also cannot recover.
        body, _status, _final_url = await fetch_or_browse(
            _SEARCH_URL,
            params={"q": query.query},
            engine_id=_ENGINE_ID,
            should_fallback=self._fallback_predicate(query.query),
            http_client=self._http,
            home_url=_BASE_URL,
            ready_selector=_RESULT_SELECTOR,
        )
        return self._parse(body, query.count)

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
        url = unwrap_redirect(href)
        if not is_http_url(url):
            url = urljoin(_BASE_URL, url)
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
