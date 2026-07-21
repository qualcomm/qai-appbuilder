# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Background-task registry — keep strong refs to fire-and-forget tasks.

Problem this solves
--------------------
``asyncio.create_task(coro)`` returns a :class:`asyncio.Task`. If the
caller drops that reference, CPython only holds a *weak* reference to the
running task, so it can be garbage-collected mid-flight ("task was
destroyed but it is pending"). Such fire-and-forget tasks also:

* are never cancelled on app shutdown (orphaned coroutines / leaked
  subprocesses), and
* swallow their exceptions silently (the traceback is only logged when
  the task object is GC'd, which may never happen deterministically).

:class:`TaskRegistry` fixes all three: it holds a strong ref in a
``set`` until the task finishes (``add_done_callback`` discards it),
logs any non-cancellation exception, and exposes :meth:`cancel_all`
for orderly shutdown.

Design notes
------------
* Pure ``asyncio`` + ``qai.platform.logging`` — no framework / context
  imports, so any layer may construct one.
* ``spawn`` is the ergonomic entry point (wraps ``create_task`` +
  ``add``); ``add`` adopts an already-created task.
* The registry is **not** a lock — concurrent ``spawn`` from the same
  event loop is safe (single-threaded per loop); it is *not* safe to
  share one instance across event loops.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from qai.platform.logging import get_logger

__all__ = ["TaskRegistry"]

_LOGGER = get_logger(__name__)


class TaskRegistry:
    """Holds strong references to background tasks until they complete.

    Usage::

        tasks = TaskRegistry()
        tasks.spawn(_drain_output(proc), name="oc-drain")
        ...
        await tasks.cancel_all()  # on shutdown
    """

    __slots__ = ("_tasks",)

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> "asyncio.Task[Any]":
        """Create a task from ``coro``, retain a strong ref, and return it."""
        task = asyncio.create_task(coro, name=name)
        self.add(task)
        return task

    def add(self, task: "asyncio.Task[Any]") -> None:
        """Adopt an already-created task (strong ref until it finishes)."""
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: "asyncio.Task[Any]") -> None:
        """Discard the finished task and log any non-cancellation error."""
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _LOGGER.warning(
                "background_task.failed",
                task_name=task.get_name(),
                error_type=type(exc).__name__,
                exc_info=exc,
            )

    async def cancel_all(self) -> None:
        """Cancel every outstanding task and await their teardown.

        Idempotent: a second call is a no-op once the set is empty.
        Cancellation exceptions are swallowed (expected); other errors
        were already logged by :meth:`_on_done`.
        """
        if not self._tasks:
            return
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    def __len__(self) -> int:
        return len(self._tasks)
