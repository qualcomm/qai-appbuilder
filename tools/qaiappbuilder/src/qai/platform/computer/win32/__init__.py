# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Win32 primitives for desktop control (capture + input + keymap)."""

from __future__ import annotations

from .keymap import KEY_ALIASES, ResolvedKey, resolve_key

__all__ = [
    "KEY_ALIASES",
    "ResolvedKey",
    "resolve_key",
]
