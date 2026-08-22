# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Cross-context tool bridge: wire the platform ``computer`` tool into chat.

Registers the ``computer`` tool onto the chat-side
:class:`RegistryBackedToolInvocation` registry so an LLM-emitted
``tool_call`` for ``computer`` resolves to a real call into
:class:`~qai.platform.computer.ports.DesktopControllerPort` (via
:func:`~qai.platform.computer.tool_handlers.handle_computer`) instead of
falling through to ``chat.tool_not_registered``.

Structure mirrors ``_chat_background_process_tool_bridge``: a dedicated
post-build hook registers the schema + handler once the controller
factory is available.

Cross-context discipline
------------------------
Lives in ``apps/api`` (the only layer allowed to compose contexts):

* the handler from :mod:`qai.platform.computer.tool_handlers` speaks only
  to ``DesktopControllerPort`` (a Protocol) + an injected screenshot
  saver callable,
* the schema from :mod:`qai.platform.computer.tool_schemas` is a pure
  dict,
* the chat side gets a single ``async`` callable typed against
  :class:`~qai.chat.application.ports.ToolInvocationRequest`,

so neither ``qai.chat`` nor ``qai.platform.computer`` learns about the
other.

Session isolation
-----------------
A desktop controller is built lazily PER conversation owner and cached;
``ConversationId.value`` is the owner id (stable across the
conversation, distinct between conversations, no PII). Controllers are
tracked in the platform owner registry so a conversation teardown can
release its worker subprocess.

Authorization reuse
-------------------
``computer_approval`` classifies each batch: a pure ``screenshot`` /
``wait`` batch is ``"read"`` (auto); any input action is ``"exec"`` and
runs the SAME ``file_guard.enforce_exec`` path the ``exec`` /
``background_process`` tools use. ``file_guard=None`` (guard off) is a
graceful allow-all, matching exec's no-guard behaviour.
"""

from __future__ import annotations

from typing import Any

from apps.api._chat_tool_result_render import render_tool_result_text_with_hints
from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest
from qai.platform.computer.ports import DesktopControllerPort
from qai.platform.computer.tool_handlers import (
    computer_approval,
    format_approval_details,
    handle_computer,
)
from qai.platform.computer.tool_schemas import COMPUTER_TOOL_SCHEMA
from qai.platform.logging import get_logger

__all__ = ["register_computer_tool_into_chat"]

_log = get_logger(__name__)


def register_computer_tool_into_chat(
    *,
    tools: Any,
    controller_for_owner: Any,
    save_screenshot: Any | None = None,
    file_guard: Any | None = None,
) -> tuple[str, ...]:
    """Register ``computer`` on the chat registry.

    Args:
        tools: the chat tools port; must be a
            :class:`RegistryBackedToolInvocation` or registration no-ops.
        controller_for_owner: ``Callable[[str], DesktopControllerPort]``
            returning (lazily building + owner-registering) the controller
            for a conversation owner id.
        save_screenshot: ``Callable[[str, str, str], Awaitable[str|None]]``
            persisting the PNG and returning a fetch URL (``None`` → the
            model sees only the text summary).
        file_guard: the same ``FileGuardPort`` the ``exec`` tool uses;
            ``None`` = guard off (allow-all), matching exec.

    Returns:
        ``("computer",)`` on success, ``()`` on a graceful no-op.
    """
    if not isinstance(tools, RegistryBackedToolInvocation):
        return ()

    async def _enforce_input_guard(args: dict[str, Any]) -> dict[str, Any] | None:
        """Authorize an ``exec``-class (input) batch; ``None`` to proceed.

        Read-only batches (screenshot/wait) skip the guard. Input batches
        run ``file_guard.enforce_exec`` with a synthetic command line that
        renders the batch, so the SAME PolicyCenter / ASK path the exec
        tool uses gates desktop input. A DENY returns a tool-result dict.
        """
        if computer_approval(args) != "exec":
            return None
        if file_guard is None:
            return None
        details = format_approval_details(args)
        synthetic = "computer: " + "; ".join(details)
        try:
            from qai.ai_coding.infrastructure.tools.errors import ToolGuardDenied

            await file_guard.enforce_exec(
                command=synthetic,
                cwd=None,
                caller="chat.tool.computer",
            )
        except ToolGuardDenied as exc:
            return {
                "ok": False,
                "error_code": getattr(exc, "error_code", "ai_coding.tool.exec_denied"),
                "message": getattr(exc, "message", str(exc)),
            }
        except Exception:  # noqa: BLE001 — a guard glitch degrades to allow
            return None
        return None

    async def _chat_computer(request: ToolInvocationRequest) -> Any:
        owner_id = request.conversation_id.value
        args = dict(request.arguments or {})
        _actions = args.get("actions")
        _kinds = (
            [a.get("type") for a in _actions if isinstance(a, dict)]
            if isinstance(_actions, list)
            else "screenshot(default)"
        )
        # Surface the actual keyboard payload (keypress chords / type text) so a
        # spurious system overlay (e.g. Win+W Ink workspace, Win+Shift+S) can be
        # traced to the exact key the model sent. The computer tool cannot draw
        # on screen itself — such overlays are Windows/OEM features triggered by
        # a global shortcut, so the key log is the smoking gun.
        _keys: list[Any] = []
        if isinstance(_actions, list):
            for a in _actions:
                if not isinstance(a, dict):
                    continue
                if a.get("type") == "keypress" and a.get("keys"):
                    _keys.append({"keypress": a.get("keys")})
                elif a.get("type") == "type" and a.get("text"):
                    _txt = str(a.get("text"))
                    _keys.append({"type": _txt[:40]})
                elif a.get("keys"):  # modifier keys on click/move/etc.
                    _keys.append({a.get("type"): a.get("keys")})
        _log.info(
            "chat.computer_tool.invoke",
            owner_id=owner_id,
            action_count=len(_actions) if isinstance(_actions, list) else 0,
            kinds=_kinds,
            keys=_keys or None,
            approval=computer_approval(args),
            audience=args.get("audience") or "both(default)",
        )
        denied = await _enforce_input_guard(args)
        if denied is not None:
            _log.warning("chat.computer_tool.denied", owner_id=owner_id, reason=denied.get("error_code"))
            return render_tool_result_text_with_hints(denied)
        try:
            controller = controller_for_owner(owner_id)
        except Exception:  # noqa: BLE001
            _log.warning("chat.computer_tool.controller_build_failed", exc_info=True)
            return render_tool_result_text_with_hints(
                {
                    "ok": False,
                    "error_code": "computer.controller_unavailable",
                    "message": "desktop controller could not be started",
                }
            )
        result = await handle_computer(
            args,
            controller=controller,
            save_screenshot=save_screenshot,
            conversation_id=owner_id,
            message_id=request.tab_id.value if request.tab_id else "screenshot",
        )
        _log.info(
            "chat.computer_tool.result",
            owner_id=owner_id,
            ok=result.get("ok"),
            error_code=result.get("error_code"),
        )
        return render_tool_result_text_with_hints(result)

    try:
        tools.register("computer", _chat_computer, schema=COMPUTER_TOOL_SCHEMA)
    except Exception:  # noqa: BLE001 — never block chat startup
        _log.warning("chat.computer_tool.register_failed", exc_info=True)
        return ()
    return ("computer",)


# Re-export the port for callers building a controller factory.
_ = DesktopControllerPort
