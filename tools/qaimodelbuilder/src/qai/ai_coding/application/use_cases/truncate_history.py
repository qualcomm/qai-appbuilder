# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: truncate a trailing slice of a session's message history.

Backs the legacy ``POST /api/cc/sessions/{id}/truncate_history`` route
(File-rollback / Edit-and-Resend mode).  Locates the user-message
anchor by its session-position (the legacy route accepts a
``after_msg_id`` string; the new aggregate identifies messages by
position only) and delegates to
:meth:`CodingSession.truncate_history_after`.

The "anchor by id" → "anchor by position" mismatch
--------------------------------------------------
Legacy ``after_msg_id`` was a synthetic id of the form
``cc-user-<timestamp>`` assigned by the legacy session manager when
ferrying the user message.  The new domain stores only the
:class:`MessageContent` text on the aggregate (richer metadata flows
through events) so the route layer is responsible for resolving an
``after_msg_id`` -> ``marker_index`` mapping.  The current contract:

* The route layer accepts an integer ``after_index`` (0-based index
  into the session's message list) and forwards it as
  ``marker_index``.
* If the legacy clients send the legacy string id, the route layer
  parses it; bridging is the route layer's job, not the use case's.

This keeps the use case domain-pure and lets the route layer
preserve the legacy wire contract without leaking timestamp-based
ids into the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import CodingSessionRepositoryPort
from qai.ai_coding.domain import CodingSessionId
from qai.platform.events import EventBus
from qai.platform.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class TruncateHistoryCommand:
    """Input for :class:`TruncateHistoryUseCase`."""

    session_id: CodingSessionId
    marker_index: int
    include_self: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class TruncateHistoryResult:
    """Outcome — count removed plus the new history length."""

    removed: int
    remaining: int


class TruncateHistoryUseCase:
    """Application service for the truncate-history Edit-and-Resend flow."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        event_bus: EventBus,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus

    async def execute(
        self, command: TruncateHistoryCommand
    ) -> TruncateHistoryResult:
        session = await self._repository.get(command.session_id)
        removed = session.truncate_history_after(
            marker_index=command.marker_index,
            include_self=command.include_self,
        )
        if removed > 0:
            await self._repository.save(session)
            for event in session.drain_events():
                await self._event_bus.publish(event)
        logger.info(
            "ai_coding.truncate_history.ok",
            session_id=str(command.session_id),
            marker_index=command.marker_index,
            include_self=command.include_self,
            removed=removed,
        )
        return TruncateHistoryResult(
            removed=removed,
            remaining=len(session.messages),
        )


__all__ = [
    "TruncateHistoryCommand",
    "TruncateHistoryResult",
    "TruncateHistoryUseCase",
]
