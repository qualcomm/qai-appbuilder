# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Read the QGenie account's dual-bucket quota for the sidebar gauge.

Resolves the provider's ``base_url`` from the registry and its credential from
the :class:`SecretStore` — the SAME key chat sends requests with, so the
figures shown always describe the account actually being billed. A separate
"quota key" setting would let the two drift and quietly display someone else's
allowance.

Never raises: the caller is a status widget, and a quota read failing must
not turn into a visible error for a user who may not even use QGenie.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from qai.model_catalog.application.ports import (
    ProviderRegistryPort,
    QGenieQuotaPort,
)
from qai.model_catalog.domain.qgenie_quota import (
    QGENIE_ERR_BAD_BASE_URL,
    QGENIE_ERR_NOT_CONFIGURED,
    QGENIE_ERR_NO_API_KEY,
    QGENIE_ERR_UNREACHABLE,
    QGenieQuotaSnapshot,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence.secrets import SecretStore

__all__ = ["QGENIE_PROVIDER_ID", "GetQGenieQuotaUseCase"]

#: Provider id the internal edition seeds the QGenie catalog under
#: (``internal_config.toml`` ``[cloud_providers.qgenie]``).
QGENIE_PROVIDER_ID: Final[str] = "qgenie"

#: Secret-store service namespace shared with the probe use cases, so the key
#: the user pastes once is found by every reader.
_PROVIDER_SECRET_SERVICE: Final[str] = "qai.model_catalog.provider"  # noqa: S105 — namespace, not a secret


class GetQGenieQuotaUseCase:
    """Fetch the current QGenie quota snapshot."""

    __slots__ = ("_quota", "_registry", "_secret_store")

    def __init__(
        self,
        *,
        registry: ProviderRegistryPort,
        quota: QGenieQuotaPort,
        secret_store: SecretStore | None = None,
    ) -> None:
        self._registry = registry
        self._quota = quota
        self._secret_store = secret_store

    async def execute(self, *, force: bool = False) -> QGenieQuotaSnapshot:
        """Return the snapshot, or an empty one when QGenie is not usable here.

        ``force`` bypasses the adapter cache; the exhaustion check needs it so
        a switch decision is never made on a stale reading.
        """
        config = await self._safe_get_config()
        if config is None:
            # No QGenie provider in this build / install (external edition).
            return QGenieQuotaSnapshot(error=QGENIE_ERR_NOT_CONFIGURED)

        base_url = config.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            return QGenieQuotaSnapshot(error=QGENIE_ERR_BAD_BASE_URL)

        api_key = self._resolve_key()
        if not api_key:
            # First launch ships the catalog without a credential on purpose;
            # the "paste your key" onboarding is the right signal, not a gauge
            # claiming zero quota.
            return QGenieQuotaSnapshot(error=QGENIE_ERR_NO_API_KEY)

        try:
            return await self._quota.fetch(
                base_url=base_url, api_key=api_key, force=force
            )
        except Exception:  # noqa: BLE001 — a widget read must never propagate
            return QGenieQuotaSnapshot(error=QGENIE_ERR_UNREACHABLE)

    async def _safe_get_config(self) -> dict[str, object] | None:
        try:
            return await self._registry.get_provider_config(QGENIE_PROVIDER_ID)
        except Exception:  # noqa: BLE001 — missing provider is not an error
            return None

    def _resolve_key(self) -> str | None:
        """Return the user's QGenie api_key, or ``None``.

        Mirrors ``ProbeCloudModelPermissionsUseCase._resolve_key`` so both
        readers agree on where the credential lives.
        """
        store = self._secret_store
        if store is None:
            return None
        try:
            if not store.exists(_PROVIDER_SECRET_SERVICE, QGENIE_PROVIDER_ID):
                return None
            value = store.get(_PROVIDER_SECRET_SERVICE, QGENIE_PROVIDER_ID)
        except Exception:  # noqa: BLE001 — treat any failure as "no key"
            return None
        return value if isinstance(value, str) and value else None
