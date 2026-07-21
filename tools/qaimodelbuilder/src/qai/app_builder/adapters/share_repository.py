# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`ShareRepositoryPort` (PR-045).

Schema reference: ``qai-db-schema.md`` §3.4 (``app_builder_share``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.app_builder.domain.errors import ShareNotFoundError
from qai.app_builder.domain.share import Share, ShareToken
from qai.app_builder.domain.value_objects import RunId

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteShareRepository"]


_INSERT_OR_REPLACE_SQL = (
    "INSERT INTO app_builder_share "
    "(id, run_id, created_at, expires_at, revoked) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "run_id=excluded.run_id, "
    "created_at=excluded.created_at, "
    "expires_at=excluded.expires_at, "
    "revoked=excluded.revoked"
)


class SqliteShareRepository:
    """aiosqlite implementation of :class:`ShareRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def save(self, share: Share) -> None:
        params = (
            share.token.value,
            share.run_id.value,
            share.created_at.isoformat(),
            share.expires_at.isoformat() if share.expires_at else None,
            1 if share.revoked else 0,
        )
        try:
            async with self._db.connection() as conn:
                await conn.execute(_INSERT_OR_REPLACE_SQL, params)
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.share.save_failed",
                f"failed to save share {share.token!r}: {exc}",
                operation="share.save",
                cause=exc,
            ) from exc

    async def get_by_token(self, token: ShareToken) -> Share:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, run_id, created_at, expires_at, revoked "
                    "FROM app_builder_share WHERE id = ?",
                    (token.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.share.get_failed",
                f"failed to load share {token!r}: {exc}",
                operation="share.get_by_token",
                cause=exc,
            ) from exc
        if row is None:
            raise ShareNotFoundError(
                message=f"share token {token} not found",
                details={"token": str(token)},
            )
        return self._row_to_share(row)

    async def list_for_run(self, run_id: RunId) -> tuple[Share, ...]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, run_id, created_at, expires_at, revoked "
                    "FROM app_builder_share "
                    "WHERE run_id = ? "
                    "ORDER BY created_at ASC",
                    (run_id.value,),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.share.list_for_run_failed",
                f"failed to list shares for run {run_id!r}: {exc}",
                operation="share.list_for_run",
                cause=exc,
            ) from exc
        return tuple(self._row_to_share(r) for r in rows)

    @staticmethod
    def _row_to_share(row: tuple[object, ...]) -> Share:
        return Share(
            token=ShareToken(value=str(row[0])),
            run_id=RunId(value=str(row[1])),
            created_at=datetime.fromisoformat(str(row[2])),
            expires_at=(
                datetime.fromisoformat(str(row[3]))
                if row[3] is not None
                else None
            ),
            revoked=bool(int(row[4] or 0)),
        )
