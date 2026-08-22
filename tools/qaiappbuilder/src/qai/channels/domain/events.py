# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain events emitted by the channels bounded context.

All events inherit from :class:`qai.platform.events.DomainEvent` and use
the ``channels.<verb>_<subject>`` topic naming convention from
``docs/90-refactor/S2-sub-agent-spec.md`` §9.

Events carry **value snapshots only** (id strings, enum values,
timestamps); they never reference live mutable aggregates so subscribers
in other contexts (chat / ai_coding via ``MessageBridgePort``) can be
scheduled later without race conditions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from qai.platform.events import DomainEvent

from .kinds import ChannelKind
from .value_objects import ChannelMessageStatus, ChannelStatus, QrLoginStatus


# ---------------------------------------------------------------------------
# Channel lifecycle events
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelStartedEvent(DomainEvent):
    """Emitted when an instance transitions into ``running``."""

    event_type: ClassVar[str] = "channels.channel_started"

    instance_id: str
    kind: ChannelKind
    started_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelStoppedEvent(DomainEvent):
    """Emitted when an instance transitions into ``stopped``."""

    event_type: ClassVar[str] = "channels.channel_stopped"

    instance_id: str
    kind: ChannelKind
    stopped_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelErrorEvent(DomainEvent):
    """Emitted when an instance transitions into ``error``.

    ``previous_status`` carries the status the aggregate was in before
    the failure so subscribers can distinguish "failed to start" from
    "crashed while running".
    """

    event_type: ClassVar[str] = "channels.channel_error"

    instance_id: str
    kind: ChannelKind
    previous_status: ChannelStatus
    reason: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelAcknowledgedEvent(DomainEvent):
    """Emitted when an operator acknowledges the ``error`` state.

    The instance transitions ``error`` → ``stopped``; this event lets
    the WebUI broadcaster clear the error badge in real time.
    """

    event_type: ClassVar[str] = "channels.channel_acknowledged"

    instance_id: str
    kind: ChannelKind
    acknowledged_at: datetime


# ---------------------------------------------------------------------------
# Message events
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelMessageReceivedEvent(DomainEvent):
    """Emitted by :class:`IngestWebhookUseCase` after signature
    verification + payload parse + :class:`ChannelMessage` creation."""

    event_type: ClassVar[str] = "channels.message_received"

    message_id: str
    instance_id: str
    kind: ChannelKind
    sender_id: str
    arrived_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelMessageDispatchedEvent(DomainEvent):
    """Emitted when a parsed message is handed to
    :class:`MessageBridgePort` for downstream handling
    (chat / ai_coding)."""

    event_type: ClassVar[str] = "channels.message_dispatched"

    message_id: str
    instance_id: str
    kind: ChannelKind
    target: str
    dispatched_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelMessageRepliedEvent(DomainEvent):
    """Emitted when a reply has been sent back to the channel."""

    event_type: ClassVar[str] = "channels.message_replied"

    message_id: str
    instance_id: str
    kind: ChannelKind
    replied_at: datetime
    final_status: ChannelMessageStatus


# ---------------------------------------------------------------------------
# QR login events
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class QrLoginIssuedEvent(DomainEvent):
    event_type: ClassVar[str] = "channels.qr_login_issued"

    challenge_id: str
    instance_id: str
    kind: ChannelKind
    issued_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class QrLoginScannedEvent(DomainEvent):
    event_type: ClassVar[str] = "channels.qr_login_scanned"

    challenge_id: str
    instance_id: str
    scanned_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class QrLoginConfirmedEvent(DomainEvent):
    event_type: ClassVar[str] = "channels.qr_login_confirmed"

    challenge_id: str
    instance_id: str
    confirmed_at: datetime
    final_status: QrLoginStatus


# ---------------------------------------------------------------------------
# Session-index events
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class SessionIndexUpdatedEvent(DomainEvent):
    """Emitted when the channel-user → internal-session mapping changes.

    Replaces the legacy module-level ``_user_cc_sessions`` dict in
    ``backend/channels/wechat`` (see inventory §5.2).
    """

    event_type: ClassVar[str] = "channels.session_index_updated"

    instance_id: str
    channel_user_id: str
    internal_user_id: str | None
    coding_session_id: str | None
    updated_at: datetime


__all__ = [
    "ChannelStartedEvent",
    "ChannelStoppedEvent",
    "ChannelErrorEvent",
    "ChannelAcknowledgedEvent",
    "ChannelMessageReceivedEvent",
    "ChannelMessageDispatchedEvent",
    "ChannelMessageRepliedEvent",
    "QrLoginIssuedEvent",
    "QrLoginScannedEvent",
    "QrLoginConfirmedEvent",
    "SessionIndexUpdatedEvent",
]
