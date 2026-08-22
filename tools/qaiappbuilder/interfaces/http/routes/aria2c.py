# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Aria2c download daemon HTTP routes.

The ``GET /api/aria2c/status`` route now returns the V1 5-state banner
shape (available / can_auto_install / install_status / daemon_running /
bin_dir / exe_path / daemon_pid / rpc_port / install_error), backed by the
``service_release`` context's :class:`Aria2cManagerPort`.

``POST /api/aria2c/cancel/{task_id}`` cancels an in-flight download
(delegates to the download engine's per-task cancellation registry).

``POST /start`` / ``POST /stop`` start / stop the shared RPC daemon
(V1 parity — auto-installs the binary on Windows when absent).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


class Aria2cStartResponse(BaseModel):
    status: str
    pid: int | None = None
    message: str


class Aria2cStopResponse(BaseModel):
    status: str
    message: str


class Aria2cCancelResponse(BaseModel):
    cancelled: bool
    task_id: str


def build_router(*, container: "Container") -> APIRouter:
    router = APIRouter(prefix="/api/aria2c", tags=["aria2c"])

    @router.get("/status")
    async def get_status() -> dict:
        """Return the V1 5-state aria2c status banner shape."""
        status = await container.service_release.get_aria2c_status_use_case.execute()
        return status.to_wire()

    @router.post("/cancel/{task_id:path}", response_model=Aria2cCancelResponse)
    async def cancel_download(task_id: str) -> Aria2cCancelResponse:
        cancelled = await container.service_release.cancel_download_use_case.execute(
            task_id=task_id
        )
        return Aria2cCancelResponse(cancelled=cancelled, task_id=task_id)

    @router.post("/start", response_model=Aria2cStartResponse)
    async def start_aria2c() -> Aria2cStartResponse:
        """Ensure aria2c is installed + the RPC daemon is running (V1 parity).

        Auto-installs the aria2c binary (Windows) on first use, then starts
        the shared RPC daemon. Returns the post-action status in the frozen
        response shape (status / pid / message).
        """
        status = await container.service_release.start_aria2c_use_case.execute()
        if status.daemon_running:
            return Aria2cStartResponse(
                status="ok",
                pid=status.daemon_pid,
                message=f"aria2c daemon running on port {status.rpc_port}",
            )
        if not status.available:
            return Aria2cStartResponse(
                status="error",
                pid=None,
                message=status.install_error or "aria2c not available",
            )
        return Aria2cStartResponse(
            status="error",
            pid=None,
            message=status.install_error or "aria2c daemon failed to start",
        )

    @router.post("/stop", response_model=Aria2cStopResponse)
    async def stop_aria2c() -> Aria2cStopResponse:
        """Stop the aria2c RPC daemon (V1 parity)."""
        status = await container.service_release.stop_aria2c_use_case.execute()
        return Aria2cStopResponse(
            status="ok",
            message=(
                "aria2c daemon still running"
                if status.daemon_running
                else "aria2c daemon stopped"
            ),
        )

    return router
