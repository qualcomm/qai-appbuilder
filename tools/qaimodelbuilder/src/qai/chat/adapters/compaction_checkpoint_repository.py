# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`CompactionCheckpointStorePort` (CCD-5).

Schema reference: ``qai-db-schema.md`` / migration 044
(``chat_compaction_checkpoint``). Like
:class:`~qai.chat.adapters.sub_agent_session_repository.
SqliteSubAgentSessionRepository`, a compaction checkpoint is a **single-row
aggregate** -- the whole checkpoint (including its already-summarised OpenAI
wire head) lives in ONE ``chat_compaction_checkpoint`` row keyed by
``conversation_id``, with the wire serialised to ``compacted_wire_json``.
:meth:`save` is therefore a single ``BEGIN IMMEDIATE`` upsert with no
child-row rewrite.

The checkpoint follows the conversation's lifecycle: the DB-level
``ON DELETE CASCADE`` on ``conversation_id`` removes it when the parent
conversation row is deleted (``foreign_keys=ON``), and :meth:`delete` lets a
caller drop it explicitly (idempotent). This is the durable backing store for
:class:`~qai.chat.application.use_cases.streaming.StreamChatUseCase`'s
in-process write-through cache, so compaction state survives a restart
(PENDING-WORK.md §1 CCD-5).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.chat.application.use_cases._agentic_kernel import CompactionCheckpoint
from qai.chat.domain.ids import ConversationId
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = ["SqliteCompactionCheckpointRepository"]


_COLUMNS = (
    "conversation_id, anchor_index, compacted_wire_json, estimated_tokens, "
    "last_eff_prompt, created_at, anchor_message_id, updated_at"
)


class SqliteCompactionCheckpointRepository:
    """aiosqlite implementation of :class:`CompactionCheckpointStorePort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------
    async def save(
        self,
        conversation_id: ConversationId,
        checkpoint: CompactionCheckpoint,
    ) -> None:
        """Insert or upsert ``checkpoint`` for ``conversation_id`` (by PK).

        Single-row aggregate: a ``BEGIN IMMEDIATE`` then ``INSERT ... ON
        CONFLICT(conversation_id) DO UPDATE`` so a repeated save for the same
        conversation overwrites the prior checkpoint in place (the in-memory
        write-through cache is the source of truth; persistence merely mirrors
        it). ``estimated_tokens`` / ``last_eff_prompt`` / ``anchor_message_id``
        are nullable — ``None`` round-trips to SQL NULL and back to the
        dataclass's ``None`` defaults.
        """
        cid = conversation_id.value
        compacted_wire_json = json.dumps(
            checkpoint.compacted_wire, ensure_ascii=False
        )
        estimated_tokens = (
            int(checkpoint.estimated_tokens)
            if checkpoint.estimated_tokens is not None
            else None
        )
        last_eff_prompt = (
            int(checkpoint.last_eff_prompt)
            if checkpoint.last_eff_prompt is not None
            else None
        )
        anchor_message_id = checkpoint.anchor_message_id
        updated_at = datetime.now(timezone.utc).isoformat()
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(
                        "INSERT INTO chat_compaction_checkpoint ("
                        "conversation_id, anchor_index, compacted_wire_json, "
                        "estimated_tokens, last_eff_prompt, created_at, "
                        "anchor_message_id, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(conversation_id) DO UPDATE SET "
                        " anchor_index=excluded.anchor_index, "
                        " compacted_wire_json=excluded.compacted_wire_json, "
                        " estimated_tokens=excluded.estimated_tokens, "
                        " last_eff_prompt=excluded.last_eff_prompt, "
                        " created_at=excluded.created_at, "
                        " anchor_message_id=excluded.anchor_message_id, "
                        " updated_at=excluded.updated_at",
                        (
                            cid,
                            int(checkpoint.anchor_index),
                            compacted_wire_json,
                            estimated_tokens,
                            last_eff_prompt,
                            float(checkpoint.created_at),
                            anchor_message_id,
                            updated_at,
                        ),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.compaction_checkpoint.save_failed",
                f"failed to save compaction checkpoint for {cid!r}: {exc}",
                operation="compaction_checkpoint.save",
                cause=exc,
            ) from exc

    async def delete(self, conversation_id: ConversationId) -> None:
        """Remove the conversation's checkpoint; idempotent (no error if absent)."""
        cid = conversation_id.value
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_compaction_checkpoint "
                    "WHERE conversation_id = ?",
                    (cid,),
                )
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.compaction_checkpoint.delete_failed",
                f"failed to delete compaction checkpoint for {cid!r}: {exc}",
                operation="compaction_checkpoint.delete",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------
    async def load(
        self,
        conversation_id: ConversationId,
    ) -> CompactionCheckpoint | None:
        """Return the conversation's checkpoint or ``None`` if not persisted."""
        cid = conversation_id.value
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_compaction_checkpoint "
                    "WHERE conversation_id = ?",
                    (cid,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.compaction_checkpoint.load_failed",
                f"failed to load compaction checkpoint for {cid!r}: {exc}",
                operation="compaction_checkpoint.load",
                cause=exc,
            ) from exc
        if row is None:
            return None
        return self._row_to_checkpoint(row)

    # ------------------------------------------------------------------
    # Row -> domain
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_checkpoint(row: tuple[object, ...]) -> CompactionCheckpoint:
        # Column order matches ``_COLUMNS`` (the SELECT projection):
        # 0 conversation_id, 1 anchor_index, 2 compacted_wire_json,
        # 3 estimated_tokens, 4 last_eff_prompt, 5 created_at,
        # 6 anchor_message_id, 7 updated_at.
        wire_raw = str(row[2] or "[]")
        try:
            wire = json.loads(wire_raw)
            if not isinstance(wire, list):
                wire = []
        except (TypeError, ValueError):
            wire = []
        compacted_wire: list[dict[str, Any]] = [
            entry for entry in wire if isinstance(entry, dict)
        ]
        estimated_tokens: int | None = None
        if row[3] is not None:
            try:
                estimated_tokens = int(row[3])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                estimated_tokens = None
        last_eff_prompt: int | None = None
        if row[4] is not None:
            try:
                last_eff_prompt = int(row[4])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                last_eff_prompt = None
        try:
            created_at = float(row[5])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            created_at = 0.0
        anchor_message_id = (
            str(row[6]) if row[6] is not None else None
        )
        return CompactionCheckpoint(
            anchor_index=int(row[1]),  # type: ignore[arg-type]
            compacted_wire=compacted_wire,
            estimated_tokens=estimated_tokens,
            last_eff_prompt=last_eff_prompt,
            created_at=created_at,
            anchor_message_id=anchor_message_id,
        )
