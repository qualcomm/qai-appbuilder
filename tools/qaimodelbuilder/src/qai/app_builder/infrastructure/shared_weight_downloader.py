# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Adapter loading the shared ``weight_downloader`` helper by file path.

``factory/app_builder/shared/weight_downloader.py`` holds the single
shared implementation of the device-detection + archive-extract logic used
by both the Pack runner subprocesses and the API side. Because ``factory/``
is NOT a Python package (it is shipped install assets, imported by the
spawned runners via a ``sys.path`` injection of the shared dir — see
``_app_builder_di.py`` ``_pack_shared_pythonpath`` / the runner bootstrap),
the API side loads it here with :func:`importlib.util.spec_from_file_location`
against the resolved shared-dir path rather than mutating the global
``sys.path`` (no process-wide side effect).

This adapter exposes exactly the two callables the
:class:`~qai.app_builder.application.use_cases.download_weights.DownloadModelWeightsUseCase`
needs — ``extract_weights_archive`` and ``detect_device_model`` — resolved
lazily on first use and cached. The DI root passes the resolved shared dir
(the same ``<repo_root>/factory/app_builder/shared`` path
``_pack_shared_pythonpath`` computes).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Callable

__all__ = ["SharedWeightDownloader"]

_MODULE_FILENAME = "weight_downloader.py"


class SharedWeightDownloader:
    """Lazy loader exposing the shared ``weight_downloader`` callables."""

    __slots__ = ("_shared_dir", "_module")

    def __init__(self, *, shared_dir: Path) -> None:
        self._shared_dir = Path(shared_dir)
        self._module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if self._module is not None:
            return self._module
        path = self._shared_dir / _MODULE_FILENAME
        if not path.is_file():
            raise FileNotFoundError(
                f"shared weight_downloader not found at {path}"
            )
        spec = importlib.util.spec_from_file_location(
            "qai_app_builder_shared_weight_downloader", path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load weight_downloader from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module

    @property
    def extract_weights_archive(self) -> Callable[..., None]:
        """Return the shared ``extract_weights_archive`` callable."""
        return self._load().extract_weights_archive

    @property
    def detect_device_model(self) -> Callable[[], str]:
        """Return a zero-arg wrapper over ``detect_device_model``.

        The shared helper's signature is ``detect_device_model(*, tag=...)``;
        the use case injects a plain ``Callable[[], str]``, so wrap it.
        """
        fn = self._load().detect_device_model
        return lambda: fn()
