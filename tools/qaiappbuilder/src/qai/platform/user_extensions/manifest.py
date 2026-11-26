# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``extension.json`` parsing + validation for user extensions.

appbuilder's own manifest format (design doc §10.0). Example::

    {
        "manifest_version": 1,
        "name": "demo-ext",
        "version": "1.0.0",
        "description": "What it does",
        "entry": "main.py",
        "tools": [{"name": "demo_echo", "group": "read"}],
        "skills": {"dir": "skills", "default_modes": {"demo-skill": "both"}},
    }

Rules:

* ``manifest_version`` must be :data:`MANIFEST_VERSION`; an unknown key is an
  error (a typo must not silently disable a feature). A later format bumps
  the version instead of adding keys an older host would reject.
* ``name``: ``^[a-z][a-z0-9]*(-[a-z0-9]+)*$``, <= 64, equals the directory name.
* ``description``: a string or a ``{locale: text}`` object.
* ``entry`` (optional): relative path of a ``.py`` file or of a package
  directory (with ``__init__.py``) that defines ``activate(ctx)``.
* ``tools[].name``: ``[a-z0-9_]``, starts with a letter, <= 64, no ``__``
  (reserved for MCP ``server__tool``); ``tools[].group``: ``read`` / ``edit``
  / ``command`` / ``null`` -- required, so the author decides consciously
  whether persona permission groups can switch the tool off.
* ``skills.dir`` (default ``skills``) and ``skills.default_modes``
  (``skill id -> off/cloud/local/both``).

Pure stdlib so the platform package stays dependency-free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "ExtensionManifest",
    "ManifestError",
    "SkillsDeclaration",
    "ToolDeclaration",
    "load_manifest",
    "parse_manifest",
]

MANIFEST_FILENAME: Final[str] = "extension.json"
#: The manifest format this host implements.
MANIFEST_VERSION: Final[int] = 1
DEFAULT_SKILLS_DIR: Final[str] = "skills"

_EXT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_TOOL_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*$")
_NAME_MAX: Final[int] = 64
#: Forward-slash relative path; no ``..``, no leading ``/`` or ``.`` segment.
_REL_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*/?$"
)

_GROUPS: Final[frozenset[str | None]] = frozenset({"read", "edit", "command", None})
_SKILL_MODES: Final[frozenset[str]] = frozenset({"off", "cloud", "local", "both"})

_TOP_KEYS: Final[frozenset[str]] = frozenset(
    {"manifest_version", "name", "version", "description", "entry", "tools", "skills"}
)
_REQUIRED_TOP: Final[tuple[str, ...]] = ("manifest_version", "name", "version", "description")
_TOOL_KEYS: Final[frozenset[str]] = frozenset({"name", "group"})
_SKILLS_KEYS: Final[frozenset[str]] = frozenset({"dir", "default_modes"})


class ManifestError(ValueError):
    """The manifest is missing, unreadable or invalid. The message names the field."""


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    """One ``tools[]`` entry."""

    name: str
    group: str | None


@dataclass(frozen=True, slots=True)
class SkillsDeclaration:
    """``skills``: the directory (relative to the extension root) + default modes."""

    dir: str = DEFAULT_SKILLS_DIR
    default_modes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    name: str
    version: str
    description: str
    root: Path
    #: Relative path of the Python entry (file or package dir); ``None`` = no code.
    entry: str | None = None
    tools: tuple[ToolDeclaration, ...] = ()
    skills: SkillsDeclaration = field(default_factory=SkillsDeclaration)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tools)

    def tool(self, name: str) -> ToolDeclaration | None:
        return next((t for t in self.tools if t.name == name), None)


def load_manifest(ext_dir: Path) -> ExtensionManifest:
    """Read + validate ``<ext_dir>/extension.json``."""
    path = ext_dir / MANIFEST_FILENAME
    if not path.is_file():
        raise ManifestError(f"{MANIFEST_FILENAME} not found in {ext_dir}")
    try:
        # ``utf-8-sig`` tolerates a BOM written by Windows editors.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path} is not valid JSON: {exc}") from exc
    return parse_manifest(raw, root=ext_dir)


def parse_manifest(raw: Any, *, root: Path) -> ExtensionManifest:
    """Validate an already-decoded manifest object."""
    obj = _object(raw, "manifest")
    _known_keys(obj, _TOP_KEYS, where="")
    _require(obj, _REQUIRED_TOP, where="")
    version_no = obj["manifest_version"]
    if version_no != MANIFEST_VERSION or isinstance(version_no, bool):
        raise ManifestError(
            f"manifest_version {version_no!r} is not supported (this host implements "
            f"{MANIFEST_VERSION})"
        )
    name = _parse_name(obj["name"], root=root)
    version = obj["version"]
    if not isinstance(version, str) or not version.strip():
        raise ManifestError("version must be a non-empty string")
    entry = obj.get("entry")
    if entry is not None:
        entry = _rel_path(entry, label="entry")
    tools = _parse_tools(obj.get("tools"))
    if tools and entry is None:
        raise ManifestError("tools requires entry (the code that registers them)")
    return ExtensionManifest(
        name=name,
        version=version,
        description=_text(obj["description"], label="description"),
        root=root,
        entry=entry,
        tools=tools,
        skills=_parse_skills(obj.get("skills")),
    )


def _parse_name(name: Any, *, root: Path) -> str:
    if not isinstance(name, str) or len(name) > _NAME_MAX or not _EXT_NAME_RE.match(name):
        raise ManifestError(
            f"name {name!r} must match ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ (max {_NAME_MAX})"
        )
    if name != root.name:
        raise ManifestError(f"name {name!r} must equal its directory name {root.name!r}")
    return name


def _parse_tools(raw: Any) -> tuple[ToolDeclaration, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError("tools must be an array")
    out: list[ToolDeclaration] = []
    for index, item in enumerate(raw):
        where = f"tools[{index}]"
        entry = _object(item, where)
        _known_keys(entry, _TOOL_KEYS, where=where)
        _require(entry, ("name", "group"), where=where)
        name = entry["name"]
        if (
            not isinstance(name, str)
            or len(name) > _NAME_MAX
            or "__" in name
            or not _TOOL_NAME_RE.match(name)
        ):
            raise ManifestError(
                f"{where}.name {name!r} must be lower-case letters, digits and '_', "
                f"start with a letter, max {_NAME_MAX}, no '__'"
            )
        if any(t.name == name for t in out):
            raise ManifestError(f"{where}.name {name!r} is declared twice")
        group = entry["group"]
        if group not in _GROUPS:
            raise ManifestError(f"{where}.group {group!r} must be one of read, edit, command, null")
        out.append(ToolDeclaration(name=name, group=group))
    return tuple(out)


def _parse_skills(raw: Any) -> SkillsDeclaration:
    if raw is None:
        return SkillsDeclaration()
    obj = _object(raw, "skills")
    _known_keys(obj, _SKILLS_KEYS, where="skills")
    skills_dir = _rel_path(obj.get("dir", DEFAULT_SKILLS_DIR), label="skills.dir")
    modes = _object(obj.get("default_modes", {}), "skills.default_modes")
    for skill_id, mode in modes.items():
        if mode not in _SKILL_MODES:
            raise ManifestError(
                f"skills.default_modes[{skill_id!r}] {mode!r} must be one of "
                "off, cloud, local, both"
            )
    return SkillsDeclaration(dir=skills_dir, default_modes=dict(modes))


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be a JSON object")
    return value


def _known_keys(obj: dict[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        prefix = f"{where}: " if where else ""
        raise ManifestError(
            f"{prefix}unknown field(s) {', '.join(map(repr, unknown))}; "
            f"allowed: {', '.join(sorted(allowed))}"
        )


def _require(obj: dict[str, Any], keys: tuple[str, ...], *, where: str) -> None:
    for key in keys:
        if key not in obj:
            prefix = f"{where} " if where else ""
            raise ManifestError(f"{prefix}missing required field {key!r}")


def _rel_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _REL_PATH_RE.match(value) or ".." in value.split("/"):
        raise ManifestError(
            f"{label} {value!r} must be a relative path like 'skills' or 'src/main.py'"
        )
    return value.rstrip("/")


def _text(value: Any, *, label: str) -> str:
    """A plain string, or the ``en`` entry of a ``{locale: text}`` mapping."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        for key in ("en", "zh-CN", "zh-TW"):
            text = value.get(key)
            if isinstance(text, str):
                return text
        first = next(iter(value.values()))
        if isinstance(first, str):
            return first
    raise ManifestError(f"{label} must be a string or a locale->string object")
