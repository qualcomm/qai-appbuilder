# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``GetWorkerStatusUseCase`` — surface :class:`WorkerPoolStatus` to the API.

Thin wrapper over :class:`WorkerStatusPort.status`. Lets the route
handler depend on the application layer (per S3 spec §2 invariant 14)
without bypassing it for a getattr-style call on the port.
"""

from __future__ import annotations

from qai.app_builder.application.ports import WorkerPoolStatus, WorkerStatusPort

__all__ = ["GetWorkerStatusUseCase"]


class GetWorkerStatusUseCase:
    """Return the current :class:`WorkerPoolStatus` snapshot."""

    def __init__(self, *, worker_status: WorkerStatusPort) -> None:
        self._worker_status = worker_status

    async def execute(self) -> WorkerPoolStatus:
        return await self._worker_status.status()
