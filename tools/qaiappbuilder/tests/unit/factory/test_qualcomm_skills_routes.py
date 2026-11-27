# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Real Container/extension manager/FastAPI integration, with offline synthetic deliveries.

Only pip's subprocess boundary is faked. The real MCP registry has its master
switch off: dynamic configs are registered without launching wheel code or
opening a network connection. No application lifespan/full graph is needed.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_qualcomm_skills_packages import ROOT, FakePip, delivery

from apps.api._user_extensions_di import extension_skill_roots, wire_user_extensions_into_chat
from apps.api.di import Container
from interfaces.http.error_handlers import register_error_handlers
from interfaces.http.routes import extension_routes, extensions
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.adapters.mcp_client import McpServerRegistry
from qai.chat.domain import tool_groups
from qai.platform.config import DataPaths, Settings
from qai.platform.persistence.secrets import NullSecretStore
from qai.platform.skills import set_contributed_default_modes
from qai.platform.user_extensions import module_name_for

pytestmark = pytest.mark.unit

NAME = "qualcomm-skills"
BASE = f"/api/ext/{NAME}"
SERVER = f"{NAME}-demo-board"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pip = FakePip()

    def fake_run(cmd, **kwargs):
        # Patch the subprocess boundary, not a Packages instance: each automatic
        # reload imports a fresh extension module with fresh default arguments.
        assert cmd[:4] == [sys.executable, "-m", "pip", "install"]
        return pip(cmd, kwargs["timeout"])

    def no_network(*_args, **_kwargs):
        pytest.fail("route tests must never open a network connection")

    connect = socket.socket.connect

    def guarded_connect(sock, address):
        # Windows asyncio's socketpair fallback needs a local loopback socket.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return connect(sock, address)
        return no_network()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    groups = dict(tool_groups.TOOL_GROUP_MEMBERS)
    prefix = module_name_for(NAME)
    previous_modules = {key for key in sys.modules if key == prefix or key.startswith(prefix + ".")}
    tools = RegistryBackedToolInvocation()
    config_path = tmp_path / "config" / "mcp_servers.json"
    registry = McpServerRegistry(tools=tools, config_path=config_path, enabled=False)
    container = Container(
        settings=Settings(),
        data_paths=DataPaths(tmp_path),
        database=None,  # type: ignore[arg-type] - unused by extension composition
        secret_store=NullSecretStore(),
        events=None,  # type: ignore[arg-type] - unused by extension composition
        repo_root=ROOT,
    )
    container.chat = SimpleNamespace(  # type: ignore[assignment]
        tools=tools, mcp_server_registry=registry
    )
    manager = wire_user_extensions_into_chat(chat=container.chat, container=container)
    assert manager is not None and container.user_extensions is manager
    status = next(s for s in manager.statuses if s.name == NAME)
    assert status.state == "loaded", status.error
    data_dir = tmp_path / "extensions-data" / NAME
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(extensions.build_router(container=container))
    app.include_router(extension_routes.build_router(container=container))
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client,
                manager=manager,
                container=container,
                registry=registry,
                pip=pip,
                data_dir=data_dir,
                config_path=config_path,
            )
            manager.unload_all()
            client.portal.call(manager.sync_runtime)
            assert client.portal.call(registry.list_servers) == ()
    finally:
        manager.unload_all()
        tool_groups.TOOL_GROUP_MEMBERS.clear()
        tool_groups.TOOL_GROUP_MEMBERS.update(groups)
        set_contributed_default_modes(None)
        for key in list(sys.modules):
            if (key == prefix or key.startswith(prefix + ".")) and key not in previous_modules:
                sys.modules.pop(key, None)


def _status(env):
    response = env.client.get("/api/extensions")
    assert response.status_code == 200, response.text
    return next(s for s in response.json()["extensions"] if s["name"] == NAME)


def _stage(env, **wheel_kwargs):
    response = env.client.post(
        f"{BASE}/packages/stage",
        params={"filename": r"C:\deliveries\HS00-1.zip"},
        content=delivery(requires=(), **wheel_kwargs),
        headers={"Content-Type": "application/zip"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _install(env):
    staged = _stage(env)
    response = env.client.post(
        f"{BASE}/packages/install", json={"token": staged["token"], "allow_network": False}
    )
    assert response.status_code == 201, response.text
    return response.json()["installed"]


def test_builtin_ui_and_uninstall_refused_without_stopping(env) -> None:
    meta = _install(env)
    status = _status(env)
    assert status["builtin"] is True and status["state"] == "loaded"
    response = env.client.get("/api/extensions/ui")
    assert response.status_code == 200, response.text
    entry = next(e for e in response.json()["extensions"] if e["name"] == NAME)
    assert entry["label"] == "Qualcomm Skills" and entry["actions"] == []
    assert entry["module"].startswith(f"{BASE}/_ui/index.js?v=")
    module = env.client.get(entry["module"])
    assert module.status_code == 200 and module.text
    assert module.headers["content-type"].startswith("application/javascript")

    for query in ("", "?purge_data=true"):
        refused = env.client.delete(f"/api/extensions/{NAME}{query}")
        assert refused.status_code == 409 and refused.json()["code"] == "extensions.builtin"
        assert _status(env) == status
        assert (env.data_dir / "packages" / meta["id"] / "package.json").is_file()
        assert [s.config.name for s in env.client.portal.call(env.registry.list_servers)] == [
            SERVER
        ]


def test_raw_upload_returns_nested_preview_without_installing(env) -> None:
    staged = _stage(env)
    assert set(staged) == {"token", "preview"}
    preview = staged["preview"]
    assert staged["token"] == preview["token"]
    assert preview["name"] == "demo-board-mcp" and preview["version"] == "1.2"
    assert preview["nesting"] == ["HS00-1.zip", "HS00-1_rev_1.1.zip", "Demo_Authorized.zip"]
    assert preview["skill_count"] == 3 and preview["server"] == "demo_board_mcp.server"
    assert preview["readme"].startswith("# demo-board-mcp")
    assert preview["missing"] == [] and preview["python_ok"] is True
    assert env.client.get(f"{BASE}/packages").json() == {"packages": []}
    assert env.pip.calls == [] and _status(env)["mcp_servers"] == []
    assert env.client.portal.call(env.registry.list_servers) == ()
    assert (env.data_dir / "staging" / staged["token"]).is_dir()
    discarded = env.client.delete(f"{BASE}/packages/stage/{staged['token']}")
    assert discarded.status_code == 204
    assert not (env.data_dir / "staging" / staged["token"]).exists()


def test_install_automatically_reloads_and_registers_dynamic_mcp(env) -> None:
    before = env.manager.host.ui_module_url(NAME)
    meta = _install(env)
    assert env.manager.host.ui_module_url(NAME) != before
    assert _status(env)["mcp_servers"] == [SERVER]
    (server,) = env.client.portal.call(env.registry.list_servers)
    assert server.config.name == SERVER and server.source == f"extension:{NAME}"
    assert server.connected is False  # real registry's offline master gate
    (resolved,) = env.manager.host.mcp_servers()
    site = env.data_dir / "packages" / meta["id"] / "site"
    assert resolved.command == sys.executable
    assert resolved.args == ("-m", "demo_board_mcp.server")
    assert resolved.env["PYTHONPATH"] == str(site)
    assert resolved.env["PYTHONNOUSERSITE"] == "1"
    assert (site / "demo_board_mcp" / "server.py").is_file()
    assert env.client.get(f"{BASE}/packages").json() == {"packages": [meta]}
    readme = env.client.get(f"{BASE}/packages/{meta['id']}/readme")
    assert readme.status_code == 200 and readme.json()["readme"].startswith("# demo-board-mcp")
    (cmd,) = env.pip.calls
    assert "--no-index" in cmd and "--no-deps" in cmd and "--proxy" not in cmd
    assert not env.config_path.exists()  # contributed config is not persisted as user-owned
    assert extension_skill_roots(env.container) == []
    assert _status(env)["skills"] == []  # wheel skills are discovered through MCP, not a scan
    assert not any((env.data_dir / "staging").iterdir())


def test_delete_stops_mcp_before_files_then_reloads_without_client_followup(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _install(env)
    package_dir = env.data_dir / "packages" / meta["id"]
    before = env.manager.host.ui_module_url(NAME)
    stopped = []
    drop = env.registry._drop_server

    async def observe_drop(name):
        assert name == SERVER
        assert (package_dir / "site" / "demo_board_mcp" / "server.py").is_file()
        assert env.manager.host.mcp_servers() == ()  # unloaded before deletion
        await drop(name)
        stopped.append(name)

    monkeypatch.setattr(env.registry, "_drop_server", observe_drop)
    response = env.client.delete(f"{BASE}/packages/{meta['id']}")
    assert response.status_code == 200, response.text
    assert response.json() == {"removed": meta["id"], "reload": True}
    assert stopped == [SERVER] and not package_dir.exists()
    assert env.client.portal.call(env.registry.list_servers) == ()
    assert env.manager.host.mcp_servers() == ()
    assert _status(env)["state"] == "loaded" and _status(env)["mcp_servers"] == []
    assert env.manager.host.ui_module_url(NAME) != before
    assert env.client.get(f"{BASE}/packages").json() == {"packages": []}
    assert env.client.get(f"{BASE}/packages/{meta['id']}/readme").json() == {"readme": ""}


def test_builtin_can_be_disabled_and_enabled_with_package_data_kept(env) -> None:
    meta = _install(env)
    response = env.client.post(f"/api/extensions/{NAME}/disable")
    assert response.status_code == 200 and response.json()["state"] == "disabled"
    assert response.json()["builtin"] is True
    assert env.client.portal.call(env.registry.list_servers) == ()
    assert env.client.get(f"{BASE}/packages").status_code == 404
    assert all(e["name"] != NAME for e in env.client.get("/api/extensions/ui").json()["extensions"])
    assert (env.data_dir / "packages" / meta["id"] / "package.json").is_file()
    enabled = env.client.post(f"/api/extensions/{NAME}/enable")
    assert enabled.status_code == 200 and enabled.json()["mcp_servers"] == [SERVER]
    assert env.client.get(f"{BASE}/packages").json() == {"packages": [meta]}
    assert [s.config.name for s in env.client.portal.call(env.registry.list_servers)] == [SERVER]


def test_failed_install_restores_existing_mcp_via_runtime_reload(env) -> None:
    meta = _install(env)
    staged = _stage(env, version="1.3")
    env.pip.returncode = 1
    env.pip.stderr = "ERROR: synthetic failure (no real pip or network)"
    response = env.client.post(
        f"{BASE}/packages/install", json={"token": staged["token"], "allow_network": False}
    )
    assert response.status_code == 400 and response.json()["code"] == "qcskills.pip_failed"
    assert _status(env)["state"] == "loaded" and _status(env)["mcp_servers"] == [SERVER]
    assert [s.config.name for s in env.client.portal.call(env.registry.list_servers)] == [SERVER]
    assert env.client.get(f"{BASE}/packages").json() == {"packages": [meta]}
    assert (env.data_dir / "packages" / meta["id"] / "package.json").is_file()
    assert not list((env.data_dir / "packages").glob(".installing-*"))
