# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Security routes — auto_approve config (legacy) + full V1-aligned config endpoints. (split from security.py).

Pure-move extraction (zero behaviour change): the route handlers are
byte-identical to the originals; they were nested closures inside
``build_router`` and are now nested inside this registrar instead,
still capturing the ``container`` passed in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._dto import (
    AutoApproveConfigRequest,
    AutoApproveConfigResponse,
    AutoApproveFullRequest,
    AutoApproveFullResponse,
    AutoApproveToolToggles,
    CommandListConfig,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from apps.api.di import Container


def _register_auto_approve_routes(router: "APIRouter", *, container: "Container") -> None:
    # ── auto_approve (2) ──────────────────────────────────────────────

    @router.get(
        "/auto_approve/config",
        response_model=AutoApproveConfigResponse,
    )
    async def auto_approve_get() -> AutoApproveConfigResponse:
        cfg = container.security.security_runtime_state.get_settings(
            "auto_approve"
        ) or {"enabled": False, "trusted_paths": []}
        return AutoApproveConfigResponse(
            enabled=bool(cfg.get("enabled", False)),
            trusted_paths=list(cfg.get("trusted_paths", [])),
        )

    @router.put(
        "/auto_approve/config",
        response_model=AutoApproveConfigResponse,
    )
    async def auto_approve_put(
        body: AutoApproveConfigRequest,
    ) -> AutoApproveConfigResponse:
        updated = container.security.security_runtime_state.update_settings(
            "auto_approve",
            {
                "enabled": body.enabled,
                "trusted_paths": list(body.trusted_paths),
            },
        )
        return AutoApproveConfigResponse(
            enabled=bool(updated["enabled"]),
            trusted_paths=list(updated["trusted_paths"]),
        )

    # ── auto_approve (V1-aligned full config: tool toggles + cmd lists) ──
    # New top-level GET/PUT /auto_approve (separate path & bucket from
    # /auto_approve/config above). Stores under `auto_approve_tool` so the
    # legacy `enabled`/`trusted_paths` bucket stays untouched.
    _AUTO_APPROVE_TOOL_BUCKET = "auto_approve_tool"

    def _default_auto_approve_tool_state() -> dict[str, Any]:
        # V1: blacklist defaults to ENABLED. SEC-BLACKLIST-UI-TRANSPARENCY-1 —
        # surface the SAME built-in dangerous-command prefixes the enforcement
        # adapter falls back to (``RuntimeStateAutoApproveAdapter``), so a
        # fresh install's 自动审批 panel HONESTLY shows the default protection
        # instead of an empty list that made users think nothing was blocked.
        # Single source of truth: the DOMAIN constant
        # ``qai.security.domain.command_blacklist.DEFAULT_COMMAND_BLACKLIST_PREFIXES``
        # (this route only mirrors it for display; the enforcement adapter
        # reads the same constant). The user can still edit/clear the list via
        # PUT; once they save, their bucket (empty or not) wins over this
        # default. Importing the domain layer from a route is allowed by the
        # ``interfaces-stays-thin`` contract (only ``adapters`` /
        # ``infrastructure`` are forbidden).
        from qai.security.domain.command_blacklist import (
            DEFAULT_COMMAND_BLACKLIST_PREFIXES,
        )

        return {
            "auto_approve": {
                "read": False,
                "write": False,
                "exec": False,
                "glob": False,
                "grep": False,
            },
            "command_whitelist": {"enabled": False, "prefixes": []},
            "command_blacklist": {
                "enabled": True,
                "prefixes": list(DEFAULT_COMMAND_BLACKLIST_PREFIXES),
            },
        }

    def _build_auto_approve_full_response(
        cfg: dict[str, Any],
    ) -> AutoApproveFullResponse:
        aa_raw = cfg.get("auto_approve", {}) or {}
        wl_raw = cfg.get("command_whitelist", {}) or {}
        bl_raw = cfg.get("command_blacklist", {}) or {}
        return AutoApproveFullResponse(
            auto_approve=AutoApproveToolToggles(
                read=bool(aa_raw.get("read", False)),
                write=bool(aa_raw.get("write", False)),
                exec=bool(aa_raw.get("exec", False)),
                glob=bool(aa_raw.get("glob", False)),
                grep=bool(aa_raw.get("grep", False)),
            ),
            command_whitelist=CommandListConfig(
                enabled=bool(wl_raw.get("enabled", False)),
                prefixes=list(wl_raw.get("prefixes", [])),
            ),
            command_blacklist=CommandListConfig(
                # V1: only false when explicitly false.
                enabled=bl_raw.get("enabled", True) is not False,
                prefixes=list(bl_raw.get("prefixes", [])),
            ),
        )

    @router.get(
        "/auto_approve",
        response_model=AutoApproveFullResponse,
    )
    async def auto_approve_full_get() -> AutoApproveFullResponse:
        cfg = container.security.security_runtime_state.get_settings(
            _AUTO_APPROVE_TOOL_BUCKET
        ) or _default_auto_approve_tool_state()
        return _build_auto_approve_full_response(cfg)

    @router.put(
        "/auto_approve",
        response_model=AutoApproveFullResponse,
    )
    async def auto_approve_full_put(
        body: AutoApproveFullRequest,
    ) -> AutoApproveFullResponse:
        updated = container.security.security_runtime_state.update_settings(
            _AUTO_APPROVE_TOOL_BUCKET,
            {
                "auto_approve": body.auto_approve.model_dump(),
                "command_whitelist": body.command_whitelist.model_dump(),
                "command_blacklist": body.command_blacklist.model_dump(),
            },
        )
        return _build_auto_approve_full_response(updated)
