# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain types for the platform uploads shared kernel sub-module.

This module is part of the shared kernel and may be imported by any BC
that needs file upload capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


class UploadCategory(Enum):
    """Classification of uploaded files."""

    IMAGE = "image"
    MODEL = "model"
    CODE = "code"
    DATASET = "dataset"
    AUDIO = "audio"
    VOICE = "voice"


@dataclass(frozen=True, slots=True)
class UploadRecord:
    """Immutable record describing a successfully stored upload."""

    id: str
    category: UploadCategory
    filename: str
    size_bytes: int
    path: Path
    created_at: datetime
    conv_id: str | None = None  # V1 parity: per-conversation upload isolation
