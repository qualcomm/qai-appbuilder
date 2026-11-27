# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Installing from a zip archive or a folder: stage (validate, run nothing) -> commit."""

from __future__ import annotations

import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from qai.platform.user_extensions import (
    ExtensionInstaller,
    InstallError,
    discover_extension_dirs,
    installer as inst,
)

from .conftest import manifest, tool_decl

_MANIFEST = manifest(
    "demo-ext",
    entry="main.py",
    tools=[tool_decl("demo_echo", "command")],
    mcp_servers=[{"name": "srv", "command": "x"}],
)
_CODE = "raise RuntimeError('must never run while staging')\n"


def _zip(files: dict[str, str | bytes], *, links: tuple[str, ...] = ()) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    buf.seek(0)
    return buf


def _ext_files(prefix: str = "", data=_MANIFEST) -> dict[str, str]:
    return {
        f"{prefix}extension.json": json.dumps(data),
        f"{prefix}main.py": _CODE,
        f"{prefix}skills/demo-skill/SKILL.md": "---\nname: demo-skill\ndescription: d\n---\n",
    }


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "extensions"
    r.mkdir()
    return r


@pytest.mark.parametrize("prefix", ["", "demo-ext/", "anything-1.2/"])
def test_stage_zip_summary_and_commit(root: Path, prefix: str) -> None:
    installer = ExtensionInstaller(root)
    staged = installer.stage_zip(_zip(_ext_files(prefix)))
    s = staged.summary()
    assert (s["name"], s["version"], s["runs_code"], s["replaces"]) == (
        "demo-ext",
        "1.0.0",
        True,
        False,
    )
    assert s["tools"] == [{"name": "demo_echo", "group": "command"}]
    assert s["skills"] == ["demo-skill"]
    assert s["mcp_servers"] == [
        {"name": "demo-ext-srv", "transport": "stdio", "command": "x", "url": None}
    ]
    assert s["files"] == 3
    assert discover_extension_dirs(root) == []  # staging is invisible to the scanner

    m = installer.commit(staged.token)
    assert m.root == root / "demo-ext"
    assert (root / "demo-ext" / "main.py").read_text() == _CODE
    assert [p.name for p in discover_extension_dirs(root)] == ["demo-ext"]
    assert not any(installer.staging_root.iterdir())


@pytest.mark.parametrize(
    ("files", "links", "code"),
    [
        ({"../evil.py": "x", **_ext_files()}, (), "unsafe_archive"),
        ({"/abs.py": "x", **_ext_files()}, (), "unsafe_archive"),
        ({"C:/abs.py": "x", **_ext_files()}, (), "unsafe_archive"),
        ({"a\\..\\..\\evil.py": "x", **_ext_files()}, (), "unsafe_archive"),
        (_ext_files(), ("link",), "unsafe_archive"),
        ({"a/extension.json": "{}", "b/extension.json": "{}"}, (), "no_manifest"),
        ({"readme.txt": "x"}, (), "no_manifest"),
        ({"extension.json": "{not json"}, (), "invalid_manifest"),
        ({"extension.json": json.dumps({"name": "../x"})}, (), "invalid_manifest"),
        ({"extension.json": json.dumps({"name": ".staging"})}, (), "invalid_manifest"),
        (
            {"extension.json": json.dumps({"name": "demo-ext", "version": "1"})},
            (),
            "invalid_manifest",
        ),
    ],
)
def test_bad_archives_are_refused_and_leave_nothing(
    root: Path, tmp_path: Path, files, links, code: str
) -> None:
    installer = ExtensionInstaller(root)
    with pytest.raises(InstallError) as exc:
        installer.stage_zip(_zip(files, links=links))
    assert exc.value.code == f"extensions.{code}"
    assert not any(installer.staging_root.iterdir())
    assert not (tmp_path / "evil.py").exists()


def test_not_a_zip(root: Path) -> None:
    with pytest.raises(InstallError, match="not a zip") as exc:
        ExtensionInstaller(root).stage_zip(io.BytesIO(b"plain text"))
    assert exc.value.code == "extensions.bad_archive"


def test_size_limits(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inst, "MAX_TOTAL_BYTES", 1000)
    with pytest.raises(InstallError) as exc:
        ExtensionInstaller(root).stage_zip(_zip({**_ext_files(), "big.bin": b"0" * 2000}))
    assert exc.value.code == "extensions.too_large"
    monkeypatch.setattr(inst, "MAX_FILES", 2)
    with pytest.raises(InstallError) as exc:
        ExtensionInstaller(root).stage_zip(_zip(_ext_files()))
    assert exc.value.code == "extensions.too_many_files"


def test_forged_header_sizes_never_get_past_the_cap(root: Path, monkeypatch) -> None:
    """Declared sizes are forgeable: refused (CRC / real byte count), never a crash."""
    monkeypatch.setattr(inst, "MAX_TOTAL_BYTES", 1000)
    buf = _zip({**_ext_files(), "big.bin": b"0" * 5000})
    real_infolist = zipfile.ZipFile.infolist

    def lying(self):
        infos = real_infolist(self)
        for i in infos:
            i.file_size = 1
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", lying)
    installer = ExtensionInstaller(root)
    with pytest.raises(InstallError) as exc:
        installer.stage_zip(buf)
    assert exc.value.code in {"extensions.too_large", "extensions.bad_archive"}
    assert not any(installer.staging_root.iterdir())


def test_a_corrupt_member_is_a_bad_archive(root: Path) -> None:
    raw = bytearray(_zip(_ext_files()).getvalue())
    at = raw.index(b"raise RuntimeError")
    raw[at : at + 5] = b"XXXXX"  # body no longer matches its CRC
    with pytest.raises(InstallError) as exc:
        ExtensionInstaller(root).stage_zip(io.BytesIO(bytes(raw)))
    assert exc.value.code == "extensions.bad_archive"


def test_existing_extension_needs_replace_and_is_restored_on_failure(root: Path) -> None:
    installer = ExtensionInstaller(root)
    installer.commit(installer.stage_zip(_zip(_ext_files())).token)
    newer = dict(_MANIFEST, version="2.0.0")
    staged = installer.stage_zip(_zip(_ext_files(data=newer)))
    assert staged.summary()["installed_version"] == "1.0.0"
    assert staged.summary()["replaces"] is True

    with pytest.raises(InstallError) as exc:
        installer.commit(staged.token)
    assert exc.value.code == "extensions.exists"
    assert json.loads((root / "demo-ext" / "extension.json").read_text())["version"] == "1.0.0"

    installer.commit(staged.token, replace=True)
    assert json.loads((root / "demo-ext" / "extension.json").read_text())["version"] == "2.0.0"
    assert not (root / ".trash").exists() or not any((root / ".trash").iterdir())


def test_stage_folder_copies_without_links_and_caches(root: Path, tmp_path: Path) -> None:
    src = tmp_path / "src" / "whatever"
    for rel, content in _ext_files().items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(content, encoding="utf-8")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"x")
    installer = ExtensionInstaller(root)
    staged = installer.stage_folder(src)
    assert staged.manifest.name == "demo-ext"
    installer.commit(staged.token)
    assert not (root / "demo-ext" / "__pycache__").exists()
    assert (src / "main.py").exists()  # the source is left alone


def _entries(files: dict[str, str | bytes]) -> list[tuple[str, io.BytesIO]]:
    return [(n, io.BytesIO(c.encode() if isinstance(c, str) else c)) for n, c in files.items()]


def test_stage_files_from_a_browser_picked_folder(root: Path) -> None:
    files = {
        **_ext_files(),
        "__pycache__/main.cpython-313.pyc": b"x",
        "sub/__pycache__/m.pyc": b"x",
        ".git/config": b"x",
    }
    installer = ExtensionInstaller(root)
    staged = installer.stage_files(_entries(files))
    assert staged.manifest.name == "demo-ext" and staged.files == 3
    assert staged.skills == ("demo-skill",)
    installer.commit(staged.token)
    assert (root / "demo-ext" / "main.py").is_file()
    assert not (root / "demo-ext" / "__pycache__").exists()
    assert not (root / "demo-ext" / ".git").exists()
    assert not any(p.name == "raw" for p in installer.staging_root.rglob("*"))


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("../evil.py", "extensions.unsafe_archive"),
        ("a/../../evil.py", "extensions.unsafe_archive"),
        ("C:/evil.py", "extensions.unsafe_archive"),
        ("/abs/evil.py", "extensions.unsafe_archive"),
    ],
)
def test_stage_files_refuses_unsafe_paths(root: Path, name: str, code: str) -> None:
    installer = ExtensionInstaller(root)
    with pytest.raises(InstallError) as exc:
        installer.stage_files(_entries({**_ext_files(), name: b"x"}))
    assert exc.value.code == code
    assert not any(installer.staging_root.iterdir())  # nothing left behind
    assert not (root.parent / "evil.py").exists()


def test_stage_files_limits_and_missing_manifest(root: Path, monkeypatch) -> None:
    installer = ExtensionInstaller(root)
    with pytest.raises(InstallError) as exc:
        installer.stage_files(_entries({"main.py": "x"}))
    assert exc.value.code == "extensions.no_manifest"
    monkeypatch.setattr(inst, "MAX_FILES", 2)
    with pytest.raises(InstallError) as exc:
        installer.stage_files(_entries(_ext_files()))
    assert exc.value.code == "extensions.too_many_files"
    monkeypatch.setattr(inst, "MAX_FILES", 5000)
    monkeypatch.setattr(inst, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(InstallError) as exc:
        installer.stage_files(_entries(_ext_files()))
    assert exc.value.code == "extensions.too_large"
    assert not any(installer.staging_root.iterdir())


def test_stage_folder_refusals(root: Path, tmp_path: Path) -> None:
    installer = ExtensionInstaller(root)
    with pytest.raises(InstallError) as exc:
        installer.stage_folder(tmp_path / "nope")
    assert exc.value.code == "extensions.not_a_folder"
    (tmp_path / "empty").mkdir()
    with pytest.raises(InstallError) as exc:
        installer.stage_folder(tmp_path / "empty")
    assert exc.value.code == "extensions.no_manifest"
    inside = root / "demo-ext"
    inside.mkdir()
    (inside / "extension.json").write_text(json.dumps(_MANIFEST), encoding="utf-8")
    with pytest.raises(InstallError) as exc:
        installer.stage_folder(inside)
    assert exc.value.code == "extensions.already_in_place"


def test_tokens(root: Path) -> None:
    installer = ExtensionInstaller(root)
    for bad in ("", "../../x", "zz" * 8, "0" * 15):
        with pytest.raises(InstallError):
            installer.commit(bad)
        assert installer.discard(bad) is False
    token = installer.stage_zip(_zip(_ext_files())).token
    assert installer.discard(token) is True
    with pytest.raises(InstallError) as exc:
        installer.commit(token)
    assert exc.value.code == "extensions.unknown_token"


def test_expired_staging_is_swept(root: Path) -> None:
    now = [1_000_000.0]
    installer = ExtensionInstaller(root, clock=lambda: now[0])
    old = installer.stage_zip(_zip(_ext_files())).token
    now[0] += 10**10  # far beyond the TTL (mtimes are real, the clock is not)
    installer.stage_zip(_zip(_ext_files()))
    assert not (installer.staging_root / old).exists()
