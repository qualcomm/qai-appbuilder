# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Chat SSE streaming route — PR-033 stage B.

Single endpoint:

* ``GET /api/chat/conversations/{conversation_id}/stream`` — open a
  Server-Sent Events stream that runs one chat turn and emits
  ``StreamFrame`` values until completion / abort.

Wire format (locked here per refactor-plan §10.4 + S3-spec §4.4)
---------------------------------------------------------------

::

    event: message
    data: {"frame_id": "...", "frame_type": "chunk", "sequence": 0,
           "payload": {"text": "Hello "}}

    event: message
    data: {"frame_id": "...", "frame_type": "end", "sequence": 2,
           "payload": {"reason": "completed"}}

    event: error
    data: {"type": "ConversationLockedError", "code": "chat.conversation_locked",
           "message": "...", "details": {...}}

    event: done
    data: {}

    : ping

Rules:

* Every ``StreamFrame`` becomes one ``message`` event whose data is
  the JSON-serialised frame (``frame_id``, ``frame_type``, ``sequence``,
  ``payload``). Adapters MUST NOT introduce new ``event:`` types.
* The final frame always has ``frame_type=end``; after it we emit
  one ``done`` event and close the stream.
* On a :class:`QaiError` (typically :class:`ConversationLockedError`
  or :class:`TabNotFoundError`), one ``error`` event carries the
  unified ``QaiError.to_dict()`` envelope and we close the stream.
* Heartbeat ``: ping`` is emitted every 15 s of idle silence so
  proxies don't kill the connection. Heartbeats are inserted by the
  SSE layer here, NOT by the use case (per S3 spec §4.4 line 211).

This module is the ONE place ``StreamFrame`` becomes wire bytes.
PR-035 ai_coding will copy the helpers below or — if a second SSE
PR confirms reuse — the helpers can be promoted to
``interfaces/http/sse.py`` (recorded as a coordination request in the
manifest).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from qai.chat.application.use_cases.streaming import StreamChatInput
from qai.chat.application.use_cases.orchestrate_discussion import (
    OrchestrateDiscussionInput,
)
from qai.chat.application.use_cases.tab_management import OpenTabInput
from qai.chat.application.use_cases._image_refs import (
    extract_image_refs as _shared_extract_image_refs,
    resolve_image_refs_to_vision_blocks as _shared_resolve_image_refs_to_vision_blocks,
)
from qai.chat.domain.content import MessageContent
from qai.chat.domain.conversation import Conversation
from qai.chat.domain.errors import (
    ConversationLockedError,
    TabNotFoundError,
)
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType
from qai.platform.errors import QaiError
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


logger = get_logger(__name__)


_HEARTBEAT_INTERVAL_SECONDS: float = 15.0
"""How often to emit ``: ping\\n\\n`` if the use case yields nothing."""


def _parse_name_array(raw: str | None) -> list[str]:
    """Parse a JSON-encoded array of names (tool names / skill ids).

    Used for the additive ``disabled_tools`` / ``disabled_skills`` query
    params (per-session tool / SKILL override). Tolerant: a missing /
    malformed / non-array / non-string-element value degrades to ``[]`` (no
    override) so a bad client value never breaks the stream — matching the
    defensive ``tool_params`` JSON parse below.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str) and item]


def _extract_image_refs(prompt_text: str) -> tuple[str, ...]:
    """Extract ``/api/images/files/...`` URLs from markdown image syntax.

    Thin delegation to the shared chat-application helper
    (``qai.chat.application.use_cases._image_refs``) so the user-prompt image
    path here and the agentic loop's ``question``-answer image path decode with
    ONE口径. V1 parity (useChat.js:2067-2077): the WebUI prepends uploaded
    images as ``![name](/api/images/files/xxx)`` markdown.
    """
    return _shared_extract_image_refs(prompt_text)


def _resolve_image_refs_to_vision_blocks(
    *,
    container: "Container",
    image_refs: tuple[str, ...],
    prompt_text: str,
) -> list[dict[str, Any]]:
    """Resolve image URLs to OpenAI-Vision content blocks (route layer).

    Delegates to the shared chat-application helper, passing the chat
    container's ``image_upload_store`` adapter as the path resolver. The
    interfaces layer keeps adapter access; the decode logic itself lives in
    the application layer so the agentic loop can reuse it without importing
    ``interfaces``.
    """
    store = getattr(container.chat, "image_upload_store", None)
    return _shared_resolve_image_refs_to_vision_blocks(
        store=store,
        image_refs=image_refs,
        source_text=prompt_text,
    )


# ---- Wire-format helpers (frame format spec lives here) ------------------


def format_sse_message(frame: StreamFrame) -> bytes:
    """Encode a :class:`StreamFrame` as one SSE ``message`` event."""
    payload: dict[str, Any] = {
        "frame_id": frame.frame_id,
        "frame_type": frame.frame_type.value,
        "sequence": frame.sequence,
        "payload": dict(frame.payload),
    }
    return _sse_event("message", payload)


def format_sse_error(error: QaiError) -> bytes:
    """Encode a :class:`QaiError` as one SSE ``error`` event."""
    return _sse_event("error", error.to_dict())


def format_sse_done() -> bytes:
    """Encode the terminal ``done`` event."""
    return _sse_event("done", {})


def format_sse_heartbeat() -> bytes:
    """SSE comment-line heartbeat (proxies count it as data)."""
    return b": ping\n\n"


def _sse_event(name: str, data: Any) -> bytes:
    """Render one SSE event with a ``name`` and JSON ``data`` field."""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {body}\n\n".encode("utf-8")


# ---- Tab provisioning (delegated to OpenTabUseCase) ----------------------


async def _resolve_or_create_tab(
    *, container: "Container", tab_id: TabId, conversation: Conversation
) -> ConversationTab:
    """Find a tab by id or open one bound to the given conversation.

    R11 dealign fix: the route no longer constructs a domain
    :class:`ConversationTab` aggregate itself (the old find→``open``→save
    inline path violated the "interfaces stay thin" boundary — tab
    lifecycle is application-layer concern).  It now delegates to the
    application :class:`~qai.chat.application.use_cases.tab_management.OpenTabUseCase`,
    which is idempotent on a known ``tab_id`` for the same conversation
    and mints a fresh record otherwise.  The use case validates the
    conversation exists (already loaded by the caller) and persists the
    tab through :class:`TabSessionStorePort`, returning the aggregate so
    the SSE / WS routes can echo the id back to the front-end.
    """
    return await container.chat.open_tab_use_case.execute(
        OpenTabInput(
            conversation_id=conversation.id.value,
            tab_id=tab_id.value,
        ),
    )


# ---- Stream body builder --------------------------------------------------

async def _stream_chat_sse(
    *,
    container: "Container",
    conversation_id: str,
    tab_id_raw: str,
    prompt_text: str,
    tool_mode: str | None = None,
    tool_params: dict[str, Any] | None = None,
    model_id: str | None = None,
    locale: str | None = None,
    wechat_sync_user_id: str | None = None,
    feishu_sync_user_id: str | None = None,
    subagent_id: str | None = None,
    allow_question: bool = False,
    allow_child_spawn: bool = False,
    self_allow_spawn: bool = False,
    disabled_tools: list[str] | None = None,
    disabled_skills: list[str] | None = None,
) -> AsyncIterator[bytes]:
    """Drive ``StreamChatUseCase`` and yield SSE-encoded bytes.

    Lifecycle:
        1. Resolve / provision the tab.
        2. Build the use case input.
        3. ``async for`` the frames; for each frame, race against a
           heartbeat timer so an idle stream still emits ``: ping``
           every ~15 s.
        4. On natural completion emit one ``done`` event and stop.
        5. On :class:`QaiError`, emit one ``error`` event and stop.

    ``tool_mode`` and ``tool_params`` are forwarded to
    :class:`StreamChatInput.extra` so the system-prompt builder can pick
    a feature-specific prompt and the LLM port receives sampling
    overrides. Both are optional — when ``None`` the default behaviour
    matches the pre-T2.6-A code path exactly.

    ``wechat_sync_user_id`` / ``feishu_sync_user_id`` — when provided,
    after the stream ends with a successful ``END`` frame the route
    fire-and-forgets a push of "[WebUI 提问]/[AI 回复]" to the bound
    channel user (V1 parity: main.py:6754-6815).
    """
    try:
        conv = await container.chat.conversations.get(
            ConversationId.of(conversation_id),
        )
        tab = await _resolve_or_create_tab(
            container=container,
            tab_id=TabId.of(tab_id_raw),
            conversation=conv,
        )

        extra: dict[str, Any] = {}
        if tool_mode is not None and tool_mode != "":
            extra["tool_mode"] = tool_mode
        if tool_params is not None and len(tool_params) > 0:
            extra["tool_params"] = dict(tool_params)
        # UI language for system-prompt framing localization (additive).
        if locale is not None and locale != "":
            extra["locale"] = locale
        # User take-over of a sub-agent: the use case loads this sub-agent's
        # persisted context + restricts to the sub-agent tool set when present.
        if subagent_id is not None and subagent_id != "":
            extra["subagent_id"] = subagent_id
            # Take-over only: whether the 'question' tool is advertised to the
            # taken-over sub-agent (default False = excluded, matching the
            # autonomous sub-agent set). The use case honours this ONLY when the
            # take-over actually loads (State-Truth-First: no take-over context
            # → no question injection regardless of this flag).
            if allow_question:
                extra["allow_question"] = True
            # Take-over only: whether THIS sub-agent may itself spawn sub-agents
            # (default False = the 'agent' tool stays excluded, autonomous
            # parity). Honoured by the use case ONLY on a live take-over —
            # State-Truth-First (no take-over context → no spawn-tool injection
            # regardless of this flag). Independent of allow_child_spawn below.
            if self_allow_spawn:
                extra["self_allow_spawn"] = True
        # Main-agent turn (no take-over): whether the FIRST-LEVEL sub-agents
        # this turn spawns are themselves allowed to create sub-agents. Default
        # False keeps the hard recursion guard. Forwarded to the sub-agent
        # dispatch via the use case; ignored on a take-over turn (where the
        # main agent does not spawn — it IS a sub-agent).
        elif allow_child_spawn:
            extra["allow_child_spawn"] = True

        # Per-session ("this conversation only") tool / SKILL override (V2
        # enhancement; additive). Names the user switched OFF for THIS session
        # — the use case drops them from the per-turn advertised tool schemas
        # (``_collect_tool_schemas``) and the per-turn skill catalog
        # (``_build_available_skills_xml`` + cloud system-prompt skill list).
        # Applied per-turn only; never mutates global tool-safety / forge.config.
        # Empty / absent ⇒ byte-for-byte the pre-feature behaviour.
        if disabled_tools:
            extra["disabled_tools"] = [str(n) for n in disabled_tools if n]
        if disabled_skills:
            extra["disabled_skills"] = [str(n) for n in disabled_skills if n]

        # R12 dealign: code-persona resolution (id → prompt + name) now
        # happens inside ``StreamChatUseCase`` via the injected cross-BC
        # ``CodePersonaResolverPort`` (apps bridge), so the route no
        # longer imports ``qai.user_prefs`` or resolves the persona here.

        # P0-1: resolve image URLs to base64 vision blocks BEFORE
        # entering the use case (interfaces layer has adapter access).
        image_refs = _extract_image_refs(prompt_text)
        if image_refs:
            vision_blocks = _resolve_image_refs_to_vision_blocks(
                container=container,
                image_refs=image_refs,
                prompt_text=prompt_text,
            )
            if vision_blocks:
                extra["image_content_blocks"] = vision_blocks

        request = StreamChatInput(
            tab_id=tab.id,
            conversation_id=conv.id,
            user_message=MessageContent(
                text=prompt_text,
                media_refs=image_refs,
            ),
            model_hint=model_id if model_id else None,
            extra=extra if extra else None,
        )
        agen = await container.chat.stream_chat_use_case.execute(request)
    except QaiError as exc:
        yield format_sse_error(exc)
        return

    broadcast_registered = (
        container.chat.chat_stream_broadcaster.register(
            tab_id=tab.id,
            conversation_id=conv.id,
            title=conv.title,
            model_id=model_id if model_id else None,
        )
        is not None
    )

    # ── Channel sync collection (V1 main.py:6732-6734) ────────────────
    _collect_sync = bool(wechat_sync_user_id or feishu_sync_user_id)
    _assistant_parts: list[str] = []
    _completed_ok = False
    _title_scheduled = False

    try:
        async for frame in _with_heartbeat(agen):
            if frame is None:
                yield format_sse_heartbeat()
                continue
            # ── Fire-and-forget first-round auto title (V1 parity) ────────
            # Trigger as EARLY as possible — on the first real frame — NOT at
            # the end of the (possibly minutes-long, multi-tool) agentic turn.
            # The title only depends on the first user message, which the use
            # case has already persisted by the time it yields its first frame
            # (``_prepare_turn`` appends + saves the user message before opening
            # the LLM). Waiting for END left the tab showing "新对话" for the
            # whole turn. The push self-gates (first round only / title_manual /
            # local-model fallback), so an early fire is safe and idempotent.
            if not _title_scheduled:
                _title_scheduled = True
                from apps.api._chat_title_push import (
                    schedule_first_round_title,
                )

                schedule_first_round_title(
                    container=container,
                    conversation_id=conversation_id,
                    user_text=prompt_text,
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
                container.chat.chat_stream_broadcaster.publish(tab.id, frame)
            yield format_sse_message(frame)
    except asyncio.CancelledError:
        if broadcast_registered:
            container.chat.chat_stream_broadcaster.mark_aborted(
                tab.id,
                reason="sse_disconnect",
            )
            container.chat.chat_stream_broadcaster.mark_terminal(tab.id)
        raise
    except QaiError as exc:
        if broadcast_registered:
            container.chat.chat_stream_broadcaster.mark_terminal(tab.id)
        yield format_sse_error(exc)
        return

    if broadcast_registered:
        container.chat.chat_stream_broadcaster.mark_terminal(tab.id)
    yield format_sse_done()

    # ── Fire-and-forget channel sync push (V1 main.py:6754-6815) ──────
    if _completed_ok and _assistant_parts:
        from apps.api._chat_sync_push import schedule_channel_sync_push

        schedule_channel_sync_push(
            container=container,
            conversation_id=conversation_id,
            user_text=prompt_text,
            assistant_text="".join(_assistant_parts),
            wechat_sync_user_id=wechat_sync_user_id,
            feishu_sync_user_id=feishu_sync_user_id,
        )


async def _stream_discussion_sse(
    *,
    container: "Container",
    conversation_id: str,
    tab_id_raw: str,
    prompt_text: str,
    pinned_speaker: str | None = None,
    model_id: str | None = None,
    locale: str | None = None,
) -> AsyncIterator[bytes]:
    """Drive ``OrchestrateDiscussionUseCase`` and yield SSE-encoded bytes.

    The multi-agent discussion path (block 4). Structurally identical to
    :func:`_stream_chat_sse` (resolve/provision the tab → build the input →
    drive the frame stream with the same heartbeat cadence + ``done`` / ``error``
    framing), but dispatches to the OUTER speaker-selection orchestrator instead
    of the single-agent ``StreamChatUseCase``. The orchestrator yields the SAME
    :class:`StreamFrame` type (every frame stamped with the speaking
    participant's ``sender_id`` + ``speaker_changed`` boundaries), so the wire
    format is unchanged — the front-end attributes each frame to its speaker.

    Channel-sync push / image-vision resolution are single-agent concerns and
    are intentionally NOT replicated here (a discussion is a multi-speaker web
    session, not a 1:1 channel relay).
    """
    try:
        conv = await container.chat.conversations.get(
            ConversationId.of(conversation_id),
        )
        tab = await _resolve_or_create_tab(
            container=container,
            tab_id=TabId.of(tab_id_raw),
            conversation=conv,
        )
        request = OrchestrateDiscussionInput(
            conversation_id=conv.id,
            tab_id=tab.id,
            user_message=MessageContent(text=prompt_text),
            pinned_speaker=pinned_speaker or None,
            # A discussion is fully self-contained: every system-level LLM call
            # (intent / planner / validator / manager / social) resolves its
            # model from the mode template + roster, NEVER from the externally
            # supplied tab model_id. Forcing this None severs the old
            # query::mb_pro leak into discussions.
            default_model_id=None,
            # UI language (en / zh-CN / zh-TW) for built-in template i18n
            # (migration 056): drives the runtime persona / mode-framing override
            # so a built-in role/mode's text follows the user's chosen language.
            # None / unknown → the orchestrator normalises to zh-CN (the product
            # default) → Simplified text, byte-for-byte the pre-056 behaviour.
            locale=locale or None,
        )
        agen = container.chat.orchestrate_discussion.execute(request)
    except QaiError as exc:
        yield format_sse_error(exc)
        return

    broadcast_registered = (
        container.chat.chat_stream_broadcaster.register(
            tab_id=tab.id,
            conversation_id=conv.id,
            title=conv.title,
            model_id=model_id if model_id else None,
        )
        is not None
    )

    # ── Auto-title state (V1 parity with single-agent SSE path above) ─────
    # Discussion conversations share the same conversation_id key the title
    # use case + push bridge use, so the SAME fire-and-forget machinery the
    # single-agent path uses also names the discussion tab on its first
    # real frame. ``schedule_first_round_title`` self-gates (first-round +
    # title_manual + local-model fallback) so an early fire is safe and
    # idempotent — no risk of duplicate titles when a discussion has many
    # speaker turns.
    _title_scheduled = False
    try:
        async for frame in _with_heartbeat(agen):
            if frame is None:
                yield format_sse_heartbeat()
                continue
            if not _title_scheduled:
                _title_scheduled = True
                from apps.api._chat_title_push import (
                    schedule_first_round_title,
                )

                schedule_first_round_title(
                    container=container,
                    conversation_id=conversation_id,
                    user_text=prompt_text,
                    model_id=model_id,
                )
            if broadcast_registered:
                container.chat.chat_stream_broadcaster.publish(tab.id, frame)
            yield format_sse_message(frame)
    except asyncio.CancelledError:
        if broadcast_registered:
            container.chat.chat_stream_broadcaster.mark_aborted(
                tab.id,
                reason="sse_disconnect",
            )
            container.chat.chat_stream_broadcaster.mark_terminal(tab.id)
        raise
    except QaiError as exc:
        if broadcast_registered:
            container.chat.chat_stream_broadcaster.mark_terminal(tab.id)
        yield format_sse_error(exc)
        return

    if broadcast_registered:
        container.chat.chat_stream_broadcaster.mark_terminal(tab.id)
    yield format_sse_done()


async def _with_heartbeat(
    source: AsyncIterator[StreamFrame],
) -> AsyncIterator[StreamFrame | None]:
    """Yield frames from ``source`` plus periodic ``None`` markers.

    ``None`` is the "send a heartbeat" sentinel; the SSE layer
    translates it via :func:`format_sse_heartbeat`.

    V1 parity (``backend/main.py:6675-6743``): a single long-lived producer
    task drains ``source`` into a queue; the consumer waits on the queue
    with a timeout, emitting a heartbeat on each idle window. The producer
    is **never cancelled mid-stream** — only after it has finished
    naturally (or when the consumer generator itself is closed). The
    previous implementation wrapped each ``__anext__`` in a fresh task and
    cancelled it in a ``finally`` every iteration; for a ``source`` that
    holds an ``async with httpx.stream(...)`` open (local GenieAPIService /
    cloud SSE) that cancellation tore down the upstream HTTP connection
    after the first frame, truncating the model's reply ("Client connection
    has been broken"). Decoupling via a queue keeps the upstream stream
    alive for its full lifetime while still honouring the heartbeat cadence.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def _producer() -> None:
        try:
            async for frame in source:
                await queue.put(frame)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except BaseException as exc:  # noqa: BLE001 - propagate to consumer
            await queue.put(_ProducerError(exc))
        finally:
            await queue.put(_STOP)

    producer_task = asyncio.create_task(_producer())
    try:
        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=_HEARTBEAT_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                # Idle window — emit a heartbeat and keep waiting. The
                # producer is untouched (still draining the upstream stream).
                yield None
                continue
            if item is _STOP:
                return
            if isinstance(item, _ProducerError):
                raise item.exc
            yield item  # type: ignore[misc]
    finally:
        if not producer_task.done():
            producer_task.cancel()
            try:
                await producer_task
            except asyncio.CancelledError:
                # ``producer_task`` was cancelled by us above; its
                # CancelledError is expected and benign.
                pass
            except Exception:  # noqa: BLE001 - best-effort drain
                logger.warning(
                    "chat.sse.producer_task_cleanup_failed",
                    exc_info=True,
                )


class _ProducerError:
    """Carries an exception raised inside the producer to the consumer."""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_STOP: Any = object()


async def _anext_safe(aiter_: AsyncIterator[StreamFrame]) -> Any:
    """``__anext__`` returning :data:`_STOP` instead of raising
    :class:`StopAsyncIteration` (so :func:`asyncio.wait` can resolve)."""
    try:
        return await aiter_.__anext__()
    except StopAsyncIteration:
        return _STOP


# ---- Router factory -------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the chat SSE router."""
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    @router.get(
        "/conversations/{conversation_id}/stream",
        responses={
            200: {
                "content": {"text/event-stream": {}},
                "description": "SSE stream of StreamFrame events.",
            }
        },
    )
    async def stream_chat(
        conversation_id: str,
        tab_id: str = Query(..., min_length=1, max_length=64),
        prompt: str = Query(..., min_length=1, max_length=1_000_000),
        # T2.6-A — additive optional sampling + tool-mode params.
        # Refactor-plan §3.1 explicitly allows new query params; the
        # legacy 2-param call still works because each new param has
        # a default. The system-prompt builder + LLM port consume
        # these via ``StreamChatInput.extra``.
        tool_mode: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            description=(
                "Optional toolbar mode (model-build / app-builder / "
                "code / translate / ppt) — drives the system-prompt "
                "branch."
            ),
        ),
        temperature: float | None = Query(
            default=None, ge=0.0, le=2.0,
            description="Sampling temperature override (0..2).",
        ),
        top_p: float | None = Query(
            default=None, ge=0.0, le=1.0,
            description="Sampling top-p override (0..1).",
        ),
        max_tokens: int | None = Query(
            default=None, ge=0, le=1_000_000,
            description="Sampling max-tokens override (0 = no limit).",
        ),
        tool_params: str | None = Query(
            default=None,
            max_length=8192,
            description=(
                "Optional JSON-encoded feature-mode parameters "
                "(code: {speed,persona?} / translate: {target_lang} / "
                "ppt: {length}).  Merged with the sampling overrides "
                "below.  Additive query param (refactor-plan §3.1)."
            ),
        ),
        model_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=256,
            description=(
                "Optional selected model id (V1 ``selectedModelId`` parity). "
                "Forwarded to the provider-routing LLM stream as the "
                "``model_hint`` so the turn is routed to the owning cloud / "
                "local provider.  Additive query param (refactor-plan §3.1)."
            ),
        ),
        locale: str | None = Query(
            default=None,
            max_length=16,
            description=(
                "Optional UI language (en / zh-CN / zh-TW) so the system-prompt "
                "builder localizes its feature-mode framing to the user's "
                "language.  Additive query param (refactor-plan §3.1)."
            ),
        ),
        wechat_sync_user_id: str | None = Query(
            default=None,
            max_length=256,
            description=(
                "Optional WeChat user id for dual-端 sync push. "
                "When provided, the AI reply is pushed to this WeChat "
                "user after stream completion (V1 main.py:6649 parity). "
                "Additive query param (refactor-plan §3.1)."
            ),
        ),
        feishu_sync_user_id: str | None = Query(
            default=None,
            max_length=256,
            description=(
                "Optional Feishu user id for dual-端 sync push. "
                "When provided, the AI reply is pushed to this Feishu "
                "user after stream completion (V1 main.py:6653 parity). "
                "Additive query param (refactor-plan §3.1)."
            ),
        ),
        subagent_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            description=(
                "Optional sub-agent session id. When present, this turn is a "
                "USER TAKE-OVER of that sub-agent: the turn continues the "
                "sub-agent's own persisted context (its prior messages + tool "
                "outputs) instead of the parent conversation, using the "
                "sub-agent tool set (no nested 'agent'). Additive query param "
                "(refactor-plan §3.1)."
            ),
        ),
        allow_question: bool = Query(
            default=False,
            description=(
                "Sub-agent take-over only: when True, the 'question' tool is "
                "advertised to the taken-over sub-agent so it can ask the user "
                "a blocking question (its dialog is reachable because the user "
                "has the sub-agent tab open and is conversing with it). Default "
                "False keeps 'question' excluded (matches the autonomous "
                "sub-agent tool set). Ignored on non-take-over turns. Additive "
                "query param (refactor-plan §3.1)."
            ),
        ),
        allow_child_spawn: bool = Query(
            default=False,
            description=(
                "Main-agent turn only: when True, the first-level sub-agents "
                "this turn spawns are granted the 'agent' (spawn) tool so they "
                "may create their own (second-level / grand) sub-agents. "
                "Controls ONLY the main agent's direct children, not deeper "
                "levels. Default False keeps the historical hard recursion "
                "guard (sub-agents cannot spawn). Ignored on a sub-agent "
                "take-over turn. Additive query param (refactor-plan §3.1)."
            ),
        ),
        self_allow_spawn: bool = Query(
            default=False,
            description=(
                "Sub-agent take-over only: when True, the 'agent' (spawn) tool "
                "is advertised to the taken-over sub-agent so it may create its "
                "own sub-agents. Independent of 'allow_child_spawn'. Default "
                "False keeps the autonomous sub-agent parity (no spawn). "
                "Ignored on non-take-over turns. Additive query param "
                "(refactor-plan §3.1)."
            ),
        ),
        discussion: bool = Query(
            default=False,
            description=(
                "Multi-agent discussion opt-in (block 4). When True (or the "
                "conversation's persisted ``meta.discussion.is_discussion`` is "
                "set) this turn is routed to the OUTER speaker-selection "
                "orchestrator instead of the single-agent stream — every frame "
                "is stamped with the speaking participant's ``sender_id``. "
                "Default False routes to the unchanged single-agent path. "
                "Additive query param (refactor-plan §3.1)."
            ),
        ),
        pinned_speaker: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            description=(
                "Optional discussion pin: let this participant id speak first "
                "(one turn). Only honoured on the discussion path. Additive "
                "query param (refactor-plan §3.1)."
            ),
        ),
        disabled_tools: str | None = Query(
            default=None,
            max_length=4096,
            description=(
                "Per-session ('this conversation only') DISABLED tool names — a "
                "JSON-encoded array (e.g. ['exec','agent']). The use case drops "
                "these from the per-turn advertised tool schemas. Applied "
                "per-turn only; never mutates the global tool-safety config. "
                "Omitted/empty ⇒ the full default tool set. Additive query "
                "param (refactor-plan §3.1)."
            ),
        ),
        disabled_skills: str | None = Query(
            default=None,
            max_length=4096,
            description=(
                "Per-session ('this conversation only') DISABLED skill ids — a "
                "JSON-encoded array. The use case drops these from the per-turn "
                "skill catalog (local <available_skills> XML + cloud "
                "system-prompt skill list). Applied per-turn only; never "
                "mutates the per-skill forge.config mode. Additive query param "
                "(refactor-plan §3.1)."
            ),
        ),
    ) -> StreamingResponse:
        """Open an SSE stream that runs one chat turn.

        Frame contract is documented in this module's docstring; the
        client is expected to read until it sees a ``done`` event or
        a single ``error`` event.
        """
        # ── Discussion-mode routing (block 4) ──────────────────────────────
        # Route to the multi-agent orchestrator when the client opts in via
        # ``?discussion=1`` OR the conversation is persisted as a discussion
        # (``meta.discussion.is_discussion``). The persisted-flag probe is a
        # cheap read on the conversation aggregate (State-Truth-First: the
        # source of truth is the stored config, not a client assertion); any
        # lookup failure degrades to the single-agent path (a missing
        # conversation surfaces its proper error inside that path). The
        # NON-discussion path below is byte-for-byte unchanged (judgement 2).
        route_discussion = bool(discussion)
        if not route_discussion:
            try:
                _conv = await container.chat.conversations.find(
                    ConversationId.of(conversation_id),
                )
                _cfg = _conv.discussion if _conv is not None else None
                route_discussion = bool(_cfg and _cfg.get("is_discussion"))
            except Exception:  # noqa: BLE001 — never break the stream on a probe
                route_discussion = False

        if route_discussion:
            disc_body = _stream_discussion_sse(
                container=container,
                conversation_id=conversation_id,
                tab_id_raw=tab_id,
                prompt_text=prompt,
                pinned_speaker=pinned_speaker or None,
                model_id=model_id,
                locale=locale,
            )
            return StreamingResponse(
                disc_body,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        merged_params: dict[str, Any] = {}
        # Feature-mode params arrive JSON-encoded; parse defensively so a
        # malformed value never breaks the stream (just ignored).
        if tool_params:
            try:
                parsed = json.loads(tool_params)
                if isinstance(parsed, dict):
                    merged_params.update(parsed)
            except (ValueError, TypeError):
                pass
        if temperature is not None:
            merged_params["temperature"] = float(temperature)
        if top_p is not None:
            merged_params["top_p"] = float(top_p)
        if max_tokens is not None and max_tokens > 0:
            merged_params["max_tokens"] = int(max_tokens)

        # Per-session tool / SKILL override arrives JSON-encoded; parse
        # defensively (a malformed value is simply ignored → no override).
        disabled_tools_list = _parse_name_array(disabled_tools)
        disabled_skills_list = _parse_name_array(disabled_skills)

        body = _stream_chat_sse(
            container=container,
            conversation_id=conversation_id,
            tab_id_raw=tab_id,
            prompt_text=prompt,
            tool_mode=tool_mode,
            tool_params=merged_params if merged_params else None,
            model_id=model_id,
            locale=locale,
            wechat_sync_user_id=wechat_sync_user_id or None,
            feishu_sync_user_id=feishu_sync_user_id or None,
            subagent_id=subagent_id or None,
            allow_question=allow_question,
            allow_child_spawn=allow_child_spawn,
            self_allow_spawn=self_allow_spawn,
            disabled_tools=disabled_tools_list or None,
            disabled_skills=disabled_skills_list or None,
        )
        return StreamingResponse(
            body,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router


__all__ = [
    "build_router",
    "format_sse_message",
    "format_sse_error",
    "format_sse_done",
    "format_sse_heartbeat",
]
