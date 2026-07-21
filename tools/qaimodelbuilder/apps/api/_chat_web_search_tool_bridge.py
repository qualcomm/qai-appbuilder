# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Cross-context tool bridge: wire the internal-only ``web_search`` into chat.

Registers the conditional ``web_search`` tool onto the chat-side
:class:`RegistryBackedToolInvocation` registry so an LLM-emitted ``tool_call``
resolves to a REAL intranet search (via the
:class:`~qai.platform.edition.web_search.SearchProviderRegistry`) instead of
falling through to ``chat.tool_not_registered``.

Why a separate bridge (mirrors ``_chat_appbuilder_tool_bridge``)
----------------------------------------------------------------
``web_search`` is internal-only and conditional. The base
``register_ai_coding_tools_into_chat`` (``_chat_tool_bridge``) calls
``build_default_tool_handlers`` WITHOUT a ``search_registry``, so it
deliberately does not register ``web_search`` (the conditional gate). This
bridge is the dedicated post-build hook that adds it — exactly like
``register_appbuilder_tools_into_chat`` adds the conditional App Builder tools
— and is itself only invoked when ``settings.is_internal`` yields a registry.

Cross-context discipline: lives in ``apps/api`` (the only layer allowed to
compose contexts). It reuses the ai_coding ``tool_web_search`` handler + the
``TOOL_SCHEMAS["web_search"]`` schema (both already in ``qai.ai_coding``) and
the ``render_tool_result_text`` projection, so neither ``qai.chat`` nor
``qai.ai_coding`` learns about ``qai.platform.edition.web_search``. The handler
speaks to the registry purely through its ``search(...)`` duck-type.
"""

from __future__ import annotations

from typing import Any

from apps.api._chat_tool_result_render import render_tool_result_text_with_hints
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest
from qai.platform.logging import get_logger

__all__ = ["register_web_search_tool_into_chat"]

_log = get_logger(__name__)


def register_web_search_tool_into_chat(
    *,
    tools: RegistryBackedToolInvocation,
    search_registry: Any,
    file_guard: Any | None = None,
) -> tuple[str, ...]:
    """Register ``web_search`` on the chat registry, backed by ``search_registry``.

    Returns the names registered (``("web_search",)`` on success, ``()`` when
    the tools port is not the registry-backed adapter, no ``search_registry``
    was supplied, or the ai_coding handler/schema cannot be imported).
    """
    if not isinstance(tools, RegistryBackedToolInvocation):
        return ()
    if search_registry is None:
        return ()

    try:
        from qai.ai_coding.infrastructure.tools.handlers import (
            TOOL_SCHEMAS,
            tool_web_search,
        )
    except Exception:  # noqa: BLE001 — best-effort cross-context wiring
        return ()

    schema = (
        TOOL_SCHEMAS.get("web_search") if isinstance(TOOL_SCHEMAS, dict) else None
    )

    async def _chat_web_search(request: ToolInvocationRequest) -> Any:
        args = dict(request.arguments or {})
        result = await tool_web_search(
            args,
            file_guard=file_guard,
            search_registry=search_registry,
        )
        return render_tool_result_text_with_hints(result)

    try:
        tools.register("web_search", _chat_web_search, schema=schema)
    except Exception:  # noqa: BLE001 — never block chat startup
        _log.warning("chat.web_search_tool.register_failed", exc_info=True)
        return ()
    return ("web_search",)
