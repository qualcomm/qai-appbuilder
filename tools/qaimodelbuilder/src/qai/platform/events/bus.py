# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Asyncio in-process event bus.

A subscription is an async function ``async def handler(envelope) -> None``.
Subscriptions are routed by either:
- exact ``DomainEvent`` subclass (``isinstance``), or
- glob-like topic pattern on ``event.event_type`` (e.g. ``"chat.*"``).

The bus serialises delivery per subscription via a bounded queue. If a
subscriber is slow or hung, only its own queue fills up and ``publish``
will eventually raise ``BackpressureError`` for that subscription rather
than blocking other subscribers.

Errors raised inside a handler are caught, logged via the optional
``on_subscriber_error`` callback (or the stdlib ``logging`` module if
none provided), and do NOT propagate to the publisher. This is the
"never let one bad listener take down the whole event flow" rule.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.platform.errors import InfrastructureError
from qai.platform.ids import new_ulid

from .types import DomainEvent, EventEnvelope

if TYPE_CHECKING:  # pragma: no cover
    pass


_log = logging.getLogger(__name__)

EventHandler = Callable[[EventEnvelope], Awaitable[None]]
SubscriberErrorHook = Callable[[EventEnvelope, BaseException], Awaitable[None] | None]


class BackpressureError(InfrastructureError):
    """Subscriber queue is full for too long; publish refused."""

    default_code = "events.backpressure"


@dataclass(slots=True)
class EventSubscription:
    """Handle returned by ``EventBus.subscribe``.

    Use ``await sub.unsubscribe()`` to detach the handler and drain its
    pending queue. After ``unsubscribe()`` the subscription is inert.
    """

    id: str
    event_type_filter: type[DomainEvent] | str
    handler: EventHandler
    queue_max: int = 256

    # internal — not part of the public contract
    _queue: asyncio.Queue[EventEnvelope | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=256)
    )
    _task: asyncio.Task[None] | None = None
    _closed: bool = False

    async def unsubscribe(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._task
        self._task = None
        if task is None:
            return
        # The worker task may have been created on a *different* event loop
        # than the one closing the bus — this happens when a nested
        # ``TestClient`` (its own loop) tears down an app whose EventBus
        # subscriptions were started on the outer loop.  Awaiting a task
        # bound to another loop raises ``RuntimeError: attached to a
        # different loop``.  Detect that case and cancel the foreign-loop
        # task without awaiting it on the wrong loop, so ``close()`` stays
        # robust regardless of which loop drives teardown.
        try:
            task_loop = task.get_loop()
        except Exception:  # noqa: BLE001 — defensive; treat as unknown loop
            task_loop = None
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if task_loop is not None and task_loop is not running_loop:
            # Foreign loop: signal the worker to exit and cancel without
            # cross-loop await.  ``call_soon_threadsafe`` is the only safe
            # way to touch another loop's objects.
            if not task.done():
                try:
                    task_loop.call_soon_threadsafe(task.cancel)
                except Exception:  # noqa: BLE001 — loop may be closed already
                    pass
            return
        # Same loop (normal path): poison-pill the worker and await exit.
        await self._queue.put(None)
        await task



class EventBus:
    """Asyncio in-process event bus.

    Each ``EventBus`` is independent: there is NO module-level singleton.
    Compose it through DI (``apps/api/di.py``) or pass explicitly.

    Parameters:
        on_subscriber_error: optional async/sync hook called when a
            subscriber raises. Defaults to logging via ``logging``.
        publish_timeout_s: max time ``publish`` will wait for a slow
            subscriber's queue before raising ``BackpressureError``.
        default_queue_size: per-subscription bounded queue size.
    """

    def __init__(
        self,
        *,
        on_subscriber_error: SubscriberErrorHook | None = None,
        publish_timeout_s: float = 1.0,
        default_queue_size: int = 256,
    ) -> None:
        if publish_timeout_s <= 0:
            raise ValueError("publish_timeout_s must be > 0")
        if default_queue_size <= 0:
            raise ValueError("default_queue_size must be > 0")
        self._subscriptions: list[EventSubscription] = []
        self._lock = asyncio.Lock()
        self._on_error = on_subscriber_error
        self._publish_timeout_s = publish_timeout_s
        self._default_queue_size = default_queue_size
        self._closed = False

    async def subscribe(
        self,
        event_type: type[DomainEvent] | str,
        handler: EventHandler,
        *,
        queue_size: int | None = None,
    ) -> EventSubscription:
        """Register an async handler.

        ``event_type`` may be a ``DomainEvent`` subclass (matched by
        ``isinstance``) or a string topic pattern matched against
        ``event.event_type`` via ``fnmatch`` (e.g. ``"chat.*"``).
        """
        if self._closed:
            raise InfrastructureError(
                "events.bus_closed",
                "Cannot subscribe to a closed EventBus",
            )
        size = queue_size if queue_size is not None else self._default_queue_size
        if size <= 0:
            raise ValueError("queue_size must be > 0")

        sub = EventSubscription(
            id=new_ulid(),
            event_type_filter=event_type,
            handler=handler,
            queue_max=size,
            _queue=asyncio.Queue(maxsize=size),
        )
        sub._task = asyncio.create_task(self._run_subscription(sub), name=f"event-sub-{sub.id}")

        async with self._lock:
            self._subscriptions.append(sub)
        return sub

    async def publish(
        self,
        event: DomainEvent,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
        headers: dict[str, Any] | None = None,
    ) -> EventEnvelope:
        """Publish an event to all matching subscribers.

        Returns the ``EventEnvelope`` actually delivered (useful for tests
        and audit logging). Delivery is **non-blocking and best-effort**: each
        subscriber's bounded queue is filled with ``put_nowait``; if a
        subscriber's queue is full the event is DROPPED for THAT subscriber
        only (reported via the ``on_subscriber_error`` hook / log) and delivery
        continues to the rest. ``publish`` never blocks on a slow consumer and
        never raises ``BackpressureError`` to the caller — a slow/stale listener
        must not slow down or take down the publisher or the whole event flow.
        Reliable-delivery consumers must drain their queue promptly or accept
        drops under sustained pressure.
        """
        if self._closed:
            raise InfrastructureError(
                "events.bus_closed",
                "Cannot publish to a closed EventBus",
            )

        envelope = EventEnvelope(
            id=new_ulid(),
            occurred_at=datetime.now(timezone.utc),
            event=event,
            request_id=request_id,
            correlation_id=correlation_id,
            headers=dict(headers) if headers else {},
        )

        # snapshot subscriptions to avoid holding the lock while delivering
        async with self._lock:
            # Lazily GC unsubscribed subscriptions. ``EventSubscription.
            # unsubscribe()`` poison-pills the worker task (so nothing ever
            # drains that subscription's queue again) and flips ``_closed``,
            # but — being a method on the subscription, which holds no
            # back-reference to the bus — it cannot de-register itself from
            # this list. Left registered, a closed subscription is still a
            # publish target: its orphaned 256-slot queue fills once and then
            # EVERY later publish raises QueueFull -> BackpressureError, which
            # is exactly the multi-thousand-line "events.backpressure" traceback
            # flood a leaked ``/api/events`` SSE/WS subscription produced. Prune
            # here (the one place that already holds the lock and is on the
            # bus's own loop) so a disconnected client's subscription stops
            # receiving events the moment it is closed.
            if any(s._closed for s in self._subscriptions):
                self._subscriptions = [
                    s for s in self._subscriptions if not s._closed
                ]
            targets = [s for s in self._subscriptions if _matches(s, event)]

        if not targets:
            return envelope

        for sub in targets:
            try:
                # NON-BLOCKING delivery: never let a slow/stale subscriber
                # block the publisher — not even for `publish_timeout_s`.
                #
                # Previously this did `await asyncio.wait_for(queue.put(...),
                # timeout=1.0)`. When a subscriber's queue was full and not
                # being drained (e.g. a leaked / unread `/api/events` SSE
                # subscription), EVERY publish blocked the publisher for a full
                # ~1s before timing out. The chat streaming hot path publishes
                # one `chat.streamed_frame` event PER FRAME *before* yielding the
                # frame to the client, so each token was delayed ~1s — the chat
                # appeared "extremely slow". (V1 has no such per-frame event-bus
                # hop at all; it yields straight to the HTTP response.)
                #
                # Honour this module's documented contract (header docstring
                # lines 8-16: "never let one bad listener take down the whole
                # event flow"): deliver best-effort. If a subscriber's bounded
                # queue is full, DROP the event for that subscriber (report via
                # the error hook) and move on instantly. Reliable-delivery
                # consumers must drain promptly or accept drops under pressure.
                sub._queue.put_nowait(envelope)
            except asyncio.QueueFull as exc:
                # Drop for THIS subscriber only; keep delivering to the rest and
                # never block / never fail the publisher.
                backpressure = BackpressureError(
                    "events.backpressure",
                    f"Subscriber {sub.id} queue full; event dropped",
                    details={
                        "operation": "publish",
                        "subscription_id": sub.id,
                        "queue_maxsize": sub._queue.maxsize,
                    },
                    cause=exc,
                )
                await self._notify_subscriber_error(envelope, backpressure)
        return envelope

    async def close(self) -> None:
        """Detach all subscriptions and wait for handler tasks to exit."""
        if self._closed:
            return
        self._closed = True
        async with self._lock:
            subs = list(self._subscriptions)
            self._subscriptions.clear()
        for sub in subs:
            await sub.unsubscribe()

    async def _run_subscription(self, sub: EventSubscription) -> None:
        while True:
            envelope = await sub._queue.get()
            if envelope is None:
                # poison pill — exit
                return
            try:
                await sub.handler(envelope)
            except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
                raise
            except BaseException as exc:  # noqa: BLE001 — bus must isolate failures
                await self._notify_subscriber_error(envelope, exc)

    async def _notify_subscriber_error(
        self,
        envelope: EventEnvelope,
        exc: BaseException,
    ) -> None:
        if self._on_error is None:
            _log.error(
                "Event subscriber raised: event=%s exc=%r",
                envelope.event_type,
                exc,
                exc_info=exc,
            )
            return
        try:
            result = self._on_error(envelope, exc)
            if asyncio.iscoroutine(result):
                await result
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as exc_in_hook:  # noqa: BLE001 — last-resort hook safety
            _log.error(
                "on_subscriber_error hook itself raised: hook_exc=%r original_exc=%r",
                exc_in_hook,
                exc,
            )


def _matches(sub: EventSubscription, event: DomainEvent) -> bool:
    f = sub.event_type_filter
    if isinstance(f, str):
        return fnmatch.fnmatchcase(event.event_type, f)
    return isinstance(event, f)
