# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""The ``skill_install`` tool: argument handling, output text, wiring, and the install-skill skill."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qai.chat.adapters.skill_install_tool import SKILL_INSTALL_TOOL_SCHEMA, SkillInstallToolHandler
from qai.chat.adapters.skill_installer import SkillInstallResult
from qai.chat.adapters.system_prompt_builder import TOOL_USE_PHILOSOPHY_GUIDANCE
from qai.chat.application.use_cases.tool_advertise import SUB_AGENT_EXCLUDED_TOOLS, TOOL_ORDER
from qai.platform.skills.discovery import SkillDiscovery

_ROOT = Path(__file__).resolve().parents[4]
_SKILLS_DIR = _ROOT / "skills"


def _call(result: SkillInstallResult | None = None, **args: Any) -> tuple[str, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def fake(source: str, **kw: Any) -> SkillInstallResult:
        seen.update(kw, source=source)
        return result or SkillInstallResult(ok=True, skill_id="s", path="P", files=2, size=2048, name="s", description="d")

    h = SkillInstallToolHandler(skills_root=Path("S"), builtin_root=Path("B"), install_fn=fake)
    out = asyncio.run(h.execute(SimpleNamespace(arguments=args)))  # type: ignore[arg-type]
    return out, seen


# ── arguments ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [None, "", "   ", 5, ["x"]])
def test_source_is_required(bad: Any) -> None:
    out, seen = _call(source=bad)
    assert "needs 'source'" in out and seen == {}


@pytest.mark.parametrize("bad", ["", "  ", 5])
def test_a_malformed_name_is_rejected_without_installing(bad: Any) -> None:
    out, seen = _call(source="x", name=bad)
    assert "'name' must be a non-empty string" in out and seen == {}


@pytest.mark.parametrize("bad", ["true", 1, None])
def test_replace_must_be_a_boolean(bad: Any) -> None:
    out, seen = _call(source="x", replace=bad)
    assert "'replace' must be true or false" in out and seen == {}


def test_arguments_are_forwarded_and_roots_come_from_the_handler() -> None:
    _out, seen = _call(source=" C:/a b/s.zip ", name=" my-id ", replace=True)
    assert seen["source"] == " C:/a b/s.zip "  # the installer owns path clean-up
    assert (seen["name"], seen["replace"]) == ("my-id", True)
    assert seen["skills_root"] == Path("S") and seen["builtin_root"] == Path("B")


def test_defaults_do_not_replace() -> None:
    _out, seen = _call(source="x")
    assert seen["replace"] is False and seen["name"] is None


# ── output ──────────────────────────────────────────────────────────────────


def test_success_text_names_the_next_turn_and_the_default_mode() -> None:
    out, _ = _call(source="x")
    assert "Installed skill 's'" in out and "NEXT model turn" in out and "cloud" in out


def test_an_upgrade_says_so() -> None:
    out, _ = _call(SkillInstallResult(ok=True, skill_id="s", replaced=True), source="x")
    assert "Upgraded skill 's'" in out


def test_bundled_scripts_are_surfaced_to_the_agent() -> None:
    res = SkillInstallResult(ok=True, skill_id="s", scripts=("a.py", "b.ps1"), scripts_total=5)
    out, _ = _call(res, source="x")
    assert "5 runnable file(s)" in out and "a.py, b.ps1" in out and "+3 more" in out


def test_a_pinned_skill_triggers_a_warning() -> None:
    out, _ = _call(SkillInstallResult(ok=True, skill_id="s", pinned=True), source="x")
    assert "pinned: true" in out and "WARNING" in out


def test_failure_text_carries_the_step_and_reassures() -> None:
    out, _ = _call(SkillInstallResult(ok=False, step="exists", error="already there"), source="x")
    assert "FAILED at step 'exists': already there" in out and "unchanged" in out


def test_an_unexpected_exception_becomes_text() -> None:
    def boom(*_a: Any, **_k: Any) -> SkillInstallResult:
        raise RuntimeError("disk on fire")

    h = SkillInstallToolHandler(skills_root=Path("S"), install_fn=boom)
    out = asyncio.run(h.execute(SimpleNamespace(arguments={"source": "x"})))  # type: ignore[arg-type]
    assert "RuntimeError" in out and "disk on fire" in out


def test_the_real_installer_is_used_by_default(tmp_path: Path) -> None:
    src = tmp_path / "src" / "real-skill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: real-skill\ndescription: d\n---\n# R\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    h = SkillInstallToolHandler(skills_root=skills)
    out = asyncio.run(h.execute(SimpleNamespace(arguments={"source": str(src)})))  # type: ignore[arg-type]
    assert "Installed skill 'real-skill'" in out and (skills / "real-skill" / "SKILL.md").is_file()


# ── schema + wiring ─────────────────────────────────────────────────────────


def test_schema_requires_only_source() -> None:
    fn = SKILL_INSTALL_TOOL_SCHEMA["function"]
    assert fn["name"] == "skill_install" and fn["parameters"]["required"] == ["source"]
    assert set(fn["parameters"]["properties"]) == {"source", "name", "replace"}


def test_the_tool_is_main_agent_only_and_ordered() -> None:
    assert "skill_install" in SUB_AGENT_EXCLUDED_TOOLS
    assert "skill_install" in TOOL_ORDER


def test_the_tool_is_registered_in_the_chat_container() -> None:
    src = (_ROOT / "apps/api/_chat_di.py").read_text(encoding="utf-8")
    assert re.search(r'tools\.register\(\s*"skill_install"', src), "skill_install is no longer registered"
    assert 'container.repo_root / "skills"' in src and '"factory" / "chat_features"' in src


def test_the_main_prompt_mentions_the_tool_and_the_skill() -> None:
    assert "skill_install" in TOOL_USE_PHILOSOPHY_GUIDANCE and "install-skill" in TOOL_USE_PHILOSOPHY_GUIDANCE


# ── the install-skill skill itself ──────────────────────────────────────────


def test_install_skill_is_discovered_with_usable_metadata() -> None:
    s = SkillDiscovery(_SKILLS_DIR).find("install-skill")
    assert s is not None, "skills/install-skill/SKILL.md was not discovered"
    assert "skill" in [t.lower() for t in s.tags] and ".zip" in s.use_for and s.description


def test_install_skill_body_teaches_the_safe_flow() -> None:
    body = (_SKILLS_DIR / "install-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "skill_install" in body and "question" in body
    assert "NEXT turn" in body
    assert "replace=true" in body and "Never replace without asking" in body
    assert "FileGuard" in body and "Allow Lists" in body
    for step in ("no_skill_md", "not_discovered", "unsafe_archive", "too_large", "exists"):
        assert step in body, step
