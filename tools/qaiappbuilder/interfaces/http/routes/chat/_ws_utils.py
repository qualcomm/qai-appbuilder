# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared WebSocket helpers for the chat data + control planes.

Centralises the "accept → first-frame send race" guard pattern (the
same race the ``_control_ws.py:165`` hello-send fix addressed). Every
``websocket.accept()`` is followed by at least one ``send_json``, and
the client can disconnect in the tiny window between them (page reload
right after upgrade, tab closed during handshake, …). Without a guard
the disconnect bubbles up as an unhandled ASGI exception and dumps a
noisy traceback (``ClientDisconnected`` → ``WebSocketDisconnect``).

Use :func:`safe_send_json` for every server→client send that may run
in such a window. It returns ``False`` instead of raising when the
peer is gone; callers typically just ``return``.

Mirrors ``_control_ws.py``'s inline catch shape — the same module
uses this helper too (P13 / report D.6) so there is exactly one
implementation. AGENTS.md "复用 > 重造".
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from qai.platform.logging import get_logger


__all__ = ["safe_send_json", "spawn_disconnect_watcher"]


_log = get_logger(__name__)


async def safe_send_json(ws: WebSocket, data: dict[str, Any]) -> bool:
    """Send ``data`` as JSON on ``ws``; swallow disconnect races.

    Returns ``True`` if the send succeeded, ``False`` if the peer was
    already gone (or the underlying transport refused mid-send). The
    caller should typically ``return`` on ``False`` — the connection is
    no longer usable, there is nothing to clean up beyond what
    starlette / uvicorn do on disconnect, and the page will reconnect
    on its own.

    Caught exceptions:

    * :class:`fastapi.WebSocketDisconnect` — clean client close
      between ``accept()`` and our first send (or anywhere mid-stream).
    * ``RuntimeError`` — starlette raises this from ``send()`` once the
      connection has reached a terminal state (e.g. a disconnect
      message was already consumed by a prior send attempt on the same
      task).
    * ``ConnectionError`` — covers the uvicorn/asyncio "after close"
      send variants (``ConnectionResetError`` / ``ConnectionAbortedError``).
    """
    try:
        await ws.send_json(data)
        return True
    except (WebSocketDisconnect, RuntimeError, ConnectionError) as exc:
        _log.debug(
            "chat.ws.send_aborted",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False


def spawn_disconnect_watcher(
    ws: WebSocket,
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """Watch a pure-server→push WebSocket for a client disconnect.

    Root-cause fix for the ``socket.send() raised exception.`` log flood
    (asyncio ``proactor_events.py`` / ``selector_events.py``): once a
    connection is lost, writing to the transport is SILENTLY dropped and
    counted — it does NOT raise — so ``send_json`` (and therefore
    :func:`safe_send_json`) returns success even to a dead peer. A pure-push
    loop (e.g. ``/api/ws/events`` emitting a heartbeat every 30s) that only
    ever writes then never learns the peer is gone and keeps writing forever;
    after ``LOG_THRESHOLD_FOR_CONNLOST_WRITES`` (5) such writes asyncio logs
    the warning on EVERY subsequent write until uvicorn's ping-timeout finally
    tears the socket down — a sustained flood, worst on the Windows
    ProactorEventLoop where a half-open connection lingers.

    The RELIABLE disconnect signal is the ASGI *receive* channel: on
    connection loss uvicorn delivers a ``{"type": "websocket.disconnect"}``
    message, which starlette surfaces as :class:`WebSocketDisconnect` from
    ``receive()``. A pure-push loop never receives, so it never observes it.
    This helper spawns a background task that drains ``receive()`` until the
    disconnect (or any terminal receive error) and sets the returned
    :class:`asyncio.Event`. The push loop races its own work against
    ``disconnected.wait()`` and exits the instant the peer is gone — so at most
    ONE write ever lands on a lost transport (well under the 5-write threshold)
    and the flood cannot occur.

    Returns ``(disconnected, task)``. The caller MUST cancel ``task`` in its
    ``finally`` (the task also self-completes on disconnect). Cancelling a
    completed task is a harmless no-op.
    """
    disconnected = asyncio.Event()

    async def _watch() -> None:
        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    return
        except WebSocketDisconnect:
            return
        except (RuntimeError, ConnectionError):
            # Terminal socket state observed on the receive side — treat as a
            # disconnect so the push loop stops writing.
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let the watcher crash escape
            _log.debug(
                "chat.ws.disconnect_watch_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return
        finally:
            disconnected.set()

    task = asyncio.create_task(_watch())
    return disconnected, task
