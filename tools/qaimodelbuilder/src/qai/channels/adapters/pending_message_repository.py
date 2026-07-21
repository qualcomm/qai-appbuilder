# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`PendingMessageStorePort` (PR-097 / S9 §6 R-20).

Persists Layer-3 pending channel messages so a server restart does not
silently drop CC results the user never saw — restoring parity with the
legacy ``backend/channels/wechat/channel.py`` ``_pending_cc_results``
map (S9 audit §6 R-20).

Schema reference: migration
``src/qai/platform/persistence/migrations_sql/010_channel_pending_message.sql``
(table ``channel_pending_message``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError
from qai.platform.time import ensure_aware_utc

from qai.channels.application.ports import PendingMessageStorePort
from qai.channels.domain import ChannelInstanceId, ChannelUserId

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqlitePendingMessageRepository"]


_INSERT_SQL = (
    "INSERT INTO channel_pending_message "
    "(instance_id, user_id, message, created_at_iso, expires_at_iso) "
    "VALUES (?, ?, ?, ?, ?)"
)

_SELECT_LIVE_SQL = (
    "SELECT id, message FROM channel_pending_message "
    "WHERE instance_id = ? AND user_id = ? AND expires_at_iso > ? "
    "ORDER BY id ASC"
)

_SELECT_ALL_FOR_KEY_SQL = (
    "SELECT id FROM channel_pending_message "
    "WHERE instance_id = ? AND user_id = ?"
)

_DELETE_BY_IDS_SQL_TMPL = (
    "DELETE FROM channel_pending_message WHERE id IN ({placeholders})"
)

_HAS_PENDING_SQL = (
    "SELECT 1 FROM channel_pending_message "
    "WHERE instance_id = ? AND user_id = ? AND expires_at_iso > ? "
    "LIMIT 1"
)


class SqlitePendingMessageRepository(PendingMessageStorePort):
    """aiosqlite implementation of :class:`PendingMessageStorePort`.

    Restores parity with the legacy ``_pending_cc_results`` map by
    persisting pending Layer-3 messages so a server restart no longer
    drops CC results the user never saw (PR-097 / S9 §6 R-20).

    The adapter:

    * **Persists** every push as an autoincrement-keyed row so FIFO
      order is stable across restarts.
    * **Filters** expired rows on every read; ``pop_all`` returns only
      live messages and removes them in the same transaction.
    * **Tolerates** schema-level expiry: a row past its
      ``expires_at_iso`` is invisible to readers but still occupies a
      row until garbage-collected — out-of-band sweeps (or the next
      ``pop_all`` for the same key) reclaim the storage.
    """

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def push(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        message: str,
        expires_at: datetime,
    ) -> None:
        if not message:
            return
        normalised_expiry = ensure_aware_utc(expires_at)
        now_iso = datetime.now(timezone.utc).isoformat()
        params = (
            instance_id.value,
            user_id.value,
            message,
            now_iso,
            normalised_expiry.isoformat(),
        )
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(_INSERT_SQL, params)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.pending_message.push_failed",
                f"failed to push pending message for "
                f"{instance_id.value!r}/{user_id.value!r}: {exc}",
                operation="channels.pending_message.push",
                cause=exc,
            ) from exc

    async def pop_all(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> list[str]:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            async with self._db.connection() as conn:
                try:
                    cur = await conn.execute(
                        _SELECT_LIVE_SQL,
                        (instance_id.value, user_id.value, now_iso),
                    )
                    rows = await cur.fetchall()
                    await cur.close()
                    if not rows:
                        # Drop expired rows opportunistically so the
                        # table does not grow unbounded.
                        await self._purge_for_key(
                            conn, instance_id.value, user_id.value
                        )
                        await conn.commit()
                        return []
                    ids = [int(r[0]) for r in rows]
                    messages = [str(r[1]) for r in rows]
                    placeholders = ",".join("?" * len(ids))
                    delete_sql = _DELETE_BY_IDS_SQL_TMPL.format(
                        placeholders=placeholders
                    )
                    await conn.execute(delete_sql, ids)
                    # Also drop any expired rows for the same key so
                    # the next push starts from a clean slate.
                    await self._purge_for_key(
                        conn, instance_id.value, user_id.value
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.pending_message.pop_all_failed",
                f"failed to pop pending messages for "
                f"{instance_id.value!r}/{user_id.value!r}: {exc}",
                operation="channels.pending_message.pop_all",
                cause=exc,
            ) from exc
        return messages

    async def has_pending(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> bool:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    _HAS_PENDING_SQL,
                    (instance_id.value, user_id.value, now_iso),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.pending_message.has_pending_failed",
                f"failed to check pending messages for "
                f"{instance_id.value!r}/{user_id.value!r}: {exc}",
                operation="channels.pending_message.has_pending",
                cause=exc,
            ) from exc
        return row is not None

    @staticmethod
    async def _purge_for_key(
        conn: object, instance_id: str, user_id: str
    ) -> None:
        """Drop expired rows for ``(instance, user)`` on the given conn.

        Best-effort; failures bubble up so the surrounding transaction
        rolls back together with the read.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        await conn.execute(  # type: ignore[attr-defined]
            "DELETE FROM channel_pending_message "
            "WHERE instance_id = ? AND user_id = ? "
            "AND expires_at_iso <= ?",
            (instance_id, user_id, now_iso),
        )
