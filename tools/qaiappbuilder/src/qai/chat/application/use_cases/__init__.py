# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public API for ``qai.chat.application.use_cases``.

Use-case classes are the application-layer entry points consumed by
the HTTP / WS interfaces and (in some cases) by inter-context handlers.
"""

from __future__ import annotations

from qai.chat.application.use_cases.compact import (
    ContextDecisionResult,
    GetContextDecisionInput,
    GetContextDecisionUseCase,
)
from qai.chat.application.use_cases.conversation_management import (
    CreateConversationInput,
    CreateConversationUseCase,
    DeleteConversationInput,
    DeleteConversationUseCase,
    GetConversationMessagesInput,
    GetConversationMessagesUseCase,
    ListConversationsInput,
    ListConversationsUseCase,
    RenameConversationInput,
    RenameConversationUseCase,
    SetConversationWorkspaceInput,
    SetConversationWorkspaceUseCase,
)
from qai.chat.application.use_cases.extras import (
    EnhancePromptInput,
    EnhancePromptResult,
    EnhancePromptUseCase,
    GetPromptSnapshotInput,
    GetPromptSnapshotUseCase,
    SavePromptSnapshotInput,
    SavePromptSnapshotUseCase,
    UploadImageInput,
    UploadImageUseCase,
)
from qai.chat.application.use_cases.streaming import (
    AsyncioStreamAbortHandle,
    StopChatInput,
    StopChatResult,
    StopChatUseCase,
    StreamChatInput,
    StreamChatUseCase,
)
from qai.chat.application.use_cases.tab_management import (
    CloseTabInput,
    CloseTabUseCase,
    ListActiveTabsUseCase,
    OpenTabInput,
    OpenTabUseCase,
)
from qai.chat.application.use_cases.title import (
    GenerateTitleInput,
    GenerateTitleResult,
    GenerateTitleUseCase,
    fallback_title,
)
from qai.chat.application.use_cases.fork_conversation import (
    ForkConversationInput,
    ForkConversationResult,
    ForkConversationUseCase,
)
from qai.chat.application.use_cases.migrate_conversation import (
    MigrateConversationInput,
    MigrateConversationResult,
    MigrateConversationUseCase,
)

__all__ = [
    # conversation management
    "CreateConversationUseCase",
    "CreateConversationInput",
    "ListConversationsUseCase",
    "ListConversationsInput",
    "GetConversationMessagesUseCase",
    "GetConversationMessagesInput",
    "RenameConversationUseCase",
    "RenameConversationInput",
    "SetConversationWorkspaceUseCase",
    "SetConversationWorkspaceInput",
    "DeleteConversationUseCase",
    "DeleteConversationInput",
    # streaming
    "StreamChatUseCase",
    "StreamChatInput",
    "StopChatUseCase",
    "StopChatInput",
    "StopChatResult",
    "AsyncioStreamAbortHandle",
    # context decision (Task T rename of legacy CompactChat*)
    "GetContextDecisionUseCase",
    "GetContextDecisionInput",
    "ContextDecisionResult",
    # tab management (PR-043 / issue d)
    "OpenTabUseCase",
    "OpenTabInput",
    "CloseTabUseCase",
    "CloseTabInput",
    "ListActiveTabsUseCase",
    # PR-402 / S7.5 lane L4
    "GenerateTitleUseCase",
    "GenerateTitleInput",
    "GenerateTitleResult",
    "fallback_title",
    # PR-403 / S7.5 lane L4
    "UploadImageUseCase",
    "UploadImageInput",
    "EnhancePromptUseCase",
    "EnhancePromptInput",
    "EnhancePromptResult",
    "GetPromptSnapshotUseCase",
    "GetPromptSnapshotInput",
    "SavePromptSnapshotUseCase",
    "SavePromptSnapshotInput",
    # fork (branch) conversation
    "ForkConversationUseCase",
    "ForkConversationInput",
    "ForkConversationResult",
    # migrate (P5 handoff) conversation
    "MigrateConversationUseCase",
    "MigrateConversationInput",
    "MigrateConversationResult",
]
