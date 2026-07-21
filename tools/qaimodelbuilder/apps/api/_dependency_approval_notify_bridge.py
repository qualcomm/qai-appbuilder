# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer WebUI notifier for dep_broker pending requests.

Restores V1 ``backend/security/dep_broker.py:191-203``'s
``_broadcast("dep_install_request", {...})``: when a dep-install command is
intercepted and enqueued, the WebUI must be told in real time so the approval
card pops without waiting for the next poll.

The notifier publishes a :class:`DepInstallRequestedEvent` on the platform
:class:`EventBus`; the global ``/api/events`` SSE route forwards it to the
browser as an ``event: dep_install_request`` frame. The bridge sits at the
apps composition root because it crosses bounded contexts (``qai.dependency_approval``
produces the request; ``qai.platform`` carries the event); the dep_broker BC
never imports the platform EventBus directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from qai.dependency_approval.domain import PendingRequest
from qai.platform.events import DomainEvent, EventBus
from qai.platform.logging import get_logger

_log = get_logger(__name__)

__all__ = ["DepInstallRequestedEvent", "EventBusDepInstallNotifier"]


@dataclass(frozen=True, slots=True, kw_only=True)
class DepInstallRequestedEvent(DomainEvent):
    """A dep-install command was intercepted and is awaiting approval.

    Wire ``type`` is ``dep_install_request`` (V1 SSE event name parity), so
    the WebUI listens for the same event key it always did. Carries value
    snapshots only (ids + plain strings) per the :class:`DomainEvent`
    contract.
    """

    event_type: ClassVar[str] = "dep_install_request"

    id: str
    command: str
    # Immutable value snapshot (DomainEvent contract): a tuple, not a mutable
    # list, so the published event cannot be aliased / mutated by a listener.
    denied_args: tuple[str, ...] = ()
    requester: str = "ai_coding.tool"
    created_at: datetime | None = None


class EventBusDepInstallNotifier:
    """``DepInstallNotifier`` that publishes onto the platform EventBus."""

    def __init__(self, *, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    async def __call__(self, request: PendingRequest) -> None:
        event = DepInstallRequestedEvent(
            id=request.id,
            command=request.command or " ".join(request.command_args),
            denied_args=tuple(request.denied_args),
            requester=request.requester,
            created_at=request.created_at,
        )
        try:
            await self._event_bus.publish(event)
        except Exception as exc:  # noqa: BLE001 — notify is best-effort
            _log.warning("dependency_approval.notify_failed", error=str(exc))
