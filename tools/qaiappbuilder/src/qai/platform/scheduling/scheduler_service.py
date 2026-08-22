# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Runtime scheduler for agent-facing scheduled tasks.

Unlike :class:`~qai.platform.scheduling.background_tasks.BackgroundTaskManager`
(a *static* run-once + fixed-interval scheduler whose task set is frozen at
``start``), this service supports **runtime add / remove** and the three
schedule kinds (interval / one-shot / cron) with next-run computation. It is
driven by a single periodic *tick* loop (default 60 s) rather than one loop per
task.

Design (mirrors the platform's existing conventions):

- **Neutral core**: the scheduler knows nothing about chat / agents. It runs an
  injected async ``executor`` callback per due task and publishes a
  :class:`ScheduledTaskFired` event on the shared bus; the concrete "run one
  isolated agent turn" logic lives in the chat context and is wired in via the
  callback. Keeps ``qai.platform.scheduling`` free of any chat import.
- **At-most-once**: a due task's ``next_run_at`` is advanced *before* the
  executor runs, so a crash between advance and run drops the fire rather than
  replaying it on restart.
- **Exception isolation**: one task's failure is recorded on the row
  (``state=error``, ``last_error``) and never stops the tick loop or other
  tasks (mirrors ``BackgroundTaskManager._invoke_safely``).
- **State-Truth-First**: "is a task currently executing?" is tracked by an
  in-flight id set, not an optimistic flag; the loop's liveness is the live
  asyncio task.
- **Bounded concurrency**: due tasks in one tick run under an
  ``asyncio.Semaphore`` so a burst cannot spawn unbounded work.
- Single-process assumption (first cut): concurrency is guarded by an
  ``asyncio.Lock`` / semaphore, not a cross-process file lock.

The scheduler owns no storage logic; it reads/writes tasks through the injected
:class:`~qai.platform.scheduling.task_store.SqliteScheduledTaskStore` and
computes fire times via :func:`~qai.platform.scheduling.schedule_parser.
next_run_at`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

from qai.platform.events import EventBus
from qai.platform.logging import get_logger
from qai.platform.time import Clock, SystemClock

from .events import ScheduledTaskFired
from .schedule_parser import next_run_at
from .scheduled_task import ScheduledTask, TaskRunRecord, TaskState
from .task_store import ScheduledTaskConflictError, SqliteScheduledTaskStore

__all__ = [
    "TaskExecutor",
    "TaskRunResult",
    "TaskDeferredError",
    "SchedulerService",
]

_log = get_logger("qai.platform.scheduling")

#: Result of running one task: (ok, result_text). ``result_text`` is the run's
#: final text on success, or a short failure summary on error.
TaskRunResult = tuple[bool, str]

#: Injected executor: run one due task's prompt to completion and return the
#: outcome. Implemented in the chat context (drives an isolated agent turn).
TaskExecutor = Callable[[ScheduledTask], Awaitable[TaskRunResult]]

#: Default tick cadence (seconds).
DEFAULT_TICK_INTERVAL_SECONDS: float = 60.0

#: Default cap on concurrent task runs within (and across) ticks.
DEFAULT_MAX_CONCURRENCY: int = 4

#: Upper bound on a single task run before it is abandoned (total wall-clock,
#: not inactivity — the finer inactivity watchdog is deferred).
DEFAULT_RUN_TIMEOUT_SECONDS: float = 600.0

#: When the executor DEFERS a fire (the target tab is mid-turn — a live user
#: or another turn holds the conversation), the scheduler re-arms the task to
#: retry after this delay instead of dropping the occurrence. Kept short so a
#: reminder that lands while the user is chatting still fires soon after the
#: turn ends, and >= the min interval so it never busy-spins.
DEFAULT_DEFER_RETRY_SECONDS: float = 60.0

#: Max consecutive defers for one occurrence before the scheduler gives up on
#: THAT occurrence (a persistently busy tab must not re-arm forever). On giving
#: up: a one-shot is marked skipped-not-run (recorded, not a hard error); a
#: recurring task drops just this occurrence and proceeds to its next period.
DEFAULT_MAX_DEFERS: int = 10


class TaskDeferredError(Exception):
    """Raised by a :data:`TaskExecutor` to signal "not run — re-arm me later".

    Distinct from returning ``(False, ...)`` (a real failed run) and from
    ``(True, ...)`` (a successful run): a defer means the occurrence did NOT
    execute because the target was busy, so the scheduler must NOT consume the
    occurrence (no ``completed_runs`` bump, no terminal state) — it re-arms the
    task to retry shortly (see :data:`DEFAULT_DEFER_RETRY_SECONDS`). This is
    the "don't drop a fire just because the conversation was mid-turn" path.
    """

    def __init__(self, reason: str = "target busy") -> None:
        super().__init__(reason)
        self.reason = reason


class SchedulerService:
    """Runtime-dynamic scheduler for :class:`ScheduledTask` rows.

    Lifecycle::

        svc = SchedulerService(store=..., bus=..., executor=...)
        await svc.start()      # in lifespan startup (reloads persisted tasks)
        ...
        await svc.add(task)    # from the tool handler at runtime
        await svc.remove(id)
        ...
        await svc.shutdown()   # in lifespan shutdown

    All public coroutines are safe to call from the event-loop thread. One
    service owns one loop.
    """

    def __init__(
        self,
        *,
        store: SqliteScheduledTaskStore,
        bus: EventBus,
        executor: TaskExecutor,
        clock: Clock | None = None,
        tick_interval_seconds: float = DEFAULT_TICK_INTERVAL_SECONDS,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
        defer_retry_seconds: float = DEFAULT_DEFER_RETRY_SECONDS,
        max_defers: int = DEFAULT_MAX_DEFERS,
    ) -> None:
        if tick_interval_seconds <= 0:
            raise ValueError("tick_interval_seconds must be > 0")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be > 0")
        self._store = store
        self._bus = bus
        self._executor = executor
        self._clock: Clock = clock or SystemClock()
        self._tick_interval = float(tick_interval_seconds)
        self._run_timeout = float(run_timeout_seconds)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._loop_task: asyncio.Task[None] | None = None
        self._in_flight: set[str] = set()
        self._in_flight_lock = asyncio.Lock()
        self._started = False
        self._stopping = False
        self._defer_retry = float(defer_retry_seconds)
        self._max_defers = int(max_defers)
        # In-memory per-task consecutive-defer counter (a busy tab re-arm is a
        # short-lived condition; no need to persist across restarts). Reset to
        # 0 the moment an occurrence actually runs or is finally given up on.
        self._defer_counts: dict[str, int] = {}

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        """Start the tick loop (non-blocking). Idempotent.

        Existing persisted tasks are picked up naturally on the first tick via
        :meth:`SqliteScheduledTaskStore.due_tasks`; a task whose stored
        ``next_run_at`` is already in the past fires once on the first tick
        (subject to the one-shot grace window baked into ``next_run_at``).
        """
        if self._started:
            _log.debug("scheduling.already_started")
            return
        self._started = True
        self._stopping = False
        self._loop_task = asyncio.create_task(self._run_loop(), name="scheduling-tick")
        _log.info("scheduling.started", tick_interval_seconds=self._tick_interval)

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the tick loop and await its completion (bounded)."""
        self._stopping = True
        task = self._loop_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=timeout)
        except TimeoutError:
            _log.warning("scheduling.shutdown_timeout", timeout_seconds=timeout)

    # -- runtime task management (called by the tool handler) -------------
    async def add(self, task: ScheduledTask) -> ScheduledTask:
        """Persist a new task with its first ``next_run_at`` computed.

        Returns the stored task (version advanced). If the schedule yields no
        future run (e.g. a one-shot already past its grace window) the task is
        stored COMPLETED with a null ``next_run_at`` so it is auditable but
        never fires.
        """
        nxt = next_run_at(task.schedule, now=self._clock.now(), last_run_at=None)
        state = task.state if nxt is not None else TaskState.COMPLETED
        stored = await self._store.save(
            task.with_changes(next_run_at=nxt, state=state)
        )
        _log.info(
            "scheduling.task_added",
            task_id=stored.task_id,
            name=stored.display_name,
            kind=stored.schedule.kind.value,
            schedule=stored.schedule.display,
            next_run_at=nxt.isoformat() if nxt is not None else None,
            conversation_id=stored.conversation_id,
            tab_id=stored.tab_id,
        )
        return stored

    async def remove(self, task_id: str) -> bool:
        """Delete a task. Returns True if a row was removed."""
        return await self._store.remove(task_id)

    async def remove_by_conversation(self, conversation_id: str) -> list[str]:
        """Stop and delete every task belonging to ``conversation_id``.

        Used when a conversation is deleted: the tasks must not keep firing
        against a gone conversation (they would fail-persist forever). Deleting
        the rows stops future fires (the tick's ``due_tasks`` only reads live
        rows); a task caught mid-fire finishes that one run, and its post-run
        outcome save simply CAS-conflicts on the deleted row and is skipped.
        The in-flight guard set is cleared for the removed ids so a later id
        reuse is not falsely treated as still-running. Returns removed ids.
        """
        removed = await self._store.delete_by_conversation(conversation_id)
        if removed:
            async with self._in_flight_lock:
                for task_id in removed:
                    self._in_flight.discard(task_id)
            _log.info(
                "scheduling.removed_by_conversation",
                conversation_id=conversation_id,
                count=len(removed),
                task_ids=removed,
            )
        return removed

    async def set_enabled(self, task_id: str, enabled: bool) -> ScheduledTask | None:
        """Pause (``enabled=False``) or resume a task.

        Resuming recomputes ``next_run_at`` from *now* so a task paused across
        several missed fires does not stampede. Returns the updated task, or
        ``None`` if the id is unknown.
        """
        task = await self._store.get(task_id)
        if task is None:
            return None
        if enabled:
            nxt = next_run_at(
                task.schedule, now=self._clock.now(), last_run_at=task.last_run_at
            )
            state = TaskState.SCHEDULED if nxt is not None else TaskState.COMPLETED
            updated = task.with_changes(
                enabled=True, state=state, next_run_at=nxt
            )
        else:
            updated = task.with_changes(enabled=False, state=TaskState.PAUSED)
        return await self._store.save(updated)

    # -- tick loop --------------------------------------------------------
    async def _run_loop(self) -> None:
        """Drive the periodic tick until cancelled (exception-isolated)."""
        try:
            while not self._stopping:
                await self._tick()
                await asyncio.sleep(self._tick_interval)
        except asyncio.CancelledError:
            _log.debug("scheduling.loop_cancelled")
        except Exception:  # noqa: BLE001 — loop machinery must never crash
            _log.exception("scheduling.loop_crashed")

    async def _tick(self) -> None:
        """Fire every due task once, exception-isolated.

        Concurrency model: tasks are grouped by ``tab_id`` and each GROUP runs
        SERIALLY, while distinct groups run in parallel (bounded by
        ``max_concurrency`` inside :meth:`_run_executor`). This matters because
        the chat executor serialises writes per tab via a stream lock: two
        tasks bound to the SAME tab firing in the same tick would race for that
        lock, and the loser aborts with a tab-locked / tab-state error — fatal
        for a one-shot task (it has no next run to retry). Serialising per tab
        lets the second task fire cleanly after the first releases the lock (or
        cleanly skip via the executor's busy-check if the first is still
        streaming). Tasks in different conversations/tabs are unaffected and
        still overlap.

        First-cut limitation (by design): the loop awaits the whole batch
        before the next tick, so a long run (bounded by ``run_timeout``) delays
        the next tick. The in-flight id set still guards the (rare) overlap
        where a run outlives its tick.
        """
        now = self._clock.now()
        try:
            due = await self._store.due_tasks(now)
        except Exception:  # noqa: BLE001 — a bad read must not kill the loop
            _log.exception("scheduling.due_query_failed")
            return
        if not due:
            return
        _log.info(
            "scheduling.tick_due",
            count=len(due),
            task_ids=[t.task_id for t in due],
        )
        # Group by tab so same-tab tasks serialise; groups run in parallel.
        by_tab: dict[str, list[ScheduledTask]] = {}
        for task in due:
            by_tab.setdefault(task.tab_id, []).append(task)

        async def _run_group(group: list[ScheduledTask]) -> None:
            for task in group:
                await self._fire(task)

        groups = [_run_group(g) for g in by_tab.values()]
        await asyncio.gather(*groups, return_exceptions=True)

    async def _fire(self, task: ScheduledTask) -> None:
        """Run one due task at-most-once, record outcome, publish result.

        Steps (order is the at-most-once contract):
          1. claim the id (skip if already in flight from a prior overlapping
             tick);
          2. advance ``next_run_at`` and bump ``completed_runs`` in storage
             *before* running, so a crash mid-run does not replay the fire;
          3. run the injected executor under the concurrency semaphore with a
             total-time timeout;
          4. record the outcome (``last_status`` / ``last_error`` / terminal
             ``state``) and publish :class:`ScheduledTaskFired`.
        """
        async with self._in_flight_lock:
            if task.task_id in self._in_flight:
                return
            self._in_flight.add(task.task_id)
        try:
            advanced = await self._advance_before_run(task)
            if advanced is None:
                return  # a concurrent writer moved it; skip this fire
            _log.info(
                "scheduling.fire_start",
                task_id=advanced.task_id,
                name=advanced.display_name,
                run_no=advanced.completed_runs,
            )
            try:
                ok, text = await self._run_executor(advanced)
            except TaskDeferredError as deferred:
                await self._handle_defer(original=task, advanced=advanced, reason=deferred.reason)
                return
            # Ran to a real outcome — clear any prior defer streak.
            self._defer_counts.pop(task.task_id, None)
            run_id = await self._record_outcome(advanced, ok=ok, text=text)
            await self._publish(advanced, ok=ok, text=text, run_id=run_id)
            _log.info(
                "scheduling.fire_done",
                task_id=advanced.task_id,
                ok=ok,
                result_chars=len(text),
                next_run_at=(
                    advanced.next_run_at.isoformat()
                    if advanced.next_run_at is not None
                    else None
                ),
            )
        finally:
            async with self._in_flight_lock:
                self._in_flight.discard(task.task_id)

    async def _advance_before_run(
        self, task: ScheduledTask
    ) -> ScheduledTask | None:
        """Pre-advance next_run + completed_runs (at-most-once). Returns the
        updated aggregate, or ``None`` if the CAS lost to a concurrent writer.
        """
        now = self._clock.now()
        completed = task.completed_runs + 1
        nxt = next_run_at(task.schedule, now=now, last_run_at=now)
        exhausted = (
            task.repeat_times is not None and completed >= task.repeat_times
        )
        next_run = None if (exhausted or not task.schedule.recurring) else nxt
        state = task.state if next_run is not None else TaskState.COMPLETED
        try:
            return await self._store.save(
                task.with_changes(
                    completed_runs=completed,
                    last_run_at=now,
                    next_run_at=next_run,
                    state=state,
                )
            )
        except ScheduledTaskConflictError:
            _log.info("scheduling.fire_skipped_conflict", task_id=task.task_id)
            return None

    async def _handle_defer(
        self,
        *,
        original: ScheduledTask,
        advanced: ScheduledTask,
        reason: str,
    ) -> None:
        """Re-arm a fire the executor deferred (busy tab) — do NOT drop it.

        ``_advance_before_run`` already persisted the OCCURRENCE-CONSUMING
        advance (``completed_runs`` bumped, ``next_run_at`` moved / cleared,
        maybe ``COMPLETED``). A defer means that occurrence never ran, so we
        UNDO the advance and reschedule:

        * under the defer cap → restore ``completed_runs`` / ``state`` /
          ``enabled`` to their pre-advance values and set ``next_run_at`` to
          ``now + defer_retry`` so the SAME occurrence retries shortly (the
          reminder fires soon after the busy turn ends). ``completed_runs`` is
          NOT consumed and no terminal state is set.
        * at the cap → give up on THIS occurrence (a permanently busy tab must
          not re-arm forever): a recurring task advances to its real next
          period (dropping just this occurrence); a one-shot is recorded as
          skipped-not-run (``last_status='skipped'``) and COMPLETED — a benign
          "was busy too long", never a hard ERROR.

        The CAS baseline is ``advanced`` (the row currently persisted, whose
        version the pre-advance bumped); we re-save it with the corrected
        fields. A concurrent user edit (pause/remove) wins and is a no-op.
        """
        now = self._clock.now()
        count = self._defer_counts.get(original.task_id, 0) + 1
        if count < self._max_defers:
            self._defer_counts[original.task_id] = count
            retry_at = now + timedelta(seconds=self._defer_retry)
            try:
                await self._store.save(
                    advanced.with_changes(
                        completed_runs=original.completed_runs,
                        next_run_at=retry_at,
                        state=original.state,
                        enabled=original.enabled,
                        last_status="deferred",
                    )
                )
            except ScheduledTaskConflictError:
                _log.info("scheduling.defer_skipped_conflict", task_id=original.task_id)
                return
            _log.info(
                "scheduling.run_deferred",
                task_id=original.task_id,
                reason=reason,
                defer_count=count,
                retry_at=retry_at.isoformat(),
            )
            return

        # Cap reached — give up on this occurrence.
        self._defer_counts.pop(original.task_id, None)
        if original.schedule.recurring:
            nxt = next_run_at(original.schedule, now=now, last_run_at=now)
            exhausted = (
                original.repeat_times is not None
                and original.completed_runs >= original.repeat_times
            )
            next_run = None if exhausted else nxt
            state = original.state if next_run is not None else TaskState.COMPLETED
        else:
            next_run = None
            state = TaskState.COMPLETED
        try:
            await self._store.save(
                advanced.with_changes(
                    completed_runs=original.completed_runs,
                    next_run_at=next_run,
                    state=state,
                    last_status="skipped",
                    last_error="会话持续繁忙，本次未执行",
                )
            )
        except ScheduledTaskConflictError:
            _log.info("scheduling.defer_skipped_conflict", task_id=original.task_id)
            return
        _log.info(
            "scheduling.run_defer_gave_up",
            task_id=original.task_id,
            reason=reason,
            defer_count=count,
        )

    async def _run_executor(self, task: ScheduledTask) -> TaskRunResult:
        """Run the injected executor with a total-time bound; isolate errors."""
        async with self._semaphore:
            try:
                return await asyncio.wait_for(
                    self._executor(task), timeout=self._run_timeout
                )
            except TimeoutError:
                return False, f"scheduled task timed out after {self._run_timeout:.0f}s"
            except asyncio.CancelledError:
                raise
            except TaskDeferredError:
                # Not a failure — the executor is asking to re-arm (busy tab).
                # Propagate to _fire, which undoes the pre-advance and reschedules.
                raise
            except Exception as exc:  # noqa: BLE001 — isolate per-task failure
                _log.warning(
                    "scheduling.task_run_failed",
                    task_id=task.task_id,
                    error=str(exc),
                )
                return False, f"scheduled task failed: {exc}"

    async def _record_outcome(
        self, task: ScheduledTask, *, ok: bool, text: str
    ) -> str:
        """Persist the run outcome and resolve the resulting state.

        Returns the id of the run-record row (empty string when the append
        failed) so ``_publish`` can carry it on the WS event. Live and
        backfill clients both de-dup on this id, so it MUST match the row
        the notifications endpoint returns.
        """
        if task.next_run_at is not None:
            state = TaskState.SCHEDULED
        else:
            state = TaskState.COMPLETED if ok else TaskState.ERROR
        try:
            await self._store.save(
                task.with_changes(
                    last_status="ok" if ok else "error",
                    last_error="" if ok else text[:2000],
                    state=state,
                )
            )
        except ScheduledTaskConflictError:
            # A concurrent edit (e.g. user paused/removed mid-run) wins; the
            # outcome is best-effort audit, never a hard failure.
            _log.info("scheduling.outcome_skipped_conflict", task_id=task.task_id)
        # Append a run record (history + notification full-text + the durable
        # output of a global task). Best-effort: a run-record write failure
        # must never fail the fire — but if it succeeded the caller carries
        # the row id onto the WS event so live clients de-dup against later
        # backfill of the same fire.
        run_id = uuid.uuid4().hex
        try:
            await self._store.add_run(
                TaskRunRecord(
                    id=run_id,
                    task_id=task.task_id,
                    conversation_id=task.conversation_id or "",
                    ok=ok,
                    status="ok" if ok else "error",
                    result_text=text,
                    ran_at=self._clock.now(),
                )
            )
        except Exception:  # noqa: BLE001 — run record is best-effort audit
            _log.warning("scheduling.run_record_failed", task_id=task.task_id)
            return ""
        return run_id

    async def _publish(
        self,
        task: ScheduledTask,
        *,
        ok: bool,
        text: str,
        run_id: str,
    ) -> None:
        try:
            await self._bus.publish(
                ScheduledTaskFired(
                    run_id=run_id,
                    task_id=task.task_id,
                    task_name=task.display_name,
                    conversation_id=task.conversation_id or "",
                    tab_id=task.tab_id or "",
                    ok=ok,
                    result_text=text,
                )
            )
        except Exception:  # noqa: BLE001 — delivery is best-effort
            _log.warning("scheduling.publish_failed", task_id=task.task_id)
