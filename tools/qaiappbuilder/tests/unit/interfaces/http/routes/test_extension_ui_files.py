# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Route A: ``ui.module`` in the manifest and ``/api/ext/<name>/_ui/<file>``.

Only the directory holding the module is served; everything else of the
extension (Python, config, data) must stay unreachable from the browser.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api._user_extensions_di import wire_user_extensions_into_chat
from interfaces.http.middleware.auth import AuthMiddleware
from interfaces.http.routes import extension_routes, extensions
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.domain import tool_groups
from qai.platform.user_extensions import ManifestError, module_name_for, parse_manifest

JS = "export function activate(api) { api.ready = true; }\n"


def _manifest(**ui: Any) -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "name": "ui-ext",
        "version": "1.0.0",
        "description": "d",
        "ui": {"label": "UI", **ui},
    }


# -- manifest ------------------------------------------------------------------


def test_module_parsed() -> None:
    m = parse_manifest(_manifest(module="web/index.js"), root=Path("/x/ui-ext"))
    assert m.ui is not None
    assert (m.ui.module, m.ui.module_dir, m.ui.module_file) == ("web/index.js", "web", "index.js")
    deep = parse_manifest(_manifest(module="a/b/main.mjs"), root=Path("/x/ui-ext"))
    assert deep.ui is not None and (deep.ui.module_dir, deep.ui.module_file) == ("a/b", "main.mjs")


@pytest.mark.parametrize(
    ("module", "match"),
    [
        ("index.js", "inside a subdirectory"),
        ("web/index.ts", r"\.js or \.mjs"),
        ("web/index.html", r"\.js or \.mjs"),
        ("../web/index.js", "relative path"),
        ("/abs/index.js", "relative path"),
        ("web/../main.py", "relative path"),
        (3, "relative path"),
    ],
)
def test_bad_module(module: Any, match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        parse_manifest(_manifest(module=module), root=Path("/x/ui-ext"))


def test_a_module_needs_no_backend_code() -> None:
    m = parse_manifest(_manifest(module="web/index.js"), root=Path("/x/ui-ext"))
    assert m.entry is None  # a UI-only extension is valid


# -- HTTP ----------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path):
    ext = tmp_path / "extensions" / "ui-ext"
    (ext / "web" / "sub").mkdir(parents=True)
    (ext / "web" / "index.js").write_bytes(JS.encode())
    (ext / "web" / "sub" / "util.js").write_text("export const x = 1;\n", encoding="utf-8")
    (ext / "web" / "style.css").write_text("a{}", encoding="utf-8")
    (ext / "web" / "evil.py").write_text("print('no')", encoding="utf-8")
    (ext / "web" / "page.html").write_text("<script>1</script>", encoding="utf-8")
    (ext / "extension.json").write_text(
        json.dumps(_manifest(module="web/index.js")), encoding="utf-8"
    )
    (ext / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")
    (ext / "config.toml").write_text('token = "T"', encoding="utf-8")
    (tmp_path / "outside.js").write_text("export const outside = 1;", encoding="utf-8")
    snapshot = dict(tool_groups.TOOL_GROUP_MEMBERS)
    container = SimpleNamespace(data_paths=SimpleNamespace(root=tmp_path), user_extensions=None)
    manager = wire_user_extensions_into_chat(
        chat=SimpleNamespace(tools=RegistryBackedToolInvocation()), container=container
    )
    app = FastAPI()
    app.include_router(extension_routes.build_router(container=container))
    app.include_router(extensions.build_router(container=container))
    yield SimpleNamespace(client=TestClient(app), manager=manager, ext=ext, root=tmp_path)
    manager.unload_all()
    sys.modules.pop(module_name_for("ui-ext"), None)
    tool_groups.TOOL_GROUP_MEMBERS.clear()
    tool_groups.TOOL_GROUP_MEMBERS.update(snapshot)


def test_the_module_is_listed_with_a_versioned_url(env) -> None:
    (entry,) = env.client.get("/api/extensions/ui").json()["extensions"]
    assert entry["name"] == "ui-ext"
    assert entry["module"].startswith("/api/ext/ui-ext/_ui/index.js?v=1.0.0-")


def test_files_are_served_with_the_right_types(env) -> None:
    r = env.client.get("/api/ext/ui-ext/_ui/index.js")
    assert r.status_code == 200 and r.content == JS.encode()
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-cache"
    assert (
        env.client.get("/api/ext/ui-ext/_ui/style.css")
        .headers["content-type"]
        .startswith("text/css")
    )
    assert env.client.get("/api/ext/ui-ext/_ui/sub/util.js").status_code == 200
    assert env.client.head("/api/ext/ui-ext/_ui/index.js").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "..%2fsecret.txt",
        "%2e%2e/secret.txt",
        "sub/../../secret.txt",
        "../config.toml",
        "../extension.json",
        "..\\secret.txt",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "evil.py",  # not an allowed type
        "page.html",  # never served: it would run as a page of the app's origin
        "nope.js",
        "sub",  # a directory
        "",
    ],
)
def test_nothing_outside_the_module_directory_is_reachable(env, path: str) -> None:
    r = env.client.get(f"/api/ext/ui-ext/_ui/{path}")
    assert r.status_code in (404, 405), (path, r.status_code)
    assert "TOP-SECRET" not in r.text and "token" not in r.text


def test_only_get_and_head(env) -> None:
    r = env.client.post("/api/ext/ui-ext/_ui/index.js", json={})
    assert r.status_code == 405 and r.headers["allow"] == "GET, HEAD"
    assert env.client.delete("/api/ext/ui-ext/_ui/index.js").status_code == 405


def test_an_oversize_file_is_refused(env, monkeypatch) -> None:
    monkeypatch.setattr(extension_routes, "_UI_MAX_FILE_BYTES", 10)
    assert env.client.get("/api/ext/ui-ext/_ui/index.js").status_code == 413


def test_a_link_out_of_the_directory_is_refused(env) -> None:
    link = env.ext / "web" / "out"
    try:
        if os.name == "nt":
            importlib.import_module("_winapi").CreateJunction(str(env.root), str(link))
        else:
            link.symlink_to(env.root, target_is_directory=True)
    except (OSError, AttributeError):
        pytest.skip("directory links not supported here")
    assert env.client.get("/api/ext/ui-ext/_ui/out/outside.js").status_code == 404


def test_follows_disable_reload_and_a_missing_module(env) -> None:
    env.manager.set_enabled("ui-ext", False)
    assert env.client.get("/api/ext/ui-ext/_ui/index.js").status_code == 404
    assert env.client.get("/api/extensions/ui").json()["extensions"] == []
    env.manager.set_enabled("ui-ext", True)
    assert env.client.get("/api/ext/ui-ext/_ui/index.js").status_code == 200

    first = env.client.get("/api/extensions/ui").json()["extensions"][0]["module"]
    env.manager.reload("ui-ext")
    second = env.client.get("/api/extensions/ui").json()["extensions"][0]["module"]
    assert first != second  # a reload changes ?v= so the browser fetches the new code

    (env.ext / "web" / "index.js").unlink()
    env.manager.reload("ui-ext")
    assert env.client.get("/api/extensions/ui").json()["extensions"][0]["module"] is None


def test_unknown_extension_is_404(env) -> None:
    assert env.client.get("/api/ext/nope/_ui/index.js").status_code == 404


def test_the_ui_prefix_cannot_be_a_route(env) -> None:
    """``_ui`` is reserved: a declared route can never shadow the file server."""
    with pytest.raises(ManifestError):
        parse_manifest(
            {**_manifest(), "entry": "main.py", "routes": [{"method": "GET", "path": "_ui/x"}]},
            root=Path("/x/ui-ext"),
        )


def test_extension_paths_are_never_public() -> None:
    """The login gate covers the extensions' front-end files and routes.

    ``AuthMiddleware`` lets a few prefixes through unauthenticated; none may
    ever cover ``/api/ext/`` (the module is fetched with the session cookie).
    """
    for path in (
        "/api/ext/ui-ext/_ui/index.js",
        "/api/ext/ui-ext/hosts",
        "/api/extensions/ui",
        "/api/extensions/install/upload",
    ):
        assert AuthMiddleware._is_public(path) is False, path
