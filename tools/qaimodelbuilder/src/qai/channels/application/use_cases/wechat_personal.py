# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: personal-WeChat logout orchestration (R16).

The ``POST /api/wechat/logout`` route previously inlined a two-step
orchestration: tear down the live wechatbot SDK ``Bot`` via the
QR-login adapter, then halt the channel instance's long-poll transport
via :class:`StopChannelInstanceUseCase`.  That two-step coordination is
application-level business logic, so this use case owns it and the route
becomes a thin caller.

Behaviour is byte-for-byte identical to the inline route logic (v2.7 §3
immutability — logout behaviour unchanged): logout the SDK bot first,
then stop the channel instance (mirrors the legacy
``stop_channel`` ordering).  The kind validation (instance must be a
WeChat instance) stays in the route since it maps a path-prefix slug to
an HTTP ``NotFoundError`` envelope — a transport concern.
"""

from __future__ import annotations

from qai.channels.application.ports import WechatPersonalQrLoginPort
from qai.channels.application.use_cases.manage_lifecycle import (
    StopChannelInstanceUseCase,
)
from qai.channels.domain import ChannelInstance, ChannelInstanceId


class LogoutWechatPersonalUseCase:
    """Logout personal WeChat and stop the channel instance.

    Wraps the two collaborators the route layer otherwise coordinated by
    hand:

    1. :meth:`WechatPersonalQrLoginPort.logout` — tears down the active
       wechatbot SDK ``Bot`` held by the QR-login adapter.
    2. :meth:`StopChannelInstanceUseCase.execute` — halts the long-poll
       transport so no further inbound messages are pumped (mirrors the
       legacy ``stop_channel``).
    """

    def __init__(
        self,
        *,
        qr_login: WechatPersonalQrLoginPort,
        stop_use_case: StopChannelInstanceUseCase,
    ) -> None:
        self._qr_login = qr_login
        self._stop_use_case = stop_use_case

    async def execute(
        self, instance_id: ChannelInstanceId
    ) -> ChannelInstance:
        """Tear down the SDK bot, then stop the channel instance.

        Returns the stopped :class:`ChannelInstance` so callers can
        confirm the terminal state if desired (the route discards it and
        returns ``{"ok": True}`` to preserve the existing wire shape).
        """
        await self._qr_login.logout()
        return await self._stop_use_case.execute(instance_id)


__all__ = ["LogoutWechatPersonalUseCase"]
