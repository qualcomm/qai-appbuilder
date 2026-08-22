# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the platform uploads sub-module (PR-605).

This is NOT a bounded-context namespace — it's a platform shared kernel
module. The factory builds a :class:`FilesystemUploadStore` using
``container.data_paths`` to resolve the uploads root directory.

Usage (when I1 integrates into ``apps.api.main``)::

    from apps.api._uploads_di import build_upload_store
    from interfaces.http.routes.uploads import build_router as build_uploads_router

    store = build_upload_store(container)
    app.include_router(build_uploads_router(store=store))
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.platform.uploads import FilesystemUploadStore

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

__all__ = ["build_upload_store"]


def build_upload_store(container: "Container") -> FilesystemUploadStore:
    """Build the filesystem upload store from the container's data paths.

    The uploads root is ``<data_dir>/blobs/uploads``.
    """
    uploads_root = container.data_paths.blobs_dir / "uploads"
    return FilesystemUploadStore(base_dir=uploads_root)
