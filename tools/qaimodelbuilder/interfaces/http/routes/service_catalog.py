# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Service-catalog HTTP routes — hardware-grouped model download center.

V1 ``backend/model_catalog_manager.py`` parity, exposed under a **new**
``/api/service-catalog`` prefix so it does not collide with the frozen
``model_catalog`` context routes at ``/api/model-catalog`` (entries /
cloud-models / providers, consumed by chat + channels).

Routes:
- ``GET    /api/service-catalog``                       — list models (npu/gpu/cpu + variants)
- ``POST   /api/service-catalog/download``              — SSE stream
- ``POST   /api/service-catalog/install``               — unzip into models/
- ``DELETE /api/service-catalog/install/{model_id}``    — delete (?delete_zip=)
- ``GET    /api/service-catalog/local-status``          — downloaded/installed scan
- ``GET    /api/service-catalog/download-status``       — in-flight downloads (M-4)

Delegates to ``container.service_release``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from interfaces.http.routes._sse import sse_data, sse_done
from qai.service_release.application.use_cases import (
    InstallModelCommand,
    StartModelDownloadCommand,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


class CatalogModelsResponse(BaseModel):
    models: list[dict]


class ModelDownloadRequest(BaseModel):
    model_id: str
    download_url: str
    checksum_sha256: str = ""
    #: Optional per-platform cancellation key (``variant_id``). When absent the
    #: backend uses ``model_id`` (single-platform). The model's save dir is
    #: always keyed by ``model_id`` so a model's variants share one dir.
    task_id: str = ""


class ModelInstallRequestBody(BaseModel):
    save_path: str
    model_id: str = ""
    install_dir: str = ""
    #: Selected platform variant id (e.g. ``qwen3-8b-8480-qnn2.44``). Persisted
    #: as an install marker so the Installed pill can show the platform label
    #: ("Installed · Snapdragon X2 Elite"), mirroring ServiceVersionCard.
    variant_id: str = ""


def build_router(*, container: "Container") -> APIRouter:
    router = APIRouter(prefix="/api/service-catalog", tags=["service_catalog"])

    @router.get("", response_model=CatalogModelsResponse)
    async def list_models() -> CatalogModelsResponse:
        models = (
            await container.service_release.list_catalog_models_use_case.execute()
        )
        return CatalogModelsResponse(models=[m.to_wire() for m in models])

    @router.post("/download")
    async def download_model(body: ModelDownloadRequest) -> StreamingResponse:
        command = StartModelDownloadCommand(
            model_id=body.model_id,
            download_url=body.download_url,
            checksum_sha256=body.checksum_sha256,
            task_id=body.task_id,
        )
        iterator = container.service_release.stream_model_download_use_case.execute(
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

    @router.post("/install")
    async def install_model(body: ModelInstallRequestBody) -> dict:
        result = await container.service_release.install_model_use_case.execute(
            InstallModelCommand(
                save_path=body.save_path,
                model_id=body.model_id,
                install_dir=body.install_dir,
                variant_id=body.variant_id,
            )
        )
        return result.to_wire()

    @router.delete("/install/{model_id:path}")
    async def delete_model(
        model_id: str, delete_zip: bool = Query(default=True)
    ) -> dict:
        return await container.service_release.delete_model_use_case.execute(
            model_id=model_id, delete_zip=delete_zip
        )

    @router.get("/local-status")
    async def models_local_status() -> dict:
        status = (
            await container.service_release.get_models_local_status_use_case.execute()
        )
        return status.to_wire()

    @router.get("/download-status")
    async def download_status() -> dict:
        """Snapshot of currently in-flight downloads (M-4, State-Truth).

        V1 parity: ``GET /api/model-catalog/download-status`` let a
        reconnecting client re-query progress after an SSE disconnect. The
        snapshot comes straight from the download engine's REAL in-flight
        tasks (``active_downloads``) — not an optimistic cache — so a task
        that has finished/cancelled no longer appears (铁律 1).
        """
        engine = getattr(container.service_release, "download_engine", None)
        active_fn = getattr(engine, "active_downloads", None)
        if not callable(active_fn):
            return {"downloads": []}
        return {"downloads": [p.to_wire() for p in active_fn()]}

    return router
