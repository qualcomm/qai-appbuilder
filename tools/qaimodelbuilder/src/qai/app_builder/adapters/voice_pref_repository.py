# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`VoiceInputPreferenceRepositoryPort` (PR-045 + PR-307).

Schema reference: ``qai-db-schema.md`` §3.5 (``app_builder_voice_pref``).
The table is a singleton (``id = 'default'``) — multi-user storage is
intentionally outside this product's scope (see §10.6 of the schema doc).

PR-307 adds a ``preferred_variant_id`` column (mirrors the
:class:`VoiceInputPreference.preferred_variant_id` domain field). The
column is added by migration `009_voice_pref_variant_id.sql`. To keep
the adapter self-contained across migration history it uses
**schema-aware** queries: it inspects ``PRAGMA table_info`` once per
connection and falls back to the PR-045 two-column shape when the new
column is absent. This means:

* Existing v2 installations (migration 009 not yet applied) keep
  working, returning ``preferred_variant_id=None``.
* Post-migration installations transparently round-trip the field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError
from qai.platform.time import Clock

from qai.app_builder.domain.value_objects import AppModelId
from qai.app_builder.domain.voice_preference import VoiceInputPreference

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteVoicePrefRepository"]


_SINGLETON_ID = "default"


# ---------------------------------------------------------------------------
# SQL — two flavours (with / without preferred_variant_id column)
# ---------------------------------------------------------------------------
_SELECT_LEGACY = (
    "SELECT enabled, preferred_model_id "
    "FROM app_builder_voice_pref WHERE id = ?"
)
_SELECT_V307 = (
    "SELECT enabled, preferred_model_id, preferred_variant_id "
    "FROM app_builder_voice_pref WHERE id = ?"
)
_UPSERT_LEGACY = (
    "INSERT INTO app_builder_voice_pref "
    "(id, enabled, preferred_model_id, updated_at) "
    "VALUES (?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "enabled=excluded.enabled, "
    "preferred_model_id=excluded.preferred_model_id, "
    "updated_at=excluded.updated_at"
)
_UPSERT_V307 = (
    "INSERT INTO app_builder_voice_pref "
    "(id, enabled, preferred_model_id, preferred_variant_id, updated_at) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "enabled=excluded.enabled, "
    "preferred_model_id=excluded.preferred_model_id, "
    "preferred_variant_id=excluded.preferred_variant_id, "
    "updated_at=excluded.updated_at"
)


class SqliteVoicePrefRepository:
    """aiosqlite implementation of the singleton voice preference store."""

    __slots__ = ("_db", "_clock", "_has_variant_column")

    def __init__(self, *, db: "Database", clock: Clock) -> None:
        self._db = db
        self._clock = clock
        # ``None`` = unknown (introspect on first call); ``True`` /
        # ``False`` once known. Cached because the schema doesn't
        # change at runtime.
        self._has_variant_column: bool | None = None

    async def get(self) -> VoiceInputPreference:
        try:
            async with self._db.connection() as conn:
                has_variant = await self._detect_variant_column(conn)
                sql = _SELECT_V307 if has_variant else _SELECT_LEGACY
                cur = await conn.execute(sql, (_SINGLETON_ID,))
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.voice_pref.get_failed",
                f"failed to load voice preference: {exc}",
                operation="voice_pref.get",
                cause=exc,
            ) from exc
        if row is None:
            return VoiceInputPreference.default()
        enabled = bool(int(row[0] or 0))
        preferred = (
            AppModelId(value=str(row[1])) if row[1] is not None else None
        )
        preferred_variant: str | None = None
        if has_variant and len(row) >= 3 and row[2] is not None:
            preferred_variant = str(row[2])
        # Cross-field invariant on the VO: variant_id requires model_id.
        # If the DB has a stray variant_id without model_id, drop it
        # silently rather than raising — DB integrity is best-effort.
        if preferred is None:
            preferred_variant = None
        return VoiceInputPreference(
            enabled=enabled,
            preferred_model_id=preferred,
            preferred_variant_id=preferred_variant,
        )

    async def set(self, pref: VoiceInputPreference) -> None:
        try:
            async with self._db.connection() as conn:
                has_variant = await self._detect_variant_column(conn)
                if has_variant:
                    sql = _UPSERT_V307
                    params = (
                        _SINGLETON_ID,
                        1 if pref.enabled else 0,
                        (
                            pref.preferred_model_id.value
                            if pref.preferred_model_id is not None
                            else None
                        ),
                        pref.preferred_variant_id,
                        self._clock.now().isoformat(),
                    )
                else:
                    sql = _UPSERT_LEGACY
                    params = (
                        _SINGLETON_ID,
                        1 if pref.enabled else 0,
                        (
                            pref.preferred_model_id.value
                            if pref.preferred_model_id is not None
                            else None
                        ),
                        self._clock.now().isoformat(),
                    )
                await conn.execute(sql, params)
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.voice_pref.set_failed",
                f"failed to save voice preference: {exc}",
                operation="voice_pref.set",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _detect_variant_column(self, conn) -> bool:  # noqa: ANN001
        """Return True iff ``preferred_variant_id`` exists in the table.

        The result is cached on the instance so we only do one PRAGMA
        per repository lifetime. SQLite ``PRAGMA table_info`` returns
        rows ``(cid, name, type, notnull, dflt, pk)``.
        """
        if self._has_variant_column is not None:
            return self._has_variant_column
        cur = await conn.execute(
            "PRAGMA table_info(app_builder_voice_pref)"
        )
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        column_names = {str(r[1]) for r in rows}
        self._has_variant_column = "preferred_variant_id" in column_names
        return self._has_variant_column
