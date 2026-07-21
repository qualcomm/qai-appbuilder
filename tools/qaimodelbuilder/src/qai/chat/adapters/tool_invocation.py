# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Registry-backed :class:`ToolInvocationPort` adapter (PR-042).

Dispatches a named tool to one of a registry of async callables. The
adapter ships with an empty registry; the application root populates
it explicitly with the tools the chat surface should expose (e.g. the
``agent`` sub-agent tool wired in ``apps/api/_chat_di.py``). Plugins
or alternate fronts that need additional tools register their own
callables on the same registry.

This adapter deliberately stays in the chat context: cross-context tool
invocation must come through a dedicated coordination port -- the chat
context never imports ``qai.tools.*`` or any other context's adapters
directly (``context-isolation`` import-linter contract).

PR-fix-cloud-tools (2026-06-04): :meth:`register` now accepts an optional
``schema`` argument carrying the OpenAI function-calling schema for the
tool, and :meth:`schemas` returns the tuple of registered schemas so the
streaming use case can advertise them to cloud LLMs as the standard
``tools=[...]`` request body field.  Both additions are additive; the
``schema`` parameter defaults to ``None`` and existing call sites that
register handlers without a schema continue to work (they just do not
contribute a tool to the cloud-side ``tools[]`` advertisement).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from qai.chat.application.ports import (
    ToolInvocationRequest,
    ToolInvocationResult,
)


__all__ = ["RegistryBackedToolInvocation", "ToolHandler", "TOOL_ORDER"]


ToolHandler = Callable[[ToolInvocationRequest], Awaitable[Any]]
"""Async function: takes a request, returns whatever the tool produces."""


# Fixed advertisement order for the tools exposed on the wire
# (``payload["tools"]``).  Rationale (user 2026-06-15): the model should see
# the most-used, general-purpose tools FIRST so the ordering nudges it toward
# the common file/shell operations rather than the niche ones.  Tools NOT
# listed here fall back to alphabetical order AFTER the listed ones (so a
# newly-registered tool still appears deterministically without a code change,
# just at the tail).  This is the SINGLE source of truth for tool order; any
# other renderer that lists tools (e.g. a prompt-body section) should sort by
# this same list so every surface stays consistent.
TOOL_ORDER: tuple[str, ...] = (
    "read",
    "edit",
    "write",
    "apply_patch",
    "exec",
    "background_process",
    "glob",
    "grep",
    "webfetch",
    "web_search",
    "agent",
    # Harness control tools (V2 enhancement): list after the work tools so the
    # model reaches for real file/shell operations first.
    "list_subagents",
    "skill",
    "todowrite",
    "question",
    # Conditional / mode-specific tools last (only advertised in their mode).
    "appbuilder_run",
    "appbuilder_batch_run",
)


def order_tool_names(names: "Iterable[str]") -> list[str]:
    """Sort tool names by :data:`TOOL_ORDER`, unlisted names last (A-Z).

    The single ordering primitive shared by every place that lists tools so
    the wire ``payload["tools"]`` order is deterministic and identical across
    surfaces.  Listed names keep their :data:`TOOL_ORDER` index; names absent
    from the list are appended in alphabetical order after all listed ones.
    """
    rank = {name: i for i, name in enumerate(TOOL_ORDER)}
    tail = len(TOOL_ORDER)
    return sorted(names, key=lambda n: (rank.get(n, tail), n))


class RegistryBackedToolInvocation:
    """Dispatch tool invocations against a name -> callable registry.

    The handler signature is ``async (request) -> Any``; the result is
    wrapped in :class:`ToolInvocationResult` (``ok=True``). Errors
    raised by the handler become ``ok=False`` results carrying
    ``error_code`` / ``error_message``.

    Unknown tool names yield ``ok=False`` with ``error_code =
    "chat.tool_not_registered"`` -- this matches the contract that the
    port never raises (the use case decides how to surface it).

    Each registered tool may carry an optional OpenAI function-calling
    schema (see :meth:`register` ``schema`` parameter); :meth:`schemas`
    returns the registered schemas as a tuple so cloud LLM callers can
    forward them on the wire as ``payload["tools"]``.
    """

    __slots__ = ("_handlers", "_schemas", "_cloud_desc_overrides")

    def __init__(
        self,
        *,
        handlers: dict[str, ToolHandler] | None = None,
    ) -> None:
        self._handlers: dict[str, ToolHandler] = dict(handlers or {})
        # Schema dict keyed by tool name; only populated for tools that
        # were registered with a non-``None`` ``schema`` argument.
        self._schemas: dict[str, dict[str, Any]] = {}
        # Cloud-only tool description overrides keyed by tool name.  Injected
        # by the apps-layer bridge from the ai_coding registry so the chat
        # context never imports ai_coding directly (context-isolation).
        self._cloud_desc_overrides: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        schema: Mapping[str, Any] | None = None,
    ) -> None:
        """Register a handler. Idempotent overwrite.

        ``schema`` is the OpenAI function-calling schema for this tool
        (shape: ``{"type": "function", "function": {"name", "description",
        "parameters"}}``).  When supplied, :meth:`schemas` will include it
        in its return value so the streaming use case can advertise the
        tool to cloud LLMs via ``payload["tools"]``.  ``None`` (the
        default) keeps the legacy behaviour: the tool is dispatchable on
        ``invoke`` but is not advertised to the LLM via the standard
        OpenAI tool-call protocol (callers may still surface it through
        the system prompt's ``<tools>`` XML block as a fallback for local
        models).
        """
        if not name:
            raise ValueError("tool name must be non-empty")
        self._handlers[name] = handler
        if schema is not None:
            # Defensive copy + dict() coercion isolates the registry from
            # mutations to the caller's dict and ensures the value is a
            # plain dict (some callers may pass MappingProxyType / other
            # Mapping subtypes).
            self._schemas[name] = dict(schema)
        else:
            # Drop any previous schema for this name so a re-register
            # without ``schema`` matches "no schema" semantics rather
            # than silently retaining stale advertisement.
            self._schemas.pop(name, None)

    def unregister(self, name: str) -> None:
        """Remove a handler. Idempotent; missing names are ignored."""
        self._handlers.pop(name, None)
        self._schemas.pop(name, None)

    @property
    def registered_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def schemas(self) -> tuple[dict[str, Any], ...]:
        """Return registered OpenAI tool schemas as a tuple.

        Order is deterministic and follows :data:`TOOL_ORDER` (most-used,
        general-purpose tools first — read / edit / write / exec …; unlisted
        tools appended alphabetically) so the wire ``payload["tools"]``
        ordering is stable, intentional, and identical across surfaces.  Two
        consecutive calls produce byte-identical tuples — useful for tests and
        for any cache that keys on the rendered ``tools[]`` array.  Empty when
        no tool was registered with a schema.

        Returned dicts are fresh shallow copies; callers may safely
        add/remove top-level keys without affecting the registry's
        stored schemas.  The nested ``function`` / ``parameters`` blocks
        are NOT deep-copied (schemas can be large; copying is reserved
        for the caller that actually intends to mutate them).
        """
        return tuple(
            dict(self._schemas[name]) for name in order_tool_names(self._schemas)
        )

    def set_cloud_description_overrides(
        self, overrides: Mapping[str, Mapping[str, Any]] | None
    ) -> None:
        """Store cloud-only tool description overrides (keyed by tool name).

        Injected by the apps-layer bridge from the ai_coding registry so the
        chat context never imports ai_coding directly (context-isolation).
        Cloud turns apply these richer descriptions; on-device turns keep the
        short registered schema text unchanged.
        """
        self._cloud_desc_overrides = {
            str(k): dict(v) for k, v in (overrides or {}).items()
        }

    def cloud_description_overrides(self) -> dict[str, dict[str, Any]]:
        """Return the stored cloud description overrides (fresh shallow copy)."""
        return {k: dict(v) for k, v in self._cloud_desc_overrides.items()}

    async def invoke(
        self,
        request: ToolInvocationRequest,
    ) -> ToolInvocationResult:
        handler = self._handlers.get(request.tool_name)
        if handler is None:
            return ToolInvocationResult(
                tool_name=request.tool_name,
                ok=False,
                result=None,
                error_code="chat.tool_not_registered",
                error_message=(
                    f"no handler registered for tool {request.tool_name!r}"
                ),
            )
        try:
            result = await handler(request)
        except Exception as exc:  # noqa: BLE001 -- adapter contract: never raise
            return ToolInvocationResult(
                tool_name=request.tool_name,
                ok=False,
                result=None,
                error_code="chat.tool_handler_failed",
                error_message=(
                    f"tool {request.tool_name!r} handler raised: {exc}"
                ),
            )
        return ToolInvocationResult(
            tool_name=request.tool_name,
            ok=True,
            result=result,
        )
