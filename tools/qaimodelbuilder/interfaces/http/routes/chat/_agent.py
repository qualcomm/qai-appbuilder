# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Agent-template HTTP routes (``/api/chat/agent-templates``).

An **agent template** is a named, reusable definition of a SINGLE multi-agent
discussion role (an "agent": 资深架构师 / 全栈开发 / 严谨测试 …) — the smallest
reusable unit in the three-tier template system (single role → team → mode,
§27).  These endpoints let the template panel:

* list built-in presets + the user's saved agents;
* save / patch / delete a saved agent;
* apply (import) an agent into a conversation as one ``named_agent``
  participant (the "reuse a role for a new task" flow).

PURE V2 enhancement (V1 has no multi-agent discussion; 细则 4-bis 受保护). New
routes only — no existing path / method / payload changed (§3.1). Handlers are
thin (``interfaces-stays-thin``): parse → one ``execute(...)`` → serialise.
Domain errors propagate to the global error middleware
(``AgentTemplateNotFoundError`` → 404, ``AgentTemplateReadOnlyError`` → 400 —
it subclasses ``ValidationError``, which ``error_handlers`` maps to 400).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from qai.chat.application.use_cases.agent_template_management import (
    ApplyAgentTemplateInput,
    CloneAgentTemplateInput,
    CreateAgentTemplateInput,
    DeleteAgentTemplateInput,
    ResetAgentTemplateInput,
    UpdateAgentTemplateInput,
)
from qai.chat.domain.agent_template import AgentTemplate
from qai.chat.domain.ids import AgentTemplateId, ConversationId
from qai.chat.domain.participant import Participant

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---- DTOs -----------------------------------------------------------------


class AgentConfig(BaseModel):
    """Per-agent config blob (mirrors ``ParticipantConfig``)."""

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


class AgentTemplateItem(BaseModel):
    """Single :class:`AgentTemplate` projection."""

    id: str
    name: str
    description: str
    display_name: str
    model_id: str | None
    persona: str | None
    config: AgentConfig | None
    is_builtin: bool
    created_at: str
    updated_at: str
    #: When this template was cloned from another (e.g. a built-in preset), the
    #: SOURCE template's id; ``None`` for originals. Tail-appended (§3.1).
    cloned_from_id: str | None = None
    #: Multi-language (i18n) maps for built-in presets (migration 056), each a
    #: ``{"en":..,"zh-CN":..,"zh-TW":..}`` object. ``None`` for custom rows →
    #: the frontend falls back to the single-language fields above. Tail-appended
    #: (§3.1); single source of truth = the seed → DB (no locale-file dup).
    name_i18n: dict[str, str] | None = None
    description_i18n: dict[str, str] | None = None
    display_name_i18n: dict[str, str] | None = None
    persona_i18n: dict[str, str] | None = None


class AgentTemplateListResponse(BaseModel):
    """``GET /api/chat/agent-templates`` body."""

    items: list[AgentTemplateItem]


class CreateAgentTemplateRequest(BaseModel):
    """``POST /api/chat/agent-templates`` body."""

    name: str = Field(default="", max_length=256)
    description: str = Field(default="", max_length=2000)
    display_name: str = Field(default="", max_length=256)
    model_id: str | None = Field(default=None, max_length=256)
    persona: str | None = Field(default=None, max_length=100_000)
    config: AgentConfig | None = Field(default=None)


class UpdateAgentTemplateRequest(BaseModel):
    """``PATCH /api/chat/agent-templates/{id}`` body (PATCH semantics).

    Only fields the client SENDS are applied; field-presence is read from
    ``model_fields_set``.
    """

    name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    display_name: str | None = Field(default=None, max_length=256)
    model_id: str | None = Field(default=None, max_length=256)
    persona: str | None = Field(default=None, max_length=100_000)
    config: AgentConfig | None = Field(default=None)


class ApplyAgentTemplateRequest(BaseModel):
    """``POST /api/chat/agent-templates/{id}/apply`` body."""

    conversation_id: str = Field(min_length=1, max_length=64)


class ApplyAgentTemplateResponse(BaseModel):
    """``POST /api/chat/agent-templates/{id}/apply`` result."""

    conversation_id: str
    participant_id: str


# ---- Helpers --------------------------------------------------------------


def _config_to_model(config: dict[str, Any] | None) -> AgentConfig | None:
    if not config:
        return None
    return AgentConfig(
        allowed_tools=config.get("allowed_tools"),
        color=config.get("color"),
        enabled_skills=config.get("enabled_skills"),
    )


def _template_to_item(template: AgentTemplate) -> AgentTemplateItem:
    return AgentTemplateItem(
        id=template.id.value,
        name=template.name,
        description=template.description,
        display_name=template.display_name,
        model_id=template.model_id,
        persona=template.persona,
        config=_config_to_model(template.config),
        is_builtin=template.is_builtin,
        created_at=template.created_at.isoformat(),
        updated_at=template.updated_at.isoformat(),
        cloned_from_id=template.cloned_from_id,
        name_i18n=template.name_i18n,
        description_i18n=template.description_i18n,
        display_name_i18n=template.display_name_i18n,
        persona_i18n=template.persona_i18n,
    )


# ---- Router factory -------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the agent-template REST router bound to ``container``."""
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    @router.get(
        "/agent-templates",
        response_model=AgentTemplateListResponse,
    )
    async def list_agent_templates() -> AgentTemplateListResponse:
        rows = await container.chat.list_agent_templates_use_case.execute()
        return AgentTemplateListResponse(
            items=[_template_to_item(t) for t in rows],
        )

    @router.post(
        "/agent-templates",
        response_model=AgentTemplateItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_agent_template(
        body: CreateAgentTemplateRequest,
    ) -> AgentTemplateItem:
        template = await container.chat.create_agent_template_use_case.execute(
            CreateAgentTemplateInput(
                name=body.name,
                description=body.description,
                display_name=body.display_name,
                model_id=body.model_id,
                persona=body.persona,
                config=body.config.to_blob() if body.config is not None else None,
            ),
        )
        return _template_to_item(template)

    @router.patch(
        "/agent-templates/{template_id}",
        response_model=AgentTemplateItem,
    )
    async def update_agent_template(
        template_id: str,
        body: UpdateAgentTemplateRequest,
    ) -> AgentTemplateItem:
        sent = body.model_fields_set
        kwargs: dict[str, Any] = {}
        if "name" in sent:
            kwargs["name"] = body.name
        if "description" in sent:
            kwargs["description"] = body.description
        if "display_name" in sent:
            kwargs["display_name"] = body.display_name
        if "model_id" in sent:
            kwargs["model_id"] = body.model_id
        if "persona" in sent:
            kwargs["persona"] = body.persona
        if "config" in sent:
            kwargs["config"] = (
                body.config.to_blob() if body.config is not None else None
            )
        template = await container.chat.update_agent_template_use_case.execute(
            UpdateAgentTemplateInput(
                template_id=AgentTemplateId.of(template_id),
                **kwargs,
            ),
        )
        return _template_to_item(template)

    @router.delete(
        "/agent-templates/{template_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def delete_agent_template(template_id: str) -> None:
        await container.chat.delete_agent_template_use_case.execute(
            DeleteAgentTemplateInput(
                template_id=AgentTemplateId.of(template_id),
            ),
        )
        return None

    @router.post(
        "/agent-templates/{template_id}/apply",
        response_model=ApplyAgentTemplateResponse,
    )
    async def apply_agent_template(
        template_id: str,
        body: ApplyAgentTemplateRequest,
    ) -> ApplyAgentTemplateResponse:
        created: Participant = (
            await container.chat.apply_agent_template_use_case.execute(
                ApplyAgentTemplateInput(
                    template_id=AgentTemplateId.of(template_id),
                    conversation_id=ConversationId.of(body.conversation_id),
                ),
            )
        )
        return ApplyAgentTemplateResponse(
            conversation_id=body.conversation_id,
            participant_id=created.id.value,
        )

    @router.post(
        "/agent-templates/{template_id}/clone",
        response_model=AgentTemplateItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def clone_agent_template(template_id: str) -> AgentTemplateItem:
        template = await container.chat.clone_agent_template_use_case.execute(
            CloneAgentTemplateInput(
                template_id=AgentTemplateId.of(template_id),
            ),
        )
        return _template_to_item(template)

    @router.post(
        "/agent-templates/{template_id}/reset",
        response_model=AgentTemplateItem,
    )
    async def reset_agent_template(template_id: str) -> AgentTemplateItem:
        template = await container.chat.reset_agent_template_use_case.execute(
            ResetAgentTemplateInput(
                template_id=AgentTemplateId.of(template_id),
            ),
        )
        return _template_to_item(template)

    return router


__all__ = ["build_router"]
