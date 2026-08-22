# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Playwright availability probe shared by every browser-automation consumer.

Playwright is an optional install profile (the ``search`` / ``e2e`` extras).
Both the web-search browser engine and the interactive browser tool must be
able to answer "is a browser backend importable in this process?" WITHOUT
triggering an :class:`ImportError` at module-import time. This module owns the
single guarded import + boolean probe so callers never duplicate the
try/except dance.
"""

from __future__ import annotations

try:  # pragma: no cover - import guard exercised only where extras missing
    from playwright.async_api import async_playwright as _async_playwright

    _PLAYWRIGHT_IMPORT_OK = True
except ImportError:  # pragma: no cover - depends on install profile
    _async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_IMPORT_OK = False

__all__ = ["async_playwright", "is_playwright_available"]

#: The imported ``async_playwright`` factory, or ``None`` when Playwright is not
#: installed. Consumers MUST check :func:`is_playwright_available` (or handle a
#: ``None`` here) before calling it.
async_playwright = _async_playwright


def is_playwright_available() -> bool:
    """Report whether Playwright is importable in this process.

    Cheap and side-effect free — safe to call at registration / gating time to
    decide whether a browser-backed feature should be advertised at all.
    """
    return _PLAYWRIGHT_IMPORT_OK
