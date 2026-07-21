# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""WebUI→Channel sync push bridge (V1 parity: main.py:6754-6815).

When a WebUI chat stream completes successfully and the request carried
``wechat_sync_user_id`` and/or ``feishu_sync_user_id``, this bridge
pushes the user's prompt and the AI's reply to the bound channel user(s)
in fire-and-forget mode (``asyncio.create_task``).

Message format matches V1 exactly:
    "[WebUI 提问]\n{user_text}"
    "[AI 回复]\n{assistant_text}"

This module sits at the apps composition root because pushing a message
crosses from the ``chat`` context into the ``channels`` context (the SSE/WS
routes in ``interfaces/http/routes/chat/`` import this bridge; the bridge
uses ``PushChannelMessageUseCase`` which lives in ``qai.channels``).

Cross-context isolation (§3.2): chat does NOT import channels — this
apps-layer bridge is the join point.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

logger = get_logger(__name__)


__all__ = [
    "schedule_channel_sync_push",
]


def schedule_channel_sync_push(
    *,
    container: "Container",
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    wechat_sync_user_id: str | None = None,
    feishu_sync_user_id: str | None = None,
) -> None:
    """Fire-and-forget push of "[WebUI 提问]/[AI 回复]" to bound channels.

    Called by the SSE/WS route AFTER the stream ends with a successful
    ``END`` frame.  Both ``wechat_sync_user_id`` and ``feishu_sync_user_id``
    are optional — we push to whichever is provided.

    When the explicit ``sync_user_id`` is empty, we fall back to looking
    up the conversation→channel-user binding from the ChannelBindings on
    the relevant channel instance (V1 parity: main.py:6649-6655).

    This is intentionally fire-and-forget: pushing to a channel must
    never delay or break the SSE/WS response to the WebUI client.
    """
    if not assistant_text:
        return

    if wechat_sync_user_id:
        asyncio.create_task(
            _do_push(
                container=container,
                kind="wechat",
                target_user_id=wechat_sync_user_id,
                conversation_id=conversation_id,
                user_text=user_text,
                assistant_text=assistant_text,
            )
        )

    if feishu_sync_user_id:
        asyncio.create_task(
            _do_push(
                container=container,
                kind="feishu",
                target_user_id=feishu_sync_user_id,
                conversation_id=conversation_id,
                user_text=user_text,
                assistant_text=assistant_text,
            )
        )

    # Fallback: if neither sync_user_id was explicitly provided, try to
    # resolve from the ChannelBindings on each running instance (V1 parity:
    # main.py:6650-6655 — ``get_webui_wechat_binding(session_id)`` /
    # ``get_feishu_uid_for_conv(session_id)``).
    if not wechat_sync_user_id and not feishu_sync_user_id:
        asyncio.create_task(
            _push_from_bindings(
                container=container,
                conversation_id=conversation_id,
                user_text=user_text,
                assistant_text=assistant_text,
            )
        )


async def _push_from_bindings(
    *,
    container: "Container",
    conversation_id: str,
    user_text: str,
    assistant_text: str,
) -> None:
    """Resolve sync targets from ChannelBindings and push.

    Iterates over all channel instances (both kinds), checks bindings for
    the given conversation_id, and pushes to any bound user.  Best-effort.
    """
    try:
        from qai.channels.domain import ChannelKind

        channels_ns = getattr(container, "channels", None)
        if channels_ns is None:
            return
        instance_repo = channels_ns.instance_repository
        for kind in (ChannelKind.WECHAT, ChannelKind.FEISHU):
            instances = await instance_repo.list_by_kind(kind)
            for instance in instances:
                if not instance.is_running():
                    continue
                bindings = instance.get_bindings()
                target = bindings.lookup(conversation_id)
                if not target:
                    continue
                await _do_push(
                    container=container,
                    kind=instance.kind.value,
                    target_user_id=target,
                    conversation_id=conversation_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "chat.sync_push.bindings_lookup_failed",
            conversation_id=conversation_id,
            error=str(exc),
        )


async def _do_push(
    *,
    container: "Container",
    kind: str,
    target_user_id: str,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
) -> None:
    """Push "[WebUI 提问]" and "[AI 回复]" to a single channel user.

    Uses :class:`PushChannelMessageUseCase` from the channels context.
    Errors are swallowed (logged at warning) — V1 parity: main.py:6791-6795.
    """
    try:
        from qai.channels.domain import ChannelKind, ChannelUserId
        from qai.channels.application.use_cases.push_message import (
            PushChannelMessageCommand,
        )

        channels_ns = getattr(container, "channels", None)
        if channels_ns is None:
            logger.warning(
                "chat.sync_push.no_channels_context",
                kind=kind,
                target=target_user_id,
            )
            return

        # Find the running instance of the requested kind.
        instance_repo = channels_ns.instance_repository
        target_kind = ChannelKind.from_str(kind)
        instances = await instance_repo.list_by_kind(target_kind)
        target_instance = None
        for inst in instances:
            if inst.is_running():
                target_instance = inst
                break

        if target_instance is None:
            logger.info(
                "chat.sync_push.no_running_instance",
                kind=kind,
                conversation_id=conversation_id,
            )
            return

        push_uc = channels_ns.push_channel_message_use_case
        target_uid = ChannelUserId(target_user_id)
        instance_id = target_instance.instance_id

        # Push user question
        if user_text:
            await push_uc.execute(
                PushChannelMessageCommand(
                    instance_id=instance_id,
                    target=target_uid,
                    text=f"[WebUI 提问]\n{user_text}",
                )
            )

        # Push AI reply
        if assistant_text:
            await push_uc.execute(
                PushChannelMessageCommand(
                    instance_id=instance_id,
                    target=target_uid,
                    text=f"[AI 回复]\n{assistant_text}",
                )
            )

        logger.info(
            "chat.sync_push.success",
            kind=kind,
            conversation_id=conversation_id,
            target=target_user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "chat.sync_push.failed",
            kind=kind,
            conversation_id=conversation_id,
            target=target_user_id,
            error=str(exc),
        )
