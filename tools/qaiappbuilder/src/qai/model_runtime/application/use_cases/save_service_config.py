# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``SaveServiceConfigUseCase`` — persist service_config.json + api_keys.

Encapsulates the write side of the GenieAPIService config surface that
previously lived inline in ``interfaces/http/routes/model_runtime.py``:

1. Resolve the active config path via an injected ``genie_root`` provider +
   the :class:`ServiceConfigRepositoryPort` (same logic as the read path).
2. Extract the two cloud ``api_key`` fields and mirror them to the platform
   :class:`SecretStore` (so the UI read path can detect their presence).
3. Write the complete document — including plaintext ``api_key`` values —
   to ``service_config.json``.  GenieAPIService.exe is a standalone C++
   process that reads only this file; it has no access to the Python
   SecretStore.  Keeping the key in the JSON is therefore a hard
   requirement for the service to authenticate against the cloud endpoint.

SecretStore failure is logged but never fatal: the document still
persists with the plaintext key so GenieAPIService.exe can use it.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from qai.model_runtime.application.ports import ServiceConfigRepositoryPort
from qai.model_runtime.domain.service_config import deep_merge_defaults
from qai.platform.errors import PreconditionFailedError
from qai.platform.persistence.secrets import SecretStore

logger = logging.getLogger("qai.model_runtime.application.save_service_config")

# SecretStore namespaces for the two cloud api_keys. These MUST match the
# namespaces the chat cloud-inference link reads from
# (``apps/api/_model_resolver_bridge.py`` ``_LEGACY_SECRET_FALLBACKS`` and the
# v1->v2 migrator, which align with V1 ``global::cloud_model`` semantics).
# Storing under any other namespace means the key is written but never read.
_CLOUD_MODEL_SECRET_SVC = "qai.cloud.cloud_model"
_ENTERPRISE_CLOUD_SECRET_SVC = "qai.cloud.enterprise_cloud_model"
_API_KEY_KEY = "api_key"
_MASK = "****"

# Error code surfaced when a save is attempted while GenieAPIService is not
# installed (no exe-dir config file to write). Mapped to HTTP 412 by the
# unified error handler.
_NOT_INSTALLED_CODE = "model_runtime.service_not_installed"


class SaveServiceConfigUseCase:
    """Persist service_config.json; api_keys also mirrored to SecretStore."""

    def __init__(
        self,
        *,
        repository: ServiceConfigRepositoryPort,
        secret_store: SecretStore,
        genie_root_provider: Callable[[], Awaitable[str]],
    ) -> None:
        self._repository = repository
        self._secret_store = secret_store
        self._genie_root_provider = genie_root_provider

    async def execute(self, config: dict[str, Any]) -> dict[str, Any]:
        genie_root = await self._genie_root_provider()
        active_path = self._repository.resolve_active_path(genie_root)

        # Single source of truth: the config file next to GenieAPIService.exe.
        # When the service is not installed there is no authoritative file to
        # write — fail fast so a save never half-applies.
        if not active_path:
            raise PreconditionFailedError(
                _NOT_INSTALLED_CODE,
                "GenieAPIService is not installed; install it before "
                "configuring the service.",
            )

        data = copy.deepcopy(config)
        secret_errors: dict[str, str] = {}

        # Resolve the real api_key values to write:
        # - If the submitted value is "****" (unchanged mask), read the
        #   current value from SecretStore so we don't erase a saved key.
        # - If the submitted value is a real key, use it as-is.
        # - If empty, keep empty (user explicitly cleared the field).
        cloud_key = self._resolve_key(
            _CLOUD_MODEL_SECRET_SVC,
            data.get("cloud_model", {}).get("api_key"),
        )
        enterprise_key = self._resolve_key(
            _ENTERPRISE_CLOUD_SECRET_SVC,
            data.get("enterprise_cloud_model", {}).get("api_key"),
        )

        # Mirror real keys to SecretStore (so UI exists() check works).
        if cloud_key:
            self._store_key(
                _CLOUD_MODEL_SECRET_SVC, cloud_key, "cloud_model", secret_errors
            )
        if enterprise_key:
            self._store_key(
                _ENTERPRISE_CLOUD_SECRET_SVC,
                enterprise_key,
                "enterprise_cloud_model",
                secret_errors,
            )

        # Write api_key back into the document so GenieAPIService.exe can
        # read it. The file lives in the per-user data directory and is
        # protected by OS-level ACLs (same protection as before this change).
        if "cloud_model" in data:
            data["cloud_model"]["api_key"] = cloud_key or ""
        if "enterprise_cloud_model" in data:
            data["enterprise_cloud_model"]["api_key"] = enterprise_key or ""

        # Read-modify-write: deep-merge the submitted data on top of the
        # existing document, then write the result.
        existing = self._repository.load(path=active_path)
        merged = deep_merge_defaults(existing, data)
        self._repository.save(merged, path=active_path)

        result: dict[str, Any] = {"status": "saved"}
        if secret_errors:
            result["secret_store_errors"] = secret_errors
        return result

    def _resolve_key(self, service: str, submitted: str | None) -> str | None:
        """Return the real key to persist.

        - Non-empty value that is not ``"****"`` → use as-is (new key from user).
        - ``"****"`` or ``None`` / ``""`` → read existing value from SecretStore.
          The empty-string case guards against the UI sending "" because
          GetServiceConfigUseCase returned "" due to a SecretStore read failure;
          we preserve the stored key rather than overwriting it with empty.
        """
        if submitted and submitted != _MASK:
            return submitted
        try:
            existing = self._secret_store.get(service, _API_KEY_KEY)
            return existing or None
        except Exception:  # noqa: BLE001
            return None

    def _store_key(
        self,
        service: str,
        value: str | None,
        section: str,
        errors: dict[str, str],
    ) -> None:
        """Mirror *value* to SecretStore; log but don't raise on failure."""
        if not value or value == _MASK:
            return
        try:
            self._secret_store.set(service, _API_KEY_KEY, value)
        except Exception as exc:  # noqa: BLE001 — non-fatal; key is already in JSON
            logger.warning(
                "Failed to mirror %s api_key to SecretStore: %s", section, exc
            )
            errors[section] = str(exc)


__all__ = ["SaveServiceConfigUseCase"]
