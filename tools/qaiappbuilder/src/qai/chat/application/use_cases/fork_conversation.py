# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: fork (branch) a conversation.

Fork creates a NEW conversation containing a SUBSET of messages from the
source conversation.  Supported slicing modes (mutually exclusive — at
most ONE may be non-None):

* ``keep_first_n`` — keep only the first N messages.
* ``keep_last_n`` — keep only the last N messages.
* ``keep_first_rounds`` — keep only the first N *rounds* (a round starts
  at a ``role:user`` message and includes every following assistant /
  tool / system message until the next ``role:user``).
* ``keep_last_rounds`` — keep only the last N rounds (same grouping).
* ``up_to_message_id`` — keep every message up to AND INCLUDING the
  message with the given id.

All five omitted → full clone.

Additional post-slice knobs:

* ``include_tool_calls`` (default ``False``) — when ``False`` the sliced
  history is filtered down to a pure user↔assistant Q&A: ``role:tool``
  messages are dropped, and ``role:assistant`` messages whose textual
  content is empty / whitespace-only (i.e. intermediate tool-invocation
  turns that only carried ``tool_calls``) are dropped too. When ``True``
  the sliced history is preserved verbatim.
* ``title`` — optional custom title for the forked conversation
  (truncated to 256 chars). If omitted the fork title falls back to
  ``f"{source.title} (fork)"``.
* ``inherit_settings`` (default ``True``) — when ``True`` the fork
  inherits ``workspace`` / ``discussion`` from the source's ``meta``
  (see the ``Conversation`` docstring for the recognised meta keys).
  ``pinned`` / ``favorite`` / ``budget`` / ``title_manual`` /
  channel-source keys are NEVER inherited (a fork gets a fresh
  sidebar / library / budget / channel identity).

The forked conversation always records lineage:

* ``meta["parent_id"]`` = source conversation id string (V1 parity).
* ``meta["forked_from"] = {"conversation_id", "round", "title"}`` where
  ``round`` is the number of *rounds* the sliced fork contains (i.e.
  where in the source's timeline the fork "branches off").

Persistence: the new conversation is saved via the existing
:meth:`ConversationRepositoryPort.save` (header upsert + full message
write), so no repository extension is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qai.chat.application.ports import ConversationRepositoryPort
from qai.chat.application.use_cases._agentic_kernel import (
    TOOL_CALLS_CONTENT_SENTINEL,
)
from qai.chat.domain.content import MessageRole
from qai.chat.domain.conversation import Conversation, ConversationStatus
from qai.chat.domain.ids import ConversationId, MessageId
from qai.chat.domain.message import Message
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock

if TYPE_CHECKING:  # pragma: no cover
    pass

_log = get_logger(__name__)

# ``meta`` keys we copy from source → fork when ``inherit_settings=True``.
# Only session-scoped configuration (workspace, discussion) travels; sidebar
# flags (pinned / favorite), budget, and channel provenance stay fresh on the
# fork. Kept as a module-level constant so the policy is discoverable and
# survives future ``Conversation.meta`` growth without silently over-copying.
_INHERITABLE_META_KEYS: tuple[str, ...] = ("workspace", "discussion")

_MAX_TITLE_LENGTH: int = 256


@dataclass(frozen=True, slots=True, kw_only=True)
class ForkConversationInput:
    """Input DTO for the fork-conversation use case.

    New fields are TAIL-APPENDED with defaults so existing call sites keep
    working unchanged (AGENTS.md §3.1 namespace lock).
    """

    source_id: ConversationId
    keep_first_n: int | None = None
    keep_last_n: int | None = None
    keep_first_rounds: int | None = None
    keep_last_rounds: int | None = None
    up_to_message_id: str | None = None
    include_tool_calls: bool = False
    title: str | None = None
    inherit_settings: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ForkConversationResult:
    """Output DTO carrying the newly-created forked conversation."""

    conversation: Conversation


def _group_into_rounds(messages: list[Message]) -> list[list[Message]]:
    """Group ``messages`` into rounds by walking start-at-``user`` boundaries.

    A round begins at each ``role:user`` message and includes every following
    ``assistant`` / ``tool`` / ``system`` message until the next ``role:user``
    (or the end of the list). Any leading non-user messages (e.g. a bare
    system prompt) form a leading pseudo-round so no message is lost.
    """
    rounds: list[list[Message]] = []
    current: list[Message] = []
    for msg in messages:
        if msg.role is MessageRole.USER:
            if current:
                rounds.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        rounds.append(current)
    return rounds


def _filter_tool_activity(messages: list[Message]) -> list[Message]:
    """Drop tool messages and intermediate tool-invocation assistant turns.

    Used when ``include_tool_calls=False`` to produce a clean user↔assistant
    Q&A history:

    * every ``role:tool`` message is dropped;
    * every ``role:assistant`` message that only carried ``tool_calls``
      (its ``content.text`` is the internal ``TOOL_CALLS_CONTENT_SENTINEL``
      placeholder — see ``_agentic_kernel`` and ``_streaming_helpers``) is
      dropped. ``MessageContent`` rejects blank text, so historical rows
      never store true whitespace-only content; the sentinel is the
      single, canonical marker for "assistant turn with no readable text".

    The final assistant response of each round (the one with actual model
    output text) is preserved.
    """
    kept: list[Message] = []
    for msg in messages:
        if msg.role is MessageRole.TOOL:
            continue
        if (
            msg.role is MessageRole.ASSISTANT
            and msg.content.text == TOOL_CALLS_CONTENT_SENTINEL
        ):
            continue
        kept.append(msg)
    return kept


class ForkConversationUseCase:
    """Fork (branch) an existing conversation into a new one."""

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._conversations = conversations
        self._clock = clock
        self._ids = ids

    async def execute(
        self, request: ForkConversationInput
    ) -> ForkConversationResult:
        # ── Validate slicer combinations ─────────────────────────────────
        # ``up_to_message_id`` is an ANCHOR (cut point), not a count slicer,
        # so it MAY combine with a trailing count/round slicer: the message
        # list is first cut to the anchor, then the trailing slicer runs on
        # that prefix (semantics: "keep the last N rounds UP TO this message").
        # The count slicers remain mutually exclusive with each other.
        count_slicers = {
            "keep_first_n": request.keep_first_n,
            "keep_last_n": request.keep_last_n,
            "keep_first_rounds": request.keep_first_rounds,
            "keep_last_rounds": request.keep_last_rounds,
        }
        active_counts = [
            name for name, value in count_slicers.items() if value is not None
        ]
        if len(active_counts) > 1:
            raise ValueError(
                "count slicers are mutually exclusive; at most one of "
                f"{sorted(count_slicers)} may be set, got {active_counts}",
            )
        # ``up_to_message_id`` is an ANCHOR: it cuts the history first, then
        # any count slicer runs on the resulting prefix.  ALL combinations are
        # valid — "first N rounds of the prefix up to anchor" and "last N
        # rounds of the prefix up to anchor" both have clear semantics.

        for name, value in count_slicers.items():
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative, got {value}")

        # ── Load source ──────────────────────────────────────────────────
        source = await self._conversations.get(request.source_id)

        # ── Slice messages ───────────────────────────────────────────────
        messages: list[Message] = list(source.messages)

        # Step 1: anchor cut (if any) — keep up to AND INCLUDING the anchor.
        if request.up_to_message_id is not None:
            cut = None
            for idx, msg in enumerate(messages):
                if msg.id.value == request.up_to_message_id:
                    cut = idx
                    break
            if cut is None:
                raise ValueError(
                    f"message id {request.up_to_message_id} not found in "
                    "source conversation",
                )
            messages = messages[: cut + 1]

        # Step 2: count/round slicer on the (possibly anchor-cut) prefix.
        if request.keep_first_n is not None:
            messages = messages[: request.keep_first_n]
        elif request.keep_last_n is not None:
            messages = (
                messages[-request.keep_last_n:]
                if request.keep_last_n > 0
                else []
            )
        elif request.keep_first_rounds is not None:
            rounds = _group_into_rounds(messages)
            kept_rounds = rounds[: request.keep_first_rounds]
            messages = [m for r in kept_rounds for m in r]
        elif request.keep_last_rounds is not None:
            rounds = _group_into_rounds(messages)
            kept_rounds = (
                rounds[-request.keep_last_rounds:]
                if request.keep_last_rounds > 0
                else []
            )
            messages = [m for r in kept_rounds for m in r]

        # ── Filter tool activity (post-slice) ────────────────────────────
        if not request.include_tool_calls:
            messages = _filter_tool_activity(messages)

        # ── Count rounds in the resulting slice ──────────────────────────
        fork_point_round = len(_group_into_rounds(messages))

        # ── Mint new ids for cloned messages ─────────────────────────────
        # We must assign fresh ids so the fork is fully independent.
        old_to_new: dict[MessageId, MessageId] = {}
        cloned_messages: list[Message] = []
        for msg in messages:
            new_msg_id = MessageId.generate(self._ids)
            old_to_new[msg.id] = new_msg_id
            # Remap parent_id to the new id (or None if parent not in slice)
            new_parent_id: MessageId | None = None
            if msg.parent_id is not None:
                new_parent_id = old_to_new.get(msg.parent_id)
            cloned_messages.append(
                Message(
                    id=new_msg_id,
                    role=msg.role,
                    content=msg.content,
                    created_at=msg.created_at,
                    parent_id=new_parent_id,
                    tool_calls=msg.tool_calls,
                    tool_results=msg.tool_results,
                    usage=msg.usage,
                    model_id=msg.model_id,
                    model_provider=msg.model_provider,
                    meta=msg.meta,
                    sender_id=msg.sender_id,
                )
            )

        # ── Resolve fork title ───────────────────────────────────────────
        custom_title = (request.title or "").strip()
        fork_title = (
            custom_title if custom_title else f"{source.title} (fork)"
        )
        # Truncate to 256 chars (domain max) if needed
        if len(fork_title) > _MAX_TITLE_LENGTH:
            fork_title = fork_title[: _MAX_TITLE_LENGTH - 3] + "..."

        # ── Compose fork meta (parent + optional inherited settings) ─────
        meta: dict = {"parent_id": source.id.value}
        if request.inherit_settings and isinstance(source.meta, dict):
            for key in _INHERITABLE_META_KEYS:
                if key in source.meta:
                    value = source.meta[key]
                    # Defensive copy for dict-valued keys (e.g. discussion) so
                    # the fork's meta never aliases the source aggregate.
                    meta[key] = dict(value) if isinstance(value, dict) else value
        meta["forked_from"] = {
            "conversation_id": source.id.value,
            "round": fork_point_round,
            "title": source.title,
        }

        # ── Build new conversation ───────────────────────────────────────
        now = self._clock.now()
        new_conv = Conversation(
            id=ConversationId.generate(self._ids),
            title=fork_title,
            created_at=now,
            updated_at=now,
            status=ConversationStatus.ACTIVE,
            messages=cloned_messages,
            meta=meta,
        )

        # ── Persist ──────────────────────────────────────────────────────
        await self._conversations.save(new_conv)

        _log.info(
            "chat.conversation_forked",
            source_id=source.id.value,
            fork_id=new_conv.id.value,
            message_count=len(cloned_messages),
            round_count=fork_point_round,
            include_tool_calls=request.include_tool_calls,
            inherit_settings=request.inherit_settings,
        )
        return ForkConversationResult(conversation=new_conv)


__all__ = [
    "ForkConversationUseCase",
    "ForkConversationInput",
    "ForkConversationResult",
]
