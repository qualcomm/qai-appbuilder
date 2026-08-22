# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Cross-context tool bridge: wire the ``browser`` tool into chat.

Registers the conditional ``browser`` tool onto the chat-side
:class:`RegistryBackedToolInvocation` registry so an LLM-emitted ``tool_call``
resolves to a REAL browser interaction (via the neutral
:class:`~qai.platform.web_automation.session.BrowserSessionManager`) instead of
falling through to ``chat.tool_not_registered``.

Mirrors ``_chat_web_search_tool_bridge``: the ``browser`` tool is conditional
(only when a manager is injected — i.e. Playwright installed + not disabled),
so the base chat tool wiring does not register it; this dedicated post-build
hook adds it. Lives in ``apps/api`` (the only layer allowed to compose
contexts). It reuses the ai_coding ``tool_browser`` handler + the
``TOOL_SCHEMAS["browser"]`` schema and the ``render_tool_result_text`` hint
projection, so neither ``qai.chat`` nor ``qai.ai_coding`` learns about the
manager package. The handler speaks to the manager purely through its
duck-typed ``open`` / ``close`` / ``get`` / ``run`` surface.
"""

from __future__ import annotations

from typing import Any

from apps.api._chat_tool_result_render import render_tool_result_text_with_hints
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest, ToolInvocationResult
from qai.platform.logging import get_logger

__all__ = ["register_browser_tool_into_chat"]

_log = get_logger(__name__)


def register_browser_tool_into_chat(
    *,
    tools: RegistryBackedToolInvocation,
    browser_manager: Any,
) -> tuple[str, ...]:
    """Register ``browser`` on the chat registry, backed by ``browser_manager``.

    Returns ``("browser",)`` on success, or ``()`` when the tools port is not
    the registry-backed adapter, no manager was supplied, or the ai_coding
    handler/schema cannot be imported.
    """
    if not isinstance(tools, RegistryBackedToolInvocation):
        return ()
    if browser_manager is None:
        return ()

    try:
        from qai.ai_coding.infrastructure.tools.handlers import (
            TOOL_SCHEMAS,
            tool_browser,
        )
    except Exception:  # noqa: BLE001 — best-effort cross-context wiring
        return ()

    schema = TOOL_SCHEMAS.get("browser") if isinstance(TOOL_SCHEMAS, dict) else None

    async def _chat_browser(request: ToolInvocationRequest) -> Any:
        args = dict(request.arguments or {})
        # tool_browser RAISES ToolError on every failure path (it never returns
        # an ``{ok: False}`` dict). Catch it here so the chat frame carries the
        # SAME ``ai_coding.tool.browser_error`` code the ai_coding-registry
        # ``_wrap`` path emits — otherwise invoke()'s generic normalization
        # would produce a divergent code for the identical failure.
        from qai.ai_coding.infrastructure.tools.errors import ToolError

        try:
            result = await tool_browser(args, browser_manager=browser_manager)
        except ToolError as exc:
            return ToolInvocationResult(
                tool_name=request.tool_name,
                ok=False,
                result=f"[tool_error] {exc}",
                error_code="ai_coding.tool.browser_error",
                error_message=str(exc),
            )
        rendered = render_tool_result_text_with_hints(result)
        if isinstance(result, dict) and result.get("ok") is False:
            code = result.get("error_code")
            return ToolInvocationResult(
                tool_name=request.tool_name,
                ok=False,
                result=rendered,
                error_code=code if isinstance(code, str) and code else None,
                error_message=(
                    result.get("message")
                    if isinstance(result.get("message"), str)
                    else None
                ),
            )
        return rendered

    try:
        tools.register("browser", _chat_browser, schema=schema)
    except Exception:  # noqa: BLE001 — never block chat startup
        _log.warning("chat.browser_tool.register_failed", exc_info=True)
        return ()
    return ("browser",)
