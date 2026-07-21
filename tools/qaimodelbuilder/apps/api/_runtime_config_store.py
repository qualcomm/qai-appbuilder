# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared forge_config persistence for the unified security-settings surface.

Background
----------
The 2026-06 security-settings unification gave the WebUI three authoritative
state surfaces that must each survive a restart (decision 2A):

1. **Typed security/tools switches** — ``file_guard_enabled`` /
   ``allow_exec_tool`` / ``sandbox_enabled`` (``SecuritySettings``) and
   ``file_broker_enabled`` / ``ssl_verify`` / ``project_skip_dirs`` /
   ``global_proxy`` (``ToolsSettings``). These drive backend behaviour but the
   pydantic ``Settings`` model is immutable per process, so the
   ``GET/PUT /api/security/runtime-config`` route persists operator edits here
   and :func:`load_runtime_config_overrides` feeds them back into
   ``load_settings(overrides=...)`` at the next boot.
2. **Security runtime-state buckets** — the operator-tunable
   ``auto_approve`` / ``path_patterns`` / ``project_access`` /
   ``skill_policies`` buckets owned by
   :class:`qai.security.application.security_runtime_state.SecurityRuntimeStateService`.
   :class:`ForgeRuntimeStatePersistence` implements the security-context
   :class:`RuntimeStatePersistencePort` against this same file.

Why one module
--------------
Both surfaces live in the single shared ``<data>/config/forge_config.json``
document (the same file chat-hooks / proxy / the six legacy KV sections used).
Centralising the read-modify-write here keeps the route layer and the
persistence adapter from each re-deriving the path + JSON I/O, and keeps the
two regions (``security_runtime_config`` and ``security_runtime_state``) from
clobbering each other on partial writes.

Clean Architecture
-------------------
This is an **apps-layer** helper: it is allowed to touch the filesystem and to
know the forge_config layout. The security *context* only sees the abstract
:class:`RuntimeStatePersistencePort`; the typed-switch persistence is consumed
by ``load_settings`` (platform config) which already owns the override merge.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

__all__ = [
    "RUNTIME_CONFIG_KEY",
    "RUNTIME_STATE_KEY",
    "TOOL_OUTPUT_SWITCH_FIELDS",
    "ForgeRuntimeStatePersistence",
    "forge_config_path",
    "load_runtime_config_overrides",
    "read_runtime_config",
    "write_runtime_config",
]

_LOGGER = logging.getLogger("qai.api.runtime_config")

# Forge-config sub-keys owned by the unified security-settings surface.
RUNTIME_CONFIG_KEY = "security_runtime_config"
RUNTIME_STATE_KEY = "security_runtime_state"
# Legacy forge_config sub-key (pre 2026-07 sandbox→path rename). Still READ on
# load so runtime state persisted by an older build is not silently lost; new
# writes always use ``RUNTIME_STATE_KEY``.
_LEGACY_RUNTIME_STATE_KEY = "sandbox_runtime_state"

# The whitelisted typed switches the ``/api/security/runtime-config`` surface
# persists. Keyed by the Settings sub-model they belong to so
# :func:`load_runtime_config_overrides` can build the nested overrides dict.
SECURITY_SWITCH_FIELDS: tuple[str, ...] = (
    "file_guard_enabled",
    "allow_exec_tool",
    "sandbox_enabled",
    "native_file_guard_enabled",
)
TOOLS_SWITCH_FIELDS: tuple[str, ...] = (
    "file_broker_enabled",
    "file_broker_max_entries",
    # ssl_verify removed from tools sub-model (2026-07-10): it is now a
    # top-level Settings field (Settings.ssl_verify) so it must be persisted
    # as a top-level override, not nested under "tools". See TOP_LEVEL_FIELDS.
    "project_skip_dirs",
    "global_proxy",
)
# Top-level Settings fields that are persisted via the runtime-config route
# and must be mapped back as top-level overrides (not nested under a sub-model).
TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "ssl_verify",
)
# tool_output in-prompt size caps (ToolOutputSettings). Persisted under the
# same runtime-config region; mapped back into the ``tool_output`` Settings
# sub-model by :func:`load_runtime_config_overrides`.
TOOL_OUTPUT_SWITCH_FIELDS: tuple[str, ...] = (
    "read_max_lines",
    "read_max_bytes",
    "read_max_line_length",
    "glob_max_results",
    "grep_max_matches",
    "grep_max_line_length",
    "grep_max_output_bytes",
)


def forge_config_path(data_root: Path) -> Path:
    """Resolve ``<data>/config/forge_config.json``.

    Mirrors ``interfaces/http/routes/user_prefs.py::_forge_config_file_path``
    and ``apps/api/_chat_di.py`` so every surface operates on one file.
    """
    return data_root / "config" / "forge_config.json"


def _read_doc(data_root: Path) -> dict[str, Any]:
    path = forge_config_path(data_root)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _LOGGER.warning("forge_config read failed at %s", path, exc_info=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_doc(data_root: Path, doc: dict[str, Any]) -> None:
    path = forge_config_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Typed-switch surface (GET/PUT /api/security/runtime-config)
# ---------------------------------------------------------------------------


def read_runtime_config(data_root: Path) -> dict[str, Any]:
    """Return the persisted ``security_runtime_config`` region (``{}`` absent)."""
    region = _read_doc(data_root).get(RUNTIME_CONFIG_KEY, {})
    return region if isinstance(region, dict) else {}


def write_runtime_config(data_root: Path, partial: dict[str, Any]) -> dict[str, Any]:
    """Merge *partial* into the persisted runtime-config region; return it.

    A field-whitelisted shallow merge: only the known security/tools switch
    fields are persisted, everything else in *partial* is ignored.
    """
    allowed = (
        set(SECURITY_SWITCH_FIELDS)
        | set(TOOLS_SWITCH_FIELDS)
        | set(TOOL_OUTPUT_SWITCH_FIELDS)
        | set(TOP_LEVEL_FIELDS)
    )
    doc = _read_doc(data_root)
    region = doc.get(RUNTIME_CONFIG_KEY, {})
    if not isinstance(region, dict):
        region = {}
    for key, value in partial.items():
        if key in allowed:
            region[key] = value
    doc[RUNTIME_CONFIG_KEY] = region
    _write_doc(data_root, doc)
    return region


def load_runtime_config_overrides(data_root: Path) -> dict[str, Any]:
    """Build the ``load_settings(overrides=...)`` dict from persisted switches.

    Returns a nested ``{"security": {...}, "tools": {...},
    "tool_output": {...}}`` dict containing only the switches the operator has
    persisted via the runtime-config route. An empty dict when nothing was
    persisted, so a fresh install keeps the ``server.toml`` / env / default
    precedence untouched.
    """
    region = read_runtime_config(data_root)
    if not region:
        return {}
    security: dict[str, Any] = {}
    tools: dict[str, Any] = {}
    tool_output: dict[str, Any] = {}
    top_level: dict[str, Any] = {}
    for field in SECURITY_SWITCH_FIELDS:
        if field in region:
            security[field] = region[field]
    for field in TOOLS_SWITCH_FIELDS:
        if field in region:
            tools[field] = region[field]
    for field in TOOL_OUTPUT_SWITCH_FIELDS:
        if field in region:
            tool_output[field] = region[field]
    # 2026-07-10: top-level Settings fields (e.g. ssl_verify) must be mapped
    # as top-level overrides so load_settings picks them up correctly.
    for field in TOP_LEVEL_FIELDS:
        if field in region:
            top_level[field] = region[field]
    overrides: dict[str, Any] = {}
    if security:
        overrides["security"] = security
    if tools:
        overrides["tools"] = tools
    if tool_output:
        overrides["tool_output"] = tool_output
    # Top-level fields are merged directly (not nested).
    overrides.update(top_level)
    return overrides


# ---------------------------------------------------------------------------
# Sandbox runtime-state surface (RuntimeStatePersistencePort)
# ---------------------------------------------------------------------------


class ForgeRuntimeStatePersistence:
    """Persist the security runtime-state buckets into ``forge_config``.

    Implements the security-context
    :class:`qai.security.application.ports.RuntimeStatePersistencePort` against
    the shared forge_config document. Synchronous + best-effort: the
    :class:`SecurityRuntimeStateService` swallows ``save`` failures so a UI
    toggle never crashes on a transient disk error.
    """

    def __init__(self, *, data_root: Path) -> None:
        self._data_root = data_root

    def load(self) -> dict[str, Any]:
        doc = _read_doc(self._data_root)
        # New key first; fall back to the pre-rename ``sandbox_runtime_state``
        # bucket so state written by an older build survives the upgrade.
        region = doc.get(RUNTIME_STATE_KEY)
        if region is None:
            region = doc.get(_LEGACY_RUNTIME_STATE_KEY, {})
        return region if isinstance(region, dict) else {}

    def save(self, state: dict[str, Any]) -> None:
        doc = _read_doc(self._data_root)
        doc[RUNTIME_STATE_KEY] = state
        _write_doc(self._data_root, doc)
