# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure domain layer of the ai_coding bounded context.

Public exports are kept minimal: only the names the application layer
or other modules in this package actually reference.  Adapters and
infrastructure code MUST NOT add imports to this package; the
``.importlinter`` ``layered-ai_coding`` contract enforces it.
"""

from __future__ import annotations

from .entities import (
    CodingSession,
    PermissionRequest,
    Skill,
    ToolInvocation,
    make_skill_registered_event,
)
from .errors import (
    CodingSessionAlreadyTerminatedError,
    CodingSessionNotFoundError,
    InvalidSessionStateError,
    PermissionRequestAlreadyDecidedError,
    PermissionRequestNotFoundError,
    ProviderNotAvailableError,
    SkillNotRegisteredError,
    WorkspaceLockedError,
)
from .events import (
    CodingSessionInterruptedEvent,
    CodingSessionRenamedEvent,
    CodingSessionRestoredEvent,
    CodingSessionStartedEvent,
    CodingSessionStatusChangedEvent,
    CodingSessionStreamFrameEvent,
    CodingSessionTerminatedEvent,
    EffortChangedEvent,
    MessageHistoryTruncatedEvent,
    NotifyBindingChangedEvent,
    PermissionDecidedEvent,
    PermissionRequestedEvent,
    SkillRegisteredEvent,
    ToolCompletedEvent,
    ToolFailedEvent,
    ToolInvokedEvent,
    WorkspaceChangedEvent,
)
from .value_objects import (
    CodingSessionConfig,
    CodingSessionId,
    CodingStreamFrame,
    HookConfig,
    HookEvent,
    McpServerConfig,
    MessageContent,
    OutputFormat,
    PermissionDecision,
    PermissionRequestId,
    Provider,
    SessionStatus,
    StreamFrameKind,
    ToolInvocationId,
    ToolName,
    Workspace,
)

__all__ = [
    # Value objects
    "CodingSessionId",
    "PermissionRequestId",
    "ToolInvocationId",
    "Provider",
    "SessionStatus",
    "PermissionDecision",
    "StreamFrameKind",
    "Workspace",
    "ToolName",
    "MessageContent",
    "CodingStreamFrame",
    # PR-107 SDK 12 enhancements
    "CodingSessionConfig",
    "HookConfig",
    "HookEvent",
    "McpServerConfig",
    "OutputFormat",
    # Entities
    "CodingSession",
    "PermissionRequest",
    "ToolInvocation",
    "Skill",
    "make_skill_registered_event",
    # Errors
    "CodingSessionNotFoundError",
    "CodingSessionAlreadyTerminatedError",
    "ProviderNotAvailableError",
    "WorkspaceLockedError",
    "PermissionRequestNotFoundError",
    "PermissionRequestAlreadyDecidedError",
    "SkillNotRegisteredError",
    "InvalidSessionStateError",
    # Events
    "CodingSessionStartedEvent",
    "CodingSessionTerminatedEvent",
    "CodingSessionStatusChangedEvent",
    "CodingSessionStreamFrameEvent",
    "PermissionRequestedEvent",
    "PermissionDecidedEvent",
    "ToolInvokedEvent",
    "ToolCompletedEvent",
    "ToolFailedEvent",
    "SkillRegisteredEvent",
    "WorkspaceChangedEvent",
    # PR-104a session lifecycle extras
    "CodingSessionInterruptedEvent",
    "CodingSessionRenamedEvent",
    "CodingSessionRestoredEvent",
    "EffortChangedEvent",
    "MessageHistoryTruncatedEvent",
    "NotifyBindingChangedEvent",
]
