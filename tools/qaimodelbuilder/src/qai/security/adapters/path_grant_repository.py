# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`PathGrantRepositoryPort` (PR-040).

Schema reference: ``qai-db-schema.md`` §1.3 (security_path_grant).
Replaces the legacy ``config/persistent_acl.json`` (89.7 KB / ~7800
rows) with an indexed relational store; reads are O(log N) via
``ix_security_path_grant_subject`` and ``ix_security_path_grant_path``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.security.domain.entities import PathGrant
from qai.security.domain.errors import PathGrantConflictError
from qai.security.domain.value_objects import (
    AceMask,
    GrantSource,
    Subject,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqlitePathGrantRepository"]


_INSERT_OR_REPLACE_SQL = (
    "INSERT INTO security_path_grant "
    "(id, subject_kind, subject_identifier, path, mask_bits, "
    "source, created_at, expires_at, scope_kind, scope_key, is_directory, "
    "is_program) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "subject_kind=excluded.subject_kind, "
    "subject_identifier=excluded.subject_identifier, "
    "path=excluded.path, "
    "mask_bits=excluded.mask_bits, "
    "source=excluded.source, "
    "created_at=excluded.created_at, "
    "expires_at=excluded.expires_at, "
    "scope_kind=excluded.scope_kind, "
    "scope_key=excluded.scope_key, "
    "is_directory=excluded.is_directory, "
    "is_program=excluded.is_program"
)

# Column list shared by every SELECT so the row tuple layout is stable.
_GRANT_COLUMNS = (
    "id, subject_kind, subject_identifier, path, "
    "mask_bits, source, created_at, expires_at, scope_kind, scope_key, "
    "is_directory, is_program"
)


class SqlitePathGrantRepository:
    """aiosqlite implementation of :class:`PathGrantRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def get(self, grant_id: str) -> PathGrant | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_GRANT_COLUMNS} "
                    "FROM security_path_grant WHERE id = ?",
                    (grant_id,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.path_grant.get_failed",
                f"failed to load grant {grant_id!r}: {exc}",
                operation="path_grant.get",
                cause=exc,
            ) from exc
        return None if row is None else self._row_to_grant(row)

    async def list_for_subject(
        self, subject: Subject
    ) -> list[PathGrant]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_GRANT_COLUMNS} "
                    "FROM security_path_grant "
                    "WHERE subject_kind = ? AND subject_identifier = ? "
                    "ORDER BY created_at ASC",
                    (subject.kind, subject.identifier),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.path_grant.list_for_subject_failed",
                f"failed to list grants for {subject!r}: {exc}",
                operation="path_grant.list_for_subject",
                cause=exc,
            ) from exc
        return [self._row_to_grant(r) for r in rows]

    async def list_all(self) -> list[PathGrant]:
        """Return every persisted grant (PR-4 — startup whitelist seeding).

        Ordered by ``created_at`` ascending. Used at API startup to seed
        the native FileGuard persistent whitelist from the durable
        (``permanent`` / non-expiring) grants; callers filter by
        :meth:`PathGrant.is_expired` / ``expires_at`` themselves.
        """
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_GRANT_COLUMNS} "
                    "FROM security_path_grant "
                    "ORDER BY created_at ASC"
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.path_grant.list_all_failed",
                f"failed to list all grants: {exc}",
                operation="path_grant.list_all",
                cause=exc,
            ) from exc
        return [self._row_to_grant(r) for r in rows]

    async def list_for_path(self, path: str) -> list[PathGrant]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_GRANT_COLUMNS} "
                    "FROM security_path_grant "
                    "WHERE path = ? "
                    "ORDER BY created_at ASC",
                    (path,),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.path_grant.list_for_path_failed",
                f"failed to list grants for path {path!r}: {exc}",
                operation="path_grant.list_for_path",
                cause=exc,
            ) from exc
        return [self._row_to_grant(r) for r in rows]

    async def save(self, grant: PathGrant) -> None:
        params = (
            grant.grant_id,
            grant.subject.kind,
            grant.subject.identifier,
            grant.path,
            grant.mask.to_bits(),
            grant.source.value,
            grant.created_at.isoformat(),
            grant.expires_at.isoformat() if grant.expires_at else None,
            grant.scope_kind,
            grant.scope_key,
            1 if grant.is_directory else 0,
            1 if grant.is_program else 0,
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
            module = type(exc).__module__
            if module.startswith("sqlite3") or module.startswith("aiosqlite"):
                raise PathGrantConflictError(
                    f"failed to save grant {grant.grant_id!r}: {exc}",
                    details={
                        "grant_id": grant.grant_id,
                        "sqlite_error": str(exc),
                    },
                ) from exc
            raise PersistenceError(
                "security.path_grant.save_failed",
                f"failed to save grant {grant.grant_id!r}: {exc}",
                operation="path_grant.save",
                cause=exc,
            ) from exc

    async def delete(self, grant_id: str) -> None:
        # Per port contract: silent no-op if grant absent.
        try:
            async with self._db.connection() as conn:
                await conn.execute(
                    "DELETE FROM security_path_grant WHERE id = ?",
                    (grant_id,),
                )
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.path_grant.delete_failed",
                f"failed to delete grant {grant_id!r}: {exc}",
                operation="path_grant.delete",
                cause=exc,
            ) from exc

    @staticmethod
    def _row_to_grant(
        row: tuple[object, ...],
    ) -> PathGrant:
        grant_id = str(row[0])
        subject = Subject(kind=str(row[1]), identifier=str(row[2]))
        path = str(row[3])
        mask = AceMask.from_bits(int(row[4]))
        source = GrantSource(str(row[5]))
        created_at = datetime.fromisoformat(str(row[6]))
        expires_at = (
            datetime.fromisoformat(str(row[7])) if row[7] is not None else None
        )
        # Tail-appended scope columns (migration 051). Rows written before
        # the migration read back the column DEFAULTs (permanent / "").
        scope_kind = str(row[8]) if len(row) > 8 and row[8] is not None else "permanent"
        scope_key = str(row[9]) if len(row) > 9 and row[9] is not None else ""
        # Tail-appended directory-grant flag (migration 053). Rows written
        # before the migration read back the DEFAULT 0 (single-file semantics).
        is_directory = bool(row[10]) if len(row) > 10 and row[10] is not None else False
        # Tail-appended program-grant flag (migration 054). Rows written before
        # the migration read back the DEFAULT 0 (exact-string / dir semantics).
        is_program = bool(row[11]) if len(row) > 11 and row[11] is not None else False
        return PathGrant(
            grant_id=grant_id,
            subject=subject,
            path=path,
            mask=mask,
            source=source,
            created_at=created_at,
            expires_at=expires_at,
            scope_kind=scope_kind,
            scope_key=scope_key,
            is_directory=is_directory,
            is_program=is_program,
        )
