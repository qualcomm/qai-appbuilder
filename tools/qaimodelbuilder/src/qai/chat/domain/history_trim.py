# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Round-based conversation-history trimming (pure domain logic).

A *round* mirrors V1's channel semantics
(``QAIModelBuilder_v1_pure/backend/channels/wechat/channel.py:237``
``_trim_history_by_rounds``):

* A round STARTS at a ``role=user`` message.
* It INCLUDES every following ``assistant`` / ``tool`` message (the
  tool-using middle turns) up to — but excluding — the next ``user``
  message.

:func:`trim_messages_by_rounds` keeps the most recent ``max_rounds`` rounds
and drops the oldest WHOLE rounds.  Trimming always falls on a round boundary
(a ``user`` message), so a tool round is never sliced apart — no orphan
``role=tool`` message can be produced.

This is the *user-explicit hard cap* applied BEFORE token compaction: when a
channel user issues ``/compact <N>`` the per-user override is forwarded as
``StreamChatInput.extra["max_history_rounds"]`` and the streaming use case
trims the derived history to ``N`` rounds here; the remainder is then handed
to the normal token-budget compaction path as usual.  When no override is
present this function is never called, so plain token compaction governs
(V2 upgrade over V1's unconditional one-size-fits-all round cap).

Pure function, no side effects: the chat domain owns the notion of a "round"
and must stay free of fastapi / sqlalchemy / apps imports (AGENTS.md §3.2 /
domain-purity contract).
"""

from __future__ import annotations

from typing import Any, Sequence, TypeVar

T = TypeVar("T")


def _is_user_message(message: Any) -> bool:
    """Return ``True`` when *message* is a ``role=user`` turn.

    Tolerant of both the domain :class:`~qai.chat.domain.content.MessageRole`
    enum (compared by its ``.value``) and plain ``str`` roles, mirroring the
    defensive role check used elsewhere in the streaming pipeline
    (``_streaming_subagent_frames.drop_trailing_current_user``).
    """
    role = getattr(message, "role", None)
    if role is None:
        return False
    return role == "user" or getattr(role, "value", None) == "user"


def trim_messages_by_rounds(
    messages: Sequence[T], max_rounds: int
) -> tuple[T, ...]:
    """Keep the most recent ``max_rounds`` conversation rounds.

    V1 parity: ``backend/channels/wechat/channel.py:237``
    ``_trim_history_by_rounds``.

    Args:
        messages: The conversation history (domain ``Message`` objects, or any
            objects exposing a ``role`` attribute).  Each ``role=user`` entry
            marks the start of a round.
        max_rounds: How many of the most recent rounds to retain.  ``<= 0``
            yields an empty history (matches V1's guard).

    Returns:
        A tuple of the retained messages, sliced on a round boundary so no
        round is split (no orphan ``role=tool`` message is created).  The
        input is returned (as a tuple) unchanged when it already has
        ``<= max_rounds`` rounds.
    """
    seq: tuple[T, ...] = tuple(messages)
    if max_rounds <= 0:
        return ()
    if not seq:
        return seq

    # Indices of each round start (every ``role=user`` message).
    user_indices = [i for i, m in enumerate(seq) if _is_user_message(m)]
    total_rounds = len(user_indices)

    if total_rounds <= max_rounds:
        return seq  # nothing to drop

    # Drop the oldest (total_rounds - max_rounds) rounds; keep from the start
    # of the first retained round (a ``user`` boundary).
    rounds_to_drop = total_rounds - max_rounds
    keep_from = user_indices[rounds_to_drop]
    return seq[keep_from:]
