# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`SubAgentSessionRepositoryPort`.

Schema reference: ``qai-db-schema.md`` §2.6 (chat_subagent_session,
migration 030).  Unlike :class:`~qai.chat.adapters.conversation_repository.
SqliteConversationRepository` (a multi-row aggregate), a sub-agent session
is a **single-row aggregate** -- the whole session (including its
AUTHORITATIVE structured :class:`Message` transcript) lives in one
``chat_subagent_session`` row, with the transcript serialised to the
``messages_json`` column (SUBAGENT-UNIFY-6: the legacy flat
``wire_messages_json`` column was dropped in migration 048; the
feed-the-model wire is rebuilt from the transcript on demand).  :meth:`save`
is therefore a single ``BEGIN IMMEDIATE`` upsert with no child-row rewrite.

The aggregate carries the sub-agent's persisted context so the main agent
can wake it up and resume, and the user can take it over.  It follows the
parent conversation's lifecycle: :meth:`delete_by_parent` cascade-removes
all sessions when the parent conversation is deleted (mirroring the DB
``ON DELETE CASCADE`` while still working when the caller deletes via this
repository directly).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.errors import (
    InvalidMessageContentError,
    SubAgentSessionConflictError,
    SubAgentSessionNotFoundError,
)
from qai.chat.domain.ids import (
    ConversationId,
    MessageId,
    SubAgentSessionId,
)
from qai.chat.domain.message import Message
from qai.chat.domain.sub_agent_session import (
    SubAgentOwner,
    SubAgentSession,
    SubAgentSessionStatus,
)
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = ["SqliteSubAgentSessionRepository"]


logger = logging.getLogger(__name__)


_COLUMNS = (
    "id, root_conversation_id, parent_message_id, parent_subagent_id, depth, "
    "subagent_type, title, prompt_preview, status, owner, rounds, "
    "created_at, updated_at, version, usage_json, round_snapshots_json, "
    "last_prompt_tokens, allow_spawn, model_id, model_provider, messages_json"
)


def _message_to_json(msg: Message) -> dict[str, Any]:
    """Serialise a :class:`Message` to a plain JSON-able dict.

    Full-unification structured transcript serialisation (migration 047). The
    shape mirrors the fields the detail route / front-end ``mapHistoryItems``
    consume (role / text / tool_calls / usage / model / meta / sender / created
    / parent), so one round-trip covers persistence AND display. ``tool_calls``
    / ``tool_results`` cards are stored verbatim (they already carry the render
    shape — id / tool / args / output / status / durationMs / thought_signature
    — so a resume rebuild and a tab render both read the SAME dicts).
    """
    return {
        "id": msg.id.value,
        "role": msg.role.value,
        "text": msg.content.text,
        "created_at": msg.created_at.isoformat(),
        "parent_id": msg.parent_id.value if msg.parent_id is not None else None,
        "tool_calls": [dict(c) for c in msg.tool_calls],
        "tool_results": [dict(r) for r in msg.tool_results],
        "usage": dict(msg.usage) if isinstance(msg.usage, dict) else None,
        "model_id": msg.model_id,
        "model_provider": msg.model_provider,
        "meta": dict(msg.meta) if isinstance(msg.meta, dict) else None,
        "sender_id": msg.sender_id,
    }


def _json_to_message(
    raw: dict[str, Any],
    *,
    session_created_at: datetime | None = None,
) -> Message:
    """Rebuild a :class:`Message` from its :func:`_message_to_json` dict.

    Defensive read (SUBA-RESTORE-MSG-1): a malformed individual message MUST
    NOT cause the message (or, by extension, the whole session) to be silently
    dropped. Two specific recoveries:

    1. ``created_at`` missing / not an ISO string → fall back to
       ``session_created_at`` (the parent session's own ``created_at``, the
       most accurate proxy we have for "when did this message happen") when
       supplied, otherwise to :func:`datetime.now`. Always logs a warning so
       the data-loss-shaped corruption is surfaced (AGENTS.md State-Truth-
       First §3.5: exception paths must be explicit, never silent).
    2. ``text`` missing / null / non-string → type-guard to ``""``; then if
       :class:`MessageContent` rejects the value (it forbids empty / blank
       text by domain invariant — including ``" "``, since ``assert_non_empty``
       rejects ``value.strip() == ""``), fall back to a clearly-labelled
       ``"[empty]"`` sentinel so the message is preserved (with a warning)
       rather than silently dropped by the caller's ``except`` clause. The
       write path always stores a non-empty body; this guard is purely
       belt-and-suspenders for legacy / externally-written rows.
    """
    role_raw = str(raw.get("role") or "assistant")
    try:
        role = MessageRole(role_raw)
    except ValueError:
        role = MessageRole.ASSISTANT
    parent_raw = raw.get("parent_id")
    parent_id = (
        MessageId.of(str(parent_raw))
        if isinstance(parent_raw, str) and parent_raw
        else None
    )
    tool_calls = tuple(
        dict(c) for c in (raw.get("tool_calls") or []) if isinstance(c, dict)
    )
    tool_results = tuple(
        dict(r) for r in (raw.get("tool_results") or []) if isinstance(r, dict)
    )
    usage_raw = raw.get("usage")
    meta_raw = raw.get("meta")
    raw_id = raw.get("id")
    msg_id = (
        MessageId.of(str(raw_id))
        if isinstance(raw_id, str) and raw_id
        else MessageId.of(uuid.uuid4().hex)
    )
    # Type-guard text to a string (preserve the actual value, including ``""``
    # — do NOT munge it). ``MessageContent`` enforces non-empty-after-strip as
    # a domain invariant (``" "`` is also rejected); an empty / blank text
    # below is caught and degraded with a warning instead of crashing the
    # caller.
    _text_raw = raw.get("text")
    _text = _text_raw if isinstance(_text_raw, str) else ""
    try:
        content = MessageContent(text=_text)
    except InvalidMessageContentError:
        # Empty / blank text — preserve the message with a clearly-labelled
        # "[empty]" sentinel rather than letting the caller's ``except``
        # clause drop the whole row (which would shorten the transcript and
        # break the tool_calls parent_id chain). A bare space (``" "``) is
        # NOT viable here: ``MessageContent`` rejects whitespace-only too
        # (``value.strip() == ""``); "[empty]" passes the invariant and is
        # self-describing for a downstream reader.  Surface the corruption
        # via a warning.
        logger.warning(
            "malformed sub-agent message row: empty/blank text "
            "(id=%r, role=%r); preserving message with '[empty]' sentinel "
            "to keep transcript intact",
            raw.get("id"),
            role.value,
        )
        content = MessageContent(text="[empty]")
    # ``created_at`` is the second silent-loss site: a missing or non-ISO
    # value used to bubble ``KeyError`` / ``ValueError`` up to the caller's
    # ``except (TypeError, ValueError, KeyError): continue`` and DROP the
    # message. Recover with the parent session's ``created_at`` (or
    # wall-clock last-resort, tz-aware UTC since ``Message`` rejects naive
    # datetimes) and log a warning so the corruption is visible.
    created_at_raw = raw.get("created_at")
    try:
        if created_at_raw is None:
            raise KeyError("created_at")
        created_at = datetime.fromisoformat(str(created_at_raw))
    except (KeyError, TypeError, ValueError):
        fallback = session_created_at or datetime.now(timezone.utc)
        logger.warning(
            "malformed sub-agent message row: missing or invalid "
            "created_at=%r (id=%r); falling back to %s to keep transcript "
            "intact",
            created_at_raw,
            raw.get("id"),
            "session.created_at" if session_created_at is not None else "datetime.now(utc)",
        )
        created_at = fallback
    return Message(
        id=msg_id,
        role=role,
        content=content,
        created_at=created_at,
        parent_id=parent_id,
        tool_calls=tool_calls,
        tool_results=tool_results,
        usage=dict(usage_raw) if isinstance(usage_raw, dict) else None,
        model_id=(
            str(raw["model_id"]) if raw.get("model_id") is not None else None
        ),
        model_provider=(
            str(raw["model_provider"])
            if raw.get("model_provider") is not None
            else None
        ),
        meta=dict(meta_raw) if isinstance(meta_raw, dict) else None,
        sender_id=(
            str(raw["sender_id"]) if raw.get("sender_id") is not None else None
        ),
    )


class SqliteSubAgentSessionRepository:
    """aiosqlite implementation of :class:`SubAgentSessionRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------
    async def save(self, session: SubAgentSession) -> None:
        """Insert or upsert ``session`` (single-row aggregate, by id).

        Block 4 — optimistic-lock compare-and-swap on the ``version`` column:

        * INSERT a new row (no prior row exists) storing the aggregate's
          current ``version`` — which is ``0`` for a brand-new
          :meth:`SubAgentSession.start`-ed session, and (defensively) the
          carried version for a working copy whose row was concurrently
          deleted, keeping the CAS chain intact;
        * for an existing row, UPDATE only when the stored ``version`` still
          matches the version this aggregate was loaded with, bumping it to
          ``version + 1`` on success. When the guarded UPDATE affects 0 rows
          AND the row still exists, another writer moved the version forward
          first → raise :class:`SubAgentSessionConflictError` instead of
          silently clobbering their turns.

        On success the in-memory aggregate's ``version`` is advanced to match
        the persisted value so a follow-up save by the same writer uses the
        correct expected version.
        """
        common = (
            session.root_conversation_id.value,
            session.parent_message_id.value
            if session.parent_message_id is not None
            else None,
            session.parent_subagent_id.value
            if session.parent_subagent_id is not None
            else None,
            int(session.depth),
            session.subagent_type,
            session.title,
            session.prompt_preview,
            session.status.value,
            session.owner.value,
            int(session.rounds),
            session.updated_at.isoformat(),
        )
        # Tail-appended (§3.1, migration 035): cumulative usage + per-round
        # prompt snapshots. ``None`` → store NULL so an old row / light caller
        # round-trips to the aggregate's ``None`` default unchanged.
        usage_json = (
            json.dumps(session.usage, ensure_ascii=False)
            if session.usage is not None
            else None
        )
        round_snapshots_json = (
            json.dumps(
                # JSON object keys are strings — store the int round nos as
                # their decimal string; the read path coerces back to int.
                {str(k): v for k, v in session.round_snapshots.items()},
                ensure_ascii=False,
            )
            if session.round_snapshots is not None
            else None
        )
        # Tail-appended (§3.1, migration 037): the most-recent round's provider-
        # measured prompt_tokens (replace-last context-occupancy figure). Stored
        # as a bare INTEGER / NULL — NOT JSON. ``None`` → NULL so an old row /
        # never-measured session round-trips to the aggregate's ``None`` default.
        last_prompt_tokens = (
            int(session.last_prompt_tokens)
            if session.last_prompt_tokens is not None
            else None
        )
        # Tail-appended (§3.1, migration 045): whether the sub-agent was granted
        # the ability to spawn its own sub-agents at spawn time. Stored as a
        # bare INTEGER 0/1 (the column is NOT NULL DEFAULT 0). Mirrors the
        # domain bool — the read path coerces back via ``bool(int(...))``.
        allow_spawn = 1 if session.allow_spawn else 0
        # Tail-appended (§3.1, migration 046): the sub-agent's OWN model — the
        # single source of truth for the budget denominator. Stored RAW (any
        # ``local::`` prefix preserved); ``None`` → NULL so an old row / a
        # never-set session round-trips to the aggregate's ``None`` default.
        model_id = (
            str(session.model_id) if session.model_id is not None else None
        )
        model_provider = (
            str(session.model_provider)
            if session.model_provider is not None
            else None
        )
        # The AUTHORITATIVE structured transcript (migration 047) — the
        # sub-agent's turns as Message objects, serialised the SAME way the
        # detail route / front-end consume them. This is the SOLE transcript
        # truth source (SUBAGENT-UNIFY-6: the legacy flat ``wire_messages_json``
        # column was dropped in migration 048). NOT NULL DEFAULT '[]'.
        messages_json = json.dumps(
            [_message_to_json(m) for m in session.messages],
            ensure_ascii=False,
        )
        expected_version = int(session.version)
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = await conn.execute(
                        "SELECT version FROM chat_subagent_session WHERE id = ?",
                        (session.id.value,),
                    )
                    row = await cur.fetchone()
                    await cur.close()
                    if row is None:
                        # Fresh insert — store the aggregate's current version
                        # (0 for a brand-new ``start()``-ed session; a carried
                        # version when a working copy's row was concurrently
                        # deleted, preserving the CAS chain).
                        await conn.execute(
                            "INSERT INTO chat_subagent_session ("
                            "id, root_conversation_id, parent_message_id, "
                            "parent_subagent_id, depth, "
                            "subagent_type, title, prompt_preview, status, "
                            "owner, rounds, created_at, "
                            "updated_at, version, usage_json, "
                            "round_snapshots_json, last_prompt_tokens, "
                            "allow_spawn, model_id, model_provider, "
                            "messages_json) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                            "?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                session.id.value,
                                *common[:-1],
                                session.created_at.isoformat(),
                                common[-1],
                                expected_version,
                                usage_json,
                                round_snapshots_json,
                                last_prompt_tokens,
                                allow_spawn,
                                model_id,
                                model_provider,
                                messages_json,
                            ),
                        )
                        new_version = expected_version
                    else:
                        new_version = expected_version + 1
                        upd = await conn.execute(
                            "UPDATE chat_subagent_session SET "
                            " root_conversation_id=?, parent_message_id=?, "
                            " parent_subagent_id=?, depth=?, "
                            " subagent_type=?, title=?, prompt_preview=?, "
                            " status=?, owner=?, "
                            " rounds=?, updated_at=?, version=?, "
                            " usage_json=?, round_snapshots_json=?, "
                            " last_prompt_tokens=?, allow_spawn=?, "
                            " model_id=?, model_provider=?, messages_json=? "
                            "WHERE id=? AND version=?",
                            (
                                *common,
                                new_version,
                                usage_json,
                                round_snapshots_json,
                                last_prompt_tokens,
                                allow_spawn,
                                model_id,
                                model_provider,
                                messages_json,
                                session.id.value,
                                expected_version,
                            ),
                        )
                        affected = upd.rowcount
                        await upd.close()
                        if affected == 0:
                            await conn.rollback()
                            raise SubAgentSessionConflictError(
                                session.id.value,
                                expected_version=expected_version,
                            )
                    await conn.commit()
                except SubAgentSessionConflictError:
                    raise
                except Exception:
                    await conn.rollback()
                    raise
        except SubAgentSessionConflictError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.sub_agent_session.save_failed",
                f"failed to save sub-agent session {session.id.value!r}: {exc}",
                operation="sub_agent_session.save",
                cause=exc,
            ) from exc
        # Advance the in-memory aggregate so a subsequent save by the same
        # writer uses the correct expected version (CAS chain).
        session.version = new_version

    async def delete(self, session_id: SubAgentSessionId) -> None:
        """Remove the session; raise if missing."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_subagent_session WHERE id = ?",
                    (session_id.value,),
                )
                rows_affected = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.sub_agent_session.delete_failed",
                f"failed to delete sub-agent session {session_id.value!r}: {exc}",
                operation="sub_agent_session.delete",
                cause=exc,
            ) from exc
        if rows_affected == 0:
            raise SubAgentSessionNotFoundError(session_id.value)

    async def delete_by_parent(
        self,
        root_conversation_id: ConversationId,
    ) -> int:
        """Cascade-delete every sub-agent session under a root conversation.

        Returns the number of rows removed. The parameter is the ROOT
        (top-of-tree) conversation id — the column all sub-agent rows share
        regardless of depth (migration 049 renamed ``parent_conversation_id``
        to ``root_conversation_id`` so the name matches this always-was-root
        semantic).
        """
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_subagent_session "
                    "WHERE root_conversation_id = ?",
                    (root_conversation_id.value,),
                )
                rows_affected = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.sub_agent_session.delete_by_parent_failed",
                "failed to delete sub-agent sessions for root "
                f"{root_conversation_id.value!r}: {exc}",
                operation="sub_agent_session.delete_by_parent",
                cause=exc,
            ) from exc
        return rows_affected if isinstance(rows_affected, int) and rows_affected > 0 else 0

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------
    async def find(
        self,
        session_id: SubAgentSessionId,
    ) -> SubAgentSession | None:
        """Return the aggregate or ``None`` if not present."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_subagent_session "
                    "WHERE id = ?",
                    (session_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.sub_agent_session.find_failed",
                f"failed to load sub-agent session {session_id.value!r}: {exc}",
                operation="sub_agent_session.find",
                cause=exc,
            ) from exc
        if row is None:
            return None
        return self._row_to_session(row)

    async def get(self, session_id: SubAgentSessionId) -> SubAgentSession:
        """Return the aggregate; raise :class:`SubAgentSessionNotFoundError`."""
        session = await self.find(session_id)
        if session is None:
            raise SubAgentSessionNotFoundError(session_id.value)
        return session

    async def list_by_root_conversation(
        self,
        root_conversation_id: ConversationId,
    ) -> tuple[SubAgentSession, ...]:
        """Return every sub-agent session under a ROOT conversation.

        Ordered ``created_at`` ASC. Returns the FULL sub-agent forest (every
        depth — depth 1 first-level, depth 2 grand, depth 3 great-grand, …
        mixed together). Use :meth:`list_by_parent_subagent` to walk one
        specific direct-parent's children (one level of the tree).
        """
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_subagent_session "
                    "WHERE root_conversation_id = ? "
                    "ORDER BY created_at ASC",
                    (root_conversation_id.value,),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.sub_agent_session.list_by_root_conversation_failed",
                "failed to list sub-agent sessions for root "
                f"{root_conversation_id.value!r}: {exc}",
                operation="sub_agent_session.list_by_root_conversation",
                cause=exc,
            ) from exc
        return tuple(self._row_to_session(r) for r in rows)

    async def list_by_parent_subagent(
        self,
        parent_subagent_id: SubAgentSessionId,
    ) -> tuple[SubAgentSession, ...]:
        """Return the direct children of one sub-agent (created_at ASC).

        Walks one level of the sub-agent tree — every sub-agent row whose
        ``parent_subagent_id`` equals the given id. Returns an empty tuple when
        the parent has no children (walking a leaf node yields nothing, not an
        error).
        """
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_subagent_session "
                    "WHERE parent_subagent_id = ? "
                    "ORDER BY created_at ASC",
                    (parent_subagent_id.value,),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.sub_agent_session.list_by_parent_subagent_failed",
                "failed to list direct children of sub-agent "
                f"{parent_subagent_id.value!r}: {exc}",
                operation="sub_agent_session.list_by_parent_subagent",
                cause=exc,
            ) from exc
        return tuple(self._row_to_session(r) for r in rows)

    # ------------------------------------------------------------------
    # Row -> domain
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_session(row: tuple[object, ...]) -> SubAgentSession:
        parent_message_id_raw = row[2]
        parent_message_id = (
            MessageId.of(str(parent_message_id_raw))
            if parent_message_id_raw is not None
            else None
        )
        # Tree edges (migration 049). Indices 3/4 in the new ``_COLUMNS``
        # projection. ``parent_subagent_id`` is NULL for depth-1 rows (direct
        # parent is the main agent); ``depth`` defaults to 1 both at the row
        # level (NOT NULL DEFAULT 1 in SQL) and at the aggregate level.
        parent_subagent_id_raw = row[3]
        parent_subagent_id = (
            SubAgentSessionId.of(str(parent_subagent_id_raw))
            if parent_subagent_id_raw is not None
            else None
        )
        try:
            depth = int(row[4]) if row[4] is not None else 1  # type: ignore[arg-type]
        except (TypeError, ValueError):
            depth = 1
        if depth < 1:
            depth = 1
        # SUBAGENT-UNIFY-6: the legacy ``wire_messages_json`` column was dropped
        # (migration 048); migration 049 further renames the column layout so
        # every tail index shifts by +2 (parent_subagent_id + depth were inserted
        # between parent_message_id and subagent_type). Tail-appended columns
        # keep their short-projection guards so a row read before a column
        # existed (or a NULL value) maps to the aggregate's default — full
        # backward compat with rows written by earlier schema versions.
        usage: dict[str, Any] | None = None
        if len(row) > 14 and row[14] is not None:
            try:
                parsed_usage = json.loads(str(row[14]))
                if isinstance(parsed_usage, dict):
                    usage = parsed_usage
            except (TypeError, ValueError):
                usage = None
        round_snapshots: dict[int, list[dict[str, Any]]] | None = None
        if len(row) > 15 and row[15] is not None:
            try:
                parsed_snaps = json.loads(str(row[15]))
            except (TypeError, ValueError):
                parsed_snaps = None
            if isinstance(parsed_snaps, dict):
                coerced: dict[int, list[dict[str, Any]]] = {}
                for k, v in parsed_snaps.items():
                    try:
                        rk = int(k)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(v, list):
                        coerced[rk] = [e for e in v if isinstance(e, dict)]
                round_snapshots = coerced or None
        # ``last_prompt_tokens`` (migration 037). Index 16.
        last_prompt_tokens: int | None = None
        if len(row) > 16 and row[16] is not None:
            try:
                last_prompt_tokens = int(row[16])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                last_prompt_tokens = None
        # ``allow_spawn`` (migration 045). Index 17. Stored INTEGER 0/1.
        allow_spawn = False
        if len(row) > 17 and row[17] is not None:
            try:
                allow_spawn = bool(int(row[17]))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                allow_spawn = False
        # ``model_id`` / ``model_provider`` (migration 046). Indices 18/19.
        # Stored RAW (any ``local::`` prefix preserved).
        model_id: str | None = None
        if len(row) > 18 and row[18] is not None:
            model_id = str(row[18])
        model_provider: str | None = None
        if len(row) > 19 and row[19] is not None:
            model_provider = str(row[19])
        # The AUTHORITATIVE structured transcript (migration 047) — the SOLE
        # transcript truth source. Index 20 matches ``messages_json``'s tail
        # position in the new ``_COLUMNS``. A NULL/invalid value maps to an
        # empty list.
        # Pre-parse the session's own ``created_at`` so a malformed per-
        # message ``created_at`` can fall back to it (SUBA-RESTORE-MSG-1)
        # instead of dropping the row.
        session_created_at = datetime.fromisoformat(str(row[11]))
        messages: list[Message] = []
        if len(row) > 20 and row[20] is not None:
            try:
                parsed_msgs = json.loads(str(row[20]))
            except (TypeError, ValueError):
                parsed_msgs = None
            if isinstance(parsed_msgs, list):
                for entry in parsed_msgs:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        messages.append(
                            _json_to_message(
                                entry,
                                session_created_at=session_created_at,
                            )
                        )
                    except (TypeError, ValueError, KeyError):
                        # A single malformed row never breaks the whole load.
                        continue
        return SubAgentSession(
            id=SubAgentSessionId.of(str(row[0])),
            root_conversation_id=ConversationId.of(str(row[1])),
            parent_message_id=parent_message_id,
            parent_subagent_id=parent_subagent_id,
            depth=depth,
            subagent_type=str(row[5]),
            title=str(row[6]) if row[6] is not None else "",
            prompt_preview=str(row[7]) if row[7] is not None else "",
            status=SubAgentSessionStatus(str(row[8])),
            owner=SubAgentOwner(str(row[9])),
            rounds=int(row[10]),
            created_at=session_created_at,
            updated_at=datetime.fromisoformat(str(row[12])),
            version=int(row[13]) if len(row) > 13 and row[13] is not None else 0,
            usage=usage,
            round_snapshots=round_snapshots,
            last_prompt_tokens=last_prompt_tokens,
            allow_spawn=allow_spawn,
            model_id=model_id,
            model_provider=model_provider,
            messages=messages,
        )
