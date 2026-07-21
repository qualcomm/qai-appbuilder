# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``RefreshReleaseManifestUseCase`` -- reconcile catalog with upstream."""

from __future__ import annotations

from qai.model_catalog.application.ports import (
    ManifestFetcherPort,
    ModelEntryRepositoryPort,
)
from qai.model_catalog.domain.entities import ReleaseManifest
from qai.model_catalog.domain.events import ReleaseManifestRefreshedEvent
from qai.platform.events import EventBus


class RefreshReleaseManifestUseCase:
    """Pull the latest release manifest from upstream.

    The use case ONLY refreshes the cached manifest; it does NOT
    auto-download or auto-install anything.  Higher layers decide what
    to do with the new metadata (the legacy code coupled fetch +
    install, which is exactly the scaling problem we are unwinding).
    """

    def __init__(
        self,
        *,
        fetcher: ManifestFetcherPort,
        entry_repository: ModelEntryRepositoryPort,
        event_bus: EventBus,
    ) -> None:
        self._fetcher = fetcher
        self._entry_repo = entry_repository
        self._event_bus = event_bus

    async def execute(self) -> ReleaseManifest:
        manifest = await self._fetcher.fetch_latest()
        # We *deliberately* do not write into entry_repository here.
        # The repo is part of the constructor so reconciliation
        # subclasses can extend the use case without breaking the
        # signature, but this implementation is read-only by design.
        await self._event_bus.publish(
            ReleaseManifestRefreshedEvent(
                manifest_version=manifest.manifest_version,
                entry_count=manifest.entry_count,
            )
        )
        return manifest


__all__ = ["RefreshReleaseManifestUseCase"]
