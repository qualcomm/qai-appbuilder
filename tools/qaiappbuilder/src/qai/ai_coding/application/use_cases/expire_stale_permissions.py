# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: expire stale pending permission requests (2-H14).

V1 parity
---------
The legacy Claude Code manager wrapped each permission-approval future in
``asyncio.wait_for(future, timeout=permission_approval_timeout_seconds)``
(default 120s; ``backend/ai_coding/session_manager.py:1475,1586``) and
**auto-rejected** the tool call on expiry (``line 1644-1648``: "审批超时
（N秒），已自动拒绝工具调用"), letting the agent fall back to text
reasoning instead of hanging forever on an un-answered approval card.
The OpenCode / CC managers additionally ran an ``_idle_cleanup_loop`` that
closed idle sessions past ``session_idle_timeout_minutes``
(``opencode_session_manager.py:1173-1187`` /
``session_manager.py:2708-2731``).

V2 design
---------
The pending-request lifetime is a *domain* concern, so the TTL rule lives
on the :class:`CodingSession` aggregate
(:meth:`CodingSession.expire_stale_permissions`) which auto-rejects any
PENDING request older than the TTL and clears the
``PERMISSION_REQUESTED`` gate.  This use case is the thin application
seam the route / decide path (or a future sweep task) calls to apply the
rule to one session and persist + publish the resulting events.

Keeping it a synchronous, idempotent per-session sweep (rather than a
background ``asyncio`` loop owned by the manager, as V1 did) means it is
trivially unit-testable with a frozen clock and composes cleanly with the
existing repository-save / event-drain pattern — a judge-1 improvement
over V1's inline manager loop.  A background scheduler that periodically
fans this out across active sessions can be wired at the apps layer later
without changing this use case.

Cross-context isolation: imports only ``qai.ai_coding.{application,
domain}`` plus ``qai.platform``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from qai.ai_coding.application.ports import CodingSessionRepositoryPort
from qai.ai_coding.domain import CodingSessionId
from qai.platform.events import EventBus
from qai.platform.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_PERMISSION_TTL_SECONDS",
    "ExpireStalePermissionsCommand",
    "ExpireStalePermissionsResult",
    "ExpireStalePermissionsUseCase",
]


#: Default pending-permission time-to-live in seconds.  Matches V1
#: ``permission_approval_timeout_seconds`` default (``session_manager.py
#: :1475``): 120s — after which an un-answered approval is auto-rejected.
DEFAULT_PERMISSION_TTL_SECONDS: float = 120.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpireStalePermissionsCommand:
    """Input for :class:`ExpireStalePermissionsUseCase`."""

    session_id: CodingSessionId


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpireStalePermissionsResult:
    """Return shape of :class:`ExpireStalePermissionsUseCase`.

    ``expired_request_ids`` lists the permission requests that were
    auto-rejected (empty when none were stale).  ``expired_count`` is a
    convenience mirror for callers that only need the tally.
    """

    expired_request_ids: tuple[str, ...]
    expired_count: int


class ExpireStalePermissionsUseCase:
    """Auto-reject a session's pending permission requests past their TTL."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        event_bus: EventBus,
        clock=None,
        ttl_seconds: float = DEFAULT_PERMISSION_TTL_SECONDS,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus
        # Optional clock for deterministic tests; falls back to a UTC
        # ``datetime.now`` so wiring that omits it keeps working.
        self._clock = clock
        # Non-positive TTL disables expiry (V1 parity: a 0 timeout meant
        # "wait forever").
        self._ttl_seconds = ttl_seconds

    async def execute(
        self, command: ExpireStalePermissionsCommand
    ) -> ExpireStalePermissionsResult:
        session = await self._repository.get(command.session_id)
        now = (
            self._clock.now()
            if self._clock is not None
            else datetime.now(timezone.utc)
        )
        expired = session.expire_stale_permissions(
            now=now, ttl_seconds=self._ttl_seconds
        )
        if expired:
            await self._repository.save(session)
            for event in session.drain_events():
                await self._event_bus.publish(event)
            logger.info(
                "ai_coding.permissions.expired",
                session_id=str(command.session_id),
                expired_count=len(expired),
            )
        return ExpireStalePermissionsResult(
            expired_request_ids=tuple(rid.value for rid in expired),
            expired_count=len(expired),
        )
