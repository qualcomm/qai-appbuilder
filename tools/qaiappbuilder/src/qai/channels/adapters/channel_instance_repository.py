# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`ChannelInstanceRepositoryPort` (PR-047).

Schema reference: ``qai-db-schema.md`` §5.1 (channels_instance).
Replaces the legacy ``data/wechat_creds.json`` 270-byte plaintext file
plus per-process ``_running_channels`` list with an indexed relational
store.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.channels.domain import (
    ChannelInstance,
    ChannelInstanceId,
    ChannelInstanceNotFoundError,
    ChannelKind,
    ChannelStatus,
    CredentialsRef,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteChannelInstanceRepository"]


_INSERT_OR_REPLACE_SQL = (
    "INSERT INTO channels_instance "
    "(id, kind, name, status, credentials_service, credentials_key, "
    "last_error, metadata_json, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "kind=excluded.kind, "
    "name=excluded.name, "
    "status=excluded.status, "
    "credentials_service=excluded.credentials_service, "
    "credentials_key=excluded.credentials_key, "
    "last_error=excluded.last_error, "
    "metadata_json=excluded.metadata_json, "
    "created_at=excluded.created_at, "
    "updated_at=excluded.updated_at"
)


_SELECT_COLS = (
    "id, kind, name, status, credentials_service, credentials_key, "
    "last_error, metadata_json, created_at, updated_at"
)


class SqliteChannelInstanceRepository:
    """aiosqlite implementation of :class:`ChannelInstanceRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def save(self, instance: ChannelInstance) -> None:
        params = (
            instance.instance_id.value,
            instance.kind.value,
            instance.name,
            instance.status.value,
            instance.credentials_ref.service,
            instance.credentials_ref.key,
            instance.last_error,
            json.dumps(list(instance.metadata)),
            instance.created_at.isoformat(),
            instance.updated_at.isoformat(),
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
                "channels.instance.save_failed",
                f"failed to save instance "
                f"{instance.instance_id.value!r}: {exc}",
                operation="channels.instance.save",
                cause=exc,
            ) from exc

    async def get(self, instance_id: ChannelInstanceId) -> ChannelInstance:
        result = await self.find(instance_id)
        if result is None:
            raise ChannelInstanceNotFoundError(instance_id.value)
        return result

    async def find(
        self, instance_id: ChannelInstanceId
    ) -> ChannelInstance | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SELECT_COLS} FROM channels_instance "
                    "WHERE id = ?",
                    (instance_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.instance.find_failed",
                f"failed to find instance {instance_id.value!r}: {exc}",
                operation="channels.instance.find",
                cause=exc,
            ) from exc
        return None if row is None else self._row_to_instance(row)

    async def list_by_kind(
        self, kind: ChannelKind
    ) -> tuple[ChannelInstance, ...]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_SELECT_COLS} FROM channels_instance "
                    "WHERE kind = ? ORDER BY created_at ASC",
                    (kind.value,),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.instance.list_failed",
                f"failed to list instances for kind {kind.value!r}: {exc}",
                operation="channels.instance.list_by_kind",
                cause=exc,
            ) from exc
        return tuple(self._row_to_instance(r) for r in rows)

    async def delete(self, instance_id: ChannelInstanceId) -> None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM channels_instance WHERE id = ?",
                    (instance_id.value,),
                )
                deleted = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.instance.delete_failed",
                f"failed to delete instance {instance_id.value!r}: {exc}",
                operation="channels.instance.delete",
                cause=exc,
            ) from exc
        if not deleted:
            raise ChannelInstanceNotFoundError(instance_id.value)

    @staticmethod
    def _row_to_instance(row: tuple[object, ...]) -> ChannelInstance:
        metadata_raw = row[7]
        if metadata_raw in (None, "", "{}"):
            metadata: tuple[tuple[str, str], ...] = ()
        else:
            decoded = json.loads(str(metadata_raw))
            if isinstance(decoded, dict):
                # Legacy single-write path used dict; defensive normalise.
                metadata = tuple((str(k), str(v)) for k, v in decoded.items())
            else:
                metadata = tuple(
                    (str(k), str(v)) for k, v in decoded
                )
        return ChannelInstance(
            instance_id=ChannelInstanceId(value=str(row[0])),
            kind=ChannelKind(str(row[1])),
            name=str(row[2]),
            status=ChannelStatus(str(row[3])),
            credentials_ref=CredentialsRef(
                service=str(row[4]), key=str(row[5])
            ),
            last_error=str(row[6]),
            metadata=metadata,
            created_at=datetime.fromisoformat(str(row[8])),
            updated_at=datetime.fromisoformat(str(row[9])),
        )
