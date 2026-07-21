# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for the platform uploads shared-kernel sub-module.

These own the upload-policy orchestration that previously lived inline in
the HTTP route layer (``interfaces/http/routes/uploads.py``):

* :class:`UploadFileUseCase` — generic per-category upload: enforce the
  category size ceiling, then persist via the :class:`UploadStorePort`.
* :class:`UploadCodeFileUseCase` — legacy ``POST /api/upload/code``
  parity: enforce the code-file extension allowlist + the 10 MB cap,
  then persist under :attr:`UploadCategory.CODE`.

Policy decisions live in :mod:`qai.platform.uploads.policy`; this layer
sequences them and delegates persistence to the port. The HTTP route is
left as a thin translation layer (request → use case → DTO / error →
``HTTPException``).
"""

from __future__ import annotations

import qai.platform.uploads.policy as policy
from qai.platform.uploads.extract_dataset import (
    extract_archive,
    is_archive_filename,
)
from qai.platform.uploads.ports import UploadStorePort
from qai.platform.uploads.types import UploadCategory, UploadRecord


class UploadFileUseCase:
    """Generic per-category upload: validate size, then persist."""

    def __init__(self, *, store: UploadStorePort) -> None:
        self._store = store

    async def execute(
        self,
        *,
        category: UploadCategory,
        filename: str,
        content: bytes,
        conv_id: str | None = None,
    ) -> UploadRecord:
        policy.validate_category_size(category, len(content))
        return await self._store.save(
            category=category,
            filename=filename,
            content=content,
            conv_id=conv_id,
        )


class UploadDatasetUseCase:
    """Dataset upload with V1-parity archive auto-extraction.

    V1 (``backend/main.py`` L6160-6206): a dataset upload that is a
    ``.zip`` / ``.tar*`` archive is **extracted** into the dataset
    directory (with zip-slip / path-traversal rejection) so the user sees
    the individual files; a single (non-archive) file is stored as-is.

    This use case mirrors that: it enforces the dataset size ceiling on
    the uploaded payload, then —

    * **archive** → extract in memory (:func:`extract_archive`,
      stdlib zip/tar + traversal guard) and persist each member through
      the :class:`UploadStorePort` (reusing the store's index / list /
      delete machinery instead of a parallel on-disk layout). Returns one
      :class:`UploadRecord` per extracted file.
    * **plain file** → persist as-is. Returns a one-element list.

    Returning a list (rather than a single record) lets the route report
    how many files an archive yielded, matching V1's "extracted N files"
    response while keeping every file individually listable / deletable.
    """

    def __init__(self, *, store: UploadStorePort) -> None:
        self._store = store

    async def execute(
        self,
        *,
        filename: str,
        content: bytes,
        conv_id: str | None = None,
    ) -> list[UploadRecord]:
        policy.validate_category_size(UploadCategory.DATASET, len(content))

        if not is_archive_filename(filename):
            record = await self._store.save(
                category=UploadCategory.DATASET,
                filename=filename,
                content=content,
                conv_id=conv_id,
            )
            return [record]

        # Archive: extract (traversal-safe) then persist each member.
        members = extract_archive(content=content, filename=filename)
        records: list[UploadRecord] = []
        for member_name, member_bytes in members:
            record = await self._store.save(
                category=UploadCategory.DATASET,
                filename=member_name,
                content=member_bytes,
                conv_id=conv_id,
            )
            records.append(record)
        return records


class UploadCodeFileUseCase:
    """Legacy ``POST /api/upload/code`` parity.

    Enforces the extension allowlist (no-suffix files allowed) + the
    legacy 10 MB single-file cap, then persists under
    :attr:`UploadCategory.CODE`. When the original filename is empty the
    stored filename falls back to ``"code"`` (V1 parity).
    """

    def __init__(self, *, store: UploadStorePort) -> None:
        self._store = store

    async def execute(
        self,
        *,
        filename: str,
        content: bytes,
    ) -> UploadRecord:
        policy.validate_code_extension(filename)
        policy.validate_code_size(len(content))
        return await self._store.save(
            category=UploadCategory.CODE,
            filename=filename or "code",
            content=content,
        )
