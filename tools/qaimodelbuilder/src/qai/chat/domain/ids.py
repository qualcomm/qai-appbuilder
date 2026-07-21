# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Identifier value objects for the chat bounded context.

Each identifier is a thin :class:`~dataclasses.dataclass` wrapping a single
``value: str`` field.  Keeping them as proper VOs (instead of bare ``str``
``NewType`` aliases) gives us:

* construction-time validation (non-empty, length-bounded);
* type safety -- ``ConversationId`` and ``TabId`` are NOT interchangeable
  even though both are strings underneath;
* equality / hashing for free via ``frozen=True``;
* a single place to evolve representation later (e.g. swap ULID for
  UUID v7) without touching call sites.

Construction goes through :meth:`generate` (uses an injected
:class:`~qai.platform.ids.IdGenerator`) or :meth:`of` (wraps an
already-existing string from persistence).  Domain code MUST NOT call
``new_ulid()`` directly to keep ID generation testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.platform.ids import IdGenerator
from qai.platform.io_validator import assert_max_length, assert_non_empty

_MAX_ID_LENGTH = 64


def _validate_id(value: str, *, name: str) -> str:
    assert_non_empty(value, name=name)
    assert_max_length(value, max_length=_MAX_ID_LENGTH, name=name)
    return value


@dataclass(frozen=True, slots=True)
class ConversationId:
    """Stable identifier for a :class:`Conversation` aggregate."""

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="ConversationId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> ConversationId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> ConversationId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class MessageId:
    """Stable identifier for a :class:`Message` entity."""

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="MessageId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> MessageId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> MessageId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class TabId:
    """Stable identifier for a :class:`ConversationTab` aggregate.

    A tab represents the user's *view* on a conversation in the front-end
    (one front-end tab = one ``TabId``).  Multiple tabs may point at the
    same :class:`ConversationId`, but only one of them may be in
    ``streaming`` state at a time -- see :class:`ConversationTab` for
    the state-machine rules.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="TabId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> TabId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> TabId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ExperienceId:
    """Stable identifier for an :class:`Experience` entry."""

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="ExperienceId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> ExperienceId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> ExperienceId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SubAgentSessionId:
    """Stable identifier for a :class:`SubAgentSession` aggregate.

    A sub-agent session carries the persisted context (``wire_messages``)
    of a sub-agent spawned from a parent conversation, enabling the main
    agent to wake it up and resume, and the user to take it over.  Kept as
    a proper VO (not a bare ``str``) for the same type-safety reasons as
    :class:`ConversationId`.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="SubAgentSessionId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> SubAgentSessionId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> SubAgentSessionId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ParticipantId:
    """Stable identifier for a :class:`Participant` aggregate.

    A participant is a "speaker" in a conversation (the user, the main
    agent, a sub-agent, or a future named agent).  This id is orthogonal
    to ``MessageRole`` -- it answers *who* produced a turn, not *what kind*
    of turn it is.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="ParticipantId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> ParticipantId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> ParticipantId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class RosterTemplateId:
    """Stable identifier for a :class:`RosterTemplate` aggregate.

    A roster template is a named, reusable bundle of discussion role
    definitions (a "team").  Unlike :class:`ParticipantId` (which identifies a
    speaker bound to one conversation) this id identifies a
    conversation-independent, library-level template.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="RosterTemplateId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> RosterTemplateId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> RosterTemplateId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class AgentTemplateId:
    """Stable identifier for an :class:`AgentTemplate` aggregate.

    An agent template is a named, reusable definition of a SINGLE discussion
    role (the smallest reusable unit in the three-tier template system, §27).
    Unlike :class:`ParticipantId` (a speaker bound to one conversation) this id
    identifies a conversation-independent, library-level template; unlike
    :class:`RosterTemplateId` (a whole "team") it identifies just one role.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="AgentTemplateId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> AgentTemplateId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> AgentTemplateId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ModeTemplateId:
    """Stable identifier for a :class:`ModeTemplate` aggregate.

    A mode template is a named collaboration mode ("怎么协作": discussion /
    review / debate / implementation / custom) — the third tier of the
    three-tier template system (§26 / §27).  Orthogonal to
    :class:`RosterTemplateId` (a team "谁参与") and :class:`AgentTemplateId`
    (one role).
    """

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="ModeTemplateId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> ModeTemplateId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> ModeTemplateId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


__all__ = [
    "ConversationId",
    "MessageId",
    "TabId",
    "ExperienceId",
    "SubAgentSessionId",
    "ParticipantId",
    "RosterTemplateId",
    "AgentTemplateId",
    "ModeTemplateId",
]
