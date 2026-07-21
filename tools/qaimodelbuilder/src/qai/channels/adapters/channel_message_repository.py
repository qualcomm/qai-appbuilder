# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`ChannelMessageRepositoryPort` (PR-047).

Schema reference: ``qai-db-schema.md`` §5.2 (channels_message).
The ``UNIQUE(kind, provider_event_id)`` constraint is the idempotency
key for :class:`IngestWebhookUseCase`; this adapter relies on it.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.channels.domain import (
    ChannelInstanceId,
    ChannelKind,
    ChannelMessage,
    ChannelMessageId,
    ChannelMessageNotFoundError,
    ChannelMessageStatus,
    ChannelUserId,
    Command,
    MessageContent,
    MessageReplyRef,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteChannelMessageRepository"]


_INSERT_OR_REPLACE_SQL = (
    "INSERT INTO channels_message "
    "(id, instance_id, kind, sender_user_id, provider_event_id, "
    "content_text, status, parsed_verb, parsed_args_json, "
    "reply_provider_message_id, failure_reason, arrived_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "instance_id=excluded.instance_id, "
    "kind=excluded.kind, "
    "sender_user_id=excluded.sender_user_id, "
    "provider_event_id=excluded.provider_event_id, "
    "content_text=excluded.content_text, "
    "status=excluded.status, "
    "parsed_verb=excluded.parsed_verb, "
    "parsed_args_json=excluded.parsed_args_json, "
    "reply_provider_message_id=excluded.reply_provider_message_id, "
    "failure_reason=excluded.failure_reason, "
    "arrived_at=excluded.arrived_at, "
    "updated_at=excluded.updated_at"
)

_SELECT_COLS = (
    "id, instance_id, kind, sender_user_id, provider_event_id, "
    "content_text, status, parsed_verb, parsed_args_json, "
    "reply_provider_message_id, failure_reason, arrived_at, updated_at"
)


class SqliteChannelMessageRepository:
    """aiosqlite implementation of :class:`ChannelMessageRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def save(self, message: ChannelMessage) -> None:
        cmd = message.parsed_command
        ref = message.reply_ref
        params = (
            message.message_id.value,
            message.instance_id.value,
            message.kind.value,
            message.sender.value,
            message.provider_event_id,
            message.content.text,
            message.status.value,
            cmd.verb if cmd is not None else None,
            json.dumps(list(cmd.args)) if cmd is not None else None,
            ref.outbound_provider_message_id if ref is not None else None,
            message.failure_reason,
            message.arrived_at.isoformat(),
            message.updated_at.isoformat(),
        )
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(_INSERT_OR_REPLACE_SQL, params)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.message.save_failed",
                f"failed to save message {message.message_id.value!r}: {exc}",
                operation="channels.message.save",
                cause=exc,
            ) from exc

    async def get(self, message_id: ChannelMessageId) -> ChannelMessage:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SELECT_COLS} FROM channels_message "
                    "WHERE id = ?",
                    (message_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.message.get_failed",
                f"failed to load message {message_id.value!r}: {exc}",
                operation="channels.message.get",
                cause=exc,
            ) from exc
        if row is None:
            raise ChannelMessageNotFoundError(message_id.value)
        return self._row_to_message(row)

    async def find_by_provider_event_id(
        self, kind: ChannelKind, provider_event_id: str
    ) -> ChannelMessage | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SELECT_COLS} FROM channels_message "
                    "WHERE kind = ? AND provider_event_id = ?",
                    (kind.value, provider_event_id),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.message.find_by_event_failed",
                f"failed to find message for kind={kind.value!r} "
                f"event={provider_event_id!r}: {exc}",
                operation="channels.message.find_by_provider_event_id",
                cause=exc,
            ) from exc
        return None if row is None else self._row_to_message(row)

    @staticmethod
    def _row_to_message(row: tuple[object, ...]) -> ChannelMessage:
        message_id_v = str(row[0])
        verb = row[7]
        args_raw = row[8]
        outbound = row[9]
        if verb is not None:
            args_tuple: tuple[str, ...]
            if args_raw is None or args_raw == "":
                args_tuple = ()
            else:
                decoded = json.loads(str(args_raw))
                args_tuple = tuple(str(a) for a in decoded)
            cmd: Command | None = Command(verb=str(verb), args=args_tuple)
        else:
            cmd = None
        msg_id = ChannelMessageId(value=message_id_v)
        if outbound is not None:
            reply_ref: MessageReplyRef | None = MessageReplyRef(
                inbound_message_id=msg_id,
                outbound_provider_message_id=str(outbound),
            )
        else:
            reply_ref = None
        return ChannelMessage(
            message_id=msg_id,
            instance_id=ChannelInstanceId(value=str(row[1])),
            kind=ChannelKind(str(row[2])),
            sender=ChannelUserId(value=str(row[3])),
            provider_event_id=str(row[4]),
            content=MessageContent(text=str(row[5])),
            status=ChannelMessageStatus(str(row[6])),
            parsed_command=cmd,
            reply_ref=reply_ref,
            failure_reason=str(row[10]),
            arrived_at=datetime.fromisoformat(str(row[11])),
            updated_at=datetime.fromisoformat(str(row[12])),
        )
