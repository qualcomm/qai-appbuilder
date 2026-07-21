# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: permanently delete a :class:`CodingSession` row.

Backs the legacy ``DELETE /api/cc/sessions/{id}/permanent`` route.

Distinction from ``TerminateCodingSessionUseCase``
--------------------------------------------------
* :class:`TerminateCodingSessionUseCase` flips the aggregate's
  ``status`` to ``TERMINATED`` but **keeps the row** so it surfaces
  in the history view.
* :class:`HardDeleteSessionUseCase` (this file) **physically deletes**
  the row from the SQLite store; the legacy route was used by the
  WebUI's "彻底删除历史会话" (delete forever) action.

Sequence
--------
1. Load the session by id (so a 404 is raised before we touch the
   database) — provides parity with the legacy route's
   ``HTTPException(404, "Session not found")`` shape.
2. If the session is **not** already ``TERMINATED``, terminate it
   first (release lock, drop provider) so we never delete a row that
   still owns a workspace lock.  This avoids a class of resource
   leaks the legacy code tolerated by accident.
3. Delete the row via :meth:`CodingSessionRepositoryPort.delete`.
4. Emit no domain event — the deletion is treated as a hard
   tombstone.  Audit logging is the route layer's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    CodingProviderPort,
    CodingSessionRepositoryPort,
    WorkspaceLockPort,
)
from qai.ai_coding.domain import (
    CodingSessionId,
    SessionStatus,
)
from qai.platform.events import EventBus
from qai.platform.logging import get_logger
from qai.platform.time import Clock

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class HardDeleteSessionCommand:
    """Input for :class:`HardDeleteSessionUseCase`."""

    session_id: CodingSessionId
    # Reason recorded on the implicit terminate-then-delete sequence
    # when the session was not already terminated.
    reason: str = "permanent_delete"


class HardDeleteSessionUseCase:
    """Application service for physically removing a coding session row."""

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

    async def execute(self, command: HardDeleteSessionCommand) -> None:
        session = await self._repository.get(command.session_id)

        # If still live, terminate first so the lock is released and
        # the provider task is dropped before the row disappears.
        if session.status is not SessionStatus.TERMINATED:
            try:
                await self._provider_port.terminate(
                    session_id=command.session_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ai_coding.hard_delete_session.provider_error",
                    session_id=str(command.session_id),
                    error=repr(exc),
                )
            session.terminate(reason=command.reason, now=self._clock.now())
            await self._repository.save(session)
            await self._workspace_lock.release(session.workspace)
            for event in session.drain_events():
                await self._event_bus.publish(event)

        await self._repository.delete(command.session_id)
        logger.info(
            "ai_coding.hard_delete_session.ok",
            session_id=str(command.session_id),
        )


__all__ = [
    "HardDeleteSessionCommand",
    "HardDeleteSessionUseCase",
]
