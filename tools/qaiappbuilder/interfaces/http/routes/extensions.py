# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""User-extension management routes (design doc §7).

* ``GET  /api/extensions``                -- every known extension and its status
* ``POST /api/extensions/reload``         -- unload all, scan ``<data>/extensions`` again
* ``POST /api/extensions/{name}/reload``  -- reload one extension
* ``POST /api/extensions/{name}/enable``  -- switch on (persisted) and load
* ``POST /api/extensions/{name}/disable`` -- switch off (persisted) and unload
* ``DELETE /api/extensions/{name}?purge_data=true`` -- uninstall: delete the
  directory; ``purge_data`` also deletes its data directory, secrets, settings

A reload takes effect at once: tools and skills appear / change / disappear
without restarting. A failing extension is reported in its status (HTTP 200);
it never fails the request. Extension code runs synchronously, so the
operations run in a worker thread.

``503 extensions.unavailable`` when the host is not wired (stripped-down
containers); ``404 extensions.not_found`` for an unknown extension name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Response
from starlette.concurrency import run_in_threadpool

from qai.platform.errors import ExternalServiceError, NotFoundError
from qai.platform.user_extensions import ExtensionStatus, UnknownExtensionError

if TYPE_CHECKING:  # pragma: no cover
    from apps.api._user_extensions_di import UserExtensionManager
    from apps.api.di import Container

__all__ = ["build_router", "status_to_wire"]


def status_to_wire(status: ExtensionStatus) -> dict[str, Any]:
    """One extension's status as the HTTP API returns it."""
    return {
        "name": status.name,
        "version": status.version,
        "state": status.state,
        "error": status.error,
        "tools": list(status.tools),
        "skills": list(status.skills),
        "warnings": list(status.warnings),
        "prompts": dict(status.prompts),
    }


def build_router(*, container: Container) -> APIRouter:
    router = APIRouter(prefix="/api/extensions", tags=["extensions"])

    def _manager() -> UserExtensionManager:
        manager: UserExtensionManager | None = getattr(container, "user_extensions", None)
        if manager is None:
            raise ExternalServiceError(
                "extensions.unavailable",
                "user extensions are not available in this build",
                service="extensions",
            )
        return manager

    def _listing(manager: UserExtensionManager) -> dict[str, Any]:
        return {
            "directory": str(manager.host.scan_root),
            "extensions": [status_to_wire(s) for s in manager.statuses],
        }

    @router.get("")
    async def list_extensions() -> dict[str, Any]:
        return _listing(_manager())

    @router.post("/reload")
    async def reload_all() -> dict[str, Any]:
        manager = _manager()
        await run_in_threadpool(manager.reload_all)
        return _listing(manager)

    async def _run(name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await run_in_threadpool(fn, *args, **kwargs)
        except UnknownExtensionError:
            raise NotFoundError("extensions.not_found", "extension", name) from None

    @router.post("/{name}/reload")
    async def reload_one(name: str) -> dict[str, Any]:
        manager = _manager()
        return status_to_wire(await _run(name, manager.reload, name))

    @router.post("/{name}/enable")
    async def enable(name: str) -> dict[str, Any]:
        manager = _manager()
        return status_to_wire(await _run(name, manager.set_enabled, name, True))

    @router.post("/{name}/disable")
    async def disable(name: str) -> dict[str, Any]:
        manager = _manager()
        return status_to_wire(await _run(name, manager.set_enabled, name, False))

    @router.delete("/{name}", status_code=204)
    async def remove(name: str, purge_data: bool = False) -> Response:
        manager = _manager()
        await _run(name, manager.remove, name, purge_data=purge_data)
        return Response(status_code=204)

    return router
