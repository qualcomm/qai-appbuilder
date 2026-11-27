# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ctx.register_mcp_server``: MCP servers an extension decides at run time."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from qai.platform.user_extensions import (
    ExtensionError,
    ManifestError,
    module_name_for,
    parse_manifest,
)

from .conftest import manifest

OK_CODE = """
def activate(ctx):
    ctx.register_mcp_server(
        "pkg-one", command="python", args=["-m", "pkg.server"],
        env={"PYTHONPATH": "C:/x/site", "API_KEY": "s3cret"}, secret_env=["API_KEY"],
    )
    ctx.register_mcp_server("remote", transport="http", url="https://mcp.example.com")
"""


def _dyn(**kw: Any) -> dict[str, Any]:
    return manifest("dyn-ext", entry="main.py", dynamic_mcp_servers=True, **kw)


def test_manifest_flag() -> None:
    assert parse_manifest(_dyn(), root=Path("/x/dyn-ext")).dynamic_mcp_servers is True
    assert parse_manifest(manifest("dyn-ext"), root=Path("/x/dyn-ext")).dynamic_mcp_servers is False
    with pytest.raises(ManifestError, match="true or false"):
        parse_manifest(
            manifest("dyn-ext", entry="main.py", dynamic_mcp_servers="yes"), root=Path("/x/dyn-ext")
        )
    with pytest.raises(ManifestError, match="requires entry"):
        parse_manifest(manifest("dyn-ext", dynamic_mcp_servers=True), root=Path("/x/dyn-ext"))


def test_registered_servers_are_listed_and_follow_the_extension(make_ext, new_host) -> None:
    make_ext("dyn-ext", manifest_data=_dyn(), code=OK_CODE)
    host = new_host()
    (status,) = host.load_all()
    assert status.state == "loaded", status.error
    assert status.mcp_servers == ("dyn-ext-pkg-one", "dyn-ext-remote")
    servers = {s.name: s for s in host.mcp_servers()}
    one = servers["dyn-ext-pkg-one"]
    assert (one.extension, one.transport, one.command, one.args) == (
        "dyn-ext",
        "stdio",
        "python",
        ("-m", "pkg.server"),
    )
    assert one.env["PYTHONPATH"] == "C:/x/site" and one.secret_env_keys == ("API_KEY",)
    assert servers["dyn-ext-remote"].url == "https://mcp.example.com"

    host.unload("dyn-ext")
    assert host.mcp_servers() == ()


def test_a_reload_runs_activate_again_with_the_new_set(make_ext, new_host) -> None:
    ext = make_ext("dyn-ext", manifest_data=_dyn(), code=OK_CODE)
    host = new_host()
    host.load_all()
    (ext / "main.py").write_text(
        "def activate(ctx):\n    ctx.register_mcp_server('pkg-two', command='python')\n",
        encoding="utf-8",
    )
    assert host.reload("dyn-ext").mcp_servers == ("dyn-ext-pkg-two",)
    assert [s.name for s in host.mcp_servers()] == ["dyn-ext-pkg-two"]


@pytest.mark.parametrize(
    ("call", "match"),
    [
        ("ctx.register_mcp_server('Bad_Name', command='x')", "name must match"),
        (
            "ctx.register_mcp_server('a', command='x'); ctx.register_mcp_server('a', command='y')",
            "twice",
        ),
        ("ctx.register_mcp_server('a')", "stdio needs command"),
        ("ctx.register_mcp_server('a', command='x', url='http://h')", "stdio needs command"),
        ("ctx.register_mcp_server('a', transport='http')", "http needs url"),
        (
            "ctx.register_mcp_server('a', transport='http', url='http://h', command='x')",
            "http needs url",
        ),
        ("ctx.register_mcp_server('a', transport='ws', url='x')", "transport must be"),
        ("ctx.register_mcp_server('a', command='x', timeout_s=0)", "timeout_s"),
        ("ctx.register_mcp_server('a', command='x', timeout_s=True)", "timeout_s"),
    ],
)
def test_misuse_fails_the_load(make_ext, new_host, call: str, match: str) -> None:
    make_ext("dyn-ext", manifest_data=_dyn(), code=f"def activate(ctx):\n    {call}\n")
    (status,) = new_host().load_all()
    assert status.state == "failed" and match in status.error, status.error


def test_without_the_manifest_flag_it_is_refused(make_ext, new_host) -> None:
    make_ext(
        "dyn-ext",
        manifest_data=manifest("dyn-ext", entry="main.py"),
        code="def activate(ctx):\n    ctx.register_mcp_server('a', command='x')\n",
    )
    (status,) = new_host().load_all()
    assert status.state == "failed" and "dynamic_mcp_servers" in status.error


def test_only_during_activate(make_ext, new_host) -> None:
    make_ext(
        "dyn-ext",
        manifest_data=_dyn(),
        code="SAVED = []\ndef activate(ctx):\n    SAVED.append(ctx)\n",
    )
    host = new_host()
    host.load_all()

    ctx = sys.modules[module_name_for("dyn-ext")].SAVED[0]
    with pytest.raises(ExtensionError, match="only allowed while activate"):
        ctx.register_mcp_server("late", command="x")
