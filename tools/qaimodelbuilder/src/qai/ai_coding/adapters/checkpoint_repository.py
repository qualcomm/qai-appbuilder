# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""kv_user_prefs-backed :class:`CheckpointRepositoryPort` adapter (PR-105).

Stores per-session checkpoint snapshots in the shared
``kv_user_prefs`` table (migration 007) under per-session keys
``ai_coding.checkpoints.<session_id>``.  Each row holds the full
list of checkpoints for that session as a JSON array; the array is
small (typically <100 entries) so atomic read-modify-write is
acceptable.

Why kv_user_prefs?
------------------
PR-105 ships a *minimal* checkpoint surface so the WebUI can
exercise the round-trip without forcing a schema migration (which
would be I1 lane territory).  The current implementation persists
checkpoints as a JSON array under per-session
``ai_coding.checkpoints.<session_id>`` keys in the shared
``kv_user_prefs`` table; the port surface is shape-stable so a
different storage backend (e.g. a delegating adapter that hands off
to OpenCode-CLI's native ``revert`` API) is a drop-in replacement
without touching call sites.

Concurrency
-----------
``BEGIN IMMEDIATE`` serialises writers within a single process; the
legacy WebUI never had concurrent checkpoint creates so this is
sufficient.  Cross-process locking is delegated to the platform
:class:`Database` connection.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.ai_coding.application.ports import (
    CheckpointRecord,
    CheckpointRepositoryPort,
)
from qai.ai_coding.domain import CodingSessionId
from qai.platform.errors import PersistenceError, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = [
    "CHECKPOINT_KEY_PREFIX",
    "KvCheckpointRepository",
]


#: Per-session kv key prefix.  The full key is
#: ``ai_coding.checkpoints.<session_id>``.
CHECKPOINT_KEY_PREFIX = "ai_coding.checkpoints."


def _key_for(session_id: CodingSessionId) -> str:
    return f"{CHECKPOINT_KEY_PREFIX}{session_id.value}"


class KvCheckpointRepository(CheckpointRepositoryPort):
    """``CheckpointRepositoryPort`` impl backed by ``kv_user_prefs``."""

    __slots__ = ("_db",)

    def __init__(self, *, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        session_id: CodingSessionId,
        snapshot: dict[str, Any],
        label: str | None = None,
    ) -> CheckpointRecord:
        checkpoint_id = f"cp-{secrets.token_hex(8)}"
        created_at = datetime.now(timezone.utc).isoformat()
        record_dict = {
            "checkpoint_id": checkpoint_id,
            "created_at": created_at,
            "label": label,
            "snapshot": snapshot,
        }

        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = await conn.execute(
                        "SELECT value_json FROM kv_user_prefs WHERE key = ?",
                        (_key_for(session_id),),
                    )
                    row = await cur.fetchone()
                    await cur.close()
                    items: list[dict[str, Any]] = []
                    if row is not None and row[0]:
                        try:
                            loaded = json.loads(str(row[0]))
                            if isinstance(loaded, list):
                                items = [
                                    e for e in loaded if isinstance(e, dict)
                                ]
                        except json.JSONDecodeError:
                            items = []
                    items.append(record_dict)
                    payload = json.dumps(items, sort_keys=True)
                    now = datetime.now(timezone.utc).isoformat()
                    await conn.execute(
                        "INSERT INTO kv_user_prefs (key, value_json, "
                        "updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "value_json=excluded.value_json, "
                        "updated_at=excluded.updated_at",
                        (_key_for(session_id), payload, now),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:
            raise PersistenceError(
                "ai_coding.checkpoint.save_failed",
                f"failed to persist checkpoint: {exc}",
                operation="checkpoint.save",
                cause=exc,
            ) from exc

        return CheckpointRecord(
            checkpoint_id=checkpoint_id,
            created_at=created_at,
            label=label,
            snapshot=snapshot,
        )

    async def list_for_session(
        self, session_id: CodingSessionId
    ) -> list[CheckpointRecord]:
        items = await self._read_items(session_id)
        return [_dict_to_record(d) for d in items]

    async def get(
        self,
        *,
        session_id: CodingSessionId,
        checkpoint_id: str,
    ) -> CheckpointRecord:
        items = await self._read_items(session_id)
        for d in items:
            if d.get("checkpoint_id") == checkpoint_id:
                return _dict_to_record(d)
        raise ValidationError(
            code="ai_coding.checkpoint_not_found",
            message=(
                f"checkpoint {checkpoint_id!r} not found for session "
                f"{session_id}"
            ),
            field_errors={"checkpoint_id": [checkpoint_id]},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _read_items(
        self, session_id: CodingSessionId
    ) -> list[dict[str, Any]]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT value_json FROM kv_user_prefs WHERE key = ?",
                    (_key_for(session_id),),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None or not row[0]:
                    return []
                try:
                    loaded = json.loads(str(row[0]))
                except json.JSONDecodeError:
                    return []
                if not isinstance(loaded, list):
                    return []
                return [e for e in loaded if isinstance(e, dict)]
        except Exception as exc:
            raise PersistenceError(
                "ai_coding.checkpoint.load_failed",
                f"failed to load checkpoints: {exc}",
                operation="checkpoint.load",
                cause=exc,
            ) from exc


def _dict_to_record(d: dict[str, Any]) -> CheckpointRecord:
    snapshot = d.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    return CheckpointRecord(
        checkpoint_id=str(d.get("checkpoint_id", "")),
        created_at=str(d.get("created_at", "")),
        label=d.get("label") if isinstance(d.get("label"), str) else None,
        snapshot=snapshot,
    )
