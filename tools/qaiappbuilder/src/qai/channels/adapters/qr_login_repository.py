# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed QR login challenge persistence (PR-047).

Schema reference: ``qai-db-schema.md`` §5.4 (channels_qr_login_challenge).

The repository is **internal** to the channels QR login adapters —
:class:`QrLoginPort` itself does not expose storage; PR-024's domain
``QrLoginChallenge`` VO is the persistence shape.  Each provider
adapter (:class:`WechatQrLogin`) holds a reference to this repository
and stores / fetches challenges through it.

Feishu has no QR login flow (gated at the route layer) so its adapter
does not exercise this repository.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.channels.domain import (
    QrLoginChallenge,
    QrLoginChallengeNotFoundError,
    QrLoginStatus,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteQrLoginChallengeRepository"]


_UPSERT_SQL = (
    "INSERT INTO channels_qr_login_challenge "
    "(id, instance_id, status, issued_at, expires_at, qr_url) "
    "VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "instance_id=excluded.instance_id, "
    "status=excluded.status, "
    "issued_at=excluded.issued_at, "
    "expires_at=excluded.expires_at, "
    "qr_url=excluded.qr_url"
)


class SqliteQrLoginChallengeRepository:
    """Persistence helper for :class:`QrLoginChallenge` aggregates."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def upsert(
        self, challenge: QrLoginChallenge, *, instance_id: str
    ) -> None:
        params = (
            challenge.challenge_id,
            instance_id,
            challenge.status.value,
            challenge.issued_at.isoformat(),
            challenge.expires_at.isoformat(),
            challenge.qr_url,
        )
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(_UPSERT_SQL, params)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.qr.upsert_failed",
                f"failed to upsert qr challenge "
                f"{challenge.challenge_id!r}: {exc}",
                operation="channels.qr.upsert",
                cause=exc,
            ) from exc

    async def find(self, challenge_id: str) -> QrLoginChallenge | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, instance_id, status, issued_at, "
                    "expires_at, qr_url FROM channels_qr_login_challenge "
                    "WHERE id = ?",
                    (challenge_id,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "channels.qr.find_failed",
                f"failed to find qr challenge {challenge_id!r}: {exc}",
                operation="channels.qr.find",
                cause=exc,
            ) from exc
        if row is None:
            return None
        return QrLoginChallenge(
            challenge_id=str(row[0]),
            instance_id_value=str(row[1]),
            status=QrLoginStatus(str(row[2])),
            issued_at=datetime.fromisoformat(str(row[3])),
            expires_at=datetime.fromisoformat(str(row[4])),
            qr_url=str(row[5]) if row[5] is not None else None,
        )

    async def get(self, challenge_id: str) -> QrLoginChallenge:
        result = await self.find(challenge_id)
        if result is None:
            raise QrLoginChallengeNotFoundError(challenge_id)
        return result
