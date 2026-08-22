# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Interactive browser tool handler (``browser``).

Multiplexes three actions over a session-scoped
:class:`~qai.platform.web_automation.session.BrowserSessionManager` (injected at
the apps/api DI seam; the tool is absent when Playwright is not installed):

* ``open``  — acquire/create a named tab on a browser kind (headless / connected
  / spawned), optionally navigating to ``url``.
* ``close`` — release a named tab (or all tabs), optionally killing spawned
  processes.
* ``run``   — drive the tab: either a JavaScript ``code`` body executed in the
  page context (with a ``tab`` DOM facade + ``display()``), OR a single named
  ``op`` helper (observe / aria_snapshot / screenshot / goto / click / fill /
  type / press / select / extract / wait_for_selector / …) for the page-level
  operations the page-context JS cannot perform itself.

The tool drives the browser only; host filesystem/network/subprocess access is
NOT reachable from here — those remain the responsibility of the ``read`` /
``web_fetch`` / ``exec`` / ``agent`` tools.
"""

from __future__ import annotations

from typing import Any

from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.ai_coding.infrastructure.tools.handlers._shared import _ok
from qai.platform.logging import get_logger

_log = get_logger(__name__)

_VALID_ACTIONS = ("open", "close", "run")


async def tool_browser(
    args: dict[str, Any],
    *,
    browser_manager: Any | None = None,
) -> dict[str, Any]:
    """Handle a ``browser`` tool call. See module docstring for the action set."""
    if browser_manager is None:
        raise ToolError("browser: browser support is not configured in this build")

    action = args.get("action")
    if action not in _VALID_ACTIONS:
        raise ToolError("browser: 'action' must be one of open|close|run")
    name = args.get("name") or "main"

    # Import here so a build without Playwright still imports this handler
    # module cleanly (the manager is only ever injected when it is available).
    from qai.platform.web_automation.tab import BrowserToolError

    _log.info(
        "browser_tool.call",
        extra={
            "action": action,
            "tab": name,
            "op": args.get("op"),
            "has_code": bool(args.get("code")),
            "kind": args.get("kind"),
            "url": args.get("url"),
        },
    )
    try:
        if action == "open":
            tab = await browser_manager.open(
                name=name,
                url=args.get("url"),
                kind=args.get("kind"),
                app=args.get("app"),
                viewport=args.get("viewport"),
                wait_until=args.get("wait_until"),
                dialogs=args.get("dialogs"),
            )
            _log.info("browser_tool.ok", extra={"action": "open", "tab": name, "url": tab.url()})
            return _ok(f"browser open ok (tab={name})", tab=name, url=tab.url())

        if action == "close":
            want_kill = bool(args.get("kill"))
            # Capture the active kind before close: on a CONNECTED browser a
            # kill only disconnects — the process (which the tool did NOT spawn)
            # keeps running and must be terminated by whatever started it.
            active_kind = getattr(browser_manager, "kind", None)
            closed = await browser_manager.close(
                name=name,
                all=bool(args.get("all")),
                kill=want_kill,
            )
            _log.info("browser_tool.ok", extra={"action": "close", "closed": closed})
            msg = f"browser close ok ({closed} tab(s))"
            if want_kill and active_kind == "connected":
                msg += (
                    " — note: this was a CONNECTED browser; kill only "
                    "disconnects. The external browser process is NOT terminated "
                    "by this tool; stop it via whatever started it (e.g. the "
                    "exec/background_process that launched it)."
                )
            return _ok(msg, closed=closed, browser_kind=active_kind)

        # action == "run"
        tab = browser_manager.get(name)
        result = await _dispatch_run(tab, args)
        _log.info(
            "browser_tool.ok",
            extra={"action": "run", "tab": name, "op": args.get("op"), "has_code": bool(args.get("code"))},
        )
        return result
    except ToolError as exc:
        _log.warning("browser_tool.error", extra={"action": action, "tab": name, "op": args.get("op"), "error": str(exc)})
        raise
    except BrowserToolError as exc:
        _log.warning("browser_tool.error", extra={"action": action, "tab": name, "op": args.get("op"), "error": str(exc)})
        raise ToolError(f"browser: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - normalize everything else
        _log.warning("browser_tool.error", extra={"action": action, "tab": name, "op": args.get("op"), "error": repr(exc)})
        if type(exc).__name__ == "TimeoutError":
            raise ToolError(f"browser: operation timed out: {exc}") from exc
        raise ToolError(f"browser: {action} failed: {exc}") from exc


async def _dispatch_run(tab: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Execute the ``run`` action: a JS ``code`` body or a single named ``op``."""
    code = args.get("code")
    op = args.get("op")
    timeout = args.get("timeout")

    if code is not None and op is not None:
        raise ToolError("browser run: provide either 'code' or 'op', not both")

    if code is not None:
        if not isinstance(code, str):
            raise ToolError("browser run: 'code' must be a string of JavaScript")
        result = await tab.run(code, timeout_s=timeout)
        value = result.get("value")
        displays = result.get("displays") or []
        # Fold the return value + display() output INTO the message so the chat
        # renderer (which surfaces `message` for tools it does not special-case)
        # actually shows it to the model — otherwise only "ok" would be seen.
        parts = []
        if value is not None:
            parts.append(f"return: {_stringify(value)}")
        if displays:
            parts.append("display:\n" + "\n".join(_stringify(d) for d in displays))
        message = "\n".join(parts) if parts else "browser run ok (code) — no return value / display()"
        return _ok(message, tab=tab.name, value=value, displays=displays, url=tab.url())

    if op is None:
        raise ToolError("browser run: provide 'code' (JavaScript) or 'op' (named helper)")

    return await _dispatch_op(tab, op, args, timeout)


async def _dispatch_op(  # noqa: C901,PLR0911,PLR0912 - flat op dispatch table
    tab: Any, op: str, args: dict[str, Any], timeout: float | None
) -> dict[str, Any]:
    """Dispatch a single named page-level helper op against ``tab``."""
    sel = args.get("selector")
    if op == "goto":
        url = _require(args, "url", op)
        new_url = await tab.goto(url, wait_until=args.get("wait_until"), timeout_s=timeout)
        return _ok(f"navigated to {new_url}", tab=tab.name, url=new_url)
    if op == "observe":
        entries = await tab.observe(viewport_only=bool(args.get("viewport_only")))
        listing = "\n".join(
            f"  [{e.get('id')}] {e.get('role')} {e.get('name','')!r}"
            + (f" ={e.get('value')!r}" if e.get("value") else "")
            for e in entries
        )
        message = f"observe: {len(entries)} element(s)" + (f"\n{listing}" if listing else "")
        return _ok(message, tab=tab.name, elements=entries)
    if op == "aria_snapshot":
        snap = await tab.aria_snapshot(sel)
        return _ok(snap or "(empty aria snapshot)", tab=tab.name, aria=snap)
    if op == "extract":
        text = await tab.extract(selector=sel)
        return _ok(text or "(no text extracted)", tab=tab.name, text=text)
    if op == "click":
        await tab.click(_require(args, "selector", op), timeout_s=timeout)
        return _ok("click ok", tab=tab.name)
    if op == "fill":
        await tab.fill(_require(args, "selector", op), _require(args, "value", op), timeout_s=timeout)
        return _ok("fill ok", tab=tab.name)
    if op == "type":
        await tab.type(_require(args, "selector", op), _require(args, "value", op), timeout_s=timeout)
        return _ok("type ok", tab=tab.name)
    if op == "press":
        await tab.press(_require(args, "key", op), selector=sel, timeout_s=timeout)
        return _ok("press ok", tab=tab.name)
    if op == "scroll":
        await tab.scroll(float(args.get("delta_x", 0)), float(args.get("delta_y", 0)))
        return _ok("scroll ok", tab=tab.name)
    if op == "scroll_into_view":
        await tab.scroll_into_view(_require(args, "selector", op), timeout_s=timeout)
        return _ok("scroll_into_view ok", tab=tab.name)
    if op == "select":
        values = args.get("values") or ([args["value"]] if "value" in args else [])
        selected = await tab.select(_require(args, "selector", op), *values, timeout_s=timeout)
        return _ok(f"selected {selected}", tab=tab.name, selected=selected)
    if op == "upload_file":
        paths = args.get("paths") or ([args["path"]] if "path" in args else [])
        await tab.upload_file(_require(args, "selector", op), *paths, timeout_s=timeout)
        return _ok("upload_file ok", tab=tab.name)
    if op == "drag":
        await tab.drag(_require(args, "source", op), _require(args, "target", op), timeout_s=timeout)
        return _ok("drag ok", tab=tab.name)
    if op == "wait_for_selector":
        await tab.wait_for_selector(
            _require(args, "selector", op), state=args.get("state", "visible"), timeout_s=timeout
        )
        return _ok("wait_for_selector ok", tab=tab.name)
    if op == "wait_for_url":
        new_url = await tab.wait_for_url(_require(args, "pattern", op), timeout_s=timeout)
        return _ok(f"url now {new_url}", tab=tab.name, url=new_url)
    if op == "wait_for_load_state":
        await tab.wait_for_load_state(args.get("state", "load"), timeout_s=timeout)
        return _ok("wait_for_load_state ok", tab=tab.name)
    if op == "screenshot":
        path = await _resolve_screenshot_path(args.get("path"))
        saved = await tab.screenshot(
            path=path, full_page=bool(args.get("full_page")), selector=sel
        )
        return _ok(f"screenshot saved to {saved}", tab=tab.name, path=saved)
    raise ToolError(f"browser run: unknown op {op!r}")


def _require(args: dict[str, Any], key: str, op: str) -> Any:
    value = args.get(key)
    if value is None:
        raise ToolError(f"browser run op {op!r}: missing required '{key}'")
    return value


def _stringify(value: Any) -> str:
    """Render a JS return/display value for the text message shown to the model.

    Strings pass through; dict/list are compact JSON; everything else via str().
    """
    if isinstance(value, str):
        return value
    try:
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


async def _resolve_screenshot_path(explicit: str | None) -> str:
    """Return a screenshot output path, defaulting under ``data/tool_results``."""
    if explicit:
        return explicit
    import time
    from pathlib import Path

    out_dir = Path("data") / "tool_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / f"browser_shot_{int(time.time() * 1000)}.png")
