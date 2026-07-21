# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""SKILL.md / skill.json discovery + NPU detection.

Ported faithfully from v1 ``backend/skill_manager.py`` (the discovery
half). Differences from v1, by design:

* No ``security.skill_injection_scanner.scan_and_sanitize`` call — that
  sanitised the markdown *body*; discovery only consumes front-matter
  *metadata* (name / description / tags / use_for), so the body is never
  surfaced here. Body sanitisation, if needed, stays a ``security`` BC
  concern reached via an ``apps/api`` bridge (cross-context isolation,
  AGENTS.md §3.2).
* No FileGuard ``skill.policy.json`` registration — that is a separate
  ``security`` capability wired elsewhere; discovery is pure filesystem
  read with no cross-context import.
* Mode persistence is NOT here. Discovery always reports the on-disk
  truth with ``mode`` left at the caller's default; the HTTP route layer
  merges persisted per-skill mode from ``forge.config``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

logger = logging.getLogger("qai.platform.skills")

# 4-state run modes (v1 parity).
VALID_MODES: Final[frozenset[str]] = frozenset({"off", "cloud", "local", "both"})
# Modes that require ``npu_optimized``.
NPU_MODES: Final[frozenset[str]] = frozenset({"local", "both"})

# ── YAML front-matter parser (no external dep; v1 parity) ──────────────────

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^(\w[\w\-]*)\s*:\s*(.+)$")

# Recognised icon extensions, in priority order (v1 parity).
_ICON_EXTS: Final[tuple[str, ...]] = (".png", ".svg", ".jpg", ".webp")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Extract YAML-like front-matter from markdown text.

    Returns ``(meta_dict, body_without_frontmatter)``. Only handles simple
    ``key: value`` pairs (no nested YAML). Matched-pair quote stripping is
    preserved verbatim from v1 to avoid the ``"it's"`` corruption bug.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        km = _KV_RE.match(line.strip())
        if km:
            val = km.group(2).strip()
            if len(val) >= 2 and (
                (val[0] == '"' and val[-1] == '"')
                or (val[0] == "'" and val[-1] == "'")
            ):
                val = val[1:-1]
            meta[km.group(1)] = val
    body = text[m.end():]
    return meta, body


def _extract_description(body: str, max_chars: int = 200) -> str:
    """Pull the first non-heading paragraph from markdown body."""
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:max_chars]
    return ""


def parse_skill_metadata(md_file: Path) -> tuple[str, str] | None:
    """Parse a single root-level ``SKILL.md`` into ``(name, description)``.

    Unlike :meth:`SkillDiscovery.scan` (which treats a directory's *subdirs*
    as skills), this reads one ``SKILL.md`` file whose front-matter *is* the
    skill definition (e.g. ``factory/app_builder/SKILL.md``). ``name`` comes
    from the front-matter ``name:`` key (falling back to the file's parent
    directory name); ``description`` from the ``description:`` key or the
    first body paragraph. Returns ``None`` when the file is unreadable.

    Reuses the same :func:`_parse_frontmatter` / :func:`_extract_description`
    helpers ``SkillDiscovery`` uses, so parsing semantics stay identical.
    """
    try:
        text = md_file.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read %s: %s", md_file, exc)
        return None
    meta, body = _parse_frontmatter(text)
    name = str(meta.get("name", "")).strip() or md_file.parent.name
    description = str(meta.get("description", "")) or _extract_description(body)
    return name, description


@dataclass(slots=True)
class SkillInfo:
    """Immutable discovered skill record (v1 ``SkillInfo`` field parity).

    ``mode`` defaults to ``"cloud"`` to mirror v1's "active for cloud
    models by default" behaviour; callers override it with the persisted
    per-skill mode read from ``forge.config``.

    ``pinned`` marks a skill as always-on: when ``True``, user-level
    ``forge.config`` overrides (toggle / set_mode) are ignored and the
    skill is always visible in the catalog (mode stays ``"cloud"``).  Set
    via ``pinned: true`` in the SKILL.md YAML front-matter.
    """

    skill_id: str
    name: str
    description: str = ""
    icon: str = ""
    tags: list[str] = field(default_factory=list)
    use_for: str = ""
    skill_path: str = ""
    npu_optimized: bool = False
    mode: str = "cloud"
    pinned: bool = False

    @property
    def enabled(self) -> bool:
        """Backward-compat: True when the skill is active for any model type."""
        return self.mode != "off"

    def to_dict(self) -> dict[str, object]:
        """Return the v1-shaped ``to_dict()`` payload."""
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "tags": list(self.tags),
            "use_for": self.use_for,
            "skill_path": self.skill_path,
            "npu_optimized": self.npu_optimized,
            "mode": self.mode,
            "enabled": self.enabled,  # backward-compat
            "pinned": self.pinned,
        }


class SkillDiscovery:
    """Scan one or more ``skills/`` directory trees and parse each skill's metadata.

    Stateless apart from the configured root directories: ``scan()`` re-reads
    the filesystem every call so callers get live data without a cache
    (matching the v1 reload semantics).

    ``skills_dir`` accepts either a single :class:`~pathlib.Path` (backward
    compatible) or a list of paths.  When multiple directories are given,
    ``scan()`` merges results in order: the first directory that defines a
    given ``skill_id`` wins (earlier directories take precedence).
    ``icon_path()`` searches directories in the same order.
    """

    def __init__(self, skills_dir: Path | list[Path]) -> None:
        if isinstance(skills_dir, list):
            self._dirs: list[Path] = [Path(d) for d in skills_dir]
        else:
            self._dirs = [Path(skills_dir)]

    @property
    def skills_dir(self) -> Path:
        """Primary (first) skills directory — preserved for backward compat."""
        return self._dirs[0]

    @property
    def skills_dirs(self) -> list[Path]:
        """All configured skills directories."""
        return list(self._dirs)

    def scan(self) -> list[SkillInfo]:
        """Discover all skills across all configured directories.

        Results are ordered by skill_id (directory name).  When the same
        skill_id appears in multiple directories the first directory wins.
        A missing directory yields an empty contribution (logged at WARNING).
        """
        seen: dict[str, SkillInfo] = {}
        for skills_dir in self._dirs:
            if not skills_dir.exists():
                logger.warning("skills directory not found: %s", skills_dir)
                continue
            count = 0
            for entry in sorted(skills_dir.iterdir()):
                if not entry.is_dir():
                    continue
                skill = self._parse_skill_dir(entry)
                if skill is not None and skill.skill_id not in seen:
                    seen[skill.skill_id] = skill
                    count += 1
            logger.info("discovered %d skills from %s", count, skills_dir)
        return sorted(seen.values(), key=lambda s: s.skill_id)

    def find(self, skill_id: str) -> SkillInfo | None:
        """Return the discovered ``SkillInfo`` for ``skill_id`` or ``None``."""
        for skill in self.scan():
            if skill.skill_id == skill_id:
                return skill
        return None

    def icon_path(self, skill_id: str) -> Path | None:
        """Return the on-disk icon file path for ``skill_id`` or ``None``.

        Resolves the first matching ``icon.<ext>`` inside the skill
        directory across all configured directories (searched in order).
        ``skill_id`` is validated against directory traversal.
        """
        if not skill_id or "/" in skill_id or "\\" in skill_id or skill_id in {".", ".."}:
            return None
        if "\x00" in skill_id:
            return None
        for skills_dir in self._dirs:
            skill_dir = skills_dir / skill_id
            try:
                skill_dir.resolve().relative_to(skills_dir.resolve())
            except (ValueError, OSError):
                continue
            if not skill_dir.is_dir():
                continue
            for ext in _ICON_EXTS:
                candidate = skill_dir / f"icon{ext}"
                if candidate.is_file():
                    return candidate
        return None

    # ── internals ─────────────────────────────────────────────────────────

    def _parse_skill_dir(self, path: Path) -> SkillInfo | None:
        skill_id = path.name

        md_file = path / "SKILL.md"
        json_file = path / "skill.json"

        meta: dict[str, object] = {}
        description = ""

        if md_file.exists():
            try:
                text = md_file.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("could not read %s: %s", md_file, exc)
                return None
            meta, body = _parse_frontmatter(text)
            description = str(meta.get("description", "")) or _extract_description(body)
        elif json_file.exists():
            try:
                meta = json.loads(json_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("invalid skill.json in %s: %s", path, exc)
                return None
            description = str(meta.get("description", ""))
        else:
            logger.debug("skipping %s — no SKILL.md or skill.json", path)
            return None

        # Icon → relative URL for the frontend.
        icon = ""
        for ext in _ICON_EXTS:
            if (path / f"icon{ext}").exists():
                icon = f"/api/skills/{skill_id}/icon"
                break

        # skill_path → absolute path to the metadata file (read-tool target).
        skill_path = str(md_file) if md_file.exists() else str(json_file)

        tags_raw = meta.get("tags", "")
        if isinstance(tags_raw, str):
            tags = [t.strip() for t in tags_raw.split(",")]
        elif isinstance(tags_raw, list):
            tags = [str(t).strip() for t in tags_raw]
        else:
            tags = []

        # NPU-optimised: trailing "." on the raw ``tags:`` value (after
        # stripping whitespace), OR a legacy ``npu.txt`` marker file.
        npu_optimized = (
            (isinstance(tags_raw, str) and tags_raw.rstrip().endswith("."))
            or (path / "npu.txt").exists()
        )

        # Strip the trailing "." sentinel from the last tag if present.
        if npu_optimized and tags and tags[-1].endswith("."):
            tags[-1] = tags[-1][:-1].rstrip()
            if not tags[-1]:
                tags.pop()

        # ``pinned: true`` in front-matter → always-on; user toggle ignored.
        pinned_raw = meta.get("pinned", "")
        pinned = str(pinned_raw).strip().lower() in ("true", "1", "yes")

        return SkillInfo(
            skill_id=skill_id,
            name=str(meta.get("name", skill_id)) or skill_id,
            description=description,
            icon=icon,
            tags=tags,
            use_for=str(meta.get("use_for", "")),
            skill_path=skill_path,
            npu_optimized=npu_optimized,
            mode="cloud",
            pinned=pinned,
        )
