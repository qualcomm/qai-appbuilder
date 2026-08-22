# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Per-tab operations for the interactive browser tool.

A :class:`TabHandle` wraps a single Playwright ``Page`` (+ its owning context)
and exposes the tab-helper surface the reference tool offers — navigation,
interaction, waits, snapshots, screenshots, ``extract``, and arbitrary
page-context JS ``run`` — each mapped onto Playwright ``async_api`` and each
wrapped with a per-op timeout so a hung op fails fast with a named error rather
than stalling the whole tool call.

Selectors accept the reference vocabulary (CSS / ``text/`` / ``xpath/`` /
``aria/`` / ``ref/eN``) parsed by :mod:`.selectors`; ``ref/eN`` resolves against
the ``data-qai-ref`` attributes tagged by the last :meth:`observe`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from qai.platform.logging import get_logger

from .facade import REF_ATTR, observe_script, run_wrapper
from .redact import redact_url_credentials
from .selectors import ParsedSelector, parse_selector

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from playwright.async_api import BrowserContext, Locator, Page

__all__ = ["BrowserToolError", "TabHandle"]

_log = get_logger(__name__)

#: Return type of the awaitable handed to :meth:`TabHandle._act`.
_T = TypeVar("_T")

#: Per-op ceilings (ms), tiered by what the op is waiting for. A stuck op must
#: surface a named error with recovery budget left, so none of these approach a
#: typical tool wall-clock.
#:
#: ``goto`` and ``run`` are deliberately NOT capped by these: navigation and
#: model code legitimately need the full budget.
_DEFAULT_OP_TIMEOUT_MS = 30_000
#: Quick read ops (snapshot/extract) get a shorter ceiling.
_QUICK_OP_TIMEOUT_MS = 20_000
#: Interactive ops (click/fill/type/…). Lower than the read ceiling because an
#: element that exists becomes actionable quickly or not at all; waiting 30s
#: mostly means the page will never cooperate.
_ACTION_OP_TIMEOUT_MS = 8_000

#: A selector op whose selector has matched NOTHING for this long fails fast
#: instead of burning its whole ceiling. A wrong selector — or the right one on
#: the wrong page (consent wall, pre-navigation document) — is the most common
#: automation mistake, and it should cost ~2s rather than the full ceiling.
#:
#: Measured before this existed: a bad selector cost 30.02s; a good one 0.08s.
#:
#: Crucially this only fires on CONFIRMED zero matches. An element that exists
#: but is not yet visible/enabled keeps the full ceiling, so the fast path does
#: not truncate legitimate waiting on a slow-rendering page.
_ZERO_MATCH_FAIL_FAST_MS = 2_000
#: Poll cadence for the zero-match watchdog.
_ZERO_MATCH_POLL_MS = 250


class BrowserToolError(Exception):
    """Raised for a browser-tool operation failure (bad selector, timeout, …).

    The tool handler converts this into the ``{ok: False, ...}`` envelope; it
    carries a human-readable message only (no host detail).
    """


def _timeout_ms(timeout_s: float | None, default_ms: int) -> int:
    if timeout_s is None:
        return default_ms
    try:
        ms = int(float(timeout_s) * 1000)
    except (TypeError, ValueError):
        return default_ms
    return max(1, ms)


class TabHandle:
    """One named tab: a Playwright ``Page`` plus its owning context."""

    __slots__ = ("_context", "_default_timeout_ms", "name", "page")

    def __init__(
        self,
        *,
        name: str,
        page: Page,
        context: BrowserContext,
        default_timeout_ms: int = _DEFAULT_OP_TIMEOUT_MS,
    ) -> None:
        self.name = name
        self.page = page
        self._context = context
        self._default_timeout_ms = default_timeout_ms

    # -- helpers -------------------------------------------------------------

    def url(self) -> str:
        """Current page URL, with any ``user:pass@`` credentials redacted."""
        return redact_url_credentials(self.page.url)

    async def _locator(self, selector: str, timeout_ms: int) -> Locator:
        """Resolve a model selector string into a Playwright ``Locator``."""
        try:
            parsed: ParsedSelector = parse_selector(selector)
        except ValueError as exc:
            raise BrowserToolError(str(exc)) from exc
        page = self.page
        if parsed.kind == "css":
            return page.locator(parsed.value)
        if parsed.kind == "text":
            return page.get_by_text(parsed.value)
        if parsed.kind == "xpath":
            return page.locator(f"xpath={parsed.value}")
        if parsed.kind == "aria":
            # aria/Name → match by accessible name across the common interactive
            # roles (Playwright has no role-agnostic name lookup, so chain the
            # roles a model most often targets).
            name = parsed.value
            roles = (
                "link", "button", "textbox", "checkbox", "radio",
                "combobox", "menuitem", "tab", "option",
            )
            locator = page.get_by_role(roles[0], name=name)  # type: ignore[arg-type]
            for role in roles[1:]:
                locator = locator.or_(page.get_by_role(role, name=name))  # type: ignore[arg-type]
            return locator
        if parsed.kind == "ref":
            return page.locator(f'[{REF_ATTR}="{parsed.value}"]')
        raise BrowserToolError(f"unsupported selector kind: {parsed.kind}")

    async def _zero_match_watchdog(
        self, locator: Locator, selector: str, op: str
    ) -> None:
        """Fail fast once ``locator`` is confirmed to match nothing.

        Polls the match count and raises as soon as it has stayed at zero for
        :data:`_ZERO_MATCH_FAIL_FAST_MS`. It never returns normally: once the
        element is seen it parks until cancelled, so the caller's real action
        keeps the remaining budget to wait for the element to become actionable.

        A ``count()`` call that itself fails (detached frame mid-navigation) is
        treated as "cannot judge yet", never as zero: reporting a bad selector
        during a navigation would be wrong and would mask the real error.
        """
        deadline = _ZERO_MATCH_FAIL_FAST_MS / 1000
        waited = 0.0
        # A verdict requires evidence: at least one probe must have actually
        # answered "zero". Without this, a locator whose count() keeps raising
        # (frame detaching through a navigation) would fall out of the loop and
        # be reported as a bad selector — the exact misdiagnosis this watchdog
        # must never make.
        observed_zero = False
        while waited < deadline:
            try:
                if await locator.count() > 0:
                    await asyncio.Event().wait()  # park; cancelled by _act
                observed_zero = True
            except Exception as exc:  # noqa: BLE001 — see below
                # Cannot judge this tick (frame detached mid-navigation, page
                # closing). Treated as "unknown", never as zero.
                _log.debug(
                    "browser.zero_match_probe_unavailable",
                    selector=selector,
                    reason=type(exc).__name__,
                )
            await asyncio.sleep(_ZERO_MATCH_POLL_MS / 1000)
            waited += _ZERO_MATCH_POLL_MS / 1000
        if not observed_zero:
            # Never got a usable reading — yield the verdict to the real action's
            # own ceiling and error, which will describe the actual problem.
            await asyncio.Event().wait()
        raise BrowserToolError(
            f"{op}: selector {selector!r} matched no element after "
            f"{_ZERO_MATCH_FAIL_FAST_MS}ms — check the selector, or whether the "
            f"page finished loading / is the page you expect (url={self.url()})"
        )

    async def _require_present(
        self, locator: Locator, selector: str, op: str
    ) -> None:
        """Fail fast when ``selector`` is confirmed absent, else return.

        Same evidence rule as :meth:`_zero_match_watchdog` — an unanswerable
        probe is "unknown", never "absent" — but it returns instead of parking,
        so callers can use it as a precondition on a SECOND selector that the
        race-based watchdog cannot cover.
        """
        deadline = _ZERO_MATCH_FAIL_FAST_MS / 1000
        waited = 0.0
        observed_zero = False
        while waited < deadline:
            try:
                if await locator.count() > 0:
                    return
                observed_zero = True
            except Exception as exc:  # noqa: BLE001 — unknown, not absent
                _log.debug(
                    "browser.presence_probe_unavailable",
                    selector=selector,
                    reason=type(exc).__name__,
                )
            await asyncio.sleep(_ZERO_MATCH_POLL_MS / 1000)
            waited += _ZERO_MATCH_POLL_MS / 1000
        if not observed_zero:
            return
        raise BrowserToolError(
            f"{op}: selector {selector!r} matched no element after "
            f"{_ZERO_MATCH_FAIL_FAST_MS}ms — check the selector, or whether the "
            f"page finished loading / is the page you expect (url={self.url()})"
        )

    async def _act(
        self,
        op: str,
        selector: str,
        locator: Locator,
        action: Coroutine[Any, Any, _T],
        *,
        explicit_timeout: bool,
    ) -> _T:
        """Run ``action``, racing it against the zero-match watchdog.

        The watchdog is skipped when the caller passed an explicit timeout: that
        is a deliberate "wait this long for something to appear", and cutting it
        short at 2s would break the one case where waiting is the whole point.
        """
        if explicit_timeout:
            try:
                return await action
            except Exception as exc:
                raise _as_tool_error(op, exc, selector) from exc

        act_task = asyncio.ensure_future(action)
        watch_task = asyncio.ensure_future(
            self._zero_match_watchdog(locator, selector, op)
        )
        try:
            done, _pending = await asyncio.wait(
                (act_task, watch_task), return_when=asyncio.FIRST_COMPLETED
            )
            # The action winning is the normal path; surface its outcome as-is.
            if act_task in done:
                try:
                    return act_task.result()
                except Exception as exc:
                    raise _as_tool_error(op, exc, selector) from exc
            # Otherwise the watchdog fired: its error names the real problem.
            watch_task.result()
            msg = f"{op}: watchdog returned without a verdict"  # pragma: no cover
            raise BrowserToolError(msg)  # pragma: no cover
        finally:
            for task in (act_task, watch_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(act_task, watch_task, return_exceptions=True)

    # -- navigation ----------------------------------------------------------

    async def goto(self, url: str, *, wait_until: str | None = None, timeout_s: float | None = None) -> str:
        ms = _timeout_ms(timeout_s, self._default_timeout_ms)
        try:
            await self.page.goto(url, wait_until=wait_until or "domcontentloaded", timeout=ms)
        except Exception as exc:
            raise _as_tool_error("goto", exc) from exc
        return self.url()

    # -- interaction ---------------------------------------------------------

    async def click(self, selector: str, *, timeout_s: float | None = None) -> None:
        ms = _timeout_ms(timeout_s, _ACTION_OP_TIMEOUT_MS)
        loc = await self._locator(selector, ms)
        await self._act(
            "click",
            selector,
            loc,
            loc.first.click(timeout=ms),
            explicit_timeout=timeout_s is not None,
        )

    async def fill(self, selector: str, value: str, *, timeout_s: float | None = None) -> None:
        ms = _timeout_ms(timeout_s, _ACTION_OP_TIMEOUT_MS)
        loc = await self._locator(selector, ms)
        await self._act(
            "fill",
            selector,
            loc,
            loc.first.fill(str(value), timeout=ms),
            explicit_timeout=timeout_s is not None,
        )

    async def type(self, selector: str, text: str, *, delay_ms: float = 0, timeout_s: float | None = None) -> None:
        ms = _timeout_ms(timeout_s, _ACTION_OP_TIMEOUT_MS)
        loc = await self._locator(selector, ms)
        await self._act(
            "type",
            selector,
            loc,
            loc.first.press_sequentially(str(text), delay=delay_ms, timeout=ms),
            explicit_timeout=timeout_s is not None,
        )

    async def press(self, key: str, *, selector: str | None = None, timeout_s: float | None = None) -> None:
        ms = _timeout_ms(timeout_s, _ACTION_OP_TIMEOUT_MS)
        if selector is None:
            # Keyboard-only press has no selector to watch.
            try:
                await self.page.keyboard.press(key)
            except Exception as exc:
                raise _as_tool_error("press", exc) from exc
            return
        loc = await self._locator(selector, ms)
        await self._act(
            "press",
            selector,
            loc,
            loc.first.press(key, timeout=ms),
            explicit_timeout=timeout_s is not None,
        )

    async def scroll(self, delta_x: float, delta_y: float) -> None:
        try:
            await self.page.mouse.wheel(delta_x, delta_y)
        except Exception as exc:
            raise _as_tool_error("scroll", exc) from exc

    async def scroll_into_view(self, selector: str, *, timeout_s: float | None = None) -> None:
        ms = _timeout_ms(timeout_s, _ACTION_OP_TIMEOUT_MS)
        loc = await self._locator(selector, ms)
        await self._act(
            "scroll_into_view",
            selector,
            loc,
            loc.first.scroll_into_view_if_needed(timeout=ms),
            explicit_timeout=timeout_s is not None,
        )

    async def select(self, selector: str, *values: str, timeout_s: float | None = None) -> list[str]:
        ms = _timeout_ms(timeout_s, _ACTION_OP_TIMEOUT_MS)
        loc = await self._locator(selector, ms)
        return await self._act(
            "select",
            selector,
            loc,
            loc.first.select_option(list(values), timeout=ms),
            explicit_timeout=timeout_s is not None,
        )

    async def upload_file(self, selector: str, *paths: str, timeout_s: float | None = None) -> None:
        ms = _timeout_ms(timeout_s, _ACTION_OP_TIMEOUT_MS)
        loc = await self._locator(selector, ms)
        await self._act(
            "upload_file",
            selector,
            loc,
            loc.first.set_input_files(list(paths), timeout=ms),
            explicit_timeout=timeout_s is not None,
        )

    async def drag(self, source: str, target: str, *, timeout_s: float | None = None) -> None:
        ms = _timeout_ms(timeout_s, _ACTION_OP_TIMEOUT_MS)
        src = await self._locator(source, ms)
        dst = await self._locator(target, ms)
        # Two selectors, so the source-only watchdog would blame the source for a
        # missing TARGET — the error would name the one selector that is fine.
        # Probing the target up front keeps the diagnosis honest; it costs one
        # count() on the success path.
        if timeout_s is None:
            await self._require_present(dst, target, "drag")
        await self._act(
            "drag",
            source,
            src,
            src.first.drag_to(dst.first, timeout=ms),
            explicit_timeout=timeout_s is not None,
        )

    # -- waits ---------------------------------------------------------------

    async def wait_for_load_state(self, state: str = "load", *, timeout_s: float | None = None) -> None:
        ms = _timeout_ms(timeout_s, self._default_timeout_ms)
        try:
            await self.page.wait_for_load_state(state, timeout=ms)  # type: ignore[arg-type]
        except Exception as exc:
            raise _as_tool_error("wait_for_load_state", exc) from exc

    async def wait_for_selector(self, selector: str, *, state: str = "visible", timeout_s: float | None = None) -> None:
        ms = _timeout_ms(timeout_s, self._default_timeout_ms)
        loc = await self._locator(selector, ms)
        try:
            await loc.first.wait_for(state=state, timeout=ms)  # type: ignore[arg-type]
        except Exception as exc:
            raise _as_tool_error("wait_for_selector", exc, selector) from exc

    async def wait_for_url(self, pattern: str, *, timeout_s: float | None = None) -> str:
        ms = _timeout_ms(timeout_s, self._default_timeout_ms)
        try:
            await self.page.wait_for_url(pattern, timeout=ms)
        except Exception as exc:
            raise _as_tool_error("wait_for_url", exc) from exc
        return self.url()

    # -- snapshots -----------------------------------------------------------

    async def observe(self, *, viewport_only: bool = False) -> list[dict[str, Any]]:
        try:
            result = await self.page.evaluate(observe_script(viewport_only=viewport_only))
        except Exception as exc:
            raise _as_tool_error("observe", exc) from exc
        return result if isinstance(result, list) else []

    async def aria_snapshot(self, selector: str | None = None) -> str:
        try:
            loc = self.page.locator(selector) if selector else self.page.locator("body")
            return await loc.aria_snapshot()
        except Exception as exc:
            raise _as_tool_error("aria_snapshot", exc, selector) from exc

    async def extract(self, *, selector: str | None = None) -> str:
        try:
            if selector:
                loc = await self._locator(selector, _QUICK_OP_TIMEOUT_MS)
                return await loc.first.inner_text(timeout=_QUICK_OP_TIMEOUT_MS)
            return await self.page.inner_text("body", timeout=_QUICK_OP_TIMEOUT_MS)
        except Exception as exc:
            raise _as_tool_error("extract", exc, selector) from exc

    async def screenshot(self, *, path: str | None = None, full_page: bool = False, selector: str | None = None) -> str:
        try:
            target: Any = self.page
            if selector:
                loc = await self._locator(selector, _QUICK_OP_TIMEOUT_MS)
                target = loc.first
            if path is None:
                # Caller resolves the output dir; require an explicit path here.
                raise BrowserToolError("screenshot: an output path is required")
            out = Path(path)
            out.parent.mkdir(parents=True, exist_ok=True)
            if selector:
                await target.screenshot(path=str(out))
            else:
                await target.screenshot(path=str(out), full_page=full_page)
            return str(out)
        except BrowserToolError:
            raise
        except Exception as exc:
            raise _as_tool_error("screenshot", exc, selector) from exc

    # -- arbitrary page-context JS ------------------------------------------

    async def run(self, code: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Execute model JS ``code`` in the page context via the ``tab`` facade.

        Returns ``{"value": <return>, "displays": [...]}``. Runs in the page
        sandbox only — cannot reach the host filesystem/network/tools.
        """
        ms = _timeout_ms(timeout_s, self._default_timeout_ms)
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(run_wrapper(code)),
                timeout=ms / 1000,
            )
        except TimeoutError as exc:
            raise BrowserToolError(f"run: code execution timed out after {ms}ms") from exc
        except Exception as exc:
            raise _as_tool_error("run", exc) from exc
        if isinstance(result, dict):
            return {"value": result.get("value"), "displays": result.get("displays") or []}
        return {"value": result, "displays": []}


def _as_tool_error(op: str, exc: Exception, selector: str | None = None) -> BrowserToolError:
    """Normalize a Playwright/DOM exception into a :class:`BrowserToolError`."""
    name = type(exc).__name__
    where = f" (selector={selector!r})" if selector else ""
    if name == "TimeoutError":
        return BrowserToolError(f"{op}: operation timed out{where}")
    msg = str(exc).splitlines()[0] if str(exc) else name
    return BrowserToolError(f"{op}: {msg}{where}")
