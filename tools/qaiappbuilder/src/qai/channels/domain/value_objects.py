# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Value objects for the channels bounded context.

All VOs are frozen dataclasses with ``slots=True``.  They never carry
mutable references — every field is either an immutable primitive,
another VO, an enum, or an ``int``/``str``/``datetime``.

Notable design decisions:

* :class:`WebhookPayload` is a *parsed envelope* — it contains the
  structured fields a use case needs (event-id / sender / verbatim
  text / arrival time) **after** signature verification and provider-
  specific deserialisation.  The raw HTTP body never reaches domain code.
* :class:`Command` represents the result of running a parser over the
  message text.  ``args`` is a ``tuple`` (immutable) rather than a
  ``list`` so the VO stays hashable.
* :class:`ChannelStatus` / :class:`ChannelMessageStatus` are pure
  enums — the *transitions* between them belong on the aggregate
  classes, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
)
from qai.platform.time import ensure_aware_utc

from .ids import ChannelMessageId, ChannelUserId
from .kinds import ChannelKind

_MAX_TEXT_LENGTH = 16_384
_MAX_VERB_LENGTH = 64
_MAX_ARG_LENGTH = 1_024
_MAX_REF_LENGTH = 256


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class ChannelStatus(str, Enum):
    """Lifecycle status of a :class:`ChannelInstance`.

    Transitions (enforced by the aggregate, not by the enum)::

        stopped  -> starting -> running -> stopping -> stopped
                                       \\
                                        -> error  -> stopped
        starting -> error -> stopped
    """

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


class ChannelMessageStatus(str, Enum):
    """Lifecycle status of a :class:`ChannelMessage`.

    Transitions::

        received -> parsed -> dispatched -> replied
        received -> parsed -> dispatched -> failed
        received -> failed
        received -> parsed -> failed
    """

    RECEIVED = "received"
    PARSED = "parsed"
    DISPATCHED = "dispatched"
    REPLIED = "replied"
    FAILED = "failed"


class MessageDirection(str, Enum):
    """Direction of a channel message relative to our system."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ChannelHealth(str, Enum):
    """Coarse health classification reported by transports."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


class QrLoginStatus(str, Enum):
    """Lifecycle of a :class:`QrLoginChallenge`."""

    ISSUED = "issued"
    SCANNED = "scanned"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Content / payload VOs
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ImageAttachment:
    """Inbound image-attachment metadata (PR-097 §2 R-8).

    Carries enough information for the dispatch bridge to fetch the
    raw bytes via the provider's REST API after the parser has run —
    parsers stay synchronous, downloads happen later.

    ``mime_hint`` is best-effort (Feishu's webhook envelope does not
    always carry a content type); the actual MIME type is sniffed
    from magic bytes after download in the kind-specific image
    decoder.
    """

    message_id: str
    image_key: str
    kind: ChannelKind
    mime_hint: str | None = None

    def __post_init__(self) -> None:
        assert_non_empty(
            self.message_id, name="ImageAttachment.message_id"
        )
        assert_max_length(
            self.message_id,
            max_length=_MAX_REF_LENGTH,
            name="ImageAttachment.message_id",
        )
        assert_non_empty(
            self.image_key, name="ImageAttachment.image_key"
        )
        assert_max_length(
            self.image_key,
            max_length=_MAX_REF_LENGTH,
            name="ImageAttachment.image_key",
        )
        if self.mime_hint is not None:
            assert_max_length(
                self.mime_hint,
                max_length=_MAX_REF_LENGTH,
                name="ImageAttachment.mime_hint",
            )


@dataclass(frozen=True, slots=True)
class RichTextSegment:
    """One inline segment of a Feishu post-message line (PR-097 §2 R-14).

    Mirrors the Feishu ``post`` content schema where a line is an
    ordered list of segments such as
    ``{"tag": "text", "text": "hello"}`` or
    ``{"tag": "a", "text": "click", "href": "..."}``.

    The VO is provider-shape-shaped on purpose: it is the smallest
    common denominator across Feishu post / WeChat text / WeChat
    markdown, and the dispatcher renders it per-kind.  ``href`` is empty
    for plain-text segments.
    """

    tag: str
    text: str
    href: str = ""

    def __post_init__(self) -> None:
        assert_non_empty(self.tag, name="RichTextSegment.tag")
        assert_max_length(
            self.tag,
            max_length=_MAX_VERB_LENGTH,
            name="RichTextSegment.tag",
        )
        assert_max_length(
            self.text,
            max_length=_MAX_TEXT_LENGTH,
            name="RichTextSegment.text",
        )
        assert_max_length(
            self.href,
            max_length=_MAX_TEXT_LENGTH,
            name="RichTextSegment.href",
        )


@dataclass(frozen=True, slots=True)
class RichTextContent:
    """Feishu-style rich-text body (PR-097 §2 R-14).

    Paired with :class:`MessageContent.rich_text` so the dispatcher
    can route to :meth:`FeishuTransport.send_rich_text` (msg_type=
    ``post``) when set; falls back to plain-text :meth:`send` when
    unset.

    ``lines`` is a tuple-of-tuples to keep the VO hashable.  Each
    inner tuple is one line of the post; an empty tuple is a blank
    line.
    """

    title: str
    lines: tuple[tuple[RichTextSegment, ...], ...] = ()

    def __post_init__(self) -> None:
        assert_max_length(
            self.title,
            max_length=_MAX_TEXT_LENGTH,
            name="RichTextContent.title",
        )


@dataclass(frozen=True, slots=True)
class MessageContent:
    """Body of a :class:`ChannelMessage`.

    Two extension points (PR-097 §2 R-8 / R-14) sit alongside the
    primary ``text`` field:

    * ``attachments`` — a tuple of :class:`ImageAttachment` for inbound
      messages that carried images.  The dispatcher invokes the
      kind-specific image decoder
      (:mod:`qai.channels.adapters.feishu_image_decoder` /
      :mod:`qai.channels.adapters.wechat_image_decoder`) before
      forwarding to the chat / ai_coding bridges.
    * ``rich_text`` — optional :class:`RichTextContent`.  When set the
      outbound dispatcher routes to
      :meth:`FeishuTransport.send_rich_text` instead of the plain
      :meth:`send`; the ``text`` field is still required (kept as a
      degraded plain-text fallback for providers that do not support
      post / markdown messages).

    The VO remains frozen + hashable; ``attachments`` is a tuple and
    ``rich_text`` is itself a frozen dataclass.
    """

    text: str
    attachments: tuple[ImageAttachment, ...] = ()
    rich_text: RichTextContent | None = None

    def __post_init__(self) -> None:
        assert_non_empty(self.text, name="MessageContent.text")
        assert_max_length(
            self.text, max_length=_MAX_TEXT_LENGTH, name="MessageContent.text"
        )


@dataclass(frozen=True, slots=True)
class Command:
    """Result of running a command parser over a :class:`MessageContent`.

    A :class:`Command` is what use cases dispatch downstream: the raw
    text never crosses the channels↔chat boundary, only the structured
    verb + args.

    ``args`` is a :class:`tuple` so the VO remains hashable / safe to
    use as a dict key.
    """

    verb: str
    args: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        assert_non_empty(self.verb, name="Command.verb")
        assert_max_length(
            self.verb, max_length=_MAX_VERB_LENGTH, name="Command.verb"
        )
        for idx, arg in enumerate(self.args):
            assert_max_length(
                arg,
                max_length=_MAX_ARG_LENGTH,
                name=f"Command.args[{idx}]",
            )


@dataclass(frozen=True, slots=True)
class WebhookPayload:
    """Structured envelope produced by :class:`WebhookPayloadParserPort`.

    Fields:
    * ``kind`` — provider that emitted the webhook.
    * ``provider_event_id`` — provider-side id used for idempotency.
    * ``sender`` — :class:`ChannelUserId` of the originating user.
    * ``content`` — :class:`MessageContent` parsed from the body.
    * ``arrived_at`` — tz-aware UTC datetime when the webhook was received.
    * ``raw_metadata`` — provider-specific extras (read-only mapping
      preserved as a tuple of (key, value) pairs to keep the VO hashable
      and frozen-friendly).

    The signed raw body itself is **not** part of the VO: signature
    verification happens before construction.
    """

    kind: ChannelKind
    provider_event_id: str
    sender: ChannelUserId
    content: MessageContent
    arrived_at: datetime
    raw_metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        assert_non_empty(
            self.provider_event_id, name="WebhookPayload.provider_event_id"
        )
        assert_max_length(
            self.provider_event_id,
            max_length=_MAX_REF_LENGTH,
            name="WebhookPayload.provider_event_id",
        )
        # Force tz-aware UTC so downstream code never sees naive timestamps.
        # ensure_aware_utc returns a (possibly converted) datetime; we
        # bypass dataclass frozen by going through object.__setattr__.
        normalised = ensure_aware_utc(self.arrived_at)
        if normalised is not self.arrived_at:
            object.__setattr__(self, "arrived_at", normalised)


@dataclass(frozen=True, slots=True)
class ChannelHealthReport:
    """Status snapshot returned by :meth:`ChannelTransportPort.health`."""

    status: ChannelHealth
    detail: str = ""
    checked_at: datetime | None = None

    def __post_init__(self) -> None:
        assert_max_length(
            self.detail,
            max_length=_MAX_TEXT_LENGTH,
            name="ChannelHealthReport.detail",
        )
        if self.checked_at is not None:
            normalised = ensure_aware_utc(self.checked_at)
            if normalised is not self.checked_at:
                object.__setattr__(self, "checked_at", normalised)


@dataclass(frozen=True, slots=True)
class CredentialsRef:
    """Pointer to a credential record in :class:`SecretStore`.

    ``service`` is the SecretStore namespace (e.g.
    ``"qai.channels.feishu"``) and ``key`` the per-instance key.
    The VO **deliberately** holds no plaintext: resolving it goes through
    :class:`~qai.channels.application.ports.CredentialsResolverPort`.
    """

    service: str
    key: str

    def __post_init__(self) -> None:
        assert_non_empty(self.service, name="CredentialsRef.service")
        assert_non_empty(self.key, name="CredentialsRef.key")
        assert_max_length(
            self.service,
            max_length=_MAX_REF_LENGTH,
            name="CredentialsRef.service",
        )
        assert_max_length(
            self.key, max_length=_MAX_REF_LENGTH, name="CredentialsRef.key"
        )


@dataclass(frozen=True, slots=True)
class QrLoginChallenge:
    """Challenge issued for a personal-WeChat-style QR login flow.

    The image bytes themselves are *not* in the domain VO — adapters
    return a transport-level URL or blob ref via the application layer.

    ``qr_url`` (append-only field) carries the REAL provider QR-login URL
    once the wechatbot SDK reports it via its ``on_qr_url`` callback.  It
    is ``None`` until the SDK supplies a URL; the image-render use case
    encodes this URL into the scannable QR PNG (V1 parity:
    ``backend/channels/wechat/channel.py:794`` ``_qr_url`` →
    ``api_routes.py:94`` ``qrcode.make(qr_url)``).  Kept optional with a
    default so every existing constructor call site stays valid.
    """

    challenge_id: str
    instance_id_value: str
    issued_at: datetime
    expires_at: datetime
    status: QrLoginStatus = QrLoginStatus.ISSUED
    qr_url: str | None = None

    def __post_init__(self) -> None:
        assert_non_empty(
            self.challenge_id, name="QrLoginChallenge.challenge_id"
        )
        assert_max_length(
            self.challenge_id,
            max_length=_MAX_REF_LENGTH,
            name="QrLoginChallenge.challenge_id",
        )
        assert_non_empty(
            self.instance_id_value, name="QrLoginChallenge.instance_id_value"
        )
        issued = ensure_aware_utc(self.issued_at)
        expires = ensure_aware_utc(self.expires_at)
        if issued is not self.issued_at:
            object.__setattr__(self, "issued_at", issued)
        if expires is not self.expires_at:
            object.__setattr__(self, "expires_at", expires)
        if expires < issued:
            raise ValueError(
                "QrLoginChallenge.expires_at must be >= issued_at"
            )

    def is_expired(self, *, now: datetime) -> bool:
        return ensure_aware_utc(now) >= self.expires_at


@dataclass(frozen=True, slots=True)
class MessageReplyRef:
    """Reference linking an outbound reply back to the inbound message
    it answers.

    Used by :class:`SendChannelReplyUseCase` to mark
    :class:`ChannelMessage` as ``replied``.
    """

    inbound_message_id: ChannelMessageId
    outbound_provider_message_id: str

    def __post_init__(self) -> None:
        assert_non_empty(
            self.outbound_provider_message_id,
            name="MessageReplyRef.outbound_provider_message_id",
        )
        assert_max_length(
            self.outbound_provider_message_id,
            max_length=_MAX_REF_LENGTH,
            name="MessageReplyRef.outbound_provider_message_id",
        )


# ---------------------------------------------------------------------------
# Channel-level settings (PR-202)
# ---------------------------------------------------------------------------

_MAX_SETTING_FIELD_LENGTH = 1_024
_MAX_BINDING_KEY_LENGTH = 256
_MAX_BINDINGS_COUNT = 1_000


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelProxyConfig:
    """Outbound HTTP proxy configuration for a single channel instance.

    The plaintext password is **never** stored on the VO — the route
    layer hands it to :class:`SecretStore` (namespace
    ``"qai.channels.proxy"``, key ``instance_id``) and only the
    presence-of-password indicator survives on the VO so the front-end
    can render a "(saved)" placeholder without leaking the value.

    Empty ``url`` means "no proxy"; ``username`` may be empty even
    when ``url`` is set (anonymous proxy).
    """

    url: str = ""
    username: str = ""
    has_password: bool = False

    def __post_init__(self) -> None:
        assert_max_length(
            self.url,
            max_length=_MAX_SETTING_FIELD_LENGTH,
            name="ChannelProxyConfig.url",
        )
        assert_max_length(
            self.username,
            max_length=_MAX_SETTING_FIELD_LENGTH,
            name="ChannelProxyConfig.username",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelModelConfig:
    """Per-channel default model selection.

    Empty strings in both fields means "follow the global UI default"
    (matches old ``forge_config.json`` semantics where a missing key
    means the channel inherits the WebUI's currently-selected model).
    """

    model_id: str = ""
    model_provider: str = ""

    def __post_init__(self) -> None:
        assert_max_length(
            self.model_id,
            max_length=_MAX_SETTING_FIELD_LENGTH,
            name="ChannelModelConfig.model_id",
        )
        assert_max_length(
            self.model_provider,
            max_length=_MAX_SETTING_FIELD_LENGTH,
            name="ChannelModelConfig.model_provider",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelSettings:
    """Complete user-managed settings for a single :class:`ChannelInstance`.

    PR-202 stores this as a JSON-encoded blob in the
    :attr:`ChannelInstance.metadata` tuple under the key
    ``"settings_v1"``; the legacy free-form ``metadata`` slots remain
    available for ad-hoc kind-specific extensions (e.g. WeChat's
    ``proxy_password_saved_at`` timestamp).

    Fields:

    * ``auto_start`` — whether the channel should be started on app
      boot (mirrors legacy ``forge_config.feishu_auto_start`` /
      ``wechat_channel.auto_connect``).
    * ``proxy`` — :class:`ChannelProxyConfig` for outbound HTTP.
    * ``model`` — :class:`ChannelModelConfig` for default LLM.
    * ``kind_specific`` — flat ``dict[str, str]`` of provider-specific
      non-secret config (Feishu: ``app_id`` / ``encrypt_key``;
      WeChat: none currently).  Plaintext secrets (app_secret,
      encrypt_key, verification_token) are NEVER stored here — they
      live in the SecretStore via :class:`CredentialsRef`.
    * ``has_app_secret`` — Feishu-only presence-of-secret indicator.
      The plaintext Feishu ``app_secret`` is **never** stored on the VO
      — the route layer hands it to :class:`SecretStore` (namespace
      ``"qai.channels.feishu.app_secret"``, key ``instance_id``) exactly
      like :class:`ChannelProxyConfig.has_password` does for the proxy
      password.  Only this presence flag survives so the front-end can
      render a "(saved)" placeholder without leaking the value.  WeChat
      has no ``app_secret`` and never sets this (default ``False``).

    The VO is frozen / hashable; the route layer hands it to
    :class:`UpdateChannelConfigUseCase` which produces a new
    :class:`ChannelInstance` via :meth:`ChannelInstance.with_settings`.
    """

    auto_start: bool = False
    proxy: ChannelProxyConfig = field(default_factory=ChannelProxyConfig)
    model: ChannelModelConfig = field(default_factory=ChannelModelConfig)
    kind_specific: tuple[tuple[str, str], ...] = ()
    has_app_secret: bool = False

    def __post_init__(self) -> None:
        for k, v in self.kind_specific:
            assert_max_length(
                k,
                max_length=_MAX_SETTING_FIELD_LENGTH,
                name="ChannelSettings.kind_specific.key",
            )
            assert_max_length(
                v,
                max_length=_MAX_SETTING_FIELD_LENGTH,
                name="ChannelSettings.kind_specific.value",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelBindings:
    """WebUI conversation ↔ channel-user bindings for one instance.

    Each binding is ``(conversation_id, channel_user_id)`` — when the
    front-end's chat tab posts a reply on ``conversation_id``, the
    channel sync layer (the :class:`PushChannelMessageUseCase` in
    :mod:`qai.channels.application.use_cases.push_message`) looks up
    the binding and pushes the same text out to the bound channel
    user.

    The VO is canonicalised: ``entries`` is always returned sorted by
    ``conversation_id`` so equality / hashing is stable.

    Empty ``channel_user_id`` is treated as "no binding" — the
    :meth:`with_binding` helper drops such entries.
    """

    entries: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if len(self.entries) > _MAX_BINDINGS_COUNT:
            raise ValueError(
                f"ChannelBindings.entries exceeds maximum "
                f"({len(self.entries)} > {_MAX_BINDINGS_COUNT})"
            )
        seen: set[str] = set()
        for conv_id, channel_user_id in self.entries:
            assert_non_empty(
                conv_id, name="ChannelBindings.entries.conversation_id"
            )
            assert_max_length(
                conv_id,
                max_length=_MAX_BINDING_KEY_LENGTH,
                name="ChannelBindings.entries.conversation_id",
            )
            assert_max_length(
                channel_user_id,
                max_length=_MAX_BINDING_KEY_LENGTH,
                name="ChannelBindings.entries.channel_user_id",
            )
            if conv_id in seen:
                raise ValueError(
                    f"duplicate conversation_id in ChannelBindings: "
                    f"{conv_id!r}"
                )
            seen.add(conv_id)
        # Defensive sort + freeze.
        ordered = tuple(
            sorted(self.entries, key=lambda kv: kv[0])
        )
        if ordered != self.entries:
            object.__setattr__(self, "entries", ordered)

    def lookup(self, conversation_id: str) -> str:
        """Return the bound channel-user id, or empty string if none."""
        for conv_id, user_id in self.entries:
            if conv_id == conversation_id:
                return user_id
        return ""

    def with_binding(
        self, *, conversation_id: str, channel_user_id: str
    ) -> "ChannelBindings":
        """Return a new VO with the binding set / replaced.

        Empty ``channel_user_id`` removes the binding (matches the
        legacy semantics where ``POST /api/wechat/bindings`` with empty
        ``wechat_user_id`` cleared the binding).
        """
        kept = tuple(
            (c, u) for c, u in self.entries if c != conversation_id
        )
        if not channel_user_id:
            return ChannelBindings(entries=kept)
        return ChannelBindings(
            entries=kept + ((conversation_id, channel_user_id),)
        )

    def without_binding(
        self, *, conversation_id: str
    ) -> "ChannelBindings":
        """Return a new VO with the binding for ``conversation_id`` removed."""
        return ChannelBindings(
            entries=tuple(
                (c, u) for c, u in self.entries if c != conversation_id
            )
        )

    def as_dict(self) -> dict[str, str]:
        """Convenience: return the bindings as a plain dict for serialisation."""
        return {c: u for c, u in self.entries}


# ---------------------------------------------------------------------------
# Channel invocation context (no-UI channel identifier for PolicyCenter)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ChannelContext:
    """Invocation context attached to outbound bridge calls.

    When a channel (WeChat / Feishu) dispatches a message to the
    AI / tool execution bridge, this VO carries the channel type identifier
    so downstream security layers (PolicyCenter) can auto-downgrade ASK
    permission decisions to DENY — channels have no interactive UI for the
    user to approve tool-permission prompts.

    Fields:

    * ``channel_type`` — lowercase channel kind value (``"wechat"`` /
      ``"feishu"``).  Matches :attr:`ChannelKind.value`.
    * ``session_id`` — the conversation / session id on the channels side,
      used by PolicyCenter for per-session grants.
    * ``instance_id`` — the :class:`ChannelInstanceId` value for tracing.
    * ``is_no_ui`` — always ``True`` for channel invocations; indicates the
      caller has no interactive approval UI.  Kept explicit so the bridge
      does not need to re-derive this from ``channel_type``.
    """

    channel_type: str
    session_id: str = ""
    instance_id: str = ""
    is_no_ui: bool = True

    def __post_init__(self) -> None:
        assert_non_empty(
            self.channel_type, name="ChannelContext.channel_type"
        )
        assert_max_length(
            self.channel_type,
            max_length=_MAX_VERB_LENGTH,
            name="ChannelContext.channel_type",
        )
        assert_max_length(
            self.session_id,
            max_length=_MAX_REF_LENGTH,
            name="ChannelContext.session_id",
        )
        assert_max_length(
            self.instance_id,
            max_length=_MAX_REF_LENGTH,
            name="ChannelContext.instance_id",
        )

    @classmethod
    def from_kind(
        cls,
        kind: "ChannelKind",
        *,
        session_id: str = "",
        instance_id: str = "",
    ) -> "ChannelContext":
        """Construct from a :class:`ChannelKind` enum member."""
        return cls(
            channel_type=kind.value,
            session_id=session_id,
            instance_id=instance_id,
            is_no_ui=True,
        )


__all__ = [
    "ChannelStatus",
    "ChannelMessageStatus",
    "MessageDirection",
    "ChannelHealth",
    "QrLoginStatus",
    "MessageContent",
    "ImageAttachment",
    "RichTextSegment",
    "RichTextContent",
    "Command",
    "WebhookPayload",
    "ChannelHealthReport",
    "CredentialsRef",
    "QrLoginChallenge",
    "MessageReplyRef",
    # PR-202: settings + bindings
    "ChannelProxyConfig",
    "ChannelModelConfig",
    "ChannelSettings",
    "ChannelBindings",
    # Channel invocation context
    "ChannelContext",
]
