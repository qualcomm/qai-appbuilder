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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from qai.platform.logging import get_logger
from qai.platform.user_extensions import RouteRequest, RouteResponse, call_handler

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

__all__ = ["build_router"]

_LOG = get_logger("interfaces.http.extension_routes")
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
_MAX_BODY = 10 * 1024 * 1024
#: Never handed to extension code: the app's own credentials.
_HIDDEN_HEADERS = frozenset({"cookie", "authorization", "x-csrf-token", "proxy-authorization"})

#: ``/api/ext/<name>/_ui/<file>``: the extension's front-end (route A). ``_ui`` is
#: reserved (route paths cannot start with ``_``; segments are ``[a-z0-9]...``).
_UI_PREFIX = "_ui/"
#: Explicit types: the OS registry can map ``.js`` to ``text/plain``, which the
#: browser refuses for ES modules (design doc A.3).
_UI_TYPES: dict[str, str] = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain",
    ".map": "application/json",
}
_UI_MAX_FILE_BYTES = 5 * 1024 * 1024  # one file; a bundler's single-file output fits easily


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "ExtensionRouteError", "code": code, "message": message},
        headers=extra.get("headers"),
    )


def build_router(*, container: Container) -> APIRouter:
    router = APIRouter(prefix="/api/ext", tags=["extensions"])

    @router.api_route("/{extension}/{path:path}", methods=_METHODS)
    async def dispatch(  # noqa: PLR0911 - one exit per HTTP outcome
        extension: str, path: str, request: Request
    ) -> Response:
        manager = getattr(container, "user_extensions", None)
        host = getattr(manager, "host", None)
        if host is None:
            return _error(503, "extensions.unavailable", "user extensions are not available")
        method = request.method.upper()
        if path == _UI_PREFIX.rstrip("/") or path.startswith(_UI_PREFIX):
            return _serve_ui_file(host, extension, path[len(_UI_PREFIX) :], method)
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


def _serve_ui_file(  # noqa: PLR0911 - one exit per HTTP outcome
    host: Any, extension: str, rel: str, method: str
) -> Response:
    """One file of the extension's front-end directory (GET / HEAD only).

    Only the directory the manifest names (``ui.module``'s parent) is served;
    the resolved file must lie inside it (``..``, absolute paths, links out of
    it are refused with the same 404 as a missing file, so nothing about the
    rest of the extension can be probed).
    """
    if method not in ("GET", "HEAD"):
        return _error(
            405, "extensions.method_not_allowed", "only GET", headers={"Allow": "GET, HEAD"}
        )
    not_found = _error(404, "extensions.route_not_found", "no such file")
    directory: Path | None = host.ui_module_dir(extension)
    if directory is None or not rel or "\x00" in rel:
        return not_found
    try:
        base = directory.resolve()
        target = (directory / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return not_found
        media = _UI_TYPES.get(target.suffix.lower())
        if media is None:
            return not_found  # only known, harmless types are served
        if target.stat().st_size > _UI_MAX_FILE_BYTES:
            return _error(413, "extensions.file_too_large", "file is too large")
    except (OSError, ValueError):
        return not_found
    # Revalidate every time (ETag / Last-Modified come with FileResponse): the
    # module's own relative imports carry no ?v=, so they must not be cached
    # blindly. The entry module URL itself is versioned (?v=), which is what
    # makes the browser load new code after a reload.
    return FileResponse(
        target,
        media_type=media,
        headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"},
    )


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
