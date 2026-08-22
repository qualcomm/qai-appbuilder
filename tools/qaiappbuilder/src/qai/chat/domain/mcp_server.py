# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""MCP (Model Context Protocol) value objects for the chat bounded context.

An *MCP server* is an external process (``stdio`` subprocess) or network
endpoint (``sse`` / ``http``) that speaks the Model Context Protocol
(Anthropic open standard) and exposes a set of extra *tools*.  Once a server
is connected, its tools are advertised to the chat LLM alongside the built-in
tools and, when the model calls one, the invocation flows back through the
SAME :class:`qai.chat.application.ports.ToolInvocationPort` pipeline the
built-in tools use (so guardrails / truncation / advertise-filtering apply
uniformly).

Cross-context note
------------------
These value objects are declared **independently** and NOT imported from
``qai.ai_coding.domain`` — the ``context-isolation`` import-linter contract
forbids the chat context from importing another bounded context (AGENTS.md
§3.2).  The shapes intentionally mirror the Claude-Agent-SDK
``McpServerConfig`` family so a config authored for one is interchangeable
with the other at the JSON level.

These are pure value objects (no I/O, no ``httpx`` / ``apps`` / ``interfaces``
imports — domain-purity, AGENTS.md §3.5).  The actual connect / discover /
invoke work lives in
:class:`qai.chat.application.ports.McpServerRegistryPort` adapters under
``qai.chat.adapters`` / ``qai.chat.infrastructure``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from qai.platform.io_validator import (
    ValidationError as _IoValidationError,
    assert_matches,
    assert_max_length,
    assert_non_empty,
    assert_no_control_chars,
)

__all__ = [
    "McpTransport",
    "McpServerConfig",
    "McpTool",
    "McpResource",
    "McpPrompt",
    "McpPromptArgument",
]

# A server ``name`` is the unique key (AGENTS.md §3.9.1 — MCP is keyed by a
# unique server name, no per-user ``instance_id``).  Constrained to a
# filesystem/JSON-key-safe slug so it can be used verbatim as a SecretStore
# key namespace and a tool-name prefix.
_MAX_NAME_LENGTH: int = 128
_NAME_PATTERN: str = r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$"
_MAX_COMMAND_LENGTH: int = 4096
_MAX_URL_LENGTH: int = 4096
_MAX_ARGS: int = 128
_MAX_ARG_LENGTH: int = 4096
_MAX_ENV_ENTRIES: int = 256
_MAX_HEADER_ENTRIES: int = 128


class McpTransport(str, Enum):
    """How the chat context talks to an MCP server.

    * :attr:`STDIO` — spawn a local subprocess and exchange newline-delimited
      JSON-RPC 2.0 messages over its stdin/stdout (``command`` + ``args``).
    * :attr:`SSE` — connect to a remote Server-Sent-Events MCP endpoint
      (``url`` + optional ``headers``).
    * :attr:`HTTP` — connect to a remote streamable-HTTP MCP endpoint
      (``url`` + optional ``headers``).

    Values are the lowercase literals used by the Claude-Agent-SDK config so a
    server entry authored against that contract is byte-compatible.
    """

    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"


@dataclass(frozen=True, slots=True, kw_only=True)
class McpServerConfig:
    """One MCP server registration.

    ``name`` is the unique key.  The ``transport`` selects which set of the
    remaining fields is meaningful:

    * ``stdio`` — ``command`` (required, non-empty), ``args`` (list), ``env``
      (dict of extra environment overrides), ``cwd`` (optional working dir).
    * ``sse`` / ``http`` — ``url`` (required, non-empty), ``headers`` (dict;
      credential-bearing header VALUES are persisted via
      :class:`qai.platform.persistence.secrets.SecretStore`, never in
      plain-text config — AGENTS.md §3.3).

    ``timeout_s`` bounds a single connect / discover / invoke round-trip so a
    hung server never stalls the agent loop.

    Pure value object — validation happens in ``__post_init__`` and raises
    :class:`ValueError` (mapped to HTTP 400 by the route error middleware).
    """

    name: str
    transport: McpTransport = McpTransport.STDIO
    # stdio
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # sse / http
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # shared
    timeout_s: float = 30.0
    # Per-server master switch (tail-appended — MCP marketplace phase 1;
    # AGENTS.md §3.1 append-only). Independent of the GLOBAL ``chat_mcp_enabled``
    # gate and of the connection state — a server is only surfaced to the model
    # when GLOBAL-enabled AND this ``enabled`` AND currently connected (three-way
    # AND, enforced in the registry). Defaults to ``True`` so a freshly-installed
    # server is active immediately, and so a legacy persisted config that lacks
    # the key re-hydrates as enabled (backwards compatible).
    enabled: bool = True
    # Which ``env`` keys hold SECRET values (API keys / tokens) — tail-appended
    # (AGENTS.md §3.1 append-only). Declared by a keyed catalog entry's
    # ``secret_fields``. The registry externalises THESE env values to the
    # platform SecretStore on persist (only a ``__secret__`` sentinel is written
    # to the on-disk config, mirroring how remote ``headers`` secrets are
    # handled — AGENTS.md §3.3), then re-hydrates them from the SecretStore at
    # load / spawn time. A non-listed env key (e.g. a plain endpoint URL) is
    # persisted as-is. Empty for credential-free servers.
    secret_env_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            assert_non_empty(self.name, name="name")
            assert_max_length(
                self.name, max_length=_MAX_NAME_LENGTH, name="name"
            )
            assert_matches(self.name, pattern=_NAME_PATTERN, name="name")
            if self.transport is McpTransport.STDIO:
                assert_non_empty(self.command or "", name="command")
                assert_max_length(
                    self.command or "",
                    max_length=_MAX_COMMAND_LENGTH,
                    name="command",
                )
                assert_no_control_chars(self.command or "", name="command")
                if len(self.args) > _MAX_ARGS:
                    raise ValueError(f"too many args (max {_MAX_ARGS})")
                for a in self.args:
                    assert_max_length(
                        a, max_length=_MAX_ARG_LENGTH, name="arg"
                    )
                if len(self.env) > _MAX_ENV_ENTRIES:
                    raise ValueError(
                        f"too many env entries (max {_MAX_ENV_ENTRIES})"
                    )
            else:  # sse / http
                assert_non_empty(self.url or "", name="url")
                assert_max_length(
                    self.url or "", max_length=_MAX_URL_LENGTH, name="url"
                )
                assert_no_control_chars(self.url or "", name="url")
                if len(self.headers) > _MAX_HEADER_ENTRIES:
                    raise ValueError(
                        f"too many headers (max {_MAX_HEADER_ENTRIES})"
                    )
        except _IoValidationError as exc:
            raise ValueError(str(exc)) from exc
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")


@dataclass(frozen=True, slots=True, kw_only=True)
class McpTool:
    """A single tool discovered on a connected MCP server.

    ``server_name`` identifies the owning :class:`McpServerConfig`; ``name`` is
    the tool name as advertised by that server.  ``schema`` is the tool's
    JSON-Schema ``inputSchema`` (an object schema) as returned by the server's
    ``tools/list`` response — the registry wraps it into the OpenAI
    function-calling shape before advertising to the LLM.

    ``qualified_name`` is the wire tool name the LLM sees — the server name and
    the tool name joined with ``__`` so two servers exposing a like-named tool
    (e.g. two ``search`` tools) never collide, and so the invocation dispatcher
    can route a ``tool_call`` back to the right server by splitting on the first
    ``__``.
    """

    server_name: str
    name: str
    description: str = ""
    schema: dict[str, object] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        """Return ``"{server_name}__{name}"`` — the collision-safe wire name."""
        return f"{self.server_name}__{self.name}"


@dataclass(frozen=True, slots=True, kw_only=True)
class McpResource:
    """A single resource exposed by a connected MCP server.

    Resources are MCP's read-only content endpoints (files, DB rows, API
    payloads, ...) addressed by a ``uri``.  The registry surfaces a connected
    server's resources to the LLM as callable tools
    (``mcp__<server>__list_resources`` / ``mcp__<server>__read_resource``) so
    the model can enumerate + fetch them through the SAME tool pipeline as any
    other tool (advertise / guardrail / truncator / sub-agent inheritance).

    ``uri`` is the MCP resource URI; ``name`` / ``mime_type`` are the optional
    human-readable label + content type from the server's ``resources/list``.
    """

    server_name: str
    uri: str
    name: str = ""
    mime_type: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class McpPromptArgument:
    """One declared argument of an :class:`McpPrompt`.

    Mirrors the MCP ``prompts/list`` argument shape:
    ``{name, description?, required?}``.
    """

    name: str
    description: str = ""
    required: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class McpPrompt:
    """A single prompt template exposed by a connected MCP server.

    Prompts are MCP's reusable, parameterised message templates (returned by
    ``prompts/list``; rendered by ``prompts/get``).  The registry surfaces a
    connected server's prompts to the LLM as a callable tool
    (``mcp__<server>__get_prompt``) so the model can render a named prompt with
    arguments and fold the result into its reasoning — through the SAME tool
    pipeline as any other tool.

    ``arguments`` describes the declared parameters so the model knows what to
    pass when it invokes the ``get_prompt`` tool.
    """

    server_name: str
    name: str
    description: str = ""
    arguments: tuple[McpPromptArgument, ...] = ()
