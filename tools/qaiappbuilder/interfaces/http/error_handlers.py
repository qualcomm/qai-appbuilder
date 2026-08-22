# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Unified FastAPI exception handlers for the qai HTTP surface.

Maps :mod:`qai.platform.errors` exception hierarchy to HTTP status codes
and a single, stable JSON envelope shape::

    {
      "type":    "<ExceptionClassName>",
      "code":    "<machine_readable_code>",
      "message": "<human_readable>",
      "details": { ... }   # optional
    }

The shape is the canonical output of :meth:`QaiError.to_dict`. Routes
must NOT roll their own JSON error responses; let the corresponding
``QaiError`` propagate and this handler will translate it.

Status code mapping (S3-sub-agent-spec.md §4.3):

    ValidationError              -> 400
    UnauthorizedError            -> 401
    ForbiddenError               -> 403
    NotFoundError                -> 404
    ConflictError                -> 409
    PreconditionFailedError      -> 412
    RateLimitedError             -> 429   (also sets Retry-After header)
    DomainError (other)          -> 422
    InfrastructureError (any)    -> 503
    QaiError (unknown subclass)  -> 500

Two non-``QaiError`` cases are also bridged to the unified envelope:

    fastapi.exceptions.RequestValidationError -> 400
    Exception (anything else)                 -> 500   (traceback NOT
        leaked in the body; it is logged via structlog instead)

Registered once from ``apps.api.main.create_app`` via
:func:`register_error_handlers`. PR-031..PR-036 must NOT re-register
or re-implement; they reuse this module.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from qai.platform.errors import (
    ConflictError,
    DomainError,
    ForbiddenError,
    InfrastructureError,
    NotFoundError,
    PreconditionFailedError,
    QaiError,
    RateLimitedError,
    UnauthorizedError,
    ValidationError,
)
from qai.platform.logging import get_logger

logger = get_logger(__name__)


# ---- Status code mapping --------------------------------------------------

# Order matters: more specific subclasses listed first. Looked up by
# ``isinstance`` in :func:`_status_for`.
_STATUS_MAP: tuple[tuple[type[QaiError], int], ...] = (
    (ValidationError, 400),
    (UnauthorizedError, 401),
    (ForbiddenError, 403),
    (NotFoundError, 404),
    (ConflictError, 409),
    (PreconditionFailedError, 412),
    (RateLimitedError, 429),
    # DomainError must come AFTER any application subclass it might shadow,
    # but no application subclass is also a DomainError so this ordering
    # is safe; kept here for clarity.
    (DomainError, 422),
    (InfrastructureError, 503),
)


def _status_for(exc: QaiError) -> int:
    """Return the HTTP status code for a ``QaiError`` instance.

    Falls back to 500 for unknown ``QaiError`` subclasses.
    """
    for cls, status in _STATUS_MAP:
        if isinstance(exc, cls):
            return status
    return 500


# ---- Handlers -------------------------------------------------------------


async def _handle_qai_error(request: Request, exc: QaiError) -> JSONResponse:
    """Translate any ``QaiError`` to the unified JSON envelope."""
    status = _status_for(exc)
    body = exc.to_dict()
    headers: dict[str, str] = {}

    # 429 contract: surface Retry-After (seconds, integer-rounded up).
    if isinstance(exc, RateLimitedError):
        retry_after_s = (exc.details or {}).get("retry_after_s")
        if isinstance(retry_after_s, (int, float)) and retry_after_s >= 0:
            headers["Retry-After"] = str(int(math.ceil(retry_after_s)))

    logger.info(
        "http.qai_error",
        path=str(request.url.path),
        method=request.method,
        type=body["type"],
        code=body["code"],
        status=status,
    )
    return JSONResponse(status_code=status, content=body, headers=headers or None)


async def _handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Rewrite FastAPI's default 422 to the unified 400 envelope.

    ``details.field_errors`` mimics ``ValidationError.field_errors`` so
    clients have a single shape to parse.
    """
    field_errors: dict[str, list[str]] = {}
    for err in exc.errors():
        loc_parts: list[str] = []
        for part in err.get("loc", ()):
            loc_parts.append(str(part))
        field = ".".join(loc_parts) if loc_parts else "<root>"
        msg = str(err.get("msg", "invalid"))
        field_errors.setdefault(field, []).append(msg)

    body: dict[str, Any] = {
        "type": "RequestValidationError",
        "code": "request.validation",
        "message": "Request payload failed validation.",
        "details": {"field_errors": field_errors},
    }
    logger.info(
        "http.request_validation_error",
        path=str(request.url.path),
        method=request.method,
        field_count=len(field_errors),
    )
    return JSONResponse(status_code=400, content=body)


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for non-``QaiError`` exceptions.

    Body never includes the traceback; the full chain is logged via
    structlog (``logger.exception``) so operators retain triage data.
    """
    body = {
        "type": "InternalServerError",
        "code": "internal.unexpected",
        "message": "An unexpected internal error occurred.",
    }
    logger.exception(
        "http.unexpected_error",
        path=str(request.url.path),
        method=request.method,
        exc_type=type(exc).__name__,
    )
    return JSONResponse(status_code=500, content=body)


# ---- Public registration --------------------------------------------------


def register_error_handlers(app: FastAPI) -> None:
    """Register the unified error handlers on ``app``.

    Idempotency is FastAPI's responsibility: registering the same
    handler twice on the same app is a programming error. ``create_app``
    must call this exactly once.
    """
    # ``QaiError`` must be registered BEFORE the bare ``Exception``
    # fallback so the more specific handler is preferred.
    app.add_exception_handler(QaiError, _handle_qai_error)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        _handle_request_validation_error,  # type: ignore[arg-type]
    )
    app.add_exception_handler(Exception, _handle_unexpected)


__all__ = ["register_error_handlers"]
