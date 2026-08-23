# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``GetServiceConfigUseCase`` — read service_config.json (api_key masked).

Encapsulates the read side of the GenieAPIService config surface that
previously lived inline in ``interfaces/http/routes/model_runtime.py``:

1. Resolve the configured GenieAPIService install root (``genie_root``)
   via an injected async callable (the route no longer reaches into
   ``user_prefs`` directly).
2. Resolve the active config path + load the merged document via the
   :class:`ServiceConfigRepositoryPort`.
3. Mask ``api_key`` fields using the platform :class:`SecretStore`
   (presence -> ``"****"``, absence -> ``""``), with a graceful fallback to
   the on-document value when the keyring lookup raises.
4. Return the document plus V1-parity ``meta`` (``using_default_config`` /
   ``config_file_path`` / ``genie_root_configured``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from qai.model_runtime.application.ports import ServiceConfigRepositoryPort
from qai.platform.persistence.secrets import SecretStore

# SecretStore namespaces / key for the two cloud api_keys. These MUST match
# the namespaces the chat cloud-inference link reads from
# (``apps/api/_model_resolver_bridge.py`` ``_LEGACY_SECRET_FALLBACKS`` and the
# v1->v2 migrator, which align with V1 ``global::cloud_model`` semantics) and
# the write side in ``save_service_config.py`` — otherwise a saved key would
# read back as absent here.
_CLOUD_MODEL_SECRET_SVC = "qai.cloud.cloud_model"
_ENTERPRISE_CLOUD_SECRET_SVC = "qai.cloud.enterprise_cloud_model"
_API_KEY_KEY = "api_key"

_logger = logging.getLogger("qai.model_runtime.application.get_service_config")


class GetServiceConfigUseCase:
    """Return service_config.json with ``api_key`` values masked."""

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

    async def execute(self) -> dict[str, Any]:
        genie_root = await self._genie_root_provider()
        active_path = self._repository.resolve_active_path(genie_root)
        cfg = self._repository.load(path=active_path)

        secret = self._secret_store
        has_cloud_key = self._key_exists(secret, _CLOUD_MODEL_SECRET_SVC, _API_KEY_KEY)
        _logger.info(
            "get_service_config: genie_root=%r active_path=%r "
            "has_cloud_key_from_secret=%s json_api_key_len=%d",
            genie_root,
            active_path,
            has_cloud_key,
            len(cfg.get("cloud_model", {}).get("api_key", "") or ""),
        )
        if not has_cloud_key:
            has_cloud_key = bool(cfg.get("cloud_model", {}).get("api_key", ""))
        has_enterprise_key = self._key_exists(
            secret, _ENTERPRISE_CLOUD_SECRET_SVC, _API_KEY_KEY
        )
        if not has_enterprise_key:
            has_enterprise_key = bool(
                cfg.get("enterprise_cloud_model", {}).get("api_key", "")
            )

        if "cloud_model" in cfg:
            cfg["cloud_model"]["api_key"] = "****" if has_cloud_key else ""
        if "enterprise_cloud_model" in cfg:
            cfg["enterprise_cloud_model"]["api_key"] = (
                "****" if has_enterprise_key else ""
            )

        # ``active_path`` is empty when GenieAPIService is not installed (the
        # config above is then the in-memory defaults, for read-only display).
        # Surface an empty ``config_file_path`` in that case rather than a
        # misleading cwd-resolved path.
        config_file_path = (
            str(Path(active_path).resolve()) if active_path else ""
        )
        return {
            "config": cfg,
            "meta": {
                # using_default_config = True when genie_root is not set,
                # matching V1 main.py:4069 exactly.
                "using_default_config": not bool(genie_root),
                "config_file_path": config_file_path,
                "genie_root_configured": bool(genie_root),
            },
        }

    @staticmethod
    def _key_exists(secret: SecretStore, service: str, key: str) -> bool:
        """Return True when a key is present in the secret store.

        Tries ``exists()`` first; on failure (or False) also tries ``get()``
        as a second probe.  This guards against ``_FallbackSecretStore``
        silently swallowing keyring errors inside ``exists()`` and returning
        False even when the value is actually stored in the OS keyring.
        """
        try:
            if secret.exists(service, key):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            value = secret.get(service, key)
            return bool(value)
        except Exception:  # noqa: BLE001
            return False


__all__ = ["GetServiceConfigUseCase"]
