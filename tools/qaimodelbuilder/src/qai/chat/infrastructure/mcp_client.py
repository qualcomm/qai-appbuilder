# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Minimal JSON-RPC 2.0 transports for MCP (Model Context Protocol) servers.

This module implements the *transport* half of the chat context's MCP
integration: connecting to an MCP server, running the ``initialize`` handshake,
listing / calling its tools (``tools/list`` / ``tools/call``), and — for the
resources/prompts surface — listing + reading resources (``resources/list`` /
``resources/read``) and listing + rendering prompts (``prompts/list`` /
``prompts/get``).  It deliberately implements a **minimal JSON-RPC 2.0 client**
over the stdlib (``asyncio.subprocess`` for ``stdio``) + the
already-core-dependency ``httpx`` (for ``sse`` / streamable-``http``) rather
than pulling in the optional ``mcp`` SDK, so that:

* no new runtime dependency is added (AGENTS.md 🟠 cross-platform constraint —
  external / Linux installs must not be forced to pull a heavy MCP SDK);
* the transports stay cross-platform (``asyncio.subprocess`` + ``httpx``);
* domain-purity is preserved (this is an ``infrastructure``-layer module; the
  ``httpx`` import is legitimate here, never in ``domain``).

The higher-level registry that owns config persistence + tool registration onto
the shared chat tool port lives in :mod:`qai.chat.adapters.mcp_client`.

Cross-context isolation
-----------------------
Imports only ``qai.chat.domain`` + stdlib + ``httpx``.  No imports of other
bounded contexts (``context-isolation`` contract).
"""

from __future__ import annotations

import asyncio
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
    ) -> None:
        self._config = config
        self._ssl_verify = ssl_verify
        # Live Settings.ssl_verify provider (apps/api._global_proxy
        # .build_ssl_verify_provider). Read at client-build time (in ``_connect``)
        # so a runtime SSL toggle hot-applies to NEW MCP remote connections. NOTE:
        # an already-open pooled MCP client keeps its old verify until it is
        # reconnected — acceptable (the toggle applies on next connect). Frozen
        # ``ssl_verify`` bool is the back-compat fallback.
        self._ssl_verify_provider = ssl_verify_provider
        self._proc: asyncio.subprocess.Process | None = None
        self._http: httpx.AsyncClient | None = None
        self._rpc_id = 0
        self._lock = asyncio.Lock()

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
        """
        await self._connect()

    def is_alive(self) -> bool:
        """True iff this client's underlying transport is still usable.

        State-Truth-First: this is a DIRECT probe of the real resource, not an
        optimistic flag — for stdio it checks the spawned child is still running
        (``returncode is None``); for a remote client it checks the HTTP client
        is open. A pooled client that fails this is dead and must be re-spawned.
        """
        if self._proc is not None:
            return self._proc.returncode is None
        if self._http is not None:
            return not self._http.is_closed
        return False

    # ---- lifecycle -------------------------------------------------------

    async def _connect(self) -> None:
        cfg = self._config
        if cfg.transport is McpTransport.STDIO:
            # Timing diagnostics: spawning npx (esp. first-launch package
            # download through the corporate MITM CA) can be slow; logging the
            # wall-clock elapsed at INFO makes "connected but took N s" vs
            # "stuck spawning" visible in the backend log.
            import time

            t0 = time.monotonic()
            await self._spawn_stdio()
            await self._initialize()
            elapsed = time.monotonic() - t0
            logger.info(
                "chat.mcp.stdio_connected name=%s cmd=%s elapsed_s=%.1f",
                cfg.name,
                cfg.command,
                elapsed,
            )
            return
        else:
            # Outbound TLS verification for MCP remote (sse/http) servers
            # follows the unified ``Settings.ssl_verify`` switch (edition-derived
            # default), consistent with the LLM gateway client and every other
            # outbound client in this project. When it is False (internal edition
            # / enterprise TLS-intercepting environment) the corporate MITM CA no
            # longer trips UnknownIssuer / CERTIFICATE_VERIFY_FAILED.
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(cfg.timeout_s),
                headers=dict(cfg.headers),
                # Live read so a runtime Settings.ssl_verify toggle hot-applies to
                # this NEW connection (already-open pooled clients keep their old
                # verify until reconnect); frozen bool fallback.
                verify=(
                    self._ssl_verify_provider()
                    if self._ssl_verify_provider is not None
                    else self._ssl_verify
                ),
            )
        await self._initialize()

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
        """Terminate the subprocess / close the HTTP client.  Idempotent."""
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            # Drain any pending stderr so a child that wrote to a full stderr
            # PIPE buffer cannot deadlock on terminate/wait. Best-effort +
            # bounded — this is teardown, never the hot path.
            stderr = getattr(proc, "stderr", None)
            if stderr is not None:
                try:
                    await asyncio.wait_for(stderr.read(), timeout=1.0)
                except (TimeoutError, Exception):  # noqa: BLE001
                    pass
            try:
                proc.terminate()
            except ProcessLookupError:  # pragma: no cover
                pass
            except Exception:  # noqa: BLE001
                pass
            else:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except (TimeoutError, Exception):  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
        if self._http is not None:
            http = self._http
            self._http = None
            try:
                await http.aclose()
            except Exception:  # noqa: BLE001
                pass

    # ---- JSON-RPC --------------------------------------------------------

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def _rpc(
        self, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send one JSON-RPC request and return its ``result`` (or raise)."""
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        if self._proc is not None:
            return await self._rpc_stdio(request)
        if self._http is not None:
            return await self._rpc_http(request)
        raise McpConnectionError("not connected")

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        note = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if self._proc is not None and self._proc.stdin is not None:
            line = (json.dumps(note) + "\n").encode("utf-8")
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()
        elif self._http is not None:
            try:
                await self._http.post(
                    self._config.url or "", json=note
                )
            except Exception:  # noqa: BLE001 — notifications are best-effort
                pass

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

    async def _rpc_http(self, request: dict[str, Any]) -> Any:
        http = self._http
        if http is None:
            raise McpConnectionError("http client unavailable")
        url = self._config.url or ""
        try:
            resp = await http.post(
                url,
                json=request,
                headers={"Accept": "application/json, text/event-stream"},
            )
        except (httpx.HTTPError, OSError) as exc:
            raise McpConnectionError(f"http_error: {exc}") from exc
        if resp.status_code >= 400:
            raise McpConnectionError(f"http_status_{resp.status_code}")
        ctype = resp.headers.get("content-type", "")
        body = resp.text
        if "text/event-stream" in ctype:
            msg = _first_sse_json(body)
        else:
            try:
                msg = json.loads(body) if body.strip() else {}
            except (ValueError, TypeError) as exc:
                raise McpConnectionError(f"bad_json: {exc}") from exc
        if not isinstance(msg, dict):
            raise McpConnectionError("unexpected response shape")
        return _unwrap_rpc_result(msg)

    # ---- MCP methods -----------------------------------------------------

    async def _initialize(self) -> None:
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
        # Per spec, notify the server that initialization is complete.
        await self._notify("notifications/initialized")

    async def list_tools(self) -> tuple[McpTool, ...]:
        """Return the tools the server advertises via ``tools/list``."""
        result = await self._rpc("tools/list", {})
        raw_tools = []
        if isinstance(result, dict):
            raw_tools = result.get("tools") or []
        out: list[McpTool] = []
        for entry in raw_tools:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            schema = entry.get("inputSchema")
            out.append(
                McpTool(
                    server_name=self._config.name,
                    name=name,
                    description=str(entry.get("description") or ""),
                    schema=dict(schema) if isinstance(schema, dict) else {},
                )
            )
        return tuple(out)

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        """Call ``tool_name`` and return its consolidated textual result.

        MCP ``tools/call`` returns ``{"content": [{type, text|...}], "isError":
        bool}``.  We concatenate the text of every ``text`` content block; a
        non-text block is rendered as its JSON.  An ``isError`` result is
        prefixed with ``[mcp error]`` so the model can react.
        """
        result = await self._rpc(
            "tools/call", {"name": tool_name, "arguments": arguments or {}}
        )
        return _render_tool_result(result)

    async def list_resources(self) -> tuple[McpResource, ...]:
        """Return the resources the server advertises via ``resources/list``."""
        result = await self._rpc("resources/list", {})
        raw = result.get("resources") or [] if isinstance(result, dict) else []
        out: list[McpResource] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            uri = entry.get("uri")
            if not isinstance(uri, str) or not uri:
                continue
            out.append(
                McpResource(
                    server_name=self._config.name,
                    uri=uri,
                    name=str(entry.get("name") or ""),
                    mime_type=str(entry.get("mimeType") or ""),
                )
            )
        return tuple(out)

    async def read_resource(self, uri: str) -> str:
        """Read one resource by ``uri`` via ``resources/read``; return its text.

        MCP ``resources/read`` returns ``{"contents": [{uri, mimeType,
        text|blob}]}``.  Text content blocks are concatenated; a binary
        (``blob``) block is rendered as a short ``[binary <mime> <n> bytes]``
        placeholder (the LLM cannot consume raw base64 usefully).
        """
        result = await self._rpc("resources/read", {"uri": uri})
        return _render_resource_read(result)

    async def list_prompts(self) -> tuple[McpPrompt, ...]:
        """Return the prompts the server advertises via ``prompts/list``."""
        result = await self._rpc("prompts/list", {})
        raw = result.get("prompts") or [] if isinstance(result, dict) else []
        out: list[McpPrompt] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            args_raw = entry.get("arguments") or []
            args: list[McpPromptArgument] = []
            if isinstance(args_raw, list):
                for a in args_raw:
                    if not isinstance(a, dict):
                        continue
                    aname = a.get("name")
                    if not isinstance(aname, str) or not aname:
                        continue
                    args.append(
                        McpPromptArgument(
                            name=aname,
                            description=str(a.get("description") or ""),
                            required=bool(a.get("required", False)),
                        )
                    )
            out.append(
                McpPrompt(
                    server_name=self._config.name,
                    name=name,
                    description=str(entry.get("description") or ""),
                    arguments=tuple(args),
                )
            )
        return tuple(out)

    async def get_prompt(
        self, name: str, arguments: dict[str, Any]
    ) -> str:
        """Render a named prompt via ``prompts/get``; return its text.

        MCP ``prompts/get`` returns ``{"description"?, "messages": [{role,
        content: {type, text|...}}]}``.  We render each message as
        ``<role>: <text>`` so the model sees the assembled template.
        """
        result = await self._rpc(
            "prompts/get", {"name": name, "arguments": arguments or {}}
        )
        return _render_prompt_get(result)


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
