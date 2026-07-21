# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``KvUserPrefsRepository`` — aiosqlite-backed user-prefs adapter.

Stores every user-prefs document in the shared ``kv_user_prefs``
table (migration 007) keyed by :class:`PrefsKey`.  One row per key;
:meth:`save` performs a transactional read-modify-write with a
shallow merge (top-level keys in ``updates`` overwrite, everything
else preserved).

Schema reference: ``qai-db-schema.md`` §6.X.2 / §10.4 (kv_user_prefs).

Why share the table with ai_coding / model_catalog?
---------------------------------------------------
Migration 007 was created in S6 specifically to host all per-user
JSON preferences under one transactional store; ai_coding (PR-104b)
and model_catalog (PR-044) already use it for their own keys.
user_prefs adopts the same pattern — different keys, same row
shape — so we don't fork the infrastructure for what is functionally
the same operation.

No sensitive values
-------------------
The persisted documents MUST NOT carry API keys / tokens / passwords.
Credentials live in :class:`qai.platform.persistence.secrets.SecretStore`
(OS keyring + Fernet fallback, AGENTS §3.3).  The route layer
validates inbound request bodies against allow-lists so a malicious
caller cannot smuggle a credential value through a preference doc.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.platform.errors import PersistenceError
from qai.user_prefs.domain import PrefsDocument, PrefsKey, coerce_document

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["KvUserPrefsRepository"]


class KvUserPrefsRepository:
    """Persist user_prefs documents to the shared ``kv_user_prefs`` row store."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def load(self, key: PrefsKey) -> PrefsDocument:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT value_json FROM kv_user_prefs WHERE key = ?",
                    (key.value,),
                )
                row = await cur.fetchone()
                await cur.close()
            if row is None:
                return {}
            raw = row[0]
            if not raw:
                return {}
            return coerce_document(json.loads(str(raw)))
        except Exception as exc:
            raise PersistenceError(
                "user_prefs.kv.load_failed",
                f"failed to load user_prefs document {key.value!r}: {exc}",
                operation="user_prefs.load",
                cause=exc,
            ) from exc

    async def save(
        self,
        key: PrefsKey,
        *,
        updates: PrefsDocument,
    ) -> PrefsDocument:
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = await conn.execute(
                        "SELECT value_json FROM kv_user_prefs WHERE key = ?",
                        (key.value,),
                    )
                    row = await cur.fetchone()
                    await cur.close()
                    current: dict[str, Any] = {}
                    if row is not None and row[0]:
                        try:
                            loaded = json.loads(str(row[0]))
                            if isinstance(loaded, dict):
                                current = loaded
                        except json.JSONDecodeError:
                            current = {}

                    merged = dict(current)
                    for k, v in updates.items():
                        merged[k] = v

                    payload = json.dumps(merged, sort_keys=True)
                    now = datetime.now(timezone.utc).isoformat()
                    await conn.execute(
                        "INSERT INTO kv_user_prefs (key, value_json, "
                        "updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "value_json=excluded.value_json, "
                        "updated_at=excluded.updated_at",
                        (key.value, payload, now),
                    )
                    await conn.commit()
                    return merged
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:
            raise PersistenceError(
                "user_prefs.kv.save_failed",
                f"failed to save user_prefs document {key.value!r}: {exc}",
                operation="user_prefs.save",
                cause=exc,
            ) from exc
