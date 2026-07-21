# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public API for ``qai.chat.domain``.

Aggregate roots, entities, value objects, events and errors for the
chat bounded context.

Importing from this package is the supported way to use chat domain
types.  See refactor-plan v2.5 §6 / §7 / §10 for design context.
"""

from __future__ import annotations

from qai.chat.domain.content import (
    ContextSize,
    MessageContent,
    MessageRole,
    TokenCount,
)
from qai.chat.domain.conversation import Conversation, ConversationStatus
from qai.chat.domain.errors import (
    ChatStreamAbortedError,
    ConversationLockedError,
    ConversationNotFoundError,
    ExperienceNotFoundError,
    InvalidContextSizeError,
    InvalidConversationTitleError,
    InvalidMessageContentError,
    ParticipantNotFoundError,
    SubAgentSessionNotFoundError,
    TabNotFoundError,
    TabStateError,
)
from qai.chat.domain.events import (
    ChatStreamAbortedEvent,
    ChatStreamCompletedEvent,
    ChatStreamFrameEvent,
    ChatStreamStartedEvent,
    ConversationCreatedEvent,
    ConversationDeletedEvent,
    ConversationRenamedEvent,
    MessageAppendedEvent,
    TabAbortedEvent,
    TabClosedEvent,
    TabOpenedEvent,
)
from qai.chat.domain.experience import CategoryStat, Experience
from qai.chat.domain.hook import HookConfig, HookDecision, HookEvent
from qai.chat.domain.ids import (
    ConversationId,
    ExperienceId,
    MessageId,
    ParticipantId,
    SubAgentSessionId,
    TabId,
)
from qai.chat.domain.mcp_catalog import (
    CURATED_CATALOG,
    CuratedCatalogEntry,
    get_catalog_entry,
)
from qai.chat.domain.mcp_server import (
    McpPrompt,
    McpPromptArgument,
    McpResource,
    McpServerConfig,
    McpTool,
    McpTransport,
)
from qai.chat.domain.message import Message
from qai.chat.domain.participant import Participant, ParticipantKind
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.chat.domain.sub_agent_session import (
    SubAgentOwner,
    SubAgentSession,
    SubAgentSessionStatus,
)
from qai.chat.domain.tab import ConversationTab, TabStatus

__all__ = [
    # ids
    "ConversationId",
    "MessageId",
    "TabId",
    "ExperienceId",
    "SubAgentSessionId",
    "ParticipantId",
    # value objects
    "MessageContent",
    "MessageRole",
    "TokenCount",
    "ContextSize",
    "StreamFrame",
    "StreamFrameType",
    # entities / aggregates
    "Conversation",
    "ConversationStatus",
    "ConversationTab",
    "TabStatus",
    "Message",
    "Experience",
    "CategoryStat",
    "SubAgentSession",
    "SubAgentSessionStatus",
    "SubAgentOwner",
    "Participant",
    "ParticipantKind",
    # hooks
    "HookEvent",
    "HookConfig",
    "HookDecision",
    # mcp servers
    "McpTransport",
    "McpServerConfig",
    "McpTool",
    "McpResource",
    "McpPrompt",
    "McpPromptArgument",
    # mcp marketplace catalog
    "CuratedCatalogEntry",
    "CURATED_CATALOG",
    "get_catalog_entry",
    # events
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
    # errors
    "ConversationNotFoundError",
    "TabNotFoundError",
    "ExperienceNotFoundError",
    "SubAgentSessionNotFoundError",
    "ParticipantNotFoundError",
    "ConversationLockedError",
    "TabStateError",
    "ChatStreamAbortedError",
    "InvalidContextSizeError",
    "InvalidMessageContentError",
    "InvalidConversationTitleError",
]
