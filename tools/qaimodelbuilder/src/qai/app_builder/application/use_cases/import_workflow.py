# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Three import-workflow use cases: dry-run / commit / rollback.

The use cases are thin orchestrators over :class:`ImportPort`. They
exist as separate classes (rather than methods on a single use case)
so each one corresponds 1:1 to a legacy HTTP route — see
``02-routes.md`` lines 168–170.
"""

from __future__ import annotations

from collections.abc import Iterable

from qai.app_builder.application.ports import ImportPort
from qai.app_builder.domain.events import (
    ImportCommittedEvent,
    ImportRolledBackEvent,
)
from qai.app_builder.domain.import_plan import CommitId, ImportPlan
from qai.platform.events import EventBus
from qai.platform.time import Clock

__all__ = [
    "ImportDryRunUseCase",
    "ImportCommitUseCase",
    "ImportRollbackUseCase",
]


class ImportDryRunUseCase:
    """Inspect candidate sources and return a non-mutating :class:`ImportPlan`."""

    def __init__(self, *, importer: ImportPort) -> None:
        self._importer = importer

    async def execute(self, *, candidates: Iterable[str]) -> ImportPlan:
        return await self._importer.dry_run(candidates)


class ImportCommitUseCase:
    """Commit a previously-validated :class:`ImportPlan`.

    Returns the new :class:`CommitId` and publishes
    :class:`ImportCommittedEvent`. Empty / no-op plans are committed
    too (the adapter decides whether they generate an audit row).
    """

    def __init__(
        self,
        *,
        importer: ImportPort,
        events: EventBus,
        clock: Clock,
    ) -> None:
        self._importer = importer
        self._events = events
        self._clock = clock

    async def execute(self, *, plan: ImportPlan) -> CommitId:
        commit_id = await self._importer.commit(plan)
        await self._events.publish(
            ImportCommittedEvent(
                commit_id=commit_id,
                item_count=len(plan.items),
                committed_at=self._clock.now(),
            )
        )
        return commit_id


class ImportRollbackUseCase:
    """Roll back a previously-committed import.

    Surfaces :class:`qai.app_builder.domain.errors.ImportConflictError`
    when ``commit_id`` is unknown or already rolled back.
    """

    def __init__(
        self,
        *,
        importer: ImportPort,
        events: EventBus,
        clock: Clock,
    ) -> None:
        self._importer = importer
        self._events = events
        self._clock = clock

    async def execute(self, *, commit_id: CommitId) -> None:
        await self._importer.rollback(commit_id)
        await self._events.publish(
            ImportRolledBackEvent(
                commit_id=commit_id,
                rolled_back_at=self._clock.now(),
            )
        )
