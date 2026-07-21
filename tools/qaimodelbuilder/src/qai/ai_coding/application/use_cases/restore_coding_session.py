# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: restore a previously-terminated :class:`CodingSession`.

Backs the legacy ``POST /api/cc/sessions/{id}/restore`` route.  The
aggregate transitions from ``TERMINATED`` back to ``ACTIVE``; the
legacy ``fork`` flag is ferried through to the emitted event so
provider adapters can fork their backend session on the next message.

Sequence (happy path)
---------------------
1. Load the session by id.
2. Domain :meth:`CodingSession.restore` flips the status (no-op when
   the session is already non-terminated) and queues a
   :class:`CodingSessionRestoredEvent`.
3. Acquire the workspace lock for the session's current workspace
   (the lock was released on terminate; we must re-acquire so a
   second restore on the same workspace contends).
4. Persist the aggregate.  If save fails, release the freshly
   acquired lock so retries don't leak.
5. Drain & publish queued domain events.

Idempotency
-----------
Restoring an already-active session is a no-op on the aggregate;
the use case still acquires the lock to keep the post-condition
("workspace lock held by this session") consistent.  If the lock
is already held it raises :class:`WorkspaceLockedError` which the
route layer surfaces as HTTP 409.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    CodingProviderPort,
    CodingSessionRepositoryPort,
    WorkspaceLockPort,
)
from qai.ai_coding.domain import (
    CodingSession,
    CodingSessionId,
    SessionStatus,
)
from qai.platform.events import EventBus
from qai.platform.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreCodingSessionCommand:
    """Input for :class:`RestoreCodingSessionUseCase`."""

    session_id: CodingSessionId
    fork: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class RestoreCodingSessionResult:
    """Outcome of the restore call.

    * ``session`` — the (possibly mutated) aggregate, ready for the
      route layer to render via :func:`_session_to_response`.
    * ``restored`` — :data:`True` iff the aggregate transitioned from
      ``TERMINATED`` → ``ACTIVE``; :data:`False` when the session
      was already non-terminated.
    * ``forked`` — echoes the input ``fork`` flag so the route layer
      can include it in the response without re-reading the command.
    """

    session: CodingSession
    restored: bool
    forked: bool


class RestoreCodingSessionUseCase:
    """Application service for re-activating a closed coding session."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        workspace_lock: WorkspaceLockPort,
        event_bus: EventBus,
        provider_port: CodingProviderPort | None = None,
    ) -> None:
        self._repository = repository
        self._workspace_lock = workspace_lock
        self._event_bus = event_bus
        # 2-H6: optional provider hook so a ``fork=True`` restore forks a
        # real new upstream session (drops the cached upstream id).  Kept
        # optional (default ``None``) so DI wiring that omits it keeps
        # working — the aggregate-level restore + ``forked`` event are
        # unchanged; only the provider-side fork is skipped when absent.
        self._provider_port = provider_port

    async def execute(
        self, command: RestoreCodingSessionCommand
    ) -> RestoreCodingSessionResult:
        session = await self._repository.get(command.session_id)
        was_terminated = session.status is SessionStatus.TERMINATED

        if not was_terminated:
            # Already active: idempotent no-op.  Do NOT touch the
            # workspace lock — the original spawn / restore call
            # already holds it.
            logger.info(
                "ai_coding.restore_coding_session.noop",
                session_id=str(command.session_id),
            )
            return RestoreCodingSessionResult(
                session=session,
                restored=False,
                forked=command.fork,
            )

        # Terminated → re-acquire the workspace lock first so a
        # contending restore on the same workspace fails before we
        # mutate the aggregate.
        await self._workspace_lock.acquire(session.workspace)

        try:
            session.restore(forked=command.fork)
            await self._repository.save(session)
        except BaseException:
            await self._workspace_lock.release(session.workspace)
            raise

        # 2-H6: when forking, drop the cached upstream session id so the
        # next turn lazily creates a brand-new backend conversation
        # instead of resuming the old one (V1 ``fork_session`` parity).
        # Best-effort — a provider that has no live handle / no upstream
        # id simply reports ``False`` and the restore still succeeds.
        if command.fork and self._provider_port is not None:
            try:
                await self._provider_port.fork_session(
                    session_id=command.session_id
                )
            except Exception as exc:  # noqa: BLE001 — fork is best-effort
                logger.warning(
                    "ai_coding.restore_coding_session.fork_failed",
                    session_id=str(command.session_id),
                    error=repr(exc),
                )

        for event in session.drain_events():
            await self._event_bus.publish(event)

        logger.info(
            "ai_coding.restore_coding_session.ok",
            session_id=str(command.session_id),
            forked=command.fork,
        )
        return RestoreCodingSessionResult(
            session=session,
            restored=True,
            forked=command.fork,
        )


__all__ = [
    "RestoreCodingSessionCommand",
    "RestoreCodingSessionResult",
    "RestoreCodingSessionUseCase",
]
