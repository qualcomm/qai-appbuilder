# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for chat conversation management (CRUD + search + pagination).

This module groups the synchronous-ish use cases that operate purely on
the conversation aggregate:

* :class:`CreateConversationUseCase`
* :class:`ListConversationsUseCase` (search + filter)
* :class:`GetConversationMessagesUseCase` (pagination)
* :class:`RenameConversationUseCase`
* :class:`DeleteConversationUseCase`

All use cases follow the same shape: dependencies are injected through
``__init__``; ``execute(...)`` is the single public method; logging and
event publishing are explicit and traceable.

Design notes:

* Use cases NEVER call ``datetime.now()`` directly -- they take a
  :class:`~qai.platform.time.Clock` so tests can use ``FrozenClock``.
* Identifier minting goes through :class:`~qai.platform.ids.IdGenerator`.
* Domain events are published asynchronously via
  :class:`~qai.platform.events.EventBus`; if no bus is wired (e.g. in
  pure unit tests) the use case still works -- the bus is optional.
* Adapter errors propagate as-is; chat-specific translation happens at
  the repository boundary (see ports).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qai.chat.application.ports import (
    ConversationListItem,
    ConversationRepositoryPort,
    MessagesPage,
)
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.events import (
    ConversationCreatedEvent,
    ConversationDeletedEvent,
    ConversationRenamedEvent,
)
from qai.chat.domain.ids import ConversationId
from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock

if TYPE_CHECKING:  # pragma: no cover
    pass

_log = get_logger(__name__)


async def _publish_best_effort(
    events: EventBus | None,
    event: object,
    *,
    op: str,
) -> None:
    """Publish a domain event without letting delivery failures abort the
    business operation.

    V1 parity (backend/main.py:5803-5811 + history_store.py:324-333):
    conversation CRUD in V1 is a *pure DB operation* that returns success as
    soon as the row is written/removed — it does not depend on any event-bus
    delivery. V2 added domain-event publishing as a side-effect, but a slow /
    stale subscriber (e.g. a leaked ``/api/events`` SSE subscription whose
    256-deep queue is full) makes ``EventBus.publish`` raise
    ``BackpressureError`` after ``publish_timeout_s``. Awaiting that publish
    inside the use case (with no guard) let the backpressure error bubble up
    and turned an already-succeeded DB write/delete into an HTTP 503 — the
    user saw "删除失败 … queue full for >1.0s" / "Cannot stream over SSE
    without a conversation_id" even though the DB op had committed.

    Event notification is a best-effort side-effect (same contract as
    ``apps/api/_channel_webui_broadcast.py:_publish``). A failed publish must
    only be logged, never propagated — so the user-visible CRUD result mirrors
    V1 (the DB op already succeeded). This guards against ANY publish failure
    (backpressure, closed bus, subscriber errors), not just backpressure.
    """
    if events is None:
        return
    try:
        await events.publish(event)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — notification must never abort the op
        _log.warning(
            "chat.conversation_event_publish_failed",
            op=op,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CreateConversationInput:
    title: str
    # Optional appended field (AGENTS.md §3.1 — tail-only growth): channel
    # source metadata seeded onto the new conversation's ``meta`` dict so a
    # service restart can restore the same conversation for the same channel
    # user (V1 parity: ``history_store.upsert_conversation(meta=...)`` ->
    # ``get_latest_wechat_conversation``).  ``None`` (web-UI default) keeps
    # ``Conversation.meta = None`` unchanged.
    meta: dict | None = None


class CreateConversationUseCase:
    """Create a brand-new, empty conversation."""

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
        events: EventBus | None = None,
    ) -> None:
        self._conversations = conversations
        self._clock = clock
        self._ids = ids
        self._events = events

    async def execute(self, request: CreateConversationInput) -> Conversation:
        now = self._clock.now()
        conv = Conversation.create(
            conversation_id=ConversationId.generate(self._ids),
            title=request.title,
            now=now,
            meta=request.meta,
        )
        await self._conversations.save(conv)
        if self._events is not None:
            await _publish_best_effort(
                self._events,
                ConversationCreatedEvent(
                    conversation_id=conv.id,
                    title=conv.title,
                    created_at=conv.created_at,
                ),
                op="create",
            )
        _log.info(
            "chat.conversation_created",
            conversation_id=conv.id.value,
            title=conv.title,
        )
        return conv


# ---------------------------------------------------------------------------
# List / search
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ListConversationsInput:
    query: str | None = None
    limit: int = 50
    offset: int = 0
    favorite_only: bool = False
    pinned_only: bool = False


class ListConversationsUseCase:
    """List conversations, optionally filtered by a free-text query."""

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
    ) -> None:
        self._conversations = conversations

    async def execute(
        self,
        request: ListConversationsInput,
    ) -> tuple[ConversationListItem, ...]:
        if request.query:
            return await self._conversations.search(
                query=request.query,
                limit=request.limit,
            )
        return await self._conversations.list(
            limit=request.limit,
            offset=request.offset,
            favorite_only=request.favorite_only,
            pinned_only=request.pinned_only,
        )


# ---------------------------------------------------------------------------
# Paged messages
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class GetConversationMessagesInput:
    conversation_id: ConversationId
    cursor: str | None = None
    limit: int = 50


class GetConversationMessagesUseCase:
    """Return a single page of messages for a conversation."""

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
    ) -> None:
        self._conversations = conversations

    async def execute(
        self,
        request: GetConversationMessagesInput,
    ) -> MessagesPage:
        # The repository raises ConversationNotFoundError if missing -- we
        # simply trust the boundary contract here.
        return await self._conversations.fetch_messages_page(
            conversation_id=request.conversation_id,
            cursor=request.cursor,
            limit=request.limit,
        )


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class RenameConversationInput:
    conversation_id: ConversationId
    new_title: str


class RenameConversationUseCase:
    """Update the title of an existing conversation."""

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        clock: Clock,
        events: EventBus | None = None,
    ) -> None:
        self._conversations = conversations
        self._clock = clock
        self._events = events

    async def execute(self, request: RenameConversationInput) -> Conversation:
        conv = await self._conversations.get(request.conversation_id)
        old_title = conv.title
        conv.rename(request.new_title, now=self._clock.now())
        # A user-initiated rename locks the title: stamp ``title_manual`` so
        # first-round auto-title generation never overwrites the user's choice
        # (apps/api/_chat_title_push reads this flag).
        conv.mark_title_manual(now=self._clock.now())
        await self._conversations.save(conv)
        if self._events is not None:
            await _publish_best_effort(
                self._events,
                ConversationRenamedEvent(
                    conversation_id=conv.id,
                    old_title=old_title,
                    new_title=conv.title,
                    renamed_at=conv.updated_at,
                ),
                op="rename",
            )
        _log.info(
            "chat.conversation_renamed",
            conversation_id=conv.id.value,
            old_title=old_title,
            new_title=conv.title,
        )
        return conv


# ---------------------------------------------------------------------------
# Set per-session workspace
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class SetConversationWorkspaceInput:
    conversation_id: ConversationId
    workspace: str | None


class SetConversationWorkspaceUseCase:
    """Set (or clear) a conversation's per-session workspace directory.

    Persisted in ``conversation.meta["workspace"]``. A blank / ``None``
    value clears it so the session falls back to the global configured
    workspace. Used by the agentic loop to default the file/exec tools'
    working root to this directory for the session.
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        clock: Clock,
    ) -> None:
        self._conversations = conversations
        self._clock = clock

    async def execute(
        self, request: SetConversationWorkspaceInput
    ) -> Conversation:
        conv = await self._conversations.get(request.conversation_id)
        conv.set_workspace(request.workspace, now=self._clock.now())
        await self._conversations.save(conv)
        _log.info(
            "chat.conversation_workspace_set",
            conversation_id=conv.id.value,
            workspace=(request.workspace or "").strip() or None,
        )
        return conv


# ---------------------------------------------------------------------------
# Pin / unpin
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class SetConversationPinnedInput:
    conversation_id: ConversationId
    pinned: bool


class SetConversationPinnedUseCase:
    """Pin (or unpin) a conversation to the top of the sidebar list.

    Persisted in ``conversation.meta["pinned"]`` (no new column / migration;
    same range as :class:`SetConversationWorkspaceUseCase`). The UI surfaces
    pinned conversations above the time-bucketed history.
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        clock: Clock,
    ) -> None:
        self._conversations = conversations
        self._clock = clock

    async def execute(
        self, request: SetConversationPinnedInput
    ) -> Conversation:
        conv = await self._conversations.get(request.conversation_id)
        conv.set_pinned(request.pinned, now=self._clock.now())
        await self._conversations.save(conv)
        _log.info(
            "chat.conversation_pinned_set",
            conversation_id=conv.id.value,
            pinned=request.pinned,
        )
        return conv


# ---------------------------------------------------------------------------
# Favorite / unfavorite
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class SetConversationFavoriteInput:
    conversation_id: ConversationId
    favorite: bool


class SetConversationFavoriteUseCase:
    """Favorite (or unfavorite) a conversation for the favorites library.

    Persisted in ``conversation.meta["favorite"]`` (no new column /
    migration; same range as :class:`SetConversationWorkspaceUseCase`).
    Favorited conversations appear in the favorites dialog for quick recall.
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        clock: Clock,
    ) -> None:
        self._conversations = conversations
        self._clock = clock

    async def execute(
        self, request: SetConversationFavoriteInput
    ) -> Conversation:
        conv = await self._conversations.get(request.conversation_id)
        conv.set_favorite(request.favorite, now=self._clock.now())
        await self._conversations.save(conv)
        _log.info(
            "chat.conversation_favorite_set",
            conversation_id=conv.id.value,
            favorite=request.favorite,
        )
        return conv


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteConversationInput:
    conversation_id: ConversationId


class DeleteConversationUseCase:
    """Remove a conversation and emit a deletion event."""

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        clock: Clock,
        events: EventBus | None = None,
    ) -> None:
        self._conversations = conversations
        self._clock = clock
        self._events = events

    async def execute(self, request: DeleteConversationInput) -> None:
        await self._conversations.delete(request.conversation_id)
        if self._events is not None:
            await _publish_best_effort(
                self._events,
                ConversationDeletedEvent(
                    conversation_id=request.conversation_id,
                    deleted_at=self._clock.now(),
                ),
                op="delete",
            )
        _log.info(
            "chat.conversation_deleted",
            conversation_id=request.conversation_id.value,
        )


__all__ = [
    "CreateConversationUseCase",
    "CreateConversationInput",
    "ListConversationsUseCase",
    "ListConversationsInput",
    "GetConversationMessagesUseCase",
    "GetConversationMessagesInput",
    "RenameConversationUseCase",
    "RenameConversationInput",
    "SetConversationWorkspaceUseCase",
    "SetConversationWorkspaceInput",
    "SetConversationPinnedUseCase",
    "SetConversationPinnedInput",
    "SetConversationFavoriteUseCase",
    "SetConversationFavoriteInput",
    "DeleteConversationUseCase",
    "DeleteConversationInput",
]
