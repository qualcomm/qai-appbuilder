# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``SkillDiscovery`` contributed roots + ``source`` + contributed default modes."""

from __future__ import annotations

from pathlib import Path

import pytest

from qai.platform.skills import (
    ContributedSkillRoot,
    SkillDiscovery,
    resolve_skill_mode,
    set_contributed_default_modes,
)


def _skill(root: Path, skill_id: str, desc: str = "d") -> Path:
    d = root / skill_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\ndescription: {desc}\n---\n# {skill_id}\n", encoding="utf-8")
    (d / "icon.png").write_bytes(b"\x89PNG")
    return d


@pytest.fixture
def roots(tmp_path: Path):
    user = tmp_path / "user"
    ext = tmp_path / "ext" / "skills"
    _skill(user, "shared", desc="user copy")
    _skill(user, "mine")
    _skill(ext, "shared", desc="extension copy")
    _skill(ext, "ext-only")
    _skill(ext, "hidden")
    return user, ext


def test_without_contributed_roots_behaviour_unchanged(roots) -> None:
    user, _ = roots
    disc = SkillDiscovery([user])
    assert [s.skill_id for s in disc.scan()] == ["mine", "shared"]
    assert all(s.source == "" for s in disc.scan())
    assert disc.contributed_roots() == ()


def test_contributed_roots_append_after_own(roots) -> None:
    user, ext = roots
    disc = SkillDiscovery(
        [user],
        contributed_roots=lambda: [ContributedSkillRoot(path=ext, source="extension:demo")],
    )
    by_id = {s.skill_id: s for s in disc.scan()}
    assert set(by_id) == {"mine", "shared", "ext-only", "hidden"}
    assert by_id["shared"].description == "user copy"  # app copy wins
    assert by_id["shared"].source == ""
    assert by_id["ext-only"].source == "extension:demo"
    assert by_id["ext-only"].to_dict()["source"] == "extension:demo"


def test_contributed_excluded_paths(roots) -> None:
    user, ext = roots
    disc = SkillDiscovery(
        [user],
        contributed_roots=lambda: [
            ContributedSkillRoot(path=ext, source="extension:demo", excluded=(ext / "hidden",))
        ],
    )
    ids = {s.skill_id for s in disc.scan()}
    assert "hidden" not in ids
    assert disc.find("hidden") is None
    assert disc.icon_path("hidden") is None
    assert disc.icon_path("ext-only") == ext / "ext-only" / "icon.png"


def test_contributed_allow_list(roots) -> None:
    """``only``: a skill dir the contributor never vetted is not discovered."""
    user, ext = roots
    disc = SkillDiscovery(
        [user],
        contributed_roots=lambda: [
            ContributedSkillRoot(path=ext, source="extension:demo", only=frozenset({"ext-only"}))
        ],
    )
    ids = {s.skill_id for s in disc.scan() if s.source}
    assert ids == {"ext-only"}
    assert disc.icon_path("hidden") is None


def test_provider_is_read_every_scan(roots) -> None:
    user, ext = roots
    current: list[ContributedSkillRoot] = []
    disc = SkillDiscovery([user], contributed_roots=lambda: current)
    assert disc.find("ext-only") is None
    current.append(ContributedSkillRoot(path=ext, source="extension:demo"))
    assert disc.find("ext-only") is not None


def test_skills_dirs_report_only_own_roots(roots) -> None:
    user, ext = roots
    disc = SkillDiscovery(
        [user], contributed_roots=lambda: [ContributedSkillRoot(path=ext, source="x")]
    )
    assert disc.skills_dirs == [user]
    assert disc.skills_dir == user


def test_broken_provider_keeps_app_skills(roots) -> None:
    user, _ = roots

    def _boom():
        raise RuntimeError("boom")

    disc = SkillDiscovery([user], contributed_roots=_boom)
    assert [s.skill_id for s in disc.scan()] == ["mine", "shared"]


@pytest.fixture
def _reset_modes():
    yield
    set_contributed_default_modes(None)


@pytest.mark.usefixtures("_reset_modes")
def test_contributed_default_modes() -> None:
    set_contributed_default_modes(lambda: {"ext-skill": "both", "bad": "on", "ppt-gen": "both"})
    assert resolve_skill_mode(None, False, "ext-skill") == "both"
    assert resolve_skill_mode(None, False, "bad") == "cloud"  # invalid mode ignored
    assert resolve_skill_mode(None, False, "ppt-gen") == "off"  # built-in default wins
    assert resolve_skill_mode({"mode": "local"}, False, "ext-skill") == "local"  # user wins
    set_contributed_default_modes(None)
    assert resolve_skill_mode(None, False, "ext-skill") == "cloud"


@pytest.mark.usefixtures("_reset_modes")
def test_broken_default_mode_provider_degrades() -> None:
    def _boom():
        raise RuntimeError("boom")

    set_contributed_default_modes(_boom)
    assert resolve_skill_mode(None, False, "ext-skill") == "cloud"
