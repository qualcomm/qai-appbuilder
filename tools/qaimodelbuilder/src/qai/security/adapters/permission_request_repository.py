# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`PermissionRequestRepositoryPort` (PR-040).

Schema reference: ``qai-db-schema.md`` §1.5 (security_permission_request).
The CHECK constraint set already validates state ∈ {pending, approved,
rejected, expired, cancelled} and the requested-mask range; the
"resolved_at NULL iff state == pending" cross-column rule is enforced by
the domain :class:`PermissionRequest.__post_init__` (schema doc §9.11).

The ``list_pending`` implementation leverages the partial index
``ix_security_permission_request_pending`` for an O(log N) read in the
common case where most requests have been resolved.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.security.domain.entities import PermissionRequest
from qai.security.domain.value_objects import (
    AceMask,
    RequestId,
    RequestState,
    Resource,
    Subject,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqlitePermissionRequestRepository"]


_SELECT_COLUMNS = (
    "id, subject_kind, subject_identifier, "
    "resource_kind, resource_identifier, requested_mask_bits, "
    "state, created_at, resolved_at, resolution_reason"
)


_INSERT_OR_REPLACE_SQL = (
    "INSERT INTO security_permission_request "
    "(id, subject_kind, subject_identifier, resource_kind, "
    "resource_identifier, requested_mask_bits, state, created_at, "
    "resolved_at, resolution_reason) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "subject_kind=excluded.subject_kind, "
    "subject_identifier=excluded.subject_identifier, "
    "resource_kind=excluded.resource_kind, "
    "resource_identifier=excluded.resource_identifier, "
    "requested_mask_bits=excluded.requested_mask_bits, "
    "state=excluded.state, "
    "created_at=excluded.created_at, "
    "resolved_at=excluded.resolved_at, "
    "resolution_reason=excluded.resolution_reason"
)


class SqlitePermissionRequestRepository:
    """aiosqlite implementation of :class:`PermissionRequestRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def get(self, request_id: RequestId) -> PermissionRequest | None:
        # The fake accepted bare strings; the real adapter strictly takes
        # a ``RequestId`` value object so the type contract is enforced
        # at the boundary. Callers that hold a string must wrap it
        # explicitly via ``RequestId(value=...)``.
        rid = request_id.value
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SELECT_COLUMNS} "
                    "FROM security_permission_request WHERE id = ?",
                    (rid,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.permission_request.get_failed",
                f"failed to load request {rid!r}: {exc}",
                operation="permission_request.get",
                cause=exc,
            ) from exc
        return None if row is None else self._row_to_request(row)

    async def list_pending(self) -> list[PermissionRequest]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SELECT_COLUMNS} "
                    "FROM security_permission_request "
                    "WHERE state = 'pending' "
                    "ORDER BY created_at DESC"
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.permission_request.list_pending_failed",
                f"failed to list pending requests: {exc}",
                operation="permission_request.list_pending",
                cause=exc,
            ) from exc
        return [self._row_to_request(r) for r in rows]

    async def save(self, request: PermissionRequest) -> None:
        params = (
            request.request_id.value,
            request.subject.kind,
            request.subject.identifier,
            request.resource.kind,
            request.resource.identifier,
            request.requested_mask.to_bits(),
            request.state.value,
            request.created_at.isoformat(),
            (
                request.resolved_at.isoformat()
                if request.resolved_at is not None
                else None
            ),
            request.resolution_reason,
        )
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(_INSERT_OR_REPLACE_SQL, params)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.permission_request.save_failed",
                f"failed to save request {request.request_id.value!r}: {exc}",
                operation="permission_request.save",
                cause=exc,
            ) from exc

    @staticmethod
    def _row_to_request(
        row: tuple[object, ...],
    ) -> PermissionRequest:
        request_id = RequestId(value=str(row[0]))
        subject = Subject(kind=str(row[1]), identifier=str(row[2]))
        resource = Resource(kind=str(row[3]), identifier=str(row[4]))
        requested_mask = AceMask.from_bits(int(row[5]))
        state = RequestState(str(row[6]))
        created_at = datetime.fromisoformat(str(row[7]))
        resolved_at = (
            datetime.fromisoformat(str(row[8])) if row[8] is not None else None
        )
        resolution_reason = str(row[9] or "")
        return PermissionRequest(
            request_id=request_id,
            subject=subject,
            resource=resource,
            requested_mask=requested_mask,
            state=state,
            created_at=created_at,
            resolved_at=resolved_at,
            resolution_reason=resolution_reason,
        )
