# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer cross-context bridge: chat model resolution → model_catalog + secrets.

Block-2 routing fix
-------------------

The chat :class:`~qai.chat.adapters.model_resolver.ProviderAwareModelResolver`
needs to answer "which cloud provider owns this model id, and what are its
``base_url`` + ``api_key``?" so a user who selects a cloud model
(``cloud_llm`` / ``provider_b`` / ...) has their chat request routed to the
correct upstream instead of the single default endpoint baked into the chat
settings.

Per the import-linter ``context-isolation`` contract, ``qai.chat.*`` may
NEVER import ``qai.model_catalog.*`` or ``qai.platform.persistence.secrets``
directly.  This module — at the ``apps/api`` composition root — is the only
place that legitimately sees the chat-side abstraction
(:class:`~qai.chat.application.ports.ProviderConfigLookupPort`) together with
the model_catalog provider registry and the platform ``SecretStore``.

Resolution strategy
--------------------

1. ``ProviderRegistryPort.list_provider_configs()`` returns one row per
   configured provider::

       {"provider_id": "cloud_llm",
        "config": {"base_url": "https://...",
                   "models": [{"model_id": "provider::model-...", ...}, ...]}}

   The ``base_url`` lives in the (non-secret) provider config; the
   ``models`` list enumerates which model ids that provider serves.

2. The bridge finds the provider whose ``models`` list contains the
   requested ``model_id`` and reads its ``base_url``.

3. The provider's ``api_key`` is fetched from the OS-keyring-backed
   :class:`SecretStore` (§3.3 — credentials never live in the provider
   config plaintext / logs).  The lookup tries, in order, the dedicated
   per-provider record then the legacy cloud-model records, so existing
   installs keep working:

       service = "qai.model_catalog.provider", key = "<provider_id>"
       service = "qai.cloud.cloud_model",      key = "api_key"
       service = "qai.cloud.enterprise_cloud_model", key = "api_key"

   A missing credential is NOT an error — the endpoint is returned with
   ``api_key=None`` (keyless gateway / not-yet-provisioned), matching the
   :class:`ProviderConfigLookupPort` contract.

The bridge never raises on a miss; it returns ``None`` so the resolver can
fall back to the settings default endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qai.platform.logging import get_logger

from qai.chat.application.ports import (
    ProviderConfigLookupPort,
    ProviderEndpoint,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence.secrets import SecretStore

logger = get_logger(__name__)

__all__ = ["ModelCatalogProviderLookupBridge"]


#: SecretStore service namespace for per-provider cloud-inference api keys.
#: Key is the free-form ``provider_id`` (``cloud_llm`` / ``provider_b`` / ...).
_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"

#: Legacy SecretStore records seeded by the v1→v2 migrator
#: (``service_config.py:_SECRET_LOCATIONS``).  Tried as a fallback so installs
#: migrated before the per-provider convention still resolve a key.
_LEGACY_SECRET_FALLBACKS: tuple[tuple[str, str], ...] = (
    ("qai.cloud.cloud_model", "api_key"),
    ("qai.cloud.enterprise_cloud_model", "api_key"),
)

#: Reserved provider id for the QAI Service token pool. The factory seed
#: (``factory/_source/cloud_models.json``) writes this id and the pool's model
#: rows reference it, so it is the single point where the credential path and
#: the catalog agree. Authentication for this provider is NOT a stored key: it
#: is the signed-in user's short-lived broker JWT (see :func:`_resolve_service_jwt`).
QAI_SERVICE_PROVIDER_ID = "qai-service"


def _resolve_service_jwt() -> str | None:
    """Return the held QAI Service JWT, or ``None`` when there is none.

    The token lives in ``interfaces.http.auth.qai_service_token``, filled at
    SSO login. Importing it here is the same ``apps → interfaces`` direction
    ``apps/api/main.py`` already uses for the auth middleware, and it keeps
    the chat context clean: ``qai.chat.*`` never learns that a token pool
    exists — it only ever sees a resolved ``api_key`` on a ProviderEndpoint.

    Guarded so a CLI / test container that never mounts the HTTP auth surface
    degrades to "no credential" instead of an ImportError on the chat path.

    FUTURE (edition cutover — see :class:`QaiServiceSettings` docstring): the
    token pool is meant for external developers only. When that switch is
    thrown, THIS is the one place to gate — return ``None`` early when
    ``settings.is_internal``. No JWT ⇒ the pool's models resolve no credential
    ⇒ they drop out of chat and the sidebar gauge hides itself, so a single
    branch retires the capability coherently front to back. It is deliberately
    NOT gated today: a dev source tree always self-reports ``internal``, so
    gating now would make the feature untestable where it is built.
    """
    try:
        from interfaces.http.auth.qai_service_token import get_service_jwt
    except ImportError:  # pragma: no cover — HTTP auth surface not mounted
        return None
    return get_service_jwt()



class ModelCatalogProviderLookupBridge(ProviderConfigLookupPort):
    """Resolve a model id to its provider endpoint via model_catalog + secrets.

    Duck-typed inputs keep the bridge free of a hard ``qai.model_catalog``
    import at module scope:

    * ``provider_registry`` — any object exposing
      ``async list_provider_configs() -> list[dict]`` (the model_catalog
      :class:`ProviderRegistryPort`).
    * ``secret_store`` — the platform :class:`SecretStore` (``get`` /
      ``exists``).  Optional: when ``None`` the bridge still routes by
      ``base_url`` but always returns ``api_key=None``.
    """

    __slots__ = ("_provider_registry", "_secret_store")

    def __init__(
        self,
        *,
        provider_registry: Any,
        secret_store: "SecretStore | None" = None,
    ) -> None:
        self._provider_registry = provider_registry
        self._secret_store = secret_store

    async def lookup_for_model(
        self, model_id: str
    ) -> ProviderEndpoint | None:
        if not model_id:
            return None
        try:
            rows = await self._provider_registry.list_provider_configs()
        except Exception as exc:  # noqa: BLE001 — never crash chat routing
            logger.warning(
                "chat.model_resolver_bridge.list_providers_failed",
                error=str(exc),
            )
            return None

        for row in rows or ():
            if not isinstance(row, dict):
                continue
            provider_id = row.get("provider_id")
            config = row.get("config")
            if not isinstance(provider_id, str) or not isinstance(config, dict):
                continue
            if not self._config_serves_model(config, model_id):
                continue
            base_url = config.get("base_url")
            if not isinstance(base_url, str) or not base_url:
                # Provider claims the model but has no usable endpoint;
                # treat as a miss so the resolver falls back cleanly.
                return None
            api_key = self._resolve_api_key(provider_id)
            return ProviderEndpoint(
                provider_id=provider_id,
                base_url=base_url,
                api_key=api_key,
                api_model_id=self._api_model_id_for(config, model_id),
                params=self._params_for(config, model_id),
                # This bridge is the only layer that knows a provider
                # authenticates via SSO rather than a stored key. Marking it
                # here lets the transport tell "sign in again" apart from
                # "configure a key" when the credential is missing — for the
                # pool the latter is a dead end, because there is no key a
                # user could ever paste.
                is_sso=self._is_service_token_provider(provider_id),
            )
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _config_serves_model(config: dict[str, Any], model_id: str) -> bool:
        raw_models = config.get("models")
        if not isinstance(raw_models, list):
            return False
        for raw in raw_models:
            if isinstance(raw, dict) and raw.get("model_id") == model_id:
                return True
        return False

    @staticmethod
    def _api_model_id_for(config: dict[str, Any], model_id: str) -> str | None:
        """Return the ``api_model_id`` wire override for ``model_id``.

        V1 parity:
        a cloud model entry may carry an optional ``api_model_id`` that is
        the name actually sent to the provider's API (e.g. dated
        ``claude-sonnet-4-20250514``) while ``model_id`` stays a stable
        display id. Returns ``None`` when absent/empty so the resolver
        sends ``model_id`` verbatim.
        """
        raw_models = config.get("models")
        if not isinstance(raw_models, list):
            return None
        for raw in raw_models:
            if not isinstance(raw, dict) or raw.get("model_id") != model_id:
                continue
            wire = raw.get("api_model_id")
            if isinstance(wire, str) and wire:
                return wire
            return None
        return None

    @staticmethod
    def _params_for(
        config: dict[str, Any], model_id: str
    ) -> dict[str, Any] | None:
        """Return the per-model ``params`` constraint dict for ``model_id``.

        The cloud model catalog config (``cloud_models.json``) may attach a
        ``params`` object to a model entry declaring sampling-parameter
        constraints the user configured in Settings → Cloud Models::

            {"temperature": {"supported": false},
             "top_p": {"supported": false},
             "thought_signature": {"required": true}}

        Returns ``None`` when the entry has no (non-empty) ``params`` so the
        chat resolver falls back to the family-regex defaults.
        """
        raw_models = config.get("models")
        if not isinstance(raw_models, list):
            return None
        for raw in raw_models:
            if not isinstance(raw, dict) or raw.get("model_id") != model_id:
                continue
            params = raw.get("params")
            if isinstance(params, dict) and params:
                return params
            return None
        return None

    def _resolve_api_key(self, provider_id: str) -> str | None:
        # 0) QAI Service token pool: the bearer is the signed-in user's
        #    short-lived broker JWT, minted at login from the Okta id_token —
        #    NOT a stored key. Checked FIRST and returned unconditionally
        #    (even as None) so a stale SecretStore entry for this provider can
        #    never be sent to the pool: the pool would reject it, and worse, a
        #    real key for some other provider must never leak to this host.
        #    None here surfaces as "no credential" and the request fails with
        #    a clean auth error telling the user to sign in again.
        if self._is_service_token_provider(provider_id):
            return _resolve_service_jwt()
        if (store := self._secret_store) is None:
            return None
        # 1) Dedicated per-provider record.
        key = self._secret_get(store, _PROVIDER_SECRET_SERVICE, provider_id)
        if key:
            return key
        # 2) Legacy migrator-seeded records.
        for service, legacy_key in _LEGACY_SECRET_FALLBACKS:
            key = self._secret_get(store, service, legacy_key)
            if key:
                return key
        return None

    @staticmethod
    def _is_service_token_provider(provider_id: str) -> bool:
        """Whether ``provider_id`` is the QAI Service token pool.

        Identified by its reserved provider id rather than a config flag: the
        id is what the factory seed writes and what the model rows reference,
        so there is exactly one place the two can agree. A user cannot rename
        it into (or out of) the pool by editing a config document.
        """
        return provider_id == QAI_SERVICE_PROVIDER_ID

    @staticmethod
    def _secret_get(
        store: "SecretStore", service: str, key: str
    ) -> str | None:
        try:
            if not store.exists(service, key):
                return None
            value = store.get(service, key)
        except Exception:  # noqa: BLE001
            # SecretStore raises NotFoundError / ValueError on bad input;
            # any failure means "no usable credential" — never propagate.
            return None
        return value or None
