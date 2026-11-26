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
* ``GET  /api/extensions/{name}/settings`` -- the settings card (secret values
  are never returned, only ``is_set``)
* ``POST /api/extensions/install/upload`` -- multipart ``file`` (a .zip):
  unpack + validate, return the capability summary with a ``token``. Runs
  no extension code.
* ``POST /api/extensions/install/folder`` -- ``{"path": "..."}``: the same
  for a local folder (the backend runs on the user's machine).
* ``POST /api/extensions/install/{token}`` -- ``{"replace": bool}``: install
  the staged extension and load it; ``409 extensions.exists`` when one with
  that name is installed and ``replace`` is false.
* ``DELETE /api/extensions/install/{token}`` -- discard a staged install
* ``PUT  /api/extensions/{name}/settings`` -- ``{"values": {key: value|null}}``;
  ``400 extensions.invalid_settings`` with ``details.field_errors``

A reload takes effect at once: tools and skills appear / change / disappear
without restarting. A failing extension is reported in its status (HTTP 200);
it never fails the request. Extension code runs synchronously, so the
operations run in a worker thread.

``503 extensions.unavailable`` when the host is not wired (stripped-down
containers); ``404 extensions.not_found`` for an unknown extension name.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, File, Response, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from qai.platform.errors import (
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)
from qai.platform.user_extensions import (
    ExtensionStatus,
    InstallError,
    ManifestError,
    SettingsValidationError,
    UnknownExtensionError,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api._user_extensions_di import UserExtensionManager
    from apps.api.di import Container

__all__ = ["build_router", "status_to_wire"]


class SettingsUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class FolderInstall(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class InstallCommit(BaseModel):
    replace: bool = False


#: Upload cap; the archive is also capped unpacked (installer.MAX_TOTAL_BYTES).
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


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
        "mcp_servers": list(status.mcp_servers),
    }


def build_router(*, container: Container) -> APIRouter:  # noqa: PLR0915 - one closure per route
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
        await manager.sync_runtime()
        return _listing(manager)

    async def _run(name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a (sync) manager operation in a worker thread, then bring the
        async parts of the app (MCP servers) in line with the result."""
        try:
            result = await run_in_threadpool(fn, *args, **kwargs)
        except UnknownExtensionError:
            raise NotFoundError("extensions.not_found", "extension", name) from None
        except ManifestError as exc:
            raise ValidationError("extensions.invalid_manifest", str(exc)) from None
        except SettingsValidationError as exc:
            raise ValidationError(
                "extensions.invalid_settings",
                "some settings are invalid",
                field_errors={k: [v] for k, v in exc.errors.items()},
            ) from None
        except InstallError as exc:
            if exc.code == "extensions.exists":
                raise ConflictError(exc.code, str(exc)) from None
            if exc.code == "extensions.unknown_token":
                raise NotFoundError(exc.code, "staged install", name) from None
            raise ValidationError(exc.code, str(exc)) from None
        await _manager().sync_runtime()
        return result

    # -- install ------------------------------------------------------------

    @router.post("/install/upload")
    async def install_upload(file: UploadFile = File(...)) -> dict[str, Any]:  # noqa: B008 - FastAPI idiom
        manager = _manager()
        data = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValidationError(
                "extensions.too_large", f"the archive is larger than {_MAX_UPLOAD_BYTES} bytes"
            )
        summary: dict[str, Any] = await _run("upload", manager.stage_zip, io.BytesIO(data))
        return summary

    @router.post("/install/folder")
    async def install_folder(body: FolderInstall) -> dict[str, Any]:
        manager = _manager()
        summary: dict[str, Any] = await _run(
            "folder", manager.stage_folder, Path(body.path).expanduser()
        )
        return summary

    @router.post("/install/{token}")
    async def install_commit(token: str, body: InstallCommit) -> dict[str, Any]:
        manager = _manager()
        status = await _run(token, manager.install, token, replace=body.replace)
        return status_to_wire(status)

    @router.delete("/install/{token}", status_code=204)
    async def install_discard(token: str) -> Response:
        manager = _manager()
        if not await _run(token, manager.discard_staged, token):
            raise NotFoundError("extensions.unknown_token", "staged install", token)
        return Response(status_code=204)

    @router.get("/{name}/settings")
    async def get_settings(name: str) -> dict[str, Any]:
        manager = _manager()
        return {"settings": await _run(name, manager.settings, name)}

    @router.put("/{name}/settings")
    async def put_settings(name: str, body: SettingsUpdate) -> dict[str, Any]:
        manager = _manager()
        return {"settings": await _run(name, manager.update_settings, name, body.values)}

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
        # Stop its MCP server processes BEFORE the directory is deleted (a
        # running process can keep Windows from deleting files it uses).
        await _run(name, manager.unload, name)
        await _run(name, manager.remove, name, purge_data=purge_data)
        return Response(status_code=204)

    return router
