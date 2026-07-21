# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer reverse bridge: ai_coding → channels (6 interop items).

Architecture cleanup (A-1 step2): :class:`AiCodingToChannelNotifier`
previously shared a module with the forward
:class:`apps.api._ai_coding_channel_bridge.AiCodingChannelBridge`,
inflating that file past the cohesion soft-cap.  The reverse-direction
notifier crosses the same ai_coding↔channels boundary but is an
independent collaborator (it pushes outbound notifications rather than
routing inbound messages), so it now lives in its own module.

``apps.api._ai_coding_channel_bridge`` re-exports the class so its
``__all__`` and every existing importer remain unchanged.

This module lives at the apps composition root because it crosses the
ai_coding↔channels boundary; neither domain imports the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.platform.logging import get_logger

from qai.channels.domain import (
    ChannelInstanceId,
    ChannelUserId,
)
from qai.channels.application.use_cases.push_message import (
    PushChannelMessageCommand,
)
from qai.channels.application.use_cases.session_index import (
    BindSessionIndexCommand,
)

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

logger = get_logger(__name__)

__all__ = ["AiCodingToChannelNotifier"]


# ---------------------------------------------------------------------------
# Reverse bridge: ai_coding → channels (6 interop items)
# ---------------------------------------------------------------------------
class AiCodingToChannelNotifier:
    """Apps-layer bridge for ai_coding → channels direction.

    Restores 6 v1 interop capabilities:

    1. **set_user_cc_session** — bind a channel user to a CC/OC session
       (v1: ``wechat_channel.set_user_cc_session`` / feishu equivalent).
    2. **wechat_notify** — push AI coding completion to WeChat user.
    3. **feishu_notify** — push AI coding completion to Feishu user.
    4. **done_push** — push long-task completion notification.
    5. **turn_warning_sync** — push over-turn-count warning.
    6. **restore_owner_bind** — re-bind session owner on restore/set_active.

    All methods are best-effort: failures are logged but never raise to
    the caller, matching v1's ``asyncio.create_task`` fire-and-forget
    pattern.

    This class lives at the apps composition root because it crosses
    the ai_coding↔channels boundary; neither domain imports the other.
    """

    __slots__ = ("_container",)

    def __init__(self, *, container: "Container") -> None:
        self._container = container

    # ------------------------------------------------------------------
    # 1. set_user_cc_session — bind channel user → coding session
    # ------------------------------------------------------------------
    async def set_user_cc_session(
        self,
        *,
        instance_id: str,
        channel_user_id: str,
        coding_session_id: str | None,
        internal_user_id: str | None = None,
    ) -> bool:
        """Bind (or unbind) a channel user to a CC/OC session.

        Mirrors v1 ``wechat_channel.set_user_cc_session(user_id, session_id)``
        and the feishu equivalent.  The channel dispatch layer uses this
        mapping to decide whether an inbound message should route to
        ai_coding or to normal chat.

        Returns ``True`` on success.
        """
        channels = getattr(self._container, "channels", None)
        if channels is None:
            return False
        bind_uc = getattr(channels, "bind_session_index_use_case", None)
        if bind_uc is None:
            return False
        try:
            await bind_uc.execute(
                BindSessionIndexCommand(
                    instance_id=ChannelInstanceId(instance_id),
                    channel_user_id=ChannelUserId(channel_user_id),
                    internal_user_id=internal_user_id,
                    coding_session_id=coding_session_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ai_coding_channel.set_user_cc_session_failed",
                instance_id=instance_id,
                channel_user_id=channel_user_id,
                error=str(exc),
            )
            return False
        return True

    # ------------------------------------------------------------------
    # 2. wechat_notify — push AI coding result to WeChat user
    # ------------------------------------------------------------------
    async def wechat_notify(
        self,
        *,
        instance_id: str,
        wechat_user_id: str,
        question: str,
        reply: str,
        session_id: str = "",
    ) -> bool:
        """Push an AI coding completion notification to a WeChat user.

        Mirrors v1 ``_do_wechat_sync``: sends ``[WebUI 提问]\\n{q}`` and
        ``[Claude Code 回复]\\n{r}`` as two consecutive messages via
        :class:`PushChannelMessageUseCase`.

        Returns ``True`` when both messages are delivered.
        """
        q_text = f"[WebUI 提问]\n{question}"
        r_text = f"[Claude Code 回复]\n{reply}"
        ok1 = await self._push_text(
            instance_id=instance_id,
            target_user_id=wechat_user_id,
            text=q_text,
        )
        ok2 = await self._push_text(
            instance_id=instance_id,
            target_user_id=wechat_user_id,
            text=r_text,
        )
        if ok1 and ok2:
            logger.info(
                "ai_coding_channel.wechat_notify_done",
                session_id=session_id,
                user_id=wechat_user_id,
            )
        return ok1 and ok2

    # ------------------------------------------------------------------
    # 3. feishu_notify — push AI coding result to Feishu user
    # ------------------------------------------------------------------
    async def feishu_notify(
        self,
        *,
        instance_id: str,
        feishu_user_id: str,
        question: str,
        reply: str,
        session_id: str = "",
    ) -> bool:
        """Push an AI coding completion notification to a Feishu user.

        Mirrors v1 ``_do_feishu_sync``: sends ``[WebUI 提问]\\n{q}`` and
        ``[Claude Code 回复]\\n{r}`` as two consecutive messages via
        :class:`PushChannelMessageUseCase`.

        Returns ``True`` when both messages are delivered.
        """
        q_text = f"[WebUI 提问]\n{question}"
        r_text = f"[Claude Code 回复]\n{reply}"
        ok1 = await self._push_text(
            instance_id=instance_id,
            target_user_id=feishu_user_id,
            text=q_text,
        )
        ok2 = await self._push_text(
            instance_id=instance_id,
            target_user_id=feishu_user_id,
            text=r_text,
        )
        if ok1 and ok2:
            logger.info(
                "ai_coding_channel.feishu_notify_done",
                session_id=session_id,
                user_id=feishu_user_id,
            )
        return ok1 and ok2

    # ------------------------------------------------------------------
    # 4. done_push — long-task completion push
    # ------------------------------------------------------------------
    async def done_push(
        self,
        *,
        instance_id: str,
        target_user_id: str,
        session_id: str,
        summary: str,
    ) -> bool:
        """Push a long-task completion notification to a channel user.

        Mirrors v1's ``broadcast_fn({"type": "cc_session_updated", ...})``
        pattern, but pushes directly to the user's channel rather than
        relying on WebUI broadcast.  Used when a CC/OC session completes
        a long-running task and the user is waiting on the channel side.

        Returns ``True`` on success.
        """
        text = f"✅ 任务完成\n{summary}" if summary else "✅ 任务完成"
        ok = await self._push_text(
            instance_id=instance_id,
            target_user_id=target_user_id,
            text=text,
        )
        if ok:
            logger.info(
                "ai_coding_channel.done_push_sent",
                session_id=session_id,
                user_id=target_user_id,
            )
        return ok

    # ------------------------------------------------------------------
    # 5. turn_warning_sync — over-turn-count warning push
    # ------------------------------------------------------------------
    async def turn_warning_sync(
        self,
        *,
        instance_id: str,
        target_user_id: str,
        session_id: str,
        warning_message: str,
    ) -> bool:
        """Push a turn-count warning to a channel user.

        Mirrors v1's ``turn_warning`` event handling in
        ``backend/ai_coding/api_routes.py:982-1005``: when the CC/OC
        session exceeds the configured turn threshold, the warning is
        synchronously pushed to the bound WeChat/Feishu user.

        Returns ``True`` on success.
        """
        ok = await self._push_text(
            instance_id=instance_id,
            target_user_id=target_user_id,
            text=warning_message,
        )
        if ok:
            logger.info(
                "ai_coding_channel.turn_warning_sent",
                session_id=session_id,
                user_id=target_user_id,
            )
        return ok

    # ------------------------------------------------------------------
    # 6. restore_owner_bind — re-bind session owner on restore
    # ------------------------------------------------------------------
    async def restore_owner_bind(
        self,
        *,
        instance_id: str,
        channel_user_id: str,
        coding_session_id: str,
        internal_user_id: str | None = None,
    ) -> bool:
        """Re-bind a channel user → coding session after a restart/restore.

        Mirrors v1's ``set_active`` endpoint logic
        (``backend/ai_coding/api_routes.py:1199-1315``): when a session
        is restored from SQLite, the channel-side session index is
        automatically updated so future inbound messages from the channel
        user route to the correct CC/OC session.

        Semantically equivalent to :meth:`set_user_cc_session` but
        named separately for clarity in call-site documentation.

        Returns ``True`` on success.
        """
        return await self.set_user_cc_session(
            instance_id=instance_id,
            channel_user_id=channel_user_id,
            coding_session_id=coding_session_id,
            internal_user_id=internal_user_id,
        )

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------
    async def _push_text(
        self,
        *,
        instance_id: str,
        target_user_id: str,
        text: str,
    ) -> bool:
        """Push a text message to a channel user via PushChannelMessageUseCase.

        Returns ``True`` on successful delivery (all chunks ok).
        """
        channels = getattr(self._container, "channels", None)
        if channels is None:
            logger.warning(
                "ai_coding_channel.push_text_no_channels_context",
                instance_id=instance_id,
            )
            return False
        push_uc = getattr(
            channels, "push_channel_message_use_case", None
        )
        if push_uc is None:
            logger.warning(
                "ai_coding_channel.push_text_no_push_uc",
                instance_id=instance_id,
            )
            return False
        try:
            result = await push_uc.execute(
                PushChannelMessageCommand(
                    instance_id=ChannelInstanceId(instance_id),
                    target=ChannelUserId(target_user_id),
                    text=text,
                )
            )
            return result.all_ok
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ai_coding_channel.push_text_failed",
                instance_id=instance_id,
                target=target_user_id,
                error=str(exc),
            )
            return False
