# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Contributed (extension-provided) MCP servers in ``McpServerRegistry``.

Never written to ``mcp_servers.json``; read-only to the user API except
enable / disable (whose choice IS persisted); replaced as a whole by
``set_contributed_servers``; a user's own server always wins a name clash.
Uses a real stdio MCP server subprocess (``tests/fixtures/mini_mcp_server.py``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from qai.chat.adapters import McpServerRegistry, RegistryBackedToolInvocation
from qai.chat.application.ports import McpServerReadOnlyError, ToolInvocationRequest
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.mcp_server import McpServerConfig

pytestmark = pytest.mark.unit

_SERVER = str(Path(__file__).resolve().parents[3] / "fixtures" / "mini_mcp_server.py")
SRC = "extension:demo-ext"


def _cfg(name: str = "demo-ext-mini", prefix: str = "P:") -> McpServerConfig:
    return McpServerConfig(
        name=name, command=sys.executable, args=(_SERVER,), env={"MINI_MCP_PREFIX": prefix}
    )


@pytest.fixture
def reg(tmp_path: Path):
    """Each test closes the registry itself, inside its own event loop."""
    tools = RegistryBackedToolInvocation()
    registry = McpServerRegistry(
        tools=tools, config_path=tmp_path / "mcp_servers.json", enabled=True
    )
    return registry, tools, tmp_path / "mcp_servers.json"


async def _settle(registry: McpServerRegistry) -> None:
    for _ in range(200):
        if not registry._background:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("background connect did not finish")


async def _call(tools: RegistryBackedToolInvocation, name: str, **args):
    return await tools.invoke(
        ToolInvocationRequest(
            tab_id=TabId("t"), conversation_id=ConversationId("c"), tool_name=name, arguments=args
        )
    )


async def test_contributed_server_connects_and_is_never_persisted(reg) -> None:
    registry, tools, path = reg
    try:
        await registry.add_server(
            McpServerConfig(name="mine", command="definitely-not-a-command-xyz")
        )
        refused = await registry.set_contributed_servers({SRC: [_cfg()]})
        assert refused == {}
        await _settle(registry)

        statuses = {s.config.name: s for s in await registry.list_servers()}
        assert statuses["demo-ext-mini"].connected, statuses["demo-ext-mini"].error
        assert statuses["demo-ext-mini"].source == SRC
        assert statuses["mine"].source == ""
        assert set(statuses["demo-ext-mini"].tool_names) == {"echo", "env", "cwd"}
        result = await _call(tools, "demo-ext-mini__echo", text="hi")
        assert result.ok and result.result == "P:hi"

        saved = json.loads(path.read_text("utf-8"))
        assert [s["name"] for s in saved["servers"]] == ["mine"]
        assert "demo-ext-mini" not in path.read_text("utf-8")
    finally:
        await registry.aclose()


async def test_user_api_cannot_edit_or_remove_it(reg) -> None:
    registry, _tools, _ = reg
    try:
        await registry.set_contributed_servers({SRC: [_cfg()]})
        await _settle(registry)
        with pytest.raises(McpServerReadOnlyError, match="extension:demo-ext"):
            await registry.add_server(_cfg())
        with pytest.raises(McpServerReadOnlyError):
            await registry.remove_server("demo-ext-mini")
    finally:
        await registry.aclose()


async def test_disable_is_allowed_and_remembered_across_restarts(reg) -> None:
    registry, tools, path = reg
    try:
        await registry.set_contributed_servers({SRC: [_cfg()]})
        await _settle(registry)
        status = await registry.set_enabled("demo-ext-mini", False)
        assert not status.connected and "demo-ext-mini__echo" not in tools.registered_tools
        assert json.loads(path.read_text("utf-8"))["disabled_contributed"] == ["demo-ext-mini"]
    finally:
        await registry.aclose()

    # "Restart": a new registry over the same file; the extension contributes again.
    tools2 = RegistryBackedToolInvocation()
    again = McpServerRegistry(tools=tools2, config_path=path, enabled=True)
    try:
        await again.set_contributed_servers({SRC: [_cfg()]})
        await _settle(again)
        (st,) = await again.list_servers()
        assert st.config.enabled is False and not st.connected
        assert tools2.registered_tools == ()
    finally:
        await again.aclose()


async def test_sync_replaces_changed_and_drops_removed(reg) -> None:
    registry, tools, _ = reg
    try:
        await registry.set_contributed_servers({SRC: [_cfg(prefix="v1:")]})
        await _settle(registry)
        assert (await _call(tools, "demo-ext-mini__echo", text="x")).result == "v1:x"
        pooled = registry._pool["demo-ext-mini"]

        await registry.set_contributed_servers({SRC: [_cfg(prefix="v1:")]})  # unchanged
        await _settle(registry)
        assert registry._pool["demo-ext-mini"] is pooled  # same live session

        await registry.set_contributed_servers({SRC: [_cfg(prefix="v2:")]})  # changed
        await _settle(registry)
        assert (await _call(tools, "demo-ext-mini__echo", text="x")).result == "v2:x"
        assert pooled.is_alive() is False  # old process closed

        await registry.set_contributed_servers({})  # extension gone
        assert await registry.list_servers() == ()
        assert not any(n.startswith("demo-ext-mini") for n in tools.registered_tools)
        assert registry._pool == {}
    finally:
        await registry.aclose()


async def test_user_server_wins_a_name_clash(reg) -> None:
    registry, _tools, _ = reg
    try:
        await registry.add_server(McpServerConfig(name="demo-ext-mini", command="nope-xyz"))
        refused = await registry.set_contributed_servers({SRC: [_cfg()]})
        assert "one of your own" in refused["demo-ext-mini"]
        (st,) = await registry.list_servers()
        assert st.source == "" and st.config.command == "nope-xyz"

        both = await registry.set_contributed_servers(
            {SRC: [_cfg("x-mini")], "extension:other": [_cfg("x-mini")]}
        )
        assert "already provided by extension:demo-ext" in both["x-mini"]
        await _settle(registry)
    finally:
        await registry.aclose()


async def test_global_switch_off_keeps_it_registered_but_disconnected(tmp_path: Path) -> None:
    tools = RegistryBackedToolInvocation()
    registry = McpServerRegistry(tools=tools, config_path=tmp_path / "m.json", enabled=False)
    try:
        await registry.set_contributed_servers({SRC: [_cfg()]})
        (st,) = await registry.list_servers()
        assert st.source == SRC and not st.connected and "mcp disabled" in st.error
        assert registry._background == set()
        await registry.set_global_enabled(True)  # turning MCP on connects it too
        (st,) = await registry.list_servers()
        assert st.connected, st.error
        assert not (tmp_path / "m.json").read_text("utf-8").count("demo-ext-mini")
    finally:
        await registry.aclose()
