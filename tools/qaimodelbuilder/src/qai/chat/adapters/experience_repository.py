# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`ExperienceRepositoryPort` (PR-042).

Schema reference: ``qai-db-schema.md`` §2.4 (chat_experience).
Single-row aggregate -- INSERT OR REPLACE on save, DELETE on delete.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from qai.chat.domain.errors import ExperienceNotFoundError
from qai.chat.domain.experience import Experience
from qai.chat.domain.ids import ExperienceId
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = ["SqliteExperienceRepository"]


_COLUMNS = "id, category, content, metadata_json, created_at"


class SqliteExperienceRepository:
    """aiosqlite implementation of :class:`ExperienceRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def save(self, experience: Experience) -> None:
        """Insert or replace ``experience`` keyed by id."""
        params = (
            experience.id.value,
            experience.category,
            experience.content,
            json.dumps(experience.metadata),
            experience.created_at.isoformat(),
        )
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(
                        "INSERT INTO chat_experience "
                        "(id, category, content, metadata_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        " category=excluded.category, "
                        " content=excluded.content, "
                        " metadata_json=excluded.metadata_json",
                        params,
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.experience.save_failed",
                f"failed to save experience {experience.id.value!r}: {exc}",
                operation="experience.save",
                cause=exc,
            ) from exc

    async def get(self, experience_id: ExperienceId) -> Experience:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_experience WHERE id = ?",
                    (experience_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.experience.get_failed",
                f"failed to load experience {experience_id.value!r}: {exc}",
                operation="experience.get",
                cause=exc,
            ) from exc
        if row is None:
            raise ExperienceNotFoundError(experience_id.value)
        return self._row_to_experience(row)

    async def delete(self, experience_id: ExperienceId) -> None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_experience WHERE id = ?",
                    (experience_id.value,),
                )
                rows_affected = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.experience.delete_failed",
                f"failed to delete experience {experience_id.value!r}: {exc}",
                operation="experience.delete",
                cause=exc,
            ) from exc
        if rows_affected == 0:
            raise ExperienceNotFoundError(experience_id.value)

    async def list(
        self,
        *,
        category: str | None = None,
        limit: int = 50,
    ) -> tuple[Experience, ...]:
        if limit <= 0:
            return ()
        try:
            async with self._db.connection() as conn:
                if category is None:
                    cur = await conn.execute(
                        f"SELECT {_COLUMNS} FROM chat_experience "
                        "ORDER BY created_at DESC LIMIT ?",
                        (int(limit),),
                    )
                else:
                    cur = await conn.execute(
                        f"SELECT {_COLUMNS} FROM chat_experience "
                        "WHERE category = ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (category, int(limit)),
                    )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.experience.list_failed",
                f"failed to list experiences: {exc}",
                operation="experience.list",
                cause=exc,
            ) from exc
        return tuple(self._row_to_experience(r) for r in rows)

    async def list_categories(self) -> tuple[str, ...]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT DISTINCT category FROM chat_experience "
                    "ORDER BY category ASC"
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.experience.list_categories_failed",
                f"failed to list categories: {exc}",
                operation="experience.list_categories",
                cause=exc,
            ) from exc
        return tuple(str(r[0]) for r in rows)

    @staticmethod
    def _row_to_experience(row: tuple[object, ...]) -> Experience:
        metadata_raw = str(row[3] or "{}")
        try:
            metadata = json.loads(metadata_raw)
            if not isinstance(metadata, dict):
                metadata = {}
        except (TypeError, ValueError):
            metadata = {}
        return Experience(
            id=ExperienceId.of(str(row[0])),
            category=str(row[1]),
            content=str(row[2]),
            metadata=metadata,
            created_at=datetime.fromisoformat(str(row[4])),
        )
