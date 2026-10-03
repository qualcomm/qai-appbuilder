# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Minimal in-memory test doubles for the chat application ports.

These are plain, dependency-free stand-ins for :class:`ConversationRepositoryPort`,
:class:`TabSessionStorePort`, :class:`StreamAbortRegistryPort`, :class:`Clock`
and :class:`IdGenerator` — enough surface for unit tests that construct a
:class:`StreamChatUseCase` (or a sibling use case) directly, without a real
database or event loop plumbing. Not a production adapter; no persistence,
no concurrency guarantees beyond what a single-threaded test needs.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from qai.chat.adapters.stream_abort_registry import InMemoryStreamAbortRegistry
from qai.chat.application.ports import ConversationListItem, MessagesPage
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.errors import ConversationNotFoundError, TabNotFoundError
from qai.chat.domain.ids import ConversationId
from qai.chat.domain.tab import ConversationTab, TabStatus


__all__ = [
    "FakeClock",
    "FakeConversationRepository",
    "FakeIdGenerator",
    "FakeStreamAbortRegistry",
    "FakeTabSessionStore",
]


class FakeClock:
    """Deterministic :class:`Clock` with a no-arg constructor.

    Starts at a fixed UTC instant and only moves when :meth:`sleep` /
    :meth:`sleep_async` or :meth:`advance` is called — real wall-clock time
    never leaks into a test's assertions.
    """

    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._monotonic_ns = 0

    def now(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        return self._monotonic_ns

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"advance() requires seconds >= 0, got {seconds!r}")
        self._now = self._now + timedelta(seconds=seconds)
        self._monotonic_ns += int(seconds * 1_000_000_000)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    async def sleep_async(self, seconds: float) -> None:
        self.advance(seconds)


class FakeIdGenerator:
    """Deterministic :class:`IdGenerator`: ``id-1``, ``id-2``, ... per instance."""

    def __init__(self, *, prefix: str = "id") -> None:
        self._prefix = prefix
        self._counter = itertools.count(1)

    def new_id(self) -> str:
        return f"{self._prefix}-{next(self._counter)}"


# ``InMemoryStreamAbortRegistry`` is already a complete, dependency-free,
# in-memory implementation of ``StreamAbortRegistryPort`` (promoted out of
# the DI module for exactly this reason) — no need to re-implement it.
FakeStreamAbortRegistry = InMemoryStreamAbortRegistry


class FakeTabSessionStore:
    """In-memory :class:`TabSessionStorePort`."""

    def __init__(self) -> None:
        self._tabs: dict[str, ConversationTab] = {}

    async def save(self, tab: ConversationTab) -> None:
        self._tabs[tab.id.value] = tab

    async def get(self, tab_id) -> ConversationTab:
        try:
            return self._tabs[tab_id.value]
        except KeyError:
            raise TabNotFoundError(tab_id.value) from None

    async def find(self, tab_id) -> ConversationTab | None:
        return self._tabs.get(tab_id.value)

    async def delete(self, tab_id) -> None:
        try:
            del self._tabs[tab_id.value]
        except KeyError:
            raise TabNotFoundError(tab_id.value) from None

    async def list_active(self) -> tuple[ConversationTab, ...]:
        return tuple(
            tab for tab in self._tabs.values() if tab.status != TabStatus.CLOSED
        )


class FakeConversationRepository:
    """In-memory :class:`ConversationRepositoryPort`.

    Stores the aggregate BY REFERENCE (not a deep copy) — a test that
    mutates ``conv`` in place (e.g. ``conv.append_message(m)``) before
    calling :meth:`save` sees exactly that state on the next :meth:`get`,
    mirroring how a real session object is used within one test.
    """

    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}

    async def save(self, conversation: Conversation) -> None:
        self._conversations[conversation.id.value] = conversation

    async def save_messages(self, conversation: Conversation, *, conn=None) -> None:
        self._conversations[conversation.id.value] = conversation

    async def save_meta(self, conversation: Conversation) -> None:
        self._conversations[conversation.id.value] = conversation

    async def append_message_atomic(
        self, conversation_id: ConversationId, message, *, conn=None
    ) -> None:
        conv = await self.get(conversation_id)
        conv.messages.append(message)

    async def get(self, conversation_id: ConversationId) -> Conversation:
        try:
            return self._conversations[conversation_id.value]
        except KeyError:
            raise ConversationNotFoundError(conversation_id.value) from None

    async def find(self, conversation_id: ConversationId) -> Conversation | None:
        return self._conversations.get(conversation_id.value)

    async def find_latest_by_channel_user(
        self, source: str, channel_user_id: str
    ) -> Conversation | None:
        candidates = [
            c
            for c in self._conversations.values()
            if isinstance(c.meta, dict)
            and c.meta.get("source") == source
            and c.meta.get("channel_user_id") == channel_user_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.updated_at)

    async def delete(self, conversation_id: ConversationId) -> None:
        try:
            del self._conversations[conversation_id.value]
        except KeyError:
            raise ConversationNotFoundError(conversation_id.value) from None

    async def find_ids_by_selected_mode(
        self, mode_id: str
    ) -> tuple[ConversationId, ...]:
        return tuple(
            c.id
            for c in self._conversations.values()
            if isinstance(c.meta, dict)
            and (c.meta.get("discussion") or {}).get("selected_mode_id") == mode_id
        )

    async def clear_request_ids(self) -> int:
        updated = 0
        for conv in self._conversations.values():
            for message in conv.messages:
                meta = getattr(message, "meta", None)
                if isinstance(meta, dict) and meta.pop("request_id", None) is not None:
                    updated += 1
        return updated

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        favorite_only: bool = False,
        pinned_only: bool = False,
    ) -> tuple[ConversationListItem, ...]:
        items = sorted(
            self._conversations.values(), key=lambda c: c.updated_at, reverse=True
        )
        if favorite_only:
            items = [c for c in items if isinstance(c.meta, dict) and c.meta.get("favorite")]
        if pinned_only:
            items = [c for c in items if isinstance(c.meta, dict) and c.meta.get("pinned")]
        page = items[offset : offset + limit]
        return tuple(
            ConversationListItem(conversation=c, message_count=len(c.messages))
            for c in page
        )

    async def search(
        self, *, query: str, limit: int = 50
    ) -> tuple[ConversationListItem, ...]:
        needle = query.lower()
        matches = [
            c
            for c in self._conversations.values()
            if needle in c.title.lower()
            or any(needle in (getattr(m.content, "text", "") or "").lower() for m in c.messages)
        ]
        matches.sort(key=lambda c: c.updated_at, reverse=True)
        return tuple(
            ConversationListItem(conversation=c, message_count=len(c.messages))
            for c in matches[:limit]
        )

    async def fetch_messages_page(
        self,
        *,
        conversation_id: ConversationId,
        cursor: str | None = None,
        limit: int = 50,
    ) -> MessagesPage:
        conv = await self.get(conversation_id)
        start = int(cursor) if cursor else 0
        page = conv.messages[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(conv.messages) else None
        return MessagesPage(items=tuple(page), next_cursor=next_cursor)
