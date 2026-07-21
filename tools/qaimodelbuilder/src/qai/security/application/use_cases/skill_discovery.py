# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Skill-policy discovery + listing use cases (R9 cohesion fix).

Moves the skill aggregation algorithm out of
``interfaces/http/routes/security.py`` into the application layer:

* :class:`SkillDiscoveryUseCase` — the three-way aggregation (active
  registry capabilities + orphan policy overrides + filesystem-scanned
  skills) with per-skill ``read``/``write``/``trusted_binaries`` merge,
  dedup, and the ``features`` vs ``skills`` source classification that
  backed ``GET /api/security/skill-discovery``.
* :class:`GetSkillPolicyUseCase` — the per-skill effective-policy build
  (override merged onto capability defaults) backing
  ``GET/PUT /api/security/skill_policy/{skill_name}``.

The merge/dedup/classify helpers + the per-skill override bucket I/O all
move here; the route handlers now only project the returned plain
dataclasses onto their Pydantic wire DTOs.

V1 parity: classification names, merge ordering (capability defaults
first, override appended, first-seen dedup), orphan-override handling,
filesystem scan of ``skills/`` + ``features/`` and the response field set
are byte-for-byte identical to the inline code they replace.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qai.platform.skills import SkillDiscovery, parse_skill_metadata
from qai.security.application.security_runtime_state import (
    SecurityRuntimeStateService,
)
from qai.security.domain.skill_capability import SkillCapability

from ..ports import SkillCapabilityRegistryPort

__all__ = [
    "GetSkillPolicyUseCase",
    "SKILL_POLICY_OVERRIDES_BUCKET",
    "SkillDiscoveryResult",
    "SkillDiscoveryUseCase",
    "SkillEntry",
    "SkillPolicyView",
    "classify_skill_source",
]


# Built-in feature capabilities (mirrors V1 ``FEATURE_META`` keys at legacy
# ``SecurityConfigPanel.js:85-90``). Used to classify each discovered skill
# as ``source="features"`` (built-in) vs ``source="skills"`` (user-installed
# agent skill).
_FEATURE_SKILL_NAMES: frozenset[str] = frozenset(
    {
        "model-builder",
        "model-hub",
        "ppt-gen",
        "code-assist",
        "translate",
        "app-builder",
    }
)

# Bucket key inside ``SecurityRuntimeStateService.settings`` storing the
# user's per-skill policy overrides.
SKILL_POLICY_OVERRIDES_BUCKET = "skill_policies"


def classify_skill_source(skill_name: str, capability_name: str) -> str:
    """Classify a skill as ``"features"`` or ``"skills"`` (V1 parity).

    Either name matching one of the four built-in identifiers wins
    ``features``; everything else is an agent ``skills`` entry.
    """
    if (
        skill_name in _FEATURE_SKILL_NAMES
        or capability_name in _FEATURE_SKILL_NAMES
    ):
        return "features"
    return "skills"


def _merge(base: list[str], extra: list[str]) -> list[str]:
    """Append ``extra`` onto ``base``, deduplicating (first-seen order)."""
    seen: set[str] = set()
    out: list[str] = []
    for item in (*base, *extra):
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def read_skill_policy_overrides(
    state: SecurityRuntimeStateService,
) -> dict[str, dict[str, list[str]]]:
    """Return the persisted per-skill policy overrides bucket.

    Always returns a fresh dict-of-dicts (no aliasing). A missing or
    malformed bucket is normalised to ``{}``.
    """
    raw = state.get_settings(SKILL_POLICY_OVERRIDES_BUCKET)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for skill_name, payload in raw.items():
        if not isinstance(skill_name, str) or not isinstance(payload, dict):
            continue
        out[skill_name] = {
            "read": [
                str(p) for p in payload.get("read", []) if isinstance(p, str)
            ],
            "write": [
                str(p) for p in payload.get("write", []) if isinstance(p, str)
            ],
            "trusted_binaries": [
                str(p)
                for p in payload.get("trusted_binaries", [])
                if isinstance(p, str)
            ],
        }
    return out


def write_skill_policy_override(
    state: SecurityRuntimeStateService,
    *,
    skill_name: str,
    read: list[str],
    write: list[str],
    trusted_binaries: list[str],
) -> dict[str, list[str]]:
    """Persist a per-skill override into ``security_runtime_state``.

    Empty input lists are accepted (V1 lets the operator clear a field).
    Any prior override for ``skill_name`` is fully replaced. Returns the
    canonical entry stored.
    """
    overrides = read_skill_policy_overrides(state)
    entry = {
        "read": [str(p).strip() for p in read if str(p).strip()],
        "write": [str(p).strip() for p in write if str(p).strip()],
        "trusted_binaries": [
            str(p).strip() for p in trusted_binaries if str(p).strip()
        ],
    }
    overrides[skill_name] = entry
    state.update_settings(SKILL_POLICY_OVERRIDES_BUCKET, overrides)
    return entry


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillEntry:
    """One discovered skill row (registry / override / filesystem)."""

    skill_name: str
    capability_name: str
    read_paths: list[str]
    write_paths: list[str]
    exec_paths: list[str]
    trusted_binaries: list[str]
    description: str
    source: str
    active: bool
    has_policy: bool
    raw_read: list[str]
    raw_write: list[str]
    raw_trusted_binaries: list[str]

    def to_dict(self) -> dict[str, object]:
        """Serialise to the V1-aligned discovery wire shape.

        Includes the legacy ``read`` / ``write`` short aliases used by the
        V1 ``permissionSummary`` computation (SecurityConfigPanel.js).
        """
        return {
            "skill_name": self.skill_name,
            "capability_name": self.capability_name,
            "read_paths": self.read_paths,
            "write_paths": self.write_paths,
            "exec_paths": self.exec_paths,
            "trusted_binaries": self.trusted_binaries,
            "description": self.description,
            "source": self.source,
            "active": self.active,
            "has_policy": self.has_policy,
            "raw_read": self.raw_read,
            "raw_write": self.raw_write,
            "raw_trusted_binaries": self.raw_trusted_binaries,
            "read": self.read_paths,
            "write": self.write_paths,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillDiscoveryResult:
    """Aggregate result for ``GET /api/security/skill-discovery``."""

    skills: list[dict[str, object]]
    by_name: dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True, kw_only=True)
class SkillPolicyView:
    """Per-skill effective policy view (override merged on defaults)."""

    skill_name: str
    capability_name: str
    read_paths: list[str]
    write_paths: list[str]
    exec_paths: list[str]
    trusted_binaries: list[str]
    description: str
    raw_read: list[str]
    raw_write: list[str]
    raw_trusted_binaries: list[str]
    has_policy: bool
    active: bool
    source: str


def _entry_for(
    *,
    skill_name: str,
    cap: SkillCapability | None,
    overrides: dict[str, dict[str, list[str]]],
) -> SkillEntry:
    """Build one :class:`SkillEntry` for a skill + optional capability."""
    override = overrides.get(skill_name)
    raw_read = list(override["read"]) if override else []
    raw_write = list(override["write"]) if override else []
    raw_trusted = list(override["trusted_binaries"]) if override else []

    if cap is not None:
        read_paths = _merge(list(cap.read_paths), raw_read)
        write_paths = _merge(list(cap.write_paths), raw_write)
        exec_paths = list(cap.exec_paths)
        trusted = _merge(list(cap.trusted_binaries), raw_trusted)
        description = cap.description
        capability_name = cap.capability_name
        active_flag = True
    else:
        read_paths = raw_read
        write_paths = raw_write
        exec_paths = []
        trusted = raw_trusted
        description = ""
        capability_name = skill_name
        active_flag = False

    return SkillEntry(
        skill_name=skill_name,
        capability_name=capability_name,
        read_paths=read_paths,
        write_paths=write_paths,
        exec_paths=exec_paths,
        trusted_binaries=trusted,
        description=description,
        source=classify_skill_source(skill_name, capability_name),
        active=active_flag,
        has_policy=override is not None,
        raw_read=raw_read,
        raw_write=raw_write,
        raw_trusted_binaries=raw_trusted,
    )


class SkillDiscoveryUseCase:
    """Aggregate registered + orphan + filesystem-discovered skills."""

    def __init__(
        self,
        *,
        registry: SkillCapabilityRegistryPort,
        runtime_state: SecurityRuntimeStateService,
        repo_root: Path,
    ) -> None:
        self._registry = registry
        self._state = runtime_state
        self._repo_root = repo_root

    async def execute(self) -> SkillDiscoveryResult:
        active = await self._registry.list_active()
        overrides = read_skill_policy_overrides(self._state)

        registered_names: set[str] = set()
        skills_list: list[dict[str, object]] = []
        by_name: dict[str, dict[str, object]] = {}

        for cap in active:
            skill_name = cap.capability_name
            registered_names.add(skill_name)
            entry = _entry_for(
                skill_name=skill_name, cap=cap, overrides=overrides
            ).to_dict()
            skills_list.append(entry)
            by_name[skill_name] = entry

        # Orphan overrides (override exists but capability not active).
        for skill_name in overrides:
            if skill_name in registered_names:
                continue
            entry = _entry_for(
                skill_name=skill_name, cap=None, overrides=overrides
            ).to_dict()
            skills_list.append(entry)
            by_name[skill_name] = entry

        # V1-parity: scan skills/ and the built-in chat-feature skill packs on
        # disk; skills found on disk but not in the registry surface with
        # active=False. The built-in packs moved from the legacy ``features/``
        # dir to ``factory/chat_features/`` in the S8 cutover; the
        # classification label stays ``"features"`` for V1 parity.
        self._scan_fs_dir(
            self._repo_root / "skills", "skills", skills_list, by_name
        )
        self._scan_fs_dir(
            self._repo_root / "factory" / "chat_features",
            "features",
            skills_list,
            by_name,
        )
        # The App Builder skill is defined by a ROOT-level SKILL.md
        # (``factory/app_builder/SKILL.md``), not by a subdir SKILL.md, so it
        # must be registered from that one file — a directory scan of
        # ``factory/app_builder`` would miss it and wrongly surface the
        # ``_template`` placeholder subdir instead.
        self._register_root_skill(
            self._repo_root / "factory" / "app_builder",
            "features",
            skills_list,
            by_name,
        )

        return SkillDiscoveryResult(skills=skills_list, by_name=by_name)

    def _register_root_skill(
        self,
        skill_dir: Path,
        source_label: str,
        skills_list: list[dict[str, object]],
        by_name: dict[str, dict[str, object]],
    ) -> None:
        """Register a skill defined by ``<skill_dir>/SKILL.md`` (root-level).

        Unlike :meth:`_scan_fs_dir` (which treats a directory's *subdirs* as
        skills), this parses the single root ``SKILL.md`` whose front-matter
        *is* the skill definition and adds one entry keyed by the front-matter
        ``name:``. Deduplicates against an already-registered capability of the
        same name (same first-seen-wins rule as :meth:`_scan_fs_dir`).
        """
        md_file = skill_dir / "SKILL.md"
        if not md_file.is_file():
            return
        parsed = parse_skill_metadata(md_file)
        if parsed is None:
            return
        skill_name, description = parsed
        if skill_name in by_name:
            by_name[skill_name]["source"] = source_label
            return
        entry: dict[str, object] = {
            "skill_name": skill_name,
            "capability_name": skill_name,
            "read_paths": [],
            "write_paths": [],
            "exec_paths": [],
            "trusted_binaries": [],
            "description": description or "",
            "source": source_label,
            "active": False,
            "has_policy": False,
            "raw_read": [],
            "raw_write": [],
            "raw_trusted_binaries": [],
            "read": [],
            "write": [],
        }
        skills_list.append(entry)
        by_name[skill_name] = entry

    def _scan_fs_dir(
        self,
        root: Path,
        source_label: str,
        skills_list: list[dict[str, object]],
        by_name: dict[str, dict[str, object]],
    ) -> None:
        """Add filesystem-discovered skills not already in ``by_name``."""
        disc = SkillDiscovery(root)
        for skill_info in disc.scan():
            sname = skill_info.skill_id
            if sname in by_name:
                # Already present from registry or overrides; update the
                # source label if it was classified differently.
                by_name[sname]["source"] = source_label
                continue
            entry: dict[str, object] = {
                "skill_name": sname,
                "capability_name": sname,
                "read_paths": [],
                "write_paths": [],
                "exec_paths": [],
                "trusted_binaries": [],
                "description": skill_info.description or "",
                "source": source_label,
                "active": False,
                "has_policy": False,
                "raw_read": [],
                "raw_write": [],
                "raw_trusted_binaries": [],
                "read": [],
                "write": [],
            }
            skills_list.append(entry)
            by_name[sname] = entry


class GetSkillPolicyUseCase:
    """Build the per-skill effective policy view (override on defaults).

    Backs both ``GET`` and ``PUT /api/security/skill_policy/{skill_name}``:
    the PUT route writes the override via :func:`write_skill_policy_override`
    then calls this to render the merged result.
    """

    def __init__(
        self,
        *,
        registry: SkillCapabilityRegistryPort,
        runtime_state: SecurityRuntimeStateService,
    ) -> None:
        self._registry = registry
        self._state = runtime_state

    async def is_known(self, skill_name: str) -> bool:
        """True iff a capability is registered or an override exists."""
        cap = await self._registry.get(skill_name)
        if cap is not None:
            return True
        overrides = read_skill_policy_overrides(self._state)
        return skill_name in overrides

    async def execute(self, *, skill_name: str) -> SkillPolicyView:
        cap = await self._registry.get(skill_name)
        overrides = read_skill_policy_overrides(self._state)
        return self._build(skill_name=skill_name, capability=cap, overrides=overrides)

    def _build(
        self,
        *,
        skill_name: str,
        capability: SkillCapability | None,
        overrides: dict[str, dict[str, list[str]]],
    ) -> SkillPolicyView:
        override = overrides.get(skill_name)
        has_policy = override is not None
        raw_read = list(override["read"]) if override else []
        raw_write = list(override["write"]) if override else []
        raw_trusted = list(override["trusted_binaries"]) if override else []

        if capability is None:
            # Orphan override: surface override fields, active=False.
            return SkillPolicyView(
                skill_name=skill_name,
                capability_name=skill_name,
                read_paths=raw_read,
                write_paths=raw_write,
                exec_paths=[],
                trusted_binaries=raw_trusted,
                description="",
                raw_read=raw_read,
                raw_write=raw_write,
                raw_trusted_binaries=raw_trusted,
                has_policy=has_policy,
                active=False,
                source=classify_skill_source(skill_name, skill_name),
            )

        # Effective lists = override layered onto capability defaults.
        return SkillPolicyView(
            skill_name=skill_name,
            capability_name=capability.capability_name,
            read_paths=_merge(list(capability.read_paths), raw_read),
            write_paths=_merge(list(capability.write_paths), raw_write),
            exec_paths=list(capability.exec_paths),
            trusted_binaries=_merge(
                list(capability.trusted_binaries), raw_trusted
            ),
            description=capability.description,
            raw_read=raw_read,
            raw_write=raw_write,
            raw_trusted_binaries=raw_trusted,
            has_policy=has_policy,
            active=True,
            source=classify_skill_source(
                skill_name, capability.capability_name
            ),
        )
