# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`AppModelRepositoryPort` (PR-045).

Schema reference: ``qai-db-schema.md`` §3.1 (``app_builder_model_definition``).
Replaces the legacy ``config/app_builder_models.json`` file.

``input_presets`` and ``required_catalog_ids`` are stored as JSON
columns (``input_presets_json`` / ``required_catalog_ids_json``) per
the schema; this adapter handles the (de)serialisation to keep the
domain layer JSON-free.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError
from qai.platform.time import Clock

from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.errors import AppModelNotFoundError
from qai.app_builder.domain.taxonomy import Taxonomy
from qai.app_builder.domain.value_objects import (
    AppModelId,
    InputPreset,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteAppModelRepository"]


_INSERT_OR_REPLACE_SQL = (
    "INSERT INTO app_builder_model_definition "
    "(id, title, taxonomy_path, enabled, pinned, "
    "input_presets_json, required_catalog_ids_json, "
    "user_imported, version, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "title=excluded.title, "
    "taxonomy_path=excluded.taxonomy_path, "
    "enabled=excluded.enabled, "
    "pinned=excluded.pinned, "
    "input_presets_json=excluded.input_presets_json, "
    "required_catalog_ids_json=excluded.required_catalog_ids_json, "
    "user_imported=excluded.user_imported, "
    "version=excluded.version, "
    "updated_at=excluded.updated_at"
)


class SqliteAppModelRepository:
    """aiosqlite implementation of :class:`AppModelRepositoryPort`."""

    __slots__ = ("_db", "_clock")

    def __init__(self, *, db: "Database", clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def list_all(self) -> tuple[AppModelDefinition, ...]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, title, taxonomy_path, enabled, pinned, "
                    "input_presets_json, required_catalog_ids_json, "
                    "user_imported, version "
                    "FROM app_builder_model_definition "
                    # Built-in models first (user_imported=0), then
                    # user-imported (user_imported=1). Built-ins keep the
                    # legacy ``pinned DESC, title ASC`` order; user-imported
                    # models sort by ``created_at ASC`` (add-time) so the
                    # gallery lists them in the order the user imported them
                    # (title used only as a deterministic tie-break). The
                    # CASE keeps each tier on its own sort key so surfacing
                    # add-time for user models never perturbs built-in order.
                    "ORDER BY user_imported ASC, pinned DESC, "
                    "CASE WHEN user_imported = 1 THEN created_at ELSE title END ASC, "
                    "title ASC"
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.app_model.list_failed",
                f"failed to list app models: {exc}",
                operation="app_model.list",
                cause=exc,
            ) from exc
        return tuple(self._row_to_model(r) for r in rows)

    async def get(self, model_id: AppModelId) -> AppModelDefinition:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, title, taxonomy_path, enabled, pinned, "
                    "input_presets_json, required_catalog_ids_json, "
                    "user_imported, version "
                    "FROM app_builder_model_definition WHERE id = ?",
                    (model_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.app_model.get_failed",
                f"failed to load app model {model_id!r}: {exc}",
                operation="app_model.get",
                cause=exc,
            ) from exc
        if row is None:
            raise AppModelNotFoundError(
                message=f"app model {model_id} not found",
                details={"model_id": str(model_id)},
            )
        return self._row_to_model(row)

    async def save(self, model: AppModelDefinition) -> None:
        """Insert or update an app model definition.

        Not part of the Port contract today but exposed for adapter-side
        seeding and for the import-commit code path. Keeps the schema
        symmetric (read AND write through the same table).
        """
        now_iso = self._clock.now().isoformat()
        params = (
            model.id.value,
            model.title,
            "/".join(model.taxonomy.segments),
            1 if model.enabled else 0,
            1 if model.pinned else 0,
            json.dumps(
                [
                    {"name": p.name, "payload": p.payload}
                    for p in model.input_presets
                ]
            ),
            json.dumps(list(model.required_catalog_ids)),
            1 if model.user_imported else 0,
            model.version,
            now_iso,
            now_iso,
        )
        try:
            async with self._db.connection() as conn:
                await conn.execute(_INSERT_OR_REPLACE_SQL, params)
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.app_model.save_failed",
                f"failed to save app model {model.id!r}: {exc}",
                operation="app_model.save",
                cause=exc,
            ) from exc

    async def delete(self, model_id: AppModelId) -> None:
        # Verify the row exists first so we can raise the canonical
        # NotFoundError per the port contract.
        await self.get(model_id)
        try:
            async with self._db.connection() as conn:
                # Deleting a model means deleting it together with its run
                # history. ``app_builder_run.model_id`` declares
                # ``ON DELETE RESTRICT`` (migration 003) to stop a stray
                # ``DELETE`` from silently orphaning runs, so we must first
                # remove the model's runs explicitly. The dependent
                # ``app_builder_artifact`` + ``app_builder_share`` rows both
                # declare ``ON DELETE CASCADE`` on ``app_builder_run.id``, so
                # deleting the run rows clears them atomically.
                # ``app_builder_audit_entry`` holds only SOFT references
                # (model_id / run_id, no FOREIGN KEY) so the audit trail is
                # preserved — audit must outlive deletion (schema doc §3.6).
                #
                # This matches V1 behaviour: deleting a model never failed
                # (the JSON overlay dropped it and the separate run-history DB
                # kept the rows as harmless orphans). V2's relational FK must
                # not turn that into a hard blocker, so a model delete cascades
                # to its own runs. Wrapped in BEGIN IMMEDIATE so the cascade is
                # observably atomic against concurrent reads.
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(
                        "DELETE FROM app_builder_run WHERE model_id = ?",
                        (model_id.value,),
                    )
                    await conn.execute(
                        "DELETE FROM app_builder_model_definition WHERE id = ?",
                        (model_id.value,),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.app_model.delete_failed",
                f"failed to delete app model {model_id!r}: {exc}",
                operation="app_model.delete",
                cause=exc,
            ) from exc

    @staticmethod
    def _row_to_model(row: tuple[object, ...]) -> AppModelDefinition:
        model_id = AppModelId(value=str(row[0]))
        title = str(row[1])
        taxonomy_path = str(row[2] or "")
        segments: tuple[str, ...] = (
            tuple(taxonomy_path.split("/")) if taxonomy_path else ()
        )
        enabled = bool(int(row[3] or 0))
        pinned = bool(int(row[4] or 0))
        presets_data = json.loads(str(row[5] or "[]"))
        catalog_ids_data = json.loads(str(row[6] or "[]"))
        user_imported = bool(int(row[7] or 0)) if len(row) > 7 else False
        version = str(row[8]) if len(row) > 8 and row[8] else "1.0.0"
        return AppModelDefinition(
            id=model_id,
            title=title,
            taxonomy=Taxonomy(segments=segments),
            enabled=enabled,
            pinned=pinned,
            input_presets=tuple(
                InputPreset(
                    name=str(item["name"]),
                    payload=dict(item.get("payload", {})),
                )
                for item in presets_data
            ),
            required_catalog_ids=tuple(str(c) for c in catalog_ids_data),
            user_imported=user_imported,
            version=version,
        )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)
