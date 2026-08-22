# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""SHA-256 backed :class:`ChecksumVerifierPort` (PR-044).

Re-uses :class:`qai.platform.crypto.hashes.Hash256` for the digest
computation and :class:`qai.model_catalog.adapters.file_system_blob_store.FileSystemBlobStore`
to resolve :class:`StorageKey` to a real filesystem path. The two
collaborators are injected so unit tests can swap a stub blob store
without touching the disk.

Streaming
---------
The verifier reads the blob in chunks (default 1 MiB) so multi-gigabyte
model weights can be hashed without loading the file into memory in
one shot.

Algorithm support
-----------------
The current adapter supports ``sha256`` only — the only algorithm
present in production model manifests today. The Port allows ``blake3``
in the schema but no callsite uses it; raising ``ValueError`` for an
unknown algorithm surfaces the gap loudly should a future manifest
declare one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from qai.platform.crypto.hashes import Hash256

from qai.model_catalog.domain.value_objects import (
    Checksum,
    ChecksumAlgorithm,
    StorageKey,
)

if TYPE_CHECKING:  # pragma: no cover
    pass


__all__ = [
    "Hash256ChecksumVerifier",
    "BlobPathResolverPort",
]


_DEFAULT_CHUNK_SIZE: int = 1024 * 1024  # 1 MiB


class BlobPathResolverPort(Protocol):
    """Minimal slice of :class:`BlobStorePort` needed for verification.

    Decoupling this from the full :class:`BlobStorePort` lets tests
    inject a no-op resolver without implementing the rest of the blob
    store contract. The actual production implementation is
    :class:`FileSystemBlobStore.resolve_path`.
    """

    def resolve_path(self, key: StorageKey) -> Path:
        """Return the absolute filesystem path that ``key`` maps to."""
        ...


class Hash256ChecksumVerifier:
    """SHA-256 streaming :class:`ChecksumVerifierPort`."""

    __slots__ = ("_resolver", "_chunk_size")

    def __init__(
        self,
        *,
        resolver: BlobPathResolverPort,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError(
                f"chunk_size must be > 0, got {chunk_size!r}"
            )
        self._resolver = resolver
        self._chunk_size = chunk_size

    async def compute(
        self, target: StorageKey, *, algorithm: str
    ) -> str:
        if algorithm != ChecksumAlgorithm.SHA256.value:
            raise ValueError(
                f"unsupported checksum algorithm {algorithm!r}; "
                f"expected {ChecksumAlgorithm.SHA256.value!r}"
            )
        path = self._resolver.resolve_path(target)
        if not path.exists():
            raise FileNotFoundError(f"blob not found at {path}")
        return self._sha256_file(path)

    async def verify(
        self, target: StorageKey, expected: Checksum
    ) -> bool:
        actual = await self.compute(
            target, algorithm=expected.algorithm.value
        )
        return expected.matches(actual)

    # ── Internals ──────────────────────────────────────────────────────

    def _sha256_file(self, path: Path) -> str:
        """Stream-hash ``path`` and return the canonical lower-case hex.

        Uses :class:`Hash256` to perform a final wrap so the returned
        string is guaranteed to satisfy the platform's hash VO contract.
        """
        digester = hashlib.sha256()
        with path.open("rb") as fp:
            while True:
                chunk = fp.read(self._chunk_size)
                if not chunk:
                    break
                digester.update(chunk)
        # Round-trip through Hash256.of for canonical-case enforcement.
        return Hash256.of(digester.hexdigest()).value
