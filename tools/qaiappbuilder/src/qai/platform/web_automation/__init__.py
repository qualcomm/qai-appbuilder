# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Edition-neutral browser-automation primitives (shared kernel).

Stateless building blocks shared by every browser-automation consumer in the
codebase — the web-search browser engine and the interactive browser tool:

* :func:`is_playwright_available` / :data:`async_playwright` — availability probe.
* :data:`CHROMIUM_LAUNCH_ARGS` / :data:`DEFAULT_IDLE_CLOSE_SECONDS` — launch config.
* :func:`apply_stealth` / :func:`stealth_init_script` — anti-fingerprint patch.
* :func:`build_context_options` — TLS/viewport-aware ``new_context`` kwargs.

Consumers layer their own stateful managers on top; nothing here holds browser
state, so importing this package never starts a browser.
"""

from __future__ import annotations

from .context_options import build_context_options
from .guard import async_playwright, is_playwright_available
from .launch import CHROMIUM_LAUNCH_ARGS, DEFAULT_IDLE_CLOSE_SECONDS
from .stealth import apply_stealth, stealth_init_script

__all__ = [
    "CHROMIUM_LAUNCH_ARGS",
    "DEFAULT_IDLE_CLOSE_SECONDS",
    "apply_stealth",
    "async_playwright",
    "build_context_options",
    "is_playwright_available",
    "stealth_init_script",
]
