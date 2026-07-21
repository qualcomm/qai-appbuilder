# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``VerifyChecksumUseCase`` -- post-download integrity check."""

from __future__ import annotations

from dataclasses import dataclass

from qai.model_catalog.application.ports import (
    ChecksumVerifierPort,
    ModelEntryRepositoryPort,
)
from qai.model_catalog.domain.errors import (
    ChecksumMismatchError,
    ModelEntryNotFoundError,
)
from qai.model_catalog.domain.events import (
    ChecksumMismatchEvent,
    ChecksumVerifiedEvent,
)
from qai.model_catalog.domain.ids import ModelEntryId, ModelVersionId
from qai.model_catalog.domain.value_objects import StorageKey
from qai.platform.events import EventBus


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifyChecksumCommand:
    """Input DTO for :class:`VerifyChecksumUseCase`."""

    model_id: ModelEntryId
    version_id: ModelVersionId
    target: StorageKey


class VerifyChecksumUseCase:
    """Verify a downloaded blob against the version's stored
    :class:`Checksum`.

    Mismatch raises :class:`ChecksumMismatchError` AND publishes
    :class:`ChecksumMismatchEvent` (the event is for audit / UI; the
    exception is for the immediate caller).
    """

    def __init__(
        self,
        *,
        entry_repository: ModelEntryRepositoryPort,
        verifier: ChecksumVerifierPort,
        event_bus: EventBus,
    ) -> None:
        self._entry_repo = entry_repository
        self._verifier = verifier
        self._event_bus = event_bus

    async def execute(self, command: VerifyChecksumCommand) -> None:
        entry = await self._entry_repo.find_by_id(command.model_id)
        if entry is None:
            raise ModelEntryNotFoundError(command.model_id.value)
        version = entry.get_version(command.version_id)

        actual = await self._verifier.compute(
            command.target, algorithm=version.checksum.algorithm.value
        )
        if not version.checksum.matches(actual):
            await self._event_bus.publish(
                ChecksumMismatchEvent(
                    version_id=version.version_id.value,
                    expected=version.checksum.value,
                    actual=actual,
                )
            )
            raise ChecksumMismatchError(
                expected=version.checksum.value,
                actual=actual,
                version_id=version.version_id.value,
            )
        await self._event_bus.publish(
            ChecksumVerifiedEvent(version_id=version.version_id.value)
        )


__all__ = ["VerifyChecksumUseCase", "VerifyChecksumCommand"]
