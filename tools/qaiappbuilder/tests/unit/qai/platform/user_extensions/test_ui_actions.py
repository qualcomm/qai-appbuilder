# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Declarative UI (route B): manifest ``ui``, ``ctx.register_action``, ``run_action``."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from qai.platform.user_extensions import (
    ActionResult,
    ActionValidationError,
    ExtensionError,
    ManifestError,
    module_name_for,
    parse_manifest,
    run_action,
)

from .conftest import manifest

UI = {
    "label": "Remote Host",
    "icon": "🖥",
    "actions": [
        {
            "id": "authorize",
            "label": "Authorize host",
            "fields": [
                {"key": "host", "type": "text", "label": "Host", "required": True},
                {"key": "password", "type": "secret", "label": "Password"},
                {
                    "key": "port",
                    "type": "number",
                    "label": "Port",
                    "min": 1,
                    "max": 65535,
                    "default": 22,
                },
            ],
        },
        {"id": "status", "label": "Status"},
    ],
}

CODE = """
from qai.ext import ActionResult

SEEN = []

def activate(ctx):
    def authorize(call):
        SEEN.append(call)
        host = call.values["host"]
        return ActionResult(message="authorized " + host, prompt="check " + host)
    ctx.register_action("authorize", authorize)
"""


def _parse(ui: Any, **kw: Any):
    return parse_manifest(manifest(ui=ui, entry="main.py", **kw), root=Path("/x/demo-ext"))


def test_ui_parsed() -> None:
    m = _parse(UI)
    assert m.ui is not None and m.ui.label == "Remote Host" and m.ui.icon == "🖥"
    auth = m.ui.action("authorize")
    assert auth is not None and [f.key for f in auth.fields] == ["host", "password", "port"]
    assert m.ui.action("nope") is None


@pytest.mark.parametrize(
    ("ui", "match"),
    [
        ({}, "missing required field 'label'"),
        ({"label": "x", "icon": "too-long-icon"}, "ui.icon"),
        ({"label": "x", "actions": {}}, "must be an array"),
        ({"label": "x", "actions": [{"label": "y"}]}, "'id'"),
        ({"label": "x", "actions": [{"id": "Bad", "label": "y"}]}, "id"),
        (
            {"label": "x", "actions": [{"id": "a", "label": "y"}, {"id": "a", "label": "z"}]},
            "twice",
        ),
        (
            {
                "label": "x",
                "actions": [
                    {"id": "a", "label": "y", "fields": [{"key": "k", "type": "rgb", "label": "l"}]}
                ],
            },
            "type",
        ),
        ({"label": "x", "actions": [{"id": "a", "label": "y", "onclick": "z"}]}, "unknown field"),
        ({"label": "x", "page": "index.html"}, "unknown field"),
        ({"label": "x", "actions": [{"id": f"a{i}", "label": "y"} for i in range(9)]}, "at most"),
    ],
)
def test_bad_ui(ui: Any, match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        _parse(ui)


def test_actions_need_an_entry() -> None:
    with pytest.raises(ManifestError, match=r"ui\.actions requires entry"):
        parse_manifest(manifest(ui=UI), root=Path("/x/demo-ext"))
    # A mode without actions (label only, e.g. just an "active" prompt) needs no code.
    m = parse_manifest(manifest(ui={"label": "Docs"}), root=Path("/x/demo-ext"))
    assert m.ui is not None and m.ui.actions == ()


def test_ui_entries_list_only_handled_actions(make_ext, new_host) -> None:
    make_ext("demo-ext", manifest_data=manifest("demo-ext", entry="main.py", ui=UI), code=CODE)
    host = new_host()
    (status,) = host.load_all()
    assert any("'status' is declared but has no handler" in w for w in status.warnings)
    (entry,) = host.ui_entries()
    assert entry["name"] == "demo-ext" and entry["label"] == "Remote Host"
    assert [a["id"] for a in entry["actions"]] == ["authorize"]
    fields = {f["key"]: f for f in entry["actions"][0]["fields"]}
    assert "default" not in fields["password"] and fields["port"]["default"] == 22
    assert host.action_handler("demo-ext", "status") is None
    host.unload("demo-ext")
    assert host.ui_entries() == [] and host.action_handler("demo-ext", "authorize") is None


def test_register_action_misuse_fails_the_load(make_ext, new_host) -> None:
    make_ext(
        "demo-ext",
        manifest_data=manifest("demo-ext", entry="main.py", ui=UI),
        code="def activate(ctx):\n    ctx.register_action('undeclared', lambda c: None)\n",
    )
    (status,) = new_host().load_all()
    assert status.state == "failed" and "not declared in the manifest's ui.actions" in status.error


def test_run_action_validates_then_calls(make_ext, new_host) -> None:
    make_ext("demo-ext", manifest_data=manifest("demo-ext", entry="main.py", ui=UI), code=CODE)
    host = new_host()
    host.load_all()
    decl, handler = host.action_handler("demo-ext", "authorize")

    with pytest.raises(ActionValidationError) as exc:
        asyncio.run(run_action(decl, handler, {"port": 0, "extra": 1}))
    assert set(exc.value.errors) == {"host", "port", "extra"}
    assert sys.modules[module_name_for("demo-ext")].SEEN == []  # never called

    result = asyncio.run(
        run_action(decl, handler, {"host": "h1", "password": "pw"}, conversation_id="c", tab_id="t")
    )
    assert result == ActionResult(message="authorized h1", prompt="check h1")
    (call,) = sys.modules[module_name_for("demo-ext")].SEEN
    assert call.values == {"host": "h1", "password": "pw", "port": 22}
    assert (call.conversation_id, call.tab_id, call.action_id) == ("c", "t", "authorize")


@pytest.mark.parametrize(
    ("ret", "expected"),
    [(None, ActionResult()), ("done", ActionResult(message="done"))],
)
def test_run_action_return_shapes(ret: Any, expected: ActionResult) -> None:
    decl = _parse({"label": "x", "actions": [{"id": "a", "label": "y"}]}).ui.action("a")

    async def handler(_call):
        return ret

    assert asyncio.run(run_action(decl, handler, {})) == expected


def test_run_action_rejects_other_return_types() -> None:
    decl = _parse({"label": "x", "actions": [{"id": "a", "label": "y"}]}).ui.action("a")
    with pytest.raises(ExtensionError, match="returned int"):
        asyncio.run(run_action(decl, lambda _c: 42, {}))
