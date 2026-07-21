# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""First-round auto title generation bridge (V1 parity: main.py:6817-6851).

When a WebUI chat stream completes successfully **on the first round** of a
conversation, V1 fire-and-forgets a model-summarised title for that
conversation (``backend/main.py:6818`` — gated on ``_user_count <= 1``).  V2
had wired :class:`GenerateTitleUseCase` into the chat DI but never triggered
it from the streaming path (only the CLI ``conv`` command called it), so the
conversation kept the truncated "first 80 chars of the first message" title
created at conversation-creation time and the model never summarised anything.

This bridge restores that behaviour for both the SSE and WS streaming routes:
after the stream ends with a successful ``END`` frame, the route calls
:func:`schedule_first_round_title` which — in fire-and-forget mode — checks
whether this is the conversation's first round and whether the user has
manually renamed it, then runs :class:`GenerateTitleUseCase` to persist a
model-summarised title.

Manual-rename lock
------------------
Once the user manually renames a conversation (PATCH rename route /
``/rename`` command), the rename route stamps ``meta["title_manual"] = True``
on the conversation.  This bridge skips title generation for any conversation
that carries that flag, so an auto-summary never clobbers a title the user
chose themselves (V1 sidestepped this implicitly by only generating on the
first round; V2 makes it explicit so a manual rename *on* the first round is
also protected).

This is intentionally fire-and-forget: title generation must never delay or
break the SSE/WS response to the WebUI client (V1 ``asyncio.create_task``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from qai.chat.application.use_cases.title import (
    GenerateTitleInput,
)
from qai.chat.domain.content import MessageRole
from qai.chat.domain.ids import ConversationId
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

logger = get_logger(__name__)


__all__ = [
    "schedule_first_round_title",
    "TITLE_MANUAL_META_KEY",
]


# Conversation.meta key recording that the user manually set the title.
# When present and truthy, auto title generation is skipped (the user's
# chosen title is authoritative).  Stored in the already-persisted
# ``Conversation.meta`` dict so no DB schema / wire-contract change is needed
# (AGENTS.md §3.1 — meta is an additive namespace).
TITLE_MANUAL_META_KEY: str = "title_manual"


def schedule_first_round_title(
    *,
    container: "Container",
    conversation_id: str,
    user_text: str,
    model_id: str | None = None,
) -> None:
    """Fire-and-forget auto-summary of the conversation title (first round).

    Called by the SSE/WS route as EARLY as possible — on the first stream
    frame, NOT after the (possibly long, multi-tool) turn ends — so the tab /
    sidebar title updates right after the user sends, not minutes later.  The
    title only depends on the first user message, which the use case persists
    before opening the LLM, so by the first frame the first-round gate sees
    exactly one user message.

    ``model_id`` is the model the user is chatting with THIS turn (the route's
    send param / ``model_hint``).  It's known up-front at send time — we don't
    wait for an assistant message to record it.  The title generator uses it to
    route to that model's cloud provider; a local / absent model falls back to
    the truncation heuristic (user 2026-06-17: local models never use a model
    to generate the title).

    No-ops synchronously when ``user_text`` is empty; all real work (loading
    the conversation, checking the first-round / manual guards, running the use
    case) happens inside the spawned task so the streaming response is never
    blocked.
    """
    if not user_text or not user_text.strip():
        return
    asyncio.create_task(
        _do_generate_title(
            container=container,
            conversation_id=conversation_id,
            user_text=user_text,
            model_id=model_id,
        )
    )


async def _do_generate_title(
    *,
    container: "Container",
    conversation_id: str,
    user_text: str,
    model_id: str | None = None,
) -> None:
    """Load the conversation, apply guards, and persist a model title.

    Guards (both must pass):

    * **First round only** — V1 ``_user_count <= 1`` (main.py:6819-6820).
      We count persisted ``user`` messages on the conversation; ``> 1`` means
      the user has already had a prior turn, so the title was (or should have
      been) generated earlier — skip.
    * **Not manually renamed** — skip when ``meta[TITLE_MANUAL_META_KEY]`` is
      truthy, so an auto-summary never overwrites a user-chosen title.

    ``model_id`` (the model the user is chatting with this turn) is forwarded to
    the title generator so the request routes to that model's cloud provider; a
    local / absent model falls back to the truncation heuristic.

    Errors are swallowed (logged at warning); a failed title summary must
    never surface to the user (V1 ``except ... logger.debug``).
    """
    try:
        conv = await container.chat.conversations.get(
            ConversationId.of(conversation_id),
        )

        # Manual-rename lock: user's chosen title wins.
        meta = conv.meta if isinstance(conv.meta, dict) else {}
        if meta.get(TITLE_MANUAL_META_KEY):
            logger.info(
                "chat.auto_title.skip_manual",
                conversation_id=conversation_id,
            )
            return

        # First-round gate (V1 main.py:6819 — ``_user_count <= 1``).
        user_messages = conv.messages_by_role(MessageRole.USER)
        logger.info(
            "chat.auto_title.enter",
            conversation_id=conversation_id,
            user_message_count=len(user_messages),
            model_id=model_id,
        )
        if len(user_messages) > 1:
            logger.info(
                "chat.auto_title.skip_not_first_round",
                conversation_id=conversation_id,
                user_message_count=len(user_messages),
            )
            return

        result = await container.chat.generate_title_use_case.execute(
            GenerateTitleInput(
                conversation_id=conv.id,
                user_message=user_text,
                model_id=model_id,
                persist=True,
            ),
        )
        logger.info(
            "chat.auto_title.generated",
            conversation_id=conversation_id,
            title=getattr(result, "title", None),
            used_fallback=getattr(result, "used_fallback", None),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort, never break a turn
        logger.warning(
            "chat.auto_title.failed",
            conversation_id=conversation_id,
            error=str(exc),
        )
