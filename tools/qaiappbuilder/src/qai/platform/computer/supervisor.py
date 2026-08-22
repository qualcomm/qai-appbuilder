# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Subprocess supervisor implementing :class:`DesktopControllerPort`.

Owns the worker subprocess lifecycle and the async IPC. Key invariants
(book 03 §4):

* **Lazy start** — the worker spawns on the first ``execute``; ``init``
  runs then and its ``ready`` capabilities are cached.
* **Serial execution** — an ``asyncio.Lock`` serializes batches so two
  ``execute`` calls never enter the worker concurrently.
* **Cancel = terminate** — a cancelled ``execute`` terminates the worker
  (no half-executed input left dangling).
* **Bounded, idempotent close** — ``close`` sends ``close``, waits
  briefly for ``closed``, then force-terminates the process tree.

Also provides the owner registry + bounded release used by the chat
bridge to tie desktop sessions to a conversation and reap them on
teardown (book 03 §6).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import _protocol as proto
from .ports import DesktopControllerPort
from .types import Capabilities, Capture, DesktopError, SessionOptions

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.process import ProcessKillGroup

from qai.platform.logging import get_logger

__all__ = [
    "ComputerSupervisor",
    "register_computer_controller",
    "release_computer_sessions_for_owner",
    "smoke_test_computer_worker",
]

_log = get_logger(__name__)

# Timeout constants (book 03 §8).
_START_TIMEOUT_S = 10.0
_CLOSE_TIMEOUT_S = 1.5
_SMOKE_TIMEOUT_S = 5.0
_OPERATION_TIMEOUT_S = 8.0

#: StreamReader line limit for worker stdout. A ``result`` frame embeds a
#: base64 PNG; a full-screen capture can be a few hundred KB, so the
#: default 64 KiB limit would raise ``LimitOverrunError`` mid-line. 32 MiB
#: covers the composite-pixel cap with wide margin.
_STREAM_LIMIT_BYTES = 32 * 1024 * 1024

_WORKER_MODULE = "qai.platform.computer.worker_entry"


class ComputerSupervisor(DesktopControllerPort):
    """Async controller backed by an isolated worker subprocess."""

    __slots__ = (
        "_options",
        "_kill_group",
        "_proc",
        "_lock",
        "_caps",
        "_next_id",
        "_closed",
        "_reader_task",
        "_pending",
        "_pending_ready",
        "_python",
    )

    def __init__(
        self,
        *,
        options: SessionOptions | None = None,
        kill_group: "ProcessKillGroup | None" = None,
        python_executable: str | None = None,
    ) -> None:
        self._options = options or SessionOptions()
        self._kill_group = kill_group
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._caps: Capabilities | None = None
        self._next_id = 0
        self._closed = False
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Capture]] = {}
        self._pending_ready: asyncio.Future[Capabilities] | None = None
        self._python = python_executable or sys.executable

    @property
    def capabilities(self) -> Capabilities | None:
        return self._caps

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(self, actions: list) -> Capture:
        if self._closed:
            raise DesktopError("controller is closed", name="Closed")
        async with self._lock:
            await self._ensure_started()
            frame_id = self._next_id
            self._next_id += 1
            fut: asyncio.Future[Capture] = asyncio.get_running_loop().create_future()
            self._pending[frame_id] = fut
            _log.info(
                "computer.supervisor.execute",
                frame_id=frame_id,
                action_count=len(actions),
                kinds=[getattr(a, "type", "?") for a in actions],
            )
            try:
                await self._write(proto.make_execute(frame_id, actions))
                cap = await asyncio.wait_for(fut, timeout=_OPERATION_TIMEOUT_S)
                _log.info(
                    "computer.supervisor.execute_ok",
                    frame_id=frame_id,
                    width=getattr(cap, "width", None),
                    height=getattr(cap, "height", None),
                    png_bytes=len(cap.data) if getattr(cap, "data", None) else 0,
                )
                return cap
            except asyncio.CancelledError:
                # Cancel => terminate the worker; no half-run input dangling.
                _log.warning("computer.supervisor.execute_cancelled", frame_id=frame_id)
                await self._terminate("execute cancelled")
                raise
            except (asyncio.TimeoutError, DesktopError) as exc:
                _log.warning(
                    "computer.supervisor.execute_failed",
                    frame_id=frame_id,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                await self._terminate("execute failed")
                raise
            finally:
                self._pending.pop(frame_id, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            with contextlib.suppress(Exception):
                await self._write(proto.make_close())
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=_CLOSE_TIMEOUT_S)
        finally:
            await self._terminate("close")

    # ------------------------------------------------------------------
    # Lifecycle internals
    # ------------------------------------------------------------------

    async def _ensure_started(self) -> None:
        if self._proc is not None:
            return
        try:
            from qai.platform.process import no_window_creationflags
        except Exception:  # noqa: BLE001
            def no_window_creationflags() -> int:  # type: ignore[misc]
                return 0

        creationflags = no_window_creationflags() if sys.platform == "win32" else 0
        start_new_session = sys.platform != "win32"
        proc = await asyncio.create_subprocess_exec(
            self._python,
            "-m",
            _WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=start_new_session,
            creationflags=creationflags,
            # A ``result`` frame carries a base64 PNG that easily exceeds
            # the default 64 KiB StreamReader line limit; raise it so a
            # full-screen screenshot line is read in one piece.
            limit=_STREAM_LIMIT_BYTES,
        )
        self._proc = proc
        _log.info("computer.worker.spawned", pid=proc.pid, module=_WORKER_MODULE)
        if proc.pid is not None and self._kill_group is not None:
            try:
                from qai.platform.background_process.kill import assign_to_job

                assign_to_job(self._kill_group, proc.pid)
            except Exception:  # noqa: BLE001
                _log.warning("computer.worker.job_assign_failed", exc_info=True)
        self._reader_task = asyncio.create_task(self._read_loop(proc))
        if proc.stderr is not None:
            asyncio.create_task(self._drain_stderr(proc))
        await self._init()

    async def _drain_stderr(self, proc: "asyncio.subprocess.Process") -> None:
        """Surface worker stderr (crashes / tracebacks) into the log.

        The worker's protocol channel is stdout; stderr carries only
        unexpected diagnostics (import errors, Win32 faults). Draining it
        here makes a worker that dies before ``ready`` visible in logs
        instead of a bare init timeout.
        """
        assert proc.stderr is not None
        try:
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    _log.warning("computer.worker.stderr", pid=proc.pid, line=text)
        except Exception:  # noqa: BLE001
            pass

    async def _init(self) -> None:
        assert self._proc is not None
        ready: asyncio.Future[Capabilities] = asyncio.get_running_loop().create_future()
        self._pending_ready = ready
        await self._write(proto.make_init(self._options.to_dict()))
        try:
            self._caps = await asyncio.wait_for(ready, timeout=_START_TIMEOUT_S)
            _log.info(
                "computer.worker.ready",
                backend=getattr(self._caps, "backend", None),
                capture=getattr(self._caps, "capture", None),
                input=getattr(self._caps, "input", None),
                capture_permission=getattr(self._caps, "capture_permission", None),
                input_permission=getattr(self._caps, "input_permission", None),
                display_count=getattr(self._caps, "display_count", None),
            )
        except asyncio.TimeoutError:
            _log.warning("computer.worker.init_timeout")
            await self._terminate("init timeout")
            raise DesktopError("worker init timed out", name="StartTimeout")

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                self._dispatch_frame(line)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._fail_all(DesktopError("worker stream closed", name="WorkerGone"))

    def _dispatch_frame(self, line: str) -> None:
        try:
            frame = proto.decode_frame(line)
        except Exception:  # noqa: BLE001
            return
        if frame.type == "ready":
            fut = self._pending_ready
            if fut is not None and not fut.done():
                fut.set_result(Capabilities.from_dict(frame.payload["capabilities"]))
            return
        if frame.type == "result":
            fid = int(frame.payload.get("id", -1))
            self._caps = Capabilities.from_dict(frame.payload["capabilities"])
            fut = self._pending.get(fid)
            if fut is not None and not fut.done():
                fut.set_result(proto.capture_from_wire(frame.payload["capture"]))
            return
        if frame.type == "error":
            err = frame.payload.get("error") or {}
            exc = DesktopError(
                str(err.get("message", "worker error")),
                name=str(err.get("name", "WorkerError")),
            )
            fid = frame.payload.get("id")
            if fid is None:
                ready = self._pending_ready
                if ready is not None and not ready.done():
                    ready.set_exception(exc)
                self._fail_all(exc)
            else:
                fut = self._pending.get(int(fid))
                if fut is not None and not fut.done():
                    fut.set_exception(exc)
            return
        # pong / closed: no waiter to resolve here.

    def _fail_all(self, exc: BaseException) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        ready = self._pending_ready
        if ready is not None and not ready.done():
            ready.set_exception(exc)

    async def _write(self, frame: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise DesktopError("worker not running", name="WorkerGone")
        proc.stdin.write(proto.encode_frame(frame))
        await proc.stdin.drain()

    async def _terminate(self, reason: str) -> None:
        proc = self._proc
        self._proc = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        self._fail_all(DesktopError(f"worker terminated: {reason}", name="WorkerGone"))
        if proc is None:
            return
        try:
            from qai.platform.process import terminate_process_tree

            await asyncio.shield(terminate_process_tree(proc))
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                proc.kill()
                await proc.wait()


# ---------------------------------------------------------------------------
# Owner registry + release (book 03 §6)
# ---------------------------------------------------------------------------

_owned: dict[str, set[DesktopControllerPort]] = {}

_RELEASE_TIMEOUT_S = 3.0


def register_computer_controller(
    owner_id: str | None, controller: DesktopControllerPort
) -> Callable[[], None]:
    """Register ``controller`` under ``owner_id``; return an unregister fn.

    A falsy ``owner_id`` yields a no-op unregister (no ownership tracked).
    """
    if not owner_id:
        return lambda: None
    _owned.setdefault(owner_id, set()).add(controller)

    def _unregister() -> None:
        bucket = _owned.get(owner_id)
        if bucket is None:
            return
        bucket.discard(controller)
        if not bucket:
            _owned.pop(owner_id, None)

    return _unregister


async def release_computer_sessions_for_owner(owner_id: str | None) -> None:
    """Close + drop every controller registered under ``owner_id``.

    Closes concurrently (all-settled); a bounded timeout means a stuck
    close logs a warning rather than blocking teardown.
    """
    if not owner_id:
        return
    controllers = _owned.pop(owner_id, set())
    if not controllers:
        return

    async def _close(c: DesktopControllerPort) -> None:
        with contextlib.suppress(Exception):
            await c.close()

    try:
        await asyncio.wait_for(
            asyncio.gather(*(_close(c) for c in controllers), return_exceptions=True),
            timeout=_RELEASE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _log.warning("computer.release.timeout", owner_id=owner_id)


# ---------------------------------------------------------------------------
# Smoke self-check (book 03 §7)
# ---------------------------------------------------------------------------


async def smoke_test_computer_worker(*, timeout: float = _SMOKE_TIMEOUT_S) -> bool:
    """Spawn a worker, ping it, confirm ``pong``, then terminate.

    Does NOT touch the real desktop (no init/execute). Returns ``True``
    on a clean ping/pong round-trip.
    """
    try:
        from qai.platform.process import no_window_creationflags
    except Exception:  # noqa: BLE001
        def no_window_creationflags() -> int:  # type: ignore[misc]
            return 0

    creationflags = no_window_creationflags() if sys.platform == "win32" else 0
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        _WORKER_MODULE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=creationflags,
        limit=_STREAM_LIMIT_BYTES,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(proto.encode_frame(proto.make_ping(1)))
        await proc.stdin.drain()
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        frame = proto.decode_frame(raw.decode("utf-8").strip())
        return frame.type == "pong" and int(frame.payload.get("id", -1)) == 1
    finally:
        with contextlib.suppress(Exception):
            from qai.platform.process import terminate_process_tree

            await asyncio.shield(terminate_process_tree(proc))
