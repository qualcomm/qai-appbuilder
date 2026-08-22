# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Value objects for the ``service_release`` bounded context.

This context restores the V1 "Download Center" capability (legacy
``backend/version_manager.py`` + ``model_catalog_manager.py`` +
``aria2c_downloader.py``). Unlike ``model_catalog`` (which is a
persisted DDD aggregate), the V1 download center is *stateless*:

* it fetches a remote manifest (GenieAPIService release manifest or the
  hardware-grouped model catalog),
* streams a download (aria2c RPC with httpx fallback),
* installs (unzips) into ``bin/`` / ``models/``,
* and re-derives "downloaded / installed" state by scanning the disk.

There is **no** database aggregate here — the source of truth is the
remote manifest plus the local filesystem. The value objects below are
therefore immutable wire/projection shapes, not persisted entities.

All field names mirror the V1 ``to_dict()`` shapes exactly so the
frontend (ported 1:1 from the V1 ``useDownloadCenter`` composable) keeps
working without translation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union

#: A user-facing text that may be a plain string OR a per-language mapping
#: (``{"zh-CN": "...", "en": "...", "zh-TW": "..."}``). Remote catalogs /
#: release manifests can carry their own translations this way; the backend
#: passes the value through UNCHANGED (str stays str, dict stays dict) and the
#: frontend resolves the active UI language at render time (so switching the
#: WebUI language is instant, no re-fetch — and third-party-hosted catalogs
#: can ship their own translations without the app shipping locale keys).
#: Backward-compatible: a plain string is a valid value.
LocalizedText = Union[str, dict[str, str]]

# ---------------------------------------------------------------------------
# Download progress / status (shared by version + model downloads)
# ---------------------------------------------------------------------------


class DownloadStatus(str, Enum):
    """V1 ``DownloadProgress.status`` value set (legacy parity).

    ``preparing`` is emitted only while aria2c is auto-installing; the
    terminal states are ``done`` / ``error`` / ``cancelled``.
    """

    IDLE = "idle"
    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class DownloadEngineKind(str, Enum):
    """Which engine produced a progress frame (V1 ``engine`` field)."""

    ARIA2C = "aria2c"
    HTTPX = "httpx"
    NONE = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadProgress:
    """One streamed progress snapshot.

    Mirrors V1 ``DownloadProgress.to_dict()`` + the ``engine`` field that
    ``stream_download`` injects. NB: the V1 ``version`` wire field
    actually carries the *task id* (``version`` / ``version-platformId``
    for service packages, ``model_id`` / ``variant_id`` for models). We
    keep the wire name ``version`` for 1:1 frontend parity but name the
    attribute ``task_id`` internally.

    The backend does **not** compute speed/ETA — the frontend derives it
    from successive ``downloaded_bytes`` deltas (V1 ``_calcSpeed``).
    """

    task_id: str
    filename: str = ""
    downloaded_bytes: int = 0
    total_bytes: int = 0
    status: DownloadStatus = DownloadStatus.DOWNLOADING
    error: str = ""
    save_path: str = ""
    engine: DownloadEngineKind = DownloadEngineKind.NONE

    @property
    def percent(self) -> float:
        """``round(downloaded/total*100, 1)``; ``0.0`` when total unknown."""
        if self.total_bytes <= 0:
            return 0.0
        return round(self.downloaded_bytes / self.total_bytes * 100, 1)

    def to_wire(self) -> dict[str, object]:
        """Serialise to the V1 SSE frame shape (wire field ``version``)."""
        return {
            "version": self.task_id,
            "filename": self.filename,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "percent": self.percent,
            "status": self.status.value,
            "error": self.error,
            "save_path": self.save_path,
            "engine": self.engine.value,
        }


# ---------------------------------------------------------------------------
# GenieAPIService version packages
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ServicePackage:
    """A per-platform package within a :class:`ServiceVersion`.

    Mirrors V1 ``packages[]`` entries. ``download_url`` is validated to be
    an HTTP(S) URL by the parser; invalid packages are dropped upstream.
    """

    platform: str
    platform_id: str
    download_url: str
    description: LocalizedText = ""
    min_driver_version: str = ""
    is_default: bool = False

    def to_wire(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "platform_id": self.platform_id,
            "description": self.description,
            "download_url": self.download_url,
            "min_driver_version": self.min_driver_version,
            "is_default": self.is_default,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceVersion:
    """A GenieAPIService release entry (V1 ``VersionEntry``)."""

    version: str
    download_url: str = ""
    release_date: str = ""
    checksum_sha256: str = ""
    size_bytes: int = 0
    is_recommended: bool = False
    min_driver_version: str = ""
    changelog: LocalizedText = ""
    tags: tuple[str, ...] = ()
    description: LocalizedText = ""
    packages: tuple[ServicePackage, ...] = ()

    def to_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "download_url": self.download_url,
            "release_date": self.release_date,
            "checksum_sha256": self.checksum_sha256,
            "size_bytes": self.size_bytes,
            "is_recommended": self.is_recommended,
            "min_driver_version": self.min_driver_version,
            "changelog": self.changelog,
            "tags": list(self.tags),
            "description": self.description,
            "packages": [p.to_wire() for p in self.packages],
        }


# ---------------------------------------------------------------------------
# Hardware-grouped model catalog
# ---------------------------------------------------------------------------


class ModelHardware(str, Enum):
    """V1 ``hardware`` value set (drives the NPU/GPU/CPU grouping)."""

    NPU = "npu"
    GPU = "gpu"
    CPU = "cpu"


class ModelFormat(str, Enum):
    """V1 ``format`` value set: ``qnn`` (NPU) / ``gguf`` (GPU/CPU) / ``mnn`` (CPU)."""

    QNN = "qnn"
    GGUF = "gguf"
    MNN = "mnn"


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelVariant:
    """A per-platform model build (V1 schema_version 2 ``variants[]``)."""

    variant_id: str
    platform: str = ""
    chip: str = ""
    description: LocalizedText = ""
    min_driver_version: str = ""
    download_url: str = ""
    size_bytes: int = 0
    checksum_sha256: str = ""

    def to_wire(self) -> dict[str, object]:
        return {
            "variant_id": self.variant_id,
            "platform": self.platform,
            "chip": self.chip,
            "description": self.description,
            "min_driver_version": self.min_driver_version,
            "download_url": self.download_url,
            "size_bytes": self.size_bytes,
            "checksum_sha256": self.checksum_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogModel:
    """A model catalog entry (V1 ``ModelEntry.to_dict()``)."""

    model_id: str
    name: str
    family: str = ""
    parameter_size: str = ""
    model_format: ModelFormat = ModelFormat.GGUF
    hardware: ModelHardware = ModelHardware.CPU
    context_length: int = 0
    download_url: str = ""
    model_type: str = "llm"  # "llm" | "vlm"
    min_driver_version: str = ""
    quantization: str = ""
    short_description: LocalizedText = ""
    description: LocalizedText = ""
    features: tuple[LocalizedText, ...] = ()
    tags: tuple[str, ...] = ()
    size_bytes: int = 0
    checksum_sha256: str = ""
    variants: tuple[ModelVariant, ...] = ()

    def to_wire(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "family": self.family,
            "parameter_size": self.parameter_size,
            "format": self.model_format.value,
            "hardware": self.hardware.value,
            "context_length": self.context_length,
            "download_url": self.download_url,
            "type": self.model_type,
            "min_driver_version": self.min_driver_version,
            "quantization": self.quantization,
            "short_description": self.short_description,
            "description": self.description,
            "features": list(self.features),
            "tags": list(self.tags),
            "size_bytes": self.size_bytes,
            "checksum_sha256": self.checksum_sha256,
            "variants": [v.to_wire() for v in self.variants],
        }


# ---------------------------------------------------------------------------
# aria2c status (5-state banner)
# ---------------------------------------------------------------------------


class Aria2cInstallStatus(str, Enum):
    """V1 ``aria2cStatus.install_status`` value set."""

    IDLE = "idle"
    INSTALLING = "installing"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class Aria2cStatus:
    """The aria2c availability snapshot driving the 5-state UI banner.

    Mirrors V1 ``downloader.get_status()`` exactly (field names + types):
    available / can_auto_install / exe_path / daemon_running / daemon_pid
    / rpc_port / install_status / install_error / bin_dir.
    """

    available: bool = False
    can_auto_install: bool = False
    exe_path: str = ""
    daemon_running: bool = False
    daemon_pid: int | None = None
    rpc_port: int = 6800
    install_status: Aria2cInstallStatus = Aria2cInstallStatus.IDLE
    install_error: str = ""
    bin_dir: str = ""

    def to_wire(self) -> dict[str, object]:
        return {
            "available": self.available,
            "can_auto_install": self.can_auto_install,
            "exe_path": self.exe_path,
            "daemon_running": self.daemon_running,
            "daemon_pid": self.daemon_pid,
            "rpc_port": self.rpc_port,
            "install_status": self.install_status.value,
            "install_error": self.install_error,
            "bin_dir": self.bin_dir,
        }


# ---------------------------------------------------------------------------
# Local disk status projections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalItemStatus:
    """Per-item downloaded/installed projection (V1 local-status entry)."""

    downloaded: bool = False
    save_path: str = ""
    installed: bool = False
    install_path: str = ""
    #: Platform discriminator parsed from the on-disk artifact name (the
    #: trailing driver tag, e.g. ``v81`` for Snapdragon X2 Elite / ``v73`` for
    #: X Elite). Lets the UI attribute the installed/downloaded state to the
    #: correct platform tab (the version dir/zip is per-platform). Empty when
    #: the name carries no recognisable tag. Additive wire field (§3.1).
    platform_driver: str = ""

    def to_wire(self) -> dict[str, object]:
        return {
            "downloaded": self.downloaded,
            "save_path": self.save_path,
            "installed": self.installed,
            "install_path": self.install_path,
            "platform_driver": self.platform_driver,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class VersionsLocalStatus:
    """``GET /api/versions/local-status`` projection."""

    versions: dict[str, LocalItemStatus] = field(default_factory=dict)
    auto_configured: bool = False
    auto_configured_path: str = ""

    def to_wire(self) -> dict[str, object]:
        return {
            "versions": {k: v.to_wire() for k, v in self.versions.items()},
            "auto_configured": self.auto_configured,
            "auto_configured_path": self.auto_configured_path,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelsLocalStatus:
    """``GET /api/service-catalog/local-status`` projection."""

    models: dict[str, LocalItemStatus] = field(default_factory=dict)

    def to_wire(self) -> dict[str, object]:
        return {"models": {k: v.to_wire() for k, v in self.models.items()}}


# ---------------------------------------------------------------------------
# Install / delete / config results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceInstallResult:
    ok: bool
    root_path: str
    exe_path: str
    version: str
    zip_deleted: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "root_path": self.root_path,
            "exe_path": self.exe_path,
            "version": self.version,
            "zip_deleted": self.zip_deleted,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelInstallResult:
    ok: bool
    install_path: str
    model_id: str
    zip_deleted: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "install_path": self.install_path,
            "model_id": self.model_id,
            "zip_deleted": self.zip_deleted,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadSettings:
    """forge_config download-section projection (read/write)."""

    save_dir: str = ""
    version_list_url: str = ""
    catalog_url: str = ""
    fetch_timeout_seconds: int = 15
    download_timeout_seconds: int = 300
    ssl_verify: bool = False

    def to_wire(self) -> dict[str, object]:
        return {
            "save_dir": self.save_dir,
            "version_list_url": self.version_list_url,
            "catalog_url": self.catalog_url,
            "fetch_timeout_seconds": self.fetch_timeout_seconds,
            "download_timeout_seconds": self.download_timeout_seconds,
            "ssl_verify": self.ssl_verify,
        }


__all__ = [
    "DownloadStatus",
    "DownloadEngineKind",
    "DownloadProgress",
    "ServicePackage",
    "ServiceVersion",
    "ModelHardware",
    "ModelFormat",
    "ModelVariant",
    "CatalogModel",
    "Aria2cInstallStatus",
    "Aria2cStatus",
    "LocalItemStatus",
    "VersionsLocalStatus",
    "ModelsLocalStatus",
    "ServiceInstallResult",
    "ModelInstallResult",
    "DownloadSettings",
]
