# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`ProviderRegistryPort` (PR-044).

Backed by the ``kv_user_prefs`` shared key-value table (schema doc
§6.X.2 / §10.4) under the dedicated key prefix ``model_catalog.provider.``.

Why ``kv_user_prefs`` instead of a context-local table?
-------------------------------------------------------

Per ``qai-db-schema.md`` §10.4, provider configs are exactly the kind of
"frequently-mutated UI prefs" the cross-context preference store was
created for. A bespoke table would buy us nothing (no relational
queries, no joins, no per-row SQL CHECKs that match the JSON shape) and
would couple a context-isolated migration to fields that may evolve
independently per provider. The KV blob is canonicalised through
``json.dumps(... sort_keys=True)`` so identical configs produce
identical strings, and the ``provider_id`` is validated at the
application/domain layer (see :class:`ProviderConfigInvalidError`) —
adapters do **not** rewrite domain validation.

Cross-PR note (channels): channels (PR-047) will introduce its own
secret-store-backed credentials resolver under a different KV key
namespace; placing this adapter under the ``model_catalog.provider.``
prefix keeps the two completely orthogonal.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from qai.platform.errors import PersistenceError
from qai.platform.time import Clock

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteProviderRegistry"]


_KEY_PREFIX = "model_catalog.provider."


class SqliteProviderRegistry:
    """KV-backed :class:`ProviderRegistryPort`.

    Persistence shape per row::

        kv_user_prefs.key        = "model_catalog.provider.<provider_id>"
        kv_user_prefs.value_json = json.dumps(config, sort_keys=True)
        kv_user_prefs.updated_at = clock.now().isoformat()
    """

    __slots__ = ("_db", "_clock")

    def __init__(self, *, db: "Database", clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def list_provider_configs(self) -> list[dict[str, Any]]:
        prefix = _KEY_PREFIX
        like_pattern = prefix + "%"
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT key, value_json FROM kv_user_prefs "
                    "WHERE key LIKE ? "
                    "ORDER BY key ASC",
                    (like_pattern,),
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.provider.list_failed",
                f"failed to list provider configs: {exc}",
                operation="provider.list",
                cause=exc,
            ) from exc
        result: list[dict[str, Any]] = []
        for row in rows:
            key = str(row[0])
            provider_id = key[len(prefix):]
            config = self._decode_config(provider_id, str(row[1]))
            result.append({"provider_id": provider_id, "config": config})
        return result

    async def get_provider_config(
        self, provider_id: str
    ) -> dict[str, Any] | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT value_json FROM kv_user_prefs WHERE key = ?",
                    (_KEY_PREFIX + provider_id,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.provider.get_failed",
                f"failed to load provider {provider_id!r}: {exc}",
                operation="provider.get",
                cause=exc,
            ) from exc
        if row is None:
            return None
        return self._decode_config(provider_id, str(row[0]))

    async def save_provider_config(
        self, provider_id: str, config: dict[str, Any]
    ) -> None:
        key = _KEY_PREFIX + provider_id
        value_json = json.dumps(
            dict(config), sort_keys=True, separators=(",", ":")
        )
        updated_at_iso = self._clock.now().isoformat()
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(
                        "INSERT INTO kv_user_prefs "
                        "(key, value_json, updated_at) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "value_json=excluded.value_json, "
                        "updated_at=excluded.updated_at",
                        (key, value_json, updated_at_iso),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.provider.save_failed",
                f"failed to save provider {provider_id!r}: {exc}",
                operation="provider.save",
                cause=exc,
            ) from exc

    @staticmethod
    def _decode_config(provider_id: str, value_text: str) -> dict[str, Any]:
        try:
            decoded = json.loads(value_text)
        except json.JSONDecodeError as exc:  # pragma: no cover
            raise PersistenceError(
                "model_catalog.provider.config_corrupt",
                f"provider {provider_id!r} value_json is not valid JSON: {exc}",
                operation="provider.deserialize",
                cause=exc,
            ) from exc
        if not isinstance(decoded, dict):  # pragma: no cover
            raise PersistenceError(
                "model_catalog.provider.config_corrupt",
                f"provider {provider_id!r} value_json must decode to a dict",
                operation="provider.deserialize",
            )
        return decoded


def _parse_iso(value: str) -> datetime:
    """Helper for tests that round-trip ``updated_at`` strings."""
    return datetime.fromisoformat(value)
