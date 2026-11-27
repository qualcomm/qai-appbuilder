# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Built-in extensions (``factory/extensions``): read in place, first, never uninstalled."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from qai.platform.user_extensions import (
    BuiltinExtensionError,
    ExtensionStateStore,
    UserExtensionHost,
    module_name_for,
)

from .conftest import FakeSink


def _ext(root: Path, name: str, *, tool: str | None = None, version: str = "1") -> Path:
    d = root / name
    d.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "name": name,
        "version": version,
        "description": "d",
    }
    if tool:
        manifest["entry"] = "main.py"
        manifest["tools"] = [{"name": tool, "group": None}]
        (d / "main.py").write_text(
            "def activate(ctx):\n"
            f"    ctx.register_tool({tool!r}, lambda c: {version!r}, description='d')\n",
            encoding="utf-8",
        )
    (d / "extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


@pytest.fixture
def roots(tmp_path: Path):
    builtin, user = tmp_path / "factory" / "extensions", tmp_path / "data" / "extensions"
    builtin.mkdir(parents=True)
    user.mkdir(parents=True)
    yield builtin, user, tmp_path
    for name in ("core-ext", "user-ext"):
        sys.modules.pop(module_name_for(name), None)


def _host(roots, **kw: Any) -> tuple[UserExtensionHost, FakeSink]:
    builtin, user, tmp = roots
    sink = FakeSink()
    return UserExtensionHost(user, sink, data_root=tmp / "data", builtin_root=builtin, **kw), sink


def test_builtin_and_user_extensions_load_and_are_marked(roots) -> None:
    builtin, user, _ = roots
    _ext(builtin, "core-ext", tool="core_tool")
    _ext(user, "user-ext", tool="user_tool")
    host, sink = _host(roots)
    statuses = {s.name: s for s in host.load_all()}
    assert statuses["core-ext"].builtin is True and statuses["core-ext"].state == "loaded"
    assert statuses["user-ext"].builtin is False and statuses["user-ext"].state == "loaded"
    assert {"core_tool", "user_tool"} <= sink.tool_names()
    assert host.is_builtin("core-ext") and not host.is_builtin("user-ext")
    assert statuses["core-ext"].root == builtin / "core-ext"  # read in place, never copied


def test_a_user_extension_cannot_take_a_builtin_name(roots) -> None:
    builtin, user, _ = roots
    _ext(builtin, "core-ext", tool="core_tool", version="builtin")
    _ext(user, "core-ext", tool="core_tool", version="user")
    host, _ = _host(roots)
    (status,) = [s for s in host.load_all() if s.name == "core-ext"]
    assert status.builtin is True and status.root == builtin / "core-ext"
    assert host.manifest("core-ext").version == "builtin"
    assert host.reload("core-ext").root == builtin / "core-ext"  # reload keeps the built-in


def test_a_builtin_cannot_be_uninstalled_but_can_be_disabled(roots, tmp_path: Path) -> None:
    builtin, _, tmp = roots
    _ext(builtin, "core-ext", tool="core_tool")
    state = ExtensionStateStore(tmp / "data" / "config" / "extensions.json")
    host, sink = _host(roots, is_disabled=state.is_disabled)
    host.load_all()
    with pytest.raises(BuiltinExtensionError):
        host.remove("core-ext")
    with pytest.raises(BuiltinExtensionError):
        host.remove("core-ext", purge_data=True)
    assert (builtin / "core-ext" / "extension.json").is_file()  # untouched
    assert "core_tool" in sink.tool_names()

    state.set_disabled("core-ext", True)
    status = host.reload("core-ext")
    assert status.state == "disabled" and status.builtin is True
    assert "core_tool" not in sink.tool_names()


def test_without_a_builtin_root_nothing_changes(tmp_path: Path) -> None:
    user = tmp_path / "extensions"
    _ext(user, "user-ext", tool="user_tool")
    host = UserExtensionHost(user, FakeSink(), data_root=tmp_path)
    (status,) = host.load_all()
    assert status.builtin is False and host.builtin_names() == frozenset()
    sys.modules.pop(module_name_for("user-ext"), None)
