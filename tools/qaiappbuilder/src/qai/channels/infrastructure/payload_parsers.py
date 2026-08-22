# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Real :class:`WebhookPayloadParserPort` adapters (PR-047, PR-097).

One implementation per :class:`ChannelKind`.  Each parser decodes the
provider's wire format into a kind-agnostic :class:`WebhookPayload`.

Because the providers use different envelope shapes the
implementations cannot collapse into one parametric class without
re-introducing the SCC pattern that the refactor split apart.

The parsers are tolerant about ``arrived_at`` — if the provider gives
a timestamp we use it (after tz-normalisation in
:class:`WebhookPayload`); otherwise we fall back to ``datetime.now(UTC)``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from qai.channels.domain import (
    ChannelKind,
    ChannelUserId,
    ImageAttachment,
    MessageContent,
    WebhookPayload,
    WebhookPayloadInvalidError,
)

__all__ = [
    "WechatPayloadParser",
    "FeishuPayloadParser",
    "extract_feishu_image_attachment",
]


#: Placeholder caption injected for an inbound Feishu image message that
#: carried no caller-supplied text.  :class:`MessageContent` enforces a
#: non-empty ``text`` invariant, and the dispatch bridge strips this exact
#: token back out before synthesising the "请描述这张图片" prompt
#: (apps.api._channel_dispatch_bridge._resolve_image_attachments).  Kept as
#: a module constant so the WebSocket inbound path
#: (qai.channels.infrastructure.feishu_ws) and the webhook parser agree on
#: the same sentinel.
_FEISHU_IMAGE_PLACEHOLDER_TEXT = "[image]"


def extract_feishu_image_attachment(
    *,
    content_raw: Any,
    message_id: str,
    message_type: str,
) -> tuple[str | None, tuple[ImageAttachment, ...]]:
    """Extract ``(text, attachments)`` from a Feishu inbound message body.

    Shared by BOTH inbound paths so the image-handling logic never
    drifts between them (refactor judgement 1 — reuse over duplication):

    * the webhook parser (:class:`FeishuPayloadParser`), and
    * the WebSocket transport
      (:class:`qai.channels.infrastructure.feishu_ws.FeishuWebSocketTransport`).

    ``content_raw`` is ``event.message.content`` — a JSON-encoded string
    (the common Feishu wire shape, e.g. ``'{"image_key": "img_v3_..."}'``)
    or, for tolerant callers, an already-decoded ``dict``.  When
    ``message_type == "image"`` and a usable ``image_key`` + ``message_id``
    are present, an :class:`ImageAttachment` is built so the dispatch
    bridge can fetch the bytes later via
    :mod:`qai.channels.adapters.feishu_image_decoder` (parsers stay
    synchronous; downloads happen downstream — PR-097 §2 R-8).

    Mirrors V1 ``backend/channels/feishu/channel.py:815-825`` which only
    special-cased ``msg_type == "image"`` (text + image are the only
    inbound media types V1's WS path handled; every other type fell
    through to a plain "暂不支持" text reply).

    Returns:
        ``(text, attachments)`` where ``text`` is the caller-supplied
        caption (or the ``[image]`` placeholder when an image carried no
        caption, so :class:`MessageContent`'s non-empty invariant holds),
        and ``attachments`` is a possibly-empty tuple of
        :class:`ImageAttachment`.  ``text`` may be ``None`` when the body
        was an undecodable string with no caption — the caller decides how
        to fall back.
    """
    decoded: Any = None
    text: str | None = None
    if isinstance(content_raw, str):
        try:
            decoded = json.loads(content_raw)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            text = (
                decoded.get("text")
                if isinstance(decoded.get("text"), str)
                else None
            )
        elif decoded is None:
            # Undecodable string — treat the raw body as the caption.
            text = content_raw
    elif isinstance(content_raw, dict):
        decoded = content_raw
        raw_text = content_raw.get("text")
        text = raw_text if isinstance(raw_text, str) else None

    attachments: tuple[ImageAttachment, ...] = ()
    if message_type == "image" and isinstance(decoded, dict):
        image_key = decoded.get("image_key")
        if (
            isinstance(image_key, str)
            and image_key
            and isinstance(message_id, str)
            and message_id
        ):
            attachments = (
                ImageAttachment(
                    message_id=str(message_id),
                    image_key=str(image_key),
                    kind=ChannelKind.FEISHU,
                ),
            )
            # Image messages carry no caller-supplied text — supply a
            # placeholder caption so MessageContent's non-empty invariant
            # holds; the dispatcher rebuilds the multimodal content list
            # from the attachments tuple.
            if not text:
                text = _FEISHU_IMAGE_PLACEHOLDER_TEXT
    return text, attachments

def _parse_json(kind: ChannelKind, raw_body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookPayloadInvalidError(
            kind.value, f"non-JSON body: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise WebhookPayloadInvalidError(
            kind.value, "envelope must be a JSON object"
        )
    return data


def _parse_iso_or_now(value: object) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    if isinstance(value, (int, float)):
        # WeChat timestamps come as UNIX seconds.
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError):
            pass
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# WeChat
# ---------------------------------------------------------------------------
class WechatPayloadParser:
    """Parses inbound WeChat Official Account webhook bodies.

    The official wire format is XML, but bot proxies (e.g. the
    micro-frontend the legacy code used) typically forward a JSON
    transcription with the following keys::

        {
            "ToUserName": "...",
            "FromUserName": "<openid>",
            "MsgId": "<event id>",
            "Content": "<text>",
            "CreateTime": <unix-seconds>
        }

    For tests + legacy proxies a flat ``{event_id, sender, text}``
    envelope is also accepted (matches the PR-036 wire shape).
    """

    KIND = ChannelKind.WECHAT
    __slots__ = ()

    def parse(
        self,
        kind: ChannelKind,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        instance_id: str | None = None,  # noqa: ARG002 — port-compat
    ) -> WebhookPayload:
        if kind is not self.KIND:
            raise WebhookPayloadInvalidError(
                kind.value, f"parser only handles {self.KIND.value}"
            )
        data = _parse_json(kind, raw_body)
        # Accept Pascal (XML→JSON), lower (custom) or flat
        # ``{event_id, sender, text}`` field names.
        sender = (
            data.get("FromUserName")
            or data.get("from_user")
            or data.get("sender")
        )
        event_id = (
            data.get("MsgId")
            or data.get("msg_id")
            or data.get("event_id")
        )
        text = (
            data.get("Content")
            or data.get("content")
            or data.get("text")
        )
        ct = (
            data.get("CreateTime")
            or data.get("create_time")
            or data.get("arrived_at")
        )
        if not sender or not event_id or text is None:
            raise WebhookPayloadInvalidError(
                kind.value,
                "missing required wechat fields "
                "(FromUserName/MsgId/Content)",
            )
        return WebhookPayload(
            kind=kind,
            provider_event_id=str(event_id),
            sender=ChannelUserId(value=str(sender)),
            content=MessageContent(text=str(text)),
            arrived_at=_parse_iso_or_now(ct),
        )


# ---------------------------------------------------------------------------
# Feishu (Lark)
# ---------------------------------------------------------------------------
class FeishuPayloadParser:
    """Parses inbound Feishu / Lark webhook bodies.

    Feishu's event-2.0 envelope shape::

        {
            "schema": "2.0",
            "header": {"event_id": "...", "create_time": "ms-string"},
            "event": {
                "sender": {"sender_id": {"open_id": "..."}},
                "message": {
                    "content": "{\\"text\\":\\"hi\\"}",
                    "message_type": "text"
                }
            }
        }

    For backwards compatibility we also accept a flat envelope
    ``{event_id, sender, text}``.
    """

    KIND = ChannelKind.FEISHU
    __slots__ = ()

    def parse(
        self,
        kind: ChannelKind,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        instance_id: str | None = None,  # noqa: ARG002 — port-compat
    ) -> WebhookPayload:
        if kind is not self.KIND:
            raise WebhookPayloadInvalidError(
                kind.value, f"parser only handles {self.KIND.value}"
            )
        data = _parse_json(kind, raw_body)
        # Feishu nested envelope?
        header = data.get("header")
        event = data.get("event")
        if isinstance(header, dict) and isinstance(event, dict):
            event_id = header.get("event_id")
            ct = header.get("create_time")
            sender_id = (
                event.get("sender", {})
                .get("sender_id", {})
                .get("open_id")
                if isinstance(event.get("sender"), dict)
                else None
            )
            message = event.get("message")
            if not isinstance(message, dict):
                raise WebhookPayloadInvalidError(
                    kind.value, "missing event.message in feishu envelope"
                )
            message_type = message.get("message_type") or ""
            message_id = message.get("message_id") or ""
            content_raw = message.get("content")
            # Shared with the WebSocket inbound path so image-handling
            # never drifts between the two (see
            # :func:`extract_feishu_image_attachment`).
            text, attachments = extract_feishu_image_attachment(
                content_raw=content_raw,
                message_id=str(message_id),
                message_type=message_type,
            )
            if not (event_id and sender_id and text):
                raise WebhookPayloadInvalidError(
                    kind.value,
                    "missing event_id/open_id/text in feishu envelope",
                )
            arrived_at = _feishu_timestamp(ct)
            return WebhookPayload(
                kind=kind,
                provider_event_id=str(event_id),
                sender=ChannelUserId(value=str(sender_id)),
                content=MessageContent(
                    text=str(text),
                    attachments=attachments,
                ),
                arrived_at=arrived_at,
            )
        # Flat fallback envelope (used by legacy proxies + tests).
        event_id = data.get("event_id")
        sender = data.get("sender")
        text = data.get("text")
        ct = data.get("arrived_at")
        if not (event_id and sender and text):
            raise WebhookPayloadInvalidError(
                kind.value,
                "missing required feishu fields (event_id/sender/text)",
            )
        return WebhookPayload(
            kind=kind,
            provider_event_id=str(event_id),
            sender=ChannelUserId(value=str(sender)),
            content=MessageContent(text=str(text)),
            arrived_at=_parse_iso_or_now(ct),
        )


def _feishu_timestamp(value: object) -> datetime:
    """Feishu's ``create_time`` is a millisecond UNIX timestamp string."""
    if isinstance(value, str) and value:
        try:
            ms = int(value)
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        except (ValueError, OSError):
            return _parse_iso_or_now(value)
    return _parse_iso_or_now(value)



