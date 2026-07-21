# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""forge_config download-section settings adapter (JSON file backed).

Ports the V1 ``forge_config_manager`` download-related properties:
``download.save_dir`` / ``version_check.version_list_url`` /
``model_catalog.catalog_url`` / ``version_check.fetch_timeout_seconds`` /
``version_check.download_timeout_seconds`` / ``version_check.ssl_verify``.

The file is read with ``utf-8-sig`` (BOM tolerant) and written back with a
shallow merge that preserves unrelated sections (V1 ``update()``
semantics). Defaults mirror V1 exactly (note ``ssl_verify`` defaults to
**False** for enterprise-network leniency).
"""

from __future__ import annotations

import json
from pathlib import Path

from qai.service_release.application.ports import DownloadSettingsPort
from qai.service_release.domain.value_objects import DownloadSettings

_DEFAULT_VERSION_LIST_URL = (
    "https://github.com/qualcomm/qai-appbuilder/releases/download/"
    "v2.34.0/release_manifest.json"
)
_DEFAULT_CATALOG_URL = (
    "https://github.com/qualcomm/qai-appbuilder/releases/download/"
    "v2.34.0/model_catalog.json"
)


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return default


class JsonForgeDownloadSettings(DownloadSettingsPort):
    """Reads/writes the download section of a forge_config JSON file."""

    __slots__ = ("_path",)

    def __init__(self, *, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        try:
            text = self._path.read_text(encoding="utf-8-sig")
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def read_save_dir(self) -> str:
        """Read just ``download.save_dir`` synchronously (V1 path override).

        Used by :class:`DownloadPaths.save_dir_provider` to resolve the live
        archive directory on each access without an async hop. Returns ``""``
        when unset (callers fall back to the default download dir).
        """
        data = self._load()
        dl = data.get("download", {})
        dl = dl if isinstance(dl, dict) else {}
        return str(dl.get("save_dir", ""))

    def read_genie_root_path(self) -> str:
        """Read ``genie_service.root_path`` synchronously (V1 parity).

        Used by the local-status scanner to decide whether the
        GenieAPIService install dir still needs auto-configuring
        (V1 ``main.py:4974``). Returns ``""`` when unset.
        """
        data = self._load()
        gs = data.get("genie_service", {})
        gs = gs if isinstance(gs, dict) else {}
        return str(gs.get("root_path", "") or "").strip()

    def write_genie_root_path(self, root_path: str) -> None:
        """Persist ``genie_service.root_path`` with V1 shallow-merge semantics.

        Mirrors V1 ``forge_config.update({"genie_service": {"root_path": ...}})``
        (``main.py:4989``): unrelated sections are preserved. Written with
        ``ensure_ascii=False`` + 2-space indent (this module's wire format).
        """
        data = self._load()
        gs = data.get("genie_service")
        if not isinstance(gs, dict):
            gs = {}
        gs["root_path"] = root_path
        data["genie_service"] = gs
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def read(self) -> DownloadSettings:
        data = self._load()
        vc = data.get("version_check", {})
        vc = vc if isinstance(vc, dict) else {}
        mc = data.get("model_catalog", {})
        mc = mc if isinstance(mc, dict) else {}
        dl = data.get("download", {})
        dl = dl if isinstance(dl, dict) else {}
        return DownloadSettings(
            save_dir=str(dl.get("save_dir", "")),
            version_list_url=(
                str(vc.get("version_list_url") or "") or _DEFAULT_VERSION_LIST_URL
            ),
            catalog_url=(
                str(mc.get("catalog_url") or "") or _DEFAULT_CATALOG_URL
            ),
            fetch_timeout_seconds=int(vc.get("fetch_timeout_seconds", 15) or 15),
            download_timeout_seconds=int(
                vc.get("download_timeout_seconds", 300) or 300
            ),
            ssl_verify=_coerce_bool(vc.get("ssl_verify", False), False),
        )

    async def write(self, settings: DownloadSettings) -> DownloadSettings:
        data = self._load()
        vc = data.get("version_check")
        if not isinstance(vc, dict):
            vc = {}
        mc = data.get("model_catalog")
        if not isinstance(mc, dict):
            mc = {}
        dl = data.get("download")
        if not isinstance(dl, dict):
            dl = {}
        dl["save_dir"] = settings.save_dir
        vc["version_list_url"] = settings.version_list_url
        vc["fetch_timeout_seconds"] = settings.fetch_timeout_seconds
        vc["download_timeout_seconds"] = settings.download_timeout_seconds
        vc["ssl_verify"] = settings.ssl_verify
        mc["catalog_url"] = settings.catalog_url
        data["version_check"] = vc
        data["model_catalog"] = mc
        data["download"] = dl
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return settings


__all__ = ["JsonForgeDownloadSettings"]
