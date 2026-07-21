# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: deliver a user message to a live :class:`CodingSession`.

Backs the legacy ``POST /api/cc/sessions/{id}/messages`` route which is
the *send* half of the two-step send-then-stream protocol; the SSE
stream is consumed via ``GET /sessions/{id}/stream`` (PR-035).

Sequence (happy path)
---------------------
1. Load the session by id.
2. If the session is ``TERMINATED`` raise
   :class:`CodingSessionAlreadyTerminatedError` (route layer surfaces
   as HTTP 410 via the legacy mapping).
3. Append the message to the aggregate (domain mutator
   :meth:`CodingSession.append_message`).
4. Forward the message to the provider via
   :meth:`CodingProviderPort.send_message` so the streaming task
   picks it up.
5. Persist the aggregate.
6. Drain & publish queued domain events.

The use case does NOT manage the SSE stream itself — that is the
responsibility of :class:`StreamCodingSessionUseCase`.  The route
layer returns ``stream_url`` pointing at the existing stream
endpoint so the client can subscribe.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    CodingProviderPort,
    CodingSessionRepositoryPort,
)
from qai.ai_coding.domain import (
    CodingSessionId,
    MessageContent,
)
from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class SendUserMessageCommand:
    """Input for :class:`SendUserMessageUseCase`.

    PR-095 (S9 audit §2.2 H-19 image-in-prompt parity) appends
    ``image_b64`` + ``image_mime`` to the existing dataclass — fields
    are added at the END of the parameter list and default to
    ``None`` so all historical callers continue to compile without
    change.  The provider adapter forwards the pair into the
    Anthropic Messages multimodal payload (``{"type":"image","source":
    {"type":"base64","media_type":...,"data":...}}``).
    """

    session_id: CodingSessionId
    content: MessageContent
    # Optional client-supplied idempotency key; mirrors the legacy
    # ``client_request_id`` query parameter.  When omitted the use
    # case generates a fresh ``message_id`` via the injected
    # :class:`IdGenerator`.
    client_request_id: str | None = None
    # PR-095 / S9 H-19: optional inline image attachment.  When both
    # values are present the provider forwards the pair into the
    # upstream multimodal request.  Both ``None`` keeps the legacy
    # text-only path unchanged.
    image_b64: str | None = None
    image_mime: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SendUserMessageResult:
    """Outcome of :class:`SendUserMessageUseCase`.

    Wire shape mirrors the legacy ``POST /messages`` body:

    * ``message_id`` — opaque token the client uses to subscribe to
      the per-message SSE stream.
    * ``user_msg_id`` — display id assigned to the user message
      inside the aggregate's history list (the route layer renders
      it 1:1).
    * ``stream_url`` — relative URL pointing at the SSE stream that
      consumes this message.
    """

    message_id: str
    user_msg_id: str
    stream_url: str


class SendUserMessageUseCase:
    """Application service for the *send* half of send-then-stream."""

    def __init__(
        self,
        *,
        provider_port: CodingProviderPort,
        repository: CodingSessionRepositoryPort,
        ids: IdGenerator,
        event_bus: EventBus,
    ) -> None:
        self._provider_port = provider_port
        self._repository = repository
        self._ids = ids
        self._event_bus = event_bus

    async def execute(
        self, command: SendUserMessageCommand
    ) -> SendUserMessageResult:
        session = await self._repository.get(command.session_id)
        # ``append_message`` raises ``CodingSessionAlreadyTerminatedError``
        # if the session is terminated; surface that to the route as 410
        # via the unified error handler.
        session.append_message(command.content)

        # Forward to provider so the live streaming task picks the
        # message up.  Failures here propagate verbatim — the message
        # has been recorded on the aggregate but not persisted yet.
        # PR-095 / S9 H-19: when the command carries an inline image,
        # stage it on the provider so the next ``send_message`` call
        # produces a multimodal content block.  ``attach_image`` is a
        # duck-typed optional hook — providers that don't override it
        # simply ignore the attachment and the text-only path runs.
        if (
            command.image_b64 is not None
            and command.image_mime is not None
        ):
            attach = getattr(self._provider_port, "attach_image", None)
            if callable(attach):
                attach(
                    session_id=command.session_id,
                    image_b64=command.image_b64,
                    image_mime=command.image_mime,
                )

        await self._provider_port.send_message(
            session_id=command.session_id,
            content=command.content,
        )

        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)

        # ``client_request_id`` ferries the legacy idempotency seed.
        # The route layer is responsible for honouring duplicates;
        # the use case just echoes the seed back when supplied and
        # generates a fresh id otherwise.
        request_id = command.client_request_id or self._ids.new_id()
        # Session-scoped user message id mirrors the legacy
        # ``cc-user-<ts>`` shape but uses the platform :class:`IdGenerator`
        # so test fixtures can deterministically assert the value.
        user_msg_id = self._ids.new_id()
        # Stream URL is provider-prefix-aware — the route layer is the
        # only place that knows whether this is a ``/api/cc`` or
        # ``/api/oc`` mount.  We return a path that includes the
        # session id but not the prefix; the route layer prepends it.
        stream_url = (
            f"/sessions/{command.session_id.value}"
            f"/messages/{request_id}/stream"
        )

        logger.info(
            "ai_coding.send_user_message.ok",
            session_id=str(command.session_id),
            message_id=request_id,
        )
        return SendUserMessageResult(
            message_id=request_id,
            user_msg_id=user_msg_id,
            stream_url=stream_url,
        )


__all__ = [
    "SendUserMessageCommand",
    "SendUserMessageResult",
    "SendUserMessageUseCase",
]
