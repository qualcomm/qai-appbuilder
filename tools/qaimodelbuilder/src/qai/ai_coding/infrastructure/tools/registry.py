# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Build the default tool handler registry for ai_coding (PR-101).

Each registered handler is an ``async`` callable that:

1. delegates security checks to the injected :class:`FileGuardPort`,
2. delegates pre/post processing to the injected :class:`FileBrokerPort`,
3. converts ``ToolGuardDenied`` / ``ToolError`` into structured failure
   dicts so :class:`RegistryBackedToolBridge.invoke` always receives a
   ``dict[str, Any]`` (it would otherwise be wrapped as
   ``ToolBridgeResult(ok=False, error_code="tool_invocation_failed")``).

The handlers returned by :func:`build_default_tool_handlers` match the
:class:`qai.ai_coding.adapters.tool_bridge.ToolHandler` callable
signature, but this module deliberately does NOT import that adapter:
the layered importlinter contract forbids
``qai.ai_coding.infrastructure`` from depending on
``qai.ai_coding.adapters``.  We instead spell the signature inline as
``Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]``.  The
``apps/api/`` layer composes the two by passing the dict into
:meth:`RegistryBackedToolBridge.register` (PR-101 §10).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from qai.ai_coding.application.ports import (
    FileBrokerPort,
    FileGuardPort,
    ToolResultStorePort,
)
from qai.ai_coding.infrastructure.tools.errors import ToolError, ToolGuardDenied
from qai.ai_coding.infrastructure.tools.handlers import (
    TOOL_SCHEMAS,
    tool_apply_patch,
    tool_appbuilder_run,
    tool_edit,
    tool_exec,
    tool_glob,
    tool_grep,
    tool_list,
    tool_read,
    tool_web_search,
    tool_webfetch,
    tool_write,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.process import ProcessRunnerPort
    from qai.platform.scheduling.path_locks import PathLockManager

    from qai.ai_coding.infrastructure.tools.handlers.exec import (
        NativeGuardDenialProbe,
    )

# Spelled inline (cannot import ``qai.ai_coding.adapters.ToolHandler`` here:
# layered contract forbids infrastructure → adapters).
_ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# Result fields that may carry oversized tool output and should be routed
# through the :class:`ToolResultStorePort` (V1 parity: legacy persisted the
# large ``exec`` stdout/stderr so the model could ``read`` it back).  Keyed
# by tool name → ordered tuple of dict fields to consider for persistence.
#
# ONLY tools whose output is UNORDERED / arbitrarily large and has NO native
# pagination belong here (``exec`` stdout/stderr). ``read`` / ``list`` are
# DELIBERATELY EXCLUDED: their output is an ORDERED slice (file lines /
# directory entries) and they already expose ``offset`` pagination as their
# recovery mechanism — they return the WHOLE slice in one call up to their own
# line/byte caps (read: 2000 lines / 100KB) and, only past those caps, append a
# "continue with offset=N" notice. Routing them through the store would re-cut
# a result the tool intentionally returned whole into an 8KB-head + 4KB-tail
# preview (the store threshold is 16KB, far below read's 100KB cap), DROPPING
# the middle — and the persisted file is itself > 16KB so re-reading it just
# gets head+tail-cut again (an unrecoverable loop). ``read``/``list`` recover
# via ``offset`` re-reads, never via disk persistence. ``grep`` persists
# INTERNALLY inside its own handler (it calls the store with ``stored_path``);
# ``glob`` returns a structured file LIST (capped at glob_max_results), not a
# contiguous text blob — so neither belongs here either.
_STORABLE_RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    "exec": ("stdout", "stderr"),
}

__all__ = [
    "TOOL_SCHEMAS",
    "build_default_tool_handlers",
]


_TOOL_FUNCS: dict[str, Any] = {
    "read": tool_read,
    "list": tool_list,
    "write": tool_write,
    "edit": tool_edit,
    "glob": tool_glob,
    "grep": tool_grep,
    "exec": tool_exec,
    "webfetch": tool_webfetch,
    "apply_patch": tool_apply_patch,
    "appbuilder_run": tool_appbuilder_run,
}

# Conditional tool: ``web_search`` is registered ONLY when a ``search_registry``
# is injected at ``build_default_tool_handlers`` time. The registry +
# providers live in ``qai.platform.edition.web_search`` (internal-only,
# physically excluded from external artifacts) and are constructed at the
# apps/api DI seam behind ``settings.is_internal``. On external editions no
# registry is wired → ``web_search`` never enters the returned handler map →
# the tool simply does not exist (same conditional pattern the App Builder
# tools use). Kept OUT of ``_TOOL_FUNCS`` so it is never registered
# unconditionally.
_WEB_SEARCH_TOOL = "web_search"


def _wrap(
    tool_name: str,
    raw: Any,
    *,
    file_guard: FileGuardPort,
    file_broker: FileBrokerPort,
    tool_result_store: ToolResultStorePort | None = None,
    process_runner: "ProcessRunnerPort | None" = None,
    path_lock: "PathLockManager | None" = None,
    search_registry: Any | None = None,
    guard_token_provider: "Callable[[], str | None] | None" = None,
    ask_pending_probe: "Callable[[int], bool] | None" = None,
    native_denial_probe: "NativeGuardDenialProbe | None" = None,
    allow_x86: bool = False,
) -> _ToolHandler:
    """Return a closure satisfying :type:`ToolHandler`.

    The closure captures the injected guard / broker and:

    * runs ``file_broker.pre_call`` (which may mutate args or raise);
    * calls the underlying ``tool_*`` coroutine with ``file_guard``;
    * runs ``file_broker.post_call`` on success;
    * routes oversized output fields through ``tool_result_store`` (when
      injected) so the full body is persisted and the model sees a
      head+tail preview with a ``read(path=...)`` retrieval hint (V1
      parity with ``backend/tool_result_storage.py``);
    * converts ``ToolGuardDenied`` / ``ToolError`` into a stable
      ``{"ok": False, "error_code": ..., "message": ...}`` dict.
    """

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            args = await file_broker.pre_call(tool_name=tool_name, args=args)
        except ToolGuardDenied as exc:
            return {
                "ok": False,
                "error_code": exc.error_code,
                "message": exc.message,
            }

        try:
            # U-004a — the ``exec`` tool runs through the injected
            # ``ProcessRunnerPort`` when one is available (plain
            # ``SubprocessProcessRunner`` after the 2026-07-01 sandbox
            # cleanup — see docs/85-tasks/windows-acl-sandbox-cleanup-
            # 2026-07-01.md), falling back to a bare subprocess otherwise.
            # Other tools take no runner.
            if tool_name == "exec" and process_runner is not None:
                result: Any = await raw(
                    args,
                    file_guard=file_guard,
                    process_runner=process_runner,
                    guard_token_provider=guard_token_provider,
                    ask_pending_probe=ask_pending_probe,
                    native_denial_probe=native_denial_probe,
                    allow_x86=allow_x86,
                )
            elif tool_name == "exec":
                # No runner injected (bare-subprocess fallback path). Still
                # hand the guard-token provider so the raw spawn marks its
                # subtree as guarded (2026-07-06 guard-only reversal).
                result = await raw(
                    args,
                    file_guard=file_guard,
                    guard_token_provider=guard_token_provider,
                    ask_pending_probe=ask_pending_probe,
                    native_denial_probe=native_denial_probe,
                    allow_x86=allow_x86,
                )
            elif tool_name in ("glob", "grep") and tool_result_store is not None:
                # 退化 #11 (subtask 3): pass the store to the search handlers
                # so an oversized full result list (more than the in-prompt
                # preview sample shows) is persisted + retrievable via
                # ``read(path=...)`` — the same落盘 capability ``exec`` has.
                result = await raw(
                    args,
                    file_guard=file_guard,
                    tool_result_store=tool_result_store,
                )
            elif tool_name in ("write", "edit", "apply_patch") and (
                path_lock is not None
            ):
                # PARALLEL-TOOL-1: hand the per-path lock to the write-class
                # tools so two concurrent writers of the SAME file serialise
                # (different files still run in parallel). Other tools take no
                # lock. Mirrors the exec/process_runner differential-injection
                # pattern above.
                result = await raw(
                    args,
                    file_guard=file_guard,
                    path_lock=path_lock,
                )
            elif tool_name == _WEB_SEARCH_TOOL:
                # web_search is a conditional internal-only tool: hand it the
                # injected SearchProviderRegistry (the pluggable backend
                # selector). It is only ever wrapped when a registry was
                # supplied, so ``search_registry`` is non-None here.
                result = await raw(
                    args,
                    file_guard=file_guard,
                    search_registry=search_registry,
                )
            else:
                result = await raw(args, file_guard=file_guard)
        except ToolGuardDenied as exc:
            return {
                "ok": False,
                "error_code": exc.error_code,
                "message": exc.message,
            }
        except ToolError as exc:
            return {
                "ok": False,
                "error_code": f"ai_coding.tool.{tool_name}_error",
                "message": str(exc),
            }

        if not isinstance(result, dict):
            return {
                "ok": False,
                "error_code": f"ai_coding.tool.{tool_name}_invalid_result",
                "message": (
                    f"tool {tool_name!r} returned non-dict result "
                    f"({type(result).__name__})"
                ),
            }

        # Only run post hook on successful results — broker truncation
        # is not meaningful on failure envelopes.
        if result.get("ok"):
            try:
                result = await file_broker.post_call(
                    tool_name=tool_name, result=result
                )
            except ToolGuardDenied as exc:
                return {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": exc.message,
                }

        # Persist + preview oversized output fields (V1 parity).  Applied
        # to both success and error envelopes because a failed ``exec``
        # (non-zero exit, timeout) can still emit a huge stdout/stderr the
        # model may need to ``read`` back.
        if tool_result_store is not None:
            return _apply_result_store(
                tool_name, result, store=tool_result_store
            )
        # ``result`` is a dict here (guarded by the isinstance check above);
        # the annotation is ``Any`` only because ``raw``'s return type is.
        return cast("dict[str, Any]", result)

    return _handler


def _apply_result_store(
    tool_name: str,
    result: dict[str, Any],
    *,
    store: ToolResultStorePort,
) -> dict[str, Any]:
    """Route oversized text fields of ``result`` through ``store``.

    For each configured field (see :data:`_STORABLE_RESULT_FIELDS`) whose
    value is a string above the store's threshold, replace it with the
    store's head+tail preview (which embeds the persisted file path +
    retrieval hint).  Mutates a shallow copy so the caller's dict is not
    aliased.  No-op for tools with no storable fields.
    """
    fields = _STORABLE_RESULT_FIELDS.get(tool_name)
    if not fields:
        return result

    updated: dict[str, Any] | None = None
    for field in fields:
        value = result.get(field)
        if not isinstance(value, str) or not value:
            continue
        preview = store.store(
            value, tool_name=tool_name, context_hint=field
        )
        if not preview.truncated:
            continue
        if updated is None:
            updated = dict(result)
        updated[field] = preview.preview
    return updated if updated is not None else result


def build_default_tool_handlers(
    *,
    file_guard: FileGuardPort,
    file_broker: FileBrokerPort,
    tool_result_store: ToolResultStorePort | None = None,
    process_runner: "ProcessRunnerPort | None" = None,
    path_lock: "PathLockManager | None" = None,
    search_registry: Any | None = None,
    guard_token_provider: "Callable[[], str | None] | None" = None,
    ask_pending_probe: "Callable[[int], bool] | None" = None,
    native_denial_probe: "NativeGuardDenialProbe | None" = None,
    allow_x86: bool = False,
) -> dict[str, _ToolHandler]:
    """Return the canonical ``{tool_name: ToolHandler}`` mapping.

    The application root (``apps/api/_ai_coding_di.py``) feeds the
    returned dict into :class:`RegistryBackedToolBridge` at wiring
    time.  Tests building a bridge with custom adapters should call
    this same factory so the wrapping behaviour stays in sync with the
    production wiring.

    ``tool_result_store`` is optional: when supplied, oversized output
    fields (see :data:`_STORABLE_RESULT_FIELDS`) are persisted and the
    model is shown a head+tail preview with a ``read(path=...)``
    retrieval hint (V1 parity).  When ``None`` the handlers behave
    exactly as before (the underlying tool's own hard truncation
    applies).

    ``search_registry`` is optional and gates the conditional internal-only
    ``web_search`` tool: when a registry is supplied (apps/api DI on an
    internal edition, behind ``settings.is_internal``), ``web_search`` is
    added to the returned map and wired to that registry. When ``None``
    (external edition / no search backend), ``web_search`` is NOT registered
    and the tool does not exist — mirroring how the App Builder tools are
    conditionally wired. This is layer-1 of the four-layer edition defence.

    ``native_denial_probe`` (D2-B) is optional. When wired by the apps
    composition root (which pre-composes ``AuditQueryPort`` +
    :func:`build_native_guard_denial_note` — layer-crossing is only legal
    there), a non-zero exit from ``exec`` triggers an audit query scoped to
    the child's pid tree and prepends any recovered native FileGuard denial
    rows to ``exit_diagnostics``. When ``None`` (fail-open) the handler
    behaves exactly as before D2-B (the D1 keyword-hint from
    ``exec_diagnostics`` is still emitted).
    """
    funcs: dict[str, Any] = dict(_TOOL_FUNCS)
    if search_registry is not None:
        funcs[_WEB_SEARCH_TOOL] = tool_web_search
    return {
        name: _wrap(
            name,
            func,
            file_guard=file_guard,
            file_broker=file_broker,
            tool_result_store=tool_result_store,
            process_runner=process_runner,
            path_lock=path_lock,
            search_registry=search_registry,
            guard_token_provider=guard_token_provider,
            ask_pending_probe=ask_pending_probe,
            native_denial_probe=native_denial_probe,
            allow_x86=allow_x86,
        )
        for name, func in funcs.items()
    }
