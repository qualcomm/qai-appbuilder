# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer ports owned by ``apps.api`` (no bounded context).

These ports describe collaborators that the API process needs but that
do not belong to any single bounded context. They live under ``apps/``
rather than ``src/qai/<context>/application/ports.py`` because the
"system" surface (health, build-info, edition, reboot) is **not** a
domain — it is a process-level capability of the API server itself.

S3 PR-030: introduces :class:`RebootSignalPort` (used by
``POST /api/system/reboot``). PR-040 will replace the in-memory fake
in :mod:`apps.api.di` with a real implementation that signals the
supervisor (`SIGTERM` + ``exit 75``).
"""

from __future__ import annotations

from typing import Protocol


class RebootSignalPort(Protocol):
    """Signals the supervisor to restart the API process.

    Implementations MUST be idempotent: calling :meth:`signal_reboot`
    twice in quick succession should not raise. They SHOULD return
    promptly (the route responds 202 immediately and only later does
    the supervisor act); blocking I/O belongs in the implementation,
    not the route handler.
    """

    async def signal_reboot(self, *, reason: str) -> None: ...


__all__ = ["RebootSignalPort"]
