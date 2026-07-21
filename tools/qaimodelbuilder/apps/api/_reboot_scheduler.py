# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cross-context reboot scheduler shared by ``system`` and ``security`` ports.

Two reboot-related ports exist in the codebase, by historical /
business-domain reasons:

* :class:`apps.api.system_ports.RebootSignalPort` (``signal_reboot``) —
  wired to ``POST /api/system/reboot`` (system namespace).
* :class:`qai.security.application.ports.RebootSignalPort`
  (``request_reboot``) — wired to ``UpdatePolicyUseCase`` (security
  namespace; a policy change may require a process restart).

Both must ultimately exit the process with
``settings.server.reboot_exit_code`` (default 75 — external contract,
see ``08-business-capabilities.md`` §9.2). The supervisor (systemd /
shell wrapper) restarts the process on observing exit-code 75.

To keep the two ports duck-typed Protocol-compatible AND share a single
underlying scheduler / debounce window / exit-code value, this module
defines a private :class:`_RebootScheduler` that both adapters delegate
to. The scheduler:

* records the reason of each request (newest first);
* coalesces concurrent calls — only the first call schedules the exit;
* schedules exit asynchronously (``asyncio.create_task``) so the route
  handler can flush the 202 response BEFORE the process disappears;
* allows tests to inject a no-op ``exit_callback`` so the test process
  is not killed by ``sys.exit(75)``.

The scheduler lives under ``apps/api/`` rather than under any bounded
context because it is a process-level capability of the API server,
shared between two contexts via composition; placing it inside
``security/`` would force ``system_ports`` adapters to import a
``security`` module and break the ``contexts-do-not-import-each-other``
import-linter contract.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from qai.platform.logging import get_logger

__all__ = [
    "SecurityRebootSignalAdapter",
    "SystemRebootSignalAdapter",
    "has_graceful_exit_handler",
    "_RebootScheduler",
]


_log = get_logger(__name__)

ExitCallback = Callable[[int], None]
GracefulExitHandler = Callable[[int], bool]

_graceful_exit_handler: GracefulExitHandler | None = None
_requested_exit_code: int | None = None


def set_graceful_exit_handler(handler: GracefulExitHandler | None) -> None:
    """Install a process-local graceful-exit handler for supervised API runs.

    ``sys.exit`` raised from an asyncio task interrupts ``asyncio.run`` while
    websocket / httpx async generators may still be mid-iteration.  On Python
    3.13 that can surface noisy shutdown races such as
    ``RuntimeError: aclose(): asynchronous generator is already running``.  The
    uvicorn entry point therefore installs a handler that asks the server to
    shut down gracefully and then returns the requested exit code from
    ``main()``.  Unit tests and debug code that do not install a handler keep
    the historical ``sys.exit(code)`` behaviour.

    Installing or clearing a handler also **resets** the recorded
    ``_requested_exit_code`` so a fresh ``main()`` run never inherits a stale
    code from a previous in-process run (State-Truth-First: do not let
    process-local residue stand in for the real, current request).
    """

    global _graceful_exit_handler, _requested_exit_code
    _graceful_exit_handler = handler
    _requested_exit_code = None


def get_requested_exit_code(default: int = 0) -> int:
    """Return the code requested through the graceful-exit handler."""

    return _requested_exit_code if _requested_exit_code is not None else default


def has_graceful_exit_handler() -> bool:
    """Return True when a process-local graceful-exit handler is installed.

    ``apps.api.main.main()`` installs a handler tied to the live
    ``uvicorn.Server`` before ``server.run()`` loads the app factory.  The app
    factory must not overwrite that handler; otherwise ``POST /api/system/reboot``
    raises ``SystemExit`` inside the ASGI worker instead of setting
    ``server.should_exit``, leaving the supervisor waiting on a half-dead child.
    Reload workers, by contrast, enter only through the factory and therefore
    have no handler; the factory installs a fallback only in that case.
    """

    return _graceful_exit_handler is not None


def _default_exit(code: int) -> None:
    """Default exit callback: graceful uvicorn stop or ``sys.exit`` fallback.

    Wrapped so tests can inject ``lambda code: None`` instead of actually
    killing the test runner.  Production supervised runs install a uvicorn
    handler via :func:`set_graceful_exit_handler`; direct / test contexts fall
    back to ``sys.exit`` so the external exit-code contract is preserved.
    """

    global _requested_exit_code
    _requested_exit_code = code
    handler = _graceful_exit_handler
    if handler is not None:
        try:
            if handler(code):
                return
        except Exception:  # noqa: BLE001 - fallback preserves old behaviour
            _log.exception("reboot.graceful_exit_handler_failed")
    sys.exit(code)


@dataclass(slots=True)
class _RebootScheduler:
    """Coalescing scheduler for reboot signals.

    Both :class:`SystemRebootSignalAdapter` and
    :class:`SecurityRebootSignalAdapter` delegate ``schedule(reason=...)``
    here. The first call within an active scheduling window schedules an
    asyncio task that, after ``delay_seconds``, invokes ``exit_callback``
    with ``exit_code``. Subsequent calls during the same window only
    record their reasons; they do NOT spawn additional exit tasks.

    Tests construct a scheduler with ``exit_callback=lambda c: None``
    and ``delay_seconds=0.0`` to drain the task synchronously.

    Reasons are exposed via :attr:`reasons` (read-only via accessor)
    primarily to support assertion in tests; the production code path
    only logs them.
    """

    exit_code: int = 75
    delay_seconds: float = 0.5
    exit_callback: ExitCallback = field(default=_default_exit)
    signaled: bool = field(default=False, init=False)
    reasons: list[str] = field(default_factory=list, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    async def schedule(self, *, reason: str) -> None:
        """Record the reason and (idempotently) schedule the exit.

        Coalescing rule: at most one in-flight exit task per scheduler
        instance. Multiple ``schedule`` calls extend the recorded
        ``reasons`` list but do not multiply the exit invocations.
        """

        await self._schedule_with_code(reason=reason, code=self.exit_code)

    async def schedule_exit(self, *, reason: str) -> None:
        """Schedule a clean process *exit* (code 0), not a reboot.

        Used by ``POST /api/system/exit`` — the desktop shell calls this on
        window-close so the backend runs its full lifespan ``shutdown`` hooks
        (close DB, stop daemons) and then exits with code 0. The supervisor
        (`apps/cli/serve.py`) treats exit 0 as "stop, do not respawn", whereas
        :meth:`schedule` uses ``exit_code`` (75) which the supervisor respawns.

        This exists because a Tauri GUI shell has no console, so the
        ``CTRL_BREAK_EVENT`` path (`desktop/src-tauri/src/lib.rs`) cannot
        deliver a graceful stop; an HTTP call is the reliable cross-process
        graceful-stop channel.
        """

        await self._schedule_with_code(reason=reason, code=0)

    async def _schedule_with_code(self, *, reason: str, code: int) -> None:
        self.reasons.append(reason)
        self.signaled = True
        _log.info(
            "reboot.scheduled",
            reason=reason,
            exit_code=code,
            already_signaled=self._task is not None,
        )
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (sync test context). Invoke immediately so
            # callers in non-async tests can still observe the side
            # effect via the injected exit_callback.
            self.exit_callback(code)
            return
        self._task = loop.create_task(self._delayed_exit(code))

    async def _delayed_exit(self, code: int) -> None:
        try:
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)
            self.exit_callback(code)
        except SystemExit:
            # Re-raise so the supervisor sees exit_code; lifespan
            # finalisers and uvicorn's shutdown still run.
            raise
        except Exception:  # noqa: BLE001 — diagnostic-only
            _log.exception("reboot.delayed_exit_failed")
            raise


class SystemRebootSignalAdapter:
    """Real :class:`apps.api.system_ports.RebootSignalPort` implementation.

    Delegates to a shared :class:`_RebootScheduler`; keeps the system
    namespace's port shape (``signal_reboot``) intact while sharing
    the underlying mechanism with ``security``.
    """

    __slots__ = ("_scheduler",)

    def __init__(self, *, scheduler: _RebootScheduler) -> None:
        self._scheduler = scheduler

    async def signal_reboot(self, *, reason: str) -> None:
        await self._scheduler.schedule(reason=reason)


class SecurityRebootSignalAdapter:
    """Real :class:`qai.security.application.ports.RebootSignalPort` impl.

    Same scheduler, different port method name (``request_reboot``)
    matching the security port's contract. ``UpdatePolicyUseCase``
    invokes this when a policy change requires a process restart.
    """

    __slots__ = ("_scheduler",)

    def __init__(self, *, scheduler: _RebootScheduler) -> None:
        self._scheduler = scheduler

    async def request_reboot(self, *, reason: str) -> None:
        await self._scheduler.schedule(reason=reason)
