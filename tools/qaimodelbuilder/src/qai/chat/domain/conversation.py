# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``Conversation`` aggregate root for the chat bounded context.

A :class:`Conversation` owns:

* a stable id (:class:`ConversationId`);
* a human-readable title (mutable);
* an ordered list of :class:`Message` instances (append-only from the
  domain's perspective; rewrites happen at the persistence layer
  through compaction);
* a status (``active`` / ``archived``).

Mutability model
----------------
The aggregate is mutable -- this is the *one* place in the chat domain
where state changes happen.  Value objects (ids, content, frames) are
all frozen.  All mutators here:

* validate inputs (raising :class:`InvalidConversationTitleError` etc.);
* emit no platform side-effects (no logging / no IO);
* keep ``updated_at`` consistent with the most recent change.

Branch / parent semantics
-------------------------
Branching is supported indirectly: a freshly appended message may set
``parent_id`` to any existing message in the conversation.  The
aggregate verifies the parent exists; ordering is by append-order, not
by parent links.  Retrieving the ``active branch`` is a query at the
read-model level (out of scope for the domain).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from qai.chat.domain.content import MessageContent, MessageRole
from qai.chat.domain.errors import (
    InvalidConversationTitleError,
)
from qai.chat.domain.ids import ConversationId, MessageId
from qai.chat.domain.message import Message
from qai.platform.io_validator import (
    ValidationError as _IoValidationError,
)
from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
)
from qai.platform.time import ensure_aware_utc

_MAX_TITLE_LENGTH: int = 256


class ConversationStatus(str, Enum):
    """Lifecycle status of a conversation."""

    ACTIVE = "active"
    ARCHIVED = "archived"


def _validate_title(title: str) -> str:
    try:
        assert_non_empty(title, name="Conversation.title")
        assert_max_length(
            title,
            max_length=_MAX_TITLE_LENGTH,
            name="Conversation.title",
        )
    except _IoValidationError as exc:
        raise InvalidConversationTitleError(str(exc)) from exc
    return title


@dataclass(slots=True)
class Conversation:
    """The aggregate root for a chat conversation.

    Construction goes through :meth:`create` to ensure ``created_at`` /
    ``updated_at`` are set consistently.  Loading from persistence uses
    the regular constructor with ``messages`` populated.
    """

    id: ConversationId
    title: str
    created_at: datetime
    updated_at: datetime
    status: ConversationStatus = ConversationStatus.ACTIVE
    messages: list[Message] = field(default_factory=list)
    # V1-parity channel source metadata (history_store.py upsert_conversation).
    # Stores e.g. {"source": "wechat", "wechat_user_id": "..."} for channel
    # conversations; None for web-UI conversations.  Appended with default None
    # so existing constructions stay valid (AGENTS.md §3.1).
    #
    # Recognised ``meta`` sub-keys (all optional; all reuse this one persisted
    # ``meta_json`` carrier — no new column / migration, AGENTS.md §3.1):
    #   * ``source`` / ``channel_user_id`` — channel provenance (see above).
    #   * ``title_manual`` (bool) — user chose the title; skip auto-title.
    #   * ``workspace`` (str) — per-session working directory override.
    #   * ``discussion`` (dict) — multi-agent discussion config.
    #   * ``pinned`` / ``favorite`` (bool) — sidebar / library flags.
    #   * ``budget`` (dict) — per-conversation TOKEN budget cap:
    #       ``{"max_tokens": int | None, "used_tokens": int}``. A token-based
    #       cap (renamed from the CC SDK's ``max_budget_usd`` — this project has
    #       no cross-provider USD pricing but DOES have accurate provider usage
    #       counts). ``max_tokens=None`` / absent ⇒ the budget is DISABLED (the
    #       backward-compatible default); ``used_tokens`` is a running counter
    #       (0 on a fresh conversation) fed ONLY from provider-authoritative
    #       per-round usage (State-Truth-First — never an estimate) by the
    #       ``BudgetTrackerPort`` adapter. Read/written via that port + the
    #       ``PATCH /conversations/{id}/budget`` route; the domain aggregate
    #       carries no dedicated field for it (kept in ``meta`` like the flags
    #       above), and :class:`~qai.chat.domain.budget.BudgetCheckResult` is
    #       the VO the port returns from a check.
    meta: dict | None = None

    #: Running counter of the provider-measured full (uncompressed) history
    #: prompt token size (migration 036). Fed per-turn from provider usage
    #: measurements (see ``StreamChatUseCase._finalize_assistant_message``)
    #: and read by ``CompactChatUseCase`` as the "before" figure of the
    #: ``GET /context`` compaction badge. ``None`` = legacy / never measured
    #: (derived on read from the last assistant turn / char estimate).
    #: Tail-appended with default ``None`` so existing constructions stay
    #: valid (AGENTS.md §3.1).
    full_history_tokens: int | None = None

    #: Persisted promote-ready detection result (migration 057). Written at
    #: turn end (``StreamChatUseCase._finalize_assistant_message`` via the
    #: apps-layer ``PromoteReadyScanPort``): the model workspace path extracted
    #: from the turn's final summary + the precision variants scanned on disk.
    #: Shape:
    #:   {"workdir": str, "variants": [{"precision": str, "label": str}, ...],
    #:    "checked_at": "<iso8601>"}
    #: An empty ``workdir`` / empty ``variants`` records "checked, nothing to
    #: promote"; ``None`` = never detected (legacy / forward-compatible
    #: default). Read by the frontend promote-ready CTA (0 on-open disk scans).
    #: Tail-appended with default ``None`` so existing constructions stay valid
    #: (AGENTS.md §3.1). Persisted via ``save_messages`` (preserve_header) too —
    #: both ON CONFLICT branches write it, like ``full_history_tokens``.
    detected_model: dict | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, ConversationId):
            raise TypeError(
                "Conversation.id must be ConversationId, got "
                f"{type(self.id).__name__}",
            )
        _validate_title(self.title)
        ensure_aware_utc(self.created_at)
        ensure_aware_utc(self.updated_at)
        if not isinstance(self.status, ConversationStatus):
            raise TypeError(
                "Conversation.status must be ConversationStatus, got "
                f"{type(self.status).__name__}",
            )

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        conversation_id: ConversationId,
        title: str,
        now: datetime,
        meta: dict | None = None,
    ) -> Conversation:
        """Construct a brand-new, empty conversation.

        ``meta`` is an **optional appended** parameter (AGENTS.md §3.1 —
        only grows the signature at the tail; existing call sites that omit
        it keep ``meta=None``).  It seeds the already-persisted ``meta`` dict
        so a freshly-minted conversation can carry channel-source metadata
        (e.g. ``{"source": "wechat", "channel_user_id": "..."}``) at birth —
        V1 parity with ``history_store.upsert_conversation(meta=...)``
        (``QAIModelBuilder_v0.5_pure/backend/channels/wechat/channel.py:1083``)
        so a service restart can later restore the same conversation for the
        same channel user (``get_latest_wechat_conversation``).  A non-dict /
        empty value normalises to ``None`` (matching the field default), so
        web-UI conversations stay ``meta=None``.
        """
        ts = ensure_aware_utc(now)
        return cls(
            id=conversation_id,
            title=_validate_title(title),
            created_at=ts,
            updated_at=ts,
            status=ConversationStatus.ACTIVE,
            messages=[],
            meta=dict(meta) if isinstance(meta, dict) and meta else None,
        )

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def rename(self, new_title: str, *, now: datetime) -> None:
        """Set a new title and bump ``updated_at``."""
        self.title = _validate_title(new_title)
        self.updated_at = ensure_aware_utc(now)

    def mark_title_manual(self, *, now: datetime) -> None:
        """Record that the user manually chose this conversation's title.

        Sets ``meta["title_manual"] = True`` so the first-round auto-title
        generation (``apps/api/_chat_title_push``) skips this conversation —
        a user-chosen title is authoritative and must never be overwritten
        by a model summary. Other ``meta`` keys (channel source, workspace)
        are preserved. Bumps ``updated_at``. Stored in the already-persisted
        ``meta`` dict so no DB schema change is needed (AGENTS.md §3.1).
        """
        meta = dict(self.meta) if isinstance(self.meta, dict) else {}
        meta["title_manual"] = True
        self.meta = meta
        self.updated_at = ensure_aware_utc(now)

    def set_workspace(self, workspace: str | None, *, now: datetime) -> None:
        """Set (or clear) the per-session workspace directory in ``meta``.

        A non-empty ``workspace`` records the session-specific working
        directory under ``meta["workspace"]``; a blank / ``None`` value
        clears it (the session then falls back to the global configured
        workspace). Other ``meta`` keys (e.g. channel source) are
        preserved. Bumps ``updated_at``.
        """
        cleaned = (workspace or "").strip()
        meta = dict(self.meta) if isinstance(self.meta, dict) else {}
        if cleaned:
            meta["workspace"] = cleaned
        else:
            meta.pop("workspace", None)
        self.meta = meta or None
        self.updated_at = ensure_aware_utc(now)

    def set_discussion(
        self,
        discussion: dict[str, Any] | None,
        *,
        now: datetime,
    ) -> None:
        """Set (or clear) the multi-agent discussion config in ``meta``.

        Stores the discussion settings under ``meta["discussion"]`` (scheme
        A -- reuse the already-persisted ``meta_json`` carrier, no new table
        / migration; docs/70-multi-agent/multi-agent-conversation-design.md §16). A
        ``None`` / empty value clears it (the conversation reverts to a plain
        non-discussion chat). Recognised keys:

        * ``is_discussion`` (bool): whether discussion mode is active;
        * ``selector_mode`` (str): ``"manager"`` / ``"round_robin"``;
        * ``max_rounds`` (int): hard cap on speaker rounds;
        * ``enable_judge`` (bool): whether a final judge turn runs.
        * ``discussion_prompt`` (str): optional discussion FRAMING prompt
          prepended before each speaker's persona (§18.1); empty / absent ⇒
          the orchestrator falls back to its built-in default.
        * ``convergence_control_enabled`` (bool): DISC-2 二期 master switch for
          discussion convergence control (absent ⇒ OFF).
        * ``manager_early_end_enabled`` (bool): allow the manager selector to
          END early (absent ⇒ OFF).
        * ``soft_stop_enabled`` (bool): allow soft-stop convergence
          (absent ⇒ OFF).
        * ``soft_stop_mode`` (str): soft-stop aggressiveness
          (absent ⇒ ``"conservative"``).

        Other ``meta`` keys (workspace, channel source, title_manual) are
        preserved. Bumps ``updated_at``. Mirrors :meth:`set_workspace`'s
        copy -> set/pop -> ``meta or None`` -> bump range so it travels the
        ``save`` path (not ``save_messages``).
        """
        if discussion is not None and not isinstance(discussion, dict):
            raise TypeError("discussion must be a dict or None")
        meta = dict(self.meta) if isinstance(self.meta, dict) else {}
        if discussion:
            meta["discussion"] = dict(discussion)
        else:
            meta.pop("discussion", None)
        self.meta = meta or None
        self.updated_at = ensure_aware_utc(now)

    @property
    def discussion(self) -> dict[str, Any] | None:
        """Return the multi-agent discussion config from ``meta`` (or ``None``).

        Read accessor for ``meta["discussion"]``; returns a defensive copy so
        callers cannot mutate the aggregate's ``meta`` in place.
        """
        if not isinstance(self.meta, dict):
            return None
        value = self.meta.get("discussion")
        return dict(value) if isinstance(value, dict) else None

    def set_pinned(self, pinned: bool, *, now: datetime) -> None:
        """Pin (or unpin) the conversation to the top of the sidebar list.

        Stores a boolean flag under ``meta["pinned"]`` (scheme A -- reuse the
        already-persisted ``meta_json`` carrier, no new column / migration;
        same range as :meth:`set_workspace`). A truthy value pins the
        conversation so the UI surfaces it above the time-bucketed history; a
        falsy value clears the flag. Other ``meta`` keys (workspace, channel
        source, discussion, favorite) are preserved. Bumps ``updated_at``.
        """
        meta = dict(self.meta) if isinstance(self.meta, dict) else {}
        if pinned:
            meta["pinned"] = True
        else:
            meta.pop("pinned", None)
        self.meta = meta or None
        self.updated_at = ensure_aware_utc(now)

    @property
    def pinned(self) -> bool:
        """Return whether the conversation is pinned (``meta["pinned"]``)."""
        if not isinstance(self.meta, dict):
            return False
        return bool(self.meta.get("pinned"))

    def set_favorite(self, favorite: bool, *, now: datetime) -> None:
        """Favorite (or unfavorite) the conversation for the favorites library.

        Stores a boolean flag under ``meta["favorite"]`` (scheme A -- reuse
        the already-persisted ``meta_json`` carrier, no new column /
        migration; same range as :meth:`set_workspace`). A truthy value marks
        the conversation so it appears in the favorites dialog; a falsy value
        clears the flag. Other ``meta`` keys (workspace, channel source,
        discussion, pinned) are preserved. Bumps ``updated_at``.
        """
        meta = dict(self.meta) if isinstance(self.meta, dict) else {}
        if favorite:
            meta["favorite"] = True
        else:
            meta.pop("favorite", None)
        self.meta = meta or None
        self.updated_at = ensure_aware_utc(now)

    @property
    def favorite(self) -> bool:
        """Return whether the conversation is favorited (``meta["favorite"]``)."""
        if not isinstance(self.meta, dict):
            return False
        return bool(self.meta.get("favorite"))

    def archive(self, *, now: datetime) -> None:
        """Mark the conversation as archived (idempotent)."""
        if self.status is ConversationStatus.ARCHIVED:
            return
        self.status = ConversationStatus.ARCHIVED
        self.updated_at = ensure_aware_utc(now)

    def append_message(self, message: Message) -> None:
        """Append a message; raises if its ``parent_id`` is not present.

        The aggregate does not deduplicate by id; callers must mint a
        unique :class:`MessageId` per append.  Duplicate ids raise
        :class:`ValueError` to surface programming errors early.
        """
        if not isinstance(message, Message):
            raise TypeError(
                "append_message expects Message, got "
                f"{type(message).__name__}",
            )
        for existing in self.messages:
            if existing.id == message.id:
                raise ValueError(
                    f"message id {message.id} already present in conversation",
                )
        if message.parent_id is not None:
            if not any(m.id == message.parent_id for m in self.messages):
                raise ValueError(
                    f"parent_id {message.parent_id} does not refer to an "
                    "existing message",
                )
        self.messages.append(message)
        self.updated_at = message.created_at

    # ------------------------------------------------------------------
    # Convenience queries (no IO -- pure list scans)
    # ------------------------------------------------------------------
    @property
    def message_count(self) -> int:
        return len(self.messages)

    def find_message(self, message_id: MessageId) -> Message | None:
        for m in self.messages:
            if m.id == message_id:
                return m
        return None

    def last_message(self) -> Message | None:
        return self.messages[-1] if self.messages else None

    def messages_by_role(self, role: MessageRole) -> list[Message]:
        return [m for m in self.messages if m.role is role]

    def matches_text(self, needle: str) -> bool:
        """Return True iff title or any message contains ``needle``.

        Case-insensitive substring match.  Used by the search use case
        when a fancier full-text index isn't available -- adapters may
        override with proper FTS.
        """
        if not needle:
            return True
        n = needle.lower()
        if n in self.title.lower():
            return True
        for m in self.messages:
            if n in m.content.text.lower():
                return True
        return False


__all__ = [
    "Conversation",
    "ConversationStatus",
    # re-export for convenience
    "MessageContent",
    "MessageRole",
]
