# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem-backed upload store adapter.

Stores files under ``<data_dir>/uploads/<category>/<ulid>_<filename>``.
Metadata is held in an in-memory index that is rebuilt from disk on startup,
so uploads survive service restarts (V1 parity: V1 scanned the filesystem
directly on every list request; V2 rebuilds once at init for efficiency).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from qai.platform.ids import new_ulid
from qai.platform.uploads.types import UploadCategory, UploadRecord


# Max sane filename length after sanitisation.
_MAX_FILENAME_LEN: int = 200

# Characters allowed in the sanitised filename portion.
_SAFE_CHARS = re.compile(r"[^a-zA-Z0-9._\-]")

# ULID is exactly 26 uppercase alphanumeric characters.
_ULID_RE = re.compile(r"^([0-9A-Z]{26})_(.+)$")


def _sanitize_filename(name: str) -> str:
    """Return a filesystem-safe version of *name*."""
    # Strip path separators and collapse unsafe chars to underscore.
    name = name.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    sanitized = _SAFE_CHARS.sub("_", name)
    if not sanitized or sanitized.startswith("."):
        sanitized = "upload" + sanitized
    return sanitized[:_MAX_FILENAME_LEN]


def _mtime_utc(path: Path) -> datetime:
    """Return the file's mtime as a UTC-aware datetime."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


class FilesystemUploadStore:
    """Adapter: stores uploads on the local filesystem.

    Parameters:
        base_dir: root directory for uploads (typically ``data/uploads``).
    """

    def __init__(self, *, base_dir: Path) -> None:
        self._base_dir = base_dir
        # In-memory index: upload_id → UploadRecord
        self._index: dict[str, UploadRecord] = {}
        # Rebuild index from disk so uploads survive service restarts.
        self._rebuild_index()

    # ------------------------------------------------------------------
    # Index rebuild (startup)
    # ------------------------------------------------------------------

    def _rebuild_index(self) -> None:
        """Scan *base_dir* and populate the in-memory index from existing files.

        Supports two directory layouts:
        * ``<category>/<ULID>_<filename>``          — legacy / no conv_id
        * ``<category>/<conv_id>/<ULID>_<filename>`` — V1 parity conv_id isolation

        Unknown sub-directories and files that don't match the pattern are
        silently skipped.
        """
        if not self._base_dir.exists():
            return
        category_by_value = {c.value: c for c in UploadCategory}
        for cat_dir in self._base_dir.iterdir():
            if not cat_dir.is_dir():
                continue
            category = category_by_value.get(cat_dir.name)
            if category is None:
                continue
            for entry in cat_dir.iterdir():
                if entry.is_file():
                    # Flat layout: <category>/<ULID>_<filename>
                    self._index_file(entry, category, conv_id=None)
                elif entry.is_dir():
                    # Conv-id sub-directory: <category>/<conv_id>/<ULID>_<filename>
                    conv_id = entry.name
                    for file_path in entry.iterdir():
                        if file_path.is_file():
                            self._index_file(file_path, category, conv_id=conv_id)

    def _index_file(
        self,
        file_path: Path,
        category: "UploadCategory",
        conv_id: str | None,
    ) -> None:
        """Register a single file into the in-memory index (if it matches the naming convention)."""
        m = _ULID_RE.match(file_path.name)
        if not m:
            return
        upload_id, original_name = m.group(1), m.group(2)
        if upload_id in self._index:
            return  # already indexed (shouldn't happen, but be safe)
        record = UploadRecord(
            id=upload_id,
            category=category,
            filename=original_name,
            size_bytes=file_path.stat().st_size,
            path=file_path,
            created_at=_mtime_utc(file_path),
            conv_id=conv_id,
        )
        self._index[upload_id] = record

    # ------------------------------------------------------------------
    # UploadStorePort implementation
    # ------------------------------------------------------------------

    async def save(
        self,
        *,
        category: UploadCategory,
        filename: str,
        content: bytes,
        conv_id: str | None = None,
    ) -> UploadRecord:
        """Persist *content* to disk and register in the in-memory index.

        When *conv_id* is provided the file is stored under
        ``<base_dir>/<category>/<conv_id>/`` (V1 parity).
        Otherwise it falls back to the flat ``<base_dir>/<category>/`` layout.
        """
        upload_id = new_ulid()
        safe_name = _sanitize_filename(filename)
        if conv_id is not None:
            target_dir = self._base_dir / category.value / conv_id
        else:
            target_dir = self._base_dir / category.value
        target_dir.mkdir(parents=True, exist_ok=True)

        stored_name = f"{upload_id}_{safe_name}"
        target_path = target_dir / stored_name
        target_path.write_bytes(content)

        record = UploadRecord(
            id=upload_id,
            category=category,
            filename=filename,
            size_bytes=len(content),
            path=target_path,
            created_at=datetime.now(timezone.utc),
            conv_id=conv_id,
        )
        self._index[upload_id] = record
        return record

    async def list_recent(
        self,
        *,
        limit: int = 50,
        conv_id: str | None = None,
    ) -> list[UploadRecord]:
        """Return up to *limit* most recent uploads (newest first).

        When *conv_id* is provided only records belonging to that conversation
        are returned (V1 parity: per-session file list).
        """
        records = sorted(
            self._index.values(),
            key=lambda r: r.created_at,
            reverse=True,
        )
        if conv_id is not None:
            records = [r for r in records if r.conv_id == conv_id]
        return records[:limit]

    async def delete(self, upload_id: str) -> bool:
        """Remove an upload from disk and from the in-memory index."""
        record = self._index.pop(upload_id, None)
        if record is None:
            return False
        try:
            record.path.unlink(missing_ok=True)
        except OSError:
            pass
        return True

    async def get_path(self, upload_id: str) -> Path | None:
        """Resolve the on-disk path for *upload_id*."""
        record = self._index.get(upload_id)
        return record.path if record is not None else None
