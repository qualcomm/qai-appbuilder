# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Inbound WeChat long-poll transport (S9 PR-093 §2.1 C-8).

Restores the personal-WeChat inbound message reception that the legacy
``backend/channels/wechat/channel.py:_create_bot/_run_poll`` provided
via the third-party ``wechatbot`` SDK.  The new architecture had
outbound HTTP transports
(:class:`qai.channels.infrastructure.transports.WechatTransport`) but
no inbound counterpart — addressed by parity-audit row §2.1 C-8.

This file is named ``wechat_longpoll.py`` and lives at
``qai.channels.infrastructure.wechat_longpoll`` to avoid colliding
with the existing
``qai.channels.infrastructure.transports`` *module* (a single
``transports.py`` file would conflict with a ``transports/`` package
of the same name).

Implements :class:`~qai.channels.application.ports.InboundTransportPort`
so the dispatch bridge in :mod:`apps.api._channel_dispatch_bridge`
can consume messages with the same shape regardless of provider.
The watchdog
:class:`~qai.channels.infrastructure.transport_watchdog.TransportWatchdog`
supervises this transport for reconnection on transient SDK failure.

Optional dependency
-------------------
The ``wechatbot`` SDK is **not** in the runtime dependency manifest
(it is provider-locked and not always available); the import is
guarded so this module loads cleanly in CI / dev environments where
the SDK is missing.  When the SDK is unavailable :meth:`start` raises
:class:`~qai.platform.errors.ExternalServiceError` with a clear
operator-facing message.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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

logger = get_logger(__name__)

__all__ = ["WechatLongPollTransport"]

# Reconnect backoff (seconds) — matches the legacy WeChat inbound
# loop and is shared with TransportWatchdog's _RECONNECT_DELAYS.
_RECONNECT_BACKOFF: tuple[float, ...] = (5.0, 10.0, 30.0, 60.0, 120.0)

#: Bounded inbound-dedup set sizing (4-M14 / §3.7 bounded-prune).  This
#: dedup is a V2 addition mirroring the Feishu transport's bounded-prune
#: set (``feishu_ws.py``); V1/v0.5 personal-WeChat (``backend/channels/
#: wechat/channel.py``) had no processed-id set of its own. Same sizing as
#: Feishu: drop the oldest 500 once the map grows past 1000.
_DEDUP_MAX = 1000
_DEDUP_PRUNE = 500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dedup_check_and_add(seen: dict[str, None], message_id: str) -> bool:
    """Return ``True`` when ``message_id`` was already seen (a duplicate).

    Bounded insertion-ordered set: inserts new ids, evicting the oldest
    :data:`_DEDUP_PRUNE` once the map exceeds :data:`_DEDUP_MAX` (V1
    bounded-prune parity).  Falsy ids are never deduplicated so a synthesised
    fallback id never collapses distinct messages.
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


class WechatLongPollTransport(InboundTransportPort):
    """Inbound transport over the wechatbot SDK long-poll loop.

    Per-instance state — no module-level globals (matches the
    "state-on-the-instance" rule used by the outbound transports).
    The SDK / bot factory is **injected** via the constructor so
    tests can substitute a stub bot without monkey-patching the
    ``wechatbot`` import.

    The transport buffers inbound messages on an
    :class:`asyncio.Queue` so :meth:`stream` is a clean iterator.
    The SDK callback (executed on whatever thread the SDK chooses)
    converts each raw event to a :class:`ChannelMessage` and pushes
    it onto the queue via :func:`asyncio.run_coroutine_threadsafe`.
    """

    KIND = ChannelKind.WECHAT

    __slots__ = (
        "_bot_factory",
        "_id_factory",
        "_clock",
        "_bot",
        "_queue",
        "_loop",
        "_started",
        "_reconnect_attempts",
        "_creds_dir",
        "_login_task",
        "_poll_task",
        "_adopted_bots",
        "_network_policy_resolver",
        "_network_policy_active",
        "_seen_message_ids",
        # Remembered across the connection lifetime so the watchdog's
        # no-arg ``restart()`` can rebuild the bot without the caller
        # re-supplying them.  ``_credentials`` is sensitive (§3.3):
        # in-memory only, never logged / persisted.  For the adopted
        # (QR-login) path ``_credentials`` is ``""`` — the rebuild
        # then reuses the persisted scanned session at ``cred_path``.
        "_instance",
        "_credentials",
    )

    def __init__(
        self,
        *,
        bot_factory: Any | None = None,
        id_factory: Any | None = None,
        clock: Any | None = None,
        queue_max_size: int = 256,
        creds_dir: Any | None = None,
        network_policy_resolver: Any | None = None,
    ) -> None:
        """Construct the transport.

        Args:
            bot_factory: Callable returning a wechatbot SDK ``Bot``
                instance bound to the given credentials.  When
                ``None`` we default to importing ``wechatbot`` at
                :meth:`start` time and constructing the bot from
                the SDK directly.
            id_factory: Optional ``ids`` provider used to mint
                :class:`ChannelMessageId` values.  When ``None`` we
                fall back to ``uuid.uuid4().hex``.
            clock: Optional clock returning ``datetime``; defaults
                to UTC ``datetime.now``.
            queue_max_size: Bounded buffer for inbound messages.
            creds_dir: Directory where the personal-WeChat scanned
                session (``cred_path``) is persisted per instance
                (V1 parity: ``data_dir / "wechat_creds.json"``).  When
                ``None`` a ``data/channels/wechat`` fallback is used.
            network_policy_resolver: Optional callable
                ``(instance) -> SdkNetworkPolicy | None`` used to apply
                proxy + per-domain TLS-skip to the wechatbot SDK's
                internal aiohttp traffic for the lifetime of the
                connection (V1 parity:
                ``backend/channels/wechat/channel.py:38-59``).  When
                ``None`` no network policy is applied.
        """
        from pathlib import Path

        self._bot_factory = bot_factory
        self._id_factory = id_factory
        self._clock = clock
        self._creds_dir = Path(creds_dir) if creds_dir is not None else None
        self._bot: Any = None
        self._login_task: Any = None
        self._poll_task: Any = None
        # Method A (single-bot, V1 parity): the QR-login adapter hands its
        # already-logged-in ``WeChatBot`` here via :meth:`adopt_bot` so the
        # transport drives ``start()`` on the SAME bot that scanned the QR —
        # no second bot, no second login, no second QR (the "反复弹码" bug).
        # Keyed by instance_id value; consumed (popped) by :meth:`start`.
        self._adopted_bots: dict[str, Any] = {}
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
        # Remembered at start() so restart() can rebuild the bot.
        self._instance: ChannelInstance | None = None
        self._credentials: str | None = None

    # ------------------------------------------------------------------
    # InboundTransportPort
    # ------------------------------------------------------------------
    def adopt_bot(self, instance: ChannelInstance, bot: Any) -> None:
        """Adopt an already-logged-in ``WeChatBot`` for ``instance`` (Method A).

        V1 parity (``backend/channels/wechat/channel.py`` single ``_bot``):
        the personal-WeChat bot that scanned the QR and logged in is the SAME
        bot that runs the long-poll receive loop.  V2 splits *triggering* the
        QR login (the :class:`WechatPersonalQrLoginAdapter`) from *receiving*
        messages (this transport), so the adapter hands its live, logged-in
        bot here once ``login()`` returns; the next :meth:`start` for this
        instance drives ``start()`` on that very bot instead of constructing a
        second bot and logging in again (which would pop a fresh QR — the
        "反复弹码" bug).

        Idempotent / last-write-wins per instance.  The adopted bot is
        consumed by :meth:`start`; ownership (and the duty to ``bot.stop()`` it
        in :meth:`stop`) transfers to this transport at that point.
        """
        if instance.kind is not self.KIND:
            raise AssertionError(
                f"transport bound to {self.KIND!r} got {instance.kind!r}"
            )
        self._adopted_bots[instance.instance_id.value] = bot

    async def start(
        self, instance: ChannelInstance, credentials: str
    ) -> None:
        if instance.kind is not self.KIND:
            raise AssertionError(
                f"transport bound to {self.KIND!r} got {instance.kind!r}"
            )
        # Method A: when the QR-login adapter has handed us an already-logged-in
        # bot for this instance, adopt it and drive the receive loop on that
        # SAME bot (skip build + the second login — V1 single-bot parity). No
        # credentials are required in this path: the adopted bot already holds
        # its session in memory.
        adopted = self._adopted_bots.pop(instance.instance_id.value, None)
        if adopted is not None:
            self._loop = asyncio.get_running_loop()
            # Network policy: the QR adapter installed it at trigger_login and
            # keeps its own ref until logout; we ALSO take a ref for the
            # transport's own lifetime so the patches survive the adapter's
            # eventual release (ref-counted in sdk_network — each component
            # balances its own apply/release).
            self._apply_network_policy(instance)
            self._bot = adopted
            self._wire_callbacks(self._bot, instance)
            self._started.add(instance.instance_id.value)
            self._reconnect_attempts = 0
            # Remember context for watchdog reconnect.  Adopted bots
            # have no credentials; a rebuild reuses the persisted
            # scanned session at ``cred_path``.
            self._instance = instance
            self._credentials = ""
            # Adopted bot is already logged in → only run the blocking
            # ``start()`` receive loop (skip login).
            self._spawn_receive_loop(
                self._bot, instance, skip_login=True
            )
            logger.info(
                "channels.wechat.longpoll.started",
                instance_id=instance.instance_id.value,
                adopted=True,
            )
            return
        if not credentials:
            raise ExternalServiceError(
                "channels.transport.missing_credentials",
                "wechat long-poll transport requires credentials",
                service=self.KIND.value,
            )
        self._loop = asyncio.get_running_loop()
        # Apply proxy + per-domain TLS-skip for the wechatbot SDK's
        # internal aiohttp traffic before constructing the bot, and keep
        # it active for the whole connection (V1 parity:
        # ``backend/channels/wechat/channel.py:38-59`` patched aiohttp at
        # import; we scope it to the connection lifetime + restore on stop).
        self._apply_network_policy(instance)
        try:
            self._bot = await self._build_bot(instance, credentials)
        except Exception:
            # Build failed — release the policy we just installed so the
            # reference count stays balanced.
            self._release_network_policy()
            # Forget any context so a stray watchdog restart() cannot
            # resurrect a never-started instance.
            self._instance = None
            self._credentials = None
            raise
        self._started.add(instance.instance_id.value)
        self._reconnect_attempts = 0
        # Remember context so the watchdog's no-arg restart() can rebuild.
        self._instance = instance
        self._credentials = credentials
        logger.info(
            "channels.wechat.longpoll.started",
            instance_id=instance.instance_id.value,
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
                "channels.wechat.longpoll.network_policy_failed",
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
        # Forget the connection context — a subsequent restart() must
        # not resurrect a stopped instance.
        self._instance = None
        self._credentials = None
        # Drop any adopted-but-not-yet-started bot for this instance so a
        # ``stop`` before the lifecycle ``start`` consumed it does not leak a
        # logged-in bot (the SAME bot is torn down via ``self._bot`` below once
        # ``start`` adopted it; this only covers the not-yet-started window).
        pending = self._adopted_bots.pop(instance.instance_id.value, None)
        if pending is not None and pending is not self._bot:
            try:
                stop_fn = getattr(pending, "stop", None) or getattr(
                    pending, "close", None
                )
                if stop_fn is not None:
                    result = stop_fn()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.warning(
                    "channels.wechat.longpoll.pending_bot_cleanup_failed",
                    instance_id=instance.instance_id.value,
                    error=str(exc),
                )
        # Release the SDK network policy installed at start().
        self._release_network_policy()
        # Cancel the background QR-login task (started fire-and-forget in
        # _build_bot) so it does not outlive stop() and leak into the
        # process (important for the in-process test suite).
        login_task = self._login_task
        self._login_task = None
        if login_task is not None and not login_task.done():
            login_task.cancel()
            try:
                await login_task
            except asyncio.CancelledError:
                # We cancelled this task ourselves; benign on stop().
                pass
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                logger.warning(
                    "channels.wechat.longpoll.login_task_cleanup_failed",
                    error=str(exc),
                    exc_info=True,
                )
        bot = self._bot
        self._bot = None
        if bot is not None:
            try:
                stop_fn = getattr(bot, "stop", None) or getattr(
                    bot, "close", None
                )
                if stop_fn is not None:
                    result = stop_fn()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.warning(
                    "channels.wechat.longpoll.stop_failed",
                    instance_id=instance.instance_id.value,
                    error=str(exc),
                )
        # Tear down the login→poll background task (V1 parity:
        # ``_poll_task`` cancellation on stop, ``channel.py``).  ``bot.stop()``
        # above unblocks the SDK's blocking ``start()`` loop so the task
        # finishes; we still cancel + await as a hard fallback so the blocking
        # loop never outlives stop() as an orphan (State-Truth-First 铁律5:
        # 异常退出路径必须兜底).
        poll_task = self._poll_task
        self._poll_task = None
        if poll_task is not None and not poll_task.done():
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                # We cancelled this task ourselves; benign on stop().
                pass
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                logger.warning(
                    "channels.wechat.longpoll.poll_task_cleanup_failed",
                    error=str(exc),
                    exc_info=True,
                )

    async def stream(
        self, instance: ChannelInstance
    ) -> AsyncIterator[ChannelMessage]:  # type: ignore[override]
        """Yield inbound messages until :meth:`stop` is called."""
        while instance.instance_id.value in self._started:
            try:
                msg = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            yield msg

    def is_alive(self) -> bool:
        """Return ``True`` if the SDK bot is connected.

        The bot exposes ``is_logged_in()`` / ``is_connected()`` in
        most builds; we duck-type both names so the watchdog can
        detect drops without coupling to a specific SDK version.

        State-Truth-First (AGENTS.md 铁律1): when the real ``wechatbot``
        SDK exposes NONE of those liveness methods (V1/v0.5 never called
        any — they monitored the poll task instead), we must NOT blindly
        ``return True`` (that would make the watchdog永久失效 for WeChat —
        a dropped bot whose poll loop已退出 would never be detected /
        reconnected). Fall back to the live poll-task handle, mirroring
        v0.5's watchdog which checks ``_poll_task.done()``
        (``backend/channels/wechat/channel.py`` ``_watchdog_loop``) and the
        feishu transport's own task-liveness fallback.
        """
        bot = self._bot
        if bot is None:
            return False
        for attr in ("is_logged_in", "is_connected", "is_running"):
            fn = getattr(bot, attr, None)
            if callable(fn):
                try:
                    return bool(fn())
                except Exception:  # noqa: BLE001
                    return False
        # No SDK liveness method — use the real poll-task state as the
        # truth source (a finished/cancelled task means the receive loop
        # is dead and the connection must be rebuilt).
        return self._poll_task is not None and not self._poll_task.done()

    async def restart(self) -> None:
        """Tear down the dead bot and rebuild it (true reconnect).

        Called by :class:`TransportWatchdog` when :meth:`is_alive`
        reports the connection dropped.  The previous implementation
        only set ``self._bot = None`` and relied on a caller re-invoking
        ``start()`` — but the watchdog only ever calls ``restart()``, so
        the bot was never rebuilt: ``is_alive()`` stayed ``False`` and
        the watchdog re-``restart()``-ed forever without reconnecting.
        We now rebuild the bot from the ``instance`` + ``credentials``
        remembered at :meth:`start`.  ``_build_bot`` reuses the
        persisted scanned session at ``cred_path`` (V1 parity), so a
        transient drop reconnects without a fresh QR prompt; only a
        truly invalid session falls back to QR.  ``self._started`` /
        ``self._queue`` are left intact so the in-flight :meth:`stream`
        consumer continues uninterrupted.
        """
        self._reconnect_attempts += 1
        attempt = self._reconnect_attempts
        delay_ix = min(attempt - 1, len(_RECONNECT_BACKOFF) - 1)
        instance = self._instance
        credentials = self._credentials
        if instance is None or credentials is None:
            # Never started (or already stopped) — nothing to reconnect.
            logger.warning(
                "channels.wechat.longpoll.restart_no_context",
                attempt=attempt,
            )
            self._bot = None
            return
        logger.warning(
            "channels.wechat.longpoll.restart",
            attempt=attempt,
            backoff_seconds=_RECONNECT_BACKOFF[delay_ix],
        )
        # Drop the dead bot before rebuilding. Best-effort stop; the SDK
        # ``start()`` poll loop is superseded by _spawn_receive_loop in
        # _build_bot (it cancels the prior _poll_task).
        bot = self._bot
        self._bot = None
        if bot is not None:
            try:
                stop_fn = getattr(bot, "stop", None) or getattr(
                    bot, "close", None
                )
                if stop_fn is not None:
                    result = stop_fn()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                logger.warning(
                    "channels.wechat.longpoll.restart_stop_failed",
                    instance_id=instance.instance_id.value,
                    error=str(exc),
                )
        # Rebuild via the same path start() uses; reuses persisted session.
        self._bot = await self._build_bot(instance, credentials)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _build_bot(
        self, instance: ChannelInstance, credentials: str
    ) -> Any:
        """Construct the SDK bot or raise a clear operator error."""
        if self._bot_factory is not None:
            bot = self._bot_factory(instance=instance, credentials=credentials)
            if asyncio.iscoroutine(bot):
                bot = await bot
            self._wire_callbacks(bot, instance)
            self._spawn_receive_loop(bot, instance)
            return bot
        try:
            import wechatbot  # type: ignore[import-not-found]
        except ImportError as exc:
            logger.warning(
                "channels.wechat.longpoll.sdk_missing",
                instance_id=instance.instance_id.value,
                detail=str(exc),
            )
            raise ExternalServiceError(
                "channels.transport.sdk_missing",
                "wechatbot SDK not installed; cannot start long-poll "
                "transport for personal WeChat. Install the SDK or "
                "use a different ChannelKind.",
                service=self.KIND.value,
                cause=exc,
            ) from exc
        # The wechatbot SDK exposes its bot class as ``WeChatBot`` (V1
        # ``backend/channels/wechat/channel.py:22`` + the QR-login adapter
        # ``qr_login.py:462`` both use ``WeChatBot``).  Older builds may
        # have shipped a ``Bot`` alias; accept either so this aligns with
        # V1 / the QR path and never regresses on the real SDK.
        bot_cls = getattr(wechatbot, "WeChatBot", None) or getattr(
            wechatbot, "Bot", None
        )
        if bot_cls is None:
            raise ExternalServiceError(
                "channels.transport.sdk_incompatible",
                "wechatbot module loaded but does not expose a "
                "WeChatBot/Bot class",
                service=self.KIND.value,
            )
        # Construct the bot using the wechatbot SDK's real signature.
        # The personal-WeChat ``WeChatBot`` is QR-login based (V1
        # ``backend/channels/wechat/channel.py:823`` + ``qr_login.py``):
        # it takes ``cred_path`` (where the scanned session is persisted)
        # plus optional QR/lifecycle callbacks — NOT a ``token=`` kwarg.
        # We persist credentials per instance under the configured creds
        # dir and let the SDK reuse a prior scanned session.  Fall back to
        # the legacy ``token=`` shape only for older SDKs / test fakes that
        # still accept it, so injected stub bots keep working.
        import inspect

        try:
            params = inspect.signature(bot_cls).parameters
        except (TypeError, ValueError):
            params = {}
        if "cred_path" in params:
            cred_path = self._cred_path_for(instance)
            kwargs: dict[str, Any] = {"cred_path": cred_path}
            if "base_url" in params and credentials:
                # Some deployments stash a base_url override in the
                # credential blob; only pass it when the SDK accepts it.
                pass
            bot = bot_cls(**kwargs)
        elif "token" in params:
            bot = bot_cls(token=credentials)
        else:
            bot = bot_cls()
        self._wire_callbacks(bot, instance)
        self._spawn_receive_loop(bot, instance)
        return bot

    def _spawn_receive_loop(
        self, bot: Any, instance: ChannelInstance, *, skip_login: bool = False
    ) -> None:
        """Bring the bot's inbound long-poll receive loop up (V1 parity).

        V1/v0.5 parity (``backend/channels/wechat/channel.py:735-782``
        ``_login_and_poll`` / ``_run_poll``): the personal-WeChat
        ``WeChatBot`` requires BOTH ``login()`` (QR / credential reuse) AND
        ``start()`` (the *blocking* long-poll receive loop) — the two run
        **in sequence**, not as an either/or.  v0.5 ran them in a background
        task (``asyncio.create_task(_run_poll())``) so the API call returned
        promptly while the bot kept polling for messages.

        The prior V2 code treated login/start as mutually exclusive: a real
        ``WeChatBot`` (which exposes ``login``) only had ``login()`` called and
        ``start()`` lived in an unreachable ``else`` branch — so the receive
        loop never started, ``on_message`` never fired, and inbound WeChat
        messages were never received (the "微信发消息无反应" bug).

        ``skip_login=True`` is the Method-A adopted-bot path: the bot handed in
        by the QR-login adapter is ALREADY logged in (it scanned the QR), so we
        must NOT call ``login()`` again (a second login pops a fresh QR — the
        "反复弹码" bug).  We only drive the blocking ``start()`` receive loop on
        that same bot (V1 single-bot parity).

        Called for the adopted-bot path, the production SDK path, and the
        injected ``bot_factory`` (test) path so the receive loop is driven
        identically.
        """
        login_fn = None if skip_login else getattr(bot, "login", None)
        start_fn = getattr(bot, "start", None) or getattr(bot, "run", None)
        # Supersede any prior login→poll task (e.g. a watchdog ``restart`` ->
        # ``start`` cycle) so a repeated start does not leak the previous
        # fire-and-forget task.  We cancel but do not await here — start() must
        # return promptly; the old task tears down in the background.
        prev_poll = self._poll_task
        if prev_poll is not None and not prev_poll.done():
            prev_poll.cancel()
        self._poll_task = None
        if login_fn is not None:
            # login → start in ONE background task so ``start()``'s blocking
            # receive loop does not stall the caller (which must return so
            # ``WechatLongPollTransport.start`` / the lifecycle use case can
            # finish bringing the instance to ``running``).
            self._poll_task = asyncio.ensure_future(
                self._login_then_poll(
                    bot=bot,
                    login_fn=login_fn,
                    start_fn=start_fn,
                    instance=instance,
                )
            )
        elif start_fn is not None:
            # Adopted-bot path (skip_login) OR a token-SDK / test fake exposing
            # only start/run: drive the receive loop directly.  If it is a
            # coroutine it is the blocking poll loop → background task (don't
            # await, see above); a synchronous start_fn returns immediately.
            #
            # We route an adopted (already-logged-in) bot through
            # ``_login_then_poll`` with ``login_fn=None`` so the SAME calm
            # AuthError / error handling and CancelledError propagation apply to
            # its blocking ``start()`` loop.
            self._poll_task = asyncio.ensure_future(
                self._login_then_poll(
                    bot=bot,
                    login_fn=None,
                    start_fn=start_fn,
                    instance=instance,
                )
            )
        # else: neither login nor start (no-op stub bot) → nothing to spawn.

    async def _login_then_poll(
        self,
        *,
        bot: Any,
        login_fn: Any,
        start_fn: Any,
        instance: ChannelInstance,
    ) -> None:
        """Login (unless adopted) then run the blocking long-poll loop.

        Mirrors v0.5 ``_login_and_poll`` + ``_run_poll``
        (``backend/channels/wechat/channel.py:735-782``): ``login()`` first
        (reusing the persisted scanned session when present), then ``start()``
        which blocks while delivering inbound messages via the ``on_message``
        callback wired in :meth:`_wire_callbacks`.  Runs inside the
        fire-and-forget task spawned by :meth:`_spawn_receive_loop`; cancelled
        and torn down by :meth:`stop`.

        When ``login_fn`` is ``None`` (Method-A adopted bot) we skip login
        entirely and only run ``start()`` — the adopted bot is already logged
        in (single-bot, V1 parity; no second QR).
        """
        try:
            if login_fn is not None:
                result = login_fn()
                if asyncio.iscoroutine(result):
                    await result
            if start_fn is not None:
                started = start_fn()
                if asyncio.iscoroutine(started):
                    # Blocking long-poll loop — keep it running until stop()
                    # cancels this task (then ``bot.stop()`` unblocks it).
                    await started
        except asyncio.CancelledError:
            # stop() cancelled us — propagate so the awaiter (stop) sees it.
            raise
        except Exception as exc:  # noqa: BLE001
            # An expired/abandoned QR login is an *expected* operator flow —
            # the user simply did not scan in time (the wechatbot SDK raises
            # ``AuthError: QR code expired N times — login aborted``).  Emit a
            # calm INFO line WITHOUT a scary traceback in that case (mirrors
            # the calm handling V1 used for "user didn't scan"); reserve the
            # ERROR + ``exc_info`` for genuinely unexpected long-poll failures
            # so real bugs still surface a stack.
            exc_name = type(exc).__name__
            msg = str(exc)
            is_expected_login_abort = exc_name == "AuthError" or (
                "QR code expired" in msg or "login aborted" in msg
            )
            # The wechatbot SDK does ``json.loads(resp.text())`` unconditionally
            # (``wechatbot/protocol.py:_parse_response``), so when the upstream
            # (``ilinkai.weixin.qq.com``) returns a NON-JSON body — an HTML
            # error/interception page — it surfaces as
            # ``JSONDecodeError: Expecting value: line 1 column 1 (char 0)``
            # instead of a clean error. The overwhelmingly common cause is the
            # request NOT going through the corporate proxy (direct-connect →
            # gateway ``403 Forbidden`` HTML page, ``X-Direct-Response: true``),
            # or the proxy itself returning a block page. Emit a calm, actionable
            # WARNING (no scary traceback) so the operator knows to check the
            # channel proxy config rather than chasing a "crash".
            is_non_json_upstream = exc_name == "JSONDecodeError" or (
                "Expecting value" in msg
                or "line 1 column 1 (char 0)" in msg
            )
            if is_expected_login_abort:
                logger.info(
                    "channels.wechat.longpoll.login_aborted",
                    instance_id=instance.instance_id.value,
                    reason=msg or exc_name,
                    hint=(
                        "QR code was not scanned in time; re-trigger login "
                        "from the Channels page to reconnect"
                    ),
                )
            elif is_non_json_upstream:
                logger.warning(
                    "channels.wechat.longpoll.upstream_non_json",
                    instance_id=instance.instance_id.value,
                    error=msg,
                    exc_type=exc_name,
                    hint=(
                        "WeChat endpoint returned a non-JSON body (usually an "
                        "HTML 403/interception page). The SDK request most "
                        "likely did NOT go through the configured proxy, or the "
                        "proxy returned a block page. Verify the WeChat channel "
                        "proxy settings and that the proxy can reach "
                        "ilinkai.weixin.qq.com, then reconnect."
                    ),
                )
            else:
                logger.error(
                    "channels.wechat.longpoll.login_or_poll_failed",
                    instance_id=instance.instance_id.value,
                    error=msg,
                    exc_info=True,
                )

    def _cred_path_for(self, instance: ChannelInstance) -> str:
        """Return the per-instance credential persistence path.

        Mirrors V1 ``_create_bot``'s ``cred_path=data_dir /
        "wechat_creds.json"`` — one creds file per instance so a scanned
        session is reused across restarts.  Uses the configured creds dir
        when available, else a temp-dir fallback (keeps test fakes that
        never read the path working).
        """
        from pathlib import Path

        base = self._creds_dir or Path.cwd() / "data" / "channels" / "wechat"
        base.mkdir(parents=True, exist_ok=True)
        return str(base / f"wechat_creds_{instance.instance_id.value}.json")

    def _wire_callbacks(self, bot: Any, instance: ChannelInstance) -> None:
        """Wire the SDK's incoming-message callback to our queue.

        The wechatbot SDK exposes an ``on_message`` decorator-style
        registration; we register a callback that converts each raw
        event into a :class:`ChannelMessage` and threadsafely pushes
        it onto the asyncio queue.
        """
        loop = self._loop

        def _callback(raw: Any) -> None:
            if loop is None or loop.is_closed():
                return
            try:
                channel_msg = self._raw_to_channel_message(raw, instance)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.wechat.longpoll.bad_event",
                    instance_id=instance.instance_id.value,
                    error=str(exc),
                )
                return
            # 4-M14 — drop a re-delivered message before it hits the queue
            # (V2 bounded message-id dedup, mirroring Feishu; §3.7).
            if _dedup_check_and_add(
                self._seen_message_ids, channel_msg.provider_event_id
            ):
                logger.debug(
                    "channels.wechat.longpoll.duplicate_dropped",
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
                    "channels.wechat.longpoll.enqueue_failed",
                    instance_id=instance.instance_id.value,
                    error=str(exc),
                )

        # Try common SDK callback registration shapes.
        for attr in ("on_message", "set_message_handler", "register"):
            register = getattr(bot, attr, None)
            if callable(register):
                try:
                    register(_callback)
                except TypeError:
                    # Decorator-style: register expects a function arg
                    register("message")(_callback)
                return

    def _raw_to_channel_message(
        self, raw: Any, instance: ChannelInstance
    ) -> ChannelMessage:
        """Convert a raw SDK event to a :class:`ChannelMessage`.

        SDK events expose either dict-style access or attribute
        access; we duck-type both so different SDK versions / fakes
        all work.
        """
        def _get(name: str, default: str = "") -> str:
            if isinstance(raw, dict):
                value = raw.get(name, default)
            else:
                value = getattr(raw, name, default)
            return str(value or default)

        text = _get("text") or _get("content")
        sender = _get("user_id") or _get("from_user") or _get("openid")
        provider_event = (
            _get("msg_id") or _get("message_id") or _get("event_id")
        )
        if not provider_event:
            # Synthesise from timestamp+sender so duplicate detection
            # still works even when the SDK omits a stable id.
            provider_event = f"wechat-{sender}-{int(_utcnow().timestamp() * 1000)}"

        clock = self._clock
        now = (
            clock.now() if clock is not None and hasattr(clock, "now") else _utcnow()
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
            sender=ChannelUserId(sender or "unknown"),
            provider_event_id=provider_event,
            content=MessageContent(text=text),
            arrived_at=now,
        )
