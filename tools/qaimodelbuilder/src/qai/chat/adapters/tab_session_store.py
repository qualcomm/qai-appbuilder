# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`TabSessionStorePort` (PR-043).

Schema reference: ``qai-db-schema.md`` §2.3 (chat_conversation_tab) +
``src/qai/platform/persistence/migrations_sql/002_create_chat_schema.sql``
lines 76-91. Single-row aggregate -- INSERT OR REPLACE on save, DELETE
on delete. ``list_active`` returns every tab whose status is NOT
``closed`` (the partial index ``ix_chat_conversation_tab_streaming``
documents the hot path for "currently-streaming tab" lookups, which
this adapter does not need at the port boundary).

PR-043 retires the final remaining ``_FakeTabSessionStore`` from
``apps/api/_chat_di.py``; the lock-in test
``test_pr_043_fake_retirement.py`` (and the updated PR-042 test) assert
zero ``class _Fake<Port>`` matches in that file after this PR.

Design notes:

* The ``chat_conversation_tab`` table has an FK to ``chat_conversation``
  with ``ON DELETE CASCADE`` -- deleting a conversation also removes
  its tabs without any explicit work here.
* The adapter does NOT enforce the "at most one streaming tab per
  conversation" invariant: that is the application layer's job (see
  :class:`StreamAbortRegistryPort`); the adapter is a pure store.
* ``find`` returns ``None`` for missing rows; ``get`` raises
  :class:`TabNotFoundError`; ``delete`` raises if the row was missing.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from qai.chat.domain.errors import TabNotFoundError
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.tab import ConversationTab, TabStatus
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = ["SqliteTabSessionStore"]


_COLUMNS = "id, conversation_id, status, created_at, last_active_at"


class SqliteTabSessionStore:
    """aiosqlite implementation of :class:`TabSessionStorePort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------
    async def save(self, tab: ConversationTab) -> None:
        """Insert or upsert ``tab`` keyed by id."""
        params = (
            tab.id.value,
            tab.conversation_id.value,
            tab.status.value,
            tab.created_at.isoformat(),
            tab.last_active_at.isoformat(),
        )
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(
                        "INSERT INTO chat_conversation_tab "
                        "(id, conversation_id, status, "
                        " created_at, last_active_at) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        " conversation_id=excluded.conversation_id, "
                        " status=excluded.status, "
                        " last_active_at=excluded.last_active_at",
                        params,
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.tab.save_failed",
                f"failed to save tab {tab.id.value!r}: {exc}",
                operation="tab.save",
                cause=exc,
            ) from exc

    async def delete(self, tab_id: TabId) -> None:
        """Remove the tab row; raise :class:`TabNotFoundError` if missing."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_conversation_tab WHERE id = ?",
                    (tab_id.value,),
                )
                rows_affected = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.tab.delete_failed",
                f"failed to delete tab {tab_id.value!r}: {exc}",
                operation="tab.delete",
                cause=exc,
            ) from exc
        if rows_affected == 0:
            raise TabNotFoundError(tab_id.value)

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------
    async def find(self, tab_id: TabId) -> ConversationTab | None:
        """Return the tab or ``None``."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_conversation_tab "
                    "WHERE id = ?",
                    (tab_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.tab.find_failed",
                f"failed to load tab {tab_id.value!r}: {exc}",
                operation="tab.find",
                cause=exc,
            ) from exc
        if row is None:
            return None
        return self._row_to_tab(row)

    async def get(self, tab_id: TabId) -> ConversationTab:
        """Return the tab; raise :class:`TabNotFoundError` if missing."""
        existing = await self.find(tab_id)
        if existing is None:
            raise TabNotFoundError(tab_id.value)
        return existing

    async def list_active(self) -> tuple[ConversationTab, ...]:
        """Return every tab whose status is NOT ``closed``.

        Ordered by ``last_active_at`` DESC so the most recently used
        tab comes first -- the front-end uses this to restore tab
        focus after a reload.
        """
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_conversation_tab "
                    "WHERE status != ? "
                    "ORDER BY last_active_at DESC",
                    (TabStatus.CLOSED.value,),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.tab.list_active_failed",
                f"failed to list active tabs: {exc}",
                operation="tab.list_active",
                cause=exc,
            ) from exc
        return tuple(self._row_to_tab(r) for r in rows)

    # ------------------------------------------------------------------
    # Row -> domain
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_tab(row: tuple[object, ...]) -> ConversationTab:
        return ConversationTab(
            id=TabId.of(str(row[0])),
            conversation_id=ConversationId.of(str(row[1])),
            status=TabStatus(str(row[2])),
            created_at=datetime.fromisoformat(str(row[3])),
            last_active_at=datetime.fromisoformat(str(row[4])),
        )
