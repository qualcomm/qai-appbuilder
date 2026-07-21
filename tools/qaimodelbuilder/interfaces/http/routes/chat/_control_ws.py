# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Chat control-plane WebSocket — ``WS /api/chat/control``.

Architecture rationale (AGENTS.md 判据 1 / §🔴 State-Truth-First)
----------------------------------------------------------------
Chat surface has two distinct planes of traffic flowing between the browser
and the server:

* **Data plane** — server→client token / tool_call / tool_result / done /
  error frames (long-lived, drive the UI's streaming view). Carried on
  :mod:`._sse` (default) or :mod:`._ws` (opt-in). Both physical channels
  hold a connection open for the lifetime of one chat turn.

* **Control plane** — client→server control signals scoped to an
  in-flight turn: today ``answer`` (resolve a pending blocking
  ``question`` tool call) and ``stop`` (cooperative abort). Historically
  these went out as plain ``POST /api/chat/answer`` / ``POST /api/chat/stop``
  REST calls.

Mixing both planes onto the same HTTP/1.1 socket pool produces a real,
reproducible foot-gun: when several tabs each hold a long SSE stream
open, the browser's per-origin ``HTTP/1.1`` connection limit (6 in all
mainstream engines, hard-coded) is saturated by data-plane streams, and a
subsequent control-plane ``POST /api/chat/answer`` is *queued client-side*
until one of those streams releases its slot — producing minute-scale
"my answer never arrives" hangs (the original symptom that motivated this
endpoint: a 438-second wait between dialog-close and the suspended
``question`` tool resuming).

This module introduces a **dedicated control-plane channel** that is:

1. **Physically independent of the data plane** — a WebSocket has its own
   socket and is *not* subject to the per-origin HTTP/1.1 6-connection
   pool, so it cannot be queued behind any SSE / fetch traffic.
2. **Multiplexed across tabs over a single connection** — the browser
   opens ONE ``/api/chat/control`` WebSocket per page and routes
   per-tab control frames over it (tab id is carried inside each frame).
   This keeps the global WebSocket connection budget tiny while still
   giving every tab an instant, unqueued control path.
3. **Scoped to control only** — it never carries token frames. Data
   plane (SSE / WS) is untouched by this work.

The REST endpoints ``POST /api/chat/answer`` and ``POST /api/chat/stop``
remain available (AGENTS.md §3.1 — locked routes / paths cannot be
deleted) and serve as a graceful fallback when the control WebSocket is
not connected (initial page load race, network blip, automated /
TestClient flows).

Wire format
-----------
On upgrade the client supplies no query params; the WebSocket is owned by
the *browser tab / page*, not by any single chat tab. The server sends a
handshake envelope then enters a receive loop.

Server → client (handshake)::

    {"type": "hello", "protocol": "qai.chat.control/1"}

Client → server::

    {"type": "answer", "tab_id": "<tab-id>", "answer": "<text>"}
    {"type": "stop",   "tab_id": "<tab-id>", "reason": "<optional>"}
    {"type": "retry_now", "tab_id": "<tab-id>"}   # cut short a network-retry
                                                  # backoff; non-terminal

Server → client (per-command ack — fire-and-forget on the client; useful
for debugging / future UX hooks)::

    {"type": "ack",   "id": "<echoed client_msg_id or null>",
                      "kind": "answer"|"stop",
                      "tab_id": "<tab-id>",
                      "ok": true|false,
                      "result": {...}}      # ``delivered`` / ``aborted`` / etc.
    {"type": "error", "id": "<echoed client_msg_id or null>",
                      "error": "<short message>"}

Idempotency, ordering, and error handling mirror the REST endpoints
exactly:

* answering a tab whose ``question`` already timed out / was cancelled →
  ``ok=true, result={"delivered": false}`` (benign no-op);
* stopping a tab with no in-flight stream → ``ok=true,
  result={"aborted": false}``;
* a malformed frame → ``ok=false`` + ``error`` envelope; the connection
  *does not* close (a single bad frame from one tab must not take down
  the whole page's control channel).

Origin / CSRF
-------------
This route follows the project-wide WebSocket auth posture documented in
``interfaces/http/middleware/csrf.py``: the global ``CsrfMiddleware``
intentionally does NOT cover WebSocket handshakes (HTTP-method semantics
do not match WS upgrades) and the starlette/FastAPI ``CORSMiddleware``
does not act on WS upgrades either, so neither the CSRF double-submit
cookie nor the CORS allow-list provides protection here. The existing
``WS /api/chat/ws`` data-plane endpoint follows the same posture (no
in-handler Origin check), and this control endpoint is deliberately
consistent with it — it is a strictly equivalent alternative channel for
the same locked REST routes (``POST /api/chat/answer`` / ``POST
/api/chat/stop``) that already run un-CSRF'd on the same server, and the
deployment is the same loopback-only ``127.0.0.1`` listener.

When the project later decides to gate WebSocket upgrades on an Origin
allow-list (a single follow-up that should apply uniformly across every
WS route on this app), the natural seam is a small shared helper invoked
at the top of each ``@router.websocket`` handler — this module will pick
it up by adding one call right after ``websocket.accept()``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from qai.chat.application.use_cases.streaming import StopChatInput
from qai.chat.application.use_cases.streaming import CancelToolInput
from qai.chat.domain.ids import TabId
from qai.platform.errors import QaiError
from qai.platform.logging import get_logger

from ._ws_utils import safe_send_json as _safe_send_json

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


logger = get_logger(__name__)


# Frame ``type`` values accepted on the client→server direction.
_CMD_ANSWER = "answer"
_CMD_STOP = "stop"
# Mid-turn user injection (V2 enhancement): the "inject" button records a
# new ``role:user`` instruction to fold into the SAME in-flight run at its
# inter-round seam. Unlike ``stop`` / ``answer`` it has NO REST equivalent
# (it only makes sense for a live, in-process stream).
_CMD_INJECT = "inject"
# Withdraw a not-yet-folded injection (the user edited/cancelled its pending
# bubble before the run loop drained it) so the run does NOT also fold it in.
_CMD_INJECT_CANCEL = "inject_cancel"
# "立即重试" (V2 enhancement): the user manually restored connectivity while a
# turn is waiting out a network-retry backoff and wants it to re-open NOW
# instead of waiting the escalating delay. Like ``inject`` it has NO REST
# equivalent (it only makes sense for a live, in-process stream mid-backoff).
# NON-terminal — it does not abort the turn, it just cuts the current wait
# short so the retry loop re-opens the LLM stream immediately.
_CMD_RETRY_NOW = "retry_now"
# Per-call single-tool cancel (V2 enhancement): the user clicked the stop
# button on ONE running tool card. NON-terminal — it does NOT abort the turn;
# it cancels just that one tool (by ``call_id``), the backend synthesizes a
# ``[cancelled]`` tool_result for it, and the turn continues with the other
# tools' results. Has a REST equivalent (``/api/chat/cancel_tool``) for the
# WS-down fallback.
_CMD_CANCEL_TOOL = "cancel_tool"

# Validation limits — kept in lock-step with the equivalent Pydantic
# ``Field`` constraints on the REST endpoints
# (``StopChatRequest`` / ``AnswerQuestionRequest`` in ``_rest.py``) so the
# WS control plane and the REST fallback accept exactly the same inputs.
# Drifting these from REST would make "WS down -> REST fallback" subtly
# change validity, which we explicitly do NOT want.
_MAX_TAB_ID_LENGTH = 64       # mirrors StopChatRequest.tab_id / AnswerQuestionRequest.tab_id
_MAX_ANSWER_LENGTH = 100_000  # mirrors AnswerQuestionRequest.answer
_MAX_REASON_LENGTH = 64       # mirrors StopChatRequest.reason
_MAX_CALL_ID_LENGTH = 128     # mirrors CancelToolRequest.call_id (tool_call ids)
# Mid-turn injection text ceiling: same order as an answer (a free-form user
# instruction), capped to keep a single control frame bounded.
_MAX_INJECT_LENGTH = 100_000


def build_router(*, container: "Container") -> APIRouter:
    """Build the chat control-plane WS router."""
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    @router.websocket("/control")
    async def chat_control_ws(websocket: WebSocket) -> None:
        """Dedicated control-plane WebSocket for the whole page.

        One per browser tab/page. Multiplexes ``answer`` and ``stop``
        control frames for every chat tab the page owns. Does NOT carry
        streaming data frames (those continue to flow on SSE / the
        per-turn data WS).
        """
        await websocket.accept()
        # P13 — accept→first-frame race guard via the shared helper
        # (``_ws_utils.safe_send_json``). The client can disconnect in
        # the tiny window between ``accept()`` and this first ``hello``
        # send (observed at startup: a page opens the control WS then
        # immediately closes/reloads). Without this guard the
        # disconnect escapes as an unhandled ASGI exception
        # (ClientDisconnected → WebSocketDisconnect) and dumps a noisy
        # traceback. The helper swallows the race; returning False
        # means "peer is gone, nothing to clean up" so we just exit.
        # Same implementation backs ``_ws.py`` — single source of truth
        # for the pattern (report P13).
        if not await _safe_send_json(
            websocket,
            {"type": "hello", "protocol": "qai.chat.control/1"},
        ):
            return

        try:
            while True:
                try:
                    msg = await websocket.receive_json()
                except WebSocketDisconnect:
                    return
                except (ValueError, KeyError, UnicodeDecodeError) as exc:
                    # Defensive: starlette's ``receive_json`` raises
                    #   * ``json.JSONDecodeError`` (subclass of ValueError)
                    #     when the text payload is not valid JSON;
                    #   * ``KeyError("text")`` when the client sent a
                    #     binary WebSocket frame in text mode;
                    #   * ``UnicodeDecodeError`` if mode were ever
                    #     switched to binary with non-utf8 bytes.
                    # All three mean "this frame is garbage" — surface
                    # an error envelope and KEEP the connection open so
                    # other tabs on this page can keep sending control
                    # frames. Closing here would let one bad client
                    # silence every tab the page owns.
                    try:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "id": None,
                                "error": f"malformed control frame: {type(exc).__name__}",
                            },
                        )
                    except (WebSocketDisconnect, RuntimeError):
                        return
                    continue

                await _dispatch_control_frame(
                    websocket=websocket,
                    container=container,
                    frame=msg if isinstance(msg, dict) else {},
                )
        except WebSocketDisconnect:
            # Page navigated away / tab closed — nothing to clean up.
            # The data plane (SSE / WS) has its own disconnect handling.
            return
        except RuntimeError:
            # starlette raises ``RuntimeError`` from ``websocket.receive``
            # / ``send`` once the connection has reached a terminal state
            # (e.g. a disconnect message was already consumed by a prior
            # send_json attempt). Treat as "connection gone, exit cleanly"
            # — the control plane carries no resumable state, and letting
            # this bubble out would crash the WS endpoint with no benefit.
            return
        except (
            asyncio.CancelledError
        ):  # pragma: no cover — server shutdown path
            try:
                await websocket.close(code=status.WS_1001_GOING_AWAY)
            except RuntimeError:
                pass
            raise

    return router


# ---------------------------------------------------------------------------
# Frame dispatch
# ---------------------------------------------------------------------------


async def _dispatch_control_frame(
    *,
    websocket: WebSocket,
    container: "Container",
    frame: dict[str, Any],
) -> None:
    """Route one client→server control frame to the right use case.

    Sends a single ``ack`` (success) or ``error`` (malformed / domain
    rejection) envelope back over the same WebSocket. The reply is
    fire-and-forget from the client's perspective but observable for
    debugging / future UX hooks.

    Catches :class:`QaiError` (domain validation rejections from
    ``TabId.of`` etc.) so a single malformed frame from one tab never
    tears down the shared control connection — that page-scoped WS
    multiplexes every chat tab's control traffic, and one bad tab id
    must not silence the others. Unexpected exceptions are logged and
    surfaced as a generic error envelope (same containment guarantee)
    rather than allowed to propagate out and close the WS.
    """
    client_msg_id = frame.get("id") if isinstance(frame.get("id"), str) else None
    kind = frame.get("type")

    try:
        if kind == _CMD_ANSWER:
            await _handle_answer_frame(
                websocket=websocket,
                container=container,
                frame=frame,
                client_msg_id=client_msg_id,
            )
            return

        if kind == _CMD_STOP:
            await _handle_stop_frame(
                websocket=websocket,
                container=container,
                frame=frame,
                client_msg_id=client_msg_id,
            )
            return

        if kind == _CMD_INJECT:
            await _handle_inject_frame(
                websocket=websocket,
                container=container,
                frame=frame,
                client_msg_id=client_msg_id,
            )
            return

        if kind == _CMD_INJECT_CANCEL:
            await _handle_inject_cancel_frame(
                websocket=websocket,
                container=container,
                frame=frame,
                client_msg_id=client_msg_id,
            )
            return

        if kind == _CMD_RETRY_NOW:
            await _handle_retry_now_frame(
                websocket=websocket,
                container=container,
                frame=frame,
                client_msg_id=client_msg_id,
            )
            return

        if kind == _CMD_CANCEL_TOOL:
            await _handle_cancel_tool_frame(
                websocket=websocket,
                container=container,
                frame=frame,
                client_msg_id=client_msg_id,
            )
            return

        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=f"unsupported control frame type {kind!r}",
        )
    except QaiError as exc:
        # Domain rejection (e.g. malformed ID slipped past our shallow
        # checks). Surface to the client without taking down the WS.
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=str(exc),
        )
    except Exception as exc:  # containment: keep the WS alive for other tabs
        # Unexpected: log loudly, but still keep the control WS alive
        # so other tabs on this page can keep sending control frames.
        logger.error(
            "chat.control.unexpected_error",
            kind=str(kind),
            error=str(exc),
            exc_info=True,
        )
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="internal error handling control frame",
        )


async def _handle_answer_frame(
    *,
    websocket: WebSocket,
    container: "Container",
    frame: dict[str, Any],
    client_msg_id: str | None,
) -> None:
    tab_id_raw = frame.get("tab_id")
    answer = frame.get("answer")
    if not isinstance(tab_id_raw, str) or not tab_id_raw:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="answer.tab_id must be a non-empty string",
        )
        return
    if len(tab_id_raw) > _MAX_TAB_ID_LENGTH:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=(
                f"answer.tab_id length {len(tab_id_raw)} "
                f"exceeds maximum {_MAX_TAB_ID_LENGTH}"
            ),
        )
        return
    if not isinstance(answer, str):
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="answer.answer must be a string",
        )
        return
    if len(answer) > _MAX_ANSWER_LENGTH:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=(
                f"answer.answer length {len(answer)} "
                f"exceeds maximum {_MAX_ANSWER_LENGTH}"
            ),
        )
        return

    # Mirrors POST /api/chat/answer (interfaces/http/routes/chat/_rest.py)
    # exactly — same registry, same idempotency semantics.
    delivered = container.chat.question_registry.resolve(
        TabId.of(tab_id_raw),
        answer,
    )
    logger.info(
        "chat.control.answer",
        tab_id=tab_id_raw,
        delivered=delivered,
        via="ws",
    )
    await _send_ack(
        websocket,
        client_msg_id=client_msg_id,
        kind=_CMD_ANSWER,
        tab_id=tab_id_raw,
        ok=True,
        result={"delivered": delivered},
    )


async def _handle_inject_frame(
    *,
    websocket: WebSocket,
    container: "Container",
    frame: dict[str, Any],
    client_msg_id: str | None,
) -> None:
    """Record a mid-turn user injection for a tab's live stream.

    The "inject" button (V2 enhancement) folds a new ``role:user`` instruction
    into the SAME in-flight run: the streaming run loop drains this registry at
    its inter-round seam (between tool rounds) and appends each pending text as
    a ``role:user`` message + emits an ``injected_message`` data frame. Unlike
    ``answer`` / ``stop`` there is NO REST equivalent — an injection only makes
    sense for a live, in-process stream, so it is control-plane-only.

    ``delivered`` reports whether the registry accepted the injection (a
    non-empty text was recorded). It does NOT guarantee the model will fold it
    in this turn: if the turn ends with no further tool round, the run loop's
    teardown drops the un-consumed injection and the frontend's local fallback
    re-sends it as a fresh queued turn (so no user content is lost).
    """
    tab_id_raw = frame.get("tab_id")
    text = frame.get("text")
    if not isinstance(tab_id_raw, str) or not tab_id_raw:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="inject.tab_id must be a non-empty string",
        )
        return
    if len(tab_id_raw) > _MAX_TAB_ID_LENGTH:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=(
                f"inject.tab_id length {len(tab_id_raw)} "
                f"exceeds maximum {_MAX_TAB_ID_LENGTH}"
            ),
        )
        return
    if not isinstance(text, str):
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="inject.text must be a string",
        )
        return
    if len(text) > _MAX_INJECT_LENGTH:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=(
                f"inject.text length {len(text)} "
                f"exceeds maximum {_MAX_INJECT_LENGTH}"
            ),
        )
        return

    # Record the pending injection; the live streaming run loop drains it at
    # its inter-round seam (empty / whitespace-only text is rejected by the
    # registry → ``delivered=False``).
    #
    # Image parity: images ride INSIDE ``text`` as ``![](url)`` markdown (the
    # SAME shape a normal submit uses), so the inject frame needs no separate
    # media field — the run loop's inter-round seam extracts the refs from the
    # text and resolves them to vision blocks (streaming.py ``_inject_hook``).
    delivered = container.chat.injection_registry.inject(
        TabId.of(tab_id_raw),
        text,
    )
    logger.info(
        "chat.control.inject",
        tab_id=tab_id_raw,
        delivered=delivered,
        via="ws",
    )
    await _send_ack(
        websocket,
        client_msg_id=client_msg_id,
        kind=_CMD_INJECT,
        tab_id=tab_id_raw,
        ok=True,
        result={"delivered": delivered},
    )


async def _handle_inject_cancel_frame(
    *,
    websocket: WebSocket,
    container: "Container",
    frame: dict[str, Any],
    client_msg_id: str | None,
) -> None:
    """Withdraw a not-yet-folded mid-turn injection (V2 enhancement).

    The inject button shows the queued text as an editable / cancellable
    bubble until the run loop folds it in. When the user edits or cancels
    that bubble first, the frontend sends this so the run loop does NOT also
    fold the withdrawn text into the wire (otherwise the same text would be
    both re-edited in the composer AND injected -- a double submission).

    ``removed`` reports whether a pending injection was actually withdrawn;
    ``False`` is a benign no-op (it was already drained / never pending).
    """
    tab_id_raw = frame.get("tab_id")
    text = frame.get("text")
    if not isinstance(tab_id_raw, str) or not tab_id_raw:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="inject_cancel.tab_id must be a non-empty string",
        )
        return
    if len(tab_id_raw) > _MAX_TAB_ID_LENGTH:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=(
                f"inject_cancel.tab_id length {len(tab_id_raw)} "
                f"exceeds maximum {_MAX_TAB_ID_LENGTH}"
            ),
        )
        return
    if not isinstance(text, str):
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="inject_cancel.text must be a string",
        )
        return
    removed = container.chat.injection_registry.withdraw(
        TabId.of(tab_id_raw),
        text,
    )
    logger.info(
        "chat.control.inject_cancel",
        tab_id=tab_id_raw,
        removed=removed,
        via="ws",
    )
    await _send_ack(
        websocket,
        client_msg_id=client_msg_id,
        kind=_CMD_INJECT_CANCEL,
        tab_id=tab_id_raw,
        ok=True,
        result={"removed": removed},
    )


async def _handle_retry_now_frame(
    *,
    websocket: WebSocket,
    container: "Container",
    frame: dict[str, Any],
    client_msg_id: str | None,
) -> None:
    """Cut short a tab's network-retry backoff so it re-opens now.

    The "立即重试" button (V2 enhancement): while a turn waits out an
    escalating network-retry backoff (3s → 5s → 10s → 30s …), the user may
    have manually restored connectivity and wants the next attempt at once.
    NON-terminal and control-plane-only (no REST equivalent — it only makes
    sense for a live, mid-backoff stream). Idempotent + benign: signalling a
    tab that is not currently waiting is a no-op (``requested=False``).
    """
    tab_id_raw = frame.get("tab_id")
    if not isinstance(tab_id_raw, str) or not tab_id_raw:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="retry_now.tab_id must be a non-empty string",
        )
        return
    if len(tab_id_raw) > _MAX_TAB_ID_LENGTH:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=(
                f"retry_now.tab_id length {len(tab_id_raw)} "
                f"exceeds maximum {_MAX_TAB_ID_LENGTH}"
            ),
        )
        return
    # Signal the abort registry's retry-now flag; the in-flight
    # ``_abortable_sleep`` polls it and re-opens the LLM stream immediately.
    # ``getattr`` probe keeps a pre-retry-now registry stub safe (returns
    # ``requested=False``).
    request_retry = getattr(
        container.chat.abort_registry, "request_retry_now", None
    )
    requested = bool(request_retry(TabId.of(tab_id_raw))) if request_retry else False
    logger.info(
        "chat.control.retry_now",
        tab_id=tab_id_raw,
        requested=requested,
        via="ws",
    )
    await _send_ack(
        websocket,
        client_msg_id=client_msg_id,
        kind=_CMD_RETRY_NOW,
        tab_id=tab_id_raw,
        ok=True,
        result={"requested": requested},
    )


async def _handle_stop_frame(
    *,
    websocket: WebSocket,
    container: "Container",
    frame: dict[str, Any],
    client_msg_id: str | None,
) -> None:
    tab_id_raw = frame.get("tab_id")
    reason_raw = frame.get("reason")
    if not isinstance(tab_id_raw, str) or not tab_id_raw:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="stop.tab_id must be a non-empty string",
        )
        return
    if len(tab_id_raw) > _MAX_TAB_ID_LENGTH:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=(
                f"stop.tab_id length {len(tab_id_raw)} "
                f"exceeds maximum {_MAX_TAB_ID_LENGTH}"
            ),
        )
        return
    # ``reason`` semantics in lock-step with REST ``StopChatRequest``:
    #   * field MAY be omitted entirely      → defaults to "user_requested"
    #     (matches Pydantic ``Field("user_requested", ...)``);
    #   * field MUST NOT be present-but-invalid (empty / non-string /
    #     length>64) → reject with an explicit error envelope, matching
    #     Pydantic's 422 response on the REST side. Silently substituting
    #     the default for a malformed reason would be a quiet behavior
    #     divergence between the two channels.
    if "reason" not in frame:
        reason = "user_requested"
    elif not isinstance(reason_raw, str) or not reason_raw:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="stop.reason must be a non-empty string when provided",
        )
        return
    elif len(reason_raw) > _MAX_REASON_LENGTH:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=(
                f"stop.reason length {len(reason_raw)} "
                f"exceeds maximum {_MAX_REASON_LENGTH}"
            ),
        )
        return
    else:
        reason = reason_raw

    # Mirrors POST /api/chat/stop (interfaces/http/routes/chat/_rest.py)
    # exactly — same use case, same idempotency semantics.
    result = await container.chat.stop_chat_use_case.execute(
        StopChatInput(tab_id=TabId.of(tab_id_raw), reason=reason),
    )
    logger.info(
        "chat.control.stop",
        tab_id=tab_id_raw,
        aborted=result.aborted,
        reason=result.reason,
        via="ws",
    )
    await _send_ack(
        websocket,
        client_msg_id=client_msg_id,
        kind=_CMD_STOP,
        tab_id=tab_id_raw,
        ok=True,
        result={"aborted": result.aborted, "reason": result.reason},
    )


async def _handle_cancel_tool_frame(
    *,
    websocket: WebSocket,
    container: "Container",
    frame: dict[str, Any],
    client_msg_id: str | None,
) -> None:
    """Per-call single-tool cancel. Mirrors POST /api/chat/cancel_tool.

    Validates ``tab_id`` + ``call_id`` with the same limits as the REST DTO,
    then delegates to :class:`CancelToolUseCase` (which is idempotent and never
    aborts the turn).
    """
    tab_id_raw = frame.get("tab_id")
    call_id_raw = frame.get("call_id")
    if not isinstance(tab_id_raw, str) or not tab_id_raw:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="cancel_tool.tab_id must be a non-empty string",
        )
        return
    if len(tab_id_raw) > _MAX_TAB_ID_LENGTH:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=(
                f"cancel_tool.tab_id length {len(tab_id_raw)} "
                f"exceeds maximum {_MAX_TAB_ID_LENGTH}"
            ),
        )
        return
    if not isinstance(call_id_raw, str) or not call_id_raw:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message="cancel_tool.call_id must be a non-empty string",
        )
        return
    if len(call_id_raw) > _MAX_CALL_ID_LENGTH:
        await _send_error(
            websocket,
            client_msg_id=client_msg_id,
            message=(
                f"cancel_tool.call_id length {len(call_id_raw)} "
                f"exceeds maximum {_MAX_CALL_ID_LENGTH}"
            ),
        )
        return

    result = await container.chat.cancel_tool_use_case.execute(
        CancelToolInput(tab_id=TabId.of(tab_id_raw), call_id=call_id_raw),
    )
    logger.info(
        "chat.control.cancel_tool",
        tab_id=tab_id_raw,
        call_id=call_id_raw,
        cancelled=result.cancelled,
        via="ws",
    )
    await _send_ack(
        websocket,
        client_msg_id=client_msg_id,
        kind=_CMD_CANCEL_TOOL,
        tab_id=tab_id_raw,
        ok=True,
        result={"cancelled": result.cancelled, "call_id": result.call_id},
    )


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


async def _send_ack(
    websocket: WebSocket,
    *,
    client_msg_id: str | None,
    kind: str,
    tab_id: str,
    ok: bool,
    result: dict[str, Any],
) -> None:
    try:
        await websocket.send_json(
            {
                "type": "ack",
                "id": client_msg_id,
                "kind": kind,
                "tab_id": tab_id,
                "ok": ok,
                "result": result,
            },
        )
    except (WebSocketDisconnect, RuntimeError):
        # Client gone between dispatch and ack — the control action
        # already executed (registry.resolve / stop_chat_use_case), so
        # there is nothing to roll back. Swallow.
        return


async def _send_error(
    websocket: WebSocket,
    *,
    client_msg_id: str | None,
    message: str,
) -> None:
    try:
        await websocket.send_json(
            {
                "type": "error",
                "id": client_msg_id,
                "error": message,
            },
        )
    except (WebSocketDisconnect, RuntimeError):
        return


__all__ = ["build_router"]
