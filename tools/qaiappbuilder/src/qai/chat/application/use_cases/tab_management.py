# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for chat tab management (PR-043 / issue d).

Three small use cases that complete the multi-tab parallel chat surface
and let the route layer stop calling :meth:`ConversationTab.open`
directly (PR-033 manifest §11.1 coordination request):

* :class:`OpenTabUseCase`         -- mint a fresh tab on a conversation
* :class:`CloseTabUseCase`        -- transition a tab to ``closed``
* :class:`ListActiveTabsUseCase`  -- enumerate non-closed tabs

Design notes:

* Use cases follow the same shape as PR-021's conversation_management
  module (kw-only ``__init__``, ``execute`` is the single public
  method, dependencies injected explicitly).
* :class:`OpenTabUseCase` requires the conversation to exist; missing
  conversations propagate as :class:`ConversationNotFoundError`.
* :class:`CloseTabUseCase` is **not** idempotent at the missing-tab
  level: closing an unknown tab raises :class:`TabNotFoundError`.
  Closing an already-closed tab is allowed (matches
  :meth:`ConversationTab.close` semantics).
* No domain events are emitted: tab lifecycle is private UI state, no
  cross-context observers exist.
* Errors propagate from the repository / domain layer:
  :class:`TabStateError` from
  :meth:`ConversationTab.close` when the tab is mid-stream;
  :class:`TabNotFoundError` from the store; the route layer maps these
  through the unified error handler.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.chat.application.ports import (
    ConversationRepositoryPort,
    TabSessionStorePort,
)
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.tab import ConversationTab
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock


__all__ = [
    "OpenTabUseCase",
    "OpenTabInput",
    "CloseTabUseCase",
    "CloseTabInput",
    "ListActiveTabsUseCase",
]


_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Open
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class OpenTabInput:
    """Inputs to :class:`OpenTabUseCase`.

    ``tab_id=None`` mints a fresh id (the common case from front-end
    "open new tab"); a non-None id idempotently re-opens an existing
    tab record (e.g. after a page reload that already owns a TabId).
    """

    conversation_id: str
    tab_id: str | None = None


class OpenTabUseCase:
    """Open a tab on a conversation, returning the persisted aggregate.

    Validates the conversation exists (raising
    :class:`ConversationNotFoundError` otherwise), generates a fresh
    :class:`TabId` if one was not supplied, persists via
    :class:`TabSessionStorePort.save`, and returns the resulting
    :class:`ConversationTab` so the caller can echo the id back to the
    front-end.
    """

    def __init__(
        self,
        *,
        tabs: TabSessionStorePort,
        conversations: ConversationRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._tabs = tabs
        self._conversations = conversations
        self._clock = clock
        self._ids = ids

    async def execute(self, request: OpenTabInput) -> ConversationTab:
        conversation_id = ConversationId.of(request.conversation_id)
        # Raise ConversationNotFoundError if missing (matches
        # _resolve_or_create_tab semantics in _sse.py).
        await self._conversations.get(conversation_id)
        tab_id = (
            TabId.of(request.tab_id)
            if request.tab_id is not None
            else TabId.generate(self._ids)
        )
        # Idempotent re-open: if a tab with this id is already on file
        # for the same conversation, return it unchanged.
        existing = await self._tabs.find(tab_id)
        if existing is not None and existing.conversation_id == conversation_id:
            return existing
        tab = ConversationTab.open(
            tab_id=tab_id,
            conversation_id=conversation_id,
            now=self._clock.now(),
        )
        await self._tabs.save(tab)
        _log.info(
            "chat.tab_opened",
            tab_id=tab.id.value,
            conversation_id=tab.conversation_id.value,
        )
        return tab


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CloseTabInput:
    """Inputs to :class:`CloseTabUseCase`."""

    tab_id: str


class CloseTabUseCase:
    """Mark a tab as closed and persist the new state.

    Raises :class:`TabNotFoundError` if no such tab exists; raises
    :class:`TabStateError` if the tab is currently streaming (the
    caller must abort the stream first via :class:`StopChatUseCase`).
    """

    def __init__(
        self,
        *,
        tabs: TabSessionStorePort,
        clock: Clock,
    ) -> None:
        self._tabs = tabs
        self._clock = clock

    async def execute(self, request: CloseTabInput) -> ConversationTab:
        tab_id = TabId.of(request.tab_id)
        tab = await self._tabs.get(tab_id)
        tab.close(now=self._clock.now())
        await self._tabs.save(tab)
        _log.info("chat.tab_closed", tab_id=tab.id.value)
        return tab


# ---------------------------------------------------------------------------
# List active
# ---------------------------------------------------------------------------
class ListActiveTabsUseCase:
    """Return every non-closed tab, ordered by ``last_active_at`` DESC."""

    def __init__(self, *, tabs: TabSessionStorePort) -> None:
        self._tabs = tabs

    async def execute(self) -> tuple[ConversationTab, ...]:
        return await self._tabs.list_active()
