# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Artifact installer + local-status scanner + aria2c manager + settings.

These adapters port the filesystem/process algorithms from the V1
download center (``backend/main.py`` install/delete/local-status handlers,
``backend/aria2c_downloader.py`` status/cancel, ``forge_config_manager.py``
download section).

They live in ``adapters`` (not ``infrastructure``) because they touch the
local filesystem/process exactly like the model_catalog SQLite adapters
touch the DB — they are the "driven" side of the hexagon.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from qai.service_release.application.ports import (
    Aria2cManagerPort,
    ArtifactInstallerPort,
    DownloadSettingsPort,
    LocalStatusScannerPort,
)
from qai.service_release.domain.errors import (
    DownloadNotFoundError,
    InstallFailedError,
)
from qai.service_release.infrastructure.aria2c_daemon import (
    RPC_PORT as _ARIA2C_RPC_PORT,
)
from qai.service_release.infrastructure.download_engine import HttpxDownloadEngine
from qai.service_release.infrastructure.download_paths import DownloadPaths
from qai.service_release.domain.value_objects import (
    Aria2cInstallStatus,
    Aria2cStatus,
    LocalItemStatus,
    ModelInstallResult,
    ModelsLocalStatus,
    ServiceInstallResult,
    VersionsLocalStatus,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.service_release.infrastructure.aria2c_daemon import Aria2cDaemon

_SERVICE_EXE = "GenieAPIService.exe"
_VERSION_DIR_RE = re.compile(r"^\d+\.\d+")
_INSTALLED_VER_RE = re.compile(r"[_v](\d+\.\d+(?:\.\d+)?)")
#: Trailing driver/platform tag in an artifact name, e.g. the ``v81`` in
#: ``GenieAPIService_v2.3.7_QAIRT_v2.44.0_v81`` (driver v81 = Snapdragon X2
#: Elite, v73 = X Elite). It is the LAST ``_v<digits>`` group (the QAIRT
#: ``_v2.44.0`` in the middle is skipped because the tag has no dots).
_DRIVER_TAG_RE = re.compile(r"_v(\d+)(?![.\d])")
#: File written into the install dir at ``install_model`` time so the scanner
#: can recover which platform variant was installed. The shared install dir
#: ``models/<model_id>/`` carries no platform info in its name (both tabs show
#: a consistent path), so without this marker the UI cannot label the
#: Installed pill with the platform — V2 mirrors ServiceVersionCard's
#: "Installed · <platform>" affordance for parity.
_INSTALL_MARKER = ".qai-install.json"


def _variant_id_from_model_config(model_dir: Path) -> str:
    """Recover the installed variant_id from a model's ``config.json`` paths.

    State-Truth-First fallback for installed models that carry NO
    ``.qai-install.json`` marker (e.g. placed manually or installed by an
    older build) AND whose flattened dir name lacks a variant suffix
    (``models/qwen3-8b/`` rather than ``models/qwen3-8b-8480/``).

    Genie ``config.json`` embeds the ORIGINAL variant directory in every
    internal path it references — tokenizer / ctx-bins / extensions all look
    like ``models/<variant_id>/<file>`` (e.g.
    ``models/qwen3-8b-8480/tokenizer.json``).  That path segment is the
    authoritative on-disk record of which platform variant was installed, so
    we read it back here.  This resolves the multi-variant case the dir name
    and ``_driver_tag_of`` cannot (both variants flatten to the same dir).

    Returns the extracted variant_id (e.g. ``"qwen3-8b-8480"``), or ``""`` when
    no config / no embedded path is found (caller degrades to a bare pill).
    """
    config_path = model_dir / "config.json"
    if not config_path.is_file():
        return ""
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    # Match the FIRST ``models/<segment>/`` reference. ``<segment>`` is the
    # variant dir the artifact was built against; it is more specific than the
    # (flattened) install dir name. Anchored on ``models/`` to avoid matching
    # unrelated slashes; the segment excludes path separators and quotes.
    m = re.search(r'models[\\/]+([^\\/"\']+)[\\/]', raw)
    if not m:
        return ""
    seg = m.group(1).strip()
    # Guard against the degenerate case where the embedded path already uses
    # the flattened dir name (no extra info) — still return it; the caller
    # maps it to a catalog variant and a non-match simply yields no platform.
    return seg


def _safe_dir_segment(name: str) -> str:
    """Sanitise a caller-supplied name into one filesystem-safe dir segment.

    * Takes only the final path component (defeats ``../`` / nested paths).
    * Collapses internal whitespace runs to a single ``-`` (so a display name
      like ``"Qwen3 8B"`` becomes ``"Qwen3-8B"`` rather than a space-containing
      dir that breaks the UI's id-based local-status lookup).
    * Drops characters illegal in Windows file names and control chars; trims
      trailing dots/spaces (also illegal on Windows).

    Returns ``""`` when nothing usable remains, so the caller can fall back.
    """
    if not name:
        return ""
    seg = re.split(r"[\\/]+", name.strip())[-1]  # final segment only
    seg = re.sub(r"\s+", "-", seg.strip())  # whitespace runs → single hyphen
    seg = re.sub(r'[<>:"|?*\x00-\x1f]', "", seg)  # Windows-illegal chars
    return seg.rstrip(". ")  # Windows: no trailing dot/space


def _driver_tag_of(name: str) -> str:
    """Extract the trailing driver tag (e.g. ``v81``) from an artifact name.

    Accepts a bare dir name or a ``*.zip`` file name (the extension is
    stripped first so the ``v81`` in ``...v81.zip`` is still the tail).
    Returns ``""`` when the name carries no recognisable ``_v<digits>`` tail
    (so callers degrade to version-only, platform-agnostic behaviour).
    """
    stem = name[:-4] if name.lower().endswith(".zip") else name
    matches = _DRIVER_TAG_RE.findall(stem)
    return f"v{matches[-1]}" if matches else ""



def _extract_zip_to_dir(zip_path: Path, dest_dir: Path, *, tmp_base: Path) -> None:
    """Unzip with V1 single-top-level-dir flattening.

    V1 parity (``backend/main.py:4438-4488``): when the archive has a single
    top-level entry, its *contents* are promoted into ``dest_dir`` to avoid a
    redundant nested directory (e.g. ``models/Qwen3 8B/qwen3-8b-8380/...``).
    The flatten triggers on ``len(top_level) == 1`` alone — matching V1 — so
    archives that include an explicit bare directory entry (``qwen3-8b-8380/``)
    still flatten correctly. (The previous extra ``all(...)`` guard wrongly
    skipped flattening for such archives, leaving a two-level nest that broke
    the daemon's ``-c <models_root>/<model>/config.json`` path resolution.)
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if n and not n.startswith("__MACOSX")]
            top_level = {n.split("/")[0].split("\\")[0] for n in names}
            top_level.discard("")
            if len(top_level) == 1:
                tmp_extract = tmp_base / f"{zip_path.stem}_tmp_extract"
                if tmp_extract.exists():
                    shutil.rmtree(tmp_extract, ignore_errors=True)
                tmp_extract.mkdir(parents=True, exist_ok=True)
                zf.extractall(tmp_extract)
                inner = tmp_extract / next(iter(top_level))
                dest_dir.mkdir(parents=True, exist_ok=True)
                source = inner if inner.is_dir() else tmp_extract
                for item in source.iterdir():
                    shutil.move(str(item), str(dest_dir / item.name))
                shutil.rmtree(tmp_extract, ignore_errors=True)
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                zf.extractall(dest_dir)
    except zipfile.BadZipFile as exc:
        raise InstallFailedError(f"corrupt archive: {exc}") from exc
    except OSError as exc:
        raise InstallFailedError(f"extraction failed: {exc}") from exc


def _delete_zip_after_install(zip_path: Path) -> bool:
    try:
        zip_path.unlink(missing_ok=True)
        parent = zip_path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
        return True
    except OSError:
        return False


def _processes_under(prefix: Path) -> list[int]:
    """Return PIDs whose executable image lives under ``prefix`` (Windows).

    File-level real-state probe via ``wmic`` (already on Windows; same
    approach as the legacy uninstaller). Returns [] on non-Windows or when
    ``wmic`` is unavailable.
    """
    if sys.platform != "win32":
        return []
    prefix_str = str(prefix).replace("\\", "\\\\")  # wmic LIKE escape
    try:
        out = subprocess.check_output(  # noqa: S603
            [
                "wmic",
                "process",
                "where",
                f"ExecutablePath like '{prefix_str}%%'",
                "get",
                "ProcessId",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pid = int(line)
            if pid > 4:  # skip system PIDs
                pids.append(pid)
    return pids


def _kill_processes_under(prefix: Path) -> None:
    """Force-kill any process whose image path lives under ``prefix``.

    Windows holds a hard lock on a loaded DLL/exe (``GenieAPIService.exe`` /
    its ``Genie.dll``), so deleting an install dir while the service is still
    running fails with ``WinError 5 Access denied``. Before deleting we stop
    the owning process so the OS releases the handles (real-state-first:
    we don't assume it's stopped, we make it so).

    Scope is intentionally narrowed to ``prefix`` (the exact dir being
    deleted) to avoid killing an unrelated GenieAPIService that another
    install dir is running. This is the FORCE fallback for an orphan the
    graceful ``service_stopper`` can't reach; best-effort + non-fatal.
    """
    for pid in _processes_under(prefix):
        try:
            subprocess.run(  # noqa: S603, S607
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            pass


def _rmtree_unlocking(target: Path) -> None:
    """``shutil.rmtree`` that copes with a still-running GenieAPIService.

    Strategy (escalating, each step cheap):
      1. plain ``rmtree``;
      2. on ``PermissionError`` / ``OSError`` (WinError 5 — loaded DLL/exe
         locked), kill the process(es) running out of ``target``, wait a
         moment for the OS to release handles, then retry a few times.

    Raises the final :class:`OSError` if the tree still can't be removed, so
    the caller can surface an actionable message rather than a bare WinError.
    """
    try:
        shutil.rmtree(target)
        return
    except OSError:
        pass
    _kill_processes_under(target)
    last_exc: OSError | None = None
    for _attempt in range(5):
        time.sleep(0.4)  # let Windows release the file handles
        try:
            shutil.rmtree(target)
            return
        except OSError as exc:
            last_exc = exc
    if last_exc is not None:
        raise last_exc



class FileSystemArtifactInstaller(ArtifactInstallerPort):
    """Installs/deletes archives on the local filesystem (V1 parity)."""

    __slots__ = ("_paths", "_service_stopper")

    def __init__(
        self,
        *,
        paths: DownloadPaths,
        service_stopper: "Callable[[], Awaitable[None]] | None" = None,
    ) -> None:
        self._paths = paths
        # Optional async hook to GRACEFULLY stop the running GenieAPIService
        # before deleting its install dir (so the OS releases the Genie.dll
        # handle and the delete doesn't hit WinError 5). Wired in the apps DI
        # layer to ``model_runtime``'s stop() (CTRL_BREAK + timeout, V1 parity)
        # — keeping ``service_release`` context-isolated (§3.2: cross-context
        # collaboration lives in the apps bridge, not via a direct import).
        # When None (tests / no daemon), deletion relies on the in-adapter
        # process-kill fallback for any orphan holding the lock.
        self._service_stopper = service_stopper

    async def install_service(
        self, *, save_path: str, version: str
    ) -> ServiceInstallResult:
        zip_path = Path(save_path)
        if not zip_path.exists() or zip_path.suffix.lower() != ".zip":
            raise InstallFailedError(f"invalid save_path: {save_path}")
        bin_dir = self._paths.bin_dir
        bin_dir.mkdir(parents=True, exist_ok=True)
        dest_dir = bin_dir / zip_path.stem
        bak_dir = dest_dir.with_suffix(".bak")
        if dest_dir.exists():
            if bak_dir.exists():
                shutil.rmtree(bak_dir, ignore_errors=True)
            dest_dir.rename(bak_dir)
        try:
            _extract_zip_to_dir(zip_path, dest_dir, tmp_base=bin_dir)
        except InstallFailedError:
            if bak_dir.exists() and not dest_dir.exists():
                bak_dir.rename(dest_dir)
            raise
        if bak_dir.exists():
            shutil.rmtree(bak_dir, ignore_errors=True)
        zip_deleted = _delete_zip_after_install(zip_path)
        return ServiceInstallResult(
            ok=True,
            root_path=str(dest_dir),
            exe_path=str(dest_dir / _SERVICE_EXE),
            version=version,
            zip_deleted=zip_deleted,
        )

    async def install_model(
        self, *, save_path: str, model_id: str, install_dir: str = "",
        variant_id: str = "",
    ) -> ModelInstallResult:
        zip_path = Path(save_path)
        if not zip_path.exists() or zip_path.suffix.lower() != ".zip":
            raise InstallFailedError(f"invalid save_path: {save_path}")
        models_root = self._paths.models_dir
        models_root.mkdir(parents=True, exist_ok=True)
        # Defence in depth: the install dir name must be a single, filesystem-
        # safe segment that matches the catalog id the UI looks up by. We
        # sanitise whatever hint the caller gave (the frontend now passes the
        # canonical task id, but a stray display name like "Qwen3 8B" must
        # never again create a space-containing dir that the 3-level local-
        # status lookup can't match). Falls back to the zip stem.
        dest_name = _safe_dir_segment(install_dir) or _safe_dir_segment(
            zip_path.stem
        )
        dest_dir = models_root / dest_name
        bak_dir = dest_dir.with_suffix(".bak")
        if dest_dir.exists():
            if bak_dir.exists():
                shutil.rmtree(bak_dir, ignore_errors=True)
            dest_dir.rename(bak_dir)
        try:
            _extract_zip_to_dir(zip_path, dest_dir, tmp_base=models_root)
        except InstallFailedError:
            if bak_dir.exists() and not dest_dir.exists():
                bak_dir.rename(dest_dir)
            raise
        if bak_dir.exists():
            shutil.rmtree(bak_dir, ignore_errors=True)
        # Persist which platform variant was installed. A model installs ONE
        # shared copy under ``models/<model_id>/`` (no platform in the dir
        # name, so both tabs show a consistent path), but the UI still wants
        # to label the Installed pill with the platform ("Installed ·
        # Snapdragon X2 Elite", mirroring ServiceVersionCard). We can't
        # recover the platform from the flattened dir or the (now-deleted)
        # zip name, so we drop a tiny marker the scanner reads back. Only
        # written when the caller actually supplies a ``variant_id`` — empty
        # ⇒ single-platform / legacy caller, the bare "✓ Installed" pill is
        # the right degradation. Best-effort: a write failure must not fail
        # the install (the model is already extracted at this point).
        marker_variant = _safe_dir_segment(variant_id)
        if marker_variant:
            try:
                (dest_dir / _INSTALL_MARKER).write_text(
                    json.dumps({"variant_id": marker_variant}),
                    encoding="utf-8",
                )
            except OSError:
                pass
        zip_deleted = _delete_zip_after_install(zip_path)
        return ModelInstallResult(
            ok=True,
            install_path=str(dest_dir),
            model_id=model_id or dest_name,
            zip_deleted=zip_deleted,
        )

    def _install_dirs_for(self, version: str) -> list[Path]:
        """Installed dirs (under bin/) whose name matches ``version``."""
        pattern = re.compile(r"(?<![.\d])" + re.escape(version) + r"(?![.\d])")
        return [
            child
            for child in self._paths.bin_dir.glob("*")
            if child.is_dir()
            and not child.name.endswith(".bak")
            and pattern.search(child.name)
        ]

    @staticmethod
    def _model_dirs_matching(parent: Path, model_id: str) -> list[Path]:
        """Dirs under ``parent`` whose name equals ``model_id`` OR starts with
        ``<model_id>`` followed by a separator (``-`` / ``_`` / ``.``).

        Lets ``delete_model`` clear both the new shared layout
        (``models/qwen3-8b/``) and the legacy per-variant layout
        (``models/qwen3-8b-8480-qnn2.44/``) under one ``model_id`` request,
        mirroring the UI's lenient lookup so "Shows Installed but Delete 404s"
        cannot recur on a layout migration.
        """
        if not parent.exists():
            return []
        out: list[Path] = []
        for child in parent.iterdir():
            if not child.is_dir() or child.name.endswith(".bak"):
                continue
            name = child.name
            if name == model_id:
                out.append(child)
                continue
            if name.startswith(model_id) and len(name) > len(model_id):
                sep = name[len(model_id)]
                if sep in ("-", "_", "."):
                    out.append(child)
        return out

    async def is_installed_service_running(self, *, version: str) -> bool:
        """Best-effort: is any process running out of the version's install dir?

        File-level real-state probe (NOT the service status machine): we ask
        the OS which process has an image loaded under the dir we're about to
        delete. This catches an orphan GenieAPIService (e.g. spawned by an old
        backend) that the model_runtime ``poll()``-based status can't see —
        exactly the "shows Stopped but actually running" case behind WinError 5.
        Returns False on non-Windows / no wmic.
        """
        for child in self._install_dirs_for(version):
            if await asyncio.to_thread(_processes_under, child):
                return True
        return False

    async def delete_installed_service(
        self, *, version: str, stop_running: bool = False
    ) -> dict[str, object]:
        pattern = re.compile(r"(?<![.\d])" + re.escape(version) + r"(?![.\d])")
        # Graceful first: if asked, stop the managed GenieAPIService so the OS
        # releases its loaded Genie.dll before we delete (real-state-first:
        # don't assume it's stopped — make it so). Best-effort; an orphan the
        # manager doesn't own is handled by the per-dir kill fallback below.
        if stop_running and self._service_stopper is not None:
            try:
                await self._service_stopper()
            except Exception:  # noqa: BLE001 — never block delete on stop error
                pass
        deleted: list[str] = []
        for child in self._paths.bin_dir.glob("*"):
            if child.is_dir() and not child.name.endswith(".bak") and pattern.search(
                child.name
            ):
                try:
                    # rmtree that first stops a still-running GenieAPIService
                    # whose loaded Genie.dll would otherwise lock the dir
                    # (WinError 5). Run off the event loop: the kill+retry
                    # sleeps would otherwise block it.
                    await asyncio.to_thread(_rmtree_unlocking, child)
                    deleted.append(str(child))
                except OSError as exc:
                    raise InstallFailedError(
                        f"delete failed: {exc}. The GenieAPIService at "
                        f"{child} is still running or its files are locked. "
                        "Stop the service (Service page → Stop, or close the "
                        "app) and try again; if it persists, reboot."
                    ) from exc
        if not deleted:
            raise DownloadNotFoundError(version, "no installed version directory found")
        return {"ok": True, "deleted_paths": deleted, "version": version}

    async def delete_downloaded_service(self, *, version: str) -> dict[str, object]:
        save_dir = self._paths.version_save_dir(version)
        deleted: list[str] = []
        if save_dir.exists():
            for zip_file in save_dir.glob("*.zip"):
                try:
                    zip_file.unlink()
                    deleted.append(str(zip_file))
                    aria = zip_file.with_suffix(zip_file.suffix + ".aria2")
                    aria.unlink(missing_ok=True)
                except OSError:
                    pass
            if not any(save_dir.iterdir()):
                save_dir.rmdir()
        if not deleted:
            raise DownloadNotFoundError(version, "no downloaded zip found")
        return {"ok": True, "deleted_files": deleted, "version": version}

    async def delete_model(
        self, *, model_id: str, delete_zip: bool = True
    ) -> dict[str, object]:
        deleted_install: list[str] = []
        deleted_zip: list[str] = []
        # Resolve the on-disk dirs to delete. A model installs ONE shared copy
        # keyed by model_id, but we tolerate dirs whose name *starts with*
        # model_id (legacy per-variant layout like ``qwen3-8b-8480-qnn2.44``)
        # so existing installs remain deletable after the model-level switch —
        # mirroring the 3-level lookup the scanner/UI use to recognise them.
        # Matching the UI's view (State-Truth-First) avoids the "shows
        # Installed but Delete 404s" desync.
        install_targets = self._model_dirs_matching(self._paths.models_dir, model_id)
        for install_dir in install_targets:
            try:
                await asyncio.to_thread(_rmtree_unlocking, install_dir)
                deleted_install.append(str(install_dir))
            except OSError as exc:
                raise InstallFailedError(
                    f"delete failed: {exc}. The model files at {install_dir} "
                    "are locked (a service may have them open). Stop the "
                    "service / close the app and try again."
                ) from exc
        if delete_zip:
            for save_dir in self._model_dirs_matching(
                self._paths.download_dir, model_id
            ):
                for zip_file in save_dir.glob("*.zip"):
                    try:
                        zip_file.unlink()
                        deleted_zip.append(str(zip_file))
                        zip_file.with_suffix(zip_file.suffix + ".aria2").unlink(
                            missing_ok=True
                        )
                    except OSError:
                        pass
                if save_dir.exists() and not any(save_dir.iterdir()):
                    save_dir.rmdir()
        if not deleted_install and not deleted_zip:
            raise DownloadNotFoundError(model_id, "no model artifacts found")
        return {
            "ok": True,
            "deleted_install_dirs": deleted_install,
            "deleted_zip_files": deleted_zip,
            "model_id": model_id,
        }


class FileSystemLocalStatusScanner(LocalStatusScannerPort):
    """Re-derives downloaded/installed state by scanning the disk (V1 parity)."""

    __slots__ = ("_paths", "_on_auto_configure", "_read_root_path")

    def __init__(
        self,
        *,
        paths: DownloadPaths,
        on_auto_configure: Callable[[str], None] | None = None,
        read_root_path: Callable[[], str] | None = None,
    ) -> None:
        self._paths = paths
        # Optional hook to persist auto-detected root_path (V1 auto_configured).
        # Receives the auto-detected install dir; the DI layer wires it to
        # write ``forge_config.genie_service.root_path`` (V1 main.py:4989).
        self._on_auto_configure = on_auto_configure
        # Optional sync reader of the current ``genie_service.root_path``.
        # When it returns a non-empty value the auto-configure step is
        # skipped (V1 only auto-writes when root_path is unset).
        self._read_root_path = read_root_path

    async def scan_versions(self) -> VersionsLocalStatus:
        versions: dict[str, LocalItemStatus] = {}
        # Downloaded scan: downloads/<version>/*.zip (complete only, no .aria2).
        if self._paths.download_dir.exists():
            for child in self._paths.download_dir.iterdir():
                if not child.is_dir() or not _VERSION_DIR_RE.match(child.name):
                    continue
                for zip_file in child.glob("*.zip"):
                    aria = zip_file.with_suffix(zip_file.suffix + ".aria2")
                    if aria.exists():
                        continue
                    prev = versions.get(child.name)
                    versions[child.name] = LocalItemStatus(
                        downloaded=True,
                        save_path=str(zip_file),
                        installed=prev.installed if prev else False,
                        install_path=prev.install_path if prev else "",
                        # Platform tag from the zip name (e.g. v81); keep an
                        # already-known installed tag if the downloaded zip
                        # carries none.
                        platform_driver=_driver_tag_of(zip_file.name)
                        or (prev.platform_driver if prev else ""),
                    )
                    break
        # Installed scan: bin/<dir-with-version>/GenieAPIService.exe.
        if self._paths.bin_dir.exists():
            for child in self._paths.bin_dir.iterdir():
                if not child.is_dir() or child.name.endswith(".bak"):
                    continue
                if not (child / _SERVICE_EXE).exists():
                    continue
                m = _INSTALLED_VER_RE.search(child.name)
                if not m:
                    continue
                ver = m.group(1)
                prev = versions.get(ver)
                versions[ver] = LocalItemStatus(
                    downloaded=prev.downloaded if prev else False,
                    save_path=prev.save_path if prev else "",
                    installed=True,
                    install_path=str(child),
                    # Installed platform tag from the dir name (e.g. v81);
                    # this is the authoritative one once installed.
                    platform_driver=_driver_tag_of(child.name)
                    or (prev.platform_driver if prev else ""),
                )
        # V1 parity (main.py:4969-4996): when ``genie_service.root_path`` is
        # unset but a GenieAPIService install exists on disk, auto-configure
        # it to the newest installed version so the Service/Settings pages
        # detect the daemon after a forge_config reset or first run.
        auto_configured = False
        auto_configured_path = ""
        if self._on_auto_configure is not None:
            current_root = ""
            if self._read_root_path is not None:
                try:
                    current_root = (self._read_root_path() or "").strip()
                except Exception:  # noqa: BLE001 — convenience read; never fatal
                    current_root = ""
            if not current_root:
                best_path = self._newest_installed_path(versions)
                if best_path:
                    try:
                        self._on_auto_configure(best_path)
                        auto_configured = True
                        auto_configured_path = best_path
                    except Exception:  # noqa: BLE001 — never fail the scan
                        auto_configured = False
                        auto_configured_path = ""
        return VersionsLocalStatus(
            versions=versions,
            auto_configured=auto_configured,
            auto_configured_path=auto_configured_path,
        )

    @staticmethod
    def _newest_installed_path(versions: dict[str, LocalItemStatus]) -> str:
        """Return the install_path of the newest installed version (V1 sort)."""
        installed = [
            (ver, info.install_path)
            for ver, info in versions.items()
            if info.installed and info.install_path
        ]
        if not installed:
            return ""

        def _ver_key(v: str) -> list[int]:
            try:
                return [int(x) for x in v.split(".")]
            except ValueError:
                return [0]

        installed.sort(key=lambda x: _ver_key(x[0]), reverse=True)
        return installed[0][1]

    async def scan_models(self) -> ModelsLocalStatus:
        # Multi-platform models share one save dir keyed by ``model_id`` (the
        # zip filename carries the per-platform driver tag, e.g.
        # ``qwen3-8b-8480.zip``). Likewise an installed model lives under
        # ``models/<model_id>/`` regardless of which platform was installed —
        # ``platform_driver`` records *which* platform's artifact is on disk
        # so the UI can highlight the matching tab and pill (V1-style
        # version-level aggregation, see ``scan_versions`` for the parallel).
        models: dict[str, LocalItemStatus] = {}
        # Downloaded: downloads/<model_id>/*.zip (skip version dirs).
        if self._paths.download_dir.exists():
            for child in self._paths.download_dir.iterdir():
                if not child.is_dir() or _VERSION_DIR_RE.match(child.name):
                    continue
                # Pick the newest complete (no .aria2 sibling) zip; surface its
                # platform_driver so a partially-cancelled-then-resumed cohort
                # still attributes correctly. Multiple platform zips can
                # coexist here — we record the first complete one as the
                # downloaded artifact (Install picks by zip filename anyway).
                for zip_file in child.glob("*.zip"):
                    if zip_file.with_suffix(zip_file.suffix + ".aria2").exists():
                        continue
                    prev = models.get(child.name)
                    models[child.name] = LocalItemStatus(
                        downloaded=True,
                        save_path=str(zip_file),
                        installed=prev.installed if prev else False,
                        install_path=prev.install_path if prev else "",
                        platform_driver=_driver_tag_of(zip_file.name)
                        or (prev.platform_driver if prev else ""),
                    )
                    break
        # Installed: models/<model_id>/ containing model files.
        if self._paths.models_dir.exists():
            for child in self._paths.models_dir.iterdir():
                if not child.is_dir() or child.name.endswith(".bak"):
                    continue
                has_model = (
                    (child / "config.json").exists()
                    or any(child.glob("*.gguf"))
                    or any(child.glob("*.mnn"))
                    or any(child.glob("*.bin"))
                )
                if not has_model:
                    continue
                prev = models.get(child.name)
                # Recover which platform was installed. Priority:
                #   1. .qai-install.json marker (authoritative — written at
                #      install time). Stored under ``platform_driver`` as the
                #      ``variant_id`` so the UI can map it back to the chip
                #      label without a separate field.
                #   2. ``_v<digits>`` driver tag in the dir / file names
                #      (legacy fallback for ServiceVersion-style artifacts).
                #   3. Previous downloaded entry's tag (best effort).
                marker_variant = ""
                marker_path = child / _INSTALL_MARKER
                if marker_path.is_file():
                    try:
                        data = json.loads(
                            marker_path.read_text(encoding="utf-8")
                        )
                        if isinstance(data, dict):
                            mv = data.get("variant_id", "")
                            if isinstance(mv, str):
                                marker_variant = mv.strip()
                    except (OSError, ValueError):
                        marker_variant = ""
                drv = marker_variant or _driver_tag_of(child.name)
                if not drv:
                    for f in child.iterdir():
                        if not f.is_file():
                            continue
                        tag = _driver_tag_of(f.name)
                        if tag:
                            drv = tag
                            break
                if not drv:
                    # State-Truth-First fallback: read the variant_id embedded
                    # in the model's config.json internal paths
                    # (``models/<variant_id>/...``). Resolves multi-variant,
                    # marker-less installs (e.g. qwen3-8b with both 8380/8480
                    # variants in the catalog) that the dir name and
                    # ``_driver_tag_of`` cannot disambiguate.
                    drv = _variant_id_from_model_config(child)
                models[child.name] = LocalItemStatus(
                    downloaded=prev.downloaded if prev else False,
                    save_path=prev.save_path if prev else "",
                    installed=True,
                    install_path=str(child),
                    platform_driver=drv
                    or (prev.platform_driver if prev else ""),
                )
        return ModelsLocalStatus(models=models)


class Aria2cManager(Aria2cManagerPort):
    """aria2c status / start / stop / cancel, backed by :class:`Aria2cDaemon`.

    Delegates binary discovery, auto-install and RPC-daemon lifecycle to the
    injected :class:`Aria2cDaemon` (infrastructure). Cancellation signals the
    download engine's per-task event (httpx path) *and* — when a daemon is
    live — removes the active aria2c gid via RPC.

    When no daemon is wired (legacy / tests), it degrades to the httpx-only
    behaviour: ``available`` reflects a PATH lookup, downloads use httpx, and
    start/stop are no-ops.

    V1 source-of-truth: ``backend/aria2c_downloader.py`` get_status/start/stop.
    """

    __slots__ = ("_paths", "_engine", "_daemon", "_binary_resolver")

    def __init__(
        self,
        *,
        paths: DownloadPaths,
        engine: "HttpxDownloadEngine",
        daemon: "Aria2cDaemon | None" = None,
        binary_resolver: Callable[[], str | None] | None = None,
    ) -> None:
        self._paths = paths
        self._engine = engine
        self._daemon = daemon
        self._binary_resolver = binary_resolver or (lambda: shutil.which("aria2c"))

    async def get_status(self) -> Aria2cStatus:
        if self._daemon is not None:
            d = self._daemon
            # ``is_rpc_alive`` issues a synchronous urlopen (timeout 5s) against
            # the RPC daemon — running it inline would block the asyncio event
            # loop for up to 5s.  Off-load to a worker thread (same pattern as
            # start()/stop()).  ``daemon_pid`` only reads in-memory process
            # attributes, so it stays inline.
            daemon_running = await asyncio.to_thread(d.is_rpc_alive)
            return Aria2cStatus(
                available=d.available,
                can_auto_install=d.can_auto_install,
                exe_path=d.exe_path,
                daemon_running=daemon_running,
                daemon_pid=d.daemon_pid(),
                rpc_port=_ARIA2C_RPC_PORT,
                install_status=d.install_status,
                install_error=d.install_error,
                bin_dir=str(d.bin_dir),
            )
        # Legacy path: PATH lookup only, no daemon lifecycle.
        exe = self._binary_resolver()
        available = exe is not None
        is_windows = sys.platform.startswith("win")
        can_auto_install = (
            not available and is_windows and self._paths.bin_dir is not None
        )
        return Aria2cStatus(
            available=available,
            can_auto_install=can_auto_install,
            exe_path=exe or "",
            daemon_running=False,
            daemon_pid=None,
            rpc_port=_ARIA2C_RPC_PORT,
            install_status=Aria2cInstallStatus.IDLE,
            install_error="",
            bin_dir=str(self._paths.bin_dir),
        )

    async def start(self) -> Aria2cStatus:
        """Ensure the binary + RPC daemon are ready (V1 POST /start)."""
        if self._daemon is not None:
            if await self._daemon.ensure_binary():
                await asyncio.to_thread(self._daemon.ensure_daemon)
        return await self.get_status()

    async def stop(self) -> Aria2cStatus:
        """Stop the RPC daemon (V1 POST /stop)."""
        if self._daemon is not None:
            await asyncio.to_thread(self._daemon.stop_daemon)
        return await self.get_status()

    async def cancel(self, *, task_id: str) -> bool:
        # Signal the httpx/aria2c stream loop to stop (it removes the gid).
        return self._engine.request_cancel(task_id)


__all__ = [
    "FileSystemArtifactInstaller",
    "FileSystemLocalStatusScanner",
    "Aria2cManager",
]
