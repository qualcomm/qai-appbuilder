# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``RegisterModelEntryUseCase`` -- adds a new entry to the catalog."""

from __future__ import annotations

from dataclasses import dataclass

from qai.model_catalog.application.ports import ModelEntryRepositoryPort
from qai.model_catalog.domain.entities import ModelEntry
from qai.model_catalog.domain.errors import ModelEntryConflictError
from qai.model_catalog.domain.events import ModelEntryRegisteredEvent
from qai.model_catalog.domain.ids import ModelEntryId
from qai.model_catalog.domain.value_objects import (
    ProviderKind,
    SourceUrl,
    Taxonomy,
)
from qai.platform.events import EventBus


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterModelEntryCommand:
    """Input DTO for :class:`RegisterModelEntryUseCase`."""

    model_id: ModelEntryId
    name: str
    provider: ProviderKind
    source_url: SourceUrl
    description: str = ""
    taxonomy: Taxonomy | None = None


class RegisterModelEntryUseCase:
    """Register a brand-new :class:`ModelEntry` in the catalog.

    Conflicts (entry already exists) raise
    :class:`ModelEntryConflictError`.  Successful registration publishes
    :class:`ModelEntryRegisteredEvent` on the supplied event bus.
    """

    def __init__(
        self,
        *,
        repository: ModelEntryRepositoryPort,
        event_bus: EventBus,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus

    async def execute(self, command: RegisterModelEntryCommand) -> ModelEntry:
        existing = await self._repository.find_by_id(command.model_id)
        if existing is not None:
            raise ModelEntryConflictError(command.model_id.value)
        entry = ModelEntry(
            model_id=command.model_id,
            name=command.name,
            provider=command.provider,
            source_url=command.source_url,
            description=command.description,
            taxonomy=command.taxonomy if command.taxonomy is not None else Taxonomy(),
        )
        await self._repository.add(entry)
        await self._event_bus.publish(
            ModelEntryRegisteredEvent(
                model_id=entry.model_id.value,
                name=entry.name,
                provider=entry.provider,
            )
        )
        return entry


__all__ = ["RegisterModelEntryUseCase", "RegisterModelEntryCommand"]
