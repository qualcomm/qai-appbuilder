# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Shared browser-context option builder.

Turns the caller-supplied unified TLS-verification switch (and optional
viewport) into the keyword mapping accepted by Playwright's
``browser.new_context(...)`` / ``chromium.launch_persistent_context(...)``.
Kept free of any search-specific config so both the web-search engine and the
interactive browser tool can build contexts the same way.
"""

from __future__ import annotations

from typing import Any

__all__ = ["CHROME_USER_AGENT", "build_context_options"]

#: Desktop Chrome UA used for every automated context.
#:
#: Playwright's default headless UA carries the literal ``HeadlessChrome``
#: product token, which is the single cheapest automation tell on the web —
#: Mojeek answers it with a hard ``403 automated queries`` wall even after the
#: ``navigator.webdriver`` patch, so the token must never reach the wire.
#: Matches the reference implementation fingerprint (Chrome 149 on macOS) so the UA agrees
#: with the ``sec-ch-ua*`` client hints the plain-HTTP path sends.
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


def build_context_options(
    *,
    ssl_verify: bool,
    viewport: dict[str, int] | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """Return ``new_context`` kwargs for the given TLS / viewport / proxy policy.

    ``ssl_verify`` is the unified ``Settings.ssl_verify`` value: when ``False``
    the context ignores HTTPS certificate errors (enterprise MITM gateways with
    an untrusted CA), when ``True`` it enforces verification. ``viewport`` is an
    optional ``{"width": W, "height": H}`` mapping; omitted → Playwright's
    default viewport.

    ``proxy_url`` routes the browser through an HTTP proxy. This MUST be plumbed
    through: Playwright launches its own process and inherits nothing from the
    ``httpx`` clients, so without it the browser engines egress from a different
    IP than every other engine. Observed consequence: with the corporate proxy
    configured, Google's plain-HTTP path succeeded (US egress) while the headless
    browser — still on the direct egress — was answered with the
    ``unusual traffic`` interstitial, so the browser engine failed on exactly the
    query the proxy had just made reachable.

    The context always overrides the user agent: see :data:`CHROME_USER_AGENT`.
    """
    options: dict[str, Any] = {
        "ignore_https_errors": not ssl_verify,
        "user_agent": CHROME_USER_AGENT,
    }
    if proxy_url:
        options["proxy"] = {"server": proxy_url}
    if viewport is not None:
        width = viewport.get("width")
        height = viewport.get("height")
        if isinstance(width, int) and isinstance(height, int):
            options["viewport"] = {"width": width, "height": height}
    return options
