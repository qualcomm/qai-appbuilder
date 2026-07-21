# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`ExperienceRecallPort` (PR-402 / S7.5 lane L4).

Migrates :meth:`backend.memory.ExperienceMemory.recall` and
:meth:`backend.memory.ExperienceMemory.build_context_block` (~150 LOC
combined) into the chat bounded context, sharing the
:data:`chat_experience` table that
:class:`qai.chat.adapters.SqliteExperienceRepository` (PR-042) already
manages — we do NOT introduce a new table.

Search strategy:

* **FTS5** is the primary path. Migration ``009_chat_experience_fts5.sql``
  (PR-094 §17.5 #15) created the ``experience_fts`` virtual table and
  3 sync triggers (insert / update / delete) that keep it in lock-step
  with ``chat_experience``. Each ``recall`` call queries the FTS index
  via ``MATCH`` first; results are ordered by FTS5's BM25 ``rank``.
* **LIKE fallback** is engaged when the FTS index is unavailable
  (e.g. SQLite build without FTS5, migration not applied yet, or a
  query string that produces an FTS5 syntax error). The fallback
  splits the query on whitespace, lowercases each term, and OR-joins
  the per-term ``LIKE`` matches against the ``category`` and
  ``content`` columns plus a ``metadata_json`` substring scan.

Output shape: :class:`ExperienceRecall` value objects + a
:class:`MemoryContextBlock` rendered as the legacy
``<past_experiences>`` XML block (1:1 with
``backend/memory.py:240-281``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from qai.chat.application.ports import (
    ExperienceRecall,
    ExperienceRecallPort,
    MemoryContextBlock,
)
from qai.platform.errors import PersistenceError
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


_log = get_logger(__name__)


_RECALL_COLUMNS = "id, category, content, metadata_json"


class SqliteExperienceRecall(ExperienceRecallPort):
    """aiosqlite implementation of :class:`ExperienceRecallPort`.

    Reads from the same ``chat_experience`` table managed by
    :class:`SqliteExperienceRepository` (CRUD).  Splitting recall into
    its own adapter keeps :class:`ExperienceRepositoryPort` Protocol
    signatures stable (v2.7 §3.1 — Protocols on the public surface
    must not gain methods).
    """

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def recall(
        self,
        *,
        query: str,
        limit: int = 5,
    ) -> tuple[ExperienceRecall, ...]:
        terms = [t for t in query.strip().split() if t]
        if not terms:
            return ()
        if limit <= 0:
            return ()

        # PR-094 §17.5 #13 — FTS5 first; LIKE only on FTS unavailable.
        # The FTS5 path uses BM25 ``rank`` ordering for true relevance;
        # the LIKE path keeps the legacy substring match as a safety net
        # for environments without FTS5 (older SQLite builds, or before
        # migration 009 has run).
        rows = await self._recall_via_fts5(terms=terms, limit=limit)
        if rows is None:
            rows = await self._recall_via_like(terms=terms, limit=limit)
        return tuple(self._row_to_recall(row, terms=terms) for row in rows)

    async def _recall_via_fts5(
        self,
        *,
        terms: list[str],
        limit: int,
    ) -> list[tuple[Any, ...]] | None:
        """Return rows via the ``experience_fts`` virtual table or ``None``.

        ``None`` indicates the FTS index is unavailable for this query
        (table missing, FTS5 not compiled in, or the constructed MATCH
        expression rejected by the FTS5 parser); callers should fall
        back to :meth:`_recall_via_like`.
        """
        # FTS5 OR-joins each term with explicit ``OR`` and quotes each
        # term to neutralise FTS5 syntactic characters (``-`` ``"`` ``*``
        # ``(`` ``)`` etc.) that could otherwise turn user input into
        # syntax errors.
        match_expr = " OR ".join(
            f'"{t.replace(chr(34), chr(34) + chr(34))}"' for t in terms
        )
        sql = (
            "SELECT e.id, e.category, e.content, e.metadata_json "
            "FROM experience_fts AS f "
            "JOIN chat_experience AS e ON e.id = f.experience_id "
            "WHERE experience_fts MATCH ? "
            "ORDER BY rank "
            "LIMIT ?"
        )
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(sql, (match_expr, int(limit)))
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001 — fall back to LIKE
            _log.debug(
                "experience_recall.fts5_unavailable: falling back to LIKE: %s",
                exc,
            )
            return None
        return list(rows)

    async def _recall_via_like(
        self,
        *,
        terms: list[str],
        limit: int,
    ) -> list[tuple[Any, ...]]:
        """Legacy LIKE-fallback path; mirrors ``backend/memory.py:204-237``."""
        # Per-term ``(category LIKE ? OR content LIKE ? OR
        # metadata_json LIKE ?)`` clauses, OR-joined.  Case-insensitive
        # LIKE is the SQLite default for ASCII; for multilingual
        # content the substring match still works since SQLite compares
        # bytes.
        clause_chunks: list[str] = []
        params: list[Any] = []
        for term in terms:
            pattern = f"%{term}%"
            clause_chunks.append(
                "(category LIKE ? OR content LIKE ? OR metadata_json LIKE ?)",
            )
            params.extend([pattern, pattern, pattern])
        where_clause = " OR ".join(clause_chunks)
        params.append(limit)

        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_RECALL_COLUMNS} FROM chat_experience "
                    f"WHERE {where_clause} "
                    f"ORDER BY created_at DESC LIMIT ?",
                    tuple(params),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.experience.recall_failed",
                f"failed to recall experiences: {exc}",
                operation="experience.recall",
                cause=exc,
            ) from exc

        return list(rows)

    async def build_context_block(
        self,
        *,
        query: str,
        max_chars: int = 3000,
    ) -> MemoryContextBlock:
        # Pull a generous candidate set (legacy used limit=10) so the
        # XML render has enough rows to choose from before hitting the
        # char budget.
        hits = await self.recall(query=query, limit=10)
        if not hits:
            return MemoryContextBlock(text="", hit_ids=())

        opener = "<past_experiences>"
        closer = "</past_experiences>"
        lines: list[str] = [opener]
        current_len = len(opener) + len(closer) + 2  # +2 for joining newlines
        used_ids: list[str] = []

        for hit in hits:
            entry_lines = [
                f'  <experience id="{_xml_attr(hit.experience_id)}" '
                f'category="{_xml_attr(hit.category)}">',
                f"    <content>{_xml_text(hit.content)}</content>",
            ]
            # Inline a couple of metadata keys when present (legacy parity:
            # ``key_steps`` and ``reusable_insights`` were the two fields
            # the old block surfaced).
            metadata = hit.metadata
            for key in ("key_steps", "reusable_insights"):
                value = metadata.get(key) if isinstance(metadata, dict) else None
                if value:
                    entry_lines.append(
                        f"    <{key}>{_xml_text(_render_meta_value(value))}</{key}>",
                    )
            entry_lines.append("  </experience>")
            entry_text = "\n".join(entry_lines)
            if current_len + len(entry_text) + 1 > max_chars:
                break
            lines.append(entry_text)
            current_len += len(entry_text) + 1
            used_ids.append(hit.experience_id)

        if not used_ids:
            # Even the smallest hit overflowed the budget.
            return MemoryContextBlock(text="", hit_ids=())

        lines.append(closer)
        return MemoryContextBlock(text="\n".join(lines), hit_ids=tuple(used_ids))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_recall(
        row: tuple[Any, ...],
        *,
        terms: list[str],
    ) -> ExperienceRecall:
        exp_id, category, content, metadata_json = row[0], row[1], row[2], row[3]
        try:
            metadata = (
                json.loads(metadata_json) if metadata_json else {}
            )
            if not isinstance(metadata, dict):
                metadata = {}
        except json.JSONDecodeError:
            metadata = {}

        # Heuristic relevance: fraction of query terms that appear in
        # category + content (case-insensitive substring). This signal
        # backs both code paths (FTS5 and LIKE) so callers see a stable
        # relevance scalar even when the underlying query strategy
        # changed mid-session (e.g. FTS5 → LIKE on a malformed expr).
        haystack = (str(category) + "\n" + str(content)).lower()
        if terms:
            hits = sum(1 for t in terms if t.lower() in haystack)
            relevance = hits / len(terms)
        else:
            relevance = 0.0

        return ExperienceRecall(
            experience_id=str(exp_id),
            category=str(category),
            content=str(content),
            metadata=metadata,
            relevance=relevance,
        )


def _xml_attr(value: str) -> str:
    """Escape an XML attribute value (minimal: ``&`` ``<`` ``>`` ``"``)."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _xml_text(value: str) -> str:
    """Escape XML text content (minimal: ``&`` ``<`` ``>``)."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _render_meta_value(value: Any) -> str:
    """Render a metadata value as text — list/dict become JSON, scalars
    stringify.  Stable serialisation is important so the rendered
    block is deterministic across runs."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


__all__ = ["SqliteExperienceRecall"]
