# ruff: noqa: E501 - embedded extension source and long request lines
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Extension HTTP routes: manifest ``routes[]``, ``ctx.register_route``, ``/api/ext/...``."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api._user_extensions_di import wire_user_extensions_into_chat
from interfaces.http.routes import extension_routes
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.domain import tool_groups
from qai.platform.user_extensions import ManifestError, module_name_for, parse_manifest

ROUTES = [
    {"method": "GET", "path": "status"},
    {"method": "GET", "path": "hosts/{host_id}"},
    {"method": "GET", "path": "hosts/default"},
    {"method": "POST", "path": "hosts"},
    {"method": "GET", "path": "raw"},
    {"method": "DELETE", "path": "hosts/{host_id}"},
    {"method": "GET", "path": "boom"},
    {"method": "GET", "path": "bad"},
]

CODE = """
from qai.ext import RouteResponse

HOSTS = {}

def activate(ctx):
    ctx.register_route("GET", "status", lambda req: {"ok": True, "hosts": len(HOSTS), "q": req.query})

    async def get_host(req):
        h = HOSTS.get(req.params["host_id"])
        return h if h else RouteResponse(body={"error": "unknown host"}, status=404)
    ctx.register_route("GET", "hosts/{host_id}", get_host)
    ctx.register_route("GET", "hosts/default", lambda req: {"literal": True})

    def add(req):
        HOSTS[req.body["id"]] = req.body
        return RouteResponse(body=req.body, status=201)
    ctx.register_route("POST", "hosts", add)
    ctx.register_route("GET", "raw", lambda req: RouteResponse(
        body="<b>hi</b>", content_type="text/html", headers={"x-ext": "1"}))
    ctx.register_route("DELETE", "hosts/{host_id}", lambda req: HOSTS.pop(req.params["host_id"], None) and None)
    def boom(req):
        raise RuntimeError("kaput")
    ctx.register_route("GET", "boom", boom)
    ctx.register_route("GET", "bad", lambda req: {1, 2})
"""


def _manifest(**kw: Any) -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "name": "demo-ext",
        "version": "1",
        "description": "d",
        "entry": "main.py",
        **kw,
    }


# -- manifest ----------------------------------------------------------------------


def test_routes_parsed_and_matched() -> None:
    m = parse_manifest(_manifest(routes=ROUTES), root=Path("/x/demo-ext"))
    r = m.route("GET", "hosts/{host_id}")
    assert r is not None
    assert r.match("GET", "hosts/h1") == {"host_id": "h1"}
    assert r.match("GET", "hosts/h1/x") is None
    assert r.match("POST", "hosts/h1") is None
    assert m.route("GET", "status").match("GET", "/status/") == {}


@pytest.mark.parametrize(
    ("routes", "match"),
    [
        ({}, "must be an array"),
        ([{"method": "TRACE", "path": "x"}], "method"),
        ([{"method": "GET", "path": "/x"}], "segments"),
        ([{"method": "GET", "path": "x/"}], "segments"),
        ([{"method": "GET", "path": "X"}], "segments"),
        ([{"method": "GET", "path": "a/../b"}], "segments"),
        ([{"method": "GET", "path": "a/{id}/{id}"}], "repeats"),
        ([{"method": "GET", "path": "a/{x}"}, {"method": "GET", "path": "a/{y}"}], "clashes"),
        ([{"method": "GET", "path": "a", "auth": False}], "unknown field"),
    ],
)
def test_bad_routes(routes: Any, match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        parse_manifest(_manifest(routes=routes), root=Path("/x/demo-ext"))


def test_routes_need_an_entry() -> None:
    data = _manifest(routes=ROUTES)
    del data["entry"]
    with pytest.raises(ManifestError, match="routes requires entry"):
        parse_manifest(data, root=Path("/x/demo-ext"))


# -- HTTP --------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path):
    ext = tmp_path / "extensions" / "demo-ext"
    ext.mkdir(parents=True)
    (ext / "extension.json").write_text(json.dumps(_manifest(routes=ROUTES)), encoding="utf-8")
    (ext / "main.py").write_text(textwrap.dedent(CODE), encoding="utf-8")
    snapshot = dict(tool_groups.TOOL_GROUP_MEMBERS)
    container = SimpleNamespace(data_paths=SimpleNamespace(root=tmp_path), user_extensions=None)
    manager = wire_user_extensions_into_chat(
        chat=SimpleNamespace(tools=RegistryBackedToolInvocation()), container=container
    )
    app = FastAPI()
    app.include_router(extension_routes.build_router(container=container))
    yield SimpleNamespace(client=TestClient(app), manager=manager, ext=ext)
    manager.unload_all()
    sys.modules.pop(module_name_for("demo-ext"), None)
    tool_groups.TOOL_GROUP_MEMBERS.clear()
    tool_groups.TOOL_GROUP_MEMBERS.update(snapshot)


def test_json_get_with_query(env) -> None:
    r = env.client.get("/api/ext/demo-ext/status", params={"a": "1"})
    assert r.status_code == 200 and r.json() == {"ok": True, "hosts": 0, "q": {"a": "1"}}


def test_post_body_params_status_and_literal_precedence(env) -> None:
    r = env.client.post("/api/ext/demo-ext/hosts", json={"id": "h1", "addr": "10.0.0.1"})
    assert r.status_code == 201 and r.json()["addr"] == "10.0.0.1"
    assert env.client.get("/api/ext/demo-ext/hosts/h1").json()["id"] == "h1"
    assert env.client.get("/api/ext/demo-ext/hosts/nope").status_code == 404
    assert env.client.get("/api/ext/demo-ext/hosts/default").json() == {"literal": True}
    assert env.client.delete("/api/ext/demo-ext/hosts/h1").status_code == 204


def test_raw_response(env) -> None:
    r = env.client.get("/api/ext/demo-ext/raw")
    assert r.text == "<b>hi</b>" and r.headers["content-type"].startswith("text/html")
    assert r.headers["x-ext"] == "1"


def test_errors(env) -> None:
    r = env.client.get("/api/ext/demo-ext/boom")
    assert r.status_code == 500 and "kaput" in r.json()["message"]
    assert env.client.get("/api/ext/demo-ext/bad").status_code == 500
    assert env.client.get("/api/ext/demo-ext/nope").status_code == 404
    assert env.client.get("/api/ext/no-ext/status").status_code == 404
    r = env.client.put("/api/ext/demo-ext/hosts/h1")
    assert r.status_code == 405 and set(r.headers["allow"].split(", ")) == {"GET", "DELETE"}


def test_secrets_of_the_app_are_not_passed_on(env) -> None:
    code = (
        (env.ext / "main.py")
        .read_text("utf-8")
        .replace(
            'ctx.register_route("GET", "status", lambda req: {"ok": True, "hosts": len(HOSTS), "q": req.query})',
            'ctx.register_route("GET", "status", lambda req: req.headers)',
        )
    )
    (env.ext / "main.py").write_text(code, encoding="utf-8")
    env.manager.reload("demo-ext")
    seen = env.client.get(
        "/api/ext/demo-ext/status",
        headers={"Authorization": "Bearer x", "Cookie": "qai_session=s", "X-Custom": "1"},
    ).json()
    assert "authorization" not in seen and "cookie" not in seen and seen["x-custom"] == "1"


def test_routes_follow_disable_and_reload(env) -> None:
    env.manager.set_enabled("demo-ext", False)
    assert env.client.get("/api/ext/demo-ext/status").status_code == 404
    env.manager.set_enabled("demo-ext", True)
    assert env.client.get("/api/ext/demo-ext/status").status_code == 200


def test_undeclared_route_fails_the_load(env) -> None:
    (env.ext / "main.py").write_text(
        "def activate(ctx):\n    ctx.register_route('GET', 'secret', lambda r: 1)\n",
        encoding="utf-8",
    )
    status = env.manager.reload("demo-ext")
    assert status.state == "failed" and "not declared in the manifest's routes" in status.error
    assert env.client.get("/api/ext/demo-ext/status").status_code == 404
