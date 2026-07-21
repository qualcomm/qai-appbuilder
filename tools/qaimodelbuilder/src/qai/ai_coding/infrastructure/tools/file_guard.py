# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Default :class:`FileGuardPort` implementations.

* :class:`NoopFileGuard` — pass-through; used by tests and by builds
  where security is handled out-of-band.  This is the default adapter
  wired in ``apps/api/_ai_coding_di.py``; production builds compose a
  ``PolicyCenter``-backed bridge over the same port (see
  ``apps/api/_permission_bridge.py``) so ``qai.ai_coding`` never
  imports the security context directly.
* :class:`AlwaysDenyFileGuard` — ergonomic adapter for tests that
  exercise the deny path without standing up a real PolicyCenter.
"""

from __future__ import annotations

from qai.ai_coding.infrastructure.tools.errors import ToolGuardDenied

__all__ = ["AlwaysDenyFileGuard", "NoopFileGuard"]


class NoopFileGuard:
    """All operations allowed (default; matches FILEGUARD_DISABLED=1)."""

    async def enforce_read(self, *, path: str, caller: str) -> None:
        return None

    async def enforce_write(self, *, path: str, caller: str) -> None:
        return None

    async def enforce_delete(self, *, path: str, caller: str) -> None:
        # SEC-ENHANCE-AUDITUX-1: delete shares the write decision path but
        # is audit-distinguishable at the production adapter. The noop
        # guard has no audit, so it is a pass-through — identical shape to
        # enforce_write.
        return None

    async def enforce_exec(
        self, *, command: str, cwd: str | None, caller: str
    ) -> None:
        return None

    async def enforce_project_access(
        self, *, path: str, operation: str
    ) -> None:
        return None

    # 退化 #10 — per-file read probes (non-raising). Pass-through guard
    # allows everything, so per-file glob/grep filtering never drops a file.
    async def is_read_allowed(self, *, path: str) -> bool:
        return True

    async def is_statically_allowed(self, *, path: str) -> bool:
        return True


class AlwaysDenyFileGuard:
    """All operations denied; convenient for testing the guard path."""

    async def enforce_read(self, *, path: str, caller: str) -> None:
        raise ToolGuardDenied(
            message=f"FileGuard denied read: {path}",
            error_code="ai_coding.tool.read_denied",
        )

    async def enforce_write(self, *, path: str, caller: str) -> None:
        raise ToolGuardDenied(
            message=f"FileGuard denied write: {path}",
            error_code="ai_coding.tool.write_denied",
        )

    async def enforce_delete(self, *, path: str, caller: str) -> None:
        # SEC-ENHANCE-AUDITUX-1: delete shares the write decision path.
        # AlwaysDeny denies everything with the same write error code so
        # tests exercising the deny path see identical behaviour whether
        # they exercise enforce_write or enforce_delete.
        raise ToolGuardDenied(
            message=f"FileGuard denied delete: {path}",
            error_code="ai_coding.tool.write_denied",
        )

    async def enforce_exec(
        self, *, command: str, cwd: str | None, caller: str
    ) -> None:
        raise ToolGuardDenied(
            message=f"FileGuard denied command: {command[:120]}",
            error_code="ai_coding.tool.exec_denied",
        )

    async def enforce_project_access(
        self, *, path: str, operation: str
    ) -> None:
        raise ToolGuardDenied(
            message=f"Project directory access denied: {operation} on {path}",
            error_code="ai_coding.tool.project_access_denied",
        )

    # 退化 #10 — per-file read probes: deny guard reports nothing readable,
    # so per-file glob/grep filtering would drop every match.
    async def is_read_allowed(self, *, path: str) -> bool:
        return False

    async def is_statically_allowed(self, *, path: str) -> bool:
        return False
