# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Security routes — skill_policy + channels + skills + skill-discovery + templates endpoints. (split from security.py).

Pure-move extraction (zero behaviour change): the route handlers are
byte-identical to the originals; they were nested closures inside
``build_router`` and are now nested inside this registrar instead,
still capturing the ``container`` passed in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Response, status

from qai.platform.errors import NotFoundError, ValidationError
from qai.security.application.use_cases import skill_discovery as _skill_discovery
from qai.security.application.use_cases.security_templates import template_catalog
from qai.security.domain.entities import ChannelPolicy
from qai.security.domain.skill_capability import SkillCapability, SkillCapabilityViolation
from qai.security.domain.value_objects import AskQuotaWindow, Channel

from ._dto import (
    ApplyTemplateRequest,
    ApplyTemplateResponse,
    ChannelPolicyResponse,
    ChannelPolicyUpdateRequest,
    ChannelsListResponse,
    SkillPolicyResponse,
    SkillPolicyUpdateRequest,
    SkillRegisterRequest,
    SkillRegisterResponse,
    SkillsListResponse,
    _policy_to_response,
    _skill_policy_view_to_dto,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from apps.api.di import Container


def _register_skills_routes(router: "APIRouter", *, container: "Container") -> None:
    # ── skill_policy (1 read + 1 write) ───────────────────────────────

    @router.get(
        "/skill_policy/{skill_name}",
        response_model=SkillPolicyResponse,
    )
    async def skill_policy_get(
        skill_name: str,
    ) -> SkillPolicyResponse:
        use_case = container.security.get_skill_policy_use_case
        if not await use_case.is_known(skill_name):
            raise NotFoundError(
                "security.skill_policy.not_found",
                "skill_policy",
                skill_name,
            )
        view = await use_case.execute(skill_name=skill_name)
        return _skill_policy_view_to_dto(view)

    @router.put(
        "/skill_policy/{skill_name}",
        response_model=SkillPolicyResponse,
    )
    async def skill_policy_put(
        skill_name: str,
        body: SkillPolicyUpdateRequest,
    ) -> SkillPolicyResponse:
        """Persist a per-skill ``read`` / ``write`` / ``trusted_binaries``
        override (V1 ``saveSkillPolicy`` parity).

        The override is stored in the in-memory
        :class:`SecurityRuntimeStateService` ``skill_policies`` bucket
        (process-local, by design — see
        :mod:`qai.security.application.security_runtime_state`). The
        capability registry itself is not mutated; the override is
        layered on top by :class:`GetSkillPolicyUseCase` and by any code
        that reads the bucket at runtime.
        """
        if not skill_name or not isinstance(skill_name, str):
            raise ValidationError(
                "security.skill_policy.invalid_name",
                "skill_name must be a non-empty string",
                field_errors={"skill_name": ["non-empty string required"]},
            )
        _skill_discovery.write_skill_policy_override(
            container.security.security_runtime_state,
            skill_name=skill_name,
            read=body.read,
            write=body.write,
            trusted_binaries=body.trusted_binaries,
        )
        view = await container.security.get_skill_policy_use_case.execute(
            skill_name=skill_name,
        )
        return _skill_policy_view_to_dto(view)

    # ── channels (2) ──────────────────────────────────────────────────

    @router.get("/channels", response_model=ChannelsListResponse)
    async def channels_list() -> ChannelsListResponse:
        repo = container.security.channel_policy_repository
        policies = await repo.list_all()
        return ChannelsListResponse(
            channels=[
                {
                    "name": p.name,
                    "requires_ui": p.requires_ui,
                    "description": p.description,
                    "quota": (
                        {
                            "window_seconds": p.quota.window_seconds,
                            "max_asks": p.quota.max_asks,
                        }
                        if p.quota is not None
                        else None
                    ),
                }
                for p in policies
            ],
        )

    @router.put(
        "/channels/{name}", response_model=ChannelPolicyResponse
    )
    async def channels_put(
        name: str,
        body: ChannelPolicyUpdateRequest,
    ) -> ChannelPolicyResponse:
        try:
            channel = Channel(
                name=name, requires_ui=body.requires_ui
            )
        except ValueError as exc:
            raise ValidationError(
                "security.channel.invalid_name",
                f"channel name invalid: {exc}",
                field_errors={"name": [str(exc)]},
            ) from exc
        quota = None
        if body.quota is not None:
            try:
                quota = AskQuotaWindow(
                    window_seconds=body.quota.window_seconds,
                    max_asks=body.quota.max_asks,
                )
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    "security.channel.invalid_quota",
                    f"quota invalid: {exc}",
                    field_errors={"quota": [str(exc)]},
                ) from exc
        policy = ChannelPolicy(
            channel=channel,
            quota=quota,
            description=body.description or "",
        )
        await container.security.channel_policy_repository.save(policy)
        return ChannelPolicyResponse(
            name=policy.name,
            requires_ui=policy.requires_ui,
            description=policy.description,
            quota=(
                {
                    "window_seconds": policy.quota.window_seconds,
                    "max_asks": policy.quota.max_asks,
                }
                if policy.quota is not None
                else None
            ),
        )

    # ── skills (3) ────────────────────────────────────────────────────

    @router.get(
        "/skills/list_active", response_model=SkillsListResponse
    )
    async def skills_list_active() -> SkillsListResponse:
        active = (
            await container.security.skill_capability_registry.list_active()
        )
        return SkillsListResponse(
            skills=[
                {
                    "capability_name": c.capability_name,
                    "read_paths": list(c.read_paths),
                    "write_paths": list(c.write_paths),
                    "exec_paths": list(c.exec_paths),
                    "trusted_binaries": list(c.trusted_binaries),
                    "description": c.description,
                }
                for c in active
            ],
        )

    @router.post(
        "/skills/register",
        response_model=SkillRegisterResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def skills_register(
        body: SkillRegisterRequest,
    ) -> SkillRegisterResponse:
        try:
            cap = SkillCapability(
                capability_name=body.capability_name,
                read_paths=tuple(body.read_paths),
                write_paths=tuple(body.write_paths),
                exec_paths=tuple(body.exec_paths),
                trusted_binaries=tuple(body.trusted_binaries),
                description=body.description or "",
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "security.skill_capability.invalid",
                f"capability invalid: {exc}",
                field_errors={"capability": [str(exc)]},
            ) from exc
        try:
            result = (
                await container.security.register_skill_capability_use_case.execute(
                    skill_name=body.skill_name,
                    capability=cap,
                    skill_body=body.skill_body or "",
                )
            )
        except SkillCapabilityViolation as exc:
            # blocked by injection scanner — surface as 412
            raise ValidationError(
                exc.code,
                exc.message,
                field_errors={"skill_body": ["high-severity threat detected"]},
            ) from exc
        return SkillRegisterResponse(
            skill_name=result.skill_name,
            audit_id=result.audit_id,
            scanner_warnings=list(result.scanner_warnings),
        )

    @router.delete(
        "/skills/{skill_name}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def skills_delete(skill_name: str) -> Response:
        await (
            container.security.unregister_skill_capability_use_case.execute(
                skill_name=skill_name
            )
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ── skill-discovery (W1-H §8) ─────────────────────────────────────

    @router.get("/skill-discovery")
    async def skill_discovery() -> dict[str, Any]:
        """Return skill discovery scan results.

        Aggregates all registered skill capabilities and surfaces them
        as a discovery payload for the WebUI security panel.

        Response shape (existing fields are unchanged for backward
        compatibility, v2.7 §3.1; V1-aligned fields are tail-appended):

        * ``skills`` (list[dict]) — one entry per discovered skill,
          including *both* registered capabilities **and** orphan
          policy overrides whose capability is no longer registered
          (``active=false``). Each entry tail-appends V1 fields:
          ``source`` / ``active`` / ``has_policy`` / ``raw_read`` /
          ``raw_write`` / ``raw_trusted_binaries``.
        * ``total`` (int) — len(skills).
        * ``scan_status`` (str) — ``"complete"``.
        * ``by_name`` (dict) — V1-style dict keyed by skill_name; the
          WebUI ``SkillCapabilitiesPanel`` consumes this directly to
          render the two-section layout (built-in features vs agent
          skills) without re-keying client-side.

        The three-way aggregation (registry + orphan overrides +
        filesystem scan) with per-skill merge / dedup / classification
        lives in :class:`SkillDiscoveryUseCase` (R9 cohesion fix); this
        handler only assembles the outer envelope.
        """
        result = await container.security.skill_discovery_use_case.execute()
        return {
            "skills": result.skills,
            "total": len(result.skills),
            "scan_status": "complete",
            "by_name": result.by_name,
        }

    # ── security templates (W1-H §9) ──────────────────────────────────

    @router.get("/templates")
    async def security_templates() -> dict[str, Any]:
        """Return the built-in security policy templates (V1 parity).

        Lists the shipped ``demo`` / ``development`` / ``strict`` templates
        from :func:`template_catalog` — the same application-layer source of
        truth whose rule sets ``POST /templates`` applies — so each entry's
        ``rules_count`` is derived from the real rule set and can never drift
        from what an apply actually writes. V1 read the same fixed set from
        ``config/policy_templates/{id}.json`` at runtime and shipped no
        template-authoring UI; V2 keeps the catalog as a built-in set
        (out-of-box source mirrored at
        ``factory/_source/policy_templates/{id}.json``).
        """
        templates = template_catalog()
        return {
            "templates": templates,
            "total": len(templates),
        }

    @router.post("/templates", response_model=ApplyTemplateResponse)
    async def apply_security_template(
        body: ApplyTemplateRequest,
    ) -> ApplyTemplateResponse:
        """Apply a built-in policy template (V1 parity).

        Replaces the Policy's rule set with the template's rules via the
        locked :class:`UpdatePolicyUseCase` — the rules CRUD contract is
        reused, not bypassed, so a template apply triggers the same
        version bump + reboot-signal path as any manual rule edit.
        Unknown template ids raise :class:`NotFoundError` (404).
        """
        new_policy = await (
            container.security.apply_security_template_use_case.execute(
                template_id=body.template,
            )
        )
        if new_policy is None:
            raise NotFoundError(
                "security.template.not_found",
                "security_template",
                body.template,
            )
        return ApplyTemplateResponse(
            template=body.template,
            policy=_policy_to_response(new_policy, container=container),
        )
