# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Port (Protocol) for the upload store abstraction.

Consumers depend on :class:`UploadStorePort` rather than a concrete
adapter so the backing storage can be swapped (filesystem, cloud, etc.)
without touching business logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from qai.platform.uploads.types import UploadCategory, UploadRecord


class UploadStorePort(Protocol):
    """Abstract upload persistence contract."""

    async def save(
        self,
        *,
        category: UploadCategory,
        filename: str,
        content: bytes,
        conv_id: str | None = None,
    ) -> UploadRecord:
        """Persist an uploaded file and return its record.

        When *conv_id* is provided the file is stored under a per-conversation
        sub-directory (V1 parity: ``data/uploads/<category>/<conv_id>/``).
        """
        ...

    async def list_recent(
        self,
        *,
        limit: int = 50,
        conv_id: str | None = None,
    ) -> list[UploadRecord]:
        """Return the most recent uploads (newest first).

        When *conv_id* is provided only records belonging to that conversation
        are returned (V1 parity: ``GET /api/upload/model/list?conv_id=xxx``).
        Pass ``None`` to return all records regardless of conversation.
        """
        ...

    async def delete(self, upload_id: str) -> bool:
        """Delete an upload by ID. Returns True if found and deleted."""
        ...

    async def get_path(self, upload_id: str) -> Path | None:
        """Resolve the on-disk path for an upload. None if not found."""
        ...
