# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for the ai_coding bounded context.

Each use case lives in its own module to keep individual files small
and the import graph flat.  This package re-exports the command
dataclasses and the use case classes for convenience.
"""

from __future__ import annotations

from .change_workspace import (
    ChangeWorkspaceCommand,
    ChangeWorkspaceUseCase,
)
from .abort_revert import (
    AbortSessionCommand,
    AbortSessionResult,
    AbortSessionUseCase,
    RevertMessageCommand,
    RevertMessageResult,
    RevertMessageUseCase,
)
from .decide_permission import (
    DecidePermissionCommand,
    DecidePermissionUseCase,
)
from .get_coding_session import (
    GetCodingSessionQuery,
    GetCodingSessionUseCase,
)
from .get_session_history import (
    GetSessionHistoryQuery,
    GetSessionHistoryResult,
    GetSessionHistoryUseCase,
)
from .hard_delete_session import (
    HardDeleteSessionCommand,
    HardDeleteSessionUseCase,
)
from .health_status import (
    HealthStatusQuery,
    HealthStatusResult,
    HealthStatusUseCase,
    ModelInfo,
    ProviderInfo,
)
from .interrupt_session import (
    InterruptSessionCommand,
    InterruptSessionResult,
    InterruptSessionUseCase,
)
from .invoke_tool import InvokeToolCommand, InvokeToolUseCase
from .list_coding_sessions import (
    ListCodingSessionsQuery,
    ListCodingSessionsUseCase,
)
from .manage_checkpoints import (
    CheckpointInfo,
    CheckpointResult,
    CreateCheckpointCommand,
    CreateCheckpointUseCase,
    ListCheckpointsQuery,
    ListCheckpointsResult,
    ListCheckpointsUseCase,
    RewindCheckpointCommand,
    RewindCheckpointResult,
    RewindCheckpointUseCase,
)
from .manage_coding_config import (
    GetCodingConfigQuery,
    GetCodingConfigUseCase,
    SaveCodingConfigCommand,
    SaveCodingConfigUseCase,
)
from .manage_coding_credentials import (
    CC_CREDENTIAL_VARS,
    CC_SECRET_SERVICE,
    OC_CREDENTIAL_VARS,
    OC_SECRET_SERVICE,
    CredentialStatus,
    CredentialsStatusResult,
    DeleteCredentialCommand,
    DeleteCredentialUseCase,
    GetCodingCredentialsUseCase,
    SaveCodingCredentialsCommand,
    SaveCodingCredentialsResult,
    SaveCodingCredentialsUseCase,
)
from .manage_oc_service import (
    GetOcServiceLogsQuery,
    GetOcServiceLogsUseCase,
    GetOcServiceStatusUseCase,
    OcServiceLogsResult,
    StartOcServiceUseCase,
    StopOcServiceCommand,
    StopOcServiceUseCase,
)
from .manage_skills import (
    DiscoverSkillsUseCase,
    RegisterSkillCommand,
    RegisterSkillUseCase,
)
from .query_context_usage import (
    ContextSizeResult,
    ContextUsageQuery,
    ContextUsageResult,
    GetContextSizeUseCase,
    GetContextUsageUseCase,
)
from .rename_session import (
    RenameSessionCommand,
    RenameSessionUseCase,
)
from .request_permission import (
    RequestPermissionCommand,
    RequestPermissionUseCase,
)
from .restore_coding_session import (
    RestoreCodingSessionCommand,
    RestoreCodingSessionResult,
    RestoreCodingSessionUseCase,
)
from .send_user_message import (
    SendUserMessageCommand,
    SendUserMessageResult,
    SendUserMessageUseCase,
)
from .set_active_session import (
    SetActiveSessionCommand,
    SetActiveSessionUseCase,
)
from .set_session_effort import (
    SetSessionEffortCommand,
    SetSessionEffortUseCase,
)
from .set_session_notify import (
    NotifyChannel,
    SetSessionNotifyCommand,
    SetSessionNotifyUseCase,
)
from .spawn_coding_session import (
    SpawnCodingSessionCommand,
    SpawnCodingSessionUseCase,
)
from .stream_coding_session import (
    StreamCodingSessionCommand,
    StreamCodingSessionUseCase,
)
from .terminate_coding_session import (
    TerminateCodingSessionCommand,
    TerminateCodingSessionUseCase,
)
from .truncate_history import (
    TruncateHistoryCommand,
    TruncateHistoryResult,
    TruncateHistoryUseCase,
)

__all__ = [
    "AbortSessionCommand",
    "AbortSessionResult",
    "AbortSessionUseCase",
    "CC_CREDENTIAL_VARS",
    "CC_SECRET_SERVICE",
    "ChangeWorkspaceCommand",
    "ChangeWorkspaceUseCase",
    "CheckpointInfo",
    "CheckpointResult",
    "ContextSizeResult",
    "ContextUsageQuery",
    "ContextUsageResult",
    "CreateCheckpointCommand",
    "CreateCheckpointUseCase",
    "CredentialStatus",
    "CredentialsStatusResult",
    "DecidePermissionCommand",
    "DecidePermissionUseCase",
    "DeleteCredentialCommand",
    "DeleteCredentialUseCase",
    "DiscoverSkillsUseCase",
    "GetCodingConfigQuery",
    "GetCodingConfigUseCase",
    "GetCodingCredentialsUseCase",
    "GetCodingSessionQuery",
    "GetCodingSessionUseCase",
    "GetContextSizeUseCase",
    "GetContextUsageUseCase",
    "GetOcServiceLogsQuery",
    "GetOcServiceLogsUseCase",
    "GetOcServiceStatusUseCase",
    "GetSessionHistoryQuery",
    "GetSessionHistoryResult",
    "GetSessionHistoryUseCase",
    "HardDeleteSessionCommand",
    "HardDeleteSessionUseCase",
    "HealthStatusQuery",
    "HealthStatusResult",
    "HealthStatusUseCase",
    "InterruptSessionCommand",
    "InterruptSessionResult",
    "InterruptSessionUseCase",
    "InvokeToolCommand",
    "InvokeToolUseCase",
    "ListCheckpointsQuery",
    "ListCheckpointsResult",
    "ListCheckpointsUseCase",
    "ListCodingSessionsQuery",
    "ListCodingSessionsUseCase",
    "ModelInfo",
    "NotifyChannel",
    "OC_CREDENTIAL_VARS",
    "OC_SECRET_SERVICE",
    "OcServiceLogsResult",
    "ProviderInfo",
    "RegisterSkillCommand",
    "RegisterSkillUseCase",
    "RenameSessionCommand",
    "RenameSessionUseCase",
    "RequestPermissionCommand",
    "RequestPermissionUseCase",
    "RestoreCodingSessionCommand",
    "RestoreCodingSessionResult",
    "RestoreCodingSessionUseCase",
    "RevertMessageCommand",
    "RevertMessageResult",
    "RevertMessageUseCase",
    "RewindCheckpointCommand",
    "RewindCheckpointResult",
    "RewindCheckpointUseCase",
    "SaveCodingConfigCommand",
    "SaveCodingConfigUseCase",
    "SaveCodingCredentialsCommand",
    "SaveCodingCredentialsResult",
    "SaveCodingCredentialsUseCase",
    "SendUserMessageCommand",
    "SendUserMessageResult",
    "SendUserMessageUseCase",
    "SetActiveSessionCommand",
    "SetActiveSessionUseCase",
    "SetSessionEffortCommand",
    "SetSessionEffortUseCase",
    "SetSessionNotifyCommand",
    "SetSessionNotifyUseCase",
    "SpawnCodingSessionCommand",
    "SpawnCodingSessionUseCase",
    "StartOcServiceUseCase",
    "StopOcServiceCommand",
    "StopOcServiceUseCase",
    "StreamCodingSessionCommand",
    "StreamCodingSessionUseCase",
    "TerminateCodingSessionCommand",
    "TerminateCodingSessionUseCase",
    "TruncateHistoryCommand",
    "TruncateHistoryResult",
    "TruncateHistoryUseCase",
]
