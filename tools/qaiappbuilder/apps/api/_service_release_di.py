# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``service_release`` bounded context.

Restores the V1 "Download Center" capability (GenieAPIService version
packages + hardware-grouped model catalog + aria2c + streamed downloads +
install/delete + local-status + forge_config download settings) as a
standalone Clean-Architecture context, so it does not perturb the frozen
``model_catalog`` contracts (entries / cloud-models / providers) consumed
by chat / channels.

This context is *stateless* (no DB aggregate): the source of truth is the
remote manifest plus the local filesystem. The wiring therefore composes
HTTP / filesystem / subprocess adapters only — no repositories, no
migrations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from qai.service_release.adapters.filesystem_installer import (
    Aria2cManager,
    FileSystemArtifactInstaller,
    FileSystemLocalStatusScanner,
)
from qai.service_release.adapters.forge_settings import JsonForgeDownloadSettings
from qai.service_release.application.ports import (
    Aria2cManagerPort,
    ArtifactInstallerPort,
    DownloadEnginePort,
    DownloadSettingsPort,
    LocalStatusScannerPort,
    ServiceCatalogSourcePort,
)
from qai.service_release.application.use_cases import (
    CancelDownloadUseCase,
    DeleteDownloadedServiceUseCase,
    DeleteInstalledServiceUseCase,
    DeleteModelUseCase,
    GetAria2cStatusUseCase,
    GetDownloadSettingsUseCase,
    GetModelsLocalStatusUseCase,
    GetVersionsLocalStatusUseCase,
    InstallModelUseCase,
    InstallServiceUseCase,
    ListCatalogModelsUseCase,
    ListServiceVersionsUseCase,
    StartAria2cUseCase,
    StopAria2cUseCase,
    StreamModelDownloadUseCase,
    StreamServiceDownloadUseCase,
    UpdateDownloadSettingsUseCase,
)
from qai.service_release.infrastructure.aria2c_daemon import Aria2cDaemon
from qai.service_release.infrastructure.download_engine import HttpxDownloadEngine
from qai.service_release.infrastructure.download_paths import DownloadPaths
from qai.service_release.infrastructure.http_catalog_source import HttpCatalogSource

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


__all__ = ["ServiceReleaseServices", "build_service_release_services"]


@dataclass(slots=True)
class ServiceReleaseServices:
    """Application services for the ``service_release`` bounded context."""

    # raw ports (additive, exposed for tests / cross-context composition)
    catalog_source: ServiceCatalogSourcePort
    download_engine: DownloadEnginePort
    installer: ArtifactInstallerPort
    local_status_scanner: LocalStatusScannerPort
    aria2c_manager: Aria2cManagerPort
    download_settings: DownloadSettingsPort
    # catalog listing
    list_service_versions_use_case: ListServiceVersionsUseCase
    list_catalog_models_use_case: ListCatalogModelsUseCase
    # streamed downloads
    stream_service_download_use_case: StreamServiceDownloadUseCase
    stream_model_download_use_case: StreamModelDownloadUseCase
    # install / delete
    install_service_use_case: InstallServiceUseCase
    install_model_use_case: InstallModelUseCase
    delete_installed_service_use_case: DeleteInstalledServiceUseCase
    delete_downloaded_service_use_case: DeleteDownloadedServiceUseCase
    delete_model_use_case: DeleteModelUseCase
    # local status
    get_versions_local_status_use_case: GetVersionsLocalStatusUseCase
    get_models_local_status_use_case: GetModelsLocalStatusUseCase
    # aria2c
    get_aria2c_status_use_case: GetAria2cStatusUseCase
    cancel_download_use_case: CancelDownloadUseCase
    start_aria2c_use_case: StartAria2cUseCase
    stop_aria2c_use_case: StopAria2cUseCase
    # settings
    get_download_settings_use_case: GetDownloadSettingsUseCase
    update_download_settings_use_case: UpdateDownloadSettingsUseCase


def _resolve_download_paths(
    container: "Container", settings_port: DownloadSettingsPort
) -> DownloadPaths:
    """Resolve downloads/bin/models working dirs.

    Defaults sit under the data root (``<data>/downloads|bin|models``).
    A non-empty ``forge_config.download.save_dir`` overrides the download
    directory (V1 behaviour), allowing the user to relocate large archives.
    """
    root: Path = container.data_paths.root
    default_download_dir = root / "downloads"
    bin_dir = root / "bin"
    models_dir = root / "models"
    return DownloadPaths(
        default_download_dir=default_download_dir,
        bin_dir=bin_dir,
        models_dir=models_dir,
        # V1 forge_config.download.save_dir override, resolved per-access so a
        # runtime PUT /api/versions/download-settings takes effect immediately
        # (empty value falls back to default_download_dir — V1 behaviour).
        save_dir_provider=settings_port.read_save_dir,
    )


def _resolve_forge_config_path(container: "Container") -> Path:
    """forge_config.json location (data root, created on first write)."""
    return container.data_paths.root / "config" / "forge_config.json"


def _make_service_stopper(
    container: "Container",
) -> "Callable[[], Awaitable[None]]":
    """Build the lazy graceful-stop bridge to ``model_runtime`` (§3.2).

    Resolved at delete time (not build time) because ``model_runtime`` is
    composed after ``service_release`` in the container. Calls the daemon's
    ``StopServiceUseCase`` (CTRL_BREAK + timeout, V1-parity graceful stop).
    Best-effort: if ``model_runtime`` isn't wired or stop fails, the adapter's
    per-dir process-kill fallback still handles an orphan lock.
    """

    async def _stop() -> None:
        model_runtime = getattr(container, "model_runtime", None)
        if model_runtime is None:
            return
        uc = getattr(model_runtime, "stop_service_use_case", None)
        if uc is None:
            return
        await uc.execute()

    return _stop


def build_service_release_services(
    container: "Container",
) -> ServiceReleaseServices:
    settings_port: DownloadSettingsPort = JsonForgeDownloadSettings(
        path=_resolve_forge_config_path(container)
    )
    paths = _resolve_download_paths(container, settings_port)

    # H-2: live global-proxy provider. Reads ``settings.tools.global_proxy``
    # at call time so a runtime-config change hot-applies to catalog fetches
    # + downloads (V1 routed both through the global proxy). Defined here in
    # the apps DI layer so service_release stays import-isolated from the
    # settings object (context isolation §3.2).
    def _global_proxy_provider() -> str | None:
        settings = getattr(container, "settings", None)
        tools = getattr(settings, "tools", None) if settings else None
        return getattr(tools, "global_proxy", None)

    catalog_source = HttpCatalogSource(
        settings=settings_port, proxy_provider=_global_proxy_provider
    )
    # 缺口 9 — the aria2c binary auto-install download routes through the
    # mechanism-B global proxy (with embedded user:pass@ auth) — a "file
    # download" class request. Reuses the shared apps-layer provider so the
    # daemon stays import-isolated from settings / SecretStore (§3.2).
    from apps.api._global_proxy import build_global_proxy_provider

    aria2c_daemon = Aria2cDaemon(
        bin_dir=paths.bin_dir,
        proxy_provider=build_global_proxy_provider(container),
    )
    engine = HttpxDownloadEngine(
        paths=paths,
        settings=settings_port,
        aria2c=aria2c_daemon,
        proxy_provider=_global_proxy_provider,
    )
    installer = FileSystemArtifactInstaller(
        paths=paths,
        # §3.2 cross-context bridge: deleting an installed GenieAPIService
        # version should gracefully stop a running one first (release the
        # Genie.dll lock → no WinError 5). The stopper is resolved LAZILY at
        # call time because ``model_runtime`` is built AFTER ``service_release``
        # in the container; capturing it eagerly here would be None. This keeps
        # ``service_release`` import-isolated from ``model_runtime`` (the wiring
        # lives in the apps layer, per §3.2).
        service_stopper=_make_service_stopper(container),
    )
    scanner = FileSystemLocalStatusScanner(
        paths=paths,
        # V1 parity (main.py:4969-4996): when ``genie_service.root_path`` is
        # unset but a GenieAPIService install exists under ``data/bin``, the
        # scanner auto-writes it so the Service/Settings pages detect the
        # daemon. Both read + write target the same forge_config.json owned by
        # ``settings_port`` so the merge preserves unrelated sections.
        on_auto_configure=settings_port.write_genie_root_path,
        read_root_path=settings_port.read_genie_root_path,
    )
    aria2c = Aria2cManager(paths=paths, engine=engine, daemon=aria2c_daemon)

    return ServiceReleaseServices(
        catalog_source=catalog_source,
        download_engine=engine,
        installer=installer,
        local_status_scanner=scanner,
        aria2c_manager=aria2c,
        download_settings=settings_port,
        list_service_versions_use_case=ListServiceVersionsUseCase(
            source=catalog_source
        ),
        list_catalog_models_use_case=ListCatalogModelsUseCase(source=catalog_source),
        stream_service_download_use_case=StreamServiceDownloadUseCase(engine=engine),
        stream_model_download_use_case=StreamModelDownloadUseCase(engine=engine),
        install_service_use_case=InstallServiceUseCase(installer=installer),
        install_model_use_case=InstallModelUseCase(installer=installer),
        delete_installed_service_use_case=DeleteInstalledServiceUseCase(
            installer=installer
        ),
        delete_downloaded_service_use_case=DeleteDownloadedServiceUseCase(
            installer=installer
        ),
        delete_model_use_case=DeleteModelUseCase(installer=installer),
        get_versions_local_status_use_case=GetVersionsLocalStatusUseCase(
            scanner=scanner
        ),
        get_models_local_status_use_case=GetModelsLocalStatusUseCase(scanner=scanner),
        get_aria2c_status_use_case=GetAria2cStatusUseCase(manager=aria2c),
        cancel_download_use_case=CancelDownloadUseCase(manager=aria2c),
        start_aria2c_use_case=StartAria2cUseCase(manager=aria2c),
        stop_aria2c_use_case=StopAria2cUseCase(manager=aria2c),
        get_download_settings_use_case=GetDownloadSettingsUseCase(
            settings=settings_port
        ),
        update_download_settings_use_case=UpdateDownloadSettingsUseCase(
            settings=settings_port
        ),
    )
