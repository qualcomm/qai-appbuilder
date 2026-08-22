# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Tool progress emitter — a per-turn context any slow tool can call to
publish a keep-alive ``tool_result(partial=True)`` frame.

Design
------
The tool handler contract is ``async def handler(args) -> dict[str, Any]``:
a single-return coroutine with no per-tool streaming callback. Retrofitting
per-tool streaming would touch the entire tool_router / tool_executor /
kernel chain — big surface area for marginal UX gain, because most tools are
fast in the common case and the queued-inject + fresh-turn-on-completion +
hard-cancel + auto-park mechanisms already deliver the core "user can send a
new message" UX.

This module is the LIGHTWEIGHT alternative: an opt-in ContextVar-backed
emitter. A tool that knows it is about to do slow I/O calls
:func:`emit_progress` with a short status string; the emitter (installed by
the streaming loop's tool-round scope) forwards it to the wire as a
``tool_result(partial=True, delta=status)`` frame so the frontend's tool card
shows a live "working…" status without any change to the tool handler
signature or the kernel contract.

When no emitter is installed (unit tests / non-chat contexts) the call is a
cheap no-op.

Why ``qai.platform``
--------------------
The two sides of this contract live in DIFFERENT bounded contexts: the
PRODUCERS are the ``qai.ai_coding`` tool handlers (read / write / glob /
grep / web_fetch / web_search / apply_patch), and the CONSUMER is the chat
streaming loop (``qai.chat.application.use_cases.streaming``), which installs
the emitter that pushes frames onto the tab's SSE stream. ``.importlinter``'s
``context-isolation`` contract forbids a direct cross-context import and
whitelists only ``qai.platform.**``, so this shared cross-cutting capability
belongs here — the same reasoning (and the same resolution) as
:mod:`qai.platform.exec_race`.

Why ``reset_tool_progress_emitter`` is public API
------------------------------------------------
Installing returns a ``Token`` and the caller must restore the prior value on
turn end. Exposing only ``set_...`` forces the consumer to reach for the
module's ``ContextVar`` handle directly to call ``.reset(token)`` — which is
exactly what the chat streaming loop used to do, importing a private
underscore-prefixed name across a context boundary. A consumer must NEVER
touch the ContextVar private handle: the variable is this module's
implementation detail, and its identity/lifetime is not part of the contract.
:func:`reset_tool_progress_emitter` closes that hole — install and restore are
now a symmetric pair of public functions, and the ContextVar never leaves this
module. Do NOT re-expose it.

Usage
-----
::

    from qai.platform.tool_progress import emit_progress

    async def tool_web_search(args, *, search_registry):
        emit_progress("searching web…")
        result = await search_registry.execute(args)
        return {"results": result}
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token

from qai.platform.logging import get_logger


__all__ = [
    "emit_progress",
    "ToolProgressEmitter",
    "set_tool_progress_emitter",
    "reset_tool_progress_emitter",
]


_log = get_logger(__name__)


#: Signature of an installed emitter.  Takes a short status text and
#: (optionally) an ``event_kind`` classifier ("network", "match", "bytes",
#: …).  Never raises — installations must swallow errors internally.
ToolProgressEmitter = Callable[[str, str | None], None]


_CTX_VAR: ContextVar[ToolProgressEmitter | None] = ContextVar(
    "tool_progress_emitter", default=None,
)


def emit_progress(status: str, event_kind: str | None = None) -> None:
    """Emit a keep-alive progress signal from within a tool handler.

    Safe to call outside a chat turn: falls back to no-op when no
    emitter is installed.  Never raises; a failing emitter is logged and
    swallowed so a tool handler never fails because of observability.
    """
    if not isinstance(status, str) or not status:
        return
    emitter = _CTX_VAR.get()
    if emitter is None:
        return
    try:
        emitter(status, event_kind)
    except Exception as exc:  # noqa: BLE001 — never break a tool
        _log.debug(
            "tool_progress.emitter_failed",
            extra={"error": repr(exc)},
        )


def set_tool_progress_emitter(
    emitter: ToolProgressEmitter | None,
) -> "Token[ToolProgressEmitter | None]":
    """Install (or clear) the per-turn tool-progress emitter.

    Returns the ``Token`` the caller MUST hand back to
    :func:`reset_tool_progress_emitter` on turn end to restore the prior
    value.  Passing ``None`` disables emission for the current scope.
    """
    return _CTX_VAR.set(emitter)


def reset_tool_progress_emitter(
    token: "Token[ToolProgressEmitter | None]",
) -> None:
    """Restore the emitter that was installed before *token* was issued.

    The public counterpart of :func:`set_tool_progress_emitter`: callers
    restore through this function instead of touching this module's
    ``ContextVar`` handle (see the module docstring). Best-effort — a stale
    or foreign token (already reset, or issued in a different context) is
    logged and swallowed, because a failed restore of a telemetry channel
    must never break the turn that is unwinding.
    """
    try:
        _CTX_VAR.reset(token)
    except (ValueError, RuntimeError) as exc:
        # ``ContextVar.reset`` raises ``ValueError`` for a token that was
        # already used or was created in another Context.  Both mean "the
        # scope we wanted to restore is already gone" — nothing left to do.
        _log.debug(
            "tool_progress.reset_failed",
            extra={"error": repr(exc)},
        )
