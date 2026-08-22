# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""M5 per-tab soft-steer registry (replaces the ContextVar approach).

Why not a ContextVar
--------------------
The turn body (``StreamChatUseCase._run`` → ``_run_followup_loop``) is an
**async generator**.  A ``ContextVar.set()`` executed INSIDE a generator
mutates only that generator's own context; when the generator yields back
to its consumer the value is NOT visible in the consumer's context, and a
tool invoked from the consumer side (or from a spawned task whose context
was copied BEFORE the set) reads ``None``.  That silently disabled the
whole M5 auto-background feature.

This module replaces the ContextVar with an explicit process-local
registry keyed by ``tab_id`` — the same key the tool-invocation request
already carries — so any layer that knows the tab can look the context up
with no reliance on context propagation semantics.

Contract
--------
* ``register(tab_id, ctx)`` at turn start; ``unregister(tab_id)`` at turn
  end (both idempotent).
* ``get(tab_id)`` returns the live context or ``None``.
* Process-local, single event loop (same invariant as the injection /
  abort registries).  No lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


__all__ = [
    "TabSoftSteerCtx",
    "register_tab_soft_steer",
    "unregister_tab_soft_steer",
    "get_tab_soft_steer",
    "resolve_tab_for_conversation",
]


@dataclass(frozen=True, slots=True)
class TabSoftSteerCtx:
    """Per-turn soft-steer context, addressable by ``tab_id``.

    ``soft_steer_event`` — the abort handle's cooperative soft-steer signal.
    Long-running tools (``exec``) race their internal wait against this
    event; when it fires and enough time has elapsed the tool voluntarily
    hands the running process to ``background_process_manager`` and returns
    a synthetic "backgrounded" result.

    ``background_process_manager`` — the shared manager singleton (typed
    ``Any`` to avoid a cross-context import).  Duck-typed on
    ``adopt(proc=..., session_id=..., command=..., cwd=..., description=...)``.

    ``owner_session_id`` — the ``TabId.value``.  Used to route a background
    completion notice back to this tab.

    ``conversation_id`` — the ``ConversationId.value``.  This is the key the
    ``background_process`` LLM tool uses for ownership checks
    (``_chat_background_process_tool_bridge`` passes
    ``request.conversation_id.value``), so an ADOPTED record MUST be
    registered under it — otherwise the model's own ``status`` / ``logs`` /
    ``stop`` calls on the returned ``bgp_...`` id resolve to
    ``process_not_found`` (observed 2026-08-03).

    ``threshold_ms`` — foreground-wait budget in milliseconds; ``None``
    means the auto-background threshold arm of the exec race is skipped
    (the "auto-background disabled" setting).  Stop and mid-turn steering
    are unaffected.

    ``abort_event`` — the abort handle's hard Stop event (the same
    ``asyncio.Event`` ``StopChatUseCase`` trips).  ``None`` = not wired,
    so the exec race skips its Stop arm and Stop reaches the tool only
    through task cancellation, as it did pre-race.

    ``injection_subscribe`` — a zero-arg, ALREADY TAB-BOUND factory
    returning ``(event, dispose)`` for this tab's mid-turn injections
    (see
    :class:`qai.chat.adapters.injection_signal_bridge.InjectionSignalBridge`).
    Passed as a closure rather than the bridge itself so a tool in
    another bounded context can use it without importing any ``qai.chat``
    symbol (``.importlinter`` ``context-isolation``).  ``None`` = not
    wired, so the consumer SKIPS the injection arm of the race entirely —
    it must not substitute a never-firing event and pretend the arm is
    live.  Callers MUST invoke ``dispose`` in a ``finally``.
    """

    soft_steer_event: asyncio.Event
    background_process_manager: Any
    owner_session_id: str
    conversation_id: str = ""
    threshold_ms: int | None = None
    abort_event: asyncio.Event | None = None
    injection_subscribe: (
        Callable[[], tuple[asyncio.Event, Callable[[], None]]] | None
    ) = None


#: ``tab_id_value -> TabSoftSteerCtx``.
_REGISTRY: dict[str, TabSoftSteerCtx] = {}


def register_tab_soft_steer(tab_id: str, ctx: TabSoftSteerCtx) -> None:
    """Publish ``ctx`` for ``tab_id``.  Overwrites any prior entry."""
    if not tab_id:
        return
    _REGISTRY[tab_id] = ctx


def unregister_tab_soft_steer(tab_id: str) -> None:
    """Drop ``tab_id``'s context (turn teardown).  Idempotent."""
    if not tab_id:
        return
    _REGISTRY.pop(tab_id, None)


def get_tab_soft_steer(tab_id: str) -> TabSoftSteerCtx | None:
    """Return ``tab_id``'s live context, or ``None`` when not registered."""
    if not tab_id:
        return None
    return _REGISTRY.get(tab_id)


def resolve_tab_for_conversation(conversation_id: str) -> str | None:
    """Return the tab id whose live turn owns ``conversation_id``.

    Used by background-completion routing: an M5-adopted exec child is
    registered with the CONVERSATION id as its ``session_id`` (the
    ``background_process`` tool's ownership key), but tab-scoped delivery is
    keyed by TAB id — this maps one to the other using the live
    soft-steer registrations.  ``None`` when no live turn matches (the
    caller then treats the input as a tab id, which is correct for
    LLM-started background processes).
    """
    if not conversation_id:
        return None
    for tab_id, ctx in _REGISTRY.items():
        if ctx.conversation_id == conversation_id:
            return tab_id
    return None
