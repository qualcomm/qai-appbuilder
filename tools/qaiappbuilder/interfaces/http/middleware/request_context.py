# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Request-ID propagation middleware (E-1, V2 robustness enhancement).

Binds a per-request ``request_id`` into the structured-logging context
(:func:`qai.platform.logging.bind_request_id`) for the duration of the
request so every log record emitted while handling it carries the same
``request_id`` (merged by the ``_add_request_context`` structlog
processor). The id is also echoed back to the caller in the
``X-Request-ID`` response header for client-side correlation.

Resolution rules
----------------
* If the inbound request carries a non-empty ``X-Request-ID`` header,
  its value is reused verbatim (stripped, and truncated to
  :data:`_MAX_REQUEST_ID_LEN` characters to bound log/header size and
  defend against header-injection of unbounded values).
* Otherwise a fresh ``uuid4().hex`` is generated.

This is the HTTP-layer counterpart to the already-present platform
primitives ``bind_request_id`` (``platform/logging/context.py``) and the
``_add_request_context`` log processor (``platform/logging/config.py``).
Per ``.importlinter`` contract ``interfaces-stays-thin`` this module only
depends on ``starlette`` and ``qai.platform``.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from qai.platform.logging.context import bind_request_id

__all__ = ["RequestContextMiddleware", "REQUEST_ID_HEADER"]


#: Canonical request-id header name (request inbound + response echo).
REQUEST_ID_HEADER = "X-Request-ID"

#: Upper bound on accepted inbound request-id length (chars). Values
#: longer than this are truncated so a caller cannot bloat log lines /
#: response headers with an unbounded id.
_MAX_REQUEST_ID_LEN = 128


def _resolve_request_id(request: Request) -> str:
    """Return the request id to use: inbound header (sanitised) or fresh.

    An inbound ``X-Request-ID`` is stripped of surrounding whitespace and
    truncated to :data:`_MAX_REQUEST_ID_LEN`. When absent or empty after
    stripping, a fresh ``uuid4().hex`` is generated.
    """
    raw = request.headers.get(REQUEST_ID_HEADER)
    if raw is not None:
        candidate = raw.strip()
        if candidate:
            return candidate[:_MAX_REQUEST_ID_LEN]
    return uuid.uuid4().hex


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a per-request ``request_id`` and echo it back to the client."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = _resolve_request_id(request)
        with bind_request_id(request_id):
            response = await call_next(request)
        # Echo back so the client (and any upstream proxy) can correlate.
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
