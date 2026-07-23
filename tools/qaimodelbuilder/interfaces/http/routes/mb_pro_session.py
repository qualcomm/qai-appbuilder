# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""MB Pro (Model Builder Pro) session-control routes — /api/mb-pro-session/*.

These routes back the chat composer's「Pro / 增强」mode toolbar buttons:

* ``POST /api/mb-pro-session/connect``   — establish the session + SSE long-poll
* ``POST /api/mb-pro-session/disconnect``— tear down the session
* ``GET  /api/mb-pro-session/state``     — current connection snapshot
* ``GET  /api/mb-pro-session/version``   — remote agent version info

The session is owned by a process-singleton ``SessionManager`` in the chat
infrastructure layer; the chat turn (``query::mb_pro`` hint →
``SessionQueryServiceAdapter``) reuses that same live session, so these routes
only manage lifecycle — they do NOT carry conversation messages (those flow
through the normal chat WS/SSE).

Layering (import-linter ``interfaces-stays-thin``): this module must NOT import
``qai.chat.infrastructure``. The session-lifecycle facade
(:class:`~apps.api._mb_pro_session_bridge.MbProSessionController`) is composed at
the apps/api composition root and injected onto ``container.mb_pro_session``;
these handlers consume it by duck-typing — exactly how the CEBot query-services
route reads ``container`` without reaching into infrastructure.

Lifecycle is **user-controlled** (mb-pro-integration-plan.md decision §2.9):
the user clicks 连接 / 断开 in the Pro toolbar; the chat turn never auto-creates
a session.

internal-only (four-layer defence, layer 1 — runtime gate): the controller is
``None`` on external editions (built only when ``settings.is_internal``), so
every handler short-circuits to a 404-equivalent / disabled response.

CSRF: the non-GET handlers are subject to the global double-submit-cookie
``CsrfMiddleware``; the frontend mirrors the ``qai_csrf`` cookie into the
``X-QAI-CSRF`` header on connect/disconnect (read-only GET state/version are
exempt). No extra handling is needed here — the middleware enforces it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# ---------------------------------------------------------------------------
# Schemas (module level — FastAPI body-param resolution discipline)
# ---------------------------------------------------------------------------


class ConnectRequest(BaseModel):
    """Body for ``POST /api/mb-pro-session/connect``.

    ``tab_id`` binds the session to THIS chat tab (each tab owns an independent
    MB Pro session so histories never mix and each reconnects/restores by its
    own ``session_id``). ``tab_id`` — not ``conversation_id`` — is the key
    because a brand-new chat has no conversation id until its first message,
    while the Pro toolbar's「连接」happens before that; the chat turn keys its
    session lookup off ``tab_id`` too, so the two agree. The remaining fields
    are optional: an empty body (besides ``tab_id``) connects to the
    factory-default host (``internal_config.toml [query_services.mb_pro]``).
    The Pro settings dialog overrides ``agent_url`` / ``insecure`` for a custom
    host and ``session_id`` to re-attach to this tab's existing remote session
    (history restore).

    ``conversation_id`` (optional, appended): when present, the controller
    consumes the Agent's connect-time greeting burst and persists its
    self-intro as an assistant message bound to this conversation
    (see ``PersistMbProGreetingUseCase``). The frontend ensures a conversation
    exists before calling connect on a brand-new tab so this anchor is
    available. Omitted ⇒ greeting persistence + broadcast are skipped (the
    backend still completes the session-level connect).
    """

    tab_id: str
    agent_url: str | None = None
    session_id: str | None = None
    insecure: bool | None = None
    conversation_id: str | None = None


class DisconnectRequest(BaseModel):
    """Body for ``POST /api/mb-pro-session/disconnect`` — which tab."""

    tab_id: str


class SessionStateResponse(BaseModel):
    """Snapshot of the current MB Pro session connection."""

    connected: bool
    session_id: str | None = None
    agent_url: str | None = None
    insecure: bool = False


class VersionResponse(BaseModel):
    """Remote agent version info (opaque passthrough)."""

    version: dict[str, Any]


class RefreshAccessResponse(BaseModel):
    """Result of a MB Pro access re-check."""

    authorized: bool
    ldap_error: bool


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _controller(container: "Container") -> Any | None:
    """Return the injected MB Pro session controller, or ``None`` (external)."""
    return getattr(container, "mb_pro_session", None)


def build_router(*, container: "Container") -> APIRouter:
    """Build the MB Pro session-control router (internal-only behaviour)."""
    router = APIRouter(prefix="/api/mb-pro-session", tags=["mb-pro"])

    @router.post("/connect", response_model=SessionStateResponse)
    async def connect(body: ConnectRequest, request: Request) -> SessionStateResponse:
        user = getattr(getattr(request, "state", None), "user", None) or {}
        # Access control gate: only authorized MB Pro members may connect.
        # Checked before the ctrl=None guard so unauthorized users see 403
        # rather than 404 on external/unconfigured editions.
        if not user.get("is_mb_pro_authorized", False):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "mb_pro.access_denied",
                    "ldap_error": bool(user.get("mb_pro_access_check_failed", False)),
                },
            )
        ctrl = _controller(container)
        if ctrl is None:
            raise HTTPException(status_code=404, detail="MB Pro not available")
        raw_username: str = user.get("username") or ""
        # Strip email domain if present (e.g. "alice@example.com" → "alice")
        # so the value is a valid MB Pro session_id ([A-Za-z0-9_.-]+ only).
        username: str | None = (raw_username.split("@")[0] or None) if raw_username else None
        try:
            state = await ctrl.connect(
                tab_id=body.tab_id,
                agent_url=body.agent_url,
                session_id=body.session_id,
                insecure=body.insecure,
                conversation_id=body.conversation_id,
                username=username,
            )
        except RuntimeError as exc:
            # The bridge raises a structured ``MbProProbeError`` (a
            # RuntimeError subclass carrying ``code`` + ``details``) for the
            # auto-probe pool outcomes, and a plain ``RuntimeError`` for
            # everything else (per-port connect failures, "not configured").
            #
            # We DUCK-TYPE off ``code`` rather than importing the bridge's
            # exception type — the interfaces layer must not depend on
            # ``apps.api`` (import-linter ``interfaces-stays-thin``); it already
            # consumes the controller by duck-typing for the same reason.
            #
            # Structured probe errors are returned as a machine-readable body
            # (``{code, message, details}``) so the FRONTEND renders a localized
            # (i18n) message from ``code`` — the backend ships DATA, not a
            # pre-formatted user sentence. Status is derived from the code:
            #   * ``mb_pro.pool_all_busy``     → 503 (temporary exhaustion)
            #   * ``mb_pro.pool_all_offline``  → 502 (service down)
            #   * ``mb_pro.pool_network_error``→ 502 (caller's network issue)
            #   * anything else                → 502
            code = getattr(exc, "code", "")
            if isinstance(code, str) and code.startswith("mb_pro."):
                details = getattr(exc, "details", {}) or {}
                status = 503 if code == "mb_pro.pool_all_busy" else 502
                raise HTTPException(
                    status_code=status,
                    detail={
                        "code": code,
                        "message": str(exc),
                        "details": details,
                    },
                ) from exc
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return SessionStateResponse(**state)

    @router.post("/disconnect", response_model=SessionStateResponse)
    async def disconnect(body: DisconnectRequest) -> SessionStateResponse:
        ctrl = _controller(container)
        if ctrl is None:
            raise HTTPException(status_code=404, detail="MB Pro not available")
        state = await ctrl.disconnect(tab_id=body.tab_id)
        return SessionStateResponse(**state)

    @router.get("/state", response_model=SessionStateResponse)
    async def state(tab_id: str) -> SessionStateResponse:
        ctrl = _controller(container)
        if ctrl is None:
            return SessionStateResponse(connected=False)
        return SessionStateResponse(
            **ctrl.get_state(tab_id=tab_id)
        )

    @router.get("/version", response_model=VersionResponse)
    async def version(
        agent_url: str | None = None,
        insecure: bool | None = None,
    ) -> VersionResponse:
        ctrl = _controller(container)
        if ctrl is None:
            raise HTTPException(status_code=404, detail="MB Pro not available")
        try:
            info = await ctrl.fetch_version(agent_url=agent_url, insecure=insecure)
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — surface as 502
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return VersionResponse(version=info)

    @router.post("/refresh-access", response_model=RefreshAccessResponse)
    async def refresh_access(
        request: Request, response: Response
    ) -> RefreshAccessResponse:
        """Re-check MB Pro LDAP membership and update the session cookie.

        Allows a user who was just added to the list to gain access without
        logging out. The session expiry (``exp``) is preserved — this endpoint
        does NOT extend the session lifetime.
        """
        from interfaces.http.routes.auth import _check_mb_pro_access
        from interfaces.http.middleware.auth import (
            dump_session,
            load_session_full,
            session_secret,
        )

        settings = getattr(container, "settings", None)
        if settings is None or not getattr(settings, "is_internal", False):
            raise HTTPException(status_code=404, detail="MB Pro not available")
        auth_settings = getattr(settings, "auth", None)
        if auth_settings is None:
            raise HTTPException(status_code=500, detail="Auth not configured")

        user = getattr(getattr(request, "state", None), "user", None) or {}
        # NOTE: since 2026-07-19, `_check_mb_pro_access` normalizes the
        # username internally (`.split("@")[0]`), so this pre-strip is
        # idempotent — kept for readability + defence-in-depth. Safe to
        # simplify to `user.get("username") or ""` when someone touches
        # this block next.
        username = (user.get("username") or "").split("@")[0]
        # Follow the unified outbound-TLS switch (Settings.ssl_verify): default
        # False on internal editions so self-signed corporate ceflow certs
        # pass without env-var setup. This is a request-time read so a runtime
        # toggle via /api/security/runtime-config hot-applies to the next
        # refresh call.
        _ssl_verify = bool(getattr(settings, "ssl_verify", False))
        authorized, ldap_error = await _check_mb_pro_access(
            username, ssl_verify=_ssl_verify
        )

        updated = dict(user)
        updated["is_mb_pro_authorized"] = authorized
        updated["mb_pro_access_check_failed"] = ldap_error

        # Re-issue the cookie preserving the original exp so this does not
        # accidentally extend the session lifetime.
        data_root = getattr(getattr(container, "data_paths", None), "root", None)
        secret = session_secret(auth_settings.session_secret, data_root)
        raw = request.cookies.get(auth_settings.session_cookie_name, "")
        full = load_session_full(raw, secret) if raw else {}
        original_exp = int(full.get("exp") or 0)
        remaining_ttl = max(60, original_exp - int(time.time()))

        cookie_val = dump_session(updated, remaining_ttl, secret)
        response.set_cookie(
            auth_settings.session_cookie_name,
            cookie_val,
            httponly=True,
            samesite="lax",
            secure=not getattr(auth_settings, "debug", False),
        )
        return RefreshAccessResponse(authorized=authorized, ldap_error=ldap_error)

    return router


__all__ = ["build_router"]
