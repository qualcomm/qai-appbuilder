# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Okta OIDC login / callback / logout / me routes.

Mounted under no prefix (the paths are absolute — ``/auth/login``,
``/callback``, ``/auth/logout``, ``/auth/signed-out``, ``/api/auth/me``)
because ``/callback`` is fixed by the URI registered with Okta and
``/api/auth/me`` is the SPA's stable auth-introspection endpoint.

Wire shape (RFC 8252 loopback OAuth 2.0 Authorization Code + PKCE + optional
``private_key_jwt`` client authentication):

1. ``GET /auth/login`` mints a fresh PKCE ``code_verifier`` + ``state`` and
   303-redirects to ``{issuer}/v1/authorize`` with the ``code_challenge``.
2. Okta authenticates the user and 303-redirects back to
   ``http://localhost:<server.port><redirect_path>`` (typically
   ``/callback``) with ``?code=&state=``.
3. ``GET /callback`` POSTs the code to ``{issuer}/v1/token`` (form-encoded
   — Okta rejects JSON here), receives ``{access_token, id_token, ...}``,
   verifies the ``id_token`` JWKS signature via :mod:`interfaces.http.auth.jwt`,
   discards the Okta access / refresh tokens (this is a login-only
   integration; the HMAC-signed session cookie carries the full state),
   and 303-redirects to ``next``.

Pending ``state`` entries live in a module-level dict — single-process
uvicorn, small (bounded by ``state_ttl_seconds``), a restart safely
invalidates in-flight logins. ``_cleanup_states()`` runs at every
``/auth/login`` so expired entries never leak.

The redirect_uri is constructed **explicitly** from ``server.port`` +
``redirect_path`` rather than ``request.url_for()``: Okta strict-matches
the whole URI against the registered value, and ``request.url_for()``
would echo back whatever host the browser used (``127.0.0.1`` vs
``localhost``), which is a mismatch. See ``qai-agent/server/auth.py`` L148.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from interfaces.http.auth.jwt import build_client_assertion, verify_id_token
from interfaces.http.middleware.auth import (
    b64url_encode,
    dump_session,
    get_current_user,
    load_session_full,
    public_user,
    session_secret,
)
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from qai.platform.config.settings import AuthSettings

__all__ = ["build_router"]

logger = get_logger("qai.auth")


# ── In-memory PKCE state: state → {code_verifier, redirect_uri, next,
#    created_at}. Entries expire per ``auth.state_ttl_seconds``. Single
#    process uvicorn — no multi-worker sharing to worry about.
_PENDING_STATES: dict[str, dict[str, Any]] = {}

# ── MB Pro LDAP access check ──────────────────────────────────────────────────

_LDAP_TIMEOUT = 3.0


def _resolve_ldap_group() -> str:
    """Return the MB Pro LDAP membership group identifier, or ``""`` if absent.

    Like :func:`_resolve_ldap_validate_url`, the group name is an internal-only
    value: it lives in the external-excluded ``qai.platform.edition`` package
    (``internal_config.toml [mb_pro] ldap_group``) so the open-source artifact
    carries no internal distribution-list name. On external editions the import
    fails and this returns ``""``; the access check never reaches the point of
    using it because :func:`_resolve_ldap_validate_url` already returned ``""``
    and short-circuited the check.
    """
    try:
        from qai.platform.edition import get_mb_pro_ldap_group
    except ImportError:
        return ""
    return get_mb_pro_ldap_group()


def _resolve_ldap_validate_url() -> str:
    """Return the MB Pro LDAP validate endpoint URL, or ``""`` if unavailable.

    MB Pro is an internal-only capability; its LDAP validate endpoint lives on
    the corporate network (ceflow) and its URL must never ship in an external
    artifact. The value therefore lives in the internal-only
    ``qai.platform.edition`` package (``internal_config.toml [mb_pro]
    ldap_validate_url``), which is physically excluded from external builds.

    On external editions the ``edition`` package is absent, so the import fails
    and this returns ``""``; ``_check_mb_pro_access`` then short-circuits to
    ``(False, False)`` — the external login flow is unaffected and no MB Pro
    access is granted. On internal editions the URL is read from the TOML.
    """
    try:
        from qai.platform.edition import get_mb_pro_ldap_url
    except ImportError:
        return ""
    return get_mb_pro_ldap_url()


async def _check_mb_pro_access(
    username: str, *, ssl_verify: bool = True
) -> tuple[bool, bool]:
    """Check if ``username`` is a member of the MB Pro access group.

    Calls the internal LDAP validate endpoint, which returns the full member
    list. We check whether ``username`` (email-domain-stripped, e.g.
    ``"alice"``) appears in ``members``.

    The endpoint URL is resolved via :func:`_resolve_ldap_validate_url`. On the
    external edition it resolves to ``""`` (MB Pro is internal-only) and this
    function short-circuits to ``(authorized=False, ldap_error=False)`` — a
    graceful "not authorized, not an error" so external login succeeds and MB
    Pro simply stays unavailable.

    **Username normalization** (2026-07-19): the LDAP endpoint returns
    members as SHORT usernames (``"alice"``, ``"bob"``, …), never full
    email addresses. Callers may pass either form:
      • Okta OIDC ``preferred_username`` = ``"alice@example.com"`` →
        login callback path
      • Session-cookie ``username`` already stripped by caller →
        ``mb_pro_session.py`` refresh path
    We normalize inside this function so both call sites agree without each
    having to remember to ``.split("@")[0]``. The prior bug: the login
    callback compared ``"alice@example.com" in ["alice", ...]``
    which is always False, so every user was denied at login even when
    the LDAP endpoint returned a valid membership. See release notes 2026-07-19.

    Returns ``(authorized, ldap_error)``. On any failure (timeout, non-200,
    malformed response, ``valid=false``), returns ``(False, True)`` so the
    caller surfaces a "service unavailable" message rather than silently
    denying access.

    ``ssl_verify`` is the effective unified outbound-TLS switch
    (``Settings.ssl_verify``, edition-derived default: False for internal
    dev / self-signed corporate gateways, True for packaged external
    release). Callers pass the resolved global value (login callback:
    ``_resolve_ssl_verify()``; refresh route: ``settings.ssl_verify``) so
    corporate self-signed ceflow certs Just Work on internal editions
    without additional env-var setup.

    Logs an ``auth.mb_pro.ldap_ok`` debug event on success and a warning
    event on each failure branch (non-200, invalid response, exception).
    The warning branches are intentionally verbose to aid diagnosis of
    LDAP/network issues without requiring debug-level logging in production.
    """
    # MB Pro is internal-only. On external editions the LDAP endpoint URL is
    # absent (the qai.platform.edition package ships only in internal builds),
    # so resolve to "" and short-circuit to "not authorized, not an error":
    # external login proceeds normally and MB Pro stays unavailable.
    ldap_validate_url = _resolve_ldap_validate_url()
    if not ldap_validate_url:
        return False, False

    # Normalize to the short LDAP handle. .split("@")[0] on a string with no
    # "@" is a no-op, so this is idempotent for callers who already strip.
    ldap_username = (username or "").split("@")[0]

    _verify: bool = bool(ssl_verify)
    try:
        async with httpx.AsyncClient(
            timeout=_LDAP_TIMEOUT, trust_env=False, verify=_verify
        ) as client:
            r = await client.post(
                ldap_validate_url,
                json={"identifier": _resolve_ldap_group()},
            )
        if r.status_code != 200:
            logger.warning(
                "auth.mb_pro.ldap_non_200",
                username=ldap_username,
                username_raw=username,
                status_code=r.status_code,
                url=ldap_validate_url,
                body_preview=r.text[:200] if r.text else "",
            )
            return False, True
        data = r.json()
        if not data.get("valid"):
            logger.warning(
                "auth.mb_pro.ldap_invalid_response",
                username=ldap_username,
                username_raw=username,
                valid_field=data.get("valid"),
                keys=list(data.keys()) if isinstance(data, dict) else None,
                body_preview=str(data)[:200],
            )
            return False, True
        members: list[str] = data.get("members") or []
        authorized = ldap_username in members
        logger.debug(
            "auth.mb_pro.ldap_ok",
            username=ldap_username,
            username_raw=username,
            authorized=authorized,
            member_count=len(members),
        )
        return authorized, False
    except Exception as exc:  # noqa: BLE001 — any transport failure → ldap_error
        logger.warning(
            "auth.mb_pro.ldap_exception",
            username=ldap_username,
            username_raw=username,
            exc_type=type(exc).__name__,
            exc_msg=str(exc)[:300],
            url=ldap_validate_url,
        )
        return False, True


def build_router(
    *,
    settings: "AuthSettings",
    server_port: int,
    data_root: Path,
    ssl_verify: bool | None = None,
    ssl_verify_provider: "Callable[[], bool] | None" = None,
) -> APIRouter:
    """Build the router bound to the given ``AuthSettings`` snapshot.

    ``server_port`` is passed explicitly (not read from ``settings``) so
    the redirect_uri stays in sync with the actual bound port on
    ``server.port``.

    ``ssl_verify`` is the unified outbound-TLS switch (top-level
    ``Settings.ssl_verify``, edition-derived default); it governs the
    Okta token-exchange + id_token JWKS fetches so auth follows the same
    TLS-verification policy as every other outbound client. When omitted
    the legacy per-``AuthSettings`` value is used (test / standalone calls).

    ``ssl_verify_provider`` (``Callable[[], bool] | None``) is the LIVE global
    Settings.ssl_verify provider. When present it takes precedence and is read
    at REQUEST time (inside each handler, per Okta exchange / JWKS fetch) so a
    runtime SSL toggle hot-applies — the router itself is built once at
    ``main.py`` startup, so a build-time-frozen bool would never see the toggle.
    Okta IS included in the global toggle (user decision). ``AuthSettings
    .ssl_verify`` remains the standalone/test fallback.
    """

    def _resolve_ssl_verify() -> bool:
        # LIVE read (request time): provider > explicit build-time bool >
        # per-AuthSettings fallback. This is why auth's outbound clients are
        # built per-request inside the handlers — so this resolves fresh.
        if ssl_verify_provider is not None:
            return bool(ssl_verify_provider())
        return settings.ssl_verify if ssl_verify is None else ssl_verify

    router = APIRouter(tags=["auth"])
    # Resolve the effective session secret ONCE per router build — same
    # value the middleware uses, kept in sync via ``session_secret``'s
    # file-backed cache under ``data_root``.
    secret = session_secret(settings.session_secret, data_root)

    @router.get("/auth/login")
    async def auth_login(request: Request) -> RedirectResponse:
        next_path = _safe_next(request.query_params.get("next", "/"))

        # ``enabled=False`` disables the entire gate; hitting /auth/login
        # in that mode short-circuits to ``next`` so a stale SPA link
        # doesn't dead-end.
        if not settings.enabled:
            return RedirectResponse(next_path, status_code=303)

        # Already signed in → skip Okta, bounce straight to ``next``.
        if (
            get_current_user(request, settings.session_cookie_name, secret)
            is not None
        ):
            return RedirectResponse(next_path, status_code=303)

        _cleanup_states(settings.state_ttl_seconds)

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)

        # Okta strict-matches the entire URI, incl. host. See module docstring.
        callback_url = f"http://localhost:{server_port}{settings.redirect_path}"

        _PENDING_STATES[state] = {
            "created_at": time.monotonic(),
            "code_verifier": verifier,
            "redirect_uri": callback_url,
            "next": next_path,
        }

        challenge = b64url_encode(
            hashlib.sha256(verifier.encode("utf-8")).digest()
        )
        params = {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": callback_url,
            "scope": " ".join(settings.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = (
            f"{settings.issuer.rstrip('/')}/v1/authorize?{urlencode(params)}"
        )
        # ``state`` and ``code_challenge`` are a nonce and a hash — not
        # secrets. Logging the full URL is intentional: it is the exact
        # value that goes to Okta and is the artifact IT needs when
        # diagnosing a login failure.
        logger.info(
            "auth.login.redirect",
            issuer=settings.issuer,
            client_id=settings.client_id,
            redirect_uri=callback_url,
            scope=" ".join(settings.scopes),
            authorize_url=authorize_url,
        )
        return RedirectResponse(authorize_url, status_code=303)

    @router.get("/callback")
    async def auth_callback(request: Request) -> Any:
        if not settings.enabled:
            return RedirectResponse("/", status_code=303)

        qp = dict(request.query_params)
        # Mask the one-time authorization code — leaking it into logs is
        # a real bleed of a single-use bearer secret.
        qp_safe = {
            k: (v[:6] + "…(masked)" if k == "code" and v else v)
            for k, v in qp.items()
        }
        logger.info("auth.callback.query", params=qp_safe)

        error = request.query_params.get("error")
        if error:
            description = request.query_params.get("error_description", "")
            error_uri = request.query_params.get("error_uri", "")
            logger.warning(
                "auth.callback.okta_error",
                error=error,
                description=description,
                error_uri=error_uri,
            )
            return PlainTextResponse(
                f"Okta returned error: {error} - {description}",
                status_code=400,
            )

        code = str(request.query_params.get("code") or "").strip()
        state = str(request.query_params.get("state") or "").strip()
        if not code or not state:
            return PlainTextResponse(
                "Missing auth code or state.", status_code=400
            )

        record = _PENDING_STATES.pop(state, None)
        if record is None or _state_expired(record, settings.state_ttl_seconds):
            logger.warning(
                "auth.callback.state_expired_or_unknown",
                had_record=record is not None,
            )
            return PlainTextResponse(
                "Login state expired or unknown. Please try again.",
                status_code=400,
            )
        logger.debug("auth.callback.state_matched", state=state)

        try:
            bundle = await _exchange_code(
                settings,
                data_root=data_root,
                code=code,
                code_verifier=str(record["code_verifier"]),
                redirect_uri=str(record["redirect_uri"]),
                ssl_verify=_resolve_ssl_verify(),
            )
        except FileNotFoundError as exc:
            # A missing .pem is a config error, not a transient upstream fault.
            logger.error("auth.callback.misconfigured", error=str(exc))
            return PlainTextResponse(str(exc), status_code=500)
        except Exception as exc:  # noqa: BLE001
            logger.warning("auth.callback.token_exchange_failed", error=repr(exc))
            return PlainTextResponse(
                "Could not complete SSO login. Please try again.",
                status_code=502,
            )

        try:
            claims = verify_id_token(
                bundle["id_token"],
                client_id=settings.client_id,
                issuer=settings.issuer,
                ssl_verify=_resolve_ssl_verify(),
            )
        except (jwt.InvalidTokenError, RuntimeError, ValueError) as exc:
            logger.warning("auth.callback.id_token_invalid", error=repr(exc))
            return PlainTextResponse(
                "Identity token verification failed.", status_code=401
            )

        user = public_user(claims)
        # ``sub``, ``email``, ``iss`` are already destined for the session
        # cookie — logging them here is safe and useful for diagnosing
        # domain-allowlist rejections vs. IdP configuration drift.
        logger.info(
            "auth.callback.id_token_claims",
            sub=claims.get("sub"),
            email=claims.get("email"),
            iss=claims.get("iss"),
            idp=claims.get("idp"),
            amr=claims.get("amr"),
        )

        # Email domain allow-list — empty tuple ⇒ no restriction.
        if settings.allowed_email_domains:
            reason = _check_email_domain(
                user.get("email", ""), settings.allowed_email_domains
            )
            if reason is not None:
                logger.warning(
                    "auth.callback.access_denied",
                    email=user.get("email"),
                    reason=reason,
                )
                return PlainTextResponse(
                    f"Access denied: {reason}", status_code=403
                )

        # LOGIN-ONLY: the Okta access_token / refresh_token in ``bundle``
        # have served their purpose (proving possession of the code and
        # anchoring the id_token). Discard them — the HMAC-signed cookie
        # below is the entire session state.

        # Check MB Pro group membership once at login; result rides in the
        # session cookie so we never need to re-query LDAP on every request.
        # ssl_verify follows the unified outbound-TLS switch (edition-derived
        # default: False for internal / self-signed ceflow, True for external),
        # resolved LIVE so a runtime toggle hot-applies to the next login.
        _authorized, _ldap_error = await _check_mb_pro_access(
            user.get("username", ""), ssl_verify=_resolve_ssl_verify()
        )
        user["is_mb_pro_authorized"] = _authorized
        user["mb_pro_access_check_failed"] = _ldap_error

        cookie = dump_session(user, settings.session_ttl_seconds, secret)
        response = RedirectResponse(
            _safe_next(record.get("next", "/")), status_code=303
        )
        response.set_cookie(
            key=settings.session_cookie_name,
            value=cookie,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        logger.info(
            "auth.callback.login_ok",
            username=user.get("username"),
            email=user.get("email"),
            is_mb_pro_authorized=user.get("is_mb_pro_authorized"),
            mb_pro_access_check_failed=user.get("mb_pro_access_check_failed"),
        )
        return response

    @router.get("/auth/logout")
    async def auth_logout() -> RedirectResponse:
        # Clear the LOCAL session cookie only. We deliberately do NOT
        # call Okta's /v1/revoke or end_session: on some Okta tenants
        # revoking also nukes the browser's SSO session in a way that
        # makes the next /v1/authorize return HTTP 400. Since we persist
        # no tokens locally, dropping the cookie is a complete sign-out
        # for our app.
        response = RedirectResponse("/auth/signed-out", status_code=303)
        response.delete_cookie(
            key=settings.session_cookie_name,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @router.get("/auth/signed-out", response_class=HTMLResponse)
    async def auth_signed_out() -> HTMLResponse:
        return HTMLResponse(_SIGNED_OUT_HTML)

    @router.get("/api/auth/me")
    async def auth_me(request: Request) -> JSONResponse:
        if not settings.enabled:
            # Feature disabled ⇒ every caller is implicitly "in".
            return JSONResponse(
                {
                    "auth_enabled": False,
                    "authenticated": True,
                    "user": None,
                    "expires_at": None,
                }
            )
        raw = request.cookies.get(settings.session_cookie_name)
        payload = (
            load_session_full(raw, secret) if raw is not None else None
        )
        if payload is None:
            return JSONResponse(
                {
                    "auth_enabled": True,
                    "authenticated": False,
                    "user": None,
                    "expires_at": None,
                }
            )
        user = payload.get("user")
        return JSONResponse(
            {
                "auth_enabled": True,
                "authenticated": True,
                "user": public_user(user) if isinstance(user, dict) else None,
                "expires_at": int(payload.get("exp") or 0) or None,
            }
        )

    @router.post("/api/auth/renew")
    async def auth_renew(request: Request) -> JSONResponse:
        """Slide the session expiry forward for an active user.

        Called by the SPA keep-alive timer a few minutes before ``exp``
        so someone mid-task is never kicked out. Re-issues the HMAC cookie
        with a fresh ``exp = now + session_ttl_seconds`` on top of the
        SAME verified user identity — no Okta round-trip, no token store.
        Returns 401 (via the standard middleware envelope shape) when the
        current cookie is missing / expired / tampered, so the client can
        re-prompt login.
        """
        if not settings.enabled:
            return JSONResponse({"authenticated": True, "expires_at": None})
        raw = request.cookies.get(settings.session_cookie_name)
        payload = (
            load_session_full(raw, secret) if raw is not None else None
        )
        user = payload.get("user") if payload is not None else None
        if payload is None or not isinstance(user, dict):
            return JSONResponse(
                {
                    "type": "UnauthorizedError",
                    "code": "auth.required",
                    "message": "Session expired",
                    "authenticated": False,
                    "expires_at": None,
                },
                status_code=401,
            )
        now = int(time.time())
        new_exp = now + int(settings.session_ttl_seconds or 28800)
        cookie = dump_session(user, settings.session_ttl_seconds, secret)
        response = JSONResponse(
            {"authenticated": True, "expires_at": new_exp}
        )
        response.set_cookie(
            key=settings.session_cookie_name,
            value=cookie,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        logger.debug("auth.renew.ok", username=user.get("username"))
        return response

    return router


# ── token exchange (private) ───────────────────────────────────────────────
async def _exchange_code(
    settings: "AuthSettings",
    *,
    data_root: Path,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    ssl_verify: bool | None = None,
) -> dict[str, Any]:
    """POST ``/v1/token`` with the authorization code.

    Body is form-encoded (Okta rejects JSON here) and the ``redirect_uri``
    MUST match the one sent on ``/v1/authorize`` byte-for-byte.

    ``client_auth_method``:
      * ``"none"`` — Public Client + PKCE only; the code_verifier proves
        possession of the original code_challenge.
      * ``"private_key_jwt"`` — attach a JWT client assertion signed with
        the private key under ``private_key_path`` (see
        :func:`interfaces.http.auth.jwt.build_client_assertion`).

    Any Okta 4xx / 5xx is surfaced with its ``error`` / ``error_description``
    so operators can distinguish "wrong key" from "code expired" without
    reading a raw response body.
    """
    token_endpoint = f"{settings.issuer.rstrip('/')}/v1/token"
    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "client_id": settings.client_id,
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }

    method = (settings.client_auth_method or "none").lower()
    if method == "private_key_jwt":
        form["client_assertion_type"] = (
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        )
        form["client_assertion"] = build_client_assertion(
            client_id=settings.client_id,
            token_endpoint=token_endpoint,
            private_key_path=settings.private_key_path,
            data_root=data_root,
        )
    elif method == "none":
        # Public Client + PKCE only. Nothing to attach.
        pass
    else:
        raise RuntimeError(f"Unsupported client_auth_method: {method!r}")

    async with httpx.AsyncClient(
        verify=settings.ssl_verify if ssl_verify is None else ssl_verify,
        timeout=settings.exchange_timeout_seconds,
        follow_redirects=False,
    ) as client:
        response = await client.post(
            token_endpoint,
            data=form,
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            body_snippet = response.text[:500]
            try:
                err = response.json()
                detail = (
                    f"{err.get('error', '?')}: "
                    f"{err.get('error_description', '')}"
                )
            except Exception:  # noqa: BLE001
                detail = body_snippet
            logger.warning(
                "auth.token_exchange.http_error",
                status=response.status_code,
                method=method,
                detail=detail,
            )
            raise RuntimeError(
                f"Okta token exchange failed ({response.status_code}): {detail}"
            )
        body = response.json()

    for required in ("access_token", "id_token"):
        if required not in body:
            raise RuntimeError(f"Okta response missing {required}")
    return body


# ── state map helpers ──────────────────────────────────────────────────────
def _state_expired(record: dict[str, Any], ttl_seconds: int) -> bool:
    """True if ``record`` is older than ``ttl_seconds`` (with a 30 s floor).

    ``created_at`` comes from :func:`time.monotonic` — resistant to
    system-clock jumps that would otherwise let a state entry appear to
    live "forever" or expire immediately.
    """
    created_at = float(record.get("created_at") or 0)
    return time.monotonic() > created_at + max(30, int(ttl_seconds or 300))


def _cleanup_states(ttl_seconds: int) -> None:
    """Drop every pending state whose TTL has elapsed."""
    expired = [
        s
        for s, r in _PENDING_STATES.items()
        if _state_expired(r, ttl_seconds)
    ]
    for state in expired:
        _PENDING_STATES.pop(state, None)
    if expired:
        logger.debug("auth.state.cleanup", removed=len(expired))


# ── misc ───────────────────────────────────────────────────────────────────
def _safe_next(raw: Any) -> str:
    """Sanitise the ``?next=`` param against open-redirect vectors.

    Anything not starting with a single ``/`` (absolute URLs,
    protocol-relative ``//host``, anchors, etc.) collapses to ``/``.

    Backslashes are treated as forward-slashes for the purpose of the
    ``//`` check because some legacy browsers (older IE, and a handful
    of embedded WebView variants) normalise ``\\`` → ``/`` at URL parse
    time, which would turn a value like ``/\\evil.com`` into an effective
    ``//evil.com`` cross-host redirect. Rejecting the raw byte pattern
    keeps us safe on those clients even though modern engines do not
    perform the substitution.
    """
    text = str(raw or "/").strip() or "/"
    # Normalise ANY leading backslash into a forward slash before the
    # gate below runs — this catches ``\evil.com`` (bare backslash) and
    # ``/\evil.com`` (mixed slash) in one sweep.
    if "\\" in text:
        text = text.replace("\\", "/")
    if not text.startswith("/") or text.startswith("//"):
        return "/"
    return text


def _check_email_domain(
    email: str, allowed_domains: tuple[str, ...]
) -> str | None:
    """Return ``None`` if the email is admissible, otherwise a
    human-readable rejection reason (used verbatim in the 403 body)."""
    email_norm = str(email or "").lower().strip()
    if "@" not in email_norm:
        return "email claim is required for the domain allow-list"
    domain = email_norm.rsplit("@", 1)[-1]
    allowed = {
        d.lower().strip().lstrip("@")
        for d in allowed_domains
        if str(d).strip()
    }
    if domain not in allowed:
        return f"email domain {domain!r} is not in the allow-list"
    return None


# ── signed-out landing page ────────────────────────────────────────────────
# Palette / typography aligned with apps/api/_spa_mount.py's _DIST_MISSING_HTML:
# dark #0f1420 canvas, #e0e6f0 primary text, #8a9ab5 secondary, system-ui.
_SIGNED_OUT_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Signed out — QAI AppBuilder</title>
  <style>
    html, body { height: 100%; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      background: #0f1420;
      color: #e0e6f0;
      display: grid;
      place-items: center;
      min-height: 100vh;
    }
    main {
      width: min(440px, calc(100vw - 32px));
      background: #161c2c;
      border: 1px solid #232a3f;
      border-radius: 12px;
      padding: 32px;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.35);
      text-align: center;
    }
    .brand {
      font-size: 0.85rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: #8a9ab5;
      margin-bottom: 20px;
    }
    h1 {
      font-size: 1.35rem;
      margin: 0 0 8px;
      color: #e0e6f0;
    }
    p {
      color: #8a9ab5;
      line-height: 1.5;
      margin: 0 0 24px;
    }
    a.btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      padding: 0 22px;
      border-radius: 8px;
      background: #3b82f6;
      color: #ffffff;
      text-decoration: none;
      font-weight: 600;
      transition: background 0.15s ease;
    }
    a.btn:hover {
      background: #2563eb;
    }
  </style>
</head>
<body>
  <main>
    <div class="brand">QAI AppBuilder</div>
    <h1>You&rsquo;ve been signed out</h1>
    <p>Sign in again to continue.</p>
    <a class="btn" href="/auth/login">Sign in again</a>
  </main>
</body>
</html>"""
