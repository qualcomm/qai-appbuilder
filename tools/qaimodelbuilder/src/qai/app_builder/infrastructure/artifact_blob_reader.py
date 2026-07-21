# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem-backed :class:`ArtifactBlobReaderPort` (PR-045).

Reads bytes previously written by :class:`FileSystemArtifactStore`,
streaming them in 64 KiB chunks so the HTTP layer can pipe them
straight to a ``StreamingResponse``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from qai.platform.errors import NotFoundError, PersistenceError

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config import DataPaths

__all__ = ["FileSystemArtifactBlobReader"]


_CHUNK_SIZE = 64 * 1024


class FileSystemArtifactBlobReader:
    """Read blob bytes for a previously-written artifact.

    Resolves ``data/blobs/app_builder/<run_id>/<relative_path>`` and
    yields chunks of up to 64 KiB. If the file is absent the iterator
    raises :class:`qai.platform.errors.NotFoundError` on first read.
    """

    __slots__ = ("_data_paths",)

    def __init__(self, *, data_paths: "DataPaths") -> None:
        self._data_paths = data_paths

    def open(
        self, *, run_id: str, relative_path: str
    ) -> AsyncIterator[bytes]:
        return self._iter(run_id, relative_path)

    async def _iter(
        self, run_id: str, relative_path: str
    ) -> AsyncIterator[bytes]:
        run_dir = self._data_paths.blob_dir(
            "app_builder", subkey=run_id
        )
        target = run_dir.joinpath(*relative_path.split("/"))
        # Defence-in-depth: confirm we stay inside the run directory.
        try:
            resolved_target = target.resolve(strict=False)
            resolved_run_dir = run_dir.resolve(strict=False)
            resolved_target.relative_to(resolved_run_dir)
        except (ValueError, OSError) as exc:
            raise NotFoundError(
                "app_builder.artifact_not_found",
                "artifact",
                f"{run_id}:{relative_path}",
            ) from exc
        if not target.is_file():
            raise NotFoundError(
                "app_builder.artifact_not_found",
                "artifact",
                f"{run_id}:{relative_path}",
            )
        try:
            with target.open("rb") as fh:
                while True:
                    chunk = fh.read(_CHUNK_SIZE)
                    if not chunk:
                        return
                    yield chunk
        except OSError as exc:
            raise PersistenceError(
                "app_builder.artifact.read_failed",
                f"failed to read artifact bytes: {exc}",
                operation="artifact.read",
                cause=exc,
            ) from exc
