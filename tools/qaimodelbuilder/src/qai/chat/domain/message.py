# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``Message`` entity for the chat bounded context.

A :class:`Message` represents a single turn in a conversation.  It is
NOT the aggregate root -- :class:`~qai.chat.domain.conversation.Conversation`
owns its messages -- but it has its own identity (:class:`MessageId`)
because clients reference individual messages (e.g. for branch parents
and tool-result attribution).

Branch / parent semantics:

* ``parent_id`` is the id of the message this one logically follows.
  ``None`` denotes a "root" message (the first turn of a conversation
  or the start of an alternative branch).
* The chat domain itself does not enforce DAG structure -- that
  belongs to :class:`Conversation` -- but ``Message`` instances are
  intended to be linked by ``parent_id`` to support branching and
  retry-without-losing-history flows.

Tool-call / tool-result fields are kept as opaque payloads (``dict``)
because the chat context is not the source of truth for tool schemas;
the ``tools`` context owns those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.ids import MessageId
from qai.platform.time import ensure_aware_utc


@dataclass(frozen=True, slots=True, kw_only=True)
class Message:
    """A single turn in a conversation.

    Frozen by design: appending to a conversation produces a NEW Message
    rather than mutating an existing one.  Mutating fields (e.g. when a
    streamed assistant turn is finalised) is modelled by emitting a
    new frozen instance and replacing it inside :class:`Conversation`.
    """

    id: MessageId
    role: MessageRole
    content: MessageContent
    created_at: datetime
    parent_id: MessageId | None = None
    tool_calls: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    tool_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    # Token usage for an assistant turn, as emitted by the terminal stream
    # ``END`` frame (normalized OpenAI shape:
    # ``{prompt_tokens, completion_tokens, total_tokens, cache_read_tokens?,
    # cache_write_tokens?}`` — see
    # ``qai.chat.infrastructure.llm_stream._extract_usage``).  ``None`` when
    # the turn carried no usage block (older streams / non-assistant roles).
    # Optional appended field (AGENTS.md §3.1: namespace fields may only be
    # appended); stays a plain dict so the domain holds no transport types.
    usage: dict[str, Any] | None = None
    # Model that produced an assistant turn (V1 parity:
    # ``msg.model_id`` / ``msg.model_provider``). Lets the UI show the real
    # model name in a history bubble even after the user switches models.
    # ``None`` for non-assistant turns / legacy rows. Optional appended
    # fields (AGENTS.md §3.1) — plain strings, no transport types.
    model_id: str | None = None
    model_provider: str | None = None
    # V1-parity free-form meta envelope (mirrors V1 ``messages.meta`` JSON;
    # see ``backend/history_store.py:_row_to_message``). V2 persists the
    # client-renderable render extras the streaming writer emits
    # (``build_assistant_meta``) under a single ``meta`` dict instead of
    # growing sibling columns: currently ``request_id`` (prompt-snapshot
    # button), ``perf`` (latency line) and ``subAgentBlocks`` (sub-agent
    # fold blocks). The frontend lifts ``perf`` / ``subAgentBlocks`` to
    # top-level ChatMessage fields on history load and reads ``request_id``
    # from ``meta.request_id``. Other V1 render fields (image_url /
    # tool_full_output / tool_truncated / tool_output_size) ride the live
    # SSE frames and are not part of the persisted render extras.
    # Optional appended field (AGENTS.md §3.1) — a plain dict, so the domain
    # holds no transport types. ``None`` / absent ⇒ a turn with no extras.
    meta: dict[str, Any] | None = None
    # Speaker dimension, orthogonal to ``role``: identifies *which
    # participant* produced this turn. Today it carries sub-agent output
    # attribution (a message produced inside a spawned sub-agent's run
    # belongs to that sub-agent's :class:`Participant`); in the future it
    # will carry the attribution of distinct named agents in a multi-agent
    # conversation (e.g. Analyst vs Skeptic), where several turns share
    # ``role=assistant`` but differ by ``sender_id``. ``None`` is the
    # default for ordinary user / main-agent messages and keeps legacy
    # rows backward-compatible. Stores the raw ``ParticipantId`` string
    # (no VO) so the domain entity holds no cross-reference object graph.
    # Optional appended field (AGENTS.md §3.1) — a plain string, so the
    # domain holds no transport types.
    sender_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, MessageId):
            raise TypeError(
                f"Message.id must be MessageId, got {type(self.id).__name__}",
            )
        if not isinstance(self.role, MessageRole):
            raise TypeError(
                f"Message.role must be MessageRole, got {type(self.role).__name__}",
            )
        if not isinstance(self.content, MessageContent):
            raise TypeError(
                "Message.content must be MessageContent, got "
                f"{type(self.content).__name__}",
            )
        # Force tz-aware UTC: domain code never deals with naive datetimes.
        ensure_aware_utc(self.created_at)
        if self.parent_id is not None and not isinstance(self.parent_id, MessageId):
            raise TypeError(
                "Message.parent_id must be MessageId or None, got "
                f"{type(self.parent_id).__name__}",
            )
        for call in self.tool_calls:
            if not isinstance(call, dict):
                raise TypeError("tool_calls entries must be dict")
        for result in self.tool_results:
            if not isinstance(result, dict):
                raise TypeError("tool_results entries must be dict")
        if self.usage is not None and not isinstance(self.usage, dict):
            raise TypeError("usage must be a dict or None")
        if self.model_id is not None and not isinstance(self.model_id, str):
            raise TypeError("model_id must be a str or None")
        if self.model_provider is not None and not isinstance(self.model_provider, str):
            raise TypeError("model_provider must be a str or None")
        if self.meta is not None and not isinstance(self.meta, dict):
            raise TypeError("meta must be a dict or None")
        if self.sender_id is not None and not isinstance(self.sender_id, str):
            raise TypeError("sender_id must be a str or None")

    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def has_tool_results(self) -> bool:
        return bool(self.tool_results)


__all__ = ["Message"]
