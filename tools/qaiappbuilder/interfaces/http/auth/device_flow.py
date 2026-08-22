"""OAuth 2.0 Device Authorization Grant (RFC 8628) — headless SSO.

All HTTP calls use httpx with the same ``ssl_verify`` convention as
``_exchange_code`` (routes/auth.py) and ``_fetch_jwks`` (auth/jwt.py):
the caller passes the resolved ``AuthSettings.ssl_verify`` value so the
corporate-CA / self-signed-cert policy is consistent across every Okta
outbound call.

The module is framework-free (no FastAPI imports) and can be called from
the CLI (``asyncio.run``), a background task, or any future entrypoint.

Public API
----------
``device_authorize``     — POST /v1/device/authorize  (steps 1–2)
``poll_for_token``       — POST /v1/token loop         (steps 4–5)
``refresh_access_token`` — POST /v1/token refresh      (step 7, silent renewal)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from qai.platform.logging import get_logger

logger = get_logger("qai.auth.device_flow")

# RFC 8628 §3.4 grant-type URI
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


# ---------------------------------------------------------------------------
# Step (1)(2): device/authorize
# ---------------------------------------------------------------------------


async def device_authorize(
    *,
    client_id: str,
    issuer: str,
    scopes: tuple[str, ...],
    ssl_verify: bool,
    timeout: int = 15,
) -> dict[str, Any]:
    """POST ``{issuer}/v1/device/authorize`` and return the Okta response.

    Successful response fields (RFC 8628 §3.2):
      ``device_code``              — opaque long token; machine-only
      ``user_code``                — short human-readable code (e.g. ``ABCD-EFGH``)
      ``verification_uri``         — URL user opens to enter ``user_code``
      ``verification_uri_complete``— URL with ``user_code`` pre-filled (QR-code friendly)
      ``expires_in``               — seconds until ``user_code`` expires
      ``interval``                 — minimum polling interval in seconds

    Raises ``RuntimeError`` on HTTP 4xx / 5xx (Okta error details included).
    """
    endpoint = f"{issuer.rstrip('/')}/v1/device/authorize"
    form: dict[str, str] = {
        "client_id": client_id,
        "scope": " ".join(scopes),
    }
    async with httpx.AsyncClient(verify=ssl_verify, timeout=timeout) as client:
        resp = await client.post(
            endpoint, data=form, headers={"Accept": "application/json"}
        )
    if resp.status_code >= 400:
        _raise_okta_error("device_authorize", resp)
    body = resp.json()
    logger.debug(
        "device_flow.authorize_ok",
        user_code=body.get("user_code"),
        expires_in=body.get("expires_in"),
    )
    return body


# ---------------------------------------------------------------------------
# Steps (4)(5): poll /v1/token
# ---------------------------------------------------------------------------


async def poll_for_token(
    *,
    client_id: str,
    issuer: str,
    device_code: str,
    interval: int,
    ssl_verify: bool,
    max_seconds: int,
) -> dict[str, Any]:
    """Poll ``{issuer}/v1/token`` until the user completes authorization.

    RFC 8628 §3.5 terminal / non-terminal error handling:

    =====================  ===================================================
    Okta ``error``         Action
    =====================  ===================================================
    ``authorization_pending``  Non-terminal — keep polling at current interval
    ``slow_down``              Non-terminal — ``interval += 5``, keep polling
    ``access_denied``          Terminal — user clicked Deny; raise RuntimeError
    ``expired_token``          Terminal — ``user_code`` TTL passed; raise
    HTTP 200                   Success — return token dict
    anything else              Terminal — raise RuntimeError with Okta detail
    =====================  ===================================================

    ``max_seconds`` is a hard wall-clock cap (``AuthSettings.device_poll_max_seconds``).
    The function raises ``RuntimeError`` once that deadline passes.
    """
    endpoint = f"{issuer.rstrip('/')}/v1/token"
    form: dict[str, str] = {
        "grant_type": _DEVICE_GRANT,
        "client_id": client_id,
        "device_code": device_code,
    }
    deadline = time.monotonic() + max_seconds
    poll_interval = max(interval, 1)

    async with httpx.AsyncClient(verify=ssl_verify, timeout=15) as client:
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "device-flow timeout: user did not complete authorization "
                    f"within {max_seconds}s"
                )

            await asyncio.sleep(poll_interval)

            resp = await client.post(
                endpoint, data=form, headers={"Accept": "application/json"}
            )

            if resp.status_code == 200:
                body = resp.json()
                if "access_token" not in body:
                    raise RuntimeError(
                        "Okta returned 200 but response is missing access_token"
                    )
                logger.info("device_flow.token_granted")
                return body

            # Parse the Okta error field from the 4xx body
            try:
                err_body = resp.json()
                err = err_body.get("error", "")
            except Exception:  # noqa: BLE001
                err = ""

            if err == "authorization_pending":
                logger.debug("device_flow.pending", interval=poll_interval)
                continue
            elif err == "slow_down":
                poll_interval += 5
                logger.debug("device_flow.slow_down", new_interval=poll_interval)
                continue
            elif err == "access_denied":
                raise RuntimeError(
                    "device-flow: user denied authorization"
                )
            elif err == "expired_token":
                raise RuntimeError(
                    "device-flow: user_code has expired — please run "
                    "`qai auth device-login` again to start a new session"
                )
            else:
                _raise_okta_error("poll_for_token", resp)


# ---------------------------------------------------------------------------
# Step (7): silent refresh
# ---------------------------------------------------------------------------


async def refresh_access_token(
    *,
    client_id: str,
    issuer: str,
    refresh_token: str,
    ssl_verify: bool,
    timeout: int = 15,
) -> dict[str, Any]:
    """Exchange a ``refresh_token`` for a fresh set of tokens.

    POST ``{issuer}/v1/token`` with ``grant_type=refresh_token``.  Okta
    returns a new ``{access_token, id_token, expires_in, ...}``.

    Raises ``RuntimeError`` when Okta rejects the token (revoked, expired,
    or device-flow grant type not enabled on the authorization server).
    """
    endpoint = f"{issuer.rstrip('/')}/v1/token"
    form: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    async with httpx.AsyncClient(verify=ssl_verify, timeout=timeout) as client:
        resp = await client.post(
            endpoint, data=form, headers={"Accept": "application/json"}
        )
    if resp.status_code >= 400:
        _raise_okta_error("refresh_access_token", resp)
    logger.debug("device_flow.refresh_ok")
    return resp.json()


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _raise_okta_error(ctx: str, resp: httpx.Response) -> None:
    """Parse Okta error body and raise a descriptive RuntimeError."""
    try:
        err = resp.json()
        detail = f"{err.get('error', '?')}: {err.get('error_description', '')}"
    except Exception:  # noqa: BLE001
        detail = resp.text[:300]
    raise RuntimeError(f"Okta {ctx} failed ({resp.status_code}): {detail}")
