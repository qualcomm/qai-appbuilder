# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Roster-template HTTP routes (``/api/chat/roster-templates``).

A **roster template** is a named, reusable bundle of multi-agent discussion role
definitions (a "team": Architect / Developer / Tester ...).  These endpoints let
the discussion panel:

* list built-in presets + the user's saved templates;
* save the current roster as a new template / patch / delete a saved template;
* apply (import) a template's roles into a conversation as ``named_agent``
  participants (the "reuse a team for a new task" flow).

PURE V2 enhancement (V1 has no multi-agent discussion; 细则 4-bis受保护). New
routes only — no existing path / method / payload changed (§3.1). Handlers are
thin (``interfaces-stays-thin``): parse → one ``execute(...)`` → serialise.
Domain errors propagate to the global error middleware
(``RosterTemplateNotFoundError`` → 404, ``RosterTemplateReadOnlyError`` → 400 —
it subclasses ``ValidationError``, which ``error_handlers`` maps to 400).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from qai.chat.application.use_cases.roster_template_management import (
    ApplyRosterTemplateInput,
    ApplyRosterTemplateResult,
    CloneRosterTemplateInput,
    CreateRosterTemplateInput,
    DeleteRosterTemplateInput,
    ResetRosterTemplateInput,
    RosterMemberInput,
    UpdateRosterTemplateInput,
)
from qai.chat.domain.ids import ConversationId, RosterTemplateId
from qai.chat.domain.roster_template import RosterTemplate

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---- DTOs -----------------------------------------------------------------


class RosterMemberConfig(BaseModel):
    """Per-member config blob (mirrors ``ParticipantConfig``)."""

    allowed_tools: list[str] | None = Field(default=None)
    color: int | str | None = Field(default=None)
    enabled_skills: list[str] | None = Field(default=None)

    def to_blob(self) -> dict[str, Any] | None:
        blob: dict[str, Any] = {}
        if self.allowed_tools is not None:
            blob["allowed_tools"] = list(self.allowed_tools)
        if self.color is not None:
            blob["color"] = self.color
        if self.enabled_skills is not None:
            blob["enabled_skills"] = list(self.enabled_skills)
        return blob or None


class RosterMemberBody(BaseModel):
    """One role definition in a roster-template request / response."""

    display_name: str = Field(default="", max_length=256)
    model_id: str | None = Field(default=None, max_length=256)
    persona: str | None = Field(default=None, max_length=100_000)
    config: RosterMemberConfig | None = Field(default=None)

    def to_input(self) -> RosterMemberInput:
        return RosterMemberInput(
            display_name=self.display_name,
            model_id=self.model_id,
            persona=self.persona,
            config=self.config.to_blob() if self.config is not None else None,
        )


class RosterTemplateItem(BaseModel):
    """Single :class:`RosterTemplate` projection."""

    id: str
    name: str
    description: str
    members: list[RosterMemberBody]
    is_builtin: bool
    default_mode_id: str | None = None
    created_at: str
    updated_at: str
    #: When this template was cloned from another (e.g. a built-in preset), the
    #: SOURCE template's id; ``None`` for originals. Tail-appended (§3.1).
    cloned_from_id: str | None = None
    #: Multi-language (i18n) maps for built-in presets (migration 056).
    #: ``name_i18n`` / ``description_i18n`` are ``{"en":..,"zh-CN":..,"zh-TW":..}``
    #: objects; ``members_i18n`` is ``{locale: [{display_name, persona, config},
    #: ...]}`` (per-member localised text, index-aligned with ``members``).
    #: ``None`` for custom rows → the frontend falls back to the single-language
    #: fields above. Tail-appended (§3.1); single source of truth = seed → DB.
    name_i18n: dict[str, str] | None = None
    description_i18n: dict[str, str] | None = None
    members_i18n: dict[str, list[dict[str, Any]]] | None = None


class RosterTemplateListResponse(BaseModel):
    """``GET /api/chat/roster-templates`` body."""

    items: list[RosterTemplateItem]


class CreateRosterTemplateRequest(BaseModel):
    """``POST /api/chat/roster-templates`` body."""

    name: str = Field(default="", max_length=256)
    description: str = Field(default="", max_length=2000)
    members: list[RosterMemberBody] = Field(default_factory=list)
    default_mode_id: str | None = Field(default=None, max_length=64)


class UpdateRosterTemplateRequest(BaseModel):
    """``PATCH /api/chat/roster-templates/{id}`` body (PATCH semantics).

    Only fields the client SENDS are applied; ``members`` (when sent) REPLACES
    the full list. Field-presence is read from ``model_fields_set``.
    """

    name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    members: list[RosterMemberBody] | None = Field(default=None)
    default_mode_id: str | None = Field(default=None, max_length=64)


class ApplyRosterTemplateRequest(BaseModel):
    """``POST /api/chat/roster-templates/{id}/apply`` body."""

    conversation_id: str = Field(min_length=1, max_length=64)


class ApplyRosterTemplateResponse(BaseModel):
    """``POST /api/chat/roster-templates/{id}/apply`` result."""

    conversation_id: str
    participant_ids: list[str]
    members_added: int
    #: Non-null only when the team carried a ``default_mode_id`` that resolved to
    #: an existing mode and was selected on the conversation (so the UI can
    #: toast "mode also applied").
    applied_mode_id: str | None = None
    applied_mode_name: str | None = None


# ---- Helpers --------------------------------------------------------------


def _member_to_body(member: Any) -> RosterMemberBody:
    config = member.config or {}
    cfg = (
        RosterMemberConfig(
            allowed_tools=config.get("allowed_tools"),
            color=config.get("color"),
            enabled_skills=config.get("enabled_skills"),
        )
        if config
        else None
    )
    return RosterMemberBody(
        display_name=member.display_name,
        model_id=member.model_id,
        persona=member.persona,
        config=cfg,
    )


def _template_to_item(template: RosterTemplate) -> RosterTemplateItem:
    return RosterTemplateItem(
        id=template.id.value,
        name=template.name,
        description=template.description,
        members=[_member_to_body(m) for m in template.members],
        is_builtin=template.is_builtin,
        default_mode_id=template.default_mode_id,
        created_at=template.created_at.isoformat(),
        updated_at=template.updated_at.isoformat(),
        cloned_from_id=template.cloned_from_id,
        name_i18n=template.name_i18n,
        description_i18n=template.description_i18n,
        members_i18n=template.members_i18n,
    )


# ---- Router factory -------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the roster-template REST router bound to ``container``."""
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    @router.get(
        "/roster-templates",
        response_model=RosterTemplateListResponse,
    )
    async def list_roster_templates() -> RosterTemplateListResponse:
        rows = await container.chat.list_roster_templates_use_case.execute()
        return RosterTemplateListResponse(
            items=[_template_to_item(t) for t in rows],
        )

    @router.post(
        "/roster-templates",
        response_model=RosterTemplateItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_roster_template(
        body: CreateRosterTemplateRequest,
    ) -> RosterTemplateItem:
        template = await container.chat.create_roster_template_use_case.execute(
            CreateRosterTemplateInput(
                name=body.name,
                description=body.description,
                members=tuple(m.to_input() for m in body.members),
                default_mode_id=body.default_mode_id,
            ),
        )
        return _template_to_item(template)

    @router.patch(
        "/roster-templates/{template_id}",
        response_model=RosterTemplateItem,
    )
    async def update_roster_template(
        template_id: str,
        body: UpdateRosterTemplateRequest,
    ) -> RosterTemplateItem:
        sent = body.model_fields_set
        kwargs: dict[str, Any] = {}
        if "name" in sent:
            kwargs["name"] = body.name
        if "description" in sent:
            kwargs["description"] = body.description
        if "members" in sent:
            kwargs["members"] = tuple(
                m.to_input() for m in (body.members or [])
            )
        if "default_mode_id" in sent:
            kwargs["default_mode_id"] = body.default_mode_id
        template = await container.chat.update_roster_template_use_case.execute(
            UpdateRosterTemplateInput(
                template_id=RosterTemplateId.of(template_id),
                **kwargs,
            ),
        )
        return _template_to_item(template)

    @router.delete(
        "/roster-templates/{template_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def delete_roster_template(template_id: str) -> None:
        await container.chat.delete_roster_template_use_case.execute(
            DeleteRosterTemplateInput(
                template_id=RosterTemplateId.of(template_id),
            ),
        )
        return None

    @router.post(
        "/roster-templates/{template_id}/apply",
        response_model=ApplyRosterTemplateResponse,
    )
    async def apply_roster_template(
        template_id: str,
        body: ApplyRosterTemplateRequest,
    ) -> ApplyRosterTemplateResponse:
        result: ApplyRosterTemplateResult = (
            await container.chat.apply_roster_template_use_case.execute(
                ApplyRosterTemplateInput(
                    template_id=RosterTemplateId.of(template_id),
                    conversation_id=ConversationId.of(body.conversation_id),
                ),
            )
        )
        return ApplyRosterTemplateResponse(
            conversation_id=body.conversation_id,
            participant_ids=[p.id.value for p in result.participants],
            members_added=len(result.participants),
            applied_mode_id=result.applied_mode_id,
            applied_mode_name=result.applied_mode_name,
        )

    @router.post(
        "/roster-templates/{template_id}/clone",
        response_model=RosterTemplateItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def clone_roster_template(template_id: str) -> RosterTemplateItem:
        template = await container.chat.clone_roster_template_use_case.execute(
            CloneRosterTemplateInput(
                template_id=RosterTemplateId.of(template_id),
            ),
        )
        return _template_to_item(template)

    @router.post(
        "/roster-templates/{template_id}/reset",
        response_model=RosterTemplateItem,
    )
    async def reset_roster_template(template_id: str) -> RosterTemplateItem:
        template = await container.chat.reset_roster_template_use_case.execute(
            ResetRosterTemplateInput(
                template_id=RosterTemplateId.of(template_id),
            ),
        )
        return _template_to_item(template)

    return router


__all__ = ["build_router"]
