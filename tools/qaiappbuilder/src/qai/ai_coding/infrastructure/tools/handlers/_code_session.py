# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Host side of the ``run_code`` tool — owns the interpreter subprocess.

Responsibilities
----------------
* Spawn (and re-spawn) the child interpreter that holds the live namespace.
* Serialise cells onto it: one in flight at a time, so a cell can never see
  a half-applied namespace from a concurrent cell.
* Bound every cell with a hard deadline, and guarantee the child is dead
  (whole tree) when that deadline fires.
* Rebuild transparently after a crash or a timeout kill, because a hung
  cell must not permanently disable the tool.

Why a hard deadline plus rebuild
--------------------------------
State that survives across calls is the point of this tool, but it is also
its failure mode: one cell that blocks forever would otherwise wedge the
session for good. A wall-clock ceiling per cell, followed by a tree kill and
an automatic respawn, converts "session permanently dead" into "this cell
failed, prior state is gone" — recoverable, and the message says so
explicitly so the model knows it must re-establish what it needs.

The child is spawned with the SAME process-tree hygiene the rest of the
platform uses (:func:`no_window_creationflags`,
:func:`terminate_process_tree`) so a native extension that forks helpers
cannot leave orphans behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import sys
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qai.platform.process import (
    no_window_creationflags,
    terminate_process_tree,
)

logger = logging.getLogger("qai.ai_coding.tools.run_code")

#: Absolute path of the child-side script (sibling module, run as a script).
_RUNNER_SCRIPT = str(Path(__file__).resolve().parent / "_code_session_runner.py")

#: Wait budget for the child's initial ``ready`` frame. Generous enough for a
#: cold interpreter start on a loaded machine, short enough to fail fast when
#: the interpreter is broken.
_STARTUP_TIMEOUT_SECONDS = 30.0

#: Grace period between asking the child to stop and killing its tree.
_SHUTDOWN_GRACE_SECONDS = 2.0

#: Default per-cell ceiling when the caller does not name one.
DEFAULT_TIMEOUT_SECONDS = 60.0

#: Upper bound on a caller-supplied timeout; a cell that needs longer than
#: this belongs in a background process, not an interactive namespace.
MAX_TIMEOUT_SECONDS = 600.0

#: Reader ceiling for ONE protocol frame. The child caps a stream frame's text
#: at 128 KiB; JSON escaping of control characters can inflate that ~6x, so the
#: host must accept comfortably more than the default 64 KiB or an oversized
#: frame would be discarded instead of read.
_FRAME_LIMIT_BYTES = 2 * 1024 * 1024

#: Child stderr lines kept for diagnostics. Only used to enrich a startup
#: failure message — the stream itself is drained continuously (an undrained
#: pipe fills at ~64 KiB and blocks the child forever).
_STDERR_TAIL_LINES = 5


@dataclass(slots=True)
class CellOutcome:
    """Everything one executed cell produced."""

    status: str  # "ok" | "error" | "timeout" | "crashed"
    stdout: str = ""
    stderr: str = ""
    displays: list[str] = field(default_factory=list)
    value: str | None = None
    error_name: str | None = None
    error_message: str | None = None
    traceback: str = ""
    count: int = 0
    restarted: bool = False


class CodeSession:
    """A single long-lived interpreter and the lock that serialises it.

    Not safe to share across event loops; the tool handler keeps exactly one
    instance per process (see :func:`get_code_session`).
    """

    def __init__(self, *, python_executable: str | None = None) -> None:
        self._python = python_executable or sys.executable
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._lock = asyncio.Lock()
        self._cells_run = 0

    # -- lifecycle ---------------------------------------------------

    @property
    def is_live(self) -> bool:
        """True when a child exists and has not exited."""
        return self._proc is not None and self._proc.returncode is None

    async def _spawn(self, *, cwd: str | None, env_overrides: dict[str, str]) -> None:
        """Start the child and wait for its ``ready`` frame."""
        # A child that exited on its own between calls is still referenced here
        # (with its stderr pump running). Reap it before overwriting the slot,
        # or the old process and task leak for the daemon's lifetime.
        await self._hard_stop()
        env = os.environ.copy()
        # The protocol is UTF-8 JSON over pipes; on Windows the child would
        # otherwise encode with the console code page and mangle non-ASCII.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        # A stale PYTHONPATH/-HOME from the parent can make the child import a
        # different interpreter's stdlib; drop both and let the child use its
        # own defaults.
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        env.update(env_overrides)

        self._stderr_tail.clear()
        try:
            proc = await asyncio.create_subprocess_exec(
                self._python,
                "-u",
                _RUNNER_SCRIPT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                limit=_FRAME_LIMIT_BYTES,
                creationflags=no_window_creationflags(),
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"run_code: cannot start the interpreter: {exc}"
            ) from exc

        self._proc = proc
        # Must start BEFORE the ready wait: an interpreter that dies on startup
        # writes its traceback to stderr, and an undrained pipe would otherwise
        # be able to block the child mid-write.
        self._start_stderr_drain(proc)
        try:
            frame = await asyncio.wait_for(
                self._read_frame(), timeout=_STARTUP_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            await self._hard_stop()
            raise RuntimeError(
                "run_code: the interpreter did not become ready in "
                f"{_STARTUP_TIMEOUT_SECONDS:.0f}s{self._stderr_hint()}"
            ) from exc
        except asyncio.CancelledError:
            # Nobody else holds this child: dropping the reference without
            # killing it would leak an interpreter for the process's lifetime.
            await self._kill_shielded()
            raise
        if frame is None or frame.get("type") != "ready":
            await self._hard_stop()
            raise RuntimeError(
                "run_code: the interpreter produced no ready signal"
                f"{self._stderr_hint()}"
            )

    def _start_stderr_drain(self, proc: asyncio.subprocess.Process) -> None:
        """Continuously consume the child's real stderr (never let it fill)."""
        stream = proc.stderr
        if stream is None:
            return

        async def _pump() -> None:
            while True:
                try:
                    raw = await stream.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    continue  # oversized line: readline already dropped it
                except (OSError, asyncio.IncompleteReadError):
                    return
                if not raw:
                    return
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    logger.debug("run_code: child stderr: %s", text[:500])

        self._stderr_task = asyncio.create_task(_pump())

    def _stderr_hint(self) -> str:
        """Render the child's last stderr lines for a startup failure message."""
        if not self._stderr_tail:
            return ""
        return " (interpreter stderr: " + " | ".join(self._stderr_tail) + ")"

    async def _kill_shielded(self) -> None:
        """Kill the child even while the calling task is being cancelled.

        ``terminate_process_tree`` awaits the reap, so a second cancellation
        landing on this task would otherwise orphan the child mid-kill (the
        shielding contract that function documents for its callers).
        """
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(self._hard_stop())

    async def _hard_stop(self) -> None:
        """Kill the child's whole tree; safe to call when already dead."""
        proc = self._proc
        self._proc = None
        task = self._stderr_task
        self._stderr_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if proc is None:
            return
        with contextlib.suppress(Exception):
            await terminate_process_tree(proc)

    async def shutdown(self) -> None:
        """Ask the child to exit, then force it if it lingers."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            await self._hard_stop()
            return
        with contextlib.suppress(Exception):
            await self._write({"op": "shutdown"})
        # Close the request stream too: the child's read loop then hits EOF and
        # unwinds even if the shutdown frame never reached it.
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_GRACE_SECONDS)
        await self._hard_stop()

    # -- framing -----------------------------------------------------

    async def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("run_code: the interpreter is not running")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        proc.stdin.write(line.encode("utf-8"))
        await proc.stdin.drain()

    async def _read_frame(self) -> dict[str, Any] | None:
        """Read one JSON frame; ``None`` at EOF (child gone)."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        stream = proc.stdout
        while True:
            try:
                raw = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # A frame longer than the reader's buffer: ``readline`` has
                # already dropped the partial line, so just keep reading rather
                # than treating an unreadable frame as a dead child.
                logger.debug("run_code: oversized frame from child; dropped")
                continue
            except (OSError, asyncio.IncompleteReadError):
                return None
            if not raw:
                return None
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except json.JSONDecodeError:
                # Not a frame (a native extension writing to fd 1, say).
                logger.debug("run_code: non-frame line from child: %s", text[:200])
                continue
            if isinstance(frame, dict):
                return frame

    # -- execution ---------------------------------------------------

    async def run(
        self,
        code: str,
        *,
        timeout: float,
        reset: bool,
        cwd: str | None,
        env_overrides: dict[str, str] | None = None,
    ) -> CellOutcome:
        """Execute *code* in the persistent namespace.

        Serialised by an :class:`asyncio.Lock`: concurrent callers queue
        rather than interleaving cells into one namespace.
        """
        async with self._lock:
            restarted = False
            if not self.is_live:
                await self._spawn(cwd=cwd, env_overrides=env_overrides or {})
                # A fresh child means prior names are gone. Say so when the
                # caller did not ask for it, so a silent state loss cannot be
                # mistaken for a namespace that merely forgot one binding.
                restarted = self._cells_run > 0
            outcome = await self._run_once(code, timeout=timeout, reset=reset)
            outcome.restarted = restarted or outcome.restarted
            self._cells_run += 1
            return outcome

    async def _run_once(
        self, code: str, *, timeout: float, reset: bool
    ) -> CellOutcome:
        req_id = uuid.uuid4().hex
        outcome = CellOutcome(status="ok")
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        try:
            await self._write({"id": req_id, "code": code, "reset": reset})
        except (OSError, RuntimeError, ValueError):
            # Any failure to hand the request over means the child is not
            # reachable: a dead pipe (OSError / BrokenPipeError), a closed
            # transport (RuntimeError / ValueError), or no child at all.
            await self._hard_stop()
            return CellOutcome(
                status="crashed",
                stderr="the interpreter exited before the cell was sent",
                restarted=True,
            )

        try:
            status = await asyncio.wait_for(
                self._drain(req_id, outcome, stdout_parts, stderr_parts),
                timeout=timeout,
            )
        except TimeoutError:
            # The cell is still running inside the child; the ONLY reliable way
            # to reclaim the interpreter is to kill it. State is lost, which the
            # caller is told explicitly.
            await self._hard_stop()
            outcome.status = "timeout"
            outcome.stdout = "".join(stdout_parts)
            outcome.stderr = "".join(stderr_parts)
            outcome.restarted = True
            return outcome
        except asyncio.CancelledError:
            # The caller went away mid-cell. Leaving the child alive would let
            # an unobserved cell keep mutating the namespace AND leave its
            # frames in the pipe for the next request to misread, so the
            # interpreter goes with the request.
            await self._kill_shielded()
            raise

        outcome.stdout = "".join(stdout_parts)
        outcome.stderr = "".join(stderr_parts)
        if status == "crashed":
            tail = self._stderr_hint()
            await self._hard_stop()
            outcome.status = "crashed"
            outcome.stderr = (outcome.stderr + tail).strip()
            outcome.restarted = True
            return outcome
        outcome.status = status
        return outcome

    async def _drain(
        self,
        req_id: str,
        outcome: CellOutcome,
        stdout_parts: list[str],
        stderr_parts: list[str],
    ) -> str:
        """Consume frames until *req_id*'s ``done`` frame (or EOF).

        Returns the child-reported status, or ``"crashed"`` at EOF. Frames are
        attributed to the request that produced them: a thread the previous
        cell spawned can still be printing, and its output must not be folded
        into this cell's result.
        """
        while True:
            frame = await self._read_frame()
            if frame is None:
                return "crashed"
            frame_id = frame.get("id")
            if frame_id is not None and frame_id != req_id:
                continue
            kind = frame.get("type")
            if kind == "stdout":
                stdout_parts.append(str(frame.get("text", "")))
            elif kind == "stderr":
                stderr_parts.append(str(frame.get("text", "")))
            elif kind == "display":
                outcome.displays.append(str(frame.get("text", "")))
            elif kind == "value":
                outcome.value = str(frame.get("text", ""))
            elif kind == "error":
                outcome.error_name = str(frame.get("name", "") or "")
                outcome.error_message = str(frame.get("message", "") or "")
                outcome.traceback = str(frame.get("traceback", "") or "")
            elif kind == "done":
                outcome.count = int(frame.get("count") or 0)
                return str(frame.get("status") or "ok")


#: Process-wide session holder. One namespace per daemon keeps the mental
#: model simple: "the names I bound are still there next call". Held in a
#: dict rather than a rebindable module global so the accessors below mutate
#: a container instead of declaring ``global`` (AGENTS.md forbids global
#: mutable state; a single-slot holder is the smallest honest alternative).
_SESSION_HOLDER: dict[str, CodeSession] = {}


def get_code_session() -> CodeSession:
    """Return the process-wide session, creating it on first use."""
    session = _SESSION_HOLDER.get("current")
    if session is None:
        session = CodeSession()
        _SESSION_HOLDER["current"] = session
    return session


async def shutdown_code_session() -> None:
    """Stop the session's interpreter (idempotent); for app shutdown."""
    session = _SESSION_HOLDER.pop("current", None)
    if session is not None:
        await session.shutdown()


def resolve_timeout(raw: Any) -> float:
    """Clamp a caller-supplied timeout into the supported window."""
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    # NaN survives both comparisons below (``nan <= 0`` and ``min`` are False /
    # NaN-propagating), and ``asyncio.wait_for(timeout=nan)`` fires instantly —
    # a cell that never ran would be reported as having timed out.
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(value, MAX_TIMEOUT_SECONDS)
