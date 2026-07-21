# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite / settings-driven adapters for the security bounded context.

PR-040 swaps each in-memory ``_Fake<Port>`` from ``apps/api/_security_di.py``
for a real adapter wired against the ``security_*`` tables defined in
``qai-db-schema.md`` §1 plus ``SecuritySettings``. Field names on
:class:`apps.api._security_di.SecurityServices` are part of the public
route contract and are NOT changed here.
"""

from __future__ import annotations

from .ask_rate_limiter import InMemoryAskRateLimiter
from .audit_hook import AuditHookAdapter
from .auto_approve_adapter import RuntimeStateAutoApproveAdapter
from .audit_query import SqliteAuditQuery
from .audit_sink import SqliteAuditSink
from .channel_policy_repository import SqliteChannelPolicyRepository
from .native_file_guard import (
    DisabledNativeFileGuard,
    NativeFileGuard,
    resolve_dll_path,
)
from .path_grant_repository import SqlitePathGrantRepository
from .path_normalizer import normalize_path
from .path_pattern import normalised_match
from .pending_permission_store import (
    NullPermissionPendingStore,
    SqlitePendingPermissionStore,
)
from .permission_broadcast import EventBusPermissionBroadcast
from .permission_request_repository import SqlitePermissionRequestRepository
from .policy_decision_cache import PolicyDecisionCache
from .policy_hot_reload import PolicyHotReloadWatcher
from .policy_repository import SqlitePolicyRepository
from .skill_capability_registry import InMemorySkillCapabilityRegistry
from .smart_approval import SettingsSmartApprovalAdapter
from .smart_approval_llm import SmartApprovalLLMAdapter

# Phase 3 (2026-07-01) — Windows AppContainer/LPAC sandbox execution chain
# removed (``SandboxedProcessRunner`` / ``SandboxPolicyBuilder`` /
# ``sandbox_cmd_rewriter`` adapters + ``infrastructure/daemon`` + the
# ``launcher_resolver`` infrastructure module are gone). The de-sandbox
# refactor (2026-07-04) then removed the remaining orphaned security-side
# sandbox execution framework (``sandbox_run_context`` /
# ``sandbox_config_holder`` / ``sandbox_config_factory`` / the
# execute-sandboxed use cases / ``sandbox_routing`` / ``sandbox_state_machine``),
# since the live FileGuard exec path never consumed them.

__all__ = [
    "AuditHookAdapter",
    "DisabledNativeFileGuard",
    "EventBusPermissionBroadcast",
    "InMemoryAskRateLimiter",
    "InMemorySkillCapabilityRegistry",
    "NativeFileGuard",
    "NullPermissionPendingStore",
    "PolicyDecisionCache",
    "PolicyHotReloadWatcher",
    "RuntimeStateAutoApproveAdapter",
    "SettingsSmartApprovalAdapter",
    "SmartApprovalLLMAdapter",
    "SqliteAuditQuery",
    "SqliteAuditSink",
    "SqliteChannelPolicyRepository",
    "SqlitePathGrantRepository",
    "SqlitePendingPermissionStore",
    "SqlitePermissionRequestRepository",
    "SqlitePolicyRepository",
    "normalize_path",
    "normalised_match",
    "resolve_dll_path",
]
