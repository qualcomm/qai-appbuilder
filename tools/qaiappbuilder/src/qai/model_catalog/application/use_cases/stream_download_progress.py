# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``StreamDownloadProgressUseCase`` -- relay engine progress to callers.

The use case bridges the engine's :meth:`stream_progress` AsyncIterator
to the rest of the system: it persists the latest progress on the
:class:`DownloadJob` and yields each snapshot to its own caller
(typically an HTTP/SSE handler). Per-snapshot progress is delivered over
that dedicated stream, NOT the event bus, so no per-snapshot event is
published.

When the engine's stream completes, the use case finalises the job:

* If ``progress.is_complete`` ⇒ ``DownloadJob.complete()`` and emit
  :class:`DownloadCompletedEvent`.
* Otherwise the engine considered the job done without finishing the
  bytes -- treat as a soft failure.

Adapters that need richer failure semantics (e.g. exceptions raised
mid-stream) should propagate them to the use-case layer so it can
record a proper failure reason.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from qai.model_catalog.application.ports import (
    DownloadEnginePort,
    DownloadJobRepositoryPort,
)
from qai.model_catalog.domain.errors import DownloadJobNotFoundError
from qai.model_catalog.domain.events import (
    DownloadCompletedEvent,
    DownloadFailedEvent,
)
from qai.model_catalog.domain.ids import DownloadJobId
from qai.model_catalog.domain.value_objects import DownloadProgress
from qai.platform.events import EventBus
from qai.platform.time import Clock


class StreamDownloadProgressUseCase:
    """Yield progress updates and finalise the job at end-of-stream."""

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

    async def execute(
        self, job_id: DownloadJobId
    ) -> AsyncIterator[DownloadProgress]:
        job = await self._job_repo.find_by_id(job_id)
        if job is None:
            raise DownloadJobNotFoundError(job_id.value)
        return self._iterate(job_id)

    async def _iterate(
        self, job_id: DownloadJobId
    ) -> AsyncIterator[DownloadProgress]:
        # Re-fetch lazily so we always work against the persisted state.
        job = await self._job_repo.find_by_id(job_id)
        if job is None:
            raise DownloadJobNotFoundError(job_id.value)

        last: DownloadProgress | None = None
        try:
            async for snapshot in self._engine.stream_progress(job):
                last = snapshot
                if not job.state.is_terminal:
                    job.update_progress(snapshot, now=self._clock.now())
                    await self._job_repo.update(job)
                    # Per-snapshot ``DownloadProgressedEvent`` is intentionally
                    # NOT published to the in-process event bus. No production
                    # subscriber consumes per-progress events — the progress
                    # reaches the front-end over the dedicated download SSE
                    # (the route consumes this use case's async iterator
                    # directly). The only bus subscriber that matched
                    # ``model_catalog.download_progressed`` was the global
                    # ``/api/events`` notification SSE, which dropped it, so
                    # publishing per snapshot only floods that bounded queue
                    # (the historical ``events.backpressure`` log-spam). The
                    # terminal lifecycle events (started / completed / failed)
                    # below ARE low-frequency notifications and stay on the bus.
                yield snapshot
        except Exception as exc:  # noqa: BLE001 -- normalise + re-raise
            if not job.state.is_terminal:
                job.fail(reason=str(exc), now=self._clock.now())
                await self._job_repo.update(job)
                await self._event_bus.publish(
                    DownloadFailedEvent(
                        job_id=job.job_id.value,
                        version_id=job.target_model_version_id.value,
                        reason=str(exc),
                    )
                )
            raise

        # Stream ended cleanly — finalise based on the last snapshot.
        if not job.state.is_terminal:
            if last is not None and last.is_complete:
                job.complete(now=self._clock.now())
                await self._job_repo.update(job)
                await self._event_bus.publish(
                    DownloadCompletedEvent(
                        job_id=job.job_id.value,
                        version_id=job.target_model_version_id.value,
                    )
                )
            else:
                reason = (
                    "engine stream exhausted before completion"
                    if last is not None
                    else "engine stream produced no progress events"
                )
                job.fail(reason=reason, now=self._clock.now())
                await self._job_repo.update(job)
                await self._event_bus.publish(
                    DownloadFailedEvent(
                        job_id=job.job_id.value,
                        version_id=job.target_model_version_id.value,
                        reason=reason,
                    )
                )


__all__ = ["StreamDownloadProgressUseCase"]
