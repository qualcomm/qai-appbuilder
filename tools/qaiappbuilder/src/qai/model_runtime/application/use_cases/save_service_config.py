# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``SaveServiceConfigUseCase`` — persist service_config.json + api_keys.

Encapsulates the write side of the GenieAPIService config surface that
previously lived inline in ``interfaces/http/routes/model_runtime.py``:

1. Resolve the active config path via an injected ``genie_root`` provider +
   the :class:`ServiceConfigRepositoryPort` (same logic as the read path).
2. Extract the two cloud ``api_key`` fields, store them in the platform
   :class:`SecretStore`, and strip them from the JSON document so plaintext
   keys are never written to disk (§3.3 credentials rule).
3. Strip any stale ``api_key`` from the existing document, then deep-merge
   the submitted document on top and persist.

SecretStore failure is no longer silently swallowed: it is logged AND
surfaced in the response (``secret_store_errors``) so a failed key save is
not lost — the config document still persists (best-effort, V1 parity), but
the caller learns the key did not stick.
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
    """Persist service_config.json; api_keys go to the SecretStore."""

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
        # write — fail fast (before touching the SecretStore) so a save never
        # half-applies (keys stored, document refused) and no zombie fallback
        # is created. The frontend disables the config entrypoints as the
        # primary guard; this is the hard backend guarantee.
        if not active_path:
            raise PreconditionFailedError(
                _NOT_INSTALLED_CODE,
                "GenieAPIService is not installed; install it before "
                "configuring the service.",
            )

        data = copy.deepcopy(config)
        secret_errors: dict[str, str] = {}

        # Extract and store api_keys in SecretStore (never write to JSON).
        cloud_key = data.get("cloud_model", {}).pop("api_key", None)
        self._store_key(
            _CLOUD_MODEL_SECRET_SVC, cloud_key, "cloud_model", secret_errors
        )
        enterprise_key = data.get("enterprise_cloud_model", {}).pop("api_key", None)
        self._store_key(
            _ENTERPRISE_CLOUD_SECRET_SVC,
            enterprise_key,
            "enterprise_cloud_model",
            secret_errors,
        )

        # Strip any stale api_key from the submitted data so the merge never
        # reintroduces a plaintext key into the document.
        data.get("cloud_model", {}).pop("api_key", None)
        data.get("enterprise_cloud_model", {}).pop("api_key", None)

        # Read-modify-write (V1 parity): load the existing document, strip
        # its api_key fields too, deep-merge the submitted data on top, then
        # write the result. Stripping both sides guarantees no ``api_key``
        # ever lands on disk in plaintext.
        existing = self._repository.load(path=active_path)
        existing.get("cloud_model", {}).pop("api_key", None)
        existing.get("enterprise_cloud_model", {}).pop("api_key", None)
        merged = deep_merge_defaults(existing, data)
        self._repository.save(merged, path=active_path)

        result: dict[str, Any] = {"status": "saved"}
        if secret_errors:
            # Surface the failure rather than silently dropping it: the
            # document persisted but the key did not reach the SecretStore.
            result["secret_store_errors"] = secret_errors
        return result

    def _store_key(
        self,
        service: str,
        value: str | None,
        section: str,
        errors: dict[str, str],
    ) -> None:
        """Store *value* under *service* if it is a real (non-masked) key."""
        if not value or value == _MASK:
            return
        try:
            self._secret_store.set(service, _API_KEY_KEY, value)
        except Exception as exc:  # noqa: BLE001 — non-fatal for config save
            logger.warning(
                "Failed to store %s api_key in SecretStore: %s", section, exc
            )
            errors[section] = str(exc)


__all__ = ["SaveServiceConfigUseCase"]
