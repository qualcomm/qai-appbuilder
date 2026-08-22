# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`FeedbackRepositoryPort` (S9 close).

Schema reference: ``qai-db-schema.md`` §3.8 (``app_builder_feedback``;
delivered by migration 011). Each ``Feedback`` row is independent — we
never replace a previous row in-place; an updated rating is a brand new
row with a fresh id and timestamp, so the history is auditable.

This adapter restores the rating-history surface that the legacy
``backend/app_builder/api_routes.py:1646-1691`` flow lost when it
overwrote ``app_builder_run.user_rating`` in place. Quality-score
injection (:class:`InjectQualityScoreUseCase`) reads
``list_for_run`` and folds the rows into a single signal.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.app_builder.domain.feedback import Feedback
from qai.app_builder.domain.value_objects import RunId

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteFeedbackRepository"]


_INSERT_SQL = (
    "INSERT INTO app_builder_feedback "
    "(id, run_id, rating, text, extra_json, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


_LIST_SQL = (
    "SELECT id, run_id, rating, text, extra_json, created_at "
    "FROM app_builder_feedback "
    "WHERE run_id = ? "
    "ORDER BY created_at DESC"
)


# Per-run latest rating, grouped in a single pass. ``GROUP BY run_id`` with a
# correlated tie-break on ``created_at`` then ``id`` (descending) selects the
# newest feedback row for each run; the window emulation via a self-anti-join
# keeps the query a single round-trip (avoids the N+1 fan-out of calling
# ``latest_for_run`` once per run on a history page). The ``IN (...)`` list is
# parameterised per call from the requested run ids.
def _latest_ratings_sql(n: int) -> str:
    placeholders = ",".join("?" for _ in range(n))
    return (
        "SELECT f.run_id, f.rating "
        "FROM app_builder_feedback AS f "
        "WHERE f.run_id IN (" + placeholders + ") "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM app_builder_feedback AS g "
        "  WHERE g.run_id = f.run_id "
        "  AND (g.created_at > f.created_at "
        "       OR (g.created_at = f.created_at AND g.id > f.id))"
        ")"
    )


class SqliteFeedbackRepository:
    """aiosqlite implementation of :class:`FeedbackRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def save(self, feedback: Feedback) -> None:
        params = (
            feedback.id,
            feedback.run_id.value,
            int(feedback.rating),
            feedback.text,
            json.dumps(dict(feedback.extra), ensure_ascii=False),
            feedback.created_at.isoformat(),
        )
        try:
            async with self._db.connection() as conn:
                await conn.execute(_INSERT_SQL, params)
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.feedback.save_failed",
                f"failed to save feedback {feedback.id!r}: {exc}",
                operation="feedback.save",
                cause=exc,
            ) from exc

    async def list_for_run(self, run_id: RunId) -> tuple[Feedback, ...]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(_LIST_SQL, (run_id.value,))
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.feedback.list_failed",
                f"failed to list feedback for {run_id!r}: {exc}",
                operation="feedback.list_for_run",
                cause=exc,
            ) from exc
        return tuple(self._row_to_domain(row) for row in rows)

    async def latest_ratings_for_runs(
        self, run_ids: Iterable[RunId]
    ) -> dict[str, int]:
        ids = [rid.value for rid in run_ids]
        if not ids:
            return {}
        sql = _latest_ratings_sql(len(ids))
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(sql, tuple(ids))
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.feedback.latest_ratings_failed",
                f"failed to fetch latest ratings for {len(ids)} runs: {exc}",
                operation="feedback.latest_ratings_for_runs",
                cause=exc,
            ) from exc
        return {str(row[0]): int(row[1]) for row in rows}  # type: ignore[arg-type]

    @staticmethod
    def _row_to_domain(row: tuple[object, ...]) -> Feedback:
        return Feedback(
            id=str(row[0]),
            run_id=RunId(value=str(row[1])),
            rating=int(row[2]),  # type: ignore[arg-type]
            text=str(row[3]) if row[3] is not None else "",
            extra=dict(json.loads(str(row[4] or "{}"))),
            created_at=datetime.fromisoformat(str(row[5])),
        )
