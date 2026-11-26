# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""User extensions wired into the REAL chat tool registry (stage 1 vertical slice).

Covers: an extension under ``<data>/extensions`` becomes an advertised,
dispatchable chat tool; a built-in name always wins; the tool joins its
declared persona group; the tool reaches ``compose_advertised_tools``
like any built-in; the explore sub-agent never sees it.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import apps.api._user_extensions_di as di
from apps.api._user_extensions_di import extension_skill_roots, wire_user_extensions_into_chat
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest
from qai.chat.application.use_cases.tool_advertise import compose_advertised_tools
from qai.chat.domain import tool_groups
from qai.chat.domain.agent_profile import EXPLORE
from qai.chat.domain.ids import ConversationId, TabId
from qai.platform.persistence.secrets import NullSecretStore
from qai.platform.skills import SkillDiscovery
from qai.platform.user_extensions import module_name_for

_CODE = """
def activate(ctx):
    prefix = ctx.config.get("prefix", "?")

    async def _echo(call):
        return prefix + ":" + call.arguments.get("text", "")

    for name in ("demo_echo", "demo_run", "read"):
        ctx.register_tool(
            name,
            _echo,
            description="demo",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        )
"""


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "b", "parameters": {}}}


@pytest.fixture
def data_root(tmp_path: Path):
    ext = tmp_path / "extensions" / "demo-ext"
    ext.mkdir(parents=True)
    (ext / "extension.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "name": "demo-ext",
                "version": "0.1.0",
                "description": "demo",
                "entry": "main.py",
                "tools": [
                    {"name": "demo_echo", "group": "read"},
                    {"name": "demo_run", "group": "command"},
                    {"name": "read", "group": "read"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (ext / "main.py").write_text(textwrap.dedent(_CODE), encoding="utf-8")
    (ext / "config.toml").write_text('prefix = "demo"\n', encoding="utf-8")
    snapshot = dict(tool_groups.TOOL_GROUP_MEMBERS)
    yield tmp_path
    tool_groups.TOOL_GROUP_MEMBERS.clear()
    tool_groups.TOOL_GROUP_MEMBERS.update(snapshot)
    sys.modules.pop(module_name_for("demo-ext"), None)


@pytest.fixture
def wired(data_root: Path):
    tools = RegistryBackedToolInvocation()
    builtin_read = object()

    async def _builtin_read(_request):
        return builtin_read

    tools.register("read", _builtin_read, schema=_schema("read"))
    chat = SimpleNamespace(tools=tools)
    container = SimpleNamespace(data_paths=SimpleNamespace(root=data_root), user_extensions=None)
    host = wire_user_extensions_into_chat(chat=chat, container=container)
    return SimpleNamespace(tools=tools, host=host, container=container, builtin_read=builtin_read)


def _request(tool: str, **arguments) -> ToolInvocationRequest:
    return ToolInvocationRequest(
        tab_id=TabId("tab-1"),
        conversation_id=ConversationId("conv-1"),
        tool_name=tool,
        arguments=arguments,
    )


async def test_extension_tool_is_dispatchable(wired) -> None:
    result = await wired.tools.invoke(_request("demo_echo", text="hello"))
    assert result.ok is True
    assert result.result == "demo:hello"


def test_extension_tool_is_advertised(wired) -> None:
    names = {s["function"]["name"] for s in wired.tools.schemas()}
    assert {"demo_echo", "demo_run", "read"} <= names


async def test_builtin_name_wins(wired) -> None:
    (status,) = wired.host.statuses
    assert status.state == "loaded"
    assert status.tools == ("demo_echo", "demo_run")
    assert any("'read' refused" in w for w in status.warnings)
    result = await wired.tools.invoke(_request("read"))
    assert result.result is wired.builtin_read


def test_host_stashed_on_container(wired) -> None:
    assert wired.container.user_extensions is wired.host


def test_tool_joins_declared_persona_group(wired) -> None:
    assert "demo_echo" in tool_groups.TOOL_GROUP_MEMBERS["read"]
    assert "demo_run" in tool_groups.TOOL_GROUP_MEMBERS["command"]
    disabled = tool_groups.groups_to_disabled_tools(["read"])
    assert "demo_run" in disabled
    assert "demo_echo" not in disabled


def test_main_agent_advertises_extension_tools(wired) -> None:
    advertised = compose_advertised_tools(
        wired.tools.schemas(),
        tool_mode=None,
        is_local=False,
        excluded=frozenset(),
        inject_agent=False,
        agent_schema_factory=None,
    )
    names = {s["function"]["name"] for s in advertised}
    assert {"demo_echo", "demo_run"} <= names


def test_session_disabled_tools_drop_extension_tool(wired) -> None:
    advertised = compose_advertised_tools(
        wired.tools.schemas(),
        tool_mode=None,
        is_local=False,
        excluded=frozenset(),
        inject_agent=False,
        agent_schema_factory=None,
        disabled_tools=frozenset({"demo_run"}),
    )
    names = {s["function"]["name"] for s in advertised}
    assert "demo_run" not in names
    assert "demo_echo" in names


def test_explore_subagent_never_sees_extension_tools(wired) -> None:
    advertised = compose_advertised_tools(
        wired.tools.schemas(),
        tool_mode=None,
        is_local=False,
        excluded=frozenset(),
        inject_agent=False,
        agent_schema_factory=None,
        profile=EXPLORE,
    )
    names = {s["function"]["name"] for s in advertised}
    assert not names & {"demo_echo", "demo_run"}
    assert "read" in names


def test_no_extensions_dir_is_a_noop(tmp_path: Path) -> None:
    tools = RegistryBackedToolInvocation()
    container = SimpleNamespace(data_paths=SimpleNamespace(root=tmp_path), user_extensions=None)
    host = wire_user_extensions_into_chat(chat=SimpleNamespace(tools=tools), container=container)
    assert host is not None
    assert host.statuses == ()
    assert tools.registered_tools == ()


async def test_app_services_reach_the_context(data_root: Path) -> None:
    """The secret store and the live proxy / TLS settings come from the container."""
    (data_root / "extensions" / "demo-ext" / "main.py").write_text(
        textwrap.dedent(
            """
            def activate(ctx):
                async def _probe(call):
                    ctx.secrets.set("k", "v")
                    secret = ctx.secrets.get("k")
                    return (ctx.proxy_url(), ctx.ssl_verify, secret, str(ctx.data_dir))
                ctx.register_tool("demo_echo", _probe, description="probe")
            """
        ),
        encoding="utf-8",
    )
    store = NullSecretStore()
    settings = SimpleNamespace(
        tools=SimpleNamespace(global_proxy="http://proxy:8080"), ssl_verify=False
    )
    container = SimpleNamespace(
        data_paths=SimpleNamespace(root=data_root),
        user_extensions=None,
        secret_store=store,
        settings=settings,
    )
    tools = RegistryBackedToolInvocation()
    host = wire_user_extensions_into_chat(chat=SimpleNamespace(tools=tools), container=container)
    assert host is not None and host.statuses[0].state == "loaded", host.statuses[0].error
    result = await tools.invoke(_request("demo_echo"))
    proxy, ssl, secret, data_dir = result.result
    assert proxy == "http://proxy:8080"
    assert ssl is False
    assert secret == "v" and store.get("ext.demo-ext", "k") == "v"
    assert Path(data_dir) == data_root / "extensions-data" / "demo-ext"


def test_a_policy_failure_fails_that_extension_and_leaves_no_tool(
    data_root: Path, monkeypatch
) -> None:
    """If the permission group cannot be joined, the tool must not be registered at all."""

    def _boom(*_args):
        raise RuntimeError("policy failed")

    monkeypatch.setattr(di, "add_group_members", _boom)
    tools = RegistryBackedToolInvocation()
    container = SimpleNamespace(data_paths=SimpleNamespace(root=data_root), user_extensions=None)
    manager = wire_user_extensions_into_chat(chat=SimpleNamespace(tools=tools), container=container)
    assert manager is not None
    (status,) = manager.statuses
    assert status.state == "failed" and "policy failed" in status.error
    assert tools.registered_tools == ()


def test_skill_added_after_load_is_not_discovered(data_root: Path) -> None:
    """Only the skills vetted at load reach the scan (allow-list)."""
    skills = data_root / "extensions" / "demo-ext" / "skills"
    for sid in ("vetted",):
        (skills / sid).mkdir(parents=True)
        (skills / sid / "SKILL.md").write_text(
            f"---\nname: {sid}\ndescription: d\n---\n", encoding="utf-8"
        )
    tools = RegistryBackedToolInvocation()
    container = SimpleNamespace(data_paths=SimpleNamespace(root=data_root), user_extensions=None)
    wire_user_extensions_into_chat(chat=SimpleNamespace(tools=tools), container=container)
    (skills / "late").mkdir()
    (skills / "late" / "SKILL.md").write_text(
        "---\nname: late\ndescription: d\n---\n", encoding="utf-8"
    )
    disc = SkillDiscovery([], contributed_roots=lambda: extension_skill_roots(container))
    assert {s.skill_id for s in disc.scan()} == {"vetted"}


def test_non_registry_tools_port_is_skipped(tmp_path: Path) -> None:
    container = SimpleNamespace(data_paths=SimpleNamespace(root=tmp_path), user_extensions=None)
    chat = SimpleNamespace(tools=object())
    assert wire_user_extensions_into_chat(chat=chat, container=container) is None
    assert container.user_extensions is None
