# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`SkillRegistryPort` for ai_coding (PR-046).

Schema reference: ``qai-db-schema.md`` §4.5 (``ai_coding_skill``).
Replaces the in-memory ``_FakeAiCodingSkillRegistry`` from S3.

This adapter is the *primary* implementation of
:class:`qai.ai_coding.application.ports.SkillRegistryPort` for the
ai_coding context.  PR-044 will introduce a sibling
``SqliteModelSkillRegistry`` for the model_catalog context; the two
are kept distinct (per ``qai-db-schema.md`` §10.3) so a security
escalation in one cannot cross over.  Cross-context callers that need
to surface model_catalog skills inside an ai_coding session reach
through the ``apps/api/_skill_registry_bridge.py`` adapter rather than
sharing a registry instance.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from qai.ai_coding.domain import Skill, SkillNotRegisteredError
from qai.platform.errors import PersistenceError
from qai.platform.time import Clock

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteAiCodingSkillRegistry"]


class SqliteAiCodingSkillRegistry:
    """aiosqlite implementation of :class:`SkillRegistryPort`."""

    __slots__ = ("_clock", "_db")

    def __init__(self, *, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def register(self, skill: Skill) -> None:
        registered_at = self._clock.now().isoformat()
        try:
            async with self._db.connection() as conn:
                await conn.execute(
                    "INSERT INTO ai_coding_skill "
                    "(name, description, spec_json, enabled, registered_at) "
                    "VALUES (?, ?, ?, 1, ?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "description=excluded.description, "
                    "spec_json=excluded.spec_json, "
                    "enabled=1, "
                    "registered_at=excluded.registered_at",
                    (
                        skill.name,
                        skill.description,
                        json.dumps(skill.spec, sort_keys=True),
                        registered_at,
                    ),
                )
                await conn.commit()
        except Exception as exc:
            raise PersistenceError(
                "ai_coding.skill.register_failed",
                f"failed to register skill {skill.name!r}: {exc}",
                operation="skill.register",
                cause=exc,
            ) from exc

    async def list_skills(self) -> list[Skill]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT name, description, spec_json "
                    "FROM ai_coding_skill "
                    "WHERE enabled = 1 "
                    "ORDER BY name ASC"
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:
            raise PersistenceError(
                "ai_coding.skill.list_failed",
                f"failed to list skills: {exc}",
                operation="skill.list",
                cause=exc,
            ) from exc
        return [self._row_to_skill(r) for r in rows]

    async def get(self, name: str) -> Skill:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT name, description, spec_json "
                    "FROM ai_coding_skill "
                    "WHERE name = ? AND enabled = 1",
                    (name,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:
            raise PersistenceError(
                "ai_coding.skill.get_failed",
                f"failed to load skill {name!r}: {exc}",
                operation="skill.get",
                cause=exc,
            ) from exc
        if row is None:
            raise SkillNotRegisteredError(
                message=f"skill {name} not registered",
                details={"skill_name": name},
            )
        return self._row_to_skill(row)

    @staticmethod
    def _row_to_skill(row: tuple[object, ...]) -> Skill:
        spec_json = str(row[2] or "{}")
        return Skill(
            name=str(row[0]),
            description=str(row[1] or ""),
            spec=json.loads(spec_json),
        )

    @staticmethod
    def _parse_iso(value: str) -> datetime:  # pragma: no cover — reserved
        return datetime.fromisoformat(value)
