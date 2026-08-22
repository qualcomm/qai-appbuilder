# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""EventBus-backed :class:`PermissionBroadcastPort` adapter (PR-501).

Translates use-case-level broadcast calls into
:class:`qai.security.domain.events.PermissionRequestedEvent` and
:class:`PermissionAskBlockedEvent` publications on the platform
:class:`qai.platform.events.EventBus`. Subscribers in
``apps/api/_permission_bridge.py`` (already wired in S3 PR-031) and
the SSE endpoint translate the events to their respective wire formats.

The legacy ``PolicyCenter._broadcast_callback`` was a synchronous
function pumping ``ask_user`` payloads onto SSE
(``backend/security/policy.py:1202-1270``). Promoting it to a typed
domain event preserves the same fan-out shape while keeping the
domain unaware of FastAPI / SSE concerns (§3.2 cross-context isolation
+ ``layered-security`` import-linter contract).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from qai.security.domain.entities import PermissionRequest
from qai.security.domain.events import (
    PermissionAskBlockedEvent,
    PermissionRequestedEvent,
)
from qai.security.domain.value_objects import Channel, Resource, Subject

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.events import EventBus
    from qai.platform.time import Clock

__all__ = ["EventBusPermissionBroadcast"]


class EventBusPermissionBroadcast:
    """Implementation of :class:`PermissionBroadcastPort` over EventBus."""

    __slots__ = ("_events", "_clock")

    def __init__(self, *, events: "EventBus", clock: "Clock") -> None:
        self._events = events
        self._clock = clock

    async def publish_permission_request(
        self,
        request: PermissionRequest,
        *,
        channel: Channel | None,
    ) -> None:
        # ``RequestPermissionUseCase`` already publishes the
        # :class:`PermissionRequestedEvent`; this adapter republishes
        # the exact same shape so the public domain event stays the
        # single source of truth (``channel`` is intentionally absent
        # from the event payload — the channel context is carried by
        # the SSE bridge on a per-connection basis). Republish is a
        # no-op fan-out: subscribers de-duplicate on ``request_id``.
        del channel  # honoured by the SSE bridge, not the event
        await self._events.publish(
            PermissionRequestedEvent(
                request_id=request.request_id,
                subject=request.subject,
                resource=request.resource,
                requested_mask=request.requested_mask,
                occurred_at=request.created_at,
            )
        )

    async def publish_ask_blocked(
        self,
        *,
        channel: Channel,
        subject: Subject,
        resource: Resource,
        reason: str,
    ) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty str")
        now: datetime = self._clock.now()
        await self._events.publish(
            PermissionAskBlockedEvent(
                channel_name=channel.name,
                subject=subject,
                resource=resource,
                reason=reason,
                occurred_at=now,
            )
        )
