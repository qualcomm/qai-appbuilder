# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed store for scheduled tasks (``scheduling_job`` table).

A multi-row aggregate: one :class:`ScheduledTask` per row, keyed by
``task_id``. :meth:`save` is a single ``BEGIN IMMEDIATE`` upsert guarded by an
optimistic-concurrency compare-and-swap on the ``version`` column — a stale
writer is rejected with :class:`ScheduledTaskConflictError` rather than
silently clobbering a concurrent update (mirrors the sub-agent session repo).

Schema reference: migration ``068_create_scheduling_schema``. All timestamps
round-trip as ISO-8601 UTC strings via ``qai.platform.time`` helpers so the
store and the scheduler's due-check share one clock.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.platform.errors import PersistenceError
from qai.platform.logging import get_logger
from qai.platform.time import from_iso8601, to_iso8601

from .scheduled_task import (
    Schedule,
    ScheduleKind,
    ScheduledTask,
    TaskRunRecord,
    TaskState,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = [
    "ScheduledTaskConflictError",
    "ScheduledTaskNotFoundError",
    "SqliteScheduledTaskStore",
]

_log = get_logger("qai.platform.scheduling")

_COLUMNS = (
    "id, name, prompt, schedule_kind, schedule_display, run_at, "
    "interval_seconds, cron_expr, conversation_id, tab_id, repeat_times, "
    "completed_runs, enabled, state, created_at, next_run_at, last_run_at, "
    "last_status, last_error, version, model_id, enabled_tools, enabled_skills, "
    "start_at, tz_offset_minutes"
)


class ScheduledTaskConflictError(RuntimeError):
    """Raised when a save loses the optimistic-lock CAS (concurrent update)."""

    def __init__(self, task_id: str, *, expected_version: int) -> None:
        super().__init__(
            f"scheduled task {task_id!r} was modified concurrently "
            f"(expected version {expected_version})"
        )
        self.task_id = task_id
        self.expected_version = expected_version


class ScheduledTaskNotFoundError(LookupError):
    """Raised when an operation targets a task id that does not exist."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"scheduled task {task_id!r} not found")
        self.task_id = task_id


def _iso_or_none(dt: datetime | None) -> str | None:
    return to_iso8601(dt) if dt is not None else None


def _dt_or_none(value: Any) -> datetime | None:
    return from_iso8601(value) if value else None


def _names_to_json(names: tuple[str, ...]) -> str | None:
    """Serialise a name whitelist for storage: empty tuple -> ``NULL``."""
    return json.dumps(list(names), ensure_ascii=False) if names else None


def _json_to_names(value: Any) -> tuple[str, ...]:
    """Deserialise a stored whitelist: ``NULL`` / empty / malformed -> ``()``."""
    if not value:
        return ()
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return ()
    if not isinstance(loaded, (list, tuple)):
        return ()
    return tuple(str(n) for n in loaded if n)


def _row_to_task(row: Any) -> ScheduledTask:
    """Rebuild a :class:`ScheduledTask` from a ``scheduling_job`` row tuple."""
    (
        task_id,
        name,
        prompt,
        schedule_kind,
        schedule_display,
        run_at,
        interval_seconds,
        cron_expr,
        conversation_id,
        tab_id,
        repeat_times,
        completed_runs,
        enabled,
        state,
        created_at,
        next_run_at,
        last_run_at,
        last_status,
        last_error,
        version,
        model_id,
        enabled_tools,
        enabled_skills,
        start_at,
        tz_offset_minutes,
    ) = row
    kind = ScheduleKind(schedule_kind)
    schedule = Schedule(
        kind=kind,
        display=schedule_display,
        run_at=_dt_or_none(run_at) if kind is ScheduleKind.ONCE else None,
        interval_seconds=(
            float(interval_seconds) if kind is ScheduleKind.INTERVAL else None
        ),
        cron_expr=cron_expr if kind is ScheduleKind.CRON else None,
        # Wall-clock intent (migration 074). Both NULL on pre-074 rows, which
        # is exactly the legacy behaviour: no explicit first run, UTC anchoring.
        start_at=_dt_or_none(start_at),
        tz_offset_minutes=(
            int(tz_offset_minutes) if tz_offset_minutes is not None else None
        ),
    )
    return ScheduledTask(
        task_id=task_id,
        prompt=prompt,
        schedule=schedule,
        conversation_id=conversation_id or None,
        tab_id=tab_id or None,
        name=name or "",
        model_id=model_id or None,
        repeat_times=int(repeat_times) if repeat_times is not None else None,
        completed_runs=int(completed_runs),
        enabled=bool(int(enabled)),
        state=TaskState(state),
        created_at=from_iso8601(created_at),
        next_run_at=_dt_or_none(next_run_at),
        last_run_at=_dt_or_none(last_run_at),
        last_status=last_status or "",
        last_error=last_error or "",
        version=int(version),
        enabled_tools=_json_to_names(enabled_tools),
        enabled_skills=_json_to_names(enabled_skills),
    )


def _row_to_run_record(row: Any) -> TaskRunRecord:
    """Rebuild a :class:`TaskRunRecord` from a ``scheduling_task_run`` row tuple.

    Column order MUST match every ``SELECT`` this module issues against the
    run table — a mismatch here silently ships the wrong data (SQLite is
    positional, not named). Kept as ONE helper so adding a column is one
    edit, not four.
    """
    (
        run_id,
        task_id,
        conversation_id,
        ok,
        status,
        result_text,
        ran_at,
        notified_at,
    ) = row
    return TaskRunRecord(
        id=run_id,
        task_id=task_id,
        conversation_id=conversation_id or "",
        ok=bool(int(ok)),
        status=status or "",
        result_text=result_text or "",
        ran_at=from_iso8601(ran_at),
        notified_at=_dt_or_none(notified_at),
    )


class SqliteScheduledTaskStore:
    """aiosqlite implementation of the scheduled-task repository."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    # -- write ------------------------------------------------------------
    async def save(self, task: ScheduledTask) -> ScheduledTask:
        """Insert or update ``task`` under an optimistic-lock CAS on ``version``.

        Returns a copy of ``task`` with ``version`` advanced to the persisted
        value (frozen aggregate — the caller must use the returned instance for
        any follow-up save to keep the CAS chain intact).

        Raises:
            ScheduledTaskConflictError: if an existing row's stored version no
                longer matches ``task.version`` (a concurrent writer won).
            PersistenceError: on any underlying database failure.
        """
        sched = task.schedule
        expected_version = int(task.version)
        values_tail = (
            task.name or "",
            task.prompt,
            sched.kind.value,
            sched.display,
            _iso_or_none(sched.run_at),
            sched.interval_seconds,
            sched.cron_expr,
            task.conversation_id or "",
            task.tab_id or "",
            task.repeat_times,
            int(task.completed_runs),
            1 if task.enabled else 0,
            task.state.value,
            _iso_or_none(task.next_run_at),
            _iso_or_none(task.last_run_at),
            task.last_status or "",
            task.last_error or "",
            task.model_id,
            _names_to_json(task.enabled_tools),
            _names_to_json(task.enabled_skills),
            # Wall-clock intent (migration 074) — appended last so the shared
            # tail keeps its positional contract with both statements below.
            _iso_or_none(sched.start_at),
            sched.tz_offset_minutes,
        )
        try:
            async with self._db.connection() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = await conn.execute(
                        "SELECT version FROM scheduling_job WHERE id = ?",
                        (task.task_id,),
                    )
                    row = await cur.fetchone()
                    await cur.close()
                    if row is None:
                        await conn.execute(
                            "INSERT INTO scheduling_job ("
                            "id, name, prompt, schedule_kind, schedule_display, "
                            "run_at, interval_seconds, cron_expr, "
                            "conversation_id, tab_id, repeat_times, "
                            "completed_runs, enabled, state, next_run_at, "
                            "last_run_at, last_status, last_error, model_id, "
                            "enabled_tools, enabled_skills, "
                            "start_at, tz_offset_minutes, "
                            "created_at, version) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                task.task_id,
                                *values_tail,
                                to_iso8601(task.created_at),
                                expected_version,
                            ),
                        )
                        new_version = expected_version
                    else:
                        new_version = expected_version + 1
                        upd = await conn.execute(
                            "UPDATE scheduling_job SET "
                            " name=?, prompt=?, schedule_kind=?, "
                            " schedule_display=?, run_at=?, interval_seconds=?, "
                            " cron_expr=?, conversation_id=?, tab_id=?, "
                            " repeat_times=?, completed_runs=?, enabled=?, "
                            " state=?, next_run_at=?, last_run_at=?, "
                            " last_status=?, last_error=?, model_id=?, "
                            " enabled_tools=?, enabled_skills=?, "
                            " start_at=?, tz_offset_minutes=?, "
                            " version=? "
                            "WHERE id=? AND version=?",
                            (
                                *values_tail,
                                new_version,
                                task.task_id,
                                expected_version,
                            ),
                        )
                        affected = upd.rowcount
                        await upd.close()
                        if affected == 0:
                            await conn.rollback()
                            raise ScheduledTaskConflictError(
                                task.task_id, expected_version=expected_version
                            )
                    await conn.commit()
                except ScheduledTaskConflictError:
                    raise
                except Exception:
                    await conn.rollback()
                    raise
        except ScheduledTaskConflictError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "scheduling.job.save_failed",
                f"failed to save scheduled task {task.task_id!r}: {exc}",
                operation="scheduling.job.save",
                cause=exc,
            ) from exc
        return task.with_changes(version=new_version)

    async def remove(self, task_id: str) -> bool:
        """Delete a task by id. Returns True if a row was removed."""
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM scheduling_job WHERE id = ?", (task_id,)
                )
                affected = cur.rowcount
                await cur.close()
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "scheduling.job.remove_failed",
                f"failed to remove scheduled task {task_id!r}: {exc}",
                operation="scheduling.job.remove",
                cause=exc,
            ) from exc
        return affected > 0

    async def delete_by_conversation(self, conversation_id: str) -> list[str]:
        """Delete every task targeting ``conversation_id``.

        Returns the ids of the removed tasks so the caller (conversation
        delete) can also cancel their live in-flight runs / scheduled loops.
        """
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT id FROM scheduling_job WHERE conversation_id = ?",
                    (conversation_id,),
                )
                rows = await cur.fetchall()
                await cur.close()
                ids = [r[0] for r in rows]
                if ids:
                    await conn.execute(
                        "DELETE FROM scheduling_job WHERE conversation_id = ?",
                        (conversation_id,),
                    )
                    await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "scheduling.job.delete_by_conversation_failed",
                f"failed to delete tasks for conversation "
                f"{conversation_id!r}: {exc}",
                operation="scheduling.job.delete_by_conversation",
                cause=exc,
            ) from exc
        return ids

    # -- read -------------------------------------------------------------
    async def get(self, task_id: str) -> ScheduledTask | None:
        """Load one task by id, or ``None`` if it does not exist."""
        async with self._db.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM scheduling_job WHERE id = ?",
                (task_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        return _row_to_task(row) if row is not None else None

    async def list_all(self) -> list[ScheduledTask]:
        """Load every task (ordered by creation time)."""
        return await self._query(
            f"SELECT {_COLUMNS} FROM scheduling_job ORDER BY created_at ASC"
        )

    async def list_by_conversation(
        self, conversation_id: str
    ) -> list[ScheduledTask]:
        """Load every task targeting ``conversation_id`` (creation order)."""
        return await self._query(
            f"SELECT {_COLUMNS} FROM scheduling_job "
            "WHERE conversation_id = ? ORDER BY created_at ASC",
            (conversation_id,),
        )

    async def due_tasks(self, now: datetime) -> list[ScheduledTask]:
        """Load enabled, SCHEDULED tasks whose ``next_run_at`` is due (<= now).

        Paused / completed / errored rows and rows with a NULL ``next_run_at``
        (no further runs) are excluded at the SQL level; ordering by
        ``next_run_at`` fires the most-overdue task first.
        """
        return await self._query(
            f"SELECT {_COLUMNS} FROM scheduling_job "
            "WHERE enabled = 1 AND state = ? "
            "AND next_run_at IS NOT NULL AND next_run_at <= ? "
            "ORDER BY next_run_at ASC",
            (TaskState.SCHEDULED.value, to_iso8601(now)),
        )

    # -- run records ------------------------------------------------------
    async def add_run(self, record: TaskRunRecord) -> None:
        """Append one run-history row (best-effort audit; never CAS-guarded).

        Recorded on every fire so the notification center / run-records view /
        global-task output all read one durable source. A write failure is
        raised as :class:`PersistenceError` by the caller's wrapper — the
        scheduler treats run-record persistence as best-effort and swallows.

        A fresh row is written UNREAD unconditionally: ``notified_at`` is
        hard-coded to the ``''`` sentinel in the INSERT (the ``record.notified_at``
        field is IGNORED by this method — every real fire produces an
        unread row, and reads/updates happen through the dedicated
        list/mark methods, never round-tripping through ``add_run``). See
        migration 075: the WebUI's notification bell fetches all unread
        rows on WS connect and again on every reconnect, so a fire that
        happened while the client's WebSocket was silently dead surfaces
        the next time it comes back. WS push remains the fast path for a
        live client, but the row IS the durable source of truth — the WS
        event is only an optimisation.
        """
        async with self._db.connection() as conn:
            await conn.execute(
                "INSERT INTO scheduling_task_run "
                "(id, task_id, conversation_id, ok, status, result_text, "
                "ran_at, notified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, '')",
                (
                    record.id,
                    record.task_id,
                    record.conversation_id or "",
                    1 if record.ok else 0,
                    record.status or "",
                    record.result_text or "",
                    to_iso8601(record.ran_at),
                ),
            )
            await conn.commit()

    async def list_runs(
        self, task_id: str, *, limit: int = 50
    ) -> list[TaskRunRecord]:
        """Return one task's run records, newest first (capped by ``limit``)."""
        async with self._db.connection() as conn:
            cur = await conn.execute(
                "SELECT id, task_id, conversation_id, ok, status, result_text, "
                "ran_at, notified_at FROM scheduling_task_run "
                "WHERE task_id = ? ORDER BY ran_at DESC LIMIT ?",
                (task_id, int(limit)),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_run_record(r) for r in rows]

    async def list_unread_runs(
        self, *, limit: int = 200
    ) -> list[TaskRunRecord]:
        """Return every unread run record across all tasks, newest first.

        Backs ``GET /api/scheduled-tasks/notifications/unread``: the frontend
        calls this on WS (re)connect to backfill the notification bell with
        anything the live WS transport missed. ``limit`` is a hard safety cap
        (a very stale client that has been offline for weeks should not pull
        an unbounded blob); production traffic sits at single-digit unread
        counts so the cap is never observed.
        """
        async with self._db.connection() as conn:
            cur = await conn.execute(
                "SELECT id, task_id, conversation_id, ok, status, result_text, "
                "ran_at, notified_at FROM scheduling_task_run "
                "WHERE notified_at = '' "
                "ORDER BY ran_at DESC LIMIT ?",
                (int(limit),),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_run_record(r) for r in rows]

    async def mark_run_read(
        self, run_id: str, *, at: datetime | None = None
    ) -> bool:
        """Stamp ONE run row as read; return ``True`` iff the row existed AND
        was previously unread (so the endpoint can distinguish "no-op idempotent
        re-dismiss" from "there is no such run"; the WebUI treats both as
        success but tests need the distinction). Idempotent — a re-mark on an
        already-read row is a no-op that returns ``False``.
        """
        stamp = to_iso8601(at if at is not None else datetime.now(timezone.utc))
        async with self._db.connection() as conn:
            cur = await conn.execute(
                "UPDATE scheduling_task_run SET notified_at = ? "
                "WHERE id = ? AND notified_at = ''",
                (stamp, run_id),
            )
            await conn.commit()
            changed = cur.rowcount or 0
            await cur.close()
        return changed > 0

    async def mark_all_runs_read(
        self, *, at: datetime | None = None
    ) -> int:
        """Stamp every currently-unread run as read; return the count updated.

        Used by the notification center's "mark all as read" bulk action. A
        run that becomes unread AFTER this call (i.e. a fire that lands mid-
        request) is not swept — the next bulk mark will pick it up. There is
        no locking; the partial-index scan is O(unread) so contention is
        negligible in practice.
        """
        stamp = to_iso8601(at if at is not None else datetime.now(timezone.utc))
        async with self._db.connection() as conn:
            cur = await conn.execute(
                "UPDATE scheduling_task_run SET notified_at = ? "
                "WHERE notified_at = ''",
                (stamp,),
            )
            await conn.commit()
            changed = cur.rowcount or 0
            await cur.close()
        return changed

    async def _query(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[ScheduledTask]:
        async with self._db.connection() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_task(r) for r in rows]
