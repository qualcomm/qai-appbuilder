# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""jwt.py — JWT helpers for the Okta OIDC SSO integration.

Two independent pieces:

  * `build_client_assertion()` — the JWT this app signs with its RSA private
    key to authenticate itself to Okta's `/token` endpoint (RFC 7523 §2.2
    client authentication, i.e. `private_key_jwt`). It replaces the
    client_secret that a Public Client (pure PKCE) would omit. Only used when
    `auth.client_auth_method == "private_key_jwt"`; the default Native/Public
    client (`client_auth_method == "none"`) never calls it. Loaded once and
    cached in memory (private key material never touches logs or disk again
    after boot).
  * `verify_id_token()` — the OIDC id_token Okta hands us back is a JWT signed
    by Okta's RSA key. We MUST verify its signature against the JWKS at
    `{issuer}/v1/keys`, or any attacker could hand us a forged claim set and
    log in as anyone. `PyJWKClient` from pyjwt would do this — but internally
    it uses urllib and can't honour `ssl_verify=False`, which we need when
    Okta is behind a corporate CA. So we fetch JWKS with httpx ourselves and
    hand the raw JWK dict to `jwt.PyJWK`.

Both helpers are stateless from the caller's perspective — module-level
caches (`_key_cache` and `_jwks_cache`) are pure optimisations.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import jwt
from jwt import PyJWK

logger = logging.getLogger("qai.auth.jwt")

# ── private key (this app's identity to Okta) ─────────────────────────────
_key_cache: bytes | None = None
_key_cache_path: str = ""


def _load_private_key(path: str, data_root: Path) -> bytes:
    """Load the RSA private key from disk, once.

    Path resolution: absolute → as-is; relative → under `data_root`.
    `~` is expanded. Missing file raises with an operator-friendly message.
    """
    global _key_cache, _key_cache_path
    if _key_cache is not None and _key_cache_path == path:
        return _key_cache

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = data_root / p
    if not p.is_file():
        raise FileNotFoundError(
            f"Okta SSO private key not found: {p}\n"
            f"Copy the .pem issued by IT into that location, or set "
            f"`auth.private_key_path` in config.json to point at it."
        )
    _key_cache = p.read_bytes()
    _key_cache_path = path
    return _key_cache


def build_client_assertion(
    client_id: str,
    token_endpoint: str,
    private_key_path: str,
    data_root: Path,
    lifetime_seconds: int = 300,
) -> str:
    """Sign a `private_key_jwt` client assertion for Okta's /token endpoint.

    See RFC 7523 §2.2 + Okta's OIDC docs. `aud` MUST be the token endpoint URL
    (Okta rejects the assertion if it points anywhere else, including the
    issuer root). `jti` guards against replay across a short window.
    """
    now = int(time.time())
    return jwt.encode(
        {
            "iss": client_id,
            "sub": client_id,
            "aud": token_endpoint,
            "iat": now,
            "exp": now + max(60, int(lifetime_seconds)),
            "jti": str(uuid.uuid4()),
        },
        _load_private_key(private_key_path, data_root),
        algorithm="RS256",
    )


# ── id_token verification (Okta's identity to us) ─────────────────────────
_jwks_cache: dict[str, PyJWK] = {}   # kid → PyJWK
_jwks_cache_issuer: str = ""
_jwks_fetched_at: float = 0.0
_JWKS_TTL_SECONDS = 24 * 3600


def _fetch_jwks(issuer: str, ssl_verify: bool) -> dict[str, PyJWK]:
    """Fetch and index JWKS from `{issuer}/v1/keys` by `kid`.

    Cached for 24h — Okta rotates keys rarely and always publishes the new one
    ahead of using it, so a stale cache is safe for that window. If a token
    arrives with a kid we've never seen, the caller can force a re-fetch.
    """
    global _jwks_cache, _jwks_cache_issuer, _jwks_fetched_at
    jwks_uri = f"{issuer.rstrip('/')}/v1/keys"
    # ``ssl_verify`` is resolved LIVE by the auth router per request
    # (build_router._resolve_ssl_verify → global Settings.ssl_verify provider),
    # so this JWKS client honours a runtime SSL toggle at fetch time.
    with httpx.Client(verify=ssl_verify, timeout=10.0) as client:
        response = client.get(jwks_uri)
        response.raise_for_status()
        body: dict[str, Any] = response.json()

    keys_by_kid: dict[str, PyJWK] = {}
    for jwk_dict in body.get("keys", []):
        kid = jwk_dict.get("kid")
        if not kid:
            continue
        try:
            keys_by_kid[kid] = PyJWK(jwk_dict)
        except Exception as exc:
            logger.warning("Skipping unparseable JWK kid=%s: %r", kid, exc)

    if not keys_by_kid:
        raise RuntimeError(f"JWKS at {jwks_uri} contained no usable keys")

    _jwks_cache = keys_by_kid
    _jwks_cache_issuer = issuer
    _jwks_fetched_at = time.time()
    return keys_by_kid


def _get_signing_key(kid: str, issuer: str, ssl_verify: bool) -> PyJWK:
    """Return the JWK for `kid`, refreshing the cache once if it's a miss.

    We refresh at most once per call so a token forged with an unknown kid
    can't drive unbounded network traffic.
    """
    stale = (
        _jwks_cache_issuer != issuer
        or (time.time() - _jwks_fetched_at) > _JWKS_TTL_SECONDS
    )
    if not stale and kid in _jwks_cache:
        return _jwks_cache[kid]
    keys = _fetch_jwks(issuer, ssl_verify)
    if kid not in keys:
        raise ValueError(
            f"id_token signing key {kid!r} not present in JWKS at {issuer}"
        )
    return keys[kid]


def verify_id_token(
    id_token: str,
    *,
    client_id: str,
    issuer: str,
    ssl_verify: bool,
    leeway_seconds: int = 30,
) -> dict[str, Any]:
    """Verify Okta's id_token and return its claims.

    Checks (all MUST pass, per OIDC Core §3.1.3.7):
      1. RS256 signature against the JWKS key with matching `kid`.
      2. `iss` == the configured issuer.
      3. `aud` contains our client_id.
      4. `exp` / `iat` are within `leeway_seconds`.
      5. `sub` and `exp` claims are present.

    Any failure raises `jwt.InvalidTokenError` (or a subclass). A caller that
    treats this as a boolean and swallows the exception is a security bug.
    """
    header = jwt.get_unverified_header(id_token)
    kid = header.get("kid")
    if not kid:
        raise jwt.InvalidTokenError("id_token header has no kid")

    signing_key = _get_signing_key(kid, issuer, ssl_verify)
    return jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=issuer.rstrip("/"),
        leeway=leeway_seconds,
        options={"require": ["exp", "iat", "sub"]},
    )
