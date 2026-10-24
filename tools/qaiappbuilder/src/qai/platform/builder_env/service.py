# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Run ``scripts/setup/install_builder_env.py`` as a background child process.

Why a child process (not in-process code): the install is ~4 GB of downloads, uv/pip
runs and a 2 GB unzip. Isolated in its own process it cannot block the event loop or
any other feature, a crash cannot take the server down, and "cancel" is a clean
process-tree kill.

Contract with the script: one JSON object per stdout line (``start`` / ``step`` /
``progress`` / ``log`` / ``result``). Lines that are not JSON are kept as log text.

Rules enforced here:
* at most ONE job at a time (``AlreadyRunningError`` otherwise);
* the script is started from the *app's own* ``scripts`` folder, never from a path the
  caller supplies; the only caller-controlled inputs are the mirror choice and a proxy
  URL, passed as argv items (no shell);
* a finished job is kept (so the UI can show the failure and offer "retry"); a new
  ``start`` replaces it;
* the job is cancelled / its process tree killed on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from qai.platform.logging import get_logger
from qai.platform.process import terminate_process_tree
from qai.platform.process.spawn_flags import no_window_creationflags

__all__ = [
    "AlreadyRunningError",
    "BuilderEnvService",
    "InstallJob",
    "InstallState",
    "ScriptMissingError",
]

_LOGGER = get_logger(__name__)

_MIRRORS = ("auto", "official", "cn")
_LOG_TAIL = 40
_STREAM_LIMIT = 1 << 20  # one JSON line may be long (a traceback tail)


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill *proc* and everything it started, then reap it. Never raises.

    On Windows the tree must be killed BEFORE the parent: ``terminate_process_tree`` kills the
    parent first, after which ``taskkill /T`` can no longer find its descendants, so a
    grandchild (uv / pip / aria2c) survives, keeps the stdout pipe open and the reader never
    sees EOF. Here ``taskkill /F /T`` goes first; the platform helper is then used to reap
    (and as the fallback on POSIX / when taskkill is unavailable).
    """
    if proc.returncode is None and sys.platform == "win32":
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=no_window_creationflags(),
            )
            await asyncio.wait_for(killer.wait(), timeout=15)
    await terminate_process_tree(proc)


class InstallState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AlreadyRunningError(RuntimeError):
    """An installer job is already running."""


class ScriptMissingError(RuntimeError):
    """The installer script (or the interpreter to run it) cannot be found."""


@dataclass(slots=True)
class InstallJob:
    job_id: str
    state: InstallState = InstallState.RUNNING
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    overall: float = 0.0
    step: str = ""
    step_state: str = ""
    message: str = ""
    # byte/phase/percent progress of the step in flight
    unit: str = ""
    done: int = 0
    total: int | None = None
    speed: int = 0
    eta: int | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    log_tail: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_TAIL))
    # terminal outcome
    error_code: str = ""
    error_message: str = ""
    failed_step: str = ""
    exit_code: int | None = None
    # ("ok" | error code, message, step) from the script's ``result`` line, until the exit settles it
    verdict: tuple[str, str, str] | None = None


class BuilderEnvService:
    """Owns the (single) installer job and the detection entry point."""

    def __init__(
        self,
        *,
        script_path: Path,
        python_exe: Path | str,
        app_root: Path,
        arch: str,
        no_window: Callable[[], int] = no_window_creationflags,
    ) -> None:
        self._script = Path(script_path)
        self._python = str(python_exe)
        self._root = Path(app_root)
        self._arch = arch
        self._no_window = no_window
        self._job: InstallJob | None = None
        self._task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._cancel_requested = False

    # ── queries ────────────────────────────────────────────────────────────

    @property
    def job(self) -> InstallJob | None:
        return self._job

    @property
    def running(self) -> bool:
        return self._job is not None and self._job.state is InstallState.RUNNING

    @property
    def installable(self) -> bool:
        """The installer script is present in this build."""
        return self._script.is_file()

    # ── commands ───────────────────────────────────────────────────────────

    def _argv(self, mirror: str, proxy: str) -> list[str]:
        argv = [self._python, "-u", str(self._script), "--root", str(self._root), "--arch", self._arch]
        if mirror != "auto":
            argv += ["--mirror", mirror]
        if proxy:
            argv += ["--proxy", proxy]
        return argv

    def start(self, *, mirror: str = "auto", proxy: str = "") -> InstallJob:
        """Create the job and launch the child. Returns immediately.

        Must be called from the running event loop (the route handler).
        """
        if self.running:
            raise AlreadyRunningError("a model-conversion environment install is already running")
        if mirror not in _MIRRORS:
            raise ValueError(f"mirror must be one of {_MIRRORS}")
        proxy = proxy.strip()
        if proxy and not proxy.lower().startswith(("http://", "https://", "socks5://", "socks5h://")):
            raise ValueError("proxy must start with http://, https:// or socks5://")
        if not self._script.is_file():
            raise ScriptMissingError(f"installer script not found: {self._script}")
        job = InstallJob(job_id=uuid.uuid4().hex[:12])
        self._job = job
        self._cancel_requested = False
        self._task = asyncio.get_running_loop().create_task(
            self._run(job, self._argv(mirror, proxy)), name=f"builder-env-{job.job_id}"
        )
        return job

    async def cancel(self) -> bool:
        """Stop the running job. True when there was one to stop."""
        if not self.running:
            return False
        self._cancel_requested = True
        task = self._task
        # The child may not exist yet (cancel right after start): wait briefly for the reader to
        # either spawn it (it then honours the flag itself) or finish.
        for _ in range(100):
            if self._proc is not None or task is None or task.done():
                break
            await asyncio.sleep(0.05)
        proc = self._proc
        if proc is not None:
            await asyncio.shield(_kill_tree(proc))
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=20)
        return True

    async def aclose(self) -> None:
        """Shutdown hook: kill the child, wait for the reader to finish."""
        if self.running:
            await self.cancel()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ── the reader ─────────────────────────────────────────────────────────

    async def _run(self, job: InstallJob, argv: list[str]) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self._root),
                limit=_STREAM_LIMIT,
                creationflags=self._no_window() if sys.platform == "win32" else 0,
            )
        except (OSError, ValueError) as exc:
            self._finish(job, InstallState.FAILED, code="spawn_failed", message=f"{type(exc).__name__}: {exc}")
            return
        self._proc = proc
        try:
            if self._cancel_requested:  # cancel() ran before the child existed: honour it now
                await asyncio.shield(_kill_tree(proc))
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                self._ingest(job, raw.decode("utf-8", errors="replace"))
            rc = await proc.wait()
            job.exit_code = rc
            self._conclude(job, rc)
        except asyncio.CancelledError:
            await asyncio.shield(_kill_tree(proc))
            self._finish(job, InstallState.CANCELLED, code="cancelled", message="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - never leave the job in "running"
            await asyncio.shield(_kill_tree(proc))
            _LOGGER.warning("builder_env.reader_failed", error=repr(exc))
            self._finish(job, InstallState.FAILED, code="internal", message=f"{type(exc).__name__}: {exc}")
        finally:
            self._proc = None

    def _conclude(self, job: InstallJob, rc: int) -> None:
        """Settle the final state once the process has exited (exit_code is known).

        The script's own ``result`` line (``job.verdict``) wins over the bare exit code.
        """
        if job.state is not InstallState.RUNNING:
            return
        if job.verdict is not None:
            code, message, step = job.verdict
            if code == "ok":
                self._finish(job, InstallState.SUCCEEDED, code="", message=message)
                job.overall = 100.0
            else:
                if step:
                    job.step = step
                state = InstallState.CANCELLED if code == "cancelled" else InstallState.FAILED
                self._finish(job, state, code=code, message=message)
        elif self._cancel_requested or rc == 3:
            self._finish(job, InstallState.CANCELLED, code="cancelled", message="cancelled")
        elif rc == 0:
            # exit 0 without a result line: trust the exit code, but say so
            self._finish(job, InstallState.SUCCEEDED, code="", message="finished")
            job.overall = 100.0
        else:
            tail = " | ".join(list(job.log_tail)[-3:])
            self._finish(job, InstallState.FAILED, code="exit_" + str(rc),
                         message=f"installer exited with code {rc}" + (f": {tail}" if tail else ""))

    @staticmethod
    def _finish(job: InstallJob, state: InstallState, *, code: str, message: str) -> None:
        if job.state is not InstallState.RUNNING:
            return
        job.state = state
        job.finished_at = time.time()
        if state is not InstallState.SUCCEEDED:
            job.error_code = code
            job.error_message = message
            job.failed_step = job.step
        else:
            job.message = message or job.message

    def _ingest(self, job: InstallJob, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
            if not isinstance(ev, dict):
                raise ValueError
        except ValueError:
            job.log_tail.append(line[:300])  # non-JSON output (e.g. a crash) is still worth showing
            return
        kind = ev.get("event")
        if isinstance(ev.get("overall"), (int, float)):
            job.overall = max(job.overall, float(ev["overall"]))  # never move backwards
        if kind == "start":
            steps = ev.get("steps")
            if isinstance(steps, list):
                job.steps = [s for s in steps if isinstance(s, dict)]
        elif kind == "step":
            job.step = str(ev.get("step") or job.step)
            job.step_state = str(ev.get("state") or "")
            job.message = str(ev.get("message") or "")
            if job.step_state in ("running",):
                job.unit, job.done, job.total, job.speed, job.eta = "", 0, None, 0, None
        elif kind == "progress":
            job.step = str(ev.get("step") or job.step)
            job.unit = str(ev.get("unit") or "")
            job.done = int(ev.get("done") or 0)
            total = ev.get("total")
            job.total = int(total) if isinstance(total, (int, float)) else None
            job.speed = int(ev.get("speed") or 0)
            eta = ev.get("eta")
            job.eta = int(eta) if isinstance(eta, (int, float)) else None
            if ev.get("message"):
                job.message = str(ev["message"])
        elif kind == "log":
            text = str(ev.get("line") or "")
            if text:
                job.log_tail.append(text)
        elif kind == "result":
            # Only RECORD the script's verdict here. The job becomes terminal in ``_conclude``,
            # once the process has really exited and exit_code is known -- otherwise a poll could
            # see "succeeded" while the child is still running.
            ok = bool(ev.get("ok"))
            job.verdict = (
                "ok" if ok else str(ev.get("code") or "failed"),
                str(ev.get("message") or ""),
                str(ev.get("step") or ""),
            )
