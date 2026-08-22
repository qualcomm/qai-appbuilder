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
        # Read existing bucket so partial updates preserve sibling fields
        # (a caller sending only read_allow_patterns keeps write_allow_patterns
        # intact).
        existing = container.security.security_runtime_state.get_settings(
            "path_patterns"
        ) or {}
        merged: dict[str, Any] = {
            # Preserve existing fields by default.
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

