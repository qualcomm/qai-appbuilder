# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed store + full-text recall over ``chat_experience``.

The experience store is the agent-editable long-term "lessons" layer: the
model writes distilled, reusable knowledge (decisions, gotchas, user
preferences) via the ``remember_experience`` tool and pulls the relevant
handful back on demand via ``recall_experience`` — a deliberate mirror of the
``search_conversations`` design (the model decides WHEN to look back, instead
of an always-injected block). Experiences live in the ``chat_experience``
table (migration 002) with an FTS5 index (``experience_fts``, migration 009)
and recall-ranking metadata (``updated_at`` / ``use_count`` / ``importance``,
migration 064).

Two collaborators, split by Interface Segregation:

* :class:`SqliteExperienceRepository` — write side (save / update / delete).
* :class:`SqliteExperienceRecall` — read side (FTS5 BM25 recall with a LIKE
  fallback, reusing :mod:`qai.chat.adapters._fts_query` so CJK bigram handling
  and ``<mark>`` snippets match the conversation-search tool exactly).

CJK handling mirrors the conversation-search repository: the ``unicode61``
tokenizer treats a contiguous CJK run as a single token, so a CJK query goes
straight to the LIKE substring scan; non-CJK queries take the FTS (BM25) path
with a LIKE fallback when the index is unavailable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.chat.adapters._fts_query import build_fts_match
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = [
    "ExperienceRecallHit",
    "SqliteExperienceRecall",
    "SqliteExperienceRepository",
]

#: Category / content length bounds mirror the CHECK constraints on
#: ``chat_experience`` (migration 002): category 1..64, content 1..100000.
_MAX_CATEGORY_LEN = 64
_MAX_CONTENT_LEN = 100_000

#: Importance is a 0.0..1.0 salience weight; out-of-range inputs are clamped.
_MIN_IMPORTANCE = 0.0
_MAX_IMPORTANCE = 1.0
_DEFAULT_IMPORTANCE = 0.5


def _has_cjk(text: str) -> bool:
    """Return True if *text* contains any CJK ideograph.

    A contiguous CJK run is one FTS5 ``unicode61`` token, so a CJK query
    cannot MATCH a substring; such queries take the LIKE path instead.
    """
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def _like_pattern(query: str) -> str:
    """Build a ``LIKE`` substring pattern with metacharacters escaped."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _now_iso() -> str:
    """Current UTC timestamp as ISO-8601 (single source of truth for time)."""
    return datetime.now(timezone.utc).isoformat()


def _clamp_importance(value: Any) -> float:
    """Coerce *value* to a 0.0..1.0 float, defaulting on junk input."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_IMPORTANCE
    if out != out:  # NaN
        return _DEFAULT_IMPORTANCE
    return max(_MIN_IMPORTANCE, min(_MAX_IMPORTANCE, out))


@dataclass(frozen=True, slots=True, kw_only=True)
class ExperienceRecallHit:
    """One ``recall`` result: a matched experience with its full content."""

    experience_id: str
    category: str
    content: str
    importance: float
    created_at: str


class SqliteExperienceRepository:
    """aiosqlite write side over ``chat_experience`` (save / update / delete).

    Best-effort and self-contained: generates its own ids (uuid4 hex) and ISO
    timestamps so it needs no clock / id-generator DI. The ``experience_fts``
    triggers (migration 009) keep the FTS index in sync on every write, so this
    class never touches the FTS table directly.
    """

    __slots__ = ("_db",)

    def __init__(self, *, db: Database) -> None:
        self._db = db

    async def save(
        self,
        *,
        category: str,
        content: str,
        importance: float = _DEFAULT_IMPORTANCE,
    ) -> str:
        """Insert a new experience; returns its generated id.

        ``category`` / ``content`` are clipped to the table's CHECK bounds so a
        caller can never trip the constraint. ``importance`` is clamped to
        0.0..1.0.
        """
        category = (category or "general").strip()[:_MAX_CATEGORY_LEN] or "general"
        content = (content or "").strip()[:_MAX_CONTENT_LEN]
        if not content:
            raise PersistenceError(
                "chat.experience.save_empty",
                "cannot save an experience with empty content",
                operation="experience.save",
            )
        exp_id = uuid.uuid4().hex
        now = _now_iso()
        sql = (
            "INSERT INTO chat_experience "
            "(id, category, content, metadata_json, created_at, use_count, "
            "importance) "
            "VALUES (?, ?, ?, '{}', ?, 0, ?)"
        )
        # updated_at is intentionally omitted (reads NULL): a freshly-created
        # row has never been edited, so update() is the only writer of
        # updated_at (migration 064 semantics: NULL == never edited).
        params = (exp_id, category, content, now, _clamp_importance(importance))
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(sql, params)
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001 — wrap all driver errors
            raise PersistenceError(
                "chat.experience.save_failed",
                f"failed to save experience: {exc}",
                operation="experience.save",
                cause=exc,
            ) from exc
        return exp_id

    async def update(
        self,
        *,
        experience_id: str,
        content: str | None = None,
        importance: float | None = None,
    ) -> bool:
        """Update an experience's content and/or importance in place.

        Returns True when a row was updated, False when ``experience_id`` did
        not exist. Touches ``updated_at`` only when something actually changed.
        The FTS ``AFTER UPDATE`` trigger re-syncs the index when content moves.
        """
        sets: list[str] = []
        params: list[Any] = []
        if content is not None:
            trimmed = content.strip()[:_MAX_CONTENT_LEN]
            if not trimmed:
                raise PersistenceError(
                    "chat.experience.update_empty",
                    "cannot update an experience to empty content",
                    operation="experience.update",
                )
            sets.append("content = ?")
            params.append(trimmed)
        if importance is not None:
            sets.append("importance = ?")
            params.append(_clamp_importance(importance))
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.append(_now_iso())
        params.append(experience_id)
        sql = f"UPDATE chat_experience SET {', '.join(sets)} WHERE id = ?"  # noqa: S608 — set fragments are literal
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(sql, tuple(params))
                changed = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.experience.update_failed",
                f"failed to update experience: {exc}",
                operation="experience.update",
                cause=exc,
            ) from exc
        return bool(changed)

    async def delete(self, *, experience_id: str) -> bool:
        """Delete an experience; returns True when a row was removed.

        The FTS ``AFTER DELETE`` trigger removes the mirrored index row.
        """
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_experience WHERE id = ?", (experience_id,)
                )
                changed = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.experience.delete_failed",
                f"failed to delete experience: {exc}",
                operation="experience.delete",
                cause=exc,
            ) from exc
        return bool(changed)


class SqliteExperienceRecall:
    """aiosqlite read side: FTS5 BM25 recall over ``chat_experience`` content.

    ``recall`` returns up to ``limit`` hits ordered by relevance (FTS ``rank``
    tie-broken by ``importance`` then recency; the LIKE fallback orders by
    ``importance`` then recency). Each hit carries the experience's FULL
    ``content`` (entries are short and length-capped, so no snippet truncation
    that could cut off the answer). Reuses :func:`build_fts_match` for CJK
    bigram handling on the FTS path, identical to ``search_conversations``.
    """

    __slots__ = ("_db",)

    def __init__(self, *, db: Database) -> None:
        self._db = db

    async def recall(self, *, query: str, limit: int = 5) -> tuple[ExperienceRecallHit, ...]:
        """Full-text recall over experience content; empty query -> no hits."""
        if not query or not query.strip() or limit <= 0:
            return ()
        rows: list[tuple[Any, ...]] | None
        if _has_cjk(query):
            rows = None  # CJK: unicode61 can't MATCH a substring -> LIKE path
        else:
            rows = await self._recall_fts(query=query, limit=limit)
        if rows is None:
            rows = await self._recall_like(query=query, limit=limit)
        return tuple(
            ExperienceRecallHit(
                experience_id=str(r[0]),
                category=str(r[1] or ""),
                content=str(r[2] or ""),
                importance=float(r[3] if r[3] is not None else _DEFAULT_IMPORTANCE),
                created_at=str(r[4] or ""),
            )
            for r in rows
        )

    async def _recall_fts(
        self, *, query: str, limit: int
    ) -> list[tuple[Any, ...]] | None:
        """FTS body search + snippet; ``None`` signals FTS unavailable."""
        match_expr = build_fts_match(query)
        sql = (
            "SELECT e.id, e.category, e.content, "  # noqa: S608 — no user input interpolated
            "e.importance, e.created_at "
            "FROM experience_fts AS f "
            "JOIN chat_experience AS e ON e.id = f.experience_id "
            "WHERE experience_fts MATCH ? "
            "ORDER BY rank, e.importance DESC, e.created_at DESC "
            "LIMIT ?"
        )
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(sql, (match_expr, int(limit)))
                rows = await cur.fetchall()
                await cur.close()
        except Exception:  # noqa: BLE001 — fall back to LIKE
            return None
        return [tuple(r) for r in rows]

    async def _recall_like(self, *, query: str, limit: int) -> list[tuple[Any, ...]]:
        """LIKE fallback: per-term OR match + Python-side snippet.

        The query is split on whitespace into terms; a row matches when its
        content contains ANY term (OR — recall-first). Matching the WHOLE query
        as one substring would require the content to contain the entire query
        phrase verbatim, which almost never holds for a multi-word question
        (e.g. "项目 内部代号 目标 NPU 量化精度" never appears literally in a
        stored lesson). Terms are still each escaped for LIKE metacharacters.
        """
        terms = [t for t in query.split() if t.strip()] or [query]
        clauses = " OR ".join("e.content LIKE ? ESCAPE '\\'" for _ in terms)
        sql = (
            "SELECT e.id, e.category, e.content, e.importance, e.created_at "  # noqa: S608 — clauses are literal LIKE fragments
            "FROM chat_experience AS e "
            f"WHERE {clauses} "
            "ORDER BY e.importance DESC, e.created_at DESC "
            "LIMIT ?"
        )
        params = [_like_pattern(t) for t in terms]
        params.append(int(limit))
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(sql, tuple(params))
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.experience.recall_failed",
                f"failed to recall experiences: {exc}",
                operation="experience.recall",
                cause=exc,
            ) from exc
        # Return full content (row shape already matches recall(): id, category,
        # content, importance, created_at). Experience entries are short and
        # length-capped, so returning the whole content — rather than a
        # truncated snippet — avoids cutting off the answer the model needs.
        return [tuple(r) for r in rows]
