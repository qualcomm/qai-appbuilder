# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""The shipped ``modem-log-prefilter`` skill is discoverable and its bundle is intact."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from qai.platform.skills.discovery import SkillDiscovery

_SKILLS_DIR = Path(__file__).resolve().parents[4] / "skills"
_SKILL_DIR = _SKILLS_DIR / "modem-log-prefilter"


def test_skill_is_discovered_with_usable_metadata() -> None:
    found = SkillDiscovery(_SKILLS_DIR).find("modem-log-prefilter")
    assert found is not None, "skills/modem-log-prefilter/SKILL.md was not discovered"
    assert found.description
    assert "modem" in [t.lower() for t in found.tags]
    assert found.use_for


def test_every_relative_link_in_skill_md_exists() -> None:
    body = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)#\s]+)\)", body)
    assert links, "SKILL.md no longer links to its bundled files"
    missing = [rel for rel in links if not rel.startswith(("http://", "https://")) and not (_SKILL_DIR / rel).exists()]
    assert not missing, f"SKILL.md links to files that are not bundled: {missing}"


def test_planning_runs_with_the_standard_library_only(tmp_path: Path) -> None:
    out = tmp_path / "plan.json"
    run = subprocess.run(
        [sys.executable, str(_SKILL_DIR / "scripts" / "modem_log.py"), "plan", "--skill", "Modem_NR_Camping", "--out", str(out)],
        capture_output=True, text=True, timeout=120, cwd=_SKILL_DIR,
    )
    assert run.returncode == 0, run.stderr
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert "filters" in plan and plan["skills"]
