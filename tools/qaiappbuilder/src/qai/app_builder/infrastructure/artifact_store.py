# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem-backed :class:`ArtifactStorePort` (PR-045).

Persists artifact bytes under ``DataPaths.blob_dir("app_builder")`` and
returns an :class:`Artifact` VO describing the stored file. Database
persistence of the artifact metadata happens later through
:class:`SqliteRunRepository.save` once the artifact is attached to its
owning :class:`Run`.

Layout (per ``qai-db-schema.md`` §6.4):

::

    <data>/blobs/app_builder/<run_id>/<relative_path>

The runner / use case is responsible for choosing ``relative_path``;
this adapter only validates that the resolved final path stays under
the run's blob directory (defence-in-depth on top of the domain VO's
path validation).

The adapter is technically platform-side, not persistence-side
(``infrastructure/`` rather than ``adapters/``) — same call as
:class:`FileSystemArtifactBlobReader`. Both share a small helper for
resolving the on-disk path.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from qai.platform.crypto.hashes import Hash256
from qai.platform.errors import PersistenceError

from qai.app_builder.domain.artifact import Artifact, ArtifactKind
from qai.app_builder.domain.errors import ArtifactWriteError
from qai.app_builder.domain.value_objects import RunId

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config import DataPaths

__all__ = ["FileSystemArtifactStore"]


_DEFAULT_CHUNK_SIZE = 64 * 1024  # 64 KiB


class FileSystemArtifactStore:
    """Filesystem implementation of :class:`ArtifactStorePort`.

    Each run owns a private directory under
    ``DataPaths.blob_dir("app_builder", subkey=<run_id>)``; relative
    paths are resolved into that directory. The blob directory is
    created lazily on the first write.
    """

    __slots__ = ("_data_paths",)

    def __init__(self, *, data_paths: "DataPaths") -> None:
        self._data_paths = data_paths

    async def write(
        self,
        *,
        run_id: RunId,
        relative_path: str,
        kind: ArtifactKind,
        data: bytes,
    ) -> Artifact:
        try:
            target = self._resolve(run_id=run_id, relative_path=relative_path)
            self._data_paths.ensure(target.parent)
            target.write_bytes(data)
        except (OSError, ValueError) as exc:
            raise ArtifactWriteError(
                message=(
                    f"failed to write artifact {relative_path!r} "
                    f"for run {run_id}: {exc}"
                ),
                details={
                    "run_id": str(run_id),
                    "relative_path": relative_path,
                },
                cause=exc,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.artifact.write_failed",
                f"failed to write artifact: {exc}",
                operation="artifact.write",
                cause=exc,
            ) from exc

        digest = hashlib.sha256(data).hexdigest()
        return Artifact(
            path=relative_path,
            size_bytes=len(data),
            kind=kind,
            checksum=Hash256(value=digest),
        )

    async def write_stream(
        self,
        *,
        run_id: RunId,
        relative_path: str,
        kind: ArtifactKind,
        data: AsyncIterator[bytes],
    ) -> Artifact:
        hasher = hashlib.sha256()
        total = 0
        try:
            target = self._resolve(run_id=run_id, relative_path=relative_path)
            self._data_paths.ensure(target.parent)
            with target.open("wb") as fh:
                async for chunk in data:
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise ValueError(
                            "stream chunk must be bytes, "
                            f"got {type(chunk).__name__}"
                        )
                    fh.write(chunk)
                    hasher.update(chunk)
                    total += len(chunk)
        except (OSError, ValueError) as exc:
            raise ArtifactWriteError(
                message=(
                    f"failed to stream artifact {relative_path!r} "
                    f"for run {run_id}: {exc}"
                ),
                details={
                    "run_id": str(run_id),
                    "relative_path": relative_path,
                },
                cause=exc,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "app_builder.artifact.write_stream_failed",
                f"failed to stream artifact: {exc}",
                operation="artifact.write_stream",
                cause=exc,
            ) from exc

        return Artifact(
            path=relative_path,
            size_bytes=total,
            kind=kind,
            checksum=Hash256(value=hasher.hexdigest()),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve(self, *, run_id: RunId, relative_path: str):
        # Domain VO already rejected absolute paths / `..` / drive
        # letters; we still normalise via DataPaths to keep the
        # canonical layout in one place.
        run_dir = self._data_paths.blob_dir(
            "app_builder", subkey=run_id.value
        )
        target = run_dir.joinpath(*relative_path.split("/"))
        # Defence-in-depth: resolve and confirm we stay inside run_dir.
        try:
            resolved_target = target.resolve(strict=False)
            resolved_run_dir = run_dir.resolve(strict=False)
            resolved_target.relative_to(resolved_run_dir)
        except (ValueError, OSError) as exc:
            raise ArtifactWriteError(
                message=(
                    f"refusing to write outside run directory: {relative_path!r}"
                ),
                details={
                    "run_id": str(run_id),
                    "relative_path": relative_path,
                },
                cause=exc,
            ) from exc
        return target
