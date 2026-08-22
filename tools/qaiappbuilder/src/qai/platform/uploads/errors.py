# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain/application errors for the platform uploads sub-module.

These let the application layer (policy + use cases) reject invalid
uploads without knowing anything about HTTP. The HTTP route maps them to
the appropriate status codes / detail strings, preserving the V1
contract (413 for oversize, 400 for unsupported extension).

Each error carries a ready-to-surface ``detail`` message so the route is
a pure translation layer (error → ``HTTPException``) and never re-derives
the policy wording.
"""

from __future__ import annotations


class UploadPolicyError(Exception):
    """Base class for upload-policy rejections.

    ``detail`` is the human-facing message intended for the API response
    body (already localised to match the V1 contract).
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail: str = detail


class UploadTooLargeError(UploadPolicyError):
    """Raised when an uploaded file exceeds its size ceiling (→ HTTP 413)."""


class UnsupportedExtensionError(UploadPolicyError):
    """Raised when a code file has a disallowed extension (→ HTTP 400)."""
