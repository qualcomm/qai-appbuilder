# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``mcp_manage``: per-server ``timeout_s`` (wheel installs 600s, hand-added 120s)."""

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
from qai.chat.domain.mcp_server import McpServerConfig


class _Registry:
    """Minimal ``McpServerRegistryPort`` stand-in that records the configs it is given."""

    def __init__(self, *, gate: bool = True) -> None:
        self.gate = gate
        self.servers: dict[str, McpServerConfig] = {}

    def global_enabled(self) -> bool:
        return self.gate

    async def add_server(self, config: McpServerConfig) -> McpServerStatus:
        self.servers[config.name] = config
        return McpServerStatus(config=config, connected=self.gate, tool_count=1, tool_names=("t",))

    async def list_servers(self) -> tuple[McpServerStatus, ...]:
        return tuple(McpServerStatus(config=c, connected=True) for c in self.servers.values())


async def _fake_install(wheel_path: str, **_: Any) -> WheelInstallResult:
    return WheelInstallResult(
        ok=True, dist_name="demo", version="1", venv_dir="V", command="V/demo.exe", args=(),
    )


def _run(reg: _Registry, **arguments: Any) -> str:
    h = McpManageToolHandler(registry=reg, data_dir=Path("D"), install_fn=_fake_install)  # type: ignore[arg-type]
    return asyncio.run(h.execute(SimpleNamespace(arguments=arguments)))  # type: ignore[arg-type]


def test_install_wheel_defaults_to_the_maximum_timeout() -> None:
    reg = _Registry()
    out = _run(reg, action="install_wheel", name="d", wheel_path="w.whl")
    assert reg.servers["d"].timeout_s == 600.0
    assert "timeout 600s" in out and "Per-request timeout: 600s" in out


def test_install_wheel_honours_an_explicit_timeout_and_caps_it() -> None:
    reg = _Registry()
    _run(reg, action="install_wheel", name="a", wheel_path="w.whl", timeout_s=240)
    _run(reg, action="install_wheel", name="b", wheel_path="w.whl", timeout_s=99999)
    assert reg.servers["a"].timeout_s == 240.0
    assert reg.servers["b"].timeout_s == 600.0  # never above what the HTTP layer allows


def test_junk_timeouts_fall_back_to_the_default() -> None:
    reg = _Registry()
    for i, bad in enumerate([0, -5, "soon", None, True, float("nan")]):
        _run(reg, action="install_wheel", name=f"s{i}", wheel_path="w.whl", timeout_s=bad)
        assert reg.servers[f"s{i}"].timeout_s == 600.0, bad


def test_manual_add_defaults_to_120s_and_accepts_an_override() -> None:
    reg = _Registry()
    _run(reg, action="add", name="x", command="c")
    _run(reg, action="add", name="y", command="c", timeout_s=45)
    assert reg.servers["x"].timeout_s == 120.0
    assert reg.servers["y"].timeout_s == 45.0


def test_status_shows_each_servers_timeout() -> None:
    reg = _Registry()
    _run(reg, action="add", name="x", command="c", timeout_s=75)
    assert "timeout 75s" in _run(reg, action="status")


def test_schema_documents_timeout_s() -> None:
    props = MCP_MANAGE_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert props["timeout_s"]["type"] == "number"
    assert "600" in props["timeout_s"]["description"]
