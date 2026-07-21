# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for roster-template management (CRUD + apply-to-conversation).

A **roster template** (``docs/70-multi-agent/multi-agent-conversation-design.md``
"内置预设模板 + 用户自定义参与者") is a named, reusable bundle of discussion
role definitions (a "team": Architect / Developer / Tester ...).  It lets a user
preview + import a roster instead of rebuilding it from scratch in every new
conversation — PURE V2 enhancement (V1 has no multi-agent discussion at all;
细则 4-bis受保护).

Two orthogonal storage backbones:

* the **library** — :class:`~qai.chat.domain.roster_template.RosterTemplate`
  rows (``chat_roster_template``, migration 038), conversation-INDEPENDENT, each
  holding its member role definitions; built-ins (``is_builtin``) are
  factory-seeded presets and treated as read-only here; and
* the **conversation roster** — when a template is *applied*, one
  :class:`~qai.chat.domain.participant.Participant` (``kind=NAMED_AGENT``) is
  instantiated per :class:`RosterTemplateMember` on the target conversation
  (reusing the exact ``Participant.create`` path the participant-CRUD use cases
  use, so behaviour is identical to manually adding each role).

This module groups the thin application use cases the REST layer composes its
roster-template endpoints from.  They are deliberately small wrappers over the
:class:`RosterTemplateRepositoryPort` / :class:`ParticipantRepositoryPort` /
:class:`ConversationRepositoryPort` + the domain mutators, so the interfaces
layer stays thin (``interfaces-stays-thin`` contract).

Layering: ``application/use_cases`` — depends only on ``application.ports``
Protocols + ``domain`` + the platform ``Clock`` / ``IdGenerator``.  Imports no
adapters / apps / interfaces, so ``layered-chat`` / ``context-isolation`` hold.
These use cases sit ALONGSIDE the existing participant / discussion use cases
and never touch the single-agent conversation paths (judgement 2 unchanged).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qai.chat.application.ports import (
    ConversationRepositoryPort,
    ModeTemplateRepositoryPort,
    ParticipantRepositoryPort,
    RosterTemplateRepositoryPort,
)
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.errors import (
    ConversationNotFoundError,
    RosterTemplateNotFoundError,
)
from qai.chat.domain.ids import (
    ConversationId,
    ModeTemplateId,
    ParticipantId,
    RosterTemplateId,
)
from qai.chat.domain.participant import Participant, ParticipantKind
from qai.chat.domain.roster_template import RosterTemplate, RosterTemplateMember
from qai.platform.errors import ValidationError
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock

__all__ = [
    "ApplyRosterTemplateInput",
    "ApplyRosterTemplateResult",
    "ApplyRosterTemplateUseCase",
    "CloneRosterTemplateInput",
    "CloneRosterTemplateUseCase",
    "CreateRosterTemplateInput",
    "CreateRosterTemplateUseCase",
    "DeleteRosterTemplateInput",
    "DeleteRosterTemplateUseCase",
    "ListRosterTemplatesUseCase",
    "ResetRosterTemplateInput",
    "ResetRosterTemplateNotCloneError",
    "ResetRosterTemplateUseCase",
    "RosterMemberInput",
    "RosterTemplateReadOnlyError",
    "UpdateRosterTemplateInput",
    "UpdateRosterTemplateUseCase",
]

_log = get_logger(__name__)

# Sentinel distinguishing "field omitted from a PATCH" (leave unchanged) from
# "field explicitly set" (replace). Mirrors participant_management._UNSET.
_UNSET: Any = object()


class RosterTemplateReadOnlyError(ValidationError):
    """Raised when a write is attempted against a built-in (preset) template."""

    default_code = "chat.roster_template_read_only"

    def __init__(self, template_id: str) -> None:
        super().__init__(
            self.default_code,
            f"roster template {template_id!r} is a built-in preset and cannot "
            "be modified or deleted",
        )


class ResetRosterTemplateNotCloneError(ValidationError):
    """Raised when reset is attempted on a template that is not a user clone.

    Only a user clone (``is_builtin == False`` AND ``cloned_from_id`` set) can
    be reset — reset restores the copy's business fields from its source.
    """

    default_code = "chat.roster_template_not_clone"

    def __init__(self, template_id: str) -> None:
        super().__init__(
            self.default_code,
            f"roster template {template_id!r} is not a user clone of a preset "
            "and cannot be reset (only cloned copies can be reset)",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RosterMemberInput:
    """A single role definition supplied by the request layer."""

    display_name: str = ""
    model_id: str | None = None
    persona: str | None = None
    config: dict[str, Any] | None = None


def _to_member(member: RosterMemberInput) -> RosterTemplateMember:
    """Map a request member to a domain value object (domain validates shape)."""
    return RosterTemplateMember(
        display_name=member.display_name,
        model_id=member.model_id,
        persona=member.persona,
        config=dict(member.config) if member.config else None,
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
# List roster templates
# ---------------------------------------------------------------------------
class ListRosterTemplatesUseCase:
    """List every roster template (built-in presets first, then user saved)."""

    def __init__(self, *, templates: RosterTemplateRepositoryPort) -> None:
        self._templates = templates

    async def execute(self) -> tuple[RosterTemplate, ...]:
        return await self._templates.list_all()


# ---------------------------------------------------------------------------
# Create roster template
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CreateRosterTemplateInput:
    name: str = ""
    description: str = ""
    members: tuple[RosterMemberInput, ...] = field(default_factory=tuple)
    #: Optional default collaboration mode bound to the team (§26.9 / §27 tail).
    default_mode_id: str | None = None


class CreateRosterTemplateUseCase:
    """Create a brand-new user roster template (``is_builtin`` is always False).

    Built-in presets are seeded by the install pipeline only; the API never
    mints built-ins.
    """

    def __init__(
        self,
        *,
        templates: RosterTemplateRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._templates = templates
        self._clock = clock
        self._ids = ids

    async def execute(self, request: CreateRosterTemplateInput) -> RosterTemplate:
        template = RosterTemplate.create(
            template_id=RosterTemplateId.generate(self._ids),
            now=self._clock.now(),
            name=request.name,
            description=request.description,
            members=tuple(_to_member(m) for m in request.members),
            is_builtin=False,
            default_mode_id=request.default_mode_id,
        )
        await self._templates.save(template)
        return template


# ---------------------------------------------------------------------------
# Update roster template (PATCH semantics)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateRosterTemplateInput:
    template_id: RosterTemplateId
    #: Each field defaults to ``_UNSET`` = "omitted, leave unchanged".
    name: Any = _UNSET
    description: Any = _UNSET
    #: When provided, REPLACES the full member list (tuple of RosterMemberInput).
    members: Any = _UNSET
    #: When provided (str or None), sets/clears the bound default mode.
    default_mode_id: Any = _UNSET


class UpdateRosterTemplateUseCase:
    """Patch a user roster template's name / description / members.

    Built-in presets are read-only (``RosterTemplateReadOnlyError``).
    """

    def __init__(
        self,
        *,
        templates: RosterTemplateRepositoryPort,
        clock: Clock,
    ) -> None:
        self._templates = templates
        self._clock = clock

    async def execute(self, request: UpdateRosterTemplateInput) -> RosterTemplate:
        template = await self._templates.get(request.template_id)
        if template.is_builtin:
            raise RosterTemplateReadOnlyError(request.template_id.value)
        now = self._clock.now()
        if request.name is not _UNSET:
            template.rename(request.name, now=now)
        if request.description is not _UNSET:
            template.set_description(request.description, now=now)
        if request.members is not _UNSET:
            template.set_members(
                tuple(_to_member(m) for m in request.members),
                now=now,
            )
        if request.default_mode_id is not _UNSET:
            template.set_default_mode_id(request.default_mode_id, now=now)
        await self._templates.save(template)
        return template


# ---------------------------------------------------------------------------
# Delete roster template
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteRosterTemplateInput:
    template_id: RosterTemplateId


class DeleteRosterTemplateUseCase:
    """Delete a user roster template; built-in presets cannot be deleted."""

    def __init__(self, *, templates: RosterTemplateRepositoryPort) -> None:
        self._templates = templates

    async def execute(self, request: DeleteRosterTemplateInput) -> None:
        template = await self._templates.find(request.template_id)
        if template is None:
            raise RosterTemplateNotFoundError(request.template_id.value)
        if template.is_builtin:
            raise RosterTemplateReadOnlyError(request.template_id.value)
        await self._templates.delete(request.template_id)


# ---------------------------------------------------------------------------
# Clone roster template (copy any template into a new user record)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CloneRosterTemplateInput:
    template_id: RosterTemplateId


class CloneRosterTemplateUseCase:
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
        templates: RosterTemplateRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._templates = templates
        self._clock = clock
        self._ids = ids

    async def execute(
        self, request: CloneRosterTemplateInput
    ) -> RosterTemplate:
        src = await self._templates.get(request.template_id)
        clone = RosterTemplate.create(
            template_id=RosterTemplateId.generate(self._ids),
            now=self._clock.now(),
            name=f"{src.name} (副本)",
            description=src.description,
            members=tuple(src.members),
            is_builtin=False,
            default_mode_id=src.default_mode_id,
            cloned_from_id=src.id.value,
        )
        await self._templates.save(clone)
        _log.info(
            "chat.roster_template.cloned",
            source_id=src.id.value,
            clone_id=clone.id.value,
        )
        return clone


# ---------------------------------------------------------------------------
# Reset roster template (restore a clone's fields from its source, in place)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ResetRosterTemplateInput:
    template_id: RosterTemplateId


class ResetRosterTemplateUseCase:
    """Restore a cloned copy's business fields from its source, in place.

    Only a user clone (``is_builtin == False`` AND ``cloned_from_id`` set) may
    be reset (``ResetRosterTemplateNotCloneError`` otherwise). The copy keeps
    its OWN id / created_at / cloned_from_id (no delete/recreate → no dangling
    reference); only the business fields are overwritten and ``updated_at`` is
    bumped.
    """

    def __init__(
        self,
        *,
        templates: RosterTemplateRepositoryPort,
        clock: Clock,
    ) -> None:
        self._templates = templates
        self._clock = clock

    async def execute(
        self, request: ResetRosterTemplateInput
    ) -> RosterTemplate:
        copy = await self._templates.get(request.template_id)
        if copy.is_builtin or not copy.cloned_from_id:
            raise ResetRosterTemplateNotCloneError(request.template_id.value)
        src = await self._templates.get(
            RosterTemplateId.of(copy.cloned_from_id),
        )
        now = self._clock.now()
        copy.rename(src.name, now=now)
        copy.set_description(src.description, now=now)
        copy.set_members(tuple(src.members), now=now)
        copy.set_default_mode_id(src.default_mode_id, now=now)
        await self._templates.save(copy)
        _log.info(
            "chat.roster_template.reset",
            template_id=copy.id.value,
            source_id=src.id.value,
        )
        return copy


# ---------------------------------------------------------------------------
# Apply roster template to a conversation
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyRosterTemplateInput:
    template_id: RosterTemplateId
    conversation_id: ConversationId


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyRosterTemplateResult:
    """Outcome of applying a roster template to a conversation.

    ``applied_mode_id`` / ``applied_mode_name`` are non-``None`` only when the
    team carried a ``default_mode_id`` that resolved to an existing mode and was
    selected on the conversation (so the UI can toast "mode also applied").
    """

    participants: tuple[Participant, ...]
    applied_mode_id: str | None = None
    applied_mode_name: str | None = None


class ApplyRosterTemplateUseCase:
    """Instantiate a roster template's members as named agents on a conversation.

    Each :class:`RosterTemplateMember` becomes a fresh ``kind=NAMED_AGENT``
    :class:`Participant` (a new :class:`ParticipantId` per member — template
    members have no per-conversation identity) created via the same
    ``Participant.create`` path the manual participant-CRUD use case uses.  The
    target conversation must exist; the template must exist.

    If the template carries an optional ``default_mode_id`` (§26.9 / §27 tail)
    that resolves to an existing collaboration mode, this also selects that mode
    on the conversation (``meta["discussion"]["selected_mode_id"]``, tail-
    appended §3.1) — the same write path :class:`ApplyModeTemplateUseCase` uses.
    A ``None`` / missing / stale ``default_mode_id`` leaves the conversation's
    current mode untouched (zero-regression: existing apply behaviour is
    byte-for-byte unchanged when no mode is bound).

    NOTE: this ADDS the template's roles to the conversation; it does not clear
    pre-existing participants (the caller decides whether to apply onto a fresh
    conversation or an existing one — applying onto a brand-new conversation is
    the "reuse a team for a new task" flow with a naturally clean discussion
    state).
    """

    def __init__(
        self,
        *,
        templates: RosterTemplateRepositoryPort,
        participants: ParticipantRepositoryPort,
        conversations: ConversationRepositoryPort,
        mode_templates: ModeTemplateRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._templates = templates
        self._participants = participants
        self._conversations = conversations
        self._mode_templates = mode_templates
        self._clock = clock
        self._ids = ids

    async def execute(
        self, request: ApplyRosterTemplateInput
    ) -> ApplyRosterTemplateResult:
        template = await self._templates.get(request.template_id)
        conv = await _require_conversation(
            self._conversations, request.conversation_id
        )
        created: list[Participant] = []
        for member_index, member in enumerate(template.members):
            participant = Participant.create(
                participant_id=ParticipantId.generate(self._ids),
                conversation_id=request.conversation_id,
                kind=ParticipantKind.NAMED_AGENT,
                now=self._clock.now(),
                display_name=member.display_name,
                model_id=member.model_id,
                persona=member.persona,
                config=dict(member.config) if member.config else None,
                # Provenance for runtime i18n persona override (migration 056).
                # A team member has no id of its own, so we store a COMPOSITE
                # key ``"<roster_id>#<member_index>"`` (e.g. ``builtin-arch-
                # dev-test#0``). At runtime the orchestrator splits on ``#`` to
                # recover (roster_id, index) and looks up
                # ``members_i18n[locale][index].persona`` for the translation.
                # The index is the member's ORDER position in
                # ``template.members`` — which is exactly the order the built-in
                # seed's ``members_i18n[locale]`` array uses, so they align 1:1.
                template_id=f"{template.id.value}#{member_index}",
            )
            await self._participants.save(participant)
            created.append(participant)

        applied_mode_id: str | None = None
        applied_mode_name: str | None = None
        if template.default_mode_id:
            # Tolerate a stale/deleted mode id: only select it when it still
            # resolves to an existing mode (no hard FK by design — §041 migration).
            mode = await self._mode_templates.find(
                ModeTemplateId.of(template.default_mode_id),
            )
            if mode is not None:
                discussion = dict(conv.discussion or {})
                discussion["selected_mode_id"] = mode.id.value
                discussion["mode_selection_policy"] = "manual"
                conv.set_discussion(discussion, now=self._clock.now())
                await self._conversations.save(conv)
                applied_mode_id = mode.id.value
                applied_mode_name = mode.name

        _log.info(
            "chat.roster_template.applied",
            template_id=request.template_id.value,
            conversation_id=request.conversation_id.value,
            members_added=len(created),
            applied_mode_id=applied_mode_id,
        )
        return ApplyRosterTemplateResult(
            participants=tuple(created),
            applied_mode_id=applied_mode_id,
            applied_mode_name=applied_mode_name,
        )
