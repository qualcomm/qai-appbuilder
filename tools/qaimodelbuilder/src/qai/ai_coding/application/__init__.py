# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application layer of the ai_coding bounded context.

Exposes the public ports and use cases.  Adapters and infrastructure
should depend on this package; the domain layer should NOT.
"""

from __future__ import annotations

from .ports import (
    CodingProviderPort,
    CodingSessionRepositoryPort,
    PermissionDecisionPort,
    SkillRegistryPort,
    ToolBridgePort,
    ToolBridgeResult,
    WorkspaceLockHandle,
    WorkspaceLockPort,
)

__all__ = [
    "CodingProviderPort",
    "CodingSessionRepositoryPort",
    "PermissionDecisionPort",
    "SkillRegistryPort",
    "ToolBridgePort",
    "ToolBridgeResult",
    "WorkspaceLockHandle",
    "WorkspaceLockPort",
]
