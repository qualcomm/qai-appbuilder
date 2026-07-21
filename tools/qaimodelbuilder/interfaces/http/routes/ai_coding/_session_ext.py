# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Session-extension routes (``_register_session_extension_routes``).

abort / revert / checkpoint / rewind / context_usage / context_size.
Mounted on BOTH the CC and OC sub-routers.  Extracted verbatim from the
former single-file ``ai_coding.py`` (zero behaviour change).  The DTOs
in this group are used only by this module, so they live here rather
than in the shared ``_dto`` module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, Field

from qai.ai_coding.application.use_cases.abort_revert import (
    AbortSessionCommand,
    RevertMessageCommand,
)
from qai.ai_coding.application.use_cases.manage_checkpoints import (
    CreateCheckpointCommand,
    ListCheckpointsQuery,
    RewindCheckpointCommand,
)
from qai.ai_coding.application.use_cases.query_context_usage import (
    ContextUsageQuery,
)
from qai.ai_coding.domain import (
    CodingSessionId,
    Provider,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


class AbortResponse(BaseModel):
    """Body of ``POST /api/{cc|oc}/sessions/{id}/abort`` (PR-105).

    Wire shape mirrors the legacy OC response
    ``{"ok": true, "session_id": "..."}`` from
    ``backend/ai_coding/opencode_api_routes.py``.
    """

    ok: bool
    session_id: str


class RevertRequest(BaseModel):
    """Body of ``POST /api/{cc|oc}/sessions/{id}/revert`` (PR-105).

    Accepts EITHER ``message_id`` (legacy ``<provider>-user-<n>``
    string) OR ``after_index`` (new 0-based int).  The route layer
    resolves both to a ``marker_index`` before invoking the use case.
    """

    message_id: str | None = Field(default=None, max_length=128)
    after_index: int | None = Field(default=None, ge=0)
    # OpenCode's native revert API also accepts ``part_id``; the
    # field is preserved for wire-shape parity but the new
    # implementation does not differentiate (PR-108c may wire it).
    part_id: str | None = Field(default=None, max_length=128)


class RevertResponse(BaseModel):
    """Body of ``POST /api/{cc|oc}/sessions/{id}/revert`` (PR-105)."""

    ok: bool
    session_id: str
    removed: int
    remaining: int


class CreateCheckpointRequest(BaseModel):
    """Body of ``POST /api/{cc|oc}/sessions/{id}/checkpoint`` (PR-105)."""

    label: str | None = Field(default=None, max_length=128)


class CheckpointInfoEnvelope(BaseModel):
    """Wire shape of one :class:`CheckpointInfo` record."""

    checkpoint_id: str
    created_at: str
    label: str | None = None
    message_count: int


class CreateCheckpointResponse(BaseModel):
    """Body of ``POST /api/{cc|oc}/sessions/{id}/checkpoint`` (PR-105)."""

    ok: bool
    session_id: str
    checkpoint: CheckpointInfoEnvelope


class ListCheckpointsResponse(BaseModel):
    """Body of ``GET /api/{cc|oc}/sessions/{id}/checkpoints`` (C-3).

    V1 parity (``backend/ai_coding/api_routes.py:2074-2110``): list the
    per-session checkpoints the WebUI can rewind to.
    ``checkpointing_enabled`` mirrors V1's flag so the frontend knows
    whether file-checkpointing is active for this provider/session.
    """

    ok: bool
    session_id: str
    checkpoints: list[CheckpointInfoEnvelope]
    checkpointing_enabled: bool


class RewindCheckpointRequest(BaseModel):
    """Body of ``POST /api/{cc|oc}/sessions/{id}/rewind`` (PR-105)."""

    checkpoint_id: str = Field(..., min_length=1, max_length=128)


class RewindCheckpointResponse(BaseModel):
    """Body of ``POST /api/{cc|oc}/sessions/{id}/rewind`` (PR-105)."""

    ok: bool
    session_id: str
    checkpoint_id: str
    removed: int
    remaining: int
    # 2-H3 / CC SDK file checkpoint-rewind: whether the provider performed a
    # TRUE on-disk file restoration (SDK backend + enable_file_checkpointing)
    # vs a message-only rewind (HTTP backend / checkpointing off).  Tail-
    # appended per v2.7 §3.1 (additive) so the frontend can render an
    # accurate "files restored" vs "messages only" toast.
    files_rewound: bool = False


class ContextUsageResponse(BaseModel):
    """Body of ``GET /api/{cc|oc}/sessions/{id}/context_usage`` (PR-105).

    Mirrors the legacy CC SDK ``get_context_usage()`` shape
    ``{ok, totalTokens, maxTokens, percentage}``.
    """

    ok: bool
    total_tokens: int = Field(..., alias="totalTokens")
    max_tokens: int = Field(..., alias="maxTokens")
    percentage: float

    model_config = {"populate_by_name": True}


class ContextSizeResponse(BaseModel):
    """Body of ``GET /api/{cc|oc}/sessions/{id}/context_size`` (PR-105).

    Mirrors the legacy OC ``GET /api/oc/sessions/{id}/context_size``
    eight-key payload.  The token counters are now populated from the
    :class:`CodingSession` aggregate's cumulative usage fields (written
    back by :class:`StreamCodingSessionUseCase` from each turn's provider
    ``usage`` frame — see ``query_context_usage.GetContextSizeUseCase``);
    they are no longer hard-zeroed.  A brand-new session that has not
    streamed a turn yet legitimately reports ``0`` (no usage observed).
    """

    # OpenAPI ``$ref`` key stability (pure-refactor pin): this short name
    # collides with ``chat._rest.ContextSizeResponse``.  Pydantic
    # disambiguates the JSON-schema ``$ref`` key by the model's
    # ``__module__``; pin it to the package module so the disambiguated
    # key matches the pre-split single-file ``ai_coding`` module.  All
    # fields are builtins so no deferred forward-ref resolution is needed.
    __module__ = "interfaces.http.routes.ai_coding"

    last_input_tokens: int
    total_input_tokens: int
    total_output_tokens: int
    total_tool_calls: int
    turn_count: int
    context_limit: int
    usage_pct: float
    model: str


def _register_session_extension_routes(
    router: APIRouter,
    *,
    container: "Container",
    provider: Provider,
    history_id_prefix: str,
) -> None:
    """Attach abort + revert + checkpoint + context routes.

    Mounted on BOTH the CC and OC sub-routers; the wire shapes are
    identical (per the prompt's PR-105 task list).  ``provider`` is
    captured for future use-case dispatch when (PR-108c) the
    checkpoint/rewind path differentiates by provider.
    """
    services = container.ai_coding

    async def _resolve_checkpointing_enabled(
        services_ref: object, _session_id: str
    ) -> bool:
        """Best-effort read of ``enable_file_checkpointing`` (C-3).

        V1 (``backend/ai_coding/api_routes.py:2101-2102``) read the flag
        off the CC config document (``cc_config.enable_file_checkpointing``).
        V2 keeps the flag in the per-provider coding-config doc; pick the
        CC vs OC config use case by ``provider`` and read it back.  Never
        raises — a missing use case / read failure reports ``False`` so
        the list still returns (the flag is informational for the UI).
        """
        cfg_uc_name = (
            "get_coding_config_use_case"
            if provider is Provider.CLAUDE_CODE
            else "get_oc_coding_config_use_case"
        )
        cfg_uc = getattr(services_ref, cfg_uc_name, None)
        if cfg_uc is None:
            return False
        try:
            doc = await cfg_uc.execute()
        except Exception:  # noqa: BLE001 — informational flag, never abort
            return False
        if isinstance(doc, dict):
            return bool(doc.get("enable_file_checkpointing", False))
        return bool(getattr(doc, "enable_file_checkpointing", False))

    # -- abort --------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/abort",
        response_model=AbortResponse,
    )
    async def abort_session(
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> AbortResponse:
        await services.abort_session_use_case.execute(
            AbortSessionCommand(session_id=CodingSessionId(value=session_id))
        )
        return AbortResponse(ok=True, session_id=session_id)

    # -- revert -------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/revert",
        response_model=RevertResponse,
    )
    async def revert_message(
        body: RevertRequest,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> RevertResponse:
        # Resolve marker_index from either explicit ``after_index``
        # or the legacy ``message_id`` string.
        marker_index: int | None = body.after_index
        if marker_index is None and body.message_id:
            for accepted in (history_id_prefix, "cc-user-", "oc-user-"):
                if body.message_id.startswith(accepted):
                    tail = body.message_id[len(accepted):]
                    if tail.isdigit():
                        marker_index = int(tail)
                        break
        if marker_index is None:
            from qai.platform.errors import ValidationError as _PV

            raise _PV(
                code="ai_coding.invalid_revert_params",
                message=(
                    "either after_index (int) or message_id "
                    "('<provider>-user-<n>') is required"
                ),
                field_errors={
                    "after_index": ["required when message_id is absent"],
                },
            )

        result = await services.revert_message_use_case.execute(
            RevertMessageCommand(
                session_id=CodingSessionId(value=session_id),
                marker_index=marker_index,
            )
        )
        return RevertResponse(
            ok=True,
            session_id=session_id,
            removed=result.removed,
            remaining=result.remaining,
        )

    # -- create checkpoint --------------------------------------------

    @router.post(
        "/sessions/{session_id}/checkpoint",
        response_model=CreateCheckpointResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_checkpoint(
        session_id: str = Path(..., min_length=1, max_length=128),
        body: CreateCheckpointRequest | None = None,
    ) -> CreateCheckpointResponse:
        label = body.label if body is not None else None
        result = await services.create_checkpoint_use_case.execute(
            CreateCheckpointCommand(
                session_id=CodingSessionId(value=session_id),
                label=label,
            )
        )
        return CreateCheckpointResponse(
            ok=True,
            session_id=session_id,
            checkpoint=CheckpointInfoEnvelope(
                checkpoint_id=result.checkpoint.checkpoint_id,
                created_at=result.checkpoint.created_at,
                label=result.checkpoint.label,
                message_count=result.checkpoint.message_count,
            ),
        )

    # -- list checkpoints (C-3: V1 ``GET /api/cc/sessions/{id}/checkpoints``)

    @router.get(
        "/sessions/{session_id}/checkpoints",
        response_model=ListCheckpointsResponse,
    )
    async def list_session_checkpoints(
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> ListCheckpointsResponse:
        """List the per-session checkpoints (V1 parity).

        Backs the V1 ``GET /api/cc/sessions/{id}/checkpoints`` route
        (``backend/ai_coding/api_routes.py:2074-2110``) whose use case
        (:class:`ListCheckpointsUseCase`) shipped in PR-105 but had no
        route mounted.  ``checkpointing_enabled`` is derived best-effort
        from the session's stored :class:`CodingSessionConfig`
        (``enable_file_checkpointing``) so the frontend can show whether
        a rewind will restore files (SDK backend) or only truncate the
        message history (HTTP backend / flag off).
        """
        result = await services.list_checkpoints_use_case.execute(
            ListCheckpointsQuery(
                session_id=CodingSessionId(value=session_id),
            )
        )
        return ListCheckpointsResponse(
            ok=True,
            session_id=session_id,
            checkpoints=[
                CheckpointInfoEnvelope(
                    checkpoint_id=cp.checkpoint_id,
                    created_at=cp.created_at,
                    label=cp.label,
                    message_count=cp.message_count,
                )
                for cp in result.checkpoints
            ],
            checkpointing_enabled=await _resolve_checkpointing_enabled(
                services, session_id
            ),
        )

    # -- rewind to checkpoint -----------------------------------------

    @router.post(
        "/sessions/{session_id}/rewind",
        response_model=RewindCheckpointResponse,
    )
    async def rewind_to_checkpoint(
        body: RewindCheckpointRequest,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> RewindCheckpointResponse:
        result = await services.rewind_checkpoint_use_case.execute(
            RewindCheckpointCommand(
                session_id=CodingSessionId(value=session_id),
                checkpoint_id=body.checkpoint_id,
            )
        )
        return RewindCheckpointResponse(
            ok=True,
            session_id=session_id,
            checkpoint_id=result.checkpoint_id,
            removed=result.removed,
            remaining=result.remaining,
            files_rewound=result.files_rewound,
        )

    # -- context_usage ------------------------------------------------

    @router.get(
        "/sessions/{session_id}/context_usage",
        response_model=ContextUsageResponse,
    )
    async def get_context_usage(
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> ContextUsageResponse:
        result = await services.get_context_usage_use_case.execute(
            ContextUsageQuery(
                session_id=CodingSessionId(value=session_id),
            )
        )
        return ContextUsageResponse(
            ok=result.ok,
            total_tokens=result.total_tokens,
            max_tokens=result.max_tokens,
            percentage=result.percentage,
        )

    # -- context_size -------------------------------------------------

    @router.get(
        "/sessions/{session_id}/context_size",
        response_model=ContextSizeResponse,
    )
    async def get_context_size(
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> ContextSizeResponse:
        result = await services.get_context_size_use_case.execute(
            ContextUsageQuery(
                session_id=CodingSessionId(value=session_id),
            )
        )
        return ContextSizeResponse(
            last_input_tokens=result.last_input_tokens,
            total_input_tokens=result.total_input_tokens,
            total_output_tokens=result.total_output_tokens,
            total_tool_calls=result.total_tool_calls,
            turn_count=result.turn_count,
            context_limit=result.context_limit,
            usage_pct=result.usage_pct,
            model=result.model,
        )
