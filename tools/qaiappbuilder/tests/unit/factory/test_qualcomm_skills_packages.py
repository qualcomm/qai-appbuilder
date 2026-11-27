# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Built-in ``qualcomm-skills``: unpacking nested deliveries, recognising the
MCP wheel, installing into the package's own directory, removing.

Every archive here is synthetic (shaped like a real delivery, no real content);
pip is replaced by a fake runner that "installs" by unzipping the wheel.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
_PKG = ROOT / "factory" / "extensions" / "qualcomm-skills" / "qcskills" / "packages.py"
_spec = importlib.util.spec_from_file_location("qcskills_packages_under_test", _PKG)
assert _spec and _spec.loader
pk = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pk
_spec.loader.exec_module(pk)


def wheel_bytes(
    name: str = "demo_board_mcp",
    version: str = "1.2",
    *,
    author: str = "Qualcomm Omega",
    server: str | None = "from mcp.server.fastmcp import FastMCP\nmcp = FastMCP('x')\nmcp.run()\n",
    requires: tuple[str, ...] = ("mcp<2.0,>=1.2.0",),
    requires_python: str = ">=3.10",
    skills: int = 3,
    extra: dict[str, bytes] | None = None,
) -> bytes:
    dist = name.replace("_", "-")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/__init__.py", "")
        for member, data in (extra or {}).items():
            zf.writestr(member, data)
        if server is not None:
            zf.writestr(f"{name}/server.py", server)
        for i in range(skills):
            zf.writestr(f"{name}/_skills_bundle/area/skill{i}/SKILL.md", f"---\nname: s{i}\n---\n")
        meta = [
            "Metadata-Version: 2.1",
            f"Name: {dist}",
            f"Version: {version}",
            f"Author: {author}",
            f"Summary: MCP server exposing {dist} skills",
            f"Requires-Python: {requires_python}",
        ]
        meta += [f"Requires-Dist: {r}" for r in requires]
        meta += ['Requires-Dist: pytest>=8; extra == "dev"']
        zf.writestr(f"{name}-{version}.dist-info/METADATA", "\n".join(meta) + "\n")
        zf.writestr(f"{name}-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    return buf.getvalue()


def zipped(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for k, v in files.items():
            zf.writestr(k, v)
    return buf.getvalue()


def delivery(**wheel_kw: Any) -> bytes:
    """outer.zip -> rev.zip -> HS/Authorized.zip -> {README, wheel, pdf, skills/}."""
    name = wheel_kw.get("name", "demo_board_mcp")
    version = wheel_kw.get("version", "1.2")
    inner = zipped(
        {
            "README.md": "# demo-board-mcp\n\npip install the wheel, then run it.\n",
            f"{name}-{version}-py3-none-any.whl": wheel_bytes(**wheel_kw),
            "Release_Notes.pdf": b"%PDF-1.4",
            "demo-skills/area/skill0/SKILL.md": "---\nname: s0\n---\n",
        }
    )
    rev = zipped({"HS00-1/Demo_Authorized.zip": inner})
    return zipped({"HS00-1_rev_1.1.zip": rev, "Cover Page.pdf": b"%PDF", "Legal.pdf": b"%PDF"})


class FakePip:
    """Unzips wheels into --target; dependency-only calls succeed without extraction."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode, self.stderr = returncode, stderr

    def __call__(self, cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        wheel_arg = next((a for a in cmd if a.endswith(".whl")), None)
        if self.returncode == 0 and wheel_arg is not None:
            target = Path(cmd[cmd.index("--target") + 1])
            with zipfile.ZipFile(wheel_arg) as zf:
                zf.extractall(target)
        return subprocess.CompletedProcess(cmd, self.returncode, "", self.stderr)


@pytest.fixture
def store(tmp_path: Path):
    pip = FakePip()
    s = pk.Packages(tmp_path / "data", python="PY", runner=pip, proxy_url=lambda: None)
    s.pip = pip  # type: ignore[attr-defined]
    return s


# -- unpacking ----------------------------------------------------------------------


def test_three_levels_are_unpacked_and_the_wheel_is_found(store) -> None:
    preview = store.stage(delivery(), "HS00-1.zip")
    assert preview["name"] == "demo-board-mcp" and preview["version"] == "1.2"
    assert preview["nesting"] == ["HS00-1.zip", "HS00-1_rev_1.1.zip", "Demo_Authorized.zip"]
    assert preview["skill_count"] == 3  # embedded in the wheel, not the loose skills dir
    assert preview["server"] == "demo_board_mcp.server"
    assert preview["readme"].startswith("# demo-board-mcp")
    assert set(preview["documents"]) == {"Release_Notes.pdf", "Cover Page.pdf", "Legal.pdf"}
    assert preview["requires"] == ["mcp<2.0,>=1.2.0"]  # extras dropped
    assert preview["python_ok"] is True and preview["replaces"] == []
    assert store.installed() == []  # a preview installs nothing


@pytest.mark.parametrize(
    ("member", "code"),
    [
        ("../evil.txt", "unsafe_archive"),
        ("/abs.txt", "unsafe_archive"),
        ("C:/x.txt", "unsafe_archive"),
        ("a/../../evil.txt", "unsafe_archive"),
    ],
)
def test_unsafe_members_are_refused(store, member: str, code: str) -> None:
    with pytest.raises(pk.PackageError) as exc:
        store.stage(zipped({member: "x"}))
    assert exc.value.code == f"qcskills.{code}"
    assert not any(store.staging_dir.iterdir())  # nothing left behind


def test_a_symlink_member_is_refused(store) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(pk.PackageError, match="symbolic link"):
        store.stage(buf.getvalue())


def test_nesting_is_limited(store, monkeypatch) -> None:
    monkeypatch.setattr(pk, "MAX_NESTING", 2)
    with pytest.raises(pk.PackageError) as exc:
        store.stage(delivery())
    assert exc.value.code == "qcskills.too_deep"


def test_size_is_counted_while_unpacking(store, monkeypatch) -> None:
    monkeypatch.setattr(pk, "MAX_TOTAL_BYTES", 1000)
    with pytest.raises(pk.PackageError) as exc:
        store.stage(zipped({"big.bin": b"0" * 5000}))
    assert exc.value.code == "qcskills.too_large"


def test_a_fake_zip_inside_is_left_alone(store) -> None:
    data = zipped(
        {
            "notes.zip": b"this is not a zip",
            "w/README.md": "Qualcomm",
            "w/demo_board_mcp-1.2-py3-none-any.whl": wheel_bytes(),
        }
    )
    assert store.stage(data)["name"] == "demo-board-mcp"


def test_not_a_zip(store) -> None:
    with pytest.raises(pk.PackageError) as exc:
        store.stage(b"definitely not a zip")
    assert exc.value.code == "qcskills.bad_archive"


# -- recognising ----------------------------------------------------------------------


def test_no_wheel_or_no_mcp_server_is_not_a_package(store) -> None:
    with pytest.raises(pk.PackageError) as exc:
        store.stage(zipped({"README.md": "Qualcomm", "skills/a/SKILL.md": "x"}))
    assert exc.value.code == "qcskills.no_wheel"
    with pytest.raises(pk.PackageError, match="no MCP server"):
        store.stage(
            zipped(
                {
                    "README.md": "Qualcomm",
                    "x-1-py3-none-any.whl": wheel_bytes("x", "1", server=None),
                }
            )
        )


def test_a_non_qualcomm_wheel_is_refused(store) -> None:
    data = zipped(
        {"README.md": "my tools", "mine-1-py3-none-any.whl": wheel_bytes("mine", "1", author="Me")}
    )
    with pytest.raises(pk.PackageError) as exc:
        store.stage(data)
    assert exc.value.code == "qcskills.not_qualcomm" and "Skills page" in str(exc.value)


def test_the_newest_of_several_versions_is_used(store) -> None:
    files = {"README.md": "Qualcomm"}
    for v in ("0.1.2", "0.1.13", "0.1.10"):
        files[f"quts_tool_mcp-{v}-py3-none-any.whl"] = wheel_bytes("quts_tool_mcp", v)  # type: ignore[assignment]
    assert store.stage(zipped(files))["version"] == "0.1.13"


def test_stdio_flag_is_detected(store) -> None:
    src = (
        "import argparse\np=argparse.ArgumentParser()\n"
        "p.add_argument('--stdio', action='store_true')\n"
    )
    preview = store.stage(delivery(server=src))
    assert preview["server"] == "demo_board_mcp.server --stdio"


def test_requires_python_is_checked() -> None:
    assert pk._python_ok(">=3.10") and pk._python_ok(">=3.9,<4") and pk._python_ok("")
    assert not pk._python_ok(">=99") and not pk._python_ok("<3.0")


# -- install / list / remove -----------------------------------------------------------


def test_install_extracts_into_the_package_dir_and_lists_it(store) -> None:
    preview = store.stage(delivery(requires=()), "HS00-1.zip")
    meta = store.install(preview["token"])
    assert store.pip.calls == []  # no dependencies: pip is not needed
    pkg_dir = store.packages_dir / meta["id"]
    assert (pkg_dir / "site" / "demo_board_mcp" / "server.py").is_file()
    assert (pkg_dir / "README.md").read_text(encoding="utf-8").startswith("# demo-board-mcp")
    assert meta["server_name"] == "demo-board" and meta["source"] == "HS00-1.zip"
    assert [p["id"] for p in store.installed()] == [meta["id"]]
    assert not any(store.staging_dir.iterdir())  # staging cleaned

    (spec,) = store.server_specs()
    assert spec["name"] == "demo-board" and spec["command"] == "PY"
    assert spec["args"] == ["-m", "demo_board_mcp.server"]
    assert (
        spec["env"]["PYTHONPATH"] == str(pkg_dir / "site")
        and spec["env"]["PYTHONNOUSERSITE"] == "1"
    )


def test_offline_install_and_proxy(tmp_path: Path, monkeypatch) -> None:
    pip = FakePip()
    store = pk.Packages(
        tmp_path, python="PY", runner=pip, proxy_url=lambda: "http://u:p@proxy:8080"
    )
    store.install(store.stage(delivery(requires=()))["token"], allow_network=False)
    assert pip.calls == []
    monkeypatch.setattr(
        pk.md, "version", lambda _name: (_ for _ in ()).throw(pk.md.PackageNotFoundError())
    )
    with pytest.raises(pk.PackageError) as exc:
        store.install(store.stage(delivery(version="1.3"))["token"], allow_network=False)
    assert exc.value.code == "qcskills.dependencies" and pip.calls == []
    store.install(store.stage(delivery(version="1.4"))["token"])
    (cmd,) = pip.calls
    assert "--only-binary=:all:" in cmd and cmd[cmd.index("--proxy") + 1] == "http://u:p@proxy:8080"


def test_pip_failure_leaves_nothing_and_hides_the_proxy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        pk.md, "version", lambda _name: (_ for _ in ()).throw(pk.md.PackageNotFoundError())
    )
    pip = FakePip(
        returncode=1,
        stderr="ERROR: Could not find a version that satisfies PyMuPDF\nvia http://u:p@proxy:8080",
    )
    store = pk.Packages(
        tmp_path, python="PY", runner=pip, proxy_url=lambda: "http://u:p@proxy:8080"
    )
    token = store.stage(delivery())["token"]
    with pytest.raises(pk.PackageError) as exc:
        store.install(token)
    assert exc.value.code == "qcskills.pip_failed" and "network" in str(exc.value)
    assert "u:p" not in str(exc.value) and "<proxy>" in str(exc.value)
    assert store.installed() == [] and not list(store.packages_dir.glob(".installing-*"))


def test_a_new_version_replaces_the_old_one(store) -> None:
    store.install(store.stage(delivery(version="1.2"))["token"])
    preview = store.stage(delivery(version="1.3"))
    assert preview["replaces"] == ["1.2"]
    store.install(preview["token"])
    assert [p["version"] for p in store.installed()] == ["1.3"]


def test_remove_and_unknown_ids(store) -> None:
    meta = store.install(store.stage(delivery())["token"])
    store.remove(meta["id"])
    assert store.installed() == [] and store.server_specs() == []
    for bad in (meta["id"], "../data", "..", "a/b"):
        with pytest.raises(pk.PackageError) as exc:
            store.remove(bad)
        assert exc.value.code == "qcskills.unknown_package"


def test_tokens_are_checked(store) -> None:
    for bad in ("nope", "../../x", "0" * 24):
        with pytest.raises(pk.PackageError) as exc:
            store.install(bad)
        assert exc.value.code == "qcskills.unknown_token"


def test_expired_staging_is_swept(tmp_path: Path) -> None:
    now = [1000.0]
    store = pk.Packages(tmp_path, python="PY", runner=FakePip(), clock=lambda: now[0])
    token = store.stage(delivery())["token"]
    os.utime(store.staging_dir / token, (0, 0))
    now[0] = 10_000_000.0
    store.stage(delivery())
    assert not (store.staging_dir / token).exists()


def test_server_name() -> None:
    assert pk._server_name("dragonwing-windows-mcp") == "dragonwing-windows"
    assert pk._server_name("sm8975-mcp") == "sm8975"
    assert pk._server_name("quts-skill-validator-mcp") == "quts-skill-validator"
    assert pk._server_name("Weird__Name..mcp") == "weird-name"


def test_package_json_is_valid(store) -> None:
    meta = store.install(store.stage(delivery())["token"])
    on_disk = json.loads(
        (store.packages_dir / meta["id"] / "package.json").read_text(encoding="utf-8")
    )
    assert on_disk == meta


@pytest.mark.parametrize(
    ("requirement", "expected"),
    [("demo-dependency>=2", ["demo-dependency>=2"]), ("demo-dependency>=1", [])],
)
def test_missing_requirements_checks_installed_version(store, monkeypatch, requirement, expected):
    monkeypatch.setattr(pk.md, "version", lambda _name: "1.0")
    assert store.missing_requirements([requirement]) == expected


def test_missing_requirements_skips_false_marker(store, monkeypatch):
    def unexpected_lookup(_name):
        pytest.fail("a false marker must not query the installed distribution")

    monkeypatch.setattr(pk.md, "version", unexpected_lookup)
    assert store.missing_requirements(['demo-dependency>=2; python_version < "0"']) == []


def test_nested_wheel_keeps_basename_and_short_path(store):
    basename = "demo_board_mcp-1.2-py3-none-any.whl"
    deep_parent = "/".join(["long-directory" * 4] * 10)
    archive = zipped({"inner.zip": zipped({f"{deep_parent}/{basename}": wheel_bytes(requires=())})})
    preview = store.stage(archive)
    work = store.staging_dir / preview["token"]
    staged = json.loads((work / "staged.json").read_text(encoding="utf-8"))
    wheel_path = work / staged["wheel"]
    assert wheel_path.name == basename
    assert len(wheel_path.relative_to(work / "unpacked").parts) == 3
    assert len(wheel_path.parent.name) == 8
    assert preview["wheel"] == basename
    meta = store.install(preview["token"])
    assert (store.packages_dir / meta["id"] / "site" / "demo_board_mcp" / "server.py").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows path limit")
def test_install_wheel_with_paths_over_windows_max_path(store):
    long_dir = "/".join(["skill-directory-" * 3] * 4)
    name = f"demo_board_mcp/_skills_bundle/{long_dir}/scripts/test_script.py"
    wheel = wheel_bytes(requires=(), extra={name: b"print('ok')"})
    preview = store.stage(
        zipped({"inner.zip": zipped({"demo_board_mcp-1.2-py3-none-any.whl": wheel})})
    )
    meta = store.install(preview["token"])
    installed = store.packages_dir / meta["id"] / "site" / Path(name)
    assert len(str(installed)) > 260
    assert Path("\\\\?\\" + str(installed.resolve())).read_bytes() == b"print('ok')"
    store._rm(store.packages_dir / meta["id"])


def test_pip_failure_preserves_previous_version(store, monkeypatch):
    old = store.install(store.stage(delivery(version="1.2", requires=()))["token"])
    old_dir = store.packages_dir / old["id"]
    old_server = (old_dir / "site" / "demo_board_mcp" / "server.py").read_bytes()
    monkeypatch.setattr(pk.md, "version", lambda _name: "1.0")
    preview = store.stage(delivery(version="1.3", requires=("demo-dependency>=2",)))
    pip = store.pip

    def failing_runner(cmd, timeout):
        pip.returncode = 1
        pip.stderr = "ERROR: synthetic pip failure"
        return pip(cmd, timeout)

    monkeypatch.setattr(store, "_runner", failing_runner)
    with pytest.raises(pk.PackageError) as exc:
        store.install(preview["token"])
    assert exc.value.code == "qcskills.pip_failed"
    assert store.installed() == [old]
    assert (old_dir / "site" / "demo_board_mcp" / "server.py").read_bytes() == old_server
    assert not (store.packages_dir / preview["id"]).exists()
    assert not list(store.packages_dir.glob(".installing-*"))
    assert not list(store.packages_dir.glob(".backup-*"))
    assert (store.staging_dir / preview["token"]).is_dir()


def test_fake_pip_accepts_dependency_only_install(tmp_path):
    pip = FakePip()
    cmd = ["PY", "-m", "pip", "install", "--target", str(tmp_path), "demo-dependency>=2"]
    assert pip(cmd, 1).returncode == 0
    assert pip.calls == [cmd]


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths")
def test_rm_removes_windows_long_path(tmp_path):
    root = tmp_path / "installed"
    leaf = root.joinpath(*(["nested-directory-" * 3] * 6))
    extended_leaf = Path("\\\\?\\" + str(leaf.resolve()))
    extended_leaf.mkdir(parents=True)
    (extended_leaf / "server.py").write_text("# synthetic server", encoding="utf-8")
    assert len(str(leaf)) > 260
    pk.Packages._rm(root)
    assert not root.exists()
