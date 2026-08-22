# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer channel inbound dispatch (S9 PR-093 §2.1 C-6 + PR-097).

Restores the central inbound-message dispatch pipeline that the
legacy ``_handle_message_locked`` provided: command parse → CC/OC session
route OR ChatHandler-stream route → tool executor → history
persist → broadcast outbound.  Without this the new architecture
had a webhook ingest path but no end-to-end "user types message →
LLM replies" wiring — addressed by parity-audit row §2.1 C-6 and
its dependencies (§2.1 C-7 CC/OC routing, §2.2 H-14 realtime
delivery, §2.4 L-5..L-9 help/grant/status text/tool-formatting).

PR-097 deliverables (R-3, R-4, R-10, R-11, R-12, R-21):

* **R-3** — function is now reachable from production: HTTP
  ``POST /api/{kind}/dispatch`` invokes :func:`dispatch_inbound_message`
  directly as the primary dispatch path.  The long-poll path
  established by Sub-G feeds the same function from its consumer
  task.
* **R-4** — the streamed reply is delivered through
  :class:`~qai.channels.application.services.realtime_delivery.RealtimeDeliveryService.context`
  so the typing keepalive + 3-layer fallback (in-context →
  out-of-context → pending queue) runs around every CC/OC and
  chat-stream emission.
* **R-10** — CC/OC tool events feed a per-message
  :class:`~qai.channels.application.tool_progress_aggregator.ToolProgressAggregator`
  formatted via :class:`~qai.channels.adapters.channel_tool_formatter.ChannelToolFormatter`.
* **R-11** — bare slash commands (``/list /use /status /rename
  /delete /new /clear /compact /model /models /grant /reboot
  /help /stop /cc /oc``) resolve via a verb→handler switch table.
* **R-12** — ``/grant`` routes to
  :class:`apps.api._channel_grant_bridge.ChannelGrantBridge` which
  fronts the security context's session-grant API.
* **R-21** — dead-code stubs (``_ = tool_formatter`` /
  ``_ = permission_bridge``) are removed; both parameters are
  consumed by R-10 and R-12 wiring respectively.

This module sits at the apps composition root because the dispatch
pipeline must coordinate three contexts that channels itself may
not import:

* :mod:`qai.chat` — normal-chat path through
  :class:`apps.api._chat_message_bridge.ChatMessageBridge`.
* :mod:`qai.ai_coding` — CC / OC session path through
  :class:`apps.api._ai_coding_channel_bridge.AiCodingChannelBridge`.
* :mod:`qai.security` — ``/grant`` permission decisions through
  :class:`apps.api._channel_grant_bridge.ChannelGrantBridge`.

Concurrency model
-----------------
A per-(instance, user) :class:`asyncio.Lock` serialises message
handling for the same conversational thread, matching the legacy
``_user_locks`` semantics.  The lock map is module-private and
keyed on ``(instance_id, user_id)`` so two distinct users (or two
distinct channel instances bound to the same user across providers)
process in parallel.  Locks are kept indefinitely — the working
set is bounded by active users and channel instances and ``WeakValueDictionary``
would defeat the "same lock object across calls" requirement.

Output shape
------------
:func:`dispatch_inbound_message` is an async generator yielding
:class:`OutboundFrame` dataclasses.  Each frame represents either a
text segment that the route layer should pipe into the realtime
delivery service (which itself runs the 3-layer fallback) or a
terminal error message.  The function ALSO drives the realtime
delivery service internally for in-stream Layer-1 / Layer-2
delivery, so the route layer's main job is to ACK the webhook /
long-poll source promptly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from qai.platform.errors import ApplicationError
from qai.platform.logging import get_logger
from qai.platform.i18n import t

from qai.channels.adapters.channel_help_text import (
    format_cc_help,
    format_main_help,
    format_oc_help,
)
from qai.channels.adapters.channel_tool_formatter import (
    ChannelToolFormatter,
    ToolStatus,
)
from qai.channels.adapters import (
    feishu_image_decoder as _feishu_image_decoder,
    wechat_image_decoder as _wechat_image_decoder,
)
from qai.channels.application.ports import (
    BridgeReply,
    CommandParserPort,
    SessionIndexRepositoryPort,
)
from qai.channels.application.use_cases.bare_command_router import (
    BareCommandKind,
    classify_bare_command,
)
from qai.channels.application.services.realtime_delivery import (
    RealtimeDeliveryService,
)
from qai.channels.application.tool_progress_aggregator import (
    RichToolEvent,
    ToolProgressAggregator,
)
from qai.channels.application.use_cases.conversation_commands import (
    CONVERSATION_COMMAND_VERBS,
    HandleConversationCommandUseCase,
)
from qai.channels.domain import (
    ChannelContext,
    ChannelInstanceId,
    ChannelKind,
    ChannelMessage,
    ChannelMessageId,
    ChannelUserId,
    Command,
    ImageAttachment,
    InvalidCommandError,
    MessageBridgeUnavailableError,
    MessageContent,
)

from . import _chat_message_bridge as _chat_bridge_module

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container
    from ._ai_coding_channel_bridge import AiCodingChannelBridge
    from ._channel_grant_bridge import ChannelGrantBridge
    from ._chat_message_bridge import ChatMessageBridge
    from ._reboot_scheduler import _RebootScheduler

logger = get_logger(__name__)

__all__ = [
    "OutboundFrame",
    "OutboundFrameKind",
    "DispatchServices",
    "dispatch_inbound_message",
    "current_channel_tool_context",
]


# ---------------------------------------------------------------------------
# Channel tool context — request-scoped context var for downstream bridges
# ---------------------------------------------------------------------------
#: Module-level :class:`~contextvars.ContextVar` that carries the channel
#: invocation context (channel_type, is_no_ui, etc.) into downstream bridge
#: calls.  The dispatch function sets this before calling chat / ai_coding
#: bridges so that PolicyCenter can auto-downgrade ASK → DENY for no-UI
#: channels without requiring signature changes to every bridge method.
#:
#: Consumers (e.g. :class:`ChatMessageBridge`, :class:`AiCodingChannelBridge`)
#: can read ``current_channel_tool_context.get()`` to obtain the dict.
current_channel_tool_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "current_channel_tool_context", default=None
)


# ---------------------------------------------------------------------------
# Tool-progress batching config (forge_config override) — V0.5 parity
# ---------------------------------------------------------------------------
#: Hard-coded defaults for the channel tool-progress aggregator.  Mirror
#: :class:`ToolProgressAggregator` dataclass defaults (5 events / 2.5s) — the
#: V2-chosen values, intentionally NOT the V0.5 3/1.0 (user decision).
_DEFAULT_TOOL_BATCH_SIZE = 5
_DEFAULT_TOOL_PROGRESS_MIN_INTERVAL = 2.5

#: forge_config (``claude_code`` section) keys an operator may set to tune the
#: channel tool-progress push cadence.  Key names kept identical to V0.5
#: so an operator's
#: existing config habit carries over.
_TOOL_BATCH_SIZE_KEY = "wechat_tool_batch_size"
_TOOL_PROGRESS_MIN_INTERVAL_KEY = "wechat_tool_progress_min_interval"


def _build_tool_progress_aggregator(
    services: "DispatchServices",
) -> "ToolProgressAggregator":
    """Construct a :class:`ToolProgressAggregator`, honouring forge_config.

    V0.5 parity: the CC handler read
    ``wechat_tool_batch_size`` / ``wechat_tool_progress_min_interval`` from the
    ``claude_code`` config section to tune the tool-progress push cadence (the
    OC handler did the same).  V2 reads the same two
    keys from ``forge_config.json`` → ``claude_code`` so an operator can still
    tune the cadence, while the *defaults* stay V2's 5 / 2.5 (user decision).

    Best-effort + synchronous: any missing file / parse failure / illegal
    value (``<= 0``) degrades to the hard-coded default so the inbound
    dispatch path never raises on a bad config.
    """
    batch_size = _DEFAULT_TOOL_BATCH_SIZE
    min_interval = _DEFAULT_TOOL_PROGRESS_MIN_INTERVAL
    container = getattr(services, "container", None)
    data_paths = getattr(container, "data_paths", None)
    if data_paths is not None:
        try:
            from ._runtime_config_store import forge_config_path

            path = forge_config_path(data_paths.root)
            if path.is_file():
                import json

                doc = json.loads(path.read_text(encoding="utf-8"))
                section = (
                    doc.get("claude_code") if isinstance(doc, dict) else None
                )
                if isinstance(section, dict):
                    raw_batch = section.get(_TOOL_BATCH_SIZE_KEY)
                    if raw_batch is not None:
                        parsed = int(raw_batch)
                        if parsed > 0:
                            batch_size = parsed
                    raw_interval = section.get(_TOOL_PROGRESS_MIN_INTERVAL_KEY)
                    if raw_interval is not None:
                        parsed_interval = float(raw_interval)
                        if parsed_interval > 0:
                            min_interval = parsed_interval
        except Exception:  # noqa: BLE001 — dispatch path must never raise
            logger.warning(
                "channels.dispatch.tool_progress_config_read_failed",
                exc_info=True,
            )
    return ToolProgressAggregator(
        batch_size=batch_size,
        min_interval_seconds=min_interval,
    )


# ---------------------------------------------------------------------------
# Output frames
# ---------------------------------------------------------------------------
class OutboundFrameKind:
    """String constants for :class:`OutboundFrame.kind`.

    Kept as plain string constants (rather than an Enum) so the
    apps layer stays free of any new public Enum surface — the
    dispatch route layer just passes the strings through.
    """

    REPLY = "reply"          # final or interim text reply
    PROGRESS = "progress"    # tool-progress batch
    ERROR = "error"          # operator-facing error message


@dataclass(frozen=True, slots=True, kw_only=True)
class OutboundFrame:
    """One unit of channel-bound output produced by the dispatcher.

    The route layer translates this into the provider's outbound
    transport call (or pipes it through
    :class:`~qai.channels.application.services.realtime_delivery.RealtimeDeliveryService`
    when running under the dispatch bridge directly).
    """

    kind: str
    text: str
    coding_session_id: str | None = None
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Per-(instance, user) lock map
# ---------------------------------------------------------------------------
_DISPATCH_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}

#: Upper bound on :data:`_DISPATCH_LOCKS` before idle-lock pruning kicks in
#: (V1 bounded prune).
_DISPATCH_LOCKS_MAX = 2000


def _prune_idle_dispatch_locks() -> None:
    """Drop every currently-unheld lock from :data:`_DISPATCH_LOCKS`.

    A lock held by an in-flight ``async with`` reports ``locked() is True``
    and is preserved; only idle locks are evicted, bounding the map's growth
    for long-running processes without breaking active critical sections.
    """
    for key in [k for k, lk in _DISPATCH_LOCKS.items() if not lk.locked()]:
        _DISPATCH_LOCKS.pop(key, None)


def _lock_for(
    instance_id: ChannelInstanceId, user_id: ChannelUserId
) -> asyncio.Lock:
    key = (instance_id.value, user_id.value)
    lock = _DISPATCH_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _DISPATCH_LOCKS[key] = lock
        if len(_DISPATCH_LOCKS) > _DISPATCH_LOCKS_MAX:
            _prune_idle_dispatch_locks()
    return lock


# ---------------------------------------------------------------------------
# DispatchServices — bundles the cross-context dependencies
# ---------------------------------------------------------------------------
@dataclass(slots=True, kw_only=True)
class DispatchServices:
    """All dependencies required by :func:`dispatch_inbound_message`.

    Bundled into one dataclass so the route layer / consumer task
    constructs the wiring once at startup; per-message dispatch is
    a single async-iterator call against the bundle.

    The realtime-delivery callables (``reply_in_context_factory``,
    ``send_to_user``, ``send_typing``) are provider-specific — for
    personal-WeChat the long-poll transport supplies all three;
    for Feishu only ``send_to_user`` is supplied and the
    delivery service skips Layer-1 / typing keepalive.
    """

    chat_bridge: "ChatMessageBridge"
    ai_coding_bridge: "AiCodingChannelBridge"
    grant_bridge: "ChannelGrantBridge | None"
    command_parser: CommandParserPort
    channel_session_repo: SessionIndexRepositoryPort
    delivery_service: RealtimeDeliveryService
    tool_formatter: ChannelToolFormatter
    container: "Container"
    reboot_scheduler: "_RebootScheduler | None" = None
    conversation_command_use_case: HandleConversationCommandUseCase | None = None
    # Realtime-delivery callable factories (provider-specific).
    reply_in_context_factory: (
        Callable[
            [ChannelMessage], Callable[[str], Awaitable[bool]] | None
        ]
        | None
    ) = None
    send_to_user: (
        Callable[[ChannelMessage], Callable[[str, str], Awaitable[bool]]]
        | None
    ) = None
    send_typing: (
        Callable[[ChannelMessage], Callable[[str], Awaitable[None]] | None]
        | None
    ) = None


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------
async def dispatch_inbound_message(
    channel_msg: ChannelMessage,
    *,
    services: DispatchServices,
    channel_context: ChannelContext | None = None,
) -> AsyncIterator[OutboundFrame]:
    """End-to-end inbound dispatch — equivalent to
    ``_handle_message_locked``.

    The dispatch order matches the legacy code so existing UX is
    preserved:

    1. **Command parse** — the channel ``CommandParserPort`` (with
       PR-093 §3.1 F-9 ``/c`` → ``/compact`` aliasing) returns a
       :class:`Command` or ``None``.
    2. **Bare slash-command switch table (PR-097 R-11)** — verbs
       handled inside this module without crossing into chat /
       ai_coding (``help``, ``new``, ``clear``, ``compact``,
       ``model``, ``models``, ``grant``, ``reboot``).
    3. **CC/OC session route** — when the user has an active coding
       session (or types ``/cc`` / ``/oc`` / ``/stop``), we
       call :class:`AiCodingChannelBridge`.
    3b. **Conversation command route** — ``/list`` / ``/use`` /
       ``/status`` / ``/rename`` / ``/delete`` are routed to
       :class:`HandleConversationCommandUseCase` (chat conversations,
       NOT ai_coding sessions).
    4. **Normal-chat route** — otherwise we stream through
       :class:`ChatMessageBridge`.  Each text chunk is delivered
       through :class:`RealtimeDeliveryService.context` so the
       3-layer fallback runs in-stream.
    5. **Errors** are converted to user-facing
       :class:`OutboundFrame(kind="error")` frames; only fatal bugs
       propagate.

    The per-(instance, user) lock is acquired around the entire
    dispatch so a slow stream cannot interleave with a subsequent
    message from the same user.
    """
    chat_bridge = services.chat_bridge
    ai_coding_bridge = services.ai_coding_bridge
    command_parser = services.command_parser
    channel_session_repo = services.channel_session_repo
    delivery_service = services.delivery_service
    tool_formatter = services.tool_formatter

    lock = _lock_for(channel_msg.instance_id, channel_msg.sender)
    async with lock:
        # ── 0. build tool_context from channel_context ────────────
        tool_context: dict[str, Any] | None = None
        if channel_context is not None:
            tool_context = {
                "channel": channel_context.channel_type,
                "no_ui": channel_context.is_no_ui,
                "session_id": channel_context.session_id,
                "instance_id": channel_context.instance_id,
            }
        # Publish to the module-level ContextVar so downstream bridges
        # (ChatMessageBridge, AiCodingChannelBridge) can read it without
        # signature changes.
        current_channel_tool_context.set(tool_context)

        # ── 1. command parse ──────────────────────────────────────
        try:
            command = command_parser.parse(channel_msg.content)
        except InvalidCommandError as exc:
            yield OutboundFrame(
                kind=OutboundFrameKind.ERROR,
                text=t("channels.dispatch.error.invalid_command", exc=str(exc)),
            )
            return

        # ── 2. bare-command switch table (PR-097 R-11) ────────────
        if command is not None:
            verb = command.verb.lower()
            handler_text = await _try_bare_command(
                verb=verb,
                command=command,
                channel_msg=channel_msg,
                services=services,
            )
            if handler_text is not None:
                # Already-formatted text reply (or ⚠️ error) — push
                # through the delivery service so the user sees it
                # via Layer-1/2.
                yield await _emit_via_delivery(
                    text=handler_text,
                    channel_msg=channel_msg,
                    services=services,
                    kind=OutboundFrameKind.REPLY,
                )
                return

        # ── 3. resolve active CC/OC session ───────────────────────
        active_session = await channel_session_repo.find(
            channel_msg.instance_id, channel_msg.sender
        )
        active_coding_session_id = (
            getattr(active_session, "coding_session_id", None)
            if active_session is not None
            else None
        )
        has_active_coding = bool(active_coding_session_id)

        # ── 4. open the realtime delivery context ──────────────────
        reply_in_context = (
            services.reply_in_context_factory(channel_msg)
            if services.reply_in_context_factory is not None
            else None
        )
        send_to_user_cb = (
            services.send_to_user(channel_msg)
            if services.send_to_user is not None
            else _build_default_send_to_user(channel_msg, services)
        )
        # IM-first UX: acknowledge immediately in Feishu/WeChat before slow LLM
        # / tool work starts.  This is a real outbound channel message (not just
        # WebUI state), so the user sees the bot is alive right away. Worded in
        # Chinese to match the other user-visible channel strings in this module
        # (and the project's primary user language); avoids the English/Chinese
        # split with the bot's actual reply (which follows the user's language).
        try:
            await send_to_user_cb(channel_msg.sender.value, t("channels.dispatch.thinking_ack"))
        except Exception:  # noqa: BLE001 — acknowledgement must never block reply
            logger.warning(
                "channels.dispatch.thinking_ack_failed",
                instance_id=channel_msg.instance_id.value,
                user_id=channel_msg.sender.value,
                exc_info=True,
            )
        send_typing_cb = (
            services.send_typing(channel_msg)
            if services.send_typing is not None
            else None
        )

        # ── 5. route selection ────────────────────────────────────
        try:
            async with delivery_service.context(
                instance_id=channel_msg.instance_id,
                user_id=channel_msg.sender,
                reply_in_context=reply_in_context,
                send_to_user=send_to_user_cb,
                send_typing=send_typing_cb,
            ) as session:
                # Drain any pending Layer-3 message from the previous
                # turn before processing the new one.
                await delivery_service.drain_pending(
                    instance_id=channel_msg.instance_id,
                    user_id=channel_msg.sender,
                    reply_in_context=reply_in_context,
                    send_to_user=send_to_user_cb,
                )

                if command is not None and command.verb.lower() == "stop":
                    # 4-M9 — /stop targets the active task.  Prefer the
                    # ai_coding session when one is active (V1 routes /stop
                    # to the running CC/OC task); otherwise cancel the
                    # in-flight normal-chat stream via the abort registry
                    # (V1 ``_user_running_tasks`` parity).
                    if has_active_coding:
                        reply = await ai_coding_bridge.deliver(
                            channel_msg,
                            command,
                            active_session_id=(
                                str(active_coding_session_id)
                                if active_coding_session_id
                                else None
                            ),
                        )
                        text = reply.reply_text or ""
                        if text:
                            await session.deliver(text)
                        yield _bridge_reply_to_frame(reply)
                        return
                    stopped = _chat_bridge_module.stop_chat_stream(
                        container=services.container,
                        instance_id=channel_msg.instance_id.value,
                        user_id=channel_msg.sender.value,
                    )
                    stop_text = (
                        t("channels.dispatch.stop.stopped")
                        if stopped
                        else t("channels.dispatch.stop.no_task")
                    )
                    await session.deliver(stop_text)
                    yield OutboundFrame(
                        kind=OutboundFrameKind.REPLY, text=stop_text
                    )
                    return

                if command is not None and command.verb.lower() in (
                    "cc",
                    "oc",
                ):
                    reply: BridgeReply = await ai_coding_bridge.deliver(
                        channel_msg,
                        command,
                        active_session_id=(
                            str(active_coding_session_id)
                            if active_coding_session_id
                            else None
                        ),
                    )
                    text = reply.reply_text or ""
                    if text:
                        await session.deliver(text)
                    yield _bridge_reply_to_frame(reply)
                    return

                # ── 5b. conversation commands (/list /use /status
                #    /rename /delete) → HandleConversationCommandUseCase
                if command is not None and command.verb.lower() in CONVERSATION_COMMAND_VERBS:
                    conv_uc = services.conversation_command_use_case
                    if conv_uc is not None:
                        reply_text = await conv_uc.execute(
                            channel_msg, command
                        )
                    else:
                        reply_text = t(
                            "channels.dispatch.error.conversation_uc_unavailable"
                        )
                    if reply_text:
                        await session.deliver(reply_text)
                    yield OutboundFrame(
                        kind=OutboundFrameKind.REPLY,
                        text=reply_text,
                    )
                    return

                if has_active_coding and active_coding_session_id:
                    # Stream tool-aware ai_coding response.
                    # M3 — decode the first inbound image to raw
                    # (base64, mime) so a CC/OC session can识图 (V1 →
                    # ``handle_cc_message(image_b64, image_mime)``).  No
                    # image / decode failure → text-only turn (degrade).
                    _coding_image = await _decode_coding_image(
                        channel_msg=channel_msg,
                        services=services,
                    )
                    _img_b64, _img_mime = (
                        _coding_image if _coding_image is not None
                        else (None, None)
                    )
                    aggregator = _build_tool_progress_aggregator(services)
                    final_text = await _stream_ai_coding(
                        channel_msg=channel_msg,
                        session_id=str(active_coding_session_id),
                        ai_coding_bridge=ai_coding_bridge,
                        delivery=session,
                        aggregator=aggregator,
                        tool_formatter=tool_formatter,
                        services=services,
                        image_b64=_img_b64,
                        image_mime=_img_mime,
                    )
                    yield OutboundFrame(
                        kind=OutboundFrameKind.REPLY,
                        text=final_text,
                        coding_session_id=str(active_coding_session_id),
                    )
                    return

                # Normal chat path — stream chunks for realtime UX
                # PR-097 K-1 (Sub-L): when the inbound message carries
                # one or more :class:`ImageAttachment` entries (Feishu
                # webhook parser populates these for ``message_type ==
                # "image"``; WeChat long-poll parser populates them
                # whenever the SDK event includes a downloadable image
                # handle), resolve them into OpenAI-Vision content
                # blocks BEFORE opening the LLM stream so the
                # multimodal payload reaches the model in one round.
                image_content_blocks = await _resolve_image_attachments(
                    channel_msg=channel_msg,
                    services=services,
                )
                # M1 — local-unavailable → cloud auto-fallback + notice
                # (V0.5 ``_resolve_fallback_model``).  When the channel's
                # intended model is local (or "follow global" resolving to a
                # local model) but the on-device service is NOT running and a
                # cloud model exists, temporarily route THIS turn to the first
                # cloud model and tell the user once.  Temporary only — we do
                # NOT persist a channel model switch, so the next turn
                # re-detects and returns to local once the service is back.
                model_hint_override, fallback_notice = (
                    await _resolve_chat_model_fallback(
                        channel_msg=channel_msg,
                        chat_bridge=chat_bridge,
                        services=services,
                    )
                )
                if fallback_notice:
                    await session.deliver(fallback_notice)
                # M5 / M6 — consume the STRUCTURED event stream so the
                # normal-chat path reaches V0.5 parity:
                #   * tool-call lifecycle events feed the SAME
                #     ToolProgressAggregator the CC/OC path uses
                #     (``_stream_ai_coding``), surfacing batched
                #     ``🔄 工具调用进度`` lines (V0.5);
                #   * a ``turn_warning`` event is delivered inline (V0.5).
                # Pure text chats emit no tool events, so quiet chats stay
                # quiet (no progress noise).
                aggregator = _build_tool_progress_aggregator(services)
                chunk_count = 0
                # v0.5 parity: accumulate the streamed text deltas
                # into ONE reply and send it as a SINGLE message at stream
                # end — NOT one IM message per chunk.  Delivering each chunk
                # separately made Feishu render every token on its own line
                # (a bubble per delta).  We still yield per-chunk partial
                # frames for any live WebUI consumer, but the actual channel
                # send (session.deliver) happens once with the full text.
                text_parts: list[str] = []
                async for event in chat_bridge.stream_events(
                    channel_msg,
                    command,
                    image_content_blocks=image_content_blocks,
                    model_hint_override=model_hint_override,
                ):
                    if event.kind == "text" and event.text:
                        chunk_count += 1
                        text_parts.append(event.text)
                        # Live partial frame only (WebUI live-update). The
                        # IM message is sent once below to avoid per-token
                        # bubbles.
                        yield OutboundFrame(
                            kind=OutboundFrameKind.REPLY,
                            text=event.text,
                            metadata=(("partial", "true"),),
                        )
                    elif event.kind == "progress" and event.text:
                        # Sub-agent progress must be pushed step-by-step to the
                        # channel, not held until the final answer. Deliver it
                        # live to the channel here; also yield a non-partial
                        # PROGRESS frame for any downstream consumer (the WebUI
                        # sidebar bubble deliberately does NOT concatenate these
                        # — see _channels_di.py — so the realtime bubble matches
                        # the persisted answer; progress surfaces in the WebUI
                        # via the streaming SSE path).
                        await session.deliver(event.text)
                        yield OutboundFrame(
                            kind=OutboundFrameKind.PROGRESS,
                            text=event.text,
                        )
                    elif event.kind == "tool":
                        # V0.5 parity: push
                        # ONE progress line per tool, on COMPLETION.  Skip the
                        # "running" lifecycle so a single tool never renders as
                        # two batches (the second of which lost its name).
                        if event.tool_status == "running":
                            continue
                        # Computer-tool screenshot delivery: fold the captured
                        # screenshot URL(s) into the reply as image markdown so
                        # the final ``reply_text`` → ``split_image_refs`` →
                        # ``dispatch_images`` path (in ``_build_default_send_to_user``)
                        # pushes them to WeChat/Feishu as native images. Applies
                        # to ANY audience (the WebUI renders them from the tool
                        # card; channels have no card, so they get the image
                        # here). ``send_to_user`` above only sees the FINAL text,
                        # so appending to ``text_parts`` is what makes the shot
                        # reach the phone.
                        if event.screenshot_urls:
                            for _shot_url in event.screenshot_urls:
                                text_parts.append(
                                    f"\n\n![screenshot]({_shot_url})\n\n"
                                )
                        status = (
                            ToolStatus.SUCCESS
                            if event.tool_status == "success"
                            else ToolStatus.ERROR
                        )
                        aggregator.add_rich(
                            RichToolEvent(
                                tool_name=event.tool_name,
                                args=event.tool_args,
                                status=status,
                            )
                        )
                        if aggregator.should_flush():
                            batch_text = aggregator.flush_rich(tool_formatter)
                            if batch_text:
                                await session.deliver(batch_text)
                    elif event.kind == "progress" and event.text:
                        # Sub-agent lifecycle progress (start/tool/done/error/
                        # summary).  Emitted by ChatMessageBridge with
                        # ``kind="progress"`` so it is sent to the channel
                        # IMMEDIATELY rather than buffered into the final
                        # assistant bubble (text_parts).  Deliver it inline to
                        # the channel user AND yield a live WebUI partial frame,
                        # mirroring the "text" branch's dual delivery.  Without
                        # this branch the progress lines (previously emitted as
                        # ``kind="text"``) would be silently dropped for channel
                        # users — a behavior regression.
                        await session.deliver(event.text)
                        yield OutboundFrame(
                            kind=OutboundFrameKind.REPLY,
                            text=event.text,
                            metadata=(("partial", "true"),),
                        )
                    elif event.kind == "turn_warning" and event.text:
                        await session.deliver(event.text)
                # Flush any residual tool-progress batch before finishing.
                if aggregator.pending_count > 0:
                    batch_text = aggregator.flush_rich(tool_formatter)
                    if batch_text:
                        await session.deliver(batch_text)
                # v0.5 parity: send the accumulated reply as ONE message and
                # emit a single FINAL (non-partial) frame carrying the full
                # text so the HTTP /dispatch aggregator returns it too.
                reply_text = "".join(text_parts).strip()
                # A screenshot-only turn (e.g. ``audience=user`` where the model
                # emits no prose) has ``chunk_count == 0`` but a non-empty
                # ``reply_text`` carrying the ``![screenshot](url)`` markdown —
                # still deliver it so the image reaches the phone. Only fall back
                # when there is genuinely nothing to send.
                if not reply_text:
                    fallback = t("channels.dispatch.chat.empty_reply")
                    await session.deliver(fallback)
                    yield OutboundFrame(
                        kind=OutboundFrameKind.REPLY,
                        text=fallback,
                    )
                else:
                    await session.deliver(reply_text)
                    yield OutboundFrame(
                        kind=OutboundFrameKind.REPLY,
                        text=reply_text,
                    )
                return

        except MessageBridgeUnavailableError as exc:
            logger.warning(
                "channels.dispatch.bridge_unavailable",
                instance_id=channel_msg.instance_id.value,
                user_id=channel_msg.sender.value,
                error=str(exc),
            )
            yield OutboundFrame(
                kind=OutboundFrameKind.ERROR,
                text=t("channels.dispatch.error.bridge_unavailable"),
            )
            return
        except ApplicationError as exc:
            yield OutboundFrame(
                kind=OutboundFrameKind.ERROR,
                text=t("channels.dispatch.error.application", exc=str(exc)),
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "channels.dispatch.unhandled_error",
                instance_id=channel_msg.instance_id.value,
                user_id=channel_msg.sender.value,
                error=str(exc),
                exc_info=True,
            )
            yield OutboundFrame(
                kind=OutboundFrameKind.ERROR,
                text=t("channels.dispatch.error.unhandled"),
            )
            return


# ---------------------------------------------------------------------------
# Bare slash-command switch table (PR-097 R-11)
# ---------------------------------------------------------------------------
async def _try_bare_command(
    *,
    verb: str,
    command: Command,
    channel_msg: ChannelMessage,
    services: DispatchServices,
) -> str | None:
    """Resolve a bare slash command in the channels layer.

    Returns the user-facing reply text when the verb is handled
    here; returns ``None`` when the verb falls through to ai_coding
    or chat routing.

    A-1 step1 split: the pure verb→category decision (incl. ``/compact``
    argument parsing) now lives in
    :func:`qai.channels.application.use_cases.bare_command_router.classify_bare_command`;
    this function keeps only the I/O orchestration that crosses into chat /
    model-catalog / grant / reboot collaborators — contexts the channels
    application layer may not import.

    The PR-097 R-11 table covers verbs that do NOT need an active
    CC/OC session and produce a single-line reply.  Verbs that
    *do* need ai_coding routing (``cc``/``oc``/``stop``) or
    conversation-command routing (``list``/``use``/``status``/
    ``rename``/``delete``) are intentionally excluded so the main
    dispatch routes them via the appropriate bridge.
    """
    classified = classify_bare_command(verb=verb, args=command.args)
    if classified is None:
        return None

    kind = classified.kind

    if kind == BareCommandKind.HELP:
        return format_main_help(channel_msg.kind.value)

    if kind == BareCommandKind.CC_HELP:
        return format_cc_help(channel_msg.kind.value)
    if kind == BareCommandKind.OC_HELP:
        return format_oc_help(channel_msg.kind.value)

    if kind == BareCommandKind.NEW:
        # 4-M7 — V1: both /new and /clear
        # cancel the user's pending permission requests and switch to a
        # fresh conversation; /clear additionally drops the cached
        # conversation id so no history carries over.
        await _cancel_pending_grants_for_user(
            channel_msg=channel_msg, services=services
        )
        _chat_bridge_module.mark_force_new_conv(
            instance_id=channel_msg.instance_id.value,
            user_id=channel_msg.sender.value,
        )
        if verb == "clear":
            # M2 — V0.5: ``/clear`` DELETES the
            # conversation from the persistent history store (not just the
            # in-memory cache), then opens a fresh session.  Best-effort:
            # a delete failure is swallowed inside the helper so the reset
            # never 500s.
            await _chat_bridge_module.delete_conversation_for_user(
                container=services.container,
                instance_id=channel_msg.instance_id,
                channel_user_id=channel_msg.sender.value,
            )
            # V0.5.
            return t("channels.dispatch.new.cleared")
        # V0.5.
        return t("channels.dispatch.new.opened")

    if kind == BareCommandKind.COMPACT:
        return await _chat_bridge_module.force_compact_chat(
            container=services.container,
            instance_id=channel_msg.instance_id.value,
            user_id=channel_msg.sender.value,
        )

    if kind == BareCommandKind.COMPACT_STATUS:
        return await _chat_bridge_module.compact_status(
            container=services.container,
            instance_id=channel_msg.instance_id.value,
            user_id=channel_msg.sender.value,
        )

    if kind == BareCommandKind.COMPACT_MIGRATE:
        return await _chat_bridge_module.compact_migrate(
            container=services.container,
            instance_id=channel_msg.instance_id.value,
            user_id=channel_msg.sender.value,
        )

    if kind == BareCommandKind.COMPACT_CLEAR:
        return await _chat_bridge_module.compact_clear(
            container=services.container,
            instance_id=channel_msg.instance_id.value,
            user_id=channel_msg.sender.value,
        )

    if kind == BareCommandKind.COMPACT_INVALID:
        return t("channels.chat.compact.invalid")

    if kind == BareCommandKind.MODEL_SET:
        return await _handle_model_set(
            channel_msg=channel_msg,
            args=classified.args,
            services=services,
        )
    if kind == BareCommandKind.MODEL_LIST:
        return await _handle_model_list(services=services)

    if kind == BareCommandKind.GRANT:
        return await _handle_grant(
            channel_msg=channel_msg,
            args=classified.args,
            services=services,
        )

    if kind == BareCommandKind.REBOOT:
        return await _handle_reboot(services=services)

    return None


async def _handle_model_set(
    *,
    channel_msg: ChannelMessage,
    args: tuple[str, ...],
    services: DispatchServices,
) -> str:
    """Handle ``/model [n|id|0]`` — switch the per-channel model (4-M6).

    V1 truth: ``_resolve_model_arg`` + ``_format_models_reply``.

    * no arg → show current model + usage hint;
    * ``0`` / ``default`` → reset to the platform default (empty model);
    * numeric → select the N-th model from the **combined local + cloud**
      list, using the SAME continuous numbering ``/models`` displays
      (V1 numbers local then cloud in one sequence — see ``_handle_model_list``);
    * id / name → match against the combined list, else verbatim id.

    回退-3 fix — V1 ``_resolve_model_arg`` resolves ``/model <n>`` against
    ALL real models (local + cloud), so a channel user could pick a cloud
    model by index.  V2 previously indexed only ``local_models`` so cloud
    models were unreachable via ``/model <n>``; this restores V1 parity.

    After persisting via ``container.channels.update_channel_model_use_case``
    a ``local::`` model that is not already running is auto-loaded through
    ``container.model_runtime.load_model_use_case`` (V1 ``_load_model_fn``).
    """
    container = services.container
    channels = getattr(container, "channels", None)
    if channels is None:
        return t("channels.dispatch.model.unavailable")
    update_uc = getattr(channels, "update_channel_model_use_case", None)
    if update_uc is None:
        return t("channels.dispatch.model.unavailable")

    local_models, current = await _enumerate_local_models(services)
    # Combined local + cloud list in the SAME order ``/models`` shows
    # (local first, then cloud) so numeric indices match what the user saw.
    cloud_ids = await _list_catalog_models(services)
    cloud_models: list[dict[str, Any]] = [
        {"model_id": cid, "name": cid, "is_running": False, "is_local": False}
        for cid in cloud_ids
    ]
    for m in local_models:
        m.setdefault("is_local", True)
    all_models: list[dict[str, Any]] = [*local_models, *cloud_models]

    if not args:
        cur = current or t("channels.dispatch.model.default_label")
        return t("channels.dispatch.model.show", cur=cur)

    arg = args[0].strip()
    reset = arg in ("0", "default", "默认")
    if reset:
        model_id = ""
    elif arg.isdigit():
        idx = int(arg) - 1
        if idx < 0 or idx >= len(all_models):
            return t(
                "channels.dispatch.model.index_out_of_range",
                arg=arg,
                total=len(all_models),
            )
        model_id = all_models[idx]["model_id"]
    else:
        # id / name / prefix match against the combined list, else verbatim.
        model_id = _match_local_model(all_models, arg) or arg

    try:
        from qai.channels.application.use_cases.manage_settings import (
            UpdateChannelModelCommand,
        )

        await update_uc.execute(
            UpdateChannelModelCommand(
                instance_id=channel_msg.instance_id,
                model_id=model_id,
                model_provider="",
            )
        )
    except ApplicationError as exc:
        return t("channels.dispatch.model.failed", exc=str(exc))
    except Exception as exc:  # noqa: BLE001
        return t("channels.dispatch.model.failed", exc=str(exc))

    if reset or not model_id:
        return t("channels.dispatch.model.reset_ok")

    # Auto-load local models that are not already running (V1 parity).
    auto_load_msg = await _maybe_autoload_local_model(
        services=services, model_id=model_id, local_models=local_models
    )
    return t("channels.dispatch.model.switched", model_id=model_id, auto_load_msg=auto_load_msg)


async def _enumerate_local_models(
    services: DispatchServices,
) -> tuple[list[dict[str, Any]], str]:
    """Return ``(local_models, current_model_id)`` from model_runtime.

    Each entry is ``{"model_id", "name", "is_running"}``.  Degrades to an
    empty list (and empty current) when model_runtime is not wired so the
    channel command still produces a graceful reply.
    """
    container = services.container
    runtime = getattr(container, "model_runtime", None)
    if runtime is None:
        return [], ""
    list_uc = getattr(runtime, "list_models_use_case", None)
    status_uc = getattr(runtime, "get_status_use_case", None)
    current = ""
    if status_uc is not None:
        try:
            status = await status_uc.execute()
            if isinstance(status, dict):
                current = str(status.get("model") or "")
        except Exception:  # noqa: BLE001
            current = ""
    models: list[dict[str, Any]] = []
    if list_uc is not None:
        try:
            infos = await list_uc.execute()
        except Exception:  # noqa: BLE001
            infos = []
        for info in infos or []:
            name = getattr(info, "name", None) or str(info)
            model_id = f"local::{name}"
            models.append(
                {
                    "model_id": model_id,
                    "name": name,
                    "is_running": bool(current) and current in (name, model_id),
                }
            )
    return models, current


def _match_local_model(
    local_models: list[dict[str, Any]], arg: str
) -> str | None:
    """Match ``arg`` against local model id / name / id-prefix (V1 parity)."""
    arg_lower = arg.lower()
    for m in local_models:
        if m["model_id"] == arg or m["name"] == arg:
            return m["model_id"]
    for m in local_models:
        if m["name"].lower() == arg_lower:
            return m["model_id"]
    prefix = [
        m for m in local_models if m["model_id"].lower().startswith(arg_lower)
    ]
    if len(prefix) == 1:
        return prefix[0]["model_id"]
    return None


async def _maybe_autoload_local_model(
    *,
    services: DispatchServices,
    model_id: str,
    local_models: list[dict[str, Any]],
) -> str:
    """Auto-load a ``local::`` model that is not already running (4-M6).

    Returns an appended message fragment (``"\\n\\n<load message>"``) or an
    empty string.  Best-effort: load failures degrade to no extra message.
    """
    if not model_id.startswith("local::"):
        return ""
    runtime = getattr(services.container, "model_runtime", None)
    if runtime is None:
        return ""
    load_uc = getattr(runtime, "load_model_use_case", None)
    if load_uc is None:
        return ""
    target = next(
        (m for m in local_models if m["model_id"] == model_id), None
    )
    if target is not None and target.get("is_running"):
        return ""
    model_name = model_id.split("::", 1)[-1]
    try:
        result = await load_uc.execute(model_name)
    except Exception:  # noqa: BLE001 — auto-load is best-effort
        return ""
    if isinstance(result, dict):
        status = result.get("status") or ""
        if status:
            return t("channels.dispatch.model.autoload_with_status", model_name=model_name, status=status)
    return t("channels.dispatch.model.autoload", model_name=model_name)


async def _resolve_chat_model_fallback(
    *,
    channel_msg: ChannelMessage,
    chat_bridge: "ChatMessageBridge",
    services: DispatchServices,
) -> tuple[str | None, str | None]:
    """Local-unavailable → cloud auto-fallback for the normal-chat turn (M1).

    V0.5 truth: ``_resolve_fallback_model``.  The
    channel resolves its *intended* model (the channel-saved model, else the
    globally selected chat model); when that intention is a **local**
    on-device model (``local::`` prefix, or empty = "follow global / default"
    which routes on-device) but the local inference service is NOT running,
    and at least one cloud model is configured, the turn is temporarily
    routed to the first cloud model and the user is told once.

    Returns ``(model_hint_override, notice)``:

    * ``model_hint_override`` — the cloud model id to use for THIS turn, or
      ``None`` to leave model resolution to the chat bridge (no fallback).
    * ``notice`` — the user-facing ``⚠️ 本地模型当前不可用，已自动切换到云端模型：{name}``
      line (V0.5 verbatim) to deliver before streaming, or ``None``.

    Liveness uses the inference service's process-level ``running`` flag
    (``model_runtime`` ``get_status``), NOT a per-model match: V0.5 used
    ``is_running OR svc_process_running`` so a service that is up but still
    loading the model does NOT trigger a spurious fallback (避免启动期误判).
    Best-effort: any failure degrades to "no fallback" (the chat bridge then
    resolves the model as before) so a status hiccup never blocks a turn.
    """
    try:
        intended = await chat_bridge._resolve_model_hint(channel_msg)
    except Exception:  # noqa: BLE001 — never block a turn
        return None, None
    # An explicit non-local model needs no fallback (V0.5: only local /
    # follow-global intentions are eligible).
    if intended and not intended.startswith("local::"):
        return None, None

    # Intended target is local (explicit ``local::*``) or empty (= default,
    # which routes on-device).  Check whether the local service is up.
    container = services.container
    runtime = getattr(container, "model_runtime", None)
    if runtime is None:
        return None, None
    status_uc = getattr(runtime, "get_status_use_case", None)
    service_running = False
    if status_uc is not None:
        try:
            status = await status_uc.execute()
            if isinstance(status, dict):
                # ``running`` = the managed inference process is alive
                # (deterministic ``Popen.poll`` — process_service.py:473).
                # This is the V0.5 ``svc_process_running`` grace: a service
                # that is up but mid-load must NOT fall back.
                service_running = bool(status.get("running"))
        except Exception:  # noqa: BLE001
            service_running = False
    if service_running:
        return None, None

    # Local not running — fall back to the first cloud model, if any.
    cloud_ids = await _list_catalog_models(services)
    if not cloud_ids:
        # No cloud model to fall back to → leave as-is (V0.5 routes per
        # original config; the chat bridge surfaces the offline notice).
        return None, None
    fallback_id = cloud_ids[0]
    notice = t("channels.dispatch.model.local_unavailable_fallback", fallback_id=fallback_id)
    return fallback_id, notice


async def _handle_model_list(*, services: DispatchServices) -> str:
    """Handle ``/models`` — list local + cloud models with index (4-M6).

    V1 truth: enumerate local (with running marker) then cloud models with a shared
    1-based index, mark the active model, and surface the ``/model 0``
    reset hint.  Falls back to the model_catalog list when model_runtime
    is not wired.
    """
    local_models, current = await _enumerate_local_models(services)
    cloud_items = await _list_catalog_models(services)

    if not local_models and not cloud_items:
        return t("channels.dispatch.models.none")

    lines: list[str] = [
        t("channels.dispatch.models.header", total=len(local_models) + len(cloud_items))
    ]
    idx = 1
    if local_models:
        lines.append(t("channels.dispatch.models.local_header"))
        for m in local_models:
            status = (
                t("channels.dispatch.models.status_running")
                if m["is_running"]
                else t("channels.dispatch.models.status_unloaded")
            )
            lines.append(t("channels.dispatch.models.item", idx=idx, name=m['name'], status=status))
            idx += 1
    if cloud_items:
        if local_models:
            lines.append("")
        lines.append(t("channels.dispatch.models.cloud_header"))
        for cid in cloud_items:
            lines.append(t("channels.dispatch.models.cloud_item", idx=idx, cid=cid))
            idx += 1
    lines.append(
        t("channels.dispatch.models.current", current=current or t("channels.dispatch.model.default_label"))
    )
    lines.append(t("channels.dispatch.models.hint"))
    return "\n".join(lines)


async def _list_catalog_models(services: DispatchServices) -> list[str]:
    """Best-effort flat list of cloud/catalog model ids."""
    container = services.container
    catalog = getattr(container, "model_catalog", None)
    if catalog is None:
        return []
    list_uc = getattr(catalog, "list_models_use_case", None) or getattr(
        catalog, "list_provider_configs_use_case", None
    )
    if list_uc is None:
        return []
    try:
        result = await list_uc.execute()
    except Exception:  # noqa: BLE001
        return []
    items: list[str] = []
    iterable: Any
    if isinstance(result, (list, tuple)):
        iterable = result
    elif hasattr(result, "models"):
        iterable = result.models
    elif hasattr(result, "items"):
        iterable = result.items
    else:
        iterable = []
    for entry in iterable:
        if isinstance(entry, str):
            items.append(entry)
        elif isinstance(entry, dict):
            mid = entry.get("id") or entry.get("model_id") or entry.get(
                "name"
            )
            if mid:
                items.append(str(mid))
        else:
            mid = (
                getattr(entry, "id", None)
                or getattr(entry, "model_id", None)
                or getattr(entry, "name", None)
            )
            if mid:
                items.append(str(mid))
    return items


async def _cancel_pending_grants_for_user(
    *,
    channel_msg: ChannelMessage,
    services: DispatchServices,
) -> int:
    """Cancel the user's pending permission requests (4-M7).

    V1 cancels any outstanding
    permission/grant prompts when the user resets the conversation so a
    stale ASK does not bleed into the new turn.  Resolves the active CC/OC
    session id (the grant subject identifier — see
    :class:`apps.api._channel_grant_bridge.ChannelGrantBridge`), lists the
    security context's pending requests, and cancels those belonging to the
    subject.

    Best-effort: any failure is swallowed (a reset must never 500).  Returns
    the count cancelled.
    """
    container = services.container
    security = getattr(container, "security", None)
    if security is None:
        return 0
    repo = getattr(security, "permission_request_repository", None)
    cancel_uc = getattr(
        security, "cancel_permission_request_use_case", None
    )
    if repo is None or cancel_uc is None:
        return 0
    session_repo = services.channel_session_repo
    try:
        entry = await session_repo.find(
            channel_msg.instance_id, channel_msg.sender
        )
    except Exception:  # noqa: BLE001
        entry = None
    coding_session_id = (
        getattr(entry, "coding_session_id", None) if entry else None
    )
    if not coding_session_id:
        return 0
    try:
        from qai.security.domain.value_objects import Subject

        subject = Subject(kind="user", identifier=str(coding_session_id))
    except Exception:  # noqa: BLE001
        return 0
    try:
        pending = await repo.list_pending()
    except Exception:  # noqa: BLE001
        return 0
    cancelled = 0
    for req in pending or []:
        if getattr(req, "subject", None) != subject:
            continue
        try:
            await cancel_uc.execute(
                request_id=req.request_id, cancelled_by=subject
            )
            cancelled += 1
        except Exception:  # noqa: BLE001 — best-effort
            continue
    if cancelled:
        logger.info(
            "channels.dispatch.cancelled_pending_grants",
            instance_id=channel_msg.instance_id.value,
            user_id=channel_msg.sender.value,
            count=cancelled,
        )
    return cancelled


async def _handle_grant(
    *,
    channel_msg: ChannelMessage,
    args: tuple[str, ...],
    services: DispatchServices,
) -> str:
    """Route ``/grant`` through :class:`ChannelGrantBridge`.

    Argument shapes accepted (mirrors legacy
    ``wechat/session_commands.py``, op-first canonical order):

    * ``/grant <op> <path>``         — grant op on path (V1 canonical,
      e.g. ``/grant read C:/data``).  A lenient path-first fallback is
      also accepted (4-M15 fix).
    * ``/grant revoke <op> <path>``  — revoke grant (op-aware, 4-M15)
    * ``/grant list``                — list grants
    """
    bridge = services.grant_bridge
    if bridge is None:
        return t("channels.dispatch.grant.unavailable")
    if not args:
        return t("channels.dispatch.grant.usage")
    head = args[0].strip().lower()
    if head in ("list", "ls"):
        return await bridge.handle_grant_command(
            instance_id=channel_msg.instance_id,
            user_id=channel_msg.sender,
            verb="list",
        )
    if head in ("revoke", "remove", "rm"):
        # 4-M15 — V1 parses
        # ``/grant revoke <op> <path>`` (op ∈ read/write/exec).  We keep
        # the legacy single-arg ``/grant revoke <path>`` shape working for
        # backward compatibility: when only one positional follows
        # ``revoke`` we treat it as the path with no op filter.
        rest = args[1:]
        if len(rest) < 1:
            return t("channels.dispatch.grant.revoke_usage")
        revoke_op: str | None = None
        revoke_path: str
        if len(rest) >= 2 and rest[0].strip().lower() in (
            "read",
            "write",
            "exec",
        ):
            revoke_op = rest[0].strip().lower()
            revoke_path = rest[1]
        elif len(rest) >= 2 and rest[0].strip().lower() not in (
            "read",
            "write",
            "exec",
        ) and not rest[0].startswith(("/", ".", "~")) and "/" not in rest[0] and "\\" not in rest[0]:
            # First token looks like an (invalid) op rather than a path.
            return t("channels.dispatch.grant.invalid_op", op=rest[0])
        else:
            revoke_path = rest[0]
        return await bridge.handle_grant_command(
            instance_id=channel_msg.instance_id,
            user_id=channel_msg.sender,
            verb="revoke",
            path=revoke_path,
            op=revoke_op,
        )
    # Default add-grant.  4-M15 fix — V1 canonical order is
    # ``/grant <op> <path>`` (op first), e.g. ``/grant read C:/data``
    # (V1: op = parts[0], path = parts[1];
    # also what the help text + grant bridge usage string document).  V2
    # previously parsed ``/grant <path> <op>`` (path first), which broke
    # the V1 user habit — ``/grant read C:/data`` was read as
    # path="read", op="C:/data" and rejected as an invalid op.  We restore
    # op-first while staying lenient: when the first token is NOT a known
    # op but the second one IS, accept the legacy path-first shape too so
    # neither ordering surprises the user.
    if len(args) < 2:
        return t("channels.dispatch.grant.needs_op_and_path")
    _OPS = ("read", "write", "exec", "all")
    a0, a1 = args[0].strip(), args[1].strip()
    if a0.lower() in _OPS:
        # V1 canonical: /grant <op> <path>
        grant_op, grant_path = a0, a1
    elif a1.lower() in _OPS:
        # Lenient back-compat: /grant <path> <op>
        grant_op, grant_path = a1, a0
    else:
        # Neither token is a recognised op — surface V1-style usage.
        return t("channels.dispatch.grant.usage_add")
    return await bridge.handle_grant_command(
        instance_id=channel_msg.instance_id,
        user_id=channel_msg.sender,
        verb="grant",
        path=grant_path,
        op=grant_op,
    )


async def _handle_reboot(*, services: DispatchServices) -> str:
    """Trigger the shared reboot scheduler (PR-097 R-11)."""
    sched = services.reboot_scheduler
    if sched is None:
        return t("channels.dispatch.reboot.unavailable")
    try:
        await sched.schedule(reason="channel:/reboot")
    except Exception as exc:  # noqa: BLE001
        return t("channels.dispatch.reboot.failed", exc=str(exc))
    return t("channels.dispatch.reboot.scheduled")


# ---------------------------------------------------------------------------
# AI-coding stream consumption (PR-097 R-10)
# ---------------------------------------------------------------------------
async def _stream_ai_coding(
    *,
    channel_msg: ChannelMessage,
    session_id: str,
    ai_coding_bridge: "AiCodingChannelBridge",
    delivery: Any,
    aggregator: ToolProgressAggregator,
    tool_formatter: ChannelToolFormatter,
    services: DispatchServices,
    image_b64: str | None = None,
    image_mime: str | None = None,
) -> str:
    """Consume :meth:`AiCodingChannelBridge.stream_with_tools`,
    aggregate tool events, deliver text + batched progress lines.

    Returns the final accumulated reply text so the dispatch can
    surface it in the terminal :class:`OutboundFrame`.

    M3 — ``image_b64`` / ``image_mime`` (decoded by
    :func:`_decode_coding_image`) are forwarded to ``stream_with_tools``
    so a CC/OC session can识图 (V1 ``handle_cc_message(image_b64,
    image_mime)`` parity).
    """
    final_text = ""
    text_parts: list[str] = []
    async for event in ai_coding_bridge.stream_with_tools(
        channel_msg,
        session_id,
        image_b64=image_b64,
        image_mime=image_mime,
    ):
        if event.kind == "text" and event.text:
            text_parts.append(event.text)
            await delivery.deliver(event.text)
        elif event.kind == "subagent" and event.text:
            # 4-M12 — render CC sub-agent lifecycle (start/done/error) as
            # a channel text line.
            await delivery.deliver(event.text)
        elif event.kind == "turn_warning" and event.text:
            # 4-M11 — push the over-turn-count reminder to the channel
            # user.  Delivered inline through the realtime delivery
            # session AND relayed through the reverse notifier's
            # ``turn_warning_sync`` (V1 parity: the warning is pushed to
            # the bound WeChat/Feishu user once per crossed threshold;
            # the use case only emits one frame per new tier so we never
            # double-push the same threshold).
            await delivery.deliver(event.text)
            await _push_turn_warning(
                channel_msg=channel_msg,
                session_id=session_id,
                warning_message=event.text,
                services=services,
            )
        elif event.kind == "tool":
            # V0.5 parity:
            # one progress line per tool, on COMPLETION.  Skip "running" so a
            # single tool is not rendered as two batches (the second losing
            # its name).
            if event.tool_status == "running":
                continue
            status = (
                ToolStatus.SUCCESS
                if event.tool_status == "success"
                else ToolStatus.ERROR
            )
            aggregator.add_rich(
                RichToolEvent(
                    tool_name=event.tool_name,
                    args=event.tool_args,
                    status=status,
                )
            )
            if aggregator.should_flush():
                batch_text = aggregator.flush_rich(tool_formatter)
                if batch_text:
                    await delivery.deliver(batch_text)
        elif event.kind == "done":
            final_text = event.text or "".join(text_parts)
    # Flush any residual tool-progress batch.
    if aggregator.pending_count > 0:
        batch_text = aggregator.flush_rich(tool_formatter)
        if batch_text:
            await delivery.deliver(batch_text)
    return final_text or t("channels.dispatch.ai_coding.empty_reply")


async def _push_turn_warning(
    *,
    channel_msg: ChannelMessage,
    session_id: str,
    warning_message: str,
    services: DispatchServices,
) -> None:
    """Relay an over-turn-count warning via the reverse notifier (4-M11).

    Constructs the :class:`AiCodingToChannelNotifier` over the live
    container and calls :meth:`turn_warning_sync` (V1 parity: the warning
    is pushed to the bound channel user).  Best-effort — the notifier
    already swallows its own failures, and any construction error here is
    logged without aborting the stream.
    """
    try:
        from ._ai_coding_notify_bridge import AiCodingToChannelNotifier

        notifier = AiCodingToChannelNotifier(container=services.container)
        await notifier.turn_warning_sync(
            instance_id=channel_msg.instance_id.value,
            target_user_id=channel_msg.sender.value,
            session_id=session_id,
            warning_message=warning_message,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort relay
        logger.warning(
            "channels.dispatch.turn_warning_relay_failed",
            instance_id=channel_msg.instance_id.value,
            user_id=channel_msg.sender.value,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _resolve_image_attachments(
    *,
    channel_msg: ChannelMessage,
    services: DispatchServices,
) -> list[dict[str, Any]] | None:
    """Build OpenAI-Vision content blocks for an inbound message's images.

    PR-097 K-1 (Sub-L) — restores the legacy inbound-image
    flow: dispatch each :class:`ImageAttachment` carried by the
    inbound :class:`ChannelMessage` to the matching kind-specific
    decoder
    (:mod:`qai.channels.adapters.feishu_image_decoder` /
    :mod:`qai.channels.adapters.wechat_image_decoder`), collect the
    OpenAI-Vision style blocks each decoder returns, and merge them
    into one list the chat bridge can thread through to the LLM
    stream as a multimodal user message.

    Returns ``None`` when the inbound message has no attachments
    (cheap short-circuit; the caller skips multimodal wiring).
    Returns a possibly-empty list when attachments existed; an empty
    list means every attachment failed to decode and the caller
    should fall back to plain-text dispatch.

    Behaviour per kind:

    * :data:`ChannelKind.FEISHU` — calls
      :func:`feishu_image_decoder.build_image_content` with the live
      :class:`FeishuTenantTokenCache` resolved via
      ``services.container.channels.feishu_token_cache_factory``;
      missing factory or HTTP client factory degrades that
      attachment to a text-only fallback block.
    * :data:`ChannelKind.WECHAT` — calls
      :func:`wechat_image_decoder.build_image_content` with a
      ``download_fn`` bound to the live wechatbot SDK ``Bot`` from
      ``services.container.channels.wechat_longpoll_transport``;
      missing transport / inactive bot degrades to a text-only
      fallback block (matches the legacy
      ``"收到一张图片（下载失败，无法显示）"`` UX).

    Failure across the whole resolver is downgrade-not-error: any
    exception is logged at WARNING and we fall back to plain-text
    dispatch so the user still gets a reply.
    """
    attachments: tuple[ImageAttachment, ...] = (
        channel_msg.content.attachments
    )
    if not attachments:
        return None

    container = services.container
    channels_ns = getattr(container, "channels", None)
    blocks: list[dict[str, Any]] = []
    caption = channel_msg.content.text or ""
    # Strip the placeholder "[image]" caption injected by the parsers
    # (PR-097 §2 R-8) so the synthesised "请描述这张图片" prompt is not
    # diluted with literal brackets when it reaches the LLM.
    if caption.strip() == "[image]":
        caption = ""

    for attachment in attachments:
        try:
            kind_blocks = await _decode_attachment(
                attachment=attachment,
                channel_msg=channel_msg,
                caption=caption,
                channels_ns=channels_ns,
            )
        except Exception as exc:  # noqa: BLE001 — degrade-not-error
            logger.warning(
                "channels.dispatch.image_decode_failed",
                instance_id=channel_msg.instance_id.value,
                user_id=channel_msg.sender.value,
                kind=attachment.kind.value,
                message_id=attachment.message_id,
                image_key=attachment.image_key,
                error=str(exc),
            )
            kind_blocks = [
                {
                    "type": "text",
                    "text": t("channels.dispatch.image.download_failed"),
                }
            ]
        if kind_blocks:
            blocks.extend(kind_blocks)
        # Caption is shared across attachments — only include it once
        # so the LLM does not see "请描述这张图片" repeated per image.
        caption = ""
    return blocks


def _fallback_image_text_block() -> dict[str, Any]:
    return {
        "type": "text",
        "text": t("channels.dispatch.image.download_failed"),
    }


async def _decode_attachment(
    *,
    attachment: ImageAttachment,
    channel_msg: ChannelMessage,
    caption: str,
    channels_ns: Any,
) -> list[dict[str, Any]]:
    """Dispatch a single :class:`ImageAttachment` to the kind decoder.

    Internal helper for :func:`_resolve_image_attachments` — split out
    so the per-kind branch logic stays readable.  Returns whatever
    block list the decoder produces (the decoders themselves return a
    text-only fallback block on download failure rather than raising,
    so this function rarely surfaces an exception in practice).
    """
    if attachment.kind is ChannelKind.FEISHU:
        return await _decode_feishu_attachment(
            attachment=attachment,
            channel_msg=channel_msg,
            caption=caption,
            channels_ns=channels_ns,
        )

    if attachment.kind is ChannelKind.WECHAT:
        return await _decode_wechat_attachment(
            attachment=attachment,
            caption=caption,
            channels_ns=channels_ns,
        )

    # No other kind has inbound image attachments in the supported set.
    return []


async def _decode_feishu_attachment(
    *,
    attachment: ImageAttachment,
    channel_msg: ChannelMessage,
    caption: str,
    channels_ns: Any,
) -> list[dict[str, Any]]:
    """Resolve a Feishu :class:`ImageAttachment` to OpenAI-Vision blocks.

    Threads the per-instance :class:`FeishuTenantTokenCache` through
    the Feishu image decoder.  Looks the instance up via the channels
    instance repository so the token cache used for download matches
    the instance the message originated on (operators rotating an
    app_secret on instance A must not see instance B downloads
    spoofed against their token).
    """
    token_cache_factory = getattr(
        channels_ns, "feishu_token_cache_factory", None
    )
    http_client_factory = getattr(
        channels_ns, "http_client_factory", None
    )
    instance_repo = getattr(channels_ns, "instance_repository", None)
    if (
        token_cache_factory is None
        or http_client_factory is None
        or instance_repo is None
    ):
        return [_fallback_image_text_block()]
    try:
        instance = await instance_repo.get(channel_msg.instance_id)
    except Exception as exc:  # noqa: BLE001 — degrade-not-error
        logger.warning(
            "channels.dispatch.feishu_image_instance_lookup_failed",
            instance_id=channel_msg.instance_id.value,
            error=str(exc),
        )
        return [_fallback_image_text_block()]
    try:
        cache = token_cache_factory(instance)
    except Exception as exc:  # noqa: BLE001 — degrade-not-error
        logger.warning(
            "channels.dispatch.feishu_image_token_cache_failed",
            instance_id=channel_msg.instance_id.value,
            error=str(exc),
        )
        return [_fallback_image_text_block()]
    return await _feishu_image_decoder.build_image_content(
        message_id=attachment.message_id,
        image_key=attachment.image_key,
        caption=caption,
        tenant_token_cache=cache,
        http_client_factory=http_client_factory,
    )


async def _decode_wechat_attachment(
    *,
    attachment: ImageAttachment,
    caption: str,
    channels_ns: Any,
) -> list[dict[str, Any]]:
    """Resolve a WeChat :class:`ImageAttachment` to OpenAI-Vision blocks.

    The wechatbot SDK's ``Bot.download(msg_handle)`` accepts the SDK
    message handle and returns raw bytes (sync or coroutine).  The
    WeChat long-poll path stores the SDK handle / message id on the
    :attr:`ImageAttachment.image_key` field; this helper wraps a thin
    ``download_fn`` callable so the decoder stays free of any SDK
    reference (the channels adapter never imports ``wechatbot``).
    """
    longpoll = getattr(channels_ns, "wechat_longpoll_transport", None)
    bot = getattr(longpoll, "_bot", None) if longpoll else None
    if bot is None:
        return [_fallback_image_text_block()]
    msg_handle = attachment.image_key

    async def _download() -> bytes | None:
        try:
            result = bot.download(msg_handle)
        except Exception:  # noqa: BLE001 — degrade-not-error
            return None
        if asyncio.iscoroutine(result):
            try:
                result = await result
            except Exception:  # noqa: BLE001
                return None
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        return None

    decoded = await _wechat_image_decoder.download_image_base64(_download)
    if decoded is None:
        return _wechat_image_decoder.build_image_content(
            image_b64=None,
            mime_type=None,
            text=caption,
        )
    b64, mime = decoded
    return _wechat_image_decoder.build_image_content(
        image_b64=b64,
        mime_type=mime,
        text=caption,
    )


async def _decode_coding_image(
    *,
    channel_msg: ChannelMessage,
    services: DispatchServices,
) -> tuple[str, str] | None:
    """Decode the FIRST inbound image to raw ``(base64, mime)`` for CC/OC.

    M3 — the CC/OC ai_coding path needs the raw base64 + MIME pair (it
    forwards them on :class:`SendUserMessageCommand`, which the provider
    stages into the Anthropic multimodal request via ``attach_image``),
    NOT the OpenAI-Vision content blocks the normal-chat path builds via
    :func:`_resolve_image_attachments`.  This helper reuses the SAME
    per-kind download wiring (Feishu tenant-token cache /
    :func:`feishu_image_decoder.download_image_base64`; WeChat SDK
    ``Bot.download`` / :func:`wechat_image_decoder.download_image_base64`)
    — only the OUTPUT shape differs (raw pair vs vision blocks), so we do
    not duplicate the download logic, we just call the decoders'
    ``download_image_base64`` entry points directly.

    Mirrors V1 which
    called ``_download_image_base64(msg)`` → ``(image_b64, image_mime)``
    and passed the pair into ``handle_cc_message``.

    Returns ``None`` when the inbound message carries no image, the
    provider wiring is missing, or the download fails (degrade-not-error:
    the CC turn proceeds text-only, exactly like V1 when the download
    returned ``None``).
    """
    attachments: tuple[ImageAttachment, ...] = (
        channel_msg.content.attachments
    )
    if not attachments:
        return None
    attachment = attachments[0]
    container = services.container
    channels_ns = getattr(container, "channels", None)
    if channels_ns is None:
        return None

    try:
        if attachment.kind is ChannelKind.FEISHU:
            token_cache_factory = getattr(
                channels_ns, "feishu_token_cache_factory", None
            )
            http_client_factory = getattr(
                channels_ns, "http_client_factory", None
            )
            instance_repo = getattr(channels_ns, "instance_repository", None)
            if (
                token_cache_factory is None
                or http_client_factory is None
                or instance_repo is None
            ):
                return None
            instance = await instance_repo.get(channel_msg.instance_id)
            cache = token_cache_factory(instance)
            return await _feishu_image_decoder.download_image_base64(
                message_id=attachment.message_id,
                image_key=attachment.image_key,
                tenant_token_cache=cache,
                http_client_factory=http_client_factory,
            )
        if attachment.kind is ChannelKind.WECHAT:
            longpoll = getattr(
                channels_ns, "wechat_longpoll_transport", None
            )
            bot = getattr(longpoll, "_bot", None) if longpoll else None
            if bot is None:
                return None
            msg_handle = attachment.image_key

            async def _download() -> bytes | None:
                try:
                    result = bot.download(msg_handle)
                except Exception:  # noqa: BLE001 — degrade-not-error
                    return None
                if asyncio.iscoroutine(result):
                    try:
                        result = await result
                    except Exception:  # noqa: BLE001
                        return None
                if isinstance(result, (bytes, bytearray)):
                    return bytes(result)
                return None

            return await _wechat_image_decoder.download_image_base64(
                _download
            )
    except Exception as exc:  # noqa: BLE001 — degrade-not-error
        logger.warning(
            "channels.dispatch.coding_image_decode_failed",
            instance_id=channel_msg.instance_id.value,
            kind=attachment.kind.value,
            error=str(exc),
        )
        return None
    return None


def _bridge_reply_to_frame(reply: BridgeReply) -> OutboundFrame:
    """Wrap a :class:`BridgeReply` as a single :class:`OutboundFrame`."""
    return OutboundFrame(
        kind=OutboundFrameKind.REPLY,
        text=reply.reply_text or "",
        coding_session_id=reply.coding_session_id,
    )


async def _emit_via_delivery(
    *,
    text: str,
    channel_msg: ChannelMessage,
    services: DispatchServices,
    kind: str,
) -> OutboundFrame:
    """Push ``text`` through the realtime delivery service AND build the
    matching :class:`OutboundFrame` for the route layer's structured log.

    Used for bare-command replies which are short, single-text
    messages — opening a full delivery context would be overkill, so
    we open one just for the single ``deliver`` call.
    """
    reply_in_context = (
        services.reply_in_context_factory(channel_msg)
        if services.reply_in_context_factory is not None
        else None
    )
    send_to_user_cb = (
        services.send_to_user(channel_msg)
        if services.send_to_user is not None
        else _build_default_send_to_user(channel_msg, services)
    )
    # NOTE: no "Thinking" acknowledgement here. ``_emit_via_delivery`` serves
    # the fast paths (e.g. bare slash-commands like /help, /model) that reply
    # almost instantly, so an ack would just be noise before the real reply.
    # The ack lives only on the slow LLM/tool path in ``dispatch_inbound_message``.
    send_typing_cb = (
        services.send_typing(channel_msg)
        if services.send_typing is not None
        else None
    )
    try:
        async with services.delivery_service.context(
            instance_id=channel_msg.instance_id,
            user_id=channel_msg.sender,
            reply_in_context=reply_in_context,
            send_to_user=send_to_user_cb,
            send_typing=send_typing_cb,
        ) as session:
            await session.deliver(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "channels.dispatch.bare_command_delivery_failed",
            instance_id=channel_msg.instance_id.value,
            user_id=channel_msg.sender.value,
            error=str(exc),
        )
    return OutboundFrame(kind=kind, text=text)


#: Process-wide memo of the chat image store keyed by its resolved root
#: path, so the ``url->bytes`` resolver is not rebuilt on every outbound
#: send.  The store is a thin ``root``-holding object with no I/O in its
#: constructor beyond ``mkdir(exist_ok=True)``.
_IMAGE_STORE_CACHE: dict[str, Any] = {}


def _build_image_url_to_bytes(
    container: "Container",
) -> "Callable[[str], bytes | None] | None":
    """Build the opaque ``url -> bytes`` resolver for outbound images.

    Context-isolation seam: the chat image store
    (:class:`FileSystemImageUploadStore`) lives in the ``qai.chat``
    bounded context, which the ``qai.channels`` context must NOT import
    (``.importlinter`` ``context-isolation``).  This composition-root
    function — living in the ``apps`` layer, which is explicitly allowed
    to bridge contexts — closes over the store's ``get_path`` (URL →
    on-disk path, with the traversal guard) and hands the channels
    dispatcher only a plain ``Callable[[str], bytes | None]``.  The
    channels code never sees a chat symbol; it just calls the callable.

    Returns ``None`` when the data root is unavailable (lean test
    containers) so the caller degrades to a text-only reply.
    """
    data_paths = getattr(container, "data_paths", None)
    if data_paths is None:
        return None
    try:
        # ARCH-2 parity with ``_chat_di`` / ``_spa_mount``: chat images
        # are a chat-context blob under ``data/blobs/chat`` served at the
        # V1-locked ``/api/images/files/`` prefix.
        images_root = data_paths.blob_dir("chat")
    except Exception:  # noqa: BLE001
        return None
    root_key = str(images_root)
    store = _IMAGE_STORE_CACHE.get(root_key)
    if store is None:
        from qai.chat.adapters.image_upload_store import (
            FileSystemImageUploadStore,
        )

        store = FileSystemImageUploadStore(root=images_root)
        _IMAGE_STORE_CACHE[root_key] = store

    def _resolve(url: str) -> "bytes | None":
        try:
            path = store.get_path(url)
            if path is None:
                return None
            return path.read_bytes()
        except Exception:  # noqa: BLE001
            logger.warning(
                "channels.dispatch.image_bytes_read_failed", url=url
            )
            return None

    return _resolve


def _build_default_send_to_user(
    channel_msg: ChannelMessage,
    services: DispatchServices,
) -> Callable[[str, str], Awaitable[bool]]:
    """Default ``send_to_user`` callable backed by the channel's
    outbound transport.

    Used when the DI wiring did not supply a kind-specific callable
    (typical for HTTP-driven providers like Feishu).  Looks
    up the instance + transport through ``container.channels`` and
    invokes :meth:`ChannelTransportPort.send`.

    For Feishu group messages (``channel_msg.group_id`` is set) the
    reply is sent to the group chat via ``receive_id_type=chat_id``
    rather than back to the individual sender (Zagent parity).
    """
    container = services.container

    async def _send(target_user_id: str, text: str) -> bool:
        channels = getattr(container, "channels", None)
        if channels is None:
            return False
        instance_repo = getattr(channels, "instance_repository", None)
        if instance_repo is None:
            return False
        try:
            instance = await instance_repo.get(channel_msg.instance_id)
        except Exception:  # noqa: BLE001
            return False
        transport_for_kind = getattr(channels, "transport_for_kind", None)
        if transport_for_kind is None:
            return False
        try:
            # PR-097 R-6 parity — route the outbound reply through the
            # OutboundReplyDispatcher so long replies are split at the
            # per-kind char limit (WeChat 4000) with a ``(i/N)`` page suffix
            # and a 50ms inter-chunk gap (V0.5
            # ``reply_long``).  Previously the
            # inbound path called ``transport.send`` with the full text, so a
            # long normal-chat / command reply was truncated or rejected by
            # the WeChat 微信硬上限.  We reuse the existing dispatcher rather
            # than re-implementing chunking (复用 > 重造, AGENTS.md 细则2).
            from qai.channels.adapters.reply_dispatcher import (
                OutboundReplyDispatcher,
                split_image_refs,
            )

            dispatcher = OutboundReplyDispatcher(
                transport_factory=lambda _inst: transport_for_kind(
                    channel_msg.kind
                )
            )

            # Screenshot delivery (computer tool): the assistant reply may
            # embed ``![screenshot](/api/images/files/...)`` markdown. On a
            # phone the raw markdown is un-renderable text, so we split it
            # into the去图纯文本 (sent as the text reply) plus the ordered
            # list of image URLs (each pushed as a native image message).
            clean_text, image_urls = split_image_refs(text)

            # Feishu group message: reply to the *group chat*
            # (``receive_id_type=chat_id``) rather than the individual
            # sender.  We pass a ``send_fn`` so the group reply still gets the
            # dispatcher's chunking + rich-text handling (previously the group
            # path bypassed the dispatcher and could not split long replies or
            # preserve rich text).  A parallel ``send_image_fn`` routes images
            # to ``send_image_to_chat`` for the same group.
            group_id = getattr(channel_msg, "group_id", None)
            send_fn = None
            send_image_fn = None
            if channel_msg.kind is ChannelKind.FEISHU and group_id:
                transport = transport_for_kind(channel_msg.kind)
                send_to_chat = getattr(transport, "send_to_chat", None)
                if send_to_chat is not None:

                    async def _send_to_group(
                        content: MessageContent,
                    ) -> str:
                        return await send_to_chat(  # type: ignore[misc]
                            instance, group_id, content
                        )

                    send_fn = _send_to_group

                send_image_to_chat = getattr(
                    transport, "send_image_to_chat", None
                )
                if send_image_to_chat is not None:

                    async def _send_image_to_group(data: bytes) -> str:
                        return await send_image_to_chat(  # type: ignore[misc]
                            instance, group_id, data
                        )

                    send_image_fn = _send_image_to_group

            # Text reply first (去图后的纯文本).  When the reply was
            # image-only the clean text is empty — skip the text send
            # (``MessageContent.text`` requires non-empty) and go straight
            # to the images.
            if clean_text:
                await dispatcher.dispatch(
                    instance,
                    ChannelUserId(value=target_user_id),
                    MessageContent(text=clean_text),
                    channel_msg.message_id,
                    send_fn=send_fn,
                )

            # Then each screenshot as its own image message.
            if image_urls:
                resolver = _build_image_url_to_bytes(container)
                if resolver is None:
                    logger.warning(
                        "channels.dispatch.image_resolver_unavailable",
                        instance_id=channel_msg.instance_id.value,
                    )
                else:
                    await dispatcher.dispatch_images(
                        instance,
                        ChannelUserId(value=target_user_id),
                        image_urls,
                        image_url_to_bytes=resolver,
                        send_image_fn=send_image_fn,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.dispatch.send_to_user_failed",
                instance_id=channel_msg.instance_id.value,
                user_id=target_user_id,
                error=str(exc),
            )
            return False
        return True

    return _send


# Make ChannelKind importable from this module for callers that already
# import OutboundFrame here.  Re-exporting keeps the public surface
# stable across the PR-097 split.
_ = ChannelKind


# ---------------------------------------------------------------------------
# Public lock-map accessor (used by tests + manual cleanup)
# ---------------------------------------------------------------------------
def _reset_locks_for_test() -> None:  # pragma: no cover
    """Test helper: clear the module-level lock map.

    Not part of the public API — kept module-private (single
    leading underscore is the convention for "test-only seam") so
    production code never depends on it.
    """
    _DISPATCH_LOCKS.clear()
