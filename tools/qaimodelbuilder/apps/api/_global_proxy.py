# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Global proxy (mechanism B) provider — apps/api wiring root.

Single source of truth for the *machine-readable* global-proxy URL that the
"file download" class of outbound traffic (release manifest fetch, aria2c
binary auto-install, model-weight S3 downloads, the download engine, the
service catalog fetch, the ``webfetch`` tool) should route through.

Design (edition-dual-form §8 "全局代理覆盖范围")
-----------------------------------------------
Three proxy mechanisms coexist in V2:

* **Mechanism A** ``qai.chat.infrastructure.proxy_helper`` — an unwired shell.
* **Mechanism B** ``ToolsSettings.global_proxy`` / ``network_proxy`` — what the
  frontend "设置 → 代理" actually writes; this module fronts mechanism B.
* **Mechanism C** the channels per-instance proxy (WeChat / Feishu), which
  stays independent and is NOT touched here.

The proxy URL persisted in ``ToolsSettings.global_proxy`` carries **no**
credentials (AGENTS.md §3.3): the username lives in
``forge.config network_proxy.proxy_username`` and the password in the
:class:`SecretStore`. :func:`build_global_proxy_provider` returns a
zero-argument callable that, **at call time** (so a runtime-config edit
hot-applies), reads ``settings.tools.global_proxy`` and splices
``quote(user):quote(pass)@host[:port]`` into it from user_prefs + SecretStore.

The provider is deliberately a plain ``Callable[[], str | None]`` so each
consuming infrastructure adapter (``HttpReleaseManifestFetcher`` /
``Aria2cDaemon`` / the model-weight downloaders wired by S-F) stays
import-isolated from the settings object + the SecretStore — the apps/api
wiring root owns the config source (context-isolation §3.2).

State-Truth-First (AGENTS.md 🔴)
-------------------------------
The provider reflects the **live** ``ToolsSettings`` value: when no proxy is
configured it returns ``None`` and callers connect directly (graceful — proxy
is never forced). Reading the live value at call time (not at build time)
means a runtime proxy change takes effect on the next request, matching the
already-wired download-engine / catalog / webfetch outlets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


logger = get_logger(__name__)


# SecretStore namespace for the network-proxy password (AGENTS.md §3.3 — the
# credential never enters the KV / forge_config document or any log). Mirrors
# ``apps/api/_user_prefs_di.py`` (the write side: ``SaveProxyUseCase``) and
# ``apps/api/_ai_coding_di.py`` (the webfetch read side).
_PROXY_SECRET_SERVICE = "qai.network.proxy"  # noqa: S105 — keyring SERVICE name
_PROXY_SECRET_KEY = "proxy_password"  # noqa: S105 — keyring KEY name, not a value
_PROXY_CONFIG_SUBKEY = "network_proxy"


def embed_proxy_auth(container: "Container", bare_url: str | None) -> str | None:
    """Embed ``user:pass@`` into *bare_url* from user_prefs + SecretStore.

    V1 parity (``backend/tools/_webfetch.py:120-143`` /
    ``_security.py:500-519``): the persisted proxy URL carries no credentials;
    the username lives in ``forge.config network_proxy.proxy_username`` and the
    password in the :class:`SecretStore` (AGENTS.md §3.3). When both are
    present we splice ``quote(user):quote(pass)@host[:port]`` into the URL.
    Username-only / no-credential / empty-URL cases return *bare_url* unchanged.

    The password value is read from the SecretStore and embedded only into the
    returned in-memory URL — it is never logged, never written back to the
    config document.

    This is the shared implementation behind both the ``webfetch`` proxy
    embedding (``apps/api/_ai_coding_di.py``) and the global-proxy provider for
    the "file download" outlets (manifest / aria2c / model weights).
    """
    if not bare_url:
        return bare_url

    # Username from forge.config network_proxy (synchronous read of the shared
    # forge_config.json document — same file ``_runtime_config_store`` owns).
    username = ""
    try:
        import json

        from apps.api._runtime_config_store import forge_config_path

        path = forge_config_path(container.data_paths.root)
        if path.is_file():
            doc = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                section = doc.get(_PROXY_CONFIG_SUBKEY, {})
                if isinstance(section, dict):
                    username = str(section.get("proxy_username", "") or "")
    except Exception:  # noqa: BLE001 — never break wiring on config I/O
        username = ""

    if not username:
        # No username → no auth to embed (V1: ``if _gpn and password``).
        return bare_url

    # Password from the SecretStore (AGENTS.md §3.3).
    password = ""
    secret_store = getattr(container, "secret_store", None)
    if secret_store is not None:
        try:
            if secret_store.exists(_PROXY_SECRET_SERVICE, _PROXY_SECRET_KEY):
                password = secret_store.get(
                    _PROXY_SECRET_SERVICE, _PROXY_SECRET_KEY
                )
        except Exception:  # noqa: BLE001 — missing / unreadable secret ⇒ bare
            password = ""

    if not password:
        return bare_url

    from urllib.parse import quote, urlparse, urlunparse

    parsed = urlparse(bare_url)
    auth_netloc = (
        f"{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{parsed.hostname or ''}"
    )
    if parsed.port:
        auth_netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=auth_netloc))


def build_global_proxy_provider(
    container: "Container",
) -> Callable[[], str | None]:
    """Return a live ``() -> str | None`` mechanism-B global-proxy provider.

    The returned callable reads ``container.settings.tools.global_proxy`` at
    **call time** (so a runtime-config edit hot-applies) and embeds any
    configured ``user:pass@`` auth via :func:`embed_proxy_auth`. Returns the
    fully-authenticated proxy URL, or ``None`` when no proxy is configured
    (callers then connect directly — proxy is never forced; State-Truth-First).

    This is the single接入点 the "file download" outlets share:

    *缺口 7 ``HttpReleaseManifestFetcher`` (model_catalog),
    * 缺口 9 ``Aria2cDaemon`` binary auto-install (service_release),
    * 缺口 10 the whisper / zipformer / melotts weight downloaders (S-F),

    all take this provider so a single global-proxy setting fans out to every
    download path without each adapter importing settings / SecretStore.

    Contract for S-F (缺口 10)
    --------------------------
    * import:   ``from apps.api._global_proxy import build_global_proxy_provider``
    * signature: ``build_global_proxy_provider(container) -> Callable[[], str | None]``
    * usage:    call the returned provider in the weight downloader at request
                time; treat ``None`` as "no proxy, connect directly".
    """

    def _provider() -> str | None:
        settings = getattr(container, "settings", None)
        tools = getattr(settings, "tools", None) if settings is not None else None
        bare = getattr(tools, "global_proxy", None) if tools is not None else None
        if not bare:
            return None
        try:
            return embed_proxy_auth(container, bare)
        except Exception:  # noqa: BLE001 — never break a download on proxy read
            logger.warning("global_proxy.embed_auth_failed", exc_info=True)
            return bare

    return _provider


def build_ssl_verify_provider(container: "Container") -> Callable[[], bool]:
    """Live ``Settings.ssl_verify`` provider. Reads ``container.settings.ssl_verify``
    at CALL TIME so a runtime-config toggle (``apply_tools_runtime_config`` mutates
    ``container.settings.ssl_verify``) hot-applies to every outbound client that
    builds its httpx client through this provider. Default ``True`` when unresolved.

    Mirrors :func:`build_global_proxy_provider` exactly (same live-read pattern):
    each consuming adapter takes this ``Callable[[], bool]`` and reads it at the
    point where the httpx client is constructed, so disabling SSL verification via
    the global toggle takes effect immediately on the next client build — no adapter
    imports the settings object directly (context-isolation).
    """

    def _provider() -> bool:
        settings = getattr(container, "settings", None)
        val = getattr(settings, "ssl_verify", True) if settings is not None else True
        return bool(val)

    return _provider


__all__ = [
    "build_global_proxy_provider",
    "build_ssl_verify_provider",
    "embed_proxy_auth",
]
