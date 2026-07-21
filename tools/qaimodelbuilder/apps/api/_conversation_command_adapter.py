# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Concrete :class:`ConversationCommandPort` for channel slash commands.

Wires the five chat-conversation slash commands (``/list`` / ``/use`` /
``/status`` / ``/rename`` / ``/delete``) — declared as a pure Protocol in
:mod:`qai.channels.application.use_cases.conversation_commands` — to the
real ``qai.chat`` conversation-management use cases.  This adapter lives in
the apps composition root precisely because it must cross the
channels→chat context boundary (``qai.channels`` may not import
``qai.chat``; the apps layer may).

Behaviour parity (事实来源 = V0.5
``backend/channels/session_commands.py:288-600``, 微信/飞书已验证可正确工作):

* ``/list [N]`` — list the channel user's recent conversations (default 5,
  cap 50), newest first, with relative-time + round-count, ``▶`` marking the
  current one (``session_commands.py:288-344``).
* ``/use <n>`` — switch the current conversation to the n-th listed item so
  the next inbound message resumes that conversation's history
  (``session_commands.py:347-436``).
* ``/status`` — show name / round count / current model / tool-call count /
  approximate context size (``session_commands.py:439-525``).
* ``/rename <name>`` — rename the current conversation
  (``session_commands.py:528-571``).
* ``/delete`` — delete the current conversation and start fresh
  (``session_commands.py:574-600``).

Architecture notes
-------------------
* The adapter resolves the **current** conversation for a channel user the
  same way :class:`ChatMessageBridge` does: the in-memory
  ``_chat_message_bridge._USER_CONV_IDS`` cache first (V0.5
  ``_user_conv_ids``), falling back to the persisted
  ``ConversationRepositoryPort.find_latest_by_channel_user`` lookup (V0.5
  ``get_latest_wechat/feishu_conversation``).  We reuse the bridge's
  module-level cache rather than inventing a second source of truth
  (State-Truth-First, AGENTS.md §3.10) so ``/use`` / ``/delete`` stay
  consistent with the normal-chat resume path.
* Listing filters the chat repository's flat ``list()`` projection by the
  conversation ``meta`` ``{"source", "channel_user_id"}`` pair the bridge
  stamps on every channel conversation, recovering V0.5's per-user scoping
  (``_list_user_conversations_wechat`` / ``_feishu``) without adding a new
  repository method.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from qai.channels.application.use_cases.conversation_commands import (
    ConversationInfo,
)
from qai.channels.domain import ChannelInstanceId, ChannelUserId
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.di import Container

logger = get_logger(__name__)

# V0.5 ``session_commands.py:278-280`` — default list size 5, hard cap 50.
_DEFAULT_LIST_COUNT = 5
_MAX_LIST_COUNT = 50


class ChatConversationCommandAdapter:
    """Implements ``ConversationCommandPort`` over ``qai.chat`` use cases.

    The five methods mirror the V0.5 ``session_commands`` handlers but build
    on the new chat use cases (``ListConversationsUseCase`` /
    ``RenameConversationUseCase`` / ``DeleteConversationUseCase`` /
    ``GetConversationMessagesUseCase``) and the chat-bridge conversation
    cache, never re-implementing persistence.
    """

    __slots__ = ("_container",)

    def __init__(self, *, container: "Container") -> None:
        self._container = container

    # ------------------------------------------------------------------
    # Port methods
    # ------------------------------------------------------------------
    async def list_conversations(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        limit: int = 10,
    ) -> list[ConversationInfo]:
        """Return the channel user's recent conversations (newest first).

        V0.5 ``session_commands.py:288-344`` + ``_list_user_conversations_*``:
        scoped to THIS channel user (``meta.source`` + ``meta.channel_user_id``),
        ordered by ``updated_at`` DESC, capped at ``limit``.
        """
        chat = getattr(self._container, "chat", None)
        list_uc = getattr(chat, "list_conversations_use_case", None)
        if list_uc is None:
            return []

        source, channel_user = self._channel_keys(instance_id, user_id)
        current_id = await self._current_conversation_id(instance_id, user_id)

        # The chat repo's flat ``list()`` is not channel-scoped, so over-fetch
        # then filter by the channel-source meta the bridge stamps.  A
        # generous pool keeps the newest ``limit`` for this user even when the
        # process serves several channel users.
        from qai.chat.application.use_cases.conversation_management import (
            ListConversationsInput,
        )

        pool = min(_MAX_LIST_COUNT * 4, 200)
        items = await list_uc.execute(ListConversationsInput(limit=pool, offset=0))

        scoped = [
            item for item in items if self._matches_channel_user(item, source, channel_user)
        ]
        scoped = scoped[: max(1, min(limit, _MAX_LIST_COUNT))]

        out: list[ConversationInfo] = []
        for item in scoped:
            conv = item.conversation
            out.append(
                ConversationInfo(
                    conversation_id=conv.id.value,
                    title=(conv.title or "（未命名）"),
                    updated_at=_format_relative_time(conv.updated_at),
                    round_count=int(getattr(item, "round_count", 0) or 0),
                    is_current=(current_id is not None and conv.id.value == current_id),
                )
            )
        return out

    async def use_conversation(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        index: int,
    ) -> str:
        """Switch the current conversation to the 1-based ``index``.

        V0.5 ``session_commands.py:347-436``: resolve the index against the
        same scoped list ``/list`` shows, then re-point the channel user's
        cached conversation id so the next inbound message resumes that
        conversation's history.  Reply ``✅ 已切换到会话：… 共 N 轮 …``.
        """
        convs = await self.list_conversations(
            instance_id, user_id, limit=_MAX_LIST_COUNT
        )
        if not convs:
            return "\U0001f4cb 暂无历史会话记录"
        if index > len(convs):
            return (
                f"\u26a0\ufe0f 编号 {index} 超出范围（共 {len(convs)} 条历史）\n\n"
                "发送 /list 查看列表"
            )

        target = convs[index - 1]
        # Re-point the bridge's per-user conversation cache + clear any
        # pending force-new flag so the NEXT message resumes this one
        # (V0.5 set ``_user_conv_ids[user] = target`` and dropped
        # ``force_new_conv_users``).
        from . import _chat_message_bridge as bridge

        key = (instance_id.value, user_id.value)
        bridge._USER_CONV_IDS[key] = target.conversation_id
        bridge._FORCE_NEW_CONV_USERS.discard(key)

        return (
            f"\u2705 已切换到会话：{target.title}\n"
            f"共 {target.round_count} 轮对话历史\n\n"
            "直接发消息即可继续对话。"
        )

    async def get_status(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> str:
        """Return the current conversation's status block.

        V0.5 ``session_commands.py:439-525``: name / round count / current
        model / tool-call count / approximate context size.  When no
        conversation is active yet, mirror V0.5's "没有活跃会话" reply.
        """
        conv_id = await self._current_conversation_id(instance_id, user_id)
        model_display = await self._channel_model_display(instance_id)

        if not conv_id:
            return "\U0001f4ca 当前没有活跃会话\n\n直接发消息即可开始新对话。"

        chat = getattr(self._container, "chat", None)
        conversations = getattr(chat, "conversations", None)
        title = "（未命名）"
        messages: list[Any] = []
        conv: Any = None
        if conversations is not None:
            try:
                conv = await conversations.get(_conversation_id_of(conv_id))
                title = conv.title or title
                messages = list(getattr(conv, "messages", ()) or ())
            except Exception:  # noqa: BLE001 — status must never 500
                pass

        round_count = sum(1 for m in messages if _role_value(m) == "user")
        tool_call_count = sum(1 for m in messages if _role_value(m) == "tool")
        # Cloud-first token accounting: read the per-conversation running
        # counter (``chat_conversation.full_history_tokens``, maintained per
        # turn) instead of a local BPE re-tokenisation. State-Truth-First
        # (AGENTS.md 铁律 1): the running counter reflects the provider's
        # measured wire size, strictly more accurate than re-estimating.
        ctx_tokens = int(getattr(conv, "full_history_tokens", None) or 0)

        lines = [
            "\U0001f4ca 当前会话状态",
            f"名称：{title}",
            f"对话轮数：{round_count} 轮",
            f"当前模型：{model_display}",
        ]
        if tool_call_count > 0:
            lines.append(f"工具调用：{tool_call_count} 次")
        if ctx_tokens > 0:
            lines.append(f"上下文大小：{_fmt_tokens(ctx_tokens)} tokens（含历史）")
        lines.append(f"会话 ID：{conv_id[:12]}…")
        lines.append("\n\U0001f4a1 /rename <名称> 重命名  /delete 删除当前会话")
        return "\n".join(lines)

    async def rename_conversation(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        new_name: str,
    ) -> str:
        """Rename the current conversation.

        V0.5 ``session_commands.py:528-571``: update title only; reply
        ``✅ 当前会话已重命名为：…``.
        """
        conv_id = await self._current_conversation_id(instance_id, user_id)
        if not conv_id:
            return "\u26a0\ufe0f 当前没有活跃会话，请先发送一条消息开始对话"

        chat = getattr(self._container, "chat", None)
        rename_uc = getattr(chat, "rename_conversation_use_case", None)
        if rename_uc is None:
            return "\u26a0\ufe0f 重命名功能当前不可用"

        from qai.chat.application.use_cases.conversation_management import (
            RenameConversationInput,
        )

        await rename_uc.execute(
            RenameConversationInput(
                conversation_id=_conversation_id_of(conv_id),
                new_title=new_name,
            )
        )
        return f"\u2705 当前会话已重命名为：{new_name}"

    async def delete_conversation(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> str:
        """Delete the current conversation and start fresh.

        V0.5 ``session_commands.py:574-600``: delete the conversation,
        drop the cache, mark force-new so the next message opens a new
        session; reply ``🗑 当前会话已删除，已开启新会话。``.  Reuses the
        bridge's :func:`delete_conversation_for_user` (which clears the cache
        AND deletes from the DB via the chat delete use case) so there is a
        single deletion path.
        """
        from . import _chat_message_bridge as bridge

        await bridge.delete_conversation_for_user(
            container=self._container,
            instance_id=instance_id,
            channel_user_id=user_id.value,
        )
        # Force the next inbound message to mint a fresh conversation
        # (V0.5 ``force_new_conv_users.add(user_id)``).
        bridge.mark_force_new_conv(
            instance_id=instance_id.value, user_id=user_id.value
        )
        return "\U0001f5d1 当前会话已删除，已开启新会话。"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _channel_keys(
        instance_id: ChannelInstanceId, user_id: ChannelUserId
    ) -> tuple[str, str]:
        # ``source`` is the channel kind; the bridge does not retain the kind
        # on the bare ids here, so we recover it from the persisted meta when
        # filtering (``_matches_channel_user`` accepts any source whose
        # channel_user_id matches when ``source`` is empty).
        return ("", user_id.value)

    @staticmethod
    def _matches_channel_user(item: Any, source: str, channel_user: str) -> bool:
        meta = getattr(getattr(item, "conversation", None), "meta", None)
        if not isinstance(meta, dict):
            return False
        if str(meta.get("channel_user_id") or "") != channel_user:
            return False
        if source and str(meta.get("source") or "") != source:
            return False
        return True

    async def _current_conversation_id(
        self, instance_id: ChannelInstanceId, user_id: ChannelUserId
    ) -> str | None:
        """Resolve the current conversation id (cache → persistence).

        V0.5 ``_user_conv_ids.get(user)`` with the
        ``get_latest_*_conversation`` restart-recovery fallback.
        """
        from . import _chat_message_bridge as bridge

        cached = bridge._USER_CONV_IDS.get((instance_id.value, user_id.value))
        if cached:
            return cached

        chat = getattr(self._container, "chat", None)
        conversations = getattr(chat, "conversations", None)
        find_latest = getattr(conversations, "find_latest_by_channel_user", None)
        if find_latest is None:
            return None
        try:
            # ``source`` unknown at this layer; the repo matches both source
            # and channel_user_id, but the channel_user_id alone is unique in
            # practice.  We try the common channel kinds.
            for source in ("wechat", "feishu", "wecom"):
                conv = await find_latest(source, user_id.value)
                if conv is not None:
                    bridge._USER_CONV_IDS[(instance_id.value, user_id.value)] = (
                        conv.id.value
                    )
                    return conv.id.value
        except Exception:  # noqa: BLE001 — recovery is best-effort
            return None
        return None

    async def _channel_model_display(self, instance_id: ChannelInstanceId) -> str:
        """Return the channel's configured model id, or "跟随全局设置".

        V0.5 ``session_commands.py:481`` ``model_id or "跟随全局设置"``.
        """
        channels = getattr(self._container, "channels", None)
        get_settings = getattr(channels, "get_channel_settings_use_case", None)
        if get_settings is None:
            return "跟随全局设置"
        try:
            settings_vo = await get_settings.execute(instance_id)
            model_id = getattr(getattr(settings_vo, "model", None), "model_id", "")
            return str(model_id) if model_id else "跟随全局设置"
        except Exception:  # noqa: BLE001
            return "跟随全局设置"


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------
def _conversation_id_of(raw: str):  # type: ignore[no-untyped-def]
    from qai.chat.domain.ids import ConversationId

    return ConversationId.of(raw)


def _role_value(message: Any) -> str:
    role = getattr(message, "role", "")
    return getattr(role, "value", role) or ""


def _fmt_tokens(n: int) -> str:
    """V0.5 ``session_commands.py:474-479`` token formatting."""
    if n <= 0:
        return "0"
    if n < 10_000:
        return f"~{n}"
    return f"~{round(n / 1000)}K"


def _format_relative_time(value: Any) -> str:
    """Relative-time string (V0.5 ``session_commands.py:680-703 _format_time``).

    Accepts a ``datetime`` (V2 ``Conversation.updated_at``) or an ISO string.
    """
    if value is None:
        return "未知时间"
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        seconds = (now - dt).total_seconds()
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return f"{int(seconds // 60)} 分钟前"
        if seconds < 86400:
            return f"{int(seconds // 3600)} 小时前"
        if seconds < 86400 * 7:
            return f"{int(seconds // 86400)} 天前"
        return dt.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        s = str(value)
        return s[:10] if len(s) >= 10 else s
