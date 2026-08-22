# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Tool interruptibility contract.

Central registry declaring which tool names are safe to hard-cancel on a
mid-turn steer signal.

Judgement rule
--------------
A tool is *interruptible* ONLY when its outstanding work is a "waiting
for a UI panel" that must VANISH the instant the user sends a fresh
message — the panel would otherwise stay open forever behind the new
conversation flow.

Pure query-latency tools (network fetch, sqlite search) and side-effecting
tools (exec / write / edit / apply_patch) are DELIBERATELY NOT
interruptible: their result is worth delivering, so the correct UX is
"let it finish and fold the result in on the next round" (M6 aside +
normal ``tool_result`` path), NOT "throw away the half-done work".
Side-effecting tools additionally receive the cooperative soft-steer
signal and may voluntarily yield (see
:mod:`qai.platform.turn_soft_steer_ctx`).

MCP tools follow the same rule: they default to non-interruptible.  A
third-party MCP server's "read-only" call is still worth delivering; a
mutating one MUST NOT be cancelled mid-flight.  No dynamic registration
API is exposed — introducing one would either be dead code (nobody
knows an MCP call is safe to cancel without inspecting the remote
server, which we cannot) or actively unsafe.

Contract
--------
Each entry is either a plain ``bool`` (unconditional) or a predicate
``(args: dict) -> bool`` (per-invocation judgement).  A predicate that
raises is treated as ``False`` (safest default).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


__all__ = ["is_interruptible", "InterruptibilityValue"]


InterruptibilityValue = bool | Callable[[dict[str, Any]], bool]


#: Central whitelist.  Keys are tool NAMES (matching the string registered
#: on ``ToolInvocationPort``); values are :type:`InterruptibilityValue`.
#:
#: Kept intentionally minimal: today ``question`` is the only tool whose
#: outstanding work is a UI panel that must close on a fresh user
#: message.  Everything else — including web_fetch / web_search /
#: search_conversations / MCP tools — falls through to the ``False``
#: default so the model / user see the result even after the user has
#: moved on.
_INTERRUPTIBILITY: dict[str, InterruptibilityValue] = {
    # ``question`` opens a modal-style panel and blocks until the user
    # answers.  If the user instead sends a fresh chat message, the
    # panel MUST close (otherwise it stays visible-but-orphaned behind
    # the new conversation).  Hard-cancelling the tool causes the
    # handler's ``await user_answer`` to raise CancelledError which
    # collapses the panel — the correct UX.
    "question": True,
}


def is_interruptible(tool_name: str, args: dict[str, Any] | None = None) -> bool:
    """Resolve whether ``tool_name`` (with ``args``) is interruptible.

    Default (no entry): False — safer to keep the tool running.  A
    predicate that raises is caught and treated as False.
    """
    value = _INTERRUPTIBILITY.get(tool_name)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    try:
        return bool(value(args or {}))
    except Exception:  # noqa: BLE001 — safest default
        return False
