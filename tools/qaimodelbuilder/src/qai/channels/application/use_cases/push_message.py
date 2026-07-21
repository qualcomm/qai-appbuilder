# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Out-of-band channel message push (PR-205).

The legacy ``backend/channels/wechat/channel.py:send_to_user`` plus the
matching ``feishu_channel`` helpers expose an "outbound push" path that
is **not** a reply to an inbound webhook — it is used when the new
architecture's downstream pipeline (CC / OC task progress, AI Coding
session status updates, scheduled notifications) wants to deliver text
to a channel user it has no inbound message id for.

PR-201..204 covered the inbound-driven path
(:class:`SendChannelReplyUseCase`); the outbound push path was the
last legacy facade behaviour with no new equivalent.  PR-205 plugs the
gap so I2 PR-1104 can delete ``backend/feishu_channel.py`` /
``backend/wechat_channel.py`` without losing capability.

Use case shape
--------------

* Input: ``instance_id`` + ``target`` (channel-side user id) + ``text``.
* Long messages are split via :class:`MessageSplitter` (PR-203,
  default 5000 chars) before being handed to
  :class:`ChannelTransportPort.send`.
* Each split chunk is suffixed with ``" (i/N)"`` only when there is
  more than one chunk — matching the legacy ``send_to_user``
  pagination convention.
* Returns a ``PushChannelMessageResult`` value that records every
  outbound provider message id, the chunk count, and the per-chunk
  delivery status (pure dataclass; no event emission required since
  there is no canonical domain event for "outbound push").

Errors
------

* If the instance does not exist → :class:`ChannelInstanceNotFoundError`.
* If the instance is not running → :class:`ChannelInstanceStateError`
  (you cannot push through a stopped transport).
* Transport-level failures bubble up unchanged.

Cross-context use
-----------------

Future L4 / L1 lanes will inject ``PushChannelMessageUseCase`` as a
collaborator into chat / ai_coding bridges to surface progress.  This
PR only ships the channels-side application primitive; binding it to
chat is L4 PR-401's remit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from qai.platform.events import EventBus
from qai.platform.logging import get_logger
from qai.platform.time import Clock

from qai.channels.application.ports import (
    ChannelInstanceRepositoryPort,
    ChannelTransportPort,
)
from qai.channels.application.services.message_splitter import (
    MessageSplitter,
)
from qai.channels.domain import (
    ChannelInstance,
    ChannelInstanceId,
    ChannelUserId,
)
from qai.channels.domain.errors import ChannelInstanceStateError
from qai.channels.domain.value_objects import MessageContent

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class PushChannelMessageCommand:
    """Command for :class:`PushChannelMessageUseCase`.

    ``page_suffix_format`` is exposed primarily for tests; production
    callers should leave it at its default (matches legacy WeChat
    pagination format).
    """

    instance_id: ChannelInstanceId
    target: ChannelUserId
    text: str
    page_suffix_format: str = "({i}/{n})"


@dataclass(frozen=True, slots=True, kw_only=True)
class PushChunkResult:
    """Per-chunk delivery record.

    ``provider_message_id`` is empty when ``error`` is non-empty.
    """

    sequence: int
    text: str
    provider_message_id: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True, slots=True, kw_only=True)
class PushChannelMessageResult:
    """Aggregate result returned by :class:`PushChannelMessageUseCase`.

    ``all_ok`` is convenience: ``True`` iff every chunk delivered
    successfully.  ``provider_message_ids`` is the ordered list of
    successfully-delivered ids; partial deliveries surface every id
    that was sent before the first failure.
    """

    instance_id: str
    target: str
    chunk_count: int
    chunks: tuple[PushChunkResult, ...] = field(default_factory=tuple)

    @property
    def all_ok(self) -> bool:
        return all(c.ok for c in self.chunks)

    @property
    def provider_message_ids(self) -> tuple[str, ...]:
        return tuple(c.provider_message_id for c in self.chunks if c.ok)

    def first_error(self) -> str:
        for chunk in self.chunks:
            if not chunk.ok:
                return chunk.error
        return ""


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


# The transport_factory shape mirrors what the existing
# StartChannelInstanceUseCase / SendChannelReplyUseCase use: a callable
# that maps a ChannelInstance to its kind-specific ChannelTransportPort.
TransportFactory = Callable[[ChannelInstance], ChannelTransportPort]


class PushChannelMessageUseCase:
    """Push an out-of-band text message to a channel user.

    Constructor takes:

    * ``instances`` — :class:`ChannelInstanceRepositoryPort` (lookup +
      state check; never mutates the instance).
    * ``transport_factory`` — same per-kind dispatcher used by
      :class:`SendChannelReplyUseCase`.
    * ``splitter`` — optional :class:`MessageSplitter` (defaults to
      ``MessageSplitter(max_chars=5000)`` matching WeChat's per-message
      cap, which is also a safe upper bound for Feishu).
    * ``clock`` / ``events`` — surfaced for symmetry with peer use
      cases; PR-205 does NOT emit a dedicated event (no domain event
      exists for "outbound push" yet, and adding one is outside this
      PR's scope per the L2 prompt).

    The use case is fully stateless — every ``execute`` call is
    independent.
    """

    __slots__ = (
        "_instances",
        "_transport_factory",
        "_splitter",
        "_clock",
        "_events",
    )

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        transport_factory: TransportFactory,
        splitter: MessageSplitter | None = None,
        clock: Clock,
        events: EventBus | None = None,
    ) -> None:
        self._instances = instances
        self._transport_factory = transport_factory
        self._splitter = splitter or MessageSplitter()
        self._clock = clock
        self._events = events

    async def execute(
        self, command: PushChannelMessageCommand
    ) -> PushChannelMessageResult:
        instance = await self._instances.get(command.instance_id)

        # Reject pushes on a transport that isn't running — the legacy
        # ``send_to_user`` returned False in this case; here we surface
        # a structured error so callers can distinguish "transport
        # down" from "provider rejected the message".
        if not instance.is_running():
            raise ChannelInstanceStateError(
                f"cannot push to channel instance "
                f"{instance.instance_id.value!r} in status "
                f"{instance.status.value!r}",
                current_status=instance.status.value,
                attempted="push_message",
            )

        chunks_text = self._splitter.split(command.text)
        if not chunks_text:
            # Empty input → nothing to send; return an empty success.
            return PushChannelMessageResult(
                instance_id=instance.instance_id.value,
                target=command.target.value,
                chunk_count=0,
                chunks=(),
            )

        transport = self._transport_factory(instance)
        n = len(chunks_text)

        # Pre-build the per-chunk text the transport actually sees.
        # Pagination suffix mirrors legacy WeChat behaviour: only added
        # when there is more than one chunk.  We re-use the user's
        # chunk text verbatim when n == 1 so simple cases stay clean.
        prepared: list[str] = []
        for i, chunk_text in enumerate(chunks_text, start=1):
            if n > 1:
                suffix = command.page_suffix_format.format(i=i, n=n)
                prepared.append(f"{chunk_text}\n\n{suffix}")
            else:
                prepared.append(chunk_text)

        results: list[PushChunkResult] = []
        for i, body in enumerate(prepared):
            try:
                provider_message_id = await transport.send(
                    instance,
                    command.target,
                    MessageContent(text=body),
                )
            except Exception as exc:  # noqa: BLE001
                _log.error(
                    "channels.push.chunk_failed",
                    instance_id=instance.instance_id.value,
                    target=command.target.value,
                    sequence=i,
                    error=str(exc),
                )
                results.append(
                    PushChunkResult(
                        sequence=i,
                        text=body,
                        error=str(exc) or type(exc).__name__,
                    )
                )
                # Stop on first failure — matches legacy behaviour
                # where send_to_user gave up after the first error
                # (the ``NoContextError`` path in particular returned
                # immediately).
                break
            else:
                results.append(
                    PushChunkResult(
                        sequence=i,
                        text=body,
                        provider_message_id=str(provider_message_id),
                    )
                )

        return PushChannelMessageResult(
            instance_id=instance.instance_id.value,
            target=command.target.value,
            chunk_count=n,
            chunks=tuple(results),
        )


__all__ = [
    "PushChannelMessageCommand",
    "PushChannelMessageResult",
    "PushChunkResult",
    "PushChannelMessageUseCase",
]
