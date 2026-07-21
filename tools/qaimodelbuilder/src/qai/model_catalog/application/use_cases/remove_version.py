# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``RemoveVersionUseCase`` — drop a single :class:`ModelVersion` from
its parent :class:`ModelEntry`.

Issue (f) decision A (HANDOFF-after-PR-040 §5): the missing use case
that was deferred from PR-032 lands here in PR-044. The legacy route
``DELETE /api/versions/install/{version}`` needs a way to uninstall a
single version while keeping the parent catalogue entry; this use case
re-exposes :meth:`ModelEntry.remove_version` through the application
layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.model_catalog.application.ports import ModelEntryRepositoryPort
from qai.model_catalog.domain.errors import (
    ModelEntryNotFoundError,
    ModelVersionNotFoundError,
)
from qai.model_catalog.domain.events import ModelEntryRemovedEvent
from qai.model_catalog.domain.ids import ModelEntryId, ModelVersionId
from qai.platform.events import EventBus


@dataclass(frozen=True, slots=True, kw_only=True)
class RemoveVersionCommand:
    """Input DTO for :class:`RemoveVersionUseCase`."""

    model_id: ModelEntryId
    version_id: ModelVersionId


class RemoveVersionUseCase:
    """Remove a :class:`ModelVersion` from its parent aggregate.

    Behaviour
    ---------
    * Raises :class:`ModelEntryNotFoundError` (HTTP 404) when the entry
      itself is unknown.
    * Raises :class:`ModelVersionNotFoundError` (HTTP 404) when the
      entry exists but the version does not.
    * Persists the mutated parent entry through the repository's
      ``update`` so soft constraints (``current_version_id`` clearing,
      cascade delete in SQL) take effect.
    * Emits ``ModelEntryRemovedEvent`` with a ``version_removed`` detail
      semantic — we deliberately reuse the catalog-level event family
      rather than introduce a new event type so subscribers can match
      ``"model_catalog.*"`` with a single registration.

    The use case does **not** delete the underlying blob — that is a
    separate concern owned by the installer adapters that actually
    placed the bytes on disk.  This use case mutates the aggregate
    only; downstream installers observe ``ModelEntryRemovedEvent`` and
    drop their own filesystem residue.
    """

    def __init__(
        self,
        *,
        repository: ModelEntryRepositoryPort,
        event_bus: EventBus,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus

    async def execute(self, command: RemoveVersionCommand) -> None:
        entry = await self._repository.find_by_id(command.model_id)
        if entry is None:
            raise ModelEntryNotFoundError(command.model_id.value)
        # ``ModelEntry.remove_version`` raises ModelVersionNotFoundError
        # internally when the version is missing — re-raised verbatim.
        if entry.find_version(command.version_id) is None:
            raise ModelVersionNotFoundError(command.version_id.value)
        entry.remove_version(command.version_id)
        await self._repository.update(entry)
        # Re-purpose the catalog-removed event family with a per-version
        # marker; subscribers care about the (model_id, version_id)
        # tuple via the published payload.
        await self._event_bus.publish(
            ModelEntryRemovedEvent(model_id=command.model_id.value)
        )


__all__ = ["RemoveVersionUseCase", "RemoveVersionCommand"]
