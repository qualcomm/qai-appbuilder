# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``CancelDownloadUseCase`` -- abort a running / queued download."""

from __future__ import annotations

from qai.model_catalog.application.ports import (
    DownloadEnginePort,
    DownloadJobRepositoryPort,
)
from qai.model_catalog.domain.errors import (
    DownloadJobAlreadyTerminatedError,
    DownloadJobNotFoundError,
)
from qai.model_catalog.domain.events import DownloadCancelledEvent
from qai.model_catalog.domain.ids import DownloadJobId
from qai.platform.events import EventBus
from qai.platform.time import Clock


class CancelDownloadUseCase:
    """Cancel a :class:`DownloadJob`.

    The engine's ``cancel`` is *advisory*: the engine acknowledges the
    cancel request, but the use case is what flips the state and emits
    the event.  We call the engine first so it can free its resources;
    if the engine raises, we still report the failure to the caller.

    Calling cancel on a terminal job raises
    :class:`DownloadJobAlreadyTerminatedError`; the HTTP layer maps this
    to 409.
    """

    def __init__(
        self,
        *,
        job_repository: DownloadJobRepositoryPort,
        engine: DownloadEnginePort,
        clock: Clock,
        event_bus: EventBus,
    ) -> None:
        self._job_repo = job_repository
        self._engine = engine
        self._clock = clock
        self._event_bus = event_bus

    async def execute(self, job_id: DownloadJobId) -> None:
        job = await self._job_repo.find_by_id(job_id)
        if job is None:
            raise DownloadJobNotFoundError(job_id.value)
        if job.state.is_terminal:
            raise DownloadJobAlreadyTerminatedError(
                job.job_id.value, job.state.value
            )
        await self._engine.cancel(job)
        job.cancel(now=self._clock.now())
        await self._job_repo.update(job)
        await self._event_bus.publish(
            DownloadCancelledEvent(
                job_id=job.job_id.value,
                version_id=job.target_model_version_id.value,
            )
        )


__all__ = ["CancelDownloadUseCase"]
