# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: QR-based login flow (personal WeChat / cc_handler-style)."""

from __future__ import annotations

from qai.platform.events import EventBus
from qai.platform.time import Clock

from qai.channels.application.ports import (
    ChannelInstanceRepositoryPort,
    QrLoginPort,
)
from qai.channels.domain import (
    ChannelInstanceId,
    QrLoginChallenge,
    QrLoginConfirmedEvent,
    QrLoginIssuedEvent,
    QrLoginScannedEvent,
    QrLoginStatus,
)


class IssueQrLoginUseCase:
    """Ask the provider for a fresh QR challenge."""

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        qr: QrLoginPort,
        events: EventBus,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._qr = qr
        self._events = events
        self._clock = clock

    async def execute(
        self, instance_id: ChannelInstanceId
    ) -> QrLoginChallenge:
        instance = await self._instances.get(instance_id)
        challenge = await self._qr.issue(instance)
        await self._events.publish(
            QrLoginIssuedEvent(
                challenge_id=challenge.challenge_id,
                instance_id=instance.instance_id.value,
                kind=instance.kind,
                issued_at=challenge.issued_at,
            )
        )
        return challenge


class ConfirmQrLoginUseCase:
    """Poll / confirm a previously-issued QR challenge.

    Uses :meth:`QrLoginPort.check_status` to learn the current state and
    :meth:`QrLoginPort.confirm` to complete the flow.  The use case
    publishes the appropriate event for whatever stage the challenge is
    in.
    """

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        qr: QrLoginPort,
        events: EventBus,
        clock: Clock,
    ) -> None:
        self._instances = instances
        self._qr = qr
        self._events = events
        self._clock = clock

    async def execute(
        self,
        instance_id: ChannelInstanceId,
        challenge_id: str,
        *,
        confirm: bool = False,
    ) -> QrLoginChallenge:
        instance = await self._instances.get(instance_id)
        if confirm:
            confirmed = await self._qr.confirm(instance, challenge_id)
            await self._events.publish(
                QrLoginConfirmedEvent(
                    challenge_id=confirmed.challenge_id,
                    instance_id=instance.instance_id.value,
                    confirmed_at=self._clock.now(),
                    final_status=confirmed.status,
                )
            )
            return confirmed
        current = await self._qr.check_status(instance, challenge_id)
        if current.status is QrLoginStatus.SCANNED:
            await self._events.publish(
                QrLoginScannedEvent(
                    challenge_id=current.challenge_id,
                    instance_id=instance.instance_id.value,
                    scanned_at=self._clock.now(),
                )
            )
        return current


__all__ = ["IssueQrLoginUseCase", "ConfirmQrLoginUseCase"]
