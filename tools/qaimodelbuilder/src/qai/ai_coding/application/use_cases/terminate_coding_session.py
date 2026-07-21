# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: terminate a running :class:`CodingSession`."""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    CodingProviderPort,
    CodingSessionRepositoryPort,
    WorkspaceLockPort,
)
from qai.ai_coding.domain import CodingSessionId
from qai.platform.events import EventBus
from qai.platform.logging import get_logger
from qai.platform.time import Clock

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class TerminateCodingSessionCommand:
    """Input for :class:`TerminateCodingSessionUseCase`."""

    session_id: CodingSessionId
    reason: str = "user_request"


class TerminateCodingSessionUseCase:
    """Application service for closing a coding session."""

    def __init__(
        self,
        *,
        provider_port: CodingProviderPort,
        repository: CodingSessionRepositoryPort,
        workspace_lock: WorkspaceLockPort,
        clock: Clock,
        event_bus: EventBus,
    ) -> None:
        self._provider_port = provider_port
        self._repository = repository
        self._workspace_lock = workspace_lock
        self._clock = clock
        self._event_bus = event_bus

    async def execute(self, command: TerminateCodingSessionCommand) -> None:
        session = await self._repository.get(command.session_id)
        if session.status.value == "terminated":
            # Idempotent: nothing to do.
            return

        # Stop the backend first; even if it raises we still mark the
        # aggregate as terminated so callers don't get stuck on a
        # half-dead session.
        try:
            await self._provider_port.terminate(session_id=command.session_id)
        except Exception as exc:  # noqa: BLE001 — must not block aggregate close
            logger.warning(
                "ai_coding.terminate_coding_session.provider_error",
                session_id=str(command.session_id),
                error=repr(exc),
            )

        session.terminate(reason=command.reason, now=self._clock.now())
        await self._repository.save(session)
        await self._workspace_lock.release(session.workspace)
        for event in session.drain_events():
            await self._event_bus.publish(event)
        logger.info(
            "ai_coding.terminate_coding_session.ok",
            session_id=str(command.session_id),
            reason=command.reason,
        )


__all__ = [
    "TerminateCodingSessionCommand",
    "TerminateCodingSessionUseCase",
]
