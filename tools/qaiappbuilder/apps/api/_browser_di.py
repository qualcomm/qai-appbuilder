# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Composition-root builder for the interactive ``browser`` tool's manager.

Builds a process-lifetime :class:`BrowserSessionManager` when Playwright is
importable (the ``search`` / ``e2e`` install profile) and the optional
``[browser]`` config does not disable it. Returns ``None`` otherwise, which the
callers use to leave the ``browser`` tool unregistered (SERVICE_GATED) — a build
without Playwright simply never advertises the tool.

Cross-context discipline: lives in ``apps/api`` (the only layer allowed to
compose contexts). It reads the unified ``Settings.ssl_verify`` and constructs
the neutral ``qai.platform.web_automation`` manager; neither ``qai.chat`` nor
``qai.ai_coding`` learns about the manager package directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qai.platform.logging import get_logger

__all__ = ["build_browser_manager"]

_log = get_logger(__name__)


def build_browser_manager(*, container: Any) -> Any | None:
    """Build the browser-tool session manager, or ``None`` when unavailable.

    Gated on: (1) Playwright importable; (2) ``settings.browser.enabled`` not
    explicitly ``False``. TLS follows the unified ``settings.ssl_verify``.
    """
    try:
        from qai.platform.web_automation import is_playwright_available
    except Exception:  # noqa: BLE001 — package import must never break startup
        return None
    if not is_playwright_available():
        _log.info("browser_tool.unavailable", reason="playwright_not_installed")
        return None

    settings = getattr(container, "settings", None)
    browser_cfg = getattr(settings, "browser", None)
    if browser_cfg is not None and getattr(browser_cfg, "enabled", True) is False:
        _log.info("browser_tool.disabled", reason="config")
        return None

    ssl_verify = bool(getattr(settings, "ssl_verify", True))
    idle_close_seconds = int(
        getattr(browser_cfg, "idle_close_seconds", 300) if browser_cfg else 300
    )
    # Persistent user-data dir for the ``spawned`` kind lives under the app's
    # runtime data dir so it is cleaned up with the rest of the workspace.
    user_data_dir = str(Path("data") / "runtime" / "browser_profile")

    try:
        from qai.platform.web_automation.session import BrowserSessionManager

        manager = BrowserSessionManager(
            ssl_verify=ssl_verify,
            idle_close_seconds=idle_close_seconds,
            persistent_user_data_dir=user_data_dir,
        )
    except Exception:  # noqa: BLE001 — never break startup on manager build
        _log.warning("browser_tool.manager_build_failed", exc_info=True)
        return None

    _log.info("browser_tool.enabled", ssl_verify=ssl_verify, idle_close_seconds=idle_close_seconds)
    return manager
