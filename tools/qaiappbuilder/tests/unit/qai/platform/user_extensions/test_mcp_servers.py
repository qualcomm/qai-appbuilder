# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Extension MCP servers: manifest ``mcp_servers[]`` + resolution in the host."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from qai.platform.user_extensions import ExtensionStateStore, ManifestError, parse_manifest

from .conftest import FakeSecretStore, manifest

SETTINGS = [
    {"key": "region", "type": "select", "label": "R", "options": ["us", "eu"], "default": "eu"},
    {"key": "verbose", "type": "bool", "label": "V", "default": True},
    {"key": "token", "type": "secret", "label": "T"},
]


def _parse(servers: Any, settings: Any = SETTINGS):
    return parse_manifest(
        manifest(settings=settings, mcp_servers=servers), root=Path("/x/demo-ext")
    )


# -- manifest ------------------------------------------------------------------


def test_servers_parsed() -> None:
    m = _parse(
        [
            {
                "name": "local",
                "command": "${python}",
                "args": ["${root}/srv.py"],
                "env": {"A": "1"},
            },
            {
                "name": "remote",
                "transport": "http",
                "url": "https://x/${setting:region}",
                "headers": {"Authorization": "Bearer ${secret:token}"},
                "timeout_s": 5,
            },
        ]
    )
    local, remote = m.mcp_servers
    assert (local.transport, local.command, local.args, dict(local.env)) == (
        "stdio",
        "${python}",
        ("${root}/srv.py",),
        {"A": "1"},
    )
    assert remote.transport == "http" and remote.timeout_s == 5.0


@pytest.mark.parametrize(
    ("server", "match"),
    [
        ({"command": "x"}, "missing required field 'name'"),
        ({"name": "Bad_Name", "command": "x"}, "name"),
        ({"name": "a"}, "needs 'command'"),
        ({"name": "a", "transport": "http"}, "needs 'url'"),
        ({"name": "a", "transport": "ws", "url": "u"}, "stdio, http or sse"),
        ({"name": "a", "command": "x", "url": "u"}, "url is not valid for transport 'stdio'"),
        ({"name": "a", "transport": "sse", "url": "u", "args": []}, "args is not valid"),
        ({"name": "a", "command": "x", "args": [1]}, "array of strings"),
        ({"name": "a", "command": "x", "env": {"A": 1}}, "string values"),
        ({"name": "a", "command": "x", "timeout_s": 0}, "positive"),
        ({"name": "a", "command": "${home}"}, "unknown placeholder"),
        ({"name": "a", "command": "x", "env": {"T": "${setting:token}"}}, "non-secret"),
        ({"name": "a", "command": "x", "env": {"T": "${secret:region}"}}, "secret"),
        ({"name": "a", "command": "x", "env": {"T": "${setting:nope}"}}, "declared"),
        ({"name": "a", "command": "x", "autostart": True}, "unknown field"),
    ],
)
def test_bad_servers(server: dict[str, Any], match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        _parse([server])


def test_duplicate_server_name() -> None:
    with pytest.raises(ManifestError, match="declared twice"):
        _parse([{"name": "a", "command": "x"}, {"name": "a", "command": "y"}])


# -- host resolution ---------------------------------------------------------------


def _ext(make_ext, servers: Any) -> Path:
    return make_ext(
        "demo-ext", manifest_data=manifest("demo-ext", settings=SETTINGS, mcp_servers=servers)
    )


def test_placeholders_are_resolved(make_ext, new_host, tmp_path: Path) -> None:
    ext = _ext(
        make_ext,
        [
            {
                "name": "srv",
                "command": "${python}",
                "args": ["${root}/srv.py", "--data", "${data_dir}", "--v=${setting:verbose}"],
                "env": {"REGION": "${setting:region}", "TOKEN": "${secret:token}", "PLAIN": "p"},
            }
        ],
    )
    store = FakeSecretStore()
    store.set("ext.demo-ext", "token", "s3cret")
    state = ExtensionStateStore(tmp_path / "state.json")
    state.set_settings("demo-ext", {"region": "us"})
    host = new_host(secret_store=store, stored_settings=state.settings)
    (status,) = host.load_all()

    assert status.state == "loaded" and status.mcp_servers == ("demo-ext-srv",), status.warnings
    (srv,) = host.mcp_servers()
    data_dir = tmp_path / "data" / "extensions-data" / "demo-ext"
    assert srv.command == sys.executable
    assert srv.args == (f"{ext}/srv.py", "--data", str(data_dir), "--v=true")
    assert dict(srv.env) == {"REGION": "us", "TOKEN": "s3cret", "PLAIN": "p"}
    assert srv.secret_env_keys == ("TOKEN",)
    assert srv.cwd is None and data_dir.is_dir()


def test_a_missing_secret_skips_only_that_server(make_ext, new_host) -> None:
    _ext(
        make_ext,
        [
            {"name": "needs-token", "command": "x", "env": {"T": "${secret:token}"}},
            {"name": "plain", "command": "x"},
        ],
    )
    host = new_host(secret_store=FakeSecretStore())
    (status,) = host.load_all()
    assert status.state == "loaded"
    assert status.mcp_servers == ("demo-ext-plain",)
    assert any(
        "'needs-token' not started: secret setting 'token' is not set" in w for w in status.warnings
    )


def test_refresh_after_settings_change_and_unload(make_ext, new_host, tmp_path: Path) -> None:
    _ext(make_ext, [{"name": "srv", "command": "x", "env": {"T": "${secret:token}"}}])
    store = FakeSecretStore()
    host = new_host(secret_store=store)
    (status,) = host.load_all()
    assert host.mcp_servers() == ()

    store.set("ext.demo-ext", "token", "now-set")
    host.refresh_mcp("demo-ext")
    (srv,) = host.mcp_servers()
    assert srv.env["T"] == "now-set"
    assert not any("not started" in w for w in status.warnings)

    host.unload("demo-ext")
    assert host.mcp_servers() == ()


def test_disabled_extension_has_no_servers(make_ext, new_host) -> None:
    _ext(make_ext, [{"name": "srv", "command": "x"}])
    host = new_host(is_disabled=lambda _n: True)
    host.load_all()
    assert host.mcp_servers() == ()
