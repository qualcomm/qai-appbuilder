# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ListDownloadJobsUseCase`` — list active download jobs.

Issue (f) decision A (HANDOFF-after-PR-040 §5): the missing use case
that was deferred from PR-032 lands here in PR-044 alongside the real
:class:`SqliteDownloadJobRepository`.

The use case is intentionally small: it merely projects active
:class:`DownloadJob` aggregates from the repository. All filtering is
left to the caller (route layer); the legacy ``GET /api/aria2c/tasks``
behaviour required the active set, which is what
:meth:`DownloadJobRepositoryPort.list_active` already exposes, so we
wrap it 1:1.
"""

from __future__ import annotations

from qai.model_catalog.application.ports import DownloadJobRepositoryPort
from qai.model_catalog.domain.entities import DownloadJob


class ListDownloadJobsUseCase:
    """Return every :class:`DownloadJob` currently in a non-terminal state.

    Use cases never expose port instances; this thin wrapper exists so
    the HTTP layer can call a use case (consistent with PR-032 routes)
    rather than poking the repository directly.
    """

    def __init__(
        self,
        *,
        job_repository: DownloadJobRepositoryPort,
    ) -> None:
        self._job_repo = job_repository

    async def execute(self) -> list[DownloadJob]:
        """Return active jobs ordered by repository convention."""
        return await self._job_repo.list_active()


__all__ = ["ListDownloadJobsUseCase"]
