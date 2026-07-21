# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem-backed :class:`BlobStorePort` (PR-044).

Resolution strategy
-------------------
Each :class:`StorageKey` ``(category, name)`` becomes a file at::

    <root>/<category>/<name>

Both ``category`` and ``name`` are validated by the VO at construction
time to forbid path-separators, ``..``, and casing oddities; the
adapter therefore concatenates with ``/`` (joinpath) without further
escaping. The root is supplied through :class:`DataPaths`
(``data_paths.models_dir()`` is the canonical choice) so the legacy
"hard-coded ``data/models/`` paths" pattern is gone.

The adapter also exposes :meth:`resolve_path` so the
:class:`Hash256ChecksumVerifier` can hash a blob without re-implementing
path resolution.

Concurrency
-----------
Every method is ``async`` to satisfy the Port contract, but most
operations are synchronous filesystem calls — these are fast enough on
local SSDs that wrapping in a thread pool would only add overhead. The
removal helper guards against races by treating ``FileNotFoundError``
as success (idempotent semantics).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.model_catalog.domain.value_objects import StorageKey

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config import DataPaths


__all__ = ["FileSystemBlobStore"]


class FileSystemBlobStore:
    """Filesystem implementation of :class:`BlobStorePort`."""

    __slots__ = ("_root",)

    def __init__(self, *, root: Path) -> None:
        if not isinstance(root, Path):
            raise TypeError(
                f"root must be a pathlib.Path, got {type(root).__name__}"
            )
        self._root = root.resolve()

    @classmethod
    def from_data_paths(cls, data_paths: "DataPaths") -> "FileSystemBlobStore":
        """Build the canonical production blob store under ``blobs/``.

        :class:`DataPaths` exposes ``blobs_dir`` as the partition for
        per-context binary content; the model_catalog blob layout
        slots in as ``blobs/<category>/<name>``. The directory is
        created lazily by :class:`DataPaths.ensure_top_levels`.
        """
        return cls(root=data_paths.blobs_dir)

    @property
    def root(self) -> Path:
        return self._root

    def resolve_path(self, key: StorageKey) -> Path:
        """Map a logical :class:`StorageKey` to a concrete filesystem path."""
        return self._root / key.category / key.name

    async def exists(self, key: StorageKey) -> bool:
        try:
            return self.resolve_path(key).is_file()
        except OSError as exc:
            raise PersistenceError(
                "model_catalog.blob.exists_failed",
                f"failed to stat blob {key.category}/{key.name}: {exc}",
                operation="blob.exists",
                cause=exc,
            ) from exc

    async def remove(self, key: StorageKey) -> None:
        path = self.resolve_path(key)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise PersistenceError(
                "model_catalog.blob.remove_failed",
                f"failed to remove blob {key.category}/{key.name}: {exc}",
                operation="blob.remove",
                cause=exc,
            ) from exc

    async def size_bytes(self, key: StorageKey) -> int:
        path = self.resolve_path(key)
        try:
            if not path.is_file():
                return 0
            return path.stat().st_size
        except OSError as exc:
            raise PersistenceError(
                "model_catalog.blob.size_failed",
                f"failed to stat blob {key.category}/{key.name}: {exc}",
                operation="blob.size_bytes",
                cause=exc,
            ) from exc

    async def list_keys(self, category: str) -> list[StorageKey]:
        category_dir = self._root / category
        try:
            if not category_dir.is_dir():
                return []
            return [
                StorageKey(category=category, name=p.name)
                for p in sorted(category_dir.iterdir())
                if p.is_file()
            ]
        except OSError as exc:
            raise PersistenceError(
                "model_catalog.blob.list_failed",
                f"failed to list blobs under {category!r}: {exc}",
                operation="blob.list_keys",
                cause=exc,
            ) from exc
