# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Handler + schema for the ``skill_install`` tool (agent self-service skill install).

Installs a user's skill -- a local folder or ``.zip`` holding a ``SKILL.md`` -- into the
app's ``skills/`` directory. Installing only COPIES files: no script is run, nothing is
pip-installed. A skill can bundle scripts the agent will run later, so the decision to
install is the user's -- the ``install-skill`` skill makes the agent confirm the source
first.

* Main-agent only (see ``tool_advertise.SUB_AGENT_EXCLUDED_TOOLS``).
* Best-effort like the other harness tools: failures come back as short diagnostic
  text, never as an exception that breaks the turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.adapters.skill_installer import SkillInstallResult
    from qai.chat.application.ports import ToolInvocationRequest

__all__ = ["SKILL_INSTALL_TOOL_SCHEMA", "SkillInstallToolHandler"]

_log = get_logger(__name__)

SKILL_INSTALL_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "skill_install",
        "description": (
            "Install a user-supplied skill (a local folder or a .zip that contains a "
            "SKILL.md) into the app so it appears in the skill list. Use this whenever the "
            "user asks to install / add / import their own skill, instead of copying files "
            "or unzipping yourself. Only copies files -- nothing is executed. Skills can "
            "bundle scripts that are run later, so ask the user (question tool) to confirm "
            "the source before calling it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": (
                        "Absolute path of the skill folder or .zip file, exactly as the "
                        "user gave it (no extra quotes or escaping)."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Skill id (= folder name) to install it under. Letters, digits, "
                        "'_', '.', '-'. Default: the 'name' in its SKILL.md, else the "
                        "source's name."
                    ),
                },
                "replace": {
                    "type": "boolean",
                    "description": (
                        "Set true ONLY to upgrade a skill that is already installed, after "
                        "the user agreed. Default false: an existing skill is never touched."
                    ),
                },
            },
            "required": ["source"],
        },
    },
}


class SkillInstallToolHandler:
    """Handler for the ``skill_install`` tool."""

    __slots__ = ("_skills_root", "_builtin_root", "_install")

    def __init__(
        self,
        *,
        skills_root: Path,
        builtin_root: Path | None = None,
        install_fn: Callable[..., SkillInstallResult] | None = None,
    ) -> None:
        self._skills_root = Path(skills_root)
        self._builtin_root = Path(builtin_root) if builtin_root is not None else None
        # Injectable for tests; defaults to the real installer.
        self._install = install_fn

    async def execute(self, request: ToolInvocationRequest) -> str:
        args = request.arguments if isinstance(request.arguments, dict) else {}
        source = args.get("source")
        if not isinstance(source, str) or not source.strip():
            return "[skill_install] needs 'source' (absolute path of a skill folder or .zip)."
        name = args.get("name")
        if name is not None and not (isinstance(name, str) and name.strip()):
            return "[skill_install] 'name' must be a non-empty string (the skill id)."
        replace = args.get("replace", False)
        if not isinstance(replace, bool):
            return "[skill_install] 'replace' must be true or false."
        install = self._install
        if install is None:
            from qai.chat.adapters.skill_installer import install_skill

            install = install_skill
        try:
            res = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: install(
                    source,
                    skills_root=self._skills_root,
                    builtin_root=self._builtin_root,
                    name=name.strip() if isinstance(name, str) else None,
                    replace=replace,
                ),
            )
        except Exception as exc:  # noqa: BLE001 -- tool contract: text, never raise
            _log.warning("chat.skill_install.failed", error=str(exc))
            return f"[skill_install] failed unexpectedly: {type(exc).__name__}: {exc}"
        if not res.ok:
            return (
                f"[skill_install] FAILED at step '{res.step}': {res.error}\n"
                "Nothing was installed (any previous version is unchanged)."
            )
        return _format_success(res)


def _format_success(res: SkillInstallResult) -> str:
    lines = [
        f"{'Upgraded' if res.replaced else 'Installed'} skill '{res.skill_id}' "
        f"({res.files} files, {res.size / 1024:.0f} KB) at {res.path}",
        f"Name: {res.name} | Description: {res.description or '(none)'}"
        + (f" | Tags: {', '.join(res.tags)}" if res.tags else ""),
        "It is enabled for cloud-model turns by default; the user can switch it off in the "
        "Skills page. It is available from the NEXT model turn -- tell the user, then use it "
        "in the following message.",
    ]
    if res.scripts_total:
        shown = ", ".join(res.scripts)
        more = f" (+{res.scripts_total - len(res.scripts)} more)" if res.scripts_total > len(res.scripts) else ""
        lines.append(
            f"This skill bundles {res.scripts_total} runnable file(s) the agent may execute "
            f"when following it: {shown}{more}. Tell the user so they know what they installed."
        )
    if res.pinned:
        lines.append(
            "WARNING: its SKILL.md sets 'pinned: true', which keeps it always on and ignores "
            "the user's on/off switch. Tell the user."
        )
    lines.extend(f"note: {n}" for n in res.notes)
    return "\n".join(lines)
