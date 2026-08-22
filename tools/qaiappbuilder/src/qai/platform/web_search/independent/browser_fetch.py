# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""fetch-then-browser upgrade chain for the keyless HTML scrapers.

A keyless engine used to make a single plain HTTP request and fail hard the
moment the target served a bot-detection challenge. This module gives every
engine the reference implementation's fetch-then-browser escalation instead:

1. try the plain HTTP fetch first (cheap, no browser start), carrying the
   dynamic ``browser_headers`` fingerprint;
2. if the transport itself fails (TLS/connection error) **and** Playwright is
   importable, re-run the same navigation through a headless Chromium page;
3. if the fetch *succeeds* but the engine's own challenge detector flags the
   body / status as an anti-scrape interstitial, upgrade to the browser too;
4. the browser path reuses the existing :class:`BrowserLifecycle` (stealth +
   ``ignore_https_errors`` already honoured there) — optionally warming cookies
   on ``home_url`` first and waiting for ``ready_selector`` — and returns the
   rendered ``page.content()``;
5. if the browser is unavailable or also fails, raise
   :class:`EngineBlockedError` so the aggregator penalizes and skips the engine.

Everything Playwright is import-guarded through :func:`browser.is_available`: a
deployment without the ``search`` extra simply never upgrades and surfaces the
plain-HTTP outcome.
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from qai.platform.web_search.independent.browser import BrowserLifecycle, apply_stealth, is_available
from qai.platform.web_search.independent.errors import (
    EngineBlockedError,
    EngineError,
)
from qai.platform.web_search.independent.http_client import (
    _ACCEPT_ENCODING,
    HttpClient,
    browser_headers,
)

__all__ = ["ShouldFallback", "fetch_or_browse"]

_log = logging.getLogger(__name__)

# Static (non-randomized) headers for engines that prefer consistent fingerprints
# (e.g. Mojeek with randomizeHeaders: false in the reference implementation). Matches a standard
# Chrome UA.
#
# ``Accept-Encoding`` is deliberately absent: ``browser_headers()`` stamps the
# decodable-codec set onto every header dict it returns, and these engines route
# through it. Repeating a hardcoded ``gzip, deflate, br, zstd`` here is what made
# Baidu answer with undecodable Brotli and parse to zero hits.
_STATIC_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Encoding": _ACCEPT_ENCODING,
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Priority": "u=0, i",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", ";Not A Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
}


class ShouldFallback(Protocol):
    """Predicate deciding whether a plain-HTTP response is an anti-scrape page.

    Called with the response body text and status code; returns ``True`` when
    the engine should escalate to the browser (challenge page / non-2xx body).
    """

    def __call__(self, html: str, status: int) -> bool: ...


#: Navigation budget (ms) for ONE goto on the browser upgrade path.
#:
#: Sized from MEASURED navigation time against the aggregator's deadlines, not
#: in isolation. Google needs ~4s for the homepage warm-up and ~7s for the SERP
#: on this network, so a 10s budget timed the whole engine out mid-navigation
#: ("Page.goto: Timeout 10000ms exceeded") even though the page would have
#: loaded. 12s covers the observed worst case with margin.
#:
#: The counter-pressure is real and must not be forgotten: ``_browse`` can spend
#: this budget twice per attempt (homepage + target) across ``attempts`` retries,
#: so an over-generous value is what let Mojeek run ~187s. The retry-level guard
#: for that is the engine's own ``attempts`` count and the aggregator's 20s hard
#: deadline, NOT a per-goto budget so small that healthy pages cannot finish.
_NAV_TIMEOUT_MS = 12_000
#: Wait budget (ms) for ``ready_selector`` to appear. A page that rendered will
#: usually satisfy the selector immediately; this only covers late hydration.
_READY_WAIT_MS = 6_000


class _LifecycleHolder:
    """Lazy, thread-safe holder for the shared upgrade-path browser lifecycle.

    Distinct from the browser engine's own instance; the lifecycle is
    idle-closed, so a spare one is cheap and only ever warms Chromium when an
    upgrade actually fires. A ``BrowserLifecycle()`` constructor never touches
    Playwright, so building the holder lazily is purely to keep import-time
    side-effect free.
    """

    __slots__ = ("_lifecycle", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lifecycle: BrowserLifecycle | None = None

    def get(self) -> BrowserLifecycle:
        if self._lifecycle is None:
            with self._lock:
                if self._lifecycle is None:
                    self._lifecycle = BrowserLifecycle()
        return self._lifecycle


_LIFECYCLE = _LifecycleHolder()


async def fetch_or_browse(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: Any | None = None,
    headers: dict[str, str] | None = None,
    referer: str | None = None,
    engine_id: str,
    should_fallback: Callable[[str, int], bool],
    home_url: str | None = None,
    ready_selector: str | None = None,
    ready_selector_timeout_ms: int | None = None,
    http_client: HttpClient | None = None,
    randomize_headers: bool = True,
    after_navigation: Callable[..., Any] | None = None,
    attempts: int = 1,
    retry_delay_ms: int = 0,
) -> tuple[str, int, str]:
    """Fetch ``url`` via HTTP, escalating to a headless browser when blocked.

    Returns ``(html, status, final_url)``. ``status`` is the browser sentinel
    ``200`` when the browser path served the HTML (Playwright does not surface a
    numeric status through ``page.content()``).

    Raises :class:`EngineBlockedError` when a challenge was detected and the
    browser could not recover it (unavailable or failed), and re-raises a
    transport error only when the browser is unavailable to retry it.

    ``http_client`` is injectable for tests; production self-provisions one.
    """
    client = http_client if http_client is not None else HttpClient(engine_id)
    if randomize_headers:
        request_headers = headers if headers is not None else browser_headers()
    else:
        request_headers = headers if headers is not None else _STATIC_HEADERS.copy()
    if referer:
        # the reference implementation pairs a Referer with ``Sec-Fetch-Site: same-origin``; the
        # default
        # profile carries ``none``, and a referer alongside ``none`` is an
        # inconsistent fingerprint that bot walls (Mojeek) score against us.
        request_headers = {
            **request_headers,
            "Referer": referer,
            "Sec-Fetch-Site": "same-origin",
        }

    try:
        response = await client.request(
            method, url, headers=request_headers, params=params, data=data
        )
    except (EngineError, httpx.TransportError):
        # A transport-level failure (TLS / connection reset). ``HttpClient``
        # maps a cert-verify failure to ``EngineTlsError`` but re-raises other
        # transport errors raw, so both are caught. Escalate to the browser
        # when it is available; otherwise the plain-HTTP error stands.
        if is_available():
            return await _browse(
                url,
                params=params,
                engine_id=engine_id,
                home_url=home_url,
                ready_selector=ready_selector,
                ready_selector_timeout_ms=ready_selector_timeout_ms,
                after_navigation=after_navigation,
                attempts=attempts,
                retry_delay_ms=retry_delay_ms,
                should_fallback=should_fallback,
            )
        raise
    else:
        body = response.text
        status = response.status_code
        if not should_fallback(body, status):
            return body, status, str(response.url)
        # Challenge detected on the plain fetch. Upgrade if we can; if the
        # browser is unavailable the fetch was effectively blocked.
        if not is_available():
            raise EngineBlockedError(
                engine_id, "anti-scrape challenge and browser upgrade unavailable"
            )
        return await _browse(
            url,
            params=params,
            engine_id=engine_id,
            home_url=home_url,
            ready_selector=ready_selector,
            ready_selector_timeout_ms=ready_selector_timeout_ms,
            after_navigation=after_navigation,
            attempts=attempts,
            retry_delay_ms=retry_delay_ms,
            should_fallback=should_fallback,
        )


def _with_params(url: str, params: dict[str, Any] | None) -> str:
    """Fold query ``params`` into ``url`` (the browser navigates a full URL)."""
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"


async def _browse(
    url: str,
    *,
    params: dict[str, Any] | None,
    engine_id: str,
    home_url: str | None,
    ready_selector: str | None,
    ready_selector_timeout_ms: int | None = None,
    after_navigation: Callable[..., Any] | None = None,
    attempts: int = 1,
    retry_delay_ms: int = 0,
    should_fallback: Callable[[str, int], bool] | None = None,
) -> tuple[str, int, str]:
    """Render ``url`` through a headless Chromium page and return its content.

    ``after_navigation`` is an optional async callback invoked after the page
    navigates to the target URL (e.g. ALTCHA solving).  It receives the page
    as its sole argument.

    ``attempts`` controls how many times to re-navigate the target URL when
    ``should_fallback`` still flags the rendered body as a challenge page.  A
    bot wall arrives as an ordinary HTTP response (Mojeek's "automated
    queries" refusal is a 403 body, its ALTCHA interstitial a 200), so the
    retry must be driven by the body predicate rather than by an exception.
    ``retry_delay_ms`` is the pause between attempts.  The page and its
    homepage cookie warm-up are established once and reused across attempts,
    matching the reference implementation: a fresh page would discard the very cookies
    the retry depends on.
    """
    target = _with_params(url, params)
    tries = max(1, attempts)
    try:
        async with _LIFECYCLE.get().page() as page:
            await apply_stealth(page)
            page.set_default_timeout(_NAV_TIMEOUT_MS)
            page.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
            if home_url is not None:
                # Warm cookies on the homepage first (two-step navigation).
                await page.goto(home_url, wait_until="domcontentloaded")
            for attempt in range(tries):
                if attempt > 0 and retry_delay_ms > 0:
                    await asyncio.sleep(retry_delay_ms / 1000.0)
                await page.goto(target, wait_until="domcontentloaded")
                if after_navigation is not None:
                    await after_navigation(page)
                if ready_selector is not None:
                    await _wait_for_selector(page, ready_selector, ready_selector_timeout_ms)
                html = await page.content()
                final_url = getattr(page, "url", target) or target
                if should_fallback is None or attempt == tries - 1:
                    return html, 200, final_url
                if not should_fallback(html, 200):
                    return html, 200, final_url
                _log.debug(
                    "%s: browser attempt %d/%d still challenged; retrying",
                    engine_id, attempt + 1, tries,
                )
    except EngineError:
        raise
    except Exception as exc:
        raise EngineBlockedError(engine_id, f"browser upgrade failed: {exc}") from exc
    raise EngineBlockedError(engine_id, f"browser upgrade exhausted {tries} attempt(s)")


async def _wait_for_selector(page: object, selector: str, timeout_ms: int | None = None) -> None:
    wait_for = getattr(page, "wait_for_selector", None)
    if wait_for is None:
        return
    try:
        await wait_for(selector, timeout=timeout_ms or _READY_WAIT_MS)
    except Exception as exc:
        # A missing selector may just mean an empty (but genuine) result page;
        # the engine's own parse / challenge check decides. Only a non-timeout
        # error propagates.
        if type(exc).__name__ == "TimeoutError":
            return
        raise
