# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Extension-shipped skills: collecting the skill roots."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import pytest

from .conftest import manifest


def _skill(root: Path, skill_id: str, *, body: str = "Does things.") -> Path:
    d = root / skill_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {body}\n---\n# {skill_id}\n", encoding="utf-8"
    )
    return d


def _ext(make_ext, name: str, **fields: Any) -> Path:
    return make_ext(name, manifest_data=manifest(name, **fields))


def test_data_only_extension_contributes_skills(make_ext, new_host) -> None:
    ext = _ext(make_ext, "docs-ext")
    _skill(ext / "skills", "docs-helper")
    _skill(ext / "skills", "docs-search")
    (ext / "skills" / "not-a-skill").mkdir()  # no SKILL.md
    (ext / "skills" / "README.md").write_text("x", encoding="utf-8")
    host = new_host()
    (status,) = host.load_all()

    assert status.state == "loaded"
    assert status.skills == ("docs-helper", "docs-search")
    (root,) = host.skill_roots()
    assert root.extension == "docs-ext"
    assert root.path == ext / "skills"
    assert root.excluded == ()


def test_custom_skills_dir(make_ext, new_host) -> None:
    ext = _ext(make_ext, "docs-ext", skills={"dir": "content/skills"})
    _skill(ext / "content" / "skills", "alpha")
    (status,) = new_host().load_all()
    assert status.skills == ("alpha",)


def test_missing_explicit_skills_dir_warns(make_ext, new_host) -> None:
    _ext(make_ext, "docs-ext", skills={"dir": "nope"})
    host = new_host()
    (status,) = host.load_all()
    assert status.state == "loaded"
    assert host.skill_roots() == ()
    assert any("'nope' does not exist" in w for w in status.warnings)


def test_default_skills_dir_absent_is_silent(make_ext, new_host) -> None:
    _ext(make_ext, "docs-ext")
    host = new_host()
    (status,) = host.load_all()
    assert status.warnings == []
    assert host.skill_roots() == ()


def test_id_taken_by_app_skill_is_refused(make_ext, new_host) -> None:
    ext = _ext(make_ext, "docs-ext")
    _skill(ext / "skills", "model-builder")
    _skill(ext / "skills", "docs-helper")
    host = new_host(taken_skill_ids=lambda: frozenset({"model-builder"}))
    (status,) = host.load_all()
    assert status.skills == ("docs-helper",)
    assert any("'model-builder' refused" in w for w in status.warnings)
    assert ext / "skills" / "model-builder" in host.skill_roots()[0].excluded


def test_id_taken_by_earlier_extension_is_refused(make_ext, new_host) -> None:
    a = _ext(make_ext, "a-ext")
    b = _ext(make_ext, "b-ext")
    _skill(a / "skills", "shared")
    _skill(b / "skills", "shared")
    _skill(b / "skills", "own")
    sa, sb = new_host().load_all()
    assert sa.skills == ("shared",)
    assert sb.skills == ("own",)
    assert any("'shared' refused" in w for w in sb.warnings)


def test_failed_extension_contributes_no_skills(make_ext, new_host) -> None:
    ext = make_ext("demo-ext", code="def activate(ctx):\n    raise RuntimeError('x')\n")
    _skill(ext / "skills", "would-be")
    host = new_host()
    (status,) = host.load_all()
    assert status.state == "failed"
    assert host.skill_roots() == ()


def test_extension_with_code_and_skills(make_ext, new_host) -> None:
    ext = make_ext("demo-ext", code="def activate(ctx):\n    pass\n")
    _skill(ext / "skills", "demo-skill")
    (status,) = new_host().load_all()
    assert status.state == "loaded"
    assert status.skills == ("demo-skill",)


def test_default_modes_only_for_visible_skills(make_ext, new_host) -> None:
    ext = _ext(
        make_ext,
        "docs-ext",
        skills={"default_modes": {"both-skill": "both", "taken": "local", "ghost": "off"}},
    )
    _skill(ext / "skills", "both-skill")
    _skill(ext / "skills", "taken")
    host = new_host(taken_skill_ids=lambda: frozenset({"taken"}))
    (status,) = host.load_all()
    assert host.skill_default_modes() == {"both-skill": "both"}
    assert any("no skill 'ghost'" in w for w in status.warnings)
    # 'taken' exists (refused, reported as such) -- no second, misleading warning
    assert not any("no skill 'taken'" in w for w in status.warnings)


def test_default_modes_without_skills_dir_warn(make_ext, new_host) -> None:
    _ext(make_ext, "docs-ext", skills={"default_modes": {"ghost": "off"}})
    (status,) = new_host().load_all()
    assert status.state == "loaded"
    assert any("no skill 'ghost'" in w for w in status.warnings)


def test_skill_policy_json_warns(make_ext, new_host) -> None:
    ext = _ext(make_ext, "docs-ext")
    d = _skill(ext / "skills", "guarded")
    (d / "skill.policy.json").write_text("{}", encoding="utf-8")
    (status,) = new_host().load_all()
    assert status.skills == ("guarded",)
    assert any("skill.policy.json is not supported" in w for w in status.warnings)


def _link_dir(link: Path, target: Path) -> bool:
    """Directory link (junction on Windows: no admin needed). False if unsupported."""
    try:
        if os.name == "nt":
            _winapi = importlib.import_module("_winapi")
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except (OSError, AttributeError, NotImplementedError):
        return False
    return True


def test_skill_escaping_through_link_is_excluded(make_ext, new_host, tmp_path: Path) -> None:
    ext = _ext(make_ext, "docs-ext")
    (ext / "skills").mkdir()
    outside = _skill(tmp_path / "elsewhere", "stolen")
    if not _link_dir(ext / "skills" / "stolen", outside):
        pytest.skip("directory links not supported here")
    host = new_host()
    (status,) = host.load_all()
    assert status.skills == ()
    assert any("'stolen' resolves outside" in w for w in status.warnings)
    assert ext / "skills" / "stolen" in host.skill_roots()[0].excluded


def test_skills_dir_escaping_through_link_is_ignored(make_ext, new_host, tmp_path: Path) -> None:
    ext = _ext(make_ext, "docs-ext")
    outside = tmp_path / "elsewhere"
    _skill(outside, "stolen")
    if not _link_dir(ext / "skills", outside):
        pytest.skip("directory links not supported here")
    host = new_host()
    (status,) = host.load_all()
    assert host.skill_roots() == ()
    assert any("resolves outside the extension" in w for w in status.warnings)


def test_unload_all_clears_skill_roots(make_ext, new_host) -> None:
    ext = _ext(make_ext, "docs-ext")
    _skill(ext / "skills", "docs-helper")
    host = new_host()
    host.load_all()
    host.unload_all()
    assert host.skill_roots() == ()
