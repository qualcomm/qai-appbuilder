# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""OC service subprocess control routes (``_register_oc_service_routes``).

The 4 OC-only routes (start / stop / status / logs).  Extracted verbatim
from the former single-file ``ai_coding.py`` (zero behaviour change).
The DTOs in this group are used only by this module, so they live here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Query
from pydantic import BaseModel

from qai.ai_coding.application.use_cases.manage_oc_service import (
    GetOcServiceLogsQuery,
    StopOcServiceCommand,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


class OcServiceStatusResponse(BaseModel):
    """Body of ``GET /api/oc/service/status`` (PR-105).

    Mirrors the legacy OC ``oc_proc_manager.status()`` shape.
    """

    running: bool
    pid: int | None = None
    uptime_seconds: float | None = None
    port: int = 0
    cli_path: str = ""
    external: bool = False


class OcServiceStartResponse(BaseModel):
    """Body of ``POST /api/oc/service/start`` (PR-105)."""

    ok: bool
    already_running: bool
    running: bool
    pid: int | None = None
    uptime_seconds: float | None = None
    port: int = 0
    cli_path: str = ""
    external: bool = False


class OcServiceStopRequest(BaseModel):
    """Body of ``POST /api/oc/service/stop`` (PR-105) — optional."""

    force: bool = False


class OcServiceStopResponse(BaseModel):
    """Body of ``POST /api/oc/service/stop`` (PR-105)."""

    ok: bool
    running: bool
    port: int = 0
    cli_path: str = ""


class OcServiceLogsResponse(BaseModel):
    """Body of ``GET /api/oc/service/logs`` (PR-105)."""

    lines: list[str]


def _register_oc_service_routes(
    router: APIRouter,
    *,
    container: "Container",
) -> None:
    """Attach the 4 OC service subprocess control routes onto ``router``.

    These routes are OC-only — there is no CC equivalent because the
    Claude Code provider runs in-process (no managed subprocess).
    All routes mirror the legacy ``backend/ai_coding/opencode_api_routes.py``
    wire shapes 1:1.
    """
    services = container.ai_coding

    @router.get("/service/status", response_model=OcServiceStatusResponse)
    async def oc_service_status() -> OcServiceStatusResponse:
        s = await services.get_oc_service_status_use_case.execute()
        return OcServiceStatusResponse(
            running=s.running,
            pid=s.pid,
            uptime_seconds=s.uptime_seconds,
            port=s.port,
            cli_path=s.cli_path,
            external=s.external,
        )

    @router.post("/service/start", response_model=OcServiceStartResponse)
    async def oc_service_start() -> OcServiceStartResponse:
        # Capture the pre-start state to surface the legacy
        # ``already_running`` signal — the WebUI uses it to show the
        # right toast.
        pre = await services.get_oc_service_status_use_case.execute()
        s = await services.start_oc_service_use_case.execute()
        return OcServiceStartResponse(
            ok=True,
            already_running=pre.running,
            running=s.running,
            pid=s.pid,
            uptime_seconds=s.uptime_seconds,
            port=s.port,
            cli_path=s.cli_path,
            external=s.external,
        )

    @router.post("/service/stop", response_model=OcServiceStopResponse)
    async def oc_service_stop(
        body: OcServiceStopRequest | None = None,
    ) -> OcServiceStopResponse:
        force = bool(body.force) if body is not None else False
        s = await services.stop_oc_service_use_case.execute(
            StopOcServiceCommand(force=force)
        )
        return OcServiceStopResponse(
            ok=True,
            running=s.running,
            port=s.port,
            cli_path=s.cli_path,
        )

    @router.get("/service/logs", response_model=OcServiceLogsResponse)
    async def oc_service_logs(
        last_n: int = Query(default=100, ge=1, le=500),
    ) -> OcServiceLogsResponse:
        result = await services.get_oc_service_logs_use_case.execute(
            GetOcServiceLogsQuery(last_n=last_n)
        )
        return OcServiceLogsResponse(lines=list(result.lines))
