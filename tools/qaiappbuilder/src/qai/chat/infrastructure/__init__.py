# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public API for ``qai.chat.infrastructure`` (PR-042).

Houses non-SQLite adapters (network / process / filesystem). Currently
exports :class:`HttpOpenAICompatibleLLMStream` (the OpenAI-compatible
HTTP transport for the LLM streaming port) and
:class:`MultiProviderLLMStream` (a routing wrapper that dispatches by
provider id). Additional provider transports, when needed, are added
beside these two — this module is the single seam between the chat
context and the network.
"""

from __future__ import annotations

from qai.chat.infrastructure.hook_engine import (
    LazyReloadHookEngine,
    NullHookEngine,
    SubprocessHookEngine,
    build_hook_engine,
)
from qai.chat.infrastructure.mcp_client import (
    McpConnectionError,
    McpTransportClient,
    call_tool as mcp_call_tool,
    discover_prompts as mcp_discover_prompts,
    discover_resources as mcp_discover_resources,
    discover_tools as mcp_discover_tools,
    get_prompt as mcp_get_prompt,
    read_resource as mcp_read_resource,
)
from qai.chat.infrastructure.llm_stream import (
    HttpOpenAICompatibleLLMStream,
    MultiProviderLLMStream,
)
from qai.chat.infrastructure.provider_routing_stream import (
    ProviderRoutingLLMStream,
)


__all__ = [
    "HttpOpenAICompatibleLLMStream",
    "MultiProviderLLMStream",
    "ProviderRoutingLLMStream",
    "LazyReloadHookEngine",
    "NullHookEngine",
    "SubprocessHookEngine",
    "build_hook_engine",
    "McpConnectionError",
    "McpTransportClient",
    "mcp_call_tool",
    "mcp_discover_tools",
    "mcp_discover_resources",
    "mcp_read_resource",
    "mcp_discover_prompts",
    "mcp_get_prompt",
]
