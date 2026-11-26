# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``extension.json`` parsing + validation for user extensions (stage 1).

The manifest format is the QAIModelBuilder ``api_version: 2`` contract
(forward-compatible: a ModelBuilder manifest must parse here). Only the
fields stage 1 acts on are modelled; every other field is accepted and
ignored, except the ones listed in :data:`_UNSUPPORTED_CONTRIBUTES`, which
produce a warning so the operator knows that part of the extension is inert.

Validation rules mirror ModelBuilder's ``manifest.schema.json`` +
``manifest_records`` semantic rules for the fields modelled here:

* ``name``: ``^[a-z][a-z0-9]*(-[a-z0-9]+)*$``, <= 64, equals the directory name;
* ``python_package`` (optional): ``"qai_ext_" + name.replace("-", "_")``;
* ``contributes.tools[].name``: ``^[a-z][a-z0-9]*(_[a-z0-9]+)*_?$``, <= 64,
  no ``__`` (reserved for MCP ``server__tool`` names);
* ``contributes.tools[].group`` in ``read`` / ``edit`` / ``command`` / ``null``;
* ``contributes.tools[].result_category`` in the six ModelBuilder values.

Pure stdlib (no pydantic / jsonschema) so the platform package stays
dependency-free and unit-testable on its own.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "HOST_EDITION",
    "MANIFEST_FILENAME",
    "SUPPORTED_API_VERSION",
    "ExtensionManifest",
    "ManifestError",
    "SkillsDeclaration",
    "ToolDeclaration",
    "load_manifest",
    "parse_manifest",
]

MANIFEST_FILENAME: Final[str] = "extension.json"
#: The ModelBuilder manifest generation this host implements.
SUPPORTED_API_VERSION: Final[int] = 2

_EXT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_TOOL_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*_?$")
_NAME_MAX: Final[int] = 64

_GROUPS: Final[frozenset[str | None]] = frozenset({"read", "edit", "command", None})
_RESULT_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"read", "query", "command", "subagent", "receipt", "exempt"}
)
_OUTPUT_KINDS: Final[frozenset[str]] = frozenset({"text", "dict", "envelope", "streaming"})

#: ModelBuilder ``$defs.rel_path``: forward-slash relative path, no ``..``,
#: no leading ``/`` or ``.`` segment.
_REL_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*/?$"
)
_EDITIONS: Final[frozenset[str]] = frozenset({"internal", "external"})
_SKILL_MODES: Final[frozenset[str]] = frozenset({"off", "cloud", "local", "both"})
#: ModelBuilder ``skills_dir`` default.
DEFAULT_SKILLS_DIR: Final[str] = "skills"
#: The edition this host runs as: the extension framework targets the external
#: build (design doc §1 / no ``IS_INTERNAL`` switch).
HOST_EDITION: Final[str] = "external"

#: Tool-declaration attributes stage 1 honours. Every other declared attribute
#: is kept on :attr:`ToolDeclaration.unsupported` so it can be reported.
_SUPPORTED_TOOL_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "group", "result_category", "output_kind", "mutating"}
)

#: ``contributes`` keys whose content stage 1 does not act on.
_UNSUPPORTED_CONTRIBUTES: Final[tuple[str, ...]] = (
    "modes",
    "static",
    "settings",
    "settings_schema",
    "package_tools",
    "cli",
    "tables_owned",
    "features",
    "migrations",
)

#: Top-level keys whose content stage 1 does not act on (accepted, warned).
_UNSUPPORTED_TOP_LEVEL: Final[tuple[str, ...]] = (
    "frontend",
    "agents_dir",
    "bootstrap",
    "secrets",
    "permissions",
    "auth",
    "exports",
    "storage",
)


class ManifestError(ValueError):
    """The manifest is missing, unreadable or violates the contract.

    The message is operator-facing: it names the offending field.
    """


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    """One ``contributes.tools[]`` entry."""

    name: str
    group: str | None
    result_category: str
    output_kind: str = "text"
    #: ``None`` = not declared (ModelBuilder tri-state).
    mutating: bool | None = None
    #: Declared attribute names this host does not honour yet.
    unsupported: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillsDeclaration:
    """Where the extension's skills live and which are visible to this host.

    Paths are relative to the extension root (forward slashes, validated).
    ``excluded`` are sub-paths the running edition must not expose (from
    ``skills[].editions`` and ``edition_paths``); ``default_modes`` maps a
    skill id to its ``skills[].default_mode``.
    """

    skills_dir: str = DEFAULT_SKILLS_DIR
    excluded: tuple[str, ...] = ()
    default_modes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    """The parts of ``extension.json`` stage 1 acts on."""

    name: str
    version: str
    api_version: int
    description: str
    root: Path
    python_package: str | None
    tools: tuple[ToolDeclaration, ...] = ()
    skills: SkillsDeclaration = field(default_factory=SkillsDeclaration)
    #: Non-fatal notes (unsupported fields, ...), operator-facing.
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tools)

    def tool(self, name: str) -> ToolDeclaration | None:
        for decl in self.tools:
            if decl.name == name:
                return decl
        return None


def expected_python_package(name: str) -> str:
    return "qai_ext_" + name.replace("-", "_")


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
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")

    _require(raw, ("name", "version", "api_version", "description"), where="")
    name = _parse_name(raw["name"], root=root)
    version = raw["version"]
    if not isinstance(version, str) or not version.strip():
        raise ManifestError("version must be a non-empty string")
    api_version = _parse_api_version(raw["api_version"])
    description = _text(raw["description"], field_name="description")
    python_package = _parse_python_package(raw.get("python_package"), name=name)

    contributes = raw.get("contributes") or {}
    if not isinstance(contributes, dict):
        raise ManifestError("contributes must be an object")
    warnings = [
        f"{key!r} is not supported yet and is ignored"
        for key in _UNSUPPORTED_TOP_LEVEL
        if raw.get(key)
    ]
    warnings += [
        f"contributes.{key} is not supported yet and is ignored"
        for key in _UNSUPPORTED_CONTRIBUTES
        if contributes.get(key)
    ]

    tools = _parse_tools(contributes.get("tools"), warnings=warnings)
    if tools and python_package is None:
        raise ManifestError("contributes.tools requires python_package")

    return ExtensionManifest(
        name=name,
        version=version,
        api_version=api_version,
        description=description,
        root=root,
        python_package=python_package,
        tools=tools,
        skills=_parse_skills(raw),
        warnings=tuple(warnings),
    )


def _parse_skills(raw: dict[str, Any]) -> SkillsDeclaration:
    """``skills_dir`` + ``skills[]`` + ``edition_paths`` (ModelBuilder semantics).

    ``skills[].dir`` must sit inside ``skills_dir``. An entry whose
    ``editions`` omit :data:`HOST_EDITION` -- and every ``edition_paths`` key
    whose editions omit it -- becomes an exclusion. ``default_mode`` is kept
    only for entries visible to this edition.
    """
    skills_dir = _rel_path(raw.get("skills_dir", DEFAULT_SKILLS_DIR), label="skills_dir")
    excluded: list[str] = []
    default_modes: list[tuple[str, str]] = []

    items = raw.get("skills") or []
    if not isinstance(items, list):
        raise ManifestError("skills must be an array")
    for index, item in enumerate(items):
        where = f"skills[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{where} must be an object")
        _require(item, ("dir", "editions"), where=where)
        rel = _rel_path(item["dir"], label=f"{where}.dir")
        if rel != skills_dir and not rel.startswith(skills_dir + "/"):
            raise ManifestError(f"{where}.dir {rel!r} must be inside skills_dir {skills_dir!r}")
        editions = _editions(item["editions"], label=f"{where}.editions")
        mode = item.get("default_mode")
        if mode is not None:
            _one_of(mode, _SKILL_MODES, f"{where}.default_mode")
        if HOST_EDITION not in editions:
            excluded.append(rel)
        elif mode is not None:
            default_modes.append((rel.rsplit("/", 1)[-1], mode))

    edition_paths = raw.get("edition_paths") or {}
    if not isinstance(edition_paths, dict):
        raise ManifestError("edition_paths must be an object")
    for key, value in edition_paths.items():
        rel = _rel_path(key, label=f"edition_paths key {key!r}")
        if HOST_EDITION not in _editions(value, label=f"edition_paths[{key!r}]"):
            excluded.append(rel)

    return SkillsDeclaration(
        skills_dir=skills_dir,
        excluded=tuple(dict.fromkeys(excluded)),
        default_modes=tuple(default_modes),
    )


def _rel_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _REL_PATH_RE.match(value) or ".." in value.split("/"):
        raise ManifestError(f"{label} {value!r} must be a relative path like 'skills/x'")
    return value.rstrip("/")


def _editions(value: Any, *, label: str) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(set(value)) != len(value)
        or any(v not in _EDITIONS for v in value)
    ):
        raise ManifestError(f"{label} must be a non-empty list of 'internal' / 'external'")
    return frozenset(value)


def _require(entry: dict[str, Any], keys: tuple[str, ...], *, where: str) -> None:
    for key in keys:
        if key not in entry:
            prefix = f"{where} " if where else ""
            raise ManifestError(f"{prefix}missing required field {key!r}")


def _parse_name(name: Any, *, root: Path) -> str:
    if not isinstance(name, str) or len(name) > _NAME_MAX or not _EXT_NAME_RE.match(name):
        raise ManifestError(
            f"name {name!r} must match ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ (max {_NAME_MAX})"
        )
    if name != root.name:
        raise ManifestError(f"name {name!r} must equal its directory name {root.name!r}")
    return name


def _parse_api_version(api_version: Any) -> int:
    if not isinstance(api_version, int) or isinstance(api_version, bool) or api_version < 1:
        raise ManifestError("api_version must be a positive integer")
    if api_version != SUPPORTED_API_VERSION:
        raise ManifestError(
            f"api_version {api_version} is not supported (this host implements "
            f"{SUPPORTED_API_VERSION})"
        )
    return api_version


def _parse_python_package(python_package: Any, *, name: str) -> str | None:
    if python_package is None:
        return None
    expected = expected_python_package(name)
    if python_package != expected:
        raise ManifestError(f"python_package {python_package!r} must be {expected!r}")
    return expected


def _text(value: Any, *, field_name: str) -> str:
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
    raise ManifestError(f"{field_name} must be a string or a locale->string object")


def _parse_tools(raw: Any, *, warnings: list[str]) -> tuple[ToolDeclaration, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError("contributes.tools must be an array")
    out: list[ToolDeclaration] = []
    for index, entry in enumerate(raw):
        decl = _parse_tool(entry, where=f"contributes.tools[{index}]")
        if any(d.name == decl.name for d in out):
            raise ManifestError(
                f"contributes.tools[{index}].name {decl.name!r} is declared twice"
            )
        if decl.unsupported:
            warnings.append(
                f"tool {decl.name!r}: {', '.join(decl.unsupported)} not supported yet "
                "and ignored"
            )
        out.append(decl)
    return tuple(out)


def _parse_tool(entry: Any, *, where: str) -> ToolDeclaration:
    if not isinstance(entry, dict):
        raise ManifestError(f"{where} must be an object")
    _require(entry, ("name", "group", "result_category"), where=where)
    name = entry["name"]
    if (
        not isinstance(name, str)
        or len(name) > _NAME_MAX
        or "__" in name
        or not _TOOL_NAME_RE.match(name)
    ):
        raise ManifestError(
            f"{where}.name {name!r} must match ^[a-z][a-z0-9]*(_[a-z0-9]+)*_?$ "
            f"(max {_NAME_MAX}, no '__')"
        )
    _one_of(entry["group"], _GROUPS, f"{where}.group")
    _one_of(entry["result_category"], _RESULT_CATEGORIES, f"{where}.result_category")
    output_kind = entry.get("output_kind", "text")
    _one_of(output_kind, _OUTPUT_KINDS, f"{where}.output_kind")
    mutating = entry.get("mutating")
    if mutating is not None and not isinstance(mutating, bool):
        raise ManifestError(f"{where}.mutating must be a boolean")
    return ToolDeclaration(
        name=name,
        group=entry["group"],
        result_category=entry["result_category"],
        output_kind=output_kind,
        mutating=mutating,
        unsupported=tuple(sorted(k for k in entry if k not in _SUPPORTED_TOOL_KEYS)),
    )


def _one_of(value: Any, allowed: frozenset[Any], label: str) -> None:
    if value not in allowed:
        shown = sorted("null" if v is None else v for v in allowed)
        raise ManifestError(f"{label} {value!r} must be one of {shown}")
