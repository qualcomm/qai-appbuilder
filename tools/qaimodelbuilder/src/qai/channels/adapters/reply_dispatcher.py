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
from typing import TYPE_CHECKING, Awaitable, Callable

from qai.channels.application.ports import (
    ChannelTransportPort,
)
from qai.channels.application.services.message_splitter import (
    MessageSplitter,
)
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

__all__ = ["OutboundReplyDispatcher"]


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
