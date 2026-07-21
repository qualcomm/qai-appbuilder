# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Chat WebSocket route — PR-033 stage C.

Single endpoint:

* ``WS /api/chat/ws`` — bidirectional WebSocket. The client supplies
  ``conversation_id`` and ``tab_id`` as query string params at upgrade
  time; the server sends a ``ready`` envelope, then each ``send``
  command opens one streaming chat turn.

Wire format (JSON envelope per frame; locked here for PR-035 too)
-----------------------------------------------------------------

Server → client (handshake):
    ``{"type": "ready", "session_id": "<tab_id>"}``

Client → server:
    ``{"type": "send",  "prompt": "..."}`` — start a new turn
    ``{"type": "stop"}``                   — abort the in-flight turn

Server → client (during a turn):
    ``{"type": "frame", "frame": {<StreamFrame projection>}}``
        where the frame projection mirrors the SSE ``data`` shape:
        ``{"frame_id": "...", "frame_type": "...", "sequence": int,
           "payload": {...}}``.
    ``{"type": "error", "error": {<QaiError.to_dict()>}}`` — terminal
    ``{"type": "done"}``                                    — terminal

The server closes the WS with code 1000 after a ``done`` or ``error``
envelope, OR if the client sends an unrecognised command shape.

Routes intentionally do NOT instantiate any global ``WebSocketEndpoint``
— per S3 spec §6 invariant, all wiring is scoped inside the factory.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from qai.chat.application.use_cases.streaming import (
    StopChatInput,
    StreamChatInput,
)
from qai.chat.domain.content import MessageContent
from qai.chat.domain.errors import (
    ConversationNotFoundError,
    InvalidMessageContentError,
)
from qai.chat.domain.ids import ConversationId, SubAgentSessionId, TabId
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.platform.errors import QaiError
from qai.platform.logging import get_logger

from ._sse import (  # private but in-package reuse OK
    _HEARTBEAT_INTERVAL_SECONDS,
    _extract_image_refs,
    _resolve_image_refs_to_vision_blocks,
    _resolve_or_create_tab,
)
from ._ws_utils import safe_send_json as _safe_send_json

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


logger = get_logger(__name__)


def _frame_to_envelope(frame: StreamFrame) -> dict[str, Any]:
    """Project a :class:`StreamFrame` into the WS wire dict."""
    return {
        "type": "frame",
        "frame": {
            "frame_id": frame.frame_id,
            "frame_type": frame.frame_type.value,
            "sequence": frame.sequence,
            "payload": dict(frame.payload),
        },
    }


def _frame_projection(frame: StreamFrame) -> dict[str, Any]:
    """Project a frame to the shared SSE/WS wire payload shape."""
    return {
        "frame_id": frame.frame_id,
        "frame_type": frame.frame_type.value,
        "sequence": frame.sequence,
        "payload": dict(frame.payload),
    }


def _error_envelope(error: QaiError) -> dict[str, Any]:
    return {"type": "error", "error": error.to_dict()}


# Heartbeat sentinel + envelope (root-cause fix for "long silent tool → WS
# idle-dropped by an intermediary → reconnect → tool frames swallowed →
# tool card not rendered live, only after reload"). The chat WS previously had
# NO keep-alive (unlike the SSE route's 15 s ``: ping`` — ``_sse.py``), so a
# long tool with no STDOUT (e.g. a multi-minute MSVC compile) left the socket
# completely idle and any proxy / browser / OS idle-timeout could drop it.
# We mirror the SSE heartbeat cadence + its proven queue-based producer /
# consumer shape (NEVER cancel the producer mid-stream — that would tear down
# the upstream agentic generator) so an idle turn still emits a lightweight
# ``{"type":"ping"}`` envelope every ``_HEARTBEAT_INTERVAL_SECONDS`` to keep
# the connection alive. §3.1: ``ping`` is a NEW envelope type appended to the
# WS protocol — no existing envelope (send/stop/ready/frame/error/done) shape
# changes.
_WS_HEARTBEAT: Any = object()
"""Sentinel yielded by :func:`_iter_frames_with_heartbeat` on an idle window."""

_WS_STOP: Any = object()
"""Sentinel marking the source generator is exhausted."""


class _WsProducerError:
    """Carries an exception raised inside the producer to the consumer."""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


async def _iter_frames_with_heartbeat(
    source: AsyncIterator[StreamFrame],
) -> AsyncIterator[Any]:
    """Yield frames from ``source`` plus periodic :data:`_WS_HEARTBEAT` markers.

    Mirrors the SSE route's ``_with_heartbeat`` (``_sse.py``) verbatim in shape:
    a single long-lived producer task drains ``source`` into a queue; the
    consumer waits on the queue with ``_HEARTBEAT_INTERVAL_SECONDS`` timeout,
    yielding :data:`_WS_HEARTBEAT` on each idle window. The producer is NEVER
    cancelled mid-stream (only after it finishes naturally or the consumer
    generator is closed) — cancelling per ``__anext__`` would tear down the
    upstream agentic generator (which may hold an ``async with httpx.stream``
    open for a cloud LLM round), truncating the turn. A producer exception is
    forwarded as :class:`_WsProducerError` so the consumer can re-raise it on
    the caller's stack (preserving the existing ``QaiError`` / generic error
    handling in :func:`_run_one_turn`).
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def _producer() -> None:
        try:
            async for frame in source:
                await queue.put(frame)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except BaseException as exc:  # noqa: BLE001 - propagate to consumer
            await queue.put(_WsProducerError(exc))
        finally:
            await queue.put(_WS_STOP)

    producer_task = asyncio.create_task(_producer())
    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=_HEARTBEAT_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                # Idle window — emit a heartbeat and keep waiting. The producer
                # is untouched (still draining the upstream agentic stream).
                yield _WS_HEARTBEAT
                continue
            if item is _WS_STOP:
                return
            if isinstance(item, _WsProducerError):
                raise item.exc
            yield item
    finally:
        if not producer_task.done():
            # Cancellation is required on client disconnect / server shutdown,
            # but awaiting the producer immediately can race an upstream
            # ``async with httpx.AsyncClient.stream(...)`` that is already inside
            # ``__anext__`` on Python 3.13, producing the noisy shutdown warning
            # ``RuntimeError: aclose(): asynchronous generator is already
            # running``.  Close the *outer* generator first; if it is mid-yield
            # the aclose may legitimately raise that RuntimeError, so treat it
            # as best-effort and then drain the producer with a short bound.
            try:
                await source.aclose()
            except RuntimeError as exc:
                if "asynchronous generator is already running" not in str(exc):
                    raise
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - best-effort close
                logger.debug("chat.ws.source_aclose_failed", exc_info=True)
            producer_task.cancel()
            try:
                await asyncio.wait_for(producer_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                # We cancelled it (or shutdown is already tearing down the loop);
                # either case is expected and benign.
                pass
            except RuntimeError as exc:
                if "asynchronous generator is already running" in str(exc):
                    logger.debug(
                        "chat.ws.producer_task_already_closing",
                        exc_info=True,
                    )
                else:
                    logger.warning(
                        "chat.ws.producer_task_cleanup_failed",
                        exc_info=True,
                    )
            except Exception:  # noqa: BLE001 - best-effort drain
                logger.warning(
                    "chat.ws.producer_task_cleanup_failed",
                    exc_info=True,
                )


def build_router(*, container: "Container") -> APIRouter:
    """Build the chat WS router."""
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    @router.websocket("/ws")
    async def chat_ws(
        websocket: WebSocket,
        conversation_id: str = Query(..., min_length=1, max_length=64),
        tab_id: str = Query(..., min_length=1, max_length=64),
    ) -> None:
        await websocket.accept()
        try:
            conv = await container.chat.conversations.get(
                ConversationId.of(conversation_id),
            )
            tab = await _resolve_or_create_tab(
                container=container,
                tab_id=TabId.of(tab_id),
                conversation=conv,
            )
        except QaiError as exc:
            # ``send_json`` here runs in the accept→first-frame race window
            # (the client can disconnect between ``accept()`` and the very
            # first server send — page reload, tab close mid-handshake).
            # ``safe_send_json`` swallows that race; if False, the peer is
            # already gone so the subsequent ``close()`` would just raise
            # ``RuntimeError`` — also tolerated. Same protocol contract as
            # the prior raw-send path.
            await _safe_send_json(websocket, _error_envelope(exc))
            try:
                await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            except RuntimeError:
                pass
            return

        # First server→client frame after ``accept()``. The accept→ready window
        # is the canonical race the helper guards (mirrors the ``_control_ws.py``
        # hello-send fix at line 165 — same root cause: a client that opens then
        # immediately closes before we send our first envelope).
        if not await _safe_send_json(
            websocket,
            {"type": "ready", "session_id": tab.id.value},
        ):
            return

        try:
            while True:
                try:
                    msg = await websocket.receive_json()
                except WebSocketDisconnect:
                    return
                except ValueError:
                    await websocket.send_json(
                        _error_envelope(
                            InvalidMessageContentError(
                                "websocket payload must be JSON",
                            ),
                        ),
                    )
                    await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                    return

                kind = msg.get("type") if isinstance(msg, dict) else None
                if kind == "send":
                    prompt = msg.get("prompt")
                    if not isinstance(prompt, str) or not prompt:
                        await websocket.send_json(
                            _error_envelope(
                                InvalidMessageContentError(
                                    "send.prompt must be a non-empty string",
                                ),
                            ),
                        )
                        await websocket.close(
                            code=status.WS_1003_UNSUPPORTED_DATA,
                        )
                        return
                    # Optional feature-mode fields (additive to the existing
                    # ``send`` envelope; frame format unchanged).  Forwarded
                    # to the system-prompt builder via StreamChatInput.extra.
                    ws_tool_mode = msg.get("tool_mode")
                    ws_tool_params = msg.get("tool_params")
                    ws_model_id = msg.get("model_id")
                    # UI language (en / zh-CN / zh-TW) so the system-prompt
                    # builder can localize its feature-mode framing. Additive
                    # per §3.1; flows to the builder via extra["locale"].
                    ws_locale = msg.get("locale")
                    # Channel sync push fields (V1 parity: main.py:6649-6655)
                    ws_wechat_sync = msg.get("wechat_sync_user_id")
                    ws_feishu_sync = msg.get("feishu_sync_user_id")
                    # SSE-parity advanced fields (additive; bring the WS data
                    # plane to functional parity with the SSE route so the
                    # default transport can be WS without losing features —
                    # see ``_sse.py`` query params of the same names).
                    #   * sampling overrides (temperature / top_p / max_tokens)
                    #     merge into tool_params exactly like the SSE route
                    #     (``_sse.py:717-732``).
                    #   * subagent_id / allow_question drive sub-agent take-over.
                    ws_temperature = msg.get("temperature")
                    ws_top_p = msg.get("top_p")
                    ws_max_tokens = msg.get("max_tokens")
                    ws_subagent_id = msg.get("subagent_id")
                    ws_allow_question = msg.get("allow_question")
                    # Sub-agent spawn-permission switches (SSE parity
                    # ``_sse.py``). ``allow_child_spawn`` (main-agent turn): let
                    # first-level sub-agents spawn grand sub-agents.
                    # ``self_allow_spawn`` (take-over turn): let THIS sub-agent
                    # spawn its own sub-agents. Both default off.
                    ws_allow_child_spawn = msg.get("allow_child_spawn")
                    ws_self_allow_spawn = msg.get("self_allow_spawn")
                    # Per-session ("this conversation only") tool / SKILL
                    # override (additive; SSE parity ``disabled_tools`` /
                    # ``disabled_skills`` query params). Arrays of names the
                    # user switched OFF for this session; applied per-turn only.
                    ws_disabled_tools = msg.get("disabled_tools")
                    ws_disabled_skills = msg.get("disabled_skills")

                    merged_params: dict[str, Any] = {}
                    if isinstance(ws_tool_params, dict):
                        merged_params.update(ws_tool_params)
                    if isinstance(ws_temperature, (int, float)):
                        merged_params["temperature"] = float(ws_temperature)
                    if isinstance(ws_top_p, (int, float)):
                        merged_params["top_p"] = float(ws_top_p)
                    if isinstance(ws_max_tokens, int) and ws_max_tokens > 0:
                        merged_params["max_tokens"] = int(ws_max_tokens)

                    await _run_one_turn(
                        websocket=websocket,
                        container=container,
                        conversation_id=conv.id,
                        tab_id=tab.id,
                        prompt=prompt,
                        tool_mode=ws_tool_mode
                        if isinstance(ws_tool_mode, str)
                        else None,
                        tool_params=merged_params if merged_params else None,
                        model_id=ws_model_id
                        if isinstance(ws_model_id, str) and ws_model_id
                        else None,
                        locale=ws_locale
                        if isinstance(ws_locale, str) and ws_locale
                        else None,
                        wechat_sync_user_id=ws_wechat_sync
                        if isinstance(ws_wechat_sync, str) and ws_wechat_sync
                        else None,
                        feishu_sync_user_id=ws_feishu_sync
                        if isinstance(ws_feishu_sync, str) and ws_feishu_sync
                        else None,
                        subagent_id=ws_subagent_id
                        if isinstance(ws_subagent_id, str) and ws_subagent_id
                        else None,
                        allow_question=bool(ws_allow_question),
                        allow_child_spawn=bool(ws_allow_child_spawn),
                        self_allow_spawn=bool(ws_self_allow_spawn),
                        disabled_tools=[
                            str(n) for n in ws_disabled_tools if isinstance(n, str) and n
                        ]
                        if isinstance(ws_disabled_tools, list)
                        else None,
                        disabled_skills=[
                            str(n) for n in ws_disabled_skills if isinstance(n, str) and n
                        ]
                        if isinstance(ws_disabled_skills, list)
                        else None,
                    )
                    return
                elif kind == "stop":
                    await container.chat.stop_chat_use_case.execute(
                        StopChatInput(tab_id=tab.id, reason="ws_client_stop"),
                    )
                    await websocket.send_json({"type": "done"})
                    await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                    return
                else:
                    await websocket.send_json(
                        _error_envelope(
                            InvalidMessageContentError(
                                f"unsupported command type {kind!r}",
                            ),
                        ),
                    )
                    await websocket.close(
                        code=status.WS_1003_UNSUPPORTED_DATA,
                    )
                    return
        except WebSocketDisconnect:
            # Client gone; signal abort so an in-flight stream wraps up.
            await container.chat.stop_chat_use_case.execute(
                StopChatInput(
                    tab_id=tab.id, reason="ws_disconnect"
                ),
            )

    @router.websocket("/subagents/{subagent_id}/ws")
    async def subagent_ws(
        websocket: WebSocket,
        subagent_id: str,
        from_seq: int = Query(default=0, ge=0),
    ) -> None:
        """Live WebSocket stream of a sub-agent's events (block 2; WS variant).

        Pure server→client push: a standalone sub-agent tab opens this to
        (1) backfill the events it missed since the sub-agent started (the
        broadcaster replays its buffer from cursor 0) and (2) follow live
        frames until the sub-agent finishes, then the server closes.

        WS (not SSE) so concurrent sub-agent tabs do NOT each consume one of
        the browser's ~6 per-host HTTP/1.1 connections (the same reason the
        main chat stream is WS). The replay state machine is SHARED with the
        SSE endpoint (:meth:`SubAgentStreamBroadcaster.replay`): it serves the
        in-memory frame buffer when an entry exists, else falls back to the
        persisted :class:`SubAgentSession` snapshot (restart / TTL expired).

        ``from_seq`` (default 0, byte-parity with the historical contract;
        additive per §3.1) — a reconnecting client that already applied
        frames up to sequence *S* passes ``from_seq=S + 1`` so the
        broadcaster emits ONLY frames with ``sequence >= from_seq``.
        Together with cross-run sequence inheritance in
        :meth:`SubAgentStreamBroadcaster.register`, this eliminates the
        "切走切回子 Agent 标签, 之前已渲染的内容再次逐段重播" duplication
        without any client-side dedupe layer: the server simply does not
        re-send what the client has already rendered. Mirrors the
        main-agent ``active_run_ws`` design (block 2 parity).

        Wire shape (JSON envelope per frame; mirrors the main chat WS):
        ``{"type": "frame", "event": "<frame|state|done|error>",
           "payload": {...}}`` per replayed event, then a terminal
        ``{"type": "done"}`` and a normal close. The frontend normalises each
        ``payload`` exactly like it did the SSE ``frame`` event, so the render
        pipeline is unchanged.

        There is no inbound command channel — a sub-agent progress view is
        read-only (take-over / interrupt go through their own routes), so the
        server only watches for a client disconnect to stop pushing.
        """
        await websocket.accept()
        sid = SubAgentSessionId.of(subagent_id)
        broadcaster = container.chat.subagent_stream_broadcaster
        repo = container.chat.subagent_sessions
        try:
            async for event_name, payload in broadcaster.replay(
                sid, repository=repo, from_seq=from_seq,
            ):
                # First iteration runs in the accept→first-frame race window
                # (see :func:`_safe_send_json` docstring). On race exit we
                # ``return`` — the ``replay`` generator's ``finally`` releases
                # the broadcaster subscriber slot when this coroutine is GC'd.
                if not await _safe_send_json(
                    websocket,
                    {"type": "frame", "event": event_name, "payload": payload},
                ):
                    return
            # Replay exhausted (sub-agent terminal) — signal the client to
            # stop waiting, then close normally.
            await _safe_send_json(websocket, {"type": "done"})
            try:
                await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
            except RuntimeError:
                # Already closed (race during shutdown) — fine.
                pass
        except WebSocketDisconnect:
            # Client closed the tab; the broadcaster's per-subscriber event is
            # released by ``replay``'s ``finally`` when the generator is GC'd /
            # aclosed. Nothing else to clean up (read-only stream).
            return
        except QaiError as exc:
            # Error envelope may also race the accept (when ``replay`` raises
            # immediately, e.g. unresolvable sid).
            await _safe_send_json(websocket, _error_envelope(exc))
            try:
                await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            except (RuntimeError, WebSocketDisconnect):
                pass

    @router.websocket("/active-runs/{tab_id}/ws")
    async def active_run_ws(
        websocket: WebSocket,
        tab_id: str,
        from_seq: int = Query(default=0, ge=0),
    ) -> None:
        """Attach to an already-running ordinary chat turn."""
        await websocket.accept()
        tab = TabId.of(tab_id)
        broadcaster = container.chat.chat_stream_broadcaster
        if broadcaster.get(tab) is None:
            # "Not found" envelope sent in the accept→first-frame race window
            # (the client may have closed already if it raced a stale tab id).
            await _safe_send_json(
                websocket,
                {
                    "type": "error",
                    "error": {
                        "type": "NotFoundError",
                        "code": "chat.active_run_not_found",
                        "message": f"active chat run {tab_id} not found",
                        "details": {"tab_id": tab_id},
                    },
                },
            )
            try:
                await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            except RuntimeError:
                pass
            return
        try:
            async for replay_frame in broadcaster.replay(tab, from_seq=from_seq):
                # First replay frame can race ``accept()`` (page reload right
                # after the upgrade); :func:`_safe_send_json` returns False on
                # that race so we exit cleanly.
                if not await _safe_send_json(
                    websocket,
                    {
                        "type": "frame",
                        "sequence": replay_frame.sequence,
                        "backfill": replay_frame.backfill,
                        "frame": _frame_projection(replay_frame.frame),
                    },
                ):
                    return
            await _safe_send_json(websocket, {"type": "done"})
            try:
                await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
            except RuntimeError:
                pass
        except WebSocketDisconnect:
            return

    return router


async def _run_one_turn(
    *,
    websocket: WebSocket,
    container: "Container",
    conversation_id: ConversationId,
    tab_id: TabId,
    prompt: str,
    tool_mode: str | None = None,
    tool_params: dict[str, Any] | None = None,
    model_id: str | None = None,
    wechat_sync_user_id: str | None = None,
    feishu_sync_user_id: str | None = None,
    subagent_id: str | None = None,
    allow_question: bool = False,
    allow_child_spawn: bool = False,
    self_allow_spawn: bool = False,
    disabled_tools: list[str] | None = None,
    disabled_skills: list[str] | None = None,
    locale: str | None = None,
) -> None:
    """Drive one ``StreamChatUseCase`` turn over the WebSocket."""
    try:
        extra: dict[str, Any] = {}
        if tool_mode:
            extra["tool_mode"] = tool_mode
        if tool_params:
            extra["tool_params"] = dict(tool_params)
        # UI language for system-prompt framing localization (additive).
        if locale:
            extra["locale"] = locale
        # Sub-agent take-over (SSE parity ``_sse.py:287-297``): when a
        # sub-agent id is present this turn continues that sub-agent's own
        # persisted context using the sub-agent tool set; ``allow_question``
        # only matters on the take-over path (ignored otherwise by the use
        # case — State-Truth-First: no take-over context → no injection).
        if subagent_id:
            extra["subagent_id"] = subagent_id
            if allow_question:
                extra["allow_question"] = True
            # Take-over only: let THIS sub-agent spawn its own sub-agents
            # (default off = the 'agent' tool stays excluded). Independent of
            # allow_child_spawn below. Honoured by the use case only on a live
            # take-over (State-Truth-First).
            if self_allow_spawn:
                extra["self_allow_spawn"] = True
        # Main-agent turn (no take-over): let the FIRST-LEVEL sub-agents this
        # turn spawns create their own sub-agents. Default off keeps the hard
        # recursion guard (SSE parity ``_sse.py``).
        elif allow_child_spawn:
            extra["allow_child_spawn"] = True
        # Per-session tool / SKILL override (SSE parity — see ``_sse.py``).
        # Applied per-turn only; never mutates global config.
        if disabled_tools:
            extra["disabled_tools"] = [str(n) for n in disabled_tools if n]
        if disabled_skills:
            extra["disabled_skills"] = [str(n) for n in disabled_skills if n]
        # R12 dealign: code-persona resolution now happens inside
        # ``StreamChatUseCase`` via the injected cross-BC resolver port
        # (apps bridge); the WS route no longer imports ``qai.user_prefs``.
        #
        # P0-1 image parity (``_sse.py:304-314``): resolve uploaded image
        # refs in the prompt to base64 vision blocks BEFORE the use case, so
        # multimodal turns over WS behave exactly like the SSE path.
        image_refs = _extract_image_refs(prompt)
        if image_refs:
            vision_blocks = _resolve_image_refs_to_vision_blocks(
                container=container,
                image_refs=image_refs,
                prompt_text=prompt,
            )
            if vision_blocks:
                extra["image_content_blocks"] = vision_blocks
        request = StreamChatInput(
            tab_id=tab_id,
            conversation_id=conversation_id,
            user_message=MessageContent(text=prompt, media_refs=image_refs),
            model_hint=model_id if model_id else None,
            extra=extra if extra else None,
        )
        agen = await container.chat.stream_chat_use_case.execute(request)
    except QaiError as exc:
        await websocket.send_json(_error_envelope(exc))
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    # ── Channel sync collection (V1 main.py:6732-6734) ────────────────
    _collect_sync = bool(wechat_sync_user_id or feishu_sync_user_id)
    _assistant_parts: list[str] = []
    _completed_ok = False
    _title_scheduled = False

    broadcast_registered = False
    try:
        broadcast_registered = (
            container.chat.chat_stream_broadcaster.register(
                tab_id=tab_id,
                conversation_id=conversation_id,
                model_id=model_id,
            )
            is not None
        )
        async for item in _iter_frames_with_heartbeat(agen):
            # Idle-window heartbeat: keep the connection alive during a long
            # SILENT tool (no frames produced) so an intermediary idle-timeout
            # does not drop the socket mid-turn. Pure keep-alive — it carries no
            # turn data, is not a StreamFrame, and never advances ``sequence``.
            if item is _WS_HEARTBEAT:
                await websocket.send_json({"type": "ping"})
                continue
            frame = item
            # ── Fire-and-forget first-round auto title (V1 parity) ────────
            # Trigger on the FIRST frame (user message already persisted by
            # then), NOT after the turn ends — so the tab/sidebar title updates
            # right after the user sends, not after a long multi-tool turn. The
            # push self-gates (first round / title_manual / local fallback).
            if not _title_scheduled:
                _title_scheduled = True
                from apps.api._chat_title_push import (
                    schedule_first_round_title,
                )

                schedule_first_round_title(
                    container=container,
                    conversation_id=conversation_id.value,
                    user_text=prompt,
                    model_id=model_id,
                )
            # Collect CHUNK text for channel sync push
            if _collect_sync and frame.frame_type == StreamFrameType.CHUNK:
                text = frame.payload.get("text", "")
                if text:
                    _assistant_parts.append(text)
            if frame.frame_type == StreamFrameType.END:
                _completed_ok = True
            if broadcast_registered:
                container.chat.chat_stream_broadcaster.publish(tab_id, frame)
            await websocket.send_json(_frame_to_envelope(frame))
    except QaiError as exc:
        if broadcast_registered:
            container.chat.chat_stream_broadcaster.mark_terminal(tab_id)
        await websocket.send_json(_error_envelope(exc))
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return
    except WebSocketDisconnect as exc:
        # Log the close code / reason so future mid-turn disconnects are
        # diagnosable at all (1000 clean close, 1001 going-away/tab-close,
        # 1006 abnormal/TCP-drop, 1011 server-error, 4xxx application-defined).
        # Silently swallowing this made it impossible to tell WHY a heavy
        # streaming turn (20+ tool rounds) sometimes ends in ``connection
        # closed`` followed by ``chat.tool_calls_flushed_on_interrupt`` — the
        # server saw the close but did not record who initiated it or with
        # what code, so the next incident can be attributed correctly (client
        # tab suspend vs proxy idle-cut vs true network failure).
        logger.warning(
            "chat.ws.disconnected_mid_turn",
            tab_id=tab_id.value,
            conversation_id=conversation_id.value,
            code=exc.code,
            reason=exc.reason,
        )
        if broadcast_registered:
            container.chat.chat_stream_broadcaster.mark_aborted(
                tab_id,
                reason="ws_disconnect",
            )
            container.chat.chat_stream_broadcaster.mark_terminal(tab_id)
        return

    if broadcast_registered:
        container.chat.chat_stream_broadcaster.mark_terminal(tab_id)
    await websocket.send_json({"type": "done"})
    try:
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
    except RuntimeError:
        # Already closed (race during shutdown) — fine.
        pass

    # ── Fire-and-forget channel sync push (V1 main.py:6754-6815) ──────
    if _completed_ok and _assistant_parts:
        from apps.api._chat_sync_push import schedule_channel_sync_push

        schedule_channel_sync_push(
            container=container,
            conversation_id=conversation_id.value,
            user_text=prompt,
            assistant_text="".join(_assistant_parts),
            wechat_sync_user_id=wechat_sync_user_id,
            feishu_sync_user_id=feishu_sync_user_id,
        )

    # NOTE: first-round auto title is scheduled ON THE FIRST FRAME above
    # (with the correct ``model_id``), NOT here at stream end.  A second
    # END-time call without ``model_id`` used to re-run the title push: it
    # still passed the first-round gate (the round's single user message
    # was already persisted), resolved no endpoint (model_id=None →
    # no base_url), and so OVERWROTE the model-summarised title with the
    # truncation fallback.  The early first-frame call is sufficient and
    # idempotent, so the redundant END-time call is removed.


__all__ = ["build_router"]
