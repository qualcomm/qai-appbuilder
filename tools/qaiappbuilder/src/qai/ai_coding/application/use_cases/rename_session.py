# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: rename a :class:`CodingSession`.

Backs the legacy ``POST /api/cc/sessions/{id}/rename`` route.
Delegates the title-mutation to the domain method
:meth:`CodingSession.rename` (PR-104a) which validates that the new
title is non-empty and emits a :class:`CodingSessionRenamedEvent`.

The legacy route allows renaming closed sessions in the history
view; we mirror that by NOT raising on ``TERMINATED`` (the domain
method also permits it).
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import CodingSessionRepositoryPort
from qai.ai_coding.domain import CodingSession, CodingSessionId
from qai.platform.events import EventBus
from qai.platform.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class RenameSessionCommand:
    """Input for :class:`RenameSessionUseCase`."""

    session_id: CodingSessionId
    new_title: str


class RenameSessionUseCase:
    """Application service for setting a session's display title."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        event_bus: EventBus,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus

    async def execute(self, command: RenameSessionCommand) -> CodingSession:
        session = await self._repository.get(command.session_id)
        session.rename(command.new_title)
        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)
        logger.info(
            "ai_coding.rename_session.ok",
            session_id=str(command.session_id),
            new_title=command.new_title,
        )
        return session


__all__ = ["RenameSessionCommand", "RenameSessionUseCase"]
