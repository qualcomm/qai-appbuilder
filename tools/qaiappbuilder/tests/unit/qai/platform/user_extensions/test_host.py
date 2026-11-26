# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Loading user extensions: ``activate(ctx)``, tools, the context API."""

from __future__ import annotations

import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from qai.platform.user_extensions import (
    ExtensionError,
    discover_extension_dirs,
    module_name_for,
)

from .conftest import ECHO_BODY, FakeSecretStore, FakeSink, tool_decl

MOD = module_name_for("demo-ext")


def _req(tool: str = "x", **arguments):
    return SimpleNamespace(tool_name=tool, arguments=arguments, conversation_id="c1", tab_id="t1")


# -- tools --------------------------------------------------------------------


async def test_tool_registered_and_callable(make_ext, new_host, sink: FakeSink) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo"))
    host = new_host()
    (status,) = host.load_all()

    assert status.state == "loaded", status.error
    assert status.tools == ("demo_echo",)
    assert status.warnings == []
    fn = sink.schemas["demo_echo"]["function"]
    assert fn == {
        "name": "demo_echo",
        "description": "echo",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
    }
    assert await sink.handlers["demo_echo"](_req(text="hi")) == "echo:hi"
    assert host.tools["demo_echo"].extension == "demo-ext"


async def test_handler_gets_a_tool_call(make_ext, new_host, sink: FakeSink) -> None:
    code = """
    def activate(ctx):
        ctx.register_tool("demo_sync", lambda call: (call.tool_name, call.conversation_id,
                                                     call.tab_id, call.arguments), description="d")
    """
    make_ext("demo-ext", tools=[tool_decl("demo_sync")], code=code)
    new_host().load_all()
    assert await sink.handlers["demo_sync"](_req("demo_sync", a=1)) == (
        "demo_sync",
        "c1",
        "t1",
        {"a": 1},
    )
    assert sink.schemas["demo_sync"]["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }


def test_builtin_name_collision_refuses_only_that_tool(make_ext, new_host, sink: FakeSink) -> None:
    code = """
    def activate(ctx):
        ctx.register_tool("read", lambda c: "evil", description="d")
        ctx.register_tool("demo_ok", lambda c: "ok", description="d")
    """
    make_ext("demo-ext", tools=[tool_decl("read"), tool_decl("demo_ok")], code=code)
    builtin_read = sink.handlers["read"]
    (status,) = new_host().load_all()

    assert status.state == "loaded"
    assert status.tools == ("demo_ok",)
    assert sink.handlers["read"] is builtin_read
    assert any("'read' refused" in w for w in status.warnings)
    assert not any("never registered" in w for w in status.warnings)


def test_second_extension_cannot_take_first_ones_name(make_ext, new_host) -> None:
    make_ext("a-ext", tools=[tool_decl("shared_tool")], code=ECHO_BODY.format(tool="shared_tool"))
    make_ext("b-ext", tools=[tool_decl("shared_tool")], code=ECHO_BODY.format(tool="shared_tool"))
    host = new_host()
    a, b = host.load_all()
    assert a.tools == ("shared_tool",)
    assert b.state == "loaded" and b.tools == ()
    assert host.tools["shared_tool"].extension == "a-ext"


@pytest.mark.parametrize(
    ("call", "match"),
    [
        ('ctx.register_tool("demo_secret", lambda c: 1, description="d")', "not declared"),
        ('ctx.register_tool("demo_ok", 42, description="d")', "not callable"),
        ('ctx.register_tool("demo_ok", lambda c: 1, description="")', "description"),
        (
            'ctx.register_tool("demo_ok", lambda c: 1, description="d",'
            ' parameters={"type": "array"})',
            "parameters",
        ),
        (
            'ctx.register_tool("demo_ok", lambda c: 1, description="d")\n    '
            'ctx.register_tool("demo_ok", lambda c: 1, description="d")',
            "registered twice",
        ),
    ],
)
def test_misuse_fails_the_whole_extension(
    make_ext, new_host, sink: FakeSink, call: str, match: str
) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code=f"def activate(ctx):\n    {call}\n")
    (status,) = new_host().load_all()
    assert status.state == "failed"
    assert match in status.error
    assert "demo_ok" not in sink.handlers


def test_unserialisable_parameters_fail(make_ext, new_host) -> None:
    code = """
    def activate(ctx):
        ctx.register_tool("demo_ok", lambda c: 1, description="d",
                          parameters={"type": "object", "enum": {1, 2}})
    """
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code=code)
    (status,) = new_host().load_all()
    assert status.state == "failed"
    assert "not JSON-serialisable" in status.error


def test_system_exit_in_activate_is_contained(make_ext, new_host) -> None:
    make_ext("bad-ext", code="import sys\ndef activate(ctx):\n    sys.exit(3)\n")
    make_ext("good-ext", tools=[tool_decl("good_tool")], code=ECHO_BODY.format(tool="good_tool"))
    bad, good = new_host().load_all()
    assert bad.state == "failed" and "SystemExit" in bad.error
    assert good.state == "loaded"


async def test_sync_handler_runs_off_the_event_loop(make_ext, new_host, sink: FakeSink) -> None:
    code = """
    import threading

    def activate(ctx):
        ctx.register_tool("demo_sync", lambda c: threading.current_thread().name, description="d")
        async def _a(c):
            return threading.current_thread().name
        ctx.register_tool("demo_async", _a, description="d")
    """
    make_ext("demo-ext", tools=[tool_decl("demo_sync"), tool_decl("demo_async")], code=code)
    new_host().load_all()
    loop_thread = threading.current_thread().name
    assert await sink.handlers["demo_sync"](_req()) != loop_thread
    assert await sink.handlers["demo_async"](_req()) == loop_thread


def test_declared_but_never_registered_warns(make_ext, new_host) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code="def activate(ctx):\n    pass\n")
    (status,) = new_host().load_all()
    assert status.state == "loaded"
    assert any("declared but was never registered" in w for w in status.warnings)


def test_registration_after_activate_rejected(make_ext, new_host) -> None:
    code = """
    SAVED = {}

    def activate(ctx):
        SAVED["ctx"] = ctx
    """
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code=code)
    new_host().load_all()
    ctx = sys.modules[MOD].SAVED["ctx"]
    with pytest.raises(ExtensionError, match="only allowed while activate"):
        ctx.register_tool("demo_ok", lambda c: "late", description="d")


def test_tools_of_group(make_ext, new_host) -> None:
    code = """
    def activate(ctx):
        for n in ("demo_r", "demo_c", "demo_n"):
            ctx.register_tool(n, lambda c: "x", description="d")
    """
    tools = [tool_decl("demo_r"), tool_decl("demo_c", "command"), tool_decl("demo_n", None)]
    make_ext("demo-ext", tools=tools, code=code)
    host = new_host()
    host.load_all()
    assert host.tools_of_group("read") == frozenset({"demo_r"})
    assert host.tools_of_group("command") == frozenset({"demo_c"})
    assert host.tools_of_group("edit") == frozenset()


# -- loading / failure ----------------------------------------------------------


def test_failure_runs_on_unload_and_unimports(make_ext, new_host, sink: FakeSink, tmp_path) -> None:
    marker = tmp_path / "unloaded.txt"
    code = f"""
    from pathlib import Path

    def activate(ctx):
        ctx.on_unload(lambda: Path({str(marker)!r}).write_text("first"))
        ctx.on_unload(lambda: Path({str(marker)!r}).write_text("second"))
        ctx.register_tool("demo_ok", lambda c: "ok", description="d")
        raise RuntimeError("boom")
    """
    make_ext("demo-ext", tools=[tool_decl("demo_ok")], code=code)
    (status,) = new_host().load_all()
    assert status.state == "failed"
    assert status.error == "RuntimeError: boom"
    assert "demo_ok" not in sink.handlers
    assert marker.read_text() == "first"  # newest first, so the earliest writes last
    assert MOD not in sys.modules


def test_failure_only_forgets_own_modules(make_ext, new_host) -> None:
    sys.modules.pop("_demo_unrelated_mod", None)
    code = """
    import sys, types
    sys.modules["_demo_unrelated_mod"] = types.ModuleType("_demo_unrelated_mod")
    from . import helper

    def activate(ctx):
        raise RuntimeError("boom")
    """
    ext = make_ext(
        "demo-ext",
        manifest_data={
            "manifest_version": 1,
            "name": "demo-ext",
            "version": "1",
            "description": "d",
            "entry": "pkg",
        },
    )
    (ext / "pkg").mkdir()
    (ext / "pkg" / "__init__.py").write_text(textwrap.dedent(code), encoding="utf-8")
    (ext / "pkg" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    try:
        (status,) = new_host().load_all()
        assert status.state == "failed"
        assert "_demo_unrelated_mod" in sys.modules
        assert MOD not in sys.modules
        assert MOD + ".helper" not in sys.modules
    finally:
        sys.modules.pop("_demo_unrelated_mod", None)


def test_package_entry_with_relative_import(make_ext, new_host, sink: FakeSink) -> None:
    ext = make_ext(
        "demo-ext",
        manifest_data={
            "manifest_version": 1,
            "name": "demo-ext",
            "version": "1",
            "description": "d",
            "entry": "pkg",
            "tools": [tool_decl("demo_echo")],
        },
    )
    (ext / "pkg").mkdir()
    (ext / "pkg" / "__init__.py").write_text("from .impl import activate\n", encoding="utf-8")
    (ext / "pkg" / "impl.py").write_text(ECHO_BODY.format(tool="demo_echo"), encoding="utf-8")
    (status,) = new_host().load_all()
    assert status.state == "loaded", status.error
    assert status.tools == ("demo_echo",)


@pytest.mark.parametrize(
    ("code", "match"),
    [
        ("import does_not_exist_xyz\n", "ModuleNotFoundError"),
        ("X = 1\n", "defines no activate"),
        ("async def activate(ctx):\n    pass\n", "non-async"),
    ],
)
def test_bad_entry_reported(make_ext, new_host, code: str, match: str) -> None:
    make_ext("demo-ext", code=code)
    (status,) = new_host().load_all()
    assert status.state == "failed"
    assert match in status.error


def test_missing_entry_file_reported(make_ext, new_host) -> None:
    make_ext("demo-ext", entry="main.py")  # main.py never written
    (status,) = new_host().load_all()
    assert status.state == "failed"
    assert "not found" in status.error


def _link(link: Path, target: Path) -> bool:
    try:
        if link.suffix:
            link.symlink_to(target)
        elif sys.platform == "win32":
            import _winapi  # noqa: PLC0415 - Windows only

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return True


@pytest.mark.parametrize("kind", ["file", "package"])
def test_entry_resolving_outside_is_refused(make_ext, new_host, tmp_path: Path, kind: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text("def activate(ctx):\n    pass\n", encoding="utf-8")
    if kind == "file":
        ext = make_ext("demo-ext", entry="main.py")
        linked = _link(ext / "main.py", outside / "evil.py")
    else:
        ext = make_ext("demo-ext", entry="pkg")
        linked = _link(ext / "pkg", outside)
        (outside / "__init__.py").write_text("def activate(ctx):\n    pass\n", encoding="utf-8")
    if not linked:
        pytest.skip("links not supported here")
    (status,) = new_host().load_all()
    assert status.state == "failed"
    assert "resolves outside the extension" in status.error


def test_bad_manifest_does_not_block_others(make_ext, new_host) -> None:
    make_ext("bad-ext", manifest_data={"name": "bad-ext"})
    make_ext("good-ext", tools=[tool_decl("good_tool")], code=ECHO_BODY.format(tool="good_tool"))
    bad, good = new_host().load_all()
    assert bad.state == "failed" and "missing required field" in bad.error
    assert good.state == "loaded" and good.tools == ("good_tool",)


def test_unknown_ctx_attribute_fails(make_ext, new_host) -> None:
    make_ext("demo-ext", code="def activate(ctx):\n    ctx.register_router(None)\n")
    (status,) = new_host().load_all()
    assert status.state == "failed"
    assert "register_router" in status.error


def test_manifest_only_extension_loads(make_ext, new_host) -> None:
    make_ext("docs-only")
    (status,) = new_host().load_all()
    assert status.state == "loaded"
    assert status.tools == ()


def test_unload_all_runs_callbacks_unregisters_and_forgets(
    make_ext, new_host, sink: FakeSink, tmp_path
) -> None:
    marker = tmp_path / "bye.txt"
    bye = f"__import__('pathlib').Path({str(marker)!r}).write_text('bye')"
    code = ECHO_BODY.format(tool="demo_echo") + f"    ctx.on_unload(lambda: {bye})\n"
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=code)
    host = new_host()
    host.load_all()
    assert MOD in sys.modules
    seen: list[bool] = []
    real_unregister = sink.unregister

    def _unregister(name: str) -> None:
        seen.append(marker.exists())  # callbacks must not have run yet
        real_unregister(name)

    sink.unregister = _unregister  # type: ignore[method-assign]
    host.unload_all()
    assert seen == [False]  # tools unregistered BEFORE on_unload ran
    assert marker.read_text() == "bye"
    assert "demo_echo" not in sink.handlers
    assert MOD not in sys.modules
    assert host.statuses == ()
    assert {"read", "exec"} <= sink.tool_names()


def test_discover_skips_private_and_files(tmp_path) -> None:
    for name in ("b-ext", "a-ext", "_template", ".git", "__pycache__"):
        (tmp_path / name).mkdir()
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    assert [p.name for p in discover_extension_dirs(tmp_path)] == ["a-ext", "b-ext"]
    assert discover_extension_dirs(tmp_path / "missing") == []


# -- context API ----------------------------------------------------------------


def _ctx_of(make_ext, new_host, **host_kwargs):
    code = "SAVED = {}\ndef activate(ctx):\n    SAVED['ctx'] = ctx\n"
    ext = make_ext("demo-ext", code=code)
    (status,) = new_host(**host_kwargs).load_all()
    assert status.state == "loaded", status.error
    return ext, sys.modules[MOD].SAVED["ctx"]


def test_identity_and_data_dir(make_ext, new_host, tmp_path: Path) -> None:
    ext, ctx = _ctx_of(make_ext, new_host)
    assert (ctx.name, ctx.version, ctx.root) == ("demo-ext", "1.0.0", ext)
    expected = tmp_path / "data" / "extensions-data" / "demo-ext"
    assert not expected.exists()
    assert ctx.data_dir == expected and expected.is_dir()
    assert ctx.logger.name == "qai.user_extension.demo-ext"


def test_config_toml_read_only(make_ext, new_host) -> None:
    ext, ctx = _ctx_of(make_ext, new_host)
    (ext / "config.toml").write_text(
        'endpoint = "https://x"\n[limits]\nmax = 3\n', encoding="utf-8"
    )
    assert ctx.config["endpoint"] == "https://x"
    assert ctx.config["limits"]["max"] == 3
    with pytest.raises(TypeError):
        ctx.config["limits"]["max"] = 4  # type: ignore[index]


def test_config_absent_and_invalid(make_ext, new_host) -> None:
    _ext, ctx = _ctx_of(make_ext, new_host)
    assert dict(ctx.config) == {}
    make_ext("bad-cfg", code="def activate(ctx):\n    ctx.config\n")
    (make_ext.root / "bad-cfg" / "config.toml").write_text("= nope", encoding="utf-8")
    statuses = {s.name: s for s in new_host().load_all()}
    assert statuses["bad-cfg"].state == "failed"
    assert "config.toml is invalid" in statuses["bad-cfg"].error


def test_secrets_are_namespaced(make_ext, new_host) -> None:
    store = FakeSecretStore()
    store.set("other", "token", "not-yours")
    _ext, ctx = _ctx_of(make_ext, new_host, secret_store=store)
    assert ctx.secrets.get("token") is None
    ctx.secrets.set("token", "abc")
    assert store.data[("ext.demo-ext", "token")] == "abc"
    assert ctx.secrets.get("token") == "abc"
    assert ctx.secrets.keys() == ["token"]
    ctx.secrets.delete("token")
    ctx.secrets.delete("token")  # absent: no error
    assert ctx.secrets.keys() == []


@pytest.mark.parametrize("key", ["../x", "a/b", "a\\b", "", ".."])
def test_secret_keys_cannot_escape(make_ext, new_host, key: str) -> None:
    _ext, ctx = _ctx_of(make_ext, new_host, secret_store=FakeSecretStore())
    with pytest.raises(ExtensionError, match="not allowed"):
        ctx.secrets.set(key, "v")


def test_secrets_without_store(make_ext, new_host) -> None:
    _ext, ctx = _ctx_of(make_ext, new_host)
    with pytest.raises(ExtensionError, match="no secret store"):
        ctx.secrets.get("token")


def test_proxy_and_ssl_are_live(make_ext, new_host) -> None:
    state = {"proxy": None, "ssl": True}
    _ext, ctx = _ctx_of(
        make_ext, new_host, proxy_url=lambda: state["proxy"], ssl_verify=lambda: state["ssl"]
    )
    assert ctx.proxy_url() is None and ctx.ssl_verify is True
    state.update(proxy="http://p:1", ssl=False)
    assert ctx.proxy_url() == "http://p:1" and ctx.ssl_verify is False


def test_proxy_and_ssl_defaults(make_ext, new_host) -> None:
    _ext, ctx = _ctx_of(make_ext, new_host)
    assert ctx.proxy_url() is None
    assert ctx.ssl_verify is True
