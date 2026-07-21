# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: mark a :class:`CodingSession` as the active one.

Backs the legacy ``POST /api/cc/sessions/{id}/set_active`` route.
The legacy semantics ferry "this session is the focused tab" to
external channels (wechat / feishu) so notifications route to the
right session.  In the new architecture the channels lane (L7) owns
that bridge; this use case is a thin wrapper that:

* validates the session exists,
* refuses to operate on terminated sessions (the route layer
  surfaces 409),
* returns the session itself so the route layer can render the
  legacy ``{"ok": True, "session_id": ..., "active": True}`` shape.

No domain mutation is needed — the active-session marker is a
view-layer concept.  When the channels lane (L7) plugs in cross-BC
notifications, the bridge will subscribe to the
:class:`CodingSessionStatusChangedEvent` stream and infer the
"active" session from the most-recently-touched entry.  See §10 in
the PR-104a manifest for the cross-lane bridge pattern.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import CodingSessionRepositoryPort
from qai.ai_coding.domain import (
    CodingSession,
    CodingSessionAlreadyTerminatedError,
    CodingSessionId,
    SessionStatus,
)
from qai.platform.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class SetActiveSessionCommand:
    """Input for :class:`SetActiveSessionUseCase`."""

    session_id: CodingSessionId


class SetActiveSessionUseCase:
    """Application service for the active-session marker call."""

    def __init__(self, *, repository: CodingSessionRepositoryPort) -> None:
        self._repository = repository

    async def execute(
        self, command: SetActiveSessionCommand
    ) -> CodingSession:
        session = await self._repository.get(command.session_id)
        if session.status is SessionStatus.TERMINATED:
            raise CodingSessionAlreadyTerminatedError(
                message=(
                    f"coding session {command.session_id} is terminated; "
                    "cannot mark as active"
                ),
                details={"session_id": str(command.session_id)},
            )
        logger.info(
            "ai_coding.set_active_session.ok",
            session_id=str(command.session_id),
        )
        return session


__all__ = ["SetActiveSessionCommand", "SetActiveSessionUseCase"]
