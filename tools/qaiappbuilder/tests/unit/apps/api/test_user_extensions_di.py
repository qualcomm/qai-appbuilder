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

from apps.api._user_extensions_di import wire_user_extensions_into_chat
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest
from qai.chat.application.use_cases.tool_advertise import compose_advertised_tools
from qai.chat.domain import tool_groups
from qai.chat.domain.agent_profile import EXPLORE
from qai.chat.domain.ids import ConversationId, TabId

_CODE = '''
def build_services(ctx):
    ctx.register_mode("demo-ext")  # degraded: warning only
    return {"prefix": "demo"}

def wire_into_chat(ctx, services):
    async def _echo(request):
        return services["prefix"] + ":" + request.arguments.get("text", "")

    for name in ("demo_echo", "demo_run", "read"):
        ctx.register_chat_tool(
            name,
            _echo,
            schema={"type": "function", "function": {
                "name": name, "description": "demo",
                "parameters": {"type": "object",
                               "properties": {"text": {"type": "string"}}}}},
        )
'''


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "b", "parameters": {}}}


@pytest.fixture
def data_root(tmp_path: Path):
    ext = tmp_path / "extensions" / "demo-ext"
    pkg = ext / "python" / "qai_ext_demo_ext"
    pkg.mkdir(parents=True)
    (ext / "extension.json").write_text(
        json.dumps(
            {
                "name": "demo-ext",
                "version": "0.1.0",
                "api_version": 2,
                "description": "demo",
                "python_package": "qai_ext_demo_ext",
                "contributes": {
                    "modes": [{"id": "demo-ext"}],
                    "tools": [
                        {"name": "demo_echo", "group": "read", "result_category": "query"},
                        {
                            "name": "demo_run",
                            "group": "command",
                            "result_category": "command",
                            "mutating": True,
                        },
                        {"name": "read", "group": "read", "result_category": "read"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text(textwrap.dedent(_CODE), encoding="utf-8")
    snapshot = dict(tool_groups.TOOL_GROUP_MEMBERS)
    yield tmp_path
    tool_groups.TOOL_GROUP_MEMBERS.clear()
    tool_groups.TOOL_GROUP_MEMBERS.update(snapshot)
    sys.modules.pop("qai_ext_demo_ext", None)


@pytest.fixture
def wired(data_root: Path):
    tools = RegistryBackedToolInvocation()
    builtin_read = object()

    async def _builtin_read(_request):
        return builtin_read

    tools.register("read", _builtin_read, schema=_schema("read"))
    chat = SimpleNamespace(tools=tools)
    container = SimpleNamespace(
        data_paths=SimpleNamespace(root=data_root), user_extensions=None
    )
    host = wire_user_extensions_into_chat(chat=chat, container=container)
    return SimpleNamespace(
        tools=tools, host=host, container=container, builtin_read=builtin_read
    )


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
    assert any("register_mode is not supported yet" in w for w in status.warnings)
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


def test_non_registry_tools_port_is_skipped(tmp_path: Path) -> None:
    container = SimpleNamespace(data_paths=SimpleNamespace(root=tmp_path), user_extensions=None)
    chat = SimpleNamespace(tools=object())
    assert wire_user_extensions_into_chat(chat=chat, container=container) is None
    assert container.user_extensions is None
