# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cross-context tool bridge: register ai_coding's 10 tools into chat (Batch B / B-1).

Anti-corruption layer that adapts the ``qai.ai_coding`` 10 production tool
handlers (``read`` / ``list`` / ``write`` / ``edit`` / ``glob`` / ``grep`` /
``exec`` / ``webfetch`` / ``apply_patch`` / ``appbuilder_run``) into
the ``qai.chat`` :class:`ToolHandler` callable signature so the chat
context can dispatch tool calls emitted by the LLM (``tool_call``
frames) without ever importing ``qai.ai_coding``.

This module lives in ``apps/api/`` because cross-context import is
forbidden by the ``context-isolation`` import-linter contract;
``apps/api/`` is the only layer permitted to compose two contexts.
The bridge follows the same pattern as
:mod:`apps.api._skill_registry_bridge` and
:mod:`apps.api._chat_message_bridge`: lazy imports of the source
context's types are localised to this module so neither
``qai.chat`` nor ``qai.ai_coding`` acquires a hard dependency on the
other's domain.

Signature shapes
----------------
* ai_coding handler  — ``async (dict[str, Any]) -> dict[str, Any]``.
* chat ToolHandler   — ``async (ToolInvocationRequest) -> Any``
  (the ``RegistryBackedToolInvocation`` adapter wraps the return
  value into :class:`ToolInvocationResult` ``ok=True`` envelope, or
  ``ok=False`` with ``chat.tool_handler_failed`` on raise).

The bridge unwraps ``ToolInvocationRequest.arguments`` into the dict
the ai_coding handler expects, awaits it, and returns the raw dict
result (which is what the chat use case forwards to the LLM as
``tool_result`` content).

Why the bridge does NOT register schemas on the chat side
---------------------------------------------------------
The chat ``ToolInvocationPort`` exposes only ``invoke()``.  Tool
schemas are advertised to the LLM via the system prompt's
``<tools>`` XML section (``extra["tools_xml"]`` /
``RichSystemPromptBuilder.tools_xml``).  This bridge therefore also
exposes :func:`build_tools_xml_section` which renders the same
9-tool ``TOOL_SCHEMAS`` table into the legacy XML shape the system
prompt builder expects, so callers can pre-compute the XML once at
DI time and inject it into the builder constructor.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from apps.api._chat_tool_result_render import render_tool_result_text_with_hints
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest

__all__ = [
    "register_ai_coding_tools_into_chat",
    "build_tools_xml_section",
]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_ai_coding_tools_into_chat(
    *,
    tools: RegistryBackedToolInvocation,
    file_guard: Any,
    file_broker: Any,
    tool_result_store: Any | None = None,
    path_lock: Any = None,
) -> tuple[str, ...]:
    """Register the ai_coding 9 production tools onto the chat tool registry.

    The chat ``ToolInvocationPort`` adapter dispatches by tool name.
    Each ai_coding handler is wrapped into a chat-shaped
    ``async (ToolInvocationRequest) -> Any`` closure that:

    1. extracts ``request.arguments`` (a ``dict``);
    2. awaits the underlying ai_coding ``async (dict) -> dict`` handler
       (which already converts ``ToolGuardDenied`` / ``ToolError`` into
       ``{"ok": False, "error_code": ..., "message": ...}`` dicts);
    3. returns the dict directly — the
       :class:`RegistryBackedToolInvocation` adapter will wrap it into
       :class:`ToolInvocationResult` ``ok=True`` so the chat use case's
       ``tool_result`` frame carries the structured payload.

    Cross-context import discipline
    -------------------------------
    The ai_coding factory is imported lazily inside this function so a
    failure to import (e.g. minimal deployments without ai_coding
    wired) degrades to "no tool registered" instead of breaking
    ``build_chat_services``.  This also matches the pattern used by
    :class:`SkillRegistryBridge._resolve_factories`.

    Returns
    -------
    tuple[str, ...]
        The names of tools successfully registered (sorted).  An empty
        tuple means the ai_coding context could not be reached and
        chat will respond with ``chat.tool_not_registered`` for every
        tool call (the legacy fallback behaviour).
    """
    try:
        # Lazy import — apps/api is allowed to compose two contexts;
        # this import does NOT happen at qai.chat or qai.ai_coding
        # source level so the import-linter context-isolation contract
        # stays clean.  See _skill_registry_bridge for the same pattern.
        from qai.ai_coding.infrastructure.tools.registry import (
            TOOL_SCHEMAS,
            build_default_tool_handlers,
        )
    except Exception:  # noqa: BLE001 — best-effort cross-context wiring
        return ()

    try:
        ai_handlers = build_default_tool_handlers(
            file_guard=file_guard,
            file_broker=file_broker,
            tool_result_store=tool_result_store,
            path_lock=path_lock,
        )
    except Exception:  # noqa: BLE001
        return ()

    registered: list[str] = []
    for tool_name, ai_handler in ai_handlers.items():
        wrapped = _wrap_ai_coding_handler(ai_handler)
        # PR-fix-cloud-tools (2026-06-04): also forward the OpenAI
        # function-calling schema so the chat-side registry can
        # advertise the tool to cloud LLMs via the standard
        # ``payload["tools"]`` field.  ``TOOL_SCHEMAS`` is keyed by the
        # same tool name as ``build_default_tool_handlers`` returns;
        # missing keys (defensive) fall through as ``None`` which keeps
        # the legacy behaviour for that single tool.
        schema = TOOL_SCHEMAS.get(tool_name) if isinstance(TOOL_SCHEMAS, dict) else None
        try:
            tools.register(tool_name, wrapped, schema=schema)
        except Exception:  # noqa: BLE001 — never block chat startup
            continue
        registered.append(tool_name)

    # Feed the cloud-only enhanced descriptions into the registry so the chat
    # streaming loop can overlay them for cloud turns WITHOUT importing
    # ai_coding (context-isolation). apps/ is the only layer allowed to bridge
    # two contexts.
    try:
        from qai.ai_coding.infrastructure.tools.handlers._shared import (
            CLOUD_TOOL_DESCRIPTION_OVERRIDES,
        )

        setter = getattr(tools, "set_cloud_description_overrides", None)
        if callable(setter):
            setter(CLOUD_TOOL_DESCRIPTION_OVERRIDES)
    except Exception:  # noqa: BLE001 — best-effort; never break chat startup
        pass

    return tuple(sorted(registered))


def _wrap_ai_coding_handler(
    ai_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> Callable[[ToolInvocationRequest], Awaitable[Any]]:
    """Return a chat-shaped :type:`ToolHandler` wrapping ``ai_handler``.

    The closure intentionally captures only ``ai_handler``; no module
    globals or context state are referenced so the wrapper is safe to
    register more than once and is independent of process lifetime.

    The ai_coding handler returns a **structured dict** (``_ok(...)`` /
    ``{"ok": False, ...}``).  V1 fed the model a **plain-text string**
    (``backend/tools/*.py`` handlers ``return str``), so we project the
    dict into V1-style text here — in the apps/api ACL layer — before it
    reaches the chat use case.  This makes ``streaming.py`` receive a
    ``str`` on the normal path (its ``str(raw_result)`` fallback no
    longer fires), so the ``role:tool`` wire content **and** the chat UI
    tool card both show clean V1-style output instead of a Python dict
    ``repr`` (the reported "工具输出是 JSON 格式化数据" bug).  The
    structured dict shape is untouched at the source — only this
    chat-facing projection turns it into text.
    """

    async def _chat_handler(request: ToolInvocationRequest) -> Any:
        # ai_coding handlers expect a plain ``dict[str, Any]`` of args;
        # ToolInvocationRequest.arguments is already that shape.  Defensive
        # ``dict(...)`` copy isolates mutations the handler might make.
        args = dict(request.arguments)
        result = await ai_handler(args)
        # Project the structured result into V1-style plain text for the
        # LLM / UI tool card.  Non-dict results pass through ``str``
        # unchanged inside the renderer.
        return render_tool_result_text_with_hints(result)

    return _chat_handler


# ---------------------------------------------------------------------------
# tools_xml renderer
# ---------------------------------------------------------------------------

#: Tools that must NOT appear in the local-daemon ``<tools>`` XML section even
#: though they live in the global ``TOOL_SCHEMAS`` table.
#:
#: ``web_search`` is a CLOUD-only, internal-only tool: its handler is dispatched
#: by the Python chat backend (against the internal CEBot search provider) and
#: is wired ONLY on internal editions behind ``settings.is_internal`` (see
#: ``registry.build_default_tool_handlers`` + ``apps.api._web_search_bridge``).
#: The XML section here feeds the ON-DEVICE GenieAPIService daemon's local
#: small model, which has NO web_search implementation. Advertising web_search
#: to that local model would re-create the exact "model is told a tool exists,
#: calls it, gets nothing" black hole this whole feature set out to remove. So
#: it is excluded from the local XML; the cloud lane advertises it via the
#: per-turn ``tools_schemas`` path (which is built from REGISTERED handlers, so
#: it is naturally absent on external editions).
_XML_EXCLUDED_TOOLS: frozenset[str] = frozenset({"web_search"})


def build_tools_xml_section() -> str:
    """Render the ai_coding ``TOOL_SCHEMAS`` into a ``<tools>`` XML block.

    The chat system prompt builder consumes the resulting string via
    ``extra["tools_xml"]`` / ``RichSystemPromptBuilder.tools_xml`` and
    splices it verbatim into the assembled prompt so the LLM sees a
    function-calling-style tool list.  The rendered shape mirrors the
    legacy ``backend.chat_handler._build_tools_section`` format:

        <tools>
        <tool name="read">
        <description>...</description>
        <parameters>
        {... JSON Schema ...}
        </parameters>
        </tool>
        ...
        </tools>

    Returns the empty string if the ai_coding ``TOOL_SCHEMAS`` cannot
    be imported (minimal deployments) — callers treat that as
    "no tools advertised" and the system prompt builder simply omits
    the section.
    """
    try:
        from qai.ai_coding.infrastructure.tools.registry import TOOL_SCHEMAS
    except Exception:  # noqa: BLE001
        return ""

    if not TOOL_SCHEMAS:
        return ""

    parts: list[str] = ["<tools>"]
    for tool_name in sorted(TOOL_SCHEMAS.keys()):
        if tool_name in _XML_EXCLUDED_TOOLS:
            # Internal-only / cloud-only tool — never advertise to the local
            # on-device daemon model (no local implementation). See
            # ``_XML_EXCLUDED_TOOLS``.
            continue
        spec = TOOL_SCHEMAS[tool_name]
        # ``spec`` follows the OpenAI function-calling shape:
        #   {"type": "function", "function": {"name", "description", "parameters"}}
        fn = spec.get("function", {}) if isinstance(spec, dict) else {}
        if not isinstance(fn, dict):
            fn = {}
        name = str(fn.get("name", tool_name))
        description = str(fn.get("description", ""))
        parameters = fn.get("parameters", {})
        try:
            params_json = json.dumps(parameters, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            params_json = "{}"
        parts.append(f'<tool name="{name}">')
        parts.append(f"<description>{description}</description>")
        parts.append("<parameters>")
        parts.append(params_json)
        parts.append("</parameters>")
        parts.append("</tool>")
    parts.append("</tools>")
    return "\n".join(parts)
