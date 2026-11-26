# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Extension-shipped skills: manifest fields + skill-root collection."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import pytest

from qai.platform.user_extensions import (
    HOST_EDITION,
    ManifestError,
    UserExtensionHost,
    parse_manifest,
)

from .conftest import FakeSink


def _skill(root: Path, skill_id: str, *, body: str = "Does things.") -> Path:
    d = root / skill_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {body}\n---\n# {skill_id}\n", encoding="utf-8"
    )
    return d


def _parse(**over: Any):
    data = {"name": "demo-ext", "version": "1", "api_version": 2, "description": "d", **over}
    return parse_manifest(data, root=Path("/x/demo-ext"))


# -- manifest -----------------------------------------------------------------


def test_host_edition_is_external() -> None:
    assert HOST_EDITION == "external"


def test_skills_defaults() -> None:
    s = _parse().skills
    assert s.skills_dir == "skills"
    assert s.excluded == ()
    assert s.default_modes == ()


def test_skills_entries_edition_and_default_mode() -> None:
    """Shape of QAIModelBuilder builder-suite's manifest."""
    s = _parse(
        skills_dir="skills",
        skills=[
            {"dir": "skills/internal-only", "editions": ["internal"]},
            {"dir": "skills/both", "editions": ["internal", "external"], "default_mode": "both"},
            {"dir": "skills/nomode", "editions": ["external"]},
            {"dir": "skills/int-mode", "editions": ["internal"], "default_mode": "off"},
        ],
        edition_paths={"skills/both/internal-notes": ["internal"], "skills/shared": ["external"]},
    ).skills
    assert s.excluded == ("skills/internal-only", "skills/int-mode", "skills/both/internal-notes")
    assert s.default_modes == (("both", "both"),)  # internal-only entry's mode is dropped


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"skills_dir": "../outside"}, "skills_dir"),
        ({"skills_dir": "/abs"}, "skills_dir"),
        ({"skills_dir": "a/../b"}, "skills_dir"),
        ({"skills": [{"dir": "other/x", "editions": ["external"]}]}, "inside skills_dir"),
        ({"skills": [{"dir": "skills/x"}]}, "editions"),
        ({"skills": [{"dir": "skills/x", "editions": []}]}, "editions"),
        ({"skills": [{"dir": "skills/x", "editions": ["pro"]}]}, "editions"),
        (
            {"skills": [{"dir": "skills/x", "editions": ["external"], "default_mode": "on"}]},
            "default_mode",
        ),
        ({"skills": "skills/x"}, "skills must be an array"),
        ({"edition_paths": {"skills/x": "external"}}, "edition_paths"),
        ({"edition_paths": ["skills/x"]}, "edition_paths must be an object"),
    ],
)
def test_bad_skills_fields(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        _parse(**over)


def test_skills_fields_no_longer_warn() -> None:
    m = _parse(skills_dir="skills", skills=[{"dir": "skills/a", "editions": ["external"]}])
    assert not any("skills" in w for w in m.warnings)


# -- host: collecting skill roots ---------------------------------------------


def _ext(make_ext, name: str, **manifest: Any) -> Path:
    data = {"name": name, "version": "1", "api_version": 2, "description": "d", **manifest}
    return make_ext(name, manifest=data)


def test_data_only_extension_contributes_skills(make_ext, sink: FakeSink) -> None:
    ext = _ext(make_ext, "docs-ext")
    _skill(ext / "skills", "docs-helper")
    _skill(ext / "skills", "docs-search")
    (ext / "skills" / "not-a-skill").mkdir()  # no SKILL.md
    (ext / "skills" / "README.md").write_text("x", encoding="utf-8")
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()

    assert status.state == "loaded"
    assert status.skills == ("docs-helper", "docs-search")
    (root,) = host.skill_roots()
    assert root.extension == "docs-ext"
    assert root.path == ext / "skills"
    assert root.excluded == ()


def test_custom_skills_dir(make_ext, sink: FakeSink) -> None:
    ext = _ext(make_ext, "docs-ext", skills_dir="content/skills")
    _skill(ext / "content" / "skills", "alpha")
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()
    assert status.skills == ("alpha",)


def test_missing_explicit_skills_dir_warns(make_ext, sink: FakeSink) -> None:
    _ext(make_ext, "docs-ext", skills_dir="nope")
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()
    assert status.state == "loaded"
    assert host.skill_roots() == ()
    assert any("'nope' does not exist" in w for w in status.warnings)


def test_default_skills_dir_absent_is_silent(make_ext, sink: FakeSink) -> None:
    _ext(make_ext, "docs-ext")
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()
    assert status.warnings == []
    assert host.skill_roots() == ()


def test_edition_excluded_skill_is_hidden(make_ext, sink: FakeSink) -> None:
    ext = _ext(
        make_ext,
        "docs-ext",
        skills=[{"dir": "skills/secret", "editions": ["internal"]}],
    )
    _skill(ext / "skills", "secret")
    _skill(ext / "skills", "public")
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()
    assert status.skills == ("public",)
    assert ext / "skills" / "secret" in host.skill_roots()[0].excluded


def test_id_taken_by_app_skill_is_refused(make_ext, sink: FakeSink) -> None:
    ext = _ext(make_ext, "docs-ext")
    _skill(ext / "skills", "model-builder")
    _skill(ext / "skills", "docs-helper")
    host = UserExtensionHost(
        make_ext.root, sink, taken_skill_ids=lambda: frozenset({"model-builder"})
    )
    (status,) = host.load_all()
    assert status.skills == ("docs-helper",)
    assert any("'model-builder' refused" in w for w in status.warnings)
    assert ext / "skills" / "model-builder" in host.skill_roots()[0].excluded


def test_id_taken_by_earlier_extension_is_refused(make_ext, sink: FakeSink) -> None:
    a = _ext(make_ext, "a-ext")
    b = _ext(make_ext, "b-ext")
    _skill(a / "skills", "shared")
    _skill(b / "skills", "shared")
    _skill(b / "skills", "own")
    host = UserExtensionHost(make_ext.root, sink)
    sa, sb = host.load_all()
    assert sa.skills == ("shared",)
    assert sb.skills == ("own",)
    assert any("'shared' refused" in w for w in sb.warnings)


def test_failed_extension_contributes_no_skills(make_ext, sink: FakeSink) -> None:
    code = "def build_services(ctx):\n    raise RuntimeError('x')\n"
    ext = make_ext("demo-ext", tools=[], code=code)
    _skill(ext / "skills", "would-be")
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()
    assert status.state == "failed"
    assert host.skill_roots() == ()


def test_extension_with_code_and_skills(make_ext, sink: FakeSink) -> None:
    ext = make_ext("demo-ext", tools=[], code="def build_services(ctx):\n    return None\n")
    _skill(ext / "skills", "demo-skill")
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()
    assert status.state == "loaded"
    assert status.skills == ("demo-skill",)


def test_skill_default_modes_only_for_visible_skills(make_ext, sink: FakeSink) -> None:
    ext = _ext(
        make_ext,
        "docs-ext",
        skills=[
            {"dir": "skills/both-skill", "editions": ["external"], "default_mode": "both"},
            {"dir": "skills/taken", "editions": ["external"], "default_mode": "local"},
        ],
    )
    _skill(ext / "skills", "both-skill")
    _skill(ext / "skills", "taken")
    host = UserExtensionHost(make_ext.root, sink, taken_skill_ids=lambda: frozenset({"taken"}))
    host.load_all()
    assert host.skill_default_modes() == {"both-skill": "both"}


def test_skill_policy_json_warns(make_ext, sink: FakeSink) -> None:
    ext = _ext(make_ext, "docs-ext")
    d = _skill(ext / "skills", "guarded")
    (d / "skill.policy.json").write_text("{}", encoding="utf-8")
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()
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


def test_skill_escaping_through_link_is_excluded(make_ext, sink: FakeSink, tmp_path: Path) -> None:
    ext = _ext(make_ext, "docs-ext")
    (ext / "skills").mkdir()
    outside = _skill(tmp_path / "elsewhere", "stolen")
    if not _link_dir(ext / "skills" / "stolen", outside):
        pytest.skip("directory links not supported here")
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()
    assert status.skills == ()
    assert any("'stolen' resolves outside" in w for w in status.warnings)
    assert ext / "skills" / "stolen" in host.skill_roots()[0].excluded


def test_skills_dir_escaping_through_link_is_ignored(
    make_ext, sink: FakeSink, tmp_path: Path
) -> None:
    ext = _ext(make_ext, "docs-ext")
    outside = tmp_path / "elsewhere"
    _skill(outside, "stolen")
    if not _link_dir(ext / "skills", outside):
        pytest.skip("directory links not supported here")
    host = UserExtensionHost(make_ext.root, sink)
    (status,) = host.load_all()
    assert host.skill_roots() == ()
    assert any("resolves outside the extension" in w for w in status.warnings)


def test_unload_all_clears_skill_roots(make_ext, sink: FakeSink) -> None:
    ext = _ext(make_ext, "docs-ext")
    _skill(ext / "skills", "docs-helper")
    host = UserExtensionHost(make_ext.root, sink)
    host.load_all()
    host.unload_all()
    assert host.skill_roots() == ()
