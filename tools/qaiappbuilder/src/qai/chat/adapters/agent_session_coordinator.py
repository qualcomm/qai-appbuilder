# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-tab coordination between the agent loop and background jobs.

This module owns the ONE authoritative answer to the question:

    "Does tab X have work that still needs the agent's attention?"

Two independent producers publish "pending work" for a tab:

1. ``BackgroundJobDispatcher`` — a sub-agent or background exec job that
   was auto-parked has just reached a terminal state; its completion
   notice has been appended to the conversation as a
   ``role=SYSTEM_NOTICE`` message.  The main agent has NOT yet observed
   it because its previous turn already finished streaming.
2. Any future producer that wants to hand the agent an out-of-band
   trigger without going through the user-message input path (e.g. a
   scheduled task firing, a permission grant landing).

The consumer is the agent loop itself: when a round ends with no more
tool calls and no assistant work in flight, it consults
:meth:`AgentSessionCoordinator.has_pending_work` and, if true, replays
one more round instead of unwinding.  When the tab is idle (no live
stream) the dispatcher directly kicks
:meth:`AgentSessionCoordinator.run_headless_followup` which spins up a
one-shot :class:`StreamChatUseCase` run whose sole input is the pending
work already sitting in the conversation.

Design principles
=================

* **Single source of truth** — pending state lives in this class,
  keyed by :class:`TabId`.  Every producer records "there is pending
  work for tab X" through :meth:`notify`; every consumer clears it
  through :meth:`ack`.  No parallel bookkeeping (dedup keys, aside
  consumed sets, tab-busy polling) lives elsewhere.
* **Idempotent notify** — a producer that re-fires an already-notified
  key is a no-op.  This eliminates the "double-drain" pitfall the old
  per-tab aside queue fought with a ``_consumed`` gate.
* **No secondary lookup** — the ack side does not need to know WHICH
  notification is being acked; the agent loop always drains "everything
  pending for this tab, right now".  If two notifications land during
  the same idle window they are folded into one follow-up turn (the
  agent sees BOTH SYSTEM_NOTICE messages in its wire history).
* **Callable-based headless kick** — the coordinator does NOT depend on
  :class:`StreamChatUseCase` directly (avoiding a domain layer ↔
  application layer import cycle).  DI wires an ``async
  (tab_id, conv_id, model_hint) -> None`` callable at construction time.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from qai.platform.logging import get_logger

if TYPE_CHECKING:
    from qai.chat.domain.ids import ConversationId, TabId

__all__ = [
    "AgentSessionCoordinator",
    "HeadlessFollowupRunner",
    "PendingWorkSnapshot",
]

_log = get_logger(__name__)

#: Hard ceiling on headless follow-up turns a SINGLE :meth:`_run_one`
#: invocation may run.  Guards the pathological case where a runner both
#: consumes AND re-adds pending keys on every iteration, so the drain
#: would never converge inside one call.  Promoted from a ``_run_one``
#: local to module scope so the value is visible to tests and to the
#: sliding-window ceiling below (they are DIFFERENT guards — see
#: :data:`_DRAIN_WINDOW_MAX`).
_MAX_ITER: int = 8

#: Sliding-window drain throttle.  ``_MAX_ITER`` bounds ONE call; this
#: bounds the RATE across calls.  Without it, a producer that fires a
#: fresh notification every few seconds re-enters :meth:`_run_one`
#: indefinitely — each invocation individually under ``_MAX_ITER``, yet
#: the tab burns an unbounded number of LLM turns.  Once
#: ``_DRAIN_WINDOW_MAX`` drains have started inside the trailing
#: ``_DRAIN_WINDOW_SECONDS``, further drains are SKIPPED (not queued and
#: not acked) until the window drains out, so pending keys survive for a
#: later trigger.
_DRAIN_WINDOW_SECONDS: float = 60.0
_DRAIN_WINDOW_MAX: int = 20

#: A THIRD ceiling guards this same notice-integration path from the
#: ``application`` layer: ``streaming._MAX_PENDING_INTEGRATION`` bounds
#: turn-internal integration retries — how many times ONE live
#: user-visible turn may reopen a round because a new SYSTEM_NOTICE
#: landed after the assistant finished.  All three are deliberately
#: separate and MUST NOT be merged; they catch different runaway modes:
#: ``_MAX_ITER`` = unbounded iterations inside one drain call,
#: ``_DRAIN_WINDOW_*`` = unbounded FRESH drain calls per tab over time,
#: ``_MAX_PENDING_INTEGRATION`` = unbounded re-opens inside one live
#: turn.  It is declared beside its own consumer because ``application``
#: must not import ``adapters`` (``.importlinter`` ``layered-chat``).


#: Callable signature the coordinator invokes when a producer fires a
#: notification for a tab that is currently idle.  Runs one headless
#: agent turn that consumes the pending SYSTEM_NOTICE row(s) already
#: sitting in the conversation.
#:
#: Contract (§UX-headless-integrated-summary refactor 2026-08-07):
#: runner returns the SET of dedup_keys it OBSERVED on its own wire
#: history (typically every SYSTEM_NOTICE currently persisted on the
#: conversation, since the wire is rebuilt at turn open).  The
#: coordinator uses that set to decide which pending keys to ack —
#: NOT a pre-run snapshot.  This closes the race where a
#: SYSTEM_NOTICE arrives mid-drain and would either be double-summarised
#: (fire another headless whose wire already contained the same row)
#: or stranded (never observed by any turn).  Empty set on failure /
#: legacy runners returning ``None`` triggers the pre-run snapshot
#: fallback — same behaviour the refactor started from.
#:
#: Implementations MUST be idempotent per tab — the coordinator
#: serialises calls per tab (see :meth:`_run_one`).
HeadlessFollowupRunner = Callable[
    ["TabId", "ConversationId", str | None],
    Awaitable["frozenset[str] | None"],
]


@dataclass(frozen=True, slots=True)
class PendingWorkSnapshot:
    """Immutable view of a tab's pending-work state at a point in time.

    Two callers observe this:

    * The agent loop, at end-of-round, to decide whether to loop again.
    * Tests and diagnostic dumps, to assert coordinator state.
    """

    tab_id: TabId
    pending_keys: tuple[str, ...]
    """Producer-supplied dedup keys still waiting to be acked."""

    is_headless_running: bool
    """``True`` iff a headless follow-up turn is currently executing for
    this tab.  Used to prevent the dispatcher from kicking a second
    concurrent follow-up while one is already in flight (which would
    race for the same SYSTEM_NOTICE row)."""

    @property
    def has_pending(self) -> bool:
        return bool(self.pending_keys)


@dataclass(slots=True)
class _TabState:
    """Mutable per-tab state, held inside the coordinator's map.

    ``pending_keys`` maps ``dedup_key`` → ``(conversation_id,
    model_hint)``.  The values are captured at notify time so a later
    :meth:`on_tab_idle` transition can fire a headless follow-up with
    the right args EVEN IF the original coroutine that called
    ``notify`` has long finished.  Insertion order is preserved (an
    ``OrderedDict``) so a follow-up drains the OLDEST pending first —
    matching the order in which the sub-agents / bgp jobs actually
    settled.
    """

    pending_keys: OrderedDict[str, tuple[ConversationId, str | None]] = (
        field(default_factory=OrderedDict)
    )
    headless_task: asyncio.Task[None] | None = None
    pending_futures: dict[str, asyncio.Future[None]] = field(
        default_factory=dict
    )
    """``dedup_key`` → the future that resolves once that notice's row is
    durably persisted.  Populated by producers whose persistence is
    deferred (a write queue); empty when the producer awaits its write
    inline, in which case :meth:`settle_pending` is trivially satisfied.
    Cleared alongside the matching ``pending_keys`` entry on ack so a
    long-lived tab never accumulates settled futures."""

    drain_timestamps: deque[float] = field(default_factory=deque)
    """Monotonic start times of recent headless drain iterations, oldest
    first.  Trimmed to the trailing :data:`_DRAIN_WINDOW_SECONDS` on
    every drain attempt (see :meth:`_run_one`)."""


class AgentSessionCoordinator:
    """The single owner of "does tab X still need the agent?" state.

    Instances are process-wide (one per :class:`Container`); the
    per-tab state map is keyed by :class:`TabId`.

    Thread-safety: not thread-safe.  All access MUST happen on the
    event loop thread (matches every other adapter in this codebase;
    the ``asyncio.Task`` field alone forces this).
    """

    __slots__ = (
        "_headless_runner",
        "_is_tab_streaming",
        "_monotonic",
        "_states",
    )

    def __init__(
        self,
        *,
        headless_runner: HeadlessFollowupRunner,
        is_tab_streaming: Callable[[TabId], bool],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            headless_runner: The one-shot follow-up-turn entry point,
                wired from :class:`StreamChatUseCase.run_headless_followup`
                by DI.  Called when a notification lands on an idle tab.
            is_tab_streaming: Snapshot predicate ``(tab_id) -> bool`` —
                returns ``True`` while the tab has a live stream handle
                registered.  Used to decide whether the dispatcher's
                notification should kick a headless follow-up or just
                sit in ``pending_keys`` until the active turn's next
                round-end consults us.  In practice this wraps
                :meth:`StreamAbortRegistryPort.is_streaming`.
            monotonic: Monotonic clock backing the drain sliding window.
                Injectable so a test can advance time without sleeping
                through a real 60-second window.
        """
        self._headless_runner = headless_runner
        self._is_tab_streaming = is_tab_streaming
        self._monotonic = monotonic
        self._states: dict[TabId, _TabState] = {}

    # ------------------------------------------------------------------
    # Producer side — a background job or any other out-of-band
    # trigger records that tab X now has work to look at.
    # ------------------------------------------------------------------
    async def notify(
        self,
        *,
        tab_id: TabId,
        conversation_id: ConversationId,
        dedup_key: str,
        model_hint: str | None = None,
    ) -> None:
        """Register a pending-work notification for ``tab_id``.

        Behaviour:

        * If ``dedup_key`` was already recorded for this tab, no-op
          (idempotent — a re-published event never fires twice).
        * If the tab currently has a live stream, we ONLY record the
          key; the streaming turn's own round-end check will drain it.
        * If the tab is idle AND no headless follow-up is already
          running, we kick :attr:`_headless_runner` for one turn.

        Args:
            tab_id: The tab that owns the notification.
            conversation_id: The conversation whose SYSTEM_NOTICE row
                the follow-up turn will pick up.  Passed straight to
                the headless runner — the coordinator itself never
                touches the conversation.
            dedup_key: Producer-supplied opaque identifier.  Same key
                across re-publishes MUST collapse to one notify (the
                dispatcher folds SQLite-persisted "already-appended"
                detection into this key so we don't append twice).
            model_hint: Passed through to :attr:`_headless_runner`
                unchanged.  ``None`` means "let the runner resolve".
        """
        state = self._states.setdefault(tab_id, _TabState())
        if dedup_key in state.pending_keys:
            _log.debug(
                "coord.notify.dedup_skip",
                tab_id=str(tab_id),
                dedup_key=dedup_key,
            )
            return
        state.pending_keys[dedup_key] = (conversation_id, model_hint)

        # Live stream on this tab? Its own round-end check will drain us.
        if self._is_tab_streaming(tab_id):
            _log.info(
                "coord.notify.queued_for_live_turn",
                tab_id=str(tab_id),
                dedup_key=dedup_key,
                pending=len(state.pending_keys),
            )
            return

        # Idle tab.  Kick a headless follow-up unless one is already in
        # flight (its round-end check will pick up the newly-added key).
        if state.headless_task is not None and not state.headless_task.done():
            _log.info(
                "coord.notify.headless_already_running",
                tab_id=str(tab_id),
                dedup_key=dedup_key,
            )
            return
        _log.info(
            "coord.notify.starting_headless_followup",
            tab_id=str(tab_id),
            dedup_key=dedup_key,
        )
        state.headless_task = asyncio.create_task(
            self._run_one(tab_id, conversation_id, model_hint),
            name=f"agent-coord-headless-{tab_id}",
        )

    # ------------------------------------------------------------------
    # Consumer side — the agent loop asks "is there work" and, after a
    # turn actually consumed the pending SYSTEM_NOTICE row(s), acks by
    # dedup_key.  The two calls are separate so the loop can bail out
    # BEFORE running a round if it also sees other reasons to stop.
    # ------------------------------------------------------------------
    def has_pending_work(self, tab_id: TabId) -> bool:
        """Return ``True`` iff ``tab_id`` has un-acked pending keys."""
        st = self._states.get(tab_id)
        return bool(st and st.pending_keys)

    def on_tab_idle(self, tab_id: TabId) -> None:
        """Signal that ``tab_id`` just transitioned from streaming to
        idle (a live turn's ``_release_streaming_tab`` fired).

        If pending SYSTEM_NOTICE work was queued during the live turn
        AND no headless follow-up is already in flight, this kicks the
        headless runner using the CAPTURED ``(conversation_id,
        model_hint)`` from the FIRST pending notify — so a completion
        that arrived while the tab was busy is not silently stranded.
        Best-effort: any exception is caught (never breaks the
        caller's release path).  Called synchronously from
        :meth:`StreamChatUseCase._release_streaming_tab` — the actual
        follow-up turn runs on an :meth:`asyncio.create_task` so we
        never block release.
        """
        st = self._states.get(tab_id)
        if st is None or not st.pending_keys:
            return
        if st.headless_task is not None and not st.headless_task.done():
            _log.debug(
                "coord.on_tab_idle.headless_already_running",
                tab_id=str(tab_id),
            )
            return
        # Fire on the OLDEST pending — its captured args are the ones
        # to use.  Later pendings (if any) will be observed by that
        # follow-up's own wire history rebuild (SYSTEM_NOTICE folded
        # into the wire).
        first_key = next(iter(st.pending_keys))
        conv_id, model_hint = st.pending_keys[first_key]
        _log.info(
            "coord.on_tab_idle.starting_headless_followup",
            tab_id=str(tab_id),
            dedup_key=first_key,
            pending=len(st.pending_keys),
        )
        st.headless_task = asyncio.create_task(
            self._run_one(tab_id, conv_id, model_hint),
            name=f"agent-coord-headless-{tab_id}",
        )

    def ack(self, *, tab_id: TabId, dedup_key: str) -> None:
        """Remove ``dedup_key`` from the pending set.

        Idempotent — acking an unknown key is a no-op (keeps callers
        from having to guard).  The full ``dedup_key`` for a SYSTEM_NOTICE
        message rides in ``msg.meta[SYSTEM_NOTICE_META_DEDUP_KEY]``, so
        the agent loop's follow-up round can walk its wire history and
        ack every SYSTEM_NOTICE it observed.
        """
        st = self._states.get(tab_id)
        if st is None:
            return
        st.pending_keys.pop(dedup_key, None)
        # Drop the persistence future alongside its key.  Keeping it would
        # leak one entry per notice for the lifetime of the tab AND would
        # make a later ``settle_pending`` await work that is long done.
        st.pending_futures.pop(dedup_key, None)
        _log.debug(
            "coord.ack",
            tab_id=str(tab_id),
            dedup_key=dedup_key,
            remaining=len(st.pending_keys),
        )

    def ack_all(self, *, tab_id: TabId) -> None:
        """Drain every pending key for ``tab_id`` in one shot.

        Convenience for the "follow-up round already consumed every
        SYSTEM_NOTICE for this tab" fast path.
        """
        st = self._states.get(tab_id)
        if st is None:
            return
        if st.pending_keys:
            _log.debug(
                "coord.ack_all",
                tab_id=str(tab_id),
                dropped=len(st.pending_keys),
            )
        st.pending_keys.clear()
        st.pending_futures.clear()

    # ------------------------------------------------------------------
    # Persistence settling — the live turn's pending-integration seam
    # (N.52) asks "are the rows I am about to fold into the wire actually
    # on disk yet?" before it reopens a round.
    # ------------------------------------------------------------------
    def register_pending_future(
        self,
        *,
        tab_id: TabId,
        dedup_key: str,
        future: asyncio.Future[None],
    ) -> None:
        """Attach a persistence future to an already-notified key.

        For a producer that awaits its own write before calling
        :meth:`notify` (the current dispatcher — see
        ``BackgroundJobDispatcher._dispatch``) there is nothing to
        register: the row is on disk by the time the key exists.  A
        producer that defers the write to a queue registers the future it
        got back, so :meth:`settle_pending` can block the live turn's
        pending integration until the row is readable.

        Registering for a key that is not (or no longer) pending is a
        no-op — the notice was already integrated, so nothing waits on it.
        """
        st = self._states.get(tab_id)
        if st is None or dedup_key not in st.pending_keys:
            return
        st.pending_futures[dedup_key] = future

    async def settle_pending(
        self, tab_id: TabId, timeout_ms: int
    ) -> tuple[bool, int]:
        """Await every registered persistence future for ``tab_id``.

        Returns ``(settled, flushed_count)`` where ``settled`` is
        ``False`` iff the wait timed out.  ``flushed_count`` is how many
        futures had completed by the time we returned — on the timeout
        path that is the honest partial count for the caller's log.

        With no registered futures this returns ``(True, 0)``
        immediately: the producer persisted inline, so the rows the
        caller is about to fold into its wire are already readable.

        Uses :func:`asyncio.wait` rather than
        ``wait_for(gather(...))``: the futures belong to the PRODUCER's
        write, not to us.  ``wait_for`` cancels its awaitable on timeout,
        which would propagate into those futures and abort writes that
        are merely slow — turning "the caller waits for the next trigger"
        into "the notice row is never written at all".  ``asyncio.wait``
        observes without cancelling, so a timeout costs nothing but time.
        """
        st = self._states.get(tab_id)
        if st is None or not st.pending_futures:
            return (True, 0)
        futures = list(st.pending_futures.values())
        _, not_done = await asyncio.wait(
            futures, timeout=timeout_ms / 1000
        )
        flushed = len(futures) - len(not_done)
        if not_done:
            _log.warning(
                "coord.pending_settle.timeout",
                tab_id=str(tab_id),
                timeout_ms=timeout_ms,
                awaited=len(futures),
                flushed=flushed,
            )
            return (False, flushed)
        return (True, flushed)

    # ------------------------------------------------------------------
    # Introspection — used by tests + diagnostic dumps.
    # ------------------------------------------------------------------
    def snapshot(self, tab_id: TabId) -> PendingWorkSnapshot:
        """Return the current pending state for ``tab_id``."""
        st = self._states.get(tab_id)
        if st is None:
            return PendingWorkSnapshot(
                tab_id=tab_id,
                pending_keys=(),
                is_headless_running=False,
            )
        return PendingWorkSnapshot(
            tab_id=tab_id,
            pending_keys=tuple(sorted(st.pending_keys)),
            is_headless_running=(
                st.headless_task is not None and not st.headless_task.done()
            ),
        )

    async def _run_one(
        self,
        tab_id: TabId,
        conversation_id: ConversationId,
        model_hint: str | None,
    ) -> None:
        """Drain the pending SYSTEM_NOTICE queue for ``tab_id``.

        Runs headless follow-up turns until pending is empty (or a
        turn fails / the ceiling trips).  Every iteration takes the
        SET OF DEDUP_KEYS THE TURN OBSERVED on its own rebuilt wire
        as the ack surface — NOT the pre-run snapshot — so a
        SYSTEM_NOTICE that arrived mid-turn AND is therefore already
        visible on the next turn's LLM wire is acked by the turn
        that saw it, avoiding a redundant "just repeat what I already
        said" follow-up (the reported "第二次 summary 说'我上面已经
        说过了'" symptom).

        Invariants:

        * **Runner-reported observation** — the runner returns the
          frozenset of dedup_keys present in its wire.  A legacy
          runner (or an exception path) returning ``None`` falls back
          to the pre-run pending snapshot as the ack surface.
        * **Task-lifetime drain** — the whole drain runs inside ONE
          :class:`asyncio.Task` (the one stored on
          ``_TabState.headless_task``).  A re-entrant
          :meth:`on_tab_idle` while THIS task is running is a legitimate
          "already running, do nothing"; any key it saw will be
          observed by the NEXT iteration's runner (which rebuilds its
          wire from the live conversation) OR acked here in the CURRENT
          iteration's post-run ack if it landed before the LLM stream
          finished.

        The follow-up args (``conversation_id`` / ``model_hint``) are
        the ones captured at the FIRST notify that kicked this drain.

        Any exception per iteration is logged and swallowed — the loop
        breaks out on failure (leaves pending intact for retry).

        TWO ceilings guard this loop, and they are NOT redundant:

        * :data:`_MAX_ITER` bounds iterations WITHIN one call — the
          pathological runner that consumes and re-adds keys forever
          would otherwise never let this coroutine return.
        * :data:`_DRAIN_WINDOW_MAX` over
          :data:`_DRAIN_WINDOW_SECONDS` bounds the drain RATE ACROSS
          calls.  Each producer notification on an idle tab starts a
          FRESH ``_run_one`` task, so a steady trickle of notices
          (a sub-agent fleet settling one member at a time) satisfies
          ``_MAX_ITER`` on every single call while still burning an
          unbounded number of LLM turns.  The window makes the whole
          drain path back off instead: iterations stop, pending keys stay
          un-acked, and the next trigger after the window empties
          resumes.
        """
        state = self._states.setdefault(tab_id, _TabState())
        iteration = 0
        while state.pending_keys and iteration < _MAX_ITER:
            now = self._monotonic()
            cutoff = now - _DRAIN_WINDOW_SECONDS
            while state.drain_timestamps and state.drain_timestamps[0] <= cutoff:
                state.drain_timestamps.popleft()
            if len(state.drain_timestamps) >= _DRAIN_WINDOW_MAX:
                _log.warning(
                    "coord.headless.drain_window_exceeded",
                    tab_id=str(tab_id),
                    conv_id=str(conversation_id),
                    window_seconds=_DRAIN_WINDOW_SECONDS,
                    window_max=_DRAIN_WINDOW_MAX,
                    remaining=len(state.pending_keys),
                )
                return
            state.drain_timestamps.append(now)
            iteration += 1
            fallback_keys = list(state.pending_keys.keys())
            observed: frozenset[str] | None = None
            try:
                observed = await self._headless_runner(
                    tab_id, conversation_id, model_hint,
                )
            except asyncio.CancelledError:
                _log.info(
                    "coord.headless.cancelled",
                    tab_id=str(tab_id),
                    conv_id=str(conversation_id),
                    iteration=iteration,
                )
                raise
            except Exception as exc:  # noqa: BLE001 — top-of-task boundary
                _log.warning(
                    "coord.headless.failed",
                    tab_id=str(tab_id),
                    conv_id=str(conversation_id),
                    iteration=iteration,
                    error=type(exc).__name__,
                    error_msg=str(exc),
                )
                # Pending keys stay so a later trigger can retry; do
                # NOT ack on failure.  Break out of the drain loop so
                # we don't spin on a failing runner.
                return
            # Ack surface = pre-run pending ∪ runner-observed.  Rationale:
            #
            # * Pre-run pending is guaranteed visible to the runner's wire
            #   because the dispatcher persists the SYSTEM_NOTICE row
            #   BEFORE calling ``notify()`` (see
            #   :meth:`BackgroundJobDispatcher._dispatch`).  Any key sitting
            #   in ``pending_keys`` when the iteration started has a row on
            #   disk that the wire rebuild folded in, so the LLM saw it —
            #   ack is safe.
            # * Runner-observed picks up mid-cycle arrivals (a SYSTEM_NOTICE
            #   persisted DURING the turn's rounds is on the conversation
            #   at ``_finalize_turn`` time; the LLM's follow-up-round wire
            #   rebuild — or the next iteration's rebuild — folds it in).
            #
            # Taking the UNION is aggressive but safe: worst case we ack a
            # key the LLM did not literally read this cycle, but that key
            # WAS folded in the wire (by construction) so this is
            # semantically equivalent to "the LLM saw it".  Not taking the
            # union risks the reported drain-loop-ceiling bug where an
            # observed-empty runner (e.g. persistence-lookup exception,
            # DB race) never converges: pending stays >0, drain re-runs
            # 8 times, hits ``_MAX_ITER``.
            ack_set: set[str] = set(fallback_keys)
            if observed is not None:
                ack_set |= set(observed)
            for k in ack_set:
                self.ack(tab_id=tab_id, dedup_key=k)
            _log.info(
                "coord.headless.iteration_completed",
                tab_id=str(tab_id),
                iteration=iteration,
                observed_count=len(ack_set),
                observed_keys=sorted(ack_set),
                fallback_used=observed is None,
                remaining_pending=len(state.pending_keys),
                remaining_keys=sorted(state.pending_keys.keys()),
            )
        if iteration >= _MAX_ITER and state.pending_keys:
            _log.warning(
                "coord.headless.drain_ceiling_reached",
                tab_id=str(tab_id),
                remaining=len(state.pending_keys),
                max_iter=_MAX_ITER,
            )
