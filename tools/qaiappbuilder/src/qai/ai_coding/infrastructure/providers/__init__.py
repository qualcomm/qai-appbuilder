# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP-backed coding provider adapters (PR-046, PR-102)."""

from __future__ import annotations

from .base import (
    DEFAULT_HTTP_CONFIG,
    HttpCodingProviderBase,
    HttpStreamError,
    ProviderHttpConfig,
)
from .claude_code import ClaudeCodeProvider
from .http_transport import (
    HttpTransportPort,
    HttpxTransport,
    InMemorySseTransport,
    SseEvent,
    parse_sse_bytes,
)
from .multi_provider import MultiProviderCodingAdapter
from .open_code import OpenCodeProvider

__all__ = [
    "DEFAULT_HTTP_CONFIG",
    "ClaudeCodeProvider",
    "HttpCodingProviderBase",
    "HttpStreamError",
    "HttpTransportPort",
    "HttpxTransport",
    "InMemorySseTransport",
    "MultiProviderCodingAdapter",
    "OpenCodeProvider",
    "ProviderHttpConfig",
    "SseEvent",
    "parse_sse_bytes",
]
