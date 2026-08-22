# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`ConversationRepositoryPort` (PR-042).

Schema reference: ``qai-db-schema.md`` §2.1 (chat_conversation) + §2.2
(chat_message). The conversation aggregate is multi-row -- a header row
in ``chat_conversation`` plus N child rows in ``chat_message`` -- so
:meth:`save` is a single ``BEGIN IMMEDIATE`` transaction that

* upserts the header row;
* deletes every existing message row for the conversation;
* inserts the current ``messages`` list.

This DELETE-then-INSERT shape mirrors PR-040's
:class:`SqlitePolicyRepository` and accepts the modest write
amplification because conversations are append-only in the domain layer
(``Conversation.append_message``) so the savings from a smarter diff
would be marginal.

Free-text search (``search``) uses the FTS5 ``chat_message_fts`` virtual
table (migration 016) for message-body matching plus snippet highlighting,
mirroring V1 ``backend/history_store.py:688-714``. The matched message
excerpt is wrapped in ``<mark>...</mark>`` via FTS5 ``snippet(...)`` and the
CJK query is bigram-preprocessed (V1 history_store.py:690-696) so Chinese
substrings still match the ``unicode61`` tokenizer. When the FTS index is
unavailable (older SQLite without FTS5, migration not yet applied, or a
malformed MATCH expression) the adapter falls back to a ``LIKE`` scan over
title + body and synthesises the snippet in Python.

Cursor format for :meth:`fetch_messages_page` is ``"position:<int>"``
where ``<int>`` is the 0-based ``chat_message.position`` of the *first*
row of the next page; ``None`` means "no more pages".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from qai.chat.adapters._fts_query import build_fts_match, make_snippet
from qai.chat.application.ports import (
    ConversationListItem,
    MessagesPage,
)
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.conversation import Conversation, ConversationStatus
from qai.chat.domain.errors import ConversationNotFoundError
from qai.chat.domain.ids import ConversationId, MessageId
from qai.chat.domain.message import Message
from qai.platform.errors import PersistenceError
from qai.platform.persistence import write_transaction

if TYPE_CHECKING:  # pragma: no cover
    from typing import Any

    from qai.platform.persistence import Database


__all__ = ["SqliteConversationRepository"]


_HEADER_COLUMNS = (
    "id, title, status, created_at, updated_at, meta_json, "
    "full_history_tokens, detected_model_json"
)
_MESSAGE_COLUMNS = (
    "id, conversation_id, parent_id, role, content_text, "
    "media_refs_json, tool_calls_json, tool_results_json, "
    "created_at, position, usage_json, model_id, model_provider, "
    "meta_json, sender_id"
)


class SqliteConversationRepository:
    """aiosqlite implementation of :class:`ConversationRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    @property
    def database(self) -> "Database":
        """The engine behind this repository.

        Exposed so a serialised writer can lease ONE connection for a whole
        worker and hand it back through the ``conn`` argument of the write
        methods, instead of paying the connection setup per transaction.
        """
        return self._db

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------
    async def find(
        self,
        conversation_id: ConversationId,
    ) -> Conversation | None:
        """Return the aggregate with all messages, or ``None``."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_HEADER_COLUMNS} FROM chat_conversation "
                    "WHERE id = ?",
                    (conversation_id.value,),
                )
                header = await cur.fetchone()
                await cur.close()
                if header is None:
                    return None
                cur = await conn.execute(
                    f"SELECT {_MESSAGE_COLUMNS} FROM chat_message "
                    "WHERE conversation_id = ? "
                    "ORDER BY position ASC",
                    (conversation_id.value,),
                )
                msg_rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.conversation.find_failed",
                f"failed to load conversation {conversation_id.value!r}: {exc}",
                operation="conversation.find",
                cause=exc,
            ) from exc
        return self._rows_to_conversation(header, msg_rows)

    async def find_latest_by_channel_user(
        self,
        source: str,
        channel_user_id: str,
    ) -> Conversation | None:
        """Return the newest non-archived conversation for a channel user.

        V1 parity: ``history_store.get_latest_wechat_conversation`` /
        ``get_latest_feishu_conversation``
        (``QAIModelBuilder_v0.5_pure/backend/channels/.../history_store.py:363-405``)
        ::

            WHERE is_deleted = 0
              AND json_extract(meta, '$.<kind>_user_id') = ?
            ORDER BY updated_at DESC LIMIT 1

        V2 uses a unified ``meta`` shape ``{"source": ..., "channel_user_id":
        ...}`` (instead of V1's per-kind ``wechat_user_id`` /
        ``feishu_open_id`` keys) and matches both keys via ``json_extract``
        on the already-persisted ``meta_json`` column (same json1 mechanism
        the repo already uses for ``$.request_id`` — :meth:`clear_request_ids`).
        ``is_deleted`` has no V2 column: deletion is a hard ``DELETE`` (see
        :meth:`delete`), and archive is ``status`` — so we exclude
        ``status = 'archived'`` to mirror V1's "skip hidden" intent.

        Loads the FULL aggregate (header + messages) so the channels bridge
        can restore the conversation AND its history (V1 ``_ensure_conv``
        re-hydrates ``_user_histories`` from ``get_messages(conv_id)``).
        Returns ``None`` when no match.
        """
        if not source or not channel_user_id:
            return None
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_HEADER_COLUMNS} FROM chat_conversation "
                    "WHERE status != ? "
                    "AND json_extract(meta_json, '$.source') = ? "
                    "AND json_extract(meta_json, '$.channel_user_id') = ? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (
                        ConversationStatus.ARCHIVED.value,
                        source,
                        channel_user_id,
                    ),
                )
                header = await cur.fetchone()
                await cur.close()
                if header is None:
                    return None
                cur = await conn.execute(
                    f"SELECT {_MESSAGE_COLUMNS} FROM chat_message "
                    "WHERE conversation_id = ? "
                    "ORDER BY position ASC",
                    (str(header[0]),),
                )
                msg_rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.conversation.find_latest_by_channel_user_failed",
                f"failed to find latest conversation for "
                f"{source!r}/{channel_user_id!r}: {exc}",
                operation="conversation.find_latest_by_channel_user",
                cause=exc,
            ) from exc
        return self._rows_to_conversation(header, msg_rows)

    async def find_ids_by_selected_mode(
        self,
        mode_id: str,
    ) -> tuple[ConversationId, ...]:
        """Return ids of conversations selecting ``mode_id`` (decision 7).

        Matches on the persisted ``meta_json`` via ``json_extract`` (same json1
        mechanism the repo already uses for ``$.source`` / ``$.request_id``).
        A header-only query (no message hydration) — the caller only needs the
        ids to count + clear the selection.
        """
        if not mode_id:
            return ()
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id FROM chat_conversation "
                    "WHERE json_extract("
                    "meta_json, '$.discussion.selected_mode_id') = ?",
                    (mode_id,),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.conversation.find_ids_by_selected_mode_failed",
                f"failed to find conversations for mode {mode_id!r}: {exc}",
                operation="conversation.find_ids_by_selected_mode",
                cause=exc,
            ) from exc
        return tuple(ConversationId.of(str(r[0])) for r in rows)

    async def get(self, conversation_id: ConversationId) -> Conversation:
        """Return the aggregate; raise :class:`ConversationNotFoundError`."""
        conv = await self.find(conversation_id)
        if conv is None:
            raise ConversationNotFoundError(conversation_id.value)
        return conv

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        favorite_only: bool = False,
        pinned_only: bool = False,
    ) -> tuple[ConversationListItem, ...]:
        """Return conversations ordered by ``updated_at`` DESC.

        ``favorite_only`` / ``pinned_only`` restrict the result to rows whose
        ``meta_json`` carries a truthy ``favorite`` / ``pinned`` flag (the
        favorites dialog uses ``favorite_only`` so it sees the COMPLETE set of
        favorited conversations, not just the recent ``limit`` window the
        sidebar shows). Both filters are AND-combined when set together. The
        flags live in ``meta_json`` (no dedicated column), so we match via
        ``json_extract`` against the JSON ``true`` literal.
        """
        if limit <= 0:
            return ()
        if offset < 0:
            offset = 0
        where_clauses: list[str] = []
        if favorite_only:
            # ``json_extract`` returns SQLite integer 1 for a JSON ``true``
            # (NOT the JSON literal), so the flag is matched with ``= 1``.
            where_clauses.append(
                "json_extract(c.meta_json, '$.favorite') = 1"
            )
        if pinned_only:
            where_clauses.append(
                "json_extract(c.meta_json, '$.pinned') = 1"
            )
        where_sql = (
            (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        )
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT c.id, c.title, c.status, c.created_at, "
                    "c.updated_at, c.meta_json, "
                    "(SELECT COUNT(*) FROM chat_message m "
                    " WHERE m.conversation_id = c.id) AS msg_count, "
                    "(SELECT COUNT(*) FROM chat_message m "
                    " WHERE m.conversation_id = c.id AND m.role = 'user') "
                    " AS round_count, "
                    "(SELECT COALESCE(SUM(json_array_length("
                    "  m.tool_calls_json)), 0) FROM chat_message m "
                    " WHERE m.conversation_id = c.id "
                    "   AND m.tool_calls_json IS NOT NULL "
                    "   AND m.tool_calls_json != '[]') "
                    " AS tool_call_count, "
                    "(SELECT COUNT(*) FROM chat_subagent_session s "
                    " WHERE s.root_conversation_id = c.id) "
                    " AS subagent_count "
                    "FROM chat_conversation c "
                    f"{where_sql} "
                    "ORDER BY c.updated_at DESC "
                    "LIMIT ? OFFSET ?",
                    (int(limit), int(offset)),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.conversation.list_failed",
                f"failed to list conversations: {exc}",
                operation="conversation.list",
                cause=exc,
            ) from exc
        return tuple(
            ConversationListItem(
                conversation=self._rows_to_conversation(row[:6], ()),
                message_count=int(row[6] or 0),
                round_count=int(row[7] or 0),
                tool_call_count=int(row[8] or 0),
                subagent_count=int(row[9] or 0),
            )
            for row in rows
        )

    async def search(
        self,
        *,
        query: str,
        limit: int = 50,
    ) -> tuple[ConversationListItem, ...]:
        """Full-text search across title + message body, with snippets.

        V1 parity (``backend/history_store.py:688-714``): matches the
        message *body* via the FTS5 ``chat_message_fts`` index and returns
        a ``<mark>``-highlighted snippet of the best-matching message. Title
        matches are unioned in (FTS5 only indexes message bodies, exactly
        like V1). Falls back to a ``LIKE`` scan + Python-side snippet when
        the FTS index is unavailable.
        """
        if limit <= 0:
            return ()
        if not query:
            # Empty query yields the most-recent conversations (mirrors
            # the in-memory fake's behaviour).
            return await self.list(limit=limit, offset=0)

        rows = await self._search_via_fts5(query=query, limit=limit)
        if rows is None:
            rows = await self._search_via_like(query=query, limit=limit)
        return tuple(
            ConversationListItem(
                conversation=self._rows_to_conversation(row[:6], ()),
                message_count=int(row[6] or 0),
                round_count=int(row[7] or 0),
                tool_call_count=int(row[8] or 0),
                snippet=str(row[9] or ""),
            )
            for row in rows
        )

    async def _search_via_fts5(
        self,
        *,
        query: str,
        limit: int,
    ) -> list[tuple[object, ...]] | None:
        """FTS5 body search + ``<mark>`` snippet, unioned with title LIKE.

        Returns ``None`` to signal "FTS unavailable / query rejected" so
        the caller falls back to :meth:`_search_via_like`.

        The result projection is ``(id, title, status, created_at,
        updated_at, msg_count, round_count, tool_call_count, snippet)`` —
        the same 8-column shape :meth:`list` produces, plus the snippet.
        """
        match_expr = build_fts_match(query)
        like = f"%{query}%"
        # Two correlated subqueries:
        #  * body-match arm: JOIN chat_message_fts; snippet() highlights the
        #    matched message body with <mark>...</mark> (V1 params:
        #    column 0, '<mark>', '</mark>', '...', 32 tokens of context).
        #  * title-match arm: plain LIKE on the conversation title, empty
        #    snippet (V1's FTS index only covers message bodies, so a
        #    title-only hit carries no body excerpt — same as V1).
        sql = (
            "SELECT c.id, c.title, c.status, c.created_at, c.updated_at, "
            "c.meta_json, "
            "(SELECT COUNT(*) FROM chat_message m "
            " WHERE m.conversation_id = c.id) AS msg_count, "
            "(SELECT COUNT(*) FROM chat_message m "
            " WHERE m.conversation_id = c.id AND m.role = 'user') "
            " AS round_count, "
            "(SELECT COALESCE(SUM(json_array_length("
            "  m.tool_calls_json)), 0) FROM chat_message m "
            " WHERE m.conversation_id = c.id "
            "   AND m.tool_calls_json IS NOT NULL "
            "   AND m.tool_calls_json != '[]') "
            " AS tool_call_count, "
            "( SELECT snippet(chat_message_fts, 0, '<mark>', '</mark>', "
            "          '...', 32) "
            "  FROM chat_message_fts "
            "  WHERE chat_message_fts.conversation_id = c.id "
            "    AND chat_message_fts MATCH ? "
            "  ORDER BY rank LIMIT 1 ) AS snippet "
            "FROM chat_conversation c "
            "WHERE c.id IN ( "
            "    SELECT conversation_id FROM chat_message_fts "
            "    WHERE chat_message_fts MATCH ? "
            "  ) OR c.title LIKE ? "
            "ORDER BY c.updated_at DESC "
            "LIMIT ?"
        )
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    sql, (match_expr, match_expr, like, int(limit))
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception:  # noqa: BLE001 — fall back to LIKE
            # FTS5 not compiled in, migration 016 not applied, or the MATCH
            # expression was rejected by the FTS5 parser.
            return None
        return [tuple(r) for r in rows]

    async def _search_via_like(
        self,
        *,
        query: str,
        limit: int,
    ) -> list[tuple[object, ...]]:
        """LIKE fallback: title + body match with a Python-side snippet."""
        like = f"%{query}%"
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT DISTINCT c.id, c.title, c.status, c.created_at, "
                    "c.updated_at, c.meta_json, "
                    "(SELECT COUNT(*) FROM chat_message m2 "
                    " WHERE m2.conversation_id = c.id) AS msg_count, "
                    "(SELECT COUNT(*) FROM chat_message m2 "
                    " WHERE m2.conversation_id = c.id AND m2.role = 'user') "
                    " AS round_count, "
                    "(SELECT COALESCE(SUM(json_array_length("
                    "  m2.tool_calls_json)), 0) FROM chat_message m2 "
                    " WHERE m2.conversation_id = c.id "
                    "   AND m2.tool_calls_json IS NOT NULL "
                    "   AND m2.tool_calls_json != '[]') "
                    " AS tool_call_count, "
                    "(SELECT m3.content_text FROM chat_message m3 "
                    " WHERE m3.conversation_id = c.id "
                    "   AND m3.content_text LIKE ? "
                    " ORDER BY m3.position ASC LIMIT 1) AS match_text "
                    "FROM chat_conversation c "
                    "LEFT JOIN chat_message m ON m.conversation_id = c.id "
                    "WHERE c.title LIKE ? OR m.content_text LIKE ? "
                    "ORDER BY c.updated_at DESC "
                    "LIMIT ?",
                    (like, like, like, int(limit)),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.conversation.search_failed",
                f"failed to search conversations: {exc}",
                operation="conversation.search",
                cause=exc,
            ) from exc
        out: list[tuple[object, ...]] = []
        for row in rows:
            match_text = row[9]
            snippet = (
                make_snippet(str(match_text), query)
                if match_text is not None
                else ""
            )
            out.append((*row[:9], snippet))
        return out

    async def fetch_messages_page(
        self,
        *,
        conversation_id: ConversationId,
        cursor: str | None = None,
        limit: int = 50,
    ) -> MessagesPage:
        """Return one page of messages.

        Cursor format: ``"position:<int>"``. ``None`` cursor starts at
        position 0.
        """
        if limit <= 0:
            limit = 50
        start = _parse_cursor(cursor)

        # Verify conversation exists (raise NotFound if not).
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT 1 FROM chat_conversation WHERE id = ?",
                    (conversation_id.value,),
                )
                exists = await cur.fetchone()
                await cur.close()
                if exists is None:
                    raise ConversationNotFoundError(conversation_id.value)
                cur = await conn.execute(
                    f"SELECT {_MESSAGE_COLUMNS} FROM chat_message "
                    "WHERE conversation_id = ? AND position >= ? "
                    "ORDER BY position ASC "
                    "LIMIT ?",
                    (conversation_id.value, start, int(limit)),
                )
                rows = await cur.fetchall()
                await cur.close()
        except ConversationNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.conversation.fetch_messages_failed",
                f"failed to fetch messages: {exc}",
                operation="conversation.fetch_messages_page",
                cause=exc,
            ) from exc

        items = tuple(self._row_to_message(r) for r in rows)
        next_cursor: str | None = None
        if len(items) == limit and items:
            next_cursor = f"position:{rows[-1][9] + 1}"
        return MessagesPage(items=items, next_cursor=next_cursor)

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------
    async def save(self, conversation: Conversation) -> None:
        """Atomically upsert the conversation and replace its messages.

        This writes the conversation HEADER (``title`` / ``status`` /
        ``meta_json``) from the in-memory aggregate.  Use this from the
        dedicated header-owning use cases (create / rename / set-workspace /
        auto-title) that intentionally set the title or meta.

        For turn persistence (streaming appends), prefer
        :meth:`save_messages`, which preserves the DB's authoritative
        ``title`` / ``meta_json`` rather than clobbering them from a
        possibly-stale in-memory copy (State-Truth-First: a concurrent
        manual rename / title_manual flag must survive a turn that loaded
        the conversation before the rename happened).
        """
        await self._save_internal(conversation, header_mode="full")

    async def save_messages(
        self, conversation: Conversation, *, conn: "Any | None" = None
    ) -> None:
        """Persist the conversation's messages, preserving the DB header.

        Same as :meth:`save` for the message rows + ``status`` /
        ``updated_at``, but on an EXISTING row it does NOT overwrite the
        persisted ``title`` or ``meta_json``.  On a brand-new row it still
        writes the aggregate's title/meta (the first turn that creates the
        row legitimately seeds them).

        Rationale (State-Truth-First, A/F fix): streaming loads the
        conversation once at turn start and re-uses that snapshot for every
        save during the (possibly minutes-long) turn.  If the user renames
        the conversation mid-turn, the final turn ``save`` would otherwise
        roll back the user's title + ``title_manual`` flag from the stale
        snapshot.  By not touching the header on conflict, a concurrent
        rename always wins.

        ``conn`` lets a serialised writer supply a connection it holds
        across many transactions — see :meth:`append_message_atomic`.
        """
        await self._save_internal(
            conversation, header_mode="preserve", conn=conn
        )

    async def append_message_atomic(
        self,
        conversation_id: ConversationId,
        message: Message,
        *,
        conn: "Any | None" = None,
    ) -> None:
        """Append ONE message row atomically — see the port docstring.

        Uses ``BEGIN IMMEDIATE`` + ``SELECT COALESCE(MAX(position)+1, 0)``
        inside the same transaction so two concurrent appenders serialise
        cleanly on SQLite's write lock and each land at a distinct
        position (no ``UNIQUE(conversation_id, position)`` violation).

        Also bumps ``chat_conversation.updated_at`` so a subsequent
        :meth:`find` yields a fresh header (State-Truth-First — the
        aggregate's ``updated_at`` reflects the most recent write).

        ``conn`` lets a serialised writer supply a connection it holds
        across many transactions (see
        :meth:`Database.lease_persistent_connection`), which removes the
        per-transaction connection setup — the dominant cost of a small
        write.  ``None`` leases a fresh one, exactly as before.
        """
        now_iso = datetime.now(UTC).isoformat()
        try:
            async with write_transaction(self._db, conn) as tx:
                cur = await tx.execute(
                    "SELECT 1 FROM chat_conversation WHERE id = ?",
                    (conversation_id.value,),
                )
                header_row = await cur.fetchone()
                await cur.close()
                if header_row is None:
                    raise ConversationNotFoundError(conversation_id.value)
                cur = await tx.execute(
                    "SELECT COALESCE(MAX(position) + 1, 0) "
                    "FROM chat_message WHERE conversation_id = ?",
                    (conversation_id.value,),
                )
                pos_row = await cur.fetchone()
                await cur.close()
                next_position = int(pos_row[0]) if pos_row else 0
                await tx.execute(
                    "INSERT INTO chat_message ("
                    "id, conversation_id, parent_id, role, "
                    "content_text, media_refs_json, tool_calls_json, "
                    "tool_results_json, created_at, position, "
                    "usage_json, model_id, model_provider, meta_json, "
                    "sender_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        message.id.value,
                        conversation_id.value,
                        message.parent_id.value if message.parent_id else None,
                        message.role.value,
                        message.content.text,
                        json.dumps(
                            list(message.content.media_refs), ensure_ascii=False,
                        ),
                        json.dumps(list(message.tool_calls), ensure_ascii=False),
                        json.dumps(list(message.tool_results), ensure_ascii=False),
                        message.created_at.isoformat(),
                        next_position,
                        json.dumps(message.usage, ensure_ascii=False)
                        if message.usage
                        else None,
                        message.model_id,
                        message.model_provider,
                        json.dumps(message.meta, ensure_ascii=False)
                        if message.meta
                        else None,
                        message.sender_id,
                    ),
                )
                await tx.execute(
                    "UPDATE chat_conversation SET updated_at = ? WHERE id = ?",
                    (now_iso, conversation_id.value),
                )
                await tx.commit()
        except ConversationNotFoundError:
            raise
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.conversation.append_message_atomic_failed",
                f"failed to append message to {conversation_id.value!r}: {exc}",
                operation="conversation.append_message_atomic",
                cause=exc,
            ) from exc

    async def save_meta(self, conversation: Conversation) -> None:
        """Persist messages + ``meta_json`` (and status), but PRESERVE ``title``.

        For discussion / implementation state write-backs that live in
        ``meta["discussion"]``: they must update ``meta_json`` from the
        aggregate, yet must NOT clobber ``title`` from a possibly-stale
        in-memory copy.  This closes the "auto-title generated on the first
        frame, then overwritten by the discussion state-back ``save`` from the
        turn's stale snapshot" bug: the discussion turn loads the conversation
        once (title still "新对话"), so a full ``save`` at END would roll the
        model-summarised title back.  Writing meta while preserving title lets
        the concurrent auto-title survive (State-Truth-First).
        """
        await self._save_internal(conversation, header_mode="meta")

    async def _save_internal(
        self,
        conversation: Conversation,
        *,
        header_mode: str,
        conn: "Any | None" = None,
    ) -> None:
        header_params = (
            conversation.id.value,
            conversation.title,
            conversation.status.value,
            conversation.created_at.isoformat(),
            conversation.updated_at.isoformat(),
            json.dumps(conversation.meta, ensure_ascii=False)
            if conversation.meta
            else None,
            conversation.full_history_tokens,
            json.dumps(conversation.detected_model, ensure_ascii=False)
            if conversation.detected_model
            else None,
        )
        message_rows: list[tuple[object, ...]] = []
        for position, msg in enumerate(conversation.messages):
            message_rows.append(
                (
                    msg.id.value,
                    conversation.id.value,
                    msg.parent_id.value if msg.parent_id else None,
                    msg.role.value,
                    msg.content.text,
                    json.dumps(list(msg.content.media_refs), ensure_ascii=False),
                    json.dumps(list(msg.tool_calls), ensure_ascii=False),
                    json.dumps(list(msg.tool_results), ensure_ascii=False),
                    msg.created_at.isoformat(),
                    position,
                    json.dumps(msg.usage, ensure_ascii=False) if msg.usage else None,
                    msg.model_id,
                    msg.model_provider,
                    json.dumps(msg.meta, ensure_ascii=False) if msg.meta else None,
                    msg.sender_id,
                )
            )

        # On conflict, three header modes:
        #  * "preserve" (save_messages): bump status/updated_at + token/model
        #    columns only; leave BOTH title and meta_json untouched (a
        #    concurrent manual rename / discussion-config edit survives a stale
        #    turn snapshot).
        #  * "meta" (save_meta): also write meta_json from the aggregate, but
        #    still PRESERVE title — discussion / implementation state-backs must
        #    persist their meta yet never roll back a concurrently-generated
        #    auto-title from the turn's stale snapshot (the "title lost on
        #    restart" bug).
        #  * "full" (save): header-owning saves write title AND meta_json from
        #    the aggregate (create / rename / set-workspace / auto-title).
        # ALL branches write full_history_tokens + detected_model_json (the
        # streaming preserve path uses them; omitting would freeze the column).
        if header_mode == "preserve":
            on_conflict = (
                "ON CONFLICT(id) DO UPDATE SET "
                " status=excluded.status, "
                " updated_at=excluded.updated_at, "
                " full_history_tokens=excluded.full_history_tokens, "
                " detected_model_json=excluded.detected_model_json"
            )
        elif header_mode == "meta":
            on_conflict = (
                "ON CONFLICT(id) DO UPDATE SET "
                " status=excluded.status, "
                " updated_at=excluded.updated_at, "
                " meta_json=excluded.meta_json, "
                " full_history_tokens=excluded.full_history_tokens, "
                " detected_model_json=excluded.detected_model_json"
            )
        else:  # "full"
            on_conflict = (
                "ON CONFLICT(id) DO UPDATE SET "
                " title=excluded.title, "
                " status=excluded.status, "
                " updated_at=excluded.updated_at, "
                " meta_json=excluded.meta_json, "
                " full_history_tokens=excluded.full_history_tokens, "
                " detected_model_json=excluded.detected_model_json"
            )

        try:
            async with write_transaction(self._db, conn) as tx:
                await tx.execute(
                    "INSERT INTO chat_conversation "
                    "(id, title, status, created_at, updated_at, meta_json, "
                    "full_history_tokens, detected_model_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    + on_conflict,
                    header_params,
                )
                # §concurrent-writer refactor 2026-08-07 — see the
                # docstring on the fix rationale.  Two-pass write:
                #
                # 1. Read existing message ids + their DB positions.
                #    Snapshot rows whose id ALREADY exists are UPDATEd
                #    in place, keeping the DB-authoritative position
                #    (never rewriting concurrent writer's positions).
                # 2. Snapshot rows whose id does NOT exist are new
                #    additions from THIS writer.  Their position is
                #    computed as ``current_tail + 1, +2, ...`` at
                #    INSERT time so a concurrent writer's atomic
                #    append (``append_message_atomic``) that landed
                #    between load and save is threaded correctly:
                #    this writer's new rows land AFTER it.
                #
                # This ordering is stable for the common case (one
                # active writer) and correct for the concurrent case
                # (the atomic-append's row is chronologically before
                # this snapshot's new tail — matching wall time).
                conv_id_str = conversation.id.value
                cur = await tx.execute(
                    "SELECT id, position FROM chat_message "
                    "WHERE conversation_id = ?",
                    (conv_id_str,),
                )
                db_id_position: dict[str, int] = {
                    str(row[0]): int(row[1])
                    for row in await cur.fetchall()
                }
                await cur.close()
                if db_id_position:
                    next_position = max(db_id_position.values()) + 1
                else:
                    next_position = 0
                new_rows: list[tuple[object, ...]] = []
                update_rows: list[tuple[object, ...]] = []
                for row in message_rows:
                    msg_id = str(row[0])
                    if msg_id in db_id_position:
                        # Existing DB row — upsert-by-id with the
                        # DB position (snapshot's position may be
                        # stale if a concurrent writer inserted
                        # between load and save).
                        update_rows.append(
                            (
                                *row[:9],
                                db_id_position[msg_id],
                                *row[10:],
                            )
                        )
                    else:
                        # New row this writer added — assign a fresh
                        # tail position now.
                        new_rows.append(
                            (
                                *row[:9],
                                next_position,
                                *row[10:],
                            )
                        )
                        next_position += 1
                _MSG_UPSERT = (
                    "INSERT INTO chat_message ("
                    "id, conversation_id, parent_id, role, "
                    "content_text, media_refs_json, tool_calls_json, "
                    "tool_results_json, created_at, position, "
                    "usage_json, model_id, model_provider, meta_json, "
                    "sender_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    " parent_id=excluded.parent_id, "
                    " role=excluded.role, "
                    " content_text=excluded.content_text, "
                    " media_refs_json=excluded.media_refs_json, "
                    " tool_calls_json=excluded.tool_calls_json, "
                    " tool_results_json=excluded.tool_results_json, "
                    " created_at=excluded.created_at, "
                    " position=excluded.position, "
                    " usage_json=excluded.usage_json, "
                    " model_id=excluded.model_id, "
                    " model_provider=excluded.model_provider, "
                    " meta_json=excluded.meta_json, "
                    " sender_id=excluded.sender_id"
                )
                if update_rows:
                    await tx.executemany(_MSG_UPSERT, update_rows)
                if new_rows:
                    await tx.executemany(_MSG_UPSERT, new_rows)
                await tx.commit()
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.conversation.save_failed",
                f"failed to save conversation {conversation.id.value!r}: {exc}",
                operation="conversation.save",
                cause=exc,
            ) from exc

    async def delete(self, conversation_id: ConversationId) -> None:
        """Remove the conversation; raise if missing.

        ``ON DELETE CASCADE`` removes child messages atomically.
        """
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_conversation WHERE id = ?",
                    (conversation_id.value,),
                )
                rows_affected = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.conversation.delete_failed",
                f"failed to delete conversation {conversation_id.value!r}: {exc}",
                operation="conversation.delete",
                cause=exc,
            ) from exc
        if rows_affected == 0:
            raise ConversationNotFoundError(conversation_id.value)

    async def clear_request_ids(self) -> int:
        """Strip ``meta.request_id`` from every chat_message row at startup.

        9-G1 — prompt-snapshot parity with V1 ``history_store.py:660-686``.
        ``request_id`` links a stored assistant message to an in-memory
        prompt-snapshot entry (``PromptSnapshotStore`` is an ``OrderedDict``
        that is empty after a restart). If the persisted ``meta.request_id``
        survives the restart the UI keeps rendering the "view prompt
        snapshot" button for historical messages whose snapshot no longer
        exists, so the click 404s. Clearing the persisted ``request_id`` on
        startup keeps the DB consistent with the volatile snapshot store.

        Uses ``json_remove`` so the rest of ``meta_json`` (perf,
        subAgentBlocks, ...) is preserved. Returns the number of rows
        updated. Best-effort: a missing FTS/json1 extension or empty table
        simply updates zero rows.
        """
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "UPDATE chat_message "
                    "SET meta_json = json_remove(meta_json, '$.request_id') "
                    "WHERE meta_json IS NOT NULL "
                    "AND json_extract(meta_json, '$.request_id') IS NOT NULL",
                )
                rows_affected = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.message.clear_request_ids_failed",
                f"failed to clear request_id from message meta: {exc}",
                operation="message.clear_request_ids",
                cause=exc,
            ) from exc
        return rows_affected if isinstance(rows_affected, int) and rows_affected > 0 else 0

    # ------------------------------------------------------------------
    # Row -> domain
    # ------------------------------------------------------------------
    @staticmethod
    def _rows_to_conversation(
        header: tuple[object, ...],
        message_rows: tuple | list,
    ) -> Conversation:
        conv_id = ConversationId.of(str(header[0]))
        title = str(header[1])
        status = ConversationStatus(str(header[2]))
        created_at = datetime.fromisoformat(str(header[3]))
        updated_at = datetime.fromisoformat(str(header[4]))
        # meta_json (column index 5) appended by migration 020; NULL / absent
        # (short rows from older projections) -> None.
        meta_raw = header[5] if len(header) > 5 else None
        meta = json.loads(str(meta_raw)) if meta_raw else None
        # full_history_tokens (column index 6) appended by migration 036; NULL
        # / absent (short rows from 6-col list/search projections) -> None.
        full_history_tokens = header[6] if len(header) > 6 else None
        # detected_model_json (column index 7) appended by migration 057; NULL
        # / absent (short list/search projections, or an old DB before 057)
        # -> None. Forward-compatible: a conversation from a DB without the
        # column simply loads with ``detected_model=None``.
        detected_model_raw = header[7] if len(header) > 7 else None
        detected_model = (
            json.loads(str(detected_model_raw)) if detected_model_raw else None
        )
        messages: list[Message] = []
        for row in message_rows:
            messages.append(SqliteConversationRepository._row_to_message(row))
        return Conversation(
            id=conv_id,
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            status=status,
            messages=messages,
            meta=meta if isinstance(meta, dict) else None,
            full_history_tokens=(
                int(full_history_tokens)
                if full_history_tokens is not None
                else None
            ),
            detected_model=(
                detected_model if isinstance(detected_model, dict) else None
            ),
        )

    @staticmethod
    def _row_to_message(row: tuple[object, ...]) -> Message:
        msg_id = MessageId.of(str(row[0]))
        parent_id_raw = row[2]
        parent_id = (
            MessageId.of(str(parent_id_raw)) if parent_id_raw is not None else None
        )
        role = MessageRole(str(row[3]))
        media_refs = tuple(json.loads(str(row[5] or "[]")))
        tool_calls = tuple(json.loads(str(row[6] or "[]")))
        tool_results = tuple(json.loads(str(row[7] or "[]")))
        created_at = datetime.fromisoformat(str(row[8]))
        # usage_json (column index 10) is appended (migration 013); NULL /
        # absent (short rows from list/search projections) -> no usage.
        usage_raw = row[10] if len(row) > 10 else None
        usage = json.loads(str(usage_raw)) if usage_raw else None
        # model_id / model_provider (column index 11/12) appended
        # (migration 018); NULL / absent (short projection rows) -> None.
        model_id_raw = row[11] if len(row) > 11 else None
        model_provider_raw = row[12] if len(row) > 12 else None
        # meta_json (column index 13) appended (migration 021); NULL / absent
        # (short projection rows) -> no extras. Holds the render extras the
        # streaming writer persists (``build_assistant_meta`` in
        # ``_streaming_subagent_frames.py``): currently ``request_id`` /
        # ``perf`` / ``subAgentBlocks``. The frontend lifts ``perf`` /
        # ``subAgentBlocks`` to top-level ChatMessage fields on load
        # (``chatTabs.ts``) and reads ``request_id`` from ``meta.request_id``
        # (prompt-snapshot button). Other V1 render fields (image_url /
        # tool_full_output / tool_truncated / tool_output_size) are not
        # persisted in v2 (the live SSE frames carry them; they are not part
        # of the stored render extras), so they are intentionally absent here.
        meta_raw = row[13] if len(row) > 13 else None
        meta = json.loads(str(meta_raw)) if meta_raw else None
        # sender_id (column index 14) appended (migration 031); NULL / absent
        # (short projection rows) -> None. The orthogonal "who said it"
        # identity (a ParticipantId string) for multi-agent discussions; for
        # ordinary single-assistant chat it is NULL.
        sender_id_raw = row[14] if len(row) > 14 else None
        return Message(
            id=msg_id,
            role=role,
            content=MessageContent(text=str(row[4]), media_refs=media_refs),
            created_at=created_at,
            parent_id=parent_id,
            tool_calls=tool_calls,
            tool_results=tool_results,
            usage=usage if isinstance(usage, dict) else None,
            model_id=str(model_id_raw) if model_id_raw is not None else None,
            model_provider=(
                str(model_provider_raw) if model_provider_raw is not None else None
            ),
            meta=meta if isinstance(meta, dict) else None,
            sender_id=str(sender_id_raw) if sender_id_raw is not None else None,
        )


def _parse_cursor(cursor: str | None) -> int:
    """Decode ``"position:<int>"`` cursors. Invalid cursors fall to 0."""
    if not cursor:
        return 0
    try:
        prefix, _, value = cursor.partition(":")
        if prefix != "position":
            return 0
        return max(0, int(value))
    except (ValueError, TypeError):
        return 0

