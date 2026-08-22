# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Double-submit-cookie CSRF middleware (PR-040, issue e decision A).

Mounted in ``apps/api/main.py`` AFTER :func:`register_error_handlers`
and BEFORE ``include_router``. Returns the unified
``{type, code, message, details?}`` JSON envelope directly on a CSRF
violation instead of relying on the global error handler — Starlette's
``BaseHTTPMiddleware`` runs ``dispatch`` inside an anyio ``TaskGroup``
that wraps any uncaught exception in an ``ExceptionGroup`` before the
``add_exception_handler`` chain sees it, which breaks single-type
mappings.

Wire shape
----------
* On any request lacking the configured cookie (default ``qai_csrf``),
  the response is augmented with a fresh ``Set-Cookie`` header containing
  a 32-byte urlsafe token.
* Non-safe HTTP methods (anything outside
  ``settings.security.csrf_method_safe``, i.e. POST / PUT / PATCH /
  DELETE) MUST present:

  - the cookie (``request.cookies.get(cookie_name)``), and
  - the matching header (``request.headers.get(header_name)``)

  with a non-empty equal value. Any mismatch / absence yields a 403
  envelope (``type=ForbiddenError``, ``code=security.csrf.missing``).
* Path allowlist (default in ``SecuritySettings.csrf_path_allowlist``)
  short-circuits the check for read-only public endpoints (system
  health / build-info / edition / OpenAPI / docs / redoc).

WebSocket connections (``Upgrade: websocket``) are intentionally NOT
covered — WS handshakes do not match the HTTP method semantics; if a
specific WS path needs origin protection, the route handler must add it
explicitly. ``SecuritySettings.csrf_enabled = False`` short-circuits
the entire middleware so it remains a no-op in dev / test
configurations that opt out (default is ``True``).
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config.settings import SecuritySettings

__all__ = ["CsrfMiddleware"]


_DEFAULT_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


class CsrfMiddleware(BaseHTTPMiddleware):
    """Double-submit-cookie CSRF protection middleware."""

    def __init__(self, app: ASGIApp, *, settings: "SecuritySettings") -> None:
        super().__init__(app)
        self._enabled: bool = bool(settings.csrf_enabled)
        self._cookie_name: str = settings.csrf_cookie_name
        self._header_name: str = settings.csrf_header_name
        self._token_bytes: int = int(settings.csrf_token_bytes)
        # Tuple of path prefixes; we use prefix matching so trailing
        # subpaths (e.g. ``/docs/oauth2-redirect``) are covered without
        # listing each one explicitly when the parent prefix is allowed.
        self._allowlist: tuple[str, ...] = tuple(
            settings.csrf_path_allowlist or ()
        )
        self._safe_methods: frozenset[str] = _DEFAULT_SAFE_METHODS

    async def dispatch(self, request: Request, call_next):
        # Short-circuit when CSRF is globally disabled.
        if not self._enabled:
            return await call_next(request)

        method = request.method.upper()
        path = request.url.path
        is_safe_method = method in self._safe_methods
        is_allowlisted = self._is_allowlisted(path)

        # Enforce the cookie/header pair on non-safe methods that aren't
        # in the allowlist.
        if not is_safe_method and not is_allowlisted:
            cookie_value = request.cookies.get(self._cookie_name) or ""
            header_value = request.headers.get(self._header_name) or ""
            # Use ``compare_digest`` so the comparison time does not
            # depend on byte-level prefixes — defends against the
            # extremely-narrow timing-oracle reading of CSRF tokens.
            if (
                not cookie_value
                or not header_value
                or not secrets.compare_digest(cookie_value, header_value)
            ):
                # Return the unified envelope directly. Raising here
                # would propagate through anyio's TaskGroup and arrive
                # at the FastAPI exception handler wrapped in an
                # ExceptionGroup, which the registered ForbiddenError
                # handler would not match.
                return JSONResponse(
                    status_code=403,
                    content={
                        "type": "ForbiddenError",
                        "code": "security.csrf.missing",
                        "message": "CSRF token missing or mismatched",
                    },
                )

        response = await call_next(request)

        # Refresh / install the cookie when it is absent. We do not
        # rotate the token on every successful POST/PUT/etc because the
        # double-submit pattern is already tamper-resistant; rotating
        # adds churn for SPAs that cache the token in memory.
        if not request.cookies.get(self._cookie_name):
            self._set_csrf_cookie(request, response)
        return response

    def _is_allowlisted(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self._allowlist)

    def _set_csrf_cookie(self, request: Request, response: Response) -> None:
        token = secrets.token_urlsafe(self._token_bytes)
        # Secure flag follows the request scheme so test clients on
        # ``http://testserver`` still observe the cookie.
        is_https = request.url.scheme == "https"
        response.set_cookie(
            key=self._cookie_name,
            value=token,
            httponly=False,  # JS must read it to mirror into the header
            secure=is_https,
            samesite="lax",
            path="/",
        )
