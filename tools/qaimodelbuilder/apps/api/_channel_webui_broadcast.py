# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer WebUI broadcaster for channel inbound / outbound traffic.

PR-097 (S9 §6 R-19): restores the WebUI live-update side-channel that
the legacy broadcast helpers (``backend/channels/feishu/channel.py``
``_broadcast_feishu_message`` at line 1495 and the parallel
``backend/channels/wechat/channel.py`` block at line 1395 emitting
``wechat_update_conv``) provided.  Without this, the WebUI's Channels
panel does not reflect inbound chats arriving through Feishu / WeChat
transports — operators see a stale view until they reload.

Design
------

The broadcaster is a thin shim over :class:`qai.platform.events.EventBus`.
It publishes two structured :class:`DomainEvent` payloads on the bus:

* ``channels.webui.inbound`` — emitted right after the dispatch bridge
  has parsed and persisted an inbound channel message.
* ``channels.webui.outbound`` — emitted after each outbound delivery
  through :class:`RealtimeDeliveryService.deliver`.

The WebUI's chat-events SSE stream subscribes to the ``channels.webui.*``
topic and forwards each envelope to connected browsers as a JSON frame
with ``type`` set to ``channel_inbound`` / ``channel_outbound``, mirroring
the legacy ``feishu_update_conv`` / ``wechat_update_conv`` shape so the
front-end channels panel logic is unchanged.

The broadcaster sits at the apps composition root because it crosses
the channels↔chat boundary in an observability-only direction (channels
publishes, the chat-events route subscribes); domain code never imports
it and channels' application layer never touches the chat SSE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

from qai.platform.events import DomainEvent, EventBus
from qai.platform.logging import get_logger

from qai.channels.domain import (
    ChannelInstance,
    ChannelKind,
    ChannelUserId,
)
from qai.channels.domain.value_objects import ChannelStatus

logger = get_logger(__name__)

__all__ = [
    "ChannelWebUIBroadcaster",
    "ChannelWebUIConnectionEvent",
    "ChannelWebUIErrorEvent",
    "ChannelWebUIInboundEvent",
    "ChannelWebUIOutboundEvent",
    "ChannelWebUIQrEvent",
]


#: Per-kind SSE frame ``type`` the WebUI conversation-list handler keys on.
#: Mirrors the legacy frame names V1 broadcast (``wechat_update_conv`` /
#: ``feishu_update_conv``) so the front-end sidebar logic is unchanged.
_KIND_UPDATE_CONV_TYPE: dict[ChannelKind, str] = {
    ChannelKind.WECHAT: "wechat_update_conv",
    ChannelKind.FEISHU: "feishu_update_conv",
}


def _channel_update_conv_frame(
    *,
    kind: ChannelKind,
    conversation_id: str,
    sender_or_target_id: str,
    text: str,
    role: str,
    occurred_at: datetime,
) -> dict[str, Any]:
    """Build the V1-shaped ``{type, conv_id, title, messages}`` SSE frame.

    V1 (``backend/channels/wechat/channel.py:1418`` /
    ``feishu/channel.py:1508``) broadcast this exact shape; the WebUI's
    conversation-list handler inserts / moves the conversation in the sidebar
    keyed by ``conv_id`` and appends ``messages``.  ``conv_id`` MUST be the
    real conversation id (empty → the front-end cannot address a row, which
    was the "history not refreshing" bug).
    """
    ts_ms = int(occurred_at.timestamp() * 1000)
    frame_type = _KIND_UPDATE_CONV_TYPE.get(kind, "wechat_update_conv")
    return {
        "type": frame_type,
        "conv_id": conversation_id,
        # Title is left empty: the backend already persisted the channel
        # conversation's title (``[飞书]`` / ``[微信]`` …); the WebUI handler
        # falls back to its own source-tag label when title is empty, and a
        # subsequent ``GET /api/chat/conversations`` reconciles the real one.
        "title": "",
        "messages": [
            {
                "role": role,
                "content": text,
                "timestamp": ts_ms,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Domain events
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelWebUIInboundEvent(DomainEvent):
    """Inbound channel message snapshot — published to the chat-events SSE.

    Carries value snapshots only (ids + plain strings); no live aggregate
    references, matching the :class:`DomainEvent` contract so subscribers
    can be scheduled later without race conditions.
    """

    event_type: ClassVar[str] = "channels.webui.inbound"

    instance_id: str
    kind: ChannelKind
    sender_id: str
    text: str
    conversation_id: str
    occurred_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Project onto the V1 ``wechat_update_conv`` / ``feishu_update_conv``
        SSE frame the front-end already consumes.

        V1 parity (``backend/channels/wechat/channel.py:1418`` /
        ``feishu/channel.py:1508``) broadcast a frame shaped
        ``{type, conv_id, title, messages:[...]}`` that the WebUI's
        conversation list handler (V1 ``useChat.js:2935-2994``) used to
        insert / move the conversation to the top of the sidebar instantly.
        The ``/api/events`` route prefers ``to_dict()`` when present, so we
        emit that exact shape (instead of the raw dataclass) — keyed by the
        REAL ``conversation_id`` resolved by the dispatch bridge so the
        sidebar can address the row (an empty conv_id can't be inserted).
        """
        return _channel_update_conv_frame(
            kind=self.kind,
            conversation_id=self.conversation_id,
            sender_or_target_id=self.sender_id,
            text=self.text,
            role="user",
            occurred_at=self.occurred_at,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelWebUIOutboundEvent(DomainEvent):
    """Outbound reply snapshot — published to the chat-events SSE."""

    event_type: ClassVar[str] = "channels.webui.outbound"

    instance_id: str
    kind: ChannelKind
    target_id: str
    text: str
    conversation_id: str
    occurred_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Project the assistant reply onto the V1 update-conv SSE frame.

        Mirror of :meth:`ChannelWebUIInboundEvent.to_dict` for the reply
        direction (``role="assistant"``) so the WebUI appends the bot's turn
        to the same conversation row and keeps the sidebar fresh.
        """
        return _channel_update_conv_frame(
            kind=self.kind,
            conversation_id=self.conversation_id,
            sender_or_target_id=self.target_id,
            text=self.text,
            role="assistant",
            occurred_at=self.occurred_at,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelWebUIConnectionEvent(DomainEvent):
    """Connection status change — mirrors v1 ``_status`` transitions.

    Emitted when a channel instance transitions between states
    (idle → logging_in → connected → error → stopped).  The WebUI
    Channels panel uses this to show real-time connection indicators
    per channel instance.
    """

    event_type: ClassVar[str] = "channels.webui.connection_status"

    instance_id: str
    kind: ChannelKind
    status: ChannelStatus
    previous_status: ChannelStatus | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelWebUIQrEvent(DomainEvent):
    """QR code lifecycle event — mirrors v1 ``on_qr_url`` / ``on_scanned``.

    Emitted when a QR login challenge is issued, scanned, confirmed, or
    expires.  The WebUI renders the QR image or status badge accordingly.
    """

    event_type: ClassVar[str] = "channels.webui.qr_update"

    instance_id: str
    kind: ChannelKind
    challenge_id: str
    qr_status: str  # "issued" | "scanned" | "confirmed" | "expired"
    qr_url: str  # URL for the QR image (empty after scanned/confirmed)
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelWebUIErrorEvent(DomainEvent):
    """Error/exception event — mirrors v1 ``on_error`` callback.

    Emitted when a channel instance encounters a transport-level or
    runtime error.  The WebUI shows a toast / inline warning so operators
    are aware without reloading.
    """

    event_type: ClassVar[str] = "channels.webui.error"

    instance_id: str
    kind: ChannelKind
    reason: str
    recoverable: bool
    occurred_at: datetime


# ---------------------------------------------------------------------------
# Broadcaster
# ---------------------------------------------------------------------------
class ChannelWebUIBroadcaster:
    """Pushes inbound / outbound channel messages to WebUI clients.

    Mirrors legacy ``backend/channels/feishu/channel.py``
    ``_broadcast_feishu_message`` (line 1495-1531) and the parallel
    WeChat block emitting ``wechat_update_conv`` so the WebUI's
    Channels panel reflects live channel traffic.  Restored by
    PR-097 / S9 §6 R-19.

    The broadcaster does not write to a transport directly — it
    publishes :class:`ChannelWebUIInboundEvent` /
    :class:`ChannelWebUIOutboundEvent` on the platform
    :class:`EventBus`; the WebUI's chat-events SSE route subscribes
    to ``channels.webui.*`` and serialises each envelope to the
    front-end.

    Failures are *non-fatal*: if the bus rejects a publish (e.g.
    backpressure), we log a WARNING and return; the user-facing
    channel reply path is unaffected.
    """

    __slots__ = ("_event_bus",)

    def __init__(self, *, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    async def broadcast_inbound(
        self,
        *,
        instance: ChannelInstance,
        sender: ChannelUserId,
        text: str,
        conversation_id: str,
    ) -> None:
        """Emit a ``channels.webui.inbound`` event onto the chat-events bus.

        Called by the dispatch bridge after persisting the inbound
        :class:`ChannelMessage` and resolving the WebUI conversation
        binding.  Empty ``text`` is allowed (image-only messages decode
        to an empty placeholder before LLM ingestion); the WebUI still
        wants to surface an entry in its panel.
        """
        event = ChannelWebUIInboundEvent(
            instance_id=instance.instance_id.value,
            kind=instance.kind,
            sender_id=sender.value,
            text=text,
            conversation_id=conversation_id,
            occurred_at=datetime.now(timezone.utc),
        )
        await self._publish(event)

    async def broadcast_outbound(
        self,
        *,
        instance: ChannelInstance,
        target: ChannelUserId,
        text: str,
        conversation_id: str,
    ) -> None:
        """Emit a ``channels.webui.outbound`` event after a successful send.

        Mirrors :meth:`broadcast_inbound` for the reply direction; the
        :class:`RealtimeDeliveryService` calls this after each Layer-1
        / Layer-2 success so the WebUI shows assistant turns in real
        time.
        """
        event = ChannelWebUIOutboundEvent(
            instance_id=instance.instance_id.value,
            kind=instance.kind,
            target_id=target.value,
            text=text,
            conversation_id=conversation_id,
            occurred_at=datetime.now(timezone.utc),
        )
        await self._publish(event)

    async def broadcast_connection_status(
        self,
        *,
        instance: ChannelInstance,
        status: ChannelStatus,
        previous_status: ChannelStatus | None = None,
    ) -> None:
        """Emit a ``channels.webui.connection_status`` event.

        Called when a channel instance transitions between lifecycle
        states.  Mirrors v1's module-level ``_status`` variable updates
        that were consumed by the WebUI status poller — now pushed in
        real time via the chat-events SSE.
        """
        event = ChannelWebUIConnectionEvent(
            instance_id=instance.instance_id.value,
            kind=instance.kind,
            status=status,
            previous_status=previous_status,
            occurred_at=datetime.now(timezone.utc),
        )
        await self._publish(event)

    async def broadcast_qr_update(
        self,
        *,
        instance: ChannelInstance,
        challenge_id: str,
        qr_status: str,
        qr_url: str = "",
    ) -> None:
        """Emit a ``channels.webui.qr_update`` event.

        Mirrors v1's ``on_qr_url`` / ``on_scanned`` / ``on_expired``
        callbacks so the WebUI renders the QR code or status in real
        time without polling the ``/api/wechat/status`` endpoint.
        """
        event = ChannelWebUIQrEvent(
            instance_id=instance.instance_id.value,
            kind=instance.kind,
            challenge_id=challenge_id,
            qr_status=qr_status,
            qr_url=qr_url,
            occurred_at=datetime.now(timezone.utc),
        )
        await self._publish(event)

    async def broadcast_error(
        self,
        *,
        instance: ChannelInstance,
        reason: str,
        recoverable: bool = True,
    ) -> None:
        """Emit a ``channels.webui.error`` event.

        Mirrors v1's ``on_error`` callback.  Broadcast is best-effort;
        the actual error handling is in the transport layer — this only
        pushes an informational notification to connected WebUI clients.
        """
        event = ChannelWebUIErrorEvent(
            instance_id=instance.instance_id.value,
            kind=instance.kind,
            reason=reason,
            recoverable=recoverable,
            occurred_at=datetime.now(timezone.utc),
        )
        await self._publish(event)

    async def _publish(
        self,
        event: (
            ChannelWebUIInboundEvent
            | ChannelWebUIOutboundEvent
            | ChannelWebUIConnectionEvent
            | ChannelWebUIQrEvent
            | ChannelWebUIErrorEvent
        ),
    ) -> None:
        try:
            await self._event_bus.publish(event)
        except Exception as exc:  # noqa: BLE001 — broadcast is best-effort
            logger.warning(
                "channels.webui.broadcast_failed",
                event_type=event.event_type,
                instance_id=event.instance_id,
                error=str(exc),
            )
