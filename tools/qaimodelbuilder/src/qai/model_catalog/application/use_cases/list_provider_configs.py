# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ListProviderConfigsUseCase`` -- enumerate provider configs (masked)."""

from __future__ import annotations

from typing import Any

from qai.model_catalog.application.ports import ProviderRegistryPort
from qai.platform.persistence.secrets import SecretStore

#: api_key field name and the mask returned in its place.
_API_KEY_FIELD = "api_key"
_MASK = "****"

#: SecretStore namespace the runtime provider read/write path uses
#: (mirrors ``UpdateProviderConfigUseCase._PROVIDER_SECRET_SERVICE`` and
#: ``apps/api/_model_resolver_bridge``). The key is the provider id.
_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"

#: Legacy migrator-seeded credential locations, checked as a fallback so the
#: ``has_api_key`` flag stays consistent with the runtime read path
#: (``apps/api/_model_resolver_bridge._resolve_api_key`` / its
#: ``_LEGACY_SECRET_FALLBACKS``). Without this, a user whose key lives only in
#: a legacy record would see a spurious "needs API key" prompt even though
#: inference would actually succeed.
_LEGACY_SECRET_FALLBACKS: tuple[tuple[str, str], ...] = (
    ("qai.cloud.cloud_model", "api_key"),
    ("qai.cloud.enterprise_cloud_model", "api_key"),
)

#: Field added to each provider config telling the UI whether an api_key is
#: set for that provider (WITHOUT ever exposing the value). Lets the client
#: distinguish "provider has models but no key yet" (→ prompt the user) from
#: "fully configured".
_HAS_API_KEY_FIELD = "has_api_key"


class ListProviderConfigsUseCase:
    """Return all provider configs (formerly ``cloud_models.json`` entries).

    api_key is never returned in plaintext (§3.3 + V1 parity,
    ``main.py:4144-4153``): if a config carries an ``api_key`` (it should not
    after :class:`UpdateProviderConfigUseCase` strips it, but a legacy row
    might), it is replaced with the ``"****"`` mask before leaving this use
    case. The real key lives only in the SecretStore.

    When a :class:`SecretStore` is injected, each returned config also carries
    a boolean ``has_api_key`` flag computed from
    ``secret_store.exists(_PROVIDER_SECRET_SERVICE, provider_id)`` — the value
    itself is NEVER surfaced. The UI uses this to prompt for a key when a
    provider ships models but has no credential yet (the internal-edition
    qgenie first-launch flow). Without a store, the flag is omitted (the
    field type is optional on the client) and behaviour is unchanged.
    """

    def __init__(
        self,
        *,
        registry: ProviderRegistryPort,
        secret_store: SecretStore | None = None,
    ) -> None:
        self._registry = registry
        self._secret_store = secret_store

    async def execute(self) -> list[dict[str, Any]]:
        rows = await self._registry.list_provider_configs()
        return [self._project_row(row) for row in rows]

    def _project_row(self, row: dict[str, Any]) -> dict[str, Any]:
        masked = _mask_row(row)
        if self._secret_store is None:
            return masked
        provider_id = masked.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return masked
        has_key = self._key_exists(provider_id)
        config = masked.get("config")
        if isinstance(config, dict):
            new_config = dict(config)
            new_config[_HAS_API_KEY_FIELD] = has_key
            masked = dict(masked)
            masked["config"] = new_config
        else:
            masked = dict(masked)
            masked[_HAS_API_KEY_FIELD] = has_key
        return masked

    def _key_exists(self, provider_id: str) -> bool:
        """Return whether a non-empty api_key is resolvable for ``provider_id``.

        Mirrors the runtime resolver's lookup order
        (``_model_resolver_bridge._resolve_api_key``): the dedicated
        per-provider record first, then the legacy migrator-seeded fallbacks.
        This keeps the UI's ``has_api_key`` flag consistent with whether
        inference would actually find a credential.

        Never raises: a broken SecretStore must not break the provider list
        (the UI degrades to "no key set", which at worst shows the prompt).
        """
        store = self._secret_store
        if store is None:
            return False
        # 1) Dedicated per-provider record.
        if self._secret_has(_PROVIDER_SECRET_SERVICE, provider_id):
            return True
        # 2) Legacy migrator-seeded records (resolver parity).
        for service, legacy_key in _LEGACY_SECRET_FALLBACKS:
            if self._secret_has(service, legacy_key):
                return True
        return False

    def _secret_has(self, service: str, key: str) -> bool:
        """True iff a non-empty secret value exists at ``(service, key)``."""
        store = self._secret_store
        if store is None:
            return False
        try:
            if not store.exists(service, key):
                return False
            value = store.get(service, key)
        except Exception:  # noqa: BLE001 — best-effort presence probe
            return False
        return isinstance(value, str) and bool(value)


def _mask_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``row`` with any plaintext api_key replaced by a mask.

    Handles both wire shapes the registry may return:

    * production adapter -- ``{"provider_id": ..., "config": {...}}``: mask
      ``config["api_key"]``.
    * flat config dict -- ``{"api_key": ..., ...}``: mask ``api_key`` directly.
    """
    if not isinstance(row, dict):
        return row
    masked = dict(row)
    config = masked.get("config")
    if isinstance(config, dict):
        masked["config"] = _mask_config(config)
    elif _API_KEY_FIELD in masked:
        masked = _mask_config(masked)
    return masked


def _mask_config(config: dict[str, Any]) -> dict[str, Any]:
    if _API_KEY_FIELD not in config:
        return config
    out = dict(config)
    raw = out.get(_API_KEY_FIELD)
    out[_API_KEY_FIELD] = _MASK if (isinstance(raw, str) and raw) else ""
    return out


__all__ = ["ListProviderConfigsUseCase"]
