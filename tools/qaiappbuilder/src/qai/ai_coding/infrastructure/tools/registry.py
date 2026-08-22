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

import time
from collections.abc import Awaitable, Callable
from enum import IntEnum
from typing import TYPE_CHECKING, Any, cast

from qai.ai_coding.application.ports import (
    FileBrokerPort,
    FileGuardPort,
    ToolResultStorePort,
)
from qai.ai_coding.infrastructure.tools import _tool_call_log
from qai.ai_coding.infrastructure.tools.errors import ToolError, ToolGuardDenied
from qai.ai_coding.infrastructure.tools.handlers import (
    TOOL_SCHEMAS,
    tool_appbuilder_run,
    tool_apply_patch,
    tool_ast_edit,
    tool_ast_grep,
    tool_browser,
    tool_edit,
    tool_exec,
    tool_glob,
    tool_grep,
    tool_list,
    tool_read,
    tool_run_code,
    tool_scan_secrets,
    tool_web_fetch,
    tool_web_search,
    tool_write,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.ai_coding.infrastructure.tools.handlers.exec import (
        NativeGuardDenialProbe,
    )
    from qai.platform.process import ProcessRunnerPort
    from qai.platform.scheduling.path_locks import PathLockManager

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
    "ToolTier",
    "build_default_tool_handlers",
    "get_tool_tier",
]


class ToolTier(IntEnum):
    """Tool admission tiers (lowest footprint first).

    Every tool MUST declare its tier at registration time.
    Higher tiers require stronger justification.
    """

    BUILTIN = 0  # Extends existing tool — zero new schema surface
    SKILL_GATED = 1  # Only active when a skill activates it
    SERVICE_GATED = 2  # Only active when a service/config is present
    CORE = 3  # Always advertised to LLM — highest cost


# Tier assignment for ALL tools advertised to the LLM. Default is CORE
# (backward compat). Tools from multiple registration sources are listed
# together here as the SINGLE source of truth for footprint governance.
#
# CORE tools are always (or near-always) visible to the LLM on every call.
# SERVICE_GATED tools only appear when their backing service is configured
# (e.g. App Builder model loaded → appbuilder_run/appbuilder_batch_run).
#
# ``web_search`` is CORE despite conditional registration: the condition is
# an EDITION gate (internal vs. external), not a user-configurable service.
_TOOL_TIERS: dict[str, ToolTier] = {
    # ── 文件读取 ─────────────────────────────────────────────────────────
    "read": ToolTier.CORE,
    "list": ToolTier.CORE,
    "glob": ToolTier.CORE,
    "grep": ToolTier.CORE,
    "scan_secrets": ToolTier.CORE,
    "ast_grep": ToolTier.CORE,
    "web_fetch": ToolTier.CORE,
    "web_search": ToolTier.CORE,
    # ── 文件编辑 ─────────────────────────────────────────────────────────
    "write": ToolTier.CORE,
    "edit": ToolTier.CORE,
    "apply_patch": ToolTier.CORE,
    "ast_edit": ToolTier.CORE,
    # ── 命令执行 (exec = 前台, background_process = 后台) ────────────────
    "exec": ToolTier.CORE,
    "background_process": ToolTier.CORE,
    "run_code": ToolTier.CORE,
    "scheduled_task": ToolTier.CORE,
    # ── 子代理 (agent = spawn, sub_agent = 管理已有) ────────────────
    "agent": ToolTier.CORE,
    "sub_agent": ToolTier.CORE,
    # ── 会话辅助 ─────────────────────────────────────────────────────────
    "todowrite": ToolTier.CORE,
    "question": ToolTier.CORE,
    "skill": ToolTier.CORE,
    "search_conversations": ToolTier.CORE,
    # ── 条件工具 (SERVICE_GATED — 仅 App Builder 配置时注入) ─────────────
    "appbuilder_run": ToolTier.SERVICE_GATED,
    "appbuilder_batch_run": ToolTier.SERVICE_GATED,
    # ── 条件工具 (SERVICE_GATED — 仅 Playwright 安装 + [browser] 配置时注入) ─
    "browser": ToolTier.SERVICE_GATED,
}


def get_tool_tier(tool_name: str) -> ToolTier:
    """Return the declared tier for *tool_name*, defaulting to CORE."""
    return _TOOL_TIERS.get(tool_name, ToolTier.CORE)

_TOOL_FUNCS: dict[str, Any] = {
    "read": tool_read,
    "list": tool_list,
    "write": tool_write,
    "edit": tool_edit,
    "glob": tool_glob,
    "grep": tool_grep,
    "scan_secrets": tool_scan_secrets,
    "exec": tool_exec,
    "run_code": tool_run_code,
    "web_fetch": tool_web_fetch,
    "apply_patch": tool_apply_patch,
    "appbuilder_run": tool_appbuilder_run,
    "ast_grep": tool_ast_grep,
    "ast_edit": tool_ast_edit,
}

# Conditional tool: ``web_search`` is registered ONLY when a ``search_registry``
# is injected at ``build_default_tool_handlers`` time. The registry + the
# multi-engine ``web`` provider live in the shared-kernel package
# ``qai.platform.web_search`` and ship to BOTH editions, so the apps/api DI
# seam builds a non-empty registry (at least ``web``) internally AND externally
# → ``web_search`` is now available under both editions. (The intranet
# ``cebot`` provider remains internal-only, added to the same registry only
# when ``settings.is_internal``.) The conditional wiring is unchanged: if no
# registry is injected (extreme construction failure), ``web_search`` never
# enters the returned handler map and the tool simply does not exist. Kept OUT
# of ``_TOOL_FUNCS`` so it is never registered unconditionally.
_WEB_SEARCH_TOOL = "web_search"

# ``run_code`` executes Python in a persistent interpreter owned by this
# process. Named here so the differential-injection branch above (which hands
# it the result store but NOT the file guard) has a single spelling.
_RUN_CODE_TOOL = "run_code"

# Conditional tool: ``browser`` is registered ONLY when a ``browser_manager``
# is injected at ``build_default_tool_handlers`` time (apps/api builds one when
# Playwright is importable and the ``[browser]`` config enables it). Kept OUT of
# ``_TOOL_FUNCS`` so it is never registered unconditionally — a build without
# Playwright simply never sees the tool.
_BROWSER_TOOL = "browser"


def _wrap(
    tool_name: str,
    raw: Any,
    *,
    file_guard: FileGuardPort,
    file_broker: FileBrokerPort,
    tool_result_store: ToolResultStorePort | None = None,
    process_runner: ProcessRunnerPort | None = None,
    path_lock: PathLockManager | None = None,
    search_registry: Any | None = None,
    browser_manager: Any | None = None,
    guard_token_provider: Callable[[], str | None] | None = None,
    ask_pending_probe: Callable[[int], bool] | None = None,
    ask_flush_for_pid: Callable[[int], Awaitable[list[str]]] | None = None,
    native_denial_probe: NativeGuardDenialProbe | None = None,
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
            # Route the deny message through the shared FileGuard hint helper
            # (2026-07-23) as ``confirmed_fileguard``: catching
            # ``ToolGuardDenied`` here means FileGuard (or FileBroker's
            # security-policy guard) raised it — the source is verified, so
            # the strong ``[FileGuard]`` V2 guidance is warranted. The helper
            # lives in the same context (``qai.ai_coding.infrastructure.
            # tools.handlers.exec_diagnostics``) so this stays inside the
            # context-isolation import-linter contract. Idempotent: skipped
            # when guidance is already present in the message.
            from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
                append_fileguard_hint_if_denial,
            )
            return {
                "ok": False,
                "error_code": exc.error_code,
                "message": append_fileguard_hint_if_denial(
                    exc.message, attribution="confirmed_fileguard"
                ),
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
                    ask_flush_for_pid=ask_flush_for_pid,
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
                    ask_flush_for_pid=ask_flush_for_pid,
                    native_denial_probe=native_denial_probe,
                    allow_x86=allow_x86,
                )
            elif tool_name in ("glob", "grep", "scan_secrets", "ast_grep") and (
                tool_result_store is not None
            ):
                # 退化 #11 (subtask 3): pass the store to the search handlers
                # so an oversized full result list (more than the in-prompt
                # preview sample shows) is persisted + retrievable via
                # ``read(path=...)`` — the same落盘 capability ``exec`` has.
                # ``scan_secrets`` shares the shape exactly: same read-only
                # file-guard need, same "findings beyond the cap must stay
                # reachable" contract.
                result = await raw(
                    args,
                    file_guard=file_guard,
                    tool_result_store=tool_result_store,
                )
            elif tool_name == "ast_edit":
                # ``ast_edit`` needs BOTH extras, so it gets its own branch:
                # the result store (a wide structural rewrite can preview far
                # more changes than fit in-prompt, and an elided change must
                # stay retrievable via ``read(path=...)``) AND the per-path
                # lock — it is a WRITE tool whose target files are only known
                # after its dry-run, so it locks them itself once discovered
                # (``path_lock`` may be None, which the handler treats as "no
                # lock wired", exactly like write/edit/apply_patch).
                result = await raw(
                    args,
                    file_guard=file_guard,
                    tool_result_store=tool_result_store,
                    path_lock=path_lock,
                )
            elif tool_name == _RUN_CODE_TOOL:
                # run_code executes in its own long-lived interpreter, so it
                # takes no ``file_guard`` (it performs no path operation of its
                # own) — only the store, for an oversized cell body.
                result = await raw(args, tool_result_store=tool_result_store)
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
                # web_search is a conditional tool (both editions): hand it the
                # injected SearchProviderRegistry (the pluggable backend
                # selector). It is only ever wrapped when a registry was
                # supplied, so ``search_registry`` is non-None here.
                # L-Py-5 (2026-07-28): ``file_guard`` removed — ``web_search``
                # performs no filesystem access.
                result = await raw(
                    args,
                    search_registry=search_registry,
                )
            elif tool_name == _BROWSER_TOOL:
                # browser is a conditional tool: hand it the injected
                # BrowserSessionManager. Only ever wrapped when a manager was
                # supplied, so ``browser_manager`` is non-None here. No
                # file_guard — the tool drives the browser only, never the
                # host filesystem.
                result = await raw(
                    args,
                    browser_manager=browser_manager,
                )
            elif tool_name == "web_fetch":
                # L-Py-5 (2026-07-28): ``web_fetch`` performs no filesystem
                # access — URL policy is enforced at the harness layer, so
                # ``file_guard`` is no longer forwarded (nor accepted by the
                # handler). Split from the ``else`` catch-all so the wiring
                # is explicit / grep-able.
                result = await raw(args)
            else:
                result = await raw(args, file_guard=file_guard)
        except ToolGuardDenied as exc:
            # Confirmed FileGuard denial — see the pre-call twin above.
            from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
                append_fileguard_hint_if_denial,
            )
            return {
                "ok": False,
                "error_code": exc.error_code,
                "message": append_fileguard_hint_if_denial(
                    exc.message, attribution="confirmed_fileguard"
                ),
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
                # Confirmed FileGuard denial — see the pre-call twin above.
                from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
                    append_fileguard_hint_if_denial,
                )
                return {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": append_fileguard_hint_if_denial(
                        exc.message, attribution="confirmed_fileguard"
                    ),
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

    async def _logged(args: dict[str, Any]) -> dict[str, Any]:
        """Log the call, then delegate. See :mod:`._tool_call_log`.

        Wrapping OUTSIDE ``_handler`` is deliberate: it observes every exit —
        the guard-denied dicts built early, the stored-result path, and any
        exception — without threading a log call through 190 lines of branches,
        where one forgotten path would silently record nothing.
        """
        started = time.perf_counter()
        try:
            outcome = await _handler(args)
        except BaseException as exc:  # noqa: BLE001 — observed, then re-raised
            _tool_call_log.record(
                tool=tool_name,
                args=args,
                error=exc,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        _tool_call_log.record(
            tool=tool_name,
            args=args,
            result=outcome,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return outcome

    return _logged


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
    process_runner: ProcessRunnerPort | None = None,
    path_lock: PathLockManager | None = None,
    search_registry: Any | None = None,
    browser_manager: Any | None = None,
    guard_token_provider: Callable[[], str | None] | None = None,
    ask_pending_probe: Callable[[int], bool] | None = None,
    ask_flush_for_pid: Callable[[int], Awaitable[list[str]]] | None = None,
    native_denial_probe: NativeGuardDenialProbe | None = None,
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

    ``search_registry`` is optional and gates the conditional ``web_search``
    tool: when a registry is supplied (apps/api DI builds it on both editions —
    the ``web`` engine roster ships to both; ``cebot`` is added only behind
    ``settings.is_internal``), ``web_search`` is added to the returned map and
    wired to that registry. When it is ``None`` (extreme construction failure),
    ``web_search`` is NOT registered and the tool does not exist — mirroring how
    the App Builder tools are conditionally wired.

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
    if browser_manager is not None:
        funcs[_BROWSER_TOOL] = tool_browser
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
            browser_manager=browser_manager,
            guard_token_provider=guard_token_provider,
            ask_pending_probe=ask_pending_probe,
            ask_flush_for_pid=ask_flush_for_pid,
            native_denial_probe=native_denial_probe,
            allow_x86=allow_x86,
        )
        for name, func in funcs.items()
    }
