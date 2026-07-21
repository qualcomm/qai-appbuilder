# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for multi-agent discussion participant management (CRUD + config).

A multi-agent discussion (docs/70-multi-agent/multi-agent-conversation-design.md §16) is
configured by two orthogonal pieces of persisted state, both backed by the
already-locked chat aggregates / ports (no new table beyond migration 034's
``chat_participant.config_json`` column):

* the conversation's **named-agent roster** — one
  :class:`~qai.chat.domain.participant.Participant` (``kind=NAMED_AGENT``) per
  speaker, each carrying its ``display_name`` / ``model_id`` / ``persona`` and a
  free-form ``config`` blob (``allowed_tools`` + ``color``); and
* the conversation's **discussion switch** —
  ``Conversation.meta["discussion"]`` (``is_discussion`` / ``selector_mode`` /
  ``max_rounds`` / ``enable_judge``), set via
  :meth:`~qai.chat.domain.conversation.Conversation.set_discussion`.

This module groups the thin application use cases the REST layer composes the
participant-CRUD + discussion-config endpoints from. They are deliberately
small wrappers over the :class:`ParticipantRepositoryPort` /
:class:`ConversationRepositoryPort` + the domain mutators
(:meth:`Participant.create` / :meth:`Participant.rename` /
:meth:`Participant.set_config` / :meth:`Conversation.set_discussion`) so the
interfaces layer stays thin (``interfaces-stays-thin`` contract): the route
handler only parses the request, calls one ``execute(...)``, and serialises the
result.

Layering: ``application/use_cases`` — depends only on ``application.ports``
Protocols + ``domain`` + the platform ``Clock`` / ``IdGenerator``. Imports no
adapters / apps / interfaces, so ``layered-chat`` / ``context-isolation`` hold.
Multi-agent discussion is a pure V2 enhancement (细则 4-bis); these use cases
sit ALONGSIDE the existing single-agent conversation use cases and never touch
their paths (judgement 2: non-discussion behaviour is unchanged).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from qai.chat.application.ports import (
    ConversationRepositoryPort,
    ParticipantRepositoryPort,
)
from qai.chat.application.use_cases.implementation_plan import (
    FeatureItem,
    feature_item_from_dict,
    read_implementation_plan,
    write_implementation_plan,
)
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.errors import (
    ConversationNotFoundError,
    ParticipantNotFoundError,
)
from qai.chat.domain.ids import ConversationId, ParticipantId
from qai.chat.domain.participant import Participant, ParticipantKind
from qai.platform.errors import ConflictError, ValidationError
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock

__all__ = [
    "CreateParticipantInput",
    "CreateParticipantUseCase",
    "DeleteParticipantInput",
    "DeleteParticipantUseCase",
    "GetDiscussionConfigInput",
    "GetDiscussionConfigUseCase",
    "GetImplementationPlanInput",
    "GetImplementationPlanUseCase",
    "ListParticipantsInput",
    "ListParticipantsUseCase",
    "SetDiscussionConfigInput",
    "SetDiscussionConfigUseCase",
    "UpdateImplementationPlanInput",
    "UpdateImplementationPlanUseCase",
    "UpdateParticipantInput",
    "UpdateParticipantUseCase",
]

_log = get_logger(__name__)

# Sentinel distinguishing "field omitted from a PATCH" (leave unchanged) from
# "field explicitly set to None" (clear it). A plain ``None`` default cannot
# express both, so PATCH-style updates use this object.
_UNSET: Any = object()


async def _require_conversation(
    conversations: ConversationRepositoryPort,
    conversation_id: ConversationId,
) -> Conversation:
    """Load a conversation or raise the unified not-found error."""
    conv = await conversations.find(conversation_id)
    if conv is None:
        raise ConversationNotFoundError(conversation_id.value)
    return conv


def _coerce_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise a request ``config`` dict to the aggregate's blob shape.

    An empty / falsy value clears the config (``None``). Shape validation is
    delegated to :meth:`Participant.set_config` / :meth:`Participant.create`
    (the domain is the single validator), so a malformed blob raises a domain
    ``TypeError`` the route maps to a 4xx.
    """
    return dict(config) if config else None


# ---------------------------------------------------------------------------
# List participants
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ListParticipantsInput:
    conversation_id: ConversationId
    #: When True, return only ``NAMED_AGENT`` rows (the discussion roster);
    #: otherwise every participant of the conversation. The REST list endpoint
    #: surfaces named agents only (the user / main-agent / sub-agent rows are
    #: not user-managed discussion speakers).
    named_agents_only: bool = True


class ListParticipantsUseCase:
    """List a conversation's participants (optionally only named agents)."""

    def __init__(self, *, participants: ParticipantRepositoryPort) -> None:
        self._participants = participants

    async def execute(
        self, request: ListParticipantsInput
    ) -> tuple[Participant, ...]:
        rows = await self._participants.list_by_conversation(
            request.conversation_id
        )
        if request.named_agents_only:
            return tuple(
                p for p in rows if p.kind is ParticipantKind.NAMED_AGENT
            )
        return tuple(rows)


# ---------------------------------------------------------------------------
# Create participant
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class CreateParticipantInput:
    conversation_id: ConversationId
    display_name: str = ""
    model_id: str | None = None
    persona: str | None = None
    config: dict[str, Any] | None = None


class CreateParticipantUseCase:
    """Create a brand-new ``NAMED_AGENT`` participant for a discussion.

    Validates the parent conversation exists (so an orphaned roster row can
    never be created), mints a fresh :class:`ParticipantId`, and persists the
    aggregate. The ``kind`` is fixed to ``NAMED_AGENT`` — this use case is the
    discussion-roster create path; sub-agent participants are created by the
    sub-agent loop, not here.
    """

    def __init__(
        self,
        *,
        participants: ParticipantRepositoryPort,
        conversations: ConversationRepositoryPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._participants = participants
        self._conversations = conversations
        self._clock = clock
        self._ids = ids

    async def execute(self, request: CreateParticipantInput) -> Participant:
        # A NAMED_AGENT (discussion role) MUST carry its own cloud model — a
        # discussion is self-contained and never borrows the tab's selected
        # model. Reject a blank/whitespace model_id at the write boundary
        # (domain keeps model_id nullable for user / main_agent / sub_agent
        # participants, so the guard lives here, not in the aggregate).
        if not request.model_id or not request.model_id.strip():
            raise ValidationError(
                "chat.participant.model_required",
                "A discussion participant must have a model_id.",
                field_errors={"model_id": ["model_id is required"]},
            )
        await _require_conversation(
            self._conversations, request.conversation_id
        )
        participant = Participant.create(
            participant_id=ParticipantId.generate(self._ids),
            conversation_id=request.conversation_id,
            kind=ParticipantKind.NAMED_AGENT,
            now=self._clock.now(),
            display_name=request.display_name,
            model_id=request.model_id,
            persona=request.persona,
            config=_coerce_config(request.config),
        )
        await self._participants.save(participant)
        return participant


# ---------------------------------------------------------------------------
# Update participant (PATCH semantics)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateParticipantInput:
    conversation_id: ConversationId
    participant_id: ParticipantId
    #: Each field defaults to ``_UNSET`` = "omitted, leave unchanged". An
    #: explicit ``None`` clears the (nullable) field.
    display_name: Any = _UNSET
    model_id: Any = _UNSET
    persona: Any = _UNSET
    config: Any = _UNSET


class UpdateParticipantUseCase:
    """Update a discussion participant's mutable fields (PATCH semantics).

    Loads the aggregate (404 on a miss, scoped to the parent conversation),
    applies only the supplied fields via the domain mutators, and re-saves.
    ``display_name`` uses :meth:`Participant.rename`; ``config`` uses
    :meth:`Participant.set_config`; ``model_id`` / ``persona`` are set on the
    aggregate directly (no dedicated mutator) and ``updated_at`` is bumped via
    a ``rename``-equivalent touch when only those change.
    """

    def __init__(
        self,
        *,
        participants: ParticipantRepositoryPort,
        clock: Clock,
    ) -> None:
        self._participants = participants
        self._clock = clock

    async def execute(self, request: UpdateParticipantInput) -> Participant:
        participant = await self._participants.find(request.participant_id)
        if participant is None or (
            participant.conversation_id != request.conversation_id
        ):
            # Scoped lookup: a participant id that belongs to a different
            # conversation is treated as not-found for this conversation.
            raise ParticipantNotFoundError(request.participant_id.value)

        now = self._clock.now()
        touched = False
        if request.display_name is not _UNSET:
            participant.rename(str(request.display_name), now=now)
            touched = True
        if request.model_id is not _UNSET:
            # PATCH target is always a NAMED_AGENT (discussion role) — clearing
            # or blanking its model is rejected (parity with create; a
            # self-contained discussion always needs a real model). Omitting the
            # field (``_UNSET``) leaves it unchanged.
            if request.model_id is None or not str(request.model_id).strip():
                raise ValidationError(
                    "chat.participant.model_required",
                    "A discussion participant must have a model_id.",
                    field_errors={"model_id": ["model_id is required"]},
                )
            participant.model_id = str(request.model_id)
            touched = True
        if request.persona is not _UNSET:
            participant.persona = (
                None if request.persona is None else str(request.persona)
            )
            touched = True
        if request.config is not _UNSET:
            participant.set_config(_coerce_config(request.config), now=now)
            touched = True
        if touched:
            # Ensure ``updated_at`` advances even when only model_id / persona
            # changed (those set the field directly without a mutator).
            participant.updated_at = now
            await self._participants.save(participant)
        return participant


# ---------------------------------------------------------------------------
# Delete participant
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteParticipantInput:
    conversation_id: ConversationId
    participant_id: ParticipantId


class DeleteParticipantUseCase:
    """Delete a discussion participant (scoped to its parent conversation)."""

    def __init__(self, *, participants: ParticipantRepositoryPort) -> None:
        self._participants = participants

    async def execute(self, request: DeleteParticipantInput) -> None:
        participant = await self._participants.find(request.participant_id)
        if participant is None or (
            participant.conversation_id != request.conversation_id
        ):
            raise ParticipantNotFoundError(request.participant_id.value)
        await self._participants.delete(request.participant_id)


# ---------------------------------------------------------------------------
# Discussion config (read + write)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class GetDiscussionConfigInput:
    conversation_id: ConversationId


class GetDiscussionConfigUseCase:
    """Return a conversation's discussion config blob (or ``None``)."""

    def __init__(self, *, conversations: ConversationRepositoryPort) -> None:
        self._conversations = conversations

    async def execute(
        self, request: GetDiscussionConfigInput
    ) -> dict[str, Any] | None:
        conv = await _require_conversation(
            self._conversations, request.conversation_id
        )
        return conv.discussion


@dataclass(frozen=True, slots=True, kw_only=True)
class SetDiscussionConfigInput:
    conversation_id: ConversationId
    #: Tri-state discussion-mode flag (MERGE semantics — fixes the partial-PATCH
    #: wipe bug):
    #:
    #: * ``True``  — enable discussion mode and MERGE the supplied switches
    #:   over the existing ``meta["discussion"]`` blob;
    #: * ``False`` — EXPLICITLY clear the whole discussion config;
    #: * ``None``  — KEEP the current on/off state and MERGE the supplied
    #:   switches (the real front-end emits a single-key partial PATCH that
    #:   omits ``is_discussion`` — this resolves to None so a toggle of ONE
    #:   switch never silently drops the others).
    is_discussion: bool | None = None
    selector_mode: str | None = None
    max_rounds: int | None = None
    enable_judge: bool | None = None
    discussion_prompt: str | None = None
    #: Collaboration-mode V1 (§26/§27) — tail-appended (§3.1). The selected mode
    #: id + how it was selected (auto/manual/locked/suggested). ``None`` leaves
    #: any existing selection untouched (PATCH semantics).
    selected_mode_id: str | None = None
    mode_selection_policy: str | None = None
    #: DISC-2 二期 convergence-control flags — tail-appended (§3.1). ``None``
    #: leaves any existing value untouched (PATCH semantics). These are user
    #: switches assembled via the ``if ... is not None`` whitelist in
    #: :meth:`SetDiscussionConfigUseCase.execute` (NOT preserved keys).
    convergence_control_enabled: bool | None = None
    manager_early_end_enabled: bool | None = None
    soft_stop_enabled: bool | None = None
    soft_stop_mode: str | None = None
    #: DISC-2 P4-step1 (§22A.7) — social/lightweight-path response policy,
    #: tail-appended (§3.1). ``None`` leaves any existing value untouched (PATCH
    #: semantics); a user switch assembled via the ``if ... is not None``
    #: whitelist in :meth:`SetDiscussionConfigUseCase.execute`.
    social_response_policy: str | None = None
    #: DISC-2 P4-step2 (§22A.7, final step) — Manager prompt customization,
    #: tail-appended (§3.1). ``None`` leaves any existing value untouched (PATCH
    #: semantics); user switches assembled via the ``if ... is not None``
    #: whitelist in :meth:`SetDiscussionConfigUseCase.execute`. The front-end may
    #: send only ``manager_prompt_append`` (mode inferred at read time).
    manager_prompt_customization_mode: str | None = None
    manager_prompt_append: str | None = None
    #: DISC-1 §22.7 ("discussion → implementation" master switch) + DISC-2
    #: §22A.5 (LLM grey-zone intent classifier) feature flags — tail-appended
    #: (§3.1). ``None`` leaves any existing value untouched (PATCH semantics);
    #: user switches assembled via the ``if ... is not None`` whitelist in
    #: :meth:`SetDiscussionConfigUseCase.execute`. A missing key in the persisted
    #: meta resolves to OFF at orchestrator read time (legacy untouched).
    implementation_enabled: bool | None = None
    intent_classifier_enabled: bool | None = None
    #: DISC-1 TODO-2 — user-tunable numeric/string knobs, tail-appended (§3.1).
    #: ``None`` leaves any existing value untouched (PATCH semantics); a missing
    #: key resolves to the conservative default at orchestrator read time. User
    #: switches assembled via the ``if ... is not None`` whitelist in
    #: :meth:`SetDiscussionConfigUseCase.execute`.
    impl_max_total_file_edits: int | None = None
    impl_max_total_exec_calls: int | None = None
    impl_max_total_runtime_seconds: int | None = None
    impl_max_total_changed_files: int | None = None
    soft_stop_similarity: float | None = None
    soft_stop_min_rounds: int | None = None
    soft_stop_consecutive_turns: int | None = None
    intent_classifier_model: str | None = None
    intent_classifier_timeout_ms: int | None = None
    implementation_planner_model: str | None = None
    implementation_planner_timeout_ms: int | None = None
    #: DISC-1 三期-step5 + 完成判定 B — validator / verify-command knobs,
    #: tail-appended (§3.1). ``None`` leaves the existing value untouched (PATCH
    #: semantics); a missing key resolves to the conservative default at
    #: orchestrator read time (validator OFF, timeouts at constant defaults).
    implementation_validator_enabled: bool | None = None
    implementation_validator_timeout_ms: int | None = None
    implementation_verify_command_timeout_ms: int | None = None


#: Backend-managed ``meta["discussion"]`` keys NOT owned by the config PATCH —
#: documents which keys are owned by the apply-mode route / discussion state
#: machine rather than the user-switch PATCH. Since the PATCH now MERGE-seeds
#: from the existing blob (``dict(existing)``), these are preserved automatically
#: and no explicit copy loop is needed; the constant is retained as the
#: authoritative inventory of backend-managed keys (referenced by the use-case
#: docstring) so future switches are not accidentally treated as user-owned.
_PRESERVED_DISCUSSION_KEYS: tuple[str, ...] = (
    "selected_mode_id",
    "mode_selection_policy",
    "discussion_state",
    "last_active_speaker",
    "last_intent_classification",
    "stance_snapshot",
    "implementation",
)


class SetDiscussionConfigUseCase:
    """Set, MERGE, or clear a conversation's multi-agent discussion config.

    Writes ``meta["discussion"]`` via :meth:`Conversation.set_discussion`
    (scheme A — reuse the persisted ``meta_json``, no new table). The PATCH
    is MERGE-semantic so a partial update only touches the keys it carries:

    * ``is_discussion is False`` — EXPLICITLY clears the whole blob;
    * ``is_discussion is True`` / ``is_discussion is None`` — seeds the new
      blob from the EXISTING ``meta["discussion"]`` (preserving every prior
      switch + the backend-managed keys, see :data:`_PRESERVED_DISCUSSION_KEYS`)
      then overlays only the supplied switches. ``None`` additionally keeps the
      current on/off state, so the real front-end's single-key partial PATCH
      (which omits ``is_discussion``) never silently drops the other switches.

    Persists through the (locked) conversation ``save`` path.
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        clock: Clock,
    ) -> None:
        self._conversations = conversations
        self._clock = clock

    async def execute(
        self, request: SetDiscussionConfigInput
    ) -> dict[str, Any] | None:
        conv = await _require_conversation(
            self._conversations, request.conversation_id
        )
        if request.is_discussion is False:
            # Only an EXPLICIT False clears the whole blob; a partial PATCH that
            # omits is_discussion resolves to None and MERGES instead.
            conv.set_discussion(None, now=self._clock.now())
        else:
            existing = conv.discussion or {}
            # Seed from the EXISTING blob so a partial PATCH preserves every
            # prior switch (+ the backend-managed keys, which ride along in the
            # copy); the explicit-value overlay below then updates only the
            # supplied keys. is_discussion=None keeps the current on/off state.
            discussion: dict[str, Any] = dict(existing)
            discussion["is_discussion"] = True
            if request.selector_mode is not None:
                discussion["selector_mode"] = request.selector_mode
            if request.max_rounds is not None:
                discussion["max_rounds"] = request.max_rounds
            if request.enable_judge is not None:
                discussion["enable_judge"] = request.enable_judge
            if request.discussion_prompt is not None:
                discussion["discussion_prompt"] = request.discussion_prompt
            # Mode selection (§26/§27): an explicit value wins; otherwise carry
            # over whatever the apply-mode route last set.
            if request.selected_mode_id is not None:
                discussion["selected_mode_id"] = request.selected_mode_id
            if request.mode_selection_policy is not None:
                discussion["mode_selection_policy"] = request.mode_selection_policy
            # DISC-2 二期 convergence-control flags (user switches) — assemble
            # via the same explicit-value whitelist; a missing flag in the
            # persisted meta resolves to OFF at orchestrator read time.
            if request.convergence_control_enabled is not None:
                discussion["convergence_control_enabled"] = (
                    request.convergence_control_enabled
                )
            if request.manager_early_end_enabled is not None:
                discussion["manager_early_end_enabled"] = (
                    request.manager_early_end_enabled
                )
            if request.soft_stop_enabled is not None:
                discussion["soft_stop_enabled"] = request.soft_stop_enabled
            if request.soft_stop_mode is not None:
                discussion["soft_stop_mode"] = request.soft_stop_mode
            if request.social_response_policy is not None:
                discussion["social_response_policy"] = (
                    request.social_response_policy
                )
            # DISC-2 P4-step2 (§22A.7) — Manager prompt customization (user
            # switches); a missing key resolves to no-append at read time.
            if request.manager_prompt_customization_mode is not None:
                discussion["manager_prompt_customization_mode"] = (
                    request.manager_prompt_customization_mode
                )
            if request.manager_prompt_append is not None:
                discussion["manager_prompt_append"] = (
                    request.manager_prompt_append
                )
            # DISC-1 §22.7 / DISC-2 §22A.5 feature flags (user switches) — a
            # missing key resolves to OFF at orchestrator read time. A fresh tab
            # seeds these ON via the front-end's enable-discussion full-config
            # PATCH; legacy conversations without the key stay OFF.
            if request.implementation_enabled is not None:
                discussion["implementation_enabled"] = (
                    request.implementation_enabled
                )
            if request.intent_classifier_enabled is not None:
                discussion["intent_classifier_enabled"] = (
                    request.intent_classifier_enabled
                )
            # DISC-1 三期-step5 — OPTIONAL LLM validator flag (user switch); a
            # missing key resolves to OFF at orchestrator read time (legacy +
            # every existing conversation untouched).
            if request.implementation_validator_enabled is not None:
                discussion["implementation_validator_enabled"] = (
                    request.implementation_validator_enabled
                )
            # DISC-1 TODO-2 user-tunable numeric/string knobs — same
            # explicit-value whitelist (``None`` ⇒ untouched). Iterated to keep
            # the merge readable; meta key == attr name (1:1, snake_case).
            for _knob in (
                "impl_max_total_file_edits",
                "impl_max_total_exec_calls",
                "impl_max_total_runtime_seconds",
                "impl_max_total_changed_files",
                "soft_stop_similarity",
                "soft_stop_min_rounds",
                "soft_stop_consecutive_turns",
                "intent_classifier_model",
                "intent_classifier_timeout_ms",
                "implementation_planner_model",
                "implementation_planner_timeout_ms",
                "implementation_validator_timeout_ms",
                "implementation_verify_command_timeout_ms",
            ):
                _value = getattr(request, _knob)
                if _value is not None:
                    discussion[_knob] = _value
            # Backend-managed keys (:data:`_PRESERVED_DISCUSSION_KEYS`) already
            # rode along in ``dict(existing)`` above, so no separate copy loop is
            # needed — the MERGE seed inherently preserves them.
            conv.set_discussion(discussion, now=self._clock.now())
        await self._conversations.save(conv)
        return conv.discussion


# ---------------------------------------------------------------------------
# Implementation plan (read + edit) — DISC-1 二期-step4 (§22.9)
# ---------------------------------------------------------------------------
#: FeatureItem fields the front-end is allowed to author/edit. Everything else
#: (``status`` beyond pending↔skipped, ``result_summary`` / ``last_error`` /
#: ``started_at`` / ``finished_at`` / ``attempt_count`` / ``suggested_role`` /
#: ``id``) is BACKEND TRUTH (§🔴 State-Truth-First) — the plan editor must never
#: let the UI overwrite the run state machine's bookkeeping. The merge in
#: :meth:`UpdateImplementationPlanUseCase._merge_item` enforces this whitelist.
_USER_EDITABLE_ITEM_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "acceptance_criteria",
    "assigned_role",
    "verify_command",
)
#: The only ``status`` transitions the user may drive from the plan editor
#: (pending ↔ skipped). Every other status (in_progress / done / failed) is
#: owned by the run state machine and preserved from the backend item.
_USER_EDITABLE_STATUSES: frozenset[str] = frozenset({"pending", "skipped"})


@dataclass(frozen=True, slots=True, kw_only=True)
class GetImplementationPlanInput:
    conversation_id: ConversationId


class GetImplementationPlanUseCase:
    """Return a conversation's ``meta["discussion"]`` blob for plan reading.

    Returns the raw discussion dict (or ``None``) — the route handler runs
    :func:`read_implementation_plan` + :func:`plan_to_dict` itself, mirroring
    how :class:`GetDiscussionConfigUseCase` returns the discussion dict and lets
    the handler shape the response. A conversation with no discussion / no plan
    yields ``None`` (the handler returns the stable empty-shell body), so the
    flag-OFF / no-plan path never errors.
    """

    def __init__(self, *, conversations: ConversationRepositoryPort) -> None:
        self._conversations = conversations

    async def execute(
        self, request: GetImplementationPlanInput
    ) -> dict[str, Any] | None:
        conv = await _require_conversation(
            self._conversations, request.conversation_id
        )
        return conv.discussion


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateImplementationPlanInput:
    conversation_id: ConversationId
    #: The full ordered items array the front-end submits. Each entry is a
    #: feature-item wire dict (snake_case). The use case treats this as the
    #: authoritative item SET (by id): an id present here that already exists
    #: is MERGED (only user-editable fields applied, backend truth preserved);
    #: a new id is appended as a fresh pending item; an existing id absent from
    #: this list is DELETED.
    items: tuple[dict[str, Any], ...] = ()


class UpdateImplementationPlanUseCase:
    """Edit a conversation's implementation plan items (DISC-1 二期-step4, §22.9).

    The plan lives at ``meta["discussion"]["implementation"]`` (a backend-managed,
    §3.1-additive key). This use case lets the front-end re-shape the item LIST
    (reorder / add / delete / edit assigned_role+labels / skip) while the run
    state machine retains exclusive ownership of every execution-truth field.

    Guarantees (§🔴 State-Truth-First):

    * **Backend truth is never overwritten.** For an item id that already exists,
      only :data:`_USER_EDITABLE_ITEM_FIELDS` (+ a pending↔skipped ``status``
      flip) are taken from the request; ``result_summary`` / ``last_error`` /
      ``started_at`` / ``finished_at`` / ``attempt_count`` / ``suggested_role``
      and any non-editable ``status`` are PRESERVED from the persisted item.
    * **assigned_role must be a real roster participant.** A non-``None``
      ``assigned_role`` not in the conversation's named-agent roster raises a
      :class:`ValidationError` (→ 400) so the UI surfaces the bad pin rather
      than silently dropping it.
    * **In-flight item is protected.** When ``phase == "implementing"``, the item
      identified by ``current_item`` (or any item whose persisted status is
      ``in_progress``) cannot be edited or deleted — that raises a
      :class:`ConflictError` (→ 409).

    A conversation with NO plan (flag-OFF / pre-extraction) is a safe no-op: the
    discussion blob is returned unchanged, never an error.
    """

    def __init__(
        self,
        *,
        conversations: ConversationRepositoryPort,
        participants: ParticipantRepositoryPort,
        clock: Clock,
    ) -> None:
        self._conversations = conversations
        self._participants = participants
        self._clock = clock

    async def execute(
        self, request: UpdateImplementationPlanInput
    ) -> dict[str, Any] | None:
        conv = await _require_conversation(
            self._conversations, request.conversation_id
        )
        current = conv.discussion or {}
        plan = read_implementation_plan(current)
        if plan is None:
            # No plan to edit (flag-OFF / pre-extraction). Safe no-op — return
            # the discussion unchanged rather than erroring (空壳 contract).
            return conv.discussion

        # Roster of legal assigned_role targets (named-agent participant ids).
        roster = await self._participants.list_by_conversation(
            request.conversation_id
        )
        valid_role_ids = {
            p.id.value
            for p in roster
            if p.kind is ParticipantKind.NAMED_AGENT
        }

        # In-flight protection set: the current_item + any persisted in_progress
        # item ids, only meaningful while implementing.
        protected_ids: set[str] = set()
        if plan.phase == "implementing":
            if plan.current_item:
                protected_ids.add(plan.current_item)
            protected_ids.update(
                item.id
                for item in plan.items
                if item.status == "in_progress"
            )

        existing_by_id: dict[str, FeatureItem] = {
            item.id: item for item in plan.items
        }
        incoming_ids: set[str] = set()
        new_items: list[FeatureItem] = []
        for raw in request.items:
            normalized = feature_item_from_dict(raw)
            if normalized is None:
                # Non-dict entry — drop it (State-Truth-First defensive read).
                continue
            item_id = normalized.id
            existing = existing_by_id.get(item_id) if item_id else None
            if existing is not None:
                incoming_ids.add(item_id)
                self._guard_protected(existing, protected_ids)
                merged = self._merge_item(
                    existing, normalized, valid_role_ids
                )
                new_items.append(merged)
            else:
                # New item — a fresh pending work-item authored by the user.
                self._validate_role(normalized.assigned_role, valid_role_ids)
                new_items.append(
                    replace(normalized, status="pending")
                )

        # Deletions: existing ids absent from the incoming list are dropped —
        # but a protected (in-flight) item may not be deleted.
        for item in plan.items:
            if item.id in incoming_ids:
                continue
            self._guard_protected(item, protected_ids)

        now_iso = self._clock.now().isoformat()
        plan2 = replace(plan, items=tuple(new_items), updated_at=now_iso)
        new_discussion = write_implementation_plan(current, plan2)
        conv.set_discussion(new_discussion, now=self._clock.now())
        await self._conversations.save(conv)
        return conv.discussion

    def _guard_protected(
        self, item: FeatureItem, protected_ids: set[str]
    ) -> None:
        """Raise 409 if ``item`` is the in-flight item (cannot edit/delete)."""
        if item.id in protected_ids:
            raise ConflictError(
                "chat.implementation_plan.item_in_progress",
                "Cannot edit or delete an item that is currently being "
                "implemented; pause or stop the run first.",
                details={"item_id": item.id},
            )

    def _validate_role(
        self, assigned_role: str | None, valid_role_ids: set[str]
    ) -> None:
        """Raise 400 if a non-None assigned_role is not a roster participant."""
        if assigned_role is not None and assigned_role not in valid_role_ids:
            raise ValidationError(
                "chat.implementation_plan.invalid_assigned_role",
                "assigned_role must reference an existing discussion "
                "participant.",
                field_errors={"assigned_role": [assigned_role]},
            )

    def _merge_item(
        self,
        existing: FeatureItem,
        incoming: FeatureItem,
        valid_role_ids: set[str],
    ) -> FeatureItem:
        """Merge user-editable fields onto a backend item; preserve truth fields.

        Only :data:`_USER_EDITABLE_ITEM_FIELDS` + a pending↔skipped ``status``
        flip are taken from ``incoming``; every other field (run-state truth)
        is preserved from ``existing``. A bad ``assigned_role`` raises 400.
        """
        self._validate_role(incoming.assigned_role, valid_role_ids)
        # Status: only a pending↔skipped flip is honoured. Any other incoming
        # status is ignored and the backend status preserved. If the backend
        # item is in a run-owned status (in_progress/done/failed), the user
        # cannot move it either — preserve it.
        status = existing.status
        if (
            existing.status in _USER_EDITABLE_STATUSES
            and incoming.status in _USER_EDITABLE_STATUSES
        ):
            status = incoming.status
        return replace(
            existing,
            title=incoming.title,
            description=incoming.description,
            acceptance_criteria=incoming.acceptance_criteria,
            assigned_role=incoming.assigned_role,
            status=status,
            verify_command=incoming.verify_command,
        )
