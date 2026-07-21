# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: soft-interrupt a live :class:`CodingSession`.

Backs the legacy ``POST /api/cc/sessions/{id}/interrupt`` route.

Sequence (happy path)
---------------------
1. Load the session by id.
2. If the session is ``TERMINATED`` raise
   :class:`CodingSessionAlreadyTerminatedError` (route surfaces 410).
3. If the session is already ``IDLE`` return early with
   ``interrupted=False`` — there is no in-flight turn to cancel.
4. Ask the provider to drop the live stream / subprocess via a
   provider-specific interrupt path (we use
   :meth:`CodingProviderPort.terminate` since the port has no
   dedicated soft-interrupt yet — the legacy "soft" semantic is
   preserved at the domain level by transitioning back to ``IDLE``
   without resetting permission grants).
5. Domain :meth:`CodingSession.interrupt` flips the status to
   ``IDLE`` and queues a :class:`CodingSessionInterruptedEvent`.
6. Persist the aggregate.
7. Drain & publish queued domain events.

Returns
-------
``InterruptSessionResult`` so the route layer can preserve the
legacy wire shape ``{"ok": True, "interrupted": True}`` /
``{"ok": False, "reason": "..."}``.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    CodingProviderPort,
    CodingSessionRepositoryPort,
)
from qai.ai_coding.domain import (
    CodingSessionAlreadyTerminatedError,
    CodingSessionId,
    SessionStatus,
)
from qai.platform.events import EventBus
from qai.platform.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class InterruptSessionCommand:
    """Input for :class:`InterruptSessionUseCase`."""

    session_id: CodingSessionId


@dataclass(frozen=True, slots=True, kw_only=True)
class InterruptSessionResult:
    """Outcome of the interrupt call.

    * ``interrupted`` — :data:`True` iff there was a live turn to
      cancel; :data:`False` when the session was already idle.
    * ``reason`` — human-readable diagnostic when ``interrupted`` is
      :data:`False`; the route layer surfaces it in the
      ``{"ok": false, "reason": "..."}`` legacy shape.
    """

    interrupted: bool
    reason: str | None = None


class InterruptSessionUseCase:
    """Application service for soft-interrupting an in-flight turn."""

    def __init__(
        self,
        *,
        provider_port: CodingProviderPort,
        repository: CodingSessionRepositoryPort,
        event_bus: EventBus,
    ) -> None:
        self._provider_port = provider_port
        self._repository = repository
        self._event_bus = event_bus

    async def execute(
        self, command: InterruptSessionCommand
    ) -> InterruptSessionResult:
        session = await self._repository.get(command.session_id)

        if session.status is SessionStatus.TERMINATED:
            raise CodingSessionAlreadyTerminatedError(
                message=(
                    f"coding session {command.session_id} is terminated; "
                    "cannot interrupt"
                ),
                details={"session_id": str(command.session_id)},
            )

        if session.status is SessionStatus.IDLE:
            # No live turn — return the legacy "ok=False" envelope
            # shape via :class:`InterruptSessionResult` without
            # touching the provider or the aggregate.
            return InterruptSessionResult(
                interrupted=False,
                reason="No running task to interrupt (session is not busy)",
            )

        # Best-effort provider-side termination of the in-flight
        # stream.  ``CodingProviderPort.terminate`` is documented as
        # idempotent and tolerates "already gone".  Errors are logged
        # but do not block the soft-interrupt — the aggregate's
        # status flip is what unblocks the user.
        try:
            await self._provider_port.terminate(session_id=command.session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ai_coding.interrupt_session.provider_error",
                session_id=str(command.session_id),
                error=repr(exc),
            )

        session.interrupt()
        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)

        logger.info(
            "ai_coding.interrupt_session.ok",
            session_id=str(command.session_id),
        )
        return InterruptSessionResult(interrupted=True)


__all__ = [
    "InterruptSessionCommand",
    "InterruptSessionResult",
    "InterruptSessionUseCase",
]
