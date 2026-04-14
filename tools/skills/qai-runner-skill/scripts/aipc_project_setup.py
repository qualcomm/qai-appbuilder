# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
#!/usr/bin/env python3
"""
aipc_project_setup.py — Initialize an AIPC project with standard templates.

Actions
-------
1. Attach ``assets/aipc_AGENTS.md`` to the project's ``AGENTS.md``
   (appends if the file already exists; creates it if it does not).
2. Copy ``assets/aipc_plan.md`` to the project directory as ``aipc_plan.md``
   (backs up any existing file before overwriting).

Usage
-----
Run from the skill directory (do NOT copy this script to the project folder):

    python skills/aipc-toolkit/scripts/aipc_project_setup.py  [project_dir]  [options]

Arguments
---------
project_dir     Target project directory.  Defaults to the current working
                directory when omitted.

Options
-------
--agents-only   Only attach AGENTS.md; skip aipc_plan.md.
--plan-only     Only copy aipc_plan.md; skip AGENTS.md.
--force         Re-append / overwrite even when the target files already
                contain AIPC content.
"""

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths — resolved relative to this script so it works from any cwd
# ---------------------------------------------------------------------------
SKILL_DIR = Path(__file__).resolve().parent.parent
AGENTS_TEMPLATE = SKILL_DIR / "assets" / "aipc_AGENTS.md"
PLAN_TEMPLATE = SKILL_DIR / "assets" / "aipc_plan.md"

# Sentinel written into AGENTS.md so we can detect a previous attachment
_AIPC_AGENTS_SENTINEL = "<!-- AIPC-TOOLKIT:AGENTS -->"

_AGENTS_HEADER = f"""\n
---
{_AIPC_AGENTS_SENTINEL}
<!-- AIPC Toolkit Agent Definitions                                  -->
<!-- Appended automatically by aipc_project_setup.py                -->
<!-- Source: skills/aipc-toolkit/references/aipc_AGENTS.md          -->
<!-- ----------------------------------------------------------------->

"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _backup(path: Path) -> Path:
    """Create a timestamped backup of *path* and return the backup path."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    backup_path = path.with_name(f"{path.name}.bak_{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def setup_agents(project_dir: Path, force: bool = False) -> None:
    """Attach aipc_AGENTS.md to <project_dir>/AGENTS.md."""
    if not AGENTS_TEMPLATE.is_file():
        print(f"[ERROR] Template not found: {AGENTS_TEMPLATE}", file=sys.stderr)
        sys.exit(1)

    target = project_dir / "AGENTS.md"
    template_content = _read(AGENTS_TEMPLATE)

    if target.exists():
        existing = _read(target)

        if _AIPC_AGENTS_SENTINEL in existing:
            if force:
                print(f"[FORCE] Re-appending AIPC agent definitions to {target.name}")
            else:
                print(
                    f"[SKIP]  {target.name} already contains AIPC agent definitions.\n"
                    f"        Use --force to re-append."
                )
                return

        # Append the template after a clear separator
        with target.open("a", encoding="utf-8") as fh:
            fh.write(_AGENTS_HEADER)
            fh.write(template_content)
            fh.write("\n")
        print(f"[OK]    Appended aipc_AGENTS.md  ->  {target}")

    else:
        # No existing AGENTS.md — create one directly from the template
        shutil.copy2(AGENTS_TEMPLATE, target)
        print(f"[OK]    Created {target} from aipc_AGENTS.md template")


def setup_plan(project_dir: Path, force: bool = False) -> None:
    """Copy aipc_plan.md to <project_dir>/aipc_plan.md and auto-fill START_TIME."""
    if not PLAN_TEMPLATE.is_file():
        print(f"[ERROR] Template not found: {PLAN_TEMPLATE}", file=sys.stderr)
        sys.exit(1)

    target = project_dir / "aipc_plan.md"

    if target.exists():
        backup_path = _backup(target)
        print(f"[BAK]   Backed up existing aipc_plan.md  ->  {backup_path.name}")

    # Copy template
    shutil.copy2(PLAN_TEMPLATE, target)
    
    # Auto-fill START_TIME with current system time
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    content = _read(target)
    content = content.replace(
        "START_TIME    = <!-- YYYY-MM-DD HH:MM get current system time -->",
        f"START_TIME    = {current_time}"
    )
    _write(target, content)
    
    print(f"[OK]    Copied aipc_plan.md  ->  {target} (START_TIME={current_time})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Initialize an AIPC project with standard agent and plan templates.\n\n"
            "Run this script from its original location inside the skill directory;\n"
            "do NOT copy it to the project folder."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        help="Target project directory (default: current working directory)",
    )
    parser.add_argument(
        "--agents-only",
        action="store_true",
        help="Only attach AGENTS.md; skip aipc_plan.md",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only copy aipc_plan.md; skip AGENTS.md",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-append / overwrite even if AIPC content already exists",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"[ERROR] Project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print("  AIPC Project Setup")
    print("=" * 60)
    print(f"  Project dir : {project_dir}")
    print(f"  Skill dir   : {SKILL_DIR}")
    print()

    if not args.plan_only:
        setup_agents(project_dir, force=args.force)

    if not args.agents_only:
        setup_plan(project_dir, force=args.force)

    print()
    print("Next steps:")
    print("  1. Open aipc_plan.md and fill in the Config section variables.")
    print("  2. Activate the aipc-toolkit skill and follow the plan.")
    print("=" * 60)


if __name__ == "__main__":
    main()