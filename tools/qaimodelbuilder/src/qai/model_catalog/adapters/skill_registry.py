# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`SkillRegistryPort` (PR-044).

Schema reference: ``qai-db-schema.md`` §6.6 (model_catalog_skill).

The skill registry is intentionally context-isolated: while a sister
table ``ai_coding_skill`` (§4.5) exists for runtime concerns, this
adapter only reads/writes ``model_catalog_skill``. PR-046 will compose
across the two tables at the apps layer rather than crossing the
context-isolation contract.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from qai.platform.errors import PersistenceError

from qai.model_catalog.domain.entities import SkillDefinition
from qai.model_catalog.domain.ids import SkillName

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteModelSkillRegistry"]


_UPSERT_SQL = (
    "INSERT INTO model_catalog_skill "
    "(name, version, enabled, manifest_json) "
    "VALUES (?, ?, ?, ?) "
    "ON CONFLICT(name) DO UPDATE SET "
    "version=excluded.version, "
    "enabled=excluded.enabled, "
    "manifest_json=excluded.manifest_json"
)


class SqliteModelSkillRegistry:
    """aiosqlite implementation of :class:`SkillRegistryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def list_skills(self) -> list[SkillDefinition]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT name, version, enabled, manifest_json "
                    "FROM model_catalog_skill "
                    "ORDER BY name ASC"
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.skill.list_failed",
                f"failed to list skills: {exc}",
                operation="skill.list",
                cause=exc,
            ) from exc
        return [self._row_to_skill(r) for r in rows]

    async def get(self, skill: SkillName) -> SkillDefinition | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT name, version, enabled, manifest_json "
                    "FROM model_catalog_skill WHERE name = ?",
                    (skill.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.skill.get_failed",
                f"failed to load skill {skill.value!r}: {exc}",
                operation="skill.get",
                cause=exc,
            ) from exc
        return None if row is None else self._row_to_skill(row)

    async def upsert(self, skill: SkillDefinition) -> None:
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(
                        _UPSERT_SQL,
                        (
                            skill.skill_name.value,
                            skill.version,
                            1 if skill.enabled else 0,
                            json.dumps(
                                skill.manifest, separators=(",", ":")
                            ),
                        ),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.skill.upsert_failed",
                f"failed to upsert skill {skill.skill_name.value!r}: {exc}",
                operation="skill.upsert",
                cause=exc,
            ) from exc

    @staticmethod
    def _row_to_skill(row: tuple[object, ...]) -> SkillDefinition:
        manifest_text = str(row[3] or "{}")
        try:
            manifest: dict[str, Any] = json.loads(manifest_text)
        except json.JSONDecodeError as exc:
            raise PersistenceError(
                "model_catalog.skill.manifest_corrupt",
                f"manifest_json is not valid JSON: {exc}",
                operation="skill.deserialize",
                cause=exc,
            ) from exc
        return SkillDefinition(
            skill_name=SkillName(str(row[0])),
            version=str(row[1]),
            enabled=bool(int(row[2] or 0)),
            manifest=manifest,
        )
