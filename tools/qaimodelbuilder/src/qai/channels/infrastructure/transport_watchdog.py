# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Transport reconnection watchdog for channel connections."""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

_RECONNECT_DELAYS = (5.0, 10.0, 30.0, 60.0, 120.0)


class WatchableTransport(Protocol):
    """Transport that can be monitored and restarted."""

    def is_alive(self) -> bool:
        """Check if the transport connection is healthy."""
        ...

    async def restart(self) -> None:
        """Tear down and rebuild the transport connection."""
        ...


class TransportWatchdog:
    """Monitors a transport connection and reconnects on failure.

    Usage:
        watchdog = TransportWatchdog(transport=my_transport, check_interval=120.0)
        watchdog.start()
        # ... later ...
        await watchdog.stop()
    """

    def __init__(
        self,
        *,
        transport: WatchableTransport,
        check_interval: float = 120.0,
        max_consecutive_failures: int = 10,
    ):
        self._transport = transport
        self._check_interval = check_interval
        self._max_failures = max_consecutive_failures
        self._task: asyncio.Task | None = None
        self._consecutive_failures = 0
        self._stopped = False

    def start(self) -> None:
        """Start the watchdog background task."""
        if self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the watchdog."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        """Main watchdog loop."""
        while not self._stopped:
            await asyncio.sleep(self._check_interval)
            if self._stopped:
                break

            if self._transport.is_alive():
                self._consecutive_failures = 0
                continue

            # Transport is dead — attempt reconnection
            self._consecutive_failures += 1
            if self._consecutive_failures > self._max_failures:
                logger.error(
                    "watchdog.max_failures_reached",
                    extra={"consecutive": self._consecutive_failures},
                )
                break

            delay = _RECONNECT_DELAYS[
                min(self._consecutive_failures - 1, len(_RECONNECT_DELAYS) - 1)
            ]
            logger.warning(
                "watchdog.transport_dead",
                extra={"attempt": self._consecutive_failures, "delay": delay},
            )
            await asyncio.sleep(delay)

            try:
                await self._transport.restart()
                # State-Truth-First (AGENTS.md 铁律3): do NOT reset the
                # failure counter just because restart() returned without
                # raising — that is the "success based on call return, not
                # real confirmation" anti-pattern. The counter is reset at
                # the top of the loop ONLY when the next ``is_alive()`` probe
                # confirms the connection is actually back. This keeps a
                # connection that "restart()s cleanly but never truly
                # reconnects" accumulating failures until it hits the
                # ``max_failures`` give-up bound, instead of spinning forever
                # while falsely logging success every cycle.
                logger.info(
                    "watchdog.restart_attempted",
                    extra={"attempt": self._consecutive_failures},
                )
            except Exception:
                logger.exception("watchdog.reconnect_failed")

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()
