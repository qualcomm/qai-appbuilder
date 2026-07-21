# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Event types: ``DomainEvent`` base + transport ``EventEnvelope``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Base class for in-process domain events.

    Subclasses are typically frozen dataclasses with ``slots=True`` and
    ``kw_only=True``. They MUST NOT carry references to live mutable
    application objects (use ids + value snapshots instead) so that
    subscribers can be scheduled later without race conditions.

    Subclasses should override ``event_type`` with a stable string id used
    by topic-based routing and by audit/logging.
    """

    # Override in subclasses, e.g. ``event_type = "chat.message_sent"``.
    event_type: ClassVar[str] = "platform.unspecified_event"


@dataclass(frozen=True, slots=True, kw_only=True)
class EventEnvelope:
    """Transport wrapper added by the bus when an event is published.

    Carries metadata required by subscribers and by logging:

    - ``id`` — unique id of this delivery (NOT of the event payload).
    - ``occurred_at`` — when the event was published (tz-aware UTC).
    - ``event`` — the underlying ``DomainEvent`` payload.
    - ``request_id`` / ``correlation_id`` — propagation hints for log/trace
      correlation across context boundaries.
    - ``attempt`` — 1-based retry counter; bumped by the bus if it ever
      retries (current implementation does not retry, kept for forward
      compatibility with persistent transports).
    """

    id: str
    occurred_at: datetime
    event: DomainEvent
    request_id: str | None = None
    correlation_id: str | None = None
    attempt: int = 1
    headers: dict[str, Any] = field(default_factory=dict)

    @property
    def event_type(self) -> str:
        return self.event.event_type
