# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``channels`` bounded context (PR-047 + PR-201).

PR-047 replaced 11 in-memory ``_Fake<Port>`` adapters with real
adapters; PR-201 (this revision) adds two production-grade pieces:

1. **Real chat dispatch**: the bridge wired into
   :class:`SendChannelReplyUseCase` is now
   :class:`apps.api._chat_message_bridge.ChatMessageBridge`,
   which calls into ``ChatServices.{create_conversation,open_tab,
   stream_chat,close_tab}_use_case`` via the public
   ``ChatServices`` surface.  The interim ``EchoMessageBridge`` is
   retained as a class but no longer wired into production runtime
   here.
2. **Per-instance signing secret**: signature verifiers no longer share
   a hard-coded ``"qai-channels-default-verifier"`` token.  At verify
   time the new :class:`_PerInstanceSignatureVerifier` adapter:

   * Looks up ``container.secret_store.get("qai.channels.signing", instance_id)``.
   * Constructs a fresh per-kind verifier with that signing secret.
   * Delegates the verification.
   * Falls back to a constructor-supplied default token on
     :class:`NotFoundError` so dev / test scenarios that have not
     populated the SecretStore continue to work.  The fallback is
     **logged at WARNING level** every time it kicks in so production
     deployments cannot silently ride on the default.

Cross-context bridge
--------------------
``MessageBridgePort`` is the only seam from channels to chat /
ai_coding.  Per the import-linter ``context-isolation`` contract,
``qai.channels.*`` may NEVER import ``qai.chat.*``.  The bridge
implementation lives in :mod:`apps.api._chat_message_bridge`
(apps composition layer).

Field-name lock
---------------
``ChannelsServices`` field names are part of the route contract
(PR-036 §11) and PR-201 keeps every existing field unchanged.  No new
field is added — the per-instance signing secret is plumbed through
the existing ``WebhookSignatureVerifierPort`` slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from qai.platform.errors import NotFoundError
from qai.platform.logging import get_logger

from qai.channels.adapters import (
    ChannelTransportRegistry,
    FeishuQrLogin,
    FeishuTenantTokenCache,
    KindDispatchedPayloadParser,
    OutboundReplyDispatcher,
    RegexCommandParser,
    SecretStoreCredentialsResolver,
    SqliteChannelInstanceRepository,
    SqliteChannelMessageRepository,
    SqlitePendingMessageRepository,
    SqliteQrLoginChallengeRepository,
    SqliteSessionIndexRepository,
    WechatQrLogin,
)
from qai.channels.adapters.channel_tool_formatter import (
    ChannelToolFormatter,
)
from qai.channels.application.ports import (
    ChannelInstanceRepositoryPort,
    ChannelMessageRepositoryPort,
    ChannelTransportPort,
    CommandParserPort,
    CredentialsResolverPort,
    InboundTransportPort,
    SessionIndexRepositoryPort,
    WebhookSignatureVerifierPort,
)
from qai.channels.application.services.realtime_delivery import (
    PendingMessageQueue,
    RealtimeDeliveryConfig,
    RealtimeDeliveryService,
)
from qai.channels.application.use_cases.ingest_webhook import (
    IngestWebhookUseCase,
    SendChannelReplyUseCase,
)
from qai.channels.application.use_cases.manage_bindings import (
    BindChannelConversationUseCase,
    GetChannelBindingsUseCase,
    UnbindChannelConversationUseCase,
)
from qai.channels.application.use_cases.manage_lifecycle import (
    AcknowledgeChannelErrorUseCase,
    StartChannelInstanceUseCase,
    StopChannelInstanceUseCase,
)
from qai.channels.application.use_cases.manage_settings import (
    GetChannelSettingsUseCase,
    UpdateChannelConfigUseCase,
    UpdateChannelModelUseCase,
    UpdateChannelProxyUseCase,
)
from qai.channels.application.use_cases.qr_image import (
    LookupQrLoginChallengeUseCase,
    RenderQrImageUseCase,
)
from qai.channels.application.use_cases.qr_login import (
    ConfirmQrLoginUseCase,
    IssueQrLoginUseCase,
)
from qai.channels.application.use_cases.push_message import (
    PushChannelMessageUseCase,
)
from qai.channels.application.use_cases.register_channel_instance import (
    RegisterChannelInstanceUseCase,
)
from qai.channels.application.use_cases.session_index import (
    BindSessionIndexUseCase,
    LookupSessionIndexUseCase,
)
from qai.channels.application.use_cases.wechat_personal import (
    LogoutWechatPersonalUseCase,
)
from qai.channels.domain import (
    ChannelInstance,
    ChannelInstanceId,
    ChannelKind,
    ChannelMessage,
    WebhookSignatureInvalidError,
)
from qai.channels.infrastructure import (
    FeishuPayloadParser,
    FeishuSigVerifier,
    FeishuTransport,
    WechatPayloadParser,
    WechatSigVerifier,
    WechatTransport,
)
from qai.channels.adapters.qr_login import WechatPersonalQrLoginAdapter
from qai.channels.infrastructure.feishu_ws import FeishuWebSocketTransport
from qai.channels.infrastructure.transport_watchdog import TransportWatchdog
from qai.channels.infrastructure.wechat_longpoll import (
    WechatLongPollTransport,
)
from qai.platform.persistence.secrets import SecretStore

from ._ai_coding_channel_bridge import AiCodingChannelBridge
from ._channel_webui_broadcast import ChannelWebUIBroadcaster
from ._chat_message_bridge import ChatMessageBridge

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# ChannelsServices namespace
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChannelsServices:
    """Application services + ports for the ``channels`` bounded context.

    Field names are part of the route contract (PR-036 §11) and must
    NOT change between S3 (fakes) and S4 (real adapters).  PR-201
    preserves every field name unchanged.
    """

    # use cases
    register_channel_instance_use_case: RegisterChannelInstanceUseCase
    start_channel_instance_use_case: StartChannelInstanceUseCase
    stop_channel_instance_use_case: StopChannelInstanceUseCase
    acknowledge_channel_error_use_case: AcknowledgeChannelErrorUseCase
    ingest_webhook_use_case: IngestWebhookUseCase
    send_channel_reply_use_case: SendChannelReplyUseCase
    issue_qr_login_use_case: IssueQrLoginUseCase
    confirm_qr_login_use_case: ConfirmQrLoginUseCase
    bind_session_index_use_case: BindSessionIndexUseCase
    lookup_session_index_use_case: LookupSessionIndexUseCase
    # direct ports surfaced for read-only route paths
    instance_repository: ChannelInstanceRepositoryPort
    message_repository: ChannelMessageRepositoryPort
    session_repository: SessionIndexRepositoryPort
    credentials_resolver: CredentialsResolverPort
    transport_for_kind: ChannelTransportRegistry
    # PR-202: settings + bindings use cases (append-only — preserves
    # the field-name lock; the route layer in
    # ``interfaces/http/routes/channels.py`` wires the 27 new
    # settings/proxy/model/bindings endpoints to these slots).
    get_channel_settings_use_case: GetChannelSettingsUseCase
    update_channel_config_use_case: UpdateChannelConfigUseCase
    update_channel_proxy_use_case: UpdateChannelProxyUseCase
    update_channel_model_use_case: UpdateChannelModelUseCase
    get_channel_bindings_use_case: GetChannelBindingsUseCase
    bind_channel_conversation_use_case: BindChannelConversationUseCase
    unbind_channel_conversation_use_case: UnbindChannelConversationUseCase
    # PR-204: QR-image rendering + read-only challenge lookup
    # (append-only — preserves the field-name lock; the route layer
    # in ``interfaces/http/routes/channels.py`` wires the new
    # ``GET /api/{kind}/qr/{id}/image`` and
    # ``WS /api/{kind}/qr/events`` endpoints to these slots).
    render_qr_image_use_case: RenderQrImageUseCase
    lookup_qr_challenge_use_case: LookupQrLoginChallengeUseCase
    # PR-205: out-of-band push (replaces legacy ``send_to_user`` etc.
    # in ``backend/feishu_channel.py`` / ``backend/wechat_channel.py``;
    # ensures business capability survives the I2 PR-1104 deletion).
    # Append-only — preserves the §3.1 field-name lock.
    push_channel_message_use_case: PushChannelMessageUseCase
    # S9 PR-093: application-layer wiring for inbound transports +
    # rich realtime delivery.  All append-only — every existing field
    # name above is unchanged so the §3.1 field-name lock holds.
    #
    # * ``inbound_transport_for_kind`` — :class:`InboundTransportPort`
    #   registry; ``WechatLongPollTransport`` for personal WeChat
    #   long-poll, ``FeishuWebSocketTransport`` for Feishu WS.
    # * ``realtime_delivery_service`` — typing keepalive + 3-layer
    #   fallback wired to the channel-driver layer.
    # * ``ai_coding_channel_bridge`` — :class:`MessageBridgePort`
    #   front for ``qai.ai_coding`` (CC/OC); the dispatch bridge
    #   selects between this and the chat bridge based on whether
    #   the user has an active coding session.
    # * ``chat_message_bridge`` — re-exposes the bridge built above
    #   so the dispatch wiring can hold one reference.
    # * ``tool_formatter`` — :class:`ChannelToolFormatter` for
    #   rich tool-progress lines.
    inbound_transport_for_kind: dict[ChannelKind, InboundTransportPort]
    realtime_delivery_service: RealtimeDeliveryService
    ai_coding_channel_bridge: AiCodingChannelBridge
    chat_message_bridge: ChatMessageBridge
    tool_formatter: ChannelToolFormatter
    # PR-097 R-3 — surface the command parser so the apps-level
    # dispatch bridge in :mod:`apps.api._channel_dispatch_bridge`
    # can resolve channel verbs without reaching into the private
    # :class:`IngestWebhookUseCase`.  Append-only — preserves the
    # §3.1 field-name lock.
    command_parser: CommandParserPort
    # PR-097 R-13: live wechatbot SDK adapter for personal-WeChat
    # QR-login.  Append-only — preserves the §3.1 field-name lock.
    # Routes that handle ``POST /api/wechat/qr/trigger_login`` invoke
    # ``wechat_personal_qr_login.trigger_login(instance, force=...)``.
    wechat_personal_qr_login: WechatPersonalQrLoginAdapter
    # R16 (Clean-Arch correction): two-step personal-WeChat logout
    # orchestration (tear down SDK bot → stop channel instance) lifted
    # out of the ``POST /api/wechat/logout`` route into a use case so the
    # route stays thin.  Append-only — preserves the §3.1 field-name lock.
    logout_wechat_personal_use_case: LogoutWechatPersonalUseCase
    # PR-097 (S9 §6 R-19): WebUI live-update broadcaster — publishes
    # ``channels.webui.inbound`` / ``channels.webui.outbound`` events on
    # the platform EventBus so the chat-events SSE forwards them to the
    # browser.  Restores parity with the legacy
    # ``_broadcast_feishu_message`` / ``wechat_update_conv`` helpers.
    # Append-only — preserves the §3.1 field-name lock.
    webui_broadcaster: ChannelWebUIBroadcaster
    # PR-097 K-1 (Sub-L) — image-attachment ingestion plumbing.
    # The dispatch bridge needs three pieces to drive the per-kind
    # image decoders at the dispatch boundary (legacy parity row §6
    # R-8 — restores the inbound image flow that
    # ``backend/channels/wechat/channel.py:2174-2229`` and
    # ``backend/channels/feishu/channel.py:1444-1493`` provided):
    #
    # * ``feishu_token_cache_factory`` — supplies a per-instance
    #   :class:`FeishuTenantTokenCache` so
    #   :func:`qai.channels.adapters.feishu_image_decoder.build_image_content`
    #   can mint a tenant_access_token bearer.
    # * ``http_client_factory`` — async HTTP client factory threaded
    #   into the Feishu image decoder for the
    #   ``GET /open-apis/im/v1/messages/.../resources/...`` call.
    # * ``wechat_longpoll_transport`` — lets the dispatcher reach the
    #   live wechatbot SDK ``Bot`` (via ``_bot.download(msg_handle)``)
    #   when an inbound WeChat image attachment carries the SDK msg
    #   handle.
    #
    # All three are optional: missing dependencies cause the matching
    # provider's image attachments to degrade to text-only blocks
    # rather than block the dispatch path entirely.  Append-only —
    # preserves the §3.1 field-name lock.
    feishu_token_cache_factory: (
        Callable[[ChannelInstance], FeishuTenantTokenCache] | None
    ) = None
    http_client_factory: Callable[..., Any] | None = None
    wechat_longpoll_transport: WechatLongPollTransport | None = None


# ---------------------------------------------------------------------------
# SecretStore namespace + dev fallback for signing secrets
# ---------------------------------------------------------------------------

#: SecretStore service name for per-instance webhook signing secrets.
#: Each :class:`ChannelInstance` has its own record at
#: ``(SIGNING_SECRET_SERVICE, instance.instance_id.value)``.
SIGNING_SECRET_SERVICE = "qai.channels.signing"

#: SecretStore service name for per-instance Feishu app_secret.  Paired
#: with the ``app_id`` stored in the :class:`ChannelSettings`
#: ``kind_specific`` slot (key ``"app_id"``, serialised into the
#: ``settings_v1`` metadata blob); together they drive
#: :class:`FeishuTenantTokenCache` (PR-097 §2 R-2 Feishu half) and the
#: inbound :class:`FeishuWebSocketTransport`.  The route layer writes
#: the bare app_secret here (keyed by ``instance_id``) — the SAME single
#: namespace both read paths consume.
FEISHU_APP_SECRET_SERVICE = "qai.channels.feishu.app_secret"



#: Development fallback token used by the signature verifier when
#: SecretStore has no per-instance entry (typical for the in-test
#: ``TestClient`` path that registers an instance without first writing
#: a signing secret).  Production deployments MUST populate
#: ``qai.channels.signing/{instance_id}`` and never see this fallback —
#: every fallback emits a WARNING log line keyed by ``instance_id`` so
#: ops can grep for it.
#:
#: PR-201 keeps the *value* identical to the legacy
#: ``_DEFAULT_VERIFIER_TOKEN`` constant so
#: ``tests/integration/http/test_channels_routes.py`` (outside the L2
#: file domain) continues to compute matching signatures unchanged.
DEFAULT_DEV_SIGNING_SECRET = "qai-channels-default-verifier"


# ---------------------------------------------------------------------------
# Per-instance signature verifier
# ---------------------------------------------------------------------------


class _PerInstanceSignatureVerifier(WebhookSignatureVerifierPort):
    """:class:`WebhookSignatureVerifierPort` impl with per-instance secrets.

    For each call, looks up the signing secret in the SecretStore at
    ``(SIGNING_SECRET_SERVICE, instance_id)``; on
    :class:`NotFoundError`, falls back to the default development
    secret with a structured WARNING log line.

    A fresh per-kind verifier (:class:`WechatSigVerifier` /
    :class:`FeishuSigVerifier`) is
    constructed for each call.  The construction cost is negligible
    (three string assignments) and per-call construction sidesteps any
    threading / cache-invalidation concerns when the SecretStore value
    is rotated.

    The Protocol's ``verify(kind, raw_body, headers)`` shape is kept
    exactly — instance lookup happens via
    :meth:`verify_for_instance` which the ingest use case calls when
    it has the instance in scope.  When called via the plain
    Protocol-shape ``verify`` (no instance id), we fall back to the
    default dev secret immediately.

    This is **not** a new port shape — the use case continues to call
    ``self._verifier.verify(kind, raw_body, headers)``.  PR-201
    introduces the optional ``instance_id`` kw-arg additively on the
    Protocol; existing callers that omit it still work.
    """

    __slots__ = ("_secret_store", "_default_secret")

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        default_secret: str = DEFAULT_DEV_SIGNING_SECRET,
    ) -> None:
        self._secret_store = secret_store
        self._default_secret = default_secret

    def _resolve_secret(self, instance_id: str | None) -> str:
        """Return the signing secret for ``instance_id``.

        Falls back to :data:`DEFAULT_DEV_SIGNING_SECRET` when:

        * ``instance_id`` is ``None`` (legacy callers that have not
          adopted the additive kw-arg yet).
        * The SecretStore has no record for the instance.

        Both fallback paths emit a WARNING log so production missing
        the per-instance entry surfaces in the operator's log stream.
        """
        if instance_id is None:
            logger.warning(
                "channels.signing.fallback_no_instance",
                reason="instance_id not supplied to verifier",
            )
            return self._default_secret
        try:
            return self._secret_store.get(
                SIGNING_SECRET_SERVICE, instance_id
            )
        except NotFoundError:
            logger.warning(
                "channels.signing.fallback_dev_secret",
                instance_id=instance_id,
                reason="no per-instance signing secret in SecretStore",
            )
            return self._default_secret

    def verify(
        self,
        kind: ChannelKind,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        instance_id: str | None = None,
    ) -> None:
        secret = self._resolve_secret(instance_id)
        per_kind = _build_per_kind_verifier(kind, secret)
        per_kind.verify(kind, raw_body, headers)


def _build_per_kind_verifier(
    kind: ChannelKind, secret: str
) -> WebhookSignatureVerifierPort:
    """Construct a fresh per-kind verifier with the given signing secret."""
    if kind is ChannelKind.WECHAT:
        return WechatSigVerifier(token=secret)
    if kind is ChannelKind.FEISHU:
        return FeishuSigVerifier(secret=secret)
    # Defensive: KindDispatchedSignatureVerifier already raises
    # ChannelKindNotSupportedError before reaching here, but keep an
    # explicit branch so a future ChannelKind addition surfaces a clear
    # error instead of silently passing.
    raise WebhookSignatureInvalidError(
        kind.value,
        details={
            "reason": (
                "no signature verifier for kind "
                f"{kind.value!r}; supports wechat / feishu"
            )
        },
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_channels_services(container: "Container") -> ChannelsServices:
    """Wire :attr:`Container.channels` with PR-201 production adapters."""
    db = container.database
    clock = container.clock
    ids = container.ids
    events = container.events
    secret_store = container.secret_store

    # Live Settings.ssl_verify provider (single reused pattern; mirrors the
    # global-proxy provider). Threaded into the channel httpx REST clients so a
    # runtime SSL toggle hot-applies to new channel REST/identity clients. The
    # per-instance proxy TLS-skip (SdkNetworkPolicy, mechanism C) stays
    # INDEPENDENT and is NOT affected.
    from apps.api._global_proxy import build_ssl_verify_provider

    ssl_verify_provider = build_ssl_verify_provider(container)

    # ── repositories ──────────────────────────────────────────────────
    instances_repo = SqliteChannelInstanceRepository(db=db)
    messages_repo = SqliteChannelMessageRepository(db=db)
    sessions_repo = SqliteSessionIndexRepository(db=db)
    qr_repo = SqliteQrLoginChallengeRepository(db=db)

    # ── credentials ───────────────────────────────────────────────────
    credentials = SecretStoreCredentialsResolver(store=secret_store)

    # ── inbound transports (constructed before outbound) ────────────
    # PR-097 R-1: ``WechatTransport`` (outbound personal-WeChat) is a
    # delegate that calls ``WechatLongPollTransport._bot.send`` /
    # ``_bot.reply``; we therefore construct the inbound transport
    # first so the outbound dict can hold a reference to it.  The
    # FeishuWebSocketTransport mirrors the same pattern (Sub-H owns
    # FeishuTransport rich-text wiring).
    #
    # SDK network policy resolver — applies proxy + per-domain TLS-skip
    # to the wechatbot / lark_oapi SDK traffic for the connection
    # lifetime (V1 parity: the wechat/feishu channels patched
    # aiohttp/requests/websockets for proxy + enterprise-MITM TLS skip).
    # Built here because it needs SecretStore (proxy password) which the
    # transports do not otherwise hold.
    wechat_policy_resolver = _build_sdk_network_policy_resolver(
        secret_store=secret_store, kind=ChannelKind.WECHAT
    )
    feishu_policy_resolver = _build_sdk_network_policy_resolver(
        secret_store=secret_store, kind=ChannelKind.FEISHU
    )
    wechat_longpoll = WechatLongPollTransport(
        id_factory=ids,
        clock=clock,
        network_policy_resolver=wechat_policy_resolver,
    )
    feishu_ws = FeishuWebSocketTransport(
        id_factory=ids,
        clock=clock,
        network_policy_resolver=feishu_policy_resolver,
        ssl_verify=container.settings.ssl_verify,
        ssl_verify_provider=ssl_verify_provider,
    )

    # ── transports (one per kind) ─────────────────────────────────────
    # Feishu REST calls (tenant_access_token refresh, outbound send, image
    # download) go through httpx rather than the lark_oapi SDK.  The SDK network
    # policy above patches requests/websockets for enterprise MITM certificates,
    # but it cannot affect httpx; build the channel HTTP client with the same
    # TLS-relaxed behaviour so inbound Feishu messages can be answered on
    # corporate networks that inject a self-signed certificate chain.
    client_factory = _build_httpx_factory(
        ssl_verify=container.settings.ssl_verify,
        ssl_verify_provider=ssl_verify_provider,
    )
    # PR-097 §2 R-2 Feishu half: per-instance tenant_access_token cache
    # factory.  Resolves (app_id, app_secret) from instance metadata +
    # SecretStore on first send through each instance, then memoises
    # the cache so concurrent sends share token state.
    feishu_token_cache_factory = _build_feishu_token_cache_factory(
        secret_store=secret_store,
        client_factory=client_factory,
        ssl_verify=container.settings.ssl_verify,
        ssl_verify_provider=ssl_verify_provider,
    )
    transports: dict[ChannelKind, ChannelTransportPort] = {
        ChannelKind.WECHAT: WechatTransport(
            longpoll_transport=wechat_longpoll,
        ),
        ChannelKind.FEISHU: FeishuTransport(
            client_factory=client_factory,
            base_url=FeishuTransport.DEFAULT_BASE_URL,
            token_cache_factory=feishu_token_cache_factory,
        ),
    }
    transport_registry = ChannelTransportRegistry(transports)

    def _transport_factory(
        instance: ChannelInstance,
    ) -> ChannelTransportPort:
        return transport_registry(instance.kind)

    # ── signature verifier (per-instance, SecretStore-backed) ─────────
    verifier = _PerInstanceSignatureVerifier(
        secret_store=secret_store,
        default_secret=DEFAULT_DEV_SIGNING_SECRET,
    )

    # ── payload parsers (one per kind) ────────────────────────────────
    parser = KindDispatchedPayloadParser(
        {
            ChannelKind.WECHAT: WechatPayloadParser(),
            ChannelKind.FEISHU: FeishuPayloadParser(),
        }
    )

    # ── command parser ────────────────────────────────────────────────
    command_parser = RegexCommandParser()

    # ── QR login (per kind; feishu placeholder until PR-204) ──────────
    qr_dispatch: dict[ChannelKind, object] = {
        ChannelKind.WECHAT: WechatQrLogin(
            repo=qr_repo, ids=ids, clock=clock
        ),
        ChannelKind.FEISHU: FeishuQrLogin(),
    }
    qr = _QrDispatcher(qr_dispatch)

    # ── reply dispatcher ──────────────────────────────────────────────
    replies = OutboundReplyDispatcher(
        transport_factory=_transport_factory
    )

    # ── PR-097 R-5: inbound transport factory + consumer + watchdogs ──
    # The lifecycle use cases below take the inbound factory + consumer
    # callback so they can drive the long-poll loop on START and tear
    # it down on STOP.
    inbound_by_kind: dict[ChannelKind, InboundTransportPort] = {
        ChannelKind.WECHAT: wechat_longpoll,
        ChannelKind.FEISHU: feishu_ws,
    }

    def _inbound_factory(
        instance: ChannelInstance,
    ) -> InboundTransportPort | None:
        return inbound_by_kind.get(instance.kind)

    # Per-instance watchdog map — module-private to the builder; the
    # use cases call ``_watchdog_starter`` / ``_watchdog_stopper``
    # which manage entries.  Keyed by ChannelInstanceId value.
    watchdogs: dict[str, TransportWatchdog] = {}

    async def _watchdog_starter(
        instance: ChannelInstance,
        inbound_transport: InboundTransportPort,
    ) -> None:
        """Spawn a watchdog supervising the inbound transport.

        The watchdog uses :class:`TransportWatchdog`'s default
        5/10/30/60/120s reconnect backoff — matches the legacy
        ``backend/channels/wechat/channel.py:834 _watchdog_loop``.
        """
        # WatchableTransport requires ``is_alive`` + ``restart`` —
        # the personal-WeChat long-poll transport implements both;
        # the Feishu WS transport implements them too (Sub-H).
        wd = TransportWatchdog(transport=inbound_transport)
        wd.start()
        watchdogs[instance.instance_id.value] = wd

    async def _watchdog_stopper(instance: ChannelInstance) -> None:
        wd = watchdogs.pop(instance.instance_id.value, None)
        if wd is not None:
            await wd.stop()

    # The actual consumer callback (channel_msg → dispatch bridge) is
    # constructed by ``apps.api._channel_dispatch_bridge`` consumers
    # at the wiring layer; PR-097 R-5 publishes the inbound message
    # via the realtime delivery service so any subscriber (route /
    # tests) can observe it.  The bridge wiring lives in Sub-J's
    # scope; here we provide a default that records the message via
    # the message repository so messages persist even when no live
    # subscriber is attached.
    # The inbound consumer drives the full dispatch pipeline so an
    # inbound long-poll / WS message produces an LLM reply that is sent
    # back to the channel — restoring V1's "user sends message → bot
    # replies" behaviour.  Personal-WeChat (long-poll) and Feishu (WS)
    # both feed their ``stream`` into this consumer via the lifecycle
    # use case (``manage_lifecycle._drain_inbound_stream``).
    #
    # Why dispatch here (not just persist): the prior implementation only
    # ``messages_repo.save(message)``'d and never called
    # :func:`dispatch_inbound_message`, so a connected Feishu/WeChat
    # channel received messages but never replied (the "连接成功但发消息
    # 无反应" bug).  ``dispatch_inbound_message`` internally drives the
    # realtime delivery service whose default ``send_to_user`` resolves
    # the per-kind outbound transport and sends the reply back, so simply
    # exhausting the async generator both generates AND delivers the
    # reply.
    def _build_inbound_dispatch_services(msg_instance: ChannelInstance):  # type: ignore[no-untyped-def]
        from qai.channels.application.use_cases.conversation_commands import (
            HandleConversationCommandUseCase,
        )

        from ._channel_dispatch_bridge import DispatchServices
        from ._channel_grant_bridge import ChannelGrantBridge
        from ._conversation_command_adapter import (
            ChatConversationCommandAdapter,
        )

        security = getattr(container, "security", None)
        grant_bridge = (
            ChannelGrantBridge(
                security_services=security,
                channel_session_index_repo=sessions_repo,
            )
            if security is not None
            else None
        )
        reboot_scheduler = getattr(container, "reboot_scheduler", None)
        # Wire the 5 chat-conversation slash commands (/list /use /status
        # /rename /delete).  Without this the dispatch bridge's ``conv_uc is
        # None`` branch replied "⚠️ 会话管理命令当前不可用" — a regression vs
        # V0.5 (``session_commands.py:288-600``, 微信/飞书已验证可用).  The
        # adapter crosses into qai.chat (allowed in the apps composition root)
        # and is gated on the chat namespace being present.
        conversation_command_use_case = None
        if getattr(container, "chat", None) is not None:
            conversation_command_use_case = HandleConversationCommandUseCase(
                port=ChatConversationCommandAdapter(container=container)
            )
        return DispatchServices(
            chat_bridge=bridge,
            ai_coding_bridge=ai_coding_bridge,
            grant_bridge=grant_bridge,
            command_parser=command_parser,
            channel_session_repo=sessions_repo,
            delivery_service=delivery_service,
            tool_formatter=tool_formatter,
            container=container,
            reboot_scheduler=reboot_scheduler,
            conversation_command_use_case=conversation_command_use_case,
        )

    async def _default_inbound_consumer(
        instance: ChannelInstance, message: "ChannelMessage"
    ) -> None:
        logger.info(
            "channels.inbound.received",
            instance_id=instance.instance_id.value,
            kind=instance.kind.value,
            message_id=message.message_id.value,
            sender=message.sender.value,
            text_preview=message.content.text[:80] if message.content.text else "",
        )
        # 1. Persist the inbound message so audit / replay works.
        try:
            await messages_repo.save(message)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.inbound.persist_failed",
                instance_id=instance.instance_id.value,
                error=str(exc),
            )
        # 2. Drive the dispatch pipeline (command parse → CC/OC or chat
        #    LLM stream → realtime delivery → outbound transport).  We
        #    exhaust the async generator; the realtime delivery service
        #    handles sending the reply back to the channel user.  We also
        #    accumulate the final (non-partial) REPLY text so we can emit a
        #    single ``channels.webui.outbound`` event once the turn finishes
        #    (H2 — mirrors v0.5 broadcasting the assistant turn to the WebUI).
        _outbound_parts: list[str] = []
        try:
            from ._channel_dispatch_bridge import (
                OutboundFrameKind,
                dispatch_inbound_message,
            )

            services = _build_inbound_dispatch_services(instance)
            async for _frame in dispatch_inbound_message(
                message, services=services
            ):
                # The frames are also delivered in-band by the delivery
                # service; we drain them here so the generator runs to
                # completion (its side effects send the reply).
                #
                # WebUI parity: only FINAL assistant replies (REPLY) are joined
                # into the sidebar's outbound bubble, so it matches what gets
                # persisted (answer text only; sub-agent progress is folded into
                # separate blocks server-side). PROGRESS frames are delivered
                # live to the channel via ``session.deliver`` and surface in the
                # WebUI through the streaming SSE path — they must NOT be
                # concatenated into the final-answer text here, or the realtime
                # sidebar bubble would briefly differ from the persisted form
                # (progress lines glued onto the answer). Partial text deltas
                # likewise stay live-only.
                if (
                    getattr(_frame, "kind", None) == OutboundFrameKind.REPLY
                    and _frame.text
                    and ("partial", "true") not in _frame.metadata
                ):
                    _outbound_parts.append(_frame.text)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "channels.inbound.dispatch_failed",
                instance_id=instance.instance_id.value,
                message_id=message.message_id.value,
                error=str(exc),
                exc_info=True,
            )
        # 3. Surface the inbound + outbound turn to the WebUI sidebar in real
        #    time (V1 ``wechat/channel.py:1418`` / ``feishu/channel.py:1508``
        #    broadcast ``wechat_update_conv`` / ``feishu_update_conv`` with the
        #    REAL conv_id + new messages so the history list refreshes
        #    instantly).  We resolve the conversation id the dispatch bridge
        #    minted / restored for this (instance, sender) AFTER dispatch — it
        #    is empty before the chat bridge runs.  Without a real conv_id the
        #    WebUI cannot key the sidebar row (the "历史会话不即时刷新" bug).
        #    Best-effort: a broadcast hiccup never blocks the reply path.
        conversation_id = ""
        try:
            from ._chat_message_bridge import get_conversation_for_user

            # Group messages key their conversation by group_id;
            # p2p messages key by sender.
            _group_id = getattr(message, "group_id", None)
            _conv_key = _group_id if _group_id else message.sender.value
            conversation_id = (
                get_conversation_for_user(
                    instance.instance_id, _conv_key
                )
                or ""
            )
        except Exception:  # noqa: BLE001 — resolution is best-effort
            conversation_id = ""
        logger.info(
            "channels.inbound.webui_broadcast_check",
            instance_id=instance.instance_id.value,
            kind=instance.kind.value,
            sender=message.sender.value,
            conversation_id=conversation_id or "(empty)",
            outbound_parts=len(_outbound_parts),
        )
        # Only broadcast when we have a real conversation to address — a
        # command-only turn (e.g. ``/status``) never mints one, and an empty
        # conv_id frame the WebUI cannot insert (V1 ``useChat.js:2938``
        # ``if (!conv_id ...) return``).
        if conversation_id:
            try:
                await webui_broadcaster.broadcast_inbound(
                    instance=instance,
                    sender=message.sender,
                    text=message.content.text,
                    conversation_id=conversation_id,
                )
            except Exception:  # noqa: BLE001 — broadcast is best-effort
                pass
            if _outbound_parts:
                try:
                    await webui_broadcaster.broadcast_outbound(
                        instance=instance,
                        target=message.sender,
                        text="".join(_outbound_parts),
                        conversation_id=conversation_id,
                    )
                except Exception:  # noqa: BLE001 — broadcast is best-effort
                    pass

    # ── message bridge — PR-201 activates real chat dispatch ──────────
    bridge = ChatMessageBridge(container=container)

    # ── use cases ─────────────────────────────────────────────────────
    register_uc = RegisterChannelInstanceUseCase(
        instances=instances_repo,
        credentials=credentials,
        ids=ids,
        clock=clock,
    )
    start_uc = StartChannelInstanceUseCase(
        instances=instances_repo,
        credentials=credentials,
        transport_factory=_transport_factory,
        events=events,
        clock=clock,
        # PR-097 R-5 — inbound long-poll lifecycle for WeChat / Feishu.
        inbound_transport_factory=_inbound_factory,
        inbound_consumer=_default_inbound_consumer,
        watchdog_starter=_watchdog_starter,
    )
    stop_uc = StopChannelInstanceUseCase(
        instances=instances_repo,
        transport_factory=_transport_factory,
        events=events,
        clock=clock,
        inbound_transport_factory=_inbound_factory,
        watchdog_stopper=_watchdog_stopper,
    )
    acknowledge_uc = AcknowledgeChannelErrorUseCase(
        instances=instances_repo,
        events=events,
        clock=clock,
    )
    ingest_uc = IngestWebhookUseCase(
        instances=instances_repo,
        messages=messages_repo,
        verifier=verifier,
        parser=parser,
        commands=command_parser,
        ids=ids,
        events=events,
        clock=clock,
    )
    reply_uc = SendChannelReplyUseCase(
        instances=instances_repo,
        messages=messages_repo,
        replies=replies,
        events=events,
        clock=clock,
    )
    issue_qr_uc = IssueQrLoginUseCase(
        instances=instances_repo,
        qr=qr,
        events=events,
        clock=clock,
    )
    confirm_qr_uc = ConfirmQrLoginUseCase(
        instances=instances_repo,
        qr=qr,
        events=events,
        clock=clock,
    )
    bind_uc = BindSessionIndexUseCase(
        instances=instances_repo,
        sessions=sessions_repo,
        events=events,
        clock=clock,
    )
    lookup_uc = LookupSessionIndexUseCase(sessions=sessions_repo)

    # ── PR-202: settings + bindings use cases ─────────────────────────
    get_settings_uc = GetChannelSettingsUseCase(
        instances=instances_repo, clock=clock
    )
    update_config_uc = UpdateChannelConfigUseCase(
        instances=instances_repo, clock=clock
    )
    update_proxy_uc = UpdateChannelProxyUseCase(
        instances=instances_repo, clock=clock
    )
    update_model_uc = UpdateChannelModelUseCase(
        instances=instances_repo, clock=clock
    )
    get_bindings_uc = GetChannelBindingsUseCase(
        instances=instances_repo, clock=clock
    )
    bind_conversation_uc = BindChannelConversationUseCase(
        instances=instances_repo, clock=clock
    )
    unbind_conversation_uc = UnbindChannelConversationUseCase(
        instances=instances_repo, clock=clock
    )

    # ── PR-204: QR-image rendering + read-only challenge lookup ───────
    lookup_qr_uc = LookupQrLoginChallengeUseCase(
        instances=instances_repo,
        challenges=qr_repo,
    )
    render_qr_image_uc = RenderQrImageUseCase(lookup=lookup_qr_uc)

    # ── PR-205: out-of-band push (replaces legacy ``send_to_user``) ──
    push_message_uc = PushChannelMessageUseCase(
        instances=instances_repo,
        transport_factory=_transport_factory,
        clock=clock,
        events=events,
    )

    # ── S9 PR-093 + PR-097 R-5: inbound transports map ─────────────────
    # InboundTransportPort registry — one impl per provider with a
    # known long-poll / WS contract.  The transport instances themselves
    # were constructed earlier so :class:`WechatTransport` (PR-097 R-1)
    # could hold a reference to the long-poll transport, and the
    # lifecycle use cases (PR-097 R-5) drive ``start`` / ``stop`` on
    # them via ``_inbound_factory`` defined above.
    inbound_transports: dict[ChannelKind, InboundTransportPort] = dict(
        inbound_by_kind
    )
    # Realtime delivery — config sourced from
    # Settings.channels.context_token_age_guard_seconds (G-Agent
    # delivered the Settings field; we read it here so PR-093 wiring
    # honours the documented value).
    settings = getattr(container, "settings", None)
    channels_settings = (
        getattr(settings, "channels", None) if settings is not None else None
    )
    age_guard = (
        float(channels_settings.context_token_age_guard_seconds)
        if channels_settings is not None
        and hasattr(channels_settings, "context_token_age_guard_seconds")
        else 90.0
    )
    delivery_service = RealtimeDeliveryService(
        config=RealtimeDeliveryConfig(
            context_token_age_guard_seconds=age_guard,
        ),
        pending_queue=PendingMessageQueue(
            # PR-097 (S9 §6 R-20): persist Layer-3 messages so a server
            # restart does not silently drop CC results the user never
            # saw — restoring parity with the legacy
            # ``_pending_cc_results`` map.
            _store=SqlitePendingMessageRepository(db=db),
        ),
    )
    # Bridges — both lazy/duck-typed so they don't crash when chat /
    # ai_coding namespaces are not yet wired during early bootstrap.
    ai_coding_bridge = AiCodingChannelBridge(container=container)
    tool_formatter = ChannelToolFormatter()

    # PR-097 R-13: live wechatbot SDK QR-login adapter for personal
    # WeChat.  Constructed alongside the long-poll transport so both
    # share a consistent SDK lifecycle expectation; routes invoke
    # ``trigger_login`` to drive the real ``bot.login(force=...)``
    # flow restored from ``backend/channels/wechat/channel.py:498``.
    wechat_personal_qr_login_adapter = WechatPersonalQrLoginAdapter(
        repo=qr_repo,
        ids=ids,
        clock=clock,
        network_policy_resolver=wechat_policy_resolver,
        # When the SDK login() returns (user confirmed the scan), bring
        # the channel instance up so the long-poll transport starts
        # receiving messages — V1/v0.5 parity (single bot logged-in then
        # polled; V2 reuses the scanned creds for the inbound transport,
        # so no second scan).  Resolves the instance by its value id and
        # drives the existing lifecycle use case.
        on_confirmed=lambda instance_id_value: start_uc.execute(
            ChannelInstanceId(value=instance_id_value)
        ),
        # Method A (single-bot, V1 parity): hand the already-logged-in bot to
        # the long-poll transport so its ``start`` adopts it and runs the
        # receive loop on the SAME bot — no second bot, no second login, no
        # second QR (the "反复弹码" root cause was a second bot built with a
        # mismatched cred_path re-popping the QR).  Fired by ``_run_login``
        # right after ``bot.login()`` succeeds, before ``on_confirmed``.
        on_bot_ready=wechat_longpoll.adopt_bot,
        # On a FORCED login, delete the per-instance scanned-session file
        # so the SDK shows a fresh QR (v0.5 ``stop_channel(clear_creds)``
        # parity). Reuse the long-poll transport's path convention so the
        # exact same file the SDK persists is the one removed.  Also used to
        # give the QR-login bot the SAME per-instance cred_path the transport
        # uses (Method A: the bot the transport adopts persists its session
        # where the transport / a later restart expects it).
        creds_path_resolver=wechat_longpoll._cred_path_for,
    )

    # ── R16 — personal-WeChat logout orchestration use case ──────────
    # Wraps the two-step "logout SDK bot → stop channel instance" the
    # ``POST /api/wechat/logout`` route previously inlined.
    logout_wechat_personal_uc = LogoutWechatPersonalUseCase(
        qr_login=wechat_personal_qr_login_adapter,
        stop_use_case=stop_uc,
    )

    # ── PR-097 (S9 §6 R-19): WebUI live-update broadcaster ───────────
    # Publishes channel inbound / outbound events on the platform
    # EventBus so the chat-events SSE pushes them to the browser,
    # restoring parity with the legacy broadcast helpers.
    webui_broadcaster = ChannelWebUIBroadcaster(event_bus=events)

    return ChannelsServices(
        register_channel_instance_use_case=register_uc,
        start_channel_instance_use_case=start_uc,
        stop_channel_instance_use_case=stop_uc,
        acknowledge_channel_error_use_case=acknowledge_uc,
        ingest_webhook_use_case=ingest_uc,
        send_channel_reply_use_case=reply_uc,
        issue_qr_login_use_case=issue_qr_uc,
        confirm_qr_login_use_case=confirm_qr_uc,
        bind_session_index_use_case=bind_uc,
        lookup_session_index_use_case=lookup_uc,
        instance_repository=instances_repo,
        message_repository=messages_repo,
        session_repository=sessions_repo,
        credentials_resolver=credentials,
        transport_for_kind=transport_registry,
        get_channel_settings_use_case=get_settings_uc,
        update_channel_config_use_case=update_config_uc,
        update_channel_proxy_use_case=update_proxy_uc,
        update_channel_model_use_case=update_model_uc,
        get_channel_bindings_use_case=get_bindings_uc,
        bind_channel_conversation_use_case=bind_conversation_uc,
        unbind_channel_conversation_use_case=unbind_conversation_uc,
        render_qr_image_use_case=render_qr_image_uc,
        lookup_qr_challenge_use_case=lookup_qr_uc,
        push_channel_message_use_case=push_message_uc,
        # S9 PR-093 — inbound transports + delivery + bridges
        inbound_transport_for_kind=inbound_transports,
        realtime_delivery_service=delivery_service,
        ai_coding_channel_bridge=ai_coding_bridge,
        chat_message_bridge=bridge,
        tool_formatter=tool_formatter,
        # PR-097 R-3 — apps-level dispatch needs the command parser.
        command_parser=command_parser,
        # PR-097 K-1 (Sub-L) — image-attachment ingestion plumbing for
        # the apps-layer dispatch bridge.  ``feishu_token_cache_factory``
        # + ``http_client_factory`` feed
        # :func:`qai.channels.adapters.feishu_image_decoder.build_image_content`
        # and ``wechat_longpoll_transport`` exposes the live wechatbot
        # SDK ``Bot`` so :func:`qai.channels.adapters.wechat_image_decoder.build_image_content`
        # can call ``_bot.download(msg_handle)``.
        feishu_token_cache_factory=feishu_token_cache_factory,
        http_client_factory=client_factory,
        wechat_longpoll_transport=wechat_longpoll,
        # PR-097 R-13 + R-19 — personal-WeChat QR-login adapter wired
        # alongside the long-poll transport, plus the WebUI broadcaster
        # publishing inbound/outbound channel events on the platform
        # EventBus for the chat-events SSE consumer.
        wechat_personal_qr_login=wechat_personal_qr_login_adapter,
        logout_wechat_personal_use_case=logout_wechat_personal_uc,
        webui_broadcaster=webui_broadcaster,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _QrDispatcher:
    """Dispatches :class:`QrLoginPort` calls by ``instance.kind``.

    The channel use cases see one :class:`QrLoginPort` instance; this
    helper forwards each method call to the right per-kind adapter.
    """

    __slots__ = ("_handlers",)

    def __init__(self, handlers: dict[ChannelKind, object]) -> None:
        self._handlers: dict[ChannelKind, object] = dict(handlers)

    def _resolve(self, instance: ChannelInstance) -> object:
        try:
            return self._handlers[instance.kind]
        except KeyError as exc:
            from qai.channels.domain import ChannelKindNotSupportedError

            raise ChannelKindNotSupportedError(
                instance.kind.value
            ) from exc

    async def issue(self, instance: ChannelInstance):  # type: ignore[no-untyped-def]
        return await self._resolve(instance).issue(instance)  # type: ignore[attr-defined]

    async def check_status(  # type: ignore[no-untyped-def]
        self, instance: ChannelInstance, challenge_id: str
    ):
        return await self._resolve(instance).check_status(  # type: ignore[attr-defined]
            instance, challenge_id
        )

    async def confirm(  # type: ignore[no-untyped-def]
        self, instance: ChannelInstance, challenge_id: str
    ):
        return await self._resolve(instance).confirm(  # type: ignore[attr-defined]
            instance, challenge_id
        )


def _build_httpx_factory(*, ssl_verify: bool = True, ssl_verify_provider=None):  # type: ignore[no-untyped-def]
    """Return a callable that yields an ``httpx.AsyncClient``.

    Lazy-imports ``httpx`` so that test environments without httpx
    (highly unlikely — it's a required dep) still load the channels
    DI module.

    ``ssl_verify`` is intentionally configurable because Feishu channel REST
    calls are made by httpx, not by the lark_oapi SDK.  The SDK-side network
    patch relaxes TLS for requests/websockets only; without threading the same
    policy here, tenant_access_token refresh fails behind corporate/self-signed
    TLS gateways and the bot receives messages but cannot reply.

    ``ssl_verify_provider`` (``Callable[[], bool] | None``) is the LIVE
    Settings.ssl_verify provider; when present it is read inside ``_factory``
    (each client build) so a runtime SSL toggle hot-applies to new channel REST
    clients. Falls back to the frozen ``ssl_verify`` bool otherwise. The
    per-instance proxy TLS-skip (SdkNetworkPolicy, mechanism C) stays
    INDEPENDENT — this only controls the global-toggle verify of the httpx REST
    client.
    """
    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "httpx is required for channels transports; install it via "
            "the project's pyproject.toml"
        ) from exc

    def _factory(*, timeout: float):
        verify = (
            ssl_verify_provider() if ssl_verify_provider is not None else ssl_verify
        )
        return httpx.AsyncClient(timeout=timeout, verify=verify)

    return _factory


#: SecretStore service used for the per-instance proxy password (mirror
#: of the constant in ``interfaces/http/routes/channels.py`` so the DI
#: resolver reads the same record the proxy-save route writes).
PROXY_PASSWORD_SECRET_SERVICE = "qai.channels.proxy"


def _build_sdk_network_policy_resolver(
    *,
    secret_store: SecretStore,
    kind: ChannelKind,
):  # type: ignore[no-untyped-def]
    """Return ``(instance) -> SdkNetworkPolicy | None`` for a channel kind.

    Reads the per-instance proxy config (url / username from the
    instance settings, password from the SecretStore at
    ``(PROXY_PASSWORD_SECRET_SERVICE, instance_id)``) and produces a
    :class:`SdkNetworkPolicy` that:

    * routes the SDK's traffic through the configured proxy (if any), and
    * skips TLS verification for the provider's own domains (V1 parity:
      enterprise proxies present a MITM cert the default trust store
      rejects).

    Returns ``None`` on unexpected error (best-effort — the transport
    then connects without a policy).  An empty proxy still yields a
    policy whose sole effect is the per-domain TLS skip, matching V1's
    behaviour of always relaxing TLS for the channel host while
    connected.
    """
    from qai.channels.infrastructure.sdk_network import (
        FEISHU_TLS_SKIP_DOMAINS,
        WECHAT_TLS_SKIP_DOMAINS,
        SdkNetworkPolicy,
    )

    if kind is ChannelKind.WECHAT:
        domains = WECHAT_TLS_SKIP_DOMAINS
        patch_aiohttp, patch_requests, patch_websockets = True, False, False
    else:  # FEISHU
        domains = FEISHU_TLS_SKIP_DOMAINS
        patch_aiohttp, patch_requests, patch_websockets = False, True, True

    def _resolver(instance: ChannelInstance):  # type: ignore[no-untyped-def]
        try:
            settings = instance.get_settings()
            proxy = settings.proxy
            proxy_url = proxy.url or ""
            proxy_username = proxy.username or ""
            proxy_password = ""
            if proxy_url and proxy.has_password:
                try:
                    proxy_password = secret_store.get(
                        PROXY_PASSWORD_SECRET_SERVICE,
                        instance.instance_id.value,
                    )
                except NotFoundError:
                    proxy_password = ""
            return SdkNetworkPolicy(
                proxy_url=proxy_url,
                proxy_username=proxy_username,
                proxy_password=proxy_password,
                tls_skip_domains=domains,
                patch_aiohttp=patch_aiohttp,
                patch_requests=patch_requests,
                patch_websockets=patch_websockets,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort policy
            logger.warning(
                "channels.sdk_network.resolve_failed",
                instance_id=instance.instance_id.value,
                error=str(exc),
            )
            return None

    return _resolver


def _resolve_instance_proxy_url(
    *,
    secret_store: SecretStore,
    instance: ChannelInstance,
) -> str:
    """Return the per-instance channel proxy URL with ``user:pass@`` spliced in.

    7.3 — the Feishu tenant_access_token refresh is a channel (mechanism C),
    per-instance concern. Reads the same proxy config the SDK network policy
    resolver uses (url / username from instance settings, password from the
    SecretStore at ``(PROXY_PASSWORD_SECRET_SERVICE, instance_id)``) and
    returns an inline-auth ``http(s)://[user:pass@]host:port`` URL, or ``""``
    when no proxy is configured (caller then connects directly — proxy never
    forced; State-Truth-First). The password is spliced into the in-memory URL
    only and never logged.
    """
    try:
        settings = instance.get_settings()
        proxy = settings.proxy
        base = (proxy.url or "").strip()
        if not base:
            return ""
        username = (proxy.username or "").strip()
        password = ""
        if proxy.has_password:
            try:
                password = secret_store.get(
                    PROXY_PASSWORD_SECRET_SERVICE, instance.instance_id.value
                )
            except NotFoundError:
                password = ""
        if not (username and password):
            return base
        from urllib.parse import quote, urlparse, urlunparse

        parsed = urlparse(base)
        auth_netloc = (
            f"{quote(username, safe='')}:{quote(password, safe='')}"
            f"@{parsed.hostname or ''}"
        )
        if parsed.port:
            auth_netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=auth_netloc))
    except Exception as exc:  # noqa: BLE001 — best-effort; direct on failure
        logger.warning(
            "channels.feishu.token_proxy_resolve_failed",
            instance_id=instance.instance_id.value,
            error=str(exc),
        )
        return ""


def _build_feishu_token_cache_factory(
    *,
    secret_store: SecretStore,
    client_factory,  # type: ignore[no-untyped-def]
    ssl_verify: bool = True,
    ssl_verify_provider=None,  # type: ignore[no-untyped-def]
):  # type: ignore[no-untyped-def]
    """Return a per-instance :class:`FeishuTenantTokenCache` factory.

    Caches one :class:`FeishuTenantTokenCache` per
    ``ChannelInstance.instance_id`` so concurrent sends through the
    same instance share token state (the cache itself coalesces
    refresh callers behind an ``asyncio.Lock``).

    Credentials sourcing (PR-097 §2 R-2 Feishu half):

    * ``app_id`` — ``ChannelInstance.get_settings().kind_specific``
      key ``"app_id"`` (the :class:`ChannelSettings` ``kind_specific``
      slot for Feishu, serialised into the ``settings_v1`` metadata
      blob and written by ``POST /api/feishu/config``).
    * ``app_secret`` — :class:`SecretStore` record at
      ``(FEISHU_APP_SECRET_SERVICE, instance.instance_id.value)``.

    Both are required; missing either raises a clear
    :class:`ExternalServiceError` at first send time so the operator
    sees a deterministic failure rather than a silent token-refresh
    loop.
    """
    caches: dict[str, FeishuTenantTokenCache] = {}

    def _factory(instance: ChannelInstance) -> FeishuTenantTokenCache:
        instance_id = instance.instance_id.value
        cache = caches.get(instance_id)
        if cache is not None:
            return cache
        # ``app_id`` lives in the persisted ``ChannelSettings``
        # ``kind_specific`` slot (written by ``POST /api/feishu/config``
        # and serialised into the ``settings_v1`` metadata blob), NOT as
        # a flat top-level ``metadata`` key — read it from there so the
        # outbound token cache and the inbound WebSocket transport share
        # the SAME app_id source (was a real-send bug: the old flat
        # ``metadata["app_id"]`` scan never matched the nested blob).
        app_id = ""
        for k, v in instance.get_settings().kind_specific:
            if k == "app_id":
                app_id = v
                break
        if not app_id:
            from qai.platform.errors import ExternalServiceError

            raise ExternalServiceError(
                "channels.feishu.token_refresh_failed",
                f"feishu instance {instance_id!r} is missing the "
                "'app_id' setting; configure ChannelSettings "
                "kind_specific['app_id'] before starting the channel",
                service="feishu",
            )
        try:
            app_secret = secret_store.get(
                FEISHU_APP_SECRET_SERVICE, instance_id
            )
        except NotFoundError as exc:
            from qai.platform.errors import ExternalServiceError

            raise ExternalServiceError(
                "channels.feishu.token_refresh_failed",
                f"feishu instance {instance_id!r} has no app_secret "
                "in SecretStore; populate "
                f"({FEISHU_APP_SECRET_SERVICE!r}, {instance_id!r}) "
                "before starting the channel",
                service="feishu",
                cause=exc,
            ) from exc
        cache = FeishuTenantTokenCache(
            http_client_factory=_proxied_client_factory(
                client_factory=client_factory,
                proxy_url=_resolve_instance_proxy_url(
                    secret_store=secret_store, instance=instance
                ),
                ssl_verify=ssl_verify,
                ssl_verify_provider=ssl_verify_provider,
            ),
            app_id=app_id,
            app_secret=app_secret,
        )
        caches[instance_id] = cache
        return cache

    return _factory


def _proxied_client_factory(
    *,
    client_factory,  # type: ignore[no-untyped-def]
    proxy_url: str,
    ssl_verify: bool = True,
    ssl_verify_provider=None,  # type: ignore[no-untyped-def]
):  # type: ignore[no-untyped-def]
    """Wrap ``client_factory`` so its httpx client routes through ``proxy_url``.

    7.3 — the base ``client_factory`` (``_build_httpx_factory``) builds a plain
    ``httpx.AsyncClient(timeout=...)`` with no proxy, so the Feishu token
    refresh previously went direct even when the channel had a proxy
    configured (and httpx is NOT covered by the ``requests.Session`` SDK patch).
    When ``proxy_url`` is set we build an ``httpx.AsyncClient(timeout=...,
    proxy=proxy_url)`` so the token refresh honours the channel proxy
    (mechanism C). Empty ``proxy_url`` → delegate unchanged (direct connection;
    proxy never forced).

    ``ssl_verify_provider`` is the LIVE Settings.ssl_verify provider; read inside
    ``_factory`` so a runtime SSL toggle hot-applies. Frozen bool fallback.
    """
    if not proxy_url:
        return client_factory

    def _factory(*, timeout: float):  # type: ignore[no-untyped-def]
        import httpx

        verify = (
            ssl_verify_provider() if ssl_verify_provider is not None else ssl_verify
        )
        return httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy_url,
            verify=verify,
        )

    return _factory


__all__ = [
    "ChannelsServices",
    "build_channels_services",
    "DEFAULT_DEV_SIGNING_SECRET",
    "SIGNING_SECRET_SERVICE",
    "FEISHU_APP_SECRET_SERVICE",
]
