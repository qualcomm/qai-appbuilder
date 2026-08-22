# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Provider-specific infrastructure adapters for the channels context.

Each module exposes one adapter class per :class:`ChannelKind`:

* ``transports`` — outbound HTTPS senders (Wechat / Feishu)
* ``signature_verifiers`` — inbound HMAC / SHA-1 sig verification
* ``payload_parsers`` — provider-shape JSON → ``WebhookPayload``
"""

from __future__ import annotations

from .payload_parsers import (
    FeishuPayloadParser,
    WechatPayloadParser,
)
from .signature_verifiers import (
    FeishuSigVerifier,
    WechatSigVerifier,
)
from .transports import FeishuTransport, WechatTransport

__all__ = [
    # transports
    "WechatTransport",
    "FeishuTransport",
    # signature verifiers
    "WechatSigVerifier",
    "FeishuSigVerifier",
    # payload parsers
    "WechatPayloadParser",
    "FeishuPayloadParser",
]
