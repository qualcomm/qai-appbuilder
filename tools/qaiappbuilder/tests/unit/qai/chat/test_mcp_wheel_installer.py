# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``mcp_wheel_installer`` -- read a local wheel and install it as a stdio MCP.

The wheel is built in ``tmp_path``; the venv / pip / uv subprocesses are replaced
by a recording fake ``runner`` so nothing real is created or downloaded.
"""

from __future__ import annotations

import asyncio
import sys
import zipfile
from pathlib import Path

import pytest

from qai.chat.adapters.mcp_wheel_installer import (
    install_wheel_as_mcp,
    read_wheel_info,
)


def _make_wheel(
    dir_: Path,
    *,
    name: str = "demo_mcp",
    version: str = "0.1.1",
    scripts: dict[str, str] | None = None,
    top_level: bool = True,
) -> Path:
    whl = dir_ / f"{name}-{version}-py3-none-any.whl"
    dist = f"{name}-{version}.dist-info"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr(f"{name}/__init__.py", "")
        zf.writestr(
            f"{dist}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\nlong description\n",
        )
        if scripts is not None:
            body = "[console_scripts]\n" + "".join(
                f"{k} = {v}\n" for k, v in scripts.items()
            )
            zf.writestr(f"{dist}/entry_points.txt", body)
        if top_level:
            zf.writestr(f"{dist}/top_level.txt", f"{name}\n")
    return whl


class _FakeRunner:
    """Records every command; creates the venv python / script wrappers on demand."""

    def __init__(
        self,
        *,
        venv_ok: bool = True,
        pip_present: bool = True,
        ensurepip_ok: bool = True,
        install_rc: int = 0,
        freeze_ok: bool = True,
        scripts_to_create: tuple[str, ...] = (),
    ) -> None:
        self.cmds: list[list[str]] = []
        self._venv_ok = venv_ok
        self._pip_present = pip_present
        self._ensurepip_ok = ensurepip_ok
        self._install_rc = install_rc
        self._freeze_ok = freeze_ok
        self._scripts = scripts_to_create

    def __call__(self, cmd: list[str], timeout: int) -> tuple[int, str]:
        self.cmds.append(cmd)
        if "venv" in cmd and "-m" in cmd and cmd[cmd.index("-m") + 1] == "venv":
            if not self._venv_ok:
                return 1, "venv creation exploded"
            venv = Path(cmd[-1])
            bindir = venv / ("Scripts" if sys.platform == "win32" else "bin")
            bindir.mkdir(parents=True, exist_ok=True)
            exe = "python.exe" if sys.platform == "win32" else "python"
            (bindir / exe).write_text("")
            for s in self._scripts:
                sfx = ".exe" if sys.platform == "win32" else ""
                (bindir / f"{s}{sfx}").write_text("")
            return 0, ""
        if cmd[:3] == [sys.executable, "-m", "pip"] and "freeze" in cmd:
            return (0, "mcp==1.9.0\nhttpx==0.27.0\n-e git+x#egg=y\n") if self._freeze_ok else (1, "no pip")
        if cmd[-2:] == ["pip", "--version"]:
            return (0, "pip 24") if self._pip_present else (1, "No module named pip")
        if "ensurepip" in cmd:
            return (0, "") if self._ensurepip_ok else (1, "ensurepip failed")
        if "install" in cmd:
            return self._install_rc, "boom: could not build native wheel" if self._install_rc else "ok"
        return 0, ""


def _run(coro):
    return asyncio.run(coro)


# ── read_wheel_info ─────────────────────────────────────────────────────────


def test_read_wheel_info_extracts_name_version_and_console_scripts(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"demo-server": "demo_mcp.server:main", "other": "demo_mcp.o:x"})
    info = read_wheel_info(whl)
    assert info.dist_name == "demo_mcp"
    assert info.version == "0.1.1"
    assert info.console_scripts == ("demo-server", "other")
    assert info.top_level == ("demo_mcp",)


def test_read_wheel_info_without_entry_points_has_no_scripts(tmp_path: Path) -> None:
    info = read_wheel_info(_make_wheel(tmp_path, scripts=None))
    assert info.console_scripts == ()


def test_read_wheel_info_infers_top_level_from_layout_when_file_missing(tmp_path: Path) -> None:
    info = read_wheel_info(_make_wheel(tmp_path, top_level=False))
    assert info.top_level == ("demo_mcp",)


def test_read_wheel_info_only_reads_console_scripts_section(tmp_path: Path) -> None:
    whl = tmp_path / "x-1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("x-1.0.dist-info/METADATA", "Name: x\nVersion: 1.0\n")
        zf.writestr(
            "x-1.0.dist-info/entry_points.txt",
            "[gui_scripts]\ngui = x:g\n\n[console_scripts]\nreal = x:m\n",
        )
    assert read_wheel_info(whl).console_scripts == ("real",)


@pytest.mark.parametrize("make", ["not_whl", "missing", "bad_zip", "no_metadata"])
def test_read_wheel_info_rejects_bad_input(tmp_path: Path, make: str) -> None:
    if make == "not_whl":
        p = tmp_path / "a.zip"
        p.write_text("x")
    elif make == "missing":
        p = tmp_path / "nope-1-py3-none-any.whl"
    elif make == "bad_zip":
        p = tmp_path / "bad-1-py3-none-any.whl"
        p.write_text("this is not a zip")
    else:
        p = tmp_path / "nometa-1-py3-none-any.whl"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("nometa/__init__.py", "")
    with pytest.raises(ValueError):
        read_wheel_info(p)


# ── install_wheel_as_mcp ────────────────────────────────────────────────────


def test_rejects_an_illegal_server_name_before_touching_anything(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    runner = _FakeRunner()
    res = _run(install_wheel_as_mcp(whl, server_name="bad name!", data_dir=tmp_path, runner=runner))
    assert not res.ok and res.step == "validate"
    assert runner.cmds == []


def test_reports_a_bad_wheel_as_a_structured_error(tmp_path: Path) -> None:
    res = _run(install_wheel_as_mcp(tmp_path / "nope.whl", server_name="ok", data_dir=tmp_path, runner=_FakeRunner()))
    assert not res.ok and res.step == "read_wheel"
    assert "not found" in res.error


def test_installs_with_pip_and_returns_the_console_script(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"demo-server": "demo_mcp.server:main"})
    runner = _FakeRunner(scripts_to_create=("demo-server",))
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=runner, uv_path=None))
    assert res.ok, res.error
    assert Path(res.command).name.startswith("demo-server")
    assert res.args == ()
    assert res.entry_is_guess is False
    install_cmds = [c for c in runner.cmds if "install" in c]
    assert len(install_cmds) == 1
    assert install_cmds[0][1:4] == ["-m", "pip", "install"]
    assert str(whl) in install_cmds[0]
    # host-version constraints were written, without the editable line
    cons = tmp_path / "mcp_packages" / "_envs" / "shared" / "_host_constraints.txt"  # new servers default to the shared environment
    assert cons.read_text(encoding="utf-8").splitlines() == ["mcp==1.9.0", "httpx==0.27.0"]
    assert "-c" in install_cmds[0]


def test_prefers_uv_when_available(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    runner = _FakeRunner(scripts_to_create=("s",))
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=runner, uv_path="/bin/uv"))
    assert res.ok, res.error
    uv_cmd = next(c for c in runner.cmds if c[0] == "/bin/uv")
    assert uv_cmd[1:3] == ["pip", "install"] and "--python" in uv_cmd
    assert not any(c[1:4] == ["-m", "pip", "install"] for c in runner.cmds)


def test_bootstraps_pip_when_the_venv_has_none(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    runner = _FakeRunner(pip_present=False, scripts_to_create=("s",))
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=runner, uv_path=None))
    assert res.ok, res.error
    assert any("ensurepip" in c for c in runner.cmds)
    assert any("ensurepip" in n for n in res.notes)


def test_fails_cleanly_when_pip_cannot_be_bootstrapped(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    runner = _FakeRunner(pip_present=False, ensurepip_ok=False)
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=runner, uv_path=None))
    assert not res.ok and res.step == "bootstrap_pip"


def test_surfaces_the_pip_error_text_when_install_fails(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    runner = _FakeRunner(install_rc=1)
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=runner, uv_path=None))
    assert not res.ok and res.step == "install_wheel"
    assert "native wheel" in res.error


def test_venv_creation_failure_is_reported(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=_FakeRunner(venv_ok=False), uv_path=None))
    assert not res.ok and res.step == "create_venv"


def test_install_proceeds_without_constraints_if_host_pip_freeze_fails(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    runner = _FakeRunner(freeze_ok=False, scripts_to_create=("s",))
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=runner, uv_path=None))
    assert res.ok, res.error
    assert any("constraints" in n for n in res.notes)
    assert all("-c" not in c for c in runner.cmds if "install" in c)


def test_explicit_entry_is_used_and_validated(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"first": "m:a", "second": "m:b"})
    ok = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, entry="second",
                                   runner=_FakeRunner(scripts_to_create=("first", "second")), uv_path=None))
    assert ok.ok and Path(ok.command).name.startswith("second")
    bad = _run(install_wheel_as_mcp(whl, server_name="demo2", data_dir=tmp_path, entry="nope",
                                    runner=_FakeRunner(), uv_path=None))
    assert not bad.ok and bad.step == "resolve_entry" and "first, second" in bad.error


def test_without_console_scripts_it_guesses_python_dash_m(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts=None)
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=_FakeRunner(), uv_path=None))
    assert res.ok and res.entry_is_guess
    assert res.args == ("-m", "demo_mcp")
    assert Path(res.command).name.startswith("python")


def test_reinstall_reuses_the_existing_venv(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"s": "m:f"})
    r1 = _FakeRunner(scripts_to_create=("s",))
    _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=r1, uv_path=None))
    r2 = _FakeRunner(scripts_to_create=("s",))
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=r2, uv_path=None))
    assert res.ok
    assert not any(c[1:3] == ["-m", "venv"] for c in r2.cmds)


# ── entry selection (the user's real wheel ships four console scripts) ──────

from qai.chat.adapters.mcp_wheel_installer import choose_entry  # noqa: E402

_QUTS_SCRIPTS = {
    "quts-agent-evidence-mcp": "quts_skill_validator_mcp.agent_quts_mcp:main",
    "quts-skill-mcp": "quts_skill_validator_mcp.server:main",
    "quts-skill-rest": "quts_skill_validator_mcp.rest_api:main",
    "quts-skill-validator": "quts_skill_validator_mcp.cli:main",
}


def test_picks_the_server_module_among_several_mcp_named_scripts(tmp_path: Path) -> None:
    info = read_wheel_info(_make_wheel(tmp_path, name="quts_skill_validator_mcp", scripts=_QUTS_SCRIPTS))
    assert choose_entry(info, None) == ("quts-skill-mcp", "")


def test_a_single_mcp_named_script_wins(tmp_path: Path) -> None:
    info = read_wheel_info(_make_wheel(tmp_path, scripts={"tool-cli": "d.cli:m", "tool-mcp": "d.x:m"}))
    assert choose_entry(info, None) == ("tool-mcp", "")


def test_ambiguity_is_reported_not_guessed(tmp_path: Path) -> None:
    info = read_wheel_info(_make_wheel(tmp_path, scripts={"alpha": "d.a:m", "beta": "d.b:m"}))
    chosen, why = choose_entry(info, None)
    assert chosen is None and "alpha, beta" in why and "entry=" in why


def test_explicit_entry_always_wins_even_over_the_heuristic(tmp_path: Path) -> None:
    info = read_wheel_info(_make_wheel(tmp_path, name="quts_skill_validator_mcp", scripts=_QUTS_SCRIPTS))
    assert choose_entry(info, "quts-agent-evidence-mcp") == ("quts-agent-evidence-mcp", "")


def test_ambiguous_entry_fails_before_anything_is_created_or_installed(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"alpha": "d.a:m", "beta": "d.b:m"})
    runner = _FakeRunner()
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=runner, uv_path=None))
    assert not res.ok and res.step == "resolve_entry"
    assert runner.cmds == []
    assert not (tmp_path / "mcp_packages").exists()


def test_missing_wrapper_falls_back_to_the_entry_points_module(tmp_path: Path) -> None:
    whl = _make_wheel(tmp_path, scripts={"only": "demo_pkg.server:main"})
    res = _run(install_wheel_as_mcp(whl, server_name="demo", data_dir=tmp_path, runner=_FakeRunner(), uv_path=None))
    assert res.ok and res.entry_is_guess
    assert res.args == ("-m", "demo_pkg.server")  # the module, never the script name
