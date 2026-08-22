# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Real :class:`ReplyDispatcherPort` adapter (PR-047 + PR-097 R-6).

The dispatcher resolves the right :class:`ChannelTransportPort` per
``instance.kind`` and delegates ``send`` to it, then synthesises a
:class:`MessageReplyRef` linking the outbound provider message id back
to the inbound :class:`ChannelMessageId`.

This is intentionally separate from :class:`ChannelTransportPort` so
the dispatch step can be faked in isolation (transports concern
themselves with start/stop/send/health; dispatch concerns itself with
the reply VO + state machine integration).

PR-097 R-6 — per-kind chunking
------------------------------

Provider message size limits:

* WeChat (personal account): **4000 chars** per message — long replies
  are split with a ``({i}/{n})`` suffix on each part so the user can
  follow ordering on mobile.
* Feishu: **4000 chars** per message — split text/post bodies plain.

Splitting uses :class:`~qai.channels.application.services.message_splitter.MessageSplitter`
which respects paragraph / sentence / word boundaries before falling
back to an arbitrary cut.  Between chunks we ``asyncio.sleep`` for
:data:`_INTER_CHUNK_DELAY_SECONDS` (50ms) to stay below provider
rate-limit thresholds — this matches the legacy ``await asyncio.sleep(0.05)``
gap used by ``cc_handler.reply_long``.

The first part of a multi-part reply produces the
:class:`MessageReplyRef`; subsequent parts reuse the same outbound
``in_reply_to`` link so the inbound state-machine entry stays single-
threaded (only one ``MessageReplyRef`` per inbound message at the
domain level — kept the §3.1 contract intact).
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Awaitable, Callable

from qai.channels.application.ports import (
    ChannelTransportPort,
)
from qai.channels.application.services.message_splitter import (
    MessageSplitter,
)
from qai.platform.logging import get_logger
from qai.channels.domain import (
    ChannelInstance,
    ChannelKind,
    ChannelMessageId,
    ChannelUserId,
    MessageContent,
    MessageReplyRef,
)

if TYPE_CHECKING:  # pragma: no cover
    pass

__all__ = [
    "OutboundReplyDispatcher",
    "IMAGE_REF_PATTERN",
    "split_image_refs",
    "ImageUrlToBytes",
]

logger = get_logger(__name__)

#: Callable resolving a chat image URL (``/api/images/files/...``) to
#: its raw bytes, or ``None`` when the file is missing / unreadable.
#: Supplied by the apps composition root so the channels context never
#: imports the chat image store (context-isolation contract): the
#: bridge closes over ``FileSystemImageUploadStore.get_path`` and hands
#: down only this opaque callable.
ImageUrlToBytes = Callable[[str], "bytes | None"]

#: Markdown image syntax pointing at the chat image static mount —
#: byte-for-byte the same pattern the chat context uses
#: (``qai.chat.application.use_cases._image_refs.IMAGE_REF_PATTERN``).
#: Duplicated (not imported) so the channels context stays isolated;
#: the shared cross-context contract is the URL shape, not the module.
IMAGE_REF_PATTERN = re.compile(r"!\[[^\]]*\]\((/api/images/files/[^)]+)\)")


def split_image_refs(text: str) -> "tuple[str, tuple[str, ...]]":
    """Split ``text`` into ``(clean_text, image_urls)``.

    ``image_urls`` preserves document order (and duplicates) so every
    ``![...](/api/images/files/...)`` screenshot is delivered exactly
    once as its own image message.  ``clean_text`` is ``text`` with the
    image-markdown removed and surrounding blank lines collapsed, so the
    text reply no longer carries an un-renderable raw markdown link.
    """
    if not text:
        return "", ()
    urls = tuple(m.group(1) for m in IMAGE_REF_PATTERN.finditer(text))
    if not urls:
        return text, ()
    stripped = IMAGE_REF_PATTERN.sub("", text)
    # Collapse the blank lines left where the image markdown used to be.
    lines = [ln.rstrip() for ln in stripped.splitlines()]
    collapsed: list[str] = []
    for ln in lines:
        if not ln and collapsed and not collapsed[-1]:
            continue
        collapsed.append(ln)
    clean = "\n".join(collapsed).strip()
    return clean, urls


TransportFactory = Callable[[ChannelInstance], ChannelTransportPort]


# Per-kind char-limit table.  Sourced from the legacy code paths cited
# in this module's docstring; kept module-level so a unit test can
# import and assert the values without instantiating the dispatcher.
_PER_KIND_CHAR_LIMIT: dict[ChannelKind, int] = {
    ChannelKind.WECHAT: 4000,
    ChannelKind.FEISHU: 4000,
}

#: Sleep between consecutive chunks of a multi-part reply.  Matches the
#: 50ms gap used by ``backend/channels/wechat/cc_handler.reply_long``.
_INTER_CHUNK_DELAY_SECONDS: float = 0.05


def _suffix_for_part(index: int, total: int) -> str:
    """Build the ``({i}/{n})`` suffix appended to multi-part WeChat
    replies (legacy ``cc_handler.split_long_message``).

    Returns ``""`` when ``total <= 1`` so single-message replies do
    not carry a numeric tag.
    """
    if total <= 1:
        return ""
    return f"\n({index}/{total})"


class OutboundReplyDispatcher:
    """Sends an outbound reply via the right kind-specific transport.

    PR-097 R-6: long replies are split at the per-kind char limit and
    sent sequentially with a 50ms inter-chunk delay.  The
    :class:`MessageReplyRef` returned points to the *first* outbound
    chunk; the legacy semantics (one ``MessageReplyRef`` per inbound
    message) are preserved unchanged.
    """

    __slots__ = ("_transport_factory",)

    def __init__(self, *, transport_factory: TransportFactory) -> None:
        self._transport_factory = transport_factory

    async def dispatch(
        self,
        instance: ChannelInstance,
        target: ChannelUserId,
        content: MessageContent,
        in_reply_to: ChannelMessageId,
        *,
        send_fn: "Callable[[MessageContent], Awaitable[str]] | None" = None,
    ) -> MessageReplyRef:
        """Dispatch an outbound reply, splitting long plain-text bodies.

        ``send_fn`` overrides how each (possibly chunked) part is sent.  It
        defaults to ``transport.send(instance, target, content)`` (reply to an
        individual user).  Callers that must reply to a *group chat* — e.g.
        Feishu group messages, which require ``receive_id_type=chat_id`` via
        ``transport.send_to_chat(instance, chat_id, content)`` — pass a
        ``send_fn`` so group replies get the **same** chunking + rich-text
        handling as individual replies (previously the group path bypassed
        the dispatcher and could neither split long messages nor send the
        ``(i/n)`` suffix).
        """
        transport = self._transport_factory(instance)
        limit = _PER_KIND_CHAR_LIMIT.get(instance.kind, 4000)
        text = content.text or ""

        if send_fn is None:

            async def _default_send(c: MessageContent) -> str:
                return await transport.send(instance, target, c)

            send_fn = _default_send

        # Short-path: rich-text + within limit, or plain text fits in
        # one chunk.  Falls through to the chunked path only when the
        # plain-text body exceeds the per-kind cap.
        if content.rich_text is not None or len(text) <= limit:
            outbound_id = await send_fn(content)
            return MessageReplyRef(
                inbound_message_id=in_reply_to,
                outbound_provider_message_id=outbound_id,
            )

        splitter = MessageSplitter(max_chars=limit)
        chunks = splitter.split(text)
        # Defensive: empty splitter output means empty input — already
        # filtered above; treat any zero-length chunks as no-ops.
        non_empty = [c for c in chunks if c]
        if not non_empty:
            outbound_id = await send_fn(content)
            return MessageReplyRef(
                inbound_message_id=in_reply_to,
                outbound_provider_message_id=outbound_id,
            )

        first_outbound_id: str | None = None
        total = len(non_empty)
        for i, chunk in enumerate(non_empty, start=1):
            suffix = (
                _suffix_for_part(i, total)
                if instance.kind is ChannelKind.WECHAT
                else ""
            )
            chunk_text = chunk + suffix
            chunk_content = MessageContent(text=chunk_text)
            outbound_id = await send_fn(chunk_content)
            if first_outbound_id is None:
                first_outbound_id = outbound_id
            if i < total:
                await asyncio.sleep(_INTER_CHUNK_DELAY_SECONDS)

        # ``first_outbound_id`` is always set after the loop because
        # ``non_empty`` is non-empty by the early return above.
        assert first_outbound_id is not None
        return MessageReplyRef(
            inbound_message_id=in_reply_to,
            outbound_provider_message_id=first_outbound_id,
        )

    async def dispatch_images(
        self,
        instance: ChannelInstance,
        target: ChannelUserId,
        image_urls: "tuple[str, ...]",
        *,
        image_url_to_bytes: ImageUrlToBytes,
        send_image_fn: (
            "Callable[[bytes], Awaitable[str]] | None"
        ) = None,
    ) -> int:
        """Resolve + send each image ref as its own image message.

        ``image_url_to_bytes`` is the opaque apps-supplied resolver
        (URL → raw bytes or ``None``); ``send_image_fn`` overrides how a
        single image is delivered (defaults to
        ``transport.send_image(instance, target, bytes)`` for individual
        replies — group callers pass a ``send_image_fn`` bound to
        ``send_image_to_chat`` so the image lands in the group chat).

        Best-effort per image: a resolve miss (``None`` bytes) or a
        single failed send is logged and skipped so one bad screenshot
        never sinks the whole reply.  Returns the count sent, spacing
        consecutive images by the same 50ms inter-chunk gap as text.
        """
        if not image_urls:
            return 0
        transport = self._transport_factory(instance)

        if send_image_fn is None:
            send_image = getattr(transport, "send_image", None)
            if send_image is None:
                logger.warning(
                    "channels.dispatch.image_send_unsupported",
                    kind=instance.kind.value,
                )
                return 0

            async def _default_send_image(data: bytes) -> str:
                return await send_image(instance, target, data)

            send_image_fn = _default_send_image

        sent = 0
        total = len(image_urls)
        for idx, url in enumerate(image_urls):
            try:
                data = image_url_to_bytes(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.dispatch.image_resolve_failed",
                    url=url,
                    error=str(exc),
                )
                data = None
            if not data:
                logger.warning(
                    "channels.dispatch.image_resolve_empty", url=url
                )
                continue
            try:
                await send_image_fn(data)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.dispatch.image_send_failed",
                    url=url,
                    kind=instance.kind.value,
                    error=str(exc),
                )
                continue
            if idx + 1 < total:
                await asyncio.sleep(_INTER_CHUNK_DELAY_SECONDS)
        return sent
