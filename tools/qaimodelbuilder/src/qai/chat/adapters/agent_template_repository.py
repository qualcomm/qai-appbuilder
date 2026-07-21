# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`AgentTemplateRepositoryPort`.

Schema reference: ``qai-db-schema.md`` §2.9 (chat_agent_template, migration
039).  An :class:`~qai.chat.domain.agent_template.AgentTemplate` is a
**single-row aggregate** -- one library entry holds one role definition (its
``config`` lives in a ``config_json`` blob) -- so :meth:`save` is a single
``BEGIN IMMEDIATE`` upsert with no child-row rewrite (mirroring
:class:`~qai.chat.adapters.roster_template_repository.
SqliteRosterTemplateRepository`).

Unlike a participant (strictly conversation-scoped), an agent template is a
conversation-independent library entry: it has no ``conversation_id`` and no FK
to ``chat_conversation``.  ``is_builtin`` marks factory-seeded preset templates.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from qai.chat.domain.agent_template import AgentTemplate
from qai.chat.domain.errors import AgentTemplateNotFoundError
from qai.chat.domain.ids import AgentTemplateId
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = ["SqliteAgentTemplateRepository"]


_COLUMNS = (
    "id, name, description, display_name, model_id, persona, config_json, "
    "is_builtin, cloned_from_id, created_at, updated_at, "
    "name_i18n_json, description_i18n_json, display_name_i18n_json, "
    "persona_i18n_json"
)


def _config_to_json(config: dict[str, Any] | None) -> str | None:
    if config is None:
        return None
    return json.dumps(config, ensure_ascii=False)


def _i18n_to_json(i18n: dict[str, str] | None) -> str | None:
    """Serialise an i18n map to JSON, or ``None`` when absent (write path)."""
    if not i18n:
        return None
    return json.dumps(i18n, ensure_ascii=False)


def _i18n_from_json(raw: object) -> dict[str, str] | None:
    """Parse a ``*_i18n_json`` column into a per-locale map.

    A NULL / malformed / non-object value degrades to ``None`` (fall back to the
    canonical single-language column) rather than raising, so a single bad row
    cannot break a list/find (AGENTS.md §8 forward-compatibility).
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    # Keep only str->str entries so downstream resolve never trips on a
    # non-string value.
    result = {k: v for k, v in parsed.items() if isinstance(k, str) and isinstance(v, str)}
    return result or None


def _config_from_json(raw: object) -> dict[str, Any] | None:
    """Parse ``config_json`` into a config blob.

    A malformed / non-object value degrades to ``None`` rather than raising, so
    a single bad row cannot break a list/find.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


class SqliteAgentTemplateRepository:
    """aiosqlite implementation of :class:`AgentTemplateRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------
    async def save(self, template: AgentTemplate) -> None:
        """Insert or upsert ``template`` (single-row aggregate, by id)."""
        params = (
            template.id.value,
            template.name,
            template.description,
            template.display_name,
            template.model_id,
            template.persona,
            _config_to_json(template.config),
            1 if template.is_builtin else 0,
            template.cloned_from_id,
            template.created_at.isoformat(),
            template.updated_at.isoformat(),
            _i18n_to_json(template.name_i18n),
            _i18n_to_json(template.description_i18n),
            _i18n_to_json(template.display_name_i18n),
            _i18n_to_json(template.persona_i18n),
        )
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(
                        "INSERT INTO chat_agent_template ("
                        "id, name, description, display_name, model_id, "
                        "persona, config_json, is_builtin, cloned_from_id, "
                        "created_at, updated_at, name_i18n_json, "
                        "description_i18n_json, display_name_i18n_json, "
                        "persona_i18n_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        " name=excluded.name, "
                        " description=excluded.description, "
                        " display_name=excluded.display_name, "
                        " model_id=excluded.model_id, "
                        " persona=excluded.persona, "
                        " config_json=excluded.config_json, "
                        " is_builtin=excluded.is_builtin, "
                        " updated_at=excluded.updated_at, "
                        " name_i18n_json=excluded.name_i18n_json, "
                        " description_i18n_json=excluded.description_i18n_json, "
                        " display_name_i18n_json=excluded.display_name_i18n_json, "
                        " persona_i18n_json=excluded.persona_i18n_json",
                        params,
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.agent_template.save_failed",
                f"failed to save agent template {template.id.value!r}: {exc}",
                operation="agent_template.save",
                cause=exc,
            ) from exc

    async def delete(self, template_id: AgentTemplateId) -> None:
        """Remove the template; raise if missing."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_agent_template WHERE id = ?",
                    (template_id.value,),
                )
                rows_affected = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.agent_template.delete_failed",
                f"failed to delete agent template {template_id.value!r}: {exc}",
                operation="agent_template.delete",
                cause=exc,
            ) from exc
        if rows_affected == 0:
            raise AgentTemplateNotFoundError(template_id.value)

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------
    async def find(
        self,
        template_id: AgentTemplateId,
    ) -> AgentTemplate | None:
        """Return the aggregate or ``None`` if not present."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_agent_template WHERE id = ?",
                    (template_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.agent_template.find_failed",
                f"failed to load agent template {template_id.value!r}: {exc}",
                operation="agent_template.find",
                cause=exc,
            ) from exc
        if row is None:
            return None
        return self._row_to_template(row)

    async def get(self, template_id: AgentTemplateId) -> AgentTemplate:
        """Return the aggregate; raise :class:`AgentTemplateNotFoundError`."""
        template = await self.find(template_id)
        if template is None:
            raise AgentTemplateNotFoundError(template_id.value)
        return template

    async def list_all(self) -> tuple[AgentTemplate, ...]:
        """Return every template, built-ins first then by ``created_at`` ASC."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_agent_template "
                    "ORDER BY is_builtin DESC, created_at ASC",
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.agent_template.list_all_failed",
                f"failed to list agent templates: {exc}",
                operation="agent_template.list_all",
                cause=exc,
            ) from exc
        return tuple(self._row_to_template(r) for r in rows)

    # ------------------------------------------------------------------
    # Row -> domain
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_template(row: tuple[object, ...]) -> AgentTemplate:
        # i18n columns are tail-appended (index 11-14); a short projection
        # (legacy row read before migration 056) leaves them unset -> None ->
        # canonical single-language fallback (AGENTS.md §8).
        return AgentTemplate(
            id=AgentTemplateId.of(str(row[0])),
            name=str(row[1]) if row[1] is not None else "",
            description=str(row[2]) if row[2] is not None else "",
            display_name=str(row[3]) if row[3] is not None else "",
            model_id=str(row[4]) if row[4] is not None else None,
            persona=str(row[5]) if row[5] is not None else None,
            config=_config_from_json(row[6]),
            is_builtin=bool(row[7]),
            cloned_from_id=str(row[8]) if row[8] is not None else None,
            created_at=datetime.fromisoformat(str(row[9])),
            updated_at=datetime.fromisoformat(str(row[10])),
            name_i18n=_i18n_from_json(row[11]) if len(row) > 11 else None,
            description_i18n=_i18n_from_json(row[12]) if len(row) > 12 else None,
            display_name_i18n=_i18n_from_json(row[13]) if len(row) > 13 else None,
            persona_i18n=_i18n_from_json(row[14]) if len(row) > 14 else None,
        )
