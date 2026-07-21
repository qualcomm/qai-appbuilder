# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Memory context use case (PR-402 / S7.5 lane L4).

Migrates :meth:`backend.memory.ExperienceMemory.build_context_block`
into the chat bounded context as a thin orchestration layer over
:class:`ExperienceRecallPort`.

The use case is a one-call wrapper that:

1. invokes :meth:`ExperienceRecallPort.build_context_block`;
2. emits an info-level structured log so observability can audit which
   experience ids ended up in the prompt of which conversation;
3. returns the materialised XML block to the caller (chat use case
   layer is responsible for actually injecting it into the system
   prompt — keeping the side-effect surface minimal here).
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.chat.application.ports import (
    ExperienceRecallPort,
    MemoryContextBlock,
)
from qai.chat.domain.ids import ConversationId
from qai.platform.logging import get_logger

_log = get_logger(__name__)


# Default cap on rendered ``<past_experiences>`` block size. Mirrors
# legacy ``backend/memory.py:build_context_block(max_chars=3000)``.
DEFAULT_MEMORY_BLOCK_MAX_CHARS: int = 3000


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildMemoryContextInput:
    """Inputs to :meth:`BuildMemoryContextUseCase.execute`."""

    query: str
    """Free-text query — typically the latest user message or a
    summarised topic.  Empty / whitespace-only queries skip the recall
    call and return an empty block."""
    conversation_id: ConversationId | None = None
    """Optional, used purely for logging context — the recall side does
    not filter by conversation (experiences are global / cross-session
    knowledge)."""
    max_chars: int = DEFAULT_MEMORY_BLOCK_MAX_CHARS


class BuildMemoryContextUseCase:
    """Render a ``<past_experiences>`` XML block for prompt injection.

    Single-port use case; all retrieval logic lives in the port
    implementation (the production adapter today is
    :class:`~qai.chat.adapters.experience_recall.SqliteExperienceRecall`,
    which uses ``LIKE`` against the ``chat_experience`` table — the
    same shape the legacy backend exposed).  Swapping to FTS5 or a
    vector backend is a port-implementation concern and does not change
    this use case's contract.
    The use case adds:

    * empty-query short-circuit (saves a DB roundtrip);
    * structured logging for audit;
    * a stable :class:`MemoryContextBlock` return shape for callers
      that want to inject the text directly into a system prompt.
    """

    def __init__(self, *, recall: ExperienceRecallPort) -> None:
        self._recall = recall

    async def execute(
        self,
        input: BuildMemoryContextInput,
    ) -> MemoryContextBlock:
        if not input.query or not input.query.strip():
            return MemoryContextBlock(text="", hit_ids=())
        block = await self._recall.build_context_block(
            query=input.query,
            max_chars=input.max_chars,
        )
        _log.info(
            "chat.memory_context_built",
            conversation_id=(
                input.conversation_id.value if input.conversation_id else None
            ),
            query_length=len(input.query),
            block_chars=len(block.text),
            hit_count=len(block.hit_ids),
        )
        return block


__all__ = [
    "BuildMemoryContextUseCase",
    "BuildMemoryContextInput",
    "DEFAULT_MEMORY_BLOCK_MAX_CHARS",
]
