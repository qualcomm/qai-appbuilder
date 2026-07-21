# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""System / health / build-info / edition / reboot routes.

Thin "interfaces adapter": no business logic, just exposes
process-level diagnostic data + lifecycle signal from
``apps.api.di.Container`` over HTTP.

This module owns the 4 process-level routes under ``/api/system``:
- ``GET  /api/system/health``      — liveness + DB pragma snapshot
- ``GET  /api/system/build-info``  — package metadata + edition + paths
- ``GET  /api/system/edition``     — ``{"edition": "external"|"internal"}``
- ``POST /api/system/reboot``      — schedule a supervisor restart (202,
  external contract: exit code 75)

The other "system / settings / config" routes that legacy inventory
``02-routes.md`` §3.10 grouped under "system" have been redistributed
to their owning bounded contexts during the S3..S6 BC carving:

- ``/api/forge-config``, ``/api/preferences``, ``/api/proxy``,
  ``/api/settings/{dep_broker,exec_broker,process_proxy,file_broker,
  project_snapshot,file_watcher}`` → ``user_prefs`` (see
  ``interfaces/http/routes/user_prefs.py``).
- ``/api/versions/*`` (download / install / status) →
  ``model_catalog`` (see ``interfaces/http/routes/model_catalog.py``).
- ``/api/code-personas/*`` → ``ai_coding`` (see
  ``interfaces/http/routes/ai_coding.py``).

The historical migration audit lives in ``PR-030-manifest.md`` §3.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, status
from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

_START_TIME = time.time()


# ---- Response DTOs --------------------------------------------------------


class HealthResponse(BaseModel):
    """``GET /api/system/health`` payload."""

    status: str
    db: dict[str, object]
    timestamp: str


class BuildInfoResponse(BaseModel):
    """``GET /api/system/build-info`` payload."""

    name: str
    version: str
    edition: str
    python_path: str
    data_dir: str


class EditionResponse(BaseModel):
    """``GET /api/system/edition`` payload."""

    edition: str
    #: Convenience boolean mirroring ``settings.is_internal`` (``edition ==
    #: "internal"``). Lets the frontend branch edition-specific UX — e.g. the
    #: "missing cloud API key" flow opens an in-place key dialog on the
    #: internal edition (where the provider is pre-configured) but guides the
    #: user to Settings → Cloud Models on external (where they must add a
    #: provider first). Non-sensitive: it only reveals which build this is.
    is_internal: bool


class RebootResponse(BaseModel):
    """``POST /api/system/reboot`` payload.

    ``exit_code`` echoes the supervisor contract (default 75) so the
    caller can verify the server is wired correctly before triggering.
    """

    status: str
    exit_code: int


class PythonInfoResponse(BaseModel):
    version: str
    executable: str
    platform: str


class PlatformInfoResponse(BaseModel):
    system: str
    machine: str
    release: str


class UptimeResponse(BaseModel):
    start_time: str
    uptime_seconds: float


class DiskUsageResponse(BaseModel):
    total: int
    used: int
    free: int
    path: str


class EnvResponse(BaseModel):
    PYTHONPATH: str
    PATH: str
    COMPUTERNAME: str


class FeaturesResponse(BaseModel):
    features: list[str]


class ConfigSummaryResponse(BaseModel):
    host: str
    port: int
    docs_enabled: bool
    edition: str
    csrf_enabled: bool


# ---- Router factory -------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build a router bound to the given DI container.

    The router holds no module-level state; it is reconstructed every
    time ``apps.api.main.create_app`` is called (i.e., per FastAPI app).
    """
    router = APIRouter(prefix="/api/system", tags=["system"])

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        h = await container.database.health_check()
        return HealthResponse(
            status="ok",
            db={
                "journal_mode": h.journal_mode,
                "foreign_keys": h.foreign_keys,
                "user_version": h.user_version,
                "size_bytes": h.size_bytes,
            },
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    @router.get("/build-info", response_model=BuildInfoResponse)
    async def build_info() -> BuildInfoResponse:
        return BuildInfoResponse(
            name="qaimodelbuilder",
            version=_app_version(),
            edition=container.settings.edition,
            python_path=str(container.repo_root),
            data_dir=str(container.data_paths.root),
        )

    @router.get("/edition", response_model=EditionResponse)
    async def edition() -> EditionResponse:
        return EditionResponse(
            edition=container.settings.edition,
            is_internal=bool(container.settings.is_internal),
        )

    @router.post(
        "/reboot",
        response_model=RebootResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def reboot() -> RebootResponse:
        await container.system.reboot_signal.signal_reboot(
            reason="api.system.reboot.requested"
        )
        return RebootResponse(
            status="scheduled",
            exit_code=container.settings.server.reboot_exit_code,
        )

    @router.post(
        "/exit",
        response_model=RebootResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def exit_() -> RebootResponse:
        """Schedule a clean process exit (code 0 → supervisor does NOT respawn).

        Primary caller: the Tauri desktop shell on window-close. A GUI process
        has no console so it cannot deliver ``CTRL_BREAK_EVENT`` to the backend
        for a graceful stop (see ``desktop/src-tauri/src/lib.rs``); this HTTP
        endpoint is the reliable graceful-stop channel. The 202 is flushed
        before the (debounced) exit so the shell sees a clean ack. Same
        coalescing/flush mechanism as ``/reboot`` but exit code 0.
        """
        await container.reboot_scheduler.schedule_exit(
            reason="api.system.exit.requested"
        )
        return RebootResponse(status="scheduled", exit_code=0)

    @router.get("/python-info", response_model=PythonInfoResponse)
    async def python_info() -> PythonInfoResponse:
        return PythonInfoResponse(
            version=sys.version,
            executable=sys.executable,
            platform=sys.platform,
        )

    @router.get("/platform", response_model=PlatformInfoResponse)
    async def platform_info() -> PlatformInfoResponse:
        return PlatformInfoResponse(
            system=platform.system(),
            machine=platform.machine(),
            release=platform.release(),
        )

    @router.get("/uptime", response_model=UptimeResponse)
    async def uptime() -> UptimeResponse:
        return UptimeResponse(
            start_time=datetime.fromtimestamp(
                _START_TIME, tz=timezone.utc
            ).isoformat(),
            uptime_seconds=time.time() - _START_TIME,
        )

    @router.get("/disk-usage", response_model=DiskUsageResponse)
    async def disk_usage() -> DiskUsageResponse:
        data_path = container.data_paths.root
        usage = shutil.disk_usage(str(data_path))
        return DiskUsageResponse(
            total=usage.total,
            used=usage.used,
            free=usage.free,
            path=str(data_path),
        )

    @router.get("/env", response_model=EnvResponse)
    async def env_info() -> EnvResponse:
        return EnvResponse(
            PYTHONPATH=os.environ.get("PYTHONPATH", ""),
            PATH=os.environ.get("PATH", ""),
            COMPUTERNAME=os.environ.get("COMPUTERNAME", ""),
        )

    @router.get("/features", response_model=FeaturesResponse)
    async def features() -> FeaturesResponse:
        return FeaturesResponse(
            features=[
                "chat",
                "ai_coding",
                "app_builder",
                "channels",
                "model_builder",
                "security",
            ]
        )

    @router.get("/config/summary", response_model=ConfigSummaryResponse)
    async def config_summary() -> ConfigSummaryResponse:
        return ConfigSummaryResponse(
            host=container.settings.server.host,
            port=container.settings.server.port,
            docs_enabled=container.settings.server.docs_enabled,
            edition=container.settings.edition,
            csrf_enabled=container.settings.security.csrf_enabled,
        )

    return router


def _app_version() -> str:
    try:
        from importlib.metadata import version

        return version("qaimodelbuilder")
    except Exception:  # noqa: BLE001
        return "0.0.0-dev"
