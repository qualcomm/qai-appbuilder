# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-level background periodic task scheduler.

A small, dependency-free "run-once-on-start + repeat-every-interval" scheduler
built on :mod:`asyncio`, driven by the FastAPI lifespan (startup / shutdown).
It is **platform-neutral and edition-agnostic**: it knows nothing about any
bounded context (``qai.<ctx>``), HTTP, or internal/external editions. Any
feature that needs a lightweight periodic job can register a callable.

Typical consumers
-----------------
* ``usage_reporter`` (run once on start, then every 24h) — internal-only.
* future: model-catalog sync, cache cleanup, health self-checks, ...

Design (mirrors V1 ``backend/background/__init__.py`` behaviour, but split into
clearer collaborators):

- **No extra dependency**: pure ``asyncio.create_task``; no apscheduler.
- **Non-blocking**: :meth:`register` only queues a task and returns; the first
  invocation runs asynchronously inside the loop, so application startup is
  never blocked.
- **Exception isolation**: a failure in one task neither affects other tasks
  nor crashes the manager — it is logged and the task keeps its schedule.
- **State-Truth-First** (AGENTS.md): "is a task running?" is answered from the
  real :class:`asyncio.Task` state (``task.done()``), never from an optimistic
  flag. Cancellation propagates ``CancelledError`` correctly and is awaited so
  no zombie tasks survive shutdown.
- **Sync / async callables**: a registered callable may be a plain function
  (its return value is awaited only if awaitable) or an ``async def``.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from qai.platform.logging import get_logger

_log = get_logger("qai.platform.scheduling")

#: Task callback signature: a no-argument callable returning ``None`` or an
#: awaitable that resolves to ``None``.
TaskFunc = Callable[[], None | Awaitable[None]]


@dataclass
class _ScheduledTask:
    """Internal bookkeeping for a single registered periodic task."""

    name: str
    func: TaskFunc
    interval_seconds: float
    run_on_start: bool = True
    initial_delay_seconds: float = 0.0
    last_run_ts: float = 0.0
    run_count: int = 0
    error_count: int = 0
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        """True iff the backing asyncio.Task exists and is not finished.

        State-Truth-First: derived from the live task object, not a flag.
        """
        return self.task is not None and not self.task.done()


class BackgroundTaskManager:
    """Application-level periodic task manager.

    Lifecycle::

        mgr = BackgroundTaskManager()
        mgr.register("usage_reporter", send_usage, interval_seconds=24 * 3600)
        await mgr.start()      # in lifespan startup
        ...
        await mgr.shutdown()   # in lifespan shutdown

    All public coroutine methods are safe to call from the event loop thread.
    The manager is not thread-safe across loops (one loop per manager).
    """

    def __init__(self) -> None:
        self._tasks: dict[str, _ScheduledTask] = {}
        self._started: bool = False
        self._stopping: bool = False
        self._lock = asyncio.Lock()

    # -- registration -----------------------------------------------------
    def register(
        self,
        name: str,
        func: TaskFunc,
        *,
        interval_seconds: float,
        run_on_start: bool = True,
        initial_delay_seconds: float = 0.0,
    ) -> None:
        """Register a periodic task.

        Parameters
        ----------
        name:
            Unique task name (used for logging and duplicate-registration
            guarding). A second registration under the same name is ignored.
        func:
            No-argument callable; plain function or ``async def``.
        interval_seconds:
            Delay between two successive runs, in seconds. Must be ``> 0``.
            E.g. once per day = ``24 * 3600``.
        run_on_start:
            If True (default), run once immediately after :meth:`start`
            (after ``initial_delay_seconds``). If False, the first run happens
            only after one full ``interval_seconds``.
        initial_delay_seconds:
            Extra delay before the very first run, to avoid contending with the
            startup burst. Applies whether or not ``run_on_start`` is set.

        Raises
        ------
        ValueError:
            If ``interval_seconds <= 0`` or ``initial_delay_seconds < 0``.
        RuntimeError:
            If called after :meth:`start` (registration must precede start).
        """
        if interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds must be > 0 (got {interval_seconds})"
            )
        if initial_delay_seconds < 0:
            raise ValueError(
                f"initial_delay_seconds must be >= 0 (got {initial_delay_seconds})"
            )
        if self._started:
            raise RuntimeError(
                "BackgroundTaskManager.register() must be called before start()"
            )
        if name in self._tasks:
            _log.warning(
                "background.task_already_registered", task=name, ignored=True
            )
            return
        self._tasks[name] = _ScheduledTask(
            name=name,
            func=func,
            interval_seconds=float(interval_seconds),
            run_on_start=run_on_start,
            initial_delay_seconds=float(initial_delay_seconds),
        )
        _log.info(
            "background.task_registered",
            task=name,
            interval_seconds=float(interval_seconds),
            run_on_start=run_on_start,
            initial_delay_seconds=float(initial_delay_seconds),
        )

    # -- start ------------------------------------------------------------
    async def start(self) -> None:
        """Start the loops for all registered tasks (non-blocking).

        Idempotent: a second call while already started is a no-op.
        """
        async with self._lock:
            if self._started:
                _log.debug("background.already_started")
                return
            self._started = True

        for st in self._tasks.values():
            if st.task is None or st.task.done():
                st.task = asyncio.create_task(
                    self._run_loop(st), name=f"bg-{st.name}"
                )

    # -- shutdown ---------------------------------------------------------
    async def shutdown(self, timeout: float = 5.0) -> None:
        """Gracefully stop: cancel every task and await its completion.

        Each task is cancelled independently and gathered with
        ``return_exceptions=True`` so one task's teardown error cannot stop
        the others from being awaited (exception isolation). A bounded
        ``timeout`` prevents a misbehaving task from hanging shutdown forever.
        """
        self._stopping = True
        pending: list[asyncio.Task[None]] = []
        for st in self._tasks.values():
            t = st.task
            if t is not None and not t.done():
                t.cancel()
                pending.append(t)
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout,
            )
        except TimeoutError:
            _log.warning(
                "background.shutdown_timeout",
                timeout_seconds=timeout,
                pending=len(pending),
            )

    # -- internal loop ----------------------------------------------------
    async def _run_loop(self, st: _ScheduledTask) -> None:
        """Drive a single task's schedule until cancelled.

        On :class:`asyncio.CancelledError` the loop exits cleanly (the error is
        absorbed here so that gathering during shutdown does not surface it as
        a failure). Any non-cancel exception escaping the loop machinery is
        logged and the loop terminates without restarting — but note that the
        common case (the callback raising) is already isolated inside
        :meth:`_invoke_safely`, so a failing callback keeps its schedule.
        """
        try:
            if st.initial_delay_seconds > 0:
                await asyncio.sleep(st.initial_delay_seconds)

            if st.run_on_start:
                await self._invoke_safely(st)

            while not self._stopping:
                await asyncio.sleep(st.interval_seconds)
                await self._invoke_safely(st)
        except asyncio.CancelledError:
            _log.debug("background.task_cancelled", task=st.name)
            # Swallow: cancellation is the expected shutdown path. We do NOT
            # re-raise because the loop coroutine is owned by this manager and
            # shutdown() already gathers with return_exceptions=True; the task
            # ends in the (normal) finished state rather than the cancelled
            # state, which is the documented contract here.
        except Exception:  # noqa: BLE001 — loop machinery must never crash
            _log.exception("background.loop_crashed", task=st.name)

    async def _invoke_safely(self, st: _ScheduledTask) -> None:
        """Invoke the callback once; isolate any non-cancel exception.

        ``CancelledError`` is re-raised so cancellation during a callback
        propagates up to :meth:`_run_loop` (State-Truth-First: a cancel is a
        real cancel, not a swallowed error). Every other exception is recorded
        in ``error_count`` and logged, leaving the schedule intact.
        """
        st.last_run_ts = time.time()
        st.run_count += 1
        try:
            result = st.func()
            if inspect.isawaitable(result):
                await result
            _log.debug("background.task_ran", task=st.name, run=st.run_count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — isolate per-task failures
            st.error_count += 1
            _log.warning(
                "background.task_run_failed",
                task=st.name,
                run=st.run_count,
                error_count=st.error_count,
                error=str(exc),
            )

    # -- introspection ----------------------------------------------------
    def stats(self) -> dict[str, dict[str, Any]]:
        """Return per-task runtime statistics (health-check / debugging).

        The ``running`` flag is derived from the live asyncio.Task state.
        """
        return {
            name: {
                "interval_seconds": st.interval_seconds,
                "run_count": st.run_count,
                "error_count": st.error_count,
                "last_run_ts": st.last_run_ts,
                "running": st.running,
            }
            for name, st in self._tasks.items()
        }
