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
    """Transition a PermissionRequest from PENDING to CANCELLED."""

    def __init__(
        self,
        *,
        request_repository: PermissionRequestRepositoryPort,
        event_bus: EventBus,
        clock: Clock,
        wait_registry: PermissionWaitRegistry | None = None,
    ) -> None:
        self._requests = request_repository
        self._events = event_bus
        self._clock = clock
        self._wait_registry = wait_registry

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
        return cancelled
