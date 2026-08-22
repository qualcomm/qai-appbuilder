# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Apps-layer bridge: build the web-search provider registry.

The ``web_search`` chat/coding tool is backed by a pluggable
:class:`SearchProviderRegistry`. Its providers now split across two layers:

* the multi-engine ``web`` provider (:class:`IndependentSearchProvider`) lives
  in the shared-kernel package ``qai.platform.web_search`` and is registered on
  BOTH editions — external builds import it just fine;
* the intranet ``cebot`` provider (:class:`CebotSearchProvider`) lives in
  ``qai.platform.edition.web_search`` — a package physically excluded from
  external artifacts — so it is registered ONLY when ``settings.is_internal``,
  via a LOCAL import guarded by ``try/except`` (a stripped external tree lacks
  the package, so the import fails and cebot is simply skipped).

So the registry is non-empty on both editions (at least ``web``); it is
``None`` only in the extreme case where even ``web`` fails to construct. The
default provider is ``cebot`` when internal and cebot registered successfully
(unchanged behaviour); otherwise it is ``web``.

The engine roster + tuning come from ``qai.platform.web_search.config``
(shipped to both editions); the per-service ``cebot`` api_key comes from the
SecretStore (namespace ``qai.model_catalog.provider``).
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


def _secret_get(store: SecretStore | None, key: str) -> str | None:
    if store is None:
        return None
    try:
        return store.get(_PROVIDER_SECRET_SERVICE, key)
    except Exception:  # noqa: BLE001 — any failure ⇒ no usable credential
        return None


def build_search_registry(*, container: Any) -> Any | None:
    """Build the web-search provider registry, or ``None`` if empty.

    Always registers the multi-engine ``web`` provider (both editions); when
    ``settings.is_internal`` it additionally registers the intranet ``cebot``
    provider (LOCAL, guarded import — absent on external). Returns ``None`` only
    when even ``web`` fails to construct (so there is no backend to offer).

    The registry is suitable to pass as ``search_registry=`` to
    ``build_default_tool_handlers`` (which then registers the ``web_search``
    tool) and to the chat-side web-search tool bridge.
    """
    # The registry + neutral abstraction ship to both editions.
    try:
        from qai.platform.web_search import SearchProviderRegistry
    except Exception:  # pragma: no cover — shared kernel is always present
        return None

    settings = getattr(container, "settings", None)
    is_internal = settings is not None and bool(
        getattr(settings, "is_internal", False)
    )
    secret_store = getattr(container, "secret_store", None)

    registry = SearchProviderRegistry()

    # ``web`` — available on both editions.
    _register_independent_web(registry, container, secret_store)

    # ``cebot`` — internal-only, default when present.
    if is_internal:
        _register_cebot(registry, secret_store)

    # ``web`` was registered first, so it is the default automatically unless
    # ``cebot`` registered with ``default=True`` (internal) and overrode it.
    if len(registry) == 0:
        return None
    return registry


def _register_cebot(
    registry: Any, secret_store: SecretStore | None
) -> None:
    """Register the intranet CEBot provider (internal-only, non-default).

    LOCAL, guarded imports: ``qai.platform.edition`` is physically excluded from
    external artifacts, so on a stripped tree the import fails and this is a
    clean no-op (``web`` stays the only provider).
    """
    try:
        from qai.platform.edition import get_query_services
        from qai.platform.edition.web_search import CebotSearchProvider
    except Exception:  # pragma: no cover - package excluded on external
        return

    descriptors_cfg = get_query_services()
    cebot_fields = descriptors_cfg.get(_CEBOT_SERVICE_ID)
    if not cebot_fields:
        return

    api_key = _secret_get(secret_store, _CEBOT_SERVICE_ID)
    try:
        provider = CebotSearchProvider(
            descriptor_fields=cebot_fields,
            api_key=api_key,
        )
    except Exception:  # noqa: BLE001 — a malformed descriptor must not crash DI
        _log.warning("web_search.cebot_provider.build_failed", exc_info=True)
        return

    registry.register(_CEBOT_SERVICE_ID, provider)


def _register_independent_web(
    registry: Any, container: Any, secret_store: SecretStore | None
) -> None:
    """Register the multi-engine ``web`` provider (both editions).

    Best-effort: a build/import failure is logged and skipped. The roster +
    tuning come from ``qai.platform.web_search.config`` (shipped externally),
    so this works without the edition package.
    """
    try:
        from qai.platform.web_search.config import (
            get_independent_search_config,
            get_search_engines,
        )
        from qai.platform.web_search.independent import IndependentSearchProvider
        from qai.platform.web_search.independent.quota import QuotaStore
        from qai.platform.web_search.independent.scoring import ScoreStore

        database = getattr(container, "database", None)
        if database is None:
            _log.warning("web_search.independent.no_database")
            return
        engine_specs = get_search_engines()
        # ``enabled_by_default = false`` in search_config.toml means "off until
        # the user turns it on". The scoring store owns that decision because
        # it is the seam that decides participation; pass the ids so a fresh
        # install seeds them ``forced_off`` instead of ``auto``.
        default_off = frozenset(
            str(spec.get("engine_id"))
            for spec in engine_specs
            if not spec.get("enabled_by_default", True)
        )
        score_store = ScoreStore(database, default_off_engines=default_off)
        quota_store = QuotaStore(database)
        provider = IndependentSearchProvider(
            engine_specs_raw=engine_specs,
            independent_cfg=get_independent_search_config(),
            score_store=score_store,
            quota_store=quota_store,
            secret_store=secret_store,
        )
        registry.register("web", provider, default=True)
        _log.info(
            "web_search.independent.registered",
            extra={"engine_ids": [s.get("engine_id") for s in engine_specs]},
        )
    except Exception:  # noqa: BLE001 — never break chat startup
        _log.warning("web_search.independent.register_failed", exc_info=True)
