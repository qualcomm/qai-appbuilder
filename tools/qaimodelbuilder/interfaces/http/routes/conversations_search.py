# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Conversation search HTTP route.

Route:
- ``GET /api/conversations/search`` — search conversations by keyword

This restores the legacy ``/api/conversations/search`` endpoint that the
WebUI search panel uses. Delegates to the ``ListConversationsInput`` use
case, which routes a non-empty ``query`` through the repository's
full-text ``search`` path (FTS5 message-body match + ``<mark>``-highlighted
snippet, V1 parity with ``backend/history_store.py:688-714``). The
``snippet`` field carries the highlighted excerpt the front-end renders via
``v-html``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Query
from pydantic import BaseModel

from qai.chat.application.use_cases.conversation_management import (
    ListConversationsInput,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---------------------------------------------------------------------------
# Response DTOs
# ---------------------------------------------------------------------------


class SearchResultItem(BaseModel):
    id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    message_count: int
    snippet: str = ""


class ConversationSearchResponse(BaseModel):
    results: list[SearchResultItem]
    total: int
    query: str


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the conversations search router."""
    router = APIRouter(tags=["conversations"])

    @router.get(
        "/api/conversations/search",
        response_model=ConversationSearchResponse,
    )
    async def search_conversations(
        q: str = Query(..., min_length=1, max_length=256),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> ConversationSearchResponse:
        """Search conversations by keyword across titles and messages."""
        items = await container.chat.list_conversations_use_case.execute(
            ListConversationsInput(query=q, limit=limit, offset=offset),
        )
        results = [
            SearchResultItem(
                id=item.conversation.id.value,
                title=item.conversation.title,
                status=item.conversation.status.value,
                created_at=item.conversation.created_at.isoformat(),
                updated_at=item.conversation.updated_at.isoformat(),
                message_count=item.message_count,
                snippet=item.snippet,
            )
            for item in items
        ]
        return ConversationSearchResponse(
            results=results,
            total=len(results),
            query=q,
        )

    return router
