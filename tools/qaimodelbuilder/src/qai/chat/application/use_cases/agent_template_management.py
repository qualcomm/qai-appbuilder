# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for agent-template management (CRUD + apply-to-conversation).

An **agent template** (``docs/70-multi-agent/multi-agent-conversation-design.md``
§27) is a named, reusable definition of a SINGLE discussion role (an "agent":
资深架构师 / 全栈开发 / 严谨测试 …).  It is the smallest reusable unit in the
three-tier template system (single role → team → mode), orthogonal to the
team-level :class:`RosterTemplate`.  It lets a user preview + import one role
instead of re-typing it in every new conversation / team — PURE V2 enhancement
(V1 has no multi-agent discussion at all; 细则 4-bis 受保护).

Two orthogonal storage backbones (mirrors roster_template_management):

* the **library** — :class:`~qai.chat.domain.agent_template.AgentTemplate` rows
  (``chat_agent_template``, migration 039), conversation-INDEPENDENT, each
  holding one role definition; built-ins (``is_builtin``) are factory-seeded
  presets and treated as read-only here; and
* the **conversation roster** — when a template is *applied*, one
  :class:`~qai.chat.domain.participant.Participant` (``kind=NAMED_AGENT``) is
  instantiated on the target conversation (reusing the exact
  ``Participant.create`` path the participant-CRUD use cases use).

Layering: ``application/use_cases`` — depends only on ``application.ports``
Protocols + ``domain`` + the platform ``Clock`` / ``IdGenerator``.  Imports no
adapters / apps / interfaces, so ``layered-chat`` / ``context-isolation`` hold.
These use cases sit ALONGSIDE the existing participant / discussion / roster
use cases and never touch the single-agent conversation paths (judgement 2
unchanged).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qai.chat.application.ports import (
    AgentTemplateRepositoryPort,
    ConversationRepositoryPort,
    ParticipantRepositoryPort,
)
from qai.chat.domain.agent_template import AgentTemplate
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.errors import (
    AgentTemplateNotFoundError,
    ConversationNotFoundError,
)
from qai.chat.domain.ids import (
    AgentTemplateId,
    ConversationId,
    ParticipantId,
)
from qai.chat.domain.participant import Participant, ParticipantKind
from qai.platform.errors import ValidationError
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock

__all__ = [
    "AgentTemplateReadOnlyError",
    "ApplyAgentTemplateInput",
    "ApplyAgentTemplateUseCase",
    "CloneAgentTemplateInput",
    "CloneAgentTemplateUseCase",
    "CreateAgentTemplateInput",
    "CreateAgentTemplateUseCase",
    "DeleteAgentTemplateInput",
    "DeleteAgentTemplateUseCase",
    "ListAgentTemplatesUseCase",
    "ResetAgentTemplateInput",
    "ResetAgentTemplateNotCloneError",
    "ResetAgentTemplateUseCase",
    "UpdateAgentTemplateInput",
    "UpdateAgentTemplateUseCase",
]

_log = get_logger(__name__)

# Sentinel distinguishing "field omitted from a PATCH" (leave unchanged) from
# "field explicitly set" (replace). Mirrors roster_template_management._UNSET.
_UNSET: Any = object()


class AgentTemplateReadOnlyError(ValidationError):
    """Raised when a write is attempted against a built-in (preset) template."""

    default_code = "chat.agent_template_read_only"

    def __init__(self, template_id: str) -> None:
        super().__init__(
            self.default_code,
            f"agent template {template_id!r} is a built-in preset and cannot "
            "be modified or deleted",
        )


class ResetAgentTemplateNotCloneError(ValidationError):
    """Raised when reset is attempted on a template that is not a user clone.

    Only a user clone (``is_builtin == False`` AND ``cloned_from_id`` set) can
    be reset — reset restores the copy's business fields from its source. A
    built-in preset (the original, read-only) or an original user template
    (never cloned, so no source to restore from) has nothing to reset to.
    """

    default_code = "chat.agent_template_not_clone"

    def __init__(self, template_id: str) -> None:
        super().__init__(
            self.default_code,
            f"agent template {template_id!r} is not a user clone of a preset "
            "and cannot be reset (only cloned copies can be reset)",
        )


async def _require_conversation(
    conversations: ConversationRepositoryPort,
    conversation_id: ConversationId,
) -> Conversation:
    conv = await conversations.find(conversation_id)
    if conv is None:
        raise ConversationNotFoundError(conversation_id.value)
    return conv


# ---------------------------------------------------------------------------
# List agent templates
# ---------------------------------------------------------------------------
class ListAgentTemplatesUseCase:
    """List every agent template (built-in presets first, then user saved)."""

    def __init__(self, *, templates: AgentTemplateRepositoryPort) -> None:
        self._templates = templates

    async def execute(self) -> tuple[AgentTemplate, ...]:
        return await self._templates.list_all()


# ---------------------------------------------------------------------------
# Create agent template
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CreateAgentTemplateInput:
    name: str = ""
    description: str = ""
    display_name: str = ""
    model_id: str | None = None
    persona: str | None = None
    config: dict[str, Any] | None = None


class CreateAgentTemplateUseCase:
    """Create a brand-new user agent template (``is_builtin`` is always False).

    Built-in presets are seeded by the install pipeline only; the API never
    mints built-ins.
    """

    def __init__(
        self,
        *,
        templates: AgentTemplateRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._templates = templates
        self._clock = clock
        self._ids = ids

    async def execute(self, request: CreateAgentTemplateInput) -> AgentTemplate:
        template = AgentTemplate.create(
            template_id=AgentTemplateId.generate(self._ids),
            now=self._clock.now(),
            name=request.name,
            description=request.description,
            display_name=request.display_name,
            model_id=request.model_id,
            persona=request.persona,
            config=dict(request.config) if request.config else None,
            is_builtin=False,
        )
        await self._templates.save(template)
        return template


# ---------------------------------------------------------------------------
# Update agent template (PATCH semantics)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateAgentTemplateInput:
    template_id: AgentTemplateId
    #: Each field defaults to ``_UNSET`` = "omitted, leave unchanged".
    name: Any = _UNSET
    description: Any = _UNSET
    display_name: Any = _UNSET
    model_id: Any = _UNSET
    persona: Any = _UNSET
    config: Any = _UNSET


class UpdateAgentTemplateUseCase:
    """Patch a user agent template's metadata + role definition.

    Built-in presets are read-only (``AgentTemplateReadOnlyError``).  The role
    fields (display_name / model_id / persona / config) are patched together
    via :meth:`AgentTemplate.set_role` when any of them is sent; metadata
    (name / description) patches independently.
    """

    def __init__(
        self,
        *,
        templates: AgentTemplateRepositoryPort,
        clock: Clock,
    ) -> None:
        self._templates = templates
        self._clock = clock

    async def execute(self, request: UpdateAgentTemplateInput) -> AgentTemplate:
        template = await self._templates.get(request.template_id)
        if template.is_builtin:
            raise AgentTemplateReadOnlyError(request.template_id.value)
        now = self._clock.now()
        if request.name is not _UNSET:
            template.rename(request.name, now=now)
        if request.description is not _UNSET:
            template.set_description(request.description, now=now)
        role_fields_sent = any(
            f is not _UNSET
            for f in (
                request.display_name,
                request.model_id,
                request.persona,
                request.config,
            )
        )
        if role_fields_sent:
            template.set_role(
                now=now,
                display_name=(
                    template.display_name
                    if request.display_name is _UNSET
                    else request.display_name
                ),
                model_id=(
                    template.model_id
                    if request.model_id is _UNSET
                    else request.model_id
                ),
                persona=(
                    template.persona
                    if request.persona is _UNSET
                    else request.persona
                ),
                config=(
                    template.config
                    if request.config is _UNSET
                    else (dict(request.config) if request.config else None)
                ),
            )
        await self._templates.save(template)
        return template


# ---------------------------------------------------------------------------
# Delete agent template
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteAgentTemplateInput:
    template_id: AgentTemplateId


class DeleteAgentTemplateUseCase:
    """Delete a user agent template; built-in presets cannot be deleted."""

    def __init__(self, *, templates: AgentTemplateRepositoryPort) -> None:
        self._templates = templates

    async def execute(self, request: DeleteAgentTemplateInput) -> None:
        template = await self._templates.find(request.template_id)
        if template is None:
            raise AgentTemplateNotFoundError(request.template_id.value)
        if template.is_builtin:
            raise AgentTemplateReadOnlyError(request.template_id.value)
        await self._templates.delete(request.template_id)


# ---------------------------------------------------------------------------
# Clone agent template (copy any template into a new user record)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CloneAgentTemplateInput:
    template_id: AgentTemplateId


class CloneAgentTemplateUseCase:
    """Clone any template (incl. a built-in preset) into a new user record.

    The clone copies ALL business fields of the source, gets a brand-new id,
    is always ``is_builtin=False``, records ``cloned_from_id=source.id``, and
    gets a "(副本)" name suffix. Cloning a built-in preset is the core
    "edit a preset" flow — the preset stays read-only; the user edits the copy.
    NO ``is_builtin`` gate here (cloning a preset is explicitly allowed).
    """

    def __init__(
        self,
        *,
        templates: AgentTemplateRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._templates = templates
        self._clock = clock
        self._ids = ids

    async def execute(self, request: CloneAgentTemplateInput) -> AgentTemplate:
        src = await self._templates.get(request.template_id)
        clone = AgentTemplate.create(
            template_id=AgentTemplateId.generate(self._ids),
            now=self._clock.now(),
            name=f"{src.name} (副本)",
            description=src.description,
            display_name=src.display_name,
            model_id=src.model_id,
            persona=src.persona,
            config=dict(src.config) if src.config else None,
            is_builtin=False,
            cloned_from_id=src.id.value,
        )
        await self._templates.save(clone)
        _log.info(
            "chat.agent_template.cloned",
            source_id=src.id.value,
            clone_id=clone.id.value,
        )
        return clone


# ---------------------------------------------------------------------------
# Reset agent template (restore a clone's fields from its source, in place)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ResetAgentTemplateInput:
    template_id: AgentTemplateId


class ResetAgentTemplateUseCase:
    """Restore a cloned copy's business fields from its source, in place.

    Only a user clone (``is_builtin == False`` AND ``cloned_from_id`` set) may
    be reset (``ResetAgentTemplateNotCloneError`` otherwise). The copy keeps its
    OWN id / created_at / cloned_from_id (no delete/recreate → no dangling
    reference); only the business fields are overwritten from the source and
    ``updated_at`` is bumped.
    """

    def __init__(
        self,
        *,
        templates: AgentTemplateRepositoryPort,
        clock: Clock,
    ) -> None:
        self._templates = templates
        self._clock = clock

    async def execute(self, request: ResetAgentTemplateInput) -> AgentTemplate:
        copy = await self._templates.get(request.template_id)
        if copy.is_builtin or not copy.cloned_from_id:
            raise ResetAgentTemplateNotCloneError(request.template_id.value)
        src = await self._templates.get(
            AgentTemplateId.of(copy.cloned_from_id),
        )
        now = self._clock.now()
        copy.rename(src.name, now=now)
        copy.set_description(src.description, now=now)
        copy.set_role(
            now=now,
            display_name=src.display_name,
            model_id=src.model_id,
            persona=src.persona,
            config=dict(src.config) if src.config else None,
        )
        await self._templates.save(copy)
        _log.info(
            "chat.agent_template.reset",
            template_id=copy.id.value,
            source_id=src.id.value,
        )
        return copy


# ---------------------------------------------------------------------------
# Apply agent template to a conversation
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyAgentTemplateInput:
    template_id: AgentTemplateId
    conversation_id: ConversationId


class ApplyAgentTemplateUseCase:
    """Instantiate an agent template as one named agent on a conversation.

    The :class:`AgentTemplate` becomes a fresh ``kind=NAMED_AGENT``
    :class:`Participant` (a new :class:`ParticipantId`) created via the same
    ``Participant.create`` path the manual participant-CRUD use case uses.  The
    target conversation must exist; the template must exist.

    NOTE: this ADDS one role to the conversation; it does not clear pre-existing
    participants (the caller decides whether to apply onto a fresh conversation
    or an existing one).
    """

    def __init__(
        self,
        *,
        templates: AgentTemplateRepositoryPort,
        participants: ParticipantRepositoryPort,
        conversations: ConversationRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._templates = templates
        self._participants = participants
        self._conversations = conversations
        self._clock = clock
        self._ids = ids

    async def execute(self, request: ApplyAgentTemplateInput) -> Participant:
        template = await self._templates.get(request.template_id)
        await _require_conversation(self._conversations, request.conversation_id)
        participant = Participant.create(
            participant_id=ParticipantId.generate(self._ids),
            conversation_id=request.conversation_id,
            kind=ParticipantKind.NAMED_AGENT,
            now=self._clock.now(),
            display_name=template.display_name,
            model_id=template.model_id,
            persona=template.persona,
            config=dict(template.config) if template.config else None,
            # Provenance for runtime i18n persona override (migration 056):
            # a single-role import stores the bare agent template id so the
            # discussion orchestrator can re-resolve persona by (id + locale).
            template_id=template.id.value,
        )
        await self._participants.save(participant)
        _log.info(
            "chat.agent_template.applied",
            template_id=request.template_id.value,
            conversation_id=request.conversation_id.value,
            participant_id=participant.id.value,
        )
        return participant
