# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""ChannelMessage entity and its lifecycle.

Replaces the legacy 40+39 module-level globals in
``backend/channels/feishu/channel.py`` /
``backend/channels/wechat/channel.py`` (see
``docs/90-refactor/inventory/03-imports-dependencies.md``): every piece
of in-flight message state is now carried *on the message itself*
rather than in a process-wide dict.

A :class:`ChannelMessage` is created when an inbound webhook produces
a :class:`WebhookPayload`; it then walks through the
:class:`~qai.channels.domain.value_objects.ChannelMessageStatus` state
machine until it is either ``replied`` or ``failed``.

The aggregate is ``frozen=True`` — every transition returns a new
instance.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
)
from qai.platform.time import ensure_aware_utc

from .errors import ChannelMessageStateError
from .ids import ChannelInstanceId, ChannelMessageId, ChannelUserId
from .kinds import ChannelKind
from .value_objects import (
    ChannelMessageStatus,
    Command,
    MessageContent,
    MessageReplyRef,
)

_MAX_REASON_LENGTH = 1024
_MAX_PROVIDER_REF_LENGTH = 256


@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelMessage:
    """An inbound channel message and its in-flight state."""

    message_id: ChannelMessageId
    instance_id: ChannelInstanceId
    kind: ChannelKind
    sender: ChannelUserId
    provider_event_id: str
    content: MessageContent
    arrived_at: datetime
    status: ChannelMessageStatus = ChannelMessageStatus.RECEIVED
    parsed_command: Command | None = None
    reply_ref: MessageReplyRef | None = None
    failure_reason: str = ""
    updated_at: datetime
    #: For group/channel messages: the group chat_id to reply into.
    #: ``None`` for private (p2p) messages — reply goes to ``sender``.
    group_id: str | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        assert_non_empty(
            self.provider_event_id,
            name="ChannelMessage.provider_event_id",
        )
        assert_max_length(
            self.provider_event_id,
            max_length=_MAX_PROVIDER_REF_LENGTH,
            name="ChannelMessage.provider_event_id",
        )
        assert_max_length(
            self.failure_reason,
            max_length=_MAX_REASON_LENGTH,
            name="ChannelMessage.failure_reason",
        )
        for attr in ("arrived_at", "updated_at"):
            current = getattr(self, attr)
            normalised = ensure_aware_utc(current)
            if normalised is not current:
                object.__setattr__(self, attr, normalised)

    @classmethod
    def receive(
        cls,
        *,
        message_id: ChannelMessageId,
        instance_id: ChannelInstanceId,
        kind: ChannelKind,
        sender: ChannelUserId,
        provider_event_id: str,
        content: MessageContent,
        arrived_at: datetime,
        group_id: str | None = None,
    ) -> ChannelMessage:
        """Factory invoked by :class:`IngestWebhookUseCase`."""

        return cls(
            message_id=message_id,
            instance_id=instance_id,
            kind=kind,
            sender=sender,
            provider_event_id=provider_event_id,
            content=content,
            arrived_at=arrived_at,
            status=ChannelMessageStatus.RECEIVED,
            parsed_command=None,
            reply_ref=None,
            failure_reason="",
            updated_at=arrived_at,
            group_id=group_id,
        )

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def mark_parsed(
        self, *, command: Command, now: datetime
    ) -> ChannelMessage:
        """Attach a parsed :class:`Command` and move to ``parsed``."""

        if self.status is not ChannelMessageStatus.RECEIVED:
            raise ChannelMessageStateError(
                f"cannot parse message {self.message_id.value!r} "
                f"from status {self.status.value!r}",
                current_status=self.status.value,
                attempted="mark_parsed",
            )
        return replace(
            self,
            status=ChannelMessageStatus.PARSED,
            parsed_command=command,
            updated_at=ensure_aware_utc(now),
        )

    def mark_dispatched(self, *, now: datetime) -> ChannelMessage:
        """Move from ``parsed`` → ``dispatched``."""

        if self.status is not ChannelMessageStatus.PARSED:
            raise ChannelMessageStateError(
                f"cannot dispatch message {self.message_id.value!r} "
                f"from status {self.status.value!r}",
                current_status=self.status.value,
                attempted="mark_dispatched",
            )
        return replace(
            self,
            status=ChannelMessageStatus.DISPATCHED,
            updated_at=ensure_aware_utc(now),
        )

    def mark_replied(
        self, *, reply_ref: MessageReplyRef, now: datetime
    ) -> ChannelMessage:
        """Attach a :class:`MessageReplyRef` and move to ``replied``."""

        if self.status is not ChannelMessageStatus.DISPATCHED:
            raise ChannelMessageStateError(
                f"cannot mark message {self.message_id.value!r} replied "
                f"from status {self.status.value!r}",
                current_status=self.status.value,
                attempted="mark_replied",
            )
        if reply_ref.inbound_message_id != self.message_id:
            raise ChannelMessageStateError(
                "reply_ref.inbound_message_id does not match this message",
                current_status=self.status.value,
                attempted="mark_replied",
            )
        return replace(
            self,
            status=ChannelMessageStatus.REPLIED,
            reply_ref=reply_ref,
            updated_at=ensure_aware_utc(now),
        )

    def mark_failed(
        self, *, reason: str, now: datetime
    ) -> ChannelMessage:
        """Move into the ``failed`` terminal state with ``reason``."""

        assert_non_empty(reason, name="ChannelMessage.reason")
        if self.status in (
            ChannelMessageStatus.REPLIED,
            ChannelMessageStatus.FAILED,
        ):
            raise ChannelMessageStateError(
                f"message {self.message_id.value!r} is already terminal "
                f"(status={self.status.value!r})",
                current_status=self.status.value,
                attempted="mark_failed",
            )
        return replace(
            self,
            status=ChannelMessageStatus.FAILED,
            failure_reason=reason,
            updated_at=ensure_aware_utc(now),
        )

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------
    def is_terminal(self) -> bool:
        return self.status in (
            ChannelMessageStatus.REPLIED,
            ChannelMessageStatus.FAILED,
        )


__all__ = ["ChannelMessage"]
