# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Abstract ports for the channels bounded context.

Every external collaborator the application layer touches is exposed as
a :class:`typing.Protocol` here.  Adapters (S4 PR-04x) implement these
ports — domain code never depends on them and use cases only see the
Protocol.

Design philosophy:

* **One port, many adapters** for transports — :class:`ChannelTransportPort`
  is invoked with a :class:`ChannelInstance` whose ``kind`` discriminator
  picks the concrete implementation at the composition root.  This is
  the central anti-SCC choice (refactor-plan §8.7 + inventory §5.2):
   the legacy ``feishu/`` / ``wechat/`` parallel packages are
  replaced by *one* application service plus N small adapters.
* **No leakage of channel-kind specifics** into use cases — every kind-
  dispatching port returns a kind-agnostic VO (``WebhookPayload`` /
  ``ChannelHealthReport``).
* **Credentials are never plaintext** in domain / application code:
  :class:`CredentialsResolverPort` is the only seam at which a SecretStore
  read happens, and use cases pass the resolved string straight to the
  transport without persisting it.
* **MessageBridgePort** is the *only* hook into chat / ai_coding —
  channels never imports those contexts (refactor-plan §8.7 +
  ``.importlinter`` ``context-isolation`` contract).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from qai.channels.domain import (
    ChannelHealthReport,
    ChannelInstance,
    ChannelInstanceId,
    ChannelKind,
    ChannelMessage,
    ChannelMessageId,
    ChannelUserId,
    Command,
    CredentialsRef,
    MessageContent,
    MessageReplyRef,
    QrLoginChallenge,
    SessionIndexEntry,
    WebhookPayload,
)


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------
@runtime_checkable
class ChannelInstanceRepositoryPort(Protocol):
    """Persistence port for :class:`ChannelInstance` aggregates.

    Implementations live in S4 PR-04x; PR-026 will design the SQL
    schema. The Protocol is async to match aiosqlite-backed adapters.
    """

    async def save(self, instance: ChannelInstance) -> None:
        """Insert or replace an instance row by its ``instance_id``."""
        ...

    async def get(self, instance_id: ChannelInstanceId) -> ChannelInstance:
        """Return the instance.

        Raises:
            ChannelInstanceNotFoundError: if no record exists.
        """
        ...

    async def find(
        self, instance_id: ChannelInstanceId
    ) -> ChannelInstance | None:
        """Return the instance, or ``None`` if not found (no raise)."""
        ...

    async def list_by_kind(
        self, kind: ChannelKind
    ) -> tuple[ChannelInstance, ...]:
        """Return all instances for a given :class:`ChannelKind`."""
        ...

    async def delete(self, instance_id: ChannelInstanceId) -> None:
        """Remove an instance.

        Raises:
            ChannelInstanceNotFoundError: if no record exists.
        """
        ...


@runtime_checkable
class ChannelMessageRepositoryPort(Protocol):
    """Persistence port for :class:`ChannelMessage` entities."""

    async def save(self, message: ChannelMessage) -> None:
        ...

    async def get(self, message_id: ChannelMessageId) -> ChannelMessage:
        """Raises ChannelMessageNotFoundError when absent."""
        ...

    async def find_by_provider_event_id(
        self, kind: ChannelKind, provider_event_id: str
    ) -> ChannelMessage | None:
        """Used for inbound idempotency (de-dup on retry)."""
        ...


@runtime_checkable
class SessionIndexRepositoryPort(Protocol):
    """Persistence port for :class:`SessionIndex` entries.

    Replaces the legacy module-level ``_user_cc_sessions`` dict.
    """

    async def upsert(self, entry: SessionIndexEntry) -> None:
        ...

    async def find(
        self,
        instance_id: ChannelInstanceId,
        channel_user_id: ChannelUserId,
    ) -> SessionIndexEntry | None:
        ...

    async def list_for_instance(
        self, instance_id: ChannelInstanceId
    ) -> tuple[SessionIndexEntry, ...]:
        ...

    async def delete(
        self,
        instance_id: ChannelInstanceId,
        channel_user_id: ChannelUserId,
    ) -> None:
        """Raises SessionIndexEntryNotFoundError when absent."""
        ...


# ---------------------------------------------------------------------------
# Transport / signature / parsing
# ---------------------------------------------------------------------------
@runtime_checkable
class ChannelTransportPort(Protocol):
    """The single transport-side port — one impl per :class:`ChannelKind`.

    ``credentials`` is the *resolved* secret string (already fetched
    from the SecretStore via :class:`CredentialsResolverPort`).  The
    port itself never touches the SecretStore.
    """

    async def start(
        self, instance: ChannelInstance, credentials: str
    ) -> None:
        """Bring the underlying transport up."""
        ...

    async def stop(self, instance: ChannelInstance) -> None:
        """Tear the transport down."""
        ...

    async def send(
        self,
        instance: ChannelInstance,
        target: ChannelUserId,
        content: MessageContent,
    ) -> str:
        """Deliver ``content`` to ``target``.

        Returns the provider-side message id (used for
        :class:`MessageReplyRef`).
        """
        ...

    async def health(
        self, instance: ChannelInstance
    ) -> ChannelHealthReport:
        ...


@runtime_checkable
class WebhookSignatureVerifierPort(Protocol):
    """Verifies the integrity of an inbound webhook body.

    One impl per :class:`ChannelKind` — registered into a
    kind-keyed registry by the composition root; the use case asks the
    registry for the verifier matching the inbound kind.

    PR-201 added the optional ``instance_id`` keyword parameter.
    Implementations that need a per-instance signing secret (the
    production wiring at :mod:`apps.api._channels_di`) read the
    SecretStore record at ``("qai.channels.signing", instance_id)`` and
    construct a fresh per-kind verifier for the call.  Older
    implementations that share one secret for all instances simply
    ignore the kw-arg, preserving §3.1 backwards-compatibility.
    """

    def verify(
        self,
        kind: ChannelKind,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        instance_id: str | None = None,
    ) -> None:
        """Validate the signature.

        Args:
            kind: Provider channel kind matching the verifier.
            raw_body: Raw webhook body bytes (some providers HMAC the
                body, others just sign headers).
            headers: Inbound HTTP headers (already lower-cased by the
                aggregator if needed).
            instance_id: Optional :class:`ChannelInstanceId` value of
                the instance receiving the webhook.  Implementations
                that resolve a per-instance signing secret (PR-201)
                use this; legacy fakes that ignore it remain
                contract-compliant.

        Raises:
            WebhookSignatureInvalidError: if verification fails.
            ChannelKindNotSupportedError: if no verifier is registered
                for ``kind``.
        """
        ...


@runtime_checkable
class WebhookPayloadParserPort(Protocol):
    """Parses a raw webhook body into a :class:`WebhookPayload` VO.

    Like the verifier, one impl per :class:`ChannelKind`.
    """

    def parse(
        self,
        kind: ChannelKind,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        instance_id: str | None = None,
    ) -> WebhookPayload:
        """Return a parsed envelope.

        ``instance_id`` is an additive kw-arg (PR-097) used by parsers
        that need per-instance crypto material — e.g. webhook payload
        decryption / signature verification — to look up the right
        ``EncodingAESKey`` / ``app_id`` / verification token.  Parsers
        that don't need it simply ignore the kw-arg.

        Raises:
            WebhookPayloadInvalidError: if the body is malformed.
            ChannelKindNotSupportedError: if no parser is registered
                for ``kind``.
        """
        ...


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------
@runtime_checkable
class CommandParserPort(Protocol):
    """Translates a :class:`MessageContent` to a structured
    :class:`Command`.

    Returns ``None`` when the message is plain chat (not a command),
    so the dispatcher can route it to an LLM rather than a verb handler.
    Raises :class:`InvalidCommandError` only when the text *looks* like
    a command (e.g. starts with ``/``) but is unparseable.
    """

    def parse(self, content: MessageContent) -> Command | None:
        ...


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
@runtime_checkable
class CredentialsResolverPort(Protocol):
    """Resolves a :class:`CredentialsRef` to a plaintext secret.

    Implementations wrap :class:`qai.platform.persistence.secrets.SecretStore`
    — the channels application layer never imports SecretStore directly,
    so PR-026 / PR-040+ can swap backends without touching channel use
    cases.
    """

    async def resolve(self, ref: CredentialsRef) -> str:
        """Return the plaintext credential.

        Raises:
            CredentialsNotFoundError: if no record exists for ``ref``.
        """
        ...

    async def store(self, ref: CredentialsRef, secret: str) -> None:
        """Persist ``secret`` under ``ref``.  Idempotent (overwrite)."""
        ...


# ---------------------------------------------------------------------------
# QR login
# ---------------------------------------------------------------------------
@runtime_checkable
class QrLoginPort(Protocol):
    """Provider-specific QR-login flow (personal WeChat / cc_handler-style).

    The port surface is intentionally narrow: issue + check + confirm.
    Image-rendering / polling are adapter concerns.
    """

    async def issue(
        self, instance: ChannelInstance
    ) -> QrLoginChallenge:
        ...

    async def check_status(
        self, instance: ChannelInstance, challenge_id: str
    ) -> QrLoginChallenge:
        ...

    async def confirm(
        self, instance: ChannelInstance, challenge_id: str
    ) -> QrLoginChallenge:
        ...


# ---------------------------------------------------------------------------
# Bridge to chat / ai_coding (the *only* outbound seam)
# ---------------------------------------------------------------------------
@runtime_checkable
class MessageBridgePort(Protocol):
    """The single seam between channels and chat / ai_coding.

    Refactor-plan §8.7: the legacy
    ``channels/feishu/channel.py → channels/wechat/cc_handler.py`` direct
    import is replaced by this Protocol.  Use cases dispatch a parsed
    :class:`ChannelMessage` (+ optional :class:`Command`) to the bridge
    and receive an opaque ``BridgeReply`` back.

    Implementations route to the chat conversation service or the
    ai_coding session service in the composition root.

    NOTE for cross-PR coordination: this Protocol intentionally does
    **not** import ``qai.chat.StreamFrame`` — channels speak in their
    own VOs.  S4 (PR-04x) will write a thin translator at the wiring
    layer.  See manifest §"Cross-PR coordination".
    """

    async def deliver(
        self,
        message: ChannelMessage,
        command: Command | None,
    ) -> BridgeReply:
        """Hand a message to the downstream context.

        Raises:
            MessageBridgeUnavailableError: if the bridge cannot accept
                the dispatch (downstream context overloaded / down).
        """
        ...


from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class BridgeReply:
    """Response returned by :meth:`MessageBridgePort.deliver`.

    A simple structured value — not a stream — so channels can decide
    immediately whether to send a single reply or to fork the streaming
    output into multiple channel messages.  Streaming bridges (e.g.
    chat) buffer their tokens into a final reply text and surface them
    here.

    Fields:
    * ``reply_text`` — what to send back to the channel user.  Empty
      string means "the bridge handled it without a textual reply"
      (rare; e.g. a slash-command that only updated state).
    * ``coding_session_id`` — optional pointer to the
      :class:`~qai.ai_coding.domain.CodingSession` that handled the
      command, so callers can update :class:`SessionIndex`.
    """

    reply_text: str
    coding_session_id: str | None = None


# ---------------------------------------------------------------------------
# Inbound transports (S9 PR-093 §2.1 C-8)
# ---------------------------------------------------------------------------
@runtime_checkable
class InboundTransportPort(Protocol):
    """Long-lived transport that *receives* messages from a provider.

    The outbound :class:`ChannelTransportPort` covers HTTPS-only
    "send a reply" flows.  Personal WeChat (wechatbot SDK long-poll)
    and Feishu / Lark Open Platform (lark_oapi WebSocket) require a
    dedicated long-lived inbound channel — this port is the seam.

    Implementations live in
    :mod:`qai.channels.infrastructure.wechat_longpoll`
    and :mod:`qai.channels.infrastructure.feishu_ws`
    (S9 PR-093 §2.1 C-8).  They wrap the third-party SDK's
    callback/poll loop and surface incoming messages as a clean
    :class:`ChannelMessage` async iterator so the application layer
    sees one uniform shape regardless of provider.

    Lifecycle (start → iterate → stop) matches
    :class:`ChannelTransportPort` so a single watchdog
    (:class:`~qai.channels.infrastructure.transport_watchdog.TransportWatchdog`)
    can supervise both directions.

    Reconnection is the *adapter's* responsibility — the watchdog
    only restarts the adapter; the adapter's own SDK-specific retry
    logic (5 / 10 / 30 / 60 / 120s backoff) keeps the iterator alive
    across transient network blips.
    """

    async def start(
        self, instance: ChannelInstance, credentials: str
    ) -> None:
        """Open the inbound connection and begin receiving."""
        ...

    async def stop(self, instance: ChannelInstance) -> None:
        """Close the inbound connection cleanly."""
        ...

    def stream(
        self, instance: ChannelInstance
    ) -> AsyncIterator[ChannelMessage]:
        """Return an async iterator over inbound messages.

        The iterator must terminate cleanly when :meth:`stop` is
        called; transient I/O errors should be handled internally
        and surface as a ``DEGRADED`` health report rather than
        terminating the iterator.
        """
        ...

    def is_alive(self) -> bool:
        """Return ``True`` if the underlying transport is healthy.

        Compatible with
        :class:`~qai.channels.infrastructure.transport_watchdog.WatchableTransport`
        so the watchdog can supervise inbound transports directly.
        """
        ...


# ---------------------------------------------------------------------------
# Reply send helper
# ---------------------------------------------------------------------------
@runtime_checkable
class ReplyDispatcherPort(Protocol):
    """Helper port that bundles
    :class:`ChannelTransportPort.send` + producing a
    :class:`MessageReplyRef`.

    Kept distinct from ChannelTransportPort so we can fake the dispatch
    side independently in tests (transport fakes only need ``start`` /
    ``stop`` / ``send`` / ``health`` shapes).
    """

    async def dispatch(
        self,
        instance: ChannelInstance,
        target: ChannelUserId,
        content: MessageContent,
        in_reply_to: ChannelMessageId,
    ) -> MessageReplyRef:
        ...


__all__ = [
    "ChannelInstanceRepositoryPort",
    "ChannelMessageRepositoryPort",
    "SessionIndexRepositoryPort",
    "ChannelTransportPort",
    "InboundTransportPort",
    "WebhookSignatureVerifierPort",
    "WebhookPayloadParserPort",
    "CommandParserPort",
    "CredentialsResolverPort",
    "QrLoginPort",
    "MessageBridgePort",
    "BridgeReply",
    "ReplyDispatcherPort",
    "PendingMessageStorePort",
]


# ---------------------------------------------------------------------------
# Pending message store (PR-097 / S9 §6 R-20)
# ---------------------------------------------------------------------------
@runtime_checkable
class PendingMessageStorePort(Protocol):
    """Persistence port for the realtime-delivery Layer-3 pending queue.

    PR-097 (S9 §6 R-20) restores parity with the legacy
    ``backend/channels/wechat/channel.py`` ``_pending_cc_results`` map by
    persisting Layer-3 messages so a server restart does not silently
    drop a CC result the user never saw.

    Implementations:
        * :class:`~qai.channels.application.services.realtime_delivery.InMemoryPendingMessageStore`
          for tests / fallback.
        * :class:`~qai.channels.adapters.pending_message_repository.SqlitePendingMessageRepository`
          for production (table ``channel_pending_message`` from
          migration 010).
    """

    async def push(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        message: str,
        expires_at: datetime,
    ) -> None:
        """Insert a pending Layer-3 message for ``(instance, user)``.

        ``expires_at`` carries a tz-aware UTC datetime; rows past
        their expiry are filtered out by :meth:`pop_all` and may be
        garbage-collected by an out-of-band sweep.
        """
        ...

    async def pop_all(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> list[str]:
        """Return all non-expired messages for ``(instance, user)`` and remove them.

        Returned in FIFO order so the dispatch bridge replays the queue
        exactly as it was filled.  Empty list when nothing pending.
        """
        ...

    async def has_pending(
        self,
        *,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> bool:
        """Return ``True`` if at least one non-expired message is queued."""
        ...


@runtime_checkable
class WechatPersonalQrLoginPort(Protocol):
    """Tear-down seam for the live personal-WeChat QR-login adapter.

    Only the ``logout`` method is part of the application-layer contract
    used by :class:`LogoutWechatPersonalUseCase` — the route layer no
    longer reaches into the concrete
    :class:`~qai.channels.adapters.qr_login.WechatPersonalQrLoginAdapter`
    to orchestrate "tear down the SDK bot then stop the channel".  The
    ``trigger_login`` flow stays on the concrete adapter (the login route
    needs its richer signature) and is intentionally absent here.
    """

    async def logout(self) -> None:
        """Tear down the active wechatbot SDK ``Bot`` instance, if any."""
        ...
