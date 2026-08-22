# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Observe :class:`ScheduledTaskFired` events and fan them out.

A scheduled task runs as a real agent turn on its bound conversation (see
:class:`~qai.chat.adapters.scheduled_task_runner.ScheduledTaskRunner`): the
turn persists its own user prompt and assistant reply and emits the live
``ChatStream*`` events internally, so the result is ALREADY in the
conversation as a genuine agent turn — this bridge does NOT persist anything
(an earlier version that appended a second ``assistant`` message per fire
would double-post the result).

What this bridge DOES, on each :class:`ScheduledTaskFired`:

* records one ``scheduling.fired_notified`` info log (observability seam);
* if a channel-push callback was injected, invokes it so a task that was
  created from a Feishu / WeChat conversation delivers its result back to
  that channel user. The task fires headlessly (the runner drives
  ``collect_completion``, bypassing the SSE/WS routes that trigger the WebUI
  channel-sync push), so without this callback a channel-scheduled task would
  surface its result ONLY in the WebUI. The callback is best-effort and lives
  at the apps layer (it crosses ``chat`` → ``channels``); this adapter stays
  free of any ``qai.channels`` import — it just awaits the injected coroutine.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from qai.platform.events import EventBus
from qai.platform.events.bus import EventEnvelope
from qai.platform.logging import get_logger
from qai.platform.scheduling import ScheduledTaskFired

_log = get_logger(__name__)

__all__ = ["ScheduledTaskAsideBridge"]

#: Injected fan-out callback: given a fired event, deliver it out-of-band
#: (e.g. push to the bound channel user). Best-effort; may raise — the bridge
#: swallows and logs. ``None`` disables the fan-out (log-only).
ScheduledTaskFiredSink = Callable[[ScheduledTaskFired], Awaitable[None]]


class ScheduledTaskAsideBridge:
    """Subscribe to scheduled-task completions; log + optionally fan out.

    Delivery of the result into the conversation happens inside the agent turn
    the scheduler drives, so this bridge never persists. It observes the fire
    and, when a :data:`ScheduledTaskFiredSink` is wired, forwards the event to
    it (channel push).
    """

    __slots__ = ("_bus", "_started", "_push_cb")

    def __init__(
        self,
        *,
        event_bus: EventBus,
        push_cb: "ScheduledTaskFiredSink | None" = None,
    ) -> None:
        """
        Args:
            event_bus: shared :class:`EventBus` the scheduler publishes on.
            push_cb: optional out-of-band delivery callback (channel push).
                ``None`` ⇒ log-only (the WebUI still shows the result via the
                persisted turn + the ``scheduling.task_fired`` WS event).
        """
        self._bus = event_bus
        self._started = False
        self._push_cb = push_cb

    async def start(self) -> None:
        """Subscribe to scheduled-task events. Idempotent."""
        if self._started:
            return
        await self._bus.subscribe(ScheduledTaskFired, self._on_fired)
        self._started = True

    async def _on_fired(self, envelope: EventEnvelope) -> None:
        ev = envelope.event
        if not isinstance(ev, ScheduledTaskFired):
            return
        _log.info(
            "scheduling.fired_notified",
            task_id=ev.task_id,
            run_id=ev.run_id,
            conversation_id=ev.conversation_id,
            ok=ev.ok,
        )
        if self._push_cb is not None:
            try:
                await self._push_cb(ev)
            except Exception as exc:  # noqa: BLE001 — delivery is best-effort
                _log.warning(
                    "scheduling.fired_push_failed",
                    task_id=ev.task_id,
                    error=str(exc),
                )
