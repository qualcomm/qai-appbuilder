# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public API of channel application use cases.

All use cases are constructible via keyword-only DI; none of them have
import-time side effects, none of them touch a real network / DB / OS
resource.  Wiring happens at the composition root (S4 PR-04x).
"""

from __future__ import annotations

from .ingest_webhook import (
    IngestWebhookCommand,
    IngestWebhookResult,
    IngestWebhookUseCase,
    SendChannelReplyUseCase,
    SendReplyCommand,
)
from .manage_lifecycle import (
    AcknowledgeChannelErrorUseCase,
    StartChannelInstanceUseCase,
    StopChannelInstanceUseCase,
    TransportFactory,
)
from .qr_login import ConfirmQrLoginUseCase, IssueQrLoginUseCase
from .register_channel_instance import (
    RegisterChannelInstanceCommand,
    RegisterChannelInstanceUseCase,
)
from .session_index import (
    BindSessionIndexCommand,
    BindSessionIndexUseCase,
    LookupSessionIndexUseCase,
)
from .conversation_commands import (
    CONVERSATION_COMMAND_VERBS,
    ConversationCommandPort,
    ConversationInfo,
    HandleConversationCommandUseCase,
)

__all__ = [
    "RegisterChannelInstanceCommand",
    "RegisterChannelInstanceUseCase",
    "TransportFactory",
    "StartChannelInstanceUseCase",
    "StopChannelInstanceUseCase",
    "AcknowledgeChannelErrorUseCase",
    "IngestWebhookCommand",
    "IngestWebhookResult",
    "IngestWebhookUseCase",
    "SendReplyCommand",
    "SendChannelReplyUseCase",
    "IssueQrLoginUseCase",
    "ConfirmQrLoginUseCase",
    "BindSessionIndexCommand",
    "BindSessionIndexUseCase",
    "LookupSessionIndexUseCase",
    "CONVERSATION_COMMAND_VERBS",
    "ConversationCommandPort",
    "ConversationInfo",
    "HandleConversationCommandUseCase",
]
