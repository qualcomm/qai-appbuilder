# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Real-time delivery service with 3-layer fallback (S9 PR-093 §2.2 H-14).

Restores the production-grade real-time channel delivery semantics
that the legacy ``backend/channels/wechat/cc_handler.py:379-870``
provided for long-running tool tasks: a *typing keepalive* timer
plus a *3-layer delivery fallback chain* gated on a *context_token
age guard* configured via
:attr:`~qai.platform.config.settings.ChannelsSettings.context_token_age_guard_seconds`
(default 90s).  Without these the new architecture would drop CC
results silently after the WeChat ``msg._context_token`` expired —
addressed by parity-audit row §2.2 H-14.

Three-layer fallback chain
--------------------------

1. **Layer 1 — In-context reply** (``bot.reply``): the canonical
   path; uses the still-fresh ``msg._context_token`` carried on the
   inbound event.  Works for messages delivered within the
   ``context_token_age_guard_seconds`` window.
2. **Layer 2 — Out-of-context push** (``send_to_user``): when the
   token is past the age guard, or the in-context reply raised an
   API error, we fall back to a separate token (the rolling token
   refreshed by every successful outbound send).  This window can
   exceed 10 minutes in practice, far beyond the inbound token TTL.
3. **Layer 3 — Pending queue**: if the out-of-context push *also*
   fails (very rare; happens when both tokens expire concurrently),
   we stash the message in a per-user pending queue.  PR-097
   (S9 §6 R-20) makes this queue *persistent* — :class:`PendingMessageQueue`
   delegates to a :class:`PendingMessageStorePort` so a server
   restart no longer drops Layer-3 messages.  The next time the
   user sends a message, the dispatch bridge flushes the queue
   first via the fresh token in the new inbound event.

Typing keepalive
----------------
A background coroutine sends "正在输入..." every
:data:`_TYPING_INTERVAL_SECONDS` (8 by default — matches the legacy
30s keepalive but tightened for the §2.2 H-14 requirement that the
user see continuous activity even on slow tools).  The keepalive is
torn down in ``finally`` so a broken stream never leaks the task.

The service is **completely provider-neutral**: layer 1 / 2 are
delegated to caller-supplied callables (so adapters bind them to
their SDK without channels importing it).  Layer 3's pending queue
is backed by a :class:`PendingMessageStorePort` — production wiring
in :mod:`apps.api._channels_di` selects the SQLite adapter
(:class:`~qai.channels.adapters.pending_message_repository.SqlitePendingMessageRepository`);
tests / early bootstrap fall back to :class:`InMemoryPendingMessageStore`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from qai.platform.logging import get_logger

from qai.channels.application.ports import PendingMessageStorePort
from qai.channels.domain import ChannelInstanceId, ChannelUserId

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = get_logger(__name__)

__all__ = [
    "RealtimeDeliveryService",
    "RealtimeDeliveryConfig",
    "PendingMessageQueue",
    "InMemoryPendingMessageStore",
]

_TYPING_INTERVAL_SECONDS: float = 8.0

# Default Layer-3 expiry — matches the legacy ``_pending_cc_results``
# semantics where a queued message that goes unflushed for a day is
# considered stale and discarded on the next ``pop_all``.
_PENDING_DEFAULT_TTL_SECONDS: int = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
ReplyInContext = Callable[[str], Awaitable[bool]]
"""Layer 1 sender bound to a specific inbound message; returns ``True``
on success.  Adapter binds it to ``bot.reply(msg, text)``.  May be
``None`` for providers without an in-context reply concept (e.g.
Feishu HTTPS-only)."""

SendToUser = Callable[[str, str], Awaitable[bool]]
"""Layer 2 sender; ``(user_id, text) -> success``.  Adapter binds it
to ``bot.send_to_user(user_id, text)`` etc.  Required on every
provider — channels without this fail closed."""

SendTyping = Callable[[str], Awaitable[None]]
"""Periodic keepalive callable; ``(user_id) -> None``.  Best-effort —
exceptions are swallowed.  May be ``None`` for providers without a
typing-indicator API; the keepalive task is then skipped entirely."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RealtimeDeliveryConfig:
    """Tunables for the delivery service.

    ``context_token_age_guard_seconds`` is sourced from
    :attr:`qai.platform.config.settings.ChannelsSettings.context_token_age_guard_seconds`
    by the wiring layer (apps/api/_channels_di.py); the default
    here is the same 90s used by Settings so unit tests that
    instantiate the service directly get the right behaviour.
    """

    context_token_age_guard_seconds: float = 90.0
    typing_keepalive_seconds: float = _TYPING_INTERVAL_SECONDS
    typing_text: str = "正在输入..."


# ---------------------------------------------------------------------------
# In-memory pending message store (Layer 3 fallback)
# ---------------------------------------------------------------------------
class InMemoryPendingMessageStore(PendingMessageStorePort):
    """Process-local :class:`PendingMessageStorePort` impl.

    Used by tests and during early bootstrap before the SQLite adapter
    is wired.  Production deployments inject
    :class:`~qai.channels.adapters.pending_message_repository.SqlitePendingMessageRepository`
    so PR-097 / S9 §6 R-20 parity holds across server restarts.

    The store keeps one FIFO list per ``(instance_id, user_id)``; rows
    past their ``expires_at`` are dropped on every read.  Operations
    are guarded by an :class:`asyncio.Lock` so concurrent dispatch
    cycles for the same user serialise their queue updates.
    """

    __slots__ = ("_buckets", "_lock")

    def __init__(self) -> None:
        self._buckets: dict[
            tuple[str, str],
            list[tuple[str, datetime]],
        ] = {}
        self._lock = asyncio.Lock()

    async def push(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        message: str,
        expires_at: datetime,
    ) -> None:
        if not message:
            return
        key = (instance_id.value, user_id.value)
        async with self._lock:
            self._buckets.setdefault(key, []).append((message, expires_at))

    async def pop_all(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> list[str]:
        key = (instance_id.value, user_id.value)
        now = datetime.now(timezone.utc)
        async with self._lock:
            rows = self._buckets.pop(key, [])
        return [msg for msg, expires in rows if expires > now]

    async def has_pending(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> bool:
        key = (instance_id.value, user_id.value)
        now = datetime.now(timezone.utc)
        async with self._lock:
            rows = self._buckets.get(key, [])
            return any(expires > now for _, expires in rows)


# ---------------------------------------------------------------------------
# PendingMessageQueue façade (Layer 3)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PendingMessageQueue:
    """Per-(instance, user) pending message buffer (Layer 3).

    PR-097 (S9 §6 R-20): the queue now delegates to a
    :class:`PendingMessageStorePort` so Layer-3 messages survive a
    process restart.  When no store is provided at construction time,
    the queue creates an :class:`InMemoryPendingMessageStore` so
    existing tests / early bootstrap keep working unchanged.

    Behaviour mirrors the legacy ``_pending_cc_results[user_id]`` shape
    used by ``backend/channels/wechat/channel.py``: each new message
    is appended to the per-user FIFO, drained on the next inbound
    interaction, and tagged with a default 24-hour TTL so the queue
    cannot grow without bound.

    Methods are async because the SQLite-backed store performs I/O.
    The :class:`RealtimeDeliveryService` (and its
    :class:`_DeliveryContext`) await every call; callers outside this
    module follow the same shape.
    """

    _store: PendingMessageStorePort = field(
        default_factory=InMemoryPendingMessageStore
    )
    _ttl_seconds: int = _PENDING_DEFAULT_TTL_SECONDS

    async def put(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        message: str,
    ) -> None:
        if not message:
            return
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self._ttl_seconds
        )
        await self._store.push(
            instance_id=instance_id,
            user_id=user_id,
            message=message,
            expires_at=expires_at,
        )

    async def pop(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> str | None:
        """Return the oldest pending message, or ``None`` if empty.

        ``pop_all`` on the underlying store drains everything; we keep
        the legacy single-message shape (used by
        :meth:`RealtimeDeliveryService.drain_pending`) by returning
        the first item and re-pushing the rest with a fresh expiry so
        the FIFO order is preserved across process restarts.
        """
        messages = await self._store.pop_all(
            instance_id=instance_id, user_id=user_id
        )
        if not messages:
            return None
        head, tail = messages[0], messages[1:]
        if tail:
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=self._ttl_seconds
            )
            for msg in tail:
                await self._store.push(
                    instance_id=instance_id,
                    user_id=user_id,
                    message=msg,
                    expires_at=expires_at,
                )
        return head

    async def has_pending(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> bool:
        return await self._store.has_pending(
            instance_id=instance_id, user_id=user_id
        )


# ---------------------------------------------------------------------------
# RealtimeDeliveryService
# ---------------------------------------------------------------------------
class RealtimeDeliveryService:
    """Coordinates typing keepalive + 3-layer delivery fallback.

    Usage from the dispatch bridge::

        async with delivery.context(
            instance_id=msg.instance_id,
            user_id=msg.sender,
            reply_in_context=lambda text: bot.reply(msg, text),
            send_to_user=bot.send_to_user,
            send_typing=bot.send_typing,
        ) as session:
            # ... long-running CC stream emits text deltas ...
            await session.deliver(final_text)

    The context manager:

    * starts a background typing-keepalive task on entry,
    * captures the inbound time so the age guard knows when to
      switch to out-of-context delivery,
    * cancels the keepalive on exit.

    Each call to :meth:`DeliverySession.deliver` runs the 3-layer
    fallback once and updates the pending queue on terminal failure.
    """

    __slots__ = ("_config", "_pending")

    def __init__(
        self,
        *,
        config: RealtimeDeliveryConfig | None = None,
        pending_queue: PendingMessageQueue | None = None,
    ) -> None:
        self._config = config or RealtimeDeliveryConfig()
        self._pending = pending_queue or PendingMessageQueue()

    @property
    def pending(self) -> PendingMessageQueue:
        """Access the Layer-3 pending queue (drained by the bridge)."""
        return self._pending

    def context(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        reply_in_context: ReplyInContext | None,
        send_to_user: SendToUser,
        send_typing: SendTyping | None = None,
    ) -> "_DeliveryContext":
        """Open a delivery session covering one inbound→outbound cycle.

        ``reply_in_context`` and ``send_typing`` may be ``None`` for
        providers without those capabilities (e.g. Feishu): the
        service then skips Layer-1 / typing keepalive and runs the
        Layer-2 → Layer-3 chain only.
        """
        return _DeliveryContext(
            service=self,
            instance_id=instance_id,
            user_id=user_id,
            reply_in_context=reply_in_context,
            send_to_user=send_to_user,
            send_typing=send_typing,
        )

    async def drain_pending(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        reply_in_context: ReplyInContext | None,
        send_to_user: SendToUser,
    ) -> bool:
        """Flush any pending Layer-3 message for ``(instance, user)``.

        Returns ``True`` if a message was found *and* successfully
        delivered (Layers 1 → 2).  When delivery fails, the message
        is re-queued so the next interaction can try again.

        ``reply_in_context`` may be ``None`` for Feishu; in
        that case the drain skips Layer-1 and tries Layer-2 first.
        """
        message = await self._pending.pop(
            instance_id=instance_id, user_id=user_id
        )
        if not message:
            return False
        ok = False
        if reply_in_context is not None:
            try:
                ok = await reply_in_context(message)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.delivery.drain_layer1_failed",
                    instance_id=instance_id.value,
                    user_id=user_id.value,
                    error=str(exc),
                )
                ok = False
        if not ok:
            try:
                ok = await send_to_user(user_id.value, message)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.delivery.drain_layer2_failed",
                    instance_id=instance_id.value,
                    user_id=user_id.value,
                    error=str(exc),
                )
                ok = False
        if not ok:
            # Re-queue so we try again on the next inbound message.
            await self._pending.put(
                instance_id=instance_id,
                user_id=user_id,
                message=message,
            )
            return False
        return True


class _DeliveryContext:
    """Context manager + delivery handle returned by
    :meth:`RealtimeDeliveryService.context`.

    Public API exposed via ``async with``:

    * :meth:`deliver(text)` — run the 3-layer fallback for one
      message.  Updates the pending queue on terminal failure.
    """

    __slots__ = (
        "_service",
        "_instance_id",
        "_user_id",
        "_reply_in_context",
        "_send_to_user",
        "_send_typing",
        "_token_captured_at",
        "_typing_task",
    )

    def __init__(
        self,
        *,
        service: RealtimeDeliveryService,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        reply_in_context: ReplyInContext | None,
        send_to_user: SendToUser,
        send_typing: SendTyping | None,
    ) -> None:
        self._service = service
        self._instance_id = instance_id
        self._user_id = user_id
        self._reply_in_context = reply_in_context
        self._send_to_user = send_to_user
        self._send_typing = send_typing
        self._token_captured_at: float = 0.0
        self._typing_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "_DeliveryContext":
        self._token_captured_at = time.monotonic()
        if self._send_typing is not None:
            self._typing_task = asyncio.create_task(self._run_keepalive())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._typing_task is not None:
            self._typing_task.cancel()
            try:
                await self._typing_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
            self._typing_task = None

    @property
    def token_age_seconds(self) -> float:
        return time.monotonic() - self._token_captured_at

    async def deliver(self, text: str) -> bool:
        """Run the 3-layer fallback once for ``text``.

        Returns ``True`` on Layer-1 or Layer-2 success; ``False``
        when the message was queued to Layer 3 (call-site can
        treat both as "user will see this eventually").

        When ``reply_in_context`` was supplied as ``None`` (e.g.
        Feishu path) Layer-1 is skipped entirely and the chain runs
        Layer-2 → Layer-3.
        """
        if not text:
            return True
        # Layer-1 vs Layer-2 selection — token age guard
        guard = self._service._config.context_token_age_guard_seconds
        age = self.token_age_seconds
        prefer_layer2 = age >= guard or self._reply_in_context is None

        if not prefer_layer2 and self._reply_in_context is not None:
            try:
                ok = await self._reply_in_context(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.delivery.layer1_raised",
                    instance_id=self._instance_id.value,
                    user_id=self._user_id.value,
                    age_seconds=round(age, 1),
                    error=str(exc),
                )
                ok = False
            if ok:
                return True
            logger.info(
                "channels.delivery.layer1_failed_falling_to_layer2",
                instance_id=self._instance_id.value,
                user_id=self._user_id.value,
                age_seconds=round(age, 1),
            )
        elif self._reply_in_context is None:
            logger.debug(
                "channels.delivery.layer1_skipped_no_callable",
                instance_id=self._instance_id.value,
                user_id=self._user_id.value,
            )
        else:
            logger.info(
                "channels.delivery.token_aged_using_layer2",
                instance_id=self._instance_id.value,
                user_id=self._user_id.value,
                age_seconds=round(age, 1),
                guard_seconds=guard,
            )

        # Layer 2
        try:
            ok = await self._send_to_user(self._user_id.value, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.delivery.layer2_raised",
                instance_id=self._instance_id.value,
                user_id=self._user_id.value,
                error=str(exc),
            )
            ok = False
        if ok:
            return True

        # Layer 3 — queue for next interaction
        logger.warning(
            "channels.delivery.layer3_queued",
            instance_id=self._instance_id.value,
            user_id=self._user_id.value,
            text_length=len(text),
        )
        await self._service._pending.put(
            instance_id=self._instance_id,
            user_id=self._user_id,
            message=text,
        )
        return False

    # ------------------------------------------------------------------
    # Keepalive task
    # ------------------------------------------------------------------
    async def _run_keepalive(self) -> None:
        interval = self._service._config.typing_keepalive_seconds
        send = self._send_typing
        if send is None:
            return
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await send(self._user_id.value)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "channels.delivery.keepalive_failed",
                        user_id=self._user_id.value,
                        error=str(exc),
                    )
        except asyncio.CancelledError:
            pass
