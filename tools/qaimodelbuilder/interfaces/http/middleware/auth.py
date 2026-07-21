# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""FastAPI middleware: Okta OIDC session-cookie gate for the Web UI.

Sits alongside :mod:`interfaces.http.middleware.csrf` in the middleware
stack. On every non-public request:

* If ``auth.enabled`` is False → transparently pass through (no cookie
  parsing, no session lookup — the feature is a total no-op).
* Otherwise, load the HMAC-signed session cookie from
  ``auth.session_cookie_name`` and bind the decoded user dict to
  ``request.state.user``. This is a **single-user** deployment, so we
  deliberately do NOT push into any ``ContextVar`` / user-id isolator
  (see AGENTS.md — the qai-agent multi-user machinery would be dead
  weight here).
* Failure modes (missing cookie / bad signature / expired session) split
  by client type:

  - Browser navigation (``Accept`` includes ``text/html``, path does not
    begin with ``/api/`` or ``/session``) → ``303`` redirect to
    ``/auth/login?next=<original URL with query>``.
  - Everything else (SPA fetch / CLI / bot) → ``401`` JSON envelope
    ``{type, code, message, details}`` matching the same shape the CSRF
    middleware emits on a 403 (see :mod:`.csrf` for the design rationale
    on returning the envelope directly instead of raising).

Public paths (login/callback/logout/signed-out, ``/api/auth/me``, system
health/build-info/edition, OpenAPI/docs/redoc, favicon, root) and public
prefixes (assets, image files, appbuilder files, LLM ``/v1/``, channel
webhooks) are hard-coded on the class rather than made configurable —
adding one is a security decision, not a config decision.

Supporting helpers (``dump_session`` / ``load_session`` /
``session_secret`` / ``public_user`` / ``get_current_user`` /
``b64url_encode``) are exposed at module level so
:mod:`interfaces.http.routes.auth` can reuse them for the callback and
``/api/auth/me`` handlers without pulling middleware state into a
router.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config.settings import AuthSettings

__all__ = [
    "AuthMiddleware",
    "b64url_encode",
    "b64url_decode",
    "dump_session",
    "load_session",
    "get_current_user",
    "session_secret",
    "public_user",
]

logger = get_logger("qai.auth")


# ── base64url (no padding) ─────────────────────────────────────────────────
def b64url_encode(data: bytes) -> str:
    """URL-safe base64 with the trailing ``=`` padding stripped."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data: str) -> bytes:
    """Inverse of :func:`b64url_encode`; re-adds ``=`` padding as needed."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("ascii"))


# ── user projection ────────────────────────────────────────────────────────
def public_user(claims: dict[str, Any]) -> dict[str, Any]:
    """Project an id_token claim set (or a session ``user`` sub-dict) into
    the fixed shape we store in the session cookie and return over
    ``/api/auth/me``.

    Idempotent: calling this on an already-projected user dict is safe.
    """
    username = str(
        claims.get("username")
        or claims.get("preferred_username")
        or claims.get("email")
        or ""
    ).strip()
    email = str(claims.get("email") or "").strip().lower()
    if not username and email:
        username = email.split("@", 1)[0]
    if not username:
        # Okta always returns `sub`; fall back to it as an opaque handle
        # rather than reject a login on a blank preferred_username.
        username = str(claims.get("sub") or "")
    name = str(claims.get("name") or username).strip()
    display_name = str(
        claims.get("display_name") or claims.get("name") or username
    ).strip()
    return {
        "username": username,
        "email": email,
        "name": name,
        "display_name": display_name,
        "sub": str(claims.get("sub") or ""),
        "auth_source": str(claims.get("auth_source") or "okta_oidc"),
        "is_mb_pro_authorized": bool(claims.get("is_mb_pro_authorized", False)),
        "mb_pro_access_check_failed": bool(claims.get("mb_pro_access_check_failed", False)),
    }


# ── HMAC-signed session cookie ─────────────────────────────────────────────
def _sign(data: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), data.encode("ascii"), hashlib.sha256
    ).digest()
    return b64url_encode(digest)


def dump_session(user: dict[str, Any], ttl_seconds: int, secret: str) -> str:
    """Encode ``user`` into ``<payload>.<signature>`` with the given secret.

    ``ttl_seconds`` becomes ``exp``; the cookie also carries ``iat`` for
    diagnostics but ``exp`` is what gates expiry on load.
    """
    now = int(time.time())
    payload = {
        "user": public_user(user),
        "iat": now,
        "exp": now + int(ttl_seconds or 28800),
    }
    data = b64url_encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    )
    sig = _sign(data, secret)
    return f"{data}.{sig}"


def load_session(cookie: str, secret: str) -> dict[str, Any] | None:
    """Verify signature + expiry and return the ``user`` sub-dict, or
    ``None`` on any failure. Timing-safe by using
    :func:`hmac.compare_digest` on the signature comparison — see
    AGENTS.md §5 (real-state-first: identity checks never leak byte
    prefixes through comparison timing).
    """
    full = load_session_full(cookie, secret)
    if full is None:
        return None
    user = full.get("user")
    return dict(user) if isinstance(user, dict) else None


def load_session_full(cookie: str, secret: str) -> dict[str, Any] | None:
    """Like :func:`load_session` but returns the WHOLE verified payload
    (``{user, iat, exp}``) instead of just the ``user`` sub-dict, so
    callers that need ``exp`` (the ``/api/auth/me`` + ``/api/auth/renew``
    keep-alive surface) don't have to re-parse the cookie. Returns
    ``None`` on any signature / decode / expiry failure.
    """
    try:
        data, sig = cookie.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(data, secret), sig):
        return None
    try:
        payload = json.loads(b64url_decode(data).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return payload


def get_current_user(
    request: Request, cookie_name: str, secret: str
) -> dict[str, Any] | None:
    """Return the session user dict for ``request`` or ``None``. Never
    raises: the caller decides whether the missing/invalid session is a
    303 redirect or a 401 envelope.
    """
    raw = request.cookies.get(cookie_name)
    if not raw:
        return None
    return load_session(raw, secret)


# ── session-secret bootstrap ───────────────────────────────────────────────
def session_secret(configured: str, data_root: Path) -> str:
    """Return the effective HMAC secret for signing session cookies.

    Precedence:

    1. Non-blank ``configured`` value (from ``auth.session_secret``).
    2. Cached secret under ``<data_root>/auth_session_secret`` — the
       stable value across restarts so existing sessions keep working.
    3. Freshly-generated 48-byte urlsafe token, persisted to that file
       with a best-effort ``chmod 0o600`` (Windows silently drops mode
       bits; failure is not fatal).

    If persistence itself fails (unwritable directory / IO error) we
    fall back to an ephemeral secret and log a warning. Any existing
    cookies then fail signature verification on next request — the safe
    outcome (users re-login).
    """
    text = str(configured or "").strip()
    if text:
        return text
    path = data_root / "auth_session_secret"
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(48)
        path.write_text(secret, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            # Windows or restricted filesystem — mode bits are advisory.
            pass
        return secret
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "auth.session_secret persistence failed; using ephemeral key",
            error=repr(exc),
            path=str(path),
        )
        return secrets.token_urlsafe(48)


# ── middleware ─────────────────────────────────────────────────────────────
class AuthMiddleware(BaseHTTPMiddleware):
    """Session-cookie gate. See module docstring for the wire shape."""

    # World-reachable paths — anything added here bypasses authentication
    # entirely, including on 0.0.0.0 binds. Keep tight.
    _PUBLIC_EXACT: frozenset[str] = frozenset(
        {
            # Login flow itself cannot require a session.
            "/auth/login",
            "/callback",
            "/auth/logout",
            "/auth/signed-out",
            # SPA introspects the auth state without being authenticated.
            "/api/auth/me",
            # Process-level liveness / metadata (see system routes) — no
            # secrets, no data, safe to publish on the loopback interface.
            "/api/system/health",
            "/api/system/build-info",
            "/api/system/edition",
            # Auto-generated FastAPI documentation.
            "/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
            # Static assets served by the shell.
            "/favicon.ico",
            "/",
        }
    )
    _PUBLIC_PREFIXES: tuple[str, ...] = (
        "/assets/",
        "/api/images/files/",
        "/api/appbuilder/files/",
        # LLM proxy endpoints authenticate on their own credentials.
        "/v1/",
        # External channel webhooks authenticate via HMAC / signature.
        "/api/wechat/webhook",
        "/api/feishu/webhook",
    )

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: "AuthSettings",
        data_root: Path,
    ) -> None:
        super().__init__(app)
        self._settings = settings
        self._enabled: bool = bool(settings.enabled)
        self._cookie_name: str = settings.session_cookie_name
        self._data_root: Path = data_root
        # Resolve the secret ONCE at construction time. Subsequent
        # requests must never race on generating a fresh key.
        self._secret: str = session_secret(settings.session_secret, data_root)

    async def dispatch(self, request: Request, call_next):
        # Master switch — the entire feature is a no-op when disabled.
        if not self._enabled:
            return await call_next(request)

        path = request.url.path
        if self._is_public(path):
            return await call_next(request)

        user = get_current_user(request, self._cookie_name, self._secret)
        if user is not None:
            request.state.user = user
            return await call_next(request)

        # ── Missing / invalid session — differentiated by client type ─────
        #
        # UX contract (2026-07-17): **the application must always render
        # its own shell first**. A user who opens the app URL — or any
        # SPA-router deep link like ``/chat`` / ``/settings`` / any other
        # front-end path — must see the QAI ModelBuilder interface, not
        # be silently teleported to Okta / account.qualcomm.com before
        # the SPA ever loads. The latter feels to end users like the
        # shortcut is broken or the URL was hijacked ("I clicked
        # ModelBuilder, why am I on Qualcomm's login page?"). The
        # correct login flow is: the SPA shell renders → the SPA hydrates
        # ``/api/auth/me`` (which IS public — see ``_PUBLIC_EXACT``) →
        # sees ``authenticated=false`` → shows the in-app ``LoginPrompt``
        # modal so the user can *choose* to sign in.
        #
        # Regression trigger (both commits in HEAD, session-cookie-masked):
        #
        #   * ``5ee75b45`` (2026-07-12) introduced this middleware, which
        #     was written to 303-redirect any non-public HTML request to
        #     ``/auth/login``. That path serves the auth router which
        #     303s again to Okta — a full page teleport before the SPA
        #     runs a single line of JavaScript.
        #   * ``fa4ac9c`` (2026-07-15) changed the app launcher
        #     (``apps/cli/_endpoint_helper.py:605``) to open ``/chat``
        #     on startup. ``/`` is in ``_PUBLIC_EXACT`` but ``/chat`` is
        #     not, so the redirect fired on every cold start whenever
        #     the session cookie was absent / expired.
        #   * The bug went unnoticed because the 8-hour session TTL is
        #     silently slid forward on every request, so long-running
        #     users never hit an expired cookie in practice.
        #
        # Fix (2026-07-17): route missing-session BY CLIENT TYPE instead
        # of BY REDIRECT.
        #
        #   * Browser HTML navigations → pass through. The SPA shell
        #     loads normally, then the SPA's own ``LoginPrompt`` modal
        #     handles the unauthenticated state. The user always sees
        #     the QAI interface first. **No server-side 303 to Okta.**
        #   * SPA JSON fetches / CLI / bots (i.e. non-HTML tooling that
        #     already handles envelope errors) → 401 envelope so
        #     ``http.ts:parseAndInterceptApiError`` on the SPA side can
        #     surface the LoginPrompt modal from a mid-session 401 too.
        #
        # Note that ``request.state.user`` stays unset on the pass-through
        # branch. That is fine: downstream code that needs a user reads
        # it via ``get_current_user`` / the ``/api/auth/me`` route, both
        # of which already tolerate ``None``. The SPA HTML response is
        # a static bundle — it has no dependency on the request user.
        login_url = _login_url(request)
        if _wants_browser_redirect(request):
            logger.debug(
                "auth.middleware.pass_through_html",
                path=path,
                reason="spa_shell_renders_first_then_login_prompt_modal",
            )
            return await call_next(request)

        logger.debug(
            "auth.middleware.reject_api",
            path=path,
            login_url=login_url,
        )
        # Envelope mirrors the CsrfMiddleware 403 shape (see .csrf module
        # docstring on why we bypass FastAPI's exception-handler chain).
        return JSONResponse(
            status_code=401,
            content={
                "type": "UnauthorizedError",
                "code": "auth.required",
                "message": "Authentication required",
                "details": {"login_url": login_url},
            },
        )

    @classmethod
    def _is_public(cls, path: str) -> bool:
        if path in cls._PUBLIC_EXACT:
            return True
        return any(path.startswith(p) for p in cls._PUBLIC_PREFIXES)


# ── request-shape helpers (module-private) ─────────────────────────────────
def _wants_browser_redirect(request: Request) -> bool:
    """True when the request looks like a browser navigation.

    Two conditions: the path is neither an ``/api/`` route nor a
    ``/session`` route (both are consumed by the SPA / CLI as JSON), AND
    the ``Accept`` header advertises HTML (``text/html``, ``*/*``, or is
    absent — some fetch-family libs omit it).
    """
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/session"):
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "*/*" in accept or not accept


def _login_url(request: Request) -> str:
    """Build ``/auth/login?next=<original URL, query preserved>``."""
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return "/auth/login?" + urlencode({"next": next_path})
