# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.platform.uploads — Cross-BC platform sub-module for file uploads.

This shared kernel module provides a reusable file upload abstraction
that multiple bounded contexts can use (chat for image upload,
app_builder for model/code uploads, etc.).

Public API:
    - :class:`UploadCategory` — enum of upload categories
    - :class:`UploadRecord` — immutable record for a stored upload
    - :class:`UploadStorePort` — Protocol for the upload store
    - :class:`FilesystemUploadStore` — filesystem-backed adapter
    - :class:`UploadFileUseCase` — generic per-category upload orchestration
    - :class:`UploadCodeFileUseCase` — legacy code-file upload orchestration
    - :class:`UploadPolicyError` / subclasses — policy rejections
"""

from __future__ import annotations

from qai.platform.uploads.errors import (
    UnsupportedExtensionError,
    UploadPolicyError,
    UploadTooLargeError,
)
from qai.platform.uploads.extract_dataset import (
    DatasetExtractionError,
    extract_archive,
    is_archive_filename,
)
from qai.platform.uploads.filesystem_store import FilesystemUploadStore
from qai.platform.uploads.ports import UploadStorePort
from qai.platform.uploads.types import UploadCategory, UploadRecord
from qai.platform.uploads.use_cases import (
    UploadCodeFileUseCase,
    UploadDatasetUseCase,
    UploadFileUseCase,
)

__all__ = [
    "DatasetExtractionError",
    "FilesystemUploadStore",
    "UnsupportedExtensionError",
    "UploadCategory",
    "UploadCodeFileUseCase",
    "UploadDatasetUseCase",
    "UploadFileUseCase",
    "UploadPolicyError",
    "UploadRecord",
    "UploadStorePort",
    "UploadTooLargeError",
    "extract_archive",
    "is_archive_filename",
]
