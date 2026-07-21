# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Skills policy + business-registry use cases (R6).

Two families of skill endpoints live in ``user_prefs``:

1. **Policy** (PR-606): ``GET /api/skills/policy`` + ``POST
   /api/skills/{set_mode,toggle,reload}`` — aggregate ``skills.mode`` /
   ``skills.overrides`` / ``skills.last_reload`` state persisted in the
   ``forge.config`` document. Each writer is a read-modify-write of the
   ``skills`` sub-key.
2. **Business registry**: ``GET /api/skills`` + ``POST /api/skills/
   {skill_id}/set_mode`` — live-scan the on-disk ``skills/`` directory
   (via the :class:`SkillDiscovery` platform port), merge each skill's
   persisted per-skill 4-state mode from ``skills.overrides``, and
   enforce the NPU requirement for ``local``/``both`` modes.

The directory scan + ``_resolve_mode`` override merge + NPU validation
are application policy; they previously lived inline in the route layer.
Moving them here keeps ``interfaces/`` declarative.

Result shapes are returned to the route 1:1 with the legacy handlers;
NPU-violation / unknown-skill error conditions are surfaced as
domain-level exceptions (:class:`SkillNotFoundError` /
:class:`SkillModeNotAllowedError`) so the route maps them to the same
404 / 400 responses without embedding the validation logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from qai.platform.skills import (
    NPU_MODES as NPU_SKILL_MODES,
    VALID_MODES as VALID_SKILL_MODES,
    SkillDiscovery,
)
from qai.user_prefs.application.use_cases.load_document import (
    LoadDocumentUseCase,
)
from qai.user_prefs.application.use_cases.save_document import (
    SaveDocumentUseCase,
)

__all__ = [
    "GetSkillPolicyUseCase",
    "ListSkillsUseCase",
    "ReloadSkillsUseCase",
    "SetSkillModeUseCase",
    "SetSkillPolicyModeUseCase",
    "SkillModeNotAllowedError",
    "SkillNotFoundError",
    "ToggleSkillUseCase",
]

_SKILLS_SUBKEY = "skills"


class SkillNotFoundError(LookupError):
    """Raised when a ``skill_id`` is not a discovered skill (→ 404)."""

    def __init__(self, skill_id: str) -> None:
        self.skill_id = skill_id
        super().__init__(f"Unknown skill: {skill_id}")


class SkillModeNotAllowedError(ValueError):
    """Raised when a mode requires NPU optimisation but the skill lacks it.

    (→ 400, v1 ``set_mode`` ValueError parity.)
    """

    def __init__(self, skill_id: str, mode: str) -> None:
        self.skill_id = skill_id
        self.mode = mode
        super().__init__(
            f"Skill '{skill_id}' is not NPU-optimized; "
            f"mode '{mode}' requires NPU optimization."
        )


def _coerce_section(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


#: Per-skill DEFAULT run mode used when the user has NOT persisted an explicit
#: ``skills.overrides[<skill_id>]`` entry in ``forge.config``. Skills absent
#: from this map fall through to ``"cloud"`` (enabled). This is how a built-in
#: chat-feature skill ships DISABLED-by-default while remaining user-toggleable
#: (a persisted override always wins over this default). ``model-hub`` (the
#: promoted former ``aihub-model-run`` skill) is a first-class feature mode and
#: falls through to ``"cloud"`` (enabled) here like ``model-builder``.
#: Requested default: ppt-gen / code-assist / meetingminutes ship OFF; the
#: rest (e.g. model-builder, model-hub) stay on.
_DEFAULT_SKILL_MODE: dict[str, str] = {
    "ppt-gen": "off",
    "code-assist": "off",
    "meetingminutes": "off",
}


def _resolve_mode(
    override: Any, npu_optimized: bool, skill_id: str = "",
) -> str:
    """Merge a per-skill forge.config override into the discovered mode.

    Precedence (mirrors v1 + the existing /api/skills/toggle data):
      1. explicit ``mode`` key (off/cloud/local/both) if present & valid;
         a persisted local/both that is no longer NPU-optimised falls
         back to 'cloud' (v1 _load_all reset behaviour).
      2. else legacy ``enabled`` bool: False -> 'off', True -> 'cloud'.
      3. else the per-skill default (``_DEFAULT_SKILL_MODE``), defaulting to
         'cloud' (enabled) for skills not listed there.
    """
    if isinstance(override, dict):
        mode = override.get("mode")
        if isinstance(mode, str) and mode in VALID_SKILL_MODES:
            if mode in NPU_SKILL_MODES and not npu_optimized:
                return "cloud"
            return mode
        enabled = override.get("enabled")
        if isinstance(enabled, bool):
            return "cloud" if enabled else "off"
    return _DEFAULT_SKILL_MODE.get(skill_id, "cloud")


# ---------------------------------------------------------------------------
# Policy use cases (PR-606)
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class GetSkillPolicyUseCase:
    """Return aggregated skill policy state from forge.config."""

    load_document_use_case: LoadDocumentUseCase
    forge_config_key: str

    async def execute(self) -> dict[str, Any]:
        doc = await self.load_document_use_case.execute(self.forge_config_key)
        skills = _coerce_section(doc.get(_SKILLS_SUBKEY, {}))
        return {
            "mode": skills.get("mode", "auto"),
            "overrides": skills.get("overrides", {}),
            "last_reload": skills.get("last_reload", None),
        }


@dataclass(slots=True, frozen=True)
class SetSkillPolicyModeUseCase:
    """Persist the global skill mode preference in ``skills.mode``."""

    load_document_use_case: LoadDocumentUseCase
    save_document_use_case: SaveDocumentUseCase
    forge_config_key: str

    async def execute(self, mode: str) -> dict[str, Any]:
        doc = await self.load_document_use_case.execute(self.forge_config_key)
        skills = _coerce_section(doc.get(_SKILLS_SUBKEY, {}))
        skills["mode"] = mode
        await self.save_document_use_case.execute(
            self.forge_config_key, updates={_SKILLS_SUBKEY: skills}
        )
        return {"status": "saved", "mode": mode}


@dataclass(slots=True, frozen=True)
class ToggleSkillUseCase:
    """Persist a per-skill ``enabled`` flag in ``skills.overrides``.

    When ``skill_discovery`` is wired (optional), pinned skills are
    silently ignored so they cannot be disabled via the toggle API.
    """

    load_document_use_case: LoadDocumentUseCase
    save_document_use_case: SaveDocumentUseCase
    forge_config_key: str
    skill_discovery: SkillDiscovery | None = None

    async def execute(self, skill_name: str, enabled: bool) -> dict[str, Any]:
        # Pinned skills are always-on; toggle is silently ignored.
        if self.skill_discovery is not None:
            skill = self.skill_discovery.find(skill_name)
            if skill is not None and skill.pinned:
                return {
                    "status": "pinned",
                    "skill_name": skill_name,
                    "enabled": True,
                }
        doc = await self.load_document_use_case.execute(self.forge_config_key)
        skills = _coerce_section(doc.get(_SKILLS_SUBKEY, {}))
        overrides = skills.get("overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
        overrides.setdefault(skill_name, {})["enabled"] = enabled
        skills["overrides"] = overrides
        await self.save_document_use_case.execute(
            self.forge_config_key, updates={_SKILLS_SUBKEY: skills}
        )
        return {
            "status": "saved",
            "skill_name": skill_name,
            "enabled": enabled,
        }


@dataclass(slots=True, frozen=True)
class ReloadSkillsUseCase:
    """Persist a ``skills.last_reload`` UTC timestamp."""

    load_document_use_case: LoadDocumentUseCase
    save_document_use_case: SaveDocumentUseCase
    forge_config_key: str

    async def execute(self) -> dict[str, Any]:
        doc = await self.load_document_use_case.execute(self.forge_config_key)
        skills = _coerce_section(doc.get(_SKILLS_SUBKEY, {}))
        skills["last_reload"] = datetime.now(UTC).isoformat()
        await self.save_document_use_case.execute(
            self.forge_config_key, updates={_SKILLS_SUBKEY: skills}
        )
        return {"status": "reloaded"}


# ---------------------------------------------------------------------------
# Business-registry use cases (directory discovery + per-skill mode)
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class ListSkillsUseCase:
    """Return the v1-shaped skill business list.

    Scans the ``skills/`` directory live on every call (no cache, v1
    reload parity) and merges each skill's persisted per-skill mode from
    ``forge.config skills.overrides``.
    """

    load_document_use_case: LoadDocumentUseCase
    skill_discovery: SkillDiscovery
    forge_config_key: str

    async def execute(self) -> dict[str, Any]:
        doc = await self.load_document_use_case.execute(self.forge_config_key)
        skills_doc = _coerce_section(doc.get(_SKILLS_SUBKEY, {}))
        overrides = skills_doc.get("overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
        out: list[dict[str, Any]] = []
        for skill in self.skill_discovery.scan():
            # Pinned skills are always-on; ignore user overrides.
            if not skill.pinned:
                skill.mode = _resolve_mode(
                    overrides.get(skill.skill_id),
                    skill.npu_optimized,
                    skill.skill_id,
                )
            out.append(skill.to_dict())
        return {"skills": out}


@dataclass(slots=True, frozen=True)
class SetSkillModeUseCase:
    """Set + persist a per-skill 4-state run mode.

    Raises:
        SkillNotFoundError: ``skill_id`` is not discovered (→ 404).
        SkillModeNotAllowedError: ``mode`` is ``local``/``both`` but the
            skill is not NPU-optimised (→ 400).

    Persists to ``forge.config skills.overrides[skill_id].mode``,
    tail-appending the ``mode`` sub-key alongside any existing
    ``enabled`` flag written by ``/api/skills/toggle``.
    """

    load_document_use_case: LoadDocumentUseCase
    save_document_use_case: SaveDocumentUseCase
    skill_discovery: SkillDiscovery
    forge_config_key: str

    async def execute(self, skill_id: str, mode: str) -> dict[str, Any]:
        skill = self.skill_discovery.find(skill_id)
        if skill is None:
            raise SkillNotFoundError(skill_id)
        if skill.pinned:
            # Pinned skills are always-on; mode changes are silently ignored
            # so the UI can call this without error but the skill stays active.
            return {
                "skill_id": skill_id,
                "mode": skill.mode,
                "npu_optimized": skill.npu_optimized,
                "pinned": True,
            }
        if mode in NPU_SKILL_MODES and not skill.npu_optimized:
            raise SkillModeNotAllowedError(skill_id, mode)
        doc = await self.load_document_use_case.execute(self.forge_config_key)
        skills = _coerce_section(doc.get(_SKILLS_SUBKEY, {}))
        overrides = skills.get("overrides", {})
        if not isinstance(overrides, dict):
            overrides = {}
        entry = overrides.get(skill_id, {})
        if not isinstance(entry, dict):
            entry = {}
        entry["mode"] = mode
        overrides[skill_id] = entry
        skills["overrides"] = overrides
        await self.save_document_use_case.execute(
            self.forge_config_key, updates={_SKILLS_SUBKEY: skills}
        )
        return {
            "skill_id": skill_id,
            "mode": mode,
            "npu_optimized": skill.npu_optimized,
        }
