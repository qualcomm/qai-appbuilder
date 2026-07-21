# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""GoMaster session-control routes — /api/gomaster-session/*.

These routes back the chat composer's「GoMaster 在线」mode toolbar buttons:

* ``POST /api/gomaster-session/connect``    — create + remember this tab's session
* ``POST /api/gomaster-session/disconnect`` — forget this tab's session
* ``GET  /api/gomaster-session/state``      — current session snapshot
* ``GET  /api/gomaster-session/native-url`` — the original GoMaster site URL
  (internal-only; the frontend "open original GoMaster" link target)

The chat turn (``query::gomaster`` hint → ``GomasterSessionAdapter``) reuses the
same remembered ``session_id``, so these routes only manage lifecycle — they do
NOT carry conversation messages (those flow through the normal chat WS/SSE).

Layering (import-linter ``interfaces-stays-thin``): this module must NOT import
``qai.chat.infrastructure``. The session-lifecycle facade
(:class:`~apps.api._gomaster_session_bridge.GomasterSessionController`) is
composed at the apps/api composition root and injected onto
``container.gomaster_session``; these handlers consume it by duck-typing.

internal-only (four-layer defence, layer 1 — runtime gate): the controller is
``None`` on external editions (built only when ``settings.is_internal``), so
every handler short-circuits to a 404-equivalent / disabled response. The
intranet ``native-url`` is therefore never disclosed on external builds.

CSRF: the non-GET handlers are subject to the global double-submit-cookie
``CsrfMiddleware`` (same as the MB Pro routes); read-only GETs are exempt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---------------------------------------------------------------------------
# Schemas (module level — FastAPI body-param resolution discipline)
# ---------------------------------------------------------------------------


class ConnectRequest(BaseModel):
    """Body for ``POST /api/gomaster-session/connect``.

    ``tab_id`` binds the session to THIS chat tab (each tab owns an independent
    GoMaster session so histories never mix). The remaining fields are optional:
    an empty body (besides ``tab_id``) connects to the factory-default host
    (``internal_config.toml [query_services.gomaster]``). The settings dialog
    overrides ``agent_url`` for a custom host and ``session_id`` to re-attach to
    this tab's existing remote session (history restore).
    """

    tab_id: str
    agent_url: str | None = None
    session_id: str | None = None


class DisconnectRequest(BaseModel):
    """Body for ``POST /api/gomaster-session/disconnect`` — which tab."""

    tab_id: str


class SessionStateResponse(BaseModel):
    """Snapshot of the current GoMaster session connection."""

    connected: bool
    session_id: str | None = None
    agent_url: str | None = None


class NativeUrlResponse(BaseModel):
    """The original GoMaster site URL (internal-only)."""

    url: str | None = None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _controller(container: "Container") -> Any | None:
    """Return the injected GoMaster session controller, or ``None`` (external)."""
    return getattr(container, "gomaster_session", None)


def build_router(*, container: "Container") -> APIRouter:
    """Build the GoMaster session-control router (internal-only behaviour)."""
    router = APIRouter(prefix="/api/gomaster-session", tags=["gomaster"])

    @router.post("/connect", response_model=SessionStateResponse)
    async def connect(body: ConnectRequest) -> SessionStateResponse:
        ctrl = _controller(container)
        if ctrl is None:
            raise HTTPException(status_code=404, detail="GoMaster not available")
        try:
            state = await ctrl.connect(
                tab_id=body.tab_id,
                agent_url=body.agent_url,
                session_id=body.session_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return SessionStateResponse(**state)

    @router.post("/disconnect", response_model=SessionStateResponse)
    async def disconnect(body: DisconnectRequest) -> SessionStateResponse:
        ctrl = _controller(container)
        if ctrl is None:
            raise HTTPException(status_code=404, detail="GoMaster not available")
        state = await ctrl.disconnect(tab_id=body.tab_id)
        return SessionStateResponse(**state)

    @router.get("/state", response_model=SessionStateResponse)
    async def state(tab_id: str) -> SessionStateResponse:
        ctrl = _controller(container)
        if ctrl is None:
            return SessionStateResponse(connected=False)
        return SessionStateResponse(**ctrl.get_state(tab_id=tab_id))

    @router.get("/native-url", response_model=NativeUrlResponse)
    async def native_url() -> NativeUrlResponse:
        ctrl = _controller(container)
        if ctrl is None:
            # External edition: the intranet URL is never disclosed.
            return NativeUrlResponse(url=None)
        return NativeUrlResponse(url=ctrl.native_url())

    return router


__all__ = ["build_router"]
