# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Handler + schema for the ``mcp_manage`` tool (agent self-service MCP install).

Gives the agent deterministic actions for the whole "install an MCP" chain that
used to be a hand-rolled mix of ``exec`` + raw HTTP calls:

* ``status``             -- global gate + every server's connection state / tools
* ``install_wheel``      -- install a local ``.whl`` into a dedicated venv, register it
* ``add``                -- register a stdio / sse / http server by hand
* ``test``               -- (re)connect a registered server, report tools
* ``enable`` / ``disable`` -- the per-server switch
* ``remove``             -- unregister
* ``set_global_enabled`` -- the master ``chat_mcp_enabled`` gate

Security stance (secure-by-default is a product decision, not the tool's):

* ``install_wheel`` NEVER turns the global gate on. When it is off the result says
  so plainly; the agent must ask the user and then call ``set_global_enabled``.
* Installing a package runs third-party code. The tool validates its inputs
  (a real ``.whl`` file, a legal server name) but the *decision* to install is
  the user's -- the ``install-mcp`` skill makes the agent confirm first.
* This tool is main-agent only (see ``tool_advertise.SUB_AGENT_EXCLUDED_TOOLS``).

Best-effort like the other harness tools: errors come back as short diagnostic
text, never as an exception that breaks the turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from qai.chat.domain.mcp_server import (
    MAX_RESULT_CHARS_MAX,
    MAX_RESULT_CHARS_MIN,
    McpServerConfig,
    McpTransport,
)
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.adapters.mcp_wheel_installer import WheelInstallResult
    from qai.chat.application.ports import (
        McpServerRegistryPort,
        McpServerStatus,
        ToolInvocationRequest,
    )

__all__ = ["MCP_MANAGE_TOOL_SCHEMA", "McpManageToolHandler"]

_log = get_logger(__name__)

_ACTIONS = (
    "status",
    "install_wheel",
    "add",
    "test",
    "enable",
    "disable",
    "remove",
    "set_global_enabled",
)

#: Per-server ``timeout_s`` the tool registers with (seconds). It bounds each
#: stdio request: handshake-class requests are additionally capped at 120s by the
#: transport (a server that is alive but not speaking MCP fails fast), while every
#: ``tools/call`` may stay silent for up to this long. A wheel-installed server is
#: freshly built, cold-starts slowly on Windows and often wraps long-running work,
#: so it gets the maximum the HTTP layer accepts; a hand-registered server gets the
#: same value the marketplace uses for remote entries (the 30s domain default was
#: documented there as "too short").
_WHEEL_DEFAULT_TIMEOUT_S: float = 600.0
_ADD_DEFAULT_TIMEOUT_S: float = 120.0
_TIMEOUT_MAX_S: float = 600.0

_LAUNCH_ARGS_HINT = (
    "Launch arguments: many stdio MCP servers only speak MCP when started with a "
    "flag (very commonly `--stdio`). Check the package's README (usually next to "
    "the wheel) for the documented `command` + `args`; if it shows args, "
    "re-run install_wheel with args=[...] (e.g. args=[\"--stdio\"])."
)

_NEXT_TURN_NOTE = (
    "Note: tools of a newly connected server are advertised from the NEXT model "
    "turn, so do not try to call them in this same turn -- tell the user they "
    "are ready and continue in the next message."
)

MCP_MANAGE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mcp_manage",
        "description": (
            "Install and manage MCP (Model Context Protocol) servers. Use this "
            "whenever the user asks to install / add / enable / remove an MCP "
            "server, instead of editing config files or calling HTTP endpoints "
            "yourself. Actions: 'status' (global switch + servers), "
            "'install_wheel' (install a local .whl as a stdio MCP server in its "
            "own venv and register it), 'add' (register a stdio/sse/http server "
            "by hand), 'test' (reconnect + list tools), 'enable'/'disable' "
            "(one server), 'remove', 'set_global_enabled' (the master switch). "
            "IMPORTANT: installing a package runs third-party code and the "
            "master switch is a security setting -- ask the user (question "
            "tool) before install_wheel and before set_global_enabled."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "What to do. Defaults to 'status'.",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Server name (letters/digits/_/./-, must start with a "
                        "letter or digit). Required for every action except "
                        "'status' and 'set_global_enabled'."
                    ),
                },
                "wheel_path": {
                    "type": "string",
                    "description": (
                        "install_wheel: absolute path of the local .whl file."
                    ),
                },
                "environment": {
                    "type": "string",
                    "description": (
                        "install_wheel: which Python environment the server lives "
                        "in. Servers in the SAME environment can import each "
                        "other's packages and share one copy of common "
                        "dependencies. Omit to use the shared environment "
                        "('shared'). Give a different name to keep a server "
                        "apart -- do that when installing it fails because it "
                        "needs different versions of packages the shared "
                        "environment already holds (those versions are kept "
                        "fixed so one install cannot break another server). "
                        "Letters, digits, '_', '.', '-'."
                    ),
                },
                "entry": {
                    "type": "string",
                    "description": (
                        "install_wheel: which console script to launch when the "
                        "wheel offers several (default: the first)."
                    ),
                },
                "transport": {
                    "type": "string",
                    "enum": ["stdio", "sse", "http"],
                    "description": "add: transport (default stdio).",
                },
                "command": {
                    "type": "string",
                    "description": "add (stdio): executable to launch.",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "add (stdio) / install_wheel: launch arguments for the "
                        "server. Many stdio MCP servers REQUIRE a flag such as "
                        "[\"--stdio\"] -- take it from the package README."
                    ),
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "add (stdio) / install_wheel: extra env vars.",
                },
                "url": {
                    "type": "string",
                    "description": "add (sse/http): endpoint URL.",
                },
                "timeout_s": {
                    "type": "number",
                    "description": (
                        "install_wheel / add: seconds a server may stay silent "
                        "during one request (max 600). install_wheel defaults to "
                        "600, add to 120. Connecting/listing tools never waits "
                        "longer than 120s regardless. Raise it only if the "
                        "package README says its tools run very long."
                    ),
                },
                "max_result_chars": {
                    "type": "integer",
                    "description": (
                        "install_wheel / add: cap, in characters, on ONE tool "
                        "result shown to the model for this server (10000-2000000). "
                        "Unset keeps the normal model budget (30K-50K); set it for "
                        "a server whose tools return whole documents so they are "
                        "not cut in the middle. 0 clears it. Omit to keep the "
                        "current value."
                    ),
                },
                "enabled": {
                    "type": "boolean",
                    "description": "set_global_enabled: the new master-switch state.",
                },
            },
            "required": [],
        },
    },
}


def _fmt_status(st: McpServerStatus) -> str:
    cfg = st.config
    state = "connected" if st.connected else "NOT connected"
    where = (
        f"{cfg.command} {' '.join(cfg.args)}".strip()
        if cfg.transport is McpTransport.STDIO
        else (cfg.url or "")
    )
    line = (
        f"- {cfg.name} [{cfg.transport.value}] {state}"
        f"{'' if cfg.enabled else ' (disabled)'}"
        f" | tools: {st.tool_count} | timeout {cfg.timeout_s:g}s"
        f"{f' | result cap {cfg.max_result_chars}' if cfg.max_result_chars else ''}"
        f" | {where}"
    )
    if st.tool_names:
        line += "\n    tools: " + ", ".join(st.tool_names[:20])
        if len(st.tool_names) > 20:
            line += f", ... (+{len(st.tool_names) - 20} more)"
    if st.error:
        line += f"\n    error: {st.error}"
    return line


def _resolve_timeout(value: Any, default: float) -> float:
    """``timeout_s`` from tool args: junk / non-positive -> default, capped at 600."""
    if isinstance(value, bool):
        return default
    try:
        t = float(value)
    except (TypeError, ValueError):
        return default
    if t != t or t <= 0:  # NaN or non-positive
        return default
    return min(t, _TIMEOUT_MAX_S)


def _resolve_result_cap(value: Any) -> int | None:
    """``max_result_chars`` from tool args.

    ``None`` = not provided / junk (the registry then keeps any existing value);
    ``0`` = explicitly clear; otherwise clamped into the allowed range.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if n == 0:
        return 0
    if n < 0:
        return None
    return max(MAX_RESULT_CHARS_MIN, min(n, MAX_RESULT_CHARS_MAX))


def _as_str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return ()


class McpManageToolHandler:
    """Handler for the ``mcp_manage`` tool."""

    __slots__ = ("_registry", "_data_dir", "_install")

    def __init__(
        self,
        *,
        registry: McpServerRegistryPort | None,
        data_dir: Path,
        install_fn: Any = None,
    ) -> None:
        self._registry = registry
        self._data_dir = Path(data_dir)
        # Injectable for tests; defaults to the real wheel installer.
        self._install = install_fn

    async def execute(self, request: ToolInvocationRequest) -> str:
        reg = self._registry
        if reg is None:
            return "[mcp_manage] MCP management is not available in this environment."
        args = request.arguments if isinstance(request.arguments, dict) else {}
        action = args.get("action") or "status"
        try:
            if action == "status":
                return await self._status(reg)
            if action == "set_global_enabled":
                return await self._set_global(reg, args)
            if action == "install_wheel":
                return await self._install_wheel(reg, args)
            if action == "add":
                return await self._add(reg, args)
            if action in ("test", "enable", "disable", "remove"):
                return await self._by_name(reg, action, args)
            return (
                f"[mcp_manage] unknown action {action!r}. Valid actions: "
                + ", ".join(_ACTIONS)
                + "."
            )
        except ValueError as exc:  # config validation (McpServerConfig)
            return f"[mcp_manage] invalid input for {action}: {exc}"
        except Exception as exc:  # noqa: BLE001 -- never break the turn
            _log.warning(
                "chat.mcp_manage.failed",
                action=str(action),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return (
                f"[mcp_manage error] the {action} action failed "
                f"({type(exc).__name__}: {exc})."
            )

    # ---- actions -----------------------------------------------------

    async def _status(self, reg: McpServerRegistryPort) -> str:
        gate = reg.global_enabled()
        servers = await reg.list_servers()
        head = (
            f"MCP master switch (chat_mcp_enabled): {'ON' if gate else 'OFF'}."
            + (
                ""
                if gate
                else " While OFF, servers can be registered but are NOT started "
                "and expose no tools. Turning it on is the user's decision -- ask first."
            )
        )
        if not servers:
            return head + "\nNo MCP servers are registered."
        return head + f"\n{len(servers)} server(s):\n" + "\n".join(
            _fmt_status(s) for s in servers
        )

    async def _set_global(
        self, reg: McpServerRegistryPort, args: dict[str, Any]
    ) -> str:
        enabled = args.get("enabled")
        if not isinstance(enabled, bool):
            return "[mcp_manage] set_global_enabled needs 'enabled' (true/false)."
        await reg.set_global_enabled(enabled)
        servers = await reg.list_servers()
        connected = sum(1 for s in servers if s.connected)
        return (
            f"MCP master switch is now {'ON' if enabled else 'OFF'}. "
            f"{connected}/{len(servers)} registered server(s) connected."
            + (f"\n{_NEXT_TURN_NOTE}" if enabled and connected else "")
        )

    async def _install_wheel(
        self, reg: McpServerRegistryPort, args: dict[str, Any]
    ) -> str:
        name = args.get("name")
        wheel_path = args.get("wheel_path")
        if not isinstance(name, str) or not name.strip():
            return "[mcp_manage] install_wheel needs 'name' (the server name)."
        if not isinstance(wheel_path, str) or not wheel_path.strip():
            return "[mcp_manage] install_wheel needs 'wheel_path' (absolute .whl path)."
        install = self._install
        if install is None:
            from qai.chat.adapters.mcp_wheel_installer import install_wheel_as_mcp

            install = install_wheel_as_mcp
        env_raw = args.get("environment")
        if env_raw is not None and not (isinstance(env_raw, str) and env_raw.strip()):
            return "[mcp_manage] environment must be a non-empty name (letters, digits, '_', '.', '-')."
        res: WheelInstallResult = await install(
            wheel_path.strip(),
            server_name=name.strip(),
            data_dir=self._data_dir,
            entry=args.get("entry") if isinstance(args.get("entry"), str) else None,
            environment=env_raw.strip() if isinstance(env_raw, str) else None,
        )
        if not res.ok:
            return (
                f"[mcp_manage] install_wheel FAILED at step '{res.step}': {res.error}\n"
                "Nothing was registered. Fix the cause and retry, or tell the user."
            )
        extra_args = _as_str_tuple(args.get("args"))
        cfg = McpServerConfig(
            name=name.strip(),
            transport=McpTransport.STDIO,
            command=res.command,
            args=tuple(res.args) + extra_args,
            env=_as_str_dict(args.get("env")),
            timeout_s=_resolve_timeout(args.get("timeout_s"), _WHEEL_DEFAULT_TIMEOUT_S),
            max_result_chars=_resolve_result_cap(args.get("max_result_chars")),
        )
        st = await reg.add_server(cfg)
        gate = reg.global_enabled()
        lines = [
            f"Installed {res.dist_name} {res.version} into {res.venv_dir}",
            *(
                [
                    f"Environment: {res.environment}"
                    + (
                        " (also holds: "
                        + ", ".join(f"{d} {v}" for d, v in res.env_wheels)
                        + ")"
                        if res.env_wheels
                        else ""
                    )
                ]
                if res.environment
                else []
            ),
            *(
                [
                    "Note: this server kept its existing per-server environment. "
                    "To move it into the shared one, install it again with "
                    "environment='shared'."
                ]
                if res.env_is_legacy
                else []
            ),
            f"Registered MCP server '{cfg.name}': {cfg.command} {' '.join(cfg.args)}".rstrip(),
            f"Per-request timeout: {cfg.timeout_s:g}s (re-run install_wheel with "
            "timeout_s=... to change it).",
        ]
        if res.entry_is_guess:
            lines.append(
                "WARNING: the launch command is a guess (see notes); verify it "
                "with action='test'."
            )
        if len(res.console_scripts) > 1:
            lines.append(
                "The wheel offers several console scripts: "
                + ", ".join(res.console_scripts)
                + ". Using the first unless 'entry' was given."
            )
        lines.extend(f"note: {n}" for n in res.notes)
        if not extra_args:
            lines.append(_LAUNCH_ARGS_HINT)
        if not gate:
            lines.append(
                "The MCP master switch is OFF, so the server was saved but NOT "
                "started. Ask the user whether to turn it on; if they agree call "
                "mcp_manage(action='set_global_enabled', enabled=true). Do NOT "
                "turn it on without asking."
            )
        else:
            lines.append(_fmt_status(st))
            if st.connected:
                lines.append(_NEXT_TURN_NOTE)
            elif not extra_args:
                lines.append(
                    "It did not connect without launch arguments -- this is the "
                    "usual cause; retry install_wheel with the args from the README."
                )
        return "\n".join(lines)

    async def _add(self, reg: McpServerRegistryPort, args: dict[str, Any]) -> str:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return "[mcp_manage] add needs 'name'."
        raw_transport = args.get("transport") or "stdio"
        try:
            transport = McpTransport(raw_transport)
        except ValueError:
            return "[mcp_manage] transport must be one of: stdio, sse, http."
        cfg = McpServerConfig(
            name=name.strip(),
            transport=transport,
            command=args.get("command") if isinstance(args.get("command"), str) else None,
            args=_as_str_tuple(args.get("args")),
            env=_as_str_dict(args.get("env")),
            url=args.get("url") if isinstance(args.get("url"), str) else None,
            timeout_s=_resolve_timeout(args.get("timeout_s"), _ADD_DEFAULT_TIMEOUT_S),
            max_result_chars=_resolve_result_cap(args.get("max_result_chars")),
        )
        st = await reg.add_server(cfg)
        out = ["Registered MCP server:", _fmt_status(st)]
        if not reg.global_enabled():
            out.append(
                "The MCP master switch is OFF, so it was saved but NOT started "
                "(ask the user before turning it on)."
            )
        elif st.connected:
            out.append(_NEXT_TURN_NOTE)
        return "\n".join(out)

    async def _by_name(
        self, reg: McpServerRegistryPort, action: str, args: dict[str, Any]
    ) -> str:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return f"[mcp_manage] {action} needs 'name'."
        name = name.strip()
        if action == "remove":
            removed = await reg.remove_server(name)
            return (
                f"Removed MCP server '{name}'."
                if removed
                else f"No MCP server named '{name}' is registered."
            )
        if action == "test":
            st = await reg.test_server(name)
        else:
            st = await reg.set_enabled(name, action == "enable")
        out = [_fmt_status(st)]
        if not reg.global_enabled():
            out.append(
                "The MCP master switch is OFF, so nothing connects until the user "
                "turns it on."
            )
        elif st.connected:
            out.append(_NEXT_TURN_NOTE)
        return "\n".join(out)
