# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder — weight-download routes (``/weights/download`` family).

Backs the "download model weights" feature: start a built-in model's
weight download through the shared multi-threaded aria2c engine, stream
live progress over SSE, and cancel an in-flight download.

Routes
------
* ``POST   /api/app-builder/weights/download``                  — start
* ``GET    /api/app-builder/weights/download/{job_id}/progress`` — SSE
* ``DELETE /api/app-builder/weights/download/{job_id}``          — cancel

SSE wire contract (mirrors model_catalog via the shared translator)
-------------------------------------------------------------------
The progress endpoint returns ``text/event-stream`` frames produced by
:func:`interfaces.http.sse.stream_progress_frames` — ``event: progress``
per snapshot, a terminal ``event: done`` (emitted only AFTER the use case
extracts the archive into ``models/<id>/``), and ``event: error`` carrying
the ``QaiError`` envelope when extraction fails.

The route stays thin (Contract 4): it reaches the use case through
``container.app_builder.download_model_weights_use_case`` and never imports
app_builder infrastructure directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from interfaces.http.sse import stream_progress_frames

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


class WeightDownloadRequest(BaseModel):
    """Start-download request body."""

    model_id: str = Field(..., min_length=1, max_length=128)


class WeightDownloadJobResponse(BaseModel):
    """Start-download response — the created job id."""

    job_id: str


def register(router: APIRouter, *, container: "Container") -> None:
    """Mount the weight-download routes onto ``router``."""

    def _services() -> Any:
        return container.app_builder

    def _require_uc() -> Any:
        """Return the weight-download use case, or a clean 503 when it is not
        wired (a lean container where ``repo_root`` / the pack shared dir did
        not resolve — the DI leaves the use case ``None`` in that case).
        Guards against a raw ``AttributeError`` → ugly 500."""
        uc = _services().download_model_weights_use_case
        if uc is None:
            raise HTTPException(
                status_code=503,
                detail="weight-download use case not available",
            )
        return uc

    @router.post(
        "/weights/download",
        response_model=WeightDownloadJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_weight_download(
        body: WeightDownloadRequest,
    ) -> WeightDownloadJobResponse:
        uc = _require_uc()
        job_id = await uc.start(body.model_id)
        return WeightDownloadJobResponse(job_id=job_id)

    @router.get("/weights/download/{job_id}/progress")
    async def stream_weight_download_progress(job_id: str) -> StreamingResponse:
        # ``stream`` may raise (unknown job) BEFORE the iterator begins, in
        # which case the global handler produces a clean 404 envelope. Once
        # the iterator is awaited we have committed to a 200 response and any
        # further failure (e.g. extraction) is surfaced as an SSE error frame.
        uc = _require_uc()
        iterator = await uc.stream(job_id)
        return StreamingResponse(
            stream_progress_frames(iterator),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.delete(
        "/weights/download/{job_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def cancel_weight_download(job_id: str) -> Response:
        uc = _require_uc()
        await uc.cancel(job_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ---- weights download (WS) ----------------------------------------

    async def _safe_send_json(ws: WebSocket, data: dict) -> bool:  # type: ignore[type-arg]
        try:
            await ws.send_json(data)
            return True
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            return False

    @router.websocket("/weights/download/{job_id}/ws")
    async def weights_download_ws(websocket: WebSocket, job_id: str) -> None:
        """WebSocket variant of the weight-download progress SSE endpoint.

        Replays progress frames from the ProgressBroadcaster, falling
        back to a fresh use-case stream when no broadcaster entry exists
        (single-subscriber mode). Wire format:
        ``{"type": "frame", "event": "<name>", "payload": {...}}``
        """
        await websocket.accept()
        progress_broadcaster = getattr(_services(), "progress_broadcaster", None)

        # If a broadcaster entry exists, replay from it (multi-subscriber).
        if progress_broadcaster is not None and progress_broadcaster.get(job_id) is not None:
            try:
                async for event_name, payload in progress_broadcaster.replay(job_id):
                    if not await _safe_send_json(
                        websocket,
                        {"type": "frame", "event": event_name, "payload": payload},
                    ):
                        return
                await _safe_send_json(websocket, {"type": "done"})
                try:
                    await websocket.close(code=1000)
                except (RuntimeError, WebSocketDisconnect):
                    pass
            except WebSocketDisconnect:
                return
            except Exception as exc:
                await _safe_send_json(
                    websocket,
                    {"type": "error", "code": "internal", "message": str(exc)},
                )
                try:
                    await websocket.close(code=1011)
                except (RuntimeError, WebSocketDisconnect):
                    pass
            return

        # Fallback: consume the use-case iterator directly (single subscriber).
        try:
            uc = _require_uc()
        except HTTPException as exc:
            await _safe_send_json(
                websocket,
                {"type": "error", "code": "unavailable", "message": str(exc.detail)},
            )
            try:
                await websocket.close(code=4503)
            except (RuntimeError, WebSocketDisconnect):
                pass
            return

        try:
            iterator = await uc.stream(job_id)
        except Exception as exc:
            await _safe_send_json(
                websocket,
                {"type": "error", "code": "not_found", "message": str(exc)},
            )
            try:
                await websocket.close(code=4404)
            except (RuntimeError, WebSocketDisconnect):
                pass
            return

        try:
            async for snapshot in iterator:
                payload = {
                    "bytes_downloaded": snapshot.bytes_downloaded,
                    "total_bytes": snapshot.total_bytes,
                    "speed_bps": snapshot.speed_bps,
                    "eta_seconds": snapshot.eta_seconds,
                    "percent": snapshot.percent,
                    "is_complete": snapshot.is_complete,
                }
                if not await _safe_send_json(
                    websocket,
                    {"type": "frame", "event": "progress", "payload": payload},
                ):
                    return
            await _safe_send_json(websocket, {"type": "done"})
            try:
                await websocket.close(code=1000)
            except (RuntimeError, WebSocketDisconnect):
                pass
        except WebSocketDisconnect:
            return
        except Exception as exc:
            await _safe_send_json(
                websocket,
                {"type": "error", "code": "internal", "message": str(exc)},
            )
            try:
                await websocket.close(code=1011)
            except (RuntimeError, WebSocketDisconnect):
                pass
