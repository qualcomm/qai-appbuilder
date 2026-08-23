# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``StartServiceUseCase`` — start the local inference daemon."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from qai.model_runtime.application.ports import (
    InferenceServicePort,
    ServiceConfigRepositoryPort,
)
from qai.platform.persistence.secrets import SecretStore

# SecretStore namespaces for cloud api_keys (must match get/save_service_config).
_CLOUD_MODEL_SECRET_SVC = "qai.cloud.cloud_model"
_API_KEY_KEY = "api_key"

# cloud_model.base_url values that indicate the user has not yet configured
# the cloud endpoint (placeholder shipped in the default service_config.json).
_PLACEHOLDER_BASE_URL_MARKERS = (
    "your-api-endpoint.example.com",
    "example.com",
)


def _cloud_model_base_url_is_unconfigured(base_url: str) -> bool:
    """Return True when *base_url* is empty or still the factory placeholder."""
    url = (base_url or "").strip()
    if not url:
        return True
    return any(marker in url for marker in _PLACEHOLDER_BASE_URL_MARKERS)


def _secret_key_exists(secret_store: SecretStore, service: str, key: str) -> bool:
    """Return True when *key* is present under *service* in the secret store.

    Tries ``exists()`` first, then ``get()`` as a fallback to guard against
    ``_FallbackSecretStore`` silently returning False from ``exists()`` on
    arm64 Windows where keyring reads can fail after a successful write.
    """
    try:
        if secret_store.exists(service, key):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(secret_store.get(service, key))
    except Exception:  # noqa: BLE001
        return False


def _cloud_model_misconfigured_fields(
    cloud_cfg: dict,
    secret_store: SecretStore | None,
) -> list[str]:
    """Return a list of unconfigured field names for an enabled cloud_model block.

    Checks ``base_url`` (empty / placeholder) and ``api_key`` (empty in both
    the JSON document and the SecretStore). Returns an empty list when
    everything looks configured.
    """
    bad: list[str] = []
    if _cloud_model_base_url_is_unconfigured(cloud_cfg.get("base_url", "")):
        bad.append("base_url")

    # api_key is stripped from the JSON document on save (stored in SecretStore).
    # Check the SecretStore first; fall back to the on-doc value for installs
    # that have not yet been migrated to SecretStore storage.
    api_key_present = False
    if secret_store is not None:
        api_key_present = _secret_key_exists(
            secret_store, _CLOUD_MODEL_SECRET_SVC, _API_KEY_KEY
        )
    if not api_key_present:
        # Fallback: check the raw JSON value (may still be present on first
        # save if the SecretStore write failed, or on legacy installs).
        api_key_present = bool((cloud_cfg.get("api_key") or "").strip())
    if not api_key_present:
        bad.append("api_key")
    return bad


class StartServiceUseCase:
    """Start the inference daemon, optionally loading a model.

    When *config_repository* and *genie_root_provider* are supplied, the use
    case reads service_config.json before spawning the daemon and emits a
    ``warnings`` field in the result when ``cloud_model`` is enabled but
    ``base_url`` or ``api_key`` has not been configured.  The daemon is
    started regardless — the warning is advisory so operators can configure the
    cloud endpoint without blocking the local inference path.
    """

    def __init__(
        self,
        *,
        service: InferenceServicePort,
        config_repository: ServiceConfigRepositoryPort | None = None,
        genie_root_provider: Callable[[], Awaitable[str]] | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self._service = service
        self._config_repository = config_repository
        self._genie_root_provider = genie_root_provider
        self._secret_store = secret_store

    async def execute(
        self,
        *,
        model_name: str | None = None,
        port: int | None = None,
        loglevel: int | None = None,
    ) -> dict[str, Any]:
        warnings: list[str] = []

        if self._config_repository is not None and self._genie_root_provider is not None:
            try:
                genie_root = await self._genie_root_provider()
                active_path = self._config_repository.resolve_active_path(genie_root)
                cfg = self._config_repository.load(path=active_path)
                cloud_cfg = cfg.get("cloud_model", {})
                if cloud_cfg.get("enabled", False):
                    bad_fields = _cloud_model_misconfigured_fields(
                        cloud_cfg, self._secret_store
                    )
                    if bad_fields:
                        fields_str = " / ".join(f"cloud_model.{f}" for f in bad_fields)
                        warnings.append(
                            f"{fields_str} 尚未配置。"
                            " 当端侧模型无法处理复杂问题时，智能路由将无法转发至云端模型。"
                            " 请在 GenieAPIService 配置页面填写实际的云端 endpoint URL、"
                            "api_key 和 model 字段，然后重启服务以使配置生效。"
                        )
            except Exception:  # noqa: BLE001 — config read is advisory; never block start
                pass

        await self._service.start(
            model_name=model_name, port=port, loglevel=loglevel
        )

        result: dict[str, Any] = {"status": "starting"}
        if warnings:
            result["warnings"] = warnings
        return result


__all__ = ["StartServiceUseCase"]
