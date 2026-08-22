# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: set the per-session thinking-depth override.

Backs the legacy ``POST /api/cc/sessions/{id}/effort`` route.
Delegates the mutation to :meth:`CodingSession.set_effort` (PR-104a)
which validates the value against the legacy CLI set
(``low`` / ``medium`` / ``high`` / ``max`` / :data:`None`) and emits
an :class:`EffortChangedEvent`.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import CodingSessionRepositoryPort
from qai.ai_coding.domain import CodingSession, CodingSessionId
from qai.platform.events import EventBus
from qai.platform.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class SetSessionEffortCommand:
    """Input for :class:`SetSessionEffortUseCase`."""

    session_id: CodingSessionId
    # ``None`` clears the override and the global default applies.
    effort: str | None


class SetSessionEffortUseCase:
    """Application service for adjusting per-session thinking depth."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        event_bus: EventBus,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus

    async def execute(
        self, command: SetSessionEffortCommand
    ) -> CodingSession:
        session = await self._repository.get(command.session_id)
        # Domain validation (allowed value set) lives on the
        # ``CodingSessionConfig`` value object via ``replace``;
        # invalid values raise :class:`ValueError` from
        # ``__post_init__``.  The route layer maps ``ValueError`` ->
        # 400 via the unified error handler.
        session.set_effort(command.effort)
        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)
        logger.info(
            "ai_coding.set_session_effort.ok",
            session_id=str(command.session_id),
            effort=command.effort or "None (use global)",
        )
        return session


__all__ = ["SetSessionEffortCommand", "SetSessionEffortUseCase"]
