# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-turn context vars for the M5+M6 auto-background feature.

Instead of threading ``soft_steer_event`` and the background-process
manager through every layer of the tool-invocation stack, we set them
once at turn-start (by :class:`StreamChatUseCase._run`) into a pair of
:class:`contextvars.ContextVar` slots.  Any tool handler running inside
that turn's async task can read them synchronously without a signature
change to any port.

This is intentionally a very thin utility — no coupling to any specific
tool, port, or adapter.

Usage
-----
Producer (turn-start, ``StreamChatUseCase._run``)::

    with set_turn_soft_steer_ctx(
        soft_steer_event=handle.soft_steer_event,
        background_process_manager=bg_pm,
        owner_session_id=tab.id.value,
    ):
        # ... run the kernel / drain frames / etc ...

Consumer (inside a tool, e.g. ``tool_exec``)::

    ctx = get_turn_soft_steer_ctx()
    if ctx is not None:
        # ctx.soft_steer_event, ctx.background_process_manager,
        # ctx.owner_session_id are all available.

Contract
--------
* ``ContextVar`` values are per-task.  A tool spawned via
  :func:`asyncio.create_task` inside the turn INHERITS the context, so
  parallel tool calls all see the same soft-steer event and manager.
* Reading in a context that was never set returns ``None`` — every
  legacy path stays byte-identical.
* The ``__enter__`` / ``__exit__`` pattern uses ``ContextVar.set()`` +
  ``ContextVar.reset()`` so nested calls (e.g. sub-agent turns) do not
  clobber the outer context.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any


__all__ = [
    "TurnSoftSteerCtx",
    "get_turn_soft_steer_ctx",
    "set_turn_soft_steer_ctx",
]


@dataclass(frozen=True, slots=True)
class TurnSoftSteerCtx:
    """Per-turn context published by :class:`StreamChatUseCase._run`.

    ``soft_steer_event`` — the abort handle's cooperative soft-steer signal.
    Long-running tools (``exec`` / ``bash``) race their internal wait
    against this event; when it fires and enough time has elapsed, the
    tool voluntarily hands the running process to
    ``background_process_manager`` and returns a synthetic "backgrounded"
    result.

    ``background_process_manager`` — the shared manager singleton (typed
    ``Any`` here to avoid a cross-context import).  Duck-typed on
    ``start(StartInput) -> Awaitable[Info]``.

    ``owner_session_id`` — the ``TabId.value`` to stamp on
    ``StartInput.session_id`` so the eventual completion notification can
    be routed back to this tab.
    """

    soft_steer_event: asyncio.Event
    background_process_manager: Any
    owner_session_id: str
    #: M5 threshold in milliseconds. Supplied by DI (from ``ChatSettings``
    #: ``bash_auto_background_enabled`` / ``bash_auto_background_threshold_ms``
    #: via ``_chat_di._build_auto_bg_threshold_reader`` +
    #: ``_wrap_with_settings_fallback``).  ``None`` means auto-background is
    #: off for this call — either the user switched it off or the DI wiring
    #: did not thread the setting here.  Consumers skip the exec race's
    #: auto-background threshold arm (Stop and mid-turn steering are
    #: unaffected), and never silently fall back to a phantom default.
    threshold_ms: int | None = None
    #: The abort handle's hard Stop event (the one ``StopChatUseCase``
    #: trips).  ``None`` = not wired, so the consumer skips the exec
    #: race's Stop arm and Stop reaches the tool only through task
    #: cancellation, as it did pre-race.
    abort_event: asyncio.Event | None = None
    #: Zero-arg, ALREADY TAB-BOUND factory returning
    #: ``(event, dispose)`` for this turn's mid-turn injections (see
    #: :class:`qai.chat.adapters.injection_signal_bridge.InjectionSignalBridge`).
    #: A closure rather than the bridge itself so a tool in another
    #: bounded context can use it without importing any ``qai.chat``
    #: symbol (``.importlinter`` ``context-isolation``).  ``None`` = not
    #: wired, so the consumer SKIPS the injection arm of the race
    #: entirely — it must not substitute a never-firing event and pretend
    #: the arm is live.  Callers MUST invoke ``dispose`` in a ``finally``.
    injection_subscribe: (
        Callable[[], tuple[asyncio.Event, Callable[[], None]]] | None
    ) = None


_CTX_VAR: contextvars.ContextVar[TurnSoftSteerCtx | None] = contextvars.ContextVar(
    "qai.turn_soft_steer_ctx",
    default=None,
)


def get_turn_soft_steer_ctx() -> TurnSoftSteerCtx | None:
    """Return the current turn's soft-steer context, or ``None`` when unset."""
    return _CTX_VAR.get()


@contextlib.contextmanager
def set_turn_soft_steer_ctx(
    *,
    soft_steer_event: asyncio.Event,
    background_process_manager: Any,
    owner_session_id: str,
    threshold_ms: int | None = None,
    abort_event: asyncio.Event | None = None,
    injection_subscribe: (
        Callable[[], tuple[asyncio.Event, Callable[[], None]]] | None
    ) = None,
):
    """Publish a per-turn context; automatically resets on exit.

    Use as a context manager around the whole turn body::

        with set_turn_soft_steer_ctx(...):
            await self._kernel.run(...)
    """
    ctx = TurnSoftSteerCtx(
        soft_steer_event=soft_steer_event,
        background_process_manager=background_process_manager,
        owner_session_id=owner_session_id,
        threshold_ms=threshold_ms,
        abort_event=abort_event,
        injection_subscribe=injection_subscribe,
    )
    token = _CTX_VAR.set(ctx)
    try:
        yield ctx
    finally:
        _CTX_VAR.reset(token)


@contextlib.contextmanager
def detach_injection_arm_for_subagent():
    """Drop the PARENT tab's injection arm for a nested sub-agent's tools.

    ``ContextVar`` values are inherited by every task spawned inside a turn
    (see the module Contract), which is what lets parallel tool calls of ONE
    agent share the soft-steer event and the manager. A spawned sub-agent is
    NOT such a tool: it is an independent run with its own rounds and its own
    abort registry, so inheriting :attr:`TurnSoftSteerCtx.injection_subscribe`
    — documented as "ALREADY TAB-BOUND" to the PARENT tab — made the parent's
    Ctrl+Enter injection win the race inside an ``exec`` the SUB-agent had
    started, backgrounding a child the user never addressed.

    Everything else stays: ``threshold_ms``, ``abort_event`` and the manager
    are turn-scoped resources the sub-agent's tools legitimately share (a
    parent Stop must still reach them, and the auto-background threshold is a
    global policy, not a per-tab signal).

    No-op when no context is published, so non-chat callers are unaffected.
    """
    ctx = _CTX_VAR.get()
    if ctx is None or ctx.injection_subscribe is None:
        yield
        return
    token = _CTX_VAR.set(replace(ctx, injection_subscribe=None))
    try:
        yield
    finally:
        _CTX_VAR.reset(token)
