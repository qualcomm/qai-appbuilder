# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Shared Chromium launch primitives.

Neutral, stateless configuration reused by both the web-search browser engine
(anonymous single-context lifecycle) and the interactive browser tool
(named multi-tab session manager). Keeping the flag set + idle default in ONE
place means a fingerprint / sandbox-hardening change lands everywhere at once.
"""

from __future__ import annotations

__all__ = ["CHROMIUM_LAUNCH_ARGS", "DEFAULT_IDLE_CLOSE_SECONDS"]

#: Chromium launch flags that reduce the automation fingerprint and keep the
#: sandbox happy in CI / restricted (containerised, low-/dev-shm) environments.
#: Cross-platform: valid on Windows and Linux (Ubuntu/Debian glibc) alike.
CHROMIUM_LAUNCH_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
)

#: Idle window (seconds) after which a warm, page-less browser is torn down.
DEFAULT_IDLE_CLOSE_SECONDS = 300
