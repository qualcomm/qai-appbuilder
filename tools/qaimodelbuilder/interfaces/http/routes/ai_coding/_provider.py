# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-provider handler registration (``_register_provider_routes``).

The 11 shared CC/OC endpoints. Extracted verbatim from the former
single-file ``ai_coding.py`` (zero behaviour change).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import APIRouter, Path, Query, status
from fastapi.responses import StreamingResponse

from qai.ai_coding.application.use_cases.decide_permission import (
    DecidePermissionCommand,
)
from qai.ai_coding.application.use_cases.health_status import (
    HealthStatusQuery,
)
from qai.ai_coding.application.use_cases.invoke_tool import InvokeToolCommand
from qai.ai_coding.application.use_cases.list_coding_sessions import (
    ListCodingSessionsQuery,
)
from qai.ai_coding.application.use_cases.manage_skills import RegisterSkillCommand
from qai.ai_coding.application.use_cases.request_permission import (
    RequestPermissionCommand,
)
from qai.ai_coding.application.use_cases.spawn_coding_session import (
    SpawnCodingSessionCommand,
)
from qai.ai_coding.application.use_cases.stream_coding_session import (
    StreamCodingSessionCommand,
)
from qai.ai_coding.application.use_cases.terminate_coding_session import (
    TerminateCodingSessionCommand,
)
from qai.ai_coding.domain import (
    CodingSessionId,
    MessageContent,
    PermissionDecision,
    PermissionRequestId,
    Provider,
    Skill,
    ToolName,
    Workspace,
)

from ._dto import (
    CodingSessionResponse,
    DecidePermissionBody,
    HealthResponse,
    InvokeToolRequest,
    PermissionRequestResponse,
    RequestPermissionBody,
    SessionListResponse,
    SkillListResponse,
    SkillRequest,
    SkillResponse,
    SpawnSessionRequest,
    TerminateSessionResponse,
    ToolInvocationResponse,
    _build_session_config,
    _permission_request_to_response,
    _session_to_response,
    _skill_to_response,
    _stream_frame_to_sse,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


def _register_provider_routes(
    router: APIRouter,
    *,
    container: "Container",
    provider: Provider,
) -> None:
    """Attach the 11 endpoints for a single provider onto ``router``.

    The ``provider`` value is captured by closure on each handler so the
    same use cases serve both CC and OC; this is the route-layer
    realisation of the PR-023 "1 ports / N adapters" design.
    """
    services = container.ai_coding

    # -- sessions ----------------------------------------------------------

    @router.post(
        "/sessions",
        response_model=CodingSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def spawn_session(body: SpawnSessionRequest) -> CodingSessionResponse:
        session = await services.spawn_coding_session_use_case.execute(
            SpawnCodingSessionCommand(
                provider=provider,
                workspace=Workspace(path=body.workspace),
                initial_prompt=(
                    MessageContent(text=body.initial_prompt)
                    if body.initial_prompt is not None
                    else None
                ),
                title=body.title,
                config=_build_session_config(body.config),
            )
        )
        return _session_to_response(session)

    @router.get("/sessions", response_model=SessionListResponse)
    async def list_active_sessions() -> SessionListResponse:
        sessions = await services.list_coding_sessions_use_case.execute(
            ListCodingSessionsQuery(scope="active")
        )
        return SessionListResponse(
            sessions=[
                _session_to_response(s)
                for s in sessions
                if s.provider is provider
            ]
        )

    @router.get("/sessions/history/all", response_model=SessionListResponse)
    async def list_all_sessions() -> SessionListResponse:
        sessions = await services.list_coding_sessions_use_case.execute(
            ListCodingSessionsQuery(scope="all")
        )
        return SessionListResponse(
            sessions=[
                _session_to_response(s)
                for s in sessions
                if s.provider is provider
            ]
        )

    @router.delete(
        "/sessions/{session_id}",
        response_model=TerminateSessionResponse,
    )
    async def terminate_session(
        session_id: str = Path(..., min_length=1, max_length=128),
        reason: str = Query(default="user_request", max_length=256),
    ) -> TerminateSessionResponse:
        await services.terminate_coding_session_use_case.execute(
            TerminateCodingSessionCommand(
                session_id=CodingSessionId(value=session_id),
                reason=reason,
            )
        )
        return TerminateSessionResponse(session_id=session_id, status="terminated")

    # -- streaming ---------------------------------------------------------

    @router.get("/sessions/{session_id}/stream")
    async def stream_session(
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> StreamingResponse:
        # Resolve the session up-front so any synchronous lookup error
        # (session not found, terminated) is translated by the unified
        # error handler with the correct status code instead of being
        # buried inside an SSE body. We then hand the iterator to
        # StreamingResponse which will trigger ``mark_streaming`` and
        # frame consumption.
        sid = CodingSessionId(value=session_id)
        # Touch the repository directly so 404 / 422 surface here.
        session = await services.coding_session_repository.get(sid)
        if session.status.value == "terminated":
            from qai.ai_coding.domain.errors import (
                CodingSessionAlreadyTerminatedError,
            )

            raise CodingSessionAlreadyTerminatedError(
                message=f"coding session {sid} is terminated",
                details={"session_id": str(sid)},
            )

        iterator = await services.stream_coding_session_use_case.execute(
            StreamCodingSessionCommand(session_id=sid)
        )

        async def _body() -> AsyncIterator[bytes]:
            async for frame in iterator:
                yield _stream_frame_to_sse(frame).encode("utf-8")
            # Always emit a closing ``done`` so the client knows the
            # stream finished cleanly even when the use case did not
            # yield an END frame itself.
            yield b"event: done\ndata: {}\n\n"

        return StreamingResponse(
            _body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- tools -------------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/tools/invoke",
        response_model=ToolInvocationResponse,
    )
    async def invoke_tool(
        body: InvokeToolRequest,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> ToolInvocationResponse:
        invocation = await services.invoke_tool_use_case.execute(
            InvokeToolCommand(
                session_id=CodingSessionId(value=session_id),
                tool_name=ToolName(value=body.tool_name),
                args=dict(body.args),
            )
        )
        return ToolInvocationResponse(
            invocation_id=str(invocation.invocation_id),
            tool_name=str(invocation.tool_name),
            status=invocation.status,
            duration_ms=invocation.duration_ms,
            result=invocation.result,
            error_code=invocation.error_code,
        )

    # -- permissions -------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/permissions",
        response_model=PermissionRequestResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def request_permission(
        body: RequestPermissionBody,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> PermissionRequestResponse:
        request = await services.request_permission_use_case.execute(
            RequestPermissionCommand(
                session_id=CodingSessionId(value=session_id),
                tool_name=ToolName(value=body.tool_name),
                args=dict(body.args),
            )
        )
        return _permission_request_to_response(request)

    @router.post(
        "/permissions/{request_id}/decide",
        response_model=PermissionRequestResponse,
    )
    async def decide_permission(
        body: DecidePermissionBody,
        request_id: str = Path(..., min_length=1, max_length=128),
    ) -> PermissionRequestResponse:
        decision = (
            PermissionDecision.APPROVED
            if body.decision == "approved"
            else PermissionDecision.REJECTED
        )
        decided = await services.decide_permission_use_case.execute(
            DecidePermissionCommand(
                session_id=CodingSessionId(value=body.session_id),
                request_id=PermissionRequestId(value=request_id),
                decision=decision,
                updated_input=body.updated_input,
                updated_permissions=body.updated_permissions,
            )
        )
        return _permission_request_to_response(decided)

    # -- skills ------------------------------------------------------------

    @router.get("/skills", response_model=SkillListResponse)
    async def discover_skills() -> SkillListResponse:
        skills = await services.discover_skills_use_case.execute()
        return SkillListResponse(skills=[_skill_to_response(s) for s in skills])

    @router.post(
        "/skills",
        response_model=SkillResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def register_skill(body: SkillRequest) -> SkillResponse:
        skill = await services.register_skill_use_case.execute(
            RegisterSkillCommand(
                skill=Skill(
                    name=body.name,
                    description=body.description,
                    spec=dict(body.spec),
                )
            )
        )
        return _skill_to_response(skill)

    # -- health ------------------------------------------------------------

    @router.get("/health", response_model=HealthResponse)
    async def health(
        refresh: int = Query(
            default=0,
            ge=0,
            le=1,
            description=(
                "When 1, bypass the model-catalog 5-minute cache and "
                "re-enumerate the upstream /v1/models (V1 ?refresh=1 "
                "parity for the model-source badge's 🔄 button)."
            ),
        ),
    ) -> HealthResponse:
        # PR-105: delegate to :class:`HealthStatusUseCase` which folds
        # the legacy ``/providers`` + ``/models`` response shapes into
        # a single payload.  Existing clients that only read the
        # ``provider`` / ``available`` / ``available_providers``
        # fields continue to work.  C1: ``refresh`` forces the model
        # catalog to bypass its cache.
        result = await services.health_status_use_case.execute(
            HealthStatusQuery(provider=provider, refresh=bool(refresh))
        )
        return HealthResponse(
            provider=result.provider,
            available=result.available,
            available_providers=list(result.available_providers),
            providers=[
                {
                    "id": p.id,
                    "name": p.name,
                    "available": p.available,
                }
                for p in result.providers
            ],
            models=[
                {
                    "id": m.id,
                    "name": m.name,
                    "provider_id": m.provider_id,
                }
                for m in result.models
            ],
            # U-5: legacy V1 footer parity (additive).
            sdk_available=result.sdk_available,
            sdk_version=result.sdk_version,
            auth_configured=result.auth_configured,
            auth_source=result.auth_source,
            active_sessions=result.active_sessions,
            total_sessions=result.total_sessions,
            # C1: model-source badge parity (additive).
            models_source=result.models_source,
            models_base_url=result.models_base_url,
            models_base_url_source=result.models_base_url_source,
            models_error=result.models_error,
            models_cached_age=result.models_cached_age,
        )
