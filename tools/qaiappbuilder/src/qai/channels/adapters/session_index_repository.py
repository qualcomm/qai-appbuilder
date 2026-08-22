# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`SessionIndexRepositoryPort` (PR-047).

Schema reference: ``qai-db-schema.md`` §5.3 (channels_session_index).
Replaces the legacy module-level ``_user_cc_sessions: dict`` global with
a composite-keyed table so two independent WeChat instances under the
same ``wxid`` no longer collide.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.channels.domain import (
    ChannelInstanceId,
    ChannelUserId,
    SessionIndexEntry,
    SessionIndexEntryNotFoundError,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteSessionIndexRepository"]


_UPSERT_SQL = (
    "INSERT INTO channels_session_index "
    "(instance_id, channel_user_id, internal_user_id, "
    "coding_session_id, updated_at) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(instance_id, channel_user_id) DO UPDATE SET "
    "internal_user_id=excluded.internal_user_id, "
    "coding_session_id=excluded.coding_session_id, "
    "updated_at=excluded.updated_at"
)


_SELECT_COLS = (
    "instance_id, channel_user_id, internal_user_id, "
    "coding_session_id, updated_at"
)


class SqliteSessionIndexRepository:
    """aiosqlite implementation of :class:`SessionIndexRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def upsert(self, entry: SessionIndexEntry) -> None:
        params = (
            entry.instance_id.value,
            entry.channel_user_id.value,
            entry.internal_user_id,
            entry.coding_session_id,
            entry.updated_at.isoformat(),
        )
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(_UPSERT_SQL, params)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.session_index.upsert_failed",
                f"failed to upsert session entry "
                f"{entry.instance_id.value!r}/"
                f"{entry.channel_user_id.value!r}: {exc}",
                operation="channels.session_index.upsert",
                cause=exc,
            ) from exc

    async def find(
        self,
        instance_id: ChannelInstanceId,
        channel_user_id: ChannelUserId,
    ) -> SessionIndexEntry | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SELECT_COLS} FROM channels_session_index "
                    "WHERE instance_id = ? AND channel_user_id = ?",
                    (instance_id.value, channel_user_id.value),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.session_index.find_failed",
                f"failed to find session entry: {exc}",
                operation="channels.session_index.find",
                cause=exc,
            ) from exc
        return None if row is None else self._row_to_entry(row)

    async def list_for_instance(
        self, instance_id: ChannelInstanceId
    ) -> tuple[SessionIndexEntry, ...]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SELECT_COLS} FROM channels_session_index "
                    "WHERE instance_id = ? ORDER BY updated_at ASC",
                    (instance_id.value,),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.session_index.list_failed",
                f"failed to list session entries: {exc}",
                operation="channels.session_index.list_for_instance",
                cause=exc,
            ) from exc
        return tuple(self._row_to_entry(r) for r in rows)

    async def delete(
        self,
        instance_id: ChannelInstanceId,
        channel_user_id: ChannelUserId,
    ) -> None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM channels_session_index "
                    "WHERE instance_id = ? AND channel_user_id = ?",
                    (instance_id.value, channel_user_id.value),
                )
                deleted = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.session_index.delete_failed",
                f"failed to delete session entry: {exc}",
                operation="channels.session_index.delete",
                cause=exc,
            ) from exc
        if not deleted:
            raise SessionIndexEntryNotFoundError(channel_user_id.value)

    @staticmethod
    def _row_to_entry(row: tuple[object, ...]) -> SessionIndexEntry:
        return SessionIndexEntry(
            instance_id=ChannelInstanceId(value=str(row[0])),
            channel_user_id=ChannelUserId(value=str(row[1])),
            internal_user_id=(
                str(row[2]) if row[2] is not None else None
            ),
            coding_session_id=(
                str(row[3]) if row[3] is not None else None
            ),
            updated_at=datetime.fromisoformat(str(row[4])),
        )
