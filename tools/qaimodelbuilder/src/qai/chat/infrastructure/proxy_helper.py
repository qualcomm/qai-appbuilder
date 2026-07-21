# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""proxy_helper — Global network proxy configuration for outbound HTTP.

Capabilities:
  1. Read proxy URL from config (HTTP_PROXY / HTTPS_PROXY / ALL_PROXY keys).
  2. Build httpx proxy mapping for httpx.AsyncClient / httpx.Client.
  3. Environment variable fallback (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY).
  4. NO_PROXY exclusion list handling (comma-separated hosts/CIDRs).
  5. SSL verify control (per-client toggle).
  6. Connection pool size configuration (max_connections, max_keepalive_connections).

Design:
  - Infrastructure layer: may import httpx, os, typing, urllib.parse.
  - Does NOT import backend.* / features.* / apps.* / interfaces.*.
  - Credentials are managed via SecretStore port (injected or lazy-resolved).
  - Thread-safe module-level state with a simple lock.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Protocol
from urllib.parse import quote, urlparse, urlunparse

logger = logging.getLogger("qai.chat.proxy")


# ── SecretStore protocol (structural subtype) ────────────────────────────────
# We declare a minimal protocol here to avoid importing the platform package
# at module level (keeps this module testable in isolation).


class _SecretStoreProto(Protocol):
    """Minimal subset of qai.platform.persistence.secrets.SecretStore."""

    def get(self, service: str, key: str) -> str: ...
    def set(self, service: str, key: str, value: str) -> None: ...
    def delete(self, service: str, key: str) -> None: ...


# ── Constants ────────────────────────────────────────────────────────────────

SERVICE_PROXY = "qai.proxy"

KEY_GLOBAL_PASSWORD = "global"
KEY_WECHAT_PASSWORD = "wechat_channel"
KEY_FEISHU_PASSWORD = "feishu_channel"

# Default connection pool sizes
DEFAULT_MAX_CONNECTIONS = 100
DEFAULT_MAX_KEEPALIVE = 20


# ── Module-level state (thread-safe) ─────────────────────────────────────────

_lock = threading.Lock()
_proxy_url: str = ""
_proxy_username: str = ""
_no_proxy: str = ""
_ssl_verify: bool = True
_max_connections: int = DEFAULT_MAX_CONNECTIONS
_max_keepalive: int = DEFAULT_MAX_KEEPALIVE
_secret_store: _SecretStoreProto | None = None


# ── Initialization ───────────────────────────────────────────────────────────


def init(
    secret_store: _SecretStoreProto,
    *,
    proxy_url: str = "",
    proxy_username: str = "",
    no_proxy: str = "",
    ssl_verify: bool = True,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    max_keepalive: int = DEFAULT_MAX_KEEPALIVE,
) -> None:
    """Initialize proxy configuration from application config.

    Parameters
    ----------
    secret_store : SecretStore
        Injected credential store for proxy passwords.
    proxy_url : str
        Proxy address (e.g. "http://proxy.corp:8080"). Empty = no proxy.
    proxy_username : str
        Proxy username (empty = no auth).
    no_proxy : str
        Comma-separated hosts/CIDRs to bypass proxy.
    ssl_verify : bool
        Whether to verify SSL certificates on outbound connections.
    max_connections : int
        Max total connections in the pool.
    max_keepalive : int
        Max keepalive connections in the pool.
    """
    global _proxy_url, _proxy_username, _no_proxy, _ssl_verify
    global _max_connections, _max_keepalive, _secret_store

    with _lock:
        _secret_store = secret_store
        _proxy_url = proxy_url.strip()
        _proxy_username = proxy_username.strip()
        _no_proxy = no_proxy.strip()
        _ssl_verify = ssl_verify
        _max_connections = max_connections
        _max_keepalive = max_keepalive

    logger.info(
        "Proxy config initialized: url=%s, user=%s, no_proxy=%s, verify=%s, pool=(%d/%d)",
        _proxy_url or "(none)",
        _proxy_username or "(none)",
        _no_proxy or "(none)",
        _ssl_verify,
        _max_connections,
        _max_keepalive,
    )


# ── Public API: set/get global proxy ─────────────────────────────────────────


def set_global_proxy(
    proxy_url: str,
    username: str = "",
    password: str = "",
) -> None:
    """Set global proxy configuration; password is stored in SecretStore.

    Parameters
    ----------
    proxy_url : str
        Proxy address. Empty string clears the proxy.
    username : str
        Proxy username (empty = no auth).
    password : str
        Proxy password. Non-empty and non-mask ("****") stores to SecretStore.
        Empty string clears stored password.
    """
    global _proxy_url, _proxy_username

    with _lock:
        url = proxy_url.strip()
        uname = username.strip()

        if not url:
            _proxy_url = ""
            _proxy_username = ""
            _delete_password(KEY_GLOBAL_PASSWORD)
            logger.info("Global proxy cleared")
            return

        _proxy_url = url
        _proxy_username = uname

        if password and password != "****":
            _save_password(KEY_GLOBAL_PASSWORD, password)
        elif not password:
            _delete_password(KEY_GLOBAL_PASSWORD)

        logger.info(
            "Global proxy set: %s (user=%s, has_password=%s)",
            url,
            uname or "none",
            bool(_load_password(KEY_GLOBAL_PASSWORD)),
        )


def get_global_proxy() -> dict[str, str]:
    """Return current proxy config (no password) for API responses."""
    with _lock:
        return {
            "proxy_url": _proxy_url,
            "proxy_username": _proxy_username,
            "no_proxy": _no_proxy,
            "ssl_verify": str(_ssl_verify).lower(),
        }


# ── Public API: build httpx kwargs ───────────────────────────────────────────


def get_httpx_proxy_kwargs(
    *,
    target_url: str = "",
    extra_verify: bool | None = None,
) -> dict[str, Any]:
    """Build keyword arguments for httpx.AsyncClient / httpx.Client.

    Handles:
      - Proxy URL with embedded auth credentials
      - NO_PROXY exclusion matching
      - SSL verify override
      - Connection pool limits

    Parameters
    ----------
    target_url : str
        The URL being requested (used for NO_PROXY matching). If empty,
        proxy is always applied when configured.
    extra_verify : bool | None
        Override SSL verify setting. None uses the module-level default.

    Returns
    -------
    dict
        Ready to unpack: ``httpx.AsyncClient(**get_httpx_proxy_kwargs())``
    """
    kwargs: dict[str, Any] = {}

    with _lock:
        proxy_url = _proxy_url
        username = _proxy_username
        no_proxy = _no_proxy
        verify = _ssl_verify if extra_verify is None else extra_verify
        max_conn = _max_connections
        max_ka = _max_keepalive

    # SSL verify
    if not verify:
        kwargs["verify"] = False

    # Connection pool limits
    try:
        import httpx
        kwargs["limits"] = httpx.Limits(
            max_connections=max_conn,
            max_keepalive_connections=max_ka,
        )
    except ImportError:
        pass

    # Proxy
    effective_proxy = _resolve_proxy_url(
        proxy_url, username, target_url, no_proxy
    )
    if effective_proxy:
        kwargs["proxy"] = effective_proxy

    return kwargs


def get_env_proxy_url(scheme: str = "https") -> str:
    """Get proxy URL from environment variables (fallback mechanism).

    Checks (in order): ALL_PROXY, HTTPS_PROXY/HTTP_PROXY (based on scheme),
    then lowercase variants.

    Returns
    -------
    str
        Proxy URL or empty string if none found.
    """
    candidates: list[str]
    if scheme.lower() == "https":
        candidates = ["ALL_PROXY", "HTTPS_PROXY", "all_proxy", "https_proxy"]
    else:
        candidates = ["ALL_PROXY", "HTTP_PROXY", "all_proxy", "http_proxy"]

    for var in candidates:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return ""


def get_no_proxy_list() -> list[str]:
    """Return the effective NO_PROXY exclusion list (from config + env).

    Returns
    -------
    list[str]
        List of hosts/domains/CIDRs that should bypass the proxy.
    """
    with _lock:
        config_no_proxy = _no_proxy

    entries: set[str] = set()

    # From config
    if config_no_proxy:
        for item in config_no_proxy.split(","):
            item = item.strip()
            if item:
                entries.add(item.lower())

    # From environment
    for var in ("NO_PROXY", "no_proxy"):
        env_val = os.environ.get(var, "").strip()
        if env_val:
            for item in env_val.split(","):
                item = item.strip()
                if item:
                    entries.add(item.lower())

    return sorted(entries)


# ── Public API: aiohttp support ──────────────────────────────────────────────


def get_aiohttp_proxy_args() -> tuple[str | None, Any]:
    """Return (proxy_url, proxy_auth) for aiohttp session.

    Returns
    -------
    tuple[str | None, aiohttp.BasicAuth | None]
    """
    with _lock:
        proxy_url = _proxy_url
        username = _proxy_username

    if not proxy_url:
        return None, None

    try:
        import aiohttp
        password = _load_password(KEY_GLOBAL_PASSWORD)
        proxy_auth = None
        if username and password:
            proxy_auth = aiohttp.BasicAuth(username, password)
        return proxy_url, proxy_auth
    except ImportError:
        return proxy_url, None


# ── Internal helpers ─────────────────────────────────────────────────────────


def _resolve_proxy_url(
    proxy_url: str,
    username: str,
    target_url: str,
    no_proxy: str,
) -> str:
    """Resolve effective proxy URL, considering NO_PROXY and auth.

    Returns empty string if proxy should not be used.
    """
    if not proxy_url:
        # Fall back to environment
        scheme = "https"
        if target_url:
            parsed_target = urlparse(target_url)
            scheme = parsed_target.scheme or "https"
        proxy_url = get_env_proxy_url(scheme)
        if not proxy_url:
            return ""

    # Check NO_PROXY exclusion
    if target_url and _is_no_proxy(target_url, no_proxy):
        return ""

    # Embed auth if credentials available
    password = _load_password(KEY_GLOBAL_PASSWORD)
    if username and password:
        parsed = urlparse(proxy_url)
        auth_netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{parsed.hostname}"
        if parsed.port:
            auth_netloc += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=auth_netloc))

    return proxy_url


def _is_no_proxy(target_url: str, config_no_proxy: str) -> bool:
    """Check if target_url matches NO_PROXY exclusion list."""
    try:
        parsed = urlparse(target_url)
        host = (parsed.hostname or "").lower()
    except Exception:
        return False

    if not host:
        return False

    exclusions = get_no_proxy_list.__wrapped__(config_no_proxy) if hasattr(get_no_proxy_list, "__wrapped__") else _get_no_proxy_entries(config_no_proxy)

    for entry in exclusions:
        if entry == "*":
            return True
        if host == entry:
            return True
        # Domain suffix match: ".example.com" matches "sub.example.com"
        if entry.startswith(".") and host.endswith(entry):
            return True
        # Also match without leading dot
        if not entry.startswith(".") and host.endswith("." + entry):
            return True

    return False


def _get_no_proxy_entries(config_no_proxy: str) -> list[str]:
    """Parse NO_PROXY entries from config string + environment."""
    entries: set[str] = set()

    if config_no_proxy:
        for item in config_no_proxy.split(","):
            item = item.strip().lower()
            if item:
                entries.add(item)

    for var in ("NO_PROXY", "no_proxy"):
        env_val = os.environ.get(var, "").strip()
        if env_val:
            for item in env_val.split(","):
                item = item.strip().lower()
                if item:
                    entries.add(item)

    return list(entries)


def _load_password(key: str) -> str:
    """Load password from SecretStore. Returns empty string on failure."""
    store = _secret_store
    if store is None:
        return ""
    try:
        return store.get(SERVICE_PROXY, key)
    except Exception:
        return ""


def _save_password(key: str, password: str) -> None:
    """Save password to SecretStore."""
    store = _secret_store
    if store is None:
        logger.warning("Cannot save proxy password: SecretStore not initialized")
        return
    try:
        store.set(SERVICE_PROXY, key, password)
    except Exception as exc:
        logger.warning("Failed to save proxy password: %s", exc)


def _delete_password(key: str) -> None:
    """Delete password from SecretStore."""
    store = _secret_store
    if store is None:
        return
    try:
        store.delete(SERVICE_PROXY, key)
    except Exception as exc:  # noqa: BLE001 - best-effort delete
        # Log the key NAME and exception TYPE only — never the secret
        # value itself (AGENTS.md §3.3 credentials must not reach logs).
        logger.warning(
            "Failed to delete proxy password for key %r: %s",
            key,
            type(exc).__name__,
        )
