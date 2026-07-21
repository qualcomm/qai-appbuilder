# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Default :class:`PermissionDecisionPort` adapter (PR-046).

Replaces the in-memory ``_FakeAiCodingPermissionDecision`` from S3 with
a deterministic policy-driven decision adapter.  Without a configured
allow-list every request defaults to ``PENDING`` so the user must
explicitly approve via the route layer; admins can pre-grant a tool
by adding its name to the allow-list, in which case the adapter
returns ``APPROVED`` immediately and the session bypasses the prompt.

Cross-context bridge
--------------------
The richer security-context-driven decisions (RBAC + smart-approval +
sandbox grants) are wired via ``apps/api/_permission_bridge.py`` which
delegates to the security context's ``RequestPermissionUseCase``.  The
``apps/api/_ai_coding_di.py`` factory chooses whichever decision
adapter is appropriate for the current container; this adapter is the
zero-dependency default that keeps the ai_coding context self-
sufficient for tests and offline tooling.
"""

from __future__ import annotations

from collections.abc import Iterable

from qai.ai_coding.domain import (
    PermissionDecision,
    PermissionRequest,
    Workspace,
)

__all__ = ["AllowListPermissionDecision"]


class AllowListPermissionDecision:
    """Static allow-list :class:`PermissionDecisionPort` adapter.

    Tool names listed on construction are auto-approved; everything
    else is held as ``PENDING`` so the user can decide via the
    ``/api/{cc|oc}/permissions/{id}/decide`` endpoint.
    """

    __slots__ = ("_allow_list",)

    def __init__(self, *, allow_list: Iterable[str] | None = None) -> None:
        self._allow_list: frozenset[str] = frozenset(allow_list or ())

    @property
    def allow_list(self) -> frozenset[str]:
        return self._allow_list

    async def evaluate(
        self,
        *,
        request: PermissionRequest,
        workspace: Workspace,
    ) -> PermissionDecision:
        if request.tool_name.value in self._allow_list:
            return PermissionDecision.APPROVED
        return PermissionDecision.PENDING
