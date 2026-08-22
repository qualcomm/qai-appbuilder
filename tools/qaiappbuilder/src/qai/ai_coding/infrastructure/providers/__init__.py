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
from .claude_cli_locator import locate_claude_cli, normalise_cli_path
from .claude_code import ClaudeCodeProvider
from .claude_code_sdk import ClaudeCodeSdkProvider, claude_sdk_available
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
    "ClaudeCodeSdkProvider",
    "HttpCodingProviderBase",
    "HttpStreamError",
    "HttpTransportPort",
    "HttpxTransport",
    "InMemorySseTransport",
    "MultiProviderCodingAdapter",
    "OpenCodeProvider",
    "ProviderHttpConfig",
    "SseEvent",
    "claude_sdk_available",
    "locate_claude_cli",
    "normalise_cli_path",
    "parse_sse_bytes",
]
