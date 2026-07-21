# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-process sub-agent event broadcast + SSE replay state machine.

Runtime-defect fix (block 2): a sub-agent has two *disconnected* event
channels today —

* **Channel A (in parent tab):** the events
  :meth:`AgentToolHandler.iter_events` yields (``subagent_start`` /
  ``subagent_output`` / ``subagent_tool`` / ``subagent_done`` /
  ``subagent_error``) are forwarded *inline* inside the **parent
  conversation's** SSE stream, so the parent tab's ``subAgentBlocks``
  update live.
* **Channel B (standalone sub-agent tab):** ``openSubAgentTab`` only
  ``GET``s ``/api/chat/subagents/{id}`` *once* for a snapshot — no
  subscription, no stream. So while the sub-agent runs, a standalone tab
  does NOT update (close + reopen was the only way to see progress).

This broadcaster is the **third, independent fan-out channel** that fixes
channel B: every event the sub-agent emits is *also* published here with a
monotonic sequence, and a standalone tab opens
``GET /api/chat/subagents/{id}/stream`` (SSE) to backfill the history +
follow live frames. It is a 1:1 port of the production-proven App Builder
:class:`qai.app_builder.application.run_stream_broadcaster.RunStreamBroadcaster`
范式 (frame buffer + per-subscriber wake-up Event + per-connection cursor
backfill + TTL eviction + background-task ownership + terminal-snapshot
fallback), kept entirely inside the ``qai.chat`` context (§3.2 — no reuse
of another context's ``RunFrame`` / ``RunStreamEntry``).

Design notes mirrored from the blueprint:

* **Per-subscriber wake-up** (R-6 / D7): each ``stream`` subscriber
  registers its **own** ``asyncio.Event`` in
  :attr:`SubAgentStreamEntry.subscribers`; ``publish`` / ``mark_terminal``
  set *every* registered event, and each subscriber clears only its own —
  so two concurrent subscribers (the main-agent run + a user watching)
  cannot swallow each other's wake-up. (Requirement ③.)
* **Per-connection cursor backfill**: a subscriber that connects late (or
  reconnects after closing its tab while the sub-agent still runs) replays
  the full ``frames`` buffer from ``cursor=0`` then follows live frames to
  the terminal mark. (Requirements ① + ②.)
* **TTL eviction**: terminal entries are GC'd lazily ``_TTL_S`` after the
  terminal mark, on the next :meth:`register` call.
* **Background-task ownership** (R-3): held in a
  :class:`~qai.platform.tasks.TaskRegistry` so the lifespan can
  :meth:`aclose` them at shutdown.
* **Terminal double-track fallback**: when no in-memory entry exists
  (server restarted / TTL expired) :meth:`replay` reads the persisted
  :class:`SubAgentSession` snapshot from the repository and replays its
  wire history + terminal state so a late client still sees the result.
  Cross-process per-frame resume is intentionally NOT implemented (the
  light in-memory model is the chosen scope — restart drops the buffer and
  falls back to the snapshot).

Concurrency: the registry is read/written exclusively from inside the
asyncio event loop (single-threaded per loop), so no lock is required —
identical to the App Builder broadcaster.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qai.chat.domain.ids import SubAgentSessionId
from qai.chat.domain.sub_agent_session import SubAgentSessionStatus
from qai.platform.logging import get_logger
from qai.platform.tasks import TaskRegistry

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.application.ports import SubAgentSessionRepositoryPort

__all__ = [
    "SubAgentStreamBroadcaster",
    "SubAgentStreamEntry",
    "SubAgentStreamFrame",
]

logger = get_logger(__name__)


#: TTL for terminal sub-agent entries in the broadcast registry. After the
#: terminal mark the entry stays around at least this long so a late SSE
#: subscriber can still backfill the full event list. Older terminal entries
#: are GC'd lazily on the next :meth:`register`. Mirrors the App Builder
#: ``_RUN_BUFFER_TTL_S`` value (10 min — generous; frames are tiny dicts).
_TTL_S: float = 600.0


@dataclass(frozen=True, slots=True)
class SubAgentStreamFrame:
    """One buffered sub-agent event with a monotonic sequence.

    ``payload`` is the raw ``SubAgentEvent`` dict the sub-agent loop yields
    (``{"type": "subagent_output", "index": ..., "content": ...}`` etc.).
    The chat context owns this frame type; it is NOT shared with any other
    context (§3.2). ``sequence`` is a per-sub-agent monotonic counter the
    broadcaster assigns on :meth:`SubAgentStreamBroadcaster.publish` so the
    SSE wire can carry it for client-side ordering / de-dup.

    Note: the buffer begins at ``subagent_start`` — the broadcaster entry is
    registered right after the sub-agent's id is resolved
    (``_resolve_session``) and BEFORE the start event is emitted, so the
    start frame (now carrying ``subagent_id``) is captured into the buffer
    and a standalone tab that backfills sees it too. The frontend stream path
    still treats ``subagent_start`` as a block pre-allocate hint.
    """

    sequence: int
    payload: dict[str, Any]


@dataclass
class SubAgentStreamEntry:
    """Per-sub-agent broadcast state shared by the publisher + subscribers.

    Mirrors :class:`RunStreamEntry`: a frame buffer, a set of
    per-subscriber wake-up events, and terminal bookkeeping.
    """

    frames: list[SubAgentStreamFrame] = field(default_factory=list)
    # Per-subscriber wake-up events (R-6 / D7). Each subscriber registers its
    # own ``asyncio.Event``; ``publish`` / ``mark_terminal`` set them all and
    # each subscriber clears only its own, so no subscriber can swallow
    # another's wake-up.
    subscribers: set[asyncio.Event] = field(default_factory=set)
    done: bool = False
    terminal_at: float | None = None  # monotonic ts when ``done`` flipped
    # Next sequence number to assign — monotonic per entry.
    _next_seq: int = 0


class SubAgentStreamBroadcaster:
    """Application-layer sub-agent event broadcast registry + SSE replay.

    A single instance is held by the chat DI namespace
    (``container.chat.subagent_stream_broadcaster``); it lives for the
    process lifetime so the sub-agent loop's publisher and the
    ``GET /api/chat/subagents/{id}/stream`` subscribers share the same
    in-process frame buffers.
    """

    def __init__(
        self,
        *,
        buffer_ttl_s: float = _TTL_S,
        replay_wait_s: float = 1.0,
        idle_ticks_before_snapshot_check: int = 30,
    ) -> None:
        self._streams: dict[str, SubAgentStreamEntry] = {}
        self._buffer_ttl_s = buffer_ttl_s
        # Fix 3 (defence-in-depth) stall-guard tunables — the per-tick replay
        # wait window and how many CONSECUTIVE idle ticks elapse before the
        # replay consults the persisted snapshot to break a would-be dead-wait
        # on an entry that never flipped ``done``. Defaults keep production
        # behaviour (1.0s wait, ~30s before the snapshot fallback); tests inject
        # tiny values to exercise the fallback quickly.
        self._replay_wait_s = replay_wait_s
        self._idle_ticks_before_snapshot_check = idle_ticks_before_snapshot_check
        # R-3 — hold strong refs to background tasks so they are not GC'd
        # mid-flight and are cancelled on :meth:`aclose`. (Used by callers
        # that want the broadcaster to own a drain/abort task per sub-agent.)
        self._tasks = TaskRegistry()

    # ---- registry mutators -------------------------------------------

    def register(self, subagent_id: str) -> SubAgentStreamEntry:
        """Allocate and store a fresh broadcast entry for ``subagent_id``.

        Lazily evicts terminal entries older than the configured TTL so
        abandoned sub-agents do not accumulate indefinitely. Idempotent
        per logical run: a fresh entry replaces any prior one for the same
        id (a wake / new run starts a new buffer).

        **Sequence monotonicity across runs (resume)** — a new entry
        inherits the prior entry's ``_next_seq`` cursor when one exists
        (a resume of the same sub-agent). Frame sequence numbers are then
        strictly monotonic within the *sub-agent id's lifetime* — not just
        within a single run's buffer. This lets a client that reconnects
        after a resume pass ``from_seq=<last_applied_seq>`` and receive
        ONLY frames it hasn't seen (whether they belong to the prior run
        that TTL-evicted or the new run that just began), never an echo
        of already-rendered content. Without this inheritance the new
        entry would restart at 0 and the client's ``last_applied_seq``
        (say 500 from the prior run) would silently swallow every frame
        of the new run until it caught up — a user-visible "resume tab
        stays frozen" bug. See ``replay`` for the consumer side.
        """
        now = time.monotonic()
        # Capture the sequence-lifetime cursor BEFORE the TTL eviction pass —
        # otherwise a resume that arrives after this sub-agent's OWN prior
        # entry has aged past TTL would evict that entry (line below), see
        # `prior=None`, and restart sequences at 0. A client that still holds
        # `last_applied_seq` from the prior run (e.g. a sub-agent tab that
        # stayed open across the TTL window) would then send `from_seq=<big>`
        # on its next re-subscribe and the replay would silently swallow
        # every new-run frame until the counter caught up — a "resume tab
        # stays frozen" bug that the docstring above promises NOT to have.
        # `_streams.get` is O(1); the extra lookup costs nothing.
        prior = self._streams.get(subagent_id)
        expired = [
            sid
            for sid, entry in self._streams.items()
            if entry.done
            and entry.terminal_at is not None
            and now - entry.terminal_at > self._buffer_ttl_s
        ]
        for sid in expired:
            self._streams.pop(sid, None)

        entry = SubAgentStreamEntry()
        # Sequence-lifetime inheritance: when a prior entry for this
        # sub-agent still exists (resume / rapid re-register), continue its
        # monotonic sequence counter so ``from_seq``-based replay stays
        # correct across runs. Frames of the new run get sequences strictly
        # greater than the last frame of the prior run, matching the
        # ``last_applied_seq`` value clients hold from before the swap.
        # Note: `prior` is captured pre-eviction (above), so it survives even
        # when the prior entry has aged past TTL — resume across a long
        # idle interval still preserves the sequence invariant.
        if prior is not None:
            entry._next_seq = prior._next_seq
        self._streams[subagent_id] = entry
        return entry

    def publish(self, subagent_id: str, payload: dict[str, Any]) -> None:
        """Append a frame carrying ``payload`` and wake subscribers.

        No-op when no entry is registered for ``subagent_id`` (the
        sub-agent ran without persistence / before the entry was created).
        Assigns the next monotonic sequence to the buffered frame.
        """
        entry = self._streams.get(subagent_id)
        if entry is None:
            return
        frame = SubAgentStreamFrame(sequence=entry._next_seq, payload=dict(payload))
        entry._next_seq += 1
        entry.frames.append(frame)
        for ev in set(entry.subscribers):
            ev.set()

    def mark_terminal(self, subagent_id: str) -> None:
        """Mark the sub-agent's broadcast entry terminal and wake subscribers.

        The terminal *content* (the ``subagent_done`` / ``subagent_error``
        payload) is published as an ordinary frame via :meth:`publish`
        BEFORE this call; ``mark_terminal`` only flips the loop-exit flag so
        each subscriber drains its remaining frames then closes.
        """
        entry = self._streams.get(subagent_id)
        if entry is None:
            return
        entry.done = True
        entry.terminal_at = time.monotonic()
        for ev in set(entry.subscribers):
            ev.set()

    def trim_before(self, subagent_id: str, seq: int) -> None:
        """Drop buffered frames whose ``sequence < seq``.

        Architectural alignment with the main-agent
        :class:`ChatStreamBroadcaster` (whose buffer holds **only** the
        currently-streaming turn — already-committed turns live in the
        conversation history). Sub-agent broadcaster historically retained
        EVERY published frame for the entry's whole lifetime, so:

          1. ``SubAgentSession.record_messages`` finalizes round *R* into the
             persisted transcript (the authoritative HTTP snapshot), AND
          2. the broadcaster's buffer **still** held every frame that
             produced that transcript.

        A standalone sub-agent tab that opened via
        ``GET /api/chat/subagents/{id}`` (HTTP snapshot — full transcript)
        and then subscribed to ``/ws`` (broadcaster cursor=0 replay — same
        frames again, tagged ``backfill: true``) would render the
        transcript TWICE — exactly the user-reported 2×/3× duplication
        (07:33 AM identical block repeated). The frontend had no way to
        distinguish "snapshot already covered this" from "this is new".

        After this call the buffer's semantic matches the main-agent
        broadcaster: it holds **only** the frames produced AFTER the latest
        ``record_messages`` checkpoint — i.e. frames the persisted snapshot
        does NOT yet cover. A late subscriber's cursor=0 replay is then
        precisely complementary to the snapshot, not an echo of it.

        Called by ``agent_tool._record_running_round`` /
        ``_finalize_session`` right AFTER ``session.record_messages``
        succeeds — via the :meth:`trim_all_published` convenience wrapper
        so the adapter never touches the entry's ``_next_seq`` cursor
        directly.

        ``sequence`` values are NOT renumbered — they stay monotonic so
        live subscribers' cursors and frame-ordering invariants are
        preserved (a subscriber that already advanced past ``seq`` simply
        keeps following from where it was; a fresh subscriber finds the
        first remaining frame at some ``sequence >= seq`` and processes
        from there). No-op when no entry is registered.
        """
        entry = self._streams.get(subagent_id)
        if entry is None:
            return
        # Linear scan is fine: frames are sorted by sequence (publish-time
        # append-only), so we just find the first frame with sequence>=seq
        # and slice. List size is bounded by "frames since last record_*",
        # which is a single round's worth in practice (~tens of frames).
        cut = 0
        for i, frame in enumerate(entry.frames):
            if frame.sequence >= seq:
                cut = i
                break
        else:
            cut = len(entry.frames)
        if cut > 0:
            entry.frames = entry.frames[cut:]

    def trim_all_published(self, subagent_id: str) -> None:
        """Drop EVERY frame published so far for ``subagent_id``.

        Convenience wrapper over :meth:`trim_before` for the common "the
        persisted snapshot now covers everything I've published up to now"
        checkpoint (called from ``agent_tool._record_running_round`` +
        ``_finalize_session`` right after ``session.record_messages`` /
        ``session.save`` succeeds). Encapsulates the sequence-cursor lookup
        so callers do not read the entry's ``_next_seq`` write cursor from
        the outside — that field is broadcaster-internal state, not part of
        the port surface.

        No-op when no entry is registered (mirrors :meth:`trim_before`).
        After this returns, ``entry.frames`` is empty AND ``entry._next_seq``
        is unchanged — the next :meth:`publish` continues assigning
        monotonic sequences from where the trim happened, so live
        subscribers' cursors and the ``from_seq``-based resume protocol
        keep working unchanged.
        """
        entry = self._streams.get(subagent_id)
        if entry is None:
            return
        # `trim_before(_next_seq)` — every frame currently in the buffer has
        # ``sequence < _next_seq`` (publish assigns then increments), so this
        # drops the whole buffer while preserving the sequence counter.
        self.trim_before(subagent_id, entry._next_seq)

    def get(self, subagent_id: str) -> SubAgentStreamEntry | None:
        """Return the broadcast entry for ``subagent_id`` (or ``None``)."""
        return self._streams.get(subagent_id)

    def spawn(self, coro: Any, *, name: str) -> "asyncio.Task[Any]":
        """Own a background task (held strongly; cancelled on :meth:`aclose`).

        Used by callers (e.g. the interrupt path) that want a per-sub-agent
        task whose lifetime is bounded by the broadcaster.
        """
        return self._tasks.spawn(coro, name=name)

    async def aclose(self) -> None:
        """Cancel all outstanding background tasks (R-3).

        Called from the app ``lifespan`` shutdown path so abandoned
        coroutines do not leak past process shutdown. Idempotent.
        """
        await self._tasks.cancel_all()

    # ---- SSE replay state machine ------------------------------------

    async def replay(
        self,
        subagent_id: SubAgentSessionId,
        *,
        repository: "SubAgentSessionRepositoryPort",
        from_seq: int = 0,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(event_name, payload)`` tuples for an SSE / WS subscriber.

        New subscribers see the full frame history (the registry is
        cumulative) then live frames until the terminal mark. When the
        registry has no entry (server restarted / TTL expired) the generator
        falls back to a single repository snapshot of the persisted
        :class:`SubAgentSession` so a polling client still learns the result.

        ``from_seq`` (default 0 — full replay, byte-parity with the historical
        contract) — a resuming / reconnecting client that has already applied
        frames up to sequence *S* passes ``from_seq=S + 1`` so the broadcaster
        emits ONLY frames with ``sequence >= from_seq``. This mirrors the
        main-agent ``ChatStreamBroadcaster.replay(from_seq=...)`` design and
        lets the client stitch a resumed WS onto its already-rendered
        transcript without any duplication OR any client-side sequence
        dedupe layer — the backend simply doesn't send what the client has
        already applied. Combined with the ``register``-time sequence
        inheritance (see :meth:`register`), this is correct across the
        full sub-agent id lifetime (multiple runs / resumes).

        The caller (route layer) encodes each tuple to SSE / WS wire bytes.
        """
        entry = self._streams.get(subagent_id.value)
        # Hold the delegate generator explicitly so we can ``aclose`` it
        # deterministically on client disconnect — otherwise the delegate's
        # ``finally`` (which discards the per-subscriber event, R-6) would
        # only run on GC, leaking the subscriber registration.
        inner = (
            self._replay_without_entry(subagent_id, repository)
            if entry is None
            else self._replay_with_entry(
                subagent_id, entry, repository, from_seq=from_seq,
            )
        )
        try:
            async for ev in inner:
                yield ev
        except Exception as exc:  # noqa: BLE001 — convert to error frame
            yield (
                "error",
                {
                    "type": "InternalServerError",
                    "code": "internal.unexpected",
                    "message": str(exc) or type(exc).__name__,
                },
            )
        finally:
            await inner.aclose()

    async def _replay_without_entry(
        self,
        subagent_id: SubAgentSessionId,
        repository: "SubAgentSessionRepositoryPort",
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Fallback: no broadcast entry → single persisted snapshot.

        The per-frame history is unrecoverable after a restart, but the
        client still sees the sub-agent's status + final result text so a
        late-opened standalone tab is not left blank.
        """
        session = await repository.find(subagent_id)
        if session is None:
            yield (
                "error",
                {
                    "type": "NotFoundError",
                    "code": "chat.subagent_not_found",
                    "message": f"sub-agent session {subagent_id.value} not found",
                    "details": {"subagent_id": subagent_id.value},
                },
            )
            return
        # Surface the persisted status as a ``state`` frame, then a closing
        # ``done`` frame. The wire history itself is served by the snapshot
        # REST endpoint (``GET /api/chat/subagents/{id}``) the client already
        # fetched before subscribing; here we only need to report terminality
        # so the client stops waiting for live frames.
        yield (
            "state",
            {
                "subagent_id": session.id.value,
                "status": session.status.value,
                "rounds": session.rounds,
            },
        )
        is_error = session.status is SubAgentSessionStatus.ERROR
        yield (
            "done" if not is_error else "error",
            {
                "subagent_id": session.id.value,
                "status": session.status.value,
                "rounds": session.rounds,
            }
            if not is_error
            else {
                "type": "DomainError",
                "code": "chat.subagent_failed",
                "message": "sub-agent run failed",
                "details": {
                    "subagent_id": session.id.value,
                    "status": session.status.value,
                },
            },
        )

    async def _replay_with_entry(
        self,
        subagent_id: SubAgentSessionId,
        entry: SubAgentStreamEntry,
        repository: "SubAgentSessionRepositoryPort",
        *,
        from_seq: int = 0,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Live + backfilled replay loop bound to a broadcast entry.

        Loop terminates when the registry flips to ``done`` AND the cursor
        has caught up with the buffer. Each buffered ``SubAgentStreamFrame``
        is emitted as an ``event: frame`` carrying ``{sequence, payload}``;
        the payload's ``type`` field (``subagent_output`` etc.) lets the
        client dispatch to the same renderer the parent stream uses.

        ``from_seq`` filters *every* yielded frame (backfill AND live) to
        ``frame.sequence >= from_seq``. A reconnecting client passes its
        highest-applied sequence so the loop emits ONLY unseen frames —
        no duplication, no client-side dedupe. When 0 (default), semantics
        are byte-identical to the pre-``from_seq`` contract (full replay).
        """
        my_event = asyncio.Event()
        entry.subscribers.add(my_event)
        # Fix 3 (defence-in-depth) — bounded stall guard so a broadcast entry
        # that NEVER flips ``done`` cannot wedge this replay (and, through it,
        # the ``subagent_ws`` that awaits its exhaustion) FOREVER. The root
        # cause of the "sub-agent tab never settles" bug was a cancel path that
        # bypassed ``mark_terminal`` (now fixed at ``agent_tool.py`` iter_events
        # finally); this guard is a SECOND line of defence so any FUTURE
        # miss-to-mark-terminal path degrades to a bounded fallback instead of a
        # permanent dead-wait: after several idle ticks with no new frame we
        # consult the AUTHORITATIVE persisted session; if it is already terminal
        # the live entry is simply never going to be marked (the producer is
        # gone), so we emit the closing ``done`` and return. A still-``running``
        # snapshot means the producer is legitimately mid-round → keep waiting.
        _idle_ticks = 0
        _IDLE_TICKS_BEFORE_SNAPSHOT_CHECK = (
            self._idle_ticks_before_snapshot_check
        )
        try:
            # CURSOR-BY-SEQUENCE (bugfix: 子 Agent 独立标签页 round 3-8 的工具卡/
            # 输出在执行期间完全不显示、直到子 Agent 结束才一次性刷出).
            #
            # ROOT CAUSE of the old bug: the cursor used to be a LIST INDEX into
            # ``entry.frames`` (``entry.frames[cursor]``). But ``trim_before`` /
            # ``trim_all_published`` — called after EVERY round's
            # ``record_messages`` — slices the list head (``frames = frames[cut:]``),
            # so the surviving frames shift left by ``cut`` while a live
            # subscriber's absolute index cursor does NOT. After a trim the
            # index pointed PAST the freshly-published frames, silently skipping
            # every frame produced between the subscriber's position and the
            # trim boundary (rounds 3-8 vanished; the subscriber jumped straight
            # to round 9). The docstring's claim that "sequence values are not
            # renumbered so cursors are preserved" was FALSE for an index cursor.
            #
            # FIX: track the highest sequence already EMITTED (``last_seq``) and
            # emit every buffered frame with ``sequence > last_seq``. Sequences
            # are monotonic and stable across trims, so trimming the list head
            # can never make us skip an un-emitted frame. ``from_seq`` gating is
            # folded in by seeding ``last_seq = from_seq - 1``.
            last_seq = from_seq - 1

            def _drain_new_frames() -> list[SubAgentStreamFrame]:
                """Buffered frames with ``sequence > last_seq``, in order."""
                return [f for f in entry.frames if f.sequence > last_seq]

            # Frames already buffered when this subscriber attaches are
            # BACKFILL (history): by the time a late / re-subscribing client's
            # WS finishes connecting, the sub-agent (same process) may have
            # produced many frames — these are NOT live deltas the user can
            # watch appear, they are the transcript-so-far. We tag them
            # ``backfill: true`` so the client applies them in ONE synchronous
            # batch (instant, correct for history) instead of pushing each
            # through its ~16ms rAF / 50ms coalescer — which, with a burst of
            # buffered frames, would塌缩 the whole so-far transcript into a
            # single paint AND make a fast resume run look "一次性刷出". Frames
            # that arrive AFTER the first drain (woken by ``publish``) are LIVE
            # (``backfill`` omitted) and the client streams them frame-by-frame.
            # ``live`` flips to True the first time we block on ``my_event`` —
            # i.e. once the initial buffer is drained and we are following the
            # publisher in real time.
            live = False
            while True:
                for frame in _drain_new_frames():
                    last_seq = frame.sequence
                    payload: dict[str, Any] = {
                        "sequence": frame.sequence,
                        "payload": frame.payload,
                    }
                    if not live:
                        payload["backfill"] = True
                    yield ("frame", payload)
                    if not live:
                        # Perf (B2): yield control to the event loop after each
                        # buffered BACKFILL frame so the same-process cursor-0
                        # replay does not burst every queued frame into a single
                        # microtask batch on the client. ``sleep(0)`` only
                        # reschedules onto the ready queue (no timer), so it adds
                        # no measurable latency. Live frames (below) are already
                        # naturally spread by the publisher, so no yield needed.
                        await asyncio.sleep(0)

                # Terminal? Emit a closing ``done`` event and exit.
                if entry.done and not _drain_new_frames():
                    yield ("done", {"subagent_id": subagent_id.value})
                    return

                # Wait for the next frame OR the terminal flag. The timeout
                # is a safety belt so a stalled publisher cannot wedge the
                # SSE stream — on each tick the outer loop re-checks the
                # buffer + terminal flag. From here on every frame is LIVE.
                live = True
                try:
                    await asyncio.wait_for(
                        my_event.wait(), timeout=self._replay_wait_s
                    )
                    _idle_ticks = 0
                except asyncio.TimeoutError:
                    # Idle window elapsed with no publish / terminal. Count it;
                    # after enough consecutive idle ticks fall back to the
                    # persisted snapshot (Fix 3 defence-in-depth). If the
                    # session is already terminal the in-memory entry will never
                    # be marked (producer gone) → close cleanly instead of
                    # dead-waiting. A ``running`` snapshot → keep following.
                    _idle_ticks += 1
                    if (
                        not entry.done
                        and _idle_ticks >= _IDLE_TICKS_BEFORE_SNAPSHOT_CHECK
                    ):
                        _idle_ticks = 0
                        try:
                            _snap = await repository.find(subagent_id)
                        except Exception:  # noqa: BLE001 — best-effort probe
                            _snap = None
                        if (
                            _snap is not None
                            and _snap.status is not SubAgentSessionStatus.RUNNING
                        ):
                            # Emit any frames still queued (cursor may lag),
                            # then a closing ``done`` and return so the WS layer
                            # sends ``{type:done}`` and closes the socket.
                            for frame in _drain_new_frames():
                                last_seq = frame.sequence
                                yield (
                                    "frame",
                                    {
                                        "sequence": frame.sequence,
                                        "payload": frame.payload,
                                    },
                                )
                            yield ("done", {"subagent_id": subagent_id.value})
                            return
                finally:
                    my_event.clear()
        finally:
            entry.subscribers.discard(my_event)
