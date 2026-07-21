# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem path resolution for the ``service_release`` download center.

V1 keeps three working directories (relative to the WebUI dir, overridable
via forge_config):

* ``downloads/`` — where archives are streamed to (``save_dir``).
* ``bin/``       — where GenieAPIService versions are installed.
* ``models/``    — where models are installed.

In V2 these are resolved from the injected :class:`DownloadPaths` (wired
in DI from ``DataPaths`` + the forge_config download section). Keeping the
resolution in one immutable object lets every adapter share identical
path logic without re-reading config.

V1 ``forge_config.download.save_dir`` override
----------------------------------------------
V1 lets the user relocate downloaded archives via
``forge_config.download.save_dir``. That setting is mutable at runtime
(``PUT /api/versions/download-settings``), so ``download_dir`` is resolved
**per access** through an optional ``save_dir_provider`` callable rather
than frozen once at DI build time. When the provider returns a non-empty
path it overrides the default download directory; an empty / missing
value falls back to ``default_download_dir`` (V1 behaviour). ``bin_dir``
and ``models_dir`` are *not* relocated by ``save_dir`` (V1 installs the
service/models under the data root regardless of where archives land).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DownloadPaths:
    """Resolved working directories for the download center.

    ``default_download_dir`` is the fallback archive directory. A non-empty
    ``save_dir_provider()`` result overrides it at access time (V1
    ``forge_config.download.save_dir``). Adapters always read the
    :attr:`download_dir` *property* so the override stays live across
    runtime settings changes.
    """

    default_download_dir: Path
    bin_dir: Path
    models_dir: Path
    save_dir_provider: Callable[[], str | None] | None = field(
        default=None, compare=False
    )

    @property
    def download_dir(self) -> Path:
        """Current archive directory, honouring the save_dir override.

        Returns ``default_download_dir`` when no provider is wired or the
        provider yields an empty value (V1 default behaviour).
        """
        if self.save_dir_provider is not None:
            raw = self.save_dir_provider()
            if raw and raw.strip():
                return Path(raw.strip())
        return self.default_download_dir

    def version_save_dir(self, version: str) -> Path:
        return self.download_dir / version

    def model_save_dir(self, model_id: str) -> Path:
        return self.download_dir / model_id

    def ensure(self) -> None:
        for d in (self.download_dir, self.bin_dir, self.models_dir):
            d.mkdir(parents=True, exist_ok=True)


__all__ = ["DownloadPaths"]
