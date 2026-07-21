# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application ports for the ``service_release`` bounded context.

These ``Protocol`` boundaries let the use cases orchestrate the V1
download-center workflow without depending on httpx / subprocess /
filesystem concretes (those live in ``infrastructure`` / ``adapters``).

Design note: the V1 download center is *stateless* (no DB aggregate);
the "source of truth" is the remote manifest plus the local filesystem.
So instead of a repository port we have:

* :class:`ServiceCatalogSourcePort` — fetch + parse remote manifests.
* :class:`DownloadEnginePort` — stream a download (aria2c → httpx).
* :class:`ArtifactInstallerPort` — unzip into bin/ or models/.
* :class:`LocalStatusScannerPort` — re-derive downloaded/installed state.
* :class:`Aria2cManagerPort` — aria2c status / cancel / auto-install.
* :class:`DownloadSettingsPort` — read/write the forge_config download section.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from qai.service_release.domain.value_objects import (
    Aria2cStatus,
    CatalogModel,
    DownloadProgress,
    DownloadSettings,
    ModelInstallResult,
    ModelsLocalStatus,
    ServiceInstallResult,
    ServiceVersion,
    VersionsLocalStatus,
)


@runtime_checkable
class ServiceCatalogSourcePort(Protocol):
    """Fetches + parses the remote release manifest and model catalog."""

    async def fetch_service_versions(self) -> list[ServiceVersion]:
        """Return GenieAPIService versions from ``version_list_url``.

        Raises ``CatalogUnavailableError`` for content/config problems and
        platform infrastructure errors for transport failures.
        """
        ...

    async def fetch_catalog_models(self) -> list[CatalogModel]:
        """Return hardware-grouped catalog models from ``catalog_url``."""
        ...


@runtime_checkable
class DownloadEnginePort(Protocol):
    """Streams a download, preferring aria2c then falling back to httpx."""

    def stream_download(
        self,
        *,
        task_id: str,
        sub_dir: str,
        download_url: str,
        checksum_sha256: str = "",
    ) -> AsyncIterator[DownloadProgress]:
        """Yield :class:`DownloadProgress` frames until done/error/cancelled.

        ``sub_dir`` is the per-item folder name under the configured
        ``save_dir`` (V1: ``version`` for service packages, ``model_id``
        for models). ``task_id`` is the cancellation/lookup key
        (``version`` / ``version-platformId`` / ``model_id`` / ``variant_id``).
        """
        ...


@runtime_checkable
class ArtifactInstallerPort(Protocol):
    """Unzips a downloaded archive into the install root."""

    async def install_service(
        self, *, save_path: str, version: str
    ) -> ServiceInstallResult:
        """Unzip a GenieAPIService package into ``bin/`` and wire root_path."""
        ...

    async def install_model(
        self, *, save_path: str, model_id: str, install_dir: str = "",
        variant_id: str = "",
    ) -> ModelInstallResult:
        """Unzip a model archive into the models root.

        ``variant_id`` records the user-selected platform so the installer
        can persist a marker for the scanner (drives the Installed pill's
        platform label). Optional — empty degrades to no platform label.
        """
        ...

    async def delete_installed_service(
        self, *, version: str, stop_running: bool = False
    ) -> dict[str, object]:
        """Remove an installed GenieAPIService version directory.

        ``stop_running``: when True, gracefully stop a running service for
        that version first (so the loaded ``Genie.dll`` lock is released
        before deletion). An orphan the manager doesn't own is force-stopped
        by the adapter as a fallback.
        """
        ...

    async def is_installed_service_running(self, *, version: str) -> bool:
        """Best-effort: is a process running out of the version's install dir?"""
        ...

    async def delete_downloaded_service(self, *, version: str) -> dict[str, object]:
        """Remove a downloaded-but-not-installed service zip."""
        ...

    async def delete_model(
        self, *, model_id: str, delete_zip: bool = True
    ) -> dict[str, object]:
        """Remove an installed model directory (and optionally its zip)."""
        ...


@runtime_checkable
class LocalStatusScannerPort(Protocol):
    """Re-derives downloaded/installed state by scanning the disk."""

    async def scan_versions(self) -> VersionsLocalStatus:
        ...

    async def scan_models(self) -> ModelsLocalStatus:
        ...


@runtime_checkable
class Aria2cManagerPort(Protocol):
    """aria2c availability / cancellation / auto-install management."""

    async def get_status(self) -> Aria2cStatus:
        ...

    async def cancel(self, *, task_id: str) -> bool:
        ...

    async def start(self) -> Aria2cStatus:
        """Ensure the binary is installed and the RPC daemon is running.

        V1 ``POST /api/aria2c/start``. Auto-installs aria2c (Windows) when
        absent, then starts the shared RPC daemon. Returns the post-action
        status snapshot (``daemon_running`` reflects the result).
        """
        ...

    async def stop(self) -> Aria2cStatus:
        """Stop the RPC daemon (V1 ``POST /api/aria2c/stop``).

        Returns the post-action status snapshot.
        """
        ...


@runtime_checkable
class DownloadSettingsPort(Protocol):
    """Reads/writes the forge_config download section."""

    def read_save_dir(self) -> str:
        """Read just ``download.save_dir`` synchronously (V1 path override).

        Resolves the live archive directory on each access (used by
        :class:`DownloadPaths.save_dir_provider`) without an async hop.
        Returns ``""`` when unset (callers fall back to the default).
        """
        ...

    def read_genie_root_path(self) -> str:
        """Read ``genie_service.root_path`` synchronously (V1 parity).

        Used by the local-status scanner to decide whether the
        GenieAPIService install dir still needs auto-configuring. Returns
        ``""`` when unset.
        """
        ...

    def write_genie_root_path(self, root_path: str) -> None:
        """Persist ``genie_service.root_path`` (V1 auto-configure write).

        Shallow-merges into forge_config.json, preserving unrelated
        sections (V1 ``forge_config.update`` semantics).
        """
        ...

    async def read(self) -> DownloadSettings:
        ...

    async def write(self, settings: DownloadSettings) -> DownloadSettings:
        ...


__all__ = [
    "ServiceCatalogSourcePort",
    "DownloadEnginePort",
    "ArtifactInstallerPort",
    "LocalStatusScannerPort",
    "Aria2cManagerPort",
    "DownloadSettingsPort",
]
