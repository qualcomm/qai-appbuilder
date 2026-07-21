# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-process ordinary chat stream broadcast + replay state machine.

The primary chat WS/SSE routes stream frames directly to the initiating
connection.  The active-runs UI also needs to attach to an already running turn
from another tab/window.  This broadcaster provides that in-process fan-out: the
route that owns the model stream publishes every :class:`StreamFrame`, while
late subscribers replay buffered frames and then follow live frames until the
turn becomes terminal.

State is intentionally process-local.  In-flight LLM streams are process-local
too, and startup already cleans stale persisted ``streaming`` tab state.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrame

__all__ = [
    "ActiveChatRunSnapshot",
    "ChatStreamBroadcaster",
    "ChatStreamEntry",
    "ChatStreamReplayFrame",
]

_TTL_S: float = 600.0
_MAX_FRAMES: int = 4096


@dataclass(frozen=True, slots=True)
class ChatStreamReplayFrame:
    """One replayable ordinary-chat stream frame."""

    sequence: int
    frame: StreamFrame
    backfill: bool = False


@dataclass(frozen=True, slots=True)
class ActiveChatRunSnapshot:
    """Read-side projection for one ordinary chat active run."""

    tab_id: TabId
    conversation_id: ConversationId | None
    title: str | None
    model_id: str | None
    model_provider: str | None
    started_at: datetime
    last_active_at: datetime
    aborted: bool
    reason: str | None
    terminal: bool


@dataclass
class ChatStreamEntry:
    """Per-tab broadcast state shared by publisher and subscribers."""

    conversation_id: ConversationId | None
    title: str | None
    model_id: str | None
    model_provider: str | None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_active_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    frames: list[ChatStreamReplayFrame] = field(default_factory=list)
    subscribers: set[asyncio.Event] = field(default_factory=set)
    aborted: bool = False
    reason: str | None = None
    done: bool = False
    terminal_at: float | None = None
    _next_seq: int = 0


class ChatStreamBroadcaster:
    """Application-layer ordinary chat stream broadcast registry."""

    def __init__(
        self,
        *,
        buffer_ttl_s: float = _TTL_S,
        max_frames: int = _MAX_FRAMES,
    ) -> None:
        self._streams: dict[str, ChatStreamEntry] = {}
        self._buffer_ttl_s = buffer_ttl_s
        self._max_frames = max(1, int(max_frames))

    def register(
        self,
        *,
        tab_id: TabId,
        conversation_id: ConversationId | None,
        title: str | None = None,
        model_id: str | None = None,
        model_provider: str | None = None,
    ) -> ChatStreamEntry | None:
        """Allocate a fresh broadcast entry for an ordinary chat turn.

        Returns ``None`` when the same tab already owns a non-terminal entry.
        Routes use that ownership signal to avoid a failed duplicate send from
        overwriting or terminal-marking the real in-flight stream.
        """
        self._evict_expired()
        existing = self._streams.get(tab_id.value)
        if existing is not None and not existing.done:
            return None
        entry = ChatStreamEntry(
            conversation_id=conversation_id,
            title=title,
            model_id=model_id,
            model_provider=model_provider,
        )
        self._streams[tab_id.value] = entry
        return entry

    def publish(self, tab_id: TabId, frame: StreamFrame) -> None:
        """Buffer ``frame`` and wake all subscribers. No-op if unregistered."""
        entry = self._streams.get(tab_id.value)
        if entry is None:
            return
        entry.last_active_at = datetime.now(UTC)
        replay_frame = ChatStreamReplayFrame(
            sequence=entry._next_seq,
            frame=frame,
        )
        entry._next_seq += 1
        entry.frames.append(replay_frame)
        overflow = len(entry.frames) - self._max_frames
        if overflow > 0:
            del entry.frames[:overflow]
        for ev in set(entry.subscribers):
            ev.set()

    def mark_aborted(self, tab_id: TabId, *, reason: str) -> None:
        """Record that stop was requested for this run and wake subscribers."""
        entry = self._streams.get(tab_id.value)
        if entry is None:
            return
        entry.aborted = True
        entry.reason = reason
        entry.last_active_at = datetime.now(UTC)
        for ev in set(entry.subscribers):
            ev.set()

    def mark_terminal(self, tab_id: TabId) -> None:
        """Mark a run terminal after its final frame has been published."""
        entry = self._streams.get(tab_id.value)
        if entry is None:
            return
        entry.done = True
        entry.terminal_at = time.monotonic()
        entry.last_active_at = datetime.now(UTC)
        for ev in set(entry.subscribers):
            ev.set()

    def get(self, tab_id: TabId) -> ChatStreamEntry | None:
        return self._streams.get(tab_id.value)

    def list_active(self) -> tuple[ActiveChatRunSnapshot, ...]:
        """Return non-terminal ordinary chat runs currently tracked."""
        self._evict_expired()
        items: list[ActiveChatRunSnapshot] = []
        for raw_tab_id, entry in self._streams.items():
            if entry.done:
                continue
            items.append(
                ActiveChatRunSnapshot(
                    tab_id=TabId.of(raw_tab_id),
                    conversation_id=entry.conversation_id,
                    title=entry.title,
                    model_id=entry.model_id,
                    model_provider=entry.model_provider,
                    started_at=entry.started_at,
                    last_active_at=entry.last_active_at,
                    aborted=entry.aborted,
                    reason=entry.reason,
                    terminal=entry.done,
                ),
            )
        return tuple(sorted(items, key=lambda item: item.started_at))

    async def replay(
        self,
        tab_id: TabId,
        *,
        from_seq: int = 0,
    ) -> AsyncIterator[ChatStreamReplayFrame]:
        """Replay buffered frames and follow live frames until terminal."""
        entry = self._streams.get(tab_id.value)
        if entry is None:
            return
        my_event = asyncio.Event()
        entry.subscribers.add(my_event)
        try:
            requested_seq = max(0, int(from_seq))
            cursor = next(
                (
                    index
                    for index, frame in enumerate(entry.frames)
                    if frame.sequence >= requested_seq
                ),
                len(entry.frames),
            )
            live = False
            while True:
                while cursor < len(entry.frames):
                    buffered = entry.frames[cursor]
                    yield ChatStreamReplayFrame(
                        sequence=buffered.sequence,
                        frame=buffered.frame,
                        backfill=not live,
                    )
                    cursor += 1
                if entry.done:
                    break
                live = True
                my_event.clear()
                await my_event.wait()
        finally:
            entry.subscribers.discard(my_event)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            tab_id
            for tab_id, entry in self._streams.items()
            if entry.done
            and entry.terminal_at is not None
            and now - entry.terminal_at > self._buffer_ttl_s
        ]
        for tab_id in expired:
            self._streams.pop(tab_id, None)
