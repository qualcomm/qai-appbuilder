# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Enable / disable (persisted switches) and uninstall."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

from qai.platform.user_extensions import (
    ExtensionStateStore,
    UnknownExtensionError,
)

from .conftest import ECHO_BODY, FakeSecretStore, FakeSink, manifest, tool_decl


def _skill(ext: Path, skill_id: str) -> None:
    d = ext / "skills" / skill_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {skill_id}\ndescription: d\n---\n", encoding="utf-8")


# -- state store ----------------------------------------------------------------


def test_state_store_roundtrip(tmp_path: Path) -> None:
    store = ExtensionStateStore(tmp_path / "config" / "extensions.json")
    assert store.disabled() == frozenset()
    store.set_disabled("a-ext", True)
    store.set_disabled("b-ext", True)
    store.set_disabled("a-ext", False)
    store.set_settings("b-ext", {"endpoint": "x"})
    again = ExtensionStateStore(store.path)  # a new process reads the same file
    assert again.disabled() == frozenset({"b-ext"})
    assert again.settings("b-ext") == {"endpoint": "x"}
    again.forget("b-ext")
    assert again.disabled() == frozenset() and again.settings("b-ext") == {}
    assert not list(store.path.parent.glob(".extensions.*"))  # no temp files left


@pytest.mark.parametrize("content", ["{not json", "[]", '{"disabled": "a", "settings": 3}'])
def test_state_store_tolerates_a_bad_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "extensions.json"
    path.write_text(content, encoding="utf-8")
    store = ExtensionStateStore(path)
    assert store.disabled() == frozenset()
    assert store.settings("a") == {}


# -- disabled extensions ----------------------------------------------------------


def test_a_disabled_extension_loads_nothing(make_ext, new_host, sink: FakeSink, tmp_path) -> None:
    ext = make_ext(
        "demo-ext", tools=[tool_decl("demo_echo")], code="raise RuntimeError('never run')\n"
    )
    _skill(ext, "demo-skill")
    store = ExtensionStateStore(tmp_path / "state.json")
    store.set_disabled("demo-ext", True)

    host = new_host(is_disabled=store.is_disabled)
    (status,) = host.load_all()

    assert status.state == "disabled" and status.error == ""
    assert "demo_echo" not in sink.handlers
    assert host.skill_roots() == ()


def test_toggling_takes_effect_on_reload(make_ext, new_host, sink: FakeSink, tmp_path) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo"))
    store = ExtensionStateStore(tmp_path / "state.json")
    host = new_host(is_disabled=store.is_disabled)
    host.load_all()
    assert "demo_echo" in sink.handlers

    store.set_disabled("demo-ext", True)
    assert host.reload("demo-ext").state == "disabled"
    assert "demo_echo" not in sink.handlers

    store.set_disabled("demo-ext", False)
    assert host.reload("demo-ext").state == "loaded"
    assert "demo_echo" in sink.handlers


def test_load_all_does_not_retry_a_disabled_extension(make_ext, new_host, tmp_path) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo"))
    calls: list[str] = []

    def is_disabled(name: str) -> bool:
        calls.append(name)
        return True

    host = new_host(is_disabled=is_disabled)
    host.load_all()
    host.load_all()
    assert calls == ["demo-ext"]


# -- remove -------------------------------------------------------------------------


def test_remove_deletes_the_directory_and_keeps_data_by_default(
    make_ext, new_host, sink: FakeSink, tmp_path
) -> None:
    ext = make_ext(
        "demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo")
    )
    secrets = FakeSecretStore()
    secrets.set("ext.demo-ext", "token", "t")
    host = new_host(secret_store=secrets)
    host.load_all()
    data = tmp_path / "data" / "extensions-data" / "demo-ext"
    data.mkdir(parents=True)
    (data / "cache.json").write_text("{}", encoding="utf-8")

    host.remove("demo-ext")

    assert not ext.exists()
    assert "demo_echo" not in sink.handlers
    assert host.statuses == ()
    assert data.is_dir() and secrets.exists("ext.demo-ext", "token")


def test_remove_with_purge_deletes_data_and_secrets(make_ext, new_host, tmp_path) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo"))
    secrets = FakeSecretStore()
    secrets.set("ext.demo-ext", "token", "t")
    secrets.set("ext.other-ext", "token", "keep")
    host = new_host(secret_store=secrets)
    host.load_all()
    data = tmp_path / "data" / "extensions-data" / "demo-ext"
    data.mkdir(parents=True)

    host.remove("demo-ext", purge_data=True)

    assert not data.exists()
    assert secrets.list_keys("ext.demo-ext") == []
    assert secrets.get("ext.other-ext", "token") == "keep"


def test_remove_a_failed_extension(make_ext, new_host) -> None:
    ext = make_ext("demo-ext", manifest_data={"name": "demo-ext"})  # invalid manifest
    host = new_host()
    assert host.load_all()[0].state == "failed"
    host.remove("demo-ext")
    assert not ext.exists() and host.statuses == ()


@pytest.mark.parametrize("name", ["no-such-ext", "..", "../data", "a/b"])
def test_remove_refuses_unknown_or_escaping_names(new_host, tmp_path, name: str) -> None:
    (tmp_path / "data").mkdir(exist_ok=True)
    with pytest.raises(UnknownExtensionError):
        new_host().remove(name)
    assert (tmp_path / "data").is_dir()


def test_remove_unlinks_a_linked_directory_without_touching_the_target(
    make_ext, new_host, tmp_path
) -> None:
    target = tmp_path / "elsewhere" / "demo-ext"
    target.mkdir(parents=True)
    (target / "extension.json").write_text(json.dumps(manifest("demo-ext")), encoding="utf-8")
    (target / "keep.txt").write_text("x", encoding="utf-8")
    link = make_ext.root / "demo-ext"
    try:
        if os.name == "nt":
            importlib.import_module("_winapi").CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except (OSError, AttributeError):
        pytest.skip("directory links not supported here")
    host = new_host()
    host.load_all()

    host.remove("demo-ext")

    assert not link.exists()
    assert (target / "keep.txt").read_text() == "x"
