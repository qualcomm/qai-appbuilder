# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`BenchmarkRepositoryPort` (S9 close).

Schema reference: ``qai-db-schema.md`` §3.9 (``app_builder_benchmark``;
delivered by migration 011 alongside ``app_builder_feedback``). One row
per ``POST /benchmark`` invocation; updated in place as the harness
progresses (``scheduled`` → ``running`` → ``completed`` | ``failed``).

We use ``INSERT ... ON CONFLICT(id) DO UPDATE`` so :meth:`save` is
idempotent across both the initial scheduling write and the terminal
status update written by the background harness; the row's id never
changes after the first ``schedule`` call.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import NotFoundError, PersistenceError

from qai.app_builder.application.ports import BenchmarkRecord
from qai.app_builder.domain.value_objects import AppModelId

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteBenchmarkRepository"]


_INSERT_OR_REPLACE_SQL = (
    "INSERT INTO app_builder_benchmark "
    "(id, model_id, iterations, warmup, inputs_json, status, "
    "stats_json, raw_latencies_json, error_message, "
    "created_at, finished_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "model_id=excluded.model_id, "
    "iterations=excluded.iterations, "
    "warmup=excluded.warmup, "
    "inputs_json=excluded.inputs_json, "
    "status=excluded.status, "
    "stats_json=excluded.stats_json, "
    "raw_latencies_json=excluded.raw_latencies_json, "
    "error_message=excluded.error_message, "
    "created_at=excluded.created_at, "
    "finished_at=excluded.finished_at"
)


_GET_SQL = (
    "SELECT id, model_id, iterations, warmup, inputs_json, status, "
    "stats_json, raw_latencies_json, error_message, "
    "created_at, finished_at "
    "FROM app_builder_benchmark "
    "WHERE id = ?"
)


class SqliteBenchmarkRepository:
    """aiosqlite implementation of :class:`BenchmarkRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def save(self, record: BenchmarkRecord) -> None:
        params = (
            record.id,
            record.model_id.value,
            int(record.iterations),
            int(record.warmup),
            json.dumps(dict(record.inputs), ensure_ascii=False),
            record.status,
            json.dumps(dict(record.stats), ensure_ascii=False),
            json.dumps(list(record.raw_latencies_ms), ensure_ascii=False),
            record.error_message,
            record.created_at.isoformat(),
            record.finished_at.isoformat() if record.finished_at else None,
        )
        try:
            async with self._db.connection() as conn:
                await conn.execute(_INSERT_OR_REPLACE_SQL, params)
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.benchmark.save_failed",
                f"failed to save benchmark {record.id!r}: {exc}",
                operation="benchmark.save",
                cause=exc,
            ) from exc

    async def get(self, benchmark_id: str) -> BenchmarkRecord:
        if not isinstance(benchmark_id, str) or not benchmark_id:
            raise NotFoundError(
                "app_builder.benchmark.not_found",
                "benchmark",
                str(benchmark_id),
            )
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(_GET_SQL, (benchmark_id,))
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.benchmark.get_failed",
                f"failed to load benchmark {benchmark_id!r}: {exc}",
                operation="benchmark.get",
                cause=exc,
            ) from exc
        if row is None:
            raise NotFoundError(
                "app_builder.benchmark.not_found",
                "benchmark",
                benchmark_id,
            )
        return self._row_to_domain(row)

    @staticmethod
    def _row_to_domain(row: tuple[object, ...]) -> BenchmarkRecord:
        finished_raw = row[10]
        return BenchmarkRecord(
            id=str(row[0]),
            model_id=AppModelId(value=str(row[1])),
            iterations=int(row[2]),  # type: ignore[arg-type]
            warmup=int(row[3]),  # type: ignore[arg-type]
            inputs=dict(json.loads(str(row[4] or "{}"))),
            status=str(row[5]),
            stats=dict(json.loads(str(row[6] or "{}"))),
            raw_latencies_ms=tuple(
                float(x) for x in json.loads(str(row[7] or "[]"))
            ),
            error_message=str(row[8]) if row[8] is not None else None,
            created_at=datetime.fromisoformat(str(row[9])),
            finished_at=(
                datetime.fromisoformat(str(finished_raw))
                if finished_raw is not None
                else None
            ),
        )
