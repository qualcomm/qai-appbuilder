# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`ParticipantRepositoryPort`.

Schema reference: ``qai-db-schema.md`` §2.7 (chat_participant, migration
030).  A :class:`~qai.chat.domain.participant.Participant` is a
**single-row aggregate** -- the whole speaker identity lives in one
``chat_participant`` row -- so :meth:`save` is a single ``BEGIN IMMEDIATE``
upsert with no child-row rewrite (mirroring
:class:`~qai.chat.adapters.sub_agent_session_repository.
SqliteSubAgentSessionRepository`).

A participant is the orthogonal "who is speaking" dimension that
complements ``MessageRole`` ("what kind of turn"); today it carries
sub-agents (``kind=sub_agent``) and in the future named role agents
(``kind=named_agent``).  The optional ``subagent_session_id`` links a
sub-agent participant to its persisted :class:`SubAgentSession`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from qai.chat.domain.errors import ParticipantNotFoundError
from qai.chat.domain.ids import (
    ConversationId,
    ParticipantId,
    SubAgentSessionId,
)
from qai.chat.domain.participant import Participant, ParticipantKind
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = ["SqliteParticipantRepository"]


_COLUMNS = (
    "id, conversation_id, kind, display_name, model_id, persona, "
    "subagent_session_id, created_at, updated_at, config_json, template_id"
)


class SqliteParticipantRepository:
    """aiosqlite implementation of :class:`ParticipantRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------
    async def save(self, participant: Participant) -> None:
        """Insert or upsert ``participant`` (single-row aggregate, by id)."""
        params = (
            participant.id.value,
            participant.conversation_id.value,
            participant.kind.value,
            participant.display_name,
            participant.model_id,
            participant.persona,
            participant.subagent_session_id.value
            if participant.subagent_session_id is not None
            else None,
            participant.created_at.isoformat(),
            participant.updated_at.isoformat(),
            # Per-participant config blob (migration 034 ``config_json``):
            # JSON-serialised when present, NULL when the aggregate carries
            # no config (plain user / main-agent / sub-agent participants).
            json.dumps(participant.config, ensure_ascii=False)
            if participant.config
            else None,
            # Built-in template provenance (migration 056 ``template_id``):
            # the source template id / composite ``<roster_id>#<index>`` key,
            # or NULL for user-authored / main / sub-agent participants.
            participant.template_id,
        )
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(
                        "INSERT INTO chat_participant ("
                        "id, conversation_id, kind, display_name, model_id, "
                        "persona, subagent_session_id, created_at, updated_at, "
                        "config_json, template_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        " conversation_id=excluded.conversation_id, "
                        " kind=excluded.kind, "
                        " display_name=excluded.display_name, "
                        " model_id=excluded.model_id, "
                        " persona=excluded.persona, "
                        " subagent_session_id=excluded.subagent_session_id, "
                        " updated_at=excluded.updated_at, "
                        " config_json=excluded.config_json, "
                        " template_id=excluded.template_id",
                        params,
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.participant.save_failed",
                f"failed to save participant {participant.id.value!r}: {exc}",
                operation="participant.save",
                cause=exc,
            ) from exc

    async def delete(self, participant_id: ParticipantId) -> None:
        """Remove the participant; raise if missing."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_participant WHERE id = ?",
                    (participant_id.value,),
                )
                rows_affected = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.participant.delete_failed",
                f"failed to delete participant {participant_id.value!r}: {exc}",
                operation="participant.delete",
                cause=exc,
            ) from exc
        if rows_affected == 0:
            raise ParticipantNotFoundError(participant_id.value)

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------
    async def find(
        self,
        participant_id: ParticipantId,
    ) -> Participant | None:
        """Return the aggregate or ``None`` if not present."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_participant WHERE id = ?",
                    (participant_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.participant.find_failed",
                f"failed to load participant {participant_id.value!r}: {exc}",
                operation="participant.find",
                cause=exc,
            ) from exc
        if row is None:
            return None
        return self._row_to_participant(row)

    async def get(self, participant_id: ParticipantId) -> Participant:
        """Return the aggregate; raise :class:`ParticipantNotFoundError`."""
        participant = await self.find(participant_id)
        if participant is None:
            raise ParticipantNotFoundError(participant_id.value)
        return participant

    async def list_by_conversation(
        self,
        conversation_id: ConversationId,
    ) -> tuple[Participant, ...]:
        """Return all participants of the conversation, ``created_at`` ASC."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_participant "
                    "WHERE conversation_id = ? "
                    "ORDER BY created_at ASC",
                    (conversation_id.value,),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.participant.list_by_conversation_failed",
                "failed to list participants for conversation "
                f"{conversation_id.value!r}: {exc}",
                operation="participant.list_by_conversation",
                cause=exc,
            ) from exc
        return tuple(self._row_to_participant(r) for r in rows)

    # ------------------------------------------------------------------
    # Row -> domain
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_participant(row: tuple[object, ...]) -> Participant:
        model_id_raw = row[4]
        persona_raw = row[5]
        subagent_session_id_raw = row[6]
        # Per-participant config blob (migration 034 ``config_json``, column
        # index 9). Tail-appended to ``_COLUMNS`` so a short projection (legacy
        # row read before the column existed) leaves ``config`` unset. A
        # malformed / non-object JSON value degrades to ``None`` rather than
        # raising, so a single bad row cannot break a list/find.
        config: dict[str, object] | None = None
        if len(row) > 9 and row[9] is not None:
            try:
                parsed = json.loads(str(row[9]))
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                config = parsed
        # Built-in template provenance (migration 056 ``template_id``, column
        # index 10). Tail-appended so a short projection (legacy row read before
        # the column existed) leaves ``template_id`` as None -> no runtime
        # persona override (existing behaviour unchanged).
        template_id: str | None = None
        if len(row) > 10 and row[10] is not None:
            template_id = str(row[10])
        return Participant(
            id=ParticipantId.of(str(row[0])),
            conversation_id=ConversationId.of(str(row[1])),
            kind=ParticipantKind(str(row[2])),
            display_name=str(row[3]) if row[3] is not None else "",
            model_id=str(model_id_raw) if model_id_raw is not None else None,
            persona=str(persona_raw) if persona_raw is not None else None,
            subagent_session_id=(
                SubAgentSessionId.of(str(subagent_session_id_raw))
                if subagent_session_id_raw is not None
                else None
            ),
            config=config,
            template_id=template_id,
            created_at=datetime.fromisoformat(str(row[7])),
            updated_at=datetime.fromisoformat(str(row[8])),
        )
