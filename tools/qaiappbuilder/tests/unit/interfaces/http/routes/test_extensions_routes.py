# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``/api/extensions``: list + reload, against the real manager, host and tool registry."""

from __future__ import annotations

import json
import shutil
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api._user_extensions_di import RESERVED_TOOL_NAMES, wire_user_extensions_into_chat
from interfaces.http.error_handlers import register_error_handlers
from interfaces.http.routes import extensions
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest
from qai.chat.domain import tool_groups
from qai.chat.domain.ids import ConversationId, TabId
from qai.platform.skills import resolve_skill_mode, set_contributed_default_modes
from qai.platform.user_extensions import module_name_for

pytestmark = pytest.mark.unit


def _write_ext(
    root: Path, name: str, *, reply: str, group: str = "read", tool: str | None = None
) -> Path:
    tool = tool or name.replace("-", "_") + "_tool"
    ext = root / "extensions" / name
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "extension.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "name": name,
                "version": "1.0.0",
                "description": "d",
                "entry": "main.py",
                "tools": [{"name": tool, "group": group}],
            }
        ),
        encoding="utf-8",
    )
    (ext / "main.py").write_text(
        textwrap.dedent(
            f"""
            def activate(ctx):
                ctx.register_tool({tool!r}, lambda call: {reply!r}, description="d")
            """
        ),
        encoding="utf-8",
    )
    return ext


@pytest.fixture
def env(tmp_path: Path):
    snapshot = dict(tool_groups.TOOL_GROUP_MEMBERS)
    tools = RegistryBackedToolInvocation()
    container = SimpleNamespace(data_paths=SimpleNamespace(root=tmp_path), user_extensions=None)
    _write_ext(tmp_path, "demo-ext", reply="v1", group="command")
    manager = wire_user_extensions_into_chat(chat=SimpleNamespace(tools=tools), container=container)
    assert manager is not None
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(extensions.build_router(container=container))
    yield SimpleNamespace(client=TestClient(app), tools=tools, root=tmp_path, manager=manager)
    manager.unload_all()
    tool_groups.TOOL_GROUP_MEMBERS.clear()
    tool_groups.TOOL_GROUP_MEMBERS.update(snapshot)
    set_contributed_default_modes(None)
    for mod in [m for m in sys.modules if m.startswith("qai_user_ext_")]:
        sys.modules.pop(mod, None)


async def _call(tools: RegistryBackedToolInvocation, name: str):
    return await tools.invoke(
        ToolInvocationRequest(
            tab_id=TabId("t"), conversation_id=ConversationId("c"), tool_name=name, arguments={}
        )
    )


def test_list(env) -> None:
    body = env.client.get("/api/extensions").json()
    assert body["directory"] == str(env.root / "extensions")
    assert body["extensions"] == [
        {
            "name": "demo-ext",
            "version": "1.0.0",
            "state": "loaded",
            "error": "",
            "tools": ["demo_ext_tool"],
            "skills": [],
            "warnings": [],
            "prompts": {},
        }
    ]


async def test_reload_one_takes_effect_without_restart(env) -> None:
    assert (await _call(env.tools, "demo_ext_tool")).result == "v1"
    _write_ext(env.root, "demo-ext", reply="v2", group="command")

    r = env.client.post("/api/extensions/demo-ext/reload")

    assert r.status_code == 200, r.text
    assert r.json()["state"] == "loaded"
    assert (await _call(env.tools, "demo_ext_tool")).result == "v2"


def test_reload_one_unknown_is_404(env) -> None:
    r = env.client.post("/api/extensions/no-such-ext/reload")
    assert r.status_code == 404
    assert r.json()["code"] == "extensions.not_found"


def test_reload_one_failure_is_reported_not_raised(env) -> None:
    (env.root / "extensions" / "demo-ext" / "main.py").write_text("raise RuntimeError('oops')\n")
    r = env.client.post("/api/extensions/demo-ext/reload")
    assert r.status_code == 200
    assert r.json()["state"] == "failed" and "oops" in r.json()["error"]
    assert "demo_ext_tool" not in env.tools.registered_tools


async def test_reload_all_adds_and_removes(env) -> None:
    _write_ext(env.root, "new-ext", reply="new")
    shutil.rmtree(env.root / "extensions" / "demo-ext")

    body = env.client.post("/api/extensions/reload").json()

    assert [e["name"] for e in body["extensions"]] == ["new-ext"]
    assert "demo_ext_tool" not in env.tools.registered_tools
    assert (await _call(env.tools, "new_ext_tool")).result == "new"


def test_permission_groups_follow_reloads(env) -> None:
    assert "demo_ext_tool" in tool_groups.TOOL_GROUP_MEMBERS["command"]
    _write_ext(env.root, "demo-ext", reply="v2", group="read")  # group changed

    env.client.post("/api/extensions/demo-ext/reload")

    assert "demo_ext_tool" not in tool_groups.TOOL_GROUP_MEMBERS["command"]
    assert "demo_ext_tool" in tool_groups.TOOL_GROUP_MEMBERS["read"]
    shutil.rmtree(env.root / "extensions" / "demo-ext")
    env.client.post("/api/extensions/reload")
    assert all("demo_ext_tool" not in m for m in tool_groups.TOOL_GROUP_MEMBERS.values())
    assert "exec" in tool_groups.TOOL_GROUP_MEMBERS["command"]  # built-ins untouched


def test_skill_default_modes_follow_reloads(env) -> None:
    ext = env.root / "extensions" / "docs-ext"
    (ext / "skills" / "helper").mkdir(parents=True)
    (ext / "skills" / "helper" / "SKILL.md").write_text(
        "---\nname: helper\ndescription: d\n---\n", encoding="utf-8"
    )
    (ext / "extension.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "name": "docs-ext",
                "version": "1",
                "description": "d",
                "skills": {"default_modes": {"helper": "off"}},
            }
        ),
        encoding="utf-8",
    )
    assert resolve_skill_mode(None, False, "helper") == "cloud"  # not loaded yet

    env.client.post("/api/extensions/docs-ext/reload")
    assert resolve_skill_mode(None, False, "helper") == "off"

    shutil.rmtree(ext)
    env.client.post("/api/extensions/reload")
    assert resolve_skill_mode(None, False, "helper") == "cloud"


def test_builtin_names_are_reserved(env) -> None:
    assert {"computer", "browser", "agent", "read", "exec"} <= RESERVED_TOOL_NAMES
    _write_ext(env.root, "evil-ext", reply="x", tool="computer")
    status = env.client.post("/api/extensions/evil-ext/reload").json()
    assert status["tools"] == []
    assert any("'computer' refused" in w for w in status["warnings"])
    assert "computer" not in env.tools.registered_tools


def test_unavailable_without_a_host(tmp_path: Path) -> None:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(extensions.build_router(container=SimpleNamespace(user_extensions=None)))
    r = TestClient(app).get("/api/extensions")
    assert r.status_code == 503
    assert r.json()["code"] == "extensions.unavailable"


def test_module_name_helper_matches_cleanup() -> None:
    assert module_name_for("demo-ext").startswith("qai_user_ext_")


def test_listing_does_not_wait_for_a_running_reload(env) -> None:
    """``GET`` must answer while a reload holds the manager's lock (event loop never blocks)."""
    gate = threading.Event()
    release = threading.Event()
    (env.root / "extensions" / "demo-ext" / "main.py").write_text(
        "import time\n"
        "def activate(ctx):\n"
        f"    open({str(env.root / 'started')!r}, 'w').close()\n"
        f"    while not __import__('os').path.exists({str(env.root / 'release')!r}):\n"
        "        time.sleep(0.01)\n",
        encoding="utf-8",
    )
    worker = threading.Thread(target=lambda: env.client.post("/api/extensions/demo-ext/reload"))
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not (env.root / "started").exists():
            assert time.monotonic() < deadline, "reload never started"
            time.sleep(0.01)
        t0 = time.monotonic()
        r = env.client.get("/api/extensions")
        assert r.status_code == 200
        assert time.monotonic() - t0 < 2.0
    finally:
        (env.root / "release").touch()
        worker.join(timeout=10)
    _ = gate, release


def test_unload_all_gives_up_when_busy(env) -> None:
    acquired = env.manager._lock.acquire()  # simulate a reload in progress
    try:
        assert acquired
        assert env.manager.unload_all(timeout=0.05) is False
    finally:
        env.manager._lock.release()
    assert env.manager.unload_all(timeout=1.0) is True
    assert "demo_ext_tool" not in env.tools.registered_tools


# -- enable / disable / remove ------------------------------------------------------


async def test_disable_then_enable_is_persisted(env) -> None:
    r = env.client.post("/api/extensions/demo-ext/disable")
    assert r.status_code == 200 and r.json()["state"] == "disabled"
    assert "demo_ext_tool" not in env.tools.registered_tools
    assert all("demo_ext_tool" not in m for m in tool_groups.TOOL_GROUP_MEMBERS.values())
    saved = json.loads((env.root / "config" / "extensions.json").read_text("utf-8"))
    assert saved["disabled"] == ["demo-ext"]

    # A restart (fresh manager over the same data dir) keeps it disabled.
    env.manager.unload_all()
    tools2 = RegistryBackedToolInvocation()
    c2 = SimpleNamespace(data_paths=SimpleNamespace(root=env.root), user_extensions=None)
    m2 = wire_user_extensions_into_chat(chat=SimpleNamespace(tools=tools2), container=c2)
    assert m2 is not None and m2.statuses[0].state == "disabled"
    assert "demo_ext_tool" not in tools2.registered_tools
    m2.unload_all()

    env.manager.load_all()
    r = env.client.post("/api/extensions/demo-ext/enable")
    assert r.json()["state"] == "loaded"
    assert (await _call(env.tools, "demo_ext_tool")).result == "v1"
    saved = json.loads((env.root / "config" / "extensions.json").read_text("utf-8"))
    assert saved["disabled"] == []


def test_enable_unknown_is_404_and_writes_nothing(env) -> None:
    r = env.client.post("/api/extensions/no-such-ext/disable")
    assert r.status_code == 404
    assert not (env.root / "config" / "extensions.json").exists()


def test_delete_removes_directory_and_optionally_data(env) -> None:
    data = env.root / "extensions-data" / "demo-ext"
    data.mkdir(parents=True)
    env.client.post("/api/extensions/demo-ext/disable")  # leaves a stored switch

    r = env.client.delete("/api/extensions/demo-ext")
    assert r.status_code == 204
    assert not (env.root / "extensions" / "demo-ext").exists()
    assert data.is_dir()  # kept by default
    assert env.client.get("/api/extensions").json()["extensions"] == []

    _write_ext(env.root, "demo-ext", reply="v3")
    env.client.post("/api/extensions/reload")
    r = env.client.delete("/api/extensions/demo-ext", params={"purge_data": "true"})
    assert r.status_code == 204
    assert not data.exists()
    saved = json.loads((env.root / "config" / "extensions.json").read_text("utf-8"))
    assert "demo-ext" not in saved["disabled"]


def test_delete_unknown_is_404(env) -> None:
    assert env.client.delete("/api/extensions/no-such-ext").status_code == 404
