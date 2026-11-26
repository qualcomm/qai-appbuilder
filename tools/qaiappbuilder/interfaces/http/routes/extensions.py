# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""User-extension management routes (design doc §7).

* ``GET  /api/extensions``                -- every known extension and its status
* ``POST /api/extensions/reload``         -- unload all, scan ``<data>/extensions`` again
* ``POST /api/extensions/{name}/reload``  -- reload one extension

A reload takes effect at once: tools and skills appear / change / disappear
without restarting. A failing extension is reported in its status (HTTP 200);
it never fails the request. Extension code runs synchronously, so the
operations run in a worker thread.

``503 extensions.unavailable`` when the host is not wired (stripped-down
containers); ``404 extensions.not_found`` for an unknown extension name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
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

    @router.post("/{name}/reload")
    async def reload_one(name: str) -> dict[str, Any]:
        manager = _manager()
        try:
            status = await run_in_threadpool(manager.reload, name)
        except UnknownExtensionError:
            raise NotFoundError("extensions.not_found", "extension", name) from None
        return status_to_wire(status)

    return router
