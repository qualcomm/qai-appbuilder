# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain events for the chat bounded context.

All events here inherit from
:class:`qai.platform.events.types.DomainEvent` and follow the
``<context>.<verb>_<subject>`` naming convention from the S2 spec.

Events are pure data: they carry **ids and value snapshots only**, never
references to live aggregates, so subscribers can be scheduled on any
loop without race conditions.

Naming pattern: ``chat.<verb>_<subject>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from qai.chat.domain.ids import ConversationId, MessageId, TabId
from qai.chat.domain.stream_frame import StreamFrame
from qai.platform.events import DomainEvent


# ---------------------------------------------------------------------------
# Conversation lifecycle
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationCreatedEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.created_conversation"
    conversation_id: ConversationId
    title: str
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationRenamedEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.renamed_conversation"
    conversation_id: ConversationId
    old_title: str
    new_title: str
    renamed_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationDeletedEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.deleted_conversation"
    conversation_id: ConversationId
    deleted_at: datetime


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class MessageAppendedEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.appended_message"
    conversation_id: ConversationId
    message_id: MessageId
    role: str
    appended_at: datetime


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class TabOpenedEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.opened_tab"
    tab_id: TabId
    conversation_id: ConversationId
    opened_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class TabAbortedEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.aborted_tab"
    tab_id: TabId
    conversation_id: ConversationId
    aborted_at: datetime
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TabClosedEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.closed_tab"
    tab_id: TabId
    conversation_id: ConversationId
    closed_at: datetime


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ChatStreamStartedEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.started_stream"
    tab_id: TabId
    conversation_id: ConversationId
    started_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatStreamFrameEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.streamed_frame"
    tab_id: TabId
    conversation_id: ConversationId
    frame: StreamFrame


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatStreamCompletedEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.completed_stream"
    tab_id: TabId
    conversation_id: ConversationId
    completed_at: datetime
    frame_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatStreamAbortedEvent(DomainEvent):
    event_type: ClassVar[str] = "chat.aborted_stream"
    tab_id: TabId
    conversation_id: ConversationId
    aborted_at: datetime
    reason: str


__all__ = [
    "ConversationCreatedEvent",
    "ConversationRenamedEvent",
    "ConversationDeletedEvent",
    "MessageAppendedEvent",
    "TabOpenedEvent",
    "TabAbortedEvent",
    "TabClosedEvent",
    "ChatStreamStartedEvent",
    "ChatStreamFrameEvent",
    "ChatStreamCompletedEvent",
    "ChatStreamAbortedEvent",
]
