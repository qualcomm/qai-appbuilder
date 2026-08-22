# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Relay MB Pro session events to the chat stream broadcaster.

The Pro toolbar's「连接」opens a long-lived SSE stream to the MB Pro Agent.
Two entry points can trigger a turn on that session:

1.  The user types in the QAI chat composer → the ``query::mb_pro`` route
    drives one turn via :class:`SessionQueryServiceAdapter`, which owns the
    ``chat_stream_broadcaster`` entry for that tab and yields SSE-encoded
    :class:`StreamFrame` values to the browser.
2.  The user clicks a button INSIDE the embedded MB Pro builder-panel iframe
    (e.g. "创建转换任务") → the same MB Pro Agent produces the same SSE event
    stream, but no QAI chat turn drives it.  This use case bridges that gap.

Every MB Pro session event is fan-out'd through
:meth:`SessionManager.subscribe` to a subscriber queue.  This use case is a
long-lived task that:

* Detects each ``user_msg`` event as a new turn boundary and tries to
  ``broadcaster.register`` the tab.  Success → publish every subsequent
  mapper-produced :class:`StreamFrame` (with :class:`MbProTurnRounder`
  stamping) until ``agent_ready`` closes the turn (or the SSE reader dies).
* If ``register`` returns ``None`` — the ordinary chat SSE path is already
  serving a turn for this tab — enter *shadow* mode: keep feeding the mapper
  so its ``_delta_buf`` / ``pending_usage`` stay coherent with the event
  stream, but drop every frame.  The next turn boundary re-tries the
  registration.
* On task cancellation (Pro disconnect), unsubscribe cleanly.

Layered contract: the use case never imports infrastructure directly.  The
concrete :class:`MbProMapper` / :class:`MbProTurnRounder` / the subscriber
adapter (wrapping ``SessionManager``) are injected by the apps composition
root (``_mb_pro_session_bridge._build_relay_use_case``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrame
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.application.chat_stream_broadcaster import ChatStreamBroadcaster

__all__ = [
    "RelayMapperPort",
    "RelayRounderPort",
    "RelaySubscriberPort",
    "RelayMbProSessionInput",
    "RelayMbProSessionUseCase",
]

_log = get_logger(__name__)


@runtime_checkable
class RelayMapperPort(Protocol):
    """Turn a single MB Pro SSE event into zero or more StreamFrames.

    Application-layer twin of ``QueryEventMapper`` + ``QueryMappingContext``
    scoped to what this use case needs.  ``new_context`` mints a per-turn
    context (fresh delta buffer + sequence counter); ``map_event`` translates
    one event through the current context.  The concrete adapter (built at
    the composition root) wraps the infrastructure :class:`MbProMapper`.

    ``current_ctx`` is passed on every call so the use case can share ONE
    mapper instance across a whole turn (the mapper is stateless per-call
    but the context carries the delta buffer).
    """

    def new_context(
        self, *, my_session_id: str | None = None
    ) -> Any:
        """Return a fresh per-turn context/sequencer."""
        ...

    def map_event(self, event: dict[str, Any], ctx: Any) -> Iterable[StreamFrame]:
        """Map one MB Pro SSE event to zero or more :class:`StreamFrame`s."""
        ...


@runtime_checkable
class RelayRounderPort(Protocol):
    """Round-index + tool-timing stamper across one turn.

    Application-layer twin of :class:`MbProTurnRounder`.  Kept as a Port so
    the application module never imports the infra class directly.  The
    concrete instance is built fresh per relay task (persists across all
    turns of one MB Pro session).
    """

    def new_turn(self) -> None:
        """Reset per-turn text-run pointer (round_index keeps growing)."""
        ...

    def apply(self, frame: StreamFrame) -> StreamFrame:
        """Stamp round_index / emitted_at_ms / duration_ms as appropriate."""
        ...


@runtime_checkable
class RelaySubscriberPort(Protocol):
    """Subscribe to a MB Pro session's SSE event stream.

    Application-layer twin of :class:`SessionManager`'s subscribe / peek APIs.
    The queue is populated by the manager's SSE reader; the sentinel closed
    event is enqueued when the reader dies (see
    ``session_adapter._SSE_CLOSED_EVENT``).
    """

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new subscriber queue and return it."""
        ...

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Deregister a previously subscribed queue."""
        ...

    def peek_session_id(self) -> str | None:
        """Return the manager's current remote session id (for filtering)."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class RelayMbProSessionInput:
    """Inputs to :class:`RelayMbProSessionUseCase.execute`.

    ``tab_id`` keys the broadcaster registry (each tab owns an independent
    MB Pro session and an independent broadcaster entry).  ``conversation_id``
    is the persistence anchor the broadcaster stamps into each entry — the
    frontend uses it to route the frames to the right chat conversation.
    """

    tab_id: str
    conversation_id: str


class RelayMbProSessionUseCase:
    """Long-lived relay: MB Pro events → ChatStreamBroadcaster frames.

    Kicked off (``asyncio.create_task``) by ``MbProSessionController.connect``
    right after a fresh session is established; cancelled on ``disconnect``.
    """

    __slots__ = (
        "_broadcaster",
        "_subscriber",
        "_mapper",
        "_rounder_factory",
        "_ids",
    )

    def __init__(
        self,
        *,
        broadcaster: "ChatStreamBroadcaster",
        subscriber: RelaySubscriberPort,
        mapper: RelayMapperPort,
        rounder_factory: Any,
        ids: IdGenerator,
    ) -> None:
        self._broadcaster = broadcaster
        self._subscriber = subscriber
        self._mapper = mapper
        self._rounder_factory = rounder_factory
        self._ids = ids

    async def execute(self, request: RelayMbProSessionInput) -> None:
        """Consume events from ``subscriber`` and publish frames until cancel.

        State machine per event:

        * ``idle``: waiting for the next ``user_msg`` (turn start).  Any
          intervening event is ignored (turns should always start with
          ``user_msg``; a stray ``text_delta`` while idle is dropped).
        * ``turn_active``: mapper + rounder + broadcaster.publish for every
          event.  ``agent_ready`` publishes an END frame + ``mark_terminal``
          and returns to idle.
        * ``shadow``: same event pump but every frame is dropped.  Enters
          when ``broadcaster.register`` returned ``None`` (chat SSE path
          already owns the tab).  ``agent_ready`` returns to idle.
        """
        tab_id = TabId.of(request.tab_id)
        conv_id = ConversationId.of(request.conversation_id)
        queue = self._subscriber.subscribe()
        # ONE rounder for the whole session lifetime — round_index grows
        # monotonically across every turn so a late tool_result still binds
        # to its earlier tool_call's round.  ``new_turn`` only resets the
        # text-run pointer + FIFOs.
        rounder = self._rounder_factory()

        _State = str  # noqa: N806  # sentinel for readability
        state: _State = "idle"
        ctx: Any = None

        try:
            while True:
                event = await queue.get()
                if event.get("__sse_closed__") is True:
                    if state != "idle":
                        # Reader died mid-turn — publish a terminal END so
                        # the frontend's active-run WS closes cleanly.
                        self._publish_end(tab_id, ctx, rounder, reason="failed")
                        self._broadcaster.mark_terminal(tab_id)
                    return
                evt_type = event.get("type")
                if evt_type == "user_msg" and state == "idle":
                    # Race-window sleep: the QAI chat SSE path registers the
                    # broadcaster entry SYNCHRONOUSLY the moment the user
                    # submits a chat message, but that register only lands
                    # after the network hop the events_ws signal channel
                    # also depends on.  Both this relay AND events_ws sleep
                    # ~100ms before deciding ownership so the chat SSE
                    # register wins by construction on the common case
                    # (see ``interfaces/http/routes/mb_pro_session.py``).
                    # Without this sleep the relay would routinely beat the
                    # chat SSE register — enter turn_active — and publish a
                    # duplicate frame stream to the same broadcaster entry
                    # while the browser was already consuming the SSE stream
                    # directly (the "every character appears twice" bug).
                    await asyncio.sleep(0.1)
                    # Try to open a fresh broadcaster entry for this turn.
                    entry = self._broadcaster.register(
                        tab_id=tab_id, conversation_id=conv_id, from_relay=True
                    )
                    if entry is None:
                        # Chat SSE path owns the tab; run in shadow mode until
                        # the next agent_ready boundary.
                        state = "shadow"
                        ctx = self._mapper.new_context(
                            my_session_id=self._subscriber.peek_session_id()
                        )
                        rounder.new_turn()
                        _log.info(
                            "mb_pro.relay.shadow_turn_started",
                            tab_id=tab_id.value,
                        )
                    else:
                        state = "turn_active"
                        ctx = self._mapper.new_context(
                            my_session_id=self._subscriber.peek_session_id()
                        )
                        rounder.new_turn()
                        _log.info(
                            "mb_pro.relay.turn_started",
                            tab_id=tab_id.value,
                            conversation_id=conv_id.value,
                        )
                if state == "idle":
                    # Not in a turn yet — nothing to publish.  A stray
                    # ``queue_state`` / ``turn`` before the first ``user_msg``
                    # is dropped silently (mapper state carries over the next
                    # turn's fresh context anyway).
                    continue
                # In a turn (real or shadow): feed the mapper + rounder.
                frames = list(self._mapper.map_event(event, ctx))
                for frame in frames:
                    stamped = rounder.apply(frame)
                    if state == "turn_active":
                        self._broadcaster.publish(tab_id, stamped)
                # End of turn: publish END + mark_terminal, reset state.
                if evt_type == "agent_ready":
                    if state == "turn_active":
                        self._publish_end(
                            tab_id, ctx, rounder, reason="completed"
                        )
                        self._broadcaster.mark_terminal(tab_id)
                    state = "idle"
                    ctx = None
        except asyncio.CancelledError:
            # Disconnect path — clean up.  If a turn was in flight, publish
            # a terminal END so any attached WS closes rather than hanging.
            if state == "turn_active" and ctx is not None:
                try:
                    self._publish_end(tab_id, ctx, rounder, reason="failed")
                    self._broadcaster.mark_terminal(tab_id)
                except Exception:  # noqa: BLE001 — never mask CancelledError
                    _log.warning(
                        "mb_pro.relay.terminal_publish_failed",
                        tab_id=tab_id.value,
                        exc_info=True,
                    )
            raise
        except Exception:  # noqa: BLE001 — never crash the connect task
            _log.warning(
                "mb_pro.relay.unexpected_error",
                tab_id=tab_id.value,
                exc_info=True,
            )
            if state == "turn_active" and ctx is not None:
                try:
                    self._publish_end(tab_id, ctx, rounder, reason="failed")
                    self._broadcaster.mark_terminal(tab_id)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._subscriber.unsubscribe(queue)

    def _publish_end(
        self,
        tab_id: TabId,
        ctx: Any,
        rounder: RelayRounderPort,
        *,
        reason: str,
    ) -> None:
        """Emit a terminal END frame stamped through the rounder."""
        end_frame = StreamFrame.end(
            frame_id=self._ids.new_id(),
            sequence=ctx.take_sequence(),
            reason=reason,
        )
        self._broadcaster.publish(tab_id, rounder.apply(end_frame))
