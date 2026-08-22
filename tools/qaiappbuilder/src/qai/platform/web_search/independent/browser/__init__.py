# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Playwright + Chromium lifecycle and stealth for the browser engine.

Re-exports the public surface the browser engine depends on: the shared
:class:`BrowserLifecycle`, the :func:`is_available` probe the loader consults,
and :func:`apply_stealth` for the anti-fingerprint init script.
"""

from __future__ import annotations

from .lifecycle import DEFAULT_IDLE_CLOSE_SECONDS, BrowserLifecycle, is_available
from .stealth import apply_stealth

__all__ = [
    "DEFAULT_IDLE_CLOSE_SECONDS",
    "BrowserLifecycle",
    "apply_stealth",
    "is_available",
]
