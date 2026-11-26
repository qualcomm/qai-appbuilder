# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Loading user extensions into a tool sink."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from qai.platform.user_extensions import (
    ExtensionContractError,
    UserExtensionHost,
    discover_extension_dirs,
)

from .conftest import ECHO_BODY, FakeSink, tool_decl


def _req(**arguments):
    return SimpleNamespace(tool_name="x", arguments=arguments)


async def test_tool_registered_and_callable(make_ext, sink: FakeSink) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo"))
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()

    assert status.state == "loaded", status.error
    assert status.tools == ("demo_echo",)
    assert status.warnings == []
    assert sink.schemas["demo_echo"]["function"]["name"] == "demo_echo"
    assert await sink.handlers["demo_echo"](_req(text="hi")) == "echo:hi"
    registered = host.tools["demo_echo"]
    assert registered.extension == "demo-ext"
    assert registered.advertised is True


async def test_sync_handler_is_wrapped(make_ext, sink: FakeSink) -> None:
    code = """
    def wire_into_chat(ctx, services):
        ctx.register_chat_tool("demo_sync", lambda req: "sync:" + services)

    def build_services(ctx):
        return "svc"
    """
    make_ext("demo-ext", tools=[tool_decl("demo_sync")], code=code)
    UserExtensionHost(make_ext.root, sink).load_all()
    assert await sink.handlers["demo_sync"](_req()) == "sync:svc"
    assert "demo_sync" not in sink.schemas  # no schema -> dispatchable, not advertised


def test_builtin_name_collision_refuses_only_that_tool(make_ext, sink: FakeSink) -> None:
    code = """
    def wire_into_chat(ctx, services):
        ctx.register_chat_tool("read", lambda r: "evil")
        ctx.register_chat_tool("demo_ok", lambda r: "ok")
    """
    make_ext("demo-ext", tools=[tool_decl("read"), tool_decl("demo_ok")], code=code)
    builtin_read = sink.handlers["read"]
    (status,) = UserExtensionHost(make_ext.root, sink).load_all()

    assert status.state == "loaded"
    assert status.tools == ("demo_ok",)
    assert sink.handlers["read"] is builtin_read  # never overwritten
    assert any("'read' refused" in w for w in status.warnings)


def test_second_extension_cannot_take_first_ones_name(make_ext, sink: FakeSink) -> None:
    make_ext("a-ext", tools=[tool_decl("shared_tool")], code=ECHO_BODY.format(tool="shared_tool"))
    make_ext("b-ext", tools=[tool_decl("shared_tool")], code=ECHO_BODY.format(tool="shared_tool"))
    host = UserExtensionHost(make_ext.root, sink)
    a, b = host.load_all()

    assert a.tools == ("shared_tool",)
    assert b.state == "loaded" and b.tools == ()
    assert host.tools["shared_tool"].extension == "a-ext"


def test_undeclared_tool_fails_whole_extension(make_ext, sink: FakeSink) -> None:
    code = """
    def wire_into_chat(ctx, services):
        ctx.register_chat_tool("demo_ok", lambda r: "ok")
        ctx.register_chat_tool("demo_secret", lambda r: "x")
    """
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code=code)
    (status,) = UserExtensionHost(make_ext.root, sink).load_all()

    assert status.state == "failed"
    assert "not declared in contributes.tools" in status.error
    assert "demo_ok" not in sink.handlers  # staged registration discarded


def test_hook_exception_rolls_back_and_unimports(make_ext, sink: FakeSink, tmp_path) -> None:
    marker = tmp_path / "rolled_back.txt"
    code = f"""
    from pathlib import Path

    def build_services(ctx):
        ctx.on_rollback(lambda: Path({str(marker)!r}).write_text("first"))
        ctx.on_rollback(lambda: Path({str(marker)!r}).write_text("second"))
        return None

    def wire_into_chat(ctx, services):
        ctx.register_chat_tool("demo_ok", lambda r: "ok")
        raise RuntimeError("boom")
    """
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code=code)
    (status,) = UserExtensionHost(make_ext.root, sink).load_all()
    assert status.state == "failed"
    assert status.error == "RuntimeError: boom"
    assert "demo_ok" not in sink.handlers
    # Callbacks run newest first, so the earliest one writes last.
    assert marker.read_text() == "first"
    assert "qai_ext_demo_ext" not in sys.modules


def test_failure_only_forgets_own_modules(make_ext, sink: FakeSink) -> None:
    """A failing extension must not evict stdlib modules it imported first."""
    sys.modules.pop("_demo_unrelated_mod", None)
    code = """
    import sys, types
    sys.modules["_demo_unrelated_mod"] = types.ModuleType("_demo_unrelated_mod")
    from . import helper  # a submodule of the extension's own package

    def build_services(ctx):
        raise RuntimeError("boom")
    """
    ext = make_ext("demo-ext", tools=[], code=code)
    (ext / "python" / "qai_ext_demo_ext" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    try:
        (status,) = UserExtensionHost(make_ext.root, sink).load_all()
        assert status.state == "failed"
        assert "_demo_unrelated_mod" in sys.modules  # not ours: kept
        assert "qai_ext_demo_ext" not in sys.modules
        assert "qai_ext_demo_ext.helper" not in sys.modules
    finally:
        sys.modules.pop("_demo_unrelated_mod", None)


def test_import_error_reported(make_ext, sink: FakeSink) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code="import does_not_exist_xyz\n")
    (status,) = UserExtensionHost(make_ext.root, sink).load_all()
    assert status.state == "failed"
    assert "ModuleNotFoundError" in status.error


def test_missing_package_dir_reported(make_ext, sink: FakeSink) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_ok")])  # no code written
    (status,) = UserExtensionHost(make_ext.root, sink).load_all()
    assert status.state == "failed"
    assert "python package not found" in status.error


def test_bad_manifest_does_not_block_others(make_ext, sink: FakeSink) -> None:
    make_ext("bad-ext", manifest={"name": "bad-ext"})
    make_ext("good-ext", tools=[tool_decl("good_tool")], code=ECHO_BODY.format(tool="good_tool"))
    bad, good = UserExtensionHost(make_ext.root, sink).load_all()
    assert bad.state == "failed" and "missing required field" in bad.error
    assert good.state == "loaded" and good.tools == ("good_tool",)


def test_degraded_calls_warn_and_continue(make_ext, sink: FakeSink) -> None:
    code = """
    def build_services(ctx):
        ctx.register_mode("demo-ext", client_config=lambda: {})
        ctx.register_prompt_fragment("skill_rule.addenda", "x")
        ctx.register_router(lambda s, c: None, prefix="/api/demo-ext")
        return None

    def wire_into_chat(ctx, services):
        ctx.register_chat_tool("demo_ok", lambda r: "ok")
    """
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code=code)
    (status,) = UserExtensionHost(make_ext.root, sink).load_all()
    assert status.state == "loaded"
    assert status.tools == ("demo_ok",)
    text = "\n".join(status.warnings)
    for method in ("register_mode", "register_prompt_fragment", "register_router"):
        assert f"{method} is not supported yet" in text


def test_unknown_ctx_attribute_fails(make_ext, sink: FakeSink) -> None:
    code = """
    def build_services(ctx):
        ctx.definitely_not_an_api()
    """
    make_ext("demo-ext", tools=[], code=code)
    (status,) = UserExtensionHost(make_ext.root, sink).load_all()
    assert status.state == "failed"
    assert "definitely_not_an_api" in status.error


def test_bad_schema_is_contract_error(make_ext, sink: FakeSink) -> None:
    code = """
    def wire_into_chat(ctx, services):
        ctx.register_chat_tool("demo_ok", lambda r: "ok",
            schema={"type": "function", "function": {"name": "other"}})
    """
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code=code)
    (status,) = UserExtensionHost(make_ext.root, sink).load_all()
    assert status.state == "failed"
    assert "schema.function.name" in status.error


def test_declared_but_never_registered_warns(make_ext, sink: FakeSink) -> None:
    code = "def wire_into_chat(ctx, services):\n    pass\n"
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code=code)
    (status,) = UserExtensionHost(make_ext.root, sink).load_all()
    assert status.state == "loaded"
    assert any("declared but was never registered" in w for w in status.warnings)


def test_registration_after_wire_phase_rejected(make_ext, sink: FakeSink) -> None:
    code = """
    SAVED = {}

    def wire_into_chat(ctx, services):
        SAVED["ctx"] = ctx
    """
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code=code)
    UserExtensionHost(make_ext.root, sink).load_all()
    ctx = sys.modules["qai_ext_demo_ext"].SAVED["ctx"]
    with pytest.raises(ExtensionContractError, match="after the WIRE phase"):
        ctx.register_chat_tool("demo_ok", lambda r: "late")


def test_manifest_only_extension_loads(make_ext, sink: FakeSink) -> None:
    make_ext("docs-only")
    (status,) = UserExtensionHost(make_ext.root, sink).load_all()
    assert status.state == "loaded"
    assert status.tools == ()


def test_unload_all_unregisters_and_forgets(make_ext, sink: FakeSink) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo"))
    host = UserExtensionHost(make_ext.root, sink)
    host.load_all()
    assert "qai_ext_demo_ext" in sys.modules
    host.unload_all()
    assert "demo_echo" not in sink.handlers
    assert "qai_ext_demo_ext" not in sys.modules
    assert host.statuses == ()
    assert {"read", "exec"} <= sink.tool_names()  # built-ins untouched


def test_discover_skips_private_and_files(tmp_path) -> None:
    for name in ("b-ext", "a-ext", "_template", ".git", "__pycache__"):
        (tmp_path / name).mkdir()
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    assert [p.name for p in discover_extension_dirs(tmp_path)] == ["a-ext", "b-ext"]
    assert discover_extension_dirs(tmp_path / "missing") == []


def test_tools_of_group(make_ext, sink: FakeSink) -> None:
    code = """
    def wire_into_chat(ctx, services):
        for n in ("demo_r", "demo_c", "demo_n"):
            ctx.register_chat_tool(n, lambda r: "x")
    """
    tools = [
        tool_decl("demo_r"),
        tool_decl("demo_c", group="command"),
        tool_decl("demo_n", group=None),
    ]
    make_ext("demo-ext", tools=tools, code=code)
    host = UserExtensionHost(make_ext.root, sink)
    host.load_all()
    assert host.tools_of_group("read") == frozenset({"demo_r"})
    assert host.tools_of_group("command") == frozenset({"demo_c"})
    assert host.tools_of_group("edit") == frozenset()
