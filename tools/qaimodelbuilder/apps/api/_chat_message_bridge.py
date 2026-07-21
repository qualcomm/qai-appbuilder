# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer cross-context bridge: channels → chat (PR-047 + PR-201 + S9 PR-093 + PR-097).

The :class:`MessageBridgePort` lives in
:mod:`qai.channels.application.ports`; per the import-linter
``context-isolation`` contract, ``qai.channels.*`` may NEVER import
``qai.chat.*`` directly.  This module — at the apps composition root —
is the only place that legitimately sees both contexts.

PR-201 — runtime activation
---------------------------

PR-047 shipped :class:`ChatMessageBridge` with a ``[chat-pending]`` echo
fallback because the chat side did not yet expose a usable
``send_message`` use case shape.  PR-201 activates the real path:

* Mint a one-shot conversation per inbound message
  (``"channel:{kind}/{instance_id}/{user_id}"`` titled).
* Open a fresh tab on that conversation.
* Run :class:`StreamChatUseCase`, accumulating every ``CHUNK`` frame's
  ``payload["text"]`` into the assistant reply text.
* Close the tab and return the accumulated text via :class:`BridgeReply`.

S9 PR-093 — streaming variant for channel real-time delivery
------------------------------------------------------------

The original :meth:`ChatMessageBridge.deliver` buffers the entire
reply before returning a single :class:`BridgeReply`.  S9 PR-093
adds :meth:`ChatMessageBridge.stream_text` which yields each
incoming text chunk as it arrives so the dispatch bridge in
:mod:`apps.api._channel_dispatch_bridge` can surface mid-stream
typing indicators / partial replies through the realtime delivery
service (D-5).  The existing ``deliver()`` shape is preserved for
backward compat with the channel ingest use case (S4 PR-047).

PR-097 R-16 / R-17 — per-user conversation reuse + ``/new`` / ``/clear``
-----------------------------------------------------------------------

The legacy ``backend/channels/wechat/channel.py:280-1296`` mapped each
channel user to a single long-lived conversation id
(``_user_conv_ids``) so subsequent messages from the same user
extended the same conversation history rather than creating a new
conversation per message.  ``/new`` and ``/clear`` flagged the user
into ``_force_new_conv_users`` so the next message minted a fresh
conversation regardless of the cached id.

PR-097 restores both behaviours on top of the new architecture by
keeping a module-level ``(ChannelInstanceId, channel_user_id) ->
conversation_id`` map plus a per-instance ``force_new`` set.  The
state is module-level (rather than on a class instance) because the
DI builder constructs :class:`ChatMessageBridge` exactly once per
process; a per-instance asyncio lock serialises check-and-update so
two concurrent inbound messages from the same user do not both mint
a new conversation.

The bridge depends only on the ``ChatServices`` *public* fields
(``conversations`` / ``tabs`` plus the existing
``create_conversation_use_case`` / ``open_tab_use_case`` /
``stream_chat_use_case`` / ``close_tab_use_case``); no chat-context
code is touched, preserving the field-name lock from §3.1.

If the chat namespace exposes none of those use cases (very early
bootstrap), the bridge surfaces
:class:`MessageBridgeUnavailableError` rather than a soft echo.  The
old echo fallback was a v2.7 §3 violation (silent degradation in a
production code path) and is removed.

The legacy :class:`EchoMessageBridge` is preserved verbatim because
``tests/integration/http/test_channels_routes.py`` (outside the L2
file domain) explicitly constructs it and asserts the ``echo:``
prefix.  That test exercises the dispatch → reply pipeline against a
deterministic bridge; PR-202 / I2 cutover may swap it later, but
PR-201 keeps the symbol stable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from qai.platform.errors import ApplicationError
from qai.platform.logging import get_logger

from qai.channels.application.ports import BridgeReply, MessageBridgePort
from qai.channels.domain import (
    ChannelInstanceId,
    ChannelMessage,
    Command,
    MessageBridgeUnavailableError,
)

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

logger = get_logger(__name__)

__all__ = [
    "ChatMessageBridge",
    "ChatStreamEvent",
    "EchoMessageBridge",
    "mark_force_new_conv",
    "consume_force_new_conv",
    "compact_history",
    "clear_conversation_for_user",
    "delete_conversation_for_user",
    "get_conversation_for_user",
    "stop_chat_stream",
    "set_max_history_rounds",
    "reset_max_history_rounds",
    "get_max_history_rounds",
    "get_global_max_history_rounds",
]


# ---------------------------------------------------------------------------
# Force-new-conversation state (PR-097 R-11 / R-17)
# ---------------------------------------------------------------------------
#
# Mirrors the legacy ``backend/channels/wechat/channel.py:284
# _force_new_conv_users`` set: a per-(instance_id, channel_user_id)
# flag that tells the next inbound message from that user to skip
# the existing conversation and mint a fresh one.  Set by the
# ``/new`` and ``/clear`` slash commands; consumed (and cleared) by
# the next call to :meth:`ChatMessageBridge.deliver` /
# :meth:`stream_text` for the same key.
#
# Module-level state is fine here because the apps composition
# root creates exactly one bridge per process and the dispatch
# bridge takes a per-(instance, user) lock around message handling
# (see ``_channel_dispatch_bridge.py:_lock_for``), so concurrent
# mutation from two threads cannot interleave in-flight delivery.
_FORCE_NEW_CONV_USERS: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# Active chat-stream tab registry (4-M9 — /stop for normal chat)
# ---------------------------------------------------------------------------
#: ``(instance_id_value, channel_user_id) -> tab_id_value`` for the chat
#: stream currently in flight for that user.  V1 parity:
#: ``backend/channels/wechat/channel.py`` ``_user_running_tasks`` — a
#: per-user handle the ``/stop`` command uses to cancel an in-progress
#: normal-chat generation.  Populated by
#: :meth:`ChatMessageBridge.stream_text` when the tab opens and cleared in
#: its ``finally``; :func:`stop_chat_stream` reads it to abort via the
#: chat context's :class:`StreamAbortRegistryPort`.
_ACTIVE_CHAT_TABS: dict[tuple[str, str], str] = {}


# ---------------------------------------------------------------------------
# Per-user max-history-rounds override (4-M8 — /compact <N> / /compact 0)
# ---------------------------------------------------------------------------
#: ``(instance_id_value, channel_user_id) -> rounds`` override.  V1 parity:
#: ``backend/channels/wechat/channel.py`` ``_user_max_history_rounds`` — the
#: per-user target depth set by ``/compact <N>``.  ``/compact 0`` clears the
#: entry (restoring the global budget).  When present the value is forwarded
#: through ``StreamChatInput.extra["max_history_rounds"]`` so the chat
#: streaming pipeline trims subsequent turns to that depth.  The chat use
#: case honours it as a user-explicit HARD round cap applied BEFORE token
#: compaction (``qai.chat.domain.history_trim.trim_messages_by_rounds`` →
#: ``StreamChatUseCase._build_base_wire_messages``), trimming on a round
#: boundary so no tool round is sliced apart; the trimmed history is then
#: handed to normal token compaction.
_USER_MAX_HISTORY_ROUNDS: dict[tuple[str, str], int] = {}


def set_max_history_rounds(
    *, instance_id: str, user_id: str, rounds: int
) -> None:
    """Set the per-user max-history-rounds override (``/compact <N>``)."""
    _USER_MAX_HISTORY_ROUNDS[(instance_id, user_id)] = max(1, rounds)


def reset_max_history_rounds(*, instance_id: str, user_id: str) -> bool:
    """Clear the per-user override (``/compact 0``).  Returns prior presence."""
    return (
        _USER_MAX_HISTORY_ROUNDS.pop((instance_id, user_id), None) is not None
    )


def get_max_history_rounds(*, instance_id: str, user_id: str) -> int | None:
    """Return the per-user override, or ``None`` when using the global budget."""
    return _USER_MAX_HISTORY_ROUNDS.get((instance_id, user_id))


#: Hard-coded fallback when no global ``channels.max_history_rounds`` is
#: configured.  V1 parity: ``backend/channels/wechat/channel.py:174``
#: (``_DEFAULT_MAX_HISTORY_ROUNDS = 20``).
_GLOBAL_DEFAULT_MAX_HISTORY_ROUNDS = 20


def get_global_max_history_rounds(*, container: "Container") -> int:
    """Return the configured global ``channels.max_history_rounds`` default.

    Used by the bare ``/compact`` view-only path (V1
    ``wechat/channel.py:1711`` ``_get_max_history_rounds()``): the no-arg
    command echoes the *current* global default (and any per-user override)
    WITHOUT trimming or mutating state.

    Resolution (V1 ``_get_max_history_rounds`` priority 2/3):

    1. ``forge_config.json`` → ``channels.max_history_rounds`` when present
       and a positive integer;
    2. :data:`_GLOBAL_DEFAULT_MAX_HISTORY_ROUNDS` (20) otherwise.

    Best-effort: any read / parse failure degrades to the hard-coded default
    so the view-only command never raises.
    """
    data_paths = getattr(container, "data_paths", None)
    if data_paths is None:
        return _GLOBAL_DEFAULT_MAX_HISTORY_ROUNDS
    try:
        from ._runtime_config_store import forge_config_path

        path = forge_config_path(data_paths.root)
        if not path.is_file():
            return _GLOBAL_DEFAULT_MAX_HISTORY_ROUNDS
        import json

        doc = json.loads(path.read_text(encoding="utf-8"))
        channels = doc.get("channels") if isinstance(doc, dict) else None
        if isinstance(channels, dict):
            val = channels.get("max_history_rounds")
            if val is not None:
                return max(1, int(val))
    except Exception:  # noqa: BLE001 — view-only must never raise
        logger.warning(
            "channels.chat_bridge.global_max_history_rounds_read_failed",
            exc_info=True,
        )
    return _GLOBAL_DEFAULT_MAX_HISTORY_ROUNDS


def stop_chat_stream(
    *, container: "Container", instance_id: str, user_id: str
) -> bool:
    """Abort the user's in-flight normal-chat stream (4-M9).

    Returns ``True`` when an active chat tab was found and aborted via the
    chat context's :class:`StreamAbortRegistryPort`.  Returns ``False``
    when no chat stream is running for the user (the dispatch bridge then
    reports "nothing to stop").  Best-effort — abort registry errors are
    swallowed and reported as "not stopped".
    """
    key = (instance_id, user_id)
    tab_id_value = _ACTIVE_CHAT_TABS.get(key)
    if not tab_id_value:
        return False
    chat = getattr(container, "chat", None)
    if chat is None:
        return False
    registry = getattr(chat, "abort_registry", None)
    if registry is None:
        return False
    try:
        from qai.chat.domain.ids import TabId

        return bool(
            registry.abort(TabId.of(tab_id_value), reason="user_requested")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "channels.chat_bridge.stop_chat_stream_failed",
            instance_id=instance_id,
            user_id=user_id,
            error=str(exc),
        )
        return False


def mark_force_new_conv(*, instance_id: str, user_id: str) -> None:
    """Register that the next message from ``(instance_id, user_id)`` should
    open a fresh conversation rather than reuse the prior one.

    Called by the dispatch bridge's ``/new`` and ``/clear`` handlers.
    """
    _FORCE_NEW_CONV_USERS.add((instance_id, user_id))


def consume_force_new_conv(*, instance_id: str, user_id: str) -> bool:
    """Atomically check-and-clear the force-new flag for ``(instance, user)``.

    Returns ``True`` when the flag was set (caller should mint a new
    conversation); ``False`` when no force-new request was outstanding.
    """
    key = (instance_id, user_id)
    if key in _FORCE_NEW_CONV_USERS:
        _FORCE_NEW_CONV_USERS.discard(key)
        return True
    return False


async def compact_history(
    *,
    container: "Container",
    instance_id: str,
    user_id: str,
    rounds: int,
) -> str:
    """Compact the channel-mapped chat conversation to ``rounds`` round-trips.

    PR-097 R-11 — implements the ``/compact <n>`` slash command for
    the channel surface.  The chat namespace's
    :class:`~qai.chat.application.use_cases.compact.CompactChatUseCase`
    surfaces a "needs compaction?" decision; when it is present we
    report the current context-size status back to the user.  When
    the chat namespace is wired but no compact use case is exposed
    we return a graceful status string (no exception bubbles out so
    the dispatch bridge can pipe the message through the realtime
    delivery service).

    The ``rounds`` argument is plumbed through as the ``budget_tokens``
    estimate (1 round ≈ 800 tokens) so the chat use case can decide
    whether compaction is needed at the user's chosen target depth;
    this matches the legacy ``/compact <N>`` UX where N is the count
    of recent rounds the user wants to keep.

    Returns the user-facing reply text (Chinese single line) so the
    dispatch bridge can pipe it through the realtime delivery service.

    4-M8 — in addition to evaluating the compaction decision the bridge now
    records a per-user max-history-rounds override
    (:func:`set_max_history_rounds`) so subsequent turns are trimmed to the
    user's chosen depth (V1 ``_user_max_history_rounds`` parity), and — when
    a bound conversation exists and is over threshold — drives the chat
    context's :class:`ContextCompressionPort` to trim the persisted history
    immediately rather than only on the next turn.
    """
    # V1 parity: record the per-user override so future turns honour the
    # requested depth (forwarded through StreamChatInput.extra by
    # :meth:`ChatMessageBridge.stream_text`).
    set_max_history_rounds(
        instance_id=instance_id, user_id=user_id, rounds=rounds
    )
    chat = getattr(container, "chat", None)
    if chat is None:
        return "\u26a0\ufe0f /compact 当前不可用：chat 模块未启用"
    compact_uc = getattr(chat, "compact_chat_use_case", None)
    if compact_uc is None:
        return "\u26a0\ufe0f /compact 当前不可用"
    # Resolve the conversation tied to this channel (instance, user)
    # via the existing channel-bindings repo if present; absence is
    # not an error — we simply report that we cannot find a target.
    bindings = getattr(chat, "channel_bindings_repository", None)
    conversation_id = None
    if bindings is not None:
        try:
            entry = await bindings.find(instance_id, user_id)
        except Exception:  # noqa: BLE001
            entry = None
        if entry is not None:
            conversation_id = getattr(entry, "conversation_id", None)
    if conversation_id is None:
        return (
            f"\u2705 /compact: 已设置历史保留 {rounds} 轮，"
            f"将在下一轮对话生效。"
        )
    from qai.chat.application.use_cases.compact import (
        CompactChatInput,
    )
    try:
        result = await compact_uc.execute(
            CompactChatInput(
                conversation_id=conversation_id,
                budget_tokens=max(800, rounds * 800),
            )
        )
    except ApplicationError as exc:
        return f"\u26a0\ufe0f /compact 失败: {exc!s}"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "channels.chat_bridge.compact_failed",
            instance_id=instance_id,
            user_id=user_id,
            rounds=rounds,
            error=str(exc),
        )
        return f"\u26a0\ufe0f /compact 失败: {exc!s}"
    needs = getattr(result, "needs_compaction", False)
    if needs:
        # Drive the real compaction immediately (V1 trims on /compact).
        dropped = await _compact_now(
            container=container,
            conversation_id=conversation_id,
            rounds=rounds,
        )
        suffix = (
            f"，已压缩约 {dropped} 条历史消息" if dropped else ""
        )
        return (
            f"\u2705 /compact: 已将历史保留 {rounds} 轮{suffix}，"
            "后续可用 /compact 0 恢复默认。"
        )
    return (
        f"\u2705 /compact: 已设置历史保留 {rounds} 轮；"
        "当前长度仍在目标内，无需压缩。"
    )


async def _compact_now(
    *,
    container: "Container",
    conversation_id: Any,
    rounds: int,
) -> int:
    """Trim the persisted conversation history via the chat compressor.

    4-M8 — invokes ``container.chat.context_compressor.compress`` over the
    conversation's message list, preserving the most-recent ``rounds`` round
    trips (≈ ``rounds * 2`` messages).  Best-effort: returns the number of
    messages dropped (``0`` when the compressor / repo is unavailable or no
    trimming was needed).  Failures are swallowed so /compact never 500s.
    """
    chat = getattr(container, "chat", None)
    if chat is None:
        return 0
    compressor = getattr(chat, "context_compressor", None)
    conversations = getattr(chat, "conversations", None) or getattr(
        chat, "conversation_repository", None
    )
    if compressor is None or conversations is None:
        return 0
    try:
        conv = await conversations.get(conversation_id)
    except Exception:  # noqa: BLE001
        return 0
    raw_messages = getattr(conv, "messages", None)
    if not raw_messages:
        return 0
    try:
        message_dicts = [
            {
                "role": getattr(getattr(m, "role", None), "value", "user"),
                "content": getattr(
                    getattr(m, "content", None), "text", ""
                )
                or "",
            }
            for m in raw_messages
        ]
        compressed = await compressor.compress(
            message_dicts,
            preserve_tail=max(2, rounds * 2),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort compaction
        logger.warning(
            "channels.chat_bridge.compact_now_failed",
            error=str(exc),
        )
        return 0
    dropped = len(message_dicts) - len(compressed)
    return max(0, dropped)


# ---------------------------------------------------------------------------
# PR-097 R-16 — per-user conversation reuse (companion to R-17)
# ---------------------------------------------------------------------------
#: ``(instance_id_value, channel_user_id) -> conversation_id``.
#:
#: Module-level because the bridge is constructed exactly once per
#: process by the DI builder; the map survives across calls so
#: subsequent messages from the same channel user extend the same
#: conversation, matching the legacy
#: ``backend/channels/wechat/channel.py:280 _user_conv_ids`` semantics.
#: Concurrent inbound messages from the same user are serialised by
#: :data:`_USER_CONV_LOCKS` below.
_USER_CONV_IDS: dict[tuple[str, str], str] = {}

#: Friendly channel-kind labels for the WebUI sidebar conversation title
#: (V1 parity: ``[微信]`` / ``[飞书]`` prefixes — see ``_conversation_title``).
_CHANNEL_TITLE_LABELS: dict[str, str] = {
    "wechat": "\u5fae\u4fe1",
    "feishu": "\u98de\u4e66",
}

#: ``(instance_id_value, channel_user_id) -> asyncio.Lock``.
#:
#: Per-(instance, user) lock guarding the check-and-update sequence
#: of :data:`_USER_CONV_IDS` + :data:`_FORCE_NEW_CONV_USERS` so two
#: concurrent inbound messages from the same user cannot both mint
#: a new conversation.  Idle locks are pruned once the map grows past
#: :data:`_USER_CONV_LOCKS_MAX` (V1 ``backend/main.py:1296-1312`` bounded
#: prune); a ``WeakValueDictionary`` would defeat the "same lock object
#: across calls" requirement (the lock could be GC'd before ``async with``).
_USER_CONV_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}

#: Upper bound on :data:`_USER_CONV_LOCKS` before idle-lock pruning kicks in.
_USER_CONV_LOCKS_MAX = 2000


def _prune_idle_user_conv_locks() -> None:
    """Drop every currently-unheld lock from :data:`_USER_CONV_LOCKS`.

    A lock held by an in-flight ``async with`` reports ``locked() is True``
    and is preserved; only idle locks are evicted, bounding the map's growth
    for long-running processes without breaking active critical sections.
    """
    for key in [k for k, lk in _USER_CONV_LOCKS.items() if not lk.locked()]:
        _USER_CONV_LOCKS.pop(key, None)


def _conv_lock_for(
    instance_id: ChannelInstanceId, channel_user_id: str
) -> asyncio.Lock:
    key = (instance_id.value, channel_user_id)
    lock = _USER_CONV_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _USER_CONV_LOCKS[key] = lock
        if len(_USER_CONV_LOCKS) > _USER_CONV_LOCKS_MAX:
            _prune_idle_user_conv_locks()
    return lock


def get_conversation_for_user(
    instance_id: ChannelInstanceId, channel_user_id: str
) -> str | None:
    """Return the cached conversation id for a channel user (read-only).

    Mirrors V1 ``backend/channels/wechat/channel.py:280 _user_conv_ids``
    lookups: the dispatch bridge minted / restored the conversation id for
    this ``(instance_id, channel_user_id)`` and cached it in
    :data:`_USER_CONV_IDS`.  The channels inbound consumer reads it AFTER
    dispatch so the WebUI live-update broadcast can carry the REAL
    conversation id (the sidebar history list is keyed by it).  Returns
    ``None`` when no conversation has been minted yet (e.g. a command-only
    turn that never reached the chat bridge).

    Append-only public surface — does not mutate the cache (cf.
    :func:`clear_conversation_for_user`).
    """
    return _USER_CONV_IDS.get((instance_id.value, channel_user_id))


def clear_conversation_for_user(
    instance_id: ChannelInstanceId, channel_user_id: str
) -> str | None:
    """Drop any cached conversation id for the user and return it.

    Forces the next message to either restore from persistence (via
    :class:`ChannelBindings`) or mint a new conversation.  Differs
    from :func:`mark_force_new_conv` in that it does not persist the
    "force new" flag — useful for direct cleanup paths that should
    not pretend the user typed ``/new``.

    M2 — returns the conversation id that was cached (or ``None`` when
    none was), so the ``/clear`` handler can delete it from persistence
    (V0.5 ``wechat/channel.py:1361-1377`` / ``feishu/channel.py:856-869``
    deleted the conversation from the history store, not just the
    in-memory cache).
    """
    return _USER_CONV_IDS.pop((instance_id.value, channel_user_id), None)


async def delete_conversation_for_user(
    *,
    container: "Container",
    instance_id: ChannelInstanceId,
    channel_user_id: str,
) -> bool:
    """Best-effort delete of the user's cached conversation from the DB (M2).

    V0.5 parity: ``/clear`` removed the conversation from the persistent
    history store (``wechat/channel.py:1361-1377``: ``/clear`` deletes the
    DB record then opens a fresh session), whereas ``/new`` keeps it.  V2
    previously only dropped the in-memory ``_USER_CONV_IDS`` cache so the
    persisted conversation lingered in the WebUI history list after a
    channel ``/clear``.

    Resolves the cached conversation id (clearing the cache as a side
    effect, same as :func:`clear_conversation_for_user`) and, when present,
    deletes it via the chat context's :class:`DeleteConversationUseCase`.
    Best-effort: a missing use case / delete failure is swallowed (a reset
    must never 500).  Returns ``True`` when a conversation was deleted.
    """
    conv_id = clear_conversation_for_user(instance_id, channel_user_id)
    if not conv_id:
        return False
    chat = getattr(container, "chat", None)
    if chat is None:
        return False
    delete_uc = getattr(chat, "delete_conversation_use_case", None)
    if delete_uc is None:
        return False
    try:
        from qai.chat.application.use_cases.conversation_management import (
            DeleteConversationInput,
        )
        from qai.chat.domain.ids import ConversationId

        await delete_uc.execute(
            DeleteConversationInput(
                conversation_id=ConversationId.of(conv_id)
            )
        )
    except Exception as exc:  # noqa: BLE001 — /clear must never 500
        logger.warning(
            "channels.chat_bridge.clear_delete_failed",
            instance_id=instance_id.value,
            user_id=channel_user_id,
            conversation_id=conv_id,
            error=str(exc),
        )
        return False
    return True


def _reset_for_test() -> None:  # pragma: no cover
    """Test-only helper to clear all module-level conversation state."""
    _USER_CONV_IDS.clear()
    _FORCE_NEW_CONV_USERS.clear()
    _USER_CONV_LOCKS.clear()
    _ACTIVE_CHAT_TABS.clear()
    _USER_MAX_HISTORY_ROUNDS.clear()


from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _CachedConversationRef:
    """Lightweight conversation reference for the cached-reuse path.

    The fresh-mint code path returns a real :class:`Conversation`
    aggregate from :class:`CreateConversationUseCase`; the cached
    reuse path only needs the id (no aggregate reload — chat domain
    handles that internally when the tab is opened on the existing
    conversation).  This frozen ref exposes the same ``.id`` /
    ``.id.value`` shape the streaming code expects.
    """

    id: Any  # qai.chat.domain.ids.ConversationId, late-bound to avoid import


def _safe_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, returning ``default`` on any failure.

    Used by the sub-agent stream-event branches so a malformed
    ``index`` / ``total`` / ``rounds`` payload field never raises mid-stream
    and aborts the channel reply (graceful-degrade per the dispatch path's
    "never crash a turn" rule).
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ChatStreamEvent:
    """Structured event yielded by :meth:`ChatMessageBridge.stream_events`.

    M5 / M6 (V0.5 parity) — the original :meth:`stream_text` yielded only
    plain text chunks (``AsyncIterator[str]``), so two V0.5 behaviours that
    ride NON-chunk stream frames were silently dropped on the channel path:

    * **M5 工具调用进度** — V0.5 ``wechat/channel.py:1752-1759`` pushed a
      ``🔄 工具调用进度（第 N 批）`` line on every tool call/result; the chat
      stream surfaces these as ``TOOL_CALL`` / ``TOOL_RESULT`` frames that
      ``stream_text`` ignored.
    * **M6 超轮次提醒** — V0.5 ``wechat/channel.py:1854-1868`` pushed a
      ``⚠️ 当前会话已达到 N 轮…`` notice; the chat use case emits an aligned
      ``TURN_WARNING`` frame (``streaming.py:2637-2688``) that ``stream_text``
      also dropped.

    A lightweight discriminated event (string ``kind`` — no Enum, keeping the
    apps layer free of a new public Enum surface, mirroring
    :class:`apps.api._ai_coding_channel_bridge.AiCodingStreamEvent`):

    * ``"text"`` — assistant text delta (``text``).
    * ``"progress"`` — channel-visible progress line that should be sent
      immediately (sub-agent start/tool/done/error/summary), not buffered into
      the final assistant bubble.
    * ``"tool"`` — tool lifecycle (``tool_name`` / ``tool_args`` /
      ``tool_status`` ∈ ``"running" / "success" / "error"``).
    * ``"turn_warning"`` — over-turn-count advisory (pre-rendered ``text``).
    """

    __slots__ = ("kind", "text", "tool_name", "tool_args", "tool_status")

    def __init__(
        self,
        *,
        kind: str,
        text: str = "",
        tool_name: str = "",
        tool_args: dict[str, Any] | None = None,
        tool_status: str = "",
    ) -> None:
        self.kind = kind
        self.text = text
        self.tool_name = tool_name
        self.tool_args = tool_args or {}
        self.tool_status = tool_status


class ChatMessageBridge(MessageBridgePort):
    """Real :class:`MessageBridgePort` — routes channel messages to chat.

    PR-201 wires the bridge against the *public* ``ChatServices``
    surface exposed by :mod:`apps.api._chat_di` (PR-042 / PR-043):

    * ``create_conversation_use_case``
    * ``open_tab_use_case``
    * ``stream_chat_use_case``
    * ``close_tab_use_case``

    None of those names are new — they were all delivered before
    PR-201 — so no chat-side change is required by this PR.  L4 will
    deepen :class:`StreamChatUseCase` (tool-call dispatch / multi-turn
    history) without changing the surface this bridge touches.

    Behaviour:

    * Each inbound :class:`ChannelMessage` mints **one fresh
      conversation** with a deterministic title encoding the channel
      kind, instance id and sender id, so operators reading
      ``conversations`` storage can correlate channel traffic.  The
      command verb (when present) is appended to the title for
      observability — this is metadata only; the LLM sees only the
      raw message text.
    * The bridge collects every ``CHUNK`` frame's text payload into a
      single string and returns it.  Non-CHUNK frames (TOOL_CALL /
      TOOL_RESULT / END / ERROR) are observed but not echoed — channels
      reply with text only; tool execution is fully internal.
    * An ``ERROR`` frame raises
      :class:`MessageBridgeUnavailableError` so the route layer can
      surface a 503 to the channel transport instead of mis-attributing
      the failure to the inbound webhook.
    * The bridge always closes the tab in ``finally`` so a broken
      stream does not leak active tabs.

    Concurrency: each call gets its own conversation/tab, so two
    concurrent channel deliveries cannot collide on the per-tab
    streaming lock from :class:`StreamAbortRegistryPort`.
    """

    __slots__ = ("_container",)

    def __init__(self, *, container: "Container") -> None:
        self._container = container

    async def _resolve_model_hint(
        self, message: ChannelMessage
    ) -> str | None:
        """Resolve the model a channel turn should use (v0.5 parity).

        v0.5/V1 ``_resolve_fallback_model``
        (``backend/channels/wechat/channel.py:848`` /
        ``feishu/channel.py:1807``) resolved the effective model in two
        layers:

        1. the channel instance's OWN saved model (``set_model``), then
        2. when that is empty, the globally selected chat model
           (``forge_config.ui.selected_model_id`` — the model the user
           picked in the chat UI).

        V2 split the channel→chat path through this bridge, which until
        now hard-coded ``model_hint=None`` — so neither layer was read,
        the resolver fell through to an empty default endpoint and the
        turn surfaced ``[no LLM endpoint configured]`` (Feishu) / silent
        no-reply (WeChat).  This restores both layers: the V2 channel
        model lives in the channel settings VO; the global selection
        lives in user_prefs under ``ui.preferences.selected_model_id``
        (the V2 home of the legacy ``forge_config.ui.*`` keys).

        Returns the effective model id, or ``None`` when neither layer
        has a value (the resolver then applies its own default).
        """
        # Layer 1 — the channel instance's own saved model.
        try:
            channels = getattr(self._container, "channels", None)
            get_settings = (
                getattr(channels, "get_channel_settings_use_case", None)
                if channels is not None
                else None
            )
            if get_settings is not None:
                settings_vo = await get_settings.execute(
                    message.instance_id
                )
                model_id = getattr(
                    getattr(settings_vo, "model", None), "model_id", ""
                )
                if model_id:
                    return str(model_id)
        except Exception as exc:  # noqa: BLE001 — never block a turn
            logger.warning(
                "channels.bridge.channel_model_lookup_failed",
                instance_id=message.instance_id.value,
                kind=message.kind.value,
                error=str(exc),
            )

        # Layer 2 — the globally selected chat model (user_prefs).
        #
        # Two storage layouts co-exist in production:
        #   a) New layout (POST /api/preferences): document key
        #      ``ui.preferences`` → ``{"selected_model_id": "..."}``
        #   b) Legacy flat key: ``ui.selected_model_id`` → ``"..."``
        #      (written by the old frontend before the user_prefs BC
        #      was introduced; still present on existing installs).
        # We check (a) first, then fall back to (b) so both layouts work.
        try:
            prefs = getattr(self._container, "user_prefs", None)
            load_doc = (
                getattr(prefs, "load_document_use_case", None)
                if prefs is not None
                else None
            )
            if load_doc is not None:
                # (a) new layout — ui.preferences document
                doc = await load_doc.execute("ui.preferences")
                selected = str(doc.get("selected_model_id", "") or "")
                if selected:
                    return selected
            # (b) legacy flat key — read ui.selected_model_id directly
            # from the kv_user_prefs table.  ``load_document_use_case``
            # is not suitable here because ``coerce_document`` strips
            # plain-string values to ``{}`` (it only accepts dicts).
            db = getattr(self._container, "database", None)
            if db is not None:
                import json as _json
                async with db.connection() as _conn:
                    async with await _conn.execute(
                        "SELECT value_json FROM kv_user_prefs"
                        " WHERE key = 'ui.selected_model_id'",
                    ) as _cur:
                        _row = await _cur.fetchone()
                if _row and _row[0]:
                    flat_val = _json.loads(str(_row[0]))
                    if isinstance(flat_val, str) and flat_val:
                        return flat_val
        except Exception as exc:  # noqa: BLE001 — never block a turn
            logger.warning(
                "channels.bridge.global_model_lookup_failed",
                instance_id=message.instance_id.value,
                kind=message.kind.value,
                error=str(exc),
            )

        return None

    async def deliver(
        self,
        message: ChannelMessage,
        command: Command | None,
    ) -> BridgeReply:
        chat = getattr(self._container, "chat", None)
        if chat is None:
            raise MessageBridgeUnavailableError(
                "chat context not wired yet"
            )

        # Public ChatServices fields used by the bridge — every name was
        # established by PR-033 / PR-042 / PR-043 and is part of the §3.1
        # field-name lock.
        create_conv_uc = getattr(chat, "create_conversation_use_case", None)
        open_tab_uc = getattr(chat, "open_tab_use_case", None)
        stream_uc = getattr(chat, "stream_chat_use_case", None)
        close_tab_uc = getattr(chat, "close_tab_use_case", None)
        if (
            create_conv_uc is None
            or open_tab_uc is None
            or stream_uc is None
            or close_tab_uc is None
        ):
            raise MessageBridgeUnavailableError(
                "chat context missing required use cases "
                "(create_conversation / open_tab / stream / close_tab)"
            )

        # Lazy import — keeps the apps module small at import time and
        # avoids fastapi's import graph dragging chat.domain into every
        # consumer of this file.
        from qai.chat.application.use_cases.streaming import (
            StreamChatInput,
        )
        from qai.chat.application.use_cases.tab_management import (
            CloseTabInput,
        )
        from qai.chat.domain.content import MessageContent
        from qai.chat.domain.stream_frame import StreamFrameType

        try:
            conversation, tab = await self._acquire_conversation_and_tab(
                message=message,
                command=command,
                create_conv_uc=create_conv_uc,
                open_tab_uc=open_tab_uc,
            )
        except MessageBridgeUnavailableError:
            raise

        text_parts: list[str] = []
        last_error: tuple[str, str] | None = None
        try:
            # v0.5 parity: resolve the channel/global model (was hard-coded
            # None — the root cause of "no LLM endpoint configured").
            model_hint = await self._resolve_model_hint(message)
            stream_request = StreamChatInput(
                tab_id=tab.id,
                conversation_id=conversation.id,
                user_message=MessageContent(text=message.content.text),
                model_hint=model_hint,
                extra={
                    "channel_kind": message.kind.value,
                    "channel_instance_id": message.instance_id.value,
                    "channel_user_id": message.sender.value,
                    "channel_command_verb": (
                        command.verb if command is not None else None
                    ),
                },
            )
            iterator = await stream_uc.execute(stream_request)
            async for frame in iterator:
                if frame.frame_type is StreamFrameType.CHUNK:
                    chunk_text = frame.payload.get("text", "")
                    if isinstance(chunk_text, str) and chunk_text:
                        text_parts.append(chunk_text)
                elif frame.frame_type is StreamFrameType.ERROR:
                    last_error = (
                        str(frame.payload.get("code", "stream_error")),
                        str(frame.payload.get("message", "")),
                    )
                # TOOL_CALL / TOOL_RESULT / END are observed but not echoed
        except ApplicationError:
            # Domain-level errors (ConversationLockedError, etc.) bubble
            # up so the route layer can map them to a 4xx envelope.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "channels.bridge.chat_stream_failed",
                instance_id=message.instance_id.value,
                kind=message.kind.value,
                conversation_id=conversation.id.value,
                tab_id=tab.id.value,
                error=str(exc),
            )
            raise MessageBridgeUnavailableError(
                f"chat dispatch failed: stream: {exc}"
            ) from exc
        finally:
            # Best-effort tab cleanup — never let a tab leak on error.
            try:
                await close_tab_uc.execute(
                    CloseTabInput(tab_id=tab.id.value),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.bridge.close_tab_failed",
                    tab_id=tab.id.value,
                    error=str(exc),
                )

        if last_error is not None:
            code, msg = last_error
            raise MessageBridgeUnavailableError(
                f"chat stream emitted error frame: {code}: {msg}"
            )

        reply_text = "".join(text_parts)
        return BridgeReply(
            reply_text=reply_text,
            coding_session_id=None,
        )

    async def stream_text(
        self,
        message: ChannelMessage,
        command: Command | None,
        *,
        image_content_blocks: list[dict[str, Any]] | None = None,
        model_hint_override: str | None = None,
    ) -> AsyncIterator[str]:
        """S9 PR-093: yield text chunks as they arrive (vs buffering in deliver()).

        Thin backward-compatible wrapper over :meth:`stream_events`: it
        filters the structured event stream down to ``kind == "text"`` and
        yields each delta's text, preserving the original
        ``AsyncIterator[str]`` contract for any caller that only wants text
        (tests + the pre-M5/M6 dispatch path).  The dispatch bridge now uses
        :meth:`stream_events` directly so it can also surface tool-progress
        (M5) and over-turn-count (M6) events.

        ``image_content_blocks`` is forwarded verbatim (PR-097 K-1
        multimodal input — see :meth:`stream_events`).
        ``model_hint_override`` is forwarded verbatim (M1 cloud fallback).
        """
        async for event in self.stream_events(
            message,
            command,
            image_content_blocks=image_content_blocks,
            model_hint_override=model_hint_override,
        ):
            if event.kind == "text" and event.text:
                yield event.text

    async def stream_events(
        self,
        message: ChannelMessage,
        command: Command | None,
        *,
        image_content_blocks: list[dict[str, Any]] | None = None,
        model_hint_override: str | None = None,
    ) -> AsyncIterator["ChatStreamEvent"]:
        """Stream the chat reply as structured :class:`ChatStreamEvent`s.

        Used by :mod:`apps.api._channel_dispatch_bridge` to surface
        partial replies through
        :class:`~qai.channels.application.services.realtime_delivery.RealtimeDeliveryService`
        so users on slow tools see streaming progress instead of one
        delayed final reply.

        Creates the same conversation/tab structure as :meth:`deliver`
        but yields each frame as a typed event:

        * ``CHUNK`` → ``ChatStreamEvent(kind="text", ...)``
        * ``TOOL_CALL`` → ``kind="tool"`` ``tool_status="running"``  (M5)
        * ``TOOL_RESULT`` (final, non-partial) → ``kind="tool"``
          ``tool_status="success"|"error"``  (M5; V0.5
          ``wechat/channel.py:1752-1759`` per-tool progress line)
        * ``TURN_WARNING`` → ``kind="turn_warning"`` carrying the chat use
          case's pre-rendered message  (M6; V0.5
          ``wechat/channel.py:1854-1868``)
        * ``SUBAGENT_START`` / ``SUBAGENT_TOOL`` / ``SUBAGENT_DONE`` /
          ``SUBAGENT_ERROR`` / ``AGENT_SUMMARY`` → ``kind="text"`` lines so
          the IM user sees multi-sub-agent orchestration progress inline,
          exactly like V0.5 (``wechat/channel.py:1675-1695`` /
          ``feishu/channel.py:1207-1227``).  Without these branches the
          frames fell through to the no-op tail and the channel user saw a
          silent gap during sub-agent runs (regression vs V0.5/V1).
        * ``ERROR`` → raises :class:`MessageBridgeUnavailableError`

        PR-097 K-1 (Sub-L) — multimodal image input
        -------------------------------------------
        ``image_content_blocks`` is the OpenAI-Vision style content list
        produced by
        :func:`apps.api._channel_dispatch_bridge._resolve_image_attachments`
        when the inbound :class:`ChannelMessage` carried one or more
        :class:`~qai.channels.domain.value_objects.ImageAttachment` entries.
        When supplied it is forwarded through ``extra["image_content_blocks"]``;
        :class:`StreamChatUseCase` assembles the multimodal LLM message list.
        When ``None`` or empty, behaviour is unchanged.
        """
        chat = getattr(self._container, "chat", None)
        if chat is None:
            raise MessageBridgeUnavailableError(
                "chat context not wired yet"
            )

        create_conv_uc = getattr(chat, "create_conversation_use_case", None)
        open_tab_uc = getattr(chat, "open_tab_use_case", None)
        stream_uc = getattr(chat, "stream_chat_use_case", None)
        close_tab_uc = getattr(chat, "close_tab_use_case", None)
        if (
            create_conv_uc is None
            or open_tab_uc is None
            or stream_uc is None
            or close_tab_uc is None
        ):
            raise MessageBridgeUnavailableError(
                "chat context missing required use cases"
            )

        from qai.chat.application.use_cases.streaming import StreamChatInput
        from qai.chat.application.use_cases.tab_management import (
            CloseTabInput,
        )
        from qai.chat.domain.content import MessageContent
        from qai.chat.domain.stream_frame import StreamFrameType

        conversation, tab = await self._acquire_conversation_and_tab(
            message=message,
            command=command,
            create_conv_uc=create_conv_uc,
            open_tab_uc=open_tab_uc,
        )
        # 4-M9 — record the active chat tab so /stop can abort this stream.
        _active_tab_key = (
            message.instance_id.value,
            message.sender.value,
        )
        _ACTIVE_CHAT_TABS[_active_tab_key] = tab.id.value
        try:
            extra: dict[str, Any] = {
                "channel_kind": message.kind.value,
                "channel_instance_id": message.instance_id.value,
                "channel_user_id": message.sender.value,
                "channel_command_verb": (
                    command.verb if command is not None else None
                ),
            }
            # PR-097 K-1: when the dispatch bridge resolved one or
            # more inbound image attachments to OpenAI-Vision blocks,
            # forward the block list verbatim through ``extra``.  The
            # chat use case (:class:`StreamChatUseCase`) reads it from
            # ``extra["image_content_blocks"]`` and assembles the full
            # multimodal LLM message list against the loaded
            # conversation history before opening the LLM stream;
            # this keeps the bridge free of any Conversation /
            # Message domain knowledge (conv may be a
            # ``_CachedConversationRef`` here, with ``.id`` but no
            # ``.messages``).
            if image_content_blocks:
                extra["image_content_blocks"] = list(image_content_blocks)
            # 4-M8 — forward the per-user max-history-rounds override so the
            # chat streaming pipeline trims this turn to the requested depth
            # (V1 ``_user_max_history_rounds`` parity).  Additive ``extra``
            # key — chat honours it as a hard round cap applied before token
            # compaction when present, ignores it otherwise.
            _rounds = get_max_history_rounds(
                instance_id=message.instance_id.value,
                user_id=message.sender.value,
            )
            if _rounds is not None:
                extra["max_history_rounds"] = _rounds
            # v0.5 parity: resolve the channel/global model (was hard-coded
            # None — the root cause of "no LLM endpoint configured").
            # M1 — when the dispatch bridge already decided a cloud fallback
            # (local service down), honour that override instead of
            # re-resolving (which would re-pick the unavailable local model).
            if model_hint_override:
                model_hint = model_hint_override
            else:
                model_hint = await self._resolve_model_hint(message)
            stream_request = StreamChatInput(
                tab_id=tab.id,
                conversation_id=conversation.id,
                user_message=MessageContent(text=message.content.text),
                model_hint=model_hint,
                extra=extra,
            )
            iterator = await stream_uc.execute(stream_request)
            # Track the most-recent TOOL_CALL's name+args so the terminal
            # TOOL_RESULT event can back-fill them even when the result
            # frame's own ``tool_name`` field is empty (observed in
            # production: TOOL_RESULT payload carries ``tool_name`` per the
            # domain factory, but the value can be an empty string when the
            # upstream adapter omits it).  Keyed by ``tool_call_id`` when
            # present, else by a simple "last call" sentinel — ordinary
            # single-tool-per-round chat only ever has one in-flight call.
            _last_tool_name: str = ""
            _last_tool_args: dict = {}
            _tool_call_id_to_name: dict[str, str] = {}
            _tool_call_id_to_args: dict[str, dict] = {}
            async for frame in iterator:
                if frame.frame_type is StreamFrameType.CHUNK:
                    chunk = frame.payload.get("text", "")
                    if isinstance(chunk, str) and chunk:
                        yield ChatStreamEvent(kind="text", text=chunk)
                elif frame.frame_type is StreamFrameType.TOOL_CALL:
                    # V0.5 parity (``wechat/channel.py:1752-1759``): push ONE
                    # progress line per tool, on COMPLETION (not on start).
                    # Record the call's name+args here so the TOOL_RESULT
                    # branch can emit the single terminal event with the
                    # correct name — do NOT yield a "running" event (that
                    # caused one tool to appear as two progress batches, with
                    # the second batch losing the tool name).
                    if frame.payload.get("phase") == "generating_args":
                        continue
                    _last_tool_name = str(frame.payload.get("tool_name", ""))
                    _last_tool_args = (
                        frame.payload.get("arguments")
                        if isinstance(frame.payload.get("arguments"), dict)
                        else {}
                    )
                    tcid = frame.payload.get("tool_call_id") or ""
                    if tcid:
                        _tool_call_id_to_name[tcid] = _last_tool_name
                        _tool_call_id_to_args[tcid] = _last_tool_args
                elif frame.frame_type is StreamFrameType.TOOL_RESULT:
                    # M5 — only the FINAL result frame (not the streaming
                    # ``partial`` increments, nor ``generating_args``
                    # progress) carries the tool's completion status.  V0.5
                    # marked ❌ when the result text was a ``[tool_error]``
                    # sentinel; mirror that here.
                    if frame.payload.get("partial"):
                        continue
                    if frame.payload.get("phase") == "generating_args":
                        continue
                    result_text = frame.payload.get("result")
                    is_error = isinstance(result_text, str) and (
                        result_text.startswith("[tool_error]")
                        or result_text.startswith("[guardrail_blocked]")
                    )
                    # Resolve tool name: prefer the result frame's own field,
                    # fall back to the tracked TOOL_CALL name (by id, then
                    # by last-call sentinel).  This ensures the single
                    # terminal progress line always carries the tool name
                    # even when the result frame omits it.
                    tcid = frame.payload.get("tool_call_id") or ""
                    resolved_name = (
                        str(frame.payload.get("tool_name") or "")
                        or _tool_call_id_to_name.get(tcid, "")
                        or _last_tool_name
                    )
                    resolved_args = (
                        _tool_call_id_to_args.get(tcid)
                        or _last_tool_args
                        or {}
                    )
                    yield ChatStreamEvent(
                        kind="tool",
                        tool_name=resolved_name,
                        tool_args=resolved_args,
                        tool_status="error" if is_error else "success",
                    )
                elif frame.frame_type is StreamFrameType.TURN_WARNING:
                    # M6 — V0.5 ``wechat/channel.py:1854-1868`` pushed an
                    # over-turn-count notice.  The chat use case already emits
                    # a TURN_WARNING frame with an aligned pre-rendered
                    # ``message`` (``streaming.py:2637-2688``); forward it so
                    # the dispatch bridge can deliver it to the channel user
                    # (previously this frame was silently dropped here).
                    warn_text = frame.payload.get("message")
                    if not (isinstance(warn_text, str) and warn_text):
                        tc = frame.payload.get("turn_count")
                        warn_text = (
                            f"⚠️ 当前会话已达到 {tc} 轮对话，"
                            "建议尽快清理历史或创建新会话。"
                            if tc
                            else "⚠️ 当前会话轮次较多，建议尽快创建新会话。"
                        )
                    yield ChatStreamEvent(
                        kind="turn_warning", text=warn_text
                    )
                elif frame.frame_type is StreamFrameType.SUBAGENT_START:
                    # V0.5 parity (``wechat/channel.py:1675-1680`` /
                    # ``feishu/channel.py:1207-1212``): announce sub-agent
                    # N/M start + its task preview as an inline text line so
                    # the IM user follows multi-agent orchestration.  V2
                    # ``index`` is 0-based (StreamFrame validates
                    # ``index < total``); display ``index + 1`` like V0.5.
                    payload = frame.payload
                    idx = _safe_int(payload.get("index"), 0)
                    total = _safe_int(payload.get("total"), 1)
                    preview = str(payload.get("prompt_preview", "") or "")
                    label = (
                        f"【子Agent {idx + 1}"
                        + (f"/{total}" if total > 1 else "")
                        + "】"
                    )
                    yield ChatStreamEvent(
                        kind="progress",
                        text=f"\n{label} 开始执行...\n任务：{preview}\n",
                    )
                elif frame.frame_type is StreamFrameType.SUBAGENT_TOOL:
                    # V0.5 parity (``wechat/channel.py:1681-1685``): one
                    # ``  🔧 tool(args)`` line per sub-agent tool call, args
                    # rendered ``k=repr(v)`` and head-truncated to 80 chars.
                    payload = frame.payload
                    tname = str(payload.get("tool_name", "") or "")
                    targs = payload.get("tool_args")
                    if not isinstance(targs, dict):
                        targs = {}
                    args_str = ", ".join(
                        f"{k}={v!r}" for k, v in targs.items()
                    )
                    yield ChatStreamEvent(
                        kind="progress",
                        text=f"  \U0001f527 {tname}({args_str[:80]})\n",
                    )
                elif frame.frame_type is StreamFrameType.SUBAGENT_DONE:
                    # V0.5 parity (``wechat/channel.py:1686-1689``):
                    # ``  ✅ 子Agent N 完成（R 轮）``.
                    payload = frame.payload
                    idx = _safe_int(payload.get("index"), 0)
                    rounds = _safe_int(payload.get("rounds"), 0)
                    yield ChatStreamEvent(
                        kind="progress",
                        text=(
                            f"  \u2705 子Agent {idx + 1} 完成"
                            f"（{rounds} 轮）\n"
                        ),
                    )
                elif frame.frame_type is StreamFrameType.SUBAGENT_ERROR:
                    # V0.5 parity (``wechat/channel.py:1690-1693``):
                    # ``  ❌ 子Agent N 出错：<msg>``.
                    payload = frame.payload
                    idx = _safe_int(payload.get("index"), 0)
                    err_msg = str(payload.get("message", "") or "")
                    yield ChatStreamEvent(
                        kind="progress",
                        text=f"  \u274c 子Agent {idx + 1} 出错：{err_msg}\n",
                    )
                elif frame.frame_type is StreamFrameType.AGENT_SUMMARY:
                    # V0.5 parity (``wechat/channel.py:1694-1695``): a
                    # separator + "主Agent总结" header between the last
                    # sub-agent event and the parent agent's follow-up text.
                    # V0.5 used a U+2500 box-drawing rule; we render a plain
                    # ``---`` rule instead so the line never trips the project
                    # locale-encoding guard (AGENTS.md §3.10.2 flags
                    # box-drawing inside string literals as possible mojibake)
                    # — same user-perceived semantics, no UX regression.
                    yield ChatStreamEvent(
                        kind="progress",
                        text="\n---\n\U0001f4cb 主Agent总结：\n",
                    )
                elif frame.frame_type is StreamFrameType.ERROR:
                    code = frame.payload.get("code", "stream_error")
                    msg = frame.payload.get("message", "")
                    raise MessageBridgeUnavailableError(
                        f"chat stream error frame: {code}: {msg}"
                    )
        finally:
            _ACTIVE_CHAT_TABS.pop(_active_tab_key, None)
            try:
                await close_tab_uc.execute(
                    CloseTabInput(tab_id=tab.id.value)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channels.bridge.close_tab_failed",
                    tab_id=tab.id.value,
                    error=str(exc),
                )

    async def _acquire_conversation_and_tab(
        self,
        *,
        message: ChannelMessage,
        command: Command | None,
        create_conv_uc: Any,
        open_tab_uc: Any,
    ) -> tuple[Any, Any]:
        """PR-097 R-16 — reuse the channel user's conversation when possible.

        Behaviour:

        1. Acquire the per-(instance, user) lock so the check + update
           is atomic against concurrent inbound messages from the
           same user.
        2. If the user is in :data:`_FORCE_NEW_CONV_USERS` for this
           instance, mint a fresh conversation and remove them from
           the set (one-shot, matches legacy
           ``_force_new_conv_users.discard(user_id)`` semantics).
        3. Otherwise, if a cached conversation id exists in
           :data:`_USER_CONV_IDS`, open a tab on it.  If opening the
           tab raises (e.g. the conversation was deleted out from
           under us), fall through to mint a new conversation.
        4. Otherwise mint a new conversation and persist the cached
           id so subsequent messages from the same user extend the
           same history.

        Returns ``(conversation, tab)`` — both objects expose
        ``.id`` / ``.id.value`` to the caller.
        """
        from qai.chat.application.use_cases.conversation_management import (
            CreateConversationInput,
        )
        from qai.chat.application.use_cases.tab_management import (
            OpenTabInput,
        )

        instance_id = message.instance_id
        user_id = message.sender.value
        # For group messages, key the conversation by group_id so all
        # members of the same group share one conversation thread.
        # For p2p messages, key by sender as before.
        group_id = getattr(message, "group_id", None)
        conv_user_key = group_id if group_id else user_id
        title = _conversation_title(message=message, command=command)
        cache_key = (instance_id.value, conv_user_key)

        async with _conv_lock_for(instance_id, conv_user_key):
            # PR-097 R-17: ``/new`` / ``/clear`` flag wins over the
            # cached conversation id.  ``consume_force_new_conv`` is
            # one-shot — it returns the prior flag value and clears
            # it atomically so the next message reuses the new
            # conversation we are about to mint.
            force_new = consume_force_new_conv(
                instance_id=instance_id.value, user_id=conv_user_key
            )
            cached_conv_id = (
                None if force_new else _USER_CONV_IDS.get(cache_key)
            )
            # H3 — restart recovery (V1 ``wechat/channel.py:1040-1091
            # _ensure_conv`` -> ``get_latest_wechat_conversation``): the
            # in-memory ``_USER_CONV_IDS`` cache is lost on a service
            # restart, so a returning channel user would otherwise get a
            # brand-new conversation every reboot.  When the cache misses
            # AND this is NOT an explicit ``/new`` / ``/clear`` reset, fall
            # back to the persisted store: find the most-recently-updated
            # conversation tagged with this (source, channel_user_id) and
            # resume it (re-populating the cache).  ``/new`` / ``/clear``
            # (``force_new``) intentionally SKIP recovery so the user gets a
            # fresh conversation — V1 parity (``_force_new_conv_users`` skips
            # the DB-restore branch in ``_ensure_conv``).
            if cached_conv_id is None and not force_new:
                cached_conv_id = await self._recover_channel_conversation(
                    message=message, cache_key=cache_key
                )
            if cached_conv_id is not None:
                try:
                    tab = await open_tab_uc.execute(
                        OpenTabInput(conversation_id=cached_conv_id)
                    )
                    # Construct a proper :class:`ConversationId` so
                    # downstream :class:`StreamChatInput` receives the
                    # same shape as the freshly-minted path.
                    from qai.chat.domain.ids import ConversationId

                    conversation = _CachedConversationRef(
                        id=ConversationId.of(cached_conv_id)
                    )
                    # Auto-bind: keep ChannelBindings in sync so the
                    # WebUI→channel sync-push fallback (_push_from_bindings)
                    # can push replies back to the channel user without
                    # requiring a manual binding step.  Only for p2p
                    # messages (group_id is None) — group replies need
                    # send_to_chat which PushChannelMessageUseCase does
                    # not currently support.
                    if not group_id:
                        await _auto_bind_channel_conversation(
                            container=self._container,
                            instance_id=instance_id,
                            conversation_id=cached_conv_id,
                            channel_user_id=user_id,
                        )
                    return conversation, tab
                except ApplicationError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # The cached conversation is gone — drop it and
                    # fall through to minting a new one.
                    logger.warning(
                        "channels.bridge.cached_conversation_open_failed",
                        instance_id=instance_id.value,
                        user_id=user_id,
                        conversation_id=cached_conv_id,
                        error=str(exc),
                    )
                    _USER_CONV_IDS.pop(cache_key, None)

            try:
                conversation = await create_conv_uc.execute(
                    CreateConversationInput(
                        title=title,
                        # H3 — tag the new conversation with its channel
                        # source so a future restart can recover it via
                        # ``find_latest_by_channel_user`` (V1
                        # ``upsert_conversation(meta={"source":"wechat",
                        # "wechat_user_id":...})``).  Unified V2 meta shape:
                        # ``{"source": <kind>, "channel_user_id": <user>}``.
                        meta={
                            "source": message.kind.value,
                            "channel_user_id": conv_user_key,
                        },
                    ),
                )
            except ApplicationError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "channels.bridge.create_conversation_failed",
                    instance_id=instance_id.value,
                    kind=message.kind.value,
                    error=str(exc),
                )
                raise MessageBridgeUnavailableError(
                    f"chat dispatch failed: create_conversation: {exc}"
                ) from exc

            try:
                tab = await open_tab_uc.execute(
                    OpenTabInput(
                        conversation_id=conversation.id.value
                    ),
                )
            except ApplicationError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "channels.bridge.open_tab_failed",
                    instance_id=instance_id.value,
                    kind=message.kind.value,
                    conversation_id=conversation.id.value,
                    error=str(exc),
                )
                raise MessageBridgeUnavailableError(
                    f"chat dispatch failed: open_tab: {exc}"
                ) from exc

            # Successful mint — record the cached id.  The
            # force-new flag was already consumed at the top of the
            # locked section so the next message will reuse the new
            # conversation rather than treating it as another /new.
            _USER_CONV_IDS[cache_key] = conversation.id.value
            # Auto-bind: establish ChannelBindings so the WebUI→channel
            # sync-push fallback (_push_from_bindings) can push replies
            # back to the channel user.  Only for p2p messages.
            if not group_id:
                await _auto_bind_channel_conversation(
                    container=self._container,
                    instance_id=instance_id,
                    conversation_id=conversation.id.value,
                    channel_user_id=user_id,
                )
            return conversation, tab

    async def _recover_channel_conversation(
        self,
        *,
        message: ChannelMessage,
        cache_key: tuple[str, str],
    ) -> str | None:
        """Resume a persisted conversation for this channel user (H3).

        Restart-recovery branch of :meth:`_acquire_conversation_and_tab` (V1
        ``wechat/channel.py:1040-1091 _ensure_conv`` ->
        ``get_latest_wechat_conversation``): queries the chat conversation
        repository for the most-recently-updated conversation tagged with
        ``meta = {"source": <kind>, "channel_user_id": <user>}`` and, when
        found, re-populates the in-memory ``_USER_CONV_IDS`` cache so the
        caller reuses it (the subsequent ``open_tab`` re-opens it and the
        chat use case re-loads its history — V1 re-hydrated
        ``_user_histories`` the same way).

        Returns the recovered conversation id, or ``None`` when the store /
        use case is unavailable or no prior conversation exists.  Best-effort:
        any lookup failure degrades to ``None`` (mint a fresh conversation)
        so a transient persistence hiccup never blocks a turn.
        """
        chat = getattr(self._container, "chat", None)
        if chat is None:
            return None
        conversations = getattr(chat, "conversations", None)
        find_latest = getattr(
            conversations, "find_latest_by_channel_user", None
        )
        if find_latest is None:
            return None
        try:
            conv = await find_latest(
                message.kind.value, message.sender.value
            )
        except Exception as exc:  # noqa: BLE001 — recovery is best-effort
            logger.warning(
                "channels.bridge.recover_conversation_failed",
                instance_id=message.instance_id.value,
                kind=message.kind.value,
                user_id=message.sender.value,
                error=str(exc),
            )
            return None
        if conv is None:
            return None
        conv_id_value = conv.id.value
        _USER_CONV_IDS[cache_key] = conv_id_value
        logger.info(
            "channels.bridge.recovered_conversation",
            instance_id=message.instance_id.value,
            kind=message.kind.value,
            user_id=message.sender.value,
            conversation_id=conv_id_value,
        )
        return conv_id_value


class EchoMessageBridge(MessageBridgePort):
    """Stand-in :class:`MessageBridgePort` for tests / offline runs.

    Critically this is a **real production adapter** (the historical
    ``_FakeXxx`` pattern was retired in PR-047) so it survives the
    fake-retirement guard; the legacy ``_FakeMessageBridge`` is gone.

    PR-201 keeps :class:`EchoMessageBridge` in the codebase because
    ``tests/integration/http/test_channels_routes.py`` (outside the L2
    file domain) instantiates it explicitly and asserts the ``echo:``
    prefix.  Production wiring (``apps/api/_channels_di.py``) now uses
    :class:`ChatMessageBridge` instead.
    """

    __slots__ = ("_prefix",)

    def __init__(self, *, prefix: str = "echo:") -> None:
        self._prefix = prefix

    async def deliver(
        self,
        message: ChannelMessage,
        command: Command | None,
    ) -> BridgeReply:
        return BridgeReply(
            reply_text=f"{self._prefix} {message.content.text}",
            coding_session_id=None,
        )


async def _auto_bind_channel_conversation(
    *,
    container: "Container",
    instance_id: "ChannelInstanceId",
    conversation_id: str,
    channel_user_id: str,
) -> None:
    """Best-effort: persist conversation→channel-user binding in ChannelBindings.

    Called after a channel conversation is created or resumed so the
    WebUI→channel sync-push fallback (:func:`_chat_sync_push._push_from_bindings`)
    can automatically push AI replies back to the bound channel user
    without requiring a manual binding step.

    Only applies to p2p messages (callers must guard ``group_id is None``
    before calling this helper).

    Failures are swallowed — binding is best-effort and must never
    block or break the inbound message flow.
    """
    try:
        channels_ns = getattr(container, "channels", None)
        if channels_ns is None:
            return
        bind_uc = getattr(
            channels_ns, "bind_channel_conversation_use_case", None
        )
        if bind_uc is None:
            return
        from qai.channels.application.use_cases.manage_bindings import (
            BindChannelConversationCommand,
        )

        await bind_uc.execute(
            BindChannelConversationCommand(
                instance_id=instance_id,
                conversation_id=conversation_id,
                channel_user_id=channel_user_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "channels.bridge.auto_bind_failed",
            instance_id=instance_id.value,
            conversation_id=conversation_id,
            error=str(exc),
        )


def _conversation_title(
    *, message: ChannelMessage, command: Command | None
) -> str:
    """Build a human-readable conversation title for a channel message.

    V1 parity (``backend/channels/wechat/channel.py:1236-1242
    _make_conv_title`` / ``feishu/channel.py:1720-1729``): the WebUI's
    left-hand history list shows a FRIENDLY title — a ``[微信]`` / ``[飞书]``
    prefix followed by the first ~30 chars of the user's message text (or
    the sender's short id when the message has no text, e.g. an image).
    The previous ``channel:<kind>/<instance>/<sender>`` machine string was
    a V2-only regression that surfaced raw instance ULIDs in the sidebar.

    The title is metadata only — the LLM is never exposed to it.
    """
    label = _CHANNEL_TITLE_LABELS.get(message.kind.value, message.kind.value)
    text = (message.content.text or "").strip()
    if text:
        snippet = text[:30] + ("\u2026" if len(text) > 30 else "")
        return f"[{label}] {snippet}"
    # No text (image-only / sticker): fall back to a short sender id,
    # mirroring V1's ``[飞书] {short_id}`` / ``[微信] {nickname}``.
    short_sender = (message.sender.value or "")[:12] or "unknown"
    return f"[{label}] {short_sender}"


def _resolve_chat_use_case(chat_services: Any) -> Any | None:
    """Legacy helper retained for backward-compatibility with PR-047 unit tests.

    PR-047 probed for ``deliver_for_channel`` / ``execute_for_channel``
    shapes that never landed; PR-201 uses the real public surface
    instead.  This helper is now a thin shim that returns ``None`` to
    signal "no legacy shape present" — kept so any external test that
    imported it still loads.
    """
    return None
