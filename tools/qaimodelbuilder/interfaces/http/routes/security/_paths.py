# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Security routes — path_patterns + project_access endpoints. (split from security.py).

Pure-move extraction (zero behaviour change): the route handlers are
byte-identical to the originals; they were nested closures inside
``build_router`` and are now nested inside this registrar instead,
still capturing the ``container`` passed in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._dto import (
    PathPatternsRequest,
    PathPatternsResponse,
    PatternConfig,
    ProjectAccessRequest,
    ProjectAccessResponse,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from apps.api.di import Container


def _register_paths_routes(router: "APIRouter", *, container: "Container") -> None:
    # ── path_patterns (2) ─────────────────────────────────────────────

    def _build_path_patterns_response(
        cfg: dict[str, Any],
    ) -> PathPatternsResponse:
        rp_raw = cfg.get("read_allow_patterns", {}) or {}
        wp_raw = cfg.get("write_allow_patterns", {}) or {}
        return PathPatternsResponse(
            deny=list(cfg.get("deny", [])),
            allow=list(cfg.get("allow", [])),
            read_allow_patterns=PatternConfig(
                enabled=bool(rp_raw.get("enabled", False)),
                patterns=list(rp_raw.get("patterns", [])),
            ),
            write_allow_patterns=PatternConfig(
                enabled=bool(wp_raw.get("enabled", False)),
                patterns=list(wp_raw.get("patterns", [])),
            ),
        )

    @router.get(
        "/path_patterns", response_model=PathPatternsResponse
    )
    async def path_patterns_get() -> PathPatternsResponse:
        cfg = container.security.security_runtime_state.get_settings(
            "path_patterns"
        ) or {}
        return _build_path_patterns_response(cfg)

    @router.put(
        "/path_patterns", response_model=PathPatternsResponse
    )
    async def path_patterns_put(
        body: PathPatternsRequest,
    ) -> PathPatternsResponse:
        # Read existing bucket so V1 tail-appended fields support partial
        # updates (legacy callers only sending deny/allow keep their
        # read_allow_patterns / write_allow_patterns intact).
        existing = container.security.security_runtime_state.get_settings(
            "path_patterns"
        ) or {}
        merged: dict[str, Any] = {
            "deny": list(body.deny),
            "allow": list(body.allow),
            # Preserve existing tail-appended fields by default.
            "read_allow_patterns": dict(
                existing.get("read_allow_patterns", {})
                or {"enabled": False, "patterns": []}
            ),
            "write_allow_patterns": dict(
                existing.get("write_allow_patterns", {})
                or {"enabled": False, "patterns": []}
            ),
        }
        if body.read_allow_patterns is not None:
            merged["read_allow_patterns"] = body.read_allow_patterns.model_dump()
        if body.write_allow_patterns is not None:
            merged["write_allow_patterns"] = body.write_allow_patterns.model_dump()

        updated = container.security.security_runtime_state.update_settings(
            "path_patterns", merged
        )
        return _build_path_patterns_response(updated)

    # ── project_access (2) ────────────────────────────────────────────

    # V1-aligned default skip directories (mirrors
    # frontend `useProjectAccess.DEFAULT_SKIP_DIRS` / V1 `resetSkipDirs`).
    _DEFAULT_SKIP_DIRS = [
        "venv",
        ".venv",
        "env",
        ".env",
        "node_modules",
        "__pycache__",
        ".git",
        "build",
        "dist",
        ".mypy_cache",
    ]

    @router.get(
        "/project_access", response_model=ProjectAccessResponse
    )
    async def project_access_get() -> ProjectAccessResponse:
        cfg = container.security.security_runtime_state.get_settings(
            "project_access"
        ) or {"enabled": True}
        return ProjectAccessResponse(
            enabled=bool(cfg.get("enabled", True)),
            path=str(cfg.get("path", "")),
            skip_dirs=list(cfg.get("skip_dirs", _DEFAULT_SKIP_DIRS)),
        )

    @router.put(
        "/project_access", response_model=ProjectAccessResponse
    )
    async def project_access_put(
        body: ProjectAccessRequest,
    ) -> ProjectAccessResponse:
        # Partial update (V1 `updateStatus(updates)`): start from existing
        # settings and overlay only the fields supplied by the client.
        current = container.security.security_runtime_state.get_settings(
            "project_access"
        ) or {"enabled": True}

        merged: dict[str, Any] = {
            "enabled": body.enabled,
            "path": str(current.get("path", "")),
            "skip_dirs": list(
                current.get("skip_dirs", _DEFAULT_SKIP_DIRS)
            ),
        }
        if body.path is not None:
            merged["path"] = body.path
        if body.skip_dirs is not None:
            merged["skip_dirs"] = list(body.skip_dirs)

        updated = container.security.security_runtime_state.update_settings(
            "project_access",
            merged,
        )
        # De-sandbox refactor (2026-07-04) — the project_access bucket is
        # persisted above via the runtime-state service (survives a restart
        # through ``ForgeRuntimeStatePersistence``) and the FileGuard
        # project-access gate re-reads the live bucket on every dispatch
        # (``apps.api._file_guard_bridge._project_access_provider``), so the
        # edit takes effect immediately without an API restart. The old
        # ``SandboxConfigChangedEvent`` emission (which rebuilt the deleted
        # ``SandboxConfigHolder``) was removed alongside the orphaned sandbox
        # execution framework.
        return ProjectAccessResponse(
            enabled=bool(updated["enabled"]),
            path=str(updated.get("path", "")),
            skip_dirs=list(updated.get("skip_dirs", [])),
        )
