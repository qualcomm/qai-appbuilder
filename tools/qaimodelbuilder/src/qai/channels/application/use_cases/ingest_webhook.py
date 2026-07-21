# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: webhook ingestion + reply.

Two closely-related use cases live here because they form the inbound
half of a webhook-driven channel exchange:

1. :class:`IngestWebhookUseCase` — verify signature, parse payload,
   create a :class:`ChannelMessage` (with idempotency on
   ``provider_event_id``), parse a :class:`Command`, persist, publish
   :class:`ChannelMessageReceivedEvent`.
2. :class:`SendChannelReplyUseCase` — push the bridge reply text back
   to the channel via :class:`ReplyDispatcherPort`, mark replied,
   publish :class:`ChannelMessageRepliedEvent`.

The dispatch-to-bridge step that used to live here as a third use case
now runs through :func:`apps.api._channel_dispatch_bridge.dispatch_inbound_message`
— the apps-layer pipeline that coordinates chat / ai_coding / security
contexts and drives the streamed realtime delivery service.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.time import Clock

from qai.channels.application.ports import (
    ChannelInstanceRepositoryPort,
    ChannelMessageRepositoryPort,
    CommandParserPort,
    ReplyDispatcherPort,
    WebhookPayloadParserPort,
    WebhookSignatureVerifierPort,
)
from qai.channels.domain import (
    ChannelContext,
    ChannelInstanceId,
    ChannelKind,
    ChannelMessage,
    ChannelMessageId,
    ChannelMessageReceivedEvent,
    ChannelMessageRepliedEvent,
    Command,
    MessageContent,
    MessageReplyRef,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 1. Ingest
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class IngestWebhookCommand:
    """Inbound command for :class:`IngestWebhookUseCase`."""

    instance_id: ChannelInstanceId
    kind: ChannelKind
    raw_body: bytes
    headers: dict[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestWebhookResult:
    """Outcome of :meth:`IngestWebhookUseCase.execute`.

    ``deduplicated`` is ``True`` iff an earlier webhook with the same
    provider event id was already ingested — in that case ``message`` is
    the *original* persisted record and no new event is published.

    ``channel_context`` carries the channel-type identifier that the
    apps-layer dispatch bridge passes to downstream security / tool
    execution so that PolicyCenter can auto-downgrade ASK → DENY for
    no-UI channels (PR-097 parity with v1 ``_tool_ctx["channel"]``).
    """

    message: ChannelMessage
    parsed_command: Command | None
    deduplicated: bool
    channel_context: ChannelContext


class IngestWebhookUseCase:
    """Verify → parse → idempotency-check → persist → publish."""

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        messages: ChannelMessageRepositoryPort,
        verifier: WebhookSignatureVerifierPort,
        parser: WebhookPayloadParserPort,
        commands: CommandParserPort,
        ids: IdGenerator,
        events: EventBus,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._messages = messages
        self._verifier = verifier
        self._parser = parser
        self._commands = commands
        self._ids = ids
        self._events = events
        self._clock = clock

    async def execute(
        self, command: IngestWebhookCommand
    ) -> IngestWebhookResult:
        # The instance lookup happens up-front so an unknown id fails
        # fast (avoids running expensive signature verification on
        # spam traffic to non-existent instances).
        instance = await self._instances.get(command.instance_id)
        # Signature verification — raises WebhookSignatureInvalidError on
        # failure. Use cases never catch this; they let it propagate.
        # PR-201: pass ``instance_id`` so the verifier can resolve a
        # per-instance signing secret from SecretStore.  Verifiers that
        # ignore the kw-arg (legacy fakes) remain contract-compliant.
        self._verifier.verify(
            command.kind,
            command.raw_body,
            command.headers,
            instance_id=instance.instance_id.value,
        )
        payload = self._parser.parse(
            command.kind,
            command.raw_body,
            command.headers,
            instance_id=instance.instance_id.value,
        )

        existing = await self._messages.find_by_provider_event_id(
            command.kind, payload.provider_event_id
        )
        if existing is not None:
            return IngestWebhookResult(
                message=existing,
                parsed_command=existing.parsed_command,
                deduplicated=True,
                channel_context=ChannelContext.from_kind(
                    command.kind,
                    instance_id=instance.instance_id.value,
                ),
            )

        message_id = ChannelMessageId.generate(self._ids)
        message = ChannelMessage.receive(
            message_id=message_id,
            instance_id=instance.instance_id,
            kind=instance.kind,
            sender=payload.sender,
            provider_event_id=payload.provider_event_id,
            content=payload.content,
            arrived_at=payload.arrived_at,
        )
        parsed_command = self._commands.parse(payload.content)
        if parsed_command is not None:
            message = message.mark_parsed(
                command=parsed_command, now=self._clock.now()
            )
        else:
            # Plain chat → still mark parsed so the dispatch step is
            # uniform.  A None command tells the bridge to route to LLM.
            message = message.mark_parsed(
                command=Command(verb="__chat__"),
                now=self._clock.now(),
            )

        await self._messages.save(message)
        await self._events.publish(
            ChannelMessageReceivedEvent(
                message_id=message.message_id.value,
                instance_id=message.instance_id.value,
                kind=message.kind,
                sender_id=message.sender.value,
                arrived_at=message.arrived_at,
            )
        )
        return IngestWebhookResult(
            message=message,
            parsed_command=parsed_command,
            deduplicated=False,
            channel_context=ChannelContext.from_kind(
                command.kind,
                instance_id=instance.instance_id.value,
            ),
        )


# ---------------------------------------------------------------------------
# 2. Send reply
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class SendReplyCommand:
    inbound_message_id: ChannelMessageId
    reply_text: str


class SendChannelReplyUseCase:
    """Send the bridge's reply back to the channel and mark the message
    ``replied``."""

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        messages: ChannelMessageRepositoryPort,
        replies: ReplyDispatcherPort,
        events: EventBus,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._messages = messages
        self._replies = replies
        self._events = events
        self._clock = clock

    async def execute(
        self, command: SendReplyCommand
    ) -> ChannelMessage:
        message = await self._messages.get(command.inbound_message_id)
        instance = await self._instances.get(message.instance_id)
        ref: MessageReplyRef = await self._replies.dispatch(
            instance,
            message.sender,
            MessageContent(text=command.reply_text),
            message.message_id,
        )
        replied = message.mark_replied(
            reply_ref=ref, now=self._clock.now()
        )
        await self._messages.save(replied)
        await self._events.publish(
            ChannelMessageRepliedEvent(
                message_id=replied.message_id.value,
                instance_id=replied.instance_id.value,
                kind=replied.kind,
                replied_at=replied.updated_at,
                final_status=replied.status,
            )
        )
        return replied


__all__ = [
    "IngestWebhookCommand",
    "IngestWebhookResult",
    "IngestWebhookUseCase",
    "SendReplyCommand",
    "SendChannelReplyUseCase",
]
