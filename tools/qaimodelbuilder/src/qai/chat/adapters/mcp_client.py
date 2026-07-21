# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""MCP server registry adapter for the chat bounded context.

Implements :class:`qai.chat.application.ports.McpServerRegistryPort`.  Owns:

* **Config persistence** — the set of :class:`McpServerConfig` is stored as a
  JSON file at ``<data>/config/mcp_servers.json`` (the daemon runtime config
  dir, AGENTS.md §2.4 ``data/config/`` = runtime user config).  This is the
  GLOBAL persistence choice (task spec): MCP servers are a workspace-wide
  capability, not per-conversation.  The choice is documented here so a future
  reader knows where the truth lives.  Credential-bearing header VALUES are
  NOT written to this file — they go to
  :class:`qai.platform.persistence.secrets.SecretStore` (AGENTS.md §3.3) and
  are re-hydrated at connect time.

* **Tool registration** — the KEY architectural win: instead of a bespoke
  advertise/invoke path, a connected server's tools are registered as
  ``handler + schema`` entries on the SHARED
  :class:`qai.chat.adapters.tool_invocation.RegistryBackedToolInvocation`.
  Because ``StreamChatUseCase._collect_tool_schemas`` advertises whatever that
  registry's ``schemas()`` returns and dispatches ``tool_call`` frames through
  its ``invoke()``, MCP tools automatically flow through the same
  advertise-filter → guardrail → truncator pipeline as the built-in tools with
  ZERO streaming-use-case change.  Each MCP tool is registered under its
  collision-safe ``{server}__{tool}`` qualified name (:attr:`McpTool.
  qualified_name`) and the handler routes back to the owning server.

* **Resources / prompts surface (Plan A — expose as tools)** — a connected
  server's *resources* and *prompts* are ALSO surfaced to the LLM as callable
  tools on the SAME shared registry, rather than injected into the streaming
  prompt assembly (Plan B).  Plan A is chosen because it reuses the entire
  tools pipeline already built above (advertise-filter / guardrail / truncator
  / sub-agent inheritance) with ZERO change to ``streaming.py`` — identical to
  the tool-bridge architecture — whereas Plan B would have to modify the
  streaming prompt-assembly main path (higher risk, forbidden file domain).
  For each connected server that advertises them, the registry auto-registers:

  * ``mcp__<server>__list_resources`` (no args) — enumerate the server's
    resources (uri / name / mime);
  * ``mcp__<server>__read_resource`` (``{"uri": str}``) — read one resource;
  * ``mcp__<server>__list_prompts`` (no args) — enumerate the server's prompts;
  * ``mcp__<server>__get_prompt`` (``{"name": str, "arguments"?: object}``) —
    render one prompt.

  These capability tools are registered ONLY for a server that is enabled +
  connected AND actually advertises the corresponding capability (a server with
  no resources gets no resource tools).  They are dropped the moment the server
  disconnects / is removed (same lifecycle as the direct tools), so the model
  can never reach a resource / prompt of an un-enabled or unreachable server.

* **Secure-by-default gate** — when ``enabled`` is ``False`` (the
  ``chat.chat_mcp_enabled`` Settings gate, default off) the registry never
  spawns a subprocess / opens a session; :meth:`list_servers` still returns the
  persisted configs (so the UI can show + edit them) but connections are not
  made and no tools are advertised.  Mirrors the ``SubprocessHookEngine``
  ``enabled=False`` gate.

Cross-context isolation
-----------------------
Imports only ``qai.chat.{domain,application,infrastructure}`` + stdlib +
``qai.platform.persistence.secrets`` (the platform SecretStore port, allowed
from any context).  No imports of other bounded contexts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qai.chat.application.ports import (
    McpServerRegistryPort,
    McpServerStatus,
    ToolInvocationRequest,
)
from qai.chat.domain.mcp_catalog import (
    CURATED_CATALOG,
    CuratedCatalogEntry,
    get_catalog_entry,
)
from qai.chat.domain.mcp_server import (
    McpPrompt,
    McpResource,
    McpServerConfig,
    McpTool,
    McpTransport,
)
from qai.chat.infrastructure.mcp_client import (
    McpConnectionError,
    McpTransportClient,
    call_tool as _mcp_call_tool,
    discover_prompts as _mcp_discover_prompts,
    discover_resources as _mcp_discover_resources,
    get_prompt as _mcp_get_prompt,
    read_resource as _mcp_read_resource,
)
from qai.chat.infrastructure.mcp_registry_source import (
    McpRegistrySourceError,
    fetch_registry_entries as _fetch_registry_entries,
    fetch_registry_page as _fetch_registry_page,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from qai.chat.adapters.tool_invocation import RegistryBackedToolInvocation
    from qai.platform.persistence.secrets import SecretStore

logger = logging.getLogger("qai.chat.mcp_registry")

__all__ = [
    "McpServerRegistry",
    "McpToolInvocationAdapter",
]

#: SecretStore namespace under which per-server header credentials live.
#: Keyed ``<server_name>.<header_name>``.
_SECRET_SERVICE: str = "chat_mcp"

#: Soft upper bound on how many browsed registry entries the in-memory install
#: cache retains. The cache exists only so an install can resolve a browsed
#: entry by id; it must not grow unbounded as the user searches/pages many
#: times. On overflow the OLDEST entries are dropped (most-recently-browsed
#: kept) — see :meth:`McpServerRegistry._merge_registry_cache`.
_MAX_REGISTRY_CACHE: int = 500


class McpToolInvocationAdapter:
    """Bridge one MCP tool into a :class:`ToolInvocationPort`-style handler.

    The registry constructs one adapter per discovered tool and registers its
    :meth:`invoke` as the handler on the shared tool registry (paired with the
    OpenAI function-calling schema built from the tool's JSON-Schema).  When the
    LLM calls the tool, :meth:`invoke` opens a fresh connection to the owning
    server, calls the tool, and returns the textual result — flowing back
    through the same guardrail / truncator pipeline as any built-in tool.

    The call is dispatched through the registry's PERSISTENT session pool
    (``invoker``): the server's subprocess is spawned once and reused across
    many calls (the MCP session model), so there is no per-call ~6-13s cold
    start and stateful servers (memory graph, playwright session) keep their
    state between calls. A dead pooled session is transparently re-spawned by
    the pool. When no pool invoker is wired (e.g. a unit test constructing the
    adapter directly), it falls back to a one-shot connection.
    """

    __slots__ = ("_config_provider", "_tool_name", "_invoker")

    def __init__(
        self,
        *,
        tool_name: str,
        config_provider: "Any",
        invoker: "Any" = None,
    ) -> None:
        # ``config_provider`` is a zero-arg callable returning the CURRENT
        # McpServerConfig for the owning server (so an edited config / removed
        # server is reflected without re-registering the handler). Returns
        # ``None`` when the server was removed.
        # ``invoker`` is an async callable ``(config, tool_name, arguments) ->
        # str`` that routes through the registry's persistent session pool; when
        # ``None`` a one-shot connection is used (test / fallback).
        self._tool_name = tool_name
        self._config_provider = config_provider
        self._invoker = invoker

    async def invoke(self, request: ToolInvocationRequest) -> str:
        config = self._config_provider()
        if config is None:
            raise RuntimeError(
                f"mcp server for tool {self._tool_name!r} is no longer registered"
            )
        if self._invoker is not None:
            return await self._invoker(config, self._tool_name, request.arguments)
        return await _mcp_call_tool(config, self._tool_name, request.arguments)


def _to_openai_schema(tool: McpTool) -> dict[str, Any]:
    """Wrap an :class:`McpTool` into the OpenAI function-calling schema shape.

    The advertised NAME is the collision-safe qualified name so two servers'
    like-named tools never clash.  ``parameters`` is the tool's JSON-Schema
    ``inputSchema`` (an object schema); a missing / non-object schema falls back
    to the permissive empty-object schema so the model can still call it.
    """
    params = tool.schema if isinstance(tool.schema, dict) and tool.schema else {
        "type": "object",
        "properties": {},
    }
    desc = tool.description or f"MCP tool {tool.name} on server {tool.server_name}"
    # Prefix the description so the model knows the tool's provenance.
    desc = f"[MCP:{tool.server_name}] {desc}"
    return {
        "type": "function",
        "function": {
            "name": tool.qualified_name,
            "description": desc,
            "parameters": params,
        },
    }


# ---------------------------------------------------------------------------
# Resources / prompts capability tools (Plan A — expose as callable tools)
# ---------------------------------------------------------------------------
#: Prefix for the synthetic capability-tool names so they are visually grouped
#: and clearly attributable to the MCP resources/prompts surface.
_MCP_TOOL_PREFIX = "mcp__"


def _resource_tool_names(server_name: str) -> tuple[str, str]:
    """Return the (list_resources, read_resource) tool names for a server."""
    return (
        f"{_MCP_TOOL_PREFIX}{server_name}__list_resources",
        f"{_MCP_TOOL_PREFIX}{server_name}__read_resource",
    )


def _prompt_tool_names(server_name: str) -> tuple[str, str]:
    """Return the (list_prompts, get_prompt) tool names for a server."""
    return (
        f"{_MCP_TOOL_PREFIX}{server_name}__list_prompts",
        f"{_MCP_TOOL_PREFIX}{server_name}__get_prompt",
    )


def _list_resources_schema(server_name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _resource_tool_names(server_name)[0],
            "description": (
                f"[MCP:{server_name}] List the resources exposed by the "
                f"'{server_name}' MCP server (returns each resource's uri, "
                "name and mime type). Call this first to discover what you can "
                "read, then use the read_resource tool with a uri."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _read_resource_schema(server_name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _resource_tool_names(server_name)[1],
            "description": (
                f"[MCP:{server_name}] Read one resource from the '{server_name}' "
                "MCP server by its uri (obtain the uri from the list_resources "
                "tool). Returns the resource's text content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "description": "The MCP resource uri to read.",
                    }
                },
                "required": ["uri"],
            },
        },
    }


def _list_prompts_schema(server_name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _prompt_tool_names(server_name)[0],
            "description": (
                f"[MCP:{server_name}] List the prompt templates exposed by the "
                f"'{server_name}' MCP server (returns each prompt's name, "
                "description and declared arguments). Call this to discover "
                "prompts, then use get_prompt to render one."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _get_prompt_schema(server_name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _prompt_tool_names(server_name)[1],
            "description": (
                f"[MCP:{server_name}] Render a named prompt template from the "
                f"'{server_name}' MCP server with the given arguments. Returns "
                "the assembled prompt text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The prompt name (from list_prompts).",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Argument name→value map for the prompt.",
                    },
                },
                "required": ["name"],
            },
        },
    }


def _render_resource_list(resources: tuple[McpResource, ...]) -> str:
    """Render a resource list into a compact text block for the model."""
    if not resources:
        return "(no resources)"
    lines = [
        f"- {r.uri}"
        + (f" ({r.name})" if r.name else "")
        + (f" [{r.mime_type}]" if r.mime_type else "")
        for r in resources
    ]
    return "\n".join(lines)


def _render_prompt_list(prompts: tuple[McpPrompt, ...]) -> str:
    """Render a prompt list into a compact text block for the model."""
    if not prompts:
        return "(no prompts)"
    lines: list[str] = []
    for p in prompts:
        args = ", ".join(
            a.name + ("*" if a.required else "") for a in p.arguments
        )
        line = f"- {p.name}"
        if p.description:
            line += f": {p.description}"
        if args:
            line += f"  (args: {args})"
        lines.append(line)
    return "\n".join(lines)


class _McpResourceToolHandler:
    """Handlers for a server's resources capability tools (Plan A).

    ``config_provider`` returns the CURRENT :class:`McpServerConfig` for the
    owning server (or ``None`` if it was removed). Each handler opens a fresh
    connection to the server, so a removed / disconnected server fails just
    that one call rather than corrupting shared state.
    """

    __slots__ = ("_config_provider", "_client_provider")

    def __init__(self, *, config_provider: "Any", client_provider: "Any" = None) -> None:
        # ``client_provider`` is an async callable ``(config) -> live
        # McpTransportClient`` from the registry pool (reuse the persistent
        # session). When ``None``, falls back to a one-shot connection.
        self._config_provider = config_provider
        self._client_provider = client_provider

    async def list_resources(self, request: ToolInvocationRequest) -> str:
        config = self._config_provider()
        if config is None:
            return "[mcp error] server no longer registered"
        if self._client_provider is not None:
            client = await self._client_provider(config)
            resources = await client.list_resources()
        else:
            resources = await _mcp_discover_resources(config)
        return _render_resource_list(resources)

    async def read_resource(self, request: ToolInvocationRequest) -> str:
        config = self._config_provider()
        if config is None:
            return "[mcp error] server no longer registered"
        uri = str((request.arguments or {}).get("uri") or "").strip()
        if not uri:
            return "[mcp error] missing required argument 'uri'"
        if self._client_provider is not None:
            client = await self._client_provider(config)
            return await client.read_resource(uri)
        return await _mcp_read_resource(config, uri)


class _McpPromptToolHandler:
    """Handlers for a server's prompts capability tools (Plan A)."""

    __slots__ = ("_config_provider", "_client_provider")

    def __init__(self, *, config_provider: "Any", client_provider: "Any" = None) -> None:
        self._config_provider = config_provider
        self._client_provider = client_provider

    async def list_prompts(self, request: ToolInvocationRequest) -> str:
        config = self._config_provider()
        if config is None:
            return "[mcp error] server no longer registered"
        if self._client_provider is not None:
            client = await self._client_provider(config)
            prompts = await client.list_prompts()
        else:
            prompts = await _mcp_discover_prompts(config)
        return _render_prompt_list(prompts)

    async def get_prompt(self, request: ToolInvocationRequest) -> str:
        config = self._config_provider()
        if config is None:
            return "[mcp error] server no longer registered"
        args = request.arguments or {}
        name = str(args.get("name") or "").strip()
        if not name:
            return "[mcp error] missing required argument 'name'"
        prompt_args = args.get("arguments")
        if not isinstance(prompt_args, dict):
            prompt_args = {}
        if self._client_provider is not None:
            client = await self._client_provider(config)
            return await client.get_prompt(name, prompt_args)
        return await _mcp_get_prompt(config, name, prompt_args)


def _materialise_stdio_entry(
    entry: CuratedCatalogEntry,
    *,
    server_name: str,
    arg_values: dict[str, str],
    env_values: dict[str, str],
    error_cls: type[Exception],
) -> McpServerConfig:
    """Build a stdio config: substitute args + collect declared env vars."""
    for placeholder in entry.requires_args:
        if not str(arg_values.get(placeholder, "")).strip():
            raise error_cls(
                f"catalog entry {entry.id!r} requires argument {placeholder!r}"
            )
    substituted: list[str] = []
    for token in entry.args_template:
        if token in arg_values and str(arg_values[token]).strip():
            substituted.append(str(arg_values[token]).strip())
        elif token.startswith("<") and token.endswith(">"):
            continue
        else:
            substituted.append(token)
    env: dict[str, str] = {}
    for var in getattr(entry, "env_schema", ()):
        val = str(env_values.get(var, "")).strip()
        if val:
            env[var] = val
    for var in getattr(entry, "env_required", ()):
        if not str(env_values.get(var, "")).strip():
            raise error_cls(f"catalog entry {entry.id!r} requires env var {var!r}")
    # Which of the collected env keys are secrets (API keys / tokens): the
    # entry's declared secret_fields intersected with its env schema. These are
    # externalised to the SecretStore on persist (not written plain-text to the
    # on-disk config) — mirroring how remote header secrets are handled.
    env_schema = set(getattr(entry, "env_schema", ()))
    secret_env_keys = tuple(
        f for f in getattr(entry, "secret_fields", ()) if f in env_schema and f in env
    )
    return McpServerConfig(
        name=server_name,
        transport=McpTransport.STDIO,
        command=entry.command,
        args=tuple(substituted),
        env=env,
        # 90s (was 30s) to accommodate first-time cold starts of uvx/npx
        # (measured cold start ~13s); stdio servers are re-spawned on every
        # discover/call, so multiple startups stack up — avoid handshake timeout.
        timeout_s=90.0,
        enabled=True,
        secret_env_keys=secret_env_keys,
    )


def _materialise_remote_entry(
    entry: CuratedCatalogEntry,
    *,
    server_name: str,
    transport: str,
    header_values: dict[str, str],
    error_cls: type[Exception],
) -> McpServerConfig:
    """Build an sse/http config: use url + collect declared headers."""
    if not entry.url:
        raise error_cls(
            f"catalog entry {entry.id!r} has no url for remote transport"
        )
    headers: dict[str, str] = {}
    for hname in getattr(entry, "headers_schema", ()):
        val = str(header_values.get(hname, "")).strip()
        if val:
            headers[hname] = val
    for hname in getattr(entry, "headers_required", ()):
        if not str(header_values.get(hname, "")).strip():
            raise error_cls(f"catalog entry {entry.id!r} requires header {hname!r}")
    return McpServerConfig(
        name=server_name,
        transport=McpTransport(transport),
        url=entry.url,
        headers=headers,
        # 90s (was 30s) to accommodate first-time cold starts of uvx/npx
        # (measured cold start ~13s); stdio servers are re-spawned on every
        # discover/call, so multiple startups stack up — avoid handshake timeout.
        timeout_s=90.0,
        enabled=True,
    )


def _materialise_entry(
    entry: CuratedCatalogEntry,
    *,
    name: str | None,
    arg_values: dict[str, str],
    env_values: dict[str, str],
    header_values: dict[str, str],
    error_cls: type[Exception],
) -> McpServerConfig:
    """Build an :class:`McpServerConfig` from a catalog entry + user inputs.

    Dispatches on the entry ``transport``:

    * **stdio** (curated + registry packages) — substitute ``<PLACEHOLDER>`` args
      and collect declared env vars (``env_required`` enforced);
    * **sse / http** (registry remotes) — use ``url`` + collect declared headers
      (``headers_required`` enforced); ``add_server`` externalises each header
      VALUE to the SecretStore (``__secret__`` sentinel on disk).

    Raises ``error_cls`` (``McpCatalogInstallError``) on a missing required
    placeholder / env / header.
    """
    server_name = name or entry.id
    transport = str(getattr(entry, "transport", "stdio") or "stdio")
    if transport == "stdio":
        return _materialise_stdio_entry(
            entry,
            server_name=server_name,
            arg_values=arg_values,
            env_values=env_values,
            error_cls=error_cls,
        )
    return _materialise_remote_entry(
        entry,
        server_name=server_name,
        transport=transport,
        header_values=header_values,
        error_cls=error_cls,
    )


class McpServerRegistry(McpServerRegistryPort):
    """Concrete :class:`McpServerRegistryPort` backed by a JSON config file.

    See the module docstring for the persistence + tool-registration + gate
    design.  All public methods are ``async`` and safe under concurrent access
    (guarded by an ``asyncio.Lock``).
    """

    def __init__(
        self,
        *,
        tools: "RegistryBackedToolInvocation",
        config_path: Path,
        enabled: bool = False,
        secret_store: "SecretStore | None" = None,
        registry_source_base_url: str | None = None,
        registry_source_ttl_s: float = 900.0,
        ssl_verify: bool = True,
        ssl_verify_provider: "Callable[[], bool] | None" = None,
    ) -> None:
        self._tools = tools
        self._config_path = config_path
        self._enabled = enabled
        self._secret_store = secret_store
        # Unified outbound-TLS switch (top-level ``Settings.ssl_verify``,
        # edition-derived default) threaded into every spawned / opened
        # ``McpTransportClient`` so MCP servers follow the same TLS-verification
        # policy as every other outbound client.
        self._ssl_verify = ssl_verify
        # Live Settings.ssl_verify provider forwarded into every spawned /
        # opened ``McpTransportClient`` so NEW MCP remote connections read the
        # global SSL toggle at connect time (already-open pooled clients keep
        # their old verify until reconnect — acceptable). Frozen bool fallback.
        self._ssl_verify_provider = ssl_verify_provider
        # ── Phase-2 dynamic official-registry source (marketplace) ──
        # USER-DRIVEN, no backend gate: browsing the catalog NEVER auto-fetches
        # the third-party registry. ``list_catalog`` returns only the curated
        # source + whatever registry entries are already cached; the network
        # fetch happens ONLY on ``refresh_catalog`` — i.e. when the user
        # explicitly picks the "registry" source and clicks "load / refresh" in
        # the panel. So the user's click IS the consent to reach out; there is
        # no hidden operator flag they cannot control. Offline-safe: a failed
        # refresh degrades to curated + any prior cache (never raises).
        self._registry_source_base_url = registry_source_base_url
        self._registry_source_ttl_s = max(0.0, registry_source_ttl_s)
        # In-memory cache of the last successful dynamic-source fetch (+ the
        # monotonic timestamp it was fetched at) so re-opening the panel within
        # the TTL does not re-hit the network. ``None`` = never fetched.
        self._registry_cache: tuple[CuratedCatalogEntry, ...] | None = None
        self._registry_cache_at: float = 0.0
        # Last dynamic-source degradation reason (empty when the last fetch — or
        # the cache — is healthy), surfaced to the UI as a soft banner.
        self._registry_source_error: str = ""
        self._lock = asyncio.Lock()
        # name -> config
        self._servers: dict[str, McpServerConfig] = {}
        # name -> last live status fields
        self._connected: dict[str, bool] = {}
        self._errors: dict[str, str] = {}
        self._tool_names: dict[str, tuple[str, ...]] = {}
        # name -> discovered resource/prompt counts (for status projection);
        # only populated for a currently-connected server.
        self._resource_count: dict[str, int] = {}
        self._prompt_count: dict[str, int] = {}
        # qualified tool names we registered on the shared port, per server, so
        # remove/replace can drop exactly those.
        self._registered_qtools: dict[str, list[str]] = {}
        self._loaded = False
        # ── Persistent MCP session pool (name -> live McpTransportClient) ──
        # The MCP standard's session model: spawn/open a server's transport ONCE
        # and reuse it for many tools/call + *_list round-trips, instead of the
        # old "spawn per operation" (which paid a ~6-13s npx cold start on EVERY
        # call and lost stateful servers' state — memory graph / playwright
        # session — between calls). A pooled client is health-probed
        # (``is_alive`` — a real returncode/closed check, State-Truth-First) and
        # transparently re-spawned if the child died. Closed on
        # remove / disable / disconnect / shutdown.
        self._pool: dict[str, McpTransportClient] = {}
        # Guards pool mutation (acquire/close). Distinct from ``self._lock``
        # (which guards config/state) to avoid a slow spawn holding the config
        # lock and blocking read-only API like ``list_servers``.
        self._pool_lock = asyncio.Lock()

    # ---- config file I/O -------------------------------------------------

    def _load_from_disk(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if not self._config_path.is_file():
                return
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — config must never break startup
            logger.warning("chat.mcp.config_load_failed path=%s", self._config_path)
            return
        entries = (raw or {}).get("servers") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                cfg = self._config_from_dict(entry)
            except (ValueError, TypeError):
                continue
            self._servers[cfg.name] = cfg
        # Global master switch: the persisted file is the single truth source
        # once written. A file that carries ``global_enabled`` OVERRIDES the
        # constructor's default (the Settings seed); an older file WITHOUT the
        # key leaves ``self._enabled`` at the constructor default (backwards
        # compatible — an existing config keeps whatever the seed said).
        if isinstance(raw, dict) and "global_enabled" in raw:
            self._enabled = bool(raw.get("global_enabled"))

    def _persist_to_disk(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "global_enabled": self._enabled,
                "servers": [self._config_to_dict(c) for c in self._servers.values()]
            }
            # UTF-8, no BOM, LF (AGENTS.md §3.10).
            self._config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
        except Exception:  # noqa: BLE001 — persistence best-effort; never crash
            logger.warning("chat.mcp.config_persist_failed path=%s", self._config_path)

    def _config_to_dict(self, cfg: McpServerConfig) -> dict[str, Any]:
        """Serialise a config, stashing credential header + env values in SecretStore."""
        headers = self._externalise_headers(cfg)
        env = self._externalise_env(cfg)
        return {
            "name": cfg.name,
            "transport": cfg.transport.value,
            "command": cfg.command,
            "args": list(cfg.args),
            "env": env,
            "secret_env_keys": list(cfg.secret_env_keys),
            "cwd": cfg.cwd,
            "url": cfg.url,
            "headers": headers,
            "timeout_s": cfg.timeout_s,
            "enabled": cfg.enabled,
        }

    def _externalise_env(self, cfg: McpServerConfig) -> dict[str, str]:
        """Move each SECRET env VALUE into SecretStore; keep a sentinel on disk.

        Only env keys listed in ``cfg.secret_env_keys`` (API keys / tokens) are
        externalised — a non-secret env value (e.g. a plain endpoint URL) is
        persisted as-is. Mirrors :meth:`_externalise_headers` for remote headers
        (AGENTS.md §3.3: no plain-text credential on disk). When no SecretStore
        is wired, a secret value is DROPPED (never written plain-text) — the
        operator must re-enter it.
        """
        secret_keys = set(cfg.secret_env_keys)
        out: dict[str, str] = {}
        for key, val in cfg.env.items():
            if key not in secret_keys:
                out[key] = val  # plain, non-secret env — persist as-is
                continue
            if self._secret_store is not None:
                try:
                    self._secret_store.set(
                        _SECRET_SERVICE, f"{cfg.name}.env.{key}", val
                    )
                    out[key] = "__secret__"
                    continue
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "chat.mcp.secret_env_set_failed server=%s", cfg.name
                    )
            # No secret store / failure: do NOT persist the plain value.
            out[key] = "__secret__"
        return out

    def _externalise_headers(self, cfg: McpServerConfig) -> dict[str, str]:
        """Move each header VALUE into SecretStore; store a placeholder on disk.

        Returns a dict mapping header name → the sentinel ``"__secret__"`` so
        the on-disk config never carries a plain-text credential (AGENTS.md
        §3.3).  When no SecretStore is wired, the header values are dropped
        entirely (never written plain-text) — the operator must re-enter them.
        """
        out: dict[str, str] = {}
        for hname, hval in cfg.headers.items():
            if self._secret_store is not None:
                try:
                    self._secret_store.set(
                        _SECRET_SERVICE, f"{cfg.name}.{hname}", hval
                    )
                    out[hname] = "__secret__"
                    continue
                except Exception:  # noqa: BLE001
                    logger.warning("chat.mcp.secret_set_failed server=%s", cfg.name)
            # No secret store / failure: do NOT persist the plain value.
            out[hname] = "__secret__"
        return out

    def _config_from_dict(self, entry: dict[str, Any]) -> McpServerConfig:
        """Build a config from a persisted dict, re-hydrating header secrets."""
        name = str(entry.get("name") or "")
        transport = McpTransport(str(entry.get("transport") or "stdio"))
        headers_raw = entry.get("headers") or {}
        headers: dict[str, str] = {}
        if isinstance(headers_raw, dict):
            for hname, hval in headers_raw.items():
                if hval == "__secret__" and self._secret_store is not None:
                    try:
                        headers[hname] = self._secret_store.get(
                            _SECRET_SERVICE, f"{name}.{hname}"
                        )
                        continue
                    except Exception:  # noqa: BLE001 — missing secret → skip header
                        continue
                if isinstance(hval, str) and hval != "__secret__":
                    headers[hname] = hval
        args_raw = entry.get("args") or []
        env_raw = entry.get("env") or {}
        secret_env_raw = entry.get("secret_env_keys") or []
        secret_env_keys = (
            tuple(str(k) for k in secret_env_raw)
            if isinstance(secret_env_raw, list)
            else ()
        )
        # Re-hydrate env: a ``__secret__`` sentinel for a declared secret env key
        # is fetched back from the SecretStore; plain values pass through.
        env: dict[str, str] = {}
        if isinstance(env_raw, dict):
            for k, v in env_raw.items():
                ks, vs = str(k), str(v)
                if vs == "__secret__" and self._secret_store is not None:
                    try:
                        env[ks] = self._secret_store.get(
                            _SECRET_SERVICE, f"{name}.env.{ks}"
                        )
                        continue
                    except Exception:  # noqa: BLE001 — missing secret → skip env
                        continue
                if vs != "__secret__":
                    env[ks] = vs
        return McpServerConfig(
            name=name,
            transport=transport,
            command=entry.get("command"),
            args=tuple(str(a) for a in args_raw) if isinstance(args_raw, list) else (),
            env=env,
            cwd=entry.get("cwd"),
            url=entry.get("url"),
            headers=headers,
            timeout_s=float(entry.get("timeout_s") or 30.0),
            secret_env_keys=secret_env_keys,
            # Legacy configs (pre-marketplace) lack the key → default True
            # (backwards compatible: an existing server stays enabled).
            enabled=bool(entry.get("enabled", True)),
        )

    # ---- tool registration ----------------------------------------------

    def _register_tools(self, cfg: McpServerConfig, tools: tuple[McpTool, ...]) -> None:
        self._unregister_tools(cfg.name)
        qnames: list[str] = []
        for tool in tools:
            adapter = McpToolInvocationAdapter(
                tool_name=tool.name,
                config_provider=lambda n=cfg.name: self._servers.get(n),
                invoker=self._pool_call_tool,
            )
            self._tools.register(
                tool.qualified_name,
                adapter.invoke,
                schema=_to_openai_schema(tool),
            )
            qnames.append(tool.qualified_name)
        self._registered_qtools[cfg.name] = qnames

    def _register_capability_tools(
        self,
        cfg: McpServerConfig,
        *,
        has_resources: bool,
        has_prompts: bool,
    ) -> None:
        """Register the resources/prompts capability tools (Plan A).

        Only registers a capability's tools when the CONNECTED server actually
        advertises that capability (``has_resources`` / ``has_prompts``), so a
        server with no prompts never adds prompt tools. Appends to the server's
        existing registered-tool list so :meth:`_unregister_tools` drops them
        together with the direct tools.
        """
        qnames = self._registered_qtools.setdefault(cfg.name, [])
        provider = lambda n=cfg.name: self._servers.get(n)  # noqa: E731
        if has_resources:
            res_handler = _McpResourceToolHandler(
                config_provider=provider, client_provider=self._pool_client
            )
            list_name, read_name = _resource_tool_names(cfg.name)
            self._tools.register(
                list_name,
                res_handler.list_resources,
                schema=_list_resources_schema(cfg.name),
            )
            self._tools.register(
                read_name,
                res_handler.read_resource,
                schema=_read_resource_schema(cfg.name),
            )
            qnames.extend([list_name, read_name])
        if has_prompts:
            prompt_handler = _McpPromptToolHandler(
                config_provider=provider, client_provider=self._pool_client
            )
            list_name, get_name = _prompt_tool_names(cfg.name)
            self._tools.register(
                list_name,
                prompt_handler.list_prompts,
                schema=_list_prompts_schema(cfg.name),
            )
            self._tools.register(
                get_name,
                prompt_handler.get_prompt,
                schema=_get_prompt_schema(cfg.name),
            )
            qnames.extend([list_name, get_name])

    def _unregister_tools(self, name: str) -> None:
        for qname in self._registered_qtools.pop(name, []):
            self._tools.unregister(qname)

    # ---- connect / discover ---------------------------------------------

    async def _acquire_client(self, cfg: McpServerConfig) -> McpTransportClient:
        """Return a LIVE persistent transport client for ``cfg`` (pool reuse).

        The MCP session model: reuse one spawned subprocess / HTTP client across
        many round-trips instead of re-spawning per operation. Returns the
        pooled client if it is still alive (``is_alive`` — a real
        returncode/closed probe); otherwise (never connected, or the child
        died) closes any stale client, spawns a fresh persistent one via
        ``connect()``, pools it, and returns it. Raises ``McpConnectionError``
        on connect failure (caller records the per-server error). Guarded by
        ``_pool_lock`` so concurrent invokes for the same server do not spawn
        duplicates.
        """
        async with self._pool_lock:
            client = self._pool.get(cfg.name)
            if client is not None and client.is_alive():
                return client
            # Stale / dead — drop it (best-effort close) and re-spawn.
            if client is not None:
                self._pool.pop(cfg.name, None)
                with contextlib.suppress(Exception):
                    await client.aclose()
            fresh = McpTransportClient(
                cfg,
                ssl_verify=self._ssl_verify,
                # Forward the live provider so this NEW (re)connection reads the
                # global SSL toggle at connect time.
                ssl_verify_provider=self._ssl_verify_provider,
            )
            await fresh.connect()  # spawn + initialize, KEPT open
            self._pool[cfg.name] = fresh
            return fresh

    async def _close_pooled(self, name: str) -> None:
        """Close + drop the pooled client for ``name`` (idempotent, best-effort)."""
        async with self._pool_lock:
            client = self._pool.pop(name, None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _pool_client(self, config: McpServerConfig) -> McpTransportClient:
        """Client-provider for capability handlers: a live pooled session."""
        return await self._acquire_client(config)

    async def _pool_call_tool(
        self, config: McpServerConfig, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        """Invoke ``tool_name`` on the server's PERSISTENT pooled session.

        Reuses the spawned subprocess (no per-call cold start). If the pooled
        session's transport died between calls (e.g. the server crashed), the
        first attempt raises, we drop the dead client and retry ONCE with a
        freshly-spawned session (State-Truth-First: probe + reconnect rather
        than assume the cached handle is good). A second failure propagates as a
        normal tool error (handled upstream → ``ok=False`` to the model).
        """
        client = await self._acquire_client(config)
        try:
            return await client.call_tool(tool_name, arguments)
        except (McpConnectionError, OSError, BrokenPipeError):
            # Dead session — drop it and retry once with a fresh spawn.
            await self._close_pooled(config.name)
            client = await self._acquire_client(config)
            return await client.call_tool(tool_name, arguments)

    async def _connect_and_register(self, cfg: McpServerConfig) -> McpServerStatus:
        # ── Three-way gate (layer 1 + 2): GLOBAL master switch AND the
        # per-server ``enabled`` switch. Either off → keep the config but
        # DISCONNECT + unregister every tool/resource/prompt so the model can
        # reach NOTHING of this server (identical thoroughness to the global
        # gate). Layer 3 (connected) is the actual connect attempt below.
        if not self._enabled:
            self._connected[cfg.name] = False
            self._errors[cfg.name] = "mcp disabled (chat_mcp_enabled=false)"
            self._tool_names[cfg.name] = ()
            self._resource_count[cfg.name] = 0
            self._prompt_count[cfg.name] = 0
            self._unregister_tools(cfg.name)
            await self._close_pooled(cfg.name)
            return self._status_for(cfg)
        if not cfg.enabled:
            self._connected[cfg.name] = False
            self._errors[cfg.name] = "server disabled (enabled=false)"
            self._tool_names[cfg.name] = ()
            self._resource_count[cfg.name] = 0
            self._prompt_count[cfg.name] = 0
            self._unregister_tools(cfg.name)
            await self._close_pooled(cfg.name)
            return self._status_for(cfg)
        try:
            # (Re)connect with the CURRENT config. Close any existing pooled
            # session first so an edited config (changed command/args/env/
            # headers via add_server/update) always spawns a FRESH process —
            # otherwise _acquire_client would reuse the stale old-config client.
            # The fresh session is then KEPT OPEN in the pool for subsequent
            # tools/call (no re-spawn per call; stateful servers keep state).
            await self._close_pooled(cfg.name)
            client = await self._acquire_client(cfg)
            tools = await client.list_tools()
            try:
                resources = await client.list_resources()
            except Exception:  # noqa: BLE001 — capability optional
                resources = ()
            try:
                prompts = await client.list_prompts()
            except Exception:  # noqa: BLE001 — capability optional
                prompts = ()
        except McpConnectionError as exc:
            self._connected[cfg.name] = False
            self._errors[cfg.name] = str(exc)
            self._tool_names[cfg.name] = ()
            self._resource_count[cfg.name] = 0
            self._prompt_count[cfg.name] = 0
            self._unregister_tools(cfg.name)
            await self._close_pooled(cfg.name)
            return self._status_for(cfg)
        except Exception as exc:  # noqa: BLE001 — never propagate out of the port
            self._connected[cfg.name] = False
            self._errors[cfg.name] = f"unexpected: {exc}"
            self._tool_names[cfg.name] = ()
            self._resource_count[cfg.name] = 0
            self._prompt_count[cfg.name] = 0
            self._unregister_tools(cfg.name)
            await self._close_pooled(cfg.name)
            return self._status_for(cfg)
        # Register the direct tools first (this resets the server's qtool list).
        self._register_tools(cfg, tools)
        # resources / prompts already discovered above (best-effort within
        # discover_all — a capability the server does not implement is an empty
        # tuple, not a failure).
        self._register_capability_tools(
            cfg,
            has_resources=bool(resources),
            has_prompts=bool(prompts),
        )
        self._resource_count[cfg.name] = len(resources)
        self._prompt_count[cfg.name] = len(prompts)
        self._connected[cfg.name] = True
        self._errors[cfg.name] = ""
        self._tool_names[cfg.name] = tuple(t.name for t in tools)
        return self._status_for(cfg)

    async def _safe_discover_resources(
        self, cfg: McpServerConfig
    ) -> tuple[McpResource, ...]:
        """Discover a connected server's resources; ``()`` if unsupported/failed.

        Reuses the server's PERSISTENT pooled session (no per-call spawn) — this
        is called for already-connected servers, so ``_acquire_client`` returns
        the live client. Never raises: a server that does not implement
        ``resources/list`` (rpc_error method-not-found) simply contributes no
        resources, so its resource capability tools are not registered.
        """
        try:
            client = await self._acquire_client(cfg)
            return await client.list_resources()
        except Exception:  # noqa: BLE001 — capability optional; best-effort
            return ()

    async def _safe_discover_prompts(
        self, cfg: McpServerConfig
    ) -> tuple[McpPrompt, ...]:
        """Discover a connected server's prompts; ``()`` if unsupported/failed.

        Reuses the persistent pooled session (no per-call spawn).
        """
        try:
            client = await self._acquire_client(cfg)
            return await client.list_prompts()
        except Exception:  # noqa: BLE001 — capability optional; best-effort
            return ()

    def _status_for(self, cfg: McpServerConfig) -> McpServerStatus:
        names = self._tool_names.get(cfg.name, ())
        return McpServerStatus(
            config=cfg,
            connected=self._connected.get(cfg.name, False),
            tool_count=len(names),
            tool_names=names,
            error=self._errors.get(cfg.name, ""),
        )

    # ---- port surface ----------------------------------------------------

    async def list_servers(self) -> tuple[McpServerStatus, ...]:
        async with self._lock:
            self._load_from_disk()
            return tuple(self._status_for(c) for c in self._servers.values())

    async def add_server(self, config: McpServerConfig) -> McpServerStatus:
        async with self._lock:
            self._load_from_disk()
            self._servers[config.name] = config
            self._persist_to_disk()
            return await self._connect_and_register(config)

    async def remove_server(self, name: str) -> bool:
        async with self._lock:
            self._load_from_disk()
            existed = name in self._servers
            cfg = self._servers.pop(name, None)
            self._unregister_tools(name)
            self._connected.pop(name, None)
            self._errors.pop(name, None)
            self._tool_names.pop(name, None)
            self._resource_count.pop(name, None)
            self._prompt_count.pop(name, None)
            if cfg is not None and self._secret_store is not None:
                # Drop any persisted header credentials for this server.
                for hname in cfg.headers:
                    try:
                        self._secret_store.delete(
                            _SECRET_SERVICE, f"{name}.{hname}"
                        )
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        pass
                # Drop any persisted secret ENV credentials for this server.
                for ekey in cfg.secret_env_keys:
                    try:
                        self._secret_store.delete(
                            _SECRET_SERVICE, f"{name}.env.{ekey}"
                        )
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        pass
            # Close + drop the persistent pooled session for this server.
            await self._close_pooled(name)
            if existed:
                self._persist_to_disk()
            return existed

    async def set_enabled(self, name: str, enabled: bool) -> McpServerStatus:
        async with self._lock:
            self._load_from_disk()
            cfg = self._servers.get(name)
            if cfg is None:
                return McpServerStatus(
                    config=McpServerConfig(name=name or "unknown", command="_"),
                    connected=False,
                    error="server not found",
                )
            if cfg.enabled == enabled:
                # No change — return the current status (idempotent).
                return self._status_for(cfg)
            # Persist the flip on the frozen VO (dataclasses.replace).
            import dataclasses

            new_cfg = dataclasses.replace(cfg, enabled=enabled)
            self._servers[name] = new_cfg
            self._persist_to_disk()
            # Apply: ON → (re)connect + register; OFF → the gate in
            # ``_connect_and_register`` disconnects + unregisters.
            return await self._connect_and_register(new_cfg)

    def global_enabled(self) -> bool:
        """Return the GLOBAL master switch state (the single truth source).

        Reflects ``self._enabled`` — seeded from the constructor default
        (Settings.chat_mcp_enabled) and thereafter overridden by the persisted
        ``global_enabled`` in ``mcp_servers.json`` / by :meth:`set_global_enabled`.

        State-Truth-First: ensures the persisted value has been loaded BEFORE
        reading, so a standalone call returns the on-disk truth rather than the
        (possibly stale) constructor seed. ``_load_from_disk`` is synchronous,
        idempotent (``if self._loaded: return`` guard reads the file at most
        once) and lock-free — so calling it here adds no async / lock and cannot
        deadlock, keeping this method's synchronous signature intact.
        """
        self._load_from_disk()
        return self._enabled

    async def set_global_enabled(self, enabled: bool) -> None:
        """Flip the GLOBAL master switch, persist it, and re-apply every server.

        The global switch is the layer-1 gate in the three-way AND
        (global AND per-server enabled AND connected). Turning it:

        * ON → each configured + per-server-enabled server is (re)connected and
          its tools/resources/prompts registered;
        * OFF → every server is disconnected + unregistered (the ``not
          self._enabled`` branch of :meth:`_connect_and_register`), while the
          persisted configs are KEPT so the user can flip it back on.

        State-Truth-First: the per-server ``connected`` / ``error`` fields are
        driven by the REAL connect attempt (no optimistic write). The new state
        is persisted to ``mcp_servers.json`` (``global_enabled``) so it survives
        a restart as the truth source (overriding the Settings seed).
        """
        async with self._lock:
            self._load_from_disk()
            if self._enabled == enabled:
                # No change — still ensure disk carries the current value.
                self._persist_to_disk()
                return
            self._enabled = enabled
            self._persist_to_disk()
            # Re-apply to every configured server (connect on / disconnect off);
            # the gate inside ``_connect_and_register`` handles both directions.
            for cfg in list(self._servers.values()):
                await self._connect_and_register(cfg)

    async def list_catalog(self) -> tuple[CuratedCatalogEntry, ...]:
        # USER-DRIVEN aggregation (no backend gate): return the static
        # ``curated`` source PLUS whatever dynamic ``registry`` entries are
        # already cached — but NEVER auto-fetch the network here. Opening the
        # marketplace panel must not silently reach out to a third-party
        # registry; the fetch only happens on ``refresh_catalog`` (the user's
        # explicit "load / refresh" click). On a cold start the cache is empty,
        # so this returns curated only until the user loads the registry source.
        return CURATED_CATALOG + (self._registry_cache or ())

    async def refresh_catalog(self) -> tuple[CuratedCatalogEntry, ...]:
        """Fetch the dynamic official-registry source ON DEMAND, return catalog.

        This is the ONLY method that reaches the network — invoked when the user
        explicitly picks the "registry" source and clicks "load / refresh" in
        the panel (their click IS the consent to reach out; there is no hidden
        operator flag). Forces a fresh fetch (bypasses the TTL) and merges the
        result into the cache. Graceful — a failed fetch degrades to curated +
        any prior cache and records ``registry_source_error`` (never raises).
        """
        registry_entries = await self._get_registry_entries(force=True)
        return CURATED_CATALOG + registry_entries

    async def browse_registry(
        self,
        *,
        search: str | None = None,
        cursor: str | None = None,
        limit: int = 30,
    ) -> tuple[tuple[CuratedCatalogEntry, ...], str | None]:
        """Browse ONE page of the dynamic official-registry source (user-driven).

        This is the search / pagination entry point for the marketplace (distinct
        from :meth:`list_catalog` — cached/curated — and :meth:`refresh_catalog`
        — first-page reload). Each call reaches the network (the user's search /
        "load more" click IS the consent), fetching the registry page for the
        given ``search`` (server-side ``name`` filter) and ``cursor``.

        Returns ``(entries, next_cursor)`` where ``next_cursor`` is ``None`` on
        the last page. Graceful: on a fetch failure it records the reason in
        ``registry_source_error`` and returns ``((), None)`` (NEVER raises) so
        the panel degrades to a soft banner rather than an error.

        The fetched entries are MERGED into ``self._registry_cache`` (accumulate,
        de-duped by id) so a subsequent :meth:`install_from_catalog`
        (``source="registry"``) — which resolves via the cache, no network — can
        find a browsed-but-not-first-page entry.
        """
        try:
            kwargs: dict[str, Any] = {"limit": limit}
            if self._registry_source_base_url:
                kwargs["base_url"] = self._registry_source_base_url
            if search:
                kwargs["search"] = search
            if cursor:
                kwargs["cursor"] = cursor
            entries, next_cursor = await _fetch_registry_page(**kwargs)
        except McpRegistrySourceError as exc:
            self._registry_source_error = str(exc)
            logger.warning("chat.mcp.registry_browse_failed reason=%s", exc)
            return (), None
        except Exception as exc:  # noqa: BLE001 — never propagate out of the port
            self._registry_source_error = f"unexpected: {exc}"
            logger.warning("chat.mcp.registry_browse_unexpected reason=%s", exc)
            return (), None
        self._registry_source_error = ""
        self._merge_registry_cache(entries)
        return entries, next_cursor

    def _merge_registry_cache(
        self, entries: tuple[CuratedCatalogEntry, ...]
    ) -> None:
        """Accumulate browsed registry entries into the cache (de-dup by id).

        Keeps the FIRST-seen entry for a given id (a later page's duplicate does
        not overwrite it). Preserves ordering: existing cached entries first,
        then the newly-seen ones — so "load more" appends rather than reshuffles.

        Bounded by :data:`_MAX_REGISTRY_CACHE` (soft cap): the cache exists ONLY
        so a subsequent ``install_from_catalog(source="registry")`` can resolve a
        browsed entry, so it must not grow unbounded across many searches. When
        the merge exceeds the cap we keep the MOST RECENT entries (drop the
        oldest head) — the just-browsed items the user is most likely to install
        stay resolvable.
        """
        existing = self._registry_cache or ()
        seen = {e.id for e in existing}
        merged = list(existing)
        for entry in entries:
            if entry.id in seen:
                continue
            seen.add(entry.id)
            merged.append(entry)
        if len(merged) > _MAX_REGISTRY_CACHE:
            # Drop the oldest (head) entries, keep the newest tail.
            merged = merged[-_MAX_REGISTRY_CACHE:]
        self._registry_cache = tuple(merged)
        self._registry_cache_at = time.monotonic()

    async def _get_registry_entries(
        self, *, force: bool
    ) -> tuple[CuratedCatalogEntry, ...]:
        """Fetch the dynamic ``registry`` entries (cache-first, TTL-bounded).

        Within the TTL a cached result is returned WITHOUT a network call. On a
        fetch failure the method degrades gracefully: it records the reason and
        returns the last good cache if any (else an empty tuple) — it NEVER
        raises, so the panel always loads (State-Truth-First: the truth is "we
        could not reach the registry", surfaced via ``registry_source_error``,
        not a crash). Only ``refresh_catalog`` calls this (user-driven fetch).
        """
        now = time.monotonic()
        # NOTE: today the ONLY caller is ``refresh_catalog`` with ``force=True``
        # (user-driven refresh), so this cache-fresh short-circuit is currently
        # unreachable. It is kept deliberately as a cache-first seam: a future
        # caller (e.g. a ``list_catalog`` that opts into a bounded auto-refresh)
        # can pass ``force=False`` to reuse a still-fresh cache within the TTL
        # without a network round-trip. Not dead by oversight — reserved.
        cache_fresh = (
            self._registry_cache is not None
            and (now - self._registry_cache_at) < self._registry_source_ttl_s
        )
        if not force and cache_fresh:
            return self._registry_cache or ()
        try:
            kwargs: dict[str, Any] = {}
            if self._registry_source_base_url:
                kwargs["base_url"] = self._registry_source_base_url
            entries = await _fetch_registry_entries(**kwargs)
        except McpRegistrySourceError as exc:
            self._registry_source_error = str(exc)
            logger.warning("chat.mcp.registry_source_failed reason=%s", exc)
            # Degrade: keep serving the last good cache if we have one.
            return self._registry_cache or ()
        except Exception as exc:  # noqa: BLE001 — never propagate out of the port
            self._registry_source_error = f"unexpected: {exc}"
            logger.warning("chat.mcp.registry_source_unexpected reason=%s", exc)
            return self._registry_cache or ()
        self._registry_cache = entries
        self._registry_cache_at = now
        self._registry_source_error = ""
        return entries

    def catalog_sources(self) -> tuple[str, ...]:
        """Return the catalog source ids the UI should offer in its selector.

        Always ``("curated", "registry")`` — the dynamic registry source is
        ALWAYS offered so the user can pick it and click "load / refresh" to
        fetch. This does NOT mean the network was hit: the entries only appear
        after the user explicitly refreshes. (Kept a method, not a constant, so
        a future policy could hide it, but there is no backend gate today.)
        """
        return ("curated", "registry")

    def registry_source_error(self) -> str:
        """Return the last dynamic-source degradation reason (empty when healthy)."""
        return self._registry_source_error

    def _find_catalog_entry(
        self, entry_id: str, *, source: str | None = None
    ) -> CuratedCatalogEntry | None:
        """Resolve a catalog entry by id (+ optional source) across both sources.

        When ``source`` is given it disambiguates a cross-source id collision
        (a dynamic registry server whose slug happens to equal a curated id):
        ``"curated"`` looks up ONLY the static set, ``"registry"`` looks up ONLY
        the cached dynamic entries — so the user always installs the exact card
        they clicked. When ``source`` is ``None`` (legacy / unspecified) curated
        wins on a collision (the static, trusted set takes precedence). The
        registry side is served from the in-memory cache (a prior
        ``list_catalog`` populated it); this lookup performs NO network request
        so an install never blocks on the registry being reachable.
        """
        if source == "registry":
            for entry in self._registry_cache or ():
                if entry.id == entry_id:
                    return entry
            return None
        if source == "curated":
            return get_catalog_entry(entry_id)
        curated = get_catalog_entry(entry_id)
        if curated is not None:
            return curated
        for entry in self._registry_cache or ():
            if entry.id == entry_id:
                return entry
        return None

    async def _refetch_registry_entry(
        self, entry_id: str
    ) -> CuratedCatalogEntry | None:
        """Best-effort single-id re-fetch of a registry entry (State-Truth-First).

        The in-memory ``_registry_cache`` is only a FAST PATH for install
        resolution; the official registry is the TRUE source. When a
        ``source="registry"`` install misses the cache — e.g. the user browsed
        past :data:`_MAX_REGISTRY_CACHE` items and the earlier entry was evicted,
        or a restart cleared the cache — we go back to the registry rather than
        immediately failing.

        Reuses :meth:`browse_registry` (which fetches the first page for the
        given ``search`` term, merges results into the cache, and NEVER raises —
        a network failure returns an empty page). We search by ``entry_id`` since
        the registry only matches ``server.name`` substrings and the id is the
        name's slug tail — often a substring of the name, so this frequently
        hits. Best-effort: if the id is not a name substring (page has no match)
        or the network is down, we return ``None`` and the caller falls back to
        the original :class:`McpCatalogInstallError` (self-heals: the user can
        re-browse to the item). It NEVER upgrades a 400 into a 500.
        """
        # browse_registry swallows McpRegistrySourceError / unexpected errors
        # (returns an empty page), so this cannot raise for a network problem.
        await self.browse_registry(search=entry_id, limit=50)
        return self._find_catalog_entry(entry_id, source="registry")

    async def install_from_catalog(
        self,
        entry_id: str,
        *,
        name: str | None = None,
        arg_values: dict[str, str] | None = None,
        env_values: dict[str, str] | None = None,
        header_values: dict[str, str] | None = None,
        source: str | None = None,
    ) -> McpServerStatus:
        """Materialise a catalog entry (curated OR registry) into a server.

        Resolves the entry across BOTH sources (curated first, then the cached
        registry — no network), builds the right :class:`McpServerConfig` for
        its transport (phase 1: stdio + arg substitution; phase 2: also remote
        sse/http with ``url`` + headers, and stdio + required env keys), then
        delegates the connect + persist to :meth:`add_server` so the SAME
        three-way gate + SecretStore externalisation apply. Secret HEADER values
        (remote servers) and secret ENV values (stdio servers) are both routed
        to the SecretStore (only a ``__secret__`` sentinel on disk; re-hydrated
        at load and injected into the child process at spawn) — never persisted
        plain-text / echoed back to the client.

        Raises :class:`McpCatalogInstallError` (a ``ValueError`` subclass → HTTP
        400) on an unknown entry / a missing required argument / env / header.
        """
        # Imported lazily to avoid an application→adapter import at module load
        # (the error type lives with the use case, application layer).
        from qai.chat.application.use_cases.manage_mcp_servers import (
            McpCatalogInstallError,
        )

        entry = self._find_catalog_entry(entry_id, source=source)
        if entry is None:
            # State-Truth-First fallback: a registry entry may have been evicted
            # from the local cache (soft cap) or lost across a restart. Before
            # failing, go back to the TRUE source and re-fetch it by id once.
            # Only for the registry side (curated is a static in-process set that
            # never needs a fetch). ``source is None`` also falls through here
            # because curated already missed above, so the intent is registry.
            if source != "curated":
                entry = await self._refetch_registry_entry(entry_id)
        if entry is None:
            raise McpCatalogInstallError(f"unknown catalog entry {entry_id!r}")
        config = _materialise_entry(
            entry,
            name=name,
            arg_values=dict(arg_values or {}),
            env_values=dict(env_values or {}),
            header_values=dict(header_values or {}),
            error_cls=McpCatalogInstallError,
        )
        return await self.add_server(config)

    async def test_server(self, name: str) -> McpServerStatus:
        async with self._lock:
            self._load_from_disk()
            cfg = self._servers.get(name)
            if cfg is None:
                return McpServerStatus(
                    config=McpServerConfig(name=name or "unknown", command="_"),
                    connected=False,
                    error="server not found",
                )
            return await self._connect_and_register(cfg)

    async def connect_all(self) -> None:
        """Connect every persisted server (called at startup when enabled).

        Best-effort: a failed server is recorded with its error and skipped;
        the others still connect.  A no-op when the registry is disabled.
        """
        async with self._lock:
            self._load_from_disk()
            if not self._enabled:
                return
            for cfg in list(self._servers.values()):
                await self._connect_and_register(cfg)

    async def aclose(self) -> None:
        # Tear down every persistent pooled session (terminate the spawned
        # subprocesses / close remote HTTP clients) and drop the registered tool
        # handlers so a re-wire does not double-register.
        async with self._pool_lock:
            clients = list(self._pool.values())
            self._pool.clear()
        for client in clients:
            with contextlib.suppress(Exception):
                await client.aclose()
        async with self._lock:
            for name in list(self._registered_qtools):
                self._unregister_tools(name)

    # ---- resources / prompts surface (MCP-RESOURCES-SURFACE) -------------
    # Every method here obeys the SAME gate as the tools surface:
    #   * registry disabled (``chat_mcp_enabled=false``) → empty / "mcp disabled"
    #   * per-server: only a CURRENTLY-connected server is queried; an
    #     unconnected / failed / unknown server is skipped (list) or errors
    #     (read/get). This is the SAME ``self._enabled`` + ``self._connected``
    #     truth the tools surface uses — no second, looser judgement.

    def _is_connected(self, name: str) -> bool:
        # Three-way AND (State-Truth-First — check all three explicitly rather
        # than relying only on the connect side-effect): GLOBAL master switch
        # AND the per-server ``enabled`` switch AND the live connection state.
        if not self._enabled:
            return False
        cfg = self._servers.get(name)
        if cfg is None or not cfg.enabled:
            return False
        return bool(self._connected.get(name, False))

    async def list_resources(self) -> tuple[McpResource, ...]:
        async with self._lock:
            self._load_from_disk()
            if not self._enabled:
                return ()
            out: list[McpResource] = []
            for cfg in self._servers.values():
                if not self._is_connected(cfg.name):
                    continue
                out.extend(await self._safe_discover_resources(cfg))
            return tuple(out)

    async def list_prompts(self) -> tuple[McpPrompt, ...]:
        async with self._lock:
            self._load_from_disk()
            if not self._enabled:
                return ()
            out: list[McpPrompt] = []
            for cfg in self._servers.values():
                if not self._is_connected(cfg.name):
                    continue
                out.extend(await self._safe_discover_prompts(cfg))
            return tuple(out)

    async def read_resource(self, server_name: str, uri: str) -> str:
        async with self._lock:
            self._load_from_disk()
            if not self._enabled:
                return "[mcp error] mcp disabled (chat_mcp_enabled=false)"
            if not self._is_connected(server_name):
                return f"[mcp error] server {server_name!r} is not connected"
            cfg = self._servers.get(server_name)
            if cfg is None:
                return f"[mcp error] server {server_name!r} not found"
        # Network call OUTSIDE the lock (the config is captured; a concurrent
        # remove only makes the read fail, never corrupts shared state). Reuses
        # the persistent pooled session (no per-call spawn).
        try:
            client = await self._acquire_client(cfg)
            return await client.read_resource(uri)
        except Exception as exc:  # noqa: BLE001 — surface as tool error text
            return f"[mcp error] read failed: {exc}"

    async def get_prompt(
        self, server_name: str, name: str, arguments: dict[str, Any]
    ) -> str:
        async with self._lock:
            self._load_from_disk()
            if not self._enabled:
                return "[mcp error] mcp disabled (chat_mcp_enabled=false)"
            if not self._is_connected(server_name):
                return f"[mcp error] server {server_name!r} is not connected"
            cfg = self._servers.get(server_name)
            if cfg is None:
                return f"[mcp error] server {server_name!r} not found"
        try:
            client = await self._acquire_client(cfg)
            return await client.get_prompt(name, arguments or {})
        except Exception as exc:  # noqa: BLE001 — surface as tool error text
            return f"[mcp error] get_prompt failed: {exc}"

    # ---- status projection accessors (for the HTTP routes) ----------------

    def resource_count(self, name: str) -> int:
        """Return the last-discovered resource count for a connected server."""
        return self._resource_count.get(name, 0)

    def prompt_count(self, name: str) -> int:
        """Return the last-discovered prompt count for a connected server."""
        return self._prompt_count.get(name, 0)
