# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Real :class:`QrLoginPort` adapters (PR-047 + PR-097 R-13).

Two implementations — one per :class:`ChannelKind` — each persists
challenges through a shared :class:`SqliteQrLoginChallengeRepository`
so polling routes can survive a process restart.

PR-097 R-13 — personal-WeChat QR-login adapter
----------------------------------------------
The legacy ``backend/channels/wechat/channel.py:498 trigger_login``
flow drove a wechatbot SDK ``Bot`` whose ``on_qr_url`` callback fired
when the SDK obtained a fresh QR URL from the upstream personal-WeChat
service, and whose ``on_logout`` callback fired when the session
expired.  :class:`WechatPersonalQrLoginAdapter` restores that wiring on
top of the real :class:`SqliteQrLoginChallengeRepository`: when the
SDK reports a URL it is published as the ``qr_url`` field of the
persisted :class:`QrLoginChallenge`; on logout the challenge is moved
to :class:`QrLoginStatus.EXPIRED` so polling clients see the
transition.

Provider-specific notes
-----------------------

* **WeChat (personal)**: uses the legacy "cc_handler"-style polling
  protocol — :class:`WechatQrLogin` (the prior PR-047 adapter)
  remains for the route-driven challenge state machine.
  :class:`WechatPersonalQrLoginAdapter` (PR-097 R-13) drives the
  *real* SDK ``login`` flow so operators can scan a fresh QR with
  their personal account.
* **Feishu**: QR login is *not* supported by Feishu — the route layer
  rejects ``/api/feishu/qr/*`` with ``channels.qr_login_not_supported``
  before reaching the use case.  We still wire :class:`FeishuQrLogin`
  for symmetry; calling its methods raises
  :class:`ChannelKindNotSupportedError`.

Security
--------

Provider QR endpoints would normally need an access token + return
provider-specific blob URLs.  The PR-047 state-machine adapter
(:class:`WechatQrLogin`) implements a correct, persistent
challenge lifecycle independent of upstream provider availability;
:class:`WechatPersonalQrLoginAdapter` (PR-097 R-13) layers the live
wechatbot SDK on top so operators can complete an actual personal-
account login.
"""

from __future__ import annotations

from dataclasses import replace as _replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable

from qai.platform.errors import ExternalServiceError
from qai.platform.logging import get_logger
from qai.platform.time import Clock

from qai.channels.domain import (
    ChannelInstance,
    ChannelKind,
    QrLoginChallenge,
    QrLoginChallengeNotFoundError,
    QrLoginStatus,
)
from qai.channels.domain.errors import ChannelKindNotSupportedError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.ids import IdGenerator

    from .qr_login_repository import (
        SqliteQrLoginChallengeRepository,
    )

__all__ = [
    "WechatQrLogin",
    "FeishuQrLogin",
    "WechatPersonalQrLoginAdapter",
]


logger = get_logger(__name__)

_DEFAULT_TTL_MINUTES = 5


class _BaseQrLogin:
    """Shared challenge-lifecycle plumbing for the per-kind QR adapters."""

    KIND: ChannelKind
    PREFIX: str

    __slots__ = ("_repo", "_ids", "_clock", "_ttl_minutes")

    def __init__(
        self,
        *,
        repo: "SqliteQrLoginChallengeRepository",
        ids: "IdGenerator",
        clock: Clock,
        ttl_minutes: int = _DEFAULT_TTL_MINUTES,
    ) -> None:
        self._repo = repo
        self._ids = ids
        self._clock = clock
        self._ttl_minutes = ttl_minutes

    async def issue(
        self, instance: ChannelInstance
    ) -> QrLoginChallenge:
        if instance.kind is not self.KIND:
            raise ChannelKindNotSupportedError(instance.kind.value)
        # Mint a fresh challenge (status=ISSUED) so polling routes have a
        # row to advance.  Any prior challenge for the same instance
        # remains in the repo until its TTL expires.
        now = self._clock.now()
        challenge_id = f"{self.PREFIX}-{self._ids.new_id()}"
        challenge = QrLoginChallenge(
            challenge_id=challenge_id,
            instance_id_value=instance.instance_id.value,
            issued_at=now,
            expires_at=now + timedelta(minutes=self._ttl_minutes),
            status=QrLoginStatus.ISSUED,
        )
        await self._repo.upsert(
            challenge, instance_id=instance.instance_id.value
        )
        return challenge

    async def check_status(
        self, instance: ChannelInstance, challenge_id: str
    ) -> QrLoginChallenge:
        if instance.kind is not self.KIND:
            raise ChannelKindNotSupportedError(instance.kind.value)
        challenge = await self._repo.get(challenge_id)
        if challenge.instance_id_value != instance.instance_id.value:
            raise QrLoginChallengeNotFoundError(challenge_id)
        # Time-box: expire if past expiry.  SCANNED is exempt: once the
        # user has scanned, they may take a while to confirm on the phone
        # (and V2 then drives login() to completion + CONFIRMED out of
        # band via WechatPersonalQrLoginAdapter._run_login).  Flipping a
        # SCANNED challenge to EXPIRED on the 5-min TTL would make the
        # WebUI bounce back to "二维码过期" / break the image endpoint
        # right as confirmation lands.  V1/v0.5 never expired a scanned
        # session — the SDK login() simply awaited confirmation.  Keep
        # CONFIRMED / EXPIRED terminal as before.
        now = self._clock.now()
        if challenge.is_expired(now=now) and challenge.status not in (
            QrLoginStatus.SCANNED,
            QrLoginStatus.CONFIRMED,
            QrLoginStatus.EXPIRED,
        ):
            expired = _replace(challenge, status=QrLoginStatus.EXPIRED)
            await self._repo.upsert(
                expired, instance_id=instance.instance_id.value
            )
            return expired
        # First poll → SCANNED (mirrors legacy cc_handler proxy state).
        if challenge.status is QrLoginStatus.ISSUED:
            scanned = _replace(challenge, status=QrLoginStatus.SCANNED)
            await self._repo.upsert(
                scanned, instance_id=instance.instance_id.value
            )
            return scanned
        return challenge

    async def confirm(
        self, instance: ChannelInstance, challenge_id: str
    ) -> QrLoginChallenge:
        if instance.kind is not self.KIND:
            raise ChannelKindNotSupportedError(instance.kind.value)
        challenge = await self._repo.get(challenge_id)
        if challenge.instance_id_value != instance.instance_id.value:
            raise QrLoginChallengeNotFoundError(challenge_id)
        confirmed = _replace(challenge, status=QrLoginStatus.CONFIRMED)
        await self._repo.upsert(
            confirmed, instance_id=instance.instance_id.value
        )
        return confirmed


class WechatQrLogin(_BaseQrLogin):
    """:class:`QrLoginPort` for personal WeChat (cc_handler-style)."""

    KIND = ChannelKind.WECHAT
    PREFIX = "wechat-qr"


class FeishuQrLogin:
    """:class:`QrLoginPort` placeholder for Feishu — never invoked.

    The route layer rejects ``/api/feishu/qr/*`` with
    ``channels.qr_login_not_supported`` before any use case runs, so
    the methods here exist only to satisfy the port contract.  Calling
    any of them surfaces :class:`ChannelKindNotSupportedError` in case
    the route guard regresses.
    """

    KIND = ChannelKind.FEISHU
    __slots__ = ()

    async def issue(
        self, instance: ChannelInstance
    ) -> QrLoginChallenge:
        raise ChannelKindNotSupportedError(self.KIND.value)

    async def check_status(
        self, instance: ChannelInstance, challenge_id: str
    ) -> QrLoginChallenge:
        raise ChannelKindNotSupportedError(self.KIND.value)

    async def confirm(
        self, instance: ChannelInstance, challenge_id: str
    ) -> QrLoginChallenge:
        raise ChannelKindNotSupportedError(self.KIND.value)


# ---------------------------------------------------------------------------
# PR-097 R-13: live wechatbot SDK adapter for personal-WeChat QR login
# ---------------------------------------------------------------------------
#: Type alias for the bot factory the adapter uses.  Kept generic so a
#: stub bot can be injected in tests without importing wechatbot.
_BotFactory = Callable[..., Any]


class WechatPersonalQrLoginAdapter:
    """Drive a real personal-WeChat login through the wechatbot SDK.

    The legacy ``backend/channels/wechat/channel.py:498 trigger_login``
    constructs a ``WeChatBot(on_qr_url=..., on_logout=...)`` and calls
    ``bot.login(force=force)``.  This adapter restores that flow on
    top of the new architecture's :class:`QrLoginChallenge`
    persistence:

    * On ``trigger_login`` we mint / refresh a challenge, then build
      a bot that publishes any QR URL the SDK obtains via the
      ``on_qr_url`` callback (we update the persisted challenge with
      the URL so polling routes can return it).
    * The ``on_logout`` callback transitions the challenge to
      :class:`QrLoginStatus.EXPIRED`.
    * If the wechatbot SDK is missing — same situation as
      :class:`~qai.channels.infrastructure.wechat_longpoll.WechatLongPollTransport.start`
      — :class:`ExternalServiceError` is raised with a clear Chinese
      operator message so the WebUI surfaces "请安装 wechatbot SDK
      以启用个人微信登录" instead of a confusing stack trace.

    Why this is a separate class from :class:`WechatQrLogin`
    --------------------------------------------------------
    :class:`WechatQrLogin` (PR-047) implements the
    :class:`QrLoginPort` Protocol used by the route-driven challenge
    state machine (issue / check_status / confirm).  This adapter
    drives the *live SDK*; it lives alongside the long-poll transport
    and is invoked via :class:`apps.api._channels_di` plumbing.  The
    two are complementary — one is the persistence-driven port, the
    other is the SDK-driven implementation that publishes URLs into
    the persistence layer.
    """

    KIND = ChannelKind.WECHAT
    PREFIX = "wechat-qr"

    __slots__ = (
        "_repo",
        "_ids",
        "_clock",
        "_ttl_minutes",
        "_bot_factory",
        "_active_bot",
        "_login_task",
        "_network_policy_resolver",
        "_network_policy_active",
        "_on_confirmed",
        "_on_bot_ready",
        "_creds_path_resolver",
    )

    def __init__(
        self,
        *,
        repo: "SqliteQrLoginChallengeRepository",
        ids: "IdGenerator",
        clock: Clock,
        ttl_minutes: int = _DEFAULT_TTL_MINUTES,
        bot_factory: _BotFactory | None = None,
        network_policy_resolver: Any | None = None,
        on_confirmed: Callable[[str], Any] | None = None,
        on_bot_ready: Callable[[ChannelInstance, Any], Any] | None = None,
        creds_path_resolver: Callable[[ChannelInstance], str] | None = None,
    ) -> None:
        """Construct the adapter.

        Args:
            repo: Shared QR challenge persistence.
            ids: Id generator for new challenge ids.
            clock: Source of ``now()``.
            ttl_minutes: Challenge TTL — matches PR-047 default.
            bot_factory: Optional injectable bot factory for tests.
                When ``None`` we import ``wechatbot.WeChatBot`` lazily
                at :meth:`trigger_login` time.
            network_policy_resolver: Optional callable
                ``(instance) -> SdkNetworkPolicy | None`` applying proxy
                + per-domain TLS-skip to the wechatbot SDK's internal
                aiohttp traffic for the QR-login + connected-session
                lifetime (V1 parity:
                ``backend/channels/wechat/channel.py:38-59``).
            on_confirmed: Optional callable ``(instance_id_value) ->
                Awaitable | None`` invoked once ``bot.login()`` returns
                successfully (user confirmed the scan on the phone).
                Wired by ``apps.api._channels_di`` to
                ``StartChannelInstanceUseCase.execute`` so the inbound
                long-poll transport comes up and the channel starts
                receiving messages — V1/v0.5 parity:
                ``backend/channels/wechat/channel.py:772,782`` set
                ``_status="connected"`` then ``await _bot.start()`` in
                the same ``_run_poll`` loop. V2 splits login (this
                adapter) from message-receiving (``WechatLongPollTransport``
                via the lifecycle use case), so the confirmed callback
                bridges the two; the long-poll bot reuses the scanned
                credentials persisted per instance (no second scan).
        """
        self._repo = repo
        self._ids = ids
        self._clock = clock
        self._ttl_minutes = ttl_minutes
        self._bot_factory = bot_factory
        self._active_bot: Any = None
        self._login_task: Any = None
        self._network_policy_resolver = network_policy_resolver
        self._network_policy_active: bool = False
        self._on_confirmed = on_confirmed
        self._on_bot_ready = on_bot_ready
        self._creds_path_resolver = creds_path_resolver

    async def trigger_login(
        self, instance: ChannelInstance, force: bool = False
    ) -> str:
        """Trigger the SDK login flow — equivalent to legacy
        ``trigger_login(force)``.

        Mints a fresh challenge, builds the bot with the
        URL-publish / scanned / expire callbacks wired into the
        persisted challenge, then calls ``bot.login(force=...)``.
        Errors surface as :class:`ExternalServiceError` so the route
        layer can map them to a clean envelope.

        Returns the minted ``challenge_id`` so the route layer can hand
        it back to the WebUI, which then polls
        ``GET /api/wechat/qr-image?challenge_id=...`` for the scannable
        QR PNG and ``GET /api/wechat/qr/{id}/status`` for login progress.
        Without returning it the front-end had no way to address the
        SDK-driven challenge (the prior bug behind "二维码无法显示").
        """
        if instance.kind is not self.KIND:
            raise ChannelKindNotSupportedError(instance.kind.value)

        # Apply proxy + per-domain TLS-skip for the wechatbot SDK's
        # internal aiohttp traffic before building the bot (V1 parity:
        # ``backend/channels/wechat/channel.py:38-59``).  Held until
        # :meth:`logout`.
        self._apply_network_policy(instance)

        # Mint / refresh the challenge so the SDK callback has a
        # row to update.  Any prior challenge for the same instance
        # remains in the repo until its TTL expires; UI polling will
        # find the most recent by id (which the route layer
        # remembers across the issue / image / WS calls).
        now = self._clock.now()
        challenge_id = f"{self.PREFIX}-{self._ids.new_id()}"
        challenge = QrLoginChallenge(
            challenge_id=challenge_id,
            instance_id_value=instance.instance_id.value,
            issued_at=now,
            expires_at=now + timedelta(minutes=self._ttl_minutes),
            status=QrLoginStatus.ISSUED,
        )
        await self._repo.upsert(
            challenge, instance_id=instance.instance_id.value
        )

        try:
            bot = await self._build_bot(instance, challenge_id)
        except Exception:
            # Build failed (e.g. SDK missing) — release the policy we
            # installed so the reference count stays balanced.
            self._release_network_policy()
            raise
        self._active_bot = bot

        login_fn = getattr(bot, "login", None)
        if login_fn is None:
            self._release_network_policy()
            raise ExternalServiceError(
                "channels.wechat.sdk_incompatible",
                "wechatbot Bot 实例缺少 login() 方法,无法触发个人微信登录",
                service=self.KIND.value,
            )

        import asyncio as _asyncio

        # The wechatbot SDK's ``login`` is a coroutine that AWAITS until
        # the user scans the QR (it returns ``Credentials`` on success).
        # We must NOT await it inline — that would block this request
        # until the scan completes and the WebUI would never receive the
        # challenge_id it needs to render / poll the QR.  V1 ran the
        # login flow as a background task (``_login_and_poll``); mirror
        # that here: kick login off fire-and-forget and return the
        # challenge_id immediately.  The QR URL arrives out-of-band via
        # the ``on_qr_url`` callback (persisted onto the challenge), and
        # ``on_scanned`` / ``on_expired`` drive the challenge state so
        # the WebUI's status poll observes progress.
        instance_id_value = instance.instance_id.value

        async def _run_login() -> None:
            try:
                result = login_fn(force=force)
                if _asyncio.iscoroutine(result):
                    await result
                # V1/v0.5 parity: ``bot.login()`` returns ONLY after the
                # user confirms the scan on their phone (the SDK awaits
                # confirmation internally). V1
                # ``backend/channels/wechat/channel.py:745`` then enters
                # ``_run_poll`` which sets ``_status="connected"`` (:772)
                # and ``await _bot.start()`` (:782).  V2 splits these:
                # here we (1) advance the challenge to CONFIRMED so the
                # WebUI's status poll flips from "已扫码" to "已连接",
                # and (2) fire ``_on_confirmed`` to bring up the inbound
                # long-poll transport (StartChannelInstanceUseCase) so the
                # channel actually receives messages.  Without this the UI
                # spun forever on "已扫码,请在手机上确认" and WeChat replies
                # never arrived.
                existing = await self._repo.find(challenge_id)
                if existing is not None and existing.status not in (
                    QrLoginStatus.CONFIRMED,
                    QrLoginStatus.EXPIRED,
                ):
                    await self._repo.upsert(
                        _replace(existing, status=QrLoginStatus.CONFIRMED),
                        instance_id=instance_id_value,
                    )
                # Bring up message-receiving (long-poll transport).  Best
                # effort: a start failure must not crash the fire-and-forget
                # login task — the challenge is already CONFIRMED so the UI
                # shows connected; transport errors surface via the channel
                # status / watchdog reconnect path.
                #
                # Method A (single-bot, V1 parity): hand the SAME, now
                # logged-in bot to the long-poll transport BEFORE
                # ``_on_confirmed`` so the transport's ``start`` adopts it and
                # runs ``start()`` on it (no second bot, no second login, no
                # second QR — V1 ``_run_poll`` uses the same ``_bot`` that
                # logged in).  Ownership transfers to the transport: we clear
                # ``_active_bot`` so ``logout`` does not also stop the bot the
                # transport now owns (avoids a double-stop / torn session).
                if self._on_bot_ready is not None:
                    try:
                        ready = self._on_bot_ready(instance, bot)
                        if _asyncio.iscoroutine(ready):
                            await ready
                        # Hand-off succeeded → transport owns the bot now.
                        self._active_bot = None
                    except _asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "channels.wechat.bot_handoff_failed",
                            instance_id=instance_id_value,
                            challenge_id=challenge_id,
                            error=str(exc),
                        )
                if self._on_confirmed is not None:
                    try:
                        started = self._on_confirmed(instance_id_value)
                        if _asyncio.iscoroutine(started):
                            await started
                    except _asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "channels.wechat.post_login_start_failed",
                            instance_id=instance_id_value,
                            challenge_id=challenge_id,
                            error=str(exc),
                        )
            except _asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # The wechatbot SDK does ``json.loads`` on the raw HTTP body
                # (``wechatbot/protocol.py``), so a non-JSON upstream response
                # — an HTML 403/interception page returned when the request did
                # NOT go through the configured proxy (direct-connect →
                # corporate gateway block page), or when the proxy itself
                # returns a block page — surfaces as
                # ``JSONDecodeError: Expecting value: line 1 column 1 (char 0)``.
                # Log a calm, actionable WARNING for that case (no scary
                # traceback), and reserve ERROR for genuinely unexpected faults.
                exc_name = type(exc).__name__
                msg = str(exc)
                is_non_json_upstream = exc_name == "JSONDecodeError" or (
                    "Expecting value" in msg
                    or "line 1 column 1 (char 0)" in msg
                )
                if is_non_json_upstream:
                    logger.warning(
                        "channels.wechat.qr_login.upstream_non_json",
                        instance_id=instance_id_value,
                        challenge_id=challenge_id,
                        error=msg,
                        exc_type=exc_name,
                        hint=(
                            "WeChat endpoint returned a non-JSON body (usually "
                            "an HTML 403/interception page). The SDK request "
                            "most likely did NOT go through the configured "
                            "proxy, or the proxy returned a block page. Verify "
                            "the WeChat channel proxy settings and that the "
                            "proxy can reach ilinkai.weixin.qq.com, then retry."
                        ),
                    )
                else:
                    logger.error(
                        "channels.wechat.qr_login_failed",
                        instance_id=instance_id_value,
                        challenge_id=challenge_id,
                        error=msg,
                    )
                # Mark the challenge EXPIRED so the WebUI's status poll
                # surfaces the failure instead of spinning forever.
                try:
                    existing = await self._repo.find(challenge_id)
                    if existing is not None and (
                        existing.status is not QrLoginStatus.EXPIRED
                    ):
                        await self._repo.upsert(
                            _replace(
                                existing, status=QrLoginStatus.EXPIRED
                            ),
                            instance_id=instance_id_value,
                        )
                except Exception:  # noqa: BLE001
                    pass
                # Release the network policy on failure — otherwise a
                # dangling policy pins the aiohttp monkey-patch to the
                # (possibly stale) proxy config used for THIS attempt,
                # and the next ``trigger_login`` cannot install a fresh
                # policy with the reference count > 0.  ``_apply`` now
                # also re-installs unconditionally, so this is belt-
                # and-braces; keeping it here keeps refcount balanced
                # even when the user never retries logout.
                self._release_network_policy()

        # Validate up front that the SDK bot constructed cleanly (the
        # build above already ran); spawn the login loop and return.
        # R-4 — supersede any prior login task so a repeated
        # ``trigger_login`` does not leak the previous fire-and-forget
        # task. We cancel (but do not await — we must return the new
        # challenge_id immediately) and let it tear down in the
        # background.
        prev = self._login_task
        if prev is not None and not prev.done():
            prev.cancel()
        self._login_task = _asyncio.ensure_future(_run_login())
        return challenge_id

    def _apply_network_policy(self, instance: ChannelInstance) -> None:
        """Install / refresh the SDK network policy from the CURRENT instance.

        Every ``trigger_login`` call re-resolves the policy from the DB and
        re-installs it — the adapter **must not** latch a stale in-memory
        policy across logins.  This directly satisfies AGENTS.md §🔴
        State-Truth-First Rule 3 (proxy is a real external resource; its
        truth lives in ``settings_v1.proxy`` on disk, not in ``self._…``).

        Concretely: without this refresh, the sequence
          1. user clicks "连接微信" without proxy configured →
             ``install_sdk_network_policy(proxy_url="")`` installs the
             aiohttp monkey-patch with ``proxy=None``;
          2. user configures a proxy in ChannelProxyPanel and saves →
             ``settings_v1.proxy.url`` is persisted, but the closure
             inside the already-installed patch still captures the empty
             string;
          3. user clicks "连接微信" again → we short-circuit on the old
             latch flag and never re-read the DB.

        The whole SDK stays wedged on the empty-proxy patch until the
        server is restarted.  Re-resolving + re-installing on every call
        makes ``proxy_url`` in the aiohttp patch closure track the DB
        without any restart / logout dance.
        """
        resolver = self._network_policy_resolver
        if resolver is None:
            return
        try:
            policy = resolver(instance)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "channels.wechat.qr_login.network_policy_failed",
                instance_id=instance.instance_id.value,
                error=str(exc),
            )
            return
        if policy is None:
            return
        from qai.channels.infrastructure.sdk_network import (
            install_sdk_network_policy,
        )

        # If a policy was installed on a previous call, release it first
        # so the reference count drops to zero and the next install
        # applies the freshly-resolved policy (aiohttp/requests/websockets
        # patches capture proxy_url in a closure — re-installing is the
        # only way to update it).  ``_release_network_policy`` is a
        # no-op when nothing is active, so this is safe on first call.
        self._release_network_policy()
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

    async def _build_bot(
        self, instance: ChannelInstance, challenge_id: str
    ) -> Any:
        """Construct the SDK ``WeChatBot`` with QR / logout callbacks.

        Both callbacks are synchronous (the SDK fires them on
        whatever thread it chooses), so we stash an event loop
        reference and use :func:`asyncio.run_coroutine_threadsafe`
        to drive the persistence updates without blocking the
        callback thread.
        """
        import asyncio as _asyncio

        loop = _asyncio.get_running_loop()
        instance_id_value = instance.instance_id.value
        repo = self._repo
        clock = self._clock
        ttl_minutes = self._ttl_minutes

        def _publish_qr_url(url: str) -> None:
            """SDK callback: a fresh QR URL is available for scanning."""
            async def _update() -> None:
                existing = await repo.find(challenge_id)
                if existing is None:
                    # Challenge was reaped; rebuild it so the URL is
                    # not silently dropped.
                    now = clock.now()
                    base = QrLoginChallenge(
                        challenge_id=challenge_id,
                        instance_id_value=instance_id_value,
                        issued_at=now,
                        expires_at=now + timedelta(minutes=ttl_minutes),
                        status=QrLoginStatus.ISSUED,
                    )
                else:
                    base = existing
                # Persist the REAL provider QR URL onto the challenge so
                # ``GET /qr/{id}/image`` can encode a scannable WeChat
                # login QR (V1 parity:
                # ``backend/channels/wechat/channel.py:794`` stores the
                # SDK url in ``_qr_url`` →
                # ``api_routes.py:94 qrcode.make(qr_url)``).  Touch
                # ``issued_at`` too so clients polling "is there a fresh
                # QR?" observe the change.
                logger.info(
                    "channels.wechat.qr_url_published",
                    instance_id=instance_id_value,
                    challenge_id=challenge_id,
                    url=str(url)[:200],
                )
                refreshed = _replace(
                    base, issued_at=clock.now(), qr_url=str(url)
                )
                await repo.upsert(
                    refreshed, instance_id=instance_id_value
                )

            try:
                if loop is None or loop.is_closed():
                    return
                _asyncio.run_coroutine_threadsafe(_update(), loop)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.wechat.qr_publish_failed",
                    challenge_id=challenge_id,
                    error=str(exc),
                )

        def _on_scanned() -> None:
            """SDK callback: the user scanned the QR — advance to SCANNED."""
            async def _update() -> None:
                existing = await repo.find(challenge_id)
                if existing is None:
                    return
                if existing.status in (
                    QrLoginStatus.SCANNED,
                    QrLoginStatus.CONFIRMED,
                    QrLoginStatus.EXPIRED,
                ):
                    return
                scanned = _replace(existing, status=QrLoginStatus.SCANNED)
                await repo.upsert(
                    scanned, instance_id=instance_id_value
                )

            try:
                if loop is None or loop.is_closed():
                    return
                _asyncio.run_coroutine_threadsafe(_update(), loop)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.wechat.qr_scanned_failed",
                    challenge_id=challenge_id,
                    error=str(exc),
                )

        def _expire(*_args: object) -> None:
            """SDK callback: QR expired / session ended → mark EXPIRED.

            Accepts ``*_args`` so it is usable as both the SDK's
            ``on_expired()`` (no-arg) and ``on_error(exc)`` (one-arg)
            callback shapes — either way the challenge is moved to
            :class:`QrLoginStatus.EXPIRED` so polling clients stop
            waiting on a dead QR.
            """
            async def _update() -> None:
                existing = await repo.find(challenge_id)
                if existing is None:
                    return
                if existing.status is QrLoginStatus.EXPIRED:
                    return
                expired = _replace(
                    existing, status=QrLoginStatus.EXPIRED
                )
                await repo.upsert(
                    expired, instance_id=instance_id_value
                )

            try:
                if loop is None or loop.is_closed():
                    return
                _asyncio.run_coroutine_threadsafe(_update(), loop)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.wechat.qr_expire_failed",
                    challenge_id=challenge_id,
                    error=str(exc),
                )

        if self._bot_factory is not None:
            bot = self._bot_factory(
                on_qr_url=_publish_qr_url,
                on_scanned=_on_scanned,
                on_expired=_expire,
                on_error=_expire,
            )
            if _asyncio.iscoroutine(bot):
                bot = await bot
            return bot

        try:
            import wechatbot  # type: ignore[import-not-found]
        except ImportError as exc:
            logger.warning(
                "channels.wechat.sdk_missing",
                instance_id=instance_id_value,
                detail=str(exc),
            )
            raise ExternalServiceError(
                "channels.wechat.sdk_missing",
                "wechatbot SDK 未安装,无法触发个人微信扫码登录;"
                "请先安装 SDK 或在配置中改用其他通道类型。",
                service=self.KIND.value,
                cause=exc,
            ) from exc

        bot_cls = getattr(wechatbot, "WeChatBot", None) or getattr(
            wechatbot, "Bot", None
        )
        if bot_cls is None:
            raise ExternalServiceError(
                "channels.wechat.sdk_incompatible",
                "wechatbot 模块已加载但未导出 WeChatBot/Bot 类",
                service=self.KIND.value,
            )
        # The wechatbot ``WeChatBot`` exposes ``on_qr_url`` /
        # ``on_scanned`` / ``on_expired`` / ``on_error`` constructor
        # callbacks (verified against the installed SDK signature).  V1
        # wired all four (``backend/channels/wechat/channel.py:823-829``);
        # the prior V2 code passed a non-existent ``on_logout=`` kwarg
        # which made ``WeChatBot(**)`` raise ``TypeError`` before login
        # could even start — surfacing to the WebUI as "个人微信登录失败"
        # and a blank QR.  Wire only the SDK-supported callbacks.
        #
        # cred_path (Method A / V1 parity): pass the SAME per-instance
        # ``cred_path`` the long-poll transport uses
        # (``creds_path_resolver`` == ``WechatLongPollTransport._cred_path_for``)
        # so the scanned session is persisted where the transport expects it.
        # This bot IS the one the transport adopts and runs ``start()`` on
        # (single-bot), and on a later restart the same file lets
        # ``login(force=False)`` skip the QR.  The prior code omitted
        # ``cred_path`` → the SDK saved to its default ``~/.wechatbot`` path,
        # mismatching the transport's per-instance path (the "反复弹码" root
        # cause when a second bot was built).
        kwargs: dict[str, Any] = {
            "on_qr_url": _publish_qr_url,
            "on_scanned": _on_scanned,
            "on_expired": _expire,
            "on_error": _expire,
        }
        if self._creds_path_resolver is not None:
            try:
                kwargs["cred_path"] = self._creds_path_resolver(instance)
            except Exception as exc:  # noqa: BLE001 — resolver is best-effort
                logger.warning(
                    "channels.wechat.qr_login.cred_path_resolve_failed",
                    instance_id=instance_id_value,
                    error=str(exc),
                )
        return bot_cls(**kwargs)

    async def logout(self) -> None:
        """Tear down the active bot, if any.

        Mirrors the legacy
        ``backend/channels/wechat/channel.py`` logout path so the
        challenge moves to :class:`QrLoginStatus.EXPIRED` deterministically.
        """
        import asyncio as _asyncio

        # R-4 — cancel and await the background login task so logout
        # (and app shutdown) does not leave the fire-and-forget
        # ``_run_login`` coroutine running / the SDK ``login`` awaiting a
        # scan that will never come.
        task = self._login_task
        self._login_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except _asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — teardown best-effort
                pass
        bot = self._active_bot
        self._active_bot = None
        # Release the SDK network policy installed at trigger_login().
        self._release_network_policy()
        if bot is None:
            return
        logout_fn = getattr(bot, "logout", None)
        if logout_fn is None:
            return

        try:
            result = logout_fn()
            if _asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.wechat.logout_failed",
                error=str(exc),
            )
