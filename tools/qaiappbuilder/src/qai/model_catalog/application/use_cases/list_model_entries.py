# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ListModelEntriesUseCase`` -- enumerate the catalog."""

from __future__ import annotations

from qai.model_catalog.application.ports import ModelEntryRepositoryPort
from qai.model_catalog.domain.entities import ModelEntry


class ListModelEntriesUseCase:
    """Return all :class:`ModelEntry` rows.

    Filtering / pagination is left to higher layers; this use case
    intentionally exposes the *full* set so HTTP handlers can apply
    their own pagination policy without re-running the query.
    """

    def __init__(self, *, repository: ModelEntryRepositoryPort) -> None:
        self._repository = repository

    async def execute(self) -> list[ModelEntry]:
        return await self._repository.list_all()


__all__ = ["ListModelEntriesUseCase"]
