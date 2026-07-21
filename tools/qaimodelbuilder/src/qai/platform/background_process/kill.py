# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Background-process kill dispatcher (session-lifetime x POSIX vs Windows).

This module is the *routing + escalation* layer that sits on top of two
existing platform-shared kernels:

* :mod:`qai.platform.process.tree_kill` -- cross-platform subtree tree-kill
  (Windows ``taskkill /F /T`` + POSIX SIGTERM/SIGKILL ladder + reap).
* :mod:`qai.platform.process.kill_group` -- Win32 Job Object
  ``KILL_ON_JOB_CLOSE`` (the "parent dies -> child dies" OS-level safeguard
  mandated by ``AGENTS.md`` 🔴 State-Truth-First 铁律 5).

It does **not** re-implement either of them. The v1 manager only owns
``session`` / ``parent`` lifetimes (both in-memory, killed when the
owning session ends or the daemon shuts down), so this module fans out
to just two code paths depending on platform:

* **POSIX** -- ``os.killpg(pid, SIGTERM)`` → wait ``KILL_GRACE_S`` → if
  still alive, ``os.killpg(pid, SIGKILL)``.
* **Windows** -- ``taskkill /F /T`` in one step (no softer Windows tree
  signal -- mirrors :mod:`tree_kill` rationale).

The Job Object integration is done at *spawn time* via
:func:`assign_to_job`, not at kill time: a child assigned to a
``KILL_ON_JOB_CLOSE`` job is killed by the OS when the manager process
exits *for any reason*, which is the orthogonal "hard parent death"
fallback rail. The kill paths here are the *normal* (graceful + escalation)
teardown that runs while the manager is still alive.

All kill operations are best-effort: a process that has already exited
(``ProcessLookupError``) or is otherwise unreachable (``OSError``,
``PermissionError``) is treated as "done"; nothing here raises. Errors
are logged at WARNING and swallowed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from qai.platform.process import ProcessKillGroup

from qai.platform.process import terminate_process_tree

__all__ = [
    "KILL_GRACE_S",
    "alive_group",
    "alive_pid",
    "assign_to_job",
    "kill_session",
    "wait_exit",
]

logger = logging.getLogger("qai.platform.background_process.kill")

# SIGTERM -> SIGKILL escalation window (3.0 s) for session-lifetime +
# POSIX.
KILL_GRACE_S: float = 3.0


# ---------------------------------------------------------------------------
# liveness helpers (sync, used by manager / probe round trip)
# ---------------------------------------------------------------------------


def alive_pid(pid: int) -> bool:
    """Return True iff ``pid`` looks like a live, non-self process.

    Cheap liveness check used by callers that need a synchronous yes/no
    before deciding to escalate. ``alive_pid`` only answers "is there a
    process at that PID at all?", not "is it ours?" -- callers that need
    ownership semantics must do their own probe.

    * ``pid <= 0`` or ``pid == os.getpid()`` -> ``False`` (defensive: we
      never want to signal pid 0 / the current process / a negative pgid).
    * **POSIX** -- ``os.kill(pid, 0)`` ; ``PermissionError`` means "exists
      but owned by someone else", which still counts as alive.
    * **Windows** -- ``psutil.pid_exists(pid)`` (psutil is already a
      project dependency, see ``pyproject.toml`` and AGENTS.md §5.1).

    Never raises.
    """
    if pid <= 0 or pid == os.getpid():
        return False
    if sys.platform == "win32":
        try:
            import psutil

            return bool(psutil.pid_exists(pid))
        except Exception:  # noqa: BLE001 - best-effort liveness check
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # The process exists but we are not allowed to signal it -- still
        # alive from the OS's point of view.
        return True
    except (ProcessLookupError, OSError):
        return False


def alive_group(pid: int) -> bool:
    """Return True iff a POSIX process group with id ``pid`` has members.

    POSIX-only: ``os.kill(-pid, 0)`` probes the process group. On Windows
    process groups in the POSIX sense do not exist, so this returns
    ``False`` unconditionally (callers fall back to :func:`alive_pid`).

    Never raises.
    """
    if pid <= 0 or pid == os.getpid():
        return False
    if sys.platform == "win32":
        return False
    try:
        os.kill(-pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False


# ---------------------------------------------------------------------------
# Job Object assignment (Windows; no-op elsewhere)
# ---------------------------------------------------------------------------


def assign_to_job(kill_group: "ProcessKillGroup | None", pid: int) -> bool:
    """Best-effort assign ``pid`` to the manager's Win32 Job Object.

    Thin wrapper around :meth:`ProcessKillGroup.assign` that tolerates a
    missing kill-group (``None``) so the manager can call this
    unconditionally right after every spawn without an ``if`` branch:

    .. code-block:: python

        proc = await asyncio.create_subprocess_exec(...)
        assign_to_job(self._kill_group, proc.pid)

    Returns ``True`` only when the assignment really happened (Windows,
    job created OK, ``AssignProcessToJobObject`` returned non-zero).
    Returns ``False`` on every non-Windows platform, when no kill-group
    was injected, or when the kill-group reported it is not available.
    Callers treat ``False`` as "the extra OS-level orphan-kill rail is
    not active here" -- the graceful kill paths in this module are still
    enough for the common case.
    """
    if kill_group is None:
        return False
    return kill_group.assign(pid)


# ---------------------------------------------------------------------------
# wait helpers
# ---------------------------------------------------------------------------


async def wait_exit(
    proc: asyncio.subprocess.Process | None, timeout_s: float
) -> None:
    """Await ``proc.wait()`` up to ``timeout_s`` seconds; never raise on timeout.

    A ``None`` proc or an already-exited proc (``returncode is not None``)
    returns immediately. A timeout is swallowed -- the caller is expected
    to follow up with an escalation step.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return
    except ProcessLookupError:
        return


# ---------------------------------------------------------------------------
# session-lifetime kill (any platform)
# ---------------------------------------------------------------------------


async def kill_session(
    *,
    pid: int,
    proc: asyncio.subprocess.Process | None,
) -> None:
    """Kill a session-lifetime child (best-effort, never raises).

    * **Windows** -- delegate to
      :func:`qai.platform.process.terminate_process_tree` (one shot
      ``taskkill /F /T`` + reap). There is no graceful tree signal on
      Windows; the platform tree_kill helper already implements the
      single-force-step ladder mandated by the design doc §5.8.
    * **POSIX** -- send ``SIGTERM`` to the whole process group
      (``os.killpg(pid, SIGTERM)``) so a child that spawned grandchildren
      is signaled too, then wait :data:`KILL_GRACE_S` for the child to
      exit cleanly. If still alive, escalate with ``SIGKILL`` against the
      same group. Each ``killpg`` call falls back to ``os.kill(pid, ...)``
      (single process) when ``killpg`` raises a non-``ProcessLookupError``
      ``OSError`` -- for example because the child did not actually start
      its own session (no ``start_new_session=True`` at spawn time).

    All ``ProcessLookupError`` is swallowed: a process that already exited
    is the success case, not an error. All other errors are logged and
    swallowed -- kill is *always* best-effort here (the in-process
    ``poll()`` truth source and the Job Object rail are the safety nets).
    """
    if proc is not None and proc.returncode is not None:
        return
    if pid <= 0:
        return

    if sys.platform == "win32":
        # Reuse the platform tree-kill helper; it already does
        # taskkill /F /T + reap and is import-linter-clean from every context.
        if proc is not None:
            try:
                await terminate_process_tree(proc)
            except Exception:  # noqa: BLE001 - last-resort, never propagate
                logger.warning(
                    "background_process.kill.kill_session windows tree_kill "
                    "raised pid=%d",
                    pid,
                    exc_info=True,
                )
        else:
            # No Process handle (defensive; should not happen in the
            # session-lifetime flow which always retains the handle).
            # Best-effort: skip; nothing to taskkill against a pid alone
            # without bringing in the subprocess module here (the existing
            # taskkill path lives in :mod:`tree_kill` and only takes a
            # Process).
            logger.debug(
                "background_process.kill.kill_session windows pid=%d "
                "without Process handle; skipping (orphan kill is the "
                "Job Object's job)",
                pid,
            )
        return

    # POSIX: SIGTERM the group, wait grace, SIGKILL the group if still alive.
    _posix_signal_group_or_pid(pid, _sigterm_value())
    await wait_exit(proc, KILL_GRACE_S)
    if proc is not None and proc.returncode is not None:
        return
    if not alive_pid(pid):
        return
    _posix_signal_group_or_pid(pid, _sigkill_value())
    # One final short reap so we don't leave a zombie if the child was
    # actually still our direct child.
    await wait_exit(proc, KILL_GRACE_S)


def _sigterm_value() -> int:  # noqa: D401 - tiny indirection for win32 import
    """Return ``signal.SIGTERM`` (POSIX-only call site)."""
    import signal

    return int(signal.SIGTERM)


def _sigkill_value() -> int:  # noqa: D401 - tiny indirection for win32 import
    """Return ``signal.SIGKILL`` (POSIX-only call site)."""
    import signal

    return int(signal.SIGKILL)


def _posix_signal_group_or_pid(pid: int, sig: int) -> None:
    """Send ``sig`` to the process group ``pid``; fall back to the single pid.

    Best-effort: ``ProcessLookupError`` (target already gone) is the
    success case and is swallowed silently. Any other ``OSError`` -- most
    commonly ``EPERM`` from ``killpg`` because the child was not made a
    session leader at spawn time -- triggers a fallback to
    ``os.kill(pid, sig)`` against the single process. Everything is
    logged at DEBUG, never raises.
    """
    try:
        os.killpg(pid, sig)
        return
    except ProcessLookupError:
        return
    except OSError as exc:
        logger.debug(
            "background_process.kill killpg failed pid=%d sig=%d (%s); "
            "falling back to single-pid kill",
            pid,
            sig,
            exc,
        )
    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(pid, sig)

