# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.platform.events — In-process asyncio event bus.

Used as the cross-context coordination primitive (Clean Architecture: domain
events). Bounded contexts publish events; subscribers in other contexts (or
in the same one) react asynchronously without taking a hard import dependency.

Design constraints (refactor-plan v2.5 §6 / §7 / §15.1):
- No module-level mutable state — callers create explicit ``EventBus`` instances.
- Subscribers run on the asyncio event loop; failures in one subscriber must
  not prevent other subscribers from running.
- Backpressure: subscribers receive events one at a time (per-subscription
  serialised). The bus does not buffer beyond a small bounded queue.
- Type-safe: events are simple dataclasses; subscribers declare the event
  type they want.
"""

from __future__ import annotations

from .bus import EventBus, EventSubscription
from .types import DomainEvent, EventEnvelope

__all__ = [
    "DomainEvent",
    "EventBus",
    "EventEnvelope",
    "EventSubscription",
]
