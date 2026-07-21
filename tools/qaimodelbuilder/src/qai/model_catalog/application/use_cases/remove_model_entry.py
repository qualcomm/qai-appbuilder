# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``RemoveModelEntryUseCase`` -- delete an entry from the catalog."""

from __future__ import annotations

from qai.model_catalog.application.ports import ModelEntryRepositoryPort
from qai.model_catalog.domain.errors import ModelEntryNotFoundError
from qai.model_catalog.domain.events import ModelEntryRemovedEvent
from qai.model_catalog.domain.ids import ModelEntryId
from qai.platform.events import EventBus


class RemoveModelEntryUseCase:
    """Hard-delete a :class:`ModelEntry`.

    Raises :class:`ModelEntryNotFoundError` if the id is unknown so the
    HTTP layer can map the call to a 404.
    """

    def __init__(
        self,
        *,
        repository: ModelEntryRepositoryPort,
        event_bus: EventBus,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus

    async def execute(self, model_id: ModelEntryId) -> None:
        existing = await self._repository.find_by_id(model_id)
        if existing is None:
            raise ModelEntryNotFoundError(model_id.value)
        await self._repository.remove(model_id)
        await self._event_bus.publish(
            ModelEntryRemovedEvent(model_id=model_id.value)
        )


__all__ = ["RemoveModelEntryUseCase"]
