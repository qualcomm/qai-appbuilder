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

#: Field added to each provider config telling the UI HOW that provider is
#: authenticated. Two values today:
#:
#: * ``"api_key"`` — the usual cloud provider: the user supplies a static key,
#:   which lands in the SecretStore. ``has_api_key == False`` here genuinely
#:   means "unusable until the user sets a key" → prompt for one.
#: * ``"sso"`` — the credential is minted at runtime from the signed-in
#:   identity (the QAI Service token pool exchanges the Okta ``id_token`` for a
#:   short-lived per-user JWT; see ``interfaces/http/auth/qai_service_token``).
#:   There is NO key to store, so ``has_api_key`` is ALWAYS ``False`` for such a
#:   provider and must NOT be read as "needs configuration" — asking the user
#:   for a key would be asking for something that does not exist.
#:
#: Why a field rather than the client hardcoding the pool's provider id: the id
#: is deployment data (it comes from the factory seed), so a client that
#: pattern-matched on it would silently break if the seed were renamed. The
#: backend already knows which credential path a provider uses, so it is the
#: honest place to state the auth mode.
_AUTH_MODE_FIELD = "auth_mode"

#: ``auth_mode`` value for a provider whose credential is a runtime, per-user
#: token derived from the SSO login rather than a stored key.
AUTH_MODE_SSO = "sso"

#: ``auth_mode`` value for a provider authenticated by a stored static key.
#: The default for every provider that is not explicitly keyless.
AUTH_MODE_API_KEY = "api_key"

#: Reserved provider id of the QAI Service token pool. MUST match
#: ``apps/api/_model_resolver_bridge.QAI_SERVICE_PROVIDER_ID`` (the runtime
#: credential path) and the ``providers`` key the factory seed writes
#: (``factory/_source/cloud_models.json``) — those two plus this line are the
#: only places the pool is named, and they have to agree.
#:
#: Duplicated rather than imported because this module is ``qai.model_catalog``
#: application code and ``apps.*`` is above it: the import-linter
#: ``domain-purity`` / layering contracts forbid reaching upward. A test pins
#: the two literals together so they cannot drift.
QAI_SERVICE_PROVIDER_ID = "qai-service"


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

    Every config additionally carries an ``auth_mode`` field stating HOW the
    provider is authenticated (``"api_key"`` or ``"sso"``). This is emitted
    unconditionally — it is derived from the provider identity, not from any
    secret — so a client can tell a provider that *needs* a key from one that
    can never have one. The QAI Service token pool is ``"sso"``: its bearer is
    a short-lived per-user JWT minted from the SSO login, so its
    ``has_api_key`` is permanently ``False`` and prompting for a key would be
    a dead end (the bug this field exists to prevent).
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
        provider_id = masked.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return masked
        # ``auth_mode`` is identity-derived, so it is emitted even without a
        # SecretStore (a hand-rolled test container / minimal deployment still
        # needs the client to know the pool is keyless).
        fields: dict[str, Any] = {
            _AUTH_MODE_FIELD: _auth_mode_for(provider_id),
        }
        # ``has_api_key`` requires a store to probe; omitted otherwise, which
        # the client treats as "unknown" (field is optional).
        if self._secret_store is not None:
            fields[_HAS_API_KEY_FIELD] = self._key_exists(provider_id)
        config = masked.get("config")
        masked = dict(masked)
        if isinstance(config, dict):
            masked["config"] = {**config, **fields}
        else:
            masked.update(fields)
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


def _auth_mode_for(provider_id: str) -> str:
    """Return the ``auth_mode`` for ``provider_id``.

    Identified by the reserved provider id rather than a config flag — the same
    reasoning as ``_model_resolver_bridge._is_service_token_provider``: the id
    is what the factory seed writes and what the model rows reference, so it is
    the single point where the credential path and the catalog agree. A user
    cannot rename a provider into (or out of) keyless auth by editing a config
    document.
    """
    if provider_id == QAI_SERVICE_PROVIDER_ID:
        return AUTH_MODE_SSO
    return AUTH_MODE_API_KEY


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


__all__ = [
    "AUTH_MODE_API_KEY",
    "AUTH_MODE_SSO",
    "QAI_SERVICE_PROVIDER_ID",
    "ListProviderConfigsUseCase",
]
