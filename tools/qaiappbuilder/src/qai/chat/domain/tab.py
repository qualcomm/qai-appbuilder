# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ConversationTab`` aggregate for multi-tab parallel chat.

A *tab* is the user's view on a conversation in the front-end.  One
front-end tab corresponds to exactly one :class:`ConversationTab`.
Multiple tabs can be open at the same time; each tab streams
independently from the others.

State machine
-------------
Allowed states (``TabStatus``):

::

    IDLE  ──start_stream──>  STREAMING
    STREAMING  ──complete──>  IDLE
    STREAMING  ──abort───>  ABORTED
    IDLE       ──close───>  CLOSED
    ABORTED    ──close───>  CLOSED
    CLOSED                          (terminal)

Any other transition raises :class:`TabStateError`.

Why a separate aggregate?
-------------------------
The :class:`Conversation` aggregate models persistent message history.
The tab models *transient* presentation state -- which conversation is
currently open in which front-end tab, and is it streaming?  Persisting
tabs is cheap (small row), and decoupling them lets multiple tabs point
at the same conversation without forcing :class:`Conversation` itself
to track UI concerns.

Concurrency note
----------------
Distinct conversations may stream in parallel without coordination.
Two tabs streaming the *same* conversation is a domain conflict; the
:class:`StreamChatUseCase` checks this through
:class:`StreamAbortRegistryPort` and raises
:class:`ConversationLockedError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from qai.chat.domain.errors import TabStateError
from qai.chat.domain.ids import ConversationId, TabId
from qai.platform.time import ensure_aware_utc


class TabStatus(str, Enum):
    """The lifecycle status of a :class:`ConversationTab`."""

    IDLE = "idle"
    STREAMING = "streaming"
    ABORTED = "aborted"
    CLOSED = "closed"


@dataclass(slots=True)
class ConversationTab:
    """One front-end tab opened on a conversation.

    Mutating methods raise :class:`TabStateError` when the transition is
    not allowed by the state machine.  ``last_active_at`` is bumped on
    every successful transition.
    """

    id: TabId
    conversation_id: ConversationId
    created_at: datetime
    last_active_at: datetime
    status: TabStatus = TabStatus.IDLE

    def __post_init__(self) -> None:
        if not isinstance(self.id, TabId):
            raise TypeError(
                "ConversationTab.id must be TabId, got "
                f"{type(self.id).__name__}",
            )
        if not isinstance(self.conversation_id, ConversationId):
            raise TypeError(
                "ConversationTab.conversation_id must be ConversationId, got "
                f"{type(self.conversation_id).__name__}",
            )
        ensure_aware_utc(self.created_at)
        ensure_aware_utc(self.last_active_at)
        if not isinstance(self.status, TabStatus):
            raise TypeError(
                "ConversationTab.status must be TabStatus, got "
                f"{type(self.status).__name__}",
            )

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------
    @classmethod
    def open(
        cls,
        *,
        tab_id: TabId,
        conversation_id: ConversationId,
        now: datetime,
    ) -> ConversationTab:
        """Create a fresh tab in :data:`TabStatus.IDLE` state."""
        ts = ensure_aware_utc(now)
        return cls(
            id=tab_id,
            conversation_id=conversation_id,
            created_at=ts,
            last_active_at=ts,
            status=TabStatus.IDLE,
        )

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def start_stream(self, *, now: datetime) -> None:
        """Transition IDLE -> STREAMING."""
        if self.status is not TabStatus.IDLE:
            raise TabStateError(
                f"start_stream() requires status=IDLE, got {self.status.value}",
                current_status=self.status.value,
                attempted="start_stream",
            )
        self.status = TabStatus.STREAMING
        self.last_active_at = ensure_aware_utc(now)

    def complete_stream(self, *, now: datetime) -> None:
        """Transition STREAMING -> IDLE on normal completion."""
        if self.status is not TabStatus.STREAMING:
            raise TabStateError(
                f"complete_stream() requires status=STREAMING, got {self.status.value}",
                current_status=self.status.value,
                attempted="complete_stream",
            )
        self.status = TabStatus.IDLE
        self.last_active_at = ensure_aware_utc(now)

    def abort(self, *, now: datetime) -> None:
        """Transition STREAMING -> ABORTED on user-initiated stop."""
        if self.status is not TabStatus.STREAMING:
            raise TabStateError(
                f"abort() requires status=STREAMING, got {self.status.value}",
                current_status=self.status.value,
                attempted="abort",
            )
        self.status = TabStatus.ABORTED
        self.last_active_at = ensure_aware_utc(now)

    def close(self, *, now: datetime) -> None:
        """Close the tab.  Allowed from IDLE / ABORTED only.

        From STREAMING the caller must :meth:`abort` first -- closing
        a streaming tab without aborting would leak the underlying
        stream handle.
        """
        if self.status is TabStatus.CLOSED:
            return
        if self.status is TabStatus.STREAMING:
            raise TabStateError(
                "close() not allowed while streaming; abort() first",
                current_status=self.status.value,
                attempted="close",
            )
        self.status = TabStatus.CLOSED
        self.last_active_at = ensure_aware_utc(now)

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------
    def is_streaming(self) -> bool:
        return self.status is TabStatus.STREAMING

    def is_terminal(self) -> bool:
        return self.status is TabStatus.CLOSED


__all__ = ["ConversationTab", "TabStatus"]
