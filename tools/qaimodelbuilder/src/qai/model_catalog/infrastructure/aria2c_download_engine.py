# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Thin re-export shim for the aria2c single-frame download engine.

The implementation was lifted to
:mod:`qai.platform.download.aria2c_engine` (the platform shared kernel)
so it can be shared across bounded contexts without violating the
``context-isolation`` import-linter contract. This module re-exports the
public names so every existing
``from qai.model_catalog.infrastructure.aria2c_download_engine import ...``
caller keeps working unchanged.
"""

from __future__ import annotations

from qai.platform.download.aria2c_engine import (
    Aria2cBinaryNotFoundError,
    Aria2cDownloadEngine,
    ProcessRunnerLike,
)

__all__ = [
    "Aria2cDownloadEngine",
    "ProcessRunnerLike",
    "Aria2cBinaryNotFoundError",
]
