# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Real adapters for the channels bounded context (PR-047).

aiosqlite-backed repositories + composition helpers (registries +
reply dispatcher + QR login state machine).  Provider-specific
transports / signature verifiers / payload parsers live under
``infrastructure/``.
"""

from __future__ import annotations

from .channel_instance_repository import SqliteChannelInstanceRepository
from .channel_message_repository import SqliteChannelMessageRepository
from .command_parser import RegexCommandParser
from .credentials_resolver import SecretStoreCredentialsResolver
from .feishu_tenant_token_cache import FeishuTenantTokenCache
from .pending_message_repository import SqlitePendingMessageRepository
from .qr_login import FeishuQrLogin, WechatQrLogin
from .qr_login_repository import SqliteQrLoginChallengeRepository
from .registry import (
    ChannelTransportRegistry,
    KindDispatchedPayloadParser,
    KindDispatchedSignatureVerifier,
)
from .reply_dispatcher import OutboundReplyDispatcher
from .session_index_repository import SqliteSessionIndexRepository

__all__ = [
    "SqliteChannelInstanceRepository",
    "SqliteChannelMessageRepository",
    "SqliteSessionIndexRepository",
    "SqliteQrLoginChallengeRepository",
    "SqlitePendingMessageRepository",
    "SecretStoreCredentialsResolver",
    "RegexCommandParser",
    "ChannelTransportRegistry",
    "KindDispatchedSignatureVerifier",
    "KindDispatchedPayloadParser",
    "OutboundReplyDispatcher",
    "WechatQrLogin",
    "FeishuQrLogin",
    "FeishuTenantTokenCache",
]
