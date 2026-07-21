# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: read the message history of a :class:`CodingSession`.

Backs the legacy ``GET /api/cc/sessions/{id}/history`` route.  The
legacy endpoint returns the list of message envelopes (id / role /
content / timestamp / source) but the new :class:`CodingSession`
aggregate intentionally stores only :class:`MessageContent` (text
only) on ``messages`` — richer per-turn metadata (assistant replies,
tool calls, source tags) flows through the SSE stream and the
events bus, NOT the aggregate.

This use case therefore returns the raw text history; the route
layer wraps each text entry into the legacy envelope shape with
``role="user"`` and a synthetic ``id`` / ``timestamp`` so the wire
contract remains 1:1 with the legacy clients.  Assistant messages
will be merged in when the chat-side persistence (PR-105 OC twins
land) ferries them through the event bus into a future history
projection.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import CodingSessionRepositoryPort
from qai.ai_coding.domain import (
    CodingSessionId,
    MessageContent,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class GetSessionHistoryQuery:
    """Input for :class:`GetSessionHistoryUseCase`."""

    session_id: CodingSessionId


@dataclass(frozen=True, slots=True, kw_only=True)
class GetSessionHistoryResult:
    """Output for :class:`GetSessionHistoryUseCase`."""

    session_id: CodingSessionId
    messages: tuple[MessageContent, ...]


class GetSessionHistoryUseCase:
    """Application service for reading a session's message history."""

    def __init__(self, *, repository: CodingSessionRepositoryPort) -> None:
        self._repository = repository

    async def execute(
        self, query: GetSessionHistoryQuery
    ) -> GetSessionHistoryResult:
        session = await self._repository.get(query.session_id)
        return GetSessionHistoryResult(
            session_id=session.session_id,
            messages=tuple(session.messages),
        )


__all__ = [
    "GetSessionHistoryQuery",
    "GetSessionHistoryResult",
    "GetSessionHistoryUseCase",
]
