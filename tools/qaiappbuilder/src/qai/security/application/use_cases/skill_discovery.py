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

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from qai.platform.skills import SkillDiscovery, parse_skill_metadata
from qai.security.application.security_runtime_state import (
    SecurityRuntimeStateService,
)
from qai.security.domain.skill_capability import SkillCapability

from ..ports import SkillCapabilityRegistryPort

__all__ = [
    "SKILL_POLICY_OVERRIDES_BUCKET",
    "GetSkillPolicyUseCase",
    "SkillDiscoveryResult",
    "SkillDiscoveryUseCase",
    "SkillEntry",
    "SkillModeProvider",
    "SkillPolicyView",
    "build_effective_capabilities",
    "capability_from_override",
    "classify_skill_source",
    "merge_override_into_capability",
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

#: Provider injected by the DI layer: given every discovered ``skill_id``,
#: returns the ``skill_id -> 4-state run mode`` mapping (``"off"`` /
#: ``"cloud"`` / ``"local"`` / ``"both"``).
#:
#: Takes the id list because the answer is NOT just "what did the operator
#: override": a skill with no persisted override still HAS an effective mode
#: (the per-skill default — most skills default to ``"cloud"`` = enabled,
#: a few ship ``"off"``). Resolving only the override keys would leave every
#: never-configured skill with no mode at all, which the UI would render as
#: "nothing selected" while the Skills page showed a real mode — the two
#: surfaces must agree.
#:
#: The precedence rules and the default table live in ``user_prefs``, so the
#: security context takes this as an awaitable callable instead of importing
#: that context (cross-context purity). A fault or a missing entry degrades to
#: ``""`` = "unknown".
SkillModeProvider = Callable[[Sequence[str]], Awaitable[dict[str, str]]]


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

    Clearing every list DELETES the override rather than storing an empty
    shell: an empty entry would still count as ``has_policy=True``, so the
    Skill panel would show a "custom policy" badge for a policy that
    grants nothing and the operator could never get rid of it. Emptying
    all three fields IS how you revoke a per-skill policy.

    Any prior override for ``skill_name`` is fully replaced. Returns the
    canonical entry (all-empty when the override was removed).
    """
    overrides = read_skill_policy_overrides(state)
    entry = {
        "read": [str(p).strip() for p in read if str(p).strip()],
        "write": [str(p).strip() for p in write if str(p).strip()],
        "trusted_binaries": [
            str(p).strip() for p in trusted_binaries if str(p).strip()
        ],
    }
    if entry["read"] or entry["write"] or entry["trusted_binaries"]:
        overrides[skill_name] = entry
    else:
        overrides.pop(skill_name, None)
    state.update_settings(SKILL_POLICY_OVERRIDES_BUCKET, overrides)
    return entry


def merge_override_into_capability(
    cap: SkillCapability,
    override: dict[str, list[str]] | None,
) -> SkillCapability:
    """Layer an operator path override onto a skill's own declaration.

    The declaration (``skill.policy.json``, loaded into the registry at
    boot) is the skill author's intent; the override is the operator's
    addition on top. Never replaces, only extends — same ``_merge``
    semantics the discovery payload renders, so the surface shown in the
    Skill panel is exactly the surface enforced by
    :class:`CheckPermissionUseCase`'s skill-capability ALLOW branch.

    ``trusted_binaries`` additionally folds into ``exec_paths``: the
    loader leaves ``exec_paths`` empty (V1 parity), so without this an
    authorised binary could never satisfy ``covers_exec`` and the
    "trusted programs" field would grant nothing. The exec-deny hard gate
    is still re-checked downstream, so a protected path stays denied.

    A malformed override degrades to the untouched capability (never
    widens on bad input, never raises into the decision path).
    """
    if not override:
        return cap
    try:
        trusted = _merge(
            list(cap.trusted_binaries), override.get("trusted_binaries", [])
        )
        return SkillCapability(
            capability_name=cap.capability_name,
            read_paths=tuple(_merge(list(cap.read_paths), override.get("read", []))),
            write_paths=tuple(
                _merge(list(cap.write_paths), override.get("write", []))
            ),
            exec_paths=tuple(
                _merge(
                    list(cap.exec_paths),
                    override.get("trusted_binaries", []),
                )
            ),
            trusted_binaries=tuple(trusted),
            description=cap.description,
            sha256_pins=cap.sha256_pins,
        )
    except (TypeError, ValueError):
        return cap


def capability_from_override(
    skill_name: str,
    override: dict[str, list[str]],
) -> SkillCapability | None:
    """Synthesise a capability for a skill that has ONLY an override.

    Every agent skill under ``skills/`` ships without a
    ``skill.policy.json``, so authoring a policy in the Skill panel is
    the ONLY way it gets a file surface. Without this the editor would
    save a policy that enforces nothing.

    Returns ``None`` for an empty or malformed override (nothing to
    grant / never raise into the decision path).
    """
    read = override.get("read", [])
    write = override.get("write", [])
    trusted = override.get("trusted_binaries", [])
    if not (read or write or trusted):
        return None
    try:
        return SkillCapability(
            capability_name=skill_name,
            read_paths=tuple(read),
            write_paths=tuple(write),
            # Same trusted -> exec fold as the merge path above.
            exec_paths=tuple(trusted),
            trusted_binaries=tuple(trusted),
        )
    except (TypeError, ValueError):
        return None


def build_effective_capabilities(
    *,
    registered: list[SkillCapability],
    path_overrides: dict[str, dict[str, list[str]]],
    is_disabled: Callable[[str], bool],
) -> tuple[SkillCapability, ...]:
    """Resolve the capability set FileGuard should enforce right now.

    Three inputs, one authoritative answer:

    * ``registered`` — the live registry snapshot (each skill's on-disk
      ``skill.policy.json``).
    * ``path_overrides`` — the operator's ``skill_policies`` bucket
      (what the Security > Skill panel editor writes).
    * ``is_disabled`` — per-skill run-mode predicate: a skill switched
      ``off`` unlocks NOTHING, declaration or override. That is the
      operator's intent when they turn a skill off.

    Skills present in both are merged; skills present only in the
    override are synthesised. The result feeds
    ``CheckPermissionUseCase(skill_allow_provider=...)``.
    """
    out: list[SkillCapability] = []
    seen: set[str] = set()
    for cap in registered:
        seen.add(cap.capability_name)
        if is_disabled(cap.capability_name):
            continue
        out.append(
            merge_override_into_capability(
                cap, path_overrides.get(cap.capability_name)
            )
        )
    for skill_name, override in path_overrides.items():
        if skill_name in seen or is_disabled(skill_name):
            continue
        synthesised = capability_from_override(skill_name, override)
        if synthesised is not None:
            out.append(synthesised)
    return tuple(out)




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
    #: Operator's per-skill 4-state run mode ("off" / "cloud" / "local" /
    #: "both"). Supplied by an injected resolver (the value is owned by
    #: ``forge.config skills.overrides[<id>].mode``, a ``user_prefs``
    #: concern) so the Security > Skill panel can render the mode switch
    #: from the SAME payload it renders capabilities from. ``""`` means
    #: "unknown" — no resolver wired (minimal/test container).
    mode: str = ""

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
            "mode": self.mode,
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
    mode: str = "",
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
        mode=mode,
    )


class SkillDiscoveryUseCase:
    """Aggregate registered + orphan + filesystem-discovered skills."""

    def __init__(
        self,
        *,
        registry: SkillCapabilityRegistryPort,
        runtime_state: SecurityRuntimeStateService,
        repo_root: Path,
        skill_mode_provider: SkillModeProvider | None = None,
    ) -> None:
        self._registry = registry
        self._state = runtime_state
        self._repo_root = repo_root
        # Injected: returns the WHOLE ``skill_id -> mode`` mapping in one
        # call. The value lives in the ``forge.config`` document's
        # ``skills.overrides`` (a ``user_prefs`` concern), so the security
        # context never reads that store itself — the DI layer bridges it
        # (cross-context purity, AGENTS.md §3.2).
        #
        # Bulk (not per-skill) on purpose: the store is a KV row in SQLite, so
        # a per-skill resolver meant one DB round-trip per discovered skill
        # (25+ per panel refresh) for a single document that never changes
        # mid-scan. ``None``, a fault, or a missing entry degrades to
        # ``mode=""`` = "unknown", which the WebUI renders as no mode
        # selected rather than guessing a default.
        self._skill_mode_provider = skill_mode_provider

    async def _resolve_modes(self, skill_ids: list[str]) -> dict[str, str]:
        """Resolve every discovered skill's mode, degrading to ``{}``."""
        if self._skill_mode_provider is None or not skill_ids:
            return {}
        try:
            modes = await self._skill_mode_provider(skill_ids)
        except Exception:  # noqa: BLE001 — provider fault → unknown modes
            return {}
        if not isinstance(modes, dict):
            return {}
        return {
            k: v
            for k, v in modes.items()
            if isinstance(k, str) and isinstance(v, str)
        }

    async def execute(self) -> SkillDiscoveryResult:
        # Key by the REGISTRATION name (``skill_name``), not
        # ``capability.capability_name``: a skill may register a capability
        # under a different name, and the ``by_name`` map is documented as
        # keyed by skill_name (see ``interfaces/http/routes/security/
        # _skills.py`` ``/skill-discovery``). Using capability_name silently
        # mis-keyed those entries so a lookup by the registered skill name
        # missed. ``list_active_items`` preserves the key; fall back to the
        # older values-only accessor when an adapter predates it.
        items_accessor = getattr(self._registry, "list_active_items", None)
        if items_accessor is not None:
            active_items = await items_accessor()
        else:
            active_items = [
                (cap.capability_name, cap)
                for cap in await self._registry.list_active()
            ]
        overrides = read_skill_policy_overrides(self._state)

        registered_names: set[str] = set()
        skills_list: list[dict[str, object]] = []
        by_name: dict[str, dict[str, object]] = {}

        for skill_name, cap in active_items:
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
        # (``factory/chat_features/app-builder/SKILL.md``), not by a subdir SKILL.md, so it
        # must be registered from that one file — a directory scan of
        # ``factory/chat_features/app-builder`` would miss it and wrongly surface the
        # ``_template`` placeholder subdir instead.
        self._register_root_skill(
            self._repo_root / "factory" / "chat_features" / "app-builder",
            "features",
            skills_list,
            by_name,
        )

        # Backfill the run mode once the full skill set is known. Resolving it
        # up front would only cover the skills that HAVE a persisted override;
        # a never-configured skill still has an effective mode (its per-skill
        # default), and the panel must show the same value the Skills page
        # shows. One provider call for all ids (the store is a single KV row).
        modes = await self._resolve_modes(list(by_name))
        for sname, entry in by_name.items():
            entry["mode"] = modes.get(sname, "")

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
        skill_name, description, display_name = parsed
        if skill_name in by_name:
            # Already present from the capability registry (or an override).
            # Refresh source label so filesystem-discovered classification wins,
            # AND backfill display_name — SkillCapability has no H1 field, so
            # only the SKILL.md scan can supply the human-readable title.
            existing = by_name[skill_name]
            existing["source"] = source_label
            if not existing.get("display_name"):
                existing["display_name"] = display_name or skill_name
            return
        entry: dict[str, object] = {
            "skill_name": skill_name,
            "capability_name": skill_name,
            "read_paths": [],
            "write_paths": [],
            "exec_paths": [],
            "trusted_binaries": [],
            "description": description or "",
            "display_name": display_name or skill_name,
            "source": source_label,
            "active": False,
            "has_policy": False,
            "raw_read": [],
            "raw_write": [],
            "raw_trusted_binaries": [],
            "mode": "",
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
                # source label if it was classified differently. Backfill
                # display_name for the same reason as _register_root_skill —
                # SkillCapability carries no H1, only SKILL.md does.
                existing = by_name[sname]
                existing["source"] = source_label
                if not existing.get("display_name"):
                    existing["display_name"] = skill_info.display_name or sname
                continue
            entry: dict[str, object] = {
                "skill_name": sname,
                "capability_name": sname,
                "read_paths": [],
                "write_paths": [],
                "exec_paths": [],
                "trusted_binaries": [],
                "description": skill_info.description or "",
                "display_name": skill_info.display_name or sname,
                "source": source_label,
                "active": False,
                "has_policy": False,
                "raw_read": [],
                "raw_write": [],
                "raw_trusted_binaries": [],
                "mode": "",
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
