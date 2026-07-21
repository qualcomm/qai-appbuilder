# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: channel conversation slash-command handling.

The five conversation management commands (``/list``, ``/use``, ``/status``,
``/rename``, ``/delete``) operate on **chat conversations** — NOT on
ai_coding sessions.  The legacy v1 code in
``backend/channels/session_commands.py`` routed these to the
``handle_list_command`` / ``handle_use_command`` /
``handle_status_command`` / ``handle_rename_command`` /
``handle_delete_command`` handlers which manipulated chat history.

In the new architecture the channels context cannot import qai.chat
(§3.2 isolation).  This module defines:

* :data:`CONVERSATION_COMMAND_VERBS` — the authoritative set of verbs
  that MUST route to conversation (chat) handling, NOT ai_coding.
  The dispatch bridge in :mod:`apps.api._channel_dispatch_bridge` must
  reference this constant when deciding route selection (step 5).
* :class:`ConversationCommandPort` — a Protocol abstracting the five
  operations so the implementation can live in the apps composition
  root (which may import qai.chat).
* :class:`HandleConversationCommandUseCase` — dispatches the parsed
  :class:`Command` to the appropriate port method and returns a
  user-facing text reply.

Command routing contract (v1 parity)
-------------------------------------

+-----------+----------------------------------------------+
| Verb      | Behaviour                                    |
+-----------+----------------------------------------------+
| ``list``  | List recent chat conversations               |
| ``use``   | Switch to a specified conversation by index  |
| ``status``| Show current conversation status             |
| ``rename``| Rename the current conversation              |
| ``delete``| Delete current conversation, start fresh     |
+-----------+----------------------------------------------+

Verbs that SHOULD route to ai_coding (unchanged):
``cc``, ``oc``, ``stop``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from qai.channels.domain import (
    ChannelInstanceId,
    ChannelMessage,
    ChannelUserId,
    Command,
)

__all__ = [
    "CONVERSATION_COMMAND_VERBS",
    "ConversationCommandPort",
    "HandleConversationCommandUseCase",
]


# ---------------------------------------------------------------------------
# Authoritative verb set — the dispatch bridge MUST check membership here
# to decide chat-conversation vs ai_coding routing.
# ---------------------------------------------------------------------------

#: The five conversation management verbs that operate on chat
#: conversations.  These must NEVER be routed to ai_coding_bridge.
#: Only ``cc``, ``oc``, and ``stop`` should go to ai_coding.
CONVERSATION_COMMAND_VERBS: frozenset[str] = frozenset(
    {"list", "use", "status", "rename", "delete"}
)


# ---------------------------------------------------------------------------
# Port (Protocol) — implemented in apps layer composition root
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationInfo:
    """Lightweight conversation summary returned by list/status ops."""

    conversation_id: str
    title: str
    updated_at: str
    round_count: int = 0
    is_current: bool = False


@runtime_checkable
class ConversationCommandPort(Protocol):
    """Abstracts chat-conversation CRUD for the 5 slash commands.

    Implementations live in the apps composition root (e.g.
    ``apps.api._channel_dispatch_bridge`` or a dedicated adapter)
    because they need access to ``qai.chat`` use cases which
    ``qai.channels`` cannot import directly (§3.2).
    """

    async def list_conversations(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        limit: int = 10,
    ) -> list[ConversationInfo]:
        """Return recent conversations for the channel user."""
        ...

    async def use_conversation(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        index: int,
    ) -> str:
        """Switch to conversation at 1-based index.

        Returns user-facing reply text (success or error message).
        """
        ...

    async def get_status(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> str:
        """Return current conversation status as formatted text."""
        ...

    async def rename_conversation(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        new_name: str,
    ) -> str:
        """Rename current conversation.

        Returns user-facing reply text (success or error message).
        """
        ...

    async def delete_conversation(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> str:
        """Delete current conversation and start fresh.

        Returns user-facing reply text (success or error message).
        """
        ...


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


class HandleConversationCommandUseCase:
    """Dispatch a conversation slash-command through the port.

    The dispatch bridge calls this use case when the parsed command
    verb is in :data:`CONVERSATION_COMMAND_VERBS`.  The use case
    validates args, delegates to :class:`ConversationCommandPort`,
    and returns a formatted user-facing reply string.

    This replaces the v1 direct routing of these 5 verbs to
    ``ai_coding_bridge.deliver()`` which was incorrect — those verbs
    manage **chat** conversations, not coding sessions.
    """

    __slots__ = ("_port",)

    def __init__(self, *, port: ConversationCommandPort) -> None:
        self._port = port

    async def execute(
        self,
        channel_msg: ChannelMessage,
        command: Command,
    ) -> str:
        """Return user-facing reply text for the conversation command.

        Raises no exceptions to the caller — errors are formatted as
        user-facing ⚠️ messages (matching v1 behaviour).
        """
        verb = command.verb.lower()
        args = command.args
        instance_id = channel_msg.instance_id
        user_id = channel_msg.sender

        if verb == "list":
            return await self._handle_list(instance_id, user_id, args)
        elif verb == "use":
            return await self._handle_use(instance_id, user_id, args)
        elif verb == "status":
            return await self._handle_status(instance_id, user_id)
        elif verb == "rename":
            return await self._handle_rename(instance_id, user_id, args)
        elif verb == "delete":
            return await self._handle_delete(instance_id, user_id)
        else:
            return f"\u26a0\ufe0f 未知会话命令: /{verb}"

    # ------------------------------------------------------------------
    # Per-verb handlers
    # ------------------------------------------------------------------

    async def _handle_list(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        args: tuple[str, ...],
    ) -> str:
        # V1 default is 5 (session_commands.py:279 `_DEFAULT_LIST_COUNT`),
        # cap 50 (`_MAX_LIST_COUNT`).  回退-2 fix — V2 previously defaulted
        # to 10, diverging from the "最近 5 条" the help text promises.
        limit = 5
        if args:
            try:
                limit = int(args[0])
                if limit <= 0:
                    return "\u26a0\ufe0f 参数必须为正整数，例如：/list 10"
            except ValueError:
                return "\u26a0\ufe0f 参数必须为正整数，例如：/list 10"
            limit = min(limit, 50)

        try:
            convs = await self._port.list_conversations(
                instance_id, user_id, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            return f"\u26a0\ufe0f 查询历史记录失败：{exc}"

        if not convs:
            return "\U0001f4cb 暂无历史会话记录\n\n发送 /new 开启新会话"

        lines = [f"\U0001f4cb 历史会话（最近 {len(convs)} 条）：\n"]
        for i, conv in enumerate(convs, start=1):
            marker = f"{i}. \u25b6" if conv.is_current else f"{i}."
            lines.append(f"{marker} {conv.title}")
            summary = (
                f"   {conv.updated_at}"
                + (f" \u00b7 {conv.round_count}\u8f6e" if conv.round_count > 0 else "")
            )
            lines.append(summary)
            lines.append("")

        lines.append("\u25b6=当前会话")
        lines.append("\U0001f4a1 /use <编号> 切换到指定会话")
        return "\n".join(lines)

    async def _handle_use(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        args: tuple[str, ...],
    ) -> str:
        if not args:
            return "用法：/use <编号>\n\n发送 /list 查看历史会话列表"

        try:
            index = int(args[0])
        except ValueError:
            return "\u26a0\ufe0f 编号必须为正整数，例如：/use 2"

        if index < 1:
            return "\u26a0\ufe0f 编号从 1 开始"

        try:
            return await self._port.use_conversation(
                instance_id, user_id, index
            )
        except Exception as exc:  # noqa: BLE001
            return f"\u26a0\ufe0f 切换会话失败：{exc}"

    async def _handle_status(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> str:
        try:
            return await self._port.get_status(instance_id, user_id)
        except Exception as exc:  # noqa: BLE001
            return f"\u26a0\ufe0f 获取状态失败：{exc}"

    async def _handle_rename(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
        args: tuple[str, ...],
    ) -> str:
        if not args:
            return "用法：/rename <新名称>\n\n例如：/rename 项目讨论"

        new_name = args[0].strip()
        if not new_name:
            return "\u26a0\ufe0f 名称不能为空"

        try:
            return await self._port.rename_conversation(
                instance_id, user_id, new_name
            )
        except Exception as exc:  # noqa: BLE001
            return f"\u26a0\ufe0f 重命名失败：{exc}"

    async def _handle_delete(
        self,
        instance_id: ChannelInstanceId,
        user_id: ChannelUserId,
    ) -> str:
        try:
            return await self._port.delete_conversation(
                instance_id, user_id
            )
        except Exception as exc:  # noqa: BLE001
            return f"\u26a0\ufe0f 删除失败：{exc}"
