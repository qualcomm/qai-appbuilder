# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for the security bounded context.

Each use case encapsulates a single business workflow and is wired with
its dependencies via constructor injection (no globals, no service
locators). Use cases depend on:

* the security :mod:`qai.security.domain` layer;
* the abstract :mod:`qai.security.application.ports`;
* shared :mod:`qai.platform` primitives (``Clock``, ``IdGenerator``, …).

Use cases MUST NOT import from ``qai.security.adapters`` or
``qai.security.infrastructure`` (enforced by import-linter contract
``layered-security``).
"""

from __future__ import annotations

from .approve_permission import ApprovePermissionUseCase
from .cancel_permission_request import CancelPermissionRequestUseCase
from .check_permission import CheckPermissionResult, CheckPermissionUseCase
from .create_path_grant import CreatePathGrantUseCase
from .reject_permission import RejectPermissionUseCase
from .request_permission import RequestPermissionUseCase
from .revoke_path_grant import RevokePathGrantUseCase
from .skill_capability import (
    RegisterSkillCapabilityResult,
    RegisterSkillCapabilityUseCase,
    UnregisterSkillCapabilityUseCase,
)
from .update_policy import UpdatePolicyResult, UpdatePolicyUseCase

__all__ = [
    "ApprovePermissionUseCase",
    "CancelPermissionRequestUseCase",
    "CheckPermissionResult",
    "CheckPermissionUseCase",
    "CreatePathGrantUseCase",
    "RejectPermissionUseCase",
    "RegisterSkillCapabilityResult",
    "RegisterSkillCapabilityUseCase",
    "RequestPermissionUseCase",
    "RevokePathGrantUseCase",
    "UnregisterSkillCapabilityUseCase",
    "UpdatePolicyResult",
    "UpdatePolicyUseCase",
]
