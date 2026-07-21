# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Title generation use case (PR-402 / S7.5 lane L4).

Migrates :func:`backend.title_generator.generate_title_for_conversation`
into the chat bounded context as a use case + a fallback helper.

Two-tier strategy (legacy parity):

* **Primary** — :class:`TitleGeneratorPort` (LLM round-trip).  When it
  returns a non-empty cleaned title, that title wins.
* **Fallback** — :func:`fallback_title` (pure-function heuristic).
  Triggered when the primary returns ``None`` (timeout / network /
  empty response / too-short result), so a conversation always ends up
  with *some* title even when the LLM is unreachable.

The legacy implementation also persisted the new title to the
conversation row.  Here we do the same via
:class:`ConversationRepositoryPort.save` after the title is computed,
so the use case is one self-contained call from the route layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.chat.application.ports import (
    ConversationRepositoryPort,
    TitleGenerationRequest,
    TitleGeneratorPort,
)
from qai.chat.domain.events import ConversationRenamedEvent
from qai.chat.domain.ids import ConversationId
from qai.platform.events import EventBus
from qai.platform.logging import get_logger
from qai.platform.time import Clock

_log = get_logger(__name__)


# Maximum characters in any title (LLM-generated or fallback) — hard
# cap matches legacy ``backend/title_generator.py:generate_title`` line
# 95 + Conversation domain CHECK constraint (length(title) <= 256).
TITLE_MAX_CHARS: int = 50

# Default placeholder for empty messages — matches legacy fallback.
DEFAULT_FALLBACK_TITLE: str = "New Chat"


def fallback_title(user_message: str) -> str:
    """Produce a degraded title from the first user message.

    Migrated 1:1 from ``backend/title_generator.py:fallback_title``.
    Behaviour:

    * empty / whitespace-only input → :data:`DEFAULT_FALLBACK_TITLE`;
    * collapse ``\\n`` and ``\\r`` to spaces;
    * trim outer whitespace;
    * truncate to :data:`TITLE_MAX_CHARS` characters.

    This helper is pure (no I/O, no LLM call) and is used by
    :class:`GenerateTitleUseCase` as a guaranteed-success fallback as
    well as by callers that want a synchronous title without paying
    the LLM round-trip cost.
    """
    if not user_message or not user_message.strip():
        return DEFAULT_FALLBACK_TITLE
    title = user_message.replace("\n", " ").replace("\r", " ").strip()
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS]
    return title or DEFAULT_FALLBACK_TITLE


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerateTitleInput:
    """Inputs to :meth:`GenerateTitleUseCase.execute`."""

    conversation_id: ConversationId
    user_message: str
    timeout_seconds: float = 10.0
    model_id: str | None = None
    """The cloud model the conversation is currently using (recorded on the
    latest assistant message). Forwarded to the :class:`TitleGeneratorPort`
    so a provider-aware adapter routes the title request to the SAME cloud
    provider the user is chatting with (V1 parity: title only uses cloud
    models). ``None`` → adapter uses its configured default."""
    persist: bool = True
    """When ``True`` (default), the resolved title is written back to
    the conversation row; when ``False``, the use case only returns
    the computed title without touching the conversation aggregate."""


@dataclass(frozen=True, slots=True, kw_only=True)
class GenerateTitleResult:
    """Outcome of :meth:`GenerateTitleUseCase.execute`."""

    title: str
    used_fallback: bool
    """``True`` iff :func:`fallback_title` produced the final title
    (LLM path returned ``None``).  Useful for telemetry and to give
    the front-end a hint that the title may be regenerated later."""


class GenerateTitleUseCase:
    """Generate (and optionally persist) a conversation title.

    Wiring:

    * ``conversations`` — used to load + save the conversation when
      ``persist=True``;
    * ``title_generator`` — primary LLM strategy
      (:class:`TitleGeneratorPort`);
    * ``clock`` — drives the conversation's ``updated_at`` timestamp
      when persisting;
    * ``events`` — optional :class:`EventBus`; emits
      :class:`ConversationRenamedEvent` after persistence so other
      contexts (channels notification, search index refresh, ...)
      can react.
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        title_generator: TitleGeneratorPort,
        clock: Clock,
        events: EventBus | None = None,
    ) -> None:
        self._conversations = conversations
        self._title_generator = title_generator
        self._clock = clock
        self._events = events

    async def execute(self, input: GenerateTitleInput) -> GenerateTitleResult:
        # Try LLM first; on any failure (port returns None) fall back.
        try:
            llm_title = await self._title_generator.generate(
                TitleGenerationRequest(
                    user_message=input.user_message,
                    timeout_seconds=input.timeout_seconds,
                    model_id=input.model_id,
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Port contract says "never raise"; if an adapter does, we
            # log and degrade gracefully rather than blow up the chat
            # turn.
            _log.warning(
                "chat.title_generator_raised",
                error=str(exc),
                conversation_id=input.conversation_id.value,
            )
            llm_title = None

        used_fallback = llm_title is None or not llm_title.strip()
        if used_fallback:
            title = fallback_title(input.user_message)
        else:
            assert llm_title is not None  # mypy
            cleaned = llm_title.strip()
            if len(cleaned) > TITLE_MAX_CHARS:
                cleaned = cleaned[:TITLE_MAX_CHARS]
            title = cleaned or fallback_title(input.user_message)

        if input.persist:
            conv = await self._conversations.get(input.conversation_id)
            # Manual-rename lock (re-checked here, AFTER the LLM round-trip):
            # the ``get`` above re-loads fresh conversation state, so a manual
            # rename that landed WHILE we were awaiting the LLM (the ~10 s
            # window) is now visible. A user-chosen title is authoritative and
            # must never be overwritten by an auto-summary (requirement: once
            # the user renames, the model never touches the title again). The
            # ``_chat_title_push`` bridge also checks this flag before starting,
            # but that check can't see a rename that happens after it; this
            # re-check closes the race. Stored in ``meta`` (apps/api
            # ``_chat_title_push.TITLE_MANUAL_META_KEY``).
            meta = conv.meta if isinstance(conv.meta, dict) else {}
            if meta.get("title_manual"):
                _log.info(
                    "chat.title_generated.skip_manual",
                    conversation_id=input.conversation_id.value,
                )
                return GenerateTitleResult(
                    title=conv.title, used_fallback=used_fallback
                )
            old_title = conv.title
            if old_title != title:
                conv.rename(title, now=self._clock.now())
                await self._conversations.save(conv)
                if self._events is not None:
                    await self._events.publish(
                        ConversationRenamedEvent(
                            conversation_id=conv.id,
                            old_title=old_title,
                            new_title=title,
                            renamed_at=self._clock.now(),
                        ),
                    )
        _log.info(
            "chat.title_generated",
            conversation_id=input.conversation_id.value,
            used_fallback=used_fallback,
            title_length=len(title),
        )
        return GenerateTitleResult(title=title, used_fallback=used_fallback)


__all__ = [
    "GenerateTitleUseCase",
    "GenerateTitleInput",
    "GenerateTitleResult",
    "fallback_title",
    "TITLE_MAX_CHARS",
    "DEFAULT_FALLBACK_TITLE",
]
