# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Deferred legacy session routes (``_register_cc_only_routes``).

Mounted on BOTH the CC and OC sub-routers (per-mount prefix /
history-id / source captured via parameters).  Extracted verbatim from
the former single-file ``ai_coding.py`` (zero behaviour change).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import APIRouter, Path, status
from fastapi.responses import StreamingResponse

from qai.ai_coding.application.use_cases.change_workspace import (
    ChangeWorkspaceCommand,
)
from qai.ai_coding.application.use_cases.get_coding_session import (
    GetCodingSessionQuery,
)
from qai.ai_coding.application.use_cases.get_session_history import (
    GetSessionHistoryQuery,
)
from qai.ai_coding.application.use_cases.hard_delete_session import (
    HardDeleteSessionCommand,
)
from qai.ai_coding.application.use_cases.interrupt_session import (
    InterruptSessionCommand,
)
from qai.ai_coding.application.use_cases.rename_session import (
    RenameSessionCommand,
)
from qai.ai_coding.application.use_cases.restore_coding_session import (
    RestoreCodingSessionCommand,
)
from qai.ai_coding.application.use_cases.send_user_message import (
    SendUserMessageCommand,
)
from qai.ai_coding.application.use_cases.set_active_session import (
    SetActiveSessionCommand,
)
from qai.ai_coding.application.use_cases.set_session_effort import (
    SetSessionEffortCommand,
)
from qai.ai_coding.application.use_cases.set_session_notify import (
    SetSessionNotifyCommand,
)
from qai.ai_coding.application.use_cases.stream_coding_session import (
    StreamCodingSessionCommand,
)
from qai.ai_coding.application.use_cases.truncate_history import (
    TruncateHistoryCommand,
)
from qai.ai_coding.domain import (
    CodingSessionId,
    MessageContent,
    Provider,
    Workspace,
)

from ._dto import (
    EffortRequest,
    EffortResponse,
    FeishuNotifyRequest,
    FeishuNotifyResponse,
    GetSessionEnvelope,
    HardDeleteResponse,
    HistoryMessageEnvelope,
    HistoryResponse,
    InterruptResponse,
    RenameRequest,
    RenameResponse,
    RestoreRequest,
    RestoreResponse,
    SendMessageRequest,
    SendMessageResponse,
    SetActiveResponse,
    TruncateHistoryRequest,
    TruncateHistoryResponse,
    WechatNotifyRequest,
    WechatNotifyResponse,
    WorkingDirRequest,
    WorkingDirResponse,
    _session_to_response,
    _stream_frame_to_sse,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


def _register_cc_only_routes(
    router: APIRouter,
    *,
    container: "Container",
    provider: Provider = Provider.CLAUDE_CODE,
    url_prefix: str = "/api/cc",
    history_id_prefix: str = "cc-user-",
    history_source: str = "claude_code",
) -> None:
    """Attach the deferred legacy session routes onto ``router``.

    PR-104a originally shipped this helper for the CC sub-router only.
    PR-105 generalises it to support the OC sub-router with the same
    wire shapes — the URL prefix is captured from ``url_prefix`` and
    the history-id prefix (used by the synthetic ``GET /history``
    envelope and the ``truncate_history`` parser) is captured from
    ``history_id_prefix``.

    All payload + response shapes mirror the legacy wire contract
    1:1 per v2.7 §3.1 path-shape lock; new behaviour (e.g. the
    index-based ``truncate_history`` parity guard) is additive.
    """
    services = container.ai_coding
    # NOTE: this helper is mounted under both ``/api/cc`` and
    # ``/api/oc`` prefixes (see :func:`build_router`); the
    # ``url_prefix`` / ``history_id_prefix`` / ``history_source``
    # parameters let the same handler logic surface a CC-flavoured
    # wire shape on one mount and an OC-flavoured one on the other.

    # -- single session GET ------------------------------------------------

    @router.get(
        "/sessions/{session_id}",
        response_model=GetSessionEnvelope,
    )
    async def get_session_detail(
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> GetSessionEnvelope:
        session = await services.get_coding_session_use_case.execute(
            GetCodingSessionQuery(
                session_id=CodingSessionId(value=session_id),
            )
        )
        return GetSessionEnvelope(session=_session_to_response(session))

    # -- send message (POST half of two-step send-then-stream) -----------

    @router.post(
        "/sessions/{session_id}/messages",
        response_model=SendMessageResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def send_session_message(
        body: SendMessageRequest,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> SendMessageResponse:
        result = await services.send_user_message_use_case.execute(
            SendUserMessageCommand(
                session_id=CodingSessionId(value=session_id),
                content=MessageContent(text=body.message),
                client_request_id=body.client_request_id,
                # C-2 (V1 multimodal parity): forward an optional inline
                # image so the provider builds a multimodal content block
                # (``send_user_message.py`` stages it via ``attach_image``).
                # Pass through only when BOTH values are present so a lone
                # field falls back to the text-only path.
                image_b64=(
                    body.image_b64
                    if body.image_b64 and body.image_mime
                    else None
                ),
                image_mime=(
                    body.image_mime
                    if body.image_b64 and body.image_mime
                    else None
                ),
            )
        )
        # Prepend the configured URL prefix to the relative stream URL
        # so the response is directly usable by the WebUI client.  PR-104a
        # used a hardcoded ``/api/cc``; PR-105 generalises so the OC
        # mount surfaces ``/api/oc`` automatically.
        return SendMessageResponse(
            message_id=result.message_id,
            user_msg_id=result.user_msg_id,
            stream_url=f"{url_prefix}{result.stream_url}",
        )

    # -- per-message stream (GET half of two-step send-then-stream) ------

    @router.get("/sessions/{session_id}/messages/{message_id}/stream")
    async def stream_session_message(
        session_id: str = Path(..., min_length=1, max_length=128),
        message_id: str = Path(..., min_length=1, max_length=128),
    ) -> StreamingResponse:
        """Per-message SSE stream returned by ``POST /sessions/{id}/messages``.

        The ``stream_url`` field in the POST response points here.  The
        handler delegates to the same :class:`StreamCodingSessionUseCase`
        as the global session stream — the ``message_id`` is informational
        (used for client-side correlation) and does not filter frames.
        """
        sid = CodingSessionId(value=session_id)
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
            yield b"event: done\ndata: {}\n\n"

        return StreamingResponse(
            _body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- get history -------------------------------------------------------

    @router.get(
        "/sessions/{session_id}/history",
        response_model=HistoryResponse,
    )
    async def get_session_history(
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> HistoryResponse:
        result = await services.get_session_history_use_case.execute(
            GetSessionHistoryQuery(
                session_id=CodingSessionId(value=session_id),
            )
        )
        # Manufacture legacy-shape envelope.  The new aggregate
        # stores only text, so id / timestamp are synthesised from
        # the message position.  Assistant turns are absent until
        # the chat-side projection lands (PR-105).  PR-105
        # generalises the prefix/source so the OC mount produces
        # ``oc-user-<n>`` ids with ``opencode`` source.
        envelopes = [
            HistoryMessageEnvelope(
                id=f"{history_id_prefix}{i}",
                role="user",
                content=msg.text,
                timestamp=0,
                source=history_source,
            )
            for i, msg in enumerate(result.messages)
        ]
        return HistoryResponse(
            session_id=str(result.session_id),
            message_history=envelopes,
        )

    # -- restore -----------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/restore",
        response_model=RestoreResponse,
    )
    async def restore_session(
        session_id: str = Path(..., min_length=1, max_length=128),
        body: RestoreRequest | None = None,
    ) -> RestoreResponse:
        # Body is optional per legacy contract.
        fork = bool(body.fork) if body is not None else False
        result = await services.restore_coding_session_use_case.execute(
            RestoreCodingSessionCommand(
                session_id=CodingSessionId(value=session_id),
                fork=fork,
            )
        )
        return RestoreResponse(
            session=_session_to_response(result.session),
            restored=result.restored,
            forked=result.forked,
        )

    # -- rename ------------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/rename",
        response_model=RenameResponse,
    )
    async def rename_session(
        body: RenameRequest,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> RenameResponse:
        session = await services.rename_session_use_case.execute(
            RenameSessionCommand(
                session_id=CodingSessionId(value=session_id),
                new_title=body.name,
            )
        )
        return RenameResponse(
            ok=True,
            session_id=session_id,
            name=session.title or "",
        )

    # -- working_dir (delegates to PR-106 ChangeWorkspaceUseCase) -------

    @router.post(
        "/sessions/{session_id}/working_dir",
        response_model=WorkingDirResponse,
    )
    async def change_working_dir(
        body: WorkingDirRequest,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> WorkingDirResponse:
        await services.change_workspace_use_case.execute(
            ChangeWorkspaceCommand(
                session_id=CodingSessionId(value=session_id),
                new_workspace=Workspace(path=body.working_dir),
            )
        )
        return WorkingDirResponse(
            ok=True,
            session_id=session_id,
            working_dir=body.working_dir,
        )

    # -- set_active --------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/set_active",
        response_model=SetActiveResponse,
    )
    async def set_active(
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> SetActiveResponse:
        await services.set_active_session_use_case.execute(
            SetActiveSessionCommand(
                session_id=CodingSessionId(value=session_id),
            )
        )
        return SetActiveResponse(
            ok=True,
            session_id=session_id,
            active=True,
        )

    # -- effort ------------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/effort",
        response_model=EffortResponse,
    )
    async def set_effort(
        body: EffortRequest,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> EffortResponse:
        # Validate at the route layer too, so a 400 envelope is
        # produced for invalid values (Pydantic ``str | None`` is
        # too lax).  The use case re-validates via the value object.
        if body.effort is not None and body.effort not in {
            "low", "medium", "high", "max",
        }:
            from qai.platform.errors import ValidationError as _PlatformValidation

            raise _PlatformValidation(
                code="ai_coding.invalid_effort",
                message=(
                    "effort must be one of: low, medium, high, max, or null"
                ),
                field_errors={"effort": [str(body.effort)]},
            )
        session = await services.set_session_effort_use_case.execute(
            SetSessionEffortCommand(
                session_id=CodingSessionId(value=session_id),
                effort=body.effort,
            )
        )
        return EffortResponse(
            ok=True,
            session_id=session_id,
            effort=session.config.effort,
        )

    # -- wechat_notify -----------------------------------------------------

    @router.post(
        "/sessions/{session_id}/wechat_notify",
        response_model=WechatNotifyResponse,
    )
    async def set_wechat_notify(
        body: WechatNotifyRequest,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> WechatNotifyResponse:
        session = await services.set_session_notify_use_case.execute(
            SetSessionNotifyCommand(
                session_id=CodingSessionId(value=session_id),
                channel="wechat",
                user_id=body.wechat_user_id,
            )
        )
        return WechatNotifyResponse(
            ok=True,
            session_id=session_id,
            wechat_notify_user_id=session.wechat_notify_user_id,
        )

    # -- feishu_notify -----------------------------------------------------

    @router.post(
        "/sessions/{session_id}/feishu_notify",
        response_model=FeishuNotifyResponse,
    )
    async def set_feishu_notify(
        body: FeishuNotifyRequest,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> FeishuNotifyResponse:
        # V1 key is ``feishu_user_id``; the current frontend sends
        # ``feishu_open_id``. Accept either (V1 key wins when both set).
        user_id = body.feishu_user_id
        if user_id is None:
            user_id = body.feishu_open_id
        session = await services.set_session_notify_use_case.execute(
            SetSessionNotifyCommand(
                session_id=CodingSessionId(value=session_id),
                channel="feishu",
                user_id=user_id,
            )
        )
        return FeishuNotifyResponse(
            ok=True,
            session_id=session_id,
            feishu_notify_user_id=session.feishu_notify_user_id,
        )

    # -- hard delete -------------------------------------------------------

    @router.delete(
        "/sessions/{session_id}/permanent",
        response_model=HardDeleteResponse,
    )
    async def hard_delete_session(
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> HardDeleteResponse:
        await services.hard_delete_session_use_case.execute(
            HardDeleteSessionCommand(
                session_id=CodingSessionId(value=session_id),
            )
        )
        return HardDeleteResponse(ok=True, deleted=session_id)

    # -- interrupt ---------------------------------------------------------

    @router.post(
        "/sessions/{session_id}/interrupt",
        response_model=InterruptResponse,
    )
    async def interrupt_session(
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> InterruptResponse:
        result = await services.interrupt_session_use_case.execute(
            InterruptSessionCommand(
                session_id=CodingSessionId(value=session_id),
            )
        )
        return InterruptResponse(
            ok=result.interrupted,
            interrupted=result.interrupted,
            reason=result.reason,
        )

    # -- truncate_history --------------------------------------------------

    @router.post(
        "/sessions/{session_id}/truncate_history",
        response_model=TruncateHistoryResponse,
    )
    async def truncate_history(
        body: TruncateHistoryRequest,
        session_id: str = Path(..., min_length=1, max_length=128),
    ) -> TruncateHistoryResponse:
        # Resolve marker_index from either ``after_index`` (new) or
        # ``after_msg_id`` (legacy).  The legacy id format is
        # ``<prefix><index>`` per the GET /history projection above;
        # for backward compat we also accept the historical
        # ``cc-user-`` prefix on the OC mount.
        marker_index: int | None = body.after_index
        if marker_index is None and body.after_msg_id:
            legacy_id = body.after_msg_id
            for accepted_prefix in (history_id_prefix, "cc-user-", "oc-user-"):
                if legacy_id.startswith(accepted_prefix):
                    tail = legacy_id[len(accepted_prefix):]
                    if tail.isdigit():
                        marker_index = int(tail)
                        break
        if marker_index is None:
            from qai.platform.errors import ValidationError as _PlatformValidation

            raise _PlatformValidation(
                code="ai_coding.invalid_truncate_params",
                message=(
                    "either after_index (int) or after_msg_id "
                    "('<provider>-user-<n>') is required"
                ),
                field_errors={
                    "after_index": ["required when after_msg_id is absent"],
                },
            )

        result = await services.truncate_history_use_case.execute(
            TruncateHistoryCommand(
                session_id=CodingSessionId(value=session_id),
                marker_index=marker_index,
                include_self=body.include_self,
            )
        )
        return TruncateHistoryResponse(
            ok=True,
            removed=result.removed,
            remaining=result.remaining,
        )
