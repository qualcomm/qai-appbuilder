# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Collaboration-mode HTTP routes (``/api/chat/mode-templates``).

A **mode template** is a named collaboration mode ("怎么协作": 讨论 / 评审 / 辩论 /
实施 / custom) — the third tier of the three-tier template system (§26 / §27).
These endpoints let the mode panel:

* list built-in presets + the user's saved modes;
* save / patch / delete a saved mode (the five-tuple of §26.1);
* apply (select) a mode for a conversation (sets ``selected_mode_id`` —
  the USER-EXPLICIT privilege-selection action of §26.4).

PURE V2 enhancement (V1 has no multi-agent discussion; 细则 4-bis 受保护). New
routes only — no existing path / method / payload changed (§3.1). Handlers are
thin (``interfaces-stays-thin``): parse → one ``execute(...)`` → serialise.
Domain errors propagate to the global error middleware
(``ModeTemplateNotFoundError`` → 404, ``ModeTemplateReadOnlyError`` → 400).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from qai.chat.application.use_cases.mode_template_management import (
    ApplyModeTemplateInput,
    CloneModeTemplateInput,
    CreateModeTemplateInput,
    DeleteModeTemplateInput,
    ResetModeTemplateInput,
    UpdateModeTemplateInput,
)
from qai.chat.domain.ids import ConversationId, ModeTemplateId
from qai.chat.domain.mode_template import ModeTemplate, lint_mode

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---- DTOs -----------------------------------------------------------------


class ModeLintIssueBody(BaseModel):
    """One advisory framing↔tool-policy conflict surfaced to the user (§26.8)."""

    severity: str
    code: str
    message: str


class HardConstraintsBody(BaseModel):
    """Meeting-room soft constraints REQUEST body (decisions 3+9, §26.8 / §7.3).

    Structured (vs a bare ``dict``) so Pydantic rejects out-of-range values with
    422 at the edge — instead of letting them reach the domain ``__post_init__``
    and surface as a 500. Bounds mirror :class:`ModeHardConstraints` exactly
    (chars 50–5000, seconds 5–600). Either field ``null`` = that constraint is
    not enabled. The RESPONSE side (`ModeTemplateItem.hard_constraints`) stays a
    plain ``dict | None`` so the output shape is unchanged (§3.1).
    """

    max_chars_per_turn: int | None = Field(default=None, ge=50, le=5000)
    max_seconds_per_turn: int | None = Field(default=None, ge=5, le=600)


class ModeTemplateItem(BaseModel):
    """Single :class:`ModeTemplate` projection (identity / framing / policies)."""

    id: str
    name: str
    description: str
    framing: str
    tool_policy: dict[str, Any]
    flow_policy: dict[str, Any]
    is_builtin: bool
    created_at: str
    updated_at: str
    #: Meeting-room soft constraints (decisions 3+9, §26.8). Tail-appended (§3.1);
    #: ``{"max_chars_per_turn": int|null, "max_seconds_per_turn": int|null}`` —
    #: either field ``null`` = that constraint is not enabled.
    hard_constraints: dict[str, Any] | None = None
    #: Advisory framing↔tool-policy soft conflicts (§26.8). Tail-appended (§3.1);
    #: ``[]`` when clean. Surfaced so the UI can warn the user (never hard-blocks
    #: — the tool policy is the real gate).
    lint_issues: list[ModeLintIssueBody] = Field(default_factory=list)
    #: When this mode was cloned from another (e.g. a built-in preset), the
    #: SOURCE template's id; ``None`` for originals. Tail-appended (§3.1).
    cloned_from_id: str | None = None
    #: Multi-language (i18n) maps for built-in presets (migration 056), each a
    #: ``{"en":..,"zh-CN":..,"zh-TW":..}`` object. ``None`` for custom rows →
    #: the frontend falls back to the single-language ``name`` / ``description``
    #: / ``framing`` fields above. Tail-appended (§3.1) so the wire shape stays
    #: backward-compatible; lets the UI localise built-in template text without
    #: duplicating the translations into the frontend locale files (single
    #: source of truth = the seed → DB).
    name_i18n: dict[str, str] | None = None
    description_i18n: dict[str, str] | None = None
    framing_i18n: dict[str, str] | None = None


class ModeTemplateListResponse(BaseModel):
    """``GET /api/chat/mode-templates`` body."""

    items: list[ModeTemplateItem]


class CreateModeTemplateRequest(BaseModel):
    """``POST /api/chat/mode-templates`` body."""

    name: str = Field(default="", max_length=256)
    description: str = Field(default="", max_length=2000)
    framing: str = Field(default="", max_length=8000)
    tool_policy: dict[str, Any] | None = Field(default=None)
    flow_policy: dict[str, Any] | None = Field(default=None)
    hard_constraints: HardConstraintsBody | None = Field(default=None)


class UpdateModeTemplateRequest(BaseModel):
    """``PATCH /api/chat/mode-templates/{id}`` body (PATCH semantics).

    Only fields the client SENDS are applied; field-presence is read from
    ``model_fields_set``.
    """

    name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    framing: str | None = Field(default=None, max_length=8000)
    tool_policy: dict[str, Any] | None = Field(default=None)
    flow_policy: dict[str, Any] | None = Field(default=None)
    hard_constraints: HardConstraintsBody | None = Field(default=None)


class ApplyModeTemplateRequest(BaseModel):
    """``POST /api/chat/mode-templates/{id}/apply`` body."""

    conversation_id: str = Field(min_length=1, max_length=64)
    #: auto / manual / locked / suggested (§26.2); defaults to explicit manual.
    selection_policy: str = Field(default="manual", max_length=32)


class ApplyModeTemplateResponse(BaseModel):
    """``POST /api/chat/mode-templates/{id}/apply`` result."""

    conversation_id: str
    mode_id: str
    mode_name: str
    selection_policy: str


class ModeTemplateUsageResponse(BaseModel):
    """``GET /api/chat/mode-templates/{id}/usage`` result.

    Count of conversations currently selecting this mode
    (``meta["discussion"]["selected_mode_id"] == id``). The delete-confirm
    dialog uses it to warn the user how many conversations will be reverted to
    the sentinel ("跟随默认") if they delete the mode (decision 7).
    """

    mode_id: str
    conversation_count: int


# ---- Helpers --------------------------------------------------------------


def _template_to_item(template: ModeTemplate) -> ModeTemplateItem:
    return ModeTemplateItem(
        id=template.id.value,
        name=template.name,
        description=template.description,
        framing=template.framing,
        tool_policy=template.tool_policy.to_dict(),
        flow_policy=template.flow_policy.to_dict(),
        is_builtin=template.is_builtin,
        created_at=template.created_at.isoformat(),
        updated_at=template.updated_at.isoformat(),
        hard_constraints=template.hard_constraints.to_dict(),
        lint_issues=[
            ModeLintIssueBody(
                severity=issue.severity, code=issue.code, message=issue.message
            )
            for issue in lint_mode(template)
        ],
        cloned_from_id=template.cloned_from_id,
        name_i18n=template.name_i18n,
        description_i18n=template.description_i18n,
        framing_i18n=template.framing_i18n,
    )


# ---- Router factory -------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the mode-template REST router bound to ``container``."""
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    @router.get(
        "/mode-templates",
        response_model=ModeTemplateListResponse,
    )
    async def list_mode_templates() -> ModeTemplateListResponse:
        rows = await container.chat.list_mode_templates_use_case.execute()
        return ModeTemplateListResponse(
            items=[_template_to_item(t) for t in rows],
        )

    @router.post(
        "/mode-templates",
        response_model=ModeTemplateItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_mode_template(
        body: CreateModeTemplateRequest,
    ) -> ModeTemplateItem:
        template = await container.chat.create_mode_template_use_case.execute(
            CreateModeTemplateInput(
                name=body.name,
                description=body.description,
                framing=body.framing,
                tool_policy=body.tool_policy,
                flow_policy=body.flow_policy,
                # Normalise the validated submodel back to a plain dict so the
                # domain ModeHardConstraints.from_dict (isinstance dict) consumes
                # it; None stays None (no constraint).
                hard_constraints=(
                    body.hard_constraints.model_dump()
                    if body.hard_constraints is not None
                    else None
                ),
            ),
        )
        return _template_to_item(template)

    @router.patch(
        "/mode-templates/{template_id}",
        response_model=ModeTemplateItem,
    )
    async def update_mode_template(
        template_id: str,
        body: UpdateModeTemplateRequest,
    ) -> ModeTemplateItem:
        sent = body.model_fields_set
        kwargs: dict[str, Any] = {}
        for fieldname in (
            "name",
            "description",
            "framing",
            "tool_policy",
            "flow_policy",
            "hard_constraints",
        ):
            if fieldname in sent:
                value = getattr(body, fieldname)
                # hard_constraints is a validated submodel → normalise to a
                # plain dict (or None) for the domain layer (PATCH semantics:
                # sending null clears the constraints).
                if fieldname == "hard_constraints" and value is not None:
                    value = value.model_dump()
                kwargs[fieldname] = value
        template = await container.chat.update_mode_template_use_case.execute(
            UpdateModeTemplateInput(
                template_id=ModeTemplateId.of(template_id),
                **kwargs,
            ),
        )
        return _template_to_item(template)

    @router.delete(
        "/mode-templates/{template_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def delete_mode_template(template_id: str) -> None:
        await container.chat.delete_mode_template_use_case.execute(
            DeleteModeTemplateInput(
                template_id=ModeTemplateId.of(template_id),
            ),
        )
        return None

    @router.post(
        "/mode-templates/{template_id}/apply",
        response_model=ApplyModeTemplateResponse,
    )
    async def apply_mode_template(
        template_id: str,
        body: ApplyModeTemplateRequest,
    ) -> ApplyModeTemplateResponse:
        result = await container.chat.apply_mode_template_use_case.execute(
            ApplyModeTemplateInput(
                template_id=ModeTemplateId.of(template_id),
                conversation_id=ConversationId.of(body.conversation_id),
                selection_policy=body.selection_policy,
            ),
        )
        return ApplyModeTemplateResponse(
            conversation_id=result.conversation_id,
            mode_id=result.mode_id,
            mode_name=result.mode_name,
            selection_policy=result.selection_policy,
        )

    @router.get(
        "/mode-templates/{template_id}/usage",
        response_model=ModeTemplateUsageResponse,
    )
    async def mode_template_usage(template_id: str) -> ModeTemplateUsageResponse:
        count = await container.chat.count_mode_template_usage_use_case.execute(
            ModeTemplateId.of(template_id),
        )
        return ModeTemplateUsageResponse(
            mode_id=template_id,
            conversation_count=count,
        )

    @router.post(
        "/mode-templates/{template_id}/clone",
        response_model=ModeTemplateItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def clone_mode_template(template_id: str) -> ModeTemplateItem:
        template = await container.chat.clone_mode_template_use_case.execute(
            CloneModeTemplateInput(
                template_id=ModeTemplateId.of(template_id),
            ),
        )
        return _template_to_item(template)

    @router.post(
        "/mode-templates/{template_id}/reset",
        response_model=ModeTemplateItem,
    )
    async def reset_mode_template(template_id: str) -> ModeTemplateItem:
        template = await container.chat.reset_mode_template_use_case.execute(
            ResetModeTemplateInput(
                template_id=ModeTemplateId.of(template_id),
            ),
        )
        return _template_to_item(template)

    return router


__all__ = ["build_router"]
