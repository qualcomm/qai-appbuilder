# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed search over past conversation messages.

Backs the ``search_conversations`` tool: an agent-driven full-text search
over the ``chat_message`` table so the model can recall how a similar
problem was handled in a DIFFERENT past conversation. The CURRENT
conversation is already in the model's context window, so the default
scope EXCLUDES it (``scope='others'``); ``scope='all'`` includes it.

Search strategy mirrors :class:`SqliteConversationRepository.search`:
FTS5 ``chat_message_fts`` (migration 016) with BM25 ``rank`` ordering and
``<mark>`` snippet highlighting, falling back to a ``LIKE`` scan when the
FTS index is unavailable. CJK queries are bigram-preprocessed via the
shared :func:`build_fts_match` so Chinese substrings match the
``unicode61`` tokenizer.

Four modes:

* ``discover`` — FTS keyword search across past conversations;
* ``scroll``   — a window of messages centred on an anchor message id;
* ``read``     — a whole conversation's head + tail;
* ``browse``   — most-recent conversations (metadata only, no FTS).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from qai.chat.adapters._fts_query import build_fts_match, make_snippet
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = [
    "ConversationBrowseItem",
    "ConversationSearchHit",
    "MessageWindowItem",
    "SqliteConversationSearchRepository",
]


def _has_cjk(text: str) -> bool:
    """Return True if *text* contains any CJK ideograph.

    Used to route CJK queries away from the ``unicode61`` FTS index (which
    does not segment Chinese) toward the LIKE substring scan.
    """
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def _like_pattern(query: str) -> str:
    """Build a ``LIKE`` substring pattern with metacharacters escaped.

    Escapes the LIKE wildcards ``%`` and ``_`` plus the escape char ``\\``
    itself so a user query containing them (e.g. ``100%`` or ``foo_bar``)
    matches literally rather than as wildcards. Pairs with a SQL
    ``LIKE ? ESCAPE '\\'`` clause.
    """
    escaped = (
        query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationSearchHit:
    """One ``discover`` result: a matched message with a snippet."""

    conversation_id: str
    conversation_title: str
    message_id: str
    role: str
    snippet: str
    created_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageWindowItem:
    """One message row in a ``scroll`` / ``read`` window."""

    message_id: str
    role: str
    content_text: str
    position: int
    created_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationBrowseItem:
    """One ``browse`` result: conversation metadata (no body)."""

    conversation_id: str
    title: str
    status: str
    updated_at: str
    message_count: int


class SqliteConversationSearchRepository:
    """aiosqlite search over ``chat_message`` for the search tool."""

    __slots__ = ("_db",)

    def __init__(self, *, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------
    async def discover(
        self,
        *,
        query: str,
        current_conversation_id: str,
        scope: str = "others",
        limit: int = 10,
    ) -> tuple[ConversationSearchHit, ...]:
        """FTS5 keyword search over past conversations' message bodies.

        ``scope='others'`` (default) excludes ``current_conversation_id``;
        ``scope='all'`` includes it. Falls back to a ``LIKE`` scan when the
        FTS index is unavailable. Returns up to ``limit`` hits ordered by
        BM25 ``rank`` (FTS path) or recency (LIKE path).
        """
        if not query or not query.strip() or limit <= 0:
            return ()
        # The ``unicode61`` FTS5 tokenizer treats a contiguous CJK run as a
        # SINGLE token (it does not segment Chinese), so an FTS ``MATCH`` on a
        # CJK substring never hits. For CJK queries go straight to the LIKE
        # scan, which matches substrings reliably. Non-CJK queries keep the
        # FTS (BM25) path with a LIKE fallback when the index is unavailable.
        rows: list[tuple[Any, ...]] | None
        if _has_cjk(query):
            rows = None
        else:
            rows = await self._discover_fts(
                query=query,
                current_conversation_id=current_conversation_id,
                scope=scope,
                limit=limit,
            )
        if rows is None:
            rows = await self._discover_like(
                query=query,
                current_conversation_id=current_conversation_id,
                scope=scope,
                limit=limit,
            )
        return tuple(
            ConversationSearchHit(
                conversation_id=str(r[0]),
                conversation_title=str(r[1] or ""),
                message_id=str(r[2]),
                role=str(r[3]),
                snippet=str(r[4] or ""),
                created_at=str(r[5] or ""),
            )
            for r in rows
        )

    async def _discover_fts(
        self,
        *,
        query: str,
        current_conversation_id: str,
        scope: str,
        limit: int,
    ) -> list[tuple[Any, ...]] | None:
        """FTS body search + snippet; ``None`` signals FTS unavailable."""
        match_expr = build_fts_match(query)
        # ``scope='others'`` excludes the current conversation; ``all``
        # keeps every conversation. The scope predicate is a literal
        # fragment (no user input) so it is safe to interpolate.
        scope_pred = (
            "AND f.conversation_id <> ? " if scope != "all" else ""
        )
        sql = (
            "SELECT f.conversation_id, c.title, m.id, m.role, "  # noqa: S608
            "snippet(chat_message_fts, 0, '<mark>', '</mark>', '...', 32), "
            "m.created_at "
            "FROM chat_message_fts AS f "
            "JOIN chat_message AS m ON m.rowid = f.rowid "
            "JOIN chat_conversation AS c ON c.id = f.conversation_id "
            "WHERE chat_message_fts MATCH ? "
            f"{scope_pred}"
            "ORDER BY rank "
            "LIMIT ?"
        )
        params: list[Any] = [match_expr]
        if scope != "all":
            params.append(current_conversation_id)
        params.append(int(limit))
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(sql, tuple(params))
                rows = await cur.fetchall()
                await cur.close()
        except Exception:  # noqa: BLE001 — fall back to LIKE
            return None
        return [tuple(r) for r in rows]

    async def _discover_like(
        self,
        *,
        query: str,
        current_conversation_id: str,
        scope: str,
        limit: int,
    ) -> list[tuple[Any, ...]]:
        """LIKE fallback: body match + Python-side snippet."""
        like = _like_pattern(query)
        scope_pred = (
            "AND m.conversation_id <> ? " if scope != "all" else ""
        )
        sql = (
            "SELECT m.conversation_id, c.title, m.id, m.role, "  # noqa: S608
            "m.content_text, m.created_at "
            "FROM chat_message AS m "
            "JOIN chat_conversation AS c ON c.id = m.conversation_id "
            "WHERE m.content_text LIKE ? ESCAPE '\\' "
            f"{scope_pred}"
            "ORDER BY m.created_at DESC "
            "LIMIT ?"
        )
        params: list[Any] = [like]
        if scope != "all":
            params.append(current_conversation_id)
        params.append(int(limit))
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(sql, tuple(params))
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:
            raise PersistenceError(
                "chat.conversation_search.discover_failed",
                f"failed to search conversations: {exc}",
                operation="conversation_search.discover",
                cause=exc,
            ) from exc
        # Synthesise a snippet for each row (body -> highlighted excerpt).
        out: list[tuple[Any, ...]] = []
        for r in rows:
            snippet = make_snippet(str(r[4] or ""), query)
            out.append((r[0], r[1], r[2], r[3], snippet, r[5]))
        return out

    # ------------------------------------------------------------------
    # scroll
    # ------------------------------------------------------------------
    async def scroll(
        self,
        *,
        anchor_message_id: str,
        window: int = 10,
    ) -> tuple[MessageWindowItem, ...]:
        """Return messages centred on ``anchor_message_id``.

        Loads the anchor's conversation + position, then returns rows in
        ``[position - window, position + window]`` ordered by position.
        Empty when the anchor id does not resolve.
        """
        window = max(window, 0)
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT conversation_id, position FROM chat_message "
                    "WHERE id = ?",
                    (anchor_message_id,),
                )
                anchor = await cur.fetchone()
                await cur.close()
                if anchor is None:
                    return ()
                conv_id = str(anchor[0])
                pos = int(anchor[1])
                cur = await conn.execute(
                    "SELECT id, role, content_text, position, created_at "
                    "FROM chat_message "
                    "WHERE conversation_id = ? "
                    "AND position >= ? AND position <= ? "
                    "ORDER BY position ASC",
                    (conv_id, pos - window, pos + window),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:
            raise PersistenceError(
                "chat.conversation_search.scroll_failed",
                f"failed to scroll around {anchor_message_id!r}: {exc}",
                operation="conversation_search.scroll",
                cause=exc,
            ) from exc
        return tuple(
            MessageWindowItem(
                message_id=str(r[0]),
                role=str(r[1]),
                content_text=str(r[2] or ""),
                position=int(r[3]),
                created_at=str(r[4] or ""),
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------
    async def read(
        self,
        *,
        conversation_id: str,
        head: int = 20,
        tail: int = 10,
    ) -> tuple[MessageWindowItem, ...]:
        """Return a conversation's first ``head`` + last ``tail`` messages.

        When the conversation has at most ``head + tail`` messages, every
        message is returned once (no duplication). Empty when the
        conversation has no messages.
        """
        head = max(head, 0)
        tail = max(tail, 0)
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT COUNT(*) FROM chat_message "
                    "WHERE conversation_id = ?",
                    (conversation_id,),
                )
                count_row = await cur.fetchone()
                await cur.close()
                total = int(count_row[0]) if count_row else 0
                if total == 0:
                    return ()
                if total <= head + tail:
                    # Whole conversation fits — return everything once.
                    cur = await conn.execute(
                        "SELECT id, role, content_text, position, "
                        "created_at FROM chat_message "
                        "WHERE conversation_id = ? "
                        "ORDER BY position ASC",
                        (conversation_id,),
                    )
                    rows = await cur.fetchall()
                    await cur.close()
                else:
                    cur = await conn.execute(
                        "SELECT id, role, content_text, position, "
                        "created_at FROM chat_message "
                        "WHERE conversation_id = ? "
                        "ORDER BY position ASC LIMIT ?",
                        (conversation_id, head),
                    )
                    head_rows = await cur.fetchall()
                    await cur.close()
                    cur = await conn.execute(
                        "SELECT id, role, content_text, position, "
                        "created_at FROM ("
                        "  SELECT id, role, content_text, position, "
                        "  created_at FROM chat_message "
                        "  WHERE conversation_id = ? "
                        "  ORDER BY position DESC LIMIT ?"
                        ") ORDER BY position ASC",
                        (conversation_id, tail),
                    )
                    tail_rows = await cur.fetchall()
                    await cur.close()
                    rows = [*head_rows, *tail_rows]
        except Exception as exc:
            raise PersistenceError(
                "chat.conversation_search.read_failed",
                f"failed to read conversation {conversation_id!r}: {exc}",
                operation="conversation_search.read",
                cause=exc,
            ) from exc
        return tuple(
            MessageWindowItem(
                message_id=str(r[0]),
                role=str(r[1]),
                content_text=str(r[2] or ""),
                position=int(r[3]),
                created_at=str(r[4] or ""),
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # browse
    # ------------------------------------------------------------------
    async def browse(
        self,
        *,
        current_conversation_id: str,
        scope: str = "others",
        limit: int = 20,
    ) -> tuple[ConversationBrowseItem, ...]:
        """List most-recent conversations (metadata only, no FTS).

        ``scope='others'`` (default) excludes the current conversation.
        """
        if limit <= 0:
            return ()
        scope_pred = (
            "WHERE c.id <> ? " if scope != "all" else ""
        )
        sql = (
            "SELECT c.id, c.title, c.status, c.updated_at, "  # noqa: S608
            "(SELECT COUNT(*) FROM chat_message m "
            " WHERE m.conversation_id = c.id) AS msg_count "
            "FROM chat_conversation AS c "
            f"{scope_pred}"
            "ORDER BY c.updated_at DESC "
            "LIMIT ?"
        )
        params: list[Any] = []
        if scope != "all":
            params.append(current_conversation_id)
        params.append(int(limit))
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(sql, tuple(params))
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:
            raise PersistenceError(
                "chat.conversation_search.browse_failed",
                f"failed to browse conversations: {exc}",
                operation="conversation_search.browse",
                cause=exc,
            ) from exc
        return tuple(
            ConversationBrowseItem(
                conversation_id=str(r[0]),
                title=str(r[1] or ""),
                status=str(r[2] or ""),
                updated_at=str(r[3] or ""),
                message_count=int(r[4] or 0),
            )
            for r in rows
        )
