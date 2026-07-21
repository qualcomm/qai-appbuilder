# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`RosterTemplateRepositoryPort`.

Schema reference: ``qai-db-schema.md`` §2.8 (chat_roster_template, migration
038).  A :class:`~qai.chat.domain.roster_template.RosterTemplate` is a
**single-row aggregate** -- the whole team (its member role definitions) lives
in one ``chat_roster_template`` row's ``members_json`` JSON array -- so
:meth:`save` is a single ``BEGIN IMMEDIATE`` upsert with no child-row rewrite
(mirroring :class:`~qai.chat.adapters.participant_repository.
SqliteParticipantRepository`).

Unlike a participant (strictly conversation-scoped), a roster template is a
conversation-independent library entry: it has no ``conversation_id`` and no FK
to ``chat_conversation``.  ``is_builtin`` marks factory-seeded preset templates.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from qai.chat.domain.errors import RosterTemplateNotFoundError
from qai.chat.domain.ids import RosterTemplateId
from qai.chat.domain.roster_template import RosterTemplate, RosterTemplateMember
from qai.platform.errors import PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database


__all__ = ["SqliteRosterTemplateRepository"]


_COLUMNS = (
    "id, name, description, members_json, is_builtin, "
    "default_mode_id, cloned_from_id, created_at, updated_at, "
    "name_i18n_json, description_i18n_json, members_i18n_json"
)


def _i18n_to_json(i18n: dict[str, Any] | None) -> str | None:
    """Serialise an i18n map (str->str or str->list) to JSON, ``None`` if absent."""
    if not i18n:
        return None
    return json.dumps(i18n, ensure_ascii=False)


def _str_i18n_from_json(raw: object) -> dict[str, str] | None:
    """Parse a str->str ``*_i18n_json`` column; NULL / malformed -> ``None``.

    Forward-compatible (AGENTS.md §8): a bad blob degrades to ``None`` so the
    caller falls back to the canonical single-language column.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    result = {k: v for k, v in parsed.items() if isinstance(k, str) and isinstance(v, str)}
    return result or None


def _members_i18n_from_json(raw: object) -> dict[str, list[dict[str, Any]]] | None:
    """Parse ``members_i18n_json`` into ``{locale: [member-dict, ...]}``.

    Shape is ``{"en": [{"display_name","persona","config"}, ...], "zh-CN": [...],
    "zh-TW": [...]}``. Malformed / NULL degrades to ``None`` (fall back to the
    canonical ``members_json``); a per-locale value that is not a list is
    dropped, and non-dict entries inside a list are skipped — never raises.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    result: dict[str, list[dict[str, Any]]] = {}
    for locale, members in parsed.items():
        if not isinstance(locale, str) or not isinstance(members, list):
            continue
        cleaned = [m for m in members if isinstance(m, dict)]
        result[locale] = cleaned
    return result or None


def _members_to_json(members: tuple[RosterTemplateMember, ...]) -> str:
    payload: list[dict[str, Any]] = []
    for member in members:
        entry: dict[str, Any] = {"display_name": member.display_name}
        if member.model_id is not None:
            entry["model_id"] = member.model_id
        if member.persona is not None:
            entry["persona"] = member.persona
        if member.config is not None:
            entry["config"] = member.config
        payload.append(entry)
    return json.dumps(payload, ensure_ascii=False)


def _members_from_json(raw: object) -> tuple[RosterTemplateMember, ...]:
    """Parse ``members_json`` into member value objects.

    A malformed / non-array value degrades to an empty roster rather than
    raising, so a single bad row cannot break a list/find.
    """
    if raw is None:
        return ()
    try:
        parsed = json.loads(str(raw))
    except (ValueError, TypeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    members: list[RosterTemplateMember] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        config = item.get("config")
        # Per-item guard: a single malformed member (e.g. an out-of-shape
        # ``config`` that fails ``RosterTemplateMember`` validation) must not
        # break the whole list/find — skip it rather than propagate.
        try:
            members.append(
                RosterTemplateMember(
                    display_name=str(item.get("display_name", "")),
                    model_id=(
                        str(item["model_id"])
                        if item.get("model_id") is not None
                        else None
                    ),
                    persona=(
                        str(item["persona"])
                        if item.get("persona") is not None
                        else None
                    ),
                    config=config if isinstance(config, dict) else None,
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(members)


class SqliteRosterTemplateRepository:
    """aiosqlite implementation of :class:`RosterTemplateRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------
    async def save(self, template: RosterTemplate) -> None:
        """Insert or upsert ``template`` (single-row aggregate, by id)."""
        params = (
            template.id.value,
            template.name,
            template.description,
            _members_to_json(template.members),
            1 if template.is_builtin else 0,
            template.default_mode_id,
            template.cloned_from_id,
            template.created_at.isoformat(),
            template.updated_at.isoformat(),
            _i18n_to_json(template.name_i18n),
            _i18n_to_json(template.description_i18n),
            _i18n_to_json(template.members_i18n),
        )
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(
                        "INSERT INTO chat_roster_template ("
                        "id, name, description, members_json, is_builtin, "
                        "default_mode_id, cloned_from_id, created_at, "
                        "updated_at, name_i18n_json, description_i18n_json, "
                        "members_i18n_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        " name=excluded.name, "
                        " description=excluded.description, "
                        " members_json=excluded.members_json, "
                        " is_builtin=excluded.is_builtin, "
                        " default_mode_id=excluded.default_mode_id, "
                        " updated_at=excluded.updated_at, "
                        " name_i18n_json=excluded.name_i18n_json, "
                        " description_i18n_json=excluded.description_i18n_json, "
                        " members_i18n_json=excluded.members_i18n_json",
                        params,
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.roster_template.save_failed",
                f"failed to save roster template {template.id.value!r}: {exc}",
                operation="roster_template.save",
                cause=exc,
            ) from exc

    async def delete(self, template_id: RosterTemplateId) -> None:
        """Remove the template; raise if missing."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chat_roster_template WHERE id = ?",
                    (template_id.value,),
                )
                rows_affected = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.roster_template.delete_failed",
                f"failed to delete roster template {template_id.value!r}: {exc}",
                operation="roster_template.delete",
                cause=exc,
            ) from exc
        if rows_affected == 0:
            raise RosterTemplateNotFoundError(template_id.value)

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------
    async def find(
        self,
        template_id: RosterTemplateId,
    ) -> RosterTemplate | None:
        """Return the aggregate or ``None`` if not present."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_roster_template WHERE id = ?",
                    (template_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.roster_template.find_failed",
                f"failed to load roster template {template_id.value!r}: {exc}",
                operation="roster_template.find",
                cause=exc,
            ) from exc
        if row is None:
            return None
        return self._row_to_template(row)

    async def get(self, template_id: RosterTemplateId) -> RosterTemplate:
        """Return the aggregate; raise :class:`RosterTemplateNotFoundError`."""
        template = await self.find(template_id)
        if template is None:
            raise RosterTemplateNotFoundError(template_id.value)
        return template

    async def list_all(self) -> tuple[RosterTemplate, ...]:
        """Return every template, built-ins first then by ``created_at`` ASC."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    f"SELECT {_COLUMNS} FROM chat_roster_template "
                    "ORDER BY is_builtin DESC, created_at ASC",
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "chat.roster_template.list_all_failed",
                f"failed to list roster templates: {exc}",
                operation="roster_template.list_all",
                cause=exc,
            ) from exc
        return tuple(self._row_to_template(r) for r in rows)

    # ------------------------------------------------------------------
    # Row -> domain
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_template(row: tuple[object, ...]) -> RosterTemplate:
        # i18n columns tail-appended (index 9-11); a short projection (legacy
        # row read before migration 056) leaves them unset -> None -> canonical
        # single-language fallback (AGENTS.md §8).
        return RosterTemplate(
            id=RosterTemplateId.of(str(row[0])),
            name=str(row[1]) if row[1] is not None else "",
            description=str(row[2]) if row[2] is not None else "",
            members=_members_from_json(row[3]),
            is_builtin=bool(row[4]),
            default_mode_id=(str(row[5]) if row[5] is not None else None),
            cloned_from_id=(str(row[6]) if row[6] is not None else None),
            created_at=datetime.fromisoformat(str(row[7])),
            updated_at=datetime.fromisoformat(str(row[8])),
            name_i18n=_str_i18n_from_json(row[9]) if len(row) > 9 else None,
            description_i18n=_str_i18n_from_json(row[10]) if len(row) > 10 else None,
            members_i18n=_members_i18n_from_json(row[11]) if len(row) > 11 else None,
        )
