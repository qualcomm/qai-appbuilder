# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`ModelEntryRepositoryPort` (PR-044).

Schema reference: ``qai-db-schema.md`` §6.1 (model_catalog_entry) +
§6.2 (model_catalog_version).

A :class:`ModelEntry` is the aggregate root; :class:`ModelVersion` is a
child entity. Persistence is therefore a two-table relational model:

* ``model_catalog_entry`` — header row, one per aggregate
* ``model_catalog_version`` — child rows, ``parent_model_id`` FK with
  ``ON DELETE CASCADE``

Save semantics (mirrors PR-040 ``SqlitePolicyRepository``)
-------------------------------------------------------------

``add`` and ``update`` both rebuild the children deterministically by
deleting and re-inserting them inside an explicit ``BEGIN IMMEDIATE``
transaction so concurrent readers never see a half-mutated aggregate.

``add`` distinguishes itself by raising :class:`ModelEntryConflictError`
when the parent row already exists (the SQLite PK clash on
``model_catalog_entry.id`` surfaces as an ``IntegrityError``).

The partial unique index ``uq_catalog_version_one_downloading`` enforces
the domain invariant "at most one DOWNLOADING version per parent" at
the SQL level — the domain ``__post_init__`` catches it earlier, but
the SQL guard remains in case a future caller bypasses the aggregate
and writes through a stale entity.

Cross-context note
------------------

Saving an entry whose ``current_version_id`` references a version that
is **not** in the entry's ``versions`` list is rejected by the domain
layer's ``__post_init__`` *before* persistence — adapters never need to
double-check.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError
from qai.platform.time import Clock

from qai.model_catalog.domain.entities import ModelEntry, ModelVersion
from qai.model_catalog.domain.errors import ModelEntryConflictError
from qai.model_catalog.domain.ids import (
    ModelEntryId,
    ModelVersionId,
)
from qai.model_catalog.domain.value_objects import (
    Checksum,
    ChecksumAlgorithm,
    ModelVersionStatus,
    ProviderKind,
    SizeBytes,
    SourceUrl,
    Taxonomy,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteModelEntryRepository"]


_INSERT_ENTRY_SQL = (
    "INSERT INTO model_catalog_entry "
    "(id, name, provider, source_url, description, "
    "taxonomy_tags_json, current_version_id, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_UPSERT_ENTRY_SQL = (
    "INSERT INTO model_catalog_entry "
    "(id, name, provider, source_url, description, "
    "taxonomy_tags_json, current_version_id, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "name=excluded.name, "
    "provider=excluded.provider, "
    "source_url=excluded.source_url, "
    "description=excluded.description, "
    "taxonomy_tags_json=excluded.taxonomy_tags_json, "
    "current_version_id=excluded.current_version_id, "
    "updated_at=excluded.updated_at"
)

_INSERT_VERSION_SQL = (
    "INSERT INTO model_catalog_version "
    "(id, parent_model_id, checksum_algorithm, checksum_value, "
    "size_bytes, manifest_url, status) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


class SqliteModelEntryRepository:
    """aiosqlite implementation of :class:`ModelEntryRepositoryPort`."""

    __slots__ = ("_db", "_clock")

    def __init__(self, *, db: "Database", clock: Clock) -> None:
        self._db = db
        self._clock = clock

    # ── Reads ──────────────────────────────────────────────────────────

    async def find_by_id(
        self, model_id: ModelEntryId
    ) -> ModelEntry | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, name, provider, source_url, description, "
                    "taxonomy_tags_json, current_version_id "
                    "FROM model_catalog_entry WHERE id = ?",
                    (model_id.value,),
                )
                head = await cur.fetchone()
                await cur.close()
                if head is None:
                    return None
                cur = await conn.execute(
                    "SELECT id, parent_model_id, checksum_algorithm, "
                    "checksum_value, size_bytes, manifest_url, status "
                    "FROM model_catalog_version "
                    "WHERE parent_model_id = ? "
                    "ORDER BY id ASC",
                    (model_id.value,),
                )
                version_rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.entry.find_failed",
                f"failed to load model_entry {model_id.value!r}: {exc}",
                operation="model_entry.find_by_id",
                cause=exc,
            ) from exc
        return self._row_to_entry(head, version_rows)

    async def list_all(self) -> list[ModelEntry]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, name, provider, source_url, description, "
                    "taxonomy_tags_json, current_version_id "
                    "FROM model_catalog_entry "
                    "ORDER BY name ASC, id ASC"
                )
                head_rows = await cur.fetchall()
                await cur.close()
                # Bulk-load child rows to keep the read O(2) round-trips.
                cur = await conn.execute(
                    "SELECT id, parent_model_id, checksum_algorithm, "
                    "checksum_value, size_bytes, manifest_url, status "
                    "FROM model_catalog_version "
                    "ORDER BY parent_model_id ASC, id ASC"
                )
                version_rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.entry.list_failed",
                f"failed to list model_entries: {exc}",
                operation="model_entry.list_all",
                cause=exc,
            ) from exc

        # Group versions by parent_model_id so we don't re-query per entry.
        by_parent: dict[str, list[tuple[object, ...]]] = {}
        for row in version_rows:
            by_parent.setdefault(str(row[1]), []).append(row)

        return [
            self._row_to_entry(h, by_parent.get(str(h[0]), []))
            for h in head_rows
        ]

    # ── Writes ─────────────────────────────────────────────────────────

    async def add(self, entry: ModelEntry) -> None:
        await self._save(entry, mode="add")

    async def update(self, entry: ModelEntry) -> None:
        await self._save(entry, mode="update")

    async def remove(self, model_id: ModelEntryId) -> None:
        # Per port contract: silent no-op if absent (RemoveModelEntryUseCase
        # does the existence check). Cascade drops child version rows.
        try:
            async with self._db.connection() as conn:
                await conn.execute(
                    "DELETE FROM model_catalog_entry WHERE id = ?",
                    (model_id.value,),
                )
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.entry.remove_failed",
                f"failed to delete model_entry {model_id.value!r}: {exc}",
                operation="model_entry.remove",
                cause=exc,
            ) from exc

    # ── Internals ──────────────────────────────────────────────────────

    async def _save(self, entry: ModelEntry, *, mode: str) -> None:
        """Atomically persist an entry plus its full version list.

        ``mode`` ∈ ``{"add", "update"}``:

        * ``add`` insists the parent row is brand-new — a duplicate id
          surfaces as :class:`ModelEntryConflictError` (HTTP 409).
        * ``update`` rewrites the parent header via UPSERT and replaces
          the children wholesale, mirroring the Policy / PathGrant
          template from PR-040.
        """
        now_iso = self._clock.now().isoformat()
        head_params = (
            entry.model_id.value,
            entry.name,
            entry.provider.value,
            entry.source_url.value,
            entry.description,
            json.dumps(list(entry.taxonomy.tags), separators=(",", ":")),
            entry.current_version_id.value
            if entry.current_version_id is not None
            else None,
            now_iso,
            now_iso,
        )
        version_params: list[tuple[object, ...]] = []
        for v in entry.versions:
            version_params.append(
                (
                    v.version_id.value,
                    v.parent_model_id.value,
                    v.checksum.algorithm.value,
                    v.checksum.value,
                    v.size_bytes.value,
                    v.manifest_url.value,
                    v.status.value,
                )
            )

        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    if mode == "add":
                        await conn.execute(_INSERT_ENTRY_SQL, head_params)
                    else:
                        await conn.execute(_UPSERT_ENTRY_SQL, head_params)
                    await conn.execute(
                        "DELETE FROM model_catalog_version "
                        "WHERE parent_model_id = ?",
                        (entry.model_id.value,),
                    )
                    if version_params:
                        await conn.executemany(
                            _INSERT_VERSION_SQL, version_params
                        )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except ModelEntryConflictError:
            raise
        except Exception as exc:  # noqa: BLE001
            module = type(exc).__module__
            if module.startswith("sqlite3") or module.startswith(
                "aiosqlite"
            ):
                # IntegrityError → uniqueness clash on the parent id or
                # the partial-unique single-downloading index.
                raise ModelEntryConflictError(
                    entry.model_id.value,
                    message=(
                        f"failed to {mode} model_entry "
                        f"{entry.model_id.value!r}: {exc}"
                    ),
                ) from exc
            raise PersistenceError(
                f"model_catalog.entry.{mode}_failed",
                f"failed to {mode} model_entry "
                f"{entry.model_id.value!r}: {exc}",
                operation=f"model_entry.{mode}",
                cause=exc,
            ) from exc

    @staticmethod
    def _row_to_entry(
        head: tuple[object, ...],
        version_rows: list[tuple[object, ...]] | tuple[tuple[object, ...], ...],
    ) -> ModelEntry:
        model_id = ModelEntryId(str(head[0]))
        name = str(head[1])
        provider = ProviderKind(str(head[2]))
        source_url = SourceUrl(value=str(head[3]))
        description = str(head[4] or "")
        tags_json = str(head[5] or "[]")
        try:
            tags_raw = json.loads(tags_json)
        except json.JSONDecodeError as exc:  # pragma: no cover
            raise PersistenceError(
                "model_catalog.entry.tags_corrupt",
                f"taxonomy_tags_json is not valid JSON: {exc}",
                operation="model_entry.deserialize",
                cause=exc,
            ) from exc
        taxonomy = Taxonomy(tags=tuple(tags_raw))
        current_version_value = (
            None if head[6] is None else str(head[6])
        )
        current_version_id = (
            ModelVersionId(current_version_value)
            if current_version_value is not None
            else None
        )
        versions = [
            SqliteModelEntryRepository._row_to_version(r) for r in version_rows
        ]
        return ModelEntry(
            model_id=model_id,
            name=name,
            provider=provider,
            source_url=source_url,
            description=description,
            taxonomy=taxonomy,
            versions=versions,
            current_version_id=current_version_id,
        )

    @staticmethod
    def _row_to_version(row: tuple[object, ...]) -> ModelVersion:
        version_id = ModelVersionId(str(row[0]))
        parent_id = ModelEntryId(str(row[1]))
        checksum = Checksum(
            algorithm=ChecksumAlgorithm(str(row[2])),
            value=str(row[3]),
        )
        size_bytes = SizeBytes(value=int(row[4]))
        manifest_url = SourceUrl(value=str(row[5]))
        status = ModelVersionStatus(str(row[6]))
        return ModelVersion(
            version_id=version_id,
            parent_model_id=parent_id,
            checksum=checksum,
            size_bytes=size_bytes,
            manifest_url=manifest_url,
            status=status,
        )


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into a tz-aware ``datetime``.

    Currently only used by the test suite when it round-trips
    ``created_at`` / ``updated_at``; kept module-private to avoid
    polluting the public surface.
    """
    return datetime.fromisoformat(value)
