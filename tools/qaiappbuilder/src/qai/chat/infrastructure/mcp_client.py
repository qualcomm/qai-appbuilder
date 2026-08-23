# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""MCP (Model Context Protocol) transport client.

This module implements the *transport* half of the chat context's MCP
integration: connecting to an MCP server, running the ``initialize`` handshake,
listing / calling its tools (``tools/list`` / ``tools/call``), and — for the
resources/prompts surface — listing + reading resources (``resources/list`` /
``resources/read``) and listing + rendering prompts (``prompts/list`` /
``prompts/get``).

Transport strategy
------------------
* **stdio** — self-contained: spawns ``command`` + ``args`` and exchanges
  newline-delimited JSON-RPC frames over the child's stdin/stdout.  Uses only
  ``asyncio.subprocess`` + stdlib; no MCP SDK required.  Cross-platform.

* **sse / http (streamable-HTTP)** — delegates to the official ``mcp`` Python
  SDK (``mcp.client.streamable_http`` / ``mcp.client.sse`` +
  ``mcp.ClientSession``).  This gives us:

  - MCP OAuth 2.0 PKCE flow (RFC 9728) — servers protected by Bearer tokens
    (e.g. the internal MCP hub) are handled automatically;
  - correct ``Mcp-Session-Id`` session management;
  - full SSE streaming support;
  - future spec compliance for free as the SDK evolves.

  The ``mcp`` package is already a runtime dependency of this project
  (``pyproject.toml``), so no new dependency is introduced.

The higher-level registry that owns config persistence + tool registration onto
the shared chat tool port lives in :mod:`qai.chat.adapters.mcp_client`.

Cross-context isolation
-----------------------
Imports only ``qai.chat.domain`` + stdlib + ``httpx`` + ``mcp`` SDK.
No imports of other bounded contexts (``context-isolation`` contract).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx

from qai.chat.domain.mcp_server import (
    McpPrompt,
    McpPromptArgument,
    McpResource,
    McpServerConfig,
    McpTool,
    McpTransport,
)
from qai.platform.config.settings import LOOPBACK_HOST, LOOPBACK_HOST_NAME
from qai.platform.net.port_allocator import resolve_bindable_port

logger = logging.getLogger("qai.chat.mcp_client")

__all__ = [
    "McpConnectionError",
    "McpTransportClient",
    "discover_tools",
    "call_tool",
    "discover_resources",
    "read_resource",
    "discover_prompts",
    "get_prompt",
]

# Protocol version we advertise in ``initialize`` (MCP spec revision).  Servers
# negotiate down if they support an older one; a mismatch is not fatal for the
# tools we use (tools/list + tools/call are stable across revisions).
_PROTOCOL_VERSION: str = "2024-11-05"

_CLIENT_INFO: dict[str, Any] = {"name": "qai-modelbuilder-chat", "version": "1.0"}

# Hard cap on a single line / response body we will parse (defence-in-depth: a
# rogue server must not be able to OOM us with an unbounded line).
_MAX_LINE_BYTES: int = 8 * 1024 * 1024


class McpConnectionError(RuntimeError):
    """Raised (internally) when a connect / handshake / call fails.

    The registry catches this and surfaces the reason via ``McpServerStatus``;
    it never propagates out of the port surface.
    """


class McpTransportClient:
    """A single live connection to one MCP server.

    Use as an async context manager::

        async with McpTransportClient(config) as client:
            tools = await client.list_tools()
            result = await client.call_tool("search", {"q": "x"})

    Implements the two transport families:

    * ``stdio`` — spawns ``command`` + ``args`` and exchanges newline-delimited
      JSON-RPC frames over the child's stdin/stdout.
    * ``sse`` / ``http`` — POSTs JSON-RPC frames to ``url`` and parses the
      JSON response (streamable-HTTP style; for a bare SSE endpoint the first
      ``data:`` JSON line is used).  ``headers`` (with any credential values
      already resolved by the caller) are sent on every request.

    Every network / spawn / parse failure raises :class:`McpConnectionError`.
    """

    def __init__(
        self, config: McpServerConfig, *, ssl_verify: bool = True,
        ssl_verify_provider: Callable[[], bool] | None = None,
        secret_store: Any | None = None,
        silent_oauth: bool = False,
    ) -> None:
        self._config = config
        self._ssl_verify = ssl_verify
        self._ssl_verify_provider = ssl_verify_provider
        # SecretStore for persisting OAuth tokens between sessions.
        # Injected by the registry (which has access to the platform DI graph);
        # None means tokens are not persisted (user must re-authorise on restart).
        self._secret_store = secret_store
        # When True, _run_oauth_flow raises McpConnectionError if no cached token
        # is found instead of opening a browser window.  Used by connect_all() at
        # startup so we never pop a browser without explicit user action.
        self._silent_oauth = silent_oauth
        # --- stdio transport state ---
        self._proc: asyncio.subprocess.Process | None = None
        self._rpc_id = 0
        self._lock = asyncio.Lock()
        # --- SDK-backed transport state (sse / http) ---
        self._sdk_session: Any | None = None
        self._sdk_exit_stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> "McpTransportClient":
        await self._connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---- persistent-session support --------------------------------------

    async def connect(self) -> None:
        """Open the connection and KEEP it open (persistent-session use).

        Unlike ``async with`` (one-shot: connect → one call → close), this lets
        a caller (the registry connection pool) spawn the subprocess / open the
        HTTP client ONCE and reuse it for many ``tools/call`` / ``*_list`` round
        trips — the MCP standard's long-lived session model. The caller is then
        responsible for calling :meth:`aclose` (the pool does, on
        remove/disable/shutdown). Idempotent-ish: intended to be called once per
        client instance.

        ``_connect`` is run in a FRESH asyncio Task so that anyio cancel scopes
        pushed by ``streamable_http_client`` / ``sse_client`` belong to that
        task's scope stack, not to the HTTP request task calling us.  Without
        this isolation the HTTP request task's scope stack is corrupted, causing
        ``RuntimeError("Attempted to exit a cancel scope …")`` when the request
        completes and anyio unwinds the caller's scope stack.
        """
        loop = asyncio.get_event_loop()
        task: asyncio.Task = loop.create_task(self._connect())
        await asyncio.shield(task)

    def is_alive(self) -> bool:
        """True iff this client's underlying transport is still usable."""
        if self._proc is not None:
            return self._proc.returncode is None
        # SDK-backed: session is alive as long as the exit stack is open
        return self._sdk_session is not None and self._sdk_exit_stack is not None

    # ---- lifecycle -------------------------------------------------------

    async def _connect(self) -> None:
        cfg = self._config
        if cfg.transport is McpTransport.STDIO:
            import time
            t0 = time.monotonic()
            await self._spawn_stdio()
            await self._initialize()
            elapsed = time.monotonic() - t0
            logger.info(
                "chat.mcp.stdio_connected name=%s cmd=%s elapsed_s=%.1f",
                cfg.name, cfg.command, elapsed,
            )
            return
        # ---- SDK-backed transports: sse / http (streamable-HTTP) ----
        # Use the official mcp SDK so we get OAuth 2.0 PKCE (RFC 9728),
        # correct Mcp-Session-Id handling, and full SSE streaming for free.
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            from mcp.client.sse import sse_client
        except ImportError as exc:
            raise McpConnectionError(
                "mcp SDK not installed; run: uv pip install mcp"
            ) from exc

        verify = (
            self._ssl_verify_provider()
            if self._ssl_verify_provider is not None
            else self._ssl_verify
        )
        url = cfg.url or ""

        # Probe whether the server requires OAuth before building the full
        # client.  A lightweight HEAD/GET to the well-known resource metadata
        # endpoint tells us; if the server returns 401 with a Bearer challenge
        # we switch to the OAuth-enabled client.  This probe is skipped when
        # the user has already supplied an explicit Authorization header.
        use_oauth = False
        has_auth_header = "Authorization" in cfg.headers or "authorization" in cfg.headers
        if has_auth_header:
            # Reject the connection early if the credential was never stored —
            # a literal "__secret__" value in the header means the secret store
            # could not be read at load time.  Connecting with this sentinel
            # would result in a misleading authentication error on the remote.
            auth_val = cfg.headers.get("Authorization") or cfg.headers.get("authorization", "")
            if auth_val == "__secret__":
                raise McpConnectionError(
                    f"credentials missing for {cfg.name!r}: "
                    "the Authorization header secret was not found in the secret store; "
                    "re-install the server to re-enter credentials"
                )
        if not has_auth_header:
            use_oauth = await _server_requires_oauth(url, verify)

        if use_oauth:
            logger.info(
                "chat.mcp.oauth_required name=%s url=%s",
                cfg.name, url,
            )
            # Run the OAuth PKCE flow BEFORE entering the SDK's anyio task group.
            # The SDK's streamable_http_client uses anyio internally; running the
            # callback server concurrently inside that task group causes cancel-scope
            # cross-task errors.  Instead we obtain the Bearer token upfront and
            # inject it as a plain Authorization header — the SDK then needs no
            # auth handler at all.
            token = await _run_oauth_flow(cfg, verify, self._secret_store,
                                           silent=self._silent_oauth)
            merged_headers = {**dict(cfg.headers), "Authorization": f"Bearer {token}"}
        else:
            merged_headers = dict(cfg.headers)

        # Build the httpx client only for streamable-HTTP transport where the
        # SDK accepts a pre-configured client.  For SSE transport, sse_client
        # manages its own httpx.AsyncClient internally — passing a pre-opened
        # client via httpx_client_factory causes "Cannot open a client instance
        # more than once" because sse_client uses "async with factory(...)" which
        # calls __aenter__ a second time on an already-opened client.
        # Instead, for SSE we pass a factory that creates a fresh closed client
        # each time, letting sse_client own the open/close lifecycle.
        http_client: httpx.AsyncClient | None = None
        if cfg.transport is not McpTransport.SSE:
            http_client = httpx.AsyncClient(
                headers=merged_headers,
                timeout=httpx.Timeout(cfg.timeout_s),
                verify=verify,
            )

        def _sse_client_factory(
            headers: dict | None = None,
            auth: object = None,
            timeout: "httpx.Timeout | None" = None,
            **_: object,
        ) -> httpx.AsyncClient:
            # sse_client passes timeout=httpx.Timeout(cfg_timeout, read=sse_read_timeout)
            # where sse_read_timeout defaults to 300 s — use it so SSE event reads and
            # POST responses don't hit the shorter cfg.timeout_s read deadline.
            # Override headers (our merged_headers with auth) and verify (TLS setting).
            effective_timeout = timeout if timeout is not None else httpx.Timeout(cfg.timeout_s)
            return httpx.AsyncClient(
                headers=merged_headers,
                timeout=effective_timeout,
                verify=verify,
            )

        stack = contextlib.AsyncExitStack()
        try:
            await stack.__aenter__()
            if http_client is not None:
                await stack.enter_async_context(http_client)
            if cfg.transport is McpTransport.SSE:
                read, write = await stack.enter_async_context(
                    sse_client(url,
                               timeout=cfg.timeout_s,
                               httpx_client_factory=_sse_client_factory)
                )
            else:  # HTTP / streamable-HTTP
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(url, http_client=http_client)
                )
            session: ClientSession = await stack.enter_async_context(
                ClientSession(read, write)
            )
            try:
                await asyncio.wait_for(session.initialize(), timeout=cfg.timeout_s)
            except asyncio.TimeoutError as exc:
                raise McpConnectionError(
                    f"sdk_connect_failed: initialize timed out after {cfg.timeout_s}s"
                ) from exc
        except McpConnectionError:
            await stack.aclose()
            raise
        except Exception as exc:
            await stack.aclose()
            # Python 3.11+ wraps multiple async task errors in an ExceptionGroup.
            # Unwrap one level so the log shows the real sub-exception, not just
            # "unhandled errors in a TaskGroup (1 sub-exception)".
            cause = exc
            if isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
                cause = exc.exceptions[0]
            raise McpConnectionError(f"sdk_connect_failed: {cause}") from cause
        self._sdk_session = session
        self._sdk_exit_stack = stack
        logger.info(
            "chat.mcp.sdk_connected name=%s transport=%s url=%s oauth=%s",
            cfg.name, cfg.transport.value, url, use_oauth,
        )

    async def _spawn_stdio(self) -> None:
        import os

        cfg = self._config
        # Unified certificate fallback for MCP stdio child processes in an
        # enterprise TLS-intercepting environment. This is the SINGLE choke
        # point that covers EVERY stdio server — curated (npx), dynamic
        # registry-installed (npm→npx), and user-custom — so we do NOT sprinkle
        # per-flag hacks into individual catalog entries. When a launcher
        # downloads its package on first launch, the corporate MITM CA would
        # otherwise trip a cert-verify failure and the server would never start.
        #
        # The marketplace is npx-only for NEW installs (Python uvx is not used —
        # see mcp_catalog / mcp_registry_source). But a previously-installed
        # server persisted in mcp_servers.json (e.g. an old ``time`` that used
        # ``uvx``) or a user-custom uv-based stdio server can still be spawned,
        # so we inject BOTH the Node and the uv certificate-disable vars to be
        # comprehensive — every stdio launcher's TLS verification is turned off
        # here, matching the "disable outbound cert validation" posture used by
        # every other outbound client in this TLS-intercepting deployment.
        #
        # Injection order (so the USER always wins):
        #   1. dict(os.environ)                         — inherit the ambient env
        #   2. cert-fallback defaults (below), but ONLY when cfg.env omits them
        #   3. cfg.env                                  — explicit user overrides
        #
        #   * NODE_TLS_REJECT_UNAUTHORIZED=0 — node/npx: skip cert validation.
        #   * UV_SYSTEM_CERTS=1 — uv/uvx (>=0.7): use the OS trust store (which
        #     holds the corporate CA) instead of uv's bundled roots. Current var.
        #   * UV_NATIVE_TLS=1 — uv/uvx (older): deprecated alias, kept so older
        #     bundled uv versions are also covered.
        env = dict(os.environ)
        # The cert-fallback defaults disable / relax the stdio launcher's TLS
        # verification (node/npx + uv/uvx) for an enterprise TLS-intercepting
        # deployment. They are injected ONLY when the unified ``ssl_verify``
        # switch is off (internal edition / self-signed corporate gateway),
        # matching the "disable outbound cert validation" posture used by every
        # other outbound client in that deployment. When ``ssl_verify`` is on
        # (external / packaged release) the launchers keep their normal cert
        # validation. A USER-supplied ``cfg.env`` value always wins either way.
        _cert_defaults = {
            "NODE_TLS_REJECT_UNAUTHORIZED": "0",
            "UV_SYSTEM_CERTS": "1",
            "UV_NATIVE_TLS": "1",
        }
        user_env = cfg.env or {}
        if not self._ssl_verify:
            for key, value in _cert_defaults.items():
                if key not in user_env:
                    env[key] = value
        if cfg.env:
            env.update(cfg.env)
        # Resolve the launcher to a full path BEFORE spawning. On Windows,
        # ``create_subprocess_exec`` calls ``CreateProcess`` directly, which
        # (unlike a shell) does NOT consult PATHEXT — so a bare ``npx`` fails
        # with WinError 2 even though a ``npx.CMD`` exists on PATH (the bundled
        # portable Node ships ``npx.CMD`` / ``npm.CMD`` / ``pnpm.CMD``, not
        # ``.exe``). ``shutil.which`` DOES honour PATHEXT, so it finds the
        # ``.CMD`` and returns its full path. We search ``env['PATH']`` (which
        # already has the bundled dirs prepended) so the resolved launcher is
        # OUR portable copy. If resolution fails we fall back to the raw command
        # (spawn will then raise a clear spawn_failed the caller surfaces).
        import shutil

        command = cfg.command or ""
        resolved = shutil.which(command, path=env.get("PATH")) if command else None
        launcher = resolved or command
        try:
            logger.debug(
                "chat.mcp.spawning name=%s command=%s resolved=%s args=%s cwd=%s",
                cfg.name,
                cfg.command,
                resolved,
                cfg.args,
                cfg.cwd,
            )
            self._proc = await asyncio.create_subprocess_exec(
                launcher,
                *cfg.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cfg.cwd or None,
            )
        except (FileNotFoundError, OSError, NotImplementedError, ValueError) as exc:
            logger.warning(
                "chat.mcp.spawn_failed name=%s cmd=%s resolved=%s args=%s exc=%s",
                cfg.name,
                cfg.command,
                resolved,
                cfg.args,
                exc,
            )
            raise McpConnectionError(f"spawn_failed: {exc}") from exc

    async def _diagnose_child(self) -> str:
        """Best-effort summary of *why* a stdio child died / stalled.

        Only meaningful for the ``stdio`` transport with a live ``_proc``
        handle.  On the error path (server closed the pipe / handshake timeout)
        the child's real cause of death usually landed on **stderr**, which the
        normal read loop deliberately ignores — so this reads a short tail of
        stderr (bounded by a small timeout so a still-alive child cannot block
        the error path) plus ``returncode`` if the child already exited.

        Returns a compact one-line summary like ``exit=1 stderr=<tail>`` (empty
        string when there is nothing useful, or when reading itself fails — this
        is *supplementary* diagnostics and must never mask the original error).
        """
        proc = self._proc
        if self._config.transport is not McpTransport.STDIO or proc is None:
            return ""
        parts: list[str] = []
        try:
            rc = proc.returncode
            if rc is not None:
                parts.append(f"exit={rc}")
            stderr = proc.stderr
            if stderr is not None:
                try:
                    # Short budget: a still-running child may never EOF stderr,
                    # so cap the read; whatever partial tail we get is enough.
                    data = await asyncio.wait_for(stderr.read(), timeout=2.0)
                except (TimeoutError, Exception):  # noqa: BLE001
                    data = b""
                if data:
                    text = data.decode("utf-8", errors="replace")
                    # Collapse whitespace + keep only the last ~500 chars so a
                    # verbose traceback cannot flood the log / error message.
                    text = " ".join(text.split())
                    if len(text) > 500:
                        text = "…" + text[-500:]
                    if text:
                        parts.append(f"stderr={text}")
        except Exception:  # noqa: BLE001 — diagnostics must never raise
            return ""
        return " ".join(parts)

    async def aclose(self) -> None:
        """Terminate the subprocess / close the SDK session.  Idempotent."""
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            stderr = getattr(proc, "stderr", None)
            if stderr is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(stderr.read(), timeout=1.0)
            with contextlib.suppress(ProcessLookupError, Exception):
                proc.terminate()
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(proc.wait(), timeout=3.0)
        if self._sdk_exit_stack is not None:
            stack = self._sdk_exit_stack
            self._sdk_exit_stack = None
            self._sdk_session = None
            with contextlib.suppress(Exception):
                await stack.aclose()

    # ---- JSON-RPC (stdio only) ------------------------------------------

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def _rpc(
        self, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send one JSON-RPC request (stdio transport only)."""
        if self._proc is None:
            raise McpConnectionError("not connected (stdio)")
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        return await self._rpc_stdio(request)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (stdio transport only)."""
        if self._proc is None or self._proc.stdin is None:
            return
        note = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        line = (json.dumps(note) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    async def _rpc_stdio(self, request: dict[str, Any]) -> Any:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise McpConnectionError("stdio pipes unavailable")
        async with self._lock:
            payload = (json.dumps(request) + "\n").encode("utf-8")
            try:
                proc.stdin.write(payload)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                raise McpConnectionError(f"write_failed: {exc}") from exc
            # Read lines until we get a JSON-RPC response matching our id
            # (skip server-initiated notifications / log lines).
            want_id = request["id"]
            deadline = self._config.timeout_s
            while True:
                try:
                    raw = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=deadline
                    )
                except TimeoutError as exc:
                    diag = await self._diagnose_child()
                    logger.warning(
                        "chat.mcp.stdio_timeout name=%s deadline_s=%s %s",
                        self._config.name,
                        deadline,
                        diag,
                    )
                    msg_txt = f"timeout after {deadline}s ({diag})".rstrip(" ()")
                    raise McpConnectionError(msg_txt) from exc
                if not raw:
                    diag = await self._diagnose_child()
                    logger.warning(
                        "chat.mcp.stdio_closed name=%s %s",
                        self._config.name,
                        diag,
                    )
                    raise McpConnectionError(
                        f"server closed connection ({diag})"
                        if diag
                        else "server closed connection"
                    )
                if len(raw) > _MAX_LINE_BYTES:
                    raise McpConnectionError("response line too large")
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except (ValueError, TypeError):
                    # Non-JSON stdout line (server log) — skip.
                    continue
                if not isinstance(msg, dict) or msg.get("id") != want_id:
                    continue
                return _unwrap_rpc_result(msg)

    # ---- MCP methods -----------------------------------------------------
    # stdio: go through self._rpc (JSON-RPC over subprocess pipes)
    # sse/http: delegate to the SDK ClientSession directly

    async def _initialize(self) -> None:
        """stdio-only: run the MCP initialize handshake."""
        try:
            await self._rpc(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
            )
        except McpConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise McpConnectionError(f"initialize_failed: {exc}") from exc
        await self._notify("notifications/initialized")

    async def list_tools(self) -> tuple[McpTool, ...]:
        """Return the tools the server advertises."""
        if self._sdk_session is not None:
            result = await self._sdk_session.list_tools()
            raw_tools = getattr(result, "tools", []) or []
            return tuple(
                McpTool(
                    server_name=self._config.name,
                    name=t.name,
                    description=str(t.description or ""),
                    schema=dict(t.inputSchema) if t.inputSchema else {},
                )
                for t in raw_tools
            )
        # stdio path
        result = await self._rpc("tools/list", {})
        raw_tools = result.get("tools") or [] if isinstance(result, dict) else []
        out: list[McpTool] = []
        for entry in raw_tools:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            schema = entry.get("inputSchema")
            out.append(McpTool(
                server_name=self._config.name,
                name=name,
                description=str(entry.get("description") or ""),
                schema=dict(schema) if isinstance(schema, dict) else {},
            ))
        return tuple(out)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call ``tool_name`` and return its consolidated textual result."""
        args = _sanitize_tool_arguments(arguments, self._config.url)
        logger.info(
            "chat.mcp.call_tool name=%s tool=%s args=%s",
            self._config.name, tool_name, args,
        )
        timeout = self._config.timeout_s
        if self._sdk_session is not None:
            try:
                result = await asyncio.wait_for(
                    self._sdk_session.call_tool(tool_name, args),
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                raise McpConnectionError(
                    f"tool call timed out after {timeout}s"
                ) from exc
            rendered = _render_sdk_tool_result(result)
            logger.info(
                "chat.mcp.call_tool_result name=%s tool=%s result_len=%d result_preview=%s",
                self._config.name, tool_name, len(rendered),
                rendered[:200] if rendered else "(empty)",
            )
            return rendered
        result = await self._rpc(
            "tools/call", {"name": tool_name, "arguments": args}
        )
        rendered = _render_tool_result(result)
        logger.info(
            "chat.mcp.call_tool_result name=%s tool=%s result_len=%d result_preview=%s",
            self._config.name, tool_name, len(rendered),
            rendered[:200] if rendered else "(empty)",
        )
        return rendered

    async def list_resources(self) -> tuple[McpResource, ...]:
        """Return the resources the server advertises."""
        if self._sdk_session is not None:
            result = await self._sdk_session.list_resources()
            raw = getattr(result, "resources", []) or []
            return tuple(
                McpResource(
                    server_name=self._config.name,
                    uri=str(r.uri),
                    name=str(r.name or ""),
                    mime_type=str(getattr(r, "mimeType", "") or ""),
                )
                for r in raw
            )
        result = await self._rpc("resources/list", {})
        raw = result.get("resources") or [] if isinstance(result, dict) else []
        out: list[McpResource] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            uri = entry.get("uri")
            if not isinstance(uri, str) or not uri:
                continue
            out.append(McpResource(
                server_name=self._config.name,
                uri=uri,
                name=str(entry.get("name") or ""),
                mime_type=str(entry.get("mimeType") or ""),
            ))
        return tuple(out)

    async def read_resource(self, uri: str) -> str:
        """Read one resource by ``uri``; return its text."""
        if self._sdk_session is not None:
            from mcp.types import AnyUrl
            result = await self._sdk_session.read_resource(AnyUrl(uri))
            return _render_resource_read_sdk(result)
        result = await self._rpc("resources/read", {"uri": uri})
        return _render_resource_read(result)

    async def list_prompts(self) -> tuple[McpPrompt, ...]:
        """Return the prompts the server advertises."""
        if self._sdk_session is not None:
            result = await self._sdk_session.list_prompts()
            raw = getattr(result, "prompts", []) or []
            out: list[McpPrompt] = []
            for p in raw:
                args_raw = getattr(p, "arguments", []) or []
                args = [
                    McpPromptArgument(
                        name=a.name,
                        description=str(a.description or ""),
                        required=bool(getattr(a, "required", False)),
                    )
                    for a in args_raw
                ]
                out.append(McpPrompt(
                    server_name=self._config.name,
                    name=p.name,
                    description=str(p.description or ""),
                    arguments=tuple(args),
                ))
            return tuple(out)
        result = await self._rpc("prompts/list", {})
        raw = result.get("prompts") or [] if isinstance(result, dict) else []
        out2: list[McpPrompt] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            args_raw = entry.get("arguments") or []
            args2: list[McpPromptArgument] = []
            if isinstance(args_raw, list):
                for a in args_raw:
                    if not isinstance(a, dict):
                        continue
                    aname = a.get("name")
                    if not isinstance(aname, str) or not aname:
                        continue
                    args2.append(McpPromptArgument(
                        name=aname,
                        description=str(a.get("description") or ""),
                        required=bool(a.get("required", False)),
                    ))
            out2.append(McpPrompt(
                server_name=self._config.name,
                name=name,
                description=str(entry.get("description") or ""),
                arguments=tuple(args2),
            ))
        return tuple(out2)

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> str:
        """Render a named prompt via ``prompts/get``; return its text."""
        if self._sdk_session is not None:
            result = await self._sdk_session.get_prompt(name, arguments or {})
            return _render_prompt_get_sdk(result)
        result = await self._rpc(
            "prompts/get", {"name": name, "arguments": arguments or {}}
        )
        return _render_prompt_get(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unwrap_rpc_result(msg: dict[str, Any]) -> Any:
    """Return ``msg['result']`` or raise :class:`McpConnectionError` on error."""
    if "error" in msg and msg["error"] is not None:
        err = msg["error"]
        if isinstance(err, dict):
            raise McpConnectionError(
                f"rpc_error: {err.get('message') or err.get('code') or err}"
            )
        raise McpConnectionError(f"rpc_error: {err}")
    return msg.get("result")


def _first_sse_json(body: str) -> Any:
    """Extract the first ``data:`` JSON object from an SSE response body."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                return json.loads(data)
            except (ValueError, TypeError):
                continue
    raise McpConnectionError("no data frame in SSE response")


def _render_tool_result(result: Any) -> str:
    """Render an MCP ``tools/call`` result into plain text for the LLM."""
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False) if result is not None else ""
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
    text = "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)
    if result.get("isError"):
        return f"[mcp error] {text}"
    return text


async def discover_tools(config: McpServerConfig) -> tuple[McpTool, ...]:
    """One-shot connect → list tools → close.  Raises on any failure."""
    async with McpTransportClient(config) as client:
        return await client.list_tools()


async def call_tool(
    config: McpServerConfig, tool_name: str, arguments: dict[str, Any]
) -> str:
    """One-shot connect → call one tool → close.  Raises on any failure."""
    async with McpTransportClient(config) as client:
        return await client.call_tool(tool_name, arguments)


def _render_resource_read(result: Any) -> str:
    """Render an MCP ``resources/read`` result into plain text for the LLM."""
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False) if result is not None else ""
    parts: list[str] = []
    contents = result.get("contents")
    if isinstance(contents, list):
        for block in contents:
            if not isinstance(block, dict):
                continue
            if isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block.get("blob"), str):
                mime = str(block.get("mimeType") or "application/octet-stream")
                approx = len(block["blob"])
                parts.append(f"[binary {mime} ~{approx} base64 chars]")
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)


def _render_prompt_get(result: Any) -> str:
    """Render an MCP ``prompts/get`` result into plain text for the LLM."""
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False) if result is not None else ""
    parts: list[str] = []
    desc = result.get("description")
    if isinstance(desc, str) and desc.strip():
        parts.append(desc.strip())
    messages = result.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = msg.get("content")
            text = ""
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                text = content["text"]
            elif isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # Some servers return an array of content blocks.
                chunks = [
                    b["text"]
                    for b in content
                    if isinstance(b, dict) and isinstance(b.get("text"), str)
                ]
                text = "\n".join(chunks)
            else:
                text = json.dumps(content, ensure_ascii=False)
            parts.append(f"{role}: {text}" if role else text)
    return "\n\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)


async def discover_resources(config: McpServerConfig) -> tuple[McpResource, ...]:
    """One-shot connect → list resources → close.  Raises on any failure."""
    async with McpTransportClient(config) as client:
        return await client.list_resources()


async def read_resource(config: McpServerConfig, uri: str) -> str:
    """One-shot connect → read one resource → close.  Raises on any failure."""
    async with McpTransportClient(config) as client:
        return await client.read_resource(uri)


async def discover_prompts(config: McpServerConfig) -> tuple[McpPrompt, ...]:
    """One-shot connect → list prompts → close.  Raises on any failure."""
    async with McpTransportClient(config) as client:
        return await client.list_prompts()


async def discover_all(
    config: McpServerConfig,
) -> tuple[tuple[McpTool, ...], tuple[McpResource, ...], tuple[McpPrompt, ...]]:
    """One-shot connect → list tools + resources + prompts → close.

    PERFORMANCE: this reuses a SINGLE spawned subprocess + MCP handshake for all
    three discovery calls, instead of the three separate connects that calling
    ``discover_tools`` / ``discover_resources`` / ``discover_prompts`` in turn
    would incur. Spawning ``npx`` and completing the handshake costs several
    seconds EACH on the target platform, so collapsing 3 spawns → 1 cuts a
    server's connect time to roughly a third.

    ``tools`` is REQUIRED — if the server cannot list tools the connection is
    considered failed and the error propagates (``McpConnectionError``). Then
    ``resources`` and ``prompts`` are best-effort on the SAME connection: a
    server that does not implement that capability returns an rpc error which we
    swallow (→ empty tuple), exactly mirroring the adapter's previous
    ``_safe_discover_*`` behaviour.
    """
    async with McpTransportClient(config) as client:
        tools = await client.list_tools()  # required — propagates on failure
        try:
            resources = await client.list_resources()
        except Exception:  # noqa: BLE001 — capability optional / rpc error
            resources = ()
        try:
            prompts = await client.list_prompts()
        except Exception:  # noqa: BLE001 — capability optional / rpc error
            prompts = ()
        return tools, resources, prompts


async def get_prompt(
    config: McpServerConfig, name: str, arguments: dict[str, Any]
) -> str:
    """One-shot connect → render one prompt → close.  Raises on any failure."""
    async with McpTransportClient(config) as client:
        return await client.get_prompt(name, arguments)


# ---------------------------------------------------------------------------
# Argument sanitisation
# ---------------------------------------------------------------------------


def _is_qgenie_mcphub_url(url: str) -> bool:
    """Return True if *url* points to the QGenie MCPHub host.

    Uses the loader so the domain literal never appears in this file
    (edition dual-form §7). Returns False gracefully when the loader is
    absent (external edition) or the url is empty/non-HTTP.
    """
    if not url:
        return False
    try:
        import urllib.parse as _up
        host = _up.urlparse(url).netloc.lower()
        if not host:
            return False
        try:
            from qai.platform.edition.loader import get_mcp_hub_urls
            h = get_mcp_hub_urls()
        except ImportError:
            return False
        for key in ("qgenie_mcphub_url", "ceflow_mcphub_url"):
            ref = h.get(key) or ""
            if ref:
                ref_host = _up.urlparse(ref).netloc.lower()
                if ref_host and host == ref_host:
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _sanitize_tool_arguments(
    arguments: dict[str, Any] | None,
    server_url: str,
) -> dict[str, Any]:
    """Strip underscore-prefixed keys injected by the LLM.

    The LLM occasionally hallucinates ``_raw``, ``_meta``, or other ``_``-
    prefixed keys that are not part of any real tool schema. No MCP server
    accepts these, so we drop them universally (not just for MCPHub).
    """
    args = arguments or {}
    cleaned = {k: v for k, v in args.items() if not k.startswith("_")}
    if len(cleaned) != len(args):
        _dropped = [k for k in args if k.startswith("_")]
        logger.debug(
            "chat.mcp.sanitize_args.dropped server_url=%s dropped=%s",
            server_url, _dropped,
        )
    return cleaned


# ---------------------------------------------------------------------------
# SDK result renderers
# ---------------------------------------------------------------------------

def _render_sdk_tool_result(result: Any) -> str:
    """Render an mcp.ClientSession.call_tool() result into plain text."""
    if result is None:
        return ""
    # SDK returns a CallToolResult with .content list and .isError bool
    parts: list[str] = []
    content = getattr(result, "content", None)
    if isinstance(content, list):
        for block in content:
            btype = getattr(block, "type", None)
            if btype == "text":
                parts.append(str(getattr(block, "text", "")))
            else:
                # image / resource / other: JSON-serialise the block
                try:
                    parts.append(json.dumps(
                        block.model_dump() if hasattr(block, "model_dump") else vars(block),
                        ensure_ascii=False,
                    ))
                except Exception:  # noqa: BLE001
                    parts.append(str(block))
    text = "\n".join(parts) if parts else str(result)
    if getattr(result, "isError", False):
        return f"[mcp error] {text}"
    return text


def _render_resource_read_sdk(result: Any) -> str:
    """Render an mcp.ClientSession.read_resource() result into plain text."""
    if result is None:
        return ""
    parts: list[str] = []
    contents = getattr(result, "contents", None)
    if isinstance(contents, list):
        for block in contents:
            if hasattr(block, "text") and isinstance(block.text, str):
                parts.append(block.text)
            elif hasattr(block, "blob"):
                mime = str(getattr(block, "mimeType", "application/octet-stream") or "")
                approx = len(block.blob) if block.blob else 0
                parts.append(f"[binary {mime} ~{approx} base64 chars]")
            else:
                try:
                    parts.append(json.dumps(
                        block.model_dump() if hasattr(block, "model_dump") else vars(block),
                        ensure_ascii=False,
                    ))
                except Exception:  # noqa: BLE001
                    parts.append(str(block))
    return "\n".join(parts) if parts else str(result)


def _render_prompt_get_sdk(result: Any) -> str:
    """Render an mcp.ClientSession.get_prompt() result into plain text."""
    if result is None:
        return ""
    parts: list[str] = []
    desc = getattr(result, "description", None)
    if isinstance(desc, str) and desc.strip():
        parts.append(desc.strip())
    messages = getattr(result, "messages", None)
    if isinstance(messages, list):
        for msg in messages:
            role = str(getattr(msg, "role", "") or "")
            content = getattr(msg, "content", None)
            if content is None:
                text = ""
            elif hasattr(content, "text"):
                text = str(content.text or "")
            elif isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    str(getattr(b, "text", b)) for b in content
                )
            else:
                try:
                    text = json.dumps(
                        content.model_dump() if hasattr(content, "model_dump") else vars(content),
                        ensure_ascii=False,
                    )
                except Exception:  # noqa: BLE001
                    text = str(content)
            parts.append(f"{role}: {text}" if role else text)
    return "\n\n".join(parts) if parts else str(result)


# ---------------------------------------------------------------------------
# MCP OAuth 2.0 PKCE support
# ---------------------------------------------------------------------------
# Servers protected by Bearer tokens (e.g. the internal MCP hub) use
# the MCP OAuth 2.0 Authorization Code + PKCE flow (RFC 9728).  The SDK's
# OAuthClientProvider implements this flow; we wire it into the httpx.AsyncClient
# that is passed to streamable_http_client / sse_client.
#
# Key pieces:
#   McpOAuthTokenStorage  — persists access/refresh tokens + client_info in
#                           the project's SecretStore (keyring-backed), keyed
#                           by server name.  Tokens survive process restarts.
#   _start_callback_server_thread — one-shot HTTP callback server in a daemon
#                           thread; sets an asyncio Future when the OAuth
#                           redirect arrives (no asyncio.start_server).
#   _build_oauth_http_client — constructs the httpx.AsyncClient with
#                           OAuthClientProvider injected as the auth handler.
# ---------------------------------------------------------------------------

_OAUTH_REDIRECT_PORT_RANGE: tuple[int, int] = (49152, 49200)  # ephemeral range

_OAUTH_CALLBACK_PATH: str = "/mcp_oauth_callback"
_OAUTH_CLIENT_NAME: str = "QAI ModelBuilder"
_OAUTH_SCOPE: str = "mcp"


def _allocate_oauth_redirect_port() -> int:
    """Pick a loopback port the OAuth callback server can really bind.

    Delegates to :func:`qai.platform.net.port_allocator.resolve_bindable_port`
    so the probe uses the project's single bind-based implementation — which on
    Windows sets ``SO_EXCLUSIVEADDRUSE``, and so does not falsely succeed
    against a port another process holds with ``SO_REUSEADDR``.

    Raises :class:`NoBindablePortError` when the whole range is taken. Raising
    matters more than it looks: the caller bakes the returned port into the
    ``redirect_uri`` it sends to the authorization server *before* the callback
    listener starts, so returning an unbindable port would let the user
    authenticate successfully and then strand the redirect on a port nobody is
    listening on — a spinner that runs to timeout with no cause in the logs.
    """
    return resolve_bindable_port(
        LOOPBACK_HOST,
        fallbacks=range(*_OAUTH_REDIRECT_PORT_RANGE),
    )


def _oauth_open_browser_sync(
    authorization_url: str,
    name: str,
) -> None:
    """Open *authorization_url* in the user's browser — runs in a daemon thread.

    Hands the URL to the platform's default handler; the OAuth callback server
    captures the token when the redirect lands.
    """
    import subprocess as _subprocess
    import sys as _sys

    logger.info("chat.mcp.oauth_browser_open name=%s", name)
    try:
        if _sys.platform == "win32":
            import os as _os2
            _os2.startfile(authorization_url)  # type: ignore[attr-defined]
        elif _sys.platform == "darwin":
            _subprocess.Popen(["open", authorization_url])
        else:
            _subprocess.Popen(["xdg-open", authorization_url])
    except Exception:  # noqa: BLE001
        import webbrowser
        webbrowser.open(authorization_url)


async def _server_requires_oauth(url: str, verify: bool) -> bool:
    """Return True if the MCP server responds with a Bearer 401 challenge.

    Sends a minimal ``initialize`` probe and inspects the response.  This is
    a cheap one-shot check: if the server returns 401 with
    ``WWW-Authenticate: Bearer ...`` we know OAuth is required.  Any other
    response (200, 4xx without Bearer, network error) returns False so we
    fall back to the plain client and let the real connection attempt surface
    the actual error.
    """
    try:
        async with httpx.AsyncClient(verify=verify, timeout=httpx.Timeout(8.0)) as probe:
            r = await probe.post(
                url,
                json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                      "params": {"protocolVersion": _PROTOCOL_VERSION,
                                 "capabilities": {}, "clientInfo": _CLIENT_INFO}},
                headers={"Accept": "application/json, text/event-stream"},
            )
            if r.status_code == 401:
                www_auth = r.headers.get("www-authenticate", "")
                return www_auth.lower().startswith("bearer")
    except Exception:  # noqa: BLE001 — probe failure → not OAuth
        pass
    return False


class McpOAuthTokenStorage:
    """SecretStore-backed TokenStorage for MCP OAuth tokens.

    Persists ``OAuthToken`` and ``OAuthClientInformationFull`` in the project's
    SecretStore (keyring on Windows, encrypted file fallback) so that tokens
    survive process restarts and the user only needs to authorise once per
    server.

    Keys used (service = ``chat_mcp``):
      ``<server_name>.oauth_token``       — JSON-serialised OAuthToken
      ``<server_name>.oauth_client_info`` — JSON-serialised OAuthClientInformationFull
    """

    def __init__(self, server_name: str, secret_store: Any) -> None:
        self._name = server_name
        self._store = secret_store  # qai.platform.persistence.secrets SecretStore

    # ---- TokenStorage protocol ----

    async def get_tokens(self) -> Any | None:
        """Return stored OAuthToken or None."""
        try:
            from mcp.shared.auth import OAuthToken
            raw = self._store.get("chat_mcp", f"{self._name}.oauth_token")
            return OAuthToken.model_validate_json(raw) if raw else None
        except Exception:  # noqa: BLE001
            return None

    async def set_tokens(self, tokens: Any) -> None:
        """Persist OAuthToken."""
        try:
            self._store.set("chat_mcp", f"{self._name}.oauth_token",
                            tokens.model_dump_json())
        except Exception as _exc:  # noqa: BLE001
            logger.warning("chat.mcp.oauth_token_set_failed server=%s: %s", self._name, _exc)

    async def get_client_info(self) -> Any | None:
        """Return stored OAuthClientInformationFull or None."""
        try:
            from mcp.shared.auth import OAuthClientInformationFull
            raw = self._store.get("chat_mcp", f"{self._name}.oauth_client_info")
            return OAuthClientInformationFull.model_validate_json(raw) if raw else None
        except Exception:  # noqa: BLE001
            return None

    async def set_client_info(self, client_info: Any) -> None:
        """Persist OAuthClientInformationFull."""
        try:
            self._store.set("chat_mcp", f"{self._name}.oauth_client_info",
                            client_info.model_dump_json())
        except Exception as _exc:  # noqa: BLE001
            logger.warning("chat.mcp.oauth_client_info_set_failed server=%s: %s", self._name, _exc)


def _start_callback_server_thread(
    port: int,
    result_future: "asyncio.Future[tuple[str | None, str | None]]",
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Start a one-shot OAuth callback HTTP server in a plain daemon thread.

    Sets result_future via loop.call_soon_threadsafe so the asyncio side
    can await it without touching anyio cancel scopes.
    """
    import http.server as _http_server
    import urllib.parse as _urlparse
    import threading as _threading

    _stop = _threading.Event()

    class _Handler(_http_server.BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:  # type: ignore[override]
            pass

        def do_GET(self) -> None:  # noqa: N802
            parsed = _urlparse.urlparse(self.path)
            params = _urlparse.parse_qs(parsed.query)
            code: str | None = (params.get("code") or [None])[0]
            state: str | None = (params.get("state") or [None])[0]
            body = (
                b"<html><body style='font-family:sans-serif;padding:2em'>"
                b"<h2>&#10003; QAI ModelBuilder authorised</h2>"
                b"<p>You can close this tab and return to the app.</p>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            if not result_future.done():
                loop.call_soon_threadsafe(result_future.set_result, (code, state))
            _stop.set()

    def _serve() -> None:
        try:
            srv = _http_server.HTTPServer((LOOPBACK_HOST, port), _Handler)
            srv.timeout = 1.0
            while not _stop.is_set():
                srv.handle_request()
            srv.server_close()
        except Exception as _exc:
            logger.warning("chat.mcp.oauth_cb_server_error port=%d: %s", port, _exc)
            if not result_future.done():
                loop.call_soon_threadsafe(result_future.set_result, (None, None))

    import threading
    threading.Thread(target=_serve, daemon=True, name=f"mcp-oauth-cb-{port}").start()


async def _run_oauth_flow(
    cfg: McpServerConfig,
    verify: bool,
    secret_store: Any | None,
    *,
    silent: bool = False,
) -> str:
    """Run the full OAuth 2.0 PKCE flow and return the access token string.

    If a valid (non-expired) token is already in storage, return it immediately
    without any user interaction.  Otherwise:
    1. Dynamic-register the client with the auth server.
    2. Build the PKCE authorization URL and open the browser.
    3. Start a local HTTP server to receive the redirect callback.
    4. Exchange the auth code for tokens, persist them, return access_token.

    This function completes BEFORE any anyio task group is entered, so there
    are no cancel-scope cross-task conflicts with streamable_http_client.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    url = cfg.url or ""
    storage = McpOAuthTokenStorage(cfg.name, secret_store) if secret_store else _NullTokenStorage()

    # Fast path: reuse a cached valid token
    existing = await storage.get_tokens()
    if existing and existing.access_token:
        # Token expiry: OAuthToken.expires_in is seconds-from-issue, not absolute.
        # The SDK does not store issue time, so we cannot check expiry precisely.
        # Return it optimistically; if the server rejects it the caller will
        # get an http_status_401 error and the user can retry (which re-runs this
        # flow and gets a fresh token via the refresh_token path).
        logger.info("chat.mcp.oauth_token_cached name=%s", cfg.name)
        return existing.access_token

    # No cached token.  In silent mode (connect_all at startup) we do not open
    # a browser — the user will trigger OAuth manually via the MCP panel.
    if silent:
        raise McpConnectionError(
            f"OAuth token not cached for {cfg.name!r}; "
            "install via the MCP panel to authenticate"
        )
    port = _allocate_oauth_redirect_port()

    redirect_uri = f"http://{LOOPBACK_HOST_NAME}:{port}{_OAUTH_CALLBACK_PATH}"

    client_metadata = OAuthClientMetadata(
        redirect_uris=[redirect_uri],  # type: ignore[arg-type]
        client_name=_OAUTH_CLIENT_NAME,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=_OAUTH_SCOPE,
        token_endpoint_auth_method="none",  # PKCE public client
    )

    loop = asyncio.get_event_loop()
    cb_future: asyncio.Future = loop.create_future()  # type: ignore[type-arg]
    _start_callback_server_thread(port, cb_future, loop)

    async def redirect_handler(authorization_url: str) -> None:
        logger.info("chat.mcp.oauth_redirect name=%s", cfg.name)
        import threading as _thr
        t = _thr.Thread(
            target=_oauth_open_browser_sync,
            args=(authorization_url, cfg.name),
            daemon=True,
        )
        t.start()

    async def callback_handler() -> tuple[str, str | None]:
        """Wait for the OAuth callback and return (code, state).

        Uses asyncio.sleep polling instead of asyncio.wait_for to avoid
        creating anyio cancel scopes that conflict with the SDK task group.
        """
        deadline = asyncio.get_event_loop().time() + 300.0
        while not cb_future.done():
            if asyncio.get_event_loop().time() >= deadline:
                raise McpConnectionError(
                    "OAuth callback timed out after 300s — user did not authorise in time"
                )
            await asyncio.sleep(0.2)
        code, state = cb_future.result()
        if not code:
            raise McpConnectionError("OAuth callback received no auth code")
        return code, state

    auth_provider = OAuthClientProvider(
        server_url=url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    # Use a plain httpx client (no anyio) for the OAuth token exchange.
    async with httpx.AsyncClient(
        verify=verify,
        timeout=httpx.Timeout(30.0),
        auth=auth_provider,
    ) as oauth_http:
        # Send a real initialize request through the auth-enabled client.
        # httpx drives async_auth_flow automatically:
        #   1. auth_provider discovers protected resource metadata
        #   2. registers the client dynamically
        #   3. builds PKCE auth URL → calls redirect_handler (opens browser)
        #   4. waits for callback_handler → exchanges code for tokens
        #   5. retries the original request with Bearer token
        # We don't care about the response — we just need the side-effect
        # of the tokens being stored in `storage`.
        try:
            await oauth_http.post(
                url,
                json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                      "params": {"protocolVersion": _PROTOCOL_VERSION,
                                 "capabilities": {}, "clientInfo": _CLIENT_INFO}},
                headers={"Accept": "application/json, text/event-stream"},
            )
        except Exception:  # noqa: BLE001
            # The response may be an error (e.g. session-required 400) but
            # that's fine — the token exchange has already completed.
            pass

    # After the flow, tokens should be persisted in storage
    tokens = await storage.get_tokens()
    if tokens and tokens.access_token:
        logger.info("chat.mcp.oauth_token_obtained name=%s", cfg.name)
        return tokens.access_token
    raise McpConnectionError("OAuth flow completed but no access token was obtained")


async def _build_oauth_http_client(
    cfg: McpServerConfig,
    verify: bool,
    secret_store: Any | None,
) -> httpx.AsyncClient:
    """Return an httpx.AsyncClient with OAuthClientProvider as the auth handler.

    On first call the SDK will:
    1. Dynamic-register the client with the auth server.
    2. Build the PKCE authorization URL and call redirect_handler (opens browser).
    3. Wait for callback_handler to return (code, state) from the local server.
    4. Exchange the code for tokens and persist them via McpOAuthTokenStorage.

    On subsequent calls the SDK reads the cached token from storage and uses it
    directly (or refreshes it if expired).
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    url = cfg.url or ""

    port = _allocate_oauth_redirect_port()

    redirect_uri = f"http://{LOOPBACK_HOST_NAME}:{port}{_OAUTH_CALLBACK_PATH}"

    storage = McpOAuthTokenStorage(cfg.name, secret_store) if secret_store else _NullTokenStorage()

    client_metadata = OAuthClientMetadata(
        redirect_uris=[redirect_uri],  # type: ignore[arg-type]
        client_name=_OAUTH_CLIENT_NAME,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=_OAUTH_SCOPE,
        token_endpoint_auth_method="none",  # PKCE public client
    )

    # Shared future: the callback server resolves it, callback_handler reads it.
    loop = asyncio.get_event_loop()
    cb_future: asyncio.Future = loop.create_future()  # type: ignore[type-arg]
    _start_callback_server_thread(port, cb_future, loop)

    async def redirect_handler(authorization_url: str) -> None:
        """Open the browser so the user can authenticate."""
        logger.info("chat.mcp.oauth_redirect name=%s url=%s", cfg.name, authorization_url)
        import threading as _thr
        t = _thr.Thread(
            target=_oauth_open_browser_sync,
            args=(authorization_url, cfg.name),
            daemon=True,
        )
        t.start()

    async def callback_handler() -> tuple[str, str | None]:
        """Wait for the OAuth callback and return (code, state).

        Uses asyncio.sleep polling instead of asyncio.wait_for to avoid
        creating anyio cancel scopes that conflict with the SDK task group.
        """
        deadline = asyncio.get_event_loop().time() + 300.0
        while not cb_future.done():
            if asyncio.get_event_loop().time() >= deadline:
                raise McpConnectionError(
                    "OAuth callback timed out after 300s — user did not authorise in time"
                )
            await asyncio.sleep(0.2)
        code, state = cb_future.result()
        if not code:
            raise McpConnectionError("OAuth callback received no auth code")
        return code, state

    auth_provider = OAuthClientProvider(
        server_url=url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    return httpx.AsyncClient(
        headers=dict(cfg.headers),
        timeout=httpx.Timeout(cfg.timeout_s),
        verify=verify,
        auth=auth_provider,
    )


class _NullTokenStorage:
    """No-op TokenStorage used when no SecretStore is available."""

    async def get_tokens(self) -> None:
        return None

    async def set_tokens(self, tokens: Any) -> None:
        pass

    async def get_client_info(self) -> None:
        return None

    async def set_client_info(self, client_info: Any) -> None:
        pass

