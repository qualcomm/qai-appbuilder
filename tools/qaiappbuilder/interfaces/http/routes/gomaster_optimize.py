# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""GoMaster External Auto-Optimize proxy routes — /api/gomaster/optimize/*.

BFF proxy for the ``external`` GoMaster link (one-click, cloud-LLM-driven,
NON-conversational model optimization). Backs the frontend「GoMaster 优化」panel:

  * ``POST /api/gomaster/optimize/jobs``                    — upload ONNX + start job
  * ``GET  /api/gomaster/optimize/jobs/{job_id}``           — poll status
  * ``POST /api/gomaster/optimize/jobs/{job_id}/cancel``    — cancel
  * ``GET  /api/gomaster/optimize/jobs/{job_id}/model``     — download optimized model
  * ``GET  /api/gomaster/optimize/jobs/{job_id}/report``    — download report

Relays to the remote GoMaster External API via the injected
:class:`~qai.model_builder.application.gomaster_external_optimize.GomasterExternalOptimizePort`
adapter. The cloud LLM model/api_key are resolved server-side from the user's
selected chat model id (forwarded as the ``model_id`` form field — an id only,
never a credential); the apps bridge maps it to the provider's model id + key.
The browser only talks to this app same-origin.

Layering (interfaces-stays-thin): must NOT import qai.model_builder.infra. The
adapter is composed at the apps root and injected onto
``container.gomaster_external_optimize``; handlers duck-type off it.

internal-only (layer-1 gate): the adapter is ``None`` on external editions /
when gomaster_mode excludes "external" → handlers 404. This module is also
physically excluded from external builds (manifest).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

_log = get_logger(__name__)

# Guard the uploaded model size (defensive; ONNX models are typically < a few
# hundred MB). Rejects absurd uploads before buffering the whole body.
_MAX_MODEL_BYTES = 1024 * 1024 * 1024  # 1 GiB


def _service(container: "Container") -> Any | None:
    return getattr(container, "gomaster_external_optimize", None)


def _require(container: "Container") -> Any:
    svc = _service(container)
    if svc is None:
        raise HTTPException(status_code=404, detail="GoMaster optimize not available")
    return svc


def _map_upstream_error(exc: Exception) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError):
        return HTTPException(status_code=502, detail=f"GoMaster upstream {exc.response.status_code}")
    return HTTPException(status_code=502, detail=f"GoMaster proxy error: {exc}")


def _reject_job_id(job_id: str) -> None:
    """Reject a job_id that could traverse / manipulate the upstream URL."""
    if (
        not job_id
        or "/" in job_id
        or "\\" in job_id
        or ".." in job_id
        or any(ord(ch) < 0x20 for ch in job_id)
    ):
        raise HTTPException(status_code=400, detail="Invalid job_id")


def build_router(*, container: "Container") -> APIRouter:
    """Build the GoMaster external-optimize proxy router (internal-only)."""
    router = APIRouter(prefix="/api/gomaster/optimize", tags=["gomaster"])

    @router.post("/jobs")
    async def create_job(
        model_file: UploadFile = File(..., description="ONNX model (.onnx)"),
        benchmark_requested: bool = Form(default=False),
        run_id: str | None = Form(default=None),
        start_hint: str | None = Form(default=None),
        end_hint: str | None = Form(default=None),
        anchor_hint: str | None = Form(default=None),
        model_id: str | None = Form(default=None),
    ) -> dict[str, Any]:
        _log.info(
            "gomaster.optimize.create.received",
            extra={"filename": model_file.filename, "benchmark": benchmark_requested},
        )
        svc = _require(container)
        filename = model_file.filename or "model.onnx"
        if not filename.lower().endswith(".onnx"):
            raise HTTPException(status_code=400, detail="model_file must be a .onnx file")
        # Read the (already-spooled) upload into bytes. NOTE: we deliberately do
        # NOT hand httpx's ASYNC client a sync file object — httpx's async
        # multipart encoder cannot stream a sync SpooledTemporaryFile and hangs
        # indefinitely (observed: create.body_ready logged, create.forwarded
        # never fired). Bytes are streamed correctly by httpx in async context.
        # A ~100 MB ONNX in memory is acceptable; true streaming would require an
        # async byte-iterator wrapper (future optimization).
        content = await model_file.read()
        if not content:
            raise HTTPException(status_code=400, detail="model_file is empty")
        if len(content) > _MAX_MODEL_BYTES:
            raise HTTPException(status_code=413, detail="model_file too large")
        _log.info(
            "gomaster.optimize.create.body_ready",
            extra={"bytes": len(content)},
        )
        # Launch the (slow, ~1 min for a large model) backend→GoMaster upload in
        # the background and return an upload_id immediately; the frontend polls
        # /jobs/upload-progress/{upload_id} for real byte-level progress, then
        # switches to job polling once the job is created.
        upload_id = svc.start_background_upload(
            model_filename=filename,
            model_bytes=content,
            benchmark_requested=benchmark_requested,
            run_id=run_id,
            start_hint=start_hint,
            end_hint=end_hint,
            anchor_hint=anchor_hint,
            model_id=model_id,
        )
        _log.info("gomaster.optimize.create.upload_started", extra={"upload_id": upload_id})
        return {"upload_id": upload_id, "status": "uploading"}

    @router.get("/jobs/upload-progress/{upload_id}")
    async def upload_progress(upload_id: str) -> dict[str, Any]:
        svc = _require(container)
        _reject_job_id(upload_id)
        progress = svc.get_upload_progress(upload_id)
        if progress is None:
            raise HTTPException(status_code=404, detail="Unknown upload_id")
        return progress

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        svc = _require(container)
        _reject_job_id(job_id)
        try:
            return await svc.get_job(job_id=job_id)
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        svc = _require(container)
        _reject_job_id(job_id)
        try:
            return await svc.cancel_job(job_id=job_id)
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc

    @router.get("/jobs/{job_id}/model")
    async def download_model(job_id: str) -> Response:
        svc = _require(container)
        _reject_job_id(job_id)
        try:
            content, media, filename = await svc.download_model(job_id=job_id)
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc
        return Response(
            content=content,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/jobs/{job_id}/report")
    async def download_report(job_id: str) -> Response:
        svc = _require(container)
        _reject_job_id(job_id)
        try:
            content, media, filename = await svc.download_report(job_id=job_id)
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc
        return Response(
            content=content,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return router


__all__ = ["build_router"]
