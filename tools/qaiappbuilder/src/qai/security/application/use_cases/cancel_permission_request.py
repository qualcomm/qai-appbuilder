# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cancel a pending permission request (PR-040, issue d decision B).

This use case lets the **subject** withdraw their own permission request
before any reviewer acts on it. Only :class:`RequestState.PENDING`
requests are cancellable; the underlying
:meth:`PermissionRequest.cancel` already raises
:class:`PermissionRequestAlreadyResolvedError` if the request has been
approved / rejected / expired / cancelled previously.

Wire format on the HTTP boundary is ``DELETE
/api/security/permission/{request_id}`` (PR-040 routes/security.py).
"""

from __future__ import annotations

from qai.platform.events import EventBus
from qai.platform.time import Clock

from qai.security.application.permission_wait import PermissionWaitRegistry
from qai.security.domain.entities import PermissionRequest
from qai.security.domain.errors import PermissionRequestNotFoundError
from qai.security.domain.events import PermissionRequestCancelledEvent
from qai.security.domain.value_objects import RequestId, Subject

from ..ports import PermissionRequestRepositoryPort

__all__ = ["CancelPermissionRequestUseCase"]


class CancelPermissionRequestUseCase:
    """Transition a PermissionRequest from PENDING to CANCELLED.

    Group C (M-Sec-2): the cancel path now writes a definitive
    :class:`AuditEntry` when wired with an
    :class:`AuditSinkPort` — mirroring the approve / reject paths which
    each write their own terminal audit row.  Before this fix the audit
    trail had a hole: a user-initiated withdrawal of a permission request
    left no security-audit fact, so a reviewer could not distinguish
    "expired without response" from "user cancelled".  Best-effort: an
    audit-sink write failure never blocks the cancellation / wake.
    """

    def __init__(
        self,
        *,
        request_repository: PermissionRequestRepositoryPort,
        event_bus: EventBus,
        clock: Clock,
        wait_registry: PermissionWaitRegistry | None = None,
        audit_sink: object | None = None,
    ) -> None:
        self._requests = request_repository
        self._events = event_bus
        self._clock = clock
        self._wait_registry = wait_registry
        # M-Sec-2 (2026-07-28): optional collaborator so existing callers /
        # tests that don't wire an audit sink keep working byte-for-byte.
        self._audit_sink = audit_sink

    async def execute(
        self,
        *,
        request_id: RequestId,
        cancelled_by: Subject | None = None,
    ) -> PermissionRequest:
        """Cancel ``request_id`` and emit :class:`PermissionRequestCancelledEvent`.

        Raises:
            PermissionRequestNotFoundError: when no request with this id
                exists in the repository.
            PermissionRequestAlreadyResolvedError: when the request has
                already left the PENDING state (raised by the domain
                entity inside :meth:`PermissionRequest.cancel`).
        """

        existing = await self._requests.get(request_id)
        if existing is None:
            raise PermissionRequestNotFoundError(request_id.value)
        # Domain entity raises PermissionRequestAlreadyResolvedError
        # if state is not PENDING.
        now = self._clock.now()
        cancelled = existing.cancel(now=now)
        await self._requests.save(cancelled)
        await self._events.publish(
            PermissionRequestCancelledEvent(
                request_id=request_id,
                subject=existing.subject,
                resource=existing.resource,
                cancelled_by=cancelled_by,
                occurred_at=now,
            )
        )
        if self._wait_registry is not None:
            self._wait_registry.cancel(request_id.value)

        # M-Sec-2: definitive user-cancelled audit row. The user withdrew
        # the request BEFORE it was resolved, so the effect on the guarded
        # resource is DENY (no authorisation was granted).  Best-effort —
        # never block the cancellation on an audit-sink fault.
        if self._audit_sink is not None:
            try:
                from qai.security.domain.entities import AuditEntry
                from qai.security.domain.value_objects import PolicyAction

                audit_id = f"cancel-{request_id.value[:16]}"
                await self._audit_sink.append(  # type: ignore[attr-defined]
                    AuditEntry(
                        audit_id=audit_id,
                        occurred_at=now,
                        subject=existing.subject,
                        resource=existing.resource,
                        decision=PolicyAction.DENY,
                        rule_id=None,
                        correlation_id=None,
                        note="user_cancelled",
                        channel=None,
                        op=existing.resource.kind,
                        process_path="",
                        command_line="",
                        actor_pid=None,
                        actor_parent_pid=None,
                    )
                )
            except Exception:  # noqa: BLE001 — audit failure must not block
                import logging

                logging.getLogger(__name__).warning(
                    "cancel_permission_request: failed to write cancel audit "
                    "row for request_id=%s",
                    request_id.value,
                    exc_info=True,
                )

        return cancelled
