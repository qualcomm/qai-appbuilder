# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Inbound Feishu / Lark WebSocket transport (S9 PR-093 §2.1 C-8).

Restores the Feishu inbound message reception that the legacy
``backend/channels/feishu/channel.py:_ws_handler`` provided via the
``lark_oapi`` SDK's WebSocket client.  The new architecture had
outbound HTTP transports
(:class:`qai.channels.infrastructure.transports.FeishuTransport`) but
no inbound counterpart — addressed by parity-audit row §2.1 C-8.

This file is named ``feishu_ws.py`` and lives at
``qai.channels.infrastructure.feishu_ws`` to avoid colliding with
the existing ``qai.channels.infrastructure.transports`` module
(see :mod:`qai.channels.infrastructure.wechat_longpoll` for the same
naming convention rationale).

Implements :class:`~qai.channels.application.ports.InboundTransportPort`
so the dispatch bridge in :mod:`apps.api._channel_dispatch_bridge`
consumes messages with one shape regardless of provider.  The
watchdog
:class:`~qai.channels.infrastructure.transport_watchdog.TransportWatchdog`
supervises this transport for reconnection on transient drops.

Optional dependency
-------------------
``lark_oapi`` is **not** in the runtime dependency manifest (it is
provider-locked, optional, large); the import is guarded so this
module loads cleanly in CI / dev environments where the SDK is
missing.  When the SDK is unavailable :meth:`start` raises
:class:`~qai.platform.errors.ExternalServiceError` with a clear
operator-facing message.
"""

from __future__ import annotations
import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from qai.platform.errors import ExternalServiceError
from qai.platform.logging import get_logger

from qai.channels.application.ports import InboundTransportPort
from qai.channels.domain import (
    ChannelInstance,
    ChannelInstanceId,
    ChannelKind,
    ChannelMessage,
    ChannelMessageId,
    ChannelUserId,
    MessageContent,
)
from qai.channels.infrastructure.payload_parsers import (
    extract_feishu_image_attachment,
)

logger = get_logger(__name__)

__all__ = ["FeishuWebSocketTransport"]

#: Matches Feishu's literal ``@_all`` broadcast token in ``content.text``.
#: Feishu does NOT populate ``message.mentions`` for an @所有人 broadcast — it
#: embeds the literal ``@_all`` in the text body with ``mentions=null``.  The
#: surrounding ``(?<!\w) / (?!\w)`` guards keep us from matching ``@_all`` when
#: it is glued inside a larger token (e.g. an email or a path), so a user who
#: merely *types* the substring is not mistaken for a real broadcast mention.
_AT_ALL_TEXT_RE = re.compile(r"(?<!\w)@_all(?!\w)")

_RECONNECT_BACKOFF: tuple[float, ...] = (5.0, 10.0, 30.0, 60.0, 120.0)

#: Bounded inbound-dedup set sizing (4-M14 / §3.7 bounded-prune).  V1
#: ``backend/channels/feishu/channel.py:799-813`` kept a set of processed
#: message ids and, once it grew past 1000, dropped the oldest 500.
_DEDUP_MAX = 1000
_DEDUP_PRUNE = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dedup_check_and_add(seen: dict[str, None], message_id: str) -> bool:
    """Return ``True`` when ``message_id`` was already seen (a duplicate).

    Uses an insertion-ordered ``dict`` as a bounded LRU-ish set: a new id is
    inserted and, when the map exceeds :data:`_DEDUP_MAX`, the oldest
    :data:`_DEDUP_PRUNE` entries are evicted (V1 bounded-prune parity).
    Empty / falsy ids are never deduplicated (always treated as fresh) so a
    provider that omits a stable id does not collapse distinct messages.
    """
    if not message_id:
        return False
    if message_id in seen:
        return True
    seen[message_id] = None
    if len(seen) > _DEDUP_MAX:
        for old in list(seen)[:_DEDUP_PRUNE]:
            seen.pop(old, None)
    return False


class FeishuWebSocketTransport(InboundTransportPort):
    """Inbound transport over the lark_oapi WebSocket client.

    Per-instance state — no module-level globals.  The WebSocket
    client / SDK factory is **injected** so tests can substitute
    a stub without monkey-patching the ``lark_oapi`` import.

    Inbound events are buffered on an :class:`asyncio.Queue` so
    :meth:`stream` is a clean async iterator.  The SDK callback
    (executed in the SDK's own thread/event-loop) converts each
    Lark message envelope into a :class:`ChannelMessage` and pushes
    it onto the queue via :func:`asyncio.run_coroutine_threadsafe`.
    """

    KIND = ChannelKind.FEISHU

    __slots__ = (
        "_client_factory",
        "_id_factory",
        "_clock",
        "_ssl_verify",
        "_ssl_verify_provider",
        "_client",
        "_client_task",
        "_queue",
        "_loop",
        "_started",
        "_reconnect_attempts",
        "_network_policy_resolver",
        "_network_policy_active",
        "_seen_message_ids",
        "_bot_identity",
        # Remembered across the connection lifetime so the watchdog's
        # no-arg ``restart()`` can rebuild the SDK client without the
        # caller re-supplying them.  ``_credentials`` is the bare
        # app_secret (§3.3 sensitive value): kept in-memory only,
        # never logged / persisted.
        "_instance",
        "_credentials",
    )

    def __init__(
        self,
        *,
        client_factory: Any | None = None,
        id_factory: Any | None = None,
        clock: Any | None = None,
        queue_max_size: int = 256,
        network_policy_resolver: Any | None = None,
        ssl_verify: bool = True,
        ssl_verify_provider: "Callable[[], bool] | None" = None,
    ) -> None:
        """Construct the transport.

        Args:
            client_factory: Callable returning a lark_oapi WebSocket
                client bound to ``(app_id, app_secret)`` derived
                from credentials.  When ``None`` we default to
                importing ``lark_oapi`` at :meth:`start` time.
            id_factory: Optional ``ids`` provider (matches the
                wechat_longpoll signature).
            clock: Optional clock returning ``datetime``.
            queue_max_size: Bounded buffer for inbound messages.
            network_policy_resolver: Optional callable
                ``(instance) -> SdkNetworkPolicy | None`` applying proxy
                + per-domain TLS-skip to the lark_oapi SDK's internal
                requests / websockets traffic for the connection lifetime
                (V1 parity:
                ``backend/channels/feishu/channel.py:599-644``).  When
                ``None`` no network policy is applied.
            ssl_verify: Verify TLS certificates for the best-effort
                ``/bot/v3/info`` bot-identity lookup. Threaded from the
                unified ``Settings.ssl_verify`` switch (edition-derived
                default); False relaxes TLS for enterprise MITM gateways.
        """
        self._client_factory = client_factory
        self._id_factory = id_factory
        self._clock = clock
        self._ssl_verify = ssl_verify
        # Live Settings.ssl_verify provider (apps/api._global_proxy
        # .build_ssl_verify_provider). Read at bot-identity lookup time (per
        # connection) so a runtime SSL toggle hot-applies; frozen bool fallback.
        self._ssl_verify_provider = ssl_verify_provider
        self._client: Any = None
        self._client_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[ChannelMessage] = asyncio.Queue(
            maxsize=queue_max_size
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started: set[str] = set()
        self._reconnect_attempts: int = 0
        self._network_policy_resolver = network_policy_resolver
        self._network_policy_active: bool = False
        # 4-M14 — bounded inbound message-id dedup (insertion-ordered).
        self._seen_message_ids: dict[str, None] = {}
        # Dynamic bot identity resolved from the bound app_id/app_secret, like
        # MyAgent's LarkChannel.botIdentity.  This avoids requiring operators to
        # hard-code bot_open_id and keeps @ detection correct when a different
        # Feishu bot is bound later.
        self._bot_identity: dict[str, str] | None = None
        # Remembered at start() so restart() can rebuild the client.
        self._instance: ChannelInstance | None = None
        self._credentials: str | None = None

    # ------------------------------------------------------------------
    # InboundTransportPort
    # ------------------------------------------------------------------
    async def start(
        self, instance: ChannelInstance, credentials: str
    ) -> None:
        if instance.kind is not self.KIND:
            raise AssertionError(
                f"transport bound to {self.KIND!r} got {instance.kind!r}"
            )
        if not credentials:
            raise ExternalServiceError(
                "channels.transport.missing_credentials",
                "feishu websocket transport requires credentials",
                service=self.KIND.value,
            )
        self._loop = asyncio.get_running_loop()
        # Apply proxy + per-domain TLS-skip for the lark_oapi SDK's
        # internal requests / websockets traffic before building the
        # client, kept active for the connection lifetime (V1 parity:
        # ``backend/channels/feishu/channel.py:599-644``).
        self._apply_network_policy(instance)
        # Remember the connection context so the watchdog's no-arg
        # ``restart()`` can rebuild the SDK client (§3.3: credentials
        # are an in-memory-only sensitive value, never logged).
        self._instance = instance
        self._credentials = credentials
        self._started.add(instance.instance_id.value)
        self._reconnect_attempts = 0
        try:
            await self._open_client(instance, credentials)
        except Exception:
            self._release_network_policy()
            self._started.discard(instance.instance_id.value)
            # Start failed — forget the context so a stray watchdog
            # restart() cannot resurrect a never-started instance.
            self._instance = None
            self._credentials = None
            raise
        logger.info(
            "channels.feishu.websocket.started",
            instance_id=instance.instance_id.value,
        )

    async def _open_client(
        self, instance: ChannelInstance, credentials: str
    ) -> None:
        """Build the SDK client and spawn its run loop.

        Shared by :meth:`start` and :meth:`restart` so a reconnect
        rebuilds the client through exactly the same path (judgement
        criterion 1: no duplicated wiring).  Does **not** touch
        ``self._started`` / ``self._queue`` so an in-flight
        :meth:`stream` consumer survives a reconnect untouched — the
        rebuilt client's callback feeds the same queue.
        """
        self._client = await self._build_client(instance, credentials)
        # Spawn the client run loop so it does not block the caller.
        start_fn = getattr(self._client, "start", None) or getattr(
            self._client, "run", None
        )
        if start_fn is not None:
            self._client_task = asyncio.create_task(
                self._run_client(start_fn)
            )

    def _apply_network_policy(self, instance: ChannelInstance) -> None:
        """Install the SDK network policy for this instance, if resolved."""
        if self._network_policy_active:
            return
        resolver = self._network_policy_resolver
        if resolver is None:
            return
        try:
            policy = resolver(instance)
        except Exception as exc:  # noqa: BLE001 — policy is best-effort
            logger.warning(
                "channels.feishu.websocket.network_policy_failed",
                instance_id=instance.instance_id.value,
                error=str(exc),
            )
            return
        if policy is None:
            return
        from qai.channels.infrastructure.sdk_network import (
            install_sdk_network_policy,
        )

        install_sdk_network_policy(policy)
        self._network_policy_active = True

    def _release_network_policy(self) -> None:
        if not self._network_policy_active:
            return
        self._network_policy_active = False
        from qai.channels.infrastructure.sdk_network import (
            uninstall_sdk_network_policy,
        )

        uninstall_sdk_network_policy()

    async def stop(self, instance: ChannelInstance) -> None:
        if instance.kind is not self.KIND:
            raise AssertionError(
                f"transport bound to {self.KIND!r} got {instance.kind!r}"
            )
        self._started.discard(instance.instance_id.value)
        # Release the SDK network policy installed at start().
        self._release_network_policy()
        # Forget the connection context — a subsequent restart() must
        # not resurrect a stopped instance.
        self._instance = None
        self._credentials = None
        await self._teardown_client(instance_id=instance.instance_id.value)

    async def _teardown_client(self, *, instance_id: str | None) -> None:
        """Tear down the current SDK client + run-loop task.

        Shared by :meth:`stop` (full shutdown) and :meth:`restart`
        (reconnect).  Leaves ``self._started`` / ``self._queue`` /
        remembered context untouched so callers decide their fate.
        """
        client = self._client
        self._client = None
        if client is not None:
            try:
                stop_fn = getattr(client, "stop", None) or getattr(
                    client, "close", None
                )
                if stop_fn is not None:
                    result = stop_fn()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.feishu.websocket.stop_failed",
                    instance_id=instance_id,
                    error=str(exc),
                )
        if self._client_task is not None:
            self._client_task.cancel()
            try:
                await self._client_task
            except asyncio.CancelledError:
                # We cancelled this task ourselves; benign.
                pass
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                logger.warning(
                    "channels.feishu.websocket.client_task_cleanup_failed",
                    error=str(exc),
                    exc_info=True,
                )
            self._client_task = None

    async def stream(
        self, instance: ChannelInstance
    ) -> AsyncIterator[ChannelMessage]:  # type: ignore[override]
        while instance.instance_id.value in self._started:
            try:
                msg = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            yield msg

    def is_alive(self) -> bool:
        client = self._client
        if client is None:
            return False
        for attr in ("is_connected", "is_running", "is_alive"):
            fn = getattr(client, attr, None)
            if callable(fn):
                try:
                    return bool(fn())
                except Exception:  # noqa: BLE001
                    return False
        return self._client_task is not None and not self._client_task.done()

    async def restart(self) -> None:
        """Tear down the dead client and rebuild it (true reconnect).

        Called by :class:`TransportWatchdog` when :meth:`is_alive`
        reports the connection dropped.  Unlike a bare ``self._client
        = None`` (which left the watchdog spinning forever — it would
        keep seeing ``is_alive() == False`` and re-``restart()`` without
        ever reconnecting), this rebuilds the SDK client from the
        ``instance`` + ``credentials`` remembered at :meth:`start`, so
        ``is_alive()`` flips back to ``True`` and the watchdog's failure
        counter resets.  ``self._started`` / ``self._queue`` are left
        intact so the in-flight :meth:`stream` consumer continues
        without interruption.
        """
        self._reconnect_attempts += 1
        attempt = self._reconnect_attempts
        delay_ix = min(attempt - 1, len(_RECONNECT_BACKOFF) - 1)
        instance = self._instance
        credentials = self._credentials
        if instance is None or credentials is None:
            # Never started (or already stopped) — nothing to reconnect.
            logger.warning(
                "channels.feishu.websocket.restart_no_context",
                attempt=attempt,
            )
            self._client = None
            return
        logger.warning(
            "channels.feishu.websocket.restart",
            attempt=attempt,
            backoff_seconds=_RECONNECT_BACKOFF[delay_ix],
        )
        # Drop the dead client + run-loop task, then rebuild. Keep the
        # network policy installed (it is connection-lifetime scoped).
        await self._teardown_client(
            instance_id=instance.instance_id.value
        )
        await self._open_client(instance, credentials)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _build_client(
        self, instance: ChannelInstance, credentials: str
    ) -> Any:
        """Construct the SDK client or raise a clear operator error.

        The lark_oapi import + ``ws.Client`` construction is synchronous
        and slow (the SDK pulls in many submodules on first import, and the
        client constructor can perform synchronous network init when going
        through an enterprise proxy). Running it inline on the event loop
        blocked the main loop for ~90s at startup. V1/v0.5 offload this
        whole synchronous block to a thread pool
        (``backend/channels/feishu/channel.py`` — ``_do_start_sync`` via
        ``loop.run_in_executor(None, _do_start_sync)``), so we do the same
        here: keep the loop free while the SDK warms up.
        """
        if self._client_factory is not None:
            client = self._client_factory(
                instance=instance, credentials=credentials
            )
            if asyncio.iscoroutine(client):
                client = await client
            self._wire_callbacks(client, instance)
            return client
        loop = self._loop or asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._build_client_sync, instance, credentials
        )

    def _build_client_sync(
        self, instance: ChannelInstance, credentials: str
    ) -> Any:
        """Synchronous SDK import + client construction.

        Runs in a worker thread (see :meth:`_build_client`) so the slow
        ``import lark_oapi`` and ``ws.Client(...)`` construction never block
        the asyncio event loop. Aligned with V1/v0.5 ``_do_start_sync``.
        """
        try:
            import lark_oapi  # type: ignore[import-not-found]
        except ImportError as exc:
            logger.warning(
                "channels.feishu.websocket.sdk_missing",
                instance_id=instance.instance_id.value,
                detail=str(exc),
            )
            raise ExternalServiceError(
                "channels.transport.sdk_missing",
                "lark_oapi SDK not installed; cannot start WebSocket "
                "transport for Feishu / Lark. Install lark_oapi or "
                "use the webhook-only outbound transport.",
                service=self.KIND.value,
                cause=exc,
            ) from exc
        # Single-namespace credentials (aligned with the outbound
        # ``FeishuTenantTokenCache``): ``credentials`` is the BARE
        # ``app_secret`` resolved from
        # ``(FEISHU_APP_SECRET_SERVICE, instance_id)`` and ``app_id`` is
        # read from the persisted ``ChannelSettings.kind_specific``
        # (the same slot the ``POST /api/feishu/config`` writer fills).
        # This replaces the old "app_id:app_secret" packed string, which
        # diverged from the outbound read path and broke real sends.
        app_secret = credentials
        app_id = ""
        for k, v in instance.get_settings().kind_specific:
            if k == "app_id":
                app_id = v
                break
        # Back-compat: instances registered before the single-namespace
        # migration carry a legacy ``credentials_ref`` that resolves to the
        # packed ``"app_id:app_secret"`` string (the outbound path now reads
        # the bare secret from a different namespace). If we received such a
        # packed value, strip the ``"<app_id>:"`` prefix so we hand lark the
        # *real* app_secret — otherwise lark rejects it with
        # ``1000040345: app_id or app_secret is invalid``. Only unpack when
        # the prefix matches the resolved app_id (so a secret that happens to
        # contain ':' is left intact).
        if app_id and app_secret.startswith(f"{app_id}:"):
            app_secret = app_secret[len(app_id) + 1 :]
        elif not app_id and ":" in app_secret:
            # No app_id from settings but a packed credential — split it
            # (legacy single-source path, mirrors the old behaviour).
            split_id, _, split_secret = app_secret.partition(":")
            app_id, app_secret = split_id, split_secret
        if not app_id or not app_secret:
            raise ExternalServiceError(
                "channels.transport.bad_credentials",
                "feishu requires a configured app_id "
                "(ChannelSettings.kind_specific['app_id']) and an "
                "app_secret (SecretStore); configure both before "
                "starting the channel",
                service=self.KIND.value,
            )
        self._bot_identity = _resolve_bot_identity_sync(
            app_id,
            app_secret,
            # Live read so a runtime Settings.ssl_verify toggle hot-applies to
            # this per-connection identity/REST lookup; frozen bool fallback.
            ssl_verify=(
                self._ssl_verify_provider()
                if self._ssl_verify_provider is not None
                else self._ssl_verify
            ),
        )
        if self._bot_identity:
            logger.info(
                "channels.feishu.websocket.bot_identity_resolved",
                instance_id=instance.instance_id.value,
                bot_app_id=self._bot_identity.get("app_id") or "(unknown)",
                bot_open_app_id=self._bot_identity.get("open_app_id") or "(unknown)",
                bot_open_id=self._bot_identity.get("open_id") or "(unknown)",
                bot_name=self._bot_identity.get("name") or "(unknown)",
            )
        else:
            logger.warning(
                "channels.feishu.websocket.bot_identity_unavailable",
                instance_id=instance.instance_id.value,
                reason="bot/v3/info failed; falling back to configured app_id/open_id/name matching",
            )

        # Build the lark event handler and register the inbound message
        # callback through it.  This is the ONLY supported way to receive
        # messages on a lark ``ws.Client`` — the client itself exposes no
        # ``on_message`` / ``register_*`` instance method (verified: its
        # only public method is ``start``).  V1 parity:
        # ``backend/channels/feishu/channel.py:469-484`` builds an
        # ``EventDispatcherHandler.builder(...).register_p2_im_message_receive_v1``
        # and passes it as ``ws.Client(app_id, app_secret,
        # event_handler=...)``.  The prior V2 code constructed the client
        # WITHOUT an event_handler and then tried to find non-existent
        # ``on_message`` methods — so it silently registered nothing and
        # never received any message (the "飞书连接成功但发消息无反应" bug).
        event_handler = self._build_event_handler(lark_oapi, instance)
        ws_module = getattr(lark_oapi, "ws", None)
        ws_cls = (
            getattr(lark_oapi, "WebSocketClient", None)
            or (getattr(ws_module, "Client", None) if ws_module is not None else None)
            or getattr(lark_oapi, "Client", None)
        )
        if ws_cls is None:
            raise ExternalServiceError(
                "channels.transport.sdk_incompatible",
                "lark_oapi loaded but exposes no WebSocketClient",
                service=self.KIND.value,
            )
        try:
            client = ws_cls(
                app_id=app_id,
                app_secret=app_secret,
                event_handler=event_handler,
            )
        except TypeError:
            # Older SDK shape: positional constructor (V1 used
            # ``lark.ws.Client(app_id, app_secret, event_handler=...)``).
            client = ws_cls(app_id, app_secret, event_handler=event_handler)
        return client

    def _build_event_handler(
        self, lark_oapi: Any, instance: ChannelInstance
    ) -> Any:
        """Build a lark ``EventDispatcherHandler`` wired to our queue.

        Registers ``im.message.receive_v1`` (the inbound text/image
        message event) to a callback that converts the lark event into a
        :class:`ChannelMessage` and threadsafely enqueues it.  Mirrors V1
        ``backend/channels/feishu/channel.py:469-478``.

        4-M13 — the ``encrypt_key`` / ``verification_token`` are read from
        the persisted :class:`ChannelSettings.kind_specific` (the same slot
        ``POST /api/feishu/config`` fills with ``app_id``) and passed into
        ``EventDispatcherHandler.builder(encrypt_key, verification_token)``
        so encrypted-event-mode connections decode events correctly (V1
        truth: ``backend/channels/feishu/channel.py:471`` passes
        ``_encrypt_key`` / ``_verification_token``).  Both default to the
        empty string when unset — the long-connection (WebSocket) mode then
        authenticates purely via app_id/app_secret, matching V1's default
        when those config values are blank.  The values are config (not
        long-term credentials) and are NEVER logged.
        """
        loop = self._loop
        instance_for_cb = instance

        def _on_message_receive(data: Any) -> None:
            if loop is None or loop.is_closed():
                return
            logger.info(
                "channels.feishu.websocket.message_received",
                instance_id=instance_for_cb.instance_id.value,
                data_type=type(data).__name__,
            )
            if _should_ignore_group_message_without_bot_mention(
                data, instance_for_cb, self._bot_identity
            ):
                return
            try:
                channel_msg = self._raw_to_channel_message(
                    data, instance_for_cb
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.feishu.websocket.bad_event",
                    instance_id=instance_for_cb.instance_id.value,
                    error=str(exc),
                    exc_info=True,
                )
                return
            logger.info(
                "channels.feishu.websocket.message_parsed",
                instance_id=instance_for_cb.instance_id.value,
                message_id=channel_msg.provider_event_id,
                sender=channel_msg.sender.value,
                text_preview=channel_msg.content.text[:80] if channel_msg.content.text else "",
            )
            # 4-M14 — drop a re-delivered message before it hits the queue
            # (V1 ``_is_duplicate``).  Feishu re-sends events on ack timeout.
            if _dedup_check_and_add(
                self._seen_message_ids, channel_msg.provider_event_id
            ):
                logger.debug(
                    "channels.feishu.websocket.duplicate_dropped",
                    instance_id=instance_for_cb.instance_id.value,
                    message_id=channel_msg.provider_event_id,
                )
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    self._queue.put(channel_msg), loop
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.feishu.websocket.enqueue_failed",
                    instance_id=instance_for_cb.instance_id.value,
                    error=str(exc),
                )

        def _on_message_read(_data: Any) -> None:
            # Silently ignore read receipts (avoids lark "processor not
            # found" ERROR logs).  V1 parity: channel.py:654-659.
            return None

        def _on_bot_p2p_chat_entered(data: Any) -> None:
            # Feishu sends this access event when a user opens / re-enters the
            # bot's one-on-one chat.  It is NOT a user message and should never
            # enter the chat pipeline.  Without an explicit processor the lark
            # SDK logs a scary ERROR ("processor not found") for every such
            # event; on some SDK builds repeated unhandled events can also
            # disturb the WS receive loop.  Register and ignore it, but keep an
            # INFO breadcrumb so operators can still see that Feishu delivered
            # this side-channel event.
            logger.info(
                "channels.feishu.websocket.bot_p2p_chat_entered",
                instance_id=instance_for_cb.instance_id.value,
                data_type=type(data).__name__,
            )
            return None

        # 4-M13 — resolve encrypt_key / verification_token from the
        # instance's kind_specific config (NEVER logged).
        encrypt_key = ""
        verification_token = ""
        for k, v in instance.get_settings().kind_specific:
            if k == "encrypt_key":
                encrypt_key = v or ""
            elif k == "verification_token":
                verification_token = v or ""

        builder = lark_oapi.EventDispatcherHandler.builder(
            encrypt_key, verification_token
        ).register_p2_im_message_receive_v1(_on_message_receive)
        if hasattr(builder, "register_p2_im_message_message_read_v1"):
            builder = builder.register_p2_im_message_message_read_v1(
                _on_message_read
            )
        if hasattr(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
        ):
            builder = builder.register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(
                _on_bot_p2p_chat_entered
            )
        return builder.build()

    async def _run_client(self, start_fn: Any) -> None:
        """Run the SDK's blocking start loop without blocking us.

        The lark_oapi WebSocket ``Client.start()`` is a *blocking* call
        that drives an asyncio event loop internally. Crucially, the SDK
        captures a loop reference **at import time** via
        ``asyncio.get_event_loop()`` into the module-level
        ``lark_oapi.ws.client.loop`` — which is uvicorn's main loop. When
        ``start()`` later calls ``loop.run_until_complete(...)`` on that
        captured main loop (even from a worker thread), the main loop is
        already running → ``RuntimeError: This event loop is already
        running`` and the WS never connects.

        V1 solved this (backend/channels/feishu/channel.py:565-649) by
        running ``ws_client.start()`` on a dedicated thread that:
          1. creates a brand-new event loop,
          2. overrides the SDK's module-level ``loop`` with it, and
          3. rebuilds ``client._lock`` (the SDK's ``asyncio.Lock`` was
             bound to the old loop).
        We mirror that exactly inside ``run_in_executor`` so lark drives a
        fresh loop owned by the worker thread; our main loop stays free.
        Inbound events marshal back via ``asyncio.run_coroutine_threadsafe``
        (see ``_build_event_handler``), which is thread-safe by design.
        """
        client = self._client

        def _thread_main() -> None:
            # (1) brand-new loop owned by this worker thread.
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            # (2) override the SDK's import-time module-level loop (V1
            #     channel.py:589) so start() doesn't drive uvicorn's loop.
            try:
                import lark_oapi.ws.client as _lark_ws_client  # type: ignore

                _lark_ws_client.loop = new_loop
            except Exception:  # noqa: BLE001 — best-effort; some SDK builds differ
                pass
            # (3) rebuild the SDK's asyncio.Lock on the new loop (V1
            #     channel.py:593); the constructor bound it to the old loop.
            try:
                if hasattr(client, "_lock"):
                    client._lock = asyncio.Lock()
            except Exception:  # noqa: BLE001
                pass
            try:
                start_fn()
            finally:
                try:
                    new_loop.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            if asyncio.iscoroutinefunction(start_fn):
                # Native-async SDK shape: await directly on our loop.
                await start_fn()
                return
            # Blocking ``start()`` (self-driven loop) → dedicated daemon
            # thread with the V1 loop-override applied.
            #
            # Do NOT use ``loop.run_in_executor`` here: ThreadPoolExecutor
            # worker threads are non-daemon.  During ``POST /api/system/reboot``
            # the lark SDK's blocking ``start()`` can outlive uvicorn shutdown;
            # the server stops listening, but the Python process remains alive
            # only because that non-daemon executor thread is still blocked in
            # the SDK WS loop.  The supervisor waits forever for the child
            # process to exit and therefore never respawns.  V1/Zagent starts
            # lark ``ws_client.start()`` in a daemon thread; mirror that so a
            # graceful reboot can complete even if the SDK does not return.
            import threading

            thread = threading.Thread(
                target=_thread_main,
                name="qai-feishu-ws-client",
                daemon=True,
            )
            thread.start()
            while thread.is_alive():
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.feishu.websocket.run_failed",
                error=str(exc),
            )

    def _wire_callbacks(self, client: Any, instance: ChannelInstance) -> None:
        """Register a message callback that pushes onto our queue."""
        loop = self._loop

        def _callback(raw: Any) -> None:
            if loop is None or loop.is_closed():
                return
            if _should_ignore_group_message_without_bot_mention(raw, instance, self._bot_identity):
                return
            try:
                channel_msg = self._raw_to_channel_message(raw, instance)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.feishu.websocket.bad_event",
                    instance_id=instance.instance_id.value,
                    error=str(exc),
                )
                return
            # 4-M14 — bounded inbound dedup (V1 ``_is_duplicate``).
            if _dedup_check_and_add(
                self._seen_message_ids, channel_msg.provider_event_id
            ):
                logger.debug(
                    "channels.feishu.websocket.duplicate_dropped",
                    instance_id=instance.instance_id.value,
                    message_id=channel_msg.provider_event_id,
                )
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    self._queue.put(channel_msg), loop
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.feishu.websocket.enqueue_failed",
                    instance_id=instance.instance_id.value,
                    error=str(exc),
                )

        for attr in (
            "on_message",
            "register_p2p_chat",
            "register_message",
            "set_message_handler",
        ):
            register = getattr(client, attr, None)
            if callable(register):
                try:
                    register(_callback)
                except TypeError:
                    register("message")(_callback)
                return

    def _raw_to_channel_message(
        self, raw: Any, instance: ChannelInstance
    ) -> ChannelMessage:
        """Convert a Lark P2P chat envelope to a :class:`ChannelMessage`.

        Lark events nest the actual message under
        ``event.message.content`` (a JSON-encoded string for text
        messages); we duck-type both dict and SDK-object access.
        """
        # Try the canonical Lark shape first
        event = _get(raw, "event") or raw
        message = _get(event, "message") or event
        sender = _get(event, "sender") or _get(message, "sender") or {}

        chat_id = _get(message, "chat_id") or ""
        chat_type = _get(message, "chat_type") or "p2p"
        if not isinstance(chat_id, str):
            chat_id = str(chat_id)
        if not isinstance(chat_type, str):
            chat_type = str(chat_type)
        is_group = _is_group_chat_type(chat_type)

        content_raw = _get(message, "content")
        # The Lark message id (``om_...``) is the handle the image-download
        # REST API keys on, so resolve it BEFORE the synthesized fallback id
        # below and feed it to the shared image extractor.
        lark_message_id = _get(message, "message_id")
        message_type = _get(message, "message_type") or ""
        if not isinstance(message_type, str):
            message_type = str(message_type)
        # Shared with the webhook parser so image-handling never drifts
        # between the two inbound paths (V1 parity:
        # ``backend/channels/feishu/channel.py:815-825`` special-cased
        # ``msg_type == "image"``; text + image are the only inbound media
        # types V1's WS path handled).  The decoder downstream
        # (qai.channels.adapters.feishu_image_decoder) fetches the bytes.
        extracted_text, attachments = extract_feishu_image_attachment(
            content_raw=content_raw,
            message_id=str(lark_message_id or ""),
            message_type=message_type,
        )
        # Plain-text / rich-post bodies (and any non-image type) keep the
        # existing text-flattening behaviour; only image messages add an
        # attachment + ``[image]`` placeholder caption.  Feishu mention
        # placeholders are removed precisely via ``message.mentions[].key``
        # so legitimate ``@`` body content (e-mails, decorators) survives.
        text = (
            extracted_text
            if attachments
            else _extract_text(content_raw, _mention_keys(raw))
        )
        sender_id = (
            _get(sender, "sender_id", "open_id")
            or _get(sender, "open_id")
            or _get(sender, "user_id")
            or "unknown"
        )
        if not isinstance(sender_id, str):
            sender_id = str(sender_id)
        provider_event_id = (
            lark_message_id
            or _get(event, "event_id")
            or f"feishu-{sender_id}-{int(_utcnow().timestamp() * 1000)}"
        )

        clock = self._clock
        now = (
            clock.now()
            if clock is not None and hasattr(clock, "now")
            else _utcnow()
        )

        ids = self._id_factory
        if ids is not None and hasattr(ids, "new_id"):
            mid = ChannelMessageId(ids.new_id())
        else:
            import uuid

            mid = ChannelMessageId(uuid.uuid4().hex)

        return ChannelMessage.receive(
            message_id=mid,
            instance_id=ChannelInstanceId(instance.instance_id.value),
            kind=self.KIND,
            sender=ChannelUserId(sender_id or "unknown"),
            provider_event_id=str(provider_event_id),
            content=MessageContent(text=text, attachments=attachments),
            arrived_at=now,
            group_id=chat_id if is_group and chat_id else None,
        )


def _is_group_chat_type(chat_type: Any) -> bool:
    """Return True for any Feishu *multi-party* chat type.

    Feishu uses ``"group"`` for regular group chats and ``"topic"`` for
    topic / thread group chats.  Both are multi-party and must follow the
    group policy (reply back to the chat, require an explicit @-mention).
    Treating only ``"group"`` as a group silently regressed topic groups:
    the bot would reply to the individual sender and respond to un-mentioned
    chatter.  ``p2p`` (private) is the only non-group type.
    """
    return str(chat_type) in ("group", "topic")


def _get(obj: Any, *path: str) -> Any:
    cur = obj
    for name in path:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(name)
        else:
            cur = getattr(cur, name, None)
    return cur


def _kind_specific_map(instance: ChannelInstance) -> dict[str, str]:
    try:
        return {k: v for k, v in instance.get_settings().kind_specific}
    except Exception:  # noqa: BLE001 — metadata is best-effort
        return {}


def _resolve_bot_identity_sync(
    app_id: str, app_secret: str, *, ssl_verify: bool = True
) -> dict[str, str] | None:
    """Resolve the bound Feishu bot identity from app credentials.

    Group @ detection must be bound to the *current app*, not to a mutable
    display name and not to the broad ``isBot`` SDK hint.  Resolve the runtime
    identity once from ``app_id/app_secret`` and keep both app identifiers and
    bot open_id/name for ordered matching:

    1. mention ``app_id`` / ``open_app_id`` == current app id  (strongest)
    2. mention ``open_id`` == current bot open_id              (compat)
    3. mention ``name`` == current bot name                    (last fallback)

    The configured ``app_id`` itself is always returned as the authoritative
    app id.  ``/bot/v3/info`` is best-effort and augments it with open_app_id /
    open_id / display name when Feishu API is reachable.  No credentials are
    logged.
    """
    if not app_id or not app_secret:
        return None
    identity: dict[str, str] = {
        "app_id": app_id.strip(),
        "open_app_id": app_id.strip(),
        "open_id": "",
        "name": "",
    }
    try:
        # Match Zagent's runtime identity resolution style: use an HTTP client
        # that honours the already-installed Feishu SDK network policy (proxy +
        # TLS-skip for *.feishu.cn).  The previous urllib.request code bypassed
        # that policy entirely, so in enterprise proxy / MITM environments the
        # /bot/v3/info lookup failed and we fell back to open_id="" — producing
        # botOpenId=(unknown) and making group @openId comparison impossible.
        #
        # TLS verification for this identity lookup follows the unified
        # ``ssl_verify`` switch (threaded from Settings.ssl_verify). When it is
        # False (internal edition / enterprise MITM gateway) the lookup succeeds
        # against a self-signed corporate cert even if the sdk_network
        # ``requests`` patch is not active at call time (e.g. install-ordering
        # with another channel); urllib3's insecure warning is suppressed only
        # in that case to avoid log noise for the intentional skip.
        import requests
        import urllib3

        if not ssl_verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        token_resp = requests.post(  # noqa: S113,S501 - timeout set; TLS skip follows ssl_verify (enterprise MITM)
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
            verify=ssl_verify,
        )
        token_payload = token_resp.json()
        if int(token_payload.get("code", -1)) != 0:
            logger.warning(
                "channels.feishu.websocket.bot_identity_token_failed",
                code=token_payload.get("code"),
                msg=token_payload.get("msg"),
                http_status=token_resp.status_code,
            )
            return identity
        token = token_payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            logger.warning(
                "channels.feishu.websocket.bot_identity_token_missing",
                http_status=token_resp.status_code,
                payload_keys=sorted(str(k) for k in token_payload.keys()) if isinstance(token_payload, dict) else [],
            )
            return identity

        info_resp = requests.get(  # noqa: S113,S501 - timeout set; TLS skip follows ssl_verify (enterprise MITM)
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            verify=ssl_verify,
        )
        info_payload = info_resp.json()
        if int(info_payload.get("code", -1)) != 0:
            logger.warning(
                "channels.feishu.websocket.bot_identity_info_failed",
                code=info_payload.get("code"),
                msg=info_payload.get("msg"),
                http_status=info_resp.status_code,
                payload_keys=sorted(str(k) for k in info_payload.keys()) if isinstance(info_payload, dict) else [],
            )
            return identity
        # Feishu/Lark deployments and SDK docs have shown multiple shapes:
        #   {"bot": {"open_id": "ou_...", "app_name": "..."}}
        #   {"data": {"open_id": "ou_...", "app_name": "..."}}
        #   {"data": {"bot": {"open_id": "ou_...", "app_name": "..."}}}
        # The previous implementation handled only the first two.  When the
        # third shape arrived, it treated the whole ``data`` dict as the bot and
        # silently left open_id/name empty, causing group @openId matching to
        # fail with current_bot_open_ids=[].
        data_obj = info_payload.get("data")
        bot_obj = info_payload.get("bot")
        if not isinstance(bot_obj, dict) and isinstance(data_obj, dict):
            nested_bot = data_obj.get("bot")
            bot_obj = nested_bot if isinstance(nested_bot, dict) else data_obj
        if not isinstance(bot_obj, dict):
            logger.warning(
                "channels.feishu.websocket.bot_identity_unexpected_payload",
                payload_keys=sorted(str(k) for k in info_payload.keys()),
                data_type=type(data_obj).__name__,
                bot_type=type(info_payload.get("bot")).__name__,
            )
            return identity
        open_app_id = (
            bot_obj.get("open_app_id")
            or bot_obj.get("openAppId")
            or bot_obj.get("app_id")
            or bot_obj.get("appId")
            or app_id
        )
        open_id = bot_obj.get("open_id") or bot_obj.get("openId") or ""
        name = bot_obj.get("app_name") or bot_obj.get("appName") or bot_obj.get("name") or ""
        identity.update(
            {
                "open_app_id": open_app_id.strip() if isinstance(open_app_id, str) else app_id.strip(),
                "open_id": open_id.strip() if isinstance(open_id, str) else "",
                "name": name.strip() if isinstance(name, str) else "",
            }
        )
        logger.info(
            "channels.feishu.websocket.bot_identity_payload_parsed",
            payload_keys=sorted(str(k) for k in info_payload.keys()),
            data_keys=sorted(str(k) for k in data_obj.keys()) if isinstance(data_obj, dict) else [],
            bot_keys=sorted(str(k) for k in bot_obj.keys()),
            has_open_id=bool(identity["open_id"]),
            has_name=bool(identity["name"]),
            open_id=identity["open_id"] or "(empty)",
            name=identity["name"] or "(empty)",
        )
        return identity
    except Exception as exc:  # noqa: BLE001 - best-effort identity lookup
        logger.warning(
            "channels.feishu.websocket.bot_identity_resolve_failed",
            error=str(exc),
        )
    return identity


def _mention_app_ids(mention: Any) -> set[str]:
    """Extract App ID / Open App ID candidates from a Feishu mention object."""
    out: set[str] = set()
    candidates = (
        _get(mention, "id", "app_id"),
        _get(mention, "id", "appId"),
        _get(mention, "id", "open_app_id"),
        _get(mention, "id", "openAppId"),
        _get(mention, "app_id"),
        _get(mention, "appId"),
        _get(mention, "open_app_id"),
        _get(mention, "openAppId"),
    )
    for value in candidates:
        if isinstance(value, str) and value.strip():
            out.add(value.strip())

    # Some SDKs flatten ``id`` to a string and pair it with id_type=open_app_id.
    id_type = str(_get(mention, "idType") or _get(mention, "id_type") or "").casefold().strip()
    direct_id = _get(mention, "id")
    if id_type in {"app", "bot", "app_id", "open_app_id"} and isinstance(direct_id, str) and direct_id.strip():
        out.add(direct_id.strip())
    return out


def _mention_ids(mention: Any) -> set[str]:
    """Extract non-app user/bot open-id style identifiers from a mention."""
    out: set[str] = set()
    candidates = (
        _get(mention, "id", "open_id"),
        _get(mention, "id", "openId"),
        _get(mention, "id", "user_id"),
        _get(mention, "open_id"),
        _get(mention, "openId"),
        _get(mention, "user_open_id"),
        _get(mention, "userOpenId"),
        _get(mention, "user_id"),
        _get(mention, "userId"),
    )
    for value in candidates:
        if isinstance(value, str) and value.strip():
            out.add(value.strip())

    # Direct string id is an open/user id only when the SDK did NOT say it is an
    # app id.  This prevents ``id='cli_x', id_type='open_app_id'`` from being
    # misclassified as an open_id.
    id_type = str(_get(mention, "idType") or _get(mention, "id_type") or "").casefold().strip()
    direct_id = _get(mention, "id")
    if id_type not in {"app", "bot", "app_id", "open_app_id"} and isinstance(direct_id, str) and direct_id.strip():
        out.add(direct_id.strip())
    return out


def _mention_names(mention: Any) -> set[str]:
    out: set[str] = set()
    for key in ("name", "key", "text", "tenant_key"):
        value = _get(mention, key)
        if isinstance(value, str) and value.strip():
            out.add(value.strip().lstrip("@"))
    return out


def _bot_mention_hints(
    instance: ChannelInstance,
    bot_identity: dict[str, str] | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Return ``(app_ids, open_ids, names)`` for recognising this bot.

    App IDs are authoritative and come from the current channel instance's
    configured app_id plus the runtime identity resolved from app_id/app_secret.
    open_id/name are compatibility fallbacks only.
    """
    settings = _kind_specific_map(instance)
    app_ids: set[str] = set()
    open_ids: set[str] = set()
    names: set[str] = set()

    for key in ("app_id", "appId", "open_app_id", "openAppId"):
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            app_ids.add(value.strip())

    if bot_identity is not None:
        for key in ("app_id", "open_app_id"):
            value = bot_identity.get(key)
            if isinstance(value, str) and value.strip():
                app_ids.add(value.strip())
        value = bot_identity.get("open_id")
        if isinstance(value, str) and value.strip():
            open_ids.add(value.strip())
        value = bot_identity.get("name")
        if isinstance(value, str) and value.strip():
            names.add(value.strip().lstrip("@"))

    open_ids.update(
        value.strip()
        for key in ("bot_open_id", "botOpenId", "open_id", "openId")
        if isinstance((value := settings.get(key)), str) and value.strip()
    )
    names.update(
        value.strip().lstrip("@")
        for key in ("bot_name", "botName", "app_name", "appName")
        if isinstance((value := settings.get(key)), str) and value.strip()
    )
    if instance.name:
        names.add(instance.name.strip().lstrip("@"))
    return app_ids, open_ids, names


def _mention_compare_raw_fields(mention: Any) -> dict[str, Any]:
    """Fields used to explain how a Feishu mention was normalised.

    This is intentionally verbose because Feishu SDKs expose mention identity in
    multiple shapes.  In particular, a payload like
    ``{"openId":"ou_..."}`` is extracted by ``_mention_ids`` from the
    top-level ``openId`` field; it will only match if the current bot open_id is
    also known.
    """
    raw = {
        "key": _get(mention, "key"),
        "name": _get(mention, "name"),
        "isBot": bool(_get(mention, "isBot") or _get(mention, "is_bot")),
        "idType": _get(mention, "idType") or _get(mention, "id_type"),
        "id": _get(mention, "id"),
        "id.open_id": _get(mention, "id", "open_id"),
        "id.openId": _get(mention, "id", "openId"),
        "id.app_id": _get(mention, "id", "app_id"),
        "id.open_app_id": _get(mention, "id", "open_app_id"),
        "open_id": _get(mention, "open_id"),
        "openId": _get(mention, "openId"),
        "app_id": _get(mention, "app_id"),
        "open_app_id": _get(mention, "open_app_id"),
        "user_open_id": _get(mention, "user_open_id"),
        "userOpenId": _get(mention, "userOpenId"),
    }
    return {k: v for k, v in raw.items() if v not in (None, "")}


def _log_bot_mention_compare(
    *,
    instance: ChannelInstance,
    mention: Any,
    bot_identity: dict[str, str] | None,
    bot_app_ids: set[str],
    bot_open_ids: set[str],
    bot_names: set[str],
    mention_app_ids: set[str],
    mention_open_ids: set[str],
    mention_names: set[str],
    matched: bool,
    reason: str,
) -> None:
    logger.info(
        "channels.feishu.websocket.bot_mention_compare",
        instance_id=instance.instance_id.value,
        matched=matched,
        reason=reason,
        raw_fields=_mention_compare_raw_fields(mention),
        extracted_mention_app_ids=sorted(mention_app_ids),
        extracted_mention_open_ids=sorted(mention_open_ids),
        extracted_mention_names=sorted(mention_names),
        current_bot_app_ids=sorted(bot_app_ids),
        current_bot_open_ids=sorted(bot_open_ids),
        current_bot_names=sorted(bot_names),
        bot_identity=bot_identity or {},
        analysis=(
            "mention.openId/top-level openId is extracted into "
            "extracted_mention_open_ids; it can only match when "
            "current_bot_open_ids is non-empty and contains the same value. "
            "App-id match requires extracted_mention_app_ids to contain the "
            "current app_id/open_app_id. isBot alone is not accepted."
        ),
    )


def _is_bot_mention(
    mention: Any,
    instance: ChannelInstance,
    bot_identity: dict[str, str] | None = None,
) -> bool:
    """Return True when ``mention`` refers to this bound bot.

    Priority is deliberately strict:
      1. mention app_id/open_app_id matches current app id/open_app_id;
      2. if no app id is present, mention open_id matches current bot open_id;
      3. if no ids are present, display-name fallback;
      4. never accept merely because ``isBot`` is true.
    """
    bot_app_ids, bot_open_ids, bot_names = _bot_mention_hints(instance, bot_identity)
    mention_app_ids = _mention_app_ids(mention)
    mention_open_ids = _mention_ids(mention)
    mention_name_values = _mention_names(mention)
    mention_names_cf = {n.casefold() for n in mention_name_values}

    # 1. App ID / Open App ID strong match. This is the authoritative check.
    if bot_app_ids and mention_app_ids:
        matched = bool(bot_app_ids.intersection(mention_app_ids))
        _log_bot_mention_compare(
            instance=instance,
            mention=mention,
            bot_identity=bot_identity,
            bot_app_ids=bot_app_ids,
            bot_open_ids=bot_open_ids,
            bot_names=bot_names,
            mention_app_ids=mention_app_ids,
            mention_open_ids=mention_open_ids,
            mention_names=mention_name_values,
            matched=matched,
            reason="app_id_match" if matched else "app_id_present_but_not_current_app",
        )
        return matched

    # If the mention explicitly carries an app id but it is not ours, stop here.
    if mention_app_ids:
        _log_bot_mention_compare(
            instance=instance,
            mention=mention,
            bot_identity=bot_identity,
            bot_app_ids=bot_app_ids,
            bot_open_ids=bot_open_ids,
            bot_names=bot_names,
            mention_app_ids=mention_app_ids,
            mention_open_ids=mention_open_ids,
            mention_names=mention_name_values,
            matched=False,
            reason="mention_app_id_present_but_current_app_id_missing_or_not_matched",
        )
        return False

    # 2. Backward-compatible bot open_id match only when no app id was provided.
    if bot_open_ids and mention_open_ids:
        matched = bool(bot_open_ids.intersection(mention_open_ids))
        _log_bot_mention_compare(
            instance=instance,
            mention=mention,
            bot_identity=bot_identity,
            bot_app_ids=bot_app_ids,
            bot_open_ids=bot_open_ids,
            bot_names=bot_names,
            mention_app_ids=mention_app_ids,
            mention_open_ids=mention_open_ids,
            mention_names=mention_name_values,
            matched=matched,
            reason="open_id_match" if matched else "open_id_present_but_not_current_bot_open_id",
        )
        return matched

    # 3. Display-name fallback — fire when this mention could NOT be matched by
    #    an authoritative id. We only reach here when the mention carries no
    #    app_id (a mention app_id that wasn't ours already returned False above),
    #    so the only thing left is the open_id comparison: skip name fallback
    #    only when a usable open_id comparison WAS available (both our bot
    #    open_id and the mention open_id are known) — in that case the open_id
    #    branch above already decided. Otherwise (mention has no open_id, OR our
    #    own bot open_id is unresolved so there is nothing to compare against),
    #    the display name is the only signal and must be honoured, else a real
    #    @bot in a group lacking id metadata would be silently ignored.
    open_id_comparison_was_possible = bool(bot_open_ids and mention_open_ids)
    if (
        not open_id_comparison_was_possible
        and bot_names
        and mention_names_cf.intersection(n.casefold() for n in bot_names)
    ):
        _log_bot_mention_compare(
            instance=instance,
            mention=mention,
            bot_identity=bot_identity,
            bot_app_ids=bot_app_ids,
            bot_open_ids=bot_open_ids,
            bot_names=bot_names,
            mention_app_ids=mention_app_ids,
            mention_open_ids=mention_open_ids,
            mention_names=mention_name_values,
            matched=True,
            reason="name_fallback_no_usable_id_comparison",
        )
        return True

    # No match.  Log the exact reason, including the common case from the user's
    # sample: mention.openId was extracted, but current_bot_open_ids is empty
    # because /bot/v3/info did not provide the bot open_id and no bot_open_id is
    # configured, so there is nothing to compare it against.
    if mention_open_ids and not bot_open_ids:
        reason = "mention_open_id_extracted_but_current_bot_open_id_unknown"
    elif mention_open_ids:
        reason = "mention_open_id_extracted_but_not_equal_to_current_bot_open_id"
    elif mention_name_values:
        reason = "mention_name_not_equal_to_current_bot_name"
    elif bool(_get(mention, "isBot") or _get(mention, "is_bot")):
        reason = "isBot_without_current_app_id_or_open_id_match"
    else:
        reason = "no_comparable_mention_identity"
    _log_bot_mention_compare(
        instance=instance,
        mention=mention,
        bot_identity=bot_identity,
        bot_app_ids=bot_app_ids,
        bot_open_ids=bot_open_ids,
        bot_names=bot_names,
        mention_app_ids=mention_app_ids,
        mention_open_ids=mention_open_ids,
        mention_names=mention_name_values,
        matched=False,
        reason=reason,
    )
    return False


def _is_mention_all(mention: Any) -> bool:
    """Return True when *mention* is a Feishu @all / @所有人 broadcast.

    Feishu @所有人 appears in several forms across SDK versions / clients:
      1. ``mention_all`` / ``mentionAll`` field is truthy
      2. ``mention.key == "@_all"``
      3. ``mention.id.open_id == "ou_all"``  (Feishu fixed placeholder)
      4. ``mention.name`` is "所有人" / "All Members" / "all"
    Note: the message *content* text uses ``@_user_N`` as a placeholder for
    @所有人 (same as ordinary users), so text-based detection is unreliable;
    the mentions-array fields are the authoritative source.
    """
    # Explicit field
    if bool(_get(mention, "mention_all") or _get(mention, "mentionAll")):
        return True
    # key = "@_all"
    key = _get(mention, "key") or ""
    if isinstance(key, str) and key.strip().lstrip("@").lower() == "_all":
        return True
    # open_id = "ou_all" (Feishu official fixed placeholder for @all)
    for id_val in _mention_ids(mention):
        if id_val.lower() in ("ou_all", "_all", "all"):
            return True
    # name match
    for name in _mention_names(mention):
        if name.casefold() in ("_all", "all", "所有人", "all members"):
            return True
    return False


def _event_header_app_id(raw: Any) -> str:
    """Return the Feishu event header app_id, if present.

    For WebSocket events delivered by Feishu, ``header.app_id`` identifies the
    receiving app.  It is not the mention identity itself, but it is useful as a
    tightly-scoped fallback when Feishu's ``bot/v3/info`` failed to give us the
    bot open_id and the mention payload also contains only ``open_id``.
    """
    value = _get(raw, "header", "app_id") or _get(raw, "event", "header", "app_id")
    return value.strip() if isinstance(value, str) else ""


def _group_mentions_bot(
    raw: Any,
    instance: ChannelInstance,
    bot_identity: dict[str, str] | None = None,
) -> bool:
    event = _get(raw, "event") or raw
    message = _get(event, "message") or event
    mentions = _get(message, "mentions") or _get(event, "mentions") or []
    if not isinstance(mentions, (list, tuple)):
        return False
    mentions_list = list(mentions)

    if any(_is_bot_mention(m, instance, bot_identity) for m in mentions_list):
        return True

    # Feishu's mention payload often contains only ``id.open_id`` for a bot
    # mention, while ``/bot/v3/info`` can fail or return a shape without
    # open_id.  In that state strict matching has nothing to compare against:
    #   extracted_mention_open_ids=['ou_...']
    #   current_bot_open_ids=[]
    # However the WS event itself is delivered to exactly one app and carries
    # ``header.app_id``.  If that header matches this channel instance's app_id
    # and the group event contains exactly one non-@all mention, treat that
    # mention as the current bot and learn its open_id/name for subsequent
    # messages.  This mirrors the real Feishu delivery contract while avoiding
    # the unsafe old behaviour of accepting any ``isBot`` mention.
    header_app_id = _event_header_app_id(raw)
    bot_app_ids, bot_open_ids, bot_names = _bot_mention_hints(instance, bot_identity)
    if (
        header_app_id
        and header_app_id in bot_app_ids
        and not bot_open_ids
        and len(mentions_list) == 1
        and not _is_mention_all(mentions_list[0])
    ):
        mention = mentions_list[0]
        mention_open_ids = _mention_ids(mention)
        mention_names = _mention_names(mention)
        learned_open_id = next(iter(sorted(mention_open_ids)), "")
        raw_name = _get(mention, "name")
        learned_name = raw_name.strip() if isinstance(raw_name, str) else ""
        if bot_identity is not None:
            if learned_open_id and not bot_identity.get("open_id"):
                bot_identity["open_id"] = learned_open_id
            if learned_name and not bot_identity.get("name"):
                bot_identity["name"] = learned_name
        logger.info(
            "channels.feishu.websocket.bot_mention_event_header_app_id_fallback",
            instance_id=instance.instance_id.value,
            matched=True,
            reason="event_header_app_id_matches_current_app_single_mention_bot_open_id_unknown",
            event_header_app_id=header_app_id,
            current_bot_app_ids=sorted(bot_app_ids),
            previous_current_bot_open_ids=sorted(bot_open_ids),
            learned_open_id=learned_open_id or "(empty)",
            learned_name=learned_name or "(empty)",
            mention_open_ids=sorted(mention_open_ids),
            mention_names=sorted(mention_names),
            raw_fields=_mention_compare_raw_fields(mention),
            analysis=(
                "bot/v3/info did not provide current bot open_id, but Feishu "
                "delivered this group mention event to header.app_id matching "
                "the configured channel app_id; with a single non-@all mention "
                "we accept it and cache the mention open_id as this bot."
            ),
        )
        return True

    if header_app_id:
        logger.info(
            "channels.feishu.websocket.bot_mention_event_header_app_id_fallback_not_used",
            instance_id=instance.instance_id.value,
            event_header_app_id=header_app_id,
            current_bot_app_ids=sorted(bot_app_ids),
            current_bot_open_ids=sorted(bot_open_ids),
            mention_count=len(mentions_list),
            reason=(
                "header_app_id_missing_or_not_current_app_or_bot_open_id_already_known_or_not_single_mention"
            ),
        )
    return False


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:  # noqa: BLE001 - logging must never break receive path
        return str(value)


def _content_text(message: Any) -> str:
    content_raw = _get(message, "content") or ""
    if isinstance(content_raw, str):
        try:
            decoded = json.loads(content_raw)
            if isinstance(decoded, dict):
                text = decoded.get("text")
                if isinstance(text, str):
                    return text
        except Exception:  # noqa: BLE001
            return content_raw
        return content_raw
    if isinstance(content_raw, dict):
        text = content_raw.get("text")
        if isinstance(text, str):
            return text
    return str(content_raw) if content_raw is not None else ""


def _sender_open_id(event: Any, message: Any) -> str:
    sender = _get(event, "sender") or _get(message, "sender") or {}
    sender_id = (
        _get(sender, "sender_id", "open_id")
        or _get(sender, "open_id")
        or _get(sender, "user_id")
        or ""
    )
    return sender_id if isinstance(sender_id, str) else str(sender_id)


def _mention_log_items(mentions: Iterable[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for m in mentions:
        ids = sorted(_mention_ids(m))
        item = {
            "key": _get(m, "key"),
            "openId": _get(m, "openId") or _get(m, "open_id") or (ids[0] if ids else None),
            "userId": _get(m, "userId") or _get(m, "user_id") or _get(m, "id", "user_id"),
            "name": _get(m, "name"),
            "isBot": bool(_get(m, "isBot") or _get(m, "is_bot")),
        }
        items.append({k: v for k, v in item.items() if v is not None})
    return items


def _mention_compact_items(mentions: Iterable[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for m in mentions:
        ids = sorted(_mention_ids(m))
        items.append(
            {
                "key": _get(m, "key") or "",
                "name": _get(m, "name") or "",
                "openId": _get(m, "openId") or _get(m, "open_id") or (ids[0] if ids else ""),
            }
        )
    return items


def _log_feishu_recv_decision(
    raw: Any,
    instance: ChannelInstance,
    *,
    mentions_bot: bool,
    mention_all: bool,
    accepted: bool,
    bot_identity: dict[str, str] | None = None,
) -> None:
    event = _get(raw, "event") or raw
    message = _get(event, "message") or event
    mentions = _get(message, "mentions") or _get(event, "mentions") or []
    if not isinstance(mentions, (list, tuple)):
        mentions = []
    chat_type = _get(message, "chat_type") or "p2p"
    content = _content_text(message)[:120]
    logger.info(
        "[Feishu/recv] RECV "
        f"id={_get(message, 'message_id') or ''} "
        f"chatType={chat_type} "
        f"senderId={_sender_open_id(event, message)} "
        f"mentionedBot={str(mentions_bot).lower()} "
        f"mentionAll={str(mention_all).lower()} "
        f"mentions={_safe_json(_mention_log_items(mentions))} "
        f"content={_safe_json(content)}"
    )
    if _is_group_chat_type(chat_type):
        bot_app_ids, bot_open_ids, bot_names = _bot_mention_hints(instance, bot_identity)
        logger.info(
            "[Feishu/recv] Group msg "
            f"{'ACCEPTED' if accepted else 'IGNORED_NO_BOT_MENTION'} — "
            f"mentionedBot={str(mentions_bot).lower()} "
            f"mentionAll={str(mention_all).lower()} "
            f"botAppId={next(iter(sorted(bot_app_ids)), '(unknown)')} "
            f"botOpenId={next(iter(sorted(bot_open_ids)), '(unknown)')} "
            f"botName={next(iter(sorted(bot_names)), '(unknown)')} "
            f"mentions={_safe_json(_mention_compact_items(mentions))}"
        )


def _should_ignore_group_message_without_bot_mention(
    raw: Any,
    instance: ChannelInstance,
    bot_identity: dict[str, str] | None = None,
) -> bool:
    """Return True when a Feishu group message did not explicitly @ this bot.

    Policy:
    1. P2P messages are always accepted (return False).
    2. Group messages that explicitly mention **this** bot are accepted.
       The ``mentionedBot`` value emitted in the compatibility log below means
       "this QAI ModelBuilder bot was mentioned", not "some arbitrary bot was
       mentioned".
    3. Group @_all / @所有人 are accepted (bot is included in the broadcast).
       NOTE: Feishu does NOT populate ``mentions`` for @_all -- it embeds
       ``@_all`` literally in ``content.text`` (``mentions=null``).  We must
       check the text field directly.
    4. All other group messages are ignored.
    """
    event = _get(raw, "event") or raw
    message = _get(event, "message") or event
    chat_type = _get(message, "chat_type") or "p2p"
    mentions = _get(message, "mentions") or _get(event, "mentions") or []
    if not isinstance(mentions, (list, tuple)):
        mentions = []
    mention_count = len(mentions)

    # Rule 2: mention of this bot.  Despite the historical variable name,
    # this is intentionally *not* "any bot"; it is "this channel instance's
    # app/bot", resolved by app_id/open_app_id strong match first, then
    # bot_open_id, and only finally display-name fallback.  A bare isBot hint is
    # insufficient because it can mean "some other bot" in the same group.
    mentions_bot = _group_mentions_bot(raw, instance, bot_identity)

    # Rule 3: @_all detection
    # a) via mentions list objects (minority of platforms)
    mentions_all = any(_is_mention_all(m) for m in mentions)
    # b) Real-world observation: Feishu @_all sets mentions=null and embeds
    #    "@_all" literally inside content.text, e.g. {"text": "@_all hello"}.
    #    Match with the word-boundary-guarded regex (_AT_ALL_TEXT_RE) so a bare
    #    "@_all" glued inside a larger token (e.g. "foo@_alliance.example.com")
    #    is NOT treated as a broadcast.
    if not mentions_all:
        _text = _content_text(message)
        if isinstance(_text, str) and _AT_ALL_TEXT_RE.search(_text):
            mentions_all = True

    _log_feishu_recv_decision(
        raw,
        instance,
        mentions_bot=mentions_bot,
        mention_all=mentions_all,
        accepted=(not _is_group_chat_type(chat_type)) or mentions_bot or mentions_all,
        bot_identity=bot_identity,
    )

    if not _is_group_chat_type(chat_type):
        return False

    if mentions_bot or mentions_all:
        reason = "mentionedBot=True" if mentions_bot else "content_at_all(@_all)"
        logger.info(
            "channels.feishu.websocket.group_message_accepted",
            instance_id=instance.instance_id.value,
            chat_id=_get(message, "chat_id") or "",
            mention_count=mention_count,
            reason=reason,
        )
        return False
    logger.info(
        "channels.feishu.websocket.group_message_ignored_no_bot_mention",
        instance_id=instance.instance_id.value,
        chat_id=_get(message, "chat_id") or "",
        mention_count=mention_count,
        mentions_all=mentions_all,
    )
    return True


def _mention_keys(raw: Any) -> set[str]:
    """Collect the placeholder keys (``@_user_1`` / ``@_all``) from a Lark
    event's ``message.mentions[].key`` so they can be removed precisely from
    the body text without touching legitimate ``@`` content."""
    event = _get(raw, "event") or raw
    message = _get(event, "message") or event
    mentions = _get(message, "mentions") or _get(event, "mentions") or []
    if not isinstance(mentions, (list, tuple)):
        return set()
    keys: set[str] = set()
    for m in mentions:
        key = _get(m, "key")
        if isinstance(key, str) and key.strip():
            keys.add(key.strip())
    return keys


def _extract_text(
    content_raw: Any, mention_keys: Iterable[str] | None = None
) -> str:
    """Pull the plain-text body out of a Lark message ``content`` field.

    Lark text messages encode content as a JSON string of the form
    ``'{"text": "hi"}'``; rich messages have a ``post`` structure.
    Falls back to ``str(content_raw)`` for any unexpected shape.

    Feishu ``@mention`` placeholders (``@_all``, ``@_user_N``) are stripped
    from the extracted text so they do not pollute the LLM prompt; when
    ``mention_keys`` (from ``message.mentions[].key``) are supplied they are
    removed exactly.  Legitimate ``@`` content in the body (e-mails, package
    pins, decorators, handles) is preserved — see :func:`_strip_mentions`.
    """
    if content_raw is None:
        return ""
    if isinstance(content_raw, str):
        # Most common: JSON-encoded string
        try:
            import json as _json

            decoded = _json.loads(content_raw)
            if isinstance(decoded, dict):
                text = decoded.get("text")
                if isinstance(text, str):
                    return _strip_mentions(text, mention_keys)
                # Rich post body — flatten paragraphs into newlines
                post = decoded.get("post") or decoded.get("zh_cn")
                if isinstance(post, dict):
                    return _strip_mentions(_flatten_post(post), mention_keys)
        except (ValueError, TypeError):
            return content_raw
        return content_raw
    if isinstance(content_raw, dict):
        text = content_raw.get("text")
        if isinstance(text, str):
            return _strip_mentions(text, mention_keys)
        return _strip_mentions(_flatten_post(content_raw), mention_keys)
    return str(content_raw)


def _strip_mentions(text: str, mention_keys: Iterable[str] | None = None) -> str:
    """Remove Lark ``@mention`` placeholder tokens from a message body.

    Feishu does **not** put literal ``@<name>`` text in ``content.text``;
    it inserts **placeholder keys** of the form ``@_user_1`` / ``@_all``
    (underscore-prefixed), and the real display name lives in
    ``message.mentions[].key`` (the placeholder) + ``.name``.  The previous
    implementation used ``re.sub(r'@\\S+', '', text)`` which also deleted
    legitimate ``@`` content in the body — e-mail addresses
    (``a@b.com``), package pins (``foo@2.0``), decorators (``@property``),
    ``@`` handles, etc.  That is a real, high-frequency data-loss bug.

    This version is precise:

    1. Remove every exact ``mention_keys`` placeholder passed in (these come
       straight from ``message.mentions[].key`` so they are authoritative).
    2. As a fallback for payloads that omit the keys, remove only Feishu's
       *placeholder shape* ``@_<token>`` (underscore-prefixed: ``@_user_3``,
       ``@_all``) — never an arbitrary ``@word``.  Legitimate body ``@``
       content is left untouched because real e-mails/handles/decorators are
       not underscore-prefixed placeholders.
    """
    import re as _re

    if mention_keys:
        # Longest keys first so e.g. ``@_user_12`` is removed before ``@_user_1``.
        for key in sorted(
            {k for k in mention_keys if isinstance(k, str) and k.strip()},
            key=len,
            reverse=True,
        ):
            text = text.replace(key, "")
    # Fallback: only strip Feishu placeholder shape ``@_<token>`` (underscore
    # prefixed); this never matches real e-mails / @handles / decorators.
    text = _re.sub(r"@_\S+", "", text)
    # Collapse the whitespace left where a placeholder used to be.
    text = _re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _flatten_post(post: dict) -> str:
    """Best-effort flatten Lark ``post`` rich content to plain text."""
    chunks: list[str] = []
    body = post.get("content") or post.get("zh_cn") or []
    if isinstance(body, dict):
        body = body.get("content") or []
    if isinstance(body, list):
        for paragraph in body:
            if not isinstance(paragraph, list):
                continue
            for block in paragraph:
                if isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        chunks.append(text)
            chunks.append("\n")
    return "".join(chunks).strip()
