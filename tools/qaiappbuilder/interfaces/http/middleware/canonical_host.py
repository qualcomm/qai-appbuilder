# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause

"""Canonical-host redirect middleware.

Forces all browser requests to use ``localhost`` as the hostname.  When a
request arrives on ``127.0.0.1`` (same machine, different origin string),
this middleware returns a 307 redirect to the same path on ``localhost``.

**Why**: Okta redirect_uris are registered on ``localhost`` only.  If the
SPA runs on ``127.0.0.1`` while Okta callbacks land on ``localhost``, the
browser sees two distinct origins — breaking cookies, ``postMessage``, and
popup-based login flows.  Canonicalizing early (before the SPA loads) keeps
the entire session on one origin with zero user-visible impact.

Only ``Host`` values whose hostname portion is ``127.0.0.1`` are redirected;
the port is preserved.  Non-browser requests (``fetch`` from JS, API
clients) are NOT redirected — they typically don't set ``Sec-Fetch-Mode``
and would break if they received a 307.  We use the ``Sec-Fetch-Dest``
header (present on all modern browser navigation/document requests) to
distinguish.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp

from qai.platform.config.settings import LOOPBACK_HOST, LOOPBACK_HOST_NAME

__all__ = ["CanonicalHostMiddleware"]


class CanonicalHostMiddleware(BaseHTTPMiddleware):
    """Redirect ``127.0.0.1`` requests to ``localhost`` (same port)."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        host = request.headers.get("host", "")

        # Only act on the dotted quad — leave the host NAME (and anything
        # else) alone; that name is already the canonical origin.
        if not host.startswith(LOOPBACK_HOST):
            return await call_next(request)

        # Only redirect browser navigations (document/iframe fetches).
        # API calls from JS (fetch/XHR) and WebSocket upgrades pass through
        # so they don't get a useless 307 body.
        sec_fetch_dest = request.headers.get("sec-fetch-dest", "")
        if sec_fetch_dest not in ("document", "iframe"):
            return await call_next(request)

        # Build the canonical URL: swap the IP for the name, keep the port.
        canonical_host = host.replace(LOOPBACK_HOST, LOOPBACK_HOST_NAME, 1)
        # request.url is a starlette URL object; reconstruct with new host.
        url = str(request.url)
        canonical_url = url.replace(host, canonical_host, 1)

        return RedirectResponse(canonical_url, status_code=307)
