# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Persist the MB Pro Agent's connect-time greeting burst as a chat message.

When the Pro toolbar's「连接」succeeds, the remote MB Pro Agent proactively
pushes a fixed 3-event SSE burst (verified live 2026-06-29):

* ``queue_state`` — global busy/idle snapshot (who else is running what)
* ``agent_ready`` — end-of-turn signal
* ``turn``        — the agent's self-introduction text

Before this use case existed, ``flush_pending_events`` discarded the whole
burst because the next user-message turn would otherwise see the stale
``agent_ready`` and close empty. We instead consume the burst HERE (between
connect-success and the first turn), map it through :class:`MbProMapper` like
any other agent output, **persist** the result as a standalone assistant
message, and **broadcast** the frames via :class:`ChatStreamBroadcaster` so
any already-attached active-run subscriber renders them in real time.

Standalone assistant message (no paired user prompt) follows the same shape
as ``orchestrate_discussion._persist_implementation_summary`` (assistant
message with ``sender_id=None`` + ``meta.kind`` marker — verified pattern,
no domain constraint violated).

internal-only by composition: the bridge that constructs this use case is
itself gated behind ``settings.is_internal``; on external editions the bridge
returns ``None`` and the route short-circuits, so the concrete infrastructure
mapper is never built.

Clean Architecture: this application-layer module MUST NOT import
``qai.chat.infrastructure`` (layered contract). The event→frame mapping is a
collaborator abstracted behind the :class:`GreetingMapperPort` protocol below
and injected by the ``apps/api`` composition root
(``_mb_pro_session_bridge``), where the infra :class:`MbProMapper` /
:class:`QueryMappingContext` legitimately live. This mirrors the existing
``ConversationRepositoryPort`` injection this same use case already uses.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from qai.chat.application.ports import ConversationRepositoryPort
from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.ids import ConversationId, MessageId, TabId
from qai.chat.domain.message import Message
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.application.chat_stream_broadcaster import ChatStreamBroadcaster

__all__ = [
    "GreetingMapperPort",
    "GreetingSequencerPort",
    "PersistMbProGreetingInput",
    "PersistMbProGreetingUseCase",
]

_log = get_logger(__name__)


@runtime_checkable
class GreetingSequencerPort(Protocol):
    """Per-stream frame-sequence allocator handed to the greeting mapper.

    Application-layer twin of the infra ``QueryMappingContext`` surface the
    mapper needs (a monotonic sequence counter) without this layer importing
    infrastructure. ``QueryMappingContext`` structurally satisfies it.
    """

    def take_sequence(self) -> int:
        """Return the current sequence number and advance the counter."""
        ...


@runtime_checkable
class GreetingMapperPort(Protocol):
    """Map connect-time greeting events to :class:`StreamFrame` values.

    Application-layer twin of the infra ``QueryEventMapper`` protocol; the
    concrete :class:`MbProMapper` (wrapped with a fresh context factory) is
    injected by ``_mb_pro_session_bridge`` so the application layer never names
    an infrastructure type. ``new_context`` mints the per-stream sequencer the
    use case also uses to stamp the terminal END frame.
    """

    def new_context(
        self, *, my_session_id: str | None = None
    ) -> GreetingSequencerPort:
        """Return a fresh per-stream sequencer/context for one greeting drain.

        ``my_session_id`` (when provided) tells the mapper which remote-agent
        session this drain is bound to, so global broadcasts (``queue_state``
        etc.) that name a DIFFERENT owner can be filtered — those events
        belong to another tab's task and must not be rendered as ours.
        """
        ...

    def map_event(
        self,
        event: dict[str, Any],
        ctx: GreetingSequencerPort,
    ) -> Iterable[StreamFrame]:
        """Map one greeting event to zero or more frames, stamping ``ctx``."""
        ...

# Hard cap on drain wait so a stuck/dying agent does not block the bridge's
# fire-and-forget task forever. Real bursts settle in <100ms; 2s is generous.
_DRAIN_TIMEOUT_S: float = 2.0


@dataclass(frozen=True, slots=True, kw_only=True)
class PersistMbProGreetingInput:
    """Inputs to :class:`PersistMbProGreetingUseCase`.

    ``conversation_id`` is REQUIRED — the bridge resolves it (creating a fresh
    conversation if the tab is brand-new) before invoking the use case, so this
    layer never has to know about lazy-create semantics. ``tab_id`` keys the
    session-manager registry (the per-tab :class:`SessionManager` instance
    holding the just-drained event queue).
    """

    conversation_id: str
    tab_id: str


class PersistMbProGreetingUseCase:
    """Drain greeting events, persist as assistant message, broadcast frames."""

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        broadcaster: "ChatStreamBroadcaster",
        peek_manager: Any,
        ids: IdGenerator,
        greeting_mapper: GreetingMapperPort,
    ) -> None:
        self._conversations = conversations
        self._broadcaster = broadcaster
        self._peek_manager = peek_manager
        self._ids = ids
        self._greeting_mapper = greeting_mapper

    def reserve_broadcast(
        self, *, tab_id: str, conversation_id: str
    ) -> bool:
        """Pre-register the broadcaster entry SYNCHRONOUSLY at connect time.

        Returning True signals the entry was reserved (no other stream owns
        this tab). The fire-and-forget :meth:`execute` body is then free to
        publish into it. Returning False means another turn already owns the
        tab's stream slot — :meth:`execute` will detect the same condition and
        bail out cleanly, so the caller can still kick it off; the boolean is
        mostly for tests / logging.
        """
        from qai.chat.domain.ids import ConversationId, TabId

        entry = self._broadcaster.register(
            tab_id=TabId.of(tab_id),
            conversation_id=ConversationId.of(conversation_id),
        )
        return entry is not None

    async def execute(self, request: PersistMbProGreetingInput) -> None:
        tab_id = TabId.of(request.tab_id)
        conv_id = ConversationId.of(request.conversation_id)

        manager = self._peek_manager(tab_id.value)
        if manager is None or not manager.is_connected:
            _log.debug(
                "mb_pro_greeting.no_manager",
                tab_id=tab_id.value,
                conversation_id=conv_id.value,
            )
            return

        # The broadcast entry was reserved synchronously at connect time via
        # :meth:`reserve_broadcast` (so the frontend WS attach can't race it
        # to a 404). Confirm it is still present before we publish — a
        # disconnect between connect-success and now would have torn it down.
        if self._broadcaster.get(tab_id) is None:
            _log.info(
                "mb_pro_greeting.broadcast_entry_gone",
                tab_id=tab_id.value,
            )
            return

        # Event→frame mapping is an injected collaborator (GreetingMapperPort)
        # so this application module never imports infrastructure. The concrete
        # MbProMapper + QueryMappingContext are supplied by the apps bridge.
        # Pass this tab's remote session_id so the mapper can filter global
        # broadcasts (``queue_state`` etc.) that name a different owner.
        my_sid = manager.get_state().session_id
        ctx = self._greeting_mapper.new_context(my_session_id=my_sid)

        chunk_parts: list[str] = []
        reasoning_parts: list[str] = []
        frames: list[StreamFrame] = []
        event_count = 0
        async for event in manager.drain_greeting(timeout=_DRAIN_TIMEOUT_S):
            event_count += 1
            for frame in self._greeting_mapper.map_event(event, ctx):
                frames.append(frame)
                text = _frame_text(frame)
                if frame.frame_type is StreamFrameType.CHUNK and text:
                    chunk_parts.append(text)
                elif frame.frame_type is StreamFrameType.REASONING and text:
                    reasoning_parts.append(text)

        # Persist the assistant's self-intro text (skipped on reconnect, where
        # ``chunk_parts`` is empty because the remote emits no greeting burst).
        # ``queue_state`` is intentionally NOT persisted — it's a transient
        # snapshot. The reasoning frame still rides the live broadcast.
        persisted_text = "".join(chunk_parts).strip()
        if persisted_text:
            await self._persist_assistant_message(
                conv_id=conv_id, text=persisted_text
            )

        # Broadcast collected frames + terminal END (closes the WS attach
        # cleanly via ``done`` envelope). Always publish END so a reconnect's
        # empty burst still terminates the attached subscriber.
        for frame in frames:
            self._broadcaster.publish(tab_id, frame)
        self._broadcaster.publish(
            tab_id,
            StreamFrame.end(
                frame_id=self._ids.new_id(),
                sequence=ctx.take_sequence(),
                reason="completed",
            ),
        )
        self._broadcaster.mark_terminal(tab_id)
        _log.info(
            "mb_pro_greeting.broadcast",
            tab_id=tab_id.value,
            conversation_id=conv_id.value,
            events=event_count,
            frames=len(frames),
            persisted=bool(persisted_text),
        )

    async def _persist_assistant_message(
        self, *, conv_id: ConversationId, text: str
    ) -> None:
        conv: Conversation = await self._conversations.get(conv_id)
        msg = Message(
            id=MessageId(self._ids.new_id()),
            role=MessageRole.ASSISTANT,
            content=MessageContent(text=text),
            created_at=datetime.now(UTC),
            model_id="query::mb_pro",
            sender_id=None,
            meta={"kind": "mb_pro_greeting"},
        )
        conv.append_message(msg)
        await self._conversations.save_messages(conv)


def _frame_text(frame: StreamFrame) -> str:
    """Read the text payload of a CHUNK / REASONING frame (or return ``""``)."""
    payload = frame.payload
    if not isinstance(payload, dict):
        return ""
    val = payload.get("text")
    if isinstance(val, str):
        return val
    return ""
