# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Apps-layer bridge: build the internal-only web-search provider registry.

The ``web_search`` chat/coding tool is backed by a pluggable
:class:`SearchProviderRegistry` whose first provider is the intranet
:class:`CebotSearchProvider`. Both live in ``qai.platform.edition.web_search``
— a package physically excluded from external artifacts — so this bridge
constructs the registry ONLY when ``settings.is_internal`` is true (layer-1 of
the four-layer edition defence, AGENTS.md 🟤).

It composes the registry from three sources, exactly like
:mod:`apps.api._query_service_bridge` does for the chat transport:

* **edition config** — the CEBot descriptor fields (endpoint / model / TLS /
  …) from ``qai.platform.edition.get_query_services()`` (the same
  ``[query_services.cebot]`` table the chat query-service path reads);
* **SecretStore** — the per-service api_key (namespace
  ``qai.model_catalog.provider`` / key ``cebot``);
* **OS identity** — the ``usid`` is resolved inside the provider.

All ``qai.platform.edition.web_search`` imports are LOCAL to the internal-gated
body so a stripped external tree never triggers an ImportError when an apps
module imports this bridge at load time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence.secrets import SecretStore

__all__ = ["build_search_registry"]

_log = get_logger(__name__)

# Namespace the per-service api_key is stored under (shared with
# tools/init/edition_secrets + _query_service_bridge + _model_resolver_bridge).
_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"

# The CEBot service id in the edition ``[query_services.<id>]`` config table
# and the SecretStore key under the provider namespace.
_CEBOT_SERVICE_ID = "cebot"


def _secret_get(store: "SecretStore | None", key: str) -> str | None:
    if store is None:
        return None
    try:
        if not store.exists(_PROVIDER_SECRET_SERVICE, key):
            return None
        return store.get(_PROVIDER_SECRET_SERVICE, key)
    except Exception:  # noqa: BLE001 — any failure ⇒ no usable credential
        return None


def build_search_registry(*, container: Any) -> Any | None:
    """Build the web-search provider registry, or ``None`` on external.

    Returns a ``SearchProviderRegistry`` with the CEBot provider registered
    (default), or ``None`` when:

    * the build edition is not internal, or
    * the edition package / CEBot descriptor is unavailable, so there is no
      search backend to offer.

    The registry is suitable to pass as ``search_registry=`` to
    ``build_default_tool_handlers`` (which then registers the ``web_search``
    tool) and to the chat-side web-search tool bridge.
    """
    settings = getattr(container, "settings", None)
    if settings is None or not getattr(settings, "is_internal", False):
        return None

    # All internal-only imports are local (the package is excluded externally).
    try:
        from qai.platform.edition import get_query_services
        from qai.platform.edition.web_search import (
            CebotSearchProvider,
            SearchProviderRegistry,
        )
    except Exception:  # pragma: no cover - package excluded on external
        return None

    descriptors_cfg = get_query_services()
    cebot_fields = descriptors_cfg.get(_CEBOT_SERVICE_ID)
    if not cebot_fields:
        return None

    secret_store = getattr(container, "secret_store", None)
    api_key = _secret_get(secret_store, _CEBOT_SERVICE_ID)

    try:
        provider = CebotSearchProvider(
            descriptor_fields=cebot_fields,
            api_key=api_key,
        )
    except Exception:  # noqa: BLE001 — a malformed descriptor must not crash DI
        _log.warning("web_search.cebot_provider.build_failed", exc_info=True)
        return None

    registry = SearchProviderRegistry()
    registry.register(_CEBOT_SERVICE_ID, provider, default=True)
    return registry
