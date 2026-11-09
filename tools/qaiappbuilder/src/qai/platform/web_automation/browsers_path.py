# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Where the Playwright browsers (Chromium) live.

``Setup.bat`` / ``installer\\_setup_env.bat`` install Chromium into the app's OWN
folder, ``<app_root>/envs/ms-playwright``, by setting ``PLAYWRIGHT_BROWSERS_PATH``
for ``playwright install chromium``. It is removed together with the venvs when
the app is uninstalled, and never touches Playwright's shared per-user cache
(``%LOCALAPPDATA%\\ms-playwright``) that other programs may use.

Playwright finds its browsers through the same variable, so the backend sets it
once at startup (:func:`configure_browsers_path`); the Playwright driver started
later inherits it. An explicit ``PLAYWRIGHT_BROWSERS_PATH`` from the user wins,
and when the app folder has no browsers (an older install, a dev checkout that
has not re-run setup) nothing is set and Playwright keeps its default cache.

Stdlib only: no browser is started and Playwright is not imported here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "BROWSERS_PATH_ENV",
    "app_browsers_dir",
    "browsers_base_dir",
    "configure_browsers_path",
]

#: The variable Playwright reads to locate (and install) its browsers.
BROWSERS_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"


def app_browsers_dir(app_root: Path) -> Path:
    """The app-owned browsers folder: ``<app_root>/envs/ms-playwright``."""
    return Path(app_root) / "envs" / "ms-playwright"


def _has_browser(path: Path) -> bool:
    try:
        return path.is_dir() and any(
            c.is_dir() and c.name.startswith(("chromium-", "chromium_headless_shell-"))
            for c in path.iterdir()
        )
    except OSError:
        return False


def configure_browsers_path(app_root: Path | None) -> str | None:
    """Point Playwright at ``<app_root>/envs/ms-playwright`` when it holds a browser.

    Returns the value now in effect (``None`` = Playwright's default cache).
    Leaves a user-set ``PLAYWRIGHT_BROWSERS_PATH`` alone. Never raises.
    """
    current = os.environ.get(BROWSERS_PATH_ENV) or None
    if current or app_root is None:
        return current
    candidate = app_browsers_dir(app_root)
    if not _has_browser(candidate):
        return None
    os.environ[BROWSERS_PATH_ENV] = str(candidate)
    return str(candidate)


def browsers_base_dir() -> str:
    """The folder Playwright looks in right now (``PLAYWRIGHT_BROWSERS_PATH`` or its default cache)."""
    configured = os.environ.get(BROWSERS_PATH_ENV)
    if configured and configured != "0":
        return configured
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/ms-playwright")
    return os.path.expanduser("~/.cache/ms-playwright")
