# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Security HTTP routes (PR-031 + PR-040).

Surfaces the S2/S4 use cases (policy / permission / path grants /
cancel-permission) plus read-only inspection endpoints over
``/api/security/*``. Replaces the legacy 51 ``/api/security/*`` and
``/api/sandbox/*`` rows from ``inventory/02-routes.md`` §3.2 — see
``PR-031-manifest.md`` for the line-by-line coverage map.

Endpoints:

* ``GET    /api/security/policy``                                — read current Policy
* ``GET    /api/security/policy/version``                        — read current version only
* ``PUT    /api/security/policy``                                — UpdatePolicyUseCase
* ``POST   /api/security/permission/check``                      — CheckPermissionUseCase
* ``POST   /api/security/permission/request``                    — RequestPermissionUseCase
* ``GET    /api/security/permission/pending``                    — list pending requests
* ``POST   /api/security/permission/{request_id}/approve``       — ApprovePermissionUseCase
* ``POST   /api/security/permission/{request_id}/reject``        — RejectPermissionUseCase
* ``DELETE /api/security/permission/{request_id}``               — CancelPermissionRequestUseCase (PR-040, issue d)
* ``POST   /api/security/permission/cancel``                     — Phase 2 bulk cancel (request_id / pid / cancel_all)
* ``GET    /api/security/path-grants``                          — list grants for subject
* ``POST   /api/security/path-grants``                          — CreatePathGrantUseCase
* ``DELETE /api/security/path-grants/{grant_id}``               — RevokePathGrantUseCase
* ``GET    /api/security/audit/recent``                          — list recent audit entries (now via AuditQueryPort, issue a)

Errors are signalled by raising the appropriate :class:`qai.platform.errors.QaiError`
subclass (or a security-context subclass thereof). The unified envelope
handler installed by PR-030 turns those into the canonical
``{type, code, message, details?}`` JSON.

EXIT 75 contract: ``PUT /api/security/policy`` ultimately calls
:class:`qai.security.application.use_cases.update_policy.UpdatePolicyUseCase`
which, when the rule set changes, invokes
:class:`qai.security.application.ports.RebootSignalPort.request_reboot`.
The supervisor process performs the actual ``exit(75)``; this route
layer only carries the intent.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

from ._auto_approve import _register_auto_approve_routes as _register_auto_approve_routes
from ._dangerous_commands import (
    _register_dangerous_commands_routes as _register_dangerous_commands_routes,
)
from ._grants import _register_grants_routes as _register_grants_routes
from ._paths import _register_paths_routes as _register_paths_routes
from ._permission import _register_permission_routes as _register_permission_routes
from ._policy import _register_policy_routes as _register_policy_routes
from ._runtime_config import _register_runtime_config_routes as _register_runtime_config_routes
from ._skills import _register_skills_routes as _register_skills_routes

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


def build_router(*, container: "Container") -> APIRouter:
    """Build the security router bound to the given DI container.

    The router holds no module-level state; it is reconstructed every
    time :func:`apps.api.main.create_app` is called. The per-resource
    registrars below attach their handlers to this single router.

    Phase 3 cleanup (2026-07-01) removed the
    ``interfaces/http/routes/security/_sandbox.py`` registrar that
    previously owned ``GET/POST /api/security/sandbox/{status,toggle,
    settings,test,reset,stats,batch,execute}``; those routes consumed
    the AppContainer/LPAC launcher chain (``SandboxedProcessRunner`` /
    ``SandboxPolicyBuilder`` / ``DaemonManager`` /
    ``launcher_resolver``) which has been deleted alongside.
    """
    router = APIRouter(prefix="/api/security", tags=["security"])
    _register_policy_routes(router, container=container)
    _register_permission_routes(router, container=container)
    _register_grants_routes(router, container=container)
    _register_auto_approve_routes(router, container=container)
    _register_paths_routes(router, container=container)
    _register_skills_routes(router, container=container)
    _register_dangerous_commands_routes(router, container=container)
    _register_runtime_config_routes(router, container=container)
    return router


__all__ = ["build_router"]
