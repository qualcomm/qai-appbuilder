# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Phase 2 (2026-07-06) — durable pending-permission store adapters.

Implements :class:`qai.security.application.ports.PermissionPendingStorePort`.

Two concrete adapters ship here:

* :class:`SqlitePendingPermissionStore` — the real aiosqlite-backed adapter
  writing to ``security_pending_permission`` (migration 052). Wired by the DI
  layer (:mod:`apps.api._security_di`) when
  ``SecuritySettings.permission_pending_persist`` is True (default).
* :class:`NullPermissionPendingStore` — a zero-side-effect no-op used when
  ``permission_pending_persist`` is False (test / in-memory deployments) or
  when the ``security_pending_permission`` table is missing (fresh DB before
  migration 052 has run — degrades to no-op instead of crashing).

Design constraints (per plan §2.3 and AGENTS.md §5 State-Truth-First):

* **Never fatal** — a persistence failure MUST NOT abort a fresh ASK or the
  ``resolve`` path. The in-memory :class:`PermissionWaitRegistry` stays
  authoritative for the live process; this table is an OPERATIONAL mirror
  for cross-restart rehydrate + subprocess-gone cleanup + cancel-by-pid.
* **Idempotent writes** — ``insert_pending`` uses INSERT OR IGNORE (a retried
  native callback for the same request_id never spawns a duplicate row);
  ``mark_resolved`` UPDATEs only rows where ``resolved_at IS NULL`` so a
  second resolve for the same id is a silent no-op.
* **Best-effort reads** — ``list_unresolved`` / ``list_by_pid`` / ``find_dedupe``
  swallow any error and return the empty / None result (fail-safe: an unknown
  DB state widens NOTHING).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = [
    "NullPermissionPendingStore",
    "SqlitePendingPermissionStore",
]

_log = get_logger(__name__)


class SqlitePendingPermissionStore:
    """aiosqlite implementation of :class:`PermissionPendingStorePort`.

    Backing table: ``security_pending_permission`` (migration 052). All
    writes are UPSERT / conditional-UPDATE so retries are idempotent.
    """

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def insert_pending(
        self,
        *,
        request_id: str,
        pid: int,
        process_path: str,
        command_line: str,
        path: str,
        event: int,
        boot_id: str,
        created_at: datetime,
        actor_parent_pid: int | None = None,
    ) -> None:
        """Insert a fresh pending row (idempotent on ``request_id``)."""
        params = (
            request_id,
            int(pid),
            process_path or "",
            command_line or "",
            path,
            int(event),
            boot_id or "",
            created_at.isoformat(),
            None if actor_parent_pid in (None, 0) else int(actor_parent_pid),
        )
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(
                        "INSERT OR IGNORE INTO security_pending_permission "
                        "(request_id, pid, process_path, command_line, path, "
                        " event, boot_id, created_at, actor_parent_pid) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        params,
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception:  # noqa: BLE001 — persistence must never fail an ASK
            _log.warning(
                "security.pending_permission.insert_failed",
                request_id=request_id,
                exc_info=True,
            )

    async def mark_resolved(
        self,
        *,
        request_id: str,
        resolved_at: datetime,
        resolution: str,
    ) -> None:
        """Mark ``request_id`` resolved (idempotent)."""
        resolution_n = (resolution or "").strip().lower()
        if resolution_n not in {
            "allow",
            "deny",
            "user_cancelled",
            "subprocess_gone",
            "shutdown",
        }:
            resolution_n = "deny"
        try:
            async with self._db.connection() as conn:
                try:
                    # Only update the FIRST resolution — a second call after
                    # the row already has resolved_at is a silent no-op.
                    await conn.execute(
                        "UPDATE security_pending_permission "
                        "SET resolved_at = ?, resolution = ? "
                        "WHERE request_id = ? AND resolved_at IS NULL",
                        (
                            resolved_at.isoformat(),
                            resolution_n,
                            request_id,
                        ),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception:  # noqa: BLE001
            _log.warning(
                "security.pending_permission.resolve_failed",
                request_id=request_id,
                exc_info=True,
            )

    async def list_unresolved(self) -> list[dict[str, Any]]:
        """Return every unresolved row (oldest first)."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT request_id, pid, process_path, command_line, "
                    "path, event, boot_id, created_at, actor_parent_pid "
                    "FROM security_pending_permission "
                    "WHERE resolved_at IS NULL "
                    "ORDER BY created_at ASC"
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception:  # noqa: BLE001 — table missing / read error → empty
            return []
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                created_at = datetime.fromisoformat(str(row[7]))
            except Exception:  # noqa: BLE001 — malformed date → skip row
                continue
            result.append(
                {
                    "request_id": str(row[0]),
                    "pid": int(row[1]),
                    "process_path": str(row[2] or ""),
                    "command_line": str(row[3] or ""),
                    "path": str(row[4]),
                    "event": int(row[5]),
                    "boot_id": str(row[6] or ""),
                    "created_at": created_at,
                    "actor_parent_pid": (
                        int(row[8]) if row[8] is not None else None
                    ),
                }
            )
        return result

    async def list_by_pid(self, pid: int) -> list[str]:
        """Return unresolved ``request_id``s for ``pid``."""
        if not pid or int(pid) <= 0:
            return []
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT request_id FROM security_pending_permission "
                    "WHERE pid = ? AND resolved_at IS NULL "
                    "ORDER BY created_at ASC",
                    (int(pid),),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception:  # noqa: BLE001
            return []
        return [str(r[0]) for r in rows]

    async def find_dedupe(
        self, *, pid: int, path: str, event: int
    ) -> str | None:
        """Best-effort cross-restart dedupe lookup."""
        if not pid or int(pid) <= 0 or not path:
            return None
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT request_id FROM security_pending_permission "
                    "WHERE pid = ? AND path = ? AND event = ? "
                    "AND resolved_at IS NULL "
                    "ORDER BY created_at ASC LIMIT 1",
                    (int(pid), str(path), int(event)),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception:  # noqa: BLE001
            return None
        return None if row is None else str(row[0])

    async def resolve_orphaned_boots(self, current_boot_id: str) -> int:
        """Resolve stale-boot unresolved rows as ``shutdown`` (P-09).

        A single UPDATE marks every unresolved row whose ``boot_id`` is not
        the current boot as ``shutdown`` (their previous-process DLL pipe is
        dead and can never be woken). Rows for the current boot are left
        untouched. Returns the number of rows resolved; any error returns 0
        without raising (never aborts startup).
        """
        resolved_at = datetime.now().isoformat()
        try:
            async with self._db.connection() as conn:
                try:
                    cur = await conn.execute(
                        "UPDATE security_pending_permission "
                        "SET resolved_at = ?, resolution = 'shutdown' "
                        "WHERE resolved_at IS NULL AND boot_id != ?",
                        (resolved_at, current_boot_id or ""),
                    )
                    count = int(cur.rowcount or 0)
                    await cur.close()
                    await conn.commit()
                    return count if count > 0 else 0
                except Exception:
                    await conn.rollback()
                    raise
        except Exception:  # noqa: BLE001 — best-effort; never abort startup
            _log.warning(
                "security.pending_permission.resolve_orphaned_failed",
                exc_info=True,
            )
            return 0


class NullPermissionPendingStore:
    """Zero-side-effect no-op :class:`PermissionPendingStorePort`.

    Wired when ``SecuritySettings.permission_pending_persist`` is False
    (test / in-memory deployments). Every method is a silent no-op
    returning the "nothing persisted" value. The in-memory
    :class:`PermissionWaitRegistry` remains fully functional — only the
    durable mirror is disabled, so :class:`PendingCleanupService` still
    scans and resolves subprocess-gone entries via the registry's own
    ``list_pending()`` (see ``pending_cleanup.py``).
    """

    async def insert_pending(
        self,
        *,
        request_id: str,
        pid: int,
        process_path: str,
        command_line: str,
        path: str,
        event: int,
        boot_id: str,
        created_at: datetime,
        actor_parent_pid: int | None = None,
    ) -> None:
        return None

    async def mark_resolved(
        self,
        *,
        request_id: str,
        resolved_at: datetime,
        resolution: str,
    ) -> None:
        return None

    async def list_unresolved(self) -> list[dict[str, Any]]:
        return []

    async def list_by_pid(self, pid: int) -> list[str]:
        return []

    async def find_dedupe(
        self, *, pid: int, path: str, event: int
    ) -> str | None:
        return None

    async def resolve_orphaned_boots(self, current_boot_id: str) -> int:
        return 0
