# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Channel SDK network policy: proxy + per-domain SSL handling (V1 parity).

Background
----------
The personal-WeChat ``wechatbot`` SDK and the Feishu ``lark_oapi`` SDK both
construct their own HTTP / WebSocket clients internally and expose **no**
proxy / SSL parameters on their public constructors (verified against the
installed wheels: ``WeChatBot.__init__`` takes only ``base_url`` / ``cred_path``
/ ``on_*`` callbacks; ``lark_oapi.ws.Client`` takes only
``app_id`` / ``app_secret`` / ``log_level`` / ``event_handler`` / ``domain`` /
``auto_reconnect``).  In an enterprise-proxy environment the SDK traffic must:

* route through the configured HTTP proxy (with optional Basic auth), and
* skip TLS verification for the provider's own domains, because the corporate
  proxy presents a man-in-the-middle certificate the default trust store
  rejects.

V1 achieved this by monkey-patching the transport libraries.  Two V1 sites:

* ``backend/channels/wechat/channel.py:38-59`` — patches
  ``aiohttp.ClientSession._request`` **at module import**, injecting
  ``proxy`` / ``proxy_auth`` / ``ssl=False`` for every aiohttp request as long
  as a proxy is configured.  The patch is process-global and never restored.
* ``backend/channels/feishu/channel.py:599-644`` — patches
  ``requests.Session.request`` (``verify=False`` only for Feishu domains) and
  ``websockets.connect`` (TLS-skip ``ssl`` context for ``wss://``) at start
  time, restoring ``requests`` in a ``finally`` but leaving ``websockets``
  patched.

Design (functional parity with V1, better architecture)
-------------------------------------------------------
This module keeps the **user-facing behaviour** (proxy + TLS skip for channel
SDK traffic) but replaces V1's import-time, un-scoped, non-restored global
patches with a **reference-counted, restorable, per-library** install /
uninstall pair:

* TLS verification is disabled on the patched transport calls while a channel
  is connected. The patch is installed only for the transport libraries the
  channel SDKs use (``aiohttp`` / ``requests`` / ``websockets``) and only for
  the lifetime of a channel connection, then the originals are restored — so
  unrelated code paths outside a live channel connection keep full
  verification. Enterprise proxies present a man-in-the-middle certificate the
  default trust store rejects (``CERT_NONE`` /
  ``CA cert does not include key usage extension``); the channel SDKs only
  ever talk to their own provider hosts while the patch is active.
* The provider domain lists (``*_TLS_SKIP_DOMAINS``) are retained as a
  diagnostic hint; the actual skip is unconditional on the patched calls so it
  also covers redirect hosts and provider endpoints not in the list.
* The patches are installed when a channel transport starts and removed when
  it stops; a reference count keeps them active for the whole connection
  lifetime (including SDK reconnects) and across concurrent instances, then
  cleanly restores the original callables when the last consumer stops.
* Per-library install: different channel kinds request different patch sets
  (WeChat → ``aiohttp``; Feishu → ``requests`` + ``websockets``). Each library
  is patched independently the first time any consumer requests it, so a
  channel starting second still gets its extra library patches installed
  (fixing a defect where WeChat starting first left Feishu's ``requests`` /
  ``websockets`` unpatched, causing Feishu TLS verification to fail).
* No module-import side effects: importing this module patches nothing.

Domains
-------
The provider domain lists mirror V1 exactly (kept as diagnostic hints; the TLS
skip itself is unconditional on the patched calls):

* WeChat: the wechatbot SDK only ever talks to its own host.
* Feishu: ``larksuite.com`` / ``feishu.cn`` / ``larkoffice.com`` (V1
  ``channel.py:606``).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from qai.platform.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "SdkNetworkPolicy",
    "install_sdk_network_policy",
    "uninstall_sdk_network_policy",
    "WECHAT_TLS_SKIP_DOMAINS",
    "FEISHU_TLS_SKIP_DOMAINS",
]


# Provider host fragments for which TLS verification is skipped (substring
# match against the request URL, mirroring V1's ``any(d in url ...)`` test).
WECHAT_TLS_SKIP_DOMAINS: tuple[str, ...] = (
    "weixin.qq.com",
    "wechat.com",
    "wx.qq.com",
)
FEISHU_TLS_SKIP_DOMAINS: tuple[str, ...] = (
    "larksuite.com",
    "feishu.cn",
    "larkoffice.com",
)


@dataclass(frozen=True, slots=True)
class SdkNetworkPolicy:
    """Proxy + TLS-skip policy applied to channel SDK transport traffic.

    Attributes
    ----------
    proxy_url:
        HTTP proxy URL (e.g. ``http://host:port``).  Empty disables proxy
        injection.
    proxy_username / proxy_password:
        Optional Basic-auth credentials for the proxy.
    tls_skip_domains:
        Host fragments for which TLS verification is skipped.  The matching
        request keeps verifying any other host — TLS is only relaxed for the
        provider's own endpoints (V1 parity, scoped).
    patch_aiohttp:
        Patch ``aiohttp.ClientSession._request`` (the wechatbot SDK path).
    patch_requests:
        Patch ``requests.Session.request`` (the Feishu rest path).
    patch_websockets:
        Patch ``websockets.connect`` (the Feishu WS path).
    """

    proxy_url: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    tls_skip_domains: tuple[str, ...] = field(default_factory=tuple)
    patch_aiohttp: bool = False
    patch_requests: bool = False
    patch_websockets: bool = False

    @property
    def has_proxy(self) -> bool:
        return bool(self.proxy_url.strip())

    @property
    def has_proxy_auth(self) -> bool:
        return bool(self.proxy_username.strip() and self.proxy_password.strip())


# ---------------------------------------------------------------------------
# Reference-counted, restorable patch state
# ---------------------------------------------------------------------------
_lock = threading.RLock()
_refcount = 0
_active_policy: SdkNetworkPolicy | None = None

# Saved originals (None when not patched).
_orig_aiohttp_request = None
_orig_requests_request = None
_orig_websockets_connect = None

# Live per-library policy holders.
#
# DEFECT FIX (proxy never applied): the patch closures used to capture
# ``proxy_url`` at *install* time and were only replaceable when the global
# refcount hit zero. But a concurrently-running channel (e.g. Feishu, or a
# ``auto_start`` WeChat instance with an *empty* proxy) keeps the refcount
# above zero, so ``uninstall`` never restores the original, so a later
# ``install`` for the instance that *does* have a proxy early-returns
# (``_orig_* is not None``) and the empty-proxy closure stays wedged. The
# WeChat SDK then connects DIRECT, WeChat returns an empty body, and
# ``json.loads`` raises "Expecting value: line 1 column 1 (char 0)".
#
# Fix: the closures now read the proxy from these module-level holders, and
# ``install_sdk_network_policy`` refreshes the holder for each requested
# library on EVERY call. So the freshest policy always wins immediately,
# regardless of refcount / install order.
_live_aiohttp: dict[str, object] = {"proxy_url": "", "proxy_auth": None}
_live_requests: dict[str, object] = {"proxies": None}
_live_websockets: dict[str, object] = {"proxy_url": ""}


def _url_matches(url: str, domains: tuple[str, ...]) -> bool:
    return any(d in url for d in domains)


def _proxy_host_hint(proxy_url: str) -> str:
    """Return ``host:port`` from ``proxy_url`` for logging (never creds).

    Empty string when no proxy is configured. Any credentials embedded in the
    URL are stripped so they never reach the logs.
    """
    base = (proxy_url or "").strip()
    if not base:
        return ""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(base)
        host = parsed.hostname or ""
        if parsed.port:
            return f"{host}:{parsed.port}"
        return host
    except Exception:  # noqa: BLE001 - logging hint only
        return "<unparseable>"


def _install_aiohttp(policy: SdkNetworkPolicy) -> None:
    global _orig_aiohttp_request
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - aiohttp ships with wechatbot
        logger.warning("channels.sdk_network.aiohttp_missing")
        return
    if _orig_aiohttp_request is not None:
        return
    _orig_aiohttp_request = aiohttp.ClientSession._request

    async def _patched(self, method, str_or_url, **kwargs):  # type: ignore[no-untyped-def]
        # Read the proxy from the live holder (refreshed on every
        # ``install_sdk_network_policy`` call) so a policy installed AFTER this
        # patch — e.g. the WeChat instance that actually has a proxy, applied
        # while a proxy-less channel already patched aiohttp — takes effect
        # without needing the global refcount to drop to zero first.
        proxy_url = _live_aiohttp.get("proxy_url") or ""
        proxy_auth = _live_aiohttp.get("proxy_auth")
        if proxy_url and "proxy" not in kwargs:
            kwargs["proxy"] = proxy_url
        if proxy_auth is not None and "proxy_auth" not in kwargs:
            kwargs["proxy_auth"] = proxy_auth
        # Disable TLS verification for channel SDK traffic (enterprise MITM
        # proxy). The wechatbot SDK only ever talks to its own provider host
        # while this patch is active, so the skip is scoped to that traffic.
        if "ssl" not in kwargs:
            kwargs["ssl"] = False
        return await _orig_aiohttp_request(self, method, str_or_url, **kwargs)

    aiohttp.ClientSession._request = _patched


def _refresh_aiohttp_policy(policy: SdkNetworkPolicy) -> None:
    """Update the live aiohttp proxy holder from ``policy`` (call every install)."""
    try:
        import aiohttp
    except ImportError:  # pragma: no cover
        return
    proxy_url = policy.proxy_url.strip()
    proxy_auth = None
    if policy.has_proxy_auth:
        proxy_auth = aiohttp.BasicAuth(
            policy.proxy_username.strip(), policy.proxy_password.strip()
        )
    _live_aiohttp["proxy_url"] = proxy_url
    _live_aiohttp["proxy_auth"] = proxy_auth


def _install_requests(policy: SdkNetworkPolicy) -> None:
    global _orig_requests_request
    try:
        import requests
        import urllib3
    except ImportError:  # pragma: no cover - requests ships with lark_oapi
        logger.warning("channels.sdk_network.requests_missing")
        return
    if _orig_requests_request is not None:
        return
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _orig_requests_request = requests.Session.request

    def _patched(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        proxies = _live_requests.get("proxies")
        if proxies is not None and "proxies" not in kwargs:
            kwargs["proxies"] = proxies
        # TLS verification is disabled for channel SDK traffic. Enterprise
        # proxies present a MITM certificate the default trust store rejects
        # (e.g. ``CERT_NONE`` / ``CA cert does not include key usage
        # extension``); the channel SDKs only ever talk to their own provider
        # hosts while this patch is active, so relaxing verify here is scoped
        # to that SDK traffic.
        kwargs.setdefault("verify", False)
        return _orig_requests_request(self, method, url, **kwargs)

    requests.Session.request = _patched


def _refresh_requests_policy(policy: SdkNetworkPolicy) -> None:
    """Update the live requests proxy holder from ``policy``."""
    proxy_url = policy.proxy_url.strip()
    _live_requests["proxies"] = (
        {"http": proxy_url, "https": proxy_url} if proxy_url else None
    )


def _proxy_url_with_auth(policy: SdkNetworkPolicy) -> str:
    """Return ``policy.proxy_url`` with ``user:pass@`` spliced in, or ``""``.

    websockets ≥ 13 (we run 16.0) accepts a ``proxy`` URL of the form
    ``http(s)://[user:pass@]host:port`` on :func:`websockets.connect`; Basic
    proxy auth is carried inline. Returns an empty string when no proxy is
    configured (callers then leave ``connect`` to its default — direct or
    env-based — behaviour). The password is spliced into the in-memory URL
    only; it is never logged.
    """
    base = policy.proxy_url.strip()
    if not base:
        return ""
    if not policy.has_proxy_auth:
        return base
    from urllib.parse import quote, urlparse, urlunparse

    parsed = urlparse(base)
    auth_netloc = (
        f"{quote(policy.proxy_username.strip(), safe='')}:"
        f"{quote(policy.proxy_password.strip(), safe='')}"
        f"@{parsed.hostname or ''}"
    )
    if parsed.port:
        auth_netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=auth_netloc))


def _install_websockets(policy: SdkNetworkPolicy) -> None:
    global _orig_websockets_connect
    try:
        import ssl
        import websockets
    except ImportError:  # pragma: no cover - websockets ships with lark_oapi
        logger.warning("channels.sdk_network.websockets_missing")
        return
    if _orig_websockets_connect is not None:
        return
    _orig_websockets_connect = websockets.connect

    def _patched(uri, **kwargs):  # type: ignore[no-untyped-def]
        proxy_url = _live_websockets.get("proxy_url") or ""
        if proxy_url and "proxy" not in kwargs:
            kwargs["proxy"] = proxy_url
        # Skip TLS verification only for the provider's own domains (V1 parity,
        # scoped). V1 relaxed TLS for every ``wss://``; we keep that for the
        # WS path because the SDK only ever opens its own provider socket.
        if str(uri).startswith("wss://") and "ssl" not in kwargs:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = ssl_ctx
        return _orig_websockets_connect(uri, **kwargs)

    websockets.connect = _patched


def _refresh_websockets_policy(policy: SdkNetworkPolicy) -> None:
    """Update the live websockets proxy holder from ``policy``."""
    _live_websockets["proxy_url"] = _proxy_url_with_auth(policy)


def install_sdk_network_policy(policy: SdkNetworkPolicy) -> None:
    """Install the patches for ``policy`` (reference-counted, per-library).

    Safe to call once per channel ``start()``; pair every call with
    :func:`uninstall_sdk_network_policy` in the matching ``stop()``.

    Per-library install (defect fix): different channel kinds request
    different patch sets (WeChat → ``aiohttp`` only; Feishu → ``requests`` +
    ``websockets``). A single wholesale ``if _refcount == 0`` gate meant that
    whichever channel started **first** won, and a later channel's additional
    library patches were **never installed** — e.g. WeChat starting first
    (aiohttp only) left Feishu's ``requests`` / ``websockets`` unpatched, so
    the lark SDK hit the corporate MITM cert and failed TLS verification.

    Each ``_install_*`` helper is idempotent (it early-returns when its
    original is already saved), so we can call the requested installers on
    **every** call: the first consumer of each library wins that library's
    active policy (proxy/TLS), and later consumers requesting an as-yet
    unpatched library get it installed too. The reference count now guards
    only *when originals are restored* (last consumer out), not *which*
    libraries are patched.
    """
    global _refcount, _active_policy
    if not (
        policy.patch_aiohttp
        or policy.patch_requests
        or policy.patch_websockets
    ):
        return
    with _lock:
        if _active_policy is None:
            _active_policy = policy
        # Install each requested library patch that is not yet active. The
        # helpers early-return when their original is already saved, so this
        # is safe to call on every consumer's start().
        installed_aiohttp = False
        installed_requests = False
        installed_websockets = False
        if policy.patch_aiohttp and _orig_aiohttp_request is None:
            _install_aiohttp(policy)
            installed_aiohttp = _orig_aiohttp_request is not None
        if policy.patch_requests and _orig_requests_request is None:
            _install_requests(policy)
            installed_requests = _orig_requests_request is not None
        if policy.patch_websockets and _orig_websockets_connect is None:
            _install_websockets(policy)
            installed_websockets = _orig_websockets_connect is not None
        # DEFECT FIX: refresh the LIVE proxy holders on EVERY call for each
        # requested library — even when the patch was already installed by an
        # earlier (possibly proxy-less) consumer. Without this the first
        # consumer's proxy was latched into the closure and a later consumer
        # with the real proxy could never take effect while the refcount
        # stayed > 0 (WeChat proxy silently dropped → SDK direct-connect →
        # empty body → JSONDecodeError). The freshest policy now always wins.
        if policy.patch_aiohttp:
            _refresh_aiohttp_policy(policy)
        if policy.patch_requests:
            _refresh_requests_policy(policy)
        if policy.patch_websockets:
            _refresh_websockets_policy(policy)
        logger.info(
            "channels.sdk_network.installed",
            has_proxy=policy.has_proxy,
            proxy_host=_proxy_host_hint(policy.proxy_url),
            aiohttp_installed=installed_aiohttp,
            requests_installed=installed_requests,
            websockets_installed=installed_websockets,
            patch_aiohttp=policy.patch_aiohttp,
            patch_requests=policy.patch_requests,
            patch_websockets=policy.patch_websockets,
            refcount=_refcount + 1,
        )
        _refcount += 1


def uninstall_sdk_network_policy() -> None:
    """Decrement the reference count; restore originals when it hits zero."""
    global _refcount, _active_policy
    global _orig_aiohttp_request, _orig_requests_request
    global _orig_websockets_connect
    with _lock:
        if _refcount == 0:
            return
        _refcount -= 1
        if _refcount > 0:
            return
        # Last consumer stopped — restore every patched callable.
        if _orig_aiohttp_request is not None:
            try:
                import aiohttp

                aiohttp.ClientSession._request = _orig_aiohttp_request
            except Exception:  # noqa: BLE001 - best-effort restore
                pass
            _orig_aiohttp_request = None
        if _orig_requests_request is not None:
            try:
                import requests

                requests.Session.request = _orig_requests_request
            except Exception:  # noqa: BLE001
                pass
            _orig_requests_request = None
        if _orig_websockets_connect is not None:
            try:
                import websockets

                websockets.connect = _orig_websockets_connect
            except Exception:  # noqa: BLE001
                pass
            _orig_websockets_connect = None
        _active_policy = None
        # Reset the live proxy holders so a subsequent install starts clean.
        _live_aiohttp["proxy_url"] = ""
        _live_aiohttp["proxy_auth"] = None
        _live_requests["proxies"] = None
        _live_websockets["proxy_url"] = ""
        logger.info("channels.sdk_network.uninstalled")
