# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for collaboration-mode template management (CRUD + apply).

A **mode template** (``docs/70-multi-agent/multi-agent-conversation-design.md``
§26 / §27) is a named collaboration mode ("怎么协作": 讨论 / 评审 / 辩论 / 实施 /
custom) — the third tier of the three-tier template system, orthogonal to the
team-level :class:`RosterTemplate`.  It is identity / framing / tool_policy /
flow_policy (the V1 subset of §26.1; sandbox / execution-time confirmation are
out of V1 scope — the discussion runtime has no such gate, see
``mode_template`` module docstring).

Two orthogonal concerns (mirrors roster/agent template management):

* the **library** — :class:`~qai.chat.domain.mode_template.ModeTemplate` rows
  (``chat_mode_template``, migration 040), conversation-INDEPENDENT; built-ins
  (``is_builtin``) are factory-seeded presets, read-only here; and
* the **conversation's selected mode** — "applying" a mode to a conversation
  sets ``meta["discussion"]["selected_mode_id"]`` (+ ``mode_selection_policy``)
  via the conversation aggregate (§3.1: tail-appended meta key, no new table).

Selecting a mode must be USER-EXPLICIT (§26.4 / §26.10#4) — the classifier
never silently switches modes; the apply use case is the explicit user action,
so it simply records the selection.

Layering: ``application/use_cases`` — depends only on ``application.ports``
Protocols + ``domain`` + the platform ``Clock`` / ``IdGenerator``.  Imports no
adapters / apps / interfaces, so ``layered-chat`` / ``context-isolation`` hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qai.chat.application.ports import (
    ConversationRepositoryPort,
    ModeTemplateRepositoryPort,
)
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.errors import (
    ConversationNotFoundError,
    ModeTemplateNotFoundError,
)
from qai.chat.domain.ids import ConversationId, ModeTemplateId
from qai.chat.domain.mode_template import (
    ModeFlowPolicy,
    ModeHardConstraints,
    ModeTemplate,
    ModeToolPolicy,
)
from qai.platform.errors import ValidationError
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock

__all__ = [
    "ApplyModeTemplateInput",
    "ApplyModeTemplateResult",
    "ApplyModeTemplateUseCase",
    "CloneModeTemplateInput",
    "CloneModeTemplateUseCase",
    "CountModeTemplateUsageUseCase",
    "CreateModeTemplateInput",
    "CreateModeTemplateUseCase",
    "DeleteModeTemplateInput",
    "DeleteModeTemplateUseCase",
    "ListModeTemplatesUseCase",
    "ModeTemplateReadOnlyError",
    "ResetModeTemplateInput",
    "ResetModeTemplateNotCloneError",
    "ResetModeTemplateUseCase",
    "UpdateModeTemplateInput",
    "UpdateModeTemplateUseCase",
]

_log = get_logger(__name__)

# Sentinel distinguishing "field omitted from a PATCH" (leave unchanged) from
# "field explicitly set" (replace). Mirrors roster_template_management._UNSET.
_UNSET: Any = object()

#: Selection policy states (design §26.2 / §26.4). ``manual`` = user picked it
#: here (explicit), so the classifier may not override it.
_VALID_SELECTION_POLICIES: frozenset[str] = frozenset(
    {"auto", "manual", "locked", "suggested"},
)


class ModeTemplateReadOnlyError(ValidationError):
    """Raised when a write is attempted against a built-in (preset) mode."""

    default_code = "chat.mode_template_read_only"

    def __init__(self, template_id: str) -> None:
        super().__init__(
            self.default_code,
            f"mode template {template_id!r} is a built-in preset and cannot "
            "be modified or deleted",
        )


class ResetModeTemplateNotCloneError(ValidationError):
    """Raised when reset is attempted on a mode that is not a user clone.

    Only a user clone (``is_builtin == False`` AND ``cloned_from_id`` set) can
    be reset — reset restores the copy's business fields from its source.
    """

    default_code = "chat.mode_template_not_clone"

    def __init__(self, template_id: str) -> None:
        super().__init__(
            self.default_code,
            f"mode template {template_id!r} is not a user clone of a preset "
            "and cannot be reset (only cloned copies can be reset)",
        )


def _policies_from_inputs(
    *,
    tool_policy: dict[str, Any] | None,
    flow_policy: dict[str, Any] | None,
) -> tuple[ModeToolPolicy, ModeFlowPolicy]:
    return (
        ModeToolPolicy.from_dict(tool_policy or {}),
        ModeFlowPolicy.from_dict(flow_policy or {}),
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
# List mode templates
# ---------------------------------------------------------------------------
class ListModeTemplatesUseCase:
    """List every mode template (built-in presets first, then user saved)."""

    def __init__(self, *, templates: ModeTemplateRepositoryPort) -> None:
        self._templates = templates

    async def execute(self) -> tuple[ModeTemplate, ...]:
        return await self._templates.list_all()


# ---------------------------------------------------------------------------
# Create mode template
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CreateModeTemplateInput:
    name: str = ""
    description: str = ""
    framing: str = ""
    tool_policy: dict[str, Any] | None = None
    flow_policy: dict[str, Any] | None = None
    hard_constraints: dict[str, Any] | None = None


class CreateModeTemplateUseCase:
    """Create a brand-new user mode template (``is_builtin`` always False).

    Built-in presets are seeded by the install pipeline only; the API never
    mints built-ins.
    """

    def __init__(
        self,
        *,
        templates: ModeTemplateRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._templates = templates
        self._clock = clock
        self._ids = ids

    async def execute(self, request: CreateModeTemplateInput) -> ModeTemplate:
        tool_pol, flow_pol = _policies_from_inputs(
            tool_policy=request.tool_policy,
            flow_policy=request.flow_policy,
        )
        template = ModeTemplate.create(
            template_id=ModeTemplateId.generate(self._ids),
            now=self._clock.now(),
            name=request.name,
            description=request.description,
            framing=request.framing,
            tool_policy=tool_pol,
            flow_policy=flow_pol,
            hard_constraints=ModeHardConstraints.from_dict(
                request.hard_constraints or {},
            ),
            is_builtin=False,
        )
        await self._templates.save(template)
        return template


# ---------------------------------------------------------------------------
# Update mode template (PATCH semantics)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateModeTemplateInput:
    template_id: ModeTemplateId
    name: Any = _UNSET
    description: Any = _UNSET
    framing: Any = _UNSET
    tool_policy: Any = _UNSET
    flow_policy: Any = _UNSET
    hard_constraints: Any = _UNSET


class UpdateModeTemplateUseCase:
    """Patch a user mode template; built-ins are read-only."""

    def __init__(
        self,
        *,
        templates: ModeTemplateRepositoryPort,
        clock: Clock,
    ) -> None:
        self._templates = templates
        self._clock = clock

    async def execute(self, request: UpdateModeTemplateInput) -> ModeTemplate:
        template = await self._templates.get(request.template_id)
        if template.is_builtin:
            raise ModeTemplateReadOnlyError(request.template_id.value)
        now = self._clock.now()
        if request.name is not _UNSET:
            template.rename(request.name, now=now)
        if request.description is not _UNSET:
            template.set_description(request.description, now=now)
        if request.framing is not _UNSET:
            template.set_framing(request.framing, now=now)
        if request.tool_policy is not _UNSET:
            template.set_tool_policy(
                ModeToolPolicy.from_dict(request.tool_policy or {}),
                now=now,
            )
        if request.flow_policy is not _UNSET:
            template.set_flow_policy(
                ModeFlowPolicy.from_dict(request.flow_policy or {}),
                now=now,
            )
        if request.hard_constraints is not _UNSET:
            template.set_hard_constraints(
                ModeHardConstraints.from_dict(request.hard_constraints or {}),
                now=now,
            )
        await self._templates.save(template)
        return template


# ---------------------------------------------------------------------------
# Delete mode template
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteModeTemplateInput:
    template_id: ModeTemplateId


class DeleteModeTemplateUseCase:
    """Delete a user mode template; built-in presets cannot be deleted.

    Decision 7: deleting a mode that conversations still select reverts those
    conversations to the sentinel ("跟随默认（不指定模式）") by clearing their
    ``meta["discussion"]["selected_mode_id"]`` — so a stale id never dangles.
    The confirm dialog (driven by :class:`CountModeTemplateUsageUseCase`) is a
    front-end concern; this use case performs the cleanup unconditionally after
    the delete so the truth in the DB is always consistent (State-Truth-First).
    """

    def __init__(
        self,
        *,
        templates: ModeTemplateRepositoryPort,
        conversations: ConversationRepositoryPort,
        clock: Clock,
    ) -> None:
        self._templates = templates
        self._conversations = conversations
        self._clock = clock

    async def execute(self, request: DeleteModeTemplateInput) -> None:
        template = await self._templates.find(request.template_id)
        if template is None:
            raise ModeTemplateNotFoundError(request.template_id.value)
        if template.is_builtin:
            raise ModeTemplateReadOnlyError(request.template_id.value)
        await self._templates.delete(request.template_id)
        # Revert every conversation that still selects this mode to the sentinel
        # (decision 7): clear selected_mode_id + mode_selection_policy so the UI
        # shows "跟随默认（不指定模式）" and the runtime resolves no mode.
        affected = await self._conversations.find_ids_by_selected_mode(
            request.template_id.value,
        )
        if not affected:
            return
        now = self._clock.now()
        for conv_id in affected:
            conv = await self._conversations.find(conv_id)
            if conv is None:
                continue
            discussion = dict(conv.discussion or {})
            discussion.pop("selected_mode_id", None)
            discussion.pop("mode_selection_policy", None)
            conv.set_discussion(discussion, now=now)
            await self._conversations.save(conv)
        _log.info(
            "chat.mode_template.deleted_with_cleanup",
            template_id=request.template_id.value,
            reverted_conversations=len(affected),
        )


# ---------------------------------------------------------------------------
# Count conversations using a mode (delete-confirm dialog support)
# ---------------------------------------------------------------------------
class CountModeTemplateUsageUseCase:
    """Count conversations currently selecting a given mode (decision 7).

    Powers the delete-confirm dialog: when ``count > 0`` the front-end warns the
    user how many conversations will be reverted to the sentinel before they
    confirm the delete. A pure read (no mutation).
    """

    def __init__(self, *, conversations: ConversationRepositoryPort) -> None:
        self._conversations = conversations

    async def execute(self, template_id: ModeTemplateId) -> int:
        ids = await self._conversations.find_ids_by_selected_mode(
            template_id.value,
        )
        return len(ids)


# ---------------------------------------------------------------------------
# Clone mode template (copy any template into a new user record)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CloneModeTemplateInput:
    template_id: ModeTemplateId


class CloneModeTemplateUseCase:
    """Clone any mode (incl. a built-in preset) into a new user record.

    The clone copies ALL business fields of the source, gets a brand-new id,
    is always ``is_builtin=False``, records ``cloned_from_id=source.id``, and
    gets a "(副本)" name suffix. Cloning a built-in preset is the core
    "edit a preset" flow — the preset stays read-only; the user edits the copy.
    NO ``is_builtin`` gate here (cloning a preset is explicitly allowed).
    """

    def __init__(
        self,
        *,
        templates: ModeTemplateRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._templates = templates
        self._clock = clock
        self._ids = ids

    async def execute(self, request: CloneModeTemplateInput) -> ModeTemplate:
        src = await self._templates.get(request.template_id)
        clone = ModeTemplate.create(
            template_id=ModeTemplateId.generate(self._ids),
            now=self._clock.now(),
            name=f"{src.name} (副本)",
            description=src.description,
            framing=src.framing,
            tool_policy=src.tool_policy,
            flow_policy=src.flow_policy,
            hard_constraints=src.hard_constraints,
            is_builtin=False,
            cloned_from_id=src.id.value,
        )
        await self._templates.save(clone)
        _log.info(
            "chat.mode_template.cloned",
            source_id=src.id.value,
            clone_id=clone.id.value,
        )
        return clone


# ---------------------------------------------------------------------------
# Reset mode template (restore a clone's fields from its source, in place)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ResetModeTemplateInput:
    template_id: ModeTemplateId


class ResetModeTemplateUseCase:
    """Restore a cloned copy's business fields from its source, in place.

    Only a user clone (``is_builtin == False`` AND ``cloned_from_id`` set) may
    be reset (``ResetModeTemplateNotCloneError`` otherwise). The copy keeps its
    OWN id / created_at / cloned_from_id (no delete/recreate → no dangling
    reference); only the business fields are overwritten from the source and
    ``updated_at`` is bumped.
    """

    def __init__(
        self,
        *,
        templates: ModeTemplateRepositoryPort,
        clock: Clock,
    ) -> None:
        self._templates = templates
        self._clock = clock

    async def execute(self, request: ResetModeTemplateInput) -> ModeTemplate:
        copy = await self._templates.get(request.template_id)
        if copy.is_builtin or not copy.cloned_from_id:
            raise ResetModeTemplateNotCloneError(request.template_id.value)
        src = await self._templates.get(
            ModeTemplateId.of(copy.cloned_from_id),
        )
        now = self._clock.now()
        copy.rename(src.name, now=now)
        copy.set_description(src.description, now=now)
        copy.set_framing(src.framing, now=now)
        copy.set_tool_policy(src.tool_policy, now=now)
        copy.set_flow_policy(src.flow_policy, now=now)
        copy.set_hard_constraints(src.hard_constraints, now=now)
        await self._templates.save(copy)
        _log.info(
            "chat.mode_template.reset",
            template_id=copy.id.value,
            source_id=src.id.value,
        )
        return copy


# ---------------------------------------------------------------------------
# Apply mode template to a conversation (set selected_mode_id)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyModeTemplateInput:
    template_id: ModeTemplateId
    conversation_id: ConversationId
    #: Selection policy (§26.2/26.4); ``manual`` by default = explicit user pick.
    selection_policy: str = "manual"


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyModeTemplateResult:
    conversation_id: str
    mode_id: str
    mode_name: str
    selection_policy: str


class ApplyModeTemplateUseCase:
    """Set a conversation's selected collaboration mode.

    Records ``selected_mode_id`` + ``mode_selection_policy`` on the conversation
    ``meta["discussion"]`` (tail-appended, §3.1).  This is the USER-EXPLICIT
    privilege-selection action of §26.4 — selecting a high-risk mode (e.g. 实施)
    here is the confirmation the classifier may never perform silently.  The
    target conversation + mode must exist.
    """

    def __init__(
        self,
        *,
        templates: ModeTemplateRepositoryPort,
        conversations: ConversationRepositoryPort,
        clock: Clock,
    ) -> None:
        self._templates = templates
        self._conversations = conversations
        self._clock = clock

    async def execute(
        self, request: ApplyModeTemplateInput
    ) -> ApplyModeTemplateResult:
        template = await self._templates.get(request.template_id)
        conv = await _require_conversation(
            self._conversations, request.conversation_id
        )
        policy = (
            request.selection_policy
            if request.selection_policy in _VALID_SELECTION_POLICIES
            else "manual"
        )
        discussion = dict(conv.discussion or {})
        discussion["selected_mode_id"] = template.id.value
        discussion["mode_selection_policy"] = policy
        # Decision 1: applying a mode FILLS the conversation's flow_policy from
        # the mode template (the mode is a TEMPLATE; the conversation
        # meta["discussion"] is the runtime truth source). The user actively
        # picking a mode means accepting this overwrite (no confirm). Afterwards
        # the user may freely edit the main-panel flow controls (conversation-
        # level only — never written back to the mode template). Field mapping
        # mirrors discussion_mode / participant_management runtime reads:
        #   speaker_strategy -> selector_mode
        #   max_rounds       -> max_rounds
        #   judge_enabled    -> enable_judge
        #   allow_mode_switch-> allow_mode_switch
        flow = template.flow_policy
        discussion["selector_mode"] = flow.speaker_strategy
        discussion["max_rounds"] = flow.max_rounds
        discussion["enable_judge"] = flow.judge_enabled
        discussion["allow_mode_switch"] = flow.allow_mode_switch
        conv.set_discussion(discussion, now=self._clock.now())
        await self._conversations.save(conv)
        _log.info(
            "chat.mode_template.applied",
            template_id=request.template_id.value,
            conversation_id=request.conversation_id.value,
            selection_policy=policy,
        )
        return ApplyModeTemplateResult(
            conversation_id=request.conversation_id.value,
            mode_id=template.id.value,
            mode_name=template.name,
            selection_policy=policy,
        )
