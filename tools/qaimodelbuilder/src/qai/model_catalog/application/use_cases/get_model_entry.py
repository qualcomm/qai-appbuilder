# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``GetModelEntryUseCase`` -- single-entry lookup."""

from __future__ import annotations

from qai.model_catalog.application.ports import ModelEntryRepositoryPort
from qai.model_catalog.domain.entities import ModelEntry
from qai.model_catalog.domain.errors import ModelEntryNotFoundError
from qai.model_catalog.domain.ids import ModelEntryId


class GetModelEntryUseCase:
    """Fetch a :class:`ModelEntry` by id, raising on miss."""

    def __init__(self, *, repository: ModelEntryRepositoryPort) -> None:
        self._repository = repository

    async def execute(self, model_id: ModelEntryId) -> ModelEntry:
        entry = await self._repository.find_by_id(model_id)
        if entry is None:
            raise ModelEntryNotFoundError(model_id.value)
        return entry


__all__ = ["GetModelEntryUseCase"]
