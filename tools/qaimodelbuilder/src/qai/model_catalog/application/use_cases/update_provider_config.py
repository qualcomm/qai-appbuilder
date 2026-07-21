# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``UpdateProviderConfigUseCase`` -- save / replace a provider config.

V1 parity (api_key never lands in the config document)
-------------------------------------------------------
V1 (``backend/keyring_helper.py`` + ``models_registry.save_providers``)
extracts the per-provider ``api_key`` into the OS keyring (``provider::{name}``)
and persists only the non-secret config (``base_url`` / ``models`` / ...) to
disk. The V2 ``model_catalog`` path originally skipped this extraction, so the
whole config -- including the plaintext ``api_key`` -- was written to
``kv_user_prefs`` (a regression vs V1). This use case now mirrors V1 (and V2's
own already-aligned ``SaveServiceConfigUseCase``):

1. Extract ``config["api_key"]`` -- if it is a real (non-empty, non-masked)
   value, store it in the platform :class:`SecretStore` under the SAME
   namespace the chat cloud-inference link reads from
   (``qai.model_catalog.provider`` / ``<provider_id>``; see
   ``apps/api/_model_resolver_bridge.py``).
2. Strip ``api_key`` from the config before persisting so a plaintext key
   never reaches ``kv_user_prefs`` (§3.3 credentials rule).
3. The ``"****"`` mask (what a GET returns, see :class:`ListProviderConfigsUseCase`)
   is treated as "keep the existing key" -- it is stripped without overwriting
   the stored secret, so a round-trip GET-then-PUT never clobbers the key.

When no :class:`SecretStore` is injected (hand-rolled test containers) the use
case still strips ``api_key`` so the document stays clean; the key is simply
not persisted anywhere (the caller learns nothing was stored only if it checks).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from qai.model_catalog.application.ports import ProviderRegistryPort
from qai.model_catalog.domain.errors import ProviderConfigInvalidError
from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence.secrets import SecretStore

logger = logging.getLogger(
    "qai.model_catalog.application.update_provider_config"
)

#: SecretStore namespace for per-provider cloud-inference api keys. MUST match
#: the read path in ``apps/api/_model_resolver_bridge.py`` (``_PROVIDER_SECRET_SERVICE``)
#: so a key written here is the same key chat reads back. The free-form
#: ``provider_id`` is the key.
_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"
_API_KEY_FIELD = "api_key"
#: What a GET returns in place of a real key (see ``ListProviderConfigsUseCase``).
#: A PUT carrying the mask means "leave the stored key untouched".
_MASK = "****"


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateProviderConfigCommand:
    provider_id: str
    config: dict[str, Any]


class UpdateProviderConfigUseCase:
    """Validate + persist a provider config record (api_key -> SecretStore).

    The domain knows nothing about the inner shape of provider configs
    (different providers carry wildly different fields), so validation
    here is intentionally narrow:

    * ``provider_id`` -- non-empty, length-bounded
    * ``config`` -- must be a ``dict`` (not ``None``, not list)

    Richer validation is the registry adapter's responsibility.
    """

    def __init__(
        self,
        *,
        registry: ProviderRegistryPort,
        secret_store: "SecretStore | None" = None,
    ) -> None:
        self._registry = registry
        self._secret_store = secret_store

    async def execute(self, command: UpdateProviderConfigCommand) -> None:
        try:
            assert_non_empty(
                command.provider_id, name="UpdateProviderConfig.provider_id"
            )
            assert_max_length(
                command.provider_id,
                max_length=128,
                name="UpdateProviderConfig.provider_id",
            )
        except Exception as exc:
            raise ProviderConfigInvalidError(
                command.provider_id,
                field_errors={"provider_id": [str(exc)]},
            ) from exc

        if not isinstance(command.config, dict):
            raise ProviderConfigInvalidError(
                command.provider_id,
                field_errors={
                    "config": [
                        f"must be dict, got {type(command.config).__name__}"
                    ]
                },
            )

        # Copy so we never mutate the caller's dict, then extract + strip the
        # api_key. A real key is stored in the SecretStore; the masked
        # sentinel ("****") means "keep existing" -- strip without overwriting.
        config = dict(command.config)
        raw_key = config.pop(_API_KEY_FIELD, None)
        if isinstance(raw_key, str) and raw_key and raw_key != _MASK:
            self._store_key(command.provider_id, raw_key)

        await self._registry.save_provider_config(command.provider_id, config)

    def _store_key(self, provider_id: str, value: str) -> None:
        """Persist *value* under the per-provider SecretStore namespace.

        A SecretStore failure is logged but not fatal (V1 parity: the config
        document still persists). Without an injected store, the key is simply
        dropped -- the document stays clean either way.
        """
        if self._secret_store is None:
            logger.warning(
                "model_catalog.provider.no_secret_store",
                extra={"provider_id": provider_id},
            )
            return
        try:
            self._secret_store.set(
                _PROVIDER_SECRET_SERVICE, provider_id, value
            )
        except Exception as exc:  # noqa: BLE001 -- non-fatal for config save
            logger.warning(
                "Failed to store provider %r api_key in SecretStore: %s",
                provider_id,
                exc,
            )


__all__ = [
    "UpdateProviderConfigUseCase",
    "UpdateProviderConfigCommand",
]
