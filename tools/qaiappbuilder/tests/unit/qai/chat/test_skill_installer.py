# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``install_skill``: safe, atomic install of a user's skill (folder or zip) into ``skills/``."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from qai.chat.adapters import skill_installer as si
from qai.chat.adapters.skill_installer import install_skill
from qai.platform.skills.discovery import SkillDiscovery

_MD = "---\nname: {name}\ndescription: does {name} things\ntags: alpha, beta\nuse_for: testing {name}\n---\n\n# {name}\n\nBody.\n"


def _folder(root: Path, name: str = "my-skill", *, files: dict[str, str] | None = None, md: bool = True) -> Path:
    d = root / name
    d.mkdir(parents=True)
    if md:
        (d / "SKILL.md").write_text(_MD.format(name=name), encoding="utf-8")
    for rel, body in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return d


def _zip(path: Path, members: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for k, v in members.items():
            zf.writestr(k, v)
    return path


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    skills = tmp_path / "skills"
    builtin = tmp_path / "builtin"
    skills.mkdir()
    (builtin / "model-hub").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    return skills, builtin, work


def _install(roots, source, **kw):
    skills, builtin, _ = roots
    return install_skill(source, skills_root=skills, builtin_root=builtin, **kw)


def _leftovers(skills: Path) -> list[str]:
    return [p.name for p in skills.iterdir() if p.name.startswith(".")]


# ── folders ─────────────────────────────────────────────────────────────────


def test_a_folder_is_installed_and_discovered(roots) -> None:
    src = _folder(roots[2], files={"scripts/run.py": "print(1)\n", "ref/a.md": "x"})
    res = _install(roots, src)
    assert res.ok, res.error
    assert res.skill_id == "my-skill" and res.files == 3
    found = SkillDiscovery([roots[0]]).find("my-skill")
    assert found is not None and found.tags == ["alpha", "beta"] and found.use_for == "testing my-skill"
    assert (roots[0] / "my-skill" / "scripts" / "run.py").read_text(encoding="utf-8") == "print(1)\n"
    assert res.scripts == ("scripts/run.py",) and res.scripts_total == 1
    assert _leftovers(roots[0]) == []


def test_build_noise_and_links_are_not_copied(roots) -> None:
    src = _folder(roots[2], files={"__pycache__/x.pyc": "b", ".git/config": "c", "keep.py": "k", "m.pyc": "p"})
    res = _install(roots, src)
    assert res.ok, res.error
    names = sorted(p.name for p in (roots[0] / "my-skill").rglob("*") if p.is_file())
    assert names == ["SKILL.md", "keep.py"]


def test_a_folder_without_skill_md_is_rejected(roots) -> None:
    res = _install(roots, _folder(roots[2], md=False, files={"a.txt": "x"}))
    assert not res.ok and res.step == "no_skill_md"
    assert list(roots[0].iterdir()) == []


def test_a_missing_source_is_reported(roots) -> None:
    res = _install(roots, roots[2] / "nope")
    assert not res.ok and res.step == "read_source" and "not found" in res.error


def test_the_path_may_arrive_quoted(roots) -> None:
    src = _folder(roots[2])
    assert _install(roots, f'"{src}"').ok


# ── zips ────────────────────────────────────────────────────────────────────


def test_a_zip_with_skill_md_at_its_root(roots) -> None:
    z = _zip(roots[2] / "pack.zip", {"SKILL.md": _MD.format(name="zipped"), "a/b.txt": "x"})
    res = _install(roots, z)
    assert res.ok, res.error
    assert res.skill_id == "zipped" and (roots[0] / "zipped" / "a" / "b.txt").is_file()


def test_a_zip_wrapped_in_one_folder(roots) -> None:
    z = _zip(roots[2] / "pack.zip", {"inner/SKILL.md": _MD.format(name="inner"), "inner/s/x.py": "1"})
    res = _install(roots, z)
    assert res.ok, res.error
    assert (roots[0] / "inner" / "SKILL.md").is_file() and (roots[0] / "inner" / "s" / "x.py").is_file()


def test_the_zip_extension_is_case_insensitive(roots) -> None:
    z = _zip(roots[2] / "PACK.ZIP", {"SKILL.md": _MD.format(name="upper")})
    assert _install(roots, z).ok


@pytest.mark.parametrize(
    "evil",
    ["../escape.txt", "a/../../escape.txt", "/abs.txt", "C:/win.txt", "C:\\win.txt", "a:stream.txt", "CON", "dir/NUL.txt"],
)
def test_unsafe_zip_members_are_rejected_and_nothing_is_written(roots, evil: str) -> None:
    z = _zip(roots[2] / "bad.zip", {"SKILL.md": _MD.format(name="bad"), evil: "x"})
    res = _install(roots, z)
    assert not res.ok and res.step == "unsafe_archive", res
    assert list(roots[0].iterdir()) == []
    assert not (roots[2].parent / "escape.txt").exists() and not (roots[0] / "escape.txt").exists()


def test_a_symlink_in_a_zip_is_rejected(roots) -> None:
    zpath = roots[2] / "link.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("SKILL.md", _MD.format(name="lnk"))
        info = zipfile.ZipInfo("evil")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    res = _install(roots, zpath)
    assert not res.ok and res.step == "unsafe_archive" and "symbolic link" in res.error


def test_a_zip_without_skill_md_is_rejected(roots) -> None:
    res = _install(roots, _zip(roots[2] / "n.zip", {"a/readme.txt": "x"}))
    assert not res.ok and res.step == "no_skill_md"


def test_a_corrupt_zip_is_rejected(roots) -> None:
    p = roots[2] / "c.zip"
    p.write_bytes(b"this is not a zip")
    res = _install(roots, p)
    assert not res.ok and res.step == "read_source"


def test_size_limits(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(si, "MAX_FILES", 3)
    res = _install(roots, _zip(roots[2] / "many.zip", {"SKILL.md": _MD.format(name="many"), "1": "1", "2": "2", "3": "3"}))
    assert not res.ok and res.step == "too_large" and "files" in res.error
    monkeypatch.setattr(si, "MAX_FILES", 2000)
    monkeypatch.setattr(si, "MAX_FILE_BYTES", 10)
    res = _install(roots, _zip(roots[2] / "big.zip", {"SKILL.md": _MD.format(name="big")}))
    assert not res.ok and res.step == "too_large"
    monkeypatch.setattr(si, "MAX_FILE_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr(si, "MAX_TOTAL_BYTES", 20)
    res = _install(roots, _zip(roots[2] / "tot.zip", {"SKILL.md": _MD.format(name="tot")}))
    assert not res.ok and res.step == "too_large" and "total" in res.error
    assert list(roots[0].iterdir()) == []


def test_an_archive_that_lies_about_its_size_is_rejected(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    z = _zip(roots[2] / "lie.zip", {"SKILL.md": _MD.format(name="lie"), "big.txt": "x" * 1000})
    real_open = zipfile.ZipFile.open

    def lying(self, name, *a, **k):  # declare a tiny size while streaming more
        if getattr(name, "filename", name) == "big.txt":
            info = self.getinfo("big.txt")
            info.file_size = 5
        return real_open(self, name, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "open", lying)
    res = _install(roots, z)
    assert not res.ok and res.step == "unsafe_archive"
    assert list(roots[0].iterdir()) == []


# ── ids ─────────────────────────────────────────────────────────────────────


def test_an_explicit_name_wins(roots) -> None:
    res = _install(roots, _folder(roots[2]), name="other.id")
    assert res.ok and res.skill_id == "other.id" and (roots[0] / "other.id" / "SKILL.md").is_file()


@pytest.mark.parametrize("bad", ["has space", "../up", "a/b", "-x", "é"])
def test_bad_explicit_ids_are_rejected(roots, bad: str) -> None:
    res = _install(roots, _folder(roots[2]), name=bad)
    assert not res.ok and res.step == "validate" and list(roots[0].iterdir()) == []


def test_an_unusable_front_matter_name_falls_back_to_the_source_name(roots) -> None:
    d = _folder(roots[2], "folder-id")
    (d / "SKILL.md").write_text("---\nname: Has Spaces!\ndescription: d\n---\n# T\n", encoding="utf-8")
    res = _install(roots, d)
    assert res.ok and res.skill_id == "folder-id"


def test_a_builtin_id_is_refused_whatever_the_case(roots) -> None:
    res = _install(roots, _folder(roots[2], "model-hub"))
    assert not res.ok and res.step == "validate" and "built-in" in res.error
    res = _install(roots, _folder(roots[2], "x"), name="Model-Hub")
    assert not res.ok and "built-in" in res.error
    assert list(roots[0].iterdir()) == []


# ── replace / atomicity ─────────────────────────────────────────────────────


def test_installing_over_an_existing_skill_needs_replace(roots) -> None:
    assert _install(roots, _folder(roots[2], files={"v.txt": "1"})).ok
    again = _folder(roots[2] / "x", "my-skill", files={"v.txt": "2"})
    res = _install(roots, again)
    assert not res.ok and res.step == "exists" and "replace=true" in res.error
    assert (roots[0] / "my-skill" / "v.txt").read_text(encoding="utf-8") == "1"


def test_replace_upgrades_and_leaves_no_backup(roots) -> None:
    assert _install(roots, _folder(roots[2], files={"old.txt": "1"})).ok
    new = _folder(roots[2] / "x", "my-skill", files={"new.txt": "2"})
    res = _install(roots, new, replace=True)
    assert res.ok and res.replaced
    assert (roots[0] / "my-skill" / "new.txt").is_file() and not (roots[0] / "my-skill" / "old.txt").exists()
    assert _leftovers(roots[0]) == []


def test_a_failed_upgrade_restores_the_previous_version(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _install(roots, _folder(roots[2], files={"old.txt": "1"})).ok
    new = _folder(roots[2] / "x", "my-skill", files={"new.txt": "2"})

    class _Nothing:
        def __init__(self, *_a, **_k) -> None: ...

        def find(self, _id):  # discovery does not see the new tree
            return None

    monkeypatch.setattr(si, "SkillDiscovery", _Nothing)
    res = _install(roots, new, replace=True)
    assert not res.ok and res.step == "not_discovered"
    assert (roots[0] / "my-skill" / "old.txt").read_text(encoding="utf-8") == "1"
    assert not (roots[0] / "my-skill" / "new.txt").exists()
    assert _leftovers(roots[0]) == []


def test_a_half_installed_skill_is_never_visible_to_discovery(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    real_pick = si._pick_id

    def spy(explicit, payload, src):  # runs while the files are staged but not yet installed
        seen.append([s.skill_id for s in SkillDiscovery([roots[0]]).scan()])
        return real_pick(explicit, payload, src)

    monkeypatch.setattr(si, "_pick_id", spy)
    assert _install(roots, _folder(roots[2])).ok
    assert seen == [[]]  # the staged copy (which already holds a SKILL.md) is not listed as a skill


# ── result details ──────────────────────────────────────────────────────────


def test_pinned_is_reported(roots) -> None:
    d = _folder(roots[2])
    (d / "SKILL.md").write_text("---\nname: my-skill\ndescription: d\npinned: true\n---\n# T\n", encoding="utf-8")
    res = _install(roots, d)
    assert res.ok and res.pinned


def test_runnable_files_are_listed_and_capped(roots) -> None:
    files = {f"s/f{i:02d}.py": "1" for i in range(25)}
    res = _install(roots, _folder(roots[2], files=files))
    assert res.ok and res.scripts_total == 25 and len(res.scripts) == si._MAX_LISTED_SCRIPTS


# ── unusual paths ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("dirname", ["with space", "中文目录", "semi;colon", "amp & (1)", "it's [x] #1"])
def test_source_paths_with_unusual_characters(roots, dirname: str) -> None:
    base = roots[2] / dirname
    base.mkdir()
    res = _install(roots, _folder(base, "pretty-skill"))
    assert res.ok, res.error
    z = _zip(base / "z z (1).zip", {"SKILL.md": _MD.format(name="zip-in-odd-dir")})
    assert _install(roots, z).ok


@pytest.mark.skipif(os.name != "nt", reason="MAX_PATH only exists on Windows")
def test_deeply_nested_files_beyond_260_characters(roots) -> None:
    deep = "/".join(["segment_" + "x" * 22] * 9) + "/leaf.txt"
    z = _zip(roots[2] / "deep.zip", {"SKILL.md": _MD.format(name="deepskill"), deep: "x"})
    res = _install(roots, z)
    assert res.ok, res.error
    assert os.path.isfile(si._lp(roots[0] / "deepskill" / deep))


def test_a_transient_access_denied_while_moving_into_place_is_retried(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    real, calls = os.replace, {"n": 0}

    def flaky(src, dst):  # a scanner holds a handle inside the freshly written tree for a moment
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "access denied")
        return real(src, dst)

    monkeypatch.setattr(si.time, "sleep", lambda _s: None)
    monkeypatch.setattr(si.os, "replace", flaky)
    res = _install(roots, _folder(roots[2]))
    assert res.ok, res.error
    assert calls["n"] == 3 and (roots[0] / "my-skill" / "SKILL.md").is_file()


def test_a_permanent_access_denied_fails_cleanly_and_leaves_nothing(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(_src, _dst):
        raise PermissionError(5, "access denied")

    monkeypatch.setattr(si.time, "sleep", lambda _s: None)
    monkeypatch.setattr(si.os, "replace", denied)
    res = _install(roots, _folder(roots[2]))
    assert not res.ok and res.step == "unexpected"
    assert list(roots[0].iterdir()) == []
