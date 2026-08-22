# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Central dispatcher: background-job completion → SYSTEM_NOTICE + coord.

Replaces the legacy multi-channel wiring — a pair of per-producer aside
bridges (sub-agent completions and background exec completions), a
mid-turn aside drain gate, and an idle-tab wakeup callback that injected
a fake user message — with ONE producer→broker path.  See
``docs/70-multi-agent/complete-solution-plan-2026-08-08.md`` §12.3 for the
full motivation (the decision to delete those channels outright).

Contract
========

The dispatcher subscribes to two domain events:

* :class:`SubAgentSessionTerminated` — fired by the sub-agent kernel
  when a run reaches ``DONE`` / ``ERROR`` / ``INTERRUPTED``.
* :class:`BackgroundProcessUpdated` — fired by the exec manager on
  every status transition; the dispatcher filters for the terminal
  subset (``TERMINAL_STATUSES``).

On each qualifying event it:

1. Builds a stable ``dedup_key`` (``subagent:<id>:<status>`` /
   ``bgp:<id>:<status>``).  The key is stored in the notice's
   ``meta[SYSTEM_NOTICE_META_DEDUP_KEY]``; the coordinator's own
   idempotent :meth:`notify` also uses it to fold a re-published
   event into the same notification.
2. Constructs a :class:`Message` with role ``SYSTEM_NOTICE``, a
   human-readable summary text, and a ``meta`` dict carrying the
   producer kind + source id.
3. Appends the message to the target conversation, persists it, and
   publishes a :class:`MessageAppendedEvent` so a live frontend WS
   session paints the card immediately (a follow-up ``ChatMessage``
   frame's usual path).
4. Notifies :class:`AgentSessionCoordinator` — which either records
   the pending key (if the tab is streaming, the live turn's
   round-end will drain us) or kicks a headless follow-up turn
   (if the tab is idle).

Notes
-----

* The dispatcher is process-scoped and shares the same lifetime as the
  event bus — DI ``start``s it during lifespan setup and ``stop``s it
  on shutdown.
* It is deliberately thin: NO complex state machine, no per-tab locks,
  no dedup sets of its own (coord handles that).  Every event goes
  through the same 4-step pipeline.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from qai.chat.adapters.async_job_manager import AsyncJobKind
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.events import (
    MessageAppendedEvent,
    SubAgentSessionTerminated,
)
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.message import (
    SYSTEM_NOTICE_KIND_BG,
    SYSTEM_NOTICE_KIND_SUBAGENT,
    SYSTEM_NOTICE_META_DEDUP_KEY,
    SYSTEM_NOTICE_META_KIND,
    SYSTEM_NOTICE_META_SOURCE_ID,
    Message,
)
from qai.chat.domain.sub_agent_error import SubAgentError, SubAgentErrorKind
from qai.platform.background_process.events import BackgroundProcessUpdated
from qai.platform.background_process.ports import TERMINAL_STATUSES
from qai.platform.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from qai.chat.adapters.agent_session_coordinator import (
        AgentSessionCoordinator,
    )
    from qai.chat.application.ports import ConversationRepositoryPort
    from qai.platform.events import EventBus, EventEnvelope
    from qai.platform.events.bus import EventSubscription
    from qai.platform.ids import IdGenerator

__all__ = ["BackgroundJobDispatcher"]

_log = get_logger(__name__)

# The exec manager's ``BackgroundProcessUpdated`` fires on every status
# transition AND on debounced output growth — the dispatcher must only
# react to the FIRST transition into a terminal state.  We track per
# process the last terminal status we already dispatched; re-firing the
# same terminal transition is idempotent thanks to the coordinator's
# own dedup, but skipping the redundant DB append keeps the transcript
# clean.
_DispatchedBgKey = str  # ``bgp:<process_id>:<status>``

#: Trailing char budget of the tail-output snippet embedded in a bgp
#: SYSTEM_NOTICE.  Long outputs get truncated to keep transcript
#: budgets sane — the LLM can still call ``background_process(logs,
#: id=...)`` for the full buffer.
_MAX_BG_TAIL_CHARS: int = 1500

#: Character cap for the sub-agent result body embedded in a
#: SYSTEM_NOTICE.  A long analysis can otherwise consume a large
#: share of the next turn's wire budget; the LLM can always call
#: ``sub_agent(action=inspect, id=...)`` to recover the full
#: persisted transcript.  Follows the reference-design constant
#: ``ASYNC_PREVIEW_MAX_CHARS`` semantics — head-truncate (keeps the
#: summary opening, which for sub-agents typically has the conclusion),
#: and append a pointer sentence naming the id the model must ask for.
_MAX_SUBAGENT_RESULT_CHARS: int = 12_000

#: Upper bound on the dispatcher's in-memory dedup ring.  A background
#: exec job's terminal transition can re-fire several times as
#: :class:`BackgroundProcessUpdated` continues to publish on output
#: growth ticks — the dispatcher keeps a small LRU of "already
#: dispatched" keys to skip the redundant DB append.  Bounded so a
#: long-running process cannot grow this set without bound.  When the
#: ring overflows the oldest key is evicted; if the same terminal
#: event re-fires after eviction, the ``_has_notice_with_key``
#: conversation-level check still prevents a duplicate row.

#: Upper bound on the dispatcher's wait-suppression ring — the set of
#: dedup keys the main agent has already picked up via a synchronous
#: ``sub_agent(wait, id=...)`` / ``background_process(wait, id=...)``.
#: When the terminal event later arrives, the dispatcher skips both
#: the SYSTEM_NOTICE append and the coordinator notify, matching the
#: reference-design ``isDeliverySuppressed`` semantics (a wait snapshot IS the
#: delivery; no duplicate ``async-result`` follows).  Bounded so a
#: long-lived session cannot grow the ring without bound.
_SUPPRESSED_RING_MAX: int = 4096
_DISPATCHED_BGP_RING_MAX: int = 4096


class BackgroundJobDispatcher:
    """Single subscriber that translates background-job completions
    into SYSTEM_NOTICE messages + coordinator notifications.

    Attributes:
        _bus: The :class:`EventBus` this dispatcher subscribes to.
        _conversations: Conversation repository — used to fetch the
            target aggregate before appending the notice.
        _coordinator: :class:`AgentSessionCoordinator` handle for the
            downstream "wake main agent" call.
        _ids: Id generator for minted :class:`Message` ids.
        _tab_resolver: Optional callable ``(session_id) -> TabId | None``
            for translating a background-process's ``session_id`` into
            the tab that spawned it.  Some session ids ARE tab ids
            directly; others (e.g. adopted exec sessions from a
            headless task) are mapped through DI wiring.  ``None`` (or
            ``None``-return) → the event is dropped as non-chat-owned.
        _conv_resolver: Callable ``(tab_id) -> ConversationId | None``
            that maps a tab to its currently-active conversation.
            Rides through DI, since the tab→conv binding lives in the
            :class:`TabRepositoryPort`.
    """

    __slots__ = (
        "_async_job_manager",
        "_bgp_job_ids",
        "_bus",
        "_conv_resolver",
        "_conversations",
        "_coordinator",
        "_default_model_provider",
        "_dispatched_bgp",
        "_ids",
        "_started",
        "_subscriptions",
        "_suppressed",
        "_tab_resolver",
        "_tab_sessions",
        "_write_queue",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        conversations: ConversationRepositoryPort,
        coordinator: AgentSessionCoordinator,
        ids: IdGenerator,
        tab_resolver: (
            type[TabId] | Any
        ) = TabId,  # tests may inject a stub; production passes TabId
        conv_resolver: Any = None,
        default_model_provider: Any = None,
        async_job_manager: Any = None,
        write_queue: Any = None,
        tab_sessions: Any = None,
    ) -> None:
        self._bus = bus
        self._conversations = conversations
        self._coordinator = coordinator
        self._ids = ids
        self._tab_resolver = tab_resolver
        self._conv_resolver = conv_resolver
        # Tab store, used ONLY to map a background process's owning
        # conversation back to a live tab id.  A bgp record is stamped with
        # the CONVERSATION id (it must outlive the tab that started it), but
        # the coordinator keys pending work by TAB — so without this lookup a
        # completion notice is addressed to a tab that does not exist and the
        # headless follow-up dies with ``chat.tab_not_found``, leaving the
        # model never woken.  ``None`` = not wired (unit stubs): the notice is
        # still persisted, only the wake-up is skipped.
        self._tab_sessions = tab_sessions
        self._default_model_provider = default_model_provider
        # Batch 5 (N.27): the async-job ledger every background bash job is
        # mirrored into, so ``has_pending_work`` and the admission cap see
        # exec jobs alongside sub-agent jobs.  ``None`` = not wired (unit
        # stubs) → the mirroring is skipped entirely; the SYSTEM_NOTICE
        # pipeline below is untouched either way.
        self._async_job_manager = async_job_manager
        # Per-conversation write serialiser (``ConversationWriteQueue``).
        # When wired, the SYSTEM_NOTICE append is submitted through it so
        # it serialises with the streaming turn's saves on the same
        # conversation and yields an ack future the coordinator can hand to
        # ``settle_pending``.  ``None`` (unit stubs) falls back to calling
        # the repository directly — same row, same ordering, just without
        # the cross-writer serialisation.
        self._write_queue = write_queue
        #: ``bgp id -> AJM job id``, so the terminal event can complete the
        #: job the running-transition registered.  A process whose RUNNING
        #: transition we never saw (manager restarted mid-flight) simply has
        #: no entry and is skipped rather than registered-then-completed.
        self._bgp_job_ids: OrderedDict[str, str] = OrderedDict()
        self._subscriptions: list[EventSubscription] = []
        self._dispatched_bgp: OrderedDict[_DispatchedBgKey, None] = OrderedDict()
        self._suppressed: OrderedDict[str, None] = OrderedDict()
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Subscribe to both event streams.  Idempotent."""
        if self._started:
            return
        sub_a = await self._bus.subscribe(
            SubAgentSessionTerminated, self._on_subagent_terminated
        )
        sub_b = await self._bus.subscribe(
            BackgroundProcessUpdated, self._on_bg_updated
        )
        self._subscriptions.extend([sub_a, sub_b])
        self._started = True
        _log.info("bg_dispatcher.started")

    async def stop(self) -> None:
        """Detach every subscription.  Safe to call multiple times."""
        for sub in self._subscriptions:
            try:
                await sub.unsubscribe()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "bg_dispatcher.unsubscribe_failed",
                    error=type(exc).__name__,
                    error_msg=str(exc),
                )
        self._subscriptions.clear()
        self._started = False

    # ------------------------------------------------------------------
    # Producer: sub-agent kernel
    # ------------------------------------------------------------------
    async def _on_subagent_terminated(self, envelope: EventEnvelope) -> None:
        ev = envelope.event
        assert isinstance(ev, SubAgentSessionTerminated)
        dedup_key = f"subagent:{ev.subagent_id}:{ev.status}"
        summary = _format_subagent_summary(ev)
        await self._dispatch(
            tab_id=ev.parent_tab_id,
            conversation_id=ev.parent_conversation_id,
            dedup_key=dedup_key,
            kind=SYSTEM_NOTICE_KIND_SUBAGENT,
            source_id=ev.subagent_id,
            content_text=summary,
            model_hint=None,
        )

    # ------------------------------------------------------------------
    # Producer: background exec manager
    # ------------------------------------------------------------------
    async def _on_bg_updated(self, envelope: EventEnvelope) -> None:
        ev = envelope.event
        assert isinstance(ev, BackgroundProcessUpdated)
        info = ev.info
        if info.status not in TERMINAL_STATUSES:
            # Batch 5 (N.27): mirror the job into the async-job ledger the
            # FIRST time we see it alive.  Done here rather than inside the
            # platform manager for two reasons: the manager is in another
            # bounded context (it cannot import ``AsyncJobManager``), and it
            # only knows a ``session_id`` — the ledger's ``owner_id`` is the
            # CONVERSATION id, which needs the tab→conv resolution this
            # dispatcher already owns.
            if info.status == "running":
                await self._register_bg_job(info)
            return
        dispatched_key = f"bgp:{info.id}:{info.status}"
        if dispatched_key in self._dispatched_bgp:
            # We already dispatched THIS process's terminal transition
            # once; the manager re-publishes the same status on further
            # output-growth ticks.
            return
        self._dispatched_bgp[dispatched_key] = None
        if len(self._dispatched_bgp) > _DISPATCHED_BGP_RING_MAX:
            # Evict oldest.  If the same terminal event re-fires after
            # eviction, ``_has_notice_with_key`` still prevents a
            # duplicate DB row (the conv-level truth source).
            self._dispatched_bgp.popitem(last=False)

        # Settle the ledger entry BEFORE the notice pipeline: the notice may
        # bail out (non-chat session / no active conv), but a registered job
        # must always reach a terminal state or it would hold an admission
        # slot and keep ``has_pending_work`` true forever.
        await self._complete_bg_job(info)

        resolved = await self._resolve_bgp_owner(info.session_id)
        if resolved is None:
            _log.debug(
                "bg_dispatcher.bg.no_active_conv",
                process_id=info.id,
                session_id=info.session_id,
            )
            return
        tab_id, conversation_id = resolved

        summary = _format_bg_summary(info)
        await self._dispatch(
            tab_id=tab_id,
            conversation_id=conversation_id,
            dedup_key=dispatched_key,
            kind=SYSTEM_NOTICE_KIND_BG,
            source_id=info.id,
            content_text=summary,
            model_hint=None,
        )

    async def _register_bg_job(self, info: Any) -> None:
        """Mirror a newly-running background process into the job ledger.

        Idempotent per ``bgp`` id: the manager re-publishes ``running`` on
        every output-growth tick, and only the first one registers.
        """
        ajm = self._async_job_manager
        if ajm is None or info.id in self._bgp_job_ids:
            return
        resolved = await self._resolve_bgp_owner(info.session_id)
        if resolved is None:
            return
        _tab_id, conversation_id = resolved
        try:
            job_id = await ajm.register_job(
                kind=AsyncJobKind.BASH,
                owner_id=conversation_id.value,
                agent_id=info.id,
            )
        except Exception as exc:  # noqa: BLE001 — the ledger is bookkeeping;
            # a failure here must never break the completion pipeline.
            _log.warning(
                "bg_dispatcher.ajm_register_failed",
                process_id=info.id,
                error=type(exc).__name__,
                error_msg=str(exc),
            )
            return
        self._bgp_job_ids[info.id] = job_id
        if len(self._bgp_job_ids) > _DISPATCHED_BGP_RING_MAX:
            self._bgp_job_ids.popitem(last=False)

    async def _complete_bg_job(self, info: Any) -> None:
        """Settle the ledger entry for a background process that ended.

        A non-zero exit / signal becomes a
        :class:`~qai.chat.domain.sub_agent_error.SubAgentError` with kind
        ``EXECUTION`` carrying the output tail.  The class name is
        sub-agent-flavoured but its semantics are "async job error"
        (documented decision — a parallel ``AsyncJobError`` would be
        duplicate design for identical data).
        """
        ajm = self._async_job_manager
        if ajm is None:
            return
        job_id = self._bgp_job_ids.pop(info.id, None)
        if job_id is None:
            return
        tail = (info.output or "").strip()
        if len(tail) > _MAX_BG_TAIL_CHARS:
            tail = "…" + tail[-_MAX_BG_TAIL_CHARS:]
        failed = info.status == "failed" or (
            info.exit_code is not None and info.exit_code != 0
        )
        try:
            if failed:
                await ajm.complete_job(
                    job_id,
                    error=SubAgentError(
                        kind=SubAgentErrorKind.EXECUTION,
                        message=(
                            f"Background command exited with"
                            f" status={info.status}"
                            f" exit_code={info.exit_code}"
                            + (f"\n{tail}" if tail else "")
                        ),
                        subagent_id=info.id,
                    ),
                )
            else:
                await ajm.complete_job(job_id, result=tail)
        except Exception as exc:  # noqa: BLE001 — bookkeeping only
            _log.warning(
                "bg_dispatcher.ajm_complete_failed",
                process_id=info.id,
                job_id=job_id,
                error=type(exc).__name__,
                error_msg=str(exc),
            )

    # ------------------------------------------------------------------
    # Public: wait-suppression API — called by ``sub_agent(wait)`` /
    # ``background_process(wait)`` when the main agent picks up a
    # result synchronously.  Follows the reference-design
    # ``AsyncJobManager.markDeliverySuppressed(jobId)`` pattern: the wait
    # snapshot IS the delivery, so the eventual terminal event MUST
    # NOT append a second SYSTEM_NOTICE or wake the coordinator
    # (which would fire a redundant follow-up turn).
    # ------------------------------------------------------------------
    def mark_delivered(self, dedup_key: str) -> None:
        """Mark ``dedup_key`` as already delivered via a synchronous
        wait — the next dispatch attempt with this key will be a
        no-op.  Safe to call before or after the terminal event
        lands: the check is stateful, not order-dependent.

        Keys accepted here MUST match the format the dispatcher
        builds internally — ``subagent:<id>:<status>`` or
        ``bgp:<id>:<status>``.  A caller that isn't sure of the
        status may call this with each terminal status it accepts
        (``done`` / ``error`` / ``interrupted`` for sub-agent;
        ``exited`` / ``failed`` / ``stopped`` for exec) — extra
        entries are harmless and evicted by the LRU."""
        if dedup_key in self._suppressed:
            self._suppressed.move_to_end(dedup_key)
            return
        self._suppressed[dedup_key] = None
        if len(self._suppressed) > _SUPPRESSED_RING_MAX:
            self._suppressed.popitem(last=False)

    # ------------------------------------------------------------------
    # Core pipeline — shared by both producers.
    # ------------------------------------------------------------------
    async def _dispatch(
        self,
        *,
        tab_id: TabId,
        conversation_id: ConversationId,
        dedup_key: str,
        kind: str,
        source_id: str,
        content_text: str,
        model_hint: str | None,
    ) -> None:
        # Wait-suppression — the main agent already picked up this
        # completion synchronously via ``sub_agent(wait)`` /
        # ``background_process(wait)``.  Skip both the DB append and
        # the coordinator notify (a redundant follow-up turn would
        # re-hand the same result the LLM has just seen).
        if dedup_key in self._suppressed:
            _log.info(
                "bg_dispatcher.suppressed_by_wait",
                dedup_key=dedup_key,
                tab_id=str(tab_id),
            )
            return

        # 1. Load the conversation aggregate.
        try:
            conv = await self._conversations.get(conversation_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "bg_dispatcher.conv_load_failed",
                dedup_key=dedup_key,
                conv_id=str(conversation_id),
                error=type(exc).__name__,
                error_msg=str(exc),
            )
            return

        # Resolve ``model_hint`` — the coordinator threads this into
        # :meth:`StreamChatUseCase.run_headless_followup` for the
        # headless-followup turn.  A ``None`` here bypasses the
        # provider-routing wrapper and falls through to the offline
        # default, which emits the sentinel
        # ``"[no LLM endpoint configured]"`` — the reported failure
        # mode when a background job settles on an idle tab.  Ladder
        # (mirrors ``ScheduledTaskToolHandler._resolve_conversation_
        # model``): passed-in > last assistant msg with ``model_id`` >
        # user prefs default > ``None``.
        model_hint = await self._resolve_model_hint(
            conv=conv, initial=model_hint,
        )

        # 2. Check if this dedup_key is ALREADY persisted on the conv.
        # Same key twice means the same terminal event fired twice
        # (e.g. reload after the manager already emitted the exit
        # transition once).  Never double-append the row.
        if _has_notice_with_key(conv, dedup_key):
            _log.info(
                "bg_dispatcher.already_persisted",
                dedup_key=dedup_key,
                conv_id=str(conversation_id),
            )
            # Still notify the coordinator — the pending state may
            # have been lost across a process restart even though the
            # DB row is intact.
            await self._coordinator.notify(
                tab_id=tab_id,
                conversation_id=conversation_id,
                dedup_key=dedup_key,
                model_hint=model_hint,
            )
            return

        # 3. Build + persist the SYSTEM_NOTICE message.
        try:
            msg = _build_system_notice(
                ids=self._ids,
                content_text=content_text,
                dedup_key=dedup_key,
                kind=kind,
                source_id=source_id,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "bg_dispatcher.msg_build_failed",
                dedup_key=dedup_key,
                error=type(exc).__name__,
                error_msg=str(exc),
            )
            return

        # Persist the SYSTEM_NOTICE row via the ATOMIC single-row
        # append (§concurrent-writer refactor 2026-08-07).  Previously
        # this used ``conv.append_message(msg) + save_messages(conv)``,
        # which rewrites EVERY message row on the conversation from an
        # in-memory snapshot (DELETE all + INSERT all).  Under
        # concurrent writers — a streaming turn holding its own snapshot
        # loaded 10s+ earlier, or a headless follow-up spawned by the
        # coordinator — the writer that finishes second silently
        # clobbers the other's newly-appended rows.  The reported
        # symptom: dispatcher appends sub-agent-2's SYSTEM_NOTICE while
        # headless-1 is still streaming; when headless-1 saves its own
        # snapshot at completion, sub-agent-2's SYSTEM_NOTICE disappears
        # from the DB — the frontend never sees the second grey card,
        # and the drain loop's next follow-up (headless-2) has no wire
        # entry for it, generating an empty "I already summarised that"
        # reply.  ``append_message_atomic`` writes ONLY the new row and
        # computes its position inside the transaction so parallel
        # snapshot-based saves and this atomic append never cross-clobber.
        # When a :class:`ConversationWriteQueue` is wired the append is
        # SUBMITTED here rather than executed inline, so it serialises
        # behind the same per-conversation worker the streaming turn's
        # saves go through — the cross-clobber above becomes structurally
        # impossible instead of merely unlikely.  It rides the queue's
        # PRIORITY lane, so it costs one transaction and never waits out
        # the save lane's 100ms batch window.
        #
        # We still AWAIT the ack before step 5's ``notify``, keeping the
        # persist-strictly-before-notify invariant the whole §6.4 pipeline
        # is built on: on an IDLE tab ``notify`` kicks a headless
        # follow-up whose first act is to re-read this conversation and
        # rebuild its wire, and there is no gate between the two.  A
        # deferred write there is exactly the "wire never sees the notice,
        # follow-up runs with no context" failure the atomic append was
        # introduced to fix.
        try:
            write_ack = await self._submit_notice_write(
                conversation_id=conversation_id, message=msg,
            )
            await write_ack
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "bg_dispatcher.persist_failed",
                dedup_key=dedup_key,
                conv_id=str(conversation_id),
                error=type(exc).__name__,
                error_msg=str(exc),
            )
            return

        # 4. Publish a MessageAppendedEvent so a live frontend paints
        # the card immediately (streaming session sees it as a normal
        # transcript delta).
        try:
            await self._bus.publish(
                MessageAppendedEvent(
                    conversation_id=conversation_id,
                    message_id=msg.id,
                    role=MessageRole.SYSTEM_NOTICE.value,
                    appended_at=msg.created_at,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # Failure to publish is not fatal — the row is on disk;
            # the frontend will pick it up on next history reload.
            _log.warning(
                "bg_dispatcher.publish_failed",
                dedup_key=dedup_key,
                error=type(exc).__name__,
                error_msg=str(exc),
            )

        # 5. Notify the coordinator — this is the tell-the-agent step.
        await self._coordinator.notify(
            tab_id=tab_id,
            conversation_id=conversation_id,
            dedup_key=dedup_key,
            model_hint=model_hint,
        )
        # 6. Hand the write's ack future to the coordinator, so a live
        # turn's ``settle_pending`` can distinguish "this notice's row is
        # durable" from "nobody told me either way".  Must come AFTER
        # ``notify``: ``register_pending_future`` only attaches to an
        # already-pending key and no-ops otherwise.  Since we awaited the
        # ack above the future is already resolved, which is precisely the
        # signal ``settle_pending`` needs — it reports the notice as
        # flushed instead of guessing from an empty future map.
        self._coordinator.register_pending_future(
            tab_id=tab_id, dedup_key=dedup_key, future=write_ack,
        )
        _log.info(
            "bg_dispatcher.dispatched",
            kind=kind,
            dedup_key=dedup_key,
            tab_id=str(tab_id),
            conv_id=str(conversation_id),
        )

    async def _submit_notice_write(
        self, *, conversation_id: ConversationId, message: Message,
    ) -> Awaitable[None]:
        """Persist ``message`` and return an awaitable for its durability.

        With a write queue wired this is the queue's ack future; without
        one the repository call happens inline and the returned awaitable
        is already-settled.  Either way the caller awaits the same thing,
        so the persist-before-notify invariant holds in both wirings.

        An exception here (or from awaiting the result) means the row did
        not land — the caller must abort the dispatch rather than notify
        the agent about a notice it will never be able to read.
        """
        queue = self._write_queue
        if queue is None:
            await self._conversations.append_message_atomic(
                conversation_id=conversation_id,
                message=message,
            )
            settled: asyncio.Future[None] = (
                asyncio.get_running_loop().create_future()
            )
            settled.set_result(None)
            return settled
        return await queue.submit_append_atomic(
            conversation_id.value,
            message,
            conversation_id=conversation_id,
        )

    # ------------------------------------------------------------------
    # Resolvers — thin wrappers so tests can inject stubs.
    # ------------------------------------------------------------------
    async def _resolve_model_hint(
        self, *, conv: Any, initial: str | None,
    ) -> str | None:
        """Best-effort resolution ladder for the headless follow-up
        ``model_hint``.

        Order (first hit wins):

        1. ``initial`` — the event-supplied model, when non-empty.
           Currently the producers pass ``None``; the field is kept so
           future producers can supply a hint directly.
        2. The most recent ``assistant`` message in the conversation
           that recorded a ``model_id`` — same signal the WebUI sends
           as ``model_hint`` on a live turn, so a woken turn stays on
           the same endpoint the user was chatting on.
        3. :attr:`_default_model_provider` — the user's globally
           selected model (``ui.preferences.selected_model_id``).  Used
           when the conv has no persisted assistant turn yet (rare —
           usually there IS one since the sub-agent's parent turn
           finished before it).
        4. ``None`` — the caller (coordinator → headless runner →
           LLM adapter) sees ``model_hint=None`` and falls through to
           the offline default (the ``[no LLM endpoint configured]``
           notice), matching the pre-refactor scheduled-task /
           park-wakeup fallback semantics.
        """
        if isinstance(initial, str) and initial:
            return initial
        messages = getattr(conv, "messages", ())
        for msg in reversed(messages):
            role = getattr(msg, "role", None)
            if getattr(role, "value", role) != "assistant":
                continue
            mid = getattr(msg, "model_id", None)
            if isinstance(mid, str) and mid:
                return mid
        if self._default_model_provider is not None:
            try:
                result = self._default_model_provider()
                if inspect.isawaitable(result):
                    result = await result
            except Exception:  # noqa: BLE001 — best-effort
                result = None
            if isinstance(result, str) and result:
                return result
        return None

    def _resolve_tab_id(self, session_id: str) -> TabId | None:
        if callable(self._tab_resolver) and self._tab_resolver is not TabId:
            try:
                return self._tab_resolver(session_id)
            except Exception:  # noqa: BLE001
                return None
        # Default: session_id IS the tab id string.
        try:
            return TabId.of(session_id)
        except Exception:  # noqa: BLE001
            return None

    async def _resolve_conversation_id(
        self, tab_id: TabId,
    ) -> ConversationId | None:
        if self._conv_resolver is None:
            return None
        try:
            result = self._conv_resolver(tab_id)
            if inspect.isawaitable(result):
                result = await result
        except Exception:  # noqa: BLE001
            return None
        if isinstance(result, ConversationId):
            return result
        if isinstance(result, str) and result:
            return ConversationId(result)
        return None

    async def _resolve_bgp_owner(
        self, session_id: str,
    ) -> tuple[TabId, ConversationId] | None:
        """Resolve a background process's ``session_id`` to ``(tab, conv)``.

        Sub-agent notices arrive already carrying a real tab id, but a
        background process does not: the two chat producers stamp the
        manager's ``session_id`` with the CONVERSATION id (``di.py``'s
        ``adopt(session_id=_own_key)`` where ``_own_key`` is
        ``_soft_ctx.conversation_id``, and the ``background_process`` tool
        bridge does the same), because a bgp record must outlive the tab that
        started it.

        The tab-first path is therefore tried but MUST NOT be trusted blindly:
        ``TabId.of`` performs no membership check, so a conversation id sails
        through it and the resulting phantom tab makes the tab→conversation
        lookup raise ``TabNotFound``.  That is exactly how completion notices
        for managed execs were being dropped (logged as
        ``bg_dispatcher.bg.no_active_conv`` and never delivered), which broke
        the ``[Backgrounded]`` receipt's promise that a result arrives on its
        own.  So when the tab route yields no conversation we fall back to
        reading ``session_id`` AS the conversation id — the actual contract of
        both chat producers.

        Returns ``None`` for a session that is not a chat conversation at all
        (e.g. the App Builder's own sentinel session): with no conversation to
        append a notice to there is nothing to deliver, which is the correct
        outcome rather than an error.
        """
        tab_id = self._resolve_tab_id(session_id)
        if tab_id is not None:
            conv = await self._resolve_conversation_id(tab_id)
            if conv is not None:
                return tab_id, conv
        # ``session_id`` is the conversation id (the common case for both chat
        # producers).  The conversation is easy; the TAB is the part that must
        # be looked up rather than invented: the coordinator keys pending work
        # by tab, and ``TabId.of`` performs no membership check, so passing the
        # conversation id through it yields a tab that does not exist and the
        # headless follow-up dies with ``chat.tab_not_found`` — the notice is
        # persisted but the model is never woken (observed 2026-08-10).
        try:
            conv_id = ConversationId(session_id)
        except Exception:  # noqa: BLE001 — unusable session id
            return None
        if not await self._conversation_exists(conv_id):
            return None
        tab = await self._live_tab_for_conversation(conv_id)
        if tab is None:
            _log.debug(
                "bg_dispatcher.bg.no_live_tab",
                conversation_id=conv_id.value,
            )
            return None
        return tab, conv_id

    async def _live_tab_for_conversation(
        self, conv_id: ConversationId,
    ) -> TabId | None:
        """The active tab currently bound to ``conv_id``, or ``None``.

        A background process outlives tabs by design, so its record carries the
        conversation.  Waking the agent, however, happens on a tab.  This walks
        the live tab set instead of deriving an id, because a derived id is
        exactly what produced the phantom-tab failure this method exists to
        avoid.  ``None`` (no store wired, store failure, conversation has no
        open tab) means the notice stays in the transcript and is folded in by
        whichever turn the user drives next — strictly better than addressing a
        tab that is not there.
        """
        store = self._tab_sessions
        if store is None:
            return None
        try:
            tabs = await store.list_active()
        except Exception:  # noqa: BLE001 — store unavailable
            return None
        for tab in tabs or ():
            tab_conv = getattr(tab, "conversation_id", None)
            if getattr(tab_conv, "value", tab_conv) == conv_id.value:
                tab_id = getattr(tab, "id", None)
                if tab_id is not None:
                    return tab_id
        return None

    async def _conversation_exists(self, conv_id: ConversationId) -> bool:
        """Whether ``conv_id`` names a real conversation.

        Guards the ``session_id``-as-conversation fallback so a non-chat
        session (the App Builder's sentinel session, a unit stub) cannot
        fabricate notices against an id no conversation owns.

        ``get`` RAISES for an unknown id (it returns ``Conversation``, not an
        optional), so absence is read from the exception rather than a ``None``
        return — the same call the dispatcher already relies on at
        :meth:`_dispatch`.
        """
        try:
            await self._conversations.get(conv_id)
        except Exception:  # noqa: BLE001 — unknown id / repo failure
            return False
        return True


# ---------------------------------------------------------------------
# Helpers — pure, unit-testable, kept module-private.
# ---------------------------------------------------------------------


def _has_notice_with_key(conv: Any, dedup_key: str) -> bool:
    """Return ``True`` iff ``conv`` already carries a SYSTEM_NOTICE
    message whose meta dedup key matches ``dedup_key``.  Walks the
    aggregate's messages tuple — cheap for realistic conversation
    sizes (≤ a few hundred messages)."""
    messages = getattr(conv, "messages", ())
    for msg in messages:
        role = getattr(msg, "role", None)
        if role is not MessageRole.SYSTEM_NOTICE:
            continue
        meta = getattr(msg, "meta", None)
        if not isinstance(meta, dict):
            continue
        if meta.get(SYSTEM_NOTICE_META_DEDUP_KEY) == dedup_key:
            return True
    return False


def _build_system_notice(
    *,
    ids: IdGenerator,
    content_text: str,
    dedup_key: str,
    kind: str,
    source_id: str,
) -> Message:
    """Construct a frozen :class:`Message` for a background-job notice."""
    from qai.chat.domain.ids import MessageId  # local — avoid cycle

    return Message(
        id=MessageId.generate(ids),
        role=MessageRole.SYSTEM_NOTICE,
        content=MessageContent(text=content_text),
        created_at=datetime.now(UTC),
        meta={
            SYSTEM_NOTICE_META_DEDUP_KEY: dedup_key,
            SYSTEM_NOTICE_META_KIND: kind,
            SYSTEM_NOTICE_META_SOURCE_ID: source_id,
        },
    )


def _format_subagent_summary(ev: SubAgentSessionTerminated) -> str:
    """Human-readable summary text for a sub-agent completion notice.

    Format: ``Sub-agent <name> (<id>) finished with status=<status>.
    Result: <result>``

    Empty ``result_text`` is elided so the notice never carries the
    literal ``"(no output)"`` — the LLM can decide to invoke
    ``sub_agent(action=inspect)`` if it wants the raw transcript.
    """
    parts = [
        f"Sub-agent \"{ev.subagent_name}\" ({ev.subagent_id}) finished with status={ev.status}."
    ]
    if ev.result_text:
        result_body = ev.result_text
        if len(result_body) > _MAX_SUBAGENT_RESULT_CHARS:
            # Head-truncate: for a sub-agent's final message the
            # conclusion is usually up top (task result / decision
            # summary), and the tail is supporting detail.  The LLM
            # is told to call ``sub_agent(action=inspect)`` for
            # the full transcript.
            result_body = (
                result_body[:_MAX_SUBAGENT_RESULT_CHARS]
                + f"\n[Result truncated at {_MAX_SUBAGENT_RESULT_CHARS} chars — "
                + f"call sub_agent(action=inspect, id={ev.subagent_id}) for the full transcript.]"
            )
        parts.append(f"Result: {result_body}")
    parts.append(
        "You may now continue based on this outcome, "
        "or call sub_agent(action=inspect, id=...) for the full transcript."
    )
    return " ".join(parts)


def _format_bg_summary(info: Any) -> str:
    """Human-readable summary text for a background-process completion."""
    tail = (info.output or "").strip()
    if len(tail) > _MAX_BG_TAIL_CHARS:
        tail = "…" + tail[-_MAX_BG_TAIL_CHARS:]
    exit_bits = []
    if info.exit_code is not None:
        exit_bits.append(f"exit={info.exit_code}")
    if info.signal is not None:
        exit_bits.append(f"signal={info.signal}")
    exit_desc = f" ({', '.join(exit_bits)})" if exit_bits else ""
    parts = [
        f"Background process \"{info.command}\" (id={info.id}) finished "
        f"with status={info.status}{exit_desc}."
    ]
    if tail:
        parts.append(f"Tail output:\n{tail}")
    parts.append(
        "You may now continue based on this outcome, or call "
        "background_process(action=logs, id=...) for full output."
    )
    return "\n".join(parts)
