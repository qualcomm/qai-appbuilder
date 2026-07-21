# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Real :class:`ProcessRunnerPort` adapter using :mod:`asyncio.subprocess`.

Cross-platform notes
--------------------

* The runner uses :func:`asyncio.create_subprocess_exec` so no shell is
  ever invoked. ``argv`` must therefore be a fully-tokenised sequence.
* Windows differences vs POSIX are kept inside this module:
    * ``preexec_fn`` is unavailable on Windows so the runner does not
      use one (POSIX would be fine without it too because we never
      ``setsid``);
    * killing on timeout uses :meth:`Process.kill` which on Windows
      maps to ``TerminateProcess`` and on POSIX sends ``SIGKILL``;
    * the implementation does not depend on process groups for the
      normal exit path. On timeout/cap teardown it makes a *best-effort*
      Windows ``taskkill /T`` subtree kill (see ``_best_effort_tree_kill``)
      so a wrapper that spawned a worker doesn't leak grandchildren;
      POSIX no-ops (no session group is started). This runner stays
      synchronous-batch with stdout/stderr pumps by intent.

Output capping
--------------

Both stdout and stderr are pumped concurrently into the same byte
counter. When the cap is reached the runner kills the child and waits
for the readers to drain. The terminating frame's ``truncated`` flag is
set even if the child exited cleanly between hitting the cap and the
kill landing (i.e. cap dominates).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Mapping
from typing import Any

from qai.platform.logging import get_logger

from .ports import (
    ProcessExecutionRequest,
    ProcessExitStatus,
    ProcessFrame,
    ProcessRunnerPort,
    ProcessStartedFrame,
    ProcessStderrFrame,
    ProcessStdoutFrame,
    ProcessTerminatedFrame,
)

__all__ = ["SubprocessProcessRunner"]


_log = get_logger(__name__)

# 64 KiB read buffer per pump iteration -- balances syscalls vs.
# responsiveness. Frames smaller than this are still emitted as-is when
# the pipe drains.
_READ_CHUNK_SIZE = 64 * 1024


class SubprocessProcessRunner:
    """Default :class:`ProcessRunnerPort` implementation."""

    __slots__ = ("_read_chunk_size",)

    def __init__(self, *, read_chunk_size: int = _READ_CHUNK_SIZE) -> None:
        if read_chunk_size <= 0:
            raise ValueError(
                f"read_chunk_size must be > 0, got {read_chunk_size!r}"
            )
        self._read_chunk_size = read_chunk_size

    def run(
        self, request: ProcessExecutionRequest
    ) -> AsyncIterator[ProcessFrame]:
        # Validate cwd synchronously -- we want a clear error before any
        # frame is yielded, matching the Port contract.
        if request.cwd is not None and not os.path.isdir(request.cwd):
            raise ValueError(
                f"cwd does not exist or is not a directory: {request.cwd!r}"
            )
        return self._run(request)

    async def _run(  # noqa: C901 -- single coroutine; keeps state local
        self, request: ProcessExecutionRequest
    ) -> AsyncIterator[ProcessFrame]:
        env = self._materialise_env(request.env)
        # TEMP DIAGNOSTIC (2026-07-12): log the EXACT spawn parameters at the
        # final spawn point so a 0xC000007B / 3221225595 crash of e.g. sh.exe
        # can be diagnosed from the running daemon's log.
        _log.info(
            "SPAWN_DIAG argv=%r cwd=%r PATH_head=%r",
            list(request.argv),
            request.cwd,
            (env.get("PATH", "") if env else "")[:500],
        )
        # When the caller wants to feed bytes to the child's stdin we
        # attach a PIPE; otherwise keep the PR-041 DEVNULL behaviour so
        # the 31 existing call sites are byte-for-byte unchanged.
        stdin_param = (
            asyncio.subprocess.PIPE
            if request.stdin_data is not None
            else asyncio.subprocess.DEVNULL
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *request.argv,
                cwd=request.cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=stdin_param,
            )
        except FileNotFoundError as exc:
            # Map to a synthetic terminated frame so the consumer sees a
            # consistent shape regardless of whether the executable
            # exists. We pick exit_code = 127 -- the conventional "command
            # not found" code used by POSIX shells.
            yield ProcessTerminatedFrame(
                status=ProcessExitStatus(exit_code=127)
            )
            _log.warning("SPAWN_DIAG executable_missing argv=%r exc=%r",
                         request.argv, exc)
            return
        except (PermissionError, NotADirectoryError, OSError) as exc:
            # Permission denied / cwd-mid-flight removed / other OS-level
            # spawn failures. Surface as exit_code=126 (POSIX convention
            # for "found but not executable").
            yield ProcessTerminatedFrame(
                status=ProcessExitStatus(exit_code=126)
            )
            _log.warning("SPAWN_DIAG spawn_failed argv=%r exc=%r",
                         request.argv, exc)
            return

        assert proc.stdout is not None
        assert proc.stderr is not None
        _log.info("SPAWN_DIAG spawned pid=%s argv0=%r", proc.pid, request.argv[0] if request.argv else None)
        yield ProcessStartedFrame(pid=proc.pid)

        # Single shared queue keeps stdout / stderr ordering stable: the
        # frame that arrives first in real time is forwarded first.
        # ``None`` is the "this stream closed" sentinel.
        queue: asyncio.Queue[ProcessFrame | None] = asyncio.Queue()
        cap = request.output_byte_cap
        bytes_seen = 0
        truncated = False
        kill_requested = False

        async def _pump_stdin(payload: bytes) -> None:
            # Write the request envelope concurrently with the read
            # pumps so a payload larger than the OS pipe buffer (Win
            # 64 KiB / Linux 64 KiB+) cannot wedge the runner. The 16
            # MiB cap on ``stdin_data`` keeps this loop bounded.
            stdin_writer = proc.stdin
            if stdin_writer is None:  # pragma: no cover - PIPE always sets it
                return
            try:
                stdin_writer.write(payload)
                await stdin_writer.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # The child exited (or closed stdin) before reading the
                # request. Not fatal — surfaces via the terminated
                # frame's exit_code and the runner's normal pump path.
                pass
            finally:
                try:
                    stdin_writer.close()
                except Exception:  # noqa: BLE001 - last-resort cleanup
                    pass
                # ``wait_closed`` is best-effort: on some platforms a
                # transport that already encountered an error will
                # raise here even though ``close`` succeeded.
                try:
                    await stdin_writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass

        stdin_task: asyncio.Task[None] | None = None
        if request.stdin_data is not None:
            stdin_task = asyncio.create_task(
                _pump_stdin(request.stdin_data),
                name="process-runner-stdin",
            )

        async def _pump(
            stream: asyncio.StreamReader, factory: type
        ) -> None:
            while True:
                try:
                    chunk = await stream.read(self._read_chunk_size)
                except asyncio.CancelledError:
                    # Cooperative cancellation MUST propagate — never
                    # swallow it as if the stream merely ended.
                    raise
                except ConnectionError:
                    break
                if not chunk:
                    break
                await queue.put(factory(data=chunk))
            await queue.put(None)

        stdout_task = asyncio.create_task(
            _pump(proc.stdout, ProcessStdoutFrame),
            name="process-runner-stdout",
        )
        stderr_task = asyncio.create_task(
            _pump(proc.stderr, ProcessStderrFrame),
            name="process-runner-stderr",
        )

        async def _await_exit() -> None:
            # Driven as a task so the timeout below can cancel waiters
            # without interfering with the queue drain.
            await proc.wait()

        wait_task = asyncio.create_task(
            _await_exit(), name="process-runner-wait"
        )

        timed_out = False
        deadline_handle: asyncio.TimerHandle | None = None
        loop = asyncio.get_running_loop()

        def _on_deadline() -> None:
            nonlocal timed_out, kill_requested, deadline_handle
            if proc.returncode is not None:
                return
            # 2026-07-08 — pause the timeout while the child is BLOCKED on a
            # native FileGuard authorization dialog. The caller may inject
            # ``request.ask_pending_probe`` (a context-neutral callable): given
            # the child pid it returns True iff a native ASK is pending on this
            # process tree (State-Truth-First — asks the authority, not a
            # guess). If so we RE-ARM the deadline instead of killing, so the
            # user's decision time is not counted against the timeout. Orphan
            # safety is preserved: without a pending ASK (or no probe) the
            # child is still killed on time. Probe failure → kill (never stall).
            probe = request.ask_pending_probe
            if probe is not None and proc.pid is not None:
                try:
                    blocked = bool(probe(proc.pid))
                except Exception:  # noqa: BLE001 — probe failure must not stall
                    blocked = False
                if blocked and request.timeout_s is not None:
                    deadline_handle = loop.call_later(
                        request.timeout_s, _on_deadline
                    )
                    return
            timed_out = True
            kill_requested = True
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - race
                pass

        if request.timeout_s is not None:
            deadline_handle = loop.call_later(
                request.timeout_s, _on_deadline
            )

        eos_seen = 0  # how many None sentinels we've consumed
        try:
            while eos_seen < 2:
                frame = await queue.get()
                if frame is None:
                    eos_seen += 1
                    continue
                # Check cap BEFORE yielding so the consumer never sees
                # bytes beyond the cap.
                if isinstance(
                    frame, (ProcessStdoutFrame, ProcessStderrFrame)
                ):
                    bytes_seen += len(frame.data)
                    if cap is not None and bytes_seen > cap:
                        # Trim the over-cap chunk to fit, mark truncated,
                        # kill the child, and stop pumping further data.
                        overflow = bytes_seen - cap
                        keep = len(frame.data) - overflow
                        truncated = True
                        if keep > 0:
                            trimmed = (
                                ProcessStdoutFrame(data=frame.data[:keep])
                                if isinstance(frame, ProcessStdoutFrame)
                                else ProcessStderrFrame(data=frame.data[:keep])
                            )
                            yield trimmed
                        if not kill_requested:
                            kill_requested = True
                            try:
                                proc.kill()
                            except ProcessLookupError:  # pragma: no cover
                                pass
                        # Drain remaining sentinels but discard further
                        # data frames.
                        while eos_seen < 2:
                            tail = await queue.get()
                            if tail is None:
                                eos_seen += 1
                        break
                yield frame
        finally:
            if deadline_handle is not None:
                deadline_handle.cancel()
            # Make sure all helper tasks unwind even on consumer cancel.
            helper_tasks: tuple[asyncio.Task[Any] | None, ...] = (
                stdout_task, stderr_task, wait_task, stdin_task,
            )
            for task in helper_tasks:
                if task is not None and not task.done():
                    task.cancel()
            for task in helper_tasks:
                if task is None:
                    continue
                try:
                    await task
                except asyncio.CancelledError:
                    # These helper tasks were cancelled by us just above;
                    # their CancelledError is expected and benign.
                    pass
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    _log.warning(
                        "process_runner.helper_task_cleanup_failed",
                        task=getattr(task, "get_name", lambda: "?")(),
                        exc_info=True,
                    )
            if proc.returncode is None:
                # L-8: best-effort subtree teardown. ``proc.kill()`` only
                # signals the DIRECT child; a process that itself spawned
                # grandchildren would leave them orphaned on timeout/cap.
                # This matches V1's non-sandbox path (which also only killed
                # the direct child) so it is not a regression — but we add a
                # platform-guarded best-effort tree kill so the common case
                # (a wrapper that fork/execs a real worker) doesn't leak.
                # Sandboxed execution still relies on the launcher's Job
                # Object (KILL_ON_JOB_CLOSE) for guaranteed subtree cleanup;
                # this is the un-sandboxed convenience path only.
                _best_effort_tree_kill(proc.pid)
                try:
                    proc.kill()
                except ProcessLookupError:  # pragma: no cover - race
                    pass
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001 - last-resort cleanup
                    pass

        exit_code: int | None
        if timed_out or truncated:
            # Caller's perspective: the process did not "succeed", and
            # the OS-level returncode (whatever it is from kill) is
            # noise. Surface ``None`` so consumers always check the flag.
            exit_code = None
        else:
            exit_code = proc.returncode

        yield ProcessTerminatedFrame(
            status=ProcessExitStatus(
                exit_code=exit_code,
                timed_out=timed_out,
                truncated=truncated,
            )
        )

    @staticmethod
    def _materialise_env(
        env: Mapping[str, str] | None,
    ) -> dict[str, str] | None:
        if env is None:
            return None
        # Copy + coerce so callers can't mutate the dict mid-spawn.
        result: dict[str, str] = {}
        for k, v in env.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise TypeError(
                    "env keys and values must be str, "
                    f"got ({type(k).__name__}, {type(v).__name__})"
                )
            result[k] = v
        return result


# Type-check that we still implement the protocol after edits.
_runtime_check: ProcessRunnerPort = SubprocessProcessRunner()
del _runtime_check


def _best_effort_tree_kill(pid: int) -> None:
    """Best-effort kill of ``pid`` and its child subtree.

    Platform-guarded (``sys.platform``) with graceful fallback so the
    cross-platform / Linux-CI posture (AGENTS.md) is preserved:

    * Windows: ``taskkill /F /T /PID`` walks the child tree by PID.
    * POSIX: the runner does not ``start_new_session`` (to keep the 31
      existing call sites byte-for-byte unchanged), so there is no
      process group to target; we no-op here and let ``proc.kill()``
      handle the direct child (same as V1). A future opt-in flag could
      add ``start_new_session`` + ``os.killpg`` if a caller needs it.

    Never raises — this is last-resort cleanup; any failure (missing
    ``taskkill``, race where the tree already exited, permission) is
    swallowed and logged at debug.
    """
    if sys.platform != "win32":  # POSIX: no session group to target
        return
    try:
        import subprocess

        subprocess.run(  # noqa: S603 - fixed argv, pid is an int
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001 - best-effort, never propagate
        _log.debug("process_runner.tree_kill_failed pid=%r", pid,
                   exc_info=True)
_ = Any  # silence unused-import linter
