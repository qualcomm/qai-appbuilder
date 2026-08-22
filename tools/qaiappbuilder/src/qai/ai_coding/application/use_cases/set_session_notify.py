# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: bind / clear a session's dual-channel notify target.

Backs the legacy ``POST /api/cc/sessions/{id}/wechat_notify`` +
``.../feishu_notify`` routes.  When a binding is set, WebUI turns are
mirror-pushed to the bound WeChat user / Feishu open-id so the user can
follow the conversation from the channel app.

A single use case serves both channels (``"wechat"`` / ``"feishu"``)
parameterised by :attr:`SetSessionNotifyCommand.channel`, mirroring the
route-layer "1 use case / N providers" pattern used elsewhere in this
context.  An empty string is normalised to :data:`None` (clear binding)
to match the legacy ``body.get("...") or None`` semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from qai.ai_coding.application.ports import CodingSessionRepositoryPort
from qai.ai_coding.domain import CodingSession, CodingSessionId
from qai.platform.events import EventBus
from qai.platform.logging import get_logger

logger = get_logger(__name__)

NotifyChannel = Literal["wechat", "feishu"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SetSessionNotifyCommand:
    """Input for :class:`SetSessionNotifyUseCase`."""

    session_id: CodingSessionId
    channel: NotifyChannel
    # Empty string / ``None`` clears the binding.
    user_id: str | None


class SetSessionNotifyUseCase:
    """Application service for (un)binding dual-channel notify targets."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        event_bus: EventBus,
    ) -> None:
        self._repository = repository
        self._event_bus = event_bus

    async def execute(
        self, command: SetSessionNotifyCommand
    ) -> CodingSession:
        session = await self._repository.get(command.session_id)
        # Normalise empty string -> None (legacy "or None" semantics).
        user_id = command.user_id
        if isinstance(user_id, str):
            user_id = user_id.strip() or None
        if command.channel == "wechat":
            session.set_wechat_notify(user_id)
        else:
            session.set_feishu_notify(user_id)
        await self._repository.save(session)
        for event in session.drain_events():
            await self._event_bus.publish(event)
        logger.info(
            "ai_coding.set_session_notify.ok",
            session_id=str(command.session_id),
            channel=command.channel,
            user_id=user_id or "None (cleared)",
        )
        return session


__all__ = ["NotifyChannel", "SetSessionNotifyCommand", "SetSessionNotifyUseCase"]
