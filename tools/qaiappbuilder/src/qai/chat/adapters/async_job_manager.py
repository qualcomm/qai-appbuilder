# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Process-wide registry + admission control for asynchronous agent jobs.

A single :class:`AsyncJobManager` owns the answer to three questions the
agent loop and the REST/SSE routes keep asking about *asynchronous* work
(a spawned sub-agent, a backgrounded ``bash`` run):

1. **What is running for this owner right now?**  ``list_by_owner`` /
   ``get`` are the only lookup surface; no caller keeps a parallel dict.
2. **May this job start immediately?**  Admission is bounded by ONE
   process-wide ``asyncio.Semaphore(max_running)``.  A job registered
   while the cap is saturated lands in :attr:`AsyncJobStatus.QUEUED` and
   is promoted to :attr:`AsyncJobStatus.RUNNING` — in FIFO order — as
   soon as another job reaches a terminal state.  Without one shared cap,
   N tabs x M spawned sub-agents would fan out independently and storm
   the machine.
3. **Who wants to hear about the completion?**  Delivery sinks are
   registered *per owner* (``register_delivery_sink`` returns a disposer,
   so an SSE connection detaching cannot leak a sink).  ``complete_job``
   publishes exactly one :class:`AsyncJobCompleted` to the owner's live
   sinks — unless the job was explicitly marked
   :meth:`mark_delivery_suppressed` (the caller already delivered the
   result inline, e.g. a foreground ``await`` on the spawned agent).

Design principles
=================

* **One lock, one truth** — every mutation of :attr:`_jobs` (status
  transitions, semaphore accounting, FIFO promotion) happens under a
  single ``asyncio.Lock``.  Status transition and slot release can never
  interleave, so the "running" count and the semaphore value cannot
  drift apart.
* **Terminal is terminal** — ``COMPLETED`` / ``FAILED`` / ``CANCELLED``
  are absorbing.  Re-completing or cancelling a terminal job is a no-op
  (idempotent), which keeps double-firing producers harmless.
* **Never block on admission** — neither ``register_job`` nor
  ``mark_running`` ever awaits a free slot.  Callers observe the
  ``QUEUED`` status and are promoted asynchronously.  A method that
  awaited the semaphore would turn a saturated cap into a deadlock for
  whoever holds the caller's turn.
* **Dead letters get collected** — a terminal job whose owner has NO
  live delivery sink is a dead letter: nobody will ever read it.  It is
  evicted ``retention_seconds`` after completion so a long-lived process
  does not accumulate every job it ever ran.  The retention window
  itself exists so a *reconnecting* consumer (tab reload) still finds a
  just-finished job.
* **No domain -> adapter import** — the :class:`AsyncJob.error` type
  lives in the domain layer and is referenced under
  ``if TYPE_CHECKING:`` only, so importing this adapter never drags the
  domain error module in at runtime.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from qai.platform.logging import get_logger

if TYPE_CHECKING:
    from qai.chat.domain.sub_agent_error import SubAgentError

__all__ = [
    "AsyncJob",
    "AsyncJobCompleted",
    "AsyncJobKind",
    "AsyncJobManager",
    "AsyncJobStatus",
    "DeliverySink",
]

_log = get_logger(__name__)

#: How often :meth:`AsyncJobManager.dead_letter_evict_loop` runs one
#: eviction pass.  Independent of ``retention_seconds`` (the age a
#: terminal job must reach before it is evictable).
_EVICT_INTERVAL_SECONDS = 30.0


class AsyncJobKind(str, Enum):
    """What kind of asynchronous work a job stands for."""

    SUBAGENT = "subagent"
    BASH = "bash"


class AsyncJobStatus(str, Enum):
    """Lifecycle of an asynchronous job.

    ``QUEUED`` -> ``RUNNING`` -> one of ``COMPLETED`` / ``FAILED`` /
    ``CANCELLED``.  The three terminal states are absorbing.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: The terminal (absorbing) statuses.  A job in one of these never
#: transitions again and holds no admission slot.
_TERMINAL: frozenset[AsyncJobStatus] = frozenset(
    {
        AsyncJobStatus.COMPLETED,
        AsyncJobStatus.FAILED,
        AsyncJobStatus.CANCELLED,
    }
)


@dataclass(slots=True)
class AsyncJob:
    """One tracked asynchronous job.

    ``registered_at`` / ``completed_at`` are ``time.monotonic()`` stamps
    (never wall clock): the only thing ever computed from them is an
    elapsed retention age, which must survive a system clock jump.
    """

    id: str
    owner_id: str
    agent_id: str | None
    kind: AsyncJobKind
    status: AsyncJobStatus
    registered_at: float
    completed_at: float | None
    result_text: str | None
    error: "SubAgentError | None"
    delivery_suppressed: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL


@dataclass(frozen=True, slots=True)
class AsyncJobCompleted:
    """Event published to an owner's delivery sinks on completion."""

    job_id: str
    owner_id: str
    kind: AsyncJobKind
    result_text: str | None
    error: "SubAgentError | None"


#: A delivery sink receives one :class:`AsyncJobCompleted` per completed
#: job of the owner it was registered for.  Sync and async sinks are both
#: accepted (an awaitable return value is awaited).
DeliverySink = Callable[[AsyncJobCompleted], Awaitable[None] | None]


@dataclass(slots=True)
class _SinkRegistration:
    """A live sink plus the owner it listens for (for O(1) disposal)."""

    owner_id: str
    sink: DeliverySink


class AsyncJobManager:
    """Registry + admission control + completion fan-out for async jobs.

    Args:
        max_running: Hard cap on concurrently ``RUNNING`` jobs across the
            whole process.  Registering beyond it yields ``QUEUED`` jobs
            promoted in FIFO order as slots free up.
        retention_seconds: How long a terminal job is kept after
            completion before it becomes evictable as a dead letter.
    """

    __slots__ = (
        "_jobs",
        "_lock",
        "_next_sink_token",
        "_retention_seconds",
        "_running_semaphore",
        "_sinks",
        "_waiting",
        "evict_task",
    )

    def __init__(
        self,
        *,
        max_running: int,
        retention_seconds: float = 30.0,
    ) -> None:
        self._jobs: dict[str, AsyncJob] = {}
        # Sink registrations keyed by an opaque monotonically increasing
        # token so the disposer returned by ``register_delivery_sink``
        # removes EXACTLY its own registration — two identical sinks for
        # the same owner stay independently disposable.
        self._sinks: dict[int, _SinkRegistration] = {}
        self._next_sink_token = 0
        # FIFO of job ids waiting for an admission slot.  Ordering is the
        # whole point: a job queued first must not be starved by a later
        # registration.
        self._waiting: list[str] = []
        self._running_semaphore = asyncio.Semaphore(max_running)
        self._retention_seconds = float(retention_seconds)
        self._lock = asyncio.Lock()
        # Set by the application lifespan after it spawns
        # ``dead_letter_evict_loop`` (this factory is built from SYNC DI
        # code, where no event loop is running yet), so shutdown can
        # cancel the loop without a second bookkeeping dict.
        self.evict_task: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------
    # Registration / lifecycle
    # ---------------------------------------------------------------

    async def register_job(
        self,
        kind: AsyncJobKind,
        owner_id: str,
        agent_id: str | None = None,
    ) -> str:
        """Track a new job and try to admit it immediately.

        Returns the generated job id.  The job's :attr:`AsyncJob.status`
        is ``RUNNING`` when an admission slot was free, else ``QUEUED``
        (see :meth:`get`).  Never blocks on the cap.
        """
        job_id = uuid.uuid4().hex
        async with self._lock:
            admitted = await self._try_acquire_slot()
            job = AsyncJob(
                id=job_id,
                owner_id=owner_id,
                agent_id=agent_id,
                kind=kind,
                status=(
                    AsyncJobStatus.RUNNING if admitted else AsyncJobStatus.QUEUED
                ),
                registered_at=time.monotonic(),
                completed_at=None,
                result_text=None,
                error=None,
            )
            self._jobs[job_id] = job
            if not admitted:
                self._waiting.append(job_id)
        _log.info(
            "ajm.job_registered",
            extra={
                "job_id": job_id,
                "owner_id": owner_id,
                "agent_id": agent_id,
                "kind": kind.value,
                "status": job.status.value,
            },
        )
        return job_id

    async def mark_running(self, job_id: str) -> None:
        """Promote a ``QUEUED`` job to ``RUNNING`` if a slot is free.

        Idempotent: an already-``RUNNING`` or terminal job is untouched.
        A ``QUEUED`` job stays queued when the cap is saturated — this
        method deliberately does NOT await a slot, so the caller can
        never deadlock the cap it is waiting on.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status is not AsyncJobStatus.QUEUED:
                return
            if not await self._try_acquire_slot():
                return
            job.status = AsyncJobStatus.RUNNING
            self._discard_waiting(job_id)

    async def complete_job(
        self,
        job_id: str,
        result: str | None = None,
        error: "SubAgentError | None" = None,
    ) -> None:
        """Drive a job to its terminal state and fan out the completion.

        ``error`` non-``None`` means ``FAILED``, else ``COMPLETED``.
        Releases the job's admission slot and promotes the next queued
        job (FIFO).  Publishes exactly one :class:`AsyncJobCompleted` to
        the owner's live sinks unless the job was marked
        :meth:`mark_delivery_suppressed`.  A terminal job is a no-op.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                return
            held_slot = job.status is AsyncJobStatus.RUNNING
            job.status = (
                AsyncJobStatus.FAILED if error is not None else AsyncJobStatus.COMPLETED
            )
            job.result_text = result
            job.error = error
            job.completed_at = time.monotonic()
            self._discard_waiting(job_id)
            if held_slot:
                await self._release_slot()
            suppressed = job.delivery_suppressed
            event = AsyncJobCompleted(
                job_id=job.id,
                owner_id=job.owner_id,
                kind=job.kind,
                result_text=job.result_text,
                error=job.error,
            )
            sinks = self._live_sinks(job.owner_id)
        _log.info(
            "ajm.job_completed",
            extra={
                "job_id": job_id,
                "owner_id": event.owner_id,
                "kind": event.kind.value,
                "status": job.status.value,
                "delivery_suppressed": suppressed,
            },
        )
        if suppressed:
            return
        # Fan-out happens OUTSIDE the lock: a sink is caller code (SSE
        # push, dispatcher hop) and must never be able to stall every
        # other job's bookkeeping.
        await self._publish(event, sinks)

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel EXACTLY one job by id; ``True`` when it transitioned.

        The per-job counterpart of :meth:`cancel_by_owner`, needed because
        one sub-agent's stop must not touch its siblings sharing the same
        ``owner_id`` (conversation).  ``CANCELLED`` is kept distinct from
        ``FAILED``: a user-driven stop and a sub-agent that blew up are
        different terminal facts, and readers (status queries, error
        rendering) branch on that difference.

        Idempotent: an already-terminal or unknown job returns ``False``
        with no state change.  Like :meth:`cancel_by_owner`, no
        :class:`AsyncJobCompleted` is published — a cancellation is the
        owner's own doing, there is nothing to deliver back to it.
        """
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal:
                return False
            await self._cancel_locked(job)
        _log.info(
            "ajm.job_cancelled",
            extra={"owner_id": job.owner_id, "count": 1, "job_ids": [job_id]},
        )
        return True

    async def cancel_by_owner(self, owner_id: str) -> int:
        """Cancel every non-terminal job of ``owner_id``.

        Returns how many jobs were transitioned.  Other owners' jobs are
        untouched.  No completion event is published — a cancellation is
        the owner's own doing, there is nothing to deliver back to it.
        """
        async with self._lock:
            cancelled: list[str] = []
            for job in self._jobs.values():
                if job.owner_id != owner_id or job.is_terminal:
                    continue
                await self._cancel_locked(job)
                cancelled.append(job.id)
        if cancelled:
            _log.info(
                "ajm.job_cancelled",
                extra={
                    "owner_id": owner_id,
                    "count": len(cancelled),
                    "job_ids": cancelled,
                },
            )
        return len(cancelled)

    # ---------------------------------------------------------------
    # Delivery
    # ---------------------------------------------------------------

    def register_delivery_sink(
        self,
        owner_id: str,
        sink: DeliverySink,
    ) -> Callable[[], None]:
        """Subscribe ``sink`` to ``owner_id``'s completions.

        Returns an idempotent disposer.  Callers MUST invoke it when the
        consumer goes away (SSE disconnect, tab close); a leaked sink
        both keeps firing and pins the owner's terminal jobs out of
        dead-letter eviction.
        """
        token = self._next_sink_token
        self._next_sink_token += 1
        self._sinks[token] = _SinkRegistration(owner_id=owner_id, sink=sink)

        def _dispose() -> None:
            self._sinks.pop(token, None)

        return _dispose

    def mark_delivery_suppressed(self, job_id: str) -> None:
        """Stop a later :meth:`complete_job` from publishing an event.

        Used when the caller consumes the result inline (a foreground
        ``await`` on the job), so the owner would otherwise be told twice.
        """
        job = self._jobs.get(job_id)
        if job is not None:
            job.delivery_suppressed = True

    # ---------------------------------------------------------------
    # Queries
    # ---------------------------------------------------------------

    def list_by_owner(
        self,
        owner_id: str,
        kind: AsyncJobKind | None = None,
    ) -> list[AsyncJob]:
        """Every tracked job of ``owner_id``, registration order."""
        return [
            job
            for job in self._jobs.values()
            if job.owner_id == owner_id and (kind is None or job.kind is kind)
        ]

    def get(self, job_id: str) -> AsyncJob | None:
        """The tracked job, or ``None`` once evicted / never registered."""
        return self._jobs.get(job_id)

    # ---------------------------------------------------------------
    # Dead-letter eviction
    # ---------------------------------------------------------------

    async def evict_dead_letters(self) -> int:
        """Run ONE eviction pass; returns how many jobs were dropped.

        A job is evicted when it is terminal, completed longer ago than
        ``retention_seconds``, and its owner has no live delivery sink —
        i.e. nobody is ever going to read it.  Split out from
        :meth:`dead_letter_evict_loop` so the pass is directly
        exercisable (and testable) without waiting on the interval.
        """
        now = time.monotonic()
        async with self._lock:
            doomed = [
                job.id
                for job in self._jobs.values()
                if job.is_terminal
                and job.completed_at is not None
                and (now - job.completed_at) > self._retention_seconds
                and not self._live_sinks(job.owner_id)
            ]
            for job_id in doomed:
                self._jobs.pop(job_id, None)
        if doomed:
            _log.info(
                "ajm.dead_letter_evicted",
                extra={"count": len(doomed), "job_ids": doomed},
            )
        return len(doomed)

    async def dead_letter_evict_loop(self) -> None:
        """Background task: evict dead letters every 30s until cancelled.

        Exits quietly on cancellation (no traceback) so shutdown stays
        silent, and never lets a sink/logging failure kill the loop.
        """
        try:
            while True:
                await asyncio.sleep(_EVICT_INTERVAL_SECONDS)
                try:
                    await self.evict_dead_letters()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — the loop must survive
                    _log.warning("ajm.dead_letter_evict_failed", exc_info=True)
        except asyncio.CancelledError:
            return

    # ---------------------------------------------------------------
    # Internals (all callers hold ``self._lock`` unless noted)
    # ---------------------------------------------------------------

    async def _try_acquire_slot(self) -> bool:
        """Take an admission slot if one is free, without ever suspending.

        ``Semaphore.acquire()`` returns without yielding to the loop
        whenever the counter is positive and nobody is queued on it (and
        nobody ever is here — we never await a saturated semaphore).  So
        guarding on ``locked()`` first makes this a genuine try-acquire
        with no interleaving window, while keeping the counter the ONE
        source of truth for "how many jobs are running".
        """
        if self._running_semaphore.locked():
            return False
        await self._running_semaphore.acquire()
        return True

    async def _release_slot(self) -> None:
        """Give a slot back and promote the oldest queued job, if any."""
        self._running_semaphore.release()
        while self._waiting:
            next_id = self._waiting.pop(0)
            job = self._jobs.get(next_id)
            if job is None or job.status is not AsyncJobStatus.QUEUED:
                # Stale entry (cancelled while queued) — skip it and keep
                # looking for a genuine candidate.
                continue
            # Cannot fail: we hold ``self._lock`` and just released a
            # slot, so the counter is positive and unqueued.
            await self._try_acquire_slot()
            job.status = AsyncJobStatus.RUNNING
            return

    async def _cancel_locked(self, job: AsyncJob) -> None:
        """Drive one non-terminal ``job`` to ``CANCELLED``.

        The shared body of :meth:`cancel_job` and
        :meth:`cancel_by_owner` — one place decides what cancelling costs
        (drop any queue entry, hand back the admission slot so the next
        queued job is promoted FIFO).  Caller holds ``self._lock`` and has
        already established the job is non-terminal.
        """
        held_slot = job.status is AsyncJobStatus.RUNNING
        job.status = AsyncJobStatus.CANCELLED
        job.completed_at = time.monotonic()
        self._discard_waiting(job.id)
        if held_slot:
            await self._release_slot()

    def _discard_waiting(self, job_id: str) -> None:
        if job_id in self._waiting:
            self._waiting.remove(job_id)

    def _live_sinks(self, owner_id: str) -> list[DeliverySink]:
        return [
            reg.sink for reg in self._sinks.values() if reg.owner_id == owner_id
        ]

    async def _publish(
        self,
        event: AsyncJobCompleted,
        sinks: list[DeliverySink],
    ) -> None:
        for sink in sinks:
            try:
                outcome = sink(event)
                if inspect.isawaitable(outcome):
                    await outcome
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one bad sink must not
                # starve the remaining sinks of this completion.
                _log.warning(
                    "ajm.delivery_sink_failed",
                    extra={"job_id": event.job_id, "owner_id": event.owner_id},
                    exc_info=True,
                )
