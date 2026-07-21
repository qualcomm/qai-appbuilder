# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ProbeProviderUseCase`` -- verify a cloud provider's connectivity.

The config wizard (``qai config provider test`` / ``add``) needs to know
whether a freshly-configured provider actually works -- not just that the
config row was saved (truth-from-real-state, AGENTS.md 🔴). This use case:

1. Loads the provider's non-secret config (``base_url``) from the registry.
2. Reads its ``api_key`` from the SecretStore (the same namespace chat reads,
   ``qai.model_catalog.provider`` / ``<provider_id>``).
3. Delegates a real minimal request to the injected :class:`ProviderProbePort`.

It is shared by HTTP and CLI surfaces (a route may expose it later); the
secret never leaves this layer and is never returned to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qai.model_catalog.application.ports import (
    ProviderProbePort,
    ProviderProbeResult,
    ProviderRegistryPort,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence.secrets import SecretStore

_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbeProviderCommand:
    provider_id: str


class ProbeProviderUseCase:
    """Resolve provider config + secret, then probe connectivity."""

    def __init__(
        self,
        *,
        registry: ProviderRegistryPort,
        probe: ProviderProbePort,
        secret_store: "SecretStore | None" = None,
    ) -> None:
        self._registry = registry
        self._probe = probe
        self._secret_store = secret_store

    async def execute(
        self, command: ProbeProviderCommand
    ) -> ProviderProbeResult:
        config = await self._registry.get_provider_config(
            command.provider_id
        )
        if config is None:
            return ProviderProbeResult(
                ok=False,
                error=f"provider {command.provider_id!r} not configured",
            )
        base_url = config.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            return ProviderProbeResult(
                ok=False,
                error="provider config has no base_url",
            )
        api_key = self._resolve_key(command.provider_id)
        return await self._probe.probe(base_url=base_url, api_key=api_key)

    def _resolve_key(self, provider_id: str) -> str | None:
        store = self._secret_store
        if store is None:
            return None
        try:
            if store.exists(_PROVIDER_SECRET_SERVICE, provider_id):
                return store.get(_PROVIDER_SECRET_SERVICE, provider_id)
        except Exception:  # noqa: BLE001 -- treat any failure as "no key"
            return None
        return None


__all__ = ["ProbeProviderUseCase", "ProbeProviderCommand"]
