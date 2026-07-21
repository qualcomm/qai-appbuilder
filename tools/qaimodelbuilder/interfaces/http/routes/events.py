# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Global SSE event bus route.

Route:
- ``GET /api/events`` — global SSE stream aggregating all domain events

This restores the legacy global event bus endpoint. The WebUI uses this
single persistent connection to receive all server-push notifications
(permission requests, download progress, chat updates, etc.) instead of
polling individual resource endpoints.

Design decision (P3-1, option A): a single ``/api/events`` SSE stream
that multiplexes all event types via the platform ``EventBus``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from interfaces.http.routes.chat._ws_utils import safe_send_json

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container
    from qai.platform.events.types import EventEnvelope


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the global events SSE router."""
    router = APIRouter(tags=["events"])

    @router.get("/api/events")
    async def global_events() -> StreamingResponse:
        """Global SSE event stream.

        Subscribes to all domain events via the platform EventBus and
        serialises each as an SSE frame. The stream stays open until
        the client disconnects. A heartbeat comment is emitted every
        30 seconds to keep proxies from closing the connection.
        """
        events = container.events

        # Local fan-in queue. The bus delivers ``EventEnvelope`` objects to
        # ``_on_event``; the generator drains this queue. ``EventSubscription``
        # itself does NOT expose its internal queue, so each consumer must
        # bring its own (mirrors the pattern used in ``security/_skills.py``).
        queue: asyncio.Queue["EventEnvelope | None"] = asyncio.Queue(maxsize=256)

        async def _on_event(envelope: "EventEnvelope") -> None:
            # The global ``/api/events`` SSE is a LOW-frequency notification
            # channel (permission requests, download/run lifecycle, conversation
            # list updates, ``reboot`` ...). High-frequency per-frame streaming
            # data (chat tokens, App Builder run frames, AI Coding frames, model
            # download progress) is NOT published to the event bus at all — it is
            # dropped at the source in each streaming use case and delivered to
            # the front-end over its own dedicated SSE/WS instead. So there is no
            # per-event-type denylist to maintain here: as new streaming modes
            # are added (model conversion, translation, PPT, ...) they simply
            # follow the same "per-frame data never goes on the bus" convention
            # and this channel stays quiet automatically.
            #
            # The bounded fan-in queue below is kept purely as a defensive
            # backstop (State-Truth-First): if any future code path ever bursts
            # events faster than this SSE can serialise them, we drop the OLDEST
            # to keep the stream live rather than blocking the bus — loss is
            # acceptable for a notification channel (clients re-poll on
            # reconnect).
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:  # pragma: no cover — slow consumer
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — race
                    pass
                try:
                    queue.put_nowait(envelope)
                except asyncio.QueueFull:  # pragma: no cover — defensive
                    pass

        async def _event_generator():
            subscription = await events.subscribe(
                event_type="*",
                handler=_on_event,
            )
            try:
                while True:
                    try:
                        envelope = await asyncio.wait_for(
                            queue.get(), timeout=30.0
                        )
                    except asyncio.TimeoutError:
                        # Heartbeat to keep connection alive
                        yield b": heartbeat\n\n"
                        continue

                    if envelope is None:
                        # Bus closed
                        break

                    # ``handler`` receives an ``EventEnvelope``; the wire
                    # frame uses the underlying ``DomainEvent`` payload.
                    event = envelope.event
                    event_type = envelope.event_type
                    payload = _serialise_event(event, event_type)
                    data = json.dumps(payload, ensure_ascii=False, default=str)
                    yield f"event: {event_type}\ndata: {data}\n\n".encode(
                        "utf-8"
                    )
            finally:
                # ``EventSubscription.unsubscribe()`` is the documented
                # detach API (see ``qai.platform.events.bus``). The legacy
                # call ``subscription.cancel()`` did not exist on the
                # dataclass and raised ``AttributeError`` on every
                # disconnect, surfacing as HTTP 500.
                await subscription.unsubscribe()

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return router


def build_ws_router(*, container: "Container") -> APIRouter:
    """Build the global events **WebSocket** router.

    ``WS /api/ws/events`` — the WebSocket transport for the same global
    notification channel as the ``GET /api/events`` SSE route. The WebUI
    prefers this WS (browsers cap HTTP/1.1 connections at ~6 per origin;
    WebSocket is exempt) and falls back to the SSE route when the WS
    handshake fails. Both transports are driven by the SAME
    ``events.subscribe("*")`` fan-out and the SAME :func:`_serialise_event`
    payloads, so the front-end sees byte-identical event objects on either.

    Each connected client gets its own bounded drop-oldest fan-in queue
    (mirrors the SSE route). No per-connection session scoping: like the
    SSE route this broadcasts ALL domain events to every connected tab and
    the front-end discriminates by the payload ``type`` (loopback-only
    deployment; consistent with the existing chat control WS posture).
    """
    router = APIRouter(tags=["events"])

    @router.websocket("/api/ws/events")
    async def global_events_ws(websocket: WebSocket) -> None:
        events = container.events
        await websocket.accept()

        queue: asyncio.Queue["EventEnvelope | None"] = asyncio.Queue(maxsize=256)

        async def _on_event(envelope: "EventEnvelope") -> None:
            # Same bounded drop-oldest backstop as the SSE route: a
            # notification channel prefers staying live over blocking the
            # bus, and clients re-poll (``fetchPending``) on reconnect.
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:  # pragma: no cover — slow consumer
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — race
                    pass
                try:
                    queue.put_nowait(envelope)
                except asyncio.QueueFull:  # pragma: no cover — defensive
                    pass

        subscription = await events.subscribe(event_type="*", handler=_on_event)
        try:
            while True:
                try:
                    envelope = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Keepalive sentinel (the front-end ignores ``__heartbeat__``).
                    # WS has no SSE-comment concept; a tiny JSON frame both keeps
                    # proxies open and lets the client detect a dead peer.
                    if not await safe_send_json(
                        websocket, {"type": "__heartbeat__"}
                    ):
                        break
                    continue

                if envelope is None:  # bus closed
                    break

                payload = _serialise_event(envelope.event, envelope.event_type)
                # ``safe_send_json`` swallows the disconnect race and returns
                # False when the peer is gone → exit the loop + unsubscribe.
                if not await safe_send_json(websocket, payload):
                    break
        except WebSocketDisconnect:
            # Tab closed / navigated away — nothing to clean up beyond the
            # subscription (done in ``finally``).
            pass
        except (asyncio.CancelledError, RuntimeError):
            # Server shutdown / terminal socket state — exit cleanly; the
            # channel carries no resumable state (clients re-poll on reconnect).
            pass
        finally:
            await subscription.unsubscribe()

    return router


def _json_safe(value: Any) -> Any:
    """Recursively coerce ``value`` into JSON-native primitives.

    The global events channel is consumed by TWO transports: the SSE
    endpoint (``json.dumps(..., default=str)`` — already datetime-safe) AND
    the WebSocket endpoint (``websocket.send_json`` — uses the *default* JSON
    encoder with NO ``default=`` hook, so a raw ``datetime`` / enum / value
    object raises ``TypeError: Object of type datetime is not JSON
    serializable`` and TEARS DOWN THE SOCKET — which broke the permission-ASK
    push path: the authorization popup events never reached the front-end, so
    a `read` ASK hung until the 60s fail-closed deny).

    Normalising HERE (the single serialisation boundary both transports share)
    immunises EVERY current and future domain event without touching each
    field-locked domain dataclass. Handles the shapes security / other events
    actually carry: ``datetime`` → ISO-8601; ``Enum`` → its ``value``;
    ``Path`` → ``str``; nested dict / list / tuple / set recursed; anything
    else with an ``isoformat`` (date/time) → ISO; final fallback ``str(...)``
    (mirrors the SSE path's ``default=str``) so serialisation can never crash
    the stream.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    isoformat = getattr(value, "isoformat", None)  # date / time / etc.
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:  # noqa: BLE001 — fall through to str()
            pass
    return str(value)


def _serialise_event(event: Any, event_type: str) -> dict[str, Any]:
    """Best-effort JSON-serialisable view of a ``DomainEvent``.

    Prefers a ``to_dict`` method when the event provides one (legacy
    contract), otherwise falls back to ``dataclasses.asdict``. Plain
    objects fall through to ``{"type": event_type}`` so that the SSE
    frame still carries the routing key for clients.

    The result is run through :func:`_json_safe` so the payload is composed
    ONLY of JSON-native primitives — critical for the WebSocket transport
    (``send_json`` has no ``default=`` hook and would otherwise raise on a
    raw ``datetime`` / enum / value object and drop the connection).
    """
    to_dict = getattr(event, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
        except Exception:  # noqa: BLE001 — never break the stream on a bad event
            payload = None
        if isinstance(payload, dict):
            payload.setdefault("type", event_type)
            return _json_safe(payload)
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        try:
            payload = dataclasses.asdict(event)
        except Exception:  # noqa: BLE001 — defensive
            payload = {}
        payload.setdefault("type", event_type)
        return _json_safe(payload)
    return {"type": event_type}
