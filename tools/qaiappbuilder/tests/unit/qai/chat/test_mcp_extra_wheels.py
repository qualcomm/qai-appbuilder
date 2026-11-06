# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``extra_wheels``: install companion wheels into a server's own isolated venv.

Why it exists: every MCP server gets its own venv, so a package installed for
server A cannot be imported by server B. A server that needs a companion package
at run time (one real example: a validator that resolves skill ids by importing the
catalog package that owns them) can only work if that package is installed into
ITS venv. The feature must stay generic -- these tests use made-up names and a
tripwire fails if a real package name ever appears in the implementation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qai.chat.adapters import mcp_wheel_installer as inst
from qai.chat.adapters.mcp_manage_tool import MCP_MANAGE_TOOL_SCHEMA, McpManageToolHandler
from qai.chat.adapters.mcp_wheel_installer import WheelInstallResult, install_wheel_as_mcp
from tests.unit.qai.chat.test_mcp_wheel_installer import _FakeRunner, _make_wheel


def _run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=300))


def _install(tmp_path: Path, primary: Path, extras: list[Any], runner: Any, uv: str | None = None) -> WheelInstallResult:
    return _run(
        install_wheel_as_mcp(
            primary, server_name="demo", data_dir=tmp_path, runner=runner, uv_path=uv, extra_wheels=extras
        )
    )


def _install_cmds(runner: _FakeRunner) -> list[list[str]]:
    return [c for c in runner.cmds if "install" in c]


# ── the install command ─────────────────────────────────────────────────────


def test_pip_installs_the_primary_and_every_companion_in_one_call(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"app-server": "app_server:main"})
    cat = _make_wheel(tmp_path, name="data_catalog")
    util = _make_wheel(tmp_path, name="shared_util")
    runner = _FakeRunner(scripts_to_create=("app-server",))

    res = _install(tmp_path, app, [cat, util], runner)

    assert res.ok, res.error
    (cmd,) = _install_cmds(runner)  # exactly ONE install invocation
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert cmd[-3:] == [str(app), str(cat), str(util)]  # primary first, then the companions in order
    assert "-c" in cmd  # the host-version constraints are still applied


def test_uv_path_also_installs_everything_in_one_call(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"app-server": "app_server:main"})
    cat = _make_wheel(tmp_path, name="data_catalog")
    runner = _FakeRunner(scripts_to_create=("app-server",))

    res = _install(tmp_path, app, [cat], runner, uv="/bin/uv")

    assert res.ok, res.error
    (cmd,) = [c for c in runner.cmds if c[0] == "/bin/uv"]
    assert cmd[1:3] == ["pip", "install"] and cmd[-2:] == [str(app), str(cat)]


def test_without_extra_wheels_the_command_is_exactly_what_it_always_was(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"app-server": "app_server:main"})
    runner = _FakeRunner(scripts_to_create=("app-server",))
    res = _install(tmp_path, app, [], runner)
    assert res.ok and res.extra_wheels == ()
    assert _install_cmds(runner)[0][-1] == str(app)


def test_the_result_reports_what_was_installed_alongside(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"app-server": "app_server:main"})
    cat = _make_wheel(tmp_path, name="data_catalog", version="2.5")
    res = _install(tmp_path, app, [cat], _FakeRunner(scripts_to_create=("app-server",)))
    assert res.extra_wheels == (("data_catalog", "2.5"),)


def test_the_launch_command_comes_from_the_primary_wheel_only(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"app-server": "app_server:main"})
    # A companion that ships its own mcp-looking script must not be mistaken for the server.
    cat = _make_wheel(tmp_path, name="data_catalog", scripts={"catalog-mcp": "data_catalog:main"})
    res = _install(tmp_path, app, [cat], _FakeRunner(scripts_to_create=("app-server", "catalog-mcp")))
    assert res.ok, res.error
    assert Path(res.command).name.startswith("app-server")


# ── validation happens BEFORE anything is created or run ───────────────────


def _nothing_happened(tmp_path: Path, runner: _FakeRunner) -> None:
    assert runner.cmds == []
    assert not (tmp_path / "mcp_packages").exists()


def test_a_missing_companion_is_rejected_up_front(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"s": "m:f"})
    runner = _FakeRunner()
    res = _install(tmp_path, app, [tmp_path / "nope-1-py3-none-any.whl"], runner)
    assert not res.ok and res.step == "read_wheel"
    assert "extra_wheels[0]" in res.error and "not found" in res.error
    _nothing_happened(tmp_path, runner)


@pytest.mark.parametrize("kind", ["not_a_wheel", "bad_zip", "empty_string", "non_string"])
def test_unusable_companions_are_rejected_up_front(tmp_path: Path, kind: str) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"s": "m:f"})
    extra: Any
    if kind == "not_a_wheel":
        extra = tmp_path / "data.zip"
        extra.write_text("x")
    elif kind == "bad_zip":
        extra = tmp_path / "bad-1-py3-none-any.whl"
        extra.write_text("this is not a zip")
    elif kind == "empty_string":
        extra = "   "
    else:
        extra = 123
    runner = _FakeRunner()
    res = _install(tmp_path, app, [extra], runner)
    assert not res.ok and res.step == "read_wheel" and "extra_wheels[0]" in res.error
    _nothing_happened(tmp_path, runner)


def test_the_second_bad_companion_is_named_by_its_index(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"s": "m:f"})
    ok = _make_wheel(tmp_path, name="data_catalog")
    res = _install(tmp_path, app, [ok, tmp_path / "gone-1-py3-none-any.whl"], _FakeRunner())
    assert not res.ok and "extra_wheels[1]" in res.error


def test_listing_the_same_file_twice_or_the_primary_again_installs_it_once(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"app-server": "app_server:main"})
    cat = _make_wheel(tmp_path, name="data_catalog")
    runner = _FakeRunner(scripts_to_create=("app-server",))
    res = _install(tmp_path, app, [cat, cat, app], runner)
    assert res.ok, res.error
    assert _install_cmds(runner)[0][-2:] == [str(app), str(cat)]
    assert res.extra_wheels == (("data_catalog", "0.1.1"),)


def test_two_wheels_of_one_distribution_are_rejected(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"s": "m:f"})
    v1 = _make_wheel(tmp_path, name="data_catalog", version="1.0")
    v2 = _make_wheel(tmp_path, name="data_catalog", version="2.0")
    runner = _FakeRunner()
    res = _install(tmp_path, app, [v1, v2], runner)
    assert not res.ok and res.step == "read_wheel" and "another wheel of 'data_catalog'" in res.error
    _nothing_happened(tmp_path, runner)


def test_a_companion_with_the_primarys_name_is_rejected_whatever_the_spelling(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", version="1.0", scripts={"s": "m:f"})
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    twin = _make_wheel(other_dir, name="app_server", version="2.0")
    # PEP 503: "App.Server" == "app-server" == "app_server"
    import zipfile as _z

    renamed = other_dir / "App.Server-9-py3-none-any.whl"
    with _z.ZipFile(renamed, "w") as zf:
        zf.writestr("App.Server-9.dist-info/METADATA", "Name: App.Server\nVersion: 9\n")
    for clash in (twin, renamed):
        res = _install(tmp_path, app, [clash], _FakeRunner())
        assert not res.ok and "another wheel of 'app_server'" in res.error, clash.name


def test_more_than_the_limit_is_rejected(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"s": "m:f"})
    many = [_make_wheel(tmp_path, name=f"pkg{i}") for i in range(inst._MAX_EXTRA_WHEELS + 1)]
    runner = _FakeRunner()
    res = _install(tmp_path, app, many, runner)
    assert not res.ok and "too many extra_wheels" in res.error
    _nothing_happened(tmp_path, runner)


def test_a_failed_install_reports_the_pip_error_and_no_companions(tmp_path: Path) -> None:
    app = _make_wheel(tmp_path, name="app_server", scripts={"s": "m:f"})
    cat = _make_wheel(tmp_path, name="data_catalog")
    res = _install(tmp_path, app, [cat], _FakeRunner(install_rc=1))
    assert not res.ok and res.step == "install_wheel" and res.extra_wheels == ()


# ── the feature is generic: no package name is baked into the implementation ──


def test_no_real_package_name_is_hardcoded_in_the_implementation() -> None:
    root = Path(inst.__file__).parent
    for name in ("mcp_wheel_installer.py", "mcp_manage_tool.py"):
        text = (root / name).read_text(encoding="utf-8").lower()
        for banned in ("sm8975", "quts", "skill_resolution", "_skills_bundle"):
            assert banned not in text, f"{banned!r} must not appear in {name}: extra_wheels is generic"


# ── the mcp_manage tool ─────────────────────────────────────────────────────


class _Reg:
    def __init__(self) -> None:
        self.servers: dict[str, Any] = {}

    def global_enabled(self) -> bool:
        return True

    async def add_server(self, config: Any) -> Any:
        self.servers[config.name] = config
        return SimpleNamespace(config=config, connected=True, tool_count=1, tool_names=("t",), error="")


def _tool(extra_wheels: Any = "ABSENT", seen: dict[str, Any] | None = None) -> str:
    async def fake_install(wheel_path: str, **kw: Any) -> WheelInstallResult:
        if seen is not None:
            seen.update(kw)
        return WheelInstallResult(
            ok=True, dist_name="app-server", version="1", venv_dir="V", command="V/app.exe", args=(),
            extra_wheels=(("data-catalog", "2"), ("shared-util", "3")),
        )

    args: dict[str, Any] = {"action": "install_wheel", "name": "s", "wheel_path": "w.whl"}
    if extra_wheels != "ABSENT":
        args["extra_wheels"] = extra_wheels
    h = McpManageToolHandler(registry=_Reg(), data_dir=Path("D"), install_fn=fake_install)  # type: ignore[arg-type]
    return asyncio.run(h.execute(SimpleNamespace(arguments=args)))  # type: ignore[arg-type]


def test_the_tool_passes_the_list_through_and_reports_the_companions() -> None:
    seen: dict[str, Any] = {}
    out = _tool(["C:/x/data_catalog-2-py3-none-any.whl", "  C:/x/shared_util-3-py3-none-any.whl "], seen)
    assert seen["extra_wheels"] == ["C:/x/data_catalog-2-py3-none-any.whl", "C:/x/shared_util-3-py3-none-any.whl"]
    assert "Also installed into the same environment: data-catalog 2, shared-util 3" in out


def test_the_tool_defaults_to_no_companions_and_stays_quiet() -> None:
    seen: dict[str, Any] = {}
    out = _tool("ABSENT", seen)
    assert seen["extra_wheels"] == []
    assert "Registered MCP server 's'" in out


@pytest.mark.parametrize("bad", ["one.whl", [1, 2], ["ok.whl", ""], {"a": "b"}, [["nested.whl"]]])
def test_the_tool_rejects_a_malformed_list_without_installing(bad: Any) -> None:
    seen: dict[str, Any] = {}
    out = _tool(bad, seen)
    assert "extra_wheels must be a list of non-empty .whl path strings" in out
    assert seen == {}  # the installer was never called


def test_the_schema_explains_isolation_and_gives_a_generic_clue() -> None:
    prop = MCP_MANAGE_TOOL_SCHEMA["function"]["parameters"]["properties"]["extra_wheels"]
    assert prop["type"] == "array" and prop["items"] == {"type": "string"}
    text = prop["description"]
    assert "isolated" in text and "SAME environment" in text and "same Python environment" in text


# ── real venv, real pip: the companion is actually importable ───────────────


def _write_wheel(
    dir_: Path,
    name: str,
    version: str,
    files: dict[str, str],
    scripts: dict[str, str] | None = None,
    requires: tuple[str, ...] = (),
) -> Path:
    """A real, installable pure-Python wheel (valid WHEEL + RECORD); ``requires`` = Requires-Dist lines."""
    dist = f"{name}-{version}.dist-info"
    members: dict[str, str] = dict(files)
    reqs = "".join(f"Requires-Dist: {r}\n" for r in requires)
    members[f"{dist}/METADATA"] = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{reqs}"
    members[f"{dist}/WHEEL"] = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    members[f"{dist}/top_level.txt"] = name + "\n"
    if scripts:
        members[f"{dist}/entry_points.txt"] = "[console_scripts]\n" + "".join(f"{k} = {v}\n" for k, v in scripts.items())
    record = []
    for path, body in members.items():
        raw = body.encode("utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(raw)}")
    record.append(f"{dist}/RECORD,,")
    members[f"{dist}/RECORD"] = "\n".join(record) + "\n"
    whl = dir_ / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        for path, body in members.items():
            zf.writestr(path, body)
    return whl


def _real_pair(tmp_path: Path) -> tuple[Path, Path]:
    cat = _write_wheel(tmp_path, "data_catalog", "1.0", {"data_catalog/__init__.py": 'VALUE = "catalog-says-hi"\n'})
    app = _write_wheel(
        tmp_path, "app_server", "1.0",
        {"app_server/__init__.py": "import data_catalog\n\ndef main():\n    print(data_catalog.VALUE)\n"},
        scripts={"app-server": "app_server:main"},
    )
    return app, cat


@pytest.mark.parametrize("installer", ["pip", "default"])
def test_a_companion_is_importable_from_the_servers_own_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installer: str
) -> None:
    if installer == "pip":
        monkeypatch.setattr(inst.shutil, "which", lambda *_a, **_k: None)  # force the pip branch
    app, cat = _real_pair(tmp_path)

    if installer == "default":
        # Control (once, to keep the suite fast): WITHOUT the companion the server's
        # own code cannot import it -- the isolation this feature exists to bridge.
        alone = _run(install_wheel_as_mcp(app, server_name="alone", data_dir=tmp_path))
        if not alone.ok and alone.step in ("create_venv", "bootstrap_pip"):
            pytest.skip(f"cannot build a venv here: {alone.step}: {alone.error[:120]}")
        assert alone.ok, alone.error
        failed = subprocess.run([alone.command], capture_output=True, text=True, timeout=120)
        assert failed.returncode != 0 and "data_catalog" in failed.stderr  # ModuleNotFoundError

    # With extra_wheels the very same server works.
    both = _run(install_wheel_as_mcp(app, server_name="both", data_dir=tmp_path, extra_wheels=[cat]))
    if not both.ok and both.step in ("create_venv", "bootstrap_pip"):
        pytest.skip(f"cannot build a venv here: {both.step}: {both.error[:120]}")
    assert both.ok, both.error
    assert both.extra_wheels == (("data_catalog", "1.0"),)
    out = subprocess.run([both.command], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "catalog-says-hi"
