# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for the ``service_release`` bounded context.

Each use case is a thin orchestration over the application ports
(:mod:`qai.service_release.application.ports`). They are intentionally
small because the V1 download center is stateless — the real work lives
in the infrastructure/adapters (catalog fetch, download engine, installer,
disk scanner, aria2c manager, settings store).

URL/id validation that the HTTP layer cannot express is enforced here so
behaviour matches V1 (``download_url`` must be HTTP; ids non-empty).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from qai.service_release.application.ports import (
    Aria2cManagerPort,
    ArtifactInstallerPort,
    DownloadEnginePort,
    DownloadSettingsPort,
    LocalStatusScannerPort,
    ServiceCatalogSourcePort,
)
from qai.service_release.domain.errors import InvalidDownloadRequestError
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


def _require_http_url(url: str, *, field: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned.lower().startswith("http"):
        raise InvalidDownloadRequestError(
            f"{field} must be a valid HTTP URL",
            field_errors={field: ["must start with http"]},
        )
    return cleaned


def _require_non_empty(value: str, *, field: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise InvalidDownloadRequestError(
            f"{field} is required", field_errors={field: ["required"]}
        )
    return cleaned


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class StartServiceDownloadCommand:
    version: str
    download_url: str
    checksum_sha256: str = ""
    task_id: str = ""  # "version-platformId" for multi-platform, else version


@dataclass(frozen=True, slots=True, kw_only=True)
class StartModelDownloadCommand:
    model_id: str  # save sub-dir (shared across a model's platform variants)
    download_url: str
    checksum_sha256: str = ""
    # Cancellation / progress key. Multi-platform models share one ``model_id``
    # save dir but download each platform's zip under its own ``variant_id`` so
    # parallel downloads (and their cancels) don't collide. Defaults to
    # ``model_id`` when the caller has no per-variant key (single-platform).
    task_id: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallServiceCommand:
    save_path: str
    version: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallModelCommand:
    save_path: str
    model_id: str = ""
    install_dir: str = ""
    # Selected platform variant (for the install marker → Installed pill
    # platform label). Optional; degrades to no platform label when absent.
    variant_id: str = ""


# ---------------------------------------------------------------------------
# Catalog listing
# ---------------------------------------------------------------------------


class ListServiceVersionsUseCase:
    def __init__(self, *, source: ServiceCatalogSourcePort) -> None:
        self._source = source

    async def execute(self) -> list[ServiceVersion]:
        return await self._source.fetch_service_versions()


class ListCatalogModelsUseCase:
    def __init__(self, *, source: ServiceCatalogSourcePort) -> None:
        self._source = source

    async def execute(self) -> list[CatalogModel]:
        return await self._source.fetch_catalog_models()


# ---------------------------------------------------------------------------
# Streamed downloads
# ---------------------------------------------------------------------------


class StreamServiceDownloadUseCase:
    def __init__(self, *, engine: DownloadEnginePort) -> None:
        self._engine = engine

    def execute(
        self, command: StartServiceDownloadCommand
    ) -> AsyncIterator[DownloadProgress]:
        version = _require_non_empty(command.version, field="version")
        url = _require_http_url(command.download_url, field="download_url")
        task_id = command.task_id.strip() or version
        return self._engine.stream_download(
            task_id=task_id,
            sub_dir=version,  # V1: version packages live under save_dir/<version>
            download_url=url,
            checksum_sha256=command.checksum_sha256,
        )


class StreamModelDownloadUseCase:
    def __init__(self, *, engine: DownloadEnginePort) -> None:
        self._engine = engine

    def execute(
        self, command: StartModelDownloadCommand
    ) -> AsyncIterator[DownloadProgress]:
        model_id = _require_non_empty(command.model_id, field="model_id")
        url = _require_http_url(command.download_url, field="download_url")
        task_id = command.task_id.strip() or model_id
        return self._engine.stream_download(
            task_id=task_id,
            sub_dir=model_id,  # models share a per-model_id dir (V1 + multi-variant)
            download_url=url,
            checksum_sha256=command.checksum_sha256,
        )


# ---------------------------------------------------------------------------
# Install / delete
# ---------------------------------------------------------------------------


class InstallServiceUseCase:
    def __init__(self, *, installer: ArtifactInstallerPort) -> None:
        self._installer = installer

    async def execute(self, command: InstallServiceCommand) -> ServiceInstallResult:
        save_path = _require_non_empty(command.save_path, field="save_path")
        return await self._installer.install_service(
            save_path=save_path, version=command.version
        )


class InstallModelUseCase:
    def __init__(self, *, installer: ArtifactInstallerPort) -> None:
        self._installer = installer

    async def execute(self, command: InstallModelCommand) -> ModelInstallResult:
        save_path = _require_non_empty(command.save_path, field="save_path")
        return await self._installer.install_model(
            save_path=save_path,
            model_id=command.model_id,
            install_dir=command.install_dir,
            variant_id=command.variant_id,
        )


class DeleteInstalledServiceUseCase:
    def __init__(self, *, installer: ArtifactInstallerPort) -> None:
        self._installer = installer

    async def execute(
        self, *, version: str, stop_running: bool = False
    ) -> dict[str, object]:
        return await self._installer.delete_installed_service(
            version=_require_non_empty(version, field="version"),
            stop_running=stop_running,
        )

    async def is_running(self, *, version: str) -> bool:
        """Whether a process is running out of this version's install dir.

        Lets the caller (route / CLI) warn "the service is running and will be
        stopped" before deleting.
        """
        return await self._installer.is_installed_service_running(
            version=_require_non_empty(version, field="version")
        )


class DeleteDownloadedServiceUseCase:
    def __init__(self, *, installer: ArtifactInstallerPort) -> None:
        self._installer = installer

    async def execute(self, *, version: str) -> dict[str, object]:
        return await self._installer.delete_downloaded_service(
            version=_require_non_empty(version, field="version")
        )


class DeleteModelUseCase:
    def __init__(self, *, installer: ArtifactInstallerPort) -> None:
        self._installer = installer

    async def execute(
        self, *, model_id: str, delete_zip: bool = True
    ) -> dict[str, object]:
        return await self._installer.delete_model(
            model_id=_require_non_empty(model_id, field="model_id"),
            delete_zip=delete_zip,
        )


# ---------------------------------------------------------------------------
# Local status
# ---------------------------------------------------------------------------


class GetVersionsLocalStatusUseCase:
    def __init__(self, *, scanner: LocalStatusScannerPort) -> None:
        self._scanner = scanner

    async def execute(self) -> VersionsLocalStatus:
        return await self._scanner.scan_versions()


class GetModelsLocalStatusUseCase:
    def __init__(self, *, scanner: LocalStatusScannerPort) -> None:
        self._scanner = scanner

    async def execute(self) -> ModelsLocalStatus:
        return await self._scanner.scan_models()


# ---------------------------------------------------------------------------
# aria2c
# ---------------------------------------------------------------------------


class GetAria2cStatusUseCase:
    def __init__(self, *, manager: Aria2cManagerPort) -> None:
        self._manager = manager

    async def execute(self) -> Aria2cStatus:
        return await self._manager.get_status()


class CancelDownloadUseCase:
    def __init__(self, *, manager: Aria2cManagerPort) -> None:
        self._manager = manager

    async def execute(self, *, task_id: str) -> bool:
        return await self._manager.cancel(
            task_id=_require_non_empty(task_id, field="task_id")
        )


class StartAria2cUseCase:
    """Ensure aria2c is installed + the RPC daemon is running (V1 POST /start)."""

    def __init__(self, *, manager: Aria2cManagerPort) -> None:
        self._manager = manager

    async def execute(self) -> Aria2cStatus:
        return await self._manager.start()


class StopAria2cUseCase:
    """Stop the aria2c RPC daemon (V1 POST /stop)."""

    def __init__(self, *, manager: Aria2cManagerPort) -> None:
        self._manager = manager

    async def execute(self) -> Aria2cStatus:
        return await self._manager.stop()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class GetDownloadSettingsUseCase:
    def __init__(self, *, settings: DownloadSettingsPort) -> None:
        self._settings = settings

    async def execute(self) -> DownloadSettings:
        return await self._settings.read()


class UpdateDownloadSettingsUseCase:
    def __init__(self, *, settings: DownloadSettingsPort) -> None:
        self._settings = settings

    async def execute(self, settings: DownloadSettings) -> DownloadSettings:
        return await self._settings.write(settings)


__all__ = [
    "StartServiceDownloadCommand",
    "StartModelDownloadCommand",
    "InstallServiceCommand",
    "InstallModelCommand",
    "ListServiceVersionsUseCase",
    "ListCatalogModelsUseCase",
    "StreamServiceDownloadUseCase",
    "StreamModelDownloadUseCase",
    "InstallServiceUseCase",
    "InstallModelUseCase",
    "DeleteInstalledServiceUseCase",
    "DeleteDownloadedServiceUseCase",
    "DeleteModelUseCase",
    "GetVersionsLocalStatusUseCase",
    "GetModelsLocalStatusUseCase",
    "GetAria2cStatusUseCase",
    "CancelDownloadUseCase",
    "StartAria2cUseCase",
    "StopAria2cUseCase",
    "GetDownloadSettingsUseCase",
    "UpdateDownloadSettingsUseCase",
]
