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

from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from qai.platform.logging import get_logger


__all__ = ["safe_send_json"]


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
