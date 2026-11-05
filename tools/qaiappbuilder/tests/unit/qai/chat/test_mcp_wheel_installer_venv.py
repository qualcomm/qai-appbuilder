# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""The MCP venv must not depend on a fragile interpreter path.

Real incident (2026-11): a wheel-installed MCP stopped starting with
``did not find executable at ...\\uv\\python\\cpython-3.13-windows-x86_64-none\\python.exe``.
The venv had been built through the app's uv-made venv python, which recorded uv's
*minor-version alias* directory as the base ("home"); uv later removed that alias,
orphaning every MCP venv built that way. Build from the real base interpreter, and
rebuild a venv that can no longer start.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from qai.chat.adapters import mcp_wheel_installer as inst
from qai.chat.adapters.mcp_wheel_installer import install_wheel_as_mcp
from tests.unit.qai.chat.test_mcp_wheel_installer import _FakeRunner, _make_wheel


def _exe_name() -> str:
    return "python.exe" if sys.platform == "win32" else "python"


def _bindir(venv: Path) -> Path:
    return venv / ("Scripts" if sys.platform == "win32" else "bin")


class _UnhealthyVenvRunner(_FakeRunner):
    """Like the fake runner, but the EXISTING venv python cannot start."""

    def __call__(self, cmd, timeout):  # type: ignore[no-untyped-def]
        if cmd[1:3] == ["-c", "import sys"] and not getattr(self, "rebuilt", False):
            self.cmds.append(cmd)
            return 103, "did not find executable at 'C:/gone/python.exe'"
        out = super().__call__(cmd, timeout)
        if "venv" in cmd and cmd[cmd.index("-m") + 1:cmd.index("-m") + 2] == ["venv"]:
            self.rebuilt = True
        return out


def test_base_python_prefers_the_real_base_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "base"
    exe = base / ("python.exe" if sys.platform == "win32" else "bin/python3")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(sys, "base_prefix", str(base))
    assert inst._base_python() == str(exe)


def test_base_python_falls_back_to_sys_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "nothing-here"))
    assert inst._base_python() == sys.executable


def test_new_venv_is_built_from_the_base_interpreter_not_the_app_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inst, "_base_python", lambda: "BASE_PY")
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    runner = _FakeRunner(scripts_to_create=("s",))
    res = asyncio.run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=runner, uv_path=None))
    assert res.ok, res.error
    venv_cmd = next(c for c in runner.cmds if c[1:3] == ["-m", "venv"])
    assert venv_cmd[0] == "BASE_PY"
    assert venv_cmd[0] != sys.executable


def test_a_venv_that_cannot_start_is_rebuilt(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    venv = tmp_path / "mcp_packages" / "demo" / "venv"
    _bindir(venv).mkdir(parents=True)
    (_bindir(venv) / _exe_name()).write_text("")  # present, but "cannot start"
    stale = venv / "stale-marker.txt"
    stale.write_text("old")

    runner = _UnhealthyVenvRunner(scripts_to_create=("s",))
    res = asyncio.run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=runner, uv_path=None))

    assert res.ok, res.error
    assert not stale.exists(), "the broken venv was not removed"
    assert any(c[1:3] == ["-m", "venv"] for c in runner.cmds), "no new venv was created"
    assert any("rebuilt" in n for n in res.notes)


def test_a_healthy_existing_venv_is_kept(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    venv = tmp_path / "mcp_packages" / "demo" / "venv"
    _bindir(venv).mkdir(parents=True)
    (_bindir(venv) / _exe_name()).write_text("")
    keep = venv / "keep-marker.txt"
    keep.write_text("mine")

    runner = _FakeRunner(scripts_to_create=("s",))  # health probe -> rc 0
    res = asyncio.run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=runner, uv_path=None))

    assert res.ok, res.error
    assert keep.exists()
    assert not any(c[1:3] == ["-m", "venv"] for c in runner.cmds)
    assert not any("rebuilt" in n for n in res.notes)
