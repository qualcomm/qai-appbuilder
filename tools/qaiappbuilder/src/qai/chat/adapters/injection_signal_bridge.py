# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Bridge injection events onto the per-tab ``asyncio.Event`` exec races on.

Why this exists
---------------
:class:`~qai.chat.adapters.injection_registry.InMemoryInjectionRegistry` is
a *mailbox*: the control WebSocket writes a pending injection, and the
streaming run loop drains it at its next inter-round seam.  That polling
cadence is fine for folding the text into the wire, but useless for a
long-running ``exec``: a command that will run for three more minutes
does not reach an inter-round seam, so a user who injects "stop, do this
instead" would sit and wait for the command.

The exec race (:func:`qai.platform.exec_race.wait_for_completion`) wants
that signal as an ``asyncio.Event`` it can race on.  This bridge is the
adapter between the two: it subscribes ONCE to the registry's
:meth:`~qai.chat.adapters.injection_registry.InMemoryInjectionRegistry.on_inject`
hook and fans each notification out to every event handed out for that
tab.

Fan-out semantics
-----------------
An injection wakes **every** waiter on that tab, not just the oldest.
A tab hosts at most one in-flight turn, but that turn may have several
exec calls running in parallel (one tool round can dispatch a batch).
The user's "handle this now" applies to the whole turn, so leaving one
of those commands blocking the round would defeat the point.

Tabs are isolated: injecting into tab A never wakes tab B's waiters.

Lifetime
--------
One instance per process, composed in the chat DI root alongside the
registry singleton it observes.  :meth:`subscribe` returns a disposer
that the caller MUST invoke (a ``finally``) — an un-disposed event would
keep the tab's subscriber list growing for the life of the process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from qai.chat.domain.ids import TabId
from qai.platform.logging import get_logger

__all__ = ["InjectionSignalBridge"]

_log = get_logger(__name__)


class InjectionSignalBridge:
    """Fan registry inject-events out to per-tab :class:`asyncio.Event` waiters."""

    __slots__ = ("_dispose_registry", "_subs")

    def __init__(self, injection_registry) -> None:  # noqa: ANN001
        """Subscribe to ``injection_registry``'s inject events.

        Args:
            injection_registry: The
                :class:`~qai.chat.application.ports.InjectionRegistryPort`
                singleton.  Duck-typed on ``on_inject`` so a test stub
                need not implement the whole port.
        """
        self._subs: dict[str, list[asyncio.Event]] = {}
        self._dispose_registry = injection_registry.on_inject(self._fanout)

    def subscribe(
        self, tab_id: TabId
    ) -> tuple[asyncio.Event, Callable[[], None]]:
        """Return a fresh event for ``tab_id`` plus its disposer.

        The event is set on the NEXT injection into that tab — an
        injection that landed *before* this call is not replayed, because
        the caller (the run loop) already saw it at its seam.

        The disposer is idempotent; call it in a ``finally`` so a
        finished race stops holding a slot in the tab's waiter list.
        """
        event = asyncio.Event()
        waiters = self._subs.setdefault(tab_id.value, [])
        waiters.append(event)

        def _dispose() -> None:
            try:
                waiters.remove(event)
            except ValueError:
                return
            if not waiters:
                # Drop the empty bucket so an idle process does not keep
                # one list per tab it has ever streamed.
                self._subs.pop(tab_id.value, None)

        return event, _dispose

    def close(self) -> None:
        """Unsubscribe from the registry and drop every waiter.

        Process teardown only.  Idempotent.
        """
        if self._dispose_registry is not None:
            self._dispose_registry()
            self._dispose_registry = None
        self._subs.clear()

    def _fanout(self, tab_id: TabId) -> None:
        """Wake every waiter registered for ``tab_id``."""
        waiters = self._subs.get(tab_id.value)
        if not waiters:
            return
        # Snapshot: a woken waiter's race may dispose itself synchronously,
        # so both the iteration order and the reported count come from the
        # state at trigger time.
        pending = tuple(waiters)
        for event in pending:
            event.set()
        _log.info(
            "exec_race.injection_triggered",
            tab_id=tab_id.value,
            subscriber_count=len(pending),
        )
