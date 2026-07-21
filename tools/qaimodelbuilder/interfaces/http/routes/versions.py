# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Version management HTTP routes.

**GenieAPIService download center** (V1 ``backend/version_manager.py``
parity, consumed by the rewritten Downloads view): ``GET /api/versions``
(list release versions), ``POST /api/versions/download`` (SSE stream),
the download-center service install is ``POST /api/versions/service-install``,
``DELETE /api/versions/install/{version}``,
``DELETE /api/versions/download/{version}``,
``GET /api/versions/local-status``.

The download-center routes delegate to the ``service_release`` bounded
context (``container.service_release``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from interfaces.http.routes._sse import sse_data, sse_done
from qai.service_release.application.use_cases import (
    InstallServiceCommand,
    StartServiceDownloadCommand,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---------------------------------------------------------------------------
# Download-center DTOs (V1 parity)
# ---------------------------------------------------------------------------


class ServiceVersionsResponse(BaseModel):
    versions: list[dict]


class ServiceDownloadRequest(BaseModel):
    version: str
    download_url: str
    checksum_sha256: str = ""
    task_id: str = ""


class ServiceInstallRequestBody(BaseModel):
    save_path: str
    version: str = ""


class DownloadSettingsBody(BaseModel):
    save_dir: str = ""
    version_list_url: str = ""
    catalog_url: str = ""
    fetch_timeout_seconds: int = 15
    download_timeout_seconds: int = 300
    ssl_verify: bool = False


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    router = APIRouter(prefix="/api/versions", tags=["versions"])

    # ── GenieAPIService download center (V1 parity) ───────────────────

    @router.get("", response_model=ServiceVersionsResponse)
    async def list_versions() -> ServiceVersionsResponse:
        versions = (
            await container.service_release.list_service_versions_use_case.execute()
        )
        return ServiceVersionsResponse(versions=[v.to_wire() for v in versions])

    @router.post("/download")
    async def download_version(body: ServiceDownloadRequest) -> StreamingResponse:
        command = StartServiceDownloadCommand(
            version=body.version,
            download_url=body.download_url,
            checksum_sha256=body.checksum_sha256,
            task_id=body.task_id,
        )
        iterator = container.service_release.stream_service_download_use_case.execute(
            command
        )

        async def _stream():
            async for progress in iterator:
                yield sse_data(progress.to_wire())
            yield sse_done()

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/service-install")
    async def install_service(body: ServiceInstallRequestBody) -> dict:
        result = await container.service_release.install_service_use_case.execute(
            InstallServiceCommand(save_path=body.save_path, version=body.version)
        )
        return result.to_wire()

    @router.delete("/install/{version}")
    async def delete_installed_version(
        version: str, stop_running: bool = False
    ) -> dict:
        # ``stop_running`` (additive query param, default False = legacy
        # behaviour): when True, gracefully stop a running GenieAPIService for
        # this version before deleting so its loaded Genie.dll lock is released
        # (avoids WinError 5). The frontend sets it after the user confirms the
        # "service is running and will be stopped" dialog.
        return await container.service_release.delete_installed_service_use_case.execute(
            version=version, stop_running=stop_running
        )

    @router.get("/install/{version}/running")
    async def installed_version_running(version: str) -> dict:
        # Lets the UI/CLI warn "the service is running and will be stopped"
        # before issuing the delete (file-level real-state probe).
        running = await (
            container.service_release.delete_installed_service_use_case.is_running(
                version=version
            )
        )
        return {"version": version, "running": running}

    @router.delete("/download/{version}")
    async def delete_downloaded_version(version: str) -> dict:
        return await container.service_release.delete_downloaded_service_use_case.execute(
            version=version
        )

    @router.get("/local-status")
    async def versions_local_status() -> dict:
        status = (
            await container.service_release.get_versions_local_status_use_case.execute()
        )
        return status.to_wire()

    # ── Download settings (forge_config download section) ─────────────

    @router.get("/download-settings")
    async def get_download_settings() -> dict:
        settings = (
            await container.service_release.get_download_settings_use_case.execute()
        )
        return settings.to_wire()

    @router.put("/download-settings")
    async def update_download_settings(body: DownloadSettingsBody) -> dict:
        from qai.service_release.domain.value_objects import DownloadSettings

        settings = await container.service_release.update_download_settings_use_case.execute(
            DownloadSettings(
                save_dir=body.save_dir,
                version_list_url=body.version_list_url,
                catalog_url=body.catalog_url,
                fetch_timeout_seconds=body.fetch_timeout_seconds,
                download_timeout_seconds=body.download_timeout_seconds,
                ssl_verify=body.ssl_verify,
            )
        )
        return settings.to_wire()

    return router
