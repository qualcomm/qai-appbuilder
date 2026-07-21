# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Channel transport adapters — one per :class:`ChannelKind` (PR-047 + PR-097).

Each :class:`ChannelTransportPort` implementation here wraps the
provider's outbound API for sending messages back to the user who
triggered an inbound webhook.  All transports share the same shape so
:func:`apps.api._channels_di.build_channels_services` can register them
in a single :class:`ChannelKind`-keyed dict.

PR-097 (S9-channels-deep-parity-audit §7.1 — WeChat 仅支持个人号)
---------------------------------------------------------------
The legacy 公众号 ``cgi-bin/message/custom/send`` outbound path has
been removed.  Personal WeChat — the only model the project supports —
delivers outbound replies through the live ``wechatbot`` SDK ``Bot``
instance held by :class:`WechatLongPollTransport`; the new
:class:`WechatTransport` (audit row R-1) is a thin delegate that calls
``_bot.send`` / ``_bot.reply`` / ``_bot.send_typing`` on that bot.

Why two classes instead of one parametrised class
---------------------------------------------------
Each provider's outbound contract differs in ways that bleed into the
implementation (header names, JSON body shape, base URL — for personal
WeChat the entire SDK delegate path).  A single class with branches
would re-introduce the SCC the original refactor split apart.
Small classes with a shared protocol contract is cleaner.

State is on the instance
------------------------
* ``base_url`` — provider's outbound endpoint root (Feishu
  only; personal WeChat does not use HTTPS endpoints directly).
* ``http_timeout_seconds`` — per-request budget.
* ``_started`` — instance ids currently considered live by this
  transport (in-memory; persistence belongs to ChannelInstance.status).
* ``_send_counter`` — monotonic counter for synthesising deterministic
  outbound message ids when the provider response omits one.

There are no module-level globals and no monkey-patches of httpx /
aiohttp — fixing the legacy
``aiohttp.ClientSession`` request-method patching design.

PR-097 — Feishu hardening (audit §2 R-2 / R-14 / R-15)
------------------------------------------------------
:class:`FeishuTransport` now uses :class:`FeishuTenantTokenCache` for
the ``Authorization: Bearer <tenant_access_token>`` header (replacing
the PR-047 outbound-session placeholder), retries upstream calls on
the token-expiry codes ``99991663`` / ``99991664`` (with cache
invalidation) and on transient 429 / 5xx responses with exponential
backoff (0.5s → 1s → 2s, max 3 attempts), and exposes
:meth:`FeishuTransport.send_rich_text` for the Feishu post / rich-text
message type used by the CC progress aggregator.
"""

from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.platform.errors import ExternalServiceError
from qai.platform.logging import get_logger

from qai.channels.domain import (
    ChannelHealth,
    ChannelHealthReport,
    ChannelInstance,
    ChannelKind,
    ChannelUserId,
    MessageContent,
    RichTextContent,
)

#: Feishu error codes meaning "the tenant_access_token you sent is no
#: longer valid".  Duplicated from
#: :mod:`qai.channels.adapters.feishu_tenant_token_cache` so the
#: infrastructure layer does not import the adapters layer (the
#: ``layered-channels`` import-linter contract puts ``adapters`` above
#: ``infrastructure``).  Kept in sync via the test that asserts the
#: two constants compare equal.
FEISHU_TOKEN_EXPIRED_CODES: frozenset[int] = frozenset({99991663, 99991664})

if TYPE_CHECKING:  # pragma: no cover
    import httpx
    # NOTE: ``FeishuTenantTokenCache`` lives in
    # :mod:`qai.channels.adapters.feishu_tenant_token_cache` and is
    # used purely as a forward-reference string in method signatures
    # below.  Importing it here would violate the ``layered-channels``
    # import-linter contract (adapters > infrastructure).  String
    # forward references resolve correctly at static-typing time
    # without an actual import.

__all__ = [
    "WechatTransport",
    "FeishuTransport",
]

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SECONDS: float = 10.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _BaseHttpTransport:
    """Shared plumbing for the three provider transports.

    Each subclass picks up the kind-specific URL / header / body shape
    inside :meth:`send` while the lifecycle methods (``start`` /
    ``stop`` / ``health``) stay identical across providers.
    """

    KIND: ChannelKind  # set by subclasses

    __slots__ = (
        "_client_factory",
        "_base_url",
        "_timeout",
        "_started",
        "_send_counter",
        "_last_error",
    )

    def __init__(
        self,
        *,
        client_factory: "Any",
        base_url: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._client_factory = client_factory
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._started: set[str] = set()
        self._send_counter: int = 0
        self._last_error: str = ""

    # ------------------------------------------------------------------
    # Observability — kept compatible with the PR-036 test surface.
    # ``started_instances`` is the canonical name used by route-level
    # integration tests (see ``test_transport_dispatch_picks_right_kind``)
    # so we expose ``_started`` under that name.  ``_started`` remains
    # the implementation field; external readers should use the
    # property below.
    # ------------------------------------------------------------------
    @property
    def started_instances(self) -> set[str]:
        return self._started

    @property
    def send_counter(self) -> int:
        return self._send_counter

    # ------------------------------------------------------------------
    # Lifecycle
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
                f"transport for {self.KIND.value} requires credentials",
                service=self.KIND.value,
            )
        # Real providers would establish a long-poll / websocket here;
        # for outbound-HTTPS-only adapters (e.g. Feishu official bots)
        # start() is purely a state mark.
        self._started.add(instance.instance_id.value)
        self._last_error = ""

    async def stop(self, instance: ChannelInstance) -> None:
        if instance.kind is not self.KIND:
            raise AssertionError(
                f"transport bound to {self.KIND!r} got {instance.kind!r}"
            )
        self._started.discard(instance.instance_id.value)

    async def health(
        self, instance: ChannelInstance
    ) -> ChannelHealthReport:
        if self._last_error:
            return ChannelHealthReport(
                status=ChannelHealth.DEGRADED,
                detail=self._last_error,
                checked_at=_utcnow(),
            )
        if instance.instance_id.value in self._started:
            return ChannelHealthReport(
                status=ChannelHealth.HEALTHY,
                detail=f"{self.KIND.value} transport running",
                checked_at=_utcnow(),
            )
        return ChannelHealthReport(
            status=ChannelHealth.DOWN,
            detail=f"{self.KIND.value} transport not started",
            checked_at=_utcnow(),
        )

    # ------------------------------------------------------------------
    # Helpers (used by subclasses)
    # ------------------------------------------------------------------
    async def _post_json(
        self,
        path: str,
        json_body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST a JSON body and return the parsed response.

        Wraps :class:`httpx.AsyncClient` so subclasses don't import
        httpx directly; raises :class:`IntegrationError` on transport
        / HTTP failures so callers can translate to per-instance
        ``error`` state.
        """
        client_factory = self._client_factory
        url = f"{self._base_url}{path}"
        try:
            async with client_factory(timeout=self._timeout) as client:
                response = await client.post(
                    url, json=json_body, headers=headers or {}
                )
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"transport_error: {exc}"
            raise ExternalServiceError(
                "channels.transport.http_error",
                f"{self.KIND.value} transport HTTP error: {exc}",
                service=self.KIND.value,
                cause=exc,
            ) from exc
        if response.status_code >= 400:
            body_preview = response.text[:512]
            self._last_error = (
                f"http {response.status_code}: {body_preview}"
            )
            raise ExternalServiceError(
                "channels.transport.http_status",
                f"{self.KIND.value} transport got HTTP "
                f"{response.status_code}: {body_preview}",
                service=self.KIND.value,
                status=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "channels.transport.invalid_json",
                f"{self.KIND.value} transport returned non-JSON body",
                service=self.KIND.value,
                cause=exc,
            ) from exc
        if not isinstance(data, dict):
            raise ExternalServiceError(
                "channels.transport.invalid_envelope",
                f"{self.KIND.value} response envelope must be JSON object",
                service=self.KIND.value,
            )
        return data

    def _next_outbound_id(self, fallback_prefix: str) -> str:
        self._send_counter += 1
        return f"{fallback_prefix}-{self._send_counter}"


# ---------------------------------------------------------------------------
# WeChat (personal-account / wechatbot SDK delegate — PR-097 R-1)
# ---------------------------------------------------------------------------
class WechatTransport(_BaseHttpTransport):
    """ChannelTransport for personal WeChat — delegates to the wechatbot SDK.

    The legacy 公众号 ``cgi-bin/message/custom/send`` HTTPS path was
    removed in PR-097 (audit §7.1 — the project supports only the
    personal-account model).  This transport now holds a reference to
    the live :class:`WechatLongPollTransport` instance — the long-poll
    inbound transport that owns the wechatbot SDK ``Bot`` — and pushes
    outbound text through ``_bot.send`` / ``_bot.reply`` /
    ``_bot.send_typing`` so a single SDK session handles both
    directions, mirroring the legacy
    ``backend/channels/wechat/channel.py:446-497 send_to_user`` /
    ``_handle_message`` design.

    Why share the bot instance
    --------------------------
    The wechatbot SDK requires that outbound ``send`` / ``reply``
    calls run on the same authenticated session as the inbound
    long-poll loop — there is no stateless HTTPS POST surface for
    personal WeChat.  Holding a reference to the inbound transport
    is therefore not optional; it is the only correct wiring.

    Three exposed operations
    ------------------------
    * :meth:`send` — fire-and-forget out-of-context send to a user
      (used by :class:`PushChannelMessageUseCase`).
    * :meth:`reply_in_context` — reply within the inbound message's
      context (matches old ``await _bot.reply(msg, text)``).
    * :meth:`send_typing` — surface the typing-indicator the SDK
      exposes; consumed by the realtime delivery service.
    """

    KIND = ChannelKind.WECHAT

    # PR-097 R-1: kept for symmetry with FeishuTransport
    # (the DI builder reads ``DEFAULT_BASE_URL`` to construct each
    # transport with a stable base URL).  For personal WeChat the value
    # is unused at runtime — outbound delivery flows through the SDK
    # bot, not an HTTPS endpoint — but we keep the constant so the DI
    # construction signature stays uniform.
    DEFAULT_BASE_URL = ""
    SEND_PATH = ""

    __slots__ = ("_longpoll_transport",)

    def __init__(
        self,
        *,
        longpoll_transport: "Any",
        client_factory: "Any" = None,
        base_url: str = "",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # ``client_factory`` / ``base_url`` are accepted for DI symmetry
        # with the other transports but never used: personal WeChat does
        # not call HTTP endpoints from this class.  Passing ``None``
        # is allowed so the builder does not have to construct an
        # httpx factory just for this kind.
        super().__init__(
            client_factory=client_factory,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        self._longpoll_transport = longpoll_transport

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_bot(self) -> "Any":
        """Return the live SDK bot or raise an operator-facing error.

        The bot is owned by :class:`WechatLongPollTransport`; if the
        long-poll loop has not started (or has been torn down) we
        cannot deliver an outbound message and must surface a clear
        error rather than silently dropping the send.
        """
        bot = getattr(self._longpoll_transport, "_bot", None)
        if bot is None:
            raise ExternalServiceError(
                "channels.transport.bot_not_running",
                "personal WeChat outbound transport requires the "
                "long-poll bot to be running; start the channel "
                "instance first",
                service=self.KIND.value,
            )
        return bot

    # ------------------------------------------------------------------
    # ChannelTransportPort
    # ------------------------------------------------------------------
    async def send(
        self,
        instance: ChannelInstance,
        target: ChannelUserId,
        content: MessageContent,
    ) -> str:
        if instance.kind is not self.KIND:
            raise AssertionError(
                f"transport bound to {self.KIND!r} got {instance.kind!r}"
            )
        bot = self._resolve_bot()
        try:
            result = bot.send(target.value, content.text)
            import asyncio as _asyncio

            if _asyncio.iscoroutine(result):
                await result
        except ExternalServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"send_failed: {exc}"
            # L-1 — semantic degradation for the wechatbot "no context token"
            # case (V1 ``channel.py:481-493``). Personal WeChat's iLink
            # protocol only lets us push to a user who has messaged us first
            # (the SDK raises a ``NoContextError``); surface a clear,
            # actionable Chinese message instead of an opaque send failure so
            # the operator understands the push didn't fail for a transient
            # reason — the recipient must send a message first.
            err_name = type(exc).__name__
            if "NoContextError" in err_name or "context" in str(exc).lower():
                raise ExternalServiceError(
                    "channels.transport.no_context",
                    "无法主动推送：该微信用户尚未与机器人交互过，"
                    "需对方先发送一条消息后才能主动推送（iLink 协议限制）。",
                    service=self.KIND.value,
                    cause=exc,
                ) from exc
            raise ExternalServiceError(
                "channels.transport.send_failed",
                f"personal WeChat send failed: {exc}",
                service=self.KIND.value,
                cause=exc,
            ) from exc
        return self._next_outbound_id("wechat-personal")

    async def reply_in_context(
        self, msg_handle: "Any", text: str
    ) -> str:
        """Reply *within* an inbound message's SDK context.

        ``msg_handle`` is the raw ``IncomingMessage`` SDK object the
        long-poll callback received; ``_bot.reply(msg_handle, text)``
        is the legacy
        ``backend/channels/wechat/channel.py:1493 await _bot.reply``
        shape.  Using ``reply`` instead of ``send`` lets the SDK
        thread the response back through the original conversation
        context (so the message appears as a reply rather than a
        cold push).

        Returns the synthesised outbound id for audit symmetry with
        :meth:`send`.
        """
        bot = self._resolve_bot()
        try:
            result = bot.reply(msg_handle, text)
            import asyncio as _asyncio

            if _asyncio.iscoroutine(result):
                await result
        except ExternalServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"reply_failed: {exc}"
            raise ExternalServiceError(
                "channels.transport.reply_failed",
                f"personal WeChat reply failed: {exc}",
                service=self.KIND.value,
                cause=exc,
            ) from exc
        return self._next_outbound_id("wechat-personal")

    async def send_typing(self, target: ChannelUserId) -> None:
        """Surface the SDK typing indicator (legacy
        ``backend/channels/wechat/channel.py:1881 _bot.send_typing``)."""
        bot = self._resolve_bot()
        try:
            result = bot.send_typing(target.value)
            import asyncio as _asyncio

            if _asyncio.iscoroutine(result):
                await result
        except ExternalServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Typing failures are non-fatal — the user just won't see
            # the indicator.  Record the error for the health report
            # so operators can detect a chronically broken SDK.
            self._last_error = f"typing_failed: {exc}"
            logger.warning(
                "channels.wechat.send_typing_failed",
                instance=getattr(target, "value", ""),
                error=str(exc),
            )

    async def download(self, msg_handle: "Any") -> bytes:
        """Download the binary payload (image / voice) of an inbound message.

        Mirrors the legacy
        ``backend/channels/wechat/channel.py`` ``await _bot.download(msg)``
        path exposed by ``_build_image_content``.  Surfaces a clear
        operator-facing error when the bot is not running.
        """
        bot = self._resolve_bot()
        try:
            result = bot.download(msg_handle)
            import asyncio as _asyncio

            if _asyncio.iscoroutine(result):
                return await result
            if isinstance(result, (bytes, bytearray)):
                return bytes(result)
            raise ExternalServiceError(
                "channels.transport.download_invalid",
                "personal WeChat download returned non-bytes payload",
                service=self.KIND.value,
            )
        except ExternalServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"download_failed: {exc}"
            raise ExternalServiceError(
                "channels.transport.download_failed",
                f"personal WeChat download failed: {exc}",
                service=self.KIND.value,
                cause=exc,
            ) from exc


# ---------------------------------------------------------------------------
# Feishu (Lark) — PR-097 R-2 / R-14 / R-15
# ---------------------------------------------------------------------------
class FeishuTransport(_BaseHttpTransport):
    """ChannelTransport for Feishu / Lark Open Platform bots.

    Endpoint: ``POST /open-apis/im/v1/messages?receive_id_type=open_id``
    Authorisation: ``Bearer <tenant_access_token>`` in ``Authorization``
    header — the bearer is sourced from
    :class:`FeishuTenantTokenCache` (PR-097 R-2).  Body shape::

        {"receive_id": "<open_id>", "msg_type": "text",
         "content": "{\\"text\\":\\"...\\"}"}

    Note that ``content`` is a JSON-encoded *string* (Feishu's
    encoding-of-encoding wire format), not a nested object.

    Retry behaviour (PR-097 R-15)
    -----------------------------
    Outbound calls retry up to 3 times with exponential backoff
    (0.5s / 1s / 2s) on:

    * Body codes ``99991663`` / ``99991664`` — invalidate the token
      cache and refresh once before the retry.
    * HTTP 429 / 5xx — pure backoff, no cache invalidation.

    After retries are exhausted the transport raises
    :class:`ExternalServiceError` with code
    ``channels.feishu.token_refresh_failed`` (token codes) or
    ``channels.transport.http_status`` (transient HTTP errors).

    Rich text (PR-097 R-14)
    -----------------------
    :meth:`send_rich_text` posts a ``msg_type="post"`` message
    consumed by the CC progress aggregator for icon-rich progress
    lines.  When :attr:`MessageContent.rich_text` is set the
    dispatcher routes :meth:`send` to this method automatically.
    """

    KIND = ChannelKind.FEISHU

    DEFAULT_BASE_URL = "https://open.feishu.cn"
    SEND_PATH = "/open-apis/im/v1/messages"

    # Backoff schedule for retries — sequenced so the wall-clock
    # ceiling stays under 4 seconds even at the worst case.
    _RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0)

    __slots__ = ("_tenant_token_cache", "_token_cache_factory")

    def __init__(
        self,
        *,
        client_factory: "Any",
        base_url: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        tenant_token_cache: "FeishuTenantTokenCache | None" = None,
        token_cache_factory: (
            "Any | None"
        ) = None,
    ) -> None:
        super().__init__(
            client_factory=client_factory,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        # ``tenant_token_cache`` — eagerly-supplied single cache, used
        # in tests + single-instance deployments where the DI builder
        # already has app_id / app_secret in hand.
        # ``token_cache_factory`` — ``Callable[[ChannelInstance],
        # FeishuTenantTokenCache]`` resolving the cache lazily from
        # per-instance credentials (the production wiring used by
        # apps.api._channels_di).  At least one of the two must be
        # supplied before :meth:`send` is called.
        self._tenant_token_cache = tenant_token_cache
        self._token_cache_factory = token_cache_factory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def send(
        self,
        instance: ChannelInstance,
        target: ChannelUserId,
        content: MessageContent,
    ) -> str:
        if instance.kind is not self.KIND:
            raise AssertionError(
                f"transport bound to {self.KIND!r} got {instance.kind!r}"
            )
        # When the message carries a rich-text payload the dispatcher
        # routes through :meth:`send_rich_text` automatically.  Plain
        # text is the common path.
        if content.rich_text is not None:
            return await self.send_rich_text(
                instance,
                target,
                title=content.rich_text.title,
                content_lines=_segments_to_post_lines(content.rich_text),
            )

        body = {
            "receive_id": target.value,
            "msg_type": "text",
            # Feishu requires content to be a JSON-encoded string.
            "content": _json.dumps(
                {"text": content.text}, ensure_ascii=False
            ),
        }
        response = await self._post_with_retry(
            instance,
            f"{self.SEND_PATH}?receive_id_type=open_id",
            body,
        )
        return self._extract_outbound_id(response)

    async def send_to_chat(
        self,
        instance: ChannelInstance,
        chat_id: str,
        content: MessageContent,
    ) -> str:
        """Send a message to a Feishu group chat (receive_id_type=chat_id).

        Used when the inbound message came from a group chat so the
        reply goes back to the group rather than the individual sender
        (Zagent parity: ``send_to_chat`` in feishu/channel.py).
        """
        if instance.kind is not self.KIND:
            raise AssertionError(
                f"transport bound to {self.KIND!r} got {instance.kind!r}"
            )
        if content.rich_text is not None:
            post_content = {
                "zh_cn": {
                    "title": content.rich_text.title,
                    "content": _segments_to_post_lines(content.rich_text)
                    or [[{"tag": "text", "text": ""}]],
                }
            }
            body = {
                "receive_id": chat_id,
                "msg_type": "post",
                "content": _json.dumps(post_content, ensure_ascii=False),
            }
        else:
            body = {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": _json.dumps(
                    {"text": content.text}, ensure_ascii=False
                ),
            }
        response = await self._post_with_retry(
            instance,
            f"{self.SEND_PATH}?receive_id_type=chat_id",
            body,
        )
        return self._extract_outbound_id(response)

    async def send_rich_text(
        self,
        instance: ChannelInstance,
        target: ChannelUserId,
        *,
        title: str,
        content_lines: list[list[dict[str, Any]]],
    ) -> str:
        """Send a Feishu post-format rich-text message (PR-097 R-14).

        ``msg_type=post`` body shape::

            {
              "receive_id": "<open_id>",
              "msg_type": "post",
              "content": "{\\"zh_cn\\": {
                  \\"title\\": ...,
                  \\"content\\": <content_lines>}}"
            }

        ``content_lines`` is the post's nested ``content`` array — a
        list of lines, each line a list of segment dicts such as
        ``{"tag": "text", "text": "..."}``.  Mirrors the legacy
        ``backend/channels/feishu/channel.py:1616-1666`` behaviour.
        """
        if instance.kind is not self.KIND:
            raise AssertionError(
                f"transport bound to {self.KIND!r} got {instance.kind!r}"
            )
        post_content = {
            "zh_cn": {
                "title": title,
                "content": content_lines or [[{"tag": "text", "text": ""}]],
            }
        }
        body = {
            "receive_id": target.value,
            "msg_type": "post",
            "content": _json.dumps(post_content, ensure_ascii=False),
        }
        response = await self._post_with_retry(
            instance,
            f"{self.SEND_PATH}?receive_id_type=open_id",
            body,
        )
        return self._extract_outbound_id(response)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_token_cache(
        self, instance: ChannelInstance
    ) -> "FeishuTenantTokenCache":
        """Pick the right :class:`FeishuTenantTokenCache` for ``instance``.

        Prefers the per-instance factory wiring (production); falls
        back to the eagerly-supplied single cache when only that one
        was provided at construction time (tests / single-instance
        deployments).
        """
        if self._token_cache_factory is not None:
            return self._token_cache_factory(instance)
        if self._tenant_token_cache is not None:
            return self._tenant_token_cache
        raise ExternalServiceError(
            "channels.feishu.token_refresh_failed",
            "FeishuTransport requires a tenant_token_cache or "
            "token_cache_factory; construct one in the DI builder "
            "before sending",
            service=self.KIND.value,
        )

    async def _resolve_authorization(
        self, instance: ChannelInstance
    ) -> dict[str, str]:
        """Build the ``Authorization`` header from the token cache."""
        cache = self._resolve_token_cache(instance)
        token = await cache.get_token()
        return {"Authorization": f"Bearer {token}"}

    async def _post_with_retry(
        self,
        instance: ChannelInstance,
        path: str,
        json_body: dict[str, Any],
    ) -> dict[str, Any]:
        """POST to Feishu with token-aware + backoff-aware retries.

        Retries (max 3 attempts total) on:

        * Body ``code`` in :data:`FEISHU_TOKEN_EXPIRED_CODES` →
          invalidate the cache, refresh, retry.
        * HTTP 429 / 5xx → pure backoff retry.

        Other HTTP / body errors raise immediately.
        """
        last_exc: Exception | None = None
        max_attempts = len(self._RETRY_BACKOFF_SECONDS) + 1
        cache = self._resolve_token_cache(instance)
        for attempt in range(max_attempts):
            headers = await self._resolve_authorization(instance)
            try:
                response = await self._post_json_inspectable(
                    path, json_body, headers=headers
                )
            except _TransientHttpStatusError as transient:
                last_exc = transient.cause
                if attempt + 1 >= max_attempts:
                    raise transient.cause from None
                await asyncio.sleep(
                    self._RETRY_BACKOFF_SECONDS[attempt]
                )
                continue

            code = response.get("code")
            if isinstance(code, int) and code in FEISHU_TOKEN_EXPIRED_CODES:
                # Token rejected — invalidate cache and retry up to
                # the per-call ceiling.
                logger.warning(
                    "channels.feishu.send_token_expired",
                    code=code,
                    msg=str(response.get("msg", "")),
                    attempt=attempt + 1,
                )
                cache.invalidate()
                if attempt + 1 >= max_attempts:
                    self._last_error = (
                        f"feishu token rejected after "
                        f"{max_attempts} attempts: code={code}"
                    )
                    raise ExternalServiceError(
                        "channels.feishu.token_refresh_failed",
                        f"feishu rejected outbound call with token "
                        f"code {code} after {max_attempts} attempts",
                        service=self.KIND.value,
                    )
                await asyncio.sleep(
                    self._RETRY_BACKOFF_SECONDS[attempt]
                )
                continue

            if isinstance(code, int) and code != 0:
                # Non-token, non-zero application error — surface
                # immediately; retrying won't help.
                self._last_error = (
                    f"feishu app error code={code}: "
                    f"{response.get('msg', '')}"
                )
                raise ExternalServiceError(
                    "channels.transport.http_status",
                    f"feishu rejected outbound call: code={code} "
                    f"msg={response.get('msg', '')!r}",
                    service=self.KIND.value,
                )

            return response

        # Defensive: loop should always return / raise above, but if
        # control falls through surface the last transient error.
        if last_exc is not None:
            raise last_exc
        raise ExternalServiceError(
            "channels.transport.http_status",
            f"feishu retries exhausted on {path}",
            service=self.KIND.value,
        )

    async def _post_json_inspectable(
        self,
        path: str,
        json_body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Like :meth:`_post_json` but raises a private transient marker
        on HTTP 429 / 5xx so the retry loop can decide whether to back
        off or surface the error."""
        client_factory = self._client_factory
        url = f"{self._base_url}{path}"
        try:
            async with client_factory(timeout=self._timeout) as client:
                response = await client.post(
                    url, json=json_body, headers=headers or {}
                )
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"transport_error: {exc}"
            wrapped = ExternalServiceError(
                "channels.transport.http_error",
                f"{self.KIND.value} transport HTTP error: {exc}",
                service=self.KIND.value,
                cause=exc,
            )
            raise _TransientHttpStatusError(wrapped) from exc

        status = response.status_code
        if status == 429 or 500 <= status < 600:
            body_preview = response.text[:512]
            self._last_error = f"http {status}: {body_preview}"
            wrapped = ExternalServiceError(
                "channels.transport.http_status",
                f"{self.KIND.value} transport got HTTP {status}: "
                f"{body_preview}",
                service=self.KIND.value,
                status=status,
            )
            raise _TransientHttpStatusError(wrapped)

        if status >= 400:
            body_preview = response.text[:512]
            self._last_error = f"http {status}: {body_preview}"
            raise ExternalServiceError(
                "channels.transport.http_status",
                f"{self.KIND.value} transport got HTTP {status}: "
                f"{body_preview}",
                service=self.KIND.value,
                status=status,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "channels.transport.invalid_json",
                f"{self.KIND.value} transport returned non-JSON body",
                service=self.KIND.value,
                cause=exc,
            ) from exc
        if not isinstance(data, dict):
            raise ExternalServiceError(
                "channels.transport.invalid_envelope",
                f"{self.KIND.value} response envelope must be JSON object",
                service=self.KIND.value,
            )
        return data

    def _extract_outbound_id(
        self, response: dict[str, Any]
    ) -> str:
        # Real Feishu response wraps the message in
        # ``{"data": {"message_id": "om_..."}, ...}``; if the provider
        # returns the id we use it, otherwise we fall back to a
        # synthetic counter.
        data = response.get("data")
        if isinstance(data, dict):
            mid = data.get("message_id")
            if isinstance(mid, str) and mid:
                return mid
        return self._next_outbound_id("feishu-out")


class _TransientHttpStatusError(Exception):
    """Internal marker for HTTP 429 / 5xx so the retry loop can branch.

    The wrapped :class:`ExternalServiceError` is what the caller sees
    when retries are exhausted; the marker exists only inside
    :class:`FeishuTransport`.
    """

    __slots__ = ("cause",)

    def __init__(self, cause: ExternalServiceError) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _segments_to_post_lines(
    rich: RichTextContent,
) -> list[list[dict[str, Any]]]:
    """Project :class:`RichTextContent` into Feishu post wire shape."""
    out: list[list[dict[str, Any]]] = []
    for line in rich.lines:
        line_segments: list[dict[str, Any]] = []
        for seg in line:
            seg_dict: dict[str, Any] = {
                "tag": seg.tag,
                "text": seg.text,
            }
            if seg.href:
                seg_dict["href"] = seg.href
            line_segments.append(seg_dict)
        out.append(line_segments or [{"tag": "text", "text": ""}])
    return out



