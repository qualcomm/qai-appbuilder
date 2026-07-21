# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public domain API for the channels bounded context.

Importing from this package is the supported way to use channels
domain types::

    from qai.channels.domain import (
        ChannelInstance,
        ChannelMessage,
        ChannelKind,
        ChannelStatus,
        SessionIndex,
        ...
    )
"""

from __future__ import annotations

from .errors import (
    ChannelInstanceAlreadyRunningError,
    ChannelInstanceNotFoundError,
    ChannelInstanceStateError,
    ChannelKindNotSupportedError,
    ChannelMessageNotFoundError,
    ChannelMessageStateError,
    CredentialsNotFoundError,
    InvalidCommandError,
    MessageBridgeUnavailableError,
    QrLoginChallengeNotFoundError,
    SessionIndexEntryNotFoundError,
    WebhookPayloadInvalidError,
    WebhookSignatureInvalidError,
)
from .events import (
    ChannelAcknowledgedEvent,
    ChannelErrorEvent,
    ChannelMessageDispatchedEvent,
    ChannelMessageReceivedEvent,
    ChannelMessageRepliedEvent,
    ChannelStartedEvent,
    ChannelStoppedEvent,
    QrLoginConfirmedEvent,
    QrLoginIssuedEvent,
    QrLoginScannedEvent,
    SessionIndexUpdatedEvent,
)
from .ids import ChannelInstanceId, ChannelMessageId, ChannelUserId
from .instance import ChannelInstance
from .kinds import ChannelKind
from .message import ChannelMessage
from .session_index import SessionIndex, SessionIndexEntry
from .value_objects import (
    ChannelBindings,
    ChannelContext,
    ChannelHealth,
    ChannelHealthReport,
    ChannelMessageStatus,
    ChannelModelConfig,
    ChannelProxyConfig,
    ChannelSettings,
    ChannelStatus,
    Command,
    CredentialsRef,
    ImageAttachment,
    MessageContent,
    MessageDirection,
    MessageReplyRef,
    QrLoginChallenge,
    QrLoginStatus,
    RichTextContent,
    RichTextSegment,
    WebhookPayload,
)

__all__ = [
    # ids
    "ChannelInstanceId",
    "ChannelMessageId",
    "ChannelUserId",
    # kinds
    "ChannelKind",
    # value objects / enums
    "ChannelStatus",
    "ChannelMessageStatus",
    "MessageDirection",
    "ChannelHealth",
    "QrLoginStatus",
    "MessageContent",
    "ImageAttachment",
    "RichTextSegment",
    "RichTextContent",
    "Command",
    "WebhookPayload",
    "ChannelHealthReport",
    "CredentialsRef",
    "QrLoginChallenge",
    "MessageReplyRef",
    # PR-202: settings + bindings VOs
    "ChannelProxyConfig",
    "ChannelModelConfig",
    "ChannelSettings",
    "ChannelBindings",
    # Channel invocation context
    "ChannelContext",
    # aggregates / entities
    "ChannelInstance",
    "ChannelMessage",
    "SessionIndex",
    "SessionIndexEntry",
    # events
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
    # errors
    "ChannelInstanceNotFoundError",
    "ChannelMessageNotFoundError",
    "SessionIndexEntryNotFoundError",
    "CredentialsNotFoundError",
    "QrLoginChallengeNotFoundError",
    "ChannelInstanceAlreadyRunningError",
    "ChannelInstanceStateError",
    "ChannelMessageStateError",
    "ChannelKindNotSupportedError",
    "WebhookSignatureInvalidError",
    "WebhookPayloadInvalidError",
    "InvalidCommandError",
    "MessageBridgeUnavailableError",
]
