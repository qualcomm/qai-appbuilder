# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""On-demand Playwright + Chromium lifecycle for the browser search engine.

Owns at most one live Playwright driver + browser + headless context at a time
and reference-counts outstanding pages so Chromium is never closed out from
under an in-flight navigation; a warm browser is torn down after an idle
window. The stateless launch/stealth/guard primitives are shared with the
interactive browser tool via :mod:`qai.platform.web_automation`; this class
keeps its own narrow single-context, anonymous-refcount policy for search.

Public surface (import path unchanged for callers): :class:`BrowserLifecycle`,
:func:`is_available`, :data:`DEFAULT_IDLE_CLOSE_SECONDS`.
"""

from __future__ import annotations
import os
import sys

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from qai.platform.web_automation import (
    CHROMIUM_LAUNCH_ARGS,
    DEFAULT_IDLE_CLOSE_SECONDS,
    async_playwright,
    build_context_options,
    is_playwright_available,
)

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

from ..errors import EngineError
from ..http_client import get_search_ssl_verify

__all__ = ["DEFAULT_IDLE_CLOSE_SECONDS", "BrowserLifecycle", "is_available"]

#: Engine id used only for lifecycle-originated :class:`EngineError` messages;
#: the concrete engine reuses its own id when it raises.
_LIFECYCLE_ENGINE_ID = "browser_lifecycle"


def is_available() -> bool:
    """Report whether Playwright is importable in this process.

    Delegates to the shared availability probe. Kept as a module-level function
    on this import path for the engine loader that consults it.
    """
    return is_playwright_available()



def _find_chromium_executable() -> str | None:
    """Probe the ms-playwright directory for a usable Chromium executable.

    Playwright hard-codes the revision it downloads during ``install``, but the
    user may have only the full ``chromium-XXXX`` or a newer
    ``chromium_headless_shell-XXXX`` installed.  We scan for known layouts and
    return the first existing path, or ``None`` if nothing is found.
    """
    if sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches/ms-playwright")
    else:
        base = os.path.expanduser("~/.cache/ms-playwright")
    if not os.path.isdir(base):
        return None
    # Candidates ordered by preference: headless_shell (lighter), then full
    # chromium.  Higher revision numbers preferred (sorted descending).
    candidates: list[str] = []
    try:
        entries = sorted(os.listdir(base), reverse=True)
    except OSError:
        return None
    for entry in entries:
        if not entry.startswith(("chromium_headless_shell-", "chromium-")):
            continue
        dir_path = os.path.join(base, entry)
        if not os.path.isdir(dir_path):
            continue
        if sys.platform == "win32":
            for exe_name in ("headless_shell.exe", "chrome.exe"):
                exe = os.path.join(dir_path, "chrome-win", exe_name)
                if os.path.isfile(exe):
                    candidates.append(exe)
        elif sys.platform == "darwin":
            for app_name in ("Chromium.app/Contents/MacOS/Chromium", "headless_shell"):
                exe = os.path.join(dir_path, "chrome-mac", app_name)
                if os.path.isfile(exe):
                    candidates.append(exe)
        else:  # Linux
            for exe_name in ("headless_shell", "chrome"):
                exe = os.path.join(dir_path, "chrome-linux", exe_name)
                if os.path.isfile(exe):
                    candidates.append(exe)
    return candidates[0] if candidates else None


def _configured_proxy_url() -> str | None:
    """Return the user-configured proxy URL for the browser, or ``None``.

    Reads the SAME live seam every other outbound client uses — the web_fetch
    handler's ``get_global_proxy()``, which ``apps/api`` keeps up to date from
    ``settings.tools.global_proxy`` at boot and from every
    ``PUT /api/security/runtime-config`` afterwards. The browser therefore
    cannot silently egress by a different route than the HTTP engines.

    This previously read ``qai.chat.infrastructure.proxy_helper``, which nothing
    in the app ever initialised — it always answered "no proxy", so the browser
    connected DIRECTLY even with a proxy configured. Imported lazily to keep
    ``qai.platform`` free of an import-time dependency on ``qai.ai_coding``, and
    every failure degrades to "no proxy" rather than breaking the launch.

    Only the plain server URL is taken. Playwright accepts ``username`` /
    ``password`` separately, but embedding credentials in the server string
    would leak them into Chromium's own logs.
    """
    try:
        from qai.ai_coding.infrastructure.tools.handlers.web import (
            get_global_proxy,
        )

        proxy = get_global_proxy()
    except Exception:  # noqa: BLE001 — proxy is optional; never block the launch
        return None
    return proxy if isinstance(proxy, str) and proxy else None


class BrowserLifecycle:
    """Lazily-started, idle-closed headless Chromium shared by browser engines.

    One instance is expected per provider. It owns at most one live Playwright
    driver + browser + context at a time and reference-counts outstanding pages
    to avoid closing Chromium out from under an in-flight navigation.
    """

    __slots__ = (
        "_active_pages",
        "_browser",
        "_context",
        "_idle_close_seconds",
        "_idle_task",
        "_lock",
        "_playwright",
    )

    def __init__(self, *, idle_close_seconds: int = DEFAULT_IDLE_CLOSE_SECONDS) -> None:
        self._idle_close_seconds = idle_close_seconds
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._active_pages = 0

    async def _ensure_browser(self) -> BrowserContext:
        """Start Playwright + Chromium if not already warm; return the context."""
        if not is_playwright_available() or async_playwright is None:
            raise EngineError(_LIFECYCLE_ENGINE_ID, "playwright is not installed")
        if self._context is not None:
            return self._context
        try:
            self._playwright = await async_playwright().start()
            launch_kwargs: dict[str, object] = {
                "headless": True,
                "args": list(CHROMIUM_LAUNCH_ARGS),
            }
            try:
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            except Exception:
                # Playwright may look for chromium_headless_shell-XXXX which
                # isn't installed, while the full chromium-XXXX *is* present.
                # Probe the ms-playwright directory for a usable executable.
                exe = _find_chromium_executable()
                if exe is None:
                    raise  # re-raise the original if no fallback found
                launch_kwargs["executable_path"] = exe
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            # The browser MUST egress the same way the HTTP engines do: it is a
            # separate process and inherits nothing from the httpx clients, so
            # without this it silently uses the direct route while every other
            # engine goes through the proxy (which is how Google's plain-HTTP
            # path succeeded while this engine got the unusual-traffic wall).
            self._context = await self._browser.new_context(
                **build_context_options(
                    ssl_verify=get_search_ssl_verify(),
                    proxy_url=_configured_proxy_url(),
                ),
            )
        except Exception as exc:
            await self._teardown()
            raise EngineError(
                _LIFECYCLE_ENGINE_ID,
                f"failed to launch chromium: {exc}",
            ) from exc
        return self._context

    async def acquire_page(self) -> Page:
        """Start (if needed) and hand out a fresh page from the shared context.

        Cancels any pending idle-close timer while a page is outstanding. The
        caller MUST pass the page back to :meth:`release_page`.
        """
        async with self._lock:
            context = await self._ensure_browser()
            self._cancel_idle_timer()
            page = await context.new_page()
            self._active_pages += 1
            return page

    async def release_page(self, page: Page) -> None:
        """Close ``page`` and arm the idle-close timer once none remain."""
        with contextlib.suppress(Exception):
            await page.close()
        async with self._lock:
            if self._active_pages > 0:
                self._active_pages -= 1
            if self._active_pages == 0:
                self._arm_idle_timer()

    @contextlib.asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        """Async context manager yielding a page and releasing it on exit."""
        acquired = await self.acquire_page()
        try:
            yield acquired
        finally:
            await self.release_page(acquired)

    def _cancel_idle_timer(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

    def _arm_idle_timer(self) -> None:
        self._cancel_idle_timer()
        self._idle_task = asyncio.ensure_future(self._idle_close())

    async def _idle_close(self) -> None:
        try:
            await asyncio.sleep(self._idle_close_seconds)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self._active_pages == 0:
                await self._teardown()

    async def _teardown(self) -> None:
        """Best-effort close of context, browser and driver, resetting state."""
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._context = None
        self._browser = None
        self._playwright = None

    async def aclose(self) -> None:
        """Force-close the browser regardless of idle state (shutdown hook)."""
        self._cancel_idle_timer()
        async with self._lock:
            self._active_pages = 0
            await self._teardown()
