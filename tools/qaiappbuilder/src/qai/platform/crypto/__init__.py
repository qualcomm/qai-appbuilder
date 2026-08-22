# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public crypto helpers for the qai platform layer.

Currently exposes only :class:`Hash256` (64-char lower-case hex
SHA-256 digest). Future additions (BLAKE3, MAC primitives) belong in
sibling modules under this package.
"""

from __future__ import annotations

from qai.platform.crypto.hashes import Hash256

__all__ = ["Hash256"]
