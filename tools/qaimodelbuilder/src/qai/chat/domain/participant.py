# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``Participant`` aggregate for the chat bounded context.

A :class:`Participant` is the generic abstraction for a *speaker* in a
conversation -- the orthogonal "who said it" dimension that complements
``MessageRole`` (the "what kind of turn" dimension, which stays locked at
``system`` / ``user`` / ``assistant`` / ``tool``).

Today
-----
Every sub-agent spawned in a conversation is modelled as a
``kind=SUB_AGENT`` participant, optionally linked (via
``subagent_session_id``) to the :class:`~qai.chat.domain.sub_agent_session.
SubAgentSession` that holds its independent context.

Tomorrow (multi-agent conversations)
-------------------------------------
A single conversation may host several ``kind=NAMED_AGENT`` participants
(e.g. an *Analyst* and a *Skeptic*).  Each emits ``role=assistant``
messages that differ only by their ``Message.sender_id`` -- the message's
``sender_id`` carries this participant's :class:`ParticipantId`.  The role
contract (already locked) is untouched; the participant is the orthogonal
identity axis layered on top.

Mutability model
----------------
The aggregate is mutable (``display_name`` etc. may change).  Mutators bump
``updated_at`` and emit no platform side-effects, mirroring
:class:`~qai.chat.domain.conversation.Conversation`.  The participant is
mostly a data backbone today and intentionally carries little behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from qai.chat.domain.ids import (
    ConversationId,
    ParticipantId,
    SubAgentSessionId,
)
from qai.platform.time import ensure_aware_utc

_MAX_DISPLAY_NAME_LENGTH: int = 256


class ParticipantKind(str, Enum):
    """The category of a conversation :class:`Participant`."""

    USER = "user"
    MAIN_AGENT = "main_agent"
    SUB_AGENT = "sub_agent"
    # Reserved for future multi-agent conversations: a named role agent
    # (e.g. Analyst / Skeptic) that emits ``role=assistant`` turns
    # distinguished by ``Message.sender_id``.
    NAMED_AGENT = "named_agent"


@dataclass(slots=True, kw_only=True)
class Participant:
    """A speaker that belongs to a conversation.

    Construction goes through :meth:`create` to set ``created_at`` /
    ``updated_at`` consistently; loading from persistence uses the regular
    constructor.
    """

    id: ParticipantId
    conversation_id: ConversationId
    kind: ParticipantKind
    display_name: str = ""
    # Optional, for future named agents: the model and persona backing this
    # participant.  ``None`` for plain user / main-agent participants.
    model_id: str | None = None
    persona: str | None = None
    # Optional link: a ``SUB_AGENT`` participant owns an independent
    # :class:`SubAgentSession` carrying its own context.  ``None`` for
    # user / main-agent / not-yet-linked participants.
    subagent_session_id: SubAgentSessionId | None = None
    # Optional, for future named agents: a free-form per-participant config
    # blob persisted as JSON (``chat_participant.config_json``).  Recognised
    # keys today: ``allowed_tools`` (list[str] of tool names this role may
    # invoke in a discussion), ``enabled_skills`` (list[str] of skill ids this
    # role may use -- a WHITELIST subset of the globally-enabled skills;
    # absent/empty means the role gets NO skill) and ``color`` (a theme-palette
    # token/index for the bubble -- never a hard-coded colour).  ``None`` for
    # plain user / main-agent / sub-agent participants.
    config: dict[str, Any] | None = None
    #: Optional provenance link (migration 056 ``chat_participant.template_id``):
    #: the built-in template this participant was imported from, letting the
    #: discussion orchestrator re-resolve a built-in role's persona by
    #: (template_id + current locale) at runtime (method A). Encoding:
    #:   * single-role import  -> the agent template id (e.g. ``builtin-agent-architect``)
    #:   * team member import   -> ``"<roster_id>#<member_index>"`` (e.g.
    #:     ``builtin-arch-dev-test#0``) so the runtime can locate the exact member
    #:     inside ``members_i18n[locale]``.
    #: ``None`` = not sourced from a built-in template (user-authored / main /
    #: sub-agent) -> no override, existing behaviour byte-for-byte unchanged.
    #: Tail-appended optional field (§3.1 additive) so old constructors / old
    #: rows (which read back NULL -> None) are unaffected.
    template_id: str | None = None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, ParticipantId):
            raise TypeError(
                "Participant.id must be ParticipantId, got "
                f"{type(self.id).__name__}",
            )
        if not isinstance(self.conversation_id, ConversationId):
            raise TypeError(
                "Participant.conversation_id must be ConversationId, got "
                f"{type(self.conversation_id).__name__}",
            )
        if not isinstance(self.kind, ParticipantKind):
            raise TypeError(
                "Participant.kind must be ParticipantKind, got "
                f"{type(self.kind).__name__}",
            )
        if not isinstance(self.display_name, str):
            raise TypeError("Participant.display_name must be a str")
        if len(self.display_name) > _MAX_DISPLAY_NAME_LENGTH:
            raise ValueError(
                "Participant.display_name must be "
                f"<= {_MAX_DISPLAY_NAME_LENGTH} chars",
            )
        if self.model_id is not None and not isinstance(self.model_id, str):
            raise TypeError("Participant.model_id must be a str or None")
        if self.persona is not None and not isinstance(self.persona, str):
            raise TypeError("Participant.persona must be a str or None")
        if self.subagent_session_id is not None and not isinstance(
            self.subagent_session_id,
            SubAgentSessionId,
        ):
            raise TypeError(
                "Participant.subagent_session_id must be SubAgentSessionId "
                f"or None, got {type(self.subagent_session_id).__name__}",
            )
        if self.config is not None:
            if not isinstance(self.config, dict):
                raise TypeError("Participant.config must be a dict or None")
            allowed = self.config.get("allowed_tools")
            if allowed is not None:
                if not isinstance(allowed, (list, tuple)) or not all(
                    isinstance(tool, str) for tool in allowed
                ):
                    raise TypeError(
                        "Participant.config['allowed_tools'] must be a list "
                        "of str when present",
                    )
            color = self.config.get("color")
            if color is not None and (
                isinstance(color, bool) or not isinstance(color, (str, int))
            ):
                raise TypeError(
                    "Participant.config['color'] must be a str, int "
                    "(palette index) or None",
                )
            enabled_skills = self.config.get("enabled_skills")
            if enabled_skills is not None:
                if not isinstance(enabled_skills, (list, tuple)) or not all(
                    isinstance(sid, str) for sid in enabled_skills
                ):
                    raise TypeError(
                        "Participant.config['enabled_skills'] must be a list "
                        "of str when present",
                    )
            # Defensive deep-ish copy so callers cannot mutate the shared
            # dict / nested ``allowed_tools`` / ``enabled_skills`` list through
            # the aggregate.
            copied: dict[str, Any] = dict(self.config)
            if allowed is not None:
                copied["allowed_tools"] = list(allowed)
            if enabled_skills is not None:
                copied["enabled_skills"] = list(enabled_skills)
            self.config = copied
        if self.template_id is not None and not isinstance(self.template_id, str):
            raise TypeError("Participant.template_id must be a str or None")
        ensure_aware_utc(self.created_at)
        ensure_aware_utc(self.updated_at)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        participant_id: ParticipantId,
        conversation_id: ConversationId,
        kind: ParticipantKind,
        now: datetime,
        display_name: str = "",
        model_id: str | None = None,
        persona: str | None = None,
        subagent_session_id: SubAgentSessionId | None = None,
        config: dict[str, Any] | None = None,
        template_id: str | None = None,
    ) -> Participant:
        """Construct a brand-new participant."""
        ts = ensure_aware_utc(now)
        return cls(
            id=participant_id,
            conversation_id=conversation_id,
            kind=kind,
            display_name=display_name,
            model_id=model_id,
            persona=persona,
            subagent_session_id=subagent_session_id,
            config=config,
            template_id=template_id,
            created_at=ts,
            updated_at=ts,
        )

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def rename(self, display_name: str, *, now: datetime) -> None:
        """Set a new display name and bump ``updated_at``."""
        if not isinstance(display_name, str):
            raise TypeError("display_name must be a str")
        if len(display_name) > _MAX_DISPLAY_NAME_LENGTH:
            raise ValueError(
                f"display_name must be <= {_MAX_DISPLAY_NAME_LENGTH} chars",
            )
        self.display_name = display_name
        self.updated_at = ensure_aware_utc(now)

    def link_session(
        self,
        session_id: SubAgentSessionId,
        *,
        now: datetime,
    ) -> None:
        """Associate this participant with a sub-agent session.

        Only meaningful for ``SUB_AGENT`` participants; rejected for other
        kinds to surface programming errors early.
        """
        if self.kind is not ParticipantKind.SUB_AGENT:
            raise ValueError(
                "link_session() is only allowed for SUB_AGENT participants, "
                f"got kind={self.kind.value}",
            )
        if not isinstance(session_id, SubAgentSessionId):
            raise TypeError("session_id must be a SubAgentSessionId")
        self.subagent_session_id = session_id
        self.updated_at = ensure_aware_utc(now)

    def set_config(
        self,
        config: dict[str, Any] | None,
        *,
        now: datetime,
    ) -> None:
        """Replace the per-participant config blob and bump ``updated_at``.

        ``None`` (or an empty dict) clears the config back to ``None``.  The
        same shape validation as :meth:`__post_init__` is enforced.
        """
        if config is not None and not isinstance(config, dict):
            raise TypeError("config must be a dict or None")
        cleaned: dict[str, Any] | None = dict(config) if config else None
        if cleaned is not None:
            allowed = cleaned.get("allowed_tools")
            if allowed is not None and (
                not isinstance(allowed, (list, tuple))
                or not all(isinstance(tool, str) for tool in allowed)
            ):
                raise TypeError(
                    "config['allowed_tools'] must be a list of str when "
                    "present",
                )
            color = cleaned.get("color")
            if color is not None and (
                isinstance(color, bool) or not isinstance(color, (str, int))
            ):
                raise TypeError(
                    "config['color'] must be a str, int (palette index) "
                    "or None",
                )
            enabled_skills = cleaned.get("enabled_skills")
            if enabled_skills is not None and (
                not isinstance(enabled_skills, (list, tuple))
                or not all(isinstance(sid, str) for sid in enabled_skills)
            ):
                raise TypeError(
                    "config['enabled_skills'] must be a list of str when "
                    "present",
                )
            if allowed is not None:
                cleaned["allowed_tools"] = list(allowed)
            if enabled_skills is not None:
                cleaned["enabled_skills"] = list(enabled_skills)
        self.config = cleaned
        self.updated_at = ensure_aware_utc(now)


__all__ = [
    "Participant",
    "ParticipantKind",
]
