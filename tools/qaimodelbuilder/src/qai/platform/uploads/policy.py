# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Upload policy: per-category size ceilings, code-file extension
allowlist, and the legacy single-file code-upload cap.

These were previously hard-coded in the HTTP route layer
(``interfaces/http/routes/uploads.py``). They are pure policy data +
validation logic, so they belong to the application layer of the
platform uploads shared-kernel sub-module rather than the route.

Validation raises the dedicated errors in
:mod:`qai.platform.uploads.errors` so the HTTP layer can map them to the
exact status codes / detail strings the V1 contract requires without
owning the policy itself.
"""

from __future__ import annotations

from pathlib import Path

from qai.platform.uploads.errors import (
    UnsupportedExtensionError,
    UploadTooLargeError,
)
from qai.platform.uploads.types import UploadCategory

# ---------------------------------------------------------------------------
# Max file size per category (bytes) — verbatim from the legacy route.
# ---------------------------------------------------------------------------
MAX_SIZES: dict[UploadCategory, int] = {
    UploadCategory.IMAGE: 10 * 1024 * 1024,       # 10 MB
    UploadCategory.MODEL: 2 * 1024 * 1024 * 1024,  # 2 GB
    UploadCategory.CODE: 100 * 1024 * 1024,        # 100 MB
    UploadCategory.DATASET: 500 * 1024 * 1024,     # 500 MB
    UploadCategory.AUDIO: 50 * 1024 * 1024,        # 50 MB
    UploadCategory.VOICE: 50 * 1024 * 1024,        # 50 MB
}

# ---------------------------------------------------------------------------
# Allowed code-file extensions (legacy parity — backend/main.py L5887-5901).
#
# The V1 ``POST /api/upload/code`` route restricted uploads to common
# code / text extensions; files with NO suffix (Makefile, Dockerfile, …)
# are also permitted. The legacy single-file 10 MB cap is preserved here
# (distinct from the generic 100 MB ``UploadCategory.CODE`` ceiling used
# by the catch-all ``/api/uploads/code`` route).
# ---------------------------------------------------------------------------
ALLOWED_CODE_EXTS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    ".java", ".kt", ".swift",
    ".go", ".rs",
    ".cs", ".vb",
    ".rb", ".php", ".lua",
    ".sh", ".bash", ".zsh", ".ps1",
    ".sql",
    ".html", ".css", ".scss", ".less",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".md", ".txt", ".rst",
    ".r", ".m", ".scala",
    ".dockerfile", ".makefile",
})

# Legacy single-file code upload size cap: 10 MB (backend/main.py L5904).
MAX_CODE_BYTES: int = 10 * 1024 * 1024


def validate_category_size(category: UploadCategory, size_bytes: int) -> None:
    """Enforce the per-category size ceiling.

    Raises :class:`UploadTooLargeError` (carrying the exact V1 detail
    string) when *size_bytes* exceeds the ceiling for *category*.
    """
    max_size = MAX_SIZES[category]
    if size_bytes > max_size:
        max_mb = max_size / (1024 * 1024)
        raise UploadTooLargeError(
            f"File exceeds maximum size of {max_mb:.0f} MB for category "
            f"'{category.value}'."
        )


def validate_code_extension(filename: str) -> None:
    """Enforce the legacy code-file extension allowlist.

    Files with NO suffix (Makefile, Dockerfile, …) are allowed, mirroring
    the legacy behaviour. Raises :class:`UnsupportedExtensionError`
    (carrying the exact Chinese V1 detail string) otherwise.
    """
    suffix = Path(filename).suffix.lower()
    if suffix and suffix not in ALLOWED_CODE_EXTS:
        raise UnsupportedExtensionError(
            f"不支持的文件格式 '{suffix}'，请上传常见代码或文本文件。"
        )


def validate_code_size(size_bytes: int) -> None:
    """Enforce the legacy single-file code-upload 10 MB cap.

    Raises :class:`UploadTooLargeError` (carrying the exact Chinese V1
    detail string) when *size_bytes* exceeds :data:`MAX_CODE_BYTES`.
    """
    if size_bytes > MAX_CODE_BYTES:
        raise UploadTooLargeError(
            f"文件过大（{size_bytes // 1024} KB），上限 10 MB"
        )
