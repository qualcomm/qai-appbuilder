# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP routes contributed by user extensions: ``/api/ext/<extension>/<path>``.

One catch-all route dispatches to whatever the loaded extension registered
(``ctx.register_route``), so routes appear / disappear with the extension
without touching FastAPI's route table (which cannot be changed safely at
runtime). Everything app-wide still applies because these are ordinary
``/api`` requests: login (AuthMiddleware), CSRF for non-GET
(CsrfMiddleware), the request-size limits, the error envelope.

* unknown extension / path -> ``404 extensions.route_not_found``
* path exists, other method -> ``405`` with ``Allow``
* handler returns a JSON value -> ``200`` JSON; ``None`` -> ``204``;
  :class:`RouteResponse` -> exactly that
* handler raises -> ``500 extensions.route_failed`` with the exception text
  (logged with traceback); it never takes the server down
* request body > 10 MB -> ``413``
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from qai.platform.logging import get_logger
from qai.platform.user_extensions import RouteRequest, RouteResponse, call_handler

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

__all__ = ["build_router"]

_LOG = get_logger("interfaces.http.extension_routes")
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
_MAX_BODY = 10 * 1024 * 1024
#: Never handed to extension code: the app's own credentials.
_HIDDEN_HEADERS = frozenset({"cookie", "authorization", "x-csrf-token", "proxy-authorization"})


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "ExtensionRouteError", "code": code, "message": message},
        headers=extra.get("headers"),
    )


def build_router(*, container: Container) -> APIRouter:
    router = APIRouter(prefix="/api/ext", tags=["extensions"])

    @router.api_route("/{extension}/{path:path}", methods=_METHODS)
    async def dispatch(extension: str, path: str, request: Request) -> Response:
        manager = getattr(container, "user_extensions", None)
        host = getattr(manager, "host", None)
        if host is None:
            return _error(503, "extensions.unavailable", "user extensions are not available")
        method = request.method.upper()
        found = host.route_handler(extension, method, path)
        if found is None:
            allowed = host.route_methods(extension, path)
            if allowed:
                return _error(
                    405,
                    "extensions.method_not_allowed",
                    f"{method} is not allowed here",
                    headers={"Allow": ", ".join(sorted(allowed))},
                )
            return _error(
                404, "extensions.route_not_found", f"no route {method} /api/ext/{extension}/{path}"
            )
        handler, params = found

        raw = await request.body()
        if len(raw) > _MAX_BODY:
            return _error(413, "extensions.body_too_large", "request body is too large")
        body: Any = None
        if raw:
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                body = None
        req = RouteRequest(
            method=method,
            path=path.strip("/"),
            params=params,
            query=dict(request.query_params),
            body=body,
            headers={
                k.lower(): v for k, v in request.headers.items() if k.lower() not in _HIDDEN_HEADERS
            },
        )
        try:
            result = await call_handler(handler, req)
        except Exception as exc:  # noqa: BLE001 - extension code; never crash the server
            _LOG.warning(
                "user_extensions.route_failed", extension=extension, path=path, exc_info=True
            )
            return _error(500, "extensions.route_failed", f"{type(exc).__name__}: {exc}")
        return _to_response(result, extension=extension, path=path)

    return router


def _to_response(result: Any, *, extension: str, path: str) -> Response:
    try:
        if result is None:
            return Response(status_code=204)
        if isinstance(result, RouteResponse):
            headers = dict(result.headers)
            if isinstance(result.body, (bytes, str)):
                return Response(
                    content=result.body,
                    status_code=result.status,
                    media_type=result.content_type,
                    headers=headers,
                )
            if result.body is None:
                return Response(status_code=result.status, headers=headers)
            return JSONResponse(content=result.body, status_code=result.status, headers=headers)
        return JSONResponse(content=result)
    except (TypeError, ValueError) as exc:
        _LOG.warning("user_extensions.route_bad_result", extension=extension, path=path)
        return _error(
            500,
            "extensions.route_failed",
            f"the handler returned something not serialisable: {exc}",
        )
