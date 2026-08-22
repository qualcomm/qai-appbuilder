# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: session-index CRUD.

Replaces the legacy module-level ``_user_cc_sessions`` dict with a
proper aggregate behind a port.

Two use cases:

* :class:`BindSessionIndexUseCase` — upsert a
  ``(instance_id, channel_user_id) → (internal_user_id, coding_session_id)``
  mapping.
* :class:`LookupSessionIndexUseCase` — read a mapping.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.platform.events import EventBus
from qai.platform.time import Clock

from qai.channels.application.ports import (
    ChannelInstanceRepositoryPort,
    SessionIndexRepositoryPort,
)
from qai.channels.domain import (
    ChannelInstanceId,
    ChannelUserId,
    SessionIndexEntry,
    SessionIndexUpdatedEvent,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class BindSessionIndexCommand:
    instance_id: ChannelInstanceId
    channel_user_id: ChannelUserId
    internal_user_id: str | None = None
    coding_session_id: str | None = None


class BindSessionIndexUseCase:
    """Upsert a session-index entry and publish an update event.

    Asserts the referenced :class:`ChannelInstance` exists so callers
    can't bind orphan rows.
    """

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        sessions: SessionIndexRepositoryPort,
        events: EventBus,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._sessions = sessions
        self._events = events
        self._clock = clock

    async def execute(
        self, command: BindSessionIndexCommand
    ) -> SessionIndexEntry:
        # Raise ChannelInstanceNotFoundError if the instance is unknown.
        await self._instances.get(command.instance_id)
        entry = SessionIndexEntry(
            instance_id=command.instance_id,
            channel_user_id=command.channel_user_id,
            internal_user_id=command.internal_user_id,
            coding_session_id=command.coding_session_id,
            updated_at=self._clock.now(),
        )
        await self._sessions.upsert(entry)
        await self._events.publish(
            SessionIndexUpdatedEvent(
                instance_id=entry.instance_id.value,
                channel_user_id=entry.channel_user_id.value,
                internal_user_id=entry.internal_user_id,
                coding_session_id=entry.coding_session_id,
                updated_at=entry.updated_at,
            )
        )
        return entry


class LookupSessionIndexUseCase:
    """Read a session-index entry; return ``None`` when absent."""

    def __init__(
        self,
        *,
        sessions: SessionIndexRepositoryPort,
    ) -> None:
        self._sessions = sessions

    async def execute(
        self,
        instance_id: ChannelInstanceId,
        channel_user_id: ChannelUserId,
    ) -> SessionIndexEntry | None:
        return await self._sessions.find(instance_id, channel_user_id)


__all__ = [
    "BindSessionIndexCommand",
    "BindSessionIndexUseCase",
    "LookupSessionIndexUseCase",
]
