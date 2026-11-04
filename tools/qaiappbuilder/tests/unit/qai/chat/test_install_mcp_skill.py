# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""The shipped ``install-mcp`` skill is discoverable and tells the agent the right things."""

from __future__ import annotations

from pathlib import Path

from qai.chat.adapters.system_prompt_builder import TOOL_USE_PHILOSOPHY_GUIDANCE
from qai.platform.skills.discovery import SkillDiscovery

_SKILLS_DIR = Path(__file__).resolve().parents[4] / "skills"


def _skill():
    found = SkillDiscovery(_SKILLS_DIR).find("install-mcp")
    assert found is not None, "skills/install-mcp/SKILL.md was not discovered"
    return found


def test_skill_is_discovered_with_usable_metadata() -> None:
    s = _skill()
    assert s.name == "install-mcp"
    assert "mcp" in [t.lower() for t in s.tags]
    # the catalog entry the model matches the user's request against
    assert ".whl" in s.use_for or "wheel" in s.use_for.lower()
    assert s.description


def test_skill_body_teaches_the_safe_flow() -> None:
    # ``skill_path`` is the SKILL.md file itself (relative to the scan root's cwd).
    body = (_SKILLS_DIR / "install-mcp" / "SKILL.md").read_text(encoding="utf-8")
    assert "mcp_manage" in body
    assert 'action="install_wheel"' in body
    # confirm before installing third-party code; never flip the gate silently
    assert "question" in body
    assert "Never turn it on without asking" in body
    # blocked path => stop and ask, do not work around
    assert "FileGuard" in body and "Allow Lists" in body
    # tools of a new server appear next turn
    assert "NEXT turn" in body


def test_main_agent_prompt_mentions_mcp_manage() -> None:
    assert "mcp_manage" in TOOL_USE_PHILOSOPHY_GUIDANCE
    assert "install-mcp" in TOOL_USE_PHILOSOPHY_GUIDANCE
    assert "OFF by default" in TOOL_USE_PHILOSOPHY_GUIDANCE
