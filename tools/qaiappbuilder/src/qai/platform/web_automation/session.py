# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Session-scoped multi-tab browser manager for the interactive browser tool.

Owns named tabs across two browser kinds — ``headless`` (a managed Chromium the
tool launches and fully controls) and ``connected`` (attach over CDP to a
browser started elsewhere, e.g. by the ``background_process`` tool) — sharing
the stateless launch/stealth/context primitives with the web-search engine via
the :mod:`qai.platform.web_automation` base while keeping its own named-tab
policy. A ``connected`` browser is never terminated by this manager (the layer
that started it owns its lifecycle); ``headless`` is fully managed.

Lifecycle: lazy browser start on first ``open``; one :class:`TabHandle` per
name; an idle-close timer arms when the last tab closes; ``aclose`` force-tears
down on session end. A single ``asyncio.Lock`` guards launch + tab-map mutation.
TLS follows the unified ``Settings.ssl_verify`` value supplied at construction.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Literal

from qai.platform.logging import get_logger

from . import (
    CHROMIUM_LAUNCH_ARGS,
    DEFAULT_IDLE_CLOSE_SECONDS,
    apply_stealth,
    async_playwright,
    build_context_options,
    is_playwright_available,
)
from .tab import BrowserToolError, TabHandle

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Dialog, Page, Playwright

__all__ = ["BrowserSessionManager", "BrowserKind"]

_log = get_logger(__name__)

BrowserKind = Literal["headless", "connected"]
DialogPolicy = Literal["accept", "dismiss", "ignore"]

_DEFAULT_TAB = "main"

#: Ceiling (seconds) for each teardown await (page/context/browser close,
#: driver stop). Playwright's ``browser.close()`` on Windows headless Chromium
#: can hang or take minutes; without this bound a ``close`` tool call blocks the
#: whole chat turn (observed ~2m43s stall). On timeout we abandon the graceful
#: close (best-effort) — the driver subprocess is reaped when Playwright stops
#: or the process exits.
_CLOSE_TIMEOUT_S = 3.0


class _TabRecord:
    __slots__ = ("handle",)

    def __init__(self, handle: TabHandle) -> None:
        self.handle = handle


class BrowserSessionManager:
    """Multi-tab, multi-kind browser owner for one agent/chat session."""

    __slots__ = (
        "_browser",
        "_context",
        "_dialog_policy",
        "_dialog_tasks",
        "_idle_close_seconds",
        "_idle_task",
        "_kind",
        "_lock",
        "_owns_context",
        "_persistent_user_data_dir",
        "_playwright",
        "_ssl_verify",
        "_tabs",
    )

    def __init__(
        self,
        *,
        ssl_verify: bool = True,
        idle_close_seconds: int = DEFAULT_IDLE_CLOSE_SECONDS,
        persistent_user_data_dir: str | None = None,
    ) -> None:
        self._ssl_verify = ssl_verify
        self._idle_close_seconds = idle_close_seconds
        self._persistent_user_data_dir = persistent_user_data_dir
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._kind: BrowserKind | None = None
        self._dialog_policy: DialogPolicy = "dismiss"
        # Strong refs to fire-and-forget dialog-handler tasks so the event loop
        # cannot GC them mid-flight (asyncio holds only weak task refs).
        self._dialog_tasks: set[asyncio.Task[None]] = set()
        # True only when THIS manager created the context (headless, or a fresh
        # connected context); False when a connected kind reused the user's
        # pre-existing context — which we must NOT close on teardown.
        self._owns_context: bool = True
        self._tabs: dict[str, _TabRecord] = {}
        self._idle_task: asyncio.Task[None] | None = None

    # -- public API ----------------------------------------------------------

    async def open(
        self,
        *,
        name: str | None = None,
        url: str | None = None,
        kind: str | None = None,
        app: dict[str, Any] | None = None,
        viewport: dict[str, int] | None = None,
        wait_until: str | None = None,
        dialogs: str | None = None,
    ) -> TabHandle:
        """Acquire/create a named tab, optionally navigating to ``url``."""
        if not is_playwright_available() or async_playwright is None:
            raise BrowserToolError("browser support is not installed (playwright missing)")
        tab_name = name or _DEFAULT_TAB
        want_kind: BrowserKind = _resolve_kind(kind, app)
        async with self._lock:
            # A kind switch is only a conflict when a tab is actually in use: we
            # must not silently tear down a browser the caller is driving. But a
            # warm, tab-less browser kept alive purely for idle-reuse is fair
            # game — reuse only ever helps the SAME kind, so for a different kind
            # we dispose the idle one now and bring the requested kind up fresh.
            # Done BEFORE touching the idle timer so a genuine reject never
            # disturbs an in-use browser's idle countdown.
            if self._kind is not None and self._kind != want_kind:
                if self._tabs:
                    raise BrowserToolError(
                        f"a {self._kind} browser is already open; close all tabs before "
                        f"opening a {want_kind} browser"
                    )
                self._cancel_idle_timer()
                await self._dispose(kill=True)
            self._cancel_idle_timer()
            # Dialog policy is fixed per tab at open time (captured when the
            # handler is installed on the page). Assigned under the lock so
            # concurrent open() calls do not race on the shared field.
            if dialogs in ("accept", "dismiss", "ignore"):
                self._dialog_policy = dialogs  # type: ignore[assignment]
            existing = self._tabs.get(tab_name)
            if existing is not None:
                handle = existing.handle
                if url:
                    await handle.goto(url, wait_until=wait_until)
                return handle
            # Whether this open is bringing up the browser for the first time
            # (no live context yet). If so and any step below fails, we must
            # tear the freshly-created browser down rather than leak it.
            first_bringup = self._context is None
            try:
                context = await self._ensure_context(want_kind, app, viewport)
                page = await context.new_page()
                self._install_dialog_handler(page)
                await apply_stealth(page)
                handle = TabHandle(name=tab_name, page=page, context=context)
                self._tabs[tab_name] = _TabRecord(handle)
                if url:
                    await handle.goto(url, wait_until=wait_until)
                _log.info(
                    "browser_manager.tab_open",
                    extra={"tab": tab_name, "kind": self._kind, "total_tabs": len(self._tabs), "url": handle.url()},
                )
                return handle
            except BrowserToolError:
                await self._cleanup_failed_open(tab_name, first_bringup)
                raise
            except Exception as exc:  # noqa: BLE001 - normalize + clean up
                await self._cleanup_failed_open(tab_name, first_bringup)
                raise BrowserToolError(f"open failed: {exc}") from exc

    def get(self, name: str | None = None) -> TabHandle:
        """Return an open tab by name or raise if it does not exist."""
        tab_name = name or _DEFAULT_TAB
        record = self._tabs.get(tab_name)
        if record is None:
            raise BrowserToolError(f"no open tab named {tab_name!r}")
        return record.handle

    @property
    def kind(self) -> BrowserKind | None:
        """The active browser kind (headless/connected), or None if idle."""
        return self._kind

    async def close(
        self, *, name: str | None = None, all: bool = False, kill: bool = False
    ) -> int:
        """Close one tab / all tabs.

        Closing a page is fast; tearing the whole browser + Playwright driver
        down is NOT (on Windows the driver-pipe handshake can hang — bounded by
        ``_CLOSE_TIMEOUT_S``). So a normal ``close`` only closes the page(s) and,
        when none remain, arms the idle timer to dispose the warm browser later
        (or reuse it on the next ``open``). Only ``kill=True`` (or ``aclose`` at
        session end) disposes the browser synchronously right now — for a
        connected browser that just disconnects (never terminates it).
        """
        async with self._lock:
            if all:
                count = len(self._tabs)
                for record in list(self._tabs.values()):
                    await _safe_close_page(record.handle.page)
                self._tabs.clear()
            else:
                tab_name = name or _DEFAULT_TAB
                record = self._tabs.pop(tab_name, None)
                if record is None:
                    return 0
                await _safe_close_page(record.handle.page)
                count = 1
            if not self._tabs:
                if kill:
                    # Explicit teardown now (headless: closes it; connected: disconnect).
                    await self._dispose(kill=True)
                else:
                    # Keep the browser warm; idle timer reclaims it later. This
                    # keeps ``close`` fast and lets the next ``open`` reuse it.
                    self._arm_idle_timer()
            elif count:
                self._arm_idle_timer()
            return count

    async def aclose(self) -> None:
        """Force-close everything (session-teardown hook)."""
        self._cancel_idle_timer()
        async with self._lock:
            for record in list(self._tabs.values()):
                await _safe_close_page(record.handle.page)
            self._tabs.clear()
            await self._dispose(kill=True)

    async def _cleanup_failed_open(self, tab_name: str, first_bringup: bool) -> None:
        """Roll back a failed ``open`` (called while holding ``self._lock``).

        Drops any partially-recorded tab (closing its page), and — when this
        call was the browser's first bring-up — disposes the freshly-created
        browser so a mid-``open`` failure never leaks a tab-less live browser.
        When a browser was already serving other tabs, it is left intact and
        the idle timer is re-armed if no tabs remain.
        """
        record = self._tabs.pop(tab_name, None)
        if record is not None:
            await _safe_close_page(record.handle.page)
        if not self._tabs:
            if first_bringup:
                # We brought the browser up in this failed call — tear it down.
                # (If the failure was inside _ensure_context it already did so;
                # _dispose is idempotent, so this is a safe no-op then.)
                await self._dispose(kill=True)
            elif self._context is not None:
                # A pre-existing browser lost its last tab to this failure —
                # let it idle-close rather than leak.
                self._arm_idle_timer()

    # -- internals -----------------------------------------------------------

    async def _ensure_context(
        self,
        kind: BrowserKind,
        app: dict[str, Any] | None,
        viewport: dict[str, int] | None,
    ) -> BrowserContext:
        if self._context is not None:
            return self._context
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        chromium = self._playwright.chromium
        options = build_context_options(ssl_verify=self._ssl_verify, viewport=viewport)
        try:
            if kind == "connected":
                cdp_url = (app or {}).get("cdp_url")
                if not cdp_url:
                    raise BrowserToolError("connected kind requires app.cdp_url")
                # Chrome's --remote-debugging-port listens on IPv4 127.0.0.1
                # only; "localhost" on Windows often resolves to IPv6 ::1 first,
                # yielding ECONNREFUSED ::1:PORT. Normalize so either form works.
                cdp_url = cdp_url.replace("://localhost", "://127.0.0.1")
                self._browser = await chromium.connect_over_cdp(cdp_url)
                if self._browser.contexts:
                    # Reuse the user's PRE-EXISTING context — we do NOT own it
                    # and MUST NOT close it on teardown (that would destroy the
                    # user's tabs/session). Only the pages we add are ours.
                    self._context = self._browser.contexts[0]
                    self._owns_context = False
                else:
                    self._context = await self._browser.new_context(**options)
                    self._owns_context = True
            else:  # headless
                self._browser = await chromium.launch(
                    headless=True, args=list(CHROMIUM_LAUNCH_ARGS)
                )
                self._context = await self._browser.new_context(**options)
                self._owns_context = True
        except BrowserToolError:
            await self._dispose(kill=True)
            raise
        except Exception as exc:  # noqa: BLE001
            await self._dispose(kill=True)
            raise BrowserToolError(f"failed to open {kind} browser: {exc}") from exc
        self._kind = kind
        _log.info(
            "browser_manager.browser_up",
            extra={"kind": kind, "owns_context": self._owns_context, "ssl_verify": self._ssl_verify},
        )
        return self._context

    def _install_dialog_handler(self, page: Page) -> None:
        policy = self._dialog_policy
        if policy == "ignore":
            return

        async def _on_dialog(dialog: Dialog) -> None:
            try:
                if policy == "accept":
                    await dialog.accept()
                else:  # "dismiss"
                    await dialog.dismiss()
            except Exception:  # noqa: BLE001, S110 - best effort
                pass

        def _spawn(dialog: Dialog) -> None:
            # Retain a strong ref so the loop cannot GC the task mid-flight
            # (asyncio holds only weak refs to tasks); discard on completion.
            task = asyncio.ensure_future(_on_dialog(dialog))
            self._dialog_tasks.add(task)
            task.add_done_callback(self._dialog_tasks.discard)

        page.on("dialog", _spawn)

    async def _dispose(self, *, kill: bool) -> None:
        _log.info("browser_manager.dispose", extra={"kind": self._kind, "kill": kill})
        # Drain any in-flight dialog-handler tasks first.
        for task in list(self._dialog_tasks):
            if not task.done():
                task.cancel()
        self._dialog_tasks.clear()
        # Only close the context WE created. A reused connected context belongs
        # to the user — closing it would destroy their tabs/session. Their pages
        # (the ones the tool opened) are already closed by close()/aclose().
        if self._context is not None and self._owns_context:
            await _bounded_close(self._context.close())
        # For a CONNECTED browser, ``browser.close()`` only DISCONNECTS — the
        # attached process keeps running (correct + safe default: never
        # terminate a browser we did not spawn). On explicit ``kill=True`` we
        # make a BEST-EFFORT attempt to have it quit itself via CDP
        # ``Browser.close``, but this is unreliable (headless Chrome often
        # ignores it). We deliberately do NOT taskkill an external process — the
        # layer that STARTED the browser (e.g. the exec/background_process that
        # launched it for a connected session) owns terminating it. The tool
        # result says so on connected+kill (see the handler) to avoid the caller
        # assuming the process is gone.
        if self._browser is not None:
            if self._kind == "connected" and kill:
                await _bounded_close(self._cdp_close_browser())
            await _bounded_close(self._browser.close())
        if self._playwright is not None:
            await _bounded_close(self._playwright.stop())
        self._context = None
        self._browser = None
        self._playwright = None
        self._kind = None
        self._owns_context = True

    async def _cdp_close_browser(self) -> None:
        """Ask a CONNECTED browser to quit itself via the CDP ``Browser.close``.

        Used only on an explicit ``kill=True`` for the connected kind: opens a
        browser-level CDP session and sends ``Browser.close`` so the attached
        browser exits on its own. We never taskkill an external process — the
        browser terminates itself in response to its own protocol command.
        """
        if self._browser is None:
            return
        session = await self._browser.new_browser_cdp_session()
        try:
            await session.send("Browser.close")
        finally:
            with contextlib.suppress(Exception):
                await session.detach()

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
            if not self._tabs:
                _log.info("browser_manager.idle_close", extra={"idle_seconds": self._idle_close_seconds})
                await self._dispose(kill=True)


def _resolve_kind(kind: str | None, app: dict[str, Any] | None) -> BrowserKind:
    if kind in ("headless", "connected"):
        return kind  # type: ignore[return-value]
    if app and app.get("cdp_url"):
        return "connected"
    return "headless"


async def _safe_close_page(page: Page) -> None:
    await _bounded_close(page.close())


async def _bounded_close(coro: Any) -> None:
    """Await a teardown coroutine, abandoning it if it exceeds the close ceiling.

    Best-effort: any exception (including a timeout) is swallowed so cleanup
    never raises out of ``close``/``aclose``/``_dispose``. On timeout the
    coroutine is cancelled so it does not linger.
    """
    task = asyncio.ensure_future(coro)
    try:
        await asyncio.wait_for(task, timeout=_CLOSE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — cleanup (incl. timeout) must never raise
        with contextlib.suppress(Exception):
            task.cancel()

