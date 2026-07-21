# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Reject a pending permission request."""

from __future__ import annotations

import logging

from qai.platform.events import EventBus
from qai.platform.time import Clock

from qai.security.application.permission_wait import PermissionWaitRegistry
from qai.security.domain.entities import PermissionRequest
from qai.security.domain.errors import PermissionRequestNotFoundError
from qai.security.domain.events import PermissionRejectedEvent
from qai.security.domain.value_objects import RequestId, Subject

from ..ports import PermissionRequestRepositoryPort

__all__ = ["RejectPermissionUseCase"]

_log = logging.getLogger(__name__)


class RejectPermissionUseCase:
    """Transition a PermissionRequest from PENDING to REJECTED.

    P0 ASK restore — when wired with a :class:`PermissionWaitRegistry` the
    rejection also wakes the FileGuard ASK waiter blocked on this
    ``request_id`` with a DENY resolution (V1 ``pending.event.set()`` with
    ``Decision.DENY``). The registry is optional so existing callers keep
    working.
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
        # P-17 (2026-07-09): write the definitive DENY audit row here.
        self._audit_sink = audit_sink

    async def execute(
        self,
        *,
        request_id: RequestId,
        decided_by: Subject | None = None,
        reason: str = "",
    ) -> PermissionRequest:
        existing = await self._requests.get(request_id)
        if existing is None:
            raise PermissionRequestNotFoundError(request_id.value)
        now = self._clock.now()
        rejected = existing.reject(now=now, reason=reason)
        await self._requests.save(rejected)
        await self._events.publish(
            PermissionRejectedEvent(
                request_id=request_id,
                subject=existing.subject,
                resource=existing.resource,
                decided_by=decided_by,
                reason=reason,
                occurred_at=now,
            )
        )
        if self._wait_registry is not None:
            self._wait_registry.resolve(
                request_id.value, allow=False, scope="deny"
            )

        # P-17 (2026-07-09): write the definitive DENY audit row now that the
        # user has explicitly rejected the request. Best-effort.
        if self._audit_sink is not None:
            try:
                from qai.security.domain.entities import AuditEntry
                from qai.security.domain.value_objects import PolicyAction

                audit_id = f"reject-{request_id.value[:16]}"
                await self._audit_sink.append(
                    AuditEntry(
                        audit_id=audit_id,
                        occurred_at=now,
                        subject=existing.subject,
                        resource=existing.resource,
                        decision=PolicyAction.DENY,
                        rule_id=None,
                        correlation_id=None,
                        note="user_rejected",
                        channel=None,
                        op=existing.resource.kind,
                        process_path="",
                        command_line="",
                        actor_pid=None,
                        actor_parent_pid=None,
                    )
                )
            except Exception:  # noqa: BLE001 — audit failure must not block
                _log.warning(
                    "reject_permission: failed to write deny audit row "
                    "for request_id=%s — check AuditEntry construction / "
                    "audit_sink availability",
                    request_id.value,
                    exc_info=True,
                )

        return rejected
