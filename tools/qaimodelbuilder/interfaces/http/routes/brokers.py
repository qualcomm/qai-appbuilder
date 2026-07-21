# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Broker HTTP routes (PR-603) — dependency_approval + command_policy.

Surfaces the dependency_approval approval queue and command_policy profile
listing over ``/api/security/dependency_approval/*`` and
``/api/security/exec_profiles``.

Endpoints (5 total):

* ``GET    /api/security/dependency_approval/pending``  — list pending dep-install requests
* ``POST   /api/security/dependency_approval/approve``  — approve a pending request
* ``POST   /api/security/dependency_approval/reject``   — reject a pending request
* ``GET    /api/security/exec_profiles``       — list loaded command_policy profiles
* ``POST   /api/security/exec_profiles/reload``— hot-reload exec profiles (L-6)
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from apps.api._dependency_approval_di import DependencyApprovalServices
    from apps.api._command_policy_di import CommandPolicyServices


# ---------------------------------------------------------------------------
# Wire-format DTOs
# ---------------------------------------------------------------------------


class _PendingRequestDTO(BaseModel):
    """Wire shape for a dependency_approval pending request."""

    id: str
    command_args: list[str]
    requester: str
    created_at: str
    status: str


class PendingListResponse(BaseModel):
    """``GET /api/security/dependency_approval/pending`` payload."""

    pending: list[_PendingRequestDTO]


class ResolveRequestBody(BaseModel):
    """Body for approve/reject endpoints."""

    id: str = Field(..., min_length=1, max_length=256)


class ResolveResponse(BaseModel):
    """Response for approve/reject endpoints."""

    success: bool
    decision: str


class _ExecProfileDTO(BaseModel):
    """Wire shape for a command_policy profile.

    The locked fields ``name`` / ``allowed_commands`` / ``deny_patterns``
    keep their original wire names (§3.1) and are mapped from the
    V1-equivalent domain fields (``allowed_args`` / ``denied_args``).
    The remaining fields are tail-appended (§3.1 additive) to surface
    the full V1-equivalent profile (decision 1, 2026-06-08): match glob,
    description, structured arg lists, io constraints, source skill.
    """

    name: str
    allowed_commands: list[str]
    deny_patterns: list[str]
    # Tail-appended V1-equivalent fields (additive, optional).
    description: str = ""
    match_glob: str = ""
    allowed_args: list[str] = []
    denied_args: list[str] = []
    io_constraints: dict = {}
    source_skill: str = ""
    # Tail-appended guard-rail fields (2026-07-06, additive/optional) — surface
    # the ALLOW/ASK/DENY danger-classification config so operators can inspect
    # it via API/CLI instead of reading the toml directly.
    match_globs: list[str] = []
    ask_args: list[str] = []
    hard_deny_args: list[str] = []
    ask_rules: list[dict] = []


class ExecProfilesResponse(BaseModel):
    """``GET /api/security/exec_profiles`` payload."""

    profiles: list[_ExecProfileDTO]
    enabled: bool


class ExecProfilesReloadResponse(BaseModel):
    """``POST /api/security/exec_profiles/reload`` payload (L-6)."""

    reloaded: bool
    count: int


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(
    *,
    dependency_approval_services: "DependencyApprovalServices",
    command_policy_services: "CommandPolicyServices",
    exec_profiles_dir: "Path | None" = None,
) -> APIRouter:
    """Build the brokers router bound to the given DI services.

    The router holds no module-level state; it is reconstructed every
    time the app is created. ``exec_profiles_dir`` (when supplied) enables
    the L-6 hot-reload endpoint; absent it the endpoint reports
    ``reloaded=False`` (graceful — minimal builds without a profile dir).
    """
    router = APIRouter(prefix="/api/security", tags=["brokers"])

    # ── dependency_approval ────────────────────────────────────────────

    @router.get("/dependency_approval/pending", response_model=PendingListResponse)
    async def dependency_approval_pending() -> PendingListResponse:
        uc = dependency_approval_services.get_pending_requests_use_case
        pending = await uc.execute()
        return PendingListResponse(
            pending=[
                _PendingRequestDTO(
                    id=r.id,
                    command_args=r.command_args,
                    requester=r.requester,
                    created_at=r.created_at.isoformat(),
                    status=r.status.value,
                )
                for r in pending
            ],
        )

    @router.post("/dependency_approval/approve", response_model=ResolveResponse)
    async def dependency_approval_approve(body: ResolveRequestBody) -> ResolveResponse:
        uc = dependency_approval_services.resolve_request_use_case
        success = await uc.execute(body.id, "approve")
        return ResolveResponse(success=success, decision="approve")

    @router.post("/dependency_approval/reject", response_model=ResolveResponse)
    async def dependency_approval_reject(body: ResolveRequestBody) -> ResolveResponse:
        uc = dependency_approval_services.resolve_request_use_case
        success = await uc.execute(body.id, "reject")
        return ResolveResponse(success=success, decision="reject")

    # ── command_policy ─────────────────────────────────────────────────

    @router.get("/exec_profiles", response_model=ExecProfilesResponse)
    async def exec_profiles() -> ExecProfilesResponse:
        uc = command_policy_services.get_exec_profiles_use_case
        result = await uc.execute()
        return ExecProfilesResponse(
            profiles=[
                _ExecProfileDTO(
                    name=p.name,
                    # Locked wire names mapped from V1-equivalent domain
                    # fields (allowed_args / denied_args).
                    allowed_commands=p.allowed_args,
                    deny_patterns=p.denied_args,
                    description=p.description,
                    match_glob=p.match_glob,
                    allowed_args=p.allowed_args,
                    denied_args=p.denied_args,
                    io_constraints=p.io_constraints,
                    source_skill=p.source_skill,
                    match_globs=list(getattr(p, "match_globs", []) or []),
                    ask_args=list(getattr(p, "ask_args", []) or []),
                    hard_deny_args=list(getattr(p, "hard_deny_args", []) or []),
                    ask_rules=list(getattr(p, "ask_rules", []) or []),
                )
                for p in result.profiles
            ],
            enabled=result.enabled,
        )

    @router.post(
        "/exec_profiles/reload", response_model=ExecProfilesReloadResponse
    )
    async def exec_profiles_reload() -> ExecProfilesReloadResponse:
        """Hot-reload exec profiles from disk (L-6, V1 ``reload``).

        Re-reads ``<exec_profiles_dir>/*.toml`` and atomically swaps the
        in-memory set on the live broker so an operator's profile edits
        take effect without a restart. The filesystem load lives here
        (infrastructure) and the adapter only holds state — see
        ``InMemoryExecBroker.replace_profiles``.
        """
        broker = getattr(command_policy_services, "broker", None)
        replace = getattr(broker, "replace_profiles", None)
        if exec_profiles_dir is None or not callable(replace):
            return ExecProfilesReloadResponse(reloaded=False, count=0)
        from qai.command_policy.infrastructure.profile_loader import load_all

        profiles = load_all(exec_profiles_dir)
        replace(profiles)
        return ExecProfilesReloadResponse(
            reloaded=True, count=len(profiles)
        )

    return router
