# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`DownloadJobRepositoryPort` (PR-044).

Schema reference: ``qai-db-schema.md`` §6.3 (model_catalog_download_job).

The download-job is its own aggregate (deliberately separate from the
catalogue entry) so this adapter persists exactly one row per save call
— no parent/child fan-out. The ``state`` column is constrained at the
SQL layer to the same six values as :class:`DownloadJobState`.

Cancellation idempotency is left to the use case layer; this adapter
treats every save as a full row replacement (UPSERT).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.model_catalog.domain.entities import DownloadJob
from qai.model_catalog.domain.ids import (
    DownloadJobId,
    ModelVersionId,
)
from qai.model_catalog.domain.value_objects import (
    DownloadJobState,
    DownloadProgress,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteDownloadJobRepository"]


_INSERT_OR_REPLACE_SQL = (
    "INSERT INTO model_catalog_download_job "
    "(id, target_model_version_id, state, bytes_downloaded, total_bytes, "
    "speed_bps, eta_seconds, failure_reason, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "target_model_version_id=excluded.target_model_version_id, "
    "state=excluded.state, "
    "bytes_downloaded=excluded.bytes_downloaded, "
    "total_bytes=excluded.total_bytes, "
    "speed_bps=excluded.speed_bps, "
    "eta_seconds=excluded.eta_seconds, "
    "failure_reason=excluded.failure_reason, "
    "updated_at=excluded.updated_at"
)


_INSERT_STRICT_SQL = (
    "INSERT INTO model_catalog_download_job "
    "(id, target_model_version_id, state, bytes_downloaded, total_bytes, "
    "speed_bps, eta_seconds, failure_reason, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class SqliteDownloadJobRepository:
    """aiosqlite implementation of :class:`DownloadJobRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def add(self, job: DownloadJob) -> None:
        params = self._params(job)
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(_INSERT_STRICT_SQL, params)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.download_job.add_failed",
                f"failed to insert download_job {job.job_id.value!r}: {exc}",
                operation="download_job.add",
                cause=exc,
            ) from exc

    async def update(self, job: DownloadJob) -> None:
        params = self._params(job)
        try:
            async with self._db.connection() as conn:
                try:
                    await conn.execute(_INSERT_OR_REPLACE_SQL, params)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.download_job.update_failed",
                f"failed to update download_job {job.job_id.value!r}: {exc}",
                operation="download_job.update",
                cause=exc,
            ) from exc

    async def find_by_id(
        self, job_id: DownloadJobId
    ) -> DownloadJob | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, target_model_version_id, state, "
                    "bytes_downloaded, total_bytes, speed_bps, eta_seconds, "
                    "failure_reason, created_at, updated_at "
                    "FROM model_catalog_download_job WHERE id = ?",
                    (job_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.download_job.find_failed",
                f"failed to load download_job {job_id.value!r}: {exc}",
                operation="download_job.find_by_id",
                cause=exc,
            ) from exc
        return None if row is None else self._row_to_job(row)

    async def list_active(self) -> list[DownloadJob]:
        """Return jobs in any non-terminal state.

        The ``ix_download_job_active`` partial index lets SQLite scan
        only the live rows even on a large history table; we order by
        ``updated_at DESC`` to mirror the index's stored sort order.
        """
        active_states = (
            DownloadJobState.QUEUED.value,
            DownloadJobState.RUNNING.value,
            DownloadJobState.PAUSED.value,
        )
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, target_model_version_id, state, "
                    "bytes_downloaded, total_bytes, speed_bps, eta_seconds, "
                    "failure_reason, created_at, updated_at "
                    "FROM model_catalog_download_job "
                    "WHERE state IN (?, ?, ?) "
                    "ORDER BY updated_at DESC",
                    active_states,
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "model_catalog.download_job.list_active_failed",
                f"failed to list active download_jobs: {exc}",
                operation="download_job.list_active",
                cause=exc,
            ) from exc
        return [self._row_to_job(r) for r in rows]

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    def _params(job: DownloadJob) -> tuple[object, ...]:
        # ``created_at`` / ``updated_at`` are not optional in the schema
        # but the domain allows ``None`` (e.g. brand-new jobs assembled
        # in tests). Persist a sentinel ISO string in that case so the
        # NOT NULL constraint is satisfied; the use case always sets
        # both before reaching the adapter in production paths.
        created_iso = (
            job.created_at.isoformat() if job.created_at is not None else ""
        )
        updated_iso = (
            job.updated_at.isoformat() if job.updated_at is not None else ""
        )
        return (
            job.job_id.value,
            job.target_model_version_id.value,
            job.state.value,
            job.progress.bytes_downloaded,
            job.progress.total_bytes,
            float(job.progress.speed_bps),
            job.progress.eta_seconds,
            job.failure_reason,
            created_iso,
            updated_iso,
        )

    @staticmethod
    def _row_to_job(row: tuple[object, ...]) -> DownloadJob:
        job_id = DownloadJobId(str(row[0]))
        target_version_id = ModelVersionId(str(row[1]))
        state = DownloadJobState(str(row[2]))
        progress = DownloadProgress(
            bytes_downloaded=int(row[3]),
            total_bytes=None if row[4] is None else int(row[4]),
            speed_bps=float(row[5] or 0.0),
            eta_seconds=None if row[6] is None else float(row[6]),
        )
        failure_reason = None if row[7] is None else str(row[7])
        created_at = _parse_iso_or_none(row[8])
        updated_at = _parse_iso_or_none(row[9])
        return DownloadJob(
            job_id=job_id,
            target_model_version_id=target_version_id,
            state=state,
            progress=progress,
            failure_reason=failure_reason,
            created_at=created_at,
            updated_at=updated_at,
        )


def _parse_iso_or_none(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return datetime.fromisoformat(text)
