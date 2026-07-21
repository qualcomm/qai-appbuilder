# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Feishu tenant_access_token cache (PR-097 §2 R-2 Feishu half).

Restores the tenant_access_token caching that the legacy
``backend/channels/feishu/channel.py`` got for free from the
``lark_oapi`` SDK.  S9 PR-097 chose to **not** bring lark back as a
REST client (decision recorded in
``docs/90-refactor/S9-channels-deep-parity-audit.md`` §7.2): instead
this adapter calls Feishu's open-platform REST endpoint directly via
the project's standard ``httpx.AsyncClient`` injection pattern.

Wire format (Feishu open platform, 2024)::

    POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
    Content-Type: application/json
    Body: {"app_id": "...", "app_secret": "..."}

    Success: {"code": 0, "msg": "ok",
              "tenant_access_token": "t-...",
              "expire": 7200}      # seconds, ceiling 2h

    Failure: {"code": <non-zero>, "msg": "..."}

Refresh policy
--------------
Feishu issues tokens with a **2-hour ceiling** and instructs apps to
refresh "well before" expiry to avoid mid-request invalidation.  We
use a **1-hour safety margin**: ``get_token`` returns the cached value
only if it has at least ``_REFRESH_AHEAD_SECONDS = 3600`` seconds of
remaining lifetime; otherwise it refreshes synchronously before
returning.

When upstream rejects an outbound call with code ``99991663`` (token
expired) or ``99991664`` (invalid token) the caller invokes
:meth:`invalidate` and re-issues; this adapter performs at most one
opportunistic refresh per call to keep the retry loop bounded.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from qai.platform.errors import ExternalServiceError
from qai.platform.logging import get_logger

__all__ = [
    "FeishuTenantTokenCache",
    "FEISHU_TOKEN_ENDPOINT",
    "FEISHU_TOKEN_EXPIRED_CODES",
]

logger = get_logger(__name__)

#: Feishu open-platform endpoint for tenant_access_token issuance.
FEISHU_TOKEN_ENDPOINT = (
    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
)

#: Feishu error codes that mean "the tenant_access_token you sent is no
#: longer valid; obtain a new one and retry".  Sourced from the legacy
#: SDK's retry path (``backend/channels/feishu/channel.py:1601``).
FEISHU_TOKEN_EXPIRED_CODES: frozenset[int] = frozenset({99991663, 99991664})

# 1 hour ahead of expiry — Feishu issues 2h tokens; refreshing at 1h
# remaining gives plenty of margin even under clock skew.
_REFRESH_AHEAD_SECONDS: float = 3600.0

# Network call budget for the token refresh round-trip.
_REFRESH_HTTP_TIMEOUT_SECONDS: float = 10.0


def _now() -> float:
    return time.monotonic()


class FeishuTenantTokenCache:
    """Async cache for Feishu's tenant_access_token.

    Construction is cheap (no network); the first :meth:`get_token`
    call issues a refresh.  Concurrent ``get_token`` callers coalesce
    behind a single ``asyncio.Lock`` so we never hit the endpoint more
    than once per refresh window.

    Args:
        http_client_factory: Callable matching the channels-context
            convention ``factory(*, timeout: float) -> AsyncClient``;
            shared with :class:`_BaseHttpTransport` so proxy /
            credential plumbing stays consistent.
        app_id: Feishu app id (e.g. ``cli_a1b2c3d4e5f6g7h8``).
        app_secret: Feishu app secret.  Plaintext is fine here
            because the value is sourced from the SecretStore one
            level up (per AGENTS.md §3.3) and never logged.
        clock: Optional monotonic-clock callable (testing seam).
    """

    __slots__ = (
        "_http_client_factory",
        "_app_id",
        "_app_secret",
        "_clock",
        "_token",
        "_expires_at",
        "_lock",
    )

    def __init__(
        self,
        *,
        http_client_factory: Callable[..., Any],
        app_id: str,
        app_secret: str,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not app_id:
            raise ValueError("FeishuTenantTokenCache requires app_id")
        if not app_secret:
            raise ValueError("FeishuTenantTokenCache requires app_secret")
        self._http_client_factory = http_client_factory
        self._app_id = app_id
        self._app_secret = app_secret
        self._clock = clock or _now
        self._token: str = ""
        # Monotonic-time deadline; 0 means "no token cached".
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def get_token(self) -> str:
        """Return a valid tenant_access_token, refreshing if needed."""
        if self._token and not self._needs_refresh():
            return self._token
        async with self._lock:
            # Recheck under lock — another coroutine may have just refreshed.
            if self._token and not self._needs_refresh():
                return self._token
            await self._refresh()
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token so the next :meth:`get_token` refreshes.

        Called by the transport's retry path when upstream replies with
        :data:`FEISHU_TOKEN_EXPIRED_CODES`.
        """
        self._token = ""
        self._expires_at = 0.0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _needs_refresh(self) -> bool:
        if not self._token:
            return True
        return (self._expires_at - self._clock()) <= _REFRESH_AHEAD_SECONDS

    async def _refresh(self) -> None:
        """POST app credentials → cache (token, deadline).

        Raises :class:`ExternalServiceError` on transport / non-zero
        upstream-code failures.  The exception's ``code`` keys the
        caller's retry decision (``channels.feishu.token_refresh_failed``).
        """
        body = {"app_id": self._app_id, "app_secret": self._app_secret}
        try:
            async with self._http_client_factory(
                timeout=_REFRESH_HTTP_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(
                    FEISHU_TOKEN_ENDPOINT, json=body
                )
        except Exception as exc:  # noqa: BLE001
            raise ExternalServiceError(
                "channels.feishu.token_refresh_failed",
                f"feishu tenant_access_token refresh transport error: {exc}",
                service="feishu",
                cause=exc,
            ) from exc

        if response.status_code >= 400:
            raise ExternalServiceError(
                "channels.feishu.token_refresh_failed",
                "feishu tenant_access_token refresh returned HTTP "
                f"{response.status_code}",
                service="feishu",
                status=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "channels.feishu.token_refresh_failed",
                "feishu tenant_access_token refresh returned non-JSON body",
                service="feishu",
                cause=exc,
            ) from exc
        if not isinstance(data, dict):
            raise ExternalServiceError(
                "channels.feishu.token_refresh_failed",
                "feishu tenant_access_token refresh envelope must be JSON object",
                service="feishu",
            )

        code = data.get("code")
        if code != 0:
            # NB: do NOT include app_secret / token text in the log.
            logger.warning(
                "channels.feishu.token_refresh_rejected",
                code=code,
                msg=str(data.get("msg", "")),
                app_id=self._app_id,
            )
            raise ExternalServiceError(
                "channels.feishu.token_refresh_failed",
                f"feishu tenant_access_token refresh rejected: code={code} "
                f"msg={data.get('msg', '')!r}",
                service="feishu",
            )

        token = data.get("tenant_access_token")
        expire = data.get("expire")
        if not isinstance(token, str) or not token:
            raise ExternalServiceError(
                "channels.feishu.token_refresh_failed",
                "feishu tenant_access_token refresh missing 'tenant_access_token'",
                service="feishu",
            )
        try:
            ttl = float(expire) if expire is not None else 7200.0
        except (TypeError, ValueError):
            ttl = 7200.0
        # Clamp to the documented 2h ceiling defensively in case Feishu
        # ever returns an over-large value during a partial outage.
        ttl = min(max(ttl, 60.0), 7200.0)

        self._token = token
        self._expires_at = self._clock() + ttl
        logger.info(
            "channels.feishu.token_refreshed",
            app_id=self._app_id,
            ttl_seconds=int(ttl),
        )


# Module-level type alias — useful for callers that want to accept
# either the real cache or a test stub matching the same shape.
GetFeishuTokenFn = Callable[[], Awaitable[str]]
