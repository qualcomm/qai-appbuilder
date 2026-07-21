# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: start / stop a :class:`ChannelInstance`.

Both follow the same pattern:

1. Load aggregate from repository.
2. Move state-machine into a transient state (``starting`` / ``stopping``).
3. Call the kind-specific :class:`ChannelTransportPort` (resolved through
   a factory so the use case never imports a concrete adapter).
4. On success — move to terminal state, save, publish event.
5. On failure — move to ``error`` state, save, publish
   :class:`ChannelErrorEvent`, re-raise.

PR-097 R-5 — inbound long-poll lifecycle (personal WeChat / Feishu WS)
---------------------------------------------------------------------
Personal WeChat (wechatbot SDK long-poll) and Feishu (lark_oapi WS)
require a long-lived inbound transport on top of the outbound HTTPS
shape.  Two optional collaborators were added:

* ``inbound_transport_factory`` — returns an
  :class:`InboundTransportPort` for an instance, or ``None`` when the
  kind has no inbound long-poll (i.e. webhook-only providers).
* ``inbound_consumer`` — a callable spawned as a per-instance
  background task draining ``inbound_transport.stream(instance)``;
  the consumer pushes each :class:`ChannelMessage` into the dispatch
  bridge.

A :class:`~qai.channels.infrastructure.transport_watchdog.TransportWatchdog`
supervises the inbound transport with the legacy 5/10/30/60/120s
backoff (matches ``backend/channels/wechat/channel.py:834
_watchdog_loop``).

Both collaborators are optional so existing routes (HTTP-only
providers) continue to work unchanged — passing ``None`` reproduces
the pre-PR-097 behaviour.

Failures land in ``error`` rather than rolling back to ``stopped`` /
``running`` so operators can see *what went wrong* in the UI before
acknowledging.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Callable

from qai.platform.events import EventBus
from qai.platform.logging import get_logger
from qai.platform.time import Clock

from qai.channels.application.ports import (
    ChannelInstanceRepositoryPort,
    ChannelTransportPort,
    CredentialsResolverPort,
    InboundTransportPort,
)
from qai.channels.domain import (
    ChannelErrorEvent,
    ChannelInstance,
    ChannelInstanceId,
    ChannelMessage,
    ChannelStartedEvent,
    ChannelStoppedEvent,
    ChannelAcknowledgedEvent,
)

logger = get_logger(__name__)

TransportFactory = Callable[[ChannelInstance], ChannelTransportPort]
InboundTransportFactory = Callable[
    [ChannelInstance], InboundTransportPort | None
]
InboundConsumer = Callable[
    [ChannelInstance, ChannelMessage], Awaitable[None]
]
WatchdogStarter = Callable[
    [ChannelInstance, InboundTransportPort], "Awaitable[None] | None"
]
WatchdogStopper = Callable[[ChannelInstance], "Awaitable[None] | None"]


# ---------------------------------------------------------------------------
# Per-instance inbound bookkeeping (module-level so start + stop share state)
# ---------------------------------------------------------------------------
#: Map :class:`ChannelInstanceId` value → consumer asyncio.Task.
#: Module-level because :class:`StartChannelInstanceUseCase` and
#: :class:`StopChannelInstanceUseCase` are constructed independently
#: by the DI builder; sharing state via the use-case instance would
#: require both to be constructed together.  The map is process-local
#: (one event loop per FastAPI app); concurrent start/stop on the
#: same instance is serialised by the channel state machine.
_INBOUND_CONSUMERS: dict[str, asyncio.Task[None]] = {}


def _inbound_task_key(instance: ChannelInstance) -> str:
    return instance.instance_id.value


async def _drain_inbound_stream(
    *,
    instance: ChannelInstance,
    inbound_transport: InboundTransportPort,
    consumer: InboundConsumer,
) -> None:
    """Run :meth:`InboundTransportPort.stream` and call the consumer.

    The watchdog handles SDK-level reconnects; this task simply
    iterates the transport's stream until the transport is stopped
    (the iterator terminates cleanly per Protocol contract) or the
    task is cancelled by :class:`StopChannelInstanceUseCase`.
    """
    try:
        async for msg in inbound_transport.stream(instance):
            try:
                await consumer(instance, msg)
            except Exception as exc:  # noqa: BLE001
                # A consumer error must not kill the whole inbound
                # loop — surface it to the operator log and continue.
                logger.error(
                    "channels.inbound.consumer_error",
                    instance_id=instance.instance_id.value,
                    kind=instance.kind.value,
                    error=str(exc),
                    exc_info=True,
                )
    except asyncio.CancelledError:
        # Stop use case cancelled us — propagate so the awaiter sees
        # the cancellation and we don't accidentally swallow it.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "channels.inbound.stream_terminated",
            instance_id=instance.instance_id.value,
            kind=instance.kind.value,
            error=str(exc),
            exc_info=True,
        )


async def cancel_all_inbound_consumers() -> None:
    """Cancel and await *every* registered inbound consumer task (R-5).

    The per-instance :class:`StopChannelInstanceUseCase` cancels its own
    consumer on an explicit Stop, but on app shutdown there is no
    guarantee every running instance was explicitly stopped. This
    module-level sweep lets the ``lifespan`` finally drain any residual
    inbound long-poll / WS consumer tasks so they do not outlive the
    process (orphaned coroutines / leaked SDK connections).

    Best-effort + idempotent: a second call is a no-op once the map is
    empty. Only cancels inbound consumer tasks — the per-kind watchdog
    (if any) is an independent task tracked elsewhere.
    """
    if not _INBOUND_CONSUMERS:
        return
    tasks = list(_INBOUND_CONSUMERS.items())
    _INBOUND_CONSUMERS.clear()
    for key, task in tasks:
        if task.done():
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — shutdown best-effort
            logger.warning(
                "channels.inbound.consumer_drain_failed",
                instance_id=key,
                error=str(exc),
            )


class StartChannelInstanceUseCase:
    """Bring an instance from ``stopped`` to ``running``.

    PR-097 R-5: when the kind has an inbound long-poll transport
    (personal WeChat / Feishu WS), the use case additionally:

    * Calls :meth:`InboundTransportPort.start`.
    * Spawns the per-instance consumer task that drains
      :meth:`InboundTransportPort.stream` and forwards each message
      to the injected ``inbound_consumer`` callback.
    * Invokes the watchdog starter so reconnect supervision begins.
    """

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        credentials: CredentialsResolverPort,
        transport_factory: TransportFactory,
        events: EventBus,
        clock: Clock,
        inbound_transport_factory: InboundTransportFactory | None = None,
        inbound_consumer: InboundConsumer | None = None,
        watchdog_starter: WatchdogStarter | None = None,
    ) -> None:
        self._instances = instances
        self._credentials = credentials
        self._transport_factory = transport_factory
        self._events = events
        self._clock = clock
        self._inbound_transport_factory = inbound_transport_factory
        self._inbound_consumer = inbound_consumer
        self._watchdog_starter = watchdog_starter

    async def execute(
        self, instance_id: ChannelInstanceId
    ) -> ChannelInstance:
        instance = await self._instances.get(instance_id)
        starting = instance.request_start(now=self._clock.now())
        await self._instances.save(starting)
        try:
            secret = await self._credentials.resolve(
                starting.credentials_ref
            )
            transport = self._transport_factory(starting)
            await transport.start(starting, secret)
            # PR-097 R-5: bring up the inbound long-poll loop, if any.
            if (
                self._inbound_transport_factory is not None
                and self._inbound_consumer is not None
            ):
                inbound = self._inbound_transport_factory(starting)
                if inbound is not None:
                    await inbound.start(starting, secret)
                    consumer = self._inbound_consumer
                    task = asyncio.create_task(
                        _drain_inbound_stream(
                            instance=starting,
                            inbound_transport=inbound,
                            consumer=consumer,
                        )
                    )
                    _INBOUND_CONSUMERS[_inbound_task_key(starting)] = task
                    if self._watchdog_starter is not None:
                        result = self._watchdog_starter(starting, inbound)
                        if asyncio.iscoroutine(result):
                            await result
        except Exception as exc:
            failed = starting.mark_error(
                now=self._clock.now(),
                reason=f"{type(exc).__name__}: {exc}",
            )
            await self._instances.save(failed)
            await self._events.publish(
                ChannelErrorEvent(
                    instance_id=failed.instance_id.value,
                    kind=failed.kind,
                    previous_status=starting.status,
                    reason=failed.last_error,
                    occurred_at=failed.updated_at,
                )
            )
            logger.error(
                "channels.start_failed",
                instance_id=failed.instance_id.value,
                kind=failed.kind.value,
                reason=failed.last_error,
            )
            raise
        running = starting.mark_running(now=self._clock.now())
        await self._instances.save(running)
        await self._events.publish(
            ChannelStartedEvent(
                instance_id=running.instance_id.value,
                kind=running.kind,
                started_at=running.updated_at,
            )
        )
        return running


class StopChannelInstanceUseCase:
    """Bring an instance from ``running`` to ``stopped``.

    PR-097 R-5: when an inbound long-poll loop was spawned by
    :class:`StartChannelInstanceUseCase`, the stop path:

    * Cancels the consumer task and awaits its cleanup.
    * Calls :meth:`InboundTransportPort.stop`.
    * Invokes the watchdog stopper so the supervisor task exits.
    """

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        transport_factory: TransportFactory,
        events: EventBus,
        clock: Clock,
        inbound_transport_factory: InboundTransportFactory | None = None,
        watchdog_stopper: WatchdogStopper | None = None,
    ) -> None:
        self._instances = instances
        self._transport_factory = transport_factory
        self._events = events
        self._clock = clock
        self._inbound_transport_factory = inbound_transport_factory
        self._watchdog_stopper = watchdog_stopper

    async def execute(
        self, instance_id: ChannelInstanceId
    ) -> ChannelInstance:
        instance = await self._instances.get(instance_id)
        stopping = instance.request_stop(now=self._clock.now())
        await self._instances.save(stopping)
        try:
            transport = self._transport_factory(stopping)
            await transport.stop(stopping)
            # PR-097 R-5: tear the inbound loop down (best-effort).
            key = _inbound_task_key(stopping)
            task = _INBOUND_CONSUMERS.pop(key, None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "channels.inbound.consumer_drain_failed",
                        instance_id=stopping.instance_id.value,
                        error=str(exc),
                    )
            if self._inbound_transport_factory is not None:
                inbound = self._inbound_transport_factory(stopping)
                if inbound is not None:
                    try:
                        await inbound.stop(stopping)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "channels.inbound.stop_failed",
                            instance_id=stopping.instance_id.value,
                            error=str(exc),
                        )
            if self._watchdog_stopper is not None:
                result = self._watchdog_stopper(stopping)
                if asyncio.iscoroutine(result):
                    try:
                        await result
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "channels.watchdog.stop_failed",
                            instance_id=stopping.instance_id.value,
                            error=str(exc),
                        )
        except Exception as exc:
            failed = stopping.mark_error(
                now=self._clock.now(),
                reason=f"{type(exc).__name__}: {exc}",
            )
            await self._instances.save(failed)
            await self._events.publish(
                ChannelErrorEvent(
                    instance_id=failed.instance_id.value,
                    kind=failed.kind,
                    previous_status=stopping.status,
                    reason=failed.last_error,
                    occurred_at=failed.updated_at,
                )
            )
            logger.error(
                "channels.stop_failed",
                instance_id=failed.instance_id.value,
                kind=failed.kind.value,
                reason=failed.last_error,
            )
            raise
        stopped = stopping.mark_stopped(now=self._clock.now())
        await self._instances.save(stopped)
        await self._events.publish(
            ChannelStoppedEvent(
                instance_id=stopped.instance_id.value,
                kind=stopped.kind,
                stopped_at=stopped.updated_at,
            )
        )
        return stopped


class AcknowledgeChannelErrorUseCase:
    """Acknowledge an ``error`` state, moving the instance back to ``stopped``.

    The domain requires an explicit user acknowledgement before a
    channel in ``error`` can be restarted (see
    :meth:`~qai.channels.domain.instance.ChannelInstance.acknowledge_error`).
    This use case implements that acknowledgement:

    1. Load the aggregate.
    2. Call :meth:`acknowledge_error` (raises
       :class:`~qai.channels.domain.errors.ChannelInstanceStateError` if not
       in ``error`` state — the caller / route layer maps that to 409).
    3. Persist the updated aggregate.
    4. Publish a :class:`ChannelAcknowledgedEvent` so the WebUI live-update
       broadcaster can clear the error badge.
    """

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        events: EventBus,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._events = events
        self._clock = clock

    async def execute(
        self, instance_id: ChannelInstanceId
    ) -> ChannelInstance:
        instance = await self._instances.get(instance_id)
        acknowledged = instance.acknowledge_error(now=self._clock.now())
        await self._instances.save(acknowledged)
        await self._events.publish(
            ChannelAcknowledgedEvent(
                instance_id=acknowledged.instance_id.value,
                kind=acknowledged.kind,
                acknowledged_at=acknowledged.updated_at,
            )
        )
        logger.info(
            "channels.error_acknowledged",
            instance_id=acknowledged.instance_id.value,
            kind=acknowledged.kind.value,
        )
        return acknowledged


__all__ = [
    "TransportFactory",
    "InboundTransportFactory",
    "InboundConsumer",
    "WatchdogStarter",
    "WatchdogStopper",
    "StartChannelInstanceUseCase",
    "StopChannelInstanceUseCase",
    "AcknowledgeChannelErrorUseCase",
    "cancel_all_inbound_consumers",
]
