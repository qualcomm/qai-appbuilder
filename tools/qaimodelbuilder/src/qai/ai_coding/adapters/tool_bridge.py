# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Registry-backed :class:`ToolBridgePort` (PR-046).

Replaces the in-memory ``_FakeAiCodingToolBridge`` from S3 with a
real bridge that dispatches by tool name into a registry of
``async`` callables.  The registry is populated by the application
layer at wiring time; an empty registry means *no tool resolves* and
every invocation surfaces a ``ToolBridgeResult(ok=False,
error_code="tool_not_found")``.

Cross-context isolation
-----------------------
This adapter does NOT import any tool implementation.  Concrete
tools (filesystem, shell, app_builder) live in their own contexts
and are wired in via :meth:`register` from ``apps/api/`` (which is
the only layer permitted to compose multiple contexts).  Until the
broader tools-registry refactor lands, the bridge starts empty and
every tool call is reported as missing — matching the documented
behaviour for the "no tool dispatch yet" state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from qai.ai_coding.application.ports import ToolBridgeResult
from qai.ai_coding.domain import ToolName

__all__ = [
    "TOOL_INVOCATION_FAILED_ERROR_CODE",
    "TOOL_NOT_FOUND_ERROR_CODE",
    "RegistryBackedToolBridge",
    "ToolHandler",
]


TOOL_NOT_FOUND_ERROR_CODE = "tool_not_found"
TOOL_INVOCATION_FAILED_ERROR_CODE = "tool_invocation_failed"


# ``Callable[..., Awaitable[...]]`` rather than ``Coroutine`` so plain
# ``async def`` functions and ``functools.partial``-wrapped wrappers both
# satisfy the type.
ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class RegistryBackedToolBridge:
    """Concrete :class:`ToolBridgePort` backed by a name → handler dict.

    7-L4 (V1 parity with ``backend/tools/registry.py``'s ``ToolEntry``):
    each registration may carry optional ``conditional`` / ``streaming``
    metadata + an optional OpenAI-format ``schema``.  ``conditional=True``
    tools are NOT part of the default tool set surfaced to the model in all
    modes — they must be explicitly enabled per mode (mirrors V1
    ``schemas(exclude_conditional=True)``).  These are additive, keyword-only
    fields with benign defaults so existing ``register(tool_name=, handler=)``
    call sites are unaffected.
    """

    __slots__ = ("_conditional", "_handlers", "_schemas", "_streaming")

    def __init__(
        self,
        *,
        handlers: dict[str, ToolHandler] | None = None,
    ) -> None:
        # Defensive copy so callers cannot mutate the registry after wiring.
        self._handlers: dict[str, ToolHandler] = dict(handlers or {})
        self._conditional: set[str] = set()
        self._streaming: set[str] = set()
        self._schemas: dict[str, dict[str, Any]] = {}

    def register(
        self,
        *,
        tool_name: str,
        handler: ToolHandler,
        conditional: bool = False,
        streaming: bool = False,
        schema: dict[str, Any] | None = None,
    ) -> None:
        """Add or replace a tool handler.

        Wired via ``apps/api/`` after :class:`AiCodingServices` is
        constructed so the cross-context isolation boundary stays
        clean.

        Args:
            conditional: when ``True`` the tool is omitted from the default
                tool set surfaced to the model (see
                :meth:`registered_schemas` ``exclude_conditional``); it must
                be explicitly enabled per mode.  V1 ``ToolEntry.conditional``.
            streaming: marks the handler as supporting incremental streaming
                output (metadata only; the bridge's :meth:`invoke` always
                drains to a final result).  V1 ``ToolEntry.streaming_handler``.
            schema: optional OpenAI function-calling schema for the tool,
                surfaced via :meth:`registered_schemas`.
        """
        self._handlers[tool_name] = handler
        if conditional:
            self._conditional.add(tool_name)
        else:
            self._conditional.discard(tool_name)
        if streaming:
            self._streaming.add(tool_name)
        else:
            self._streaming.discard(tool_name)
        if schema is not None:
            self._schemas[tool_name] = schema

    def is_registered(self, tool_name: str) -> bool:
        return tool_name in self._handlers

    def is_conditional(self, tool_name: str) -> bool:
        """Return True when *tool_name* was registered ``conditional=True``."""
        return tool_name in self._conditional

    def is_streaming(self, tool_name: str) -> bool:
        """Return True when *tool_name* was registered ``streaming=True``."""
        return tool_name in self._streaming

    def registered_tool_names(
        self, *, exclude_conditional: bool = False
    ) -> list[str]:
        names: list[str] = list(self._handlers.keys())
        if exclude_conditional:
            names = [n for n in names if n not in self._conditional]
        return sorted(names)

    def registered_schemas(
        self, *, exclude_conditional: bool = False
    ) -> list[dict[str, Any]]:
        """Return the registered tool schemas (V1 ``registry.schemas``).

        Args:
            exclude_conditional: when ``True``, omit tools registered with
                ``conditional=True`` — the default tool set sent to the model.
        """
        out: list[dict[str, Any]] = []
        for name in sorted(self._schemas):
            if exclude_conditional and name in self._conditional:
                continue
            out.append(self._schemas[name])
        return out

    async def invoke(
        self,
        *,
        tool_name: ToolName,
        args: dict[str, Any],
    ) -> ToolBridgeResult:
        handler = self._handlers.get(tool_name.value)
        if handler is None:
            return ToolBridgeResult(
                ok=False,
                error_code=TOOL_NOT_FOUND_ERROR_CODE,
            )
        try:
            result = await handler(dict(args))
        except Exception:  # noqa: BLE001 — surface as result, not raise
            return ToolBridgeResult(
                ok=False,
                error_code=TOOL_INVOCATION_FAILED_ERROR_CODE,
            )
        if not isinstance(result, dict):
            return ToolBridgeResult(
                ok=False,
                error_code=TOOL_INVOCATION_FAILED_ERROR_CODE,
            )
        return ToolBridgeResult(ok=True, result=result)
