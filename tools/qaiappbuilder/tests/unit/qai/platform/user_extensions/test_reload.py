# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Unloading and reloading extensions at runtime; reserved tool names."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from qai.chat.adapters import RegistryBackedToolInvocation
from qai.platform.user_extensions import UnknownExtensionError, module_name_for

from .conftest import ECHO_BODY, FakeSink, manifest, tool_decl


def _req(tool: str, **arguments):
    return SimpleNamespace(tool_name=tool, arguments=arguments, conversation_id="c", tab_id="t")


def _code(reply: str, *, tool: str = "demo_echo", marker: Path | None = None) -> str:
    unload = (
        f"    ctx.on_unload(lambda: open({str(marker)!r}, 'a').write('{reply};'))\n"
        if marker
        else ""
    )
    return (
        "def activate(ctx):\n"
        f"    ctx.register_tool({tool!r}, lambda call: {reply!r}, description='d')\n" + unload
    )


def _rewrite(ext: Path, code: str) -> None:
    (ext / "main.py").write_text(textwrap.dedent(code), encoding="utf-8")


def _skill(ext: Path, skill_id: str) -> None:
    d = ext / "skills" / skill_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {skill_id}\ndescription: d\n---\n", encoding="utf-8")


async def test_reload_picks_up_changed_code(make_ext, new_host, sink: FakeSink, tmp_path) -> None:
    """Same-size edit in the same second: must not run a stale cached .pyc."""
    marker = tmp_path / "unloaded.txt"
    ext = make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=_code("v1", marker=marker))
    host = new_host()
    host.load_all()
    assert await sink.handlers["demo_echo"](_req("demo_echo")) == "v1"

    _rewrite(ext, _code("v2", marker=marker))
    status = host.reload("demo-ext")

    assert status.state == "loaded", status.error
    assert await sink.handlers["demo_echo"](_req("demo_echo")) == "v2"
    assert marker.read_text() == "v1;"  # the OLD instance's on_unload ran, once
    assert host.status("demo-ext") is status


async def test_reload_picks_up_a_changed_submodule(make_ext, new_host, sink: FakeSink) -> None:
    """Package entries: submodules are recompiled too (same-size, same-second edit)."""
    data = manifest("demo-ext", entry="pkg", tools=[tool_decl("demo_echo")])
    ext = make_ext("demo-ext", manifest_data=data)
    (ext / "pkg").mkdir()
    (ext / "pkg" / "__init__.py").write_text(
        "from .impl import reply\n"
        "def activate(ctx):\n"
        "    ctx.register_tool('demo_echo', lambda c: reply(), description='d')\n",
        encoding="utf-8",
    )
    impl = ext / "pkg" / "impl.py"
    impl.write_text("def reply():\n    return 'v1'\n", encoding="utf-8")
    host = new_host()
    host.load_all()
    assert await sink.handlers["demo_echo"](_req("demo_echo")) == "v1"

    impl.write_text("def reply():\n    return 'v2'\n", encoding="utf-8")
    host.reload("demo-ext")

    assert await sink.handlers["demo_echo"](_req("demo_echo")) == "v2"
    assert module_name_for("demo-ext") + ".impl" in sys.modules
    host.unload("demo-ext")
    assert module_name_for("demo-ext") + ".impl" not in sys.modules


def test_reload_picks_up_a_changed_manifest(make_ext, new_host, sink: FakeSink) -> None:
    code = (
        "def activate(ctx):\n    for n in ['demo_a', 'demo_b']:\n"
        "        ctx.register_tool(n, lambda c: n, description='d')\n"
    )
    ext = make_ext("demo-ext", tools=[tool_decl("demo_a")], code=code.replace(", 'demo_b'", ""))
    host = new_host()
    host.load_all()
    assert "demo_a" in sink.handlers

    data = manifest("demo-ext", entry="main.py", tools=[tool_decl("demo_b")])
    (ext / "extension.json").write_text(json.dumps(data), encoding="utf-8")
    _rewrite(ext, code.replace("'demo_a', ", ""))
    host.reload("demo-ext")

    assert "demo_a" not in sink.handlers
    assert "demo_b" in sink.handlers


def test_failed_reload_leaves_the_extension_unloaded(make_ext, new_host, sink: FakeSink) -> None:
    ext = make_ext(
        "demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo")
    )
    host = new_host()
    host.load_all()
    _rewrite(ext, "def activate(ctx):\n    raise RuntimeError('broken')\n")

    status = host.reload("demo-ext")

    assert status.state == "failed" and "broken" in status.error
    assert "demo_echo" not in sink.handlers
    assert host.tools == {}
    assert module_name_for("demo-ext") not in sys.modules


def test_a_broken_extension_can_be_fixed_and_reloaded(make_ext, new_host, sink: FakeSink) -> None:
    ext = make_ext("demo-ext", tools=[tool_decl("demo_echo")], code="raise RuntimeError('x')\n")
    host = new_host()
    (first,) = host.load_all()
    assert first.state == "failed"
    _rewrite(ext, ECHO_BODY.format(tool="demo_echo"))
    assert host.reload("demo-ext").state == "loaded"
    assert "demo_echo" in sink.handlers


def test_reload_does_not_touch_other_extensions(make_ext, new_host, sink: FakeSink) -> None:
    make_ext("a-ext", tools=[tool_decl("a_tool")], code=ECHO_BODY.format(tool="a_tool"))
    make_ext("b-ext", tools=[tool_decl("b_tool")], code=ECHO_BODY.format(tool="b_tool"))
    host = new_host()
    host.load_all()
    a_handler = sink.handlers["a_tool"]

    host.reload("b-ext")

    assert sink.handlers["a_tool"] is a_handler
    assert {s.name for s in host.statuses} == {"a-ext", "b-ext"}


def test_reload_unknown_name_raises(new_host) -> None:
    host = new_host()
    with pytest.raises(UnknownExtensionError):
        host.reload("no-such-ext")


def test_reload_of_a_deleted_directory_unloads_it(make_ext, new_host, sink: FakeSink) -> None:
    ext = make_ext(
        "demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo")
    )
    host = new_host()
    host.load_all()
    shutil.rmtree(ext)

    status = host.reload("demo-ext")

    assert status.state == "failed" and "no longer exists" in status.error
    assert "demo_echo" not in sink.handlers


def test_reload_all_adds_new_and_drops_removed(make_ext, new_host, sink: FakeSink) -> None:
    old = make_ext("old-ext", tools=[tool_decl("old_tool")], code=ECHO_BODY.format(tool="old_tool"))
    host = new_host()
    host.load_all()
    shutil.rmtree(old)
    make_ext("new-ext", tools=[tool_decl("new_tool")], code=ECHO_BODY.format(tool="new_tool"))

    statuses = host.reload_all()

    assert [s.name for s in statuses] == ["new-ext"]
    assert "old_tool" not in sink.handlers
    assert "new_tool" in sink.handlers


def test_load_all_twice_does_not_duplicate(make_ext, new_host, sink: FakeSink, tmp_path) -> None:
    marker = tmp_path / "unloaded.txt"
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=_code("v1", marker=marker))
    host = new_host()
    host.load_all()
    (status,) = host.load_all()
    assert status.state == "loaded" and status.warnings == []
    assert not marker.exists()


def test_reload_updates_skills(make_ext, new_host) -> None:
    ext = make_ext("docs-ext", manifest_data=manifest("docs-ext"))
    _skill(ext, "first")
    host = new_host()
    host.load_all()
    assert host.skill_roots()[0].skill_ids == ("first",)

    _skill(ext, "second")
    host.reload("docs-ext")
    assert host.skill_roots()[0].skill_ids == ("first", "second")


def test_unload_removes_skills_and_status(make_ext, new_host) -> None:
    ext = make_ext("docs-ext", manifest_data=manifest("docs-ext"))
    _skill(ext, "first")
    host = new_host()
    host.load_all()
    assert host.unload("docs-ext") is True
    assert host.skill_roots() == ()
    assert host.statuses == ()
    assert host.unload("docs-ext") is False


# -- ownership check on unregister --------------------------------------------


def test_unload_leaves_a_tool_someone_else_registered_over_ours(
    make_ext, new_host, sink: FakeSink
) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo"))
    host = new_host()
    host.load_all()
    theirs = object()
    sink.handlers["demo_echo"] = theirs  # e.g. a built-in registered later

    host.unload("demo-ext")

    assert sink.handlers["demo_echo"] is theirs


# -- reserved names -----------------------------------------------------------


def test_reserved_names_are_refused_even_when_not_registered(
    make_ext, new_host, sink: FakeSink
) -> None:
    code = """
    def activate(ctx):
        ctx.register_tool("computer", lambda c: "evil", description="d")
        ctx.register_tool("demo_ok", lambda c: "ok", description="d")
    """
    make_ext("demo-ext", tools=[tool_decl("computer"), tool_decl("demo_ok")], code=code)
    assert "computer" not in sink.handlers  # the built-in is disabled right now

    (status,) = new_host(reserved_tool_names=frozenset({"computer"})).load_all()

    assert status.tools == ("demo_ok",)
    assert "computer" not in sink.handlers
    assert any("'computer' refused" in w for w in status.warnings)


# -- review fixes ---------------------------------------------------------------


def test_group_hook_runs_before_register_and_after_unregister(
    make_ext, new_host, sink: FakeSink
) -> None:
    events: list[str] = []
    real_register, real_unregister = sink.register, sink.unregister

    def register(name, handler, *, schema):
        events.append(f"register:{name}")
        real_register(name, handler, schema=schema)

    def unregister(name):
        events.append(f"unregister:{name}")
        real_unregister(name)

    sink.register, sink.unregister = register, unregister  # type: ignore[method-assign]
    make_ext(
        "demo-ext",
        tools=[tool_decl("demo_echo", "command")],
        code=ECHO_BODY.format(tool="demo_echo"),
    )
    host = new_host(
        on_tool_added=lambda n, g: events.append(f"join:{n}:{g}"),
        on_tool_removed=lambda n: events.append(f"leave:{n}"),
    )
    host.load_all()
    host.unload("demo-ext")
    assert events == [
        "join:demo_echo:command",
        "register:demo_echo",
        "unregister:demo_echo",
        "leave:demo_echo",
    ]


def test_a_lazily_imported_submodule_is_forgotten_on_unload(
    make_ext, new_host, sink: FakeSink
) -> None:
    data = manifest("demo-ext", entry="pkg", tools=[tool_decl("demo_echo")])
    ext = make_ext("demo-ext", manifest_data=data)
    (ext / "pkg").mkdir()
    (ext / "pkg" / "__init__.py").write_text(
        "def activate(ctx):\n"
        "    def run(call):\n"
        "        from .lazy import VALUE\n"
        "        return VALUE\n"
        "    ctx.register_tool('demo_echo', run, description='d')\n",
        encoding="utf-8",
    )
    (ext / "pkg" / "lazy.py").write_text("VALUE = 'v1'\n", encoding="utf-8")
    host = new_host()
    host.load_all()
    assert asyncio.run(sink.handlers["demo_echo"](_req("demo_echo"))) == "v1"
    lazy = module_name_for("demo-ext") + ".lazy"
    assert lazy in sys.modules  # imported AFTER activate returned

    (ext / "pkg" / "lazy.py").write_text("VALUE = 'v2'\n", encoding="utf-8")
    host.reload("demo-ext")
    assert asyncio.run(sink.handlers["demo_echo"](_req("demo_echo"))) == "v2"


def test_a_host_side_failure_keeps_the_never_raises_contract(
    make_ext, new_host, sink: FakeSink
) -> None:
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=ECHO_BODY.format(tool="demo_echo"))

    def broken_scan() -> frozenset[str]:
        raise OSError("disk gone")

    host = new_host(taken_skill_ids=broken_scan)
    (status,) = host.load_all()
    assert status.state == "failed" and "host error" in status.error and "disk gone" in status.error
    assert "demo_echo" not in sink.handlers  # committed tool rolled back


def test_a_failed_directory_is_retried_by_load_all(make_ext, new_host) -> None:
    ext = make_ext("demo-ext", tools=[tool_decl("demo_echo")], code="raise RuntimeError('x')\n")
    host = new_host()
    assert host.load_all()[0].state == "failed"
    _rewrite(ext, ECHO_BODY.format(tool="demo_echo"))
    assert host.load_all()[0].state == "loaded"


def test_on_unload_system_exit_is_contained(make_ext, new_host, sink: FakeSink) -> None:
    code = (
        ECHO_BODY.format(tool="demo_echo")
        + "    ctx.on_unload(lambda: (_ for _ in ()).throw(SystemExit(1)))\n"
    )
    make_ext("demo-ext", tools=[tool_decl("demo_echo")], code=code)
    host = new_host()
    host.load_all()
    host.unload("demo-ext")  # must not raise SystemExit
    assert "demo_echo" not in sink.handlers


class _PopsDuringIteration(dict):
    """A dict whose iteration (``sorted(d)``) removes an entry mid-way: the
    exact interleaving of a reload in another thread, made deterministic."""

    def __iter__(self):
        keys = list(super().__iter__())
        for i, k in enumerate(keys):
            if i == 1:
                super().pop(keys[-1], None)
            yield k


@pytest.mark.parametrize("attr", ["_skill_roots", "_statuses"])
def test_reads_survive_an_entry_vanishing_mid_read(make_ext, new_host, attr: str) -> None:
    for i in range(3):
        ext = make_ext(f"e{i}-ext", manifest_data=manifest(f"e{i}-ext"))
        _skill(ext, f"skill-{i}")
    host = new_host()
    host.load_all()
    setattr(host, attr, _PopsDuringIteration(getattr(host, attr)))
    # Snapshot first (dict(d) copies without going through __iter__), so no KeyError.
    host.skill_roots()
    host.statuses  # noqa: B018
    host.skill_default_modes()


def test_registry_schemas_survive_a_tool_vanishing_mid_read() -> None:
    async def h(_r):
        return None

    reg = RegistryBackedToolInvocation()
    for n in ("read", "x_one", "x_two"):
        reg.register(n, h, schema={"type": "function", "function": {"name": n}})
    reg._schemas = _PopsDuringIteration(reg._schemas)
    assert len(reg.schemas()) == 3


def test_reads_work_on_snapshots_while_mutating(make_ext, new_host) -> None:
    """``skill_roots`` / ``statuses`` are read from the event loop while a reload runs."""
    for i in range(5):
        ext = make_ext(f"e{i}-ext", manifest_data=manifest(f"e{i}-ext"))
        _skill(ext, f"skill-{i}")
    host = new_host()
    host.load_all()
    errors: list[BaseException] = []
    stop = threading.Event()
    reads = [0]

    def reader() -> None:
        while not stop.is_set():
            try:
                host.skill_roots()
                host.statuses  # noqa: B018
                host.skill_default_modes()
                reads[0] += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return
            stop.wait(0.0005)  # yield the GIL to the reloading thread

    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(200):
            host.reload_all()
    finally:
        stop.set()
        t.join()
    assert errors == []
    assert reads[0] > 20  # the reader really ran concurrently
