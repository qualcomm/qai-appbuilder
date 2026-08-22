# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Cross-context process-tree teardown helper (platform shared kernel).

Several bounded contexts spawn a child process that itself fork/execs real
worker processes — a shell running a user command, a ``rg`` search, a streamed
exec engine. ``proc.kill()`` only signals the DIRECT child, so a command that
spawned grandchildren leaves them orphaned on timeout / cancel / stop. This
module is the SINGLE place that implements the subtree teardown so every
context shares one implementation rather than re-inventing it:

* :func:`best_effort_tree_kill` — synchronous: FORCE-kill the direct child
  (``proc.kill()``) immediately AND, on Windows, walk the child tree via
  ``taskkill /F /T /PID``. POSIX-safe (graceful no-op for the tree step). This
  is the immediate hard kill used from a synchronous drain loop where there is
  no event loop to await a grace period — it does NOT do the graceful ladder.
* :func:`terminate_process_tree` — async: a SIGTERM→grace→SIGKILL ladder plus a
  reap (``await proc.wait()``) so a well-behaved child gets a chance to flush /
  clean up temp files before being force-killed, and the OS does not keep a
  zombie / leave the transport open. Idempotent and never raises.

It lives under ``qai.platform.*`` (the shared kernel), NOT under any single
context, so ``qai.ai_coding`` and ``qai.tools`` can both import it without
crossing the ``context-isolation`` import-linter contract (a context importing
another context would break the 17 layered contracts).

Graceful teardown ladder (async path): the most common reason to kill a child
here is a timeout / user Stop on a long-running command. Sending an immediate
SIGKILL denies a well-behaved child the chance to flush buffers, write a partial
result, or remove a temp file. So :func:`terminate_process_tree` first asks the
child to exit gracefully and only escalates to a hard kill after a short grace
period (:data:`_GRACE_KILL_AFTER_SECONDS`):

* **POSIX** — ``proc.terminate()`` (SIGTERM) → wait the grace period → if still
  alive, ``proc.kill()`` (SIGKILL). This is a SINGLE-process ladder: it does NOT
  introduce ``start_new_session`` / ``os.killpg`` here, so the spawn call sites
  stay byte-for-byte as they are (V1 parity, unchanged signal / Ctrl-C
  semantics) and there is no process group to target. A caller that spawns a
  process which itself fork/execs a worker subtree (e.g. ``rg``) and therefore
  needs whole-group targeting is expected to make the child a session leader and
  run its own ``os.killpg`` ladder (the search track does exactly this); the
  timing skeleton — terminate, wait grace, escalate — is the same.
* **Windows** — ``taskkill /F /T /PID`` walks and FORCE-kills the whole tree in
  one step. There is no softer Windows tree signal, so the ladder collapses to a
  single force step (no SIGTERM-equivalent grace stage).

A future opt-in could add session-group targeting to the POSIX path if a caller
needs it without rolling its own ladder.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys

from qai.platform.logging import get_logger

__all__ = ["best_effort_tree_kill", "terminate_process_tree"]

_log = get_logger(__name__)

# Grace period for the reap ``proc.wait()`` after a kill before we give up.
_REAP_TIMEOUT_SECONDS = 5.0
# Grace period after a graceful SIGTERM before escalating to a force SIGKILL
# (POSIX async ladder). Generous enough for a well-behaved child to flush /
# clean up, short enough that a hung child is force-killed promptly. Mirrors the
# search track's ``_RG_FORCE_KILL_AFTER_SECONDS`` so the timing skeleton is the
# same across contexts.
_GRACE_KILL_AFTER_SECONDS = 3.0


def _taskkill_tree(pid: int | None) -> None:
    """Windows ``taskkill /F /T /PID`` subtree kill (best-effort, never raises).

    POSIX is a no-op (no session group to target; the caller's ``proc.kill()``
    handled the direct child).
    """
    if sys.platform != "win32":  # POSIX: no session group to target
        return
    if pid is None:
        return
    try:
        subprocess.run(  # noqa: S603 — fixed argv, no shell; pid is an int
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],  # noqa: S607
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        _log.debug("process.tree_kill.taskkill_failed", pid=pid)


def best_effort_tree_kill(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc`` AND its child subtree (synchronous, best-effort).

    Signals the direct child via ``proc.kill()`` and, on Windows, walks the
    child tree via ``taskkill /F /T /PID`` so a grandchild process is not
    orphaned. Never raises: a process that already exited (``ProcessLookup
    Error`` / ``OSError``) is treated as done. Safe to call more than once.

    Use this from a synchronous context (e.g. inside a drain loop) where an
    immediate, non-awaiting kill is needed. For an awaited kill-and-reap use
    :func:`terminate_process_tree`.
    """
    # already exited / not yet started — best-effort
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    _taskkill_tree(proc.pid)


async def terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc``'s subtree gracefully then reap it (async, idempotent).

    Runs the SIGTERM→grace→SIGKILL ladder (see the module docstring) and then
    awaits ``proc.wait()`` so the child is reaped (no zombie, transport closed).
    A process that has already exited (``returncode is not None``) is a no-op.
    Never raises.

    * **POSIX** — ``proc.terminate()`` (SIGTERM); if the child has not exited
      within :data:`_GRACE_KILL_AFTER_SECONDS`, escalate to
      :func:`best_effort_tree_kill` (SIGKILL). A child that exits on the SIGTERM
      is never hard-killed, so it can flush / clean up.
    * **Windows** — there is no softer tree signal, so we go straight to
      :func:`best_effort_tree_kill` (``taskkill /F /T``), the single force step.

    Cancellation note: callers that run this from a ``finally`` / async
    generator ``finally`` that may itself be re-cancelled should wrap the call
    in :func:`asyncio.shield` (so a second cancel cannot interrupt the reap and
    orphan the child) and then re-raise. This function deliberately does NOT
    shield internally — shielding is the caller's policy decision, and a caller
    that simply wants a best-effort reap on the happy path should not pay for a
    shield it does not need.
    """
    if proc.returncode is not None:
        return

    if sys.platform == "win32":
        # No softer Windows tree signal — force-kill the whole tree in one step.
        best_effort_tree_kill(proc)
    else:
        # POSIX graceful ladder: ask the child to exit, give it a grace period,
        # only then force-kill. ``proc.terminate()`` sends SIGTERM to the direct
        # child (no process group targeting here — see module docstring).
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
        try:
            await asyncio.wait_for(
                proc.wait(), timeout=_GRACE_KILL_AFTER_SECONDS
            )
            return  # exited on SIGTERM — graceful, no force kill, already reaped
        except asyncio.TimeoutError:
            # Still alive after the grace period — escalate to a force kill.
            best_effort_tree_kill(proc)
        except ProcessLookupError:
            return

    try:
        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass
    except Exception:  # noqa: BLE001 — last-resort reap, never propagate
        _log.debug("process.tree_kill.reap_failed", pid=proc.pid)
