# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`CodingConfigRepositoryPort` (PR-104b).

Stores the ai_coding UI config document in the shared
``kv_user_prefs`` table (migration 007) under the key
``ai_coding.config``.  A single row holds the full document as JSON;
:meth:`save` performs a transactional read-modify-write with a
shallow merge of the incoming ``updates`` dict over the persisted
document.

Schema reference: ``qai-db-schema.md`` §6.X.2 / §10.4 (kv_user_prefs).

No sensitive values
-------------------
The document MUST NOT contain API keys / tokens / passwords.  The
credential surface (``GET / POST / DELETE /api/cc/credentials``)
goes through :class:`qai.platform.persistence.secrets.SecretStore`
in a separate use case, never through this repository.  Route-layer
DTOs validate the inbound keys against a whitelist (PR-104b
``SaveConfigRequest``) so a malicious caller cannot smuggle a key
through the document.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = [
    "AI_CODING_CONFIG_KEY",
    "AI_CODING_OC_CONFIG_KEY",
    "KvCodingConfigRepository",
]


#: Stable key under which the ai_coding (CC) config document lives in
#: the shared ``kv_user_prefs`` table.  Other contexts following the
#: same pattern should pick a parallel ``<ctx>.config`` key.
AI_CODING_CONFIG_KEY = "ai_coding.config"


#: PR-105: stable key under which the OpenCode-side config document
#: lives.  Distinct from :data:`AI_CODING_CONFIG_KEY` so the two
#: provider configurations cannot stomp on each other (a CC-side save
#: never touches the OC document and vice versa).
AI_CODING_OC_CONFIG_KEY = "ai_coding.oc.config"


class KvCodingConfigRepository:
    """``CodingConfigRepositoryPort`` impl backed by ``kv_user_prefs``.

    PR-105: ``kv_key`` is now a constructor argument (default keeps
    PR-104b's ``ai_coding.config`` behaviour).  An OC instance built
    with ``kv_key=AI_CODING_OC_CONFIG_KEY`` reuses the same code path
    against the OC document.
    """

    __slots__ = ("_db", "_kv_key")

    def __init__(
        self,
        *,
        db: Database,
        kv_key: str = AI_CODING_CONFIG_KEY,
    ) -> None:
        self._db = db
        self._kv_key = kv_key

    async def load(self) -> dict[str, Any]:
        """Return the persisted config document or ``{}`` if absent."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT value_json FROM kv_user_prefs WHERE key = ?",
                    (self._kv_key,),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    return {}
                raw = row[0]
                if not raw:
                    return {}
                doc = json.loads(str(raw))
                if not isinstance(doc, dict):
                    # Defensive: legacy or hand-edited rows may carry a
                    # non-object payload — treat as empty.
                    return {}
                return doc
        except Exception as exc:
            raise PersistenceError(
                "ai_coding.config.load_failed",
                f"failed to load coding config: {exc}",
                operation="coding_config.load",
                cause=exc,
            ) from exc

    async def save(self, *, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge ``updates`` into the persisted doc; return the result."""
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = await conn.execute(
                        "SELECT value_json FROM kv_user_prefs WHERE key = ?",
                        (self._kv_key,),
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

                    # Shallow merge: top-level keys in ``updates``
                    # overwrite; missing keys are preserved.  Mirrors
                    # the legacy ``forge_config_manager.update`` shape.
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
                        (self._kv_key, payload, now),
                    )
                    await conn.commit()
                    return merged
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:
            raise PersistenceError(
                "ai_coding.config.save_failed",
                f"failed to save coding config: {exc}",
                operation="coding_config.save",
                cause=exc,
            ) from exc
