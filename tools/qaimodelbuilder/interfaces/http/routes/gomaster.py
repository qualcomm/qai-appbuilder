# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""GoMaster online REST/stream capability-proxy routes — /api/gomaster/*.

These BFF proxy routes back the frontend「GoMaster 在线」panels (graph optimize
diff, real-time QNN logs, model graph, benchmark, artifacts download). They
relay to the remote GoMaster server via the injected
:class:`~qai.model_builder.application.gomaster_graph_service.GomasterGraphServicePort`
adapter (which injects the server-side Bearer token) so the browser only ever
talks to this app same-origin (no CORS, no credential in the browser).

* JSON relays return the upstream JSON verbatim.
* ``run/stream`` wraps the adapter's raw SSE byte iterator in a
  ``StreamingResponse(media_type="text/event-stream")`` — the GoMaster event
  stream is passed through byte-for-byte so the frontend log component consumes
  the same shape GoMaster's own UI does.
* artifact / output downloads stream the file back with a
  ``Content-Disposition`` header.

Layering (import-linter ``interfaces-stays-thin``): this module must NOT import
``qai.model_builder.infrastructure``. The adapter is composed at the apps/api
root and injected onto ``container.gomaster_service``; handlers consume it by
duck-typing.

internal-only (four-layer defence, layer 1 — runtime gate): the adapter is
``None`` on external editions (built only when ``settings.is_internal``), so
every handler short-circuits to 404. This whole module is also physically
excluded from external builds (``manifest.toml [exclude]``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


class AutoOptimizeStartRequest(BaseModel):
    """Body for ``POST /api/gomaster/session/{sid}/auto-optimize``."""

    payload: dict[str, Any] = {}


class RunQnnRequest(BaseModel):
    """Body for ``POST /api/gomaster/qnn/run/stream`` (relayed to GoMaster)."""

    payload: dict[str, Any] = {}


class BenchmarkPairRequest(BaseModel):
    """Body for ``POST /api/gomaster/qnn/benchmark-pair``."""

    payload: dict[str, Any] = {}


def _service(container: "Container") -> Any | None:
    """Return the injected GoMaster REST-proxy adapter, or ``None`` (external)."""
    return getattr(container, "gomaster_service", None)


def _require(container: "Container") -> Any:
    svc = _service(container)
    if svc is None:
        raise HTTPException(status_code=404, detail="GoMaster not available")
    return svc


def _map_upstream_error(exc: Exception) -> HTTPException:
    """Map an upstream httpx error to a client-facing HTTPException."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return HTTPException(status_code=502, detail=f"GoMaster upstream {code}")
    return HTTPException(status_code=502, detail=f"GoMaster proxy error: {exc}")


def _reject_path_segment(value: str, *, field: str) -> None:
    """Reject a path-param value that could traverse / manipulate the upstream URL.

    These segments (``session_id`` / ``artifact_id`` / ``filename``) are joined
    verbatim into the remote GoMaster URL by the BFF adapter, which calls with a
    PRIVILEGED Bearer token — so letting a client shape arbitrary upstream
    sub-paths is a confused-deputy / SSRF-flavoured risk. Reject anything with a
    path separator, parent-dir token, or control char (400).
    """
    if (
        not value
        or "/" in value
        or "\\" in value
        or ".." in value
        or any(ord(ch) < 0x20 for ch in value)
    ):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")


def build_router(*, container: "Container") -> APIRouter:
    """Build the GoMaster REST/stream capability-proxy router (internal-only)."""
    router = APIRouter(prefix="/api/gomaster", tags=["gomaster"])

    # ── auto-optimize (graph optimize job + before/after diff) ──────────────
    @router.post("/session/{session_id}/auto-optimize")
    async def start_auto_optimize(
        session_id: str, body: AutoOptimizeStartRequest
    ) -> dict[str, Any]:
        svc = _require(container)
        try:
            return await svc.start_auto_optimize(
                session_id=session_id, payload=body.payload
            )
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc

    @router.get("/session/{session_id}/auto-optimize/{task_id}")
    async def get_auto_optimize(session_id: str, task_id: str) -> dict[str, Any]:
        svc = _require(container)
        try:
            return await svc.get_auto_optimize(session_id=session_id, task_id=task_id)
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc

    @router.get("/session/{session_id}/auto-optimize/{task_id}/compare")
    async def compare_auto_optimize(session_id: str, task_id: str) -> dict[str, Any]:
        svc = _require(container)
        try:
            return await svc.get_auto_optimize_compare(
                session_id=session_id, task_id=task_id
            )
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc

    # ── QNN run stream (real-time log SSE passthrough) ──────────────────────
    @router.post("/qnn/run/stream")
    async def run_qnn_stream(body: RunQnnRequest) -> StreamingResponse:
        svc = _require(container)
        # Wrap the adapter's raw SSE byte iterator; pass through byte-for-byte.
        return StreamingResponse(
            svc.run_qnn_stream(payload=body.payload),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── model graph ─────────────────────────────────────────────────────────
    @router.get("/model/graph")
    async def model_graph(request: Request) -> dict[str, Any]:
        svc = _require(container)
        try:
            return await svc.get_model_graph(params=dict(request.query_params))
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc

    # ── benchmark pair ───────────────────────────────────────────────────────
    @router.post("/qnn/benchmark-pair")
    async def benchmark_pair(body: BenchmarkPairRequest) -> dict[str, Any]:
        svc = _require(container)
        try:
            return await svc.benchmark_pair(payload=body.payload)
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc

    # ── artifacts / outputs ──────────────────────────────────────────────────
    @router.get("/session/{session_id}/artifacts")
    async def list_artifacts(session_id: str) -> dict[str, Any]:
        svc = _require(container)
        try:
            return await svc.list_artifacts(session_id=session_id)
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc

    @router.get("/session/{session_id}/artifacts/{artifact_id}/download")
    async def download_artifact(session_id: str, artifact_id: str) -> Response:
        svc = _require(container)
        _reject_path_segment(artifact_id, field="artifact_id")
        try:
            content, media, filename = await svc.download_artifact(
                session_id=session_id, artifact_id=artifact_id
            )
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc
        return Response(
            content=content,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/session/{session_id}/outputs/{filename}")
    async def download_output(session_id: str, filename: str) -> Response:
        svc = _require(container)
        _reject_path_segment(filename, field="filename")
        try:
            content, media, out_name = await svc.download_output(
                session_id=session_id, filename=filename
            )
        except httpx.HTTPError as exc:
            raise _map_upstream_error(exc) from exc
        return Response(
            content=content,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
        )

    return router


__all__ = ["build_router"]
