# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Chat HTTP routes aggregate (PR-033 / S3).

The chat surface is large enough (REST + SSE + data WS + control WS +
OpenAI Compat, ≥ 19 routes total) that it is split across sibling modules:

* :mod:`._rest`            — JSON REST endpoints under ``/api/chat``
* :mod:`._sse`             — Server-Sent Events streaming endpoint
                             under ``/api/chat``
* :mod:`._ws`              — Per-turn data WebSocket endpoint under
                             ``/api/chat/ws`` (opt-in alternative to SSE
                             for the data plane)
* :mod:`._control_ws`      — Page-scoped control-plane WebSocket under
                             ``/api/chat/control`` (carries ``answer`` /
                             ``stop`` frames; physically independent of
                             the data plane to bypass HTTP/1.1 6-connection
                             pool exhaustion — see module docstring)
* :mod:`._openai_compat`   — 1:1 third-party-facing OpenAI API under
                             ``/v1`` (NOT under ``/api/chat`` — see
                             ``08-business-capabilities.md`` §8.2 +
                             ``S3-sub-agent-spec.md`` §8 row 6)

This module's :func:`build_router` aggregates all sibling sub-routers
into a single :class:`fastapi.APIRouter` so :mod:`apps.api.main` mounts
everything with one ``include_router`` call.

Frame contract (locked here for PR-035 / PR-034 to follow)
----------------------------------------------------------
SSE wire format::

    event: message
    data: {<StreamFrame.payload + frame_id + frame_type + sequence>}

    event: error
    data: {<QaiError.to_dict()>}

    event: done
    data: {}

    : ping

WebSocket wire format (same JSON envelopes minus the ``event: ...``
header)::

    {"type": "ready",  "session_id": "..."}            # server -> client (handshake)
    {"type": "send",   "prompt": "..."}                # client -> server
    {"type": "stop"}                                   # client -> server
    {"type": "frame",  "frame": {...StreamFrame...}}   # server -> client
    {"type": "error",  "error": {...QaiError...}}      # server -> client (terminal)
    {"type": "done"}                                   # server -> client (terminal)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

from ._agent import build_router as _build_agent_router
from ._control_ws import build_router as _build_control_ws_router
from ._image_and_prompt import build_router as _build_image_and_prompt_router
from ._mcp import build_router as _build_mcp_router
from ._mode import build_router as _build_mode_router
from ._openai_compat import build_router as _build_openai_compat_router
from ._rest import build_router as _build_rest_router
from ._roster import build_router as _build_roster_router
from ._sse import build_router as _build_sse_router
from ._ws import build_router as _build_ws_router

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


def build_router(*, container: "Container") -> APIRouter:
    """Build the aggregate chat router.

    Combines REST + SSE + data WS + control WS + OpenAI Compat + extras
    into a single :class:`fastapi.APIRouter`.
    """
    aggregate = APIRouter()
    aggregate.include_router(_build_rest_router(container=container))
    aggregate.include_router(_build_roster_router(container=container))
    aggregate.include_router(_build_agent_router(container=container))
    aggregate.include_router(_build_mode_router(container=container))
    aggregate.include_router(_build_sse_router(container=container))
    aggregate.include_router(_build_ws_router(container=container))
    # Page-scoped control-plane WS — independent of per-turn data WS so
    # ``answer`` / ``stop`` frames never queue behind a long SSE stream.
    aggregate.include_router(_build_control_ws_router(container=container))
    aggregate.include_router(_build_openai_compat_router(container=container))
    # PR-403: legacy `/api/images/upload` + `/api/prompt/enhance` +
    # `/api/prompt-snapshot/{id}` — co-located with the chat router so
    # they share `ChatServices` DI.
    aggregate.include_router(_build_image_and_prompt_router(container=container))
    # MCP (Model Context Protocol) server management — new routes under
    # /api/chat/mcp; MCP tools flow through the existing tool pipeline.
    aggregate.include_router(_build_mcp_router(container=container))
    return aggregate


__all__ = ["build_router"]
