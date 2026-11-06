# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""MCP servers share one Python environment by default (and can opt out).

Why: servers that depend on each other (one imports a package another server ships)
cannot work from separate venvs, and vendors document a single ``pip install`` into
ONE Python. The danger of sharing is a new install silently re-versioning something
another server relies on, so installing into an environment that already has members
keeps the installed versions fixed: a conflict FAILS rather than breaking a neighbour.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qai.chat.adapters import mcp_wheel_installer as inst
from qai.chat.adapters.mcp_manage_tool import MCP_MANAGE_TOOL_SCHEMA, McpManageToolHandler
from qai.chat.adapters.mcp_wheel_installer import WheelInstallResult, install_wheel_as_mcp
from tests.unit.qai.chat.test_mcp_extra_wheels import _write_wheel
from tests.unit.qai.chat.test_mcp_wheel_installer import _FakeRunner, _make_wheel


def _run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=300))


def _install(tmp_path: Path, wheel: Path, name: str, runner: Any, **kw: Any) -> WheelInstallResult:
    return _run(install_wheel_as_mcp(wheel, server_name=name, data_dir=tmp_path, runner=runner, uv_path=kw.pop("uv", None), **kw))


def _app(tmp_path: Path, name: str, **kw: Any) -> Path:
    return _make_wheel(tmp_path, name=name, scripts={f"{name}-server": f"{name}:main"}, **kw)


def _venv_cmds(runner: _FakeRunner) -> list[list[str]]:
    return [c for c in runner.cmds if c[1:3] == ["-m", "venv"]]


def _pip_installs(runner: _FakeRunner) -> list[list[str]]:
    return [c for c in runner.cmds if c[1:4] == ["-m", "pip", "install"]]


class _EnvRunner(_FakeRunner):
    """Fake runner whose ``pip freeze --local`` (the environment's own packages) is scripted."""

    def __init__(self, *a: Any, env_freeze: str = "", unhealthy_once: bool = False, **k: Any) -> None:
        super().__init__(*a, **k)
        self.env_freeze = env_freeze
        self._unhealthy_once = unhealthy_once

    def __call__(self, cmd: list[str], timeout: int) -> tuple[int, str]:
        if cmd[1:6] == ["-m", "pip", "freeze", "--local", "--all"]:
            self.cmds.append(cmd)
            return 0, self.env_freeze
        if cmd[1:3] == ["-c", "import sys"] and self._unhealthy_once:
            self._unhealthy_once = False
            self.cmds.append(cmd)
            return 103, "did not find executable"
        return super().__call__(cmd, timeout)


# ── which environment ───────────────────────────────────────────────────────


def test_new_servers_default_to_the_shared_environment(tmp_path: Path) -> None:
    res = _install(tmp_path, _app(tmp_path, "app_one"), "one", _FakeRunner(scripts_to_create=("app_one-server",)))
    assert res.ok, res.error
    assert res.environment == "shared" and not res.env_is_legacy
    assert Path(res.venv_dir) == tmp_path / "mcp_packages" / "_envs" / "shared" / "venv"


def test_a_second_server_reuses_the_shared_venv_instead_of_building_another(tmp_path: Path) -> None:
    r1 = _FakeRunner(scripts_to_create=("app_one-server", "app_two-server"))
    _install(tmp_path, _app(tmp_path, "app_one"), "one", r1)
    r2 = _FakeRunner(scripts_to_create=("app_one-server", "app_two-server"))
    res = _install(tmp_path, _app(tmp_path, "app_two"), "two", r2)
    assert res.ok, res.error
    assert _venv_cmds(r2) == []  # no second venv
    assert res.environment == "shared"
    assert res.env_wheels == (("app_one", "0.1.1"),)  # tells the caller who it now shares with


def test_an_explicit_environment_name_is_honoured(tmp_path: Path) -> None:
    res = _install(tmp_path, _app(tmp_path, "app_one"), "one", _FakeRunner(scripts_to_create=("app_one-server",)), environment="solo")
    assert res.ok and res.environment == "solo"
    assert Path(res.venv_dir) == tmp_path / "mcp_packages" / "_envs" / "solo" / "venv"


@pytest.mark.parametrize("bad", ["has space", "../escape", "_envs", "a/b", "-x"])
def test_bad_environment_names_are_rejected_before_anything_runs(tmp_path: Path, bad: str) -> None:
    runner = _FakeRunner()
    res = _install(tmp_path, _app(tmp_path, "app_one"), "one", runner, environment=bad)
    assert not res.ok and res.step == "validate" and "environment name" in res.error
    assert runner.cmds == [] and not (tmp_path / "mcp_packages").exists()


def test_an_empty_environment_means_not_given(tmp_path: Path) -> None:
    res = _install(tmp_path, _app(tmp_path, "app_one"), "one", _FakeRunner(scripts_to_create=("app_one-server",)), environment="")
    assert res.ok and res.environment == "shared"


def test_a_server_with_an_existing_per_server_venv_keeps_it(tmp_path: Path) -> None:
    legacy = tmp_path / "mcp_packages" / "one" / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
    legacy.mkdir(parents=True)
    (legacy / ("python.exe" if sys.platform == "win32" else "python")).write_text("")
    runner = _EnvRunner(scripts_to_create=("app_one-server",))
    res = _install(tmp_path, _app(tmp_path, "app_one"), "one", runner)
    assert res.ok, res.error
    assert res.env_is_legacy and res.environment == "one"
    assert Path(res.venv_dir) == tmp_path / "mcp_packages" / "one" / "venv"
    assert not (tmp_path / "mcp_packages" / "_envs").exists()  # nothing silently moved
    assert not (tmp_path / "mcp_packages" / "one" / "env.json").exists()
    assert all(c[1:6] != ["-m", "pip", "freeze", "--local", "--all"] for c in runner.cmds)  # no env pinning for a single-member venv


def test_naming_the_shared_environment_moves_a_legacy_server_into_it(tmp_path: Path) -> None:
    legacy = tmp_path / "mcp_packages" / "one" / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
    legacy.mkdir(parents=True)
    (legacy / ("python.exe" if sys.platform == "win32" else "python")).write_text("")
    res = _install(tmp_path, _app(tmp_path, "app_one"), "one", _FakeRunner(scripts_to_create=("app_one-server",)), environment="shared")
    assert res.ok and not res.env_is_legacy and res.environment == "shared"


# ── protection: an install must not re-version what neighbours rely on ──────


def test_installing_into_an_environment_with_members_pins_what_is_installed(tmp_path: Path) -> None:
    _install(tmp_path, _app(tmp_path, "app_one"), "one", _FakeRunner(scripts_to_create=("app_one-server",)))
    runner = _EnvRunner(env_freeze="lib-x==1.0\napp_one==0.1.1\nother-lib==3.2\n", scripts_to_create=("app_two-server",))
    res = _install(tmp_path, _app(tmp_path, "app_two"), "two", runner)
    assert res.ok, res.error
    (cmd,) = _pip_installs(runner)
    assert cmd.count("-c") == 2  # host versions AND the environment's own versions
    pins = (tmp_path / "mcp_packages" / "_envs" / "shared" / "_env_constraints.txt").read_text(encoding="utf-8")
    assert "lib-x==1.0" in pins and "other-lib==3.2" in pins and "app_one==0.1.1" in pins


def test_the_package_being_installed_is_not_pinned_so_it_can_be_upgraded(tmp_path: Path) -> None:
    _install(tmp_path, _app(tmp_path, "app_one"), "one", _FakeRunner(scripts_to_create=("app_one-server",)))
    runner = _EnvRunner(env_freeze="lib-x==1.0\nApp.One==0.1.1\n", scripts_to_create=("app_one-server",))
    res = _install(tmp_path, _app(tmp_path, "app_one", version="0.2.0"), "one", runner)  # upgrade of the same dist
    assert res.ok, res.error
    pins = (tmp_path / "mcp_packages" / "_envs" / "shared" / "_env_constraints.txt").read_text(encoding="utf-8")
    assert "lib-x==1.0" in pins
    assert "app_one" not in pins.lower().replace(".", "_") and "app.one" not in pins.lower()  # PEP 503: same dist, any spelling


def test_a_conflict_fails_loudly_names_the_way_out_and_leaves_the_manifest_alone(tmp_path: Path) -> None:
    _install(tmp_path, _app(tmp_path, "app_one"), "one", _FakeRunner(scripts_to_create=("app_one-server",)))
    manifest = tmp_path / "mcp_packages" / "_envs" / "shared" / "env.json"
    before = manifest.read_text(encoding="utf-8")
    res = _install(tmp_path, _app(tmp_path, "app_two"), "two", _EnvRunner(env_freeze="lib-x==1.0\n", install_rc=1))
    assert not res.ok and res.step == "install_wheel"
    assert "environment='<another name>'" in res.error  # tells the agent what to do
    assert manifest.read_text(encoding="utf-8") == before


def test_a_fresh_environment_adds_no_pins_and_no_hint(tmp_path: Path) -> None:
    runner = _EnvRunner(env_freeze="should-not-be-read==9\n", install_rc=1)
    res = _install(tmp_path, _app(tmp_path, "app_one"), "one", runner)
    assert not res.ok and "environment='<another name>'" not in res.error
    assert all(c[1:6] != ["-m", "pip", "freeze", "--local", "--all"] for c in runner.cmds)


# ── the manifest ────────────────────────────────────────────────────────────


def test_the_manifest_records_every_member_and_upgrades_replace(tmp_path: Path) -> None:
    runner_args = {"scripts_to_create": ("app_one-server", "app_two-server")}
    one = _app(tmp_path, "app_one")
    _install(tmp_path, one, "one", _FakeRunner(**runner_args))
    _install(tmp_path, _app(tmp_path, "app_two"), "two", _EnvRunner(**runner_args))
    _install(tmp_path, _app(tmp_path, "app_one", version="0.2.0"), "one", _EnvRunner(**runner_args))
    data = json.loads((tmp_path / "mcp_packages" / "_envs" / "shared" / "env.json").read_text(encoding="utf-8"))
    by = {w["dist"]: w for w in data["wheels"]}
    assert set(by) == {"app_one", "app_two"} and by["app_one"]["version"] == "0.2.0"
    assert Path(by["app_two"]["path"]).is_file()


@pytest.mark.parametrize("junk", ["not json", "[]", '{"wheels": "x"}', '{"wheels": [1, {"dist": 5}]}'])
def test_a_corrupt_manifest_is_treated_as_empty(tmp_path: Path, junk: str) -> None:
    env = tmp_path / "mcp_packages" / "_envs" / "shared"
    env.mkdir(parents=True)
    (env / "env.json").write_text(junk, encoding="utf-8")
    res = _install(tmp_path, _app(tmp_path, "app_one"), "one", _FakeRunner(scripts_to_create=("app_one-server",)))
    assert res.ok, res.error
    assert [w["dist"] for w in json.loads((env / "env.json").read_text(encoding="utf-8"))["wheels"]] == ["app_one"]


# ── rebuilding a shared venv must not drop the other servers' packages ─────


def test_a_rebuild_reinstalls_every_member_in_one_command(tmp_path: Path) -> None:
    one = _app(tmp_path, "app_one")
    _install(tmp_path, one, "one", _FakeRunner(scripts_to_create=("app_one-server", "app_two-server")))
    runner = _EnvRunner(unhealthy_once=True, scripts_to_create=("app_one-server", "app_two-server"))
    res = _install(tmp_path, _app(tmp_path, "app_two"), "two", runner)  # the shared venv "cannot start"
    assert res.ok, res.error
    assert len(_venv_cmds(runner)) == 1  # rebuilt
    (cmd,) = _pip_installs(runner)
    assert str(one) in cmd  # the OTHER server's wheel came back with it
    assert any("rebuilt it and reinstalled 1 other wheel" in n for n in res.notes)


def test_a_rebuild_that_cannot_restore_a_member_refuses_and_touches_nothing(tmp_path: Path) -> None:
    one = _app(tmp_path, "app_one")
    _install(tmp_path, one, "one", _FakeRunner(scripts_to_create=("app_one-server",)))
    marker = tmp_path / "mcp_packages" / "_envs" / "shared" / "venv" / "marker.txt"
    marker.write_text("still here")
    one.unlink()  # the wheel the environment was built from is gone
    runner = _EnvRunner(unhealthy_once=True)
    res = _install(tmp_path, _app(tmp_path, "app_two"), "two", runner)
    assert not res.ok and res.step == "rebuild_env" and one.name in res.error
    assert marker.exists() and _venv_cmds(runner) == [] and _pip_installs(runner) == []


# ── concurrency ─────────────────────────────────────────────────────────────


class _SlowRunner(_FakeRunner):
    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.spans: list[tuple[float, float]] = []
        self._mu = threading.Lock()

    def __call__(self, cmd: list[str], timeout: int) -> tuple[int, str]:
        if cmd[1:4] == ["-m", "pip", "install"]:
            t0 = time.monotonic()
            time.sleep(0.3)
            with self._mu:
                self.spans.append((t0, time.monotonic()))
            return super().__call__(cmd, timeout)
        return super().__call__(cmd, timeout)


def test_two_installs_into_one_environment_never_overlap(tmp_path: Path) -> None:
    runner = _SlowRunner(scripts_to_create=("app_one-server", "app_two-server"))
    a, b = _app(tmp_path, "app_one"), _app(tmp_path, "app_two")

    async def both() -> list[WheelInstallResult]:
        return await asyncio.gather(
            install_wheel_as_mcp(a, server_name="one", data_dir=tmp_path, runner=runner, uv_path=None),
            install_wheel_as_mcp(b, server_name="two", data_dir=tmp_path, runner=runner, uv_path=None),
        )

    results = asyncio.run(asyncio.wait_for(both(), timeout=60))
    assert all(r.ok for r in results), [r.error for r in results]
    first, second = sorted(runner.spans)
    assert first[1] <= second[0], "two pip runs overlapped on one venv"
    assert len(_venv_cmds(runner)) == 1  # and the venv was built once, not raced


def test_locks_are_per_environment(tmp_path: Path) -> None:
    a = inst._env_lock(tmp_path / "x")
    assert inst._env_lock(tmp_path / "x") is a
    assert inst._env_lock(tmp_path / "y") is not a


# ── the mcp_manage tool ─────────────────────────────────────────────────────


class _Reg:
    def __init__(self) -> None:
        self.servers: dict[str, Any] = {}

    def global_enabled(self) -> bool:
        return True

    async def add_server(self, config: Any) -> Any:
        self.servers[config.name] = config
        return SimpleNamespace(config=config, connected=True, tool_count=1, tool_names=("t",), error="")


def _tool(seen: dict[str, Any], result: WheelInstallResult, **args: Any) -> str:
    async def fake_install(wheel_path: str, **kw: Any) -> WheelInstallResult:
        seen.update(kw)
        return result

    h = McpManageToolHandler(registry=_Reg(), data_dir=Path("D"), install_fn=fake_install)  # type: ignore[arg-type]
    base = {"action": "install_wheel", "name": "s", "wheel_path": "w.whl"}
    return asyncio.run(h.execute(SimpleNamespace(arguments={**base, **args})))  # type: ignore[arg-type]


def _ok(**over: Any) -> WheelInstallResult:
    base: dict[str, Any] = dict(ok=True, dist_name="d", version="1", venv_dir="V", command="V/d.exe", args=(), environment="shared")
    base.update(over)
    return WheelInstallResult(**base)


def test_the_tool_forwards_the_environment_and_reports_who_shares_it() -> None:
    seen: dict[str, Any] = {}
    out = _tool(seen, _ok(env_wheels=(("cat", "2"), ("lib", "3"))), environment=" modem ")
    assert seen["environment"] == "modem"
    assert "Environment: shared (also holds: cat 2, lib 3)" in out


def test_without_an_environment_the_tool_passes_none_and_stays_quiet_about_sharing() -> None:
    seen: dict[str, Any] = {}
    out = _tool(seen, _ok())
    assert seen["environment"] is None
    assert "Environment: shared" in out and "also holds" not in out


def test_a_server_that_kept_its_old_environment_is_told_how_to_move() -> None:
    out = _tool({}, _ok(environment="s", env_is_legacy=True))
    assert "kept its existing per-server environment" in out and "environment='shared'" in out


@pytest.mark.parametrize("bad", ["", "  ", 5, ["shared"]])
def test_a_malformed_environment_is_rejected_without_installing(bad: Any) -> None:
    seen: dict[str, Any] = {}
    out = _tool(seen, _ok(), environment=bad)
    assert "environment must be a non-empty name" in out and seen == {}


def test_the_schema_explains_sharing_and_the_conflict_way_out() -> None:
    desc = MCP_MANAGE_TOOL_SCHEMA["function"]["parameters"]["properties"]["environment"]["description"]
    assert "SAME environment" in desc and "shared" in desc and "different versions" in desc


# ── real venv, real pip/uv ──────────────────────────────────────────────────


def _local_index(monkeypatch: pytest.MonkeyPatch, wheels_dir: Path) -> None:
    """Make pip AND uv resolve ONLY from ``wheels_dir`` (no network, no PyPI lookups)."""
    for k, v in (("PIP_NO_INDEX", "1"), ("PIP_FIND_LINKS", str(wheels_dir)),
                 ("UV_NO_INDEX", "1"), ("UV_FIND_LINKS", str(wheels_dir))):
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize("installer", ["pip", "default"])
def test_two_servers_share_a_venv_and_a_conflicting_third_cannot_break_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installer: str
) -> None:
    import subprocess

    if installer == "pip":
        monkeypatch.setattr(inst.shutil, "which", lambda *_a, **_k: None)  # force the pip branch
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    libx1 = _write_wheel(wheels, "qai_synth_libx", "1.0", {"qai_synth_libx/__init__.py": "VERSION = '1.0'\n"})
    _write_wheel(wheels, "qai_synth_libx", "2.0", {"qai_synth_libx/__init__.py": "VERSION = '2.0'\n"})
    catalog = _write_wheel(wheels, "qai_synth_catalog", "1.0", {"qai_synth_catalog/__init__.py": "NAME = 'catalog-says-hi'\n"})
    # server A needs libx==1.0; server B imports the CATALOG package (shipped by another server).
    a = _write_wheel(
        wheels, "qai_synth_a", "1.0",
        {"qai_synth_a/__init__.py": "import qai_synth_libx\n\ndef main():\n    print('a', qai_synth_libx.VERSION)\n"},
        scripts={"qai-synth-a": "qai_synth_a:main"}, requires=("qai_synth_libx==1.0",),
    )
    b = _write_wheel(
        wheels, "qai_synth_b", "1.0",
        {"qai_synth_b/__init__.py": "import qai_synth_catalog\n\ndef main():\n    print('b', qai_synth_catalog.NAME)\n"},
        scripts={"qai-synth-b": "qai_synth_b:main"},
    )
    c = _write_wheel(  # wants a DIFFERENT libx than the one server A relies on
        wheels, "qai_synth_c", "1.0",
        {"qai_synth_c/__init__.py": "def main():\n    pass\n"},
        scripts={"qai-synth-c": "qai_synth_c:main"}, requires=("qai_synth_libx==2.0",),
    )
    _local_index(monkeypatch, wheels)

    ra = _run(install_wheel_as_mcp(a, server_name="a", data_dir=tmp_path))
    if not ra.ok and ra.step in ("create_venv", "bootstrap_pip"):
        pytest.skip(f"cannot build a venv here: {ra.step}: {ra.error[:120]}")
    assert ra.ok, ra.error
    assert ra.environment == "shared"

    # B gets the catalog package from the same environment A lives in -- no extra_wheels
    # needed for B to import what ANOTHER server ships once that package is in the env.
    rb = _run(install_wheel_as_mcp(b, server_name="b", data_dir=tmp_path, extra_wheels=[catalog]))
    assert rb.ok, rb.error
    assert Path(ra.venv_dir) == Path(rb.venv_dir)  # ONE venv
    assert ("qai_synth_a", "1.0") in rb.env_wheels
    out_b = subprocess.run([rb.command], capture_output=True, text=True, timeout=120)
    assert out_b.returncode == 0 and out_b.stdout.strip() == "b catalog-says-hi", out_b.stderr
    out_a = subprocess.run([ra.command], capture_output=True, text=True, timeout=120)
    assert out_a.stdout.strip() == "a 1.0", out_a.stderr

    # C needs libx==2.0, which would silently re-version what A relies on: it must FAIL ...
    rc = _run(install_wheel_as_mcp(c, server_name="c", data_dir=tmp_path))
    assert not rc.ok and rc.step == "install_wheel"
    assert "environment='<another name>'" in rc.error
    # ... and A must be exactly as it was.
    assert subprocess.run([ra.command], capture_output=True, text=True, timeout=120).stdout.strip() == "a 1.0"
    assert libx1.exists()
