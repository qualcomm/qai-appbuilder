# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: reassign the workspace of a live :class:`CodingSession`.

Implements the U1 decision in
``docs/90-refactor/S8-parity-audit.md`` §4 — relax the previously
immutable ``CodingSession.workspace`` invariant so the legacy
``POST /sessions/{id}/working_dir`` route can be re-implemented 1:1
against the new aggregate without forcing a session terminate-respawn
cycle.

Sequence (happy path)
---------------------
1. Load the session by id.
2. If ``new_workspace == session.workspace`` the call is a no-op:
   no lock churn, no event, no save.  This matches the legacy
   idempotent semantics.
3. Acquire the lock on the *new* workspace.  If acquisition fails
   (e.g. another session already holds it) the original session is
   left untouched and the error propagates verbatim
   (``WorkspaceLockedError``).
4. Mutate the aggregate via :meth:`CodingSession.change_workspace`,
   which queues a :class:`WorkspaceChangedEvent`.
5. Persist the aggregate.  If the save raises, **rollback** the lock
   swap by releasing the new workspace lock so retries see a clean
   state (the old lock was not yet released — see step 6).
6. Release the *old* workspace lock.
7. Drain & publish queued domain events.

Rollback story
--------------
The lock swap is intentionally ordered ``acquire(new) → save →
release(old)`` rather than ``release(old) → acquire(new)``: this
ensures that a failed acquire on the new workspace leaves the old
lock intact (no one can race in and steal the workspace from us
mid-mutation).  Save failures undo only the new acquire.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    CodingSessionRepositoryPort,
    WorkspaceLockPort,
)
from qai.ai_coding.domain import (
    CodingSessionId,
    Workspace,
)
from qai.platform.events import EventBus
from qai.platform.logging import get_logger
from qai.platform.time import Clock

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangeWorkspaceCommand:
    """Input parameters for :class:`ChangeWorkspaceUseCase`."""

    session_id: CodingSessionId
    new_workspace: Workspace


class ChangeWorkspaceUseCase:
    """Application service for swapping the workspace of a live session.

    The route layer (PR-104) wires
    ``POST /sessions/{id}/working_dir`` → this use case so the legacy
    behaviour is preserved 1:1.
    """

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        workspace_lock: WorkspaceLockPort,
        clock: Clock,
        event_bus: EventBus,
    ) -> None:
        self._repository = repository
        self._workspace_lock = workspace_lock
        self._clock = clock
        self._event_bus = event_bus

    async def execute(self, command: ChangeWorkspaceCommand) -> None:
        session = await self._repository.get(command.session_id)

        # No-op fast path: same workspace → no lock churn, no event.
        # Domain method already enforces this guard, but checking here
        # avoids a needless lock acquire/release round-trip.
        if command.new_workspace == session.workspace:
            logger.info(
                "ai_coding.change_workspace.noop",
                session_id=str(command.session_id),
                workspace=command.new_workspace.path,
            )
            # Still call the aggregate so any non-terminated guard
            # raises consistently (e.g. caller passes a terminated
            # session id with the same workspace).
            session.change_workspace(
                command.new_workspace, now=self._clock.now()
            )
            return

        old_workspace = session.workspace

        # Step 1: acquire NEW workspace lock first so a failure leaves
        # the OLD lock (and therefore the session) intact.  The port
        # raises ``WorkspaceLockedError`` on contention which the
        # caller surfaces as HTTP 409.
        await self._workspace_lock.acquire(command.new_workspace)

        # Step 2: mutate the aggregate + persist.  If save fails we
        # must release the freshly acquired lock so retries don't leak.
        try:
            session.change_workspace(
                command.new_workspace, now=self._clock.now()
            )
            await self._repository.save(session)
        except BaseException:
            await self._workspace_lock.release(command.new_workspace)
            raise

        # Step 3: release the OLD lock now that the new one is held
        # and the aggregate is persisted.  Lock release is documented
        # as idempotent so no try/except is needed here.
        await self._workspace_lock.release(old_workspace)

        # Step 4: publish queued events.
        for event in session.drain_events():
            await self._event_bus.publish(event)

        logger.info(
            "ai_coding.change_workspace.ok",
            session_id=str(command.session_id),
            old_workspace=old_workspace.path,
            new_workspace=command.new_workspace.path,
        )


__all__ = ["ChangeWorkspaceCommand", "ChangeWorkspaceUseCase"]
