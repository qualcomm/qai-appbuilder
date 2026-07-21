# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Channels HTTP routes (PR-036).

Surfaces the 10 S2 use cases of the unified channels bounded context
(`src/qai/channels/application/use_cases/*`) over **two** sibling URL
prefixes: ``/api/feishu/*``, ``/api/wechat/*``. The
prefixes are kept distinct because both the legacy frontend *and*
external messaging-platform webhook configurations depend on them.

**1-port-N-adapter dispatch (refactor-plan §8.7)**

The legacy code maintained parallel ``backend.channels.{feishu,
wechat}`` packages — two strongly-connected components plus a
top-level ``aiohttp.ClientSession._request`` monkey-patch.  This route
file replaces all that with **one** set of use cases driven by an
``ChannelKind`` discriminator carried on the path.  The composition
root in ``apps.api.di`` injects independent
``_FakeChannelTransport`` instances (one per kind); a
``transport_factory(instance) -> ChannelTransportPort`` picks the right
one by ``instance.kind`` so the use cases never see provider-specific
code.

S3 PR-036 scope (≤ 36 active routes mapped from inventory §3.5+§3.6):

* lifecycle:  ``POST /api/{kind}/register``,  ``POST /start``,
  ``POST /stop``,  ``GET /status``
* webhook:    ``POST /api/{kind}/webhook``  (signature verified via
  :class:`WebhookSignatureVerifierPort` fake; the route *never* peeks
  at the body)
* dispatch:   ``POST /api/{kind}/dispatch``,  ``POST /api/{kind}/reply``
* qr login:   ``POST /api/{kind}/qr/issue``,  ``POST /qr/{id}/confirm``,
  ``GET /qr/{id}/status``  (wechat only — feishu rejects)
* session:    ``POST /api/{kind}/session/bind``,
  ``GET /api/{kind}/session/lookup``

Legacy ``config`` / ``proxy`` / ``model`` / ``bindings`` (12 of the 43
inventory rows) are **NOT** within the surface area of the 10 already-
frozen S2 use cases.  See ``PR-036-manifest.md`` §"Cross-PR coordination
request" — the main agent owns the decision whether to add a config-
management use case in S2-bis or defer to S4.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field


class _WechatLoginRequest(BaseModel):
    """Body for ``POST /api/wechat/login``."""

    instance_id: str = Field(..., min_length=1, max_length=128)
    force: bool = False

from qai.channels.application.use_cases.ingest_webhook import (
    IngestWebhookCommand,
    IngestWebhookUseCase,
    SendChannelReplyUseCase,
    SendReplyCommand,
)
from qai.channels.application.use_cases.manage_bindings import (
    BindChannelConversationCommand,
    BindChannelConversationUseCase,
    GetChannelBindingsUseCase,
    UnbindChannelConversationCommand,
    UnbindChannelConversationUseCase,
)
from qai.channels.application.use_cases.manage_lifecycle import (
    AcknowledgeChannelErrorUseCase,
    StartChannelInstanceUseCase,
    StopChannelInstanceUseCase,
)
from qai.channels.application.services.dispatch_aggregator import (
    aggregate_dispatch_frames,
)
from qai.channels.application.use_cases.manage_settings import (
    GetChannelSettingsUseCase,
    UpdateChannelConfigCommand,
    UpdateChannelConfigUseCase,
    UpdateChannelModelCommand,
    UpdateChannelModelUseCase,
    UpdateChannelProxyUseCase,
)
from qai.channels.application.use_cases.qr_image import (
    RenderQrImageUseCase,
)
from qai.channels.application.use_cases.qr_login import (
    ConfirmQrLoginUseCase,
    IssueQrLoginUseCase,
)
from qai.channels.application.use_cases.register_channel_instance import (
    RegisterChannelInstanceCommand,
    RegisterChannelInstanceUseCase,
)
from qai.channels.application.use_cases.session_index import (
    BindSessionIndexCommand,
    BindSessionIndexUseCase,
    LookupSessionIndexUseCase,
)
from qai.channels.application.use_cases.wechat_personal import (
    LogoutWechatPersonalUseCase,
)
from qai.channels.domain import (
    ChannelHealth,
    ChannelInstanceId,
    ChannelKind,
    ChannelMessageId,
    ChannelUserId,
    QrLoginConfirmedEvent,
    QrLoginIssuedEvent,
    QrLoginScannedEvent,
)
from qai.platform.errors import NotFoundError, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


_ChannelLiteral = Literal["feishu", "wechat"]


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class RegisterChannelRequest(BaseModel):
    """Body for ``POST /api/{kind}/register``."""

    name: str = Field(..., min_length=1, max_length=256)
    secret_service: str = Field(..., min_length=1, max_length=256)
    secret_key: str = Field(..., min_length=1, max_length=256)
    # ``min_length=0`` (relaxed from 1): Feishu registers a single
    # instance up-front and provisions its ``app_secret`` later via
    # ``POST /api/feishu/config`` (the authoritative single-namespace
    # writer), so an empty secret at register time is valid — the
    # instance is configured incrementally (V1 parity).  WeChat callers
    # still send a non-empty value; this only loosens the lower bound.
    secret_value: str = Field(..., min_length=0, max_length=4096)
    metadata: dict[str, str] = Field(default_factory=dict)


class ChannelInstanceResponse(BaseModel):
    """Wire shape of a :class:`ChannelInstance`."""

    instance_id: str
    kind: str
    name: str
    status: str
    last_error: str
    created_at: str
    updated_at: str
    metadata: dict[str, str]


class InstanceIdRequest(BaseModel):
    """Common body for start/stop/status-by-id/dispatch."""

    instance_id: str = Field(..., min_length=1, max_length=128)


class StatusResponse(BaseModel):
    """``GET /api/{kind}/status?instance_id=...`` payload."""

    instance: ChannelInstanceResponse
    health: dict[str, object]


class DispatchRequest(BaseModel):
    """Body for ``POST /api/{kind}/dispatch``."""

    message_id: str = Field(..., min_length=1, max_length=128)


class DispatchResponse(BaseModel):
    """Result of a bridge dispatch."""

    reply_text: str
    coding_session_id: str | None


class ReplyRequest(BaseModel):
    """Body for ``POST /api/{kind}/reply``."""

    inbound_message_id: str = Field(..., min_length=1, max_length=128)
    reply_text: str = Field(..., min_length=1, max_length=16_384)


class WebhookResponse(BaseModel):
    """Result envelope of ``POST /api/{kind}/webhook``."""

    message_id: str
    deduplicated: bool
    parsed_command_verb: str | None


class IssueQrRequest(BaseModel):
    """Body for ``POST /api/{kind}/qr/issue``."""

    instance_id: str = Field(..., min_length=1, max_length=128)


class QrChallengeResponse(BaseModel):
    """QR challenge state."""

    challenge_id: str
    instance_id: str
    status: str
    issued_at: str
    expires_at: str


class BindSessionRequest(BaseModel):
    """Body for ``POST /api/{kind}/session/bind``."""

    instance_id: str = Field(..., min_length=1, max_length=128)
    channel_user_id: str = Field(..., min_length=1, max_length=128)
    internal_user_id: str | None = Field(default=None, max_length=128)
    coding_session_id: str | None = Field(default=None, max_length=128)


class SessionEntryResponse(BaseModel):
    """Wire shape of a :class:`SessionIndexEntry`."""

    instance_id: str
    channel_user_id: str
    internal_user_id: str | None
    coding_session_id: str | None
    updated_at: str


# ---------------------------------------------------------------------------
# DTOs — settings + bindings (PR-202)
# ---------------------------------------------------------------------------


#: SecretStore service used for the per-instance proxy password
#: (legacy ``forge_config.proxy_password`` / wechat-channel proxy
#: settings).  PR-202 keeps the value distinct from
#: ``qai.channels.signing`` so signature secrets and outbound HTTP
#: proxy passwords cannot collide on the same key.
PROXY_PASSWORD_SECRET_SERVICE = "qai.channels.proxy"


#: SecretStore service used for the per-instance Feishu ``app_secret``.
#: Kept value-identical to ``apps.api._channels_di.FEISHU_APP_SECRET_SERVICE``
#: (the outbound token cache + inbound WebSocket transport both read the
#: bare ``app_secret`` from ``(FEISHU_APP_SECRET_SERVICE, instance_id)``),
#: defined here too so the route layer's credential-I/O write does not
#: have to import from the ``apps.api`` composition root — mirrors how
#: ``PROXY_PASSWORD_SECRET_SERVICE`` is duplicated across both modules.
FEISHU_APP_SECRET_SERVICE = "qai.channels.feishu.app_secret"


class SaveChannelConfigRequest(BaseModel):
    """Body for ``POST /api/{kind}/config``.

    ``kind_specific`` is *merged* into the existing settings (keys
    absent from the body are preserved).  Empty dict is accepted and
    leaves ``kind_specific`` untouched.

    ``app_secret`` (Feishu only) is the plaintext Feishu app secret.
    Empty string means "do not change" — the existing SecretStore
    record / ``has_app_secret`` flag is preserved (matching the proxy
    password "field blanked = preserve" semantics so the front-end can
    render a ``(saved)`` placeholder without forcing a re-entry).  The
    secret is written to :class:`SecretStore` by the route layer and
    NEVER reaches the application / domain layers (AGENTS.md §3.3).
    """

    instance_id: str = Field(..., min_length=1, max_length=128)
    auto_start: bool = False
    kind_specific: dict[str, str] = Field(default_factory=dict)
    app_secret: str = Field(default="", max_length=1024)


class SaveChannelProxyRequest(BaseModel):
    """Body for ``POST /api/{kind}/proxy``.

    Empty ``password`` leaves the existing SecretStore record /
    ``has_password`` flag untouched (matching the legacy "field
    blanked = preserve" semantics so the front-end can render a
    ``(saved)`` placeholder without forcing a re-entry).
    """

    instance_id: str = Field(..., min_length=1, max_length=128)
    url: str = Field(default="", max_length=1024)
    username: str = Field(default="", max_length=1024)
    password: str = Field(default="", max_length=1024)


class SaveChannelModelRequest(BaseModel):
    """Body for ``POST /api/{kind}/model``."""

    instance_id: str = Field(..., min_length=1, max_length=128)
    model_id: str = Field(default="", max_length=1024)
    model_provider: str = Field(default="", max_length=1024)


class SaveChannelBindingRequest(BaseModel):
    """Body for ``POST /api/{kind}/bindings``.

    Empty ``channel_user_id`` clears the binding (matches legacy
    ``backend.channels.{wechat,feishu}.api_routes`` behaviour).
    """

    instance_id: str = Field(..., min_length=1, max_length=128)
    conversation_id: str = Field(..., min_length=1, max_length=256)
    channel_user_id: str = Field(default="", max_length=256)


# ---------------------------------------------------------------------------
# Helpers — domain ⇄ DTO mappers (private)
# ---------------------------------------------------------------------------


def _instance_to_dto(inst: object) -> ChannelInstanceResponse:
    """Convert a :class:`ChannelInstance` aggregate to its wire shape."""
    # local import-style annotation kept off-topic; the parameter is
    # typed loosely to avoid circular dependency complications when the
    # tests import the DTO without the aggregate.
    from qai.channels.domain import ChannelInstance

    assert isinstance(inst, ChannelInstance)
    return ChannelInstanceResponse(
        instance_id=inst.instance_id.value,
        kind=inst.kind.value,
        name=inst.name,
        status=inst.status.value,
        last_error=inst.last_error,
        created_at=inst.created_at.isoformat(),
        updated_at=inst.updated_at.isoformat(),
        metadata={k: v for k, v in inst.metadata},
    )


def _make_kind(raw: str) -> ChannelKind:
    """Convert a path-prefix slug (``feishu`` etc.) to a
    :class:`ChannelKind`, raising :class:`ValidationError` on failure.

    All three valid slugs are baked into the route prefixes themselves,
    so this is essentially defence-in-depth — but it also keeps
    :class:`ChannelKindNotSupportedError` semantics centralised.
    """
    try:
        return ChannelKind.from_str(raw)
    except ValueError as exc:
        raise ValidationError(
            "channels.invalid_kind",
            f"unknown channel kind {raw!r}",
            field_errors={"kind": [str(exc)]},
        ) from exc


def _make_instance_id(raw: str) -> ChannelInstanceId:
    """Wrap a raw string in :class:`ChannelInstanceId` or 400."""
    try:
        return ChannelInstanceId(value=raw)
    except ValueError as exc:
        raise ValidationError(
            "channels.invalid_instance_id",
            f"instance_id is invalid: {exc}",
            field_errors={"instance_id": [str(exc)]},
        ) from exc


def _make_message_id(raw: str) -> ChannelMessageId:
    try:
        return ChannelMessageId(value=raw)
    except ValueError as exc:
        raise ValidationError(
            "channels.invalid_message_id",
            f"message_id is invalid: {exc}",
            field_errors={"message_id": [str(exc)]},
        ) from exc


def _make_channel_user_id(raw: str) -> ChannelUserId:
    try:
        return ChannelUserId(value=raw)
    except ValueError as exc:
        raise ValidationError(
            "channels.invalid_channel_user_id",
            f"channel_user_id is invalid: {exc}",
            field_errors={"channel_user_id": [str(exc)]},
        ) from exc


def _qr_challenge_to_dto(ch: object) -> QrChallengeResponse:
    from qai.channels.domain import QrLoginChallenge

    assert isinstance(ch, QrLoginChallenge)
    return QrChallengeResponse(
        challenge_id=ch.challenge_id,
        instance_id=ch.instance_id_value,
        status=ch.status.value,
        issued_at=ch.issued_at.isoformat(),
        expires_at=ch.expires_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Dispatch services bundle (PR-097 R-3)
# ---------------------------------------------------------------------------
def _build_dispatch_services(container: "Container"):  # type: ignore[no-untyped-def]
    """Construct a :class:`DispatchServices` bundle from ``container``.

    Constructed per-request because the bundle is a thin dataclass
    (no I/O in __init__); building it lazily keeps the route layer
    free of module-level state and makes it trivial for tests that
    rebuild a fresh container per test client.

    The reboot scheduler + grant bridge come from the security and
    apps-level wiring respectively; both are optional and degrade
    gracefully when not registered.
    """
    from apps.api._channel_dispatch_bridge import DispatchServices
    from apps.api._channel_grant_bridge import ChannelGrantBridge

    channels = container.channels
    security = getattr(container, "security", None)
    grant_bridge = (
        ChannelGrantBridge(
            security_services=security,
            channel_session_index_repo=channels.session_repository,
        )
        if security is not None
        else None
    )
    reboot_scheduler = getattr(container, "reboot_scheduler", None)
    return DispatchServices(
        chat_bridge=channels.chat_message_bridge,
        ai_coding_bridge=channels.ai_coding_channel_bridge,
        grant_bridge=grant_bridge,
        command_parser=channels.command_parser,
        channel_session_repo=channels.session_repository,
        delivery_service=channels.realtime_delivery_service,
        tool_formatter=channels.tool_formatter,
        container=container,
        reboot_scheduler=reboot_scheduler,
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the channels router (3 sibling prefixes) bound to ``container``.

    The router holds no module-level state; it is reconstructed every
    time :func:`apps.api.main.create_app` is called.  All transport
    state lives on the per-kind ``_FakeChannelTransport`` instances
    held in ``container.channels`` — fresh per ``Container.build``.
    """
    router = APIRouter(tags=["channels"])

    for slug in ("feishu", "wechat"):
        _register_kind(router, slug=slug, container=container)
    return router


def _register_kind(
    router: APIRouter,
    *,
    slug: str,
    container: "Container",
) -> None:
    """Bind the 12 endpoints for a single :class:`ChannelKind` slug.

    The same in-memory use-case set is reused across all three slugs;
    the only thing that varies between iterations is the URL prefix
    and the ``ChannelKind`` discriminator used in
    :class:`RegisterChannelInstanceCommand`.
    """
    kind = _make_kind(slug)
    prefix = f"/api/{slug}"

    # ── lifecycle ──────────────────────────────────────────────────────

    @router.post(
        f"{prefix}/register",
        response_model=ChannelInstanceResponse,
        status_code=status.HTTP_201_CREATED,
        name=f"channels.{slug}.register",
    )
    async def register(  # type: ignore[misc]
        body: RegisterChannelRequest,
    ) -> ChannelInstanceResponse:
        use_case: RegisterChannelInstanceUseCase = (
            container.channels.register_channel_instance_use_case
        )
        # Feishu: force the credential into the single
        # ``(FEISHU_APP_SECRET_SERVICE, instance_id)`` namespace (bare
        # app_secret) so register, the ``POST /api/feishu/config``
        # writer, the outbound token cache, and the inbound WebSocket
        # transport all read/write the SAME record.  The app_secret is
        # normally provisioned later via ``/config`` (blank at register
        # time is fine); this just guarantees the ref lands in the right
        # place regardless of what the front-end passes.
        if kind is ChannelKind.FEISHU:
            cmd = RegisterChannelInstanceCommand(
                kind=kind,
                name=body.name,
                secret_service=FEISHU_APP_SECRET_SERVICE,
                secret_key="",  # ignored: instance_id used instead
                secret_value=body.secret_value,
                metadata=tuple(body.metadata.items()),
                secret_key_use_instance_id=True,
            )
        else:
            cmd = RegisterChannelInstanceCommand(
                kind=kind,
                name=body.name,
                secret_service=body.secret_service,
                secret_key=body.secret_key,
                secret_value=body.secret_value,
                metadata=tuple(body.metadata.items()),
            )
        instance = await use_case.execute(cmd)
        return _instance_to_dto(instance)

    @router.post(
        f"{prefix}/start",
        response_model=ChannelInstanceResponse,
        name=f"channels.{slug}.start",
    )
    async def start(  # type: ignore[misc]
        body: InstanceIdRequest,
    ) -> ChannelInstanceResponse:
        use_case: StartChannelInstanceUseCase = (
            container.channels.start_channel_instance_use_case
        )
        instance = await use_case.execute(
            _make_instance_id(body.instance_id)
        )
        return _instance_to_dto(instance)

    @router.post(
        f"{prefix}/stop",
        response_model=ChannelInstanceResponse,
        name=f"channels.{slug}.stop",
    )
    async def stop(  # type: ignore[misc]
        body: InstanceIdRequest,
    ) -> ChannelInstanceResponse:
        use_case: StopChannelInstanceUseCase = (
            container.channels.stop_channel_instance_use_case
        )
        instance = await use_case.execute(
            _make_instance_id(body.instance_id)
        )
        return _instance_to_dto(instance)

    @router.post(
        f"{prefix}/acknowledge",
        response_model=ChannelInstanceResponse,
        name=f"channels.{slug}.acknowledge",
    )
    async def acknowledge(  # type: ignore[misc]
        body: InstanceIdRequest,
    ) -> ChannelInstanceResponse:
        """Acknowledge an ``error`` state, moving the instance back to
        ``stopped`` so it can be restarted.

        Returns 409 if the instance is not currently in ``error`` state.
        """
        use_case: AcknowledgeChannelErrorUseCase = (
            container.channels.acknowledge_channel_error_use_case
        )
        instance = await use_case.execute(
            _make_instance_id(body.instance_id)
        )
        return _instance_to_dto(instance)

    @router.get(
        f"{prefix}/status",
        response_model=StatusResponse,
        name=f"channels.{slug}.status",
    )
    async def get_status(instance_id: str) -> StatusResponse:  # type: ignore[misc]
        repo = container.channels.instance_repository
        iid = _make_instance_id(instance_id)
        instance = await repo.find(iid)
        if instance is None or instance.kind is not kind:
            raise NotFoundError(
                "channels.instance_not_found",
                "channel_instance",
                instance_id,
            )
        # Use the per-kind transport directly for a kind-bound health
        # peek; no monkey-patch, no global state.
        transport = container.channels.transport_for_kind(kind)
        report = await transport.health(instance)
        # M-1 (State-Truth, 铁律 1): the outbound transport's ``health()``
        # only reflects whether the outbound HTTPS sender is "started" — it
        # does NOT know if the long-lived INBOUND connection (WeChat
        # long-poll / Feishu WS) actually died. Cross-check the inbound
        # transport's real liveness probe so a dead inbound link surfaces as
        # DEGRADED instead of a misleading HEALTHY. We only DOWNGRADE (never
        # upgrade) — the outbound report stays authoritative for "down".
        inbound_status = report.status
        inbound_detail = report.detail
        inbound_map = getattr(
            container.channels, "inbound_transport_for_kind", None
        )
        inbound = inbound_map.get(kind) if inbound_map else None
        if inbound is not None and report.status is ChannelHealth.HEALTHY:
            try:
                alive = bool(inbound.is_alive())
            except Exception:  # noqa: BLE001 - probe is best-effort
                alive = True  # don't fabricate a failure on probe error
            if not alive:
                inbound_status = ChannelHealth.DEGRADED
                inbound_detail = (
                    report.detail
                    or "inbound connection is not alive (reconnecting)"
                )
        return StatusResponse(
            instance=_instance_to_dto(instance),
            health={
                "status": inbound_status.value,
                "detail": inbound_detail,
                "checked_at": (
                    report.checked_at.isoformat()
                    if report.checked_at is not None
                    else None
                ),
            },
        )

    # ── webhook ingest ─────────────────────────────────────────────────

    @router.post(
        f"{prefix}/webhook",
        response_model=WebhookResponse,
        name=f"channels.{slug}.webhook",
    )
    async def webhook(  # type: ignore[misc]
        request: Request,
        instance_id: str,
    ) -> WebhookResponse:
        # NOTE: signature verification happens *inside*
        # IngestWebhookUseCase via WebhookSignatureVerifierPort.
        # The route layer must NEVER peek at the raw body other than
        # to forward it.
        raw_body = await request.body()
        headers = {k: v for k, v in request.headers.items()}
        use_case: IngestWebhookUseCase = (
            container.channels.ingest_webhook_use_case
        )
        result = await use_case.execute(
            IngestWebhookCommand(
                instance_id=_make_instance_id(instance_id),
                kind=kind,
                raw_body=raw_body,
                headers=headers,
            )
        )
        verb: str | None = None
        if result.parsed_command is not None:
            verb = result.parsed_command.verb
        return WebhookResponse(
            message_id=result.message.message_id.value,
            deduplicated=result.deduplicated,
            parsed_command_verb=verb,
        )

    @router.post(
        f"{prefix}/dispatch",
        response_model=DispatchResponse,
        name=f"channels.{slug}.dispatch",
    )
    async def dispatch(  # type: ignore[misc]
        body: DispatchRequest,
    ) -> DispatchResponse:
        # PR-097 R-3: route the inbound dispatch through the streaming
        # apps-layer pipeline so /grant, /cc, /oc, /list, ... and the
        # realtime delivery service all run.  R16 (Clean-Arch
        # correction): the business rule for folding the streamed frames
        # into a single ``reply_text`` (skip partial / stop on ERROR /
        # concatenate) now lives in the application-layer
        # :func:`aggregate_dispatch_frames`; the route only wires the
        # frame producer to the consumer and maps the result to the
        # response DTO (path / method / payload contract preserved per
        # v2.7 §3 immutability).
        from apps.api._channel_dispatch_bridge import (
            OutboundFrameKind,
            dispatch_inbound_message,
        )

        message_repo = container.channels.message_repository
        message = await message_repo.get(_make_message_id(body.message_id))

        services = _build_dispatch_services(container)

        aggregated = await aggregate_dispatch_frames(
            dispatch_inbound_message(message, services=services),
            error_kind=OutboundFrameKind.ERROR,
        )
        # Persist the parsed → dispatched state transition so a
        # subsequent ``POST /reply`` (which requires the message to be
        # in ``dispatched`` state) can record the outbound reply.  The
        # streamed dispatch pipeline above delivers the reply in-band,
        # but the inbound message's own lifecycle still needs the
        # transition recorded.  Guard the transition so re-dispatching an
        # already-dispatched/replied message is a no-op rather than a
        # 412 (idempotent dispatch).
        from qai.channels.domain import ChannelMessageStatus

        if message.status is ChannelMessageStatus.PARSED:
            dispatched = message.mark_dispatched(
                now=container.clock.now()
            )
            await message_repo.save(dispatched)
        return DispatchResponse(
            reply_text=aggregated.reply_text,
            coding_session_id=aggregated.coding_session_id,
        )

    @router.post(
        f"{prefix}/reply",
        response_model=ChannelInstanceResponse,
        name=f"channels.{slug}.reply",
    )
    async def reply(body: ReplyRequest) -> ChannelInstanceResponse:  # type: ignore[misc]
        use_case: SendChannelReplyUseCase = (
            container.channels.send_channel_reply_use_case
        )
        message = await use_case.execute(
            SendReplyCommand(
                inbound_message_id=_make_message_id(
                    body.inbound_message_id
                ),
                reply_text=body.reply_text,
            )
        )
        # The inbound message carries the originating instance_id;
        # surface the parent instance as the response so callers can
        # confirm the kind matched expectations.
        repo = container.channels.instance_repository
        instance = await repo.get(message.instance_id)
        return _instance_to_dto(instance)

    # ── qr login (wechat only) ─────────────────────────────────

    @router.post(
        f"{prefix}/qr/issue",
        response_model=QrChallengeResponse,
        name=f"channels.{slug}.qr_issue",
    )
    async def qr_issue(  # type: ignore[misc]
        body: IssueQrRequest,
    ) -> QrChallengeResponse:
        if kind is ChannelKind.FEISHU:
            raise ValidationError(
                "channels.qr_login_not_supported",
                "feishu does not support QR login",
                field_errors={"kind": [slug]},
            )
        use_case: IssueQrLoginUseCase = (
            container.channels.issue_qr_login_use_case
        )
        challenge = await use_case.execute(
            _make_instance_id(body.instance_id)
        )
        return _qr_challenge_to_dto(challenge)

    @router.post(
        f"{prefix}/qr/{{challenge_id}}/confirm",
        response_model=QrChallengeResponse,
        name=f"channels.{slug}.qr_confirm",
    )
    async def qr_confirm(  # type: ignore[misc]
        challenge_id: str,
        body: InstanceIdRequest,
    ) -> QrChallengeResponse:
        if kind is ChannelKind.FEISHU:
            raise ValidationError(
                "channels.qr_login_not_supported",
                "feishu does not support QR login",
                field_errors={"kind": [slug]},
            )
        use_case: ConfirmQrLoginUseCase = (
            container.channels.confirm_qr_login_use_case
        )
        challenge = await use_case.execute(
            _make_instance_id(body.instance_id),
            challenge_id,
            confirm=True,
        )
        return _qr_challenge_to_dto(challenge)

    @router.get(
        f"{prefix}/qr/{{challenge_id}}/status",
        response_model=QrChallengeResponse,
        name=f"channels.{slug}.qr_status",
    )
    async def qr_status(  # type: ignore[misc]
        challenge_id: str,
        instance_id: str,
    ) -> QrChallengeResponse:
        if kind is ChannelKind.FEISHU:
            raise ValidationError(
                "channels.qr_login_not_supported",
                "feishu does not support QR login",
                field_errors={"kind": [slug]},
            )
        use_case = container.channels.confirm_qr_login_use_case
        challenge = await use_case.execute(
            _make_instance_id(instance_id),
            challenge_id,
            confirm=False,
        )
        return _qr_challenge_to_dto(challenge)

    # ── PR-204: QR image rendering (wechat only) ───────────────

    @router.get(
        f"{prefix}/qr/{{challenge_id}}/image",
        name=f"channels.{slug}.qr_image",
        responses={200: {"content": {"image/png": {}}}},
    )
    async def qr_image(  # type: ignore[misc]
        challenge_id: str,
        instance_id: str,
    ) -> Response:
        if kind is ChannelKind.FEISHU:
            raise ValidationError(
                "channels.qr_login_not_supported",
                "feishu does not support QR login",
                field_errors={"kind": [slug]},
            )
        use_case: RenderQrImageUseCase = (
            container.channels.render_qr_image_use_case
        )
        png_bytes = await use_case.execute(
            _make_instance_id(instance_id),
            challenge_id,
            expected_kind=kind,
        )
        return Response(
            content=png_bytes,
            media_type="image/png",
        )

    # ── PR-204: QR login event WebSocket (wechat only) ─────────

    @router.websocket(f"{prefix}/qr/events")
    async def qr_events(  # type: ignore[misc]
        websocket: WebSocket,
        instance_id: str,
    ) -> None:
        # Feishu does not support QR login; reject the upgrade with a
        # 4400-class close so test clients see an immediate failure
        # (mirrors the HTTP ``channels.qr_login_not_supported`` 400).
        if kind is ChannelKind.FEISHU:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="channels.qr_login_not_supported",
            )
            return

        # Validate the instance_id query param up front; reject with
        # the same close code so the wire shape is symmetric.
        try:
            iid = _make_instance_id(instance_id)
        except ValidationError:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="channels.invalid_instance_id",
            )
            return

        # Validate the instance exists for this kind before accepting
        # the upgrade — saves the client from listening on a phantom
        # instance.
        repo = container.channels.instance_repository
        existing = await repo.find(iid)
        if existing is None or existing.kind is not kind:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="channels.instance_not_found",
            )
            return

        await websocket.accept()
        await _run_qr_event_loop(
            websocket=websocket,
            container=container,
            instance_id=iid.value,
        )

    # ── wechat login / logout / qr-image (legacy surface) ─────────────
    #
    # These three endpoints restore the v1 ``/api/wechat/login``,
    # ``/api/wechat/logout``, ``/api/wechat/qr-image`` surface that the
    # legacy ``backend/channels/wechat/api_routes.py`` exposed.  They
    # delegate to the :class:`WechatPersonalQrLoginAdapter` wired into
    # ``container.channels.wechat_personal_qr_login``.  Only registered
    # for the ``wechat`` slug — feishu does not support this
    # personal-account flow.

    if slug == "wechat":

        @router.post(
            f"{prefix}/login",
            name=f"channels.{slug}.login",
        )
        async def wechat_login(body: _WechatLoginRequest) -> dict:  # type: ignore[misc]
            """Trigger personal-WeChat QR login via the wechatbot SDK.

            Equivalent to legacy ``POST /api/wechat/login``.  Requires a
            registered wechat instance_id so the adapter knows which
            credentials / challenge to associate the login with.
            """
            iid = _make_instance_id(body.instance_id)
            repo = container.channels.instance_repository
            instance = await repo.get(iid)
            if instance.kind is not kind:
                raise NotFoundError(
                    "channels.instance_not_found",
                    "channel_instance",
                    body.instance_id,
                )
            adapter = container.channels.wechat_personal_qr_login
            challenge_id = await adapter.trigger_login(
                instance, force=body.force
            )
            # Hand the SDK-driven challenge_id back so the WebUI can
            # poll ``GET /api/wechat/qr-image?challenge_id=...`` for the
            # scannable QR and ``GET /api/wechat/qr/{id}/status`` for
            # login progress.  (Previously this returned only ``ok`` and
            # the front-end had no id for the SDK challenge — the QR
            # never showed.)
            return {"ok": True, "challenge_id": challenge_id}

        @router.post(
            f"{prefix}/logout",
            name=f"channels.{slug}.logout",
        )
        async def wechat_logout(body: InstanceIdRequest) -> dict:  # type: ignore[misc]
            """Logout from personal-WeChat and expire the active QR challenge.

            Equivalent to legacy ``POST /api/wechat/logout``.  Tears down
            the active wechatbot SDK Bot instance held by the adapter.
            Also stops the channel instance via the lifecycle use case so
            the long-poll transport is halted.
            """
            iid = _make_instance_id(body.instance_id)
            repo = container.channels.instance_repository
            instance = await repo.get(iid)
            if instance.kind is not kind:
                raise NotFoundError(
                    "channels.instance_not_found",
                    "channel_instance",
                    body.instance_id,
                )
            # R16 (Clean-Arch correction): the two-step "logout SDK bot
            # → stop channel instance" orchestration now lives in
            # :class:`LogoutWechatPersonalUseCase`; the route only
            # resolves + kind-checks the instance (an HTTP-envelope
            # concern) and delegates.  Behaviour / response shape
            # unchanged (v2.7 §3 immutability).
            logout_uc: LogoutWechatPersonalUseCase = (
                container.channels.logout_wechat_personal_use_case
            )
            await logout_uc.execute(iid)
            return {"ok": True}

        @router.get(
            f"{prefix}/qr-image",
            name=f"channels.{slug}.qr_image_latest",
            responses={200: {"content": {"image/png": {}}}},
        )
        async def wechat_qr_image_latest(  # type: ignore[misc]
            instance_id: str,
            challenge_id: str,
        ) -> Response:
            """Return the QR login image for a given challenge.

            Equivalent to legacy ``GET /api/wechat/qr-image``.  The
            client must supply the ``challenge_id`` returned by the
            ``POST /api/wechat/qr/issue`` or trigger_login flow.
            Renders the QR as a PNG via :class:`RenderQrImageUseCase`.
            """
            iid = _make_instance_id(instance_id)
            use_case: RenderQrImageUseCase = (
                container.channels.render_qr_image_use_case
            )
            png_bytes = await use_case.execute(
                iid, challenge_id, expected_kind=kind
            )
            return Response(
                content=png_bytes,
                media_type="image/png",
            )

    # ── session index bridge ───────────────────────────────────────────

    @router.post(
        f"{prefix}/session/bind",
        response_model=SessionEntryResponse,
        status_code=status.HTTP_201_CREATED,
        name=f"channels.{slug}.session_bind",
    )
    async def session_bind(  # type: ignore[misc]
        body: BindSessionRequest,
    ) -> SessionEntryResponse:
        use_case: BindSessionIndexUseCase = (
            container.channels.bind_session_index_use_case
        )
        entry = await use_case.execute(
            BindSessionIndexCommand(
                instance_id=_make_instance_id(body.instance_id),
                channel_user_id=_make_channel_user_id(body.channel_user_id),
                internal_user_id=body.internal_user_id,
                coding_session_id=body.coding_session_id,
            )
        )
        return SessionEntryResponse(
            instance_id=entry.instance_id.value,
            channel_user_id=entry.channel_user_id.value,
            internal_user_id=entry.internal_user_id,
            coding_session_id=entry.coding_session_id,
            updated_at=entry.updated_at.isoformat(),
        )

    @router.get(
        f"{prefix}/session/lookup",
        response_model=SessionEntryResponse,
        name=f"channels.{slug}.session_lookup",
    )
    async def session_lookup(  # type: ignore[misc]
        instance_id: str,
        channel_user_id: str,
    ) -> SessionEntryResponse:
        use_case: LookupSessionIndexUseCase = (
            container.channels.lookup_session_index_use_case
        )
        entry = await use_case.execute(
            _make_instance_id(instance_id),
            _make_channel_user_id(channel_user_id),
        )
        if entry is None:
            raise NotFoundError(
                "channels.session_index_entry_not_found",
                "session_index_entry",
                f"{instance_id}:{channel_user_id}",
            )
        return SessionEntryResponse(
            instance_id=entry.instance_id.value,
            channel_user_id=entry.channel_user_id.value,
            internal_user_id=entry.internal_user_id,
            coding_session_id=entry.coding_session_id,
            updated_at=entry.updated_at.isoformat(),
        )

    # ── PR-202: settings (config / proxy / model) ──────────────────────

    @router.get(
        f"{prefix}/config",
        name=f"channels.{slug}.get_config",
    )
    async def get_config(instance_id: str) -> dict:  # type: ignore[misc]
        # Defense-in-depth (data-bleed guard): kind-scope the lookup so a
        # wechat route never returns a feishu instance's kind_specific
        # (e.g. its app_id), and vice versa.
        _repo = container.channels.instance_repository
        _inst = await _repo.find(_make_instance_id(instance_id))
        if _inst is None or _inst.kind is not kind:
            raise NotFoundError(
                "channels.instance_not_found",
                "channel_instance",
                instance_id,
            )
        use_case: GetChannelSettingsUseCase = (
            container.channels.get_channel_settings_use_case
        )
        settings_vo = await use_case.execute(
            _make_instance_id(instance_id)
        )
        # Feishu exposes a presence-of-app_secret indicator so the
        # front-end can render a "(saved)" placeholder without echoing
        # the plaintext secret (symmetric with proxy ``has_password``).
        # WeChat has no app_secret; the flag is harmlessly ``False``.
        return {
            "auto_start": settings_vo.auto_start,
            "kind_specific": {
                k: v for k, v in settings_vo.kind_specific
            },
            "has_app_secret": settings_vo.has_app_secret,
        }

    @router.post(
        f"{prefix}/config",
        name=f"channels.{slug}.save_config",
    )
    async def save_config(  # type: ignore[misc]
        body: SaveChannelConfigRequest,
    ) -> dict:
        use_case: UpdateChannelConfigUseCase = (
            container.channels.update_channel_config_use_case
        )
        command = UpdateChannelConfigCommand(
            instance_id=_make_instance_id(body.instance_id),
            auto_start=body.auto_start,
            kind_specific=dict(body.kind_specific),
        )
        # Feishu only: the plaintext app_secret is a credential-I/O
        # concern that must never enter the application layer (§3.3).
        # The route owns the SecretStore write (bare app_secret under a
        # fixed namespace keyed by instance_id — the SAME record the
        # outbound token cache + inbound WebSocket transport read) and
        # forwards only an ``app_secret_present`` boolean.  Empty field
        # = preserve (handled in the use case).  Mirrors ``save_proxy``.
        if kind is ChannelKind.FEISHU:
            secret_present = bool(body.app_secret)
            if secret_present:
                container.secret_store.set(
                    FEISHU_APP_SECRET_SERVICE,
                    command.instance_id.value,
                    body.app_secret,
                )
            instance = await use_case.execute_preserving_secret(
                command=command,
                app_secret_present=secret_present,
            )
        else:
            instance = await use_case.execute(command)
        return {"ok": True, "instance_id": instance.instance_id.value}

    @router.get(
        f"{prefix}/proxy",
        name=f"channels.{slug}.get_proxy",
    )
    async def get_proxy(instance_id: str) -> dict:  # type: ignore[misc]
        # Defense-in-depth (data-bleed guard): verify the instance is of
        # THIS kind before returning its settings, mirroring get_status.
        # Without this a wechat route could read a feishu instance's
        # settings if the caller passed a mismatched id (the repository
        # get() is id-only, not kind-scoped).
        _repo = container.channels.instance_repository
        _iid = _make_instance_id(instance_id)
        _inst = await _repo.find(_iid)
        if _inst is None or _inst.kind is not kind:
            raise NotFoundError(
                "channels.instance_not_found",
                "channel_instance",
                instance_id,
            )
        use_case: GetChannelSettingsUseCase = (
            container.channels.get_channel_settings_use_case
        )
        settings_vo = await use_case.execute(
            _make_instance_id(instance_id)
        )
        return {
            "url": settings_vo.proxy.url,
            "username": settings_vo.proxy.username,
            "has_password": settings_vo.proxy.has_password,
        }

    @router.post(
        f"{prefix}/proxy",
        name=f"channels.{slug}.save_proxy",
    )
    async def save_proxy(  # type: ignore[misc]
        body: SaveChannelProxyRequest,
    ) -> dict:
        # R16 (Clean-Arch correction): the "empty password = preserve
        # existing has_password" three-state decision now lives in
        # :meth:`UpdateChannelProxyUseCase.execute_preserving_password`.
        # The route only owns the SecretStore write (a credential-I/O
        # concern that must never enter the application layer per §3.3)
        # and forwards a ``password_present`` boolean.  Behaviour /
        # response shape unchanged (v2.7 §3 immutability).
        instance_id = _make_instance_id(body.instance_id)
        password_present = bool(body.password)
        if password_present:
            container.secret_store.set(
                PROXY_PASSWORD_SECRET_SERVICE,
                instance_id.value,
                body.password,
            )
        use_case: UpdateChannelProxyUseCase = (
            container.channels.update_channel_proxy_use_case
        )
        await use_case.execute_preserving_password(
            instance_id=instance_id,
            url=body.url,
            username=body.username,
            password_present=password_present,
        )
        return {"ok": True}

    @router.get(
        f"{prefix}/model",
        name=f"channels.{slug}.get_model",
    )
    async def get_model(instance_id: str) -> dict:  # type: ignore[misc]
        # Defense-in-depth (data-bleed guard): kind-scope the lookup.
        _repo = container.channels.instance_repository
        _inst = await _repo.find(_make_instance_id(instance_id))
        if _inst is None or _inst.kind is not kind:
            raise NotFoundError(
                "channels.instance_not_found",
                "channel_instance",
                instance_id,
            )
        use_case: GetChannelSettingsUseCase = (
            container.channels.get_channel_settings_use_case
        )
        settings_vo = await use_case.execute(
            _make_instance_id(instance_id)
        )
        return {
            "model_id": settings_vo.model.model_id,
            "model_provider": settings_vo.model.model_provider,
        }

    @router.post(
        f"{prefix}/model",
        name=f"channels.{slug}.save_model",
    )
    async def save_model(  # type: ignore[misc]
        body: SaveChannelModelRequest,
    ) -> dict:
        use_case: UpdateChannelModelUseCase = (
            container.channels.update_channel_model_use_case
        )
        await use_case.execute(
            UpdateChannelModelCommand(
                instance_id=_make_instance_id(body.instance_id),
                model_id=body.model_id,
                model_provider=body.model_provider,
            )
        )
        return {"ok": True}

    # ── PR-202: bindings (conversation ↔ channel-user) ─────────────────

    @router.get(
        f"{prefix}/bindings",
        name=f"channels.{slug}.get_bindings",
    )
    async def get_bindings(instance_id: str) -> dict:  # type: ignore[misc]
        use_case: GetChannelBindingsUseCase = (
            container.channels.get_channel_bindings_use_case
        )
        bindings_vo = await use_case.execute(
            _make_instance_id(instance_id)
        )
        return {"bindings": bindings_vo.as_dict()}

    @router.post(
        f"{prefix}/bindings",
        name=f"channels.{slug}.save_binding",
    )
    async def save_binding(  # type: ignore[misc]
        body: SaveChannelBindingRequest,
    ) -> dict:
        use_case: BindChannelConversationUseCase = (
            container.channels.bind_channel_conversation_use_case
        )
        await use_case.execute(
            BindChannelConversationCommand(
                instance_id=_make_instance_id(body.instance_id),
                conversation_id=body.conversation_id,
                channel_user_id=body.channel_user_id,
            )
        )
        # Empty ``channel_user_id`` clears the binding — surface
        # ``None`` on the wire so the front-end can render the cleared
        # state without round-tripping.
        return {
            "ok": True,
            "conversation_id": body.conversation_id,
            "channel_user_id": (
                body.channel_user_id if body.channel_user_id else None
            ),
        }

    @router.delete(
        f"{prefix}/bindings/{{conversation_id}}",
        name=f"channels.{slug}.delete_binding",
    )
    async def delete_binding(  # type: ignore[misc]
        conversation_id: str,
        instance_id: str,
    ) -> dict:
        use_case: UnbindChannelConversationUseCase = (
            container.channels.unbind_channel_conversation_use_case
        )
        await use_case.execute(
            UnbindChannelConversationCommand(
                instance_id=_make_instance_id(instance_id),
                conversation_id=conversation_id,
            )
        )
        return {"ok": True, "conversation_id": conversation_id}


# ---------------------------------------------------------------------------
# PR-204: QR-login WebSocket event loop
# ---------------------------------------------------------------------------


#: Heartbeat interval for the QR-login WS endpoint.  Servers send a
#: ``{"type": "ping"}`` envelope when no event has flowed for this many
#: seconds.  Keeps idle proxies (the legacy Forge proxy chain) from
#: closing the connection during a long ``ISSUED`` wait.
_QR_WS_HEARTBEAT_SECONDS = 30.0


async def _run_qr_event_loop(
    *,
    websocket: WebSocket,
    container: "Container",
    instance_id: str,
) -> None:
    """Pump filtered QR-login events from the bus into the WebSocket.

    Subscribes once per connection to the three QR domain events,
    filters by ``instance_id``, and forwards JSON envelopes to the
    client.  A heartbeat ``{"type": "ping"}`` is emitted every
    :data:`_QR_WS_HEARTBEAT_SECONDS` of idleness so misbehaving
    intermediaries do not silently drop the upgrade.

    Inbound client frames are accepted as keep-alive: a textual
    ``"ping"`` is answered with ``{"type": "pong"}``; everything else
    is ignored.  Disconnect is detected via :class:`WebSocketDisconnect`.
    """
    queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()

    async def _on_issued(envelope) -> None:  # type: ignore[no-untyped-def]
        ev = envelope.event
        if ev.instance_id != instance_id:
            return
        await queue.put(
            {
                "type": "qr_login.issued",
                "challenge_id": ev.challenge_id,
                "issued_at": ev.issued_at.isoformat(),
            }
        )

    async def _on_scanned(envelope) -> None:  # type: ignore[no-untyped-def]
        ev = envelope.event
        if ev.instance_id != instance_id:
            return
        await queue.put(
            {
                "type": "qr_login.scanned",
                "challenge_id": ev.challenge_id,
                "scanned_at": ev.scanned_at.isoformat(),
            }
        )

    async def _on_confirmed(envelope) -> None:  # type: ignore[no-untyped-def]
        ev = envelope.event
        if ev.instance_id != instance_id:
            return
        await queue.put(
            {
                "type": "qr_login.confirmed",
                "challenge_id": ev.challenge_id,
                "confirmed_at": ev.confirmed_at.isoformat(),
            }
        )

    bus = container.events
    sub_issued = await bus.subscribe(QrLoginIssuedEvent, _on_issued)
    sub_scanned = await bus.subscribe(QrLoginScannedEvent, _on_scanned)
    sub_confirmed = await bus.subscribe(
        QrLoginConfirmedEvent, _on_confirmed
    )

    async def _reader_task() -> None:
        """Consume client frames; reply ``pong`` for ``ping``.

        On disconnect, push a sentinel into the queue so the writer
        coroutine wakes up and exits.
        """
        try:
            while True:
                msg = await websocket.receive_text()
                if msg == "ping":
                    await queue.put({"type": "pong"})
        except WebSocketDisconnect:
            await queue.put(None)
        except RuntimeError:
            # ``receive_text`` after close can raise RuntimeError on
            # some Starlette versions — treat as a clean shutdown.
            await queue.put(None)

    reader = asyncio.create_task(_reader_task())

    try:
        while True:
            try:
                envelope_or_sentinel = await asyncio.wait_for(
                    queue.get(),
                    timeout=_QR_WS_HEARTBEAT_SECONDS,
                )
            except asyncio.TimeoutError:
                # Idle window expired — emit heartbeat.
                try:
                    await websocket.send_json({"type": "ping"})
                    continue
                except (WebSocketDisconnect, RuntimeError):
                    return

            if envelope_or_sentinel is None:
                # Disconnect sentinel from the reader task.
                return

            try:
                await websocket.send_json(envelope_or_sentinel)
            except (WebSocketDisconnect, RuntimeError):
                return
    finally:
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            # ``reader`` was cancelled by us above — that is expected and
            # benign. But if THIS coroutine is itself being cancelled
            # (outer cancel), ``await reader`` re-raises that and we MUST
            # let it propagate after best-effort unsubscribe cleanup.
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                await sub_confirmed.unsubscribe()
                await sub_scanned.unsubscribe()
                await sub_issued.unsubscribe()
                raise
        except Exception:  # noqa: BLE001 - reader cleanup is best-effort
            pass
        # Unsubscribe in reverse order to mirror subscribe ordering.
        await sub_confirmed.unsubscribe()
        await sub_scanned.unsubscribe()
        await sub_issued.unsubscribe()

