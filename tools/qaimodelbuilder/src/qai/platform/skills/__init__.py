# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.platform.skills — Cross-BC skill directory discovery.

Shared-kernel module that scans the on-disk ``skills/`` directory tree,
parses each skill's ``SKILL.md`` (YAML front-matter) or ``skill.json``
metadata, detects NPU optimisation, and resolves icon / SKILL.md paths.

This is the v2 port of the v1 ``backend.skill_manager.SkillManager``
discovery half. Mode persistence (per-skill ``off/cloud/local/both``)
lives in the ``user_prefs`` bounded context (``forge.config`` document)
and is layered on top of the discovered metadata by the HTTP route layer.

Public API:
    - :class:`SkillInfo` — immutable discovered skill record.
    - :class:`SkillDiscovery` — scans a directory and returns ``SkillInfo``.
    - :data:`VALID_MODES` / :data:`NPU_MODES` — the 4-state mode sets.
"""

from __future__ import annotations

from qai.platform.skills.discovery import (
    NPU_MODES,
    VALID_MODES,
    SkillDiscovery,
    SkillInfo,
    parse_skill_metadata,
)
from qai.platform.skills.placeholders import (
    expand_skill_placeholders,
    get_app_root,
    reset_app_root,
    set_app_root,
)

__all__ = [
    "NPU_MODES",
    "VALID_MODES",
    "SkillDiscovery",
    "SkillInfo",
    "expand_skill_placeholders",
    "get_app_root",
    "parse_skill_metadata",
    "reset_app_root",
    "set_app_root",
]
