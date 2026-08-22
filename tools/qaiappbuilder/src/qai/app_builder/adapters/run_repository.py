# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`RunRepositoryPort` (PR-045).

Schema reference: ``qai-db-schema.md`` §3.2 (``app_builder_run``) +
§3.3 (``app_builder_artifact``). The :class:`Run` aggregate spans both
tables, so :meth:`save` uses ``BEGIN IMMEDIATE`` to atomically replace
the run header row + its associated artifact rows.

Save semantics
--------------

::

    BEGIN IMMEDIATE;
    INSERT OR REPLACE INTO app_builder_run(...)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
    DELETE FROM app_builder_artifact WHERE run_id = ?;
    INSERT INTO app_builder_artifact(...) VALUES (?, ...);  -- per artifact
    COMMIT;

We do an explicit DELETE (rather than INSERT OR REPLACE per artifact
row) because :class:`Artifact` rows are content-addressed by ULID and
the aggregate's artifact list is the single source of truth.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from qai.platform.crypto.hashes import Hash256
from qai.platform.errors import PersistenceError
from qai.platform.ids import IdGenerator

from qai.app_builder.domain.artifact import Artifact, ArtifactKind
from qai.app_builder.domain.errors import RunNotFoundError
from qai.app_builder.domain.run import Run, RunStatus
from qai.app_builder.domain.value_objects import AppModelId, RunId

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteRunRepository"]


_RUN_INSERT_OR_REPLACE_SQL = (
    "INSERT INTO app_builder_run "
    "(id, model_id, inputs_json, status, created_at, "
    "started_at, finished_at, error_message, error_code, "
    "inference_latency_ms) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "model_id=excluded.model_id, "
    "inputs_json=excluded.inputs_json, "
    "status=excluded.status, "
    "created_at=excluded.created_at, "
    "started_at=excluded.started_at, "
    "finished_at=excluded.finished_at, "
    "error_message=excluded.error_message, "
    "error_code=excluded.error_code, "
    "inference_latency_ms=excluded.inference_latency_ms"
)


_ARTIFACT_INSERT_SQL = (
    "INSERT INTO app_builder_artifact "
    "(id, run_id, relative_path, size_bytes, kind, "
    "checksum_sha256, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


class SqliteRunRepository:
    """aiosqlite implementation of :class:`RunRepositoryPort`.

    The repository owns artifact rows for a run; the standalone
    :class:`SqliteArtifactStore` only deals with bytes (blobs are
    written separately and the resulting :class:`Artifact` is then
    attached to the run via :meth:`Run.attach_artifact`).
    """

    __slots__ = ("_db", "_ids")

    def __init__(self, *, db: "Database", ids: IdGenerator) -> None:
        self._db = db
        self._ids = ids

    async def save(self, run: Run) -> None:
        run_params = (
            run.id.value,
            run.model_id.value,
            json.dumps(dict(run.inputs)),
            run.status.value,
            run.created_at.isoformat(),
            run.started_at.isoformat() if run.started_at else None,
            run.finished_at.isoformat() if run.finished_at else None,
            run.error_message,
            run.error_code,
            run.inference_latency_ms,
        )
        artifact_params: list[tuple[object, ...]] = []
        created_at_iso = run.created_at.isoformat()
        for art in run.artifacts:
            artifact_params.append(
                (
                    self._ids.new_id(),
                    run.id.value,
                    art.path,
                    art.size_bytes,
                    art.kind.value,
                    art.checksum.value if art.checksum is not None else None,
                    created_at_iso,
                )
            )

        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(_RUN_INSERT_OR_REPLACE_SQL, run_params)
                    await conn.execute(
                        "DELETE FROM app_builder_artifact WHERE run_id = ?",
                        (run.id.value,),
                    )
                    if artifact_params:
                        await conn.executemany(
                            _ARTIFACT_INSERT_SQL, artifact_params
                        )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.run.save_failed",
                f"failed to save run {run.id!r}: {exc}",
                operation="run.save",
                cause=exc,
            ) from exc

    async def get(self, run_id: RunId) -> Run:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, model_id, inputs_json, status, created_at, "
                    "started_at, finished_at, error_message, error_code, "
                    "inference_latency_ms "
                    "FROM app_builder_run WHERE id = ?",
                    (run_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
                if row is None:
                    raise RunNotFoundError(
                        message=f"run {run_id} not found",
                        details={"run_id": str(run_id)},
                    )

                cur = await conn.execute(
                    "SELECT relative_path, size_bytes, kind, checksum_sha256 "
                    "FROM app_builder_artifact "
                    "WHERE run_id = ? "
                    "ORDER BY created_at ASC",
                    (run_id.value,),
                )
                artifact_rows = await cur.fetchall()
                await cur.close()
        except RunNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.run.get_failed",
                f"failed to load run {run_id!r}: {exc}",
                operation="run.get",
                cause=exc,
            ) from exc

        return self._build_run(row, artifact_rows)

    async def list_by_model(
        self, model_id: AppModelId, *, limit: int = 50
    ) -> tuple[Run, ...]:
        if limit <= 0:
            return ()
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, model_id, inputs_json, status, created_at, "
                    "started_at, finished_at, error_message, error_code, "
                    "inference_latency_ms "
                    "FROM app_builder_run "
                    "WHERE model_id = ? "
                    "ORDER BY created_at DESC "
                    "LIMIT ?",
                    (model_id.value, int(limit)),
                )
                run_rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.run.list_by_model_failed",
                f"failed to list runs for model {model_id!r}: {exc}",
                operation="run.list_by_model",
                cause=exc,
            ) from exc
        runs: list[Run] = []
        for row in run_rows:
            artifact_rows = await self._fetch_artifacts(str(row[0]))
            runs.append(self._build_run(row, artifact_rows))
        return tuple(runs)

    async def list_active_by_model(
        self, model_id: AppModelId
    ) -> tuple[Run, ...]:
        """Return every non-terminal run for ``model_id``.

        Backs the P3 active-run protection path in
        :class:`~qai.app_builder.application.use_cases.delete_app_model.DeleteAppModelUseCase`:
        the delete flow cancels each returned run *before* pack files are
        removed, so the NPU is freed cleanly rather than being pulled out
        from under a live inference (§🔴 State-Truth-First).

        The ``status IN (...)`` filter mirrors the same non-terminal set
        as :meth:`reconcile_stale_runs` (``pending`` / ``running`` /
        ``streaming``) so both paths agree on "what does 'in-flight' mean".
        """
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, model_id, inputs_json, status, created_at, "
                    "started_at, finished_at, error_message, error_code, "
                    "inference_latency_ms "
                    "FROM app_builder_run "
                    "WHERE model_id = ? "
                    "AND status IN ('pending', 'running', 'streaming') "
                    "ORDER BY created_at ASC",
                    (model_id.value,),
                )
                run_rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.run.list_active_by_model_failed",
                f"failed to list active runs for model {model_id!r}: {exc}",
                operation="run.list_active_by_model",
                cause=exc,
            ) from exc
        runs: list[Run] = []
        for row in run_rows:
            artifact_rows = await self._fetch_artifacts(str(row[0]))
            runs.append(self._build_run(row, artifact_rows))
        return tuple(runs)

    async def get_last_for_model(self, model_id: AppModelId) -> Run | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id, model_id, inputs_json, status, created_at, "
                    "started_at, finished_at, error_message, error_code, "
                    "inference_latency_ms "
                    "FROM app_builder_run "
                    "WHERE model_id = ? "
                    "ORDER BY created_at DESC "
                    "LIMIT 1",
                    (model_id.value,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.run.get_last_for_model_failed",
                f"failed to fetch last run for {model_id!r}: {exc}",
                operation="run.get_last_for_model",
                cause=exc,
            ) from exc
        if row is None:
            return None
        artifact_rows = await self._fetch_artifacts(str(row[0]))
        return self._build_run(row, artifact_rows)

    async def delete(self, run_id: RunId) -> None:
        # Verify the row exists first so the route layer can surface a
        # 404 deterministically (DELETE without existence check is a
        # noop in SQLite, which the route layer can't distinguish from
        # a successful delete).
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT 1 FROM app_builder_run WHERE id = ?",
                    (run_id.value,),
                )
                exists_row = await cur.fetchone()
                await cur.close()
                if exists_row is None:
                    raise RunNotFoundError(
                        message=f"run {run_id} not found",
                        details={"run_id": str(run_id)},
                    )
                # Cascade order: app_builder_artifact + app_builder_share
                # both declare ``ON DELETE CASCADE`` on app_builder_run.id
                # in migration 003, and app_builder_feedback +
                # app_builder_benchmark inherit the same cascade pattern
                # in migration 011, so a single DELETE on the run row
                # removes every downstream reference atomically. We still
                # wrap the operation in BEGIN IMMEDIATE so the cascade
                # is observably atomic against concurrent reads.
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    await conn.execute(
                        "DELETE FROM app_builder_run WHERE id = ?",
                        (run_id.value,),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except RunNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.run.delete_failed",
                f"failed to delete run {run_id!r}: {exc}",
                operation="run.delete",
                cause=exc,
            ) from exc

    async def reconcile_stale_runs(self) -> int:
        """Mark orphaned non-terminal runs as FAILED (startup sweep).

        A single UPDATE flips every ``pending`` / ``running`` / ``streaming``
        row to ``failed`` (stamping ``finished_at`` + a clear
        ``error_message``/``error_code``). These are runs whose driving
        drainer task died with the previous (uncleanly-exited) process — no
        live coroutine will ever finish them, so leaving them non-terminal
        makes the history list lie ("running" forever). State-Truth-First.

        Idempotent: when no non-terminal rows exist the UPDATE matches 0 rows
        and returns 0.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = await conn.execute(
                        "UPDATE app_builder_run SET "
                        "status = 'failed', "
                        "finished_at = COALESCE(finished_at, ?), "
                        "error_message = COALESCE(error_message, ?), "
                        "error_code = COALESCE(error_code, ?) "
                        "WHERE status IN ('pending', 'running', 'streaming')",
                        (
                            now_iso,
                            "Run interrupted by a server restart "
                            "(no active process was driving it).",
                            "RUN_INTERRUPTED",
                        ),
                    )
                    reconciled = cur.rowcount if cur.rowcount is not None else 0
                    await cur.close()
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
            return int(reconciled)
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.run.reconcile_failed",
                f"failed to reconcile stale runs: {exc}",
                operation="run.reconcile_stale",
                cause=exc,
            ) from exc

    async def _fetch_artifacts(
        self, run_id_value: str
    ) -> list[tuple[object, ...]]:
        async with self._db.connection() as conn:
            cur = await conn.execute(
                "SELECT relative_path, size_bytes, kind, checksum_sha256 "
                "FROM app_builder_artifact "
                "WHERE run_id = ? "
                "ORDER BY created_at ASC",
                (run_id_value,),
            )
            rows = await cur.fetchall()
            await cur.close()
        return list(rows)

    @staticmethod
    def _build_run(
        run_row: tuple[object, ...],
        artifact_rows: list[tuple[object, ...]],
    ) -> Run:
        artifacts = tuple(
            Artifact(
                path=str(ar[0]),
                size_bytes=int(ar[1]),
                kind=ArtifactKind(str(ar[2])),
                checksum=(
                    Hash256(value=str(ar[3]))
                    if ar[3] is not None
                    else None
                ),
            )
            for ar in artifact_rows
        )
        return Run(
            id=RunId(value=str(run_row[0])),
            model_id=AppModelId(value=str(run_row[1])),
            inputs=dict(json.loads(str(run_row[2] or "{}"))),
            status=RunStatus(str(run_row[3])),
            created_at=_parse_iso(str(run_row[4])),
            started_at=(
                _parse_iso(str(run_row[5]))
                if run_row[5] is not None
                else None
            ),
            finished_at=(
                _parse_iso(str(run_row[6]))
                if run_row[6] is not None
                else None
            ),
            artifacts=artifacts,
            error_message=(
                str(run_row[7]) if run_row[7] is not None else None
            ),
            # PR-F1 (F-15) — append-only column added in migration 022.
            # Older databases that haven't applied the migration yet
            # will surface a sqlite ``no such column`` error from the
            # SELECT a few lines above (which is the desired behaviour:
            # the migration runner runs at startup, so this is a hard
            # invariant once the process is up).
            error_code=(
                str(run_row[8])
                if len(run_row) > 8 and run_row[8] is not None
                else None
            ),
            # 缺口 #6 — append-only column added in migration 028.
            # Databases that applied 003 but not yet 028 return a shorter
            # row (no column 9); the ``len(run_row) > 9`` guard surfaces
            # ``None`` for those legacy rows so the read never crashes
            # (真实状态优先 §🔴 — honest "no latency recorded", not faked).
            inference_latency_ms=(
                float(run_row[9])
                if len(run_row) > 9 and run_row[9] is not None
                else None
            ),
        )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)
