# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: control the local OpenCode HTTP server subprocess.

Backs the legacy ``GET /api/oc/service/status``,
``POST /api/oc/service/start``, ``POST /api/oc/service/stop`` and
``GET /api/oc/service/logs`` routes.

Architecture
------------
The ai_coding context delegates subprocess management to the
:class:`OcServicePort` adapter (real impl in
``qai.ai_coding.adapters.oc_service.LocalOcServiceAdapter`` using
:mod:`asyncio.subprocess`).  This module wraps each port operation in
a thin use case so the route layer stays free of subprocess control
flow and so unit tests can drive them with an in-memory fake.

The legacy backend exposed external-process detection (port-probe
fallback when the managed subprocess wasn't alive); the
:class:`OcServicePort.status` contract preserves that behaviour by
setting ``external=True`` when applicable so the WebUI can surface
the right indicator.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    OcServicePort,
    OcServiceStatus,
)
from qai.platform.logging import get_logger

logger = get_logger(__name__)


__all__ = [
    "GetOcServiceLogsQuery",
    "GetOcServiceLogsUseCase",
    "GetOcServiceStatusUseCase",
    "OcServiceLogsResult",
    "StartOcServiceUseCase",
    "StopOcServiceCommand",
    "StopOcServiceUseCase",
]


# ---------------------------------------------------------------------------
# Query / command DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class StopOcServiceCommand:
    """Input for :class:`StopOcServiceUseCase`.

    ``force`` mirrors the legacy POST body field — when :data:`True`
    the adapter terminates the subprocess unconditionally (SIGKILL on
    POSIX / TerminateProcess on Windows).  Default :data:`False`
    sends a graceful signal first.
    """

    force: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class GetOcServiceLogsQuery:
    """Input for :class:`GetOcServiceLogsUseCase`.

    ``last_n`` clamps to the adapter's internal cap (typically 500)
    so a misbehaving WebUI cannot drain the entire history in one
    call.
    """

    last_n: int = 100


@dataclass(frozen=True, slots=True, kw_only=True)
class OcServiceLogsResult:
    """Return shape of :class:`GetOcServiceLogsUseCase`."""

    lines: tuple[str, ...]


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


class GetOcServiceStatusUseCase:
    """Application service for ``GET /api/oc/service/status``."""

    def __init__(self, *, oc_service: OcServicePort) -> None:
        self._oc_service = oc_service

    async def execute(self) -> OcServiceStatus:
        return await self._oc_service.status()


class StartOcServiceUseCase:
    """Application service for ``POST /api/oc/service/start``.

    Idempotent — when the server is already running the adapter
    returns the current status snapshot without re-spawning.
    """

    def __init__(self, *, oc_service: OcServicePort) -> None:
        self._oc_service = oc_service

    async def execute(self) -> OcServiceStatus:
        status = await self._oc_service.start()
        logger.info(
            "ai_coding.oc_service.started",
            running=status.running,
            pid=status.pid,
            external=status.external,
            port=status.port,
        )
        return status


class StopOcServiceUseCase:
    """Application service for ``POST /api/oc/service/stop``."""

    def __init__(self, *, oc_service: OcServicePort) -> None:
        self._oc_service = oc_service

    async def execute(self, command: StopOcServiceCommand) -> OcServiceStatus:
        status = await self._oc_service.stop(force=command.force)
        logger.info(
            "ai_coding.oc_service.stopped",
            running=status.running,
            force=command.force,
        )
        return status


class GetOcServiceLogsUseCase:
    """Application service for ``GET /api/oc/service/logs``."""

    def __init__(self, *, oc_service: OcServicePort) -> None:
        self._oc_service = oc_service

    async def execute(self, query: GetOcServiceLogsQuery) -> OcServiceLogsResult:
        lines = await self._oc_service.logs(last_n=query.last_n)
        return OcServiceLogsResult(lines=tuple(lines))
