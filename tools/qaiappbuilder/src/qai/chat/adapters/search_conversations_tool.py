# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Handler + schema for the ``search_conversations`` tool.

Lets an agent search its PAST conversations (message bodies) to recall
how a similar problem was handled earlier. The current conversation is
already in the model's context window, so ``discover`` / ``browse``
default to ``scope='others'`` (everything EXCEPT the current
conversation); pass ``scope='all'`` to include the current one.

Modes (inferred from ``mode`` argument, default ``discover``):

* ``discover`` — FTS keyword search; args ``query`` (required),
  ``limit`` (default 10), ``scope`` (``others`` | ``all``).
* ``scroll``   — window around a message; args ``anchor_message_id``
  (required), ``window`` (default 10).
* ``read``     — a conversation's head + tail; args ``conversation_id``
  (required), ``head`` (default 20), ``tail`` (default 10).
* ``browse``   — recent conversations (metadata only); args ``limit``
  (default 20), ``scope`` (``others`` | ``all``).

The handler returns a human-readable text block (the LLM consumes it as
the tool result). Best-effort: any repository error degrades to a short
diagnostic string rather than raising, so the turn never breaks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.adapters.conversation_search_repository import (
        SqliteConversationSearchRepository,
    )
    from qai.chat.application.ports import ToolInvocationRequest


__all__ = ["SEARCH_CONVERSATIONS_TOOL_SCHEMA", "SearchConversationsToolHandler"]


_log = get_logger(__name__)

_VALID_SCOPES = ("others", "all")
_DEFAULT_DISCOVER_LIMIT = 10
_DEFAULT_BROWSE_LIMIT = 20
_DEFAULT_WINDOW = 10
_DEFAULT_HEAD = 20
_DEFAULT_TAIL = 10
_MAX_CONTENT_PREVIEW = 500


SEARCH_CONVERSATIONS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_conversations",
        "description": (
            "Search your PAST conversations to recall how a similar task or "
            "problem was handled before. Use this when the user references "
            "earlier work, when you suspect you solved something like this "
            "in another conversation, or to look up a prior decision. The "
            "CURRENT conversation is already in your context, so by default "
            "this searches OTHER conversations. Four modes: 'discover' "
            "(keyword search, default), 'scroll' (context around a found "
            "message), 'read' (a whole past conversation), 'browse' (list "
            "recent conversations)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["discover", "scroll", "read", "browse"],
                    "description": (
                        "Search mode. Defaults to 'discover' (keyword "
                        "search) when omitted."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "discover mode: keywords to search for in past "
                        "message bodies."
                    ),
                },
                "scope": {
                    "type": "string",
                    "enum": ["others", "all"],
                    "description": (
                        "discover/browse mode: 'others' (default) excludes "
                        "the current conversation; 'all' includes it."
                    ),
                },
                "anchor_message_id": {
                    "type": "string",
                    "description": (
                        "scroll mode: the message id to centre the window "
                        "on (from a prior discover result)."
                    ),
                },
                "conversation_id": {
                    "type": "string",
                    "description": (
                        "read mode: the conversation id to read (from a "
                        "prior discover/browse result)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "discover/browse mode: max results (discover "
                        "default 10, browse default 20)."
                    ),
                },
                "window": {
                    "type": "integer",
                    "description": (
                        "scroll mode: messages before and after the anchor "
                        "(default 10)."
                    ),
                },
                "head": {
                    "type": "integer",
                    "description": (
                        "read mode: first N messages (default 20)."
                    ),
                },
                "tail": {
                    "type": "integer",
                    "description": (
                        "read mode: last N messages (default 10)."
                    ),
                },
            },
            "required": [],
        },
    },
}


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort int coercion; falls back to ``default`` on junk input."""
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _coerce_scope(value: Any) -> str:
    """Normalise the scope arg; unknown values fall back to 'others'."""
    return value if value in _VALID_SCOPES else "others"


def _clip(text: str, limit: int = _MAX_CONTENT_PREVIEW) -> str:
    """Clip a message body to a bounded preview length."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class SearchConversationsToolHandler:
    """Handler for the ``search_conversations`` tool.

    Best-effort: when no repository is wired (legacy/test stubs) it
    reports gracefully rather than erroring.
    """

    __slots__ = ("_repository",)

    def __init__(
        self,
        *,
        repository: SqliteConversationSearchRepository | None = None,
    ) -> None:
        self._repository = repository

    async def execute(self, request: ToolInvocationRequest) -> str:
        repo = self._repository
        if repo is None:
            return (
                "[search_conversations] conversation search is not available "
                "in this environment."
            )
        args = request.arguments if isinstance(request.arguments, dict) else {}
        mode = args.get("mode") or "discover"
        current_id = request.conversation_id.value
        try:
            if mode == "discover":
                result = await self._discover(repo, args, current_id)
            elif mode == "scroll":
                result = await self._scroll(repo, args)
            elif mode == "read":
                result = await self._read(repo, args)
            elif mode == "browse":
                result = await self._browse(repo, args, current_id)
            else:
                result = (
                    f"[search_conversations] unknown mode {mode!r}. Valid "
                    "modes: discover, scroll, read, browse."
                )
        except Exception as exc:  # noqa: BLE001 — never break the turn
            _log.warning(
                "chat.search_conversations.failed",
                mode=str(mode),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return (
                f"[search_conversations error] the {mode} query failed; try "
                "a different query or mode."
            )
        return result

    async def _discover(
        self,
        repo: SqliteConversationSearchRepository,
        args: dict[str, Any],
        current_id: str,
    ) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return (
                "[search_conversations] discover mode needs a 'query' "
                "string."
            )
        scope = _coerce_scope(args.get("scope"))
        limit = _coerce_int(args.get("limit"), _DEFAULT_DISCOVER_LIMIT)
        hits = await repo.discover(
            query=query,
            current_conversation_id=current_id,
            scope=scope,
            limit=limit,
        )
        if not hits:
            return (
                f"No past conversations matched {query!r} "
                f"(scope={scope}). Nothing to recall for this query."
            )
        lines = [
            f"{len(hits)} match(es) for {query!r} in past conversations "
            "(most relevant first). Use mode='scroll' with a message_id for "
            "surrounding context, or mode='read' with a conversation_id for "
            "the full conversation:",
        ]
        for ordinal, hit in enumerate(hits, start=1):
            title = hit.conversation_title or "(untitled)"
            lines.append(
                f"{ordinal}. [{hit.role}] {hit.snippet or '(no excerpt)'}\n"
                f"   conversation_id: {hit.conversation_id} "
                f"| message_id: {hit.message_id} | title: {title} "
                f"| when: {hit.created_at}"
            )
        return "\n".join(lines)

    async def _scroll(
        self,
        repo: SqliteConversationSearchRepository,
        args: dict[str, Any],
    ) -> str:
        anchor = args.get("anchor_message_id")
        if not isinstance(anchor, str) or not anchor.strip():
            return (
                "[search_conversations] scroll mode needs an "
                "'anchor_message_id' string."
            )
        window = _coerce_int(args.get("window"), _DEFAULT_WINDOW)
        rows = await repo.scroll(anchor_message_id=anchor, window=window)
        if not rows:
            return (
                f"[search_conversations] message id {anchor!r} was not "
                "found; it may have been deleted."
            )
        lines = [
            f"{len(rows)} message(s) around message_id {anchor!r} "
            "(chronological order):",
        ]
        for row in rows:
            marker = " <-- anchor" if row.message_id == anchor else ""
            lines.append(
                f"[{row.role} @ pos {row.position}]{marker}: "
                f"{_clip(row.content_text)}"
            )
        return "\n".join(lines)

    async def _read(
        self,
        repo: SqliteConversationSearchRepository,
        args: dict[str, Any],
    ) -> str:
        conv_id = args.get("conversation_id")
        if not isinstance(conv_id, str) or not conv_id.strip():
            return (
                "[search_conversations] read mode needs a "
                "'conversation_id' string."
            )
        head = _coerce_int(args.get("head"), _DEFAULT_HEAD)
        tail = _coerce_int(args.get("tail"), _DEFAULT_TAIL)
        rows = await repo.read(conversation_id=conv_id, head=head, tail=tail)
        if not rows:
            return (
                f"[search_conversations] conversation {conv_id!r} has no "
                "messages or was not found."
            )
        lines = [
            f"Conversation {conv_id!r} — {len(rows)} message(s) "
            "(head + tail, chronological order):",
        ]
        for row in rows:
            lines.append(
                f"[{row.role} @ pos {row.position}]: "
                f"{_clip(row.content_text)}"
            )
        return "\n".join(lines)

    async def _browse(
        self,
        repo: SqliteConversationSearchRepository,
        args: dict[str, Any],
        current_id: str,
    ) -> str:
        scope = _coerce_scope(args.get("scope"))
        limit = _coerce_int(args.get("limit"), _DEFAULT_BROWSE_LIMIT)
        items = await repo.browse(
            current_conversation_id=current_id,
            scope=scope,
            limit=limit,
        )
        if not items:
            return (
                f"No other conversations found (scope={scope})."
            )
        lines = [
            f"{len(items)} recent conversation(s) (most recent first). "
            "Use mode='read' with a conversation_id to open one, or "
            "mode='discover' to keyword-search:",
        ]
        for ordinal, item in enumerate(items, start=1):
            title = item.title or "(untitled)"
            lines.append(
                f"{ordinal}. {title} "
                f"| conversation_id: {item.conversation_id} "
                f"| status: {item.status} | messages: {item.message_count} "
                f"| updated: {item.updated_at}"
            )
        return "\n".join(lines)
