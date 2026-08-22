# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-conversation write serialiser for the chat persistence path.

Why this exists
===============

Several independent producers write the SAME conversation concurrently:

* the streaming turn's incremental round persistence
  (``StreamChatUseCase._persist_completed_rounds`` — one snapshot save
  per agentic round);
* the streaming turn's terminal saves (finalize / interrupt / error
  tails);
* :class:`~qai.chat.adapters.background_job_dispatcher.BackgroundJobDispatcher`
  appending a ``SYSTEM_NOTICE`` row when a background job settles.

Each one opened its own ``BEGIN IMMEDIATE`` transaction, so they
serialised on SQLite's write lock at the *driver* level — with no
ordering guarantee, one retry storm per contended write, and one
transaction per call even when three of them carried the very same
aggregate.

This queue moves that serialisation up one level, where we can reason
about it:

* **Same conversation → one worker.**  Writes for a conversation run in
  submit order inside a single coroutine, so two producers can never
  interleave mid-write.
* **Different conversations → different workers.**  Six busy tabs still
  write in parallel; the queue adds no cross-conversation coupling.
* **Two lanes, priority first.**  ``submit_append_atomic`` lands in the
  priority lane and is executed as soon as the worker is free — it never
  waits out the batch window.  Its p99 submit→durable latency is one
  single-row transaction (~10-30ms on SQLite), which is what makes it
  usable as a "the row is readable now" signal.
* **Batch window on the save lane.**  ``submit_save_messages`` waits up
  to :data:`ConversationWriteQueue.BATCH_WINDOW_MS` for more saves of the
  same conversation.  Saves that carry the SAME aggregate instance —
  the common case, since a turn re-submits its one live ``Conversation``
  every round — collapse to a SINGLE ``save_messages`` call: the
  repository writes a full snapshot, so replaying an older snapshot of
  the same object adds nothing.  A turn that used to cost three
  transactions costs one.
* **Sub-agent session writes share the conversation's worker.**
  ``chat_subagent_session`` rows are written with their own
  ``BEGIN IMMEDIATE`` on the SAME database file as ``chat_message``, and
  every sub-agent belongs to exactly one conversation through its
  ``root_conversation_id``.  Keying ``submit_save_subagent_session`` on
  that root id puts session writes in the SAME per-conversation worker as
  the conversation's message writes, so the two can no longer contend:
  the mutual exclusion is structural, not a retry loop.  They ride the
  PRIORITY lane rather than the batch lane — a session save is a
  single-row CAS upsert that cannot be coalesced (each carries its own
  ``version``, so replaying an older one would lose the chain), and the
  ``finalize`` save has to be readable the moment the run reports done or
  ``sub_agent inspect`` briefly finds a stale row.
* **One connection per worker, not per transaction.**  Because a worker
  IS the single writer for its conversation, it can hold one connection
  open across every transaction it retires.  That matters more than it
  sounds: of a measured 9.52ms write, 7.32ms was opening the connection
  and replaying its per-connection PRAGMAs and only 2.20ms was the
  ``BEGIN IMMEDIATE`` / ``INSERT`` / ``COMMIT`` doing the work.  The lease
  is taken per WORKER and never shared between conversations — sharing one
  would put two conversations back on the same connection and re-create
  exactly the cross-conversation write contention this queue exists to
  remove.  Repositories accept the connection through an optional ``conn``
  argument; when the lease cannot be taken (a repository built on
  something other than :class:`~qai.platform.persistence.Database`, which
  is every test double) the worker simply runs without one and each
  transaction leases its own, as before.

Every submit returns an ``asyncio.Future[None]`` that resolves when the
write is durable (or rejects with the persistence error).  Callers that
need durability-on-return await it; the incremental round save does not.
:meth:`AgentSessionCoordinator.settle_pending` awaits the futures handed
to it by the dispatcher, which is how a live turn knows the notice rows
it is about to fold into its wire are actually readable.

Crash semantics — accepted loss
===============================

A queued-but-unflushed save is lost if the process dies inside the batch
window: at most ``BATCH_WINDOW_MS`` of *incremental* round persistence.
This is a deliberate trade, not an oversight.  Incremental saves exist
purely as a crash safety net for a long agentic turn, and the next turn
rebuilds the conversation tail from the same in-memory state anyway, so
the window's worth of loss self-heals.  Terminal saves and every
``append_atomic`` are awaited by their callers, so they are durable
before the caller proceeds — nothing user-visible rides on the window.

Retry / atomicity
=================

A batch flush is retried up to :data:`MAX_BATCH_ATTEMPTS` times as a
whole: ``save_messages`` is an idempotent upsert-by-id, so replaying it
after a partial failure converges.  Every future in the batch shares the
batch's fate — all resolve or all reject.  ``append_atomic`` is NOT
retried: it is already a single transaction whose row carries a fixed
message id, so a retry after a commit that failed on the way back would
either duplicate a notice or die on the primary key.  One attempt, then
the caller sees the error.

A sub-agent session save is retried up to :data:`MAX_LOCKED_ATTEMPTS`
times, but ONLY when the failure is a lock timeout.  Replaying that case
is safe because the save's compare-and-swap runs inside an explicit
transaction that is rolled back on failure: a write which lost the file
lock left neither the row nor the aggregate's ``version`` touched, so the
replay simply re-reads the stored version.  Every other outcome is
relayed on the first attempt — ``SubAgentSessionConflictError`` most of
all, since a lost CAS means another writer legitimately moved the version
forward and the caller's job is to reload and replay on top of it, not to
retry blindly.

The retry exists because the queue removes SAME-conversation contention,
not all of it: different conversations write the one file in parallel on
purpose, so a heavy burst can still time a write out.  Before the queue
carried session writes at all, that timeout meant the row never existed
and the sub-agent failed silently; now the worst case is a few extra
milliseconds of wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qai.chat.domain.ids import ConversationId
from qai.platform.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from qai.chat.application.ports import (
        ConversationRepositoryPort,
        SubAgentSessionRepositoryPort,
    )
    from qai.chat.domain.conversation import Conversation
    from qai.chat.domain.message import Message
    from qai.chat.domain.sub_agent_session import SubAgentSession

__all__ = ["ConversationWriteQueue"]

_log = get_logger(__name__)


def _consume_rejection(fut: asyncio.Future[None]) -> None:
    """Retrieve a rejected future's exception so asyncio stays quiet.

    The incremental save path deliberately does not await its future
    (fire-and-forget durability), so a rejected one would otherwise trip
    asyncio's "exception was never retrieved" warning on GC.  Retrieving
    it here only clears that log flag — a real ``await`` on the same
    future still raises.
    """
    if not fut.cancelled():
        fut.exception()


@dataclass(slots=True)
class _SaveOp:
    """A queued ``save_messages`` of one aggregate snapshot."""

    conversation: Conversation
    future: asyncio.Future[None]
    submitted_at: float


@dataclass(slots=True)
class _AppendOp:
    """A queued single-row atomic append."""

    conversation_id: ConversationId
    message: Message
    future: asyncio.Future[None]
    submitted_at: float


@dataclass(slots=True)
class _SessionSaveOp:
    """A queued single-row ``chat_subagent_session`` CAS upsert."""

    session: SubAgentSession
    future: asyncio.Future[None]
    submitted_at: float


@dataclass(slots=True)
class _Lane:
    """Per-conversation state: two queues, two wake signals, one worker."""

    #: Window-free lane: notice appends and sub-agent session saves, in
    #: submit order.  Both are single-row transactions that must not be
    #: coalesced or delayed, and sharing ONE deque is what keeps them
    #: ordered relative to each other.
    priority: deque[_AppendOp | _SessionSaveOp] = field(
        default_factory=deque
    )
    batch: deque[_SaveOp] = field(default_factory=deque)
    #: Set by ANY submit — wakes a worker parked on the idle timeout.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set by a PRIORITY submit, by an ``immediate`` save, or by
    #: :meth:`close` — pre-empts an open batch window so neither an
    #: ``append_atomic`` nor an awaited save waits out the accumulation.
    priority_wake: asyncio.Event = field(default_factory=asyncio.Event)
    #: An ``immediate`` save is queued: skip the window on the next step.
    #: Cleared by the worker when it consumes the batch.
    flush_now: bool = False
    worker: asyncio.Task[None] | None = None
    #: The worker's persistent write connection, held across every
    #: transaction it retires and returned when it retires or on
    #: :meth:`ConversationWriteQueue.close`.  ``None`` means the repository
    #: is not backed by a leasable ``Database`` (every test double), so each
    #: transaction leases its own connection as it always did.
    conn: Any | None = None

    @property
    def empty(self) -> bool:
        return not self.priority and not self.batch


class ConversationWriteQueue:
    """Serialises conversation writes per conversation id.

    Not thread-safe by design: like every other adapter here it lives on
    the event-loop thread, and the ``asyncio`` primitives it holds make
    that mandatory.
    """

    #: Accumulation window for the save lane.
    BATCH_WINDOW_MS: int = 100
    #: Whole-batch retry ceiling (``save_messages`` is idempotent).
    MAX_BATCH_ATTEMPTS: int = 3
    #: Attempt ceiling for a sub-agent session save that lost the FILE lock
    #: to another conversation.  The queue removes same-conversation
    #: contention; cross-conversation parallelism is deliberate, so a burst
    #: can still time one write out.  Safe to replay because the save's
    #: transaction is rolled back on failure, leaving neither the row nor
    #: the aggregate's version touched.
    MAX_LOCKED_ATTEMPTS: int = 4
    #: Base backoff between those attempts, multiplied by the attempt
    #: number, so the holder of the lock gets to finish instead of being
    #: fought for it.
    LOCKED_RETRY_BACKOFF_S: float = 0.05
    #: A worker with nothing to do this long retires, so a long-lived
    #: process does not hold one task per conversation ever touched.
    IDLE_EXIT_SECONDS: float = 30.0
    #: Grace period :meth:`close` gives workers to drain before cancel.
    CLOSE_DRAIN_TIMEOUT_SECONDS: float = 5.0

    __slots__ = (
        "_close_timeout_s",
        "_closed",
        "_conversations",
        "_idle_s",
        "_lanes",
        "_now",
        "_sessions_share_db",
        "_sub_agent_sessions",
        "_window_s",
    )

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        sub_agent_sessions: SubAgentSessionRepositoryPort | None = None,
        batch_window_ms: int | None = None,
        idle_exit_seconds: float | None = None,
        close_drain_timeout_seconds: float | None = None,
        now: Callable[[], float] = time.perf_counter,
    ) -> None:
        """
        Args:
            conversations: The repository every message flush calls.
            sub_agent_sessions: The repository every
                :meth:`submit_save_subagent_session` flush calls.  ``None``
                makes that method raise — a caller that submits session
                writes MUST wire it.
            batch_window_ms: Save-lane accumulation window.  Defaults to
                :data:`BATCH_WINDOW_MS`; tests shrink it so they never
                sleep a perceptible amount.
            idle_exit_seconds: Retirement timeout for an idle worker.
            close_drain_timeout_seconds: How long :meth:`close` waits for
                workers to drain before cancelling them.
            now: Monotonic source for the submit→ack latency metric.
        """
        self._conversations = conversations
        self._sub_agent_sessions = sub_agent_sessions
        self._window_s = (
            self.BATCH_WINDOW_MS if batch_window_ms is None else batch_window_ms
        ) / 1000
        self._idle_s = (
            self.IDLE_EXIT_SECONDS
            if idle_exit_seconds is None
            else idle_exit_seconds
        )
        self._close_timeout_s = (
            self.CLOSE_DRAIN_TIMEOUT_SECONDS
            if close_drain_timeout_seconds is None
            else close_drain_timeout_seconds
        )
        self._now = now
        self._lanes: dict[str, _Lane] = {}
        self._closed = False
        # Whether a session save may run on the CONVERSATION worker's
        # connection: only when both repositories are backed by the same
        # engine, which is how production DI wires them.  A test that pairs a
        # real conversation repository with a stub session repository (or two
        # databases) falls back to a per-transaction lease for the session
        # write, which is always correct.
        conv_db = getattr(conversations, "database", None)
        self._sessions_share_db = (
            conv_db is not None
            and conv_db is getattr(sub_agent_sessions, "database", None)
        )

    # ------------------------------------------------------------------
    # Producer API
    # ------------------------------------------------------------------
    async def submit_save_messages(
        self, conv_id: str, conversation: Conversation, *,
        immediate: bool = False,
    ) -> asyncio.Future[None]:
        """Queue a full-snapshot ``save_messages`` for ``conv_id``.

        Takes the AGGREGATE, not a message list: the repository's
        ``save_messages`` writes messages plus the status / token /
        detected-model columns off the aggregate, and the one caller that
        needs batching (``_persist_completed_rounds``) re-submits its one
        live ``Conversation`` every round.  Keeping the aggregate is what
        lets those re-submissions collapse into a single write.

        The returned future resolves once the row set is durable.  Await
        it for durability-on-return; ignore it for best-effort
        incremental persistence.

        ``immediate=True`` closes the current accumulation window as soon
        as this op is queued: the batch (this op included) flushes on the
        worker's next step instead of waiting the window out.  A caller
        that AWAITS its future must set it — otherwise it pays the window
        as pure latency, which for a turn's terminal save is latency the
        user feels.  Deferred submitters leave it ``False`` so their
        writes keep coalescing.
        """
        if self._closed:
            return await self._write_through_closed(
                lambda: self._conversations.save_messages(conversation)
            )
        lane = self._ensure_lane(conv_id)
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        lane.batch.append(
            _SaveOp(
                conversation=conversation,
                future=fut,
                submitted_at=self._now(),
            )
        )
        lane.wake.set()
        if immediate:
            lane.flush_now = True
            lane.priority_wake.set()
        return fut

    async def submit_append_atomic(
        self, conv_id: str, message: Message, *,
        conversation_id: ConversationId | None = None,
    ) -> asyncio.Future[None]:
        """Queue a single-row atomic append for ``conv_id`` — priority lane.

        Returned future resolves when the row is durable.  This is the
        contract :meth:`AgentSessionCoordinator.settle_pending` waits on,
        so it MUST be a future and not ``None``: a live turn folding a
        freshly-written ``SYSTEM_NOTICE`` into its wire has to know the
        row is readable first.

        ``conversation_id`` defaults to a value object built from
        ``conv_id``; pass it explicitly to reuse the caller's instance.
        """
        cid = (
            conversation_id
            if conversation_id is not None
            else ConversationId.of(conv_id)
        )
        if self._closed:
            return await self._write_through_closed(
                lambda: self._conversations.append_message_atomic(
                    conversation_id=cid, message=message
                )
            )
        lane = self._ensure_lane(conv_id)
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        lane.priority.append(
            _AppendOp(
                conversation_id=cid,
                message=message,
                future=fut,
                submitted_at=self._now(),
            )
        )
        lane.wake.set()
        lane.priority_wake.set()
        return fut

    async def submit_save_subagent_session(
        self, conv_id: str, session: SubAgentSession
    ) -> asyncio.Future[None]:
        """Queue a ``chat_subagent_session`` save — priority lane.

        ``conv_id`` MUST be the session's ``root_conversation_id``: that is
        what puts this write in the same worker as the conversation's
        message writes, which is the whole point.  A sub-agent at any depth
        carries the root id, so the entire sub-agent forest of one
        conversation serialises with that conversation's messages.

        Priority lane, no batch window and no coalescing: the row is a
        compare-and-swap on ``version``, so two saves of the same session
        are DISTINCT transactions that must run in submit order, and the
        finalize save has to be readable as soon as the run reports done.

        The returned future resolves when the row is durable and relays
        ``SubAgentSessionConflictError`` unchanged — a lost CAS is a domain
        outcome the caller reloads and merges from.
        """
        sessions = self._sub_agent_sessions
        if sessions is None:
            raise RuntimeError(
                "ConversationWriteQueue was built without a sub-agent "
                "session repository; submit_save_subagent_session needs one"
            )
        if self._closed:
            return await self._write_through_closed(
                lambda: sessions.save(session)
            )
        lane = self._ensure_lane(conv_id)
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        lane.priority.append(
            _SessionSaveOp(
                session=session,
                future=fut,
                submitted_at=self._now(),
            )
        )
        lane.wake.set()
        lane.priority_wake.set()
        return fut

    async def close(self) -> None:
        """Drain every lane, then retire every worker.  Idempotent.

        Closing flips the workers into no-window mode so the remaining
        batches flush immediately rather than waiting out an accumulation
        window per lane.  A worker that has not finished within
        :data:`CLOSE_DRAIN_TIMEOUT_SECONDS` is cancelled and whatever it
        still held is rejected — shutdown never hangs on a stuck write.

        Every worker's persistent connection is returned here as well.  A
        worker that drained normally already gave its own back (the pop is
        idempotent), but a CANCELLED one may not have reached its ``finally``,
        and its connection has to be closed before the process exits rather
        than waiting for the engine's own backstop.
        """
        if self._closed:
            return
        self._closed = True
        workers: list[asyncio.Task[None]] = []
        for lane in list(self._lanes.values()):
            lane.wake.set()
            lane.priority_wake.set()
            if lane.worker is not None and not lane.worker.done():
                workers.append(lane.worker)
        if workers:
            _, pending = await asyncio.wait(
                workers, timeout=self._close_timeout_s
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if pending:
                _log.warning(
                    "persistence.write_queue.close_timeout",
                    stuck_workers=len(pending),
                )
        stranded = 0
        for lane in list(self._lanes.values()):
            for op in list(lane.priority) + list(lane.batch):
                stranded += 1
                self._reject(
                    op.future,
                    RuntimeError("conversation write queue closed"),
                )
            lane.priority.clear()
            lane.batch.clear()
            await self._release_connection(lane)
        self._lanes.clear()
        _log.info("persistence.write_queue.closed", stranded=stranded)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _ensure_lane(self, conv_id: str) -> _Lane:
        """Get (or create) the lane for ``conv_id`` with a live worker.

        The worker is started lazily HERE rather than in DI: conversation
        ids are discovered at runtime, so there is no construction-time
        set of workers to spawn — and ``build_chat_services`` is a
        synchronous factory, where ``create_task`` would raise
        ``RuntimeError: no running event loop``.  Both ``submit_*`` are
        coroutines, so a running loop is guaranteed at this point.  A
        worker that retired on the idle timeout is simply restarted.
        """
        lane = self._lanes.get(conv_id)
        if lane is None:
            lane = _Lane()
            self._lanes[conv_id] = lane
        if lane.worker is None or lane.worker.done():
            lane.worker = asyncio.create_task(
                self._worker(conv_id), name=f"conv-write-{conv_id}"
            )
        return lane

    async def _worker(self, conv_id: str) -> None:
        """Serialise one conversation's writes on ONE held connection.

        The lease is taken here rather than in :meth:`_ensure_lane` because
        acquiring it is a coroutine and lane creation is not, and it is
        released in the ``finally`` so every exit — idle retirement, a
        cancelled worker, or :meth:`close` — gives the connection back.
        :meth:`Database.close` reclaims it as a backstop if a cancellation
        lands before the ``finally`` runs.
        """
        lane = self._lanes[conv_id]
        lane.conn = await self._lease_connection(conv_id)
        try:
            await self._run(conv_id, lane)
        finally:
            await self._release_connection(lane)

    async def _run(self, conv_id: str, lane: _Lane) -> None:
        """The worker loop proper: priority lane first, then the batch."""
        while True:
            if lane.priority:
                op = lane.priority.popleft()
                if isinstance(op, _SessionSaveOp):
                    await self._flush_session_save(conv_id, op, lane.conn)
                else:
                    await self._flush_append(op, lane.conn)
                continue
            if lane.batch:
                if not self._closed and not lane.flush_now:
                    lane.priority_wake.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            lane.priority_wake.wait(), self._window_s
                        )
                    if lane.priority:
                        # A notice or session save arrived during the
                        # window — it owns the next transaction; the batch
                        # keeps accumulating.
                        continue
                ops = list(lane.batch)
                lane.batch.clear()
                lane.flush_now = False
                await self._flush_batch(conv_id, ops, lane.conn)
                continue
            if self._closed:
                self._lanes.pop(conv_id, None)
                return
            lane.wake.clear()
            try:
                await asyncio.wait_for(lane.wake.wait(), self._idle_s)
            except TimeoutError:
                # Nothing arrived for a whole idle window and nothing can
                # slip in between this check and the pop (no await in
                # between, single-threaded loop) — retire.
                if lane.empty:
                    self._lanes.pop(conv_id, None)
                    return

    async def _lease_connection(self, conv_id: str) -> Any | None:
        """Borrow this worker's write connection, or ``None`` if impossible.

        ``None`` is a normal outcome, not a failure: the repository may not
        be backed by a :class:`~qai.platform.persistence.Database` at all
        (every unit-test double), and a lease that cannot be taken must
        degrade to the historical per-transaction lease rather than fail the
        write.  Reached through the repository's own accessor so the queue
        keeps depending on its port and not on the platform engine.
        """
        db = getattr(self._conversations, "database", None)
        lease = getattr(db, "lease_persistent_connection", None)
        if lease is None:
            return None
        try:
            return await lease()
        except Exception as exc:  # noqa: BLE001 — degrade, never fail a write
            _log.warning(
                "persistence.write_queue.lease_failed",
                conv_id=conv_id,
                error=type(exc).__name__,
                error_msg=str(exc),
            )
            return None

    async def _release_connection(self, lane: _Lane) -> None:
        """Return this worker's lease.  Safe to call when there was none."""
        conn = lane.conn
        lane.conn = None
        if conn is None:
            return
        db = getattr(self._conversations, "database", None)
        give_back = getattr(db, "return_persistent_connection", None)
        if give_back is None:  # pragma: no cover — leased implies returnable
            return
        with contextlib.suppress(Exception):
            await give_back(conn)

    async def _flush_append(self, op: _AppendOp, conn: Any | None) -> None:
        """Execute one atomic append in its own transaction.

        Retried on a LOCK TIMEOUT only, on exactly the reasoning already
        proven for :meth:`_flush_session_save`: the append is one
        ``BEGIN IMMEDIATE`` transaction that is rolled back on any failure,
        so a write which lost the file lock left NOTHING behind — no row, and
        no position consumed, since the position is computed inside the same
        transaction.  Replaying re-reads the tail and lands correctly.

        Why it is needed at all when the queue already serialises: the queue
        removes SAME-conversation contention only.  Different conversations
        write the one file in parallel BY DESIGN, and now that each worker
        holds its connection instead of rebuilding one per transaction, they
        retire writes roughly an order of magnitude faster — which means they
        reach for the file lock that much more often.  Under a heavy burst a
        single append can still exceed ``busy_timeout``, and a lost
        SYSTEM_NOTICE row is user-visible (a background job reports done with
        no notice to fold into the turn).  A bounded retry converts that from
        a lost row into a few milliseconds of extra wait.

        Every other failure goes to the future on the first attempt —
        ``ConversationNotFoundError`` in particular must not be replayed.
        """
        last_exc: BaseException | None = None
        for attempt in range(1, self.MAX_LOCKED_ATTEMPTS + 1):
            try:
                await self._conversations.append_message_atomic(
                    conversation_id=op.conversation_id,
                    message=op.message,
                    **({} if conn is None else {"conn": conn}),
                )
            except asyncio.CancelledError:
                self._reject(op.future, RuntimeError("write cancelled"))
                raise
            except Exception as exc:  # noqa: BLE001 — relayed to the future
                last_exc = exc
                retryable = (
                    "database is locked" in str(exc)
                    and attempt < self.MAX_LOCKED_ATTEMPTS
                )
                _log.warning(
                    "persistence.write_queue.append_failed",
                    conv_id=op.conversation_id.value,
                    attempt=attempt,
                    will_retry=retryable,
                    error=type(exc).__name__,
                    error_msg=str(exc),
                )
                if not retryable:
                    self._reject(op.future, exc)
                    return
                # Back off before re-taking the lock so the writer that
                # holds it can finish rather than being fought for it.
                await asyncio.sleep(self.LOCKED_RETRY_BACKOFF_S * attempt)
                continue
            self._resolve(op.future)
            _log.debug(
                "persistence.write_latency_ms",
                conv_id=op.conversation_id.value,
                lane="priority",
                latency_ms=round((self._now() - op.submitted_at) * 1000, 3),
            )
            return
        self._reject(
            op.future,
            last_exc or RuntimeError("conversation append failed"),
        )

    async def _flush_session_save(
        self, conv_id: str, op: _SessionSaveOp, conn: Any | None
    ) -> None:
        """Execute one sub-agent session save.

        Retried, but ONLY on a lock timeout, and it is safe to retry for a
        precise reason: ``save`` wraps its compare-and-swap in an explicit
        transaction that is rolled back on any failure, so a write which
        lost the file lock left NOTHING behind — the row, and the
        aggregate's in-memory ``version``, are exactly as they were, and
        replaying re-reads the stored version.  Nothing is lost and no
        concurrent writer can be clobbered.

        Every other outcome goes straight to the future on the first
        attempt.  ``SubAgentSessionConflictError`` in particular is a
        DOMAIN result — another writer legitimately moved the version
        forward — and the take-over path reloads and replays from it;
        retrying here would race that logic instead of helping it.

        Why a retry is needed at all when the queue already serialises:
        the queue removes SAME-conversation contention, which is what made
        the row vanish outright.  Six conversations still write the one
        file in parallel by design (that parallelism is the point), so a
        write can still lose the lock to another conversation under a heavy
        burst.  A bounded retry converts that from a lost row into a few
        milliseconds of extra wait.

        A lock-timeout retry is also why the transaction's cleanliness on
        failure matters twice over now that the connection SURVIVES the
        failed attempt: ``write_transaction`` rolls back on the way out and
        defensively on the way in, so attempt ``n+1`` starts from the same
        state attempt ``n`` did.
        """
        sessions = self._sub_agent_sessions
        assert sessions is not None  # submit rejects a missing repository
        # Only lend the connection when the session rows live in the SAME
        # database as the conversation rows.  Two Database instances would
        # mean writing the session into the conversation's FILE.
        session_conn = conn if self._sessions_share_db else None
        last_exc: BaseException | None = None
        for attempt in range(1, self.MAX_LOCKED_ATTEMPTS + 1):
            try:
                await sessions.save(
                    op.session,
                    **({} if session_conn is None else {"conn": session_conn}),
                )
            except asyncio.CancelledError:
                self._reject(op.future, RuntimeError("write cancelled"))
                raise
            except Exception as exc:  # noqa: BLE001 — relayed to the future
                last_exc = exc
                retryable = (
                    "database is locked" in str(exc)
                    and attempt < self.MAX_LOCKED_ATTEMPTS
                )
                _log.warning(
                    "persistence.write_queue.session_save_failed",
                    conv_id=conv_id,
                    subagent_id=op.session.id.value,
                    attempt=attempt,
                    will_retry=retryable,
                    error=type(exc).__name__,
                    error_msg=str(exc),
                )
                if not retryable:
                    self._reject(op.future, exc)
                    return
                # Back off before re-taking the lock so the writer that
                # holds it can finish rather than being fought for it.
                await asyncio.sleep(
                    self.LOCKED_RETRY_BACKOFF_S * attempt
                )
                continue
            self._resolve(op.future)
            _log.debug(
                "persistence.write_latency_ms",
                conv_id=conv_id,
                lane="priority",
                latency_ms=round((self._now() - op.submitted_at) * 1000, 3),
            )
            return
        self._reject(
            op.future,
            last_exc or RuntimeError("sub-agent session save failed"),
        )

    async def _flush_batch(
        self, conv_id: str, ops: list[_SaveOp], conn: Any | None
    ) -> None:
        """Flush the accumulated saves, coalescing repeated aggregates.

        Ops carrying the same ``Conversation`` INSTANCE collapse to one
        ``save_messages`` call — a snapshot write makes replaying an older
        snapshot of the same object a no-op.  Distinct instances each get
        their own call, in submit order, and the whole set shares one
        retry budget so every future in the batch resolves or rejects
        together.
        """
        targets: dict[int, Conversation] = {}
        for op in ops:
            targets[id(op.conversation)] = op.conversation
        writes = list(targets.values())
        last_exc: BaseException | None = None
        for attempt in range(1, self.MAX_BATCH_ATTEMPTS + 1):
            try:
                for conversation in writes:
                    await self._conversations.save_messages(
                        conversation,
                        **({} if conn is None else {"conn": conn}),
                    )
            except asyncio.CancelledError:
                for op in ops:
                    self._reject(op.future, RuntimeError("write cancelled"))
                raise
            except Exception as exc:  # noqa: BLE001 — relayed to the futures
                last_exc = exc
                _log.warning(
                    "persistence.write_queue.batch_failed",
                    conv_id=conv_id,
                    attempt=attempt,
                    max_attempts=self.MAX_BATCH_ATTEMPTS,
                    coalesced=len(ops),
                    error=type(exc).__name__,
                    error_msg=str(exc),
                )
                continue
            for op in ops:
                self._resolve(op.future)
            now = self._now()
            _log.info(
                "persistence.write_batch_coalesced",
                conv_id=conv_id,
                submitted=len(ops),
                transactions=len(writes),
                attempts=attempt,
            )
            _log.debug(
                "persistence.write_latency_ms",
                conv_id=conv_id,
                lane="batch",
                latency_ms=round(
                    max(now - op.submitted_at for op in ops) * 1000, 3
                ),
            )
            return
        exc = last_exc or RuntimeError("conversation batch write failed")
        for op in ops:
            self._reject(op.future, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _write_through_closed(
        self, call: Callable[[], Awaitable[None]]
    ) -> asyncio.Future[None]:
        """Perform a post-:meth:`close` write directly on the repository.

        Reached only during shutdown, when a turn still in teardown
        persists its tail.  There is no worker left to serialise through
        and at most one writer remains, so the row landing matters more
        than the (now moot) serialisation.  The returned future is already
        settled, so an awaiting caller sees the real outcome.
        """
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        try:
            await call()
        except Exception as exc:  # noqa: BLE001 — relayed to the future
            _log.warning(
                "persistence.write_queue.post_close_write_failed",
                error=type(exc).__name__,
                error_msg=str(exc),
            )
            self._reject(fut, exc)
            return fut
        self._resolve(fut)
        return fut

    @staticmethod
    def _resolve(fut: asyncio.Future[None]) -> None:
        if not fut.done():
            fut.set_result(None)

    @staticmethod
    def _reject(fut: asyncio.Future[None], exc: BaseException) -> None:
        if fut.done():
            return
        fut.set_exception(exc)
        fut.add_done_callback(_consume_rejection)

