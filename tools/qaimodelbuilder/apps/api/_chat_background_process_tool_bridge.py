# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Cross-context tool bridge: wire the platform ``background_process`` tool into chat.

Registers the ``background_process`` tool onto the chat-side
:class:`RegistryBackedToolInvocation` registry so an LLM-emitted ``tool_call``
for ``background_process`` resolves to a real call into
:class:`~qai.platform.background_process.ports.BackgroundProcessManagerPort`
(via :func:`~qai.platform.background_process.tool_handlers.handle_background_process`)
instead of falling through to ``chat.tool_not_registered``.

Why a separate bridge (mirrors ``_chat_web_search_tool_bridge``)
----------------------------------------------------------------
``background_process`` is a *platform* capability (not bound to any bounded
context) whose lifecycle handle (the manager) is built late in
``_build_contexts`` (tail of phase 2 in ``apps/api/di.py``). This dedicated
post-build hook registers the schema + handler onto the chat tool registry
once the manager is wired — exactly like ``register_web_search_tool_into_chat``
adds the conditional web-search handler.

Cross-context discipline
------------------------
Lives in ``apps/api`` (the only layer allowed to compose contexts):

* the handler from :mod:`qai.platform.background_process.tool_handlers`
  speaks only to ``BackgroundProcessManagerPort`` (a Protocol),
* the schema from :mod:`qai.platform.background_process.tool_schemas` is a
  pure dict,
* the chat side gets a single ``async`` callable typed against
  :class:`~qai.chat.application.ports.ToolInvocationRequest`,

so neither ``qai.chat`` nor ``qai.platform.background_process`` learns about
the other (``qai.** -> qai.platform.**`` is the only cross-package edge
permitted by ``.importlinter`` ``context-isolation``).

Session isolation
-----------------
The manager-side ``list`` is filtered by ``session_id`` so an LLM only sees
processes it spawned in the current conversation. We use
``ConversationId.value`` (the wrapped str) as the session id — stable across
the conversation's lifetime, distinct between conversations, no PII.
"""

from __future__ import annotations

from typing import Any

from apps.api._chat_tool_result_render import render_tool_result_text_with_hints
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest
from qai.platform.background_process.ports import (
    BackgroundProcessManagerPort,
)
from qai.platform.background_process.tool_handlers import (
    handle_background_process,
)
from qai.platform.background_process.tool_schemas import (
    BACKGROUND_PROCESS_TOOL_SCHEMA,
)
from qai.platform.logging import get_logger

__all__ = ["register_background_process_tool_into_chat"]

_log = get_logger(__name__)


def register_background_process_tool_into_chat(
    *,
    tools: Any,
    manager: BackgroundProcessManagerPort,
    file_guard: Any | None = None,
) -> tuple[str, ...]:
    """Register ``background_process`` on the chat registry, backed by ``manager``.

    Returns the names registered (``("background_process",)`` on success,
    ``()`` when the tools port is not the registry-backed adapter or the
    registration itself fails). Best-effort: never blocks chat startup.

    ``file_guard`` (2026-07-09) is the SAME ``FileGuardPort`` the ``exec`` tool
    uses. ``background_process`` spawns commands too, but historically did NO
    command-level authorization — so an LLM could run via ``background_process
    start`` a command that ``exec``'s ``enforce_exec`` would DENY/ASK (they are
    peer execution tools in the system prompt). This closes that bypass: a
    ``start`` runs the SAME pre-spawn checks as ``exec`` (protected-write
    sentinel + ``file_guard.enforce_exec`` = exec_broker args / PolicyCenter /
    net-and-lolbins ASK / program grants). ``None`` (guard off / not wired) is
    graceful — the check is skipped (allow-all), matching exec's no-guard
    behaviour. Other actions (list/status/logs/stop/restart) do NOT spawn a
    NEW command (restart re-launches an already-authorized process by id), so
    only ``start`` is gated.

    ``tools`` is typed ``Any`` rather than
    :class:`RegistryBackedToolInvocation` so callers (the wire hook in
    ``_background_process_di.py``) can pass ``chat.tools`` (typed as the
    application-level ``ToolInvocationPort`` Protocol) without an extra
    cast; we do the ``isinstance`` check here.
    """
    if not isinstance(tools, RegistryBackedToolInvocation):
        return ()

    async def _guard_command(
        command: str, cwd: str | None
    ) -> dict[str, Any] | None:
        """Run the command-level pre-spawn checks (parity with exec).

        Returns a tool-result dict when DENIED (caller surfaces it as tool text
        instead of spawning), or ``None`` to proceed. ASK blocks here
        (``enforce_exec`` awaits the approval dialog) before any spawn — no
        conflict with the manager's readiness window (which only starts after
        spawn). Never raises out: a guard glitch degrades to proceed (allow),
        matching exec's graceful no-guard path.
        """
        if file_guard is None or not command.strip():
            return None
        # Always-on protected-write pre-check (same order as exec: sentinel
        # first, then enforce_exec).
        try:
            from qai.ai_coding.infrastructure.tools.handlers._protected_command_guard import (  # noqa: E501
                protected_command_sentinel,
            )

            deny_reason = protected_command_sentinel(command)
        except Exception:  # noqa: BLE001 — pre-check must not break the tool
            deny_reason = None
        if deny_reason:
            return {
                "ok": False,
                "error_code": "ai_coding.tool.protected_write",
                "message": deny_reason,
            }
        # exec_broker / PolicyCenter / net-and-lolbins / program-grant chain.
        try:
            from qai.ai_coding.infrastructure.tools.errors import ToolGuardDenied

            await file_guard.enforce_exec(
                command=command,
                cwd=cwd,
                caller="chat.tool.background_process",
            )
        except ToolGuardDenied as exc:
            return {
                "ok": False,
                "error_code": getattr(
                    exc, "error_code", "ai_coding.tool.exec_denied"
                ),
                "message": getattr(exc, "message", str(exc)),
            }
        return None

    async def _enforce_action_guard(
        args: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Gate the spawning actions (``start`` AND ``restart``) — parity with
        exec's command-level authorization.

        ``start`` guards the LLM-supplied ``command``. ``restart`` RE-SPAWNS an
        existing process, so it must re-authorize too (a prior ``once`` grant is
        gone; policy may have tightened; the first start may have run with no
        guard). ``restart`` carries only an ``id``, so we read the stored
        command back via ``manager.get(id)`` and guard THAT. Non-spawning
        actions (list/status/logs/stop) don't run a command → no check.
        """
        if file_guard is None:
            return None
        action = (args.get("action") or "").strip().lower()
        if action == "start":
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return None  # handler schema validation will reject it
            cwd = args.get("workdir")
            cwd = cwd if isinstance(cwd, str) and cwd.strip() else None
            return await _guard_command(command, cwd)
        if action == "restart":
            proc_id = args.get("id")
            if not isinstance(proc_id, str) or not proc_id.strip():
                return None  # handler will reject a missing id
            try:
                info = await manager.get(proc_id)
            except Exception:  # noqa: BLE001 — lookup glitch → let handler handle
                return None
            if info is None:
                return None  # not found → handler returns _not_found
            cmd = getattr(info, "command", None)
            cwd = getattr(info, "cwd", None)
            if not isinstance(cmd, str) or not cmd.strip():
                return None
            cwd = cwd if isinstance(cwd, str) and cwd.strip() else None
            return await _guard_command(cmd, cwd)
        return None

    async def _chat_background_process(request: ToolInvocationRequest) -> Any:
        # ConversationId is a frozen dataclass wrapping a single ``value: str``
        # field (``src/qai/chat/domain/ids.py``); use ``.value`` to make the
        # str conversion explicit (``str(cid)`` also works via ``__str__`` but
        # ``.value`` documents the intent and avoids accidental ``repr`` if
        # the VO ever grows a custom ``__str__``).
        session_id = request.conversation_id.value
        args = dict(request.arguments or {})
        # 2026-07-09 — command-level authorization parity with exec, BEFORE
        # the manager spawns anything. Gates start AND restart (both spawn a
        # command); a DENY short-circuits to tool text.
        denied = await _enforce_action_guard(args)
        if denied is not None:
            return render_tool_result_text_with_hints(denied)
        # 2026-07-09 — opt the LLM-spawned child into native FileGuard
        # protection. This internal flag is read by tool_handlers and passed
        # to StartInput.inject_file_guard. Only the LLM tool-call path sets
        # it; all other callers (App Builder, HTTP operator) leave it False
        # so their child processes are never subject to the native hook.
        if args.get("action", "").strip().lower() == "start":
            args["_inject_file_guard"] = True
        result = await handle_background_process(
            args,
            manager=manager,
            session_id=session_id,
        )
        # Project the structured dict into V1-style plain text BEFORE it reaches
        # the chat use case — exactly like ``_chat_tool_bridge._wrap_ai_coding_handler``
        # does for the ai_coding tools. Without this the dict falls through to
        # ``streaming.py``'s ``str(raw_result)`` fallback and the LLM sees a
        # Python dict repr (``{'ok': True, 'processes': []}``) instead of the
        # human-readable rendering ``_chat_tool_result_render._render_background_process``
        # produces (the reported "LLM 解读 dict repr" bug). The renderer already
        # knows the background_process result shapes (processes / process /
        # bgp-prefixed logs) and preserves the ``[tool_error]`` sentinel + the
        # ``bgp_...`` id token the chat card salvages, so no downstream contract
        # is affected.
        return render_tool_result_text_with_hints(result)

    try:
        tools.register(
            "background_process",
            _chat_background_process,
            schema=BACKGROUND_PROCESS_TOOL_SCHEMA,
        )
    except Exception:  # noqa: BLE001 — never block chat startup
        _log.warning(
            "chat.background_process_tool.register_failed",
            exc_info=True,
        )
        return ()
    return ("background_process",)
