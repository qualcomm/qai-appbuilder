# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``mcp_manage`` tool: actions, the "never flip the gate silently" rule, wiring tables."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from qai.chat.adapters.mcp_manage_tool import (
    MCP_MANAGE_TOOL_SCHEMA,
    McpManageToolHandler,
)
from qai.chat.adapters.mcp_wheel_installer import WheelInstallResult
from qai.chat.application.ports import McpServerStatus
from qai.chat.application.use_cases import tool_advertise
from qai.chat.domain.mcp_server import McpServerConfig, McpTransport


class _FakeRegistry:
    """Just enough of ``McpServerRegistryPort`` for the handler."""

    def __init__(self, *, gate: bool = False) -> None:
        self.gate = gate
        self.servers: dict[str, McpServerConfig] = {}
        self.enabled: dict[str, bool] = {}
        self.calls: list[tuple[str, Any]] = []

    def global_enabled(self) -> bool:
        return self.gate

    async def set_global_enabled(self, enabled: bool) -> None:
        self.calls.append(("set_global_enabled", enabled))
        self.gate = enabled

    def _status(self, name: str) -> McpServerStatus:
        cfg = self.servers[name]
        up = self.gate and self.enabled.get(name, True)
        return McpServerStatus(
            config=cfg,
            connected=up,
            tool_count=2 if up else 0,
            tool_names=(f"{name}__a", f"{name}__b") if up else (),
            error="" if up else ("mcp disabled" if not self.gate else ""),
        )

    async def list_servers(self) -> tuple[McpServerStatus, ...]:
        return tuple(self._status(n) for n in self.servers)

    async def add_server(self, config: McpServerConfig) -> McpServerStatus:
        self.calls.append(("add_server", config))
        self.servers[config.name] = config
        self.enabled[config.name] = True
        return self._status(config.name)

    async def remove_server(self, name: str) -> bool:
        self.calls.append(("remove_server", name))
        return self.servers.pop(name, None) is not None

    async def test_server(self, name: str) -> McpServerStatus:
        self.calls.append(("test_server", name))
        if name not in self.servers:
            raise KeyError(name)
        return self._status(name)

    async def set_enabled(self, name: str, enabled: bool) -> McpServerStatus:
        self.calls.append(("set_enabled", (name, enabled)))
        self.enabled[name] = enabled
        return self._status(name)


def _ok_install(**over: Any):
    async def fn(wheel_path: str, **kw: Any) -> WheelInstallResult:
        fn.seen = (wheel_path, kw)  # type: ignore[attr-defined]
        base = dict(
            ok=True, dist_name="demo_mcp", version="0.1.1", venv_dir="V",
            command="V/Scripts/demo-server.exe", args=(), console_scripts=("demo-server",),
        )
        base.update(over)
        return WheelInstallResult(**base)  # type: ignore[arg-type]

    return fn


def _run(handler: McpManageToolHandler, **arguments: Any) -> str:
    req = SimpleNamespace(arguments=arguments)
    return asyncio.run(handler.execute(req))  # type: ignore[arg-type]


def _handler(reg: Any = None, install: Any = None) -> McpManageToolHandler:
    return McpManageToolHandler(registry=reg, data_dir=Path("D"), install_fn=install)


# ── status / basics ─────────────────────────────────────────────────────────


def test_no_registry_degrades_gracefully() -> None:
    assert "not available" in _run(_handler(None), action="status")


def test_status_defaults_and_reports_the_gate() -> None:
    out = _run(_handler(_FakeRegistry(gate=False)))
    assert "OFF" in out and "No MCP servers" in out
    assert "ask first" in out


def test_unknown_action_lists_valid_ones() -> None:
    out = _run(_handler(_FakeRegistry()), action="explode")
    assert "unknown action" in out and "install_wheel" in out


# ── install_wheel ───────────────────────────────────────────────────────────


def test_install_wheel_registers_but_never_turns_the_gate_on() -> None:
    reg = _FakeRegistry(gate=False)
    install = _ok_install()
    out = _run(_handler(reg, install), action="install_wheel", name="demo", wheel_path="C:/x/demo.whl")
    assert "demo" in reg.servers
    cfg = reg.servers["demo"]
    assert cfg.transport is McpTransport.STDIO and cfg.command == "V/Scripts/demo-server.exe"
    # the safety property: the master switch stays as the user left it
    assert reg.gate is False
    assert not any(c[0] == "set_global_enabled" for c in reg.calls)
    assert "OFF" in out and "Do NOT turn it on without asking" in out
    assert install.seen[0] == "C:/x/demo.whl"  # type: ignore[attr-defined]


def test_install_wheel_with_gate_on_reports_connection_and_next_turn_note() -> None:
    reg = _FakeRegistry(gate=True)
    out = _run(_handler(reg, _ok_install()), action="install_wheel", name="demo", wheel_path="w.whl")
    assert "NOT connected" not in out and "connected" in out
    assert "NEXT model turn" in out
    assert "demo__a" in out


def test_install_wheel_passes_extra_args_env_and_entry() -> None:
    reg = _FakeRegistry(gate=True)
    install = _ok_install()
    _run(_handler(reg, install), action="install_wheel", name="demo", wheel_path="w.whl",
         entry="demo-server", args=["--stdio"], env={"K": "V"})
    assert install.seen[1]["entry"] == "demo-server"  # type: ignore[attr-defined]
    cfg = reg.servers["demo"]
    assert cfg.args == ("--stdio",) and cfg.env == {"K": "V"}


def test_install_wheel_failure_registers_nothing_and_says_why() -> None:
    reg = _FakeRegistry()
    install = _ok_install(ok=False, step="install_wheel", error="no matching distribution")
    out = _run(_handler(reg, install), action="install_wheel", name="demo", wheel_path="w.whl")
    assert reg.servers == {}
    assert "FAILED at step 'install_wheel'" in out and "no matching distribution" in out


def test_install_wheel_warns_when_the_entry_is_a_guess() -> None:
    reg = _FakeRegistry(gate=True)
    out = _run(_handler(reg, _ok_install(entry_is_guess=True)), action="install_wheel", name="d", wheel_path="w.whl")
    assert "guess" in out


def test_install_wheel_requires_name_and_path() -> None:
    h = _handler(_FakeRegistry(), _ok_install())
    assert "needs 'name'" in _run(h, action="install_wheel", wheel_path="w.whl")
    assert "needs 'wheel_path'" in _run(h, action="install_wheel", name="d")


def test_install_wheel_with_a_bad_server_name_is_a_readable_error_not_a_crash() -> None:
    reg = _FakeRegistry()
    out = _run(_handler(reg, _ok_install()), action="install_wheel", name="bad name!", wheel_path="w.whl")
    assert "invalid input" in out and reg.servers == {}


# ── gate / other actions ────────────────────────────────────────────────────


def test_set_global_enabled_is_the_only_way_to_flip_the_gate() -> None:
    reg = _FakeRegistry(gate=False)
    h = _handler(reg)
    assert "needs 'enabled'" in _run(h, action="set_global_enabled")
    assert reg.gate is False
    out = _run(h, action="set_global_enabled", enabled=True)
    assert reg.gate is True and "ON" in out


def test_add_by_hand_http_and_stdio() -> None:
    reg = _FakeRegistry(gate=True)
    h = _handler(reg)
    _run(h, action="add", name="remote", transport="http", url="http://x/mcp")
    _run(h, action="add", name="local", command="npx", args=["-y", "pkg"])
    assert reg.servers["remote"].transport is McpTransport.HTTP
    assert reg.servers["local"].args == ("-y", "pkg")
    assert "transport must be" in _run(h, action="add", name="z", transport="carrier-pigeon")


def test_test_enable_disable_remove_require_a_name_and_act() -> None:
    reg = _FakeRegistry(gate=True)
    h = _handler(reg)
    _run(h, action="add", name="s", command="c")
    assert "needs 'name'" in _run(h, action="test")
    assert "connected" in _run(h, action="test", name="s")
    assert "(disabled)" not in _run(h, action="enable", name="s")
    _run(h, action="disable", name="s")
    assert reg.enabled["s"] is False
    assert "Removed" in _run(h, action="remove", name="s")
    assert "No MCP server named" in _run(h, action="remove", name="s")


def test_unexpected_registry_errors_do_not_escape() -> None:
    reg = _FakeRegistry(gate=True)
    out = _run(_handler(reg), action="test", name="missing")  # fake raises KeyError
    assert "mcp_manage error" in out and "KeyError" in out


# ── schema + advertise tables ───────────────────────────────────────────────


def test_schema_shape() -> None:
    fn = MCP_MANAGE_TOOL_SCHEMA["function"]
    assert fn["name"] == "mcp_manage"
    assert set(fn["parameters"]["properties"]["action"]["enum"]) >= {
        "status", "install_wheel", "add", "test", "enable", "disable", "remove", "set_global_enabled",
    }
    assert fn["parameters"]["required"] == []


def test_mcp_manage_is_in_tool_order_and_main_agent_only() -> None:
    assert "mcp_manage" in tool_advertise.TOOL_ORDER
    assert "mcp_manage" in tool_advertise.SUB_AGENT_EXCLUDED_TOOLS
    # ordered like the other cross-session harness tools, before conditional ones
    order = tool_advertise.order_tool_names(["implementation_plan", "mcp_manage", "recall_experience"])
    assert order == ["recall_experience", "mcp_manage", "implementation_plan"]


# ── launch-args guidance (vendor READMEs say e.g. `quts-skill-mcp --stdio`) ──


def test_install_without_args_tells_the_agent_to_check_the_readme_for_flags() -> None:
    out = _run(_handler(_FakeRegistry(gate=False), _ok_install()), action="install_wheel", name="d", wheel_path="w.whl")
    assert "--stdio" in out and "README" in out


def test_install_with_args_does_not_nag() -> None:
    reg = _FakeRegistry(gate=True)
    out = _run(_handler(reg, _ok_install()), action="install_wheel", name="d", wheel_path="w.whl", args=["--stdio"])
    assert "Launch arguments:" not in out
    assert reg.servers["d"].args == ("--stdio",)


def test_not_connected_without_args_points_at_the_missing_flag() -> None:
    class _NeverConnects(_FakeRegistry):
        def _status(self, name: str) -> McpServerStatus:
            return McpServerStatus(config=self.servers[name], connected=False, error="handshake timeout")

    out = _run(_handler(_NeverConnects(gate=True), _ok_install()), action="install_wheel", name="d", wheel_path="w.whl")
    assert "handshake timeout" in out and "usual cause" in out
