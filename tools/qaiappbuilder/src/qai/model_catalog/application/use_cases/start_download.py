# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``StartDownloadUseCase`` -- create + dispatch a :class:`DownloadJob`.

This use case is the canonical example of how the new design *breaks*
the legacy download/release-helper cycle:

* It looks up the parent :class:`ModelEntry` through
  :class:`ModelEntryRepositoryPort` -- not by reaching into a
  release-helper module.
* It resolves the source URL + storage key from the version metadata
  itself (no global config singletons).
* It hands the download to whatever :class:`DownloadEnginePort`
  implementation is wired in -- a native CLI engine, ``httpx``, or a
  fake -- with no engine-specific knowledge in the use case.

The use case does NOT block on the actual byte transfer; that is
handled by ``StreamDownloadProgressUseCase`` (or directly by the engine
adapter for fire-and-forget HTTP semantics).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from qai.model_catalog.application.ports import (
    DownloadEnginePort,
    DownloadJobRepositoryPort,
    ModelEntryRepositoryPort,
)
from qai.model_catalog.domain.entities import DownloadJob
from qai.model_catalog.domain.errors import ModelEntryNotFoundError
from qai.model_catalog.domain.events import DownloadStartedEvent
from qai.model_catalog.domain.ids import (
    DownloadJobId,
    ModelEntryId,
    ModelVersionId,
)
from qai.model_catalog.domain.value_objects import (
    DownloadJobState,
    StorageKey,
)
from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.time import Clock


_TARGET_CATEGORY = "models"


@dataclass(frozen=True, slots=True, kw_only=True)
class StartDownloadCommand:
    """Input DTO for :class:`StartDownloadUseCase`."""

    model_id: ModelEntryId
    version_id: ModelVersionId
    target_filename: str  # logical filename only — adapter resolves path


class StartDownloadUseCase:
    """Persist a :class:`DownloadJob` and hand it to the engine.

    On successful submission, transitions the job to ``RUNNING`` and
    publishes :class:`DownloadStartedEvent`.

    Failures during submission propagate to the caller; the partially
    created job (still ``QUEUED``) is preserved in the repository so the
    caller can retry / cancel.
    """

    def __init__(
        self,
        *,
        entry_repository: ModelEntryRepositoryPort,
        job_repository: DownloadJobRepositoryPort,
        engine: DownloadEnginePort,
        ids: IdGenerator,
        clock: Clock,
        event_bus: EventBus,
    ) -> None:
        self._entry_repo = entry_repository
        self._job_repo = job_repository
        self._engine = engine
        self._ids = ids
        self._clock = clock
        self._event_bus = event_bus

    async def execute(self, command: StartDownloadCommand) -> DownloadJob:
        entry = await self._entry_repo.find_by_id(command.model_id)
        if entry is None:
            raise ModelEntryNotFoundError(command.model_id.value)
        version = entry.get_version(command.version_id)

        now: datetime = self._clock.now()
        job = DownloadJob(
            job_id=DownloadJobId.generate(self._ids),
            target_model_version_id=version.version_id,
            state=DownloadJobState.QUEUED,
            created_at=now,
            updated_at=now,
        )
        await self._job_repo.add(job)

        target = StorageKey(
            category=_TARGET_CATEGORY, name=command.target_filename
        )
        await self._engine.start(
            job, source=version.manifest_url, target=target
        )

        # Mark RUNNING + persist + publish event.
        job.start(now=self._clock.now())
        await self._job_repo.update(job)
        await self._event_bus.publish(
            DownloadStartedEvent(
                job_id=job.job_id.value,
                version_id=version.version_id.value,
            )
        )
        return job


__all__ = ["StartDownloadUseCase", "StartDownloadCommand"]
