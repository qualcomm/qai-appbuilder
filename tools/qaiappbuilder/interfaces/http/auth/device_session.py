# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Silent device-flow session bootstrap for headless / remote servers.

RFC 8628 (Device Authorization Grant) is the SSO path for a server with no
local browser: the operator runs ``qai auth device-login`` on the SSH
terminal, completes authorization on any other device, and the resulting
Okta ``refresh_token`` is persisted in the :class:`SecretStore` under
``("qai.auth.okta", "refresh_token")``.

This module is the *consumer* of that stored credential. When
``auth.enabled`` **and** ``auth.device_flow_enabled`` are both True and a
request arrives with **no valid session cookie**, the auth middleware (and
the public ``/api/auth/me`` introspection route) call
:func:`try_device_session` to mint a session from the stored refresh_token:

  1. Read ``refresh_token`` from the SecretStore. Absent → return ``None``
     immediately (no network, no cooldown — the operator may run
     ``device-login`` right after and we must pick it up on the next request).
  2. POST ``{issuer}/v1/token`` with ``grant_type=refresh_token``
     (:func:`~interfaces.http.auth.device_flow.refresh_access_token`).
  3. Verify the returned ``id_token`` (JWKS signature + iss/aud/exp) via
     :func:`~interfaces.http.auth.jwt.verify_id_token`.
  4. Apply the email-domain allow-list (same check as the login gate).
  5. Populate MB Pro LDAP membership + trade the fresh ``id_token`` for a
     QAI Service JWT — the exact post-verification steps ``/callback`` runs,
     so a device-flow login is indistinguishable from a desktop login.
  6. Return the verified ``user`` dict; the caller issues the HMAC session
     cookie. If Okta rotated the refresh_token, the new one is persisted back
     to the SecretStore so the next bootstrap uses it.

Safety rails
------------
* **Failure cooldown** — a failed refresh/verify sets a process-wide
  :data:`_FAILURE_COOLDOWN` (60 s) marker so a revoked / expired token cannot
  storm Okta on every request. The "no token yet" path does NOT set the
  cooldown (it is not a failure).
* **Short success cache** — a successful bootstrap is cached for
  :data:`_SUCCESS_CACHE_TTL` (2 s), keyed by the refresh_token hash, so two
  concurrent cookie-less requests (two browser tabs opened at once) do not
  both hit Okta. The TTL is far shorter than the session lifetime, so a
  re-login with a *different* identity is never served the stale user.
* **Serialization** — an :class:`asyncio.Lock` serializes the Okta round-trip
  so at most one refresh is in flight; a waiter re-checks the cache after
  acquiring the lock and returns the cached result instead of re-calling.

The module is framework-free (no FastAPI imports) and shares the event loop
with the middleware / routes (single-process uvicorn), so the module-level
lock and cache are safe.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING, Any

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config.settings import AuthSettings
    from qai.platform.persistence.secrets import SecretStore

logger = get_logger("qai.auth.device_session")

__all__ = ["try_device_session"]

# SecretStore location the CLI writes the refresh_token to
# (``apps/cli/commands/auth.py``). Single-user deployment → one token.
_SERVICE = "qai.auth.okta"
_KEY_REFRESH = "refresh_token"

#: After a FAILED bootstrap, do not retry for this long (wall clock, seconds).
#: Bounds the Okta call rate when the stored token is revoked / expired.
_FAILURE_COOLDOWN = 60.0
#: After a SUCCESSFUL bootstrap, serve the cached user for this long (seconds).
#: Long enough to dedupe concurrent cookie-less requests, far shorter than the
#: 8 h session TTL so a re-login is never masked by a stale identity.
_SUCCESS_CACHE_TTL = 2.0

# ── process-wide bootstrap state (single event loop) ────────────────────────
# ``asyncio.Lock()`` is safe to create at module import in Python 3.10+ (it
# binds to the running loop lazily on first use); the middleware and routes
# share the same loop, so one lock serializes every bootstrap in the process.
_lock: asyncio.Lock = asyncio.Lock()
# ``(monotonic, refresh_token_hash, user)`` of the last successful bootstrap.
_recent_success: tuple[float, str, dict[str, Any]] | None = None
# ``time.monotonic()`` of the last FAILED bootstrap (0.0 = never / cleared).
_last_failed_at: float = 0.0


def _rt_hash(refresh_token: str) -> str:
    """Cheap fingerprint of the refresh_token for cache-keying.

    Never the raw token (it must not ride along in a cache key that could be
    logged); a truncated SHA-256 is enough to distinguish "same token" from
    "operator re-logged-in with a different identity".
    """
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()[:16]


async def try_device_session(
    *,
    settings: "AuthSettings",
    secret_store: "SecretStore | None",
    ssl_verify: bool,
) -> dict[str, Any] | None:
    """Attempt to mint a session from the stored device-flow refresh_token.

    Returns the verified ``user`` dict (ready for :func:`dump_session`) on
    success, or ``None`` when there is nothing to bootstrap from (no store,
    no token, in cooldown) or the Okta round-trip / verification failed.

    Never raises: a bootstrap failure must degrade to "not authenticated"
    (the caller falls through to its normal 401 / login-prompt path), never
    to a 500.
    """
    global _recent_success, _last_failed_at

    # Fast path: nothing to do, no network. A missing store / token is NOT a
    # failure — do not set the cooldown (the operator may log in right after).
    if secret_store is None:
        return None
    try:
        refresh_token = secret_store.get(_SERVICE, _KEY_REFRESH)
    except Exception:  # noqa: BLE001 — absent / unreadable → "not logged in"
        return None
    if not refresh_token:
        return None

    token_hash = _rt_hash(refresh_token)
    now = time.monotonic()

    # Cache hit (concurrent cookie-less request with the same token).
    rs = _recent_success
    if rs is not None and rs[1] == token_hash and (now - rs[0]) < _SUCCESS_CACHE_TTL:
        return rs[2]
    # In cooldown after a failure — skip without hitting Okta.
    if (now - _last_failed_at) < _FAILURE_COOLDOWN:
        return None

    async with _lock:
        # Re-check both guards under the lock: a sibling coroutine may have
        # just succeeded (cache) or just failed (cooldown) while we waited.
        now = time.monotonic()
        rs = _recent_success
        if rs is not None and rs[1] == token_hash and (now - rs[0]) < _SUCCESS_CACHE_TTL:
            return rs[2]
        if (now - _last_failed_at) < _FAILURE_COOLDOWN:
            return None

        # ── Okta refresh + id_token verification ─────────────────────────
        try:
            from interfaces.http.auth.device_flow import refresh_access_token
            from interfaces.http.auth.jwt import verify_id_token
            from interfaces.http.middleware.auth import public_user

            bundle = await refresh_access_token(
                client_id=settings.client_id,
                issuer=settings.issuer,
                refresh_token=refresh_token,
                ssl_verify=ssl_verify,
            )
            id_token = str(bundle.get("id_token") or "")
            if not id_token:
                raise RuntimeError("Okta refresh response carried no id_token")
            claims = verify_id_token(
                id_token,
                client_id=settings.client_id,
                issuer=settings.issuer,
                ssl_verify=ssl_verify,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to "not authenticated"
            logger.warning(
                "auth.device_session.refresh_failed",
                error=repr(exc),
            )
            _last_failed_at = time.monotonic()
            return None

        user = public_user(claims)

        # Email-domain allow-list (same gate as /callback / the middleware).
        if settings.allowed_email_domains:
            from interfaces.http.routes.auth import _check_email_domain

            reason = _check_email_domain(
                str(user.get("email") or ""), settings.allowed_email_domains
            )
            if reason is not None:
                logger.warning(
                    "auth.device_session.domain_denied",
                    email=user.get("email"),
                    reason=reason,
                )
                _last_failed_at = time.monotonic()
                return None

        # MB Pro LDAP membership + QAI Service JWT exchange — the exact
        # post-verification steps /callback runs, so a device-flow login is
        # indistinguishable from a desktop login. Both are best-effort and
        # never raise (see their docstrings).
        try:
            from interfaces.http.routes.auth import (
                _check_mb_pro_access,
                _exchange_qai_service_token,
            )

            authorized, ldap_error = await _check_mb_pro_access(
                str(user.get("username") or ""), ssl_verify=ssl_verify
            )
            user["is_mb_pro_authorized"] = authorized
            user["mb_pro_access_check_failed"] = ldap_error
            await _exchange_qai_service_token(id_token)
        except Exception as exc:  # noqa: BLE001 — login must never break
            logger.warning(
                "auth.device_session.enrich_failed",
                error=repr(exc),
            )

        # Persist a rotated refresh_token back so the NEXT bootstrap uses it.
        # Okta rotates refresh tokens only when the client is configured to;
        # when it does and we kept the old one, the next bootstrap would fail.
        new_refresh = str(bundle.get("refresh_token") or "")
        if new_refresh and new_refresh != refresh_token:
            try:
                secret_store.set(_SERVICE, _KEY_REFRESH, new_refresh)
            except Exception as exc:  # noqa: BLE001 — non-fatal
                logger.warning(
                    "auth.device_session.refresh_token_persist_failed",
                    error=repr(exc),
                )

        _recent_success = (time.monotonic(), token_hash, user)
        logger.info(
            "auth.device_session.ok",
            username=user.get("username"),
            email=user.get("email"),
        )
        return user
